"""Screener.in exports the owner downloads, used two ways:

- a check on the app's own figures: each quarter's and year's sales, profit before tax and
  net profit in the export against the results filings the app loaded (`check`);
- figures for what the app lacks when the AI judges a stock: quarters not loaded, the
  balance sheet and cash flows, ten years of history, or a stock the screen's universe left
  out (`ai_inputs`, read by the AI's calls and its verdicts on brokers' calls).

Only downloads: Screener.in's terms (screener.in/guides/terms/, read 1 Oct 2026) license
"personal, non-commercial transitory viewing" and rule out copying its pages, so the app
never fetches them. "Export to Excel" on a company's page (with a free login) is the route
Screener itself offers; the owner imports the file on the stock's page, the AI calls page
or with `igs import screener FILE`.

Never in the scores: an export has no filing times and its figures can be restated since,
so it can't be placed point in time (tier-3 enrichment, igs.ingest.manual).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from igs.timeutil import IST, utc_now

CRORE = 1e7
UNIT = "Rs crore, except per-share values, share counts and prices"
# Export lines checked against the app's results facts (period type, concepts tried in turn:
# revenue, or interest earned for a bank; net profit, or the owners' share).
CHECKS = {("Quarters", "Sales"): ("Q", ("revenue", "interest_earned")),
          ("Quarters", "Profit before tax"): ("Q", ("pbt",)),
          ("Quarters", "Net profit"): ("Q", ("pat", "pat_owners")),
          ("PROFIT & LOSS", "Sales"): ("FY", ("revenue", "interest_earned")),
          ("PROFIT & LOSS", "Profit before tax"): ("FY", ("pbt",)),
          ("PROFIT & LOSS", "Net profit"): ("FY", ("pat", "pat_owners"))}
TOLERANCE = 0.02            # relative difference that still agrees (rounding, definitions)
ABS_TOLERANCE_CR = 0.1      # ... or this many crore, for small figures
AI_QUARTERS, AI_YEARS = 8, 5


def page_url(symbol: str) -> str:
    """The company's page on screener.in, where "Export to Excel" is."""
    return f"https://www.screener.in/company/{symbol}/"


def exports(conn, company_id: int) -> list[dict]:
    """The company's imported Excel exports, newest first."""
    cur = conn.execute("""select p.fetch_id, p.fetched_at as imported_at,
                                 p.request_params->>'original_filename' as file,
                                 count(*) as values_
                          from screener_enrichment e join raw_payload p
                            on p.fetch_id = e.source_fetch_id
                          where e.company_id = %s and e.section is not null
                          group by p.fetch_id order by p.fetched_at desc""", (company_id,))
    return [dict(zip(("fetch_id", "imported_at", "file", "values"), r, strict=True))
            for r in cur.fetchall()]


def latest(conn, company_id: int, before: dt.datetime | None = None) -> dict | None:
    """The newest Excel export imported by `before` (any time if None): {"fetch_id",
    "imported_at", "file", "company_name", "meta": {label: value}, "sections": {section:
    {line: {ISO period: value}}}}."""
    found = [e for e in exports(conn, company_id)
             if before is None or e["imported_at"] <= before]
    if not found:
        return None
    e = found[0]
    out: dict[str, Any] = {**{k: e[k] for k in ("fetch_id", "imported_at", "file")},
                           "company_name": None, "meta": {}, "sections": {}}
    for section, field, period, text, num in conn.execute(
            """select section, field, period_label, value_text, value_num::float8
               from screener_enrichment where source_fetch_id = %s and company_id = %s""",
            (e["fetch_id"], company_id)).fetchall():
        if section == "META":
            if field == "Company name":
                out["company_name"] = text
            else:
                out["meta"][field] = num if num is not None else text
        elif period is not None:
            out["sections"].setdefault(section, {}).setdefault(field, {})[period] = num
    return out


def _ours(conn, company_id: int, as_of: dt.datetime) -> dict:
    """{(period type, basis, (year, month), concept): value in crore} from the results
    facts public at `as_of`."""
    concepts = sorted({c for _, cs in CHECKS.values() for c in cs})
    rows = conn.execute("""select period_type, statement_basis, period_end, concept,
                                  value::float8
                           from facts_as_of(%s) where company_id = %s
                             and period_type in ('Q', 'FY') and concept = any(%s)""",
                        (as_of, company_id, concepts)).fetchall()
    return {(t, b, (d.year, d.month), c): v / CRORE for t, b, d, c, v in rows}


def check(conn, company_id: int, export: dict | None = None,
          as_of: dt.datetime | None = None) -> dict | None:
    """The export's sales, profit before tax and net profit against the app's results
    filings public at `as_of` (now), consolidated and standalone, for each period both
    have: they agree within 2% (or Rs 0.1 crore) on either basis, or differ. Periods only
    the export has are listed: those are the gaps it fills. None without an export."""
    export = export or latest(conn, company_id)
    if export is None:
        return None
    ours = _ours(conn, company_id, as_of or utc_now())
    rows = []
    for (section, line), (ptype, concepts) in CHECKS.items():
        for period, value in sorted(export["sections"].get(section, {}).get(line, {}).items()):
            if value is None:
                continue
            d = dt.date.fromisoformat(period)
            # The closest of the app's figures: either basis, and for net profit the whole
            # or the owners' share, as Screener's definitions may differ.
            found = [(abs(value - ours[k]), basis, ours[k])
                     for basis in ("consolidated", "standalone") for c in concepts
                     if (k := (ptype, basis, (d.year, d.month), c)) in ours]
            row = {"period": period, "line": f"{line} ({'quarter' if ptype == 'Q' else 'year'})",
                   "screener_cr": round(value, 2)}
            if not found:
                rows.append({**row, "app_cr": None, "basis": None, "diff_pct": None,
                             "status": "only in Screener.in"})
                continue
            gap, basis, mine = min(found)
            agree = gap <= max(TOLERANCE * abs(mine), ABS_TOLERANCE_CR)
            rows.append({**row, "app_cr": round(mine, 2), "basis": basis,
                         "diff_pct": (round(100 * (value / mine - 1), 1) if mine else None),
                         "status": "agrees" if agree else "differs"})
    compared = [r for r in rows if r["status"] != "only in Screener.in"]
    return {"file": export["file"], "imported_at": export["imported_at"],
            "company_name": export["company_name"], "rows": rows,
            "compared": len(compared),
            "agree": sum(r["status"] == "agrees" for r in compared),
            "differ": [r for r in compared if r["status"] == "differs"],
            "only_in_screener": [r for r in rows if r["status"] == "only in Screener.in"]}


def summary(c: dict, detail: bool = True) -> str:
    """One line on a check, for the CLI and the import message; without `detail`, the
    differences are only counted (a table shows them)."""
    if not c["rows"]:
        return ("no sales or profit lines found to compare (the export's layout may differ "
                "from what the app reads)")
    text = (f"{c['agree']} of {c['compared']} figures agree with the app's results filings"
            if c["compared"] else "no period the app's results filings also cover")
    if c["differ"] and not detail:
        text += f"; {len(c['differ'])} differ"
    elif c["differ"]:
        text += "; differ: " + ", ".join(
            f"{r['line']} {r['period']} Rs {r['screener_cr']:,.2f} cr vs "
            f"{r['app_cr']:,.2f} ({r['basis']})" for r in c["differ"][:5])
        text += f" and {len(c['differ']) - 5} more" if len(c["differ"]) > 5 else ""
    if c["only_in_screener"]:
        text += (f"; {len(c['only_in_screener'])} figures only in Screener.in "
                 "(periods the app has no results for)")
    return text


def _next_run(conn, as_of: dt.datetime) -> dt.datetime | None:
    return conn.execute("select min(as_of) from score_run where as_of > %s",
                        (as_of,)).fetchone()[0]


def ai_inputs(conn, company_id: int, run: dict) -> dict | None:
    """What the AI is given from the latest export for a call or verdict at the run's date:
    the export imported before the next run (any, for the latest run), its lines up to the
    run's date (the last 8 quarters and 5 years), the figures at download (META) and how
    it compares with the results filings public at the run's date. None without one."""
    export = latest(conn, company_id, _next_run(conn, run["as_of"]))
    if export is None:
        return None
    day = run["as_of"].astimezone(IST).date().isoformat()
    tables = {}
    for section, lines in export["sections"].items():
        periods = sorted({p for v in lines.values() for p in v if p <= day})
        keep = periods[-(AI_QUARTERS if section == "Quarters" else AI_YEARS):]
        if keep:
            tables[section] = {"periods": keep,
                               "lines": {line: [v.get(p) for p in keep]
                                         for line, v in lines.items()}}
    c = check(conn, company_id, export, run["as_of"])
    return {"source": "Screener.in export the user downloaded and imported",
            "imported": export["imported_at"].astimezone(IST).date(),
            "company_name": export["company_name"], "unit": UNIT,
            "at_download": export["meta"], "tables": tables,
            "check_against_filings": {
                "agree": c["agree"], "compared": c["compared"],
                "differ": c["differ"][:10],
                "only_in_screener": [f"{r['line']} {r['period']}"
                                     for r in c["only_in_screener"]][:16]}}


def wanted(conn, run_id: int | None, days: int = 30, min_quarters: int = 4) -> list[dict]:
    """Stocks with brokers' calls in the last `days` days whose data is thin, and no
    Screener.in export imported in that time: outside the run, fewer than `min_quarters`
    quarters of results loaded, or the AI's latest verdict on one of their calls is "cannot
    judge". The owner downloads these exports to fill the gaps."""
    since = dt.datetime.now(IST).date() - dt.timedelta(days=days)
    cur = conn.execute("""
        with calls as (select distinct company_id from broker_call
                       where company_id is not null and called_on >= %(since)s)
        select c.company_id,
               (select si.id_value from security_identifier si join security s
                  using (security_id) where s.company_id = c.company_id
                  and si.id_type = 'NSE_SYMBOL'
                order by si.valid_to is null desc, si.valid_from desc limit 1) as symbol,
               co.name,
               not exists (select 1 from score_result r where r.run_id = %(run)s
                           and r.company_id = c.company_id) as outside,
               (select count(distinct period_end) from fundamental_fact f
                where f.company_id = c.company_id and f.period_type = 'Q') as quarters,
               exists (select 1 from broker_call b
                       join lateral (select v.verdict from ai_broker_verdict v
                                     where v.broker_call_id = b.broker_call_id
                                     order by v.given_at desc limit 1) v on true
                       where b.company_id = c.company_id and b.called_on >= %(since)s
                         and v.verdict = 'cannot judge') as cannot_judge
        from calls c join company co using (company_id)
        where not exists (select 1 from screener_enrichment e
                          join raw_payload p on p.fetch_id = e.source_fetch_id
                          where e.company_id = c.company_id and e.section is not null
                            and p.fetched_at >= %(since)s)""",
        {"since": since, "run": run_id})
    out = []
    for cid, symbol, name, outside, quarters, cannot in cur.fetchall():
        why = ((["outside the ranking"] if outside and run_id is not None else [])
               + ([f"{quarters} quarters of results loaded"] if quarters < min_quarters else [])
               + (['the AI\'s verdict was "cannot judge"'] if cannot else []))
        if why and symbol:
            out.append({"company_id": cid, "symbol": symbol, "name": name,
                        "why": "; ".join(why), "url": page_url(symbol)})
    return sorted(out, key=lambda r: r["symbol"])
