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
from pydantic import BaseModel, ConfigDict, FiniteFloat

from igs.guardrails import DISCLAIMER, assert_no_advice_language
from igs.score.red_flags import LABELS as CHECK_LABELS
from igs.timeutil import utc_now

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
                                 ic_status_generated_at, health, market_sentiment
                          from score_run order by run_id desc limit %s""", (limit,))
    for r in rows:   # the summary is for drift checks, not for display
        health = r.pop("health") or {}
        r["health_issues"] = health.get("issues", [])
        r["universe"] = health.get("universe")          # runs before 2026-09-25 lack it
    return rows


def readiness(conn: psycopg.Connection, min_quarters: int) -> dict[str, Any]:
    """How much of what a score run needs is loaded. A company is ranked only with
    `min_quarters` quarters of results and a shareholding filing (its share count gives
    the market cap). `listed` counts filings NSE's listings named; `loaded` the documents
    fetched and parsed."""
    prices = _rows(conn, """select count(distinct trade_date) as days, max(trade_date) as latest,
                                   count(distinct symbol) as symbols
                            from price_eod where exchange = 'NSE'""")[0]
    listed = {r["filing_type"]: r["n"] for r in _rows(
        conn, "select filing_type, count(*) as n from filing_ref group by filing_type")}
    pending = {r["filing_type"]: r["n"] for r in _rows(   # as xbrl.load.pending_refs
        conn, """select r.filing_type, count(*) as n from filing_ref r
                  where not exists (select 1 from raw_payload p
                                    where p.url = r.document_url and p.http_status = 200)
                  group by r.filing_type""")}
    results = _rows(conn, """select count(*) filter (where n >= %s) as enough, count(*) as some,
                                     coalesce(max(n), 0) as most
                              from (select company_id, count(distinct period_end) as n
                                    from fundamental_fact where period_type = 'Q'
                                    group by company_id) q""", (min_quarters,))[0]
    holders = _rows(conn, "select count(distinct company_id) as n from shareholding")[0]["n"]
    return {"prices": prices, "listed": listed, "pending": pending,
            "results_enough": results["enough"], "results_some": results["some"],
            "results_most": results["most"], "shareholding": holders,
            "min_quarters": min_quarters}


def resolve_run(conn, run_id: int | None) -> dict:
    cols = ("run_id, as_of, created_at, coalesce(health->'issues', '[]') as health_issues, "
            "market_sentiment")
    rows = (_rows(conn, f"select {cols} from score_run where run_id = %s", (run_id,))
            if run_id else
            _rows(conn, f"select {cols} from score_run order by run_id desc limit 1"))
    if not rows:
        raise NotFound("no score run yet; run `igs score` first")
    return rows[0]


# --------------------------------------------------------------------------- rankings


FILTERS = ("tier", "sector", "industry", "bucket", "q", "min_score", "watchlist_only")


class ScreenFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tier: str | None = None
    sector: str | None = None
    industry: str | None = None
    bucket: str | None = None
    q: str | None = None
    min_score: FiniteFloat | None = None
    watchlist_only: bool = False


def rankings(conn, run_id: int | None = None, tier: str | None = None,
             sector: str | None = None, industry: str | None = None, bucket: str | None = None,
             q: str | None = None, min_score: float | None = None,
             watchlist_only: bool = False, limit: int | None = None) -> tuple[dict, list[dict]]:
    run = resolve_run(conn, run_id)
    sql = ["""select r.rank, r.symbol, c.name, r.tier, r.tier_reason, r.composite, r.coverage,
                     r.industry, r.sector, r.bucket, r.mcap_cr::float8 as mcap_cr,
                     r.company_id, coalesce(r.growth_profile->>'profile', 'Not assessed')
                         as growth_profile,
                     (w.company_id is not null) as on_watchlist
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
        match, terms = _match_sql("r.symbol", "c.name", q)
        sql.append(f"and {match}")
        params += terms
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


# --------------------------------------------------------------------------- finding a stock

NAME_SUFFIXES = {"ltd", "ltd.", "limited"}


def _terms(q: str) -> list[str]:
    """The words of a search. "Ltd" and "Limited" are left out, so "Infosys Ltd" finds
    "Infosys Limited", unless nothing else was typed."""
    words = q.lower().split()
    return [w for w in words if w not in NAME_SUFFIXES] or words


def _literal(word: str) -> str:
    """`word` for LIKE, with its own % and _ matched as themselves."""
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _match_sql(symbol_col: str, name_col: str, q: str) -> tuple[str, list[str]]:
    """Every word of `q` in the symbol or the name, in any order and case: "hdfc bank"
    finds HDFCBANK and "HDFC Bank Limited"."""
    like = [f"%{_literal(w)}%" for w in _terms(q)]
    sql = " and ".join(f"({symbol_col} ilike %s or {name_col} ilike %s)" for _ in like)
    return f"({sql or 'true'})", [p for w in like for p in (w, w)]


def companies(conn, run_id: int | None = None, q: str | None = None,
              limit: int | None = None) -> list[dict]:
    """Companies to pick from by name or symbol: those scored in the run (under the symbol
    the run used), every other company with a current NSE symbol (in_run false), and new
    listings in NSE's latest equity list whose first price file isn't loaded yet
    (company_id None). Without `q`, sorted by name; with it, only matches, the exact
    symbol first, then symbols and names that start with it."""
    run = _rows(conn, "select run_id from score_run order by run_id desc limit 1") \
        if run_id is None else [{"run_id": run_id}]
    sql = ["""with in_run as (
                  select r.company_id, r.symbol, c.name, true as in_run
                  from score_result r join company c using (company_id) where r.run_id = %s),
              listed as (
                  select distinct on (s.company_id) s.company_id, si.id_value as symbol,
                         c.name, false as in_run
                  from security_identifier si join security s using (security_id)
                  join company c using (company_id)
                  where si.id_type = 'NSE_SYMBOL' and si.valid_to is null
                    and s.company_id not in (select company_id from in_run)
                  order by s.company_id, si.valid_from desc),
              new_listing as (
                  select null::bigint as company_id, e.symbol, e.company_name as name,
                         false as in_run
                  from nse_equity_list e
                  where e.snapshot_date = (select max(snapshot_date) from nse_equity_list)
                    and not exists (select 1 from security_identifier si
                                    where si.id_type = 'ISIN' and si.id_value = e.isin))
              select * from (select * from in_run union all
                             select * from listed
                             where upper(symbol) not in (select upper(symbol) from in_run)
                             union all
                             select * from new_listing
                             where upper(symbol) not in (select upper(symbol) from in_run
                                                         union select upper(symbol)
                                                         from listed)) x
              where true"""]
    params: list[Any] = [run[0]["run_id"] if run else None]
    order = "name, symbol"
    if q and q.strip():
        match, terms = _match_sql("symbol", "name", q)
        sql.append(f"and {match}")
        params += terms
        order = ("upper(symbol) = upper(%s) desc, symbol ilike %s desc, name ilike %s desc, "
                 + order)
        start = _literal(_terms(q)[0])
        params += [q.strip(), f"{start}%", f"{start}%"]
    sql.append(f"order by {order}")
    if limit:
        sql.append("limit %s")
        params.append(limit)
    return _rows(conn, " ".join(sql), tuple(params))


def data_dates(conn) -> dict:
    """The latest price day and NSE equity list loaded: when a new listing can appear."""
    return _rows(conn, """select (select max(trade_date) from price_eod
                                  where exchange = 'NSE') as prices,
                                 (select max(snapshot_date) from nse_equity_list)
                                  as equity_list""")[0]


def price_coverage(conn, series: list[str]) -> dict:
    """What the loaded NSE prices allow the price factors, which need a trade in the last
    10 days and 127 (6-month) or 253 (12-month) sessions: the days loaded, the trading
    days missing between the first and the latest (weekdays that are not NSE holidays),
    the longest such gap, and how many stocks trading now have enough sessions."""
    from igs.ingest.jobs import trading_days
    days = [r[0] for r in conn.execute("""select distinct trade_date from price_eod
            where exchange = 'NSE' and series = any(%s) order by 1""", (series,))]
    if not days:
        return {"days": 0}
    have = set(days)
    expected = trading_days(conn, days[0], days[-1])
    missing = [i for i, d in enumerate(expected) if d not in have]
    runs: list[list[dt.date]] = []
    for n, i in enumerate(missing):     # consecutive missing trading days make one gap
        if n and missing[n - 1] == i - 1:
            runs[-1].append(expected[i])
        else:
            runs.append([expected[i]])
    longest = max(runs, key=len) if runs else []
    now, six, twelve = conn.execute("""
        with active as (select distinct isin from price_eod where exchange = 'NSE'
                        and series = any(%(s)s) and trade_date >= %(last)s - 10)
        select count(*), count(*) filter (where n > 126), count(*) filter (where n > 252)
        from (select p.isin, count(distinct p.trade_date) as n from price_eod p
              join active using (isin)
              where p.exchange = 'NSE' and p.series = any(%(s)s) group by p.isin) t""",
        {"s": series, "last": days[-1]}).fetchone()
    return {"days": len(days), "first": days[0], "latest": days[-1], "missing": len(missing),
            "longest_gap": (longest[0], longest[-1], len(longest)) if longest else None,
            "trading_now": now, "sessions_127": six, "sessions_253": twelve}


def stock_basic(conn, symbol: str) -> dict | None:
    """What is known about a stock outside the ranking, such as a new listing: NSE's
    equity-list entry, the latest prices, filings, and how many quarters of results are
    loaded. None if neither the instrument master nor the equity list knows the symbol."""
    now = utc_now()
    listing = _rows(conn, """select symbol, isin, company_name, series, listed_on,
                                    face_value::float8 as face_value, snapshot_date
                             from nse_equity_list where upper(symbol) = upper(%s)
                             order by snapshot_date desc limit 1""", (symbol,))
    try:
        company = _company(conn, symbol)
    except NotFound:
        company = None
    if company is None and not listing:
        return None
    out = {"symbol": (company or listing[0])["symbol"],
           "name": company["name"] if company else listing[0]["company_name"],
           "company_id": company["company_id"] if company else None,
           "listing": listing[0] if listing else None,
           "prices": [], "filings": [], "quarters": 0, "as_of": now}
    if company:
        cid = company["company_id"]
        out["prices"] = price_history(conn, cid, now)
        out["filings"] = filings_feed(conn, cid, now)
        out["quarters"] = _rows(conn, """select count(distinct period_end) as n
            from fundamental_fact where company_id = %s and period_type = 'Q'""", (cid,))[0]["n"]
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
          and a.filed_at::date >= si.valid_from
          and (si.valid_to is null or a.filed_at::date < si.valid_to)
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


MIN_INDUSTRY_PE = 3      # profitable companies an industry median P/E needs


def industry_pe(conn, run_id: int, industry: str | None) -> dict | None:
    """Median P/E of the run's profitable companies in an industry (key numbers); None
    with fewer than MIN_INDUSTRY_PE of them, where a median would mean little."""
    if not industry:
        return None
    row = _rows(conn, """select percentile_cont(0.5) within group
                                (order by (key_numbers->>'pe')::float8) as median,
                                count(*) as companies
                         from score_result where run_id = %s and industry = %s
                           and key_numbers->>'pe' is not null""", (run_id, industry))[0]
    return row if row["companies"] >= MIN_INDUSTRY_PE else None


def stock_detail(conn, symbol: str, run_id: int | None = None) -> dict:
    run = resolve_run(conn, run_id)
    companies = _rows(conn, """select r.company_id, c.name, r.symbol
        from score_result r join company c using (company_id)
        where r.run_id = %s and upper(r.symbol) = upper(%s)""", (run["run_id"], symbol))
    if not companies:
        known = _company(conn, symbol)  # Preserve the unknown-symbol error for nonexistent names.
        raise NotFound(f"{known['name']} ({known['symbol']}) is not in run {run['run_id']}: "
                       f"the screening universe left it out on {run['as_of']:%Y-%m-%d} (for "
                       "example for its market cap, too few quarters filed, no recent "
                       "trades or surveillance). It can still go on the watchlist.")
    company = companies[0]
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
    summary["industry_pe"] = industry_pe(conn, run["run_id"], summary.get("industry"))
    assert_no_advice_language(summary["explanation"])
    robustness = {k: summary.get(k) for k in ROBUSTNESS_FIELDS}
    from igs.forward import evidence
    return {"run": run, "company": summary, "pillars": pillars, "top_contributions": top5,
            "forward_evidence": evidence(conn, cid, run['as_of']),
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
    filters = ScreenFilters.model_validate(filters).model_dump(exclude_unset=True)
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
    return rankings(conn, run_id, **ScreenFilters.model_validate(
        rows[0]["filters"]).model_dump(exclude_unset=True))


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
    """Insider-trading disclosures (SEBI PIT) broadcast in the `days` before as_of.
    `superseded`: a revision broadcast later (by as_of) restates this person's trade on that
    date, so this row no longer counts."""
    return _rows(conn, """
        select t.filed_at, t.person_name, t.person_category, t.insider_role,
               t.transaction_type, t.acquisition_mode, t.security_type, t.side,
               t.open_market, t.quantity::float8, t.value_inr::float8,
               t.holding_after_pct::float8, t.trade_from, t.xbrl_url, t.submission_type,
               exists (select 1 from insider_trade r
                       where r.submission_type = 'Revision' and r.exchange = t.exchange
                         and r.symbol = t.symbol and r.person_name = t.person_name
                         and r.trade_from = t.trade_from and r.filed_at > t.filed_at
                         and r.filed_at <= %s) as superseded
        from insider_trade t
        where t.symbol = upper(%s) and t.filed_at <= %s and t.filed_at > %s
        order by t.filed_at desc limit 100""",
        (as_of, symbol, as_of, as_of - dt.timedelta(days=days)))


def document_processing_summary(conn) -> list[dict]:
    return _rows(conn, """select status, count(*) as documents, sum(rows_loaded) as rows_loaded
                         from document_processing group by status order by status""")


def document_failures(conn, limit: int = 20) -> list[dict]:
    return _rows(conn, """select p.request_params->>'symbol' as symbol,
                         d.attempts, d.last_error, d.processed_at
                         from document_processing d join raw_payload p using (fetch_id)
                         where d.status = 'failed' order by d.processed_at desc limit %s""",
                 (limit,))
