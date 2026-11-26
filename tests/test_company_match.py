"""The AI matching brokers' calls whose stock wasn't recognised to a listed company,
offline: a scripted client stands in for the AI. Every link must be to a company it was
shown from the app's list, a link must count only from when it was made, and the owner
must be able to undo one."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import psycopg
import pytest
from test_ai_calls import scored  # noqa: F401  (fixture)
from test_assistant import FakeClient, _cfg, msg, text

from igs import brokers, service
from igs.assistant import company_match as cm
from igs.assistant.llm import Assistant
from igs.timeutil import IST

APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"
TODAY = dt.datetime.now(IST).date()


def config(**match):
    """The assistant on, with automatic verdict reviews off (no request beyond the
    script) and these company_match settings."""
    cfg = _cfg()
    f = cfg.features
    return cfg.model_copy(update={"features": f.model_copy(update={
        "verdicts": f.verdicts.model_copy(update={"scheduled": False}),
        "company_match": f.company_match.model_copy(update=match)})})


def unmatched(conn, name: str, broker: str = "Nomura", target: float | None = None) -> int:
    assert brokers.add_call(conn, company_id=None, stock_name=name, broker=broker,
                            stance="buy", rating="Buy", kind="research", target_price=target,
                            called_on=TODAY - dt.timedelta(days=1), source="news",
                            quote=f"{broker} keeps Buy on {name}")
    conn.commit()
    return conn.execute("select broker_call_id from broker_call where stock_name = %s",
                        (name,)).fetchone()[0]


def matches(*rows: tuple) -> object:
    return msg(text(json.dumps({"matches": [
        {"id": i, "symbol": sym, "company_name": name, "other_names": other,
         "confidence": sure, "reason": f"{why}."} for i, sym, name, other, sure, why in rows]})))


def items(request: dict) -> list[dict]:
    prompt = request["messages"][0]["content"]
    return json.loads(prompt.split("<calls>\n")[1].split("\n</calls>")[0])


def close(conn, symbol: str) -> float:
    return next(c for c in cm.candidates(conn, symbol, TODAY)
                if c["symbol"] == symbol)["close"]


@pytest.mark.db
def test_the_candidates_share_a_word_and_carry_the_price(scored):  # noqa: F811
    conn, _ = scored
    shown = cm.candidates(conn, "Example Bnk", TODAY)
    assert [c["symbol"] for c in shown] == ["BANK", "NBFC"]         # both "Example"
    assert all(c["close"] > 0 and c["close_date"] <= TODAY for c in shown)
    assert cm.candidates(conn, "Molbio Diagnostics", TODAY) == []
    # What the AI named, by symbol or by name.
    assert [c["symbol"] for c in cm.candidates(conn, "Zomato", TODAY,
                                               ["GAPS", "Gaps Engineering Limited"])] == ["GAPS"]
    assert [c["symbol"] for c in cm.candidates(conn, "Zomato", TODAY,
                                               ["", "Late Foods"])] == ["LATE"]


@pytest.mark.db
def test_the_ai_links_what_it_is_sure_of_and_suggests_the_rest(scored):  # noqa: F811
    conn, _ = scored
    bank = unmatched(conn, "Example Bnk", target=round(close(conn, "BANK") * 1.2, 2))
    either = unmatched(conn, "Example", broker="CLSA")
    abroad = unmatched(conn, "Molbio Diagnostics", broker="Jefferies")
    renamed = unmatched(conn, "Zomato", broker="Goldman Sachs")
    apple = unmatched(conn, "Apple", broker="Morgan Stanley")
    before = conn.execute("select created_at from broker_call where broker_call_id = %s",
                          (bank,)).fetchone()[0]
    client = FakeClient(
        matches((bank, "BANK", "Example Bank Ltd", [], 0.95, "The name and the price fit"),
                (either, "NBFC", "Example Finance Ltd", [], 0.6, "Bank or Finance"),
                (abroad, "", "", [], 0.9, "Not listed on the NSE"),
                (renamed, "GAPS", "Gaps Engineering Ltd", ["Zomato"], 0.9, "Renamed"),
                (apple, "AAPL", "Apple Inc", [], 0.99, "Listed in the US")),
        matches((renamed, "GAPS", "Gaps Engineering Ltd", ["Zomato"], 0.93, "Renamed")))
    got = cm.match_new(Assistant.open(conn, config(), client))
    assert (got.read, got.linked, got.suggested, got.no_match) == (
        5, ["Zomato -> GAPS", "Example Bnk -> BANK"], 1, 2)

    # 1. Every call, with the companies it could be and their prices.
    first, second = client.requests
    assert first["system"][0]["text"] == cm.SYSTEM
    shown = {i["id"]: i for i in items(first)}
    assert set(shown) == {bank, either, abroad, renamed, apple}
    assert shown[bank]["stock"] == "Example Bnk" and shown[bank]["target_price"] > 0
    assert shown[bank]["quote"] == "Nomura keeps Buy on Example Bnk"
    assert [c["symbol"] for c in shown[bank]["candidates"]] == ["BANK", "NBFC"]
    assert shown[bank]["candidates"][0]["close"] == close(conn, "BANK")
    assert shown[renamed]["candidates"] == [] and shown[apple]["candidates"] == []
    # 2. A company named outside them: asked again with the app's record of it; one the
    # app doesn't have is no match.
    assert second["messages"][0]["content"].startswith(cm.AGAIN)
    [again] = items(second)
    assert again["id"] == renamed and again["you_named"]["symbol"] == "GAPS"
    assert [c["symbol"] for c in again["candidates"]] == ["GAPS"]

    # Linked calls are recorded again under the company, so they count from now.
    company, created = conn.execute(
        "select company_id, created_at from broker_call where stock_name = 'Example Bnk'"
    ).fetchone()
    assert company == 3 and created > before
    assert conn.execute("select company_id from broker_call where stock_name = 'Zomato'"
                        ).fetchone()[0] == 5
    assert sorted(r["stock_name"] for r in brokers.unmatched(conn, 30)) == [
        "Apple", "Example", "Molbio Diagnostics"]
    rows = {r[0]: r[1:] for r in conn.execute(
        """select stock_name, symbol, company_id, outcome, rounds, linked_call_id is not null
           from broker_call_match""").fetchall()}
    assert rows == {"Example Bnk": ("BANK", 3, "linked", 1, True),
                    "Example": ("NBFC", 4, "suggested", 1, False),
                    "Molbio Diagnostics": ("", None, "no match", 1, False),
                    "Zomato": ("GAPS", 5, "linked", 2, True),
                    "Apple": ("AAPL", None, "no match", 1, False)}
    said = cm.latest(conn, [r["dedupe_key"] for r in brokers.unmatched(conn, 30)])
    assert "AAPL (Apple Inc) isn't in the app's list" in next(
        a["reason"] for a in said.values() if a["symbol"] == "AAPL")
    assert conn.execute("select count(*) from llm_call where feature = 'company_match'"
                        ).fetchone()[0] == 2
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("update broker_call_match set outcome = 'linked'")
    conn.rollback()

    # Each call is read once (the client has no answers left).
    assert cm.match_new(Assistant.open(conn, config(), FakeClient())).read == 0
    assert [c["stock_name"] for c in cm.waiting(conn, 30, retry=True)] == [
        "Apple", "Molbio Diagnostics", "Example"]


@pytest.mark.db
def test_below_the_bar_or_with_auto_link_off_it_only_suggests(scored):  # noqa: F811
    conn, _ = scored
    bank = unmatched(conn, "Example Bnk")
    answer = (bank, "BANK", "Example Bank Ltd", [], 0.95, "The name fits")
    got = cm.match_new(Assistant.open(conn, config(auto_link=False),
                                      FakeClient(matches(answer))))
    assert (got.linked, got.suggested) == ([], 1)
    got = cm.match_new(Assistant.open(conn, config(auto_link_confidence=0.96),
                                      FakeClient(matches(answer))), retry=True)
    assert (got.linked, got.suggested) == ([], 1)
    assert [r["stock_name"] for r in brokers.unmatched(conn, 30)] == ["Example Bnk"]
    # The owner takes the suggestion.
    assert brokers.match_call(conn, bank, "BANK")
    assert brokers.unmatched(conn, 30) == []


@pytest.mark.db
def test_the_owner_undoes_a_wrong_link(scored, monkeypatch):  # noqa: F811
    conn, _ = scored
    wrong = unmatched(conn, "Example Fin")
    cm.match_new(Assistant.open(conn, config(), FakeClient(
        matches((wrong, "BANK", "Example Bank Ltd", [], 0.92, "The name fits")))))
    [link] = cm.linked(conn, 30)
    assert (link["stock_name"], link["symbol"], link["company"]) == (
        "Example Fin", "BANK", "Example Bank Ltd")
    brokers.add_manual(conn, "NBFC", "Kotak", "buy", 500.0, TODAY)
    mine = conn.execute("select broker_call_id from broker_call where broker = 'Kotak'"
                        ).fetchone()[0]
    with pytest.raises(service.NotFound):
        cm.unlink(conn, mine)                      # only the AI's links

    # Its verdict under the wrong company goes with the link.
    review = conn.execute("""insert into ai_broker_review (company_id, symbol, run_id,
            in_run, data_gaps, inputs, model, prompt_version, trigger, cost_usd, reason)
        values (3, 'BANK', 1, true, '[]', '{}', 'm', 'v', 'manual', 0, 'r')
        returning review_id""").fetchone()[0]
    conn.execute("""insert into ai_broker_verdict (review_id, broker_call_id, verdict, reason)
                    values (%s, %s, 'agree', 'r')""", (review, link["broker_call_id"]))
    conn.commit()
    cm.unlink(conn, link["broker_call_id"])
    [back] = brokers.unmatched(conn, 30)
    assert back["stock_name"] == "Example Fin"
    assert back["dedupe_key"] == brokers.dedupe_key(None, "Example Fin", "Nomura",
                                                    TODAY - dt.timedelta(days=1), "buy", None)
    assert cm.linked(conn, 30) == []
    assert conn.execute("select count(*) from ai_broker_verdict").fetchone()[0] == 0
    with pytest.raises(service.NotFound):
        cm.unlink(conn, link["broker_call_id"])
    assert cm.waiting(conn, 30, retry=True) == []          # the AI won't link it again

    # The page says so, and the owner links it.
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    import streamlit
    from streamlit.testing.v1 import AppTest
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "AI calls"
    at.run()
    assert not at.exception, at.exception
    assert any("The AI linked it to BANK; you undid the link." in c.value for c in at.caption)
    at.selectbox(key="match_sym").select("NBFC").run()
    next(b for b in at.button if b.label == "Link").click().run()
    assert any(s.value == "Linked to NBFC." for s in at.success)


@pytest.mark.db
def test_the_page_shows_the_ais_suggestion_and_its_links(scored, monkeypatch):  # noqa: F811
    conn, _ = scored
    sure = unmatched(conn, "Example Bnk")
    unsure = unmatched(conn, "Example", broker="CLSA")
    cm.match_new(Assistant.open(conn, config(), FakeClient(matches(
        (sure, "BANK", "Example Bank Ltd", [], 0.95, "The name fits"),
        (unsure, "NBFC", "Example Finance Ltd", [], 0.55, "Bank or Finance")))))
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    import streamlit
    from streamlit.testing.v1 import AppTest
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "AI calls"
    at.run()
    assert not at.exception, at.exception
    assert any(e.label == "Calls the AI linked to a company (1)" for e in at.expander)
    links = next(d.value for d in at.dataframe if "linked to" in d.value.columns)
    assert links[["as written", "linked to"]].values.tolist() == [["Example Bnk", "BANK"]]
    assert any("The AI suggests **NBFC** (Example Finance Ltd), 55% sure" in m.value
               for m in at.markdown)
    next(b for b in at.button if b.label == "Link to NBFC").click().run()
    assert not at.exception, at.exception
    assert any(s.value == "Linked to NBFC." for s in at.success)
    assert brokers.unmatched(conn, 30) == []
    # Undo the AI's link from the page.
    next(b for b in at.button if b.label == "Undo this link").click().run()
    assert any("Example Bnk is back among the calls not matched" in s.value
               for s in at.success)
    assert [r["stock_name"] for r in brokers.unmatched(conn, 30)] == ["Example Bnk"]


@pytest.mark.db
def test_the_command_line_lists_the_ais_answers(scored, monkeypatch, capsys):  # noqa: F811
    from igs import cli
    conn, _ = scored
    call = unmatched(conn, "Example", broker="CLSA")
    cm.match_new(Assistant.open(conn, config(), FakeClient(matches(
        (call, "NBFC", "Example Finance Ltd", [], 0.55, "Bank or Finance")))))
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    assert cli._brokers_match(cli.build_parser().parse_args(["brokers", "match"])) == 0
    out = capsys.readouterr().out
    assert "'Example' - CLSA: Buy" in out and "AI suggests NBFC (55%): Bank or Finance." in out
