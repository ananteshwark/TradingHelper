"""Alert delivery: one digest per run by email (SMTP) and/or Telegram.

Credentials come from environment variables only. With no channel configured
the digest is only written to disk. Telegram's sendMessage is the one HTTP
POST in the codebase: it sends a notification to the user's own chat and has
nothing to do with any broker or order.
"""

from __future__ import annotations

import datetime as dt
import os
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path

import httpx

from igs.alerts.rules import Alert
from igs.config import AlertsConfig
from igs.guardrails import DISCLAIMER, assert_no_advice_language

TITLES = {"daily_failures": "Data pipeline problems",
          "run_health": "Run health (High conviction withheld)",
          "high_conviction_change": "High conviction changes",
          "top_decile_entry": "New in the top decile", "watchlist_red_flag":
          "Watchlist red flags and cautions",
          "watchlist_results": "Results filed by watchlist names",
          "pledge_change": "Promoter pledge changes",
          "insider_trade": "Insider trades", "announcement_note": "Announcement notes"}
TELEGRAM_LIMIT = 4000


def configured_channels(cfg: AlertsConfig) -> tuple[str, ...]:
    """No credentials means file-only delivery, as on a fresh installation."""
    ready = {"email": os.environ.get("IGS_SMTP_HOST") and os.environ.get("IGS_ALERT_TO"),
             "telegram": os.environ.get("IGS_TELEGRAM_TOKEN")
             and os.environ.get("IGS_TELEGRAM_CHAT_ID")}
    return tuple(c for c in ready if cfg.channels.get(c) and ready[c])


def digest(alerts: list[Alert], run_id: int, as_of: dt.datetime) -> str:
    lines = [f"IndiaGrowthScreener alerts - run {run_id}, data as of {as_of:%Y-%m-%d}", ""]
    titles = TITLES | {a.kind: a.kind.replace("_", " ").capitalize()
                       for a in alerts if a.kind not in TITLES}
    for kind, title in titles.items():
        items = [a for a in alerts if a.kind == kind]
        if items:
            lines += [f"{title} ({len(items)}):", *[f"- {a.message}" for a in items], ""]
    if not alerts:
        lines += ["No new alerts.", ""]
    lines.append(DISCLAIMER)
    return assert_no_advice_language("\n".join(lines))


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


def send_telegram(text: str, client: httpx.Client | None = None) -> bool:
    token, chat = os.environ.get("IGS_TELEGRAM_TOKEN"), os.environ.get("IGS_TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    client = client or httpx.Client(timeout=20)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for start in range(0, len(text), TELEGRAM_LIMIT):
        resp = client.post(url, data={"chat_id": chat, "text": text[start:start + TELEGRAM_LIMIT],
                                      "disable_web_page_preview": "true"})
        resp.raise_for_status()
    return True


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
    """Retry the durable outbox independently per channel, at most five attempts.

    Row locks prevent simultaneous workers from sending the same pending alert.
    Delivery is at-least-once: a process dying after remote acceptance but before
    the commit can resend, since SMTP/Telegram offer no transactional acknowledgement.
    """
    sent, errors = {}, []
    for channel in ("email", "telegram"):
        if not cfg.channels.get(channel):
            continue
        with conn.transaction():
            rows = conn.execute("""select o.alert_id, a.kind, a.company_id, a.message,
                       a.dedupe_key, a.run_id, r.as_of
                from alert_outbox o join alert_log a using (alert_id)
                join score_run r on r.run_id = a.run_id
                where o.channel = %s and o.status <> 'sent' and o.attempts < 5
                  and o.next_attempt_at <= now()
                order by a.run_id, o.alert_id for update of o skip locked""", (channel,)).fetchall()
            groups: dict[int, list] = {}
            for row in rows:
                groups.setdefault(row[5], []).append(row)
            for run_id, batch in groups.items():
                ids = [r[0] for r in batch]
                alerts = [Alert(r[1], r[2], r[3], r[4]) for r in batch]
                try:
                    text = digest(alerts, run_id, batch[0][6])
                    ok = (send_email(text, f"IndiaGrowthScreener: {len(alerts)} alerts",
                                     smtp_factory) if channel == "email"
                          else send_telegram(text, telegram_client))
                    if not ok:
                        raise RuntimeError(f"{channel} credentials are not configured")
                except Exception as exc:  # noqa: BLE001 - other channel must still be attempted
                    # Do not persist transport exception strings: Telegram URLs contain tokens.
                    reason = f"{type(exc).__name__}: {channel} delivery failed"
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
