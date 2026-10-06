"""Robustness gates, plausibility bounds and the low-base growth guard."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest
import synthetic_market as M

from igs.config import load_red_flags, load_scoring, load_universe
from igs.dq import DQLog
from igs.factors.registry import REGISTRY
from igs.pit import PitDataset, PitView
from igs.score import robustness as R
from igs.score import sanity
from igs.score.run import composite_at, composite_ranks, evaluate_date, rank_history, trading_days
from igs.timeutil import IST, end_of_day_ist

AS_OF = dt.datetime(2024, 11, 29, 23, 59, tzinfo=IST)
RB = load_scoring().robustness
WEIGHTS = dict(load_scoring().pillar_weights)
PILLARS = list(WEIGHTS)


@pytest.fixture(scope="module")
def market():
    return M.build()


def _cfgs():
    sc = load_scoring().model_copy(update={"peer_group": load_scoring().peer_group.model_copy(
        update={"min_peers": 2})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0})
    return sc, uc


def _pillars(scores: dict[int, list[float | None]]) -> pl.DataFrame:
    return pl.DataFrame([{"company_id": c, "pillar": p, "score": s}
                         for c, row in scores.items() for p, s in zip(PILLARS, row, strict=True)],
                        schema={"company_id": pl.Int64, "pillar": pl.Utf8, "score": pl.Float64})


# --------------------------------------------------------------------------- weights


def test_dirichlet_draws_are_reproducible_and_centred():
    a = R.dirichlet_draws(WEIGHTS, 400, 30, seed=1)
    assert a.equals(R.dirichlet_draws(WEIGHTS, 400, 30, seed=1))
    assert not a.equals(R.dirichlet_draws(WEIGHTS, 400, 30, seed=2))
    sums = a.group_by("draw").agg(pl.col("w").sum())["w"]
    assert ((sums - 1).abs() < 1e-9).all()
    means = dict(a.group_by("pillar").agg(pl.col("w").mean()).iter_rows())
    for k, w in WEIGHTS.items():
        assert means[k] == pytest.approx(w, abs=0.02), k


def test_weight_stability_separates_broad_from_one_pillar_strength():
    # 40 names: company 1 is best on every pillar; company 2 is only exceptional on
    # momentum (the first pillar), so it is in the top band only when momentum's drawn
    # weight is high; the rest are spread out.
    scores = {1: [2.0] * 6, 2: [4.0, 0.15, 0.15, 0.15, 0.15, 0.15]}
    for c in range(3, 41):
        x = (c - 20) / 20
        scores[c] = [x, x * 0.9, x * 1.1, x, x * 0.8, x]
    st = dict(R.weight_stability(_pillars(scores), list(scores), WEIGHTS, 10, RB).iter_rows())
    assert st[1] == 1.0
    assert 0.0 < st[2] < st[1]


def test_missing_pillar_is_renormalised_like_the_composite():
    scores = {1: [1.0, 1.0, None, 1.0, 1.0, 1.0], 2: [0.5] * 6, 3: [0.0] * 6}
    st = dict(R.weight_stability(_pillars(scores), [1, 2, 3], WEIGHTS, 34, RB).iter_rows())
    assert st[1] == 1.0 and st[3] == 0.0


# --------------------------------------------------------------------------- other gates


def test_persistence_breadth_concentration_and_blockers():
    d1, d2 = dt.date(2024, 9, 30), dt.date(2024, 10, 31)
    hist = {d1: pl.DataFrame({"company_id": [1, 2], "rank_pct": [0.05, 0.6]}),
            d2: pl.DataFrame({"company_id": [1, 2], "rank_pct": [0.2, 0.1]})}
    p = dict((r["company_id"], r["persist_hits"]) for r in
             R.persistence(hist, [d1, d2], 30).iter_rows(named=True))
    assert p == {1: 2, 2: 1}
    pillars = _pillars({1: [1.0, 0.5, 0.2, 0.1, -0.2, -0.3],
                        2: [2.0, -1.5, -0.1, -0.2, 0.1, -0.05]})
    br = {r["company_id"]: r for r in R.breadth(pillars).iter_rows(named=True)}
    assert br[1]["positive_pillars"] == 4 and br[2]["weakest_pillar"] == "quality"
    factors = pl.DataFrame({"company_id": [1, 1, 1, 2, 2], "factor": ["a", "b", "c", "a", "b"],
                            "contribution": [0.3, 0.3, 0.4, 0.9, 0.1]})
    conc = {r["company_id"]: r for r in R.concentration(factors).iter_rows(named=True)}
    assert conc[2]["top_factor"] == "a" and conc[2]["top_factor_share"] == pytest.approx(0.9)
    rob = R.evaluate(pillars, factors, [1, 2], WEIGHTS, 50, RB, hist, [d1, d2])
    reasons = R.blockers(rob, RB)
    two = reasons.filter(pl.col("company_id") == 2)["reason"].to_list()
    assert any(r.startswith("new to the top") for r in two)
    assert any(r.startswith("narrow strength") for r in two)
    assert any(r.startswith("weak quality pillar") for r in two)
    assert any(r.startswith("one factor carries the score: a") for r in two)
    one = reasons.filter(pl.col("company_id") == 1)["reason"].to_list()
    assert not any(r.startswith(("new to the top", "narrow", "weak", "one factor"))
                   for r in one)
    off = RB.model_copy(update={"enabled": False})
    assert R.blockers(rob, off).height == 0


def test_prior_month_ends():
    days = [dt.date(2024, 8, 30), dt.date(2024, 9, 27), dt.date(2024, 9, 30),
            dt.date(2024, 10, 1), dt.date(2024, 10, 31), dt.date(2024, 11, 4)]
    assert R.prior_month_ends(days, dt.date(2024, 11, 29), 2) == [dt.date(2024, 10, 31),
                                                                  dt.date(2024, 9, 30)]
    assert R.prior_month_ends(days, dt.date(2024, 1, 15), 1) == []


# --------------------------------------------------------------------------- plausibility


def test_implausible_values_are_left_out_logged_and_block():
    df = pl.DataFrame({"company_id": [1, 2, 3], "value": [0.2, 7.5, None],
                       "status": ["ok", "ok", "insufficient_data"],
                       "detail": ['{"x": 1}', '{"x": 2}', "{}"],
                       "source_fact_ids": [[1], [2], []]})
    dq = DQLog()
    out, bad = sanity.apply({"revenue_ttm_yoy": df}, {"revenue_ttm_yoy": (-0.95, 5.0)}, dq)
    r = out["revenue_ttm_yoy"]
    assert r["status"].to_list() == ["ok", "implausible", "insufficient_data"]
    assert r["value"].to_list() == [0.2, None, None]
    detail = json.loads(r["detail"][1])
    assert detail["implausible_value"] == 7.5 and detail["inputs"] == {"x": 2}
    assert bad.height == 1 and dq.issues[0].category == "implausible_factor_value"
    assert "possible data error" in sanity.blockers(bad)["reason"][0]


def test_implausible_value_reduces_coverage_not_imputed(market):
    sc, uc = _cfgs()
    tight = sc.model_copy(update={"plausibility": {**sc.plausibility,
                                                   "revenue_ttm_yoy": (-0.95, 0.01)}})
    _, _, norm, _, bad = composite_at(market, AS_OF, tight, uc, set())
    assert bad.height > 0
    rows = norm.filter(pl.col("factor") == "revenue_ttm_yoy")
    hit = rows.filter(pl.col("company_id").is_in(bad["company_id"].to_list()))
    assert set(hit["status"]) == {"implausible"} and hit["z"].null_count() == hit.height


# --------------------------------------------------------------------------- low base


def test_profit_growth_needs_a_real_base(market):
    view = PitView(market, AS_OF)
    base = REGISTRY["pat_cagr_3y"].fn(view).filter(pl.col("company_id") == 1).row(0, named=True)
    assert base["status"] == "ok" and json.loads(base["detail"])["base_margin"] > 0.02
    # Cut GROW's profit three years back to 0.5% of revenue: growth from there is arithmetic.
    t = {k: v.drop("known_at") for k, v in market.tables.items()}
    q = pl.col("period_type") == "Q"
    # The base TTM is the four quarters to September 2021.
    window = pl.col("period_end").is_between(dt.date(2020, 10, 1), dt.date(2021, 9, 30))
    f = t["facts"].with_columns(
        pl.when((pl.col("company_id") == 1) & q & window
                & pl.col("concept").is_in(["pat", "pat_owners"]))
          .then(pl.col("value") * 0.03).otherwise(pl.col("value")).alias("value"))
    t["facts"] = f
    v2 = PitView(PitDataset.from_frames(**t), AS_OF)
    r = REGISTRY["pat_cagr_3y"].fn(v2).filter(pl.col("company_id") == 1).row(0, named=True)
    assert r["status"] == "insufficient_data"
    assert json.loads(r["detail"])["base_margin"] < 0.02
    peg = REGISTRY["peg_trailing"].fn(v2).filter(pl.col("company_id") == 1).row(0, named=True)
    assert peg["status"] == "insufficient_data"


# --------------------------------------------------------------------------- end to end


def test_evaluate_date_uses_point_in_time_ranks_for_persistence(market):
    sc, uc = _cfgs()
    rf = load_red_flags()
    hist = rank_history(market, sc, uc, set())
    ev = evaluate_date(market, AS_OF, sc, uc, rf, set(), hist, trading_days(market))
    for d in R.prior_month_ends(trading_days(market), AS_OF.date(), 2):
        direct = composite_ranks(composite_at(market, end_of_day_ist(d), sc, uc, set())[3])
        assert hist.ranks[d].sort("company_id").equals(direct.sort("company_id"))
    cols = {"weight_stability", "persist_hits", "positive_pillars", "top_factor_share",
            "hc_blockers", "rank_pct"}
    assert cols <= set(ev.results.columns)
    # Held run: a run-level problem keeps everyone out of High conviction, with the reason.
    # (Five eligible synthetic names: widen the band so rank 1 is in it.)
    sc = sc.model_copy(update={"tiers": sc.tiers.model_copy(
        update={"high_conviction_top_pct": 40.0, "watchlist_top_pct": 60.0})})
    held = evaluate_date(market, AS_OF, sc, uc, rf, set(), hist, trading_days(market),
                         run_check=lambda *a: ["prices are 9 days old"])
    assert "High conviction" not in set(held.results["tier"])
    top = held.results.filter(pl.col("rank") == 1).row(0, named=True)
    assert "run held: prices are 9 days old" in top["hc_blockers"]
