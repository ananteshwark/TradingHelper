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
    assert db_conn.execute('select quantity,risk_rupees,est_charges from intraday_trade'
                           ).fetchone() == (986, Decimal('443.70'), Decimal('82.67'))
    assert 'Loss at the stop ₹443.70, estimated charges ₹82.67' in notices[-1]
