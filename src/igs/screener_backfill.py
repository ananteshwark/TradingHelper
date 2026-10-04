"""Resumable prioritized Screener export downloads; never write PIT financial facts."""
from __future__ import annotations

import os

from igs.ingest.screener_download import AccessLimited, Client, DownloadError, LoginRequired
from igs.timeutil import utc_now

LOCK = 739182435


def candidates(conn, limit=10):
    if not 1 <= limit <= 500:
        raise ValueError('limit must be between 1 and 500')
    cur = conn.execute('''
      with quarters as (
        select company_id, count(distinct period_end) n from fundamental_fact
        where period_type='Q' and filed_at<=now() and period_end<=current_date
        group by company_id),
      latest_export as (
        select distinct on(e.company_id) e.company_id,e.source_fetch_id
        from screener_enrichment e join raw_payload p on p.fetch_id=e.source_fetch_id
        join screener_export_context c using(source_fetch_id,company_id)
        where e.section='Quarters' order by e.company_id,p.fetched_at desc,p.fetch_id desc),
      export_period as (
        select e.company_id,e.period_label from screener_enrichment e
        join latest_export l using(company_id,source_fetch_id)
        where e.section='Quarters' and e.value_num is not null
          and e.period_label<=current_date::text
        group by e.company_id,e.period_label
        having bool_or(e.field in ('Sales','Revenue','Interest earned'))
           and bool_or(e.field='Net profit')),
      export_quarters as (select company_id,count(*) n from export_period group by company_id),
      ai as (select distinct on(company_id) company_id,action from ai_call
             order by company_id,created_at desc,call_id desc),
      broker_latest as (select distinct on(company_id,broker) company_id,broker,stance
          from broker_call where called_on>=current_date-90 and called_on<=current_date
          order by company_id,broker,called_on desc,broker_call_id desc),
      brokers as (select company_id,bool_or(stance in ('buy','sell')) has_call
          from broker_latest group by company_id)
      select c.company_id,si.id_value as symbol,si.id_type,c.name,coalesce(q.n,0) as quarters,
          coalesce(eq.n,0) as export_quarters,
          (coalesce(ai.action in ('buy','sell'),false) or coalesce(b.has_call,false)) has_call,
          r.composite as score,coalesce(d.attempts,0) attempts
      from company c
      join lateral (select i.id_value,i.id_type from security_identifier i
          join security s using(security_id)
          where s.company_id=c.company_id and i.id_type in ('NSE_SYMBOL','BSE_CODE')
            and i.valid_from<=current_date and (i.valid_to is null or i.valid_to>current_date)
          order by (i.id_type='NSE_SYMBOL') desc,i.valid_from desc,i.id_value limit 1) si on true
      left join quarters q using(company_id)
      left join export_quarters eq using(company_id)
      left join ai using(company_id)
      left join brokers b using(company_id)
      left join lateral (select composite from score_result where company_id=c.company_id
          order by run_id desc limit 1) r on true
      left join screener_download d using(company_id)
      where coalesce(q.n,0)<8 and coalesce(eq.n,0)<8
        and (d.next_attempt_at is null or d.next_attempt_at<=now()
             or (d.status='downloaded' and not exists (
                 select 1 from screener_export_context c where c.company_id=d.company_id
                 and c.source_fetch_id=d.source_fetch_id)))
      order by has_call desc,score desc nulls last,c.company_id limit %s
      ''', (limit,))
    return [dict(zip([col.name for col in cur.description], row, strict=True)) for row in cur]


def pause(conn, reason, hours=None):
    conn.execute('''update screener_download_control set paused_reason=%s,
        retry_after=case when %s::integer is null then null else now()+%s*interval '1 hour' end
        where singleton''', (reason, hours, hours))
    conn.commit()


def run(ctx, limit=10, client=None):
    from igs.ingest.manual import import_screener_bytes
    from igs.normalize.masters import parse_screener_workbook

    conn = ctx.conn
    if not conn.execute('select pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0]:
        return {'status': 'busy', 'downloaded': 0}
    conn.commit()
    downloader = client
    completed = 0
    try:
        blocked = conn.execute('''select paused_reason from screener_download_control
            where paused_reason is not null and (retry_after is null or retry_after>now())'''
                               ).fetchone()
        if blocked:
            return {'status': blocked[0], 'downloaded': 0}
        rows = candidates(conn, limit)
        conn.commit()
        if not rows:
            return {'status': 'No companies currently need a download', 'downloaded': 0}
        email, password = os.environ.get('SCREENER_EMAIL'), os.environ.get('SCREENER_PASSWORD')
        if not email or not password:
            return {'status': 'Credentials missing: run igs screener configure', 'downloaded': 0}
        downloader = downloader or Client()
        downloader.login(email, password)
        for row in rows:
            try:
                content, url = downloader.download(row['symbol'], row['id_type'])
                book = parse_screener_workbook(content)
                # The requested company page must confirm its NSE symbol; the workbook
                # must additionally agree with the instrument-master company name.
                from igs.brokers import normalise
                # The download already verified the exact exchange code. Compare
                # with that company's name directly: a free-text search misses
                # apostrophe/spacing differences before normalization can run.
                expected = normalise(row['name']).replace(' ', '')
                actual = normalise(book['company_name'] or '').replace(' ', '')
                if not actual or actual != expected:
                    raise DownloadError('Workbook company name needs manual identity verification')
                with conn.transaction():
                    got = import_screener_bytes(conn, ctx.store, content,
                        row['symbol']+'.xlsx', ctx.dq,
                        nse_code=row['symbol'] if row['id_type']=='NSE_SYMBOL' else None,
                        bse_code=row['symbol'] if row['id_type']=='BSE_CODE' else None,
                        source_url=url)
                    if got.company_id != row['company_id']:
                        raise DownloadError('Previously imported workbook has a different company')
                    basis = getattr(downloader, 'statement_basis', None)
                    if basis in ('consolidated', 'standalone'):
                        conn.execute('''insert into screener_export_context
                            (source_fetch_id,company_id,statement_basis) values(%s,%s,%s)
                            on conflict(source_fetch_id,company_id) do nothing''',
                            (got.fetch_id,row['company_id'],basis))
                    quarterly = book['sections'].get('Quarters', {})
                    periods = quarterly.get('periods', [])
                    fields = quarterly.get('rows', {})
                    sales = next((fields[k] for k in ('Sales','Revenue','Interest earned')
                                  if k in fields), [])
                    profit = fields.get('Net profit', [])
                    count = sum(p is not None and p <= utc_now().date().isoformat()
                                and a is not None and b is not None
                                for p,a,b in zip(periods,sales,profit,strict=False))
                    conn.execute('''insert into screener_download(company_id,status,attempts,
                        next_attempt_at,export_quarters,source_fetch_id)
                        values(%s,%s,1,now()+interval '30 days',%s,%s)
                        on conflict(company_id) do update set status=excluded.status,
                        attempts=screener_download.attempts+1,last_attempt_at=now(),
                        next_attempt_at=excluded.next_attempt_at,
                        export_quarters=excluded.export_quarters,
                        source_fetch_id=excluded.source_fetch_id,last_error=null''',
                        (row['company_id'], 'downloaded' if count>=8 else 'partial',
                         count,got.fetch_id))
                conn.commit()
                completed += 1
            except (LoginRequired, AccessLimited):
                raise
            except (DownloadError, FileNotFoundError, ValueError) as exc:
                conn.rollback()
                # Unexpected parser errors can contain worksheet content; keep only their type.
                error = str(exc) if isinstance(exc, DownloadError) else type(exc).__name__
                from igs.alerts.operations import record_issue
                record_issue('screener.' + row['symbol'], type(exc).__name__)
                delay = min(168, 2 ** min(row['attempts'], 8))
                conn.execute('''insert into screener_download(company_id,status,attempts,
                    next_attempt_at,last_error) values(%s,'failed',1,now()+%s*interval '1 hour',%s)
                    on conflict(company_id) do update set status='failed',
                    attempts=screener_download.attempts+1,last_attempt_at=now(),
                    next_attempt_at=excluded.next_attempt_at,last_error=excluded.last_error''',
                    (row['company_id'],delay,error))
                conn.commit()
        ctx.dq.persist(conn)
        conn.commit()
        return {'status': 'Batch finished', 'downloaded': completed}
    except (LoginRequired, AccessLimited) as exc:
        pause(conn, str(exc), 24 if isinstance(exc, AccessLimited) else None)
        from igs.alerts.operations import record_issue
        record_issue('screener.download', type(exc).__name__)
        return {'status': str(exc), 'downloaded': completed, 'paused': True}
    finally:
        if downloader:
            downloader.close()
        conn.rollback()
        conn.execute('select pg_advisory_unlock(%s)', (LOCK,))
        conn.commit()
