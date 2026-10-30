"""A Telegram chat for each kind of message (igs.alerts.delivery.CHATS): calls, trading,
the paper portfolios and general information, each falling back to IGS_TELEGRAM_CHAT_ID;
approvals by reply in a trading group, from the owner only; and the chats the bot has seen,
for Settings' Find chats. No request leaves the machine."""
from __future__ import annotations

import httpx
import pytest
from test_ml_orders import Broker, at, seed_call

from igs.alerts import delivery
from igs.alerts import operations as ops
from igs.config import OperationsAlerts

MAIN, CALLS, TRADING = '987654321', '-1001234567890', '-1002345678901'


def _env(monkeypatch, **chats):
    monkeypatch.setenv('IGS_TELEGRAM_TOKEN', 'fake-token')
    monkeypatch.setenv('IGS_TELEGRAM_CHAT_ID', MAIN)
    for kind, chat in chats.items():
        monkeypatch.setenv(delivery.CHATS[kind], chat)


def _recording():
    sent = []

    def answer(request):
        fields = dict(httpx.QueryParams(request.content.decode()))
        sent.append((fields['chat_id'], fields['text']))
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': len(sent)}})
    return sent, httpx.Client(transport=httpx.MockTransport(answer))


def test_each_kind_goes_to_its_own_chat_or_the_main_one(monkeypatch):
    _env(monkeypatch, calls=CALLS, trading=TRADING)
    sent, client = _recording()
    delivery.send_telegram('general', client)
    delivery.send_calls('a call', client)
    delivery.send_trading('an order', client)
    delivery.send_paper('a rebalance', client)                # no paper chat: the main one
    assert delivery.send_intraday_telegram('approve me', client) == 5
    monkeypatch.setenv(delivery.CHATS['results'], '-1004')
    delivery.send_results('a quarter', client)
    delivery.send_deals('a deal', client)                     # no deals chat: the main one
    assert sent == [(MAIN, 'general'), (CALLS, 'a call'), (TRADING, 'an order'),
                    (MAIN, 'a rebalance'), (TRADING, 'approve me'), ('-1004', 'a quarter'),
                    (MAIN, 'a deal')]
    assert delivery.chat_id('paper') == MAIN and delivery.chat_id('trading') == TRADING


def test_the_owner_is_the_main_private_chat_unless_set(monkeypatch):
    _env(monkeypatch)
    assert delivery.owner_id() == MAIN
    monkeypatch.setenv('IGS_TELEGRAM_CHAT_ID', '-1009')            # a group: not a person
    assert delivery.owner_id() == ''
    monkeypatch.setenv('IGS_TELEGRAM_OWNER_ID', '555')
    assert delivery.owner_id() == '555'


def test_the_test_message_reaches_every_chat(monkeypatch):
    _env(monkeypatch, calls=CALLS, trading=TRADING)
    sent, client = _recording()
    assert delivery.send_telegram_test(client) == [MAIN, CALLS, TRADING]
    assert sent[0][0] == CALLS and 'TEST message, not a real call' in sent[0][1]
    lines = dict(sent[1:])
    assert lines[MAIN].startswith('TEST: IndiaGrowthScreener. This chat gets: General')
    assert 'Paper portfolios' in lines[MAIN]
    assert lines[TRADING].endswith('Intraday calls, approvals and orders.')


def test_order_problems_go_to_the_trading_chat(monkeypatch, tmp_path):
    monkeypatch.setenv('IGS_NOTIFICATION_SPOOL', str(tmp_path / 'errors.sqlite3'))
    monkeypatch.setenv('IGS_OPERATIONAL_ALERTS', '1')
    monkeypatch.setattr(ops, 'settings', lambda: OperationsAlerts())
    ops.record_issue('intraday-trade', 'SquareOffFailed')
    ops.record_issue('cli.sync', 'DiskFull')
    sent = []
    monkeypatch.setattr(ops, 'send_telegram',
                        lambda text, client, chat: sent.append((chat, text)) or True)
    assert ops.deliver_errors() == 2
    assert [(c, t.splitlines()[0]) for c, t in sent] == [('trading', 'ORDER PROBLEM'),
                                                         ('general', 'DATABASE PROBLEM')]


def test_the_chats_the_bot_has_seen_are_kept_for_settings(monkeypatch):
    group = {'id': int(TRADING), 'type': 'supergroup', 'title': 'IGS trading'}
    delivery.remember_chats([
        {'update_id': 1, 'message': {'chat': {'id': int(MAIN), 'type': 'private',
                                              'first_name': 'Ananth'}, 'text': 'secret'}},
        {'update_id': 2, 'message': {'chat': group, 'new_chat_members': [{'id': 1}]}}])
    seen = delivery.seen_chats()
    assert [(c['id'], c['name'], c['type']) for c in seen] == [
        (int(TRADING), 'IGS trading', 'supergroup'), (int(MAIN), 'Ananth', 'private')]
    assert 'secret' not in delivery._seen_path().read_text()     # never the messages
    # Settings' Find chats: what Telegram still has, then what the poller saw.
    live = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={'ok': True, 'result': [{'update_id': 9, 'message': {
            'chat': {'id': -1003, 'type': 'group', 'title': 'IGS calls'}}}]})))
    assert [c['id'] for c in delivery.telegram_chats('fake-token', live)] == [
        -1003, int(TRADING), int(MAIN)]


@pytest.mark.db
@pytest.mark.usefixtures('intraday_eligibility', 'zero_intraday_charges')
def test_an_approval_in_the_trading_group_counts_only_from_the_owner(db_conn, monkeypatch):
    from igs.intraday.telegram_approvals import poll
    seed_call(db_conn)
    db_conn.execute('update ml_intraday_pick set telegram_message_id=77')
    db_conn.commit()
    _env(monkeypatch, trading=TRADING)
    group = {'id': int(TRADING), 'type': 'supergroup', 'title': 'IGS trading'}
    reply = {'text': 'APPROVED', 'reply_to_message': {'message_id': 77}}
    messages = [
        # Someone else in the group, then the owner in the main private chat: ignored.
        {'update_id': 1, 'message': {**reply, 'chat': group, 'from': {'id': 444}}},
        {'update_id': 2, 'message': {**reply, 'chat': {'id': int(MAIN), 'type': 'private'},
                                     'from': {'id': int(MAIN)}}},
        # The owner in the group: the order.
        {'update_id': 3, 'message': {**reply, 'chat': group, 'from': {'id': int(MAIN)}}}]

    def telegram(method, _token, _client, **fields):
        if method == 'getWebhookInfo':
            return {'result': {'url': ''}}
        return {'result': [m for m in messages if m['update_id'] >= int(fields['offset'])]}

    monkeypatch.setattr('igs.intraday.telegram_approvals._telegram', telegram)
    broker, said = Broker(100), []
    assert poll(db_conn, client=object(), broker=broker, clock=lambda: at(9, 52),
                notify=said.append) == 1
    assert len(broker.orders) == 1
    assert db_conn.execute('select telegram_update_id from intraday_trade').fetchone() == (3,)
    assert any(c['id'] == int(TRADING) for c in delivery.seen_chats())


@pytest.mark.db
def test_settings_saves_a_chat_for_each_kind(db_conn, monkeypatch):
    import os
    from pathlib import Path

    import streamlit
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    # Values the pickers start from, as if typed or pasted: a row copied from Find chats
    # with a typographic minus sign, a group's name, a phone number.
    _env(monkeypatch, trading=f'\u2212{TRADING[1:]}\tIGS trading\tsupergroup', calls='IGS calls')
    monkeypatch.setenv('IGS_TELEGRAM_OWNER_ID', '+919812345678')
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, 'get_option', lambda k: '127.0.0.1'
                        if k == 'server.address' else real(k))
    delivery.remember_chats([{'update_id': n, 'message': {'chat': {
        'id': int(chat), 'type': 'supergroup', 'title': title}}}
        for n, (chat, title) in enumerate(((TRADING, 'IGS trading'), (CALLS, 'IGS calls')))])
    monkeypatch.setattr(delivery, '_client', lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(
            200, json={'ok': True, 'result': []}))))
    app = Path(__file__).resolve().parents[1] / 'src' / 'igs' / 'ui' / 'app.py'
    at = AppTest.from_file(str(app), default_timeout=60).run()
    at.sidebar.radio(key='page').set_value('Settings').run()
    at.button(key='tg_find_chats').click().run()
    assert not at.exception, at.exception
    assert any(str(TRADING) in str(d.value) for d in at.dataframe)    # Find chats lists it
    # The groups the bot has seen are offered by name.
    assert f'IGS trading · supergroup ({TRADING})' in at.selectbox(key='tg_chat_trading').options
    at.button(key='tg_save_chats').click().run()
    errors = [e.value for e in at.error]
    assert any(e.startswith('AI calls and ranking alerts: “IGS calls” isn\'t a chat ID')
               for e in errors)
    assert any(e.startswith('Your Telegram user ID: “+919812345678” isn\'t a chat ID')
               for e in errors)
    assert not Path(os.environ['IGS_ENV_FILE']).exists()             # nothing saved
    at.selectbox(key='tg_chat_calls').set_value(CALLS).run()
    at.selectbox(key='tg_chat_owner').set_value('').run()
    at.button(key='tg_save_chats').click().run()
    assert not at.error, [e.value for e in at.error]
    env = Path(os.environ['IGS_ENV_FILE']).read_text(encoding='utf-8')
    assert f'IGS_TELEGRAM_TRADING_CHAT_ID={TRADING}' in env
    assert f'IGS_TELEGRAM_CALLS_CHAT_ID={CALLS}' in env and 'PAPER' not in env
    assert delivery.chat_id('trading') == TRADING                  # in effect at once
    assert at.selectbox(key='tg_chat_trading').value == TRADING      # shown as saved
    at.selectbox(key='tg_chat_calls').set_value('').run()             # blank: back to main
    at.button(key='tg_save_chats').click().run()
    assert 'CALLS' not in Path(os.environ['IGS_ENV_FILE']).read_text(encoding='utf-8')
    assert delivery.chat_id('calls') == MAIN


@pytest.mark.parametrize('typed, read', [
    ('-1001234567890', '-1001234567890'), (' -100 123 456 7890 ', '-1001234567890'),
    ('\u22121001234567890', '-1001234567890'), ('\uff0d1001234567890', '-1001234567890'),
    ('#-1001234567890', '-1001234567890'),
    ('https://web.telegram.org/a/#-1001234567890', '-1001234567890'),
    ('-1001234567890\tIGS results\tsupergroup', '-1001234567890'),   # a copied row
    ('987654321', '987654321'), ('', ''), ('  ', ''),
    ('IGS results', None), ('+919812345678', None), ('@ananth', None),
    ('https://t.me/+AbCdEf', None), ('-1001234567 or -1002345678', None)])
def test_a_chat_id_is_read_from_what_was_pasted(typed, read):
    assert delivery.parse_chat_id(typed) == read
