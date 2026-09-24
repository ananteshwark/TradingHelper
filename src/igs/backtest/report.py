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
    tiers = res.tiers.with_columns(pl.col("hc_blockers").list.join(" | ")) \
        if res.tiers.height else res.tiers
    for name, df in (("ic_by_date", res.ic), ("ic_summary", res.ic_summary),
                     ("ic_status", res.ic_status), ("quantiles", res.quantiles),
                     ("periods", res.periods), ("factor_selection", res.selection),
                     ("universe_sizes", res.universe_sizes), ("tiers_by_date", tiers),
                     ("outcomes", res.outcomes), ("failure_by_tier", res.failure_tiers),
                     ("check_effectiveness", res.check_effectiveness),
                     ("threshold_sensitivity", res.sensitivity)):
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
    lines += failure_sections(res)
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
    path.write_text(text, encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(s, indent=2, default=str),
                                          encoding="utf-8")
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


def _pct_cols(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    return df.with_columns([pl.col(c).map_elements(lambda x: _fmt(x), return_dtype=pl.Utf8)
                            .alias(c) for c in cols if c in df.columns])


def failure_sections(res: BacktestResult) -> list[str]:
    """Failure rates by tier, check effectiveness and threshold sensitivity."""
    out: list[str] = []
    ft = res.failure_tiers
    if ft is None or ft.height == 0 or ft["name_dates"].sum() == 0:
        return ["## Failure rates", "", "No rebalance date has a full failure horizon of prices "
                "after it, so failure rates cannot be measured for this period.", ""]
    out += ["## Failure rates by tier", "",
            "A name-date counts as a failure if, over the failure horizon after the entry "
            "close, its total return reached the loss limit, it fell by the drawdown limit "
            "from its running high, or it stopped trading (see backtest.yaml `failure`). "
            "The 95% interval is Wilson's; with few High conviction name-dates it is wide, "
            "and the upper end is the honest reading.", "",
            _table(_pct_cols(ft, ["failure_rate", "ci95_low", "ci95_high", "mean_return",
                                  "median_return", "mean_excess"])
                   .with_columns(pl.col("names_per_date").round(1)), limit=20), ""]
    hc = ft.filter(pl.col("group") == "High conviction").row(0, named=True)
    raw = ft.filter(pl.col("group").str.starts_with("Top ")).row(0, named=True)
    if hc["name_dates"] and raw["name_dates"]:
        better = hc["ci95_high"] is not None and raw["failure_rate"] is not None and \
            hc["ci95_high"] < raw["failure_rate"]
        out += [f"High conviction failure rate {_fmt(hc['failure_rate'])} "
                f"(95% interval {_fmt(hc['ci95_low'])} to {_fmt(hc['ci95_high'])}) vs "
                f"{_fmt(raw['failure_rate'])} for the top of the ranking before any check. "
                + ("The safeguards reduced failures by more than the uncertainty."
                   if better else
                   "The difference is within the uncertainty: this backtest does not show the "
                   "safeguards reducing failures."), ""]
    ce = res.check_effectiveness
    if ce is not None and ce.height:
        out += ["## Do the checks earn their place?", "",
                "Among names in the top of the ranking before any check, the failure rate of "
                "names that tripped each check vs names that were clear. SUPPORTED: tripped "
                "names failed more often (one-sided z >= 1.645). CONTRADICTED: they failed "
                "less often - the check removes names that did better. NO EVIDENCE / "
                "INSUFFICIENT DATA: keep or drop it on judgement, not on this table.", "",
                _table(_pct_cols(ce, ["tripped_rate", "clear_rate"])
                       .with_columns(pl.col("lift").round(2), pl.col("z").round(2)),
                       limit=100), ""]
    sens = res.sensitivity
    if sens is not None and sens.height:
        out += ["## Threshold sensitivity (choose on the first half, confirm on the second)",
                "", "Each row is the High conviction rule with different robustness "
                "thresholds, with or without the cautions. Pick thresholds by the first-half "
                "columns only; the second half shows whether that choice held up on dates it "
                "was not chosen on. The configured row is marked.", "",
                _table(_pct_cols(sens, ["choose_failure_rate", "choose_mean_excess",
                                        "confirm_failure_rate", "confirm_ci95_high",
                                        "confirm_mean_excess"]), limit=100), ""]
    return out
