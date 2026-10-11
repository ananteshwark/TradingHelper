"""igs.momentum: the 12-1 month momentum rule's paper portfolios: rule, AI-reviewed,
skipping bad results and risk-weighted."""
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


def test_risk_weights_are_inverse_to_recent_volatility():
    """A stock moving 3% a day gets a third of the weight of one moving 1%; one without
    enough history gets the median's weight."""
    rows = []
    for cid, move in ((1, 0.01), (2, 0.03)):
        close = 100.0
        for i in range(80):
            close *= 1 + (move if i % 2 else -move)
            rows.append({"company_id": cid, "symbol": f"S{cid}",
                         "trade_date": D(2024, 1, 1) + dt.timedelta(days=i),
                         "adj_close": close})
    rows += [{"company_id": 3, "symbol": "S3", "trade_date": D(2024, 3, 20), "adj_close": 50.0}]
    px = pl.DataFrame(rows)
    ranking = [{"company_id": c, "symbol": f"S{c}", "score_pct": 10.0 - c, "rank": c}
               for c in (1, 2, 3)]
    held, reviews, note = momentum.risk_picks(px, D(2024, 3, 20), ranking, 2)
    assert reviews is None and [h["symbol"] for h in held] == ["S1", "S2"]
    assert [h["weight"] for h in held] == pytest.approx([0.75, 0.25], abs=1e-3)
    assert held[1]["vol_pct"] == pytest.approx(3 * held[0]["vol_pct"], rel=0.01)
    assert note == "Weights by inverse 63-session volatility: from S1 75% to S2 25%."
    held, _, _ = momentum.risk_picks(px, D(2024, 3, 20), ranking, 3)
    assert held[2]["vol_pct"] is None
    assert held[2]["weight"] == pytest.approx(held[1]["weight"])   # the median of 1% and 3%


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
                                      ("hedged", D(2024, 9, 30), D(2024, 10, 1), 2, True),
                                      ("results", D(2024, 9, 30), D(2024, 10, 1), 2, True),
                                      ("risk", D(2024, 9, 30), D(2024, 10, 1), 2, True),
                                      ("rule", D(2024, 9, 30), D(2024, 10, 1), 2, True)]
    assert "assistant off" in rebal[0][5]
    assert rebal[2][5] == "No stock in the top ten was flagged."   # no results recorded
    assert "No F&O lot list loaded yet" in rebal[1][5]             # hedged, without the list
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
    assert res["results"]["total"] == res["rule"]["total"]          # nothing was flagged
    # The risk track holds the rule's stocks; its result is their weighted average.
    risk, rule = (db_conn.execute("select holdings from momentum_rebalance where track = %s "
                                  "order by signal_date desc limit 1", (t,)).fetchone()[0]
                  for t in ("risk", "rule"))
    assert {h["symbol"] for h in risk} == {h["symbol"] for h in rule}
    assert sum(h["weight"] for h in risk) == pytest.approx(1, abs=1e-3)
    p = res["risk"]["periods"][0]
    assert p["gross_pct"] == pytest.approx(
        sum(d["return_pct"] * d["weight_pct"] for d in p["detail"])
        / sum(d["weight_pct"] for d in p["detail"]), abs=0.02)
    assert "Since the start: rule" in sent[-1] and "skipping bad results" in sent[-1]


@pytest.mark.db
def test_a_track_added_later_starts_at_the_next_rebalance(db_conn):
    """An install that already holds the rule and AI tracks gains 'results' and 'risk' at the
    next month start, and the totals say so."""
    _setup(db_conn)
    momentum.run(db_conn, top=2, min_turnover=0)
    db_conn.execute("delete from momentum_period where track in ('results', 'risk')")
    db_conn.execute("delete from momentum_rebalance where track in ('results', 'risk')")
    db_conn.commit()
    assert momentum.run(db_conn, top=2, min_turnover=0) == "marked to 15 Oct 2024"
    db_conn.execute("insert into price_eod select * from later")
    db_conn.commit()
    sent = []
    momentum.run(db_conn, top=2, min_turnover=0, notify=sent.append)
    starts = {t: r["periods"][0]["start"] for t, r in momentum.results(db_conn).items()}
    assert starts == {"rule": D(2024, 10, 1), "ai": D(2024, 10, 1), "results": D(2024, 11, 1),
                      "risk": D(2024, 11, 1), "hedged": D(2024, 10, 1)}
    assert "skipping bad results (from Nov 2024) " in sent[-1]
    assert "risk-weighted (from Nov 2024) " in sent[-1]
    held = next(x for x in sent[-1].splitlines() if x.startswith("Rule, risk-weighted: holds"))
    assert held.count("%") == 2                                     # each stock with its weight


@pytest.mark.db
def test_the_hedged_track_holds_stocks_with_futures_and_sells_the_nifty(db_conn):
    """Only stocks on NSE's F&O lot list; the Nifty sold at their beta; a futures round trip
    on every position each month and 6% a year on the cash."""
    import db_market
    _setup(db_conn)
    for sym, kind, lot in (("GROW", "stock", 500), ("BANK", "stock", 300),
                           ("NBFC", "stock", 200), ("NIFTY", "index", 25)):
        db_conn.execute("""insert into fno_lot (as_of, symbol, kind, month, lot, source_fetch_id)
                           values ('2024-09-02', %s, %s, '2024-10-01', %s, %s)""",
                        (sym, kind, lot, db_market.FETCH))
    db_conn.commit()
    sent = []
    momentum.run(db_conn, top=2, min_turnover=0, notify=sent.append)
    holdings, note = db_conn.execute("""select holdings, note from momentum_rebalance
                                        where track = 'hedged'""").fetchone()
    rule = db_conn.execute("select holdings from momentum_rebalance where track = 'rule'"
                           ).fetchone()[0]
    ranked = [h["symbol"] for h in rule]
    assert {h["symbol"] for h in holdings} <= {"GROW", "BANK", "NBFC"} and len(holdings) == 2
    beta = holdings[0]["hedge_beta"]
    assert 0.5 <= beta <= 1.5 and all(h["hedge_beta"] == beta for h in holdings)
    assert note.startswith(f"Short Nifty futures at {beta:.2f} times the long value")
    if "CYCL" in ranked or "GAPS" in ranked:
        assert "Skipped without futures:" in note
    assert "To trade it with one lot of each:" in note and "Nifty lot" in note
    assert f"short Nifty futures at {beta:.2f} times the long value" in sent[0]
    p = db_conn.execute("""select start_date, end_date, portfolio_gross_pct::float8,
                                  portfolio_net_pct::float8, detail from momentum_period
                           where track = 'hedged'""").fetchone()
    cash = ((1 + momentum.CASH_YEAR) ** ((p[1] - p[0]).days / 365) - 1) * 100
    assert p[3] == pytest.approx(p[2] - momentum.STOCK_FUT_COST
                                 - beta * momentum.INDEX_FUT_COST + cash, abs=0.002)
    short = next(d for d in p[4] if d["symbol"] == "NIFTY futures (short)")
    longs = [d for d in p[4] if d["symbol"] != "NIFTY futures (short)"]
    assert short["weight_pct"] == pytest.approx(-beta * 100, abs=0.1)
    assert p[2] == pytest.approx(sum(d["return_pct"] for d in longs) / len(longs)
                                 - beta * short["return_pct"], abs=0.02)


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
                                           "Skipping bad results", "Risk-weighted", "Hedged",
                                           "Nifty 200 basket", "Nifty 50"]
    holdings, months = at.dataframe[0].value, at.dataframe[1].value
    assert len(holdings) == 2 and "since entry %" in holdings.columns
    assert list(months["status"]) == ["open (to the latest close)", "closed"]
