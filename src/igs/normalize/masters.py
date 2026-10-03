"""Parsers for BSE, broker (Tier 2) and Screener.in (Tier 3) masters/exports.

STATUS: BSE and Angel One layouts follow their published payloads and have not
yet been checked against a verified sample from this environment. The Screener.in Excel
export is read by the shape of its 'Data Sheet' tab (parse_screener_workbook), not a fixed
label list, and has not yet been checked against a real export either.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from typing import Any

import polars as pl

from igs.normalize import xlsx
from igs.normalize.nse import SchemaMismatch, _num, _require


def parse_bse_scrips(content: bytes) -> pl.DataFrame:
    data = json.loads(content)
    if isinstance(data, dict) and "Table" in data:
        data = data["Table"]
    if not isinstance(data, list):
        raise SchemaMismatch("BSE scrip payload is not a list")
    out = []
    for r in data:
        _require(r.keys(), ["SCRIP_CD", "Scrip_Name", "ISIN_NUMBER"], "BSE scrip row")
        out.append({"scrip_code": str(r["SCRIP_CD"]).strip(),
                    "isin": (r.get("ISIN_NUMBER") or "").strip() or None,
                    "scrip_id": (r.get("scrip_id") or "").strip() or None,
                    "name": r["Scrip_Name"].strip(), "status": r.get("Status"),
                    "scrip_group": (r.get("GROUP") or "").strip() or None,
                    "face_value": _num(str(r.get("FACE_VALUE") or "")),
                    "industry": r.get("INDUSTRY")})
    return pl.DataFrame(out, schema={"scrip_code": pl.Utf8, "isin": pl.Utf8,
                                     "scrip_id": pl.Utf8, "name": pl.Utf8, "status": pl.Utf8,
                                     "scrip_group": pl.Utf8, "face_value": pl.Float64,
                                     "industry": pl.Utf8})


def parse_angel_master(content: bytes) -> pl.DataFrame:
    """Angel One instrument master; keeps NSE/BSE cash-equity lines only."""
    data = json.loads(content)
    if not isinstance(data, list):
        raise SchemaMismatch("Angel master is not a list")
    out = []
    for r in data:
        _require(r.keys(), ["token", "symbol", "name", "exch_seg", "instrumenttype"],
                 "Angel instrument")
        if r["exch_seg"] not in ("NSE", "BSE") or r["instrumenttype"] not in ("", None):
            continue
        sym = r["symbol"]
        if r["exch_seg"] == "NSE":
            if not sym.endswith("-EQ"):
                continue
            sym = sym[:-3]
        out.append({"broker": "angel", "exchange": r["exch_seg"], "token": str(r["token"]),
                    "symbol": sym, "name": r["name"], "instrument_type": "EQ"})
    return pl.DataFrame(out, schema={"broker": pl.Utf8, "exchange": pl.Utf8, "token": pl.Utf8,
                                     "symbol": pl.Utf8, "name": pl.Utf8,
                                     "instrument_type": pl.Utf8})


def parse_screener_csv(content: bytes) -> pl.DataFrame:
    """Screener.in screen export. Mapped on the 'NSE Code' / 'BSE Code' columns the
    export includes; every other column is kept as a (field, value) pair.

    Never merged into point-in-time fundamentals: the export has no filing dates.
    """
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    if "NSE Code" not in header and "BSE Code" not in header:
        raise SchemaMismatch("Screener export needs an 'NSE Code' or 'BSE Code' column; add it "
                             "to the screen's columns before exporting")
    out = []
    for raw in reader:
        r = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        nse, bse = r.get("NSE Code") or None, r.get("BSE Code") or None
        for field, value in r.items():
            if field in ("NSE Code", "BSE Code", "S.No."):
                continue
            try:
                num = _num(value)
            except ValueError:
                num = None
            out.append({"nse_code": nse, "bse_code": bse, "section": None, "field": field,
                        "period_label": None, "value_text": value, "value_num": num})
    return pl.DataFrame(out, schema={"nse_code": pl.Utf8, "bse_code": pl.Utf8,
                                     "section": pl.Utf8, "field": pl.Utf8,
                                     "period_label": pl.Utf8, "value_text": pl.Utf8,
                                     "value_num": pl.Float64})


SCREENER_SHEET = "Data Sheet"
MONTH_YEAR = ("%b-%y", "%b %Y", "%b-%Y", "%b %y")


def _period(value: Any, date1904: bool) -> dt.date | None:
    """A 'Report Date' cell: an Excel date number, an ISO date, or a month such as "Mar-24"
    (taken as its last day, where Indian financial periods end)."""
    if isinstance(value, float):
        return xlsx.excel_date(value, date1904)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in MONTH_YEAR:
        try:
            first = dt.datetime.strptime(text, fmt).date()  # noqa: DTZ007 - a date
        except ValueError:
            continue
        nxt = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return nxt - dt.timedelta(days=1)
    return None


def parse_screener_workbook(content: bytes) -> dict:
    """A company's Screener.in "Export to Excel" workbook, from its 'Data Sheet' tab, which
    holds the figures the other tabs are computed from: {"company_name", "meta": {label:
    value}, "sections": {section: {"periods": [ISO dates], "rows": {label: [values]}}}}.

    Read by its shape rather than a fixed list of labels, so a renamed or new line still
    arrives: a row with only a label starts a section (PROFIT & LOSS, Quarters, BALANCE
    SHEET, CASH FLOW, ...), its 'Report Date' row gives the columns' dates, and every later
    row with a label and numbers is a line of it. A section without a Report Date row of
    its own (PRICE:, whose figures sit on its label's row, and DERIVED:) takes the latest
    one's dates, which are the annual ones. Rows before the first
    'Report Date' are the company name and META (shares, face value, price and market
    capitalisation when downloaded). Screener's figures are in Rs crore except per-share
    values, share counts and prices.
    """
    try:
        sheets, date1904 = xlsx.read(content)
    except xlsx.NotXlsx as exc:
        raise SchemaMismatch(f"not a Screener.in Excel export: {exc}") from exc
    name = next((n for n in sheets if n.strip().lower() == SCREENER_SHEET.lower()), None)
    if name is None:
        raise SchemaMismatch(f"not a Screener.in Excel export: no '{SCREENER_SHEET}' tab "
                             f"(tabs: {', '.join(sheets) or 'none'})")
    out: dict[str, Any] = {"company_name": None, "meta": {}, "sections": {}}
    section, periods, last_periods = "META", None, None
    rows = [r for r in sheets[name] if r and isinstance(r[0], str) and r[0].strip()]

    def heading(i: int, label: str) -> bool:
        """A label alone starts a section when it reads as a heading (PROFIT & LOSS, CASH
        FLOW:) or a Report Date row follows (Quarters); else it is a line with no figures."""
        following = rows[i + 1][0].strip().lower() if i + 1 < len(rows) else ""
        return label.isupper() or label.endswith(":") or following == "report date"
    for i, row in enumerate(rows):
        label = row[0].strip()
        values = list(row[1:])
        numbers = [v for v in values if isinstance(v, float)]
        if label.lower() == "company name":
            out["company_name"] = next((str(v).strip() for v in values if v not in (None, "")),
                                       None)
            continue
        if label.lower() == "report date":
            periods = [_period(v, date1904) for v in values]
            last_periods = periods
            out["sections"].setdefault(section, {"periods": [], "rows": {}})["periods"] = [
                None if p is None else p.isoformat() for p in periods]
            continue
        if not numbers and all(v in (None, "") for v in values) and heading(i, label):
            section, periods = label.rstrip(":").strip(), None
            continue
        if label.endswith(":") and numbers:
            section, periods = label.rstrip(":").strip(), last_periods
            out["sections"][section] = {
                "periods": [None if p is None else p.isoformat() for p in periods or []],
                "rows": {}}
        if periods is None:
            if section == "META" or last_periods is None:
                out["meta"][label] = values[0] if values else None
                continue
            periods = last_periods           # DERIVED: has no Report Date of its own
        lines = out["sections"].setdefault(section, {"periods": [
            None if p is None else p.isoformat() for p in periods], "rows": {}})["rows"]
        key, n = label.rstrip(":").strip(), 2
        while key in lines:                      # BALANCE SHEET has two "Total" lines
            key, n = f"{label} ({n})", n + 1
        lines[key] = [values[j] if j < len(values) and isinstance(values[j], float) else None
                      for j in range(len(periods))]
    if not any(s["rows"] and any(s["periods"]) for s in out["sections"].values()):
        raise SchemaMismatch(f"not a Screener.in Excel export: the '{name}' tab has no "
                             "'Report Date' row with figures under it")
    return out


def parse_screener_excel(content: bytes, nse_code: str | None,
                         bse_code: str | None) -> pl.DataFrame:
    """A Screener.in Excel export as (section, field, period, value) rows for
    screener_enrichment; the company name and META come with period None."""
    book = parse_screener_workbook(content)
    out = [{"nse_code": nse_code, "bse_code": bse_code, "section": "META", "field": k,
            "period_label": None, "value_text": None if v is None else str(v),
            "value_num": v if isinstance(v, float) else None}
           for k, v in [("Company name", book["company_name"]), *book["meta"].items()]]
    for section, s in book["sections"].items():
        for field, values in s["rows"].items():
            for period, value in zip(s["periods"], values, strict=False):
                if period is None or value is None:
                    continue
                out.append({"nse_code": nse_code, "bse_code": bse_code, "section": section,
                            "field": field, "period_label": period, "value_text": str(value),
                            "value_num": value})
    return pl.DataFrame(out, schema={"nse_code": pl.Utf8, "bse_code": pl.Utf8,
                                     "section": pl.Utf8, "field": pl.Utf8,
                                     "period_label": pl.Utf8, "value_text": pl.Utf8,
                                     "value_num": pl.Float64})
