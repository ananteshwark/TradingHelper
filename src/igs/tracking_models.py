"""Shadow predictions of research/adverse's models for the 09:45 calls and the evening picks
(docs/TRACKING.md). They are recorded beside each call and change nothing: none of the
models passed its holdout.

- At the entry (the call's first tick), A1: the trade's net return after costs (`a1_net`;
  research's VR vetoes a call when it is below 0) and the probability its stop is hit
  (`a1_stop`).
- At 15, 30, 45, 75 and 135 minutes after the entry, while the trade is open, A2: the rest
  of the trade before costs (`a2_rest`) and the probability its stop is hit later
  (`a2_stop`).

The parameters are research/adverse/paths.py's, signed for the side. The path's come from
the tracker's ticks, every five minutes, where the research had every candle: its best and
worst moves can only be as extreme as the ticks saw.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from functools import lru_cache
from pathlib import Path

from psycopg.rows import dict_row

from igs.config import config_dir
from igs.timeutil import IST

CALL_SETS = {'ml_0945': 'm0945', 'evening': 'evening'}
CHECK_MINUTES = (15, 30, 45, 75, 135)
STOP_ATR = 0.5
# Scale-like inputs stay as they are; positions in a range (0-1) become 1 - x for a short
# sell; every other input is multiplied by the side (paths.py).
POSITION = {'clv30', 'prev_clv', 'breadth'}


class Trees:
    """Gradient-boosted trees exported by research/adverse/export_models.py. A node is
    [feature, threshold, left, right, default_left, missing]: missing 2 sends a missing
    value to the default side, 1 sends zeros and missing values there, 0 counts a missing
    value as zero, as LightGBM does."""

    def __init__(self, data: dict):
        self.features, self.link, self.trees = data['features'], data['link'], data['trees']

    def raw(self, row: dict) -> float:
        x = [row.get(f) for f in self.features]
        total = 0.0
        for node in self.trees:
            while isinstance(node, list):
                f, threshold, left, right, default_left, missing = node
                v = x[f]
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    if missing == 2:
                        node = left if default_left else right
                        continue
                    v = 0.0
                if missing == 1 and v == 0.0:
                    node = left if default_left else right
                    continue
                node = left if v <= threshold else right
            total += node
        return total

    def predict(self, row: dict) -> float:
        r = self.raw(row)
        return 1 / (1 + math.exp(-r)) if self.link == 'logistic' else r


@lru_cache(maxsize=4)
def models(call_set: str, path: Path | None = None) -> dict:
    doc = json.loads((path or config_dir() / 'models' / f'adverse_{call_set}_v1.json')
                     .read_text())
    return {'name': doc['name'], **{k: Trees(v) for k, v in doc['models'].items()}}


def signed(raw: dict, side: int) -> dict:
    """paths.py's signing of the decision-time inputs, as s_<name>."""
    out = {}
    for k, v in raw.items():
        if v is None or not isinstance(v, (int, float)):
            continue
        out[f's_{k}'] = (v if side == 1 else 1 - v) if k in POSITION else v * side
    return out


def _call(conn, rec: dict) -> dict | None:
    """The call behind a tracked 09:45 call or evening pick: its features, prediction, its
    rank on its side that day, and the trading day."""
    day_s, key = rec['ref'].split('|', 1)
    day = dt.date.fromisoformat(day_s)
    table, col = (('ml_intraday_pick', 'session_date') if rec['source'] == 'ml_0945'
                  else ('evening_pick', 'signal_date'))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f'select instrument_key, side, prediction, features from {table} '
                    f'where {col} = %s', (day,))
        rows = cur.fetchall()
    me = next((r for r in rows if r['instrument_key'] == key), None)
    if me is None:
        return None
    side, pred = me['side'], float(me['prediction'])
    rank = 1 + sum(1 for r in rows if r['side'] == side
                   and side * float(r['prediction']) > side * pred)
    return {'features': me['features'] or {}, 'side': side, 'prediction': pred, 'rank': rank,
            'trade_day': rec['track_from'].astimezone(IST).date()}


def nifty_inputs(conn, nifty: dict | None, day: dt.date) -> dict:
    """The Nifty 50's gap, move from its open, previous day and last 5 days, at a quote."""
    out = {'nd_gap': None, 'nd_sofar': None, 'nd_prev1': None, 'nd_r5': None}
    if nifty and nifty.get('open') and nifty.get('prev_close'):
        out['nd_gap'] = nifty['open'] / nifty['prev_close'] - 1
        out['nd_sofar'] = nifty['price'] / nifty['open'] - 1
    closes = [float(c) for (c,) in conn.execute(
        """select close from ml_intraday_session where instrument_key = 'NSE_INDEX|Nifty 50'
           and session_date < %s order by session_date desc limit 6""", (day,)).fetchall()]
    if len(closes) >= 2:
        out['nd_prev1'] = closes[0] / closes[1] - 1
    if len(closes) >= 6:
        out['nd_r5'] = closes[0] / closes[5] - 1
    return out


def breadth(universe_quotes: dict) -> float | None:
    """The share of stocks above their previous close."""
    ups = [q['price'] > q['prev_close'] for q in universe_quotes.values()
           if q.get('prev_close')]
    return sum(ups) / len(ups) if len(ups) >= 50 else None


def decision_row(call: dict, nifty: dict, breadth_now: float | None) -> dict:
    side = call['side']
    raw = {k: v for k, v in call['features'].items() if isinstance(v, (int, float))}
    row = {**raw, **signed({**raw, **nifty, 'breadth': breadth_now}, side)}
    row.update(conviction=side * call['prediction'], rank=call['rank'], side=side,
               dow=call['trade_day'].isoweekday())
    return row


def _expected(curve, at: dt.datetime) -> float | None:
    """The usual cumulative volume by `at` (its last closed five-minute slot)."""
    if not curve:
        return None
    t = at.astimezone(IST)
    slot = (t.hour * 60 + t.minute - 555) // 5 - 1
    if slot < 0:
        return 0.0
    return curve[min(slot, 74)]


def path_row(entry: dict, ticks: list[dict], now: dict, curve, side: int, atr: float | None,
             minutes: int) -> dict:
    """paths.py's checkpoint parameters from the ticks since the entry, `now` the latest."""
    price = now['price']
    move = side * (price / entry['price'] - 1)
    moves = [side * (t['price'] / entry['price'] - 1) for t in ticks]
    back = [t for t in ticks if t['at'] <= now['at'] - dt.timedelta(minutes=15)]
    ago = back[-1] if back else entry
    row = {'minutes': minutes, 'move': move,
           'move_atr': move / atr if atr else None,
           'best_atr': max([0.0, *moves]) / atr if atr else None,
           'worst_atr': min([0.0, *moves]) / atr if atr else None,
           'last15': side * (price / ago['price'] - 1),
           'vwap_gap': side * (price / now['vwap'] - 1) if now.get('vwap') else None,
           'rvol_since': None, 'rvol_15': None, 'range_pos': None, 'nifty_move': None,
           'rel_move': None}
    for name, start in (('rvol_since', entry), ('rvol_15', ago)):
        e0, e1 = _expected(curve, start['at']), _expected(curve, now['at'])
        if e0 is not None and e1 and e1 > e0 and now.get('volume') and start.get('volume'):
            row[name] = (now['volume'] - start['volume']) / (e1 - e0)
    hi, lo = now.get('day_high'), now.get('day_low')
    if hi and lo and hi > lo:
        pos = (price - lo) / (hi - lo)
        row['range_pos'] = pos if side == 1 else 1 - pos
    if now.get('nifty') and entry.get('nifty'):
        row['nifty_move'] = side * (now['nifty'] / entry['nifty'] - 1)
        row['rel_move'] = move - row['nifty_move']
    return row


def score_entry(conn, rec: dict, nifty: dict | None, universe_quotes: dict,
                tick: dict) -> dict | None:
    """A1 at the entry; the shadow record to keep."""
    call_set = CALL_SETS.get(rec['source'])
    call = _call(conn, rec) if call_set else None
    if call is None:
        return None
    m = models(call_set)
    row = decision_row(call, nifty_inputs(conn, nifty, call['trade_day']),
                       breadth(universe_quotes))
    net, stop = m['a1_net'].predict(row), m['a1_stop'].predict(row)
    return {'model': m['name'], 'entry': tick, 'decision': row,
            'a1': {'net': round(net, 6), 'stop_p': round(stop, 4), 'veto': net < 0},
            'a2': {}}


def score_checks(rec: dict, shadow: dict, ticks: list[dict], now: dict) -> dict:
    """A2 at each checkpoint reached since the last tick, while the stop hasn't been hit."""
    m = models(CALL_SETS[rec['source']])
    entry = shadow['entry']
    side = rec['side']
    atr = shadow['decision'].get('atr_pct')
    if atr:
        stop = entry['price'] * (1 - side * STOP_ATR * atr)
        if any((t['price'] - stop) * side <= 0 for t in ticks):
            shadow['stopped_at_tick'] = True
            return shadow
    elapsed = (now['at'] - entry['at']).total_seconds() / 60
    for n, minutes in enumerate(CHECK_MINUTES, 1):
        if str(n) in shadow['a2'] or elapsed < minutes:
            continue
        row = {**shadow['decision'],
               **path_row(entry, ticks, now, rec.get('volume_curve'), side, atr, minutes)}
        rest, stop_p = m['a2_rest'].predict(row), m['a2_stop'].predict(row)
        shadow['a2'][str(n)] = {'at': now['at'].isoformat(), 'rest': round(rest, 6),
                                'stop_p': round(stop_p, 4), 'exit_e0': rest < 0,
                                'exit_c50': stop_p >= 0.5}
    return shadow
