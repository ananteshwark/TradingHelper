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
               t.value_inr::float8, t.trade_from, t.filed_at
        from insider_trade t
        left join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = t.symbol
         and t.filed_at::date >= si.valid_from
         and (si.valid_to is null or t.filed_at::date < si.valid_to)
        left join security sec on sec.security_id = si.security_id
        where t.filed_at::date <= %s""", (end,),
        {"insider_trade_id": pl.Int64, "company_id": pl.Int64, "symbol": pl.Utf8,
         "person_name": pl.Utf8, "insider_role": pl.Utf8, "security_type": pl.Utf8,
         "side": pl.Utf8, "open_market": pl.Boolean, "quantity": pl.Float64,
         "value_inr": pl.Float64, "trade_from": pl.Date, "filed_at": TS})
    filings = _frame(conn, """
        select filing_id, company_id, filing_system, filing_type, period_end, statement_basis,
               filed_at, source_url, results_format, audit_opinion
        from filing where filed_at::date <= %s""", (end,),
        {"filing_id": pl.Int64, "company_id": pl.Int64, "filing_system": pl.Utf8,
         "filing_type": pl.Utf8, "period_end": pl.Date, "statement_basis": pl.Utf8,
         "filed_at": TS, "source_url": pl.Utf8, "results_format": pl.Utf8,
         "audit_opinion": pl.Utf8})
    return PitDataset.from_frames(facts=facts, prices=prices, corporate_actions=cas,
                                  shareholding=shp, index_prices=idx, industry=industry,
                                  surveillance=surveillance, announcements=announcements,
                                  filings=filings, insider_trades=insider)
