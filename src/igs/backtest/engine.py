"""Walk-forward backtest.

At each rebalance date T (end of day, IST):
  * the universe, every factor, the composite, every red flag and caution, the
    robustness gates and the tiers are computed through PitView(dataset, T) by
    `evaluate_date` - exactly the code production scoring runs;
  * with walk-forward selection on, factors are gated by the IC measured only
    on forward returns already realised by T;
  * positions are entered at the next session's close.
Forward returns are the one place future prices are read, explicitly and
outside the point-in-time view: total-return adjusted closes (splits, bonuses,
rights and dividends), exited at the last close on or before the horizon
date. A name that stops trading before the horizon exits at its last close
and is counted as delisted/suspended in the report (survivorship is not
assumed away).

Failure measurement (failures.py) records each name's tier and the path outcome
over the failure horizon, so the report can say how often High conviction names
failed, compared with the universe and with top-ranked names before any check.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import polars as pl

import igs.factors  # noqa: F401  (registers factors)
from igs.backtest import calendar as cal
from igs.backtest import failures as F
from igs.backtest.costs import round_trip_rate
from igs.backtest.metrics import ic_summary, ic_verdicts, spearman_ic
from igs.config import (
    BacktestConfig,
    CostsConfig,
    RedFlagsConfig,
    ScoringConfig,
    UniverseConfig,
    load_red_flags,
)
from igs.dq import DQLog
from igs.normalize.adjust import adjusted_prices, event_factors
from igs.pit.view import PitDataset, PitView
from igs.score.run import composite_ranks, evaluate_date, flag_blockers, rank_history
from igs.score.run import trading_days as _trading_days
from igs.timeutil import end_of_day_ist

ENTRY_WINDOW_DAYS = 5      # must trade within this many days after T to be bought
STALE_EXIT_DAYS = 10       # last trade this long before the horizon = delisted/suspended


@dataclass
class BacktestResult:
    frequency: str
    dates: list[dt.date]
    scores: pl.DataFrame
    factor_z: pl.DataFrame
    forward: pl.DataFrame
    ic: pl.DataFrame
    ic_summary: pl.DataFrame
    ic_status: pl.DataFrame
    quantiles: pl.DataFrame
    periods: pl.DataFrame
    selection: pl.DataFrame
    universe_sizes: pl.DataFrame
    benchmark_name: str
    dq: DQLog = field(default_factory=DQLog)
    tiers: pl.DataFrame = field(default_factory=pl.DataFrame)
    checks: pl.DataFrame = field(default_factory=pl.DataFrame)
    outcomes: pl.DataFrame = field(default_factory=pl.DataFrame)
    failure_tiers: pl.DataFrame = field(default_factory=pl.DataFrame)
    check_effectiveness: pl.DataFrame = field(default_factory=pl.DataFrame)
    sensitivity: pl.DataFrame = field(default_factory=pl.DataFrame)


def trading_days(dataset: PitDataset) -> list[dt.date]:
    return _trading_days(dataset)


def total_return_prices(dataset: PitDataset) -> pl.DataFrame:
    """Adjusted closes for returns, using every corporate action (ratios are unaffected)."""
    px = dataset.tables["prices"].drop("known_at")
    cas = dataset.tables.get("corporate_actions")
    if cas is None or cas.height == 0:
        factors = pl.DataFrame(schema={"security_id": pl.Int64, "ex_date": pl.Date,
                                       "factor": pl.Float64})
    else:
        factors = event_factors(cas.drop("known_at"), px, include_dividends=True)
    last = px["trade_date"].max()
    adj = adjusted_prices(px, factors, last)
    return adj.select("company_id", "security_id", "trade_date", pl.col("adj_close").alias("tr"))


def benchmark_series(dataset: PitDataset, name: str, dq: DQLog) -> tuple[pl.DataFrame, str]:
    idx = dataset.tables.get("index_prices")
    if idx is None or idx.height == 0:
        dq.emit("error", "no_benchmark", "no index prices loaded; excess returns unavailable")
        return pl.DataFrame(schema={"trade_date": pl.Date, "bench": pl.Float64}), "none"
    names = set(idx["index_name"].unique().to_list())
    used = name
    if name not in names:
        fallback = next((n for n in ("Nifty 500", "NIFTY 500") if n in names), None)
        if fallback is None:
            dq.emit("error", "no_benchmark", f"benchmark {name!r} not loaded")
            return pl.DataFrame(schema={"trade_date": pl.Date, "bench": pl.Float64}), "none"
        dq.emit("warn", "benchmark_not_tri",
                f"{name!r} not loaded; using the {fallback!r} PRICE index. Excess returns are "
                "overstated by roughly the index dividend yield.")
        used = fallback
    return (idx.filter(pl.col("index_name") == used)
               .select("trade_date", pl.col("close").alias("bench")).sort("trade_date"), used)


def _liquidity(view: PitView) -> pl.DataFrame:
    """20-session average traded value and 60-session daily volatility per company."""
    from igs.factors.base import primary_prices
    px = primary_prices(view)
    if px.height == 0:
        return pl.DataFrame(schema={"company_id": pl.Int64, "adv": pl.Float64,
                                    "vol": pl.Float64})
    return (px.sort("company_id", "trade_date")
              .with_columns((pl.col("adj_close") / pl.col("adj_close").shift(1).over("company_id"))
                            .log().alias("_r"),
                            (pl.col("close") * pl.col("volume")).alias("_tv"))
              .group_by("company_id")
              .agg(pl.col("_tv").tail(20).mean().alias("adv"),
                   pl.col("_r").tail(60).std().alias("vol")))


def _forward_returns(tr: pl.DataFrame, bench: pl.DataFrame, days: list[dt.date],
                     picks: pl.DataFrame, date: dt.date, horizons: list[int]) -> pl.DataFrame:
    entry_day = cal.next_trading_day(days, date)
    if entry_day is None:
        return pl.DataFrame()
    window = tr.filter((pl.col("trade_date") >= entry_day)
                       & (pl.col("trade_date") <= entry_day + dt.timedelta(days=ENTRY_WINDOW_DAYS)))
    entry = (window.join(picks.select("company_id"), on="company_id").sort("trade_date")
                   .group_by("company_id").agg(pl.col("trade_date").first().alias("entry_date"),
                                               pl.col("tr").first().alias("entry_px")))
    b_entry = bench.filter(pl.col("trade_date") <= entry_day).tail(1)
    out = []
    for h in horizons:
        exit_target = cal.on_or_before(days, cal.add_months(entry_day, h))
        if exit_target is None or exit_target <= entry_day or exit_target > days[-1] or \
                cal.add_months(entry_day, h) > days[-1]:
            continue
        ex = (tr.join(entry.select("company_id"), on="company_id")
                .filter(pl.col("trade_date") <= exit_target).sort("trade_date")
                .group_by("company_id").agg(pl.col("trade_date").last().alias("exit_date"),
                                            pl.col("tr").last().alias("exit_px")))
        b_exit = bench.filter(pl.col("trade_date") <= exit_target).tail(1)
        bret = (b_exit["bench"][0] / b_entry["bench"][0] - 1) if b_entry.height and \
            b_exit.height else None
        out.append(entry.join(ex, on="company_id").with_columns(
            pl.lit(date).alias("date"), pl.lit(h).alias("horizon_m"),
            (pl.col("exit_px") / pl.col("entry_px") - 1).alias("ret"),
            pl.lit(bret, dtype=pl.Float64).alias("bench_ret"),
            ((pl.lit(exit_target) - pl.col("exit_date")).dt.total_days() > STALE_EXIT_DAYS)
            .alias("stopped_trading")))
    return pl.concat(out) if out else pl.DataFrame()


def _walk_forward_dropped(ic_rows: list[pl.DataFrame], date: dt.date, cfg: BacktestConfig,
                          rebalance_months: int) -> set[str]:
    if not cfg.walk_forward_ic_selection or not ic_rows:
        return set()
    h = cfg.ic_gate.primary_horizon_months
    ic = pl.concat(ic_rows).filter(pl.col("horizon_m") == h)
    realised = ic.filter(pl.col("realised_by") <= date)
    if realised.height == 0:
        return set()
    v = ic_verdicts(ic_summary(realised, rebalance_months), h, cfg.ic_gate.min_abs_t,
                    cfg.ic_gate.min_observations)
    return set(v.filter(pl.col("verdict") == "DROP")["factor"].to_list()) - {"composite"}


def run_backtest(dataset: PitDataset, start: dt.date, end: dt.date, frequency: str,
                 bt: BacktestConfig, sc: ScoringConfig, uc: UniverseConfig, cc: CostsConfig,
                 factors: list[str] | None = None, n_quantiles: int | None = None,
                 rf: RedFlagsConfig | None = None) -> BacktestResult:
    dq = DQLog()
    rf = rf or load_red_flags()
    days = trading_days(dataset)
    dates = (cal.monthly(days, start, end) if frequency == "monthly"
             else cal.quarterly(days, start, end, bt.rebalance.quarterly_lag_days))
    rebalance_months = 1 if frequency == "monthly" else 3
    nq = n_quantiles or bt.n_quantiles
    all_enabled = [f for p in sc.pillars.values() for f in p.enabled]
    enabled = factors or all_enabled
    restricted = set(all_enabled) - set(enabled)
    tr = total_return_prices(dataset)
    bench, bench_name = benchmark_series(dataset, bt.benchmark, dq)
    # Ranks at earlier month-ends for the persistence gate: rebalance dates reuse their own
    # ranks; any other date is computed once (with the factor restriction, before
    # walk-forward selection, which only exists for rebalance dates).
    history = rank_history(dataset, sc, uc, restricted, bt.signal_cutoff_time_ist)

    scores, zs, fwd, ic_rows, sel, sizes = [], [], [], [], [], []
    records, checks, outs = [], [], []
    for date in dates:
        wf_dropped = _walk_forward_dropped(ic_rows, date, bt, rebalance_months)
        dropped = restricted | wf_dropped
        ev = evaluate_date(dataset, end_of_day_ist(date, bt.signal_cutoff_time_ist), sc, uc,
                           rf, dropped, history, days)
        view, u, norm, res = ev.view, ev.universe, ev.norm, ev.res
        inc = u.filter(pl.col("included"))
        sizes.append({"date": date, "seen": u.height, "included": inc.height})
        if inc.height < nq:
            dq.emit("warn", "universe_too_small", f"{date}: {inc.height} names < {nq} quantiles")
            continue
        sel.extend({"date": date, "factor": f, "used": f not in wf_dropped} for f in enabled)
        rec = F.tier_records(date, ev.results, ev.flags, flag_blockers(ev.flags),
                             composite_ranks(res), ev.implausible, sc)
        records.append(rec)
        checks.append(ev.flags.select(pl.lit(date).alias("date"), "company_id", "flag",
                                      "status", "severity"))
        o = F.outcomes(tr, bench, days, date, inc, bt.failure)
        if o.height:
            outs.append(o)
        liq = _liquidity(view)
        s = (res.composite.join(inc.select("company_id", "bucket", "industry"), on="company_id")
                          .join(liq, on="company_id", how="left")
                          .with_columns(pl.lit(date).alias("date")))
        scores.append(s)
        z = norm.select("company_id", "factor", "z").with_columns(pl.lit(date).alias("date"))
        zs.append(z)
        f = _forward_returns(tr, bench, days, inc, date, bt.forward_horizons_months)
        if f.height:
            fwd.append(f)
            both = pl.concat([
                z.join(f.select("company_id", "horizon_m", "ret"), on="company_id"),
                s.select("company_id", pl.lit("composite").alias("factor"),
                         pl.col("composite").alias("z"), pl.lit(date).alias("date"))
                 .join(f.select("company_id", "horizon_m", "ret"), on="company_id")
                 .select("company_id", "factor", "z", "date", "horizon_m", "ret")],
                how="vertical_relaxed")
            ic = spearman_ic(both, ["date", "factor", "horizon_m"], "z", "ret",
                             min_n=max(5, nq))
            ic = ic.with_columns(pl.struct("date", "horizon_m").map_elements(
                lambda r: cal.add_months(r["date"], r["horizon_m"]) + dt.timedelta(days=1),
                return_dtype=pl.Date).alias("realised_by"))
            ic_rows.append(ic)

    scores_df = pl.concat(scores) if scores else pl.DataFrame()
    fwd_df = pl.concat(fwd, how="vertical_relaxed") if fwd else pl.DataFrame()
    ic_df = pl.concat(ic_rows) if ic_rows else pl.DataFrame(
        schema={"date": pl.Date, "factor": pl.Utf8, "horizon_m": pl.Int64, "ic": pl.Float64,
                "n": pl.UInt32, "realised_by": pl.Date})
    summary = ic_summary(ic_df, rebalance_months) if ic_df.height else pl.DataFrame()
    status = ic_verdicts(summary, bt.ic_gate.primary_horizon_months, bt.ic_gate.min_abs_t,
                         bt.ic_gate.min_observations) if summary.height else pl.DataFrame()
    quant = _quantiles(scores_df, fwd_df, nq)
    periods = _simulate(scores_df, tr, bench, days, nq, cc, frequency)
    stopped = fwd_df.filter(pl.col("stopped_trading")).height if fwd_df.height else 0
    if stopped:
        dq.emit("info", "exits_before_horizon",
                f"{stopped} forward returns ended early (delisted/suspended); exited at the "
                "last traded close")
    rec_df = pl.concat(records, how="vertical_relaxed") if records else pl.DataFrame()
    chk_df = pl.concat(checks) if checks else pl.DataFrame()
    out_df = pl.concat(outs) if outs else pl.DataFrame(schema=F.OUTCOME_SCHEMA)
    if rec_df.height:
        chk_df = pl.concat([chk_df, F.gate_outcomes(rec_df, sc.robustness)])
        failure_tiers = F.tier_table(rec_df, out_df, sc)
        effect = F.check_effectiveness(chk_df, rec_df, out_df, bt.failure)
        sens = F.sensitivity(rec_df, out_df, sc)
    else:
        failure_tiers = effect = sens = pl.DataFrame()
    if out_df.height == 0:
        dq.emit("warn", "no_failure_outcomes",
                f"no rebalance date has {bt.failure.horizon_months} months of prices after it; "
                "failure rates cannot be measured")
    return BacktestResult(frequency=frequency, dates=dates, scores=scores_df,
                          factor_z=pl.concat(zs) if zs else pl.DataFrame(), forward=fwd_df,
                          ic=ic_df, ic_summary=summary, ic_status=status, quantiles=quant,
                          periods=periods, selection=pl.DataFrame(sel),
                          universe_sizes=pl.DataFrame(sizes), benchmark_name=bench_name, dq=dq,
                          tiers=rec_df, checks=chk_df, outcomes=out_df,
                          failure_tiers=failure_tiers, check_effectiveness=effect,
                          sensitivity=sens)


def _quantiles(scores: pl.DataFrame, fwd: pl.DataFrame, nq: int) -> pl.DataFrame:
    if scores.height == 0 or fwd.height == 0:
        return pl.DataFrame()
    q = scores.drop_nulls("composite").with_columns(
        (pl.col("composite").rank("ordinal").over("date") * nq
         / pl.col("composite").count().over("date")).ceil().cast(pl.Int32).alias("quantile"))
    j = q.join(fwd, on=["date", "company_id"])
    return (j.group_by("date", "horizon_m", "quantile")
             .agg(pl.col("ret").mean().alias("ret"), pl.col("bench_ret").first(),
                  pl.len().alias("n"))
             .sort("date", "horizon_m", "quantile"))


def _simulate(scores: pl.DataFrame, tr: pl.DataFrame, bench: pl.DataFrame,
              days: list[dt.date], nq: int, cc: CostsConfig, frequency: str) -> pl.DataFrame:
    """Hold the top quantile equal-weighted from one rebalance to the next."""
    if scores.height == 0:
        return pl.DataFrame()
    dates = sorted(scores["date"].unique().to_list())
    rows, held = [], set()
    for d0, d1 in zip(dates, dates[1:], strict=False):
        s = scores.filter((pl.col("date") == d0) & pl.col("composite").is_not_null())
        n_top = max(1, s.height // nq)
        top = s.sort("composite", descending=True).head(n_top)
        names = set(top["company_id"].to_list())
        e0, e1 = cal.next_trading_day(days, d0), cal.next_trading_day(days, d1)
        if e0 is None or e1 is None:
            continue
        p0 = tr.filter(pl.col("trade_date") <= e0).sort("trade_date").group_by(
            "company_id").agg(pl.col("tr").last().alias("p0"))
        p1 = tr.filter(pl.col("trade_date") <= e1).sort("trade_date").group_by(
            "company_id").agg(pl.col("tr").last().alias("p1"))
        rets = top.join(p0, on="company_id").join(p1, on="company_id").with_columns(
            (pl.col("p1") / pl.col("p0") - 1).alias("ret"))
        gross = rets["ret"].mean() if rets.height else None
        bought, sold = names - held, held - names
        turnover = (len(bought) + len(sold)) / (2 * max(len(names), 1))
        order_value = cc.capital_inr / max(len(names), 1)

        def one_side(r: dict, ov: float = order_value) -> float:
            return round_trip_rate(cc, ov, r["adv"], r["vol"], r["bucket"]) / 2

        # Liquidity of a sold name is taken from its last scoring date.
        last_seen = (scores.filter(pl.col("date") <= d0).sort("date")
                           .group_by("company_id").agg(pl.all().last()))
        buy_cost = sum(one_side(r) for r in top.filter(pl.col("company_id").is_in(list(bought)))
                       .iter_rows(named=True))
        sell_cost = sum(one_side(r) for r in last_seen.filter(
            pl.col("company_id").is_in(list(sold))).iter_rows(named=True))
        cost = (buy_cost + sell_cost) / max(len(names), 1)
        b0 = bench.filter(pl.col("trade_date") <= e0).tail(1)
        b1 = bench.filter(pl.col("trade_date") <= e1).tail(1)
        bret = b1["bench"][0] / b0["bench"][0] - 1 if b0.height and b1.height else None
        rows.append({"date": d0, "next_date": d1, "n": len(names), "gross": gross,
                     "cost": cost, "net": None if gross is None else gross - cost,
                     "bench": bret, "turnover": turnover})
        held = names
    return pl.DataFrame(rows, schema={"date": pl.Date, "next_date": pl.Date, "n": pl.Int64,
                                      "gross": pl.Float64, "cost": pl.Float64,
                                      "net": pl.Float64, "bench": pl.Float64,
                                      "turnover": pl.Float64})
