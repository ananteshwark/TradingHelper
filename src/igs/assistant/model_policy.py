"""Cost-conscious routing and weekly reference rates; never infer quality from price."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import tempfile
from functools import lru_cache

import httpx

from igs.assistant import catalog
from igs.assistant.errors import AssistantUnavailable
from igs.assistant.providers import credentials
from igs.config import ModelRoute, TokenPrice, settings_dir
from igs.timeutil import utc_now

REFERENCE_URL = 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json'
ROUTER_URL = 'https://openrouter.ai/api/v1/models'
# Explicit family policy, not a claim of measured task accuracy. Unknown families
# remain available for manual selection until compatibility has been reviewed.
COMPLEX = {'ask', 'brief', 'call', 'verdicts', 'geopolitical', 'forward'}


@lru_cache(maxsize=4)
def _read_file(path, stamp):
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else {}


def read():
    try:
        path = settings_dir() / 'model_prices.json'
        return _read_file(path, path.stat().st_mtime_ns)
    except (OSError, ValueError):
        return {}


def fresh(entry, days=8):
    try:
        age = utc_now() - dt.datetime.fromisoformat(entry['updated_at'])
        return dt.timedelta(0) <= age < dt.timedelta(days=days)
    except (KeyError, TypeError, ValueError):
        return False


def rate(route):
    row = read().get('models', {}).get(f'{route.provider}:{route.model}', {})
    if fresh(row):
        try:
            return TokenPrice.model_validate(row['price'])
        except (KeyError, ValueError):
            pass
    return None


def _price(inp, out):
    values = [float(inp) * 1_000_000, float(out) * 1_000_000]
    if any(not math.isfinite(v) or v < 0 or v > 10000 for v in values):
        raise ValueError('invalid token rate')
    return {'input': values[0], 'output': values[1]}


def refresh(*, force=False, client=None):
    """No keys or task content sent to public pricing sources. Keep good data on failure."""
    settings_dir().mkdir(parents=True, exist_ok=True)
    with catalog._lock(settings_dir() / 'model_prices.lock') as acquired:
        data = dict(read())
        if not acquired or (not force and fresh(data, days=7)):
            return data
        now = utc_now().isoformat()
        models = dict(data.get('models', {}))
        errors = []
        owned = client is None
        client = client or httpx.Client(timeout=30, follow_redirects=False)
        try:
            for source in (REFERENCE_URL, ROUTER_URL):
                try:
                    response = client.get(source)
                    response.raise_for_status()
                    payload = response.json()
                    count = 0
                    if source == REFERENCE_URL:
                        if not isinstance(payload, dict):
                            raise ValueError('invalid reference catalog')
                        rows = []
                        for name, item in payload.items():
                            if not isinstance(item, dict) or item.get('mode') != 'chat':
                                continue
                            provider = item.get('litellm_provider')
                            if provider not in {'anthropic', 'openai', 'gemini', 'deepseek'}:
                                continue
                            model = name.removeprefix(provider + '/')
                            # Reject regional, batch, fine-tuned and other routed aliases.
                            if '/' in model or model.startswith('ft:'):
                                continue
                            rows.append((f'{provider}:{model}', item,
                                         item.get('input_cost_per_token'),
                                         item.get('output_cost_per_token')))
                    else:
                        rows = [(f'openrouter:{m["id"]}', m,
                                 m.get('pricing', {}).get('prompt'),
                                 m.get('pricing', {}).get('completion'))
                                for m in payload['data']]
                    for key, item, inp, out in rows:
                        try:
                            price = _price(inp, out)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        models[key] = {'price': price, 'updated_at': now,
                                       'source': source, 'metadata': item}
                        count += 1
                    if not count:
                        raise ValueError('empty pricing source')
                except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                    errors.append(f'Could not refresh {source}; previous rates retained')
        finally:
            if owned:
                client.close()
        data.update(models=models, checked_at=now, errors=errors)
        if not errors:
            data['updated_at'] = now
        fd, name = tempfile.mkstemp(dir=settings_dir(), prefix='.prices-')
        try:
            with os.fdopen(fd, 'w') as fh:
                json.dump(data, fh)
            os.replace(name, settings_dir() / 'model_prices.json')
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return data


def tier(model):
    """Reviewed general-purpose families; previews/specialist variants require admin choice."""
    model = model.split('/')[-1]
    if any(x in model for x in ('preview', 'experimental', 'exp-', 'audio', 'image',
                                 'lite', 'nano', 'codex', 'computer', 'search', ':free')):
        return 0
    if re.fullmatch(r'claude-(sonnet|opus)-[45][\w.-]*', model):
        return 2
    if re.fullmatch(r'gpt-5(?:\.\d+)?(?:-mini)?(?:-\d{4}-\d{2}-\d{2})?', model):
        return 2
    if re.fullmatch(r'gemini-(?:2\.5|3(?:\.\d+)?)-(?:pro|flash)(?:-\d+)?', model):
        return 2
    if re.fullmatch(r'deepseek-(?:reasoner|v[34][\w.-]*)', model):
        return 2
    if re.fullmatch(r'claude-haiku-4-5(?:-\d+)?', model) or model == 'deepseek-chat':
        return 1
    return 0


def recommend(cfg, task, *, data=None, prices=None):
    data = catalog.read() if data is None else data
    prices = read() if prices is None else prices
    candidates = []
    for provider, entry in data.items():
        if not fresh(entry, days=7):
            continue
        try:
            credentials(provider)
        except (AssistantUnavailable, KeyError):
            continue
        for model in entry.get('models', []):
            name = model['id']
            if tier(name) < (2 if task in COMPLEX else 1):
                continue
            row = prices.get('models', {}).get(f'{provider}:{name}', {})
            if not fresh(row):
                continue
            meta = row.get('metadata', {})
            if meta.get('deprecation_date', '9999') <= utc_now().date().isoformat():
                continue
            if provider == 'openrouter':
                params = model.get('supported_parameters') or []
                compatible = 'tools' in params and 'structured_outputs' in params
                context = meta.get('context_length', 0)
                output = meta.get('top_provider', {}).get('max_completion_tokens') or 0
            else:
                compatible = (meta.get('supports_function_calling') is True and
                              meta.get('supports_response_schema') is True)
                context = meta.get('max_input_tokens', 0)
                output = meta.get('max_output_tokens', 0)
            if not compatible or context < 64000 or output < getattr(cfg.features, task).max_tokens:
                continue
            route = ModelRoute(provider=provider, model=name)
            price = cfg.price_for(route)
            if price is None:
                continue
            # Illustrative 8k input / 2k output workload; deterministic tie breaks.
            candidates.append((price.input * .008 + price.output * .002,
                               provider, name, route))
    if candidates:
        cost, _, _, route = min(candidates)
        need = ('reasoning and structured analysis' if task in COMPLEX
                else 'extraction / classification')
        return route, (f'Suggested: {route.provider}:{route.model} — lowest estimated cost '
                       f'among eligible models for {need}; about ${cost:.4f} per '
                       '8,000 input + 2,000 output tokens. Policy-based, not a task benchmark.')
    route = ModelRoute(provider='anthropic', model=cfg.model)
    return route, ('No eligible priced model is available with configured credentials. '
                   f'Using the configured fallback: anthropic:{cfg.model}.')
