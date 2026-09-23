"""Parsers for NSE files and API payloads.

Each parser takes the raw bytes exactly as landed and returns a normalised
Polars frame. Column expectations follow NSE's published formats; they are
checked on every parse and a missing column raises `SchemaMismatch` instead
of producing a partially filled frame. The ingestion layer additionally
compares every payload's schema fingerprint with the verified one.

STATUS: written from NSE's documented formats. Not yet run against a real
payload from this environment (egress blocked); the first verified sample of
each source must be added under tests/fixtures/real/ and parsed in a test.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import zipfile
from collections.abc import Iterable
from typing import Any

import polars as pl

from igs.dq import DQLog
from igs.timeutil import IST

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
    start=1)}


class SchemaMismatch(ValueError):
    pass


class ParseError(ValueError):
    pass


# --------------------------------------------------------------------------- helpers


def _read_csv_text(content: bytes, zipped: bool = False) -> str:
    if zipped:
        zf = zipfile.ZipFile(io.BytesIO(content))
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(members) != 1:
            raise ParseError(f"expected exactly one CSV in zip, found {zf.namelist()}")
        content = zf.read(members[0])
    return content.decode("utf-8-sig", errors="strict")


def _rows(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.reader(io.StringIO(text))
    header: list[str] | None = None
    out: list[dict[str, str]] = []
    for raw in reader:
        if not any(c.strip() for c in raw):
            continue
        if header is None:
            header = [c.strip() for c in raw]
            continue
        cells = [c.strip() for c in raw]
        out.append(dict(zip(header, cells, strict=False)))
    if header is None:
        raise ParseError("empty CSV")
    return header, out


def _require(header: Iterable[str], required: Iterable[str], what: str) -> None:
    missing = [c for c in required if c not in set(header)]
    if missing:
        raise SchemaMismatch(f"{what}: missing columns {missing}; got {list(header)}")


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip().replace(",", "")
    if s in ("", "-", "NA", "N.A.", "nil", "None", "null"):
        return None
    return float(s)


def _int(s: str | None) -> int | None:
    v = _num(s)
    return None if v is None else int(round(v))


def parse_date(s: str | None) -> dt.date | None:
    """NSE uses several date spellings: 2024-07-08, 08-07-2024, 08-Jul-2024, 08-JUL-2024,
    08JUL2024, 08072024."""
    if s is None:
        return None
    s = s.strip()
    if s in ("", "-", "NA"):
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[-/ ]([A-Za-z]{3})[-/ ](\d{4})", s)
    if m:
        return dt.date(int(m[3]), _MONTHS[m[2].upper()], int(m[1]))
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        return dt.date(int(m[3]), int(m[2]), int(m[1]))
    m = re.fullmatch(r"(\d{2})([A-Za-z]{3})(\d{4})", s)
    if m:
        return dt.date(int(m[3]), _MONTHS[m[2].upper()], int(m[1]))
    m = re.fullmatch(r"(\d{2})(\d{2})(\d{4})", s)
    if m:
        return dt.date(int(m[3]), int(m[2]), int(m[1]))
    raise ParseError(f"unrecognised date {s!r}")


def parse_ist_timestamp(s: str | None) -> dt.datetime | None:
    """'22-Sep-2026 18:32:11' or '2026-09-22 18:32:11' (exchange time, IST)."""
    if s is None or not str(s).strip() or str(s).strip() == "-":
        return None
    s = str(s).strip()
    m = re.fullmatch(r"(.+?)[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if not m:
        d = parse_date(s)
        return None if d is None else dt.datetime.combine(d, dt.time(23, 59, 59), tzinfo=IST)
    d = parse_date(m[1])
    assert d is not None
    return dt.datetime(d.year, d.month, d.day, int(m[2]), int(m[3]), int(m[4] or 0), tzinfo=IST)


PRICE_SCHEMA = {
    "exchange": pl.Utf8, "trade_date": pl.Date, "isin": pl.Utf8, "symbol": pl.Utf8,
    "series": pl.Utf8, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
    "close": pl.Float64, "last": pl.Float64, "prev_close": pl.Float64, "volume": pl.Int64,
    "turnover_inr": pl.Float64, "trades": pl.Int64, "session_id": pl.Utf8,
}


# --------------------------------------------------------------------------- bhavcopy

UDIFF_REQUIRED = ["TradDt", "Sgmt", "FinInstrmTp", "ISIN", "TckrSymb", "SctySrs", "OpnPric",
                  "HghPric", "LwPric", "ClsPric", "LastPric", "PrvsClsgPric", "TtlTradgVol",
                  "TtlTrfVal", "TtlNbOfTxsExctd"]


def parse_bhavcopy_udiff(content: bytes, final_sessions: list[str], dq: DQLog,
                         fetch_id: str | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """UDiFF CM bhavcopy -> (kept final-session equity rows, all rows' session counts).

    The file can contain pre-open/interim session records alongside final ones
    (SsnId I1/I2 vs F1/F2). Only configured final sessions are kept, and if a
    key still appears more than once the file is rejected rather than guessed.
    """
    header, rows = _rows(_read_csv_text(content, zipped=content[:2] == b"PK"))
    _require(header, UDIFF_REQUIRED, "UDiFF bhavcopy")
    has_session = "SsnId" in header
    if not has_session:
        dq.emit("info", "udiff_no_session_column",
                "UDiFF file has no SsnId column; all rows treated as final", fetch_id=fetch_id)
    out, sessions = [], []
    for r in rows:
        if r.get("Sgmt") != "CM" or r.get("FinInstrmTp") != "STK":
            continue
        ssn = r.get("SsnId", "") if has_session else ""
        sessions.append(ssn)
        if has_session and ssn not in final_sessions:
            continue
        out.append({
            "exchange": "NSE", "trade_date": parse_date(r["TradDt"]), "isin": r["ISIN"],
            "symbol": r["TckrSymb"], "series": r["SctySrs"], "open": _num(r["OpnPric"]),
            "high": _num(r["HghPric"]), "low": _num(r["LwPric"]), "close": _num(r["ClsPric"]),
            "last": _num(r["LastPric"]), "prev_close": _num(r["PrvsClsgPric"]),
            "volume": _int(r["TtlTradgVol"]), "turnover_inr": _num(r["TtlTrfVal"]),
            "trades": _int(r["TtlNbOfTxsExctd"]), "session_id": ssn or None,
        })
    kept = pl.DataFrame(out, schema=PRICE_SCHEMA)
    counts = (pl.DataFrame({"session_id": sessions}, schema={"session_id": pl.Utf8})
                .group_by("session_id").len().sort("session_id"))
    dups = kept.group_by("trade_date", "isin", "series").len().filter(pl.col("len") > 1)
    if dups.height:
        raise ParseError(f"{dups.height} ISIN/series keys appear in more than one final session "
                         f"{final_sessions}; decide precedence before loading "
                         f"(e.g. {dups.head(3).to_dicts()})")
    missing_close = kept.filter(pl.col("close").is_null()).height
    if missing_close:
        dq.emit("warn", "missing_close", f"{missing_close} rows without close price",
                fetch_id=fetch_id)
    return kept, counts


LEGACY_REQUIRED = ["SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "LAST", "PREVCLOSE",
                   "TOTTRDQTY", "TOTTRDVAL", "TIMESTAMP", "TOTALTRADES", "ISIN"]


def parse_bhavcopy_legacy(content: bytes) -> pl.DataFrame:
    header, rows = _rows(_read_csv_text(content, zipped=content[:2] == b"PK"))
    _require(header, LEGACY_REQUIRED, "legacy bhavcopy")
    out = [{
        "exchange": "NSE", "trade_date": parse_date(r["TIMESTAMP"]), "isin": r["ISIN"],
        "symbol": r["SYMBOL"], "series": r["SERIES"], "open": _num(r["OPEN"]),
        "high": _num(r["HIGH"]), "low": _num(r["LOW"]), "close": _num(r["CLOSE"]),
        "last": _num(r["LAST"]), "prev_close": _num(r["PREVCLOSE"]),
        "volume": _int(r["TOTTRDQTY"]), "turnover_inr": _num(r["TOTTRDVAL"]),
        "trades": _int(r["TOTALTRADES"]), "session_id": None,
    } for r in rows]
    return pl.DataFrame(out, schema=PRICE_SCHEMA)


# --------------------------------------------------------------------------- delivery

DELIVERY_SCHEMA = {"trade_date": pl.Date, "symbol": pl.Utf8, "series": pl.Utf8,
                   "traded_qty": pl.Int64, "delivery_qty": pl.Int64, "delivery_pct": pl.Float64}

SEC_FULL_REQUIRED = ["SYMBOL", "SERIES", "DATE1", "TTL_TRD_QNTY", "DELIV_QTY", "DELIV_PER"]


def parse_sec_bhavdata_full(content: bytes) -> pl.DataFrame:
    header, rows = _rows(_read_csv_text(content))
    _require(header, SEC_FULL_REQUIRED, "sec_bhavdata_full")
    out = [{"trade_date": parse_date(r["DATE1"]), "symbol": r["SYMBOL"], "series": r["SERIES"],
            "traded_qty": _int(r["TTL_TRD_QNTY"]), "delivery_qty": _int(r["DELIV_QTY"]),
            "delivery_pct": _num(r["DELIV_PER"])} for r in rows]
    return pl.DataFrame(out, schema=DELIVERY_SCHEMA)


def parse_mto(content: bytes, trade_date: dt.date) -> pl.DataFrame:
    """Security-wise delivery position. Data lines are record type 20:
    20,<sr>,<symbol>,<series>,<traded qty>,<deliverable qty>,<deliverable %>"""
    out = []
    for line in content.decode("utf-8", errors="strict").splitlines():
        cells = [c.strip() for c in line.split(",")]
        if not cells or cells[0] != "20":
            continue
        if len(cells) < 7:
            raise SchemaMismatch(f"MTO record type 20 with {len(cells)} fields: {line!r}")
        out.append({"trade_date": trade_date, "symbol": cells[2], "series": cells[3],
                    "traded_qty": _int(cells[4]), "delivery_qty": _int(cells[5]),
                    "delivery_pct": _num(cells[6])})
    if not out:
        raise ParseError("MTO file contains no record-type-20 lines")
    return pl.DataFrame(out, schema=DELIVERY_SCHEMA)


# --------------------------------------------------------------------------- masters

EQUITY_L_REQUIRED = ["SYMBOL", "NAME OF COMPANY", "SERIES", "DATE OF LISTING", "PAID UP VALUE",
                     "MARKET LOT", "ISIN NUMBER", "FACE VALUE"]


def parse_equity_list(content: bytes) -> pl.DataFrame:
    header, rows = _rows(_read_csv_text(content))
    _require(header, EQUITY_L_REQUIRED, "EQUITY_L")
    out = [{"symbol": r["SYMBOL"], "isin": r["ISIN NUMBER"], "company_name": r["NAME OF COMPANY"],
            "series": r["SERIES"], "listed_on": parse_date(r["DATE OF LISTING"]),
            "face_value": _num(r["FACE VALUE"]), "paid_up_value": _num(r["PAID UP VALUE"]),
            "market_lot": _int(r["MARKET LOT"])} for r in rows]
    return pl.DataFrame(out, schema={
        "symbol": pl.Utf8, "isin": pl.Utf8, "company_name": pl.Utf8, "series": pl.Utf8,
        "listed_on": pl.Date, "face_value": pl.Float64, "paid_up_value": pl.Float64,
        "market_lot": pl.Int64})


NIFTY_LIST_REQUIRED = ["Company Name", "Industry", "Symbol", "Series", "ISIN Code"]


def parse_index_constituents(content: bytes) -> pl.DataFrame:
    header, rows = _rows(_read_csv_text(content))
    _require(header, NIFTY_LIST_REQUIRED, "index constituent list")
    return pl.DataFrame([{"company_name": r["Company Name"], "industry": r["Industry"],
                          "symbol": r["Symbol"], "series": r["Series"], "isin": r["ISIN Code"]}
                         for r in rows])


INDEX_CLOSE_REQUIRED = ["Index Name", "Index Date", "Open Index Value", "High Index Value",
                        "Low Index Value", "Closing Index Value"]


def parse_index_close_all(content: bytes) -> pl.DataFrame:
    header, rows = _rows(_read_csv_text(content))
    _require(header, INDEX_CLOSE_REQUIRED, "ind_close_all")
    out = [{"index_name": r["Index Name"], "trade_date": parse_date(r["Index Date"]),
            "open": _num(r["Open Index Value"]), "high": _num(r["High Index Value"]),
            "low": _num(r["Low Index Value"]), "close": _num(r["Closing Index Value"]),
            "is_total_return": False} for r in rows]
    return pl.DataFrame(out, schema={"index_name": pl.Utf8, "trade_date": pl.Date,
                                     "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
                                     "close": pl.Float64, "is_total_return": pl.Boolean})


def parse_holidays(content: bytes, segment: str = "CM") -> pl.DataFrame:
    data = json.loads(content)
    if segment not in data:
        raise SchemaMismatch(f"holiday JSON has no {segment!r} segment; keys {list(data)}")
    out = []
    for r in data[segment]:
        if "tradingDate" not in r:
            raise SchemaMismatch(f"holiday row without tradingDate: {r}")
        out.append({"holiday_date": parse_date(r["tradingDate"]),
                    "description": r.get("description", "")})
    return pl.DataFrame(out, schema={"holiday_date": pl.Date, "description": pl.Utf8})


def parse_quote_classification(content: bytes) -> dict[str, str | None]:
    """Four-level industry classification from the per-symbol quote payload."""
    data = json.loads(content)
    info = data.get("industryInfo")
    if not isinstance(info, dict):
        raise SchemaMismatch(f"quote payload has no industryInfo; keys {list(data)}")
    return {"macro_sector": info.get("macro"), "sector": info.get("sector"),
            "industry": info.get("industry"), "basic_industry": info.get("basicIndustry")}


# --------------------------------------------------------------------------- surveillance


def _walk_records(obj: Any, path: str = "") -> Iterable[tuple[str, dict]]:
    if isinstance(obj, dict):
        if "symbol" in obj and isinstance(obj.get("symbol"), str):
            yield path, obj
            return
        for k, v in obj.items():
            yield from _walk_records(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_records(v, path)


def parse_surveillance(content: bytes, measure: str, effective_from: dt.date,
                       dq: DQLog) -> pl.DataFrame:
    """ASM/GSM current lists. The payload nests lists (e.g. long-term / short-term
    ASM); every record carrying a symbol is taken, with the list it came from."""
    data = json.loads(content)
    out = []
    for path, rec in _walk_records(data):
        stage = next((str(v) for k, v in rec.items()
                      if re.search(r"stage|survdesc|indicator", k, re.IGNORECASE) and v), None)
        out.append({"measure": measure, "list_name": path or measure, "symbol": rec["symbol"],
                    "isin": rec.get("isin"), "stage": stage, "effective_from": effective_from})
    if not out:
        dq.emit("error", "surveillance_empty", f"{measure} payload contained no symbol records",
                details={"top_level_keys": list(data)[:20] if isinstance(data, dict) else "list"})
    return pl.DataFrame(out, schema={"measure": pl.Utf8, "list_name": pl.Utf8,
                                     "symbol": pl.Utf8, "isin": pl.Utf8, "stage": pl.Utf8,
                                     "effective_from": pl.Date}).unique(
        subset=["measure", "list_name", "symbol"], keep="first", maintain_order=True)


# --------------------------------------------------------------------------- corporate actions

_RS = r"(?:rs\.?|re\.?|inr|₹)\s*"
_NUM = r"(\d+(?:\.\d+)?)"


def parse_ca_subject(subject: str, face_value: float | None) -> list[dict[str, Any]]:
    """Turn an NSE corporate-action subject into one or more typed actions.

    Examples:
      'Bonus 1:1'
      'Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share'
      'Rights 1:4 @ Premium Rs 80/-'
      'Interim Dividend - Rs 5 Per Share / Special Dividend - Rs 2 Per Share'
      'Dividend - Re 1 Per Sh'          (truncated by NSE; seen in real payloads)
    Anything unrecognised becomes action_type 'other' (never dropped).
    """
    actions = []
    for part in [p.strip() for p in re.split(r"\s/\s|;", subject) if p.strip()]:
        low = part.lower()
        m = re.search(r"bonus\s*" + _NUM + r"\s*:\s*" + _NUM, low)
        if m:
            actions.append({"action_type": "bonus", "ratio_a": float(m[1]),
                            "ratio_b": float(m[2])})
            continue
        m = re.search(r"from\s*" + _RS + _NUM + r".*?to\s*" + _RS + _NUM, low)
        if m and re.search(r"split|sub-?division|consolidat", low):
            old, new = float(m[1]), float(m[2])
            kind = "consolidation" if new > old else "split"
            actions.append({"action_type": kind, "fv_old": old, "fv_new": new})
            continue
        m = re.search(r"rights\s*" + _NUM + r"\s*:\s*" + _NUM + r"\s*@\s*(premium\s*)?(?:"
                      + _RS + r")?" + _NUM, low)
        if m:
            price = float(m[4])
            if m[3]:
                if face_value is None:
                    actions.append({"action_type": "other", "note": "rights premium without FV"})
                    continue
                price += face_value
            actions.append({"action_type": "rights", "ratio_a": float(m[1]),
                            "ratio_b": float(m[2]), "issue_price": price})
            continue
        # NSE truncates long subjects ("Dividend - Re 1 Per Sh"): accept the amount when what
        # follows it is "per share" or a truncation of it, nothing else.
        m = re.search(r"dividend.*?" + _RS + _NUM + r"\s*(?:/-)?\s*"
                      r"(?:per\s*share\b.*|(?:per\s*sh(?:ar?)?\.?|per\s*s?|pe?)?\s*)$", low)
        if m:
            actions.append({"action_type": "dividend", "cash_per_share": float(m[1])})
            continue
        if re.search(r"demerger|scheme of arrangement|spin[\s-]?off", low):
            actions.append({"action_type": "demerger"})
            continue
        actions.append({"action_type": "other"})
    return actions or [{"action_type": "other"}]


CA_REQUIRED = ["symbol", "series", "subject", "exDate", "faceVal"]
CA_SCHEMA = {"exchange": pl.Utf8, "symbol": pl.Utf8, "series": pl.Utf8, "isin": pl.Utf8,
             "action_type": pl.Utf8, "ex_date": pl.Date, "record_date": pl.Date,
             "announced_at": pl.Datetime("us", "Asia/Kolkata"), "fv_old": pl.Float64,
             "fv_new": pl.Float64, "ratio_a": pl.Float64, "ratio_b": pl.Float64,
             "issue_price": pl.Float64, "cash_per_share": pl.Float64, "subject": pl.Utf8}


def parse_corporate_actions(content: bytes, dq: DQLog,
                            fetch_id: str | None = None) -> pl.DataFrame:
    data = json.loads(content)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if not isinstance(data, list):
        raise SchemaMismatch(f"corporate actions payload is {type(data).__name__}, not a list")
    out = []
    for r in data:
        _require(r.keys(), CA_REQUIRED, "corporate action row")
        ex = parse_date(r["exDate"])
        if ex is None:
            dq.emit("warn", "ca_without_ex_date", f"{r['symbol']}: {r['subject']}",
                    fetch_id=fetch_id)
            continue
        fv = _num(str(r.get("faceVal"))) if r.get("faceVal") is not None else None
        for a in parse_ca_subject(r["subject"], fv):
            if a["action_type"] in ("other", "demerger"):
                dq.emit("warn", "ca_needs_review",
                        f"{r['symbol']} ex {ex}: '{r['subject']}' parsed as {a['action_type']}",
                        fetch_id=fetch_id)
            out.append({
                "exchange": "NSE", "symbol": r["symbol"], "series": r.get("series"),
                "isin": r.get("isin"), "action_type": a["action_type"], "ex_date": ex,
                "record_date": parse_date(r.get("recDate")),
                "announced_at": parse_ist_timestamp(r.get("caBroadcastDate")),
                "fv_old": a.get("fv_old"), "fv_new": a.get("fv_new"),
                "ratio_a": a.get("ratio_a"), "ratio_b": a.get("ratio_b"),
                "issue_price": a.get("issue_price"), "cash_per_share": a.get("cash_per_share"),
                "subject": r["subject"],
            })
    return pl.DataFrame(out, schema=CA_SCHEMA)


# --------------------------------------------------------------------------- announcements

ANN_SCHEMA = {"exchange": pl.Utf8, "symbol": pl.Utf8, "filed_at": pl.Datetime("us", "Asia/Kolkata"),
              "category": pl.Utf8, "subject": pl.Utf8, "body": pl.Utf8,
              "attachment_url": pl.Utf8, "exchange_ref": pl.Utf8}


def parse_announcements(content: bytes, dq: DQLog, fetch_id: str | None = None) -> pl.DataFrame:
    data = json.loads(content)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if not isinstance(data, list):
        raise SchemaMismatch("announcements payload is not a list")
    out = []
    for r in data:
        _require(r.keys(), ["symbol", "desc"], "announcement row")
        ts = parse_ist_timestamp(r.get("exchdisstime") or r.get("an_dt") or r.get("sort_date"))
        if ts is None:
            dq.emit("warn", "announcement_without_time", f"{r['symbol']}: {r['desc']}",
                    fetch_id=fetch_id)
            continue
        out.append({"exchange": "NSE", "symbol": r["symbol"], "filed_at": ts,
                    "category": r["desc"], "subject": r.get("attchmntText") or r["desc"],
                    "body": r.get("attchmntText") or "", "attachment_url": r.get("attchmntFile"),
                    "exchange_ref": str(r.get("seq_id") or "") or None})
    return pl.DataFrame(out, schema=ANN_SCHEMA)
