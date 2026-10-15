"""Approval-gated intraday GTT entries with linked stop and target exits, and the ML
intraday calls' orders, with a stop and the app's own square-off at 15:00 (approve_ml).

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
# Every order's levels, approved or automatic, are trading settings (order_levels); these
# are their defaults. The entry limit: entry_offset_pct below the call's price for a buy,
# above for a sell (0: the call's price).
ENTRY_OFFSET_PCT = Decimal('1')
# Its stop: stop_pct below the entry limit for a buy, above for a sell (0: the call's stop).
# The target is the call's.
STOP_PCT = Decimal('1')
# An unfilled entry is cancelled order_minutes after it is placed, and by 15:15 IST.
ORDER_MINUTES = 10
LAST_ENTRY = dt.time(15, 15)
# Upstox order statuses after which an order can no longer fill.
ORDER_DONE = frozenset({'complete', 'cancelled', 'rejected', 'cancelled after market order'})

# The ML calls' orders (research/ml_live). Approvals close at 10:00 IST: entries later than
# that were not tested. The entry is a limit ML_ALLOWANCE_PCT past the live price, so it
# fills at once like the market entry tested, but no further away. The stop is half the
# 14-day ATR from the live price (research/winners). There is no target: the app squares
# the position off at 15:00, before Upstox's own square-off at 15:10 for stocks in NSE's
# closing auction, after which it refuses intraday orders in them (15:09).
ML_APPROVE_FROM, ML_APPROVE_UNTIL = dt.time(9, 45), dt.time(10, 0)
ML_SQUARE_OFF, ML_LAST_ORDER = dt.time(15, 0), dt.time(15, 9)
ML_ALLOWANCE_PCT = Decimal('0.1')
ML_STOP_ATR = Decimal('0.5')
# Below about this order value, charges take more than the ML calls' tested edge
# (docs/WINNING_TRADERS.md, trade size).
ML_MIN_USEFUL_RUPEES = 50000


def order_levels(cfg):
    """_plan's offset_pct and stop_pct under the trading settings `cfg`."""
    return {'offset_pct': Decimal(str(cfg.get('entry_offset_pct', ENTRY_OFFSET_PCT))),
            'stop_pct': Decimal(str(cfg.get('stop_pct', STOP_PCT)))}


def order_expiry(placed, cfg):
    """When an entry placed at `placed` is cancelled if still unfilled."""
    placed = require_aware(placed)
    last = dt.datetime.combine(placed.astimezone(IST).date(), LAST_ENTRY, IST)
    return min(placed + dt.timedelta(minutes=int(cfg.get('order_minutes', ORDER_MINUTES))),
               last)


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
                           'Save a standard trading OAuth access token in Settings, Upstox tab.')
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

    def cancel_order(self, order_id):
        """Cancel one open exchange order, such as the entry order a GTT rule placed."""
        return self._request('DELETE', 'https://api-hft.upstox.com/v3/order/cancel',
                             params={'order_id': order_id})

    def order_state(self, order_id):
        """(status, filled quantity) of one order; the status lower-case, as Upstox's
        order status list writes it ('open', 'complete', 'cancelled', ...)."""
        data = self._request('GET', '/v2/order/details', params={'order_id': order_id})
        try:
            status, quantity = str(data['status']).strip().lower(), int(data['filled_quantity'])
            if not status or quantity < 0:
                raise ValueError
            return status, quantity
        except (ValueError, KeyError, TypeError):
            raise BrokerError('Upstox returned an invalid order status') from None

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

    def square_off(self, instrument_key, quantity, action):
        """A market order closing `quantity` shares of an intraday position entered with
        `action` ('buy' is closed by a sale). Returns its order ID."""
        data = self._request('POST', 'https://api-hft.upstox.com/v3/order/place', json={
            'quantity': int(quantity), 'product': 'I', 'validity': 'DAY', 'price': 0,
            'tag': 'igs-ml-exit', 'instrument_token': instrument_key, 'order_type': 'MARKET',
            'transaction_type': 'SELL' if action == 'buy' else 'BUY',
            'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False, 'slice': False})
        try:
            ids = data.get('order_ids') or [data.get('order_id')]
            if not isinstance(ids[0], str) or not ids[0]:
                raise ValueError
            return ids[0]
        except (ValueError, AttributeError, TypeError, IndexError):
            raise BrokerError('Upstox response lacked an order ID; check its order book') from None


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
    Every order has the levels of the settings (order_levels); a call is held to the
    automatic minimum reward-to-risk when automatic placement is on and its volume jump is
    above 50×."""
    automatic = bool(cfg['enabled'] and cfg['auto_high_volume_enabled']
                     and exceptional_volume(result))
    if automatic:
        cfg = {**cfg, 'min_net_reward_risk': cfg['auto_min_net_reward_risk']}
    try:
        quantity, entry, stop, target, _, loss, to_target = _plan(
            {'result': result, 'instrument_key': instrument_key},
            Decimal(str(result['reference'])), cfg, tick, rates, **order_levels(cfg))
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
        raise TradeError('Upstox trading OAuth token is missing; save it in Settings, Upstox tab')
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
            if not cfg['rule_calls_enabled']:
                raise TradeError('Rule-based intraday calls are switched off in settings')
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
            # The order's own time, not what is left of the call's: a call approved near
            # its expiry still gets the full time to fill.
            expires = order_expiry(now, cfg)
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
                signal, ltp, cfg, ticks[quoted_key], intraday_rates(), **order_levels(cfg))
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
        notify_status = _notifier(notify, trade_id)
        status, gtt_id = _place(conn, broker, payload, trade_id, signal['symbol'],
                                notify_status)
        if gtt_id is None:
            return trade_id, status
        prefix = 'Automatic intraday' if source == 'auto' else 'Intraday'
        side = 'below' if result['action'] == 'buy' else 'above'
        levels = order_levels(cfg)
        offset, stop_pct = float(levels['offset_pct']), float(levels['stop_pct'])
        limit = (f'₹{entry} ({offset:g}% {side} the call\'s price)' if offset
                 else f"₹{entry} (the call's price)")
        exits = (f'stop ₹{stop} ({stop_pct:g}% {side} the limit)' if stop_pct
                 else f"stop ₹{stop} (the call's)") + f", target ₹{target} (the call's)"
        notify_status(f'{prefix} {result["action"].upper()} {signal["symbol"]}: Upstox GTT '
                      f'{gtt_id} accepted for {quantity} shares at limit {limit}; '
                      f'{exits}. Loss at the stop ₹{risk:.2f}, '
                      f'estimated charges ₹{est}. Entry fill is pending; unfilled, it is '
                      f'cancelled at {expires.astimezone(IST):%H:%M} IST. '
                      f'Trade #{trade_id}.')
        return trade_id, 'submitted'
    finally:
        if own_broker:
            broker.close()


def _notifier(notify, trade_id):
    def notify_status(message):
        try:
            if notify(message) is False:
                record_issue('intraday-trade', 'NotificationFailed', event_id=str(trade_id))
        except Exception:  # noqa: BLE001 - order state is already durable
            record_issue('intraday-trade', 'NotificationFailed', event_id=str(trade_id))
    return notify_status


def _place(conn, broker, payload, trade_id, symbol, notify_status):
    """Send a reserved trade's GTT: ('submitted', its ID), or ('rejected' | 'uncertain',
    None). Never retried."""
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
            notify_status(f'Intraday {symbol}: no order placed. {exc} '
                          f'Trade #{trade_id} rejected.')
        else:
            notify_status(f'Intraday {symbol}: Upstox submission uncertain. '
                          f'Check Upstox GTT/order book. Trade #{trade_id} '
                          'will not be retried.')
        return status, None
    conn.execute("update intraday_trade set status='submitted',gtt_order_id=%s "
                 'where trade_id=%s', (gtt_id, trade_id))
    conn.commit()
    return 'submitted', gtt_id


# --------------------------------------------------------------------------- ML calls


def ml_window(now):
    """Whether an ML call (igs.intraday.ml) can be approved now."""
    local = require_aware(now).astimezone(IST)
    return local.weekday() < 5 and ML_APPROVE_FROM <= local.time() < ML_APPROVE_UNTIL


def ml_plan(pick, ltp, cfg, tick, rates):
    """The order for an ML call at the live price `ltp`: (quantity, entry limit, stop,
    payload, loss at the stop, estimated charges for the round trip at the entry price).

    Refused once the live price is further from the price the model saw than the trading
    settings' maximum deviation. The entry limit is ML_ALLOWANCE_PCT past the live price,
    the stop ML_STOP_ATR x the 14-day ATR from it, both in whole ticks away from the trade;
    the quantity is the smaller of the amount per trade and the maximum loss divided by the
    stop distance. The GTT has an entry and a stop and no target: the app squares the
    position off at 15:00 (reconcile)."""
    side = int(pick['side'])
    action = 'buy' if side == 1 else 'sell'
    try:
        decision = Decimal(str(pick['decision_price']))
        atr_pct = Decimal(str(pick['features']['atr_pct']))
        tick = Decimal(str(tick))
        live = _money(ltp)
        if not (decision > 0 and atr_pct > 0 and tick > 0 and live > 0):
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise TradeError('This ML call lacks the price or range an order needs') from None
    moved = (live - decision) * 100 / decision
    limit = Decimal(str(cfg['max_price_deviation_pct']))
    if abs(moved) > limit:
        raise TradeError(f'The price has moved {moved:+.2f}% since the model decided at '
                         f'₹{decision:.2f}; your settings allow {limit:g}%')
    entry = to_tick(live * (1 + side * ML_ALLOWANCE_PCT / 100), tick, side)
    stop = to_tick(live * (1 - side * ML_STOP_ATR * atr_pct), tick, side)
    if stop <= 0 or not ((stop < live <= entry) if side == 1 else (entry <= live < stop)):
        raise TradeError('Stop is invalid at the live price')
    quantity = size(entry, stop, cfg)
    if quantity < 1:
        raise TradeError('One share exceeds the amount per trade or the maximum loss per '
                         'trade')
    loss = quantity * abs(entry - stop)
    est = charges(quantity, entry, entry, action, rates)
    payload = {'type': 'MULTIPLE', 'quantity': quantity, 'product': 'I',
               'instrument_token': pick['instrument_key'],
               'transaction_type': action.upper(), 'rules': [
                   {'strategy': 'ENTRY', 'trigger_type': 'IMMEDIATE',
                    'trigger_price': float(entry)},
                   {'strategy': 'STOPLOSS', 'trigger_type': 'IMMEDIATE',
                    'trigger_price': float(stop)}]}
    return quantity, entry, stop, payload, loss, est


def ml_preview(pick, cfg, rates, tick=Decimal('0.01')):
    """The order ml_plan would place at the price the model saw: {'ok': True, 'quantity',
    'entry', 'stop', 'value', 'loss', 'charges'} in rupees, or {'ok': False, 'reason'}."""
    try:
        quantity, entry, stop, _, loss, est = ml_plan(pick, pick['decision_price'], cfg,
                                                      tick, rates)
    except (TradeError, KeyError) as exc:
        return {'ok': False, 'reason': str(exc)}
    return {'ok': True, 'quantity': quantity, 'entry': float(entry), 'stop': float(stop),
            'value': float(quantity * entry), 'loss': float(loss), 'charges': float(est)}


def ml_pick(conn, day, *, instrument_key=None, telegram_message_id=None):
    """One of the day's ML calls, by its stock or by its Telegram message."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''select session_date, instrument_key, company_id, symbol, side,
                              prediction::float8 prediction, features, decision_price,
                              telegram_message_id, status
                       from ml_intraday_pick where session_date=%s
                         and (instrument_key=%s or telegram_message_id=%s)''',
                    (day, instrument_key, telegram_message_id))
        rows = cur.fetchall()
    if len(rows) != 1 or rows[0]['company_id'] is None:
        raise TradeError('Approval does not match one of today\'s ML calls'
                         if telegram_message_id is not None else
                         'No ML call for this stock today')
    if rows[0]['status'] != 'open':
        raise TradeError('This ML call is already closed')
    return rows[0]


def approve_ml(conn, instrument_key=None, *, source, telegram_message_id=None,
               telegram_update_id=None, broker=None, clock=utc_now, notify=send_telegram):
    """Submit one of today's ML calls as a real intraday order, by its stock (the page) or
    its Telegram message (a reply). Returns (trade_id, status). The live-trading switch,
    the ML-orders switch, the one-order-a-stock-a-day rule and the daily limits apply as
    to any order; a submit is never retried."""
    if source not in ('admin', 'telegram'):
        raise ValueError('Unknown approval source')
    if source == 'telegram' and (type(telegram_update_id) is not int
                                 or type(telegram_message_id) is not int):
        raise ValueError('Telegram approval message and update IDs are required')
    now = require_aware(clock())
    if not ml_window(now):
        raise TradeError(f'ML calls can be approved only until {ML_APPROVE_UNTIL:%H:%M} IST: '
                         'later entries were not tested')
    day = now.astimezone(IST).date()
    access_token = trading_token()
    if not access_token and broker is None:
        raise TradeError('Upstox trading OAuth token is missing; save it in Settings, Upstox tab')
    own_broker = broker is None
    broker = broker or Broker(access_token)
    try:
        pick = ml_pick(conn, day, instrument_key=instrument_key,
                       telegram_message_id=telegram_message_id)
        key = pick['instrument_key']
        try:
            ticks = eligibility.tick_sizes(now=now, force=True)
        except eligibility.FeedError as exc:
            raise TradeError(str(exc)) from None
        if key not in ticks:
            raise TradeError('Upstox does not currently allow intraday trading for this stock')
        ltp = broker.ltp(key)
        now = require_aware(clock())
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute('select * from intraday_trading_settings where singleton=true '
                            'for update')
                cfg = cur.fetchone()
            if not cfg['enabled']:
                raise TradeError('Live intraday trading is disabled in settings')
            if not cfg['ml_orders_enabled']:
                raise TradeError('Real orders for the ML calls are off in settings')
            if not ml_window(now):
                raise TradeError(f'ML calls can be approved only until '
                                 f'{ML_APPROVE_UNTIL:%H:%M} IST')
            if source == 'telegram' and conn.execute(
                    'select 1 from intraday_trade where telegram_update_id=%s',
                    (telegram_update_id,)).fetchone():
                raise TradeError('This Telegram approval was already processed')
            pick = ml_pick(conn, day, instrument_key=key)
            if conn.execute("""select 1 from intraday_trade where company_id=%s
                and trading_day=%s and status<>'rejected'""",
                            (pick['company_id'], day)).fetchone():
                raise TradeError('This stock already has an intraday order today')
            count, spent = conn.execute('''select count(*),coalesce(sum(notional),0)
                from intraday_trade where trading_day=%s
                and status not in ('rejected','expired')''', (day,)).fetchone()
            quantity, entry, stop, payload, risk, est = ml_plan(
                pick, ltp, cfg, ticks[key], intraday_rates())
            notional = quantity * entry
            if count >= cfg['max_daily_trades'] or spent+notional > cfg['max_daily_rupees']:
                raise TradeError('Daily intraday trade count or gross value limit reached')
            expires = min(order_expiry(now, cfg),
                          dt.datetime.combine(day, ML_SQUARE_OFF, IST))
            action = 'buy' if pick['side'] == 1 else 'sell'
            trade_id = conn.execute('''insert into intraday_trade(company_id,trading_day,
                action,kind,symbol,instrument_key,approved_by,approved_at,expires_at,quantity,
                entry_price,stop_price,notional,status,telegram_update_id,risk_rupees,
                est_charges)
                values(%s,%s,%s,'ml',%s,%s,%s,%s,%s,%s,%s,%s,%s,'submitting',%s,%s,%s)
                returning trade_id''', (pick['company_id'], day, action, pick['symbol'],
                key, source, now, expires, quantity, entry, stop, notional,
                telegram_update_id, risk, est)).fetchone()[0]
        conn.commit()  # Durable reservation BEFORE the broker request.
        notify_status = _notifier(notify, trade_id)
        status, gtt_id = _place(conn, broker, payload, trade_id, pick['symbol'], notify_status)
        if gtt_id is None:
            return trade_id, status
        small = (f' At ₹{notional:,.0f} a trade, charges take most of the tested edge '
                 f'(tested at ₹1 lakh).' if notional < ML_MIN_USEFUL_RUPEES else '')
        notify_status(f'ML intraday {action.upper()} {pick["symbol"]}: Upstox GTT {gtt_id} '
                      f'accepted for {quantity} shares, limit ₹{entry} (at most '
                      f'{ML_ALLOWANCE_PCT:g}% past the live ₹{_money(ltp)}); stop ₹{stop} '
                      f'(half the 14-day ATR). Loss at the stop ₹{risk:.2f}, estimated '
                      f'charges ₹{est}. Unfilled, the entry is cancelled at '
                      f'{expires.astimezone(IST):%H:%M} IST; filled, the app squares it off '
                      f'at {ML_SQUARE_OFF:%H:%M} IST.{small} Trade #{trade_id}.')
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
    if (not cfg or not cfg['enabled'] or not cfg['auto_high_volume_enabled']
            or not cfg['rule_calls_enabled']):
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


def _statuses(detail):
    return {r.get('strategy'): str(r.get('status') or '').upper()
            for r in detail.get('rules', [])}


def _after_fill(detail):
    """A filled entry's state: its exits must still be working."""
    rules = _statuses(detail)
    if rules.get('ENTRY') != 'COMPLETED' or any(
            rules.get(k) in ('FAILED', 'CANCELLED', 'EXPIRED') for k in ('TARGET', 'STOPLOSS')):
        return 'exit_unprotected'
    return 'entry_filled'


def _expire(broker, gtt_id, detail):
    """Cancel an entry still unfilled at its expiry: (state, GTT details) afterwards.

    An IMMEDIATE entry sends its limit order to the exchange at once, and Upstox documents
    cancelling a GTT that has not yet triggered, so the entry's own order is cancelled
    first. Only once that order is done unfilled is the GTT, with its exits, cancelled; an
    entry that filled meanwhile keeps them."""
    order_id = next((r.get('order_id') for r in detail.get('rules', [])
                     if r.get('strategy') == 'ENTRY'), None)
    if order_id:
        try:
            broker.cancel_order(order_id)
        except BrokerError:
            pass        # e.g. already filled or cancelled: its state below says which
        status, filled = broker.order_state(order_id)
        if status not in ORDER_DONE:
            return 'cancel_uncertain', detail
        if filled:
            detail = broker.details(gtt_id)
            return _after_fill(detail), detail
    broker.cancel(gtt_id)
    detail = broker.details(gtt_id)
    if _statuses(detail).get('ENTRY') == 'COMPLETED':
        return _after_fill(detail), detail
    return 'expired', detail


def _notice(state, symbol, gtt_id, trade_id, entry, expires, detail, cancelled):
    head = f'Intraday {symbol} · GTT {gtt_id}'
    if state != 'expired':
        return f'{head}: {state.replace("_", " ")}. Check Upstox positions and orders.'
    rule = next((r for r in detail.get('rules', []) if r.get('strategy') == 'ENTRY'), {})
    if cancelled:
        return (f'{head}: the entry limit ₹{entry} did not fill by '
                f'{expires.astimezone(IST):%H:%M} IST and was cancelled. No position. '
                f'Trade #{trade_id}.')
    reason = f": {rule['message']}" if rule.get('message') else ''
    return (f'{head}: Upstox shows the entry {str(rule.get("status") or "").lower()}'
            f'{reason}. No position. Trade #{trade_id}.')


def _square_off(conn, broker, trade_id, gtt_id, symbol, key, action, now, notify):
    """Close an ML trade's position at 15:00 IST with the app's own market order.

    Its GTT is cancelled first and the order is sent only once its stop can no longer
    fire: cancelled, expired or failed, or its order done. Upstox documents cancelling a
    GTT only before it triggers; when the stop survives, two exits could reverse the
    position, so none is sent and the owner is told that Upstox's own square-off at 15:10
    will close it. The attempt is recorded before the order is sent and never repeated.
    Returns 1."""
    def finish(state, detail, error=None, exit_order=None):
        conn.execute('''update intraday_trade set status=%s,broker_state=%s,last_checked_at=%s,
            last_error=coalesce(%s,last_error),exit_order_id=coalesce(%s,exit_order_id)
            where trade_id=%s''', (state, Jsonb(detail), now, error, exit_order, trade_id))
        conn.commit()

    def failed(detail, reason):
        finish('square_off_failed', detail, reason)
        record_issue('intraday-trade', 'SquareOffFailed', event_id=str(trade_id))
        notify(f'ML intraday {symbol}: no square-off at {ML_SQUARE_OFF:%H:%M} IST: {reason}. '
               'Upstox squares off intraday positions itself at 15:10 IST, for a fee. To '
               'close it sooner, cancel its GTT in Upstox, then close the position. '
               f'Trade #{trade_id}.')
        return 1

    try:
        broker.cancel(gtt_id)
    except BrokerError:
        pass        # e.g. its stop already completed: the details say
    detail = broker.details(gtt_id)
    rules = {r.get('strategy'): r for r in detail.get('rules', [])}
    stop = rules.get('STOPLOSS', {})
    if str(stop.get('status') or '').upper() == 'COMPLETED':
        finish('closed', detail)
        notify(f'ML intraday {symbol}: the stop closed it before the square-off. '
               f'Trade #{trade_id}.')
        return 1
    if not stop.get('order_id') and str(stop.get('status') or '').upper() not in (
            'CANCELLED', 'EXPIRED', 'FAILED'):
        return failed(detail, 'Upstox kept its stop working, so the app sent no second exit')
    filled = {}
    for strategy in ('ENTRY', 'STOPLOSS'):
        order_id = rules.get(strategy, {}).get('order_id')
        if not order_id:
            filled[strategy] = 0
            continue
        status, filled[strategy] = broker.order_state(order_id)
        if status not in ORDER_DONE:
            try:
                broker.cancel_order(order_id)
            except BrokerError:
                pass    # its state below says whether it is done
            status, filled[strategy] = broker.order_state(order_id)
            if status not in ORDER_DONE:
                return failed(detail, f'its {strategy.lower()} order is still {status}')
    held = filled['ENTRY'] - filled['STOPLOSS']
    if held <= 0:
        finish('closed' if filled['ENTRY'] else 'expired', detail)
        return 1
    finish('squaring_off', detail)          # durable BEFORE the order is sent
    try:
        order_id = broker.square_off(key, held, action)
    except BrokerError as exc:
        return failed(detail, f'its market order for {held} shares was refused or not '
                              f'confirmed ({exc}); check the Upstox order book')
    finish('squaring_off', detail, exit_order=order_id)
    notify(f'ML intraday {symbol}: squared off at {ML_SQUARE_OFF:%H:%M} IST, a market order '
           f'to {"sell" if action == "buy" else "buy back"} {held} shares (Upstox order '
           f'{order_id}). Trade #{trade_id}.')
    return 1


def _square_off_results(conn, broker, now, notify):
    """Settle the square-off orders sent: complete closes the trade; refused or cancelled
    leaves the position open, and is reported. One interrupted before Upstox answered is
    reported too, never resent."""
    changed = 0
    rows = conn.execute("""select trade_id,symbol,exit_order_id,last_checked_at from
        intraday_trade where status='squaring_off' order by trade_id""").fetchall()
    for trade_id, symbol, order_id, checked in rows:
        reason = None
        if order_id is None:
            if checked is not None and checked > now - dt.timedelta(seconds=30):
                continue            # the square-off may still be in flight
            reason = 'the square-off was interrupted before Upstox answered'
        else:
            try:
                status, _ = broker.order_state(order_id)
            except BrokerError:
                record_issue('intraday-trade', 'ReconcileFailed', event_id=str(trade_id))
                continue
            if status == 'complete':
                conn.execute("""update intraday_trade set status='closed',last_checked_at=%s
                    where trade_id=%s""", (now, trade_id))
                conn.commit()
                changed += 1
                continue
            if status not in ORDER_DONE:
                continue
            reason = f'Upstox shows the square-off order {status}'
        conn.execute("""update intraday_trade set status='square_off_failed',last_error=%s,
            last_checked_at=%s where trade_id=%s""", (reason, now, trade_id))
        conn.commit()
        changed += 1
        record_issue('intraday-trade', 'SquareOffFailed', event_id=str(trade_id))
        notify(f'ML intraday {symbol}: {reason}. Check Upstox positions and orders now; '
               f'Upstox squares off intraday positions itself at 15:10 IST. Trade #{trade_id}.')
    return changed


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
        rows = conn.execute("""select trade_id,gtt_order_id,expires_at,symbol,status,
            entry_price,kind,trading_day,instrument_key,action from intraday_trade
            where gtt_order_id is not null and status in
            ('submitted','entry_open','entry_filled','exit_unprotected','cancel_uncertain')
            order by trade_id""").fetchall()
        local = now.astimezone(IST)
        for (trade_id, gtt_id, expires, symbol, prior, entry_price, kind, day, key,
             action) in rows:
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
                cancelled = now >= expires and state == 'entry_open'
                if cancelled:
                    try:
                        state, detail = _expire(broker, gtt_id, detail)
                    except BrokerError:
                        state = 'cancel_uncertain'
                    if state == 'cancel_uncertain':
                        record_issue('intraday-trade', 'CancelUncertain', event_id=str(trade_id))
                error = None
                if kind == 'ml' and state in ('entry_filled', 'exit_unprotected'):
                    if local.date() == day and ML_SQUARE_OFF <= local.time() < ML_LAST_ORDER:
                        changed += _square_off(conn, broker, trade_id, gtt_id, symbol, key,
                                               action, now, notify)
                        continue
                    if local.date() > day or local.time() >= ML_LAST_ORDER:
                        state = 'square_off_failed'
                        error = (f'Not squared off by {ML_LAST_ORDER:%H:%M} IST; Upstox squares '
                                 'off intraday positions itself')
                if state == 'expired':
                    rule = rules.get('ENTRY', {}) if not cancelled else {}
                    error = rule.get('message') or None
                conn.execute('''update intraday_trade set status=%s,broker_state=%s,
                    last_checked_at=%s,last_error=coalesce(%s,last_error)
                    where trade_id=%s''', (state, Jsonb(detail), now, error, trade_id))
                conn.commit()
                if state != prior:
                    changed += 1
                    if state == 'exit_unprotected':
                        record_issue('intraday-trade', 'ExitUnprotected', event_id=str(trade_id))
                    if state == 'square_off_failed':
                        record_issue('intraday-trade', 'SquareOffFailed', event_id=str(trade_id))
                    if state in ('entry_filled','closed','expired','cancel_uncertain',
                                 'exit_unprotected','square_off_failed'):
                        notify(_notice(state, symbol, gtt_id, trade_id, entry_price, expires,
                                       detail, cancelled))
            except BrokerError:
                record_issue('intraday-trade', 'ReconcileFailed', event_id=str(trade_id))
        changed += _square_off_results(conn, broker, now, notify)
        record_pnl(conn, broker, now, notify)
        return changed
    finally:
        if own_broker:
            broker.close()


PNL_RETRY = dt.timedelta(minutes=5)
PNL_DAYS = 3


def record_pnl(conn, broker, now, notify):
    """Net P&L of closed trades not yet priced: the average fills of the entry order and
    of the exit order that closed the trade (the orders its GTT rules placed, or the app's
    own square-off of an ML trade, read from Upstox), less charges estimated at those
    fills. A trade closed some other way, or
    with unequal fills, gets a note instead. An Upstox failure is retried five minutes
    later, for three days."""
    day = now.astimezone(IST).date()
    rows = conn.execute("""select trade_id,symbol,action,broker_state,exit_order_id
        from intraday_trade
        where status='closed' and net_pnl is null and pnl_note is null and trading_day>=%s
          and (pnl_checked_at is null or pnl_checked_at<%s) order by trade_id""",
        (day - dt.timedelta(days=PNL_DAYS), now - PNL_RETRY)).fetchall()
    for trade_id, symbol, action, state, own_exit in rows:
        rules = {r.get('strategy'): r for r in (state or {}).get('rules', [])}
        entry_order = rules.get('ENTRY', {}).get('order_id')
        exits = [own_exit] if own_exit else [
            rules[k].get('order_id') for k in ('TARGET', 'STOPLOSS')
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
