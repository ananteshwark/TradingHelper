"""Intraday prices in whole exchange ticks. NSE ticks run from Rs 0.01 to Rs 5 by price
band; Upstox refuses an order price that is not a whole number of ticks."""

import datetime as dt
from decimal import Decimal

import pytest
from test_intraday import NOW, sample, seed_stock
from test_intraday_telegram import add_scan
from test_intraday_trading import FakeBroker

from igs.intraday.engine import Candle, evaluate, to_tick
from igs.intraday.trading import _plan, approve

CFG = {'max_trade_rupees': Decimal('10000'), 'max_price_deviation_pct': Decimal('.5'),
       'max_risk_rupees': Decimal('1000'), 'min_net_reward_risk': Decimal('1.5')}


def whole(price, tick):
    return (Decimal(str(price)) / Decimal(tick)) % 1 == 0


def test_levels_are_whole_ticks_stop_away_from_entry_target_toward_it():
    bars, history = sample()
    plain = evaluate(bars, history, NOW, bars)
    buy = evaluate(bars, history, NOW, bars, tick=Decimal('0.10'))
    assert (plain['stop'], plain['target']) == (100.9, 102.25)
    assert (buy['stop'], buy['target'], buy['tick']) == (100.9, 102.2, 0.1)
    down = [Candle(b.start, 200-b.open, 200-b.low, 200-b.high, 200-b.close, b.volume)
            for b in bars]
    sell = evaluate(down, history, NOW, down, tick=Decimal('0.10'))
    assert sell['action'] == 'sell'
    assert (sell['stop'], sell['target']) == (99.1, 97.8)        # 99.10 and 97.75 up
    for call in (buy, sell):
        assert whole(call['stop'], '0.10') and whole(call['target'], '0.10')


def test_a_tick_wider_than_the_setup_withholds_the_call():
    bars, history = sample()
    result = evaluate(bars, history, NOW, bars, tick=Decimal('5'))
    assert result['action'] == 'wait'
    assert result['reason'] == 'Stop and target collapse at the exchange tick'


def test_float_noise_cannot_add_a_tick():
    assert to_tick(101.35000000000001, '0.05', -1) == Decimal('101.35')
    assert to_tick(101.36, '0.05', -1) == Decimal('101.40')
    assert to_tick(101.36, '0.05', 1) == Decimal('101.35')


@pytest.mark.parametrize('action', ['buy', 'sell'])
def test_order_prices_are_rounded_even_for_calls_stored_before_ticks(
        action, zero_intraday_charges):
    """Calls stored before this change carry paisa levels (1229.61 at a Rs 0.05 tick)."""
    sign = 1 if action == 'buy' else -1
    call = {'action': action, 'reference': 1234.57, 'stop': 1234.57 - sign * 4.96,
            'target': 1234.57 + sign * 9.86}
    quantity, entry, stop, target, payload, *_ = _plan(
        {'instrument_key': 'NSE_EQ|TEST', 'result': call}, Decimal('1234.6'), CFG,
        Decimal('0.05'), zero_intraday_charges)
    assert all(whole(r['trigger_price'], '0.05') for r in payload['rules'])
    if action == 'buy':      # pay no more than the call, stop lower, target nearer
        assert (entry, stop, target) == (Decimal('1234.55'), Decimal('1229.60'),
                                         Decimal('1244.40'))
    else:
        assert (entry, stop, target) == (Decimal('1234.60'), Decimal('1239.55'),
                                         Decimal('1224.75'))
    assert quantity == int(Decimal('10000') // entry)


@pytest.mark.db
def test_approval_prices_the_order_at_the_instruments_own_tick(db_conn, monkeypatch,
                                                              zero_intraday_charges):
    monkeypatch.setattr('igs.intraday.eligibility.tick_sizes',
                        lambda **kwargs: {'NSE_EQ|INE123456789': Decimal('0.10')})
    cid = seed_stock(db_conn)
    bars, history = sample()
    call = evaluate(bars, history, NOW, bars)                 # paisa levels: 102.25
    add_scan(db_conn, cid, call)
    db_conn.execute('update intraday_trading_settings set enabled=true')
    db_conn.commit()
    broker = FakeBroker(Decimal('101.35'))
    approve(db_conn, cid, source='admin', broker=broker, clock=lambda: NOW + dt.timedelta(
        seconds=10), notify=lambda _: None)
    [order] = broker.orders
    # On the 10-paisa tick the call's 101.35 is 101.3; the limit 1% below, 100.287, rounds
    # down to 100.2, the stop 1% below that, 99.198, down to 99.1; the target 102.25 to 102.2.
    assert [r['trigger_price'] for r in order['rules']] == [100.2, 102.2, 99.1]
