"""The Spike picks tab of the Paper tracking page (igs.intraday.spike_ml, docs/SPIKES.md):
the spike model's picks among 5%+ moves, with buys and short sales recorded apart."""
from __future__ import annotations

import datetime as dt

import polars as pl
import streamlit as st

from igs.intraday import spike_ml
from igs.timeutil import IST

NOTE = ("Paper only: nothing here places an order. Every 5 minutes in the session, each NSE "
        "stock trading ₹2 crore a day whose range today is 5% or more is checked. When its "
        "move within 30 minutes first reaches 5, 6, 7, 8, 10, 12 or 15%, a fixed model "
        "predicts the result after costs of trading against the move with a 1–5% target. "
        "The first move whose best prediction is above 0.5% is the stock's pick for the day, "
        "with that target: a **buy after a fall** or a **short sale after a rise**, out at "
        "15:15 if the target isn't reached, after charges and 0.15% slippage a side on "
        "₹1 lakh. Each pick goes to Telegram (the spikes chat). A short sale within 4% of the "
        "upper circuit isn't taken. In the 2025–2026 test the model's buys made +0.60% a "
        "trade and its short sales −0.09%, a split seen after the fact: this record tests "
        "it. Each record is judged after 100 trades: its daily P&L must be above zero with a "
        "t-statistic of 2 or more (research/spike_ml/PAPER.md).")
STATUS = {"watching": "range 5%+, no pick yet", "picked": "picked", "missed": "missed (found late)",
          "closed": "closed", "none": "no pick", "void": "no entry before 15:15"}


def page(conn, title: bool = True) -> None:
    """The page; `title` False under a tab that already names it."""
    if title:
        st.header("Spike picks")
    st.caption(NOTE)
    rec = spike_ml.record(conn)
    if rec["since"]:
        st.caption(f"Recorded since {rec['since']:%d %b %Y}.")
    for key, name in (("buys", "Buys after falls"), ("shorts", "Short sales after rises"),
                      ("all_shorts", "Every short sale (comparison)")):
        s = rec[key]
        st.markdown(f"**{name}:** {s['verdict'] if 'verdict' in s else ''}.")
        m = st.columns(5)
        m[0].metric(f"{name}: trades", s["trades"])
        m[1].metric("Net ₹ since the start", f"₹{s['net_inr']:+,.0f}")
        m[2].metric("A trade, after costs",
                    "n/a" if s["mean_pct"] is None else f"{s['mean_pct']:+.2f}%")
        m[3].metric("₹ a session", "n/a" if s["daily_inr"] is None
                    else f"₹{s['daily_inr']:+,.0f}",
                    help=f"Over the {s['days']} sessions the watcher ran, zero on a day "
                         "without a trade.")
        m[4].metric("t-statistic", "n/a" if s["t"] is None else f"{s['t']:.2f}")
    if rec["missed"]["trades"]:
        st.caption(f"Missed (found more than 10 minutes late): {rec['missed']['trades']} "
                   f"pick(s), which would have made ₹{rec['missed']['net_inr']:+,.0f}.")
    st.subheader("Today's picks")
    rows = spike_ml.today(conn, dt.datetime.now(IST).date())
    if rows:
        st.dataframe(pl.DataFrame([_row(r) for r in rows], infer_schema_length=None),
                     hide_index=True, width="stretch")
    else:
        st.info("No pick today yet. The intraday job checks every 5 minutes from 09:20 while "
                "an Upstox access token is saved (Settings, Upstox).")
    st.subheader("All picks")
    t = spike_ml.trades(conn)
    if t:
        st.dataframe(pl.DataFrame([{"day": r["trade_date"], **_row(r)} for r in t],
                                  infer_schema_length=None), hide_index=True, width="stretch")
    else:
        st.caption("No pick yet.")


def _row(r: dict) -> dict:
    return {"stock": r["symbol"], "side": "buy" if r["side"] == -1 else "short",
            "move %": float(r["move_pct"]), "by": _hm(r["decision_bar"], close=True),
            "expected %": float(r["expected_pct"]), "target %": r["target_pct"],
            "entry ₹": None if r["entry"] is None else float(r["entry"]),
            "exit": "" if r["exit_bar"] is None else _hm(r["exit_bar"]),
            "why": r["exit_reason"] or STATUS[r["status"]],
            "net ₹": None if r["net_inr"] is None else float(r["net_inr"]),
            "net %": None if r["net_pct"] is None else float(r["net_pct"]),
            "record": ("missed" if r["status"] == "missed" else "taken" if r["takes"]
                       else "not taken: near the upper circuit")}


def _hm(start, close: bool = False) -> str:
    if start is None:
        return ""
    return (start + (spike_ml.BAR if close else dt.timedelta())).astimezone(IST).strftime("%H:%M")
