"""The institutional deals page (igs.institutions): prominent FIIs' and DIIs' bulk and block
deals as they arrive, and how the stocks moved against the Nifty 500 afterwards."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs import institutions

NOTE = ("Prominent foreign (FII) and domestic (DII) institutions' trades in NSE's bulk and "
        "block deal files, collected every 15 minutes on weekdays and sent on Telegram as "
        "they arrive (config/institutions.yaml lists who counts; Settings > Alerts, "
        "`institution_deals`). A bulk deal is a client's trade of more than 0.5% of a "
        "company's shares in a day; a block deal is one large trade in the exchange's block "
        "window. Each row is one institution's net trade in a stock that day. In 2013–2026 "
        "block deals, the stock beat the market on the deal day, when the deal is not yet "
        "public, and showed no reliable further move afterwards except after the largest "
        "FII purchases (research/institutional_deals). Below, each deal's move against the "
        "Nifty 500 from the deal day's close keeps the record from here.")


def page(conn) -> None:
    st.header("Institutional deals")
    st.caption(NOTE)
    cur = conn.execute('''select d.*, c.name from institution_deal d
        left join company c using (company_id)
        order by d.trade_date desc, d.value_inr desc limit 300''')
    columns = [c.name for c in cur.description]
    deals = [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]
    if not deals:
        st.info("None yet: deals appear after the next collection of NSE's bulk and block "
                "deal files in which a listed institution traded.")
        return
    rows = []
    for d in deals:
        moved = institutions.outcomes(conn, d)
        value = float(d['value_inr'])
        rows.append({
            'trade date': d['trade_date'], 'deal': d['kind'], 'institution': d['institution'],
            'type': d['investor_type'], 'side': d['side'], 'stock': d['symbol'],
            'company': d['name'] or d['security_name'],
            'shares': float(d['quantity']), 'price (Rs)': round(float(d['price']), 2),
            'value (Rs crore)': round(value / 1e7, 2),
            '% of shares': (round(float(d['pct_of_shares']), 2)
                            if d['pct_of_shares'] is not None else None),
            "x day's trading": (round(value / float(d['adv_inr']), 1)
                                if d['adv_inr'] else None),
            'next session vs Nifty 500 %': moved.get('next session'),
            '21 sessions vs Nifty 500 %': moved.get('21 sessions'),
            'sent': d['alerted_at'] is not None, 'client name': d['client_names']})
    # Columns that are empty until prices arrive still need a numeric type.
    frame = pl.DataFrame(rows, infer_schema_length=None, schema_overrides={
        c: pl.Float64 for c in ('% of shares', "x day's trading",
                                'next session vs Nifty 500 %', '21 sessions vs Nifty 500 %')})
    summary = (frame.group_by('type', 'side')
                    .agg(pl.len().alias('deals'),
                         pl.col('next session vs Nifty 500 %').count().alias('with a next session'),
                         pl.col('next session vs Nifty 500 %').mean().round(2)
                         .alias('average next session %'),
                         pl.col('21 sessions vs Nifty 500 %').count().alias('with 21 sessions'),
                         pl.col('21 sessions vs Nifty 500 %').mean().round(2)
                         .alias('average 21 sessions %'))
                    .sort('type', 'side'))
    st.subheader("The record so far")
    st.dataframe(summary, hide_index=True, use_container_width=True)
    st.subheader("Deals")
    st.dataframe(frame, hide_index=True, use_container_width=True)
