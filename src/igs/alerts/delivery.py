"""Alert delivery: one digest per run by email (SMTP) and/or Telegram (off as shipped), and
a brief Telegram message for each new AI buy or sell call (igs.alerts.call_message, the
telegram_calls channel).

Credentials come from environment variables only. With no channel configured
the digest is only written to disk. Telegram's sendMessage is the only HTTP write here: it
sends notifications to the user's own chats and has nothing to do with any broker or
order.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import os
import re
import smtplib
import unicodedata
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path

import httpx

from igs.alerts import call_message
from igs.alerts.rules import ADVICE_KINDS, SEPARATE_TELEGRAM_KINDS, Alert
from igs.config import AlertsConfig
from igs.guardrails import assert_no_advice_language

TITLES = {"daily_failures": "Data pipeline problems",
          "run_health": "Run health (High conviction withheld)",
          "high_conviction_change": "High conviction changes",
          "top_decile_entry": "New in the top decile", "watchlist_red_flag":
          "Watchlist red flags and cautions",
          "watchlist_results": "Results filed by watchlist names",
          "pledge_change": "Promoter pledge changes",
          "insider_trade": "Insider trades", "announcement_note": "Announcement notes",
          "ai_call": "AI calls (the AI's judgement, not the screen's)"}
TELEGRAM_LIMIT = 4000
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_HINTS = {401: "The bot token is wrong: copy it again from @BotFather.",
                  403: "The bot can't write to you: open it in Telegram and press Start "
                       "(or unblock it)."}
PER_CALL = ("telegram_calls",)      # one message per alert, not a digest


class TelegramError(RuntimeError):
    """A failure whose message is safe to show and store: Telegram's own description,
    never the URL, which holds the bot token."""


def configured_channels(cfg: AlertsConfig) -> tuple[str, ...]:
    """No credentials means file-only delivery, as on a fresh installation."""
    ready = {"email": os.environ.get("IGS_SMTP_HOST") and os.environ.get("IGS_ALERT_TO"),
             "telegram": telegram_ready(), "telegram_calls": telegram_ready()}
    return tuple(c for c in ready if cfg.channels.get(c) and ready[c])


def digest(alerts: list[Alert], run_id: int, as_of: dt.datetime) -> str:
    """Every line passes the advice-language guardrail except the AI's own calls, which are
    buy / hold / sell calls by design and are labelled as the AI's."""
    lines = [f"IndiaGrowthScreener alerts - run {run_id}, data as of {as_of:%Y-%m-%d}", ""]
    titles = TITLES | {a.kind: a.kind.replace("_", " ").capitalize()
                       for a in alerts if a.kind not in TITLES}
    for kind, title in titles.items():
        items = [a for a in alerts if a.kind == kind]
        if items:
            if kind not in ADVICE_KINDS:
                assert_no_advice_language("\n".join(a.message for a in items))
            lines += [f"{title} ({len(items)}):", *[f"- {a.message}" for a in items], ""]
    if not alerts:
        lines += ["No new alerts.", ""]
    return "\n".join(lines)


def send_email(text: str, subject: str, smtp_factory: Callable = smtplib.SMTP) -> bool:
    host, to = os.environ.get("IGS_SMTP_HOST"), os.environ.get("IGS_ALERT_TO")
    if not host or not to:
        return False
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = os.environ.get("IGS_ALERT_FROM", to)
    msg.set_content(text)
    with smtp_factory(host, int(os.environ.get("IGS_SMTP_PORT", "587")), timeout=30) as smtp:
        if os.environ.get("IGS_SMTP_USER"):
            smtp.starttls()
            smtp.login(os.environ["IGS_SMTP_USER"], os.environ.get("IGS_SMTP_PASSWORD", ""))
        smtp.send_message(msg)
    return True


def _client() -> httpx.Client:
    return httpx.Client(timeout=20)


def telegram_ready() -> bool:
    return bool(os.environ.get("IGS_TELEGRAM_TOKEN", "").strip()
                and os.environ.get("IGS_TELEGRAM_CHAT_ID", "").strip())


# Each kind of message can go to its own chat: a Telegram group with the bot in it. A kind
# without its own chat goes to IGS_TELEGRAM_CHAT_ID, as everything did before.
CHATS = {"general": "IGS_TELEGRAM_CHAT_ID",          # the app and the data
         "calls": "IGS_TELEGRAM_CALLS_CHAT_ID",      # AI calls and ranking alerts
         "trading": "IGS_TELEGRAM_TRADING_CHAT_ID",  # intraday calls, approvals, orders
         "paper": "IGS_TELEGRAM_PAPER_CHAT_ID",      # momentum and the Plan
         "results": "IGS_TELEGRAM_RESULTS_CHAT_ID",  # quarterly results, results days
         "deals": "IGS_TELEGRAM_DEALS_CHAT_ID"}      # institutional deals
CHAT_NAMES = {"general": "General: the app and the data",
              "calls": "AI calls and ranking alerts",
              "trading": "Intraday calls, approvals and orders",
              "paper": "Paper portfolios: momentum and the Plan",
              "results": "Quarterly results and results-day reactions",
              "deals": "Institutional deals"}


# Minus signs a phone keyboard or a copy may give instead of '-'.
_DASHES = dict.fromkeys(map(ord, "\u2212\u2010\u2011\u2012\u2013\u2014\ufe63\uff0d"), "-")


def parse_chat_id(text: str | None) -> str | None:
    """The Telegram chat or user ID in what was typed or pasted: "" when blank, None when
    there is not exactly one ID in it. Forgives spaces, other minus signs, a leading '#'
    (Telegram Web's address) and a whole copied row of Find chats. A '+' or '@' means a
    phone number, a username or an invite link, which are not IDs."""
    t = unicodedata.normalize("NFKC", str(text or "")).translate(_DASHES)
    t = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", t).strip()
    if not t:
        return ""
    if "+" in t or "@" in t:
        return None
    squeezed = re.sub(r"\s+", "", t).lstrip("#")
    if re.fullmatch(r"-?\d+", squeezed):
        return str(int(squeezed))
    found = set(re.findall(r"(?<![\d-])-?\d{5,}(?!\d)", t))
    return str(int(found.pop())) if len(found) == 1 else None


def chat_id(kind: str = "general") -> str:
    """The chat that `kind` of message goes to (CHATS); IGS_TELEGRAM_CHAT_ID when it has
    none of its own."""
    own = os.environ.get(CHATS[kind], "").strip()
    return own or os.environ.get("IGS_TELEGRAM_CHAT_ID", "").strip()


def owner_id() -> str:
    """The Telegram user whose replies can approve orders in a group: IGS_TELEGRAM_OWNER_ID,
    else IGS_TELEGRAM_CHAT_ID when that is a private chat (whose ID is the user's)."""
    own = os.environ.get("IGS_TELEGRAM_OWNER_ID", "").strip()
    main = os.environ.get("IGS_TELEGRAM_CHAT_ID", "").strip()
    return own or (main if main.isdigit() else "")


class _TelegramLogFilter(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r"(api\.telegram\.org/bot)[^/\s]+", r"\1[redacted]",
                            record.getMessage())
        record.args = ()
        return True


logging.getLogger("httpx").addFilter(_TelegramLogFilter())


def _telegram(method: str, token: str, client: httpx.Client, files: dict | None = None,
              **fields: str) -> dict:
    """One Bot API request: sendMessage and sendPhoto (with `files`) are POSTs, everything
    else here a read. Seen on 2026-09-30 with a made-up token: HTTP 401 and {"ok": false,
    "description": "Unauthorized"}."""
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        resp = (client.post(url, data=fields, files=files)
                if method in ("sendMessage", "sendPhoto")
                else client.get(url, params=fields))
    except httpx.HTTPError as exc:
        raise TelegramError(f"Could not reach Telegram ({type(exc).__name__}).") from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code == 200 and body.get("ok"):
        return body
    reason = body.get("description") or f"HTTP {resp.status_code}"
    hint = ("The chat ID is wrong, or you haven't pressed Start in your bot yet."
            if "chat not found" in reason else TELEGRAM_HINTS.get(resp.status_code, ""))
    raise TelegramError(f"Telegram refused the request: {reason}. {hint}".strip())


def send_telegram(text: str, client: httpx.Client | None = None,
                  chat_kind: str = "general") -> bool:
    """`text` to the chat for `chat_kind` (CHATS), in parts of TELEGRAM_LIMIT."""
    token = os.environ.get("IGS_TELEGRAM_TOKEN", "").strip()
    chat = chat_id(chat_kind)
    if not token or not chat:
        return False
    client = client or _client()
    for start in range(0, len(text), TELEGRAM_LIMIT):
        _telegram("sendMessage", token, client, chat_id=chat,
                  text=text[start:start + TELEGRAM_LIMIT], disable_web_page_preview="true")
    return True


send_calls = functools.partial(send_telegram, chat_kind="calls")
send_trading = functools.partial(send_telegram, chat_kind="trading")
send_paper = functools.partial(send_telegram, chat_kind="paper")
send_results = functools.partial(send_telegram, chat_kind="results")
send_deals = functools.partial(send_telegram, chat_kind="deals")


def send_telegram_photo(png: bytes, caption: str, client: httpx.Client | None = None,
                        reply_to: int | None = None, chat_kind: str = "general") -> bool:
    """One image with a short caption, as a reply to `reply_to` if given."""
    token = os.environ.get("IGS_TELEGRAM_TOKEN", "").strip()
    chat = chat_id(chat_kind)
    if not token or not chat:
        return False
    fields = {"chat_id": chat, "caption": caption[:1024]}
    if reply_to:
        fields["reply_to_message_id"] = str(reply_to)
    _telegram("sendPhoto", token, client or _client(),
              files={"photo": ("chart.png", png, "image/png")}, **fields)
    return True


def charts_on() -> bool:
    """call_messages.charts in config/alerts.yaml."""
    from igs.config import load_alerts
    return load_alerts().call_messages.charts


def send_chart(conn, company_id: int | None, client: httpx.Client | None = None,
               reply_to: int | None = None, chat_kind: str = "calls") -> bool:
    """The stock's week, month and year price chart, sent after a call or ranking
    message (igs.alerts.price_chart), to the same chat. A chart that can't be drawn or sent
    is skipped: it never holds up, fails or repeats the message it follows."""
    if not company_id or not telegram_ready():
        return False
    try:
        if not charts_on():
            return False
        from igs.alerts import price_chart
        with conn.transaction():          # a failed query leaves the caller's work intact
            made = price_chart.chart(conn, company_id)
        return made is not None and send_telegram_photo(*made, client, reply_to, chat_kind)
    except Exception as exc:  # noqa: BLE001 - the message it follows is already sent
        logging.getLogger(__name__).warning("Price chart not sent (%s)", type(exc).__name__)
        return False


send_trading_chart = functools.partial(send_chart, chat_kind="trading")


def send_intraday_telegram(text: str, client: httpx.Client | None = None) -> int | bool:
    """One Telegram message to the trading chat, with a durable ID for reply approval."""
    token = os.environ.get('IGS_TELEGRAM_TOKEN', '').strip()
    chat = chat_id('trading')
    if not token or not chat:
        return False
    if len(text) > TELEGRAM_LIMIT:
        raise TelegramError('Intraday alert is too long to approve by reply')
    result = _telegram('sendMessage', token, client or _client(), chat_id=chat,
                       text=text, disable_web_page_preview='true')
    message_id = result.get('result', {}).get('message_id')
    if type(message_id) is not int:
        raise TelegramError('Telegram did not return a message ID')
    return message_id


def _chats_from(updates: list[dict]) -> list[dict]:
    """The chats `updates` came from, newest first: ID, name and type, never the text."""
    chats: dict[int, dict] = {}
    for update in reversed(updates):
        msg = (update.get("message") or update.get("edited_message")
               or update.get("my_chat_member") or {})
        chat = msg.get("chat")
        if chat and chat.get("id") is not None and chat["id"] not in chats:
            name = chat.get("title") or " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x)
            chats[chat["id"]] = {"id": chat["id"], "name": name or chat.get("username", ""),
                                 "username": chat.get("username"), "type": chat.get("type")}
    return list(chats.values())


def _seen_path() -> Path:
    from igs.config import settings_dir
    return settings_dir() / "telegram_chats.json"


def remember_chats(updates: list[dict]) -> None:
    """Keep the chats the bot's updates came from, newest first (at most 20), for Settings'
    Find chats: the approval poller (igs.intraday.telegram_approvals) reads every update
    within seconds, after which Telegram no longer returns it. Never fails the caller."""
    found = _chats_from(updates)
    if not found:
        return
    try:
        known = {c["id"]: c for c in seen_chats()}
        merged = found + [c for i, c in known.items() if i not in {f["id"] for f in found}]
        path = _seen_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged[:20]), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    except OSError:
        logging.getLogger(__name__).warning("Telegram chats not remembered")


def seen_chats() -> list[dict]:
    """The chats remember_chats kept, newest first."""
    try:
        return json.loads(_seen_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def telegram_chats(token: str, client: httpx.Client | None = None) -> list[dict]:
    """The chats that recently wrote to the bot, newest first: after you press Start in
    your bot, yours is the first; a group appears once the bot is added to it. Those still
    waiting at Telegram (it keeps them about a day, until read), then those the approval
    poller has seen."""
    body = _telegram("getUpdates", token.strip(), client or _client())
    live = _chats_from(body.get("result", []))
    ids = {c["id"] for c in live}
    return live + [c for c in seen_chats() if c["id"] not in ids]


def send_telegram_call(conn, call_id: int, client: httpx.Client | None = None) -> bool:
    """The brief message about one AI call, to the calls chat: the call and why."""
    c = call_message.call(conn, call_id)
    if c is None:
        raise TelegramError(f"AI call {call_id} not found")
    return send_telegram(call_message.brief_message(c), client, "calls")


def send_telegram_test(client: httpx.Client | None = None) -> list[str]:
    """To check the set-up: a sample call message, laid out as a real one, to the calls
    chat, and to each other chat a line saying what it gets. Returns the chats written to."""
    if not telegram_ready():
        raise TelegramError("Telegram is not set up: .env needs IGS_TELEGRAM_TOKEN and "
                            "IGS_TELEGRAM_CHAT_ID.")
    send_telegram(call_message.brief_message(call_message.sample("Telegram")), client,
                  "calls")
    gets: dict[str, list[str]] = {}
    for kind in CHATS:
        gets.setdefault(chat_id(kind), []).append(CHAT_NAMES[kind])
    for chat, names in gets.items():
        if chat != chat_id("calls"):
            kind = next(k for k in CHATS if chat_id(k) == chat)
            send_telegram("TEST: IndiaGrowthScreener. This chat gets: " + "; ".join(names)
                          + ".", client, kind)
    return list(gets)


def deliver(alerts: list[Alert], run_id: int, as_of: dt.datetime, cfg: AlertsConfig,
            out_dir: Path, smtp_factory: Callable = smtplib.SMTP,
            telegram_client: httpx.Client | None = None) -> dict[str, bool | str]:
    text = digest(alerts, run_id, as_of)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"alerts_run{run_id}.txt"
    if alerts or not path.exists():
        path.write_text(text, encoding="utf-8")
    result: dict[str, bool | str] = {"file": str(path)}
    if alerts and cfg.channels.get("email"):
        result["email"] = send_email(text, f"IndiaGrowthScreener: {len(alerts)} alerts",
                                     smtp_factory)
    if alerts and cfg.channels.get("telegram"):
        result["telegram"] = send_telegram(text, telegram_client)
    return result


def deliver_pending(conn, cfg: AlertsConfig, *, smtp_factory: Callable = smtplib.SMTP,
                    telegram_client: httpx.Client | None = None) -> dict[str, int]:
    """Retry the durable outbox independently per channel, at most five attempts. Email and
    Telegram get one digest per run; telegram_calls one message per alert (an AI call or a
    ranking alert).

    Row locks prevent simultaneous workers from sending the same pending alert.
    Delivery is at-least-once: a process dying after remote acceptance but before
    the commit can resend, since SMTP/Telegram offer no transactional acknowledgement.
    """
    sent, errors = {}, []
    for channel in ("email", "telegram", *PER_CALL):
        if not cfg.channels.get(channel):
            continue
        with conn.transaction():
            rows = conn.execute("""select o.alert_id, a.kind, a.company_id, a.message,
                       a.dedupe_key, a.run_id, r.as_of
                from alert_outbox o join alert_log a using (alert_id)
                join score_run r on r.run_id = a.run_id
                where o.channel = %s and o.status in ('pending', 'failed') and o.attempts < 5
                  and o.next_attempt_at <= now()
                order by a.run_id, o.alert_id for update of o skip locked""", (channel,)).fetchall()
            groups: dict[int, list] = {}
            for row in rows:     # per-call channels: one alert per batch
                groups.setdefault(row[0] if channel in PER_CALL else row[5], []).append(row)
            for batch in groups.values():
                ids = [r[0] for r in batch]
                alerts = [Alert(r[1], r[2], r[3], r[4]) for r in batch]
                try:
                    if channel == "telegram_calls" and batch[0][1] in SEPARATE_TELEGRAM_KINDS:
                        text = batch[0][3]
                        if batch[0][1] == 'broker_agreement':
                            # A broker's buy goes only with a target that clears the AI
                            # calls' minimum a year (igs.call_list.given_buy).
                            from igs.call_list import agreement_target
                            given, line = agreement_target(conn, batch[0][4])
                            if not given:
                                conn.execute("""update alert_outbox set status = 'cancelled',
                                    last_error = %s where channel = %s
                                    and alert_id = any(%s)""", (line, channel, ids))
                                continue
                            text = 'BROKER + AI AGREEMENT\n' + text + (f'\n{line}' if line
                                                                        else '')
                        ok = send_telegram(text, telegram_client, "calls")
                    elif channel == "telegram_calls":
                        ok = send_telegram_call(conn, call_message.call_id(batch[0][4]),
                                                telegram_client)
                    else:
                        text = digest(alerts, batch[0][5], batch[0][6])
                        ok = (send_email(text, f"IndiaGrowthScreener: {len(alerts)} alerts",
                                         smtp_factory) if channel == "email"
                              else send_telegram(text, telegram_client))
                    if not ok:
                        raise RuntimeError(f"{channel} credentials are not configured")
                except Exception as exc:  # noqa: BLE001 - other channel must still be attempted
                    # Do not persist transport exception strings: Telegram URLs contain the
                    # bot token. TelegramError messages are written to be safe to keep.
                    reason = (f"{type(exc).__name__}: {exc}"
                              if isinstance(exc, TelegramError)
                              else f"{type(exc).__name__}: {channel} delivery failed")
                    conn.execute("""update alert_outbox set status = 'failed',
                        attempts = attempts + 1, last_error = %s,
                        next_attempt_at = now() + interval '5 minutes' * (attempts + 1)
                        where channel = %s and alert_id = any(%s)""", (reason, channel, ids))
                    errors.append(reason)
                else:
                    conn.execute("""update alert_outbox set status = 'sent',
                        attempts = attempts + 1, last_error = null, sent_at = now()
                        where channel = %s and alert_id = any(%s)""", (channel, ids))
                    conn.execute("""update alert_log set delivered = delivered ||
                        jsonb_build_object(%s::text, true) where alert_id = any(%s)""",
                        (channel, ids))
                    sent[channel] = sent.get(channel, 0) + len(ids)
                    if channel == "telegram_calls":   # a call or a ranking message
                        send_chart(conn, batch[0][2], telegram_client, chat_kind="calls")
        conn.commit()
    if errors:
        raise RuntimeError("; ".join(errors) + "; pending alerts retained for retry")
    return sent


def send_agreements(conn) -> None:
    """Try queued Telegram calls now; retain failures for the normal outbox retry."""
    from igs.config import load_alerts
    cfg = load_alerts()
    if not cfg.channels.get('telegram_calls') or not telegram_ready():
        return
    try:
        from igs.alerts.rules import queue_ranked_events
        queue_ranked_events(conn, cfg)
        deliver_pending(conn, cfg.model_copy(update={
            'channels': {'telegram_calls': True}}))
    except Exception:  # noqa: BLE001 - a sent/stored verdict must not be rolled back
        conn.rollback()
        logging.getLogger(__name__).warning('Telegram calls pending; delivery will retry')
