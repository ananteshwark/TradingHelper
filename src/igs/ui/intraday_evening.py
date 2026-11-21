"""The evening intraday calls page (igs.intraday.evening), a tab of the Trading page: the
calls recorded after the last close, each day's paper result and the record since the
start. Paper only; nothing here places an order."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs.intraday import evening
from igs.intraday.trading import (
    EVENING_APPROVE_UNTIL,
    EVENING_PLACE_FROM,
    TradeError,
    approve_evening,
    evening_orders_on,
    evening_window,
)
from igs.timeutil import IST, utc_now
from igs.ui import auth
from igs.ui.intraday_ml import _luck

NOTE = ("The record below is on paper. After each close, a machine-learning model (five "
        "gradient-boosted tree models averaged) predicts each liquid Nifty 200 stock's move "
        "from the next session's 09:15 open to 15:15, from what is known at the close: the "
        "last day, its last 30 minutes, the recent trend and the market's. The three highest "
        "predictions above "
        "+0.15% are buys and the three lowest below −0.15% short sells, recorded the evening "
        "before. Each enters at the 09:15 candle's open and exits at the 15:15 candle's open, "
        "after 0.05% slippage a side and intraday charges on ₹1 lakh a call. In its test "
        "(January 2025 to October 2026) it made +0.14% a call after costs, t 2.02, just over "
        "its bar, all of it on short sells: this record shows whether that holds from here. "
        "Real orders, when switched on in Settings, differ: approved the evening before, "
        "entered about 09:20 with a stop, and out at 15:00.")
SIDE = {1: "buy", -1: "sell short"}


def page(conn, title: bool = True) -> None:
    """The page; `title` False under a tab that already names it."""
    if title:
        st.header("Evening intraday picks")
    st.caption(NOTE)
    rec = evening.record(conn)
    t = rec["total"]
    m = st.columns(4)
    m[0].metric("Calls closed", t["trades"])
    m[1].metric("Net since the start", f"₹{t['net_inr']:+,.0f}")
    m[2].metric("Average a call", "n/a" if t["net_pct"] is None else f"{t['net_pct']:+.2f}%")
    m[3].metric("Profit factor", "n/a" if t["profit_factor"] is None
                else f"{t['profit_factor']:.2f}")
    st.caption(_luck(t))
    latest = rec["days"][-1] if rec["days"] else None
    if latest is None:
        st.info("No calls yet: the model runs after the close on trading days, from 15:45 IST.")
        return
    rows = evening.picks(conn, latest["session_date"])
    st.subheader(f"Latest: calls from the close of {latest['session_date']:%d %b %Y}")
    if rows:
        st.dataframe(_table(rows), hide_index=True, width="stretch")
    else:
        st.info(latest["note"] or "No calls that evening.")
    today = utc_now().astimezone(IST).date()
    if any(r["status"] == "open" for r in rows):
        st.caption(f"Open calls close from 15:20 IST on the next session (today is "
                   f"{today:%d %b}).")
    if auth.is_admin():
        _orders(conn, latest["session_date"])
    days = pl.DataFrame(rec["days"]).with_columns(
        pl.col("net_inr").cum_sum().alias("since the start ₹"))
    st.subheader("Each evening")
    st.line_chart(days.select("session_date", "since the start ₹"), x="session_date",
                  y="since the start ₹")
    st.dataframe(days.select(pl.col("session_date").alias("signal day"),
                             pl.col("scored").alias("stocks scored"),
                             pl.col("trades").alias("calls closed"),
                             pl.col("net_inr").round(0).alias("net ₹"),
                             "since the start ₹", "note").reverse(),
                 hide_index=True, width="stretch")
    older = [r for r in evening.picks(conn) if r["signal_date"] != latest["session_date"]]
    if older:
        st.subheader("Earlier calls")
        st.dataframe(_table(older), hide_index=True, width="stretch")


def _orders(conn, signal_date) -> None:
    """Approve the latest evening's calls for real orders, when switched on."""
    st.subheader("Real orders")
    if not evening_orders_on(conn):
        st.caption("Real orders for the evening calls are off (Settings, Intraday trading). "
                   "The paper record above is unaffected.")
        return
    now = utc_now()
    if not evening_window(now, signal_date):
        st.caption(f"Approvals for these calls closed at {EVENING_APPROVE_UNTIL:%H:%M} IST on "
                   "their trading day; see each call's order status above.")
        return
    with conn.cursor() as cur:
        cur.execute("""select instrument_key, symbol, side from evening_pick
                       where signal_date = %s and status = 'open' and approved_at is null
                       order by rank""", (signal_date,))
        waiting = cur.fetchall()
    if not waiting:
        st.caption("Every call from this evening is approved or closed.")
        return
    labels = {f"{SIDE[side]} {sym}": key for key, sym, side in waiting}
    with st.form("evening_approve"):
        choice = st.selectbox("Call", list(labels))
        sure = st.checkbox(f"I approve a real Upstox intraday order for this call, placed after "
                           f"{EVENING_PLACE_FROM:%H:%M} IST on the next trading day")
        if st.form_submit_button("Approve"):
            auth.require_access(admin=True)
            if not sure:
                st.error("Tick the confirmation to approve.")
            else:
                try:
                    st.success(approve_evening(conn, labels[choice], source="admin"))
                except TradeError as exc:
                    st.error(str(exc))


def _table(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame([{
        "signal day": r["signal_date"], "traded": r["trade_date"], "stock": r["symbol"],
        "call": SIDE[r["side"]], "predicted %": round(r["prediction"] * 100, 2),
        "entry": r["entry"], "exit": r["exit"],
        "net ₹": None if r["net_inr"] is None else round(r["net_inr"]),
        "net %": None if r["net_pct"] is None else round(r["net_pct"], 2),
        "status": r["status"],
        "real order": ("not approved" if r.get("approved_at") is None else
                       r.get("order_note") or "approved, to be placed")} for r in rows])
