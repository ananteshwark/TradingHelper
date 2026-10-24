"""The Results days page (igs.results_drift), a tab of the Paper tracking page: paper buys
after strong results reactions, and the stocks flagged now after bad ones."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs import results_drift, results_read

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


def page(conn, title: bool = True) -> None:
    """The page; `title` False under a tab that already names it."""
    if title:
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
    _reads(conn)


READS_NOTE = ("Each quarterly report filed since this was installed, read from its figures "
              "against the same quarter a year earlier: **positive** is revenue and profit up "
              "by the thresholds in Settings > Alerts (`quarterly_results`) with no negative "
              "surprise, **negative** is profit down or a loss; Telegram gets only the "
              "positive ones. The rule has not been tested on past results, so this is its "
              "test: the stock's move against the Nifty 500 from the last close before the "
              "filing, to the next session's close and 21 sessions on.")


def _reads(conn) -> None:
    st.subheader("Quarterly results read from their figures")
    st.caption(READS_NOTE)
    rows = results_read.history(conn)
    if not rows:
        st.info("None yet: reports are read as they are filed, when the operational "
                "notifications are on (docs/OPERATIONAL_NOTIFICATIONS.md).")
        return
    for horizon, text in (("next_session", "next session"), ("sessions_21", "21 sessions")):
        s = results_read.summary(rows, horizon)
        m = st.columns(3)
        for col, verdict in zip(m, ("positive", "mixed", "negative"), strict=True):
            v = s[verdict]
            col.metric(f"Read {verdict}: vs Nifty 500, {text}",
                       "n/a" if v["mean"] is None else f"{v['mean']:+.2f}%",
                       help=f"{v['n']} report(s) measured, {v['up']} beat the Nifty 500.")
    down = results_read.fallen(rows)
    if down:
        for horizon, text in (("next_session", "next session"), ("sessions_21", "21 sessions")):
            v = results_read.summary(down, horizon)["positive"]
            st.metric(f"Positive results on stocks 30%+ below their 1-year high: vs Nifty "
                      f"500, {text}", "n/a" if v["mean"] is None else f"{v['mean']:+.2f}%",
                      help=f"{v['n']} report(s) measured, {v['up']} beat the Nifty 500. A "
                           "possible bottom: the business turning while the price is down. "
                           "No price pattern called bottoms in the app's tests "
                           "(docs/BOTTOMS.md); this record tests the business side.")
    st.dataframe(pl.DataFrame([{
        "stock": r["symbol"] or r["company"], "quarter": r["period_end"],
        "filed": r["filed_at"], "basis": r["basis"], "read": r["verdict"],
        "sent": r["sent"], "below 1-year high %": r.get("below_high_pct"),
        "revenue YoY %": _r(r["revenue_yoy_pct"]),
        "profit YoY %": _r(r["profit_yoy_pct"]), "surprise": _r(r["surprise"]),
        "next session vs Nifty 500 %": _move(r, "next_session"),
        "21 sessions vs Nifty 500 %": _move(r, "sessions_21"), "why": r["reasons"]}
        for r in rows], infer_schema_length=None), hide_index=True, width="stretch")


def _move(row: dict, horizon: str):
    m = row["moves"].get(horizon)
    return None if m is None or m[1] is None else round(m[1], 2)


def _r(v):
    return None if v is None else round(v, 2)
