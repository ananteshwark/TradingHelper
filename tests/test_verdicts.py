"""The AI's verdict on every broker's call: calls no AI call covered wait for a review of
their stock's data, stocks outside the ranking included; a "cannot judge" is reviewed again
once a Screener.in export arrives; unmatched calls are linked by the owner. A scripted
client stands in for the Claude API."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest
import test_ai_calls
import xlsx_files
from test_assistant import FakeClient, _cfg, msg, text

from igs import brokers, service
from igs.assistant import calls as ai
from igs.assistant import verdicts
from igs.assistant.llm import Assistant, AssistantError
from igs.dq import DQLog
from igs.ingest.manual import import_screener_bytes
from igs.ingest.raw_store import RawStore
from igs.timeutil import IST

scored = test_ai_calls.scored
APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"
TODAY = dt.datetime.now(IST).date()


def _ids(conn, symbol: str) -> list[int]:
    return [r[0] for r in conn.execute(
        """select c.broker_call_id from broker_call c join security s using (company_id)
           join security_identifier si using (security_id)
           where si.id_type = 'NSE_SYMBOL' and si.id_value = %s
           order by c.broker_call_id""", (symbol,)).fetchall()]


def answer(ids: list[int], verdict: str = "agree", gaps: list[str] | None = None) -> FakeClient:
    return FakeClient(msg(text(json.dumps({
        "broker_verdicts": [{"id": i, "verdict": verdict, "confidence": 0.82,
                             "reason": f"Sales grew 18% while call {i} expects growth."}
                            for i in ids],
        "data_gaps": gaps or []}))))


def assistant(conn, client, **features) -> Assistant:
    cfg = _cfg()
    if features:
        cfg = cfg.model_copy(update={"features": cfg.features.model_copy(update={
            "verdicts": cfg.features.verdicts.model_copy(update=features)})})
    return Assistant.open(conn, cfg, client)


@pytest.mark.db
def test_every_broker_call_waits_for_a_verdict_until_one_is_given(scored, monkeypatch):
    """On the run's date, so a buy / hold / sell call sees the same brokers' calls."""
    conn, run_id = scored
    day = dt.date(2024, 11, 28)
    monkeypatch.setattr(verdicts, "_today", lambda: dt.date(2024, 11, 29))
    brokers.add_manual(conn, "BANK", "Kotak Institutional Equities", "buy", 12500.0, day,
                       rating="Add")
    brokers.add_manual(conn, "BANK", "Jefferies", "sell", 9000.0, day,
                       rating="Underperform")
    brokers.add_manual(conn, "GAPS", "Axis Securities", "buy", 410.0, day, kind="trading")
    due = verdicts.pending(conn, 30)
    assert [(p.symbol, p.reason) for p in due] == [
        ("BANK", "2 brokers' calls without the AI's verdict"),
        ("GAPS", "1 broker's call without the AI's verdict")]

    bank = _ids(conn, "BANK")
    client = answer([*bank, 999999])                  # a verdict on a call it wasn't shown
    got = verdicts.review(assistant(conn, client), "BANK", run_id)
    assert got["in_run"] and sorted(v["id"] for v in got["verdicts"]) == bank
    assert got["reason"] == "2 brokers' calls without the AI's verdict"
    sent = client.requests[0]["messages"][0]["content"]
    assert "<factors>" in sent and "<broker_calls>" in sent and "Jefferies" in sent
    system = client.requests[0]["system"][0]["text"]
    assert "trading idea" in system and "<screener>" in system
    assert "cannot judge\" only when neither" in system
    row = conn.execute("""select in_run, prompt_version, trigger, reason from ai_broker_review
                          where review_id = %s""", (got["review_id"],)).fetchone()
    assert row == (True, "verdicts-v2", "manual", got["reason"])
    shown = brokers.calls_for(conn, 3, day - dt.timedelta(days=5))
    assert {(r["broker"], r["ai_verdict"]) for r in shown} == {
        ("Kotak Institutional Equities", "agree"), ("Jefferies", "agree")}
    assert [p.symbol for p in verdicts.pending(conn, 30)] == ["GAPS"]

    # A later AI call's verdict on the same broker's call is the one shown.
    kotak = bank[0]
    ai.make_call(Assistant.open(conn, _cfg(), test_ai_calls.reply(broker_verdicts=[
        {"id": kotak, "verdict": "disagree", "confidence": 0.7,
         "reason": "Net interest margin fell 40 bps."}])),
        "BANK", run_id)
    latest = {r["broker"]: r["ai_verdict"] for r in brokers.calls_for(
        conn, 3, day - dt.timedelta(days=5))}
    assert latest == {"Kotak Institutional Equities": "disagree", "Jefferies": "agree"}
    assert [len(r["verdicts"]) for r in verdicts.reviews(conn, "BANK")] == [2]

    # Verdicts on none of the calls it was shown are refused, not stored.
    with pytest.raises(AssistantError, match="named none"):
        verdicts.review(assistant(conn, answer([999999])), "GAPS", run_id)


@pytest.mark.db
def test_a_stock_outside_the_ranking_is_reviewed_with_a_screener_export(scored, tmp_path):
    """GAPS is outside the run: it is judged on its results, filings and prices. Its
    "cannot judge" is reviewed again once a Screener.in export for it is imported, and
    the export is part of what the AI reads."""
    conn, run_id = scored
    brokers.add_manual(conn, "GAPS", "Axis Securities", "buy", 410.0, TODAY)
    ids = _ids(conn, "GAPS")
    client = answer(ids, "cannot judge", ["No balance sheet or cash flow loaded."])
    got = verdicts.review(assistant(conn, client), "GAPS", run_id)
    assert not got["in_run"] and not got["used_screener"]
    assert got["data_gaps"] == ["No balance sheet or cash flow loaded."]
    sent = client.requests[0]["messages"][0]["content"]
    assert '"in_run":false' in sent and "<quarters>" in sent and "<market>" in sent
    assert "<factors>" not in sent and "<screener>" not in sent
    assert verdicts.pending(conn, 30) == []

    import_screener_bytes(conn, RawStore(tmp_path), xlsx_files.screener_export(
        "Gaps Engineering Ltd", {dt.date(2024, 9, 30): (210.0, 21.0)}), "Gaps.xlsx",
        DQLog())
    conn.commit()
    due = verdicts.pending(conn, 30)
    assert [(p.symbol, p.reason) for p in due] == [
        ("GAPS", '1 judged "cannot judge" before a Screener.in export arrived')]
    client = answer(ids, "agree")
    got = verdicts.review(assistant(conn, client), "GAPS", run_id)
    assert got["used_screener"]
    sent = client.requests[0]["messages"][0]["content"]
    assert "<screener>" in sent and "Gaps Engineering Ltd" in sent
    assert brokers.calls_for(conn, 5, TODAY - dt.timedelta(days=5))[0]["ai_verdict"] == "agree"
    assert verdicts.pending(conn, 30) == []


@pytest.mark.db
def test_the_daily_reviews_stop_at_max_per_day(scored):
    conn, run_id = scored
    for symbol in ("BANK", "CYCL", "GAPS"):
        brokers.add_manual(conn, symbol, "Jefferies", "buy", 500.0, TODAY)
    every = [i for s in ("BANK", "CYCL", "GAPS") for i in _ids(conn, s)]
    client = FakeClient(*(answer(every).script * 2))
    made = verdicts.scheduled(assistant(conn, client, max_per_day=2), run_id)
    assert [r["symbol"] for r in made.made] == ["BANK", "CYCL"] and made.waiting == 1
    assert str(made).startswith("verdicts on brokers' calls for 2 stocks (BANK 1, CYCL 1)")
    again = verdicts.scheduled(assistant(conn, client, max_per_day=2), run_id)
    assert again.made == [] and again.waiting == 1          # today's two are used up
    assert verdicts.scheduled(assistant(conn, client, scheduled=False), run_id).made == []


@pytest.mark.db
def test_an_unmatched_call_is_linked_and_counts_from_then(scored):
    conn, _ = scored
    assert brokers.add_call(conn, company_id=None, stock_name="Grow Inds", broker="Nomura",
                            stance="buy", rating="Buy", kind="research", target_price=900.0,
                            called_on=TODAY - dt.timedelta(days=2), source="news")
    conn.commit()
    [row] = brokers.unmatched(conn, 30)
    assert row["stock_name"] == "Grow Inds" and verdicts.pending(conn, 30) == []
    before = conn.execute("select created_at from broker_call").fetchone()[0]
    assert brokers.match_call(conn, row["broker_call_id"], "grow")
    assert brokers.unmatched(conn, 30) == []
    company, created = conn.execute("select company_id, created_at from broker_call"
                                    ).fetchone()
    assert company == 1 and created > before       # past runs never saw it as GROW's
    assert [p.symbol for p in verdicts.pending(conn, 30)] == ["GROW"]
    with pytest.raises(service.NotFound):
        brokers.match_call(conn, row["broker_call_id"], "GROW")     # no longer unmatched
    # The same call already recorded for the company: the unmatched copy goes.
    brokers.add_call(conn, company_id=None, stock_name="Grow Inds", broker="Nomura",
                     stance="buy", rating="Buy", kind="research", target_price=900.0,
                     called_on=TODAY - dt.timedelta(days=2), source="news")
    dup = brokers.unmatched(conn, 30)[0]["broker_call_id"]
    assert not brokers.match_call(conn, dup, "GROW")
    assert conn.execute("select count(*) from broker_call").fetchone()[0] == 1


@pytest.mark.db
def test_calls_waiting_for_a_verdict_are_never_the_ones_left_out(scored):
    conn, run_id = scored
    for n in range(ai.MAX_BROKER_CALLS + 2):
        brokers.add_manual(conn, "BANK", f"Broker {n:02d}", "buy", 12000.0 + n,
                           TODAY - dt.timedelta(days=n))
    oldest = _ids(conn, "BANK")[-1]
    as_of = dt.datetime.now(IST)
    shown = ai.broker_calls(conn, 3, as_of)
    assert len(shown) == ai.MAX_BROKER_CALLS and oldest not in [c["id"] for c in shown]
    first = ai.broker_calls(conn, 3, as_of, frozenset({oldest}))
    assert first[0]["id"] == oldest and len(first) == ai.MAX_BROKER_CALLS


@pytest.mark.db
def test_the_ai_calls_page_lists_calls_waiting_and_links_unmatched_ones(scored, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest
    conn, _ = scored
    brokers.add_manual(conn, "GAPS", "Axis Securities", "buy", 410.0, TODAY)
    brokers.add_call(conn, company_id=None, stock_name="Cyclical Stl", broker="Nomura",
                     stance="sell", rating="Reduce", kind="research", target_price=90.0,
                     called_on=TODAY, source="news")
    conn.commit()
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "AI calls"
    at.run()
    assert not at.exception, at.exception
    assert any(s.value == "Brokers' calls waiting for the AI's verdict" for s in at.subheader)
    waiting = next(d.value for d in at.dataframe
                   if {"broker", "on watchlist"} <= set(d.value.columns))
    assert waiting[["stock", "broker", "why"]].values.tolist() == [
        ["GAPS", "Axis Securities", "no verdict yet"]]
    wanted = next(d.value for d in at.dataframe if "Screener.in" in d.value.columns)
    assert wanted["symbol"].to_list() == ["GAPS"]
    at.selectbox(key="match_sym").select("CYCL").run()
    next(b for b in at.button if b.label == "Link").click().run()
    assert not at.exception, at.exception
    assert any(s.value == "Linked to CYCL." for s in at.success)
    assert brokers.unmatched(conn, 30) == []
    waiting = next(d.value for d in at.dataframe
                   if {"broker", "on watchlist"} <= set(d.value.columns))
    assert sorted(waiting["stock"].to_list()) == ["CYCL", "GAPS"]


@pytest.mark.db
def test_each_brokers_call_is_its_own_line(scored):
    """Two brokers' calls on the same stock, the same day, with the same rating and target
    are two calls, each waiting for its verdict on its own line."""
    conn, _ = scored
    for firm in ("Motilal Oswal", "ICICI Securities"):
        assert brokers.add_manual(conn, "BANK", firm, "buy", 12500.0, TODAY)
    rows = verdicts.waiting(conn, 30)
    assert sorted(r["broker"] for r in rows) == ["ICICI Securities", "Motilal Oswal"]
    assert {r["symbol"] for r in rows} == {"BANK"} and {r["why"] for r in rows} == {"new"}
    [stock] = verdicts.pending(conn, 30)
    assert stock.reason == "2 brokers' calls without the AI's verdict"


@pytest.mark.db
def test_verdicts_are_refreshed_every_week(scored):
    """A verdict older than refresh_days makes its call wait again, after the stocks whose
    calls have none; the NSE check's reviews (no refresh) take only the new ones."""
    conn, run_id = scored
    brokers.add_manual(conn, "BANK", "Jefferies", "buy", 12500.0, TODAY)
    verdicts.review(assistant(conn, answer(_ids(conn, "BANK"))), "BANK", run_id)
    assert verdicts.pending(conn, 30, refresh_days=7) == []
    conn.execute("update ai_broker_verdict set given_at = now() - interval '8 days'")
    conn.commit()
    brokers.add_manual(conn, "GAPS", "Axis Securities", "buy", 410.0, TODAY)
    due = verdicts.pending(conn, 30, refresh_days=7)
    assert [(p.symbol, p.reason, p.priority) for p in due] == [
        ("GAPS", "1 broker's call without the AI's verdict", 0),
        ("BANK", "1 verdict more than 7 days old", 2)]
    assert [p.symbol for p in verdicts.pending(conn, 30)] == ["GAPS"]
    assert [r["why"] for r in verdicts.waiting(conn, 30, 7)] == ["new", "refresh"]

    every = _ids(conn, "BANK") + _ids(conn, "GAPS")
    client = FakeClient(*(answer(every).script * 2))
    first = verdicts.scheduled(assistant(conn, client), run_id, refresh=False)
    assert [r["symbol"] for r in first.made] == ["GAPS"]          # as the NSE check does
    daily = verdicts.scheduled(assistant(conn, client), run_id)
    assert [r["symbol"] for r in daily.made] == ["BANK"]          # the weekly refresh
    assert verdicts.pending(conn, 30, refresh_days=7) == []


@pytest.mark.db
def test_the_nse_check_reviews_the_calls_it_collected(scored, monkeypatch):
    """brokers.step: after collecting and reading, the AI's verdict on the new calls."""
    from igs.assistant import brokers as reader
    from igs.assistant import news_tone
    from igs.assistant.llm import Assistant as RealAssistant
    conn, _ = scored

    def collect(conn_, *a, **k):
        brokers.add_manual(conn_, "GAPS", "Axis Securities", "buy", 410.0, TODAY)
        return brokers.Collection()
    monkeypatch.setattr(brokers, "collect", collect)
    monkeypatch.setattr(reader, "read_new", lambda a: "read 0 articles")
    monkeypatch.setattr(news_tone, "read_new", lambda a: "0 tones")
    monkeypatch.setattr("igs.config.load_assistant", lambda: _cfg())

    def open_(conn_, cfg=None, client_=None):
        ids = _ids(conn_, "GAPS")
        return RealAssistant(conn_, _cfg(), answer(ids))
    monkeypatch.setattr(RealAssistant, "open", staticmethod(open_))
    text_ = brokers.step(conn)
    # The manual addition in the fake collector now reviews immediately; step
    # must not review it a second time.
    assert "none waiting" in text_
    assert conn.execute("select count(*) from ai_broker_review").fetchone()[0] == 1
    assert verdicts.pending(conn, 30) == []
