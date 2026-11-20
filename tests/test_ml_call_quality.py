"""Reject stale/late ML calls and avoid counting same-day trades as independent evidence."""
import datetime as dt
import math

import pytest
from test_intraday_ml import DAY, Feed, R30Model, at, prior

from igs.intraday import ml
from igs.ui.intraday_ml import _luck


def history():
    dates = []
    d = DAY - dt.timedelta(days=1)
    while len(dates) < ml.HISTORY:
        if d.weekday() < 5:
            dates.insert(0, d)
        d -= dt.timedelta(days=1)
    return [{**r, 'session_date': d} for r, d in zip(prior(), dates, strict=True)]


@pytest.mark.lookahead
def test_features_require_matching_completed_sessions():
    reference = history()
    assert ml.history_matches(reference, reference, DAY)
    missing = [dict(r) for r in reference]
    missing[-2]['session_date'] = missing[-3]['session_date']
    assert not ml.history_matches(missing, reference, DAY)
    assert not ml.history_matches(reference[:-1], reference, DAY)
    assert not ml.history_matches(reference, reference[:-1], DAY)
    assert not ml.history_matches(list(reversed(reference)), reference, DAY)
    assert not ml.history_matches(missing, missing, DAY)
    assert not ml.history_matches(reference, reference, reference[-1]['session_date'])
    # Calendar gaps are allowed when both observed session windows agree.
    assert ml.history_matches(reference, reference, DAY + dt.timedelta(days=3))


class Connection:
    def __init__(self):
        self.writes = []

    def execute(self, sql, args=()):
        if sql.lstrip().startswith('insert'):
            self.writes.append((sql, args))
        return self

    def fetchone(self):
        return None

    def commit(self):
        pass


def decision(monkeypatch, *, stale=False, decided=None):
    conn, feed = Connection(), Feed()
    feed.now = at(DAY, 9, 46)
    reference, stock = history(), history()
    if stale:
        stock[-1]['session_date'] -= dt.timedelta(days=1)
    monkeypatch.setattr(ml, 'universe', lambda conn: [
        {'instrument_key': 'NSE_EQ|INEA', 'company_id': 1, 'symbol': 'A'}])
    # A failed refresh leaves the stock's old cache; it must not be used for a new call.
    monkeypatch.setattr(ml, 'prepare', lambda *a, **kw: 1)
    monkeypatch.setattr(ml, '_stored', lambda conn, key, day:
                        reference if key == ml.INDEX_KEY else stock)
    result = ml.decide(conn, feed, feed.now, clock=lambda: decided or feed.now,
                       pause=lambda _: None, mdl=R30Model())
    return conn, result


def test_failed_history_refresh_cannot_issue_a_call(monkeypatch):
    conn, result = decision(monkeypatch, stale=True)
    assert result['picks'] == result['scored'] == 0
    assert not any('insert into ml_intraday_pick' in sql for sql, _ in conn.writes)
    assert '1 stocks withheld' in conn.writes[0][1][-1]


@pytest.mark.parametrize('moment', [at(DAY, 9, 55), at(DAY, 10, 0),
                                   at(DAY + dt.timedelta(days=1), 9, 46)])
@pytest.mark.lookahead
def test_late_scan_records_no_actionable_picks(monkeypatch, moment):
    conn, result = decision(monkeypatch, decided=moment)
    assert result == {'status': 'expired', 'scored': 1, 'picks': 0}
    assert len(conn.writes) == 1
    assert 'Decision window expired' in conn.writes[0][1][-1]


def test_valid_on_time_scan_keeps_the_original_selection(monkeypatch):
    conn, result = decision(monkeypatch, decided=at(DAY, 9, 54) + dt.timedelta(seconds=59))
    assert result == {'status': 'decided', 'scored': 1, 'picks': 1}
    assert 'ml_intraday_pick' in conn.writes[1][0]
    assert conn.writes[1][1][4] == 1


def test_daily_evidence_counts_cash_days_not_individual_trades():
    values = [900, 0, 0, -100]
    days = [{'net_inr': x, 'unresolved': 0, 'trades': n}
            for x, n in zip(values, (6, 0, 0, 6), strict=True)]
    got = ml.daily_evidence(days)
    mean = sum(values) / 4
    sd = math.sqrt(sum((x - mean) ** 2 for x in values) / 3)
    assert got['evidence_days'] == 4 and got['incomplete_days'] == 0
    assert got['daily_net_inr'] == mean
    assert got['t'] == pytest.approx(mean / sd * 2)
    assert ml.daily_evidence([{**d, 'trades': 6000} for d in days]) == got
    assert ml.daily_evidence([days[0]])['t'] is None


@pytest.mark.parametrize('unresolved', [1, 6])
def test_unresolved_outcomes_are_not_cash_or_evidence(unresolved):
    days = [{'net_inr': 100, 'unresolved': 0}, {'net_inr': -50, 'unresolved': 0},
            {'net_inr': 0, 'unresolved': unresolved}]
    assert ml.daily_evidence(days) == {
        'evidence_days': 2, 'incomplete_days': 1, 'daily_net_inr': 25, 't': None}


@pytest.mark.parametrize('days', [[], [{'net_inr': 0, 'unresolved': 0}] * 20,
                                 [{'net_inr': 100, 'unresolved': 0}] * 20])
def test_unestimable_statistic_never_crashes_the_page(days):
    total = {'trades': 300, **ml.daily_evidence(days)}
    assert total['t'] is None
    assert 'not enough variation' in _luck(total)


def test_page_does_not_certify_skill_from_a_large_statistic():
    total = {'trades': 300, 'evidence_days': 60, 'incomplete_days': 0, 't': 3.1}
    text = _luck(total)
    assert 't of daily net rupees: 3.10' in text
    assert 'not proof of skill' in text and 'looks like skill' not in text
    assert 'unresolved' in _luck({**total, 'incomplete_days': 1, 't': None})


@pytest.mark.db
def test_record_aggregates_real_rows_and_retains_unresolved_days(db_conn):
    days = [DAY - dt.timedelta(days=k) for k in (3, 2, 1, 0)]
    for day in days:
        db_conn.execute('''insert into ml_intraday_day
            (session_date,decided_at,model,universe,scored)
            values (%s,%s,'test',10,10)''', (day, at(day, 9, 46)))
    for day, value in zip(days[:2], (10, -5), strict=True):
        for k in range(6):
            db_conn.execute('''insert into ml_intraday_pick
                (session_date,instrument_key,symbol,side,prediction,rank,features,
                 entry_after,net_inr,net_pct,status)
                values (%s,%s,'A',1,0.01,%s,'{}',%s,%s,%s,'closed')''',
                (day, f'NSE_EQ|{k}', k + 1, at(day, 9, 46), value, value / 1000))
    db_conn.commit()
    record = ml.record(db_conn)
    assert record['total']['trades'] == 12
    assert record['total']['net_inr'] == 30
    assert [d['net_inr'] for d in record['days']] == [60, -30, 0, 0]
    expected = ml.daily_evidence(record['days'])
    assert record['total']['t'] == pytest.approx(expected['t'])
    assert record['total']['evidence_days'] == 4
    db_conn.execute('''insert into ml_intraday_pick
        (session_date,instrument_key,symbol,side,prediction,rank,features,entry_after,status)
        values (%s,'NSE_EQ|missing','X',1,0.01,1,'{}',%s,'no data')''',
        (days[-1], at(days[-1], 9, 46)))
    db_conn.commit()
    total = ml.record(db_conn)['total']
    assert total['net_inr'] == 30
    assert total['t'] is None and total['incomplete_days'] == 1
    assert total['evidence_days'] == 3
