import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from igs.access import AccessPolicy, api_authorized, role_for
from igs.api.app import app, get_conn

POLICY = AccessPolicy(issuer='https://example.auth0.com/', admin_emails=['owner@example.com'],
                      viewer_emails=['reader@example.com'])


def claims(**over):
    now = time.time()
    return {'is_logged_in': True, 'iss': POLICY.issuer, 'sub': 'auth0|123',
            'email': 'owner@example.com', 'email_verified': True, 'amr': ['pwd', 'mfa'],
            'iat': now-60, 'auth_time': now-60, 'exp': now+3600} | over


@pytest.mark.parametrize('change', [
    {'is_logged_in': False}, {'email': 'intruder@example.com'}, {'email_verified': False},
    {'email_verified': 'true'}, {'iss': 'https://attacker.example/'}, {'sub': ''},
    {'amr': ['pwd']}, {'amr': 'mfa'}, {'exp': 0}, {'iat': float('nan')},
    {'exp': None}, {'iat': time.time()+3600}, {'auth_time': time.time()-9*3600}])
def test_identity_rejected(change):
    assert role_for(claims(**change), POLICY, time.time()) is None


def test_roles_and_provider_scoped_subjects():
    assert role_for(claims(), POLICY, time.time()) == 'admin'
    assert role_for(claims(email='reader@example.com'), POLICY, time.time()) == 'viewer'
    assert role_for(claims(), AccessPolicy(), time.time()) is None
    subject_policy = AccessPolicy(issuer=POLICY.issuer, admin_subjects=['auth0|123'])
    assert role_for(claims(email_verified=False), subject_policy, time.time()) == 'admin'
    assert role_for(claims(iss='wrong'), subject_policy, time.time()) is None


def test_api_authentication_blocks_every_private_route_before_db(monkeypatch):
    def forbidden_db():
        raise AssertionError('Unauthenticated request reached the database')
    app.dependency_overrides[get_conn] = forbidden_db
    try:
        with TestClient(app) as client:
            for path in ['/runs', '/rankings', '/watchlist', '/docs', '/openapi.json']:
                assert client.get(path).status_code == 401
            assert client.post('/watchlist', json={'symbol': 'TEST'}).status_code == 401
            assert client.get('/health').status_code == 200
            assert client.get('/disclaimer', headers={
                'Authorization': 'Bearer test-api-token-' + 'x'*32}).status_code == 200
            assert client.get('/disclaimer', headers={
                'Authorization': 'Bearer wrong'}).status_code == 401
        monkeypatch.setenv('IGS_API_TOKEN', 'short')
        assert not api_authorized('Bearer short')
        monkeypatch.delenv('IGS_API_TOKEN')
        assert not api_authorized('Bearer ')
    finally:
        app.dependency_overrides.clear()


def test_public_login_screen_never_opens_database(monkeypatch):
    monkeypatch.setenv('IGS_AUTH_MODE', 'oidc')
    def forbidden_db(*a, **kw):
        raise AssertionError('Login page opened database')
    monkeypatch.setattr('igs.db.connect', forbidden_db)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'src/igs/ui/app.py')).run()
    assert not at.exception
    assert any('Sign in to your workspace' in s.value for s in at.subheader)
    assert not at.dataframe
    assert not at.sidebar.radio


def test_viewer_and_expired_callback_cannot_write(monkeypatch):
    import streamlit as st

    from igs.ui import auth
    monkeypatch.setenv('IGS_AUTH_MODE', 'oidc')
    monkeypatch.setattr(auth, 'load_policy', lambda: POLICY)
    monkeypatch.setattr(st, 'user', claims(email='reader@example.com'))
    def script():
        import streamlit as st

        from igs.ui.auth import admin_action
        @admin_action
        def mutate():
            st.success('WRITE EXECUTED')
        mutate()
    at = AppTest.from_function(script).run()
    assert not at.exception and not at.success and at.error
    monkeypatch.setattr(st, 'user', claims())
    at = AppTest.from_function(script)
    at.session_state['_last_activity'] = time.time()-3600
    at.run()
    assert not at.exception and not at.success and at.error


def test_local_mode_cannot_be_used_on_public_bind(monkeypatch):
    import streamlit as st

    from igs.ui import auth
    monkeypatch.setenv('IGS_AUTH_MODE', 'local')
    monkeypatch.setattr(st, 'get_option', lambda _: '0.0.0.0')
    assert auth.current_role() is None


def test_private_setup_creates_random_secrets_and_never_overwrites(tmp_path, monkeypatch):
    import importlib.util
    import json
    import stat
    import tomllib

    script = Path(__file__).resolve().parents[1] / 'scripts/configure-public-auth.py'
    spec = importlib.util.spec_from_file_location('setup_auth', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    def inputs():
        answers = iter(['tenant.eu.auth0.com', 'client-id', 'owner@example.com'])
        monkeypatch.setattr('builtins.input', lambda _: next(answers))
    inputs()
    monkeypatch.setattr(module.getpass, 'getpass', lambda _: 'secret-value-for-testing-only')
    module.main()
    path = tmp_path / '.streamlit/secrets.toml'
    secret = tomllib.loads(path.read_text())['auth']
    assert len(secret['cookie_secret']) >= 48
    assert secret['redirect_uri'] == 'https://stocks.ednis.ai/oauth2callback'
    assert secret['client_kwargs']['prompt'] == 'login'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    policy = json.loads((tmp_path / 'data/settings/access.yaml').read_text())
    assert policy['admin_emails'] == ['owner@example.com']
    inputs()
    with pytest.raises(SystemExit, match='already exists'):
        module.main()


@pytest.mark.db
def test_viewer_ui_has_no_write_access(db_conn, monkeypatch):
    import json
    import os

    import db_market
    import streamlit as st

    from igs.config import load_scoring, load_universe
    from igs.pit import gate
    from igs.score.pipeline import score_from_db
    from igs.ui import auth
    db_market.load(db_conn)
    gate_file = Path(os.environ['IGS_SETTINGS_DIR']).parent / 'gate.json'
    gate_file.write_text(json.dumps({'fingerprint': gate.code_fingerprint(),
                                    'passed_at': 't', 'summary': ''}))
    monkeypatch.setenv('IGS_GATE_PATH', str(gate_file))
    sc = load_scoring().model_copy(update={'peer_group': load_scoring().peer_group.model_copy(
        update={'min_peers': 2})})
    score_from_db(db_conn, db_market.AS_OF, None, sc=sc,
                  uc=load_universe().model_copy(update={'min_market_cap_cr': 0.0}))
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    monkeypatch.setenv('IGS_AUTH_MODE', 'oidc')
    monkeypatch.setattr(auth, 'load_policy', lambda: POLICY)
    monkeypatch.setattr(st, 'user', claims(email='reader@example.com'))
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'src/igs/ui/app.py'),
                          default_timeout=60).run()
    assert not at.exception
    assert 'Settings' not in at.sidebar.radio(key='page').options
    assert at.button(key='save_screen').disabled
    at.session_state['page'] = 'Stock'
    at.session_state['stock_sym'] = 'BANK'
    at.run()
    assert not at.exception
    assert at.button(key='watch_btn').disabled
    # A previously authenticated browser is denied on the next run after account removal.
    monkeypatch.setattr(auth, 'load_policy', lambda: AccessPolicy(issuer=POLICY.issuer))
    at.run()
    assert not at.dataframe and not at.sidebar.radio
