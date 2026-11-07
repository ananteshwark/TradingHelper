"""Upstox public 5-minute candles, December 2021 - 8 October 2026, for the same Nifty 200
universe (n200_long/universe.json) and the Nifty 50. One request at a time."""
import datetime as dt, json, sys, time, urllib.parse
from pathlib import Path

import httpx

out = Path(sys.argv[1])
worker, workers = int(sys.argv[2]), int(sys.argv[3])   # this process takes every n-th key
universe = json.loads((out / 'universe.json').read_text())
months, first = [], dt.date(2021, 12, 1)
while first <= dt.date(2026, 10, 8):
    nxt = (first.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    months.append((min(nxt - dt.timedelta(days=1), dt.date(2026, 10, 8)), first))
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
