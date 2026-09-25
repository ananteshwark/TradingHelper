"""Step 2 end to end: listings -> XBRL documents -> point-in-time facts, with a
restatement arriving as a revised filing for the same period a year later (as seen
in real listings: e.g. December-2024 results re-filed in July 2026)."""

from __future__ import annotations

import datetime as dt

import documented_xbrl as X
import httpx
import polars as pl
import pytest
import test_pipeline as T

from igs.ingest import jobs
from igs.ingest.sources import render_url
from igs.ingest.verify import verify_source

pytestmark = pytest.mark.db

BASE = "https://nsearchives.nseindia.com/corporate/xbrl/"
Q1_FY25 = dt.date(2024, 6, 30)
Q1_FY26 = dt.date(2025, 6, 30)


def _xbrl_routes() -> dict[str, bytes]:
    results = [
        X.results_row("ACME", Q1_FY25, "12-Aug-2024 18:31:05", BASE + "ACME_Q1FY25.xml", seq="11"),
        X.results_row("ACME", Q1_FY26, "11-Aug-2025 17:45:00", BASE + "ACME_Q1FY26.xml", seq="12"),
        X.results_row("ACME", Q1_FY25, "11-Aug-2025 17:50:00", BASE + "ACME_Q1FY25_rev.xml",
                      seq="14"),
        X.results_row("BETA", Q1_FY25, "09-Aug-2024 15:10:00", BASE + "BETA_Q1FY25.xml",
                      consolidated="Non-Consolidated", seq="13"),
    ]
    shp = [X.shp_row("ACME", Q1_FY25, "19-Jul-2024 16:00:00", BASE + "ACME_SHP_Q1FY25.xml")]
    src = T.SOURCES
    return {
        render_url(src.get("nse_financial_results_index")): X.listing(results),
        render_url(src.get("nse_shareholding_index")): X.listing(shp),
        BASE + "ACME_Q1FY25.xml": X.results_instance("ACME", Q1_FY25, X.nonfin_q(1.00e10)),
        BASE + "ACME_Q1FY26.xml": X.results_instance(
            "ACME", Q1_FY26, X.nonfin_q(1.30e10), year=2024,
            comparatives={"RevenueFromOperations": 0.95e10}),    # column not loaded
        # A year later Q1 FY25 is re-filed with revenue restated from 1.00e10 to 0.95e10.
        BASE + "ACME_Q1FY25_rev.xml": X.results_instance("ACME", Q1_FY25,
                                                         X.nonfin_q(0.95e10)),
        BASE + "BETA_Q1FY25.xml": X.results_instance("BETA", Q1_FY25, X.nonfin_q(4e9),
                                                     basis="Standalone"),
        BASE + "ACME_SHP_Q1FY25.xml": X.shp_simple("ACME", Q1_FY25, total=2.5e8,
                                                   promoter_pct=52.0, pledged_pct=8.0,
                                                   fii_pct=20.0, dii_pct=12.0),
    }


@pytest.fixture
def ctx(tmp_path, db_conn):
    routes = T._routes() | _xbrl_routes()
    store = T.RawStore(tmp_path / "raw")
    fetcher = T.Fetcher(store, min_interval_s=0, host_min_interval_s={},
                        client=httpx.Client(transport=T._transport(routes)))
    return jobs.Context(conn=db_conn, store=store, sources=T.SOURCES, fetcher=fetcher)


def _value(conn, as_of: str, symbol_company: str, period_end: dt.date) -> float | None:
    rows = T._q(conn, """select f.value::float8 from facts_as_of(%s::timestamptz) f
                         join company c using (company_id)
                         where c.name like %s and f.concept = 'revenue'
                           and f.period_type = 'Q' and f.period_end = %s""",
                (as_of, symbol_company + "%", period_end))
    return rows[0][0] if rows else None


def test_step2_pipeline(ctx):
    T._verify_all(ctx)
    for sid in ("nse_financial_results_index", "nse_shareholding_index"):
        assert verify_source(T.SOURCES.get(sid), ctx.fetcher, today=T.TODAY).status == "verified"
    T._ingest_everything(ctx)
    jobs.ingest_static(ctx, "nse_financial_results_index")
    jobs.ingest_static(ctx, "nse_shareholding_index")
    res = jobs.ingest_documents(ctx, "financial_results")
    shp = jobs.ingest_documents(ctx, "shareholding")
    assert [r.http_status for r in res + shp] == [200] * 5
    conn = ctx.conn
    conn.commit()

    assert T._q(conn, "select filing_system, statement_basis, count(*) from filing "
                "group by 1, 2 order by 1, 2") == [
        ("nse_results_reg33", "consolidated", 3), ("nse_results_reg33", "standalone", 1),
        ("nse_shareholding", None, 1)]
    # filed_at is the exchange broadcast time, not the fetch time.
    assert T._q(conn, "select min(filed_at) from filing where filing_type = 'shareholding'") == [
        (dt.datetime(2024, 7, 19, 10, 30, tzinfo=dt.UTC),)]

    # Point in time: original value until the restating filing, restated afterwards.
    assert _value(conn, "2024-08-12 18:31:04+05:30", "Acme", Q1_FY25) is None
    assert _value(conn, "2024-08-12 18:31:05+05:30", "Acme", Q1_FY25) == 1.00e10
    assert _value(conn, "2025-08-01 00:00+05:30", "Acme", Q1_FY25) == 1.00e10
    assert _value(conn, "2025-08-12 00:00+05:30", "Acme", Q1_FY25) == 0.95e10
    assert T._q(conn, """select count(*) from fundamental_fact_versioned
                         where is_restatement and concept = 'revenue'""") == [(1,)]
    assert "restatement" in [i.category for i in ctx.dq.issues]

    assert T._q(conn, "select category, pct_of_total::float8, pledged_pct::float8 "
                "from shareholding where category = 'promoter'") == [("promoter", 52.0, 8.0)]

    before = T._q(conn, "select company_id, statement_basis, period_end, period_type, concept, "
                  "value::float8, filed_at from fundamental_fact order by 1, 2, 3, 4, 5, 7")
    counts = jobs.rebuild_from_raw(ctx)
    assert counts["nse_xbrl_document"] > 0
    after = T._q(conn, "select company_id, statement_basis, period_end, period_type, concept, "
                 "value::float8, filed_at from fundamental_fact order by 1, 2, 3, 4, 5, 7")
    assert before == after


def test_validation_report(ctx, tmp_path, monkeypatch):
    import shutil

    from igs.config import config_dir
    from igs.xbrl.report import validate_fundamentals

    cfg = tmp_path / "config"
    shutil.copytree(config_dir(), cfg)
    (cfg / "hand_checked.yaml").write_text(
        "companies:\n  - {symbol: ACME, archetype: ems}\n  - {symbol: BETA, archetype: bank}\n"
        "expected:\n  - {symbol: ACME, statement_basis: consolidated, period_end: 2024-06-30,\n"
        "     period_type: Q, concept: revenue, value_cr: 950.0}\n")
    monkeypatch.setenv("IGS_CONFIG_DIR", str(cfg))
    T._verify_all(ctx)
    for sid in ("nse_financial_results_index", "nse_shareholding_index"):
        verify_source(T.SOURCES.get(sid), ctx.fetcher, today=T.TODAY)
    T._ingest_everything(ctx)
    jobs.ingest_static(ctx, "nse_financial_results_index")
    jobs.ingest_documents(ctx, "financial_results")
    ctx.conn.commit()
    results, path = validate_fundamentals(ctx.conn, tmp_path / "reports")
    status = {r.name: r.status for r in results}
    assert status == {"hand_checked_mapped": "pass", "eight_quarters": "fail",
                      "missing_quarters": "warn", "accounting_identities": "pass",
                      "hand_checked_values": "pass"}, path.read_text()


def test_dataset_loader_feeds_factors(ctx):
    import igs.factors  # noqa: F401
    from igs.factors.registry import REGISTRY
    from igs.pit import PitView
    from igs.pit.loader import load_dataset
    from igs.timeutil import IST

    T._verify_all(ctx)
    for sid in ("nse_financial_results_index", "nse_shareholding_index"):
        verify_source(T.SOURCES.get(sid), ctx.fetcher, today=T.TODAY)
    T._ingest_everything(ctx)
    jobs.ingest_static(ctx, "nse_financial_results_index")
    jobs.ingest_static(ctx, "nse_shareholding_index")
    jobs.ingest_documents(ctx, "financial_results")
    jobs.ingest_documents(ctx, "shareholding")
    ctx.conn.commit()
    ds = load_dataset(ctx.conn, dt.date(2024, 1, 1), dt.date(2025, 12, 31))
    assert {"facts", "prices", "corporate_actions", "shareholding", "index_prices", "industry",
            "surveillance", "announcements", "filings"} <= set(ds.tables)
    assert ds.tables["prices"]["company_id"].null_count() == 0
    view = PitView(ds, dt.datetime(2025, 9, 1, 23, 59, tzinfo=IST))
    # Every factor runs on database-loaded data (most are insufficient on this tiny history).
    for name, spec in REGISTRY.items():
        out = spec.fn(view)
        assert set(out.columns) == {"company_id", "value", "status", "detail",
                                    "source_fact_ids"}, name
    pledge = REGISTRY["pledge_pct"].fn(view).filter(pl.col("status") == "ok")
    assert pledge["value"].to_list() == [8.0]


def test_documents_wait_for_the_instrument_master(ctx):
    """Seen on a first real load: with no master, every document was fetched, rejected as
    unmapped, and then not fetched again. Now nothing is fetched until the master exists."""
    T._verify_all(ctx)
    assert verify_source(T.SOURCES.get("nse_financial_results_index"), ctx.fetcher,
                         today=T.TODAY).status == "verified"
    jobs.ingest_static(ctx, "nse_financial_results_index")
    ctx.conn.commit()
    with pytest.raises(jobs.MasterNotBuilt, match="4 financial_results documents"):
        jobs.ingest_documents(ctx, "financial_results")
    assert not list(ctx.store.iter_records(jobs.DOCUMENT_SOURCE))
    T._ingest_everything(ctx)                       # prices, then the master
    assert [r.http_status for r in jobs.ingest_documents(ctx, "financial_results")] == [200] * 4
