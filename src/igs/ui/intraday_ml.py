"""The ML intraday paper calls page (igs.intraday.ml): today's calls, each day's result and
the record since the start."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs.intraday import ml
from igs.timeutil import IST, utc_now

NOTE = ("Paper only: nothing here places an order. At 09:45 a machine-learning model predicts "
        "each liquid Nifty 200 stock's move to 15:15 from its first 30 minutes, its recent "
        "days and the market's. The three highest predictions above +0.15% are buys and the "
        "three lowest below −0.15% short sells. Each enters at the next 5-minute candle's open "
        "and exits at the 15:15 candle's open; results are after slippage and intraday charges "
        "on ₹1 lakh a call. In its untouched test (January 2025 to October 2026) it made "
        "+0.13% a call after costs, almost all of it on the short sells, with months of "
        "losses in between: this record shows whether that holds from here.")
SIDE = {1: "buy", -1: "sell short"}


def page(conn) -> None:
    st.header("ML intraday paper calls")
    st.caption(NOTE)
    rec = ml.record(conn)
    t = rec["total"]
    m = st.columns(4)
    m[0].metric("Calls closed", t["trades"])
    m[1].metric("Net since the start", f"₹{t['net_inr']:+,.0f}")
    m[2].metric("Average a call", "n/a" if t["net_pct"] is None else f"{t['net_pct']:+.2f}%")
    m[3].metric("Profit factor", "n/a" if t["profit_factor"] is None
                else f"{t['profit_factor']:.2f}")
    today = utc_now().astimezone(IST).date()
    rows = ml.picks(conn, today)
    st.subheader(f"Today, {today:%d %b %Y}")
    day = next((d for d in rec["days"] if d["session_date"] == today), None)
    if not rows:
        st.info(day["note"] if day and day["note"] else
                "No calls yet: the model runs at 09:45 on trading days.")
    else:
        st.dataframe(_table(rows), hide_index=True, width="stretch")
    if rec["days"]:
        days = pl.DataFrame(rec["days"]).with_columns(
            pl.col("net_inr").cum_sum().alias("since the start ₹"))
        st.subheader("Each day")
        st.line_chart(days.select("session_date", "since the start ₹"), x="session_date",
                      y="since the start ₹")
        st.dataframe(days.select(pl.col("session_date").alias("day"),
                                 pl.col("scored").alias("stocks scored"),
                                 pl.col("trades").alias("calls closed"),
                                 pl.col("net_inr").round(0).alias("net ₹"),
                                 "since the start ₹", "note").reverse(),
                     hide_index=True, width="stretch")
        older = [r for r in ml.picks(conn) if r["session_date"] != today]
        if older:
            st.subheader("Earlier calls")
            st.dataframe(_table(older), hide_index=True, width="stretch")


def _table(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame([{
        "day": r["session_date"], "stock": r["symbol"], "call": SIDE[r["side"]],
        "predicted %": round(r["prediction"] * 100, 2), "entry": r["entry"], "exit": r["exit"],
        "net ₹": None if r["net_inr"] is None else round(r["net_inr"]),
        "net %": None if r["net_pct"] is None else round(r["net_pct"], 2),
        "status": r["status"]} for r in rows])
