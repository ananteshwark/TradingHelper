"""A deterministic synthetic market for factor, scoring and backtest tests.

Companies (company_id / security_id = company_id + 100):
  1 GROW   steady grower, 10 -> 2 split ex 2022-06-15, pledge rising
  2 CYCL   cyclical with a loss year (FY21), 1:1 bonus ex 2023-03-10
  3 BANK   bank-format results, NSE basic industry "Private Sector Bank"
  4 NBFC   NBFC, basic industry "Non Banking Financial Company (NBFC)"
  5 GAPS   misses the Dec-2021 quarter entirely
  6 LATE   files ~55 days after quarter end; restates Q1 FY23 revenue a year later
Results are filed at 17:00 IST, N days after quarter end; balance sheets
(instants) come with the September and March results; FY cash flow with March.
"""

from __future__ import annotations

import datetime as dt
import math
import random

import polars as pl

from igs.pit import PitDataset
from igs.timeutil import IST

START, END = dt.date(2017, 4, 3), dt.date(2024, 12, 31)
FILE_LAG = {1: 30, 2: 35, 3: 20, 4: 40, 5: 30, 6: 55}
INDUSTRY = {1: "Industrial Products", 2: "Iron & Steel Products", 3: "Private Sector Bank",
            4: "Non Banking Financial Company (NBFC)", 5: "Industrial Products",
            6: "Packaged Foods"}
SPLIT = (1, dt.date(2022, 6, 15), 10.0, 2.0)
BONUS = (2, dt.date(2023, 3, 10), 1.0, 1.0)


def quarter_ends(start: dt.date, end: dt.date) -> list[dt.date]:
    out = []
    for y in range(start.year, end.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q = dt.date(y, m, d)
            if start <= q <= end:
                out.append(q)
    return out


def _filed(cid: int, q: dt.date) -> dt.datetime:
    d = q + dt.timedelta(days=FILE_LAG[cid])
    return dt.datetime(d.year, d.month, d.day, 17, 0, tzinfo=IST)


class _Facts:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.filing = 0

    def new_filing(self) -> int:
        self.filing += 1
        return self.filing

    def add(self, cid: int, filing: int, pe: dt.date, ptype: str, concept: str, value: float,
            filed: dt.datetime, basis: str = "consolidated") -> None:
        self.rows.append({"fact_id": len(self.rows) + 1, "filing_id": filing, "company_id": cid,
                          "statement_basis": basis, "period_end": pe, "period_type": ptype,
                          "concept": concept, "value": float(value), "filed_at": filed})


def _revenue(cid: int, i: int, rng: random.Random) -> float:
    base = {1: 5e9, 2: 8e9, 3: 2e10, 4: 6e9, 5: 3e9, 6: 1.5e9}[cid]
    growth = {1: 0.045, 2: 0.015, 3: 0.03, 4: 0.05, 5: 0.02, 6: 0.035}[cid]
    cyc = 0.25 * math.sin(i / 3.0) if cid == 2 else 0.0
    return base * (1 + growth) ** i * (1 + cyc) * (1 + rng.uniform(-0.02, 0.02))


def build(seed: int = 7) -> PitDataset:
    rng = random.Random(seed)
    F = _Facts()
    qs = quarter_ends(dt.date(2017, 6, 30), dt.date(2024, 9, 30))
    history: dict[tuple[int, dt.date], dict] = {}
    for cid in range(1, 7):
        equity = {1: 2e10, 2: 3e10, 3: 1.5e11, 4: 4e10, 5: 1e10, 6: 6e9}[cid]
        debt = {1: 3e9, 2: 2e10, 3: 0.0, 4: 1.5e11, 5: 5e9, 6: 1e9}[cid]
        for i, q in enumerate(qs):
            if cid == 5 and q == dt.date(2021, 12, 31):
                continue
            filed = _filed(cid, q)
            fid = F.new_filing()
            rev = _revenue(cid, i, rng)
            vals: dict[str, float] = {}
            if cid == 3:
                ie = rev
                vals = {"interest_earned": ie, "interest_expended": 0.55 * ie,
                        "operating_expenses": 0.2 * ie, "provisions": 0.05 * ie,
                        "other_income": 0.1 * ie}
                pbt = ie * (1 + 0.1 - 0.55 - 0.2 - 0.05)
            else:
                margin = 0.18 if cid != 2 else (0.12 + 0.25 * math.sin(i / 3.0))
                if cid == 2 and dt.date(2020, 4, 1) <= q <= dt.date(2021, 3, 31):
                    margin = -0.08
                fin, dep, oi = 0.01 * rev, 0.03 * rev, 0.02 * rev
                if cid == 4:
                    fin = 0.45 * rev
                    margin = 0.6
                    vals["impairment_on_financial_instruments"] = 0.04 * rev
                exp = rev * (1 - margin) + fin + dep
                pbt = rev + oi - exp
                vals |= {"revenue": rev, "other_income": oi, "total_income": rev + oi,
                         "total_expenses": exp, "finance_costs": fin, "depreciation": dep}
            tax = max(pbt, 0) * 0.25
            vals |= {"pbt": pbt, "tax": tax, "pat": pbt - tax, "pat_owners": pbt - tax}
            history[(cid, q)] = vals
            for c, v in vals.items():
                F.add(cid, fid, q, "Q", c, v, filed)
            # Same quarter last year as a comparative (identical unless restated).
            prev_q = dt.date(q.year - 1, q.month, q.day)
            if (cid, prev_q) in history:
                top = "interest_earned" if cid == 3 else "revenue"
                v = history[(cid, prev_q)][top]
                if cid == 6 and prev_q == dt.date(2022, 6, 30):
                    v *= 0.9      # restated a year later
                F.add(cid, fid, prev_q, "Q", top, v, filed)
            if q.month in (3, 9):
                equity *= 1.06
                debt *= 1.01
                bs = {"total_equity": equity, "equity_owners": equity,
                      "total_assets": equity + debt + 0.3 * equity,
                      "borrowings_noncurrent": 0.7 * debt, "borrowings_current": 0.3 * debt,
                      "cash": 0.05 * equity, "bank_balances": 0.01 * equity,
                      "current_investments": 0.02 * equity,
                      "inventories": 0.5 * rev if cid not in (3, 4) else 0.0,
                      "trade_receivables": (0.66 if cid != 6 else 0.9) * rev,
                      "trade_payables": 0.4 * rev}
                if cid not in (3, 4):
                    # Deterministic extra lines (no random draws, so the series above are
                    # unchanged) for the accounting checks.
                    cash_like = bs["cash"] + bs["bank_balances"] + bs["current_investments"]
                    bs |= {"current_assets": bs["inventories"] + bs["trade_receivables"]
                           + cash_like,
                           "current_liabilities": bs["trade_payables"]
                           + bs["borrowings_current"] + 0.05 * equity,
                           "ppe": 0.6 * equity, "noncurrent_investments": 0.05 * equity,
                           "share_capital": 0.05 * equity, "other_equity": 0.95 * equity,
                           "total_liabilities": bs["total_assets"] - equity}
                for c, v in bs.items():
                    F.add(cid, fid, q, "INSTANT", c, v, filed)
            if q.month == 3 and cid not in (3,):
                fy = [history.get((cid, dt.date(q.year - (m > 3), m, d))) for m, d in
                      ((6, 30), (9, 30), (12, 31), (3, 31))]
                if all(fy):
                    rev_fy = sum(x.get("revenue", 0) for x in fy)
                    exp_fy = sum(x.get("total_expenses", 0) for x in fy)
                    fin_fy = sum(x.get("finance_costs", 0) for x in fy)
                    dep_fy = sum(x.get("depreciation", 0) for x in fy)
                    for c, v in (("revenue", rev_fy), ("total_expenses", exp_fy),
                                 ("finance_costs", fin_fy), ("depreciation", dep_fy),
                                 ("cfo", 0.85 * (rev_fy - exp_fy + fin_fy + dep_fy)),
                                 ("cost_of_materials", 0.45 * rev_fy),
                                 ("employee_expense", 0.10 * rev_fy),
                                 ("other_expenses", 0.12 * rev_fy)):
                        F.add(cid, fid, q, "FY", c, v, filed)
    facts = pl.DataFrame(F.rows, schema={
        "fact_id": pl.Int64, "filing_id": pl.Int64, "company_id": pl.Int64,
        "statement_basis": pl.Utf8, "period_end": pl.Date, "period_type": pl.Utf8,
        "concept": pl.Utf8, "value": pl.Float64, "filed_at": pl.Datetime("us", "UTC")})

    # ------------------------------------------------------------------ prices
    days = []
    d = START
    while d <= END:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    rows, idx_rows = [], []
    level = 10000.0
    for day in days:
        level *= 1 + rng.gauss(0.0004, 0.01)
        idx_rows.append({"index_name": "Nifty 500", "trade_date": day, "close": level})
    for cid in range(1, 7):
        px = {1: 400.0, 2: 250.0, 3: 900.0, 4: 1200.0, 5: 150.0, 6: 80.0}[cid]
        drift = {1: 0.0012, 2: 0.0003, 3: 0.0005, 4: 0.0007, 5: 0.0001, 6: 0.0006}[cid]
        prev = None
        for day in days:
            px *= 1 + rng.gauss(drift, 0.018)
            close = px
            if cid == SPLIT[0] and day >= SPLIT[1]:
                close = px * SPLIT[3] / SPLIT[2]
            if cid == BONUS[0] and day >= BONUS[1]:
                close = px * BONUS[3] / (BONUS[2] + BONUS[3])
            prev_close = prev if prev is not None else close
            if cid == SPLIT[0] and day == SPLIT[1]:
                prev_close = prev * SPLIT[3] / SPLIT[2]
            if cid == BONUS[0] and day == BONUS[1]:
                prev_close = prev * BONUS[3] / (BONUS[2] + BONUS[3])
            vol = int(1e5 * (1 + rng.random()))
            rows.append({"security_id": cid + 100, "company_id": cid, "trade_date": day,
                         "series": "EQ",
                         "open": close, "high": close, "low": close, "close": close,
                         "prev_close": prev_close, "volume": vol,
                         "delivery_pct": 40 + 20 * rng.random() + (10 if cid == 1 and
                                                                    day.year == 2024 else 0)})
            prev = close
    prices = pl.DataFrame(rows)
    index_prices = pl.DataFrame(idx_rows)

    cas = pl.DataFrame([
        {"ca_id": 1, "security_id": SPLIT[0] + 100, "action_type": "split", "ex_date": SPLIT[1],
         "announced_at": dt.datetime(2022, 5, 20, 18, tzinfo=IST), "fv_old": SPLIT[2],
         "fv_new": SPLIT[3], "ratio_a": None, "ratio_b": None, "issue_price": None,
         "cash_per_share": None},
        {"ca_id": 2, "security_id": BONUS[0] + 100, "action_type": "bonus", "ex_date": BONUS[1],
         "announced_at": dt.datetime(2023, 2, 1, 18, tzinfo=IST), "fv_old": None,
         "fv_new": None, "ratio_a": BONUS[2], "ratio_b": BONUS[3], "issue_price": None,
         "cash_per_share": None},
    ], schema={"ca_id": pl.Int64, "security_id": pl.Int64, "action_type": pl.Utf8,
               "ex_date": pl.Date, "announced_at": pl.Datetime("us", "UTC"),
               "fv_old": pl.Float64, "fv_new": pl.Float64, "ratio_a": pl.Float64,
               "ratio_b": pl.Float64, "issue_price": pl.Float64, "cash_per_share": pl.Float64})

    # ------------------------------------------------------------------ shareholding
    shp = []
    base_shares = {1: 5e7, 2: 1e8, 3: 5e8, 4: 6e7, 5: 4e7, 6: 3e7}
    for cid in range(1, 7):
        for i, q in enumerate(qs):
            shares = base_shares[cid]
            if cid == SPLIT[0] and q >= SPLIT[1]:
                shares *= SPLIT[2] / SPLIT[3]
            if cid == BONUS[0] and q >= BONUS[1]:
                shares *= 2
            filed = dt.datetime.combine(q + dt.timedelta(days=21), dt.time(16), tzinfo=IST)
            promoter = 55.0 - (0.4 * i if cid == 5 else 0.0)
            pledge = (2.0 + 1.5 * i) if cid == 1 else (0.0 if cid != 2 else 5.0)
            fii = 15 + (0.2 * i if cid in (1, 4) else -0.1 * i)
            dii = 10 + 0.1 * i
            for cat, pct, pl_pct, holders in (
                    ("total", 100.0, None, 50000 + 100 * i),
                    ("promoter", promoter, pledge, 5),
                    ("public", 100 - promoter, None, 49990 + 100 * i),
                    ("institutions_foreign", fii, None, 100 + 3 * i),
                    ("institutions_domestic", dii, None, 40 + i)):
                shp.append({"filing_id": 100000 + cid * 1000 + i, "company_id": cid,
                            "period_end": q, "category": cat, "shares": shares * pct / 100,
                            "pct_of_total": pct, "pledged_shares": None, "pledged_pct": pl_pct,
                            "holders": float(holders), "filed_at": filed})
    shareholding = pl.DataFrame(shp, schema={
        "filing_id": pl.Int64, "company_id": pl.Int64, "period_end": pl.Date,
        "category": pl.Utf8, "shares": pl.Float64, "pct_of_total": pl.Float64,
        "pledged_shares": pl.Float64, "pledged_pct": pl.Float64, "holders": pl.Float64,
        "filed_at": pl.Datetime("us", "UTC")})

    industry = pl.DataFrame([{"company_id": c, "macro_sector": "X", "sector": "X",
                              "industry": INDUSTRY[c], "basic_industry": INDUSTRY[c],
                              "valid_from": dt.date(2017, 1, 1)} for c in range(1, 7)])
    return PitDataset.from_frames(facts=facts, prices=prices, corporate_actions=cas,
                                  shareholding=shareholding, index_prices=index_prices,
                                  industry=industry)


GATE_DATES = [
    dt.datetime(2022, 8, 1, 23, 59, tzinfo=IST),    # around the split and Q1 FY23 filings
    dt.datetime(2023, 3, 9, 23, 59, tzinfo=IST),    # day before the bonus ex-date
    dt.datetime(2023, 8, 24, 16, 59, tzinfo=IST),   # 1 minute before LATE's restating filing
    dt.datetime(2024, 11, 29, 23, 59, tzinfo=IST),  # latest
]
