"""Continuous open-trade monitoring. Observe by default; explicit --execute permits
only tightening an existing GTT stop. Never enters, cancels, adds size or market-exits.
The trailing policy is experimental, not a validated improvement in profitability.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from igs.intraday.costs import charges, intraday_rates
from igs.intraday.trading import Broker, BrokerError, settings, trading_token
from igs.pit.gate import require_gate
from igs.timeutil import IST, require_aware, utc_now

LOCK = 741070
D = Decimal
ACTIVE = ('submitted', 'entry_open', 'entry_filled', 'exit_unprotected')


@dataclass(frozen=True)
class Policy:
    # R is the entry fill minus the ORIGINAL approved stop, not a moving ATR.
    activate_r: Decimal = D('1.5')
    gap_r: Decimal = D('1')
    slippage_bps: Decimal = D('5')
    max_spread_bps: Decimal = D('30')
    max_age_seconds: int = 15
    # Thin stocks can trade rarely while the order book stays live; judge freshness by
    # the book and allow the last trade to be older.
    max_trade_age_seconds: int = 120
    min_change_ticks: int = 5

    def __post_init__(self):
        for value in (self.activate_r, self.gap_r, self.max_spread_bps):
            if not value.is_finite() or value <= 0:
                raise ValueError('Monitor policy values must be finite and positive')
        if not self.slippage_bps.is_finite() or self.slippage_bps < 0:
            raise ValueError('Invalid slippage budget')
        if (self.max_age_seconds <= 0 or self.max_trade_age_seconds < self.max_age_seconds
                or self.min_change_ticks < 1):
            raise ValueError('Invalid quote age or minimum stop increment')


class MonitorBroker(Broker):
    def positions(self):
        rows = self._request('GET', '/v2/portfolio/short-term-positions')
        if not isinstance(rows, list):
            raise BrokerError('Invalid positions response')
        return rows

    def quotes(self, keys):
        rows = self._request('GET', '/v2/market-quote/quotes',
                             params={'instrument_key': ','.join(keys)})
        if not isinstance(rows, dict):
            raise BrokerError('Invalid quotes response')
        result = {}
        for row in rows.values():
            key = row.get('instrument_token') if isinstance(row, dict) else None
            if key in result or key not in keys:
                raise BrokerError('Ambiguous quote identity')
            result[key] = row
        return result

def number(value):
    value = D(str(value))
    if not value.is_finite() or value <= 0:
        raise ValueError('Expected a positive finite price')
    return value


def quote_price(quote, key, action, now, policy):
    """Use executable-side top-of-book, not the last trade, and require both timestamps."""
    if quote.get('instrument_token') != key:
        raise ValueError('Quote instrument mismatch')
    stamp = quote['timestamp']
    updated = (dt.datetime.fromisoformat(stamp) if isinstance(stamp, str) and 'T' in stamp
               else dt.datetime.fromtimestamp(float(stamp) / 1000, dt.UTC))
    traded = dt.datetime.fromtimestamp(float(quote['last_trade_time']) / 1000, dt.UTC)
    for ts, limit in ((require_aware(updated), policy.max_age_seconds),
                      (traded, policy.max_trade_age_seconds)):
        if not 0 <= (now - ts).total_seconds() <= limit:
            raise ValueError('Stale or future quote')
    bid, ask = quote['depth']['buy'][0], quote['depth']['sell'][0]
    b, a = number(bid['price']), number(ask['price'])
    if int(bid['quantity']) <= 0 or int(ask['quantity']) <= 0 or b > a:
        raise ValueError('Missing or crossed market depth')
    if (a - b) / ((a + b) / 2) * 10000 > policy.max_spread_bps:
        raise ValueError('Spread exceeds monitoring policy')
    return b if action == 'buy' else a


def proposal(*, action, entry, original_stop, broker_stop, previous_peak, previous_stop,
             price, quantity, tick, policy, rates):
    """A monotone, cost-aware stop proposal; no prediction of the top of a move."""
    side = 1 if action == 'buy' else -1
    entry, original_stop, broker_stop, price, tick = map(
        number, (entry, original_stop, broker_stop, price, tick))
    if action not in ('buy', 'sell') or quantity <= 0:
        raise ValueError('Invalid direction or quantity')
    risk = side * (entry - original_stop)
    if risk <= 0:
        raise ValueError('Original stop does not define positive risk from the fill')
    better = max if side == 1 else min
    peak = better(entry, price, number(previous_peak or entry))
    stop = better(broker_stop, number(previous_stop or broker_stop))
    if side * (price - stop) <= 0:
        return {'action': 'stop_crossed', 'peak': peak, 'stop': stop,
                'reason': 'Observed stop crossed; check broker exit, do not send another exit'}
    if side * (peak - entry) >= policy.activate_r * risk:
        # Budget charges at entry and current mark, plus a configurable exit slippage.
        cost = max(charges(quantity, entry, entry, action, rates),
                   charges(quantity, entry, price, action, rates)) / quantity
        breakeven = entry + side * (cost + price * policy.slippage_bps / 10000)
        candidate = better(stop, peak - side * policy.gap_r * risk, breakeven)
        rounding = ROUND_CEILING if side == 1 else ROUND_FLOOR
        candidate = (candidate / tick).to_integral_value(rounding=rounding) * tick
        if candidate > 0 and side * (price - candidate) >= 2 * tick:
            stop = candidate
    change = side * (stop - broker_stop)
    return {'action': 'tighten' if change >= policy.min_change_ticks * tick else 'hold',
            'peak': peak, 'stop': stop,
            'reason': 'Experimental trailing policy; estimated costs are not guaranteed fills'}


def matched_position(trade, positions):
    rows = [r for r in positions if r.get('instrument_token') == trade['instrument_key']
            and r.get('product') == 'I']
    side = 1 if trade['action'] == 'buy' else -1
    entry_side = 'buy' if side == 1 else 'sell'
    exit_side = 'sell' if side == 1 else 'buy'
    if (len(rows) != 1 or int(rows[0]['quantity']) != side * trade['quantity']
            or int(rows[0]['overnight_quantity']) != 0
            or int(rows[0][f'day_{entry_side}_quantity']) != trade['quantity']
            or int(rows[0][f'day_{exit_side}_quantity']) != 0):
        raise ValueError('Broker position differs from the app trade; manual review required')


def stop_payload(trade, detail, stop):
    if (detail['gtt_order_id'] != trade['gtt_order_id']
            or detail['instrument_token'] != trade['instrument_key']
            or detail['product'] != 'I' or detail['type'] != 'MULTIPLE'
            or int(detail['quantity']) != trade['quantity']):
        raise ValueError('GTT identity, product or quantity mismatch')
    rules = detail['rules']
    by_kind = {r['strategy']: r for r in rules}
    if (len(by_kind) != len(rules) or set(by_kind) not in
            ({'ENTRY', 'STOPLOSS'}, {'ENTRY', 'STOPLOSS', 'TARGET'})):
        raise ValueError('Unsupported GTT rules')
    if by_kind['ENTRY']['status'] != 'COMPLETED':
        raise ValueError('Entry is not completely filled')
    if by_kind['ENTRY']['transaction_type'] != trade['action'].upper():
        raise ValueError('GTT entry direction mismatch')
    for kind in set(by_kind) - {'ENTRY'}:
        rule = by_kind[kind]
        expected = 'SELL' if trade['action'] == 'buy' else 'BUY'
        if rule['transaction_type'] != expected:
            raise ValueError('GTT exit direction mismatch')
        if rule.get('status') != 'SCHEDULED' or rule.get('order_id'):
            raise ValueError('Exit already triggered or not scheduled')
        if rule.get('trailing_gap'):
            raise ValueError('Broker trailing stop already present; do not replace it')
    copied = [{'strategy': r['strategy'], 'trigger_type': r['trigger_type'],
               'trigger_price': float(number(r['trigger_price']))} for r in rules]
    for rule in copied:
        if rule['strategy'] == 'STOPLOSS':
            rule['trigger_price'] = float(stop)
    return {'type': 'MULTIPLE', 'quantity': trade['quantity'],
            'gtt_order_id': trade['gtt_order_id'], 'rules': copied}


def serial(row):
    return {k: str(v) if isinstance(v, Decimal) else v for k, v in row.items()}


def event(conn, trade_id, now, mode, status, detail):
    result = conn.execute('''insert into trade_monitor_event
        (trade_id,created_at,mode,status,detail) values(%s,%s,%s,%s,%s) returning event_id''',
        (trade_id, now, mode, status, Jsonb(serial(detail)))).fetchone()[0]
    conn.commit()
    return result


def step(conn, broker, *, execute=False, policy=None, clock=utc_now, ticks=None):
    """One bounded polling cycle. Session lock serialises workers across UI/CLI processes."""
    policy = policy or Policy()
    now = require_aware(clock())
    local = now.astimezone(IST)
    if local.weekday() >= 5 or not dt.time(9, 15) <= local.time() < dt.time(15, 30):
        return {'status': 'outside_session', 'monitored': 0}
    if not conn.execute('select pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0]:
        conn.rollback()
        return {'status': 'busy', 'monitored': 0}
    mode = 'execute' if execute else 'observe'
    try:
        if execute:
            require_gate()
            if not settings(conn)['enabled']:
                raise ValueError('Live trading is disabled in Settings')
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute('''select t.*,m.peak,m.observed_stop from intraday_trade t
                left join trade_monitor_state m using(trade_id)
                where t.trading_day=%s and t.status=any(%s) and t.gtt_order_id is not null
                order by t.trade_id''', (local.date(), list(ACTIVE)))
            trades = cur.fetchall()
        if not trades:
            return {'status': 'idle', 'monitored': 0}
        if ticks is None:
            from igs.intraday.eligibility import tick_sizes
            ticks = tick_sizes()
        positions = broker.positions()
        keys = list({t['instrument_key'] for t in trades})
        if len(keys) > 500:
            raise ValueError('Monitoring universe exceeds broker quote batch limit')
        quotes = broker.quotes(keys)
        for trade in trades:
            try:
                _trade(conn, broker, trade, positions, quotes, ticks, mode, policy, clock)
            except (BrokerError, ValueError, KeyError, TypeError, IndexError,
                    InvalidOperation, OverflowError) as exc:
                conn.rollback()
                # Never persist raw provider responses or credentials in errors.
                event(conn, trade['trade_id'], clock(), mode, 'blocked',
                      {'reason': str(exc) if isinstance(exc, (ValueError, BrokerError))
                       else 'Malformed broker data', 'error_type': type(exc).__name__})
        return {'status': mode, 'monitored': len(trades)}
    finally:
        conn.rollback()
        conn.execute('select pg_advisory_unlock(%s)', (LOCK,))
        conn.commit()


def _trade(conn, broker, trade, positions, quotes, ticks, mode, policy, clock):
    matched_position(trade, positions)
    detail = broker.details(trade['gtt_order_id'])
    # Validate before even a hypothetical stop update.
    stop_payload(trade, detail, trade['stop_price'])
    rules = {r['strategy']: r for r in detail['rules']}
    entry, filled = broker.fill(rules['ENTRY']['order_id'])
    if filled != trade['quantity']:
        raise ValueError('Partial fill: automatic modification withheld')
    now = require_aware(clock())  # after network requests, not cycle start
    if now.astimezone(IST).date() != trade['trading_day']:
        raise ValueError('Trading day changed during monitoring')
    price = quote_price(quotes[trade['instrument_key']], trade['instrument_key'],
                        trade['action'], now, policy)
    broker_stop = number(rules['STOPLOSS']['trigger_price'])
    decision = proposal(action=trade['action'], entry=entry,
        original_stop=trade['stop_price'], broker_stop=broker_stop,
        previous_peak=trade['peak'],
        previous_stop=trade['observed_stop'] if mode == 'observe' else None,
        price=price, quantity=filled, tick=ticks[trade['instrument_key']],
        policy=policy, rates=intraday_rates())
    if mode == 'observe' and decision['action'] == 'stop_crossed':
        # Observe mode trails a hypothetical stop the broker does not hold.
        decision['reason'] = ('Hypothetical observed stop crossed; the broker stop is '
                              'unchanged and still controls the exit')
    net =(1 if trade['action'] == 'buy' else -1) * (price - entry) * filled
    net -= charges(filled, entry, price, trade['action'], intraday_rates())
    snapshot = serial({**decision, 'mode': mode, 'price': price, 'entry_fill': entry,
                       'broker_stop': broker_stop, 'estimated_net': net})
    conn.execute('''insert into trade_monitor_state
        (trade_id,peak,observed_stop,updated_at,snapshot) values(%s,%s,%s,%s,%s)
        on conflict(trade_id) do update set peak=excluded.peak,
        observed_stop=excluded.observed_stop,updated_at=excluded.updated_at,
        snapshot=excluded.snapshot''', (trade['trade_id'], decision['peak'], decision['stop'],
                                      now, Jsonb(snapshot)))
    conn.commit()
    if mode == 'observe' or decision['action'] != 'tighten':
        event(conn, trade['trade_id'], now, mode, decision['action'], snapshot)
        return
    # The established square-off worker owns the last part of the session. Never race it.
    if now.astimezone(IST).time() >= dt.time(14, 59):
        raise ValueError('Stop modifications end at 14:59 IST; existing exits retain control')
    if conn.execute("""select 1 from trade_monitor_event where trade_id=%s
        and status in ('submitting','uncertain','rejected') limit 1""",
                    (trade['trade_id'],)).fetchone():
        raise ValueError('Previous stop modification is unresolved; manual review required')
    # Refresh state immediately before any modification, not just at the cycle's start.
    matched_position(trade, broker.positions())
    fresh_detail = broker.details(trade['gtt_order_id'])
    fresh_rules = {r['strategy']: r for r in fresh_detail['rules']}
    fresh_stop = number(fresh_rules['STOPLOSS']['trigger_price'])
    fresh_quote = broker.quotes([trade['instrument_key']])[trade['instrument_key']]
    now = require_aware(clock())
    if (now.astimezone(IST).date() != trade['trading_day'] or
            not dt.time(9, 15) <= now.astimezone(IST).time() < dt.time(14, 59)):
        raise ValueError('Modification window closed during refresh')
    if not settings(conn)['enabled']:
        raise ValueError('Trading was disabled during monitoring')
    price = quote_price(fresh_quote, trade['instrument_key'], trade['action'], now, policy)
    side = 1 if trade['action'] == 'buy' else -1
    tick = number(ticks[trade['instrument_key']])
    if (side * (decision['stop'] - fresh_stop) < policy.min_change_ticks * tick
            or side * (price - decision['stop']) < 2 * tick):
        raise ValueError('Broker stop or market changed; proposal no longer valid')
    payload = stop_payload(trade, fresh_detail, decision['stop'])
    attempt = event(conn, trade['trade_id'], now, mode, 'submitting', snapshot)
    try:
        broker.modify_stop(payload)
        verified = broker.details(trade['gtt_order_id'])
        actual = next(r for r in verified['rules'] if r['strategy'] == 'STOPLOSS')
        if number(actual['trigger_price']) != decision['stop']:
            raise BrokerError('Broker stop differs after modification')
        status = 'confirmed'
    except BrokerError as exc:
        status = 'rejected' if exc.definitive else 'uncertain'
    except (KeyError, ValueError, TypeError, StopIteration, InvalidOperation):
        status = 'uncertain'
    conn.execute('update trade_monitor_event set status=%s where event_id=%s',
                 (status, attempt))
    conn.commit()


# Per-cycle observations; broker modification records are never deleted.
ROUTINE = ('hold', 'tighten', 'blocked', 'stop_crossed')


def cleanup(conn, day):
    """After the session, drop routine events for trades up to `day`, keeping each trade's
    last event; trade_monitor_state retains the final snapshot."""
    deleted = conn.execute('''delete from trade_monitor_event e using intraday_trade t
        where e.trade_id=t.trade_id and t.trading_day<=%s and e.status=any(%s)
        and e.event_id < (select max(event_id) from trade_monitor_event l
                          where l.trade_id=e.trade_id)''', (day, list(ROUTINE))).rowcount
    conn.commit()
    return deleted


def run(*, once=False, execute=False, interval=5, clock=utc_now):
    """User-run daemon. No broker calls on weekends/outside the monitoring session."""
    import psycopg

    from igs.db import connect
    if not 5 <= interval <= 60:
        raise ValueError('Polling interval must be between 5 and 60 seconds')
    token = trading_token()
    if not token:
        raise ValueError('Upstox trading token is missing; configure it in Settings')
    broker = MonitorBroker(token)
    cleaned = None
    try:
        with connect() as conn:
            while True:
                try:
                    print(step(conn, broker, execute=execute, clock=clock), flush=True)
                    local = require_aware(clock()).astimezone(IST)
                    if local.time() >= dt.time(15, 30) and cleaned != local.date():
                        print({'status': 'cleanup', 'deleted': cleanup(conn, local.date())},
                              flush=True)
                        cleaned = local.date()
                except BrokerError:
                    conn.rollback()
                    print('Monitor feed unavailable; no action submitted', flush=True)
                except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                    # Settings switched off, a malformed response: report and keep polling.
                    conn.rollback()
                    print(f'Monitor cycle skipped ({type(exc).__name__}: {exc}); '
                          'no action submitted', flush=True)
                except psycopg.OperationalError:
                    # A dead connection cannot recover here; systemd restarts the worker.
                    print('Monitor database connection lost; exiting for restart', flush=True)
                    raise
                if once:
                    return 0
                time.sleep(interval)
    finally:
        broker.close()
