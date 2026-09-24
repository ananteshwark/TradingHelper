"""UI: chart specs (no browser) and the Streamlit app driven headlessly."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
from igs.ui import charts  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"
Q = [dt.date(2023, 6, 30), dt.date(2023, 9, 30), dt.date(2023, 12, 31), dt.date(2024, 3, 31)]


def _spec(chart) -> dict:
    return chart.to_dict()


def _layers(spec: dict) -> list[dict]:
    return spec.get("layer", [spec])


def test_financials_chart_single_axis_fixed_colours_and_tooltips():
    rows = [{"period_end": q, "revenue": 1e9 * (i + 1), "pat": 1e8 * (i + 1), "opm": 0.2}
            for i, q in enumerate(Q)]
    for theme in ("light", "dark"):
        spec = _spec(charts.financials_chart(rows, theme))
        enc = spec["encoding"]
        assert "y2" not in enc and enc["y"]["title"] == "INR crore"       # one axis
        assert enc["color"]["scale"]["range"] == charts.THEMES[theme]["series"][:2]
        assert enc["tooltip"]
        assert spec["mark"]["cornerRadiusTopLeft"] == 4


def test_margin_is_its_own_chart():
    rows = [{"period_end": q, "revenue": 1.0, "pat": 1.0, "opm": 0.2 + 0.01 * i}
            for i, q in enumerate(Q)]
    spec = _spec(charts.margin_chart(rows))
    marks = [lyr["mark"]["type"] for lyr in _layers(spec)]
    assert marks == ["line", "point"]
    assert all("color" not in lyr.get("encoding", {}) for lyr in _layers(spec))  # one series


def test_shareholding_colours_follow_the_entity_and_lines_are_labelled():
    rows = [{"period_end": q, "promoter": 55.0, "public": 45.0, "institutions_foreign": 15.0,
             "institutions_domestic": 10.0} for q in Q]
    spec = _spec(charts.shareholding_chart(rows))
    line = _layers(spec)[0]
    assert line["encoding"]["color"]["scale"]["domain"] == charts.SHAREHOLDER_ORDER
    text_layer = [lyr for lyr in _layers(spec) if lyr["mark"]["type"] == "text"][0]
    assert text_layer["mark"]["color"] == charts.THEMES["light"]["text2"]   # ink, not series
    # Dropping a category must not repaint the others.
    fewer = [{k: v for k, v in r.items() if k != "institutions_domestic"} for r in rows]
    spec2 = _spec(charts.shareholding_chart(fewer))
    assert _layers(spec2)[0]["encoding"]["color"]["scale"] == \
        line["encoding"]["color"]["scale"]


def test_contribution_chart_is_diverging():
    factors = [{"factor": "roce", "contribution": 0.3, "peer_percentile": 0.9, "value": 0.25},
               {"factor": "pb", "contribution": -0.1, "peer_percentile": 0.2, "value": 7.0}]
    spec = _spec(charts.contribution_chart(factors, "dark"))
    rng = spec["encoding"]["color"]["scale"]["range"]
    assert rng == [charts.THEMES["dark"]["pos"], charts.THEMES["dark"]["neg"]]


@pytest.mark.db
def test_app_renders_every_page(db_conn, tmp_path, monkeypatch):
    import db_market

    from igs.config import load_scoring, load_universe
    from igs.pit import gate
    from igs.score.pipeline import score_from_db

    db_market.load(db_conn)
    monkeypatch.setenv("IGS_GATE_PATH", str(tmp_path / "gate.json"))
    (tmp_path / "gate.json").write_text(json.dumps(
        {"fingerprint": gate.code_fingerprint(), "passed_at": "t", "summary": ""}))
    sc = load_scoring().model_copy(update={"peer_group": load_scoring().peer_group.model_copy(
        update={"min_peers": 2})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0})
    run_id, _ = score_from_db(db_conn, db_market.AS_OF, None, sc=sc, uc=uc)
    # Stored output of the optional assistant is shown without calling it.
    with db_conn.cursor() as cur:
        cur.execute("""insert into assistant_brief (run_id, symbol, prompt_version, model, text)
                       values (%s, 'BANK', 'brief-v1', 'claude-opus-5',
                               '**Where it stands** An example brief.')""", (run_id,))
        cur.execute("""insert into announcement_note (exchange, symbol, filed_at, subject,
                           category, materiality, summary, concerns, model, prompt_version)
                       values ('NSE', 'BANK', '2024-10-02 17:00+05:30', 'Rating',
                               'debt_or_credit_rating', 'medium', 'A rating was reaffirmed.',
                               '{}', 'claude-opus-5', 'announcements-v1')""")
    db_conn.commit()
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])

    at = st_testing.AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    assert any("Personal research tool" in w.value for w in at.warning)
    # No backtest IC report: the ranking says it is not yet validated.
    assert any("Not yet validated" in w.value for w in at.warning)
    assert at.dataframe and at.dataframe[0].value.shape[0] == 5

    # Open a stock from the rankings page (button callback switches page).
    at.selectbox(key="pick_symbol").select("BANK").run()
    at.button(key="open_stock").click().run()
    assert not at.exception, at.exception
    assert any("Example Bank Ltd" in h.value for h in at.header)
    assert any("Could not be checked" in t.value for t in at.text)
    assert any("An example brief" in m.value for m in at.markdown)
    assert any("materiality" in d.value.columns for d in at.dataframe)

    for page in ("Ask", "Watchlist", "Saved screens", "Data quality"):
        at.sidebar.radio(key="page").set_value(page).run()
        assert not at.exception, (page, at.exception)


def test_direct_labels_never_collide():
    import polars as pl
    df = pl.DataFrame({"holder": ["FII", "DII", "Promoter"], "pct": [12.1, 12.9, 55.0]})
    out = {r["holder"]: r["label_y"] for r in charts.dodge_labels(df, "pct", 3.0).to_dicts()}
    assert out["DII"] - out["FII"] >= 3.0 and out["Promoter"] == 55.0


def test_margin_chart_skipped_when_not_reported():
    assert not charts.has_margin([{"period_end": Q[0], "revenue": 1.0, "pat": 1.0,
                                   "opm": None}])
