"""Machine-learning intraday paper calls (docs/INTRADAY_ML.md).

Each trading day after the first 30 minutes, a gradient-boosted tree model (trained on Upstox
5-minute candles, February 2022 to October 2026, and fixed since) predicts each liquid
Nifty 200 stock's move to 15:15. The three highest predictions above +0.15% are paper buys
and the three lowest below -0.15% paper short sells. Each enters at the open of the first
5-minute candle after the decision and exits at the open of the 15:15 candle, after
slippage and intraday charges on Rs 1 lakh: the paper record.

When real ML orders are on (trading settings), each call also gets its own Telegram
message, to be approved by a reply until 10:00 IST, or on the Trading page's ML intraday
tab; the order itself is igs.intraday.trading.approve_ml (docs/INTRADAY_ML.md, Real
orders).

The features here must stay identical to the ones the model was trained on
(research/intraday_ml/intraday_features.py).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from igs.config import config_dir
from igs.intraday.costs import charges, intraday_rates
from igs.intraday.engine import Candle
from igs.intraday.upstox import FeedError
from igs.timeutil import IST, utc_now

T = dt.time
INDEX_KEY = 'NSE_INDEX|Nifty 50'
INDEX_NAME = 'NIFTY 200'
FIRST_BARS = [T(9, 15 + 5 * i) for i in range(6)]          # 09:15 to 09:40
DECIDE_FROM, DECIDE_UNTIL = T(9, 45), T(9, 55)
EXIT_BAR = T(15, 15)
SETTLE_FROM = T(15, 20)
PREPARE_FROM = T(8, 30)
HISTORY = 22                 # prior sessions the features need
FETCH_DAYS = 45              # calendar days fetched when a stock's history is short
K, GATE = 3, 0.0015
MIN_VALUE = 50e7             # Rs 50 crore a day, 20-session average
NOTIONAL, SLIP = 100000, 0.0002
LOCK = 739182511
PAUSE = 0.15                 # between Upstox requests
MAX_FAILURES = 10            # stocks whose candles fail before a run gives up


# --------------------------------------------------------------------------- features


def _time(c: Candle) -> dt.time:
    return c.start.astimezone(IST).time()


def sessions(candles: list[Candle]) -> dict[dt.date, list[Candle]]:
    """Candles by IST session; only full sessions (first candle 09:15, at least 70)."""
    days: dict[dt.date, list[Candle]] = {}
    for c in candles:
        days.setdefault(c.start.astimezone(IST).date(), []).append(c)
    out = {}
    for d, bars in days.items():
        bars.sort(key=lambda b: b.start)
        if _time(bars[0]) == T(9, 15) and len(bars) >= 70:
            out[d] = bars
    return out


def summary(bars: list[Candle]) -> dict:
    """A full session: open, high, low, close, traded value, last-hour return, and the
    volume of its first 30 minutes."""
    o, c = bars[0].open, bars[-1].close
    k = next((i for i, b in enumerate(bars) if _time(b) >= T(14, 30)), None)
    return {'open': o, 'high': max(b.high for b in bars), 'low': min(b.low for b in bars),
            'close': c, 'value': sum(b.close * b.volume for b in bars),
            'last_hour': c / bars[k].open - 1 if k is not None else None,
            'v30': sum(b.volume for b in bars[:6])}


def first30(bars: list[Candle]) -> dict | None:
    """Today's first six candles (09:15 to 09:40), or None until all six are in."""
    six = [b for b in bars if _time(b) in FIRST_BARS]
    if [_time(b) for b in six] != FIRST_BARS:
        return None
    hi, lo, c = max(b.high for b in six), min(b.low for b in six), six[-1].close
    vol = sum(b.volume for b in six)
    vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in six) / vol if vol else c
    return {'open': six[0].open, 'high': hi, 'low': lo, 'close': c, 'volume': vol, 'vwap': vwap}


def index_features(prev: list[dict], today: list[Candle]) -> dict | None:
    """The Nifty 50's part: gap, first-30-minute return, previous-day and 5-day returns."""
    f = first30(today)
    if f is None or len(prev) < HISTORY:
        return None
    p = prev[-HISTORY:]
    return {'n_gap': f['open'] / p[-1]['close'] - 1, 'n_r30': f['close'] / f['open'] - 1,
            'n_prev_r1': p[-1]['close'] / p[-2]['close'] - 1,
            'n_r5': p[-1]['close'] / p[-6]['close'] - 1}


def stock_features(prev: list[dict], today: list[Candle], index: dict, day: dt.date) -> dict | None:
    """The model's inputs for one stock at 09:45, from its previous 22 full sessions (oldest
    first) and today's candles; None when anything is missing."""
    f = first30(today)
    if f is None or len(prev) < HISTORY or index is None:
        return None
    prev = prev[-HISTORY:]
    p, pp = prev[-1], prev[-2]
    trs = [max(x['high'] - x['low'], abs(x['high'] - y['close']), abs(x['low'] - y['close']))
           for y, x in zip(prev[-15:-1], prev[-14:], strict=True)]
    atr = sum(trs) / len(trs)
    mean_v30 = sum(x['v30'] for x in prev[-20:]) / 20
    value20 = sum(x['value'] for x in prev[-20:]) / 20
    rng = f['high'] - f['low']
    if not atr or not mean_v30 or value20 <= 0 or p['last_hour'] is None:
        return None
    r30 = f['close'] / f['open'] - 1
    row = {
        'gap': f['open'] / p['close'] - 1,
        'r30': r30,
        'prev_r1': p['close'] / pp['close'] - 1,
        'r5': p['close'] / prev[-6]['close'] - 1,
        'range30_atr': rng / atr,
        'rvol30': f['volume'] / mean_v30,
        'vwap_dist': f['close'] / f['vwap'] - 1,
        'clv30': (f['close'] - f['low']) / rng if rng else 0.5,
        'prev_clv': ((p['close'] - p['low']) / (p['high'] - p['low'])
                     if p['high'] > p['low'] else 0.5),
        'prev_last_hour': p['last_hour'],
        'prev_intraday': p['close'] / p['open'] - 1,
        'r21': p['close'] / prev[0]['close'] - 1,
        'atr_pct': atr / p['close'],
        'dist20h': p['close'] / max(x['high'] for x in prev[-20:]) - 1,
        'dist20l': p['close'] / min(x['low'] for x in prev[-20:]) - 1,
        'log_value20': math.log(value20),
        'ovn5': sum(math.log(x['open'] / y['close'])
                    for y, x in zip(prev[-6:-1], prev[-5:], strict=True)),
        'intra5': sum(math.log(x['close'] / x['open']) for x in prev[-5:]),
        **index,
        'rel30': r30 - index['n_r30'],
        **{f'dow_{i}': 1.0 if day.weekday() == i else 0.0 for i in range(5)},
        'value20': value20,
    }
    return row


def history_matches(stock: list[dict], index: list[dict], today: dt.date) -> bool:
    """Features need the same 22 completed sessions, not merely 22 stored rows.

    A failed refresh or a missing session must not turn a multi-day move into the
    previous-day feature. The index is the observed reference, not an official calendar.
    """
    if len(stock) < HISTORY or len(index) < HISTORY:
        return False
    dates = [r['session_date'] for r in stock[-HISTORY:]]
    reference = [r['session_date'] for r in index[-HISTORY:]]
    return (dates == reference and len(set(reference)) == HISTORY
            and reference == sorted(reference) and reference[-1] < today)


# --------------------------------------------------------------------------- the model


class Model:
    """Gradient-boosted regression trees, exported from LightGBM to JSON: a split is
    [feature, threshold, left, right] (left when the value is at most the threshold), a
    leaf is its value; the prediction is the sum over trees."""

    def __init__(self, data: dict):
        self.name, self.features, self.trees = data['name'], data['features'], data['trees']
        self.meta = {k: v for k, v in data.items() if k not in ('trees',)}

    def predict(self, row: dict) -> float:
        x = [float(row[f]) for f in self.features]
        total = 0.0
        for node in self.trees:
            while isinstance(node, list):
                node = node[2] if x[node[0]] <= node[1] else node[3]
            total += node
        return total


@lru_cache(maxsize=1)
def model(path: Path | None = None) -> Model:
    return Model(json.loads((path or config_dir() / 'models' / 'intraday_ml_v1.json').read_text()))


def choose(predictions: list[tuple[str, float]], k: int = K,
           gate: float = GATE) -> list[tuple[str, int, float]]:
    """(key, side, prediction): the k highest above the gate as buys (+1), the k lowest below
    minus the gate as short sells (-1)."""
    ranked = sorted(predictions, key=lambda kv: kv[1])
    longs = [kv for kv in ranked if kv[1] > gate][-k:]
    shorts = [kv for kv in ranked if kv[1] < -gate][:k]
    return ([(key, 1, p) for key, p in reversed(longs)]
            + [(key, -1, p) for key, p in shorts])


def result(side: int, entry: float, exit_price: float, rates) -> dict | None:
    """Paper result on Rs 1 lakh after slippage and intraday charges."""
    e = entry * (1 + side * SLIP)
    x = exit_price * (1 - side * SLIP)
    qty = int(NOTIONAL // e)
    if qty < 1:
        return None
    cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                         'buy' if side == 1 else 'sell', rates))
    gross = side * (x - e) * qty
    return {'quantity': qty, 'gross_inr': round(gross, 2), 'charges_inr': round(cost, 2),
            'net_inr': round(gross - cost, 2),
            'net_pct': round((gross - cost) / (e * qty) * 100, 4)}


# --------------------------------------------------------------------------- database


def universe(conn) -> list[dict]:
    """The latest Nifty 200 snapshot, with NSE symbols and Upstox instrument keys."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            select distinct on (m.company_id) m.company_id, n.id_value symbol,
                   'NSE_EQ|' || i.id_value instrument_key
            from index_member m
            join security s on s.company_id = m.company_id and s.security_type = 'equity'
            join security_identifier n on n.security_id = s.security_id
                 and n.id_type = 'NSE_SYMBOL' and n.valid_from <= current_date
                 and (n.valid_to is null or n.valid_to > current_date)
            join security_identifier i on i.security_id = s.security_id
                 and i.id_type = 'ISIN' and i.valid_from <= current_date
                 and (i.valid_to is null or i.valid_to > current_date)
            where m.index_name = %s
              and m.as_of = (select max(as_of) from index_member where index_name = %s)
            order by m.company_id, s.security_id""", (INDEX_NAME, INDEX_NAME))
        return cur.fetchall()


def _stored(conn, key: str, before: dt.date, limit: int = HISTORY) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select session_date, open::float8, high::float8, low::float8,
                              close::float8, value::float8, last_hour::float8, v30::float8
                       from ml_intraday_session where instrument_key = %s and session_date < %s
                       order by session_date desc limit %s""", (key, before, limit))
        return list(reversed(cur.fetchall()))


def _store(conn, key: str, days: dict[dt.date, list[Candle]]) -> int:
    n = 0
    for d, bars in days.items():
        s = summary(bars)
        conn.execute("""insert into ml_intraday_session (instrument_key, session_date, open, high,
                            low, close, value, last_hour, v30)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        on conflict (instrument_key, session_date) do nothing""",
                     (key, d, s['open'], s['high'], s['low'], s['close'], s['value'],
                      s['last_hour'], s['v30']))
        n += 1
    return n


def prepare(conn, feed, today: dt.date, keys: list[str], *, pause=time.sleep) -> int:
    """Store the full sessions each key lacks up to the last one before `today` (the Nifty
    50's last session tells which day that was). Returns the requests made."""
    end = today - dt.timedelta(days=1)
    requests = failures = 0
    last_session = None
    for key in [INDEX_KEY] + keys:
        stored = _stored(conn, key, today)
        if last_session is not None and stored and stored[-1]['session_date'] >= last_session \
                and len(stored) >= HISTORY:
            continue
        start = (stored[-1]['session_date'] + dt.timedelta(days=1)
                 if stored and len(stored) >= HISTORY else today - dt.timedelta(days=FETCH_DAYS))
        candles = []
        chunk = start
        try:
            while chunk <= end:
                stop = min(chunk + dt.timedelta(days=27), end)
                requests += 1
                candles += feed.candles_between(key, chunk, stop)
                pause(PAUSE)
                chunk = stop + dt.timedelta(days=1)
        except FeedError:
            failures += 1
            if key == INDEX_KEY or failures > MAX_FAILURES:
                raise
            continue
        days = sessions(candles)
        _store(conn, key, days)
        conn.commit()
        if key == INDEX_KEY:
            stored = _stored(conn, key, today)
            last_session = stored[-1]['session_date'] if stored else None
    conn.execute('delete from ml_intraday_session where session_date < %s',
                 (today - dt.timedelta(days=FETCH_DAYS + 30),))
    conn.commit()
    return requests


def decide(conn, feed, now: dt.datetime, *, clock=utc_now, pause=time.sleep,
           mdl: Model | None = None) -> dict:
    """Today's paper calls, once, between 09:45 and 09:55 IST."""
    local = now.astimezone(IST)
    today = local.date()
    if conn.execute('select 1 from ml_intraday_day where session_date = %s', (today,)).fetchone():
        return {'status': 'done'}
    mdl = mdl or model()
    stocks = universe(conn)
    if not stocks:
        conn.execute("""insert into ml_intraday_day (session_date, decided_at, model, universe,
                            scored, note) values (%s, %s, %s, 0, 0, %s)""",
                     (today, now, mdl.name, 'No Nifty 200 list loaded yet'))
        conn.commit()
        return {'status': 'no universe'}
    prepare(conn, feed, today, [s['instrument_key'] for s in stocks], pause=pause)
    index_today = [c for c in feed.candles(INDEX_KEY) if c.start.astimezone(IST).date() == today]
    index_history = _stored(conn, INDEX_KEY, today)
    index = index_features(index_history, index_today)
    if index is None:
        return {'status': 'waiting', 'message': "The Nifty 50's first 30 minutes are not in yet"}
    scored, failures, history_skips = [], 0, 0
    for s in stocks:
        key = s['instrument_key']
        history = _stored(conn, key, today)
        if not history_matches(history, index_history, today):
            history_skips += 1
            continue
        pause(PAUSE)
        try:
            bars = [c for c in feed.candles(key) if c.start.astimezone(IST).date() == today]
        except FeedError:                 # one stock's bad answer skips that stock
            failures += 1
            if failures > MAX_FAILURES:
                raise
            continue
        row = stock_features(history, bars, index, today)
        if row is None or row['value20'] < MIN_VALUE:
            continue
        scored.append((s, row, mdl.predict(row), first30(bars)['close']))
    decided = clock()
    local_decided = decided.astimezone(IST)
    expired = (local_decided.date() != today
               or not DECIDE_FROM <= local_decided.time() < DECIDE_UNTIL)
    picks = [] if expired else choose([(s['instrument_key'], p) for s, _, p, _ in scored])
    notes = []
    if expired:
        notes.append('Decision window expired during data collection; no calls issued')
    elif not picks:
        notes.append('No prediction cleared the 0.15% gate')
    if history_skips:
        notes.append(f'{history_skips} stocks withheld: prior sessions do not match the index')
    by_key = {s['instrument_key']: (s, row, price) for s, row, _, price in scored}
    conn.execute("""insert into ml_intraday_day (session_date, decided_at, model, universe,
                        scored, note) values (%s, %s, %s, %s, %s, %s)""",
                 (today, decided, mdl.name, len(stocks), len(scored),
                  '; '.join(notes)))
    for rank, (key, side, pred) in enumerate(picks, 1):
        s, row, price = by_key[key]
        conn.execute("""insert into ml_intraday_pick (session_date, instrument_key, company_id,
                            symbol, side, prediction, rank, features, entry_after, status,
                            decision_price)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)""",
                     (today, key, s['company_id'], s['symbol'], side, pred, rank,
                      Jsonb({k: round(v, 6) for k, v in row.items()}), decided, price))
    conn.commit()
    return {'status': 'expired' if expired else 'decided',
            'scored': len(scored), 'picks': len(picks)}


def _entry_exit(bars: list[Candle], after: dt.datetime) -> tuple[float | None, float | None]:
    entry = next((b.open for b in sorted(bars, key=lambda b: b.start) if b.start >= after), None)
    exit_price = next((b.open for b in bars if _time(b) == EXIT_BAR), None)
    return entry, exit_price


def settle(conn, feed, now: dt.datetime, *, pause=time.sleep) -> int:
    """Close the open paper calls of earlier days, and today's from 15:20 IST."""
    local = now.astimezone(IST)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select session_date, instrument_key, side, entry_after from ml_intraday_pick
                       where status = 'open' and (session_date < %s or %s)""",
                    (local.date(), local.time() >= SETTLE_FROM))
        rows = cur.fetchall()
    rates = intraday_rates()
    n = 0
    for r in rows:
        d = r['session_date']
        bars = feed.candles(r['instrument_key']) if d == local.date() else \
            feed.candles_between(r['instrument_key'], d, d)
        pause(PAUSE)
        bars = [b for b in bars if b.start.astimezone(IST).date() == d]
        after = r['entry_after']
        after = after + dt.timedelta(seconds=(300 - after.timestamp() % 300) % 300)
        entry, exit_price = _entry_exit(bars, after)
        if not (entry and exit_price):
            if d < local.date() - dt.timedelta(days=3):
                conn.execute("""update ml_intraday_pick set status = 'no data'
                                where session_date = %s and instrument_key = %s""",
                             (d, r['instrument_key']))
            continue
        res = result(r['side'], entry, exit_price, rates)
        if res is None:          # priced above the Rs 1 lakh a call, as in the backtest
            conn.execute("""update ml_intraday_pick set status = 'skipped', entry = %s, exit = %s
                            where session_date = %s and instrument_key = %s""",
                         (entry, exit_price, d, r['instrument_key']))
            continue
        conn.execute("""update ml_intraday_pick set status = 'closed', entry = %s, exit = %s,
                            quantity = %s, gross_inr = %s, charges_inr = %s, net_inr = %s,
                            net_pct = %s where session_date = %s and instrument_key = %s""",
                     (entry, exit_price, res['quantity'], res['gross_inr'], res['charges_inr'],
                      res['net_inr'], res['net_pct'], d, r['instrument_key']))
        n += 1
    conn.commit()
    return n


# --------------------------------------------------------------------------- reporting


def picks(conn, day: dt.date | None = None, limit: int = 200) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select session_date, symbol, side, prediction::float8 prediction, rank,
                              entry::float8 entry, exit::float8 exit, quantity,
                              net_inr::float8 net_inr, net_pct::float8 net_pct, status
                       from ml_intraday_pick where %s::date is null or session_date = %s
                       order by session_date desc, rank limit %s""", (day, day, limit))
        return cur.fetchall()


def pick(conn, day: dt.date, symbol: str) -> dict:
    """One call of the day, as trading.ml_plan needs it."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select session_date, instrument_key, company_id, symbol, side,
                              prediction::float8 prediction, features, decision_price, status
                       from ml_intraday_pick where session_date = %s and symbol = %s""",
                    (day, symbol))
        return cur.fetchone()


def record(conn) -> dict:
    """Totals of the closed paper calls and each day's net result."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select count(*) trades, coalesce(sum(net_inr), 0)::float8 net_inr,
                              avg(net_pct)::float8 net_pct, stddev(net_pct)::float8 sd_pct,
                              avg((net_inr > 0)::int)::float8 win,
                              coalesce(sum(net_inr) filter (where net_inr > 0), 0)::float8 gains,
                              coalesce(-sum(net_inr) filter (where net_inr < 0), 0)::float8 losses,
                              count(distinct session_date) days
                       from ml_intraday_pick where status = 'closed'""")
        total = cur.fetchone()
        cur.execute("""select d.session_date, d.scored, d.note,
                              count(p.*) filter (where p.status = 'closed') trades,
                              count(p.*) filter (where p.status in ('open', 'no data')) unresolved,
                              coalesce(sum(p.net_inr), 0)::float8 net_inr
                       from ml_intraday_day d left join ml_intraday_pick p using (session_date)
                       group by d.session_date, d.scored, d.note order by d.session_date""")
        days = cur.fetchall()
    total['profit_factor'] = total['gains'] / total['losses'] if total['losses'] else None
    total.update(daily_evidence(days))
    return {'total': total, 'days': days}


def daily_evidence(days: list[dict]) -> dict:
    """Descriptive t of net rupees per complete decision day, including no-call days.

    Same-day calls share market moves. Do not multiply that day's evidence by its
    number of trades, or count unresolved positions as zero-return cash days. Daily
    aggregation does not correct serial dependence, model selection or paper/live gaps.
    """
    complete = [d['net_inr'] for d in days if not d['unresolved']]
    incomplete = sum(bool(d['unresolved']) for d in days)
    n = len(complete)
    mean = sum(complete) / n if n else None
    sd = math.sqrt(sum((x - mean) ** 2 for x in complete) / (n - 1)) if n > 1 else 0.0
    t = mean / sd * math.sqrt(n) if n > 1 and sd > 0 and not incomplete else None
    return {'evidence_days': n, 'incomplete_days': incomplete,
            'daily_net_inr': mean, 't': t}


# About how many calls an edge like the backtest's (+0.13% a call, 1.0% spread a call) needs
# before it shows at t = 2, apart from luck (research/winners).
CALLS_TO_TELL = 250


def message_calls(conn, day: dt.date) -> str | None:
    rows = picks(conn, day)
    if not rows:
        return None
    lines = [f"ML intraday paper calls, {day:%d %b %Y} (paper only; no order is placed):"]
    for r in rows:
        side = 'BUY' if r['side'] == 1 else 'SELL short'
        lines.append(f"{side} {r['symbol']}: predicted {r['prediction'] * 100:+.2f}% to 15:15")
    lines.append("Entry at the next 5-minute candle's open, exit at the 15:15 candle's open. "
                 "Tracked on the page ML intraday.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- real orders


def orders_on(conn) -> bool:
    """Whether the ML calls can be ordered: live trading and real ML orders both on."""
    row = conn.execute('select enabled and ml_orders_enabled from intraday_trading_settings '
                       'where singleton=true').fetchone()
    return bool(row and row[0])


def order_message(pick: dict, preview: dict, until: dt.time, deviation) -> str:
    """One ML call, to be approved by a reply (trading.ml_preview gives `preview`)."""
    from igs.intraday.trading import ML_MIN_USEFUL_RUPEES, ML_SQUARE_OFF
    side = 'BUY' if pick['side'] == 1 else 'SELL SHORT'
    price = float(pick['decision_price'])
    lines = [f"ML INTRADAY {side} · {pick['symbol']}",
             f"{pick['session_date']:%d %b %Y} · price the model saw ₹{price:.2f} (09:45 IST) · "
             f"predicted {pick['prediction'] * 100:+.2f}% to the close"]
    if preview['ok']:
        lines.append(f"Order at your settings: about {preview['quantity']} shares "
                     f"(₹{preview['value']:,.0f}) at the live price, stop about "
                     f"₹{preview['stop']:.2f} (half the 14-day ATR), squared off by the app at "
                     f"{ML_SQUARE_OFF:%H:%M} IST. Loss at the stop about ₹{preview['loss']:,.0f}, "
                     f"charges about ₹{preview['charges']:,.0f}.")
        if preview['value'] < ML_MIN_USEFUL_RUPEES:
            lines.append(f"At ₹{preview['value']:,.0f} a trade, charges take most of the "
                         "tested edge: it was tested at ₹1 lakh (Settings, Intraday trading).")
    else:
        lines.append(f"At your settings no order can be placed: {preview['reason']}.")
    lines += ["Tested with this stop and exit, after charges: +0.05% to +0.08% a trade "
              "entered at 09:50, +0.01% to +0.06% at 10:00, mostly on short sells. Not yet "
              "proven forward.",
              f"No order has been placed. Reply APPROVED to this message before {until:%H:%M} "
              "IST for a real Upstox intraday order, or approve on the Trading page (ML "
              "intraday tab). It "
              f"is refused if the price has moved more than {deviation}% from ₹{price:.2f}."
              "\nhttps://stocks.ednis.ai/"]
    return "\n".join(lines)


def send_order_calls(conn, now: dt.datetime, sender, chart=None) -> int:
    """Each of today's calls in its own Telegram message, to be approved by a reply, while
    approvals are open and real ML orders are on. A failed send is retried on the next run,
    three times at most. With `chart` (igs.alerts.delivery.send_chart), the stock's price
    chart follows as a reply to the call. Returns the messages sent."""
    from igs.alerts.operations import record_issue
    from igs.intraday.trading import ML_APPROVE_UNTIL, ml_preview, ml_window, settings
    if not ml_window(now) or not orders_on(conn):
        return 0
    cfg, rates = settings(conn), intraday_rates()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select session_date, instrument_key, symbol, side,
                              prediction::float8 prediction, features, decision_price
                       from ml_intraday_pick where session_date = %s and status = 'open'
                         and telegram_message_id is null and telegram_attempts < 3
                         and decision_price is not null order by rank""",
                    (now.astimezone(IST).date(),))
        rows = cur.fetchall()
    conn.commit()
    sent = 0
    for pick in rows:
        text = order_message(pick, ml_preview(pick, cfg, rates), ML_APPROVE_UNTIL,
                             cfg['max_price_deviation_pct'])
        try:
            message_id = sender(text)
        except Exception as exc:  # noqa: BLE001 - retried on the next run
            record_issue('intraday-ml', type(exc).__name__)
            message_id = None
        conn.execute("""update ml_intraday_pick set telegram_attempts = telegram_attempts + 1,
                            telegram_message_id = %s
                        where session_date = %s and instrument_key = %s""",
                     (message_id if type(message_id) is int else None, pick['session_date'],
                      pick['instrument_key']))
        conn.commit()
        sent += type(message_id) is int
        if chart and type(message_id) is int:
            from igs.alerts.price_chart import company_of
            chart(conn, company_of(conn, pick['symbol']), reply_to=message_id)
    return sent


def message_results(conn, day: dt.date) -> str | None:
    rows = [r for r in picks(conn, day) if r['status'] == 'closed']
    if not rows:
        return None
    lines = [f"ML intraday paper results, {day:%d %b %Y}, after charges on ₹1 lakh each:"]
    for r in rows:
        side = 'BUY' if r['side'] == 1 else 'SELL'
        lines.append(f"{side} {r['symbol']}: ₹{r['net_inr']:+,.0f} ({r['net_pct']:+.2f}%)")
    lines.append(f"Day: ₹{sum(r['net_inr'] for r in rows):+,.0f}. "
                 f"Since the start: ₹{record(conn)['total']['net_inr']:+,.0f}.")
    return "\n".join(lines)


def step(conn, feed=None, *, clock=utc_now, pause=time.sleep, notify=None,
         sender=None, chart=None) -> str:
    """The intraday job's ML part: prepare history before the open, decide at 09:45, and
    settle from 15:20. With real ML orders on, `sender` sends each call its own message to
    approve by a reply (send_order_calls) instead of the one paper note. Returns what it
    did."""
    if not conn.execute('select pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0]:
        return 'ML intraday: another run is in progress'
    conn.commit()
    own = feed is None
    try:
        now = clock()
        local = now.astimezone(IST)
        if local.weekday() >= 5:
            return 'ML intraday: weekend'
        if own:
            from igs.intraday.scanner import token
            from igs.intraday.upstox import Upstox
            if not token():
                return 'ML intraday: no Upstox access token'
            feed = Upstox(token())
        done = []
        settled = settle(conn, feed, now, pause=pause)
        if settled:
            done.append(f'settled {settled}')
            if notify and local.time() >= SETTLE_FROM:
                text = message_results(conn, local.date())
                if text:
                    notify(text)
        if PREPARE_FROM <= local.time() < DECIDE_FROM:
            stocks = universe(conn)
            if stocks:
                n = prepare(conn, feed, local.date(), [s['instrument_key'] for s in stocks],
                            pause=pause)
                done.append(f'prepared ({n} requests)')
        elif DECIDE_FROM <= local.time() < DECIDE_UNTIL:
            res = decide(conn, feed, now, clock=clock, pause=pause)
            done.append(str(res))
            if res['status'] == 'decided' and notify and not (sender and orders_on(conn)):
                text = message_calls(conn, local.date())
                if text:
                    notify(text)
        if sender:
            sent = send_order_calls(conn, clock(), sender, chart)
            if sent:
                done.append(f'sent {sent} calls to approve')
        return 'ML intraday: ' + ('; '.join(done) or 'nothing due')
    finally:
        if own and feed is not None:
            feed.close()
        conn.rollback()
        conn.execute('select pg_advisory_unlock(%s)', (LOCK,))
        conn.commit()
