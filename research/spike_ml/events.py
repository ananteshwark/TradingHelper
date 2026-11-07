"""Spike recovery (PROTOCOL.md): the decision points, their features and their outcomes.

For each usable stock-day and each X in XS, the first candle from 09:15 to 14:55 whose move
within 30 minutes reaches X. Features are what was known at its close; outcomes start at
the next candle's open: the maximum recovery against the move before 15:15, and the net
result of fading the move with each target in TARGETS (no stop, out at the 15:15 open), at
each slippage in SLIPS. Writes OUT_DIR/events.parquet.

Usage: uv run python -I events.py DAYS_DIR NIFTY_JSON OUT_DIR
"""
import json
import math
import sys
from decimal import Decimal
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import polars as pl

from igs.intraday.costs import charges, intraday_rates

XS = (0.05, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15)
TARGETS = (0.01, 0.02, 0.03, 0.04, 0.05)
SLIPS = (0.0015, 0.0005, 0.003)
NOTIONAL = 100_000
OPEN, LAST, EXIT = 9 * 60 + 15, 14 * 60 + 55, 15 * 60 + 15
RATES = intraday_rates()
ARR, NIFTY = {}, {}           # shared with the workers (fork)


def nifty(path):
    """{date: (minutes, open, close)} of the Nifty 50's 5-minute candles."""
    days = {}
    for ts, o, _h, _lo, c, *_ in json.loads(Path(path).read_text()):
        days.setdefault(ts[:10], []).append((int(ts[11:13]) * 60 + int(ts[14:16]), o, c))
    out = {}
    for d, rows in days.items():
        rows.sort()
        m, o, c = (np.array(x, dtype=float) for x in zip(*rows, strict=True))
        out[np.datetime64(d, 'D').astype(object)] = (m, o, c)
    return out


def window(t, h, lo, c):
    """Each candle's move within 30 minutes: (rise, fall, index of the window's lowest low,
    index of its highest high)."""
    n = len(t)
    first = np.searchsorted(t, t - 25, side='left')
    idx = np.arange(n)
    low, high = lo.copy(), h.copy()
    ilow, ihigh = idx.copy(), idx.copy()
    for k in range(1, 6):
        j = idx - k
        ok = (j >= first) & (j >= 0)
        jj = np.where(ok, j, idx)
        lower, higher = lo[jj] < low, h[jj] > high
        low, ilow = np.where(lower, lo[jj], low), np.where(lower, jj, ilow)
        high, ihigh = np.where(higher, h[jj], high), np.where(higher, jj, ihigh)
    return c / low - 1, c / high - 1, ilow, ihigh


def trade(t, o, h, lo, c, k, end, s, target, slip):
    """Fade entered at candle k's open on side s: (net rupees, net %, why, exit locked)."""
    e = o[k] * (1 + s * slip)
    tp = e * (1 + s * target)
    seg = slice(k, end)
    hit = (lo[seg] <= tp) if s < 0 else (h[seg] >= tp)
    if hit.any():
        m = k + int(np.argmax(hit))
        beyond = (o[m] <= tp) if s < 0 else (o[m] >= tp)
        x, why, locked = (o[m] if beyond else tp), 'target', h[m] == lo[m]
    elif end < len(t):
        x, why, locked = o[end] * (1 - s * slip), 'time', h[end] == lo[end]
    else:
        x, why, locked = c[-1] * (1 - s * slip), 'time', h[-1] == lo[-1]
    qty = int(NOTIONAL // e)
    if qty < 1:
        return math.nan, math.nan, why, bool(locked)
    cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                         'buy' if s == 1 else 'sell', RATES))
    pnl = s * (x - e) * qty - cost
    return pnl, pnl / (e * qty) * 100, why, bool(locked)


def work(job):
    start, stop = job
    t_all, o_all, h_all, l_all, c_all, v_all = (ARR[k] for k in 'tohlcv')
    rows = []
    for d in range(start, stop):
        a, b = ARR['offsets'][d], ARR['offsets'][d + 1]
        t, o, h, lo, c, v = (x[a:b] for x in (t_all, o_all, h_all, l_all, c_all, v_all))
        day, isin, n200, med20, prevclose, prev_ret = ARR['meta'][d]
        rise, fall, ilow, ihigh = window(t, h, lo, c)
        early = t <= LAST
        end = int(np.searchsorted(t, EXIT, side='left'))
        nf = NIFTY.get(day)
        for x in XS:
            hit = early & ((rise >= x) | (fall <= -x))
            if not hit.any():
                continue
            i = int(np.argmax(hit))
            side = 1 if rise[i] >= x else -1          # 1: a rise, faded by a short sale
            k = i + 1
            if k >= len(t) or t[k] >= EXIT or k >= end:
                continue
            s = -side
            ext = ilow[i] if side == 1 else ihigh[i]
            day_hi, day_lo = h[:i + 1].max(), lo[:i + 1].min()
            rng = h[i] - lo[i]
            prev = [c[i - j] / c[i - j - 1] - 1 if i - j - 1 >= 0 else 0.0 for j in (1, 2, 3)]
            row = {
                'day': day, 'isin': isin, 'nifty200': n200, 'med20': med20, 'x': x,
                'i': i, 'minute': int(t[i]), 'side': side,
                # features
                'f_move': abs(rise[i] if side == 1 else fall[i]) * 100,
                'f_side': side,
                'f_speed': float(t[i] - t[ext]),
                'f_time': float(t[i] - OPEN),
                'f_day_ret': (c[i] / o[0] - 1) * 100,
                'f_from_prev': (c[i] / prevclose - 1) * 100,
                'f_gap': (o[0] / prevclose - 1) * 100,
                'f_range': (day_hi / day_lo - 1) * 100,
                'f_from_extreme': side * (c[i] / (day_hi if side == 1 else day_lo) - 1) * 100,
                'f_bar_range': (h[i] / lo[i] - 1) * 100,
                'f_body': abs(c[i] - o[i]) / rng if rng > 0 else 0.0,
                'f_close_loc': (c[i] - lo[i]) / rng if rng > 0 else 0.5,
                'f_r1': prev[0] * 100, 'f_r2': prev[1] * 100, 'f_r3': prev[2] * 100,
                'f_vol_ratio': v[i] / v[:i].mean() if i > 0 and v[:i].mean() > 0 else 1.0,
                'f_value_so_far': float((v[:i + 1] * c[:i + 1]).sum() / med20),
                'f_log_value': math.log(med20), 'f_log_price': math.log(c[i]),
                'f_prev_ret': (prev_ret or 0.0) * 100,
            }
            if nf is not None and len(nf[0]):
                m, no, nc = nf
                j = int(np.searchsorted(m, t[i], side='right')) - 1
                j30 = int(np.searchsorted(m, t[i] - 30, side='right')) - 1
                row['f_nifty_day'] = (nc[j] / no[0] - 1) * 100 if j >= 0 else 0.0
                row['f_nifty_30'] = ((nc[j] / (nc[j30] if j30 >= 0 else no[0]) - 1) * 100
                                     if j >= 0 else 0.0)
            else:
                row['f_nifty_day'] = row['f_nifty_30'] = math.nan
            # outcomes
            seg = slice(k, end)
            if s < 0:
                j = k + int(np.argmin(lo[seg]))
                row['y_recovery'] = (o[k] - lo[j]) / o[k] * 100
            else:
                j = k + int(np.argmax(h[seg]))
                row['y_recovery'] = (h[j] - o[k]) / o[k] * 100
            row['y_minutes'] = float(t[j] - t[k])
            for target in TARGETS:
                for slip in SLIPS:
                    pnl, net, why, locked = trade(t, o, h, lo, c, k, end, s, target, slip)
                    tag = f'{round(target * 100)}_{round(slip * 10000)}'
                    row[f'pnl_{tag}'], row[f'net_{tag}'] = pnl, net
                    if slip == SLIPS[0]:
                        row[f'why_{round(target * 100)}'] = why
                        row[f'locked_{round(target * 100)}'] = locked
            rows.append(row)
    return rows


def main():
    days_dir, nifty_json, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    NIFTY.update(nifty(nifty_json))
    days = pl.read_parquet(days_dir / 'days.parquet').sort('isin', 'day', 'minute')
    keys = days.select('isin', 'day', 'nifty200', 'med20', 'prevclose', 'prev_ret').unique(
        subset=['isin', 'day'], maintain_order=True)
    sizes = days.group_by('isin', 'day', maintain_order=True).len()['len'].to_numpy()
    ARR.update(t=days['minute'].to_numpy().astype(np.int32), o=days['open'].to_numpy(),
               h=days['high'].to_numpy(), l=days['low'].to_numpy(), c=days['close'].to_numpy(),
               v=days['volume'].to_numpy(),
               offsets=np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64),
               meta=[(r[1], r[0], r[2], r[3], r[4], r[5]) for r in keys.iter_rows()])
    n = len(ARR['meta'])
    step = max(1, n // 200)
    rows = []
    with Pool(4) as pool:
        for part in pool.imap_unordered(work, [(a, min(a + step, n)) for a in range(0, n, step)]):
            rows += part
    ev = pl.DataFrame(rows, infer_schema_length=None).sort('day', 'minute', 'isin', 'x')
    # The market's crowd: earlier 5% decision points that day, in all and on the same side.
    five = ev.filter(pl.col('x') == XS[0]).select('day', 'minute', 'side')
    counts = []
    for (day,), g in ev.group_by(['day'], maintain_order=True):
        f = five.filter(pl.col('day') == day)
        fm, fs = f['minute'].to_numpy(), f['side'].to_numpy()
        for m, s in zip(g['minute'].to_numpy(), g['side'].to_numpy(), strict=True):
            before = fm < m
            counts.append((int(before.sum()), int((before & (fs == s)).sum())))
    ev = ev.with_columns(f_crowd=pl.Series([a for a, _ in counts]),
                         f_crowd_side=pl.Series([b for _, b in counts]))
    out.mkdir(parents=True, exist_ok=True)
    ev.write_parquet(out / 'events.parquet')
    print(f'{n} stock-days; {ev.height} decision points '
          f'({ev.select("isin", "day", "i").unique().height} distinct candles)')


if __name__ == '__main__':
    main()
