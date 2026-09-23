from __future__ import annotations

import datetime as dt
import json

import documented_payloads as P
import polars as pl
import pytest

from igs.dq import DQLog
from igs.normalize import masters, nse


def test_date_spellings():
    for s in ("2024-07-08", "08-07-2024", "08-Jul-2024", "08-JUL-2024", "08JUL2024", "08072024",
              "8-Jul-2024"):
        assert nse.parse_date(s) == dt.date(2024, 7, 8), s
    assert nse.parse_date("-") is None
    with pytest.raises(nse.ParseError):
        nse.parse_date("July 8")


def test_ist_timestamps():
    ts = nse.parse_ist_timestamp("12-Jul-2024 19:02:13")
    assert ts.isoformat() == "2024-07-12T19:02:13+05:30"
    # Date-only values are conservatively placed at the end of the day.
    assert nse.parse_ist_timestamp("12-Jul-2024").hour == 23


def test_udiff_keeps_only_final_session():
    dq = DQLog()
    kept, counts = nse.parse_bhavcopy_udiff(P.udiff(dt.date(2024, 7, 8)), ["F1", "F2"], dq)
    assert kept.height == 3
    assert set(kept["session_id"]) == {"F1"}
    assert dict(counts.iter_rows()) == {"F1": 3, "I1": 3}
    acme = kept.filter(pl.col("symbol") == "ACME").row(0, named=True)
    assert acme["isin"] == P.ACME_OLD and acme["close"] == pytest.approx(505.0)


def test_udiff_rejects_ambiguous_final_sessions():
    raw = P.udiff(dt.date(2024, 7, 8))
    with pytest.raises(nse.ParseError, match="more than one final session"):
        nse.parse_bhavcopy_udiff(raw, ["F1", "I1"], DQLog())


def test_udiff_missing_column_is_loud():
    bad = P._zip("x.csv", P._csv(["TradDt", "ISIN"], [["2024-07-08", "X"]]))
    with pytest.raises(nse.SchemaMismatch, match="missing columns"):
        nse.parse_bhavcopy_udiff(bad, ["F1"], DQLog())


def test_legacy_bhavcopy():
    df = nse.parse_bhavcopy_legacy(P.legacy(dt.date(2024, 7, 1)))
    assert df.height == 3 and df["trade_date"].unique().to_list() == [dt.date(2024, 7, 1)]
    assert df.schema == pl.Schema(nse.PRICE_SCHEMA)


def test_delivery_sources_agree():
    d = dt.date(2024, 7, 9)
    a = nse.parse_sec_bhavdata_full(P.sec_full(d)).sort("symbol")
    b = nse.parse_mto(P.mto(d), d).sort("symbol")
    assert a.select("symbol", "delivery_qty").equals(b.select("symbol", "delivery_qty"))
    assert a["delivery_pct"].to_list() == pytest.approx(b["delivery_pct"].to_list())


def test_equity_list_strips_padded_headers():
    df = nse.parse_equity_list(P.equity_list())
    assert df["isin"].to_list() == [P.ACME_NEW, P.BETA, P.GAMMA]
    assert df["listed_on"][0] == dt.date(2008, 10, 6)


@pytest.mark.parametrize("subject,fv,expected", [
    ("Bonus 1:1", 10, [{"action_type": "bonus", "ratio_a": 1.0, "ratio_b": 1.0}]),
    ("Bonus 3:2", 10, [{"action_type": "bonus", "ratio_a": 3.0, "ratio_b": 2.0}]),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", 10,
     [{"action_type": "split", "fv_old": 10.0, "fv_new": 2.0}]),
    ("Face Value Split (Sub-Division) - From Re 1/- Per Share To Re 0.50/- Per Share", 1,
     [{"action_type": "split", "fv_old": 1.0, "fv_new": 0.5}]),
    ("Consolidation of shares - From Rs 1/- Per Share To Rs 10/- Per Share", 1,
     [{"action_type": "consolidation", "fv_old": 1.0, "fv_new": 10.0}]),
    ("Rights 1:4 @ Premium Rs 80/-", 10,
     [{"action_type": "rights", "ratio_a": 1.0, "ratio_b": 4.0, "issue_price": 90.0}]),
    ("Rights 2:7 @ Rs 150/-", 5,
     [{"action_type": "rights", "ratio_a": 2.0, "ratio_b": 7.0, "issue_price": 150.0}]),
    ("Interim Dividend - Rs 2 Per Share / Special Dividend - Rs 1.50 Per Share", 10,
     [{"action_type": "dividend", "cash_per_share": 2.0},
      {"action_type": "dividend", "cash_per_share": 1.5}]),
    ("Scheme of Arrangement", 10, [{"action_type": "demerger"}]),
    ("Annual General Meeting", 10, [{"action_type": "other"}]),
])
def test_corporate_action_subjects(subject, fv, expected):
    assert nse.parse_ca_subject(subject, fv) == expected


def test_corporate_actions_payload():
    dq = DQLog()
    df = nse.parse_corporate_actions(P.corporate_actions(), dq)
    assert df.sort("ex_date", "action_type")["action_type"].to_list() == [
        "dividend", "dividend", "split", "bonus"]
    split = df.filter(pl.col("action_type") == "split").row(0, named=True)
    assert split["announced_at"].isoformat() == "2024-06-20T17:05:00+05:30"
    assert dq.count() == 0


def test_surveillance_walks_nested_lists():
    dq = DQLog()
    df = nse.parse_surveillance(P.asm(), "ASM", dt.date(2024, 7, 16), dq)
    assert df.to_dicts() == [{"measure": "ASM", "list_name": "longterm.data", "symbol": "GAMMA",
                              "isin": P.GAMMA, "stage": "Stage II",
                              "effective_from": dt.date(2024, 7, 16)}]
    empty = nse.parse_surveillance(P.gsm(empty=True), "GSM", dt.date(2024, 7, 16), dq)
    assert empty.height == 0 and dq.count("error") == 1   # loud, not silent


def test_holidays_index_announcements_quote():
    assert nse.parse_holidays(P.holidays())["holiday_date"].to_list() == [dt.date(2024, 7, 17)]
    idx = nse.parse_index_close_all(P.index_close(dt.date(2024, 7, 8)))
    assert idx.filter(pl.col("index_name") == "Nifty 500")["close"][0] == 22050
    ann = nse.parse_announcements(P.announcements(), DQLog())
    assert ann["filed_at"][0].isoformat() == "2024-07-12T19:02:13+05:30"
    assert nse.parse_quote_classification(P.quote("BETA"))["basic_industry"] == \
        "Specialty Chemicals"
    with pytest.raises(nse.SchemaMismatch):
        nse.parse_quote_classification(json.dumps({"info": {}}).encode())


def test_bse_and_angel_masters():
    bse = masters.parse_bse_scrips(P.bse_scrips())
    assert bse["scrip_group"].to_list() == ["B", "A"]
    angel = masters.parse_angel_master(P.angel_master())
    assert angel["symbol"].to_list() == ["ACME", "BETA"]     # derivatives dropped


def test_screener_csv_maps_on_codes():
    csv_bytes = b"S.No.,Name,NSE Code,BSE Code,ROCE %\n1,Acme,ACME,500111,21.5\n"
    df = masters.parse_screener_csv(csv_bytes)
    assert df.to_dicts() == [
        {"nse_code": "ACME", "bse_code": "500111", "field": "Name", "period_label": None,
         "value_text": "Acme", "value_num": None},
        {"nse_code": "ACME", "bse_code": "500111", "field": "ROCE %", "period_label": None,
         "value_text": "21.5", "value_num": 21.5}]
    with pytest.raises(nse.SchemaMismatch, match="NSE Code"):
        masters.parse_screener_csv(b"Name,ROCE\nAcme,1\n")
