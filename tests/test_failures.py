"""Failure measurement: outcome definition, statistics, and that the backtest's
re-derivation of the High conviction rule matches the tiers production assigned."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
import synthetic_market as M

from igs.backtest import failures as F
from igs.backtest.engine import run_backtest
from igs.backtest.report import write_report
from igs.config import (
    RedFlagsConfig,
    load_backtest,
    load_costs,
    load_red_flags,
    load_scoring,
    load_universe,
)
from igs.guardrails import find_advice_language

FACTORS = ["revenue_ttm_yoy", "roe", "pb", "risk_adj_return_6m", "volatility_1y", "pledge_pct",
           "growth_consistency_12q", "opm_level", "revenue_cagr_3y"]


def _cfgs():
    sc = load_scoring()
    sc = sc.model_copy(update={
        "peer_group": sc.peer_group.model_copy(update={"min_peers": 2}),
        # Six synthetic names: widen the bands so High conviction can exist at all.
        "tiers": sc.tiers.model_copy(update={"high_conviction_top_pct": 40.0,
                                             "watchlist_top_pct": 70.0}),
        "robustness": sc.robustness.model_copy(update={"min_weight_stability": 0.3,
                                                       "min_positive_pillars": 2,
                                                       "max_factor_share": 0.9})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0, "min_filing_quarters": 4})
    return load_backtest(), sc, uc, load_costs()


def _rf() -> RedFlagsConfig:
    """The synthetic market has no announcements, surveillance lists or annual-report data,
    so the checks that need them can never be evaluated there (and would block High
    conviction on every date). Disabled here, explicitly."""
    raw = {k: dict(load_red_flags().flag(k)) for k in load_red_flags().names()}
    for name in ("auditor_qualification", "resignations", "surveillance",
                 "contingent_liabilities"):
        raw[name]["enabled"] = False
    return RedFlagsConfig.model_validate(raw)


@pytest.fixture(scope="module")
def result():
    bt, sc, uc, cc = _cfgs()
    return run_backtest(M.build(), dt.date(2021, 6, 1), dt.date(2023, 10, 31), "monthly", bt,
                        sc, uc, cc, factors=FACTORS, n_quantiles=3, rf=_rf()), sc


# --------------------------------------------------------------------------- statistics


def test_wilson_interval():
    lo, hi = F.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = F.wilson(5, 10)
    assert (lo, hi) == (pytest.approx(0.2366, abs=1e-4), pytest.approx(0.7634, abs=1e-4))
    assert F.wilson(0, 0) == (None, None)


def test_two_proportion_z():
    assert F.two_proportion_z(30, 100, 10, 100) > 3
    assert F.two_proportion_z(10, 100, 30, 100) < -3
    assert F.two_proportion_z(0, 0, 1, 10) is None


# --------------------------------------------------------------------------- outcomes


def test_outcome_definition_on_constructed_paths():
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(400)]
    n = len(days)

    def path(cid, fn, stop=None):
        return [{"company_id": cid, "security_id": cid, "trade_date": d, "tr": fn(i)}
                for i, d in enumerate(days) if stop is None or i < stop]
    rows = (path(1, lambda i: 100 + 0.05 * i)                              # steady
            + path(2, lambda i: 100 + i if i < 100 else 200 - 1.9 * (i - 100))   # up then -35%
            + path(3, lambda i: 100 + i if i < 100 else max(110, 200 - (i - 100)))  # dd 45%
            + path(4, lambda i: 100.0, stop=90))                            # stops trading
    tr = pl.DataFrame(rows)
    bench = pl.DataFrame({"trade_date": days, "bench": [100.0 + 0.01 * i for i in range(n)]})
    spec = load_backtest().failure
    out = F.outcomes(tr, bench, days, days[0], pl.DataFrame({"company_id": [1, 2, 3, 4]}), spec)
    r = {x["company_id"]: x for x in out.iter_rows(named=True)}
    assert r[1]["entry_date"] == days[1]                     # entered the next session
    assert not r[1]["failed"] and r[1]["fail_reason"] is None
    assert r[2]["failed"] and r[2]["fail_reason"] == "loss"
    assert r[3]["failed"] and r[3]["fail_reason"] == "drawdown"
    assert r[3]["ret"] > 0 and r[3]["max_drawdown"] < -0.4    # a gain that still failed
    assert r[4]["failed"] and r[4]["fail_reason"] == "stopped trading"
    # No full horizon after the date: nothing is measured rather than a partial answer.
    assert F.outcomes(tr, bench, days, days[-100], pl.DataFrame({"company_id": [1]}),
                      spec).height == 0


# --------------------------------------------------------------------------- the backtest


def test_hc_rule_rederivation_matches_production_tiers(result):
    res, sc = result
    rb = sc.robustness
    rec = res.tiers.with_columns(
        F.hc_mask(rb.min_weight_stability, rb.min_persistence, True, rb.enabled).alias("hc"))
    assert rec.height > 0 and (rec["tier"] == "High conviction").sum() > 0
    mismatch = rec.filter(pl.col("hc") != (pl.col("tier") == "High conviction"))
    assert mismatch.height == 0, mismatch.select("date", "company_id", "tier", "hc_blockers")


def test_failure_tables(result):
    res, _ = result
    ft = {r["group"]: r for r in res.failure_tiers.iter_rows(named=True)}
    total = ft["Whole universe"]["name_dates"]
    assert total == res.outcomes.join(res.tiers, on=["date", "company_id"]).height
    assert sum(ft[t]["name_dates"] for t in ("High conviction", "Watchlist", "Not shortlisted",
                                             "Rejected")) == total
    for r in ft.values():
        if r["name_dates"]:
            assert r["ci95_low"] <= r["failure_rate"] <= r["ci95_high"]
    ce = res.check_effectiveness
    assert {"pledge", "gate: weight stability"} <= set(ce["check"])
    assert set(ce["verdict"]) <= {"SUPPORTED", "NO EVIDENCE", "CONTRADICTED",
                                  "INSUFFICIENT DATA"}
    sens = res.sensitivity
    assert sens.filter(pl.col("configured")).height == 1
    conf = sens.filter(pl.col("configured")).row(0, named=True)
    hc_total = ft["High conviction"]["name_dates"]
    assert conf["choose_name_dates"] + conf["confirm_name_dates"] == hc_total


def test_check_effectiveness_verdicts():
    spec = load_backtest().failure
    n = 200
    records = pl.DataFrame({"date": [dt.date(2020, 1, 31)] * n, "company_id": list(range(n)),
                            "raw_rank_pct": [0.1] * n})
    # Check A trips on names 0-49, which all fail; clear names fail 5% of the time.
    fails = [i < 50 or (i % 20 == 0) for i in range(n)]
    outs = pl.DataFrame({"date": [dt.date(2020, 1, 31)] * n, "company_id": list(range(n)),
                         "failed": fails})
    checks = pl.concat([
        pl.DataFrame({"date": [dt.date(2020, 1, 31)] * n, "company_id": list(range(n)),
                      "flag": ["a"] * n, "status": ["tripped" if i < 50 else "clear"
                                                    for i in range(n)],
                      "severity": ["caution"] * n}),
        pl.DataFrame({"date": [dt.date(2020, 1, 31)] * n, "company_id": list(range(n)),
                      "flag": ["b"] * n, "status": ["tripped" if i < 5 else "clear"
                                                    for i in range(n)],
                      "severity": ["caution"] * n})])
    ce = {r["check"]: r for r in F.check_effectiveness(checks, records, outs, spec)
          .iter_rows(named=True)}
    assert ce["a"]["verdict"] == "SUPPORTED" and ce["a"]["tripped_rate"] == 1.0
    assert ce["b"]["verdict"] == "INSUFFICIENT DATA"           # 5 tripped < 20


def test_report_has_failure_sections(result, tmp_path):
    res, _ = result
    text = write_report(res, tmp_path / "bt", "Backtest (synthetic)").read_text()
    for heading in ("## Failure rates by tier", "## Do the checks earn their place?",
                    "## Threshold sensitivity"):
        assert heading in text
    assert not find_advice_language(text)
    for name in ("failure_by_tier", "check_effectiveness", "threshold_sensitivity",
                 "outcomes", "tiers_by_date"):
        assert (tmp_path / "bt" / f"{name}.csv").exists(), name
