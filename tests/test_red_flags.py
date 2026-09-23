from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
import synthetic_market as M

from igs.config import load_red_flags
from igs.pit import PitDataset, PitView
from igs.score import red_flags as RF
from igs.timeutil import IST

AS_OF = dt.datetime(2024, 11, 29, 23, 59, tzinfo=IST)
CFG = load_red_flags()


@pytest.fixture(scope="module")
def market():
    return M.build()


def _flag(view, name, cid):
    rows = RF.FLAGS[name](view, [cid], CFG.flag(name))
    return rows[0]


def _with(market: PitDataset, **frames) -> PitDataset:
    tables = {k: v.drop("known_at") for k, v in market.tables.items()}
    tables.update(frames)
    return PitDataset.from_frames(**tables)


def test_pledge_and_promoter_decline(market):
    v = PitView(market, AS_OF)
    assert _flag(v, "pledge", 1)["status"] == "tripped"          # 45.5% > 20%
    assert _flag(v, "pledge", 3)["status"] == "clear"
    # GAPS sheds 0.4 pp a quarter: 0.8 pp over two quarters is below the 5 pp limit.
    r = _flag(v, "promoter_holding_decline", 5)
    assert r["status"] == "clear" and "limit 5 pp" in r["message"]


def test_receivable_days_spike(market):
    v = PitView(market, AS_OF)
    assert _flag(v, "receivable_days_spike", 1)["status"] == "clear"
    assert _flag(v, "receivable_days_spike", 3)["status"] == "not_applicable"   # bank
    facts = market.tables["facts"].drop("known_at")
    spike = facts.with_columns(
        pl.when((pl.col("company_id") == 2) & (pl.col("concept") == "trade_receivables")
                & (pl.col("period_end") == pl.col("period_end").filter(
                    (pl.col("company_id") == 2)
                    & (pl.col("concept") == "trade_receivables")).max()))
          .then(pl.col("value") * 3).otherwise(pl.col("value")).alias("value"))
    r = _flag(PitView(_with(market, facts=spike), AS_OF), "receivable_days_spike", 2)
    assert r["status"] == "tripped" and "3-year median" in r["message"]


def test_equity_dilution_ignores_splits_and_bonuses(market):
    v = PitView(market, AS_OF)
    # GROW split 10 -> 2 and CYCL issued a 1:1 bonus: neither is dilution.
    assert _flag(v, "equity_dilution", 1)["status"] == "clear"
    assert _flag(v, "equity_dilution", 2)["status"] == "clear"
    shp = market.tables["shareholding"].drop("known_at")
    issued = shp.with_columns(
        pl.when((pl.col("company_id") == 4) & (pl.col("category") == "total"))
          .then(pl.col("shares") * pl.when(pl.col("period_end") >= dt.date(2023, 6, 30))
                .then(1.08).otherwise(1.0)
                * pl.when(pl.col("period_end") >= dt.date(2024, 3, 31)).then(1.06)
                .otherwise(1.0))
          .otherwise(pl.col("shares")).alias("shares"))
    r = _flag(PitView(_with(market, shareholding=issued), AS_OF), "equity_dilution", 4)
    assert r["status"] == "tripped" and "2 share issues" in r["message"]


def test_other_income_share(market):
    v = PitView(market, AS_OF)
    r = _flag(v, "other_income_share", 1)
    assert r["status"] == "clear"         # 2% of revenue vs 16% PBT margin = 12.5%
    assert _flag(v, "other_income_share", 3)["status"] == "not_applicable"
    facts = market.tables["facts"].drop("known_at")
    heavy = facts.with_columns(pl.when((pl.col("company_id") == 1)
                                       & (pl.col("concept") == "other_income"))
                               .then(pl.col("value") * 3).otherwise(pl.col("value"))
                               .alias("value"))
    r = _flag(PitView(_with(market, facts=heavy), AS_OF), "other_income_share", 1)
    assert r["status"] == "tripped" and r["source_ids"]


def test_flags_without_data_are_unavailable_not_clear(market):
    v = PitView(market, AS_OF)
    for name in ("auditor_qualification", "resignations", "surveillance",
                 "contingent_liabilities"):
        assert _flag(v, name, 1)["status"] == "data_unavailable", name


def test_audit_qualification_from_announcements(market):
    ann = pl.DataFrame([{"ann_id": 7, "company_id": 2, "symbol": "CYCL",
                         "filed_at": dt.datetime(2024, 5, 20, 18, tzinfo=IST),
                         "category": "Statement on Impact of Audit Qualifications",
                         "subject": "Auditor has expressed a qualified opinion on inventory",
                         "attachment_url": None}],
                       schema_overrides={"filed_at": pl.Datetime("us", "UTC")})
    v = PitView(_with(market, announcements=ann), AS_OF)
    r = _flag(v, "auditor_qualification", 2)
    assert r["status"] == "tripped" and r["source_ids"] == [7]
    assert _flag(v, "auditor_qualification", 1)["status"] == "clear"
    # Before the announcement existed it cannot have been seen.
    early = PitView(_with(market, announcements=ann), dt.datetime(2024, 5, 1, tzinfo=IST))
    assert _flag(early, "auditor_qualification", 2)["status"] != "tripped"
