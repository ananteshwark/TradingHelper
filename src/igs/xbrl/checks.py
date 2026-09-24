"""Fundamentals data-quality checks and the hand-checked-company validation.

* missing_quarters      - gaps in each company's quarterly history (reported,
                          never filled in)
* identity_breaks       - accounting identities that must hold inside one
                          filing's numbers (revenue + other income = total
                          income, assets = equity + liabilities, ...). These
                          catch mapping errors without any hand-entered values.
* compare_expected      - hand-entered values from annual reports / results
                          PDFs (config/hand_checked.yaml) vs what was parsed.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import yaml

from igs.config import config_dir
from igs.pit.view import FACT_KEY


def quarter_ends(start: dt.date, end: dt.date) -> list[dt.date]:
    out = []
    y, m = start.year, ((start.month - 1) // 3 + 1) * 3
    while True:
        d = dt.date(y, m, 30 if m in (6, 9) else 31)
        if d > end:
            return out
        if d >= start:
            out.append(d)
        m += 3
        if m > 12:
            y, m = y + 1, 3


def missing_quarters(facts: pl.DataFrame, concept: str = "revenue") -> pl.DataFrame:
    """Quarter ends between a company's first and last quarterly `concept` with no value."""
    q = (facts.filter((pl.col("period_type") == "Q") & (pl.col("concept") == concept))
              .select("company_id", "statement_basis", "period_end").unique())
    rows = []
    for (cid, basis), g in q.group_by("company_id", "statement_basis"):
        have = set(g["period_end"].to_list())
        for d in quarter_ends(min(have), max(have)):
            if d not in have:
                rows.append({"company_id": cid, "statement_basis": basis, "period_end": d})
    return pl.DataFrame(rows, schema={"company_id": pl.Int64, "statement_basis": pl.Utf8,
                                      "period_end": pl.Date})


# (name, lhs concepts summed, rhs concepts summed); a missing term skips the check
IDENTITIES = [
    ("total_income", ["revenue", "other_income"], ["total_income"]),
    ("pbt_before_exceptional", ["total_income"], ["total_expenses", "pbt_before_exceptional"]),
    ("tax_split", ["current_tax", "deferred_tax"], ["tax"]),
    ("balance_sheet", ["total_assets"], ["equity_and_liabilities"]),
    ("equity_split", ["share_capital", "other_equity"], ["equity_owners"]),
]


def identity_breaks(facts: pl.DataFrame, rel_tol: float = 0.005,
                    abs_tol: float = 1e5) -> pl.DataFrame:
    """Rows where an identity fails within one filing/basis/period."""
    key = ["filing_id", "company_id", "statement_basis", "period_end", "period_type"]
    wide = (facts.group_by([*key, "concept"]).agg(pl.col("value").first())
                 .pivot(on="concept", index=key, values="value"))
    out = []
    for name, lhs, rhs in IDENTITIES:
        if not all(c in wide.columns for c in lhs + rhs):
            continue
        sub = wide.drop_nulls(lhs + rhs).with_columns(
            pl.sum_horizontal(lhs).alias("lhs"), pl.sum_horizontal(rhs).alias("rhs"))
        sub = sub.filter((pl.col("lhs") - pl.col("rhs")).abs() >
                         pl.max_horizontal(pl.lit(abs_tol), pl.col("lhs").abs() * rel_tol))
        for r in sub.select(*key, "lhs", "rhs").iter_rows(named=True):
            out.append({**r, "identity": name})
    return pl.DataFrame(out, schema={"filing_id": pl.Int64, "company_id": pl.Int64,
                                     "statement_basis": pl.Utf8, "period_end": pl.Date,
                                     "period_type": pl.Utf8, "lhs": pl.Float64,
                                     "rhs": pl.Float64, "identity": pl.Utf8})


def load_hand_checked(path: Path | None = None) -> dict:
    return yaml.safe_load((path or config_dir() / "hand_checked.yaml").read_text(encoding="utf-8"))


def compare_expected(latest: pl.DataFrame, expected: list[dict], company_ids: dict[str, int],
                     rel_tol: float = 0.005) -> pl.DataFrame:
    """expected rows: symbol, statement_basis, period_end, period_type, concept, value_cr.

    `latest` is the latest-version fact table (see pit.latest_versions)."""
    rows = []
    for e in expected:
        cid = company_ids.get(e["symbol"])
        got = None
        if cid is not None:
            hit = latest.filter(
                (pl.col("company_id") == cid)
                & (pl.col("statement_basis") == e["statement_basis"])
                & (pl.col("period_end") == dt.date.fromisoformat(str(e["period_end"])))
                & (pl.col("period_type") == e["period_type"])
                & (pl.col("concept") == e["concept"]))
            got = hit["value"][0] / 1e7 if hit.height else None
        want = float(e["value_cr"])
        ok = got is not None and abs(got - want) <= max(0.01, abs(want) * rel_tol)
        rows.append({"symbol": e["symbol"], "concept": e["concept"],
                     "period_end": str(e["period_end"]), "expected_cr": want, "parsed_cr": got,
                     "status": "ok" if ok else ("missing" if got is None else "mismatch")})
    return pl.DataFrame(rows, schema={"symbol": pl.Utf8, "concept": pl.Utf8,
                                      "period_end": pl.Utf8, "expected_cr": pl.Float64,
                                      "parsed_cr": pl.Float64, "status": pl.Utf8})


def latest(facts: pl.DataFrame) -> pl.DataFrame:
    return (facts.sort([*FACT_KEY, "filed_at", "fact_id"])
                 .unique(subset=FACT_KEY, keep="last", maintain_order=True))
