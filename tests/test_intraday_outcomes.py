"""Paper record of intraday calls: replaying a call on its session's candles."""

import datetime as dt
from dataclasses import asdict

import pytest
from psycopg.types.json import Jsonb
from test_intraday import seed_stock

from igs.intraday.engine import BAR, Candle
from igs.intraday.outcomes import close_location, record_outcomes, resolve, summary
from igs.timeutil import IST

DAY = dt.date(2026, 10, 5)
END = dt.datetime(2026, 10, 5, 10, 0, tzinfo=IST)        # the call's candle closed at 10:00


def call(action='buy', **extra):
    sign = 1 if action == 'buy' else -1
    return {'action': action, 'candle_end': END.isoformat(),
            'expires_at': (END + 2 * BAR).isoformat(), 'reference': 100.0,
            'stop': 100.0 - sign * 1.0, 'target': 100.0 + sign * 2.0, **extra}


def bar(hhmm, low, high, close=None):
    start = dt.datetime.combine(DAY, dt.time(*hhmm), IST)
    close = (low + high) / 2 if close is None else close
    return Candle(start, close, high, low, close, 1000)


def test_target_before_stop_is_two_r():
    got = resolve(call(), [bar((10, 0), 99.95, 100.4), bar((10, 5), 99.5, 102.1)],
                  complete=True)
    assert got['outcome'] == 'target' and got['r_multiple'] == 2
    assert got['filled_at'] == END and got['exit_at'] == END + 2 * BAR


def test_stop_and_both_in_one_candle_count_as_the_stop():
    stop = resolve(call(), [bar((10, 0), 99.9, 100.3), bar((10, 5), 98.9, 100.2)],
                   complete=True)
    assert stop['outcome'] == 'stop' and stop['r_multiple'] == -1
    both = resolve(call(), [bar((10, 0), 99.9, 100.3), bar((10, 5), 98.5, 102.5)],
                   complete=True)
    assert both['outcome'] == 'stop'
    in_fill = resolve(call(), [bar((10, 0), 98.8, 102.4)], complete=True)
    assert in_fill['outcome'] == 'stop'          # order inside the fill candle unknown


def test_an_entry_not_reached_before_expiry_never_filled():
    never = [bar((10, 0), 100.2, 100.8), bar((10, 5), 100.3, 101), bar((10, 10), 99, 100)]
    assert resolve(call(), never, complete=True) == {'outcome': 'not_filled'}
    # While candles stop short of the expiry, it is not decided yet.
    assert resolve(call(), never[:1], complete=False) is None


def test_no_stop_or_target_exits_at_the_last_close_before_15_15():
    candles = [bar((10, 0), 99.9, 100.5), bar((12, 0), 99.5, 101.5),
               bar((15, 10), 100.4, 100.8, close=100.6), bar((15, 15), 90, 110)]
    got = resolve(call(), candles, complete=True)
    assert got['outcome'] == 'time_exit' and got['exit_price'] == 100.6
    assert got['r_multiple'] == pytest.approx(0.6)
    assert resolve(call(), candles[:2], complete=False) is None     # session not over


def test_a_sell_mirrors_a_buy():
    got = resolve(call('sell'), [bar((10, 0), 99.8, 100.1), bar((10, 5), 97.9, 100.5)],
                  complete=True)
    assert got['outcome'] == 'target' and got['r_multiple'] == 2


def test_close_location_is_measured_toward_the_trade():
    candle = {'candle_high': 101.0, 'candle_low': 99.0, 'reference': 100.6}
    assert close_location(call(**candle)) == pytest.approx(0.8)
    assert close_location(call('sell', **candle)) == pytest.approx(0.2)
    assert close_location(call()) is None                      # older calls: unknown


@pytest.mark.db
def test_calls_are_recorded_once_the_next_days_history_has_their_session(db_conn):
    cid = seed_stock(db_conn)
    scan = db_conn.execute("insert into intraday_scan(status) values('complete') "
                           "returning scan_id").fetchone()[0]
    result = call(rvol=60.0, candle_high=100.1, candle_low=99.0)
    db_conn.execute('''insert into intraday_signal values(%s,%s,'TEST','NSE_EQ|INE123456789',
        %s,%s)''', (scan, cid, END + dt.timedelta(seconds=20), Jsonb(result)))
    db_conn.commit()
    next_day = dt.datetime(2026, 10, 6, 9, 35, tzinfo=IST)
    assert record_outcomes(db_conn, next_day) == 0              # no later history yet
    session = [bar((10, 0), 99.95, 100.4), bar((10, 5), 99.5, 102.1)]
    db_conn.execute('''insert into intraday_history(instrument_key,session_date,candles)
        values('NSE_EQ|INE123456789',%s,%s)''',
        (next_day.date(), Jsonb([{**asdict(c), 'start': c.start.isoformat()}
                                 for c in session])))
    db_conn.commit()
    assert record_outcomes(db_conn, next_day) == 1
    assert record_outcomes(db_conn, next_day) == 0              # once
    row = db_conn.execute('select outcome,r_multiple,rvol,round(close_location,2) '
                          'from intraday_call_outcome').fetchone()
    assert row[0] == 'target' and float(row[1]) == 2 and float(row[2]) == 60
    assert float(row[3]) == pytest.approx(0.91)               # (100 - 99) / (100.1 - 99)
    groups = {g['Group']: g for g in summary(db_conn, DAY)}
    assert groups['All calls']['Calls'] == 1 and groups['All calls']['Average R'] == 2
    assert groups['Volume jump 50× and over']['Target first'] == 1
    assert groups['Closed in the top 30% of its candle']['Win rate'] == 1
    assert groups['Volume jump 1.8–5×']['Calls'] == 0
