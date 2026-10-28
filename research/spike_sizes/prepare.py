"""Spike sizes data (PROTOCOL.md): the usable candidate days' 5-minute candles in one table.

A day is used if its candles run from 09:15 to at least 15:15 and its 5-minute high and low
are within 0.5% of the bhavcopy's. Writes DATA_DIR/days.parquet: isin, symbol, day,
nifty200, med20, minute (from midnight), open, high, low, close.

Usage: uv run python -I prepare.py DATA_DIR
"""
import datetime as dt
import json
import sys
from multiprocessing import Pool
from pathlib import Path

import polars as pl

OPEN, EXIT = 9 * 60 + 15, 15 * 60 + 15
DATA = Path(sys.argv[1])
UNIVERSE = json.loads((Path(__file__).resolve().parents[1] / 'ml' / 'universe_nifty200.json')
                      .read_text())
N200 = {u['isin'] for u in UNIVERSE} | {u['trading_symbol'] for u in UNIVERSE}
WANT = {(r['isin'], r['day']): r for r in
        pl.read_parquet(DATA / 'candidates.parquet').iter_rows(named=True)}


def one(path):
    """(rows, candidate days found, days dropped)."""
    isin = path.stem.rsplit('_', 1)[0]
    days = {}
    for ts, o, h, lo, c, *_ in json.loads(path.read_text()):
        days.setdefault(ts[:10], []).append((int(ts[11:13]) * 60 + int(ts[14:16]), o, h, lo, c))
    cols = {k: [] for k in ('isin', 'symbol', 'day', 'nifty200', 'med20', 'minute', 'open',
                            'high', 'low', 'close')}
    found = dropped = 0
    for d, rows in days.items():
        day = dt.date.fromisoformat(d)
        r = WANT.get((isin, day))
        if r is None:
            continue
        found += 1
        rows.sort()
        if (rows[0][0] != OPEN or rows[-1][0] < EXIT
                or max(x[2] for x in rows) > r['high'] * 1.005
                or min(x[3] for x in rows) < r['low'] * 0.995):
            dropped += 1
            continue
        n200 = isin in N200 or r['symbol'] in N200
        for m, o, h, lo, c in rows:
            for k, v in zip(cols, (isin, r['symbol'], day, n200, r['med20'], m, o, h, lo, c),
                            strict=True):
                cols[k].append(v)
    return cols, found, dropped


def main():
    paths = sorted((DATA / 'candles').glob('*.json'))
    parts, found, dropped = [], 0, 0
    with Pool(4) as pool:
        for cols, f, d in pool.imap_unordered(one, paths, chunksize=50):
            found += f
            dropped += d
            if cols['day']:
                parts.append(pl.DataFrame(cols, schema={
                    'isin': pl.Utf8, 'symbol': pl.Utf8, 'day': pl.Date, 'nifty200': pl.Boolean,
                    'med20': pl.Float64, 'minute': pl.Int16, 'open': pl.Float64,
                    'high': pl.Float64, 'low': pl.Float64, 'close': pl.Float64}))
    out = pl.concat(parts).sort('isin', 'day', 'minute')
    out.write_parquet(DATA / 'days.parquet')
    used = out.select('isin', 'day').unique().height
    print(f'candidate stock-days {len(WANT)}; no candles from Upstox {len(WANT) - found}; '
          f'dropped (not a full session, or range off the bhavcopy by > 0.5%) {dropped}; '
          f'used {used}')


if __name__ == '__main__':
    main()
