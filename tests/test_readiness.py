"""What a first score run needs: the readiness list on an empty app, and why a run left
every company out."""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl
import pytest

from igs import service
from igs.score.pipeline import universe_summary

APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"


def test_universe_summary_folds_the_quarter_counts():
    u = pl.DataFrame({"company_id": [1, 2, 3, 4], "included": [True, False, False, False],
                      "reason": [None, "only 3 quarters filed", "only 5 quarters filed",
                                 "market cap unavailable"]})
    assert universe_summary(u, 8) == {
        "seen": 4, "included": 1,
        "excluded": {"fewer than 8 quarters of results loaded": 2,
                     "market cap unavailable": 1}}
    assert universe_summary(pl.DataFrame(), 8) == {"seen": 0, "included": 0, "excluded": {}}


@pytest.mark.db
def test_readiness_counts_what_is_loaded(db_conn):
    import db_market
    empty = service.readiness(db_conn, 8)
    assert empty["prices"]["days"] == 0 and empty["results_enough"] == 0
    assert empty["shareholding"] == 0 and empty["listed"] == {}
    db_market.load(db_conn)
    r = service.readiness(db_conn, 8)
    assert r["prices"]["days"] > 200 and r["prices"]["symbols"] >= 5
    assert r["results_enough"] >= 5 and r["shareholding"] >= 5
    assert r["results_most"] >= 8


def _app(monkeypatch, gate_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    monkeypatch.setenv("IGS_GATE_PATH", str(gate_path))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    return at, " ".join(m.value for m in at.markdown)


@pytest.mark.db
def test_first_start_lists_what_is_missing(db_conn, tmp_path, monkeypatch):
    at, text = _app(monkeypatch, tmp_path / "no-gate.json")
    assert any(s.value == "No score run yet" for s in at.subheader)
    assert "❌ Missing - **Look-ahead gate**" in text and "uv run igs gate run" in text
    assert "❌ Missing - **Prices**" in text
    assert "no results filings listed" in text and "www.nseindia.com" in text
    assert "uv run igs score" in text


@pytest.mark.db
def test_a_run_that_ranks_nobody_says_why(db_conn, tmp_path, monkeypatch):
    """Prices without results: every company is seen and left out for its quarters."""
    import db_market

    from igs.score.pipeline import score_from_db
    db_market.load(db_conn)
    with db_conn.cursor() as cur:        # as if NSE had refused every results listing
        cur.execute("alter table fundamental_fact disable trigger user")
        cur.execute("delete from fundamental_fact")
        cur.execute("alter table fundamental_fact enable trigger user")
    db_conn.commit()
    run_id, run = score_from_db(db_conn, db_market.AS_OF, None, check_gate=False)
    assert run.results.is_empty()
    u = service.runs(db_conn)[0]["universe"]
    assert u["included"] == 0 and u["seen"] >= 5
    assert u["excluded"] == {"fewer than 8 quarters of results loaded": u["seen"]}
    at, text = _app(monkeypatch, tmp_path / "no-gate.json")
    warning = " ".join(w.value for w in at.warning)
    assert "No company made the universe" in warning
    assert f"fewer than 8 quarters of results loaded: {u['seen']}" in warning
    assert "✅ Ready - **Prices**" in text and "❌ Missing - **Quarterly results**" in text


@pytest.mark.db
def test_too_few_quarters_suggests_a_provisional_look(db_conn, tmp_path, monkeypatch):
    """Everything NSE lists today is loaded, but it reaches back only 7 quarters."""
    import db_market

    from igs.score.pipeline import score_from_db
    db_market.load(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("alter table fundamental_fact disable trigger user")
        cur.execute("""delete from fundamental_fact f
                       using (select company_id, period_end,
                                     dense_rank() over (partition by company_id
                                                        order by period_end desc) as k
                              from (select distinct company_id, period_end
                                    from fundamental_fact where period_type = 'Q') q) d
                       where f.company_id = d.company_id and f.period_end = d.period_end
                         and d.k > 7""")
        cur.execute("alter table fundamental_fact enable trigger user")
    db_conn.commit()
    r = service.readiness(db_conn, 8)
    assert r["results_enough"] == 0 and r["results_most"] == 7 and r["results_some"] >= 5
    score_from_db(db_conn, db_market.AS_OF, None, check_gate=False)
    _, text = _app(monkeypatch, tmp_path / "no-gate.json")
    assert "the most any has is 7 of the 8 quarters" in text
    assert "min_filing_quarters: 6" in text
