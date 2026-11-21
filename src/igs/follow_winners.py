"""Following the winning traders of NSE's bulk deal files, on paper (docs/DEAL_TRADERS.md,
"Following them").

Their same-day bets can't be copied: the file appears after the close. What a consistent
winner (igs.deal_traders) **carried** at the close, having bought more than they sold or sold
more than they bought, can be followed the next session:
- **the side:** a buy when they carried a net purchase, a short sale when a net sale;
- **the trade:** intraday, about ₹1 lakh, in at the next session's open and out at its close
  (the bhavcopy's), with slippage both ways and Upstox's intraday charges;
- **who counts:** only traders already named consistent winners when the file showing the
  position is read, and only while the next session has not opened, so every trade could
  have been placed.

The day's trades go to Telegram (the paper chat) before the open; the results once the
session's prices are in. Nothing here places an order. Real orders are not offered until the
paper record clears BAR: enough trades, positive after charges, and unlikely to be luck.
"""
from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

from psycopg.rows import dict_row

from igs.timeutil import IST, utc_now

DEFAULTS = {'enabled': True, 'notional_inr': 100000, 'slippage_pct': 0.05,
            'min_carried_cr': 0.25, 'max_per_day': 10}
BAR = {'trades': 60, 't': 2.0}
OPEN = dt.time(9, 15)
MAX_AGE = dt.timedelta(days=3)          # results not sent by then are left unsent
VERB = {1: 'BUY', -1: 'SHORT SELL'}


def rule(cfg=None) -> dict:
    """rules.follow_winners in config/alerts.yaml over DEFAULTS."""
    if cfg is None:
        from igs.config import load_alerts
        cfg = load_alerts()
    return {**DEFAULTS, **(cfg.rules.get('follow_winners') or {})}


def next_session(conn, day: dt.date) -> dt.date:
    """The session after `day`: the next weekday that is not an NSE holiday."""
    from igs.results_drift import holidays, next_sessions
    return next_sessions(day, 1, [], holidays(conn))[0]


def signals(conn, r: dict, now: dt.datetime) -> list[dict]:
    """New paper trades: the consistent winners' carried positions in the deal files read so
    far, for sessions that have not opened. One per stock and day: winners on one side add
    up, opposite sides cancel the stock. The largest first, at most max_per_day a day."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            select b.trade_date, b.symbol, b.company_id, b.client, b.net_qty::float8 as net,
                   coalesce(case when b.net_qty > 0 then b.buy_price else b.sell_price end,
                            0)::float8 as price
            from deal_trader_bet b
            join deal_trader_flag f on f.client = b.client
            join deal_trader_scan s on s.content_hash = b.source_hash
            where b.net_qty <> 0 and b.company_id is not null
              and f.flagged_at <= s.scanned_at and b.trade_date >= %s
              and not exists (select 1 from follow_trade t where t.signal_date = b.trade_date
                              and t.symbol = b.symbol)
            order by b.trade_date, b.symbol, b.client""",
                    (now.astimezone(IST).date() - dt.timedelta(days=7),))
        rows = cur.fetchall()
    by_stock: dict[tuple, dict] = {}
    for b in rows:
        s = by_stock.setdefault((b['trade_date'], b['symbol']), {
            'signal_date': b['trade_date'], 'symbol': b['symbol'],
            'company_id': b['company_id'], 'sides': set(), 'clients': [], 'value': 0.0})
        s['sides'].add(1 if b['net'] > 0 else -1)
        s['clients'].append(b['client'])
        s['value'] += abs(b['net']) * b['price']
    out = []
    for s in by_stock.values():
        if len(s['sides']) != 1 or s['value'] < float(r['min_carried_cr']) * 1e7:
            continue
        session = next_session(conn, s['signal_date'])
        if now >= dt.datetime.combine(session, OPEN, IST):
            continue                    # too late: the session it is for has opened
        out.append({**s, 'side': s['sides'].pop(), 'session': session,
                    'clients': '; '.join(sorted(set(s['clients'])))})
    out.sort(key=lambda s: (s['signal_date'], -s['value'], s['symbol']))
    kept, per_day = [], {}
    for s in out:
        per_day[s['signal_date']] = per_day.get(s['signal_date'], 0) + 1
        if per_day[s['signal_date']] <= int(r['max_per_day']):
            kept.append(s)
    return kept


def open_trades(conn, r: dict, now: dt.datetime) -> int:
    """Record the new paper trades; returns how many."""
    made = 0
    for s in signals(conn, r, now):
        made += conn.execute("""insert into follow_trade (signal_date, symbol, company_id,
                side, clients, carried_inr, session, status, created_at)
            values (%s, %s, %s, %s, %s, %s, %s, 'open', %s)
            on conflict (signal_date, symbol) do nothing""",
                             (s['signal_date'], s['symbol'], s['company_id'], s['side'],
                              s['clients'], s['value'], s['session'], now)).rowcount
    conn.commit()
    return made


def settle(conn, r: dict, now: dt.datetime) -> int:
    """Settle each open trade once the session after its signal has prices: in at the open,
    out at the close, slippage both ways, Upstox's intraday charges. A stock locked at its
    price band (no range, or opening and closing at the extreme against the entry) gives no
    fill; one traded only trade-for-trade (not series EQ) allows no intraday trade."""
    from igs.intraday.costs import charges, intraday_rates
    rates = intraday_rates()
    slip = float(r['slippage_pct']) / 100
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from follow_trade where status = 'open' order by trade_id")
        trades = cur.fetchall()
    closed = 0                          # settled, filled or not
    for t in trades:
        day = conn.execute("""select min(trade_date) from price_eod where exchange = 'NSE'
            and symbol = %s and trade_date > %s""", (t['symbol'], t['signal_date'])).fetchone()[0]
        if day is None:
            continue
        px = conn.execute("""select open::float8, high::float8, low::float8, close::float8
            from price_eod where exchange = 'NSE' and symbol = %s and trade_date = %s
              and series = 'EQ'""", (t['symbol'], day)).fetchone()
        side = t['side']
        status = 'closed'
        if px is None:
            status = 'not intraday'
        else:
            open_, high, low, close = px
            locked = high == low or (side == 1 and open_ == high == close) \
                or (side == -1 and open_ == low == close)
            if not open_ or open_ <= 0 or locked:
                status = 'no fill'
        if status != 'closed':
            conn.execute("""update follow_trade set status = %s, trade_date = %s,
                settled_at = %s where trade_id = %s""", (status, day, now, t['trade_id']))
            closed += 1
            continue
        entry = round(open_ * (1 + side * slip), 2)
        exit_ = round(close * (1 - side * slip), 2)
        qty = int(float(r['notional_inr']) // entry)
        if qty < 1:
            conn.execute("""update follow_trade set status = 'no fill', trade_date = %s,
                settled_at = %s where trade_id = %s""", (day, now, t['trade_id']))
            closed += 1
            continue
        cost = float(charges(qty, Decimal(str(entry)), Decimal(str(exit_)),
                             'buy' if side == 1 else 'sell', rates))
        gross = side * (exit_ - entry) * qty
        net = gross - cost
        conn.execute("""update follow_trade set status = 'closed', trade_date = %s,
                open_price = %s, close_price = %s, entry = %s, exit = %s, quantity = %s,
                gross_inr = %s, charges_inr = %s, net_inr = %s, net_pct = %s, settled_at = %s
            where trade_id = %s""",
                     (day, open_, close, entry, exit_, qty, gross, cost, net,
                      net / (entry * qty) * 100, now, t['trade_id']))
        closed += 1
    conn.commit()
    return closed


def record(conn) -> dict:
    """The paper record after charges: trades, won, net rupees, the average a trade, and the
    t-statistic of the daily results."""
    rows = conn.execute("""select trade_date, sum(net_inr)::float8, count(*),
            count(*) filter (where net_inr > 0), sum(net_pct)::float8
        from follow_trade where status = 'closed' group by trade_date
        order by trade_date""").fetchall()
    days = [r[1] for r in rows]
    n = sum(r[2] for r in rows)
    out = {'trades': n, 'won': sum(r[3] for r in rows), 'days': len(days),
           'net_inr': sum(days), 'avg_pct': sum(r[4] for r in rows) / n if n else None,
           't': None}
    if len(days) > 2:
        mean = sum(days) / len(days)
        sd = math.sqrt(sum((d - mean) ** 2 for d in days) / (len(days) - 1))
        out['t'] = mean / sd * math.sqrt(len(days)) if sd > 0 else None
    out['passes'] = (n >= BAR['trades'] and out['net_inr'] > 0
                     and out['t'] is not None and out['t'] >= BAR['t'])
    return out


def bar_text(rec: dict) -> str:
    if rec['passes']:
        return (f"the paper record clears the bar ({BAR['trades']} trades or more, positive "
                f"after charges, t of {BAR['t']:.0f} or more): real orders can be considered.")
    return (f"none until the paper record clears the bar: {BAR['trades']} trades or more, "
            f"positive after charges, and a t of {BAR['t']:.0f} or more (so far "
            f"{rec['trades']} trades).")


def trade_message(trades: list[dict], r: dict) -> str:
    lines = [f"FOLLOWING WINNING TRADERS (paper): {len(trades)} trade"
             f"{'s' if len(trades) != 1 else ''} for {trades[0]['session']:%d %b}"]
    for t in trades:
        lines.append(f"{VERB[t['side']]} {t['symbol']}: {t['clients']} carried a net "
                     f"{'purchase' if t['side'] == 1 else 'sale'} of "
                     f"₹{float(t['carried_inr']) / 1e7:,.2f} crore at the close on "
                     f"{t['signal_date']:%d %b}.")
    lines.append(f"Paper trades of about ₹{float(r['notional_inr']):,.0f} each: in at the "
                 "open, out at the close, after charges. The results follow once the day's "
                 "prices are in.")
    return '\n'.join(lines)


def result_message(trades: list[dict], rec: dict) -> str:
    done = [t for t in trades if t['status'] == 'closed']
    total = sum(float(t['net_inr']) for t in done)
    lines = [f"FOLLOWING WINNING TRADERS (paper): {len(trades)} trade"
             f"{'s' if len(trades) != 1 else ''} settled, ₹{total:+,.0f} after charges"]
    for t in trades:
        if t['status'] != 'closed':
            lines.append(f"{VERB[t['side']]} {t['symbol']} on {t['trade_date']:%d %b}: "
                         f"{t['status']}")
            continue
        lines.append(f"{VERB[t['side']]} {t['symbol']} on {t['trade_date']:%d %b}: "
                     f"₹{float(t['open_price']):,.2f} at the open, "
                     f"₹{float(t['close_price']):,.2f} at the close, "
                     f"₹{float(t['net_inr']):+,.0f} after charges "
                     f"({float(t['net_pct']):+.2f}%)")
    lines.append(f"Since the start: {rec['trades']} trades, {rec['won']} won, "
                 f"₹{rec['net_inr']:+,.0f} after charges"
                 + (f", t {rec['t']:.2f}" if rec['t'] is not None else '') + '.')
    lines.append('Real orders: ' + bar_text(rec))
    return '\n'.join(lines)


def alert(conn, r: dict, notify, now: dt.datetime) -> int:
    """Send what is due, oldest first: each session's new trades before it opens, then the
    results of settled trades (for up to 3 days). A message not sent is retried next run."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from follow_trade where signal_sent_at is null
            and status = 'open' order by session, trade_id""")
        pending = [t for t in cur.fetchall()
                   if now < dt.datetime.combine(t['session'], OPEN, IST)]
        cur.execute("""select * from follow_trade where result_sent_at is null
            and settled_at is not null and settled_at > %s order by trade_date, trade_id""",
                    (now - MAX_AGE,))
        settled = cur.fetchall()
    sent = 0
    for session in sorted({t['session'] for t in pending}):
        group = [t for t in pending if t['session'] == session]
        if not notify(trade_message(group, r)):
            return sent
        conn.execute('update follow_trade set signal_sent_at = %s where trade_id = any(%s)',
                     (now, [t['trade_id'] for t in group]))
        conn.commit()
        sent += 1
    if settled:
        if not notify(result_message(settled, record(conn))):
            return sent
        conn.execute('update follow_trade set result_sent_at = %s where trade_id = any(%s)',
                     (now, [t['trade_id'] for t in settled]))
        conn.commit()
        sent += 1
    return sent


def step(conn, notify=None, now: dt.datetime | None = None) -> str:
    """The deals job's part, after igs.deal_traders: settle, open the next trades, send."""
    from igs.alerts.delivery import TelegramError
    r = rule()
    if not r['enabled']:
        return 'Following winners: off'
    now = now or utc_now()
    if notify is None:
        from igs.alerts.delivery import send_paper as notify
    closed = settle(conn, r, now)
    opened = open_trades(conn, r, now)
    try:
        sent = alert(conn, r, notify, now)
    except TelegramError as exc:
        conn.rollback()
        return (f'Following winners: {closed} settled, {opened} opened; messages wait for '
                f'the next run: {exc}')
    return f'Following winners: {closed} settled, {opened} opened, {sent} messages sent'
