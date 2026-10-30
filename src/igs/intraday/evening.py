"""Evening intraday paper calls (docs/INTRADAY_EVENING.md, research/evening).

After each session's close, a gradient-boosted tree model (five LightGBM seeds averaged,
trained on Upstox 5-minute candles, February 2022 to October 2026, and fixed since)
predicts each liquid Nifty 200 stock's move from the next session's 09:15 open to its
15:15 open, from what is known at the close. The three highest predictions above +0.15%
are paper buys and the three lowest below -0.15% paper short sells, recorded the evening
before. Each is closed from the next session's candles after 0.05% slippage a side and
intraday charges on Rs 1 lakh: the paper record. Nothing here places an order.

When real evening orders are on (trading settings), each call also gets its own Telegram
message, to be approved by a reply until 09:15 IST on the next trading day, or on the
Trading page's Evening picks tab. igs.intraday.trading records the approval and places the
order after the open (approve_evening, place_evening; docs/INTRADAY_EVENING.md).

The features must stay identical to research/evening/features.py; a row with a missing
input is not scored (research/evening/check_features.py compares the two).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time
from functools import lru_cache
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from igs.config import config_dir
from igs.intraday import ml
from igs.intraday.costs import intraday_rates
from igs.intraday.engine import Candle
from igs.intraday.upstox import FeedError
from igs.timeutil import IST, utc_now

T = dt.time
INDEX_KEY = ml.INDEX_KEY
LAST_BAR = T(15, 25)
ENTRY_BAR, EXIT_BAR = T(9, 15), T(15, 15)
DECIDE_FROM, DECIDE_UNTIL = T(15, 40), T(23, 30)
SETTLE_FROM = T(15, 20)
HISTORY = 22                  # sessions to the signal day, the signal day included
FETCH_DAYS = 45               # calendar days of history fetched for each stock
K, GATE = 3, 0.0015
MIN_VALUE = ml.MIN_VALUE
SLIP = 0.0005                 # a side: the opening minutes are the least liquid
LOCK = 739182512
PAUSE = ml.PAUSE
MAX_FAILURES = ml.MAX_FAILURES


# --------------------------------------------------------------------------- features


def _time(c: Candle) -> dt.time:
    return c.start.astimezone(IST).time()


def summary(bars: list[Candle]) -> dict:
    """A full session, as research/evening/features.py summarises it."""
    o, c = bars[0].open, bars[-1].close
    k60 = next((i for i, b in enumerate(bars) if _time(b) >= T(14, 30)), None)
    k30 = next((i for i, b in enumerate(bars) if _time(b) >= T(15, 0)), None)
    vol = sum(b.volume for b in bars)
    vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in bars) / vol if vol else c
    return {'open': o, 'high': max(b.high for b in bars), 'low': min(b.low for b in bars),
            'close': c, 'value': sum(b.close * b.volume for b in bars),
            'last_hour': c / bars[k60].open - 1 if k60 is not None else None,
            'last30': c / bars[k30].open - 1 if k30 is not None else None,
            'v_last30': sum(b.volume for b in bars[k30:]) if k30 is not None else None,
            'vwap': vwap}


def index_features(prev: list[dict]) -> dict | None:
    """The Nifty 50's part, from its 22 sessions to the signal day (oldest first)."""
    if len(prev) < HISTORY:
        return None
    p = prev[-HISTORY:]
    row = {'n_prev_r1': p[-1]['close'] / p[-2]['close'] - 1, 'n_last_hour': p[-1]['last_hour'],
           'n_r5': p[-1]['close'] / p[-6]['close'] - 1, 'n_r21': p[-1]['close'] / p[0]['close'] - 1,
           'n_last30': p[-1]['last30']}
    return None if any(v is None for v in row.values()) else row


def stock_features(prev: list[dict], index: dict | None, trade_day: dt.date) -> dict | None:
    """The model's inputs for one stock, from its 22 sessions to the signal day (oldest
    first); None when anything is missing."""
    if len(prev) < HISTORY or index is None:
        return None
    prev = prev[-HISTORY:]
    p, pp = prev[-1], prev[-2]
    trs = [max(x['high'] - x['low'], abs(x['high'] - y['close']), abs(x['low'] - y['close']))
           for y, x in zip(prev[-15:-1], prev[-14:], strict=True)]
    atr = sum(trs) / len(trs)
    value20 = sum(x['value'] for x in prev[-20:]) / 20
    v30 = [x['v_last30'] for x in prev[-21:-1] if x['v_last30']]
    mean_v30 = sum(v30) / len(v30) if v30 else None
    if (not atr or value20 <= 0 or p['last_hour'] is None or p['last30'] is None
            or not mean_v30 or not p['v_last30']):
        return None
    prev_r1 = p['close'] / pp['close'] - 1
    return {
        'prev_r1': prev_r1,
        'prev_clv': ((p['close'] - p['low']) / (p['high'] - p['low'])
                     if p['high'] > p['low'] else 0.5),
        'prev_last_hour': p['last_hour'],
        'prev_intraday': p['close'] / p['open'] - 1,
        'r5': p['close'] / prev[-6]['close'] - 1,
        'r21': p['close'] / prev[0]['close'] - 1,
        'atr_pct': atr / p['close'],
        'dist20h': p['close'] / max(x['high'] for x in prev[-20:]) - 1,
        'dist20l': p['close'] / min(x['low'] for x in prev[-20:]) - 1,
        'log_value20': math.log(value20),
        'ovn5': sum(math.log(x['open'] / y['close'])
                    for y, x in zip(prev[-6:-1], prev[-5:], strict=True)),
        'intra5': sum(math.log(x['close'] / x['open']) for x in prev[-5:]),
        'last30': p['last30'],
        'rvol_last30': p['v_last30'] / mean_v30,
        'close_vwap': p['close'] / p['vwap'] - 1,
        **index,
        'rel1': prev_r1 - index['n_prev_r1'],
        **{f'dow_{i}': 1.0 if trade_day.weekday() == i else 0.0 for i in range(5)},
        'value20': value20,
    }


def next_weekday(day: dt.date) -> dt.date:
    """The day of the week the calls are scored for. An exchange holiday makes the real
    session later; the weekday feature then differs from training, as the model can't know."""
    day += dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


# --------------------------------------------------------------------------- the model


class Ensemble:
    """Several tree models exported from LightGBM (igs.intraday.ml.Model's format); the
    prediction is their mean."""

    def __init__(self, data: dict):
        self.name, self.features = data['name'], data['features']
        self.members = [ml.Model({'name': self.name, 'features': self.features, 'trees': trees})
                        for trees in data['ensemble']]
        self.meta = {k: v for k, v in data.items() if k != 'ensemble'}

    def predict(self, row: dict) -> float:
        return sum(m.predict(row) for m in self.members) / len(self.members)


@lru_cache(maxsize=1)
def model(path: Path | None = None) -> Ensemble:
    return Ensemble(json.loads(
        (path or config_dir() / 'models' / 'intraday_evening_v1.json').read_text()))


# --------------------------------------------------------------------------- data


def history(feed, key: str, today: dt.date, *, pause=time.sleep) -> dict[dt.date, list[Candle]]:
    """Full sessions of the last FETCH_DAYS calendar days, today's included."""
    candles, chunk, end = [], today - dt.timedelta(days=FETCH_DAYS), today - dt.timedelta(days=1)
    while chunk <= end:
        stop = min(chunk + dt.timedelta(days=27), end)
        candles += feed.candles_between(key, chunk, stop)
        pause(PAUSE)
        chunk = stop + dt.timedelta(days=1)
    candles += feed.candles(key)
    pause(PAUSE)
    return ml.sessions(candles)


def complete(days: dict[dt.date, list[Candle]], today: dt.date) -> bool:
    bars = days.get(today)
    return bool(bars) and _time(bars[-1]) == LAST_BAR


# --------------------------------------------------------------------------- decide, settle


def decide(conn, feed, now: dt.datetime, *, clock=utc_now, pause=time.sleep,
           mdl: Ensemble | None = None) -> dict:
    """Tonight's paper calls for the next session, once, after today's close."""
    today = now.astimezone(IST).date()
    if conn.execute('select 1 from evening_day where signal_date = %s', (today,)).fetchone():
        return {'status': 'done'}
    mdl = mdl or model()
    stocks = ml.universe(conn)
    if not stocks:
        conn.execute("""insert into evening_day (signal_date, decided_at, model, universe, scored,
                            note) values (%s, %s, %s, 0, 0, %s)""",
                     (today, now, mdl.name, 'No Nifty 200 list loaded yet'))
        conn.commit()
        return {'status': 'no universe'}
    index_days = history(feed, INDEX_KEY, today, pause=pause)
    if not complete(index_days, today):
        return {'status': 'waiting', 'message': "Today's Nifty 50 session is not complete yet"}
    index_dates = sorted(index_days)[-HISTORY:]
    index = index_features([summary(index_days[d]) for d in index_dates])
    trade_day = next_weekday(today)
    scored, failures, mismatched = [], 0, 0
    for s in stocks:
        try:
            days = history(feed, s['instrument_key'], today, pause=pause)
        except FeedError:                 # one stock's bad answer skips that stock
            failures += 1
            if failures > MAX_FAILURES:
                raise
            continue
        dates = sorted(days)[-HISTORY:]
        if dates != index_dates:          # a missing session must not shift the features
            mismatched += 1
            continue
        summaries = [summary(days[d]) for d in dates]
        row = stock_features(summaries, index, trade_day)
        if row is None or row['value20'] < MIN_VALUE:
            continue
        scored.append((s, row, mdl.predict(row), summaries[-1]['close']))
    decided = clock()
    picks = ml.choose([(s['instrument_key'], p) for s, _, p, _ in scored], K, GATE)
    notes = [] if picks else ['No prediction cleared the 0.15% gate']
    if mismatched:
        notes.append(f'{mismatched} stocks withheld: their sessions do not match the index')
    conn.execute("""insert into evening_day (signal_date, decided_at, model, universe, scored,
                        note) values (%s, %s, %s, %s, %s, %s)""",
                 (today, decided, mdl.name, len(stocks), len(scored), '; '.join(notes)))
    by_key = {s['instrument_key']: (s, row, close) for s, row, _, close in scored}
    for rank, (key, side, pred) in enumerate(picks, 1):
        s, row, close = by_key[key]
        conn.execute("""insert into evening_pick (signal_date, instrument_key, company_id, symbol,
                            side, prediction, rank, features, status, decision_price)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)""",
                     (today, key, s['company_id'], s['symbol'], side, pred, rank,
                      Jsonb({k: round(v, 6) for k, v in row.items()}), close))
    conn.commit()
    return {'status': 'decided', 'scored': len(scored), 'picks': len(picks)}


def _session_bars(feed, key: str, day: dt.date, today: dt.date) -> list[Candle]:
    bars = feed.candles(key) if day == today else feed.candles_between(key, day, day)
    return [b for b in bars if b.start.astimezone(IST).date() == day]


def settle(conn, feed, now: dt.datetime, *, pause=time.sleep) -> int:
    """Close open calls once the session after their signal day has reached 15:15."""
    local = now.astimezone(IST)
    today = local.date()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select signal_date, instrument_key, side from evening_pick
                       where status = 'open' and signal_date < %s order by signal_date""",
                    (today,))
        rows = cur.fetchall()
    if not rows:
        return 0
    first = rows[0]['signal_date'] + dt.timedelta(days=1)
    index_bars = []
    if first < today:
        chunk = first
        while chunk < today:
            stop = min(chunk + dt.timedelta(days=27), today - dt.timedelta(days=1))
            index_bars += feed.candles_between(INDEX_KEY, chunk, stop)
            pause(PAUSE)
            chunk = stop + dt.timedelta(days=1)
    if local.time() >= SETTLE_FROM:
        index_bars += feed.candles(INDEX_KEY)
        pause(PAUSE)
    sessions = sorted(d for d, bars in ml.sessions(index_bars).items()
                      if any(_time(b) == EXIT_BAR for b in bars))
    rates = intraday_rates()
    n = 0
    for r in rows:
        trade = next((d for d in sessions if d > r['signal_date']), None)
        if trade is None:
            if r['signal_date'] < today - dt.timedelta(days=10):
                conn.execute("""update evening_pick set status = 'no data'
                                where signal_date = %s and instrument_key = %s""",
                             (r['signal_date'], r['instrument_key']))
            continue
        bars = _session_bars(feed, r['instrument_key'], trade, today)
        pause(PAUSE)
        entry = next((b.open for b in bars if _time(b) == ENTRY_BAR), None)
        exit_price = next((b.open for b in bars if _time(b) == EXIT_BAR), None)
        if not (entry and exit_price):
            if trade < today - dt.timedelta(days=3):
                conn.execute("""update evening_pick set status = 'no data', trade_date = %s
                                where signal_date = %s and instrument_key = %s""",
                             (trade, r['signal_date'], r['instrument_key']))
            continue
        res = ml.result(r['side'], entry, exit_price, rates, slip=SLIP)
        if res is None:          # priced above the Rs 1 lakh a call, as in the backtest
            conn.execute("""update evening_pick set status = 'skipped', trade_date = %s,
                                entry = %s, exit = %s
                            where signal_date = %s and instrument_key = %s""",
                         (trade, entry, exit_price, r['signal_date'], r['instrument_key']))
            continue
        conn.execute("""update evening_pick set status = 'closed', trade_date = %s, entry = %s,
                            exit = %s, quantity = %s, gross_inr = %s, charges_inr = %s,
                            net_inr = %s, net_pct = %s
                        where signal_date = %s and instrument_key = %s""",
                     (trade, entry, exit_price, res['quantity'], res['gross_inr'],
                      res['charges_inr'], res['net_inr'], res['net_pct'], r['signal_date'],
                      r['instrument_key']))
        n += 1
    conn.commit()
    return n


# --------------------------------------------------------------------------- reporting


def picks(conn, signal_date: dt.date | None = None, limit: int = 200) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select signal_date, trade_date, symbol, side, prediction::float8 prediction,
                              rank, entry::float8 entry, exit::float8 exit, quantity,
                              net_inr::float8 net_inr, net_pct::float8 net_pct, status,
                              approved_at, order_note
                       from evening_pick where %s::date is null or signal_date = %s
                       order by signal_date desc, rank limit %s""",
                    (signal_date, signal_date, limit))
        return cur.fetchall()


def record(conn) -> dict:
    """Totals of the closed paper calls and each evening's net result."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select count(*) trades, coalesce(sum(net_inr), 0)::float8 net_inr,
                              avg(net_pct)::float8 net_pct,
                              coalesce(sum(net_inr) filter (where net_inr > 0), 0)::float8 gains,
                              coalesce(-sum(net_inr) filter (where net_inr < 0), 0)::float8 losses,
                              count(distinct signal_date) days
                       from evening_pick where status = 'closed'""")
        total = cur.fetchone()
        cur.execute("""select d.signal_date session_date, d.scored, d.note,
                              count(p.*) filter (where p.status = 'closed') trades,
                              count(p.*) filter (where p.status in ('open', 'no data')) unresolved,
                              coalesce(sum(p.net_inr), 0)::float8 net_inr
                       from evening_day d left join evening_pick p using (signal_date)
                       group by d.signal_date, d.scored, d.note order by d.signal_date""")
        days = cur.fetchall()
    total['profit_factor'] = total['gains'] / total['losses'] if total['losses'] else None
    total.update(ml.daily_evidence(days))
    return {'total': total, 'days': days}


def message_calls(conn, signal_date: dt.date) -> str | None:
    rows = picks(conn, signal_date)
    if not rows:
        return None
    lines = [f"Evening intraday paper calls for the next session after "
             f"{signal_date:%d %b %Y} (paper only; no order is placed):"]
    for r in rows:
        side = 'BUY' if r['side'] == 1 else 'SELL short'
        lines.append(f"{side} {r['symbol']}: predicted {r['prediction'] * 100:+.2f}% "
                     "from the 09:15 open to 15:15")
    lines.append("Entry at the 09:15 candle's open, exit at the 15:15 candle's open. Tested "
                 "2025-2026: +0.14% a call after costs, all of it on short sells; not yet "
                 "proven forward. Tracked on the Trading page, Evening picks tab.")
    return "\n".join(lines)


def message_results(conn, trade_date: dt.date) -> str | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select symbol, side, net_inr::float8 net_inr, net_pct::float8 net_pct
                       from evening_pick where trade_date = %s and status = 'closed'
                       order by rank""", (trade_date,))
        rows = cur.fetchall()
    if not rows:
        return None
    lines = [f"Evening intraday paper results, {trade_date:%d %b %Y}, after charges on "
             "₹1 lakh each:"]
    for r in rows:
        side = 'BUY' if r['side'] == 1 else 'SELL'
        lines.append(f"{side} {r['symbol']}: ₹{r['net_inr']:+,.0f} ({r['net_pct']:+.2f}%)")
    lines.append(f"Day: ₹{sum(r['net_inr'] for r in rows):+,.0f}. "
                 f"Since the start: ₹{record(conn)['total']['net_inr']:+,.0f}.")
    return "\n".join(lines)


def order_message(pick: dict, preview: dict, deviation) -> str:
    """One evening call, to be approved by a reply (trading.ml_preview gives `preview`)."""
    from igs.intraday.trading import (
        EVENING_APPROVE_UNTIL,
        EVENING_PLACE_FROM,
        EVENING_PLACE_UNTIL,
        ML_MIN_USEFUL_RUPEES,
        ML_SQUARE_OFF,
    )
    side = 'BUY' if pick['side'] == 1 else 'SELL SHORT'
    price = float(pick['decision_price'])
    trade = next_weekday(pick['signal_date'])
    lines = [f"EVENING INTRADAY {side} · {pick['symbol']} · for {trade:%a %d %b}",
             f"Close ₹{price:.2f} on {pick['signal_date']:%d %b} · predicted "
             f"{pick['prediction'] * 100:+.2f}% from the 09:15 open to 15:15"]
    if preview['ok']:
        lines.append(f"Order at your settings: about {preview['quantity']} shares "
                     f"(₹{preview['value']:,.0f}) at the live price, stop about "
                     f"₹{preview['stop']:.2f} (half the 14-day ATR), squared off by the app "
                     f"at {ML_SQUARE_OFF:%H:%M} IST. Loss at the stop about "
                     f"₹{preview['loss']:,.0f}, charges about ₹{preview['charges']:,.0f}.")
        if preview['value'] < ML_MIN_USEFUL_RUPEES:
            lines.append(f"At ₹{preview['value']:,.0f} a trade, charges take most of the "
                         "tested edge: it was tested at ₹1 lakh (Settings, Intraday trading).")
    else:
        lines.append(f"At your settings no order can be placed: {preview['reason']}.")
    lines += ["Tested on paper from the 09:15 open, no stop, after charges: +0.14% a call, "
              "all of it on short sells. A real order enters about 09:20 with a stop, so it "
              "will differ. Not yet proven forward.",
              f"No order has been placed. Reply APPROVED to this message before "
              f"{EVENING_APPROVE_UNTIL:%H:%M} IST {trade:%a}, or approve on the Trading page "
              f"(Evening picks tab). It is placed between {EVENING_PLACE_FROM:%H:%M} and "
              f"{EVENING_PLACE_UNTIL:%H:%M} IST, and refused if the open is more than "
              f"{deviation}% from ₹{price:.2f}.\nhttps://stocks.ednis.ai/"]
    return "\n".join(lines)


def send_order_calls(conn, now: dt.datetime, sender) -> int:
    """Each of the latest evening's calls in its own Telegram message, to be approved by a
    reply, while approvals are open and evening orders are on. A failed send is retried on
    the next run, three times at most. Returns the messages sent."""
    from igs.alerts.operations import record_issue
    from igs.intraday.trading import evening_orders_on, evening_window, ml_preview, settings
    if not evening_orders_on(conn):
        return 0
    cfg, rates = settings(conn), intraday_rates()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select signal_date, instrument_key, symbol, side,
                              prediction::float8 prediction, features, decision_price
                       from evening_pick
                       where signal_date = (select max(signal_date) from evening_day)
                         and status = 'open' and telegram_message_id is null
                         and telegram_attempts < 3 and decision_price is not null
                       order by rank""")
        rows = [r for r in cur.fetchall() if evening_window(now, r['signal_date'])]
    conn.commit()
    sent = 0
    for pick in rows:
        text = order_message(pick, ml_preview(pick, cfg, rates), cfg['max_price_deviation_pct'])
        try:
            message_id = sender(text)
        except Exception as exc:  # noqa: BLE001 - retried on the next run
            record_issue('intraday-evening', type(exc).__name__)
            message_id = None
        conn.execute("""update evening_pick set telegram_attempts = telegram_attempts + 1,
                            telegram_message_id = %s
                        where signal_date = %s and instrument_key = %s""",
                     (message_id if type(message_id) is int else None, pick['signal_date'],
                      pick['instrument_key']))
        conn.commit()
        sent += type(message_id) is int
    return sent


def step(conn, feed=None, *, clock=utc_now, pause=time.sleep, notify=None,
         sender=None) -> str:
    """Settle calls whose session has reached 15:15, and after the close decide tonight's.
    With evening orders on, `sender` sends each call its own message to approve by a reply
    (send_order_calls) instead of the one paper note. Returns what it did."""
    if not conn.execute('select pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0]:
        return 'Evening intraday: another run is in progress'
    conn.commit()
    own = feed is None
    try:
        now = clock()
        local = now.astimezone(IST)
        if local.weekday() >= 5:
            return 'Evening intraday: weekend'
        if own:
            from igs.intraday.scanner import token
            from igs.intraday.upstox import Upstox
            if not token():
                return 'Evening intraday: no Upstox access token'
            feed = Upstox(token())
        done = []
        settled = settle(conn, feed, now, pause=pause)
        if settled:
            done.append(f'settled {settled}')
            if notify and local.time() >= SETTLE_FROM:
                text = message_results(conn, local.date())
                if text:
                    notify(text)
        if DECIDE_FROM <= local.time() < DECIDE_UNTIL:
            res = decide(conn, feed, now, clock=clock, pause=pause)
            done.append(str(res))
            from igs.intraday.trading import evening_orders_on
            if res['status'] == 'decided' and notify and not (sender and evening_orders_on(conn)):
                text = message_calls(conn, local.date())
                if text:
                    notify(text)
        if sender:
            sent = send_order_calls(conn, clock(), sender)
            if sent:
                done.append(f'sent {sent} calls to approve')
        return 'Evening intraday: ' + ('; '.join(done) or 'nothing due')
    finally:
        if own and feed is not None:
            feed.close()
        conn.rollback()
        conn.execute('select pg_advisory_unlock(%s)', (LOCK,))
        conn.commit()
