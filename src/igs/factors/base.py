"""Shared point-in-time building blocks for factors.

Everything here takes a PitView and returns frames derived only from what the
view exposes. Derived panels are memoised on the view (one as_of), never
across dates.

Output contract for every factor (see `finish`):
    company_id        Int64
    value             Float64   null unless status == "ok"
    status            Utf8      ok | not_applicable | insufficient_data
    detail            Utf8      JSON of the raw inputs behind the value
    source_fact_ids   List(Int64) fundamental facts used (for traceability)
Nothing is imputed: a company without the inputs gets insufficient_data.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from igs.pit.view import PitView

OK, NA, INSUFFICIENT = "ok", "not_applicable", "insufficient_data"
FINANCIAL_MODULES = ("bank", "nbfc", "insurance")
# Results forms recorded on a filing (xbrl.results.results_form) and the module each means.
FORM_MODULES = {"default": "default", "bank": "bank", "nbfc": "nbfc"}
MAX_QUARTER_AGE_DAYS = 200     # latest quarter older than this is stale
RESULT_SCHEMA = {"company_id": pl.Int64, "value": pl.Float64, "status": pl.Utf8,
                 "detail": pl.Utf8, "source_fact_ids": pl.List(pl.Int64)}

# Basic-industry names (NSE classification) that select sector modules.
# Kept here as the default; scoring.yaml may override via sector_modules.
DEFAULT_SECTOR_MODULES = {
    "bank": ["Private Sector Bank", "Public Sector Bank", "Other Bank"],
    "nbfc": ["Non Banking Financial Company (NBFC)", "Housing Finance Company",
             "Microfinance Institutions", "Investment Company", "Financial Institution"],
    "insurance": ["Life Insurance", "General Insurance"],
}


IDS = pl.List(pl.Int64)
QUARTERLY_SCHEMA = {"company_id": pl.Int64, "period_end": pl.Date, "qidx": pl.Int32,
                    "module": pl.Utf8, "top_line": pl.Float64, "ebitda": pl.Float64,
                    "ebit": pl.Float64, "finance_costs": pl.Float64, "pat": pl.Float64,
                    "other_income": pl.Float64, "pbt": pl.Float64,
                    "ids_top_line": IDS, "ids_ebitda": IDS, "ids_ebit": IDS, "ids_pat": IDS,
                    "ids_other_income": IDS, "ids_pbt": IDS}


# --------------------------------------------------------------------------- output


def empty() -> pl.DataFrame:
    return pl.DataFrame(schema=RESULT_SCHEMA)


def finish(df: pl.DataFrame, value: str, detail_cols: list[str], ids: str | None = None,
           universe: pl.DataFrame | None = None, not_applicable: pl.DataFrame | None = None
           ) -> pl.DataFrame:
    """Shape a per-company frame into the factor output contract.

    Rows with a null/non-finite value become insufficient_data. Companies in
    `universe` missing from df are insufficient_data; companies in
    `not_applicable` are not_applicable.
    """
    detail = (pl.struct([pl.col(c) for c in detail_cols]).struct.json_encode()
              if detail_cols else pl.lit("{}"))
    out = df.select(
        pl.col("company_id").cast(pl.Int64),
        pl.col(value).cast(pl.Float64).alias("value"),
        detail.alias("detail"),
        (pl.col(ids) if ids else pl.lit([], dtype=pl.List(pl.Int64)))
        .cast(pl.List(pl.Int64)).alias("source_fact_ids"),
    )
    ok = pl.col("value").is_not_null() & pl.col("value").is_finite()
    out = out.with_columns(pl.when(ok).then(pl.lit(OK)).otherwise(pl.lit(INSUFFICIENT))
                           .alias("status"),
                           pl.when(ok).then(pl.col("value")).otherwise(None).alias("value"))
    frames = [out.select(list(RESULT_SCHEMA))]
    if universe is not None:
        missing = universe.select("company_id").unique().join(out.select("company_id"),
                                                              on="company_id", how="anti")
        frames.append(_status_rows(missing, INSUFFICIENT))
    result = pl.concat(frames)
    if not_applicable is not None and not_applicable.height:
        na_ids = not_applicable.select("company_id").unique()
        result = pl.concat([result.join(na_ids, on="company_id", how="anti"),
                            _status_rows(na_ids, NA)])
    return result.sort("company_id")


def _status_rows(ids: pl.DataFrame, status: str) -> pl.DataFrame:
    return ids.select(pl.col("company_id").cast(pl.Int64),
                      pl.lit(None, dtype=pl.Float64).alias("value"),
                      pl.lit(status).alias("status"), pl.lit("{}").alias("detail"),
                      pl.lit([], dtype=pl.List(pl.Int64)).alias("source_fact_ids"))


# --------------------------------------------------------------------------- companies


def companies(view: PitView) -> pl.DataFrame:
    """Every company the view knows about through facts or prices."""
    def build() -> pl.DataFrame:
        parts = [view.facts().select("company_id")]
        if view.has("prices"):
            parts.append(view.table("prices").select("company_id"))
        return pl.concat(parts).unique().sort("company_id")
    return view.memo("companies", build)


def modules(view: PitView,
            sector_modules: dict[str, list[str]] | None = None) -> pl.DataFrame:
    """company_id -> module (default | bank | nbfc | insurance).

    From the latest known NSE basic-industry classification; where there is
    none, from the form recorded on the latest results filing (xbrl.results.
    results_form), else from the line items: a bank reports interest earned and
    no revenue from operations (NBFCs report both), an NBFC reports impairment
    on financial instruments. Never guessed from the name.
    """
    mapping = sector_modules or DEFAULT_SECTOR_MODULES

    def build() -> pl.DataFrame:
        base = companies(view).with_columns(pl.lit("default").alias("module"))
        if view.has("industry"):
            ind = (view.table("industry").sort("valid_from")
                       .group_by("company_id").agg(pl.col("basic_industry").last()))
            lookup = {bi: mod for mod, names in mapping.items() for bi in names}
            ind = ind.with_columns(pl.col("basic_industry").replace_strict(
                lookup, default=None).alias("by_industry"))
            base = base.join(ind.select("company_id", "by_industry"), on="company_id",
                             how="left")
        else:
            base = base.with_columns(pl.lit(None, dtype=pl.Utf8).alias("by_industry"))
        if view.has("filings"):
            filed = (view.table("filings")
                     .filter((pl.col("filing_type") == "financial_results")
                             & pl.col("results_format").is_in(list(FORM_MODULES)))
                     .sort("period_end", "filed_at")
                     .group_by("company_id").agg(pl.col("results_format").last())
                     .select("company_id", pl.col("results_format")
                             .replace_strict(FORM_MODULES).alias("by_filing")))
            base = base.join(filed, on="company_id", how="left")
        else:
            base = base.with_columns(pl.lit(None, dtype=pl.Utf8).alias("by_filing"))
        f = view.facts(concepts=["interest_earned", "interest_income", "revenue",
                                 "impairment_on_financial_instruments"])
        shape = (f.group_by("company_id")
                  .agg((_has("interest_earned") & ~_has("revenue")).alias("bank_fmt"),
                       (_has("interest_income") | _has("impairment_on_financial_instruments"))
                       .alias("nbfc_fmt")))
        base = base.join(shape, on="company_id", how="left")
        return base.select("company_id", pl.coalesce(
            "by_industry", "by_filing",
            pl.when(pl.col("bank_fmt")).then(pl.lit("bank"))
              .when(pl.col("nbfc_fmt")).then(pl.lit("nbfc")),
            pl.lit("default")).alias("module"))
    return view.memo("modules", build)


def _has(concept: str) -> pl.Expr:
    return (pl.col("concept") == concept).any()


def financials(view: PitView) -> pl.DataFrame:
    return modules(view).filter(pl.col("module").is_in(FINANCIAL_MODULES))


# --------------------------------------------------------------------------- fundamentals


def _qidx(col: str = "period_end") -> pl.Expr:
    return pl.col(col).dt.year() * 4 + (pl.col(col).dt.month() - 1) // 3


def basis_choice(view: PitView) -> pl.DataFrame:
    """Consolidated where the company files consolidated quarterly results, else standalone."""
    def build() -> pl.DataFrame:
        q = view.facts().filter(pl.col("period_type") == "Q")
        return (q.group_by("company_id")
                 .agg((pl.col("statement_basis") == "consolidated").any().alias("has_cons"))
                 .select("company_id", pl.when(pl.col("has_cons")).then(pl.lit("consolidated"))
                         .otherwise(pl.lit("standalone")).alias("statement_basis")))
    return view.memo("basis", build)


def _wide(view: PitView, period_type: str, key: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    def build():
        f = (view.facts().filter(pl.col("period_type") == period_type)
                 .join(basis_choice(view), on=["company_id", "statement_basis"]))
        idx = ["company_id", "period_end"]
        if f.height == 0:
            e = pl.DataFrame(schema={"company_id": pl.Int64, "period_end": pl.Date})
            return e, e
        vals = f.pivot(on="concept", index=idx, values="value", aggregate_function="first")
        ids = f.pivot(on="concept", index=idx, values="fact_id", aggregate_function="first")
        return vals.sort(idx), ids.sort(idx)
    return view.memo(key, build)


def _col(df: pl.DataFrame, name: str) -> pl.Expr:
    return pl.col(name) if name in df.columns else pl.lit(None, dtype=pl.Float64)


def _ids(df: pl.DataFrame, names: list[str]) -> pl.Expr:
    cols = [pl.col(n) for n in names if n in df.columns]
    if not cols:
        return pl.lit([], dtype=pl.List(pl.Int64))
    return pl.concat_list(cols).list.drop_nulls()


def quarterly(view: PitView) -> pl.DataFrame:
    """One row per company-quarter with the measures factors need, and the fact ids.

    top_line : revenue from operations; interest earned for bank-format filers
    ebitda   : revenue - total expenses + finance costs + depreciation;
               for financials pre-provision profit (PBT + provisions/impairment)
    ebit     : PBT + finance costs (non-financials)
    pat      : profit attributable to owners, else profit for the period
    """
    def build() -> pl.DataFrame:
        v, i = _wide(view, "Q", "wide_q")
        if v.height == 0:
            return pl.DataFrame(schema=QUARTERLY_SCHEMA)
        mods = modules(view)
        v = v.join(mods, on="company_id", how="left")
        fin = pl.col("module").is_in(FINANCIAL_MODULES)
        ebitda_nonfin = (_col(v, "revenue") - _col(v, "total_expenses")
                         + _col(v, "finance_costs") + _col(v, "depreciation"))
        ppop = _col(v, "pbt") + pl.coalesce(_col(v, "provisions"),
                                            _col(v, "impairment_on_financial_instruments"))
        out = v.select(
            "company_id", "period_end", _qidx().cast(pl.Int32).alias("qidx"), "module",
            pl.coalesce(_col(v, "revenue"), _col(v, "interest_earned")).alias("top_line"),
            pl.when(fin).then(ppop).otherwise(ebitda_nonfin).alias("ebitda"),
            pl.when(fin).then(None).otherwise(_col(v, "pbt") + _col(v, "finance_costs"))
              .alias("ebit"),
            _col(v, "finance_costs").alias("finance_costs"),
            pl.coalesce(_col(v, "pat_owners"), _col(v, "pat")).alias("pat"),
            _col(v, "other_income").alias("other_income"),
            _col(v, "pbt").alias("pbt"),
        )
        ids = i.select("company_id", "period_end",
                       _ids(i, ["revenue", "interest_earned"]).alias("ids_top_line"),
                       _ids(i, ["revenue", "total_expenses", "finance_costs", "depreciation",
                                "pbt", "provisions", "impairment_on_financial_instruments"])
                       .alias("ids_ebitda"),
                       _ids(i, ["pbt", "finance_costs"]).alias("ids_ebit"),
                       _ids(i, ["pat_owners", "pat"]).alias("ids_pat"),
                       _ids(i, ["other_income"]).alias("ids_other_income"),
                       _ids(i, ["pbt"]).alias("ids_pbt"))
        return out.join(ids, on=["company_id", "period_end"]).sort("company_id", "qidx")
    return view.memo("quarterly", build)


def latest_quarter(view: PitView) -> pl.DataFrame:
    """company_id -> qidx of the latest reported quarter, if not stale."""
    def build() -> pl.DataFrame:
        q = quarterly(view).filter(pl.col("top_line").is_not_null())
        cutoff = view.as_of_date - dt.timedelta(days=MAX_QUARTER_AGE_DAYS)
        return (q.group_by("company_id").agg(pl.col("qidx").max(),
                                             pl.col("period_end").max())
                 .filter(pl.col("period_end") >= cutoff)
                 .rename({"qidx": "last_q", "period_end": "last_period_end"}))
    return view.memo("latest_quarter", build)


def ttm(view: PitView, measure: str, lag_quarters: int = 0) -> pl.DataFrame:
    """Sum of four consecutive quarters ending `lag_quarters` before the latest quarter.

    Returns company_id, <measure>_ttm, ids. Missing any of the four quarters -> no row.
    """
    key = f"ttm_{measure}_{lag_quarters}"

    def build() -> pl.DataFrame:
        q = quarterly(view).join(latest_quarter(view), on="company_id")
        end = pl.col("last_q") - lag_quarters
        w = q.filter((pl.col("qidx") <= end) & (pl.col("qidx") > end - 4)
                     & pl.col(measure).is_not_null())
        return (w.group_by("company_id")
                 .agg(pl.col(measure).sum().alias(f"{measure}_ttm"),
                      pl.len().alias("_n"),
                      flat(pl.col(f"ids_{measure}")).alias("ids"))
                 .filter(pl.col("_n") == 4).drop("_n"))
    return view.memo(key, build)


def quarter_value(view: PitView, measure: str, lag_quarters: int) -> pl.DataFrame:
    """Single-quarter value `lag_quarters` before each company's latest quarter."""
    q = quarterly(view).join(latest_quarter(view), on="company_id")
    return (q.filter(pl.col("qidx") == pl.col("last_q") - lag_quarters)
             .select("company_id", pl.col(measure).alias("v"), pl.col(f"ids_{measure}").alias(
                 "ids")))


def balance_sheet(view: PitView) -> pl.DataFrame:
    """Latest balance sheet and the one closest to a year earlier, per company.

    Columns: company_id, bs_date, bs_date_prev and <concept>, <concept>_prev for
    the concepts factors use, plus ids / ids_prev lists.
    """
    concepts = ["total_equity", "equity_owners", "total_assets", "borrowings_noncurrent",
                "borrowings_current", "cash", "bank_balances", "current_investments",
                "inventories", "trade_receivables", "trade_payables"]

    def build() -> pl.DataFrame:
        v, i = _wide(view, "INSTANT", "wide_bs")
        if v.height == 0:
            schema = {"company_id": pl.Int64, "bs_date": pl.Date, "bs_date_prev": pl.Date,
                      "ids": IDS, "ids_prev": IDS}
            for c in concepts:
                schema[c] = pl.Float64
                schema[f"{c}_prev"] = pl.Float64
            return pl.DataFrame(schema=schema)
        v = v.select("company_id", "period_end", *[_col(v, c).alias(c) for c in concepts])
        i = i.select("company_id", "period_end", _ids(i, concepts).alias("ids"))
        v = v.join(i, on=["company_id", "period_end"])
        latest = v.sort("period_end").group_by("company_id").agg(pl.all().last())
        prev = (v.join(latest.select("company_id", pl.col("period_end").alias("_last")),
                       on="company_id")
                 .with_columns(((pl.col("_last") - pl.col("period_end")).dt.total_days() - 365)
                               .abs().alias("_dist"))
                 .filter(pl.col("_dist") <= 60)
                 .sort("_dist").group_by("company_id").agg(pl.all().first())
                 .drop("_last", "_dist"))
        prev = prev.rename({c: f"{c}_prev" for c in prev.columns if c != "company_id"})
        return (latest.rename({"period_end": "bs_date"})
                      .join(prev.rename({"period_end_prev": "bs_date_prev"}), on="company_id",
                            how="left"))
    return view.memo("balance_sheet", build)


def annual(view: PitView) -> pl.DataFrame:
    """FY facts (wide) with a fiscal-year index, for multi-year cash conversion."""
    def build() -> pl.DataFrame:
        v, i = _wide(view, "FY", "wide_fy")
        if v.height == 0:
            return pl.DataFrame(schema={"company_id": pl.Int64, "period_end": pl.Date,
                                        "cfo": pl.Float64, "ebitda": pl.Float64, "ids": IDS})
        out = v.select("company_id", "period_end",
                       _col(v, "cfo").alias("cfo"),
                       (_col(v, "revenue") - _col(v, "total_expenses") + _col(v, "finance_costs")
                        + _col(v, "depreciation")).alias("ebitda"))
        ids = i.select("company_id", "period_end",
                       _ids(i, ["cfo", "revenue", "total_expenses", "finance_costs",
                                "depreciation"]).alias("ids"))
        return out.join(ids, on=["company_id", "period_end"]).sort("company_id", "period_end")
    return view.memo("annual", build)


BS_CONCEPTS = ["total_assets", "current_assets", "current_liabilities", "total_equity",
               "equity_owners", "other_equity", "share_capital", "total_liabilities",
               "equity_and_liabilities", "ppe", "noncurrent_investments", "current_investments",
               "cash", "bank_balances", "inventories", "trade_receivables", "trade_payables",
               "borrowings_noncurrent", "borrowings_current"]
FY_CONCEPTS = ["cfo", "depreciation", "cost_of_materials", "purchases_stock_in_trade",
               "change_in_inventories", "employee_expense", "other_expenses", "finance_costs",
               "other_income"]


def bs_panel(view: PitView) -> pl.DataFrame:
    """Every balance sheet known at as_of: one row per company and balance-sheet date with
    BS_CONCEPTS as columns (null where the filing did not report the line) and `ids`."""
    def build() -> pl.DataFrame:
        v, i = _wide(view, "INSTANT", "wide_bs")
        if v.height == 0:
            return pl.DataFrame(schema={"company_id": pl.Int64, "period_end": pl.Date,
                                        **{c: pl.Float64 for c in BS_CONCEPTS}, "ids": IDS})
        out = v.select("company_id", "period_end",
                       *[_col(v, c).cast(pl.Float64).alias(c) for c in BS_CONCEPTS])
        ids = i.select("company_id", "period_end", _ids(i, BS_CONCEPTS).alias("ids"))
        return out.join(ids, on=["company_id", "period_end"]).sort("company_id", "period_end")
    return view.memo("bs_panel", build)


def fy_panel(view: PitView) -> pl.DataFrame:
    """One row per company and fiscal year end known at as_of.

    revenue, pat and pbt come from the FY figures filed with the year's last
    results, else from the sum of the fiscal year's four quarters (all four
    must be filed; never estimated from fewer). Cash flow and expense lines come
    from FY figures only. The balance sheet filed for the same date is joined
    (BS_CONCEPTS columns, null when there is none). March fiscal years are
    assumed only for the quarter-sum fallback.
    """
    def build() -> pl.DataFrame:
        schema = {"company_id": pl.Int64, "period_end": pl.Date, "revenue": pl.Float64,
                  "pat": pl.Float64, "pbt": pl.Float64,
                  **{c: pl.Float64 for c in FY_CONCEPTS},
                  **{c: pl.Float64 for c in BS_CONCEPTS}, "ids": IDS}
        v, i = _wide(view, "FY", "wide_fy")
        q = quarterly(view).with_columns(
            pl.date(pl.col("period_end").dt.year() + (pl.col("period_end").dt.month() > 3)
                    .cast(pl.Int32), 3, 31).alias("_fy"))
        qsum = (q.group_by("company_id", "_fy")
                 .agg(pl.len().alias("_n"),
                      *[pl.when(pl.col(m).count() == 4).then(pl.col(m).sum()).alias(f"q_{m}")
                        for m in ("top_line", "pat", "pbt")],
                      flat(pl.concat_list("ids_top_line", "ids_pat", "ids_pbt")).alias("q_ids"))
                 .filter(pl.col("_n") == 4).drop("_n").rename({"_fy": "period_end"}))
        if v.height:
            fy = v.select("company_id", "period_end",
                          *[_col(v, c).cast(pl.Float64).alias(f"fy_{c}")
                            for c in ("revenue", "pat", "pat_owners", "pbt")],
                          *[_col(v, c).cast(pl.Float64).alias(c) for c in FY_CONCEPTS])
            fy = fy.join(i.select("company_id", "period_end",
                                  _ids(i, ["revenue", "pat", "pat_owners", "pbt", *FY_CONCEPTS])
                                  .alias("fy_ids")), on=["company_id", "period_end"])
            j = fy.join(qsum, on=["company_id", "period_end"], how="full", coalesce=True)
        else:
            j = qsum.with_columns(*[pl.lit(None, dtype=pl.Float64).alias(f"fy_{c}")
                                    for c in ("revenue", "pat", "pat_owners", "pbt")],
                                  *[pl.lit(None, dtype=pl.Float64).alias(c)
                                    for c in FY_CONCEPTS],
                                  pl.lit([], dtype=IDS).alias("fy_ids"))
        if j.height == 0:
            return pl.DataFrame(schema=schema)
        j = j.select("company_id", "period_end",
                     pl.coalesce("fy_revenue", "q_top_line").alias("revenue"),
                     pl.coalesce("fy_pat_owners", "fy_pat", "q_pat").alias("pat"),
                     pl.coalesce("fy_pbt", "q_pbt").alias("pbt"), *FY_CONCEPTS,
                     pl.concat_list(pl.col("fy_ids").fill_null(pl.lit([], dtype=IDS)),
                                    pl.col("q_ids").fill_null(pl.lit([], dtype=IDS)))
                     .alias("_pl_ids"))
        bs = bs_panel(view).rename({"ids": "_bs_ids"})
        j = j.join(bs, on=["company_id", "period_end"], how="left")
        return (j.with_columns(pl.concat_list(pl.col("_pl_ids"),
                                              pl.col("_bs_ids").fill_null(pl.lit([], dtype=IDS)))
                               .list.unique().list.sort().alias("ids"))
                 .select(list(schema)).sort("company_id", "period_end"))
    return view.memo("fy_panel", build)


# --------------------------------------------------------------------------- market


def primary_prices(view: PitView) -> pl.DataFrame:
    """Adjusted daily prices of each company's primary line (most traded security)."""
    def build() -> pl.DataFrame:
        if not view.has("prices"):
            return pl.DataFrame(schema={"security_id": pl.Int64, "company_id": pl.Int64,
                                        "trade_date": pl.Date, "close": pl.Float64,
                                        "adj_close": pl.Float64, "volume": pl.Int64,
                                        "delivery_pct": pl.Float64})
        px = view.prices()
        recent = px.filter(pl.col("trade_date") > view.as_of_date - dt.timedelta(days=120))
        primary = (recent.group_by("company_id", "security_id")
                         .agg((pl.col("close") * pl.col("volume")).sum().alias("_tv"))
                         .sort("_tv", descending=True)
                         .group_by("company_id").agg(pl.col("security_id").first()))
        return px.join(primary, on=["company_id", "security_id"]).sort("company_id",
                                                                         "trade_date")
    return view.memo("primary_prices", build)


def last_price(view: PitView, max_age_days: int = 10) -> pl.DataFrame:
    """Latest unadjusted close per company (not older than max_age_days)."""
    px = primary_prices(view)
    cutoff = view.as_of_date - dt.timedelta(days=max_age_days)
    return (px.filter(pl.col("trade_date") >= cutoff).group_by("company_id")
              .agg(pl.col("security_id").last(), pl.col("trade_date").last().alias("px_date"),
                   pl.col("close").last().alias("px_close")))


def shares_outstanding(view: PitView) -> pl.DataFrame:
    """Total shares from the latest shareholding filing, adjusted for splits and bonuses
    with ex-date after that filing's period end (and on or before as_of)."""
    def build() -> pl.DataFrame:
        if not view.has("shareholding"):
            return pl.DataFrame(schema={"company_id": pl.Int64, "shares": pl.Float64})
        shp = (view.table("shareholding").filter(pl.col("category") == "total")
                   .sort("period_end", "filed_at").group_by("company_id")
                   .agg(pl.col("shares").last(), pl.col("period_end").last().alias("shp_date")))
        lp = last_price(view)
        shp = shp.join(lp.select("company_id", "security_id"), on="company_id", how="left")
        if view.has("corporate_actions"):
            cas = view.table("corporate_actions").filter(
                pl.col("action_type").is_in(["split", "consolidation", "bonus"])
                & (pl.col("ex_date") <= view.as_of_date))
            mult = cas.with_columns(
                pl.when(pl.col("action_type") == "bonus")
                  .then((pl.col("ratio_a") + pl.col("ratio_b")) / pl.col("ratio_b"))
                  .otherwise(pl.col("fv_old") / pl.col("fv_new")).alias("mult"))
            j = shp.join(mult.select("security_id", "ex_date", "mult"), on="security_id",
                         how="left")
            j = j.with_columns(pl.when(pl.col("ex_date") > pl.col("shp_date"))
                               .then(pl.col("mult")).otherwise(1.0).fill_null(1.0).alias("mult"))
            shp = (j.group_by("company_id", "shares", "shp_date")
                    .agg(pl.col("mult").product().alias("mult"))
                    .with_columns((pl.col("shares") * pl.col("mult")).alias("shares")))
        return shp.select("company_id", "shares", "shp_date")
    return view.memo("shares", build)


def market_cap(view: PitView) -> pl.DataFrame:
    """company_id, mcap (INR), px_close, shares."""
    def build() -> pl.DataFrame:
        return (last_price(view).join(shares_outstanding(view), on="company_id")
                .with_columns((pl.col("px_close") * pl.col("shares")).alias("mcap")))
    return view.memo("mcap", build)


def index_closes(view: PitView, name: str = "Nifty 500") -> pl.DataFrame:
    if not view.has("index_prices"):
        return pl.DataFrame(schema={"trade_date": pl.Date, "idx_close": pl.Float64})
    return (view.table("index_prices").filter(pl.col("index_name") == name)
                .select("trade_date", pl.col("close").alias("idx_close")).sort("trade_date"))


def flat(e: pl.Expr) -> pl.Expr:
    """Concatenate a list column across the rows of a group (inside .agg)."""
    return e.list.explode(keep_nulls=False, empty_as_null=False)


def cagr(end: pl.Expr, start: pl.Expr, years: float) -> pl.Expr:
    """Compound growth; undefined (null) unless both ends are positive."""
    return pl.when((end > 0) & (start > 0)).then((end / start) ** (1.0 / years) - 1.0)
