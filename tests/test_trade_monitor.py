"""The monitor must fail closed without changing entry size or creating another exit."""
import copy
import datetime as dt
from decimal import Decimal as D

import httpx
import pytest
from test_intraday import seed_stock

from igs.intraday import monitor as m
from igs.intraday.costs import intraday_rates
from igs.intraday.trading import BrokerError
from igs.timeutil import IST

NOW = dt.datetime(2026, 10, 23, 11, 0, tzinfo=IST)
KEY = 'NSE_EQ|INE123456789'


def quote(price=104, when=NOW):
    return {'instrument_token': KEY, 'timestamp': when.isoformat(),
            'last_trade_time': str(int(when.timestamp() * 1000)),
            'depth': {'buy': [{'price': price, 'quantity': 100}],
                      'sell': [{'price': price + .05, 'quantity': 100}]}}


def plan(**changes):
    args = dict(action='buy', entry=D('100'), original_stop=D('98'),
                broker_stop=D('98'), previous_peak=None, previous_stop=None,
                price=D('104'), quantity=100, tick=D('.05'), policy=m.Policy(),
                rates=intraday_rates())
    args.update(changes)
    return m.proposal(**args)


@pytest.mark.lookahead
def test_trailing_is_causal_monotone_and_symmetric():
    first = plan(price=D('102.9'))
    assert first['action'] == 'hold' and first['stop'] == 98
    peak = plan()
    assert peak['stop'] == 102 and peak['action'] == 'tighten'
    retreat = plan(price=D('103'), previous_peak=peak['peak'], previous_stop=peak['stop'])
    assert retreat['stop'] == 102
    crossed = plan(price=D('101.9'), previous_peak=104, previous_stop=102)
    assert crossed['action'] == 'stop_crossed'
    short = plan(action='sell', original_stop=102, broker_stop=102, price=96)
    assert short['peak'] == 96 and short['stop'] == 98


def test_never_widens_broker_stop_and_no_chasing_beyond_price():
    assert plan(broker_stop=103)['stop'] == 103
    p = plan(price=D('100.1'), previous_peak=104)
    assert p['stop'] == 98   # peak unseen in an outage cannot justify a crossed new stop


@pytest.mark.parametrize('changes', [{'entry': 'NaN'}, {'price': 'Infinity'},
    {'tick': 0}, {'original_stop': 101}, {'action': 'other'}, {'quantity': 0}])
def test_invalid_risk_and_prices(changes):
    with pytest.raises(ValueError):
        plan(**changes)


@pytest.mark.lookahead
@pytest.mark.parametrize('seconds', [-1, 16, 86400])
def test_stale_or_future_quotes(seconds):
    with pytest.raises(ValueError):
        m.quote_price(quote(when=NOW-dt.timedelta(seconds=seconds)), KEY, 'buy', NOW, m.Policy())


def test_quote_uses_bid_for_long_ask_for_short():
    assert m.quote_price(quote(), KEY, 'buy', NOW, m.Policy()) == 104
    assert m.quote_price(quote(), KEY, 'sell', NOW, m.Policy()) == D('104.05')
    bad = quote()
    bad['depth']['sell'][0]['price'] = 110
    with pytest.raises(ValueError, match='Spread'):
        m.quote_price(bad, KEY, 'buy', NOW, m.Policy())
    bad = quote()
    bad['depth']['buy'][0]['quantity'] = 0
    with pytest.raises(ValueError):
        m.quote_price(bad, KEY, 'buy', NOW, m.Policy())


class FakeBroker:
    def __init__(self):
        self.position = {'instrument_token': KEY, 'product': 'I', 'quantity': 100,
                         'overnight_quantity': 0, 'day_buy_quantity': 100,
                         'day_sell_quantity': 0}
        self.detail = {'gtt_order_id': 'GTT-9', 'instrument_token': KEY, 'product': 'I',
                       'type': 'MULTIPLE', 'quantity': 100, 'rules': [
            {'strategy': 'ENTRY', 'status': 'COMPLETED', 'order_id': 'E1',
             'trigger_type': 'IMMEDIATE', 'trigger_price': 100, 'transaction_type': 'BUY'},
            {'strategy': 'STOPLOSS', 'status': 'SCHEDULED',
             'trigger_type': 'IMMEDIATE', 'trigger_price': 98, 'transaction_type': 'SELL'}]}
        self.quote = quote()
        self.modified = []
        self.failure = None
        self.fill_quantity = 100

    def positions(self):
        return [self.position]

    def quotes(self, _):
        return {KEY: self.quote}

    def details(self, _):
        return copy.deepcopy(self.detail)

    def fill(self, _):
        return D(100), self.fill_quantity

    def modify_stop(self, payload):
        self.modified.append(payload)
        if self.failure:
            raise self.failure
        self.detail['rules'][1]['trigger_price'] = payload['rules'][1]['trigger_price']


def seed(conn):
    cid = seed_stock(conn)
    tid = conn.execute('''insert into intraday_trade(company_id,trading_day,action,
        symbol,instrument_key,approved_by,approved_at,expires_at,quantity,entry_price,
        stop_price,notional,status,gtt_order_id,kind)
        values(%s,%s,'buy','TEST',%s,'admin',%s,%s,100,100,98,10000,'entry_filled','GTT-9','ml')
        returning trade_id''', (cid, NOW.date(), KEY, NOW-dt.timedelta(hours=1), NOW)).fetchone()[0]
    conn.execute('update intraday_trading_settings set enabled=true')
    conn.commit()
    return tid


def cycle(conn, broker, **kwargs):
    return m.step(conn, broker, clock=lambda: NOW, ticks={KEY: D('.05')}, **kwargs)


@pytest.mark.db
def test_observe_persists_and_survives_restart_without_modifying(db_conn):
    seed(db_conn)
    broker = FakeBroker()
    assert cycle(db_conn, broker)['monitored'] == 1
    broker.quote = quote(103)
    cycle(db_conn, broker)
    assert not broker.modified
    assert db_conn.execute('select peak,observed_stop from trade_monitor_state'
                           ).fetchone() == (104, 102)
    broker.quote = quote(101)
    cycle(db_conn, broker)
    assert db_conn.execute('select status from trade_monitor_event order by event_id desc '
                          'limit 1').fetchone()[0] == 'stop_crossed'


@pytest.mark.db
def test_execute_only_modifies_existing_stop_and_confirms(db_conn, monkeypatch):
    seed(db_conn)
    monkeypatch.setattr(m, 'require_gate', lambda: None)
    broker = FakeBroker()
    cycle(db_conn, broker, execute=True)
    assert len(broker.modified) == 1
    assert broker.modified[0]['quantity'] == 100
    assert broker.modified[0]['gtt_order_id'] == 'GTT-9'
    assert broker.modified[0]['rules'][0]['trigger_price'] == 100
    assert broker.modified[0]['rules'][1]['trigger_price'] == 102
    assert db_conn.execute("select status from trade_monitor_event order by event_id desc "
                          "limit 1").fetchone()[0] == 'confirmed'
    cycle(db_conn, broker, execute=True)
    assert len(broker.modified) == 1


@pytest.mark.db
@pytest.mark.parametrize('definitive', [False, True])
def test_unknown_or_rejected_modification_is_not_retried(db_conn, monkeypatch, definitive):
    seed(db_conn)
    monkeypatch.setattr(m, 'require_gate', lambda: None)
    broker = FakeBroker()
    broker.failure = BrokerError('test failure', definitive=definitive)
    cycle(db_conn, broker, execute=True)
    cycle(db_conn, broker, execute=True)
    assert len(broker.modified) == 1
    statuses = [r[0] for r in db_conn.execute('select status from trade_monitor_event')]
    assert ('rejected' if definitive else 'uncertain') in statuses


@pytest.mark.db
@pytest.mark.parametrize('fault', ['partial', 'manual', 'stale', 'exit_triggered', 'entry_open'])
def test_unsafe_broker_states_withheld(db_conn, monkeypatch, fault):
    seed(db_conn)
    monkeypatch.setattr(m, 'require_gate', lambda: None)
    broker = FakeBroker()
    if fault == 'partial':
        broker.fill_quantity = 50
    if fault == 'manual':
        broker.position['day_buy_quantity'] = 150
        broker.position['day_sell_quantity'] = 50
    if fault == 'stale':
        broker.quote = quote(when=NOW-dt.timedelta(minutes=1))
    if fault == 'exit_triggered':
        broker.detail['rules'][1]['status'] = 'TRIGGERED'
    if fault == 'entry_open':
        broker.detail['rules'][0]['status'] = 'OPEN'
    cycle(db_conn, broker, execute=True)
    assert not broker.modified
    assert db_conn.execute('select status from trade_monitor_event').fetchone()[0] == 'blocked'


@pytest.mark.db
def test_live_switch_and_squareoff_window(db_conn, monkeypatch):
    seed(db_conn)
    monkeypatch.setattr(m, 'require_gate', lambda: None)
    broker = FakeBroker()
    db_conn.execute('update intraday_trading_settings set enabled=false')
    db_conn.commit()
    with pytest.raises(ValueError, match='disabled'):
        cycle(db_conn, broker, execute=True)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    late = NOW.replace(hour=14, minute=59)
    broker.quote = quote(when=late)
    m.step(db_conn, broker, execute=True, clock=lambda: late, ticks={KEY: D('.05')})
    assert not broker.modified


def test_stop_payload_preserves_target_and_rejects_native_trailing():
    broker = FakeBroker()
    trade = {'quantity': 100, 'gtt_order_id': 'GTT-9', 'instrument_key': KEY, 'action': 'buy'}
    broker.detail['rules'].append({'strategy': 'TARGET', 'status': 'SCHEDULED',
        'trigger_type': 'IMMEDIATE', 'trigger_price': 110, 'transaction_type': 'SELL'})
    payload = m.stop_payload(trade, broker.detail, 102)
    assert payload['rules'][2]['trigger_price'] == 110
    assert broker.detail['rules'][1]['trigger_price'] == 98
    broker.detail['rules'][1]['trailing_gap'] = 1
    with pytest.raises(ValueError, match='trailing'):
        m.stop_payload(trade, broker.detail, 102)


def test_monitor_transport_identity_and_ambiguous_acknowledgement():
    def handler(request):
        if request.method == 'PUT':
            return httpx.Response(200, json={'status': 'success', 'data': {'gtt_order_ids': []}})
        return httpx.Response(200, json={'status': 'success', 'data': {'TEST': quote()}})
    broker = m.MonitorBroker('fake-token', transport=httpx.MockTransport(handler))
    try:
        assert broker.quotes([KEY])[KEY]['instrument_token'] == KEY
        with pytest.raises(BrokerError):
            broker.quotes(['other'])
        with pytest.raises(BrokerError, match='ambiguous'):
            broker.modify_stop({'gtt_order_id': 'GTT-9'})
    finally:
        broker.close()


def test_outside_session_makes_no_database_or_broker_calls():
    assert m.step(None, None, clock=lambda: NOW.replace(hour=16))['status'] == 'outside_session'


def test_thin_stock_with_live_book_but_older_last_trade_is_usable():
    q = quote()
    q['last_trade_time'] = str(int((NOW - dt.timedelta(seconds=90)).timestamp() * 1000))
    assert m.quote_price(q, KEY, 'buy', NOW, m.Policy()) == 104
    q['last_trade_time'] = str(int((NOW - dt.timedelta(seconds=121)).timestamp() * 1000))
    with pytest.raises(ValueError, match='Stale'):
        m.quote_price(q, KEY, 'buy', NOW, m.Policy())
    with pytest.raises(ValueError):
        m.Policy(max_trade_age_seconds=10)


@pytest.mark.db
def test_observe_stop_crossed_says_hypothetical(db_conn):
    seed(db_conn)
    broker = FakeBroker()
    cycle(db_conn, broker)
    broker.quote = quote(101)
    cycle(db_conn, broker)
    reason = db_conn.execute('select detail->>%s from trade_monitor_event order by event_id '
                             'desc limit 1', ('reason',)).fetchone()[0]
    assert reason.startswith('Hypothetical')


@pytest.mark.db
def test_end_of_day_cleanup_keeps_last_and_modification_records(db_conn):
    tid = seed(db_conn)
    broker = FakeBroker()
    for _ in range(3):
        cycle(db_conn, broker)
    m.event(db_conn, tid, NOW, 'execute', 'confirmed', {})
    m.event(db_conn, tid, NOW, 'observe', 'hold', {})
    assert m.cleanup(db_conn, NOW.date()) == 3
    assert [r[0] for r in db_conn.execute('select status from trade_monitor_event '
                                          'order by event_id')] == ['confirmed', 'hold']
    assert m.cleanup(db_conn, NOW.date()) == 0
