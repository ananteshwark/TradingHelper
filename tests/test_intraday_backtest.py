"""igs intraday-backtest: Upstox candle download with cache, replay and summary."""
import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest

from igs.intraday import backtest
from igs.intraday.engine import BAR, Candle
from igs.timeutil import IST

DAYS = [dt.date(2026, 7, 1), dt.date(2026, 7, 2)]


def session(day, drift=0.0):
    start = dt.datetime.combine(day, dt.time(9, 15), IST)
    return [Candle(start + i * BAR, 100 + i * drift, 100.5 + i * drift, 99.5 + i * drift,
                   100 + i * drift, 1000) for i in range(75)]


def test_requests_are_split_into_calendar_months():
    assert backtest.chunks(dt.date(2026, 6, 15), dt.date(2026, 8, 3)) == [
        (dt.date(2026, 6, 30), dt.date(2026, 6, 15)),
        (dt.date(2026, 7, 31), dt.date(2026, 7, 1)),
        (dt.date(2026, 8, 3), dt.date(2026, 8, 1))]


def test_candles_are_downloaded_once_then_read_from_the_cache(tmp_path):
    asked = []

    def handle(request):
        asked.append(request.url.raw_path.decode())
        return httpx.Response(200, json={'status': 'success', 'data': {'candles': [
            ['2026-07-01T09:15:00+05:30', 100, 101, 99, 100.5, 1000, 0]]}})

    client = httpx.Client(base_url='https://api.upstox.com',
                          transport=httpx.MockTransport(handle))
    got = backtest.fetch(client, 'NSE_EQ|INE1', dt.date(2026, 6, 20), dt.date(2026, 7, 5),
                         tmp_path, pause=lambda _: None)
    assert len(got) == 2 and asked == [
        '/v3/historical-candle/NSE_EQ%7CINE1/minutes/5/2026-06-30/2026-06-20',
        '/v3/historical-candle/NSE_EQ%7CINE1/minutes/5/2026-07-05/2026-07-01']
    again = backtest.fetch(client, 'NSE_EQ|INE1', dt.date(2026, 6, 25), dt.date(2026, 7, 1),
                           tmp_path, pause=lambda _: None)
    assert again == got and len(asked) == 2                 # inside the cached range
    assert json.loads(next(tmp_path.iterdir()).read_text())['to'] == '2026-07-05'


def test_the_first_call_of_a_day_is_replayed_as_the_scanner_would(monkeypatch):
    days = {DAYS[0]: session(DAYS[0]), DAYS[1]: session(DAYS[1], drift=0.05)}
    seen = []

    def fake_evaluate(bars, history, now, index, evidence, tick):
        seen.append((len(bars), now, history))
        last = bars[-1]
        if last.start.astimezone(IST).time() < dt.time(10, 0):
            return {'action': 'wait'}
        end = last.start + BAR
        return {'action': 'buy', 'rvol': 4.0, 'day_move_pct': 1.0,
                'candle_end': end.isoformat(),
                'expires_at': (end + dt.timedelta(minutes=10)).isoformat(),
                'reference': last.close, 'stop': last.close - 1, 'target': last.close + 2}

    monkeypatch.setattr(backtest, 'evaluate', fake_evaluate)
    rows = backtest.replay(days, days, Decimal('0.05'), DAYS[1],
                           backtest.intraday_rates())
    assert len(rows) == 1                       # one call per stock per day
    row = rows[0]
    assert row['time'] == dt.time(10, 0) and row['outcome'] == 'target'
    assert row['r'] == 2 and 1.8 < row['r_net'] < 2           # charges on Rs 1 lakh
    bars, now, history = seen[0]
    assert bars == 6 and now == days[DAYS[1]][5].start + BAR + dt.timedelta(seconds=20)
    # The same-time candle of the prior session, then its closing candle.
    assert history == [days[DAYS[0]][5], days[DAYS[0]][-1]]


def test_summary_by_month_and_overall():
    rows = [{'day': dt.date(2026, 7, 1), 'outcome': 'target', 'r_net': 1.9},
            {'day': dt.date(2026, 8, 1), 'outcome': 'stop', 'r_net': -1.1},
            {'day': dt.date(2026, 8, 2), 'outcome': 'not_filled', 'r_net': None}]
    summary = backtest.summarise(rows)
    assert [s['Period'] for s in summary] == ['2026-07', '2026-08', 'All']
    assert summary[-1] == {'Period': 'All', 'Calls': 2, 'Stop first': 0.5,
                           'Target first': 0.5, 'Win rate': 0.5, 'Average R net': 0.4,
                           'Total R net': pytest.approx(0.8)}
