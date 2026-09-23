"""Red flags and cautions: checks evaluated point in time.

Each check returns one row per company with status (see flagbase):
  tripped           the condition was found
  clear             evaluated, nothing found
  data_unavailable  could not be evaluated; never treated as a pass
  not_applicable    the check does not apply (e.g. receivable days for a bank)
plus a human-readable message, the numbers behind it and the source rows.

What a tripped check does is configuration (red_flags.yaml, `severity`):
reject-severity checks are hard filters (Rejected, with the reason); caution
checks keep a stock out of High conviction. The governance flags live here;
accounting, data-integrity and market checks live in flags_accounting,
flags_integrity and flags_market.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable

import polars as pl

from igs.config import RedFlagsConfig
from igs.factors import base as b
from igs.pit.view import PitView
from igs.score import flags_accounting as acc
from igs.score import flags_integrity as integ
from igs.score import flags_market as mkt
from igs.score.flagbase import CLEAR, NA, REJECT, SCHEMA, TRIPPED, UNAVAILABLE
from igs.score.flagbase import row as _row

ROLE_PATTERNS = {
    "statutory_auditor": r"resign\w*.{0,60}\b(statutory\s+)?auditors?\b|\bauditors?\b.{0,60}"
                         r"resign",
    "cfo": r"resign\w*.{0,80}(chief\s+financial\s+officer|\bcfo\b)|(chief\s+financial\s+"
           r"officer|\bcfo\b).{0,80}resign",
    "independent_director": r"resign\w*.{0,80}independent\s+director|independent\s+director"
                            r".{0,80}resign",
}
QUALIFIED = r"qualified\s+opinion|audit\s+qualification|modified\s+opinion|adverse\s+opinion|" \
            r"disclaimer\s+of\s+opinion"


def _shp(view: PitView) -> pl.DataFrame:
    if not view.has("shareholding"):
        return pl.DataFrame()
    s = (view.table("shareholding").sort("filed_at")
             .unique(subset=["company_id", "period_end", "category"], keep="last"))
    return s.sort("company_id", "period_end")


def _by_company(df: pl.DataFrame | None) -> dict[int, pl.DataFrame]:
    """Split a frame by company once, so per-company checks do not rescan the table."""
    if df is None or df.height == 0 or "company_id" not in df.columns:
        return {}
    return {k[0]: g for k, g in df.partition_by("company_id", as_dict=True).items()}


def _get(parts: dict[int, pl.DataFrame], cid: int, like: pl.DataFrame | None) -> pl.DataFrame:
    got = parts.get(cid)
    if got is not None:
        return got
    return like.clear() if like is not None else pl.DataFrame()


# --------------------------------------------------------------------------- flags


def pledge(view: PitView, companies: list[int], cfg: dict) -> list[dict]:
    s = _shp(view)
    prom = _by_company(s.filter(pl.col("category") == "promoter") if s.height else s)
    out = []
    limit = cfg["max_pledged_pct_of_promoter"]
    for cid in companies:
        p = _get(prom, cid, s)
        if not p.height:
            out.append(_row(cid, "pledge", UNAVAILABLE, "no shareholding filing"))
            continue
        last = p.tail(1).row(0, named=True)
        pct = last["pledged_pct"] or 0.0
        status = TRIPPED if pct > limit else CLEAR
        out.append(_row(cid, "pledge", status,
                        f"promoter pledge {pct:.1f}% of promoter holding "
                        f"(limit {limit:g}%) as of {last['period_end']}",
                        {"pledged_pct": pct, "period_end": last["period_end"]},
                        [last["filing_id"]]))
    return out


def promoter_holding_decline(view: PitView, companies: list[int], cfg: dict) -> list[dict]:
    s = _shp(view)
    prom = _by_company(s.filter(pl.col("category") == "promoter") if s.height else s)
    limit = cfg["max_decline_pp_two_quarters"]
    out = []
    for cid in companies:
        p = _get(prom, cid, s)
        if p.height < 3:
            out.append(_row(cid, "promoter_holding_decline", UNAVAILABLE,
                            "fewer than three shareholding filings"))
            continue
        now, then = p.tail(1).row(0, named=True), p.tail(3).row(0, named=True)
        drop = (then["pct_of_total"] or 0) - (now["pct_of_total"] or 0)
        status = TRIPPED if drop > limit else CLEAR
        out.append(_row(cid, "promoter_holding_decline", status,
                        f"promoter holding {then['pct_of_total']:.2f}% ({then['period_end']}) "
                        f"-> {now['pct_of_total']:.2f}% ({now['period_end']}); limit "
                        f"{limit:g} pp", {"decline_pp": drop},
                        [then["filing_id"], now["filing_id"]]))
    return out


def surveillance(view: PitView, companies: list[int], cfg: dict) -> list[dict]:
    if not view.has("surveillance"):
        return [_row(c, "surveillance", UNAVAILABLE, "no ASM/GSM snapshot loaded")
                for c in companies]
    s = view.table("surveillance")
    measures = [m for m, on in (("ASM", cfg.get("asm", True)), ("GSM", cfg.get("gsm", True)))
                if on]
    latest = s.group_by("measure").agg(pl.col("effective_from").max().alias("_d"))
    known = set(latest["measure"].to_list())
    cur = s.join(latest, on="measure").filter(pl.col("effective_from") == pl.col("_d"))
    out = []
    for cid in companies:
        missing = [m for m in measures if m not in known]
        hit = cur.filter((pl.col("company_id") == cid) & pl.col("measure").is_in(measures))
        if hit.height:
            desc = ", ".join(f"{r['measure']} {r['stage'] or ''}".strip()
                             for r in hit.iter_rows(named=True))
            out.append(_row(cid, "surveillance", TRIPPED, f"under surveillance: {desc}",
                            {"as_of_snapshot": str(hit["effective_from"].max())}))
        elif missing:
            out.append(_row(cid, "surveillance", UNAVAILABLE,
                            f"no {'/'.join(missing)} snapshot on or before this date"))
        else:
            out.append(_row(cid, "surveillance", CLEAR, "not in the latest ASM/GSM lists"))
    return out


def _announcements(view: PitView, days: int) -> pl.DataFrame | None:
    if not view.has("announcements"):
        return None
    a = view.table("announcements")
    since = view.as_of - dt.timedelta(days=days)
    covered = a.filter(pl.col("filed_at") >= since)
    if covered.height == 0:
        return None   # nothing landed for the window: cannot say "clear"
    return covered


def resignations(view: PitView, companies: list[int], cfg: dict) -> list[dict]:
    a = _announcements(view, cfg["lookback_days"])
    out = []
    pats = {r: ROLE_PATTERNS[r] for r in cfg["roles"]}
    by_co = _by_company(a)
    for cid in companies:
        if a is None:
            out.append(_row(cid, "resignations", UNAVAILABLE,
                            f"no announcements loaded for the last {cfg['lookback_days']} days"))
            continue
        mine = _get(by_co, cid, a)
        hits = []
        for r in mine.iter_rows(named=True):
            text = f"{r['category']} {r['subject']}"
            for role, pat in pats.items():
                if re.search(pat, text, re.IGNORECASE):
                    hits.append((role, r))
        if hits:
            role, r = hits[0]
            out.append(_row(cid, "resignations", TRIPPED,
                            f"{role.replace('_', ' ')} resignation announced "
                            f"{r['filed_at']:%Y-%m-%d}: {r['subject'][:120]}",
                            {"matches": len(hits)}, [r["ann_id"]] if "ann_id" in r else [],
                            [r["attachment_url"]] if r.get("attachment_url") else []))
        else:
            out.append(_row(cid, "resignations", CLEAR,
                            f"no auditor/CFO/independent-director resignation in "
                            f"{cfg['lookback_days']} days"))
    return out


def auditor_qualification(view: PitView, companies: list[int], cfg: dict) -> list[dict]:
    filings = view.table("filings") if view.has("filings") else None
    a = _announcements(view, 400)
    with_opinion = filings.filter((pl.col("filing_type") == "financial_results")
                                  & pl.col("audit_opinion").is_not_null()) \
        if filings is not None and "audit_opinion" in filings.columns else None
    f_by, a_by = _by_company(with_opinion), _by_company(a)
    out = []
    for cid in companies:
        f = _get(f_by, cid, with_opinion) if with_opinion is not None else None
        ann = _get(a_by, cid, a) if a is not None else None
        if ann is not None and ann.height:
            hit = ann.filter(pl.concat_str("category", "subject", separator=" ")
                             .str.contains("(?i)" + QUALIFIED))
            if hit.height:
                r = hit.row(0, named=True)
                out.append(_row(cid, "auditor_qualification", TRIPPED,
                                f"audit qualification disclosed {r['filed_at']:%Y-%m-%d}: "
                                f"{r['subject'][:120]}", {}, [r["ann_id"]]))
                continue
        if f is not None and f.height:
            last = f.sort("filed_at").tail(1).row(0, named=True)
            opinion = (last["audit_opinion"] or "").strip()
            clean = opinion.lower() in ("unmodified", "unmodified opinion", "not applicable",
                                        "na", "")
            out.append(_row(cid, "auditor_qualification", CLEAR if clean else TRIPPED,
                            f"latest audit opinion: {opinion or 'not stated'}",
                            {"filing_period_end": last["period_end"]}, [last["filing_id"]],
                            [last["source_url"]] if last.get("source_url") else []))
        elif ann is not None:
            out.append(_row(cid, "auditor_qualification", CLEAR,
                            "no qualification disclosed in announcements (audit opinion field "
                            "not available in filings)"))
        else:
            out.append(_row(cid, "auditor_qualification", UNAVAILABLE,
                            "no audit opinion or announcements available"))
    return out


def _receivable_days_history(view: PitView) -> pl.DataFrame:
    """Receivable days at each balance-sheet date: receivables / TTM revenue at that date."""
    q = b.quarterly(view).sort("company_id", "qidx").with_columns(
        pl.col("top_line").rolling_sum(4).over("company_id").alias("rev_ttm"),
        (pl.col("qidx") - pl.col("qidx").shift(3).over("company_id")).alias("_span"))
    q = q.filter(pl.col("_span") == 3).select("company_id", "period_end", "rev_ttm")
    f = view.facts(concepts=["trade_receivables"]).filter(pl.col("period_type") == "INSTANT")
    f = f.join(b.basis_choice(view), on=["company_id", "statement_basis"])
    return (f.select("company_id", "period_end", "value", "fact_id")
             .join(q, on=["company_id", "period_end"])
             .with_columns((pl.col("value") / pl.col("rev_ttm") * 365).alias("days"))
             .sort("company_id", "period_end"))


def receivable_days_spike(view: PitView, companies: list[int], cfg: dict) -> list[dict]:
    hist = _receivable_days_history(view)
    hist_by = _by_company(hist)
    fin = set(b.financials(view)["company_id"].to_list())
    mult = cfg["multiple_of_3y_median"]
    out = []
    for cid in companies:
        if cid in fin:
            out.append(_row(cid, "receivable_days_spike", NA, "not applicable to financials"))
            continue
        h = _get(hist_by, cid, hist)
        if h.height < 4:
            out.append(_row(cid, "receivable_days_spike", UNAVAILABLE,
                            f"{h.height} balance sheets with receivables; need 4"))
            continue
        last = h.tail(1).row(0, named=True)
        window = h.filter(pl.col("period_end") >= last["period_end"] - dt.timedelta(days=3 * 365)
                          ).head(h.height - 1)
        if window.height < 3:
            out.append(_row(cid, "receivable_days_spike", UNAVAILABLE,
                            "not enough prior balance sheets in 3 years"))
            continue
        med = window["days"].median()
        status = TRIPPED if last["days"] > mult * med else CLEAR
        out.append(_row(cid, "receivable_days_spike", status,
                        f"receivable days {last['days']:.0f} vs 3-year median {med:.0f} "
                        f"(limit {mult:g}x)", {"days": last["days"], "median_3y": med},
                        [last["fact_id"]]))
    return out


def equity_dilution(view: PitView, companies: list[int], cfg: dict) -> list[dict]:
    s = _shp(view)
    years, min_events = cfg["lookback_years"], cfg["min_events"]
    min_cum = cfg["min_cumulative_share_increase_pct"] / 100
    cas = view.table("corporate_actions") if view.has("corporate_actions") else None
    lp = b.last_price(view)
    totals = _by_company(s.filter(pl.col("category") == "total") if s.height else s)
    lp_by = _by_company(lp)
    out = []
    for cid in companies:
        t = _get(totals, cid, s)
        since = view.as_of_date - dt.timedelta(days=365 * years)
        t = t.filter(pl.col("period_end") >= since) if t.height else t
        if t.height < 4 * years - 1:
            out.append(_row(cid, "equity_dilution", UNAVAILABLE,
                            f"{t.height} shareholding filings in {years} years"))
            continue
        shares = t.select("period_end", "shares").to_dicts()
        sec = _get(lp_by, cid, lp)["security_id"]
        mult_after: dict = {}
        if cas is not None and sec.len():
            ev = cas.filter((pl.col("security_id") == sec[0])
                            & pl.col("action_type").is_in(["split", "consolidation", "bonus"]))
            for r in ev.iter_rows(named=True):
                m = ((r["ratio_a"] + r["ratio_b"]) / r["ratio_b"] if r["action_type"] == "bonus"
                     else r["fv_old"] / r["fv_new"])
                mult_after[r["ex_date"]] = m
        events, prev = [], None
        for row in shares:
            adj = row["shares"]
            for ex, m in mult_after.items():
                if ex > row["period_end"]:
                    adj *= m      # restate to today's share basis
            if prev is not None and adj > prev * 1.005:
                events.append((row["period_end"], adj / prev - 1))
            prev = adj
        first = shares[0]["shares"] * \
            _prod(m for ex, m in mult_after.items() if ex > shares[0]["period_end"])
        cum = prev / first - 1 if first else 0.0
        status = TRIPPED if len(events) >= min_events and cum >= min_cum else CLEAR
        out.append(_row(cid, "equity_dilution", status,
                        f"{len(events)} share issues in {years} years, cumulative "
                        f"{cum:.1%} (excluding bonus/split)",
                        {"events": [(str(d), round(g, 4)) for d, g in events]}))
    return out


def _prod(xs) -> float:
    p = 1.0
    for x in xs:
        p *= x
    return p


def contingent_liabilities(view: PitView, companies: list[int], cfg: dict) -> list[dict]:
    f = view.facts(concepts=["contingent_liabilities", "total_equity", "equity_owners"])
    mult = cfg["max_multiple_of_net_worth"]
    out = []
    for cid in companies:
        c = f.filter((pl.col("company_id") == cid)
                     & (pl.col("concept") == "contingent_liabilities")).sort("period_end")
        if not c.height:
            out.append(_row(cid, "contingent_liabilities", UNAVAILABLE,
                            "contingent liabilities are not in the filings loaded (annual "
                            "report data)"))
            continue
        last = c.tail(1).row(0, named=True)
        eq = f.filter((pl.col("company_id") == cid)
                      & pl.col("concept").is_in(["equity_owners", "total_equity"])
                      & (pl.col("period_end") == last["period_end"]))
        if not eq.height:
            out.append(_row(cid, "contingent_liabilities", UNAVAILABLE,
                            "no net worth for the same date"))
            continue
        nw = eq["value"][0]
        status = TRIPPED if nw <= 0 or last["value"] > mult * nw else CLEAR
        out.append(_row(cid, "contingent_liabilities", status,
                        f"contingent liabilities {last['value'] / 1e7:,.0f} cr vs net worth "
                        f"{nw / 1e7:,.0f} cr", {}, [last["fact_id"]]))
    return out


def other_income_share(view: PitView, companies: list[int], cfg: dict) -> list[dict]:
    oi = b.ttm(view, "other_income").rename({"ids": "ids_oi"})
    pbt = b.ttm(view, "pbt")
    j = oi.join(pbt, on="company_id")
    j_by = _by_company(j)
    fin = set(b.financials(view)["company_id"].to_list())
    limit = cfg["max_pct_of_pbt"] / 100
    out = []
    for cid in companies:
        if cid in fin:
            out.append(_row(cid, "other_income_share", NA, "not applicable to financials"))
            continue
        r = _get(j_by, cid, j)
        if not r.height:
            out.append(_row(cid, "other_income_share", UNAVAILABLE,
                            "four quarters of other income and PBT not available"))
            continue
        r = r.row(0, named=True)
        ids = list(r["ids_oi"]) + list(r["ids"])
        if r["pbt_ttm"] <= 0:
            status = TRIPPED if r["other_income_ttm"] > 0 else CLEAR
            msg = (f"TTM PBT {r['pbt_ttm'] / 1e7:,.1f} cr is not positive while other income is "
                   f"{r['other_income_ttm'] / 1e7:,.1f} cr")
        else:
            share = r["other_income_ttm"] / r["pbt_ttm"]
            status = TRIPPED if share > limit else CLEAR
            msg = f"other income {share:.1%} of TTM PBT (limit {limit:.0%})"
        out.append(_row(cid, "other_income_share", status, msg,
                        {"other_income_ttm": r["other_income_ttm"], "pbt_ttm": r["pbt_ttm"]},
                        ids))
    return out


Check = Callable[[PitView, list[int], dict], "list[dict] | pl.DataFrame"]

FLAGS: dict[str, Check] = {
    # governance and ownership
    "auditor_qualification": auditor_qualification,
    "resignations": resignations,
    "pledge": pledge,
    "promoter_holding_decline": promoter_holding_decline,
    "equity_dilution": equity_dilution,
    "surveillance": surveillance,
    "contingent_liabilities": contingent_liabilities,
    # earnings quality
    "receivable_days_spike": receivable_days_spike,
    "other_income_share": other_income_share,
    "cash_not_converting": acc.cash_not_converting,
    "accruals": acc.accruals,
    "altman_distress": acc.altman_distress,
    "piotroski_weak": acc.piotroski_weak,
    "beneish_manipulation": acc.beneish_manipulation,
    "cash_debt_paradox": acc.cash_debt_paradox,
    "exceptional_items": acc.exceptional_items,
    "restatement": acc.restatement,
    # data integrity
    "unit_scale_jump": integ.unit_scale_jump,
    "statement_identity": integ.statement_identity,
    "results_overdue": integ.results_overdue,
    # market behaviour
    "illiquid": mkt.illiquid,
    "high_volatility": mkt.high_volatility,
    "speculative_runup": mkt.speculative_runup,
    "deep_drawdown": mkt.deep_drawdown,
    "trade_for_trade": mkt.trade_for_trade,
}

LABELS = {
    "auditor_qualification": "audit qualification",
    "resignations": "auditor/CFO/independent director resignation",
    "pledge": "promoter pledge",
    "promoter_holding_decline": "promoter holding decline",
    "equity_dilution": "repeated equity dilution",
    "surveillance": "exchange surveillance (ASM/GSM)",
    "contingent_liabilities": "contingent liabilities",
    "receivable_days_spike": "receivable days spike",
    "other_income_share": "other income share of profit",
    "cash_not_converting": "profits not converting to cash",
    "accruals": "high accruals",
    "altman_distress": "balance-sheet distress (Altman Z'')",
    "piotroski_weak": "weak financial trend (Piotroski)",
    "beneish_manipulation": "earnings-manipulation screen (Beneish)",
    "cash_debt_paradox": "large cash and debt, low yield on cash",
    "exceptional_items": "exceptional items in profit",
    "restatement": "restated figures",
    "unit_scale_jump": "possible unit error in filings",
    "statement_identity": "statement identities do not hold",
    "results_overdue": "results overdue",
    "illiquid": "thin trading",
    "high_volatility": "high volatility",
    "speculative_runup": "large price run-up",
    "deep_drawdown": "deep fall from the 1-year high",
    "trade_for_trade": "trade-for-trade segment",
}
OUT_SCHEMA = {**SCHEMA, "severity": pl.Utf8, "unavailable_blocks": pl.Boolean}


def label(flag: str) -> str:
    return LABELS.get(flag, flag.replace("_", " "))


def evaluate(view: PitView, companies: list[int], cfg: RedFlagsConfig) -> pl.DataFrame:
    """Every enabled check for every company, with its configured severity."""
    frames: list[pl.DataFrame] = []
    for name, fn in FLAGS.items():
        flag_cfg = cfg.flag(name)
        if not flag_cfg.get("enabled", True):
            continue
        out = fn(view, companies, flag_cfg)
        df = out if isinstance(out, pl.DataFrame) else pl.DataFrame(out, schema=SCHEMA)
        severity = flag_cfg.get("severity", REJECT)
        blocks = severity == REJECT or bool(flag_cfg.get("unavailable_blocks", True))
        frames.append(df.select(list(SCHEMA)).with_columns(
            pl.lit(severity).alias("severity"), pl.lit(blocks).alias("unavailable_blocks")))
    if not frames:
        return pl.DataFrame(schema=OUT_SCHEMA)
    return pl.concat(frames).cast(OUT_SCHEMA)
