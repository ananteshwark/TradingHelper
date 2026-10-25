"""Resumable, local-only filing extraction experiments; never writes trading tables.

python -m igs.gpu_lab --help
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx

from igs.forward import MAX_TEXT, SYSTEM, Claims, document_text, schema, validate_claims

VERSION = 'filing-benchmark-v1'
ENDPOINTS = {'computer': 'http://127.0.0.1:18081/v1',
             'server': 'http://127.0.0.1:18080/v1'}


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('timestamps must include a timezone')
    return parsed


def document(value):
    """Require provenance; never silently truncate a benchmark document."""
    for key in ('document_id', 'source_url', 'text', 'published_at', 'received_at'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f'missing document field: {key}')
    timestamp(value['published_at'])
    timestamp(value['received_at'])
    if len(value['text']) > MAX_TEXT:
        raise ValueError('document exceeds extraction text limit')
    return {key: value[key] for key in
            ('document_id', 'source_url', 'text', 'published_at', 'received_at')}


def database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('pragma journal_mode=WAL')
    conn.executescript('''
        create table if not exists jobs (
            id text primary key, model text not null, document text not null,
            protocol text not null, priority integer not null, created real not null,
            status text not null default 'pending', attempts integer not null default 0,
            available real not null default 0, result text, error text);
        create table if not exists attempts (
            job_id text not null, number integer not null, started real not null,
            finished real, error text, primary key(job_id, number));
    ''')
    return conn


def enqueue(conn, doc, model, protocol, priority=50):
    doc = document(doc)
    # Include the exact prompt/schema/version, timestamps and source in cache identity.
    identity = digest([VERSION, SYSTEM, schema(), doc, model, protocol])
    with conn:
        conn.execute('''insert or ignore into jobs
            (id,model,document,protocol,priority,created) values(?,?,?,?,?,?)''',
                     (identity, model, encode(doc), protocol, priority, time.time()))
    return identity


def claim(conn, now=None):
    """One research inference across workers; expired leases survive a crashed worker."""
    now = time.time() if now is None else now
    with conn:
        conn.execute('begin immediate')
        conn.execute("""update attempts set finished=?,error='lease expired'
            where finished is null and job_id in
            (select id from jobs where status='running' and available<=?)""", (now, now))
        conn.execute("""update jobs set status=case when attempts>=3 then 'failed'
            else 'pending' end,error='lease expired'
            where status='running' and available<=?""", (now,))
        if conn.execute("select 1 from jobs where status='running'").fetchone():
            return None
        row = conn.execute("""select * from jobs where status='pending' and available<=?
            order by priority,created,id limit 1""", (now,)).fetchone()
        if row is None:
            return None
        number = row['attempts'] + 1
        conn.execute("update jobs set status='running',attempts=?,available=? where id=?",
                     (number, now + 900, row['id']))
        conn.execute('insert into attempts(job_id,number,started) values(?,?,?)',
                     (row['id'], number, now))
        return {**dict(row), 'attempts': number}


def finish(conn, job, result=None, error=None):
    now = time.time()
    with conn:
        # A late response must not overwrite a newer lease/result.
        current = conn.execute('select status,attempts from jobs where id=?',
                               (job['id'],)).fetchone()
        if current['status'] != 'running' or current['attempts'] != job['attempts']:
            return
        state = 'done' if error is None else ('failed' if job['attempts'] >= 3 else 'pending')
        conn.execute('''update jobs set status=?,result=?,error=?,available=? where id=?''',
                     (state, encode(result) if result is not None else None, error,
                      now + 60 * job['attempts'], job['id']))
        conn.execute('update attempts set finished=?,error=? where job_id=? and number=?',
                     (now, error, job['id'], job['attempts']))


def infer(job, client):
    doc = json.loads(job['document'])
    start = time.perf_counter()
    response = client.post('/chat/completions', json={
        'model': job['model'], 'temperature': 0, 'max_tokens': 4096,
        'response_format': {'type': 'json_object'},
        'messages': [
            {'role': 'system', 'content': SYSTEM + '\nJSON schema:\n' + encode(schema())},
            {'role': 'user', 'content': encode({'filed_at': doc['published_at'],
                                              'text': doc['text']})}]})
    response.raise_for_status()
    data = response.json()
    if data.get('model') != job['model']:
        raise ValueError('model identity changed')
    choice = data['choices'][0]
    if choice.get('finish_reason') != 'stop':
        raise ValueError('incomplete extraction')
    raw = json.loads(choice['message']['content'])
    parsed = Claims.model_validate(raw)
    valid = validate_claims(raw, doc['text'], timestamp(doc['published_at']))
    return {'claims': valid, 'raw_claims': parsed.model_dump()['items'],
            'rejected_claims': len(parsed.items) - len(valid),
            'model': job['model'], 'seconds': time.perf_counter() - start,
            'usage': data.get('usage'), 'completed_at': dt.datetime.now(dt.UTC).isoformat(),
            'text_sha256': hashlib.sha256(doc['text'].encode()).hexdigest(),
            'version': VERSION}


def worker(conn, client, limit):
    for _ in range(limit):
        job = claim(conn)
        if job is None:
            break
        try:
            result = infer(job, client)
        except Exception as exc:  # noqa: BLE001 - persist failure; retry without secrets
            detail = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                detail += f': HTTP {exc.response.status_code}'
            finish(conn, job, error=detail)
            print(encode({'job': job['id'], 'error': detail}), flush=True)
        else:
            finish(conn, job, result=result)
            print(encode({'job': job['id'], 'claims': len(result['claims']),
                          'seconds': result['seconds']}), flush=True)


FACT_FIELDS = ('metric', 'kind', 'value', 'unit', 'scope', 'period_end')


def fact(item):
    return tuple(item[key] for key in FACT_FIELDS)


def report(conn, labels=None):
    """Exact fact matching against separately reviewed labels; missing is not negative."""
    labels = labels or {}
    out = {'version': VERSION, 'profitability': 'not evaluated', 'models': {}, 'jobs': []}
    for row in conn.execute('select * from jobs order by created,id'):
        doc = json.loads(row['document'])
        result = json.loads(row['result']) if row['result'] else None
        group = out['models'].setdefault(row['model'], {
            'statuses': {}, 'reviewed_documents': 0, 'true_positive': 0,
            'false_positive': 0, 'false_negative': 0, 'seconds': [],
            'precision': None, 'recall': None})
        group['statuses'][row['status']] = group['statuses'].get(row['status'], 0) + 1
        label = labels.get(doc['document_id'])
        if result:
            group['seconds'].append(result['seconds'])
        if label is not None:
            if label['text_sha256'] != hashlib.sha256(doc['text'].encode()).hexdigest():
                raise ValueError('label source hash mismatch')
            # Failed extractions count as missing facts, never disappear from recall.
            if row['status'] in ('done', 'failed'):
                expected = {fact(c) for c in Claims.model_validate(
                    {'items': label['items']}).model_dump()['items']}
                predicted = {fact(c) for c in result['claims']} if result else set()
                group['reviewed_documents'] += 1
                group['true_positive'] += len(expected & predicted)
                group['false_positive'] += len(predicted - expected)
                group['false_negative'] += len(expected - predicted)
        out['jobs'].append({'id': row['id'], 'document': doc, 'model': row['model'],
                            'status': row['status'], 'attempts': row['attempts'],
                            'protocol_sha256': row['protocol'], 'result': result,
                            'error': row['error'], 'reviewed': label is not None})
    for group in out['models'].values():
        tp, fp, fn = (group[k] for k in ('true_positive', 'false_positive', 'false_negative'))
        group['precision'] = tp / (tp + fp) if tp + fp else None
        group['recall'] = tp / (tp + fn) if tp + fn else None
    return out


def export_documents(out, limit):
    """Read-only pilot export; newly downloaded text is received now, never backdated."""
    from igs import envfile
    from igs.db import connect

    envfile.load(envfile.default_path())
    documents, errors = [], []
    with connect() as conn:
        conn.execute('set transaction read only')
        rows = conn.execute('''select ann_id,source_url,published_at,received_at,text_content
            from forward_document where length(text_content)>100 and source_url is not null
            order by received_at desc,ann_id desc limit %s''', (limit,)).fetchall()
        fallback = not rows
        if fallback:
            rows = conn.execute('''select ann_id,attachment_url,filed_at from announcement
                where exchange='NSE' and filed_at<=now()
                and filed_at>=now()-interval '30 days' and attachment_url is not null
                and (subject||' '||coalesce(body,'')) ~* %s
                order by filed_at desc,ann_id desc limit %s''',
                ('guidance|order|contract|capacity|utilisation|utilization|investor presentation|'
                 'earnings|acquisition|segment|capital adequacy', limit)).fetchall()
    for row in rows:
        ann, url, published = row[:3]
        try:
            if fallback:
                _, text = document_text(url, '')
                received = dt.datetime.now(dt.UTC)
            else:
                received, text = row[3:]
            documents.append(document(dict(document_id=str(ann), source_url=url,
                published_at=published.isoformat(), received_at=received.isoformat(), text=text)))
        except Exception as exc:  # noqa: BLE001 - retain failed sources in sample manifest
            errors.append({'document_id': str(ann), 'source_url': url,
                           'error': type(exc).__name__})
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x', encoding='utf-8') as handle:
        handle.write(''.join(encode(d) + '\n' for d in documents))
    manifest = {'fallback': fallback, 'selected': len(rows), 'exported': len(documents),
                'failures': errors, 'exported_at': dt.datetime.now(dt.UTC).isoformat(),
                'text_limit': MAX_TEXT, 'pdf_page_limit': 20,
                'file_sha256': hashlib.sha256(out.read_bytes()).hexdigest()}
    out.with_suffix('.manifest.json').write_text(encode(manifest), encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('data/gpu-lab/queue.sqlite'))
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export', help='Read-only public-document sample from app database')
    export.add_argument('--out', type=Path, required=True)
    export.add_argument('--limit', type=int, default=5)
    add = sub.add_parser('enqueue')
    add.add_argument('--documents', type=Path, required=True)
    add.add_argument('--model', required=True, help='Exact loaded model ID, never an alias')
    add.add_argument('--protocol', type=Path, required=True)
    add.add_argument('--priority', type=int, default=50, help='Lower runs first')
    run = sub.add_parser('work')
    run.add_argument('--location', choices=ENDPOINTS, default='computer')
    run.add_argument('--limit', type=int, default=5)
    output = sub.add_parser('report')
    output.add_argument('--labels', type=Path)
    output.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'export':
        if not 1 <= args.limit <= 100:
            parser.error('limit must be 1..100')
        print(encode(export_documents(args.out, args.limit)))
        return
    with database(args.db) as conn:
        if args.command == 'enqueue':
            protocol = hashlib.sha256(args.protocol.read_bytes()).hexdigest()
            docs = [document(json.loads(line)) for line in
                    args.documents.read_text(encoding='utf-8').splitlines() if line.strip()]
            for doc in docs:
                print(enqueue(conn, doc, args.model, protocol, args.priority))
        elif args.command == 'work':
            if not 1 <= args.limit <= 100:
                parser.error('limit must be 1..100')
            key = os.environ['STRATA_API_KEY']
            with httpx.Client(base_url=ENDPOINTS[args.location], trust_env=False,
                              headers={'Authorization': 'Bearer ' + key},
                              timeout=httpx.Timeout(600, connect=5)) as client:
                worker(conn, client, args.limit)
        else:
            labels = json.loads(args.labels.read_text()) if args.labels else None
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report(conn, labels), indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
