"""NSE's daily index closing values (ind_close_all) for every trading day, 2012 - 8 Oct
2026. Sequential with a pause; this process takes every n-th day.

Usage: uv run python -I fetch_idx.py OUT_DIR DAYS_JSON WORKER WORKERS
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

out = Path(sys.argv[1])
days = [dt.date.fromisoformat(d) for d in json.loads(Path(sys.argv[2]).read_text())]
worker, workers = int(sys.argv[3]), int(sys.argv[4])
client = httpx.Client(timeout=60, headers={
    'User-Agent': 'IndiaGrowthScreener research (ananteshwark/TradingHelper)'})
done = missing = 0
for n, d in enumerate(days):
    if n % workers != worker:
        continue
    path = out / f'{d}.csv'
    if path.exists() or path.with_suffix('.missing').exists():
        continue
    for attempt in range(4):
        try:
            r = client.get('https://nsearchives.nseindia.com/content/indices/'
                           f'ind_close_all_{d:%d%m%Y}.csv')
            if r.status_code == 404:
                path.with_suffix('.missing').write_text('404')
                missing += 1
                break
            r.raise_for_status()
            path.write_bytes(r.content)
            done += 1
            break
        except Exception as exc:  # noqa: BLE001
            print('retry', d, type(exc).__name__, flush=True)
            time.sleep(5 * (attempt + 1))
    time.sleep(0.2)
print(f'finished: {done} saved, {missing} missing', flush=True)
