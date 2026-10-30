"""The My trades tab of the Trading page (igs.mytrades, docs/MY_TRADES.md): the trades you
place by hand on Upstox, measured after charges, and what has worked for you."""
from __future__ import annotations

import datetime as dt

import polars as pl
import streamlit as st

from igs import mytrades
from igs.timeutil import IST
from igs.ui import auth

NOTE = ("Your own trades on Upstox, the ones you place by hand; the app's own orders are left "
        "out. Today's trades are read every 5 minutes in the session, with the Upstox token "
        "(Settings, Upstox tab); nothing here places an order. A **bet** runs from a flat "
        "position to flat again in one instrument. Charges are estimated from "
        "config/costs.yaml and may differ a little from your contract notes. Upstox's trade "
        "history goes back three financial years, with dates but no times, so older "
        "intraday bets have no side, entry time or market context. Telegram (the trading "
        "chat) gets a note when a bet opens, with your record on similar ones, and when it "
        "closes.")
HOW = ("**With a 2%+ move:** you bought after the stock had risen 2% or more on the day, or "
       "sold short after a 2% fall; **against:** the opposite. **The 30 minutes before "
       "it:** the same with 0.5%. **What stood out:** a group of at least 15 bets in the "
       "earlier two thirds of your record whose result per bet had a t-statistic of 2 or "
       "more either way, then checked on the later third, which didn't choose it. With "
       "many groups, one can stand out by chance; one that held in the later bets is worth "
       "more.")


def _inr(x) -> str:
    return "n/a" if x is None else f"₹{float(x):+,.0f}"


def _imports(conn) -> None:
    for level, text in st.session_state.pop("my_trades_flash", []):
        getattr(st, level)(text)
    day, hist = mytrades.last_import(conn, "day"), mytrades.last_import(conn, "history")
    bits = []
    if day:
        bits.append(f"today's trades last read {day['started_at'].astimezone(IST):%d %b %H:%M}"
                    + (f" ({day['error']})" if day["error"] else ""))
    if hist:
        bits.append(f"history read {hist['started_at'].astimezone(IST):%d %b %Y} for "
                    f"{hist['start_date']:%d %b %Y} – {hist['end_date']:%d %b %Y}")
    st.caption("Imports: " + ("; ".join(bits) if bits else "none yet") + ".")
    a, b = st.columns(2)
    if a.button("Read today's trades now", key="my_trades_day"):
        _run(conn, lambda acc: mytrades.import_day(conn, acc))
    if b.button("Read the trade history (three financial years)", key="my_trades_history"):
        _run(conn, lambda acc: mytrades.import_history(conn, acc))


def _run(conn, fn) -> None:
    try:
        with st.spinner("Reading your trades from Upstox..."):
            seen, added = mytrades.with_account(fn)
            mytrades.fill_signals(conn, mytrades.bets(conn)[0])
    except mytrades.AccountError as exc:
        st.session_state["my_trades_flash"] = [("error", str(exc))]
    else:
        st.session_state["my_trades_flash"] = [("success", f"{seen} fills read, {added} new.")]
    st.rerun()


def _bet_row(b: mytrades.Bet) -> dict:
    c = b.context or {}
    at = b.opened_at.astimezone(IST) if b.opened_at else None
    return {"opened": b.opened_on, "time": f"{at:%H:%M}" if at else "",
            "stock": b.symbol, "kind": mytrades.KIND_LABEL[b.kind], "side": b.side or "",
            "quantity": b.quantity,
            "entry (Rs)": float(b.entry) if b.entry is not None else None,
            "exit (Rs)": float(b.exit) if b.exit is not None and b.closed else None,
            "held": (f"{b.minutes:.0f} min" if b.minutes is not None else
                     f"{b.days} days" if b.days else "same day" if b.closed else "open"),
            "gross (Rs)": float(b.gross) if b.gross is not None else None,
            "charges (Rs)": float(b.charges) if b.charges is not None else None,
            "net (Rs)": float(b.net) if b.net is not None else None,
            "net %": b.net_pct, "day's move before (%)": c.get("day_move_pct"),
            "best while open (%)": c.get("best_pct"),
            "worst while open (%)": c.get("worst_pct")}


def page(conn, title: bool = True) -> None:
    if not auth.is_admin():
        st.info("Your own trades are shown to the account administrator only.")
        return
    if title:
        st.header("My trades")
    st.caption(NOTE)
    _imports(conn)
    rec = mytrades.record(conn)
    s = rec["summary"]
    if not rec["bets"]:
        st.info("No trades read yet. Read the trade history above to start from your last "
                "three years; today's trades are read on each intraday check.")
        return
    if s["bets"]:
        st.subheader(f"Your record since {rec['since']:%d %b %Y}")
        m = st.columns(5)
        m[0].metric("Closed bets", s["bets"])
        m[1].metric("Won", f"{s['won_pct']:.0f}%")
        m[2].metric("Net after charges", _inr(s["net_inr"]))
        m[3].metric("Charges", f"₹{s['charges_inr']:,.0f}",
                    help="Estimated. As a share of your gross profit on winning bets: "
                         + ("n/a" if s["charges_share_pct"] is None
                            else f"{s['charges_share_pct']:.0f}%") + ".")
        m[4].metric("t-statistic", "n/a" if s["t"] is None else f"{s['t']:.2f}",
                    help="Of the net result per bet. Bets on the same day aren't "
                         "independent, so it overstates how sure this is.")
        st.caption(f"Average win {_inr(s['avg_win_inr'])}, average loss "
                   f"{_inr(s['avg_loss_inr'])}"
                   + (f", profit factor {s['profit_factor']:.2f}"
                      if s["profit_factor"] is not None else "") + ".")
        st.dataframe(pl.DataFrame([{
            "kind": mytrades.KIND_LABEL[k], "bets": v["bets"], "won %": v["won_pct"],
            "net (Rs)": v["net_inr"], "charges (Rs)": v["charges_inr"],
            "net % a bet": v["avg_net_pct"], "t": v["t"]} for k, v in rec["by_kind"].items()]),
            hide_index=True, width="stretch")
        st.subheader("What stood out")
        st.caption(HOW)
        if rec["patterns"]:
            st.dataframe(pl.DataFrame([{
                "group": p["group"], "bets like this": p["value"], "bets": p["bets"],
                "won %": p["won_pct"], "Rs a bet": p["avg_net_inr"], "t": p["t"],
                "later bets": p["later_bets"], "later Rs a bet": p["later_avg_net_inr"],
                "in the later bets": p["held"]} for p in rec["patterns"]],
                infer_schema_length=None), hide_index=True, width="stretch")
        else:
            st.info(f"Nothing stood out yet: no group of {mytrades.MIN_BETS} or more bets in "
                    "the earlier two thirds of your record had a t-statistic of 2 either way.")
        ex = rec["exits"]
        if ex:
            st.subheader("Your exits")
            st.caption(f"Timed intraday bets ({ex['bets']}), before charges, in your "
                       "direction: what your exits made, the best and worst prices while "
                       "each was open (5-minute candles after the entry's), holding to 15:15 "
                       "instead, and the 30 minutes after you closed.")
            e = st.columns(5)
            for col, (label, key) in zip(e, (
                    ("Your exits, a bet", "your_exit_pct"), ("Best while open", "best_pct"),
                    ("Worst while open", "worst_pct"), ("Held to 15:15", "to_1515_pct"),
                    ("30 min after you closed", "after_exit_30m_pct")), strict=True):
                col.metric(label, "n/a" if ex[key] is None else f"{ex[key]:+.2f}%")
            if ex["winners"] and ex["winners_best_pct"] is not None:
                st.caption(f"Winners ({ex['winners']}): you took {ex['winners_exit_pct']:+.2f}% "
                           f"where the best price reached {ex['winners_best_pct']:+.2f}%. "
                           + (f"Losers ({ex['losers']}): the worst price reached "
                              f"{ex['losers_worst_pct']:+.2f}% on average."
                              if ex["losers_worst_pct"] is not None else ""))
        with st.expander("By group"):
            st.dataframe(pl.DataFrame([{
                "group": r["group"], "bets like this": r["value"], "bets": r["bets"],
                "won %": r["won_pct"], "net (Rs)": r["net_inr"], "Rs a bet": r["avg_net_inr"],
                "% a bet": r["avg_net_pct"], "t": r["t"]} for r in rec["breakdown"]],
                infer_schema_length=None), hide_index=True, width="stretch")
    if rec["open"]:
        st.subheader("Open")
        st.dataframe(pl.DataFrame([_bet_row(b) for b in rec["open"]], infer_schema_length=None),
                     hide_index=True, width="stretch")
    st.subheader("Your bets")
    shown = sorted(rec["bets"], key=lambda b: (b.opened_on, b.opened_at or dt.datetime.min
                                               .replace(tzinfo=IST)), reverse=True)[:300]
    st.dataframe(pl.DataFrame([_bet_row(b) for b in shown], infer_schema_length=None),
                 hide_index=True, width="stretch")
    left = rec["left_out"]
    if left["app_fills"] or left["unmatched_sales"]:
        st.caption(f"Left out: {left['app_fills']} fills of the app's own orders (every fill "
                   "in a stock on a day the app traded it), and "
                   f"{left['unmatched_sales']} delivery sales of shares bought before the "
                   "record starts.")
