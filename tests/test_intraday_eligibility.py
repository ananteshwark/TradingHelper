import datetime as dt
import gzip
import json
from decimal import Decimal

import httpx
import pytest

from igs.intraday.eligibility import MIS_URL, allowed_instruments, tick_sizes
from igs.intraday.upstox import FeedError
from igs.timeutil import IST

NOW = dt.datetime(2026, 10, 6, 10, 0, tzinfo=IST)


def row(key, tick=5.0):
    return {'instrument_key': key, 'exchange_token': key,
            'segment': 'NSE_EQ', 'instrument_type': 'EQ',
            'security_type': 'NORMAL', 'tick_size': tick}


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


def test_tick_sizes_come_from_the_same_list_in_rupees(tmp_path):
    """Upstox gives tick_size in paise (verified 6 Oct 2026: RELIANCE 10.0, MRF 500.0,
    IDEA 1.0)."""
    path = tmp_path / 'eligibility.json'
    with client_for([row('NSE_EQ|A', 10.0), row('NSE_EQ|B', 500.0), row('NSE_EQ|C', 1.0)],
                    [row('NSE_EQ|C')], []) as client:
        assert tick_sizes(now=NOW, client=client, cache_path=path) == {
            'NSE_EQ|A': Decimal('0.1'), 'NSE_EQ|B': Decimal('5')}
    # The cached snapshot answers both questions until it is five minutes old.
    with httpx.Client(transport=httpx.MockTransport(
            lambda _: pytest.fail('cache not used'))) as client:
        assert allowed_instruments(now=NOW, client=client, cache_path=path) == {
            'NSE_EQ|A', 'NSE_EQ|B'}


@pytest.mark.parametrize('tick', [None, 0, -5.0, 'five', True])
def test_missing_or_invalid_tick_fails_closed(tmp_path, tick):
    bad = row('NSE_EQ|A')
    if tick is None:
        del bad['tick_size']
    else:
        bad['tick_size'] = tick
    with client_for([bad, row('NSE_EQ|B')], [], []) as client:
        with pytest.raises(FeedError, match='calls withheld'):
            tick_sizes(now=NOW, client=client, cache_path=tmp_path / 'cache')


def test_snapshot_without_tick_sizes_is_downloaded_again(tmp_path):
    path = tmp_path / 'eligibility.json'
    path.write_text(json.dumps({'source': MIS_URL, 'checked_at': NOW.isoformat(),
                                'keys': ['NSE_EQ|A']}))           # before tick sizes
    seen = []
    with client_for([row('NSE_EQ|A')], [], seen) as client:
        assert tick_sizes(now=NOW, client=client, cache_path=path) == {
            'NSE_EQ|A': Decimal('0.05')}
    assert len(seen) == 2
