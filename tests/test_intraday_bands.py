"""NSE price bands and the distance from VWAP in intraday calls."""

import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest
from test_intraday import NOW, sample

from igs.intraday import engine, price_bands
from igs.intraday.engine import Candle, evaluate
from igs.intraday.upstox import FeedError

# Rows as NSE's sec_list.csv had them on 6 Oct 2026 (BOM, quoted remarks, other series).
SEC_LIST = ('﻿Symbol,Series,Security Name,Band,Remarks\n'
            'RELIANCE,EQ,RELIANCE INDUSTRIES LIMITED,No Band,"-"\n'
            'MRF,EQ,MRF LIMITED,20,"-"\n'
            'ACSTECH,BE,A C S TECHNOLOGIES LIMITED,2,"-"\n'
            'AGSTRA,BZ,AGS TRANSACT TECHNOLOGIES LIMITED,2,"GSM STAGE - 0"\n'
            'TEST,EQ,TEST LIMITED,5,"-"\n')


def client(body, status=200, seen=None):
    def handle(request):
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(status, text=body)
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_parse_keeps_eq_bands_and_no_band():
    assert price_bands.parse(SEC_LIST) == {'RELIANCE': None, 'MRF': 20, 'TEST': 5}
    with pytest.raises(ValueError):
        price_bands.parse('Symbol,Series,Security Name,Remarks\nA,EQ,A,"-"\n')
    with pytest.raises(ValueError):
        price_bands.parse(SEC_LIST.replace('No Band', 'Varies'))


def test_bands_download_once_a_day_and_fail_closed(tmp_path):
    path, seen = tmp_path / 'bands.json', []
    with client(SEC_LIST, seen=seen) as c:
        assert price_bands.bands(now=NOW, client=c, cache_path=path)['TEST'] == 5
        assert price_bands.bands(now=NOW + dt.timedelta(hours=3), client=c,
                                 cache_path=path)['TEST'] == 5
        assert len(seen) == 1
        assert json.loads(path.read_text())['day'] == NOW.date().isoformat()
    with client('', status=503) as c, pytest.raises(FeedError, match='calls withheld'):
        price_bands.bands(now=NOW + dt.timedelta(days=1), client=c, cache_path=path)
    with client('Symbol,Band\nA,5\n') as c, pytest.raises(FeedError, match='format changed'):
        price_bands.bands(now=NOW + dt.timedelta(days=1), client=c, cache_path=path)


def test_band_limits_from_the_previous_close_in_whole_ticks():
    bands = {'TEST': 5, 'FNO': None}
    assert price_bands.price_band(bands, 'TEST', Decimal('101.35'), Decimal('0.05')) == {
        'band_pct': 5, 'prev_close': 101.35, 'lower': 96.3, 'upper': 106.4}
    assert price_bands.price_band(bands, 'FNO', None, Decimal('0.05')) == {'band_pct': None}
    assert 'not listed' in price_bands.price_band(bands, 'GONE', 100, Decimal('0.05'))['unknown']
    assert 'Previous close' in price_bands.price_band(bands, 'TEST', None,
                                                       Decimal('0.05'))['unknown']


@pytest.mark.db
def test_previous_close_is_the_last_session_before_today(db_conn):
    db_conn.execute("insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,"
                    "size_bytes,blob_path,origin) values('f','s',now(),%s,1,'p','http')",
                    ('0' * 64,))
    for day, close in (('2026-10-01', 99), ('2026-10-03', 100.5), ('2026-10-05', 101)):
        db_conn.execute("insert into price_eod(exchange,trade_date,isin,symbol,series,close,"
                        "source_fetch_id) values('NSE',%s,'INE1','TEST','EQ',%s,'f')",
                        (day, close))
    assert price_bands.previous_close(db_conn, 'INE1', NOW) == Decimal('100.5')  # NOW: 5 Oct
    assert price_bands.previous_close(db_conn, 'INE1', NOW + dt.timedelta(days=9)) is None


def test_a_target_past_the_band_or_a_stock_at_its_band_gets_no_call():
    bars, history = sample()                                  # buy at 101.35, target 102.25
    tight = {'band_pct': 2, 'lower': 99.0, 'upper': 102.2}
    result = evaluate(bars, history, NOW, bars, tick=Decimal('0.05'), price_band=tight)
    assert result['action'] == 'wait'
    assert result['reason'] == 'Target beyond the upper price band of ₹102.20'
    at_band = {'band_pct': 2, 'lower': 99.0, 'upper': 101.35}
    assert 'upper price band' in evaluate(bars, history, NOW, bars, tick=Decimal('0.05'),
                                          price_band=at_band)['reason']
    wide = {'band_pct': 5, 'lower': 96.3, 'upper': 106.4}
    assert evaluate(bars, history, NOW, bars, tick=Decimal('0.05'),
                    price_band=wide)['action'] == 'buy'
    unknown = {'unknown': 'Previous close not loaded; price band unknown'}
    assert evaluate(bars, history, NOW, bars, price_band=unknown)['reason'] == unknown['unknown']
    down = [Candle(b.start, 200-b.open, 200-b.low, 200-b.high, 200-b.close, b.volume)
            for b in bars]                                    # sell at 98.65, target 97.75
    floor = {'band_pct': 2, 'lower': 97.8, 'upper': 101.0}
    assert evaluate(down, history, NOW, down, price_band=floor)['reason'] == (
        'Target beyond the lower price band of ₹97.80')


def test_a_close_far_from_vwap_is_not_chased(monkeypatch):
    bars, history = sample()       # close 101.35, VWAP 100.87, range 0.30: 1.6 ranges away
    assert evaluate(bars, history, NOW, bars)['action'] == 'buy'
    monkeypatch.setattr(engine, 'MAX_VWAP_DISTANCE_ATR', 1.5)
    result = evaluate(bars, history, NOW, bars)
    assert result['action'] == 'wait' and result['reason'].endswith('from VWAP; extended')
