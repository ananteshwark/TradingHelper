"""The Results days page (igs.results_drift): paper buys after strong results reactions, and
the stocks flagged now after bad ones."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs import results_drift

NOTE = ("Paper only: nothing here places an order. After a company's results (its first NSE "
        "results filing for a period), its reaction is its close-to-close move from the session "
        "before the filing day to the session after, against the average liquid stock (₹50 "
        "crore a day). A reaction of **+5% or better** opens a paper buy at the next open, held "
        "21 sessions and measured against the same stocks after delivery costs. One noticed "
        "after that open is recorded as missed. A reaction of **−5% or worse** flags the stock "
        "for 21 sessions: the momentum track 'skipping bad results' leaves it out and AI calls "
        "on it carry a note. In the backtest the buys beat the market by about 1% a month after "
        "costs, short of the bar set in advance, and flagged stocks lagged by about 0.9%: this "
        "record shows whether that holds from here.")
STATUS = {"open": "open (to the latest close)", "closed": "closed", "missed": "missed (seen late)",
          "no data": "no price on the entry day"}


def page(conn) -> None:
    st.header("Results days")
    st.caption(NOTE)
    rec = results_drift.record(conn)
    c = rec["closed"]
    m = st.columns(4)
    m[0].metric("Paper buys closed", c["n"])
    m[1].metric("Average vs market, after costs",
                "n/a" if c["mean_net_excess_pct"] is None else f"{c['mean_net_excess_pct']:+.2f}%")
    m[2].metric("Beat the market", "n/a" if c["win"] is None else f"{c['win']:.0%}")
    m[3].metric("t-statistic", "n/a" if c["t"] is None else f"{c['t']:.2f}")
    st.subheader("Paper buys after a +5% reaction")
    if rec["trades"]:
        st.dataframe(pl.DataFrame([{
            "stock": t["symbol"], "reaction vs market %": round(t["abnormal_pct"], 1),
            "entry": t["entry_date"], "exit": t["exit_date"] or t["exit_due"],
            "status": STATUS[t["status"]],
            "return %": _r(t["return_pct"]), "market %": _r(t["universe_pct"]),
            "costs %": _r(t["cost_pct"]), "vs market after costs %": _r(t["net_excess_pct"])}
            for t in rec["trades"]]), hide_index=True, width="stretch")
    else:
        st.info("None yet: the daily job records each results reaction once the session after "
                "the filing has traded.")
    st.subheader("Flagged now after a −5% reaction")
    if rec["flagged"]:
        st.dataframe(pl.DataFrame([{
            "stock": f["symbol"], "reaction vs market %": round(f["abnormal_pct"], 1),
            "session after results": f["after_date"], "flagged until": f["flag_until"]}
            for f in rec["flagged"]]), hide_index=True, width="stretch")
    else:
        st.caption("No stock is flagged now.")


def _r(v):
    return None if v is None else round(v, 2)
