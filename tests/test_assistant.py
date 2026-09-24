"""The optional research assistant, offline: a scripted fake client returns real SDK
message objects, so the tool loop, guardrails, budget, caching and storage are exercised
without calling the Claude API."""

from __future__ import annotations

import ast
import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from anthropic.types.beta import BetaMessage

from igs.assistant import announcements, prompts
from igs.assistant.ask import ask
from igs.assistant.brief import brief
from igs.assistant.llm import (
    FALLBACK_BETA,
    Assistant,
    AssistantError,
    AssistantUnavailable,
    BudgetExceeded,
    echo_content,
)
from igs.config import load_assistant

SRC = Path(__file__).resolve().parents[1] / "src" / "igs"
USAGE = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0}


def msg(*content: dict, stop: str = "end_turn", model: str = "claude-opus-5",
        usage: dict | None = None) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": model,
        "content": list(content), "stop_reason": stop, "stop_sequence": None,
        "usage": usage or USAGE})


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def tool(name: str, tid: str, **args) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


class FakeClient:
    """Plays back scripted responses and records every request."""

    def __init__(self, *script: BetaMessage) -> None:
        self.script = list(script)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs) -> BetaMessage:
        self.requests.append(copy.deepcopy(kwargs))
        if not self.script:
            raise AssertionError("unexpected extra request")
        return self.script.pop(0)


def _cfg(**update):
    return load_assistant().model_copy(update={"enabled": True, **update})


@pytest.fixture
def scored(db_conn, tmp_path, monkeypatch):
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


def _calls(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute("select feature, model, input_tokens, output_tokens, cost_usd::float8 "
                    "from llm_call order by call_id")
        return cur.fetchall()


# --------------------------------------------------------------------------- ask


@pytest.mark.db
def test_ask_answers_from_tools_on_the_stored_run(scored):
    conn, run_id = scored
    client = FakeClient(
        msg({"type": "thinking", "thinking": "", "signature": "sig"},
            tool("run_overview", "t1"), tool("stock_detail", "t2", symbol="grow"),
            stop="tool_use"),
        msg(text("GROW is Rejected in run 1: its promoter pledge is above the limit.")))
    a = ask(Assistant.open(conn, _cfg(), client), "Why is GROW rejected?")
    assert a.text.startswith("GROW is Rejected") and a.run_id == run_id and not a.guarded
    assert [c["tool"] for c in a.tool_calls] == ["run_overview", "stock_detail"]

    first, second = client.requests
    assert first["model"] == "claude-opus-5" and first["thinking"] == {"type": "adaptive"}
    assert first["output_config"] == {"effort": "high"}
    assert first["fallbacks"] == "default" and first["betas"] == [FALLBACK_BETA]
    assert first["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False
               for t in first["tools"])
    assert f"Score run {run_id}" in first["messages"][-1]["content"]
    # Both tool results go back together in one user turn, after the assistant turn.
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    detail = json.loads(results[1]["content"])
    assert detail["stock"]["symbol"] == "GROW" and detail["run_id"] == run_id
    assert detail["stock"]["tier"] == "Rejected" and detail["checks_tripped"]
    # Two calls logged, each 1000 in + 200 out at $5/$25 per million tokens.
    assert [(f, m) for f, m, *_ in _calls(conn)] == [("ask", "claude-opus-5")] * 2
    assert a.cost_usd == pytest.approx(2 * (1000 * 5 + 200 * 25) / 1e6)


@pytest.mark.db
def test_unknown_symbol_goes_back_to_the_model_as_a_tool_error(scored):
    conn, _ = scored
    client = FakeClient(msg(tool("stock_detail", "t1", symbol="NOPE"), stop="tool_use"),
                        msg(text("NOPE is not in this run.")))
    a = ask(Assistant.open(conn, _cfg(), client), "Tell me about NOPE")
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "unknown symbol" in result["content"]
    assert a.tool_calls == [{"tool": "stock_detail", "input": {"symbol": "NOPE"},
                             "error": True}]


@pytest.mark.db
def test_advice_language_is_rewritten_once_then_withheld(scored):
    conn, _ = scored
    client = FakeClient(msg(text("You should buy GROW now.")),
                        msg(text("GROW ranks low because of its pledge.")))
    a = ask(Assistant.open(conn, _cfg(), client), "Should I buy GROW?")
    assert a.text == "GROW ranks low because of its pledge." and a.guarded
    assert "Rewrite it" in client.requests[1]["messages"][-1]["content"]

    client = FakeClient(msg(text("Buy it.")), msg(text("Still a strong buy.")))
    a = ask(Assistant.open(conn, _cfg(), client), "Should I buy GROW?")
    assert a.text == prompts.WITHHELD


@pytest.mark.db
def test_tool_rounds_are_capped(scored):
    conn, _ = scored
    cfg = _cfg()
    cfg = cfg.model_copy(update={"features": cfg.features.model_copy(update={
        "ask": cfg.features.ask.model_copy(update={"max_tool_rounds": 2})})})
    client = FakeClient(*[msg(tool("run_overview", f"t{i}"), stop="tool_use")
                          for i in range(3)])
    a = ask(Assistant.open(conn, cfg, client), "Loop forever")
    assert len(client.requests) == 3 and any("stopped after 2 rounds" in n for n in a.notes)


# --------------------------------------------------------------------------- limits


@pytest.mark.db
def test_off_switch_budget_and_refusals(scored):
    conn, _ = scored
    with pytest.raises(AssistantUnavailable, match="enabled: true"):
        Assistant.open(conn, load_assistant().model_copy(update={"enabled": False}),
                       FakeClient())
    client = FakeClient()
    with conn.cursor() as cur:
        cur.execute("""insert into llm_call (feature, model, input_tokens, output_tokens,
                           cost_usd) values ('ask', 'claude-opus-5', 1, 1, 2.5)""")
    with pytest.raises(BudgetExceeded, match="daily budget"):
        ask(Assistant.open(conn, _cfg(daily_budget_usd=2.0), client), "Anything")
    assert client.requests == []                  # nothing was sent
    client = FakeClient(msg(stop="refusal"))
    with pytest.raises(AssistantError, match="declined"):
        ask(Assistant.open(conn, _cfg(daily_budget_usd=10.0), client), "Anything")


def test_fallback_turns_are_echoed_without_the_declined_models_blocks():
    m = msg({"type": "thinking", "thinking": "", "signature": "s"},
            text("partial"),
            {"type": "fallback", "from": {"model": "claude-opus-5"},
             "to": {"model": "claude-opus-4-8"},
             "trigger": {"type": "refusal", "category": "cyber"}},
            tool("run_overview", "t1"), stop="tool_use")
    assert [b.type for b in echo_content(m)] == ["text", "tool_use"]


def test_haiku_gets_no_thinking_or_effort(db_conn):
    cfg = _cfg(model="claude-haiku-4-5")
    client = FakeClient(msg(text("ok"), model="claude-haiku-4-5"))
    Assistant(db_conn, cfg, client).create("brief", system="s",
                                            messages=[{"role": "user", "content": "x"}])
    req = client.requests[0]
    assert "thinking" not in req and "output_config" not in req and "fallbacks" not in req


# --------------------------------------------------------------------------- brief


@pytest.mark.db
def test_brief_is_written_once_per_run_and_stock(scored):
    conn, run_id = scored
    client = FakeClient(msg(text("**Where it stands** GROW is Rejected for its pledge.")))
    assistant = Assistant.open(conn, _cfg(), client)
    first = brief(assistant, "GROW")
    again = brief(assistant, "grow")
    assert not first.cached and again.cached and again.text == first.text
    assert len(client.requests) == 1 and client.requests[0]["output_config"]["effort"] == "medium"
    assert "<data>" in client.requests[0]["messages"][0]["content"]


# --------------------------------------------------------------------------- announcements


@pytest.mark.db
def test_announcement_notes_are_validated_and_stored_once(scored):
    import db_market
    conn, _ = scored
    with conn.cursor() as cur:
        cur.execute("""insert into announcement (exchange, symbol, filed_at, category, subject,
                           source_fetch_id, ingested_at)
                       values ('NSE', 'GROW', '2024-10-01 17:00+05:30', 'Board Meeting',
                               'Board meeting on 10 October to consider results', %s, now())""",
                    (db_market.FETCH,))
    todo = announcements.pending(conn, days=5000, scope="universe", limit=50)
    assert len(todo) == 2 and todo[0]["symbol"] == "LATE"          # newest first
    batch = todo
    items = [{"index": 0, "category": "management_or_board_change", "materiality": "high",
              "summary": "The chief financial officer resigned with immediate effect.",
              "concerns": ["key_managerial_resignation"]},
             {"index": 7, "category": "other", "materiality": "low", "summary": "x",
              "concerns": []}]
    client = FakeClient(msg(text(json.dumps({"items": items}))))
    cfg = _cfg()
    cfg = cfg.model_copy(update={"features": cfg.features.model_copy(update={
        "announcements": cfg.features.announcements.model_copy(update={"batch_size": 2})})})
    res = announcements.read_new(Assistant.open(conn, cfg, client), days=5000, limit=2)
    assert (res.read, res.stored) == (2, 1)
    assert any("index 7" in i for i in res.issues) and any("got no note" in i
                                                           for i in res.issues)
    req = client.requests[0]
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["effort"] == "low" and "<announcements>" in \
        req["messages"][0]["content"]
    with conn.cursor() as cur:
        cur.execute("select symbol, materiality, concerns from announcement_note")
        assert cur.fetchall() == [(batch[0]["symbol"], "high", ["key_managerial_resignation"])]
    left = announcements.pending(conn, days=5000, scope="universe", limit=50)
    assert len(left) == len(todo) - 1                      # the noted one is not re-read


# --------------------------------------------------------------------------- isolation


def test_scoring_code_never_imports_the_assistant():
    """Rankings, checks and backtests must not depend on a language model (look-ahead and
    reproducibility): no module under pit, factors, score, backtest or normalize, nor the
    universe, may import igs.assistant or the Anthropic SDK."""
    roots = [SRC / d for d in ("pit", "factors", "score", "backtest", "normalize")]
    files = [p for r in roots for p in r.rglob("*.py")] + [SRC / "universe.py"]
    bad = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                     [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            bad += [f"{path.relative_to(SRC)}: {n}" for n in names
                    if n.startswith(("igs.assistant", "anthropic"))]
    assert bad == []


def test_config_requires_a_price_for_the_model():
    from pydantic import ValidationError

    from igs.config import AssistantConfig
    with pytest.raises(ValidationError, match="no price"):
        AssistantConfig(model="claude-unknown", prices_usd_per_mtok={})
    assert load_assistant().enabled is False                 # off unless the user turns it on


@pytest.mark.db
def test_material_notes_on_watchlist_names_raise_an_alert(scored):
    from igs.alerts.rules import watchlist_announcement_notes
    conn, _ = scored
    with conn.cursor() as cur:
        cur.execute("insert into watchlist (company_id) values (6)")          # LATE
        cur.execute("""insert into announcement_note (exchange, symbol, filed_at, subject,
                           category, materiality, summary, concerns, model, prompt_version)
                       values ('NSE', 'LATE', '2024-10-15 18:00+05:30', 'Resignation of CFO',
                               'management_or_board_change', 'medium',
                               'The CFO resigned.', '{key_managerial_resignation}',
                               'claude-opus-5', 'announcements-v1'),
                              ('NSE', 'GROW', '2024-10-01 17:00+05:30', 'Board meeting',
                               'board_meeting', 'high', 'A board meeting.', '{}',
                               'claude-opus-5', 'announcements-v1')""")
    now = dt.datetime.now(dt.UTC)
    alerts = watchlist_announcement_notes(conn, now - dt.timedelta(hours=1),
                                          now + dt.timedelta(minutes=1), {})
    # GROW isn't on the watchlist; LATE's note is medium but names a concern.
    assert [a.company_id for a in alerts] == [6]
    assert "key managerial resignation" in alerts[0].message
    assert "not used in ranking" in alerts[0].message


@pytest.mark.db
def test_api_ask_and_brief(scored, monkeypatch):
    import os

    from fastapi.testclient import TestClient

    import igs.assistant.llm as llm
    from igs.api.app import app
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    http = TestClient(app)
    r = http.post("/ask", json={"question": "Why is GROW rejected?"})
    assert r.status_code == 503 and "enabled: true" in r.json()["detail"]   # off by default

    fake = FakeClient(msg(tool("stock_detail", "t1", symbol="GROW"), stop="tool_use"),
                      msg(text("GROW is Rejected for its promoter pledge.")),
                      msg(text("**Where it stands** Rejected for its pledge.")))
    monkeypatch.setattr(llm, "load_assistant", _cfg)
    monkeypatch.setattr(llm, "make_client", lambda: fake)
    r = http.post("/ask", json={"question": "Why is GROW rejected?"})
    body = r.json()
    assert r.status_code == 200 and body["ai_generated"] is True
    assert body["answer"].startswith("GROW is Rejected") and body["disclaimer"]
    assert [x["tool"] for x in body["lookups"]] == ["stock_detail"]
    r = http.get("/stocks/GROW/brief")
    assert r.status_code == 200 and r.json()["brief"].startswith("**Where it stands**")
    assert http.get("/stocks/GROW/brief").json()["stored"] is True    # not written twice
    assert fake.script == []
