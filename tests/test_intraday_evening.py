"""Evening intraday paper calls (igs.intraday.evening)."""
import datetime as dt
import importlib.util
import math
from pathlib import Path

import pytest

from igs.intraday import evening, ml
from igs.intraday.costs import intraday_rates
from igs.intraday.engine import Candle
from igs.timeutil import IST

DAY = dt.date(2026, 10, 9)          # a Friday: the signal day
NEXT = dt.date(2026, 10, 12)        # the Monday it trades
ROOT = Path(__file__).resolve().parents[1]


def at(day, hh, mm):
    return dt.datetime.combine(day, dt.time(hh, mm), IST)


def research():
    spec = importlib.util.spec_from_file_location(
        'evening_features', ROOT / 'research' / 'evening' / 'features.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def session(day, seed):
    """75 candles with a deterministic wiggle, so every feature is non-trivial."""
    out, price = [], 100 + seed % 7
    for k in range(75):
        o = price
        c = o * (1 + 0.002 * math.sin(seed * 1.7 + k * 0.9))
        out.append(Candle(at(day, 9, 15) + dt.timedelta(minutes=5 * k), o,
                          max(o, c) * 1.001, min(o, c) * 0.999, c,
                          1e4 * (1 + (seed * 31 + k * 7) % 13)))
        price = c
    return out


def test_features_match_the_research_definitions():
    days, d, n = {}, dt.date(2026, 8, 3), 0
    while len(days) < 24:
        if d.weekday() < 5:
            days[d] = session(d, n)
            n += 1
        d += dt.timedelta(days=1)
    index = {x: session(x, 1000 + i) for i, x in enumerate(days)}
    mod = research()

    def tuples(bars):
        return [(b.start.astimezone(IST).time(), b.open, b.high, b.low, b.close, b.volume)
                for b in bars]
    nifty_rows = mod.features({x: tuples(b) for x, b in index.items()})
    ref = mod.features({x: tuples(b) for x, b in days.items()}, nifty_rows)
    dates = sorted(days)
    target = dates[-1]
    prev = dates[-23:-1]
    got = evening.stock_features(
        [evening.summary(days[x]) for x in prev],
        evening.index_features([evening.summary(index[x]) for x in prev]), target)
    want = ref[target]
    for f in evening.model().features:
        if f.startswith('dow_'):
            continue
        assert got[f] == pytest.approx(want[f], rel=1e-12, abs=1e-15), f
    assert got[f'dow_{target.weekday()}'] == 1.0
    assert got['value20'] == pytest.approx(want['value20'])
    # Not without 22 sessions, nor with a missing last-30-minutes volume.
    assert evening.stock_features([evening.summary(days[x]) for x in prev[1:]],
                                  {'n_prev_r1': 0}, target) is None


def test_the_shipped_ensemble_averages_its_members():
    m = evening.Ensemble({'name': 't', 'features': ['a'],
                          'ensemble': [[[0, 0.5, 0.001, 0.003]], [[0, 0.5, -0.001, 0.005]]]})
    assert m.predict({'a': 0.2}) == pytest.approx(0.0)
    assert m.predict({'a': 0.9}) == pytest.approx(0.004)
    shipped = evening.model()
    assert len(shipped.members) == 5 and shipped.name == 'intraday-evening-v1'
    assert math.isfinite(shipped.predict({f: 0.0 for f in shipped.features}))


def test_next_weekday_skips_the_weekend():
    assert evening.next_weekday(DAY) == NEXT
    assert evening.next_weekday(dt.date(2026, 10, 12)) == dt.date(2026, 10, 13)


class Feed:
    """Flat sessions; on the signal day A rises and B falls; on the next session A rises 0.1
    a candle from 110 and B falls 0.05 from 95."""

    def __init__(self):
        self.now = at(DAY, 16, 0)

    def close(self):
        pass

    def _day(self, key, day, upto=None):
        out = []
        for k in range(75):
            start = at(day, 9, 15) + dt.timedelta(minutes=5 * k)
            if upto is not None and start > upto:
                break
            o = 100.0
            if key == 'NSE_EQ|INEA':
                o = {DAY: 100 + 0.2 * k, NEXT: 110 + 0.1 * k}.get(day, 100.0)
            elif key == 'NSE_EQ|INEB':
                o = {DAY: 100 - 0.1 * k, NEXT: 95 - 0.05 * k}.get(day, 100.0)
            step = {DAY: {'NSE_EQ|INEA': 0.2, 'NSE_EQ|INEB': -0.1},
                    NEXT: {'NSE_EQ|INEA': 0.1, 'NSE_EQ|INEB': -0.05}}.get(day, {}).get(key, 0)
            c = o + step
            out.append(Candle(start, o, max(o, c) + 0.1, min(o, c) - 0.1, c,
                              0 if 'INDEX' in key else 1e5))
        return out

    def candles_between(self, key, start, end):
        days, d = [], start
        while d <= end:
            if d.weekday() < 5:
                days += self._day(key, d)
            d += dt.timedelta(days=1)
        return days

    def candles(self, key):
        today = self.now.astimezone(IST).date()
        return self._day(key, today, upto=self.now - dt.timedelta(minutes=5))


class LastDayModel:
    name = 'test-last-day'
    features = ['prev_r1']

    def predict(self, row):
        return row['prev_r1']


@pytest.mark.db
def test_decide_in_the_evening_and_settle_the_next_session(db_conn, monkeypatch):
    for sym in 'ABC':
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
    monkeypatch.setattr(evening, 'model', lambda: LastDayModel())
    feed, sent = Feed(), []

    def run(day, hh, mm):
        feed.now = at(day, hh, mm)
        return evening.step(db_conn, feed, clock=lambda: feed.now, pause=lambda _: None,
                            notify=sent.append)

    assert 'nothing due' in run(DAY, 15, 30)              # before the close is complete
    assert "'picks': 2" in run(DAY, 16, 0)
    rows = db_conn.execute('select symbol, side, status from evening_pick order by rank').fetchall()
    assert rows == [('A', 1, 'open'), ('B', -1, 'open')]
    assert 'BUY A' in sent[-1] and 'SELL short B' in sent[-1]
    assert "'status': 'done'" in run(DAY, 16, 30)          # once an evening
    assert 'weekend' in run(dt.date(2026, 10, 10), 16, 0)

    assert 'nothing due' in run(NEXT, 12, 0)               # not before 15:15 has closed
    run(NEXT, 15, 20)
    rates = intraday_rates()
    got = {r[0]: r[1:] for r in db_conn.execute(
        "select symbol, status, trade_date, entry::float8, exit::float8, net_inr::float8 "
        "from evening_pick").fetchall()}
    # Entry at the 09:15 open (k = 0), exit at the 15:15 open (k = 72), 0.05% a side.
    assert got['A'][:4] == ('closed', NEXT, pytest.approx(110), pytest.approx(117.2))
    assert got['A'][4] == pytest.approx(
        ml.result(1, 110, 117.2, rates, slip=evening.SLIP)['net_inr'])
    assert got['B'][:4] == ('closed', NEXT, pytest.approx(95), pytest.approx(91.4))
    assert 'Evening intraday paper results' in sent[-1] and 'Since the start' in sent[-1]
    rec = evening.record(db_conn)['total']
    assert rec['trades'] == 2 and rec['net_inr'] == pytest.approx(got['A'][4] + got['B'][4])


def test_the_evening_timer_names_one_time_a_line():
    text = (ROOT / 'scripts' / 'systemd' / 'igs-intraday-evening.timer').read_text()
    lines = [x.split('=', 1)[1] for x in text.splitlines() if x.startswith('OnCalendar=')]
    assert [x.split()[2] for x in lines] == ['15:45', '16:15', '16:45', '17:30', '19:00',
                                             '21:00']
    assert all(x.startswith('Mon..Fri *-*-* ') and x.endswith(' Asia/Kolkata') for x in lines)
