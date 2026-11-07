"""NSE end-of-day archives for every trading day, 2012 - 8 Oct 2026: the equity bhavcopy
(all stocks, so no survivorship bias; old format to 5 Jul 2024, UDiFF after) and the MTO
delivery file. Sequential with a pause, skipping files already saved.

Usage: uv run python -I fetch_bhav.py OUT_DIR DAYS_JSON
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

out = Path(sys.argv[1])
days = [dt.date.fromisoformat(d) for d in json.loads(Path(sys.argv[2]).read_text())]
UDIFF_FROM = dt.date(2024, 7, 8)
client = httpx.Client(timeout=30, headers={
    'User-Agent': 'IndiaGrowthScreener research (ananteshwark/TradingHelper)'})
BASE = 'https://nsearchives.nseindia.com'


def urls(d):
    if d < UDIFF_FROM:
        mon = d.strftime('%b').upper()
        cm = f'{BASE}/content/historical/EQUITIES/{d.year}/{mon}/cm{d:%d}{mon}{d.year}bhav.csv.zip'
    else:
        cm = f'{BASE}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip'
    mto = f'{BASE}/archives/equities/mto/MTO_{d:%d%m%Y}.DAT'
    return {'cm': (cm, out / 'cm' / f'{d}.zip'), 'mto': (mto, out / 'mto' / f'{d}.dat')}


missing = 0
for n, d in enumerate(days):
    for kind, (url, path) in urls(d).items():
        if path.exists() or path.with_suffix('.missing').exists():
            continue
        for attempt in range(4):
            try:
                r = client.get(url)
            except httpx.HTTPError as e:
                print('retry', d, kind, type(e).__name__, flush=True)
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 200:
                path.write_bytes(r.content)
            elif r.status_code == 404:
                path.with_suffix('.missing').write_text(url)
                missing += 1
            else:
                print('status', r.status_code, d, kind, flush=True)
                time.sleep(10 * (attempt + 1))
                continue
            break
        time.sleep(0.25)
    if n % 100 == 0:
        print(n, d, 'missing so far', missing, flush=True)
print('done; missing', missing, flush=True)
