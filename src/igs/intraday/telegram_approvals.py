"""Poll exact private-chat replies to a sent intraday call; never infer consent."""
from __future__ import annotations

import os

from igs.alerts.delivery import _client, _telegram, send_telegram
from igs.alerts.operations import record_issue
from igs.intraday.trading import TradeError, approve, reconcile


def poll(conn, *, client=None, broker=None, clock=None, notify=send_telegram):
    token = os.environ.get('IGS_TELEGRAM_TOKEN', '').strip()
    chat = os.environ.get('IGS_TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        return 0
    own_client = client is None
    client = client or _client()
    try:
        webhook = _telegram('getWebhookInfo', token, client).get('result', {})
        if webhook.get('url'):
            record_issue('intraday-approvals', 'TelegramWebhookConflict')
            return 0
        offset = conn.execute('select next_update_id from intraday_telegram_cursor '
                              'where singleton=true').fetchone()[0]
        updates = _telegram('getUpdates', token, client, offset=str(offset),
                            limit='100', timeout='0', allowed_updates='["message"]').get(
                                'result', [])
        processed = 0
        for update in updates:
            update_id = update.get('update_id')
            if type(update_id) is not int or update_id < offset:
                continue
            msg = update.get('message') or {}
            reply = msg.get('reply_to_message') or {}
            if (msg.get('text', '').strip().casefold() == 'approved'
                    and str((msg.get('chat') or {}).get('id')) == chat
                    and (msg.get('chat') or {}).get('type') == 'private'
                    and (msg.get('from') or {}).get('id') == (msg.get('chat') or {}).get('id')
                    and type(reply.get('message_id')) is int):
                row = conn.execute('''select company_id from intraday_telegram
                    where telegram_message_id=%s and status='sent' ''',
                    (reply['message_id'],)).fetchone()
                if row:
                    try:
                        opts = {'source': 'telegram', 'telegram_message_id': reply['message_id'],
                                'broker': broker, 'notify': notify}
                        if clock is not None:
                            opts['clock'] = clock
                        approve(conn, row[0], **opts)
                        processed += 1
                    except TradeError as exc:
                        notify(f'Intraday approval declined: {exc}')
            conn.execute('update intraday_telegram_cursor set next_update_id=%s '
                         'where singleton=true', (update_id + 1,))
            conn.commit()
            offset = update_id + 1
        return processed
    finally:
        if own_client:
            client.close()


def run():
    from igs.db import connect
    with connect() as conn:
        count = poll(conn)
        reconcile(conn)
    print(f'Processed {count} intraday approvals')
    return 0
