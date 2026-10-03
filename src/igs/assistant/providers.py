"""Provider transports. Credentials only go to fixed official API origins."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import httpx

from igs.assistant.errors import AssistantError, AssistantUnavailable

PROVIDERS = {
    'anthropic': ('Anthropic', 'ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1'),
    'openai': ('OpenAI', 'OPENAI_API_KEY', 'https://api.openai.com/v1'),
    'gemini': ('Google Gemini', 'GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta'),
    'deepseek': ('DeepSeek', 'DEEPSEEK_API_KEY', 'https://api.deepseek.com'),
    'openrouter': ('OpenRouter', 'OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1'),
}


def credentials(provider: str) -> dict:
    key = os.environ.get(PROVIDERS[provider][1])
    if provider == 'gemini':
        key = key or os.environ.get('GOOGLE_API_KEY')
    if not key:
        raise AssistantUnavailable(f'{PROVIDERS[provider][0]} API key is not configured')
    if provider == 'anthropic':
        return {'x-api-key': key, 'anthropic-version': '2023-06-01'}
    if provider == 'gemini':
        return {'x-goog-api-key': key}
    return {'Authorization': f'Bearer {key}'}


def request(provider: str, method: str, path: str, *, payload=None, params=None,
            client=None) -> dict:
    headers = credentials(provider)
    url = PROVIDERS[provider][2] + path
    own = client is None
    client = client or httpx.Client(timeout=120, follow_redirects=False)
    try:
        response = client.request(method, url, headers=headers, json=payload, params=params)
        if response.status_code in (401, 403):
            raise AssistantUnavailable(
                f'{provider}: API access denied (HTTP {response.status_code})')
        if not response.is_success:
            raise AssistantError(f'{provider}: API returned HTTP {response.status_code}; '
                                 'check model access, capabilities and request limits')
        data = response.json()
        if not isinstance(data, dict) or data.get('error'):
            raise AssistantError(f'{provider}: invalid or failed API response')
        return data
    except (httpx.HTTPError, ValueError) as exc:
        raise AssistantError(
            f'{provider}: API transport or response error ({type(exc).__name__})') from None
    finally:
        if own:
            client.close()


def block(value) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, 'model_dump'):
        return value.model_dump(exclude_none=True)
    return vars(value)


def normalized(provider, model, data, content, stop, input_tokens, output_tokens):
    actual_model = data.get('model') or data.get('modelVersion') or model
    return SimpleNamespace(
        provider=provider, model=f'{provider}/{actual_model}', raw_model=actual_model,
        content=[SimpleNamespace(**b) for b in content], stop_reason=stop,
        _request_id=data.get('id') or data.get('responseId'),
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0))


def _responses_messages(messages):
    out = []
    for msg in messages:
        content = msg['content']
        if isinstance(content, str):
            out.append(msg)
            continue
        blocks = [block(b) for b in content]
        raw = next((b['_response_items'] for b in blocks if '_response_items' in b), None)
        if raw is not None:
            out.extend(raw)
            continue
        for b in blocks:
            if b['type'] == 'text':
                out.append({'role': msg['role'], 'content': b['text']})
            elif b['type'] == 'tool_result':
                out.append({'type': 'function_call_output', 'call_id': b['tool_use_id'],
                            'output': b['content']})
    return out


def openai_create(model, system, messages, tools, schema, max_tokens, client=None):
    body: dict[str, Any] = dict(model=model, instructions=system,
        input=_responses_messages(messages), max_output_tokens=max_tokens, store=False,
        include=['reasoning.encrypted_content'])
    if tools:
        body['tools'] = [dict(type='function', name=t['name'],
            description=t['description'], parameters=t['input_schema'], strict=False)
            for t in tools]
    if schema:
        body['text'] = {'format': {'type': 'json_schema', 'name': 'result',
                                   'schema': schema, 'strict': False}}
    data = request('openai', 'POST', '/responses', payload=body, client=client)
    content = []
    stop = 'end_turn'
    for item in data.get('output', []):
        if item['type'] == 'function_call':
            content.append(dict(type='tool_use', id=item['call_id'], name=item['name'],
                                input=json.loads(item['arguments'])))
            stop = 'tool_use'
        elif item['type'] == 'message':
            for part in item.get('content', []):
                if part['type'] == 'output_text':
                    content.append(dict(type='text', text=part['text']))
                elif part['type'] == 'refusal':
                    stop = 'refusal'
    if data.get('status') == 'incomplete':
        reason = (data.get('incomplete_details') or {}).get('reason')
        stop = 'max_tokens' if reason == 'max_output_tokens' else 'refusal'
    if data.get('status') in ('failed', 'cancelled'):
        stop = 'refusal'
    if not content:
        content.append(dict(type='text', text=''))
    # Keep reasoning/function-call IDs for the next tool round; never display them.
    content[0]['_response_items'] = data.get('output', [])
    usage = data.get('usage') or {}
    return normalized('openai', model, data, content, stop,
                      usage.get('input_tokens', 0), usage.get('output_tokens', 0))


def _chat_messages(system, messages):
    out = [dict(role='system', content=system)]
    for msg in messages:
        if isinstance(msg['content'], str):
            out.append(msg)
            continue
        blocks = [block(b) for b in msg['content']]
        if msg['role'] == 'assistant':
            raw = next((b['_chat_message'] for b in blocks if '_chat_message' in b), None)
            if raw is not None:
                out.append(raw)
                continue
        for b in blocks:
            if b['type'] == 'tool_result':
                out.append(dict(role='tool', tool_call_id=b['tool_use_id'], content=b['content']))
            elif b['type'] == 'text':
                out.append(dict(role=msg['role'], content=b['text']))
    return out


def chat_create(provider, model, system, messages, tools, schema, max_tokens, client=None):
    if schema:
        system += '\nReturn only JSON matching this schema:\n' + json.dumps(schema)
    body: dict[str, Any] = dict(model=model, messages=_chat_messages(system, messages),
                               max_tokens=max_tokens)
    if schema:
        body['response_format'] = {'type': 'json_object'}
    if tools:
        body['tools'] = [dict(type='function', function=dict(name=t['name'],
            description=t['description'], parameters=t['input_schema'])) for t in tools]
    data = request(provider, 'POST', '/chat/completions', payload=body, client=client)
    if not data.get('choices'):
        raise AssistantError(f'{provider}: no completion returned')
    choice = data['choices'][0]
    msg = choice['message']
    content = [dict(type='text', text=msg.get('content') or '', _chat_message=msg)]
    for call in msg.get('tool_calls') or []:
        content.append(dict(type='tool_use', id=call['id'], name=call['function']['name'],
                            input=json.loads(call['function']['arguments'])))
    reason = choice.get('finish_reason')
    stop = {'tool_calls': 'tool_use', 'length': 'max_tokens', 'stop': 'end_turn'}.get(
        reason, 'refusal')
    usage = data.get('usage') or {}
    return normalized(provider, model, data, content, stop,
                      usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0))


def gemini_create(model, system, messages, tools, schema, max_tokens, client=None):
    contents = []
    names = {}
    for msg in messages:
        parts = []
        values = ([dict(type='text', text=msg['content'])] if isinstance(msg['content'], str)
                  else [block(b) for b in msg['content']])
        for b in values:
            if b['type'] == 'tool_use':
                names[b['id']] = (b['name'], b.get('_provider_call_id'))
        raw = next((b['_gemini_parts'] for b in values if '_gemini_parts' in b), None)
        if raw is not None:
            parts = raw
        else:
            for b in values:
                if b['type'] == 'text':
                    parts.append({'text': b['text']})
                elif b['type'] == 'tool_result':
                    name, provider_id = names[b['tool_use_id']]
                    result = {'name': name, 'response': {'result': b['content']}}
                    if provider_id:
                        result['id'] = provider_id
                    parts.append({'functionResponse': result})
        contents.append({'role': 'model' if msg['role']=='assistant' else 'user', 'parts': parts})
    generation: dict[str, Any] = {'maxOutputTokens': max_tokens}
    if schema:
        generation.update(responseMimeType='application/json', responseJsonSchema=schema)
    body = dict(systemInstruction={'parts': [{'text': system}]}, contents=contents,
                generationConfig=generation)
    if tools:
        body['tools'] = [{'functionDeclarations': [dict(name=t['name'],
            description=t['description'], parametersJsonSchema=t['input_schema']) for t in tools]}]
    data = request('gemini', 'POST', '/models/'+quote(model.removeprefix('models/'), safe='')+
                   ':generateContent', payload=body, client=client)
    candidate = (data.get('candidates') or [{}])[0]
    raw = candidate.get('content', {}).get('parts', [])
    content = []
    for i, p in enumerate(raw):
        if 'text' in p and not p.get('thought'):
            content.append(dict(type='text', text=p['text']))
        if 'functionCall' in p:
            call = p['functionCall']
            content.append(dict(type='tool_use', id=call.get('id') or f'call_{i}',
                                name=call['name'], input=call.get('args') or {},
                                _provider_call_id=call.get('id')))
    if not content:
        content.append(dict(type='text', text=''))
    content[0]['_gemini_parts'] = raw  # preserves required thought signatures
    finish = candidate.get('finishReason')
    stop = 'end_turn' if finish=='STOP' else 'max_tokens' if finish=='MAX_TOKENS' else 'refusal'
    if stop == 'end_turn' and any(c['type']=='tool_use' for c in content):
        stop = 'tool_use'
    usage = data.get('usageMetadata') or {}
    return normalized('gemini', model, data, content, stop, usage.get('promptTokenCount', 0),
                      usage.get('candidatesTokenCount', 0)+usage.get('thoughtsTokenCount', 0))


def create(provider, model, *, system, messages, tools, schema, max_tokens, client=None):
    try:
        if provider == 'openai':
            return openai_create(model, system, messages, tools, schema, max_tokens, client)
        if provider == 'gemini':
            return gemini_create(model, system, messages, tools, schema, max_tokens, client)
        return chat_create(provider, model, system, messages, tools, schema, max_tokens, client)
    except (KeyError, TypeError, ValueError) as exc:
        raise AssistantError(f'{provider}: malformed model response ({type(exc).__name__})') \
            from None
