"""Questions about a score run, answered by Claude with read-only tools (tools.py).

The run is fixed when the question is asked, so every tool result describes the same
point-in-time snapshot, and the answer names it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from igs import service
from igs.assistant import prompts
from igs.assistant.llm import Assistant, echo_content, text_of
from igs.assistant.tools import TOOLS, Toolbox

SYSTEM = f"""\
You are the research assistant inside IndiaGrowthScreener. {prompts.TOOL}

You help its user understand one stored score run: why stocks rank where they do, what their \
factor scores, checks and filings say, and what the data cannot tell. Look things up with the \
tools; they read the stored results of that run and the filings public on its date. Answer \
from what they return. If the data doesn't answer the question, say so and say what would. \
Name the run and its date once; when you rely on a filing or announcement, give its date.

{prompts.LIMITS}

{prompts.STYLE}"""


@dataclass
class Answer:
    text: str
    run_id: int
    as_of: Any
    tool_calls: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    model: str = ""
    guarded: bool = False
    notes: list[str] = field(default_factory=list)


def ask(assistant: Assistant, question: str, run_id: int | None = None,
        history: list[dict] | None = None) -> Answer:
    """`history`: earlier turns of this conversation as [{"role", "content": str}], the
    user's questions and the assistant's shown answers."""
    run = service.resolve_run(assistant.conn, run_id)
    box = Toolbox(assistant.conn, run)
    out = Answer(text="", run_id=run["run_id"], as_of=run["as_of"])
    messages: list[dict] = [*(history or []), {
        "role": "user",
        "content": f"(Score run {run['run_id']}, as of {run['as_of']:%Y-%m-%d %H:%M} UTC.)"
                   f"\n\n{question.strip()}"}]
    rounds = assistant.cfg.features.ask.max_tool_rounds

    def call() -> Any:
        m = assistant.create("ask", system=SYSTEM, messages=messages, tools=TOOLS)
        out.cost_usd += assistant.cost(m)
        out.model = m.model
        return m

    message = call()
    for _ in range(rounds):
        if message.stop_reason not in ("tool_use", "pause_turn"):
            break
        messages.append({"role": "assistant", "content": echo_content(message)})
        if message.stop_reason == "tool_use":
            results = []
            for block in message.content:
                if block.type != "tool_use":
                    continue
                content, is_error = box.call(block.name, dict(block.input or {}))
                out.tool_calls.append({"tool": block.name, "input": dict(block.input or {}),
                                       "error": is_error})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
        message = call()
    else:
        if message.stop_reason == "tool_use":
            out.notes.append(f"stopped after {rounds} rounds of tool use "
                             "(Tool rounds per question in Settings)")

    text = text_of(message)
    if message.stop_reason == "max_tokens":
        out.notes.append("the answer was cut off at max_tokens")
    if not text:
        text = "The assistant did not produce an answer."

    def rewrite(instruction: str) -> str:
        messages.append({"role": "assistant", "content": echo_content(message)})
        messages.append({"role": "user", "content": instruction})
        return text_of(call())

    out.text, out.guarded = prompts.guarded(text, rewrite)
    return out
