"""WhatsApp messages for the AI's new buy and sell calls: one detailed message per call.

Two services can send them; IGS_WHATSAPP_PROVIDER in .env picks one:
- meta: WhatsApp's own Cloud API. Outside a 24-hour window that only a message from you
  opens, it delivers nothing but templates Meta has approved, so a call goes out as the
  parameters of the `igs_ai_call` template (TEMPLATE_BODY; docs/DEPLOY.md says how to
  create it). Parameters are single lines and the whole body stays within 1,024
  characters, so long parts are shortened.
- callmebot: CallMeBot's free API for personal use. Free text, sent through its servers.

Credentials come from the environment only. Errors raised here carry the service's own
message but never a URL, header or key: CallMeBot's URL holds the API key.
"""

from __future__ import annotations

import datetime as dt
import html
import os
import re
import time
from typing import Any

import httpx

from igs.config import WhatsAppConfig
from igs.timeutil import IST

PROVIDERS = {"meta": "WhatsApp Cloud API (Meta)", "callmebot": "CallMeBot"}
KEYS = {"meta": ("IGS_WHATSAPP_TOKEN", "IGS_WHATSAPP_PHONE_ID"),
        "callmebot": ("IGS_CALLMEBOT_APIKEY",)}
META_URL = "https://graph.facebook.com/{version}/{phone_id}/messages"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
CALLMEBOT_PART = 1500           # characters per CallMeBot message; longer text is split
CALLMEBOT_GAP_S = 3.0           # between CallMeBot requests (a free, shared service)
TEMPLATE_LIMIT = 1000           # Meta allows 1,024 characters in a template body
NUMBER = re.compile(r"^\+[1-9]\d{7,14}$")
META_HINTS = {
    190: "The access token is invalid or has expired (a temporary token lasts 24 hours): "
         "create a permanent one, as docs/DEPLOY.md describes.",
    131030: "Your number is not on the test number's recipient list: add it under WhatsApp, "
            "API Setup in the Meta app.",
    132001: "The igs_ai_call template does not exist in that language or is not approved "
            "yet: check WhatsApp Manager, Message templates."}

# The template to create in WhatsApp Manager (category Utility, language English), word
# for word. {{n}} is the n-th parameter of template_params().
TEMPLATE_BODY = """New AI call from IndiaGrowthScreener: {{1}}

Confidence and horizon: {{2}}
Last close it saw: {{3}}

Summary: {{4}}

Reasons: {{5}}

Buy when: {{6}}

Sell when: {{7}}

Risks: {{8}}

Prompted by: {{9}}

Record so far: {{10}}

This is a language model's judgement, checked only by its own record. It is not \
investment advice; the decision and its risk are yours."""
FOOTER = ("The AI's judgement, checked only by its own record (AI calls page in the app). "
          "Not investment advice; the decision and its risk are yours.")

_last_callmebot = 0.0


class WhatsAppError(RuntimeError):
    """A failure whose message is safe to show and store (no URL, token or key)."""


# --------------------------------------------------------------------------- settings


def normalise_number(raw: str) -> str:
    """+919812345678 from "+91 98123 45678", "0091-98123-45678" and the like. A number
    without its country code is refused rather than guessed."""
    number = re.sub(r"[\s\-().]", "", raw or "")
    if number.startswith("00"):
        number = "+" + number[2:]
    if not NUMBER.match(number):
        raise ValueError(f"{raw!r} is not a WhatsApp number with its country code, "
                         "e.g. +919812345678")
    return number


def provider() -> str | None:
    """IGS_WHATSAPP_PROVIDER, or the one service whose keys are all set."""
    chosen = os.environ.get("IGS_WHATSAPP_PROVIDER", "").strip().lower()
    if chosen:
        return chosen
    ready = [p for p, keys in KEYS.items() if all(os.environ.get(k) for k in keys)]
    return ready[0] if ready else None


def missing() -> list[str]:
    """What .env still needs before a message can be sent."""
    chosen = provider()
    if chosen is None:
        return ["IGS_WHATSAPP_PROVIDER (meta or callmebot)"]
    if chosen not in PROVIDERS:
        return [f"IGS_WHATSAPP_PROVIDER: meta or callmebot, not {chosen!r}"]
    return [k for k in ("IGS_WHATSAPP_TO", *KEYS[chosen]) if not os.environ.get(k)]


def ready() -> bool:
    return not missing()


# --------------------------------------------------------------------------- the message


def call(conn, call_id: int) -> dict | None:
    """One AI call with the company's name and the action of the call before it."""
    with conn.cursor() as cur:
        cur.execute("""select c.call_id, c.company_id, c.symbol, co.name, c.action,
                              c.confidence, c.horizon_months, c.summary, c.reasons, c.risks,
                              c.buy_when, c.sell_when, c.data_gaps, c.price_date,
                              c.price_close, c.trigger, c.reason, c.created_at,
                              (select p.action from ai_call p
                               where p.company_id = c.company_id
                                 and p.created_at < c.created_at
                               order by p.created_at desc limit 1) as previous
                       from ai_call c join company co using (company_id)
                       where c.call_id = %s""", (call_id,))
        row = cur.fetchone()
        return dict(zip([d.name for d in cur.description], row, strict=True)) if row else None


def wanted(conn, kind: str, dedupe_key: str, cfg: WhatsAppConfig) -> bool:
    """An ai_call alert whose call is one of cfg.actions, and (changes_only) the stock's
    first call or a change of action."""
    if kind != "ai_call":
        return False
    c = call(conn, int(dedupe_key.split(":", 1)[1]))
    return (c is not None and c["action"] in cfg.actions
            and not (cfg.changes_only and c["previous"] == c["action"]))


def record_line(record: dict, action: str) -> str:
    """How the AI's earlier calls of this kind did, from the AI calls page's record."""
    rows = [s for s in record["summary"] if s["action"] == action]
    if not rows:
        return (f"no earlier {action} call is a month old yet, so the AI's {action} calls "
                "are unproven")
    return f"{action} calls against the Nifty 500: " + ", ".join(
        f"after {s['horizon']} {s['right_pct']:.0f}% right ({s['calls']})"
        if s["right_pct"] is not None else f"after {s['horizon']} not scored ({s['calls']})"
        for s in rows)


def _headline(c: dict) -> str:
    if c["action"] == "test":
        return "TEST message, not a real call"
    was = (f", was {c['previous']}" if c["previous"] else ", its first call on this stock")
    return f"{c['action'].upper()} {c['name']} ({c['symbol']}){was}"


def _close(c: dict) -> str:
    if c["price_close"] is None:
        return "not available"
    return f"Rs {c['price_close']:,.2f} on {c['price_date']:%d %b %Y}"


def _why(c: dict) -> str:
    return c["reason"] if c["trigger"] == "scheduled" and c["reason"] else \
        "your request in the app"


def _one_line(text: str) -> str:
    return " ".join(str(text).split()) or "none"


def _fit(parts: list[str], budget: int) -> list[str]:
    """Shorten the longest parts first until all fit in `budget` characters together."""
    if sum(map(len, parts)) <= budget:
        return parts
    lo, hi = 1, max(map(len, parts))
    while lo < hi:                  # the largest length cap that fits
        mid = (lo + hi + 1) // 2
        if sum(min(len(p), mid) for p in parts) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return [p if len(p) <= lo else p[:lo - 1].rstrip() + "…" for p in parts]


def template_params(c: dict, record: dict) -> list[str]:
    """TEMPLATE_BODY's ten parameters: single lines, all within TEMPLATE_LIMIT."""
    parts = [_headline(c), f"{c['confidence']:.0%}, {c['horizon_months']} months",
             _close(c), c["summary"], "; ".join(c["reasons"]), "; ".join(c["buy_when"]),
             "; ".join(c["sell_when"]), "; ".join(c["risks"]), _why(c),
             record_line(record, c["action"])]
    fixed = len(re.sub(r"\{\{\d+\}\}", "", TEMPLATE_BODY))
    return _fit([_one_line(p) for p in parts], TEMPLATE_LIMIT - fixed)


def text_message(c: dict, record: dict) -> str:
    """The whole call as WhatsApp text (CallMeBot), with *bold* headings."""
    def section(title: str, items: list[str]) -> list[str]:
        return [f"*{title}*", *[f"• {i}" for i in items], ""] if items else []
    made = c["created_at"].astimezone(IST)
    lines = ["*IndiaGrowthScreener: new AI call*", "", f"*{_headline(c)}*",
             f"Confidence {c['confidence']:.0%}, horizon {c['horizon_months']} months",
             f"Last close it saw: {_close(c)}", "", c["summary"], "",
             *section("Reasons", c["reasons"]), *section("Buy when", c["buy_when"]),
             *section("Sell when", c["sell_when"]), *section("Risks", c["risks"]),
             *section("Data gaps", c["data_gaps"]),
             f"Prompted by: {_why(c)}",
             f"Record so far: {record_line(record, c['action'])}",
             f"Call {c['call_id']}, made {made:%d %b %Y %H:%M} IST", "", f"_{FOOTER}_"]
    return "\n".join(lines)


def _parts(text: str, size: int) -> list[str]:
    """Split on line ends into pieces of at most `size` characters, numbered if several."""
    pieces, cur = [], ""
    for line in text.split("\n"):
        for chunk in [line[i:i + size] for i in range(0, len(line), size)] or [""]:
            if cur and len(cur) + 1 + len(chunk) > size:
                pieces, cur = [*pieces, cur], chunk
            else:
                cur = f"{cur}\n{chunk}" if cur else chunk
    pieces += [cur] if cur else []
    n = len(pieces)
    return pieces if n == 1 else [f"({i}/{n}) {p}" for i, p in enumerate(pieces, 1)]


# --------------------------------------------------------------------------- sending


def _client() -> httpx.Client:
    return httpx.Client(timeout=30)


def _env(name: str) -> str:
    return os.environ[name].strip()


def _send_meta(to: str, params: list[str], cfg: WhatsAppConfig, client: httpx.Client
               ) -> None:
    url = META_URL.format(version=cfg.meta_api_version, phone_id=_env("IGS_WHATSAPP_PHONE_ID"))
    body = {"messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {"name": cfg.meta_template, "language": {"code": cfg.meta_language},
                         "components": [{"type": "body", "parameters": [
                             {"type": "text", "text": p} for p in params]}]}}
    try:
        resp = client.post(url, json=body, headers={
            "Authorization": f"Bearer {_env('IGS_WHATSAPP_TOKEN')}"})
    except httpx.HTTPError as exc:
        raise WhatsAppError(f"Could not reach the WhatsApp Cloud API "
                            f"({type(exc).__name__}).") from None
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code == 200 and data.get("messages"):
        return
    err = data.get("error") or {}
    detail = (err.get("error_data") or {}).get("details")
    message = " ".join(x for x in (err.get("message"), detail) if x) or \
        f"HTTP {resp.status_code}"
    hint = META_HINTS.get(err.get("code"), "")
    raise WhatsAppError(f"The WhatsApp Cloud API refused the message: {message}. {hint}"
                        .strip())


def _send_callmebot(to: str, text: str, client: httpx.Client) -> None:
    global _last_callmebot
    params = {"phone": to, "apikey": _env("IGS_CALLMEBOT_APIKEY")}
    for part in _parts(text, CALLMEBOT_PART):
        time.sleep(max(0.0, _last_callmebot + CALLMEBOT_GAP_S - time.monotonic()))
        try:
            resp = client.get(CALLMEBOT_URL, params={**params, "text": part})
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"Could not reach CallMeBot ({type(exc).__name__}).") from None
        finally:
            _last_callmebot = time.monotonic()
        # Seen on 2026-09-30 with a wrong key: HTTP 203 and the reason in red.
        refused = re.search(r'color:\s*red[^>]*>(.*?)(?:<p|$)', resp.text, re.S)
        if resp.status_code != 200 or refused:
            reason = (html.unescape(re.sub(r"<[^>]+>", "", refused.group(1))).strip()
                      if refused else f"HTTP {resp.status_code}")
            raise WhatsAppError(f"CallMeBot refused the message: {reason}")


def send(c: dict, record: dict, cfg: WhatsAppConfig, client: httpx.Client | None = None
         ) -> str:
    """Send one call through the configured service; returns the service's name."""
    if not ready():
        raise WhatsAppError("WhatsApp is not set up; .env still needs: "
                            + ", ".join(missing()))
    try:
        to = normalise_number(os.environ["IGS_WHATSAPP_TO"])
    except ValueError as exc:
        raise WhatsAppError(f"IGS_WHATSAPP_TO: {exc}") from None
    chosen = provider()
    client = client or _client()
    if chosen == "meta":
        _send_meta(to, template_params(c, record), cfg, client)
    else:
        _send_callmebot(to, text_message(c, record), client)
    return PROVIDERS[chosen]


def send_call(conn, call_id: int, cfg: WhatsAppConfig, client: httpx.Client | None = None
              ) -> str:
    from igs.assistant.calls import track_record
    c = call(conn, call_id)
    if c is None:
        raise WhatsAppError(f"AI call {call_id} not found")
    return send(c, track_record(conn), cfg, client)


def send_test(cfg: WhatsAppConfig, client: httpx.Client | None = None) -> str:
    """A sample message, laid out as a real one, to check the set-up."""
    sample: dict[str, Any] = {
        "call_id": 0, "company_id": 0, "symbol": "EXAMPLE", "name": "Example Ltd",
        "action": "test", "previous": None, "confidence": 0.6, "horizon_months": 6,
        "summary": "IndiaGrowthScreener can reach you on WhatsApp. Each new buy or sell "
                   "call by the AI will come as a message like this one.",
        "reasons": ["the reasons the AI gives, with the figures behind them"],
        "risks": ["what could make the call wrong"],
        "buy_when": ["conditions the AI sets for buying"],
        "sell_when": ["conditions the AI sets for selling"], "data_gaps": [],
        "price_date": None, "price_close": None, "trigger": "manual", "reason": None,
        "created_at": dt.datetime.now(IST)}
    return send(sample, {"summary": []}, cfg, client)
