"""Point-in-time investable universe.

Built from what was trading on the date (so names later delisted or suspended
are included for dates when they traded), with every filter evaluated as of
that date. Every excluded company keeps a reason.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from igs.config import UniverseConfig
from igs.factors import base as b
from igs.pit.view import PitView

CRORE = 1e7
TRADED_WITHIN_DAYS = 7
MCAP_RANK_SESSIONS = 126


def _series_ok(cfg: UniverseConfig) -> list[str]:
    return list(cfg.include_series) + (list(cfg.sme_series) if cfg.include_sme else [])


def _surveillance(view: PitView) -> pl.DataFrame:
    """Companies in ASM/GSM on the as-of date: present in the latest snapshot of that
    measure taken on or before the date. Measures with no snapshot yet are unknown."""
    if not view.has("surveillance"):
        return pl.DataFrame(schema={"company_id": pl.Int64, "measure": pl.Utf8,
                                    "stage": pl.Utf8})
    s = view.table("surveillance").filter(pl.col("company_id").is_not_null())
    latest = s.group_by("measure").agg(pl.col("effective_from").max().alias("_d"))
    return (s.join(latest, on="measure").filter(pl.col("effective_from") == pl.col("_d"))
             .select("company_id", "measure", "stage").unique())


def surveillance_known(view: PitView) -> set[str]:
    if not view.has("surveillance"):
        return set()
    return set(view.table("surveillance")["measure"].unique().to_list())


def build_universe(view: PitView, cfg: UniverseConfig) -> pl.DataFrame:
    """One row per company seen in prices, with inclusion flag and reason.

    Columns: company_id, symbol, mcap_cr, avg_mcap_cr, mcap_rank, bucket, quarters_filed,
    industry, sector, basic_industry, industry_source (see factors.base.classification),
    surveillance, included, reason
    """
    px = b.primary_prices(view)
    cutoff = view.as_of_date - dt.timedelta(days=TRADED_WITHIN_DAYS)
    last = (px.group_by("company_id")
              .agg(pl.col("trade_date").last().alias("last_trade"),
                   pl.col("series").last().alias("series") if "series" in px.columns
                   else pl.lit(None, dtype=pl.Utf8).alias("series"),
                   pl.col("symbol").last().alias("symbol") if "symbol" in px.columns
                   else pl.lit(None, dtype=pl.Utf8).alias("symbol")))
    mc = b.market_cap(view).select("company_id", "mcap", "shares")
    shares = mc.select("company_id", "shares")
    avg = (px.join(shares, on="company_id")
             .group_by("company_id")
             .agg((pl.col("adj_close").tail(MCAP_RANK_SESSIONS) * pl.col("shares").first())
                  .mean().alias("avg_mcap")))
    q = b.quarterly(view).filter(pl.col("top_line").is_not_null() | pl.col("pat").is_not_null())
    filed = q.group_by("company_id").agg(pl.col("period_end").n_unique().alias("quarters_filed"))
    u = (last.join(mc.select("company_id", "mcap"), on="company_id", how="left")
             .join(avg, on="company_id", how="left")
             .join(filed, on="company_id", how="left")
             .with_columns(pl.col("quarters_filed").fill_null(0)))
    u = u.with_columns(pl.col("avg_mcap").rank("ordinal", descending=True).alias("mcap_rank"))
    buckets = cfg.market_cap_buckets
    u = u.with_columns(
        pl.when(pl.col("mcap_rank") <= buckets.large_max_rank).then(pl.lit("large"))
          .when(pl.col("mcap_rank") <= buckets.mid_max_rank).then(pl.lit("mid"))
          .when(pl.col("mcap_rank").is_not_null()).then(pl.lit("small"))
          .alias("bucket"))
    u = u.join(b.classification(view), on="company_id", how="left")
    surv = _surveillance(view)
    flagged = surv.group_by("company_id").agg(
        pl.concat_str([pl.col("measure"), pl.col("stage").fill_null("")], separator=" ")
          .str.join(", ").alias("surveillance"),
        (pl.col("measure") == "ASM").any().alias("_asm"),
        (pl.col("measure") == "GSM").any().alias("_gsm"))
    u = u.join(flagged, on="company_id", how="left").with_columns(
        pl.col("_asm").fill_null(False), pl.col("_gsm").fill_null(False))

    known = surveillance_known(view)
    reason = (
        pl.when(pl.col("last_trade") < cutoff).then(pl.lit("not traded in the last week"))
        .when(pl.col("series").is_not_null() & ~pl.col("series").is_in(_series_ok(cfg)))
        .then(pl.format("series {} excluded", pl.col("series")))
        .when(pl.col("mcap").is_null()).then(pl.lit("market cap unavailable"))
        .when(pl.col("mcap") < cfg.min_market_cap_cr * CRORE)
        .then(pl.lit(f"market cap below Rs {cfg.min_market_cap_cr:g} cr"))
        .when(pl.col("quarters_filed") < cfg.min_filing_quarters)
        .then(pl.format("only {} quarters filed", pl.col("quarters_filed")))
        .when(pl.col("_asm") & pl.lit(not cfg.include_asm)).then(pl.lit("ASM surveillance"))
        .when(pl.col("_gsm") & pl.lit(not cfg.include_gsm)).then(pl.lit("GSM surveillance"))
        .when(~pl.col("bucket").is_in(cfg.include_buckets))
        .then(pl.format("{} cap bucket not ranked", pl.col("bucket")))
    )
    if cfg.sectors.include:
        reason = reason.when(~pl.col("sector").is_in(cfg.sectors.include)).then(
            pl.lit("sector not included"))
    if cfg.sectors.exclude:
        reason = reason.when(pl.col("sector").is_in(cfg.sectors.exclude)).then(
            pl.lit("sector excluded"))
    if cfg.industries.include:
        reason = reason.when(~pl.col("industry").is_in(cfg.industries.include)).then(
            pl.lit("industry not included"))
    if cfg.industries.exclude:
        reason = reason.when(pl.col("industry").is_in(cfg.industries.exclude)).then(
            pl.lit("industry excluded"))
    u = u.with_columns(reason.otherwise(None).alias("reason"))
    u = u.with_columns(pl.col("reason").is_null().alias("included"),
                       (pl.col("mcap") / CRORE).alias("mcap_cr"),
                       (pl.col("avg_mcap") / CRORE).alias("avg_mcap_cr"),
                       pl.lit(",".join(sorted(known)) or "none").alias("surveillance_history"))
    return u.select("company_id", "symbol", "mcap_cr", "avg_mcap_cr", "mcap_rank", "bucket",
                    "quarters_filed", "industry", "sector", "basic_industry",
                    "industry_source", "surveillance",
                    "surveillance_history", "included", "reason").sort("company_id")
