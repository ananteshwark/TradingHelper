"""Is a top rank robust, or an accident of this month's weights and data?

High conviction should survive reasonable disagreement about how the score is
built. Four gates, each reported with its number:

  weight stability  the pillar weights are redrawn many times around the agreed
                    ones (Dirichlet draws, fixed seed, so every run is
                    reproducible); the share of draws in which the stock stays in
                    the High conviction band
  persistence       whether the stock was in the top persistence_top_pct at each
                    of the previous month-ends (ranks from exactly the same
                    point-in-time code, run as of those dates)
  breadth           how many pillars score above zero, and whether any pillar is
                    deeply negative (great growth with poor quality is fragile)
  concentration     the largest share of the positive contributions coming from
                    one factor (a rank carried by one number is one data error away
                    from being wrong)
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Callable

import polars as pl

from igs.config import Robustness

HISTORY_SCHEMA = {"company_id": pl.Int64, "rank_pct": pl.Float64}


def dirichlet_draws(weights: dict[str, float], n: int, concentration: float,
                    seed: int) -> pl.DataFrame:
    """n weight vectors ~ Dirichlet(concentration * w); pure Python, seeded."""
    rng = random.Random(seed)
    names = [k for k, w in weights.items() if w > 0]
    rows = []
    for d in range(n):
        g = [rng.gammavariate(concentration * weights[k], 1.0) for k in names]
        total = sum(g)
        rows.extend({"draw": d, "pillar": k, "w": x / total} for k, x in zip(names, g,
                                                                            strict=True))
    return pl.DataFrame(rows, schema={"draw": pl.Int32, "pillar": pl.Utf8, "w": pl.Float64})


def weight_stability(pillars: pl.DataFrame, eligible: list[int], weights: dict[str, float],
                     band_pct: float, cfg: Robustness) -> pl.DataFrame:
    """Share of weight draws in which each eligible company ranks within the top band_pct.

    pillars: company_id, pillar, score (null when the pillar has too little coverage).
    Missing pillars are handled as in the composite: weights renormalised over the
    pillars the company has.
    """
    out_schema = {"company_id": pl.Int64, "weight_stability": pl.Float64}
    p = pillars.filter(pl.col("company_id").is_in(eligible) & pl.col("score").is_not_null())
    if p.height == 0:
        return pl.DataFrame(schema=out_schema)
    draws = dirichlet_draws(weights, cfg.weight_draws, cfg.weight_concentration, cfg.seed)
    j = p.join(draws, on="pillar")
    comp = (j.group_by("draw", "company_id")
             .agg(((pl.col("w") * pl.col("score")).sum() / pl.col("w").sum()).alias("c")))
    n_elig = comp.group_by("draw").agg(pl.len().alias("n"))
    comp = comp.join(n_elig, on="draw").with_columns(
        (pl.col("c").rank("ordinal", descending=True).over("draw") / pl.col("n"))
        .alias("rp"))
    return (comp.group_by("company_id")
                .agg((pl.col("rp") <= band_pct / 100).mean().alias("weight_stability"))
                .cast(out_schema))


def persistence(history: dict[dt.date, pl.DataFrame], dates: list[dt.date],
                top_pct: float) -> pl.DataFrame:
    """For each company: at how many of `dates` it ranked within top_pct, of how many
    dates a ranking existed."""
    frames = []
    for d in dates:
        h = history.get(d)
        if h is None or h.height == 0:
            continue
        frames.append(h.select("company_id", (pl.col("rank_pct") <= top_pct / 100)
                               .alias("hit")))
    schema = {"company_id": pl.Int64, "persist_hits": pl.Int64, "persist_dates": pl.Int64}
    if not frames:
        return pl.DataFrame(schema=schema)
    n_dates = len(frames)
    return (pl.concat(frames).group_by("company_id")
              .agg(pl.col("hit").sum().cast(pl.Int64).alias("persist_hits"))
              .with_columns(pl.lit(n_dates, dtype=pl.Int64).alias("persist_dates"))
              .cast(schema))


def breadth(pillars: pl.DataFrame) -> pl.DataFrame:
    s = pillars.filter(pl.col("score").is_not_null())
    return (s.group_by("company_id")
             .agg((pl.col("score") > 0).sum().cast(pl.Int64).alias("positive_pillars"),
                  pl.len().cast(pl.Int64).alias("scored_pillars"),
                  pl.col("score").min().alias("weakest_pillar_score"),
                  pl.col("pillar").sort_by("score").first().alias("weakest_pillar")))


def concentration(factors: pl.DataFrame) -> pl.DataFrame:
    pos = factors.filter(pl.col("contribution") > 0)
    return (pos.group_by("company_id")
               .agg((pl.col("contribution").max() / pl.col("contribution").sum())
                    .alias("top_factor_share"),
                    pl.col("factor").sort_by("contribution").last().alias("top_factor")))


def prior_month_ends(days: list[dt.date], as_of_date: dt.date, k: int) -> list[dt.date]:
    """Last trading day of each of the k calendar months before as_of's month."""
    out = []
    y, m = as_of_date.year, as_of_date.month
    for _ in range(k):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        in_month = [d for d in days if d.year == y and d.month == m]
        if in_month:
            out.append(in_month[-1])
    return out


def evaluate(pillars: pl.DataFrame, factors: pl.DataFrame, eligible: list[int],
             weights: dict[str, float], band_pct: float, cfg: Robustness,
             history: dict[dt.date, pl.DataFrame], history_dates: list[dt.date]
             ) -> pl.DataFrame:
    """All four measures for every eligible company (one row each)."""
    base = pl.DataFrame({"company_id": eligible}, schema={"company_id": pl.Int64})
    return (base.join(weight_stability(pillars, eligible, weights, band_pct, cfg),
                      on="company_id", how="left")
                .join(persistence(history, history_dates, cfg.persistence_top_pct),
                      on="company_id", how="left")
                .join(breadth(pillars), on="company_id", how="left")
                .join(concentration(factors), on="company_id", how="left")
                .with_columns(pl.lit(len(history_dates), dtype=pl.Int64)
                              .alias("persist_required_dates")))


def blockers(rob: pl.DataFrame, cfg: Robustness) -> pl.DataFrame:
    """One row per (company, reason) for every gate a company fails."""
    if not cfg.enabled or rob.height == 0:
        return pl.DataFrame(schema={"company_id": pl.Int64, "reason": pl.Utf8})
    rules: list[tuple[pl.Expr, pl.Expr]] = [
        (pl.col("weight_stability").fill_null(0) < cfg.min_weight_stability,
         pl.format("rank not robust to weights: in the band in {} of weight variations "
                   "(needs {})", _pct(pl.col("weight_stability").fill_null(0)),
                   pl.lit(f"{cfg.min_weight_stability:.0%}"))),
        (pl.col("persist_hits").fill_null(0) < cfg.min_persistence,
         pl.format("new to the top: in the top {} at {} of the previous {} month-ends "
                   "(needs {})", pl.lit(f"{cfg.persistence_top_pct:g}%"),
                   pl.col("persist_hits").fill_null(0), pl.col("persist_required_dates"),
                   pl.lit(cfg.min_persistence))),
        (pl.col("positive_pillars").fill_null(0) < cfg.min_positive_pillars,
         pl.format("narrow strength: {} of {} pillars above zero (needs {})",
                   pl.col("positive_pillars").fill_null(0), pl.col("scored_pillars")
                   .fill_null(0), pl.lit(cfg.min_positive_pillars))),
        (pl.col("weakest_pillar_score") < cfg.min_pillar_score,
         pl.format("weak {} pillar: {} (floor {})", pl.col("weakest_pillar"),
                   pl.col("weakest_pillar_score").round(2), pl.lit(cfg.min_pillar_score))),
        (pl.col("top_factor_share") > cfg.max_factor_share,
         pl.format("one factor carries the score: {} gives {} of the positive contribution "
                   "(limit {})", pl.col("top_factor"), _pct(pl.col("top_factor_share")),
                   pl.lit(f"{cfg.max_factor_share:.0%}"))),
    ]
    frames = [rob.filter(cond.fill_null(False)).select("company_id", msg.alias("reason"))
              for cond, msg in rules]
    return pl.concat(frames)


def _pct(e: pl.Expr) -> pl.Expr:
    return (e * 100).round(0).cast(pl.Int64).cast(pl.Utf8) + pl.lit("%")


RankFn = Callable[[dt.date], pl.DataFrame]


class RankHistory:
    """Composite ranks by date, computed on demand and cached (the backtest reuses the
    ranks of earlier rebalances; production computes the prior month-ends once)."""

    def __init__(self, rank_at: RankFn) -> None:
        self._rank_at = rank_at
        self.ranks: dict[dt.date, pl.DataFrame] = {}

    def put(self, d: dt.date, ranks: pl.DataFrame) -> None:
        self.ranks[d] = ranks.select(list(HISTORY_SCHEMA)).cast(HISTORY_SCHEMA)

    def ensure(self, dates: list[dt.date]) -> dict[dt.date, pl.DataFrame]:
        for d in dates:
            if d not in self.ranks:
                self.put(d, self._rank_at(d))
        return self.ranks
