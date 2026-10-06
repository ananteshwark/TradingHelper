"""Administrator-assisted OAuth: the user signs in only on Upstox."""
import time

import streamlit as st

from igs import envfile
from igs.intraday import oauth
from igs.ui import auth

PENDING = '_upstox_oauth_pending'


def render():
    auth.require_access(admin=True)
    st.subheader('Connect your Upstox Algo app')
    st.caption('Keep this tab open. Login opens in a new tab; enter your mobile number, '
               'OTP and PIN only on Upstox. These API credentials stay in this session '
               'until you finish or cancel. Only the resulting token is saved privately.')
    st.markdown('In [Upstox Developer Apps](https://account.upstox.com/developer/apps), '
                'use your trading-enabled app and copy its exact registered redirect URL. '
                'For this site you can register `https://stocks.ednis.ai/`.')
    pending = st.session_state.get(PENDING)
    if pending and time.time() - pending['started'] > 600:
        st.session_state.pop(PENDING, None)
        pending = None
        st.info('The pending login expired. Enter the app credentials to begin again.')
    if not pending:
        with st.form('upstox_oauth_setup', clear_on_submit=True):
            key = st.text_input('Algo API key', type='password', autocomplete='off')
            secret = st.text_input('Algo API secret', type='password', autocomplete='off')
            redirect = st.text_input('Exact registered redirect URL',
                                     placeholder='https://stocks.ednis.ai/')
            if st.form_submit_button('Prepare Upstox login'):
                auth.require_access(admin=True)
                try:
                    st.session_state[PENDING] = oauth.begin(key, secret, redirect)
                except oauth.OAuthError as exc:
                    st.error(str(exc))
                else:
                    st.rerun()
        return
    st.link_button('Sign in on Upstox', oauth.login_url(pending))
    st.caption('After signing in, copy the complete URL from the new tab’s address bar '
               '(including code and state) and paste it below in this original tab. '
               'Finish within ten minutes. Do not share the URL in chat.')
    with st.form('upstox_oauth_finish', clear_on_submit=True):
        returned = st.text_input('Returned URL after Upstox login', type='password',
                                 autocomplete='off')
        if st.form_submit_button('Finish connection and save trading token'):
            auth.require_access(admin=True)
            attempt = st.session_state.pop(PENDING, None)
            try:
                if not attempt:
                    raise oauth.OAuthError('Start a new Upstox connection.')
                token = oauth.exchange(attempt, returned)
                envfile.set_value(envfile.default_path(), 'UPSTOX_TRADING_TOKEN', token)
            except oauth.OAuthError as exc:
                st.error(str(exc))
            except (OSError, ValueError):
                st.error('The server could not save the token. Check private settings storage.')
            else:
                st.success('Trading token saved. Review your trade amounts and enable live '
                           'approved trading above. No order was placed by connecting.')
    if st.button('Clear login and start again'):
        auth.require_access(admin=True)
        st.session_state.pop(PENDING, None)
        st.rerun()
