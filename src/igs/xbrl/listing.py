"""Exchange filing listings -> filing references (symbol, period, filed_at, XBRL URL).

The listing JSON is where filed_at comes from: the exchange's dissemination
timestamp. It is never taken from the XBRL document (which only knows the
period) and never from our own fetch time.

STATUS: key names follow NSE's listing payloads as documented; candidates are
tried in order and a listing row missing a required field is reported, not
guessed. Confirm on the first verified sample.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from urllib.parse import urlparse

import polars as pl

from igs.dq import DQLog
from igs.normalize.nse import SchemaMismatch, parse_date, parse_ist_timestamp

KEYS = {
    "financial_results": {
        "symbol": ["symbol"],
        "company_name": ["companyName", "company", "sm_name"],
        "period_end": ["toDate", "periodEnd", "period_end", "qeDate"],
        "filed_at": ["broadCastDate", "broadcastDate", "exchdisstime", "filingDate",
                     "submissionDate"],
        "document_url": ["xbrl", "xbrlFile", "xbrlUrl"],
        "basis_hint": ["consolidated", "nature"],
        "exchange_ref": ["seqNumber", "seqNo", "id"],
    },
    "shareholding": {
        "symbol": ["symbol"],
        "company_name": ["name", "companyName"],
        "period_end": ["date", "asOnDate", "periodEnd"],
        "filed_at": ["broadcastDate", "submissionDate", "systemDate"],
        "document_url": ["xbrl", "xbrlFile", "xbrlUrl"],
        "basis_hint": [],
        "exchange_ref": ["recordId", "id"],
    },
}
REQUIRED = ("symbol", "period_end", "filed_at", "document_url")


def _pick(row: dict[str, Any], keys: list[str]) -> Any:
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "-"):
            return v
    return None


def _basis(hint: Any) -> str | None:
    if hint is None:
        return None
    h = str(hint).strip().lower()
    if "non" in h or "standalone" in h:
        return "standalone"
    if "consolidated" in h:
        return "consolidated"
    return None


def parse_listing(content: bytes, filing_type: str, filing_system: str,
                  allowed_hosts: list[str], dq: DQLog,
                  fetch_id: str | None = None) -> pl.DataFrame:
    data = json.loads(content)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if not isinstance(data, list):
        raise SchemaMismatch(f"{filing_system} listing is not a list")
    keys = KEYS[filing_type]
    out = []
    for r in data:
        rec = {k: _pick(r, cands) for k, cands in keys.items()}
        missing = [k for k in REQUIRED if rec[k] is None]
        if missing:
            dq.emit("warn", "listing_row_incomplete",
                    f"{filing_system} row for {rec.get('symbol')} lacks {missing}",
                    fetch_id=fetch_id, details={"keys": sorted(r)[:40]})
            continue
        url = str(rec["document_url"])
        host = urlparse(url).hostname or ""
        if host not in allowed_hosts:
            dq.emit("error", "document_host_not_allowed",
                    f"{rec['symbol']}: XBRL link to {host!r} refused", fetch_id=fetch_id)
            continue
        raw_ts = str(rec["filed_at"])
        filed = parse_ist_timestamp(raw_ts)
        precise = ":" in raw_ts
        if not precise:
            dq.emit("info", "filed_at_date_only",
                    f"{rec['symbol']}: only a date for filed_at; using 23:59:59 IST",
                    fetch_id=fetch_id)
        out.append({
            "exchange": "NSE", "filing_system": filing_system, "filing_type": filing_type,
            "symbol": str(rec["symbol"]).strip(), "company_name": rec["company_name"],
            "period_end": parse_date(str(rec["period_end"])), "basis_hint": _basis(
                rec["basis_hint"]), "filed_at": filed, "filed_at_precise": precise,
            "document_url": url, "exchange_ref": None if rec["exchange_ref"] is None
            else str(rec["exchange_ref"]),
        })
    schema = {"exchange": pl.Utf8, "filing_system": pl.Utf8, "filing_type": pl.Utf8,
              "symbol": pl.Utf8, "company_name": pl.Utf8, "period_end": pl.Date,
              "basis_hint": pl.Utf8, "filed_at": pl.Datetime("us", "Asia/Kolkata"),
              "filed_at_precise": pl.Boolean, "document_url": pl.Utf8,
              "exchange_ref": pl.Utf8}
    return pl.DataFrame(out, schema=schema)


def ref_to_params(ref: dict[str, Any]) -> dict[str, Any]:
    """What a document fetch record carries so a rebuild can reload it standalone."""
    out = {}
    for k, v in ref.items():
        if isinstance(v, dt.datetime):
            out[k] = v.isoformat()
        elif isinstance(v, dt.date):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def params_to_ref(params: dict[str, Any]) -> dict[str, Any]:
    ref = dict(params)
    ref["filed_at"] = dt.datetime.fromisoformat(ref["filed_at"])
    ref["period_end"] = dt.date.fromisoformat(ref["period_end"])
    return ref
