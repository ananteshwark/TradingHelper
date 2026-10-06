"""Valuation pillar with sector-specific modules.

Which valuation factors apply to which kind of company comes from
scoring.yaml `valuation_modules` (EV/EBITDA is never applied to banks or
NBFCs). A factor that does not apply returns not_applicable for that company.
All multiples use market cap = last unadjusted close x shares outstanding
(latest shareholding filing, adjusted for later splits/bonuses).
"""

from __future__ import annotations

import datetime as dt
from functools import cache

import polars as pl

from igs.config import load_scoring
from igs.factors import base as b
from igs.factors.growth import _cagr_factor
from igs.factors.registry import factor
from igs.pit.view import PitView

PE_HISTORY_YEARS = 5
PE_HISTORY_MIN_POINTS = 24


@cache
def _modules_config() -> dict[str, tuple[str, ...]]:
    return {k: tuple(v) for k, v in load_scoring().valuation_modules.items()}


def _not_applicable(view: PitView, name: str) -> pl.DataFrame:
    cfg = _modules_config()
    mods = b.modules(view)
    allowed = [m for m, factors in cfg.items() if name in factors]
    return mods.filter(~pl.col("module").is_in(allowed))


def _finish(view: PitView, name: str, df: pl.DataFrame, detail: list[str],
            ids: str, unfavourable: pl.Expr | None = None) -> pl.DataFrame:
    return b.finish(df, "v", detail, ids, universe=b.companies(view),
                    not_applicable=_not_applicable(view, name), unfavourable=unfavourable)


def _pe_now(view: PitView) -> pl.DataFrame:
    pat = b.ttm(view, "pat")
    return (b.market_cap(view).join(pat, on="company_id")
             .with_columns(pl.when(pl.col("pat_ttm") > 0)
                           .then(pl.col("mcap") / pl.col("pat_ttm")).alias("pe")))


def _pe_history(view: PitView) -> pl.DataFrame:
    """Month-end P/E over the last 5 years using what is known at as_of.

    For month-end t the earnings are the TTM ending at the latest quarter
    first filed by t; the price is the adjusted close at t; shares are today's
    (adjusted prices make them comparable across splits and bonuses).
    """
    q = b.quarterly(view).filter(pl.col("pat").is_not_null())
    if q.height == 0 or not view.has("facts"):
        return pl.DataFrame(schema={"company_id": pl.Int64, "pe": pl.Float64})
    first_filed = (view.table("facts").filter(pl.col("period_type") == "Q")
                       .join(b.basis_choice(view), on=["company_id", "statement_basis"])
                       .group_by("company_id", "period_end")
                       .agg(pl.col("filed_at").min().alias("first_filed")))
    q = q.join(first_filed, on=["company_id", "period_end"])
    q = q.sort("company_id", "qidx").with_columns(
        pl.col("pat").rolling_sum(4).over("company_id").alias("pat_ttm"),
        (pl.col("qidx") - pl.col("qidx").shift(3).over("company_id")).alias("_span"))
    q = q.filter(pl.col("_span") == 3).select(
        "company_id", pl.col("first_filed").dt.convert_time_zone("Asia/Kolkata").dt.date()
        .alias("known_date"), "pat_ttm").sort("company_id", "known_date")

    px = b.primary_prices(view)
    if px.height == 0 or q.height == 0:
        return pl.DataFrame(schema={"company_id": pl.Int64, "pe": pl.Float64})
    start = view.as_of_date - dt.timedelta(days=365 * PE_HISTORY_YEARS)
    monthly = (px.filter(pl.col("trade_date") >= start)
                 .with_columns(pl.col("trade_date").dt.truncate("1mo").alias("_m"))
                 .sort("trade_date").group_by("company_id", "_m")
                 .agg(pl.col("trade_date").last(), pl.col("adj_close").last())
                 .sort("company_id", "trade_date"))
    shares = b.shares_outstanding(view).select("company_id", "shares")
    j = monthly.join_asof(q.rename({"known_date": "trade_date"}), on="trade_date",
                          by="company_id", strategy="backward", check_sortedness=False)
    return (j.join(shares, on="company_id")
             .with_columns(pl.when(pl.col("pat_ttm") > 0)
                           .then(pl.col("adj_close") * pl.col("shares") / pl.col("pat_ttm"))
                           .alias("pe"))
             .filter(pl.col("pe").is_not_null()))


@factor("pe_vs_own_5y_median", "valuation", False,
        "current P/E divided by the median month-end P/E of the last 5 years (needs 24 "
        "months of positive earnings); a current loss ranks with the dearest peers "
        "(unfavourable)")
def pe_vs_own_5y_median(view: PitView) -> pl.DataFrame:
    hist = (_pe_history(view).group_by("company_id")
            .agg(pl.col("pe").median().alias("pe_median_5y"), pl.len().alias("n_months")))
    j = (_pe_now(view).join(hist, on="company_id")
         .filter(pl.col("n_months") >= PE_HISTORY_MIN_POINTS)
         .with_columns((pl.col("pe") / pl.col("pe_median_5y")).alias("v")))
    return _finish(view, "pe_vs_own_5y_median", j, ["pe", "pe_median_5y", "n_months"], "ids",
                   unfavourable=pl.col("pat_ttm") <= 0)


@factor("peg_trailing", "valuation", False,
        "P/E divided by trailing 3-year profit CAGR in percent; a loss or a 3-year profit "
        "decline ranks with the dearest peers (unfavourable); undefined when the base "
        "profit is not positive or below 2% of revenue")
def peg_trailing(view: PitView) -> pl.DataFrame:
    growth = _cagr_factor(view, 'pat', 3).select(
        'company_id', pl.col('value').alias('g3'), pl.col('status').alias('g_status'),
        pl.col('source_fact_ids').alias('ids_growth'),
        pl.col('detail').str.json_path_match('$.comparison').fill_null('ttm')
          .alias('growth_comparison'))
    j = _pe_now(view).join(growth, on='company_id', how='left').with_columns(
        pl.when((pl.col('pe') > 0) & (pl.col('g3') > 0))
          .then(pl.col('pe') / (pl.col('g3') * 100)).alias('v'),
        pl.concat_list('ids', pl.col('ids_growth').fill_null(
            pl.lit([], dtype=pl.List(pl.Int64)))).alias('all_ids'))
    shrinking = (pl.col('g_status') == b.OK) & (pl.col('g3') <= 0)
    return _finish(view, 'peg_trailing', j, ['pe', 'g3', 'growth_comparison'], 'all_ids',
                   unfavourable=(pl.col('pat_ttm') <= 0) | shrinking
                   | (pl.col('g_status') == b.UNFAVOURABLE))


@factor("ev_ebitda", "valuation", False,
        "(market cap + borrowings - cash & current investments) / TTM EBITDA; negative "
        "EBITDA ranks with the dearest peers (unfavourable); never for banks or NBFCs")
def ev_ebitda(view: PitView) -> pl.DataFrame:
    bs = b.balance_sheet(view).with_columns(b.net_debt().alias("net_debt"))
    e = b.ttm(view, "ebitda")
    j = (b.market_cap(view).join(e, on="company_id").join(bs, on="company_id")
         .with_columns((pl.col("mcap") + pl.col("net_debt")).alias("ev")))
    j = j.with_columns(pl.when(pl.col("ebitda_ttm") > 0)
                       .then(pl.col("ev") / pl.col("ebitda_ttm")).alias("v"),
                       pl.concat_list("ids", "ids_right").alias("all_ids"))
    return _finish(view, "ev_ebitda", j, ["mcap", "net_debt", "ebitda_ttm"], "all_ids",
                   unfavourable=pl.col("ebitda_ttm") <= 0)


@factor("pb", "valuation", False, "market cap / equity attributable to owners; negative "
        "equity ranks with the dearest peers (unfavourable)")
def pb(view: PitView) -> pl.DataFrame:
    bs = b.balance_sheet(view).with_columns(pl.coalesce("equity_owners", "total_equity")
                                            .alias("eq"))
    j = b.market_cap(view).join(bs, on="company_id").with_columns(
        pl.when(pl.col("eq") > 0).then(pl.col("mcap") / pl.col("eq")).alias("v"))
    return _finish(view, "pb", j, ["mcap", "eq", "bs_date"], "ids",
                   unfavourable=pl.col("eq") <= 0)
