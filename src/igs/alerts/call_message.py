"""The detailed message about one AI call, sent to WhatsApp (igs.alerts.whatsapp) and
Telegram (igs.alerts.delivery) for each new buy or sell call. Which calls get one is set
under `call_messages` in config/alerts.yaml."""

from __future__ import annotations

import datetime as dt
from typing import Any

from igs.config import CallMessagesConfig
from igs.timeutil import IST

FOOTER = ("The AI's judgement, checked only by its own record (AI calls page in the app). "
          "Not investment advice; the decision and its risk are yours.")


def call(conn, call_id: int) -> dict | None:
    """One AI call with the company's name and the action of the call before it."""
    with conn.cursor() as cur:
        cur.execute("""select c.call_id, c.company_id, c.symbol, co.name, c.action,
                              c.confidence, c.horizon_months, c.summary, c.reasons, c.risks,
                              c.buy_when, c.sell_when, c.data_gaps, c.price_date,
                              c.price_close, c.trigger, c.reason, c.created_at,
                              c.vs_brokers,
                              (select p.action from ai_call p
                               where p.company_id = c.company_id
                                 and p.created_at < c.created_at
                               order by p.created_at desc limit 1) as previous
                       from ai_call c join company co using (company_id)
                       where c.call_id = %s""", (call_id,))
        row = cur.fetchone()
        return dict(zip([d.name for d in cur.description], row, strict=True)) if row else None


def load(conn, call_id: int) -> tuple[dict | None, dict]:
    """The call and the AI's record so far (the AI calls page's summary)."""
    from igs.assistant.calls import track_record
    return call(conn, call_id), track_record(conn)


def call_id(dedupe_key: str) -> int:
    """The call an ai_call alert is about ("ai_call:<call_id>")."""
    return int(dedupe_key.split(":", 1)[1])


def wanted(conn, kind: str, dedupe_key: str, cfg: CallMessagesConfig) -> bool:
    """An ai_call alert whose call is one of cfg.actions, and (changes_only) the stock's
    first call or a change of action."""
    if kind != "ai_call":
        return False
    c = call(conn, call_id(dedupe_key))
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


def headline(c: dict) -> str:
    if c["action"] == "test":
        return "TEST message, not a real call"
    was = (f", was {c['previous']}" if c["previous"] else ", its first call on this stock")
    return f"{c['action'].upper()} {c['name']} ({c['symbol']}){was}"


def close(c: dict) -> str:
    if c["price_close"] is None:
        return "not available"
    return f"Rs {c['price_close']:,.2f} on {c['price_date']:%d %b %Y}"


def why(c: dict) -> str:
    return c["reason"] if c["trigger"] == "scheduled" and c["reason"] else \
        "your request in the app"


def text_message(c: dict, record: dict, bold: bool = True) -> str:
    """The whole call as text: *bold* headings for WhatsApp, plain for Telegram."""
    def b(s: str) -> str:
        return f"*{s}*" if bold else s

    def section(title: str, items: list[str]) -> list[str]:
        return [b(title), *[f"• {i}" for i in items], ""] if items else []
    made = c["created_at"].astimezone(IST)
    lines = [b("IndiaGrowthScreener: new AI call"), "", b(headline(c)),
             f"Confidence {c['confidence']:.0%}, horizon {c['horizon_months']} months",
             f"Last close it saw: {close(c)}", "", c["summary"], "",
             *section("Reasons", c["reasons"]), *section("Buy when", c["buy_when"]),
             *section("Sell when", c["sell_when"]), *section("Risks", c["risks"]),
             *section("Data gaps", c["data_gaps"]),
             *([f"Brokers: {c['vs_brokers']}"] if c.get("vs_brokers") else []),
             f"Prompted by: {why(c)}",
             f"Record so far: {record_line(record, c['action'])}",
             f"Call {c['call_id']}, made {made:%d %b %Y %H:%M} IST", "",
             f"_{FOOTER}_" if bold else FOOTER]
    return "\n".join(lines)


def sample(where: str) -> dict[str, Any]:
    """A test call, laid out as a real one, to check a set-up."""
    return {"call_id": 0, "company_id": 0, "symbol": "EXAMPLE", "name": "Example Ltd",
            "action": "test", "previous": None, "confidence": 0.6, "horizon_months": 6,
            "summary": f"IndiaGrowthScreener can reach you on {where}. Each new buy or sell "
                       "call by the AI will come as a message like this one.",
            "reasons": ["the reasons the AI gives, with the figures behind them"],
            "risks": ["what could make the call wrong"],
            "buy_when": ["conditions the AI sets for buying"],
            "sell_when": ["conditions the AI sets for selling"], "data_gaps": [],
            "price_date": None, "price_close": None, "trigger": "manual", "reason": None,
            "created_at": dt.datetime.now(IST)}
