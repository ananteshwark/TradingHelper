"""Time helpers. Every timestamp in the system is timezone-aware.

Naive datetimes are rejected rather than guessed at: a filing stamped
"2024-05-14 18:05" means something different in IST and UTC, and on a
results day that difference decides whether the number was knowable at the
close.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = dt.UTC


def require_aware(ts: dt.datetime, what: str = "timestamp") -> dt.datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{what} must be timezone-aware, got naive {ts!r}")
    return ts


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def end_of_day_ist(day: dt.date, cutoff: dt.time = dt.time(23, 59, 59)) -> dt.datetime:
    """The as-of instant for signals formed on `day` (IST)."""
    return dt.datetime.combine(day, cutoff, tzinfo=IST)


def ist_date(ts: dt.datetime) -> dt.date:
    """Calendar date in India for an aware timestamp."""
    return require_aware(ts).astimezone(IST).date()
