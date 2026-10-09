"""Machine-learning intraday paper calls (igs.intraday.ml)."""
import datetime as dt
import math
import os

import pytest

from igs.intraday import ml
from igs.intraday.costs import charges, intraday_rates
from igs.intraday.engine import Candle
from igs.intraday.upstox import FeedError
from igs.timeutil import IST

DAY = dt.date(2026, 10, 9)          # a Friday


def at(day, hh, mm):
    return dt.datetime.combine(day, dt.time(hh, mm), IST)


def bar(day, k, o, c, v, h=None, lo=None):
    """The k-th five-minute candle from 09:15."""
    start = at(day, 9, 15) + dt.timedelta(minutes=5 * k)
    return Candle(start, o, h if h is not None else max(o, c) + 0.1,
                  lo if lo is not None else min(o, c) - 0.1, c, v)


def prior():
    """22 sessions with closes 100..121: highs and lows a rupee either side, opens 0.5 below."""
    return [{'open': 100 + i - 0.5, 'high': 101 + i, 'low': 99 + i, 'close': 100 + i,
             'value': 1e9, 'last_hour': 0.01, 'v30': 1000.0} for i in range(22)]


def test_features_follow_the_trained_definitions():
    today = [bar(DAY, k, 122 + 0.5 * k, 122.5 + 0.5 * k, 200, h=123 + 0.5 * k, lo=121.5 + 0.5 * k)
             for k in range(6)]
    index = {'n_gap': 0.001, 'n_r30': 0.002, 'n_prev_r1': 0.0, 'n_r5': 0.01}
    f = ml.stock_features(prior(), today, index, DAY)
    assert f['gap'] == pytest.approx(122 / 121 - 1)
    assert f['r30'] == pytest.approx(125 / 122 - 1)
    assert f['rel30'] == pytest.approx(125 / 122 - 1 - 0.002)
    assert f['prev_r1'] == pytest.approx(121 / 120 - 1)
    assert f['r5'] == pytest.approx(121 / 116 - 1)
    assert f['r21'] == pytest.approx(121 / 100 - 1)
    assert f['range30_atr'] == pytest.approx(4 / 2)          # every true range is 2
    assert f['atr_pct'] == pytest.approx(2 / 121)
    assert f['rvol30'] == pytest.approx(1200 / 1000)
    assert f['clv30'] == pytest.approx((125 - 121.5) / 4)
    assert f['vwap_dist'] == pytest.approx(125 / (123.25 + 1 / 3) - 1)
    assert f['prev_clv'] == pytest.approx(0.5)
    assert f['prev_intraday'] == pytest.approx(121 / 120.5 - 1)
    assert f['dist20h'] == pytest.approx(121 / 122 - 1)
    assert f['dist20l'] == pytest.approx(121 / 101 - 1)
    assert f['ovn5'] == pytest.approx(sum(math.log((c - 0.5) / (c - 1)) for c in range(117, 122)))
    assert f['intra5'] == pytest.approx(sum(math.log(c / (c - 0.5)) for c in range(117, 122)))
    assert f['log_value20'] == pytest.approx(math.log(1e9))
    assert (f['dow_4'], f['dow_0']) == (1.0, 0.0)
    assert set(ml.model().features) <= set(f)
    # Not before all six candles are in, nor without 22 sessions.
    assert ml.stock_features(prior(), today[:5], index, DAY) is None
    assert ml.stock_features(prior()[1:], today, index, DAY) is None


def test_the_model_sums_its_trees_and_the_calls_clear_the_gate():
    m = ml.Model({'name': 't', 'features': ['a', 'b'],
                  'trees': [[0, 0.5, 0.001, [1, 0.0, -0.002, 0.003]], 0.0005]})
    assert m.predict({'a': 0.2, 'b': 9}) == pytest.approx(0.0015)
    assert m.predict({'a': 0.9, 'b': -1}) == pytest.approx(-0.0015)
    assert m.predict({'a': 0.9, 'b': 1}) == pytest.approx(0.0035)
    preds = [('A', 0.004), ('B', 0.002), ('C', 0.0016), ('D', 0.0015), ('E', 0.003),
             ('F', -0.001), ('G', -0.005), ('H', -0.0016)]
    assert ml.choose(preds) == [('A', 1, 0.004), ('E', 1, 0.003), ('B', 1, 0.002),
                                ('G', -1, -0.005), ('H', -1, -0.0016)]
    # The shipped model loads and predicts.
    assert math.isfinite(ml.model().predict({f: 0.0 for f in ml.model().features}))


def test_results_after_slippage_and_charges_both_ways():
    rates = intraday_rates()
    buy = ml.result(1, 100.0, 101.0, rates)
    e, x = 100 * 1.0002, 101 * 0.9998
    cost = float(charges(999, round(e, 2), round(x, 2), 'buy', rates))
    assert buy['quantity'] == 999
    assert buy['net_inr'] == pytest.approx((x - e) * 999 - cost, abs=0.01)
    short = ml.result(-1, 100.0, 99.0, rates)
    assert short['net_inr'] > 0
    assert short['gross_inr'] == pytest.approx((100 * 0.9998 - 99 * 1.0002) * 1000, abs=0.01)
    assert ml.result(1, 150000.0, 151000.0, rates) is None      # above Rs 1 lakh a call


class Feed:
    """Full flat sessions before today; today, A rises 0.2 a candle, B falls 0.1, C is flat."""

    def __init__(self):
        self.now = at(DAY, 9, 0)
        self.requests = 0

    def close(self):
        pass

    def _day(self, key, day, upto=None):
        out = []
        for k in range(75):
            start = at(day, 9, 15) + dt.timedelta(minutes=5 * k)
            if upto is not None and start > upto:
                break
            if day == DAY and key == 'NSE_EQ|INEA':
                o = 100 + 0.2 * k
                out.append(Candle(start, o, o + 0.3, o - 0.1, o + 0.2, 1e5))
            elif day == DAY and key == 'NSE_EQ|INEB':
                o = 100 - 0.1 * k
                out.append(Candle(start, o, o + 0.1, o - 0.2, o - 0.1, 1e5))
            else:
                out.append(Candle(start, 100, 100.1, 99.9, 100, 0 if 'INDEX' in key else 1e5))
        return out

    def candles_between(self, key, start, end):
        self.requests += 1
        days, d = [], start
        while d <= end:
            if d.weekday() < 5:
                days += self._day(key, d)
            d += dt.timedelta(days=1)
        return days

    def candles(self, key):
        self.requests += 1
        if key == 'NSE_EQ|INED':
            raise FeedError('Upstox returned invalid candle data')
        return self._day(key, DAY, upto=self.now - dt.timedelta(minutes=5))


class R30Model:
    name = 'test-r30'
    features = ['r30']

    def predict(self, row):
        return row['r30']


@pytest.mark.db
def test_prepare_decide_and_settle_one_day(db_conn, monkeypatch):
    for sym in 'ABCD':            # D's candles fail today: skipped
        cid = db_conn.execute("insert into company(name) values(%s) returning company_id",
                              (f'{sym} Ltd',)).fetchone()[0]
        sid = db_conn.execute('insert into security(company_id) values(%s) returning security_id',
                              (cid,)).fetchone()[0]
        for kind, value in (('NSE_SYMBOL', sym), ('ISIN', f'INE{sym}')):
            db_conn.execute('''insert into security_identifier(security_id,id_type,id_value,
                valid_from,evidence) values(%s,%s,%s,'2020-01-01','test')''', (sid, kind, value))
        db_conn.execute("insert into index_member(index_name, as_of, company_id) "
                        "values('NIFTY 200', '2026-10-01', %s)", (cid,))
    db_conn.commit()
    monkeypatch.setattr(ml, 'model', lambda: R30Model())
    feed, sent = Feed(), []

    def run(hh, mm, ss=0):
        feed.now = at(DAY, hh, mm) + dt.timedelta(seconds=ss)
        return ml.step(db_conn, feed, clock=lambda: feed.now, pause=lambda _: None,
                       notify=sent.append)

    assert 'prepared' in run(9, 0, 20)
    stored = db_conn.execute('select count(distinct instrument_key), min(session_date), '
                             'max(session_date) from ml_intraday_session').fetchone()
    assert stored[0] == 5 and stored[2] == DAY - dt.timedelta(days=1)
    before = feed.requests
    run(9, 5, 20)                      # already prepared: only the index is checked
    assert feed.requests - before <= 1

    assert "'picks': 2" in run(9, 46, 30)
    rows = db_conn.execute('select symbol, side, status, entry_after from ml_intraday_pick '
                           'order by rank').fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [('A', 1, 'open'), ('B', -1, 'open')]
    assert 'BUY A' in sent[0] and 'SELL short B' in sent[0]
    assert "'status': 'done'" in run(9, 50, 20)          # once a day
    assert 'nothing due' in run(12, 0, 20)

    run(15, 20, 20)
    rates = intraday_rates()
    got = {r[0]: r[1:] for r in db_conn.execute(
        "select symbol, status, entry::float8, exit::float8, net_inr::float8 "
        "from ml_intraday_pick").fetchall()}
    # Decided at 09:46:30: entry at the 09:50 candle's open (k = 7), exit at 15:15's (k = 72).
    assert got['A'][:3] == ('closed', pytest.approx(101.4), pytest.approx(114.4))
    assert got['A'][3] == pytest.approx(ml.result(1, 101.4, 114.4, rates)['net_inr'])
    assert got['B'][:3] == ('closed', pytest.approx(99.3), pytest.approx(92.8))
    assert got['B'][3] == pytest.approx(ml.result(-1, 99.3, 92.8, rates)['net_inr'])
    assert 'ML intraday paper results' in sent[-1] and 'Since the start' in sent[-1]
    rec = ml.record(db_conn)['total']
    assert rec['trades'] == 2 and rec['net_inr'] == pytest.approx(got['A'][3] + got['B'][3])


@pytest.mark.db
def test_the_page_shows_today_and_the_record(db_conn, monkeypatch):
    from streamlit.testing.v1 import AppTest
    db_conn.execute("""insert into ml_intraday_day (session_date, decided_at, model, universe,
                           scored) values ('2026-10-08', '2026-10-08 04:17:00+00',
                                           'intraday-ml-v1', 197, 150)""")
    db_conn.execute("""insert into ml_intraday_pick (session_date, instrument_key, symbol, side,
                           prediction, rank, features, entry_after, entry, exit, quantity,
                           gross_inr, charges_inr, net_inr, net_pct, status)
                       values ('2026-10-08', 'NSE_EQ|X', 'XYZ', -1, -0.004, 1, '{}',
                               '2026-10-08 04:17:00+00', 100, 99, 1000, 1000, 60, 940, 0.94,
                               'closed')""")
    db_conn.commit()
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at_ = AppTest.from_string('''from igs.db import connect
from igs.ui.intraday_ml import page
page(connect(autocommit=True))''', default_timeout=30).run()
    assert not at_.exception, at_.exception
    assert any('stock' in df.value.columns and 'XYZ' in list(df.value['stock'])
               for df in at_.dataframe)
    assert [m.value for m in at_.metric][:2] == ['1', '₹+940']
    assert any('1 of about 250 calls' in c.value for c in at_.caption)
