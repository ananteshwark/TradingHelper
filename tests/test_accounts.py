"""Password sign-in (IGS_AUTH_MODE=password): the account store with scrypt hashes and
lockout (igs.accounts), the /signin page and its cookie (igs.ui.signin), the app's gate
(igs.ui.auth) and `igs users`."""
from __future__ import annotations

import logging
import sqlite3
import stat
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from streamlit.testing.v1 import AppTest

from igs import accounts
from igs.ui import signin

PASSWORD = 'correct horse battery staple'
T0 = 1_800_000_000.0


@pytest.fixture
def owner():
    accounts.add('Owner', PASSWORD, 'admin', now=T0)
    return 'owner'


def test_passwords_are_stored_only_as_salted_scrypt_hashes(owner):
    stored = accounts.hash_password(PASSWORD)
    assert stored.startswith('scrypt$32768$8$1$') and PASSWORD not in stored
    assert accounts.hash_password(PASSWORD) != stored                 # a new salt each time
    assert accounts.verify_password(PASSWORD, stored)
    assert not accounts.verify_password(PASSWORD + '!', stored)
    for broken in ('', 'plain', stored.replace('scrypt', 'md5'), stored[:-8]):
        assert not accounts.verify_password(PASSWORD, broken)
    # The file holds the hash, never the password, and only the app user can read it.
    raw = accounts.path().read_bytes()
    assert PASSWORD.encode() not in raw
    assert stat.S_IMODE(accounts.path().stat().st_mode) == 0o600
    with sqlite3.connect(accounts.path()) as conn:
        assert conn.execute('select password_hash from account').fetchone()[0].startswith(
            'scrypt$')


def test_names_passwords_and_roles_are_checked(owner):
    for name, password, role, message in (
            ('owner', PASSWORD, 'admin', 'already exists'),
            ('bad name', PASSWORD, 'viewer', 'username is'),
            ('', PASSWORD, 'viewer', 'username is'),
            ('reader', 'short', 'viewer', 'at least 12'),
            ('reader', 'reader-and-more-text', 'viewer', 'contain the username'),
            ('reader', PASSWORD, 'superuser', 'role is one of')):
        with pytest.raises(accounts.AccountError, match=message):
            accounts.add(name, password, role)
    assert accounts.add(' Reader@Example.com ', PASSWORD, 'viewer') == 'reader@example.com'
    assert [(u['username'], u['role']) for u in accounts.users()] == [
        ('owner', 'admin'), ('reader@example.com', 'viewer')]
    assert accounts.known('OWNER') == 'owner'
    # Whatever was typed for an unknown account stays out of the logs.
    assert accounts.known('my secret password') == accounts.known('nobody') == (
        'an unknown username')


def test_sign_in_and_sessions(owner):
    assert accounts.sign_in('owner', 'wrong password!!', '1.1.1.1', now=T0)[:2] == (
        None, 'wrong')
    assert accounts.sign_in('nobody', PASSWORD, '1.1.1.1', now=T0)[:2] == (None, 'wrong')
    token, result, _ = accounts.sign_in('OWNER', PASSWORD, '1.1.1.1', now=T0)
    assert result == 'ok' and len(token) >= 40
    # Only the token's hash is stored, so the file can't be used to sign in.
    with sqlite3.connect(accounts.path()) as conn:
        assert conn.execute('select token_hash from session').fetchone()[0] != token
    assert token.encode() not in accounts.path().read_bytes()

    def at(seconds, touch=False):
        return accounts.session(token, 8, 30, touch=touch, now=T0 + seconds)
    assert at(60) == accounts.User('owner', 'admin')
    assert accounts.session('made-up', 8, 30, now=T0) is None
    assert accounts.session(None, 8, 30, now=T0) is None
    assert at(25 * 60, touch=True) and at(50 * 60)        # activity keeps it open
    assert at(50 * 60 + 30 * 60) is None                    # 30 minutes idle ends it
    assert accounts.session(token, 8, 30, now=T0) is None   # ... for good
    # Eight hours in all, however active.
    token, *_ = accounts.sign_in('owner', PASSWORD, '1.1.1.1', now=T0)
    for minute in range(20, 8 * 60, 20):
        assert accounts.session(token, 8, 30, touch=True, now=T0 + minute * 60)
    assert accounts.session(token, 8, 30, now=T0 + 8 * 3600) is None
    assert accounts.users()[0]['last_sign_in_at'] == T0


def test_signing_out_a_new_password_and_removal_end_sessions(owner):
    now = T0
    first, *_ = accounts.sign_in('owner', PASSWORD, 'a', now=now)
    second, *_ = accounts.sign_in('owner', PASSWORD, 'b', now=now)
    accounts.sign_out(first)
    assert accounts.session(first, 8, 30, now=now) is None
    assert accounts.session(second, 8, 30, now=now)
    third, *_ = accounts.sign_in('owner', PASSWORD, 'c', now=now)
    accounts.set_password('owner', 'a brand new passphrase', keep=third, now=now)
    assert accounts.session(second, 8, 30, now=now) is None
    assert accounts.session(third, 8, 30, now=now)          # the one that changed it
    assert accounts.sign_in('owner', PASSWORD, 'c', now=now)[1] == 'wrong'
    assert accounts.sign_in('owner', 'a brand new passphrase', 'c', now=now)[1] == 'ok'
    with pytest.raises(accounts.AccountError, match='only administrator'):
        accounts.remove('owner')
    accounts.add('reader', PASSWORD, 'viewer')
    reader, *_ = accounts.sign_in('reader', PASSWORD, 'd', now=now)
    accounts.remove('reader')
    assert accounts.session(reader, 8, 30, now=now) is None
    with pytest.raises(accounts.AccountError, match='no account'):
        accounts.set_password('reader', 'another long passphrase')


def test_repeated_failures_block_the_account(owner):
    for k in range(accounts.PER_ADDRESS):
        assert accounts.sign_in('owner', 'wrong password!!', '6.6.6.6', now=T0 + k)[1] == (
            'wrong')
    # Even the right password is refused from that address until 15 minutes pass ...
    token, result, wait = accounts.sign_in('owner', PASSWORD, '6.6.6.6', now=T0 + 10)
    assert (token, result) == (None, 'blocked') and 880 < wait <= 900
    # ... while the owner can still sign in from elsewhere.
    assert accounts.sign_in('owner', PASSWORD, '1.1.1.1', now=T0 + 10)[1] == 'ok'
    assert accounts.sign_in('owner', PASSWORD, '6.6.6.6', now=T0 + 16 * 60)[1] == 'ok'
    # Twenty failures from many addresses in an hour block it everywhere.
    later = T0 + 3600
    for k in range(accounts.PER_ACCOUNT):
        accounts.sign_in('owner', 'wrong password!!', f'10.0.0.{k}', now=later + k)
    assert accounts.sign_in('owner', PASSWORD, '1.1.1.1', now=later + 30)[1] == 'blocked'
    assert accounts.sign_in('owner', PASSWORD, '1.1.1.1', now=later + 3700)[1] == 'ok'


def test_failed_sign_ins_are_counted_for_the_alert(owner):
    """accounts.failures: per account from any address, per address for any username;
    a username without an account is counted, never named or stored."""
    accounts.sign_in('owner', 'wrong password!!', '9.9.9.9', now=T0 - 3600)   # too long ago
    accounts.sign_in('owner', 'wrong password!!', '1.1.1.1', now=T0)
    accounts.sign_in('owner', 'wrong password!!', '1.1.1.1', now=T0 + 1)
    accounts.sign_in('owner', 'wrong password!!', '2.2.2.2', now=T0 + 2)
    seen = accounts.failures('Owner', '2.2.2.2', now=T0 + 3)
    assert seen['account'] == 'owner' and seen['account_failures'] == 3
    assert seen['account_addresses'] == ['1.1.1.1', '2.2.2.2']
    assert (seen['address_failures'], seen['blocked_minutes']) == (1, 0)
    accounts.sign_in('nobody', 'a guess at it!!', '3.3.3.3', now=T0)
    accounts.sign_in(PASSWORD, PASSWORD, '3.3.3.3', now=T0 + 1)    # a password as the name
    accounts.sign_in('owner', 'wrong password!!', '3.3.3.3', now=T0 + 2)
    seen = accounts.failures(PASSWORD, '3.3.3.3', now=T0 + 3)
    assert seen['account'] is None and seen['address_failures'] == 3
    assert seen['address_accounts'] == ['owner'] and seen['address_unknown'] == 2
    with sqlite3.connect(accounts.path()) as conn:
        stored = {r[0] for r in conn.execute('select username from attempt')}
    assert stored == {'owner', 'nobody', ''} and PASSWORD not in str(stored)
    # Five from one address block the account there; the alert says for how long.
    for k in range(3):                                      # 2 earlier + 3 = 5 from there
        accounts.sign_in('owner', 'wrong password!!', '1.1.1.1', now=T0 + 10 + k)
    assert accounts.failures('owner', '1.1.1.1', now=T0 + 20)['blocked_minutes'] == 15


def test_the_sign_in_page_queues_an_alert_after_repeated_failures(owner, monkeypatch,
                                                                    tmp_path):
    from contextlib import closing
    monkeypatch.setenv('IGS_AUTH_MODE', 'password')
    monkeypatch.setenv('IGS_OPERATIONAL_ALERTS', '1')
    monkeypatch.setenv('IGS_NOTIFICATION_SPOOL', str(tmp_path / 'errors.sqlite3'))
    client = _client()
    for k in range(accounts.ALERT_AFTER + 1):
        reply = client.post('/signin', data={'username': 'owner', 'password': f'not it {k}!!',
                                             'csrf': _form(client)})
        assert reply.status_code == 401
    with closing(sqlite3.connect(tmp_path / 'errors.sqlite3')) as conn:
        queued = [r[0] for r in conn.execute('select message from errors')]
    assert len(queued) == 1                                  # once an hour, not each failure
    assert queued[0].startswith('SIGN-IN FAILURES')
    assert '3 failed sign-ins for the account owner in 15 minutes' in queued[0]
    assert 'testclient' in queued[0] and 'not it' not in queued[0]


def _client(scheme='https'):
    async def dashboard(request):
        return PlainTextResponse('the app')
    app = Starlette(routes=[Route('/', dashboard), *signin.routes()],
                    middleware=[Middleware(signin.SignInFirst)])
    return TestClient(app, base_url=f'{scheme}://testserver', follow_redirects=False)


def _form(client):
    page = client.get('/signin')
    assert page.status_code == 200
    return page.text.split('name="csrf" value="')[1].split('"')[0]


def test_the_sign_in_page_sets_a_secure_session_cookie(owner, monkeypatch, caplog):
    monkeypatch.setenv('IGS_AUTH_MODE', 'password')
    client = _client()
    assert client.get('/').headers['location'] == '/signin'      # nothing before signing in
    page = client.get('/signin')
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    assert page.headers['cache-control'] == 'no-store'
    assert 'autocomplete="current-password"' in page.text
    csrf = _form(client)
    # Without the page's own token (another site posting the form), nothing is checked.
    assert client.post('/signin', data={'username': 'owner', 'password': PASSWORD,
                                        'csrf': 'forged'}).status_code == 400
    assert client.post('/signin', data={'username': 'owner', 'password': PASSWORD,
                                        'csrf': csrf},
                       headers={'origin': 'https://evil.example'}).status_code == 400
    with caplog.at_level(logging.WARNING, 'igs.auth'):
        wrong = client.post('/signin', data={'username': 'owner', 'password': 'not it at all',
                                             'csrf': _form(client)})
        typo = client.post('/signin', data={'username': PASSWORD, 'password': PASSWORD,
                                            'csrf': _form(client)})
    assert wrong.status_code == typo.status_code == 401
    assert 'Wrong username or password.' in wrong.text and 'igs_session' not in wrong.cookies
    assert PASSWORD not in caplog.text and 'an unknown username' in caplog.text
    done = client.post('/signin', data={'username': 'owner', 'password': PASSWORD,
                                        'csrf': _form(client)},
                       headers={'origin': 'null'})
    assert done.status_code == 303 and done.headers['location'] == '/'
    cookie = next(h for h in done.headers.get_list('set-cookie') if h.startswith('igs_session'))
    for flag in ('HttpOnly', 'Secure', 'SameSite=lax', 'Path=/', 'Max-Age=28800'):
        assert flag in cookie
    assert client.get('/').text == 'the app'
    assert client.get('/signin').headers['location'] == '/'      # already signed in
    accounts.sign_out(client.cookies['igs_session'])
    assert client.get('/').headers['location'] == '/signin'
    # Plain HTTP only to this computer itself; anywhere else the cookie is HTTPS-only.
    assert 'Secure' not in _cookie_over('http', '127.0.0.1')
    public = TestClient(_client().app, base_url='http://stocks.example')
    assert 'Secure' in public.get('/signin').headers['set-cookie']   # no sign-in over HTTP


def _cookie_over(scheme, host):
    client = TestClient(_client().app, base_url=f'{scheme}://{host}', follow_redirects=False)
    reply = client.post('/signin', data={'username': 'owner', 'password': PASSWORD,
                                         'csrf': _form(client)})
    return next(h for h in reply.headers.get_list('set-cookie') if h.startswith('igs_session'))


def test_blocked_sign_in_says_when_to_try_again(owner, monkeypatch):
    monkeypatch.setenv('IGS_AUTH_MODE', 'password')
    client = _client()
    for _ in range(accounts.PER_ADDRESS):
        client.post('/signin', data={'username': 'owner', 'password': 'not it at all',
                                     'csrf': _form(client)})
    reply = client.post('/signin', data={'username': 'owner', 'password': PASSWORD,
                                         'csrf': _form(client)})
    assert reply.status_code == 429 and 'Try again in 15 minutes' in reply.text


def test_an_unusable_account_file_refuses_sign_in_and_says_so(owner, monkeypatch):
    monkeypatch.setenv('IGS_AUTH_MODE', 'password')
    client = _client()
    csrf = _form(client)

    def broken(*args):
        raise sqlite3.OperationalError('unable to open database file')
    monkeypatch.setattr(accounts, 'sign_in', broken)
    reply = client.post('/signin', data={'username': 'owner', 'password': PASSWORD,
                                         'csrf': csrf})
    assert reply.status_code == 503 and 'working right now' in reply.text
    assert 'igs_session' not in reply.cookies


def test_other_modes_have_no_sign_in_page(monkeypatch):
    monkeypatch.setenv('IGS_AUTH_MODE', 'oidc')
    client = _client()
    assert client.get('/').text == 'the app'          # the OIDC gate is in the app itself
    assert client.get('/signin').headers['location'] == '/'
    monkeypatch.setenv('IGS_AUTH_MODE', 'password')
    page = client.get('/signin')
    assert 'No accounts exist yet' in page.text and 'igs users add' in page.text


def test_igs_ui_serves_the_sign_in_page_with_the_app():
    from streamlit.web.server.app_discovery import discover_asgi_app
    server = Path(__file__).resolve().parents[1] / 'src/igs/ui/server.py'
    found = discover_asgi_app(server)
    assert found.is_asgi_app and found.import_string == 'server:app'


APP = str(Path(__file__).resolve().parents[1] / 'src/igs/ui/app.py')


def test_the_app_shows_nothing_without_a_session(monkeypatch):
    monkeypatch.setenv('IGS_AUTH_MODE', 'password')

    def forbidden_db(*a, **kw):
        raise AssertionError('The signed-out page opened the database')
    monkeypatch.setattr('igs.db.connect', forbidden_db)
    at = AppTest.from_file(APP).run()
    assert not at.exception
    assert any('Sign in to continue' in s.value for s in at.subheader)
    assert not at.dataframe and not at.sidebar.radio


def test_roles_come_from_the_session_and_signing_out_ends_it(owner, monkeypatch):
    from igs.ui import auth
    monkeypatch.setenv('IGS_AUTH_MODE', 'password')
    accounts.add('reader', PASSWORD, 'viewer')
    tokens = {name: accounts.sign_in(name, PASSWORD, '127.0.0.1')[0]
              for name in ('owner', 'reader')}

    def script():
        import streamlit as st

        from igs.ui.auth import admin_action, gate
        gate()

        @admin_action
        def mutate():
            st.success('WRITE EXECUTED')
        mutate()
    monkeypatch.setattr(auth, 'session_token', lambda: tokens['reader'])
    at = AppTest.from_function(script).run()
    assert not at.exception and not at.success and at.error
    assert 'Signed in as reader · viewer' in at.sidebar.caption[0].value
    monkeypatch.setattr(auth, 'session_token', lambda: tokens['owner'])
    at = AppTest.from_function(script).run()
    assert not at.exception and at.success[0].value == 'WRITE EXECUTED'
    at.sidebar.button[0].click().run()
    assert not at.exception and not at.success
    assert any('Sign in to continue' in s.value for s in at.subheader)
    assert accounts.session(tokens['owner'], 8, 30) is None


def test_igs_users_asks_for_passwords_and_never_takes_them_as_arguments(monkeypatch, capsys):
    from igs import cli
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr('getpass.getpass', lambda prompt: next(answers))
    assert cli.main(['users', 'add', 'Owner', '--role', 'admin']) == 0
    assert 'Added owner (admin)' in capsys.readouterr().out
    answers = iter(['a different passphrase', 'not the same one at all'])
    assert cli.main(['users', 'passwd', 'owner']) == 1
    assert 'differ' in capsys.readouterr().err
    answers = iter(['short'])
    assert cli.main(['users', 'passwd', 'owner']) == 1
    assert 'at least 12' in capsys.readouterr().err
    assert cli.main(['users', 'list']) == 0
    assert 'owner                    admin   last sign-in never' in capsys.readouterr().out
    assert cli.main(['users', 'remove', 'owner']) == 1
    assert 'only administrator' in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(['users', 'add', 'x', '--role', 'admin', '--password', PASSWORD])
