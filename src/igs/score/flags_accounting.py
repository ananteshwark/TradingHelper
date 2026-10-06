"""Accounting-quality checks, evaluated point in time from filed statements.

These look for the patterns that most often come before a listed company's
numbers turn out to be unreliable or its balance sheet fails: profits that do
not become cash, accruals building up, a distressed balance sheet, large cash
held alongside large debt while earning little, profits carried by exceptional
items, and restated history. None is proof of anything; each is a reason not to
vouch for a stock without reading its filings. Every result carries the numbers
and fact ids behind it. Banks, NBFCs and insurers are not applicable: their
cash flow and balance-sheet structure make these ratios meaningless.

Two published models are used as screens, with their documented inputs mapped
to Ind AS lines (see each function): the Altman Z'' score for emerging markets
and the Beneish M-score. Both were estimated on non-Indian data; their value
here is measured in the backtest's check-effectiveness table, not assumed.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from igs.factors import base as b
from igs.pit.view import FACT_KEY, PitView
from igs.score.flagbase import CLEAR, TRIPPED, cr, pct, pct_or_na, result, status_when

YEAR_TOLERANCE_DAYS = 40


def _financial_ids(view: PitView) -> set[int]:
    return set(b.financials(view)["company_id"].to_list())


def _year_pairs(view: PitView) -> pl.DataFrame:
    """Latest fiscal year (suffix none) and the one a year earlier (suffix _p) per company,
    both from fy_panel; only pairs about a year apart."""
    fy = b.fy_panel(view)
    cur = fy.sort("period_end").group_by("company_id").agg(pl.all().last())
    prev = fy.rename({c: f"{c}_p" for c in fy.columns if c != "company_id"})
    j = cur.join(prev, on="company_id")
    gap = (pl.col("period_end") - pl.col("period_end_p")).dt.total_days()
    return j.filter((gap - 365).abs() <= YEAR_TOLERANCE_DAYS)


# --------------------------------------------------------------------------- checks


def cash_not_converting(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Cumulative operating cash flow over the last `years` fiscal years is zero or negative
    while cumulative reported profit is positive."""
    years = int(cfg["years"])
    fy = b.fy_panel(view).filter(pl.col("cfo").is_not_null() & pl.col("pat").is_not_null())
    g = (fy.sort("period_end").group_by("company_id")
           .agg(pl.col("period_end").tail(years).first().alias("first"),
                pl.col("period_end").tail(years).last().alias("last"),
                pl.col("cfo").tail(years).sum().alias("cfo_sum"),
                pl.col("pat").tail(years).sum().alias("pat_sum"),
                pl.len().alias("n"), b.flat(pl.col("ids").tail(years)).alias("source_ids")))
    span = (pl.col("last") - pl.col("first")).dt.total_days()
    g = g.filter((pl.col("n") >= years)
                 & ((span - 365 * (years - 1)).abs() <= YEAR_TOLERANCE_DAYS * years))
    g = g.with_columns(
        status_when((pl.col("cfo_sum") <= 0) & (pl.col("pat_sum") > 0)),
        pl.format("operating cash flow Rs {} cr vs reported profit Rs {} cr over the {} fiscal "
                  "years to {}", cr(pl.col("cfo_sum")), cr(pl.col("pat_sum")), pl.lit(years),
                  pl.col("last")).alias("message"),
        pl.struct("cfo_sum", "pat_sum", "first", "last").alias("evidence"))
    return result("cash_not_converting", companies, g,
                  f"fewer than {years} consecutive fiscal years with operating cash flow and "
                  "profit filed", _financial_ids(view))


def accruals(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Sloan accruals: (profit - operating cash flow) / average total assets, latest year."""
    limit = cfg["max_pct_of_assets"] / 100
    p = _year_pairs(view).filter(pl.col("pat").is_not_null() & pl.col("cfo").is_not_null()
                                 & pl.col("total_assets").is_not_null())
    avg = pl.when(pl.col("total_assets_p").is_not_null()) \
        .then((pl.col("total_assets") + pl.col("total_assets_p")) / 2) \
        .otherwise(pl.col("total_assets"))
    p = p.with_columns(((pl.col("pat") - pl.col("cfo")) / avg).alias("ratio"))
    p = p.filter(pl.col("ratio").is_finite()).with_columns(
        status_when(pl.col("ratio") > limit),
        pl.format("accruals (profit minus operating cash flow) {} of average assets for the "
                  "year to {} (limit {})", pct(pl.col("ratio")), pl.col("period_end"),
                  pl.lit(f"{limit:.0%}")).alias("message"),
        pl.struct("pat", "cfo", "total_assets", "ratio").alias("evidence"),
        pl.col("ids").alias("source_ids"))
    return result("accruals", companies, p,
                  "no fiscal year with profit, operating cash flow and total assets filed",
                  _financial_ids(view))


def altman_distress(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Altman Z'' (emerging-market form, without the constant):
        6.56 X1 + 3.26 X2 + 6.72 X3 + 1.05 X4
    X1 = (current assets - current liabilities) / total assets
    X2 = retained earnings / total assets (Ind AS "other equity" as the proxy, else total
         equity minus share capital)
    X3 = TTM EBIT / total assets
    X4 = book equity / total liabilities (reported, else total assets minus equity)
    Below min_z (1.1 in Altman's zones) is the distress zone."""
    min_z = cfg["min_z"]
    bs = (b.bs_panel(view).filter(pl.col("total_assets") > 0)
            .sort("period_end").group_by("company_id").agg(pl.all().last()))
    ebit = b.ttm(view, "ebit").rename({"ids": "ids_ebit"})
    j = bs.join(ebit, on="company_id").filter(
        pl.col("current_assets").is_not_null() & pl.col("current_liabilities").is_not_null()
        & pl.col("total_equity").is_not_null())
    ta = pl.col("total_assets")
    retained = pl.coalesce("other_equity", pl.col("total_equity") - pl.col("share_capital"))
    liabilities = pl.coalesce("total_liabilities", ta - pl.col("total_equity"))
    j = j.with_columns(((pl.col("current_assets") - pl.col("current_liabilities")) / ta)
                       .alias("x1"), (retained / ta).alias("x2"),
                       (pl.col("ebit_ttm") / ta).alias("x3"),
                       (pl.col("total_equity") / liabilities).alias("x4"))
    j = j.with_columns((6.56 * pl.col("x1") + 3.26 * pl.col("x2") + 6.72 * pl.col("x3")
                        + 1.05 * pl.col("x4")).alias("z"))
    j = j.filter(pl.col("z").is_finite()).with_columns(
        status_when(pl.col("z") < min_z),
        pl.format("Altman Z'' {} (distress zone below {}) on the {} balance sheet",
                  pl.col("z").round(2), pl.lit(min_z), pl.col("period_end")).alias("message"),
        pl.struct("z", "x1", "x2", "x3", "x4", "period_end").alias("evidence"),
        pl.concat_list("ids", "ids_ebit").alias("source_ids"))
    return result("altman_distress", companies, j,
                  "latest balance sheet lacks current assets/liabilities or equity, or TTM EBIT "
                  "is unavailable", _financial_ids(view))


def _adjusted_shares_at(view: PitView) -> pl.DataFrame:
    """Total shares per shareholding filing date, restated to the as-of share basis
    (splits and bonuses with ex-date after the filing date and on or before as_of)."""
    if not view.has("shareholding"):
        return pl.DataFrame(schema={"company_id": pl.Int64, "period_end": pl.Date,
                                    "adj_shares": pl.Float64})
    s = (view.table("shareholding").filter(pl.col("category") == "total")
             .sort("filed_at").unique(["company_id", "period_end"], keep="last")
             .select("company_id", "period_end", "shares"))
    lp = b.primary_prices(view).select("company_id", "security_id").unique()
    if view.has("corporate_actions"):
        cas = view.table("corporate_actions").filter(
            pl.col("action_type").is_in(["split", "consolidation", "bonus"])
            & (pl.col("ex_date") <= view.as_of_date))
        mult = cas.select("security_id", "ex_date", pl.when(pl.col("action_type") == "bonus")
                          .then((pl.col("ratio_a") + pl.col("ratio_b")) / pl.col("ratio_b"))
                          .otherwise(pl.col("fv_old") / pl.col("fv_new")).alias("mult"))
        m = (s.join(lp, on="company_id", how="left").join(mult, on="security_id", how="left")
              .with_columns(pl.when(pl.col("ex_date") > pl.col("period_end"))
                            .then(pl.col("mult")).otherwise(1.0).fill_null(1.0).alias("mult"))
              .group_by("company_id", "period_end", "shares")
              .agg(pl.col("mult").product()))
        return m.select("company_id", "period_end", (pl.col("shares") * pl.col("mult"))
                        .alias("adj_shares"))
    return s.select("company_id", "period_end", pl.col("shares").alias("adj_shares"))


def _cogs(sfx: str = "") -> pl.Expr:
    parts = [pl.col(f"{c}{sfx}") for c in ("cost_of_materials", "purchases_stock_in_trade",
                                           "change_in_inventories")]
    return pl.when(pl.any_horizontal([p.is_not_null() for p in parts])) \
             .then(pl.sum_horizontal(parts))


def piotroski_weak(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Piotroski F-score (0-9) on the latest fiscal year vs the year before:
    ROA > 0, CFO > 0, ROA up, CFO > profit, long-term debt/assets not up, current ratio up,
    no share issuance (shares restated for splits/bonuses), gross margin up, asset turnover
    up. ROA uses year-end assets for both years. Cost of goods sold is the sum of the cost
    lines reported (materials, stock-in-trade purchases, inventory change). Scored on the
    components that can be computed; needs `min_components` of them."""
    max_score, min_components = cfg["max_score"], cfg["min_components"]
    p = _year_pairs(view)
    sh = _adjusted_shares_at(view)
    p = (p.join(sh.rename({"adj_shares": "sh"}), on=["company_id", "period_end"], how="left")
          .join(sh.rename({"period_end": "period_end_p", "adj_shares": "sh_p"}),
                on=["company_id", "period_end_p"], how="left"))

    def ratio(a: str, b_: str, sfx: str = "") -> pl.Expr:
        return pl.when(pl.col(f"{b_}{sfx}") > 0).then(pl.col(f"{a}{sfx}") / pl.col(f"{b_}{sfx}"))

    lev = lambda s: pl.when(pl.col(f"total_assets{s}") > 0).then(  # noqa: E731
        pl.col(f"borrowings_noncurrent{s}").fill_null(0) / pl.col(f"total_assets{s}"))
    gm = lambda s: pl.when(pl.col(f"revenue{s}") > 0).then(  # noqa: E731
        (pl.col(f"revenue{s}") - _cogs(s)) / pl.col(f"revenue{s}"))
    tests = {
        "roa_positive": ratio("pat", "total_assets") > 0,
        "cfo_positive": pl.col("cfo") > 0,
        "roa_up": ratio("pat", "total_assets") > ratio("pat", "total_assets", "_p"),
        "cfo_above_profit": pl.col("cfo") > pl.col("pat"),
        "leverage_not_up": lev("") <= lev("_p"),
        "current_ratio_up": ratio("current_assets", "current_liabilities")
        > ratio("current_assets", "current_liabilities", "_p"),
        "no_share_issue": pl.col("sh") <= pl.col("sh_p") * 1.005,
        "gross_margin_up": gm("") > gm("_p"),
        "asset_turnover_up": ratio("revenue", "total_assets")
        > ratio("revenue", "total_assets", "_p"),
    }
    p = p.with_columns(*[e.cast(pl.Int8).alias(f"t_{k}") for k, e in tests.items()])
    tcols = [f"t_{k}" for k in tests]
    p = p.with_columns(pl.sum_horizontal(tcols).alias("score"),
                       pl.sum_horizontal([pl.col(c).is_not_null() for c in tcols])
                       .alias("components"))
    p = p.filter(pl.col("components") >= min_components).with_columns(
        status_when(pl.col("score") * 9 <= max_score * pl.col("components")),
        pl.format("Piotroski F-score {} of {} computable (weak at or below {} of 9) for the year "
                  "to {}", pl.col("score"), pl.col("components"), pl.lit(max_score),
                  pl.col("period_end")).alias("message"),
        pl.struct("score", "components", *tcols).alias("evidence"),
        pl.concat_list("ids", "ids_p").alias("source_ids"))
    return result("piotroski_weak", companies, p,
                  f"fewer than {min_components} of the 9 F-score components computable from two "
                  "consecutive fiscal years", _financial_ids(view))


BENEISH = {"const": -4.84, "dsri": 0.920, "gmi": 0.528, "aqi": 0.404, "sgi": 0.892,
           "depi": 0.115, "sgai": -0.172, "tata": 4.679, "lvgi": -0.327}


def beneish_manipulation(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Beneish (1999) eight-variable M-score on the latest fiscal year vs the one before.
    Inputs mapped to Ind AS lines: receivables = trade receivables; COGS = reported cost
    lines; securities = non-current investments (0 when not reported); SG&A proxy =
    employee benefit + other expenses (Ind AS has no SG&A line); long-term debt =
    non-current borrowings. Above `threshold` (-1.78 in Beneish's paper) is flagged."""
    thr = cfg["threshold"]
    p = _year_pairs(view)

    def r(num: pl.Expr, den: pl.Expr) -> pl.Expr:
        return pl.when(den != 0).then(num / den)

    def s(c: str, sfx: str) -> pl.Expr:
        return pl.col(f"{c}{sfx}")

    def gm(x: str) -> pl.Expr:
        return r(s("revenue", x) - _cogs(x), s("revenue", x))

    def aq(x: str) -> pl.Expr:
        return 1 - r(s("current_assets", x) + s("ppe", x)
                     + s("noncurrent_investments", x).fill_null(0), s("total_assets", x))

    def dep(x: str) -> pl.Expr:
        return r(s("depreciation", x), s("depreciation", x) + s("ppe", x))

    def sga(x: str) -> pl.Expr:
        return r(s("employee_expense", x) + s("other_expenses", x), s("revenue", x))

    def lv(x: str) -> pl.Expr:
        return r(s("current_liabilities", x) + s("borrowings_noncurrent", x).fill_null(0),
                 s("total_assets", x))

    p = p.with_columns(
        r(r(s("trade_receivables", ""), s("revenue", "")),
          r(s("trade_receivables", "_p"), s("revenue", "_p"))).alias("dsri"),
        r(gm("_p"), gm("")).alias("gmi"), r(aq(""), aq("_p")).alias("aqi"),
        r(s("revenue", ""), s("revenue", "_p")).alias("sgi"),
        r(dep("_p"), dep("")).alias("depi"), r(sga(""), sga("_p")).alias("sgai"),
        r(s("pat", "") - s("cfo", ""), s("total_assets", "")).alias("tata"),
        r(lv(""), lv("_p")).alias("lvgi"))
    comps = [k for k in BENEISH if k != "const"]
    p = p.with_columns((BENEISH["const"] + pl.sum_horizontal(
        [BENEISH[k] * pl.col(k) for k in comps])).alias("m"))
    p = p.filter(pl.all_horizontal([pl.col(k).is_not_null() & pl.col(k).is_finite()
                                    for k in comps])).with_columns(
        status_when(pl.col("m") > thr),
        pl.format("Beneish M-score {} (flag above {}) for the year to {}", pl.col("m").round(2),
                  pl.lit(thr), pl.col("period_end")).alias("message"),
        pl.struct("m", *comps).alias("evidence"),
        pl.concat_list("ids", "ids_p").alias("source_ids"))
    return result("beneish_manipulation", companies, p,
                  "two consecutive fiscal years with every M-score input were not filed",
                  _financial_ids(view))


def cash_debt_paradox(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Large cash and investments held alongside large borrowings while the cash earns
    little: other income over the last four quarters / average cash-like assets below
    max_cash_yield_pct. A company that reports no borrowing lines is clear."""
    min_cash, min_debt = cfg["min_cash_pct_assets"] / 100, cfg["min_debt_pct_assets"] / 100
    max_yield = cfg["max_cash_yield_pct"] / 100
    bs = b.balance_sheet(view).filter((pl.col("total_assets") > 0)
                                     & pl.col("cash").is_not_null())
    oi = b.ttm(view, "other_income").rename({"ids": "ids_oi"})
    cash = pl.col("cash") + pl.col("bank_balances").fill_null(0) \
        + pl.col("current_investments").fill_null(0)
    cash_p = pl.col("cash_prev") + pl.col("bank_balances_prev").fill_null(0) \
        + pl.col("current_investments_prev").fill_null(0)
    debt = pl.col("borrowings_noncurrent").fill_null(0) + pl.col("borrowings_current").fill_null(0)
    j = bs.join(oi, on="company_id", how="left").with_columns(
        cash.alias("cash_like"), debt.alias("debt"),
        pl.when(pl.col("cash_prev").is_not_null()).then((cash + cash_p) / 2).otherwise(cash)
          .alias("avg_cash"),
        (pl.col("borrowings_noncurrent").is_not_null()
         | pl.col("borrowings_current").is_not_null()).alias("debt_reported"))
    j = j.with_columns((pl.col("cash_like") / pl.col("total_assets")).alias("cash_share"),
                       (pl.col("debt") / pl.col("total_assets")).alias("debt_share"),
                       pl.when(pl.col("avg_cash") > 0)
                         .then(pl.col("other_income_ttm") / pl.col("avg_cash")).alias("yld"))
    big_both = (pl.col("cash_share") >= min_cash) & (pl.col("debt_share") >= min_debt)
    # Evaluable when the answer does not depend on a missing number.
    j = j.filter(~pl.col("debt_reported") | ~big_both | pl.col("yld").is_not_null())
    j = j.with_columns(
        pl.when(~pl.col("debt_reported")).then(pl.lit(CLEAR))
          .when(big_both & (pl.col("yld") < max_yield)).then(pl.lit(TRIPPED))
          .otherwise(pl.lit(CLEAR)).alias("status"),
        pl.when(~pl.col("debt_reported")).then(pl.lit("no borrowings reported"))
          .otherwise(pl.format("cash and investments {} of assets, borrowings {}, other income "
                               "{} of average cash (flag: both above {} / {} and yield below "
                               "{})", pct(pl.col("cash_share")), pct(pl.col("debt_share")),
                               pct_or_na(pl.col("yld")),
                               pl.lit(f"{min_cash:.0%}"), pl.lit(f"{min_debt:.0%}"),
                               pl.lit(f"{max_yield:.0%}"))).alias("message"),
        pl.struct("cash_like", "debt", "total_assets", "yld", "bs_date").alias("evidence"),
        pl.concat_list(pl.col("ids"), pl.col("ids_oi").fill_null(pl.lit([], dtype=b.IDS)))
          .alias("source_ids"))
    return result("cash_debt_paradox", companies, j,
                  "no balance sheet with total assets and cash, or other income unavailable",
                  _financial_ids(view))


def exceptional_items(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Exceptional items over the last four quarters as a share of PBT. Taken from the
    exceptional-items line, else as PBT minus PBT before exceptional items; all four
    quarters must report one of them."""
    limit = cfg["max_pct_of_pbt"] / 100
    f = view.facts(concepts=["exceptional_items", "pbt_before_exceptional", "pbt"]).filter(
        pl.col("period_type") == "Q").join(b.basis_choice(view),
                                           on=["company_id", "statement_basis"])
    if f.height == 0:
        return result("exceptional_items", companies, pl.DataFrame(
            schema={"company_id": pl.Int64, "status": pl.Utf8, "message": pl.Utf8}),
            "exceptional items not reported", _financial_ids(view))
    w = f.pivot(on="concept", index=["company_id", "period_end"], values="value",
                aggregate_function="first")
    ids = f.group_by("company_id", "period_end").agg(pl.col("fact_id").alias("ids"))
    for c in ("exceptional_items", "pbt_before_exceptional", "pbt"):
        if c not in w.columns:
            w = w.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    w = w.join(ids, on=["company_id", "period_end"]).with_columns(
        pl.coalesce("exceptional_items", pl.col("pbt") - pl.col("pbt_before_exceptional"))
        .alias("exc"))
    lq = b.latest_quarter(view)
    w = (w.join(lq, on="company_id")
          .with_columns(b._qidx().alias("qidx"))
          .filter(pl.col("qidx") > pl.col("last_q") - 4))
    g = (w.group_by("company_id")
          .agg(pl.col("exc").count().alias("n"), pl.col("exc").sum().alias("exc_ttm"),
               pl.col("pbt").sum().alias("pbt_ttm"), pl.col("pbt").count().alias("n_pbt"),
               b.flat(pl.col("ids")).alias("source_ids"))
          .filter((pl.col("n") == 4) & (pl.col("n_pbt") == 4)))
    share = pl.when(pl.col("pbt_ttm").abs() > 0).then(pl.col("exc_ttm").abs()
                                                       / pl.col("pbt_ttm").abs())
    g = g.with_columns(share.alias("share")).with_columns(
        status_when((pl.col("share") > limit) | (pl.col("share").is_null()
                                                  & (pl.col("exc_ttm") != 0))),
        pl.format("exceptional items Rs {} cr over four quarters, {} of PBT (limit {})",
                  cr(pl.col("exc_ttm")), pct_or_na(pl.col("share")),
                  pl.lit(f"{limit:.0%}")).alias("message"),
        pl.struct("exc_ttm", "pbt_ttm").alias("evidence"))
    return result("exceptional_items", companies, g,
                  "exceptional items not reported for all of the last four quarters",
                  _financial_ids(view))


def restatement(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """A previously filed revenue or profit figure changed by more than min_change_pct in a
    filing made within lookback_days. Restatements are kept as new rows (never
    overwritten), so both versions are shown."""
    concepts = cfg["concepts"]
    min_change = cfg["min_change_pct"] / 100
    since = view.as_of - dt.timedelta(days=cfg["lookback_days"])
    if not view.has("facts"):
        return result("restatement", companies, pl.DataFrame(
            schema={"company_id": pl.Int64, "status": pl.Utf8, "message": pl.Utf8}),
            "no filings loaded")
    f = view.table("facts").filter(pl.col("concept").is_in(concepts)
                                   & pl.col("period_type").is_in(["Q", "FY"]))
    v = (f.sort("filed_at", "fact_id").group_by(FACT_KEY)
          .agg(pl.col("value").first().alias("first_value"),
               pl.col("value").last().alias("last_value"),
               pl.col("filed_at").last().alias("restated_at"),
               pl.col("fact_id").first().alias("id_first"),
               pl.col("fact_id").last().alias("id_last"), pl.len().alias("versions")))
    v = v.filter((pl.col("versions") > 1) & (pl.col("restated_at") >= since)
                 & (pl.col("first_value") != 0))
    v = v.with_columns((pl.col("last_value") / pl.col("first_value") - 1).alias("change"))
    hits = (v.filter(pl.col("change").abs() >= min_change)
             .sort(pl.col("change").abs(), descending=True)
             .group_by("company_id").agg(pl.all().first(), pl.len().alias("n_restated")))
    hits = hits.with_columns(
        pl.lit(TRIPPED).alias("status"),
        pl.format("{} for the period ended {} restated from Rs {} cr to Rs {} cr ({}) in a "
                  "filing on {}; {} restated figure(s) in {} days",
                  pl.col("concept"), pl.col("period_end"), cr(pl.col("first_value")),
                  cr(pl.col("last_value")), pct(pl.col("change")),
                  pl.col("restated_at").dt.convert_time_zone("Asia/Kolkata").dt.date(),
                  pl.col("n_restated"), pl.lit(cfg["lookback_days"])).alias("message"),
        pl.struct("concept", "period_end", "first_value", "last_value", "change")
          .alias("evidence"),
        pl.concat_list("id_first", "id_last").alias("source_ids"))
    known = f.select("company_id").unique().join(hits.select("company_id"), on="company_id",
                                                 how="anti")
    clean = known.with_columns(pl.lit(CLEAR).alias("status"), pl.lit(
        f"no revenue or profit figure restated by {min_change:.0%} or more in "
        f"{cfg['lookback_days']} days").alias("message"))
    both = pl.concat([hits.select("company_id", "status", "message", "evidence",
                                  "source_ids").with_columns(
                          pl.col("evidence").struct.json_encode()),
                      clean.with_columns(pl.lit("{}").alias("evidence"),
                                         pl.lit([], dtype=b.IDS).alias("source_ids"))],
                     how="vertical_relaxed")
    return result("restatement", companies, both, "no revenue or profit filings loaded")
