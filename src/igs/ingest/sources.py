"""URL rendering for the source registry (config/sources.yaml)."""

from __future__ import annotations

import datetime as dt

from igs.config import SourceSpec

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


class SourceNotReady(RuntimeError):
    pass


def render_url(spec: SourceSpec, *, day: dt.date | None = None,
               start: dt.date | None = None, end: dt.date | None = None) -> str:
    if spec.url is None:
        raise SourceNotReady(f"{spec.id}: endpoint not yet discovered (url is null)")
    values: dict[str, str] = {}
    if spec.kind == "date_file":
        if day is None:
            raise ValueError(f"{spec.id} needs a date")
        values = {
            "yyyy": f"{day:%Y}", "mm": f"{day:%m}", "dd": f"{day:%d}",
            "MON": _MONTHS[day.month - 1],
            "yyyymmdd": f"{day:%Y%m%d}", "ddmmyyyy": f"{day:%d%m%Y}",
        }
    elif spec.kind == "date_range":
        if start is None or end is None:
            raise ValueError(f"{spec.id} needs start and end dates")
        values = {"from_dd_mm_yyyy": f"{start:%d-%m-%Y}", "to_dd_mm_yyyy": f"{end:%d-%m-%Y}"}
    return spec.url.format(**values)


def recent_weekdays(today: dt.date, n: int = 10) -> list[dt.date]:
    """Most recent weekdays strictly before today, newest first (holidays are skipped by
    trying the next one)."""
    out: list[dt.date] = []
    d = today
    while len(out) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out
