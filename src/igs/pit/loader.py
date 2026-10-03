"""Build a PitDataset from PostgreSQL.

Loads every time-stamped table the factors, scoring and backtest read, maps
prices to securities/companies through the dated ISIN ranges, and lets
`PitDataset.from_frames` attach each table's known_at. Loading more history
than an as-of date needs is harmless: PitView filters on known_at.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import psycopg

from igs.pit.view import PitDataset


def _frame(conn: psycopg.Connection, sql: str, params: tuple, schema: dict) -> pl.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return pl.DataFrame(rows, schema=schema, orient="row")


TS = pl.Datetime("us", "UTC")


def load_dataset(conn: psycopg.Connection, start: dt.date, end: dt.date,
                 series: tuple[str, ...] = ("EQ", "BE")) -> PitDataset:
    facts = _frame(conn, """
        select fact_id, filing_id, company_id, statement_basis, period_start, period_end,
               period_type, concept, value::float8, filed_at
        from fundamental_fact where filed_at::date <= %s""", (end,),
        {"fact_id": pl.Int64, "filing_id": pl.Int64, "company_id": pl.Int64,
         "statement_basis": pl.Utf8, "period_start": pl.Date, "period_end": pl.Date,
         "period_type": pl.Utf8, "concept": pl.Utf8, "value": pl.Float64, "filed_at": TS})
    prices = _frame(conn, """
        select si.security_id, s.company_id, p.trade_date, p.isin, p.symbol, p.series,
               p.open::float8, p.high::float8, p.low::float8, p.close::float8,
               p.prev_close::float8, p.volume, p.turnover_inr::float8, p.delivery_qty,
               p.delivery_pct::float8
        from price_eod p
        join security_identifier si on si.id_type = 'ISIN' and si.id_value = p.isin
         and p.trade_date >= si.valid_from and (si.valid_to is null or p.trade_date < si.valid_to)
        join security s on s.security_id = si.security_id
        where p.exchange = 'NSE' and p.series = any(%s) and p.trade_date between %s and %s""",
        (list(series), start, end),
        {"security_id": pl.Int64, "company_id": pl.Int64, "trade_date": pl.Date,
         "isin": pl.Utf8, "symbol": pl.Utf8, "series": pl.Utf8, "open": pl.Float64,
         "high": pl.Float64, "low": pl.Float64, "close": pl.Float64, "prev_close": pl.Float64,
         "volume": pl.Int64, "turnover_inr": pl.Float64, "delivery_qty": pl.Int64,
         "delivery_pct": pl.Float64})
    cas = _frame(conn, """
        select ca_id, security_id, action_type, ex_date, announced_at, fv_old::float8,
               fv_new::float8, ratio_a::float8, ratio_b::float8, issue_price::float8,
               cash_per_share::float8
        from corporate_action where security_id is not null and ex_date <= %s""", (end,),
        {"ca_id": pl.Int64, "security_id": pl.Int64, "action_type": pl.Utf8, "ex_date": pl.Date,
         "announced_at": TS, "fv_old": pl.Float64, "fv_new": pl.Float64, "ratio_a": pl.Float64,
         "ratio_b": pl.Float64, "issue_price": pl.Float64, "cash_per_share": pl.Float64})
    shp = _frame(conn, """
        select filing_id, company_id, period_end, category, shares::float8,
               pct_of_total::float8, pledged_shares::float8, pledged_pct::float8,
               holders::float8, filed_at
        from shareholding where filed_at::date <= %s""", (end,),
        {"filing_id": pl.Int64, "company_id": pl.Int64, "period_end": pl.Date,
         "category": pl.Utf8, "shares": pl.Float64, "pct_of_total": pl.Float64,
         "pledged_shares": pl.Float64, "pledged_pct": pl.Float64, "holders": pl.Float64,
         "filed_at": TS})
    idx = _frame(conn, """select index_name, trade_date, close::float8 from index_price
                          where trade_date between %s and %s""", (start, end),
                 {"index_name": pl.Utf8, "trade_date": pl.Date, "close": pl.Float64})
    industry = _frame(conn, """select company_id, macro_sector, sector, industry, basic_industry,
                                      valid_from from industry_classification""", (),
                      {"company_id": pl.Int64, "macro_sector": pl.Utf8, "sector": pl.Utf8,
                       "industry": pl.Utf8, "basic_industry": pl.Utf8, "valid_from": pl.Date})
    surveillance = _frame(conn, """
        select s.measure, s.list_name, s.symbol, s.stage, s.effective_from, sec.company_id
        from surveillance_snapshot s
        left join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = s.symbol
         and s.effective_from >= si.valid_from
         and (si.valid_to is null or s.effective_from < si.valid_to)
        left join security sec on sec.security_id = si.security_id""", (),
        {"measure": pl.Utf8, "list_name": pl.Utf8, "symbol": pl.Utf8, "stage": pl.Utf8,
         "effective_from": pl.Date, "company_id": pl.Int64})
    announcements = _frame(conn, """
        select a.ann_id, sec.company_id, a.symbol, a.filed_at, a.category, a.subject,
               a.attachment_url, a.industry_label
        from announcement a
        left join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = a.symbol
         and a.filed_at::date >= si.valid_from
         and (si.valid_to is null or a.filed_at::date < si.valid_to)
        left join security sec on sec.security_id = si.security_id
        where a.filed_at::date <= %s""", (end,),
        {"ann_id": pl.Int64, "company_id": pl.Int64, "symbol": pl.Utf8, "filed_at": TS,
         "category": pl.Utf8, "subject": pl.Utf8, "attachment_url": pl.Utf8,
         "industry_label": pl.Utf8})
    insider = _frame(conn, """
        select t.insider_trade_id, sec.company_id, t.symbol, t.person_name, t.insider_role,
               t.security_type, t.side, t.open_market, t.quantity::float8,
               t.value_inr::float8, t.trade_from, t.filed_at, t.submission_type
        from insider_trade t
        left join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = t.symbol
         and t.filed_at::date >= si.valid_from
         and (si.valid_to is null or t.filed_at::date < si.valid_to)
        left join security sec on sec.security_id = si.security_id
        where t.filed_at::date <= %s""", (end,),
        {"insider_trade_id": pl.Int64, "company_id": pl.Int64, "symbol": pl.Utf8,
         "person_name": pl.Utf8, "insider_role": pl.Utf8, "security_type": pl.Utf8,
         "side": pl.Utf8, "open_market": pl.Boolean, "quantity": pl.Float64,
         "value_inr": pl.Float64, "trade_from": pl.Date, "filed_at": TS,
         "submission_type": pl.Utf8})
    filings = _frame(conn, """
        select filing_id, company_id, filing_system, filing_type, period_end, statement_basis,
               filed_at, source_url, results_format, audit_opinion
        from filing where filed_at::date <= %s""", (end,),
        {"filing_id": pl.Int64, "company_id": pl.Int64, "filing_system": pl.Utf8,
         "filing_type": pl.Utf8, "period_end": pl.Date, "statement_basis": pl.Utf8,
         "filed_at": TS, "source_url": pl.Utf8, "results_format": pl.Utf8,
         "audit_opinion": pl.Utf8})
    geopolitical = _frame(conn, """select a.assessment_id, a.news_id, a.company_id,
        a.impact, a.confidence, a.rationale, a.evidence, a.channel, a.model,
        n.url, n.title, n.published_at, n.received_at, a.assessed_at,
        e->>'description', e->>'source_url'
        from geopolitical_assessment a join geopolitical_news n using (news_id)
        cross join lateral jsonb_array_elements(n.companies) e
        where (e->>'company_id')::bigint = a.company_id
          and a.assessed_at < (%s::date + interval '1 day') at time zone 'Asia/Kolkata'""",
        (end,), {"assessment_id": pl.Int64, "news_id": pl.Int64, "company_id": pl.Int64,
                 "impact": pl.Float64, "confidence": pl.Float64, "rationale": pl.Utf8,
                 "evidence": pl.Utf8, "channel": pl.Utf8, "model": pl.Utf8,
                 "url": pl.Utf8, "title": pl.Utf8, "published_at": TS,
                 "received_at": TS, "assessed_at": TS,
                 "exposure": pl.Utf8, "exposure_url": pl.Utf8})
    # Brokers' calls and news tone (igs.sentiment): only matched companies can move a
    # score. broker_key is the broker's name as normalised for the call's dedupe_key, so
    # "JM Financial" and "JM FINANCIAL LTD" are one broker.
    broker_calls = _frame(conn, """
        select broker_call_id, company_id, split_part(dedupe_key, '|', 2), broker, stance,
               rating, kind, target_price::float8, called_on, source, url, created_at
        from broker_call
        where company_id is not null and called_on <= %s
          and created_at < (%s::date + interval '1 day') at time zone 'Asia/Kolkata'""",
        (end, end), {"broker_call_id": pl.Int64, "company_id": pl.Int64,
                     "broker_key": pl.Utf8, "broker": pl.Utf8, "stance": pl.Utf8,
                     "rating": pl.Utf8, "kind": pl.Utf8, "target_price": pl.Float64,
                     "called_on": pl.Date, "source": pl.Utf8, "url": pl.Utf8,
                     "created_at": TS})
    news_tone = _frame(conn, """
        select t.tone_id, t.article_id, t.company_id, t.tone::float8, t.confidence::float8,
               t.reason, t.quote, t.model, a.url, a.title, a.feed_name, a.published_at,
               a.received_at, t.assessed_at
        from stock_news_tone t join broker_article a using (article_id)
        where t.company_id is not null
          and t.assessed_at < (%s::date + interval '1 day') at time zone 'Asia/Kolkata'""",
        (end,), {"tone_id": pl.Int64, "article_id": pl.Int64, "company_id": pl.Int64,
                 "tone": pl.Float64, "confidence": pl.Float64, "reason": pl.Utf8,
                 "quote": pl.Utf8, "model": pl.Utf8, "url": pl.Utf8, "title": pl.Utf8,
                 "feed_name": pl.Utf8, "published_at": TS, "received_at": TS,
                 "assessed_at": TS})
    return PitDataset.from_frames(facts=facts, prices=prices, corporate_actions=cas,
                                  shareholding=shp, index_prices=idx, industry=industry,
                                  surveillance=surveillance, announcements=announcements,
                                  filings=filings, insider_trades=insider,
                                  geopolitical=geopolitical, broker_calls=broker_calls,
                                  news_tone=news_tone, screener_facts=_screener(conn, end))


def _screener(conn, end):
    from igs.pit.screener import load
    return load(conn, end)
