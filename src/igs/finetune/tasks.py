"""Training data for FinGPT's tasks (research/local_models/PROTOCOL.md, Study E).

Each example is the request the app sends a local model for one article, rebuilt from the
stored inputs with the task's current system prompt and the schema suffix the app appends
(igs.assistant.providers.chat_create), and the answer a cloud model gave, rebuilt from the
stored results. Stock-news tones and geopolitical impacts are judged by the company's
excess return over the Nifty 500 over the next three sessions and filtered as the
protocol fixes; broker calls are imitated.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from igs.assistant import brokers as broker_task
from igs.assistant import geopolitical, news_tone
from igs.assistant.calls import _index_series, _price_rows, price_index
from igs.timeutil import IST, utc_now

TRAIN_BEFORE = dt.date(2026, 10, 13)
EVAL_UNTIL = dt.date(2026, 10, 20)
HORIZON, BAND, NEUTRAL = 3, 0.5, 0.2          # sessions, % excess, |tone| below = neutral
GEO_AGREE = 0.6
SCHEMA_SUFFIX = '\nReturn only JSON matching this schema:\n'
CLOSE = dt.time(15, 30)


def system_for(system: str, schema: dict) -> str:
    return system + SCHEMA_SUFFIX + json.dumps(schema)


def example(system: str, schema: dict, prompt: str, answer: dict, meta: dict) -> dict:
    return {'messages': [{'role': 'system', 'content': system_for(system, schema)},
                         {'role': 'user', 'content': prompt},
                         {'role': 'assistant', 'content': json.dumps(answer, ensure_ascii=False)}],
            'meta': meta}


def local_since(conn) -> dt.datetime | None:
    """When the app first asked a local model anything: later reads may be local."""
    return conn.execute("select min(called_at) from llm_call where provider='local'").fetchone()[0]


def cloud(model: str | None) -> bool:
    return bool(model) and not model.startswith('local')


# --------------------------------------------------------------------------- outcomes


class Outcomes:
    """Excess returns over the Nifty 500, three sessions on, cached per company."""

    def __init__(self, conn, start: dt.date, end: dt.date):
        self.conn, self.start, self.end = conn, start - dt.timedelta(days=10), end
        self.index = dict(_index_series(conn, self.start, end))
        self.series: dict[int, list[tuple[dt.date, float]]] = {}

    def excess(self, company_id: int | None, published: dt.datetime) -> float | None:
        if company_id is None:
            return None
        if company_id not in self.series:
            self.series[company_id] = price_index(
                _price_rows(self.conn, company_id, self.start, self.end))
        idx = self.series[company_id]
        local = published.astimezone(IST)
        day = local.date() if local.time() >= CLOSE else local.date() - dt.timedelta(days=1)
        before = [i for i, (d, _) in enumerate(idx) if d <= day]
        if not before or before[-1] + HORIZON >= len(idx):
            return None
        (d0, p0), (d1, p1) = idx[before[-1]], idx[before[-1] + HORIZON]
        b0, b1 = self.index.get(d0), self.index.get(d1)
        if not (b0 and b1):
            return None
        return 100 * ((p1 / p0 - 1) - (b1 / b0 - 1))


def agrees(score: float, excess: float | None) -> bool | None:
    if excess is None:
        return None
    if score >= NEUTRAL:
        return excess > BAND
    if score <= -NEUTRAL:
        return excess < -BAND
    return abs(excess) <= BAND


# --------------------------------------------------------------------------- tasks


def tone_examples(conn, outcomes: Outcomes, since) -> list[dict]:
    articles = conn.execute("""select article_id, title, body, published_at, tone_read_at
        from broker_article where tone_read_at is not null and tone_read_at < %s
        order by published_at""", (since,)).fetchall()
    out = []
    for article_id, title, body, published, _ in articles:
        rows = conn.execute("""select company_id, company_text, tone::float8, confidence::float8,
            reason, quote, model from stock_news_tone where article_id=%s order by tone_id""",
                            (article_id,)).fetchall()
        if any(not cloud(r[6]) for r in rows):
            continue
        items = [{'article': 0, 'company': company, 'nse_symbol': '', 'tone': tone,
                  'confidence': conf, 'reason': reason, 'quote': quote}
                 for _, company, tone, conf, reason, quote, _ in rows]
        verdicts = [agrees(r[2], outcomes.excess(r[0], published)) for r in rows]
        kept = [i for i, v in zip(items, verdicts, strict=True) if v is not False]
        a = {'title': title, 'body': body, 'published_at': published}
        out.append({**example(news_tone.SYSTEM, news_tone.SCHEMA, news_tone._prompt([a]),
                              {'items': kept}, {
                                  'task': 'news_tone', 'id': article_id,
                                  'published': published.astimezone(IST).date().isoformat(),
                                  'filtered': 'dropped' if rows and not kept else 'kept',
                                  'company_ids': [r[0] for r in rows],
                                  'verdicts': verdicts}),
                    'article': {'title': title, 'body': body}})
    return out


def broker_examples(conn, since) -> list[dict]:
    articles = conn.execute("""select article_id, title, body, published_at from broker_article
        where candidate and read_at is not null and read_at < %s order by published_at""",
                            (since,)).fetchall()
    out = []
    for article_id, title, body, published in articles:
        rows = conn.execute("""select stock_name, broker, rating, stance, kind,
            target_price::float8, called_on, quote, model from broker_call
            where article_id=%s and source='news' order by broker_call_id""",
                            (article_id,)).fetchall()
        if any(not cloud(r[8]) for r in rows):
            continue
        day = published.astimezone(IST).date()
        calls = [{'article': 0, 'broker': b, 'company': s, 'nse_symbol': '', 'rating': r,
                  'stance': st, 'kind': k, 'target_price': t or 0,
                  'report_date': '' if c == day else c.isoformat(), 'quote': q or ''}
                 for s, b, r, st, k, t, c, q, _ in rows]
        a = {'title': title, 'body': body, 'published_at': published}
        out.append({**example(broker_task.SYSTEM, broker_task.SCHEMA,
                              broker_task._prompt([a]), {'calls': calls}, {
                                  'task': 'brokers', 'id': article_id,
                                  'published': day.isoformat(), 'filtered': 'kept'}),
                    'article': {'title': title, 'body': body}})
    return out


def geo_examples(conn, outcomes: Outcomes) -> list[dict]:
    news = conn.execute("""select n.news_id, n.title, n.body, n.companies, n.intake,
        n.published_at from geopolitical_news n
        where exists (select 1 from geopolitical_assessment a where a.news_id=n.news_id)
        order by n.published_at""").fetchall()
    out = []
    for news_id, title, body, companies, intake, published in news:
        rows = conn.execute("""select company_id, impact, confidence, channel, rationale,
            evidence, model from geopolitical_assessment where news_id=%s
            order by company_id""", (news_id,)).fetchall()
        if any(not cloud(r[6]) for r in rows) or \
                {r[0] for r in rows} != {c['company_id'] for c in companies}:
            continue
        verdicts = [agrees(r[1], outcomes.excess(r[0], published)) for r in rows]
        labelled = [v for v in verdicts if v is not None]
        keep = not labelled or sum(labelled) / len(labelled) >= GEO_AGREE
        items = [{'company_id': cid, 'impact': imp, 'confidence': conf, 'channel': ch,
                  'rationale': rat, 'evidence': ev}
                 for cid, imp, conf, ch, rat, ev, _ in rows]
        payload = {'title': title, 'body': body, 'companies': companies, 'intake': intake}
        out.append({**example(geopolitical.SYSTEM, geopolitical.output_schema(),
                              json.dumps(payload), {'items': items}, {
                                  'task': 'geopolitical', 'id': news_id,
                                  'published': published.astimezone(IST).date().isoformat(),
                                  'filtered': 'kept' if keep else 'dropped',
                                  'verdicts': verdicts}),
                    'article': {'title': title, 'body': body}})
    return out


def export(conn, out: Path) -> dict:
    since = local_since(conn) or dt.datetime.now(dt.UTC)
    outcomes = Outcomes(conn, dt.date(2026, 9, 1), utc_now().astimezone(IST).date())
    rows = (tone_examples(conn, outcomes, since) + broker_examples(conn, since)
            + geo_examples(conn, outcomes))
    out.mkdir(parents=True, exist_ok=True)
    summary = {'local_since': str(since), 'train_before': TRAIN_BEFORE.isoformat(),
               'eval_until': EVAL_UNTIL.isoformat()}
    with (out / 'train.jsonl').open('w', encoding='utf-8') as tr, \
            (out / 'eval.jsonl').open('w', encoding='utf-8') as ev:
        for r in rows:
            day = dt.date.fromisoformat(r['meta']['published'])
            task = r['meta']['task']
            if day < TRAIN_BEFORE and r['meta']['filtered'] == 'kept':
                tr.write(json.dumps({'messages': r['messages'], 'meta': r['meta']},
                                    default=str) + '\n')
                summary[f'{task}_train'] = summary.get(f'{task}_train', 0) + 1
            elif TRAIN_BEFORE <= day <= EVAL_UNTIL:
                ev.write(json.dumps({'messages': r['messages'][:2], 'meta': r['meta'],
                                     'answer': json.loads(r['messages'][2]['content']),
                                     'article': r['article']}, default=str) + '\n')
                summary[f'{task}_eval'] = summary.get(f'{task}_eval', 0) + 1
            if day < TRAIN_BEFORE and r['meta']['filtered'] == 'dropped':
                summary[f'{task}_dropped'] = summary.get(f'{task}_dropped', 0) + 1
    (out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary
