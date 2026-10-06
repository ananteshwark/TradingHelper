"""NSE daily price bands (circuit limits) for intraday calls.

NSE's security list gives each stock's band as a percentage of the previous close
(2, 5, 10, 20 or 40), or "No Band" for stocks in the derivatives segment, which have
dynamic bands instead. Checked on 6 Oct 2026: 3,562 rows; EQ series bands 20 (most),
5, 10, 2 and No Band (RELIANCE, TCS, IDEA, ...). A target beyond a band cannot fill,
and a stock at its band may have no counterparty.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

import httpx

from igs.ingest.http import _BROWSER_HEADERS
from igs.intraday.engine import to_tick
from igs.intraday.upstox import FeedError
from igs.timeutil import IST, require_aware, utc_now

URL = 'https://nsearchives.nseindia.com/content/equities/sec_list.csv'
REQUIRED = {'Symbol', 'Series', 'Band'}
NO_BAND = 'No Band'
PREV_CLOSE_MAX_AGE = dt.timedelta(days=7)      # covers weekends and exchange holidays


def parse(body):
    """{symbol: band percent, or None for No Band} for the EQ series."""
    reader = csv.DictReader(io.StringIO(body.lstrip('﻿')))
    if not REQUIRED <= set(reader.fieldnames or []):
        raise ValueError('NSE security list schema changed')
    out = {}
    for row in reader:
        if (row['Series'] or '').strip() != 'EQ':
            continue
        symbol, band = (row['Symbol'] or '').strip().upper(), (row['Band'] or '').strip()
        if not symbol:
            raise ValueError('NSE security list row without a symbol')
        if band == NO_BAND:
            out[symbol] = None
        elif band.isdigit() and 0 < int(band) <= 100:
            out[symbol] = int(band)
        else:
            raise ValueError('Unknown NSE price band')
    if not out:
        raise ValueError('NSE security list has no EQ rows')
    return out


def bands(*, now=None, client=None, cache_path=None):
    """Today's bands, downloaded once per IST day. Fails closed: calls are withheld
    rather than made against yesterday's bands."""
    now = require_aware(now or utc_now())
    day = now.astimezone(IST).date().isoformat()
    path = Path(cache_path or os.environ.get('IGS_PRICE_BAND_CACHE',
        Path(__file__).resolve().parents[3] / 'data/intraday/price_bands.json'))
    try:
        cached = json.loads(path.read_text())
        if cached.get('source') == URL and cached.get('day') == day and isinstance(
                cached.get('bands'), dict):
            return cached['bands']
    except (OSError, ValueError, AttributeError):
        pass
    own = client is None
    client = client or httpx.Client(headers=_BROWSER_HEADERS, timeout=15)
    try:
        response = client.get(URL)
        if response.status_code != 200:
            raise FeedError(f'NSE price bands returned HTTP {response.status_code}; '
                            'calls withheld')
        body = response.text
        found = parse(body)
    except httpx.HTTPError:
        raise FeedError('NSE price bands download failed; calls withheld') from None
    except ValueError:
        raise FeedError('NSE price band file format changed; calls withheld') from None
    finally:
        if own:
            client.close()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'source': URL, 'day': day, 'fetched_at': now.isoformat(),
                       'sha256': hashlib.sha256(body.encode()).hexdigest(),
                       'bands': found}, stream)
        temporary.replace(path)
    except OSError:
        raise FeedError('Cannot store NSE price bands; calls withheld') from None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return found


def previous_close(conn, isin, now):
    """The official close of the last session before today, from the loaded bhavcopy."""
    day = require_aware(now).astimezone(IST).date()
    row = conn.execute('''select trade_date,close from price_eod where exchange='NSE'
        and isin=%s and series in ('EQ','BE') and trade_date<%s
        order by trade_date desc,(series='EQ') desc limit 1''', (isin, day)).fetchone()
    if not row or day - row[0] > PREV_CLOSE_MAX_AGE:
        return None
    return row[1]


def price_band(all_bands, symbol, prev_close, tick):
    """The band record an intraday call is checked against (engine.evaluate)."""
    if symbol not in all_bands:
        return {'unknown': 'NSE price band not listed for this stock'}
    pct = all_bands[symbol]
    if pct is None:
        return {'band_pct': None}       # derivatives stock: dynamic bands, not static
    if prev_close is None:
        return {'unknown': 'Previous close not loaded; price band unknown'}
    prev = Decimal(str(prev_close))
    width = prev * Decimal(pct) / 100
    return {'band_pct': pct, 'prev_close': float(prev),
            'lower': float(to_tick(prev - width, tick, -1)),
            'upper': float(to_tick(prev + width, tick, 1))}
