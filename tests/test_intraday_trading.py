import datetime as dt
from decimal import Decimal

import httpx
import pytest
from psycopg.types.json import Jsonb
from test_intraday import NOW, seed_stock
from test_intraday_telegram import add_scan, signal

from igs.intraday.telegram_approvals import poll
from igs.intraday.trading import Broker, BrokerError, TradeError, approve, reconcile

pytestmark = pytest.mark.usefixtures("intraday_eligibility", "zero_intraday_charges")


def test_broker_uses_multi_leg_gtt_and_sanitizes_failures():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith('/ltp'):
            return httpx.Response(200, json={'status': 'success', 'data': {
                'NSE_EQ|INE123456789': {'last_price': 101.35}}})
        return httpx.Response(200, json={'status': 'success', 'data': {
            'gtt_order_ids': ['GTT-1']}})

    broker = Broker('secret', transport=httpx.MockTransport(handle))
    assert broker.ltp('NSE_EQ|INE123456789') == Decimal('101.35')
    assert broker.place({'type': 'MULTIPLE'}) == 'GTT-1'
    assert requests[1].url.path == '/v3/order/gtt/place'
    assert requests[1].headers['authorization'] == 'Bearer secret'
    broker.close()
    broker = Broker('secret', transport=httpx.MockTransport(
        lambda _: httpx.Response(500, text='secret')))
    with pytest.raises(BrokerError) as exc:
        broker.place({})
    assert 'secret' not in str(exc.value)
    broker.close()
    broker = Broker('secret', transport=httpx.MockTransport(lambda _: httpx.Response(
        401, json={'status': 'error', 'errors': [{'errorCode': 'UDAPI100067',
        'message': 'The API is not permitted with a read only token.'}]})))
    with pytest.raises(BrokerError) as exc:
        broker.place({})
    assert exc.value.definitive and exc.value.code == 'UDAPI100067'
    assert 'read-only Analytics token' in str(exc.value)
    broker.close()
    broker = Broker('secret', transport=httpx.MockTransport(
        lambda _: httpx.Response(408, json={'status': 'error'})))
    with pytest.raises(BrokerError) as exc:
        broker.place({})
    assert not exc.value.definitive  # timeout responses may follow broker acceptance
    broker.close()


def test_order_fill_reads_the_documented_order_details():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={'status': 'success', 'data': {
            'status': 'complete', 'average_price': 570.95, 'filled_quantity': 1,
            'quantity': 1, 'order_id': '231019025562880'}})

    broker = Broker('secret', transport=httpx.MockTransport(handle))
    assert broker.fill('231019025562880') == (Decimal('570.95'), 1)
    assert seen[0].url.path == '/v2/order/details'
    assert seen[0].url.params['order_id'] == '231019025562880'
    broker.close()
    broker = Broker('secret', transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={'status': 'success', 'data': {'average_price': 'n/a'}})))
    with pytest.raises(BrokerError, match='invalid order fill'):
        broker.fill('1')
    broker.close()

class FakeBroker:
    def __init__(self, price):
        self.price = price
        self.orders = []
        self.cancelled = []
        self.entry_status = 'OPEN'

    def ltp(self, _):
        return self.price

    def place(self, payload):
        self.orders.append(payload)
        return 'GTT-1'

    def details(self, _):
        return {'rules': [{'strategy': 'ENTRY', 'status': self.entry_status},
                          {'strategy': 'TARGET', 'status': 'INACTIVE'},
                          {'strategy': 'STOPLOSS', 'status': 'INACTIVE'}]}

    def cancel(self, order_id):
        self.cancelled.append(order_id)


@pytest.mark.db
def test_admin_approval_caps_value_and_never_duplicates(db_conn):
    cid = seed_stock(db_conn)
    call = signal()
    add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])) + Decimal('.10'))
    notices = []
    trade_id, state = approve(db_conn, cid, source='admin', broker=broker,
                              clock=lambda: NOW, notify=notices.append)
    assert state == 'submitted'
    assert broker.orders[0]['product'] == 'I'
    assert broker.orders[0]['type'] == 'MULTIPLE'
    assert {r['strategy'] for r in broker.orders[0]['rules']} == {
        'ENTRY', 'TARGET', 'STOPLOSS'}
    assert broker.orders[0]['rules'][0]['trigger_price'] == round(call['reference'], 2)
    stored = db_conn.execute('select entry_price,notional from intraday_trade where trade_id=%s',
                              (trade_id,)).fetchone()
    assert stored[0] == Decimal(str(round(call['reference'], 2)))
    assert stored[1] == broker.orders[0]['quantity'] * stored[0] <= 10000
    assert db_conn.execute('select gtt_order_id from intraday_trade where trade_id=%s',
                           (trade_id,)).fetchone()[0] == 'GTT-1'
    with pytest.raises(TradeError, match='already'):
        approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
                notify=notices.append)
    assert len(broker.orders) == 1


@pytest.mark.db
def test_administrator_limits_have_no_fixed_application_ceiling(db_conn):
    cid = seed_stock(db_conn)
    call = signal()
    add_scan(db_conn, cid, call)
    db_conn.execute('''update intraday_trading_settings set enabled=true,
        max_trade_rupees=50000,max_daily_trades=100,max_daily_rupees=1000000''')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
            notify=lambda _: None)
    assert 10000 < broker.orders[0]['quantity'] * broker.price <= 50000


@pytest.mark.db
def test_definitive_readonly_refusal_does_not_consume_slot_or_block_retry(db_conn,
                                                                          monkeypatch):
    cid = seed_stock(db_conn)
    call = signal()
    add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()

    class ReadOnlyBroker(FakeBroker):
        def place(self, payload):
            self.orders.append(payload)
            raise BrokerError('Read-only Analytics token', definitive=True, code='UDAPI100067')

    monkeypatch.setattr('igs.intraday.trading.record_issue', lambda *args, **kwargs: None)
    broker = ReadOnlyBroker(Decimal(str(call['reference'])))
    assert approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
                   notify=lambda _: None)[1] == 'rejected'
    assert not db_conn.execute('select enabled from intraday_trading_settings').fetchone()[0]
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    assert approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
                   notify=lambda _: None)[1] == 'submitted'
    assert db_conn.execute('select status from intraday_trade order by trade_id').fetchall() == [
        ('rejected',), ('submitted',)]


def test_missing_trading_token_stops_before_reservation(monkeypatch):
    monkeypatch.setattr('igs.intraday.trading.trading_token', lambda: '')
    with pytest.raises(TradeError, match='trading OAuth token is missing'):
        approve(None, 1, source='admin', clock=lambda: NOW)


@pytest.mark.db
def test_rejected_telegram_reply_cannot_replay_same_update(db_conn, monkeypatch):
    cid = seed_stock(db_conn)
    call = signal()
    run_id = add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.execute('''insert into intraday_telegram(company_id,trading_day,action,scan_id,
        symbol,result,expires_at,status,telegram_message_id) values
        (%s,%s,'buy',%s,'TEST',%s,%s,'sent',99)''',
        (cid, NOW.date(), run_id, Jsonb(call),
         dt.datetime.fromisoformat(call['expires_at'])))
    db_conn.commit()

    class Rejected(FakeBroker):
        def place(self, payload):
            self.orders.append(payload)
            raise BrokerError('Read-only token', definitive=True, code='UDAPI100067')

    monkeypatch.setattr('igs.intraday.trading.record_issue', lambda *args, **kwargs: None)
    broker = Rejected(Decimal(str(call['reference'])))
    assert approve(db_conn, cid, source='telegram', telegram_message_id=99,
                   telegram_update_id=77, broker=broker, clock=lambda: NOW,
                   notify=lambda _: None)[1] == 'rejected'
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    with pytest.raises(TradeError, match='already processed'):
        approve(db_conn, cid, source='telegram', telegram_message_id=99,
                telegram_update_id=77, broker=broker, clock=lambda: NOW,
                notify=lambda _: None)
    assert len(broker.orders) == 1


@pytest.mark.db
def test_telegram_must_match_current_sent_call(db_conn):
    cid = seed_stock(db_conn)
    call = signal()
    run_id = add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    with pytest.raises(TradeError, match='does not match'):
        approve(db_conn, cid, source='telegram', telegram_message_id=99,
                telegram_update_id=123,
                broker=broker, clock=lambda: NOW, notify=lambda _: None)
    db_conn.execute('''insert into intraday_telegram(company_id,trading_day,action,scan_id,
        symbol,result,expires_at,status,telegram_message_id) values
        (%s,%s,'buy',%s,'TEST',%s,%s,'sent',99)''',
        (cid, NOW.date(), run_id, Jsonb(call),
         dt.datetime.fromisoformat(call['expires_at'])))
    db_conn.commit()
    assert approve(db_conn, cid, source='telegram', telegram_message_id=99,
                   telegram_update_id=123,
                   broker=broker, clock=lambda: NOW, notify=lambda _: None)[1] == 'submitted'


@pytest.mark.db
def test_telegram_reply_still_valid_after_same_candle_rescan(db_conn):
    cid = seed_stock(db_conn)
    call = signal()
    first = add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.execute('''insert into intraday_telegram(company_id,trading_day,action,scan_id,
        symbol,result,expires_at,status,telegram_message_id) values
        (%s,%s,'buy',%s,'TEST',%s,%s,'sent',99)''',
        (cid, NOW.date(), first, Jsonb(call),
         dt.datetime.fromisoformat(call['expires_at'])))
    db_conn.commit()
    add_scan(db_conn, cid, call)
    broker = FakeBroker(Decimal(str(call['reference'])))
    assert approve(db_conn, cid, source='telegram', telegram_message_id=99,
                   telegram_update_id=124,
                   broker=broker, clock=lambda: NOW, notify=lambda _: None)[1] == 'submitted'


@pytest.mark.db
def test_ambiguous_submit_never_retries(db_conn):
    cid = seed_stock(db_conn)
    call = signal()
    add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()

    class Broken(FakeBroker):
        def place(self, payload):
            self.orders.append(payload)
            raise BrokerError('Upstox connection failed')

    broker = Broken(Decimal(str(call['reference'])))
    assert approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
                   notify=lambda _: None)[1] == 'uncertain'
    with pytest.raises(TradeError, match='already'):
        approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
                notify=lambda _: None)
    assert len(broker.orders) == 1


@pytest.mark.db
def test_interrupted_submit_becomes_uncertain_without_broker_retry(db_conn, monkeypatch):
    cid = seed_stock(db_conn)
    call = signal()
    run_id = add_scan(db_conn, cid, call)
    db_conn.execute('''insert into intraday_trade(company_id,trading_day,action,scan_id,
        symbol,instrument_key,approved_by,approved_at,expires_at,quantity,entry_price,
        stop_price,target_price,notional,status) values
        (%s,%s,'buy',%s,'TEST','NSE_EQ|INE123456789','admin',%s,%s,1,100,99,102,100,
        'submitting')''', (cid, NOW.date(), run_id, NOW,
                         dt.datetime.fromisoformat(call['expires_at'])))
    db_conn.commit()
    issues = []
    monkeypatch.setattr('igs.intraday.trading.record_issue',
                        lambda *args, **kwargs: issues.append(args))
    broker = FakeBroker(Decimal('100'))
    notices = []
    assert reconcile(db_conn, broker=broker, clock=lambda: NOW+dt.timedelta(seconds=31),
                     notify=notices.append) == 1
    assert db_conn.execute('select status from intraday_trade').fetchone()[0] == 'uncertain'
    assert not broker.orders and issues and notices


@pytest.mark.db
def test_unfilled_entry_cancelled_at_expiry_but_filled_entry_keeps_exits(db_conn):
    cid = seed_stock(db_conn)
    call = signal()
    add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
            notify=lambda _: None)
    after = dt.datetime.fromisoformat(call['expires_at']) + dt.timedelta(seconds=1)
    assert reconcile(db_conn, broker=broker, clock=lambda: after, notify=lambda _: None) == 1
    assert broker.cancelled == ['GTT-1']


@pytest.mark.db
def test_filled_entry_with_failed_stop_triggers_error(db_conn, monkeypatch):
    cid = seed_stock(db_conn)
    call = signal()
    add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
            notify=lambda _: None)

    def broken_exits(_):
        return {'rules': [{'strategy': 'ENTRY', 'status': 'COMPLETED'},
                          {'strategy': 'STOPLOSS', 'status': 'FAILED'},
                          {'strategy': 'TARGET', 'status': 'SCHEDULED'}]}

    broker.details = broken_exits
    issues = []
    monkeypatch.setattr('igs.intraday.trading.record_issue',
                        lambda *args, **kwargs: issues.append(args))
    notices = []
    assert reconcile(db_conn, broker=broker, clock=lambda: NOW,
                     notify=notices.append) == 1
    assert db_conn.execute('select status from intraday_trade').fetchone()[0] == 'exit_unprotected'
    assert issues and notices


@pytest.mark.db
def test_filled_entry_retains_linked_exits_after_call_expiry(db_conn):
    cid = seed_stock(db_conn)
    call = signal()
    add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
            notify=lambda _: None)
    broker.entry_status = 'COMPLETED'
    after = dt.datetime.fromisoformat(call['expires_at']) + dt.timedelta(seconds=1)
    assert reconcile(db_conn, broker=broker, clock=lambda: after,
                     notify=lambda _: None) == 1
    assert not broker.cancelled
    assert db_conn.execute('select status from intraday_trade').fetchone()[0] == 'entry_filled'


@pytest.mark.db
def test_exact_private_telegram_reply_places_one_order(db_conn, monkeypatch):
    cid = seed_stock(db_conn)
    call = signal()
    run_id = add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.execute('''insert into intraday_telegram(company_id,trading_day,action,scan_id,
        symbol,result,expires_at,status,telegram_message_id) values
        (%s,%s,'buy',%s,'TEST',%s,%s,'sent',99)''',
        (cid, NOW.date(), run_id, Jsonb(call),
         dt.datetime.fromisoformat(call['expires_at'])))
    db_conn.commit()
    monkeypatch.setenv('IGS_TELEGRAM_TOKEN', 'fake')
    monkeypatch.setenv('IGS_TELEGRAM_CHAT_ID', '123')
    messages = [
        {'update_id': 1, 'message': {'text': 'approved', 'chat': {'id': 999,
         'type': 'private'}, 'from': {'id': 999}, 'reply_to_message': {'message_id': 99}}},
        {'update_id': 2, 'message': {'text': 'approved', 'chat': {'id': 123,
         'type': 'private'}, 'from': {'id': 123}, 'reply_to_message': {'message_id': 98}}},
        {'update_id': 3, 'message': {'text': 'approved', 'chat': {'id': 123,
         'type': 'private'}, 'from': {'id': 123}, 'reply_to_message': {'message_id': 99}}},
    ]

    def telegram(method, _token, _client, **fields):
        if method == 'getWebhookInfo':
            return {'result': {'url': ''}}
        return {'result': [m for m in messages if m['update_id'] >= int(fields['offset'])]}

    monkeypatch.setattr('igs.intraday.telegram_approvals._telegram', telegram)
    broker = FakeBroker(Decimal(str(call['reference'])))
    assert poll(db_conn, client=object(), broker=broker, clock=lambda: NOW,
                notify=lambda _: None) == 1
    assert len(broker.orders) == 1
    assert poll(db_conn, client=object(), broker=broker, clock=lambda: NOW,
                notify=lambda _: None) == 0
    assert db_conn.execute('select next_update_id from intraday_telegram_cursor').fetchone()[0] == 4


@pytest.mark.db
def test_approved_short_has_sell_entry_and_risk_levels(db_conn):
    from test_intraday import sample

    from igs.intraday.engine import Candle, evaluate

    cid = seed_stock(db_conn)
    bars, history = sample()
    down = [Candle(b.start, 200-b.open, 200-b.low, 200-b.high,
                   200-b.close, b.volume) for b in bars]
    call = evaluate(down, history, NOW, down)
    assert call['action'] == 'sell'
    add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
            notify=lambda _: None)
    order = broker.orders[0]
    assert order['transaction_type'] == 'SELL'
    levels = {r['strategy']: r['trigger_price'] for r in order['rules']}
    assert levels['TARGET'] < levels['ENTRY'] < levels['STOPLOSS']


@pytest.mark.db
def test_ineligible_stock_cannot_reserve_or_submit_order(db_conn, monkeypatch):
    cid = seed_stock(db_conn)
    add_scan(db_conn, cid, signal())
    monkeypatch.setattr('igs.intraday.eligibility.tick_sizes', lambda **kwargs: {})

    class NoRequests(FakeBroker):
        def ltp(self, key):
            pytest.fail('Ineligible stock must be blocked before broker requests')

    broker = NoRequests(Decimal('100'))
    with pytest.raises(TradeError, match='does not currently allow intraday'):
        approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW)
    assert db_conn.execute('select count(*) from intraday_trade').fetchone()[0] == 0


@pytest.mark.parametrize('action,live', [('buy', '100.3'), ('buy', '99.7'),
                                         ('sell', '100.3'), ('sell', '99.7')])
def test_entry_limit_stays_at_recommendation_when_live_quote_moves(action, live):
    from igs.intraday.trading import _plan, intraday_rates

    call = {'action': action, 'reference': 100, 'stop': 98 if action == 'buy' else 102,
            'target': 104 if action == 'buy' else 96}
    cfg = {'max_trade_rupees': Decimal('10000'), 'max_price_deviation_pct': Decimal('.5'),
           'max_risk_rupees': Decimal('1000'), 'min_net_reward_risk': Decimal('1.5')}
    quantity, entry, stop, target, payload, *_ = _plan(
        {'instrument_key': 'NSE_EQ|TEST', 'result': call}, Decimal(live), cfg, Decimal('0.05'),
        intraday_rates())
    assert entry == Decimal('100')
    assert quantity == 100
    assert payload['rules'][0] == {'strategy': 'ENTRY', 'trigger_type': 'IMMEDIATE',
                                   'trigger_price': 100.0}
    assert payload['rules'][1]['trigger_price'] == float(target)
    assert payload['rules'][2]['trigger_price'] == float(stop)


@pytest.mark.db
def test_changed_page_call_is_not_silently_approved(db_conn):
    cid = seed_stock(db_conn)
    call = signal()
    add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    with pytest.raises(TradeError, match='displayed call changed'):
        approve(db_conn, cid, source='admin', expected_call={**call, 'reference': 1},
                broker=broker, clock=lambda: NOW, notify=lambda _: None)
    assert not broker.orders
    assert db_conn.execute('select count(*) from intraday_trade').fetchone()[0] == 0


def _sent(conn, cid, run_id, call, message_id=99):
    conn.execute('''insert into intraday_telegram(company_id,trading_day,action,scan_id,
        symbol,result,expires_at,status,telegram_message_id) values
        (%s,%s,%s,%s,'TEST',%s,%s,'sent',%s)''',
        (cid, NOW.date(), call['action'], run_id, Jsonb(call),
         dt.datetime.fromisoformat(call['expires_at']), message_id))
    conn.commit()


@pytest.mark.db
def test_reply_is_honoured_until_expiry_while_later_scans_wait_or_run(db_conn):
    """The next scan starts 5m20s after the candle; a call is open for ten minutes."""
    cid = seed_stock(db_conn)
    call = signal()
    _sent(db_conn, cid, add_scan(db_conn, cid, call), call)
    add_scan(db_conn, cid, {'action': 'wait', 'reason': 'Volume jump below 1.8×',
                            'evidence': []}, observed=NOW + dt.timedelta(minutes=5))
    add_scan(db_conn, cid, {'action': 'wait', 'reason': '', 'evidence': []},
             status='running', observed=NOW + dt.timedelta(minutes=6))
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    later = NOW + dt.timedelta(minutes=7)                       # 10:07:20, expiry 10:10
    assert approve(db_conn, cid, source='telegram', telegram_message_id=99,
                   telegram_update_id=500, broker=broker, clock=lambda: later,
                   notify=lambda _: None)[1] == 'submitted'
    first = db_conn.execute('select min(scan_id) from intraday_scan').fetchone()[0]
    assert db_conn.execute('select scan_id from intraday_trade').fetchone()[0] == first


@pytest.mark.db
@pytest.mark.parametrize('later_reading', [
    {'action': 'sell', 'reason': 'reversal', 'evidence': []},
    {'action': 'wait', 'reason': 'Conflicting recent news', 'evidence': [{'direction': -1}]}])
def test_reversal_or_opposing_news_withdraws_an_open_call(db_conn, later_reading):
    cid = seed_stock(db_conn)
    call = signal()
    _sent(db_conn, cid, add_scan(db_conn, cid, call), call)
    add_scan(db_conn, cid, later_reading, observed=NOW + dt.timedelta(minutes=5))
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    later = NOW + dt.timedelta(minutes=6)
    with pytest.raises(TradeError, match='does not match'):
        approve(db_conn, cid, source='telegram', telegram_message_id=99,
                telegram_update_id=501, broker=broker, clock=lambda: later,
                notify=lambda _: None)
    with pytest.raises(TradeError, match='expired, reversed or met opposing news'):
        approve(db_conn, cid, source='admin', broker=broker, clock=lambda: later,
                notify=lambda _: None)
    assert not broker.orders


@pytest.mark.db
def test_no_approval_after_the_stated_expiry(db_conn):
    cid = seed_stock(db_conn)
    call = signal()
    _sent(db_conn, cid, add_scan(db_conn, cid, call), call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    expiry = dt.datetime.fromisoformat(call['expires_at'])
    with pytest.raises(TradeError, match='does not match'):
        approve(db_conn, cid, source='telegram', telegram_message_id=99,
                telegram_update_id=502, broker=broker, clock=lambda: expiry,
                notify=lambda _: None)
    assert not broker.orders


@pytest.mark.db
def test_page_lists_an_open_call_while_the_next_scan_runs(db_conn):
    from igs.intraday.scanner import active_calls

    cid = seed_stock(db_conn)
    call = signal()
    add_scan(db_conn, cid, call)
    add_scan(db_conn, cid, {'action': 'wait', 'reason': '', 'evidence': []},
             status='running', observed=NOW + dt.timedelta(minutes=5))
    [row] = active_calls(db_conn, NOW + dt.timedelta(minutes=6))
    assert row['company_id'] == cid and row['result']['stop'] == call['stop']
    assert active_calls(db_conn, dt.datetime.fromisoformat(call['expires_at'])) == []


def test_automatic_volume_uses_exact_unrounded_ratio():
    from igs.intraday.trading import exceptional_volume

    call = signal()
    call['baseline_volume'] = 20000
    call['candle_volume'] = 1_000_000
    assert not exceptional_volume(call)
    call['candle_volume'] = 1_000_020
    call['rvol'] = 50.0  # display rounds down, but the exact ratio exceeds 50×
    assert exceptional_volume(call)
    for change in ({'baseline_volume': 0}, {'candle_volume': float('nan')},
                   {'baseline_volume': 'bad'}):
        assert not exceptional_volume({**call, **change})


@pytest.mark.parametrize('action, levels', [
    # 100.3365 down to the 5-paisa tick; 1% below it, 99.297, down again; the call's target
    ('buy', ('100.30', '99.25', '102.25')),
    # 102.3635 up to the tick; 1% above it, 103.424, up again; the call's target
    ('sell', ('102.40', '103.45', '100.45'))])
def test_an_automatic_plan_moves_the_entry_and_stop_but_keeps_the_target(
        action, levels, zero_intraday_charges):
    from igs.intraday.trading import AUTO_ENTRY_OFFSET_PCT, AUTO_STOP_PCT, _plan

    sign = 1 if action == 'buy' else -1
    call = {'action': action, 'reference': 101.35, 'stop': 101.35 - sign * .45,
            'target': 101.35 + sign * .9}
    cfg = {'max_trade_rupees': Decimal('10000'), 'max_price_deviation_pct': Decimal('.5'),
           'max_risk_rupees': Decimal('1000'), 'min_net_reward_risk': Decimal('1.5')}
    signal = {'instrument_key': 'NSE_EQ|TEST', 'result': call}
    quantity, entry, stop, target, payload, loss, _ = _plan(
        signal, Decimal('101.35'), cfg, Decimal('0.05'), zero_intraday_charges,
        offset_pct=AUTO_ENTRY_OFFSET_PCT, stop_pct=AUTO_STOP_PCT)
    assert (entry, stop, target) == tuple(Decimal(x) for x in levels)
    assert [r['trigger_price'] for r in payload['rules']] == [float(entry), float(target),
                                                              float(stop)]
    assert quantity == int(Decimal('10000') // entry) and loss == quantity * Decimal('1.05')
    # Without them (an approved call) the order keeps the call's price, stop and target.
    assert _plan(signal, Decimal('101.35'), cfg, Decimal('0.05'),
                 zero_intraday_charges)[1:4] == tuple(
        Decimal(str(round(x, 2))) for x in (101.35, call['stop'], call['target']))


def test_an_automatic_plan_is_refused_when_the_live_price_is_at_its_stop(
        zero_intraday_charges):
    from igs.intraday.trading import AUTO_ENTRY_OFFSET_PCT, AUTO_STOP_PCT, _plan

    # A 2% call stop: the live price 98.01 is above it, but at the order's stop, 1% below
    # the 99.00 limit; the limit would fill at once and stop out.
    signal = {'instrument_key': 'NSE_EQ|TEST', 'result': {
        'action': 'buy', 'reference': 100.0, 'stop': 98.0, 'target': 104.0}}
    cfg = {'max_trade_rupees': Decimal('10000'), 'max_price_deviation_pct': Decimal('2'),
           'max_risk_rupees': Decimal('1000'), 'min_net_reward_risk': Decimal('1.5')}
    with pytest.raises(TradeError, match='already past the stop'):
        _plan(signal, Decimal('98.01'), cfg, Decimal('0.01'), zero_intraday_charges,
              offset_pct=AUTO_ENTRY_OFFSET_PCT, stop_pct=AUTO_STOP_PCT)


@pytest.mark.db
def test_auto_volume_uses_existing_limits_and_never_retries(db_conn):
    from igs.intraday.trading import place_exceptional_volume

    cid = seed_stock(db_conn)
    call = {**signal(), 'rvol': 50.0, 'candle_volume': 1_000_020,
            'baseline_volume': 20_000}
    add_scan(db_conn, cid, call)
    db_conn.execute('''update intraday_trading_settings set enabled=true,
        auto_high_volume_enabled=true''')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    notices = []
    assert place_exceptional_volume(db_conn, broker=broker, clock=lambda: NOW,
                                    notify=notices.append) == 1
    # The entry limit is 1% below the call's 101.35 (100.3365, down to the paisa), the
    # stop 1% below that (99.3267, down), and the target the call's 102.25.
    assert [(r['strategy'], r['trigger_price']) for r in broker.orders[0]['rules']] == [
        ('ENTRY', 100.33), ('TARGET', 102.25), ('STOPLOSS', 99.32)]
    assert db_conn.execute('select approved_by,entry_price,stop_price,target_price '
                           'from intraday_trade').fetchone() == (
        'auto', Decimal('100.33'), Decimal('99.32'), Decimal('102.25'))
    assert any('Automatic intraday' in notice and "limit ₹100.33 (1% below the call's "
               "price); stop ₹99.32 (1% below the limit), target ₹102.25 (the call's)"
               in notice for notice in notices)
    assert place_exceptional_volume(db_conn, broker=broker, clock=lambda: NOW,
                                    notify=notices.append) == 0
    assert len(broker.orders) == 1


@pytest.mark.db
def test_automatic_volume_stays_off_without_switch_and_at_exactly_50(db_conn):
    from igs.intraday.trading import place_exceptional_volume

    cid = seed_stock(db_conn)
    call = {**signal(), 'rvol': 50.01, 'candle_volume': 1_000_020,
            'baseline_volume': 20_000}
    add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(call['reference'])))
    assert place_exceptional_volume(db_conn, broker=broker, clock=lambda: NOW) == 0
    db_conn.execute('update intraday_trading_settings set auto_high_volume_enabled=true')
    db_conn.execute('update intraday_signal set result=jsonb_set(result, '\
                    "'{candle_volume}', '1000000'::jsonb)")
    db_conn.commit()
    assert place_exceptional_volume(db_conn, broker=broker, clock=lambda: NOW) == 0
    assert not broker.orders


@pytest.mark.db
def test_automatic_broker_rejection_is_not_retried(db_conn, monkeypatch):
    from igs.intraday.trading import place_exceptional_volume

    cid = seed_stock(db_conn)
    call = {**signal(), 'candle_volume': 1_000_020, 'baseline_volume': 20_000}
    add_scan(db_conn, cid, call)
    db_conn.execute('''update intraday_trading_settings set enabled=true,
        auto_high_volume_enabled=true''')
    db_conn.commit()

    class Rejected(FakeBroker):
        def place(self, payload):
            self.orders.append(payload)
            raise BrokerError('Definitive refusal', definitive=True, code='UDAPI999')

    monkeypatch.setattr('igs.intraday.trading.record_issue', lambda *args, **kwargs: None)
    broker = Rejected(Decimal(str(call['reference'])))
    assert place_exceptional_volume(db_conn, broker=broker, clock=lambda: NOW,
                                    notify=lambda _: None) == 1
    assert db_conn.execute('select status from intraday_trade').fetchone()[0] == 'rejected'
    assert place_exceptional_volume(db_conn, broker=broker, clock=lambda: NOW,
                                    notify=lambda _: None) == 0
    assert len(broker.orders) == 1
