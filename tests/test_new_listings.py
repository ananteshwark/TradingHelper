"""New listings are findable and viewable right after they list. Asked for by the owner,
who could not find Manipal Payment and Identity Solutions (listed on NSE 17 Sep 2026,
symbol MPIMANIPAL) in the stock search.

The instrument master is built from price files, and names came only from NSE's equity
list, so a new listing had no searchable name until that list had it, and no entry at all
before its first price file. Now the price file's own name is kept as a fallback, and
equity-list companies without prices yet can be searched and opened."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl
import pytest

from igs import service
from igs.config import load_sources
from igs.dq import DQLog
from igs.normalize import nse
from igs.normalize.load import load_prices
from igs.normalize.master_db import rebuild_instrument_master

REAL = Path(__file__).resolve().parent / "fixtures" / "real"
APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"


def _prices():
    finals = load_sources().get("nse_cm_bhavcopy_udiff").options.get("final_sessions", ["F1"])
    kept, _ = nse.parse_bhavcopy_udiff(
        (REAL / "BhavCopy_NSE_CM_0_0_0_20260918_F_0000.csv.zip").read_bytes(), finals,
        DQLog())
    return kept


def test_the_price_file_names_every_security():
    row = _prices().filter(symbol="MPIMANIPAL").row(0, named=True)
    assert (row["series"], row["isin"], row["security_name"]) == (
        "EQ", "INE241U01028", "MANIPAL PAYMENT & IDE S L")


def _raw(conn, fetch_id):
    conn.execute("""insert into raw_payload (fetch_id, source_id, fetched_at, content_sha256,
                        size_bytes, blob_path, origin)
                    values (%s, 's', now(), repeat('a', 64), 1, 'blobs/aa/a', 'http')""",
                 (fetch_id,))


@pytest.fixture
def listed(db_conn):
    """The real 18 Sep 2026 price file for Manipal and one older company, loaded and
    turned into the instrument master, with no NSE equity list yet."""
    _raw(db_conn, "bhav__20260918")
    load_prices(db_conn, _prices().filter(pl.col("symbol").is_in(["MPIMANIPAL", "20MICRONS"])),
                "bhav__20260918", DQLog())
    rebuild_instrument_master(db_conn, DQLog())
    db_conn.commit()
    return db_conn


@pytest.mark.db
def test_a_new_listing_is_found_by_name_from_its_first_price_file(listed):
    conn = listed
    found = service.companies(conn, q="manipal payment")
    assert [(c["symbol"], c["name"]) for c in found] == [
        ("MPIMANIPAL", "Manipal Payment & Ide S L")]
    basic = service.stock_basic(conn, "MPIMANIPAL")
    assert basic["quarters"] == 0 and basic["listing"] is None
    assert [p["close"] for p in basic["prices"]] == [341.35]
    # Once NSE's equity list has it, its full name is used from the next rebuild.
    _raw(conn, "eqlist__20260930")
    conn.execute("""insert into nse_equity_list (symbol, isin, company_name, series,
                        listed_on, face_value, snapshot_date, source_fetch_id)
                    values ('MPIMANIPAL', 'INE241U01028',
                            'Manipal Payment and Identity Solutions Limited', 'EQ',
                            '2026-09-17', 10, '2026-09-30', 'eqlist__20260930')""")
    rebuild_instrument_master(conn, DQLog())
    conn.commit()
    found = service.companies(conn, q="Manipal Payment and Identity Solutions Ltd")
    assert [c["name"] for c in found] == ["Manipal Payment and Identity Solutions Limited"]
    assert service.stock_basic(conn, "mpimanipal")["listing"]["listed_on"] == \
        dt.date(2026, 9, 17)


@pytest.mark.db
def test_a_listing_without_prices_yet_can_be_searched(listed):
    conn = listed
    _raw(conn, "eqlist__20260930")
    conn.execute("""insert into nse_equity_list (symbol, isin, company_name, series,
                        listed_on, face_value, snapshot_date, source_fetch_id)
                    values ('NEWCO', 'INE000N01010', 'Brand New Listing Limited', 'EQ',
                            '2026-09-30', 10, '2026-09-30', 'eqlist__20260930')""")
    conn.commit()
    found = service.companies(conn, q="brand new")
    assert [(c["symbol"], c["company_id"], c["in_run"]) for c in found] == [
        ("NEWCO", None, False)]
    basic = service.stock_basic(conn, "NEWCO")
    assert basic["company_id"] is None and basic["prices"] == []
    assert service.stock_basic(conn, "NOSUCH") is None
    assert service.data_dates(conn) == {"prices": dt.date(2026, 9, 18),
                                        "equity_list": dt.date(2026, 9, 30)}


@pytest.mark.db
def test_the_stock_page_shows_a_stock_outside_the_ranking(scored_with_listing, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "Stock"
    at.session_state["stock_sym"] = "MPIMANIPAL"
    at.run()
    assert not at.exception, at.exception
    assert "Manipal Payment & Ide S L (MPIMANIPAL)" in at.selectbox(key="stock_sym").options
    assert any(h.value == "Manipal Payment & Ide S L (MPIMANIPAL)" for h in at.header)
    assert any("Not in run" in i.value and "0 loaded" in i.value for i in at.info)
    assert any(m.label == "Last close" and m.value == "Rs 341.35" for m in at.metric)
    assert any("prices up to 18 Sep 2026" in c.value for c in at.caption)
    at.button(key="watch_btn").click().run()
    assert [w["symbol"] for w in service.watchlist(scored_with_listing)] == ["MPIMANIPAL"]


@pytest.fixture
def scored_with_listing(db_conn, tmp_path, monkeypatch):
    """The synthetic market scored, plus Manipal's first price file."""
    import test_ai_calls
    conn, _ = test_ai_calls.scored.__wrapped__(db_conn, tmp_path, monkeypatch)
    _raw(conn, "bhav__20260918")
    load_prices(conn, _prices().filter(symbol="MPIMANIPAL"), "bhav__20260918", DQLog())
    rebuild_instrument_master(conn, DQLog())
    conn.commit()
    return conn
