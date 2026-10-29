"""A second opinion on an AI call from another model, offline: scripted clients stand in for
the providers. The other model must get everything the call's model got, in full, make its
own call before it sees the call, and its review must be kept unchanged."""

from __future__ import annotations

import datetime as dt
import json
import os
import re

import httpx
import psycopg
import pytest
from test_ai_calls import APP, _insert, answer, reply, scored  # noqa: F401  (fixture)
from test_assistant import FakeClient, _cfg, msg, text
from test_model_routing import client

from igs.assistant import calls as ai
from igs.assistant import second_opinion as so
from igs.assistant.llm import Assistant
from igs.config import TokenPrice
from igs.timeutil import utc_now


def review(verdict: str = "disagree", **over) -> dict:
    return {"verdict": verdict,
            "summary": "The data shows margins falling for three quarters, which the call "
                       "doesn't weigh; a hold fits it better.",
            "supported": ["Revenue up 18% a year: the quarters show it"],
            "errors": ["ROCE is 19.0% in the factors, not 24.1%"],
            "missed": ["Promoter pledge rose from 2% to 9% in the last quarter"],
            "rule_issues": [], "confidence": 0.7, **over}


def sections(prompt: str) -> dict:
    """The prompt's sections, parsed."""
    return {m[1]: json.loads(m[2]) for m in re.finditer(r"<(\w+)>\n(.*?)\n</\1>", prompt,
                                                        re.DOTALL)}


def openai(monkeypatch, own: dict, rev: dict) -> tuple[httpx.Client, list[dict]]:
    """An OpenAI Responses API that answers the own call, then the review."""
    seen: list[dict] = []

    def handler(req):
        seen.append(json.loads(req.content))
        body = own if len(seen) == 1 else rev
        return httpx.Response(200, json={
            "id": f"resp{len(seen)}", "model": "gpt-test", "status": "completed",
            "output": [{"type": "message", "id": f"m{len(seen)}", "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps(body)}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 200}})
    return client(monkeypatch, "openai", handler), seen


def priced(prices: dict[str, tuple[float, float]]):
    """The assistant's config with these 'provider:model' prices (USD a million tokens)."""
    cfg = _cfg(automatic_prices=False)
    return cfg.model_copy(update={"prices_usd_per_mtok": {
        **cfg.prices_usd_per_mtok, **{k: TokenPrice(input=i, output=o)
                                      for k, (i, o) in prices.items()}}})


def _copy(conn, call_id: int, **sql) -> int:
    """A copy of a stored call made before context was kept: `sql` overrides columns with
    SQL expressions over the original row."""
    cols = [r[0] for r in conn.execute(
        """select column_name from information_schema.columns
           where table_name = 'ai_call' and column_name not in ('call_id', 'context')
           order by ordinal_position""").fetchall()]
    exprs = [sql.get(c, c) for c in cols]
    new = conn.execute(f"""insert into ai_call ({', '.join(cols)})
                           select {', '.join(exprs)} from ai_call where call_id = %s
                           returning call_id""", (call_id,)).fetchone()[0]
    conn.commit()
    return new


@pytest.mark.db
def test_another_ai_gets_everything_the_call_got_then_reviews_it(scored, monkeypatch):  # noqa: F811
    conn, run_id = scored
    original = FakeClient(msg(text(json.dumps(answer()))))
    made = ai.make_call(Assistant.open(conn, _cfg(), original), "GROW")
    base = made["entry_price"] or made["price_close"]
    own = answer("buy", target_price=round(base * 1.3, 2), confidence=0.55,
                 summary="Results keep improving but the stock already trades near its "
                         "52-week high.")
    http, seen = openai(monkeypatch, own, review())
    cfg = priced({"openai:gpt-test": (2, 5)})
    out = so.second_opinion(Assistant.open(conn, cfg, http), made["call_id"],
                            so.route_of("openai:gpt-test"))

    # 1. Its own call: the call's instructions and schema, and every section the call's
    # model read, unchanged; nothing about the call itself.
    minimum = cfg.features.call.min_buy_target_pct_a_year
    assert seen[0]["instructions"] == ai.system(minimum)
    assert seen[0]["text"]["format"]["schema"] == ai.output_schema()
    assert len(seen[0]["input"]) == 1
    first = seen[0]["input"][0]["content"]
    asked = original.requests[0]["messages"][0]["content"]
    assert sections(first) == sections(asked)
    assert {"why_now", "run", "result", "factors", "quarters", "shareholding", "filings",
            "insider_trades_12m", "market", "key_numbers", "growth_profile"} <= set(
        sections(first))
    assert first.splitlines()[0] == asked.splitlines()[0]   # the run and its date
    assert "<call>" not in first

    # 2. Its review, in the same conversation: its own answer, then the call.
    assert seen[1]["input"][:2] == [seen[0]["input"][0], {
        "type": "message", "id": "m1", "role": "assistant",
        "content": [{"type": "output_text", "text": json.dumps(own)}]}]
    assert seen[1]["text"]["format"]["schema"] == ai.schema_of(so.Review)
    ask = seen[1]["input"][2]["content"]
    assert ask.startswith(so.REVIEW) and "cut these sections" not in ask
    shown = json.loads(ask.split("<call>\n")[1].split("\n</call>")[0])
    assert (shown["action"], shown["summary"], shown["sell_when"]) == (
        "buy", answer()["summary"], answer()["sell_when"])
    assert shown["broker_verdicts"] == [] and "recorded_as" not in shown

    # Kept, never changed, and priced as the other provider's two requests.
    assert (out["verdict"], out["own_action"], out["context_source"]) == (
        "disagree", "buy", "stored")
    assert out["own_target_pct_a_year"] == pytest.approx(30.0, abs=0.1)
    row = conn.execute("""select provider, model, context_source, own_action, own_confidence,
                                 own_call->>'summary', verdict, errors, missed, rule_issues,
                                 cut_sections, cost_usd, instructions
                          from ai_call_second_opinion""").fetchall()
    assert row == [("openai", "openai/gpt-test", "stored", "buy", 0.55, own["summary"],
                    "disagree", review()["errors"], review()["missed"], [], [], 0.006,
                    f"{ai.PROMPT_VERSION}/{so.PROMPT_VERSION}")]
    assert conn.execute("""select provider, count(*) from llm_call
                           where feature = 'second_opinion' group by 1""").fetchall() == [
        ("openai", 2)]
    assert [o["opinion_id"] for o in so.for_call(conn, made["call_id"])] == [out["opinion_id"]]
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("update ai_call_second_opinion set verdict = 'agree'")
    conn.rollback()

    # The AI calls page: pick any call and see its second opinions.
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "AI calls"
    at.run()
    assert not at.exception, at.exception
    assert any(h.value == "Second opinion on a call" for h in at.subheader)
    page = " ".join(m.value for m in at.markdown)
    assert "**Disagrees with the call** (70% sure)" in page
    assert "Its own call, made before it saw this one: **🟢 BUY**" in page
    assert review()["errors"][0] in page


@pytest.mark.db
def test_an_older_calls_context_is_rebuilt_and_cut_sections_are_given_in_full(scored):  # noqa: F811
    """A call made before context was kept: why it was asked, the previous call and the last
    buy are rebuilt as they stood then. A section its prompt cut at 30,000 characters
    reaches the other model in full, and the review is told the call saw it cut."""
    conn, run_id = scored
    _insert(conn, run_id, 1, "GROW", "buy", dt.date(2024, 6, 28),     # a month ago
            created=utc_now() - dt.timedelta(days=30))
    made = ai.make_call(Assistant.open(conn, _cfg(), reply(action="hold")), "GROW")
    stored, source = so.context_of(conn, so._call(conn, made["call_id"]))
    assert source == "stored" and stored["previous_call"]["action"] == "buy"
    assert stored["last_buy"]["target_price"] is not None

    big = ["x" * 100] * 400
    older = _copy(conn, made["call_id"],
                  created_at="created_at - interval '1 second'",
                  inputs=f"jsonb_set(inputs, '{{filings}}', '{json.dumps(big)}'::jsonb)")
    call = so._call(conn, older)
    rebuilt, source = so.context_of(conn, call)
    assert source == "rebuilt"
    assert _same(rebuilt, stored)                   # as the call's model saw them
    _, _, cut = so.sections_of(conn, call)
    assert cut == ["filings"]

    client_ = FakeClient(msg(text(json.dumps(answer("sell")))),
                         msg(text(json.dumps(review("partly agree", errors=[])))))
    out = so.second_opinion(Assistant.open(conn, _cfg(), client_), older,
                            so.route_of(f"anthropic:{_cfg().model}"))
    first, second = client_.requests
    got = sections(first["messages"][0]["content"])
    assert got["filings"] == big                                # in full
    assert got["previous_call"]["summary"] == answer()["summary"]
    assert "since" in got["last_buy"]
    assert "cut these sections" in second["messages"][-1]["content"]
    assert "filings" in second["messages"][-1]["content"].split("<call>")[0]
    assert second["messages"][0] == first["messages"][0]
    assert second["messages"][1]["role"] == "assistant"
    assert [b.text for b in second["messages"][1]["content"]] == [json.dumps(answer("sell"))]
    assert (out["context_source"], out["cut_sections"], out["verdict"]) == (
        "rebuilt", json.dumps(["filings"]), "partly agree")
    assert conn.execute("select context_source, cut_sections from ai_call_second_opinion"
                        ).fetchall() == [("rebuilt", ["filings"])]


def _same(a: dict, b: dict) -> bool:
    """The rebuilt context equals the stored one, once both are JSON."""
    norm = lambda v: json.loads(json.dumps(v, default=lambda o: o.isoformat()))  # noqa: E731
    return norm(a) == norm(b)


def test_the_models_offered_have_a_key_and_a_price(monkeypatch):
    for _, key, _ in so.PROVIDERS.values():
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cfg = priced({"openai:gpt-test": (2, 5), "gemini:gem-test": (1, 2)})
    offered = so.models(cfg)
    assert "openai:gpt-test" in offered
    assert "gemini:gem-test" not in offered                 # no Gemini key
    assert f"anthropic:{cfg.model}" in offered              # the SDK finds Claude's login
    assert "openai:unpriced" not in offered
    assert so.route_of("deepseek:deepseek-reasoner").provider == "deepseek"
    with pytest.raises(ValueError, match="provider:model"):
        so.route_of("gpt-test")
    assert cfg.route_for("second_opinion").model == cfg.model   # until one is pinned


@pytest.mark.db
def test_a_longer_answer_than_asked_is_cut_not_refused(scored):  # noqa: F811
    """Another provider's model doesn't always keep to limits the schema leaves out: a
    review with 9 errors (asked for at most 8) is kept whole, its own call's 11 reasons
    keep the first 8, and entries too short to say anything are dropped."""
    conn, _ = scored
    made = ai.make_call(Assistant.open(conn, _cfg(), reply()), "GROW")
    own = answer("hold", reasons=[f"Reason {i}: margin up {i}%" for i in range(11)],
                 risks=["n/a", "Debt rising 20% a year"])
    rev = review(errors=[f"Error {i}: ROCE is {i}%, not 24.1%" for i in range(9)])
    client_ = FakeClient(msg(text(json.dumps(own))), msg(text(json.dumps(rev))))
    out = so.second_opinion(Assistant.open(conn, _cfg(), client_), made["call_id"],
                            so.route_of(f"anthropic:{_cfg().model}"))
    assert out["errors"] == rev["errors"]
    assert out["own"]["reasons"] == own["reasons"][:8]
    assert out["own"]["risks"] == ["Debt rising 20% a year"]
    assert "at most 8 items" in client_.requests[1]["messages"][-1]["content"]
