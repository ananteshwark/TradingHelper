"""End-to-end step 1 on documented-format payloads served by a fake network:
verify -> ingest -> instrument master -> reconciliation -> rebuild from raw."""

from __future__ import annotations

import datetime as dt
import json

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
    routes[render_url(SOURCES.get("nse_total_market_constituents"))] = P.total_market()
    routes[render_url(SOURCES.get("nse_nifty200_constituents"))] = P.total_market()
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
    fetcher = Fetcher(store, min_interval_s=0, host_min_interval_s={},
                      client=httpx.Client(transport=_transport(_routes())))
    return jobs.Context(conn=db_conn, store=store, sources=SOURCES, fetcher=fetcher)


def _verify_all(ctx, *, skip: tuple[str, ...] = ()) -> None:
    for sid in [*STATIC, *DATED, "nse_corporate_actions", "nse_announcements",
                "nse_quote_equity"]:
        if sid in skip:
            continue
        v = verify_source(SOURCES.get(sid), ctx.fetcher, today=TODAY)
        assert v.status == "verified", (sid, v.message)


def _ingest_everything(ctx, *, quotes: bool = True) -> None:
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
    if quotes:
        jobs.ingest_symbols(ctx, "nse_quote_equity", ["ACME", "BETA", "GAMMA"])
    ctx.conn.commit()


def _q(conn, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_the_check_classifies_companies_without_an_industry(tmp_path, db_conn):
    """The regular check asks NSE for the classification of companies without one, those
    in the latest score run first. While the quote API is refused it only says so; a quote
    without a classification is skipped, and no symbol is asked for again within a week."""
    routes = _routes()
    probe = render_url(SOURCES.get("nse_quote_equity"), symbol="RELIANCE")
    reliance = routes.pop(probe)                                   # refused at first
    routes[render_url(SOURCES.get("nse_quote_equity"), symbol="BETA")] = json.dumps(
        {"info": {"symbol": "BETA"}}).encode()                     # no industryInfo
    store = RawStore(tmp_path / "raw")
    ctx = jobs.Context(conn=db_conn, store=store, sources=SOURCES, fetcher=Fetcher(
        store, min_interval_s=0, host_min_interval_s={},
        client=httpx.Client(transport=_transport(routes))))
    _verify_all(ctx, skip=("nse_quote_equity",))
    _ingest_everything(ctx, quotes=False)
    conn = ctx.conn
    assert jobs.missing_classification(conn) == ["ACME", "BETA", "GAMMA"]
    # Never verified: verified on first use; refused, the step says so and moves on.
    assert jobs.ingest_missing_classification(ctx) == (
        "skipped: nse_quote_equity is not verified (HTTP 404); the weekly source check "
        "tries it again")
    assert jobs.missing_classification(conn) == ["ACME", "BETA", "GAMMA"]
    routes[probe] = reliance
    _verify_all(ctx)
    gamma = _q(conn, """select s.company_id from security_identifier si join security s
                        using (security_id) where si.id_value = 'GAMMA'""")[0][0]
    run = _q(conn, """insert into score_run (as_of, gate_fingerprint, config)
                      values (now(), 'test', '{}') returning run_id""")[0][0]
    conn.execute("""insert into score_result (run_id, company_id, tier, explanation)
                    values (%s, %s, 'Rejected', '')""", (run, gamma))
    assert jobs.missing_classification(conn, 2) == ["GAMMA", "ACME"]
    assert jobs.ingest_missing_classification(ctx) == (
        "2 of 3 companies classified; no classification for BETA")
    assert _q(conn, """select si.id_value, ic.sector, ic.basic_industry
                       from industry_classification ic join security s using (company_id)
                       join security_identifier si using (security_id)
                       where si.id_type = 'NSE_SYMBOL' and si.valid_to is null
                       order by 1""") == [("ACME", "Capital Goods", "Castings & Forgings"),
                                          ("GAMMA", "FMCG", "Packaged Foods")]
    assert jobs.missing_classification(conn) == []
    assert jobs.ingest_missing_classification(ctx).startswith("every current company")


def test_the_check_takes_sectors_from_the_index_list_once_a_day(ctx, monkeypatch):
    """Where NSE's four-level classification isn't loaded, the Nifty Total Market list gives
    each listed company its sector: by NSE symbol on the list's date, else by ISIN."""
    from igs.factors import base
    from igs.pit import PitView
    from igs.pit.loader import load_dataset
    from igs.timeutil import IST, utc_now
    _verify_all(ctx)
    _ingest_everything(ctx, quotes=False)
    conn = ctx.conn
    got = jobs.ingest_index_sectors(ctx)                # verified on first use, then loaded
    assert (got.http_status, got.rows) == (200, 3)
    assert jobs.ingest_index_sectors(ctx).startswith("loaded ")       # once a day
    monkeypatch.setattr(jobs, "SECTORS_EVERY", dt.timedelta(0))
    assert jobs.ingest_index_sectors(ctx).rows == 0                   # nothing changed
    ids = dict(_q(conn, """select si.id_value, s.company_id from security_identifier si
                           join security s using (security_id)
                           where si.id_type = 'NSE_SYMBOL' and si.valid_to is null"""))
    assert dict(_q(conn, "select company_id, sector from index_sector")) == {
        ids["ACME"]: "Capital Goods", ids["BETA"]: "Chemicals",
        ids["GAMMA"]: "Fast Moving Consumer Goods"}                   # OLDG by its ISIN
    assert {i.message for i in ctx.dq.issues if i.category == "index_sector_unmapped"} == {
        "1 index-list symbols match no company: ZETA"}
    today = dt.datetime.now(IST).date()
    view = PitView(load_dataset(conn, today - dt.timedelta(days=5), today), utc_now())
    cls = {r["company_id"]: (r["industry"], r["sector"], r["industry_source"])
           for r in base.classification(view).iter_rows(named=True)}
    assert cls[ids["ACME"]] == (None, "Capital Goods", "nse_index_list")


def test_index_members_are_kept_as_snapshots_when_they_change(ctx, monkeypatch):
    _verify_all(ctx)
    _ingest_everything(ctx)
    conn = ctx.conn
    got = jobs.ingest_daily_list(ctx, jobs.NIFTY200)       # verified on first use, then loaded
    assert (got.http_status, got.rows) == (200, 3)         # ZETA is no company here
    monkeypatch.setattr(jobs, "SECTORS_EVERY", dt.timedelta(0))
    assert jobs.ingest_daily_list(ctx, jobs.NIFTY200).rows == 0      # the same members
    ids = dict(_q(conn, """select si.id_value, s.company_id from security_identifier si
                           join security s using (security_id)
                           where si.id_type = 'NSE_SYMBOL' and si.valid_to is null"""))
    assert sorted(_q(conn, "select company_id from index_member where index_name = "
                           "'NIFTY 200'")) == sorted((ids[s],) for s in ("ACME", "BETA", "GAMMA"))


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


def test_rebuild_preserves_forward_observation_times(ctx):
    _verify_all(ctx)
    _ingest_everything(ctx)
    conn = ctx.conn
    conn.execute("""insert into forward_document(ann_id,company_id,published_at,
        received_at,assessed_at,claims) select ann_id,
        (select min(company_id) from company),filed_at,
        '2026-10-01 10:00+00','2026-10-01 10:01+00','[]'
        from announcement order by ann_id limit 1""")
    conn.commit()
    before = conn.execute('select company_id,published_at,received_at,assessed_at,claims '
                          'from forward_document').fetchall()
    assert len(before) == 1
    jobs.rebuild_from_raw(ctx)
    assert conn.execute('select company_id,published_at,received_at,assessed_at,claims '
                        'from forward_document').fetchall() == before
