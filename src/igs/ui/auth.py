"""OIDC login screen and per-action authorization; no application passwords."""
import time
from functools import wraps
from urllib.parse import urlparse

import streamlit as st

from igs.access import load_policy, mode, role_for

LOCAL = ('127.0.0.1', 'localhost', '::1')


def local_mode() -> bool:
    return mode() == 'local' and (st.get_option('server.address') or '') in LOCAL


def current_role() -> str | None:
    try:
        if local_mode():
            return 'admin'
        if mode() != 'oidc':
            return None
        policy = load_policy()
        role = role_for(dict(st.user), policy, time.time())
        last = st.session_state.get('_last_activity')
        if last is not None and time.time() - last >= policy.idle_minutes * 60:
            return None
        return role
    except Exception:  # noqa: BLE001 - a missing/broken policy never grants access
        return None


def is_admin() -> bool:
    return current_role() == 'admin'


def require_access(admin=False):
    role = current_role()
    if role is None or (admin and role != 'admin'):
        st.error('Sign in with an authorized account to continue.')
        st.stop()
    return role


def admin_action(fn):
    @wraps(fn)
    def checked(*args, **kwargs):
        require_access(admin=True)
        return fn(*args, **kwargs)
    return checked


def _configured():
    try:
        auth = st.secrets['auth']
        policy = load_policy()
        return (len(auth.get('cookie_secret', '')) >= 32
                and bool(auth.get('client_id')) and bool(auth.get('client_secret'))
                and urlparse(auth.get('redirect_uri', '')).scheme == 'https'
                and urlparse(auth.get('server_metadata_url', '')).scheme == 'https'
                and bool(policy.issuer)
                and bool(policy.admin_emails or policy.admin_subjects))
    except Exception:  # noqa: BLE001
        return False


def gate():
    role = current_role()
    if role:
        st.session_state['_last_activity'] = time.time()
        if local_mode():
            st.sidebar.warning('Local development mode — do not expose this instance publicly.')
        else:
            st.sidebar.caption(f"Signed in as {st.user.get('email', 'approved account')} · {role}")
            if st.sidebar.button('Sign out', icon=':material/logout:'):
                st.logout()
                st.stop()
        return role
    # Nothing from the application database is loaded before this gate.
    st.title('IndiaGrowthScreener')
    st.caption('Your private workspace for Indian equity research')
    left, right = st.columns([3, 2], gap='large')
    with left:
        st.header('Research with a clear record.')
        st.write('Explore growth rankings, review broker and AI calls, and track how '
                 'recommendations perform over time.')
        st.caption('Rankings · Company research · Recommendation performance')
    with right, st.container(border=True):
        st.subheader('Sign in to your workspace', icon=':material/lock:')
        st.write('Access is limited to approved accounts.')
        logged_in = st.user.get('is_logged_in', False)
        if logged_in:
            st.warning('Your session expired or this account is not authorized.')
            if st.button('Sign out and try again', width='stretch'):
                st.logout()
        elif _configured():
            if st.button('Continue to secure sign-in', type='primary', width='stretch',
                         icon=':material/login:'):
                st.login()
        else:
            st.info('Secure sign-in is being configured. Please contact the administrator.')
        st.caption('Your password is handled by the sign-in provider. Never share it here.')
    st.stop()
