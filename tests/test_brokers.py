"""Brokers' buy / hold / sell calls: read by the AI from news or entered by the owner, a
second opinion for the AI's own calls, never used in the ranking. No request leaves the
machine: feeds come from a mock transport and a scripted assistant stands in for the AI."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import test_ai_calls
from test_ai_calls import reply

from igs import brokers
from igs.assistant import brokers as reader
from igs.config import NewsFeed, load_broker_calls
from igs.timeutil import IST

scored = test_ai_calls.scored
REAL = Path(__file__).resolve().parent / "fixtures" / "real" / "et_stocks_rss_2026-09-30.xml"
APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"


def test_names_are_compared_without_ltd_and_punctuation():
    assert brokers.normalise("Dr. Reddy's Laboratories Ltd") == "dr reddy s laboratories"
    assert brokers.normalise("Mahindra & Mahindra Limited") == brokers.normalise(
        "Mahindra and Mahindra")
    day = dt.date(2026, 9, 30)
    assert brokers.dedupe_key(None, "Molbio Diagnostics Ltd", "JEFFERIES", day, "buy",
                              1600.0) == \
        brokers.dedupe_key(None, "Molbio Diagnostics", "Jefferies", day, "buy", 1600)


@pytest.mark.db
def test_stocks_are_matched_only_when_unambiguous(scored):
    conn, _ = scored
    assert brokers.match_company(conn, "Example Bank") == 3
    assert brokers.match_company(conn, "Grow Industries Limited") == 1
    assert brokers.match_company(conn, "anything", "NBFC") == 4           # symbol written
    assert brokers.match_company(conn, "Example") is None                # two companies
    assert brokers.match_company(conn, "Molbio Diagnostics") is None     # not listed here


def _fake_assistant(conn, answers):
    """Answers each batch with the calls `answers(batch titles)` returns."""
    seen = []

    def structured(feature, *, system, prompt, schema):
        items = json.loads(prompt.split("\n", 1)[1].rsplit("\n", 1)[0])
        seen.append([i["title"] for i in items])
        return {"calls": answers(items)}, SimpleNamespace(model="test-model")
    cfg = SimpleNamespace(features=SimpleNamespace(brokers=SimpleNamespace(
        batch_size=15, max_per_run=150)))
    return SimpleNamespace(conn=conn, cfg=cfg, structured=structured,
                           cost=lambda m: 0.001), seen


def _call(article, company, broker, stance, target=0.0, **over):
    return {"article": article, "broker": broker, "company": company, "nse_symbol": "",
            "rating": stance.capitalize(), "stance": stance, "kind": "research",
            "target_price": target, "report_date": "", "quote": f"{broker} on {company}.",
            **over}


@pytest.mark.db
def test_calls_are_read_from_real_news_and_kept_once(scored, monkeypatch):
    """From today's real Economic Times feed: the AI reads only the articles that mention
    a rating, target or brokerage (the order win is never sent), skips the block deal, and
    each call is stored once however often it is read."""
    conn, _ = scored
    cfg = load_broker_calls().model_copy(update={"feeds": [
        NewsFeed(name="ET stocks", url="https://economictimes.indiatimes.com/x.cms")]})
    now = dt.datetime(2026, 9, 30, 13, 0, tzinfo=IST)
    monkeypatch.setattr("igs.news.utc_now", lambda: now)
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=REAL.read_bytes()))) as client:
        got = brokers.collect(conn, cfg, client=client)
    assert (got.articles, got.candidates, got.errors) == (4, 3, [])

    def answers(items):
        out = []
        for i, item in enumerate(items):
            if "Jefferies" in item["title"]:
                out.append(_call(i, "Molbio Diagnostics", "Jefferies", "buy", 1600))
                out.append(_call(i, "Example Bank", "Jefferies", "buy", 12000))   # matched
            if "Lenskart" in item["title"]:
                out.append(_call(i, "Lenskart Solutions", "Morgan Stanley", "buy", 718,
                                 rating="Overweight", report_date="2026-09-29"))
        out.append(_call(99, "Nowhere", "Nobody", "sell"))                  # no such article
        return out
    monkeypatch.setattr(reader, "load_broker_calls", lambda: cfg.model_copy(
        update={"read_within_days": 30000}))
    assistant, seen = _fake_assistant(conn, answers)
    result = reader.read_new(assistant)
    assert len(seen) == 1 and len(seen[0]) == 3
    assert not any("KSB" in t for t in seen[0])                          # never sent
    assert (result.read, result.stored, result.unmatched) == (3, 3, 2)
    assert "matches no article" in result.issues[0]
    rows = conn.execute("""select stock_name, broker, stance, rating, target_price::float8,
                                  called_on, company_id, source, url is not null
                           from broker_call order by broker, stock_name""").fetchall()
    assert rows == [
        ("Example Bank", "Jefferies", "buy", "Buy", 12000.0, dt.date(2026, 9, 30), 3, "news",
         True),
        ("Molbio Diagnostics", "Jefferies", "buy", "Buy", 1600.0, dt.date(2026, 9, 30), None,
         "news", True),
        ("Lenskart Solutions", "Morgan Stanley", "buy", "Overweight", 718.0,
         dt.date(2026, 9, 29), None, "news", True)]
    assert reader.read_new(assistant).read == 0                          # read once
    conn.execute("update broker_article set read_at = null")
    reader.read_new(assistant)
    assert conn.execute("select count(*) from broker_call").fetchone()[0] == 3


@pytest.mark.db
def test_the_owner_adds_calls_and_the_ai_weighs_them(scored, monkeypatch):
    """A Moneycontrol call typed in by the owner is shown with its upside, makes the stock
    due for a fresh AI call, and is part of what the AI reads, which says how its own call
    compares."""
    from test_assistant import _cfg

    from igs.assistant import calls as ai
    from igs.assistant.llm import Assistant
    conn, run_id = scored
    day = dt.date(2024, 11, 28)
    assert brokers.add_manual(conn, "cycl", "Motilal Oswal", "sell", 150.0, day,
                              rating="Reduce", url="https://www.moneycontrol.com/x")
    assert not brokers.add_manual(conn, "CYCL", "motilal oswal", "sell", 150.0, day)
    with pytest.raises(ValueError, match="future"):
        brokers.add_manual(conn, "CYCL", "X", "buy", None, dt.date(2099, 1, 1))
    rows = brokers.calls_for(conn, 2, day - dt.timedelta(days=5), dt.date(2024, 11, 29))
    assert len(rows) == 1 and rows[0]["rating"] == "Reduce"
    assert rows[0]["upside"] == pytest.approx(150.0 / rows[0]["last_close"] - 1)

    due = {d.symbol: d for d in ai.due_for_call(conn, run_id, 0, 7, broker_days=7)}
    assert due["CYCL"].reason == "new broker calls: Motilal Oswal sell, target Rs 150"
    assert "CYCL" not in {d.symbol for d in ai.due_for_call(conn, run_id, 0, 7)}

    # Asked for by the owner: the AI gives its verdict on each broker's call. A verdict on
    # a call it was not shown, or a second one on the same call, is dropped.
    manual = conn.execute("select broker_call_id from broker_call").fetchone()[0]
    verdict = {"id": manual, "verdict": "agree",
               "reason": "Operating margin fell for three quarters, as the broker expects."}
    client = reply(action="sell", vs_brokers="Agrees with Motilal Oswal's Reduce: margins "
                                             "are falling.",
                   broker_verdicts=[verdict, {**verdict, "verdict": "disagree"},
                                    {**verdict, "id": manual + 999}])
    call = ai.make_call(Assistant.open(conn, _cfg(), client), "CYCL", run_id)
    sent = client.requests[0]["messages"][0]["content"]
    assert "<broker_calls>" in sent and "Motilal Oswal" in sent and '"Reduce"' in sent
    assert f'"id":{manual},' in sent and "broker_verdicts" in str(client.requests[0]["system"])
    assert call["vs_brokers"].startswith("Agrees with Motilal")
    assert call["broker_verdicts"] == [{"id": manual, "broker": "Motilal Oswal",
                                        "rating": "Reduce", "called_on": day,
                                        "verdict": "agree", "reason": verdict["reason"]}]
    stored = conn.execute("select vs_brokers, prompt_version from ai_call "
                          "where call_id = %s", (call["call_id"],)).fetchone()
    assert stored == (call["vs_brokers"], "call-v5")
    assert [(v["broker"], v["verdict"]) for v in ai.verdicts_for(conn, call["call_id"])] == [
        ("Motilal Oswal", "agree")]
    row = brokers.calls_for(conn, 2, day - dt.timedelta(days=5), dt.date(2024, 11, 29))[0]
    assert (row["ai_verdict"], row["ai_reason"]) == ("agree", verdict["reason"])
    assert brokers.recent(conn, 100000)[0]["ai_verdict"] == "agree"
    import streamlit
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "Stock"
    at.session_state["stock_sym"] = "CYCL"
    at.run()
    assert not at.exception, at.exception
    assert any(m.value.startswith("**Its verdict on each broker's call**\n- Motilal Oswal, "
                                  "Reduce (28 Nov): **agree**.") for m in at.markdown)
    brokers.delete_manual(conn, manual)       # the verdict goes with the call it was on
    assert conn.execute("select count(*) from broker_call").fetchone()[0] == 0
    assert conn.execute("select count(*) from ai_broker_verdict").fetchone()[0] == 0


def test_the_ai_answer_schema_has_the_verdicts_written_out():
    from igs.assistant import calls as ai
    schema = ai.output_schema()
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema
    item = schema["properties"]["broker_verdicts"]["items"]
    assert item["properties"]["verdict"]["enum"] == list(ai.VERDICTS)
    assert set(item["required"]) == {"id", "verdict", "reason"}
    assert item["additionalProperties"] is False
    assert "broker_verdicts" in schema["required"]


@pytest.mark.db
def test_the_stock_page_shows_and_takes_broker_calls(scored, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest
    conn, _ = scored
    brokers.add_manual(conn, "BANK", "Kotak Institutional Equities", "buy", 12500.0,
                       dt.datetime.now(IST).date(), rating="Add")
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "Stock"
    at.session_state["stock_sym"] = "BANK"
    at.run()
    assert not at.exception, at.exception
    assert any(s.value == "Brokers' calls" for s in at.subheader)
    table = next(d.value for d in at.dataframe if "broker" in d.value.columns)
    assert table["broker"].tolist() == ["Kotak Institutional Equities"]
    assert table["call"].tolist() == ["Buy (Add)"] and table["from"].tolist() == ["you"]
    assert table["AI's verdict"].tolist() == ["not reviewed yet"]
    at.text_input(key="bc_broker").input("Jefferies")
    at.selectbox(key="bc_stance").select("hold")
    at.number_input(key="bc_target").set_value(11000.0)
    next(b for b in at.button if b.label == "Add call").click().run()
    assert not at.exception, at.exception
    assert any(s.value == "Added." for s in at.success)
    table = next(d.value for d in at.dataframe if "broker" in d.value.columns)
    assert sorted(table["broker"]) == ["Jefferies", "Kotak Institutional Equities"]
    at.sidebar.radio(key="page").set_value("AI calls").run()
    assert not at.exception, at.exception
    recent = next(d.value for d in at.dataframe if "AI's latest call" in d.value.columns)
    assert set(recent["stock"]) == {"BANK"} and set(recent["AI's latest call"]) == {"none yet"}


# --------------------------------------------------------------------------- pasted pages

MC = Path(__file__).resolve().parent / "fixtures" / "real" / "moneycontrol_recos_2024-04-23.txt"


def test_moneycontrol_headlines_are_read_from_pasted_text():
    """Calls older than Moneycontrol's news sitemap are pasted from its pages. Its
    headlines, as its last RSS feed carried them, become calls."""
    calls = brokers.parse_pasted(MC.read_text(encoding="utf-8"), dt.date(2024, 4, 23),
                                 today=dt.date(2024, 4, 30))
    assert [(c["rating"], c["stock_name"], c["target_price"], c["broker"], c["called_on"])
            for c in calls] == [
        ("Buy", "HDFC Bank", 1850.0, "ICICI Securities", dt.date(2024, 4, 23)),
        ("Buy", "Tejas Networks", 1100.0, "Emkay Global Financial", dt.date(2024, 4, 23)),
        ("Buy", "Bajaj Finance", 9000.0, "Emkay Global Financial", dt.date(2024, 4, 23)),
        ("Reduce", "Persistent Systems", 3700.0, "Emkay Global Financial",
         dt.date(2024, 4, 23)),
        ("Reduce", "Aditya Birla Fashion and Retail", 230.0, "Emkay Global Financial",
         dt.date(2024, 4, 23))]                       # the feed listed Bajaj Finance twice
    assert [c["stance"] for c in calls] == ["buy", "buy", "buy", "sell", "sell"]
    # A page saved from the browser works too; a date too old to be real is not used.
    page = ("<html><script>var x = 'Buy Fake; target of Rs 1: Nobody';</script><li><h2>"
            "<a href='#'>Accumulate Infosys; target of Rs 1,531.50: KR Choksey</a></h2>"
            "<span>September 29, 2026 12:05 PM IST</span></li><li><h2>Neutral HDFC Life "
            "Insurance Company; target of ₹ 739: Motilal Oswal</h2><span>March 3, 2019</span>"
            "</li></html>")
    got = brokers.parse_pasted(page, dt.date(2026, 9, 30), today=dt.date(2026, 9, 30))
    assert [(c["stock_name"], c["stance"], c["target_price"], c["called_on"]) for c in got] \
        == [("Infosys", "buy", 1531.5, dt.date(2026, 9, 29)),
            ("HDFC Life Insurance Company", "hold", 739.0, dt.date(2026, 9, 30))]
    assert brokers.parse_pasted("nothing to see here", dt.date(2026, 9, 30)) == []


@pytest.mark.db
def test_a_pasted_page_is_stored_once(scored):
    conn, _ = scored
    text = ("Buy Example Bank; target of Rs 12,500: ICICI Securities\n"
            "September 29, 2026 01:41 PM IST\n"
            "Sell Molbio Diagnostics; target of Rs 900: Nobody Securities\n")
    got = brokers.import_pasted(conn, text, dt.date(2026, 9, 30))
    assert (got.found, got.added, got.unmatched) == (2, 2, ["Molbio Diagnostics"])
    assert str(got).startswith("Found 2 calls: 2 added")
    again = brokers.import_pasted(conn, text, dt.date(2026, 9, 30))
    assert (again.found, again.added) == (2, 0)
    rows = conn.execute("""select stock_name, company_id, source, url from broker_call
                           order by stock_name""").fetchall()
    assert rows == [("Example Bank", 3, "pasted", brokers.MONEYCONTROL_URL),
                    ("Molbio Diagnostics", None, "pasted", brokers.MONEYCONTROL_URL)]
    assert "Copy the whole page" in str(brokers.import_pasted(conn, "hello", dt.date(2026, 9, 30)))


@pytest.mark.db
def test_the_ai_calls_page_imports_a_pasted_page(scored, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "AI calls"
    at.run()
    today = dt.datetime.now(IST).date()
    at.text_area(key="mc_paste").input(
        f"Accumulate Example Finance; target of Rs 2,900: KR Choksey\n{today:%B %d, %Y}\n")
    at.button(key="mc_import").click().run()
    assert not at.exception, at.exception
    assert any("Found 1 calls: 1 added" in s.value for s in at.success)
    table = next(d.value for d in at.dataframe if "AI's latest call" in d.value.columns)
    assert table["stock"].tolist() == ["NBFC"]
    assert table["from"].tolist() == ["Moneycontrol (pasted)"]
    assert at.text_area(key="mc_paste").value == ""


# --------------------------------------------------------------------------- Moneycontrol

MC_SITEMAP = (Path(__file__).resolve().parent / "fixtures" / "real"
              / "moneycontrol_news_sitemap_2026-09-30.xml")


def test_a_headline_that_states_a_whole_call_is_read_without_the_ai():
    assert brokers.headline_calls("Buy Shriram Finance; target of Rs 1220: Motilal Oswal") == [{
        "rating": "Buy", "stance": "buy", "stock_name": "Shriram Finance",
        "target_price": 1220.0, "broker": "Motilal Oswal",
        "quote": "Buy Shriram Finance; target of Rs 1220: Motilal Oswal"}]
    assert brokers.headline_calls(
        "Nomura initiates Allied Blenders with 'Buy', Rs 850 target; sees strong growth "
        "ahead") == []                                     # the AI reads this one
    assert brokers.headline_calls("Buy Nothing; target of Rs 0: Nobody") == []
    # Each broker a headline names is a call of its own.
    two = brokers.headline_calls("Buy HDFC Bank; target of Rs 1,950: Motilal Oswal, "
                                 "ICICI Securities")
    assert [(c["broker"], c["target_price"]) for c in two] == [
        ("Motilal Oswal", 1950.0), ("ICICI Securities", 1950.0)]


@pytest.mark.db
def test_moneycontrol_calls_are_collected_from_its_news_sitemap(scored, monkeypatch):
    """From Moneycontrol's real news sitemap: only its stock and market news is kept; the
    three headlines that state a whole call are recorded without the AI, which gets only
    the three other articles about ratings. Nothing is stored twice, not even when the same
    call is pasted from its stock news page with the report's date, a day earlier."""
    conn, _ = scored
    feed = NewsFeed(name="Moneycontrol",
                    url="https://www.moneycontrol.com/news/news-sitemap.xml",
                    sections=["/news/business/stocks/", "/news/business/markets/"])
    cfg = load_broker_calls().model_copy(update={"feeds": [feed]})
    monkeypatch.setattr("igs.news.utc_now",
                        lambda: dt.datetime(2026, 9, 30, 17, 0, tzinfo=IST))
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=MC_SITEMAP.read_bytes()))) as client:
        got = brokers.collect(conn, cfg, client=client)
        again = brokers.collect(conn, cfg, force=True, client=client)
    assert (got.articles, got.headline_calls, got.candidates, got.errors) == (8, 3, 3, [])
    assert str(got).startswith("8 new articles, 3 calls read from headlines, 3 more")
    assert (again.articles, again.headline_calls) == (0, 0)
    stored = conn.execute("select payload from geopolitical_feed_fetch "
                          "order by fetch_id limit 1").fetchone()[0]
    assert b"buy-shriram-finance" in stored and b"trump-denies" not in stored
    rows = conn.execute("""select stock_name, broker, stance, rating, target_price::float8,
                                  called_on, source, company_id, model, url, quote
                           from broker_call order by stock_name""").fetchall()
    assert [r[:9] for r in rows] == [
        ("Firstsource Solutions", "Emkay Global Financial", "sell", "Reduce", 270.0,
         dt.date(2026, 9, 30), "news", None, None),
        ("ICICI Lombard", "Motilal Oswal", "hold", "Neutral", 1700.0, dt.date(2026, 9, 30),
         "news", None, None),
        ("Shriram Finance", "Motilal Oswal", "buy", "Buy", 1220.0, dt.date(2026, 9, 30),
         "news", None, None)]
    assert rows[2][9].endswith("/buy-shriram-finance-target-of-rs-1220-motilal-oswal-"
                               "14041815.html")
    assert rows[2][10] == "Buy Shriram Finance; target of Rs 1220: Motilal Oswal"
    assert {a["title"][:30] for a in reader.pending(conn, 30000, 50)} == {
        "Nomura initiates Allied Blende", "Coforge shares gain as JPMorga",
        "FIIs net sell Rs 5,353 crore, "}
    # The same call on Moneycontrol's stock news page, dated by the report (29 Sep).
    page = ("Buy Shriram Finance; target of Rs 1220: Motilal Oswal\nMotilal Oswal is bullish "
            "on Shriram Finance recommended buy rating on the stock with a target price of "
            "Rs 1220 in its research report dated September 29, 2026.")
    pasted = brokers.import_pasted(conn, page, dt.date(2026, 9, 30))
    assert (pasted.found, pasted.added) == (1, 0)
    # Five days apart it is a new call.
    assert brokers.add_call(conn, company_id=None, stock_name="Shriram Finance",
                            broker="Motilal Oswal", stance="buy", rating="Buy",
                            kind="research", target_price=1220.0,
                            called_on=dt.date(2026, 9, 25), source="manual")


@pytest.mark.db
def test_an_article_on_several_brokers_gives_a_call_for_each(scored):
    """One article, two brokerages on the same stock with the same rating and target: two
    calls, one line each, and the AI is told to list every firm's call."""
    conn, _ = scored
    conn.execute("""insert into broker_article (url, title, body, published_at, feed_name,
                        candidate)
                    values ('https://example.invalid/a', 'Jefferies, CLSA bullish on Example Bank',
                            'Jefferies and CLSA both rate Example Bank buy with a Rs 12,500
                             target.', now(), 'ET stocks', true)""")
    conn.commit()
    assistant, _ = _fake_assistant(conn, lambda items: [
        _call(0, "Example Bank", firm, "buy", 12500.0) for firm in ("Jefferies", "CLSA")])
    got = reader.read_new(assistant)
    assert got.stored == 2
    rows = brokers.calls_for(conn, 3, dt.datetime.now(IST).date() - dt.timedelta(days=2))
    assert sorted(r["broker"] for r in rows) == ["CLSA", "Jefferies"]
    assert "one entry for each firm" in reader.SYSTEM
