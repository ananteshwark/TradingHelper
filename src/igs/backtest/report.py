"""Backtest report: Markdown summary plus CSVs, and the IC status file that
production scoring reads."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from igs.backtest.engine import BacktestResult
from igs.backtest.metrics import nav_stats
from igs.guardrails import DISCLAIMER, assert_no_advice_language
from igs.recon.report import _table
from igs.timeutil import utc_now

PERIODS_PER_YEAR = {"monthly": 12, "quarterly": 4}


def _fmt(x: float | None, pct: bool = True) -> str:
    if x is None:
        return "n/a"
    return f"{x:.2%}" if pct else f"{x:.2f}"


def summarise(res: BacktestResult) -> dict:
    ppy = PERIODS_PER_YEAR[res.frequency]
    p = res.periods.drop_nulls("gross") if res.periods.height else res.periods
    out = {"frequency": res.frequency, "rebalances": len(res.dates),
           "benchmark": res.benchmark_name}
    if p.height:
        out["gross"] = nav_stats(p["gross"].to_list(), ppy)
        out["net"] = nav_stats(p["net"].to_list(), ppy)
        bench = p.drop_nulls("bench")["bench"].to_list()
        out["benchmark_stats"] = nav_stats(bench, ppy)
        out["avg_turnover"] = p["turnover"].mean()
        out["avg_cost_per_rebalance"] = p["cost"].mean()
        out["hit_rate_vs_benchmark"] = (p.drop_nulls("bench")
                                        .select((pl.col("net") > pl.col("bench")).mean())
                                        .item()) if bench else None
    return out


def spread_table(res: BacktestResult) -> pl.DataFrame:
    q = res.quantiles
    if q.height == 0:
        return pl.DataFrame()
    avg = q.group_by("horizon_m", "quantile").agg(pl.col("ret").mean(),
                                                  (pl.col("ret") - pl.col("bench_ret")).mean()
                                                  .alias("excess"), pl.len().alias("dates"))
    return avg.sort("horizon_m", "quantile")


def write_report(res: BacktestResult, out_dir: Path, title: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    s = summarise(res)
    for name, df in (("ic_by_date", res.ic), ("ic_summary", res.ic_summary),
                     ("ic_status", res.ic_status), ("quantiles", res.quantiles),
                     ("periods", res.periods), ("factor_selection", res.selection),
                     ("universe_sizes", res.universe_sizes)):
        if df.height:
            df.write_csv(out_dir / f"{name}.csv")
    lines = [f"# {title}", "", f"_{DISCLAIMER}_", "",
             f"Generated {utc_now().isoformat()}. Frequency: **{res.frequency}**, "
             f"{s['rebalances']} rebalances, benchmark: **{res.benchmark_name}**.", ""]
    if "gross" in s:
        g, n, b = s["gross"], s["net"], s["benchmark_stats"]
        lines += ["## Top-quantile portfolio (equal weight, held to next rebalance)", "",
                  "| | CAGR | volatility | max drawdown | total |", "|---|---|---|---|---|",
                  f"| gross | {_fmt(g['cagr'])} | {_fmt(g['vol'])} | {_fmt(g['max_drawdown'])} "
                  f"| {_fmt(g['total_return'])} |",
                  f"| net of costs | {_fmt(n['cagr'])} | {_fmt(n['vol'])} | "
                  f"{_fmt(n['max_drawdown'])} | {_fmt(n['total_return'])} |",
                  f"| benchmark | {_fmt(b['cagr'])} | {_fmt(b['vol'])} | "
                  f"{_fmt(b['max_drawdown'])} | {_fmt(b['total_return'])} |", "",
                  f"Average one-way turnover per rebalance: {_fmt(s['avg_turnover'])}; "
                  f"average cost per rebalance: {_fmt(s['avg_cost_per_rebalance'])}; "
                  f"share of periods beating the benchmark (net): "
                  f"{_fmt(s['hit_rate_vs_benchmark'])}.", ""]
    spread = spread_table(res)
    if spread.height:
        lines += ["## Quantile returns (mean forward return per quantile; highest = best "
                  "composite)", "", _table(spread, limit=100)]
    if res.ic_status.height:
        lines += ["## Factor information coefficients (primary horizon)", "",
                  "t-statistics use non-overlapping observations. Verdict KEEP needs a "
                  "positive mean IC and t >= the configured threshold; DROP factors are "
                  "refused by production scoring.", "",
                  _table(res.ic_status.sort("t_stat", descending=True, nulls_last=True),
                         limit=100)]
    if res.dq.issues:
        lines += ["## Data-quality notes", "",
                  _table(pl.DataFrame([{"severity": i.severity, "category": i.category,
                                        "message": i.message} for i in res.dq.issues]),
                         limit=100)]
    lines += ["## Limitations", "",
              "- A single current schedule of statutory charges is applied to all dates.",
              "- Market-cap history uses today's share count on adjusted prices.",
              "- Surveillance (ASM/GSM) status is only known from landed snapshots; earlier "
              "dates cannot exclude surveillance names.",
              "- Industry classification before it was first observed uses the earliest "
              "known label.", ""]
    text = assert_no_advice_language("\n".join(lines))
    path = out_dir / "report.md"
    path.write_text(text)
    (out_dir / "summary.json").write_text(json.dumps(s, indent=2, default=str))
    return path


def write_ic_status(res: BacktestResult, path: Path) -> Path:
    """Latest factor verdicts; production scoring refuses factors marked DROP."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = res.ic_status.filter(pl.col("factor") != "composite").to_dicts() \
        if res.ic_status.height else []
    path.write_text(json.dumps({"generated_at": utc_now().isoformat(),
                                "frequency": res.frequency, "factors": rows}, indent=2,
                               default=str))
    return path
