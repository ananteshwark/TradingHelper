"""Read new exchange announcements and note what each one is.

For announcements by companies in the latest run's universe (or on the watchlist), the
assistant records a category, a materiality level, a one-sentence factual summary and any
governance concerns (auditor or key-person resignation, default, pledge, ...). The notes are
shown next to the announcements and can raise alerts for watchlist names; they are never
used in scoring. Requests carry up to `batch_size` announcements and are constrained to a
JSON schema.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ValidationError

from igs.assistant import prompts
from igs.assistant.llm import Assistant, AssistantError
from igs.assistant.tools import to_json
from igs.guardrails import find_advice_language
from igs.timeutil import utc_now

PROMPT_VERSION = "announcements-v1"
MAX_TEXT_CHARS = 2000

CATEGORIES = [
    "financial_results", "board_meeting", "dividend_or_corporate_action",
    "capital_raise_or_dilution", "debt_or_credit_rating", "order_or_contract",
    "acquisition_merger_or_divestment", "management_or_board_change", "auditor_change",
    "pledge_or_encumbrance", "litigation_or_regulatory", "default_or_delay",
    "shareholder_meeting", "routine_compliance", "other",
]
CONCERNS = [
    "auditor_resignation", "key_managerial_resignation", "pledge_or_encumbrance",
    "default_or_delayed_payment", "litigation_or_regulatory_action",
    "audit_qualification_or_restatement", "related_party_transaction", "large_dilution",
    "credit_rating_downgrade", "fraud_or_forensic_audit",
]

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "category": {"type": "string", "enum": CATEGORIES},
            "materiality": {"type": "string", "enum": ["low", "medium", "high"]},
            "summary": {"type": "string"},
            "concerns": {"type": "array", "items": {"type": "string", "enum": CONCERNS}},
        },
        "required": ["index", "category", "materiality", "summary", "concerns"],
        "additionalProperties": False}}},
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM = f"""\
You read corporate announcements filed with the National Stock Exchange of India for \
IndiaGrowthScreener, a personal research tool. For each announcement given, return:
- category: what kind of announcement it is;
- materiality: "high" if it could change how an investor judges the company's governance, \
solvency or earnings (a resignation of the auditor or a key managerial person, a default or \
delayed payment, fraud or a forensic audit, a large dilution, major litigation or regulatory \
action, a credit-rating downgrade, a large acquisition or divestment); "medium" for a notable \
business event (a sizeable order, a fund raise, results, a rating action without downgrade); \
"low" for routine compliance and procedural filings;
- summary: one factual sentence of at most 30 words saying what was announced, with any \
amounts and dates given. No opinion and no view on the stock;
- concerns: the governance concerns the announcement itself states, or an empty list.
Judge only from the text given; if it is too thin to tell, say so in the summary and use \
"other" and "low". Return one item per announcement, with its index.

{prompts.LIMITS}"""


class Note(BaseModel):
    index: int
    category: Literal[tuple(CATEGORIES)]                       # type: ignore[valid-type]
    materiality: Literal["low", "medium", "high"]
    summary: str
    concerns: list[Literal[tuple(CONCERNS)]]                   # type: ignore[valid-type]


@dataclass
class ReadResult:
    read: int = 0
    stored: int = 0
    cost_usd: float = 0.0
    issues: list[str] = field(default_factory=list)


def pending(conn, days: int, scope: str, limit: int) -> list[dict]:
    """Announcements from the last `days` days without a note, newest first, for companies
    in the latest run (scope 'universe') or on the watchlist."""
    since = utc_now() - dt.timedelta(days=days)
    in_scope = ("s.company_id in (select company_id from watchlist)" if scope == "watchlist"
                else "s.company_id in (select company_id from watchlist union "
                     "select company_id from score_result where run_id = "
                     "(select max(run_id) from score_run))")
    with conn.cursor() as cur:
        cur.execute(f"""
            select a.exchange, a.symbol, a.filed_at, a.subject, a.category, a.body, c.name
            from announcement a
            join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = a.symbol
             and a.filed_at::date >= si.valid_from
             and (si.valid_to is null or a.filed_at::date < si.valid_to)
            join security s on s.security_id = si.security_id
            join company c on c.company_id = s.company_id
            where a.filed_at >= %s and {in_scope}
              and not exists (select 1 from announcement_note n
                              where n.exchange = a.exchange and n.symbol = a.symbol
                                and n.filed_at = a.filed_at and n.subject = a.subject)
            order by a.filed_at desc limit %s""", (since, limit))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _prompt(batch: list[dict]) -> str:
    items = [{"index": i, "symbol": a["symbol"], "company": a["name"],
              "filed_at": a["filed_at"], "nse_category": a["category"],
              "subject": a["subject"][:MAX_TEXT_CHARS],
              "text": (a["body"] or "")[:MAX_TEXT_CHARS]} for i, a in enumerate(batch)]
    return f"<announcements>\n{to_json(items)}\n</announcements>"


def _store(conn, a: dict, note: Note, model: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""insert into announcement_note (exchange, symbol, filed_at, subject,
                           category, materiality, summary, concerns, model, prompt_version)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       on conflict do nothing""",
                    (a["exchange"], a["symbol"], a["filed_at"], a["subject"], note.category,
                     note.materiality, note.summary.strip(), sorted(set(note.concerns)),
                     model, PROMPT_VERSION))


def read_new(assistant: Assistant, days: int | None = None,
             limit: int | None = None) -> ReadResult:
    f = assistant.cfg.features.announcements
    todo = pending(assistant.conn, days or f.days, f.scope, limit or f.max_per_run)
    out = ReadResult()
    for start in range(0, len(todo), f.batch_size):
        batch = todo[start:start + f.batch_size]
        out.read += len(batch)
        try:
            data, message = assistant.structured("announcements", system=SYSTEM,
                                                 prompt=_prompt(batch), schema=SCHEMA)
        except AssistantError as exc:
            out.issues.append(f"batch of {len(batch)} not read: {exc}")
            continue
        out.cost_usd += assistant.cost(message)
        model = message.model
        seen: set[int] = set()
        for raw in data.get("items", []):
            try:
                note = Note.model_validate(raw)
            except ValidationError as exc:
                out.issues.append(f"invalid note skipped: {exc.errors()[0]['msg']}")
                continue
            if not 0 <= note.index < len(batch) or note.index in seen:
                out.issues.append(f"note with index {note.index} matches no announcement")
                continue
            seen.add(note.index)
            a = batch[note.index]
            if find_advice_language(note.summary):
                out.issues.append(f"{a['symbol']} {a['filed_at']:%Y-%m-%d}: summary withheld "
                                  "(read as a recommendation)")
                continue
            _store(assistant.conn, a, note, model)
            out.stored += 1
        missing = len(batch) - len(seen)
        if missing:
            out.issues.append(f"{missing} announcement(s) got no note; retried next run")
        if not assistant.conn.autocommit:
            assistant.conn.commit()
    return out
