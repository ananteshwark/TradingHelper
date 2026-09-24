"""Backtest statistics: information coefficients, quantile spreads, NAV metrics."""

from __future__ import annotations

import math

import polars as pl


def spearman_ic(df: pl.DataFrame, by: list[str], x: str, y: str,
                min_n: int = 10) -> pl.DataFrame:
    """Rank correlation of x and y within each group (dropping nulls)."""
    d = df.drop_nulls([x, y]).with_columns(
        pl.col(x).rank("average").over(by).alias("_rx"),
        pl.col(y).rank("average").over(by).alias("_ry"))
    # A factor that is constant across names has no defined IC (NaN): dropped, not scored.
    return (d.group_by(by).agg(pl.corr("_rx", "_ry").alias("ic"), pl.len().alias("n"))
             .filter((pl.col("n") >= min_n) & pl.col("ic").is_not_null()
                     & pl.col("ic").is_not_nan()).sort(by))


def non_overlapping(dates: list, step: int) -> list:
    """Every step-th date, so that holding periods of `step` rebalances do not overlap."""
    return dates[::max(step, 1)]


def ic_summary(ic: pl.DataFrame, rebalance_months: int) -> pl.DataFrame:
    """Per factor and horizon: mean IC, IC IR and t-stat on non-overlapping observations
    (every horizon/rebalance_months-th date), plus hit rate on all observations."""
    rows = []
    for (factor, h), g in ic.group_by("factor", "horizon_m"):
        g = g.sort("date")
        dates = g["date"].to_list()
        k = max(1, h // rebalance_months)
        sub = g.filter(pl.col("date").is_in(non_overlapping(dates, k)))
        mean = sub["ic"].mean()
        sd = sub["ic"].std()
        n = sub.height
        if n < 2 or mean is None or sd is None:
            t = None
        elif sd > 0:
            t = mean / sd * math.sqrt(n)
        else:   # identical ICs: significance is unbounded in the direction of the mean
            t = math.copysign(math.inf, mean) if mean else 0.0
        rows.append({"factor": factor, "horizon_m": h, "mean_ic": mean,
                     "ic_ir": mean / sd if sd else None, "t_stat": t, "n_obs": n,
                     "hit_rate": (g["ic"] > 0).mean(), "n_dates": g.height})
    return pl.DataFrame(rows, schema={"factor": pl.Utf8, "horizon_m": pl.Int64,
                                      "mean_ic": pl.Float64, "ic_ir": pl.Float64,
                                      "t_stat": pl.Float64, "n_obs": pl.Int64,
                                      "hit_rate": pl.Float64, "n_dates": pl.Int64}
                        ).sort("factor", "horizon_m")


def ic_verdicts(summary: pl.DataFrame, horizon: int, min_abs_t: float,
                min_obs: int) -> pl.DataFrame:
    """KEEP only with the expected (positive, direction-adjusted) sign and t >= min_abs_t."""
    s = summary.filter(pl.col("horizon_m") == horizon)
    # Polars orders NaN above every number, so finiteness is checked explicitly.
    finite = pl.col("mean_ic").is_not_null() & pl.col("mean_ic").is_finite()
    return s.with_columns(
        pl.when(pl.col("n_obs") < min_obs).then(pl.lit("UNTESTED"))
          .when(finite & (pl.col("mean_ic") > 0) & pl.col("t_stat").is_not_null()
                & ~pl.col("t_stat").is_nan() & (pl.col("t_stat") >= min_abs_t))
          .then(pl.lit("KEEP"))
          .otherwise(pl.lit("DROP")).alias("verdict"))


def nav_stats(returns: list[float], periods_per_year: float) -> dict[str, float | None]:
    if not returns:
        return {"cagr": None, "vol": None, "sharpe": None, "max_drawdown": None,
                "total_return": None}
    nav, peak, mdd = 1.0, 1.0, 0.0
    for r in returns:
        nav *= 1 + r
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1)
    years = len(returns) / periods_per_year
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) \
        if len(returns) > 1 else None
    vol = sd * math.sqrt(periods_per_year) if sd else None
    cagr = nav ** (1 / years) - 1 if years > 0 and nav > 0 else None
    return {"cagr": cagr, "vol": vol,
            "sharpe": mean * periods_per_year / vol if vol else None,
            "max_drawdown": mdd, "total_return": nav - 1}
