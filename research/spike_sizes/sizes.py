"""Spike sizes (PROTOCOL.md): after a move of X within 30 minutes, a 3% target, entering at
once (N) or at the first flat candle (F), against the move (fade) or with it (follow),
with no stop (S0) or a 3% one (S3); Upstox intraday charges and slippage on Rs 1 lakh.

Usage:
  uv run python -I sizes.py dev DATA_DIR BHAV_PARQUET
  uv run python -I sizes.py confirm DATA_DIR BHAV_PARQUET X ENTRY DIRECTION STOP
DATA_DIR holds days.parquet (prepare.py).
"""
import datetime as dt
import math
import sys
from decimal import Decimal
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import polars as pl

from igs.intraday.costs import charges, intraday_rates

DEV = (dt.date(2022, 1, 1), dt.date(2024, 12, 31))
CONFIRM = (dt.date(2025, 1, 1), dt.date(2026, 10, 8))
XS = (0.05, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15)
ENTRIES, DIRECTIONS, STOPS = ('N', 'F'), ('fade', 'follow'), ('S0', 'S3')
TARGET, FLAT, NOTIONAL = 0.03, 0.01, 100_000
SLIP, SLIPS = 0.0015, (0.0005, 0.003)
LAST, EXIT = 14 * 60 + 55, 15 * 60 + 15
RATES = intraday_rates()
ARR = {}            # the period's candles, shared with the workers (fork)


def triggers(t, h, lo, c):
    """{X: (trigger index, +1 a rise / -1 a fall)} for each threshold."""
    n = len(t)
    first = np.searchsorted(t, t - 25, side='left')
    idx = np.arange(n)
    low, high = lo.copy(), h.copy()
    for k in range(1, 6):
        j = idx - k
        ok = (j >= first) & (j >= 0)
        jj = np.where(ok, j, idx)
        low = np.minimum(low, lo[jj])
        high = np.maximum(high, h[jj])
    rise, fall = c / low - 1, c / high - 1
    early = t <= LAST
    out = {}
    for x in XS:
        hit = early & ((rise >= x) | (fall <= -x))
        if hit.any():
            i = int(np.argmax(hit))
            out[x] = (i, 1 if rise[i] >= x else -1)
    return out


def simulate(t, o, h, lo, c, k, s, stop_kind, slip):
    """The trade entered at candle k's open on side s: (raw exit, filled entry, filled exit,
    why, exit candle locked)."""
    e = o[k] * (1 + s * slip)
    target = e * (1 + s * TARGET)
    stop = e * (1 - s * TARGET) if stop_kind == 'S3' else None
    end = int(np.searchsorted(t, EXIT, side='left'))        # first candle from 15:15
    end = max(end, k)
    seg = slice(k, end)
    hit_t = (lo[seg] <= target) if s < 0 else (h[seg] >= target)
    hit_s = ((h[seg] >= stop) if s < 0 else (lo[seg] <= stop)) if stop is not None else None
    it = int(np.argmax(hit_t)) if hit_t.any() else None
    is_ = int(np.argmax(hit_s)) if hit_s is not None and hit_s.any() else None
    if is_ is not None and (it is None or is_ <= it):
        m = k + is_
        beyond = (o[m] >= stop) if s < 0 else (o[m] <= stop)
        px = o[m] if beyond else stop
        return px, e, px * (1 - s * slip), 'stop', h[m] == lo[m]
    if it is not None:
        m = k + it
        beyond = (o[m] <= target) if s < 0 else (o[m] >= target)
        px = o[m] if beyond else target
        return px, e, px, 'target', h[m] == lo[m]
    if end < len(t):
        return o[end], e, o[end] * (1 - s * slip), 'time', h[end] == lo[end]
    return c[-1], e, c[-1] * (1 - s * slip), 'time', h[-1] == lo[-1]


def work(job):
    """The trades and races of days `start:stop` (offsets into ARR) for `variants`
    [(x, entry, direction, stop, slip)] or every variant at every slippage."""
    start, stop_, variants = job
    t_all, o_all, h_all, l_all, c_all = (ARR[k] for k in ('t', 'o', 'h', 'l', 'c'))
    offsets, meta = ARR['offsets'], ARR['meta']
    trades = {k: [] for k in ('x', 'entry', 'dir', 'stop', 'slip', 'day', 'isin', 'nifty200',
                              'med20', 'move', 'side', 'entry_min', 'pnl', 'net_pct',
                              'gross_pct', 'why', 'locked')}
    races = {k: [] for k in ('x', 'entry', 'day', 'move', 'outcome')}
    want = None if variants is None else set(variants)
    for d in range(start, stop_):
        a, b = offsets[d], offsets[d + 1]
        t, o, h, lo, c = t_all[a:b], o_all[a:b], h_all[a:b], l_all[a:b], c_all[a:b]
        day, isin, n200, med20 = meta[d]
        flat = (np.abs(c - o) <= FLAT * o) & (h > lo) & (t <= LAST)
        for x, (i, move) in triggers(t, h, lo, c).items():
            ks = {'N': i + 1}
            if flat[i:].any():
                ks['F'] = i + int(np.argmax(flat[i:])) + 1
            for entry, k in ks.items():
                if k >= len(t) or t[k] >= EXIT:
                    continue
                if want is None:
                    races['x'].append(x)
                    races['entry'].append(entry)
                    races['day'].append(day)
                    races['move'].append(move)
                    races['outcome'].append(_race(t, o, h, lo, k, move))
                for direction in DIRECTIONS:
                    s = -move if direction == 'fade' else move
                    for stop in STOPS:
                        for slip in (SLIP, *SLIPS):
                            if want is not None and (x, entry, direction, stop, slip) not in want:
                                continue
                            if want is None and slip != SLIP:
                                continue
                            raw_x, e, fx, why, locked = simulate(t, o, h, lo, c, k, s, stop,
                                                                 slip)
                            qty = int(NOTIONAL // e)
                            if qty < 1:
                                continue
                            cost = float(charges(qty, Decimal(str(round(e, 2))),
                                                 Decimal(str(round(fx, 2))),
                                                 'buy' if s == 1 else 'sell', RATES))
                            pnl = s * (fx - e) * qty - cost
                            for key, v in zip(trades, (
                                    x, entry, direction, stop, slip, day, isin, n200, med20,
                                    move, s, int(t[k]), pnl, pnl / (e * qty) * 100,
                                    s * (raw_x / o[k] - 1) * 100, why, bool(locked)),
                                    strict=True):
                                trades[key].append(v)
    return trades, races


def _race(t, o, h, lo, k, move):
    """Before costs, from the raw entry: 3% against the move first, 3% with it first, both
    on one candle, or neither by 15:15."""
    end = max(int(np.searchsorted(t, EXIT, side='left')), k)
    up = h[k:end] >= o[k] * (1 + TARGET)
    down = lo[k:end] <= o[k] * (1 - TARGET)
    iu = int(np.argmax(up)) if up.any() else None
    idn = int(np.argmax(down)) if down.any() else None
    if iu is None and idn is None:
        return 'neither'
    if iu is not None and idn is not None and iu == idn:
        return 'same candle'
    first_up = idn is None or (iu is not None and iu < idn)
    with_move = first_up if move == 1 else not first_up
    return 'with' if with_move else 'against'


def run(variants=None):
    n = len(ARR['meta'])
    step = max(1, n // 64)
    jobs = [(a, min(a + step, n), variants) for a in range(0, n, step)]
    trades, races = [], []
    with Pool(4) as pool:
        for tr, ra in pool.imap_unordered(work, jobs):
            trades.append(pl.DataFrame(tr, schema={
                'x': pl.Float64, 'entry': pl.Utf8, 'dir': pl.Utf8, 'stop': pl.Utf8,
                'slip': pl.Float64, 'day': pl.Date, 'isin': pl.Utf8, 'nifty200': pl.Boolean,
                'med20': pl.Float64, 'move': pl.Int8, 'side': pl.Int8, 'entry_min': pl.Int16,
                'pnl': pl.Float64, 'net_pct': pl.Float64, 'gross_pct': pl.Float64,
                'why': pl.Utf8, 'locked': pl.Boolean}))
            races.append(pl.DataFrame(ra, schema={'x': pl.Float64, 'entry': pl.Utf8,
                                                  'day': pl.Date, 'move': pl.Int8,
                                                  'outcome': pl.Utf8}))
    return pl.concat(trades), pl.concat(races)


def stats(t, sessions):
    """Daily net P&L over every session (zero on a day without a trade), and per trade."""
    daily = dict(t.group_by('day').agg(pl.col('pnl').sum()).iter_rows()) if t.height else {}
    xs = np.array([daily.get(d, 0.0) for d in sessions])
    mean, sd = xs.mean(), xs.std(ddof=1)
    if not t.height:
        return {'trades': 0, 'daily_Rs': 0.0, 't_daily': None}
    wins = t.filter(pl.col('pnl') > 0)['pnl'].sum()
    losses = -t.filter(pl.col('pnl') < 0)['pnl'].sum()
    share = {w: round(100 * (t['why'] == w).sum() / t.height, 1)
             for w in ('target', 'stop', 'time')}
    return {'trades': t.height, 'gross_%': round(t['gross_pct'].mean(), 3),
            'net_%': round(t['net_pct'].mean(), 3), 'Rs_trade': round(t['pnl'].mean()),
            'win_%': round(100 * (t['pnl'] > 0).sum() / t.height, 1),
            'PF': round(wins / losses, 2) if losses else None,
            'target_%': share['target'], 'stop_%': share['stop'], 'time_%': share['time'],
            'daily_Rs': round(mean, 1), 't_daily': round(mean / (sd / math.sqrt(len(xs))), 2)
            if sd else None}


def table(rows, title):
    print(f'\n--- {title} ---')
    with pl.Config(tbl_cols=30, tbl_width_chars=260, tbl_rows=200, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True):
        print(pl.DataFrame(rows))


def pick(t, x, entry, direction, stop, slip=SLIP):
    return t.filter((pl.col('x') == x) & (pl.col('entry') == entry) & (pl.col('dir') == direction)
                    & (pl.col('stop') == stop) & (pl.col('slip') == slip))


def variants_table(t, sessions, slip=SLIP):
    rows = []
    for x in XS:
        for entry in ENTRIES:
            for direction in DIRECTIONS:
                for stop in STOPS:
                    rows.append({'X': f'{x:.0%}', 'entry': entry, 'dir': direction, 'stop': stop,
                                 **stats(pick(t, x, entry, direction, stop, slip), sessions)})
    return rows


def race_table(r):
    rows = []
    for x in XS:
        for entry in ENTRIES:
            s = r.filter((pl.col('x') == x) & (pl.col('entry') == entry))
            if not s.height:
                continue
            share = {o: round(100 * (s['outcome'] == o).sum() / s.height, 1)
                     for o in ('against', 'with', 'neither', 'same candle')}
            rows.append({'X': f'{x:.0%}', 'entry': entry, 'moves': s.height,
                         'rises': int((s['move'] == 1).sum()),
                         '3% against the move first %': share['against'],
                         '3% with the move first %': share['with'],
                         'neither by 15:15 %': share['neither'],
                         'both on one candle %': share['same candle']})
    return rows


def breakdowns(t, sessions, label):
    first5 = t.sort('day', 'entry_min').group_by('day', maintain_order=True).head(5)
    rows = [{'group': name, **stats(sub, sessions)} for name, sub in (
        ('all', t), ('after a rise', t.filter(pl.col('move') == 1)),
        ('after a fall', t.filter(pl.col('move') == -1)),
        ('Rs 2-10 crore a day', t.filter(pl.col('med20') < 1e8)),
        ('Rs 10 crore or more', t.filter(pl.col('med20') >= 1e8)),
        ("today's Nifty 200", t.filter(pl.col('nifty200'))),
        ('first 5 signals a day', first5))]
    table(rows, f'{label}: breakdowns')
    locked = t.filter(pl.col('locked'))
    print(f'exit candle without a price range (a lock; the exit might not have filled): '
          f'{locked.height} trades, net {locked["net_pct"].mean() if locked.height else 0:+.2f}% '
          'each')
    busy = (t.group_by('day').agg(pl.len().alias('trades'), pl.col('pnl').sum().round(0))
            .sort('trades', descending=True).head(8))
    table(busy.rows(named=True), f'{label}: the busiest days')


def main():
    mode, data, bhav = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    period = DEV if mode == 'dev' else CONFIRM
    sessions = sorted(d for d in pl.read_parquet(bhav, columns=['day'])['day'].unique()
                      if period[0] <= d <= period[1])
    days = (pl.read_parquet(data / 'days.parquet')
            .filter(pl.col('day').is_between(*period)).sort('isin', 'day', 'minute'))
    keys = days.select('isin', 'day', 'nifty200', 'med20').unique(maintain_order=True)
    sizes = days.group_by('isin', 'day', maintain_order=True).len()['len'].to_numpy()
    ARR.update(t=days['minute'].to_numpy().astype(np.int32), o=days['open'].to_numpy(),
               h=days['high'].to_numpy(), l=days['low'].to_numpy(), c=days['close'].to_numpy(),
               offsets=np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64),
               meta=[(r[1], r[0], r[2], r[3]) for r in keys.iter_rows()])
    name = 'development 2022-2024' if mode == 'dev' else 'confirmation 2025 - 8 Oct 2026'
    print(f'{name}: {len(sessions)} sessions, {len(ARR["meta"])} candidate stock-days')
    t, r = run()
    table(race_table(r), 'before costs: 3% against or with the move first, from the entry')
    rows = variants_table(t, sessions)
    table(rows, f'the 56 variants, slippage {SLIP:.2%} a side')
    if mode == 'dev':
        best = max(rows, key=lambda r: r['t_daily'] if r['t_daily'] is not None else -1e9)
        x, entry, direction, stop = (float(best['X'].rstrip('%')) / 100, best['entry'],
                                     best['dir'], best['stop'])
        print(f'\nCandidate (highest development t): X {best["X"]}, {entry}, {direction}, '
              f'{stop}: t {best["t_daily"]}; ' + ('meets development t >= 3.0'
                                                  if best['t_daily'] >= 3.0 else
                                                  'development t below 3.0: does not pass'))
    else:
        x, entry, direction, stop = float(sys.argv[4]), sys.argv[5], sys.argv[6], sys.argv[7]
        s = stats(pick(t, x, entry, direction, stop), sessions)
        table([{'X': f'{x:.0%}', 'entry': entry, 'dir': direction, 'stop': stop, **s}],
              'the chosen variant')
        passed = s['daily_Rs'] > 0 and (s['t_daily'] or 0) >= 2.0 and s['trades'] >= 100
        print('Confirmation: ' + ('PASSES' if passed else 'does not pass')
              + f' (mean Rs {s["daily_Rs"]:+,.1f} a day, t {s["t_daily"]}, {s["trades"]} trades)')
    # The best variant at each threshold, and the candidate, at the other slippages.
    best_x = {}
    for row in rows:
        if row['t_daily'] is not None and (row['X'] not in best_x
                                           or row['t_daily'] > best_x[row['X']]['t_daily']):
            best_x[row['X']] = row
    wanted = [(float(b['X'].rstrip('%')) / 100, b['entry'], b['dir'], b['stop'], s)
              for b in best_x.values() for s in SLIPS]
    wanted += [(x, entry, direction, stop, s) for s in SLIPS]
    extra, _ = run(wanted)
    table([{'X': b['X'], 'entry': b['entry'], 'dir': b['dir'], 'stop': b['stop'],
            'slippage': f'{s:.2%}',
            **stats(pick(extra, float(b['X'].rstrip('%')) / 100, b['entry'], b['dir'],
                         b['stop'], s), sessions)}
           for b in best_x.values() for s in SLIPS],
          "each threshold's best variant at 0.05% and 0.30% slippage")
    chosen = pick(t, x, entry, direction, stop)
    breakdowns(chosen, sessions, f'X {x:.0%}, {entry}, {direction}, {stop}')
    for s in SLIPS:
        print(f'at {s:.2%} slippage: ', stats(pick(extra, x, entry, direction, stop, s),
                                               sessions))


if __name__ == '__main__':
    main()
