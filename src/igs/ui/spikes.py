"""The Spike reversal tab of the Paper tracking page (igs.intraday.spikes, docs/SPIKES.md):
today's spike alerts, and the paper track with and without the price-band rule."""
from __future__ import annotations

import datetime as dt

import polars as pl
import streamlit as st

from igs.intraday import spikes
from igs.timeutil import IST

NOTE = ("Paper only: nothing here places an order. Every 5 minutes in the session, every NSE "
        "stock trading ₹2 crore a day is checked for a move of **12% or more within 30 "
        "minutes**; each one is sent to Telegram (the spikes chat). The paper track trades "
        "against the move at the first flat 5-minute candle (body within 1%), with a 4% "
        "target, out at 15:15, after charges and 0.15% slippage a side on ₹1 lakh. On "
        "2022–2026 data this rule made about nothing after costs; its worst trades ended "
        "locked at a price band. So it is recorded twice: **every spike**, and the **band "
        "rule**, which leaves out a short sale whose upper band is less than 4% above the "
        "entry (a buy: the lower band 4% below). The band rule is judged after 100 trades: "
        "its daily P&L must be above zero with a t-statistic of 2 or more "
        "(research/spike_reversal/PAPER.md).")
STATUS = {"watching": "range 12%+, no spike yet", "waiting": "spike; waiting for a flat candle",
          "open": "open", "closed": "closed", "no flat": "spike; no flat candle by 14:55",
          "none": "no spike", "not tracked": "alert only"}


def page(conn, title: bool = True) -> None:
    """The page; `title` False under a tab that already names it."""
    if title:
        st.header("Spike reversal")
    st.caption(NOTE)
    rec = spikes.record(conn)
    st.markdown(f"**The band rule:** {rec['verdict']}." + (
        f" Recorded since {rec['since']:%d %b %Y}." if rec["since"] else ""))
    for key, name in (("band", "Band rule"), ("all", "Every spike")):
        s = rec[key]
        m = st.columns(5)
        m[0].metric(f"{name}: trades", s["trades"])
        m[1].metric("Net ₹ since the start", f"₹{s['net_inr']:+,.0f}")
        m[2].metric("A trade, after costs",
                    "n/a" if s["mean_pct"] is None else f"{s['mean_pct']:+.2f}%")
        m[3].metric("₹ a session", "n/a" if s["daily_inr"] is None else f"₹{s['daily_inr']:+,.0f}",
                    help=f"Over the {s['days']} sessions the watcher ran, zero on a day "
                         "without a trade.")
        m[4].metric("t-statistic", "n/a" if s["t"] is None else f"{s['t']:.2f}")
    left = rec["left_out"]
    if left["trades"]:
        st.caption(f"The band rule left out {left['trades']} trade(s): ₹{left['net_inr']:+,.0f} "
                   f"between them. {rec['all']['locked']} trade(s) ended on a candle without a "
                   "price range (a lock), where a real exit might not have filled.")
    day = dt.datetime.now(IST).date()
    st.subheader("Today's spikes")
    rows = spikes.today(conn, day)
    if rows:
        st.dataframe(pl.DataFrame([{
            "stock": r["symbol"],
            "move": None if r["alert_move_pct"] is None else f"{float(r['alert_move_pct']):+.1f}%",
            "by": _hm(r["alert_bar"], close=True),
            "alert": r["alert_status"] or "",
            "paper": STATUS[r["status"]]} for r in rows]), hide_index=True, width="stretch")
    else:
        st.info("No spike today yet. The intraday job checks every 5 minutes from 09:20 while "
                "an Upstox access token is saved (Settings, Upstox).")
    st.subheader("Paper trades")
    t = spikes.trades(conn)
    if t:
        st.dataframe(pl.DataFrame([{
            "day": r["trade_date"], "stock": r["symbol"],
            "side": "short" if r["side"] < 0 else "buy",
            "spike %": float(r["move_pct"]), "entry": _hm(r["entry_bar"]),
            "entry ₹": float(r["entry"]),
            "exit": "" if r["exit_bar"] is None else _hm(r["exit_bar"]),
            "why": r["exit_reason"] or STATUS[r["status"]],
            "net ₹": None if r["net_inr"] is None else float(r["net_inr"]),
            "net %": None if r["net_pct"] is None else float(r["net_pct"]),
            "band": r["band_status"] or "",
            "room to band %": None if r["band_room_pct"] is None else float(r["band_room_pct"]),
            "band rule": "takes" if r["band_takes"] else "leaves out"}
            for r in t], infer_schema_length=None), hide_index=True, width="stretch")
    else:
        st.caption("No paper trade yet.")


def _hm(start, close: bool = False) -> str:
    if start is None:
        return ""
    return (start + (spikes.BAR if close else dt.timedelta())).astimezone(IST).strftime("%H:%M")
