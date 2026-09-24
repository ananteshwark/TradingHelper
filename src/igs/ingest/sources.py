"""URL rendering for the source registry (config/sources.yaml)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from urllib.parse import quote

from igs.config import SourceSpec

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


class SourceNotReady(RuntimeError):
    pass


@dataclass(frozen=True)
class Paging:
    """How a `paged` source is walked (options first_page, page_size, max_pages). The
    values are what the exchange's own page was seen to request, not guesses."""
    first_page: int
    page_size: int
    max_pages: int


def paging(spec: SourceSpec) -> Paging:
    o = spec.options
    if spec.kind != "paged" or not {"first_page", "page_size", "max_pages"} <= set(o):
        raise ValueError(f"{spec.id}: a paged source needs first_page, page_size and max_pages")
    return Paging(int(o["first_page"]), int(o["page_size"]), int(o["max_pages"]))


def render_url(spec: SourceSpec, *, day: dt.date | None = None,
               start: dt.date | None = None, end: dt.date | None = None,
               symbol: str | None = None, page: int | None = None) -> str:
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
    elif spec.kind == "per_symbol":
        if not symbol:
            raise ValueError(f"{spec.id} needs a symbol")
        values = {"symbol": quote(symbol, safe="")}
    elif spec.kind == "paged":
        p = paging(spec)
        values = {"page": str(p.first_page if page is None else page), "size": str(p.page_size)}
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
