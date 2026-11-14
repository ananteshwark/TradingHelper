"""Every recommendation the app makes, with the market at the moment it was made, and its
price and volume path afterwards (docs/TRACKING.md).

`step` runs every minute on weekdays (igs-track.timer):
1. **Register:** each new recommendation in its source's table becomes a `rec_track` row:
   the 09:45 model's calls, the evening picks, the spike picks, the scanner's buy and sell
   calls, the AI's buy and sell calls and the momentum paper tracks' holdings.
2. **Capture:** in the same minute, Upstox's quote of the stock and the Nifty 50: last
   traded price, the day's volume and average traded price (VWAP) so far, the day's high
   and low. Outside market hours that is the last close ('close').
3. **Track:** a tick every 5 minutes for intraday recommendations, every 30 minutes for
   longer ones during their first 30 days and once a day after that, until their horizon
   ends. Each tick updates the path: the worst and best moves since the recommendation,
   signed for its side, when they came, and the relative volume at the worst.

Nothing here places an order or changes a recommendation.
"""
from __future__ import annotations

import datetime as dt
import math

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from igs.timeutil import IST, utc_now

NIFTY = 'NSE_INDEX|Nifty 50'
OPEN, CLOSE = dt.time(9, 15), dt.time(15, 30)
INTRADAY_EVERY = dt.timedelta(minutes=5)
SWING_EVERY = dt.timedelta(minutes=30)
SWING_DETAILED_DAYS = 30
SWING_MAX_DAYS = 365
LOOKBACK = dt.timedelta(days=3)        # how far back register looks for new ones
BATCH = 450                            # keys a quote request (Upstox allows 500)
SOURCES = ('ml_0945', 'evening', 'spike', 'scanner', 'ai_call', 'momentum')
SOURCE_NAMES = {'ml_0945': '09:45 model', 'evening': 'Evening picks', 'spike': 'Spike picks',
                'scanner': 'Scanner calls', 'ai_call': 'AI calls',
                'momentum': 'Momentum paper'}


class QuoteError(RuntimeError):
    pass


# --------------------------------------------------------------------------- quotes


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def parse_quotes(data: dict) -> dict[str, dict]:
    """{instrument key: quote} from Upstox's full market quote (/v2/market-quote/quotes):
    price, the day's volume, VWAP, high and low, and the previous close."""
    out = {}
    for row in (data or {}).values():
        if not isinstance(row, dict) or not row.get('instrument_token'):
            continue
        price = _num(row.get('last_price'))
        if not price or price <= 0:
            continue
        ohlc = row.get('ohlc') or {}
        change = _num(row.get('net_change'))
        out[row['instrument_token']] = {
            'price': price,
            'volume': int(_num(row.get('volume')) or 0) or None,
            'vwap': _num(row.get('average_price')) or None,
            'open': _num(ohlc.get('open')) or None,
            'high': _num(ohlc.get('high')) or None,
            'low': _num(ohlc.get('low')) or None,
            'prev_close': price - change if change is not None else None}
    return out


def upstox_quotes(keys: list[str]) -> dict[str, dict]:
    """Full quotes from Upstox with the market-data token; QuoteError without a token or
    when Upstox can't be reached."""
    import httpx

    from igs.intraday.scanner import token
    from igs.intraday.upstox import Upstox
    key = token()
    if not key:
        raise QuoteError('No Upstox access token (Settings, Upstox tab)')
    feed = Upstox(key)
    out = {}
    try:
        for i in range(0, len(keys), BATCH):
            try:
                r = feed.client.get('/v2/market-quote/quotes',
                                    params={'instrument_key': ','.join(keys[i:i + BATCH])})
            except httpx.HTTPError:
                raise QuoteError('Upstox connection failed') from None
            if r.status_code != 200:
                raise QuoteError(f'Upstox returned HTTP {r.status_code}')
            try:
                body = r.json()
                if body.get('status') != 'success':
                    raise ValueError
                out.update(parse_quotes(body.get('data')))
            except (ValueError, AttributeError):
                raise QuoteError('Upstox returned an invalid quote') from None
    finally:
        feed.close()
    return out


def volume_curve(candles) -> list[float] | None:
    """Mean cumulative volume at the end of each 5-minute slot over the sessions given
    (at least 10 complete ones of 75 candles): the baseline of relative volume."""
    by_day: dict[dt.date, list] = {}
    for c in candles:
        t = c.start.astimezone(IST)
        by_day.setdefault(t.date(), []).append(c)
    full = []
    for day in sorted(by_day)[-20:]:
        bars = sorted(by_day[day], key=lambda b: b.start)
        if len(bars) != 75 or bars[0].start.astimezone(IST).time() != OPEN:
            continue
        run, cum = 0.0, []
        for b in bars:
            run += b.volume
            cum.append(run)
        full.append(cum)
    if len(full) < 10:
        return None
    return [round(sum(d[i] for d in full) / len(full), 1) for i in range(75)]


def upstox_curve(key: str, now: dt.datetime) -> list[float] | None:
    """volume_curve from Upstox's five-minute candles of the 30 days before `now`; None
    when they can't be read."""
    from igs.intraday.scanner import token
    from igs.intraday.upstox import FeedError, Upstox
    secret = token()
    if not secret:
        return None
    feed = Upstox(secret)
    day = now.astimezone(IST).date()
    try:
        candles = feed.candles_between(key, day - dt.timedelta(days=30),
                                       day - dt.timedelta(days=1))
    except FeedError:
        return None
    finally:
        feed.close()
    return volume_curve(candles)


def relative_volume(curve, volume, at: dt.datetime) -> float | None:
    """The day's volume so far over the baseline's at the same time of day."""
    if not curve or not volume:
        return None
    t = at.astimezone(IST)
    minutes = t.hour * 60 + t.minute - (9 * 60 + 15)
    if minutes < 5:
        return None
    slot = min(74, minutes // 5 - 1)
    base = curve[slot]
    return volume / base if base > 0 else None


# --------------------------------------------------------------------------- sources


def _ist(day: dt.date, t: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, t, IST)


def _next_weekday(day: dt.date) -> dt.date:
    day += dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def candidates(conn, now: dt.datetime) -> list[dict]:
    """Recommendations made since LOOKBACK and not yet registered."""
    since = now - LOOKBACK
    since_day = since.astimezone(IST).date()
    out = []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select p.session_date, p.instrument_key, p.company_id, p.symbol, p.side,
                              p.decision_price, p.entry_after, d.decided_at
                       from ml_intraday_pick p join ml_intraday_day d using (session_date)
                       where p.session_date >= %s""", (since_day,))
        for r in cur.fetchall():
            out.append({'source': 'ml_0945', 'ref': f"{r['session_date']}|{r['instrument_key']}",
                        'instrument_key': r['instrument_key'], 'company_id': r['company_id'],
                        'symbol': r['symbol'], 'side': r['side'], 'horizon': 'intraday',
                        'recommended_at': r['decided_at'], 'source_price': r['decision_price'],
                        'track_from': r['entry_after'],
                        'track_until': _ist(r['session_date'], dt.time(15, 15))})
        cur.execute("""select p.signal_date, p.instrument_key, p.company_id, p.symbol, p.side,
                              p.trade_date, p.decision_price, d.decided_at
                       from evening_pick p join evening_day d using (signal_date)
                       where p.signal_date >= %s""", (since_day,))
        for r in cur.fetchall():
            day = r['trade_date'] or _next_weekday(r['signal_date'])
            out.append({'source': 'evening', 'ref': f"{r['signal_date']}|{r['instrument_key']}",
                        'instrument_key': r['instrument_key'], 'company_id': r['company_id'],
                        'symbol': r['symbol'], 'side': r['side'], 'horizon': 'intraday',
                        'recommended_at': r['decided_at'], 'source_price': r['decision_price'],
                        # From 09:20, when its real order enters (INTRADAY_EVENING.md).
                        'track_from': _ist(day, dt.time(9, 20)),
                        'track_until': _ist(day, dt.time(15, 15))})
        cur.execute("""select trade_date, instrument_key, symbol, side, decision_bar, entry,
                              move_to
                       from intraday_spike_ml
                       where trade_date >= %s and side is not null and decision_bar is not null
                         and status in ('picked', 'closed')""", (since_day,))
        for r in cur.fetchall():
            # The spike's side is the move's: a rise (1) is sold short, a fall (-1) bought.
            out.append({'source': 'spike', 'ref': f"{r['trade_date']}|{r['instrument_key']}",
                        'instrument_key': r['instrument_key'], 'company_id': None,
                        'symbol': r['symbol'], 'side': -r['side'], 'horizon': 'intraday',
                        'recommended_at': r['decision_bar'] + dt.timedelta(minutes=5),
                        'source_price': r['entry'] if r['entry'] is not None else r['move_to'],
                        'track_from': r['decision_bar'] + dt.timedelta(minutes=5),
                        'track_until': _ist(r['trade_date'], dt.time(15, 15))})
        cur.execute("""select distinct on (s.company_id, s.result->>'candle_end')
                              s.company_id, s.symbol, s.instrument_key, s.observed_at, s.result
                       from intraday_signal s join intraday_scan r using (scan_id)
                       where r.status = 'complete' and s.result->>'action' in ('buy', 'sell')
                         and s.observed_at >= %s
                       order by s.company_id, s.result->>'candle_end', s.observed_at""",
                    (since,))
        for r in cur.fetchall():
            res = r['result']
            day = r['observed_at'].astimezone(IST).date()
            out.append({'source': 'scanner', 'ref': f"{r['company_id']}|{res['candle_end']}",
                        'instrument_key': r['instrument_key'], 'company_id': r['company_id'],
                        'symbol': r['symbol'], 'side': 1 if res['action'] == 'buy' else -1,
                        'horizon': 'intraday', 'recommended_at': r['observed_at'],
                        'source_price': _num(res.get('reference')),
                        'stop': _num(res.get('stop')), 'track_from': r['observed_at'],
                        'track_until': _ist(day, dt.time(15, 15))})
        cur.execute("""select c.call_id, c.company_id, c.symbol, c.action, c.horizon_months,
                              c.created_at, c.entry_price, si.id_value as isin
                       from ai_call c
                       left join lateral (
                           select si.id_value from security s
                           join security_identifier si using (security_id)
                           where s.company_id = c.company_id and si.id_type = 'ISIN'
                           order by si.valid_to is null desc, si.valid_from desc limit 1) si
                         on true
                       where c.action in ('buy', 'sell') and c.created_at >= %s""", (since,))
        for r in cur.fetchall():
            if not r['isin']:
                continue
            days = min(SWING_MAX_DAYS, round(r['horizon_months'] * 30.4))
            out.append({'source': 'ai_call', 'ref': str(r['call_id']),
                        'instrument_key': f"NSE_EQ|{r['isin']}", 'company_id': r['company_id'],
                        'symbol': r['symbol'], 'side': 1 if r['action'] == 'buy' else -1,
                        'horizon': 'swing', 'recommended_at': r['created_at'],
                        'source_price': r['entry_price'], 'track_from': r['created_at'],
                        'track_until': r['created_at'] + dt.timedelta(days=days)})
        cur.execute("""select m.track, m.signal_date, m.entry_date, m.created_at, h.company_id,
                              h.symbol, si.id_value as isin
                       from momentum_rebalance m
                       cross join lateral jsonb_to_recordset(m.holdings)
                           as h(company_id bigint, symbol text)
                       left join lateral (
                           select si.id_value from security s
                           join security_identifier si using (security_id)
                           where s.company_id = h.company_id and si.id_type = 'ISIN'
                           order by si.valid_to is null desc, si.valid_from desc limit 1) si
                         on true
                       where m.created_at >= %s""", (since,))
        for r in cur.fetchall():
            if not r['isin']:
                continue
            start = _ist(r['entry_date'], OPEN)
            out.append({'source': 'momentum',
                        'ref': f"{r['track']}|{r['signal_date']}|{r['company_id']}",
                        'instrument_key': f"NSE_EQ|{r['isin']}", 'company_id': r['company_id'],
                        'symbol': r['symbol'], 'side': 1, 'horizon': 'swing',
                        'recommended_at': r['created_at'], 'source_price': None,
                        'track_from': start, 'track_until': start + dt.timedelta(days=35)})
        cur.execute('select source, ref from rec_track where recommended_at >= %s',
                    (since - dt.timedelta(days=7),))
        known = {(r['source'], r['ref']) for r in cur.fetchall()}
    return [c for c in out if (c['source'], c['ref']) not in known]


# --------------------------------------------------------------------------- the job


def market_open(now: dt.datetime) -> bool:
    t = now.astimezone(IST)
    return t.weekday() < 5 and OPEN <= t.time() < CLOSE


def register(conn, now: dt.datetime, *, quotes=upstox_quotes, curves=None) -> dict:
    """Register new recommendations and capture the market at this moment."""
    new = candidates(conn, now)
    if not new:
        return {'registered': 0}
    got, note = {}, ''
    try:
        got = quotes(sorted({c['instrument_key'] for c in new} | {NIFTY}))
    except QuoteError as exc:
        note = str(exc)
    live = market_open(now)
    for c in new:
        q = got.get(c['instrument_key'])
        nifty = got.get(NIFTY)
        curve = curves(c['instrument_key'], now) if curves and c['horizon'] == 'intraday' \
            else None
        lag = (now - c['recommended_at']).total_seconds()
        capture = ('live' if live else 'close') if q else 'failed'
        c_note = note or ('' if q else 'No quote for this stock')
        if q and lag > 120:
            c_note = f'Captured {lag / 60:.0f} minutes after the recommendation'
        conn.execute(
            """insert into rec_track (source, ref, instrument_key, company_id, symbol, side,
                   horizon, recommended_at, source_price, stop, track_from, track_until,
                   captured_at, rec_price, rec_volume, rec_vwap, rec_high, rec_low,
                   rec_prev_close, rec_nifty, capture, capture_note, volume_curve,
                   rec_rvol)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s)
               on conflict (source, ref) do nothing""",
            (c['source'], c['ref'], c['instrument_key'], c['company_id'], c['symbol'],
             c['side'], c['horizon'], c['recommended_at'], c.get('source_price'),
             c.get('stop'), c['track_from'], c['track_until'], now if q else None,
             q and q['price'], q and q['volume'], q and q['vwap'], q and q['high'],
             q and q['low'], q and q['prev_close'], nifty and nifty['price'], capture,
             c_note, Jsonb(curve) if curve else None,
             relative_volume(curve, q and q['volume'], now) if q and live else None))
    conn.commit()
    return {'registered': len(new), 'captured': sum(1 for c in new
                                                    if c['instrument_key'] in got)}


def due(row: dict, now: dt.datetime) -> bool:
    """Whether a tracked recommendation takes a tick now."""
    if not (row['track_from'] <= now <= row['track_until'] + dt.timedelta(minutes=1)):
        return False
    last = row['last_at']
    if row['horizon'] == 'intraday':
        return last is None or now - last >= INTRADAY_EVERY - dt.timedelta(seconds=30)
    if now - row['track_from'] <= dt.timedelta(days=SWING_DETAILED_DAYS):
        return last is None or now - last >= SWING_EVERY - dt.timedelta(seconds=30)
    t = now.astimezone(IST)
    return t.time() >= dt.time(15, 20) and (last is None or last.astimezone(IST).date()
                                            < t.date())


def update_path(row: dict, price: float, at: dt.datetime, rvol: float | None) -> dict:
    """The path's summary after a tick, signed for the side, from the price captured at
    the recommendation; when that capture failed, from this first tick's."""
    out = {'last_price': price, 'last_at': at, 'ticks': row['ticks'] + 1}
    if not row['rec_price']:
        out.update(rec_price=price, mae_pct=0.0, mae_at=at, rvol_at_mae=rvol, mfe_pct=0.0,
                   mfe_at=at)
        return out
    move = row['side'] * (price / float(row['rec_price']) - 1) * 100
    if row['mae_pct'] is None or move < row['mae_pct']:
        out.update(mae_pct=move, mae_at=at, rvol_at_mae=rvol)
    if row['mfe_pct'] is None or move > row['mfe_pct']:
        out.update(mfe_pct=move, mfe_at=at)
    return out


def _universe_keys(conn) -> list[str]:
    from igs.intraday.ml import universe
    return [s['instrument_key'] for s in universe(conn)]


def _shadow(conn, r: dict, now: dt.datetime, q: dict, nifty: dict | None,
            universe_quotes: dict) -> dict | None:
    """The shadow record after this tick (tracking_models), or None to leave it."""
    from igs import tracking_models as tm
    if r['source'] not in tm.CALL_SETS:
        return None
    tick = {'at': now.isoformat(), 'price': q['price'], 'volume': q['volume'],
            'vwap': q['vwap'], 'nifty': nifty and nifty['price']}
    try:
        if r['shadow'] is None:
            return tm.score_entry(conn, r, nifty, universe_quotes, tick) or {'error': 'no call'}
        shadow = dict(r['shadow'])
        if 'error' in shadow or shadow.get('stopped_at_tick') or len(shadow['a2']) == len(
                tm.CHECK_MINUTES):
            return None
        entry_at = dt.datetime.fromisoformat(shadow['entry']['at'])
        ticks = [{'at': at, 'price': float(p)} for at, p in conn.execute(
            'select at, price from rec_tick where rec_id = %s and at > %s order by at',
            (r['rec_id'], entry_at)).fetchall()]
        entry = {**shadow['entry'], 'at': entry_at}
        now_tick = {'at': now, 'price': q['price'], 'volume': q['volume'], 'vwap': q['vwap'],
                    'day_high': q['high'], 'day_low': q['low'],
                    'nifty': nifty and nifty['price']}
        shadow['entry'] = entry
        out = tm.score_checks(r, shadow, ticks, now_tick)
        out['entry'] = {**out['entry'], 'at': entry_at.isoformat()}
        return out
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OSError) as exc:
        return {'error': f'{type(exc).__name__}: {exc}'[:300]}


def tick(conn, now: dt.datetime, *, quotes=upstox_quotes, universe=_universe_keys) -> dict:
    """One tick of every recommendation due one now (market hours only)."""
    from igs import tracking_models as tm
    if not market_open(now):
        return {'ticks': 0}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select rec_id, source, ref, instrument_key, side, horizon, track_from,
                              track_until, last_at, rec_price, source_price, mae_pct, mfe_pct,
                              ticks, volume_curve, shadow
                       from rec_track where status = 'tracking' and track_from <= %s""",
                    (now,))
        rows = [r for r in cur.fetchall() if due(r, now)]
    if not rows:
        return {'ticks': 0}
    keys = {r['instrument_key'] for r in rows} | {NIFTY}
    wide = [] if not any(r['source'] in tm.CALL_SETS and r['shadow'] is None for r in rows) \
        else universe(conn)
    got = quotes(sorted(keys | set(wide)))
    nifty = got.get(NIFTY)
    universe_quotes = {k: got[k] for k in wide if k in got}
    n = 0
    for r in rows:
        q = got.get(r['instrument_key'])
        if not q:
            continue
        rvol = relative_volume(r['volume_curve'], q['volume'], now)
        conn.execute("""insert into rec_tick (rec_id, at, price, volume, vwap, day_high, day_low,
                            rvol, nifty) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        on conflict do nothing""",
                     (r['rec_id'], now, q['price'], q['volume'], q['vwap'], q['high'], q['low'],
                      rvol, nifty and nifty['price']))
        upd = update_path(r, q['price'], now, rvol)
        shadow = _shadow(conn, r, now, q, nifty, universe_quotes)
        if shadow is not None:
            upd['shadow'] = Jsonb(shadow)
        sets = ', '.join(f'{k} = %s' for k in upd)
        conn.execute(f'update rec_track set {sets} where rec_id = %s',
                     (*upd.values(), r['rec_id']))
        n += 1
    conn.commit()
    return {'ticks': n}


def finish(conn, now: dt.datetime) -> int:
    n = conn.execute("""update rec_track set status = 'done'
                        where status = 'tracking' and track_until < %s""", (now,)).rowcount
    conn.commit()
    return n


def step(conn, *, clock=utc_now, quotes=upstox_quotes, curves=None,
         universe=_universe_keys) -> str:
    """The minute's work (igs track)."""
    now = clock()
    done = []
    if curves is None:
        curves = upstox_curve
    try:
        res = register(conn, now, quotes=quotes, curves=curves)
        if res['registered']:
            done.append(f"registered {res['registered']} ({res['captured']} captured)")
        res = tick(conn, now, quotes=quotes, universe=universe)
        if res['ticks']:
            done.append(f"{res['ticks']} ticks")
    except QuoteError as exc:
        conn.rollback()
        done.append(f'quotes failed: {exc}')
    ended = finish(conn, now)
    if ended:
        done.append(f'{ended} finished')
    return 'Tracking: ' + ('; '.join(done) or 'nothing due')
