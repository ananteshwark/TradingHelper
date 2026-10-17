"""Refresh provider model catalogs without paid inference or changing saved routes."""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx

from igs.assistant.errors import AssistantError, AssistantUnavailable
from igs.assistant.providers import PROVIDERS, credentials, request
from igs.config import ModelRoute, settings_dir
from igs.timeutil import utc_now

REFRESH_HOURS = 6
TASK_LABELS = {'ask': 'Ask / research questions', 'brief': 'Company briefs',
    'call': 'AI buy / hold / sell calls', 'verdicts': 'Broker-call verdicts',
    'brokers': 'Extract broker calls', 'news_tone': 'Stock-news sentiment',
    'geopolitical': 'Geopolitical impact', 'announcements': 'Announcement notes',
    'forward': 'Public filing guidance / order book',
    'momentum': 'Momentum paper portfolio review'}


def path():
    return settings_dir() / 'model_catalog.json'


def valid_route(provider, model):
    try:
        return ModelRoute(provider=provider, model=model)
    except (TypeError, ValueError):
        return None


def read() -> dict:
    try:
        data = json.loads(path().read_text())
        if not isinstance(data, dict):
            return {}
        return {provider: {**entry, 'models': [model for model in entry.get('models', [])
                if isinstance(model, dict) and valid_route(provider, model.get('id'))]}
                for provider, entry in data.items()
                if provider in PROVIDERS and isinstance(entry, dict)}
    except (OSError, ValueError):
        return {}


def discover(provider, client=None) -> list[dict]:
    rows, cursor, seen = [], None, set()
    for _ in range(100):
        params = {}
        if provider == 'anthropic':
            params = {'limit': 100}
            if cursor:
                params['after_id'] = cursor
        elif provider == 'gemini':
            params = {'pageSize': 1000}
            if cursor:
                params['pageToken'] = cursor
        data = request(provider, 'GET', '/models', params=params, client=client)
        items = data.get('models' if provider=='gemini' else 'data')
        if not isinstance(items, list):
            raise AssistantError(f'{provider}: invalid model catalog')
        for item in items:
            if not isinstance(item, dict):
                continue
            model = item.get('id') or item.get('name', '').removeprefix('models/')
            if not valid_route(provider, model):
                continue
            if provider == 'gemini' and 'generateContent' not in item.get(
                    'supportedGenerationMethods', []):
                continue
            if provider == 'openrouter' and 'text' not in item.get('architecture', {}).get(
                    'output_modalities', ['text']):
                continue
            if provider == 'openai' and any(word in model for word in (
                    'embedding', 'whisper', 'tts', 'transcribe', 'dall-e', 'image',
                    'sora', 'realtime', 'audio', 'moderation')):
                continue
            rows.append({'id': model, 'name': item.get('display_name') or
                         item.get('displayName') or item.get('name') or model,
                         'capabilities': item.get('capabilities'),
                         'supported_parameters': item.get('supported_parameters'),
                         'pricing': item.get('pricing')})
        cursor = (data.get('last_id') if data.get('has_more') else None) \
            if provider=='anthropic' else data.get('nextPageToken') if provider=='gemini' else None
        if not cursor:
            return list({r['id']: r for r in rows}.values())
        if cursor in seen:
            raise AssistantError(f'{provider}: repeated model pagination cursor')
        seen.add(cursor)
    raise AssistantError(f'{provider}: model catalog pagination limit exceeded')


@contextmanager
def _lock(lock_path):
    with lock_path.open('a+b') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def refresh(*, force=False) -> dict:
    # One writer per host, shared by the CLI timer and dashboard. Use the existing
    # cross-platform advisory file lock.
    settings_dir().mkdir(parents=True, exist_ok=True)
    with _lock(settings_dir() / 'model_catalog.lock') as acquired:
        if not acquired:
            return read()
        data = read()
        now = utc_now()
        wanted = []
        for provider in PROVIDERS:
            try:
                credentials(provider)
            except AssistantUnavailable:
                continue
            entry = data.get(provider, {})
            try:
                checked = dt.datetime.fromisoformat(entry['checked_at'])
                fresh = now-checked < dt.timedelta(hours=REFRESH_HOURS)
            except (KeyError, TypeError, ValueError):
                fresh = False
            if force or not fresh:
                wanted.append(provider)

        def fetch(provider):
            entry = dict(data.get(provider, {}))
            entry['checked_at'] = now.isoformat()
            try:
                with httpx.Client(timeout=20, follow_redirects=False) as client:
                    entry['models'] = discover(provider, client)
                entry.update(updated_at=now.isoformat(), error=None)
            except (AssistantError, AssistantUnavailable) as exc:
                # A failed refresh retains the previous successful catalog.
                entry['error'] = str(exc)
            return provider, entry

        with ThreadPoolExecutor(max_workers=5) as pool:
            for provider, entry in pool.map(fetch, wanted):
                data[provider] = entry
        if wanted:
            fd, temporary = tempfile.mkstemp(dir=settings_dir(), prefix='.catalog-')
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump(data, output)
                os.replace(temporary, path())
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return data
