from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest
import synthetic_market as M

from igs.backtest import calendar as cal
from igs.backtest.costs import impact_rate, round_trip_rate, statutory_rate
from igs.backtest.engine import run_backtest, total_return_prices
from igs.backtest.metrics import ic_summary, ic_verdicts, nav_stats, spearman_ic
from igs.backtest.report import write_ic_status, write_report
from igs.config import load_backtest, load_costs, load_scoring, load_universe

FACTORS = ["revenue_ttm_yoy", "roe", "pb", "risk_adj_return_6m", "volatility_1y", "pledge_pct",
           "growth_consistency_12q"]


def _cfgs():
    sc = load_scoring().model_copy(update={"peer_group": load_scoring().peer_group.model_copy(
        update={"min_peers": 2})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0, "min_filing_quarters": 4})
    return load_backtest(), sc, uc, load_costs()


@pytest.fixture(scope="module")
def market():
    return M.build()


@pytest.fixture(scope="module")
def quarterly_result(market):
    bt, sc, uc, cc = _cfgs()
    return run_backtest(market, dt.date(2020, 1, 1), dt.date(2023, 12, 31), "quarterly", bt, sc,
                        uc, cc, factors=FACTORS, n_quantiles=3)


# --------------------------------------------------------------------------- calendar


def test_calendars():
    days = [d for d in (dt.date(2024, 1, 1) + dt.timedelta(i) for i in range(120))
            if d.weekday() < 5]
    assert cal.monthly(days, days[0], days[-1]) == [dt.date(2024, 1, 31), dt.date(2024, 2, 29),
                                                    dt.date(2024, 3, 29)]
    assert cal.quarterly(days, days[0], days[-1], 60) == [dt.date(2024, 2, 29)]
    assert cal.add_months(dt.date(2024, 1, 31), 1) == dt.date(2024, 2, 29)
    assert cal.next_trading_day(days, dt.date(2024, 3, 29)) == dt.date(2024, 4, 1)


# --------------------------------------------------------------------------- costs


def test_statutory_costs_are_asymmetric_and_small():
    cc = load_costs()
    buy, sell = statutory_rate(cc, "buy", 1e6), statutory_rate(cc, "sell", 1e6)
    assert 0.0011 < buy < 0.0013            # STT 0.1% + stamp 0.015% + small charges
    assert 0.0010 < sell < 0.0012
    assert buy > sell


def test_impact_grows_with_participation_and_is_capped():
    cc = load_costs()
    small, _ = impact_rate(cc, 1e5, 1e8, 0.02, "large")
    big, flag = impact_rate(cc, 5e7, 1e8, 0.02, "small")
    assert small < big and flag
    assert impact_rate(cc, 1e9, 1e6, 0.05, "small")[0] == pytest.approx(0.03)
    assert impact_rate(cc, 1e5, None, None, None) == (0.03, True)   # unknown = worst case
    assert round_trip_rate(cc, 1e5, 1e8, 0.02, "large") > 0.002


# --------------------------------------------------------------------------- metrics


def test_spearman_ic_and_verdicts():
    df = pl.DataFrame({"date": [1] * 10 + [2] * 10, "factor": ["f"] * 20,
                       "z": list(range(10)) * 2,
                       "ret": list(range(10)) + list(range(10))[::-1]})
    ic = spearman_ic(df, ["date", "factor"], "z", "ret", min_n=5)
    assert ic["ic"].to_list() == pytest.approx([1.0, -1.0])
    rows = pl.DataFrame({"date": list(range(24)), "factor": ["good"] * 24,
                         "horizon_m": [3] * 24, "ic": [0.05 + 0.01 * (i % 4) for i in range(24)]})
    rows = pl.concat([rows, rows.with_columns(pl.lit("bad").alias("factor"),
                                              (pl.col("ic") * -1).alias("ic"))])
    v = ic_verdicts(ic_summary(rows, rebalance_months=1), 3, 2.0, 5)
    assert dict(v.select("factor", "verdict").iter_rows()) == {"bad": "DROP", "good": "KEEP"}
    # Non-overlapping sampling: 24 monthly dates, 3-month horizon -> 8 observations.
    assert v["n_obs"].to_list() == [8, 8]


def test_nav_stats():
    s = nav_stats([0.1, -0.5, 0.2], 12)
    assert s["max_drawdown"] == pytest.approx(-0.5)
    assert s["total_return"] == pytest.approx(1.1 * 0.5 * 1.2 - 1)


# --------------------------------------------------------------------------- engine


def test_total_return_prices_remove_split_and_bonus_jumps(market):
    tr = total_return_prices(market).sort("company_id", "trade_date")
    moves = tr.with_columns((pl.col("tr") / pl.col("tr").shift(1).over("company_id") - 1)
                            .abs().alias("m"))
    assert moves["m"].max() < 0.15      # no -80% split day or -50% bonus day


def test_quarterly_backtest_runs_end_to_end(quarterly_result):
    r = quarterly_result
    assert len(r.dates) == 16
    assert r.benchmark_name == "Nifty 500"
    assert "benchmark_not_tri" in [i.category for i in r.dq.issues]
    assert set(r.forward["horizon_m"]) == {3, 6, 12}
    assert r.quantiles["quantile"].max() == 3
    assert r.periods.height == len(r.dates) - 1
    assert (r.periods["cost"] >= 0).all() and (r.periods["net"] <= r.periods["gross"]).all()
    assert {"composite", *FACTORS} >= set(r.ic_status["factor"])


def test_forward_returns_enter_after_signal_date(quarterly_result):
    f = quarterly_result.forward
    assert (f["entry_date"] > f["date"]).all()
    assert (f["exit_date"] > f["entry_date"]).all()


def test_walk_forward_selection_only_uses_realised_ic(market):
    bt, sc, uc, cc = _cfgs()
    bt = bt.model_copy(update={"ic_gate": bt.ic_gate.model_copy(
        update={"min_observations": 2, "min_abs_t": 50.0})})
    res = run_backtest(market, dt.date(2020, 1, 1), dt.date(2023, 12, 31), "quarterly", bt, sc,
                       uc, cc, factors=FACTORS, n_quantiles=3)
    sel = res.selection
    first = sel.filter(pl.col("date") == min(res.dates))
    assert first["used"].all()                      # nothing realised yet: all factors used
    later = sel.filter(pl.col("date") == max(res.dates))
    assert not later["used"].any()                  # impossible t threshold: all gated out


def test_report_and_ic_status(quarterly_result, tmp_path):
    path = write_report(quarterly_result, tmp_path / "bt", "Backtest (synthetic)")
    text = path.read_text()
    assert "Top-quantile portfolio" in text and "Quantile returns" in text
    assert "Personal research tool" not in text
    for csv in ("ic_status.csv", "quantiles.csv", "periods.csv"):
        assert (tmp_path / "bt" / csv).exists()
    status = json.loads(write_ic_status(quarterly_result, tmp_path / "ic.json").read_text())
    assert {r["factor"] for r in status["factors"]} == set(FACTORS)
    assert all(r["verdict"] in ("KEEP", "DROP", "UNTESTED") for r in status["factors"])


def test_constant_factor_has_no_ic_and_is_never_kept():
    df = pl.DataFrame({"date": [1] * 10, "factor": ["flat"] * 10, "z": [4.0] * 10,
                       "ret": [float(i) for i in range(10)]})
    assert spearman_ic(df, ["date", "factor"], "z", "ret", min_n=5).height == 0
    nan_rows = pl.DataFrame({"factor": ["flat"], "horizon_m": [3], "mean_ic": [float("nan")],
                             "ic_ir": [None], "t_stat": [float("inf")], "n_obs": [20],
                             "hit_rate": [1.0], "n_dates": [20]},
                            schema_overrides={"ic_ir": pl.Float64})
    assert ic_verdicts(nan_rows, 3, 2.0, 5)["verdict"][0] == "DROP"


def test_forward_path_never_switches_to_another_security(market):
    from igs.backtest.engine import selected_security_returns
    from igs.factors.base import primary_prices
    from igs.pit import PitView
    from igs.timeutil import end_of_day_ist

    view = PitView(market, end_of_day_ist(dt.date(2022, 12, 30)))
    primary = primary_prices(view).select("company_id", "security_id").unique()
    cid, sid = primary.row(0)
    dates = [dt.date(2023, 1, 2), dt.date(2023, 1, 3)]
    prices = pl.DataFrame({"company_id": [cid, cid, cid],
                           "security_id": [sid, sid, sid + 100000],
                           "trade_date": [dates[0], dates[1], dates[1]],
                           "tr": [100.0, 110.0, 10000.0]})
    frozen = selected_security_returns(prices, view)
    assert frozen["tr"].to_list() == [100.0, 110.0]


def test_research_filter_rejects_unknown_variant(market):
    bt, sc, uc, cc = _cfgs()
    with pytest.raises(ValueError, match='unknown research filter'):
        run_backtest(market, dt.date(2023,1,1), dt.date(2023,12,31),
                     'quarterly',bt,sc,uc,cc,research_filter='invented')


def test_strategy_filter_holds_every_qualified_stock(market, monkeypatch):
    """A strategy filter is held whole (up to 20 names), not the top tenth of the
    filtered list, which would be one stock here."""
    from igs.backtest import engine
    from igs.lab import strategies

    def everyone(view, companies, flags):
        return [{'company_id': c, 'profiles': {'garp': {'status': 'qualified'}}}
                for c in companies]
    monkeypatch.setattr(strategies, 'assess', everyone)
    bt, sc, uc, cc = _cfgs()
    res = run_backtest(market, dt.date(2023, 1, 1), dt.date(2023, 12, 31), 'quarterly',
                       bt, sc, uc, cc, research_filter='garp')
    sizes = res.scores.group_by('date').len()['len'].to_list()
    invested = res.periods.filter(pl.col('status') == 'invested')
    assert invested.height and engine.STRATEGY_MIN_NAMES <= max(sizes)
    assert invested['n_selected'].to_list() == [
        min(n, engine.STRATEGY_MAX_NAMES) for n in sizes][:invested.height]


def test_selection_weights_leave_the_rest_in_cash():
    from igs.backtest.engine import _simulate

    scores = pl.DataFrame([
        {"date": D0, "company_id": c, "security_id": 10 + c, "composite": 1.0, "adv": 1e8,
         "vol": 0.02, "bucket": "large", "weight": 0.1} for c in (1, 2)])
    tr = pl.DataFrame([(1, 11, E0, 100.0), (1, 11, E1, 110.0), (2, 12, E0, 100.0),
                       (2, 12, E1, 90.0)], schema={"company_id": pl.Int64,
                       "security_id": pl.Int64, "trade_date": pl.Date, "tr": pl.Float64},
                      orient="row")
    bench = pl.DataFrame([(E0, 100.0), (E1, 101.0)],
                         schema={"trade_date": pl.Date, "bench": pl.Float64}, orient="row")
    p = _simulate(scores, tr, bench, [E0, E1, E2], 1, load_costs(), [D0, D1])
    assert p["cash_weight"][0] == pytest.approx(0.8)
    assert p["gross"][0] == pytest.approx(0.1 * 0.10 + 0.1 * -0.10)


def test_research_profile_filter_uses_dated_growth_evidence(market):
    bt, sc, uc, cc = _cfgs()
    res = run_backtest(market, dt.date(2023,1,1), dt.date(2023,4,1),
                      'quarterly',bt,sc,uc,cc,research_filter='established')
    # This fixture can have no eligible compounders. Never replace an empty profile
    # with the unfiltered universe or a future snapshot just to produce returns.
    if res.scores.height:
        assert res.scores['composite'].null_count() == 0
    else:
        assert (res.periods["n"] == 0).all()
        assert (res.periods["gross"] == 0).all()


# Portfolio execution regressions: deliberately tiny markets with hand-computable returns.
D0, D1, D2, D3 = [dt.date(2024, m, 1) for m in range(1, 5)]
E0, E1, E2, E3 = [d + dt.timedelta(days=1) for d in (D0, D1, D2, D3)]


def _portfolio(score_rows, price_rows, dates=(D0, D1), benchmark=None, nq=1):
    from igs.backtest.engine import _simulate

    scores = pl.DataFrame([
        {"date": date, "company_id": cid, "security_id": sid, "composite": value,
         "adv": 1e8, "vol": 0.02, "bucket": "large"}
        for date, cid, sid, value in score_rows
    ], schema={"date": pl.Date, "company_id": pl.Int64, "security_id": pl.Int64,
               "composite": pl.Float64, "adv": pl.Float64, "vol": pl.Float64,
               "bucket": pl.Utf8})
    tr = pl.DataFrame(price_rows, schema={"company_id": pl.Int64, "security_id": pl.Int64,
                                         "trade_date": pl.Date, "tr": pl.Float64}, orient="row")
    bench = pl.DataFrame(benchmark if benchmark is not None else
                         [(E0, 100.0), (E1, 101.0), (E2, 102.0), (E3, 103.0)],
                         schema={"trade_date": pl.Date, "bench": pl.Float64}, orient="row")
    return _simulate(scores, tr, bench, [D0, E0, D1, E1, D2, E2, D3, E3],
                     nq, load_costs(), list(dates))


def _execution_result(periods, dates=(D0, D1)):
    from igs.backtest.engine import BacktestResult

    return BacktestResult(frequency="monthly", dates=list(dates), benchmark_name="test",
                          periods=periods, **{name: pl.DataFrame() for name in (
                              "scores", "factor_z", "forward", "ic", "ic_summary", "ic_status",
                              "quantiles", "selection", "universe_sizes")})


def test_portfolio_never_switches_security_or_uses_another_listing_price():
    p = _portfolio([(D0, 1, 10, 1.0)], [(1, 10, E0, 100), (1, 10, E1, 110),
                                       (1, 11, E0, 1000), (1, 11, E1, 1)])
    assert p["gross"][0] == pytest.approx(0.1)
    assert p["n"][0] == 1


@pytest.mark.parametrize("entry", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_missing_or_invalid_entry_leaves_cash_without_reweighting(entry):
    # The second stock has an old quote and a later quote, but no executable close.
    p = _portfolio([(D0, 1, 10, 2.0), (D0, 2, 20, 1.0)],
                   [(1, 10, E0, 100), (1, 10, E1, 120), (2, 20, D0, 100),
                    (2, 20, E0, entry), (2, 20, E1, 200)])
    row = p.row(0, named=True)
    assert row["gross"] == pytest.approx(0.1)  # half earns 20%; half stays cash
    assert row["cash_weight"] == 0.5
    assert row["missing_entry_prices"] == 1
    assert row["n_selected"] == 2 and row["n"] == 1
    assert row["turnover"] == 0.25


def test_entry_is_not_excluded_using_future_exit_availability():
    p = _portfolio([(D0, 1, 10, 2.0), (D0, 2, 20, 1.0)],
                   [(1, 10, E0, 100), (1, 10, E1, 120), (2, 20, E0, 100),
                    (2, 20, D1, 200)])
    row = p.row(0, named=True)
    assert row["n"] == 2 and row["cash_weight"] == 0
    assert row["missing_exit_prices"] == 1
    assert row["gross"] is None and row["net"] is None
    assert row["status"] == "missing_exit_price"


def test_empty_rebalance_liquidates_to_cash_and_preserves_calendar():
    p = _portfolio([(D0, 1, 10, 1.0), (D2, 1, 10, 1.0)],
                   [(1, 10, E0, 100), (1, 10, E1, 110), (1, 10, E2, 220),
                    (1, 10, E3, 242)], dates=(D0, D1, D2, D3))
    assert p["date"].to_list() == [D0, D1, D2]
    assert p["gross"].to_list() == pytest.approx([0.1, 0.0, 0.1])
    assert p["cash_weight"].to_list() == [0, 1, 0]
    assert p["turnover"].to_list() == [0.5, 0.5, 0.5]
    assert p["cost"][1] > 0 and p["net"][1] == -p["cost"][1]
    assert p["status"].to_list() == ["invested", "cash", "invested"]


def test_listing_change_is_charged_as_sale_and_purchase():
    p = _portfolio([(D0, 1, 10, 1.0), (D1, 1, 11, 1.0)],
                   [(1, 10, E0, 100), (1, 10, E1, 100),
                    (1, 11, E1, 500), (1, 11, E2, 550)], dates=(D0, D1, D2))
    assert p["gross"].to_list() == pytest.approx([0, 0.1])
    assert p["turnover"].to_list() == [0.5, 1.0]
    assert p["cost"][1] > p["cost"][0]


def test_missing_exit_does_not_silently_resume_nav_later(tmp_path):
    from igs.backtest.report import summarise

    p = _portfolio([(D0, 1, 10, 1.0), (D1, 1, 10, 1.0)],
                   [(1, 10, E0, 100), (1, 10, D1, 110), (1, 10, E2, 120)],
                   dates=(D0, D1, D2))
    res = _execution_result(p, dates=(D0, D1, D2))
    s = summarise(res)
    assert p["status"].to_list() == ["missing_exit_price", "unresolved_prior_position"]
    assert s["realized_periods"] == 0 and s["incomplete_periods"] == 2
    assert s["benchmark_periods"] == 2 and s["matched_periods"] == 0
    assert s["status"] == "INCONCLUSIVE" and "gross" not in s
    text = write_report(res, tmp_path, "Execution coverage").read_text()
    assert "INCONCLUSIVE: no realized portfolio periods" in text
    assert "withheld" in text
    assert (tmp_path / "periods.csv").exists()


def test_all_cash_is_reported_but_does_not_validate_stock_selection():
    from igs.backtest.report import summarise

    p = _portfolio([], [])
    s = summarise(_execution_result(p))
    assert p["gross"][0] == 0 and p["cost"][0] == 0
    assert s["realized_periods"] == 1 and s["cash_periods"] == 1
    assert s["invested_periods"] == 0 and s["status"] == "INCONCLUSIVE"


def test_benchmark_requires_exact_dates_and_is_not_compressed():
    from igs.backtest.report import summarise

    p = _portfolio([(D0, 1, 10, 1.0), (D1, 1, 10, 1.0)],
                   [(1, 10, E0, 100), (1, 10, E1, 110), (1, 10, E2, 121)],
                   dates=(D0, D1, D2), benchmark=[(E0, 100), (E1, 101), (D2, 102)])
    s = summarise(_execution_result(p, dates=(D0, D1, D2)))
    assert p["bench"][1] is None  # D2 is an old benchmark quote
    assert s["benchmark_coverage"] == 0.5 and s["status"] == "INCONCLUSIVE"
    assert s["benchmark_stats"]["cagr"] is None
    assert s["net"]["total_return"] is not None


def test_no_calendar_intervals_is_inconclusive():
    from igs.backtest.report import summarise

    s = summarise(_execution_result(_portfolio([], [], dates=(D0,)), dates=(D0,)))
    assert s["scheduled_periods"] == 0 and s["realized_periods"] == 0
    assert s["status"] == "INCONCLUSIVE" and "gross" not in s


def test_unavailable_next_session_is_retained_as_incomplete():
    from igs.backtest.report import summarise

    p = _portfolio([], [], dates=(D0, E3))
    s = summarise(_execution_result(p, dates=(D0, E3)))
    assert p["status"][0] == "incomplete_calendar"
    assert s["incomplete_periods"] == 1 and s["realized_periods"] == 0


def test_run_backtest_carries_signal_date_security(quarterly_result, market):
    from igs.factors.base import primary_prices
    from igs.pit import PitView
    from igs.timeutil import end_of_day_ist

    first = min(quarterly_result.dates)
    expected = primary_prices(PitView(market, end_of_day_ist(first))).select(
        "company_id", "security_id").unique()
    actual = quarterly_result.scores.filter(pl.col("date") == first)
    assert actual["security_id"].null_count() == 0
    assert actual.join(expected, on=["company_id", "security_id"]).height == actual.height


def test_completed_prefix_does_not_hide_incomplete_tail():
    from igs.backtest.report import summarise

    p = _portfolio([(D0, 1, 10, 1.0), (D1, 1, 10, 1.0)],
                   [(1, 10, E0, 100), (1, 10, E1, 110)], dates=(D0, D1, D2))
    s = summarise(_execution_result(p, dates=(D0, D1, D2)))
    assert s["realized_periods"] == 1 and s["incomplete_periods"] == 1
    assert s["status"] == "INCONCLUSIVE" and "gross" not in s


def test_research_comparison_counts_completed_periods(monkeypatch, tmp_path):
    from igs.backtest import research

    p = _portfolio([(D0, 1, 10, 1.0)], [(1, 10, E0, 100)])
    result = _execution_result(p)
    monkeypatch.setattr(research, "require_gate", lambda: None)
    monkeypatch.setattr(research, "load_dataset", lambda *a, **kw: None)
    monkeypatch.setattr(research, "run_backtest", lambda *a, **kw: result)
    report = research.compare(None, D0, D1, tmp_path)
    # The three frozen strategy hypotheses now join the five existing comparisons.
    assert len(report["variants"]) == 8
    assert {'inflection', 'compounder', 'garp'} <= report['variants'].keys()
    for variant in report["variants"].values():
        assert variant["scheduled_periods"] == 1
        assert variant["realized_periods"] == 0
        assert variant["status"] == "INCONCLUSIVE"
