"""The password sign-in page and its session cookie (IGS_AUTH_MODE=password).

Streamlit can read cookies but not set them, so signing in happens on a plain page served
by the same process (/signin, added through st.App in server.py). The app's gate finds the
session from the cookie on every run (igs.ui.auth), and a browser opening the app without
a valid session is sent to /signin before any of the app loads.
"""
from __future__ import annotations

import html
import logging
import secrets

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from igs import accounts
from igs.access import load_policy, mode

COOKIE = 'igs_session'
FORM_COOKIE = 'igs_signin'
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
log = logging.getLogger('igs.auth')
if not log.handlers:
    # Sign-ins, failures and blocks go to the service's journal (stderr), sign-ins included.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)


def current(token: str | None, *, touch: bool = False) -> accounts.User | None:
    """The user signed in with this session token, or None; never raises."""
    try:
        policy = load_policy()
        return accounts.session(token, policy.session_hours, policy.idle_minutes,
                                touch=touch)
    except Exception:  # noqa: BLE001 - a broken policy or account file never grants access
        log.exception('Session check failed')
        return None


class SignInFirst:
    """Sends a browser that opens the app without a valid session to /signin."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope['type'] == 'http' and scope['method'] in ('GET', 'HEAD')
                and scope['path'] == '/' and mode() == 'password'):
            token = Request(scope).cookies.get(COOKIE)
            if await run_in_threadpool(current, token) is None:
                await RedirectResponse('/signin', 303)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _secure(request: Request) -> bool:
    """Cookies only over HTTPS, except plain HTTP to this computer itself."""
    return request.url.scheme == 'https' or request.url.hostname not in LOOPBACK


def _same_origin(request: Request) -> bool:
    # Browsers send "null" when the page's referrer policy is no-referrer (the Caddyfile
    # sets it); the form token below still has to match.
    origin = request.headers.get('origin')
    if origin in (None, 'null'):
        return True
    return origin == f"{request.url.scheme}://{request.headers.get('host', '')}"


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · IndiaGrowthScreener</title>
<style>
:root {{ --surface: #f6f6f3; --card: #ffffff; --ink: #0b0b0b; --ink-2: #52514e;
  --line: #d9d8d0; --accent: #2a78d6; --on-accent: #ffffff; --error: #b42318;
  --error-bg: #fdecea; color-scheme: light; }}
@media (prefers-color-scheme: dark) {{ :root {{ --surface: #0e1117; --card: #171a21;
  --ink: #f2f2ef; --ink-2: #a9a8a2; --line: #33363d; --accent: #3987e5;
  --on-accent: #ffffff; --error: #ff8a80; --error-bg: #3a1d1d; color-scheme: dark; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
  background: var(--surface); color: var(--ink); padding: 24px 16px;
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ width: 100%; max-width: 380px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
.lead {{ color: var(--ink-2); margin: 0 0 24px; font-size: 15px; }}
form {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 24px; display: grid; gap: 16px; }}
h2 {{ font-size: 17px; margin: 0; }}
label {{ display: grid; gap: 6px; font-size: 14px; font-weight: 600; }}
input {{ font: inherit; color: var(--ink); background: var(--card); padding: 10px 12px;
  border: 1px solid var(--line); border-radius: 8px; width: 100%; }}
input:focus {{ outline: 2px solid var(--accent); outline-offset: 1px; border-color: transparent; }}
button {{ font: inherit; font-weight: 600; color: var(--on-accent); background: var(--accent);
  border: 0; border-radius: 8px; padding: 11px; cursor: pointer; }}
button:focus-visible {{ outline: 2px solid var(--ink); outline-offset: 2px; }}
.error {{ margin: 0; padding: 10px 12px; border-radius: 8px; font-size: 14px;
  color: var(--error); background: var(--error-bg); }}
.note {{ color: var(--ink-2); font-size: 13px; margin: 16px 0 0; }}
code {{ font-size: 13px; }}
</style></head>
<body><main>
<h1>IndiaGrowthScreener</h1>
<p class="lead">Your private workspace for Indian equity research</p>
<form method="post" action="/signin" autocomplete="on">
<h2>Sign in</h2>
{message}
<label>Username<input name="username" autocomplete="username" autocapitalize="none"
  spellcheck="false" required maxlength="64" value="{username}" {focus_user}></label>
<label>Password<input name="password" type="password" autocomplete="current-password"
  required maxlength="1024" {focus_password}></label>
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit">Sign in</button>
</form>
<p class="note">{note}</p>
</main></body></html>
"""

CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")


def _page(request: Request, error: str = '', status: int = 200,
          username: str = '') -> HTMLResponse:
    csrf = secrets.token_urlsafe(32)
    try:
        none_yet = not accounts.users()
    except Exception:  # noqa: BLE001 - the page still shows; signing in reports it
        none_yet = False
    note = ('No accounts exist yet. On the server, as the app user, run '
            '<code>uv run igs users add NAME --role admin</code>.' if none_yet else
            'Ask the administrator if you have forgotten your password.')
    body = PAGE.format(
        message=f'<p class="error" role="alert">{html.escape(error)}</p>' if error else '',
        username=html.escape(username), csrf=csrf, note=note,
        focus_user='' if username else 'autofocus',
        focus_password='autofocus' if username else '')
    response = HTMLResponse(body, status, headers={
        'Cache-Control': 'no-store', 'Content-Security-Policy': CSP,
        'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer'})
    response.set_cookie(FORM_COOKIE, csrf, max_age=3600, path='/signin', httponly=True,
                        secure=_secure(request), samesite='strict')
    if request.cookies.get(COOKIE):
        response.delete_cookie(COOKIE, path='/', secure=_secure(request), httponly=True,
                               samesite='lax')
    return response


async def signin_page(request: Request) -> Response:
    if mode() != 'password':
        return RedirectResponse('/', 303)
    if await run_in_threadpool(current, request.cookies.get(COOKIE)) is not None:
        return RedirectResponse('/', 303)
    return await run_in_threadpool(_page, request)


async def signin_submit(request: Request) -> Response:
    if mode() != 'password':
        return RedirectResponse('/', 303)
    form = await request.form()
    username = str(form.get('username', ''))[:64]
    password = str(form.get('password', ''))[:1025]
    expected = request.cookies.get(FORM_COOKIE, '')
    if (not _same_origin(request) or not expected
            or not secrets.compare_digest(expected, str(form.get('csrf', '')))):
        return await run_in_threadpool(
            _page, request, 'The sign-in page had expired. Please sign in again.', 400,
            username)
    address = request.client.host if request.client else 'unknown'
    try:
        token, result, wait = await run_in_threadpool(accounts.sign_in, username, password,
                                                      address)
        who = await run_in_threadpool(accounts.known, username)
    except Exception:  # noqa: BLE001 - e.g. an account file the app can't read
        log.exception('Sign-in failed: the account file could not be used (%s)',
                      accounts.path())
        return await run_in_threadpool(
            _page, request, "Signing in isn't working right now. The administrator can "
            "find why in the server's log.", 503, username)
    if result != 'ok':
        await run_in_threadpool(_alert, username, address)
    if result == 'blocked':
        log.warning('Sign-in blocked after repeated failures: %s from %s', who, address)
        minutes = max(1, round(wait / 60))
        return await run_in_threadpool(
            _page, request, f'Too many failed sign-ins. Try again in {minutes} minute'
            f"{'s' if minutes != 1 else ''}.", 429, username)
    if token is None:
        log.warning('Failed sign-in: %s from %s', who, address)
        return await run_in_threadpool(_page, request, 'Wrong username or password.', 401,
                                       username)
    log.info('Signed in: %s from %s', who, address)
    policy = load_policy()
    await run_in_threadpool(accounts.end_expired, policy.session_hours, policy.idle_minutes)
    response = RedirectResponse('/', 303)
    response.set_cookie(COOKIE, token, max_age=policy.session_hours * 3600, path='/',
                        httponly=True, secure=_secure(request), samesite='lax')
    response.delete_cookie(FORM_COOKIE, path='/signin', secure=_secure(request),
                           httponly=True, samesite='strict')
    return response


def _alert(username: str, address: str) -> None:
    """The Telegram alert for repeated failed sign-ins, queued for the notification
    dispatcher (igs.alerts.operations.sign_in_failures)."""
    from igs.alerts.operations import sign_in_failures
    try:
        sign_in_failures(accounts.failures(username, address))
    except Exception:  # noqa: BLE001 - an alert must never stop the sign-in page
        log.exception('Could not count failed sign-ins for the alert')


async def signin(request: Request) -> Response:
    return await (signin_submit if request.method == 'POST' else signin_page)(request)


def routes() -> list[Route]:
    return [Route('/signin', signin, methods=['GET', 'POST'])]
