"""Spike sizes data (PROTOCOL.md): Upstox public 5-minute candles for the candidate
stock-days, one request per ISIN and month (the first to the last candidate day in it).

Usage: uv run python -I fetch.py DATA_DIR WORKER WORKERS   (DATA_DIR holds candidates.parquet)
"""
import json
import sys
import time
import urllib.parse
from pathlib import Path

import httpx
import polars as pl

out = Path(sys.argv[1])
worker, workers = int(sys.argv[2]), int(sys.argv[3])
cand = pl.read_parquet(out / 'candidates.parquet')
jobs = (cand.with_columns(month=pl.col('day').dt.strftime('%Y-%m'))
        .group_by('isin', 'month').agg(pl.col('day').min().alias('frm'),
                                       pl.col('day').max().alias('to'))
        .sort('isin', 'month'))
(out / 'candles').mkdir(exist_ok=True)
client = httpx.Client(timeout=30, headers={
    'Accept': 'application/json',
    'User-Agent': 'IndiaGrowthScreener research (ananteshwark/TradingHelper)'})
failures = 0
for n, (isin, month, frm, to) in enumerate(jobs.iter_rows()):
    if n % workers != worker:
        continue
    path = out / 'candles' / f'{isin}_{month}.json'
    if path.exists():
        continue
    url = ('https://api.upstox.com/v3/historical-candle/'
           + urllib.parse.quote(f'NSE_EQ|{isin}', safe='') + f'/minutes/5/{to}/{frm}')
    for attempt in range(4):
        try:
            resp = client.get(url)
            if resp.status_code == 400:          # not known to Upstox
                path.write_text('[]')
                break
            resp.raise_for_status()
            path.write_text(json.dumps(resp.json()['data']['candles']))
            break
        except Exception as exc:  # noqa: BLE001
            print('retry', isin, month, type(exc).__name__, flush=True)
            time.sleep(5 * (attempt + 1))
    else:
        failures += 1
        print('failed', isin, month, flush=True)
    if n % 500 < workers:
        print(n, 'of', jobs.height, flush=True)
    time.sleep(0.34)
print('done; failures', failures, flush=True)
