"""Minimal .xlsx workbooks for tests, written part by part as Office Open XML lays them out
(strings shared or inline, numbers, booleans, dates as serial numbers)."""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from typing import Any
from xml.sax.saxutils import escape

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def serial(day: dt.date, date1904: bool = False) -> float:
    base = dt.date(1904, 1, 1) if date1904 else dt.date(1899, 12, 30)
    return float((day - base).days)


def workbook(sheets: dict[str, list[list[Any]]], *, inline: bool = False,
             date1904: bool = False) -> bytes:
    """Cells: str, int/float, bool, dt.date (written as a serial number) or None."""
    strings: list[str] = []

    def cell(ref: str, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, bool):
            return f'<c r="{ref}" t="b"><v>{int(v)}</v></c>'
        if isinstance(v, dt.date):
            return f'<c r="{ref}" s="1"><v>{serial(v, date1904):g}</v></c>'
        if isinstance(v, int | float):
            return f'<c r="{ref}"><v>{v}</v></c>'
        if inline:
            return f'<c r="{ref}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'
        if v not in strings:
            strings.append(v)
        return f'<c r="{ref}" t="s"><v>{strings.index(v)}</v></c>'

    parts: dict[str, str] = {}
    for n, rows in enumerate(sheets.values(), start=1):
        body = "".join(
            f'<row r="{r}">' + "".join(cell(f"{_col(c)}{r}", v) for c, v in enumerate(row))
            + "</row>" for r, row in enumerate(rows, start=1) if row)
        parts[f"xl/worksheets/sheet{n}.xml"] = (
            f'<worksheet xmlns="{MAIN}"><sheetData>{body}</sheetData></worksheet>')
    book = "".join(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>'
                   for n, name in enumerate(sheets, start=1))
    parts["xl/workbook.xml"] = (
        f'<workbook xmlns="{MAIN}" xmlns:r="{REL}">'
        + ('<workbookPr date1904="1"/>' if date1904 else "")
        + f"<sheets>{book}</sheets></workbook>")
    rels = "".join(
        f'<Relationship Id="rId{n}" Type="{REL}/worksheet" Target="worksheets/sheet{n}.xml"/>'
        for n in range(1, len(sheets) + 1))
    parts["xl/_rels/workbook.xml.rels"] = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>")
    if strings:
        parts["xl/sharedStrings.xml"] = (
            f'<sst xmlns="{MAIN}">' + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings)
            + "</sst>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, xml in parts.items():
            z.writestr(name, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + xml)
    return buf.getvalue()


def screener_export(name: str, quarters: dict[dt.date, tuple[float, float]],
                    years: dict[dt.date, tuple[float, float]] | None = None,
                    price: float = 100.0) -> bytes:
    """A Screener.in "Export to Excel" workbook laid out as its Data Sheet tab is
    documented: company name, META, then PROFIT & LOSS, Quarters, BALANCE SHEET and CASH
    FLOW sections with a Report Date row each, PRICE: on the annual dates and DERIVED:.
    `quarters` and `years`: {period end: (sales, net profit)} in Rs crore."""
    years = years or {}
    ys, qs = sorted(years), sorted(quarters)
    blank: list[Any] = []
    rows: list[list[Any]] = [
        ["COMPANY NAME", name], ["LATEST VERSION", 0.8], ["CURRENT VERSION", 0.8], blank,
        ["META"], ["Number of shares", 1.0e8], ["Face Value", 10.0],
        ["Current Price", price], ["Market Capitalization", price], blank,
        ["PROFIT & LOSS"], ["Report Date", *ys],
        ["Sales", *(years[y][0] for y in ys)],
        ["Raw Material Cost", *(round(years[y][0] * 0.5, 2) for y in ys)],
        ["Profit before tax", *(round(years[y][1] / 0.75, 2) for y in ys)],
        ["Net profit", *(years[y][1] for y in ys)], blank,
        ["Quarters"], ["Report Date", *qs],
        ["Sales", *(quarters[q][0] for q in qs)],
        ["Expenses", *(round(quarters[q][0] * 0.8, 2) for q in qs)],
        ["Net profit", *(quarters[q][1] for q in qs)], blank,
        ["BALANCE SHEET"], ["Report Date", *ys],
        ["Equity Share Capital", *(100.0 for _ in ys)], ["Total", *(900.0 for _ in ys)],
        ["Net Block", *(400.0 for _ in ys)], ["Total", *(900.0 for _ in ys)], blank,
        ["CASH FLOW:"], ["Report Date", *ys],
        ["Cash from Operating Activity", *(50.0 for _ in ys)], blank,
        ["PRICE:", *(price for _ in ys)], blank,
        ["DERIVED:"], ["Adjusted Equity Shares in Cr", *(10.0 for _ in ys)]]
    return workbook({"Profit & Loss": [["Narration"], ["Sales"]], "Data Sheet": rows})
