"""Private inference must stay on loopback and refuse the wrong model or cloud fallback."""
import json

import httpx
import pytest

from igs.assistant import providers
from igs.assistant.errors import AssistantError, AssistantUnavailable
from igs.config import ModelRoute, load_assistant


def test_local_only_overrides_every_route_and_has_no_token_charge(monkeypatch):
    monkeypatch.setenv('IGS_AI_LOCAL_ONLY', '1')
    cfg = load_assistant()
    for task in type(cfg.features).model_fields:
        route = cfg.route_for(task)
        assert route == ModelRoute(provider='local', model='strata-auto')
        assert cfg.price_for(route).input == cfg.price_for(route).output == 0


def test_local_deepseek_resolves_live_model_with_separate_credential(monkeypatch):
    monkeypatch.setenv('STRATA_API_KEY', 'private-strata-key')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'cloud-key-must-not-be-used')
    seen = []

    def handle(req):
        assert req.url.host == '127.0.0.1' and req.url.port == 18080
        assert req.headers['authorization'] == 'Bearer private-strata-key'
        seen.append(req)
        if req.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'deepseek-v4.1-local'}]})
        assert json.loads(req.content)['model'] == 'deepseek-v4.1-local'
        return httpx.Response(200, json={'model': 'deepseek-v4.1-local', 'choices': [{
            'message': {'content': 'local result'}, 'finish_reason': 'stop'}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = providers.create('local', 'strata-auto', system='test',
            messages=[{'role': 'user', 'content': 'test'}], tools=None, schema=None,
            max_tokens=32, client=client)
    assert result.provider == 'local' and result.content[0].text == 'local result'
    assert [r.method for r in seen] == ['GET', 'POST']


@pytest.mark.parametrize('ids, selected', [(['qwen-local'], 'qwen-local'),
    (['qwen-local', 'deepseek-local'], 'deepseek-local')])
def test_temporary_qwen_and_automatic_deepseek_preference(monkeypatch, ids, selected):
    monkeypatch.setenv('STRATA_API_KEY', 'private-strata-key')

    def handle(req):
        if req.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': m} for m in ids]})
        assert json.loads(req.content)['model'] == selected
        return httpx.Response(200, json={'ok': True})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert providers.request('local', 'POST', '/chat/completions',
            payload={'model': 'strata-auto'}, client=client)['ok']


@pytest.mark.parametrize('ids', [[], ['unknown-local'], ['deepseek-a', 'deepseek-b']])
def test_wrong_missing_or_ambiguous_model_never_receives_analysis(monkeypatch, ids):
    monkeypatch.setenv('STRATA_API_KEY', 'private-strata-key')

    def handle(req):
        assert req.method == 'GET'
        return httpx.Response(200, json={'data': [{'id': m} for m in ids]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AssistantUnavailable, match='not ready'):
            providers.request('local', 'POST', '/chat/completions',
                              payload={'model': 'strata-auto'}, client=client)


def test_cloud_blocked_before_credentials_or_network(monkeypatch):
    from igs.assistant.llm import make_client
    monkeypatch.setenv('IGS_AI_LOCAL_ONLY', '1')
    with pytest.raises(AssistantUnavailable, match='cloud provider'):
        providers.request('deepseek', 'POST', '/chat/completions')
    with pytest.raises(AssistantUnavailable, match='cloud provider'):
        make_client()


def test_local_outage_does_not_fall_back(monkeypatch):
    monkeypatch.setenv('STRATA_API_KEY', 'private-strata-key')

    def handle(req):
        raise httpx.ConnectError('offline', request=req)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AssistantError, match='transport'):
            providers.request('local', 'POST', '/chat/completions',
                              payload={'model': 'strata-auto'}, client=client)


@pytest.mark.db
def test_assistant_logs_actual_local_model_and_zero_provider_cost(db_conn, monkeypatch):
    from igs.assistant.llm import Assistant
    monkeypatch.setenv('IGS_AI_LOCAL_ONLY', '1')
    monkeypatch.setenv('STRATA_API_KEY', 'private-strata-key')

    def handle(req):
        if req.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'qwen-local'}]})
        return httpx.Response(200, json={'model': 'qwen-local', 'choices': [{
            'message': {'content': 'Local research result'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})

    cfg = load_assistant().model_copy(update={'enabled': True})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = Assistant.open(db_conn, cfg, client).create('ask', system='Research',
            messages=[{'role': 'user', 'content': 'Test'}])
    assert result.provider == 'local'
    row = db_conn.execute('select provider,model,cost_usd from llm_call').fetchone()
    assert row[:2] == ('local', 'local/qwen-local')
    assert float(row[2]) == 0
