"""working_capital_days_trend: a line missing in one year is a gap, not a change.

Revenue is a flat Rs 100 cr a quarter (TTM Rs 400 cr both years); balance sheets at
31 Mar 2023 and 31 Mar 2024, in Rs cr:
  company  inventories  receivables  payables
  8        30 -> 40     50 -> 60     20 -> 20   all reported: +18.25 days
  9        30 -> --     50 -> 60     20 -> 20   inventory tag missing this year
  10       -- -> --     50 -> 60     20 -> 20   no inventory either year: zero
  11       -- -> --     -- -> --     -- -> --   nothing reported
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from test_factors import AS_OF, CR, QS, _filed

import igs.factors  # noqa: F401
from igs.factors.registry import REGISTRY
from igs.pit import PitDataset, PitView

SHEETS = {8: ((30, 50, 20), (40, 60, 20)), 9: ((30, 50, 20), (None, 60, 20)),
          10: ((None, 50, 20), (None, 60, 20)), 11: ((None,) * 3, (None,) * 3)}


@pytest.fixture(scope="module")
def result():
    facts = []

    def add(cid, pe, ptype, concept, value):
        facts.append({"fact_id": len(facts) + 1, "filing_id": len(facts) + 1,
                      "company_id": cid, "statement_basis": "consolidated", "period_end": pe,
                      "period_type": ptype, "concept": concept, "value": float(value),
                      "filed_at": _filed(pe)})

    for cid, years in SHEETS.items():
        for q in QS[12:]:
            add(cid, q, "Q", "revenue", 100 * CR)
        for day, lines in zip((dt.date(2023, 3, 31), dt.date(2024, 3, 31)), years,
                              strict=True):
            add(cid, day, "INSTANT", "total_equity", 500 * CR)
            for concept, value in zip(("inventories", "trade_receivables", "trade_payables"),
                                      lines, strict=True):
                if value is not None:
                    add(cid, day, "INSTANT", concept, value * CR)
    industry = pl.DataFrame([{"company_id": c, "basic_industry": "Industrial Products",
                              "valid_from": dt.date(2018, 1, 1)} for c in SHEETS])
    ds = PitDataset.from_frames(
        facts=pl.DataFrame(facts, schema_overrides={"filed_at": pl.Datetime("us", "UTC")}),
        industry=industry)
    out = REGISTRY["working_capital_days_trend"].fn(PitView(ds, AS_OF))
    return {r["company_id"]: (r["status"], r["value"]) for r in out.iter_rows(named=True)}


def test_reported_lines_give_the_change_in_days(result):
    assert result[8][0] == "ok"
    assert result[8][1] == pytest.approx(80 / 400 * 365 - 60 / 400 * 365)      # 18.25


def test_a_line_missing_in_one_year_is_not_an_improvement(result):
    # Counting the missing inventory as zero read as 18.25 days better.
    assert result[9] == ("insufficient_data", None)


def test_a_line_absent_in_both_years_is_zero(result):
    assert result[10][0] == "ok"
    assert result[10][1] == pytest.approx(40 / 400 * 365 - 30 / 400 * 365)    # 9.125


def test_nothing_reported_is_unknown_not_unchanged(result):
    assert result[11] == ("insufficient_data", None)
