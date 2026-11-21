"""NSE's daily PR archive: keep only its board-meetings file (Bm*.txt) for each day.

Usage: uv run python -I fetch_bc.py OUT_DIR DAYS_JSON PART PARTS
"""
import datetime as dt
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import httpx

out = Path(sys.argv[1])
days = [dt.date.fromisoformat(d) for d in json.loads(Path(sys.argv[2]).read_text())]
part, parts = int(sys.argv[3]), int(sys.argv[4])
days = days[part::parts]
client = httpx.Client(timeout=60, headers={'User-Agent': 'IndiaGrowthScreener research (ananteshwark/TradingHelper)'})
for n, d in enumerate(days):
    path = out / f'{d}.txt'
    if path.exists() or path.with_suffix('.missing').exists():
        continue
    url = f'https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip'
    for attempt in range(4):
        try:
            r = client.get(url)
        except httpx.HTTPError as e:
            print('retry', d, type(e).__name__, flush=True)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                z = zipfile.ZipFile(io.BytesIO(r.content))
                name = next(x for x in z.namelist() if x.lower().startswith('bm'))
                path.write_bytes(z.read(name))
            except (zipfile.BadZipFile, StopIteration):
                path.with_suffix('.missing').write_text('no Bm file')
        elif r.status_code == 404:
            path.with_suffix('.missing').write_text(url)
        else:
            print('status', r.status_code, d, flush=True)
            time.sleep(10 * (attempt + 1))
            continue
        break
    time.sleep(0.25)
    if n % 200 == 0:
        print(n, d, flush=True)
print('done', flush=True)
