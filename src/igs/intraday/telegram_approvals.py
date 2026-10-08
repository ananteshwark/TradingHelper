"""Poll exact private-chat replies to a sent intraday call; never infer consent."""
from __future__ import annotations

import os

from igs.alerts.delivery import _client, _telegram, send_telegram
from igs.alerts.operations import record_issue
from igs.intraday.trading import TradeError, approve, place_exceptional_volume, reconcile

APPROVAL_WORDS = ('approved', 'approve')
NOT_A_REPLY = ('To approve an intraday call, reply APPROVED to the call message itself '
               '(swipe left on it, or long-press it and choose Reply). Nothing was ordered.')
NOT_A_CALL = ('That message is not an open intraday call: approve the call alert itself, '
              'before the expiry it states. Nothing was ordered.')


def poll(conn, *, client=None, broker=None, clock=None, notify=send_telegram):
    """Approve calls from APPROVED replies in the configured private chat. Every approval
    from that chat gets an answer: the order status, why it was declined, or why it could
    not be read as an approval. Messages from anyone else are ignored without a reply. A
    failure on one reply is reported and skipped, never retried, so it cannot hold up the
    replies after it."""
    token = os.environ.get('IGS_TELEGRAM_TOKEN', '').strip()
    chat = os.environ.get('IGS_TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        return 0
    own_client = client is None
    client = client or _client()

    def say(text):
        try:
            notify(text)
        except Exception:  # noqa: BLE001 - the reply outcome is already durable
            record_issue('intraday-approvals', 'NotificationFailed')

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
            sender = msg.get('chat') or {}
            reply = msg.get('reply_to_message') or {}
            text = (msg.get('text') or '').strip().casefold().rstrip('.!')
            mine = (str(sender.get('id')) == chat and sender.get('type') == 'private'
                    and (msg.get('from') or {}).get('id') == sender.get('id'))
            if mine and text in APPROVAL_WORDS:
                row = None
                if type(reply.get('message_id')) is int:
                    row = conn.execute('''select company_id from intraday_telegram
                        where telegram_message_id=%s and status='sent' ''',
                        (reply['message_id'],)).fetchone()
                if type(reply.get('message_id')) is not int:
                    say(NOT_A_REPLY)
                elif not row:
                    say(NOT_A_CALL)
                else:
                    try:
                        opts = {'source': 'telegram', 'telegram_message_id': reply['message_id'],
                                'telegram_update_id': update_id, 'broker': broker,
                                'notify': notify}
                        if clock is not None:
                            opts['clock'] = clock
                        approve(conn, row[0], **opts)
                        processed += 1
                    except TradeError as exc:
                        say(f'Intraday approval declined: {exc}')
                    except Exception as exc:  # noqa: BLE001 - see the docstring
                        conn.rollback()
                        record_issue('intraday-approvals', type(exc).__name__,
                                     event_id=str(update_id))
                        say(f'Intraday approval failed ({type(exc).__name__}) and will not '
                            'be retried. Check the Intraday page and the Upstox order book '
                            'before approving again.')
            conn.execute('update intraday_telegram_cursor set next_update_id=%s '
                         'where singleton=true', (update_id + 1,))
            conn.commit()
            offset = update_id + 1
        return processed
    finally:
        if own_client:
            client.close()


def run():
    """Automatic placement, Telegram approvals and order reconciliation are independent:
    a failure in one is reported and the others still run."""
    from igs.db import connect
    counts = {}
    with connect() as conn:
        for name, label, step in (('automatic', 'intraday-auto', place_exceptional_volume),
                                  ('approvals', 'intraday-approvals', poll),
                                  ('reconciled', 'intraday-reconcile', reconcile)):
            try:
                counts[name] = step(conn)
            except Exception as exc:  # noqa: BLE001 - reported; the next step still runs
                conn.rollback()
                record_issue(label, type(exc).__name__)
                print(f'{name} failed: {type(exc).__name__}')
                counts[name] = 0
    print(f"Processed {counts['approvals']} intraday approvals and "
          f"{counts['automatic']} automatic attempts")
    return 0
