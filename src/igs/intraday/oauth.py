"""User-completed Upstox OAuth; credentials and codes never enter logs."""
from __future__ import annotations

import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

AUTHORIZATION_URL = 'https://api.upstox.com/v2/login/authorization/dialog'
TOKEN_URL = 'https://api.upstox.com/v2/login/authorization/token'


class OAuthError(ValueError):
    pass


def begin(api_key, api_secret, redirect_uri, *, now=None):
    api_key, api_secret, redirect_uri = (
        value.strip() for value in (api_key, api_secret, redirect_uri))
    if not all((api_key, api_secret, redirect_uri)) or any(
            c.isspace() for value in (api_key, api_secret, redirect_uri) for c in value):
        raise OAuthError('Enter the API key, API secret and registered redirect URL.')
    try:
        url = urlsplit(redirect_uri)
        valid = (url.scheme == 'https' and url.hostname and not url.username
                 and not url.password and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise OAuthError('Use a registered HTTPS redirect URL without query parameters.')
    return {'client_id': api_key, 'client_secret': api_secret, 'redirect_uri': redirect_uri,
            'state': secrets.token_urlsafe(32), 'started': time.time() if now is None else now}


def login_url(pending):
    return AUTHORIZATION_URL + '?' + urlencode({
        'response_type': 'code', 'client_id': pending['client_id'],
        'redirect_uri': pending['redirect_uri'], 'state': pending['state']})


def exchange(pending, returned_url, *, now=None, transport=None):
    """Called once per pending login. The UI consumes pending state before calling."""
    elapsed = (time.time() if now is None else now) - pending['started']
    if not 0 <= elapsed <= 600:
        raise OAuthError('This login expired. Start a new Upstox connection.')
    try:
        returned = urlsplit(returned_url.strip())
        expected = urlsplit(pending['redirect_uri'])
        params = parse_qs(returned.query, strict_parsing=True)
        valid = (returned.scheme == expected.scheme and returned.netloc == expected.netloc
                 and (returned.path or '/') == (expected.path or '/') and not returned.fragment
                 and len(params.get('state', [])) == 1
                 and secrets.compare_digest(params['state'][0], pending['state']))
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise OAuthError('The returned URL does not match this login. Start again.')
    if 'error' in params:
        raise OAuthError('Upstox did not authorize the connection. Start again.')
    codes = params.get('code', [])
    if len(codes) != 1 or not codes[0] or any(c.isspace() for c in codes[0]):
        raise OAuthError('The returned URL has no valid authorization code. Start again.')
    try:
        with httpx.Client(timeout=15, transport=transport, follow_redirects=False) as client:
            response = client.post(TOKEN_URL, data={
                'code': codes[0], 'client_id': pending['client_id'],
                'client_secret': pending['client_secret'],
                'redirect_uri': pending['redirect_uri'], 'grant_type': 'authorization_code'})
        if response.status_code != 200:
            raise OAuthError('Upstox refused token generation. Check the app credentials '
                             'and redirect URL, then start a new login.')
        token = response.json().get('access_token')
        if (not isinstance(token, str) or not token
                or any(c.isspace() for c in token) or '#' in token):
            raise OAuthError('Upstox returned no usable token. Start a new login.')
        return token
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        if isinstance(exc, OAuthError):
            raise
        raise OAuthError('Token generation could not be completed. Start a new login; '
                         'authorization codes cannot be reused.') from None
