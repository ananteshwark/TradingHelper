"""Score a local model on FinGPT's held-out tasks next to the cloud answers
(research/local_models/PROTOCOL.md, Evaluation and Use rule).

Each held-out request goes to the model through Bionic's OpenAI-compatible API on this
computer, constrained to the task's JSON schema as the gateway constrains it. Every answer
must pass the app's own checks; tones and impacts are judged against the outcome exactly
as the training labels were, the cloud's stored answers on the same articles alongside.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import httpx
from pydantic import ValidationError

from igs import brokers
from igs.assistant import brokers as broker_task
from igs.assistant import geopolitical, news_tone
from igs.finetune.evaluate import endpoint
from igs.finetune.tasks import Outcomes, agrees

SCHEMAS = {'news_tone': news_tone.SCHEMA, 'brokers': broker_task.SCHEMA,
           'geopolitical': geopolitical.output_schema()}


def ask(client, base_url, model, row) -> dict | None:
    task = row['meta']['task']
    r = client.post(f'{base_url}/chat/completions', json={
        'model': model, 'messages': row['messages'], 'temperature': 0, 'max_tokens': 2048,
        'response_format': {'type': 'json_schema', 'json_schema': {
            'name': 'result', 'schema': SCHEMAS[task], 'strict': False}}})
    r.raise_for_status()
    try:
        return json.loads(r.json()['choices'][0]['message']['content'])
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def checked(task, answer, row) -> list | None:
    """The answer's items if it passes the app's checks for the task, else None."""
    if not isinstance(answer, dict):
        return None
    article = row['article']
    try:
        if task == 'news_tone':
            items = [news_tone.ReadTone.model_validate(i) for i in answer['items']]
            ok = all(i.article == 0 and news_tone.quoted(i.quote, article) for i in items)
        elif task == 'brokers':
            items = [broker_task.ReadCall.model_validate(i) for i in answer['calls']]
            ok = all(i.article == 0 for i in items)
        else:
            items = geopolitical.Assessment.model_validate(answer).items
            want = {c['company_id'] for c in json.loads(row['messages'][1]['content'])['companies']}
            ok = ({i.company_id for i in items} == want and len(items) == len(want)
                  and all(i.evidence in article['body'] for i in items))
    except (ValidationError, KeyError, TypeError):
        return None
    return items if ok else None


def agreement(conn, outcomes, task, items, row):
    """(agreeing, judged) over the items' non-neutral calls with an outcome."""
    published = row['meta']['published_at']
    good = judged = 0
    for i in items:
        score = i.tone if task == 'news_tone' else i.impact
        if abs(score) < 0.2:
            continue
        cid = (brokers.match_company(conn, i.company, i.nse_symbol) if task == 'news_tone'
               else i.company_id)
        verdict = agrees(score, outcomes.excess(cid, published))
        if verdict is not None:
            judged += 1
            good += verdict
    return good, judged


def calls_of(items):
    return {(i.company.casefold().strip(), i.broker.casefold().strip(), i.stance)
            for i in items}


def run(conn, data: Path, model: str, base_url: str = 'http://127.0.0.1:5000/v1',
        limit: int | None = None) -> dict:
    import datetime as dt
    base_url = endpoint(base_url)
    rows = [json.loads(x) for x in (data / 'eval.jsonl').read_text(encoding='utf-8').splitlines()]
    rows = [r for r in rows if r['meta']['task'] in SCHEMAS][:limit]
    outcomes = Outcomes(conn, dt.date(2026, 9, 1), dt.date(2026, 10, 31))
    stats = defaultdict(lambda: defaultdict(int))
    with httpx.Client(timeout=300) as client:
        for row in rows:
            task = row['meta']['task']
            row['meta']['published_at'] = dt.datetime.fromisoformat(
                conn.execute('select published_at::text from broker_article where article_id=%s'
                             if task != 'geopolitical' else
                             'select published_at::text from geopolitical_news where news_id=%s',
                             (row['meta']['id'],)).fetchone()[0])
            s = stats[task]
            s['n'] += 1
            mine = checked(task, ask(client, base_url, model, row), row)
            theirs = checked(task, row['answer'], row)
            s['valid'] += mine is not None
            s['cloud_valid'] += theirs is not None
            if mine is None or theirs is None:
                continue
            if task == 'brokers':
                a, b = calls_of(mine), calls_of(theirs)
                s['tp'] += len(a & b)
                s['fp'] += len(a - b)
                s['fn'] += len(b - a)
                continue
            for who, items in (('', mine), ('cloud_', theirs)):
                good, judged = agreement(conn, outcomes, task, items, row)
                s[who + 'agree'] += good
                s[who + 'judged'] += judged
            s['found_cloud_none'] += bool(mine) and not theirs
            s['none_cloud_found'] += not mine and bool(theirs)
    report = {}
    for task, s in stats.items():
        r = {'articles': s['n'], 'valid_pct': round(100 * s['valid'] / s['n'], 1),
             'cloud_valid_pct': round(100 * s['cloud_valid'] / s['n'], 1)}
        if task == 'brokers':
            tp = s['tp']
            r['precision'] = round(tp / (tp + s['fp']), 3) if tp + s['fp'] else None
            r['recall'] = round(tp / (tp + s['fn']), 3) if tp + s['fn'] else None
            r['use'] = (r['valid_pct'] >= 95 and (r['precision'] or 0) >= 0.8
                        and (r['recall'] or 0) >= 0.8)
        else:
            for who in ('', 'cloud_'):
                j = s[who + 'judged']
                r[who + 'agree_pct'] = round(100 * s[who + 'agree'] / j, 1) if j else None
                r[who + 'judged'] = j
            r['found_where_cloud_none'] = s['found_cloud_none']
            r['none_where_cloud_found'] = s['none_cloud_found']
            r['use'] = (r['valid_pct'] >= 95 and r['agree_pct'] is not None
                        and r['cloud_agree_pct'] is not None
                        and r['agree_pct'] >= r['cloud_agree_pct'] - 2)
        report[task] = r
    (data / f'eval_report_{model}.json').write_text(json.dumps(report, indent=2),
                                                    encoding='utf-8')
    return report
