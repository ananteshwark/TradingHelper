"""Key numbers for the stock page and the AI's calls: price and 52-week range, market cap,
P/E, EPS, book value, P/B, debt/equity, dividend yield, sales and profit (TTM), face value,
promoter holding and pledge.

They are computed from the run's point-in-time view with the factor library's building
blocks, so P/E and P/B use the same market cap and earnings as the valuation factors, and
stored with the run (score_result.key_numbers). Display only: none of it feeds the score.
"""

from __future__ import annotations

import datetime as dt
import math

import polars as pl

from igs.factors import base as b
from igs.pit.view import PitView

CRORE = 1e7


def _finite(v):
    return v if isinstance(v, int | float) and math.isfinite(v) else None


def key_numbers(view: PitView, company_ids: list[int]) -> dict[int, dict]:
    ids = pl.DataFrame({"company_id": company_ids}, schema={"company_id": pl.Int64})
    year_ago = view.as_of_date - dt.timedelta(days=365)

    mc = b.market_cap(view).select("company_id", "security_id", "px_date", "px_close",
                                   "shares", "mcap")
    px = b.primary_prices(view)
    rng = (px.filter(pl.col("trade_date") > year_ago).group_by("company_id")
             .agg(pl.coalesce("adj_high", "adj_close").max().alias("high_52w"),
                  pl.coalesce("adj_low", "adj_close").min().alias("low_52w"),
                  pl.col("trade_date").min().alias("range_from")))
    pat = b.ttm(view, "pat").select("company_id", "pat_ttm")
    sales = b.ttm(view, "top_line").select("company_id", pl.col("top_line_ttm").alias(
        "sales_ttm"))
    bs = b.balance_sheet(view).select(
        "company_id", "bs_date", pl.coalesce("equity_owners", "total_equity").alias("equity"),
        (pl.col("borrowings_noncurrent").fill_null(0)
         + pl.col("borrowings_current").fill_null(0)).alias("debt"),
        (pl.col("borrowings_noncurrent").is_not_null()
         | pl.col("borrowings_current").is_not_null()).alias("_has_debt"))
    financial = b.financials(view).select("company_id", pl.lit(True).alias("financial"))

    j = (ids.join(mc, on="company_id", how="left").join(rng, on="company_id", how="left")
            .join(pat, on="company_id", how="left").join(sales, on="company_id", how="left")
            .join(bs, on="company_id", how="left")
            .join(financial, on="company_id", how="left"))
    j = j.with_columns(
        pl.when(pl.col("pat_ttm") > 0).then(pl.col("mcap") / pl.col("pat_ttm")).alias("pe"),
        (pl.col("pat_ttm") / pl.col("shares")).alias("eps_ttm"),
        (pl.col("equity") / pl.col("shares")).alias("book_value"),
        pl.when(pl.col("equity") > 0).then(pl.col("mcap") / pl.col("equity")).alias("pb"),
        pl.when((pl.col("equity") > 0) & pl.col("_has_debt")
                & pl.col("financial").is_null())
          .then(pl.col("debt") / pl.col("equity")).alias("debt_to_equity"))

    dividends = _dividends(view, px, year_ago)
    holding = _promoter(view)
    face = _face_value(view)
    j = (j.join(dividends, on="company_id", how="left")
          .join(holding, on="company_id", how="left").join(face, on="company_id", how="left"))

    out = {}
    for r in j.iter_rows(named=True):
        price = r["px_close"]
        dps = r["dps_12m"]
        out[r["company_id"]] = {k: (_finite(v) if not isinstance(v, dt.date) else v.isoformat())
                                for k, v in {
            "price": price, "price_date": r["px_date"],
            "mcap_cr": None if r["mcap"] is None else r["mcap"] / CRORE,
            "high_52w": r["high_52w"], "low_52w": r["low_52w"],
            "range_from": r["range_from"],
            "pe": r["pe"], "eps_ttm": r["eps_ttm"],
            "book_value": r["book_value"], "pb": r["pb"],
            "debt_to_equity": r["debt_to_equity"],
            "dividend_12m": dps,
            "dividend_yield": (dps / price if dps is not None and price else None),
            "sales_ttm_cr": None if r["sales_ttm"] is None else r["sales_ttm"] / CRORE,
            "pat_ttm_cr": None if r["pat_ttm"] is None else r["pat_ttm"] / CRORE,
            "face_value": r["face_value"],
            "promoter_pct": r["promoter_pct"], "pledged_pct": r["pledged_pct"],
            "shareholding_date": r["shp_period"], "balance_sheet_date": r["bs_date"],
            "financial": bool(r["financial"])}.items()}
    return out


def _dividends(view: PitView, px: pl.DataFrame, since: dt.date) -> pl.DataFrame:
    """Cash dividends per share with ex-date in the last year, on today's share basis
    (a later split or bonus divides them as it divides the price)."""
    schema = {"company_id": pl.Int64, "dps_12m": pl.Float64}
    if not view.has("corporate_actions") or px.height == 0:
        return pl.DataFrame(schema=schema)
    div = (view.table("corporate_actions")
               .filter((pl.col("action_type") == "dividend") & (pl.col("ex_date") > since)
                       & (pl.col("ex_date") <= view.as_of_date)
                       & pl.col("cash_per_share").is_not_null())
               .select("security_id", pl.col("ex_date").alias("trade_date"),
                       "cash_per_share").sort("trade_date"))
    if div.height == 0:
        return pl.DataFrame(schema=schema)
    factor = (px.filter(pl.col("close") > 0)
                .select("company_id", "security_id", "trade_date",
                        (pl.col("adj_close") / pl.col("close")).alias("f"))
                .sort("trade_date"))
    j = div.join_asof(factor, on="trade_date", by="security_id", strategy="forward",
                      check_sortedness=False)
    return (j.filter(pl.col("company_id").is_not_null()).group_by("company_id")
             .agg((pl.col("cash_per_share") * pl.col("f").fill_null(1.0)).sum()
                  .alias("dps_12m")).cast(schema))


def _promoter(view: PitView) -> pl.DataFrame:
    schema = {"company_id": pl.Int64, "promoter_pct": pl.Float64, "pledged_pct": pl.Float64,
              "shp_period": pl.Date}
    if not view.has("shareholding"):
        return pl.DataFrame(schema=schema)
    return (view.table("shareholding").filter(pl.col("category") == "promoter")
                .sort("period_end", "filed_at").group_by("company_id")
                .agg(pl.col("pct_of_total").last().alias("promoter_pct"),
                     pl.col("pledged_pct").last().alias("pledged_pct"),
                     pl.col("period_end").last().alias("shp_period")).cast(schema))


def _face_value(view: PitView) -> pl.DataFrame:
    schema = {"company_id": pl.Int64, "face_value": pl.Float64}
    if not view.has("facts"):
        return pl.DataFrame(schema=schema)
    f = (view.facts(concepts=["face_value"]).filter(pl.col("value") > 0)
             .join(b.basis_choice(view), on=["company_id", "statement_basis"]))
    return (f.sort("period_end").group_by("company_id")
             .agg(pl.col("value").last().alias("face_value")).cast(schema))
