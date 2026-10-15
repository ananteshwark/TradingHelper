"""Intraday page: stored scans, current validity and administrator-only feed setup."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

import streamlit as st

from igs import envfile
from igs.intraday import eligibility, outcomes
from igs.intraday.context import add_investor_event
from igs.intraday.engine import trading_window
from igs.intraday.scanner import active_calls, candidates, latest, token
from igs.intraday.trading import TradeError, approve, trading_token
from igs.intraday.trading import settings as trade_settings
from igs.timeutil import IST, utc_now
from igs.ui import auth, upstox_connect


def active(result, now):
    expires = result.get('expires_at')
    return (trading_window(now) and result.get('action') in ('buy', 'sell') and bool(expires)
            and now < dt.datetime.fromisoformat(expires))


@st.cache_data(ttl=20, max_entries=4, show_spinner=False)
def _latest(_conn):
    return latest(_conn)


def trading_settings(conn):
    """Live trading, automatic orders, limits and order levels (Settings page)."""
    cfg = trade_settings(conn)
    st.caption('A fresh call can be approved on its page (Intraday calls, or ML intraday) or '
               'by a reply to its Telegram alert. Rule-based calls above 50× same-time volume '
               'can also be placed automatically when the option below is enabled. Upstox '
               'receives an intraday GTT entry with a linked stop, and a target for a '
               'rule-based call; an ML call is squared off by the app at 15:00 IST. Short '
               'SELL entries are allowed. An unfilled entry is cancelled after the minutes '
               'set below; a filled entry keeps its exits.')
    if not trading_token():
        st.warning('A trading OAuth token is required. The market-data Analytics token '
                   'cannot place orders. Save it on the Upstox tab before enabling.')
    with st.form('intraday_trading_settings'):
        enabled = st.checkbox('Enable live approved trading', value=cfg['enabled'])
        ml_orders = st.checkbox(
            'Real orders for the ML calls', value=cfg['ml_orders_enabled'],
            help='Each ML intraday call gets its own Telegram message: reply APPROVED to it '
                 'before 10:00 IST, or approve it on the ML intraday page. The order enters '
                 'at the live price with a stop at half the 14-day ATR and is squared off '
                 'by the app at 15:00 IST. The amount per trade, maximum loss, price '
                 'deviation and daily limits below apply. Needs live trading on as well.')
        rule_calls = st.checkbox(
            'Rule-based calls: alerts, approvals and automatic orders',
            value=cfg['rule_calls_enabled'],
            help='Off: the rule-based calls are still scanned and kept on paper on the '
                 'Intraday calls page, but none is sent to Telegram, approved or placed. '
                 'They lost money after charges in every test (docs/INTRADAY_FILTER.md).')
        auto_high_volume = st.checkbox('Automatically place calls above 50× volume',
                                       value=cfg['auto_high_volume_enabled'],
                                       help='Strictly above 50× the median volume for '
                                            'the same five-minute slot in prior '
                                            'sessions. Uses the same order levels, '
                                            'trade, loss and daily limits, and linked '
                                            'exits as an approved call, with its own '
                                            'minimum reward-to-risk.')
        per_trade_text = st.text_input('Amount per trade (₹)',
                                       value=str(cfg['max_trade_rupees']))
        trades_text = st.text_input('Maximum trades per day',
                                    value=str(cfg['max_daily_trades']))
        daily_text = st.text_input('Maximum daily gross order value (₹)',
                                   value=str(cfg['max_daily_rupees']))
        risk_text = st.text_input('Maximum loss per trade at the stop (₹)',
                                  value=str(cfg['max_risk_rupees']))
        ratio_text = st.text_input('Minimum reward-to-risk after charges',
                                   value=str(cfg['min_net_reward_risk']),
                                   help='The target, net of estimated charges, must earn '
                                   'at least this multiple of what the stop loses with '
                                   'charges. Charge rates: config/costs.yaml, intraday.')
        auto_ratio_text = st.text_input(
            'Minimum reward-to-risk after charges, automatic orders',
            value=str(cfg['auto_min_net_reward_risk']),
            help='The same check for calls placed automatically above 50× volume. A '
                 'call that falls short is skipped. Approved calls use the minimum above.')
        offset_text = st.text_input(
            'Entry limit: % better than the call\'s price (0 = the call\'s price)',
            value=str(cfg['entry_offset_pct']),
            help='Below the call\'s price for a BUY, above it for a SELL. A better price '
                 'fills less often: on July–October 2026 Upstox data, entries 1% better '
                 'filled on about 3–5% of calls within ten minutes, 0.5% better on about '
                 '17–20%, and at the call\'s price on about 80–98%. Under 5.')
        stop_text = st.text_input(
            'Stop: % beyond the entry limit (0 = the call\'s own stop)',
            value=str(cfg['stop_pct']),
            help='Below the limit for a BUY, above it for a SELL. The call\'s own stop '
                 'is 0.4% to 2% from its price. At most 5.')
        minutes_text = st.text_input(
            'Minutes an unfilled entry stays open',
            value=str(cfg['order_minutes']),
            help='Counted from when the order is placed; cancelled by 15:15 IST at the '
                 'latest. 1 to 360.')
        st.caption('The target is always the call\'s. Calls are shown and alerted only '
                   'when the order at these levels passes the reward-to-risk minimum. '
                   'Enter positive values. There are no fixed application ceilings; '
                   'Upstox and the exchange still enforce their own order rules. '
                   'Quantity is the smaller of the amount per trade and the maximum loss '
                   'divided by the stop distance.')
        if st.form_submit_button('Save trading settings'):
            auth.require_access(admin=True)
            try:
                per_trade = Decimal(per_trade_text.replace(',', '').strip())
                daily = Decimal(daily_text.replace(',', '').strip())
                trades = int(trades_text.replace(',', '').strip())
                risk = Decimal(risk_text.replace(',', '').strip())
                ratio = Decimal(ratio_text.strip())
                auto_ratio = Decimal(auto_ratio_text.strip())
                offset = Decimal(offset_text.strip().rstrip('%').strip())
                stop_pct = Decimal(stop_text.strip().rstrip('%').strip())
                minutes = int(minutes_text.strip())
                if (not all(v.is_finite() for v in (per_trade, daily, risk, ratio,
                                                    auto_ratio, offset, stop_pct))
                        or min(per_trade, daily, risk) <= 0 or min(ratio, auto_ratio) < 0
                        or trades <= 0 or not 0 <= offset < 5 or not 0 <= stop_pct <= 5
                        or not 1 <= minutes <= 360):
                    raise ValueError
                if enabled and not trading_token():
                    st.error('Save a trading OAuth token before enabling live orders.')
                else:
                    conn.execute('''update intraday_trading_settings set enabled=%s,
                        max_trade_rupees=%s,max_daily_trades=%s,max_daily_rupees=%s,
                        max_risk_rupees=%s,min_net_reward_risk=%s,
                        auto_high_volume_enabled=%s,auto_min_net_reward_risk=%s,
                        entry_offset_pct=%s,stop_pct=%s,order_minutes=%s,
                        ml_orders_enabled=%s,rule_calls_enabled=%s
                        where singleton=true''', (enabled, per_trade, trades, daily, risk,
                                                  ratio, auto_high_volume, auto_ratio,
                                                  offset, stop_pct, minutes, ml_orders,
                                                  rule_calls))
                    conn.commit()
                    st.success('Trading settings saved.')
            except (InvalidOperation, ValueError):
                st.error('Enter positive amounts, a positive whole trade count, '
                         'reward-to-risk minimums of zero or more, an entry offset '
                         'from 0 to under 5%, a stop from 0 to 5% and 1 to 360 '
                         'minutes.')


def upstox_settings(conn):
    """The Upstox app connection, market-data token and trading token (Settings page)."""
    upstox_connect.render()
    st.divider()
    st.caption('The background scanner checks up to 100 NSE stocks every five minutes, '
               'prioritizing recent AI/broker calls and then fundamental scores.')
    st.markdown('[Get an Upstox access token]'
                '(https://upstox.com/developer/api-documentation/authentication/)')
    st.write('Market-data token saved' if token() else 'Market-data token required')
    with st.form('intraday_token', clear_on_submit=True):
        value = st.text_input('Market-data token (Analytics or OAuth)', type='password',
                              autocomplete='off', help='Used only for scanning candles. '
                              'Stored in the private server .env file.')
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

    st.divider()
    st.markdown('[Generate a trading OAuth access token]'
                '(https://upstox.com/developer/api-documentation/authentication/)')
    st.caption('Use the standard OAuth access token from a trading-enabled Upstox '
               'developer app. The Analytics token is read-only. Upstox trading '
               'tokens expire at 03:30 IST the next day; replace this token when renewed.')
    st.write('Trading token saved' if trading_token() else 'Trading token required')
    with st.form('intraday_trading_token', clear_on_submit=True):
        trade_value = st.text_input('Trading OAuth access token', type='password',
                                    autocomplete='off', help='Stored privately on the server.')
        if st.form_submit_button('Save trading token'):
            auth.require_access(admin=True)
            try:
                envfile.set_value(envfile.default_path(), 'UPSTOX_TRADING_TOKEN',
                                  trade_value.strip())
            except ValueError:
                st.error('Enter a non-empty token without whitespace.')
            else:
                st.success('Trading token saved. Live trading is switched on in the '
                           'Intraday trading tab.')
    if st.button('Remove trading token'):
        auth.require_access(admin=True)
        envfile.unset(envfile.default_path(), 'UPSTOX_TRADING_TOKEN')
        conn.execute('update intraday_trading_settings set enabled=false '
                     'where singleton=true')
        conn.commit()
        st.success('Trading token removed; live trading disabled.')


def investor_names(conn):
    """Verified classifications of disclosed investor names (Settings page)."""
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


def investor_disclosure(conn):
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
    try:
        allowed = eligibility.allowed_instruments(now=now) if trading_window(now) else set()
    except eligibility.FeedError as exc:
        allowed = set()
        st.error(str(exc))
    rows = [r for r in rows if r['instrument_key'] in allowed] if trading_window(now) else rows
    # Open calls come from today's completed scans, so a call stays approvable until its
    # expiry while the next scan runs or reads 'wait' (scanner.open_calls).
    live = [r for r in active_calls(conn, now) if r['instrument_key'] in allowed]
    rules_on = bool((trade_settings(conn) or {}).get('rule_calls_enabled'))
    if not rules_on:
        st.info('Rule-based calls are switched off (Settings, Intraday trading): they are '
                'scanned and kept on paper below, but none is sent to Telegram, approved or '
                'placed. They lost money after charges in every test; the ML intraday page '
                'has the calls that passed theirs.')
    st.caption('Current Upstox MIS-eligible NSE equities only; suspended stocks are excluded. '
               'Eligibility refreshes every five minutes and is checked again at approval.')
    cols = st.columns(3)
    cols[0].metric('Active buys', sum(r['result']['action'] == 'buy' for r in live))
    cols[1].metric('Active sells', sum(r['result']['action'] == 'sell' for r in live))
    cols[2].metric('Stocks checked', run['scanned'])
    st.caption('Only setups whose order passes your trading settings (amount per trade, '
               'maximum loss and minimum reward-to-risk after charges; the automatic minimum '
               'for calls above 50× volume when automatic placement is on) are calls. Every '
               "order's limit is 1% better than the call's price, its stop 1% beyond that "
               "limit, its target the call's. Others read wait, with the reason; tick "
               '"Show waiting" to see them.')
    st.caption('Rule-based setups on five-minute candles. Strength describes supporting '
               'evidence, not a calibrated success probability. Prices are candle-close '
               'references; spread, slippage, price bands and short-sale eligibility '
               'must be checked with the broker before trading.')
    show_all = st.checkbox('Show waiting and expired setups', value=not bool(live))
    shown = rows if show_all else live
    open_keys = {(r['company_id'], r['scan_id']) for r in live}
    table = []
    for r in shown:
        s = r['result']
        valid = (r['company_id'], r['scan_id']) in open_keys
        action = s['action'].upper() if valid else ('EXPIRED' if s['action'] != 'wait' else 'WAIT')
        order = s.get('order') or {}
        table.append({'Stock': r['symbol'], 'Call': action,
            'Reference ₹': s.get('reference'), 'Stop ₹': s.get('stop'), 'Target ₹': s.get('target'),
            'Order': ('Automatic' if order.get('automatic') else 'Approval') if order else '—',
            'Shares': order.get('quantity'), 'Order limit ₹': order.get('entry'),
            'Order stop ₹': order.get('stop'), 'Order target ₹': order.get('target'),
            'Net at target ₹': order.get('net_gain'),
            'Net at stop ₹': -order['net_loss'] if order else None,
            'Reward:risk after charges': order.get('reward_risk'),
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
        if auth.is_admin() and live and rules_on:
            with st.expander('Approve an Upstox intraday order'):
                cfg = trade_settings(conn)
                if not cfg['enabled']:
                    st.info('Enable live approved trading in Settings, Intraday trading '
                            'tab, first.')
                elif not trading_token():
                    st.info('Save a trading OAuth token in Settings, Upstox tab, first.')
                else:
                    by_id = {r['company_id']: r for r in live}
                    with st.form('approve_intraday_order'):
                        cid = st.selectbox('Current call', list(by_id),
                            format_func=lambda k: f"{by_id[k]['symbol']} · "
                                f"{by_id[k]['result']['action'].upper()}")
                        call = by_id[cid]['result']
                        order = call.get('order') or {}
                        st.write(f"Call: ₹{call['reference']:.2f} · stop ₹{call['stop']:.2f} "
                                 f"· target ₹{call['target']:.2f}.")
                        if order:
                            st.write(f"Order at your settings when last scanned: "
                                     f"{order['quantity']} shares, limit ₹{order['entry']:.2f} "
                                     f"· stop ₹{order['stop']:.2f} · target "
                                     f"₹{order['target']:.2f}.")
                        st.caption(f"The limit is {cfg['entry_offset_pct']}% better than the "
                                   "call's price (0: the call's price), rechecked at approval. "
                                   "It may remain unfilled; the limit will not follow the "
                                   f"market price, and is cancelled {cfg['order_minutes']} "
                                   "minutes after it is placed if unfilled.")
                        confirmed = st.checkbox('I approve this specific intraday order')
                        if st.form_submit_button('Place approved order', type='primary'):
                            auth.require_access(admin=True)
                            if not confirmed:
                                st.error('Confirm this specific order first.')
                            else:
                                try:
                                    trade_id, state = approve(conn, cid, source='admin',
                                                              expected_call=call)
                                except TradeError as exc:
                                    st.error(str(exc))
                                else:
                                    st.success(f'Trade #{trade_id}: {state}. Verify fill and '
                                               'linked exits in Upstox.')
    else:
        st.info('No active setup meets all of the intraday rules.')
    if auth.is_admin():
        with st.expander('Today’s approved intraday orders'):
            day = now.astimezone(IST).date()
            rows = conn.execute('''select symbol,case kind when 'ml' then 'ML' else 'rule' end,
                action,quantity,entry_price,stop_price,target_price,status,entry_fill,
                exit_fill,net_pnl,pnl_note,gtt_order_id,approved_at from intraday_trade
                where trading_day=%s order by trade_id desc''', (day,)).fetchall()
            if rows:
                st.dataframe([dict(zip(('Stock','Call','Side','Qty','Entry ₹','Stop ₹',
                    'Target ₹','Status','Entry fill ₹','Exit fill ₹','Net P&L ₹','P&L note',
                    'Upstox GTT ID','Approved at'), r, strict=True)) for r in rows],
                    hide_index=True, width='stretch')
            else:
                st.caption('No approved orders today.')
            pnl_totals(conn, day)


def pnl_totals(conn, day):
    """Net P&L of closed trades, after estimated charges: today and the last 30 days."""
    cols = st.columns(2)
    for col, label, since in ((cols[0], 'Net P&L today', day),
                              (cols[1], 'Net P&L, last 30 days', day - dt.timedelta(days=30))):
        count, gross, cost, net = conn.execute('''select count(*),coalesce(sum(gross_pnl),0),
            coalesce(sum(charges),0),coalesce(sum(net_pnl),0) from intraday_trade
            where trading_day>=%s and net_pnl is not null''', (since,)).fetchone()
        col.metric(label, f'₹{net:,.2f}',
                   help=f'{count} closed trade(s): ₹{gross:,.2f} before charges, less '
                        f'₹{cost:,.2f} of estimated charges.')
    st.caption('Net P&L is worked out when a trade closes at its target or stop, or at the '
               'app\'s own square-off of an ML call: Upstox\'s average entry and exit fills, '
               'less brokerage, STT, exchange and SEBI fees, stamp duty and GST estimated at '
               'those fills (config/costs.yaml). The Upstox contract note is final. A trade '
               'closed any other way shows a note instead.')


def paper_record(conn):
    """How the calls would have done: outcomes.resolve on each session's candles."""
    since = utc_now().astimezone(IST).date() - dt.timedelta(days=60)
    with st.expander('Paper record · how the calls would have done (last 60 days)'):
        st.caption('Every buy and sell call, replayed the next day on that session\'s '
                   'five-minute candles: did the limit entry fill before the call expired, '
                   'then did the stop or the target come first, or neither by 15:15? A '
                   'candle touching both counts as the stop. R is the result in units of '
                   'the stop distance, before charges and slippage. Net ₹ is each filled '
                   'call sized as an order would be with the current amount per trade and '
                   'maximum loss, less estimated brokerage, STT, fees, stamp duty and GST '
                   '(before slippage). No order was placed for these. Read the counts: a '
                   'few calls prove nothing either way.')
        rows = outcomes.summary(conn, since)
        if not rows[0]['Calls']:
            st.info('No call has been resolved yet. Calls are resolved the day after, by '
                    'the first scan that loads the stock\'s history.')
            return
        rupees = st.column_config.NumberColumn(format='₹%.2f')
        st.dataframe(rows, hide_index=True, width='stretch', column_config={
            'Win rate': st.column_config.NumberColumn(format='percent'),
            'Charges ₹': rupees, 'Net ₹': rupees})
        st.dataframe(outcomes.recent(conn, since), hide_index=True, width='stretch',
                     column_config={'Net ₹': rupees})


def page(conn):
    auth.require_access()
    st.header('Intraday calls', icon=':material/query_stats:')
    st.caption('Upstox · momentum · unusual volume · market direction · news and disclosed trades')
    if auth.is_admin():
        st.caption('Live trading, order levels, the Upstox connection and verified investor '
                   'names are on the Settings page.')
        investor_disclosure(conn)
    readings(conn)
    paper_record(conn)
