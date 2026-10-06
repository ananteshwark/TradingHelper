"""Bounded Upstox scanner with per-day historical cache and persistent audit snapshots."""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import asdict

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from igs import envfile
from igs.alerts.operations import record_issue
from igs.intraday import eligibility
from igs.intraday.context import evidence
from igs.intraday.engine import Candle, evaluate, trading_window
from igs.intraday.upstox import FeedError, Upstox
from igs.timeutil import IST, utc_now

LOCK = 739182437


def token():
    # Re-read the private file so UI token renewal also reaches long-lived processes.
    values = envfile.parse(envfile.default_path().read_text()) if (
        envfile.default_path().is_file()) else {}
    return values.get('UPSTOX_ACCESS_TOKEN') or os.environ.get('UPSTOX_ACCESS_TOKEN', '')


def candidates(conn, limit=100, *, allowed_keys=None):
    if not 1 <= limit <= 100:
        raise ValueError('Scan limit must be between 1 and 100')
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''select distinct on(c.company_id) c.company_id,c.name,n.id_value symbol,
                'NSE_EQ|'||i.id_value instrument_key,
                coalesce(r.composite,0) score,
                (exists(select 1 from ai_call a where a.company_id=c.company_id
                  and a.action in ('buy','sell') and a.created_at between
                    now()-interval '30 days' and now())
                 or exists(select 1 from broker_call b where b.company_id=c.company_id
                    and b.stance in ('buy','sell') and b.called_on between
                    current_date-30 and current_date)
                 or exists(select 1 from intraday_investor_event e
                    where e.company_id=c.company_id and e.published_at between
                    now()-interval '3 days' and now())) has_call
            from company c join security s using(company_id)
            join security_listing l using(security_id)
            join security_identifier n on n.security_id=s.security_id and n.id_type='NSE_SYMBOL'
            join security_identifier i on i.security_id=s.security_id and i.id_type='ISIN'
            left join lateral (select composite from score_result r where
                r.company_id=c.company_id order by run_id desc limit 1) r on true
            where l.exchange='NSE' and l.status='active' and s.security_type='equity'
              and n.valid_from<=current_date and (n.valid_to is null or n.valid_to>current_date)
              and i.valid_from<=current_date and (i.valid_to is null or i.valid_to>current_date)
            order by c.company_id,s.security_id''')
        rows = cur.fetchall()
    if allowed_keys is not None:
        rows = [r for r in rows if r['instrument_key'] in allowed_keys]
    return sorted(rows, key=lambda r: (-r['has_call'], -r['score'], r['company_id']))[:limit]


def history(conn, feed, key, day):
    row = conn.execute('select candles from intraday_history where instrument_key=%s '
                       'and session_date=%s', (key, day)).fetchone()
    if row:
        return [Candle(dt.datetime.fromisoformat(b['start']),
                       *(b[k] for k in ('open', 'high', 'low', 'close', 'volume'))) for b in row[0]]
    bars = feed.candles(key, day=day)
    payload = json.loads(json.dumps([asdict(b) for b in bars], default=str))
    conn.execute('''insert into intraday_history(instrument_key,session_date,candles)
        values(%s,%s,%s) on conflict do nothing''', (key, day, Jsonb(payload)))
    conn.commit()
    return bars


def scan(conn, limit=100, *, feed=None, clock=utc_now, pause=time.sleep):
    if not 1 <= limit <= 100:
        raise ValueError('Scan limit must be between 1 and 100')
    if not conn.execute('select pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0]:
        return {'status': 'running', 'message': 'Another scanner is running'}
    conn.commit()
    scan_id = None
    own_feed = feed is None
    try:
        now = clock()
        status = 'running'
        message = ''
        if own_feed and not token():
            status, message = 'unconfigured', 'Save an Upstox access token in Intraday settings'
        elif not trading_window(now):
            status, message = 'closed', 'Outside the 09:30–15:15 IST entry window'
        scan_id = conn.execute('insert into intraday_scan(status,message) values(%s,%s) '
                               'returning scan_id', (status, message)).fetchone()[0]
        conn.commit()
        if status != 'running':
            conn.execute('update intraday_scan set finished_at=now() where scan_id=%s', (scan_id,))
            conn.commit()
            return {'status': status, 'message': message}
        feed = feed or Upstox(token())
        ticks = eligibility.tick_sizes(now=now)
        benchmark = feed.candles('NSE_INDEX|Nifty 50')
        count = 0
        deadline = time.monotonic() + 180
        for stock in candidates(conn, limit, allowed_keys=ticks):
            now = clock()
            if not trading_window(now) or time.monotonic() >= deadline:
                break
            key = stock['instrument_key']
            past = history(conn, feed, key, now.astimezone(IST).date())
            pause(.25)  # at most four requests/sec; no retry storm on refusal
            bars = feed.candles(key)
            observed = clock()
            result = evaluate(bars, past, observed, benchmark,
                              evidence(conn, stock['company_id'], stock['symbol'], observed),
                              tick=ticks[key])
            conn.execute('''insert into intraday_signal(scan_id,company_id,symbol,
                instrument_key,observed_at,result) values(%s,%s,%s,%s,%s,%s)''',
                (scan_id, stock['company_id'], stock['symbol'], key, observed, Jsonb(result)))
            count += 1
            conn.execute('update intraday_scan set scanned=%s where scan_id=%s', (count, scan_id))
            conn.commit()
            pause(.25)
        conn.execute("update intraday_scan set status='complete',finished_at=now() "
                     'where scan_id=%s', (scan_id,))
        # Baselines are replaceable daily; signal snapshots retain their input readings.
        conn.execute('delete from intraday_history where session_date<current_date-35')
        conn.commit()
        return {'status': 'complete', 'scanned': count}
    except Exception as exc:
        conn.rollback()
        message = str(exc) if isinstance(exc, FeedError) else 'Scanner failed; check server logs'
        if scan_id:
            conn.execute("update intraday_scan set status='failed',message=%s,finished_at=now() "
                         'where scan_id=%s', (message, scan_id))
            conn.commit()
        record_issue('intraday', type(exc).__name__)
        raise
    finally:
        if own_feed and feed:
            feed.close()
        conn.rollback()
        conn.execute('select pg_advisory_unlock(%s)', (LOCK,))
        conn.commit()


def latest(conn):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('select * from intraday_scan order by scan_id desc limit 1')
        run = cur.fetchone()
        if not run:
            return None, []
        cur.execute('''select s.*,c.name from intraday_signal s join company c using(company_id)
            where scan_id=%s order by symbol''', (run['scan_id'],))
        return run, cur.fetchall()
