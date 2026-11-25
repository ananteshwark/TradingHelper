"""Large trading as it happens (docs/TRADE_SURGES.md). Nothing here places an order.

The exchanges name the traders of large deals only afterwards: block deals after each block
window, bulk deals after the close (docs/INSTITUTIONAL_DEALS.md). What can be seen while it
happens is how much a stock trades.

Every intraday scan (every 5 minutes, 09:20 to 15:30), Upstox's full market quote gives each
liquid stock's volume so far today; the volume since the previous scan is what traded in
those minutes. A window in which a stock traded at least `min_share_of_day` of a normal day's
value (the median of its previous 20 sessions) and at least `min_value_cr` crore is a surge:
- **recorded**, with the price's move over the window and the order book at the scan;
- **sent** to the deals chat, the first one of each stock in a day, within 20 minutes;
- **followed:** the stock's move to the close, and the next session against the Nifty 500,
  and whether that evening's bulk or block deal file named someone in the stock.

Who traded is not known: a surge can be one institution, several, or many smaller traders.
"""
from __future__ import annotations

import datetime as dt
import time

from psycopg.rows import dict_row

from igs.timeutil import IST, utc_now

T = dt.time
DEFAULTS = {'enabled': True, 'min_share_of_day': 0.10, 'min_value_cr': 10.0}
SCAN_FROM, WINDOWS_UNTIL = T(9, 20), T(15, 35)   # the closing session starts at 15:40
MAX_GAP = dt.timedelta(minutes=12)               # a longer gap between scans isn't a window
ALERT_LATE = dt.timedelta(minutes=20)
MIN_VALUE = 2e7                                  # the spike watcher's liquid stocks
QUOTES = 500
PAUSE = 0.1
FOLLOW_DAYS = 60
LOCK = 739182515


def rule(cfg=None) -> dict:
    """rules.trade_surges in config/alerts.yaml over DEFAULTS."""
    if cfg is None:
        from igs.config import load_alerts
        cfg = load_alerts()
    return {**DEFAULTS, **(cfg.rules.get('trade_surges') or {})}


def window(before: dict | None, quote: dict, now: dt.datetime, day_value: float,
           r: dict) -> dict | None:
    """The surge between the previous scan's `before` (taken_at, volume, last_price) and
    `quote`, or None: a gap of at most MAX_GAP, volume that grew, and a traded value of at
    least min_share_of_day of `day_value` and min_value_cr crore. The value is the volume at
    the average of the two prices."""
    if before is None or day_value <= 0:
        return None
    gap = now - before['taken_at']
    volume = quote['volume'] - before['volume']
    if not dt.timedelta(0) < gap <= MAX_GAP or volume <= 0:
        return None
    price_from, price_to = float(before['last_price']), quote['last_price']
    value = volume * (price_from + price_to) / 2
    share = value / day_value
    if share < float(r['min_share_of_day']) or value < float(r['min_value_cr']) * 1e7:
        return None
    return {'window_from': before['taken_at'], 'window_to': now, 'volume': volume,
            'value_inr': value, 'day_value_inr': day_value, 'share_of_day': share,
            'price_from': price_from, 'price_to': price_to,
            'move_pct': (price_to / price_from - 1) * 100,
            'average_price': quote.get('average_price'), 'bid_qty': quote.get('bid_qty'),
            'offer_qty': quote.get('offer_qty')}


def _quotes(feed, keys, pause) -> dict:
    out = {}
    for start in range(0, len(keys), QUOTES):
        out.update(feed.quotes(keys[start:start + QUOTES]))
        pause(PAUSE)
    return out


def step(conn, feed=None, *, clock=utc_now, pause=time.sleep, notify=None,
         cfg=None) -> str:
    """The intraday job's part: quote every liquid stock, record the surges since the
    previous scan, send the new ones, and follow earlier ones. Returns what it did."""
    if not conn.execute('select pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0]:
        return 'Surges: another run is in progress'
    conn.commit()
    own = feed is None
    try:
        r = rule(cfg)
        now = clock()
        followed = follow(conn, now)
        if not r['enabled']:
            return 'Surges: off'
        local = now.astimezone(IST)
        if local.weekday() >= 5 or not SCAN_FROM <= local.time() < WINDOWS_UNTIL:
            return f'Surges: outside the session; {followed} followed'
        if own:
            from igs.intraday.scanner import token
            from igs.intraday.upstox import Upstox
            if not token():
                return 'Surges: no Upstox access token'
            feed = Upstox(token())
        from igs.intraday.spikes import universe
        stocks = universe(conn, local.date(), MIN_VALUE)
        if not stocks:
            return 'Surges: no liquid stocks; is the bhavcopy loaded?'
        quotes = _quotes(feed, [s['instrument_key'] for s in stocks], pause)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""select instrument_key, taken_at, volume, last_price
                           from trade_surge_snapshot where trade_date = %s""", (local.date(),))
            before = {row['instrument_key']: row for row in cur.fetchall()}
        found = 0
        for s in stocks:
            q = quotes.get(s['instrument_key'])
            if q is None:
                continue
            w = window(before.get(s['instrument_key']), q, now, s['value'], r)
            if w:
                found += conn.execute("""insert into trade_surge (trade_date, instrument_key,
                        isin, symbol, company_id, window_from, window_to, volume, value_inr,
                        day_value_inr, share_of_day, price_from, price_to, move_pct,
                        average_price, bid_qty, offer_qty)
                    values (%(day)s, %(key)s, %(isin)s, %(symbol)s,
                            (select s.company_id from security_identifier si
                             join security s using (security_id)
                             where si.id_type = 'ISIN' and si.id_value = %(isin)s
                               and si.valid_from <= %(day)s
                               and (si.valid_to is null or si.valid_to > %(day)s) limit 1),
                            %(window_from)s, %(window_to)s, %(volume)s, %(value_inr)s,
                            %(day_value_inr)s, %(share_of_day)s, %(price_from)s,
                            %(price_to)s, %(move_pct)s, %(average_price)s, %(bid_qty)s,
                            %(offer_qty)s)
                    on conflict do nothing""",
                                      {**w, 'day': local.date(), 'key': s['instrument_key'],
                                       'isin': s['isin'], 'symbol': s['symbol']}).rowcount
            conn.execute("""insert into trade_surge_snapshot (trade_date, instrument_key,
                    taken_at, volume, last_price) values (%s, %s, %s, %s, %s)
                on conflict (trade_date, instrument_key) do update set
                    taken_at = excluded.taken_at, volume = excluded.volume,
                    last_price = excluded.last_price""",
                         (local.date(), s['instrument_key'], now, q['volume'],
                          q['last_price']))
        conn.execute('delete from trade_surge_snapshot where trade_date < %s', (local.date(),))
        conn.commit()
        sent = send(conn, now, notify)
        return (f'Surges: {len(quotes)} quoted, {found} found, {sent} alerted, '
                f'{followed} followed')
    finally:
        conn.rollback()
        if own and feed is not None:
            feed.close()
        conn.execute('select pg_advisory_unlock(%s)', (LOCK,))
        conn.commit()


def _hm(t: dt.datetime) -> str:
    return f'{t.astimezone(IST):%H:%M}'


def message(rows: list[dict]) -> str:
    lines = [f"LARGE TRADING NOW (information, not a call): {len(rows)} stock"
             f"{'s' if len(rows) != 1 else ''} traded a large part of a normal day in a few "
             "minutes."]
    for r in rows:
        move = float(r['move_pct'])
        book = ''
        bid, offer = float(r['bid_qty'] or 0), float(r['offer_qty'] or 0)
        if bid > 0 and offer > 0:
            book = (f"; bids {bid / offer:.1f}× offers in the order book" if bid >= offer
                    else f"; offers {offer / bid:.1f}× bids in the order book")
        lines.append(
            f"{r['symbol']}: ₹{float(r['value_inr']) / 1e7:,.1f} crore traded "
            f"{_hm(r['window_from'])}–{_hm(r['window_to'])}, "
            f"{float(r['share_of_day']):.0%} of a normal day's value; price "
            f"{'up' if move >= 0 else 'down'} {abs(move):.1f}% (₹{float(r['price_from']):,.2f} "
            f"→ ₹{float(r['price_to']):,.2f}){book}.")
    lines.append("Who traded is not known yet: NSE names large traders only in its block "
                 "deal file after each block window and its bulk deal file after the close. "
                 "The Institutional deals page keeps what followed each surge.")
    return '\n'.join(lines)


def send(conn, now: dt.datetime, notify=None) -> int:
    """Today's new surges in one message: the first of each stock in the day; a later one
    is a repeat, and one found more than ALERT_LATE after its window is late. Kept unsent
    (and retried at the next scan) while Telegram is unavailable."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from trade_surge where trade_date = %s
                         and alert_status is null order by window_to, symbol""",
                    (now.astimezone(IST).date(),))
        rows = cur.fetchall()
        cur.execute("""select distinct instrument_key from trade_surge
                       where trade_date = %s and alert_status = 'sent'""",
                    (now.astimezone(IST).date(),))
        sent_before = {r['instrument_key'] for r in cur.fetchall()}
    fresh, seen = [], set(sent_before)
    for r in rows:
        status = ('repeat' if r['instrument_key'] in seen
                  else 'late' if now - r['window_to'] > ALERT_LATE else None)
        if status:
            conn.execute('update trade_surge set alert_status = %s where surge_id = %s',
                         (status, r['surge_id']))
        else:
            fresh.append(r)
            seen.add(r['instrument_key'])
    conn.commit()
    if not fresh:
        return 0
    text = message(fresh)
    if notify is None:
        from igs.alerts.delivery import send_deals as notify
    if not notify(text):
        return 0
    conn.execute("""update trade_surge set alert_status = 'sent', alerted_at = %s,
                        alert_text = %s where surge_id = any(%s)""",
                 (now, text, [r['surge_id'] for r in fresh]))
    conn.commit()
    return len(fresh)


def follow(conn, now: dt.datetime) -> int:
    """The day's close and the next session against the Nifty 500 for earlier surges, once
    the prices are in. Returns how many were filled."""
    from igs.institutions import outcomes
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select surge_id, trade_date, isin, company_id, price_to, close_price
                       from trade_surge
                       where (close_price is null or next_vs_index_pct is null)
                         and trade_date >= %s and trade_date < %s""",
                    (now.astimezone(IST).date() - dt.timedelta(days=FOLLOW_DAYS),
                     now.astimezone(IST).date() + dt.timedelta(days=1)))
        rows = cur.fetchall()
    done = 0
    for r in rows:
        close = r['close_price']
        if close is None:
            got = conn.execute("""select close from price_eod where exchange = 'NSE'
                and isin = %s and trade_date = %s order by series = 'EQ' desc limit 1""",
                               (r['isin'], r['trade_date'])).fetchone()
            if not got:
                continue
            close = got[0]
        nxt = outcomes(conn, r).get('next session') if r['company_id'] else None
        conn.execute("""update trade_surge set close_price = %s, to_close_pct = %s,
                            next_vs_index_pct = %s where surge_id = %s""",
                     (close, (float(close) / float(r['price_to']) - 1) * 100, nxt,
                      r['surge_id']))
        done += 1
    conn.commit()
    return done


NAMED = """(select string_agg(distinct d.institution || ' (' || d.kind || ', ' || d.side
                                || ')', '; ')
             from institution_deal d where d.trade_date = s.trade_date
               and d.symbol = s.symbol)"""


def recent(conn, limit: int = 300) -> list[dict]:
    """The latest surges, with the institutions the day's deal files named in the stock."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""select s.*, {NAMED} as named from trade_surge s
                        order by s.window_to desc limit %s""", (limit,))
        return cur.fetchall()


def record(conn) -> dict:
    """What followed the surges, signed for the way the price moved in the window: the
    move to the close and the next session against the Nifty 500, and how many the day's
    deal files named an institution in."""
    row = conn.execute(f"""select count(*),
            count(to_close_pct),
            avg(case when move_pct >= 0 then to_close_pct else -to_close_pct end)::float8,
            count(next_vs_index_pct),
            avg(case when move_pct >= 0 then next_vs_index_pct
                     else -next_vs_index_pct end)::float8,
            count(*) filter (where {NAMED} is not null)
        from trade_surge s""").fetchone()
    return {'surges': row[0], 'with_close': row[1], 'to_close_pct': row[2],
            'with_next': row[3], 'next_vs_index_pct': row[4], 'named': row[5]}
