import datetime as dt

import pytest
from psycopg.types.json import Jsonb
from test_intraday import NOW, sample, seed_stock

from igs.alerts.intraday import deliver, eligible, message
from igs.intraday.engine import BAR, evaluate

pytestmark = pytest.mark.usefixtures("intraday_eligibility")


def signal():
    bars, history = sample()
    return evaluate(bars, history, NOW, bars)


def add_scan(conn, cid, result=None, status='complete', observed=NOW):
    run = conn.execute('insert into intraday_scan(status) values(%s) returning scan_id',
                       (status,)).fetchone()[0]
    conn.execute('''insert into intraday_signal values(%s,%s,'TEST','NSE_EQ|INE123456789',
        %s,%s)''', (run, cid, observed, Jsonb(result or signal())))
    conn.commit()
    return run


def test_message_contains_execution_levels_and_validity():
    text = message('TEST', signal())
    for part in ('INTRADAY BUY · TEST', 'Entry limit (recommended price): ₹',
                 'Stop: ₹', 'Target: ₹',
                 '5.00×', 'momentum:', 'expires:', 'IST', 'rule-based'):
        assert part in text
    assert len(text) < 4000


@pytest.mark.lookahead
def test_no_stale_future_or_overnight_alerts():
    r = signal()
    assert eligible(r, NOW, NOW)
    assert not eligible(r, NOW+BAR, NOW)
    assert not eligible(r, NOW, NOW+BAR)
    assert not eligible(r, NOW, NOW+dt.timedelta(days=1))
    assert not eligible({**r, 'action': 'wait'}, NOW, NOW)
    assert not eligible({**r, 'candle_end': (NOW+BAR).isoformat()}, NOW, NOW)
    assert not eligible({**r, 'expires_at': NOW.isoformat()}, NOW, NOW)


@pytest.mark.db
def test_once_per_stock_direction_day_and_reversal(db_conn):
    cid = seed_stock(db_conn)
    sent = []
    sender = lambda text: sent.append(text) or True  # noqa: E731
    add_scan(db_conn, cid)
    assert deliver(db_conn, sender, clock=lambda: NOW) == 1
    assert deliver(db_conn, sender, clock=lambda: NOW) == 0
    add_scan(db_conn, cid)
    assert deliver(db_conn, sender, clock=lambda: NOW) == 0
    add_scan(db_conn, cid, {**signal(), 'action': 'sell'})
    assert deliver(db_conn, sender, clock=lambda: NOW) == 1
    assert len(sent) == 2 and sent[1].startswith('INTRADAY SELL')
    add_scan(db_conn, cid)
    assert deliver(db_conn, sender, clock=lambda: NOW) == 0


@pytest.mark.db
def test_retry_redacts_errors_and_does_not_send_withdrawn_calls(db_conn):
    cid = seed_stock(db_conn)
    add_scan(db_conn, cid)

    def fail(text):
        raise RuntimeError('private-token must not persist')

    assert deliver(db_conn, fail, clock=lambda: NOW) == 0
    row = db_conn.execute('select attempts,last_error from intraday_telegram').fetchone()
    assert row == (1, 'RuntimeError')
    sent = []
    sender = lambda text: sent.append(text) or True  # noqa: E731
    assert deliver(db_conn, sender, clock=lambda: NOW) == 0
    assert deliver(db_conn, sender, clock=lambda: NOW+dt.timedelta(minutes=1)) == 1
    assert len(sent) == 1


@pytest.mark.db
@pytest.mark.parametrize('state', ['wait', 'failed', 'expired'])
def test_unsent_calls_discarded_after_signal_changes(db_conn, state):
    cid = seed_stock(db_conn)
    add_scan(db_conn, cid)
    assert deliver(db_conn, lambda _: False, clock=lambda: NOW) == 0
    current = NOW + dt.timedelta(minutes=1)
    if state == 'wait':
        add_scan(db_conn, cid, {**signal(), 'action': 'wait'})
    elif state == 'failed':
        add_scan(db_conn, cid, status='failed')
    else:
        current = NOW+dt.timedelta(minutes=11)
    sent = []
    assert deliver(db_conn, lambda text: sent.append(text) or True, clock=lambda: current) == 0
    assert sent == []
    assert db_conn.execute('select status from intraday_telegram').fetchone()[0] == 'expired'


@pytest.mark.db
def test_incomplete_scans_are_never_announced(db_conn):
    cid = seed_stock(db_conn)
    add_scan(db_conn, cid, status='running')
    assert deliver(db_conn, lambda _: pytest.fail('must not send'), clock=lambda: NOW) == 0
    assert db_conn.execute('select count(*) from intraday_telegram').fetchone()[0] == 0


@pytest.mark.db
def test_ineligible_stock_is_not_sent_to_telegram(db_conn, monkeypatch):
    cid = seed_stock(db_conn)
    add_scan(db_conn, cid)
    assert deliver(db_conn, sender=lambda _: False, clock=lambda: NOW) == 0
    monkeypatch.setattr('igs.intraday.eligibility.allowed_instruments', lambda **kwargs: set())
    sent = []
    assert deliver(db_conn, sender=sent.append, clock=lambda: NOW) == 0
    assert sent == []
    assert db_conn.execute('select status from intraday_telegram').fetchone()[0] == 'expired'


def test_automatic_call_message_does_not_request_approval():
    result = {**signal(), 'candle_volume': 1_000_020, 'baseline_volume': 20_000}
    text = message('TEST', result, automatic=True)
    assert 'Automatic order eligible' in text
    assert 'Reply APPROVED' not in text
