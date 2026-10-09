import datetime as dt
import logging
import sqlite3

import polars as pl
import pytest

from igs.alerts import operations as ops
from igs.call_list import frame


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


def test_errors_redacted_deduplicated_and_retryable(spool):
    handler = ops.ErrorHandler(logging.ERROR)
    try:
        raise ValueError('password=SECRET botTOKEN')
    except ValueError:
        import sys
        record = logging.LogRecord('igs.demo', logging.ERROR, 'app.py', 42,
                                   'URL token SECRET', (), sys.exc_info())
    handler.emit(record)
    handler.emit(record)
    assert ops.deliver_errors(lambda _: False) == 0
    with sqlite3.connect(spool) as conn:
        rows = conn.execute('select message,attempts from errors').fetchall()
        assert len(rows) == 1 and rows[0][1] == 1
        assert 'SECRET' not in rows[0][0] and 'ValueError' in rows[0][0]
        conn.execute('update errors set next_attempt=0')
    sent = []
    assert ops.deliver_errors(lambda text: sent.append(text) or True) == 1
    assert ops.deliver_errors(lambda text: sent.append(text) or True) == 0
    assert len(sent) == 1


@pytest.mark.db
def test_ingestion_queue_commit_rollback_duplicate_and_retry(db_conn):
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
def test_data_issues_alert_only_errors_and_no_secrets(db_conn):
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
    now = dt.datetime.now(dt.UTC)
    messages = ops.messages([(1,'quarterly_report', {'company':'Example Ltd',
        'period_end':'2026-06-30','basis':'consolidated','exchange':'NSE',
        'filed_at':now.isoformat()}, now)])
    assert len(messages) == 1 and messages[0].startswith('NEW QUARTERLY REPORT')
    assert 'Example Ltd' in messages[0]


@pytest.mark.db
def test_revisions_notify_but_noop_updates_do_not(db_conn):
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
def test_daily_ingestion_collects_all_rows_without_delaying_other_alerts(db_conn):
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
    assert ops.deliver_database(c, sender) == 2
    assert not any('INGESTION SUMMARY' in t for t in sent)
    assert any('APPLICATION DATA ISSUES' in t for t in sent)
    assert any('NEW QUARTERLY REPORT' in t for t in sent)
    c.execute("update ingestion_digest_schedule set last_sent_at=now()-interval '2 days'")
    c.commit()
    assert ops.deliver_database(c, sender) == 125  # not truncated to the old 100-event limit
    assert 'sample: 250' in sent[-1]
    c.execute('''insert into operational_notification(event_key,kind,payload)
        values('next','ingestion',%s)''', (Jsonb({'table':'sample','rows':1}),))
    c.commit()
    assert ops.deliver_database(c, sender) == 0
    assert c.execute("select count(*) from operational_notification where sent_at is null"
                     ).fetchone()[0] == 1


def test_failed_cli_command_is_reported_without_error_message(spool, monkeypatch):
    from igs import cli
    def fail(_):
        raise ValueError('private token')
    monkeypatch.setattr(cli, '_main', fail)
    with pytest.raises(ValueError):
        cli.main(['score'])
    sent = []
    ops.deliver_errors(lambda t: sent.append(t) or True)
    assert 'cli.score' in sent[0] and 'ValueError' in sent[0]
    assert 'private token' not in sent[0]


def test_failed_service_not_reannounced_hourly_but_new_failure_is(spool,monkeypatch):
    from types import SimpleNamespace

    import httpx
    monkeypatch.setattr(ops,'SERVICES',('igs-verify.service',))
    state={'id':'first','result':'exit-code'}
    monkeypatch.setattr(ops.subprocess,'run',lambda *a,**k:SimpleNamespace(
        returncode=0,stdout=f"Result={state['result']}\nInvocationID={state['id']}\n"))
    monkeypatch.setattr(httpx,'get',lambda *a,**k:SimpleNamespace(
        text='ok',raise_for_status=lambda:None))
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
def test_daily_summary_counts_unique_companies_and_waits_for_digest_time(db_conn):
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
