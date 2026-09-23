"""Exact-value factor tests on a hand-built dataset.

Company 1: revenue_i = 100 cr x 1.1^i over 24 quarters (Jun-2018 .. Mar-2024),
           EBITDA margin exactly 20%, PBT 16%, PAT 12%, finance cost 1% of revenue;
           price 500 flat then rising 0.1% a day for the last 300 sessions;
           a 10 -> 5 face-value split ex 2024-05-02 (after the last shareholding filing).
Company 2: bank-format filer (Private Sector Bank).
Company 3: only three quarters of history.
"""

from __future__ import annotations

import datetime as dt
import json
import math

import polars as pl
import pytest

import igs.factors  # noqa: F401  (registers factors)
from igs.factors import base
from igs.factors.registry import REGISTRY
from igs.pit import PitDataset, PitView
from igs.timeutil import IST

CR = 1e7
QS = []
_d = dt.date(2018, 6, 30)
for _ in range(24):
    QS.append(_d)
    m = _d.month + 3
    y = _d.year + (m > 12)
    m = m - 12 if m > 12 else m
    _d = dt.date(y, m, 30 if m in (6, 9) else 31)
AS_OF = dt.datetime(2024, 5, 15, 23, 59, tzinfo=IST)


def _filed(q: dt.date) -> dt.datetime:
    d = q + dt.timedelta(days=30)
    return dt.datetime(d.year, d.month, d.day, 17, 0, tzinfo=IST)


def _dataset() -> PitDataset:
    facts, n = [], 0

    def add(cid, pe, ptype, concept, value, filed, basis="consolidated"):
        nonlocal n
        n += 1
        facts.append({"fact_id": n, "filing_id": n, "company_id": cid, "statement_basis": basis,
                      "period_end": pe, "period_type": ptype, "concept": concept,
                      "value": float(value), "filed_at": filed})

    for i, q in enumerate(QS):
        rev = 100 * CR * 1.1 ** i
        fin, dep = 0.01 * rev, 0.03 * rev
        exp = 0.8 * rev + fin + dep
        pbt = rev - exp
        for c, v in {"revenue": rev, "total_expenses": exp, "finance_costs": fin,
                     "depreciation": dep, "pbt": pbt, "pat": 0.75 * pbt}.items():
            add(1, q, "Q", c, v, _filed(q))
        add(2, q, "Q", "interest_earned", rev, _filed(q))
        add(2, q, "Q", "provisions", 0.05 * rev, _filed(q))
        add(2, q, "Q", "pbt", 0.2 * rev, _filed(q))
        add(2, q, "Q", "pat", 0.15 * rev, _filed(q))
        if i >= 21:
            add(3, q, "Q", "revenue", rev, _filed(q))
    for q, eq in ((dt.date(2023, 3, 31), 800 * CR), (dt.date(2024, 3, 31), 1000 * CR)):
        for c, v in {"total_equity": eq, "equity_owners": eq, "borrowings_noncurrent": 200 * CR,
                     "total_assets": eq + 400 * CR}.items():
            add(1, q, "INSTANT", c, v, _filed(q))
        add(2, q, "INSTANT", "equity_owners", eq, _filed(q))

    days, d = [], dt.date(2022, 1, 3)
    while d <= dt.date(2024, 5, 15):
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    px, idx = [], []
    for k, day in enumerate(days):
        t = max(0, k - (len(days) - 300))
        close = 500 * 1.001 ** t
        if day >= dt.date(2024, 5, 2):
            close /= 2
        for cid in (1, 2):
            px.append({"security_id": 100 + cid, "company_id": cid, "trade_date": day,
                       "close": close, "prev_close": close, "volume": 1000,
                       "delivery_pct": 50.0})
        idx.append({"index_name": "Nifty 500", "trade_date": day, "close": 1000.0})
    cas = pl.DataFrame([{"ca_id": 1, "security_id": 101, "action_type": "split",
                         "ex_date": dt.date(2024, 5, 2),
                         "announced_at": dt.datetime(2024, 4, 5, tzinfo=IST),
                         "fv_old": 10.0, "fv_new": 5.0, "ratio_a": None, "ratio_b": None,
                         "issue_price": None, "cash_per_share": None}],
                       schema={"ca_id": pl.Int64, "security_id": pl.Int64,
                               "action_type": pl.Utf8, "ex_date": pl.Date,
                               "announced_at": pl.Datetime("us", "UTC"), "fv_old": pl.Float64,
                               "fv_new": pl.Float64, "ratio_a": pl.Float64,
                               "ratio_b": pl.Float64, "issue_price": pl.Float64,
                               "cash_per_share": pl.Float64})
    shp = []
    for i, q in enumerate([dt.date(2023, 9, 30), dt.date(2023, 12, 31), dt.date(2024, 3, 31)]):
        filed = dt.datetime.combine(q + dt.timedelta(days=21), dt.time(16), tzinfo=IST)
        for cid in (1, 2):
            for cat, pct, pledge, holders in (("total", 100.0, None, 1000.0),
                                              ("promoter", 60.0 - i, 10.0 + 1.25 * i, 3.0),
                                              ("institutions_foreign", 10.0 + i, None, 50.0 + i),
                                              ("institutions_domestic", 5.0, None, 20.0)):
                shp.append({"filing_id": 900 + i, "company_id": cid, "period_end": q,
                            "category": cat, "shares": 1e7 * pct / 100, "pct_of_total": pct,
                            "pledged_shares": None, "pledged_pct": pledge, "holders": holders,
                            "filed_at": filed})
    industry = pl.DataFrame([{"company_id": c, "basic_industry": b,
                              "valid_from": dt.date(2018, 1, 1)}
                             for c, b in ((1, "Industrial Products"), (2, "Private Sector Bank"),
                                          (3, "Industrial Products"))])
    return PitDataset.from_frames(
        facts=pl.DataFrame(facts, schema_overrides={"filed_at": pl.Datetime("us", "UTC")}),
        prices=pl.DataFrame(px), corporate_actions=cas,
        shareholding=pl.DataFrame(shp, schema_overrides={"filed_at": pl.Datetime("us", "UTC"),
                                                         "pledged_pct": pl.Float64}),
        index_prices=pl.DataFrame(idx), industry=industry)


@pytest.fixture(scope="module")
def ds():
    return _dataset()


def run(ds, name: str, as_of: dt.datetime = AS_OF) -> dict[int, dict]:
    out = REGISTRY[name].fn(PitView(ds, as_of))
    return {r["company_id"]: r for r in out.iter_rows(named=True)}


TTM = 100 * CR * sum(1.1 ** i for i in range(20, 24))


@pytest.mark.parametrize("name,expected", [
    ("revenue_cagr_3y", 1.1 ** 4 - 1),
    ("revenue_cagr_5y", 1.1 ** 4 - 1),
    ("ebitda_cagr_3y", 1.1 ** 4 - 1),
    ("pat_cagr_3y", 1.1 ** 4 - 1),
    ("revenue_ttm_yoy", 1.1 ** 4 - 1),
    ("pat_ttm_yoy", 1.1 ** 4 - 1),
    ("growth_acceleration_4q", 0.0),
    ("growth_consistency_12q", 12.0),
    ("opm_level", 0.20),
    ("opm_trend_8q", 0.0),
    ("interest_coverage", 17.0),
    ("roce", 0.17 * TTM / (1100 * CR)),
    ("roe", 0.12 * TTM / (900 * CR)),
    ("net_debt_to_ebitda", 200 * CR / (0.2 * TTM)),
    ("pb", 500 * 1.001 ** 299 / 2 * 2e7 / (1000 * CR)),
    ("ev_ebitda", (500 * 1.001 ** 299 / 2 * 2e7 + 200 * CR) / (0.2 * TTM)),
    ("rs_12m_vs_nifty500", 1.001 ** 252 - 1),
    ("rs_6m_vs_nifty500", 1.001 ** 126 - 1),
    ("dma_50_200_state", 1.0),
    ("delivery_pct_20d_vs_1y", 1.0),
    ("promoter_holding_qoq", -1.0),
    ("pledge_pct", 12.5),
    ("pledge_trend", 2.5),
    ("fii_dii_holding_change", 1.0),
    ("institutional_holder_count", 1.0),
])
def test_exact_values(ds, name, expected):
    r = run(ds, name)[1]
    assert r["status"] == "ok", (name, r)
    assert r["value"] == pytest.approx(expected, rel=1e-9, abs=1e-12), (name, r["detail"])


def test_price_vs_200dma(ds):
    closes = [500 * 1.001 ** t for t in range(100, 300)]
    assert run(ds, "price_vs_200dma")[1]["value"] == pytest.approx(
        closes[-1] / (sum(closes) / 200) - 1)


def test_split_after_last_shareholding_does_not_halve_market_cap(ds):
    before = run(ds, "pb", dt.datetime(2024, 5, 1, 23, 59, tzinfo=IST))[1]["value"]
    after = run(ds, "pb", dt.datetime(2024, 5, 2, 23, 59, tzinfo=IST))[1]["value"]
    assert after == pytest.approx(before * 1.001, rel=1e-9)   # one day of drift, no halving


def test_financials_get_not_applicable_never_imputed(ds):
    for name in ("roce", "opm_level", "net_debt_to_ebitda", "interest_coverage", "ev_ebitda"):
        assert run(ds, name)[2]["status"] == "not_applicable", name
    assert run(ds, "roe")[2]["status"] == "ok"
    assert run(ds, "pb")[2]["status"] == "ok"


def test_short_history_is_insufficient_not_filled(ds):
    for name in ("revenue_cagr_3y", "revenue_ttm_yoy", "growth_consistency_12q"):
        r = run(ds, name)[3]
        assert r["status"] == "insufficient_data" and r["value"] is None, name


def test_values_change_only_when_the_filing_is_public(ds):
    before = run(ds, "revenue_cagr_3y", dt.datetime(2024, 4, 30, 16, 59, tzinfo=IST))[1]
    after = run(ds, "revenue_cagr_3y", dt.datetime(2024, 4, 30, 17, 0, tzinfo=IST))[1]
    ttm_before, ttm_after = (json.loads(r["detail"])["ttm_now"] for r in (before, after))
    assert ttm_after / ttm_before == pytest.approx(1.1)


def test_every_value_traces_to_source_facts(ds):
    r = run(ds, "roce")[1]
    assert len(r["source_fact_ids"]) >= 8        # 4 quarters x (PBT, finance costs) + BS
    facts = ds.tables["facts"]
    used = facts.filter(pl.col("fact_id").is_in(r["source_fact_ids"]))
    assert set(used["concept"]) >= {"pbt", "finance_costs", "total_equity"}


def test_cagr_undefined_for_non_positive_base():
    df = pl.DataFrame({"a": [110.0, 110.0, -5.0], "b": [100.0, -10.0, 100.0]})
    out = df.select(base.cagr(pl.col("a"), pl.col("b"), 1.0).alias("g"))["g"].to_list()
    assert out[0] == pytest.approx(0.1) and out[1] is None and out[2] is None


def test_registry_descriptions_and_directions():
    assert len(REGISTRY) == 32
    for spec in REGISTRY.values():
        assert spec.description and not math.isnan(float(spec.higher_is_better))
    lower_better = {n for n, s in REGISTRY.items() if not s.higher_is_better}
    assert lower_better == {"net_debt_to_ebitda", "working_capital_days_trend",
                            "pe_vs_own_5y_median", "peg_trailing", "ev_ebitda", "pb",
                            "pledge_pct", "pledge_trend"}


@pytest.mark.parametrize("keep", [("facts",), ("prices", "index_prices"), ("facts", "prices")])
def test_every_factor_survives_partial_datasets(ds, keep):
    partial = PitDataset({k: v for k, v in ds.tables.items() if k in keep})
    view = PitView(partial, AS_OF)
    for name, spec in REGISTRY.items():
        out = spec.fn(view)
        assert out.columns == list(base.RESULT_SCHEMA), name
        assert out.filter(pl.col("status") == "ok")["value"].null_count() == 0, name
