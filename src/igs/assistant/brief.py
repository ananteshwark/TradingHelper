"""A plain-language brief of one stock's result in a run, written from the stored data.

Briefs are stored per run, stock and prompt version, so opening a stock twice doesn't pay
twice; a new run or a new prompt version writes a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from igs import service
from igs.assistant import prompts
from igs.assistant.llm import Assistant, echo_content, text_of
from igs.assistant.tools import Toolbox, to_json

PROMPT_VERSION = "brief-v1"

SYSTEM = f"""\
You write short research briefs inside IndiaGrowthScreener. {prompts.TOOL}

You are given one stock's stored result from one score run, with its factor table, recent \
quarters and filings. Write a brief for someone deciding whether the stock deserves a closer \
look at its filings. Use these headings, in bold, with two to four sentences each:
**Where it stands** (tier, rank and the main reason), **What lifts the score**, \
**What holds it back**, **Checks and data gaps** (tripped checks, checks that couldn't be \
evaluated, robustness), **Worth reading in the filings** (specific filings or announcements \
by date, and what to look for in them).
Use only the data given. At most 250 words.

{prompts.LIMITS}"""


@dataclass
class Brief:
    symbol: str
    run_id: int
    text: str
    model: str
    cached: bool
    cost_usd: float = 0.0


def brief(assistant: Assistant, symbol: str, run_id: int | None = None,
          refresh: bool = False) -> Brief:
    conn = assistant.conn
    run = service.resolve_run(conn, run_id)
    box = Toolbox(conn, run)
    detail = box.stock_detail(symbol)           # NotFound if not in the run
    sym = detail["stock"]["symbol"]
    if not refresh:
        with conn.cursor() as cur:
            cur.execute("""select text, model from assistant_brief
                           where run_id = %s and symbol = %s and prompt_version = %s""",
                        (run["run_id"], sym, PROMPT_VERSION))
            row = cur.fetchone()
        if row:
            return Brief(sym, run["run_id"], row[0], row[1], cached=True)

    data = {"result": detail, "factors": box.factor_table(sym)["factors"],
            "quarters": box.financials(sym)["quarters"],
            "filings": box.filings(sym, 10)}
    messages: list[dict] = [{"role": "user", "content": (
        f"Score run {run['run_id']}, as of {run['as_of']:%Y-%m-%d}. Stored data for {sym}:\n"
        f"<data>\n{to_json(data)}\n</data>\nWrite the brief.")}]
    cost = 0.0

    def call() -> Any:
        nonlocal cost
        m = assistant.create("brief", system=SYSTEM, messages=messages)
        cost += assistant.cost(m)
        return m

    message = call()

    def rewrite(instruction: str) -> str:
        messages.append({"role": "assistant", "content": echo_content(message)})
        messages.append({"role": "user", "content": instruction})
        return text_of(call())

    text, _ = prompts.guarded(text_of(message) or "No brief was produced.", rewrite)
    with conn.cursor() as cur:
        cur.execute("""insert into assistant_brief (run_id, symbol, prompt_version, model, text)
                       values (%s, %s, %s, %s, %s)
                       on conflict (run_id, symbol, prompt_version)
                       do update set text = excluded.text, model = excluded.model,
                                     created_at = now()""",
                    (run["run_id"], sym, PROMPT_VERSION, message.model, text))
    if not conn.autocommit:
        conn.commit()
    return Brief(sym, run["run_id"], text, message.model, cached=False, cost_usd=cost)
