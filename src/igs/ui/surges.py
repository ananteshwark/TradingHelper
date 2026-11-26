"""Large trading as it happens (igs.intraday.surges), on the Institutional deals page."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs.intraday import surges
from igs.timeutil import IST

NOTE = ("Every intraday scan (every 5 minutes, 09:20–15:30) reads each liquid stock's volume "
        "from Upstox. A stock that traded a large part of a normal day's value between two "
        "scans (Settings > Alerts, `trade_surges`: by default 10% of the median day of its "
        "previous 20 sessions, and ₹50 crore) is a surge, sent to the deals chat once a stock "
        "a day. Who traded isn't known then: NSE names large traders only after each block "
        "window and after the close, and the last column shows the institutions that day's "
        "files named in the stock. The moves are signed for the way the price went in the "
        "window: positive means it kept going that way (docs/TRADE_SURGES.md).")


def _pct(v) -> str:
    return "–" if v is None else f"{v:+.2f}%"


def section(conn) -> None:
    st.subheader("Large trading as it happens (unnamed)")
    st.caption(NOTE)
    rec = surges.record(conn)
    a, b, c, d = st.columns(4)
    a.metric("Surges", rec['surges'])
    b.metric("To the close", _pct(rec['to_close_pct']), help=f"{rec['with_close']} with a close")
    c.metric("Next session vs Nifty 500", _pct(rec['next_vs_index_pct']),
             help=f"{rec['with_next']} with a next session")
    d.metric("Named that day", rec['named'])
    rows = surges.recent(conn)
    if not rows:
        st.info("None yet: surges appear once the intraday scans have run with the Upstox "
                "token set.")
        return
    st.dataframe(pl.DataFrame([{
        'window (IST)': f"{r['window_from'].astimezone(IST):%d %b %H:%M}–"
                        f"{r['window_to'].astimezone(IST):%H:%M}",
        'stock': r['symbol'],
        'traded (Rs crore)': round(float(r['value_inr']) / 1e7, 2),
        'of a normal day %': round(float(r['share_of_day']) * 100, 1),
        'price move %': round(float(r['move_pct']), 2),
        'bids / offers': (round(float(r['bid_qty']) / float(r['offer_qty']), 2)
                          if r['bid_qty'] and r['offer_qty'] else None),
        'sent': r['alert_status'] == 'sent',
        'to the close %': (round(float(r['to_close_pct']), 2)
                           if r['to_close_pct'] is not None else None),
        'next session vs Nifty 500 %': (round(float(r['next_vs_index_pct']), 2)
                                        if r['next_vs_index_pct'] is not None else None),
        'named that day': r['named']} for r in rows], infer_schema_length=None),
        hide_index=True)
