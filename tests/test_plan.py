"""igs.plan: the Plan paper portfolio of momentum stocks, gold and cash, in rupees."""
import datetime as dt

import pytest

from igs import momentum, plan
from igs.config import PlanConfig
from igs.timeutil import IST

D = dt.date
CFG = PlanConfig(capital_inr=1_000_000, core_pct=60, gold_pct=25, cash_pct=15, drift_points=5,
                 gold_symbol="GOLDBEES", cash_return_pct=6.0, stocks=2, max_stock_pct=60)


def test_charges_match_the_momentum_round_trip_and_gold_pays_no_stt():
    # Rs 1 lakh of a Rs 1,000 stock bought and sold: what igs.momentum.cost_pct says.
    both = plan.charges("stock", "buy", 1e5) + plan.charges("stock", "sell", 1e5)
    assert both == pytest.approx(momentum.cost_pct(1000.0) / 100 * 1e5, abs=0.02)
    assert plan.charges("stock", "buy", 1e5) - plan.charges("gold", "buy", 1e5) == (
        pytest.approx(100 + 30, abs=0.01))               # STT 0.1%; slippage 0.05% v 0.02%
    assert plan.charges("stock", "sell", 0) == 0.0


def test_rupees_in_lakhs_and_crores():
    assert plan.inr(1023450) == "₹10,23,450"
    assert plan.inr(12345678.4) == "₹1,23,45,678"
    assert plan.inr(999) == "₹999"
    assert plan.inr(-150000) == "-₹1,50,000"


def test_weights_are_capped_and_the_excess_spread_in_proportion():
    assert plan.cap_weights([5, 3, 2], 0.4) == pytest.approx([0.4, 0.36, 0.24])
    assert plan.cap_weights([1, 1], 0.6) == pytest.approx([0.5, 0.5])
    # Fewer stocks than the cap allows for: each at the cap, the rest stays in cash.
    assert plan.cap_weights([3, 1, 1], 0.15) == pytest.approx([0.15, 0.15, 0.15])


def test_orders_are_due_at_each_month_end_and_on_drift():
    state = {"value": 100.0, "cash": 15.0,
             "holdings": [{"kind": "gold", "value_inr": 25.0},
                          {"kind": "stock", "value_inr": 60.0}]}
    due = plan._due
    assert due(CFG, state, None, D(2024, 10, 30), D(2024, 10, 31)) is None
    assert due(CFG, state, None, D(2024, 10, 31), D(2024, 11, 1)) == ("monthly", False)
    assert due(CFG, state, None, D(2024, 12, 31), D(2025, 1, 1)) == ("quarterly", True)
    crash = {**state, "value": 70.0, "holdings": [{"kind": "gold", "value_inr": 25.0},
                                                  {"kind": "stock", "value_inr": 30.0}]}
    assert due(CFG, crash, None, D(2024, 10, 30), D(2024, 10, 31)) == ("drift", True)
    assert due(CFG, crash, None, D(2024, 10, 31), D(2024, 11, 1)) == ("monthly", True)


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
    # Gold flat at Rs 50, so only the stocks move the parts' shares.
    conn.execute("""insert into price_eod (exchange, trade_date, isin, symbol, series, open,
                        high, low, close, volume, source_fetch_id)
                    select distinct 'NSE', trade_date, 'INF204KB17I5', 'GOLDBEES', 'EQ', 50, 50,
                           50, 50, 1000000, %s from price_eod""", (db_market.FETCH,))
    conn.execute("delete from price_eod where trade_date > '2024-11-29'")
    conn.execute("create temp table later as select * from price_eod "
                 "where trade_date > '2024-10-15'")
    conn.execute("delete from price_eod where trade_date > '2024-10-15'")
    conn.commit()


def _day(conn, day):
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from plan_day where trade_date = %s", (day,))
        return cur.fetchone()


@pytest.mark.db
def test_the_plan_starts_fills_at_the_open_and_rebalances_each_month(db_conn):
    _setup(db_conn)
    sent = []
    out = plan.run(db_conn, cfg=CFG, min_turnover=0, notify=sent.append)
    assert out == "value ₹10,00,000 at 15 Oct 2024; orders for the 16 Oct open"
    reb = plan.rebalances(db_conn)
    assert len(reb) == 1 and reb[0]["reason"] == "start" and reb[0]["all_parts"]
    orders = reb[0]["orders"]
    assert {o["side"] for o in orders} == {"buy"} and len(orders) == 3
    gold = next(o for o in orders if o["kind"] == "gold")
    assert gold["shares"] == 5000                        # Rs 2.5 lakh at Rs 50
    stock_value = sum(o["shares"] * o["price"] for o in orders if o["kind"] == "stock")
    assert stock_value == pytest.approx(600_000, rel=0.02)
    assert sent[0].startswith("THE PLAN · orders for the 16 Oct open")
    assert "BUY 5000 GOLDBEES (about ₹2,50,000): gold to its share" in sent[0]
    assert "Capital ₹10,00,000." in sent[0]
    assert "After them: stocks" in sent[0] and "cash ₹1,5" in sent[0]
    assert plan.run(db_conn, cfg=CFG, min_turnover=0) == "up to date at 15 Oct 2024"

    db_conn.execute("insert into price_eod select * from later")
    db_conn.commit()
    out = plan.run(db_conn, cfg=CFG, min_turnover=0, notify=sent.append)
    assert out.endswith("orders for the 02 Dec open") and len(sent) == 2
    start = {r["signal_date"]: r for r in plan.rebalances(db_conn)}[D(2024, 10, 15)]
    assert start["filled_on"] == D(2024, 10, 16)
    assert all(o["filled"] == o["shares"] and o["fill_price"] for o in start["orders"])
    first = _day(db_conn, D(2024, 10, 16))
    spent = sum(o["filled"] * o["fill_price"] for o in start["orders"])
    fees = sum(o["charges_inr"] for o in start["orders"])
    assert float(first["charges_inr"]) == pytest.approx(fees, abs=0.01)
    assert float(first["cash_inr"]) == pytest.approx(
        1_000_000 * 1.06 ** (1 / 365) - spent - fees, abs=0.02)
    gold_value = next(h["value_inr"] for h in first["holdings"] if h["kind"] == "gold")
    assert gold_value / float(first["value_inr"]) == pytest.approx(0.25, abs=0.01)
    reasons = {r["signal_date"]: r["reason"] for r in plan.rebalances(db_conn)}
    assert reasons[D(2024, 10, 31)] == "monthly" and reasons[D(2024, 11, 29)] == "monthly"
    assert set(reasons.values()) <= {"start", "monthly", "drift"}
    days = plan.history(db_conn)
    assert all(abs(h["value"] / 10_000 - h["unit_value"]) < 1e-3 for h in days)  # no flows
    s = plan.summary(db_conn)
    assert s["trade_date"] == D(2024, 11, 29) and s["start"] == D(2024, 10, 15)
    assert s["since_pct"] == pytest.approx(days[-1]["unit_value"] - 100, abs=1e-6)
    assert s["nifty_pct"] is not None and sum(s["shares"].values()) == pytest.approx(100)
    pending = [r for r in plan.rebalances(db_conn) if r["filled_on"] is None]
    assert [r["session_date"] for r in pending] == [D(2024, 12, 2)]


@pytest.mark.db
def test_bonus_shares_dividends_and_money_added(db_conn):
    """A 1:1 bonus doubles the shares without changing the value; a dividend is credited
    on its ex-date; money added buys units at the last close and, lifting cash above its
    share, brings orders to invest it at the next open."""
    import db_market
    _setup(db_conn)
    plan.run(db_conn, cfg=CFG, min_turnover=0)
    stocks = [o for o in plan.rebalances(db_conn)[0]["orders"] if o["kind"] == "stock"]
    bonus, div = stocks[0]["company_id"], stocks[1]["company_id"]
    db_conn.execute("""update later set open = open / 2, high = high / 2, low = low / 2,
                           close = close / 2, prev_close = prev_close / 2
                       where isin = %s and trade_date >= '2024-10-21'""", (db_market.isin(bonus),))
    for cid, kind, ex, ra, rb, cash in ((bonus, "bonus", "2024-10-21", 1, 1, None),
                                        (div, "dividend", "2024-10-24", None, None, 5.0)):
        db_conn.execute("""insert into corporate_action (exchange, symbol, isin, security_id,
                               action_type, ex_date, announced_at, ratio_a, ratio_b,
                               cash_per_share, subject, source_fetch_id)
                           values ('NSE', %s, %s, %s, %s, %s, '2024-10-01', %s, %s, %s, %s, %s)""",
                        (db_market.SYMBOLS[cid], db_market.isin(cid), 100 + cid, kind, ex, ra,
                         rb, cash, kind, db_market.FETCH))
    db_conn.execute("insert into price_eod select * from later where trade_date <= '2024-11-05'")
    db_conn.commit()
    plan.run(db_conn, cfg=CFG, min_turnover=0)

    def shares(day, cid):
        return next(h["shares"] for h in _day(db_conn, day)["holdings"]
                    if h["company_id"] == cid)
    before, after = _day(db_conn, D(2024, 10, 18)), _day(db_conn, D(2024, 10, 21))
    assert shares(D(2024, 10, 21), bonus) == 2 * shares(D(2024, 10, 18), bonus)
    held_value = [next(h["value_inr"] for h in d["holdings"] if h["company_id"] == bonus)
                  for d in (before, after)]
    assert held_value[1] == pytest.approx(held_value[0], rel=0.15)   # one day's move, not -50%
    paid = _day(db_conn, D(2024, 10, 24))
    assert float(paid["dividends_inr"]) == pytest.approx(5.0 * shares(D(2024, 10, 23), div))

    db_conn.execute("insert into plan_flow (amount_inr, note, created_at) values "
                    "(200000, 'monthly saving', %s)",
                    (dt.datetime(2024, 11, 5, 18, 0, tzinfo=IST),))
    db_conn.execute("insert into price_eod select * from later where trade_date > '2024-11-05'")
    db_conn.commit()
    plan.run(db_conn, cfg=CFG, min_turnover=0)
    prev, day = _day(db_conn, D(2024, 11, 5)), _day(db_conn, D(2024, 11, 6))
    assert float(day["flows_inr"]) == 200000
    assert float(day["units"]) == pytest.approx(
        float(prev["units"]) + 200000 / float(prev["unit_value"]), rel=1e-9)
    reb = {r["signal_date"]: r for r in plan.rebalances(db_conn)}[D(2024, 11, 6)]
    assert reb["reason"] == "drift" and reb["session_date"] == D(2024, 11, 7)
    assert {o["side"] for o in reb["orders"]} == {"buy"}            # the new cash invested
    assert db_conn.execute("select applied_on from plan_flow").fetchone()[0] == D(2024, 11, 6)


@pytest.mark.db
def test_a_stock_with_a_serious_red_flag_or_bad_results_is_left_out(db_conn, monkeypatch):
    _setup(db_conn)
    px, _, _ = plan._load(db_conn, CFG, D(2023, 6, 1), D(2024, 10, 15))
    ids, _ = momentum.members(db_conn, D(2024, 10, 15))
    ranked = momentum.rank(px, ids, D(2024, 10, 15), min_turnover=0)
    top, second = ranked[0], ranked[1]
    monkeypatch.setattr(plan, "red_flags", lambda conn, day: {top["company_id"]: "promoter pledge"})
    monkeypatch.setattr("igs.results_drift.flagged", lambda conn, at: {
        second["company_id"]: {"abnormal_pct": -7.4}})
    picks, why, note = plan.core_picks(db_conn, CFG, px, D(2024, 10, 15), min_turnover=0)
    assert [p["company_id"] for p in picks] == [r["company_id"] for r in ranked[2:4]]
    assert why[top["company_id"]] == "red flag: promoter pledge"
    assert note == (f"Left out: {top['symbol']} (red flag: promoter pledge), "
                    f"{second['symbol']} (bad results reaction (-7.4%)).")
    assert sum(p["weight"] for p in picks) == pytest.approx(1)


@pytest.mark.db
def test_the_page_shows_the_plan(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest
    _setup(db_conn)
    monkeypatch.setattr(plan, "load_plan", lambda: CFG)
    monkeypatch.setattr("igs.ui.plan.load_plan", lambda: CFG)
    plan.run(db_conn, cfg=CFG, min_turnover=0)
    db_conn.execute("insert into price_eod select * from later")
    db_conn.commit()
    plan.run(db_conn, cfg=CFG, min_turnover=0)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at = AppTest.from_string('''from igs.db import connect
from igs.ui.plan import page
page(connect(autocommit=True))''', default_timeout=30).run()
    assert not at.exception, at.exception
    assert [m.label for m in at.metric] == ["Value", "Since 15 Oct 2024",
                                           "Nifty 50, same period", "Stocks / gold / cash",
                                           "Marked to"]
    assert at.subheader[0].value == "Orders for the 02 Dec 2024 open"
    holdings = next(d.value for d in at.dataframe if "part" in d.value.columns)
    assert list(holdings["part"]).count("cash") == 1 and "GOLDBEES" in list(holdings["symbol"])
