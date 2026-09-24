"""Run health: freshness of inputs and drift against the previous run."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest
import synthetic_market as M

from igs.config import load_scoring, load_universe
from igs.pit import PitDataset, PitView
from igs.score.health import HealthCheck, drift, freshness, summary
from igs.score.run import composite_at
from igs.timeutil import IST

AS_OF = dt.datetime(2024, 11, 29, 23, 59, tzinfo=IST)
CFG = load_scoring().run_health


def _cfgs():
    sc = load_scoring().model_copy(update={"peer_group": load_scoring().peer_group.model_copy(
        update={"min_peers": 2})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0})
    return sc, uc


@pytest.fixture(scope="module")
def market():
    return M.build()


def _with(market: PitDataset, **frames) -> PitDataset:
    t = {k: v.drop("known_at") for k, v in market.tables.items()}
    t.update(frames)
    return PitDataset.from_frames(**t)


def _inc(view: PitView, market: PitDataset) -> tuple:
    sc, uc = _cfgs()
    _, universe, _, res, _ = composite_at(market, view.as_of, sc, uc, set())
    return universe.filter(pl.col("included")), res


def test_missing_feeds_are_reported(market):
    view = PitView(market, AS_OF)
    inc, _ = _inc(view, market)
    issues = freshness(view, inc, CFG)
    assert "no announcement loaded" in issues and "no ASM/GSM list loaded" in issues
    assert not any(i.startswith("latest prices") for i in issues)


def test_stale_and_partial_prices(market):
    later = dt.datetime(2025, 1, 10, 23, 59, tzinfo=IST)     # prices end 2024-12-31
    view = PitView(market, later)
    inc, _ = _inc(view, market)
    assert any(i.startswith("latest prices are from 2024-12-31") for i in freshness(view, inc,
                                                                                   CFG))
    px = market.tables["prices"].drop("known_at")
    partial = px.filter(~((pl.col("trade_date") == dt.date(2024, 11, 29))
                          & pl.col("company_id").is_in([1, 2, 6])))
    ds = _with(market, prices=partial)
    view = PitView(ds, AS_OF)
    inc, _ = _inc(view, ds)
    assert any("of the universe has a price on 2024-11-29" in i
               for i in freshness(view, inc, CFG))


def test_drift_against_previous_run(market):
    view = PitView(market, AS_OF)
    inc, res = _inc(view, market)
    cur = summary(inc, res)
    assert drift(cur, cur, CFG) == []                         # same run: no drift
    prev = {**cur, "universe_size": cur["universe_size"] * 2,
            "median_coverage": (cur["median_coverage"] or 0) + 0.3,
            "factor_ok_share": {**cur["factor_ok_share"], "roe": 1.0},
            "top_decile": [999]}
    cur_bad = {**cur, "factor_ok_share": {**cur["factor_ok_share"], "roe": 0.2}}
    issues = drift(cur_bad, prev, CFG)
    assert any(i.startswith("universe size changed") for i in issues)
    assert any(i.startswith("median factor coverage fell") for i in issues)
    assert any("roe (100% -> 20%)" in i for i in issues)
    assert any("previous top decile" in i for i in issues)


def test_health_check_compares_only_with_a_recent_previous_run(market):
    view = PitView(market, AS_OF)
    inc, res = _inc(view, market)
    stale_prev = (AS_OF - dt.timedelta(days=90), {"universe_size": 1000})
    hc = HealthCheck(CFG, stale_prev)
    issues = hc(view, inc, res)
    assert not any(i.startswith("universe size") for i in issues)   # too old to compare
    assert hc.summary["universe_size"] == inc.height
    recent = HealthCheck(CFG, (AS_OF - dt.timedelta(days=30), {"universe_size": 1000}))
    assert any(i.startswith("universe size") for i in recent(view, inc, res))
    off = HealthCheck(CFG.model_copy(update={"enabled": False}))
    assert off(view, inc, res) == [] and off.summary


@pytest.mark.db
def test_pipeline_stores_health_and_uses_the_previous_run(db_conn, tmp_path, monkeypatch):
    import db_market

    from igs.pit import gate
    from igs.score.pipeline import previous_health, score_from_db
    db_market.load(db_conn)
    monkeypatch.setenv("IGS_GATE_PATH", str(tmp_path / "gate.json"))
    (tmp_path / "gate.json").write_text(json.dumps(
        {"fingerprint": gate.code_fingerprint(), "passed_at": "t", "summary": ""}))
    sc, uc = _cfgs()
    earlier = dt.datetime(2024, 10, 31, 23, 59, tzinfo=IST)
    assert previous_health(db_conn, earlier) is None
    first, _ = score_from_db(db_conn, earlier, None, sc=sc, uc=uc)
    second, run = score_from_db(db_conn, db_market.AS_OF, None, sc=sc, uc=uc)
    prev_as_of, prev = previous_health(db_conn, db_market.AS_OF)
    assert prev_as_of == earlier and prev["universe_size"] >= 1
    with db_conn.cursor() as cur:
        cur.execute("select health from score_run where run_id = %s", (second,))
        health = cur.fetchone()[0]
    assert health["issues"] == run.run_issues and health["issues"]
    assert "High conviction" not in set(run.results["tier"])
    assert any(i.category == "run_health" for i in run.dq.issues)
