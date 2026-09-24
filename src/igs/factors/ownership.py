"""Ownership pillar, from shareholding pattern filings known at the as-of date.

For each quarter the latest filed version is used (revised SHP filings
supersede earlier ones only from their own filed_at).
"""

from __future__ import annotations

import polars as pl

from igs.factors import base as b
from igs.factors.registry import factor
from igs.pit.view import PitView

MAX_GAP_DAYS = 100   # consecutive filings further apart than this are not "QoQ"


def _panel(view: PitView) -> pl.DataFrame:
    """company_id, period_end, rank (0 = latest) and per-category columns."""
    def build() -> pl.DataFrame:
        if not view.has("shareholding"):
            return pl.DataFrame(schema={"company_id": pl.Int64, "period_end": pl.Date,
                                        "filing_id": pl.Int64, "rank": pl.Int64})
        s = (view.table("shareholding").sort("filed_at")
                 .unique(subset=["company_id", "period_end", "category"], keep="last"))
        wide = s.pivot(on="category", index=["company_id", "period_end"],
                       values=["pct_of_total", "pledged_pct", "holders"],
                       aggregate_function="first")
        fid = s.group_by("company_id", "period_end").agg(pl.col("filing_id").max())
        wide = wide.join(fid, on=["company_id", "period_end"])
        return wide.sort("company_id", "period_end", descending=[False, True]).with_columns(
            pl.int_range(pl.len()).over("company_id").alias("rank"))
    return view.memo("shp_panel", build)


def _col(df: pl.DataFrame, measure: str, cat: str) -> pl.Expr:
    name = f"{measure}_{cat}"
    return pl.col(name) if name in df.columns else pl.lit(None, dtype=pl.Float64)


def _pair(view: PitView, lag: int = 1) -> pl.DataFrame:
    p = _panel(view)
    cur = p.filter(pl.col("rank") == 0)
    prev = p.filter(pl.col("rank") == lag)
    prev = prev.rename({c: f"{c}__prev" for c in prev.columns if c != "company_id"})
    j = cur.join(prev, on="company_id")
    return j.filter((pl.col("period_end") - pl.col("period_end__prev")).dt.total_days()
                    <= MAX_GAP_DAYS * lag)


def _inst(df: pl.DataFrame, measure: str, suffix: str = "") -> pl.Expr:
    f = _col(df, measure, "institutions_foreign" + suffix)
    d = _col(df, measure, "institutions_domestic" + suffix)
    return f + d


@factor("promoter_holding_qoq", "ownership", True,
        "change in promoter holding (percentage points of total shares) vs previous quarter")
def promoter_holding_qoq(view: PitView) -> pl.DataFrame:
    j = _pair(view)
    j = j.with_columns(_col(j, "pct_of_total", "promoter").alias("now"),
                       _col(j, "pct_of_total", "promoter__prev").alias("prev"),
                       pl.concat_list("filing_id", "filing_id__prev").alias("filings"))
    j = j.with_columns((pl.col("now") - pl.col("prev")).alias("v"))
    return b.finish(j, "v", ["now", "prev", "period_end", "filings"], None,
                    universe=b.companies(view))


@factor("pledge_pct", "ownership", False,
        "promoter shares pledged or encumbered, as % of promoter holding (latest filing)")
def pledge_pct(view: PitView) -> pl.DataFrame:
    p = _panel(view).filter(pl.col("rank") == 0)
    # No pledge column for a filed promoter holding means nothing is pledged.
    p = p.with_columns(pl.when(_col(p, "pct_of_total", "promoter").is_not_null())
                       .then(_col(p, "pledged_pct", "promoter").fill_null(0.0)).alias("v"))
    return b.finish(p, "v", ["period_end", "filing_id"], None, universe=b.companies(view))


@factor("pledge_trend", "ownership", False,
        "change in promoter pledge % over the last two quarters")
def pledge_trend(view: PitView) -> pl.DataFrame:
    j = _pair(view, lag=2)
    j = j.with_columns(_col(j, "pledged_pct", "promoter").fill_null(0.0).alias("now"),
                       _col(j, "pledged_pct", "promoter__prev").fill_null(0.0).alias("prev"))
    j = j.with_columns((pl.col("now") - pl.col("prev")).alias("v"))
    return b.finish(j, "v", ["now", "prev", "period_end"], None, universe=b.companies(view))


@factor("fii_dii_holding_change", "ownership", True,
        "change in foreign + domestic institutional holding (pp) vs previous quarter")
def fii_dii_holding_change(view: PitView) -> pl.DataFrame:
    j = _pair(view)
    j = j.with_columns(_inst(j, "pct_of_total").alias("now"),
                       _inst(j, "pct_of_total", "__prev").alias("prev"))
    j = j.with_columns((pl.col("now") - pl.col("prev")).alias("v"))
    return b.finish(j, "v", ["now", "prev", "period_end"], None, universe=b.companies(view))


@factor("institutional_holder_count", "ownership", True,
        "change in the number of foreign + domestic institutional holders vs previous quarter")
def institutional_holder_count(view: PitView) -> pl.DataFrame:
    j = _pair(view)
    j = j.with_columns(_inst(j, "holders").alias("now"), _inst(j, "holders", "__prev")
                       .alias("prev"))
    j = j.with_columns((pl.col("now") - pl.col("prev")).alias("v"))
    return b.finish(j, "v", ["now", "prev", "period_end"], None, universe=b.companies(view))
