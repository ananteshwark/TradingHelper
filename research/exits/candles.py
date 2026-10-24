"""Upstox 5-minute candles as one table: symbol, day, k (0 = 09:15 ... 74 = 15:25), o, h, l,
c, v. The Nifty 50 is the symbol 'NIFTY'.

Usage: uv run python -I candles.py CANDLE_DIR OUT.parquet
"""
import json
import sys
from pathlib import Path

import polars as pl

src, out = Path(sys.argv[1]), sys.argv[2]
universe = json.loads((src / 'universe.json').read_text())
files = [(u['trading_symbol'], u['instrument_key']) for u in universe] + \
    [('NIFTY', 'NSE_INDEX|Nifty 50')]
parts = []
for sym, key in files:
    rows = json.loads((src / (key.replace('|', '_').replace(' ', '_') + '.json')).read_text())
    d = pl.DataFrame(rows, schema=['ts', 'o', 'h', 'l', 'c', 'v', 'oi'], orient='row')
    d = d.with_columns(pl.col('ts').str.slice(0, 19).str.to_datetime('%Y-%m-%dT%H:%M:%S'))
    d = d.with_columns(pl.lit(sym).alias('symbol'), pl.col('ts').dt.date().alias('day'),
                       ((pl.col('ts').dt.hour().cast(pl.Int32) * 60
                         + pl.col('ts').dt.minute().cast(pl.Int32) - 555) // 5).alias('k'))
    parts.append(d.filter(pl.col('k').is_between(0, 74))
                 .select('symbol', 'day', 'k', 'o', 'h', 'l', 'c', pl.col('v').cast(pl.Float64)))
t = pl.concat(parts).unique(['symbol', 'day', 'k']).sort('symbol', 'day', 'k')
t.write_parquet(out)
print(t.height, 'candles;', t['symbol'].n_unique(), 'symbols;', t['day'].min(), 'to', t['day'].max())
