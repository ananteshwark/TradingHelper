"""Every page of NSE's Integrated Filing (Financials) listing, 500 rows a page.
Usage: fetch_if.py OUT_DIR"""
import json
import sys
import time
from pathlib import Path

import httpx

out = Path(sys.argv[1])
out.mkdir(exist_ok=True)
URL = ('https://www.nseindia.com/api/integrated-filing-results?&type=Integrated%20Filing-'
       '%20Financials&page={page}&size=500')


def client():
    c = httpx.Client(timeout=90, follow_redirects=True, headers={
        'User-Agent': 'IndiaGrowthScreener research (ananteshwark/TradingHelper)',
        'Accept': 'application/json,text/html', 'Referer': 'https://www.nseindia.com/'})
    c.get('https://www.nseindia.com/')
    time.sleep(3)
    return c


c, page, total = client(), 0, None
while total is None or page * 500 < total + 500:
    path = out / f'{page}.json'
    if not path.exists():
        for attempt in range(5):
            try:
                r = c.get(URL.format(page=page))
                if r.status_code == 200:
                    d = r.json()
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(30 * (attempt + 1))
            c.close()
            c = client()
        else:
            raise SystemExit(f'gave up at page {page}')
        path.write_text(json.dumps(d))
        time.sleep(6)
    d = json.loads(path.read_text())
    total = d['totalCount']
    print(page, total, len(d['data']), flush=True)
    page += 1
print('done', flush=True)
