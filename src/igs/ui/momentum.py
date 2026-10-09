"""The momentum paper portfolios page (igs.momentum): holdings, the AI's reviews, and each
month's results against the equal-weighted Nifty 200 basket and the Nifty 50."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs import momentum

NOTE = ("Paper only: nothing here places an order. Each month's last close ranks the Nifty "
        "200 stocks trading at least ₹50 crore a day by their return from 12 months to 1 month "
        "ago; the portfolios change at the next open. **Rule** holds the best ten. "
        "**AI-reviewed** holds the best ten the AI keeps after reading what the app holds on "
        "each, as of that close; it avoids a stock only for a specific reason it cites. **Rule, "
        "skipping bad results** holds the best ten not flagged by a results reaction of −5% "
        "or worse against the market in the previous 21 sessions (Results days page). Results "
        "are open to open between rebalances, after delivery costs (about 0.4% a round trip) "
        "on the names bought. The basket is the equal-weighted average of the stocks ranked. "
        "In the 14-year backtest the rule beat the basket in 2021–2026 and lagged it in "
        "2013–2020: this record shows how it does from here.")
TRACK_NAMES = momentum.NAMES


def _pct(v):
    return None if v is None else round(v, 2)


def page(conn) -> None:
    st.header("Momentum paper portfolios")
    st.caption(NOTE)
    hold, res = momentum.holdings(conn), momentum.results(conn)
    if not hold["rule"]:
        st.info("No portfolio yet. The daily job forms the first one once the Nifty 200 list "
                "(loaded by the check each day) and a year of prices are in.")
        return
    totals = {t: res[t]["total"] for t in momentum.TRACKS}
    m = st.columns(5)
    m[0].metric("Rule, since the start", _fmt(totals["rule"]["net_pct"]))
    m[1].metric("AI-reviewed, since the start", _fmt(totals["ai"]["net_pct"]))
    first = {t: res[t]["periods"][0]["start"] for t in momentum.TRACKS if res[t]["periods"]}
    m[2].metric("Skipping bad results", _fmt(totals["results"]["net_pct"]),
                help=(f"Since the {first['results']:%d %b %Y} open; it started later than the "
                      "others." if "results" in first and first["results"] != first.get("rule")
                      else None))
    m[3].metric("Nifty 200 basket", _fmt(totals["rule"]["basket_pct"]))
    m[4].metric("Nifty 50", _fmt(totals["rule"]["nifty_pct"]))
    for tab, track in zip(st.tabs([TRACK_NAMES[t] for t in momentum.TRACKS]), momentum.TRACKS,
                          strict=True):
        with tab:
            _track(hold[track], res[track])


def _track(h: dict | None, r: dict) -> None:
    if h is None:
        st.info("Not formed yet: it starts at the next monthly rebalance.")
        return
    st.caption(f"Formed from the {h['signal_date']:%d %b %Y} close of {h['universe']} ranked "
               f"stocks; holdings changed at the {h['entry_date']:%d %b %Y} open. "
               + (h["note"] or ""))
    open_period = next((p for p in r["periods"] if not p["complete"]), None)
    since = {d["symbol"]: d["return_pct"] for d in (open_period or {}).get("detail", [])}
    st.dataframe(pl.DataFrame([{"rank": x["rank"], "stock": x["symbol"],
                                "12-1 month return %": x["score_pct"],
                                "since entry %": since.get(x["symbol"])}
                               for x in h["holdings"]]), hide_index=True, width="stretch")
    if h["reviews"]:
        st.subheader("The AI's reviews")
        st.dataframe(pl.DataFrame([{"rank": x["rank"], "stock": x["symbol"],
                                    "decision": x["decision"], "reason": x["reason"]}
                                   for x in h["reviews"]]), hide_index=True, width="stretch")
    if r["periods"]:
        st.subheader("Each month")
        st.dataframe(pl.DataFrame([{
            "from": p["start"], "to": p["end"],
            "status": "closed" if p["complete"] else "open (to the latest close)",
            "after costs %": _pct(p["net_pct"]), "basket %": _pct(p["basket_pct"]),
            "Nifty 50 %": _pct(p["nifty_pct"]),
            "vs basket %": (None if p["basket_pct"] is None
                            else round(p["net_pct"] - p["basket_pct"], 2)),
            "bought": p["bought"]} for p in reversed(r["periods"])]),
            hide_index=True, width="stretch")


def _fmt(v) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"
