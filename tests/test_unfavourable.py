"""Measures left undefined by a negative figure are unfavourable, not unknown.

Company 4: company 1's history in tests/test_factors.py, but its last quarter (Mar 2024,
           filed 30 Apr 2024) is a large loss: TTM profit and EBITDA turn negative with
           Rs 200 cr of net debt.
Company 5: profitable, with negative equity.
Company 6: profitable, but profit fell over three years.
Company 7: a turnaround, loss three years ago and profit now: growth stays unknown.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from test_factors import AS_OF, CR, QS, _filed

import igs.factors  # noqa: F401  (registers factors)
from igs.factors.registry import REGISTRY
from igs.pit import PitDataset, PitView
from igs.timeutil import IST


def _dataset() -> PitDataset:
    facts, n = [], 0

    def add(cid, pe, ptype, concept, value):
        nonlocal n
        n += 1
        facts.append({"fact_id": n, "filing_id": n, "company_id": cid,
                      "statement_basis": "consolidated", "period_end": pe, "period_type": ptype,
                      "concept": concept, "value": float(value), "filed_at": _filed(pe)})

    def quarter(cid, q, rev, cost_share, pat_share=0.75):
        fin, dep = 0.01 * rev, 0.03 * rev
        exp = cost_share * rev + fin + dep
        for c, v in {"revenue": rev, "total_expenses": exp, "finance_costs": fin,
                     "depreciation": dep, "pbt": rev - exp,
                     "pat": pat_share * (rev - exp)}.items():
            add(cid, q, "Q", c, v)

    for i, q in enumerate(QS):
        grown = 100 * CR * 1.1 ** i
        quarter(4, q, grown, 1.6 if i == 23 else 0.8)
        quarter(5, q, grown, 0.8)
        quarter(6, q, 100 * CR, 0.8 + 0.004 * i)              # margin 20% -> 10.8%
        quarter(7, q, 100 * CR, 1.05 if i < 12 else 0.8)      # loss, then profit
    for q, eq in ((dt.date(2023, 3, 31), 800 * CR), (dt.date(2024, 3, 31), 1000 * CR)):
        for cid in (4, 5, 6, 7):
            equity = -100 * CR if cid == 5 else eq
            for c, v in {"total_equity": equity, "equity_owners": equity,
                         "borrowings_noncurrent": 200 * CR, "total_assets": eq + 400 * CR,
                         "cash": 0.0}.items():
                add(cid, q, "INSTANT", c, v)

    days, d = [], dt.date(2022, 1, 3)
    while d <= AS_OF.date():
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    px = [{"security_id": 100 + cid, "company_id": cid, "trade_date": day, "close": 500.0,
           "prev_close": 500.0, "volume": 1000, "delivery_pct": 50.0}
          for day in days for cid in (4, 5, 6, 7)]
    idx = [{"index_name": "Nifty 500", "trade_date": day, "close": 1000.0} for day in days]
    q = dt.date(2024, 3, 31)
    filed = dt.datetime.combine(q + dt.timedelta(days=21), dt.time(16), tzinfo=IST)
    shp = [{"filing_id": 900 + cid, "company_id": cid, "period_end": q, "category": "total",
            "shares": 1e7, "pct_of_total": 100.0, "pledged_shares": None, "pledged_pct": None,
            "holders": 1000.0, "filed_at": filed} for cid in (4, 5, 6, 7)]
    industry = pl.DataFrame([{"company_id": c, "basic_industry": "Industrial Products",
                              "valid_from": dt.date(2018, 1, 1)} for c in (4, 5, 6, 7)])
    return PitDataset.from_frames(
        facts=pl.DataFrame(facts, schema_overrides={"filed_at": pl.Datetime("us", "UTC")}),
        prices=pl.DataFrame(px), shareholding=pl.DataFrame(
            shp, schema_overrides={"filed_at": pl.Datetime("us", "UTC"),
                                   "pledged_pct": pl.Float64, "pledged_shares": pl.Float64}),
        index_prices=pl.DataFrame(idx), industry=industry)


@pytest.fixture(scope="module")
def ds():
    return _dataset()


def status(ds, name, as_of=AS_OF):
    out = REGISTRY[name].fn(PitView(ds, as_of))
    return {r["company_id"]: (r["status"], r["value"]) for r in out.iter_rows(named=True)}


@pytest.mark.parametrize("name", ["pe_vs_own_5y_median", "peg_trailing", "ev_ebitda",
                                  "net_debt_to_ebitda", "pat_cagr_3y", "ebitda_cagr_3y",
                                  "pat_quarter_yoy"])
def test_a_loss_is_unfavourable(ds, name):
    assert status(ds, name)[4] == ("unfavourable", None)
    # The day before the loss was filed, the same measure had a value.
    before = status(ds, name, dt.datetime(2024, 4, 29, 23, 59, tzinfo=IST))[4]
    assert before[0] == "ok", (name, before)


def test_negative_equity_is_unfavourable(ds):
    assert status(ds, "pb")[5] == ("unfavourable", None)
    assert status(ds, "pb")[4][0] == "ok"


def test_shrinking_profit_makes_peg_unfavourable_and_its_growth_negative(ds):
    growth = status(ds, "pat_cagr_3y")[6]
    assert growth[0] == "ok" and growth[1] < 0
    assert status(ds, "peg_trailing")[6] == ("unfavourable", None)
    assert status(ds, "pe_vs_own_5y_median")[6][0] == "ok"


def test_a_turnaround_is_still_unknown_growth(ds):
    """A loss three years ago is no base to compound from; not a reason to rank last."""
    assert status(ds, "pat_cagr_3y")[7] == ("insufficient_data", None)
    assert status(ds, "pat_cagr_3y")[5][0] == "ok"
