"""AI buy / hold / sell calls, offline: a scripted client stands in for the Claude API. The
call is made from everything stored on the stock, kept with its inputs, never changed, and
measured against the Nifty 500 afterwards."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import psycopg
import pytest
from test_assistant import FakeClient, _cfg, msg, text

from igs.alerts.delivery import digest
from igs.alerts.rules import Alert, watchlist_ai_calls
from igs.assistant import calls as ai
from igs.assistant.llm import Assistant, AssistantError
from igs.guardrails import AdviceLanguageError

APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"


@pytest.fixture
def scored(db_conn, tmp_path, monkeypatch):
    """The synthetic market loaded and scored as of 2024-11-29 (as in test_assistant)."""
    import db_market

    from igs.config import load_scoring, load_universe
    from igs.pit import gate
    from igs.score.pipeline import score_from_db
    db_market.load(db_conn)
    monkeypatch.setenv("IGS_GATE_PATH", str(tmp_path / "gate.json"))
    (tmp_path / "gate.json").write_text(json.dumps(
        {"fingerprint": gate.code_fingerprint(), "passed_at": "test", "summary": ""}))
    sc = load_scoring().model_copy(update={"peer_group": load_scoring().peer_group.model_copy(
        update={"min_peers": 2})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0})
    run_id, _ = score_from_db(db_conn, db_market.AS_OF, None, sc=sc, uc=uc)
    db_conn.commit()
    return db_conn, run_id


def answer(action: str = "buy", **over) -> dict:
    return {"action": action, "confidence": 0.62, "horizon_months": 12,
            "summary": "Quality and momentum are both in the top quintile of peers and "
                       "results keep improving.",
            "reasons": ["ROCE 24.1%, 82nd percentile of peers", "Revenue up 18% a year"],
            "risks": ["A weak monsoon could slow demand"],
            "buy_when": ["Quality and momentum in the top quintile (met now)"],
            "sell_when": ["Operating margin below 12% in the next results",
                          "Close below the 200-day average"],
            "data_gaps": [], **over}


def reply(**over) -> FakeClient:
    return FakeClient(msg(text(json.dumps(answer(**over)))))


def _count(conn, sql: str, params: tuple = ()) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


# --------------------------------------------------------------------------- prices


def test_the_price_index_ignores_splits():
    """A 1:5 split halves nothing: the exchange's previous close is adjusted on the ex-date,
    so the day's move is 510 / 500, not 102 / 510."""
    d = [dt.date(2024, 1, i) for i in (1, 2, 3)]
    rows = [(d[0], 500.0, None), (d[1], 510.0, 500.0), (d[2], 102.0, 102.0)]
    assert [round(v, 4) for _, v in ai.price_index(rows)] == [1.0, 1.02, 1.02]


@pytest.mark.db
def test_the_price_summary_the_ai_reads(scored):
    conn, _ = scored
    import db_market
    snap = ai.market_snapshot(conn, 1, db_market.AS_OF)
    assert snap["last_date"] == dt.date(2024, 11, 29)
    assert set(snap["returns_pct"]) == set(snap["nifty500_returns_pct"]) == set(ai.HORIZONS)
    assert snap["low_52w"] <= snap["last_close"] <= snap["high_52w"]
    assert {"avg_50d", "avg_200d", "volatility_60d_pct", "vs_avg_200d_pct"} <= set(snap)


# --------------------------------------------------------------------------- the call


@pytest.mark.db
def test_a_call_is_made_from_everything_stored_and_kept(scored):
    conn, run_id = scored
    client = reply()
    c = ai.make_call(Assistant.open(conn, _cfg(), client), "grow")
    assert (c["symbol"], c["action"], c["run_id"]) == ("GROW", "buy", run_id)
    assert c["price_date"] == dt.date(2024, 11, 29) and c["price_close"] > 0

    request = client.requests[0]
    assert request["output_config"]["effort"] == "high"
    schema = request["output_config"]["format"]["schema"]
    assert schema["properties"]["action"]["enum"] == ["buy", "hold", "sell"]
    assert "minItems" not in json.dumps(schema) and "maximum" not in json.dumps(schema)
    prompt = request["messages"][0]["content"]
    for section in ("result", "factors", "quarters", "shareholding", "filings",
                    "insider_trades_12m", "news_adjustment", "market"):
        assert f"<{section}>" in prompt
    assert "Rejected" in prompt                 # the screen's tier is part of what it reads

    row = conn.execute("""select action, confidence, sell_when, inputs->'market'->>'last_date',
                                 trigger, prompt_version from ai_call""").fetchall()
    assert row == [("buy", 0.62, answer()["sell_when"], "2024-11-29", "manual",
                    ai.PROMPT_VERSION)]
    assert _count(conn, "select count(*) from llm_call where feature = 'call'") == 1
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("update ai_call set action = 'sell'")      # a call is never changed
    conn.rollback()


@pytest.mark.db
@pytest.mark.parametrize("bad", [{"confidence": 1.5}, {"sell_when": []},
                                 {"action": "accumulate"}, {"horizon_months": 60}])
def test_an_incomplete_call_is_not_stored(scored, bad):
    conn, _ = scored
    with pytest.raises(AssistantError, match="not stored"):
        ai.make_call(Assistant.open(conn, _cfg(), reply(**bad)), "GROW")
    assert _count(conn, "select count(*) from ai_call") == 0


# --------------------------------------------------------------------------- record


def _insert(conn, run_id: int, company_id: int, symbol: str, action: str, day: dt.date,
            created: dt.datetime | None = None) -> None:
    close = conn.execute("""select p.close::float8 from price_eod p
                            join security_identifier si on si.id_type = 'ISIN'
                             and si.id_value = p.isin
                            join security s using (security_id)
                            where s.company_id = %s and p.trade_date = %s""",
                         (company_id, day)).fetchone()[0]
    a = answer(action)
    conn.execute("""insert into ai_call (company_id, symbol, run_id, action, confidence,
            horizon_months, summary, reasons, risks, buy_when, sell_when, data_gaps,
            price_date, price_close, inputs, model, prompt_version, trigger, created_at)
        values (%s, %s, %s, %s, 0.6, 12, %s, %s, %s, %s, %s, '[]', %s, %s, '{}', 'm', 'v',
                'scheduled', coalesce(%s, now()))""",
                 (company_id, symbol, run_id, action, a["summary"], json.dumps(a["reasons"]),
                  json.dumps(a["risks"]), json.dumps(a["buy_when"]),
                  json.dumps(a["sell_when"]), day, close, created))
    conn.commit()


@pytest.mark.db
def test_calls_are_measured_against_the_nifty_500_through_a_split(scored):
    """GROW splits 10:2 on 2022-06-15. A buy on 1 June is measured on the adjusted chain,
    so the split is not an 80% loss."""
    conn, run_id = scored
    start = dt.date(2022, 6, 1)
    _insert(conn, run_id, 1, "GROW", "buy", start)
    _insert(conn, run_id, 2, "CYCL", "sell", start)
    record = ai.track_record(conn, today=dt.date(2022, 9, 30))
    grow = next(c for c in record["calls"] if c["symbol"] == "GROW")
    h = grow["outcome"]["horizons"]
    assert set(h) == {"1m", "3m"}                    # 6 and 12 months haven't passed
    px = dict(conn.execute("""select trade_date, close::float8 from price_eod
                              where symbol = 'GROW' and trade_date between %s and %s""",
                           (start, dt.date(2022, 7, 1))).fetchall())
    after = px[max(d for d in px if d <= dt.date(2022, 7, 1))]
    assert h["1m"]["return_pct"] == pytest.approx(100 * (after * 5 / px[start] - 1), abs=0.1)
    assert h["1m"]["right"] == (h["1m"]["excess_pct"] > 0)
    cycl = next(c for c in record["calls"] if c["symbol"] == "CYCL")
    assert cycl["outcome"]["horizons"]["1m"]["right"] == \
        (cycl["outcome"]["horizons"]["1m"]["excess_pct"] < 0)
    summary = {(s["action"], s["horizon"]): s for s in record["summary"]}
    assert summary[("buy", "1m")]["calls"] == 1 and ("buy", "6m") not in summary


# --------------------------------------------------------------------------- daily job


@pytest.mark.db
def test_the_daily_job_calls_watchlist_stocks_when_due(scored):
    from igs import service
    conn, run_id = scored
    for sym in ("GROW", "NBFC", "BANK"):
        service.watchlist_add(conn, sym)
    cfg = _cfg()
    cfg = cfg.model_copy(update={"features": cfg.features.model_copy(update={
        "call": cfg.features.call.model_copy(update={"max_per_day": 2})})})
    client = FakeClient(*[msg(text(json.dumps(answer()))) for _ in range(2)])
    made = ai.scheduled_calls(Assistant.open(conn, cfg, client), run_id)
    assert [c["symbol"] for c in made.made] == ["BANK", "GROW"] and not made.issues
    # The day's two are spent; tomorrow the third is due, the others not for a week.
    assert ai.scheduled_calls(Assistant.open(conn, cfg, FakeClient()), run_id).made == []
    assert ai.due_for_call(conn, run_id, 7) == ["NBFC"]
    # In a later run where BANK's tier has changed, BANK is due again at once.
    later = conn.execute("""insert into score_run (as_of, gate_fingerprint, config)
                            select as_of, gate_fingerprint, config from score_run
                            where run_id = %s returning run_id""", (run_id,)).fetchone()[0]
    conn.execute("create temp table copied as select * from score_result where run_id = %s",
                 (run_id,))
    conn.execute("""update copied set run_id = %s, tier = case when symbol <> 'BANK' then tier
                        when tier = 'Watchlist' then 'Not shortlisted' else 'Watchlist' end""",
                 (later,))
    conn.execute("insert into score_result select * from copied")
    conn.commit()
    assert ai.due_for_call(conn, later, 7) == ["NBFC", "BANK"]


# --------------------------------------------------------------------------- alerts


@pytest.mark.db
def test_ai_call_alerts_say_buy_and_sell_but_nothing_else_may(scored):
    from igs import service
    conn, run_id = scored
    service.watchlist_add(conn, "GROW")
    since = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    day = dt.date(2024, 11, 29)
    now = dt.datetime.now(dt.UTC)
    _insert(conn, run_id, 1, "GROW", "buy", day, now - dt.timedelta(days=2))
    _insert(conn, run_id, 1, "GROW", "buy", day, now - dt.timedelta(days=1))
    _insert(conn, run_id, 1, "GROW", "sell", day, now)
    alerts = watchlist_ai_calls(conn, since, {"changes_only": True})
    assert [a.message.split(" (")[0] for a in alerts] == [
        "GROW: AI call BUY", "GROW: AI call SELL, was buy"]  # the repeated buy is not sent
    text_ = digest(alerts, run_id, now)
    assert "AI call SELL" in text_ and "not the screen's" in text_
    with pytest.raises(AdviceLanguageError):
        digest([Alert("top_decile_entry", 1, "GROW is a strong buy", "k")], run_id, now)


# --------------------------------------------------------------------------- UI


@pytest.mark.db
def test_the_ai_calls_page_shows_the_record(scored, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    conn, run_id = scored
    _insert(conn, run_id, 1, "GROW", "buy", dt.date(2024, 6, 28))
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "AI calls"
    at.run()
    assert not at.exception, at.exception
    assert any(h.value == "AI calls" for h in at.header)
    assert any("GROW" in str(df.value) for df in at.dataframe)
