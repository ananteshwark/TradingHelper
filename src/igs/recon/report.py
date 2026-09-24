"""Render reconciliation results as a Markdown report."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl

from igs.dq import DQLog
from igs.recon.checks import CheckResult, worst

_ICON = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}


def _table(df: pl.DataFrame, limit: int = 25) -> str:
    if df.height == 0:
        return "_no rows_\n"
    head = df.head(limit).with_columns(
        [pl.col(c).round(4) for c, t in df.schema.items() if t in (pl.Float32, pl.Float64)])
    cols = head.columns
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in head.iter_rows():
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    more = f"\n_{df.height - limit} more rows not shown_\n" if df.height > limit else "\n"
    return "\n".join(lines) + more


def render(results: list[CheckResult], dq: DQLog | None, title: str,
           generated_at: dt.datetime) -> str:
    overall = worst(results)
    out = [
        f"# {title}",
        "",
        f"Generated {generated_at.isoformat()} - overall: **{_ICON[overall]}**",
        "",
        "| check | status | summary |",
        "|---|---|---|",
    ]
    out += [f"| {r.name} | {_ICON[r.status]} | {r.summary} |" for r in results]
    for r in results:
        if r.details is not None and r.details.height and r.status != "pass":
            out += ["", f"## {r.name}", "", _table(r.details)]
    if dq is not None and dq.issues:
        out += ["", "## Data-quality issues", "",
                _table(pl.DataFrame([{"severity": i.severity, "category": i.category,
                                      "message": i.message} for i in dq.issues]), limit=200)]
    return "\n".join(out) + "\n"


def write(results: list[CheckResult], dq: DQLog | None, path: Path, title: str,
          generated_at: dt.datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(results, dq, title, generated_at))
    return path
