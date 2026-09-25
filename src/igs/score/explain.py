"""Plain-language explanations generated from factor contributions.

Every sentence is built from a factor row: the raw value, the peer percentile
and peer group used, and the source filings. No sentence is free text, and
every one passes the advice-language guardrail.
"""

from __future__ import annotations

import json

import polars as pl

from igs.guardrails import assert_no_advice_language
from igs.timeutil import IST

LABELS = {
    "revenue_cagr_3y": ("Revenue CAGR (3 years)", "pct"),
    "revenue_cagr_5y": ("Revenue CAGR (5 years)", "pct"),
    "ebitda_cagr_3y": ("EBITDA CAGR (3 years)", "pct"),
    "ebitda_cagr_5y": ("EBITDA CAGR (5 years)", "pct"),
    "pat_cagr_3y": ("Profit CAGR (3 years)", "pct"),
    "pat_cagr_5y": ("Profit CAGR (5 years)", "pct"),
    "revenue_ttm_yoy": ("Revenue growth, trailing 12 months", "pct"),
    "pat_ttm_yoy": ("Profit growth, trailing 12 months", "pct"),
    "growth_acceleration_4q": ("Growth acceleration (last 4 vs prior 4 quarters)", "pp"),
    "growth_consistency_12q": ("Quarters of positive YoY revenue growth (of 12)", "count"),
    "roce": ("ROCE", "pct"),
    "roe": ("ROE", "pct"),
    "opm_level": ("Operating margin (TTM)", "pct"),
    "opm_trend_8q": ("Operating margin trend (per quarter, 8 quarters)", "pp"),
    "cash_conversion_3y": ("Operating cash flow / EBITDA (3 years)", "pct"),
    "net_debt_to_ebitda": ("Net debt / EBITDA", "x"),
    "interest_coverage": ("Interest coverage", "x"),
    "working_capital_days_trend": ("Change in working-capital days (1 year)", "days"),
    "pe_vs_own_5y_median": ("P/E relative to its own 5-year median", "x"),
    "peg_trailing": ("PEG (trailing)", "x"),
    "ev_ebitda": ("EV / EBITDA", "x"),
    "pb": ("Price / book", "x"),
    "risk_adj_return_6m": ("6-month return / annualised volatility", "ratio"),
    "risk_adj_return_12m": ("12-month return / annualised volatility", "ratio"),
    "volatility_1y": ("Annualised volatility (1 year)", "pct"),
    "rs_6m_vs_nifty500": ("6-month return relative to Nifty 500", "pct"),
    "rs_12m_vs_nifty500": ("12-month return relative to Nifty 500", "pct"),
    "price_vs_200dma": ("Price vs 200-day average", "pct"),
    "dma_50_200_state": ("50-day average above 200-day average", "bool"),
    "delivery_pct_20d_vs_1y": ("Delivery % (20 days) vs 1-year average", "x"),
    "promoter_holding_qoq": ("Change in promoter holding (quarter)", "pp_raw"),
    "pledge_pct": ("Promoter pledge", "pct_raw"),
    "pledge_trend": ("Change in promoter pledge (2 quarters)", "pp_raw"),
    "fii_dii_holding_change": ("Change in FII + DII holding (quarter)", "pp_raw"),
    "institutional_holder_count": ("Change in number of institutional holders", "count"),
    "insider_buying_90d": ("Insider open-market purchases, last 90 days (% of market cap)",
                           "pct_raw2"),
}


def fmt_value(factor: str, v: float | None) -> str:
    if v is None:
        return "n/a"
    kind = LABELS.get(factor, (factor, "x"))[1]
    if kind == "pct":
        return f"{v:.1%}"
    if kind == "pp":
        return f"{v * 100:+.1f} pp"
    if kind == "pp_raw":
        return f"{v:+.2f} pp"
    if kind == "pct_raw":
        return f"{v:.1f}%"
    if kind == "pct_raw2":
        return f"{v:.2f}%"
    if kind == "days":
        return f"{v:+.0f} days"
    if kind == "count":
        return f"{v:.0f}"
    if kind == "bool":
        return "yes" if v >= 0.5 else "no"
    if kind == "ratio":
        return f"{v:.2f}"
    return f"{v:.2f}x"


def _ordinal(p: float) -> str:
    n = int(round(p * 100))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def factor_sentence(r: dict, filings: dict[int, dict]) -> str:
    label = LABELS.get(r["factor"], (r["factor"], "x"))[0]
    parts = [f"{label}: {fmt_value(r['factor'], r['value'])}"]
    if r.get("peer_percentile") is not None:
        parts.append(f"{_ordinal(r['peer_percentile'])} percentile of {r['peer_count']} "
                     f"{r['peer_group']} peers ({r['peer_level']})")
    srcs = sorted((filings[i] for i in set(r.get("source_filing_ids") or []) if i in filings),
                  key=lambda f: f["filed_at"], reverse=True)
    if srcs:
        more = f" (+{len(srcs) - 1} earlier filings)" if len(srcs) > 1 else ""
        parts.append(f"source: {srcs[0]['label']}{more}")
    return assert_no_advice_language(" - ".join(parts) + ".")


def top_contributions(factors: pl.DataFrame, company_id: int, n: int = 5) -> pl.DataFrame:
    return (factors.filter((pl.col("company_id") == company_id)
                           & pl.col("contribution").is_not_null())
                   .sort("contribution", descending=True).head(n))


def why(company: dict, factors: pl.DataFrame, flags: pl.DataFrame,
        filings: dict[int, dict]) -> str:
    """The 'why this stock' panel text."""
    cid = company["company_id"]
    name = company.get("name") or company.get("symbol") or f"Company {cid}"
    lines = [f"{name}: tier {company['tier']}"
             + (f" ({company['tier_reason']})" if company.get("tier_reason") else "") + "."]
    if company.get("composite") is not None:
        rank = (f", rank {company['rank']} of {company['scored']} eligible names"
                if company.get("rank") is not None else ", not ranked because it is rejected")
        lines.append(f"Composite score {company['composite']:+.2f} (coverage "
                     f"{company['coverage']:.0%}){rank}.")
        top = top_contributions(factors, cid)
        if top.height:
            lines.append("Largest positive contributions:")
            for r in top.iter_rows(named=True):
                lines.append(f"- {factor_sentence(r, filings)}")
        weak = (factors.filter((pl.col("company_id") == cid)
                               & pl.col("contribution").is_not_null())
                       .sort("contribution").head(2))
        if weak.height and weak["contribution"][0] < 0:
            lines.append("Weakest contributions:")
            for r in weak.filter(pl.col("contribution") < 0).iter_rows(named=True):
                lines.append(f"- {factor_sentence(r, filings)}")
    if company.get("weight_stability") is not None:
        lines.append(robustness_sentence(company))
    mine = flags.filter(pl.col("company_id") == cid)
    if "severity" not in mine.columns:
        mine = mine.with_columns(pl.lit("reject").alias("severity"))
    tripped = mine.filter(pl.col("status") == "tripped")
    unavailable = mine.filter(pl.col("status") == "data_unavailable")
    for severity, title in (("reject", "Red flags"), ("caution", "Cautions")):
        t = tripped.filter(pl.col("severity") == severity)
        if t.height:
            lines.append(f"{title}:")
            lines += [f"- {check_label(r['flag'])}: {r['message']}"
                      for r in t.iter_rows(named=True)]
    if unavailable.height:
        lines.append("Could not be checked (data unavailable): "
                     + ", ".join(check_label(f) for f in unavailable["flag"]) + ".")
    return assert_no_advice_language("\n".join(lines))


def check_label(flag: str) -> str:
    from igs.score.red_flags import LABELS
    return LABELS.get(flag, flag.replace("_", " "))


def robustness_sentence(c: dict) -> str:
    parts = [f"in the High conviction band in {c['weight_stability']:.0%} of pillar-weight "
             "variations"]
    if c.get("persist_dates"):
        parts.append(f"in the top of the ranking at {c.get('persist_hits') or 0} of the previous "
                     f"{c['persist_dates']} month-ends")
    if c.get("scored_pillars"):
        parts.append(f"{c.get('positive_pillars') or 0} of {c['scored_pillars']} pillars above "
                     "zero")
    if c.get("top_factor_share") is not None:
        parts.append(f"largest single-factor share of the score {c['top_factor_share']:.0%} "
                     f"({c.get('top_factor')})")
    return "Robustness: " + "; ".join(parts) + "."


def filing_labels(filings: pl.DataFrame) -> dict[int, dict]:
    """filing_id -> {label, url} for source attribution."""
    out = {}
    for r in filings.iter_rows(named=True):
        kind = "results" if r["filing_type"] == "financial_results" else "shareholding"
        basis = f" {r['statement_basis']}" if r.get("statement_basis") else ""
        filed = r["filed_at"].astimezone(IST)
        out[r["filing_id"]] = {
            "label": f"{kind}{basis} for period ending {r['period_end']} filed "
                     f"{filed:%Y-%m-%d %H:%M} IST",
            "filed_at": r["filed_at"], "url": r.get("source_url")}
    return out


def detail_dict(detail: str | None) -> dict:
    try:
        return json.loads(detail) if detail else {}
    except ValueError:
        return {}
