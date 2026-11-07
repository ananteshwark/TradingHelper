import hashlib
import json

import httpx
import pytest

from igs import gpu_lab as lab


def doc(text='Consolidated capacity was 100 MW for the year ended 2026-03-31.'):
    return dict(document_id='1', source_url='https://www.nseindia.com/filing.pdf',
                published_at='2026-04-01T10:00:00+00:00',
                received_at='2026-04-01T10:01:00+00:00', text=text)


def item():
    return dict(metric='capacity', kind='reported', value=100, unit='MW',
                scope='consolidated', period_end='2026-03-31', quote=doc()['text'],
                confidence=0.95)


def test_cache_identity_includes_model_protocol_and_source(tmp_path):
    with lab.database(tmp_path / 'queue.db') as conn:
        first = lab.enqueue(conn, doc(), 'qwen', 'protocol')
        assert first == lab.enqueue(conn, doc(), 'qwen', 'protocol')
        assert first != lab.enqueue(conn, doc(), 'deepseek', 'protocol')
        assert first != lab.enqueue(conn, doc(), 'qwen', 'changed')
        assert first != lab.enqueue(conn, doc('different text'), 'qwen', 'protocol')
        assert conn.execute('select count(*) from jobs').fetchone()[0] == 4


def test_priority_single_worker_crash_recovery_and_late_result(tmp_path):
    path = tmp_path / 'queue.db'
    with lab.database(path) as conn, lab.database(path) as other:
        lab.enqueue(conn, doc(), 'qwen', 'low', 50)
        high = lab.enqueue(conn, doc(), 'qwen', 'high', 1)
        first = lab.claim(conn, now=100)
        assert first['id'] == high
        assert lab.claim(other, now=101) is None
        recovered = lab.claim(other, now=1001)
        assert recovered['attempts'] == 2
        lab.finish(conn, first, {'stale': True})
        assert conn.execute('select result from jobs where id=?', (high,)).fetchone()[0] is None
        lab.finish(other, recovered, error='busy')
        status = conn.execute('select status from jobs where id=?', (high,)).fetchone()[0]
        assert status == 'pending'
        assert lab.claim(conn, now=1002)['protocol'] == 'low'


def test_exhausted_crashed_job_becomes_failed(tmp_path):
    with lab.database(tmp_path / 'queue.db') as conn:
        key = lab.enqueue(conn, doc(), 'qwen', 'protocol')
        for now in (0, 901, 1802):
            assert lab.claim(conn, now=now)
        assert lab.claim(conn, now=2703) is None
        assert conn.execute('select status from jobs where id=?', (key,)).fetchone()[0] == 'failed'


def test_http_busy_is_persisted_without_response_or_token(tmp_path):
    with lab.database(tmp_path / 'queue.db') as conn:
        lab.enqueue(conn, doc(), 'qwen', 'protocol')
        with httpx.Client(base_url='http://127.0.0.1:18081/v1',
                          transport=httpx.MockTransport(lambda _: httpx.Response(
                              503, text='sensitive upstream content'))) as client:
            lab.worker(conn, client, 1)
        row = conn.execute('select * from jobs').fetchone()
        assert row['status'] == 'pending'
        assert row['error'] == 'HTTPStatusError: HTTP 503'


def test_extraction_and_hallucinated_quote(tmp_path):
    with lab.database(tmp_path / 'queue.db') as conn:
        lab.enqueue(conn, doc(), 'qwen', 'protocol')
        job = lab.claim(conn)
        def respond(request):
            assert request.url.path == '/v1/chat/completions'
            assert json.loads(request.content)['model'] == 'qwen'
            return httpx.Response(200, json={'model': 'qwen', 'choices': [{
                'finish_reason': 'stop', 'message': {'content': json.dumps({'items': [
                    item(), {**item(), 'quote': 'Invented source capacity of 100 MW.'}]})}}]})
        with httpx.Client(base_url='http://127.0.0.1:18081/v1',
                          transport=httpx.MockTransport(respond)) as client:
            result = lab.infer(job, client)
        assert result['claims'] == [item()]
        assert result['rejected_claims'] == 1
        lab.finish(conn, job, result)
        unreviewed = lab.report(conn)
        assert unreviewed['models']['qwen']['precision'] is None
        labels = {'1': {'text_sha256': hashlib.sha256(doc()['text'].encode()).hexdigest(),
                        'items': [item()]}}
        measured = lab.report(conn, labels)
        assert measured['models']['qwen']['precision'] == 1
        assert measured['models']['qwen']['recall'] == 1
        assert measured['profitability'] == 'not evaluated'
        labels['1']['text_sha256'] = 'changed'
        with pytest.raises(ValueError, match='hash mismatch'):
            lab.report(conn, labels)


def test_failed_documents_count_against_recall(tmp_path):
    with lab.database(tmp_path / 'queue.db') as conn:
        lab.enqueue(conn, doc(), 'qwen', 'protocol')
        conn.execute("update jobs set status='failed'")
        labels = {'1': {'text_sha256': hashlib.sha256(doc()['text'].encode()).hexdigest(),
                        'items': [item()]}}
        measured = lab.report(conn, labels)['models']['qwen']
        assert measured['recall'] == 0
        assert measured['false_negative'] == 1


def test_document_rejects_naive_time_and_silent_truncation():
    with pytest.raises(ValueError, match='timezone'):
        lab.document({**doc(), 'published_at': '2026-04-01T10:00:00'})
    with pytest.raises(ValueError, match='text limit'):
        lab.document(doc('x' * (lab.MAX_TEXT + 1)))
