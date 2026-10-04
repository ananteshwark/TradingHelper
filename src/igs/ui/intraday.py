"""Intraday page: stored scans, current validity and administrator-only feed setup."""
from __future__ import annotations

import datetime as dt

import streamlit as st

from igs import envfile
from igs.intraday.context import add_investor_event
from igs.intraday.engine import trading_window
from igs.intraday.scanner import candidates, latest, token
from igs.timeutil import IST, utc_now
from igs.ui import auth


def active(result, now):
    expires = result.get('expires_at')
    return (trading_window(now) and result.get('action') in ('buy', 'sell') and bool(expires)
            and now < dt.datetime.fromisoformat(expires))


@st.cache_data(ttl=20, max_entries=4, show_spinner=False)
def _latest(_conn):
    return latest(_conn)


def settings(conn):
    with st.expander('Upstox connection · administrator'):
        st.caption('The background scanner checks up to 100 NSE stocks every five minutes, '
                   'prioritizing recent AI/broker calls and then fundamental scores.')
        st.markdown('[Get an Upstox access token]'
                    '(https://upstox.com/developer/api-documentation/authentication/)')
        st.write('Token saved' if token() else 'Access token required')
        with st.form('intraday_token', clear_on_submit=True):
            value = st.text_input('Upstox access token', type='password', autocomplete='off',
                                  help='Stored in the private server .env file. '
                                       'Replace it here when Upstox expires it.')
            if st.form_submit_button('Save token'):
                auth.require_access(admin=True)
                try:
                    envfile.set_value(envfile.default_path(), 'UPSTOX_ACCESS_TOKEN', value.strip())
                except ValueError:
                    st.error('Enter a non-empty token without whitespace.')
                else:
                    st.success('Token saved privately. The next scheduled scan will use it.')
        if st.button('Remove saved token'):
            auth.require_access(admin=True)
            envfile.unset(envfile.default_path(), 'UPSTOX_ACCESS_TOKEN')
            st.success('Saved token removed.')

    with st.expander('Add a verified investor disclosure'):
        st.caption('Use named public bulk/block-deal or institutional disclosures. '
                   'FII/DII market totals cannot establish that an institution bought this stock.')
        stocks = candidates(conn)
        if not stocks:
            st.info('Load the NSE instrument master first.')
            return
        by_id = {r['company_id']: r for r in stocks}
        with st.form('intraday_investor', clear_on_submit=True):
            cid = st.selectbox('Stock', list(by_id),
                               format_func=lambda k: by_id[k]['symbol'])
            investor = st.text_input('Investor name as disclosed')
            category = st.selectbox('Verified investor category', ['prominent', 'FII', 'DII'])
            side = st.selectbox('Disclosed transaction', ['buy', 'sell'])
            trade_date = st.date_input('Trade date (IST)', value=utc_now().astimezone(IST).date())
            published = st.datetime_input('Publication time (IST)',
                value=utc_now().astimezone(IST).replace(tzinfo=None))
            url = st.text_input('Public source URL')
            quote = st.text_area('Source evidence and quantity')
            if st.form_submit_button('Save verified disclosure'):
                auth.require_access(admin=True)
                try:
                    if published is None or trade_date is None:
                        raise ValueError('Trade date and publication time are required')
                    add_investor_event(conn, company_id=cid, investor=investor,
                        category=category, side=side, trade_date=trade_date,
                        published_at=published.replace(tzinfo=IST), source_url=url, evidence=quote)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success('Disclosure saved. It will be included in the next scan.')

    with st.expander('Prominent investors and institutions · verified names'):
        st.caption('NSE bulk/block deals are collected hourly. Classify exact disclosed names '
                   'after verifying the investor. Unknown names remain unclassified and '
                   'do not influence call direction. Quarterly FII/DII holdings are not live buys.')
        names = [r[0] for r in conn.execute(
            'select distinct investor from intraday_investor_event order by investor').fetchall()]
        if names:
            with st.form('intraday_investor_register'):
                name = st.selectbox('Disclosed investor', names)
                category = st.selectbox('Investor classification', ['prominent', 'FII', 'DII'])
                if st.form_submit_button('Save verified classification'):
                    auth.require_access(admin=True)
                    conn.execute('''insert into intraday_investor_watch(investor,category)
                        values(%s,%s) on conflict(investor) do update
                        set category=excluded.category''',
                        (name.upper(), category))
                    conn.commit()
                    st.success('Exact name registered for future scans.')
            register = conn.execute('select investor,category from intraday_investor_watch '
                                    'order by investor').fetchall()
            if register:
                st.dataframe([{'Investor': n, 'Category': c} for n, c in register], hide_index=True)
        else:
            st.info('Names will appear after the first bulk/block collection.')


@st.fragment(run_every='30s')
def readings(conn):
    auth.require_access()
    run, rows = _latest(conn)
    if not run:
        st.info('No intraday scan yet. The scheduled scanner will populate this page.')
        return
    now = utc_now()
    st.caption(f"Last scan: {run['started_at'].astimezone(IST):%d %b %Y %H:%M:%S} IST · "
               f"{run['status']} · {run['scanned']} stocks checked")
    if run['message']:
        st.warning(run['message'])
    if not trading_window(now):
        st.info('Entry window closed. Active calls are limited to 09:30–15:15 IST '
                'on weekdays with fresh exchange trading data.')
    usable = run['status'] == 'complete'
    live = [r for r in rows if usable and active(r['result'], now)]
    cols = st.columns(3)
    cols[0].metric('Active buys', sum(r['result']['action'] == 'buy' for r in live))
    cols[1].metric('Active sells', sum(r['result']['action'] == 'sell' for r in live))
    cols[2].metric('Stocks checked', run['scanned'])
    st.caption('Rule-based setups on five-minute candles. Strength describes supporting '
               'evidence, not a calibrated success probability. Prices are candle-close '
               'references; spread, slippage, price bands and short-sale eligibility '
               'must be checked with the broker before trading.')
    show_all = st.checkbox('Show waiting and expired setups', value=not bool(live))
    shown = rows if show_all else live
    table = []
    for r in shown:
        s = r['result']
        valid = usable and active(s, now)
        action = s['action'].upper() if valid else ('EXPIRED' if s['action'] != 'wait' else 'WAIT')
        table.append({'Stock': r['symbol'], 'Call': action,
            'Reference ₹': s.get('reference'), 'Stop ₹': s.get('stop'), 'Target ₹': s.get('target'),
            'Volume jump ×': s.get('rvol'), '15-min momentum %': s.get('momentum_pct'),
            'VWAP ₹': s.get('vwap'), 'Turnover ₹ crore': s.get('turnover_cr'),
            'Strength': s.get('strength', '—'), 'Reason': s['reason'],
            'Candle close (IST)': dt.datetime.fromisoformat(s['candle_end']).astimezone(
                IST).strftime('%H:%M') if s.get('candle_end') else '—'})
    if table:
        st.dataframe(table, hide_index=True)
        symbol = st.selectbox('Evidence for stock', [r['symbol'] for r in shown])
        chosen = next(r for r in shown if r['symbol'] == symbol)
        st.subheader(f'{symbol} · evidence')
        evidence = chosen['result'].get('evidence', [])
        if not evidence:
            st.info('No recent mapped news or verified investor disclosure available. '
                    'This is not evidence that no such activity occurred.')
        for e in evidence:
            with st.container(border=True):
                st.write(f"{e['kind']} · {e['title']}")
                st.caption('Published / first observed '+dt.datetime.fromisoformat(
                    e['published_at']).astimezone(
                    IST).strftime('%d %b %Y %H:%M IST'))
                st.write(e['detail'])
                if (e.get('url') or '').startswith('https://'):
                    st.link_button('Read source', e['url'])
    else:
        st.info('No active setup meets all of the intraday rules.')


def page(conn):
    auth.require_access()
    st.header('Intraday calls', icon=':material/query_stats:')
    st.caption('Upstox · momentum · unusual volume · market direction · news and disclosed trades')
    if auth.is_admin():
        settings(conn)
    readings(conn)
