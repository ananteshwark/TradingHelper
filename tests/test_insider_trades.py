"""Insider-trading disclosures (SEBI PIT): parser, known-at rule, the insider-buying factor,
the database round trip and the watchlist alert."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest
import synthetic_market as M

import igs.factors  # noqa: F401  (registers factors)
from igs.dq import DQLog
from igs.factors import base
from igs.factors.registry import REGISTRY
from igs.normalize.nse import INSIDER_REQUIRED, SchemaMismatch, parse_insider_trades
from igs.pit import PitView
from igs.pit.knowledge import KNOWN_AT, with_known_at
from igs.timeutil import IST


def _row(**over) -> dict:
    row = {"symbol": "GROW", "company": "Grow Industries Ltd", "anex": "7(2)",
           "acqName": "Promoter One", "personCategory": "Promoters", "secType": "Equity Shares",
           "secAcq": "2,00,000", "secVal": "50000000", "tdpTransactionType": "Buy",
           "acqMode": "Market Purchase", "acqfromDt": "01-Oct-2024", "acqtoDt": "01-Oct-2024",
           "intimDt": "02-Oct-2024", "date": "03-Oct-2024 18:30", "befAcqSharesPer": "54.6",
           "afterAcqSharesPer": "55.0", "xbrl": "https://example.invalid/pit.xml"}
    row.update(over)
    return row


def _payload(*rows: dict) -> bytes:
    return json.dumps({"acqNameList": [], "data": list(rows)}).encode()


# --------------------------------------------------------------------------- parser


def test_parser_normalises_and_dates_by_broadcast():
    dq = DQLog()
    df = parse_insider_trades(_payload(
        _row(),
        _row(acqName="Promoter Two", tdpTransactionType="Sell", acqMode="Market Sale"),
        _row(acqName="A Director", personCategory="Director", acqMode="ESOP"),
        _row(acqName="An Employee", personCategory="Designated Persons")), dq)
    assert df.height == 4 and not dq.issues
    first = df.row(0, named=True)
    assert first["filed_at"] == dt.datetime(2024, 10, 3, 18, 30, tzinfo=IST)
    assert first["quantity"] == 200000 and first["value_inr"] == 5e7
    assert (first["side"], first["open_market"], first["insider_role"]) == \
        ("buy", True, "promoter")
    assert df["side"].to_list() == ["buy", "sell", "buy", "buy"]
    assert df["open_market"].to_list() == [True, True, False, True]
    assert df["insider_role"].to_list() == ["promoter", "promoter", "director_kmp", "other"]
    # Known at the broadcast, never the trade or intimation date.
    k = with_known_at("insider_trades", df.with_columns(
        pl.col("filed_at").dt.convert_time_zone("UTC")))
    assert k[KNOWN_AT][0] == dt.datetime(2024, 10, 3, 13, 0, tzinfo=dt.UTC)


def test_parser_refuses_a_row_without_the_expected_keys():
    row = _row()
    del row["acqMode"]
    with pytest.raises(SchemaMismatch, match="acqMode") as exc:
        parse_insider_trades(_payload(row), DQLog())
    assert "its keys are" in str(exc.value)
    assert set(INSIDER_REQUIRED) <= set(_row())


def test_parser_reports_what_it_cannot_trust():
    dq = DQLog()
    df = parse_insider_trades(_payload(
        _row(date="-"),                                             # no broadcast time
        _row(date="01-Oct-2024 10:00", intimDt="02-Oct-2024"),      # broadcast before intimation
        _row(acqMode="Market Buy", personCategory="Relative of promoter")), dq)
    assert df.height == 1                                           # only the third row
    cats = [i.category for i in dq.issues]
    assert cats.count("insider_trade_incomplete") == 1
    assert cats.count("insider_trade_time_order") == 1
    unknown = [i.message for i in dq.issues if i.category == "insider_trade_unknown_value"]
    assert any("Market Buy" in m for m in unknown)
    # An unrecognised mode is kept verbatim and never counted as open market.
    r = df.row(0, named=True)
    assert r["acquisition_mode"] == "Market Buy" and not r["open_market"]
    assert parse_insider_trades(_payload(), DQLog()).height == 0


# --------------------------------------------------------------------------- factor


def test_insider_buying_counts_disclosed_open_market_purchases_only():
    ds = M.build()
    as_of = M.GATE_DATES[-1]                        # 2024-11-29 23:59 IST
    view = PitView(ds, as_of)
    out = {r["company_id"]: r for r in
           REGISTRY["insider_buying_90d"].fn(view).iter_rows(named=True)}
    mcap = dict(base.market_cap(PitView(ds, as_of)).select("company_id", "mcap").iter_rows())
    # GROW: the promoter's Oct-2024 purchase (the Jul-2022 one is outside 90 days).
    assert out[1]["value"] == pytest.approx(5e7 / mcap[1] * 100, rel=1e-12)
    # NBFC: only the September purchase; the one broadcast on 30 Nov is not known yet.
    assert out[4]["value"] == pytest.approx(1.2e7 / mcap[4] * 100, rel=1e-12)
    # A promoter sale, an ESOP allotment and an employee's purchase count for nothing.
    assert out[2]["value"] == out[3]["value"] == out[5]["value"] == 0.0
    assert json.loads(out[1]["detail"])["trades"] == 1


def test_insider_buying_needs_the_whole_window_loaded():
    ds = M.build()
    early = dt.datetime(2022, 1, 15, 23, 59, tzinfo=IST)   # first disclosure 2021-12-03
    out = REGISTRY["insider_buying_90d"].fn(PitView(ds, early))
    assert set(out["status"]) == {"insufficient_data"}        # not a zero meaning "not loaded"
    trap = dt.datetime(2023, 8, 24, 16, 59, tzinfo=IST)       # LATE's purchase broadcast 18:30
    late = {r["company_id"]: r for r in
            REGISTRY["insider_buying_90d"].fn(PitView(ds, trap)).iter_rows(named=True)}
    assert late[6]["value"] == 0.0


# --------------------------------------------------------------------------- database


@pytest.mark.db
def test_parsed_trades_reach_the_point_in_time_dataset(db_conn):
    import db_market

    from igs.normalize.load import load_insider_trades
    from igs.pit.loader import load_dataset
    db_market.load(db_conn)
    df = parse_insider_trades(_payload(_row(symbol="NBFC", acqName="Another Director",
                                            personCategory="Director",
                                            date="15-Nov-2024 19:05", intimDt="14-Nov-2024",
                                            acqfromDt="12-Nov-2024")), DQLog())
    n = load_insider_trades(db_conn, df, db_market.FETCH, dt.datetime.now(dt.UTC))
    assert n == 1
    assert load_insider_trades(db_conn, df, db_market.FETCH, dt.datetime.now(dt.UTC)) == 0
    ds = load_dataset(db_conn, dt.date(2021, 1, 1), db_market.AS_OF.date())
    t = ds.tables["insider_trades"].filter(pl.col("person_name") == "Another Director")
    assert t["company_id"].to_list() == [4]                   # symbol mapped to the company
    view = PitView(ds, db_market.AS_OF)
    v = {r["company_id"]: r["value"] for r in
         REGISTRY["insider_buying_90d"].fn(view).iter_rows(named=True)}
    mcap = dict(base.market_cap(view).select("company_id", "mcap").iter_rows())
    assert v[4] == pytest.approx((1.2e7 + 5e7) / mcap[4] * 100, rel=1e-9)


@pytest.mark.db
def test_watchlist_alert_on_insider_trades(db_conn):
    import db_market

    from igs.alerts.rules import watchlist_insider_trades
    from igs.guardrails import find_advice_language
    db_market.load(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("insert into watchlist (company_id) values (1), (2), (3)")
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    until = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    alerts = watchlist_insider_trades(db_conn, since, until, {"include_sells": True})
    msgs = sorted(a.message for a in alerts)
    assert len(msgs) == 3            # GROW x2 purchases, CYCL sale; BANK's ESOP is off market
    assert any("Promoter One (promoter) acquired 200,000 shares (Rs 5.00 cr)" in m
               for m in msgs)
    assert any("disposed of 500,000 shares" in m for m in msgs)
    assert not any(find_advice_language(m) for m in msgs)
    only_buys = watchlist_insider_trades(db_conn, since, until, {"include_sells": False})
    assert len(only_buys) == 2
