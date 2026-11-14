"""NSE's quarterly-results listing for every company, two weeks at a time (a window that
returns 1,900 rows or more is split, in case of a cap). Usage: fetch_ranges.py OUT_DIR"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

out = Path(sys.argv[1])
UA = 'IndiaGrowthScreener research (ananteshwark/TradingHelper)'
GAP = 5.0


def client():
    c = httpx.Client(timeout=90, follow_redirects=True, headers={
        'User-Agent': UA, 'Accept': 'application/json,text/html',
        'Referer': 'https://www.nseindia.com/'})
    c.get('https://www.nseindia.com/')
    time.sleep(3)
    return c


c = client()


def get(a, b):
    global c
    for attempt in range(5):
        try:
            r = c.get('https://www.nseindia.com/api/corporates-financial-results',
                      params={'index': 'equities', 'period': 'Quarterly',
                              'from_date': f'{a:%d-%m-%Y}', 'to_date': f'{b:%d-%m-%Y}'})
        except httpx.HTTPError as e:
            print('retry', a, type(e).__name__, flush=True)
            time.sleep(15 * (attempt + 1))
            continue
        time.sleep(GAP)
        if r.status_code == 200:
            try:
                rows = r.json()
            except ValueError:
                rows = None
            if rows is not None:
                return rows if isinstance(rows, list) else rows.get('data', [])
        print('status', r.status_code, a, flush=True)
        time.sleep(30 * (attempt + 1))
        c.close()
        c = client()
    raise SystemExit(f'gave up at {a}')


def window(a, b):
    path = out / f'{a}_{b}.json'
    if path.exists():
        return
    rows = get(a, b)
    if len(rows) >= 1900 and b > a:
        mid = a + (b - a) // 2
        window(a, mid)
        window(mid + dt.timedelta(days=1), b)
        return
    path.write_text(json.dumps(rows))
    print(a, b, len(rows), flush=True)


a = dt.date(2011, 1, 1)
while a <= dt.date(2025, 6, 30):
    b = a + dt.timedelta(days=13)
    window(a, b)
    a = b + dt.timedelta(days=1)
print('done', flush=True)
