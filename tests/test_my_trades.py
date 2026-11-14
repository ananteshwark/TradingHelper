"""Your own trades on Upstox, offline: mock transports stand in for Upstox, a fake feed for
its candles. Bets must run flat to flat after charges, the app's own orders must be left
out, the account must only be read, and each note must go once."""

from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from igs import mytrades as mt
from igs.config import load_costs
from igs.intraday.costs import charges as intraday_charges
from igs.intraday.costs import intraday_rates
from igs.intraday.engine import Candle
from igs.timeutil import IST

APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"
DAY = dt.date(2024, 11, 29)                       # the synthetic market's last day, a Friday
GROW = "NSE_EQ|INE001X01010"


def at(day: dt.date, hhmm: str) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)


def fill(side, qty, price, day=DAY, time="10:00", *, product="I", isin="INE001X01010",
         segment="EQ", option=None, order=None, tid=None, source="day"):
    tid = tid or f"{day}{time}{side}{qty}{price}"
    return {"exchange": "NSE", "segment": segment, "trade_date": day, "trade_id": tid,
            "traded_at": at(day, time) if source == "day" else None,
            "instrument_key": f"NSE_{segment}|{isin}", "symbol": "GROW", "isin":
            isin if segment == "EQ" else None, "expiry": None, "strike": None,
            "option_type": option, "product": product if source == "day" else None,
            "side": side, "quantity": qty, "price": Decimal(str(price)),
            "order_id": order or (tid if source == "day" else None), "order_type": "LIMIT",
            "source": source}


def day_row(side, qty, price, stamp, tid, key=GROW, product="I", symbol="GROW-EQ"):
    return {"exchange": "NSE", "product": product, "trading_symbol": symbol,
            "tradingsymbol": symbol, "instrument_token": key, "order_type": "MARKET",
            "transaction_type": side.upper(), "quantity": qty, "exchange_order_id": f"x{tid}",
            "order_id": f"O{tid}", "exchange_timestamp": stamp, "average_price": price,
            "trade_id": tid, "order_ref_id": "r", "order_timestamp": stamp}


# --------------------------------------------------------------------------- reading


def test_upstox_rows_are_read_as_fills():
    f = mt.day_fill(day_row("buy", 100, 102.5, "29-Nov-2024 10:42:10", "T1"))
    assert (f["segment"], f["isin"], f["side"], f["quantity"], f["price"], f["product"]) == (
        "EQ", "INE001X01010", "buy", 100, Decimal("102.5"), "I")
    assert f["traded_at"] == dt.datetime(2024, 11, 29, 10, 42, 10, tzinfo=IST)
    option = mt.day_fill(day_row("sell", 75, 120.0, "2024-11-29 11:00:00", "T2",
                                 key="NSE_FO|43210", product="D", symbol="NIFTY24DEC24000CE"))
    assert (option["segment"], option["option_type"], option["isin"]) == ("FO", "CE", None)
    assert mt.day_fill(day_row("buy", 1, 1.0, "29-Nov-2024 10:00:00", "T3",
                               key="NSE_INDEX|Nifty 50")) is None
    with pytest.raises(ValueError):
        mt.day_fill({**day_row("buy", 1, 1.0, "", "T4"), "order_timestamp": ""})
    hist = mt.history_fill({"exchange": "NFO", "segment": "FO", "option_type": "PE",
                            "quantity": 50, "amount": 5000.0, "trade_id": "H1",
                            "trade_date": "2024-11-12", "transaction_type": "SELL",
                            "scrip_name": "NIFTY", "strike_price": "23500.0",
                            "expiry": "2024-11-14", "price": 100.0, "isin": "",
                            "symbol": "NIFTY", "instrument_token": ""})
    assert (hist["option_type"], hist["strike"], hist["expiry"], hist["traded_at"],
            hist["product"]) == ("PE", 23500.0, dt.date(2024, 11, 14), None, None)
    assert mt.history_fill({"segment": "MF", "trade_id": "M1"}) is None
    assert mt.history_fill({"segment": "EQ", "trade_id": ""}) is None


def test_the_history_starts_three_financial_years_back():
    assert mt.history_start(dt.date(2026, 10, 30)) == dt.date(2024, 4, 1)
    assert mt.history_start(dt.date(2026, 2, 1)) == dt.date(2023, 4, 1)
    assert list(mt._months(dt.date(2024, 11, 15), dt.date(2025, 1, 10))) == [
        (dt.date(2024, 11, 15), dt.date(2024, 11, 30)), (dt.date(2024, 12, 1),
                                                          dt.date(2024, 12, 31)),
        (dt.date(2025, 1, 1), dt.date(2025, 1, 10))]


# --------------------------------------------------------------------------- bets


def test_a_bet_runs_flat_to_flat_and_a_reversal_starts_the_next():
    bets, unmatched = mt.build([
        fill("buy", 50, 100, time="10:00"), fill("buy", 50, 101, time="10:05"),
        fill("sell", 60, 102, time="10:30"), fill("sell", 140, 103, time="10:40"),
        fill("buy", 100, 102, time="11:00")])
    assert unmatched == 0 and [b.side for b in bets] == ["long", "short"]
    long, short = bets
    assert (long.quantity, long.entry, long.closed_at) == (100, Decimal("100.5"),
                                                           at(DAY, "10:40"))
    assert long.gross == 60 * 102 + 40 * 103 - (50 * 100 + 50 * 101)
    assert (short.quantity, short.entry, short.exit, short.gross) == (
        100, Decimal(103), Decimal(102), Decimal(100))
    assert short.minutes == 20 and long.minutes == 40


def test_delivery_spans_days_and_never_sells_short():
    d1, d2 = DAY - dt.timedelta(days=20), DAY - dt.timedelta(days=4)
    bets, unmatched = mt.build([
        fill("sell", 5, 99, day=d1 - dt.timedelta(days=1), product="D"),   # bought before
        fill("buy", 10, 100, day=d1, product="D"), fill("sell", 10, 110, day=d2, product="D")])
    [b] = bets
    assert unmatched == 1 and (b.kind, b.side, b.days, b.gross) == ("delivery", "long", 16,
                                                                    Decimal(100))


def test_a_history_day_round_trip_has_no_side_and_options_sell_short():
    bets, _ = mt.build([
        fill("buy", 50, 200, source="history"), fill("sell", 50, 199, source="history"),
        fill("sell", 50, 100, segment="FO", isin="43210", option="CE", time="10:00"),
        fill("buy", 50, 40, segment="FO", isin="43210", option="CE",
             day=DAY + dt.timedelta(days=3), time="11:00")])
    by = {b.kind: b for b in bets}
    assert (by["intraday"].side, by["intraday"].gross, by["intraday"].opened_at) == (
        None, Decimal(-50), None)
    assert (by["fno"].side, by["fno"].option, by["fno"].gross) == ("short", True, Decimal(3000))


def test_charges_follow_the_costs_config():
    costs = load_costs()
    [b] = mt.build([fill("buy", 98, 101.35), fill("sell", 98, 102.25, time="11:00")])[0]
    assert mt.charges(b, costs) == intraday_charges(98, 101.35, 102.25, "buy", intraday_rates())
    [d] = mt.build([fill("buy", 10, 100, product="D"),
                    fill("sell", 10, 110, day=DAY + dt.timedelta(days=9), product="D")])[0]
    st = costs.statutory
    expected = (Decimal(1000) * Decimal(str(st.stt_buy_pct)) / 100
                + Decimal(1100) * Decimal(str(st.stt_sell_pct)) / 100)
    assert mt.charges(d, costs) > expected + Decimal(str(st.dp_charge_inr_per_sell))
    [o] = mt.build([fill("sell", 50, 100, segment="FO", isin="1", option="CE"),
                    fill("buy", 50, 40, segment="FO", isin="1", option="CE", time="11:00")])[0]
    stt = Decimal(5000) * Decimal(str(costs.fno.options.stt_sell_pct)) / 100
    assert stt + 40 < mt.charges(o, costs) < stt + 60          # ₹40 brokerage, GST, fees


# --------------------------------------------------------------------------- the record


def _bet(i: int, net: float, *, move: float = 3.0, day=None) -> mt.Bet:
    day = day or dt.date(2024, 1, 1) + dt.timedelta(days=i)
    b = mt.Bet(key=f"k{i}", kind="intraday", segment="EQ", symbol="X", instrument_key="k",
               isin="I", side="long", opened_on=day, opened_at=at(day, "10:00"),
               source="day", closed_on=day, closed_at=at(day, "10:30"), quantity=10,
               buy_qty=10, sell_qty=10, buy_value=Decimal(1000),
               sell_value=Decimal(1000) + Decimal(str(net)) + 20)
    b.charges = Decimal(20)
    b.context = {"day_move_pct": move}
    return b


def test_a_group_that_stood_out_is_checked_on_the_later_bets():
    # Chasing a 2%+ rise lost in the earlier bets and kept losing; buying quiet stocks won
    # (fewer than 15 earlier bets: not tested).
    chase = [_bet(i, -100 - 10 * (i % 3)) for i in range(45)]
    quiet = [_bet(50 + i, 50 + i % 4, move=0.5) for i in range(12)]
    record = mt.patterns(chase + quiet)
    found = {(p["group"], p["value"]): p for p in record}
    p = found[("The day's move before it", "with a 2%+ move")]
    assert p["avg_net_inr"] < 0 and p["t"] < -2 and p["held"] == "held"
    assert ("The day's move before it", "under 2%") not in found
    s = mt.summary(chase)
    assert (s["bets"], s["won_pct"], s["charges_inr"]) == (45, 0.0, 900.0)
    # Reversed in the later bets: it didn't hold.
    later = [_bet(200 + i, 120, day=dt.date(2025, 1, 1) + dt.timedelta(days=i))
             for i in range(15)]
    p = {(x["group"], x["value"]): x for x in mt.patterns(chase + later)}[
        ("The day's move before it", "with a 2%+ move")]
    assert p["held"] == "didn't hold"


# --------------------------------------------------------------------------- the account


def _client_env(monkeypatch, trading="trade-token", analytics="analytics-token"):
    monkeypatch.setenv("UPSTOX_TRADING_TOKEN", trading)
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", analytics)


@pytest.mark.db
def test_the_analytics_token_reads_when_the_trading_one_is_refused(db_conn, monkeypatch):
    _client_env(monkeypatch)
    used = []

    def handler(req):
        used.append(req.headers["authorization"])
        assert req.method == "GET" and req.url.path == mt.DAY_TRADES
        if req.headers["authorization"] == "Bearer trade-token":
            return httpx.Response(401, json={"status": "error", "errors": [
                {"errorCode": "UDAPI100050"}]})
        return httpx.Response(200, json={"status": "success", "data": [
            day_row("buy", 10, 100.0, "29-Nov-2024 10:00:00", "T1")]})
    seen, added = mt.with_account(lambda a: mt.import_day(db_conn, a, at(DAY, "10:05")),
                                  httpx.MockTransport(handler))
    assert (seen, added) == (1, 1)
    assert used == ["Bearer trade-token", "Bearer analytics-token"]
    monkeypatch.delenv("UPSTOX_TRADING_TOKEN")
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN")
    with pytest.raises(mt.AccountError, match="No Upstox token"):
        mt.with_account(lambda a: None)


class FakeFeed:
    def __init__(self, bars: dict) -> None:
        self.bars = bars

    def candles(self, key):
        return self.bars.get(key, [])

    def candles_between(self, key, start, end):
        return self.bars.get(key, [])

    def close(self):
        pass


def bars(day: dt.date, start: float, step: float) -> list[Candle]:
    out, px = [], start
    t = at(day, "09:15")
    while t.time() < dt.time(15, 30):
        out.append(Candle(t, px, px + step + 0.2, px - 0.2, px + step, 1000.0))
        px += step
        t += dt.timedelta(minutes=5)
    return out


@pytest.mark.db
def test_each_bet_gets_a_note_when_it_opens_and_closes_and_the_day_a_summary(
        db_conn, monkeypatch):
    import db_market
    db_market.load(db_conn)
    _client_env(monkeypatch)
    base = round(mt._prev_close(db_conn, "INE001X01010", DAY), 2)
    entry, exit_ = round(base + 18, 2), round(base + 19, 2)     # candles rise ₹1 each
    rows = [day_row("buy", 100, entry, "29-Nov-2024 10:42:10", "T1")]

    def handler(req):
        assert req.method == "GET"
        return httpx.Response(200, json={"status": "success", "data": rows})
    transport = httpx.MockTransport(handler)
    feed = FakeFeed({GROW: bars(DAY, base, 1.0), mt.NIFTY: bars(DAY, 24000.0, 1.0)})
    sent: list[str] = []

    def send(text):
        sent.append(text)
        return True

    def run(hhmm):
        return mt.step(db_conn, send=send, now=at(DAY, hhmm), transport=transport, feed=feed)

    assert run("10:45").startswith("my trades: 1 fills today (1 new), 1 bets today")
    [opened] = sent
    assert opened.startswith(f"📒 You opened: BUY 100 GROW (intraday) at ₹{entry:,.2f}, 10:42")
    # 30 minutes before 10:42: the close of the 10:05 candle, base + 11.
    assert (f"Before it: {(entry / base - 1) * 100:+.1f}% on the day, "
            f"{(entry / (base + 11) - 1) * 100:+.1f}% in the last 30 minutes") in opened
    assert "Nifty +" in opened
    assert "Your record on intraday buys" in opened and "0 so far, too few to say" in opened
    run("10:50")
    assert len(sent) == 1                                   # each note once
    rows.append(day_row("sell", 100, exit_, "29-Nov-2024 11:30:00", "T2"))
    run("11:35")
    closed = sent[-1]
    assert closed.startswith("📒 Closed: GROW long (intraday), 48 minutes: ₹+100 before "
                             "charges")
    assert "Today so far: 1 bets" in closed
    run("15:40")
    summary = sent[-1]
    assert summary.startswith("📒 Your trades on 29 Nov: 1 bets, 1 won")
    [ctx] = [r[0] for r in db_conn.execute("select context from my_bet_context")]
    assert ctx["best_pct"] > 0 and ctx["worst_pct"] < ctx["best_pct"]
    assert {"day_move_pct", "move_30m_pct", "vwap_gap_pct", "to_1515_pct",
            "after_exit_30m_pct", "nifty_move_pct", "ml_side", "spike"} <= set(ctx)
    count = len(sent)
    run("15:45")
    assert len(sent) == count                               # nothing twice

    # The app's own trade in a stock that day leaves its fills out.
    scan = db_conn.execute("insert into intraday_scan (status) values ('complete') "
                           "returning scan_id").fetchone()[0]
    db_conn.execute("""insert into intraday_trade (company_id, trading_day, action, scan_id,
            symbol, instrument_key, approved_by, approved_at, expires_at, quantity,
            entry_price, stop_price, target_price, notional, status)
        values (1, %s, 'buy', %s, 'GROW', %s, 'admin', %s, %s, 100, 102, 101, 104, 10200,
                'closed')""", (DAY, scan, GROW, at(DAY, "10:40"), at(DAY, "10:50")))
    db_conn.commit()
    out, left = mt.bets(db_conn)
    assert out == [] and left["app_fills"] == 2


@pytest.mark.db
def test_the_history_fills_the_days_not_read_on_the_day(db_conn, monkeypatch):
    import db_market
    db_market.load(db_conn)
    _client_env(monkeypatch)
    nov = [
        {"exchange": "NSE", "segment": "EQ", "trade_id": "H1", "trade_date": "2024-11-04",
         "transaction_type": "BUY", "quantity": 10, "price": 100.0, "isin": "INE001X01010",
         "symbol": "GROW", "instrument_token": GROW, "scrip_name": "GROW", "amount": 1000},
        {"exchange": "NSE", "segment": "EQ", "trade_id": "H2", "trade_date": "2024-11-20",
         "transaction_type": "SELL", "quantity": 10, "price": 110.0, "isin": "INE001X01010",
         "symbol": "GROW", "instrument_token": GROW, "scrip_name": "GROW", "amount": 1100},
        {"exchange": "NSE", "segment": "EQ", "trade_id": "H3", "trade_date": "2024-11-29",
         "transaction_type": "BUY", "quantity": 1, "price": 1.0, "isin": "INE001X01010",
         "symbol": "GROW", "instrument_token": GROW, "scrip_name": "GROW", "amount": 1},
        {"exchange": "", "segment": "MF", "trade_id": "", "trade_date": "2024-11-05",
         "transaction_type": "BUY", "quantity": 1, "price": 10.0}]
    asked = []

    def handler(req):
        p = dict(req.url.params)
        if req.url.path == mt.DAY_TRADES:
            return httpx.Response(200, json={"status": "success", "data": [
                day_row("buy", 5, 101.0, "29-Nov-2024 10:00:00", "T9"),
                day_row("sell", 5, 101.5, "29-Nov-2024 10:20:00", "T10")]})
        asked.append((p["start_date"], p["page_number"]))
        page = int(p["page_number"])
        data = (nov[:2] if page == 1 else nov[2:]) if p["start_date"] == "2024-11-01" else []
        return httpx.Response(200, json={"status": "success", "data": data, "meta_data": {
            "page": {"page_number": page, "page_size": 2, "total_records": 4,
                     "total_pages": 2}}})
    transport = httpx.MockTransport(handler)
    mt.with_account(lambda a: mt.import_day(db_conn, a, at(DAY, "10:30")), transport)
    seen, added = mt.with_account(lambda a: mt.import_history(
        db_conn, a, dt.date(2024, 10, 15), today=dt.date(2024, 11, 30)), transport)
    assert asked == [("2024-10-15", "1"), ("2024-11-01", "1"), ("2024-11-01", "2")]
    assert (seen, added) == (2, 2)            # 29 Nov was read on the day; no fund orders
    out, _ = mt.bets(db_conn)
    assert sorted((b.kind, b.side, b.source) for b in out) == [
        ("delivery", "long", "history"), ("intraday", "long", "day")]
    assert mt.fill_signals(db_conn, out) == 1           # the delivery bet's momentum rank
    rec = mt.record(db_conn)
    assert rec["summary"]["bets"] == 2 and rec["since"] == dt.date(2024, 11, 4)
    assert mt.last_import(db_conn, "history")["fills_added"] == 2

    # The tab shows the record.
    import streamlit
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    test = AppTest.from_file(str(APP), default_timeout=60)
    test.session_state["page"] = "Trading"
    test.session_state["trading_tab"] = "My trades"
    test.run()
    assert not test.exception, test.exception
    assert any(s.value == "Your record since 04 Nov 2024" for s in test.subheader)
    table = next(d.value for d in test.dataframe if "stock" in d.value.columns)
    assert sorted(table["kind"].to_list()) == ["delivery", "intraday"]
    assert json.dumps(rec["by_kind"], default=str)
