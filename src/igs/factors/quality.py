"""Quality pillar. Balance-sheet items come from the latest statement of assets
and liabilities filed by the as-of date (half-yearly for most companies) and
the one closest to a year earlier for averages and trends."""

from __future__ import annotations

import polars as pl

from igs.factors import base as b
from igs.factors.registry import factor
from igs.pit.view import PitView

MAX_COVERAGE = 100.0   # debt-free companies: coverage capped, not infinite


def _avg(cur: str, prev: str) -> pl.Expr:
    return pl.when(pl.col(prev).is_not_null()).then((pl.col(cur) + pl.col(prev)) / 2) \
             .otherwise(pl.col(cur))


def _nonfin(view: PitView, df: pl.DataFrame, value: str, detail: list[str],
            ids: str) -> pl.DataFrame:
    return b.finish(df, value, detail, ids, universe=b.companies(view),
                    not_applicable=b.financials(view))


@factor("roce", "quality", True,
        "TTM EBIT / average capital employed (equity + borrowings); not for financials")
def roce(view: PitView) -> pl.DataFrame:
    bs = b.balance_sheet(view)
    ebit = b.ttm(view, "ebit")
    cap = bs.with_columns(
        (pl.col("total_equity") + pl.col("borrowings_noncurrent").fill_null(0)
         + pl.col("borrowings_current").fill_null(0)).alias("ce"),
        (pl.col("total_equity_prev") + pl.col("borrowings_noncurrent_prev").fill_null(0)
         + pl.col("borrowings_current_prev").fill_null(0)).alias("ce_prev"))
    j = ebit.join(cap, on="company_id").with_columns(_avg("ce", "ce_prev").alias("avg_ce"))
    j = j.with_columns(pl.when(pl.col("avg_ce") > 0).then(pl.col("ebit_ttm") / pl.col("avg_ce"))
                       .alias("v"),
                       pl.concat_list("ids", "ids_right").alias("all_ids"))
    return _nonfin(view, j, "v", ["ebit_ttm", "avg_ce", "bs_date"], "all_ids")


@factor("roe", "quality", True,
        "TTM profit to owners / average owners' equity; detail carries the DuPont split")
def roe(view: PitView) -> pl.DataFrame:
    bs = b.balance_sheet(view).with_columns(
        pl.coalesce("equity_owners", "total_equity").alias("eq"),
        pl.coalesce("equity_owners_prev", "total_equity_prev").alias("eq_prev"))
    pat = b.ttm(view, "pat")
    rev = b.ttm(view, "top_line").rename({"ids": "ids_rev"})
    j = (pat.join(rev, on="company_id").join(bs, on="company_id")
            .with_columns(_avg("eq", "eq_prev").alias("avg_eq"),
                          _avg("total_assets", "total_assets_prev").alias("avg_assets")))
    j = j.with_columns(
        pl.when(pl.col("avg_eq") > 0).then(pl.col("pat_ttm") / pl.col("avg_eq")).alias("v"),
        (pl.col("pat_ttm") / pl.col("top_line_ttm")).alias("net_margin"),
        (pl.col("top_line_ttm") / pl.col("avg_assets")).alias("asset_turnover"),
        (pl.col("avg_assets") / pl.col("avg_eq")).alias("equity_multiplier"),
        pl.concat_list("ids", "ids_rev", "ids_right").alias("all_ids"))
    return b.finish(j, "v", ["pat_ttm", "avg_eq", "net_margin", "asset_turnover",
                             "equity_multiplier"], "all_ids", universe=b.companies(view))


@factor("opm_level", "quality", True, "TTM EBITDA / TTM revenue; not for financials")
def opm_level(view: PitView) -> pl.DataFrame:
    e = b.ttm(view, "ebitda")
    r = b.ttm(view, "top_line").rename({"ids": "ids_rev"})
    j = e.join(r, on="company_id").with_columns(
        pl.when(pl.col("top_line_ttm") > 0)
          .then(pl.col("ebitda_ttm") / pl.col("top_line_ttm")).alias("v"),
        pl.concat_list("ids", "ids_rev").alias("all_ids"))
    return _nonfin(view, j, "v", ["ebitda_ttm", "top_line_ttm"], "all_ids")


@factor("opm_trend_8q", "quality", True,
        "OLS slope of quarterly operating margin over the last 8 quarters (per quarter)")
def opm_trend_8q(view: PitView) -> pl.DataFrame:
    q = b.quarterly(view).join(b.latest_quarter(view), on="company_id")
    q = q.filter((pl.col("qidx") > pl.col("last_q") - 8) & (pl.col("top_line") > 0)
                 & pl.col("ebitda").is_not_null())
    q = q.with_columns((pl.col("ebitda") / pl.col("top_line")).alias("opm"),
                       (pl.col("qidx") - pl.col("last_q")).cast(pl.Float64).alias("t"))
    g = (q.group_by("company_id")
          .agg(pl.len().alias("n"),
               ((pl.col("t") - pl.col("t").mean()) * (pl.col("opm") - pl.col("opm").mean()))
               .sum().alias("sxy"),
               ((pl.col("t") - pl.col("t").mean()) ** 2).sum().alias("sxx"),
               pl.col("opm").last().alias("opm_latest"),
               b.flat(pl.concat_list("ids_ebitda", "ids_top_line")).alias("ids"))
          .filter(pl.col("n") == 8)
          .with_columns((pl.col("sxy") / pl.col("sxx")).alias("v")))
    return _nonfin(view, g, "v", ["opm_latest", "n"], "ids")


@factor("cash_conversion_3y", "quality", True,
        "sum of operating cash flow / sum of EBITDA over the last 3 fiscal years")
def cash_conversion_3y(view: PitView) -> pl.DataFrame:
    a = b.annual(view).filter(pl.col("cfo").is_not_null() & pl.col("ebitda").is_not_null())
    a = a.sort("period_end").group_by("company_id").agg(
        pl.col("cfo").tail(3).sum().alias("cfo_3y"), pl.col("ebitda").tail(3).sum()
        .alias("ebitda_3y"), pl.len().alias("n"), b.flat(pl.col("ids").tail(3)).alias("ids"))
    a = a.filter(pl.col("n") >= 3).with_columns(
        pl.when(pl.col("ebitda_3y") > 0).then(pl.col("cfo_3y") / pl.col("ebitda_3y")).alias("v"))
    return _nonfin(view, a, "v", ["cfo_3y", "ebitda_3y"], "ids")


def _net_debt(bs: pl.DataFrame) -> pl.DataFrame:
    return bs.with_columns(
        (pl.col("borrowings_noncurrent").fill_null(0) + pl.col("borrowings_current").fill_null(0)
         - pl.col("cash").fill_null(0) - pl.col("bank_balances").fill_null(0)
         - pl.col("current_investments").fill_null(0)).alias("net_debt"))


@factor("net_debt_to_ebitda", "quality", False,
        "(borrowings - cash & equivalents - current investments) / TTM EBITDA; undefined when "
        "EBITDA is not positive; not for financials")
def net_debt_to_ebitda(view: PitView) -> pl.DataFrame:
    bs = _net_debt(b.balance_sheet(view))
    e = b.ttm(view, "ebitda")
    j = e.join(bs, on="company_id").with_columns(
        pl.when(pl.col("ebitda_ttm") > 0).then(pl.col("net_debt") / pl.col("ebitda_ttm"))
          .alias("v"),
        pl.concat_list("ids", "ids_right").alias("all_ids"))
    return _nonfin(view, j, "v", ["net_debt", "ebitda_ttm", "bs_date"], "all_ids")


@factor("interest_coverage", "quality", True,
        f"TTM EBIT / TTM finance costs, capped at {MAX_COVERAGE:g}; not for financials")
def interest_coverage(view: PitView) -> pl.DataFrame:
    ebit = b.ttm(view, "ebit")
    fc = b.quarterly(view).join(b.latest_quarter(view), on="company_id").filter(
        (pl.col("qidx") > pl.col("last_q") - 4) & pl.col("finance_costs").is_not_null()
    ).group_by("company_id").agg(pl.col("finance_costs").sum().alias("fc_ttm"),
                                 pl.len().alias("n")).filter(pl.col("n") == 4)
    j = ebit.join(fc, on="company_id").with_columns(
        pl.when(pl.col("fc_ttm") <= 0).then(MAX_COVERAGE)
          .otherwise(pl.min_horizontal(pl.col("ebit_ttm") / pl.col("fc_ttm"),
                                       pl.lit(MAX_COVERAGE))).alias("v"))
    return _nonfin(view, j, "v", ["ebit_ttm", "fc_ttm"], "ids")


@factor("working_capital_days_trend", "quality", False,
        "change in (inventory + receivables - payables) days vs a year earlier; lower is "
        "better; not for financials")
def working_capital_days_trend(view: PitView) -> pl.DataFrame:
    bs = b.balance_sheet(view)
    rev = b.ttm(view, "top_line")
    rev_prev = b.ttm(view, "top_line", 4).rename({"top_line_ttm": "rev_prev",
                                                  "ids": "ids_rev_prev"})
    wc = pl.col("inventories").fill_null(0) + pl.col("trade_receivables").fill_null(0) \
        - pl.col("trade_payables").fill_null(0)
    wc_prev = pl.col("inventories_prev").fill_null(0) \
        + pl.col("trade_receivables_prev").fill_null(0) - pl.col("trade_payables_prev").fill_null(0)
    j = (rev.join(rev_prev, on="company_id").join(bs, on="company_id")
            .filter(pl.col("bs_date_prev").is_not_null())
            .with_columns((wc / pl.col("top_line_ttm") * 365).alias("wc_days"),
                          (wc_prev / pl.col("rev_prev") * 365).alias("wc_days_prev")))
    j = j.with_columns((pl.col("wc_days") - pl.col("wc_days_prev")).alias("v"),
                       pl.concat_list("ids", "ids_rev_prev", "ids_right", "ids_prev")
                       .list.drop_nulls().alias("all_ids"))
    return _nonfin(view, j, "v", ["wc_days", "wc_days_prev", "bs_date"], "all_ids")
