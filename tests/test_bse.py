"""Stocks listed only on BSE (docs/BSE.md): BSE's price file, the companies made from it,
their prices in scoring, their splits and bonuses read from the prices, and the pages."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

from igs import service
from igs.config import load_sources, load_universe
from igs.dq import DQLog
from igs.ingest import jobs
from igs.normalize import nse
from igs.normalize.adjust import implied_actions, snap_factor
from igs.normalize.load import load_prices
from igs.normalize.master_db import rebuild_instrument_master

REAL = Path(__file__).resolve().parent / "fixtures" / "real"
BSE_FILE = REAL / "BhavCopy_BSE_CM_0_0_0_20261120_F_0000_trimmed.csv"
NSE_FILE = REAL / "BhavCopy_NSE_CM_0_0_0_20260918_F_0000.csv.zip"
DAY = dt.date(2026, 11, 20)


def _bse(content: bytes | None = None) -> pl.DataFrame:
    finals = load_sources().get(jobs.BSE_PRICES).options["final_sessions"]
    kept, _ = nse.parse_bhavcopy_udiff(content or BSE_FILE.read_bytes(), finals, DQLog(),
                                       exchange="BSE", symbol_column="FinInstrmId")
    return kept


def test_bses_file_is_read_with_its_scrip_code_and_group():
    px = _bse()
    assert px.height == 8 and set(px["exchange"]) == {"BSE"}
    row = px.filter(isin="INE721I01024").row(0, named=True)
    assert (row["symbol"], row["series"], row["close"], row["security_name"]) == (
        "544937", "A", 2220.30, "National Stock Exchange of Ind")
    assert row["turnover_inr"] == 17293798797.0 and row["trades"] == 210667


def test_a_web_page_in_place_of_the_file_is_no_file():
    page = b"<!DOCTYPE html><html lang=\"en\"><head><title>BSE</title>"
    assert jobs.web_page(page) and jobs.web_page(b"\n  <html>") and not jobs.web_page(
        BSE_FILE.read_bytes())
    spec = load_sources().get(jobs.BSE_PRICES)
    assert spec.options["web_page_means_no_file"] and spec.probe_date == dt.date(2026, 11, 20)
    assert jobs._bse_udiff(None, None, page) == 0


def test_bse_groups_admitted():
    from igs.universe import bse_series
    uc = load_universe()
    assert bse_series(uc) == ["A", "B", "T", "X", "XT"]
    assert bse_series(uc.model_copy(update={"include_sme": True}))[-2:] == ["M", "MT"]
    assert bse_series(uc.model_copy(update={"include_bse_only": False})) == []
    assert not set(bse_series(uc)) & set(uc.include_series + uc.sme_series)


def test_splits_and_bonuses_snap_to_their_ratio():
    # Real ex-dates in BSE's November 2026 files (close over the previous close): a 1:1
    # bonus (CONCORDBIO), 1:10 and 1:5 splits (GNRL, KMEW), a 3:2 bonus (PASHUPATI).
    assert snap_factor(655.20 / 1229.85) == 1 / 2
    assert snap_factor(12.23 / 106.23) == 1 / 10
    assert snap_factor(655.45 / 3149.70) == 1 / 5
    assert snap_factor(35.05 / 83.80) == 2 / 5
    assert snap_factor(0.13) == 0.13                  # no common ratio near: the move itself


def test_actions_are_read_only_from_moves_past_the_band_between_sessions():
    d = [dt.date(2026, 11, n) for n in (2, 3, 4, 5, 6)]
    px = pl.DataFrame({
        "security_id": [1] * 5 + [2] * 3 + [3] * 2,
        "trade_date": d + [d[0], d[1], d[2]] + [d[0], d[4]],
        "close": [100.0, 104.0, 52.5, 53.0, 41.0,      # 1: 1:2 on the 4th, 3:4 on the 6th
                  10.0, 11.9, 10.0,                    # 2: within the 20% band
                  50.0, 20.0]})                        # 3: didn't trade between: not read
    got = implied_actions(px)
    assert got.select("security_id", "ex_date", "fv_new").rows() == [
        (1, d[2], 0.5), (1, d[4], 0.75)]
    assert set(got["action_type"]) == {"split"} and (got["ca_id"] < 0).all()


# --------------------------------------------------------------------------- database


def _raw(conn, fetch_id):
    conn.execute("""insert into raw_payload (fetch_id, source_id, fetched_at, content_sha256,
                        size_bytes, blob_path, origin)
                    values (%s, 's', now(), repeat('a', 64), 1, 'blobs/aa/a', 'http')""",
                 (fetch_id,))


@pytest.fixture
def both(db_conn):
    """NSE's real 18 Sep 2026 file (ABB among them) and BSE's trimmed 20 Nov one, loaded and
    turned into the instrument master."""
    finals = load_sources().get("nse_cm_bhavcopy_udiff").options["final_sessions"]
    nse_px, _ = nse.parse_bhavcopy_udiff(NSE_FILE.read_bytes(), finals, DQLog())
    _raw(db_conn, "nse__20260918")
    _raw(db_conn, "bse__20261120")
    load_prices(db_conn, nse_px.filter(pl.col("symbol").is_in(["ABB", "20MICRONS"])),
                "nse__20260918", DQLog())
    assert load_prices(db_conn, _bse(), "bse__20261120", DQLog()) == 8
    stats = rebuild_instrument_master(db_conn, DQLog())
    db_conn.commit()
    assert stats["bse_only_securities"] == 4
    return db_conn


@pytest.mark.db
def test_a_stock_listed_only_on_bse_becomes_a_company(both):
    conn = both
    got = conn.execute("""select c.name, si.id_type, si.id_value, l.exchange, l.status
        from company c join security s using (company_id)
        join security_identifier si using (security_id)
        join security_listing l using (security_id)
        where l.exchange = 'BSE' order by c.name, si.id_type""").fetchall()
    assert got == [
        ("National Securities Depository", "BSE_CODE", "544467", "BSE", "active"),
        ("National Securities Depository", "ISIN", "INE301O01023", "BSE", "active"),
        ("National Stock Exchange Of Ind", "BSE_CODE", "544937", "BSE", "active"),
        ("National Stock Exchange Of Ind", "ISIN", "INE721I01024", "BSE", "active"),
        ("Rajasthan Securities Limited", "BSE_CODE", "526873", "BSE", "active"),
        ("Rajasthan Securities Limited", "ISIN", "INE184D01026", "BSE", "active"),
        ("Shanti Guru Industries Limited", "BSE_CODE", "534708", "BSE", "active"),
        ("Shanti Guru Industries Limited", "ISIN", "INE892L01019", "BSE", "active")]
    # ABB is on NSE: one company, NSE's listing only. Group Z, the bond and the fund unit
    # make no company; the SME stock does (kept for its history, as NSE's SME lines are),
    # though scoring admits SME groups only with include_sme.
    assert conn.execute("""select count(*) from security_listing
                           where exchange = 'NSE'""").fetchone()[0] == 2
    rebuild_instrument_master(conn, DQLog())                       # identity is kept
    assert conn.execute("select count(*) from company").fetchone()[0] == 6


@pytest.mark.db
def test_it_is_found_by_name_or_bse_code_and_its_page_has_bses_prices(both):
    conn = both
    found = service.companies(conn, q="national stock exchange")
    assert [(c["symbol"], c["name"], c["in_run"]) for c in found] == [
        ("544937", "National Stock Exchange Of Ind", False)]
    assert [c["name"] for c in service.companies(conn, q="544467")] == [
        "National Securities Depository"]
    basic = service.stock_basic(conn, "544937")
    assert basic["symbol"] == "544937" and [p["close"] for p in basic["prices"]] == [2220.30]
    # ABB keeps NSE's prices: BSE's row for the same ISIN is not a second price that day.
    abb = service.stock_basic(conn, "ABB")["prices"]
    assert [p["trade_date"] for p in abb] == [dt.date(2026, 9, 18)]


@pytest.mark.db
def test_scoring_reads_bses_prices_only_for_stocks_listed_only_there(both):
    from igs.pit.loader import load_dataset
    from igs.universe import bse_series, price_series
    uc = load_universe()
    ds = load_dataset(both, dt.date(2026, 9, 1), DAY, series=tuple(price_series(uc)),
                      bse_series=tuple(bse_series(uc)))
    px = ds.tables["prices"]
    assert sorted(zip(px["symbol"], px["series"], strict=True)) == [
        ("20MICRONS", "EQ"), ("526873", "X"), ("544467", "A"), ("544937", "A"),
        ("ABB", "EQ")]
    nse_only = load_dataset(both, dt.date(2026, 9, 1), DAY)       # the default: NSE alone
    assert set(nse_only.tables["prices"]["series"]) == {"EQ"}


@pytest.mark.db
def test_a_bse_only_split_is_adjusted_in_scoring(db_conn):
    from igs.pit.loader import load_dataset
    from igs.pit.view import PitView
    from igs.timeutil import end_of_day_ist
    _raw(db_conn, "bse")
    days = [dt.date(2026, 11, 16), dt.date(2026, 11, 17), dt.date(2026, 11, 18)]
    db_conn.execute("""insert into price_eod (exchange, trade_date, isin, symbol, series,
            close, source_fetch_id) values ('NSE', %s, 'INE117A01022', 'ABB', 'EQ', 6900,
            'bse')""", (days[0],))
    for day, close in zip(days, (160.45, 16.85, 17.0), strict=True):   # BNAGROCHEM's 1:10
        db_conn.execute("""insert into price_eod (exchange, trade_date, isin, symbol, series,
                close, prev_close, volume, source_fetch_id)
            values ('BSE', %s, 'INE00HZ01011', '526125', 'B', %s, %s, 1000, 'bse')""",
                        (day, close, close))
    rebuild_instrument_master(db_conn, DQLog())
    db_conn.commit()
    ds = load_dataset(db_conn, days[0], days[-1], bse_series=("B",))
    def closes(day):
        px = PitView(ds, end_of_day_ist(day)).prices().filter(symbol="526125")
        return px.sort("trade_date")["adj_close"].round(3).to_list()
    assert closes(days[-1]) == [16.045, 16.85, 17.0]
    assert closes(days[0]) == [160.45]                             # not known before it


@pytest.mark.db
def test_a_bse_only_company_is_ranked_like_the_same_company_on_nse(db_conn, tmp_path,
                                                                  monkeypatch):
    """The synthetic market's CYCL copied as a company listed only on BSE (group X): the
    same prices, filings and holdings, and the 1:1 bonus read from its prices rather than
    from NSE's corporate actions. It is ranked as CYCL is."""
    import json

    import db_market
    from test_scoring import _cfgs

    from igs.pit import gate
    from igs.score.pipeline import score_from_db
    db_market.load(db_conn)
    src, isin = db_market.isin(2), "INE007X01010"
    with db_conn.cursor() as cur:
        cur.execute("insert into company (company_id, name, company_key) "
                    "values (7, 'Cyclical Steel BSE Ltd', 'K7')")
        cur.execute("insert into security (security_id, company_id, security_key) "
                    "values (107, 7, %s)", (isin,))
        for id_type, value in (("ISIN", isin), ("BSE_CODE", "590007")):
            cur.execute("""insert into security_identifier (security_id, id_type, id_value,
                               valid_from, evidence) values (107, %s, %s, '2017-01-01', 'test')""",
                        (id_type, value))
        cur.execute("insert into security_listing (security_id, exchange, status) "
                    "values (107, 'BSE', 'active')")
        cur.execute("""insert into price_eod (exchange, trade_date, isin, symbol, series, open,
                           high, low, close, prev_close, volume, delivery_pct, source_fetch_id)
                       select 'BSE', trade_date, %s, '590007', 'X', open, high, low, close,
                              prev_close, volume, null, source_fetch_id
                       from price_eod where isin = %s""", (isin, src))
        cur.execute("""insert into filing (filing_id, company_id, exchange, filing_system,
                           filing_type, period_end, statement_basis, filed_at, ingested_at,
                           content_sha256, source_fetch_id, source_url)
                       select filing_id + 100000, 7, exchange, filing_system, filing_type,
                              period_end, statement_basis, filed_at, ingested_at,
                              md5(content_sha256) || md5(content_sha256), source_fetch_id,
                              source_url || '?bse'
                       from filing where company_id = 2""")
        cur.execute("""insert into fundamental_fact (fact_id, filing_id, company_id,
                           statement_basis, period_end, period_type, concept, source_element,
                           value, unit, filed_at, ingested_at)
                       select fact_id + 1000000, filing_id + 100000, 7, statement_basis,
                              period_end, period_type, concept, source_element, value, unit,
                              filed_at, ingested_at
                       from fundamental_fact where company_id = 2""")
        cur.execute("""insert into shareholding (filing_id, company_id, period_end, category,
                           shares, pct_of_total, pledged_shares, pledged_pct, holders,
                           filed_at, ingested_at)
                       select filing_id + 100000, 7, period_end, category, shares,
                              pct_of_total, pledged_shares, pledged_pct, holders, filed_at,
                              ingested_at
                       from shareholding where company_id = 2""")
        cur.execute("""insert into industry_classification (company_id, macro_sector, sector,
                           industry, basic_industry, valid_from)
                       select 7, macro_sector, sector, industry, basic_industry, valid_from
                       from industry_classification where company_id = 2""")
    db_conn.commit()
    monkeypatch.setenv("IGS_GATE_PATH", str(tmp_path / "gate.json"))
    (tmp_path / "gate.json").write_text(json.dumps(
        {"fingerprint": gate.code_fingerprint(), "passed_at": "test", "summary": ""}))
    sc, uc = _cfgs()
    _, run = score_from_db(db_conn, db_market.AS_OF, None, sc=sc, uc=uc)
    u = {r["company_id"]: r for r in run.universe.iter_rows(named=True)}
    assert u[7]["included"] and u[7]["symbol"] == "590007"
    assert u[7]["mcap_cr"] == pytest.approx(u[2]["mcap_cr"])
    res = {r["company_id"]: r for r in run.results.iter_rows(named=True)}
    assert res[7]["composite"] == pytest.approx(res[2]["composite"], abs=1e-6)
    assert res[7]["tier"] == res[2]["tier"]
    # Without BSE's groups it is left out, for its series, as before.
    _, nse_only = score_from_db(db_conn, db_market.AS_OF, None, sc=sc,
                                uc=uc.model_copy(update={"include_bse_only": False}))
    assert 7 not in set(nse_only.universe["company_id"])


@pytest.mark.db
def test_the_ais_price_record_reads_bse_and_its_bonuses(db_conn):
    from igs.assistant.calls import _price_rows, price_index
    _raw(db_conn, "bse")
    days = [dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4)]
    for day, close, prev in zip(days, (1200.0, 1229.85, 655.20), (1190.0, 1200.0, 1229.85),
                                strict=True):                     # CONCORDBIO's 1:1 bonus
        db_conn.execute("""insert into price_eod (exchange, trade_date, isin, symbol, series,
                close, prev_close, source_fetch_id)
            values ('BSE', %s, 'INE338H01029', '543960', 'A', %s, %s, 'bse')""",
                        (day, close, prev))
    db_conn.execute("""insert into price_eod (exchange, trade_date, isin, symbol, series,
            close, source_fetch_id) values ('NSE', %s, 'INE117A01022', 'ABB', 'EQ', 6900,
            'bse')""", (days[0],))
    rebuild_instrument_master(db_conn, DQLog())
    cid = db_conn.execute("""select s.company_id from security_identifier si
        join security s using (security_id) where si.id_value = '543960'""").fetchone()[0]
    rows = _price_rows(db_conn, cid, days[0], days[-1])
    assert [r[2] for r in rows] == [1190.0, 1200.0, pytest.approx(614.925)]
    level = price_index(rows)[-1][1]
    assert level == pytest.approx(1229.85 / 1200 * 655.20 / 614.925)   # no halving
