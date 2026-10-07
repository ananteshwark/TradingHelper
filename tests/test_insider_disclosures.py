"""NSE's insider-trading data after its May 2026 system change: the current listing
(corporates-pit-gg) and its per-disclosure XBRL, revisions, the older API kept for history,
and a trade listed by both systems counting once. Parsers run on real payloads kept in
tests/fixtures/real/."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import polars as pl
import pytest
import synthetic_market as M

import igs.factors  # noqa: F401  (registers factors)
from igs.config import load_sources
from igs.dq import DQLog
from igs.factors import base
from igs.factors.registry import REGISTRY
from igs.ingest import jobs
from igs.ingest.http import Fetcher
from igs.ingest.raw_store import RawStore
from igs.ingest.verify import verify_source
from igs.normalize.nse import SchemaMismatch, parse_insider_trades
from igs.pit import PitDataset, PitView
from igs.pit.harness import check_no_lookahead
from igs.pit.knowledge import KNOWN_AT
from igs.timeutil import IST
from igs.xbrl.insider import parse_disclosure_document, parse_disclosure_listing

REAL = Path(__file__).parent / "fixtures" / "real"
LISTING = REAL / "insider_disclosures_2026-09-22_2026-09-29.json"
OLD_API = REAL / "insider_trading_2026-04-01_2026-04-07.json"
MAYUR = REAL / "insider_xbrl_MAYURUNIQ_20260929.xml"
HCL = REAL / "insider_xbrl_HCLTECH_20260925.xml"
HOSTS = ["nsearchives.nseindia.com"]
SOURCES = load_sources()


def _refs() -> pl.DataFrame:
    return parse_disclosure_listing(LISTING.read_bytes(), HOSTS, DQLog())


def _ref(symbol: str) -> dict:
    return _refs().filter(pl.col("symbol") == symbol).row(0, named=True)


def _listing_row(**over) -> dict:
    row = next(r for r in json.loads(LISTING.read_text())["data"]
               if r["symbol"] == "MAYURUNIQ" and r["appId"] == "3748")
    return {**row, **over}


# --------------------------------------------------------------------------- listing


def test_listing_gives_one_reference_per_disclosure():
    dq = DQLog()
    refs = parse_disclosure_listing(LISTING.read_bytes(), HOSTS, dq)
    assert refs.height == 54 and not dq.issues
    assert refs["submission_type"].value_counts().sort("submission_type").rows() == [
        ("Original", 51), ("Revision", 3)]
    r = _ref("MAYURUNIQ")
    assert r["disclosure_id"] == "3748"
    # Broadcast 22:20:05, disseminated 22:20:07: public from the later.
    assert r["filed_at"] == dt.datetime(2026, 9, 29, 22, 20, 7, tzinfo=IST)
    assert r["document_url"].endswith("IT_6149_WebXMLFile_20260929_222005829.xml")
    rev = refs.filter(pl.col("symbol") == "RKFORGE").sort("filed_at")
    assert rev["submission_type"].to_list() == ["Original", "Revision"]
    assert "34,00,000" in rev["revision_remark"][1]


def test_listing_reports_what_it_cannot_use():
    dq = DQLog()
    body = json.dumps({"data": [
        _listing_row(broadcastDateTime="-"),
        _listing_row(typeOfSubmission="Withdrawal"),
        _listing_row(xmlFileName="https://elsewhere.example/IT.xml"),
        _listing_row()]}).encode()
    refs = parse_disclosure_listing(body, HOSTS, dq)
    assert refs.height == 1
    assert [i.category for i in dq.issues] == ["insider_listing_incomplete",
                                                "insider_listing_incomplete",
                                                "document_host_not_allowed"]
    row = _listing_row()
    del row["xmlFileName"]
    with pytest.raises(SchemaMismatch, match="xmlFileName"):
        parse_disclosure_listing(json.dumps({"data": [row]}).encode(), HOSTS, DQLog())


# --------------------------------------------------------------------------- XBRL


def test_a_promoters_market_purchase():
    dq = DQLog()
    df = parse_disclosure_document(MAYUR.read_bytes(), _ref("MAYURUNIQ"), dq)
    assert not dq.issues
    r = df.row(0, named=True)
    assert df.height == 1
    assert (r["person_name"], r["person_category"], r["insider_role"]) == \
        ("Kiran Poddar", "Promoter", "promoter")
    assert (r["side"], r["open_market"], r["security_type"]) == ("buy", True, "Equity")
    assert (r["quantity"], r["value_inr"]) == (2000, 1433954)
    # 0.0021 in the XBRL is a fraction: 90,727 of about 4.3 crore shares.
    assert r["holding_before_pct"] == pytest.approx(0.21)
    assert (r["trade_from"], r["intimated_on"]) == (dt.date(2026, 9, 28), dt.date(2026, 9, 29))
    assert r["filed_at"] == dt.datetime(2026, 9, 29, 22, 20, 7, tzinfo=IST)
    assert (r["disclosure_id"], r["submission_type"]) == ("3748", "Original")


def test_one_disclosure_with_several_trades():
    dq = DQLog()
    df = parse_disclosure_document(HCL.read_bytes(), _ref("HCLTECH"), dq)
    assert df.height == 8 and not dq.issues
    assert set(df["person_category"]) == {"Designated Person"}
    assert set(df["insider_role"]) == {"other"}
    assert df["side"].to_list().count("buy") == 6
    # Shares received on exercising stock units are not open-market purchases.
    assert not df.filter(pl.col("acquisition_mode") == "ESOS")["open_market"].any()
    walia = df.filter(pl.col("person_name") == "SHIV KUMAR WALIA")
    assert walia["trade_from"].to_list() == [dt.date(2026, 8, 14), dt.date(2026, 9, 22)]


def test_a_document_it_cannot_trust_loads_nothing():
    ref = _ref("MAYURUNIQ")
    xml = MAYUR.read_bytes()
    cases = {
        "insider_trade_symbol_mismatch": (xml, {**ref, "symbol": "HCLTECH"}),
        "taxonomy_mismatch": ((REAL / "results_integrated_ANNU_2026Q1_standalone.xml")
                              .read_bytes(), ref),
    }
    for category, (content, r) in cases.items():
        dq = DQLog()
        assert parse_disclosure_document(content, r, dq).height == 0
        assert [i.category for i in dq.issues] == [category]
    dq = DQLog()
    df = parse_disclosure_document(xml.replace(b">0.0021<", b">21.3<", 1), ref, dq)
    assert df["holding_before_pct"][0] is None                  # not a fraction: not stored
    assert [i.category for i in dq.issues] == ["insider_trade_holding_pct"]
    dq = DQLog()
    parse_disclosure_document(xml, {**ref, "submission_type": "Revision"}, dq)
    assert [i.category for i in dq.issues] == ["insider_trade_revision_mismatch"]


# --------------------------------------------------------------------------- older API


def test_the_older_api_for_history():
    dq = DQLog()
    df = parse_insider_trades(OLD_API.read_bytes(), dq)
    assert df.height == 74
    # Every spelling in the sample is known; three rows name no person or quantity.
    assert [i.category for i in dq.issues] == ["insider_trade_blank"]
    assert "3 disclosures" in dq.issues[0].message
    pledges = df.filter(pl.col("transaction_type").str.starts_with("Pledge"))
    assert pledges.height > 0 and set(pledges["side"]) == {"other"}
    assert df["disclosure_id"].is_null().all()


def test_the_older_api_is_verified_on_a_week_it_still_serves(tmp_path):
    spec = SOURCES.get("nse_insider_trading")
    asked: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        asked.append(str(req.url))
        if "corporates-pit?" in str(req.url):
            return httpx.Response(200, content=OLD_API.read_bytes())
        return httpx.Response(200, content=b"<html>home</html>")

    fetcher = Fetcher(RawStore(tmp_path / "raw"), min_interval_s=0, host_min_interval_s={},
                      client=httpx.Client(transport=httpx.MockTransport(handler)))
    v = verify_source(spec, fetcher, today=dt.date(2026, 9, 29))
    assert v.status == "verified", v.message
    assert "from_date=31-03-2026&to_date=07-04-2026" in asked[-1]


# --------------------------------------------------------------------------- factor


def _with_revision() -> PitDataset:
    """LATE's promoter buys on 4 Nov 2024 for Rs 1 crore; a revision broadcast on 20 Nov
    restates it as Rs 80 lakh."""
    frames = {n: df.drop(KNOWN_AT) for n, df in M.build().tables.items()}
    t = frames["insider_trades"].with_columns(pl.lit("Original").alias("submission_type"))
    n = t["insider_trade_id"].max()
    extra = pl.DataFrame([
        {**t.row(0, named=True), "insider_trade_id": n + i + 1, "company_id": 6,
         "symbol": "LATE", "person_name": "Promoter Six", "insider_role": "promoter",
         "side": "buy", "open_market": True, "quantity": 1e5, "value_inr": value,
         "trade_from": dt.date(2024, 11, 4),
         "filed_at": dt.datetime(2024, 11, day, 18, 30, tzinfo=IST), "submission_type": kind}
        for i, (value, day, kind) in enumerate([(1e7, 5, "Original"), (8e6, 20, "Revision")])],
        schema=t.schema)
    return PitDataset.from_frames(**{**frames, "insider_trades": pl.concat([t, extra])})


@pytest.mark.lookahead
def test_a_revision_counts_from_its_broadcast():
    ds = _with_revision()
    before = dt.datetime(2024, 11, 20, 18, 29, tzinfo=IST)
    after = before + dt.timedelta(minutes=2)

    def late(as_of: dt.datetime) -> float:
        view = PitView(ds, as_of)
        mcap = dict(base.market_cap(view).select("company_id", "mcap").iter_rows())
        out = REGISTRY["insider_buying_90d"].fn(view).filter(pl.col("company_id") == 6)
        return out["value"][0] * mcap[6] / 100

    assert late(before) == pytest.approx(1e7)
    assert late(after) == pytest.approx(8e6)          # replaced, not added
    check_no_lookahead(REGISTRY["insider_buying_90d"].fn, ds, [*M.GATE_DATES, before, after],
                       name="insider_buying_90d")


# --------------------------------------------------------------------------- database

BASE = "https://nsearchives.nseindia.com/corporate/xbrl/"
REV_URL = BASE + "IT_6149_WebXMLFile_20260930_101500000.xml"
TODAY = dt.date(2026, 9, 30)


def _routes() -> dict[str, bytes]:
    rows = [r for r in json.loads(LISTING.read_text())["data"]
            if r["appId"] in ("3748", "3638")]
    rows.append(_listing_row(appId="3790", broadcastDateTime="30-Sep-2026 10:15:00",
                             typeOfSubmission="Revision", revisionRemark="Value corrected",
                             xmlFileName=REV_URL))
    revised = (MAYUR.read_bytes()
               .replace(b">false</in-bse-co:RevisedFilling>", b">true</in-bse-co:RevisedFilling>")
               .replace(b">1433954<", b">1533954<"))
    return {"corporates-pit-gg": json.dumps({"data": rows}).encode(),
            _ref("MAYURUNIQ")["document_url"]: MAYUR.read_bytes(),
            _ref("HCLTECH")["document_url"]: HCL.read_bytes(), REV_URL: revised}


@pytest.fixture
def ctx(tmp_path, db_conn):
    routes = _routes()

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "corporates-pit-gg" in url:
            return httpx.Response(200, content=routes["corporates-pit-gg"])
        if url in routes:
            return httpx.Response(200, content=routes[url])
        if url == "https://www.nseindia.com/":
            return httpx.Response(200, content=b"<html>home</html>")
        return httpx.Response(404, content=b"")

    store = RawStore(tmp_path / "raw")
    fetcher = Fetcher(store, min_interval_s=0, host_min_interval_s={},
                      client=httpx.Client(transport=httpx.MockTransport(handler)))
    c = jobs.Context(conn=db_conn, store=store, sources=SOURCES, fetcher=fetcher)
    assert verify_source(SOURCES.get(jobs.INSIDER_LISTING), fetcher,
                         today=TODAY).status == "verified"
    return c


def _count(conn, sql: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


@pytest.mark.db
def test_listing_then_documents_then_rebuild(ctx):
    from igs import service
    conn = ctx.conn
    from igs.sync import insider_listing_start
    assert insider_listing_start(conn, TODAY) == TODAY - dt.timedelta(days=6)   # none yet
    listed = jobs.ingest_range(ctx, jobs.INSIDER_LISTING, TODAY - dt.timedelta(days=8), TODAY)
    assert [r.url.split("from_date=")[1] for r in listed] == [    # 7 days a request at most
        "22-09-2026&to_date=28-09-2026", "29-09-2026&to_date=30-09-2026"]
    assert _count(conn, "select count(*) from insider_disclosure_ref") == 3
    res = jobs.ingest_insider_documents(ctx)
    assert [r.rows for r in res] == [8, 1, 1]           # HCLTECH, MAYURUNIQ, its revision
    assert jobs.ingest_insider_documents(ctx) == []      # nothing asked twice
    assert _count(conn, "select count(*) from insider_trade where disclosure_id is not null") \
        == 10

    def mayur(as_of: dt.datetime) -> list[tuple]:
        return [(t["submission_type"], t["value_inr"], t["superseded"])
                for t in service.insider_trades(conn, "MAYURUNIQ", as_of)]

    assert mayur(dt.datetime(2026, 9, 30, 9, 0, tzinfo=IST)) == [
        ("Original", 1433954, False)]
    assert mayur(dt.datetime(2026, 10, 1, tzinfo=IST)) == [
        ("Revision", 1533954, False), ("Original", 1433954, True)]

    # The next check starts from the day of the latest disclosure listed, however long ago.
    assert insider_listing_start(conn, dt.date(2026, 10, 20)) == dt.date(2026, 9, 30)
    assert insider_listing_start(conn, dt.date(2027, 6, 1)) == dt.date(2027, 3, 3)   # 90 days

    counts = jobs.rebuild_from_raw(ctx, post_master=False)
    assert counts["nse_insider_xbrl"] == 10 and counts["nse_insider_disclosures"] == 3
    assert _count(conn, "select count(*) from insider_trade") == 10


@pytest.mark.db
def test_a_trade_listed_by_both_systems_counts_once(ctx):
    conn = ctx.conn
    old = {"symbol": "MAYURUNIQ", "company": "MAYUR UNIQUOTERS", "anex": "7(2)",
           "acqName": "Kiran Poddar", "personCategory": "Promoters", "secType": "Equity Shares",
           "secAcq": "2000", "secVal": "1433954", "tdpTransactionType": "Buy",
           "acqMode": "Market Purchase", "acqfromDt": "28-Sep-2026", "acqtoDt": "28-Sep-2026",
           "intimDt": "29-Sep-2026", "date": "29-Sep-2026 22:20"}
    from igs.normalize.load import load_insider_trades
    with conn.cursor() as cur:
        cur.execute("""insert into raw_payload (fetch_id, source_id, fetched_at, content_sha256,
                           size_bytes, blob_path, origin)
                       values ('old', 'nse_insider_trading', now(), repeat('0', 64), 0, 'x',
                               'manual')""")
    load_insider_trades(conn, parse_insider_trades(
        json.dumps({"data": [old]}).encode(), DQLog()), "old", dt.datetime.now(dt.UTC))
    jobs.ingest_range(ctx, jobs.INSIDER_LISTING, TODAY - dt.timedelta(days=8), TODAY)
    jobs.ingest_insider_documents(ctx)
    rows = _count(conn, "select count(*) from insider_trade where symbol = 'MAYURUNIQ'")
    assert rows == 2                                     # the older row and the revision
    assert any(i.category == "insider_trade_already_loaded" for i in ctx.dq.issues)
