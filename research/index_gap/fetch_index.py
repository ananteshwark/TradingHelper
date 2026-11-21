"""NSE's closing values of the Nifty200 Momentum 30, Nifty 200 and Nifty 500 (and the newer
momentum indices) at each month end, from its daily ind_close_all files on nsearchives.

Usage: uv run --no-project --with polars --with httpx python -I fetch_index.py \
         PRICES OUT_DIR
PRICES (research/promoter's panel) gives the month ends: each month's last session.
"""
import csv
import datetime as dt
import io
import sys
import time
from pathlib import Path

import httpx
import polars as pl

prices, out = sys.argv[1], Path(sys.argv[2])
NAMES = {'Nifty200 Momentum 30', 'Nifty 200', 'Nifty 500', 'Nifty500 Momentum 50',
         'Nifty Midcap150 Momentum 50'}
URL = 'https://nsearchives.nseindia.com/content/indices/ind_close_all_{:%d%m%Y}.csv'
days = pl.scan_parquet(prices).select('day').unique().collect()
ends = (days.with_columns(pl.col('day').dt.strftime('%Y-%m').alias('m'))
        .group_by('m').agg(pl.col('day').max()).sort('m')['day'].to_list())
rows = []
with httpx.Client(timeout=30, headers={
        'User-Agent': 'IndiaGrowthScreener research (ananteshwark/TradingHelper)'}) as client:
    for day in [d for d in ends if d >= dt.date(2020, 1, 1)]:
        resp = client.get(URL.format(day))
        if resp.status_code != 200:
            print(day, resp.status_code)
            continue
        for r in csv.reader(io.StringIO(resp.content.decode('utf-8-sig'))):
            if r and r[0] in NAMES:
                rows.append((r[0], dt.date(*map(int, reversed(r[1].split('-')))),
                             float(r[5])))
        time.sleep(1)
(pl.DataFrame(rows, schema=['index', 'day', 'close'], orient='row').unique()
 .sort('index', 'day').write_parquet(out / 'index_month_ends.parquet'))
