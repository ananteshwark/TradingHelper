"""Operational research audit and bounded replay/backfill, using verified ingestion paths."""
from __future__ import annotations

import json
from pathlib import Path

from igs.timeutil import utc_now


def audit(conn, output: Path) -> dict:
    run_id = conn.execute('select max(run_id) from score_run').fetchone()[0]
    def rows(sql, args=()):
        cur = conn.execute(sql, args)
        return [dict(zip([c.name for c in cur.description], r, strict=True)) for r in cur]
    data = {'generated_at': utc_now().isoformat(), 'run_id': run_id,
            'concepts': rows('''select concept,period_type,count(distinct company_id) companies,
                min(period_end) first_period,max(period_end) last_period from fundamental_fact
                where company_id in (select company_id from score_result where run_id=%s)
                and filed_at<=now() group by concept,period_type order by concept,period_type''',
                            (run_id,)),
            'filings': rows('''select filing_type,count(*) filings,min(period_end) first_period,
                max(period_end) last_period from filing group by filing_type'''),
            'processing_failures': rows('''select left(last_error,160) reason,count(*) documents
                from document_processing where status='failed' group by 1 order by 2 desc'''),
            'factor_coverage': rows('''select factor,count(*) companies,
                count(*) filter(where value is not null) computable,
                count(*) filter(where status='ok') peer_ranked,
                count(*) filter(where status='insufficient_peers') missing_peers
                from score_factor where run_id=%s group by factor order by factor''', (run_id,)),
            'sentiment_coverage': rows('''select count(*) companies,
                count(*) filter(where sentiment_evidence<>'{}'::jsonb) with_evidence,
                count(*) filter(where sentiment_adjustment<>0) adjusted
                from score_result where run_id=%s''', (run_id,)),
            'news_sources': rows('''select feed_name,count(*) articles,
                max(published_at) newest,count(*) filter(where tone_read_at is not null) assessed
                from broker_article group by feed_name'''),
            'interpretation': 'Absent concepts are missing disclosures or ingestion coverage, '
                              'not zero values. Company counts include stale historical facts; '
                              'factor coverage applies the actual history/freshness rules.'}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, default=str)+'\n', encoding='utf-8')
    return data


def backfill(ctx, limit: int = 25):
    """Replay cached failures, then fetch a bounded batch of listed documents.

    Uses the caller's ingestion lock, immutable raw store and source verification.
    Does not invent dates, scrape unlisted files or alter existing financial facts.
    """
    from igs.ingest.documents import replay_documents
    from igs.ingest.jobs import ingest_documents
    if not 1 <= limit <= 500:
        raise ValueError('limit must be between 1 and 500 per filing type')
    results = []
    for kind in ('financial_results', 'shareholding'):
        results.extend(replay_documents(ctx, kind, limit))
        results.extend(ingest_documents(ctx, kind, limit))
    return results
