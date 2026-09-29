"""Market cap for companies without a shareholding filing: shares = paid-up equity capital /
face value from the latest quarterly results known at the date, cross-checked against
profit / basic EPS. On the first real load 1,581 of 2,600 companies had no shareholding
filing loaded, so no market cap and no place in the universe."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
import synthetic_market as M

from igs.factors import base
from igs.pit import PitDataset, PitView
from igs.pit.harness import check_no_lookahead
from igs.pit.knowledge import KNOWN_AT

AS_OF = M.GATE_DATES[-1]                    # 2024-11-29 23:59 IST


def _frames() -> dict[str, pl.DataFrame]:
    return {n: df.drop(KNOWN_AT) for n, df in M.build().tables.items()}


def _with_capital(frames: dict, cid: int, shares, fv: float = 10.0, scale: float = 1.0,
                  eps: bool = True) -> PitDataset:
    """`cid` without shareholding filings, and with paid-up capital, face value and basic
    EPS on each of its quarterly results filings. `shares` may depend on the period end;
    `scale` mis-states the capital (a unit slip)."""
    facts = frames["facts"]
    pat = facts.filter((pl.col("company_id") == cid) & (pl.col("period_type") == "Q")
                       & (pl.col("concept") == "pat"))
    rows, n = [], facts["fact_id"].max() + 1
    for r in pat.iter_rows(named=True):
        s = shares(r["period_end"]) if callable(shares) else shares
        extra = [("paid_up_equity_capital", s * fv * scale), ("face_value", fv)]
        if eps:
            extra.append(("eps_basic", r["value"] / s))
        for concept, value in extra:
            rows.append({**r, "fact_id": n, "concept": concept, "value": value})
            n += 1
    out = dict(frames)
    out["facts"] = pl.concat([facts, pl.DataFrame(rows, schema=facts.schema)])
    out["shareholding"] = frames["shareholding"].filter(pl.col("company_id") != cid)
    return PitDataset.from_frames(**out)


def _mcap(ds: PitDataset, as_of: dt.datetime = AS_OF) -> dict[int, dict]:
    return {r["company_id"]: r for r in base.market_cap(PitView(ds, as_of)).iter_rows(named=True)}


def test_shares_come_from_paid_up_capital_without_a_shareholding_filing():
    before = _mcap(M.build())
    after = _mcap(_with_capital(_frames(), 5, 4e7))       # company 5 has 4 crore shares
    assert before[5]["shares_source"] == "shareholding"
    assert after[5]["shares_source"] == "paid_up_capital"
    assert after[5]["shares"] == pytest.approx(4e7) and after[5]["mcap"] == pytest.approx(
        before[5]["mcap"])
    assert after[1]["shares_source"] == "shareholding"   # others unchanged


def test_a_bonus_after_the_results_period_is_applied():
    """Company 2's 1:1 bonus goes ex on 2023-03-10: results for the December quarter still
    state the old capital, so the count doubles from the ex-date, as it does for shares
    from a shareholding filing."""
    ds = _with_capital(_frames(), 2, lambda pe: 1e8 if pe < M.BONUS[1] else 2e8)
    day = dt.datetime(2023, 3, 9, 23, 59, tzinfo=M.IST)
    assert _mcap(ds, day)[2]["shares"] == pytest.approx(1e8)
    assert _mcap(ds, day + dt.timedelta(days=1))[2]["shares"] == pytest.approx(2e8)


def test_a_unit_slip_is_refused_unless_eps_is_too_small_to_compare():
    slipped = _with_capital(_frames(), 5, 4e7, scale=10.0)     # capital tagged 10x
    assert 5 not in _mcap(slipped)                              # no market cap, not a wrong one
    unchecked = _with_capital(_frames(), 5, 4e7, scale=10.0, eps=False)
    assert _mcap(unchecked)[5]["shares"] == pytest.approx(4e8)  # nothing to check it against


@pytest.mark.lookahead
def test_capital_shares_are_point_in_time():
    """A rights issue doubles company 5's capital in its latest filing: the count changes
    only once that filing is public, and the harness finds no leak."""
    frames = _frames()
    last_q = (frames["facts"].filter((pl.col("company_id") == 5)
                                     & (pl.col("period_type") == "Q")
                                     & (pl.col("filed_at") <= AS_OF.astimezone(dt.UTC)))
              .sort("period_end")[-1])
    q, filed = last_q["period_end"][0], last_q["filed_at"][0]
    ds = _with_capital(frames, 5, lambda pe: 8e7 if pe >= q else 4e7)
    minute = dt.timedelta(minutes=1)
    assert _mcap(ds, filed - minute)[5]["shares"] == pytest.approx(4e7)
    assert _mcap(ds, filed + minute)[5]["shares"] == pytest.approx(8e7)
    check_no_lookahead(lambda v: base.market_cap(v).drop("shp_date"), ds,
                       [*M.GATE_DATES, filed - minute], name="market_cap")
