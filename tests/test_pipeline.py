"""End-to-end step 1 on documented-format payloads served by a fake network:
verify -> ingest -> instrument master -> reconciliation -> rebuild from raw."""

from __future__ import annotations

import datetime as dt

import documented_payloads as P
import httpx
import polars as pl
import pytest

from igs.config import load_sources
from igs.ingest import jobs
from igs.ingest.http import Fetcher
from igs.ingest.raw_store import RawStore
from igs.ingest.sources import render_url
from igs.ingest.verify import SourceNotVerified, verify_source
from igs.recon.run import run_reconciliation

pytestmark = pytest.mark.db

SOURCES = load_sources()
TODAY = dt.date(2024, 7, 17)
STATIC = {
    "nse_equity_list": P.equity_list, "nse_trading_holidays": P.holidays,
    "nse_asm": P.asm, "nse_gsm": P.gsm, "bse_scrip_master": P.bse_scrips,
    "angel_scrip_master": P.angel_master,
}
DATED = {
    "nse_cm_bhavcopy_udiff": (P.udiff, P.UDIFF_DAYS),
    "nse_cm_bhavcopy_legacy": (P.legacy, P.LEGACY_DAYS),
    "nse_sec_bhavdata_full": (P.sec_full, P.ALL_DAYS),
    "nse_mto_delivery": (P.mto, P.ALL_DAYS),
    "nse_index_close_all": (P.index_close, P.ALL_DAYS),
}


def _routes() -> dict[str, bytes]:
    routes = {"https://www.nseindia.com/": b"<html>home</html>"}
    for sid, fn in STATIC.items():
        routes[render_url(SOURCES.get(sid))] = fn()
    for sid, (fn, days) in DATED.items():
        for d in days:
            routes[render_url(SOURCES.get(sid), day=d)] = fn(d)
    # The legacy probe date predates the mini market; serve a legacy file for it.
    routes[render_url(SOURCES.get("nse_cm_bhavcopy_legacy"), day=dt.date(2024, 6, 28))] = \
        P.legacy(P.LEGACY_DAYS[0])
    for sym in ("ACME", "BETA", "GAMMA", "RELIANCE"):
        routes[render_url(SOURCES.get("nse_quote_equity"), symbol=sym)] = \
            P.quote(sym if sym != "RELIANCE" else "ACME")
    return routes


def _transport(routes: dict[str, bytes]) -> httpx.MockTransport:
    ranges = {"corporates-corporateActions": P.corporate_actions(),
              "corporate-announcements": P.announcements()}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url in routes:
            return httpx.Response(200, content=routes[url])
        for key, body in ranges.items():
            if key in url:
                return httpx.Response(200, content=body)
        return httpx.Response(404, content=b"")
    return httpx.MockTransport(handler)


@pytest.fixture
def ctx(tmp_path, db_conn):
    store = RawStore(tmp_path / "raw")
    fetcher = Fetcher(store, min_interval_s=0,
                      client=httpx.Client(transport=_transport(_routes())))
    return jobs.Context(conn=db_conn, store=store, sources=SOURCES, fetcher=fetcher)


def _verify_all(ctx) -> None:
    for sid in [*STATIC, *DATED, "nse_corporate_actions", "nse_announcements",
                "nse_quote_equity"]:
        v = verify_source(SOURCES.get(sid), ctx.fetcher, today=TODAY)
        assert v.status == "verified", (sid, v.message)


def _ingest_everything(ctx) -> None:
    for sid in STATIC:
        jobs.ingest_static(ctx, sid)
    jobs.backfill_prices(ctx, dt.date(2024, 7, 1), dt.date(2024, 7, 16))
    jobs.ingest_date(ctx, "nse_mto_delivery", dt.date(2024, 7, 16))
    jobs.ingest_range(ctx, "nse_corporate_actions", dt.date(2024, 6, 1), dt.date(2024, 7, 31),
                      chunk_days=90)
    jobs.ingest_range(ctx, "nse_announcements", dt.date(2024, 7, 1), dt.date(2024, 7, 16),
                      chunk_days=90)
    from igs.normalize.master_db import rebuild_instrument_master
    with ctx.conn.transaction():
        rebuild_instrument_master(ctx.conn, ctx.dq)
    jobs.ingest_symbols(ctx, "nse_quote_equity", ["ACME", "BETA", "GAMMA"])
    ctx.conn.commit()


def _q(conn, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_unverified_source_is_refused(ctx):
    with pytest.raises(SourceNotVerified, match="never verified"):
        jobs.ingest_static(ctx, "nse_equity_list")


def test_full_step1_pipeline(ctx, tmp_path):
    _verify_all(ctx)
    _ingest_everything(ctx)
    conn = ctx.conn

    # 12 trading days x 3 stocks; pre-open rows dropped.
    assert _q(conn, "select count(*), count(distinct session_id) from price_eod") == [(36, 1)]
    assert _q(conn, "select count(*) from price_eod where delivery_qty is null") == [(0,)]

    # ACME: one security, two ISIN ranges; OLDG -> GAMMA: one security, two symbols.
    acme = _q(conn, """select id_value, valid_from, valid_to from security_identifier
                       where id_type = 'ISIN' and id_value in (%s, %s) order by valid_from""",
              (P.ACME_OLD, P.ACME_NEW))
    assert acme == [(P.ACME_OLD, dt.date(2024, 7, 1), dt.date(2024, 7, 10)),
                    (P.ACME_NEW, dt.date(2024, 7, 10), None)]
    syms = _q(conn, """select si.id_value from security_identifier si
                       join security_identifier g on g.security_id = si.security_id
                       where g.id_type = 'ISIN' and g.id_value = %s
                         and si.id_type = 'NSE_SYMBOL' order by si.valid_from""", (P.GAMMA,))
    assert syms == [("OLDG",), ("GAMMA",)]
    assert _q(conn, "select count(*) from security") == [(3,)]
    assert sorted(_q(conn, "select id_type, count(*) from security_identifier "
                     "where id_type in ('BSE_CODE', 'ANGEL_TOKEN_NSE') group by 1")) == [
        ("ANGEL_TOKEN_NSE", 2), ("BSE_CODE", 2)]
    assert _q(conn, "select count(*) from corporate_action where security_id is null") == [(0,)]
    assert _q(conn, "select count(*) from industry_classification") == [(3,)]
    assert _q(conn, "select measure, symbol, stage from surveillance_snapshot order by 1") == [
        ("ASM", "GAMMA", "Stage II"), ("GSM", "ZETA", "Stage 1")]
    assert _q(conn, "select name from company order by name") == [
        ("Acme Industries Limited",), ("Beta Chemicals Limited",), ("Gamma Foods Limited",)]

    results, report = run_reconciliation(conn, dt.date(2024, 7, 1), dt.date(2024, 7, 16),
                                         tmp_path / "reports", dq=ctx.dq)
    statuses = {r.name: r.status for r in results}
    assert statuses == {"unique_price_rows": "pass", "symbol_consistency": "pass",
                        "isin_mapping": "pass", "calendar_coverage": "pass",
                        "factors_vs_exchange": "pass", "unexplained_gaps": "pass"}, \
        report.read_text()
    assert "overall: **PASS**" in report.read_text()

    # Rebuild from raw reproduces the same derived state.
    def snapshot():
        return {
            "prices": pl.DataFrame(_q(conn, "select trade_date, isin, series, close::float8, "
                                   "delivery_qty from price_eod order by 1, 2, 3"), orient="row"),
            "ids": pl.DataFrame(_q(conn, "select security_id, id_type, id_value, valid_from, "
                                "valid_to from security_identifier order by 1, 2, 4"),
                                orient="row"),
            "ca": pl.DataFrame(_q(conn, "select security_id, action_type, ex_date, subject "
                               "from corporate_action order by 1, 2, 3, 4"), orient="row"),
            "cls": pl.DataFrame(_q(conn, "select company_id, basic_industry "
                                "from industry_classification order by 1"), orient="row"),
        }
    before = snapshot()
    counts = jobs.rebuild_from_raw(ctx)
    assert counts["_securities"] == 3
    after = snapshot()
    for k in before:
        assert before[k].equals(after[k]), k


def test_schema_change_stops_ingestion(ctx):
    _verify_all(ctx)
    spec = SOURCES.get("nse_equity_list")
    changed = P.equity_list().replace(b"FACE VALUE", b"FACE_VAL")
    ctx.fetcher.client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=changed)))
    with pytest.raises(SourceNotVerified, match="schema changed"):
        jobs.fetch_and_load(ctx, spec, render_url(spec), {})
    assert _q(ctx.conn, "select count(*) from nse_equity_list") == [(0,)]
