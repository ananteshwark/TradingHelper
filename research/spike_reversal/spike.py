"""Spike reversal (PROTOCOL.md): after a 12% move inside 30 minutes, trade against it at
the first flat 5-minute candle, with a 4% target; Upstox intraday charges and slippage on
Rs 1 lakh a trade.

Usage:
  uv run python -I spike.py dev DATA_DIR BHAV_PARQUET NIFTY200_DIR
  uv run python -I spike.py confirm DATA_DIR BHAV_PARQUET NIFTY200_DIR FLAT STOP
DATA_DIR: candidates.parquet and candles/ (candidates.py, fetch.py). NIFTY200_DIR: the
5-minute candle files of research/ml/universe_nifty200.json (research/seq/fetch.py), for
the 8% and 10% moves, which need every day.
"""
import datetime as dt
import json
import math
import sys
from decimal import Decimal
from pathlib import Path

import polars as pl

from igs.intraday.costs import charges, intraday_rates

DEV = (dt.date(2022, 1, 1), dt.date(2024, 12, 31))
CONFIRM = (dt.date(2025, 1, 1), dt.date(2026, 10, 8))
MOVE, TARGET, NOTIONAL = 0.12, 0.04, 100_000
FLATS, STOPS = (0.0025, 0.005, 0.01), ('S0', 'S4', 'SX')
SLIP, SLIPS = 0.0015, (0.0005, 0.003)
OPEN, LAST, EXIT = 9 * 60 + 15, 14 * 60 + 55, 15 * 60 + 15
RATES = intraday_rates()


def parse(candles):
    """Upstox candles by day: {day: (minutes, open, high, low, close)}, oldest first."""
    days = {}
    for ts, o, h, lo, c, *_ in candles:
        days.setdefault(ts[:10], []).append((int(ts[11:13]) * 60 + int(ts[14:16]), o, h, lo, c))
    return {dt.date.fromisoformat(d): tuple(map(list, zip(*sorted(rows), strict=True)))
            for d, rows in days.items()}


def usable(day, bars, ref):
    """A full regular session whose range agrees with the bhavcopy's (PROTOCOL.md, Data)."""
    t, _, h, lo, _ = bars
    if t[0] != OPEN or t[-1] < EXIT:
        return False
    if ref is None:
        return True
    return max(h) <= ref[0] * 1.005 and min(lo) >= ref[1] * 0.995


def trigger(bars, move):
    """The first candle (index) closing `move` above the window's lowest low (a spike up:
    side -1, a short) or below its highest high (side +1, a buy), with the window's first
    index. The window: candles starting from 25 minutes before to this one."""
    t, _, h, lo, c = bars
    first = 0
    for i in range(len(t)):
        if t[i] > LAST:
            return None
        while t[first] < t[i] - 25:
            first += 1
        if c[i] >= (1 + move) * min(lo[first:i + 1]):
            return i, -1, first
        if c[i] <= (1 - move) * max(h[first:i + 1]):
            return i, 1, first
    return None


def flat_candle(bars, i, flat):
    t, o, h, lo, c = bars
    for j in range(i, len(t)):
        if t[j] > LAST:
            return None
        if abs(c[j] - o[j]) <= flat * o[j] and h[j] > lo[j]:
            return j
    return None


def simulate(bars, side, k, stop_kind, extreme, slip):
    """The trade entered at candle k's open: (raw entry, raw exit, filled entry, filled
    exit, why, exit candle's range is zero)."""
    t, o, h, lo, c = bars
    raw = o[k]
    e = raw * (1 + side * slip)
    target = e * (1 + side * TARGET)
    stop = {'S0': None, 'S4': e * (1 - side * TARGET),
            'SX': extreme - side * 0.005 * e}[stop_kind]
    for m in range(k, len(t)):
        if t[m] >= EXIT:
            return raw, o[m], e, o[m] * (1 - side * slip), 'time', h[m] == lo[m]
        if stop is not None and (h[m] >= stop if side < 0 else lo[m] <= stop):
            beyond = (o[m] >= stop) if side < 0 else (o[m] <= stop)
            px = o[m] if beyond else stop
            return raw, px, e, px * (1 - side * slip), 'stop', h[m] == lo[m]
        if (lo[m] <= target) if side < 0 else (h[m] >= target):
            beyond = (o[m] <= target) if side < 0 else (o[m] >= target)
            px = o[m] if beyond else target
            return raw, px, e, px, 'target', h[m] == lo[m]
    return raw, c[-1], e, c[-1] * (1 - side * slip), 'time', h[-1] == lo[-1]


def trades(days, flat, stop_kind, slip, move=MOVE):
    """One row a trade over `days`: [(isin, symbol, day, nifty200, bars)]."""
    rows = []
    for isin, symbol, day, n200, bars in days:
        trig = trigger(bars, move)
        if trig is None:
            continue
        i, side, first = trig
        j = flat_candle(bars, i, flat)
        if j is None or j + 1 >= len(bars[0]) or bars[0][j + 1] >= EXIT:
            continue
        k = j + 1
        extreme = (max(bars[2][first:k]) if side < 0 else min(bars[3][first:k]))
        raw_e, raw_x, e, x, why, locked = simulate(bars, side, k, stop_kind, extreme, slip)
        qty = int(NOTIONAL // e)
        if qty < 1:
            continue
        cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                             'buy' if side == 1 else 'sell', RATES))
        pnl = side * (x - e) * qty - cost
        rows.append({'isin': isin, 'symbol': symbol, 'day': day, 'nifty200': n200,
                     'side': side, 'trigger': bars[0][i], 'entry_min': bars[0][k],
                     'pnl': pnl, 'gross_pct': side * (raw_x / raw_e - 1) * 100,
                     'net_pct': pnl / (e * qty) * 100, 'why': why, 'locked': locked})
    return pl.DataFrame(rows, schema={
        'isin': pl.Utf8, 'symbol': pl.Utf8, 'day': pl.Date, 'nifty200': pl.Boolean,
        'side': pl.Int64, 'trigger': pl.Int64, 'entry_min': pl.Int64, 'pnl': pl.Float64,
        'gross_pct': pl.Float64, 'net_pct': pl.Float64, 'why': pl.Utf8, 'locked': pl.Boolean})


def stats(t, sessions):
    """Daily net P&L over every session (zero on a day without a trade), and per trade."""
    daily = dict(t.group_by('day').agg(pl.col('pnl').sum()).iter_rows()) if t.height else {}
    xs = [daily.get(d, 0.0) for d in sessions]
    n = len(xs)
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    wins = t.filter(pl.col('pnl') > 0)['pnl'].sum()
    losses = -t.filter(pl.col('pnl') < 0)['pnl'].sum()

    def share(which):
        return round(100 * (t['why'] == which).sum() / t.height, 1) if t.height else None

    def mean_of(col, digits):
        return round(t[col].mean(), digits) if t.height else None

    return {'trades': t.height, 'days_traded': len(daily),
            'gross_%': mean_of('gross_pct', 3), 'net_%': mean_of('net_pct', 3),
            'Rs_trade': mean_of('pnl', 0),
            'win_%': round(100 * (t['pnl'] > 0).sum() / t.height, 1) if t.height else None,
            'PF': round(wins / losses, 2) if losses else None,
            'target_%': share('target'), 'stop_%': share('stop'), 'time_%': share('time'),
            'daily_Rs': round(mean, 1),
            't_daily': round(mean / (sd / math.sqrt(n)), 2) if sd else None}


def table(rows, title):
    print(f'\n--- {title} ---')
    with pl.Config(tbl_cols=30, tbl_width_chars=250, tbl_rows=200, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True, fmt_str_lengths=40):
        print(pl.DataFrame(rows))


def load(data_dir, n200_isins, n200_symbols):
    """The candidate days with usable candles: [(isin, symbol, day, nifty200, bars)]."""
    cand = pl.read_parquet(data_dir / 'candidates.parquet')
    want = {(r['isin'], r['day']): r for r in cand.iter_rows(named=True)}
    days, missing, bad = [], 0, 0
    got = set()
    for path in sorted((data_dir / 'candles').glob('*.json')):
        isin = path.stem.rsplit('_', 1)[0]
        for day, bars in parse(json.loads(path.read_text())).items():
            r = want.get((isin, day))
            if r is None:
                continue
            got.add((isin, day))
            if not usable(day, bars, (r['high'], r['low'])):
                bad += 1
                continue
            days.append((isin, r['symbol'], day,
                         isin in n200_isins or r['symbol'] in n200_symbols, bars))
    missing = len(want) - len(got)
    print(f'candidate stock-days {len(want)}; no candles from Upstox {missing}; dropped (not '
          f'a full session, or range off the bhavcopy by > 0.5%) {bad}; used {len(days)}')
    return days


def load_n200(n200_dir, universe, bhav):
    """Every day of the Nifty 200 candle files, for the 8% and 10% moves. The bhavcopy check
    by ISIN, else by symbol; liquidity as for the candidates."""
    isins, symbols = {u['isin'] for u in universe}, {u['trading_symbol'] for u in universe}
    ref = {}
    for isin, symbol, day, high, low, med20 in (
            bhav.filter((pl.col('day') >= DEV[0])
                        & (pl.col('isin').is_in(isins) | pl.col('symbol').is_in(symbols)))
            .select('isin', 'symbol', 'day', 'high', 'low', 'med20').iter_rows()):
        ref[(isin, day)] = ref[(symbol, day)] = (high, low, med20)
    days = []
    for u in universe:
        path = n200_dir / (u['instrument_key'].replace('|', '_') + '.json')
        if not path.exists():
            continue
        for day, bars in parse(json.loads(path.read_text())).items():
            if not DEV[0] <= day <= CONFIRM[1]:
                continue
            r = ref.get((u['isin'], day)) or ref.get((u['trading_symbol'], day))
            if r is not None and (r[2] or 0) < 2e7:
                continue
            if usable(day, bars, r[:2] if r else None):
                days.append((u['isin'], u['trading_symbol'], day, True, bars))
    return days


def main():
    mode, data_dir, bhav_path = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    n200_dir = Path(sys.argv[4])
    bhav = (pl.read_parquet(bhav_path).filter(pl.col('series') == 'EQ').sort('isin', 'day')
            .with_columns(med20=pl.col('value').shift(1).rolling_median(20, min_samples=20)
                          .over('isin')))
    universe = json.loads((Path(__file__).resolve().parents[1] / 'ml' / 'universe_nifty200.json')
                          .read_text())
    n200_isins = {u['isin'] for u in universe}
    n200_symbols = {u['trading_symbol'] for u in universe}
    all_sessions = sorted(bhav['day'].unique())
    period = DEV if mode == 'dev' else CONFIRM
    sessions = [d for d in all_sessions if period[0] <= d <= period[1]]
    days = [d for d in load(data_dir, n200_isins, n200_symbols)
            if period[0] <= d[2] <= period[1]]
    name = 'development 2022-2024' if mode == 'dev' else 'confirmation 2025 - 8 Oct 2026'
    print(f'{name}: {len(sessions)} sessions, {len(days)} candidate stock-days')

    if mode == 'dev':
        rows = []
        for flat in FLATS:
            for stop in STOPS:
                rows.append({'flat': f'{flat:.2%}', 'stop': stop,
                             **stats(trades(days, flat, stop, SLIP), sessions)})
        table(rows, f'the nine variants, slippage {SLIP:.2%} a side')
        best = max(rows, key=lambda r: r['t_daily'] if r['t_daily'] is not None else -1e9)
        flat, stop = float(best['flat'].rstrip('%')) / 100, best['stop']
        print(f'\nCandidate (highest development t): flat {best["flat"]}, {stop}, '
              f't {best["t_daily"]}; ' + ('meets development t >= 2.5' if best['t_daily'] >= 2.5
                                          else 'development t below 2.5: does not pass'))
        rows = [{'flat': r['flat'], 'stop': r['stop'], 'slippage': f'{s:.2%}',
                 **stats(trades(days, float(r['flat'].rstrip('%')) / 100, r['stop'], s),
                         sessions)}
                for r in rows for s in SLIPS]
        table(rows, 'slippage 0.05% and 0.30% a side (also reported)')
    else:
        flat, stop = float(sys.argv[5]), sys.argv[6]
        t = trades(days, flat, stop, SLIP)
        s = stats(t, sessions)
        table([{'flat': f'{flat:.2%}', 'stop': stop, **s}], 'the chosen variant')
        passed = (s['daily_Rs'] > 0 and (s['t_daily'] or 0) >= 2.0 and s['trades'] >= 100)
        print('Confirmation: ' + ('PASSES' if passed else 'does not pass')
              + f' (mean Rs {s["daily_Rs"]:+,.1f} a day, t {s["t_daily"]}, {s["trades"]} trades)')

    # Also reported, for the candidate or chosen variant.
    t = trades(days, flat, stop, SLIP)
    first5 = t.sort('day', 'entry_min').group_by('day', maintain_order=True).head(5)
    rows = [{'trades': label, **stats(sub, sessions)} for label, sub in (
        ('all', t), ('buys after a fall', t.filter(pl.col('side') == 1)),
        ('short sells after a rise', t.filter(pl.col('side') == -1)),
        ("today's Nifty 200", t.filter(pl.col('nifty200'))),
        ('outside the Nifty 200', t.filter(~pl.col('nifty200'))),
        ('first 5 signals a day', first5))]
    table(rows, f'flat {flat:.2%}, {stop}: sides, Nifty 200, busy days')
    locked = t.filter(pl.col('locked'))
    print(f'exit candle without a price range (a lock; the exit might not have filled): '
          f'{locked.height} trades, net '
          f'{locked["net_pct"].mean() if locked.height else 0:+.2f}% each')
    top = (t.group_by('day').agg(pl.len().alias('trades'), pl.col('pnl').sum().round(0))
           .sort('trades', descending=True).head(8))
    table(top.rows(named=True), 'the busiest days')

    # The owner's claim: 4% back before 4% further, from the entry, by 15:15.
    claim = trades(days, flat, 'S4', 0.0)
    for side, label in ((0, 'all'), (1, 'after a fall'), (-1, 'after a rise')):
        c = claim if side == 0 else claim.filter(pl.col('side') == side)
        if c.height:
            pct = {w: 100 * (c['why'] == w).sum() / c.height for w in ('target', 'stop', 'time')}
            print(f"Owner's claim, {label}: {c.height} spikes; 4% back first "
                  f"{pct['target']:.1f}%, 4% further first {pct['stop']:.1f}%, "
                  f"neither by 15:15 {pct['time']:.1f}%")

    # 8% and 10% moves, on every Nifty 200 day (descriptive, not tests).
    n200 = [d for d in load_n200(n200_dir, universe, bhav) if period[0] <= d[2] <= period[1]]
    rows = [{'move': f'{m:.0%}', **stats(trades(n200, flat, stop, SLIP, move=m), sessions)}
            for m in (0.08, 0.10, 0.12)]
    table(rows, "today's Nifty 200, every day: the same rule after 8%, 10% and 12% moves")


main()
