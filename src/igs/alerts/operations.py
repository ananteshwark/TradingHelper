"""Committed ingestion events and redacted operational errors, delivered once per minute.

Postgres queues data events atomically with the load. A local SQLite spool keeps runtime
errors even when Postgres is unavailable. Telegram is at-least-once: a crash after remote
acceptance but before acknowledging delivery can repeat a notification.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from igs.alerts.delivery import send_telegram, telegram_ready
from igs.timeutil import IST


def enabled():
    return os.environ.get('IGS_OPERATIONAL_ALERTS') == '1'


def _spool():
    path = Path(os.environ.get('IGS_NOTIFICATION_SPOOL',
        str(Path(__file__).resolve().parents[3] / 'data/notifications/errors.sqlite3')))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=2)
    path.chmod(0o600)
    conn.execute('''create table if not exists errors (
        event_key text primary key, message text not null, sent integer not null default 0,
        attempts integer not null default 0, next_attempt real not null default 0)''')
    return conn


def _label(value):
    return re.sub(r'[^A-Za-z0-9_.:-]', '_', str(value))[:120]


def record_issue(component, error_type='Failure', *, event_id=None):
    """Never accept exception messages, URLs, environment values or tokens here.

    Identical runtime failures are suppressed for an hour, not on every UI refresh.
    Notification trouble must never replace the original exception.
    """
    if not enabled():
        return
    component, error_type = _label(component), _label(error_type)
    stamp = dt.datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')
    occurrence = event_id if event_id is not None else int(time.time()//3600)
    key = hashlib.sha256(f'{component}:{error_type}:{occurrence}'.encode()).hexdigest()
    try:
        with closing(_spool()) as conn, conn:
            conn.execute('insert or ignore into errors(event_key,message) values (?,?)',
                (key, f'APPLICATION ISSUE\n{stamp}\nComponent: {component}\n'
                 f'Type: {error_type}\nCheck the server logs for details.'))
    except Exception:  # noqa: BLE001 - even disk/DB failure must preserve original error
        pass


class ErrorHandler(logging.Handler):
    def emit(self, record):
        if record.name.startswith(('igs.alerts', 'igs.dq')):
            return
        if not record.name.startswith(('igs', 'streamlit', 'uvicorn')):
            return
        kind = (record.exc_info[0].__name__
                if record.exc_info and record.exc_info[0] else record.levelname)
        record_issue(f'{record.name}:{record.lineno}', kind)


def install_error_handler():
    if not enabled():
        return
    # Streamlit loggers do not propagate to root; attach directly to its exception logger.
    for logger in (logging.getLogger(), logging.getLogger('streamlit.error_util'),
                   logging.getLogger('streamlit.runtime.scriptrunner.script_runner')):
        if not any(isinstance(h, ErrorHandler) for h in logger.handlers):
            logger.addHandler(ErrorHandler(logging.ERROR))


def messages(rows):
    """Group pending transaction events into short messages; never send raw issue details."""
    ingestion, issues, quarters = Counter(), Counter(), []
    start = min(row[3] for row in rows).astimezone(IST)
    end = max(row[3] for row in rows).astimezone(IST)
    for _, kind, payload, _ in rows:
        if kind == 'ingestion':
            label = _label(payload['table'])
            if payload.get('operation') == 'update':
                label += ' (updated)'
            ingestion[label] += int(payload['rows'])
        elif kind == 'issue':
            issues[f"{_label(payload['severity'])}: {_label(payload['category'])}"] += \
                int(payload['count'])
        elif kind == 'quarterly_report':
            quarters.append(payload)
    result = []
    if ingestion:
        result.append('DATA INGESTION SUMMARY\n'
            f'{start:%Y-%m-%d %H:%M}–{end:%H:%M} IST\nCommitted new/updated records:\n' +
            '\n'.join(f'{name}: {count:,}' for name, count in sorted(ingestion.items())) +
            '\nCounts are records by dataset, not unique companies.')
    if issues:
        result.append('APPLICATION DATA ISSUES\n' +
            '\n'.join(f'{name}: {count:,}' for name, count in sorted(issues.items())) +
            '\nSee Data quality in the app for details.')
    for p in quarters:
        result.append('NEW QUARTERLY REPORT\n'
            f"Company: {str(p['company'])[:250]}\nQuarter ended: {p['period_end']}\n"
            f"Basis: {p['basis'] or 'not specified'}\nExchange: {p['exchange']}\n"
            f"Filed: {p['filed_at']}\nQuarterly facts have been ingested.")
    return result


def deliver_database(conn, sender=send_telegram):
    """Hourly ingestion digest; other kinds stay prompt. Failures never consume the hour."""
    total = 0
    for kind in ('issue', 'quarterly_report', 'ingestion'):
        with conn.transaction():
            if kind == 'ingestion':
                cadence = conn.execute('''select last_sent_at<=now()-interval '1 hour'
                    and next_attempt_at<=now()
                    from ingestion_digest_schedule where singleton
                    for update skip locked''').fetchone()
                if not cadence or not cadence[0]:
                    continue
            rows = conn.execute('''select notification_id,kind,payload,created_at
                from operational_notification where sent_at is null and kind=%s
                and (kind <> 'issue' or payload->>'severity'='error')
                and (kind='ingestion' or next_attempt_at<=now())
                order by notification_id limit %s
                for update skip locked''', (kind, None if kind == 'ingestion' else 100)).fetchall()
            # Quarterly reports have separate acknowledgement so one failed send does not
            # replay the already delivered reports in the batch.
            batches = [[r] for r in rows] if kind == 'quarterly_report' else [rows]
            for batch in batches:
                if not batch:
                    continue
                ids = [r[0] for r in batch]
                try:
                    for text in messages(batch):
                        if not sender(text):
                            raise RuntimeError('Telegram unavailable')
                except Exception as exc:  # noqa: BLE001 - sanitized metadata only
                    conn.execute('''update operational_notification set attempts=attempts+1,
                        last_error=%s, next_attempt_at=now()+interval '1 minute' *
                        least(60, 5*(attempts+1)) where notification_id=any(%s)''',
                        (_label(type(exc).__name__), ids))
                    if kind == 'ingestion':
                        conn.execute('''update ingestion_digest_schedule
                            set next_attempt_at=now()+interval '5 minutes' where singleton''')
                else:
                    conn.execute('''update operational_notification set sent_at=now(),
                        attempts=attempts+1,last_error=null where notification_id=any(%s)''',
                        (ids,))
                    total += len(batch)
                    if kind == 'ingestion':
                        conn.execute('update ingestion_digest_schedule set last_sent_at=now() '
                                     'where singleton')
        conn.commit()
    return total


def deliver_errors(sender=send_telegram):
    total = 0
    with closing(_spool()) as conn:
        pending = conn.execute('select event_key,message,attempts from errors '
                               'where sent=0 and next_attempt<=? limit 30',
                               (time.time(),)).fetchall()
    for key, message, attempts in pending:
        try:
            if not sender(message):
                raise RuntimeError('Telegram unavailable')
        except Exception:  # noqa: BLE001
            with closing(_spool()) as conn, conn:
                conn.execute('update errors set attempts=attempts+1,next_attempt=? '
                             'where event_key=?', (time.time()+min(3600,300*(attempts+1)), key))
        else:
            with closing(_spool()) as conn, conn:
                conn.execute('update errors set sent=1,attempts=attempts+1 where event_key=?',
                             (key,))
            total += 1
    with closing(_spool()) as conn, conn:
        conn.execute('delete from errors where sent=1 and rowid not in '
                     '(select rowid from errors order by rowid desc limit 10000)')
    return total


SERVICES = ('igs-ui.service', 'igs-daily.service', 'igs-sync.service', 'igs-news.service',
            'igs-models.service', 'igs-screener.service', 'igs-ownership.service',
            'igs-call-reviews.service', 'igs-verify.service', 'igs-intraday.service',
            'igs-intraday-deals.service')


def check_services():
    for unit in SERVICES:
        result = subprocess.run(['systemctl', '--user', 'show', unit,
                                 '--property=Result,InvocationID,ExecMainExitTimestampMonotonic'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            record_issue('service-monitor', 'StatusUnavailable')
        else:
            props = dict(line.split('=', 1) for line in result.stdout.splitlines()
                         if '=' in line)
            if props.get('Result', '') not in ('', 'success'):
                # A failed oneshot stays failed until its next run. Notify once per
                # failed invocation, not every hour that the old status is observed.
                occurrence = (props.get('InvocationID') or
                              props.get('ExecMainExitTimestampMonotonic') or 'unknown')
                record_issue(unit, 'ServiceFailed', event_id=occurrence)
    import httpx
    try:
        response = httpx.get('http://127.0.0.1:8501/_stcore/health', timeout=10)
        response.raise_for_status()
        if response.text.strip() != 'ok':
            record_issue('dashboard', 'Unhealthy')
    except Exception:  # noqa: BLE001
        record_issue('dashboard', 'Unavailable')


def run(check=False):
    if not enabled() or not telegram_ready():
        print('Operational notifications disabled or Telegram not configured')
        return 0
    import fcntl

    from igs.db import connect
    # One dispatcher, including manual invocations; errors can still be queued by other processes.
    lock_path = Path(os.environ.get('IGS_NOTIFICATION_SPOOL',
        str(Path(__file__).resolve().parents[3] / 'data/notifications/errors.sqlite3')))
    lock_path = lock_path.with_suffix('.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if check:
            check_services()
        total = deliver_errors()  # runtime/service issues are never held for the hourly digest
        try:
            with connect() as conn:
                from igs.alerts.intraday import deliver as deliver_intraday
                total += deliver_intraday(conn)
                total += deliver_database(conn)
        except Exception as exc:  # noqa: BLE001
            record_issue('notification-database', type(exc).__name__)
        total += deliver_errors()
        print(f'Delivered {total} operational notification records')
    return 0
