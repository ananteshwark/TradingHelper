"""Parsers against real NSE payloads (tests/fixtures/real, captured 2026-09-23).

These pin what the live exchange actually served, including the quirks found on
first contact: dividend subjects truncated to "Per Sh", every XBRL column
declaring the same period, values in full rupees with "Lakhs" only as the
presentation level.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import polars as pl
import pytest

from igs.config import load_sources
from igs.dq import DQLog
from igs.normalize import nse
from igs.timeutil import IST
from igs.xbrl.instance import parse_instance
from igs.xbrl.listing import parse_listing
from igs.xbrl.results import extract_results

REAL = Path(__file__).parent / "fixtures" / "real"


def _b(name: str) -> bytes:
    return (REAL / name).read_bytes()


def _row(df: pl.DataFrame, **eq) -> dict:
    cond = pl.lit(True)
    for k, v in eq.items():
        cond = cond & (pl.col(k) == v)
    hit = df.filter(cond)
    assert hit.height == 1, (eq, hit.height)
    return hit.row(0, named=True)


def test_udiff_and_legacy_bhavcopy():
    dq = DQLog()
    finals = load_sources().get("nse_cm_bhavcopy_udiff").options.get("final_sessions", ["F1"])
    kept, _ = nse.parse_bhavcopy_udiff(_b("BhavCopy_NSE_CM_0_0_0_20260918_F_0000.csv.zip"),
                                       finals, dq)
    assert kept.height == 3660 and set(kept["trade_date"]) == {dt.date(2026, 9, 18)}
    assert dq.count("error") == 0 and kept["isin"].null_count() == 0
    legacy = nse.parse_bhavcopy_legacy(_b("cm28JUN2024bhav.csv.zip"))
    assert legacy.height == 2765 and set(legacy["trade_date"]) == {dt.date(2024, 6, 28)}


def test_delivery_files():
    sec = nse.parse_sec_bhavdata_full(_b("sec_bhavdata_full_22092026.csv"))
    r = _row(sec, symbol="20MICRONS", series="EQ")
    assert (r["traded_qty"], r["delivery_qty"], r["delivery_pct"]) == (163799, 47761, 29.16)
    mto = nse.parse_mto(_b("MTO_22092026.DAT"), dt.date(2026, 9, 22))
    r = _row(mto, symbol="0MOFSL27", series="N3")
    assert (r["traded_qty"], r["delivery_qty"], r["delivery_pct"]) == (13, 13, 100.0)


def test_equity_list_index_closes_and_holidays():
    eq = nse.parse_equity_list(_b("EQUITY_L.csv"))
    r = _row(eq, symbol="20MICRONS")
    assert r["isin"] == "INE144J01027" and r["listed_on"] == dt.date(2008, 10, 6)
    assert r["face_value"] == 5.0
    idx = nse.parse_index_close_all(_b("ind_close_all_22092026.csv"))
    assert idx.height == 166
    assert _row(idx, index_name="Nifty 50")["close"] == 23329.0
    hol = nse.parse_holidays(_b("holiday_master_trading.json"))
    assert hol.height == 20 and dt.date(2026, 1, 26) in set(hol["holiday_date"])


def test_corporate_actions_including_truncated_subjects():
    dq = DQLog()
    ca = nse.parse_corporate_actions(_b("corporate_actions_2026-09-16_2026-09-23.json"), dq)
    assert ca.height == 248 and dq.count("warn") == 0 and dq.count("error") == 0
    assert _row(ca, symbol="MACPOWER")["cash_per_share"] == 1.5
    assert _row(ca, symbol="EMMVEE")["action_type"] == "dividend"   # "... Per Sh"


def test_surveillance_lists():
    asm = nse.parse_surveillance(_b("reportASM.json"), "ASM", dt.date(2026, 9, 23), DQLog())
    gsm = nse.parse_surveillance(_b("reportGSM.json"), "GSM", dt.date(2026, 9, 23), DQLog())
    assert asm.height == 201 and gsm.height == 77
    assert _row(asm, symbol="A2ZINFRA")["stage"] == "Stage I"
    assert _row(gsm, symbol="AGSTRA")["stage"] == "LXII"


def test_listings_and_announcements():
    src = load_sources()
    o = src.get("nse_financial_results_index").options
    res = parse_listing(_b("financial_results_listing_first50.json"), o["filing_type"],
                        o["filing_system"], o["allowed_hosts"], DQLog())
    first = res.row(0, named=True)
    assert first["symbol"] == "VSTTILLERS" and first["basis_hint"] == "consolidated"
    assert first["period_end"] == dt.date(2024, 12, 31)
    # Filed in July 2026 for the December-2024 quarter: known only from then.
    assert first["filed_at"] == dt.datetime(2026, 7, 30, 17, 17, 53, tzinfo=IST)
    o = src.get("nse_shareholding_index").options
    shp = parse_listing(_b("shareholding_listing_first50.json"), o["filing_type"],
                        o["filing_system"], o["allowed_hosts"], DQLog())
    assert shp.height > 0 and shp["document_url"].drop_nulls().str.ends_with(".xml").all()
    ann = nse.parse_announcements(_b("announcements_first50.json"), DQLog())
    assert ann.height == 50 and ann["filed_at"].null_count() == 0
    # NSE's single-level industry label and the ISIN ride on each announcement; 32 of these
    # 50 carry no label.
    r = _row(ann, symbol="ULTRACEMCO")
    assert (r["industry_label"], r["isin"]) == ("Cement And Cement Products", "INE481G01011")
    assert ann["industry_label"].null_count() == 32 and ann["isin"].null_count() == 0


def test_real_results_xbrl():
    dq = DQLog()
    rf = extract_results(parse_instance(_b("results_ICDSLTD_2024Q3_standalone.xml")), dq)
    assert (rf.taxonomy_version, rf.statement_basis) == ("2022", "standalone")
    assert (rf.period_start, rf.period_end) == (dt.date(2024, 10, 1), dt.date(2024, 12, 31))
    assert rf.metadata["rounding_level"] == "Lakhs"          # presentation only
    assert rf.metadata["audit_opinion"] == "Not applicable"
    v = {f["concept"]: f["value"] for f in rf.facts}
    assert {f["period_type"] for f in rf.facts} == {"Q"}      # 9-month column not stored
    # Full rupees: Rs 33.27 lakh revenue, Rs 92.37 lakh loss.
    assert v["revenue"] == 3_327_000.0 and v["pat"] == -9_237_000.0
    # The EPS cross-check holds on real numbers: -0.71 x (Rs 13.03 cr / Rs 10).
    implied = v["eps_basic"] * v["paid_up_equity_capital"] / v["face_value"]
    assert implied == pytest.approx(v["pat"], rel=0.01)
    cats = {i.category for i in dq.issues}
    assert "xbrl_conflicting_values" not in cats and dq.count("error") == 0


def _results(name: str) -> tuple:
    dq = DQLog()
    rf = extract_results(parse_instance(_b(name)), dq)
    values = {(f["concept"], f["period_type"]): f["value"] for f in rf.facts}
    return rf, values, dq


def _identity_breaks(rf) -> pl.DataFrame:
    from igs.xbrl.checks import identity_breaks
    return identity_breaks(pl.DataFrame([{**f, "filing_id": 1, "company_id": 1,
                                          "statement_basis": rf.statement_basis}
                                         for f in rf.facts]))


def test_integrated_filing_results_xbrl():
    """Integrated Filing (Financials), 2026-01-31 taxonomy: an audited consolidated Q4 with
    the year, balance sheet and cash flow."""
    rf, v, dq = _results("results_integrated_PNCINFRA_2026Q4_consolidated.xml")
    assert (rf.taxonomy_version, rf.statement_basis, rf.results_format) == (
        "2024", "consolidated", "default")
    assert (rf.period_start, rf.period_end) == (dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    assert rf.metadata["rounding_level"] == "Lakhs" and rf.metadata["audited"] == "Audited"
    assert {f["period_type"] for f in rf.facts} == {"Q", "FY", "INSTANT"}
    # Rs 1,616.98 cr revenue in the quarter, Rs 5,368.10 cr in the year.
    assert v["revenue", "Q"] == 16_169_829_000.0 and v["revenue", "FY"] == 53_681_016_000.0
    assert v["cfo", "FY"] == 45_929_651_000.0 and ("cfo", "Q") not in v
    assert v["total_assets", "INSTANT"] == v["equity_and_liabilities", "INSTANT"]
    assert _identity_breaks(rf).height == 0
    # Exceptional items are signed as reported: PBT = before-exceptional + exceptional.
    assert v["pbt", "FY"] == v["pbt_before_exceptional", "FY"] + v["exceptional_items", "FY"]
    shares = v["paid_up_equity_capital", "Q"] / v["face_value", "Q"]
    assert v["pat_owners", "FY"] / shares == pytest.approx(v["eps_basic", "FY"], abs=0.01)
    assert dq.count("warn") == 0 and dq.count("error") == 0

    rf, v, dq = _results("results_integrated_ANNU_2026Q1_standalone.xml")
    assert (rf.statement_basis, rf.period_end, rf.metadata["audited"]) == (
        "standalone", dt.date(2026, 6, 30), "Unaudited")
    assert {f["period_type"] for f in rf.facts} == {"Q"}   # Q1: year to date is the quarter
    assert v["revenue", "Q"] == 199_100_000.0 and _identity_breaks(rf).height == 0


def test_integrated_filing_nbfc_is_not_a_bank():
    """NBFCs tag interest income as InterestEarned, the bank top-line element; the form comes
    from the entry-point namespace (IntegratedFinance_NBFC)."""
    rf, v, _ = _results("results_integrated_ARIHANTCAP_2026Q4_consolidated_nbfc.xml")
    assert rf.results_format == "nbfc"
    assert v["interest_earned", "FY"] == 799_858_000.0
    assert v["revenue", "FY"] == 2_058_399_000.0 and v["pat", "FY"] == 314_625_000.0
    assert _identity_breaks(rf).height == 0
    # Without the namespace the line items still say NBFC: revenue beside interest earned,
    # and impairment on financial instruments.
    from igs.xbrl.results import results_form
    inst = parse_instance(_b("results_integrated_ARIHANTCAP_2026Q4_consolidated_nbfc.xml"))
    inst.namespaces = ()
    assert results_form(inst, {c for c, _ in v}) == "nbfc"


def test_metadata_is_taken_from_the_current_column_whatever_the_order():
    """Q4 filings state the reporting period for the quarter (OneD) and the year (FourD)."""
    raw = _b("results_integrated_PNCINFRA_2026Q4_consolidated.xml")
    quarter_start = (b'<in-capmkt:DateOfStartOfReportingPeriod contextRef="OneD">'
                     b'2026-01-01</in-capmkt:DateOfStartOfReportingPeriod>')
    assert raw.count(quarter_start) == 1
    moved = raw.replace(quarter_start, b"").replace(b"</xbrli:xbrl>",
                                                    quarter_start + b"</xbrli:xbrl>")
    rf = extract_results(parse_instance(moved), DQLog())
    assert (rf.period_start, rf.period_end) == (dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    assert {f["period_type"] for f in rf.facts} == {"Q", "FY", "INSTANT"}


def test_integrated_filing_listing():
    """The first page of NSE's Integrated Filing (Financials) listing, as served to a browser
    on 2026-09-23 and pasted from it."""
    o = load_sources().get("nse_integrated_filing_index").options
    dq = DQLog()
    content = _b("integrated_filing_listing_p1.json")
    df = parse_listing(content, o["filing_type"], o["filing_system"], o["allowed_hosts"], dq,
                       listing_keys=o["listing_keys"])
    assert df.height == 20 and dq.count() == 0
    assert df["document_url"].str.ends_with("_WEB.xml").all() and df["filed_at_precise"].all()
    first = df.row(0, named=True)
    assert (first["symbol"], first["period_end"], first["basis_hint"], first["exchange_ref"]) \
        == ("WINSOME", dt.date(2026, 6, 30), "standalone", "195779")
    # creation_Date, not the 19:18:03 broadcast: the XBRL (named ..._23092026071929_...)
    # did not exist before then.
    assert first["filed_at"] == dt.datetime(2026, 9, 23, 19, 19, 30, tzinfo=IST)
    # A revision of the March-2025 quarter filed in September 2026 (no broadcast time).
    rev = _row(df, symbol="MFML")
    assert rev["period_end"] == dt.date(2025, 3, 31) and rev["basis_hint"] == "consolidated"
    assert rev["filed_at"] == dt.datetime(2026, 9, 23, 18, 20, 59, tzinfo=IST)
    assert "INTEGRATED_FILING_NBFC_INDAS" in _row(df, symbol="ARIHANTCAP")["document_url"]
    # Both statements of a company are separate references.
    assert set(df.filter(pl.col("symbol") == "PNCINFRA")["basis_hint"]) == {
        "standalone", "consolidated"}

    other = json.loads(content)
    other["data"][0]["type"] = "Integrated Filing- Governance"
    dq = DQLog()
    df = parse_listing(json.dumps(other).encode(), o["filing_type"], o["filing_system"],
                       o["allowed_hosts"], dq, listing_keys=o["listing_keys"])
    assert df.height == 19 and [i.category for i in dq.issues] == ["listing_row_unexpected"]
