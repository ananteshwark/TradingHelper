"""Screener.in exports the owner downloads: the standard-library .xlsx reader, the Data
Sheet parser, the import (matched by name, stored once), the check against the app's
results filings, and what the AI is given. Nothing is fetched from Screener.in.

The workbooks are built in the layout Screener's Data Sheet is documented to have
(tests/xlsx_files.py); a real export has not been checked yet, which is why the parser
reads by shape rather than by a fixed list of labels."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest
import test_ai_calls
import xlsx_files

from igs import brokers, screener, service
from igs.dq import DQLog
from igs.ingest.manual import import_screener_bytes
from igs.ingest.raw_store import RawStore
from igs.normalize import masters, xlsx
from igs.normalize.nse import SchemaMismatch
from igs.timeutil import IST

scored = test_ai_calls.scored
APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"
JUN, SEP, DEC = dt.date(2024, 6, 30), dt.date(2024, 9, 30), dt.date(2024, 12, 31)


def test_cells_are_read_by_type_and_position():
    content = xlsx_files.workbook({"First": [["a", 1, None, True], [], [None, "b & c", 2.5]],
                                   "Second": [["x"]]})
    sheets, date1904 = xlsx.read(content)
    assert list(sheets) == ["First", "Second"] and not date1904
    assert sheets["First"] == [["a", 1.0, None, True], [None, "b & c", 2.5]]
    assert xlsx.read(xlsx_files.workbook({"S": [["text", 3]]}, inline=True))[0] == {
        "S": [["text", 3.0]]}
    with pytest.raises(xlsx.NotXlsx, match="not a zip"):
        xlsx.read(b"Name,ROCE\n")


def test_excel_dates_in_both_date_systems():
    day = dt.date(2024, 3, 31)
    assert xlsx.excel_date(45382.0) == day           # Excel's own number for 31 Mar 2024
    assert xlsx.excel_date(xlsx_files.serial(day, True), date1904=True) == day
    book = masters.parse_screener_workbook(xlsx_files.workbook(
        {"Data Sheet": [["Quarters"], ["Report Date", day], ["Sales", 5.0]]}, date1904=True))
    assert book["sections"]["Quarters"]["periods"] == ["2024-03-31"]


def test_a_screener_export_is_read_section_by_section():
    q = {JUN: (1692.95, 203.15), SEP: (1797.87, 215.74)}
    y = {dt.date(2023, 3, 31): (6000.0, 700.0), dt.date(2024, 3, 31): (6500.0, 780.0)}
    content = xlsx_files.screener_export("GROW INDUSTRIES LTD", q, y, price=812.5)
    book = masters.parse_screener_workbook(content)
    assert book["company_name"] == "GROW INDUSTRIES LTD"
    assert book["meta"]["Current Price"] == 812.5 and book["meta"]["Face Value"] == 10.0
    s = book["sections"]
    assert set(s) == {"PROFIT & LOSS", "Quarters", "BALANCE SHEET", "CASH FLOW", "PRICE",
                      "DERIVED"}
    assert s["Quarters"]["periods"] == ["2024-06-30", "2024-09-30"]
    assert s["Quarters"]["rows"]["Sales"] == [1692.95, 1797.87]
    assert s["PROFIT & LOSS"]["rows"]["Net profit"] == [700.0, 780.0]
    assert {"Total", "Total (2)"} <= set(s["BALANCE SHEET"]["rows"])   # liabilities, assets
    assert s["PRICE"] == {"periods": ["2023-03-31", "2024-03-31"],
                          "rows": {"PRICE": [812.5, 812.5]}}
    assert s["DERIVED"]["rows"]["Adjusted Equity Shares in Cr"] == [10.0, 10.0]
    df = masters.parse_screener_excel(content, "GROW", None)
    sales = df.filter((df["section"] == "Quarters") & (df["field"] == "Sales"))
    assert sales["period_label"].to_list() == ["2024-06-30", "2024-09-30"]
    assert df.filter(df["field"] == "Company name")["value_text"].to_list() == [
        "GROW INDUSTRIES LTD"]


def test_months_as_text_and_lines_without_figures():
    book = masters.parse_screener_workbook(xlsx_files.workbook({"Data Sheet": [
        ["COMPANY NAME", "X Ltd"], ["Quarters"], ["Report Date", "Jun-24", "Sep 2024"],
        ["Sales", 1.0, 2.0], ["New Bonus Shares"], ["Net profit", 0.5, 0.6]]}))
    q = book["sections"]["Quarters"]
    assert q["periods"] == ["2024-06-30", "2024-09-30"]
    assert q["rows"] == {"Sales": [1.0, 2.0], "New Bonus Shares": [None, None],
                         "Net profit": [0.5, 0.6]}


def test_other_workbooks_are_refused():
    with pytest.raises(SchemaMismatch, match="not a zip"):
        masters.parse_screener_workbook(b"hello")
    with pytest.raises(SchemaMismatch, match="no 'Data Sheet' tab"):
        masters.parse_screener_workbook(xlsx_files.workbook({"Sheet1": [["a"]]}))
    with pytest.raises(SchemaMismatch, match="no 'Report Date' row"):
        masters.parse_screener_workbook(xlsx_files.workbook(
            {"Data Sheet": [["COMPANY NAME", "X"], ["Sales", 1.0]]}))


def _crore(conn, cid: int, ptype: str, end: dt.date, concept: str) -> float:
    return conn.execute("""select value::float8 / 1e7 from facts_as_of(now())
                           where company_id = %s and period_type = %s and period_end = %s
                             and concept = %s and statement_basis = 'consolidated'""",
                        (cid, ptype, end, concept)).fetchone()[0]


def _grow_export(conn) -> bytes:
    """GROW's last two quarters as filed, except June's sales 5% higher, and a December
    quarter the app has no results for; the year's sales as filed (the synthetic filings
    have no annual profit, so that is a figure only the export has)."""
    q = {SEP: (_crore(conn, 1, "Q", SEP, "revenue"), _crore(conn, 1, "Q", SEP, "pat")),
         JUN: (_crore(conn, 1, "Q", JUN, "revenue") * 1.05, _crore(conn, 1, "Q", JUN, "pat")),
         DEC: (2100.0, 240.0)}
    y = {dt.date(2024, 3, 31): (_crore(conn, 1, "FY", dt.date(2024, 3, 31), "revenue"), 780.0)}
    return xlsx_files.screener_export("Grow Industries Limited", q, y, price=812.5)


@pytest.mark.db
def test_an_export_is_imported_once_and_checked_against_the_filings(scored, tmp_path):
    conn, run_id = scored
    store = RawStore(tmp_path)
    content = _grow_export(conn)
    got = import_screener_bytes(conn, store, content, "Grow Industries.xlsx", DQLog())
    conn.commit()
    assert (got.company_id, got.symbol, got.already) == (1, "GROW", False) and got.rows > 20
    again = import_screener_bytes(conn, store, content, "Grow Industries (1).xlsx", DQLog())
    assert again.already and again.company_id == 1 and "already imported" in str(again)
    assert len(screener.exports(conn, 1)) == 1

    c = screener.check(conn, 1)
    status = {(r["line"], r["period"]): r["status"] for r in c["rows"]}
    assert status[("Sales (quarter)", "2024-09-30")] == "agrees"
    assert status[("Net profit (quarter)", "2024-06-30")] == "agrees"
    assert status[("Sales (year)", "2024-03-31")] == "agrees"
    assert status[("Sales (quarter)", "2024-06-30")] == "differs"
    assert status[("Sales (quarter)", "2024-12-31")] == "only in Screener.in"
    assert [(r["period"], r["diff_pct"], r["basis"]) for r in c["differ"]] == [
        ("2024-06-30", 5.0, "consolidated")]
    text = screener.summary(c)
    assert text.startswith(f"{c['agree']} of {c['compared']} figures agree")
    assert "differ: Sales (quarter) 2024-06-30" in text and "only in Screener.in" in text

    # A name that matches two companies is refused; a symbol given decides.
    other = xlsx_files.screener_export("Example", {SEP: (1.0, 0.1)})
    with pytest.raises(ValueError, match="no single company matches the name 'Example'"):
        import_screener_bytes(conn, store, other, "Example.xlsx", DQLog())
    conn.rollback()
    got = import_screener_bytes(conn, store, other, "Example.xlsx", DQLog(), nse_code="BANK")
    assert (got.company_id, got.symbol) == (3, "BANK")


@pytest.mark.db
def test_the_ai_is_given_the_export_up_to_the_runs_date(scored, tmp_path, monkeypatch):
    conn, run_id = scored
    import_screener_bytes(conn, RawStore(tmp_path), _grow_export(conn), "g.xlsx", DQLog())
    conn.commit()
    run = service.resolve_run(conn, run_id)
    got = screener.ai_inputs(conn, 1, run)
    assert got["source"].startswith("Screener.in export") and got["unit"] == screener.UNIT
    # December's quarter ends after the run's date (29 Nov 2024), so it is left out.
    assert got["tables"]["Quarters"]["periods"] == ["2024-06-30", "2024-09-30"]
    assert got["tables"]["Quarters"]["lines"]["Net profit"][1] == pytest.approx(
        _crore(conn, 1, "Q", SEP, "pat"))
    assert got["at_download"]["Current Price"] == 812.5
    assert [r["period"] for r in got["check_against_filings"]["differ"]] == ["2024-06-30"]
    assert screener.ai_inputs(conn, 2, run) is None                  # no export for CYCL
    # An export imported after the next run belongs to that run, not this one.
    monkeypatch.setattr(screener, "_next_run", lambda conn, as_of: run["as_of"])
    assert screener.ai_inputs(conn, 1, run) is None


@pytest.mark.db
def test_stocks_with_thin_data_are_listed_until_an_export_arrives(scored, tmp_path):
    conn, run_id = scored
    today = dt.datetime.now(IST).date()
    brokers.add_manual(conn, "GAPS", "Jefferies", "buy", 500.0, today)   # not in the run
    brokers.add_manual(conn, "NBFC", "Jefferies", "buy", 500.0, today)   # in it, 30 quarters
    rows = screener.wanted(conn, run_id)
    assert [(r["symbol"], r["why"]) for r in rows] == [("GAPS", "outside the ranking")]
    assert rows[0]["url"] == "https://www.screener.in/company/GAPS/"
    import_screener_bytes(conn, RawStore(tmp_path), xlsx_files.screener_export(
        "Gaps Engineering Ltd", {SEP: (100.0, 10.0)}), "Gaps.xlsx", DQLog())
    conn.commit()
    assert screener.wanted(conn, run_id) == []


@pytest.mark.db
def test_the_stock_page_checks_an_uploaded_export(scored, tmp_path, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest
    conn, _ = scored
    monkeypatch.setenv("IGS_RAW_ROOT", str(tmp_path))
    import_screener_bytes(conn, RawStore(tmp_path), _grow_export(conn), "Grow.xlsx", DQLog())
    conn.commit()
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "Stock"
    at.session_state["stock_sym"] = "GROW"
    at.run()
    assert not at.exception, at.exception
    assert any(s.value == "Screener.in check" for s in at.subheader)
    table = next(d.value for d in at.dataframe if "Screener.in (Rs cr)" in d.value.columns)
    assert "differs" in table[""].to_list() and "agrees" in table[""].to_list()
    assert any("Grow.xlsx, imported" in m.value and "; 1 differ;" in m.value
               for m in at.markdown)
    at.session_state["page"] = "Stock"
    at.session_state["stock_sym"] = "CYCL"
    at.run()
    assert any("No export imported" in m.value and "screener.in/company/CYCL/" in m.value
               for m in at.markdown)
