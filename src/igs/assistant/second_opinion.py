"""A second opinion on an AI call from another model of the user's choice (the stock page,
the AI calls page, `igs assistant second-opinion`).

The other model gets everything the call's model got, in full:
- the same instructions (calls.system);
- why the call was asked, the previous call and the last buy as they stood then (stored
  with the call; rebuilt from the calls before it for calls made before they were kept);
- every data section the app gathered at the call's run date (the call's stored inputs),
  without the 30,000-character cut the call's own prompt applies to each section.

It answers in two steps of one conversation, so its own view comes before it sees the
call:
1. its own buy / hold / sell call, in the call's own format;
2. its review of the call against the data: agree, partly agree or disagree, with what the
   data supports, what it contradicts, what the call missed, and where it breaks the call
   instructions.
Both are stored (ai_call_second_opinion) and never changed.
"""
from __future__ import annotations

import json
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from igs.assistant import calls as ai
from igs.assistant import catalog
from igs.assistant.errors import AssistantUnavailable
from igs.assistant.llm import Assistant, AssistantError, echo_content
from igs.assistant.providers import PROVIDERS, credentials
from igs.assistant.tools import MAX_RESULT_CHARS, _default
from igs.config import ModelRoute

PROMPT_VERSION = "second-opinion-v1"
MAX_ITEMS = 8           # each review list, as asked
KEEP_ITEMS = 20         # as kept, when a model gives more than it was asked for
MIN_ITEM_CHARS = 5

REVIEW = """\
Another AI was asked for a call on this stock with exactly the instructions and data you \
were given, and made the call below. Review it against the data, as an independent \
analyst: you are not defending your own call and not deferring to it.

- verdict: "agree" (the action is right and the reasoning holds), "partly agree" (the \
action is defensible but the reasoning, target, timing or confidence is not), or \
"disagree" (the data points to a different action).
- summary: two or three sentences the user can act on: what to make of the call.
- supported: what in the call the data supports, citing figures.
- errors: statements in the call that the data contradicts or doesn't contain, each with \
the figure from the data that shows it. Not matters of judgement.
- missed: facts in the data that would change or qualify the call and that it didn't \
weigh, with figures.
- rule_issues: where the call breaks its instructions (for example a buy whose target is \
below the minimum a year, "before the move" called from the chart alone, a tripped red \
flag or a rising pledge not weighed, a broker's target adopted, remembered facts used).
- confidence: from 0 to 1, how sure you are of this review given the data.
Each list at most 8 items, the most important first; empty where there is nothing to \
say. Use only the data given."""


Item = ai.Item


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["agree", "partly agree", "disagree"]
    summary: str = Field(min_length=20, max_length=1500)
    supported: list[Item] = Field(max_length=KEEP_ITEMS)
    errors: list[Item] = Field(max_length=KEEP_ITEMS)
    missed: list[Item] = Field(max_length=KEEP_ITEMS)
    rule_issues: list[Item] = Field(max_length=KEEP_ITEMS)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


def _cut(text: str, most: int) -> str:
    text = text.strip()
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


def fit(parsed: Any, model: type[BaseModel]) -> Any:
    """`parsed` cut to `model`'s length limits: each list to its most items, each text to
    its most characters, and list entries too short to say anything dropped. The schema
    sent leaves the limits out (calls.schema_of), so a model other than the one the
    limits were written for can give more; what it gave is cut, not refused."""
    if not isinstance(parsed, dict):
        return parsed
    item_chars = ai.Item.__metadata__[0].max_length
    out = dict(parsed)
    for name, f in model.model_fields.items():
        value = out.get(name)
        most = next((m.max_length for m in f.metadata
                     if getattr(m, "max_length", None) is not None), None)
        if isinstance(value, list):
            value = [_cut(v, item_chars) if isinstance(v, str) else v for v in value]
            value = [v for v in value if not isinstance(v, str) or len(v) >= MIN_ITEM_CHARS]
            out[name] = value[:most] if most is not None else value
        elif isinstance(value, str) and most is not None:
            out[name] = _cut(value, most)
    return out


def _call(conn, call_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("""select c.*, sr.as_of from ai_call c join score_run sr using (run_id)
                       where c.call_id = %s""", (call_id,))
        row = cur.fetchone()
        if row is None:
            raise AssistantError(f"no AI call {call_id}")
        return dict(zip([d.name for d in cur.description], row, strict=True))


def context_of(conn, call: dict) -> tuple[dict, str]:
    """(why it was asked, the previous call, the last buy) as the call's model saw them, and
    whether they were 'stored' with the call or 'rebuilt' from the calls before it."""
    if call.get("context"):
        stored = call["context"]
        return {k: stored.get(k) for k in ai.CONTEXT_SECTIONS}, "stored"
    before = call["created_at"]
    return ({"why_now": call.get("reason") or "asked for a call",
             "previous_call": ai.previous_call(conn, call["company_id"], before),
             "last_buy": ai.last_buy(conn, call["company_id"], before)}, "rebuilt")


def sections_of(conn, call: dict) -> tuple[dict, str, list[str]]:
    """Every section of the call's prompt in its order, whether its context was stored or
    rebuilt, and the sections the call's own prompt cut at MAX_RESULT_CHARS."""
    context, source = context_of(conn, call)
    inputs = call["inputs"] or {}
    sections = {**context, **{k: inputs.get(k) for k in ai.DATA_SECTIONS}}
    sections.update({k: v for k, v in inputs.items() if k not in sections})
    cut = [k for k, v in sections.items() if v is not None and len(json.dumps(
        v, default=_default, ensure_ascii=False, separators=(",", ":"))) > MAX_RESULT_CHARS]
    return sections, source, cut


def shown_call(conn, call: dict) -> dict:
    """The call as the reviewer is shown it: everything the model answered, and how it was
    recorded."""
    out = {k: call.get(k) for k in ("action", "confidence", "horizon_months", "stage",
                                    "target_price", "target_pct_a_year", "summary",
                                    "reasons", "risks", "buy_when", "sell_when", "data_gaps",
                                    "vs_growth", "vs_brokers")}
    if call.get("ai_action"):
        out["action"] = call["ai_action"]
        out["recorded_as"] = call["action"]
        out["why_recorded_differently"] = ai.rule_note(call)
    out["broker_verdicts"] = [{k: v[k] for k in ("broker", "rating", "called_on", "verdict",
                                                 "reason", "confidence")}
                              for v in ai.verdicts_for(conn, call["call_id"])]
    return out


def second_opinion(assistant: Assistant, call_id: int, route: ModelRoute) -> dict:
    """Ask `route`'s model for its own call on the call's context and data, then its review
    of the call; store both and return them."""
    conn = assistant.conn
    call = _call(conn, call_id)
    sections, source, cut = sections_of(conn, call)
    run = {"run_id": call["run_id"], "as_of": call["as_of"]}
    prompt = ai.prompt_for(run, call["symbol"], sections, full=True)
    minimum = assistant.cfg.features.call.min_buy_target_pct_a_year
    system = ai.system(minimum)
    first = [{"role": "user", "content": prompt}]
    parsed, message = assistant.structured("second_opinion", system=system, messages=first,
                                           schema=ai.output_schema(), route=route)
    try:
        own = ai.Call.model_validate(fit(parsed, ai.Call))
    except ValidationError as exc:
        raise AssistantError(f"the other AI's own call was incomplete or out of range: "
                             f"{exc.error_count()} problems, first: "
                             f"{exc.errors()[0]['msg']}") from exc
    cost = assistant.cost(message)
    note = ""
    if cut:
        note = ("\nIts prompt cut these sections at 30,000 characters, so it saw less of "
                "them than you did; don't count what it couldn't see as its error, and say "
                "so where it matters: " + ", ".join(cut) + ".")
    review_prompt = (f"{REVIEW}{note}\n<call>\n"
                     f"{json.dumps(shown_call(conn, call), default=_default, ensure_ascii=False)}"
                     "\n</call>")
    conversation = first + [{"role": "assistant", "content": echo_content(message)},
                            {"role": "user", "content": review_prompt}]
    parsed, reply = assistant.structured("second_opinion", system=system, messages=conversation,
                                         schema=ai.schema_of(Review), route=route)
    try:
        review = Review.model_validate(fit(parsed, Review))
    except ValidationError as exc:
        raise AssistantError(f"the other AI's review was incomplete or out of range: "
                             f"{exc.error_count()} problems, first: "
                             f"{exc.errors()[0]['msg']}") from exc
    cost += assistant.cost(reply)
    base = call.get("entry_price") or call.get("price_close")
    pct = ai.pct_a_year(own.target_price or None, base, own.horizon_months)
    values = {
        "call_id": call_id, "provider": route.provider, "model": reply.model,
        "context_source": source, "instructions": f"{ai.PROMPT_VERSION}/{PROMPT_VERSION}",
        "cut_sections": json.dumps(cut), "own_action": own.action,
        "own_confidence": own.confidence, "own_horizon_months": own.horizon_months,
        "own_stage": own.stage, "own_target_price": own.target_price or None,
        "own_target_pct_a_year": pct, "own_call": json.dumps(own.model_dump()),
        "verdict": review.verdict, "review_summary": review.summary,
        "supported": json.dumps(review.supported), "errors": json.dumps(review.errors),
        "missed": json.dumps(review.missed), "rule_issues": json.dumps(review.rule_issues),
        "review_confidence": review.confidence, "cost_usd": round(cost, 6)}
    with conn.cursor() as cur:
        cur.execute(f"""insert into ai_call_second_opinion ({', '.join(values)})
                        values ({', '.join(['%s'] * len(values))})
                        returning opinion_id, created_at""", list(values.values()))
        vid, created = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return {**values, "opinion_id": vid, "created_at": created, "symbol": call["symbol"],
            "call_action": call["action"], "own": own.model_dump(),
            **{k: getattr(review, k) for k in ("supported", "errors", "missed",
                                               "rule_issues")}}


def for_call(conn, call_id: int) -> list[dict]:
    """The call's second opinions, newest first."""
    with conn.cursor() as cur:
        cur.execute("""select * from ai_call_second_opinion where call_id = %s
                       order by created_at desc""", (call_id,))
        return [dict(zip([d.name for d in cur.description], r, strict=True))
                for r in cur.fetchall()]


def models(cfg) -> list[str]:
    """'provider:model' choices that can run now: a key configured (or the local model)
    and a price known. Claude models need no key here: the SDK also finds a login profile
    when the request is made, as for every other AI feature."""
    found = set()
    for provider, entry in catalog.read().items():
        found.update(f"{provider}:{m['id']}" for m in entry.get("models", []))
    found.update(m if ":" in m else f"anthropic:{m}" for m in cfg.prices_usd_per_mtok)
    found.update(f"{r.provider}:{r.model}" for r in cfg.routes.values())
    if os.environ.get(PROVIDERS["local"][1]):
        found.add("local:strata-auto")
    out = []
    for choice in sorted(found):
        provider, _, model = choice.partition(":")
        if provider not in PROVIDERS:
            continue
        try:
            route = ModelRoute(provider=provider, model=model)
            if provider != "anthropic":
                credentials(provider)
        except (ValueError, AssistantUnavailable):
            continue
        if cfg.price_for(route) is not None:
            out.append(choice)
    return out


def route_of(choice: str) -> ModelRoute:
    provider, sep, model = choice.partition(":")
    if not sep:
        raise ValueError("choose a model as provider:model-id")
    return ModelRoute(provider=provider, model=model)


def estimate(cfg, route: ModelRoute, conn, call_id: int) -> float | None:
    """Roughly what a second opinion costs (USD): the prompt about twice, plus answers."""
    price = cfg.price_for(route)
    if price is None:
        return None
    sections, _, _ = sections_of(conn, _call(conn, call_id))
    tokens = len(json.dumps(sections, default=_default)) / 3.5
    return (2 * tokens * price.input + 6000 * price.output) / 1e6

