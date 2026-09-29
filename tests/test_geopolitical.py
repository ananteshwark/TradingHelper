"""News-driven ratings: sourced AI output, bounded influence and no historical leakage."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import polars as pl
import pytest
from pydantic import ValidationError

from igs.config import GeopoliticalConfig
from igs.geopolitical import Article, apply_overlay, import_articles
from igs.pit.view import PitDataset, PitView
from igs.score.normalize import ScoreResult
from igs.timeutil import utc_now

UTC = dt.UTC
NOW = dt.datetime(2026, 9, 28, 12, tzinfo=UTC)


def article():
    return {"url": "https://example.com/news", "title": "Disruption of international shipping",
            "body": "Shipping services were suspended. Freight costs increased on the route.",
            "published_at": (utc_now() - dt.timedelta(hours=1)).isoformat(),
            "companies": [{"symbol": "BANK", "description": "The company finances exporters "
                           "using the affected shipping route.",
                           "source_url": "https://example.com/exposure"}]}


def row(**updates):
    return {"assessment_id": 1, "news_id": 1, "company_id": 1, "impact": -1.0,
            "confidence": 0.8, "rationale": "Higher shipping costs reduce margins.",
            "evidence": "Shipping services were suspended.", "channel": "supply_chain",
            "model": "test-model", "url": "https://example.com/news", "title": "Shipping",
            "published_at": NOW - dt.timedelta(days=7),
            "received_at": NOW - dt.timedelta(days=6),
            "assessed_at": NOW - dt.timedelta(days=6), "exposure": "imports by sea",
            "exposure_url": "https://example.com/exposure", **updates}


def overlay(rows, cfg=None, as_of=NOW, base=1.0):
    dataset = PitDataset.from_frames(geopolitical=pl.DataFrame(rows))
    res = ScoreResult(pl.DataFrame(), pl.DataFrame(), pl.DataFrame(
        {"company_id": [1], "composite": [base], "coverage": [1.0]},
        schema_overrides={"composite": pl.Float64}))
    return apply_overlay(res, PitView(dataset, as_of), cfg or GeopoliticalConfig()).composite


@pytest.mark.lookahead
def test_assessment_and_receipt_time_prevent_historical_leakage():
    rows = [row(assessed_at=NOW + dt.timedelta(seconds=1)),
            row(assessment_id=2, received_at=NOW + dt.timedelta(seconds=1))]
    assert overlay(rows)["geopolitical_adjustment"][0] == 0
    ds = PitDataset.from_frames(geopolitical=pl.DataFrame(rows))
    res = ScoreResult(pl.DataFrame(), pl.DataFrame(), pl.DataFrame(
        {"company_id": [1], "composite": [1.0], "coverage": [1.0]}))
    for source in (ds, ds.truncate(NOW), ds.poison_future(NOW)):
        assert apply_overlay(res, PitView(source, NOW), GeopoliticalConfig()
                             ).composite["composite"][0] == 1.0


def test_confidence_decay_cap_missing_and_stale():
    r = overlay([row()]).row(0, named=True)
    assert r["geopolitical_adjustment"] == pytest.approx(-0.15 * 0.8 * 0.5)
    assert r["composite"] == pytest.approx(0.94)
    assert json.loads(r["geopolitical_evidence"])[0]["model"] == "test-model"
    for changes in ({"confidence": 0.59}, {"published_at": NOW-dt.timedelta(days=22)}):
        assert overlay([row(**changes)])["composite"][0] == 1
    assert overlay([row()], GeopoliticalConfig(enabled=False))["composite"][0] == 1
    assert overlay([row()], base=None)["composite"][0] is None
    assert overlay([row()], base=None)["geopolitical_adjustment"][0] == 0
    one = row(impact=1.0, confidence=1.0, published_at=NOW)
    assert overlay([one] * 20)["geopolitical_adjustment"][0] == pytest.approx(0.15)
    assert overlay([row(), row(impact=1.0)])["composite"][0] == 1


def test_article_rejects_unsafe_urls_naive_dates_and_missing_exposure():
    for changes in ({"url": "file:///etc/passwd"}, {"published_at": "2026-09-01T10:00:00"},
                    {"companies": []}):
        with pytest.raises(ValidationError):
            Article.model_validate({**article(), **changes})


@pytest.mark.db
def test_atomic_import_dedupe_and_ai_validation(db_conn):
    import db_market

    from igs.assistant.geopolitical import assess_pending

    db_market.load(db_conn)
    assert import_articles(db_conn, [article()]) == 1
    assert import_articles(db_conn, [article()]) == 0
    assert import_articles(db_conn, [{**article(), "url": "https://elsewhere.com/news"}]) == 0
    bad = {**article(), "url": "https://example.com/other", "body": article()["body"] + " More."}
    bad["companies"] = [{**bad["companies"][0], "symbol": "NO_SUCH_ISSUER"}]
    with pytest.raises(ValueError, match="unknown or ambiguous"):
        import_articles(db_conn, [bad])
    with pytest.raises(ValueError, match="future"):
        import_articles(db_conn, [{**article(), "published_at":
                                  (utc_now()+dt.timedelta(days=1)).isoformat()}])
    cid = db_conn.execute("select (companies->0->>'company_id')::bigint "
                          "from geopolitical_news").fetchone()[0]
    output = {"items": [{"company_id": cid, "impact": -0.5, "confidence": 0.8,
                        "channel": "financing", "rationale": "Borrowers face higher costs "
                        "over the next month, but the disruption could resolve quickly.",
                        "evidence": "This quote is invented."}]}
    calls = []

    def structured(*args, **kwargs):
        calls.append(kwargs)
        return output, SimpleNamespace(model="test-model")

    assistant = SimpleNamespace(conn=db_conn, structured=structured)
    with pytest.raises(ValueError, match="quote"):
        assess_pending(assistant)
    assert db_conn.execute("select count(*) from geopolitical_assessment").fetchone()[0] == 0
    output["items"][0]["evidence"] = "Shipping services were suspended."
    output["items"][0]["confidence"] = float("nan")
    with pytest.raises(ValidationError):
        assess_pending(assistant)
    output["items"][0]["confidence"] = 0.8
    assert assess_pending(assistant) == 1
    n = len(calls)
    assert assess_pending(assistant) == 0 and len(calls) == n
    received, assessed = db_conn.execute("""select n.received_at, a.assessed_at
        from geopolitical_news n join geopolitical_assessment a using (news_id)""").fetchone()
    assert assessed >= received


@pytest.mark.db
@pytest.mark.lookahead
def test_loader_and_persisted_score_keep_base_evidence_and_rejections(db_conn):
    import db_market

    from igs.config import load_scoring, load_universe
    from igs.score.pipeline import score_from_db

    db_market.load(db_conn)
    # Explicit historical fixture timestamps stand in for a contemporaneous assessment.
    as_of = db_market.AS_OF
    companies = [{"company_id": 1, "description": "imports by sea",
                  "source_url": "https://example.com/exposure"}]
    news_id = db_conn.execute("""insert into geopolitical_news
        (url,title,body,published_at,received_at,content_hash,companies)
        values ('https://example.com/a','Shipping','Shipping suspended',%s,%s,'abc',%s)
        returning news_id""", (as_of-dt.timedelta(days=1), as_of-dt.timedelta(hours=1),
                                json.dumps(companies))).fetchone()[0]
    db_conn.execute("""insert into geopolitical_assessment
        (news_id,company_id,impact,confidence,rationale,evidence,channel,model,
         prompt_version,assessed_at) values (%s,1,1,1,'Test rationale','Shipping suspended',
         'supply_chain','test-model','test-v1',%s)""", (news_id, as_of))
    db_conn.commit()
    sc = load_scoring()
    sc = sc.model_copy(update={"peer_group": sc.peer_group.model_copy(update={"min_peers": 2})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0})
    run_id, run = score_from_db(db_conn, as_of, None, sc=sc, uc=uc, check_gate=False)
    stored = db_conn.execute("""select base_composite, composite, geopolitical_adjustment,
        geopolitical_evidence, tier from score_result where run_id=%s and company_id=1""",
        (run_id,)).fetchone()
    assert stored[2] > 0 and stored[1] == pytest.approx(stored[0]+stored[2])
    assert stored[3][0]["news_id"] == news_id
    assert stored[4] == "Rejected"  # A favorable news assessment cannot override pledging.
    assert "Experimental geopolitical adjustment" in db_conn.execute(
        "select explanation from score_result where run_id=%s and company_id=1",
        (run_id,)).fetchone()[0]
