"""Load filing listings and XBRL documents into the point-in-time tables.

filed_at always comes from the exchange listing; ingested_at is when we loaded
it. Facts are appended; a value that differs from the latest earlier-filed
value for the same key is a restatement and is reported loudly (the new row
is still appended; nothing is overwritten).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import polars as pl
import psycopg

from igs.dq import DQLog
from igs.ingest.raw_store import FetchRecord
from igs.normalize.load import load_simple
from igs.timeutil import utc_now
from igs.xbrl.instance import XbrlError, parse_instance
from igs.xbrl.listing import params_to_ref, parse_listing
from igs.xbrl.results import TaxonomyMismatch, XbrlMappingError, extract_results
from igs.xbrl.shareholding import extract_shareholding

RESTATEMENT_TOLERANCE = 0.5   # absolute INR; XBRL values are exact


def resolve_company(conn: psycopg.Connection, symbol: str, on: dt.date) -> int | None:
    """Company for an NSE symbol as it was on a date (falls back to its latest range)."""
    with conn.cursor() as cur:
        cur.execute("""select s.company_id from security_identifier si
                       join security s using (security_id)
                       where si.id_type = 'NSE_SYMBOL' and si.id_value = %s
                       order by (si.valid_from <= %s and (si.valid_to is null or %s < si.valid_to))
                                desc, si.valid_from desc
                       limit 1""", (symbol, on, on))
        row = cur.fetchone()
    return row[0] if row else None


def load_listing(conn, content: bytes, rec: FetchRecord, options: dict[str, Any],
                 dq: DQLog) -> int:
    df = parse_listing(content, options["filing_type"], options["filing_system"],
                       options["allowed_hosts"], dq, rec.fetch_id, options.get("listing_keys"))
    return load_simple(conn, "filing_ref", df, rec.fetch_id,
                       ["exchange", "filing_system", "document_url"])


def _insert_filing(cur, company_id: int, ref: dict, rec: FetchRecord, basis: str | None,
                   period_start: dt.date | None, period_end: dt.date, taxonomy: str | None,
                   results_format: str | None, audit_opinion: str | None) -> int | None:
    cur.execute(
        """insert into filing (company_id, exchange, filing_system, filing_type, exchange_ref,
               period_start, period_end, statement_basis, filed_at, ingested_at,
               taxonomy_version, source_url, content_sha256, source_fetch_id, results_format,
               audit_opinion)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           on conflict (exchange, filing_system, content_sha256) do nothing
           returning filing_id""",
        (company_id, ref["exchange"], ref["filing_system"], ref["filing_type"],
         ref.get("exchange_ref"), period_start, period_end, basis, ref["filed_at"], utc_now(),
         taxonomy, ref["document_url"], rec.content_sha256, rec.fetch_id, results_format,
         audit_opinion))
    row = cur.fetchone()
    return row[0] if row else None


def load_results_document(conn, rec: FetchRecord, content: bytes, dq: DQLog) -> int:
    ref = params_to_ref(rec.request_params)
    try:
        rf = extract_results(parse_instance(content), dq, rec.fetch_id)
    except (XbrlError, TaxonomyMismatch, XbrlMappingError) as exc:
        dq.emit("error", "taxonomy_mismatch", f"{ref['symbol']} {ref['period_end']}: {exc}",
                fetch_id=rec.fetch_id)
        return 0
    if ref.get("basis_hint") and ref["basis_hint"] != rf.statement_basis:
        dq.emit("warn", "basis_mismatch",
                f"{ref['symbol']}: listing says {ref['basis_hint']}, XBRL says "
                f"{rf.statement_basis}; XBRL used", fetch_id=rec.fetch_id)
    if rf.period_end != ref["period_end"]:
        dq.emit("warn", "period_mismatch",
                f"{ref['symbol']}: listing period {ref['period_end']} vs XBRL {rf.period_end}",
                fetch_id=rec.fetch_id)
    company_id = resolve_company(conn, ref["symbol"], rf.period_end)
    if company_id is None:
        dq.emit("error", "filing_unmapped", f"{ref['symbol']}: no company in instrument master",
                fetch_id=rec.fetch_id)
        return 0
    if ref["filed_at"].date() < rf.period_end:
        dq.emit("error", "filed_before_period_end",
                f"{ref['symbol']}: filed {ref['filed_at']} before period end {rf.period_end}",
                fetch_id=rec.fetch_id)
        return 0
    with conn.cursor() as cur:
        filing_id = _insert_filing(cur, company_id, ref, rec, rf.statement_basis, rf.period_start,
                                   rf.period_end, rf.taxonomy_version, rf.results_format,
                                   rf.metadata.get("audit_opinion"))
        if filing_id is None:
            return 0   # same document already loaded
        _report_restatements(cur, company_id, rf.statement_basis, rf.facts, ref, dq, rec)
        now = utc_now()
        with cur.copy("""copy fundamental_fact (filing_id, company_id, statement_basis,
                             period_start, period_end, period_type, concept, source_element,
                             value, unit, decimals, filed_at, ingested_at) from stdin""") as cp:
            for f in rf.facts:
                cp.write_row((filing_id, company_id, rf.statement_basis, f["period_start"],
                              f["period_end"], f["period_type"], f["concept"],
                              f["source_element"], f["value"], f["unit"], f["decimals"],
                              ref["filed_at"], now))
    return len(rf.facts)


def _report_restatements(cur, company_id: int, basis: str, facts: list[dict], ref: dict,
                         dq: DQLog, rec: FetchRecord) -> None:
    if not facts:
        return
    cur.execute("""select distinct on (period_end, period_type, concept)
                          period_end, period_type, concept, value::float8, filed_at
                   from fundamental_fact
                   where company_id = %s and statement_basis = %s and filed_at < %s
                   order by period_end, period_type, concept, filed_at desc, fact_id desc""",
                (company_id, basis, ref["filed_at"]))
    prior = {(r[0], r[1], r[2]): (r[3], r[4]) for r in cur.fetchall()}
    for f in facts:
        old = prior.get((f["period_end"], f["period_type"], f["concept"]))
        if old and abs(old[0] - f["value"]) > RESTATEMENT_TOLERANCE:
            dq.emit("warn", "restatement",
                    f"{ref['symbol']} {basis} {f['concept']} {f['period_type']} "
                    f"{f['period_end']}: {old[0]:,.0f} -> {f['value']:,.0f}",
                    fetch_id=rec.fetch_id,
                    details={"previous_filed_at": old[1], "new_filed_at": ref["filed_at"]})


def load_shareholding_document(conn, rec: FetchRecord, content: bytes, dq: DQLog) -> int:
    ref = params_to_ref(rec.request_params)
    try:
        period_end, rows = extract_shareholding(parse_instance(content), dq, rec.fetch_id)
    except (XbrlError, XbrlMappingError) as exc:
        dq.emit("error", "taxonomy_mismatch", f"SHP {ref['symbol']} {ref['period_end']}: {exc}",
                fetch_id=rec.fetch_id)
        return 0
    company_id = resolve_company(conn, ref["symbol"], period_end)
    if company_id is None:
        dq.emit("error", "filing_unmapped", f"SHP {ref['symbol']}: no company",
                fetch_id=rec.fetch_id)
        return 0
    with conn.cursor() as cur:
        filing_id = _insert_filing(cur, company_id, ref, rec, None, None, period_end, None,
                                   None, None)
        if filing_id is None:
            return 0
        now = utc_now()
        for r in rows:
            cur.execute("""insert into shareholding (filing_id, company_id, period_end, category,
                               shares, pct_of_total, pledged_shares, pledged_pct, holders,
                               filed_at, ingested_at)
                           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (filing_id, company_id, period_end, r["category"], r.get("shares"),
                         r.get("pct_of_total"), r.get("pledged_shares"), r.get("pledged_pct"),
                         r.get("holders"), ref["filed_at"], now))
    return len(rows)


def load_document(conn, rec: FetchRecord, content: bytes, dq: DQLog) -> int:
    kind = rec.request_params.get("filing_type")
    if kind == "financial_results":
        return load_results_document(conn, rec, content, dq)
    if kind == "shareholding":
        return load_shareholding_document(conn, rec, content, dq)
    raise ValueError(f"{rec.fetch_id}: unknown filing_type {kind!r}")


def pending_refs(conn, filing_type: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Filing references whose document has not been fetched successfully yet."""
    with conn.cursor() as cur:
        cur.execute(f"""select r.exchange, r.filing_system, r.filing_type, r.symbol,
                               r.company_name, r.period_end, r.basis_hint, r.filed_at,
                               r.filed_at_precise, r.document_url, r.exchange_ref
                        from filing_ref r
                        where r.filing_type = %s and not exists (
                            select 1 from raw_payload p
                            where p.url = r.document_url and p.http_status = 200)
                        order by r.filed_at {'limit %s' if limit else ''}""",
                    (filing_type, limit) if limit else (filing_type,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def facts_frame(conn, company_ids: list[int] | None = None) -> pl.DataFrame:
    sql = """select fact_id, filing_id, company_id, statement_basis, period_start, period_end,
                    period_type, concept, value::float8 as value, filed_at
             from fundamental_fact"""
    params: tuple = ()
    if company_ids is not None:
        sql += " where company_id = any(%s)"
        params = (company_ids,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return pl.DataFrame(rows, orient="row", schema={
        "fact_id": pl.Int64, "filing_id": pl.Int64, "company_id": pl.Int64,
        "statement_basis": pl.Utf8, "period_start": pl.Date, "period_end": pl.Date,
        "period_type": pl.Utf8, "concept": pl.Utf8, "value": pl.Float64,
        "filed_at": pl.Datetime("us", "UTC")})
