"""Frozen research filters, using the production point-in-time factor contract.

Thresholds are hypotheses, not fitted parameters or validated recommendations.
All three profiles currently apply only to non-financial companies. Missing inputs
and unknown governance checks cannot qualify, except a check the red-flag
configuration marks `unavailable_blocks: false` (its inputs are not in the filings
loaded), exactly as in the production ranking. Existing universe rules still apply.

v2 (before any real-data result): v1 treated every unavailable reject check as unknown,
including contingent liabilities, which production configures not to block because the
figure isn't loaded; no company could then qualify.
"""

from __future__ import annotations

import hashlib
import json
import math

import polars as pl

from igs.factors import REGISTRY, base

VERSION = 2
# Bounds are inclusive. A tiny positive minimum represents strictly positive growth.
RULES = {
    "inflection": {
        "revenue_quarter_yoy": (0.10, 10.0),
        "pat_quarter_yoy": (0.15, 20.0),
        "revenue_2q_yoy": (0.0, 10.0),
        "pat_2q_yoy": (0.0, 20.0),
        "revenue_acceleration_1q": (0.05, None),
        "pat_acceleration_1q": (0.05, None),
        "opm_quarter_yoy": (0.0, None),
        "cash_profit_1y": (0.5, None),
        "net_debt_to_ebitda": (None, 3.0),
    },
    "compounder": {
        "revenue_cagr_3y": (0.10, 1.0),
        "pat_cagr_3y": (0.10, 1.0),
        "growth_consistency_12q": (9.0, 12.0),
        "roce": (0.15, None),
        "cash_profit_1y": (0.8, None),
        "free_cash_flow_margin": (0.0, None),
        "net_debt_to_ebitda": (None, 2.0),
        "eps_diluted_yoy": (0.0, 20.0),
    },
    "garp": {
        "revenue_cagr_3y": (0.10, 1.0),
        "pat_cagr_3y": (0.10, 1.0),
        "peg_trailing": (0.0, 1.5),
        "roce": (0.12, None),
        "cash_profit_1y": (0.8, None),
        "net_debt_to_ebitda": (None, 2.0),
        "eps_diluted_yoy": (0.0, 20.0),
    },
}
FEATURES = tuple(sorted({key for rules in RULES.values() for key in rules}))
STRICT_POSITIVE = {"revenue_2q_yoy", "pat_2q_yoy", "eps_diluted_yoy", "peg_trailing"}


def specification() -> dict:
    body = {
        "version": VERSION,
        "rules": RULES,
        "strict_positive": sorted(STRICT_POSITIVE),
        "financials": "not_applicable",
        "governance": "all reject checks must be clear; an unavailable check blocks unless "
                      "configured unavailable_blocks: false, as in the production ranking",
        "ranking": "existing composite within each filtered eligible universe",
        "experimental": True,
        "production_weights_changed": False,
    }
    body["hash"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body


def acceleration(view, measure: str) -> pl.DataFrame:
    """Latest matched-quarter YoY minus previous matched-quarter YoY (percentage points).

    Requires four exact quarters, positive profit/revenue in each, and >=2% profit
    margins at both prior-year bases. No tiny-base or missing-quarter acceleration.
    """
    q = base.quarterly(view).join(base.latest_quarter(view), on="company_id")
    panels = []
    for lag in (0, 1, 4, 5):
        panels.append(
            q.filter(pl.col("qidx") == pl.col("last_q") - lag).select(
                "company_id",
                pl.col(measure).alias(f"v{lag}"),
                pl.col("top_line").alias(f"r{lag}"),
                pl.concat_list("ids_" + measure, "ids_top_line").alias(f"ids{lag}"),
            )
        )
    joined = panels[0]
    for panel in panels[1:]:
        joined = joined.join(panel, on="company_id")
    valid = pl.all_horizontal(pl.col(f"v{lag}") > 0 for lag in (0, 1, 4, 5))
    valid &= pl.all_horizontal(pl.col(f"r{lag}") > 0 for lag in (0, 1, 4, 5))
    if measure == "pat":
        valid &= (pl.col("v4") / pl.col("r4") >= 0.02) & (pl.col("v5") / pl.col("r5") >= 0.02)
    joined = joined.with_columns(
        pl.when(valid)
        .then(pl.col("v0") / pl.col("v4") - pl.col("v1") / pl.col("v5"))
        .alias("value"),
        pl.concat_list("ids0", "ids1", "ids4", "ids5").alias("ids"),
    )
    return base.finish(
        joined, "value", ["v0", "v1", "v4", "v5", "r4", "r5"], "ids", universe=base.companies(view)
    )


def readings(view) -> dict[str, dict[int, dict]]:
    out = {}
    for name in FEATURES:
        if name.endswith("_acceleration_1q"):
            frame = acceleration(view, "pat" if name.startswith("pat") else "top_line")
        else:
            frame = REGISTRY[name].fn(view)
        out[name] = {row["company_id"]: row for row in frame.to_dicts()}
    return out


def governance_unknown(hard: list[dict]) -> bool:
    """Whether the reject-level checks leave the company's governance unknown: none were
    run, or one could not be evaluated and is configured to block when unavailable."""
    return not hard or any(
        f["status"] not in ("clear", "not_applicable", "tripped")
        and not (f["status"] == "data_unavailable" and f.get("unavailable_blocks") is False)
        for f in hard)


def assess_company(evidence: dict, flags: list[dict], module: str) -> dict:
    hard = [f for f in flags if f["severity"] == "reject"]
    blocked = any(f["status"] == "tripped" for f in hard)
    unknown = module == 'unknown' or governance_unknown(hard)
    profiles = {}
    for name, rules in RULES.items():
        missing, failed = [], []
        for feature, (low, high) in rules.items():
            reading = evidence.get(feature, {})
            value = reading.get("value")
            if reading.get("status") == "unfavourable":
                failed.append(feature)
            elif (
                reading.get("status") != "ok"
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
            ):
                missing.append(feature)
            elif (
                (low is not None and value < low)
                or (high is not None and value > high)
                or (feature in STRICT_POSITIVE and value <= 0)
            ):
                failed.append(feature)
        status = (
            "not_applicable"
            if module in base.FINANCIAL_MODULES
            else "risk_blocked"
            if blocked
            else "not_qualified"
            if failed
            else "insufficient_evidence"
            if missing or unknown
            else "qualified"
        )
        profiles[name] = {
            "status": status,
            "missing": missing,
            "failed": failed,
            "governance_unknown": unknown,
            "coverage": 1 - len(missing) / len(rules),
        }
    return profiles


def assess(view, companies: list[int], flags: pl.DataFrame) -> list[dict]:
    values = readings(view)
    modules = dict(base.modules(view).select("company_id", "module").iter_rows())
    flag_map = {}
    for row in flags.to_dicts():
        flag_map.setdefault(row["company_id"], []).append(row)
    out = []
    for cid in companies:
        evidence = {name: rows.get(cid, {}) for name, rows in values.items()}
        out.append(
            {
                "company_id": cid,
                "as_of": view.as_of.isoformat(),
                "evidence": evidence,
                "profiles": assess_company(
                    evidence, flag_map.get(cid, []), modules.get(cid, "unknown")
                ),
            }
        )
    return out
