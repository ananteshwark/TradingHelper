"""Approval-gated intraday GTT entries with linked stop and target exits.

An attempted submit is committed before the network request. An ambiguous response is
never retried automatically: Upstox GTT placement has no client idempotency key.
"""
from __future__ import annotations

import datetime as dt
import os
from decimal import ROUND_DOWN, Decimal

import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from igs import envfile
from igs.alerts.delivery import send_telegram
from igs.alerts.operations import record_issue
from igs.intraday.engine import trading_window
from igs.intraday.scanner import latest
from igs.timeutil import IST, require_aware, utc_now


class TradeError(RuntimeError):
    pass


class BrokerError(TradeError):
    def __init__(self, message, *, definitive=False, code=None):
        super().__init__(message)
        self.definitive = definitive
        self.code = code


def trading_token():
    """A standard Upstox OAuth access token, separate from the Analytics feed token."""
    path = envfile.default_path()
    values = envfile.parse(path.read_text()) if path.is_file() else {}
    return values.get('UPSTOX_TRADING_TOKEN') or os.environ.get('UPSTOX_TRADING_TOKEN', '')


class Broker:
    def __init__(self, access_token, *, transport=None):
        self.client = httpx.Client(base_url='https://api.upstox.com', timeout=12,
            headers={'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'},
            transport=transport)

    def close(self):
        self.client.close()

    def _request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise BrokerError('Upstox connection failed; order status requires '
                              'manual review') from None
        if not 200 <= response.status_code < 300:
            try:
                errors = response.json().get('errors', [])
                code = errors[0].get('errorCode') if errors else None
            except (ValueError, TypeError, AttributeError, IndexError):
                code = None
            if code == 'UDAPI100067':
                message = ('Upstox rejected the order: this is a read-only Analytics token. '
                           'Save a standard trading OAuth access token in Intraday settings.')
            elif code == 'UDAPI1154':
                message = 'Upstox rejected the order: this server IP is not registered.'
            elif response.status_code in (401, 403):
                message = 'Upstox rejected access; renew the trading OAuth token.'
            else:
                message = f'Upstox returned HTTP {response.status_code}'
                if isinstance(code, str) and code.startswith('UDAPI') and code[5:].isdigit():
                    message += f' ({code})'
            raise BrokerError(message, definitive=(400 <= response.status_code < 500
                                                   and response.status_code not in (408, 409, 425)),
                              code=code)
        try:
            body = response.json()
            if body['status'] != 'success':
                raise ValueError
            return body['data']
        except (ValueError, KeyError, TypeError):
            raise BrokerError('Upstox returned an invalid response; check its order book') from None

    def ltp(self, instrument_key):
        data = self._request('GET', '/v3/market-quote/ltp',
                             params={'instrument_key': instrument_key})
        try:
            # The response key may be the instrument key or Upstox's trading symbol.
            quote = data.get(instrument_key) or next(iter(data.values()))
            price = Decimal(str(quote['last_price']))
            if price <= 0:
                raise ValueError
            return price
        except (ValueError, KeyError, TypeError, StopIteration):
            raise BrokerError('Upstox returned an invalid live price') from None

    def place(self, payload):
        data = self._request('POST', '/v3/order/gtt/place', json=payload)
        try:
            order_id = data['gtt_order_ids'][0]
            if not isinstance(order_id, str) or not order_id:
                raise ValueError
            return order_id
        except (ValueError, KeyError, TypeError, IndexError):
            raise BrokerError('Upstox response lacked a GTT ID; check its order book') from None

    def details(self, order_id):
        data = self._request('GET', '/v3/order/gtt', params={'gtt_order_id': order_id})
        if not isinstance(data, list) or not data:
            raise BrokerError('Upstox returned no GTT details; check its order book')
        return data[0]

    def cancel(self, order_id):
        return self._request('DELETE', '/v3/order/gtt/cancel', json={'gtt_order_id': order_id})


def settings(conn):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('select * from intraday_trading_settings where singleton=true')
        return cur.fetchone()


def _money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_DOWN)


def _plan(signal, ltp, cfg):
    result = signal['result']
    action = result['action']
    reference, stop, target = (_money(result[k]) for k in ('reference', 'stop', 'target'))
    price = _money(ltp)
    if action not in ('buy', 'sell') or min(price, reference, stop, target) <= 0:
        raise TradeError('Invalid call levels')
    if abs(price-reference) * 100 / reference > cfg['max_price_deviation_pct']:
        raise TradeError('Live price moved too far from the call reference')
    if not ((stop < price < target) if action == 'buy' else (target < price < stop)):
        raise TradeError('Stop or target is invalid at the live price')
    quantity = int(cfg['max_trade_rupees'] // price)
    if quantity < 1:
        raise TradeError('One share exceeds the configured per-trade amount')
    payload = {'type': 'MULTIPLE', 'quantity': quantity, 'product': 'I',
               'instrument_token': signal['instrument_key'],
               'transaction_type': action.upper(), 'rules': [
                   {'strategy': 'ENTRY', 'trigger_type': 'IMMEDIATE',
                    'trigger_price': float(price)},
                   {'strategy': 'TARGET', 'trigger_type': 'IMMEDIATE',
                    'trigger_price': float(target)},
                   {'strategy': 'STOPLOSS', 'trigger_type': 'IMMEDIATE',
                    'trigger_price': float(stop)}]}
    return quantity, price, stop, target, payload


def approve(conn, company_id, *, source, telegram_message_id=None, telegram_update_id=None,
            broker=None, clock=utc_now, notify=send_telegram):
    """Approve one current call. Returns (trade_id, status). Never auto-retries a submit."""
    if source not in ('admin', 'telegram'):
        raise ValueError('Unknown approval source')
    if source == 'telegram' and type(telegram_update_id) is not int:
        raise ValueError('Telegram approval update ID is required')
    now = require_aware(clock())
    if not trading_window(now):
        raise TradeError('Outside the intraday entry window')
    access_token = trading_token()
    if not access_token and broker is None:
        raise TradeError('Upstox trading OAuth token is missing; save it in Intraday settings')
    own_broker = broker is None
    broker = broker or Broker(access_token)
    try:
        # Read live price before taking the database lock, then validate the call again.
        run, signals = latest(conn)
        signal = next((s for s in signals if s['company_id'] == company_id), None)
        if not run or run['status'] != 'complete' or not signal:
            raise TradeError('The latest scan has no completed call for this stock')
        quoted_key = signal['instrument_key']
        ltp = broker.ltp(quoted_key)
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute('select * from intraday_trading_settings where singleton=true '
                            'for update')
                cfg = cur.fetchone()
            if not cfg['enabled']:
                raise TradeError('Live intraday trading is disabled in settings')
            run, signals = latest(conn)
            signal = next((s for s in signals if s['company_id'] == company_id), None)
            if not run or run['status'] != 'complete' or not signal:
                raise TradeError('The call changed or its scan is incomplete')
            if signal['instrument_key'] != quoted_key:
                raise TradeError('The instrument changed while checking its price')
            result = signal['result']
            expires = require_aware(dt.datetime.fromisoformat(result['expires_at']))
            candle_end = require_aware(dt.datetime.fromisoformat(result['candle_end']))
            if (result['action'] not in ('buy', 'sell') or now >= expires
                    or now-candle_end > dt.timedelta(minutes=5)
                    or signal['observed_at'] > now
                    or candle_end.astimezone(IST).date() != now.astimezone(IST).date()):
                raise TradeError('The call expired or is no longer fresh')
            day = now.astimezone(IST).date()
            if source == 'telegram':
                if conn.execute('select 1 from intraday_trade where telegram_update_id=%s',
                                (telegram_update_id,)).fetchone():
                    raise TradeError('This Telegram approval was already processed')
                receipt = conn.execute('''select result,expires_at from intraday_telegram
                    where company_id=%s and trading_day=%s and action=%s and status='sent'
                    and telegram_message_id=%s''',
                    (company_id, day, result['action'], telegram_message_id)).fetchone()
                levels = ('candle_end', 'reference', 'stop', 'target')
                if (not receipt or receipt[1] <= now or
                        any(receipt[0].get(k) != result.get(k) for k in levels)):
                    raise TradeError('Approval does not match the current Telegram call')
            if conn.execute("""select 1 from intraday_trade where company_id=%s
                and trading_day=%s and status<>'rejected'""",
                            (company_id, day)).fetchone():
                raise TradeError('This stock already has an intraday order today')
            count, spent = conn.execute('''select count(*),coalesce(sum(notional),0)
                from intraday_trade where trading_day=%s
                and status not in ('rejected','expired')''',
                (day,)).fetchone()
            quantity, entry, stop, target, payload = _plan(signal, ltp, cfg)
            notional = quantity * entry
            if count >= cfg['max_daily_trades'] or spent+notional > cfg['max_daily_rupees']:
                raise TradeError('Daily intraday trade count or gross value limit reached')
            trade_id = conn.execute('''insert into intraday_trade(company_id,trading_day,action,
                scan_id,symbol,instrument_key,approved_by,approved_at,expires_at,quantity,
                entry_price,stop_price,target_price,notional,status,telegram_update_id)
                values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'submitting',%s)
                returning trade_id''', (company_id, day, result['action'], run['scan_id'],
                signal['symbol'], signal['instrument_key'], source, now, expires, quantity,
                entry, stop, target, notional, telegram_update_id)).fetchone()[0]
        conn.commit()  # Durable reservation BEFORE the broker request.

        def notify_status(message):
            try:
                if notify(message) is False:
                    record_issue('intraday-trade', 'NotificationFailed', event_id=str(trade_id))
            except Exception:  # noqa: BLE001 - order state is already durable
                record_issue('intraday-trade', 'NotificationFailed', event_id=str(trade_id))

        try:
            gtt_id = broker.place(payload)
        except BrokerError as exc:
            # Explicit 4xx rejection means no order; a timeout/5xx can be ambiguous.
            status = 'rejected' if exc.definitive else 'uncertain'
            conn.execute('update intraday_trade set status=%s,last_error=%s '
                         'where trade_id=%s', (status, str(exc), trade_id))
            if exc.code == 'UDAPI100067':
                conn.execute('update intraday_trading_settings set enabled=false '
                             'where singleton=true')
            conn.commit()
            record_issue('intraday-trade', 'SubmissionRejected' if exc.definitive else
                         'SubmissionUncertain', event_id=str(trade_id))
            if exc.definitive:
                notify_status(f'Intraday {signal["symbol"]}: no order placed. {exc} '
                              f'Trade #{trade_id} rejected.')
            else:
                notify_status(f'Intraday {signal["symbol"]}: Upstox submission uncertain. '
                              f'Check Upstox GTT/order book. Trade #{trade_id} '
                              'will not be retried.')
            return trade_id, status
        conn.execute("update intraday_trade set status='submitted',gtt_order_id=%s "
                     'where trade_id=%s', (gtt_id, trade_id))
        conn.commit()
        notify_status(f'Intraday {result["action"].upper()} {signal["symbol"]}: Upstox GTT '
                      f'{gtt_id} accepted for {quantity} shares at limit ₹{entry}; '
                      f'stop ₹{stop}, target ₹{target}. Entry fill is pending. '
                      f'Trade #{trade_id}.')
        return trade_id, 'submitted'
    finally:
        if own_broker:
            broker.close()


def reconcile(conn, *, broker=None, clock=utc_now, notify=send_telegram):
    """Cancel stale unfilled entries and report entry/exit states; never cancel filled exits."""
    now = require_aware(clock())
    abandoned = conn.execute("""update intraday_trade set status='uncertain',
        last_error='Submission worker stopped before recording the Upstox response'
        where status='submitting' and approved_at<%s
        returning trade_id,symbol""", (now-dt.timedelta(seconds=30),)).fetchall()
    conn.commit()
    for trade_id, symbol in abandoned:
        record_issue('intraday-trade', 'SubmissionUncertain', event_id=str(trade_id))
        notify(f'Intraday {symbol}: order submission was interrupted. Check Upstox '
               f'GTT/order book. Trade #{trade_id} will not be retried.')
    access_token = trading_token()
    if not access_token and broker is None:
        return len(abandoned)
    own_broker = broker is None
    broker = broker or Broker(access_token)
    changed = len(abandoned)
    try:
        rows = conn.execute("""select trade_id,gtt_order_id,expires_at,symbol,status from
            intraday_trade where gtt_order_id is not null and status in
            ('submitted','entry_open','entry_filled','exit_unprotected','cancel_uncertain')
            order by trade_id""").fetchall()
        for trade_id, gtt_id, expires, symbol, prior in rows:
            try:
                detail = broker.details(gtt_id)
                rules = {r.get('strategy'): r for r in detail.get('rules', [])}
                entry = rules.get('ENTRY', {}).get('status', '').upper()
                exits = {k: rules.get(k, {}).get('status', '').upper()
                         for k in ('TARGET', 'STOPLOSS')}
                if any(x == 'COMPLETED' for x in exits.values()):
                    state = 'closed'
                elif entry == 'COMPLETED' and any(
                        x in ('FAILED','CANCELLED','EXPIRED') for x in exits.values()):
                    state = 'exit_unprotected'
                elif entry == 'COMPLETED' or any(x in ('OPEN','TRIGGERED','SCHEDULED')
                                                  for x in exits.values()):
                    state = 'entry_filled'
                elif entry in ('CANCELLED','EXPIRED','FAILED'):
                    state = 'expired'
                else:
                    state = 'entry_open'
                if now >= expires and state == 'entry_open':
                    try:
                        broker.cancel(gtt_id)
                        detail = broker.details(gtt_id)
                        checked = {r.get('strategy'): r.get('status', '').upper()
                                   for r in detail.get('rules', [])}
                        if checked.get('ENTRY') == 'COMPLETED':
                            state = 'exit_unprotected' if any(
                                checked.get(k) in ('FAILED','CANCELLED','EXPIRED')
                                for k in ('TARGET','STOPLOSS')) else 'entry_filled'
                        else:
                            state = 'expired'
                    except BrokerError:
                        state = 'cancel_uncertain'
                        record_issue('intraday-trade', 'CancelUncertain', event_id=str(trade_id))
                conn.execute('''update intraday_trade set status=%s,broker_state=%s,
                    last_checked_at=%s where trade_id=%s''',
                    (state, Jsonb(detail), now, trade_id))
                conn.commit()
                if state != prior:
                    changed += 1
                    if state == 'exit_unprotected':
                        record_issue('intraday-trade', 'ExitUnprotected', event_id=str(trade_id))
                    if state in ('entry_filled','closed','expired','cancel_uncertain',
                                 'exit_unprotected'):
                        notify(f'Intraday {symbol} · GTT {gtt_id}: {state.replace("_", " ")}. '
                               'Check Upstox positions and orders.')
            except BrokerError:
                record_issue('intraday-trade', 'ReconcileFailed', event_id=str(trade_id))
        return changed
    finally:
        if own_broker:
            broker.close()
