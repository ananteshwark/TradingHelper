"""Accounting, data-integrity and market checks: each one is shown clear on the
synthetic market and then tripped by a targeted change to the data, with the
numbers checked where the formula is published (Altman, Piotroski, Beneish)."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest
import synthetic_market as M

from igs.config import RedFlagsConfig, load_red_flags, load_scoring
from igs.factors import base as b
from igs.pit import PitDataset, PitView
from igs.score.red_flags import FLAGS, evaluate
from igs.score.run import assign_tiers
from igs.timeutil import IST

AS_OF = dt.datetime(2024, 11, 29, 23, 59, tzinfo=IST)
CFG = load_red_flags()


@pytest.fixture(scope="module")
def market():
    return M.build()


def _tables(market: PitDataset) -> dict[str, pl.DataFrame]:
    return {k: v.drop("known_at") for k, v in market.tables.items()}


def _with(market: PitDataset, **frames) -> PitDataset:
    t = _tables(market)
    t.update(frames)
    return PitDataset.from_frames(**t)


def _check(ds: PitDataset, name: str, cid: int, as_of: dt.datetime = AS_OF) -> dict:
    out = FLAGS[name](PitView(ds, as_of), [cid], CFG.flag(name))
    df = out if isinstance(out, pl.DataFrame) else pl.DataFrame(out)
    return df.row(0, named=True)


def _edit_facts(market: PitDataset, cond: pl.Expr, factor: float = 1.0,
                value: float | None = None) -> pl.DataFrame:
    f = _tables(market)["facts"]
    new = pl.lit(value) if value is not None else pl.col("value") * factor
    return f.with_columns(pl.when(cond).then(new).otherwise(pl.col("value")).alias("value"))


def _is(cid: int, concept: str, period_type: str | None = None) -> pl.Expr:
    e = (pl.col("company_id") == cid) & (pl.col("concept") == concept)
    return e & (pl.col("period_type") == period_type) if period_type else e


# --------------------------------------------------------------------------- configuration


def test_every_check_is_configured_and_vice_versa():
    assert set(CFG.names()) == set(FLAGS)


def test_bad_severity_is_refused():
    with pytest.raises(ValueError, match="severity"):
        RedFlagsConfig.model_validate({"pledge": {"enabled": True, "severity": "warn"}})
    with pytest.raises(ValueError, match="unavailable_blocks"):
        RedFlagsConfig.model_validate({"pledge": {"unavailable_blocks": "yes"}})


def test_financials_are_not_applicable(market):
    for name in ("cash_not_converting", "accruals", "altman_distress", "beneish_manipulation",
                 "piotroski_weak", "cash_debt_paradox"):
        assert _check(market, name, 3)["status"] == "not_applicable", name


# --------------------------------------------------------------------------- accounting


def test_cash_not_converting(market):
    assert _check(market, "cash_not_converting", 1)["status"] == "clear"
    f = _edit_facts(market, _is(1, "cfo", "FY"), factor=-0.2)
    r = _check(_with(market, facts=f), "cash_not_converting", 1)
    assert r["status"] == "tripped" and "3 fiscal years" in r["message"]


def test_accruals(market):
    assert _check(market, "accruals", 1)["status"] == "clear"
    f = _edit_facts(market, _is(1, "cfo", "FY") & (pl.col("period_end") == dt.date(2024, 3, 31)),
                    value=0.0)
    r = _check(_with(market, facts=f), "accruals", 1)
    assert r["status"] == "tripped" and "of average assets" in r["message"]


def test_altman_value_matches_the_published_formula(market):
    v = PitView(market, AS_OF)
    bs = b.bs_panel(v).filter(pl.col("company_id") == 1).tail(1).row(0, named=True)
    ebit = b.ttm(v, "ebit").filter(pl.col("company_id") == 1)["ebit_ttm"][0]
    ta = bs["total_assets"]
    z = (6.56 * (bs["current_assets"] - bs["current_liabilities"]) / ta
         + 3.26 * bs["other_equity"] / ta + 6.72 * ebit / ta
         + 1.05 * bs["total_equity"] / bs["total_liabilities"])
    r = _check(market, "altman_distress", 1)
    assert json.loads(r["evidence"])["z"] == pytest.approx(z)
    assert r["status"] == ("tripped" if z < 1.1 else "clear")
    # Accumulated losses, a thin equity base and short-term liabilities above current
    # assets put it in the distress zone.
    latest = pl.col("period_end") == dt.date(2024, 9, 30)
    f = _tables(market)["facts"].with_columns(
        pl.when(_is(1, "current_liabilities") & latest).then(pl.col("value") * 3)
          .when(_is(1, "other_equity") & latest).then(pl.col("value") * -0.3)
          .when(_is(1, "total_equity") & latest).then(pl.col("value") * 0.1)
          .when(_is(1, "total_liabilities") & latest).then(pl.col("value") * 2.5)
          .otherwise(pl.col("value")).alias("value"))
    r = _check(_with(market, facts=f), "altman_distress", 1)
    assert r["status"] == "tripped" and json.loads(r["evidence"])["z"] < 1.1


def test_piotroski_counts_components(market):
    r = _check(market, "piotroski_weak", 1)
    ev = json.loads(r["evidence"])
    assert r["status"] == "clear" and ev["components"] >= 7
    # Loss-making, cash-burning, levering up: the score collapses.
    latest = pl.col("period_end") == dt.date(2024, 3, 31)
    f = _edit_facts(market, (pl.col("company_id") == 1) & pl.col("concept").is_in(
        ["pat", "pat_owners", "pbt"]) & (pl.col("period_type") == "Q")
        & (pl.col("period_end") > dt.date(2023, 3, 31)), factor=-1.0)
    f = f.with_columns(pl.when(_is(1, "cfo", "FY") & latest).then(-1e10)
                       .when(_is(1, "borrowings_noncurrent") & latest).then(pl.col("value") * 5)
                       .when(_is(1, "current_assets") & latest).then(pl.col("value") * 0.5)
                       .when(_is(1, "cost_of_materials", "FY") & latest)
                       .then(pl.col("value") * 1.3)
                       .otherwise(pl.col("value")).alias("value"))
    r = _check(_with(market, facts=f), "piotroski_weak", 1)
    ev = json.loads(r["evidence"])
    assert r["status"] == "tripped" and ev["score"] <= 3 and ev["components"] == 9


def test_beneish_components_and_trip(market):
    r = _check(market, "beneish_manipulation", 1)
    ev = json.loads(r["evidence"])
    assert r["status"] == "clear" and ev["m"] < -1.78
    # Steady synthetic company: every index close to 1.
    for k in ("dsri", "gmi", "aqi", "depi", "sgai", "lvgi"):
        assert ev[k] == pytest.approx(1.0, abs=0.2), k
    # Receivables tripling against sales (DSRI ~3) is the classic manipulation signal.
    f = _edit_facts(market, _is(1, "trade_receivables")
                    & (pl.col("period_end") == dt.date(2024, 3, 31)), factor=3.0)
    r = _check(_with(market, facts=f), "beneish_manipulation", 1)
    assert r["status"] == "tripped" and json.loads(r["evidence"])["dsri"] > 2.5


def test_cash_debt_paradox(market):
    assert _check(market, "cash_debt_paradox", 1)["status"] == "clear"
    latest = pl.col("period_end") == dt.date(2024, 9, 30)
    f = _edit_facts(market, _is(1, "cash") & latest, factor=12.0)
    f = f.with_columns(pl.when(_is(1, "borrowings_noncurrent") & latest)
                       .then(pl.col("value") * 12).otherwise(pl.col("value")).alias("value"))
    f = f.with_columns(pl.when(_is(1, "other_income")).then(pl.col("value") * 0.02)
                       .otherwise(pl.col("value")).alias("value"))
    r = _check(_with(market, facts=f), "cash_debt_paradox", 1)
    assert r["status"] == "tripped", r["message"]


def test_exceptional_items(market):
    assert _check(market, "exceptional_items", 1)["status"] == "data_unavailable"
    f = _tables(market)["facts"]
    q = f.filter(_is(1, "pbt", "Q") & (pl.col("period_end") >= dt.date(2023, 12, 31)))
    exc = q.with_columns(pl.lit("exceptional_items").alias("concept"),
                         (pl.col("value") * 0.4).alias("value"),
                         (pl.col("fact_id") + 10_000_000).alias("fact_id"))
    r = _check(_with(market, facts=pl.concat([f, exc])), "exceptional_items", 1)
    assert r["status"] == "tripped" and "40.0% of PBT" in r["message"]


def test_restatement_trips_only_after_it_is_filed(market):
    # LATE restates Q1 FY23 revenue by -10% in the filing of 2023-08-24 17:00 IST.
    before = dt.datetime(2023, 8, 24, 16, 59, tzinfo=IST)
    after = dt.datetime(2023, 9, 1, 23, 59, tzinfo=IST)
    assert _check(market, "restatement", 6, before)["status"] == "clear"
    r = _check(market, "restatement", 6, after)
    assert r["status"] == "tripped" and "-10.0%" in r["message"]
    assert _check(market, "restatement", 6, AS_OF)["status"] == "clear"   # > 365 days later


# --------------------------------------------------------------------------- data integrity


def test_unit_scale_jump_is_a_reject(market):
    assert _check(market, "unit_scale_jump", 2)["status"] == "clear"
    f = _edit_facts(market, _is(2, "revenue", "Q")
                    & (pl.col("period_end") == dt.date(2024, 6, 30)), factor=100.0)
    r = _check(_with(market, facts=f), "unit_scale_jump", 2)
    assert r["status"] == "tripped" and "about 100" in r["message"]
    assert CFG.flag("unit_scale_jump")["severity"] == "reject"


def test_profit_vs_eps_catches_a_unit_error(market):
    f = _tables(market)["facts"]
    q = f.filter(_is(1, "pat_owners", "Q") & (pl.col("period_end") == dt.date(2024, 9, 30)))
    shares, fv = 5e7 * 5, 2.0                       # after GROW's 10 -> 2 split
    eps = q["value"][0] / shares

    def add(eps_value: float) -> pl.DataFrame:
        extra = pl.concat([q.with_columns(pl.lit(c).alias("concept"), pl.lit(v).alias("value"),
                                          (pl.col("fact_id") + 20_000_000 + k)
                                          .alias("fact_id"))
                           for k, (c, v) in enumerate((("eps_basic", eps_value),
                                                       ("paid_up_equity_capital", shares * fv),
                                                       ("face_value", fv)))])
        return pl.concat([f, extra])
    assert _check(_with(market, facts=add(eps)), "unit_scale_jump", 1)["status"] == "clear"
    r = _check(_with(market, facts=add(eps * 100)), "unit_scale_jump", 1)
    assert r["status"] == "tripped" and "EPS x shares" in r["message"]


def test_statement_identity(market):
    assert _check(market, "statement_identity", 1)["status"] == "clear"
    f = _edit_facts(market, _is(1, "total_income", "Q")
                    & (pl.col("period_end") == dt.date(2024, 9, 30)), factor=1.3)
    r = _check(_with(market, facts=f), "statement_identity", 1)
    assert r["status"] == "tripped" and "total_income" in r["message"]


def test_results_overdue_uses_lodr_deadlines_and_extensions(market):
    # Data ends with the September 2024 quarter: December results are due 14 Feb 2025.
    assert _check(market, "results_overdue", 1,
                  dt.datetime(2025, 2, 21, 23, 59, tzinfo=IST))["status"] == "clear"
    r = _check(market, "results_overdue", 1, dt.datetime(2025, 2, 22, 23, 59, tzinfo=IST))
    assert r["status"] == "tripped" and "2025-02-14" in r["message"]
    # March-quarter results get 60 days; SEBI's 2020 extension moves the deadline further.
    f = _tables(market)["facts"].filter(pl.col("period_end") <= dt.date(2019, 12, 31))
    ds = _with(market, facts=f)
    assert _check(ds, "results_overdue", 1,
                  dt.datetime(2020, 7, 15, 23, 59, tzinfo=IST))["status"] == "clear"
    assert _check(ds, "results_overdue", 1,
                  dt.datetime(2020, 8, 10, 23, 59, tzinfo=IST))["status"] == "tripped"


# --------------------------------------------------------------------------- market


def _edit_prices(market: PitDataset, cid: int, fn) -> pl.DataFrame:
    """Apply fn to one company's prices up to AS_OF (the synthetic series runs past it)."""
    px = _tables(market)["prices"].filter(pl.col("trade_date") <= AS_OF.date())
    mine = fn(px.filter(pl.col("company_id") == cid).sort("trade_date"))
    return pl.concat([px.filter(pl.col("company_id") != cid), mine.select(px.columns)])


def test_illiquid(market):
    assert _check(market, "illiquid", 6)["status"] == "clear"
    px = _edit_prices(market, 6, lambda d: d.with_columns(pl.col("volume") // 100))
    r = _check(_with(market, prices=px), "illiquid", 6)
    assert r["status"] == "tripped" and "median traded value" in r["message"]


def test_volatility_runup_and_drawdown(market):
    for name in ("high_volatility", "speculative_runup", "deep_drawdown"):
        assert _check(market, name, 5)["status"] == "clear", name
    n = pl.int_range(pl.len())

    def zigzag(d):
        k = pl.when(n % 2 == 0).then(1.08).otherwise(1 / 1.08)
        return d.with_columns((pl.col("close") * k).alias("close"))
    ds = _with(market, prices=_edit_prices(market, 5, zigzag))
    assert _check(ds, "high_volatility", 5)["status"] == "tripped"

    def ramp(d):
        k = pl.when(n >= pl.len() - 260).then(1 + 6 * (n - (pl.len() - 260)) / 260).otherwise(1)
        return d.with_columns((pl.col("close") * k).alias("close"))
    ds = _with(market, prices=_edit_prices(market, 5, ramp))
    assert _check(ds, "speculative_runup", 5)["status"] == "tripped"

    def crash(d):
        k = pl.when(n >= pl.len() - 15).then(0.4).otherwise(1.0)
        return d.with_columns((pl.col("close") * k).alias("close"))
    ds = _with(market, prices=_edit_prices(market, 5, crash))
    r = _check(ds, "deep_drawdown", 5)
    assert r["status"] == "tripped" and "below the highest close" in r["message"]


def test_trade_for_trade(market):
    assert _check(market, "trade_for_trade", 2)["status"] == "clear"
    px = _edit_prices(market, 2, lambda d: d.with_columns(
        pl.when(pl.int_range(pl.len()) == pl.len() - 1).then(pl.lit("BE"))
          .otherwise(pl.col("series")).alias("series")))
    assert _check(_with(market, prices=px), "trade_for_trade", 2)["status"] == "tripped"


# --------------------------------------------------------------------------- tiers


def _results(n: int = 30) -> pl.DataFrame:
    return pl.DataFrame({"company_id": list(range(1, n + 1)),
                         "composite": [float(n - i) for i in range(n)],
                         "coverage": [1.0] * n})


def _flags(rows: list[tuple[int, str, str, str, bool]]) -> pl.DataFrame:
    return pl.DataFrame([{"company_id": c, "flag": f, "status": s, "severity": sev,
                          "unavailable_blocks": ub, "message": "", "evidence": "{}",
                          "source_ids": [], "source_urls": []} for c, f, s, sev, ub in rows],
                        schema={"company_id": pl.Int64, "flag": pl.Utf8, "status": pl.Utf8,
                                "severity": pl.Utf8, "unavailable_blocks": pl.Boolean,
                                "message": pl.Utf8, "evidence": pl.Utf8,
                                "source_ids": pl.List(pl.Int64),
                                "source_urls": pl.List(pl.Utf8)})


def test_severity_decides_reject_versus_caution():
    sc = load_scoring()
    flags = _flags([(1, "unit_scale_jump", "tripped", "reject", True),
                    (2, "high_volatility", "tripped", "caution", True),
                    (3, "beneish_manipulation", "data_unavailable", "caution", False),
                    (4, "altman_distress", "data_unavailable", "caution", True)])
    r = {x["company_id"]: x for x in assign_tiers(_results(), flags, sc).iter_rows(named=True)}
    assert r[1]["tier"] == "Rejected" and "possible unit error" in r[1]["tier_reason"]
    # Top 10% of 29 eligible names = ranks 1 and 2 (companies 2 and 3).
    assert r[2]["tier"] == "Watchlist" and "caution: high volatility" in r[2]["tier_reason"]
    assert r[3]["tier"] == "High conviction"          # a non-blocking check was unavailable
    assert r[4]["tier"] == "Watchlist"                # outside the band: not high anyway
    assert r[4]["tier_reason"] is None and r[4]["hc_blockers"] == []


def test_every_blocker_is_listed():
    sc = load_scoring()
    flags = _flags([(1, "illiquid", "tripped", "caution", True),
                    (1, "altman_distress", "data_unavailable", "caution", True)])
    extra = pl.DataFrame({"company_id": [1], "reason": ["weights: in the band in 40% of draws"]})
    r = assign_tiers(_results(), flags, sc, extra).filter(pl.col("company_id") == 1).row(
        0, named=True)
    assert r["tier"] == "Watchlist"
    assert set(r["hc_blockers"]) == {"caution: thin trading",
                                     "not checked: balance-sheet distress (Altman Z'')",
                                     "weights: in the band in 40% of draws"}


def test_evaluate_reports_severity(market):
    f = evaluate(PitView(market, AS_OF), [1, 2], CFG)
    sev = dict(f.select("flag", "severity").unique().iter_rows())
    assert sev["pledge"] == "reject" and sev["high_volatility"] == "caution"
    blocks = dict(f.select("flag", "unavailable_blocks").unique().iter_rows())
    assert blocks["pledge"] is True and blocks["beneish_manipulation"] is False
