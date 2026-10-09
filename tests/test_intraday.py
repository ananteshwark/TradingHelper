from __future__ import annotations

import datetime as dt
from dataclasses import replace

import httpx
import pytest

from igs.intraday.engine import BAR, Candle, evaluate
from igs.intraday.upstox import FeedError, Upstox
from igs.timeutil import IST

pytestmark = pytest.mark.usefixtures("intraday_eligibility")

NOW = dt.datetime(2026, 10, 5, 10, 0, 20, tzinfo=IST)


def sample():
    start = NOW.replace(hour=9, minute=15, second=0)
    # About Rs 26 crore traded by 09:55 (engine.MIN_TURNOVER is Rs 10 crore), with a
    # 5x same-time volume jump on the last candle.
    bars = [Candle(start+i*BAR, 100+i*.15, 100.2+i*.15, 99.9+i*.15,
                   100.15+i*.15, 200000) for i in range(9)]
    bars[-1] = replace(bars[-1], volume=1000000)
    history = [replace(bars[-1], start=bars[-1].start-dt.timedelta(days=d), volume=200000)
               for d in (3, 4, 5, 6, 7, 10)]
    return bars, history


def test_buy_and_sell_have_directional_risk_levels():
    bars, history = sample()
    buy = evaluate(bars, history, NOW, bars)
    assert buy['action'] == 'buy'
    assert buy['rvol'] == 5
    assert buy['stop'] < buy['reference'] < buy['target']
    down = [Candle(b.start, 200-b.open, 200-b.low, 200-b.high,
                   200-b.close, b.volume) for b in bars]
    sell = evaluate(down, history, NOW, down)
    assert sell['action'] == 'sell'
    assert sell['target'] < sell['reference'] < sell['stop']



def test_no_call_three_percent_or_more_into_the_days_move():
    bars, history = sample()
    last = bars[-1]

    def after(prev_close, candles=bars):
        prev = Candle(last.start - dt.timedelta(days=1), prev_close, prev_close, prev_close,
                      prev_close, 1000)
        return evaluate(candles, [*history, prev], NOW, candles)

    late = after(last.close / 1.031)
    assert late['action'] == 'wait' and 'too late to chase' in late['reason']
    assert late['day_move_pct'] == 3.1
    assert after(last.close / 1.029)['action'] == 'buy'
    # A move the other way is no chase: 3% down from yesterday, now breaking up.
    assert after(last.close / 0.97)['action'] == 'buy'
    down = [Candle(b.start, 200-b.open, 200-b.low, 200-b.high, 200-b.close, b.volume)
            for b in bars]
    assert after((200 - last.close) / 0.969, down)['action'] == 'wait'
    assert after((200 - last.close) / 0.971, down)['action'] == 'sell'


def test_no_call_under_ten_crore_traded_today():
    bars, history = sample()
    thin = [replace(b, volume=b.volume / 10) for b in bars]
    result = evaluate(thin, [replace(b, volume=b.volume / 10) for b in history], NOW, thin)
    assert result['action'] == 'wait' and 'below ₹10 crore traded today' in result['reason']

@pytest.mark.lookahead
def test_future_candles_and_future_news_cannot_change_call():
    bars, history = sample()
    expected = evaluate(bars, history, NOW, bars)
    future = replace(bars[-1], start=NOW.replace(second=0), volume=1e12)
    news = dict(known_at=(NOW+BAR).isoformat(), published_at=NOW.isoformat(), direction=-1)
    assert evaluate([*bars, future], history, NOW, [*bars, future], [news]) == expected
    # Recently observed news with an old publication date cannot drive a new setup.
    news.update(known_at=NOW.isoformat(), published_at=(NOW-dt.timedelta(days=10)).isoformat())
    assert evaluate(bars, history, NOW, bars, [news]) == expected


@pytest.mark.lookahead
def test_no_stale_overnight_incomplete_or_unbaselined_calls():
    bars, history = sample()
    assert evaluate(bars, history, NOW+dt.timedelta(minutes=6), bars)['action'] == 'wait'
    assert evaluate(bars, history, NOW+dt.timedelta(days=1), bars)['action'] == 'wait'
    assert evaluate(bars[1:], history, NOW, bars)['action'] == 'wait'
    assert evaluate(bars, history[:4], NOW, bars)['action'] == 'wait'
    assert evaluate(bars, history, NOW, [])['action'] == 'wait'
    assert evaluate(bars, history, NOW.replace(hour=15, minute=15), bars)['action'] == 'wait'
    assert evaluate(bars, [replace(b, start=b.start+BAR) for b in history], NOW,
                    bars)['action'] == 'wait'


def test_conflicting_news_vetoes_and_supporting_news_is_not_a_probability():
    bars, history = sample()
    news = dict(known_at=NOW.isoformat(), published_at=NOW.isoformat(), direction=-1)
    assert evaluate(bars, history, NOW, bars, [news])['action'] == 'wait'
    news['direction'] = 1
    assert evaluate(bars, history, NOW, bars, [news])['strength'] == 'Supported'
    flat = [replace(b, volume=20000) for b in bars]
    assert evaluate(flat, history, NOW, flat, [news])['action'] == 'wait'


def test_invalid_candles_rejected():
    bars, _ = sample()
    for change in ({'close': float('nan')}, {'volume': -1}, {'low': 1000},
                   {'start': NOW.replace(tzinfo=None)}, {'start': NOW}):
        with pytest.raises(ValueError):
            replace(bars[0], **change)


def test_upstox_contract_and_redacted_errors():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'status': 'success', 'data': {'candles': [
            ['2026-10-05T09:15:00+05:30', 100, 102, 99, 101, 10, 0]]}})

    feed = Upstox('private-token', httpx.MockTransport(handler))
    assert len(feed.candles('NSE_EQ|INE123456789')) == 1
    feed.candles('NSE_EQ|INE123456789', day=NOW.date())
    assert calls[0].headers['authorization'] == 'Bearer private-token'
    assert '/intraday/NSE_EQ%7CINE123456789/minutes/5' in str(calls[0].url)
    assert str(calls[1].url).endswith('/2026-10-04/2026-09-07')
    feed.close()
    for code in (401, 403, 429, 500):
        feed = Upstox('private-token', httpx.MockTransport(
            lambda request, code=code: httpx.Response(code, text='private-token')))
        with pytest.raises(FeedError) as exc:
            feed.candles('NSE_EQ|INE123456789')
        assert 'private-token' not in str(exc.value)
        feed.close()


@pytest.mark.db
def test_scanner_persists_only_complete_reads_and_reuses_history(db_conn, monkeypatch):
    from igs.intraday.scanner import latest, scan

    cid = db_conn.execute("insert into company(name) values('Test') returning company_id"
                          ).fetchone()[0]
    stock = dict(company_id=cid, symbol='TEST', instrument_key='NSE_EQ|INE123456789')
    monkeypatch.setattr('igs.intraday.scanner.candidates', lambda conn, limit, **kwargs: [stock])
    bars, past = sample()

    class Feed:
        historical_calls = 0

        def candles(self, key, *, day=None):
            if day:
                self.historical_calls += 1
                return past
            return bars

    feed = Feed()
    for _ in range(2):
        assert scan(db_conn, feed=feed, clock=lambda: NOW, pause=lambda _: None)['scanned'] == 1
    assert feed.historical_calls == 1
    run, rows = latest(db_conn)
    assert run['status'] == 'complete'
    # At the default Rs 10,000 per trade, the order (limit 1% below the call, stop 1% below
    # that) earns 1.28x what its stop loses after charges, under the 1.5x minimum: not a
    # call, reason kept.
    result = rows[0]['result']
    assert result['action'] == 'wait' and result['unmet']['action'] == 'buy'
    assert 'outside your trading settings' in result['reason']
    db_conn.execute('''update intraday_trading_settings set max_trade_rupees=100000,
        max_risk_rupees=1000''')
    db_conn.commit()
    scan(db_conn, feed=feed, clock=lambda: NOW, pause=lambda _: None)
    result = latest(db_conn)[1][0]['result']
    assert result['action'] == 'buy' and result['order']['quantity'] == 990
    assert result['order']['reward_risk'] >= 1.5 and not result['order']['automatic']

    class BadFeed:
        def candles(self, *args, **kwargs):
            raise FeedError('Upstox connection failed')

    with pytest.raises(FeedError):
        scan(db_conn, feed=BadFeed(), clock=lambda: NOW)
    assert latest(db_conn)[0]['status'] == 'failed'
    assert not latest(db_conn)[1]  # no fallback presenting the old successful run as current


@pytest.mark.db
@pytest.mark.lookahead
def test_investor_context_uses_received_and_publication_dates(db_conn):
    from igs.intraday.context import add_investor_event, evidence

    cid = db_conn.execute("insert into company(name) values('Test') returning company_id"
                          ).fetchone()[0]
    args = dict(company_id=cid, investor='Named Fund', category='FII', side='buy',
                trade_date=NOW.date(), published_at=NOW, source_url='https://nseindia.com/report',
                evidence='Named purchase of 100 shares', now=NOW)
    add_investor_event(db_conn, **args)
    db_conn.execute('update intraday_investor_event set received_at=%s', (NOW+BAR,))
    assert evidence(db_conn, cid, 'TEST', NOW) == []
    assert evidence(db_conn, cid, 'TEST', NOW+BAR)[0]['direction'] == 1
    with pytest.raises(ValueError):
        add_investor_event(db_conn, **{**args, 'published_at': NOW+BAR})


def test_page_validity_expires_and_disallows_weekends():
    from igs.ui.intraday import active

    signal = {'action': 'buy', 'expires_at': (NOW+BAR).isoformat()}
    assert active(signal, NOW)
    assert not active(signal, NOW+BAR)
    assert not active(signal, NOW.replace(day=10))


DEALS = ('Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded\n'
         '05-OCT-2026,TEST,Test,Named Fund,BUY,100\n'
         '05-OCT-2026,TEST,Test,Named Fund,SELL,20\n')


def test_deals_net_sides_and_reject_unknown_format():
    from decimal import Decimal

    from igs.intraday.deals import parse

    assert parse(DEALS, NOW) == [(NOW.date(), 'TEST', 'NAMED FUND', 'buy', Decimal(80))]
    assert parse(DEALS.replace('BUY,100', 'BUY,20'), NOW) == []
    assert parse(DEALS, NOW-dt.timedelta(days=1)) == []
    with pytest.raises(ValueError):
        parse('<html>Access denied</html>', NOW)


EMPTY_DEALS = ('Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,'
               'Trade Price / Wght. Avg. Price\nNO RECORDS,,,,,,\n')


def test_deals_empty_exchange_sentinel_is_not_a_schema_failure():
    from igs.intraday.deals import parse

    assert parse(EMPTY_DEALS, NOW) == []
    for body in (EMPTY_DEALS.replace('NO RECORDS,', 'NO RECORDS,TEST'),
                 EMPTY_DEALS + '05-OCT-2026,TEST,Test,Named Fund,BUY,100,20\n',
                 EMPTY_DEALS.replace('NO RECORDS', 'SERVICE UNAVAILABLE')):
        with pytest.raises(ValueError):
            parse(body, NOW)


@pytest.mark.db
def test_empty_deal_file_is_retained_without_inventing_events(db_conn):
    from igs.intraday.deals import collect

    with httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=EMPTY_DEALS))) as client:
        assert collect(db_conn, client=client, now=NOW) == 0
        assert collect(db_conn, client=client, now=NOW+BAR) == 0
    assert db_conn.execute('select count(*) from intraday_deal_fetch').fetchone()[0] == 2
    assert db_conn.execute('select count(*) from intraday_investor_event').fetchone()[0] == 0


def seed_stock(conn, symbol='TEST', isin='INE123456789'):
    cid = conn.execute("insert into company(name) values('Test') returning company_id"
                       ).fetchone()[0]
    sid = conn.execute('insert into security(company_id) values(%s) returning security_id',
                       (cid,)).fetchone()[0]
    conn.execute("insert into security_listing(security_id,exchange,status) "
                 "values(%s,'NSE','active')", (sid,))
    for kind, value in (('NSE_SYMBOL', symbol), ('ISIN', isin)):
        conn.execute('''insert into security_identifier(security_id,id_type,id_value,
            valid_from,evidence) values(%s,%s,%s,'2020-01-01','test')''', (sid, kind, value))
    conn.commit()
    return cid


@pytest.mark.db
def test_bulk_loader_deduplicates_and_does_not_guess_institution(db_conn):
    from igs.intraday.context import evidence
    from igs.intraday.deals import collect
    from igs.intraday.scanner import candidates

    cid = seed_stock(db_conn)
    assert candidates(db_conn)[0]['company_id'] == cid
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=DEALS)))
    assert collect(db_conn, client=client, now=NOW) == 2  # distinct bulk/block source snapshots
    assert collect(db_conn, client=client, now=NOW+BAR) == 0
    context = evidence(db_conn, cid, 'TEST', NOW)
    assert all(e['direction'] == 0 for e in context)
    assert all('unclassified' in e['kind'] for e in context)
    db_conn.execute("insert into intraday_investor_watch values('NAMED FUND','FII')")
    assert all(e['direction'] == 1 for e in evidence(db_conn, cid, 'TEST', NOW))
    assert db_conn.execute('select count(*) from intraday_deal_fetch').fetchone()[0] == 2
    assert db_conn.execute("select sum((payload->>'rows')::int) from operational_notification "
                           "where payload->>'table'='intraday_investor_event'").fetchone()[0] == 2
    client.close()


@pytest.mark.db
def test_intraday_page_renders_without_token_or_score_run(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest

    seed_stock(db_conn)
    monkeypatch.delenv('UPSTOX_ACCESS_TOKEN', raising=False)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at = AppTest.from_string('''from igs.ui.intraday import page
from igs.db import connect
page(connect(autocommit=True))''').run()
    assert not at.exception
    assert any('No intraday scan yet' in item.value for item in at.info)
    assert any(item.proto.type == item.proto.PASSWORD for item in at.text_input)


@pytest.mark.db
def test_no_token_scan_is_explicitly_unconfigured(db_conn, monkeypatch):
    from igs.intraday.scanner import latest, scan

    monkeypatch.delenv('UPSTOX_ACCESS_TOKEN', raising=False)
    assert scan(db_conn, clock=lambda: NOW)['status'] == 'unconfigured'
    assert latest(db_conn)[0]['finished_at'] is not None


@pytest.mark.db
def test_populated_page_shows_expired_signals_and_source_evidence(db_conn, monkeypatch):
    import os
    from pathlib import Path

    from psycopg.types.json import Jsonb
    from streamlit.testing.v1 import AppTest

    from igs.ui.intraday import _latest

    cid = seed_stock(db_conn)
    monkeypatch.delenv('UPSTOX_ACCESS_TOKEN', raising=False)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    run = db_conn.execute("insert into intraday_scan(status,scanned) values('complete',1) "
                          'returning scan_id').fetchone()[0]
    bars, history = sample()
    result = evaluate(bars, history, NOW, bars)
    result['expires_at'] = '2000-01-01T10:00:00+05:30'
    result['evidence'] = [dict(title='Named purchase', kind='Insider disclosure',
                               published_at=NOW.isoformat(), detail='Trade date shown', url=None)]
    db_conn.execute('''insert into intraday_signal values(%s,%s,'TEST','NSE_EQ|INE123456789',
        %s,%s)''', (run, cid, NOW, Jsonb(result)))
    db_conn.commit()
    _latest.clear()
    app = Path(__file__).resolve().parents[1] / 'src/igs/ui/app.py'
    at = AppTest.from_file(str(app), default_timeout=30).run()
    at.sidebar.radio(key='page').set_value('Intraday calls').run()
    assert not at.exception
    assert any('EXPIRED' in list(d.value['Call']) for d in at.dataframe if 'Call' in d.value)
    assert any('Named purchase' in text.value for text in at.markdown)


@pytest.mark.db
def test_candidates_filter_broker_eligibility_before_limit(db_conn):
    from igs.intraday.scanner import candidates

    first = seed_stock(db_conn)
    second = seed_stock(db_conn, symbol='SECOND', isin='INE987654321')
    db_conn.commit()
    assert candidates(db_conn, 1)[0]['company_id'] == first
    assert candidates(db_conn, 1, allowed_keys={'NSE_EQ|INE987654321'})[0]['company_id'] == second
    assert candidates(db_conn, 1, allowed_keys=set()) == []
