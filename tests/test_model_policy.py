import datetime as dt
import json

import httpx
import pytest

from igs.assistant import model_policy as policy
from igs.config import ModelRoute, load_assistant, settings_dir
from igs.timeutil import utc_now


def fixtures(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    now = utc_now().isoformat()
    data = {'anthropic': {'updated_at': now, 'models': [
        {'id': 'claude-haiku-4-5'}, {'id': 'claude-sonnet-5'}, {'id': 'unreviewed-cheap'}]}}
    models = {}
    for model, inp in [('claude-haiku-4-5', 1), ('claude-sonnet-5', 2), ('unreviewed-cheap', 0)]:
        models[f'anthropic:{model}'] = {'updated_at': now,
            'price': {'input': inp, 'output': inp*5}, 'metadata': {
                'supports_function_calling': True, 'supports_response_schema': True,
                'max_input_tokens': 200000, 'max_output_tokens': 64000}}
    settings_dir().mkdir(parents=True, exist_ok=True)
    (settings_dir() / 'model_prices.json').write_text(json.dumps({'models': models}))
    (settings_dir() / 'model_catalog.json').write_text(json.dumps(data))
    return data, {'models': models}


def test_task_suitability_cost_and_admin_override(monkeypatch):
    fixtures(monkeypatch)
    cfg = load_assistant()
    assert cfg.route_for('news_tone').model == 'claude-haiku-4-5'
    assert cfg.route_for('call').model == 'claude-sonnet-5'
    cfg.routes['news_tone'] = ModelRoute(provider='anthropic', model=cfg.model)
    assert cfg.route_for('news_tone').model == cfg.model
    cfg = cfg.model_copy(update={"automatic_routing": False})
    assert cfg.route_for('call').model == cfg.model


def test_missing_key_stale_catalog_and_incompatible_models(monkeypatch):
    data, prices = fixtures(monkeypatch)
    cfg = load_assistant()
    monkeypatch.delenv('ANTHROPIC_API_KEY')
    assert policy.recommend(cfg, 'call')[0].model == cfg.model
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    data['anthropic']['updated_at'] = (utc_now()-dt.timedelta(days=10)).isoformat()
    assert policy.recommend(cfg, 'call', data=data)[0].model == cfg.model
    data['anthropic']['updated_at'] = utc_now().isoformat()
    prices['models']['anthropic:claude-sonnet-5']['metadata']['max_output_tokens'] = 1
    assert policy.recommend(cfg, 'call', data=data, prices=prices)[0].model == cfg.model


def test_weekly_refresh_units_retention_and_private_requests(monkeypatch):
    seen = []
    def handler(req):
        seen.append(req)
        assert 'authorization' not in req.headers
        if str(req.url) == policy.REFERENCE_URL:
            return httpx.Response(200, json={'claude-sonnet-5': {
                'mode': 'chat', 'litellm_provider': 'anthropic',
                'input_cost_per_token': .000002, 'output_cost_per_token': .00001}})
        return httpx.Response(200, json={'data': [{'id': 'deepseek/model',
            'pricing': {'prompt': '.0000001', 'completion': '.0000002'}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = policy.refresh(client=client)
    assert data['models']['anthropic:claude-sonnet-5']['price']['input'] == 2
    assert data['models']['openrouter:deepseek/model']['price']['output'] == pytest.approx(.2)
    policy.refresh(client=client)
    assert len(seen) == 2  # weekly TTL, not every six-hour model discovery
    failed = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    retained = policy.refresh(force=True, client=failed)
    assert retained['errors'] and retained['models'] == data['models']


def test_fresh_rates_take_precedence_and_admin_can_disable(monkeypatch):
    fixtures(monkeypatch)
    cfg = load_assistant()
    route = ModelRoute(provider='anthropic', model='claude-sonnet-5')
    assert cfg.price_for(route).input == 2
    cfg = cfg.model_copy(update={"automatic_prices": False})
    assert cfg.price_for(route) == cfg.prices_usd_per_mtok[route.model]


def test_unsupported_discovered_identifier_cannot_crash_recommendation(monkeypatch):
    data, prices = fixtures(monkeypatch)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test')
    bad = '~deepseek/deepseek-v4-flash-latest'
    data['openrouter'] = {'updated_at': utc_now().isoformat(), 'models': [
        {'id': bad, 'supported_parameters': ['tools', 'structured_outputs']},
        {'id': None}, {}]}
    prices['models']['openrouter:' + bad] = {
        'updated_at': utc_now().isoformat(), 'price': {'input': 0, 'output': 0},
        'metadata': {'context_length': 1000000, 'top_provider': {'max_completion_tokens': 64000}}}
    route, _ = policy.recommend(load_assistant(), 'call', data=data, prices=prices)
    assert route.model == 'claude-sonnet-5'
