"""Study D features (PROTOCOL.md): one row per stock and day t, from candles up to the close
of t-1. Day t supplies only the entry (09:15 open) and exit (15:15 open).

Usage: python features.py DATA_DIR OUT.parquet
"""
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
T = dt.time


def load(path):
    """Study B's day filter: first candle 09:15 and at least 70 candles."""
    days = defaultdict(list)
    for row in json.loads(path.read_text()):
        if min(row[1:5]) <= 0:
            continue
        start = dt.datetime.fromisoformat(row[0]).astimezone(IST)
        days[start.date()].append((start.time(), *(float(x) for x in row[1:6])))
    out = {}
    for d, bars in days.items():
        bars.sort()
        if bars[0][0] == T(9, 15) and len(bars) >= 70:
            out[d] = bars
    return out


def summary(bars):
    o, c = bars[0][1], bars[-1][4]
    k60 = next((i for i, b in enumerate(bars) if b[0] >= T(14, 30)), None)
    k30 = next((i for i, b in enumerate(bars) if b[0] >= T(15, 0)), None)
    vol = sum(b[5] for b in bars)
    vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in bars) / vol if vol else c
    return {'o': o, 'h': max(b[2] for b in bars), 'l': min(b[3] for b in bars), 'c': c,
            'value': sum(b[4] * b[5] for b in bars),
            'last_hour': c / bars[k60][1] - 1 if k60 is not None else None,
            'last30': c / bars[k30][1] - 1 if k30 is not None else None,
            'v_last30': sum(b[5] for b in bars[k30:]) if k30 is not None else None,
            'vwap': vwap}


def features(days, nifty=None):
    """Rows keyed by day t. `nifty` None: the index's own rows."""
    dates = sorted(days)
    summ = {d: summary(days[d]) for d in dates}
    rows = {}
    for n, day in enumerate(dates):
        if n < 22:
            continue
        prev = [summ[x] for x in dates[n - 22:n]]          # 22 sessions to t-1, oldest first
        p, pp = prev[-1], prev[-2]
        row = {'prev_r1': p['c'] / pp['c'] - 1, 'prev_last_hour': p['last_hour'],
               'r5': p['c'] / prev[-6]['c'] - 1, 'r21': p['c'] / prev[0]['c'] - 1,
               'last30': p['last30']}
        if nifty is None:
            rows[day] = row
            continue
        bars = days[day]
        entry = bars[0][1]
        exit_ = next((b[1] for b in bars if b[0] == T(15, 15)), None)
        m = nifty.get(day)
        if m is None or exit_ is None:
            continue
        trs = [max(x['h'] - x['l'], abs(x['h'] - y['c']), abs(x['l'] - y['c']))
               for y, x in zip(prev[-15:-1], prev[-14:])]
        atr = sum(trs) / len(trs)
        value20 = sum(x['value'] for x in prev[-20:]) / 20
        v30 = [x['v_last30'] for x in prev[-21:-1] if x['v_last30']]
        mean_v30 = sum(v30) / len(v30) if v30 else None
        row.update({
            'prev_clv': (p['c'] - p['l']) / (p['h'] - p['l']) if p['h'] > p['l'] else 0.5,
            'prev_intraday': p['c'] / p['o'] - 1,
            'atr_pct': atr / p['c'],
            'dist20h': p['c'] / max(x['h'] for x in prev[-20:]) - 1,
            'dist20l': p['c'] / min(x['l'] for x in prev[-20:]) - 1,
            'log_value20': math.log(value20) if value20 > 0 else None,
            'value20': value20,
            'ovn5': sum(math.log(x['o'] / y['c']) for y, x in zip(prev[-6:-1], prev[-5:])),
            'intra5': sum(math.log(x['c'] / x['o']) for x in prev[-5:]),
            'rvol_last30': p['v_last30'] / mean_v30 if mean_v30 and p['v_last30'] else None,
            'close_vwap': p['c'] / p['vwap'] - 1,
            'n_prev_r1': m['prev_r1'], 'n_last_hour': m['prev_last_hour'], 'n_r5': m['r5'],
            'n_r21': m['r21'], 'n_last30': m['last30'],
            'rel1': p['c'] / pp['c'] - 1 - m['prev_r1'],
            'dow': day.weekday(), 'entry': entry, 'exit': exit_,
            'target': exit_ / entry - 1})
        rows[day] = row
    return rows


def run_stock(args):
    data, key, symbol = args
    path = data / (key.replace('|', '_').replace(' ', '_') + '.json')
    if not path.exists():
        return []
    nifty = features(load(data / 'NSE_INDEX_Nifty_50.json'))
    return [{'symbol': symbol, 'day': d, **r} for d, r in features(load(path), nifty).items()]


if __name__ == '__main__':
    data = Path(sys.argv[1])
    universe = json.loads((data / 'universe.json').read_text())
    jobs = [(data, u['instrument_key'], u['trading_symbol']) for u in universe]
    rows = []
    with ProcessPoolExecutor(8) as pool:
        for part in pool.map(run_stock, jobs, chunksize=2):
            rows.extend(part)
    df = pl.DataFrame(rows, infer_schema_length=None).sort('day', 'symbol')
    df.write_parquet(sys.argv[2])
    print(df.height, 'rows,', df['symbol'].n_unique(), 'stocks,', df['day'].n_unique(), 'days')
