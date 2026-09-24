"""When did each kind of row become knowable?

Every table that factor code can see gets a `known_at` column (UTC) computed
by exactly one rule defined here. The point-in-time view, the truncation used
by the look-ahead tests, and the backtest all use these rules, so there is a
single definition of "public at time T".

Rules:
  facts, filings, shareholding, announcements
      -> filed_at: the exchange dissemination timestamp. Never period_end.
  prices
      -> trade_date at the closing time (15:30 IST). An end-of-day signal as of
         T 23:59:59 IST therefore sees T's close, and nothing later.
  corporate_actions
      -> announced_at, or 00:00 IST on ex_date when the announcement time is
         unknown (an action is always public by its ex-date). Price adjustment
         additionally requires ex_date <= as_of date.
  index_prices
      -> trade_date at the closing time, like prices.
  surveillance
      -> 00:00 IST on effective_from (the date the stage applied).
  industry
      -> 00:00 IST on valid_from (the date the classification was observed).
"""

from __future__ import annotations

from collections.abc import Callable

import polars as pl

KNOWN_AT = "known_at"
MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE = 15, 30


def _aware_utc(col: str) -> Callable[[pl.DataFrame], pl.Expr]:
    def rule(df: pl.DataFrame) -> pl.Expr:
        dtype = df.schema[col]
        if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
            raise ValueError(f"column {col!r} must be a timezone-aware Datetime, got {dtype}")
        return pl.col(col).dt.convert_time_zone("UTC")
    return rule


def _ist_date_at(col: str, hour: int = 0, minute: int = 0) -> pl.Expr:
    return ((pl.col(col).cast(pl.Datetime("us")) + pl.duration(hours=hour, minutes=minute))
            .dt.replace_time_zone("Asia/Kolkata")
            .dt.convert_time_zone("UTC"))


def _prices(df: pl.DataFrame) -> pl.Expr:
    return _ist_date_at("trade_date", MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE)


def _corporate_actions(df: pl.DataFrame) -> pl.Expr:
    ex_start = _ist_date_at("ex_date")
    if "announced_at" not in df.columns:
        return ex_start
    announced = _aware_utc("announced_at")(df)
    return pl.min_horizontal(announced, ex_start)


def _surveillance(df: pl.DataFrame) -> pl.Expr:
    return _ist_date_at("effective_from")


def _index_prices(df: pl.DataFrame) -> pl.Expr:
    return _ist_date_at("trade_date", MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE)


def _industry(df: pl.DataFrame) -> pl.Expr:
    return _ist_date_at("valid_from")


RULES: dict[str, Callable[[pl.DataFrame], pl.Expr]] = {
    "facts": _aware_utc("filed_at"),
    "filings": _aware_utc("filed_at"),
    "shareholding": _aware_utc("filed_at"),
    "announcements": _aware_utc("filed_at"),
    "prices": _prices,
    "index_prices": _index_prices,
    "corporate_actions": _corporate_actions,
    "surveillance": _surveillance,
    "industry": _industry,
}


def with_known_at(table: str, df: pl.DataFrame) -> pl.DataFrame:
    """Return df with a known_at column computed by the rule for `table`."""
    if table not in RULES:
        raise KeyError(f"no knowledge-time rule for table {table!r}; add one to RULES")
    return df.with_columns(RULES[table](df).dt.cast_time_unit("us").alias(KNOWN_AT))
