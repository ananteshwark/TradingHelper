"""Industry classification: NSE's four-level classification where loaded, else the label
NSE puts on each company's announcements (smIndustry), point in time."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
import synthetic_market as M

from igs.config import load_scoring, load_universe
from igs.factors import base as b
from igs.pit import PitDataset, PitView
from igs.pit.harness import check_no_lookahead
from igs.score.run import composite_at
from igs.timeutil import IST

T0 = dt.datetime(2024, 1, 10, 11, tzinfo=dt.UTC)
T1 = dt.datetime(2024, 6, 10, 11, tzinfo=dt.UTC)
AS_OF = dt.datetime(2024, 3, 29, 23, 59, tzinfo=IST)
LATER = dt.datetime(2024, 9, 30, 23, 59, tzinfo=IST)


def _announcements(labels: list[tuple[int, dt.datetime, str | None]]) -> pl.DataFrame:
    return pl.DataFrame([{"ann_id": i, "company_id": cid, "symbol": f"S{cid}", "filed_at": at,
                          "category": "Board Meeting Intimation", "subject": "Board meeting",
                          "attachment_url": None, "industry_label": label}
                         for i, (cid, at, label) in enumerate(labels, 1)],
                        schema_overrides={"filed_at": pl.Datetime("us", "UTC"),
                                          "attachment_url": pl.Utf8, "industry_label": pl.Utf8})


def _dataset() -> PitDataset:
    facts = pl.DataFrame([{"fact_id": c, "filing_id": c, "company_id": c,
                           "statement_basis": "standalone", "period_end": dt.date(2023, 12, 31),
                           "period_type": "Q", "concept": "revenue", "value": 1.0,
                           "filed_at": T0} for c in range(1, 7)])
    industry = pl.DataFrame([{"company_id": 1, "macro_sector": "Financial Services",
                              "sector": "Financial Services", "industry": "Banks",
                              "basic_industry": "Private Sector Bank",
                              "valid_from": dt.date(2023, 1, 1)}])
    ann = _announcements([
        (1, T0, "Finance"),                 # NSE classification loaded: label not used
        (2, T0, "Pharmaceuticals"),
        (2, T1, "Healthcare Services"),     # relabelled after AS_OF
        (3, T0, "Banks"),
        (4, T0, "Miscellaneous"),           # a catch-all, not an industry
        (5, T1, "Finance - Housing"),       # first labelled after AS_OF
        (6, T0, None),                      # announcement without a label
    ])
    return PitDataset.from_frames(facts=facts, industry=industry, announcements=ann)


def test_nse_classification_first_then_the_latest_known_label():
    ds = _dataset()
    got = {r["company_id"]: (r["industry"], r["sector"], r["industry_source"])
           for r in b.classification(PitView(ds, AS_OF)).iter_rows(named=True)}
    assert got == {1: ("Banks", "Financial Services", "nse_classification"),
                   2: ("Pharmaceuticals", None, "announcement_label"),
                   3: ("Banks", None, "announcement_label")}
    later = {r["company_id"]: r["industry"]
             for r in b.classification(PitView(ds, LATER)).iter_rows(named=True)}
    assert later[2] == "Healthcare Services" and later[5] == "Finance - Housing"
    assert 4 not in later and 6 not in later


def test_labels_select_financial_modules():
    ds = _dataset()
    assert dict(b.modules(PitView(ds, AS_OF)).iter_rows()) == {
        1: "bank", 2: "default", 3: "bank", 4: "default", 5: "default", 6: "default"}
    assert dict(b.modules(PitView(ds, LATER)).iter_rows())[5] == "nbfc"


@pytest.mark.lookahead
def test_industry_classification_is_point_in_time():
    ds = _dataset()
    check_no_lookahead(lambda v: b.classification(v).join(b.modules(v), on="company_id"),
                       ds, [AS_OF, LATER], name="industry classification")


def test_market_without_nse_classification_is_scored_within_label_peers():
    """With the quote API unavailable the synthetic market has no four-level
    classification; its announcement labels still give peer groups and modules."""
    market = M.build()
    t = {k: v.drop("known_at") for k, v in market.tables.items() if k != "industry"}
    labels = {1: "Engineering", 2: "Engineering", 3: "Banks", 4: "Finance", 5: "Engineering",
              6: "Miscellaneous"}
    t["announcements"] = _announcements([(c, dt.datetime(2017, 5, 2, 11, tzinfo=dt.UTC), lab)
                                         for c, lab in labels.items()])
    ds = PitDataset.from_frames(**t)
    sc = load_scoring().model_copy(update={"peer_group": load_scoring().peer_group.model_copy(
        update={"min_peers": 2})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0})
    view, universe, norm, _, _ = composite_at(ds, M.GATE_DATES[-1], sc, uc, set())
    u = {r["company_id"]: (r["industry"], r["industry_source"])
         for r in universe.iter_rows(named=True)}
    assert u[1] == ("Engineering", "announcement_label") and u[6] == (None, None)
    assert dict(b.modules(view).iter_rows())[3] == "bank"
    assert dict(b.modules(view).iter_rows())[4] == "nbfc"
    ok = norm.filter(pl.col("z").is_not_null())
    assert set(ok.filter(pl.col("company_id") == 1)["peer_group"]) == {"Engineering"}
    # Unclassified: no peers to compare with, so nothing is z-scored for it.
    six = norm.filter(pl.col("company_id") == 6)
    assert six["z"].null_count() == six.height
    assert "insufficient_peers" in set(six["status"])


@pytest.mark.db
def test_labels_are_stored_and_loaded_point_in_time(db_conn, tmp_path):
    """Real announcements through the loader: the label and ISIN are kept as published and
    reach the point-in-time dataset."""
    from pathlib import Path

    from igs.config import load_sources
    from igs.ingest import jobs
    from igs.ingest.raw_store import RawStore
    from igs.pit.loader import load_dataset

    store = RawStore(tmp_path / "raw")
    rec = store.put_file(source_id="nse_announcements", path=Path(__file__).parent / "fixtures"
                         / "real" / "announcements_first50.json")
    store.index_record(db_conn, rec)
    ctx = jobs.Context(conn=db_conn, store=store, sources=load_sources())
    # 48: BATAINDIA's three identical announcements (same time and subject) are one row.
    assert jobs.HANDLERS["nse_announcements"](ctx, rec, store.read_bytes(rec)) == 48
    with db_conn.cursor() as cur:
        cur.execute("select industry_label, isin from announcement where symbol = 'ULTRACEMCO'")
        assert set(cur.fetchall()) == {("Cement And Cement Products", "INE481G01011")}
    ds = load_dataset(db_conn, dt.date(2026, 1, 1), dt.date(2026, 9, 30))
    ann = ds.tables["announcements"]
    assert ann["industry_label"].drop_nulls().len() == 16
