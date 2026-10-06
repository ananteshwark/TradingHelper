"""Current public Upstox MIS eligibility, excluding suspended instruments."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import tempfile
from pathlib import Path

import httpx

from igs.intraday.upstox import FeedError
from igs.timeutil import IST, require_aware, utc_now

MIS_URL = 'https://assets.upstox.com/market-quote/instruments/exchange/NSE_MIS.json.gz'
SUSPENDED_URL = ('https://assets.upstox.com/market-quote/instruments/exchange/'
                 'suspended-instrument.json.gz')
TTL = dt.timedelta(minutes=5)


def _contracts(content, *, mis):
    try:
        rows = json.loads(gzip.decompress(content) if content.startswith(b'\x1f\x8b') else content)
        if not isinstance(rows, list) or (mis and not rows):
            raise ValueError
        keys = set()
        for row in rows:
            key = row['instrument_key']
            series, token = row['instrument_type'], row['exchange_token']
            if (not isinstance(key, str) or '|' not in key or not isinstance(series, str)
                    or not series or type(token) not in (str, int) or not str(token)):
                raise ValueError
            if not mis or (row.get('segment') == 'NSE_EQ' and row.get('instrument_type') == 'EQ'
                           and row.get('security_type') == 'NORMAL'):
                # Suspended files also contain obsolete symbols and alternate series
                # sharing an ISIN. Match the current exchange contract, not ISIN alone.
                keys.add((key, series, str(token)))
        if mis and not keys:
            raise ValueError
        return keys
    except (ValueError, TypeError, KeyError, OSError, EOFError):
        raise FeedError('Upstox intraday eligibility file is invalid; calls withheld') from None


def allowed_instruments(*, now=None, force=False, client=None, cache_path=None):
    now = require_aware(now or utc_now())
    path = Path(cache_path or os.environ.get('IGS_UPSTOX_ELIGIBILITY_CACHE',
        Path(__file__).resolve().parents[3] / 'data/intraday/eligibility.json'))
    if not force:
        try:
            cached = json.loads(path.read_text())
            checked = require_aware(dt.datetime.fromisoformat(cached['checked_at']))
            keys = cached['keys']
            if (cached.get('source') == MIS_URL and isinstance(keys, list)
                    and all(isinstance(key, str) and key.startswith('NSE_EQ|') for key in keys)
                    and checked.astimezone(IST).date() == now.astimezone(IST).date()
                    and dt.timedelta() <= now-checked < TTL):
                return set(keys)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        mis = client.get(MIS_URL)
        suspended = client.get(SUSPENDED_URL)
        if mis.status_code != 200 or suspended.status_code != 200:
            raise FeedError('Upstox intraday eligibility unavailable; calls withheld')
        contracts = (_contracts(mis.content, mis=True)
                     - _contracts(suspended.content, mis=False))
        keys = {contract[0] for contract in contracts}
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
                       'keys': sorted(keys)}, stream)
        temporary.replace(path)
    except OSError:
        raise FeedError('Cannot store Upstox eligibility snapshot; calls withheld') from None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return keys
