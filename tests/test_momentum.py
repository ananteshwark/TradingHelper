"""igs.momentum: the 12-1 month momentum rule's paper portfolios, rule and AI-reviewed."""
import datetime as dt

import polars as pl
import pytest

from igs import momentum
from igs.timeutil import IST

D = dt.date


def test_month_starts_and_costs():
    days = [D(2024, 9, 27), D(2024, 9, 30), D(2024, 10, 1), D(2024, 10, 3), D(2024, 11, 1)]
    assert momentum.month_starts(days) == [D(2024, 10, 1), D(2024, 11, 1)]
    # STT both sides, stamp, fees, Rs 40 brokerage, DP charge and 0.1% slippage: ~0.39%.
    assert 0.37 < momentum.cost_pct(1000.0) < 0.41


def _px(scores: dict[int, float], days: int = 260, turnover: float = 1e9):
    """Prices rising so that company c's 12-1 return is scores[c] %."""
    start = D(2023, 1, 2)
    rows = []
    for cid, score in scores.items():
        base, peak = days - 1 - momentum.LOOKBACK, days - 1 - momentum.SKIP
        for i in range(days):
            # Flat to 12 months back, rising to 1 month back, then flat again.
            k = min(max(i - base, 0), peak - base)
            close = 100 * (1 + score / 100 * k / (peak - base))
            rows.append({"company_id": cid, "symbol": f"S{cid}",
                         "trade_date": start + dt.timedelta(days=i), "adj_open": close,
                         "adj_close": close, "turnover": turnover})
    return pl.DataFrame(rows), start + dt.timedelta(days=days - 1)


def test_rank_orders_by_12_1_return_and_drops_the_illiquid_and_short():
    px, last = _px({1: 10.0, 2: 40.0, 3: 25.0})
    px = pl.concat([px, _px({4: 90.0}, turnover=1e6)[0],          # illiquid
                    _px({5: 99.0}, days=200)[0]])                   # under a year of prices
    got = momentum.rank(px, {1, 2, 3, 4, 5}, last)
    assert [(r["company_id"], r["rank"]) for r in got] == [(2, 1), (3, 2), (1, 3)]
    assert got[0]["score_pct"] == pytest.approx(40.0, abs=0.01)
    assert momentum.rank(px, {1, 2}, last)[0]["company_id"] == 2   # members only


def test_the_ai_track_replaces_an_avoided_pick_with_the_next_it_keeps(monkeypatch):
    ranking = [{"company_id": c, "symbol": f"S{c}", "score_pct": 50.0 - c, "rank": c}
               for c in range(1, 6)]
    seen = []

    def review(assistant, conn, run, cand, universe):
        seen.append(cand["symbol"])
        avoid = cand["symbol"] == "S2"
        return {"symbol": cand["symbol"], "rank": cand["rank"],
                "decision": "avoid" if avoid else "keep",
                "reason": "SEBI order in the filings" if avoid else "nothing specific"}

    monkeypatch.setattr(momentum, "review", review)
    picks, reviews, note = momentum.ai_picks(object(), None, {"run_id": 1}, ranking, 3)
    assert [p["symbol"] for p in picks] == ["S1", "S3", "S4"] and note == ""
    assert seen == ["S1", "S2", "S3", "S4"] and reviews[1]["decision"] == "avoid"
    # Without an assistant, or a score run from before the entry: the rule's picks.
    assert momentum.ai_picks(None, None, {"run_id": 1}, ranking, 3)[0] == ranking[:3]
    picks, reviews, note = momentum.ai_picks(object(), None, None, ranking, 3)
    assert picks == ranking[:3] and reviews is None and "postdate" in note

    from igs.assistant.errors import BudgetExceeded

    def broke(assistant, conn, run, cand, universe):
        if cand["symbol"] == "S2":
            raise BudgetExceeded("budget")
        return review(assistant, conn, run, cand, universe)
    monkeypatch.setattr(momentum, "review", broke)
    picks, reviews, note = momentum.ai_picks(object(), None, {"run_id": 1}, ranking, 3)
    assert [p["symbol"] for p in picks] == ["S1", "S2", "S3"]
    assert "stopped after 1" in note and "unreviewed" in note


def _setup(conn):
    import db_market
    db_market.load(conn)
    conn.execute("""insert into index_price (index_name, trade_date, close, is_total_return,
                        source_fetch_id)
                    select 'Nifty 50', trade_date, close, is_total_return, source_fetch_id
                    from index_price where index_name = 'Nifty 500'""")
    for cid in range(1, 6):
        conn.execute("insert into index_member (index_name, as_of, company_id) values "
                     "('NIFTY 200', '2024-01-01', %s)", (cid,))
    # Prices to 29 November, the later half held back: the tracker first runs in October.
    conn.execute("delete from price_eod where trade_date > '2024-11-29'")
    conn.execute("create temp table later as select * from price_eod "
                 "where trade_date > '2024-10-15'")
    conn.execute("delete from price_eod where trade_date > '2024-10-15'")
    conn.commit()


@pytest.mark.db
def test_paper_portfolios_rebalance_monthly_and_close_each_period(db_conn):
    _setup(db_conn)
    sent = []
    out = momentum.run(db_conn, top=2, min_turnover=0, notify=sent.append)
    assert out == "rebalanced 1 month(s); marked to 15 Oct 2024"
    rebal = db_conn.execute("""select track, signal_date, entry_date, jsonb_array_length(holdings),
                                      reviews is null, note from momentum_rebalance
                               order by track""").fetchall()
    assert [r[:5] for r in rebal] == [("ai", D(2024, 9, 30), D(2024, 10, 1), 2, True),
                                      ("rule", D(2024, 9, 30), D(2024, 10, 1), 2, True)]
    assert "assistant off" in rebal[0][5]
    period = db_conn.execute("""select complete, end_date, bought, nifty_pct is not null,
                                       basket_pct is not null from momentum_period
                                where track = 'rule'""").fetchone()
    assert period == (False, D(2024, 10, 15), 2, True, True)
    assert sent and sent[0].startswith("MOMENTUM PAPER PORTFOLIOS · Oct 2024")
    assert momentum.run(db_conn, top=2, min_turnover=0, notify=sent.append) == (
        "marked to 15 Oct 2024") and len(sent) == 1                     # nothing new

    db_conn.execute("insert into price_eod select * from later")
    db_conn.commit()
    out = momentum.run(db_conn, top=2, min_turnover=0, notify=sent.append)
    assert out == "rebalanced 1 month(s); marked to 29 Nov 2024"
    rows = db_conn.execute("""select start_date, end_date, complete,
                                     (portfolio_gross_pct - portfolio_net_pct)::float8
                              from momentum_period where track = 'rule'
                              order by start_date""").fetchall()
    # October closed open to open at 1 November; its two buys paid the round trip.
    assert rows[0][:3] == (D(2024, 10, 1), D(2024, 11, 1), True)
    assert rows[0][3] == pytest.approx(momentum.cost_pct(100.0), abs=0.2)
    assert rows[1][0] == D(2024, 11, 1) and rows[1][2] is False
    res = momentum.results(db_conn)
    assert res["rule"]["total"]["net_pct"] is not None and len(res["ai"]["periods"]) == 2
    assert "Since the start: rule" in sent[-1]


@pytest.mark.db
def test_the_ai_reviews_with_the_signal_dates_evidence(db_conn, monkeypatch):
    _setup(db_conn)
    calls = []

    class FakeAssistant:
        def structured(self, feature, *, system, prompt, schema):
            calls.append((feature, prompt))
            avoid = "GROW" in prompt.split("ranks")[0]
            return ({"decision": "avoid" if avoid else "keep",
                     "reason": "Promoter pledge rose sharply" if avoid else "Nothing specific"},
                    None)

    run_row = {"run_id": 1, "as_of": dt.datetime(2024, 9, 30, 23, 59, tzinfo=IST)}
    monkeypatch.setattr(momentum, "_signal_run", lambda conn, signal: run_row)
    monkeypatch.setattr("igs.assistant.calls.gather",
                        lambda conn, run, symbol: (symbol, 1, {"symbol": symbol}))
    momentum.run(db_conn, assistant=FakeAssistant(), top=2, min_turnover=0)
    ai, rule = (db_conn.execute("select holdings, reviews from momentum_rebalance "
                                "where track = %s", (t,)).fetchone() for t in ("ai", "rule"))
    assert calls and all(f == "momentum" for f, _ in calls)
    assert "As of 30 Sep 2024" in calls[0][1]
    avoided = [r["symbol"] for r in ai[1] if r["decision"] == "avoid"]
    held = {h["symbol"] for h in ai[0]}
    assert avoided == (["GROW"] if "GROW" in {h["symbol"] for h in rule[0]} else [])
    assert "GROW" not in held and len(held) == 2


@pytest.mark.db
def test_the_page_shows_holdings_reviews_and_each_month(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest
    _setup(db_conn)
    momentum.run(db_conn, top=2, min_turnover=0)
    db_conn.execute("insert into price_eod select * from later")
    db_conn.commit()
    momentum.run(db_conn, top=2, min_turnover=0)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at = AppTest.from_string('''from igs.db import connect
from igs.ui.momentum import page
page(connect(autocommit=True))''', default_timeout=30).run()
    assert not at.exception, at.exception
    assert [m.label for m in at.metric] == ["Rule, since the start",
                                           "AI-reviewed, since the start",
                                           "Nifty 200 basket", "Nifty 50"]
    holdings, months = at.dataframe[0].value, at.dataframe[1].value
    assert len(holdings) == 2 and "since entry %" in holdings.columns
    assert list(months["status"]) == ["open (to the latest close)", "closed"]
