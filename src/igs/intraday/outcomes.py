"""Paper record of intraday calls: each buy/sell call replayed on its session's candles.

The limit entry fills when a candle starting before the call expires trades at or
through the recommended price. From the fill, the first of stop and target to be
touched decides the call. A candle that touches both counts as the stop, and so does
the stop in the fill candle itself: the order within a candle is unknown, so the worse
case is taken. Neither by 15:15 IST, when brokers square off intraday positions, exits
at the close of the last candle before it. Results are in R, the stop distance, before
charges and slippage. In rupees, each call is sized as an order would be with the
current trading settings and charged the estimated intraday charges.

Calls are resolved the next day, from the full session in a later scan's cached
history of the stock. The record is a measurement after the fact: no call and no order
reads it.
"""
from __future__ import annotations

import datetime as dt

from psycopg.rows import dict_row

from igs.intraday.costs import intraday_rates, net_result, size
from igs.intraday.engine import BAR, Candle
from igs.timeutil import IST, require_aware

EXIT_TIME = dt.time(15, 15)
RECORD_DAYS = 40            # the stock's cached history covers the previous 28 sessions
BANDS = ((1.8, 5), (5, 20), (20, 50), (50, None))


def close_location(result):
    """Where the call's volume candle closed in its range, toward the trade: 1 at its
    high for a buy (at its low for a sell), 0 at the other end. None if unknown."""
    high, low, close = (result.get(k) for k in ('candle_high', 'candle_low', 'reference'))
    if None in (high, low, close) or high <= low:
        return None
    where = (close - low) / (high - low)
    return where if result['action'] == 'buy' else 1 - where


def resolve(result, candles, *, complete):
    """The outcome of one call on its session's candles: a dict, or None while they
    cannot decide it yet. `complete` says the candles cover the whole session."""
    direction = 1 if result['action'] == 'buy' else -1
    start = require_aware(dt.datetime.fromisoformat(result['candle_end']))
    expires = require_aware(dt.datetime.fromisoformat(result['expires_at']))
    entry, stop, target = (float(result[k]) for k in ('reference', 'stop', 'target'))
    day = start.astimezone(IST).date()
    last_start = dt.datetime.combine(day, EXIT_TIME, IST) - BAR
    later = sorted((c for c in candles if c.start >= start and c.start <= last_start
                    and c.start.astimezone(IST).date() == day), key=lambda c: c.start)

    def fills(c):
        return c.low <= entry if direction == 1 else c.high >= entry

    def stopped(c):
        return c.low <= stop if direction == 1 else c.high >= stop

    def reached(c):
        return c.high >= target if direction == 1 else c.low <= target

    fill = next((c for c in later if c.start < expires and fills(c)), None)
    if fill is None:
        if complete or (later and later[-1].start + BAR >= expires):
            return {'outcome': 'not_filled'}
        return None

    def done(kind, candle, price):
        return {'outcome': kind, 'filled_at': fill.start, 'exit_at': candle.start + BAR,
                'exit_price': price,
                'r_multiple': round(direction * (price - entry) / abs(entry - stop), 3)}

    for c in later:
        if c.start < fill.start:
            continue
        if stopped(c):
            return done('stop', c, stop)
        if c is not fill and reached(c):
            return done('target', c, target)
    if not complete:
        return None
    return done('time_exit', later[-1], later[-1].close)


def record_outcomes(conn, now):
    """Resolve the buy/sell calls of earlier days not yet in the record, each from the
    first later cached history of its stock. Returns how many were recorded."""
    today = require_aware(now).astimezone(IST).date()
    midnight = dt.datetime.combine(today, dt.time(0), IST)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''select distinct on (s.company_id, s.result->>'candle_end')
                s.company_id, s.symbol, s.instrument_key, s.result
            from intraday_signal s join intraday_scan r using(scan_id)
            where r.status='complete' and s.result->>'action' in ('buy','sell')
              and s.observed_at between %s and %s
              and not exists(select 1 from intraday_call_outcome o
                  where o.company_id=s.company_id
                    and o.candle_end=(s.result->>'candle_end')::timestamptz)
            order by s.company_id, s.result->>'candle_end', s.scan_id desc''',
                    (midnight - dt.timedelta(days=RECORD_DAYS), midnight))
        calls = cur.fetchall()
    recorded = 0
    for call in calls:
        result = call['result']
        candle_end = require_aware(dt.datetime.fromisoformat(result['candle_end']))
        day = candle_end.astimezone(IST).date()
        row = conn.execute('''select candles from intraday_history where instrument_key=%s
            and session_date>%s order by session_date limit 1''',
                           (call['instrument_key'], day)).fetchone()
        if not row:
            continue
        candles = [Candle(dt.datetime.fromisoformat(b['start']),
                          *(b[k] for k in ('open', 'high', 'low', 'close', 'volume')))
                   for b in row[0]
                   if dt.datetime.fromisoformat(b['start']).astimezone(IST).date() == day]
        if not candles:
            continue
        outcome = resolve(result, candles, complete=True)
        conn.execute('''insert into intraday_call_outcome(company_id,candle_end,trading_day,
            symbol,action,rvol,close_location,reference,stop,target,outcome,filled_at,
            exit_at,exit_price,r_multiple) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s) on conflict do nothing''',
            (call['company_id'], candle_end, day, call['symbol'], result['action'],
             result.get('rvol'), close_location(result), result['reference'],
             result['stop'], result['target'], outcome['outcome'], outcome.get('filled_at'),
             outcome.get('exit_at'), outcome.get('exit_price'), outcome.get('r_multiple')))
        recorded += 1
    conn.commit()
    return recorded


def _limits(conn):
    row = conn.execute('select max_trade_rupees,max_risk_rupees from '
                       'intraday_trading_settings where singleton=true').fetchone()
    return {'max_trade_rupees': row[0], 'max_risk_rupees': row[1]} if row else None


def rupees(row, cfg, rates):
    """(charges, net) rupees of one resolved call, sized as an order with the trading
    settings `cfg` would be and charged at `rates`; None if it did not fill or one share
    exceeds the limits. `row`: action, reference, stop, exit_price, outcome."""
    action, reference, stop, exit_price, outcome = row
    if cfg is None or outcome == 'not_filled' or exit_price is None or reference == stop:
        return None
    quantity = size(reference, stop, cfg)
    if quantity < 1:
        return None
    _, cost, net = net_result(quantity, reference, exit_price, action, rates)
    return cost, net


def summary(conn, since, *, rates=None):
    """The record since `since` (a date), overall and by group: volume jump bands and
    how strongly the volume candle closed. One dict per group. Rupees are after
    estimated charges, at the current amount per trade and maximum loss."""
    rates = rates or intraday_rates()
    cfg = _limits(conn)
    rows = conn.execute('''select rvol,close_location,outcome,r_multiple,
        action,reference,stop,exit_price from intraday_call_outcome
        where trading_day>=%s''', (since,)).fetchall()
    money = [rupees((r[4], r[5], r[6], r[7], r[2]), cfg, rates) for r in rows]
    groups = [('All calls', lambda v, c: True)]
    for low, high in BANDS:
        label = f'Volume jump {low:g}–{high:g}×' if high else f'Volume jump {low:g}× and over'
        groups.append((label, lambda v, c, lo=low, hi=high:
                       v is not None and v >= lo and (hi is None or v < hi)))
    groups += [('Closed in the top 30% of its candle', lambda v, c: c is not None and c >= .7),
               ('Closed lower in its candle', lambda v, c: c is not None and c < .7)]
    out = []
    for label, keep in groups:
        mine = [(r, m) for r, m in zip(rows, money, strict=True)
                if keep(None if r[0] is None else float(r[0]),
                        None if r[1] is None else float(r[1]))]
        filled = [r for r, _ in mine if r[2] != 'not_filled']
        rs = [float(r[3]) for r in filled if r[3] is not None]
        sized = [m for _, m in mine if m is not None]
        out.append({'Group': label, 'Calls': len(mine), 'Filled': len(filled),
                    'Target first': sum(r[2] == 'target' for r in filled),
                    'Stop first': sum(r[2] == 'stop' for r in filled),
                    'Time exit': sum(r[2] == 'time_exit' for r in filled),
                    'Win rate': (sum(r > 0 for r in rs) / len(rs)) if rs else None,
                    'Average R': round(sum(rs) / len(rs), 2) if rs else None,
                    'Total R': round(sum(rs), 2) if rs else None,
                    'Charges ₹': float(sum(c for c, _ in sized)) if sized else None,
                    'Net ₹': float(sum(n for _, n in sized)) if sized else None})
    return out


def recent(conn, since, limit=50, *, rates=None):
    """The latest resolved calls since `since`, newest first, with the rupees of each
    after estimated charges (see summary)."""
    rates = rates or intraday_rates()
    cfg = _limits(conn)
    rows = conn.execute('''select trading_day,symbol,action,rvol,round(close_location,2),
        outcome,r_multiple,reference,stop,exit_price from intraday_call_outcome
        where trading_day>=%s order by candle_end desc limit %s''', (since, limit)).fetchall()
    out = []
    for day, symbol, action, rvol, where, outcome, r, reference, stop, exit_price in rows:
        money = rupees((action, reference, stop, exit_price, outcome), cfg, rates)
        out.append({'Day': day, 'Stock': symbol, 'Call': action, 'Volume jump ×': rvol,
                    'Close location': where, 'Outcome': outcome, 'R': r,
                    'Net ₹': None if money is None else float(money[1])})
    return out
