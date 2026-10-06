"""How often do screened names fail, and do the safeguards reduce it?

A failure is defined in backtest.yaml (`failure`): over the horizon after the
entry close, the total return falls to -max_loss_pct or worse, OR the price falls
max_drawdown_pct from its running high after entry, OR the stock stops trading,
OR (optionally) it trails the benchmark by max_underperformance_pp. Measured on
total-return prices (splits, bonuses, rights and dividends), path by path.

Three questions, each answered with counts and a 95% Wilson interval, never a
bare percentage:

  1. tier failure rates: High conviction vs Watchlist vs the rest, against the
     whole universe and against "top decile by score alone" (no checks), which is
     what the safeguards have to beat;
  2. check effectiveness: among top-ranked names (by score before any check),
     did names that tripped a check fail more often than names that did not?
     Two-proportion z; SUPPORTED / NO EVIDENCE / CONTRADICTED / INSUFFICIENT DATA;
  3. threshold sensitivity: the High conviction rule under other robustness
     thresholds, chosen on the first half of the dates and confirmed on the
     second, so a threshold is not picked on the same data that judges it.

Nothing here can make failures impossible; it measures how rare they were in the
past under exactly the production rules, and how uncertain that estimate is.
"""

from __future__ import annotations

import datetime as dt
import itertools
import math

import polars as pl

from igs.backtest import calendar as cal
from igs.config import FailureSpec, Robustness, ScoringConfig

ENTRY_WINDOW_DAYS = 5
STALE_EXIT_DAYS = 10
HC, WATCH, NOT_SHORTLISTED, REJECTED = ("High conviction", "Watchlist", "Not shortlisted",
                                        "Rejected")

OUTCOME_SCHEMA = {"date": pl.Date, "company_id": pl.Int64, "entry_date": pl.Date,
                  "exit_date": pl.Date, "ret": pl.Float64, "max_drawdown": pl.Float64,
                  "stopped_trading": pl.Boolean, "bench_ret": pl.Float64,
                  "excess": pl.Float64, "failed": pl.Boolean, "fail_reason": pl.Utf8}


# --------------------------------------------------------------------------- statistics


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """95% Wilson score interval for k failures in n."""
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (0.0 if k == 0 else max(0.0, c - h)), (1.0 if k == n else min(1.0, c + h))


def two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> float | None:
    """z for p1 > p2 with the pooled standard error."""
    if n1 == 0 or n2 == 0:
        return None
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0
    return (k1 / n1 - k2 / n2) / se


# --------------------------------------------------------------------------- outcomes


def outcomes(tr: pl.DataFrame, bench: pl.DataFrame, days: list[dt.date], date: dt.date,
             companies: pl.DataFrame, spec: FailureSpec) -> pl.DataFrame:
    """Path outcome over spec.horizon_months for each company, entered at the first close
    after `date` (within ENTRY_WINDOW_DAYS). Empty when the horizon runs past the data."""
    entry_day = cal.next_trading_day(days, date)
    if entry_day is None:
        return pl.DataFrame(schema=OUTCOME_SCHEMA)
    target = cal.add_months(entry_day, spec.horizon_months)
    exit_target = cal.on_or_before(days, target)
    if exit_target is None or target > days[-1] or exit_target <= entry_day:
        return pl.DataFrame(schema=OUTCOME_SCHEMA)
    path = (tr.join(companies.select("company_id"), on="company_id")
              .filter(pl.col("trade_date").is_between(entry_day, exit_target))
              .sort("company_id", "trade_date"))
    g = (path.group_by("company_id", maintain_order=True)
             .agg(pl.col("trade_date").first().alias("entry_date"),
                  pl.col("trade_date").last().alias("exit_date"),
                  pl.col("tr").first().alias("p0"), pl.col("tr").last().alias("p1"),
                  ((pl.col("tr") / pl.col("tr").cum_max()).min() - 1).alias("max_drawdown"))
             .filter(pl.col("entry_date")
                     <= entry_day + dt.timedelta(days=ENTRY_WINDOW_DAYS)))
    b0 = bench.filter(pl.col("trade_date") <= entry_day).tail(1)
    b1 = bench.filter(pl.col("trade_date") <= exit_target).tail(1)
    bret = b1["bench"][0] / b0["bench"][0] - 1 if b0.height and b1.height else None
    g = g.with_columns(
        pl.lit(date).alias("date"), (pl.col("p1") / pl.col("p0") - 1).alias("ret"),
        ((pl.lit(exit_target) - pl.col("exit_date")).dt.total_days() > STALE_EXIT_DAYS)
        .alias("stopped_trading"), pl.lit(bret, dtype=pl.Float64).alias("bench_ret"))
    g = g.with_columns((pl.col("ret") - pl.col("bench_ret")).alias("excess"))
    conds = [(pl.col("ret") <= -spec.max_loss_pct / 100, "loss"),
             (pl.col("max_drawdown") <= -spec.max_drawdown_pct / 100, "drawdown")]
    if spec.count_stopped_trading:
        conds.append((pl.col("stopped_trading"), "stopped trading"))
    if spec.max_underperformance_pp is not None:
        conds.append(((pl.col("excess") <= -spec.max_underperformance_pp / 100)
                      .fill_null(False), "underperformance"))
    reason = pl.when(conds[0][0]).then(pl.lit(conds[0][1]))
    for c, name in conds[1:]:
        reason = reason.when(c).then(pl.lit(name))
    g = g.with_columns(reason.otherwise(None).alias("fail_reason"))
    g = g.with_columns(pl.col("fail_reason").is_not_null().alias("failed"))
    return g.select(list(OUTCOME_SCHEMA)).cast(OUTCOME_SCHEMA)


# --------------------------------------------------------------------------- per-date records


def tier_records(date: dt.date, results: pl.DataFrame, flags: pl.DataFrame,
                 blockers_from_flags: pl.DataFrame, raw_ranks: pl.DataFrame,
                 implausible: pl.DataFrame, sc: ScoringConfig) -> pl.DataFrame:
    """One row per included company with its tier and every input of the High conviction
    rule as a separate column, so the rule can be re-evaluated under other thresholds."""
    t, rb = sc.tiers, sc.robustness
    rejected = flags.filter((pl.col("status") == "tripped") & (pl.col("severity") == "reject")
                            ).select("company_id").unique().with_columns(
                                pl.lit(True).alias("reject"))
    n_block = blockers_from_flags.group_by("company_id").agg(pl.len().alias("n_check_blocks"))
    imp = implausible.select("company_id").unique().with_columns(
        pl.lit(True).alias("implausible"))

    def col(name: str, dtype: pl.DataType) -> pl.Expr:
        return pl.col(name) if name in results.columns else pl.lit(None, dtype=dtype)
    r = results.select(
        "company_id", "tier", "composite", "coverage", "rank_pct",
        col("weight_stability", pl.Float64).alias("weight_stability"),
        col("persist_hits", pl.Int64).alias("persist_hits"),
        col("positive_pillars", pl.Int64).alias("positive_pillars"),
        col("weakest_pillar_score", pl.Float64).alias("weakest_pillar_score"),
        col("top_factor_share", pl.Float64).alias("top_factor_share"), "hc_blockers")
    r = (r.join(raw_ranks.rename({"rank_pct": "raw_rank_pct"}), on="company_id", how="left")
          .join(rejected, on="company_id", how="left")
          .join(n_block, on="company_id", how="left")
          .join(imp, on="company_id", how="left"))
    on = pl.lit(rb.enabled)
    return r.with_columns(
        pl.lit(date).alias("date"),
        pl.col("reject").fill_null(False), pl.col("implausible").fill_null(False),
        pl.col("n_check_blocks").fill_null(0),
        (pl.col("rank_pct") <= t.high_conviction_top_pct / 100).fill_null(False)
        .alias("in_band"),
        (pl.col("coverage") >= t.high_conviction_min_coverage).fill_null(False)
        .alias("coverage_ok"),
        (~on | (pl.col("positive_pillars").fill_null(0) >= rb.min_positive_pillars))
        .alias("breadth_ok"),
        (~on | ~(pl.col("weakest_pillar_score") < rb.min_pillar_score).fill_null(False))
        .alias("floor_ok"),
        (~on | ~(pl.col("top_factor_share") > rb.max_factor_share).fill_null(False))
        .alias("concentration_ok"))


def hc_mask(min_weight_stability: float, min_persistence: int, apply_checks: bool,
            robustness_on: bool = True) -> pl.Expr:
    """The High conviction rule over tier_records columns."""
    m = (pl.col("in_band") & ~pl.col("reject") & pl.col("composite").is_not_null()
         & pl.col("coverage_ok") & ~pl.col("implausible"))
    if robustness_on:
        m = (m & pl.col("breadth_ok") & pl.col("floor_ok") & pl.col("concentration_ok")
             & (pl.col("weight_stability").fill_null(0) >= min_weight_stability)
             & (pl.col("persist_hits").fill_null(0) >= min_persistence))
    if apply_checks:
        m = m & (pl.col("n_check_blocks") == 0)
    return m


def gate_outcomes(records: pl.DataFrame, rb: Robustness) -> pl.DataFrame:
    """Robustness gates and plausibility as pseudo-checks (long form, like flags)."""
    if records.height == 0:
        return pl.DataFrame(schema={"date": pl.Date, "company_id": pl.Int64, "flag": pl.Utf8,
                                    "status": pl.Utf8, "severity": pl.Utf8})
    elig = ~pl.col("reject") & pl.col("composite").is_not_null()
    gates = {
        "gate: weight stability": pl.col("weight_stability").fill_null(0)
        < rb.min_weight_stability,
        "gate: persistence": pl.col("persist_hits").fill_null(0) < rb.min_persistence,
        "gate: breadth": ~pl.col("breadth_ok"),
        "gate: pillar floor": ~pl.col("floor_ok"),
        "gate: concentration": ~pl.col("concentration_ok"),
        "gate: implausible value": pl.col("implausible"),
    }
    frames = [records.select("date", "company_id", pl.lit(name).alias("flag"),
                             pl.when(~elig).then(pl.lit("not_applicable"))
                               .when(cond).then(pl.lit("tripped")).otherwise(pl.lit("clear"))
                               .alias("status"), pl.lit("gate").alias("severity"))
              for name, cond in gates.items()]
    return pl.concat(frames)


# --------------------------------------------------------------------------- tables


def _summary(df: pl.DataFrame, label: str, n_dates: int) -> dict:
    n, k = df.height, int(df["failed"].sum()) if df.height else 0
    lo, hi = wilson(k, n)
    return {"group": label, "name_dates": n, "failures": k,
            "failure_rate": k / n if n else None, "ci95_low": lo, "ci95_high": hi,
            "mean_return": df["ret"].mean() if n else None,
            "median_return": df["ret"].median() if n else None,
            "mean_excess": df["excess"].mean() if n else None,
            "names_per_date": n / n_dates if n_dates else None}


def tier_table(records: pl.DataFrame, outs: pl.DataFrame, sc: ScoringConfig) -> pl.DataFrame:
    j = records.join(outs, on=["date", "company_id"])
    n_dates = j["date"].n_unique() if j.height else 0
    top = sc.tiers.high_conviction_top_pct
    rows = [_summary(j.filter(pl.col("tier") == tier), tier, n_dates)
            for tier in (HC, WATCH, NOT_SHORTLISTED, REJECTED)]
    rows.append(_summary(j.filter(pl.col("raw_rank_pct") <= top / 100),
                         f"Top {top:g}% by score alone (no checks)", n_dates))
    rows.append(_summary(j, "Whole universe", n_dates))
    return pl.DataFrame(rows, schema={"group": pl.Utf8, "name_dates": pl.Int64,
                                      "failures": pl.Int64, "failure_rate": pl.Float64,
                                      "ci95_low": pl.Float64, "ci95_high": pl.Float64,
                                      "mean_return": pl.Float64, "median_return": pl.Float64,
                                      "mean_excess": pl.Float64, "names_per_date": pl.Float64})


def check_effectiveness(checks: pl.DataFrame, records: pl.DataFrame, outs: pl.DataFrame,
                        spec: FailureSpec) -> pl.DataFrame:
    """checks: date, company_id, flag, status, severity (red flags, cautions and gates)."""
    pop = records.filter(pl.col("raw_rank_pct") <= spec.effectiveness_top_pct / 100)
    j = (checks.join(pop.select("date", "company_id"), on=["date", "company_id"])
               .join(outs.select("date", "company_id", "failed"), on=["date", "company_id"]))
    rows = []
    for (flag, severity), g in j.group_by("flag", "severity", maintain_order=True):
        t = g.filter(pl.col("status") == "tripped")
        c = g.filter(pl.col("status") == "clear")
        nt, kt = t.height, int(t["failed"].sum()) if t.height else 0
        nc, kc = c.height, int(c["failed"].sum()) if c.height else 0
        z = two_proportion_z(kt, nt, kc, nc)
        rt, rc = (kt / nt if nt else None), (kc / nc if nc else None)
        if nt < spec.min_tripped_for_verdict or nc == 0:
            verdict = "INSUFFICIENT DATA"
        elif z is not None and z >= 1.645:
            verdict = "SUPPORTED"
        elif z is not None and z <= -1.645:
            verdict = "CONTRADICTED"
        else:
            verdict = "NO EVIDENCE"
        rows.append({"check": flag, "severity": severity, "tripped": nt, "tripped_failed": kt,
                     "tripped_rate": rt, "clear": nc, "clear_failed": kc, "clear_rate": rc,
                     "unavailable": g.filter(pl.col("status") == "data_unavailable").height,
                     "lift": rt / rc if rt is not None and rc else None, "z": z,
                     "verdict": verdict})
    schema = {"check": pl.Utf8, "severity": pl.Utf8, "tripped": pl.Int64,
              "tripped_failed": pl.Int64, "tripped_rate": pl.Float64, "clear": pl.Int64,
              "clear_failed": pl.Int64, "clear_rate": pl.Float64, "unavailable": pl.Int64,
              "lift": pl.Float64, "z": pl.Float64, "verdict": pl.Utf8}
    return pl.DataFrame(rows, schema=schema).sort("z", descending=True, nulls_last=True)


def sensitivity(records: pl.DataFrame, outs: pl.DataFrame, sc: ScoringConfig) -> pl.DataFrame:
    """The High conviction rule under a grid of robustness thresholds and with/without
    cautions, on the first half of the dates (choose) and the second (confirm)."""
    j = records.join(outs, on=["date", "company_id"])
    schema = {"min_weight_stability": pl.Float64, "min_persistence": pl.Int64,
              "cautions_applied": pl.Boolean, "configured": pl.Boolean,
              "choose_name_dates": pl.Int64, "choose_failure_rate": pl.Float64,
              "choose_mean_excess": pl.Float64, "confirm_name_dates": pl.Int64,
              "confirm_failure_rate": pl.Float64, "confirm_ci95_high": pl.Float64,
              "confirm_mean_excess": pl.Float64}
    if j.height == 0:
        return pl.DataFrame(schema=schema)
    dates = sorted(j["date"].unique().to_list())
    first = set(dates[: len(dates) // 2])
    rb = sc.robustness
    rows = []
    grid = itertools.product(sorted({0.0, 0.4, 0.6, 0.8, rb.min_weight_stability}),
                             range(rb.persistence_months + 1), (True, False))
    for ws, mp, checks_on in grid:
        sel = j.filter(hc_mask(ws, mp, checks_on, rb.enabled))
        a = sel.filter(pl.col("date").is_in(list(first)))
        b = sel.filter(~pl.col("date").is_in(list(first)))
        kb = int(b["failed"].sum()) if b.height else 0
        rows.append({"min_weight_stability": ws, "min_persistence": mp,
                     "cautions_applied": checks_on,
                     "configured": ws == rb.min_weight_stability and mp == rb.min_persistence
                     and checks_on,
                     "choose_name_dates": a.height,
                     "choose_failure_rate": a["failed"].mean() if a.height else None,
                     "choose_mean_excess": a["excess"].mean() if a.height else None,
                     "confirm_name_dates": b.height,
                     "confirm_failure_rate": kb / b.height if b.height else None,
                     "confirm_ci95_high": wilson(kb, b.height)[1],
                     "confirm_mean_excess": b["excess"].mean() if b.height else None})
    return pl.DataFrame(rows, schema=schema)
