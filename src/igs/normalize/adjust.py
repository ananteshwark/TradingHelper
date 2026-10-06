"""Corporate-action price adjustment.

Convention: a factor f for an event with ex-date E multiplies every price
strictly before E, so pre-event prices become comparable with post-event
prices. Volumes are divided by the same factor. The cumulative factor for a
trade date t is the product of f over all events with ex_date > t.

Only events with ex_date <= as_of are applied (see `adjusted_prices`), so a
split announced for next month cannot leak into today's price levels.

Dividends are excluded from price adjustment and only used for total-return
series. Demergers and anything else without a clean formula are not adjusted
automatically: they are returned as `needs_review` so the reconciliation
report shows them, and the exchange-implied factor can be used after a human
has checked it.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

KEY = "security_id"


def split_factor(fv_old: float, fv_new: float) -> float:
    """Face value fv_old -> fv_new (e.g. 10 -> 2 gives 0.2). Covers consolidation too."""
    if fv_old <= 0 or fv_new <= 0:
        raise ValueError(f"face values must be positive, got {fv_old} -> {fv_new}")
    return fv_new / fv_old


def bonus_factor(a: float, b: float) -> float:
    """a new shares for every b held (e.g. 1:1 gives 0.5)."""
    if a <= 0 or b <= 0:
        raise ValueError(f"bonus ratio must be positive, got {a}:{b}")
    return b / (a + b)


def rights_factor(a: float, b: float, issue_price: float, cum_price: float) -> float:
    """a new shares for every b held at issue_price; cum_price is the last close before ex-date.

    factor = TERP / cum_price, TERP = (b * cum_price + a * issue_price) / (a + b).
    A rights issue priced at or above the market price has no dilution: factor 1.
    """
    if a <= 0 or b <= 0 or cum_price <= 0 or issue_price < 0:
        raise ValueError("rights inputs must be positive")
    if issue_price >= cum_price:
        return 1.0
    terp = (b * cum_price + a * issue_price) / (a + b)
    return terp / cum_price


def dividend_factor(cash_per_share: float, cum_price: float) -> float:
    """Total-return factor for a cash dividend."""
    if cum_price <= 0 or cash_per_share < 0 or cash_per_share >= cum_price:
        raise ValueError(f"invalid dividend {cash_per_share} vs price {cum_price}")
    return (cum_price - cash_per_share) / cum_price


def _cum_price(events: pl.DataFrame, prices: pl.DataFrame) -> pl.DataFrame:
    """Attach the last close strictly before each event's ex-date."""
    left = events.with_columns((pl.col("ex_date") - dt.timedelta(days=1)).alias("_cum_date"))
    right = prices.select(KEY, pl.col("trade_date").alias("_cum_date"),
                          pl.col("close").alias("cum_price")).sort(KEY, "_cum_date")
    return (left.sort(KEY, "_cum_date")
                .join_asof(right, on="_cum_date", by=KEY, strategy="backward",
                           check_sortedness=False)
                .drop("_cum_date"))


def event_factors(
    actions: pl.DataFrame,
    prices: pl.DataFrame,
    *,
    include_dividends: bool = False,
) -> pl.DataFrame:
    """One row per corporate action with its adjustment factor.

    actions needs: security_id, ca_id, action_type, ex_date and the type-specific
    columns (fv_old, fv_new, ratio_a, ratio_b, issue_price, cash_per_share).
    prices needs: security_id, trade_date, close (unadjusted).

    Returns security_id, ca_id, ex_date, action_type, factor, status where status
    is 'ok' or 'needs_review' (factor null).
    """
    with_cum = _cum_price(actions, prices)
    out = []
    for r in with_cum.iter_rows(named=True):
        kind = r["action_type"]
        factor: float | None
        status = "ok"
        try:
            if kind in ("split", "consolidation"):
                factor = split_factor(r["fv_old"], r["fv_new"])
            elif kind == "bonus":
                factor = bonus_factor(r["ratio_a"], r["ratio_b"])
            elif kind == "rights":
                if r["cum_price"] is None:
                    raise ValueError("no close before ex-date")
                factor = rights_factor(r["ratio_a"], r["ratio_b"], r["issue_price"],
                                       r["cum_price"])
            elif kind == "dividend":
                if not include_dividends:
                    continue
                if r["cum_price"] is None:
                    raise ValueError("no close before ex-date")
                factor = dividend_factor(r["cash_per_share"], r["cum_price"])
            else:
                factor, status = None, "needs_review"
        except (TypeError, ValueError):
            factor, status = None, "needs_review"
        out.append({KEY: r[KEY], "ca_id": r["ca_id"], "ex_date": r["ex_date"],
                    "action_type": kind, "factor": factor, "status": status})
    schema = {KEY: pl.Int64, "ca_id": pl.Int64, "ex_date": pl.Date, "action_type": pl.Utf8,
              "factor": pl.Float64, "status": pl.Utf8}
    return pl.DataFrame(out, schema=schema)


def cumulative_factors(prices: pl.DataFrame, factors: pl.DataFrame) -> pl.Series:
    """Cumulative adjustment factor for each price row (product over ex_date > trade_date)."""
    ev = (factors.filter(pl.col("factor").is_not_null())
                 .group_by(KEY, "ex_date").agg(pl.col("factor").product())
                 .sort(KEY, "ex_date")
                 .with_columns(pl.col("factor").cum_prod(reverse=True).over(KEY).alias("suffix"))
                 # ex_date > trade_date  <=>  ex_date - 1 day >= trade_date
                 .with_columns((pl.col("ex_date") - dt.timedelta(days=1)).alias("_key"))
                 .select(KEY, "_key", "suffix")
                 .sort(KEY, "_key"))
    left = (prices.with_row_index("_row")
                  .select("_row", KEY, pl.col("trade_date").alias("_key"))
                  .sort(KEY, "_key"))
    joined = left.join_asof(ev, on="_key", by=KEY, strategy="forward", check_sortedness=False)
    return joined.sort("_row")["suffix"].fill_null(1.0).alias("cum_factor")


def adjusted_prices(
    prices: pl.DataFrame,
    factors: pl.DataFrame,
    as_of: dt.date,
) -> pl.DataFrame:
    """Unadjusted prices up to as_of, adjusted only for events with ex_date <= as_of."""
    px = prices.filter(pl.col("trade_date") <= as_of)
    known = factors.filter(pl.col("ex_date") <= as_of)
    cum = cumulative_factors(px, known)
    out = px.with_columns(cum)
    price_cols = [c for c in ("open", "high", "low", "close", "last", "prev_close")
                  if c in out.columns]
    out = out.with_columns([(pl.col(c) * pl.col("cum_factor")).alias(f"adj_{c}")
                            for c in price_cols])
    if "volume" in out.columns:
        out = out.with_columns((pl.col("volume") / pl.col("cum_factor")).alias("adj_volume"))
    return out


def exchange_implied_factors(prices: pl.DataFrame) -> pl.DataFrame:
    """prev_close reported on day t divided by our close on the previous trading day.

    When the exchange adjusts its reference price for a corporate action, this
    ratio differs from 1 on the ex-date. It is an independent check on the
    factors derived from corporate-action data (see recon).
    """
    return (prices.sort(KEY, "trade_date")
                  .with_columns(pl.col("close").shift(1).over(KEY).alias("our_prev_close"),
                                pl.col("trade_date").shift(1).over(KEY).alias("prev_trade_date"))
                  .filter(pl.col("our_prev_close").is_not_null()
                          & pl.col("prev_close").is_not_null())
                  .with_columns((pl.col("prev_close") / pl.col("our_prev_close"))
                                .alias("implied_factor"))
                  .select(KEY, "trade_date", "prev_trade_date", "prev_close", "our_prev_close",
                          "implied_factor"))
