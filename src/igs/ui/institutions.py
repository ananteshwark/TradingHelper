"""The institutional deals page (igs.institutions): prominent FIIs' and DIIs' bulk and block
deals as they arrive, and how the stocks moved against the Nifty 500 afterwards."""
from __future__ import annotations

import datetime as dt

import polars as pl
import streamlit as st

from igs import institutions
from igs.timeutil import IST
from igs.ui import filter_state

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


def filter_deals(deals: list[dict], kinds: list[str], types: list[str],
                 sides: list[str], search: str, period: str) -> list[dict]:
    """Select displayed deals before calculating their price outcomes."""
    days = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90,
            "Last year": 365}.get(period)
    first = (dt.datetime.now(IST).date() - dt.timedelta(days=days - 1)
             if days else None)
    needle = search.strip().casefold()
    return [d for d in deals
            if (not kinds or d['kind'] in kinds)
            and (not types or d['investor_type'] in types)
            and (not sides or d['side'] in sides)
            and (not needle or any(needle in str(d.get(key) or '').casefold()
                                   for key in ('symbol', 'name', 'security_name',
                                               'institution', 'client_names')))
            and (first is None or d['trade_date'] >= first)]


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
    total = len(deals)
    kind_col, type_col, side_col = st.columns(3)
    with kind_col:
        choices = sorted({d['kind'] for d in deals})
        filter_state.restore("deals_kind", [], choices)
        kinds = st.multiselect("Deal", choices, key="deals_kind",
                               on_change=filter_state.save, args=("deals_kind",))
    with type_col:
        choices = sorted({d['investor_type'] for d in deals})
        filter_state.restore("deals_type", [], choices)
        types = st.multiselect("Investor type", choices, key="deals_type",
                               on_change=filter_state.save, args=("deals_type",))
    with side_col:
        choices = sorted({d['side'] for d in deals})
        filter_state.restore("deals_side", [], choices)
        sides = st.multiselect("Side", choices, key="deals_side",
                               on_change=filter_state.save, args=("deals_side",))
    search_col, period_col = st.columns(2)
    with search_col:
        filter_state.restore("deals_search", "")
        search = st.text_input("Stock or institution", key="deals_search",
                                on_change=filter_state.save, args=("deals_search",))
    with period_col:
        filter_state.restore("deals_period", "All dates")
        period = st.selectbox("Trade date", ["All dates", "Last 7 days", "Last 30 days",
                                                 "Last 90 days", "Last year"], key="deals_period",
                              on_change=filter_state.save, args=("deals_period",))
    deals = filter_deals(deals, kinds, types, sides, search, period)
    st.caption(f"Showing {len(deals)} of {total} deals (latest 300 loaded). "
               "The record below uses the filtered deals.")
    if not deals:
        st.info("No deals match these filters.")
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
    st.dataframe(summary, hide_index=True, width="stretch")
    st.subheader("Deals")
    st.dataframe(frame, hide_index=True, width="stretch")
