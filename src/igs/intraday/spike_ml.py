"""Spike picks: the spike-recovery model on paper (docs/SPIKES.md,
research/spike_ml/PAPER.md). Nothing here places an order.

Every 5 minutes in the session, as part of the spike watcher (igs.intraday.spikes), each
liquid NSE stock whose range today is 5% or more is checked for decision points: the first
candle whose move within 30 minutes reaches 5, 6, 7, 8, 10, 12 or 15%. A fixed LightGBM
model (config/models/spike_ml_v1.json, trained once on 2022 – 8 October 2026) predicts the
after-cost result of trading against the move with targets of 1–5%. The first point whose
best prediction exceeds 0.5% is the stock's pick for the day, with that target: a buy after
a fall, a short sale after a rise. Picks go to Telegram (the spikes chat) and are recorded
on paper in two records, buys and short sales. A short sale whose upper circuit is less
than 4% above the price is not taken.

The features must stay identical to the ones the model was trained on
(research/spike_ml/events.py); export_model.py checks that they are.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from functools import lru_cache
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from igs.config import config_dir
from igs.intraday import price_bands, spikes
from igs.intraday.engine import BAR
from igs.intraday.ml import Model
from igs.timeutil import IST, utc_now

XS = (0.05, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15)
TARGETS = (1, 2, 3, 4, 5)                  # %
GATE = 0.5                                 # predicted net % a trade
OPEN, LAST, EXIT = 9 * 60 + 15, 14 * 60 + 55, 15 * 60 + 15
WINDOW = 25                                # minutes from the first candle's start to the last's
IN_TIME = dt.timedelta(minutes=10)         # after the decision candle closed
HOT_MOVE = 0.015                           # a price move since the last scan
NIFTY = 'NSE_INDEX|Nifty 50'
SETTLE_OLD = 20                            # earlier days' picks settled a scan
MIN_TRADES = 100
FEATURES = ['f_move', 'f_side', 'f_speed', 'f_time', 'f_day_ret', 'f_from_prev', 'f_gap',
            'f_range', 'f_from_extreme', 'f_bar_range', 'f_body', 'f_close_loc', 'f_r1',
            'f_r2', 'f_r3', 'f_vol_ratio', 'f_value_so_far', 'f_log_value', 'f_log_price',
            'f_prev_ret', 'f_nifty_day', 'f_nifty_30', 'f_crowd', 'f_crowd_side']


# --------------------------------------------------------------------------- the model


class Picker:
    """One exported regressor a target (igs.intraday.ml.Model's compact trees)."""

    def __init__(self, data: dict):
        self.meta = {k: v for k, v in data.items() if k != 'models'}
        self.features, self.gate = data['features'], data['gate']
        self.models = [Model({'name': f"{data['name']}-{t}", 'features': data['features'],
                              'trees': trees})
                       for t, trees in zip(data['targets'], data['models'], strict=True)]
        self.targets = data['targets']

    def predict(self, row: dict) -> list[float]:
        return [m.predict(row) for m in self.models]


@lru_cache(maxsize=1)
def picker(path: Path | None = None) -> Picker:
    return Picker(json.loads((path or config_dir() / 'models' / 'spike_ml_v1.json').read_text()))


# --------------------------------------------------------------------------- features


def _minutes(bar) -> int:
    t = bar.start.astimezone(IST)
    return t.hour * 60 + t.minute


def points(bars, prevclose: float, prev_ret: float | None, med20: float,
           nifty=None) -> list[dict]:
    """Each threshold's first decision point among today's closed candles, with its
    features except the crowd's: the same as research/spike_ml/events.py. `nifty`: the
    Nifty 50's closed candles today."""
    t = [_minutes(b) for b in bars]
    o = [b.open for b in bars]
    h = [b.high for b in bars]
    lo = [b.low for b in bars]
    c = [b.close for b in bars]
    v = [b.volume for b in bars]
    n = len(bars)
    rise, fall, ilow, ihigh = [0.0] * n, [0.0] * n, [0] * n, [0] * n
    first = 0
    for i in range(n):
        while t[first] < t[i] - WINDOW:
            first += 1
        low, high, il, ih = lo[i], h[i], i, i
        for j in range(i - 1, max(first, i - 5) - 1, -1):
            if lo[j] < low:
                low, il = lo[j], j
            if h[j] > high:
                high, ih = h[j], j
        rise[i], fall[i], ilow[i], ihigh[i] = c[i] / low - 1, c[i] / high - 1, il, ih
    if nifty:
        nm = [_minutes(b) for b in nifty]
        no0, nc = nifty[0].open, [b.close for b in nifty]
    out = []
    for x in XS:
        hit = next((i for i in range(n) if t[i] <= LAST and (rise[i] >= x or fall[i] <= -x)),
                   None)
        if hit is None:
            continue
        i = hit
        side = 1 if rise[i] >= x else -1          # 1: a rise, faded by a short sale
        ext = ilow[i] if side == 1 else ihigh[i]
        day_hi, day_lo = max(h[:i + 1]), min(lo[:i + 1])
        rng = h[i] - lo[i]
        prev = [c[i - j] / c[i - j - 1] - 1 if i - j - 1 >= 0 else 0.0 for j in (1, 2, 3)]
        earlier = sum(v[:i]) / i if i > 0 else 0.0
        f = {'f_move': abs(rise[i] if side == 1 else fall[i]) * 100,
             'f_side': side,
             'f_speed': float(t[i] - t[ext]),
             'f_time': float(t[i] - OPEN),
             'f_day_ret': (c[i] / o[0] - 1) * 100,
             'f_from_prev': (c[i] / prevclose - 1) * 100,
             'f_gap': (o[0] / prevclose - 1) * 100,
             'f_range': (day_hi / day_lo - 1) * 100,
             'f_from_extreme': side * (c[i] / (day_hi if side == 1 else day_lo) - 1) * 100,
             'f_bar_range': (h[i] / lo[i] - 1) * 100,
             'f_body': abs(c[i] - o[i]) / rng if rng > 0 else 0.0,
             'f_close_loc': (c[i] - lo[i]) / rng if rng > 0 else 0.5,
             'f_r1': prev[0] * 100, 'f_r2': prev[1] * 100, 'f_r3': prev[2] * 100,
             'f_vol_ratio': v[i] / earlier if i > 0 and earlier > 0 else 1.0,
             'f_value_so_far': sum(a * b for a, b in zip(v[:i + 1], c[:i + 1],
                                                         strict=True)) / med20,
             'f_log_value': math.log(med20), 'f_log_price': math.log(c[i]),
             'f_prev_ret': (prev_ret or 0.0) * 100,
             'f_nifty_day': 0.0, 'f_nifty_30': 0.0}
        if nifty:
            j = sum(1 for m in nm if m <= t[i]) - 1
            j30 = sum(1 for m in nm if m <= t[i] - 30) - 1
            if j >= 0:
                f['f_nifty_day'] = (nc[j] / no0 - 1) * 100
                f['f_nifty_30'] = (nc[j] / (nc[j30] if j30 >= 0 else no0) - 1) * 100
        out.append({'x': x, 'i': i, 'minute': t[i], 'side': side, 'bar': bars[i].start,
                    'close': c[i], 'from': lo[ext] if side == 1 else h[ext], 'features': f})
    return out


def settle(bars, decision_bar: dt.datetime, side: int, target_pct: int,
           final: bool) -> dict | None:
    """The trade against the move from the candle after `decision_bar`: entry, exit and
    why, or None while it is still open. `final`: no more candles will come today."""
    k = next((n for n, b in enumerate(bars) if b.start > decision_bar), None)
    if k is None or _minutes(bars[k]) >= EXIT:
        return {'void': True} if final else None
    s = -side
    entry = bars[k].open
    target = entry * (1 + s * spikes.SLIP) * (1 + s * target_pct / 100)
    for bar in bars[k:]:
        if _minutes(bar) >= EXIT:
            return {'entry_bar': bars[k].start, 'entry': entry, 'target_price': target,
                    'exit_bar': bar.start, 'exit': bar.open, 'exit_reason': 'time',
                    'locked': bar.high == bar.low}
        if (bar.low <= target) if s < 0 else (bar.high >= target):
            beyond = (bar.open <= target) if s < 0 else (bar.open >= target)
            return {'entry_bar': bars[k].start, 'entry': entry, 'target_price': target,
                    'exit_bar': bar.start, 'exit': bar.open if beyond else target,
                    'exit_reason': 'target', 'locked': bar.high == bar.low}
    if final:
        last = bars[-1]
        return {'entry_bar': bars[k].start, 'entry': entry, 'target_price': target,
                'exit_bar': last.start, 'exit': last.close, 'exit_reason': 'time',
                'locked': last.high == last.low}
    return None


# --------------------------------------------------------------------------- the scan


def _previous(conn, isin: str, day: dt.date):
    """The last session's close and its return, from the loaded bhavcopy."""
    row = conn.execute("""select close::float8, prev_close::float8 from price_eod
                          where exchange = 'NSE' and isin = %s and series = 'EQ'
                            and trade_date < %s order by trade_date desc limit 1""",
                       (isin, day)).fetchone()
    if not row or not row[0]:
        return None, None
    return row[0], (row[0] / row[1] - 1) if row[1] else None


def watch(conn, day: dt.date, stocks: list[dict], quotes: dict) -> int:
    """Today's candidates: the liquid stocks whose range is 5% or more, with the previous
    scan's quote kept to tell which are in play."""
    added = 0
    for s in stocks:
        q = quotes.get(s['instrument_key'])
        if not q or s['value'] < spikes.MIN_VALUE or q[0] < (1 + XS[0]) * q[1]:
            continue
        last = q[2] if len(q) > 2 else None
        cur = conn.execute("""update intraday_spike_ml set prev_high = day_high,
                                  prev_low = day_low, prev_price = last_price, day_high = %s,
                                  day_low = %s, last_price = %s
                              where trade_date = %s and instrument_key = %s""",
                           (q[0], q[1], last, day, s['instrument_key']))
        if cur.rowcount:
            continue
        prevclose, prev_ret = _previous(conn, s['isin'], day)
        if prevclose is None:
            continue
        conn.execute("""insert into intraday_spike_ml (trade_date, instrument_key, isin,
                            symbol, med20, prevclose, prev_ret, day_high, day_low, last_price)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (day, s['instrument_key'], s['isin'], s['symbol'], s['value'], prevclose,
                      prev_ret, q[0], q[1], last))
        added += 1
    return added


def to_check(conn, now: dt.datetime, limit: int) -> list[dict]:
    """Today's stocks whose candles this scan needs: after 15:20 the picks to settle, then
    those in play, then the longest unchecked."""
    local = now.astimezone(IST)
    settle_now = local.time() >= spikes.RESULTS_FROM
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            select * from intraday_spike_ml where trade_date = %(day)s and not done
              and (status = 'watching' or %(settle)s)
            order by (status in ('picked', 'missed')) desc,
                     (checked_at is null or day_high > prev_high or day_low < prev_low
                      or abs(last_price / nullif(prev_price, 0) - 1) >= %(hot)s) desc,
                     checked_at nulls first, day_high / day_low desc
            limit %(limit)s""",
                    {'day': local.date(), 'settle': settle_now, 'hot': HOT_MOVE,
                     'limit': max(0, limit)})
        return cur.fetchall()


def _update(conn, row: dict, values: dict) -> None:
    sets = ', '.join(f'{k} = %({k})s' for k in values)
    conn.execute(f'update intraday_spike_ml set {sets} where trade_date = %(trade_date)s '
                 f'and instrument_key = %(instrument_key)s',
                 {**values, 'trade_date': row['trade_date'],
                  'instrument_key': row['instrument_key']})


def _crowd(conn, day: dt.date, minute: int, side: int) -> tuple[int, int]:
    """The 5% decision points found so far today before `minute`: in all, and on `side`."""
    return conn.execute("""select count(*), count(*) filter (where first5_side = %s)
                           from intraday_spike_ml where trade_date = %s
                             and first5_minute < %s""", (side, day, minute)).fetchone()


def check(conn, rows: list[dict], bars: dict, now: dt.datetime, nifty=None,
          model: Picker | None = None, state: dict | None = None) -> int:
    """Judge the new decision points of `rows` (bars: {instrument key: today's closed
    candles}) and settle their picks. Returns the new picks."""
    local = now.astimezone(IST)
    state = {} if state is None else state
    found = {}
    for row in rows:                      # first every stock's 5% point, for the crowd
        b = bars.get(row['instrument_key'])
        if not b:
            continue
        found[row['instrument_key']] = pts = points(b, float(row['prevclose']), row['prev_ret'],
                                                    float(row['med20']), nifty)
        five = next((p for p in pts if p['x'] == XS[0]), None)
        if five and row['first5_minute'] is None:
            _update(conn, row, {'first5_minute': five['minute'], 'first5_side': five['side']})
    picks = 0
    for row in rows:
        values = {'checked_at': now}
        pts = found.get(row['instrument_key'])
        if pts is not None and row['status'] == 'watching':
            values.update(_judge(conn, row, pts, now, model or picker(), state))
            picks += values.get('status') == 'picked'
        status = values.get('status', row['status'])
        if status in ('picked', 'missed') and row['instrument_key'] in bars:
            done = _settle_values({**row, **values}, bars[row['instrument_key']],
                                  final=local.time() >= spikes.CLOSE_FROM)
            values.update(done)
            status = values.get('status', status)
        values['done'] = (status in ('closed', 'none', 'void')
                          or (status == 'missed' and values.get('net_inr', row['net_inr'])
                              is not None))
        _update(conn, row, values)
    return picks


def _judge(conn, row: dict, pts: list[dict], now: dt.datetime, model: Picker,
           state: dict) -> dict:
    """The first new decision point whose best prediction clears the gate, if any."""
    local = now.astimezone(IST)
    done_until = row['evaluated_until'] if row['evaluated_until'] is not None else -1
    seen, values = set(), {}
    for p in sorted(pts, key=lambda p: (p['minute'], p['x'])):
        if p['i'] in seen or p['minute'] <= done_until:
            continue
        seen.add(p['i'])
        crowd, crowd_side = _crowd(conn, local.date(), p['minute'], p['side'])
        f = {**p['features'], 'f_crowd': crowd, 'f_crowd_side': crowd_side}
        preds = model.predict(f)
        values['evaluated_until'] = p['minute']
        best = max(preds)
        if best <= model.gate:
            continue
        target = model.targets[preds.index(best)]
        late = now - (p['bar'] + BAR) > IN_TIME
        values.update(status='missed' if late else 'picked', side=p['side'], x=p['x'],
                      decision_bar=p['bar'], move_pct=round(p['features']['f_move'], 3),
                      move_from=p['from'], move_to=p['close'], expected_pct=round(best, 3),
                      target_pct=target, preds=Jsonb([round(x, 4) for x in preds]),
                      features=Jsonb({k: round(float(v), 6) for k, v in f.items()}),
                      alert_status='late' if late else None)
        values.update(_circuit(conn, row, p, now, state))
        return values
    if local.time() >= dt.time(15, 0):
        values['status'] = 'none'
    return values


def _circuit(conn, row: dict, p: dict, now: dt.datetime, state: dict) -> dict:
    """Buys are always taken; a short sale isn't when its upper circuit is less than 4%
    above the decision candle's close, or isn't known."""
    if 'bands' not in state:
        try:
            state['bands'] = price_bands.bands(now=now)
        except spikes.FeedError:
            state['bands'] = None
    prev = price_bands.previous_close(conn, row['isin'], now)
    b = spikes.band_check(state['bands'], row['symbol'], prev, -p['side'], p['close'])
    if p['side'] == -1:
        b['band_takes'] = True
    return {'takes' if k == 'band_takes' else k: v for k, v in b.items()}


def _settle_values(row: dict, bars, final: bool) -> dict:
    out = settle(bars, row['decision_bar'], row['side'], row['target_pct'], final)
    if out is None:
        return {}
    if out.get('void'):
        return {'status': 'void'}
    res = spikes.result(-row['side'], out['entry'], out['exit'], out['exit_reason']) or {}
    status = 'closed' if row['status'] == 'picked' else row['status']
    return {**out, **res, 'status': status}


def settle_old(conn, feed, now: dt.datetime, pause) -> int:
    """Earlier days' picks still open (the watcher stopped before 15:20): settled from
    that day's candles."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike_ml where trade_date < %s
                         and status in ('picked', 'missed') and exit_bar is null
                       order by trade_date limit %s""",
                    (now.astimezone(IST).date(), SETTLE_OLD))
        rows = cur.fetchall()
    n = 0
    for row in rows:
        day = row['trade_date']
        try:
            candles = feed.candles_between(row['instrument_key'], day, day)
        except spikes.FeedError:
            break
        finally:
            pause(spikes.PAUSE)
        bars = [b for b in candles if b.start.astimezone(IST).date() == day
                and dt.time(9, 15) <= b.start.astimezone(IST).time() < dt.time(15, 30)]
        values = _settle_values(row, bars, final=True) if bars else {'status': 'void'}
        values['done'] = True
        _update(conn, row, values)
        n += 1
    conn.commit()
    return n


# --------------------------------------------------------------------------- messages


def _hm(start: dt.datetime, close: bool = False) -> str:
    return (start + (BAR if close else dt.timedelta())).astimezone(IST).strftime('%H:%M')


def alert_message(rows: list[dict]) -> str:
    taken = [r for r in rows if r['takes']]
    lines = [f"SPIKE PICKS (the model, on paper): {len(taken)} new."]
    for r in taken:
        what = 'BUY' if r['side'] == -1 else 'SHORT'
        move = 'fall' if r['side'] == -1 else 'rise'
        aim = float(r['move_to']) * (1 - r['side'] * r['target_pct'] / 100)
        lines.append(f"{what} {r['symbol']} after a {float(r['move_pct']):.1f}% {move} within "
                     f"30 minutes (₹{float(r['move_from']):,.2f} → ₹{float(r['move_to']):,.2f}"
                     f" by {_hm(r['decision_bar'], close=True)}). The model expects "
                     f"{float(r['expected_pct']):+.1f}% after costs with a {r['target_pct']}% "
                     f"target (about ₹{aim:,.2f}); paper entry at the "
                     f"{_hm(r['decision_bar'], close=True)} open, out by 15:15.")
    for r in rows:
        if not r['takes']:
            why = ("its circuit limit isn't known" if r['band_status'] == 'unknown' else
                   f"its upper circuit ₹{float(r['band_limit']):,.2f} is only "
                   f"{float(r['band_room_pct']):.1f}% above")
            lines.append(f"Not taken: SHORT {r['symbol']} after a {float(r['move_pct']):.1f}% "
                         f"rise; {why}.")
    lines.append("Paper only (Paper tracking, Spike picks). In the 2025–2026 test the model's "
                 "buys made +0.60% a trade and its short sales −0.09%; neither is proven.")
    return "\n".join(lines)


def send_alerts(conn, now: dt.datetime, sender) -> int:
    """Today's new picks, in one message."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike_ml where trade_date = %s
                         and status in ('picked', 'closed', 'void') and alert_status is null
                       order by decision_bar, symbol""", (now.astimezone(IST).date(),))
        rows = cur.fetchall()
    if not rows or not sender(alert_message(rows)):
        return 0
    for r in rows:
        _update(conn, r, {'alert_status': 'sent', 'alert_sent_at': now})
    conn.commit()
    return len(rows)


def send_results(conn, day: dt.date, sender) -> bool:
    """After 15:20, once every pick of the day is settled: the day's results and the
    records since the start."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike_ml where trade_date = %s
                         and status in ('picked', 'closed', 'missed', 'void')
                       order by decision_bar, symbol""", (day,))
        rows = cur.fetchall()
    if any(r['status'] == 'picked' or (r['status'] == 'missed' and r['exit_bar'] is None)
           for r in rows):
        return False
    todo = [r for r in rows if r['result_sent_at'] is None and r['status'] in ('closed',
                                                                              'missed')]
    if not todo:
        return False
    taken = [r for r in todo if r['status'] == 'closed' and r['takes']]
    lines = [f"Spike picks (paper), {day:%d %b}: {len(taken)} trade"
             f"{'' if len(taken) == 1 else 's'}."]
    for r in taken:
        what = 'BUY' if r['side'] == -1 else 'SHORT'
        how = 'target' if r['exit_reason'] == 'target' else 'out at 15:15'
        lines.append(f"{what} {r['symbol']}: {how}, ₹{float(r['net_inr']):+,.0f} "
                     f"({float(r['net_pct']):+.2f}%).")
    for r in todo:
        if r['status'] == 'closed' and not r['takes']:
            lines.append(f"Not taken, near the upper circuit: SHORT {r['symbol']} would have "
                         f"made ₹{float(r['net_inr']):+,.0f}.")
        elif r['status'] == 'missed' and r['net_inr'] is not None:
            what = 'BUY' if r['side'] == -1 else 'SHORT'
            lines.append(f"Missed (found too late): {what} {r['symbol']} would have made "
                         f"₹{float(r['net_inr']):+,.0f}.")
    rec = record(conn)
    for key, name in (('buys', 'Buys'), ('shorts', 'Short sales'),
                      ('all_shorts', 'Every short sale, for comparison')):
        today = sum(float(r['net_inr']) for r in todo if r['status'] == 'closed'
                    and (r['side'] == -1 if key == 'buys' else r['side'] == 1)
                    and (key != 'shorts' or r['takes']))
        s = rec[key]
        lines.append(f"{name}: ₹{today:+,.0f} today; ₹{s['net_inr']:+,.0f} since the start "
                     f"({s['trades']} trade{'' if s['trades'] == 1 else 's'}).")
    if not sender("\n".join(lines)):
        return False
    for r in todo:
        _update(conn, r, {'result_sent_at': utc_now()})
    conn.commit()
    return True


# --------------------------------------------------------------------------- the record


def record(conn) -> dict:
    """The records over every closed pick: `buys`, `shorts` (the short sales taken),
    `all_shorts` (every short sale, the comparison), `missed`, each with its verdict."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select trade_date from intraday_spike_day order by trade_date")
        days = [r['trade_date'] for r in cur.fetchall()]
        cur.execute("""select * from intraday_spike_ml where status in ('closed', 'missed')
                         and net_inr is not null order by trade_date, decision_bar""")
        rows = cur.fetchall()
    closed = [r for r in rows if r['status'] == 'closed']
    out = {'buys': [r for r in closed if r['side'] == -1],
           'shorts': [r for r in closed if r['side'] == 1 and r['takes']],
           'all_shorts': [r for r in closed if r['side'] == 1],
           'missed': [r for r in rows if r['status'] == 'missed']}
    out = {k: spikes._summary(v, days) for k, v in out.items()}
    for k in ('buys', 'shorts', 'all_shorts'):
        s = out[k]
        if s['trades'] < MIN_TRADES:
            s['verdict'] = f'too early: {s["trades"]} of {MIN_TRADES} trades'
        elif s['daily_inr'] > 0 and (s['t'] or 0) >= 2.0:
            s['verdict'] = 'passes (a candidate for real orders, the owner decides)'
        else:
            s['verdict'] = 'does not pass'
    out['since'] = days[0] if days else None
    return out


def today(conn, day: dt.date) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike_ml where trade_date = %s
                         and decision_bar is not null order by decision_bar, symbol""", (day,))
        return cur.fetchall()


def trades(conn, limit: int = 300) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select * from intraday_spike_ml where decision_bar is not null
                       order by trade_date desc, decision_bar desc limit %s""", (limit,))
        return cur.fetchall()
