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
    c.execute('update operational_notification set next_attempt_at=now()')
    c.commit()
    sent = []
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 1
    assert 'trading_holiday: 1' in sent[0]
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 0


@pytest.mark.db
def test_data_issues_only_warnings_and_errors_and_no_secrets(db_conn):
    from igs.dq import DQLog
    dq = DQLog()
    for severity in ('info','warn','error'):
        dq.emit(severity, 'test_category', 'PRIVATE secret-token')
    dq.persist(db_conn)
    db_conn.commit()
    sent = []
    ops.deliver_database(db_conn, lambda t: sent.append(t) or True)
    assert len(sent) == 1
    assert 'warn: test_category: 1' in sent[0]
    assert 'error: test_category: 1' in sent[0]
    assert 'PRIVATE' not in sent[0] and 'secret-token' not in sent[0]


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
