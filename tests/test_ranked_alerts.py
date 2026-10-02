import datetime as dt
from urllib.parse import parse_qs

import httpx
import pytest
import test_ai_calls as A
import test_alerts as T

from igs.alerts import delivery
from igs.alerts.rules import queue_ranked_events, ranked_events, record_new
from igs.config import load_alerts

pytestmark = pytest.mark.db
two_runs = T.two_runs


def test_absolute_top10_entries_and_reentry(two_runs):
    conn, first, second = two_runs
    conn.execute('update score_result set rank=11 where company_id=1')
    conn.execute('update score_result set rank=10 where run_id=%s and company_id=1', (second,))
    rules = {'top100_buys': {'enabled': False}}
    events = ranked_events(conn, second, first, rules)
    assert any(a.company_id == 1 and a.kind == 'top10_entry' for a in events)
    assert record_new(conn, events, second, ('telegram_calls',))
    assert record_new(conn, events, second, ('telegram_calls',)) == []
    assert not ranked_events(conn, second, second, rules)
    conn.execute('update score_result set rank=11 where run_id=%s and company_id=1', (second,))
    assert not any(a.company_id == 1 for a in ranked_events(conn, second, first, rules))


def test_top100_buy_threshold_and_buy_streak(two_runs):
    conn, first, second = two_runs
    rules = {'top10_entries': {'enabled': False}}
    conn.execute('update score_result set rank=100 where run_id=%s and company_id=1', (second,))
    A._insert(conn, second, 1, 'GROW', 'buy', dt.date(2024, 11, 29))
    events = ranked_events(conn, second, first, rules)
    assert len(events) == 1 and events[0].kind == 'top100_buy'
    record_new(conn, events, second, ('telegram_calls',))
    conn.execute('update score_result set rank=101 where run_id=%s and company_id=1', (second,))
    assert ranked_events(conn, second, first, rules) == []
    conn.execute('update score_result set rank=99 where run_id=%s and company_id=1', (second,))
    A._insert(conn, second, 1, 'GROW', 'buy', dt.date(2024, 11, 29))
    assert record_new(conn, ranked_events(conn, second, first, rules), second) == []
    A._insert(conn, second, 1, 'GROW', 'sell', dt.date(2024, 11, 29))
    assert ranked_events(conn, second, first, rules) == []
    A._insert(conn, second, 1, 'GROW', 'buy', dt.date(2024, 11, 29))
    assert record_new(conn, ranked_events(conn, second, first, rules), second)


def test_separate_telegram_delivery_and_obsolete_cancellation(two_runs, monkeypatch):
    conn, first, second = two_runs
    first = second
    second = conn.execute("""insert into score_run(as_of,gate_fingerprint,config)
        values(now(),'test','{}') returning run_id""").fetchone()[0]
    conn.execute("""insert into score_result(run_id,company_id,symbol,rank,scored,tier,explanation)
        select %s,company_id,symbol,rank,scored,tier,explanation
        from score_result where run_id=%s""", (second, first))
    conn.execute('update score_result set rank=11 where run_id=%s and company_id=1', (first,))
    conn.execute('update score_result set rank=1 where run_id=%s and company_id=1', (second,))
    A._insert(conn, second, 1, 'GROW', 'buy', dt.date(2024, 11, 29))
    cfg = load_alerts().model_copy(update={'channels': {'telegram_calls': True}})
    queue_ranked_events(conn, cfg)
    queue_ranked_events(conn, cfg)
    posts = []
    def receive(req):
        posts.append(parse_qs(req.content.decode())['text'][0])
        return httpx.Response(200, json={'ok': True})
    monkeypatch.setenv('IGS_TELEGRAM_TOKEN', 'fake')
    monkeypatch.setenv('IGS_TELEGRAM_CHAT_ID', '123')
    client = httpx.Client(transport=httpx.MockTransport(receive))
    delivery.deliver_pending(conn, cfg, telegram_client=client)
    assert any(p.startswith('TOP 10 ENTRY') for p in posts)
    assert any(p.startswith('TOP 100 — AI BUY') for p in posts)
    assert all(not ('TOP 10 ENTRY' in p and 'TOP 100 — AI BUY' in p) for p in posts)
    n = len(posts)
    delivery.deliver_pending(conn, cfg, telegram_client=client)
    assert len(posts) == n
    # Unsent alerts are cancelled when the stock leaves the qualifying ranks.
    conn.execute("update alert_outbox set status='failed',next_attempt_at=now()")
    conn.execute('update score_result set rank=101 where run_id=%s', (second,))
    queue_ranked_events(conn, cfg)
    remaining = conn.execute("select count(*) from alert_outbox where status<>'cancelled'")
    assert remaining.fetchone()[0] == 0
