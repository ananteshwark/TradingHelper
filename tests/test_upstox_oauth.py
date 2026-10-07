from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from igs.intraday import oauth


def pending():
    return oauth.begin('key', 'private-secret', 'https://stocks.ednis.ai/', now=100)


def returned(attempt, **extra):
    return attempt['redirect_uri'] + '?' + urlencode(
        {'code': 'private-code', 'state': attempt['state'], **extra})


def test_login_and_exchange_use_only_upstox_and_keep_secret_out_of_url():
    attempt = pending()
    url = oauth.login_url(attempt)
    assert url.startswith(oauth.AUTHORIZATION_URL + '?')
    assert 'private-secret' not in url
    assert parse_qs(urlsplit(url).query)['state'] == [attempt['state']]

    def respond(request):
        assert str(request.url) == oauth.TOKEN_URL
        assert request.method == 'POST'
        values = parse_qs(request.content.decode())
        assert values['client_secret'] == ['private-secret']
        assert values['code'] == ['private-code']
        assert values['redirect_uri'] == ['https://stocks.ednis.ai/']
        return httpx.Response(200, json={'access_token': 'saved-token'})

    assert oauth.exchange(attempt, returned(attempt), now=101,
                          transport=httpx.MockTransport(respond)) == 'saved-token'


@pytest.mark.parametrize('redirect', [
    'http://stocks.ednis.ai/', 'https://user:secret@stocks.ednis.ai/',
    'https://stocks.ednis.ai/?code=bad', 'https://stocks.ednis.ai/#fragment', '//evil.test/'])
def test_redirect_requires_https_without_credentials_query_or_fragment(redirect):
    with pytest.raises(oauth.OAuthError):
        oauth.begin('key', 'secret', redirect)


@pytest.mark.parametrize('change', ['state', 'host', 'path', 'duplicate', 'expired', 'denied'])
def test_invalid_callback_stops_before_network(change):
    attempt = pending()
    url, now = returned(attempt), 101
    if change == 'state':
        url = returned(attempt, state='wrong')
    elif change == 'host':
        url = url.replace('stocks.ednis.ai', 'evil.test')
    elif change == 'path':
        url = url.replace('/?', '/unexpected?')
    elif change == 'duplicate':
        url += '&code=other'
    elif change == 'expired':
        now = 701
    elif change == 'denied':
        url = returned(attempt, error='access_denied')

    def forbidden(request):
        pytest.fail('Invalid login must not contact the token endpoint')

    with pytest.raises(oauth.OAuthError):
        oauth.exchange(attempt, url, now=now, transport=httpx.MockTransport(forbidden))


@pytest.mark.parametrize('status,body', [
    (400, {'error': 'private-secret private-code'}), (200, {'access_token': ''}),
    (200, {'access_token': 'not a token'}), (200, []), (302, {})])
def test_errors_never_show_response_or_credentials(status, body):
    attempt = pending()
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.exchange(attempt, returned(attempt), now=101,
                       transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)))
    assert 'private-secret' not in str(exc.value)
    assert 'private-code' not in str(exc.value)


def test_admin_form_consumes_login_and_saves_only_token(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    from igs import envfile
    from igs.ui import auth, upstox_connect

    monkeypatch.setattr(auth, 'require_access', lambda **kwargs: 'admin')
    monkeypatch.setenv('IGS_ENV_FILE', str(tmp_path / '.env'))
    monkeypatch.setenv('UPSTOX_TRADING_TOKEN', 'previous')
    app = AppTest.from_string('from igs.ui.upstox_connect import render\nrender()').run()
    app.text_input[0].set_value('key')
    app.text_input[1].set_value('private-secret')
    app.text_input[2].set_value('https://stocks.ednis.ai/')
    app.button[0].click().run()
    assert not app.exception
    attempt = app.session_state[upstox_connect.PENDING]
    attempts = []

    def exchange_once(pending, value):
        attempts.append(pending)
        assert value == returned(attempt)
        return 'test-token'

    monkeypatch.setattr(oauth, 'exchange', exchange_once)
    app.text_input[0].set_value(returned(attempt))
    app.button[0].click().run()
    assert not app.exception
    assert upstox_connect.PENDING not in app.session_state
    saved = envfile.parse((tmp_path / '.env').read_text())
    assert saved == {'UPSTOX_TRADING_TOKEN': 'test-token'}
    assert len(attempts) == 1
    app.run()
    assert len(attempts) == 1


def test_browser_root_slash_normalization_keeps_registered_exchange_uri():
    attempt = oauth.begin('key', 'secret', 'https://stocks.ednis.ai', now=100)

    def respond(request):
        assert parse_qs(request.content.decode())['redirect_uri'] == ['https://stocks.ednis.ai']
        return httpx.Response(200, json={'access_token': 'token'})

    callback = 'https://stocks.ednis.ai/?' + urlencode(
        {'code': 'code', 'state': attempt['state']})
    assert oauth.exchange(attempt, callback, now=101,
                          transport=httpx.MockTransport(respond)) == 'token'
