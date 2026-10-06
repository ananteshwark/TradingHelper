"""Official Upstox V3 REST candles. Token never appears in errors or URLs."""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

import httpx

from igs.intraday.engine import Candle


class FeedError(RuntimeError):
    pass


class Upstox:
    def __init__(self, token, transport=None):
        self.client = httpx.Client(base_url='https://api.upstox.com', timeout=15,
                                  headers={'Authorization': f'Bearer {token}',
                                           'Accept': 'application/json'}, transport=transport)

    def close(self):
        self.client.close()

    def candles(self, key, *, day=None):
        key = quote(key, safe='')
        path = f'/v3/historical-candle/intraday/{key}/minutes/5'
        if day:
            path = (f'/v3/historical-candle/{key}/minutes/5/'
                    f'{day - dt.timedelta(days=1)}/{day - dt.timedelta(days=28)}')
        try:
            response = self.client.get(path)
        except httpx.HTTPError:
            raise FeedError('Upstox connection failed; retry on the next scan') from None
        if response.status_code in (401, 403):
            raise FeedError('Upstox access denied; renew the access token in Intraday settings')
        if response.status_code == 429:
            raise FeedError('Upstox rate limit reached; scan stopped until the next interval')
        if response.status_code != 200:
            raise FeedError(f'Upstox returned HTTP {response.status_code}')
        try:
            data = response.json()
            if data['status'] != 'success':
                raise ValueError
            rows = [Candle(dt.datetime.fromisoformat(row[0]), *map(float, row[1:6]))
                    for row in data['data']['candles']]
            if len({b.start for b in rows}) != len(rows):
                raise ValueError
            return sorted(rows, key=lambda b: b.start)
        except (ValueError, TypeError, KeyError, IndexError):
            raise FeedError('Upstox returned invalid candle data') from None
