"""Compare the bhavcopy-adjusted closes with Upstox's adjusted daily candles for the stocks in
both: daily returns that disagree by more than 2% point to an adjustment error.

Usage: uv run python -I check_adjust.py BUILD_DIR UPSTOX_DAILY_DIR
"""
import json
import sys
from pathlib import Path

import polars as pl

base, up = Path(sys.argv[1]), Path(sys.argv[2])
px = pl.read_parquet(base / 'prices.parquet')
bh = pl.read_parquet(base / '../parsed/bhav.parquet') if (base / '../parsed/bhav.parquet').exists() else None
uni = json.loads((up / 'universe.json').read_text())
sym_of = {u['instrument_key'].split('|')[1]: u['trading_symbol'] for u in uni}
bad, total, checked = [], 0, 0
for isin, sym in sym_of.items():
    f = up / f'NSE_EQ_{isin}.json'
    if not f.exists():
        continue
    u = pl.DataFrame([{'day': r[0][:10], 'uclose': r[4]} for r in json.loads(f.read_text())]) \
        .with_columns(pl.col('day').str.to_date()).sort('day')
    mine = px.filter(pl.col('symbol') == sym).select('day', 'aclose').sort('day')
    if mine.is_empty():
        continue
    j = mine.join(u, on='day').sort('day').with_columns(
        (pl.col('aclose') / pl.col('aclose').shift(1) - 1).alias('a'),
        (pl.col('uclose') / pl.col('uclose').shift(1) - 1).alias('b')).drop_nulls()
    d = j.filter((pl.col('a') - pl.col('b')).abs() > 0.02)
    checked += 1
    total += j.height
    if d.height:
        bad.append((sym, d.height, d.select('day', 'a', 'b').head(3).rows()))
print(f'{checked} stocks, {total} days compared, {sum(b[1] for b in bad)} days differ by >2% '
      f'in {len(bad)} stocks')
for b in sorted(bad, key=lambda x: -x[1])[:25]:
    print(b)
