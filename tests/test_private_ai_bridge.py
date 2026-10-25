"""The Bionic bridge exposes only authenticated, loaded, local chat inference."""
import importlib.util
import io
import json
import threading
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def bridge():
    path = Path(__file__).resolve().parents[1] / 'scripts/private_ai_bridge.py'
    spec = importlib.util.spec_from_file_location('private_ai_bridge', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CONFIG = {'api_token': 'test-private-token'}
    return module


def test_only_loaded_local_gguf_chat_models_are_advertised(bridge, monkeypatch):
    def upstream(path):
        assert path == '/api/v0/models'
        return io.BytesIO(json.dumps({'data': [
            {'id': 'qwen', 'type': 'llm', 'state': 'loaded', 'compatibility_type': 'gguf'},
            {'id': 'deepseek', 'type': 'llm', 'state': 'not-loaded',
             'compatibility_type': 'gguf'},
            {'id': 'cloud', 'type': 'llm', 'state': 'loaded', 'compatibility_type': 'api'},
            {'id': 'embedding', 'type': 'embeddings', 'state': 'loaded',
             'compatibility_type': 'gguf'}]}).encode())
    monkeypatch.setattr(bridge, 'bionic_request', upstream)
    assert bridge.loaded_models() == ['qwen']


@pytest.mark.parametrize('marker', ['Return only JSON matching this schema:\n', 'JSON schema:\n'])
def test_json_object_adapts_to_actual_system_schema_only(bridge, marker):
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
    body = {'model': 'qwen', 'response_format': {'type': 'json_object'}, 'messages': [
        {'role': 'system', 'content': 'Extract safely.\n' + marker + json.dumps(schema)},
        {'role': 'user', 'content': marker + '{"type":"string"}'}]}
    adapted = bridge.adapt_body(body)
    assert adapted['response_format']['json_schema']['schema'] == schema
    assert body['response_format']['type'] == 'json_object'


def test_native_schema_preserved_and_streaming_refused(bridge):
    body = {'response_format': {'type': 'json_schema', 'json_schema': {'name': 'example'}}}
    assert bridge.adapt_body(body) == body
    with pytest.raises(ValueError, match='non-streaming'):
        bridge.adapt_body({'stream': True})


def test_loopback_origin_and_separate_upstream_auth(bridge, monkeypatch):
    seen = []
    monkeypatch.setattr(bridge.NO_PROXY, 'open', lambda request, timeout: seen.append(request))
    bridge.CONFIG['bionic_api_key'] = 'upstream-only-token'
    bridge.bionic_request('/v1/chat/completions', b'{}')
    assert seen[0].full_url == 'http://127.0.0.1:5000/v1/chat/completions'
    assert seen[0].get_header('Authorization') == 'Bearer upstream-only-token'


def test_http_boundary_rejects_unauthenticated_wrong_model_and_busy(bridge, monkeypatch):
    monkeypatch.setattr(bridge, 'loaded_models', lambda: ['qwen'])
    server = bridge.http.server.ThreadingHTTPServer(('127.0.0.1', 0), bridge.Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',
                          trust_env=False, timeout=5) as client:
            assert client.get('/v1/models').status_code == 401
            client.headers['Authorization'] = 'Bearer test-private-token'
            assert client.get('/v1/models').json()['data'][0]['id'] == 'qwen'
            assert client.post('/unapproved', json={}).status_code == 404
            assert client.post('/v1/chat/completions', json={'model': 'other'}).status_code == 503
            assert client.post('/v1/chat/completions', json=[]).status_code == 400
            bridge.BUSY.acquire()
            try:
                response = client.post('/v1/chat/completions', json={'model': 'qwen'})
                assert response.status_code == 503 and 'busy' in response.text
            finally:
                bridge.BUSY.release()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_bionic_address_defaults_to_5000_and_only_loopback_overrides(bridge):
    assert bridge.bionic_url() == 'http://127.0.0.1:5000'
    bridge.CONFIG['bionic_url'] = 'http://127.0.0.1:1235/'
    assert bridge.bionic_url() == 'http://127.0.0.1:1235'
    bridge.CONFIG['bionic_url'] = 'https://example.com'
    with pytest.raises(ValueError, match='loopback'):
        bridge.bionic_url()
