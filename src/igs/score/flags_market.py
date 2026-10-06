"""Market-behaviour checks from exchange prices known at as_of.

A strong fundamental score does not protect against the ways a thinly traded
or overheated stock fails: prices that can be moved by small orders, a run-up
that has already priced in years of growth, or a stock in free fall. These
checks describe the price record, with the numbers; they are cautions (they
keep a stock out of High conviction) unless configured otherwise.

Trading sessions are the market's (every date any primary line traded), so a
stock that did not trade on some sessions shows up as rarely traded rather than
being judged only on the days it happened to trade.
"""

from __future__ import annotations

import math

import polars as pl

from igs.factors import base as b
from igs.pit.view import PitView
from igs.score.flagbase import CRORE, pct, result, status_when

TRADING_DAYS_PER_YEAR = 250


def _sessions(view: PitView, n: int) -> list:
    def build() -> list:
        px = b.primary_prices(view)
        return sorted(px["trade_date"].unique().to_list())
    days = view.memo("market_sessions", build)
    return days[-n:]


def _window(view: PitView, n: int) -> pl.DataFrame:
    days = _sessions(view, n)
    if not days:
        return b.primary_prices(view).clear()
    return b.primary_prices(view).filter(pl.col("trade_date") >= days[0])


def _none(companies: list[int], flag: str, message: str) -> pl.DataFrame:
    return result(flag, companies, pl.DataFrame(
        schema={"company_id": pl.Int64, "status": pl.Utf8, "message": pl.Utf8}), message)


def illiquid(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Median daily traded value (close x volume) over the last `lookback_sessions` market
    sessions, and the share of those sessions the stock traded at all."""
    n = cfg["lookback_sessions"]
    min_tv = cfg["min_median_traded_value_cr"] * CRORE
    min_share = cfg["min_traded_session_pct"] / 100
    sessions = len(_sessions(view, n))
    w = _window(view, n)
    if sessions == 0 or w.height == 0:
        return _none(companies, "illiquid", "no prices in the lookback window")
    g = (w.with_columns((pl.col("close") * pl.col("volume")).alias("tv"))
          .group_by("company_id")
          .agg(pl.col("tv").filter(pl.col("volume") > 0).median().alias("median_tv"),
               (pl.col("volume") > 0).sum().alias("traded")))
    g = g.with_columns((pl.col("traded") / sessions).alias("share"),
                       pl.col("median_tv").fill_null(0.0))
    g = g.with_columns(
        status_when((pl.col("median_tv") < min_tv) | (pl.col("share") < min_share)),
        pl.format("median traded value Rs {} cr a day; traded on {} of the last {} sessions "
                  "(needs Rs {} cr and {})", (pl.col("median_tv") / CRORE).round(2),
                  pct(pl.col("share"), 0), pl.lit(sessions),
                  pl.lit(f"{cfg['min_median_traded_value_cr']:g}"),
                  pl.lit(f"{min_share:.0%}")).alias("message"),
        pl.struct("median_tv", "traded", "share").alias("evidence"))
    return result("illiquid", companies, g, "no prices in the lookback window")


def high_volatility(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Annualised standard deviation of daily log returns (adjusted closes) over the last
    `lookback_sessions` sessions; needs `min_observations` returns."""
    n, limit = cfg["lookback_sessions"], cfg["max_annualised_pct"] / 100
    w = _window(view, n).sort("company_id", "trade_date").with_columns(
        (pl.col("adj_close") / pl.col("adj_close").shift(1).over("company_id")).log()
        .alias("r"))
    g = (w.group_by("company_id").agg(pl.col("r").count().alias("n"), pl.col("r").std()
                                     .alias("sd"))
          .filter(pl.col("n") >= cfg["min_observations"])
          .with_columns((pl.col("sd") * math.sqrt(TRADING_DAYS_PER_YEAR)).alias("vol")))
    g = g.with_columns(
        status_when(pl.col("vol") > limit),
        pl.format("annualised volatility {} over {} sessions (limit {})", pct(pl.col("vol"), 0),
                  "n", pl.lit(f"{limit:.0%}")).alias("message"),
        pl.struct("vol", "n").alias("evidence"))
    return result("high_volatility", companies, g,
                  f"fewer than {cfg['min_observations']} daily returns in the window")


def _year_path(view: PitView, n: int) -> pl.DataFrame:
    w = _window(view, n + 1).sort("company_id", "trade_date")
    return (w.group_by("company_id")
             .agg(pl.len().alias("n"), pl.col("adj_close").first().alias("p0"),
                  pl.col("adj_close").last().alias("p1"),
                  pl.col("adj_close").max().alias("high"),
                  pl.col("trade_date").first().alias("d0"),
                  pl.col("trade_date").last().alias("d1")))


def speculative_runup(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Adjusted price change over the last `lookback_sessions` sessions above
    max_return_pct."""
    n, limit = cfg["lookback_sessions"], cfg["max_return_pct"] / 100
    g = _year_path(view, n).filter(pl.col("n") >= cfg["min_observations"])
    g = g.with_columns((pl.col("p1") / pl.col("p0") - 1).alias("ret")).with_columns(
        status_when(pl.col("ret") > limit),
        pl.format("price change {} from {} to {} (caution above {})", pct(pl.col("ret"), 0),
                  "d0", "d1", pl.lit(f"{limit:.0%}")).alias("message"),
        pl.struct("ret", "d0", "d1").alias("evidence"))
    return result("speculative_runup", companies, g,
                  f"fewer than {cfg['min_observations']} sessions of prices")


def deep_drawdown(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Latest adjusted close vs the highest adjusted close in the last `lookback_sessions`."""
    n, limit = cfg["lookback_sessions"], cfg["max_drawdown_pct"] / 100
    g = _year_path(view, n).filter(pl.col("n") >= cfg["min_observations"])
    g = g.with_columns((pl.col("p1") / pl.col("high") - 1).alias("dd")).with_columns(
        status_when(pl.col("dd") < -limit),
        pl.format("{} below the highest close of the last {} sessions (caution beyond {})",
                  pct(pl.col("dd").abs(), 0), pl.lit(n), pl.lit(f"{limit:.0%}"))
          .alias("message"),
        pl.struct("dd", "high", "p1").alias("evidence"))
    return result("deep_drawdown", companies, g,
                  f"fewer than {cfg['min_observations']} sessions of prices")


def trade_for_trade(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Latest NSE series is a trade-for-trade series (every trade settled by delivery,
    usually imposed as a surveillance measure)."""
    px = b.primary_prices(view)
    if "series" not in px.columns:
        return _none(companies, "trade_for_trade", "series not in the price data")
    series = cfg["series"]
    g = (px.filter(pl.col("series").is_not_null()).sort("trade_date").group_by("company_id")
           .agg(pl.col("series").last(), pl.col("trade_date").last().alias("d")))
    g = g.with_columns(
        status_when(pl.col("series").is_in(series)),
        pl.format("traded in series {} on {}", "series", "d").alias("message"))
    return result("trade_for_trade", companies, g, "series not in the price data")
