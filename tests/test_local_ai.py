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


def test_local_only_can_name_an_exact_loaded_model(monkeypatch):
    monkeypatch.setenv('IGS_AI_LOCAL_ONLY', '1')
    monkeypatch.setenv('IGS_AI_LOCAL_MODEL', ' fingpt-mt-llama-3-8b-lora ')
    cfg = load_assistant()
    assert cfg.route_for('call') == ModelRoute(provider='local',
                                               model='fingpt-mt-llama-3-8b-lora')
    # Without local-only the setting does nothing.
    monkeypatch.delenv('IGS_AI_LOCAL_ONLY')
    assert cfg.route_for('call').provider != 'local'


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


def test_each_task_can_have_its_own_local_model(monkeypatch):
    monkeypatch.setenv('IGS_AI_LOCAL_ONLY', '1')
    monkeypatch.setenv('IGS_AI_LOCAL_MODEL', 'qwen-trading')
    monkeypatch.setenv('IGS_AI_LOCAL_TASK_MODELS', 'news_tone=fingpt-x, brokers = fingpt-x')
    cfg = load_assistant()
    assert cfg.route_for('news_tone').model == 'fingpt-x'
    assert cfg.route_for('brokers').model == 'fingpt-x'
    assert cfg.route_for('call').model == 'qwen-trading'
    for bad in ('nonsense=fingpt-x', 'news_tone', 'news_tone=bad model'):
        monkeypatch.setenv('IGS_AI_LOCAL_TASK_MODELS', bad)
        with pytest.raises(ValueError):
            cfg.route_for('call')


def test_a_local_route_needs_no_price_but_a_cloud_one_does():
    from igs.config import AssistantConfig
    base = {'model': 'claude-opus-5', 'prices_usd_per_mtok': {
        'claude-opus-5': {'input': 5, 'output': 25}}}
    cfg = AssistantConfig.model_validate(
        {**base, 'routes': {'call': {'provider': 'local', 'model': 'qwen/qwen3.8-27b'}}})
    assert cfg.price_for(cfg.routes['call']).input == 0
    with pytest.raises(ValueError, match='no price for deepseek:deepseek-chat'):
        AssistantConfig.model_validate(
            {**base, 'routes': {'call': {'provider': 'deepseek', 'model': 'deepseek-chat'}}})


def test_prices_younger_than_the_maximum_age_are_not_downloaded(monkeypatch):
    import datetime as dt

    from igs.assistant import model_policy
    from igs.timeutil import utc_now
    stamp = (utc_now() - dt.timedelta(hours=30)).isoformat()
    monkeypatch.setattr(model_policy, 'read', lambda: {'updated_at': stamp, 'models': {}})

    class Client:
        calls = 0

        def get(self, url):
            Client.calls += 1
            raise httpx.ConnectError('offline')

        def close(self):
            pass
    model_policy.refresh(client=Client())                   # weekly default: 30 h is fresh
    assert Client.calls == 0
    model_policy.refresh(client=Client(), max_age_days=1)   # the timer's daily refresh
    assert Client.calls == 2
