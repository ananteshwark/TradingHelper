"""Growth pillar: TTM comparisons with explicitly labelled annual CAGR fallbacks.
All inputs must be known by the as-of date."""

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
    primary = b.finish(j.rename({f"{measure}_ttm": "ttm_now"}), "v",
                    ["ttm_now", "ttm_then", "base_margin"], "all_ids",
                    universe=b.companies(view))
    # Matched fiscal-year endpoints are an explicit fallback, never synthetic quarters.
    annual = b.fy_panel(view).select("company_id", "period_end", "revenue", "pat", "ids")
    if measure == "ebitda":
        annual = annual.join(b.annual(view).select("company_id", "period_end", "ebitda",
                             pl.col("ids").alias("ebitda_ids")),
                             on=["company_id", "period_end"], how="left").with_columns(
                             pl.concat_list("ids", "ebitda_ids").alias("ids"))
    column = "revenue" if measure == "top_line" else measure
    latest = annual.sort("period_end").group_by("company_id").agg(pl.all().last())
    latest = latest.filter((pl.lit(view.as_of.date())-pl.col("period_end"))
                           .dt.total_days().is_between(0, 550))
    old = annual.select("company_id", pl.col("period_end").dt.offset_by(f"{years}y")
                        .alias("period_end"), pl.col(column).alias("annual_then"),
                        pl.col("revenue").alias("revenue_then"),
                        pl.col("ids").alias("old_ids"))
    pair = latest.join(old, on=["company_id", "period_end"]).with_columns(
        (pl.lit(True) if measure == "top_line" else
         (pl.col("revenue_then")>0) &
         (pl.col("annual_then")/pl.col("revenue_then")>=MIN_BASE_MARGIN)).alias("base_ok"))
    pair = pair.with_columns(pl.when(pl.col("base_ok"))
         .then(b.cagr(pl.col(column), pl.col("annual_then"), years)).alias("v"),
         pl.col(column).alias("annual_now"),
         pl.lit("matched_fiscal_years").alias("comparison"),
         pl.concat_list("ids", "old_ids").alias("all_ids"))
    fallback = b.finish(pair,"v",["comparison","period_end","annual_now","annual_then"],
                        "all_ids").filter(pl.col("status")==b.OK)
    fallback = fallback.join(now.join(then,on='company_id').select('company_id'),
                             on="company_id",how="anti")
    return pl.concat([primary.join(fallback.select("company_id"),on="company_id",how="anti"),
                       fallback]).sort("company_id")


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
               "matched fiscal-year endpoints when TTM history is missing; "
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


def short_growth(view: PitView, measure: str, periods: int = 1,
                 margin: bool = False) -> pl.DataFrame:
    """Matched calendar quarters, never adjacent-quarter annualisation or imputation."""
    q = b.quarterly(view).join(b.latest_quarter(view), on="company_id")

    def window(lag: int, prefix: str):
        rows = q.filter((pl.col('qidx') <= pl.col('last_q') - lag)
                        & (pl.col('qidx') > pl.col('last_q') - lag - periods)
                        & pl.col(measure).is_not_null() & pl.col('top_line').is_not_null())
        return rows.group_by('company_id').agg(
            pl.len().alias(prefix+'n'), pl.col(measure).sum().alias(prefix+'value'),
            pl.col('period_end').max().alias(prefix+'end'),
            pl.col('top_line').sum().alias(prefix+'revenue'),
            b.flat(pl.concat_list('ids_'+measure, 'ids_top_line')).alias(prefix+'ids'))

    j = window(0, 'now_').join(window(4, 'prior_'), on='company_id')
    j = j.filter((pl.col('now_n') == periods) & (pl.col('prior_n') == periods))
    valid = (pl.col('prior_value') > 0) & (pl.col('now_value') > 0)
    if measure != 'top_line':
        valid &= ((pl.col('prior_revenue') > 0) & (pl.col('now_revenue') > 0)
                  & (pl.col('prior_value') / pl.col('prior_revenue') >= MIN_BASE_MARGIN))
    value = (pl.col('now_value') / pl.col('now_revenue')
             - pl.col('prior_value') / pl.col('prior_revenue')) if margin else (
                 pl.col('now_value') / pl.col('prior_value') - 1)
    j = j.with_columns(pl.when(valid).then(value).alias('v'),
                       pl.concat_list('now_ids', 'prior_ids').alias('ids'))
    return b.finish(j, 'v', ['now_value', 'prior_value', 'now_revenue', 'prior_revenue',
                            'now_end', 'prior_end'],
                    'ids', universe=b.companies(view),
                    not_applicable=b.financials(view) if margin else None)


for _name, _measure, _periods, _margin in (
    ('revenue_quarter_yoy', 'top_line', 1, False),
    ('pat_quarter_yoy', 'pat', 1, False),
    ('revenue_2q_yoy', 'top_line', 2, False),
    ('pat_2q_yoy', 'pat', 2, False),
    ('opm_quarter_yoy', 'ebitda', 1, True),
):
    def _make_short(measure=_measure, periods=_periods, margin=_margin):
        def fn(view):
            return short_growth(view, measure, periods, margin)
        return fn
    factor(_name, 'growth', True,
           'Matched year-ago quarterly periods; positive base and profit-margin guard; '
           'experimental, tracked without changing composite weights')(_make_short())
