"""Bounded, resumable discovery of NSE's per-symbol shareholding archive."""
from __future__ import annotations

import datetime as dt
from urllib.parse import urlencode

from igs.ingest.http import FetchError
from igs.ingest.jobs import JobResult, fetch_and_load, ingest_documents
from igs.timeutil import utc_now

LOCK = 739182436


def candidates(conn, limit=10):
    if not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    return conn.execute('''select c.company_id,i.id_value
        from company c join lateral (
            select i.id_value from security_identifier i join security s using(security_id)
            where s.company_id=c.company_id and i.id_type='NSE_SYMBOL'
              and i.valid_from<=current_date and (i.valid_to is null or i.valid_to>current_date)
            order by i.valid_from desc limit 1) i on true
        left join ownership_backfill b using(company_id)
        left join lateral (select composite from score_result where company_id=c.company_id
            order by run_id desc limit 1) r on true
        where b.next_attempt_at is null or b.next_attempt_at<=now()
        order by (exists(select 1 from ai_call a where a.company_id=c.company_id
            and a.action in ('buy','sell') and a.created_at>now()-interval '90 days')
            or exists(select 1 from broker_call a where a.company_id=c.company_id
            and a.stance in ('buy','sell') and a.called_on>=current_date-90)) desc,
            r.composite desc nulls last,c.company_id limit %s''',(limit,)).fetchall()


def run(ctx, limit=10, document_limit=100):
    if not 1 <= document_limit <= 500:
        raise ValueError('document_limit must be between 1 and 500')
    conn=ctx.conn
    if not conn.execute('select pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0]:
        return []
    conn.commit()
    results=[]
    try:
        spec=ctx.sources.get('nse_shareholding_index')
        for cid,symbol in candidates(conn,limit):
            # Same verified source/schema; symbol exposes history hidden by the master.
            url=spec.url+'&'+urlencode({'symbol':symbol})
            try:
                result=fetch_and_load(ctx,spec,url,{'symbol':symbol})
            except FetchError:
                conn.rollback()
                result=JobResult(spec.id,url,None,0,None,'archive request failed')
            results.append(result)
            error=None if result.http_status==200 else f'HTTP {result.http_status}'
            conn.execute('''insert into ownership_backfill(company_id,next_attempt_at,last_error)
                values(%s,now()+%s*interval '1 hour',%s) on conflict(company_id) do update
                set checked_at=now(),next_attempt_at=excluded.next_attempt_at,
                last_error=excluded.last_error''',(cid,24 if error else 168,error))
            conn.commit()
            if error:
                break  # no repeated requests against a refused endpoint
        results.extend(ingest_documents(ctx,'shareholding',document_limit,
            since=utc_now().date()-dt.timedelta(days=730),newest_first=True))
        conn.commit()
        return results
    finally:
        conn.rollback()
        conn.execute('select pg_advisory_unlock(%s)',(LOCK,))
        conn.commit()
