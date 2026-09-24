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

FACTORS = ["revenue_ttm_yoy", "roe", "pb", "rs_6m_vs_nifty500", "pledge_pct",
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
    assert "Personal research tool" in text
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
