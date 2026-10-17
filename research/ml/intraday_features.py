"""Study B features (PROTOCOL.md): one row per stock and day at 09:45, from Upstox 5-minute
candles. Entry at the open of the 09:45 candle, exit at the open of the 15:15 candle.

Usage: uv run python -I intraday_features.py DATA_DIR OUT.parquet
"""
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl

DATA = Path(sys.argv[1])
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
T = dt.time


def load(path):
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
    """Daily open, high, low, close, traded value, last-hour return, first-30-min volume."""
    o, c = bars[0][1], bars[-1][4]
    k = next((i for i, b in enumerate(bars) if b[0] >= T(14, 30)), None)
    last_hour = c / bars[k][1] - 1 if k is not None else None
    return {'o': o, 'h': max(b[2] for b in bars), 'l': min(b[3] for b in bars), 'c': c,
            'value': sum(b[4] * b[5] for b in bars), 'last_hour': last_hour,
            'v30': sum(b[5] for b in bars[:6])}


def first30(bars):
    six = bars[:6]
    if len(six) < 6 or six[-1][0] != T(9, 40) or len(bars) < 7 or bars[6][0] != T(9, 45):
        return None
    o = six[0][1]
    hi, lo, c = max(b[2] for b in six), min(b[3] for b in six), six[-1][4]
    vol = sum(b[5] for b in six)
    vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in six) / vol if vol else c
    return {'o': o, 'hi': hi, 'lo': lo, 'c': c, 'vol': vol, 'vwap': vwap, 'entry': bars[6][1]}


def exit_price(bars):
    return next((b[1] for b in bars if b[0] == T(15, 15)), None)


def features(days, nifty=None):
    """Rows keyed by day. `nifty` given: the stock's rows; None: the index's own rows."""
    dates = sorted(days)
    summ = {d: summary(days[d]) for d in dates}
    rows = {}
    for n, day in enumerate(dates):
        if n < 22:
            continue
        prev = [summ[x] for x in dates[n - 22:n]]          # 22 prior sessions, oldest first
        p, pp = prev[-1], prev[-2]
        f = first30(days[day])
        if f is None:
            continue
        trs = [max(x['h'] - x['l'], abs(x['h'] - y['c']), abs(x['l'] - y['c']))
               for y, x in zip(prev[-15:-1], prev[-14:])]
        atr = sum(trs) / len(trs)
        row = {
            'gap': f['o'] / p['c'] - 1,
            'r30': f['c'] / f['o'] - 1,
            'prev_r1': p['c'] / pp['c'] - 1,
            'r5': p['c'] / prev[-6]['c'] - 1,
        }
        if nifty is None:
            rows[day] = row
            continue
        v30_hist = [x['v30'] for x in prev[-20:]]
        mean_v30 = sum(v30_hist) / len(v30_hist)
        value20 = sum(x['value'] for x in prev[-20:]) / 20
        ex = exit_price(days[day])
        rng = f['hi'] - f['lo']
        row.update({
            'range30_atr': rng / atr if atr else None,
            'rvol30': f['vol'] / mean_v30 if mean_v30 else None,
            'vwap_dist': f['c'] / f['vwap'] - 1,
            'clv30': (f['c'] - f['lo']) / rng if rng else 0.5,
            'prev_clv': (p['c'] - p['l']) / (p['h'] - p['l']) if p['h'] > p['l'] else 0.5,
            'prev_last_hour': p['last_hour'],
            'prev_intraday': p['c'] / p['o'] - 1,
            'r21': p['c'] / prev[0]['c'] - 1,
            'atr_pct': atr / p['c'],
            'dist20h': p['c'] / max(x['h'] for x in prev[-20:]) - 1,
            'dist20l': p['c'] / min(x['l'] for x in prev[-20:]) - 1,
            'log_value20': math.log(value20) if value20 > 0 else None,
            'value20': value20,
            'ovn5': sum(math.log(x['o'] / y['c']) for y, x in zip(prev[-6:-1], prev[-5:])),
            'intra5': sum(math.log(x['c'] / x['o']) for x in prev[-5:]),
            'dow': day.weekday(),
            'entry': f['entry'],
            'exit': ex,
        })
        m = nifty.get(day)
        if m is None or ex is None:
            continue
        row.update({'n_gap': m['gap'], 'n_r30': m['r30'], 'n_prev_r1': m['prev_r1'],
                    'n_r5': m['r5'], 'rel30': row['r30'] - m['r30'],
                    'target': ex / f['entry'] - 1})
        rows[day] = row
    return rows


def run_stock(args):
    key, symbol = args
    path = DATA / (key.replace('|', '_').replace(' ', '_') + '.json')
    if not path.exists():
        return []
    nifty = features(load(DATA / 'NSE_INDEX_Nifty_50.json'))
    out = []
    for day, row in features(load(path), nifty).items():
        out.append({'symbol': symbol, 'day': day, **row})
    return out


if __name__ == '__main__':
    universe = json.loads((DATA / 'universe.json').read_text())
    jobs = [(u['instrument_key'], u['trading_symbol']) for u in universe]
    rows = []
    with ProcessPoolExecutor(4) as pool:
        for part in pool.map(run_stock, jobs, chunksize=2):
            rows.extend(part)
    df = pl.DataFrame(rows, infer_schema_length=None).sort('day', 'symbol')
    df.write_parquet(sys.argv[2])
    print(df.height, 'rows,', df['symbol'].n_unique(), 'stocks,', df['day'].n_unique(), 'days')
    print(df.select(pl.all().null_count()).transpose(include_header=True)
          .filter(pl.col('column_0') > 0))
