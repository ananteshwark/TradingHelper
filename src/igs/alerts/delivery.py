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
          "top_decile_entry": "New in the top decile", "watchlist_red_flag":
          "Watchlist red flags", "watchlist_results": "Results filed by watchlist names",
          "pledge_change": "Promoter pledge changes"}
TELEGRAM_LIMIT = 4000


def digest(alerts: list[Alert], run_id: int, as_of: dt.datetime) -> str:
    lines = [f"IndiaGrowthScreener alerts - run {run_id}, data as of {as_of:%Y-%m-%d}", ""]
    for kind, title in TITLES.items():
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
    with smtp_factory(host, int(os.environ.get("IGS_SMTP_PORT", "587"))) as smtp:
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
    path.write_text(text)
    result: dict[str, bool | str] = {"file": str(path)}
    if alerts and cfg.channels.get("email"):
        result["email"] = send_email(text, f"IndiaGrowthScreener: {len(alerts)} alerts",
                                     smtp_factory)
    if alerts and cfg.channels.get("telegram"):
        result["telegram"] = send_telegram(text, telegram_client)
    return result
