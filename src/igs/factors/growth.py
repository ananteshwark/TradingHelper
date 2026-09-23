"""Growth pillar. Trailing-twelve-month based so every value uses only
quarterly results that were filed by the as-of date."""

from __future__ import annotations

import polars as pl

from igs.factors import base as b
from igs.factors.registry import factor
from igs.pit.view import PitView

# Profit growth from a tiny base (PAT going from 0.1% to 5% of revenue) is arithmetic,
# not growth: EBITDA and PAT growth rates need the base period's margin to be at least
# this share of that period's revenue, else the factor is insufficient_data.
MIN_BASE_MARGIN = 0.02


def _with_base_margin(view: PitView, j: pl.DataFrame, measure: str, lag: int,
                      base_col: str) -> pl.DataFrame:
    if measure == "top_line":
        return j.with_columns(pl.lit(None, dtype=pl.Float64).alias("base_margin"),
                              pl.lit(True).alias("base_ok"))
    rev = b.ttm(view, "top_line", lag).rename({"top_line_ttm": "_rev_base", "ids": "_ids_rev"})
    j = j.join(rev, on="company_id", how="left").with_columns(
        pl.when(pl.col("_rev_base") > 0).then(pl.col(base_col) / pl.col("_rev_base"))
          .alias("base_margin"))
    return j.with_columns((pl.col("base_margin") >= MIN_BASE_MARGIN).fill_null(False)
                          .alias("base_ok")).drop("_rev_base", "_ids_rev")


def _cagr_factor(view: PitView, measure: str, years: int) -> pl.DataFrame:
    now = b.ttm(view, measure, 0)
    then = b.ttm(view, measure, 4 * years).rename({f"{measure}_ttm": "ttm_then",
                                                   "ids": "ids_then"})
    j = _with_base_margin(view, now.join(then, on="company_id"), measure, 4 * years,
                          "ttm_then")
    j = j.with_columns(
        pl.when(pl.col("base_ok"))
          .then(b.cagr(pl.col(f"{measure}_ttm"), pl.col("ttm_then"), years)).alias("v"),
        pl.concat_list("ids", "ids_then").alias("all_ids"))
    return b.finish(j.rename({f"{measure}_ttm": "ttm_now"}), "v",
                    ["ttm_now", "ttm_then", "base_margin"], "all_ids",
                    universe=b.companies(view))


def _yoy(view: PitView, measure: str) -> pl.DataFrame:
    now = b.ttm(view, measure, 0)
    then = b.ttm(view, measure, 4).rename({f"{measure}_ttm": "ttm_prev", "ids": "ids_prev"})
    j = _with_base_margin(view, now.join(then, on="company_id"), measure, 4, "ttm_prev")
    j = j.with_columns(
        pl.when((pl.col("ttm_prev") > 0) & pl.col("base_ok"))
          .then(pl.col(f"{measure}_ttm") / pl.col("ttm_prev") - 1.0).alias("v"),
        pl.concat_list("ids", "ids_prev").alias("all_ids"))
    return b.finish(j.rename({f"{measure}_ttm": "ttm_now"}), "v",
                    ["ttm_now", "ttm_prev", "base_margin"], "all_ids",
                    universe=b.companies(view))


for _m, _label in (("top_line", "revenue"), ("ebitda", "ebitda"), ("pat", "pat")):
    for _y in (3, 5):
        def _make(m=_m, y=_y):
            def fn(view: PitView) -> pl.DataFrame:
                return _cagr_factor(view, m, y)
            return fn
        factor(f"{_label}_cagr_{_y}y", "growth", True,
               f"{_label.upper()} CAGR over {_y} years, TTM vs TTM {4 * _y} quarters earlier; "
               "undefined when either end is not positive"
               + ("" if _m == "top_line" else " or the base margin is below 2% of revenue")
               )(_make())


@factor("revenue_ttm_yoy", "growth", True, "TTM revenue vs TTM a year earlier")
def revenue_ttm_yoy(view: PitView) -> pl.DataFrame:
    return _yoy(view, "top_line")


@factor("pat_ttm_yoy", "growth", True, "TTM profit (owners) vs TTM a year earlier; undefined "
        "when the base is not positive or below 2% of revenue")
def pat_ttm_yoy(view: PitView) -> pl.DataFrame:
    return _yoy(view, "pat")


def _quarterly_yoy(view: PitView) -> pl.DataFrame:
    """Per company-quarter YoY growth of revenue, with the latest quarter index."""
    q = b.quarterly(view).join(b.latest_quarter(view), on="company_id")
    prev = q.select("company_id", (pl.col("qidx") + 4).alias("qidx"),
                    pl.col("top_line").alias("prev"), pl.col("ids_top_line").alias("ids_prev"))
    return (q.join(prev, on=["company_id", "qidx"])
             .with_columns(pl.when(pl.col("prev") > 0)
                           .then(pl.col("top_line") / pl.col("prev") - 1.0).alias("yoy"),
                           (pl.col("last_q") - pl.col("qidx")).alias("lag"),
                           pl.concat_list("ids_top_line", "ids_prev").alias("ids")))


@factor("growth_acceleration_4q", "growth", True,
        "mean YoY revenue growth of the last 4 quarters minus the mean of the 4 before")
def growth_acceleration_4q(view: PitView) -> pl.DataFrame:
    y = _quarterly_yoy(view).filter((pl.col("lag") < 8) & pl.col("yoy").is_not_null())
    g = (y.group_by("company_id")
          .agg(pl.col("yoy").filter(pl.col("lag") < 4).mean().alias("recent"),
               (pl.col("lag") < 4).sum().alias("n_recent"),
               pl.col("yoy").filter(pl.col("lag") >= 4).mean().alias("prior"),
               (pl.col("lag") >= 4).sum().alias("n_prior"),
               b.flat(pl.col("ids")).alias("ids"))
          .filter((pl.col("n_recent") == 4) & (pl.col("n_prior") == 4))
          .with_columns((pl.col("recent") - pl.col("prior")).alias("v")))
    return b.finish(g, "v", ["recent", "prior"], "ids", universe=b.companies(view))


@factor("growth_consistency_12q", "growth", True,
        "number of the last 12 quarters with positive YoY revenue growth (needs all 12)")
def growth_consistency_12q(view: PitView) -> pl.DataFrame:
    y = _quarterly_yoy(view).filter((pl.col("lag") < 12) & pl.col("yoy").is_not_null())
    g = (y.group_by("company_id")
          .agg((pl.col("yoy") > 0).sum().cast(pl.Float64).alias("v"), pl.len().alias("n"),
               b.flat(pl.col("ids")).alias("ids"))
          .filter(pl.col("n") == 12))
    return b.finish(g, "v", ["n"], "ids", universe=b.companies(view))
