"""What is left on the AI providers' accounts.

- Anthropic has no API for the credit balance (the Usage and Cost Admin API reports spend,
  not what is left, and needs an organization's Admin key). The owner enters the balance
  shown on the Claude Console's Billing page; the app subtracts the spend it has logged
  since then (llm_call, at its own price estimates). Other uses of the same key, such as
  other apps or the Console's own tools, aren't counted, so re-enter it now and then.
- DeepSeek: GET /user/balance with the normal API key.
- OpenRouter: GET /credits gives the account's credits and usage, but only to a management
  key; with a normal key, GET /key gives that key's own limit and what is left of it.
"""
from __future__ import annotations

import os

from igs.assistant.errors import AssistantError, AssistantUnavailable
from igs.assistant.providers import PROVIDERS, request

LIVE = ("deepseek", "openrouter")       # providers whose balance an API returns


def entered(conn, provider: str = "anthropic") -> dict | None:
    row = conn.execute("""select balance_usd::float8, as_of from llm_balance
                          where provider = %s order by as_of desc, balance_id desc limit 1""",
                       (provider,)).fetchone()
    return None if row is None else {"balance_usd": row[0], "as_of": row[1]}


def record(conn, balance_usd: float, provider: str = "anthropic") -> None:
    if balance_usd < 0:
        raise ValueError("a balance can't be negative")
    conn.execute("insert into llm_balance (provider, balance_usd) values (%s, %s)",
                 (provider, round(balance_usd, 2)))
    if not conn.autocommit:
        conn.commit()


def estimate(conn, provider: str = "anthropic") -> dict | None:
    """The entered balance less the spend logged since: {entered, as_of, spent, left}."""
    base = entered(conn, provider)
    if base is None:
        return None
    spent = conn.execute("""select coalesce(sum(cost_usd), 0)::float8 from llm_call
                            where provider = %s and called_at >= %s""",
                         (provider, base["as_of"])).fetchone()[0]
    return {"entered": base["balance_usd"], "as_of": base["as_of"], "spent": spent,
            "left": base["balance_usd"] - spent}


def deepseek(client=None) -> dict:
    """{'available': can the account make calls, 'balances': [{currency, total, granted,
    topped_up}]}."""
    data = request("deepseek", "GET", "/user/balance", client=client)
    return {"available": bool(data.get("is_available")),
            "balances": [{"currency": b.get("currency"),
                          "total": float(b.get("total_balance") or 0),
                          "granted": float(b.get("granted_balance") or 0),
                          "topped_up": float(b.get("topped_up_balance") or 0)}
                         for b in data.get("balance_infos") or []]}


def openrouter(client=None) -> dict:
    """The account's credits with a management key ({'scope': 'account', credits, used,
    left}); else the key's own limit ({'scope': 'key', limit, used, left}, limit and left
    None when the key has no cap)."""
    try:
        data = request("openrouter", "GET", "/credits", client=client)["data"]
        credits, used = float(data["total_credits"]), float(data["total_usage"])
        return {"scope": "account", "credits": credits, "used": used, "left": credits - used}
    except AssistantUnavailable as exc:
        if "HTTP 403" not in str(exc):
            raise
    data = request("openrouter", "GET", "/key", client=client)["data"]
    return {"scope": "key", "limit": data.get("limit"), "used": data.get("usage"),
            "left": data.get("limit_remaining")}


def configured(provider: str) -> bool:
    return bool(os.environ.get(PROVIDERS[provider][1]))


def live(client=None) -> dict[str, dict | str]:
    """Each LIVE provider with a key: its balance, or why it couldn't be read."""
    out: dict[str, dict | str] = {}
    for provider, fetch in (("deepseek", deepseek), ("openrouter", openrouter)):
        if not configured(provider):
            continue
        try:
            out[provider] = fetch(client)
        except (AssistantError, AssistantUnavailable, KeyError, TypeError, ValueError) as exc:
            out[provider] = str(exc) or type(exc).__name__
    return out


def lines(conn, client=None) -> list[str]:
    """The balances in words, for the CLI."""
    out = []
    est = estimate(conn)
    if est is None:
        out.append("Anthropic: no balance entered (Settings > AI assistant); the API has none")
    else:
        out.append(f"Anthropic: about ${est['left']:,.2f} left (${est['entered']:,.2f} entered "
                   f"{est['as_of']:%d %b %Y %H:%M}, ${est['spent']:,.2f} logged since)")
    for provider, b in live(client).items():
        name = PROVIDERS[provider][0]
        if isinstance(b, str):
            out.append(f"{name}: not read ({b})")
        elif provider == "deepseek":
            out.append(f"{name}: " + (", ".join(f"{x['total']:,.2f} {x['currency']}"
                                               for x in b["balances"]) or "no balance")
                       + ("" if b["available"] else " (not enough for calls)"))
        elif b["scope"] == "account":
            out.append(f"{name}: ${b['left']:,.2f} left of ${b['credits']:,.2f}")
        else:
            out.append(f"{name}: this key " + (f"has ${b['left']:,.2f} left of its "
                                               f"${b['limit']:,.2f} limit"
                                               if b["limit"] is not None else
                                               "has no limit") + f"; ${b['used'] or 0:,.2f} "
                       "used. The account balance needs a management key.")
    return out
