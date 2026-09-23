"""Rebalance calendars and horizon arithmetic on the trading calendar."""

from __future__ import annotations

import bisect
import datetime as dt


def add_months(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return dt.date(year, month, day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def monthly(days: list[dt.date], start: dt.date, end: dt.date) -> list[dt.date]:
    """Last trading day of each complete month within [start, end]: a day whose next
    trading day falls in a different month."""
    return [a for a, b in zip(days, days[1:], strict=False)
            if (a.year, a.month) != (b.year, b.month) and start <= a <= end]


def quarterly(days: list[dt.date], start: dt.date, end: dt.date, lag_days: int) -> list[dt.date]:
    """First trading day on or after each quarter end + lag_days."""
    out = []
    y = start.year - 1
    while True:
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            target = dt.date(y, m, d) + dt.timedelta(days=lag_days)
            if not days or target < days[0]:
                continue   # outside the calendar: never snap to its first day
            i = bisect.bisect_left(days, target)
            if i < len(days) and start <= days[i] <= end:
                out.append(days[i])
        y += 1
        if dt.date(y, 1, 1) > end:
            return sorted(set(out))


def next_trading_day(days: list[dt.date], d: dt.date) -> dt.date | None:
    i = bisect.bisect_right(days, d)
    return days[i] if i < len(days) else None


def on_or_before(days: list[dt.date], d: dt.date) -> dt.date | None:
    i = bisect.bisect_right(days, d)
    return days[i - 1] if i else None
