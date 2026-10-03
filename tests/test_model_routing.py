"""Offline provider contracts, catalog refresh, budget accounting and routing."""
import json

import httpx
import pytest

from igs import settings
from igs.assistant import catalog, providers
from igs.assistant.errors import AssistantError, AssistantUnavailable, BudgetExceeded
from igs.assistant.llm import Assistant, echo_content, text_of
from igs.config import AssistantConfig, load_assistant

TOOLS = [{'name': 'lookup', 'description': 'Read a stock', 'input_schema': {
    'type': 'object', 'properties': {'symbol': {'type': 'string'}}, 'required': ['symbol']}}]


def config(provider='openai', task='ask'):
    values = load_assistant().model_dump()
    values.update(enabled=True, daily_budget_usd=10,
                  routes={task: {'provider': provider, 'model': 'new-model'}})
    values['prices_usd_per_mtok'][f'{provider}:new-model'] = {'input': 2, 'output': 5}
    return AssistantConfig.model_validate(values)


def client(monkeypatch, provider, handler):
    monkeypatch.setenv(providers.PROVIDERS[provider][1], 'test-private-key')
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_routes_require_pricing_and_preserve_defaults():
    cfg = config('gemini', 'call')
    assert cfg.route_for('call').provider == 'gemini'
    assert cfg.route_for('brief').model == cfg.model
    values = cfg.model_dump()
    del values['prices_usd_per_mtok']['gemini:new-model']
    with pytest.raises(ValueError, match='no price'):
        AssistantConfig.model_validate(values)
    settings.save_assistant(cfg.model_dump())
    assert load_assistant().route_for('call') == cfg.route_for('call')
    assert load_assistant().route_for('forward').provider == 'anthropic'


@pytest.mark.db
def test_openai_tool_roundtrip_usage_and_global_budget(db_conn, monkeypatch):
    seen = []
    raw = [{'type': 'reasoning', 'id': 'r1', 'encrypted_content': 'opaque'},
           {'type': 'function_call', 'id': 'fc1', 'call_id': 'call1',
            'name': 'lookup', 'arguments': '{"symbol":"ABC"}'}]

    def handler(req):
        seen.append(json.loads(req.content))
        output = raw if len(seen)==1 else [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': 'Stored data answer'}]}]
        return httpx.Response(200, json={'id': 'req1', 'model': 'new-model',
            'status': 'completed', 'output': output,
            'usage': {'input_tokens': 100, 'output_tokens': 20}})
    http = client(monkeypatch, 'openai', handler)
    assistant = Assistant.open(db_conn, config(), http)
    messages = [{'role': 'user', 'content': 'Explain ABC'}]
    first = assistant.create('ask', system='Read only', messages=messages, tools=TOOLS)
    assert first.stop_reason == 'tool_use'
    messages += [{'role': 'assistant', 'content': echo_content(first)}, {'role': 'user',
        'content': [{'type': 'tool_result', 'tool_use_id': 'call1', 'content': '{"rank":1}'}]}]
    last = assistant.create('ask', system='Read only', messages=messages, tools=TOOLS)
    assert text_of(last) == 'Stored data answer'
    assert seen[0]['store'] is False and seen[0]['tools'][0]['name']=='lookup'
    assert seen[1]['input'][1:3] == raw
    assert seen[1]['input'][-1]['type']=='function_call_output'
    rows = db_conn.execute('select provider,model,cost_usd from llm_call').fetchall()
    assert len(rows)==2 and rows[0][:2] == ('openai', 'openai/new-model')
    assert float(rows[0][2]) == pytest.approx(0.0003)
    assistant.cfg = assistant.cfg.model_copy(update={'daily_budget_usd': 0.0005})
    with pytest.raises(BudgetExceeded):
        assistant.create('ask', system='Read only', messages=messages)
    assert len(seen)==2


@pytest.mark.parametrize('provider', ['deepseek', 'openrouter'])
def test_chat_tools_preserve_reasoning_and_results(monkeypatch, provider):
    seen = []
    raw = {'role': 'assistant', 'content': None, 'reasoning_content': 'opaque reasoning',
           'tool_calls': [{'id': 't1', 'type': 'function', 'function': {
               'name': 'lookup', 'arguments': '{"symbol":"ABC"}'}}]}

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={'id': 'r', 'model': 'new-model', 'choices': [{
            'finish_reason': 'tool_calls', 'message': raw}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})
    http = client(monkeypatch, provider, handler)
    messages = [{'role': 'user', 'content': 'ABC'}]
    first = providers.create(provider, 'new-model', system='Read only', messages=messages,
                             tools=TOOLS, schema=None, max_tokens=2048, client=http)
    messages += [{'role': 'assistant', 'content': echo_content(first)}, {'role': 'user',
        'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'result'}]}]
    providers.create(provider, 'new-model', system='Read only', messages=messages,
                     tools=TOOLS, schema=None, max_tokens=2048, client=http)
    assert seen[1]['messages'][-2] == raw
    assert seen[1]['messages'][-1]['tool_call_id']=='t1'


def test_gemini_preserves_thought_signatures_and_counts_thinking(monkeypatch):
    seen = []
    parts = [{'functionCall': {'name': 'lookup', 'id': 't1', 'args': {'symbol': 'ABC'}},
              'thoughtSignature': 'opaque'}]

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={'candidates': [{'content': {'parts': parts},
            'finishReason': 'STOP'}], 'usageMetadata': {'promptTokenCount': 10,
            'candidatesTokenCount': 4, 'thoughtsTokenCount': 6}})
    http = client(monkeypatch, 'gemini', handler)
    messages = [{'role': 'user', 'content': 'ABC'}]
    first = providers.create('gemini', 'new-model', system='Read only', messages=messages,
                             tools=TOOLS, schema=None, max_tokens=2048, client=http)
    assert first.usage.output_tokens==10 and first.stop_reason=='tool_use'
    messages += [{'role': 'assistant', 'content': echo_content(first)}, {'role': 'user',
        'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'result'}]}]
    providers.create('gemini', 'new-model', system='Read only', messages=messages,
                     tools=TOOLS, schema=None, max_tokens=2048, client=http)
    assert seen[1]['contents'][1]['parts']==parts
    assert seen[1]['contents'][2]['parts'][0]['functionResponse']['name']=='lookup'


@pytest.mark.parametrize('provider', ['openai', 'gemini', 'deepseek', 'openrouter'])
def test_structured_payload_and_truncation(monkeypatch, provider):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        if provider=='openai':
            data = {'output': [], 'status': 'incomplete',
                    'incomplete_details': {'reason': 'max_output_tokens'}, 'usage': {}}
        elif provider=='gemini':
            data = {'candidates': [{'finishReason': 'MAX_TOKENS'}]}
        else:
            data = {'choices': [{'message': {'content': '{'}, 'finish_reason': 'length'}]}
        return httpx.Response(200, json=data)
    http = client(monkeypatch, provider, handler)
    schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}}
    m = providers.create(provider, 'new-model', system='Read only',
        messages=[{'role': 'user', 'content': 'answer'}], tools=None, schema=schema,
        max_tokens=2048, client=http)
    assert m.stop_reason=='max_tokens'
    if provider=='openai':
        assert seen[0]['text']['format']['schema']==schema
    elif provider=='gemini':
        assert seen[0]['generationConfig']['responseJsonSchema']==schema
    else:
        assert seen[0]['response_format']['type']=='json_object'
        assert 'schema' in seen[0]['messages'][0]['content']


def test_provider_errors_do_not_expose_body_or_key(monkeypatch):
    http = client(monkeypatch, 'openai', lambda req: httpx.Response(
        401, json={'error': 'test-private-key and private prompt'}))
    with pytest.raises(AssistantUnavailable, match='access denied') as exc:
        providers.request('openai', 'GET', '/models', client=http)
    assert 'test-private-key' not in str(exc.value) and 'private prompt' not in str(exc.value)


def test_catalog_pagination_discovers_new_models_without_changing_routes(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        more = len(seen)==1
        return httpx.Response(200, json={'data': [{'id': 'older' if more else 'new-release'}],
                                        'has_more': more, 'last_id': 'older' if more else None})
    http = client(monkeypatch, 'anthropic', handler)
    rows = catalog.discover('anthropic', http)
    assert [r['id'] for r in rows]==['older', 'new-release']
    assert seen[1].url.params['after_id']=='older'
    assert load_assistant().routes=={}


def test_failed_catalog_refresh_retains_previous_success(monkeypatch):
    for _, variable, _ in providers.PROVIDERS.values():
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-private-key')
    monkeypatch.setattr(catalog, 'discover', lambda *args: [{'id': 'first'}])
    first = catalog.refresh(force=True)
    assert first['openai']['models']==[{'id': 'first'}]

    def unavailable(*args):
        raise AssistantError('openai: unavailable')
    monkeypatch.setattr(catalog, 'discover', unavailable)
    last = catalog.refresh(force=True)
    assert last['openai']['models']==first['openai']['models']
    assert 'unavailable' in last['openai']['error']
    assert 'test-private-key' not in catalog.path().read_text()


@pytest.mark.db
def test_claude_task_override_records_actual_model_and_price(db_conn):
    from types import SimpleNamespace

    cfg = config('anthropic', 'geopolitical')
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(model='new-model', stop_reason='end_turn',
            content=[SimpleNamespace(type='text', text='{}')], usage=SimpleNamespace(
            input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
            cache_creation_input_tokens=0))
    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    assistant = Assistant.open(db_conn, cfg, fake)
    assistant.structured('geopolitical', system='Context', prompt='News', schema={'type':'object'})
    assert seen[0]['model']=='new-model'
    row = db_conn.execute('select provider,model,cost_usd from llm_call').fetchone()
    assert row[:2]==('anthropic','new-model')
    assert float(row[2])==pytest.approx(0.0003)


def test_explicit_claude_price_overrides_legacy_default_rate():
    from types import SimpleNamespace

    cfg = load_assistant()
    assistant = Assistant(None, cfg, None)
    usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    price = type(cfg.prices_usd_per_mtok[cfg.model])(input=3, output=7)
    message = SimpleNamespace(model=cfg.model, usage=usage, _billing_model=cfg.model,
                              _billing_price=price, provider='anthropic')
    assert assistant.cost(message)==pytest.approx(0.00044)


@pytest.mark.parametrize('supported', [True, False])
def test_adaptive_thinking_uses_explicit_provider_capability(monkeypatch, supported):
    from igs.assistant.llm import uses_effort

    monkeypatch.setattr(catalog, 'read', lambda: {'anthropic': {'models': [{
        'id': 'claude-sonnet-5', 'capabilities': {'thinking': {'supported': True,
            'types': {'adaptive': {'supported': supported}}}}}]}})
    assert uses_effort('claude-sonnet-5') is supported
