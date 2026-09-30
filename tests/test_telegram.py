"""Telegram: the daily digest, and a detailed message for each new AI buy or sell call
(the telegram_calls channel). No request leaves the machine: a mock transport answers as
Telegram's Bot API did when probed with a made-up token on 2026-09-30 (HTTP 401 and
{"ok": false, "description": "Unauthorized"})."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import httpx
import pytest
import test_ai_calls
from test_ai_calls import _insert
from test_whatsapp import _call, _client

from igs.alerts import call_message, delivery

scored = test_ai_calls.scored
TOKEN = "123456789:AAE-secret-token"
APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"


def _env(monkeypatch, chat="987654321"):
    monkeypatch.setenv("IGS_TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("IGS_TELEGRAM_CHAT_ID", chat)


def _refuse(status: int, description: str):
    return lambda request: httpx.Response(status, json={
        "ok": False, "error_code": status, "description": description})


def test_telegram_errors_say_why_and_never_show_the_token(monkeypatch):
    _env(monkeypatch)
    with pytest.raises(delivery.TelegramError) as err:
        delivery.send_telegram("hello", _client(_refuse(401, "Unauthorized")))
    assert str(err.value) == ("Telegram refused the request: Unauthorized. The bot token is "
                              "wrong: copy it again from @BotFather.")
    with pytest.raises(delivery.TelegramError, match="haven't pressed Start"):
        delivery.send_telegram("hello", _client(_refuse(400, "Bad Request: chat not found")))

    def down(request):
        raise httpx.ConnectError(f"failed for https://api.telegram.org/bot{TOKEN}/sendMessage")
    with pytest.raises(delivery.TelegramError) as err:
        delivery.send_telegram("hello", _client(down))
    assert str(err.value) == "Could not reach Telegram (ConnectError)." and "secret" not in \
        str(err.value)


def test_find_my_chat_id_reads_who_wrote_to_the_bot():
    seen = []

    def updates(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 1, "message": {"message_id": 1, "text": "/start", "chat": {
                "id": 111, "type": "private", "first_name": "Old"}}},
            {"update_id": 2, "message": {"message_id": 2, "text": "/start", "chat": {
                "id": 987654321, "type": "private", "first_name": "Ananth",
                "username": "ananth"}}},
            {"update_id": 3, "message": {"message_id": 3, "text": "hi", "chat": {
                "id": 987654321, "type": "private", "first_name": "Ananth"}}}]})
    chats = delivery.telegram_chats(TOKEN, _client(updates))
    assert [c["id"] for c in chats] == [987654321, 111]               # newest first
    assert chats[0]["name"] == "Ananth" and seen[0].method == "GET"
    assert seen[0].url.path == f"/bot{TOKEN}/getUpdates"
    assert delivery.telegram_chats(TOKEN, _client(
        lambda r: httpx.Response(200, json={"ok": True, "result": []}))) == []


def test_the_telegram_message_is_the_call_and_briefly_why():
    """Asked for by the owner: only buy and sell calls, with the reason in brief."""
    c = _call()
    c["reasons"] = ["ROE 18.2%, 80th percentile of peers", "Growth " + "x" * 300,
                    "Revenue up 21% a year", "A fourth reason that is left out"]
    text = call_message.brief_message(c)
    lines = text.split("\n")
    assert lines[0] == "BUY Example Finance Ltd (NBFC), was hold"
    assert lines[1].startswith("Confidence ") and "last close Rs " in lines[1]
    why = lines[lines.index("Why:") + 1:]
    assert [w for w in why if w.startswith("• ")] == [
        "• ROE 18.2%, 80th percentile of peers", "• Growth…", "• Revenue up 21% a year"]
    assert "fourth reason" not in text and "*" not in text and len(text) < 1200
    assert text.endswith("The AI's judgement, not investment advice. Details on the AI "
                         "calls page.")
    assert call_message._short("word " * 100, 40).endswith("…")
    assert len(call_message._short("word " * 100, 40)) <= 40


@pytest.mark.db
def test_new_buy_and_sell_calls_each_reach_telegram_once(scored, monkeypatch, tmp_path):
    """Asked for by the owner: Telegram gets only the AI's new buy and sell calls, each as a
    brief message, and no digest (the NBFC hold is in the digest only)."""
    from igs.daily import send_alerts
    conn, run_id = scored
    monkeypatch.delenv("IGS_SMTP_HOST", raising=False)
    _env(monkeypatch)
    answer = {"status": 401}
    sent = []

    def telegram(request):
        if answer["status"] != 200:
            return _refuse(401, "Unauthorized")(request)
        sent.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})
    monkeypatch.setattr(delivery, "_client", lambda: _client(telegram))
    day, now = dt.date(2024, 11, 29), dt.datetime.now(dt.UTC)
    for company_id, symbol, action, days_ago in (
            (1, "GROW", "buy", 5), (1, "GROW", "buy", 4), (4, "NBFC", "hold", 3),
            (2, "CYCL", "sell", 2)):
        _insert(conn, run_id, company_id, symbol, action, day,
                now - dt.timedelta(days=days_ago))

    with pytest.raises(RuntimeError, match="Unauthorized"):
        send_alerts(conn, run_id, tmp_path)
    errors = conn.execute("select channel, last_error from alert_outbox "
                          "where status = 'failed' order by channel").fetchall()
    assert {c for c, _ in errors} == {"telegram_calls"}
    assert all("Unauthorized" in e and "secret" not in e for _, e in errors)

    answer["status"] = 200
    conn.execute("update alert_outbox set next_attempt_at = now()")
    conn.commit()
    send_alerts(conn, run_id, tmp_path)
    assert {m["chat_id"] for m in sent} == {"987654321"}
    assert [m["text"].split("\n")[0] for m in sent] == [
        "BUY Grow Industries Ltd (GROW), its first call on this stock",
        "SELL Cyclical Steel Ltd (CYCL), its first call on this stock"]
    assert all("\nWhy:\n• " in m["text"] for m in sent)
    assert "NBFC: AI call HOLD" in (tmp_path / "alerts" / f"alerts_run{run_id}.txt"
                                    ).read_text()
    send_alerts(conn, run_id, tmp_path)                       # nothing is sent twice
    assert len(sent) == 2


@pytest.mark.db
def test_the_settings_page_sets_up_telegram(db_conn, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    for key in ("IGS_TELEGRAM_TOKEN", "IGS_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    texts = []

    def telegram(request):
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5,
                "message": {"text": "/start", "chat": {"id": 987654321, "type": "private",
                                                         "first_name": "Ananth"}}}]})
        texts.append(dict(httpx.QueryParams(request.content.decode()))["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})
    monkeypatch.setattr(delivery, "_client", lambda: _client(telegram))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    at.sidebar.radio(key="page").set_value("Settings").run()
    assert any(h.value == "Telegram alerts" for h in at.subheader)
    assert at.button(key="tg_test").disabled
    at.button(key="tg_find").click().run()
    assert any("Paste the bot token" in e.value for e in at.error)
    at.text_input(key="tg_token").input(TOKEN).run()
    at.button(key="tg_find").click().run()
    assert not at.exception, at.exception
    assert at.text_input(key="tg_chat").value == "987654321"
    assert any("Found Ananth (987654321)" in s.value for s in at.success)
    at.button(key="tg_save").click().run()
    env = Path(os.environ["IGS_ENV_FILE"]).read_text(encoding="utf-8")
    assert f"IGS_TELEGRAM_TOKEN={TOKEN}" in env and "IGS_TELEGRAM_CHAT_ID=987654321" in env
    assert at.text_input(key="tg_token").value == ""          # not left in the field
    assert any("Sending to chat 987654321" in m.value for m in at.markdown)
    shown = " ".join(m.value for m in at.markdown)
    assert "secret-token" not in shown
    at.button(key="tg_test").click().run()
    assert any("check Telegram" in s.value for s in at.success)
    assert "TEST message, not a real call" in texts[0]
    at.button(key="tg_remove").click().run()
    assert "TELEGRAM" not in Path(os.environ["IGS_ENV_FILE"]).read_text(encoding="utf-8")
