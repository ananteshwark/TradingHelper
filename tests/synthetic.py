"""Synthetic point-in-time datasets for tests.

These are deliberately hand-built so every number is known in advance. They
are NOT stand-ins for exchange formats; parsers are tested against real
payloads captured by `igs sources verify`.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from igs.pit import PitDataset
from igs.timeutil import IST


def ist(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=IST)


def facts_frame(rows: list[dict]) -> pl.DataFrame:
    """rows: company_id, period_end, concept, value, filed_at (+ optional fact_id, basis, type)."""
    out = []
    for i, r in enumerate(rows, start=1):
        out.append({
            "fact_id": r.get("fact_id", i),
            "company_id": r["company_id"],
            "statement_basis": r.get("statement_basis", "consolidated"),
            "period_end": r["period_end"],
            "period_type": r.get("period_type", "Q"),
            "concept": r["concept"],
            "value": float(r["value"]),
            "filed_at": r["filed_at"],
        })
    return pl.DataFrame(out, schema={
        "fact_id": pl.Int64, "company_id": pl.Int64, "statement_basis": pl.Utf8,
        "period_end": pl.Date, "period_type": pl.Utf8, "concept": pl.Utf8,
        "value": pl.Float64, "filed_at": pl.Datetime("us", "UTC"),
    })


def business_days(start: dt.date, end: dt.date) -> list[dt.date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def price_frame(security_id: int, closes: dict[dt.date, float],
                prev_close_override: dict[dt.date, float] | None = None) -> pl.DataFrame:
    days = sorted(closes)
    rows = []
    prev = None
    for d in days:
        c = closes[d]
        pc = (prev_close_override or {}).get(d, prev)
        rows.append({"security_id": security_id, "trade_date": d, "open": c, "high": c,
                     "low": c, "close": c, "prev_close": pc, "volume": 1000})
        prev = c
    return pl.DataFrame(rows, schema={
        "security_id": pl.Int64, "trade_date": pl.Date, "open": pl.Float64,
        "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
        "prev_close": pl.Float64, "volume": pl.Int64})


def ca_frame(rows: list[dict]) -> pl.DataFrame:
    cols = {"ca_id": pl.Int64, "security_id": pl.Int64, "action_type": pl.Utf8,
            "ex_date": pl.Date, "announced_at": pl.Datetime("us", "UTC"),
            "fv_old": pl.Float64, "fv_new": pl.Float64, "ratio_a": pl.Float64,
            "ratio_b": pl.Float64, "issue_price": pl.Float64, "cash_per_share": pl.Float64}
    full = [{k: r.get(k) for k in cols} for r in rows]
    return pl.DataFrame(full, schema=cols)


def standard_dataset() -> PitDataset:
    """Two companies over 2023-2024 with the traps a leaky factor falls into:

    * company 1 files Q1 FY24 revenue 100 on 2023-08-10, then restates it to 90
      on 2024-02-12 (inside a later filing);
    * company 1's Q2 FY24 results (period_end 2023-09-30) are filed late, on
      2023-11-14 18:30 IST, i.e. after the close;
    * company 2 has a 1:1 bonus (ex 2024-03-15) announced on 2024-02-20 and a
      10 -> 2 face-value split (ex 2024-06-14);
    """
    facts = facts_frame([
        {"company_id": 1, "period_end": dt.date(2023, 6, 30), "concept": "revenue",
         "value": 100, "filed_at": ist(2023, 8, 10, 16, 0)},
        {"company_id": 1, "period_end": dt.date(2023, 9, 30), "concept": "revenue",
         "value": 120, "filed_at": ist(2023, 11, 14, 18, 30)},
        {"company_id": 1, "period_end": dt.date(2023, 12, 31), "concept": "revenue",
         "value": 130, "filed_at": ist(2024, 2, 12, 17, 0)},
        {"company_id": 1, "period_end": dt.date(2023, 6, 30), "concept": "revenue",
         "value": 90, "filed_at": ist(2024, 2, 12, 17, 0)},  # restatement
        {"company_id": 2, "period_end": dt.date(2023, 6, 30), "concept": "revenue",
         "value": 50, "filed_at": ist(2023, 7, 20, 15, 0)},
        {"company_id": 2, "period_end": dt.date(2023, 9, 30), "concept": "revenue",
         "value": 55, "filed_at": ist(2023, 10, 19, 15, 0)},
        {"company_id": 2, "period_end": dt.date(2023, 12, 31), "concept": "revenue",
         "value": 61, "filed_at": ist(2024, 1, 18, 15, 0)},
    ])

    days = business_days(dt.date(2023, 7, 3), dt.date(2024, 9, 30))
    closes_1 = {d: 100.0 + 0.1 * i for i, d in enumerate(days)}
    closes_2 = {}
    for i, d in enumerate(days):
        px = 400.0 + 0.2 * i
        if d >= dt.date(2024, 3, 15):
            px /= 2          # 1:1 bonus
        if d >= dt.date(2024, 6, 14):
            px /= 5          # 10 -> 2 split
        closes_2[d] = px
    prices = pl.concat([price_frame(1, closes_1), price_frame(2, closes_2)])

    cas = ca_frame([
        {"ca_id": 1, "security_id": 2, "action_type": "bonus", "ex_date": dt.date(2024, 3, 15),
         "announced_at": ist(2024, 2, 20, 12, 0), "ratio_a": 1, "ratio_b": 1},
        {"ca_id": 2, "security_id": 2, "action_type": "split", "ex_date": dt.date(2024, 6, 14),
         "announced_at": ist(2024, 5, 2, 12, 0), "fv_old": 10, "fv_new": 2},
    ])
    return PitDataset.from_frames(facts=facts, prices=prices, corporate_actions=cas)


AS_OF_DATES = [
    ist(2023, 11, 14, 15, 30),   # before the late Q2 filing on the same day
    ist(2023, 11, 14, 23, 59),   # after it
    ist(2024, 2, 1, 23, 59),     # before the restatement
    ist(2024, 2, 12, 23, 59),    # after the restatement
    ist(2024, 3, 1, 23, 59),     # bonus announced, not yet ex
    ist(2024, 4, 1, 23, 59),     # after bonus ex-date
    ist(2024, 6, 13, 23, 59),    # day before split ex-date
    ist(2024, 9, 30, 23, 59),
]
