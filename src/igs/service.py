"""Read/write operations shared by the API and the UI.

Everything shown to a user comes from a persisted score run (so a page can
always say which run and as-of date it reflects) or from the point-in-time
tables as of that run's date. Text fields pass the advice-language guardrail.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from typing import Any

import polars as pl
import psycopg

from igs.guardrails import DISCLAIMER, assert_no_advice_language
from igs.score.red_flags import LABELS as CHECK_LABELS

ROBUSTNESS_FIELDS = ("rank_pct", "weight_stability", "persist_hits", "persist_dates",
                     "positive_pillars", "scored_pillars", "weakest_pillar",
                     "weakest_pillar_score", "top_factor", "top_factor_share")
FIN_CONCEPTS = ["revenue", "interest_earned", "total_expenses", "finance_costs", "depreciation",
                "other_income", "pbt", "pat", "pat_owners"]


class NotFound(LookupError):
    pass


def _rows(conn: psycopg.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


# --------------------------------------------------------------------------- runs


def runs(conn, limit: int = 20) -> list[dict]:
    rows = _rows(conn, """select run_id, as_of, created_at, dropped_factors, dq_summary,
                                 ic_status_generated_at, health
                          from score_run order by run_id desc limit %s""", (limit,))
    for r in rows:   # the summary is for drift checks, not for display
        r["health_issues"] = (r.pop("health") or {}).get("issues", [])
    return rows


def resolve_run(conn, run_id: int | None) -> dict:
    cols = "run_id, as_of, created_at, coalesce(health->'issues', '[]') as health_issues"
    rows = (_rows(conn, f"select {cols} from score_run where run_id = %s", (run_id,))
            if run_id else
            _rows(conn, f"select {cols} from score_run order by run_id desc limit 1"))
    if not rows:
        raise NotFound("no score run yet; run `igs score` first")
    return rows[0]


# --------------------------------------------------------------------------- rankings


FILTERS = ("tier", "sector", "industry", "bucket", "q", "min_score", "watchlist_only")


def rankings(conn, run_id: int | None = None, tier: str | None = None,
             sector: str | None = None, industry: str | None = None, bucket: str | None = None,
             q: str | None = None, min_score: float | None = None,
             watchlist_only: bool = False, limit: int | None = None) -> tuple[dict, list[dict]]:
    run = resolve_run(conn, run_id)
    sql = ["""select r.rank, r.symbol, c.name, r.tier, r.tier_reason, r.composite, r.coverage,
                     r.industry, r.sector, r.bucket, r.mcap_cr::float8 as mcap_cr,
                     r.company_id, (w.company_id is not null) as on_watchlist
              from score_result r join company c using (company_id)
              left join watchlist w using (company_id)
              where r.run_id = %s"""]
    params: list[Any] = [run["run_id"]]
    for col, val in (("r.tier", tier), ("r.sector", sector), ("r.industry", industry),
                     ("r.bucket", bucket)):
        if val:
            sql.append(f"and {col} = %s")
            params.append(val)
    if q:
        sql.append("and (r.symbol ilike %s or c.name ilike %s)")
        params += [f"%{q}%", f"%{q}%"]
    if min_score is not None:
        sql.append("and r.composite >= %s")
        params.append(min_score)
    if watchlist_only:
        sql.append("and w.company_id is not null")
    sql.append("order by r.rank nulls last, r.composite desc nulls last, r.symbol")
    if limit:
        sql.append("limit %s")
        params.append(limit)
    return run, _rows(conn, " ".join(sql), tuple(params))


def rankings_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    buf.write(f"# {DISCLAIMER}\n")
    if rows:
        pl.DataFrame(rows).drop("company_id").write_csv(buf)
    return buf.getvalue()


def facets(conn, run_id: int | None = None) -> dict[str, list[str]]:
    run = resolve_run(conn, run_id)
    out = {}
    for col in ("tier", "sector", "industry", "bucket"):
        out[col] = [r["v"] for r in _rows(
            conn, f"select distinct {col} as v from score_result where run_id = %s "
                  f"and {col} is not null order by 1", (run["run_id"],))]
    return out


# --------------------------------------------------------------------------- stock detail


def _company(conn, symbol: str) -> dict:
    rows = _rows(conn, """select s.company_id, c.name, si.id_value as symbol
                          from security_identifier si join security s using (security_id)
                          join company c using (company_id)
                          where si.id_type = 'NSE_SYMBOL' and upper(si.id_value) = upper(%s)
                          order by si.valid_to is null desc, si.valid_from desc limit 1""",
                 (symbol,))
    if not rows:
        raise NotFound(f"unknown symbol {symbol!r}")
    return rows[0]


def financials_8q(conn, company_id: int, as_of: dt.datetime) -> list[dict]:
    """Latest eight quarters as known at the run date (point in time)."""
    rows = _rows(conn, """
        with f as (select * from facts_as_of(%s) where company_id = %s and period_type = 'Q'
                   and concept = any(%s)),
             b as (select case when bool_or(statement_basis = 'consolidated')
                               then 'consolidated' else 'standalone' end as basis from f)
        select f.period_end, f.concept, f.value::float8 as value, f.fact_id, f.filing_id
        from f, b where f.statement_basis = b.basis
        order by f.period_end""", (as_of, company_id, FIN_CONCEPTS))
    if not rows:
        return []
    df = pl.DataFrame(rows).pivot(on="concept", index="period_end", values="value",
                                  aggregate_function="first").sort("period_end").tail(8)
    col = lambda c: pl.col(c) if c in df.columns else pl.lit(None, dtype=pl.Float64)  # noqa
    df = df.with_columns(
        pl.coalesce(col("revenue"), col("interest_earned")).alias("revenue"),
        (col("revenue") - col("total_expenses") + col("finance_costs") + col("depreciation"))
        .alias("ebitda"),
        pl.coalesce(col("pat_owners"), col("pat")).alias("pat"))
    df = df.with_columns((pl.col("ebitda") / pl.col("revenue")).alias("opm"))
    keep = ["period_end", "revenue", "ebitda", "pat", "opm", "other_income", "pbt"]
    return df.select([c for c in keep if c in df.columns]).to_dicts()


def shareholding_trend(conn, company_id: int, as_of: dt.datetime) -> list[dict]:
    rows = _rows(conn, """
        select distinct on (period_end, category) period_end, category,
               pct_of_total::float8 as pct, pledged_pct::float8 as pledged_pct, filing_id
        from shareholding where company_id = %s and filed_at <= %s
        order by period_end, category, filed_at desc""", (company_id, as_of))
    if not rows:
        return []
    df = pl.DataFrame(rows)
    wide = df.pivot(on="category", index="period_end", values="pct",
                    aggregate_function="first").sort("period_end").tail(8)
    pledge = (df.filter(pl.col("category") == "promoter")
                .select("period_end", pl.col("pledged_pct").alias("promoter_pledged_pct")))
    return wide.join(pledge, on="period_end", how="left").to_dicts()


def filings_feed(conn, company_id: int, as_of: dt.datetime, limit: int = 20) -> list[dict]:
    return _rows(conn, """
        (select filed_at, 'filing' as kind,
                filing_type || coalesce(' ' || statement_basis, '') || ' for ' || period_end
                    as title, source_url as url, filing_id as ref
         from filing where company_id = %s and filed_at <= %s)
        union all
        (select a.filed_at, 'announcement', a.category || ': ' || a.subject, a.attachment_url,
                a.ann_id
         from announcement a
         join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = a.symbol
         join security s on s.security_id = si.security_id
         where s.company_id = %s and a.filed_at <= %s)
        order by filed_at desc limit %s""", (company_id, as_of, company_id, as_of, limit))


def price_history(conn, company_id: int, as_of: dt.datetime, days: int = 400) -> list[dict]:
    return _rows(conn, """
        select p.trade_date, p.close::float8 as close
        from price_eod p
        join security_identifier si on si.id_type = 'ISIN' and si.id_value = p.isin
         and p.trade_date >= si.valid_from and (si.valid_to is null or p.trade_date < si.valid_to)
        join security s on s.security_id = si.security_id
        where s.company_id = %s and p.trade_date between %s and %s
        order by p.trade_date""",
        (company_id, (as_of - dt.timedelta(days=days)).date(), as_of.date()))


def stock_detail(conn, symbol: str, run_id: int | None = None) -> dict:
    run = resolve_run(conn, run_id)
    company = _company(conn, symbol)
    cid = company["company_id"]
    res = _rows(conn, "select * from score_result where run_id = %s and company_id = %s",
                (run["run_id"], cid))
    if not res:
        raise NotFound(f"{symbol} is not in run {run['run_id']} (outside the universe on "
                       f"{run['as_of']:%Y-%m-%d})")
    factors = _rows(conn, """select factor, pillar, status, value, z, peer_percentile,
                                    peer_level, peer_group, peer_count, contribution, detail,
                                    source_fact_ids, source_filing_ids
                             from score_factor where run_id = %s and company_id = %s
                             order by contribution desc nulls last""", (run["run_id"], cid))
    filing_ids = sorted({i for f in factors for i in (f["source_filing_ids"] or [])})
    sources = {r["filing_id"]: r for r in _rows(
        conn, """select filing_id, filing_type, statement_basis, period_end, filed_at,
                        source_url from filing where filing_id = any(%s)""", (filing_ids,))}
    for f in factors:
        f["detail"] = json.loads(f["detail"]) if f["detail"] else {}
        f["sources"] = [sources[i] for i in (f["source_filing_ids"] or []) if i in sources]
    top5 = [f for f in factors if f["contribution"] is not None][:5]
    checks = _rows(conn, """select flag, status, severity, unavailable_blocks, message,
                                   evidence, source_ids, source_urls
                            from red_flag_result where run_id = %s and company_id = %s
                            order by (status = 'tripped') desc,
                                     (status = 'data_unavailable') desc, flag""",
                   (run["run_id"], cid))
    for c in checks:
        c["label"] = CHECK_LABELS.get(c["flag"], c["flag"].replace("_", " "))
    flags = [c for c in checks if c["severity"] == "reject"]
    cautions = [c for c in checks if c["severity"] == "caution"]
    pillars = _rows(conn, "select pillar, score, coverage from score_pillar "
                          "where run_id = %s and company_id = %s", (run["run_id"], cid))
    summary = {**res[0], "name": company["name"]}
    assert_no_advice_language(summary["explanation"])
    robustness = {k: summary.get(k) for k in ROBUSTNESS_FIELDS}
    return {"run": run, "company": summary, "pillars": pillars, "top_contributions": top5,
            "factors": factors, "red_flags": flags, "cautions": cautions,
            "robustness": robustness, "hc_blockers": summary.get("hc_blockers") or [],
            "financials_8q": financials_8q(conn, cid, run["as_of"]),
            "shareholding": shareholding_trend(conn, cid, run["as_of"]),
            "filings": filings_feed(conn, cid, run["as_of"]),
            "prices": price_history(conn, cid, run["as_of"]),
            "disclaimer": DISCLAIMER}


# --------------------------------------------------------------------------- watchlist


def watchlist(conn) -> list[dict]:
    return _rows(conn, """select w.company_id, c.name, w.added_at, w.note,
                                 (select si.id_value from security_identifier si
                                  join security s using (security_id)
                                  where s.company_id = w.company_id and si.id_type = 'NSE_SYMBOL'
                                  order by si.valid_to is null desc, si.valid_from desc
                                  limit 1) as symbol
                          from watchlist w join company c using (company_id)
                          order by w.added_at""")


def watchlist_add(conn, symbol: str, note: str = "") -> dict:
    company = _company(conn, symbol)
    with conn.cursor() as cur:
        cur.execute("""insert into watchlist (company_id, note) values (%s, %s)
                       on conflict (company_id) do update set note = excluded.note""",
                    (company["company_id"], note))
    conn.commit()
    return company


def watchlist_remove(conn, symbol: str) -> None:
    company = _company(conn, symbol)
    with conn.cursor() as cur:
        cur.execute("delete from watchlist where company_id = %s", (company["company_id"],))
    conn.commit()


# --------------------------------------------------------------------------- saved screens


def screens(conn) -> list[dict]:
    return _rows(conn, "select name, filters, updated_at from saved_screen order by name")


def screen_save(conn, name: str, filters: dict) -> None:
    unknown = set(filters) - set(FILTERS)
    if unknown:
        raise ValueError(f"unknown filter keys {sorted(unknown)}; allowed {FILTERS}")
    with conn.cursor() as cur:
        cur.execute("""insert into saved_screen (name, filters) values (%s, %s)
                       on conflict (name) do update set filters = excluded.filters,
                                                        updated_at = now()""",
                    (name, json.dumps(filters)))
    conn.commit()


def screen_delete(conn, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("delete from saved_screen where name = %s", (name,))
    conn.commit()


def screen_run(conn, name: str, run_id: int | None = None) -> tuple[dict, list[dict]]:
    rows = _rows(conn, "select filters from saved_screen where name = %s", (name,))
    if not rows:
        raise NotFound(f"no saved screen {name!r}")
    return rankings(conn, run_id, **rows[0]["filters"])


# --------------------------------------------------------------------------- assistant output
# Stored output of the optional research assistant, read without calling it.


def stored_brief(conn, run_id: int, symbol: str) -> dict | None:
    rows = _rows(conn, """select text, model, created_at from assistant_brief
                          where run_id = %s and symbol = upper(%s)
                          order by created_at desc limit 1""", (run_id, symbol))
    return rows[0] if rows else None


def announcement_notes(conn, company_id: int, as_of: dt.datetime, limit: int = 20) -> list[dict]:
    """The assistant's reading of this company's announcements filed by as_of."""
    return _rows(conn, """
        select n.filed_at, n.subject, n.category, n.materiality, n.summary, n.concerns, n.model
        from announcement_note n
        join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = n.symbol
         and n.filed_at::date >= si.valid_from
         and (si.valid_to is null or n.filed_at::date < si.valid_to)
        join security s on s.security_id = si.security_id
        where s.company_id = %s and n.filed_at <= %s
        order by n.filed_at desc limit %s""", (company_id, as_of, limit))


def insider_trades(conn, symbol: str, as_of: dt.datetime, days: int = 365) -> list[dict]:
    """Insider-trading disclosures (SEBI PIT) broadcast in the `days` before as_of."""
    return _rows(conn, """
        select filed_at, person_name, person_category, insider_role, transaction_type,
               acquisition_mode, security_type, side, open_market, quantity::float8,
               value_inr::float8, holding_after_pct::float8, trade_from, xbrl_url
        from insider_trade
        where symbol = upper(%s) and filed_at <= %s and filed_at > %s
        order by filed_at desc limit 100""", (symbol, as_of, as_of - dt.timedelta(days=days)))
