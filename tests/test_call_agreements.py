"""Agreement semantics, durable Telegram delivery and immediate review at ingestion."""
import datetime as dt
from urllib.parse import parse_qs

import httpx
import pytest
import test_verdicts as V

from igs import brokers, call_list, config
from igs.alerts import delivery
from igs.assistant import verdicts
from igs.assistant.llm import Assistant
from igs.timeutil import IST

scored = V.scored
pytestmark = pytest.mark.db


def test_agreement_sends_once_and_table_combines_sources(scored, monkeypatch):
    conn, run_id = scored
    brokers.add_manual(conn, 'BANK', 'Example broker', 'buy', 12000, V.TODAY)
    ids = V._ids(conn, 'BANK')
    requests = []
    def receive(req):
        requests.append(req)
        return httpx.Response(200, json={'ok': True, 'result': {}})
    client = httpx.Client(transport=httpx.MockTransport(receive))
    monkeypatch.setattr(delivery, '_client', lambda: client)
    monkeypatch.setenv('IGS_TELEGRAM_TOKEN', 'fake-token')
    monkeypatch.setenv('IGS_TELEGRAM_CHAT_ID', '123')
    assert call_list.rows(conn, [])[0]['call'] == 'Pending review'
    for _ in range(2):
        verdicts.review(V.assistant(conn, V.answer(ids)), 'BANK', run_id)
    assert len(requests) == 1
    assert 'BUY BANK' in parse_qs(requests[0].content.decode())['text'][0]
    assert '82% (High)' in parse_qs(requests[0].content.decode())['text'][0]
    assert conn.execute("select status from alert_outbox").fetchall() == [('sent',)]
    entries = call_list.rows(conn, [{'call_id': 987, 'symbol': 'GROW', 'action': 'sell',
        'created_at': dt.datetime.now(IST), 'summary': 'Weak earnings', 'confidence': .7}])
    assert {r['source'] for r in entries} == {'Broker', 'AI'}
    broker = next(r for r in entries if r['source'] == 'Broker')
    assert broker['call'] == 'Buy' and broker["AI's verdict"] == 'agree'


def test_only_full_agreement_on_buy_sell_queues_and_withdrawal_cancels(scored):
    conn, run_id = scored
    for firm, stance in [('B', 'buy'), ('S', 'sell'), ('H', 'hold')]:
        brokers.add_manual(conn, 'BANK', firm, stance, 12000, V.TODAY)
    ids = V._ids(conn, 'BANK')
    for decision in ['partly agree', 'disagree', 'cannot judge']:
        verdicts.review(V.assistant(conn, V.answer(ids, decision)), 'BANK', run_id)
        assert conn.execute('select count(*) from alert_outbox').fetchone()[0] == 0
    verdicts.review(V.assistant(conn, V.answer(ids)), 'BANK', run_id)
    assert conn.execute('select count(*) from alert_outbox').fetchone()[0] == 2
    verdicts.review(V.assistant(conn, V.answer(ids, 'disagree')), 'BANK', run_id)
    assert conn.execute('select distinct status from alert_outbox').fetchall() == [('cancelled',)]
    assert all(r['call'] == 'Not confirmed' for r in call_list.rows(conn, []))


def test_manual_add_starts_review_in_same_request(scored, monkeypatch):
    conn, run_id = scored
    fake = V.assistant(conn, V.answer([1]))
    monkeypatch.setattr(config, 'load_assistant', lambda: fake.cfg)
    monkeypatch.setattr(Assistant, 'open', lambda *args, **kwargs: fake)
    assert brokers.add_manual(conn, 'BANK', 'Example broker', 'sell', 9000, V.TODAY)
    assert call_list.rows(conn, [])[0]['call'] == 'Sell'
    assert conn.execute('select count(*) from ai_broker_review').fetchone()[0] == 1
    # Duplicate ingestion does not pay for another review or queue a second alert.
    assert not brokers.add_manual(conn, 'BANK', 'Example broker', 'sell', 9000, V.TODAY)
    assert conn.execute('select count(*) from ai_broker_review').fetchone()[0] == 1


def test_deleted_call_cancels_queued_message(scored):
    conn, run_id = scored
    brokers.add_manual(conn, 'BANK', 'Example broker', 'buy', 12000, V.TODAY)
    ids = V._ids(conn, 'BANK')
    verdicts.review(V.assistant(conn, V.answer(ids)), 'BANK', run_id)
    brokers.delete_manual(conn, ids[0])
    assert conn.execute('select status from alert_outbox').fetchall() == [('cancelled',)]
    assert delivery.deliver_pending(conn, config.load_alerts()) == {}


def test_verdict_confidence_is_required_and_bounded():
    from pydantic import ValidationError

    from igs.assistant.calls import BrokerVerdict
    record = {'id': 1, 'verdict': 'agree', 'reason': 'Revenue grew by 18 percent.'}
    with pytest.raises(ValidationError):
        BrokerVerdict.model_validate(record)
    for invalid in (-0.1, 1.1, float('nan'), float('inf'), None):
        with pytest.raises(ValidationError):
            BrokerVerdict.model_validate({**record, 'confidence': invalid})
    assert BrokerVerdict.model_validate({**record, 'confidence': 0}).confidence == 0
    assert call_list.confidence_level(0) == 'Low'
    assert call_list.confidence_level(.5) == 'Medium'
    assert call_list.confidence_level(.75) == 'High'
    assert call_list.confidence_level(None) == 'Not assessed'


def test_legacy_confidence_is_unknown_and_queued_without_repeat_alert(scored):
    conn, run_id = scored
    brokers.add_manual(conn, 'BANK', 'Example broker', 'buy', 12000, V.TODAY)
    ids = V._ids(conn, 'BANK')
    verdicts.review(V.assistant(conn, V.answer(ids)), 'BANK', run_id)
    conn.execute('update ai_broker_verdict set confidence=null')
    conn.execute('delete from alert_outbox')
    conn.execute("delete from alert_log where kind='broker_agreement'")
    conn.commit()
    old = call_list.rows(conn, [])[0]
    assert old['confidence'] is None and old['confidence level'] == 'Not assessed'
    assert verdicts.waiting(conn,30)[0]['why'] == 'confidence'
    verdicts.review(V.assistant(conn, V.answer(ids)), 'BANK', run_id)
    new = call_list.rows(conn, [])[0]
    assert new['confidence'] == .82 and new['confidence level'] == 'High'
    assert verdicts.pending(conn,30) == []
    assert conn.execute("select count(*) from alert_log where kind='broker_agreement'"
                        ).fetchone()[0] == 0
