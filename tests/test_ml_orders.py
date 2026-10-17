"""Real orders for the ML intraday calls (igs.intraday.trading.approve_ml), their 15:00
square-off, and the switch for the rule-based calls."""
import datetime as dt
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb
from test_intraday import NOW, seed_stock
from test_intraday_telegram import add_scan, signal

from igs.intraday import ml
from igs.intraday.telegram_approvals import poll
from igs.intraday.trading import (
    BrokerError,
    TradeError,
    approve,
    approve_ml,
    ml_plan,
    place_exceptional_volume,
    reconcile,
)
from igs.timeutil import IST

pytestmark = pytest.mark.usefixtures("intraday_eligibility", "zero_intraday_charges")

DAY = dt.date(2026, 10, 5)                         # a Monday
KEY = 'NSE_EQ|INE123456789'                        # the eligible test instrument (conftest)


def at(hh, mm, ss=0):
    return dt.datetime.combine(DAY, dt.time(hh, mm, ss), IST)


def seed_call(conn, side=-1, price=100.0, atr_pct=0.02, orders=True):
    """One ML call for DAY on the test stock, decided at 09:46; live and ML orders on, with
    Rs 1 lakh a trade and Rs 1,000 at most lost at the stop."""
    cid = seed_stock(conn)
    conn.execute("""insert into ml_intraday_day (session_date, decided_at, model, universe,
                        scored) values (%s, %s, 'intraday-ml-v1', 200, 150)""",
                 (DAY, at(9, 46)))
    conn.execute("""insert into ml_intraday_pick (session_date, instrument_key, company_id,
                        symbol, side, prediction, rank, features, entry_after, status,
                        decision_price)
                    values (%s, %s, %s, 'TEST', %s, %s, 1, %s, %s, 'open', %s)""",
                 (DAY, KEY, cid, side, 0.004 * side, Jsonb({'atr_pct': atr_pct}), at(9, 46),
                  price))
    conn.execute("""update intraday_trading_settings set enabled=true, ml_orders_enabled=%s,
                        max_trade_rupees=100000, max_risk_rupees=1000,
                        max_daily_rupees=300000""", (orders,))
    conn.commit()
    return cid


class Broker:
    """Upstox as the ML orders use it: a GTT with an entry and a stop (GTT-9; its entry
    order E1), the app's square-off order X1."""

    def __init__(self, price):
        self.price = Decimal(str(price))
        self.orders, self.cancelled, self.cancelled_orders, self.exits = [], [], [], []
        self.entry, self.stop, self.stop_order = 'OPEN', 'SCHEDULED', None
        self.states = {'E1': ('open', 0)}
        self.cancel_works = True

    def ltp(self, _):
        return self.price

    def place(self, payload):
        self.orders.append(payload)
        return 'GTT-9'

    def details(self, _):
        return {'rules': [{'strategy': 'ENTRY', 'status': self.entry, 'order_id': 'E1'},
                          {'strategy': 'STOPLOSS', 'status': self.stop,
                           'order_id': self.stop_order}]}

    def cancel(self, gtt_id):
        self.cancelled.append(gtt_id)
        if not self.cancel_works:
            raise BrokerError('Upstox returned HTTP 400', definitive=True)
        self.stop = 'CANCELLED'

    def cancel_order(self, order_id):
        self.cancelled_orders.append(order_id)

    def order_state(self, order_id):
        return self.states[order_id]

    def fill(self, order_id):
        return {'E1': (Decimal('99.95'), 909), 'X1': (Decimal('99.00'), 909)}[order_id]

    def square_off(self, key, quantity, action):
        self.exits.append((key, quantity, action))
        self.states['X1'] = ('open', 0)
        return 'X1'


@pytest.mark.db
def test_an_approved_call_enters_at_the_live_price_with_a_stop_and_no_target(db_conn):
    seed_call(db_conn)
    broker, sent = Broker(100), []
    trade_id, status = approve_ml(db_conn, KEY, source='admin', broker=broker,
                                  clock=lambda: at(9, 50), notify=sent.append)
    assert status == 'submitted'
    # A short sale: the limit 0.1% below the live Rs 100, the stop half of the 2% ATR above.
    # Shares: Rs 1,000 at the stop over Rs 1.10 a share (under Rs 1 lakh / 99.90).
    assert broker.orders == [{
        'type': 'MULTIPLE', 'quantity': 909, 'product': 'I', 'instrument_token': KEY,
        'transaction_type': 'SELL', 'rules': [
            {'strategy': 'ENTRY', 'trigger_type': 'IMMEDIATE', 'trigger_price': 99.9},
            {'strategy': 'STOPLOSS', 'trigger_type': 'IMMEDIATE', 'trigger_price': 101.0}]}]
    row = db_conn.execute("""select kind, scan_id, target_price, entry_price, stop_price,
                                    expires_at, approved_by, status
                             from intraday_trade where trade_id=%s""", (trade_id,)).fetchone()
    assert row == ('ml', None, None, Decimal('99.90'), Decimal('101.00'), at(10, 0), 'admin',
                   'submitted')
    assert 'squares it off at 15:00 IST' in sent[-1] and 'Trade #' in sent[-1]
    with pytest.raises(TradeError, match='already has an intraday order today'):
        approve_ml(db_conn, KEY, source='admin', broker=broker, clock=lambda: at(9, 51))


def test_a_buy_is_priced_in_whole_ticks_away_from_the_trade(zero_intraday_charges):
    pick = {'side': 1, 'decision_price': 200, 'features': {'atr_pct': 0.03},
            'instrument_key': KEY}
    cfg = {'max_price_deviation_pct': Decimal('0.5'), 'max_trade_rupees': 100000,
           'max_risk_rupees': 1000}
    quantity, entry, stop, payload, loss, _ = ml_plan(pick, Decimal('200.4'), cfg,
                                                      Decimal('0.05'), zero_intraday_charges)
    # Limit 0.1% above 200.40 (200.6004) rounded down, stop 1.5% below it rounded down.
    assert (entry, stop) == (Decimal('200.60'), Decimal('197.35'))
    assert quantity == min(int(100000 // Decimal('200.60')), int(1000 // Decimal('3.25')))
    assert payload['transaction_type'] == 'BUY' and loss == quantity * Decimal('3.25')


@pytest.mark.db
@pytest.mark.parametrize('case, message', [
    ('off', 'Real orders for the ML calls are off'),
    ('late', 'only until 10:00 IST'),
    ('moved', 'The price has moved -0.60% since the model decided'),
    ('live', 'Live intraday trading is disabled'),
])
def test_an_approval_is_refused_when_off_late_or_after_the_price_moved(db_conn, case,
                                                                       message):
    seed_call(db_conn, orders=case != 'off')
    if case == 'live':
        db_conn.execute('update intraday_trading_settings set enabled=false')
        db_conn.commit()
    broker = Broker(99.4 if case == 'moved' else 100)
    with pytest.raises(TradeError, match=message):
        approve_ml(db_conn, KEY, source='admin', broker=broker,
                   clock=lambda: at(10, 0) if case == 'late' else at(9, 55))
    assert broker.orders == []
    assert db_conn.execute('select count(*) from intraday_trade').fetchone()[0] == 0


@pytest.mark.db
def test_a_telegram_reply_to_an_ml_call_places_its_order_once(db_conn, monkeypatch):
    seed_call(db_conn)
    db_conn.execute('update ml_intraday_pick set telegram_message_id=77')
    db_conn.commit()
    monkeypatch.setenv('IGS_TELEGRAM_TOKEN', 'fake')
    monkeypatch.setenv('IGS_TELEGRAM_CHAT_ID', '123')
    me = {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123}}
    messages = [{'update_id': 1, 'message': {'text': 'Approved', **me,
                                             'reply_to_message': {'message_id': 77}}},
                {'update_id': 2, 'message': {'text': 'approved', **me,
                                             'reply_to_message': {'message_id': 77}}}]

    def telegram(method, _token, _client, **fields):
        if method == 'getWebhookInfo':
            return {'result': {'url': ''}}
        return {'result': [m for m in messages if m['update_id'] >= int(fields['offset'])]}

    monkeypatch.setattr('igs.intraday.telegram_approvals._telegram', telegram)
    broker, said = Broker(100), []
    assert poll(db_conn, client=object(), broker=broker, clock=lambda: at(9, 52),
                notify=said.append) == 1
    assert len(broker.orders) == 1
    assert db_conn.execute("select approved_by, telegram_update_id from intraday_trade"
                           ).fetchone() == ('telegram', 1)
    assert any('already has an intraday order today' in s for s in said)


@pytest.mark.db
def test_the_app_squares_off_at_1500_once_the_stop_is_cancelled(db_conn):
    seed_call(db_conn)
    broker, sent = Broker(100), []
    trade_id, _ = approve_ml(db_conn, KEY, source='admin', broker=broker,
                             clock=lambda: at(9, 50), notify=sent.append)
    broker.entry, broker.states['E1'] = 'COMPLETED', ('complete', 909)
    reconcile(db_conn, broker=broker, clock=lambda: at(14, 59), notify=sent.append)
    assert db_conn.execute('select status from intraday_trade').fetchone()[0] == 'entry_filled'
    assert broker.exits == []
    reconcile(db_conn, broker=broker, clock=lambda: at(15, 0, 20), notify=sent.append)
    assert broker.cancelled == ['GTT-9'] and broker.exits == [(KEY, 909, 'sell')]
    assert db_conn.execute('select status, exit_order_id from intraday_trade').fetchone() == (
        'squaring_off', 'X1')
    assert 'buy back 909 shares' in sent[-1]
    reconcile(db_conn, broker=broker, clock=lambda: at(15, 0, 40), notify=sent.append)
    assert broker.exits == [(KEY, 909, 'sell')]                 # never sent twice
    broker.states['X1'] = ('complete', 909)
    reconcile(db_conn, broker=broker, clock=lambda: at(15, 1), notify=sent.append)
    status, net = db_conn.execute('select status, net_pnl from intraday_trade').fetchone()
    # Short 909 at 99.95, bought back at 99.00, charges zero in this test.
    assert status == 'closed' and net == Decimal('863.55')
    assert f'Trade #{trade_id}' in sent[-1] and 'net P&L ₹863.55' in sent[-1]


@pytest.mark.db
def test_no_second_exit_while_upstox_keeps_the_stop_working(db_conn):
    seed_call(db_conn)
    broker, sent = Broker(100), []
    approve_ml(db_conn, KEY, source='admin', broker=broker, clock=lambda: at(9, 50),
               notify=sent.append)
    broker.entry, broker.states['E1'] = 'COMPLETED', ('complete', 909)
    broker.cancel_works = False
    reconcile(db_conn, broker=broker, clock=lambda: at(15, 0, 20), notify=sent.append)
    assert broker.exits == []
    assert db_conn.execute('select status from intraday_trade').fetchone()[0] == \
        'square_off_failed'
    assert 'no second exit' in sent[-1] and '15:10 IST' in sent[-1]


@pytest.mark.db
def test_a_stop_that_fired_part_way_leaves_only_the_rest_to_square_off(db_conn):
    seed_call(db_conn)
    broker = Broker(100)
    approve_ml(db_conn, KEY, source='admin', broker=broker, clock=lambda: at(9, 50),
               notify=lambda _: None)
    broker.entry, broker.states['E1'] = 'COMPLETED', ('complete', 909)
    broker.stop, broker.stop_order = 'TRIGGERED', 'S1'
    broker.states['S1'] = ('cancelled', 300)        # the app cancelled its unfilled rest
    reconcile(db_conn, broker=broker, clock=lambda: at(15, 2), notify=lambda _: None)
    assert broker.cancelled_orders == [] and broker.exits == [(KEY, 609, 'sell')]


@pytest.mark.db
def test_after_1509_or_a_lost_answer_the_app_sends_no_exit(db_conn):
    seed_call(db_conn)
    broker, sent = Broker(100), []
    approve_ml(db_conn, KEY, source='admin', broker=broker, clock=lambda: at(9, 50),
               notify=sent.append)
    broker.entry, broker.states['E1'] = 'COMPLETED', ('complete', 909)
    reconcile(db_conn, broker=broker, clock=lambda: at(15, 9), notify=sent.append)
    assert broker.exits == [] and broker.cancelled == []
    assert db_conn.execute('select status from intraday_trade').fetchone()[0] == \
        'square_off_failed'
    # A square-off interrupted before Upstox answered is reported, not resent.
    db_conn.execute("""update intraday_trade set status='squaring_off', exit_order_id=null,
                           last_checked_at=%s""", (at(15, 0),))
    db_conn.commit()
    reconcile(db_conn, broker=broker, clock=lambda: at(15, 0, 50), notify=sent.append)
    assert db_conn.execute('select status, last_error from intraday_trade').fetchone() == (
        'square_off_failed', 'the square-off was interrupted before Upstox answered')
    assert broker.exits == []


@pytest.mark.db
def test_each_call_gets_its_own_message_to_approve_while_approvals_are_open(db_conn):
    seed_call(db_conn)
    ids, sent = iter([501, 502]), []

    def sender(text):
        sent.append(text)
        return next(ids)

    assert ml.send_order_calls(db_conn, at(9, 47), sender) == 1
    assert db_conn.execute('select telegram_message_id from ml_intraday_pick'
                           ).fetchone()[0] == 501
    assert sent[0].startswith('ML INTRADAY SELL SHORT · TEST')
    assert 'Reply APPROVED to this message before 10:00 IST' in sent[0]
    assert 'about 909 shares (₹90,809)' in sent[0] and 'squared off by the app at 15:00' in sent[0]
    assert ml.send_order_calls(db_conn, at(9, 48), sender) == 0          # once
    db_conn.execute('update ml_intraday_pick set telegram_message_id=null')
    db_conn.commit()
    assert ml.send_order_calls(db_conn, at(10, 0), sender) == 0          # approvals closed
    db_conn.execute('update intraday_trading_settings set ml_orders_enabled=false')
    db_conn.commit()
    assert ml.send_order_calls(db_conn, at(9, 49), sender) == 0          # orders off


@pytest.mark.db
def test_a_small_trade_is_warned_that_charges_eat_the_edge(db_conn):
    seed_call(db_conn)
    db_conn.execute('update intraday_trading_settings set max_trade_rupees=10000')
    db_conn.commit()
    sent = []
    ml.send_order_calls(db_conn, at(9, 47), lambda text: sent.append(text) or 9)
    assert 'charges take most of the tested edge' in sent[0]


@pytest.mark.db
def test_rule_calls_are_off_by_default_alerts_approvals_and_automatic_orders(db_conn):
    from igs.alerts.intraday import deliver
    cid = seed_stock(db_conn)
    add_scan(db_conn, cid, signal())
    db_conn.execute('update intraday_trading_settings set enabled=true, '
                    'auto_high_volume_enabled=true')
    db_conn.commit()
    sent = []
    assert deliver(db_conn, lambda text: sent.append(text) or 1, clock=lambda: NOW) == 0
    assert sent == []
    with pytest.raises(TradeError, match='Rule-based intraday calls are switched off'):
        approve(db_conn, cid, source='admin', broker=Broker(100), clock=lambda: NOW)
    assert place_exceptional_volume(db_conn, broker=Broker(100), clock=lambda: NOW) == 0


def test_the_square_off_is_a_market_order_at_the_documented_endpoint():
    import httpx

    from igs.intraday.trading import Broker as Upstox
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={'status': 'success',
                                         'data': {'order_ids': ['1644490272000']}})

    upstox = Upstox('secret', transport=httpx.MockTransport(handle))
    assert upstox.square_off(KEY, 609, 'sell') == '1644490272000'
    request = seen[0]
    assert (request.method, request.url.host, request.url.path) == (
        'POST', 'api-hft.upstox.com', '/v3/order/place')
    body = __import__('json').loads(request.content)
    assert {k: body[k] for k in ('quantity', 'product', 'order_type', 'transaction_type',
                                 'instrument_token', 'validity')} == {
        'quantity': 609, 'product': 'I', 'order_type': 'MARKET', 'transaction_type': 'BUY',
        'instrument_token': KEY, 'validity': 'DAY'}
    upstox.close()


@pytest.mark.db
def test_the_page_offers_approval_only_when_real_orders_are_on(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest
    seed_call(db_conn, orders=False)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    script = '''from igs.db import connect
import igs.ui.auth as auth
auth.is_admin = lambda: True
from igs.ui.intraday_ml import page
page(connect(autocommit=True))'''
    at_ = AppTest.from_string(script, default_timeout=30).run()
    assert not at_.exception, at_.exception
    assert any('Real orders for the ML calls are off' in i.value for i in at_.info)
    assert any('squares it off at 15:00' in c.value for c in at_.caption)
