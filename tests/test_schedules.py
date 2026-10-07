"""Scheduled processing respects the AI switch, partial collection and worker exclusion."""
import os

import psycopg
import pytest

from igs import cli
from igs.config import load_assistant, load_sources


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
    monkeypatch.setattr(cli, '_news_assess', lambda args, **kwargs: calls.append(args.limit) or 0)
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


@pytest.mark.db
@pytest.mark.parametrize('collection_status', [0, 1])
def test_scheduled_news_defers_budget_but_preserves_collection_failures(
        db_conn, monkeypatch, capsys, collection_status):
    from igs.assistant.errors import BudgetExceeded
    cfg = load_assistant().model_copy(update={'enabled': True})
    monkeypatch.setattr('igs.config.load_assistant', lambda: cfg)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    monkeypatch.setattr(cli, '_news_collect', lambda args: collection_status)
    monkeypatch.setattr('igs.assistant.llm.Assistant.open', lambda conn: object())

    def exhausted(*args):
        raise BudgetExceeded('daily limit reached')
    monkeypatch.setattr('igs.assistant.geopolitical.assess_pending', exhausted)
    assert cli.main(['news', 'process']) == collection_status
    assert 'deferred' in capsys.readouterr().out
    # An explicit manual request still reports that assessment could not run.
    assert cli.main(['news', 'assess']) == 2


@pytest.mark.db
def test_daily_budget_is_deferred_and_other_errors_still_fail(db_conn, tmp_path):
    import datetime as dt

    from igs.assistant.errors import AssistantError, BudgetExceeded
    from igs.daily import DailyReport, _step
    from igs.ingest.jobs import Context
    from igs.ingest.raw_store import RawStore

    ctx = Context(conn=db_conn, store=RawStore(tmp_path), sources=load_sources())
    rep = DailyReport(day=dt.date(2026, 10, 3))

    def fail(exc):
        raise exc
    _step(rep, ctx, 'AI news', lambda: fail(BudgetExceeded('daily limit reached')))
    assert rep.steps[0][1] == 'deferred' and not rep.failed
    _step(rep, ctx, 'AI news API', lambda: fail(AssistantError('API failed')))
    assert rep.failed == ['AI news API']


@pytest.mark.db
def test_broker_collection_survives_budget_but_not_feed_errors(db_conn, monkeypatch):
    from igs import brokers
    from igs.assistant.errors import BudgetExceeded

    cfg = load_assistant().model_copy(update={'enabled': True})
    monkeypatch.setattr('igs.config.load_assistant', lambda: cfg)
    got = brokers.Collection()
    monkeypatch.setattr(brokers, 'collect', lambda conn: got)

    def exhausted(*args):
        raise BudgetExceeded('daily limit reached')
    monkeypatch.setattr('igs.assistant.llm.Assistant.open', exhausted)
    assert 'AI deferred' in brokers.step(db_conn)
    got.errors.append('feed returned HTTP 403')
    with pytest.raises(RuntimeError, match='HTTP 403'):
        brokers.step(db_conn)


def test_default_verification_skips_only_unconfigured_sources(monkeypatch, capsys):
    from types import SimpleNamespace

    cfg = load_sources()
    null_spec = next(s for s in cfg.sources if s.url is None)
    active = next(s for s in cfg.sources if s.url)
    monkeypatch.setattr('igs.config.load_sources', lambda: cfg.model_copy(
        update={'sources': [null_spec, active]}))
    checked = []

    def verify(spec, fetcher):
        checked.append(spec.id)
        return SimpleNamespace(status='failed', message='HTTP 403', row_count=None,
                               schema=None)
    monkeypatch.setattr('igs.ingest.verify.verify_source', verify)
    monkeypatch.setattr('igs.ingest.http.Fetcher', lambda store: object())
    assert cli._sources_verify(SimpleNamespace(ids=[])) == 1
    assert checked == [active.id]
    assert 'SKIPPED' in capsys.readouterr().out
    checked.clear()
    assert cli._sources_verify(SimpleNamespace(ids=[null_spec.id])) == 1
    assert checked == [null_spec.id]


@pytest.mark.db
@pytest.mark.parametrize('kind', ['news', 'brokers'])
def test_concurrent_collection_is_deferred_without_feed_error(db_conn, monkeypatch, kind):
    from igs import brokers, news

    module = news if kind == 'news' else brokers
    with psycopg.connect(os.environ['IGS_TEST_DATABASE_URL']) as other:
        other.execute('select pg_advisory_lock(%s)', (module.LOCK_KEY,))
        other.commit()
        try:
            collect = news.collect_news if kind == 'news' else brokers.collect
            report = collect(db_conn)
            assert report.deferred and not report.errors
            step = news.collection_step if kind == 'news' else brokers.step
            assert 'collection deferred' in step(db_conn)
        finally:
            other.execute('select pg_advisory_unlock(%s)', (module.LOCK_KEY,))
            other.commit()
