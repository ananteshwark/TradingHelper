"""The Telegram message about each new AI buy or sell call (igs.alerts.delivery): the call
and, briefly, why. Which calls get one is set under `call_messages` in config/alerts.yaml."""

from __future__ import annotations

import datetime as dt
from typing import Any

from igs.config import CallMessagesConfig
from igs.timeutil import IST


def call(conn, call_id: int) -> dict | None:
    """One AI call with the company's name and the action of the call before it."""
    with conn.cursor() as cur:
        cur.execute("""select c.call_id, c.company_id, c.symbol, co.name, c.action,
                              c.confidence, c.horizon_months, c.summary, c.reasons, c.risks,
                              c.buy_when, c.sell_when, c.data_gaps, c.price_date,
                              c.price_close, c.trigger, c.reason, c.created_at,
                              c.vs_brokers, c.entry_source, c.entry_date, c.entry_price,
                              c.entry_at, c.target_price, c.target_pct_a_year, c.ai_action,
                              c.stage, c.vs_growth,
                              (select json_build_object('abnormal_pct', r.abnormal_pct::float8,
                                                        'after_date', r.after_date,
                                                        'flag_until', r.flag_until)
                               from results_reaction r
                               where r.company_id = c.company_id and r.abnormal_pct <= -5
                                 and r.decided_at <= c.created_at
                                 and (c.created_at at time zone 'Asia/Kolkata')::date
                                     between r.after_date and r.flag_until
                               order by r.after_date desc limit 1) as bad_results,
                              (select p.action from ai_call p
                               where p.company_id = c.company_id
                                 and p.created_at < c.created_at
                               order by p.created_at desc limit 1) as previous
                       from ai_call c join company co using (company_id)
                       where c.call_id = %s""", (call_id,))
        row = cur.fetchone()
        names = [d.name for d in cur.description]
    if row is None:
        return None
    from igs.assistant.calls import entry_text
    c = dict(zip(names, row, strict=True))
    return {**c, "entry": entry_text(conn, c)}


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


def headline(c: dict) -> str:
    if c["action"] == "test":
        return "TEST message, not a real call"
    was = (f", was {c['previous']}" if c["previous"] else ", its first call on this stock")
    return f"{c['action'].upper()} {c['name']} ({c['symbol']}){was}"


def close(c: dict) -> str:
    """The last close the AI saw."""
    if c["price_close"] is None:
        return "not available"
    return f"Rs {c['price_close']:,.2f} on {c['price_date']:%d %b %Y}"


def entry(c: dict) -> str:
    """The price the call's record is measured from (igs.assistant.calls.entry_text)."""
    return c.get("entry") or close(c)


def target(c: dict) -> str | None:
    """A buy's target and what it comes to a year (igs.assistant.calls.target_text)."""
    from igs.assistant.calls import target_text
    return target_text(c)


def results_note(c: dict) -> str | None:
    """The bad-results flag (igs.results_drift) on the call's stock when it was made."""
    f = c.get("bad_results")
    if not f:
        return None
    return (f"Note: results moved this stock {f['abnormal_pct']:+.1f}% against the market "
            f"(session of {dt.date.fromisoformat(str(f['after_date'])):%d %b}). In the "
            "backtest, stocks after a fall like that lagged the market by about 0.9% over the "
            "next month. A paper flag, for the record: nothing is skipped.")


BRIEF_FOOTER = "The AI's judgement, not investment advice. Details on the AI calls page."
BRIEF_REASONS = 3


def _short(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def brief_message(c: dict) -> str:
    """The call and, briefly, why: for Telegram, which gets only buy and sell calls."""
    return "\n".join([
        headline(c),
        f"Confidence {c['confidence']:.0%} · {c['horizon_months']} months · "
        + (f"{c['stage']} · " if c.get("stage") else "") + f"entry {entry(c)}",
        *([f"Target {target(c)}"] if target(c) else []), "",
        _short(c["summary"], 400), "",
        "Why:", *[f"• {_short(r, 180)}" for r in c["reasons"][:BRIEF_REASONS]], "",
        *([results_note(c), ""] if results_note(c) else []),
        BRIEF_FOOTER])


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
