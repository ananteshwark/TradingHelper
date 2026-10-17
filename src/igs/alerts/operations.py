"""Telegram messages about the app itself. The dispatcher runs once a minute.

What is sent is `operations` in config/alerts.yaml. As shipped:
- the app is down: the dashboard hasn't answered for DOWN_AFTER, then again when it's back;
- the database has a problem: PostgreSQL can't be reached or queried or misses a
  migration (checked each minute, same timing), or any process hits a database error
  such as a full disk (DATABASE_ERRORS);
- a real-money order problem (ORDER_COMPONENTS);
- the daily data ingestion summary (DIGEST_AT).
Failed data jobs and data-quality errors are off.

Postgres queues data events atomically with the load. A local SQLite spool keeps runtime
errors and the health checks' state even when Postgres is unavailable. Telegram is
at-least-once: a crash after remote acceptance but before acknowledging delivery can
repeat a notification.
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


def settings():
    """`operations` in config/alerts.yaml. A file that can't be read gives the shipped
    defaults, so a bad edit never silences app-down or database messages."""
    from igs.config import OperationsAlerts, load_alerts
    try:
        return load_alerts().operations
    except Exception:  # noqa: BLE001
        return OperationsAlerts()


# Errors that mean the database itself is in trouble, whichever process meets them. Plain
# connection failures are left to the minute-by-minute check, which waits out restarts.
DATABASE_ERRORS = frozenset({
    'DiskFull', 'OutOfMemory', 'InsufficientResources', 'TooManyConnections',
    'ConfigurationLimitExceeded', 'IoError', 'DataCorrupted', 'IndexCorrupted',
    'ReadOnlySqlTransaction', 'UndefinedTable', 'UndefinedColumn', 'AdminShutdown',
    'CrashShutdown', 'CannotConnectNow'})
# Real-money intraday orders: rejected, uncertain, not squared off, exit unprotected.
ORDER_COMPONENTS = ('intraday-trade', 'intraday-approvals')
TITLES = {'database': 'DATABASE PROBLEM', 'orders': 'ORDER PROBLEM',
          'data_problems': 'APPLICATION ISSUE'}


def category(component, error_type):
    """'database', 'orders' or 'data_problems' (everything else: a data job that failed)."""
    if error_type in DATABASE_ERRORS:
        return 'database'
    if str(component).startswith(ORDER_COMPONENTS):
        return 'orders'
    return 'data_problems'


def _spool():
    path = Path(os.environ.get('IGS_NOTIFICATION_SPOOL',
        str(Path(__file__).resolve().parents[3] / 'data/notifications/errors.sqlite3')))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=2)
    path.chmod(0o600)
    conn.execute('''create table if not exists errors (
        event_key text primary key, message text not null, sent integer not null default 0,
        attempts integer not null default 0, next_attempt real not null default 0)''')
    conn.execute('''create table if not exists health (
        name text primary key, failing_since real not null, alerted_at real)''')
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
    try:
        kind = category(component, error_type)
        if not getattr(settings(), kind):
            return
        stamp = dt.datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')
        occurrence = event_id if event_id is not None else int(time.time()//3600)
        key = hashlib.sha256(f'{component}:{error_type}:{occurrence}'.encode()).hexdigest()
        with closing(_spool()) as conn, conn:
            conn.execute('insert or ignore into errors(event_key,message) values (?,?)',
                (key, f'{TITLES[kind]}\n{stamp}\nComponent: {component}\n'
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


DIGEST_AT = dt.time(21, 30)          # IST: the daily data ingestion summary


def last_digest_time(now):
    """The latest DIGEST_AT (IST) at or before `now`: a summary is due when the last one was
    sent before it, so a server that was off at DIGEST_AT catches up on its next run."""
    local = now.astimezone(IST)
    due = dt.datetime.combine(local.date(), DIGEST_AT, IST)
    return due if local >= due else due - dt.timedelta(days=1)


def companies(conn, keys):
    """Company IDs for the identifiers ingestion events keep (migration 053): 'c:' company,
    's:' security, 'i:' ISIN, 'n:' NSE symbol. Unknown identifiers are left out."""
    by = {}
    for key in keys:
        prefix, _, value = str(key).partition(':')
        by.setdefault(prefix, set()).add(value)
    found = {}
    for value in by.get('c', ()):
        if value.isdigit():
            found[f'c:{value}'] = int(value)
    queries = {
        's': 'select security_id::text, company_id from security where security_id::text = any(%s)',
        'i': """select id_value, s.company_id from security_identifier i
                join security s using (security_id) where i.id_type = 'ISIN'
                and i.id_value = any(%s)""",
        'n': """select id_value, s.company_id from security_identifier i
                join security s using (security_id) where i.id_type = 'NSE_SYMBOL'
                and i.id_value = any(%s)""",
    }
    for prefix, sql in queries.items():
        if by.get(prefix):
            for value, company_id in conn.execute(sql, (sorted(by[prefix]),)).fetchall():
                found[f'{prefix}:{value}'] = company_id
    return found


def messages(rows, resolve=None):
    """Group pending transaction events into short messages; never send raw issue details.
    `resolve` maps ingestion events' company identifiers to company IDs (see companies).
    Quarterly reports are read and sent one at a time (quarterly)."""
    ingestion, issues = Counter(), Counter()
    keys: dict[str, set] = {}
    start = min(row[3] for row in rows).astimezone(IST)
    end = max(row[3] for row in rows).astimezone(IST)
    for _, kind, payload, _ in rows:
        if kind == 'ingestion':
            label = _label(payload['table'])
            if payload.get('operation') == 'update':
                label += ' (updated)'
            ingestion[label] += int(payload['rows'])
            keys.setdefault(label, set()).update(payload.get('keys') or ())
        elif kind == 'issue':
            issues[f"{_label(payload['severity'])}: {_label(payload['category'])}"] += \
                int(payload['count'])
    result = []
    if ingestion:
        found = resolve(set().union(*keys.values())) if resolve else {}
        per = {name: {found[k] for k in ks if k in found} for name, ks in keys.items()}
        unique = set().union(*per.values())
        result.append('DATA INGESTION SUMMARY\n'
            f'{start:%d %b %Y %H:%M} – {end:%d %b %Y %H:%M} IST\n'
            f'Unique companies with new or updated data: {len(unique):,}\n'
            'Committed new/updated records by dataset (companies):\n' +
            '\n'.join(f'{name}: {count:,}' + (f' ({len(per[name]):,})' if per.get(name) else '')
                      for name, count in sorted(ingestion.items())))
    if issues:
        result.append('APPLICATION DATA ISSUES\n' +
            '\n'.join(f'{name}: {count:,}' for name, count in sorted(issues.items())) +
            '\nSee Data quality in the app for details.')
    return result


def quarterly(conn, row, sender, rule):
    """Read one new quarterly report from its figures (igs.results_read) and keep the read;
    send it only if it reads positive (all of them with rules.quarterly_results.only_positive
    off), once per company and quarter. Returns why it was not sent, or None once sent; a
    failed send raises, and the report is read again on the retry."""
    from igs import results_read as rr
    notification_id, _, payload, _ = row
    company_id = payload.get('company_id')
    period_end = rr.parse_day(payload['period_end'])
    result = rr.read(conn, company_id, period_end, payload.get('basis'),
                     rr.parse_time(payload.get('filed_at')), rule)
    why = None
    if not rule['enabled']:
        why = 'quarterly_results alerts are off'
    elif rule['only_positive'] and result['verdict'] != 'positive':
        why = f"results read {result['verdict']}"
    elif rr.already_sent(conn, company_id, period_end):
        why = 'already sent for this quarter'
    if why is None:
        filed = rr.parse_time(payload.get('filed_at'))
        text = rr.message(payload, result, rr.symbol(conn, company_id),
                          rr.positive_so_far(conn),
                          rr.below_high(conn, company_id, filed)
                          if company_id is not None and filed else None)
        if not sender(text):
            raise RuntimeError('Telegram unavailable')
    rr.keep(conn, notification_id, payload, result, why is None)
    return why


def deliver_database(conn, sender=send_telegram, now=None):
    """The daily ingestion summary, once DIGEST_AT has passed; other kinds stay prompt.
    A failed send retries without consuming the day's summary. Data issues and the summary
    are sent only if `operations` in config/alerts.yaml turns them on; otherwise they are
    marked skipped, so turning one on later starts from new events."""
    from igs import results_read
    total = 0
    now = now or dt.datetime.now(IST)
    due_after = last_digest_time(now)
    ops = settings()
    off = {'issue': None if ops.data_problems else 'data problems are off (alerts.yaml)',
           'ingestion': None if ops.ingestion_summary else
           'the ingestion summary is off (alerts.yaml)'}
    try:
        rule = results_read.rule()
    except Exception as exc:  # noqa: BLE001 - a bad alerts.yaml must not stop other alerts
        record_issue('quarterly-results-rule', type(exc).__name__)
        rule = results_read.DEFAULTS
    for kind in ('issue', 'quarterly_report', 'ingestion'):
        with conn.transaction():
            if off.get(kind):
                conn.execute('''update operational_notification set skipped=%s
                    where sent_at is null and skipped is null and kind=%s''', (off[kind], kind))
                continue
            if kind == 'ingestion':
                cadence = conn.execute('''select last_sent_at<%s and next_attempt_at<=now()
                    from ingestion_digest_schedule where singleton
                    for update skip locked''', (due_after,)).fetchone()
                if not cadence or not cadence[0]:
                    continue
            rows = conn.execute('''select notification_id,kind,payload,created_at
                from operational_notification where sent_at is null and skipped is null
                and kind=%s
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
                skipped = None
                try:
                    if kind == 'quarterly_report':
                        with conn.transaction():      # a failed read leaves the rest usable
                            skipped = quarterly(conn, batch[0], sender, rule)
                    else:
                        for text in messages(batch, lambda keys: companies(conn, keys)):
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
                    if skipped:
                        conn.execute('''update operational_notification set skipped=%s,
                            attempts=attempts+1,last_error=null
                            where notification_id=any(%s)''', (skipped, ids))
                        continue
                    conn.execute('''update operational_notification set sent_at=now(),
                        attempts=attempts+1,last_error=null where notification_id=any(%s)''',
                        (ids,))
                    total += len(batch)
                    if kind == 'ingestion':
                        conn.execute('update ingestion_digest_schedule set last_sent_at=%s '
                                     'where singleton', (now,))
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
            'igs-intraday-deals.service', 'igs-intraday-approvals.service')


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


DOWN_AFTER = 180            # seconds failing before the first message: restarts pass unseen
REMIND_EVERY = 6 * 3600     # while it stays down
DASHBOARD = 'http://127.0.0.1:8501/_stcore/health'
HEALTH = {  # name: (title, what, title when it's back, where to look)
    'app_down': ('APPLICATION DOWN', 'The dashboard', 'APPLICATION BACK UP',
                 'On the server: systemctl --user status igs-ui.service'),
    'database': ('DATABASE PROBLEM', 'PostgreSQL', 'DATABASE OK AGAIN',
                 'On the server: systemctl status postgresql'),
}


def dashboard_problem():
    """None when the dashboard answers its health check, else what is wrong."""
    import httpx
    try:
        response = httpx.get(DASHBOARD, timeout=10)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - the type only, never the message
        return f"isn't answering ({_label(type(exc).__name__)})"
    return None if response.text.strip() == 'ok' else 'answers but reports itself unhealthy'


def database_problem():
    """None when PostgreSQL answers a query and has every migration, else what is wrong."""
    import psycopg

    from igs.db import database_url, pending_migrations
    try:
        with psycopg.connect(database_url(), connect_timeout=10, autocommit=True) as conn:
            conn.execute('select 1')
            missing = pending_migrations(conn)
    except Exception as exc:  # noqa: BLE001 - the type only, never the message
        return f"can't be reached or queried ({_label(type(exc).__name__)})"
    if missing:
        return (f"is missing migration {', '.join(_label(m) for m in missing)}: run the "
                "deploy script or `uv run igs db migrate`")
    return None


def _span(seconds):
    minutes = int(seconds // 60)
    return f'{minutes} min' if minutes < 120 else f'{minutes // 60} h {minutes % 60} min'


def track(name, problem, now=None):
    """Keep check `name`'s state in the spool and queue its message: once it has failed
    for DOWN_AFTER, again every REMIND_EVERY while it stays down, and once when it works
    again. `problem` is None when it works. Returns the message queued, if any."""
    now = time.time() if now is None else now
    title, what, back, hint = HEALTH[name]
    at = lambda t: dt.datetime.fromtimestamp(t, IST).strftime('%d %b %Y %H:%M IST')  # noqa: E731
    message = None
    with closing(_spool()) as conn, conn:
        row = conn.execute('select failing_since, alerted_at from health where name=?',
                           (name,)).fetchone()
        if problem is None:
            if row and row[1] is not None:
                message = (f'{back}\n{at(now)}\n{what} is working again. It was down for '
                           f'{_span(now - row[0])}.')
            conn.execute('delete from health where name=?', (name,))
        else:
            since, alerted = row if row else (now, None)
            if (alerted is None and now - since >= DOWN_AFTER
                    or alerted is not None and now - alerted >= REMIND_EVERY):
                message = (f'{title}{", STILL" if alerted is not None else ""}\n{at(now)}\n'
                           f'{what} {problem}.\nSince {at(since)} ({_span(now - since)}).'
                           f'\n{hint}')
                alerted = now
            conn.execute('insert or replace into health values (?,?,?)',
                         (name, since, alerted))
        if message:
            key = hashlib.sha256(f'health:{name}:{now}'.encode()).hexdigest()
            conn.execute('insert or ignore into errors(event_key,message) values (?,?)',
                         (key, message))
    return message


def check_health(now=None):
    """The app-down and database checks, as `operations` in config/alerts.yaml allows."""
    ops = settings()
    if ops.app_down:
        track('app_down', dashboard_problem(), now)
    if ops.database:
        track('database', database_problem(), now)


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
            check_health()
            if settings().data_problems:
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
