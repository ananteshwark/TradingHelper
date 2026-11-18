"""The promoter trades page (igs.promoter_trades): promoters buying and selling their own
company's shares, as the app found them, and how the stocks moved afterwards."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs.institutions import outcomes
from igs.timeutil import IST

NOTE = ("Promoters and promoter-group members buying or selling their own company's shares "
        "through the market, in every listed company, from three sources:\n"
        "- **their insider-trading disclosures** (SEBI PIT), loaded every 30 minutes;\n"
        "- **NSE's bulk and block deal files**, collected every 15 minutes, when the client is "
        "one of the company's known promoters (named in its earlier disclosures): the deal "
        "day itself, before the disclosure;\n"
        "- **takeover-code (SEBI SAST) disclosures** that name a known promoter.\n\n"
        "Each is sent once on Telegram (the institutional deals chat) by Settings > Alerts, "
        "`promoter_trades`. Below, each trade's move against the Nifty 500 from the trade "
        "day's close keeps the record from here (docs/PROMOTER_TRADES.md).")
SOURCE = {'disclosure': 'insider disclosure', 'bulk': 'bulk deal', 'block': 'block deal',
          'sast': 'takeover-code filing'}


def _f(v, digits=2):
    return round(float(v), digits) if v is not None else None


def page(conn) -> None:
    st.header("Promoter trades")
    st.caption(NOTE)
    cur = conn.execute('''select * from promoter_trade
        order by first_seen_at desc, trade_id desc limit 500''')
    columns = [c.name for c in cur.description]
    trades = [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]
    if not trades:
        st.info("None yet: trades appear after the next data check (every 30 minutes) or "
                "deals collection (every 15 minutes) that finds one.")
        return
    rows = []
    for t in trades:
        moved = outcomes(conn, t) if t['trade_date'] and t['side'] != 'unknown' else {}
        rows.append({
            'found (IST)': t['first_seen_at'].astimezone(IST).replace(tzinfo=None),
            'trade date': t['trade_date'], 'stock': t['symbol'],
            'company': t['company_name'], 'side': t['side'], 'who': t['person'],
            'as': t['category'], 'source': SOURCE[t['source']],
            'shares': _f(t['quantity'], 0), 'value (Rs crore)': (
                round(float(t['value_inr']) / 1e7, 2) if t['value_inr'] else None),
            'holding after %': _f(t['holding_after_pct']),
            'next session vs Nifty 500 %': moved.get('next session'),
            '21 sessions vs Nifty 500 %': moved.get('21 sessions'),
            'sent': t['alerted_at'] is not None, 'link': t['link']})
    frame = pl.DataFrame(rows, infer_schema_length=None, schema_overrides={
        c: pl.Float64 for c in ('shares', 'value (Rs crore)', 'holding after %',
                                'next session vs Nifty 500 %', '21 sessions vs Nifty 500 %')})
    summary = (frame.filter(pl.col('side') != 'unknown').group_by('side')
               .agg(pl.len().alias('trades'),
                    pl.col('next session vs Nifty 500 %').count().alias('with a next session'),
                    pl.col('next session vs Nifty 500 %').mean().round(2)
                    .alias('average next session %'),
                    pl.col('21 sessions vs Nifty 500 %').count().alias('with 21 sessions'),
                    pl.col('21 sessions vs Nifty 500 %').mean().round(2)
                    .alias('average 21 sessions %'))
               .sort('side'))
    st.subheader("The record so far")
    st.dataframe(summary, hide_index=True)
    st.subheader("Trades")
    st.dataframe(frame, hide_index=True,
                 column_config={'link': st.column_config.LinkColumn('link')})
