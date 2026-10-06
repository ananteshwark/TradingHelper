"""Cell values out of an .xlsx workbook, with the standard library only.

An .xlsx file is a zip of XML parts (ECMA-376, Office Open XML). This reads what an export
such as Screener.in's needs: each sheet's cell values by name. Not formulas (a formula cell
gives the value cached in the file, or None if it has none), styles or formatting, so a date
stored as a number stays a number; `excel_date` converts one where the layout says it is a
date.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CELL_REF = re.compile(r"([A-Z]+)(\d+)$")
MAX_PART_BYTES = 30_000_000        # one exported sheet is far smaller; this stops zip bombs


class NotXlsx(ValueError):
    """The file is not a readable .xlsx workbook."""


def _column(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _text(el: ET.Element) -> str:
    """A string item: plain, or rich text in runs."""
    return "".join(t.text or "" for t in el.iter(f"{MAIN}t"))


def _part(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise NotXlsx(f"{name} is {info.file_size:,} bytes uncompressed, too large to read")
    return ET.fromstring(z.read(info))


def _cell(c: ET.Element, strings: list[str]) -> Any:
    kind = c.get("t", "n")
    v = c.find(f"{MAIN}v")
    if kind == "inlineStr":
        inline = c.find(f"{MAIN}is")
        return _text(inline) if inline is not None else None
    if v is None or v.text is None:
        return None
    if kind == "s":
        return strings[int(v.text)]
    if kind in ("str", "d"):
        return v.text
    if kind == "b":
        return v.text == "1"
    if kind == "e":
        return None
    return float(v.text)


def read(content: bytes) -> tuple[dict[str, list[list[Any]]], bool]:
    """({sheet name: rows of cell values, in sheet order}, whether dates count from 1904).
    Each row is a list indexed by column (A = 0), None where a cell is empty; rows with no
    cells are left out."""
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise NotXlsx("not an .xlsx workbook (not a zip file)") from exc
    with z:
        book = _part(z, "xl/workbook.xml")
        rels = _part(z, "xl/_rels/workbook.xml.rels")
        if book is None or rels is None:
            raise NotXlsx("not an .xlsx workbook (no xl/workbook.xml)")
        pr = book.find(f"{MAIN}workbookPr")
        date1904 = pr is not None and pr.get("date1904") in ("1", "true")
        targets = {r.get("Id"): r.get("Target", "") for r in rels.iter(f"{PKG}Relationship")}
        shared = _part(z, "xl/sharedStrings.xml")
        strings = [_text(si) for si in shared.iter(f"{MAIN}si")] if shared is not None else []
        sheets: dict[str, list[list[Any]]] = {}
        for s in book.iter(f"{MAIN}sheet"):
            target = targets.get(s.get(f"{REL}id"), "")
            path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            xml = _part(z, path)
            if xml is None:
                raise NotXlsx(f"sheet {s.get('name')!r} points to a missing part {path}")
            rows = []
            for row in xml.iter(f"{MAIN}row"):
                cells: list[Any] = []
                for c in row.iter(f"{MAIN}c"):
                    m = CELL_REF.match(c.get("r", ""))
                    col = _column(m.group(1)) if m else len(cells)
                    cells.extend([None] * (col + 1 - len(cells)))
                    cells[col] = _cell(c, strings)
                if any(v is not None for v in cells):
                    rows.append(cells)
            sheets[s.get("name", "")] = rows
        return sheets, date1904


def excel_date(serial: float, date1904: bool = False) -> dt.date:
    """The date an Excel serial number stands for (1900 system: 1 = 1 Jan 1900, counting
    Excel's phantom 29 Feb 1900, so day zero is 30 Dec 1899)."""
    base = dt.date(1904, 1, 1) if date1904 else dt.date(1899, 12, 30)
    return base + dt.timedelta(days=int(serial))
