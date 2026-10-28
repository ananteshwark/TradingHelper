"""Spike reversal data (PROTOCOL.md): the stock-days on which a 12% move inside 30 minutes
was possible at all, from NSE's daily bhavcopies.

A move of 12% within the session needs the day's high to be at least 12% above its low,
so only those days are fetched; the filter uses nothing a trade could not have seen except
that necessary condition. Liquidity: the median traded value of the previous 20 sessions
(known before the day) of at least Rs 2 crore. EQ series only: BE stocks are
trade-for-trade, with no intraday trading.

Usage: uv run python -I candidates.py BHAV_PARQUET OUT_DIR
"""
import datetime as dt
import sys
from pathlib import Path

import polars as pl

FIRST, LAST = dt.date(2022, 1, 1), dt.date(2026, 10, 8)
bhav = (pl.read_parquet(sys.argv[1]).filter(pl.col('series') == 'EQ').sort('isin', 'day')
        .with_columns(med20=pl.col('value').shift(1).rolling_median(20, min_samples=20)
                      .over('isin')))
days = bhav.filter(pl.col('day').is_between(FIRST, LAST)
                   & (pl.col('high') >= 1.12 * pl.col('low')) & (pl.col('med20') >= 2e7))
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
days.select('isin', 'symbol', 'day', 'open', 'high', 'low', 'close', 'prevclose', 'value',
            'med20').write_parquet(out / 'candidates.parquet')
print(days.height, 'stock-days,', days['isin'].n_unique(), 'ISINs')
