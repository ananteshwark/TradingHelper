"""Shared shape of every check (red flag or caution).

A check returns one row per company:
  company_id, flag, status, message, evidence (JSON text), source_ids, source_urls
with status one of
  tripped           the condition was found
  clear             evaluated, nothing found
  data_unavailable  could not be evaluated; never treated as a pass
  not_applicable    the check does not apply (e.g. receivable days for a bank)

Severity is configuration, not code (red_flags.yaml):
  reject   a tripped check moves the stock to Rejected with the reason
  caution  a tripped check keeps the stock out of High conviction, with the reason
`unavailable_blocks` says whether data_unavailable also keeps a stock out of High
conviction (true unless the configuration explicitly says false).
"""

from __future__ import annotations

import json

import polars as pl

TRIPPED, CLEAR, UNAVAILABLE, NA = "tripped", "clear", "data_unavailable", "not_applicable"
REJECT, CAUTION = "reject", "caution"
SCHEMA = {"company_id": pl.Int64, "flag": pl.Utf8, "status": pl.Utf8, "message": pl.Utf8,
          "evidence": pl.Utf8, "source_ids": pl.List(pl.Int64), "source_urls": pl.List(pl.Utf8)}
CRORE = 1e7


def row(cid: int, flag: str, status: str, message: str, evidence: dict | None = None,
        ids: list[int] | None = None, urls: list[str] | None = None) -> dict:
    return {"company_id": cid, "flag": flag, "status": status, "message": message,
            "evidence": json.dumps(evidence or {}, default=str), "source_ids": ids or [],
            "source_urls": urls or []}


def empty() -> pl.DataFrame:
    return pl.DataFrame(schema=SCHEMA)


def result(flag: str, companies: list[int], computed: pl.DataFrame, missing_message: str,
           not_applicable: set[int] | None = None,
           na_message: str = "not applicable to financials") -> pl.DataFrame:
    """Complete a vectorised check's output for every requested company.

    `computed` has company_id, status, message and optionally evidence (struct or
    text) and source_ids. Requested companies with no computed row are
    data_unavailable with `missing_message`; companies in `not_applicable` are
    not_applicable whatever was computed for them.
    """
    na = not_applicable or set()
    want = pl.DataFrame({"company_id": companies}, schema={"company_id": pl.Int64})
    c = computed
    if "evidence" not in c.columns:
        c = c.with_columns(pl.lit("{}").alias("evidence"))
    elif c.schema["evidence"] != pl.Utf8:
        c = c.with_columns(pl.col("evidence").struct.json_encode())
    if "source_ids" not in c.columns:
        c = c.with_columns(pl.lit([], dtype=pl.List(pl.Int64)).alias("source_ids"))
    c = c.select(pl.col("company_id").cast(pl.Int64), pl.col("status").cast(pl.Utf8),
                 pl.col("message").cast(pl.Utf8), pl.col("evidence").cast(pl.Utf8),
                 pl.col("source_ids").cast(pl.List(pl.Int64)).list.drop_nulls())
    c = c.unique("company_id", keep="first", maintain_order=True)
    out = want.join(c, on="company_id", how="left").with_columns(
        pl.when(pl.col("company_id").is_in(list(na))).then(pl.lit(NA))
          .when(pl.col("status").is_null()).then(pl.lit(UNAVAILABLE))
          .otherwise(pl.col("status")).alias("status"),
        pl.when(pl.col("company_id").is_in(list(na))).then(pl.lit(na_message))
          .when(pl.col("status").is_null()).then(pl.lit(missing_message))
          .otherwise(pl.col("message")).alias("message"),
        pl.when(pl.col("company_id").is_in(list(na)) | pl.col("status").is_null())
          .then(pl.lit("{}")).otherwise(pl.col("evidence")).alias("evidence"),
        pl.when(pl.col("company_id").is_in(list(na)) | pl.col("status").is_null())
          .then(pl.lit([], dtype=pl.List(pl.Int64))).otherwise(pl.col("source_ids"))
          .alias("source_ids"))
    return out.select(
        "company_id", pl.lit(flag).alias("flag"), "status", "message", "evidence",
        "source_ids", pl.lit([], dtype=pl.List(pl.Utf8)).alias("source_urls")
    ).cast(SCHEMA)


def status_when(condition: pl.Expr) -> pl.Expr:
    return pl.when(condition).then(pl.lit(TRIPPED)).otherwise(pl.lit(CLEAR)).alias("status")


def cr(e: pl.Expr) -> pl.Expr:
    """Rupees to a crore string with one decimal, for messages."""
    return (e / CRORE).round(1).cast(pl.Utf8)


def pct(e: pl.Expr, digits: int = 1) -> pl.Expr:
    """A fraction as a percentage string, for messages."""
    v = (e * 100).round(digits)
    return (v.cast(pl.Int64) if digits == 0 else v).cast(pl.Utf8) + pl.lit("%")


def pct_or_na(e: pl.Expr, digits: int = 1) -> pl.Expr:
    return pl.when(e.is_not_null() & e.is_finite()).then(pct(e, digits)).otherwise(pl.lit("n/a"))
