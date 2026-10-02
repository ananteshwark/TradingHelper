"""Alert delivery: one digest per run by email (SMTP) and/or Telegram (off as shipped), and
a message for each new AI buy or sell call (igs.alerts.call_message): detailed on WhatsApp
(igs.alerts.whatsapp), brief on Telegram (the telegram_calls channel).

Credentials come from environment variables only. With no channel configured
the digest is only written to disk. Telegram's sendMessage and WhatsApp's
messages endpoints are the only HTTP writes in the codebase: they send
notifications to the user's own chat and have nothing to do with any broker or
order.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path

import httpx

from igs.alerts import call_message, whatsapp
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
PER_CALL = ("whatsapp", "telegram_calls")      # one message per alert, not a digest


class TelegramError(RuntimeError):
    """A failure whose message is safe to show and store: Telegram's own description,
    never the URL, which holds the bot token."""


def configured_channels(cfg: AlertsConfig) -> tuple[str, ...]:
    """No credentials means file-only delivery, as on a fresh installation."""
    ready = {"email": os.environ.get("IGS_SMTP_HOST") and os.environ.get("IGS_ALERT_TO"),
             "telegram": telegram_ready(), "whatsapp": whatsapp.ready(),
             "telegram_calls": telegram_ready()}
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


class _TelegramLogFilter(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r"(api\.telegram\.org/bot)[^/\s]+", r"\1[redacted]",
                            record.getMessage())
        record.args = ()
        return True


logging.getLogger("httpx").addFilter(_TelegramLogFilter())


def _telegram(method: str, token: str, client: httpx.Client, **fields: str) -> dict:
    """One Bot API request: sendMessage is a POST, everything else here a read. Seen on
    2026-09-30 with a made-up token: HTTP 401 and {"ok": false, "description":
    "Unauthorized"}."""
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        resp = (client.post(url, data=fields) if method == "sendMessage"
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


def send_telegram(text: str, client: httpx.Client | None = None) -> bool:
    token = os.environ.get("IGS_TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("IGS_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    client = client or _client()
    for start in range(0, len(text), TELEGRAM_LIMIT):
        _telegram("sendMessage", token, client, chat_id=chat,
                  text=text[start:start + TELEGRAM_LIMIT], disable_web_page_preview="true")
    return True


def telegram_chats(token: str, client: httpx.Client | None = None) -> list[dict]:
    """The chats that recently wrote to the bot, newest first: after you press Start in
    your bot, yours is the first. Telegram keeps these for about a day."""
    body = _telegram("getUpdates", token.strip(), client or _client())
    chats: dict[int, dict] = {}
    for update in reversed(body.get("result", [])):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat")
        if chat and chat["id"] not in chats:
            name = chat.get("title") or " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x)
            chats[chat["id"]] = {"id": chat["id"], "name": name or chat.get("username", ""),
                                 "username": chat.get("username")}
    return list(chats.values())


def send_telegram_call(conn, call_id: int, client: httpx.Client | None = None) -> bool:
    """The brief message about one AI call: the call and why, in plain text."""
    c = call_message.call(conn, call_id)
    if c is None:
        raise TelegramError(f"AI call {call_id} not found")
    return send_telegram(call_message.brief_message(c), client)


def send_telegram_test(client: httpx.Client | None = None) -> None:
    """A sample call message, laid out as a real one, to check the set-up."""
    if not telegram_ready():
        raise TelegramError("Telegram is not set up: .env needs IGS_TELEGRAM_TOKEN and "
                            "IGS_TELEGRAM_CHAT_ID.")
    send_telegram(call_message.brief_message(call_message.sample("Telegram")), client)


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
                    telegram_client: httpx.Client | None = None,
                    whatsapp_client: httpx.Client | None = None) -> dict[str, int]:
    """Retry the durable outbox independently per channel, at most five attempts. Email and
    Telegram get one digest per run; WhatsApp and telegram_calls one message per alert (an
    AI call).

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
                    if channel == "whatsapp":
                        ok = bool(whatsapp.send_call(conn, call_message.call_id(batch[0][4]),
                                                     cfg.whatsapp, whatsapp_client))
                    elif channel == "telegram_calls" and batch[0][1] in SEPARATE_TELEGRAM_KINDS:
                        prefix = ('BROKER + AI AGREEMENT\n'
                                  if batch[0][1] == 'broker_agreement' else '')
                        ok = send_telegram(prefix + batch[0][3], telegram_client)
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
                    # Do not persist transport exception strings: Telegram and CallMeBot URLs
                    # contain tokens. WhatsAppError and TelegramError messages are written to
                    # be safe to keep.
                    reason = (f"{type(exc).__name__}: {exc}"
                              if isinstance(exc, whatsapp.WhatsAppError | TelegramError)
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
