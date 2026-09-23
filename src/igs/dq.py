"""Data-quality issue log.

Missing quarters, restatements, taxonomy mismatches, unmapped ISINs and
unexplained price gaps are reported here, loudly, and never silently imputed.
Every issue is logged at WARNING or ERROR and kept in memory so callers
(and the reconciliation report) can show it. `persist` writes the batch to the
dq_issue table.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from igs.timeutil import utc_now

log = logging.getLogger("igs.dq")

Severity = Literal["info", "warn", "error"]
_LEVEL = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


@dataclass(frozen=True)
class DQIssue:
    severity: Severity
    category: str
    message: str
    source_id: str | None = None
    fetch_id: str | None = None
    security_id: int | None = None
    as_of_date: dt.date | None = None
    details: dict[str, Any] = field(default_factory=dict)
    detected_at: dt.datetime = field(default_factory=utc_now)


class DQLog:
    def __init__(self) -> None:
        self.issues: list[DQIssue] = []

    def emit(self, severity: Severity, category: str, message: str, **ctx: Any) -> DQIssue:
        details = ctx.pop("details", {}) or {}
        issue = DQIssue(severity=severity, category=category, message=message, details=details,
                        **ctx)
        self.issues.append(issue)
        log.log(_LEVEL[severity], "[DQ %s] %s: %s %s", severity.upper(), category, message,
                json.dumps(details, default=str) if details else "")
        return issue

    def count(self, severity: Severity | None = None) -> int:
        if severity is None:
            return len(self.issues)
        return sum(1 for i in self.issues if i.severity == severity)

    def persist(self, conn) -> int:
        """Insert all issues into dq_issue. Returns rows written."""
        if not self.issues:
            return 0
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into dq_issue
                    (detected_at, severity, category, message, source_id, fetch_id,
                     security_id, as_of_date, details)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (i.detected_at, i.severity, i.category, i.message, i.source_id, i.fetch_id,
                     i.security_id, i.as_of_date, json.dumps(i.details, default=str))
                    for i in self.issues
                ],
            )
        return len(self.issues)

    def as_rows(self) -> list[dict[str, Any]]:
        return [asdict(i) for i in self.issues]
