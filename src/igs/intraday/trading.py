"""Approval-gated intraday GTT entries with linked stop and target exits.

An attempted submit is committed before the network request. An ambiguous response is
never retried automatically: Upstox GTT placement has no client idempotency key.
"""
from __future__ import annotations

import datetime as dt
import os
from decimal import ROUND_DOWN, Decimal, InvalidOperation

import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from igs import envfile
from igs.alerts.delivery import send_telegram
from igs.alerts.operations import record_issue
from igs.intraday import eligibility
from igs.intraday.costs import charges, intraday_rates, net_result, size
from igs.intraday.engine import to_tick, trading_window
from igs.intraday.scanner import open_calls
from igs.timeutil import IST, require_aware, utc_now


class TradeError(RuntimeError):
    pass


AUTO_VOLUME_MULTIPLE = Decimal('50')
# Every order's entry limit, approved or automatic: this % below the call's price for a buy,
# above for a sell.
ENTRY_OFFSET_PCT = Decimal('1')
# Its stop: this % below the entry limit for a buy, above for a sell. The target is the call's.
STOP_PCT = Decimal('1')
ORDER_LEVELS = {'offset_pct': ENTRY_OFFSET_PCT, 'stop_pct': STOP_PCT}


def exceptional_volume(result):
    """Use the unrounded stored candle and baseline volumes, not the display rvol."""
    try:
        candle = Decimal(str(result['candle_volume']))
        baseline = Decimal(str(result['baseline_volume']))
        return (candle.is_finite() and baseline.is_finite() and baseline > 0
                and candle / baseline > AUTO_VOLUME_MULTIPLE)
    except (KeyError, InvalidOperation, TypeError, ValueError, ZeroDivisionError):
        return False


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

    def fill(self, order_id):
        """(average price, filled quantity) of one order, e.g. one a GTT rule placed."""
        data = self._request('GET', '/v2/order/details', params={'order_id': order_id})
        try:
            price = Decimal(str(data['average_price']))
            quantity = int(data['filled_quantity'])
            if not price.is_finite() or price < 0 or quantity < 0:
                raise ValueError
            return price, quantity
        except (ValueError, KeyError, TypeError, InvalidOperation):
            raise BrokerError('Upstox returned an invalid order fill') from None


def settings(conn):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('select * from intraday_trading_settings where singleton=true')
        return cur.fetchone()


def _money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_DOWN)


def _plan(signal, ltp, cfg, tick, rates, *, offset_pct=Decimal(0), stop_pct=Decimal(0)):
    """Order quantity, levels and payload. Every price is a whole number of exchange ticks
    (`tick`, in rupees): a buy rounds down and a sell up, so the entry limit never pays
    more than the call's price, the stop moves away from the entry and the target toward
    it. Levels already on the tick are unchanged.

    With `offset_pct`, the entry limit is that % below the call's price for a buy (above
    for a sell). With `stop_pct`, the stop is that % below the entry limit for a buy (above
    for a sell) instead of the call's. The target stays the call's."""
    result = signal['result']
    action = result['action']
    if action not in ('buy', 'sell') or not tick or Decimal(str(tick)) <= 0:
        raise TradeError('Invalid call levels')
    direction = 1 if action == 'buy' else -1
    reference, stop, target = (to_tick(result[k], tick, direction)
                               for k in ('reference', 'stop', 'target'))
    price = _money(ltp)
    if min(price, reference, stop, target) <= 0:
        raise TradeError('Invalid call levels')
    if abs(price-reference) * 100 / reference > cfg['max_price_deviation_pct']:
        raise TradeError('Live price moved too far from the call reference')
    if not ((stop < price < target) if action == 'buy' else (target < price < stop)):
        raise TradeError('Stop or target is invalid at the live price')
    if not ((stop < reference < target) if action == 'buy' else (target < reference < stop)):
        raise TradeError('Stop or target is invalid at the recommended price')
    # Upstox IMMEDIATE sends a limit order at trigger_price. Keep the call's
    # recommended price fixed; the live quote is only an invalidation check.
    live, price = price, reference
    if offset_pct:
        price = to_tick(reference * (1 - direction * Decimal(offset_pct) / 100), tick,
                        direction)
    if stop_pct:
        stop = to_tick(price * (1 - direction * Decimal(stop_pct) / 100), tick, direction)
    if min(price, stop) <= 0 or not (
            (stop < price < target) if action == 'buy' else (target < price < stop)):
        raise TradeError('Stop or target is invalid at the order price')
    if not ((stop < live) if action == 'buy' else (live < stop)):
        raise TradeError('The live price is already past the stop')
    # Size by both caps: the order value, and what the stop would lose.
    risk_per_share = abs(price - stop)
    quantity = size(price, stop, cfg)
    if quantity < 1:
        raise TradeError('One share exceeds the amount per trade or the maximum loss per '
                         'trade')
    loss, gain = quantity * risk_per_share, quantity * abs(target - price)
    to_target = charges(quantity, price, target, action, rates)
    net_reward, net_risk = gain - to_target, loss + charges(quantity, price, stop, action, rates)
    floor = Decimal(str(cfg['min_net_reward_risk']))
    if net_reward < floor * net_risk:
        raise TradeError(f'Estimated charges of ₹{to_target} leave the target earning '
                         f'{net_reward / net_risk:.2f}× what the stop loses (minimum '
                         f'{floor:g}×). Raise the amount per trade, or skip this call.')
    payload = {'type': 'MULTIPLE', 'quantity': quantity, 'product': 'I',
               'instrument_token': signal['instrument_key'],
               'transaction_type': action.upper(), 'rules': [
                   {'strategy': 'ENTRY', 'trigger_type': 'IMMEDIATE',
                    'trigger_price': float(price)},
                   {'strategy': 'TARGET', 'trigger_type': 'IMMEDIATE',
                    'trigger_price': float(target)},
                   {'strategy': 'STOPLOSS', 'trigger_type': 'IMMEDIATE',
                    'trigger_price': float(stop)}]}
    return quantity, price, stop, target, payload, loss, to_target


def order_check(result, instrument_key, tick, cfg, rates):
    """The order an approval, or the automatic placement, would send for a call under the
    trading settings `cfg`, with the live price at the call's own; or why there would be
    none.

    {'ok': True, 'automatic', 'quantity', 'entry', 'stop', 'target', 'net_gain',
    'net_loss', 'reward_risk'}: rupees after estimated charges. {'ok': False, 'reason'}.
    Every order has the same levels (ORDER_LEVELS); a call is held to the automatic
    minimum reward-to-risk when automatic placement is on and its volume jump is above
    50×."""
    automatic = bool(cfg['enabled'] and cfg['auto_high_volume_enabled']
                     and exceptional_volume(result))
    if automatic:
        cfg = {**cfg, 'min_net_reward_risk': cfg['auto_min_net_reward_risk']}
    try:
        quantity, entry, stop, target, _, loss, to_target = _plan(
            {'result': result, 'instrument_key': instrument_key},
            Decimal(str(result['reference'])), cfg, tick, rates, **ORDER_LEVELS)
    except (TradeError, KeyError, InvalidOperation) as exc:
        return {'ok': False, 'automatic': automatic, 'reason': str(exc)}
    gain = quantity * abs(target - entry) - to_target
    lost = loss + charges(quantity, entry, stop, result['action'], rates)
    return {'ok': True, 'automatic': automatic, 'quantity': quantity, 'entry': float(entry),
            'stop': float(stop), 'target': float(target), 'net_gain': float(gain),
            'net_loss': float(lost), 'reward_risk': round(float(gain / lost), 2)}


LEVELS = ('action', 'candle_end', 'reference', 'stop', 'target')


def _call(conn, company_id, now, *, telegram_message_id=None, expected_call=None):
    """The call being approved: one of the stock's open calls (scanner.open_calls), the one
    a Telegram reply answered or the page showed, otherwise the newest. A call stays open
    until its stated expiry, so an approval no longer races the next scan."""
    calls = open_calls(conn, now, company_id).get(company_id, ([], None))[0]
    wanted = expected_call
    if telegram_message_id is not None:
        receipt = conn.execute('''select result from intraday_telegram where company_id=%s
            and trading_day=%s and status='sent' and telegram_message_id=%s
            and expires_at>%s''', (company_id, now.astimezone(IST).date(),
                                     telegram_message_id, now)).fetchone()
        if not receipt:
            raise TradeError('Approval does not match the current Telegram call')
        wanted = receipt[0]
    if wanted is None:
        if not calls:
            raise TradeError('No open call for this stock: it expired, reversed or met '
                             'opposing news')
        return calls[0]
    match = next((c for c in calls if all(c['result'].get(k) == wanted.get(k)
                                          for k in LEVELS)), None)
    if match is None:
        raise TradeError('Approval does not match the current Telegram call'
                         if telegram_message_id is not None else
                         'The displayed call changed; refresh and review it again')
    return match


def approve(conn, company_id, *, source, telegram_message_id=None, telegram_update_id=None,
            broker=None, clock=utc_now, notify=send_telegram, expected_call=None):
    """Submit one current call. Returns (trade_id, status). Never auto-retries a submit."""
    if source not in ('admin', 'telegram', 'auto'):
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
        signal = _call(conn, company_id, now, telegram_message_id=telegram_message_id,
                       expected_call=expected_call)
        quoted_key = signal['instrument_key']
        try:
            ticks = eligibility.tick_sizes(now=now, force=True)
        except eligibility.FeedError as exc:
            raise TradeError(str(exc)) from None
        if quoted_key not in ticks:
            raise TradeError('Upstox does not currently allow intraday trading for this stock')
        ltp = broker.ltp(quoted_key)
        now = require_aware(clock())
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute('select * from intraday_trading_settings where singleton=true '
                            'for update')
                cfg = cur.fetchone()
            if not cfg['enabled']:
                raise TradeError('Live intraday trading is disabled in settings')
            if source == 'auto' and not cfg['auto_high_volume_enabled']:
                raise TradeError('Automatic high-volume trading is disabled in settings')
            if not trading_window(now):
                raise TradeError('Outside the intraday entry window')
            if source == 'telegram' and conn.execute(
                    'select 1 from intraday_trade where telegram_update_id=%s',
                    (telegram_update_id,)).fetchone():
                raise TradeError('This Telegram approval was already processed')
            signal = _call(conn, company_id, now, telegram_message_id=telegram_message_id,
                           expected_call=expected_call)
            if signal['instrument_key'] != quoted_key:
                raise TradeError('The instrument changed while checking its price')
            result = signal['result']
            if source == 'auto' and not exceptional_volume(result):
                raise TradeError('The call no longer has a volume jump above 50×')
            expires = require_aware(dt.datetime.fromisoformat(result['expires_at']))
            day = now.astimezone(IST).date()
            if conn.execute("""select 1 from intraday_trade where company_id=%s
                and trading_day=%s and status<>'rejected'""",
                            (company_id, day)).fetchone():
                raise TradeError('This stock already has an intraday order today')
            count, spent = conn.execute('''select count(*),coalesce(sum(notional),0)
                from intraday_trade where trading_day=%s
                and status not in ('rejected','expired')''',
                (day,)).fetchone()
            if source == 'auto':     # its own minimum reward-to-risk after charges
                cfg = {**cfg, 'min_net_reward_risk': cfg['auto_min_net_reward_risk']}
            quantity, entry, stop, target, payload, risk, est = _plan(
                signal, ltp, cfg, ticks[quoted_key], intraday_rates(), **ORDER_LEVELS)
            notional = quantity * entry
            if count >= cfg['max_daily_trades'] or spent+notional > cfg['max_daily_rupees']:
                raise TradeError('Daily intraday trade count or gross value limit reached')
            trade_id = conn.execute('''insert into intraday_trade(company_id,trading_day,action,
                scan_id,symbol,instrument_key,approved_by,approved_at,expires_at,quantity,
                entry_price,stop_price,target_price,notional,status,telegram_update_id,
                risk_rupees,est_charges)
                values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'submitting',%s,%s,%s)
                returning trade_id''', (company_id, day, result['action'], signal['scan_id'],
                signal['symbol'], signal['instrument_key'], source, now, expires, quantity,
                entry, stop, target, notional, telegram_update_id, risk, est)).fetchone()[0]
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
        prefix = 'Automatic intraday' if source == 'auto' else 'Intraday'
        side = 'below' if result['action'] == 'buy' else 'above'
        limit = f'₹{entry} ({ENTRY_OFFSET_PCT:g}% {side} the call\'s price)'
        exits = (f'stop ₹{stop} ({STOP_PCT:g}% {side} the limit), target ₹{target} '
                 "(the call's)")
        notify_status(f'{prefix} {result["action"].upper()} {signal["symbol"]}: Upstox GTT '
                      f'{gtt_id} accepted for {quantity} shares at limit {limit}; '
                      f'{exits}. Loss at the stop ₹{risk:.2f}, '
                      f'estimated charges ₹{est}. Entry fill is pending. '
                      f'Trade #{trade_id}.')
        return trade_id, 'submitted'
    finally:
        if own_broker:
            broker.close()


def place_exceptional_volume(conn, *, broker=None, clock=utc_now, notify=send_telegram):
    """Attempt at most one fresh >50× call per run through the ordinary order safeguards."""
    now = require_aware(clock())
    if not trading_window(now):
        return 0
    cfg = settings(conn)
    if not cfg or not cfg['enabled'] or not cfg['auto_high_volume_enabled']:
        return 0
    if broker is None and not trading_token():
        return 0
    day = now.astimezone(IST).date()
    for company_id, (calls, _) in open_calls(conn, now).items():
        if conn.execute('select 1 from intraday_trade where company_id=%s and trading_day=%s',
                        (company_id, day)).fetchone():
            continue  # Includes rejected submissions: never retry an automatic order.
        for signal in calls:
            if not exceptional_volume(signal['result']):
                continue
            try:
                approve(conn, company_id, source='auto', expected_call=signal['result'],
                        broker=broker, clock=clock, notify=notify)
            except TradeError:
                conn.rollback()  # This call failed a regular validation; try another stock.
                continue
            return 1
    return 0


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
        record_pnl(conn, broker, now, notify)
        return changed
    finally:
        if own_broker:
            broker.close()


PNL_RETRY = dt.timedelta(minutes=5)
PNL_DAYS = 3


def record_pnl(conn, broker, now, notify):
    """Net P&L of closed trades not yet priced: the average fills of the entry order and
    of the exit order that closed the trade (the orders its GTT rules placed, read from
    Upstox), less charges estimated at those fills. A trade closed some other way, or
    with unequal fills, gets a note instead. An Upstox failure is retried five minutes
    later, for three days."""
    day = now.astimezone(IST).date()
    rows = conn.execute("""select trade_id,symbol,action,broker_state from intraday_trade
        where status='closed' and net_pnl is null and pnl_note is null and trading_day>=%s
          and (pnl_checked_at is null or pnl_checked_at<%s) order by trade_id""",
        (day - dt.timedelta(days=PNL_DAYS), now - PNL_RETRY)).fetchall()
    for trade_id, symbol, action, state in rows:
        rules = {r.get('strategy'): r for r in (state or {}).get('rules', [])}
        entry_order = rules.get('ENTRY', {}).get('order_id')
        exits = [rules[k].get('order_id') for k in ('TARGET', 'STOPLOSS')
                 if str(rules.get(k, {}).get('status', '')).upper() == 'COMPLETED']
        note = None
        if not entry_order or len(exits) != 1 or not exits[0]:
            note = 'Upstox shows no single exit order for this GTT; see its order book'
        else:
            try:
                entry, bought = broker.fill(entry_order)
                exit_price, sold = broker.fill(exits[0])
            except BrokerError:
                conn.execute('update intraday_trade set pnl_checked_at=%s where trade_id=%s',
                             (now, trade_id))
                conn.commit()
                record_issue('intraday-trade', 'PnlUnavailable', event_id=str(trade_id))
                continue
            if bought != sold or bought < 1:
                note = f'Entry filled {bought} shares and the exit {sold}; see Upstox'
        if note:
            conn.execute('update intraday_trade set pnl_note=%s,pnl_checked_at=%s '
                         'where trade_id=%s', (note, now, trade_id))
            conn.commit()
            record_issue('intraday-trade', 'PnlUnmatched', event_id=str(trade_id))
            continue
        gross, cost, net = net_result(bought, entry, exit_price, action, intraday_rates())
        conn.execute('''update intraday_trade set entry_fill=%s,exit_fill=%s,
            filled_quantity=%s,gross_pnl=%s,charges=%s,net_pnl=%s,pnl_checked_at=%s
            where trade_id=%s''', (entry, exit_price, bought, gross, cost, net, now, trade_id))
        conn.commit()
        notify(f'Intraday {symbol} closed: net P&L ₹{net} after estimated charges ₹{cost} '
               f'(gross ₹{gross}; {bought} shares, entry ₹{entry}, exit ₹{exit_price}). '
               f'Trade #{trade_id}.')
