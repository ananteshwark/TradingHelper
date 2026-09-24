"""Scoring end to end from the database: gate, universe, red flags, tiers,
explanations, persistence, service layer and API."""

from __future__ import annotations

import json

import db_market
import polars as pl
import pytest
from fastapi.testclient import TestClient

from igs import service
from igs.api.app import app, get_conn
from igs.config import load_scoring, load_universe
from igs.pit import gate
from igs.score.pipeline import score_from_db

pytestmark = pytest.mark.db


def _cfgs():
    sc = load_scoring().model_copy(update={"peer_group": load_scoring().peer_group.model_copy(
        update={"min_peers": 2})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0})
    return sc, uc


@pytest.fixture
def scored(db_conn, tmp_path, monkeypatch):
    db_market.load(db_conn)
    monkeypatch.setenv("IGS_GATE_PATH", str(tmp_path / "gate.json"))
    (tmp_path / "gate.json").write_text(json.dumps(
        {"fingerprint": gate.code_fingerprint(), "passed_at": "test", "summary": ""}))
    sc, uc = _cfgs()
    run_id, run = score_from_db(db_conn, db_market.AS_OF, None, sc=sc, uc=uc)
    return db_conn, run_id, run


def test_scoring_refuses_without_gate(db_conn, tmp_path, monkeypatch):
    db_market.load(db_conn)
    monkeypatch.setenv("IGS_GATE_PATH", str(tmp_path / "missing.json"))
    sc, uc = _cfgs()
    with pytest.raises(gate.GateError, match="no look-ahead gate record"):
        score_from_db(db_conn, db_market.AS_OF, None, sc=sc, uc=uc)


def test_tiers_and_red_flags(scored):
    conn, run_id, run = scored
    res = {r["company_id"]: r for r in run.results.iter_rows(named=True)}
    # GROW: 45.5% promoter pledge. LATE: CFO resignation.
    assert res[1]["tier"] == "Rejected" and "pledge" in res[1]["tier_reason"]
    assert res[6]["tier"] == "Rejected" and "resignation" in res[6]["tier_reason"]
    # GAPS is on the ASM list, so the universe (include_asm: false) excludes it before
    # scoring, with the reason kept.
    gaps = run.universe.filter(pl.col("company_id") == 5).row(0, named=True)
    assert not gaps["included"] and gaps["reason"] == "ASM surveillance" and 5 not in res
    with conn.cursor() as cur:
        cur.execute("select distinct industry_source from score_result where run_id = %s",
                    (run_id,))
        assert cur.fetchall() == [("nse_classification",)]
    flags = run.flags
    contingent = flags.filter(pl.col("flag") == "contingent_liabilities")
    assert set(contingent["status"]) == {"data_unavailable"}      # never a silent pass
    # Nobody can be High conviction while a red flag could not be evaluated.
    assert "High conviction" not in set(run.results["tier"])
    assert "factors_unvalidated" in [i.category for i in run.dq.issues]


def test_persisted_run_and_explanations(scored):
    conn, run_id, run = scored
    run_meta, rows = service.rankings(conn, run_id)
    assert run_meta["run_id"] == run_id and len(rows) == 5
    ranked = [r for r in rows if r["rank"] is not None]
    assert [r["rank"] for r in ranked] == sorted(r["rank"] for r in ranked)
    detail = service.stock_detail(conn, "BANK", run_id)
    assert detail["company"]["name"] == "Example Bank Ltd"
    text = detail["company"]["explanation"]
    assert "Composite score" in text and "Could not be checked" in text
    top = detail["top_contributions"]
    assert 0 < len(top) <= 5
    assert all(f["peer_percentile"] is not None for f in top)
    with_sources = [f for f in detail["factors"] if f["sources"]]
    assert with_sources and with_sources[0]["sources"][0]["source_url"].startswith("https://")
    assert len(detail["financials_8q"]) == 8
    assert detail["shareholding"] and detail["filings"] and detail["prices"]
    ev = next(f for f in detail["factors"] if f["factor"] == "ev_ebitda")
    assert ev["status"] == "not_applicable"                      # never for banks


def test_api(scored):
    conn, run_id, _ = scored

    def override():
        yield conn
    app.dependency_overrides[get_conn] = override
    try:
        c = TestClient(app)
        r = c.get("/rankings")
        assert r.status_code == 200 and r.headers["X-Disclaimer"].startswith("Personal")
        body = r.json()
        assert body["disclaimer"].startswith("Personal research tool") and body["count"] == 5
        assert c.get("/rankings", params={"tier": "Rejected"}).json()["count"] == 2
        csv = c.get("/rankings.csv").text
        assert csv.startswith("# Personal research tool") and "GROW" in csv
        assert c.get("/stocks/grow").status_code == 200
        why = c.get("/stocks/GROW/why").json()
        assert "pledge" in why["text"] and why["red_flags"][0]["status"] == "tripped"
        assert c.get("/stocks/NOPE").status_code == 404
        assert c.post("/watchlist", json={"symbol": "NBFC", "note": "check Q3"}).status_code \
            == 200
        assert [i["symbol"] for i in c.get("/watchlist").json()["items"]] == ["NBFC"]
        assert c.get("/rankings", params={"watchlist_only": True}).json()["count"] == 1
        assert c.post("/screens", json={"name": "cheap", "filters": {"tier": "Watchlist"}}
                      ).status_code == 200
        assert c.post("/screens", json={"name": "bad", "filters": {"colour": "red"}}
                      ).status_code == 422
        assert c.get("/screens/cheap/results").status_code == 200
        c.delete("/watchlist/NBFC")
        assert c.get("/watchlist").json()["items"] == []
        facets = c.get("/facets").json()["facets"]
        assert "Rejected" in facets["tier"]
    finally:
        app.dependency_overrides.clear()


def test_no_advice_language_anywhere_in_persisted_text(scored):
    from igs.guardrails import find_advice_language
    conn, run_id, _ = scored
    with conn.cursor() as cur:
        cur.execute("select explanation from score_result where run_id = %s", (run_id,))
        texts = [r[0] for r in cur.fetchall()]
        cur.execute("select message from red_flag_result where run_id = %s", (run_id,))
        texts += [r[0] for r in cur.fetchall()]
    assert texts and not any(find_advice_language(t) for t in texts)
