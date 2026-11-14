"""Atomically extend downloaded Upstox five-minute files after the 2026-10-08 cohort.

Usage: python extend.py CANDLES_DIR FROM_DATE THROUGH_DATE WORKER WORKERS
Run only after research/ml/fetch_candles.py has completed. A failed stock keeps its
old file unchanged and can be retried by rerunning this command.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.parse
from pathlib import Path

import httpx


def months(start: dt.date, end: dt.date):
    first = start
    while first <= end:
        next_month = (first.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        last = min(end, next_month - dt.timedelta(days=1))
        yield first, last
        first = last + dt.timedelta(days=1)


def extend(directory: Path, start: dt.date, end: dt.date, worker: int, workers: int) -> int:
    universe = json.loads((directory / 'universe.json').read_text())
    keys = ['NSE_INDEX|Nifty 50', *(row['instrument_key'] for row in universe)]
    client = httpx.Client(timeout=30, headers={'Accept': 'application/json'})
    failures = 0
    for index, key in enumerate(keys):
        if index % workers != worker:
            continue
        path = directory / (key.replace('|', '_').replace(' ', '_') + '.json')
        if not path.is_file():
            failures += 1
            print('missing initial file', key, flush=True)
            continue
        additions = []
        for first, last in months(start, end):
            url = ('https://api.upstox.com/v3/historical-candle/'
                   + urllib.parse.quote(key, safe='')
                   + f'/minutes/5/{last}/{first}')
            for attempt in range(4):
                try:
                    response = client.get(url)
                    if response.status_code == 400:  # stock not yet listed in the interval
                        break
                    response.raise_for_status()
                    additions.extend(response.json()['data']['candles'])
                    break
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    print('retry', key, first, type(exc).__name__, flush=True)
                    time.sleep(5 * (attempt + 1))
            else:
                failures += 1
                print('failed', key, first, flush=True)
                additions = None
                break
            time.sleep(0.34)
        if additions is None:
            continue
        original = json.loads(path.read_text())
        combined = {row[0]: row for row in original}
        combined.update({row[0]: row for row in additions})
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(list(combined.values())))
        temporary.replace(path)
        print(index, key, len(additions), 'new bars', flush=True)
    client.close()
    print('failures', failures, flush=True)
    return failures


if __name__ == '__main__':
    raise SystemExit(1 if extend(Path(sys.argv[1]), dt.date.fromisoformat(sys.argv[2]),
                                 dt.date.fromisoformat(sys.argv[3]), int(sys.argv[4]),
                                 int(sys.argv[5])) else 0)
