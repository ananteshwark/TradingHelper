"""Sign-in screen and per-action authorization.

IGS_AUTH_MODE=password (the default) uses the app's own accounts: the /signin page sets a
session cookie (igs.ui.signin) and every check here looks the session up again
(igs.accounts). oidc uses an identity provider through Streamlit's st.login; local has
no sign-in and works only on this computer.
"""
import logging
import time
from functools import wraps
from urllib.parse import urlparse

import streamlit as st

from igs.access import access_decision, load_policy, mode

LOCAL = ('127.0.0.1', 'localhost', '::1')


def local_mode() -> bool:
    return mode() == 'local' and (st.get_option('server.address') or '') in LOCAL


def session_token() -> str | None:
    """The password sign-in's session cookie, as the browser sent it when the page opened."""
    from igs.ui.signin import COOKIE
    try:
        return st.context.cookies.get(COOKIE)
    except Exception:  # noqa: BLE001 - no browser behind this run
        return None


def signed_in_name() -> str | None:
    """The account signed in with a password, or None (other modes, or signed out)."""
    if mode() != 'password':
        return None
    from igs.ui.signin import current
    user = current(session_token())
    return user.username if user else None


def current_role() -> str | None:
    try:
        if local_mode():
            return 'admin'
        if mode() == 'password':
            from igs.ui.signin import current
            user = current(session_token())
            st.session_state['_access_denial'] = None if user else 'signed_out'
            return user.role if user else None
        if mode() != 'oidc':
            return None
        policy = load_policy()
        role, reason = access_decision(dict(st.user), policy, time.time())
        st.session_state['_access_denial'] = reason
        last = st.session_state.get('_last_activity')
        if last is not None and time.time() - last >= policy.idle_minutes * 60:
            st.session_state['_access_denial'] = 'idle_timeout'
            return None
        return role
    except Exception:  # noqa: BLE001 - a missing/broken policy never grants access
        st.session_state['_access_denial'] = 'configuration_error'
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
        elif mode() == 'password':
            from igs import accounts
            from igs.ui.signin import current
            # A full page run is activity; table refreshes on a timer are not.
            user = current(session_token(), touch=True)
            st.sidebar.caption(f"Signed in as {user.username if user else 'you'} · {role}")
            if st.sidebar.button('Sign out', icon=':material/logout:'):
                accounts.sign_out(session_token())
                logging.getLogger('igs.auth').info('Signed out: %s', user and user.username)
                st.rerun()
        else:
            st.sidebar.caption(f"Signed in as {st.user.get('email', 'approved account')} · {role}")
            if st.sidebar.button('Sign out', icon=':material/logout:'):
                st.logout()
                st.stop()
        return role
    # Nothing from the application database is loaded before this gate.
    if mode() == 'password':
        signed_out()
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
            reason = st.session_state.get('_access_denial', 'invalid_identity')
            messages = {
                'mfa_required': 'Sign-in succeeded, but Auth0 did not confirm '
                    'multi-factor authentication. Enable MFA for this application, '
                    'then sign out and sign in again.',
                'account_not_approved': 'This account is not approved, or its email '
                    'is not verified. Use your approved account and verify its email with Auth0.',
                'expired_session': 'Your session expired. Sign out and sign in again.',
                'idle_timeout': 'Your session expired due to inactivity. '
                    'Sign out and sign in again.',
            }
            st.warning(messages.get(reason, 'Sign-in could not be validated. '
                       'Sign out and try again, or contact the administrator.'))
            if st.session_state.get('_logged_access_denial') != reason:
                logging.getLogger('igs.auth').warning('Sign-in denied: %s', reason)
                st.session_state['_logged_access_denial'] = reason
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


def signed_out():
    """Shown when the session ends while the page is open, e.g. after signing out; a page
    opened without one goes to /signin before the app loads (igs.ui.signin.SignInFirst)."""
    try:
        policy = load_policy()
        limits = (f' after {policy.idle_minutes} minutes without activity, or '
                  f'{policy.session_hours} hours after signing in')
    except Exception:  # noqa: BLE001
        limits = ''
    st.title('IndiaGrowthScreener')
    with st.container(border=True):
        st.subheader('Sign in to continue', icon=':material/lock:')
        st.write(f'You signed out, or your session ended{limits}.')
        # A plain link opens the sign-in page in this tab (st.link_button opens a new one).
        st.html('<a href="/signin" style="display:inline-block;padding:8px 18px;'
                'border-radius:8px;background:#2a78d6;color:#fff;font-weight:600;'
                'text-decoration:none">Sign in</a>')
    st.stop()
