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
    monkeypatch.setattr('igs.call_performance._index_series',
                        lambda conn, start, end: [(START, 1000), (END, 1050)])
    got = enrich(None, [{}, {}, {}], [(1, START, 'buy'), (1, END, 'sell'),
                                     (None, START, 'buy')], END)
    assert queries == [(1, START, END)]
    assert got[0]['directional return (%)'] == 10
    assert got[0]['excess vs Nifty 500 (%)'] == 5
    assert got[0]['performance'] == 'Right vs Nifty 500'
    assert got[1]['performance'] == 'Awaiting later price'
    assert got[2]['performance'] == 'Missing entry price'


INDEX = [(START, 1000), (END, 1100)]          # the Nifty 500 rose 10%


@pytest.mark.parametrize('action,last,status,excess', [
    ('buy', 115, 'Right vs Nifty 500', 5), ('buy', 105, 'Wrong vs Nifty 500', -5),
    ('sell', 105, 'Right vs Nifty 500', -5), ('sell', 115, 'Wrong vs Nifty 500', 5),
    ('buy', 110, 'Level with Nifty 500', 0), ('hold', 120, 'Hold — not scored', 10)])
def test_judged_against_the_nifty_500(action, last, status, excess):
    """A buy up 5% while the market rose 10% was not a good call."""
    got = measure([(START, 100, 100), (END, last, 100)], START, action, END, index=INDEX)
    assert got['performance'] == status
    assert got['Nifty 500 change (%)'] == 10
    assert got['excess vs Nifty 500 (%)'] == excess


def test_missing_index_data_is_said_not_guessed():
    got = measure([(START, 100, 100), (END, 105, 100)], START, 'buy', END,
                  index=[(START, 1000)])
    assert got['performance'] == 'Benchmark missing — Right direction'
    assert got['excess vs Nifty 500 (%)'] is None


def test_a_call_after_the_close_starts_from_the_next_close():
    prices = [(START, 100, 100), (D(2026, 9, 26), 104, 100), (END, 110, 104)]
    during = measure(prices, START, 'buy', END)
    after = measure(prices, START, 'buy', END, after_close=True)
    assert during['entry price date'] == START.isoformat()
    assert after['entry price date'] == '2026-09-26' and after['entry price (Rs)'] == 104


def test_after_close_is_from_15_30_ist():
    from igs.call_performance import after_close
    from igs.timeutil import IST
    assert not after_close(dt.datetime(2026, 9, 25, 15, 29, tzinfo=IST))
    assert after_close(dt.datetime(2026, 9, 25, 15, 30, tzinfo=IST))
    assert after_close(dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.UTC))   # 17:30 IST
