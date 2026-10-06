import datetime as dt
import gzip
import json

import httpx
import pytest

from igs.intraday.eligibility import MIS_URL, allowed_instruments
from igs.intraday.upstox import FeedError
from igs.timeutil import IST

NOW = dt.datetime(2026, 10, 6, 10, 0, tzinfo=IST)


def row(key):
    return {'instrument_key': key, 'exchange_token': key,
            'segment': 'NSE_EQ', 'instrument_type': 'EQ',
            'security_type': 'NORMAL'}


def client_for(mis, suspended, seen):
    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=gzip.compress(json.dumps(
            mis if str(request.url) == MIS_URL else suspended).encode()))
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_mis_allowlist_excludes_suspended_and_reuses_only_fresh_cache(tmp_path):
    path = tmp_path / 'eligibility.json'
    seen = []
    with client_for([row('NSE_EQ|A'), row('NSE_EQ|B')], [row('NSE_EQ|B')], seen) as client:
        assert allowed_instruments(now=NOW, client=client, cache_path=path) == {'NSE_EQ|A'}
        assert allowed_instruments(now=NOW+dt.timedelta(minutes=4), client=client,
                                   cache_path=path) == {'NSE_EQ|A'}
        assert len(seen) == 2
        allowed_instruments(now=NOW+dt.timedelta(minutes=5), client=client, cache_path=path)
        assert len(seen) == 4
        allowed_instruments(now=NOW+dt.timedelta(minutes=5), force=True,
                            client=client, cache_path=path)
        assert len(seen) == 6


def test_failed_refresh_never_uses_yesterdays_or_stale_allowlist(tmp_path):
    path = tmp_path / 'eligibility.json'
    path.write_text(json.dumps({'source': MIS_URL, 'checked_at': NOW.isoformat(),
                                'keys': ['NSE_EQ|A']}))
    with httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(503))) as client:
        with pytest.raises(FeedError, match='calls withheld'):
            allowed_instruments(now=NOW+dt.timedelta(minutes=5), client=client, cache_path=path)
        with pytest.raises(FeedError):
            allowed_instruments(now=NOW+dt.timedelta(days=1), client=client, cache_path=path)
        with pytest.raises(FeedError):
            allowed_instruments(now=NOW, force=True, client=client, cache_path=path)


@pytest.mark.parametrize('mis,suspended', [([], []), ({}, []), ([{}], []),
                                         ([row('NSE_EQ|A')], [{'bad': 'schema'}])])
def test_malformed_lists_fail_closed(tmp_path, mis, suspended):
    with client_for(mis, suspended, []) as client:
        with pytest.raises(FeedError):
            allowed_instruments(now=NOW, client=client, cache_path=tmp_path / 'cache')


def test_suspended_alternate_series_and_old_symbol_do_not_block_active_contract(tmp_path):
    current = row('NSE_EQ|A')
    old = {**current, 'exchange_token': 'old-token', 'trading_symbol': 'OLDNAME'}
    alternate = {**current, 'instrument_type': 'BE'}
    with client_for([current], [old, alternate], []) as client:
        assert allowed_instruments(now=NOW, client=client, cache_path=tmp_path / 'cache') == {
            'NSE_EQ|A'}
