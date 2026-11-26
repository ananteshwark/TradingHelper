"""Read-only view of the independent trade monitor; never starts a trading worker."""
import streamlit as st
from psycopg.rows import dict_row

from igs.timeutil import IST, utc_now
from igs.ui import auth


def page(conn, title=True):
    if not auth.is_admin():
        st.info('Trade monitoring is available to the account administrator.')
        return
    if title:
        st.header('Open-trade monitoring')
    st.caption('Observation is the default. Experimental trailing stops are not proven to '
               'improve returns. Broker stops remain authoritative; an observed stop is not '
               'an order. Refresh this page for the latest worker observation.')
    st.button('Refresh monitoring')
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''select t.trade_id,t.symbol,t.action,t.status,m.updated_at,
            m.snapshot from intraday_trade t left join trade_monitor_state m using(trade_id)
            where t.trading_day=%s order by t.trade_id''', (utc_now().astimezone(IST).date(),))
        rows = cur.fetchall()
        cur.execute('''select e.trade_id,t.symbol,e.created_at,e.mode,e.status,e.detail
            from trade_monitor_event e join intraday_trade t using(trade_id)
            where t.trading_day=%s order by event_id desc limit 100''',
                    (utc_now().astimezone(IST).date(),))
        events = cur.fetchall()
    if not rows:
        st.info('No app trades today. This monitor does not manage manually placed trades.')
        return
    now = utc_now()
    table = []
    for r in rows:
        snapshot = r.pop('snapshot') or {}
        age = (now - r['updated_at']).total_seconds() if r['updated_at'] else None
        r['observation'] = 'missing' if age is None else 'stale' if age > 30 else 'recent'
        r.update(snapshot)
        table.append(r)
    st.dataframe(table, hide_index=True, width='stretch')
    if any(e['status'] in ('submitting', 'uncertain') for e in events):
        st.warning('A broker modification may be unresolved. Check its order book before '
                   'any further action. The monitor will not retry uncertain modifications.')
    st.subheader('Recent monitoring events')
    st.dataframe(events, hide_index=True, width='stretch')
