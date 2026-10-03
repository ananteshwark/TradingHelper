import datetime as dt

import pytest

from igs.call_performance import enrich, measure

D = dt.date
START = D(2026, 9, 25)
END = D(2026, 9, 28)


@pytest.mark.parametrize('action,last,status,directional', [
    ('buy', 110, 'Right direction', 10), ('buy', 90, 'Wrong direction', -10),
    ('sell', 90, 'Right direction', 10), ('sell', 110, 'Wrong direction', -10),
    ('hold', 110, 'Hold — not scored', None), ('buy', 100, 'Unchanged', 0)])
def test_direction(action, last, status, directional):
    got = measure([(START, 100, 100), (END, last, 100)], START, action, END)
    assert got['performance'] == status
    assert got['directional return (%)'] == directional
    assert got['entry price (Rs)'] == 100
    assert got['latest price (Rs)'] == last


def test_split_does_not_appear_as_a_loss():
    got = measure([(START, 100, 100), (END, 55, 50)], START, 'buy', END)
    assert got['price change (%)'] == -45
    assert got['adjusted change (%)'] == 10
    assert got['performance'] == 'Right direction'


def test_weekend_future_and_missing_prices():
    prices = [(START, 100, 100), (END, 110, 100), (D(2026, 9, 29), 121, 110)]
    got = measure(prices, D(2026, 9, 26), 'buy', END)
    assert got['entry price date'] == END.isoformat()
    assert got['latest price (Rs)'] == 110
    assert got['performance'] == 'Awaiting later price'
    assert measure([], START, 'buy', END)['performance'] == 'Missing entry price'
    assert measure(prices, END, 'buy', START)['performance'] == 'Awaiting recommendation date'
    assert measure(prices, D(2026, 9, 1), 'buy', END)['performance'] == 'Missing entry price'


def test_stale_and_invalid_prices():
    prices = [(START, 100, 100), (END, 110, 100)]
    got = measure(prices, START, 'buy', D(2026, 10, 10))
    assert got['performance'] == 'Stale prices — Right direction'
    assert got['price age (days)'] == 12
    assert measure([(START, 0, 100)], START, 'buy', END)['performance'] == 'Invalid price'


def test_enrichment_shares_queries_and_preserves_each_recommendation(monkeypatch):
    queries = []
    def fetch(conn, cid, start, end):
        queries.append((cid, start, end))
        return [(START, 100, 100), (END, 110, 100)]
    monkeypatch.setattr('igs.call_performance._price_rows', fetch)
    got = enrich(None, [{}, {}, {}], [(1, START, 'buy'), (1, END, 'sell'),
                                     (None, START, 'buy')], END)
    assert queries == [(1, START, END)]
    assert got[0]['directional return (%)'] == 10
    assert got[1]['performance'] == 'Awaiting later price'
    assert got[2]['performance'] == 'Missing entry price'

