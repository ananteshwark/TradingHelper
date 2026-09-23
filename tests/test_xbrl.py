from __future__ import annotations

import datetime as dt

import documented_xbrl as X
import polars as pl
import pytest

from igs.dq import DQLog
from igs.xbrl import checks
from igs.xbrl.instance import XbrlError, parse_instance
from igs.xbrl.listing import parse_listing
from igs.xbrl.results import TaxonomyMismatch, XbrlMappingError, extract_results, period_type
from igs.xbrl.shareholding import extract_shareholding

Q1 = dt.date(2024, 6, 30)
Q4 = dt.date(2025, 3, 31)
HOSTS = ["nsearchives.nseindia.com"]


def _doc(**kw) -> bytes:
    kw.setdefault("values", X.nonfin_q(1e10, other=2e8))
    return X.results_instance("ACME", kw.pop("period_end", Q1), **kw)


def test_instance_structure():
    inst = parse_instance(_doc(comparatives={"RevenueFromOperations": 8e9}))
    assert inst.taxonomy_year() == "2022"
    assert inst.units["INRPerShare"] == "INR/shares"
    assert inst.contexts["OneD"].start == dt.date(2024, 4, 1)
    assert inst.contexts["SegD"].dims == (("SegmentsAxis", "SegmentAMember"),)
    assert {f.context for f in inst.by_name["RevenueFromOperations"]} == {"OneD", "ThreeD", "SegD"}


@pytest.mark.parametrize("bad,match", [
    (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>', "DTD"),
    (b"<root/>", "not xbrli:xbrl"),
    (b"<xbrli:xbrl", "well-formed"),
])
def test_instance_rejects(bad, match):
    with pytest.raises(XbrlError, match=match):
        parse_instance(bad)


def test_periods_come_from_column_ids_not_declared_dates():
    """Every column declares the current quarter (as in real NSE instances). The current
    column is loaded; the unverified prior-year column is skipped and reported, not
    loaded under the declared (wrong) period."""
    dq = DQLog()
    rf = extract_results(parse_instance(_doc(comparatives={"RevenueFromOperations": 8e9})), dq)
    assert rf.statement_basis == "consolidated" and rf.results_format == "default"
    assert rf.period_end == Q1 and rf.taxonomy_version == "2022"
    rev = sorted((f["period_end"], f["period_type"], f["value"]) for f in rf.facts
                 if f["concept"] == "revenue")
    assert rev == [(Q1, "Q", 1e10)]                  # the segment fact is not a total either
    cats = [i.category for i in dq.issues]
    assert "xbrl_columns_skipped" in cats and "xbrl_conflicting_values" not in cats


def test_prior_year_column_once_configured(tmp_path):
    import yaml

    from igs.config import config_dir
    cfg = yaml.safe_load((config_dir() / "xbrl_concepts.yaml").read_text())
    cfg["nse_columns"]["ThreeD"] = "same_period_last_year"
    path = tmp_path / "xbrl_concepts.yaml"
    path.write_text(yaml.safe_dump(cfg))
    rf = extract_results(parse_instance(_doc(comparatives={"RevenueFromOperations": 8e9})),
                         DQLog(), path=path)
    rev = sorted((f["period_end"], f["period_type"], f["value"]) for f in rf.facts
                 if f["concept"] == "revenue")
    assert rev == [(dt.date(2023, 6, 30), "Q", 8e9), (Q1, "Q", 1e10)]


def test_year_to_date_is_not_mistaken_for_the_quarter():
    q3 = dt.date(2024, 12, 31)
    dq = DQLog()
    rf = extract_results(parse_instance(_doc(period_end=q3, fy_values={
        "RevenueFromOperations": 3e10, "ProfitLossForPeriod": 3e9})), dq)
    q = {f["concept"]: f["value"] for f in rf.facts if f["period_type"] == "Q"}
    assert q["revenue"] == 1e10 and all(f["period_type"] in ("Q", "INSTANT") for f in rf.facts)
    assert "xbrl_conflicting_values" not in [i.category for i in dq.issues]


def test_conflicting_values_are_dropped_not_chosen():
    extra = ('<in-capmkt:RevenueFromOperations contextRef="OneD" unitRef="INR" '
             'decimals="-5">12000000000</in-capmkt:RevenueFromOperations>')
    dq = DQLog()
    with pytest.raises(XbrlMappingError, match="revenue"):
        extract_results(parse_instance(_doc(extra=extra)), dq)
    assert "xbrl_conflicting_values" in [i.category for i in dq.issues]


def test_q4_filing_carries_full_year():
    rf = extract_results(parse_instance(_doc(period_end=Q4, fy_values={
        "RevenueFromOperations": 4e10, "ProfitLossForPeriod": 5e9})), DQLog())
    fy = {(f["concept"], f["period_type"]) for f in rf.facts if f["period_type"] == "FY"}
    assert fy == {("revenue", "FY"), ("pat", "FY")}


def test_2024_taxonomy_inherits_and_unknown_year_refused():
    rf = extract_results(parse_instance(_doc(year=2024)), DQLog())
    assert rf.taxonomy_version == "2024" and any(f["concept"] == "revenue" for f in rf.facts)
    with pytest.raises(TaxonomyMismatch, match="2019"):
        extract_results(parse_instance(_doc(year=2019)), DQLog())


def test_missing_basis_is_an_error():
    with pytest.raises(XbrlMappingError, match="standalone from consolidated"):
        extract_results(parse_instance(_doc(basis="")), DQLog())


def test_missing_required_concept_means_mapping_mismatch():
    vals = X.nonfin_q(1e10)
    vals.pop("RevenueFromOperations")
    with pytest.raises(XbrlMappingError, match="revenue"):
        extract_results(parse_instance(_doc(values=vals)), DQLog())


def test_unit_mismatch_and_unmapped_elements_are_reported():
    dq = DQLog()
    vals = X.nonfin_q(1e10) | {"SomeBrandNewElement2025": 42}
    rf = extract_results(parse_instance(_doc(values=vals, units_override={
        "OtherExpenses": "pure"})), dq)
    cats = [i.category for i in dq.issues]
    assert "xbrl_unit_mismatch" in cats and "xbrl_unmapped_element" in cats
    assert not any(f["concept"] == "other_expenses" for f in rf.facts)
    unmapped = next(i for i in dq.issues if i.category == "xbrl_unmapped_element")
    assert "SomeBrandNewElement2025" in unmapped.details["elements"]


def test_bank_format():
    vals = {"InterestEarned": 3e10, "OtherIncome": 5e9, "InterestExpended": 1.6e10,
            "OperatingExpenses": 8e9, "ProvisionsOtherThanTaxAndContingencies": 2e9,
            "ProfitBeforeTax": 9e9, "TaxExpense": 2.2e9, "ProfitLossForPeriod": 6.8e9,
            "PercentageOfGrossNpa": 0.0124}
    rf = extract_results(parse_instance(_doc(values=vals, basis="Standalone")), DQLog())
    assert rf.results_format == "bank" and rf.statement_basis == "standalone"
    assert {f["concept"] for f in rf.facts} >= {"interest_earned", "provisions", "gnpa_pct"}


def test_period_types():
    from igs.xbrl.instance import Context
    assert period_type(Context("a", None, dt.date(2024, 1, 1), dt.date(2024, 3, 31), None)) == "Q"
    assert period_type(Context("b", None, dt.date(2023, 4, 1), dt.date(2024, 3, 31), None)) == "FY"
    assert period_type(Context("c", None, None, None, dt.date(2024, 3, 31))) == "INSTANT"
    # In an NSE-style Q4 filing the year-to-date column is the full year, from the
    # filing's own financial-year dates (its declared period is the quarter).
    rf = extract_results(parse_instance(_doc(period_end=Q4, fy_values={
        "RevenueFromOperations": 4e10, "ProfitLossForPeriod": 5e9})), DQLog())
    fy = [f for f in rf.facts if f["period_type"] == "FY"]
    assert fy and {f["period_start"] for f in fy} == {dt.date(Q4.year - 1, 4, 1)}


def test_shareholding_extraction():
    doc = X.shp_simple("ACME", Q1, total=1e8, promoter_pct=55.0, pledged_pct=12.5,
                       fii_pct=18.0, dii_pct=11.0)
    period, rows = extract_shareholding(parse_instance(doc), DQLog())
    by = {r["category"]: r for r in rows}
    assert period == Q1
    assert by["promoter"]["pct_of_total"] == 55.0 and by["promoter"]["pledged_pct"] == 12.5
    assert by["total"]["shares"] == 1e8
    assert by["institutions_foreign"]["holders"] == 120


def test_shareholding_without_promoter_is_refused():
    doc = X.shp_instance("ACME", Q1, {"": {"NumberOfShares": 1}, "OddMember": {
        "NumberOfShares": 1}})
    dq = DQLog()
    with pytest.raises(XbrlMappingError, match="promoter"):
        extract_shareholding(parse_instance(doc), dq)
    assert dq.issues[0].details["members"] == ["OddMember"]


def test_listing_parser():
    dq = DQLog()
    rows = [X.results_row("ACME", Q1, "12-Aug-2024 18:31:05",
                          "https://nsearchives.nseindia.com/corporate/xbrl/A.xml"),
            X.results_row("BETA", Q1, "13-Aug-2024", "https://evil.example.com/B.xml"),
            X.results_row("GAMMA", Q1, "14-Aug-2024",
                          "https://nsearchives.nseindia.com/corporate/xbrl/G.xml",
                          consolidated="Non-Consolidated"),
            {"symbol": "DELTA", "toDate": "30-Jun-2024"}]
    df = parse_listing(X.listing(rows), "financial_results", "nse_results_reg33", HOSTS, dq)
    assert df["symbol"].to_list() == ["ACME", "GAMMA"]
    assert df["filed_at"][0].isoformat() == "2024-08-12T18:31:05+05:30"
    assert df["filed_at_precise"].to_list() == [True, False]
    assert df["basis_hint"].to_list() == ["consolidated", "standalone"]
    assert sorted(i.category for i in dq.issues) == [
        "document_host_not_allowed", "filed_at_date_only", "listing_row_incomplete"]


def _facts(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"fact_id": i, "filing_id": r[0], "company_id": 1, "statement_basis": "consolidated",
          "period_end": r[1], "period_type": r[2], "concept": r[3], "value": r[4],
          "filed_at": dt.datetime(2025, 1, 1, tzinfo=dt.UTC)} for i, r in enumerate(rows)])


def test_identity_breaks_catch_mapping_errors():
    ok = _facts([(1, Q1, "Q", "revenue", 100.0e7), (1, Q1, "Q", "other_income", 5.0e7),
                 (1, Q1, "Q", "total_income", 105.0e7)])
    assert checks.identity_breaks(ok).height == 0
    bad = _facts([(1, Q1, "Q", "revenue", 100.0e7), (1, Q1, "Q", "other_income", 5.0e7),
                  (1, Q1, "Q", "total_income", 125.0e7)])
    br = checks.identity_breaks(bad)
    assert br["identity"].to_list() == ["total_income"]


def test_missing_quarters_are_listed_not_filled():
    f = _facts([(1, dt.date(2023, 12, 31), "Q", "revenue", 1.0),
                (2, dt.date(2024, 6, 30), "Q", "revenue", 1.0)])
    assert checks.missing_quarters(f)["period_end"].to_list() == [dt.date(2024, 3, 31)]


def test_compare_expected():
    f = checks.latest(_facts([(1, Q1, "Q", "revenue", 1234.5e7)]))
    exp = [{"symbol": "ACME", "statement_basis": "consolidated", "period_end": "2024-06-30",
            "period_type": "Q", "concept": "revenue", "value_cr": 1234.5},
           {"symbol": "ACME", "statement_basis": "consolidated", "period_end": "2024-06-30",
            "period_type": "Q", "concept": "pat", "value_cr": 100.0}]
    out = checks.compare_expected(f, exp, {"ACME": 1})
    assert out["status"].to_list() == ["ok", "missing"]


def test_hand_checked_list_has_required_archetypes():
    cfg = checks.load_hand_checked()
    kinds = {c["archetype"] for c in cfg["companies"]}
    assert len(cfg["companies"]) == 20
    assert {"bank", "nbfc", "ems", "commodity_cyclical"} <= kinds
    assert cfg["expected"] == []     # to be typed in by hand, never generated
