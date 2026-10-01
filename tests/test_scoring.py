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
    # Not in the quarterly filings at all, so it is configured not to block High
    # conviction; it is still reported as not checked.
    assert set(contingent["unavailable_blocks"]) == {False}
    blockers = [b for bs in run.results["hc_blockers"].to_list() for b in (bs or [])]
    assert not any("contingent" in b for b in blockers)
    # This market's announcements and surveillance list are stale, so run health
    # still withholds the tier.
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
        assert all('growth_profile' in row for row in body['rows'])
        assert body["disclaimer"].startswith("Personal research tool") and body["count"] == 5
        assert c.get("/rankings", params={"tier": "Rejected"}).json()["count"] == 2
        csv = c.get("/rankings.csv").text
        assert csv.startswith("# Personal research tool") and "GROW" in csv
        assert c.get("/stocks/grow").status_code == 200
        stored = conn.execute('select growth_profile from score_result '
                              'where run_id=%s and company_id=1', (run_id,)).fetchone()[0]
        assert stored['profile'] == 'Risk blocked'
        assert stored['evidence']['revenue_quarter_yoy']['source_fact_ids']
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


def test_historical_stock_uses_run_identity_after_symbol_reuse(scored):
    conn, run_id, _ = scored
    original = service.stock_detail(conn, "BANK", run_id)["company"]
    with conn.cursor() as cur:
        cur.execute("update security_identifier set valid_to = '2025-01-01' "
                    "where id_type = 'NSE_SYMBOL' and id_value = 'BANK'")
        cur.execute("insert into company (name) values ('Different issuer') returning company_id")
        cid = cur.fetchone()[0]
        cur.execute("insert into security (company_id) values (%s) returning security_id", (cid,))
        sid = cur.fetchone()[0]
        cur.execute("""insert into security_identifier
            (security_id, id_type, id_value, valid_from, evidence)
            values (%s, 'NSE_SYMBOL', 'BANK', '2025-01-01', 'test')""", (sid,))
    conn.commit()
    assert service.stock_detail(conn, "BANK", run_id)["company"] == original


def test_sme_config_reaches_database_loader(db_conn, monkeypatch):
    from igs.score import pipeline

    seen = []

    def loader(*args, **kwargs):
        seen.extend(kwargs["series"])
        raise RuntimeError("stop after checking dataset selection")

    monkeypatch.setattr(pipeline, "load_dataset", loader)
    uc = load_universe().model_copy(update={"include_sme": True})
    with pytest.raises(RuntimeError, match="dataset selection"):
        pipeline.score_from_db(db_conn, db_market.AS_OF, None, uc=uc)
    assert set(seen) == set(uc.include_series + uc.sme_series)


def test_find_a_company_by_name_or_symbol(scored):
    """Asked for by the owner: stocks could be looked up only by NSE symbol."""
    conn, run_id, _ = scored

    def find(q):
        return [c["symbol"] for c in service.companies(conn, run_id, q)]
    assert find("bank") == ["BANK"]                          # the symbol, or the name
    assert find("example") == ["BANK", "NBFC"]               # names that start with it
    assert find("FINANCE example") == ["NBFC"]               # any order and case
    assert find("Example Finance Limited") == find("example finance ltd") == ["NBFC"]
    assert find("50%") == find("_") == []                    # typed literally
    everyone = service.companies(conn, run_id)
    assert [c["name"] for c in everyone] == sorted(db_market.NAMES.values())
    # A company outside the run is found too: it can go on the watchlist, and its page
    # says why there is nothing to show.
    out = [c for c in everyone if not c["in_run"]]
    assert len(out) == 1 and find(out[0]["name"].split()[0]) == [out[0]["symbol"]]
    with pytest.raises(service.NotFound, match=rf"{out[0]['name']} \({out[0]['symbol']}\) "
                                                rf"is not in run {run_id}: the screening"):
        service.stock_detail(conn, out[0]["symbol"], run_id)
    # The rankings filter reads a search the same way.
    _, rows = service.rankings(conn, run_id, q="finance example ltd")
    assert [r["symbol"] for r in rows] == ["NBFC"]

    def override():
        yield conn
    app.dependency_overrides[get_conn] = override
    try:
        body = TestClient(app).get("/companies", params={"q": "example bank"}).json()
        assert body["disclaimer"].startswith("Personal research tool")
        assert [(r["symbol"], r["name"], r["in_run"]) for r in body["rows"]] == \
            [("BANK", "Example Bank Ltd", True)]
    finally:
        app.dependency_overrides.clear()


def test_key_numbers_are_stored_with_the_run_and_match_the_factors(scored):
    """Asked for by the owner: the stock page shows 52-week high/low, P/E, ROCE and the
    like. They are stored with the run, from its point-in-time data."""
    conn, run_id, _ = scored
    d = service.stock_detail(conn, "GROW", run_id)
    kn, co = d["company"]["key_numbers"], d["company"]
    assert kn["price_date"] == "2024-11-29" and kn["low_52w"] <= kn["price"] <= kn["high_52w"]
    assert abs(kn["mcap_cr"] - float(co["mcap_cr"])) < 1e-6 * kn["mcap_cr"]
    assert abs(kn["pe"] - kn["mcap_cr"] / kn["pat_ttm_cr"]) < 1e-9 * kn["pe"]
    pb = next(f for f in d["factors"] if f["factor"] == "pb")
    assert abs(kn["pb"] - pb["value"]) < 1e-9 * pb["value"]             # the factor's own P/B
    assert abs(kn["eps_ttm"] * kn["mcap_cr"] / kn["price"] - kn["pat_ttm_cr"]) < 1e-6
    assert kn["promoter_pct"] == 55.0 and kn["debt_to_equity"] is not None
    bank = service.stock_detail(conn, "BANK", run_id)["company"]["key_numbers"]
    assert bank["financial"] and bank["debt_to_equity"] is None        # not for banks
    peers = co["industry_pe"]
    assert peers is None or peers["companies"] >= 1
