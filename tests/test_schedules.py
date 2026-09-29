"""Scheduled processing respects the AI switch, partial collection and worker exclusion."""
import os

import psycopg
import pytest

from igs import cli
from igs.config import load_assistant


def test_news_process_collects_when_ai_disabled(monkeypatch):
    calls = []
    cfg = load_assistant().model_copy(update={'enabled': False})
    monkeypatch.setattr('igs.config.load_assistant', lambda: cfg)
    monkeypatch.setattr(cli, '_news_collect', lambda args: calls.append('collect') or 0)
    monkeypatch.setattr(cli, '_news_assess', lambda args: calls.append('assess') or 0)
    assert cli.main(['news', 'process']) == 0
    assert calls == ['collect']


def test_news_process_assesses_available_articles_after_partial_feed_failure(monkeypatch):
    calls = []
    cfg = load_assistant().model_copy(update={'enabled': True})
    monkeypatch.setattr('igs.config.load_assistant', lambda: cfg)
    monkeypatch.setattr(cli, '_news_collect', lambda args: calls.append('collect') or 1)
    monkeypatch.setattr(cli, '_news_assess', lambda args: calls.append(args.limit) or 0)
    assert cli.main(['news', 'process', '--limit', '3']) == 1
    assert calls == ['collect', 3]


@pytest.mark.db
def test_scheduled_assessment_does_not_duplicate_an_active_ui_worker(db_conn, monkeypatch):
    from types import SimpleNamespace

    from igs.assistant.geopolitical import ASSESSMENT_LOCK_KEY, assess_pending

    with psycopg.connect(os.environ['IGS_TEST_DATABASE_URL']) as other:
        other.execute('select pg_advisory_lock(%s)', (ASSESSMENT_LOCK_KEY,))
        other.commit()
        try:
            # No model client is provided: any attempt to call it would fail this test.
            assert assess_pending(SimpleNamespace(conn=db_conn)) == 0
        finally:
            other.execute('select pg_advisory_unlock(%s)', (ASSESSMENT_LOCK_KEY,))
            other.commit()
    # An exception after acquiring the worker lock must release it for other sessions.
    def fail(*args):
        raise ValueError('simulated failure')
    monkeypatch.setattr('igs.assistant.geopolitical._assess_pending', fail)
    with pytest.raises(ValueError, match='simulated failure'):
        assess_pending(SimpleNamespace(conn=db_conn))
    with psycopg.connect(os.environ['IGS_TEST_DATABASE_URL']) as other:
        assert other.execute('select pg_try_advisory_lock(%s)',
                             (ASSESSMENT_LOCK_KEY,)).fetchone()[0]
        other.execute('select pg_advisory_unlock(%s)', (ASSESSMENT_LOCK_KEY,))
        other.commit()
