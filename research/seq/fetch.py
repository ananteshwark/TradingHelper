"""Study C data (PROTOCOL.md): Study B's fetcher (research/ml/fetch_candles.py) with the end
date moved to 24 October 2026. Upstox public 5-minute candles, one request at a time per
worker.

Usage: python fetch.py DATA_DIR WORKER WORKERS   (DATA_DIR holds universe.json)
"""
import datetime as dt
import json
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

END = dt.date(2026, 10, 24)
out = Path(sys.argv[1])
worker, workers = int(sys.argv[2]), int(sys.argv[3])
universe = json.loads((out / 'universe.json').read_text())
months, first = [], dt.date(2021, 12, 1)
while first <= END:
    nxt = (first.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    months.append((min(nxt - dt.timedelta(days=1), END), first))
    first = nxt
keys = ['NSE_INDEX|Nifty 50'] + [u['instrument_key'] for u in universe]
client = httpx.Client(timeout=30, headers={'Accept': 'application/json'})
failures = 0
for n, key in enumerate(keys):
    if n % workers != worker:
        continue
    path = out / (key.replace('|', '_').replace(' ', '_') + '.json')
    if path.exists():
        continue
    candles = []
    for to, frm in months:
        url = ('https://api.upstox.com/v3/historical-candle/' + urllib.parse.quote(key, safe='')
               + f'/minutes/5/{to}/{frm}')
        for attempt in range(4):
            try:
                resp = client.get(url)
                if resp.status_code == 400:      # before the stock listed
                    break
                resp.raise_for_status()
                candles += resp.json()['data']['candles']
                break
            except Exception as exc:  # noqa: BLE001
                print('retry', key, to, type(exc).__name__, flush=True)
                time.sleep(5 * (attempt + 1))
        else:
            failures += 1
            print('failed', key, to, flush=True)
        time.sleep(0.34)
    path.write_text(json.dumps(candles))
    print(n, key, len(candles), flush=True)
print('done; failures', failures, flush=True)
