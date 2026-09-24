"""Data-integrity checks: is the company's own data trustworthy enough to rank on?

A screen's worst failure is ranking a stock at the top because of a data error
(a filing tagged in lakhs while declaring rupees makes revenue jump a hundred
times). These checks find such cases in what was known at as_of and report
them with the numbers. They say nothing about the company itself, only about
the data behind its score.

  unit_scale_jump     consecutive quarters (or balance sheets), or two versions of
                      the same figure, or a quarter's profit and its EPS x shares,
                      differ by a factor close to a power of ten (100, 1,000, 1 lakh,
                      10 lakh, 1 crore), the usual mistakes between rupees,
                      thousands, lakhs, millions and crores
  statement_identity  identities that must hold inside one filing (revenue + other
                      income = total income, assets = equity + liabilities, ...)
                      break in the last year's filings
  results_overdue     the next quarter's results were not filed by the SEBI LODR
                      deadline (45 days; 60 for the March quarter) plus a grace
                      period. Non-filing often comes before suspension.
"""

from __future__ import annotations

import datetime as dt
import math

import polars as pl

from igs.factors import base as b
from igs.pit.view import FACT_KEY, PitView
from igs.score.flagbase import CLEAR, TRIPPED, cr, result, status_when
from igs.xbrl.checks import IDENTITIES

SCALE_POWERS = (2, 3, 4, 5, 6, 7)      # rupees/thousands/lakhs/millions/crores confusions


def _near_power_of_ten(ratio: pl.Expr, tolerance: float, powers: tuple[int, ...]) -> pl.Expr:
    """|log10(ratio)| within log10(1 + tolerance) of one of `powers`."""
    lg = ratio.abs().log10().abs()
    tol = math.log10(1 + tolerance)
    return pl.any_horizontal([(lg - k).abs() <= tol for k in powers])


def unit_scale_jump(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    tol = cfg["tolerance"]
    n_q = cfg["lookback_quarters"]
    q = b.quarterly(view).join(b.latest_quarter(view), on="company_id")
    q = q.filter((pl.col("qidx") > pl.col("last_q") - n_q) & (pl.col("top_line") > 0))
    q = q.sort("company_id", "qidx").with_columns(
        pl.col("top_line").shift(1).over("company_id").alias("prev"),
        pl.col("period_end").shift(1).over("company_id").alias("prev_end"),
        (pl.col("qidx") - pl.col("qidx").shift(1).over("company_id")).alias("gap"))
    jumps_q = (q.filter((pl.col("gap") == 1)
                        & _near_power_of_ten(pl.col("top_line") / pl.col("prev"), tol,
                                             SCALE_POWERS))
                .select("company_id", pl.lit("quarterly revenue").alias("what"),
                        pl.col("prev").alias("a"), pl.col("prev_end").alias("a_date"),
                        pl.col("top_line").alias("b"), pl.col("period_end").alias("b_date"),
                        pl.col("ids_top_line").alias("ids")))
    bs = b.bs_panel(view).filter(pl.col("total_assets") > 0).sort("company_id", "period_end")
    bs = bs.filter(pl.col("period_end") > view.as_of_date - dt.timedelta(days=92 * n_q))
    bs = bs.with_columns(pl.col("total_assets").shift(1).over("company_id").alias("prev"),
                         pl.col("period_end").shift(1).over("company_id").alias("prev_end"))
    jumps_bs = (bs.filter(pl.col("prev").is_not_null()
                          & _near_power_of_ten(pl.col("total_assets") / pl.col("prev"), tol,
                                               SCALE_POWERS))
                  .select("company_id", pl.lit("total assets").alias("what"),
                          pl.col("prev").alias("a"), pl.col("prev_end").alias("a_date"),
                          pl.col("total_assets").alias("b"), pl.col("period_end").alias("b_date"),
                          "ids"))
    # Two versions of the same figure a power of ten apart (from 10 upwards).
    if view.has("facts"):
        f = view.table("facts").join(b.basis_choice(view), on=["company_id", "statement_basis"])
        f = f.filter((pl.col("period_end") > view.as_of_date - dt.timedelta(days=92 * n_q))
                     & (pl.col("value") != 0))
        v = (f.group_by(FACT_KEY).agg(pl.col("value").min().alias("a"),
                                      pl.col("value").max().alias("b"),
                                      pl.col("fact_id").alias("ids"), pl.len().alias("n"))
              .filter((pl.col("n") > 1) & (pl.col("a") > 0)
                      & _near_power_of_ten(pl.col("b") / pl.col("a"), tol, (1, *SCALE_POWERS))))
        jumps_v = v.select("company_id",
                           pl.format("{} in two filings", pl.col("concept")).alias("what"),
                           "a", pl.col("period_end").alias("a_date"), "b",
                           pl.col("period_end").alias("b_date"), "ids")
    else:
        jumps_v = jumps_q.clear()
    jumps = pl.concat([jumps_q, jumps_bs, jumps_v, _eps_mismatch(view, tol)],
                      how="vertical_relaxed")
    hits = (jumps.sort("b_date", descending=True).group_by("company_id")
                 .agg(pl.all().first(), pl.len().alias("n_jumps"))
                 .with_columns(
                     pl.lit(TRIPPED).alias("status"),
                     pl.format("{} Rs {} cr ({}) vs Rs {} cr ({}): a factor of about {}, the "
                               "size of a units mix-up in a filing; {} such jump(s)",
                               "what", cr(pl.col("a")), "a_date", cr(pl.col("b")), "b_date",
                               (10 ** (pl.col("b") / pl.col("a")).abs().log10().abs().round(0))
                               .cast(pl.Int64), "n_jumps").alias("message"),
                     pl.struct("what", "a", "a_date", "b", "b_date").struct.json_encode()
                       .alias("evidence"),
                     pl.col("ids").alias("source_ids")))
    known = q.select("company_id").unique().join(hits.select("company_id"), on="company_id",
                                                 how="anti")
    clean = known.with_columns(pl.lit(CLEAR).alias("status"), pl.lit(
        f"no power-of-ten jumps in revenue, total assets or restated figures over the last "
        f"{n_q} quarters").alias("message"), pl.lit("{}").alias("evidence"),
        pl.lit([], dtype=b.IDS).alias("source_ids"))
    both = pl.concat([hits.select("company_id", "status", "message", "evidence", "source_ids"),
                      clean], how="vertical_relaxed")
    return result("unit_scale_jump", companies, both,
                  f"no quarterly revenue filed in the last {n_q} quarters")


MIN_EPS_FOR_CHECK = 0.5   # EPS is rounded to paise; smaller values cannot be compared


def _eps_mismatch(view: PitView, tol: float) -> pl.DataFrame:
    """Latest quarter's profit vs basic EPS x shares (paid-up capital / face value). The
    same filing states both; a ratio near a power of ten means one of them was tagged in
    the wrong unit. (A real 2020-taxonomy filing matched to the paise: a loss of Rs 92.37
    lakh over 1.30 crore shares and EPS -0.71.)"""
    cols = ["pat_owners", "pat", "eps_basic", "paid_up_equity_capital", "face_value"]
    f = (view.facts(concepts=cols).filter(pl.col("period_type") == "Q")
             .join(b.basis_choice(view), on=["company_id", "statement_basis"]))
    schema = {"company_id": pl.Int64, "what": pl.Utf8, "a": pl.Float64, "a_date": pl.Date,
              "b": pl.Float64, "b_date": pl.Date, "ids": b.IDS}
    if f.height == 0:
        return pl.DataFrame(schema=schema)
    w = f.pivot(on="concept", index=["company_id", "period_end"], values="value",
                aggregate_function="first")
    ids = f.group_by("company_id", "period_end").agg(pl.col("fact_id").alias("ids"))
    for c in cols:
        if c not in w.columns:
            w = w.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    w = (w.join(ids, on=["company_id", "period_end"]).sort("period_end")
          .group_by("company_id").agg(pl.all().last()))
    w = w.with_columns(pl.coalesce("pat_owners", "pat").alias("profit"),
                       (pl.col("eps_basic") * pl.col("paid_up_equity_capital")
                        / pl.col("face_value")).alias("implied"))
    w = w.filter((pl.col("eps_basic").abs() >= MIN_EPS_FOR_CHECK) & (pl.col("face_value") > 0)
                 & (pl.col("implied") != 0) & pl.col("profit").is_not_null()
                 & ((pl.col("profit") > 0) == (pl.col("implied") > 0))
                 & _near_power_of_ten(pl.col("profit") / pl.col("implied"), tol, SCALE_POWERS))
    return w.select("company_id", pl.lit("quarterly profit vs EPS x shares").alias("what"),
                    pl.col("implied").alias("a"), pl.col("period_end").alias("a_date"),
                    pl.col("profit").alias("b"), pl.col("period_end").alias("b_date"),
                    "ids").cast(schema)


def statement_identity(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    """Identities inside one filing, for periods ending in the last `lookback_days`."""
    rel_tol = cfg["rel_tol_pct"] / 100
    since = view.as_of_date - dt.timedelta(days=cfg["lookback_days"])
    f = view.facts().filter(pl.col("period_end") >= since)
    if f.height == 0 or "filing_id" not in f.columns:
        return result("statement_identity", companies, pl.DataFrame(
            schema={"company_id": pl.Int64, "status": pl.Utf8, "message": pl.Utf8}),
            "no recent filings with identity totals")
    key = ["filing_id", "company_id", "statement_basis", "period_end", "period_type"]
    wide = f.pivot(on="concept", index=key, values="value", aggregate_function="first")
    checked, breaks = [], []
    for name, lhs, rhs in IDENTITIES:
        if not all(c in wide.columns for c in lhs + rhs):
            continue
        sub = wide.drop_nulls(lhs + rhs).with_columns(
            pl.sum_horizontal(lhs).alias("lhs"), pl.sum_horizontal(rhs).alias("rhs"))
        checked.append(sub.select("company_id"))
        bad = sub.filter((pl.col("lhs") - pl.col("rhs")).abs()
                         > pl.max_horizontal(pl.lit(1e5), pl.col("lhs").abs() * rel_tol))
        breaks.append(bad.select("company_id", "period_end", "period_type", "lhs", "rhs",
                                 pl.lit(name).alias("identity")))
    if not checked:
        return result("statement_identity", companies, pl.DataFrame(
            schema={"company_id": pl.Int64, "status": pl.Utf8, "message": pl.Utf8}),
            "recent filings do not report the totals needed for identity checks")
    evaluated = pl.concat(checked).unique()
    br = pl.concat(breaks) if breaks else pl.DataFrame(
        schema={"company_id": pl.Int64, "period_end": pl.Date, "period_type": pl.Utf8,
                "lhs": pl.Float64, "rhs": pl.Float64, "identity": pl.Utf8})
    first = (br.sort("period_end", descending=True).group_by("company_id")
               .agg(pl.all().first(), pl.len().alias("n")))
    out = evaluated.join(first, on="company_id", how="left").with_columns(
        status_when(pl.col("identity").is_not_null()),
        pl.when(pl.col("identity").is_not_null())
          .then(pl.format("{} identity breaks in the {} {} filing: Rs {} cr vs Rs {} cr; {} "
                          "break(s) in {} days", "identity", "period_end", "period_type",
                          cr(pl.col("lhs")), cr(pl.col("rhs")), "n", pl.lit(cfg["lookback_days"])))
          .otherwise(pl.lit(f"accounting identities hold (within {rel_tol:.0%}) in the last "
                            f"{cfg['lookback_days']} days of filings")).alias("message"))
    return result("statement_identity", companies, out,
                  "recent filings do not report the totals needed for identity checks")


def results_overdue(view: PitView, companies: list[int], cfg: dict) -> pl.DataFrame:
    grace = cfg["grace_days"]
    ext = {dt.date.fromisoformat(str(e["quarter_end"])): dt.date.fromisoformat(str(e["deadline"]))
           for e in cfg.get("extensions", [])}
    parts = []
    if view.has("facts"):
        parts.append(view.facts(concepts=["revenue", "interest_earned", "interest_income",
                                          "pat", "pbt"])
                     .filter(pl.col("period_type") == "Q").select("company_id", "period_end"))
    if view.has("filings"):
        parts.append(view.table("filings").filter(pl.col("filing_type") == "financial_results")
                     .select("company_id", "period_end"))
    if not parts:
        return result("results_overdue", companies, pl.DataFrame(
            schema={"company_id": pl.Int64, "status": pl.Utf8, "message": pl.Utf8}),
            "no quarterly results loaded")
    last = (pl.concat(parts).group_by("company_id").agg(pl.col("period_end").max().alias("last"))
              .with_columns(pl.col("last").dt.offset_by("3mo").dt.month_end().alias("next_q")))
    ext_df = pl.DataFrame({"next_q": list(ext), "ext_deadline": list(ext.values())},
                          schema={"next_q": pl.Date, "ext_deadline": pl.Date})
    last = last.join(ext_df, on="next_q", how="left").with_columns(
        pl.coalesce("ext_deadline", pl.col("next_q") + pl.duration(days=pl.when(
            pl.col("next_q").dt.month() == 3).then(cfg["q4_deadline_days"])
            .otherwise(cfg["deadline_days"]))).alias("deadline"))
    last = last.with_columns((pl.col("deadline") + pl.duration(days=grace)).alias("limit"))
    out = last.with_columns(
        status_when(pl.lit(view.as_of_date) > pl.col("limit")),
        pl.when(pl.lit(view.as_of_date) > pl.col("limit"))
          .then(pl.format("results for the quarter ended {} were due by {} (plus {} days' grace) "
                          "and had not been filed; latest filed quarter ended {}", "next_q",
                          "deadline", pl.lit(grace), "last"))
          .otherwise(pl.format("latest results for the quarter ended {}; next due by {}", "last",
                               "deadline")).alias("message"),
        pl.struct("last", "next_q", "deadline").alias("evidence"))
    return result("results_overdue", companies, out, "no quarterly results loaded")
