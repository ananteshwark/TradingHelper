"""Momentum pillar, from corporate-action-adjusted prices up to the as-of date."""

from __future__ import annotations

import datetime as dt

import polars as pl

from igs.factors import base as b
from igs.factors.registry import factor
from igs.pit.view import PitView

MAX_STALENESS_DAYS = 10   # last trade older than this -> insufficient data
BENCHMARK = "Nifty 500"


def _recent(view: PitView) -> pl.DataFrame:
    """Primary-line adjusted prices, only for companies that traded recently."""
    def build() -> pl.DataFrame:
        px = b.primary_prices(view)
        fresh = (px.group_by("company_id").agg(pl.col("trade_date").max().alias("_last"))
                   .filter(pl.col("_last") >= view.as_of_date
                           - dt.timedelta(days=MAX_STALENESS_DAYS)))
        return px.join(fresh.select("company_id"), on="company_id").sort("company_id",
                                                                            "trade_date")
    return view.memo("momentum_prices", build)


def _relative_strength(view: PitView, days: int) -> pl.DataFrame:
    px = _recent(view)
    idx = b.index_closes(view, BENCHMARK)
    g = (px.group_by("company_id")
           .agg(pl.len().alias("n"),
                pl.col("adj_close").last().alias("p_end"),
                pl.col("adj_close").get(pl.len() - 1 - days).alias("p_start"),
                pl.col("trade_date").last().alias("d_end"),
                pl.col("trade_date").get(pl.len() - 1 - days).alias("d_start"))
           .filter(pl.col("n") > days))
    if idx.height == 0:
        return b.finish(g.with_columns(pl.lit(None, dtype=pl.Float64).alias("v")), "v", [],
                        None, universe=b.companies(view))
    idx = idx.sort("trade_date")
    g = (g.sort("d_start").join_asof(idx.rename({"trade_date": "d_start", "idx_close": "i_start"}),
                                     on="d_start", strategy="backward")
          .sort("d_end").join_asof(idx.rename({"trade_date": "d_end", "idx_close": "i_end"}),
                                   on="d_end", strategy="backward"))
    g = g.with_columns((pl.col("p_end") / pl.col("p_start") - 1).alias("stock_ret"),
                       (pl.col("i_end") / pl.col("i_start") - 1).alias("index_ret"))
    g = g.with_columns((pl.col("stock_ret") - pl.col("index_ret")).alias("v"))
    return b.finish(g, "v", ["stock_ret", "index_ret", "d_start", "d_end"], None,
                    universe=b.companies(view))


@factor("rs_6m_vs_nifty500", "momentum", True,
        "126-trading-day adjusted return minus Nifty 500 return over the same dates")
def rs_6m_vs_nifty500(view: PitView) -> pl.DataFrame:
    return _relative_strength(view, 126)


@factor("rs_12m_vs_nifty500", "momentum", True,
        "252-trading-day adjusted return minus Nifty 500 return over the same dates")
def rs_12m_vs_nifty500(view: PitView) -> pl.DataFrame:
    return _relative_strength(view, 252)


def _dma(view: PitView) -> pl.DataFrame:
    px = _recent(view)
    return (px.group_by("company_id")
              .agg(pl.len().alias("n"), pl.col("adj_close").last().alias("close"),
                   pl.col("adj_close").tail(50).mean().alias("dma50"),
                   pl.col("adj_close").tail(200).mean().alias("dma200"))
              .filter(pl.col("n") >= 200))


@factor("price_vs_200dma", "momentum", True, "adjusted close / 200-day moving average - 1")
def price_vs_200dma(view: PitView) -> pl.DataFrame:
    d = _dma(view).with_columns((pl.col("close") / pl.col("dma200") - 1).alias("v"))
    return b.finish(d, "v", ["close", "dma200"], None, universe=b.companies(view))


@factor("dma_50_200_state", "momentum", True,
        "1 when the 50-day average is above the 200-day average, else 0")
def dma_50_200_state(view: PitView) -> pl.DataFrame:
    d = _dma(view).with_columns((pl.col("dma50") > pl.col("dma200")).cast(pl.Float64)
                                .alias("v"))
    return b.finish(d, "v", ["dma50", "dma200"], None, universe=b.companies(view))


@factor("delivery_pct_20d_vs_1y", "momentum", True,
        "mean delivery % of the last 20 sessions / mean over the last 250 (needs 200)")
def delivery_pct_20d_vs_1y(view: PitView) -> pl.DataFrame:
    px = _recent(view).filter(pl.col("delivery_pct").is_not_null()) \
        if "delivery_pct" in b.primary_prices(view).columns else _recent(view).clear()
    g = (px.group_by("company_id")
           .agg(pl.col("delivery_pct").tail(250).len().alias("n"),
                pl.col("delivery_pct").tail(20).mean().alias("d20"),
                pl.col("delivery_pct").tail(250).mean().alias("d250"))
           .filter(pl.col("n") >= 200)
           .with_columns(pl.when(pl.col("d250") > 0).then(pl.col("d20") / pl.col("d250"))
                         .alias("v")))
    return b.finish(g, "v", ["d20", "d250"], None, universe=b.companies(view))
