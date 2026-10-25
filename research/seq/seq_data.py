"""Study C sequences (PROTOCOL.md): one 81 x 12 window per stock and day at 09:45 - the last
full session (75 candles) and today's first six, from candles closed by 09:45 only.

Usage: python seq_data.py DATA_DIR OUT.npz
"""
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SLOTS = [(dt.datetime(2000, 1, 1, 9, 15) + dt.timedelta(minutes=5 * i)).time()
         for i in range(75)]                                  # 09:15 ... 15:25
SLOT = {t: i for i, t in enumerate(SLOTS)}
STEPS, CHANNELS = 81, 12


def load(path):
    """Study B's day filter (first candle 09:15, at least 70 candles); bars by slot."""
    days = defaultdict(list)
    for row in json.loads(path.read_text()):
        if min(row[1:5]) <= 0:
            continue
        start = dt.datetime.fromisoformat(row[0]).astimezone(IST)
        days[start.date()].append((start.time(), *(float(x) for x in row[1:6])))
    out = {}
    for d, bars in days.items():
        bars.sort()
        if bars[0][0] == dt.time(9, 15) and len(bars) >= 70:
            out[d] = {b[0]: b[1:] for b in bars if b[0] in SLOT}
    return out


def full(bars):
    return len(bars) == 75 and all(t in bars for t in SLOTS)


def windows(days, nifty):
    dates = sorted(days)
    vol = {d: np.array([days[d][t][4] if t in days[d] else np.nan for t in SLOTS])
           for d in dates}
    rows = {}
    for n, day in enumerate(dates):
        if n < 22:                        # Study B needs 22 prior sessions; same rows
            continue
        prev = dates[n - 1]
        y, t = days[prev], days[day]
        if not full(y) or any(s not in t for s in SLOTS[:6]):
            continue
        ny, nt = nifty.get(prev), nifty.get(day)
        if ny is None or nt is None or not full(ny) or any(s not in nt for s in SLOTS[:6]):
            continue
        with np.errstate(invalid='ignore', divide='ignore'):
            hist = np.vstack([vol[x] for x in dates[n - 20:n]])
            mean_v = np.nanmean(np.where(hist > 0, hist, np.nan), axis=0)
        c0, n0 = y[SLOTS[-1]][3], ny[SLOTS[-1]][3]
        w = np.zeros((STEPS, CHANNELS), dtype=np.float32)
        steps = [(y, ny, s, 0) for s in SLOTS] + [(t, nt, s, 1) for s in SLOTS[:6]]
        for i, (bars, nbars, s, today) in enumerate(steps):
            o, h, lo, c, v = bars[s]
            w[i, 0:4] = [math.log(x / c0) for x in (o, h, lo, c)]
            m = mean_v[SLOT[s]]
            w[i, 4] = math.log(v / m) if v > 0 and m > 0 and not math.isnan(m) else 0.0
            w[i, 5:9] = [math.log(x / n0) for x in nbars[s][:4]]
            w[i, 10] = SLOT[s] / 74
            w[i, 11] = today
        w[:, 0:4] = np.clip(w[:, 0:4], -0.2, 0.2)
        w[:, 5:9] = np.clip(w[:, 5:9], -0.2, 0.2)
        w[:, 4] = np.clip(w[:, 4], -5, 5)
        rows[day] = w
    return rows


def run_stock(args):
    data, key, symbol = args
    path = data / (key.replace('|', '_').replace(' ', '_') + '.json')
    if not path.exists():
        return symbol, [], None
    nifty = load(data / 'NSE_INDEX_Nifty_50.json')
    rows = windows(load(path), nifty)
    days = sorted(rows)
    return symbol, days, np.stack([rows[d] for d in days]).astype(np.float16) if days else None


if __name__ == '__main__':
    data = Path(sys.argv[1])
    universe = json.loads((data / 'universe.json').read_text())
    jobs = [(data, u['instrument_key'], u['trading_symbol']) for u in universe]
    syms, days, xs = [], [], []
    with ProcessPoolExecutor(8) as pool:
        for symbol, d, x in pool.map(run_stock, jobs, chunksize=2):
            if x is not None:
                syms += [symbol] * len(d)
                days += d
                xs.append(x)
    X = np.concatenate(xs)
    np.savez(sys.argv[2], X=X, symbol=np.array(syms),
             day=np.array([d.isoformat() for d in days]))
    print(X.shape, len(set(syms)), 'stocks', len(set(days)), 'days')
