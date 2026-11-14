"""NSE F&O end-of-day bhavcopies for every trading day, 2012 - 8 Oct 2026 (old format to
5 Jul 2024, UDiFF after). Sequential with a pause, skipping files already saved; this
process takes every n-th day.

Usage: uv run python -I fetch_fo.py OUT_DIR DAYS_JSON WORKER WORKERS
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
UDIFF_FROM = dt.date(2024, 7, 8)
BASE = 'https://nsearchives.nseindia.com'
client = httpx.Client(timeout=60, headers={
    'User-Agent': 'IndiaGrowthScreener research (ananteshwark/TradingHelper)'})


def url(d):
    if d < UDIFF_FROM:
        mon = d.strftime('%b').upper()
        return f'{BASE}/content/historical/DERIVATIVES/{d.year}/{mon}/fo{d:%d}{mon}{d.year}bhav.csv.zip'
    return f'{BASE}/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip'


done = missing = 0
for n, d in enumerate(days):
    if n % workers != worker:
        continue
    path = out / f'{d}.zip'
    if path.exists() or path.with_suffix('.missing').exists():
        continue
    for attempt in range(4):
        try:
            r = client.get(url(d))
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
    time.sleep(0.3)
    if (done + missing) % 100 == 0:
        print(f'{d}: {done} saved, {missing} missing', flush=True)
print(f'finished: {done} saved, {missing} missing', flush=True)
