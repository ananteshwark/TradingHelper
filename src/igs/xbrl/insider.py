"""Insider-trading disclosures from NSE's current system (SEBI PIT, from about May 2026).

Two payloads, both from real samples kept in tests/fixtures/real/:

- The listing (the corporates-pit-gg API): one row per disclosure, with its broadcast time
  and the link to its XBRL. filed_at is the later of broadcastDateTime and exchdisstime (the
  exchange's dissemination time, 0-3 s later in the sample): the moment the disclosure was
  public, and the only time the point-in-time view may use. A revision is a new row
  (typeOfSubmission "Revision") that does not say which disclosure it corrects.
- The XBRL ("PIT V2.0"): BSE's in-bse-co taxonomy, with the company in the undimensioned
  context and one context per trade on the ChangeInHoldingOfSecuritiesOfPromotersAxis
  ("Disclosure1", "Disclosure2", ...). Holding percentages are fractions here (0.0021 for
  0.21%), unlike the percent values of the older API; they are stored as percent.

Trades are normalised with the same vocabulary as the older API, so a value it does not
know is kept verbatim, counts as neither a buy nor open market, and is reported.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

import polars as pl

from igs.dq import DQLog
from igs.normalize.nse import (
    INSIDER_SCHEMA,
    SchemaMismatch,
    _num,
    _text,
    classify_insider_trade,
    parse_date,
    parse_ist_timestamp,
    report_unknown_insider_values,
)
from igs.xbrl.instance import Instance, XbrlError, parse_instance
from igs.xbrl.listing import listing_rows

LISTING_REQUIRED = ["appId", "symbol", "broadcastDateTime", "typeOfSubmission",
                    "xmlFileName"]
SUBMISSION_TYPES = ("Original", "Revision")
REF_SCHEMA = {"exchange": pl.Utf8, "disclosure_id": pl.Utf8, "symbol": pl.Utf8,
              "company_name": pl.Utf8, "regulation": pl.Utf8, "submission_type": pl.Utf8,
              "revision_remark": pl.Utf8, "filed_at": pl.Datetime("us", "Asia/Kolkata"),
              "document_url": pl.Utf8}

# The per-trade concepts of the in-bse-co insider-trading form.
PERSON = "NameOfThePerson"
TRADE = {
    "person_category": "CategoryOfPerson",
    "security_type": "TypeOfInstrument",
    "transaction_type": "SecuritiesAcquiredOrDisposedTransactionType",
    "acquisition_mode": "ModeOfAcquisitionOrDisposal",
    "quantity": "SecuritiesAcquiredOrDisposedNumberOfSecurity",
    "value_inr": "SecuritiesAcquiredOrDisposedValueOfSecurity",
    "holding_before": "SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding",
    "holding_after": "SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding",
    "trade_from": "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate",
    "trade_to": "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate",
    "intimated_on": "DateOfIntimationToCompany",
}


def parse_disclosure_listing(content: bytes, allowed_hosts: list[str], dq: DQLog,
                             fetch_id: str | None = None) -> pl.DataFrame:
    out = []
    for r in listing_rows(content, "insider-trading listing"):
        missing = [k for k in LISTING_REQUIRED if k not in r]
        if missing:
            raise SchemaMismatch(f"insider-trading listing row without {missing}; its keys "
                                 f"are {sorted(r)}: update the parser")
        symbol, url = _text(r["symbol"]), _text(r["xmlFileName"])
        times = [parse_ist_timestamp(_text(r.get(k)))
                 for k in ("broadcastDateTime", "exchdisstime")]
        filed = None if times[0] is None else max(t for t in times if t is not None)
        kind = _text(r["typeOfSubmission"])
        if symbol is None or url is None or filed is None or _text(r["appId"]) is None:
            dq.emit("warn", "insider_listing_incomplete",
                    f"disclosure {r.get('appId')} for {symbol}: no symbol, broadcast time "
                    "or XBRL link; skipped", fetch_id=fetch_id)
            continue
        if kind not in SUBMISSION_TYPES:
            dq.emit("warn", "insider_listing_incomplete",
                    f"disclosure {r['appId']} for {symbol}: submission type {kind!r} is "
                    f"neither of {SUBMISSION_TYPES}; skipped", fetch_id=fetch_id)
            continue
        host = urlparse(url).hostname or ""
        if host not in allowed_hosts:
            dq.emit("error", "document_host_not_allowed",
                    f"{symbol}: XBRL link to {host!r} refused", fetch_id=fetch_id)
            continue
        out.append({"exchange": "NSE", "disclosure_id": str(r["appId"]).strip(),
                    "symbol": symbol, "company_name": _text(r.get("companyName")),
                    "regulation": _text(r.get("regulation")), "submission_type": kind,
                    "revision_remark": _text(r.get("revisionRemark")), "filed_at": filed,
                    "document_url": url})
    return pl.DataFrame(out, schema=REF_SCHEMA)


def ref_to_params(ref: dict[str, Any]) -> dict[str, Any]:
    """What a document fetch record carries so a rebuild can load it standalone."""
    return {k: v.isoformat() if isinstance(v, dt.datetime) else v for k, v in ref.items()}


def params_to_ref(params: dict[str, Any]) -> dict[str, Any]:
    return {**params, "filed_at": dt.datetime.fromisoformat(params["filed_at"])}


def _by_context(inst: Instance) -> tuple[dict[str, str | None], dict[str, dict[str, str]]]:
    """(company-level facts, {trade context: facts}) by concept name."""
    main: dict[str, str | None] = {}
    trades: dict[str, dict[str, str]] = defaultdict(dict)
    for f in inst.facts:
        if inst.contexts[f.context].dims:
            if f.value is not None:
                trades[f.context][f.name] = f.value
        else:
            main[f.name] = f.value
    return main, trades


def _number(value: str | None) -> float | None:
    try:
        return _num(value)
    except ValueError:
        return None


def _pct(value: str | None, label: str, dq: DQLog, fetch_id: str | None) -> float | None:
    v = _number(value)
    if v is None:
        return None
    if not 0 <= v <= 1:
        dq.emit("warn", "insider_trade_holding_pct",
                f"{label}: holding {value} is not a fraction of 1; not stored",
                fetch_id=fetch_id)
        return None
    return v * 100


def parse_disclosure_document(content: bytes, ref: dict[str, Any], dq: DQLog,
                              fetch_id: str | None = None) -> pl.DataFrame:
    """The trades in one disclosure's XBRL, dated by the listing's broadcast time. A
    document that is not the expected form is reported and yields no rows."""
    empty = pl.DataFrame(schema=INSIDER_SCHEMA)
    what = f"{ref['symbol']} disclosure {ref['disclosure_id']}"
    try:
        inst = parse_instance(content)
    except XbrlError as exc:
        dq.emit("error", "taxonomy_mismatch", f"{what}: {exc}", fetch_id=fetch_id)
        return empty
    main, trades = _by_context(inst)
    if not trades or not all(PERSON in t for t in trades.values()):
        dq.emit("error", "taxonomy_mismatch",
                f"{what}: no per-trade {PERSON} facts; not the insider-trading form this "
                f"parser knows (schema {inst.schema_refs})", fetch_id=fetch_id)
        return empty
    symbol = _text(main.get("Symbol"))
    if symbol is not None and symbol != ref["symbol"]:
        dq.emit("error", "insider_trade_symbol_mismatch",
                f"{what}: the XBRL is for {symbol}; not loaded", fetch_id=fetch_id)
        return empty
    revised = (_text(main.get("RevisedFilling")) or "").lower() == "true"
    if revised != (ref["submission_type"] == "Revision"):
        dq.emit("warn", "insider_trade_revision_mismatch",
                f"{what}: listed as {ref['submission_type']}, XBRL says RevisedFilling="
                f"{main.get('RevisedFilling')}; the listing is used", fetch_id=fetch_id)
    filed: dt.datetime = ref["filed_at"]
    out, unknown = [], {"tdpTransactionType": set(), "acqMode": set(), "personCategory": set()}
    for ctx in sorted(trades, key=lambda c: (len(c), c)):
        t = trades[ctx]
        v = {k: _text(t.get(concept)) for k, concept in TRADE.items()}
        person = _text(t[PERSON]) or ""
        label = f"{what}: {person}"
        quantity, value = _number(v["quantity"]), _number(v["value_inr"])
        trade_from, intimated = parse_date(v["trade_from"]), parse_date(v["intimated_on"])
        if not person or quantity is None or trade_from is None:
            dq.emit("warn", "insider_trade_incomplete",
                    f"{label}: no person, quantity or trade date; row skipped",
                    fetch_id=fetch_id)
            continue
        if intimated is not None and filed.date() < intimated:
            dq.emit("warn", "insider_trade_time_order",
                    f"{label}: broadcast {filed:%Y-%m-%d} before intimation {intimated}; "
                    "row skipped", fetch_id=fetch_id)
            continue
        side, open_market, role = classify_insider_trade(
            v["transaction_type"], v["acquisition_mode"], v["person_category"], unknown)
        out.append({
            "exchange": "NSE", "symbol": ref["symbol"],
            "company_name": _text(main.get("NameOfTheCompany")) or ref.get("company_name"),
            "person_name": person, "person_category": v["person_category"],
            "insider_role": role,
            "regulation": _text(main.get("DisclosureUnderRegulation")) or ref.get("regulation"),
            "security_type": v["security_type"], "transaction_type": v["transaction_type"],
            "acquisition_mode": v["acquisition_mode"], "side": side,
            "open_market": open_market, "quantity": quantity, "value_inr": value,
            "holding_before_pct": _pct(v["holding_before"], label, dq, fetch_id),
            "holding_after_pct": _pct(v["holding_after"], label, dq, fetch_id),
            "trade_from": trade_from, "trade_to": parse_date(v["trade_to"]),
            "intimated_on": intimated, "filed_at": filed, "xbrl_url": ref["document_url"],
            "disclosure_id": ref["disclosure_id"], "submission_type": ref["submission_type"]})
    report_unknown_insider_values(unknown, dq, fetch_id)
    return pl.DataFrame(out, schema=INSIDER_SCHEMA)
