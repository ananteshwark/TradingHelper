"""The spike watcher and the spike-reversal paper track (docs/SPIKES.md,
research/spike_reversal/PAPER.md). Nothing here places an order.

Every 5 minutes in the session (the intraday job), for every liquid NSE stock:

- **an alert** to the spikes chat when it moves 12% or more within 30 minutes, once a stock
  a day (`intraday_spikes` in config/alerts.yaml);
- **the paper track:** against the move at the first flat 5-minute candle, a 4% target,
  out at 15:15, but never a short sale within 4% of the upper circuit (price band), nor a
  buy within 4% of the lower one: the band rule (B). Every spike (A) is kept beside it as
  the comparison that shows what the rule saves.

Upstox's day high and low (500 stocks a request) pick the stocks whose range allows a
spike; only their 5-minute candles are fetched. Each scan rebuilds a stock's spike and
trade from its closed candles, so a late or repeated scan gives the same answer.
"""
from __future__ import annotations

import datetime as dt
import math
import time
from decimal import Decimal

from psycopg.rows import dict_row

from igs.intraday import price_bands
from igs.intraday.costs import charges, intraday_rates
from igs.intraday.engine import BAR, session
from igs.intraday.upstox import FeedError
from igs.timeutil import IST, utc_now

T = dt.time
MOVE, FLAT, TARGET, BAND_ROOM = 0.12, 0.01, 0.04, 0.04
NOTIONAL, SLIP = 100000, 0.0015
MIN_VALUE = 2e7                  # Rs 2 crore a day, median of the previous 20 sessions
WINDOW = 25                      # minutes from the first candle's start to the last's
OPEN, LAST_SIGNAL, EXIT = T(9, 15), T(14, 55), T(15, 15)
SCAN_FROM, RESULTS_FROM, CLOSE_FROM, SCAN_UNTIL = T(9, 20), T(15, 20), T(15, 30), T(15, 40)
ALERT_LATE = dt.timedelta(minutes=30)
QUOTES = 500                     # keys a market-quote request
MAX_CANDLES = 60                 # stocks whose candles one scan fetches
PAUSE = 0.1
LOCK = 739182513
DEFAULTS = {'enabled': True, 'min_move_pct': 12, 'min_value_cr': 2}
FINAL = ('closed', 'no flat', 'none', 'not tracked')
MIN_TRADES = 100                 # before the band rule's record is judged (PAPER.md)


def rule(cfg=None) -> dict:
    """rules.intraday_spikes in config/alerts.yaml over DEFAULTS: the alerts only; the
    paper track keeps 12% and Rs 2 crore."""
    if cfg is None:
        from igs.config import load_alerts
        cfg = load_alerts()
    return {**DEFAULTS, **(cfg.rules.get('intraday_spikes') or {})}


# --------------------------------------------------------------------------- the rule


def _clock(bar) -> T:
    return bar.start.astimezone(IST).time()


def _minutes(bar) -> int:
    t = _clock(bar)
    return t.hour * 60 + t.minute


def spike(bars, move: float, last: T | None = None):
    """The first candle whose close is `move` above the lowest low of the candles starting
    from 25 minutes before it (side -1: a rise, faded by a short sale) or `move` below
    their highest high (side 1): (its index, side, the window's first index), or None.
    Only candles starting by `last` count."""
    first = 0
    for i, bar in enumerate(bars):
        if last is not None and _clock(bar) > last:
            return None
        while _minutes(bars[first]) < _minutes(bar) - WINDOW:
            first += 1
        if bar.close >= (1 + move) * min(b.low for b in bars[first:i + 1]):
            return i, -1, first
        if bar.close <= (1 - move) * max(b.high for b in bars[first:i + 1]):
            return i, 1, first
    return None


def flat_candle(bars, i: int):
    """The first candle from `i`, starting by 14:55, with a body within 1% of its open and
    a range: a candle without one is a halt or a circuit lock, not a stall."""
    for j in range(i, len(bars)):
        bar = bars[j]
        if _clock(bar) > LAST_SIGNAL:
            return None
        if abs(bar.close - bar.open) <= FLAT * bar.open and bar.high > bar.low:
            return j
    return None


def trade(bars, now: dt.datetime) -> dict:
    """The day's paper trade from its closed candles: status 'none' (no spike by 14:55),
    'waiting' (a spike, no entry yet), 'no flat', 'open' or 'closed', with its prices."""
    local = now.astimezone(IST)
    found = spike(bars, MOVE, LAST_SIGNAL)
    if found is None:
        return {'status': 'none' if local.time() >= T(15, 0) else 'watching'}
    i, side, first = found
    window = bars[first:i + 1]
    extreme = min(b.low for b in window) if side < 0 else max(b.high for b in window)
    out = {'side': side, 'trigger_bar': bars[i].start,
           'move_pct': round((bars[i].close / extreme - 1) * 100, 3)}
    j = flat_candle(bars, i)
    if j is None:
        return {**out, 'status': 'no flat' if local.time() >= T(15, 0) else 'waiting'}
    out['flat_bar'] = bars[j].start
    if j + 1 >= len(bars):
        return {**out, 'status': 'waiting'}
    k = j + 1
    if _clock(bars[k]) >= EXIT:
        return {**out, 'status': 'no flat'}
    entry = bars[k].open
    filled = entry * (1 + side * SLIP)
    target = filled * (1 + side * TARGET)
    out.update(entry_bar=bars[k].start, entry=entry, target=round(target, 4))
    for bar in bars[k:]:
        if _clock(bar) >= EXIT:
            return {**out, **_exit(bar, bar.open, 'time')}
        if (bar.low <= target) if side < 0 else (bar.high >= target):
            beyond = (bar.open <= target) if side < 0 else (bar.open >= target)
            return {**out, **_exit(bar, bar.open if beyond else target, 'target')}
    if local.time() >= CLOSE_FROM:          # no candle from 15:15: out at the last close
        return {**out, **_exit(bars[-1], bars[-1].close, 'time')}
    return {**out, 'status': 'open'}


def _exit(bar, price: float, why: str) -> dict:
    return {'status': 'closed', 'exit_bar': bar.start, 'exit': price, 'exit_reason': why,
            'locked': bar.high == bar.low}


def result(side: int, entry: float, exit_price: float, why: str, rates=None) -> dict | None:
    """Net of 0.15% slippage a side on the entry and a 15:15 exit (none on the target, a
    limit order) and Upstox's intraday charges, on Rs 1 lakh."""
    rates = rates or intraday_rates()
    e = entry * (1 + side * SLIP)
    x = exit_price if why == 'target' else exit_price * (1 - side * SLIP)
    qty = int(NOTIONAL // e)
    if qty < 1:
        return None
    cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                         'buy' if side == 1 else 'sell', rates))
    net = side * (x - e) * qty - cost
    return {'quantity': qty, 'gross_pct': round(side * (exit_price / entry - 1) * 100, 4),
            'charges_inr': round(cost, 2), 'net_inr': round(net, 2),
            'net_pct': round(net / (e * qty) * 100, 4)}


def band_check(bands: dict | None, symbol: str, prev_close, side: int, entry: float) -> dict:
    """The band rule (B): a trade is left out when the band on its losing side (the upper
    one for a short sale, the lower for a buy) is less than 4% from the entry. Stocks with
    no fixed band are kept; one whose band can't be found is left out."""
    if bands is None or symbol not in bands:
        return {'band_status': 'unknown', 'band_takes': False}
    pct = bands[symbol]
    if pct is None:
        return {'band_status': 'no band', 'band_takes': True}
    if prev_close is None:
        return {'band_status': 'unknown', 'band_takes': False}
    limit = float(prev_close) * (1 - side * pct / 100)
    room = (limit / entry - 1) if side < 0 else (1 - limit / entry)
    return {'band_status': 'fixed', 'band_pct': pct, 'band_limit': round(limit, 4),
            'band_room_pct': round(room * 100, 3), 'band_takes': room >= BAND_ROOM}


# --------------------------------------------------------------------------- the scan


def universe(conn, day: dt.date, floor: float) -> list[dict]:
    """NSE EQ stocks with a median traded value of at least `floor` rupees over their
    previous 20 sessions, still trading at the last one, and that median."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            with last as (select max(trade_date) d from price_eod
                          where exchange = 'NSE' and trade_date < %(day)s),
            recent as (
                select isin, symbol, trade_date, turnover_inr,
                       row_number() over (partition by isin order by trade_date desc) n
                from price_eod where exchange = 'NSE' and series = 'EQ'
                  and trade_date < %(day)s and trade_date > %(day)s - 60)
            select isin, (array_agg(symbol order by trade_date desc))[1] symbol,
                   'NSE_EQ|' || isin instrument_key,
                   percentile_cont(0.5) within group (order by turnover_inr)::float8 value
            from recent where n <= 20
            group by isin
            having count(*) = 20 and max(trade_date) = (select d from last)
               and percentile_cont(0.5) within group (order by turnover_inr) >= %(floor)s
            order by isin""", {'day': day, 'floor': floor})
        return cur.fetchall()


def _ranges(feed, keys, pause) -> dict:
    out = {}
    for start in range(0, len(keys), QUOTES):
        out.update(feed.day_ranges(keys[start:start + QUOTES]))
        pause(PAUSE)
    return out


def _to_check(conn, day: dt.date) -> list[dict]:
    """Today's stocks still to check, paper trades in progress first, then the longest
    unchecked, then the widest range."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike where trade_date = %s and not done
                       order by status in ('open', 'waiting') desc, checked_at nulls first,
                                day_high / day_low desc
                       limit %s""", (day, MAX_CANDLES))
        return cur.fetchall()


def _update(conn, row: dict, values: dict) -> None:
    sets = ', '.join(f'{k} = %({k})s' for k in values)
    conn.execute(f'update intraday_spike set {sets} where trade_date = %(trade_date)s '
                 f'and instrument_key = %(instrument_key)s',
                 {**values, 'trade_date': row['trade_date'],
                  'instrument_key': row['instrument_key']})


def _check(conn, row: dict, bars, now, alert_move, alerts_on, state: dict) -> None:
    """One stock: its alert spike and its paper trade, from today's closed candles."""
    local = now.astimezone(IST)
    values = {'checked_at': now}
    if alerts_on and row['alert_bar'] is None:
        found = spike(bars, alert_move)
        if found:
            i, side, first = found
            window = bars[first:i + 1]
            start = min(b.low for b in window) if side < 0 else max(b.high for b in window)
            values.update(alert_side=side, alert_bar=bars[i].start, alert_from=start,
                          alert_to=bars[i].close,
                          alert_move_pct=round((bars[i].close / start - 1) * 100, 3))
    if row['paper'] and row['status'] not in FINAL and _clock(bars[0]) != OPEN:
        values['status'] = 'not tracked'        # no 09:15 candle: not a full session
    elif row['paper'] and row['status'] not in FINAL:
        t = trade(bars, now)
        values.update(t)
        if t.get('entry') is not None and row['band_status'] is None:
            if 'bands' not in state:
                try:
                    state['bands'] = price_bands.bands(now=now)
                except FeedError:
                    state['bands'] = None
            prev = price_bands.previous_close(conn, row['isin'], now)
            values.update(band_check(state['bands'], row['symbol'], prev, t['side'],
                                     t['entry']))
        if t['status'] == 'closed':
            values.update(result(t['side'], t['entry'], t['exit'], t['exit_reason'],
                                 state.setdefault('rates', intraday_rates())) or {})
    status = values.get('status', row['status'])
    alert_found = values.get('alert_bar') or row['alert_bar']
    values['done'] = (status in FINAL and (bool(alert_found) or not alerts_on
                                           or local.time() >= CLOSE_FROM))
    _update(conn, row, values)


def step(conn, feed=None, *, clock=utc_now, pause=time.sleep, alert=None, paper=None,
         cfg=None) -> str:
    """The intraday job's spike part. `alert` sends the spike alerts (the spikes chat),
    `paper` the paper track's messages (the paper chat). Returns what it did."""
    if not conn.execute('select pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0]:
        return 'Spikes: another run is in progress'
    conn.commit()
    own = feed is None
    try:
        now = clock()
        local = now.astimezone(IST)
        if local.weekday() >= 5 or not SCAN_FROM <= local.time() < SCAN_UNTIL:
            return 'Spikes: outside the session'
        if own:
            from igs.intraday.scanner import token
            from igs.intraday.upstox import Upstox
            if not token():
                return 'Spikes: no Upstox access token'
            feed = Upstox(token())
        r = rule(cfg)
        alerts_on = bool(r['enabled'])
        alert_move = float(r['min_move_pct']) / 100
        alert_floor = float(r['min_value_cr']) * 1e7
        floor = min(MIN_VALUE, alert_floor) if alerts_on else MIN_VALUE
        stocks = universe(conn, local.date(), floor)
        if not stocks:
            return 'Spikes: no liquid stocks; is the bhavcopy loaded?'
        ranges = _ranges(feed, [s['instrument_key'] for s in stocks], pause)
        lowest = min(MOVE, alert_move) if alerts_on else MOVE
        conn.execute("""insert into intraday_spike_day (trade_date, first_scan, last_scan,
                            stocks) values (%s, %s, %s, %s)
                        on conflict (trade_date) do update set last_scan = excluded.last_scan,
                            stocks = excluded.stocks""",
                     (local.date(), now, now, len(ranges)))
        for s in stocks:
            hl = ranges.get(s['instrument_key'])
            if not hl or hl[0] < (1 + lowest) * hl[1]:
                continue
            paper_ok = s['value'] >= MIN_VALUE
            if not paper_ok and not (alerts_on and s['value'] >= alert_floor):
                continue
            conn.execute("""insert into intraday_spike (trade_date, instrument_key, isin,
                                symbol, paper, day_high, day_low, status)
                            values (%s, %s, %s, %s, %s, %s, %s, %s)
                            on conflict (trade_date, instrument_key) do update set
                                day_high = excluded.day_high, day_low = excluded.day_low""",
                         (local.date(), s['instrument_key'], s['isin'], s['symbol'],
                          paper_ok, hl[0], hl[1], 'watching' if paper_ok else 'not tracked'))
        conn.commit()
        checked, failed, state = 0, 0, {}
        for row in _to_check(conn, local.date()):
            try:
                bars = session(feed.candles(row['instrument_key']), now)
            except FeedError:
                failed += 1
                if failed >= 5:
                    break
                continue
            finally:
                pause(PAUSE)
            if bars:
                _check(conn, row, bars, now, alert_move, alerts_on, state)
            else:
                _update(conn, row, {'checked_at': now})
            conn.commit()
            checked += 1
        done = [f'{len(ranges)} quoted', f'{checked} checked']
        if failed:
            done.append(f'{failed} candle requests failed')
        if alerts_on and alert:
            done.append(f'{send_alerts(conn, now, alert, alert_move * 100)} alerted')
        if paper:
            done.append(f'{send_entries(conn, local.date(), paper)} paper entries sent')
            if local.time() >= RESULTS_FROM and send_results(conn, local.date(), paper):
                done.append('results sent')
        return 'Spikes: ' + ', '.join(done)
    finally:
        if own and feed is not None:
            feed.close()
        conn.rollback()
        conn.execute('select pg_advisory_unlock(%s)', (LOCK,))
        conn.commit()


# --------------------------------------------------------------------------- messages


def _hm(bar_start: dt.datetime, close: bool = False) -> str:
    t = bar_start + BAR if close else bar_start
    return t.astimezone(IST).strftime('%H:%M')


def _band_line(r: dict) -> str:
    if r['band_status'] == 'no band':
        return 'no fixed circuit (a stock with futures)'
    if r['band_status'] == 'unknown':
        return 'its circuit limit isn\'t known'
    edge, way = ('upper', 'above') if r['side'] < 0 else ('lower', 'below')
    return (f"{edge} circuit ₹{float(r['band_limit']):,.2f}, "
            f"{float(r['band_room_pct']):.1f}% {way}")


def alert_message(rows: list[dict], move_pct: float) -> str:
    lines = [f"SPIKE ALERT (information, not a call): {len(rows)} stock"
             f"{'s' if len(rows) > 1 else ''} moved {move_pct:g}% or more within 30 minutes."]
    for r in rows:
        word = 'up' if r['alert_side'] < 0 else 'down'
        lines.append(f"{r['symbol']} {word} {abs(float(r['alert_move_pct'])):.1f}%: "
                     f"₹{float(r['alert_from']):,.2f} → ₹{float(r['alert_to']):,.2f} by "
                     f"{_hm(r['alert_bar'], close=True)}.")
    lines.append("Paper track: against the move at the first flat 5-minute candle, 4% "
                 "target, out at 15:15, but no short sale within 4% of the upper circuit. "
                 "It made about nothing after costs in the 2022–2026 test, so it is on "
                 "paper only (Paper tracking, Spike reversal).")
    return "\n".join(lines)


def send_alerts(conn, now: dt.datetime, sender, move_pct: float = MOVE * 100) -> int:
    """Today's new spikes in one message; one found more than 30 minutes after its candle
    closed is marked late, not sent."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike where trade_date = %s
                         and alert_bar is not null and alert_status is null
                       order by alert_bar, symbol""", (now.astimezone(IST).date(),))
        rows = cur.fetchall()
    late = [r for r in rows if now - (r['alert_bar'] + BAR) > ALERT_LATE]
    fresh = [r for r in rows if r not in late]
    for r in late:
        _update(conn, r, {'alert_status': 'late'})
    conn.commit()
    if not fresh:
        return 0
    if not sender(alert_message(fresh, move_pct)):
        return 0
    for r in fresh:
        _update(conn, r, {'alert_status': 'sent', 'alert_sent_at': now})
    conn.commit()
    return len(fresh)


def entry_message(rows: list[dict]) -> str:
    """The trades the paper track takes, then those it doesn't: a short sale near the
    upper circuit, a buy near the lower one, or a circuit not known."""
    lines = ["Spike reversal (paper):"]
    for r in rows:
        after = 'rise' if r['side'] < 0 else 'fall'
        if r['band_takes']:
            band = _band_line(r)
            lines.append(f"{'SHORT' if r['side'] < 0 else 'BUY'} {r['symbol']} at "
                         f"₹{float(r['entry']):,.2f} ({_hm(r['entry_bar'])} open), after a "
                         f"{abs(float(r['move_pct'])):.1f}% {after}. Target "
                         f"₹{float(r['target']):,.2f}; out at 15:15. "
                         f"{band[:1].upper()}{band[1:]}.")
    for r in rows:
        if not r['band_takes']:
            what = 'short sale' if r['side'] < 0 else 'buy'
            if r['band_status'] == 'unknown':
                why = "its circuit limit isn't known"
            else:
                edge, way = ('upper', 'above') if r['side'] < 0 else ('lower', 'below')
                why = (f"its {edge} circuit ₹{float(r['band_limit']):,.2f} is only "
                       f"{float(r['band_room_pct']):.1f}% {way}")
            lines.append(f"Not taken: {r['symbol']}, a {what} at ₹{float(r['entry']):,.2f} "
                         f"after a {abs(float(r['move_pct'])):.1f}% {after}; {why}.")
    return "\n".join(lines)


def send_entries(conn, day: dt.date, sender) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike where trade_date = %s
                         and entry is not null and entry_sent_at is null
                       order by entry_bar, symbol""", (day,))
        rows = cur.fetchall()
    if not rows or not sender(entry_message(rows)):
        return 0
    for r in rows:
        _update(conn, r, {'entry_sent_at': utc_now()})
    conn.commit()
    return len(rows)


def send_results(conn, day: dt.date, sender) -> bool:
    """After 15:20, once every paper trade of the day is closed: the day's results and
    both records since the start."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike where trade_date = %s and paper
                       order by entry_bar nulls last, symbol""", (day,))
        rows = cur.fetchall()
    if any(r['status'] in ('waiting', 'open') for r in rows):
        return False
    todo = [r for r in rows if r['status'] == 'closed' and r['result_sent_at'] is None]
    if not todo:
        return False
    taken = [r for r in todo if r['band_takes']]
    lines = [f"Spike reversal (paper), {day:%d %b}: {len(taken)} trade"
             f"{'' if len(taken) == 1 else 's'}."]
    for r in taken:
        what = 'SHORT' if r['side'] < 0 else 'BUY'
        how = 'target' if r['exit_reason'] == 'target' else 'out at 15:15'
        lines.append(f"{what} {r['symbol']}: {how}, ₹{float(r['net_inr']):+,.0f} "
                     f"({float(r['net_pct']):+.2f}%).")
    for r in todo:
        if not r['band_takes']:
            what = 'short sale' if r['side'] < 0 else 'buy'
            lines.append(f"Not taken, near a circuit: {r['symbol']} ({what}) would have "
                         f"made ₹{float(r['net_inr']):+,.0f}"
                         f"{', ending locked' if r['locked'] else ''}.")
    rec = record(conn)
    for key, name in (('band', 'Paper track'), ('all', 'Every spike, for comparison')):
        today = sum(float(r['net_inr']) for r in todo if key == 'all' or r['band_takes'])
        s = rec[key]
        lines.append(f"{name}: ₹{today:+,.0f} today; ₹{s['net_inr']:+,.0f} since the start "
                     f"({s['trades']} trades).")
    if not sender("\n".join(lines)):
        return False
    for r in todo:
        _update(conn, r, {'result_sent_at': utc_now()})
    conn.commit()
    return True


# --------------------------------------------------------------------------- the record


def _summary(trades: list[dict], days: list[dt.date]) -> dict:
    """Trades, rupees and the daily P&L over every session the job ran (PAPER.md)."""
    daily = {d: 0.0 for d in days}
    for t in trades:
        daily[t['trade_date']] = daily.get(t['trade_date'], 0.0) + float(t['net_inr'])
    xs = list(daily.values())
    n = len(xs)
    mean = sum(xs) / n if n else None
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    nets = [float(t['net_pct']) for t in trades]
    return {'trades': len(trades), 'net_inr': round(sum(float(t['net_inr']) for t in trades), 2),
            'mean_pct': round(sum(nets) / len(nets), 3) if nets else None,
            'win': (sum(1 for x in nets if x > 0) / len(nets)) if nets else None,
            'targets': sum(1 for t in trades if t['exit_reason'] == 'target'),
            'locked': sum(1 for t in trades if t['locked']),
            'days': n, 'daily_inr': round(mean, 1) if mean is not None else None,
            't': round(mean / (sd / math.sqrt(n)), 2) if sd else None}


def record(conn) -> dict:
    """Both records over every closed paper trade: `band` (B, the paper track: no trade
    near a circuit), `all` (A, every spike, the comparison) and `left_out` (A's trades B
    didn't take), and the verdict on B."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select trade_date from intraday_spike_day order by trade_date")
        days = [r['trade_date'] for r in cur.fetchall()]
        cur.execute("""select * from intraday_spike where paper and status = 'closed'
                       and net_inr is not null order by trade_date, entry_bar""")
        closed = cur.fetchall()
    band = [t for t in closed if t['band_takes']]
    out = {'all': _summary(closed, days), 'band': _summary(band, days),
           'left_out': _summary([t for t in closed if not t['band_takes']], days),
           'since': days[0] if days else None}
    b = out['band']
    if b['trades'] < MIN_TRADES:
        out['verdict'] = f'too early: {b["trades"]} of {MIN_TRADES} trades'
    elif b['daily_inr'] > 0 and (b['t'] or 0) >= 2.0:
        out['verdict'] = 'passes (a candidate for real orders, the owner decides)'
    else:
        out['verdict'] = 'does not pass'
    return out


def today(conn, day: dt.date) -> list[dict]:
    """Today's spikes and paper trades, for the page."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike where trade_date = %s
                         and (alert_bar is not null or trigger_bar is not null)
                       order by coalesce(alert_bar, trigger_bar), symbol""", (day,))
        return cur.fetchall()


def trades(conn, limit: int = 300) -> list[dict]:
    """The paper trades, newest first."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike where paper and entry is not null
                       order by trade_date desc, entry_bar desc limit %s""", (limit,))
        return cur.fetchall()
