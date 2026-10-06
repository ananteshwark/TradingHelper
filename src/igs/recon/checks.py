"""Reconciliation checks for step 1 (ingestion, instrument master, adjusted prices).

Each check is a pure function over Polars frames returning a CheckResult with
the offending rows attached, so the report shows evidence, not just a verdict.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

import polars as pl

from igs.normalize.adjust import exchange_implied_factors

Status = Literal["pass", "warn", "fail"]
_RANK = {"pass": 0, "warn": 1, "fail": 2}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    summary: str
    details: pl.DataFrame | None = None


def worst(results: list[CheckResult]) -> Status:
    return max((r.status for r in results), key=_RANK.__getitem__, default="pass")


# --------------------------------------------------------------------------- ingestion


def unique_price_rows(prices: pl.DataFrame) -> CheckResult:
    key = ["exchange", "trade_date", "isin", "series"]
    dups = prices.group_by(key).len().filter(pl.col("len") > 1)
    if dups.height:
        return CheckResult("unique_price_rows", "fail",
                           f"{dups.height} duplicate exchange/date/ISIN/series keys "
                           "(pre-open or interim session rows not filtered?)", dups)
    return CheckResult("unique_price_rows", "pass", f"{prices.height} rows, no duplicate keys")


def session_filter(raw: pl.DataFrame, kept: pl.DataFrame, session_col: str,
                   final_sessions: list[str]) -> CheckResult:
    """Kept rows must all come from final sessions; report what was dropped by session."""
    by_session = raw.group_by(session_col).len().sort(session_col)
    bad = kept.filter(~pl.col(session_col).is_in(final_sessions))
    if bad.height:
        return CheckResult("session_filter", "fail",
                           f"{bad.height} kept rows are from non-final sessions", bad.head(50))
    dropped = raw.height - kept.height
    return CheckResult("session_filter", "pass",
                       f"kept {kept.height} final-session rows, dropped {dropped} "
                       f"(sessions kept: {final_sessions})", by_session)


def calendar_coverage(prices: pl.DataFrame, expected_dates: list[dt.date]) -> CheckResult:
    have = set(prices["trade_date"].unique().to_list())
    want = set(expected_dates)
    missing = sorted(want - have)
    extra = sorted(have - want)
    details = pl.DataFrame({
        "trade_date": missing + extra,
        "problem": ["missing"] * len(missing) + ["unexpected"] * len(extra),
    }, schema={"trade_date": pl.Date, "problem": pl.Utf8})
    if missing:
        return CheckResult("calendar_coverage", "fail",
                           f"{len(missing)} expected trading days have no prices", details)
    if extra:
        return CheckResult("calendar_coverage", "warn",
                           f"{len(extra)} dates with prices are not in the trading calendar",
                           details)
    return CheckResult("calendar_coverage", "pass", f"all {len(want)} trading days present")


# --------------------------------------------------------------------------- instrument master


def isin_mapping(prices: pl.DataFrame, identifiers: pl.DataFrame) -> CheckResult:
    """Every priced (ISIN, date) must map to exactly one security through the ISIN ranges."""
    isin_ranges = identifiers.filter(pl.col("id_type") == "ISIN").select(
        pl.col("id_value").alias("isin"), "security_key", "valid_from", "valid_to")
    obs = prices.select("isin", "trade_date").unique()
    joined = obs.join(isin_ranges, on="isin", how="left").with_columns(
        (pl.col("security_key").is_not_null()
         & (pl.col("trade_date") >= pl.col("valid_from"))
         & (pl.col("valid_to").is_null() | (pl.col("trade_date") < pl.col("valid_to"))))
        .alias("_hit"))
    counts = joined.group_by("isin", "trade_date").agg(pl.col("_hit").sum().alias("n"))
    bad = counts.filter(pl.col("n") != 1).sort("isin", "trade_date")
    if bad.height:
        return CheckResult("isin_mapping", "fail",
                           f"{bad.height} ISIN-days map to zero or several securities", bad)
    return CheckResult("isin_mapping", "pass",
                       f"{counts.height} ISIN-days each map to exactly one security")


def symbol_consistency(prices: pl.DataFrame) -> CheckResult:
    """One symbol+series should not carry two ISINs on the same day."""
    bad = (prices.group_by("trade_date", "symbol", "series")
                 .agg(pl.col("isin").n_unique().alias("n_isin"))
                 .filter(pl.col("n_isin") > 1))
    if bad.height:
        return CheckResult("symbol_consistency", "fail",
                           f"{bad.height} symbol-days carry more than one ISIN", bad)
    return CheckResult("symbol_consistency", "pass", "each symbol-day has one ISIN")


# --------------------------------------------------------------------------- adjusted prices


def factors_vs_exchange(prices: pl.DataFrame, factors: pl.DataFrame,
                        tol: float = 0.005) -> CheckResult:
    """Compare corporate-action factors with the exchange's own reference-price adjustment.

    On an ex-date the exchange resets the previous close. prev_close / our
    previous close is therefore an independent estimate of the factor:
      * event with a factor that disagrees        -> fail (wrong ratio or date)
      * exchange adjusted but no event on record  -> fail (missing corporate action)
      * event we cannot compute (needs_review)    -> warn
    """
    implied = exchange_implied_factors(prices)
    ev = (factors.group_by("security_id", "ex_date")
                 .agg(pl.col("factor").product().alias("our_factor"),
                      (pl.col("status") == "needs_review").any().alias("needs_review"),
                      pl.col("action_type").str.join(",").alias("actions")))
    j = implied.join(ev, left_on=["security_id", "trade_date"],
                     right_on=["security_id", "ex_date"], how="full", coalesce=True)
    j = j.with_columns(
        pl.when(pl.col("needs_review").fill_null(False))
          .then(pl.lit("needs_review"))
          .when(pl.col("our_factor").is_null() & ((pl.col("implied_factor") - 1).abs() > tol))
          .then(pl.lit("missing_corporate_action"))
          .when(pl.col("our_factor").is_not_null() & pl.col("implied_factor").is_null())
          .then(pl.lit("no_price_on_ex_date"))
          .when(pl.col("our_factor").is_not_null()
                & ((pl.col("implied_factor") / pl.col("our_factor") - 1).abs() > tol))
          .then(pl.lit("factor_mismatch"))
          .otherwise(pl.lit("ok"))
          .alias("verdict"))
    problems = j.filter(pl.col("verdict") != "ok").sort("security_id", "trade_date")
    n_events = ev.height
    fails = problems.filter(pl.col("verdict").is_in(["missing_corporate_action",
                                                      "factor_mismatch"])).height
    if fails:
        return CheckResult("factors_vs_exchange", "fail",
                           f"{fails} ex-dates disagree with the exchange-implied factor "
                           f"({n_events} corporate-action ex-dates checked)", problems)
    if problems.height:
        return CheckResult("factors_vs_exchange", "warn",
                           f"{problems.height} events need manual review", problems)
    return CheckResult("factors_vs_exchange", "pass",
                       f"{n_events} ex-dates agree with exchange within {tol:.1%}")


def unexplained_gaps(adjusted: pl.DataFrame, factors: pl.DataFrame,
                     threshold: float = 0.25) -> CheckResult:
    """Day-on-day adjusted moves beyond threshold that are not on an ex-date."""
    moves = (adjusted.sort("security_id", "trade_date")
                     .with_columns((pl.col("adj_close") / pl.col("adj_close").shift(1)
                                    .over("security_id") - 1).alias("ret")))
    ex = factors.select("security_id", pl.col("ex_date").alias("trade_date"),
                        pl.lit(True).alias("_ex")).unique()
    flagged = (moves.join(ex, on=["security_id", "trade_date"], how="left")
                    .filter((pl.col("ret").abs() > threshold) & pl.col("_ex").is_null())
                    .select("security_id", "trade_date", "close", "adj_close", "ret"))
    if flagged.height:
        return CheckResult("unexplained_gaps", "warn",
                           f"{flagged.height} adjusted moves beyond {threshold:.0%} with no "
                           "corporate action (genuine news, or a missing action)", flagged)
    return CheckResult("unexplained_gaps", "pass", f"no unexplained moves beyond {threshold:.0%}")


def cross_source_close(primary: pl.DataFrame, secondary: pl.DataFrame,
                       tol_bps: float = 10.0, min_match_share: float = 0.99) -> CheckResult:
    """Compare unadjusted closes between two sources on common (isin, trade_date) keys."""
    j = primary.select("isin", "trade_date", pl.col("close").alias("close_a")).join(
        secondary.select("isin", "trade_date", pl.col("close").alias("close_b")),
        on=["isin", "trade_date"], how="inner")
    if j.height == 0:
        return CheckResult("cross_source_close", "fail", "no overlapping rows between sources")
    j = j.with_columns(((pl.col("close_b") / pl.col("close_a") - 1).abs() * 1e4).alias("diff_bps"))
    within = (j["diff_bps"] <= tol_bps).mean()
    worst_rows = j.sort("diff_bps", descending=True).head(50)
    status: Status = "pass" if within >= min_match_share else "fail"
    return CheckResult("cross_source_close", status,
                       f"{within:.2%} of {j.height} common rows within {tol_bps:g} bps",
                       worst_rows)
