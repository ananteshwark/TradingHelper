"""Intraday sizing by the loss at the stop, and the charge check before an order, with
the real config/costs.yaml rates."""

from decimal import Decimal

import pytest
from test_intraday import NOW, seed_stock
from test_intraday_telegram import add_scan, signal
from test_intraday_trading import FakeBroker

from igs.intraday.trading import TradeError, _plan, approve, charges, intraday_rates

pytestmark = pytest.mark.usefixtures("intraday_eligibility")
TICK = Decimal('0.05')


def settings(per_trade, max_risk, floor='1.5'):
    return {'max_trade_rupees': Decimal(per_trade), 'max_risk_rupees': Decimal(max_risk),
            'min_net_reward_risk': Decimal(floor), 'max_price_deviation_pct': Decimal('.5')}


def call(action='buy', reference=101.35, stop=100.9, target=102.25):
    return {'instrument_key': 'NSE_EQ|TEST',
            'result': {'action': action, 'reference': reference, 'stop': stop,
                       'target': target}}


def test_round_trip_charges_by_hand():
    # 98 shares bought at 101.35 (9,932.30) and sold at 102.25 (10,020.50):
    # brokerage 0.1% capped at Rs 20 per order: 9.9323 + 10.0205 = 19.9528
    # exchange + SEBI on 19,952.80: 0.5926 + 0.0200;  GST 18% on those: 3.7017
    # STT 0.025% of the sell: 2.5051;  stamp 0.003% of the buy: 0.2980
    assert charges(98, Decimal('101.35'), Decimal('102.25'), 'buy',
                   intraday_rates()) == Decimal('27.07')
    # A short sells first: STT falls on the entry, stamp duty on the exit.
    short = charges(98, Decimal('102.25'), Decimal('101.35'), 'sell', intraday_rates())
    assert short == Decimal('27.07')


def test_a_small_ticket_is_refused_when_charges_eat_the_reward():
    """Rs 10,000 at a 0.45% stop: Rs 88 to gain, Rs 44 to lose, about Rs 27 of charges."""
    with pytest.raises(TradeError, match=r'Estimated charges of ₹27\.07 leave the target '
                                         r'earning 0\.86× what the stop loses \(minimum 1\.5×\)'):
        _plan(call(), Decimal('101.35'), settings('10000', '1000'), TICK, intraday_rates())
    # Rs 1,00,000: brokerage is capped, so charges are a smaller share of the move.
    quantity, *_, loss, est = _plan(call(), Decimal('101.35'), settings('100000', '1000'),
                                   TICK, intraday_rates())
    assert quantity == 986 and loss == Decimal('443.70') and est == Decimal('82.67')
    # The administrator can accept thinner trades.
    assert _plan(call(), Decimal('101.35'), settings('10000', '1000', '0.5'), TICK,
                 intraday_rates())[0] == 98


def test_quantity_is_capped_by_the_loss_at_the_stop():
    wide = call(reference=100, stop=98, target=104)               # a 2% stop
    quantity, *_, loss, _ = _plan(wide, Decimal('100'), settings('10000', '100', '0'), TICK,
                                  intraday_rates())
    assert quantity == 50 and loss == Decimal('100')              # not 100 shares
    with pytest.raises(TradeError, match='maximum loss per trade'):
        _plan(wide, Decimal('100'), settings('10000', '1', '0'), TICK, intraday_rates())



def test_an_automatic_order_at_two_and_a_half_times_after_charges():
    """Recommended Rs 100, stop 99, target 102: the order buys at 99.00 with its stop at
    98.01 and the call's 102 target, 3.03x before charges. After charges that is 2.17x
    at Rs 10,000 per trade, refused at 2.5x, and 2.72x at Rs 1,00,000."""
    from igs.intraday.trading import ENTRY_OFFSET_PCT, STOP_PCT

    signal = call(reference=100.0, stop=99.0, target=102.0)
    levels = {'offset_pct': ENTRY_OFFSET_PCT, 'stop_pct': STOP_PCT}
    with pytest.raises(TradeError, match=r'2\.17× what the stop loses \(minimum 2\.5×\)'):
        _plan(signal, Decimal('100'), settings('10000', '100', '2.5'), Decimal('0.01'),
              intraday_rates(), **levels)
    quantity, entry, stop, target, _, loss, est = _plan(
        signal, Decimal('100'), settings('100000', '1000', '2.5'), Decimal('0.01'),
        intraday_rates(), **levels)
    assert (quantity, entry, stop, target) == (1010, Decimal('99.00'), Decimal('98.01'),
                                               Decimal('102.00'))
    assert (loss, est) == (Decimal('999.90'), Decimal('83.31'))

@pytest.mark.db
def test_defaults_and_what_an_order_records(db_conn):
    assert db_conn.execute('select max_risk_rupees,min_net_reward_risk from '
                           'intraday_trading_settings').fetchone() == (Decimal('100.00'),
                                                                         Decimal('1.5'))
    cid = seed_stock(db_conn)
    result = signal()
    add_scan(db_conn, cid, result)
    db_conn.execute('''update intraday_trading_settings set enabled=true,
        max_trade_rupees=100000,max_daily_rupees=100000,max_risk_rupees=1000''')
    db_conn.commit()
    broker = FakeBroker(Decimal(str(result['reference'])))
    notices = []
    approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW,
            notify=notices.append)
    # Limit ₹100.33 and stop ₹99.32 (1% steps from the call's ₹101.35): the ₹1,000 loss cap
    # sizes it, 990 shares.
    assert db_conn.execute('select quantity,risk_rupees,est_charges from intraday_trade'
                           ).fetchone() == (990, Decimal('999.90'), Decimal('82.75'))
    assert 'Loss at the stop ₹999.90, estimated charges ₹82.75' in notices[-1]


class Filled(FakeBroker):
    """A GTT whose entry and target orders filled, as Upstox's GTT details show them."""

    def __init__(self, fills, exit_rule='TARGET'):
        super().__init__(Decimal('101.35'))
        self.fills = fills
        self.exit_rule = exit_rule
        self.asked = []

    def details(self, _):
        return {'rules': [
            {'strategy': 'ENTRY', 'status': 'COMPLETED', 'order_id': 'E1'},
            {'strategy': 'TARGET', 'status': 'COMPLETED' if self.exit_rule == 'TARGET'
             else 'CANCELLED', 'order_id': 'X1' if self.exit_rule == 'TARGET' else None},
            {'strategy': 'STOPLOSS', 'status': 'COMPLETED' if self.exit_rule == 'STOPLOSS'
             else 'CANCELLED', 'order_id': 'X1' if self.exit_rule == 'STOPLOSS' else None}]}

    def fill(self, order_id):
        self.asked.append(order_id)
        fill = self.fills[order_id]
        if isinstance(fill, Exception):
            raise fill
        return fill


def open_trade(conn, action='buy'):
    import datetime as dt
    cid = seed_stock(conn)
    run_id = add_scan(conn, cid, signal())
    conn.execute('''insert into intraday_trade(company_id,trading_day,action,scan_id,symbol,
        instrument_key,approved_by,approved_at,expires_at,quantity,entry_price,stop_price,
        target_price,notional,status,gtt_order_id) values(%s,%s,%s,%s,'TEST',
        'NSE_EQ|INE123456789','admin',%s,%s,98,101.35,100.9,102.25,9932.3,'entry_filled',
        'GTT-1')''', (cid, NOW.date(), action, run_id, NOW, NOW + dt.timedelta(minutes=10)))
    conn.commit()


PNL = 'select entry_fill,exit_fill,filled_quantity,gross_pnl,charges,net_pnl,pnl_note ' \
      'from intraday_trade'


@pytest.mark.db
def test_a_closed_trade_records_its_net_pnl_from_the_fills(db_conn):
    from igs.intraday.trading import reconcile

    open_trade(db_conn)
    broker = Filled({'E1': (Decimal('101.35'), 98), 'X1': (Decimal('102.25'), 98)})
    notices = []
    reconcile(db_conn, broker=broker, clock=lambda: NOW, notify=notices.append)
    # 98 x 0.90 = 88.20 before charges; 27.07 of charges (test_round_trip_charges_by_hand)
    assert db_conn.execute(PNL).fetchone() == (
        Decimal('101.35'), Decimal('102.25'), 98, Decimal('88.20'), Decimal('27.07'),
        Decimal('61.13'), None)
    assert 'net P&L ₹61.13 after estimated charges ₹27.07 (gross ₹88.20' in notices[-1]
    count = len(notices)
    reconcile(db_conn, broker=broker, clock=lambda: NOW, notify=notices.append)
    assert len(notices) == count and broker.asked == ['E1', 'X1']    # once


@pytest.mark.db
def test_a_short_stopped_out_loses_the_move_and_the_charges(db_conn):
    from igs.intraday.trading import reconcile

    open_trade(db_conn, 'sell')
    broker = Filled({'E1': (Decimal('101.35'), 98), 'X1': (Decimal('101.80'), 98)},
                    exit_rule='STOPLOSS')
    reconcile(db_conn, broker=broker, clock=lambda: NOW, notify=lambda _: None)
    gross, cost, net = db_conn.execute('select gross_pnl,charges,net_pnl '
                                       'from intraday_trade').fetchone()
    assert gross == Decimal('-44.10') and cost > 0 and net == gross - cost


@pytest.mark.db
def test_an_unreadable_fill_is_retried_and_unequal_fills_get_a_note(db_conn, monkeypatch):
    import datetime as dt

    from igs.intraday.trading import BrokerError, reconcile

    issues = []
    monkeypatch.setattr('igs.intraday.trading.record_issue',
                        lambda *args, **kwargs: issues.append(args))
    open_trade(db_conn)
    broker = Filled({'E1': (Decimal('101.35'), 98), 'X1': BrokerError('down')})
    reconcile(db_conn, broker=broker, clock=lambda: NOW, notify=lambda _: None)
    assert db_conn.execute(PNL).fetchone()[5] is None
    assert ('intraday-trade', 'PnlUnavailable') in issues
    broker.fills['X1'] = (Decimal('102.25'), 60)          # partly filled exit
    reconcile(db_conn, broker=broker, clock=lambda: NOW + dt.timedelta(minutes=1),
              notify=lambda _: None)
    assert db_conn.execute(PNL).fetchone()[5] is None      # not retried within 5 minutes
    reconcile(db_conn, broker=broker, clock=lambda: NOW + dt.timedelta(minutes=6),
              notify=lambda _: None)
    row = db_conn.execute(PNL).fetchone()
    assert row[5] is None and row[6] == 'Entry filled 98 shares and the exit 60; see Upstox'
    assert ('intraday-trade', 'PnlUnmatched') in issues


@pytest.mark.db
def test_the_page_shows_net_pnl_and_the_paper_record_in_rupees(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest

    from igs.intraday.trading import reconcile
    from igs.timeutil import IST, utc_now

    open_trade(db_conn)
    reconcile(db_conn, broker=Filled({'E1': (Decimal('101.35'), 98),
                                      'X1': (Decimal('102.25'), 98)}),
              clock=lambda: NOW, notify=lambda _: None)
    cid = db_conn.execute('select company_id from intraday_trade').fetchone()[0]
    today = utc_now().astimezone(IST)
    db_conn.execute('''insert into intraday_call_outcome(company_id,candle_end,trading_day,
        symbol,action,rvol,close_location,reference,stop,target,outcome,filled_at,exit_at,
        exit_price,r_multiple) values(%s,%s,%s,'TEST','buy',60,.9,100,99,102,'target',%s,%s,
        102,2)''', (cid, today, today.date(), today, today))
    db_conn.commit()
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at = AppTest.from_string('''import datetime as dt
from igs.db import connect
from igs.ui.intraday import paper_record, pnl_totals
conn = connect(autocommit=True)
pnl_totals(conn, dt.date(2026, 10, 5))
paper_record(conn)''', default_timeout=30).run()
    assert not at.exception
    assert at.metric[0].value == '₹61.13'
    summary, latest = (d.value for d in at.dataframe)
    assert summary['Net ₹'][0] == pytest.approx(172.58)     # test_intraday_outcomes
    assert latest['Net ₹'][0] == pytest.approx(172.58)


def test_a_call_is_priced_as_the_order_the_settings_would_place():
    """trading.order_check: every order is 1% away with its stop 1% beyond and the call's
    target; an approved call is held to the 1.5x minimum, an automatic one to 2.5x."""
    from igs.intraday.trading import order_check

    result = {'action': 'buy', 'reference': 100.0, 'stop': 99.0, 'target': 102.0,
              'candle_volume': 1_000_020, 'baseline_volume': 20_000}
    cfg = {**settings('100000', '1000'), 'enabled': True, 'auto_high_volume_enabled': False,
           'auto_min_net_reward_risk': Decimal('2.5')}
    order = order_check(result, 'NSE_EQ|TEST', Decimal('0.01'), cfg, intraday_rates())
    assert order['ok'] and not order['automatic']
    assert (order['quantity'], order['entry'], order['stop'], order['target']) == (
        1010, 99.0, 98.01, 102.0)
    assert order['net_gain'] < 3030 and order['net_loss'] > 999.9         # charges
    assert order['reward_risk'] == round(order['net_gain'] / order['net_loss'], 2)
    auto = order_check(result, 'NSE_EQ|TEST', Decimal('0.01'),
                       {**cfg, 'auto_high_volume_enabled': True}, intraday_rates())
    # The owner's example: 99.00 / 98.01 / 102, 2.72x after charges at Rs 1,00,000.
    assert auto['automatic'] and (auto['entry'], auto['stop'], auto['target']) == (
        99.0, 98.01, 102.0) and auto['reward_risk'] == 2.72
    small = order_check(result, 'NSE_EQ|TEST', Decimal('0.01'),
                        {**cfg, **settings('10000', '100'), 'auto_high_volume_enabled': True},
                        intraday_rates())
    assert not small['ok'] and small['automatic'] and 'minimum 2.5×' in small['reason']
    # The same 2.17x order at Rs 10,000 clears an approved call's 1.5x.
    approved = order_check(result, 'NSE_EQ|TEST', Decimal('0.01'),
                           {**cfg, **settings('10000', '100')}, intraday_rates())
    assert approved['ok'] and not approved['automatic'] and approved['reward_risk'] == 2.17


def test_the_call_message_shows_the_order_at_the_settings():
    from igs.alerts.intraday import message

    text = message('TEST', {**signal(), 'order': {
        'automatic': False, 'quantity': 986, 'entry': 101.35, 'stop': 100.9,
        'target': 102.25, 'net_gain': 804.73, 'net_loss': 525.0, 'reward_risk': 1.53}})
    assert 'Your order: 986 shares · limit ₹101.35 · stop ₹100.90 · target ₹102.25' in text
    assert '₹804.73 at the target, −₹525.00 at the stop (1.53×)' in text
