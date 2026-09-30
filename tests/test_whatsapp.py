"""WhatsApp messages for the AI's new buy and sell calls, through Meta's Cloud API or
CallMeBot. No request leaves the machine: a mock transport answers as each service did
when probed on 2026-09-30 (Meta: HTTP 401 and a JSON error for a bad token; CallMeBot:
HTTP 203 and the reason in red for a bad key)."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

import httpx
import pytest
import test_ai_calls
from test_ai_calls import _insert

from igs.alerts import whatsapp
from igs.config import load_alerts

scored = test_ai_calls.scored
CFG = load_alerts().whatsapp
DEPLOY = Path(__file__).resolve().parents[1] / "docs" / "DEPLOY.md"
APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"


def _call(**over) -> dict:
    return {"call_id": 7, "company_id": 4, "symbol": "NBFC", "name": "Example Finance Ltd",
            "action": "buy", "previous": "hold", "confidence": 0.63, "horizon_months": 12,
            "summary": "Quality and momentum are both in the top quintile of peers.",
            "reasons": ["ROE 18.2%, 86th percentile of peers", "PAT up 24% a year"],
            "risks": ["Credit costs could rise"],
            "buy_when": ["Close above the 200-day average (met now)"],
            "sell_when": ["Gross NPA above 4%", "Close below the 200-day average"],
            "data_gaps": ["Growth pillar not scored"], "price_date": dt.date(2024, 11, 29),
            "price_close": 845.2, "trigger": "scheduled", "reason": "new results filed",
            "created_at": dt.datetime(2026, 9, 30, 13, 35, tzinfo=dt.UTC), **over}


NO_RECORD = {"summary": []}


def _meta_env(monkeypatch):
    monkeypatch.setenv("IGS_WHATSAPP_PROVIDER", "meta")
    monkeypatch.setenv("IGS_WHATSAPP_TO", "+91 98123-45678")
    monkeypatch.setenv("IGS_WHATSAPP_TOKEN", "EAAG-secret-token")
    monkeypatch.setenv("IGS_WHATSAPP_PHONE_ID", "106540352242922")


def _callmebot_env(monkeypatch):
    monkeypatch.setenv("IGS_WHATSAPP_PROVIDER", "callmebot")
    monkeypatch.setenv("IGS_WHATSAPP_TO", "0091 98123 45678")
    monkeypatch.setenv("IGS_CALLMEBOT_APIKEY", "secret-key")
    monkeypatch.setattr(whatsapp, "CALLMEBOT_GAP_S", 0.0)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- settings


def test_numbers_need_their_country_code():
    assert whatsapp.normalise_number("+91 98123 45678") == "+919812345678"
    assert whatsapp.normalise_number("0091-(98123)-45678") == "+919812345678"
    for bad in ("9812345678", "+91 98123", "", "+0919812345678"):
        with pytest.raises(ValueError, match="country code"):
            whatsapp.normalise_number(bad)


def test_the_service_and_what_it_still_needs(monkeypatch):
    assert whatsapp.provider() is None and not whatsapp.ready()
    monkeypatch.setenv("IGS_CALLMEBOT_APIKEY", "k")
    assert whatsapp.provider() == "callmebot"                 # the one with its keys set
    assert whatsapp.missing() == ["IGS_WHATSAPP_TO"]
    monkeypatch.setenv("IGS_WHATSAPP_PROVIDER", "meta")       # an explicit choice wins
    assert whatsapp.missing() == ["IGS_WHATSAPP_TO", "IGS_WHATSAPP_TOKEN",
                                  "IGS_WHATSAPP_PHONE_ID"]
    monkeypatch.setenv("IGS_WHATSAPP_PROVIDER", "sms")
    assert "not 'sms'" in whatsapp.missing()[0]
    with pytest.raises(whatsapp.WhatsAppError, match="not set up"):
        whatsapp.send(_call(), NO_RECORD, CFG, _client(lambda r: httpx.Response(500)))


# --------------------------------------------------------------------------- the message


def test_the_template_in_the_guide_is_the_one_sent():
    """Meta sends only the template approved word for word, so the guide must show it."""
    assert whatsapp.TEMPLATE_BODY in DEPLOY.read_text(encoding="utf-8")
    assert re.findall(r"\{\{(\d+)\}\}", whatsapp.TEMPLATE_BODY) == [str(i) for i in
                                                                    range(1, 11)]


def test_template_parameters_are_single_lines_within_the_limit():
    long = _call(summary="Margins widened again.\n" * 120,
                 reasons=[f"reason {i} with  several   spaces" for i in range(40)])
    params = whatsapp.template_params(long, NO_RECORD)
    assert len(params) == 10
    assert params[0] == "BUY Example Finance Ltd (NBFC), was hold"
    assert params[1:3] == ["63%, 12 months", "Rs 845.20 on 29 Nov 2024"]
    assert all("\n" not in p and "  " not in p and p for p in params)
    assert params[3].endswith("…") and params[4].endswith("…")        # the long ones shrink
    assert params[8] == "new results filed"
    body = whatsapp.TEMPLATE_BODY
    for i, p in enumerate(params, 1):
        body = body.replace(f"{{{{{i}}}}}", p)
    assert len(body) <= 1024
    short = whatsapp.template_params(_call(previous=None, trigger="manual"), NO_RECORD)
    assert short[0].endswith("its first call on this stock")
    assert short[8] == "your request in the app" and not short[3].endswith("…")


def test_the_record_line_says_when_calls_are_unproven():
    assert whatsapp.record_line(NO_RECORD, "sell") == (
        "no earlier sell call is a month old yet, so the AI's sell calls are unproven")
    record = {"summary": [{"action": "buy", "horizon": "1m", "calls": 5, "right_pct": 60.0},
                          {"action": "buy", "horizon": "3m", "calls": 2, "right_pct": 50.0},
                          {"action": "hold", "horizon": "1m", "calls": 3, "right_pct": None}]}
    assert whatsapp.record_line(record, "buy") == (
        "buy calls against the Nifty 500: after 1m 60% right (5), after 3m 50% right (2)")


def test_the_text_message_has_every_part_and_splits_when_long():
    text = whatsapp.text_message(_call(), NO_RECORD)
    for part in ("*BUY Example Finance Ltd (NBFC), was hold*", "Confidence 63%",
                 "*Reasons*\n• ROE 18.2%", "*Sell when*\n• Gross NPA above 4%",
                 "*Data gaps*", "Prompted by: new results filed",
                 "Call 7, made 30 Sep 2026 19:05 IST", "Not investment advice"):
        assert part in text
    assert whatsapp._parts(text, 5000) == [text]
    parts = whatsapp._parts("a" * 30 + "\n" + "b" * 10, 25)
    assert parts == ["(1/2) " + "a" * 25, "(2/2) " + "a" * 5 + "\n" + "b" * 10]


# --------------------------------------------------------------------------- the services


def test_meta_gets_the_template_and_its_errors_are_explained(monkeypatch):
    _meta_env(monkeypatch)
    seen = []

    def ok(request):
        seen.append(request)
        return httpx.Response(200, json={"messaging_product": "whatsapp",
                                         "messages": [{"id": "wamid.X"}]})
    assert whatsapp.send(_call(), NO_RECORD, CFG, _client(ok)) == "WhatsApp Cloud API (Meta)"
    req = seen[0]
    assert str(req.url) == "https://graph.facebook.com/v25.0/106540352242922/messages"
    assert req.headers["Authorization"] == "Bearer EAAG-secret-token"
    body = json.loads(req.content)
    assert body["to"] == "+919812345678" and body["type"] == "template"
    assert body["template"]["name"] == "igs_ai_call"
    assert body["template"]["language"] == {"code": "en"}
    params = body["template"]["components"][0]["parameters"]
    assert len(params) == 10 and params[0] == {"type": "text", "text":
                                               "BUY Example Finance Ltd (NBFC), was hold"}

    def bad_token(request):          # as the API answered a probe with a made-up token
        return httpx.Response(401, json={"error": {
            "message": "Invalid OAuth access token - Cannot parse access token",
            "type": "OAuthException", "code": 190, "fbtrace_id": "A"}})
    with pytest.raises(whatsapp.WhatsAppError) as err:
        whatsapp.send(_call(), NO_RECORD, CFG, _client(bad_token))
    assert "Cannot parse access token" in str(err.value) and "permanent" in str(err.value)
    assert "secret" not in str(err.value)


def test_callmebot_gets_the_text_and_its_errors_keep_the_key_out(monkeypatch):
    _callmebot_env(monkeypatch)
    seen = []

    def ok(request):
        seen.append(request.url.params)
        return httpx.Response(200, text="<p>Message queued. You will receive it soon.")
    assert whatsapp.send(_call(), NO_RECORD, CFG, _client(ok)) == "CallMeBot"
    assert seen[0]["phone"] == "+919812345678" and seen[0]["apikey"] == "secret-key"
    assert seen[0]["text"].startswith("*IndiaGrowthScreener: new AI call*")

    def bad_key(request):            # as CallMeBot answered a probe with a made-up key
        return httpx.Response(203, text=(
            '<p>Message to: +919812345678<p>Text to send: x<p style="color:red"><b>APIKey '
            'is invalid.</b> Please create a new one or contact support if you lost it.'))
    with pytest.raises(whatsapp.WhatsAppError, match="CallMeBot refused the message: "
                                                     "APIKey is invalid. Please create"):
        whatsapp.send(_call(), NO_RECORD, CFG, _client(bad_key))

    def down(request):
        raise httpx.ConnectError("failed for https://api.callmebot.com/?apikey=secret-key")
    with pytest.raises(whatsapp.WhatsAppError) as err:
        whatsapp.send(_call(), NO_RECORD, CFG, _client(down))
    assert str(err.value) == "Could not reach CallMeBot (ConnectError)."


def test_a_test_message_needs_no_call(monkeypatch):
    _callmebot_env(monkeypatch)
    seen = []

    def ok(request):
        seen.append(request.url.params["text"])
        return httpx.Response(200, text="Message queued")
    whatsapp.send_test(CFG, _client(ok))
    assert "*TEST message, not a real call*" in seen[0]


# --------------------------------------------------------------------------- end to end


@pytest.mark.db
def test_new_buy_and_sell_calls_each_reach_whatsapp_once(scored, monkeypatch, tmp_path):
    """Asked for by the owner: a detailed WhatsApp message whenever a buy or sell call is
    identified. Holds and a repeated buy stay off WhatsApp; the digest is unchanged."""
    from igs.daily import send_alerts
    conn, run_id = scored
    for key in ("IGS_SMTP_HOST", "IGS_TELEGRAM_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    _callmebot_env(monkeypatch)
    answer = {"status": 203}
    sent = []

    def service(request):
        if answer["status"] != 200:
            return httpx.Response(203, text='<p style="color:red"><b>APIKey is invalid.</b>')
        sent.append(request.url.params["text"])
        return httpx.Response(200, text="Message queued")
    monkeypatch.setattr(whatsapp, "_client", lambda: _client(service))
    day, now = dt.date(2024, 11, 29), dt.datetime.now(dt.UTC)
    for company_id, symbol, action, days_ago in (
            (1, "GROW", "buy", 5), (1, "GROW", "buy", 4), (1, "GROW", "hold", 3),
            (4, "NBFC", "sell", 3), (3, "BANK", "hold", 2), (2, "CYCL", "hold", 2),
            (2, "CYCL", "buy", 1)):
        _insert(conn, run_id, company_id, symbol, action, day,
                now - dt.timedelta(days=days_ago))

    # The key is wrong: nothing is sent, the reason is kept (without the key) for a retry.
    with pytest.raises(RuntimeError, match="APIKey is invalid"):
        send_alerts(conn, run_id, tmp_path)
    rows = conn.execute("select status, last_error from alert_outbox "
                        "where channel = 'whatsapp'").fetchall()
    assert len(rows) == 3 and {r[0] for r in rows} == {"failed"}
    assert all("APIKey is invalid" in r[1] and "secret-key" not in r[1] for r in rows)

    answer["status"] = 200
    conn.execute("update alert_outbox set next_attempt_at = now()")
    conn.commit()
    send_alerts(conn, run_id, tmp_path)
    assert [t.split("\n")[2] for t in sent] == [
        "*BUY Grow Industries Ltd (GROW), its first call on this stock*",
        "*SELL Example Finance Ltd (NBFC), its first call on this stock*",
        "*BUY Cyclical Steel Ltd (CYCL), was hold*"]
    assert "Prompted by: test reason" in sent[0] and "*Buy when*" in sent[0]
    send_alerts(conn, run_id, tmp_path)                       # nothing is sent twice
    assert len(sent) == 3
    digest = (tmp_path / "alerts" / f"alerts_run{run_id}.txt").read_text(encoding="utf-8")
    assert "GROW: AI call HOLD, was buy" in digest            # holds still reach the digest


@pytest.mark.db
def test_the_settings_page_saves_whatsapp_and_sends_a_test(db_conn, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    seen = []

    def ok(request):
        seen.append(request.url.params["text"])
        return httpx.Response(200, text="Message queued")
    monkeypatch.setattr(whatsapp, "_client", lambda: _client(ok))
    monkeypatch.setattr(whatsapp, "CALLMEBOT_GAP_S", 0.0)
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    at.sidebar.radio(key="page").set_value("Settings").run()
    assert any(h.value == "WhatsApp alerts" for h in at.subheader)
    assert at.button(key="wa_test").disabled                  # nothing set up yet
    at.radio(key="wa_provider").set_value("callmebot").run()
    at.text_input(key="wa_to").input("98123 45678").run()
    at.text_input(key="wa_apikey").input("123456").run()
    at.button(key="wa_save").click().run()
    assert any("country code" in e.value for e in at.error)
    at.text_input(key="wa_to").input("+91 98123 45678").run()
    at.button(key="wa_save").click().run()
    assert not at.exception, at.exception
    env = Path(os.environ["IGS_ENV_FILE"]).read_text(encoding="utf-8")
    assert "IGS_WHATSAPP_PROVIDER=callmebot" in env and "IGS_WHATSAPP_TO=+919812345678" in env
    assert "IGS_CALLMEBOT_APIKEY=123456" in env
    assert at.text_input(key="wa_apikey").value == ""         # not left in the field
    assert any("Sending through **CallMeBot**" in m.value for m in at.markdown)
    at.button(key="wa_test").click().run()
    assert any("Sent through CallMeBot" in s.value for s in at.success)
    assert "TEST message" in seen[0]
    at.button(key="wa_remove").click().run()
    assert "WHATSAPP" not in Path(os.environ["IGS_ENV_FILE"]).read_text(encoding="utf-8")
