"""Real orders for the evening calls: approval the evening before (approve_evening), the
order after the next open (place_evening), and the switch that keeps them off."""
import datetime as dt
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb
from test_intraday import seed_stock
from test_ml_orders import KEY, Broker

from igs.intraday.engine import Candle
from igs.intraday.telegram_approvals import poll
from igs.intraday.trading import (
    TradeError,
    approve_evening,
    evening_window,
    place_evening,
    reconcile,
)
from igs.timeutil import IST

FRI, MON = dt.date(2026, 10, 2), dt.date(2026, 10, 5)


def at(day, hh, mm):
    return dt.datetime.combine(day, dt.time(hh, mm), IST)


def test_approvals_run_from_the_evening_to_0915_on_the_next_weekday():
    assert not evening_window(at(FRI, 15, 30), FRI)        # before the decision
    assert evening_window(at(FRI, 18, 0), FRI)
    assert evening_window(at(dt.date(2026, 10, 3), 11, 0), FRI)   # the weekend
    assert evening_window(at(MON, 9, 14), FRI)
    assert not evening_window(at(MON, 9, 15), FRI)
    assert not evening_window(at(dt.date(2026, 10, 6), 8, 0), FRI)  # a later day: lapsed


def seed(conn, *, side=-1, close=100.0, orders=True, deviation=1.0):
    cid = seed_stock(conn)
    conn.execute("""insert into evening_day (signal_date, decided_at, model, universe, scored)
                    values (%s, %s, 'intraday-evening-v1', 200, 150)""", (FRI, at(FRI, 15, 46)))
    conn.execute("""insert into evening_pick (signal_date, instrument_key, company_id, symbol,
                        side, prediction, rank, features, status, decision_price)
                    values (%s, %s, %s, 'TEST', %s, %s, 1, %s, 'open', %s)""",
                 (FRI, KEY, cid, side, 0.004 * side, Jsonb({'atr_pct': 0.02}), close))
    conn.execute("""update intraday_trading_settings set enabled=true,
                        evening_orders_enabled=%s, max_trade_rupees=100000,
                        max_risk_rupees=1000, max_daily_rupees=300000,
                        max_price_deviation_pct=%s""", (orders, deviation))
    conn.commit()


class Feed:
    """The Nifty 50's candles today: the 09:15 one once the session has opened."""

    def __init__(self, opened=True):
        self.opened = opened

    def candles(self, _key):
        if not self.opened:
            return []
        return [Candle(at(MON, 9, 15), 100, 101, 99, 100.5, 0)]

    def close(self):
        pass


@pytest.mark.db
@pytest.mark.usefixtures("intraday_eligibility", "zero_intraday_charges")
def test_an_approved_call_is_placed_once_after_the_open(db_conn):
    seed(db_conn)
    text = approve_evening(db_conn, KEY, source='admin', clock=lambda: at(FRI, 18, 0))
    assert 'SELL SHORT TEST' in text and 'Mon 05 Oct' in text
    with pytest.raises(TradeError, match='already approved'):
        approve_evening(db_conn, KEY, source='admin', clock=lambda: at(FRI, 18, 5))
    broker, sent = Broker(100.4), []
    # Not before 09:20, nor before the market shows it has opened.
    assert place_evening(db_conn, broker=broker, feed=Feed(), clock=lambda: at(MON, 9, 16),
                         notify=sent.append) == 0
    assert place_evening(db_conn, broker=broker, feed=Feed(opened=False),
                         clock=lambda: at(MON, 9, 21), notify=sent.append) == 0
    assert place_evening(db_conn, broker=broker, feed=Feed(), clock=lambda: at(MON, 9, 21),
                         notify=sent.append) == 1
    assert len(broker.orders) == 1 and broker.orders[0]['transaction_type'] == 'SELL'
    row = db_conn.execute("""select kind, approved_by, status, stop_price from intraday_trade"""
                          ).fetchone()
    assert row[:3] == ('evening', 'admin', 'submitted') and row[3] > Decimal('100.4')
    assert db_conn.execute('select trade_id is not null, order_note from evening_pick'
                           ).fetchone() == (True, 'submitted')
    assert 'Evening SELL TEST' in sent[-1] and 'squares it off at 15:00' in sent[-1]
    # Attempted once: a later run places nothing more.
    assert place_evening(db_conn, broker=broker, feed=Feed(), clock=lambda: at(MON, 9, 25),
                         notify=sent.append) == 0
    assert len(broker.orders) == 1


@pytest.mark.db
@pytest.mark.usefixtures("intraday_eligibility", "zero_intraday_charges")
def test_a_gap_beyond_the_deviation_limit_is_skipped_and_reported(db_conn):
    seed(db_conn, deviation=1.0)
    approve_evening(db_conn, KEY, source='admin', clock=lambda: at(FRI, 18, 0))
    broker, sent = Broker(102), []                     # opened 2% above the close
    assert place_evening(db_conn, broker=broker, feed=Feed(), clock=lambda: at(MON, 9, 21),
                         notify=sent.append) == 0
    assert broker.orders == []
    note = db_conn.execute('select order_note from evening_pick').fetchone()[0]
    assert 'moved +2.00%' in note and 'no order placed' in sent[-1]


@pytest.mark.db
@pytest.mark.usefixtures("intraday_eligibility", "zero_intraday_charges")
def test_approvals_are_refused_when_off_or_late(db_conn):
    seed(db_conn, orders=False)
    with pytest.raises(TradeError, match='evening calls are off'):
        approve_evening(db_conn, KEY, source='admin', clock=lambda: at(FRI, 18, 0))
    db_conn.execute('update intraday_trading_settings set evening_orders_enabled=true')
    db_conn.commit()
    with pytest.raises(TradeError, match='only until 09:15'):
        approve_evening(db_conn, KEY, source='admin', clock=lambda: at(MON, 9, 16))
    # Switched off after approval: the approved call is not placed.
    approve_evening(db_conn, KEY, source='admin', clock=lambda: at(FRI, 18, 0))
    db_conn.execute('update intraday_trading_settings set evening_orders_enabled=false')
    db_conn.commit()
    broker = Broker(100)
    assert place_evening(db_conn, broker=broker, feed=Feed(), clock=lambda: at(MON, 9, 21),
                         notify=lambda _: None) == 0
    assert broker.orders == []
    assert 'off in settings' in db_conn.execute('select order_note from evening_pick'
                                                ).fetchone()[0]


@pytest.mark.db
@pytest.mark.usefixtures("intraday_eligibility", "zero_intraday_charges")
def test_a_telegram_reply_records_the_approval_once_and_orders_nothing_yet(db_conn,
                                                                         monkeypatch):
    seed(db_conn)
    db_conn.execute('update evening_pick set telegram_message_id=88')
    db_conn.commit()
    monkeypatch.setenv('IGS_TELEGRAM_TOKEN', 'fake')
    monkeypatch.setenv('IGS_TELEGRAM_CHAT_ID', '123')
    me = {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123}}
    messages = [{'update_id': u, 'message': {'text': 'Approved', **me,
                                             'reply_to_message': {'message_id': 88}}}
                for u in (1, 2)]

    def telegram(method, _token, _client, **fields):
        if method == 'getWebhookInfo':
            return {'result': {'url': ''}}
        return {'result': [m for m in messages if m['update_id'] >= int(fields['offset'])]}

    monkeypatch.setattr('igs.intraday.telegram_approvals._telegram', telegram)
    broker, said = Broker(100), []
    assert poll(db_conn, client=object(), broker=broker, clock=lambda: at(FRI, 19, 0),
                notify=said.append) == 1
    assert broker.orders == []
    assert db_conn.execute('select approved_by, approval_update_id from evening_pick'
                           ).fetchone() == ('telegram', 1)
    assert 'Evening call approved' in said[0] and 'already approved' in said[1]


@pytest.mark.db
@pytest.mark.usefixtures("intraday_eligibility", "zero_intraday_charges")
def test_a_filled_evening_trade_is_squared_off_at_1500(db_conn):
    seed(db_conn)
    approve_evening(db_conn, KEY, source='admin', clock=lambda: at(FRI, 18, 0))
    broker = Broker(100)
    place_evening(db_conn, broker=broker, feed=Feed(), clock=lambda: at(MON, 9, 21),
                  notify=lambda _: None)
    broker.entry, broker.states['E1'] = 'COMPLETED', ('complete', 909)
    reconcile(db_conn, broker=broker, clock=lambda: at(MON, 14, 59), notify=lambda _: None)
    assert broker.exits == []
    reconcile(db_conn, broker=broker, clock=lambda: at(MON, 15, 1), notify=lambda _: None)
    assert broker.cancelled == ['GTT-9'] and broker.exits == [(KEY, 909, 'sell')]
    assert db_conn.execute('select status from intraday_trade').fetchone()[0] == 'squaring_off'
