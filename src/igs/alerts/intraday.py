"""Separate, expiry-aware Telegram messages for completed intraday scans."""
from __future__ import annotations

import datetime as dt

from psycopg.types.json import Jsonb

from igs.alerts.delivery import send_intraday_telegram
from igs.intraday import eligibility
from igs.intraday.engine import trading_window
from igs.intraday.scanner import latest
from igs.intraday.trading import exceptional_volume
from igs.timeutil import IST, require_aware, utc_now


def eligible(result, observed_at, now):
    if not trading_window(now) or result.get('action') not in ('buy', 'sell'):
        return False
    try:
        end = require_aware(dt.datetime.fromisoformat(result['candle_end']))
        expires = require_aware(dt.datetime.fromisoformat(result['expires_at']))
        return (observed_at <= now < expires and end.astimezone(IST).date()
                == now.astimezone(IST).date() and dt.timedelta() <= now-end
                <= dt.timedelta(minutes=5))
    except (ValueError, KeyError, TypeError):
        return False


def _candle(result):
    try:
        return require_aware(dt.datetime.fromisoformat(result['candle_end']))
    except (ValueError, KeyError, TypeError):
        return None


def message(symbol, result, *, automatic=False, ordered=False):
    end = dt.datetime.fromisoformat(result['candle_end']).astimezone(IST)
    expires = dt.datetime.fromisoformat(result['expires_at']).astimezone(IST)
    lines = [f"INTRADAY {result['action'].upper()} · {symbol}",
             f"{end:%d %b %Y} · candle close {end:%H:%M} IST",
             f"Entry limit (recommended price): ₹{result['reference']:.2f}",
             f"Stop: ₹{result['stop']:.2f} · Target: ₹{result['target']:.2f}",
             f"Volume: {result['rvol']:.2f}× same-time median · "
             f"15-min momentum: {result['momentum_pct']:+.2f}%",
             f"Strength: {result['strength']} (rule-based, not a win probability)",
             f"Reason: {result['reason'][:250]}",
             f"Setup expires: {expires:%H:%M} IST; use current broker quotes."]
    order = result.get('order')
    if order:
        lines.insert(4, f"Your order: {order['quantity']} shares · limit ₹{order['entry']:.2f} · "
                        f"stop ₹{order['stop']:.2f} · target ₹{order['target']:.2f}")
        lines.insert(5, f"After estimated charges: ₹{order['net_gain']:,.2f} at the target, "
                        f"−₹{order['net_loss']:,.2f} at the stop ({order['reward_risk']:.2f}×)")
    for item in result.get('evidence', [])[:3]:
        lines.append(f"{item.get('kind', 'Evidence')}: {item.get('title', '')[:160]}")
    if ordered:
        lines.append('This stock already has an intraday order today, so no other order can '
                     'be placed for it.\nhttps://stocks.ednis.ai/')
    elif automatic:
        lines.append('Automatic order eligible: the app may place a limit entry without a '
                     'reply. A separate order-status message confirms any submission. '
                     'The entry may remain unfilled; linked exits and administrator limits '
                     'still apply.\nhttps://stocks.ednis.ai/')
    else:
        lines.append('No order has been placed. Reply APPROVED to this message before '
                     f'{expires:%H:%M} IST '
                     'for an intraday Upstox limit entry at the recommended price or better '
                     'with linked stop/target, or approve on the '
                     'Intraday page. Admin-configured trade and daily amounts apply; a trading '
                     'OAuth token and live trading must be enabled. Entry may remain unfilled; '
                     'the limit will not follow the market price.\nhttps://stocks.ednis.ai/')
    return '\n'.join(lines)


def deliver(conn, sender=send_intraday_telegram, *, clock=utc_now):
    """Retry only while a completed latest scan still confirms a fresh setup. Each call (a
    stock, direction and candle) is sent once, so a stock called again later in the day
    gets a new message that can be approved by reply.

    Delivery is at-least-once: a crash after Telegram accepts but before commit
    can repeat a message. Database locking suppresses concurrent dispatchers.
    """
    now = require_aware(clock())
    day = now.astimezone(IST).date()
    total = 0
    allowed = eligibility.allowed_instruments(now=now) if trading_window(now) else set()
    auto = conn.execute('select enabled and auto_high_volume_enabled '
                        'from intraday_trading_settings where singleton=true').fetchone()[0]
    with conn.transaction():
        conn.execute("update intraday_telegram set status='expired' where status='pending' "
                     'and (expires_at<=%s or trading_day<>%s)', (now, day))
        run, signals = latest(conn)
        if run and run['status'] == 'running':
            return 0  # wait for the scanner to finish, rather than send an older snapshot
        valid = {s['company_id']: s for s in signals if run['status'] == 'complete'
                 and s['instrument_key'] in allowed
                 and eligible(s['result'], s['observed_at'], now)} if run else {}
        for cid, signal in valid.items():
            r = signal['result']
            conn.execute('''insert into intraday_telegram(company_id,trading_day,action,
                candle_end,scan_id,symbol,result,expires_at,next_attempt_at)
                values(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict(company_id,trading_day,action,candle_end) do update set
                    scan_id=excluded.scan_id,symbol=excluded.symbol,result=excluded.result,
                    expires_at=excluded.expires_at,status='pending'
                where intraday_telegram.sent_at is null''',
                (cid, day, r['action'], dt.datetime.fromisoformat(r['candle_end']),
                 signal['scan_id'], signal['symbol'], Jsonb(r),
                 dt.datetime.fromisoformat(r['expires_at']), now))
    conn.commit()
    # One transaction per message: a later failure cannot roll back earlier acknowledgements.
    keys = conn.execute("select company_id,trading_day,action,candle_end from intraday_telegram "
                        "where status='pending' order by company_id,candle_end").fetchall()
    conn.commit()
    for cid, trading_day, action, candle_end in keys:
        with conn.transaction():
            row = conn.execute('''select symbol,result,next_attempt_at from intraday_telegram
                where company_id=%s and trading_day=%s and action=%s and candle_end=%s
                and status='pending' for update skip locked''',
                (cid, trading_day, action, candle_end)).fetchone()
            if not row:
                continue
            current = clock()
            # Recheck the latest run before each send, including mid-batch scanner changes.
            current_run, current_signals = latest(conn)
            if current_run and current_run['status'] == 'running':
                continue
            match = next((s for s in current_signals if s['company_id'] == cid
                          and s['result']['action'] == action
                          and _candle(s['result']) == candle_end), None)
            key = (cid, trading_day, action, candle_end)
            if (not current_run or current_run['status'] != 'complete' or not match
                    or match['instrument_key'] not in eligibility.allowed_instruments(now=current)
                    or not eligible(match['result'], match['observed_at'], current)
                    or not eligible(row[1], match['observed_at'], current)):
                conn.execute("update intraday_telegram set status='expired' where "
                             'company_id=%s and trading_day=%s and action=%s and candle_end=%s',
                             key)
                continue
            if row[2] > current:
                continue
            ordered = conn.execute("""select 1 from intraday_trade where company_id=%s
                and trading_day=%s and status<>'rejected'""", (cid, trading_day)).fetchone()
            try:
                receipt = sender(message(match['symbol'], match['result'],
                                         automatic=auto and exceptional_volume(match['result']),
                                         ordered=ordered is not None))
                if not receipt:
                    raise RuntimeError('Telegram unavailable')
            except Exception as exc:  # noqa: BLE001 - never persist transport URLs/tokens
                conn.execute('''update intraday_telegram set attempts=attempts+1,last_error=%s,
                    next_attempt_at=%s where company_id=%s and trading_day=%s and action=%s
                    and candle_end=%s''',
                    (type(exc).__name__, current+dt.timedelta(minutes=1), *key))
            else:
                conn.execute('''update intraday_telegram set status='sent',sent_at=%s,
                    attempts=attempts+1,last_error=null,scan_id=%s,result=%s,
                    telegram_message_id=%s
                    where company_id=%s and trading_day=%s and action=%s and candle_end=%s''',
                    (current, match['scan_id'], Jsonb(match['result']),
                     receipt if type(receipt) is int else None, *key))
                total += 1
        conn.commit()
    return total
