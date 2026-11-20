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


def _with_issue_size(frames: dict, rows: list[tuple]) -> PitDataset:
    """NSE issue sizes: (company_id, security_id, trade_date, shares)."""
    out = dict(frames)
    out["issue_sizes"] = pl.DataFrame(rows, schema={"company_id": pl.Int64,
                                                    "security_id": pl.Int64,
                                                    "trade_date": pl.Date,
                                                    "shares": pl.Float64}, orient="row")
    return PitDataset.from_frames(**out)


def _primary(cid: int) -> int:
    lp = base.last_price(PitView(M.build(), AS_OF))
    return lp.filter(pl.col("company_id") == cid)["security_id"][0]


def test_nse_issue_size_is_the_last_source_of_shares():
    """Company 5 without a shareholding filing, results capital or a Screener export takes
    NSE's issue size for its own security; company 1, which has a filing, keeps it; an
    issue size of another security (a DVR share) is never used."""
    frames = _frames()
    frames["shareholding"] = frames["shareholding"].filter(pl.col("company_id") != 5)
    before = _mcap(PitDataset.from_frames(**frames))
    assert 5 not in before                                   # no market cap: out of the universe
    day = AS_OF.date() - dt.timedelta(days=3)
    ds = _with_issue_size(frames, [(5, _primary(5), day - dt.timedelta(days=30), 3e7),
                                   (5, _primary(5), day, 4e7),
                                   (5, 99999, day, 9e9),
                                   (1, _primary(1), day, 1.0)])
    after = _mcap(ds)
    assert (after[5]["shares_source"], after[5]["shares"]) == ("nse_issue_size", 4e7)
    assert after[5]["mcap"] == pytest.approx(after[5]["px_close"] * 4e7)
    assert after[1]["shares_source"] == "shareholding" and after[1]["shares"] != 1.0
    from igs import universe
    from igs.config import load_universe
    u = universe.build_universe(PitView(ds, AS_OF), load_universe())
    assert u.filter(pl.col("company_id") == 5)["reason"][0] != "market cap unavailable"


@pytest.mark.lookahead
def test_issue_sizes_are_known_from_their_days_close():
    """A file for a day is known from that day's close, like its prices."""
    frames = _frames()
    frames["shareholding"] = frames["shareholding"].filter(pl.col("company_id") != 5)
    day = AS_OF.date()
    ds = _with_issue_size(frames, [(5, _primary(5), day - dt.timedelta(days=1), 4e7),
                                   (5, _primary(5), day, 8e7)])
    close = dt.datetime.combine(day, dt.time(15, 30), tzinfo=M.IST)
    minute = dt.timedelta(minutes=1)
    assert _mcap(ds, close - minute)[5]["shares"] == pytest.approx(4e7)
    assert _mcap(ds, close + minute)[5]["shares"] == pytest.approx(8e7)
    check_no_lookahead(lambda v: base.market_cap(v).drop("shp_date"), ds,
                       [*M.GATE_DATES, close - minute], name="market_cap_issue_size")


@pytest.mark.db
def test_issue_sizes_load_point_in_time_by_symbol(db_conn, monkeypatch):
    """Loaded rows reach the PIT dataset under the company whose NSE symbol it was that
    day; the daily check fetches the trading days after the latest file, and a backfill
    one a month."""
    import types

    import db_market

    from igs.config import load_sources
    from igs.ingest import jobs
    from igs.normalize.load import load_simple
    from igs.normalize.nse import MARKET_CAP_SCHEMA
    from igs.pit.loader import load_dataset
    db_market.load(db_conn)
    day = dt.date(2024, 11, 28)
    rows = pl.DataFrame([(day, "GROW", "EQ", "GROW INDUSTRIES LTD", "Listed", 10.0, 123456789,
                          100.0, 1.23e10),
                         (day, "NOTHERE", "EQ", "UNKNOWN LTD", "Listed", 10.0, 5, 1.0, 5.0),
                         (day, "CYCL", "SM", "CYCLICAL STEEL", "Listed", 10.0, 7, 1.0, 7.0)],
                        schema=MARKET_CAP_SCHEMA, orient="row")
    assert load_simple(db_conn, "nse_issue_size", rows, db_market.FETCH,
                       ["trade_date", "symbol", "series"]) == 3
    db_conn.commit()
    got = load_dataset(db_conn, dt.date(2024, 1, 1), day).tables["issue_sizes"]
    assert got.select("company_id", "trade_date", "shares").rows() == [(1, day, 123456789.0)]

    fetched = []
    monkeypatch.setattr(jobs, "ingest_date", lambda ctx, sid, d: fetched.append((sid, d)))
    monkeypatch.setattr(jobs, "unverified", lambda ctx, spec: None)
    ctx = types.SimpleNamespace(conn=db_conn, sources=load_sources())
    jobs.ingest_market_cap(ctx, dt.date(2024, 12, 4))
    assert fetched == [("nse_market_cap", d) for d in (
        dt.date(2024, 11, 29), dt.date(2024, 12, 2), dt.date(2024, 12, 3),
        dt.date(2024, 12, 4))]
    fetched.clear()
    jobs.backfill_market_cap(ctx, dt.date(2023, 6, 1), dt.date(2024, 4, 10))
    assert [d for _, d in fetched] == [dt.date(2024, 2, 29), dt.date(2024, 3, 29),
                                       dt.date(2024, 4, 10)]

