"""Observed Screener quarters as a lower-priority, dated scoring fallback."""
from __future__ import annotations

import datetime as dt

import polars as pl

from igs.pit.view import FACT_KEY, latest_versions

# Values are Rs crore. Do not map Screener's operating expenses to total expenses:
# the latter include financing/depreciation and would distort EBITDA.
LINES = {'Sales': 'revenue', 'Revenue': 'revenue', 'Interest earned': 'interest_earned',
         'Net profit': 'pat', 'Profit before tax': 'pbt', 'Other Income': 'other_income',
         'Interest': 'finance_costs', 'Depreciation': 'depreciation',
         'Operating Profit': 'operating_profit'}
TS = pl.Datetime('us','UTC')
SCHEMA = {'fact_id':pl.Int64,'filing_id':pl.Int64,'company_id':pl.Int64,
          'statement_basis':pl.Utf8,'period_start':pl.Date,'period_end':pl.Date,
          'period_type':pl.Utf8,'concept':pl.Utf8,'value':pl.Float64,'filed_at':TS,
          'source_fetch_id':pl.Utf8}


def load(conn, end: dt.date) -> pl.DataFrame:
    rows=conn.execute('''select c.context_id,e.company_id,c.statement_basis,e.period_label,
        e.field,e.value_num::float8,greatest(p.fetched_at,c.verified_at),p.fetch_id
        from screener_enrichment e join raw_payload p on p.fetch_id=e.source_fetch_id
        join screener_export_context c on c.source_fetch_id=e.source_fetch_id
            and c.company_id=e.company_id
        where e.section='Quarters' and e.value_num is not null and e.field=any(%s)
          and greatest(p.fetched_at,c.verified_at) <
            (%s::date+interval '1 day') at time zone 'Asia/Kolkata'
        order by c.context_id,e.period_label,e.field''',(list(LINES),end)).fetchall()
    records=[]
    for ctx,cid,basis,period,line,value,observed,fetch in rows:
        try:
            period=dt.date.fromisoformat(period)
        except (ValueError,TypeError):
            continue
        if period>end:
            continue
        # Stable namespace distinct from positive exchange fact IDs. A quarter-line
        # key uses its calendar month and mapped concept, not its position in a batch.
        concept=LINES[line]
        key=(period.year*12+period.month)*16+list(dict.fromkeys(LINES.values())).index(concept)
        records.append((-ctx*10_000_000-key,None,cid,basis,None,period,'Q',concept,
                        value*10_000_000,observed,fetch))
    return pl.DataFrame(records,schema=SCHEMA,orient='row')


def merge(official: pl.DataFrame, fallback: pl.DataFrame, day: dt.date) -> pl.DataFrame:
    """Filter knowledge times before calling. Prefer the official basis and values."""
    fallback=fallback.filter(pl.col('period_end')<=day)
    if fallback.is_empty():
        return official
    # A company's exchange reporting basis determines eligible fallback history.
    q=official.filter(pl.col('period_type')=='Q')
    basis=(q.group_by('company_id').agg(
        pl.when((pl.col('statement_basis')=='consolidated').any())
        .then(pl.lit('consolidated')).otherwise(pl.lit('standalone')).alias('_basis')))
    fallback=(fallback.join(basis,on='company_id',how='left')
        .filter(pl.col('_basis').is_null() | (pl.col('statement_basis')==pl.col('_basis')))
        .drop('_basis'))
    fallback=latest_versions(fallback).join(official.select(FACT_KEY),on=FACT_KEY,how='anti')
    return pl.concat([official,fallback],how='diagonal_relaxed')


def quarters(conn, as_of, company_id=None):
    """The same preferred-basis quarterly inputs for coverage and stock detail screens."""
    from igs.pit.view import PitDataset, PitView
    rows=conn.execute('''select fact_id,filing_id,company_id,statement_basis,period_start,
        period_end,period_type,concept,value::float8,filed_at,null::text
        from fundamental_fact where period_type='Q' and filed_at<=%s
        and (%s::bigint is null or company_id=%s)''',(as_of,company_id,company_id)).fetchall()
    official=pl.DataFrame(rows,schema=SCHEMA,orient='row')
    supplement=load(conn,as_of.date())
    if company_id is not None:
        supplement=supplement.filter(pl.col('company_id')==company_id)
    facts=PitView(PitDataset.from_frames(facts=official,screener_facts=supplement),as_of).facts()
    basis=(facts.group_by('company_id').agg(
        pl.when((pl.col('statement_basis')=='consolidated').any())
        .then(pl.lit('consolidated')).otherwise(pl.lit('standalone')).alias('statement_basis')))
    return facts.join(basis,on=['company_id','statement_basis'])
