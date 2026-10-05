import datetime as dt
from decimal import Decimal

import httpx
import pytest
from psycopg.types.json import Jsonb
from test_intraday import NOW, seed_stock
from test_intraday_telegram import add_scan, signal

from igs.intraday.telegram_approvals import poll
from igs.intraday.trading import Broker, BrokerError, TradeError, approve, reconcile


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
    broker = FakeBroker(Decimal(str(call['reference'])))
    notices = []
    trade_id, state = approve(db_conn, cid, source='admin', broker=broker,
                              clock=lambda: NOW, notify=notices.append)
    assert state == 'submitted'
    assert broker.orders[0]['product'] == 'I'
    assert broker.orders[0]['type'] == 'MULTIPLE'
    assert {r['strategy'] for r in broker.orders[0]['rules']} == {
        'ENTRY', 'TARGET', 'STOPLOSS'}
    assert broker.orders[0]['quantity'] * broker.price <= 10000
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
