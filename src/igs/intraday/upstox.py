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
        return self._get(path)

    def candles_between(self, key, start, end):
        """Five-minute candles of the sessions from `start` to `end` (dates, at most a
        month apart)."""
        return self._get(f'/v3/historical-candle/{quote(key, safe="")}/minutes/5/{end}/{start}')

    def ltp(self, keys):
        """{instrument key: last traded price} for up to 500 keys (Upstox's market quote
        LTP). Quotes come back keyed by trading symbol; each names its instrument_token."""
        try:
            response = self.client.get('/v3/market-quote/ltp',
                                       params={'instrument_key': ','.join(keys)})
        except httpx.HTTPError:
            raise FeedError('Upstox connection failed') from None
        if response.status_code != 200:
            raise FeedError(f'Upstox returned HTTP {response.status_code}')
        try:
            data = response.json()
            if data['status'] != 'success':
                raise ValueError
            out = {}
            for quote in data['data'].values():
                price = float(quote['last_price'])
                if price > 0:
                    out[quote['instrument_token']] = price
            return out
        except (ValueError, TypeError, KeyError, AttributeError):
            raise FeedError('Upstox returned an invalid quote') from None

    def day_ranges(self, keys):
        """{instrument key: (high, low)} of today's session so far, for up to 500 keys
        (Upstox's market quote OHLC, interval 1d: its `live_ohlc`). Stocks without a trade
        today are left out."""
        try:
            response = self.client.get('/v3/market-quote/ohlc', params={
                'instrument_key': ','.join(keys), 'interval': '1d'})
        except httpx.HTTPError:
            raise FeedError('Upstox connection failed; retry on the next scan') from None
        if response.status_code in (401, 403):
            raise FeedError('Upstox access denied; renew the access token in Settings, Upstox tab')
        if response.status_code == 429:
            raise FeedError('Upstox rate limit reached; scan stopped until the next interval')
        if response.status_code != 200:
            raise FeedError(f'Upstox returned HTTP {response.status_code}')
        try:
            data = response.json()
            if data['status'] != 'success':
                raise ValueError
            out = {}
            for quote in data['data'].values():
                live = quote.get('live_ohlc') or {}
                high, low = float(live.get('high') or 0), float(live.get('low') or 0)
                if 0 < low <= high:
                    out[quote['instrument_token']] = (high, low)
            return out
        except (ValueError, TypeError, KeyError, AttributeError):
            raise FeedError('Upstox returned an invalid quote') from None

    def _get(self, path, params=None):
        try:
            response = self.client.get(path, params=params)
        except httpx.HTTPError:
            raise FeedError('Upstox connection failed; retry on the next scan') from None
        if response.status_code in (401, 403):
            raise FeedError('Upstox access denied; renew the access token in Settings, Upstox tab')
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
