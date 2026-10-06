"""Current public Upstox MIS eligibility, excluding suspended instruments."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import os
import tempfile
from decimal import Decimal
from pathlib import Path

import httpx

from igs.intraday.upstox import FeedError
from igs.timeutil import IST, require_aware, utc_now

MIS_URL = 'https://assets.upstox.com/market-quote/instruments/exchange/NSE_MIS.json.gz'
SUSPENDED_URL = ('https://assets.upstox.com/market-quote/instruments/exchange/'
                 'suspended-instrument.json.gz')
TTL = dt.timedelta(minutes=5)


def _contracts(content, *, mis):
    """{(instrument_key, series, exchange_token): tick size in paise} for the MIS list
    (normal NSE equities only); the same keys with no tick size for the suspended list."""
    try:
        rows = json.loads(gzip.decompress(content) if content.startswith(b'\x1f\x8b') else content)
        if not isinstance(rows, list) or (mis and not rows):
            raise ValueError
        found = {}
        for row in rows:
            key = row['instrument_key']
            series, token = row['instrument_type'], row['exchange_token']
            if (not isinstance(key, str) or '|' not in key or not isinstance(series, str)
                    or not series or type(token) not in (str, int) or not str(token)):
                raise ValueError
            if not mis:
                # Suspended files also contain obsolete symbols and alternate series
                # sharing an ISIN. Match the current exchange contract, not ISIN alone.
                found[(key, series, str(token))] = None
            elif (row.get('segment') == 'NSE_EQ' and row.get('instrument_type') == 'EQ'
                    and row.get('security_type') == 'NORMAL'):
                # Orders must be priced in whole ticks; Upstox gives the tick in paise.
                tick = row.get('tick_size')
                if type(tick) not in (int, float) or not math.isfinite(tick) or tick <= 0:
                    raise ValueError
                found[(key, series, str(token))] = tick
        if mis and not found:
            raise ValueError
        return found
    except (ValueError, TypeError, KeyError, OSError, EOFError):
        raise FeedError('Upstox intraday eligibility file is invalid; calls withheld') from None


def allowed_instruments(*, now=None, force=False, client=None, cache_path=None):
    """Instrument keys Upstox currently allows for intraday (MIS) trading."""
    return set(_ticks_paise(now=now, force=force, client=client, cache_path=cache_path))


def tick_sizes(*, now=None, force=False, client=None, cache_path=None):
    """{instrument key: tick size in rupees} for the same instruments."""
    return {key: Decimal(str(paise)) / 100 for key, paise in _ticks_paise(
        now=now, force=force, client=client, cache_path=cache_path).items()}


def _ticks_paise(*, now, force, client, cache_path):
    now = require_aware(now or utc_now())
    path = Path(cache_path or os.environ.get('IGS_UPSTOX_ELIGIBILITY_CACHE',
        Path(__file__).resolve().parents[3] / 'data/intraday/eligibility.json'))
    if not force:
        try:
            cached = json.loads(path.read_text())
            checked = require_aware(dt.datetime.fromisoformat(cached['checked_at']))
            ticks = cached['ticks']
            if (cached.get('source') == MIS_URL and isinstance(ticks, dict)
                    and all(isinstance(key, str) and key.startswith('NSE_EQ|')
                            and type(tick) in (int, float) and tick > 0
                            for key, tick in ticks.items())
                    and checked.astimezone(IST).date() == now.astimezone(IST).date()
                    and dt.timedelta() <= now-checked < TTL):
                return ticks
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        mis = client.get(MIS_URL)
        suspended = client.get(SUSPENDED_URL)
        if mis.status_code != 200 or suspended.status_code != 200:
            raise FeedError('Upstox intraday eligibility unavailable; calls withheld')
        allowed = _contracts(mis.content, mis=True)
        blocked = _contracts(suspended.content, mis=False)
        ticks = {contract[0]: tick for contract, tick in allowed.items()
                 if contract not in blocked}
    except httpx.HTTPError:
        raise FeedError('Upstox intraday eligibility download failed; calls withheld') from None
    finally:
        if own:
            client.close()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'source': MIS_URL, 'checked_at': now.isoformat(),
                       'ticks': dict(sorted(ticks.items()))}, stream)
        temporary.replace(path)
    except OSError:
        raise FeedError('Cannot store Upstox eligibility snapshot; calls withheld') from None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return ticks
