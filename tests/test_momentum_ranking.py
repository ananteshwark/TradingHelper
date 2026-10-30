"""The ranking scores momentum alone, ranked across the whole universe; the other pillars
are warnings (config/scoring.yaml, research/ml_rating)."""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from pydantic import ValidationError
from test_normalize import _long, _peers

import igs.factors  # noqa: F401
from igs import momentum
from igs.config import ScoringConfig, load_scoring
from igs.score.normalize import composite, normalise
from igs.score.run import pillar_warnings

CFG = load_scoring()


def test_the_score_is_momentum_and_the_other_pillars_are_warnings():
    assert CFG.pillar_weights == {"momentum": 1.0, "quality": 0.0, "valuation": 0.0,
                                  "low_volatility": 0.0, "growth": 0.0, "ownership": 0.0}
    assert CFG.peer_group.market_wide_pillars == ["momentum"]
    assert CFG.pillar_warnings.below == -1.0
    # Two momentum factors, so neither may carry more than three quarters of the score.
    assert CFG.robustness.max_factor_share == 0.75


def test_an_unknown_market_wide_pillar_is_refused():
    raw = CFG.model_dump()
    raw["peer_group"]["market_wide_pillars"] = ["luck"]
    with pytest.raises(ValidationError, match="market_wide_pillars"):
        ScoringConfig.model_validate(raw)


def test_momentum_is_ranked_across_all_stocks_and_quality_within_industry():
    # IT rose 40-49%, Steel 0-9%: across the market every IT stock beats every Steel one.
    rows = [(i, "risk_adj_return_6m", (0.40 if i <= 10 else 0.0) + 0.01 * (i % 10), "ok")
            for i in range(1, 21)]
    rows += [(i, "roce", (0.30 if i <= 10 else 0.05) + 0.001 * i, "ok") for i in range(1, 21)]
    out = normalise(_long(rows), _peers(20, industry=lambda i: "IT" if i <= 10 else "Steel"),
                    CFG)
    mom = {r["company_id"]: r for r in out.filter(pl.col("factor") == "risk_adj_return_6m")
           .iter_rows(named=True)}
    assert {r["peer_level"] for r in mom.values()} == {"market"}
    assert {r["peer_count"] for r in mom.values()} == {20}
    assert min(mom[i]["z"] for i in range(1, 11)) > max(mom[i]["z"] for i in range(11, 21))
    assert mom[9]["peer_percentile"] == 1.0 and mom[20]["peer_percentile"] == 0.05
    roce = out.filter(pl.col("factor") == "roce")
    assert set(roce["peer_level"]) == {"industry"}


def test_the_composite_is_the_momentum_pillar():
    rows = []
    for i in range(1, 11):
        rows += [(i, "risk_adj_return_6m", float(i), "ok"),
                 (i, "risk_adj_return_12m", float(i), "ok"),
                 (i, "roce", float(11 - i), "ok"), (i, "pb", float(i), "ok"),
                 (i, "volatility_1y", float(i), "ok"), (i, "revenue_cagr_3y", float(-i), "ok")]
    res = composite(normalise(_long(rows), _peers(10), CFG), CFG)
    comp = dict(res.composite.select("company_id", "composite").iter_rows())
    mom = dict(res.pillars.filter(pl.col("pillar") == "momentum")
               .select("company_id", "score").iter_rows())
    assert comp == pytest.approx(mom)
    assert max(comp, key=comp.get) == 10                      # the best momentum, whatever else
    # Unscored pillars are still computed, for the warnings and the stock page.
    assert set(res.pillars["pillar"]) >= {"quality", "valuation", "growth", "low_volatility"}
    # Only the momentum factors contribute.
    contributing = res.factors.filter(pl.col("contribution") != 0)["factor"].unique().to_list()
    assert sorted(contributing) == ["risk_adj_return_12m", "risk_adj_return_6m"]


def test_a_weak_unscored_pillar_is_a_warning_and_momentum_never_is():
    pillars = pl.DataFrame({
        "company_id": [1, 1, 1, 2, 2],
        "pillar": ["quality", "valuation", "momentum", "quality", "growth"],
        "score": [-1.43, -2.06, -3.0, -0.5, None]})
    w = dict(pillar_warnings(pillars, CFG).iter_rows())
    assert w == {1: ["weak valuation (-2.1)", "weak quality (-1.4)"]}


@pytest.mark.db
def test_the_rankings_track_follows_the_score_run_and_fills_from_the_rule(db_conn):
    ids = []
    for sym in ("A", "B", "C", "D"):
        ids.append(db_conn.execute("insert into company(name) values(%s) returning company_id",
                                   (sym,)).fetchone()[0])
    run_id = db_conn.execute("""insert into score_run (as_of, gate_fingerprint, config,
        dq_summary) values ('2026-10-30 12:00+00', 'x', '{}', '{}') returning run_id"""
                             ).fetchone()[0]
    for cid, sym, comp, tier in ((ids[0], "A", 0.5, "Watchlist"),
                                 (ids[1], "B", 2.0, "Rejected"),
                                 (ids[2], "C", 1.0, "High conviction")):
        db_conn.execute("""insert into score_result (run_id, company_id, symbol, composite,
            coverage, scored, tier, explanation) values (%s, %s, %s, %s, 1, 3, %s, '')""",
                        (run_id, cid, sym, comp, tier))
    db_conn.commit()
    ranking = [{"company_id": c, "symbol": s, "score_pct": 10.0 - k, "rank": k + 1}
               for k, (c, s) in enumerate(zip(ids, "ABCD", strict=True))]
    run = {"run_id": run_id, "as_of": dt.datetime(2026, 10, 30, 12, tzinfo=dt.UTC)}
    picks, _, note = momentum.ranking_picks(db_conn, run, ranking, 3)
    # C before A by composite. B is Rejected, so it stays out; D, which the run did not
    # score, fills the third place.
    assert [p["symbol"] for p in picks] == ["C", "A", "D"]
    assert f"score run {run_id}" in note and "D" in note
    rule, _, why = momentum.ranking_picks(db_conn, None, ranking, 3)
    assert [p["symbol"] for p in rule] == ["A", "B", "C"] and "rule's picks" in why
