import datetime as dt
import logging
import sqlite3

import polars as pl
import pytest

from igs.alerts import operations as ops
from igs.call_list import frame
from igs.config import OperationsAlerts


def test_calls_with_returns_only_after_first_hundred_rows():
    items = [{'directional return (%)': None, 'entry price date': None,
              'confidence': 1, 'target (Rs)': None} for _ in range(120)]
    items.append({'directional return (%)': -2.41, 'entry price date': '2026-10-01',
                  'confidence': 0.76, 'target (Rs)': 123.45})
    result = frame(items)
    assert result['directional return (%)'][-1] == -2.41
    assert result['confidence'].dtype == pl.Float64
    assert result['entry price date'][-1] == '2026-10-01'


@pytest.fixture
def spool(tmp_path, monkeypatch):
    path = tmp_path/'errors.sqlite3'
    monkeypatch.setenv('IGS_NOTIFICATION_SPOOL', str(path))
    monkeypatch.setenv('IGS_OPERATIONAL_ALERTS', '1')
    return path


@pytest.fixture
def all_on(monkeypatch):
    """Every operational message on, as config/alerts.yaml `operations` can set it; as
    shipped, failed data jobs and data issues are off."""
    monkeypatch.setattr(ops, 'settings', lambda: OperationsAlerts(data_problems=True))


def _spooled(spool):
    """The messages waiting in the spool at `spool` (ops._spool creates it if need be)."""
    from contextlib import closing
    assert ops._spool().close() is None and spool.exists()
    with closing(sqlite3.connect(spool)) as conn:
        return [r[0] for r in conn.execute('select message from errors order by rowid')]


def test_as_shipped_errors_are_sent_only_for_app_down_database_and_orders():
    """The owner asked for error alerts only when the app is down or the database has a
    problem; the daily ingestion summary stays."""
    from igs.config import load_alerts
    assert load_alerts().operations == OperationsAlerts(
        app_down=True, database=True, orders=True, data_problems=False,
        ingestion_summary=True, sign_ins=True)


def _seen(**over):
    return {'account': 'owner', 'account_failures': 3,
            'account_addresses': ['203.0.113.5'], 'address': '203.0.113.5',
            'address_failures': 3, 'address_accounts': ['owner'], 'address_unknown': 0,
            'blocked_minutes': 0, **over}


def test_repeated_failed_sign_ins_queue_one_alert_an_hour(spool):
    t = dt.datetime(2026, 10, 26, 21, 5, tzinfo=ops.IST).timestamp()
    assert ops.sign_in_failures(_seen(account_failures=2, address_failures=2), now=t) is None
    message = ops.sign_in_failures(_seen(), now=t)
    assert message.splitlines()[:2] == ['SIGN-IN FAILURES', '26 Oct 2026 21:05 IST']
    assert ('3 failed sign-ins for the account owner in 15 minutes, from 1 address: '
            '203.0.113.5.') in message
    assert 'igs users passwd owner' in message
    assert ops.sign_in_failures(_seen(account_failures=4), now=t + 600) is None  # same hour
    assert ops.sign_in_failures(_seen(), now=t + 3600) is not None           # the next one
    assert len(_spooled(spool)) == 2
    # From one address against usernames without an account: counted, never named.
    many = ops.sign_in_failures(_seen(account=None, account_failures=0, address='198.51.100.7',
                                      address_failures=6, address_accounts=[],
                                      address_unknown=6), now=t)
    assert ('From 198.51.100.7: 6 failed sign-ins in 15 minutes, for 6 with a username that '
            'has no account.') in many and 'passwd NAME' in many
    blocked = ops.sign_in_failures(_seen(account_failures=5, address_failures=5,
                                         blocked_minutes=14), now=t + 7200)
    assert 'Sign-in to owner is now blocked from there for 14 minutes.' in blocked


def test_sign_in_alerts_can_be_switched_off(spool, monkeypatch):
    monkeypatch.setattr(ops, 'settings', lambda: OperationsAlerts(sign_ins=False))
    assert ops.sign_in_failures(_seen()) is None
    monkeypatch.setenv('IGS_OPERATIONAL_ALERTS', '0')
    monkeypatch.setattr(ops, 'settings', lambda: OperationsAlerts())
    assert ops.sign_in_failures(_seen()) is None and _spooled(spool) == []


def test_errors_are_sorted_into_database_order_and_data_problems():
    import psycopg.errors
    assert all(hasattr(psycopg.errors, name) for name in ops.DATABASE_ERRORS)
    assert ops.category('cli.sync', 'DiskFull') == 'database'
    assert ops.category('igs.ui.app:12', 'UndefinedTable') == 'database'
    assert ops.category('intraday-trade', 'SquareOffFailed') == 'orders'
    assert ops.category('intraday-approvals', 'TelegramWebhookConflict') == 'orders'
    assert ops.category('cli.sync', 'ExitStatus1') == 'data_problems'
    assert ops.category('screener.TCS', 'HTTPStatusError') == 'data_problems'
    # A refused connection is left to the minute-by-minute check, which waits out restarts.
    assert ops.category('cli.sync', 'OperationalError') == 'data_problems'


def test_a_failed_data_job_is_not_sent_but_a_database_or_order_problem_is(spool):
    ops.record_issue('cli.sync', 'ExitStatus1')
    ops.record_issue('igs-sync.service', 'ServiceFailed', event_id='x')
    ops.record_issue('screener.TCS', 'ReadTimeout')
    assert _spooled(spool) == []
    ops.record_issue('cli.sync', 'DiskFull')
    ops.record_issue('intraday-trade', 'ExitUnprotected', event_id='7')
    sent = _spooled(spool)
    assert sent[0].startswith('DATABASE PROBLEM') and 'DiskFull' in sent[0]
    assert sent[1].startswith('ORDER PROBLEM') and 'ExitUnprotected' in sent[1]


def test_errors_redacted_deduplicated_and_retryable(spool):
    import psycopg.errors
    handler = ops.ErrorHandler(logging.ERROR)
    try:
        raise psycopg.errors.DiskFull('password=SECRET botTOKEN')
    except psycopg.errors.DiskFull:
        import sys
        record = logging.LogRecord('igs.demo', logging.ERROR, 'app.py', 42,
                                   'URL token SECRET', (), sys.exc_info())
    handler.emit(record)
    handler.emit(record)
    assert ops.deliver_errors(lambda _: False) == 0
    with sqlite3.connect(spool) as conn:
        rows = conn.execute('select message,attempts from errors').fetchall()
        assert len(rows) == 1 and rows[0][1] == 1
        assert 'SECRET' not in rows[0][0] and 'DiskFull' in rows[0][0]
        conn.execute('update errors set next_attempt=0')
    sent = []
    assert ops.deliver_errors(lambda text: sent.append(text) or True) == 1
    assert ops.deliver_errors(lambda text: sent.append(text) or True) == 0
    assert len(sent) == 1


@pytest.mark.db
def test_ingestion_queue_commit_rollback_duplicate_and_retry(db_conn, all_on):
    c = db_conn
    # The ingestion summary is daily. Make it due so this test exercises retries.
    c.execute("update ingestion_digest_schedule set last_sent_at=now()-interval '2 days'")
    c.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,
        size_bytes,blob_path,origin)
        values ('sample','test',now(),repeat('a',64),0,'x','manual')""")
    c.commit()
    c.execute("insert into trading_holiday values ('NSE','2026-10-02','Holiday','sample')")
    c.rollback()
    assert c.execute('select count(*) from operational_notification').fetchone()[0] == 0
    c.execute("insert into trading_holiday values ('NSE','2026-10-02','Holiday','sample')")
    c.commit()
    c.execute("insert into trading_holiday values ('NSE','2026-10-02','Holiday','sample') "
              'on conflict do nothing')
    c.commit()
    assert c.execute('select count(*) from operational_notification').fetchone()[0] == 1
    assert ops.deliver_database(c, lambda _: False) == 0
    assert c.execute('select attempts,sent_at from operational_notification').fetchone() == (1,None)
    assert ops.deliver_database(c, lambda _: True) == 0  # whole digest waits for its retry
    c.execute('update operational_notification set next_attempt_at=now()')
    c.execute('update ingestion_digest_schedule set next_attempt_at=now()')
    c.commit()
    sent = []
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 1
    assert 'trading_holiday: 1' in sent[0]
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 0


@pytest.mark.db
def test_data_issues_alert_only_errors_and_no_secrets(db_conn, all_on):
    from igs.dq import DQLog
    dq = DQLog()
    for severity in ('info','warn','error'):
        dq.emit(severity, 'test_category', 'PRIVATE secret-token')
    dq.persist(db_conn)
    db_conn.commit()
    assert db_conn.execute("select severity,count(*) from dq_issue group by severity "
                           "order by severity").fetchall() == [
                               ('error', 1), ('info', 1), ('warn', 1)]
    assert db_conn.execute("select payload->>'severity' from operational_notification "
                           "where kind='issue'").fetchall() == [('error',)]
    from psycopg.types.json import Jsonb
    db_conn.execute("insert into operational_notification(event_key,kind,payload) "
                    "values ('old-warning','issue',%s)",
                    (Jsonb({'severity': 'warn', 'category': 'old', 'count': 1}),))
    db_conn.commit()
    sent = []
    ops.deliver_database(db_conn, lambda t: sent.append(t) or True)
    assert len(sent) == 1
    assert 'error: test_category: 1' in sent[0]
    assert 'warn:' not in sent[0]
    assert 'PRIVATE' not in sent[0] and 'secret-token' not in sent[0]
    assert db_conn.execute("select sent_at is null from operational_notification "
                           "where event_key='old-warning'").fetchone() == (True,)


def test_quarter_message_is_separate():
    """A quarterly report is read and sent on its own (tests/test_results_read.py), never in
    the grouped messages."""
    now = dt.datetime.now(dt.UTC)
    assert ops.messages([(1,'quarterly_report', {'company':'Example Ltd',
        'period_end':'2026-06-30','basis':'consolidated','exchange':'NSE',
        'filed_at':now.isoformat()}, now)]) == []


@pytest.mark.db
def test_revisions_notify_but_noop_updates_do_not(db_conn, all_on):
    c = db_conn
    c.execute("update ingestion_digest_schedule set last_sent_at=now()-interval '2 days'")
    c.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,
        size_bytes,blob_path,origin) values ('f','test',now(),repeat('a',64),0,'x','manual')""")
    c.execute("insert into trading_holiday values ('NSE','2026-10-02','Old','f')")
    c.commit()
    c.execute("update trading_holiday set description='Old'")
    c.commit()
    assert c.execute('select count(*) from operational_notification').fetchone()[0] == 1
    c.execute("update trading_holiday set description='Corrected'")
    c.commit()
    sent = []
    ops.deliver_database(c, lambda t: sent.append(t) or True)
    assert 'trading_holiday (updated): 1' in sent[0]


@pytest.mark.db
def test_daily_ingestion_collects_all_rows_without_delaying_other_alerts(db_conn, all_on):
    from psycopg.types.json import Jsonb

    c = db_conn
    for n in range(125):
        c.execute('''insert into operational_notification(event_key,kind,payload)
            values(%s,'ingestion',%s)''', (f'load:{n}', Jsonb({'table':'sample', 'rows':2})))
    c.execute('''insert into operational_notification(event_key,kind,payload)
        values('problem','issue',%s)''',
        (Jsonb({'severity':'error','category':'test_issue','count':1}),))
    c.execute('''insert into operational_notification(event_key,kind,payload)
        values('quarter','quarterly_report',%s)''', (Jsonb({'company':'Example',
        'period_end':'2026-06-30','basis':'consolidated','exchange':'NSE','filed_at':'today'}),))
    c.commit()
    sent = []
    sender = lambda text: sent.append(text) or True  # noqa: E731
    assert ops.deliver_database(c, sender) == 1
    assert not any('INGESTION SUMMARY' in t for t in sent)
    assert any('APPLICATION DATA ISSUES' in t for t in sent)
    # Without its figures the report can't read positive, so it isn't sent.
    assert c.execute("select skipped from operational_notification where event_key='quarter'"
                     ).fetchone() == ('results read unknown',)
    c.execute("update ingestion_digest_schedule set last_sent_at=now()-interval '2 days'")
    c.commit()
    assert ops.deliver_database(c, sender) == 125  # not truncated to the old 100-event limit
    assert 'sample: 250' in sent[-1]
    c.execute('''insert into operational_notification(event_key,kind,payload)
        values('next','ingestion',%s)''', (Jsonb({'table':'sample','rows':1}),))
    c.commit()
    assert ops.deliver_database(c, sender) == 0
    assert c.execute("select count(*) from operational_notification where sent_at is null "
                     "and skipped is null").fetchone()[0] == 1


def test_failed_cli_command_is_reported_without_error_message(spool, monkeypatch):
    import psycopg.errors

    from igs import cli
    def fail(exc):
        def run(_):
            raise exc
        return run
    # A data job that failed is not sent as shipped.
    monkeypatch.setattr(cli, '_main', fail(ValueError('private token')))
    with pytest.raises(ValueError):
        cli.main(['score'])
    assert _spooled(spool) == []
    # The same command meeting a database problem is.
    monkeypatch.setattr(cli, '_main', fail(psycopg.errors.UndefinedTable('private token')))
    with pytest.raises(psycopg.errors.UndefinedTable):
        cli.main(['score'])
    sent = []
    ops.deliver_errors(lambda t: sent.append(t) or True)
    assert sent[0].startswith('DATABASE PROBLEM')
    assert 'cli.score' in sent[0] and 'UndefinedTable' in sent[0]
    assert 'private token' not in sent[0]


def test_failed_cli_command_is_sent_when_data_problems_are_on(spool, monkeypatch, all_on):
    from igs import cli
    def fail(_):
        raise ValueError('private token')
    monkeypatch.setattr(cli, '_main', fail)
    with pytest.raises(ValueError):
        cli.main(['score'])
    sent = []
    ops.deliver_errors(lambda t: sent.append(t) or True)
    assert sent[0].startswith('APPLICATION ISSUE')
    assert 'cli.score' in sent[0] and 'ValueError' in sent[0]
    assert 'private token' not in sent[0]


def test_failed_service_not_reannounced_hourly_but_new_failure_is(spool,monkeypatch,all_on):
    from types import SimpleNamespace

    monkeypatch.setattr(ops,'SERVICES',('igs-verify.service',))
    state={'id':'first','result':'exit-code'}
    monkeypatch.setattr(ops.subprocess,'run',lambda *a,**k:SimpleNamespace(
        returncode=0,stdout=f"Result={state['result']}\nInvocationID={state['id']}\n"))
    ops.check_services()
    monkeypatch.setattr(ops.time,'time',lambda:9_000_000_000)
    ops.check_services()
    with sqlite3.connect(spool) as c:
        assert c.execute('select count(*) from errors').fetchone()[0]==1
    state['result']='success'
    ops.check_services()
    state.update(id='second',result='exit-code')
    ops.check_services()
    with sqlite3.connect(spool) as c:
        assert c.execute('select count(*) from errors').fetchone()[0]==2


def test_the_summary_is_due_once_a_day_after_digest_time():
    ist = lambda d, h, m: dt.datetime(2026, 10, d, h, m, tzinfo=ops.IST)  # noqa: E731
    assert ops.last_digest_time(ist(9, 21, 29)) == ist(8, 21, 30)
    assert ops.last_digest_time(ist(9, 21, 30)) == ist(9, 21, 30)
    assert ops.last_digest_time(ist(10, 8, 0)) == ist(9, 21, 30)


@pytest.mark.db
def test_daily_summary_counts_unique_companies_and_waits_for_digest_time(db_conn, all_on):
    c = db_conn
    ist = lambda d, h, m: dt.datetime(2026, 10, d, h, m, tzinfo=ops.IST)  # noqa: E731
    c.execute("update ingestion_digest_schedule set last_sent_at=%s", (ist(8, 21, 31),))
    for cid, isin, sym in ((1, 'INE001X01010', 'ONE'), (2, 'INE002X01010', 'TWO'),
                           (3, 'INE003X01010', 'THREE')):
        c.execute("insert into company(company_id,name) values(%s,%s)", (cid, f'{sym} Ltd'))
        c.execute("insert into security(security_id,company_id) values(%s,%s)", (100 + cid, cid))
        for kind, value in (('ISIN', isin), ('NSE_SYMBOL', sym)):
            c.execute("""insert into security_identifier(security_id,id_type,id_value,valid_from,
                evidence) values(%s,%s,%s,'2020-01-01','test')""", (100 + cid, kind, value))
    c.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,
        size_bytes,blob_path,origin) values ('f','test',now(),repeat('a',64),0,'x','manual')""")
    c.commit()
    # Prices for ONE, TWO and a security not yet in the master; industries for ONE and THREE.
    for isin, sym in (('INE001X01010', 'ONE'), ('INE002X01010', 'TWO'), ('INE999X01010', 'NEW')):
        c.execute("""insert into price_eod(exchange,trade_date,isin,symbol,series,open,high,low,
            close,prev_close,volume,source_fetch_id)
            values('NSE','2026-10-09',%s,%s,'EQ',10,11,9,10,10,100,'f')""", (isin, sym))
    for cid in (1, 3):
        c.execute("""insert into industry_classification(company_id,macro_sector,sector,
            industry,basic_industry,valid_from) values(%s,'X','X','X','X','2026-10-09')""",
                  (cid,))
    c.commit()
    sent = []
    sender = lambda text: sent.append(text) or True  # noqa: E731
    assert ops.deliver_database(c, sender, now=ist(9, 21, 0)) == 0      # not before 21:30
    assert ops.deliver_database(c, sender, now=ist(9, 21, 31)) == 2
    text = sent[-1]
    assert 'Unique companies with new or updated data: 3' in text
    assert 'price_eod: 3 (2)' in text and 'industry_classification: 2 (2)' in text
    c.execute("""insert into industry_classification(company_id,macro_sector,sector,industry,
        basic_industry,valid_from) values(2,'X','X','X','X','2026-10-09')""")
    c.commit()
    assert ops.deliver_database(c, sender, now=ist(9, 23, 0)) == 0      # once a day
    assert ops.deliver_database(c, sender, now=ist(10, 21, 30)) == 1
    assert 'Unique companies with new or updated data: 1' in sent[-1]


@pytest.mark.db
def test_as_shipped_data_issues_are_skipped_and_the_summary_is_sent(db_conn, monkeypatch):
    from psycopg.types.json import Jsonb
    c = db_conn
    c.execute("update ingestion_digest_schedule set last_sent_at=now()-interval '2 days'")
    c.execute('''insert into operational_notification(event_key,kind,payload)
        values('load','ingestion',%s)''', (Jsonb({'table': 'sample', 'rows': 2}),))
    c.execute('''insert into operational_notification(event_key,kind,payload)
        values('problem','issue',%s)''',
              (Jsonb({'severity': 'error', 'category': 'test_issue', 'count': 1}),))
    c.commit()
    sent = []
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 1
    assert len(sent) == 1 and sent[0].startswith('DATA INGESTION SUMMARY')
    assert c.execute("select event_key, skipped, sent_at is not null "
                     "from operational_notification order by 1").fetchall() == [
        ('load', None, True), ('problem', 'data problems are off (alerts.yaml)', False)]
    # Turned off, the summary's events are skipped too.
    c.execute('''insert into operational_notification(event_key,kind,payload)
        values('later','ingestion',%s)''', (Jsonb({'table': 'sample', 'rows': 1}),))
    c.commit()
    monkeypatch.setattr(ops, 'settings', lambda: OperationsAlerts(ingestion_summary=False))
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 0
    assert c.execute("select skipped from operational_notification where event_key='later'"
                     ).fetchone() == ('the ingestion summary is off (alerts.yaml)',)


def test_app_down_is_sent_after_three_minutes_then_every_six_hours_and_when_back(spool):
    t0 = 1_800_000_000
    assert ops.track('app_down', "isn't answering (ConnectError)", t0) is None
    assert ops.track('app_down', "isn't answering (ConnectError)", t0 + 120) is None
    down = ops.track('app_down', "isn't answering (ConnectError)", t0 + 180)
    assert down.startswith('APPLICATION DOWN\n')
    assert "The dashboard isn't answering (ConnectError)." in down and '(3 min)' in down
    assert 'systemctl --user status igs-ui.service' in down
    assert ops.track('app_down', "isn't answering (ConnectError)", t0 + 3600) is None
    still = ops.track('app_down', "isn't answering (ConnectError)", t0 + 180 + 6 * 3600)
    assert still.startswith('APPLICATION DOWN, STILL') and '6 h 3 min' in still
    back = ops.track('app_down', None, t0 + 7 * 3600)
    assert back.startswith('APPLICATION BACK UP') and 'down for 7 h 0 min' in back
    assert ops.track('app_down', None, t0 + 8 * 3600) is None
    sent = []
    assert ops.deliver_errors(lambda t: sent.append(t) or True) == 3
    assert [t.splitlines()[0] for t in sent] == [
        'APPLICATION DOWN', 'APPLICATION DOWN, STILL', 'APPLICATION BACK UP']


def test_a_restart_shorter_than_three_minutes_sends_nothing(spool):
    t0 = 1_800_000_000
    assert ops.track('database', "can't be reached or queried (OperationalError)", t0) is None
    assert ops.track('database', "can't be reached or queried (OperationalError)",
                     t0 + 150) is None
    assert ops.track('database', None, t0 + 170) is None
    # A new failure starts its own three minutes.
    assert ops.track('database', "can't be reached or queried (OperationalError)",
                     t0 + 200) is None
    assert ops.track('database', "can't be reached or queried (OperationalError)",
                     t0 + 380).startswith('DATABASE PROBLEM\n')
    assert _spooled(spool)[0].splitlines()[2] == (
        "PostgreSQL can't be reached or queried (OperationalError).")


def test_check_health_follows_the_settings(spool, monkeypatch):
    calls = []
    monkeypatch.setattr(ops, 'dashboard_problem', lambda: calls.append('app') or None)
    monkeypatch.setattr(ops, 'database_problem', lambda: calls.append('db') or None)
    ops.check_health()
    assert calls == ['app', 'db']
    monkeypatch.setattr(ops, 'settings', lambda: OperationsAlerts(app_down=False))
    calls.clear()
    ops.check_health()
    assert calls == ['db']


def test_the_dashboard_check(monkeypatch):
    from types import SimpleNamespace

    import httpx
    def refused(*a, **k):
        raise httpx.ConnectError('connection refused to http://secret')
    monkeypatch.setattr(httpx, 'get', refused)
    assert ops.dashboard_problem() == "isn't answering (ConnectError)"
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: SimpleNamespace(
        text='ok', raise_for_status=lambda: None))
    assert ops.dashboard_problem() is None
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: SimpleNamespace(
        text='starting', raise_for_status=lambda: None))
    assert ops.dashboard_problem() == 'answers but reports itself unhealthy'


def test_an_unreachable_database_is_named_by_error_type_only(monkeypatch):
    monkeypatch.setenv('IGS_DATABASE_URL', 'postgresql://igs:SECRETPW@127.0.0.1:1/igs')
    problem = ops.database_problem()
    assert problem.startswith("can't be reached or queried (") and 'SECRETPW' not in problem


@pytest.mark.db
def test_a_missing_migration_is_a_database_problem(db_conn, monkeypatch):
    import os
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    assert ops.database_problem() is None
    last = db_conn.execute('select max(version) from schema_migrations').fetchone()[0]
    db_conn.execute('delete from schema_migrations where version = %s', (last,))
    db_conn.commit()
    assert ops.database_problem() == (f'is missing migration {last}: run the deploy script '
                                      'or `uv run igs db migrate`')
