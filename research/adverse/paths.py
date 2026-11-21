"""Study A's trades, labels, parameters and checkpoints (PROTOCOL.md).

Usage: uv run python -I paths.py DATA_DIR OUT_DIR

DATA_DIR holds candles/ (Upstox 5-minute candles, one JSON file a stock, universe.json
and NSE_INDEX_Nifty_50.json) and the two models' tables: pred_0945.parquet,
feat_0945.parquet, pred_evening.parquet, feat_evening.parquet.

Writes, for each call set (`m0945`, `evening`):
- OUT_DIR/<set>_trades.parquet: one row a superset trade (each day's 10 highest
  predictions above 0 as buys and 10 lowest below 0 as short sells), with `pick` marking
  the app's own calls, its decision-time parameters (raw and signed for the side), and
  its path by the order rule: stop-out, exit, gross and net return, the adverse and
  favourable excursions and when they happened.
- OUT_DIR/<set>_checks.parquet: one row a trade still open at a checkpoint, with its
  path parameters from the candles closed by then and what an exit there would give.

Fixed here before any of it was run:
- A candle's slot is its start in 5-minute steps from 09:15 (09:15 = 0, 15:25 = 74).
  "Closed by" a time means every candle that started before it.
- The order rule follows research/ml_live/live.py exactly: entry at the entry candle's
  open (that candle must exist), stop at half the ATR (the features' 14-day `atr_pct`),
  the first candle at or after 15:00 exits at its open; a stop exits at the stop, or the
  open if the candle opens beyond it; a trade with no candle at or after 15:00 is dropped.
- On the stop's candle only the exit counts towards the excursions: the order of the
  high and low within a candle is unknown.
- A trade is open at a checkpoint when no candle before it hit the stop. The checkpoint
  needs its own candle (the exit price is its open); without one there is no row.
- Relative volume: the span's volume over the mean of the same slots' volume in the
  previous 20 sessions that have every one of those candles, with at least 10 of them.
- Breadth: the share of the 197 stocks whose close at the last candle before the entry
  (09:45 for the 09:45 calls, 09:15 for the evening picks) is above their previous
  session's last close.
"""
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl

from igs.intraday.costs import charges, intraday_rates

data, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
NOTIONAL = 100_000
GATE = 0.0015
STOP_ATR = 0.5
SUPER = 10
EXIT_SLOT = 69                                        # 15:00
SLOTS = 75
RATES = intraday_rates()


def slot(hhmm):
    h, m = hhmm
    return (h * 60 + m - 555) // 5


SETS = {
    'm0945': {'pred': 'pred_0945.parquet', 'feat': 'feat_0945.parquet', 'entry': slot((9, 50)),
              'slip': 0.0002,
              'checks': [slot(t) for t in ((10, 5), (10, 20), (10, 35), (11, 5), (12, 5))],
              'inputs': ['gap', 'r30', 'prev_r1', 'r5', 'range30_atr', 'rvol30', 'vwap_dist',
                         'clv30', 'prev_clv', 'prev_last_hour', 'prev_intraday', 'r21',
                         'atr_pct', 'dist20h', 'dist20l', 'log_value20', 'ovn5', 'intra5',
                         'n_gap', 'n_r30', 'n_prev_r1', 'n_r5', 'rel30']},
    'evening': {'pred': 'pred_evening.parquet', 'feat': 'feat_evening.parquet',
                'entry': slot((9, 20)), 'slip': 0.0005,
                'checks': [slot(t) for t in ((9, 35), (9, 50), (10, 5), (10, 35), (11, 35))],
                'inputs': ['prev_r1', 'prev_clv', 'prev_last_hour', 'prev_intraday', 'r5',
                           'r21', 'atr_pct', 'dist20h', 'dist20l', 'log_value20', 'ovn5',
                           'intra5', 'last30', 'rvol_last30', 'close_vwap', 'n_prev_r1',
                           'n_last_hour', 'n_r5', 'n_r21', 'n_last30', 'rel1']},
}
# Scale-like inputs are used as they are; positions in a range (0-1) become 1 - x for a
# short sell; every other input is return-like and is multiplied by the side.
SCALE = {'range30_atr', 'rvol30', 'atr_pct', 'log_value20', 'rvol_last30'}
POSITION = {'clv30', 'prev_clv'}


def load(path):
    """(dates, O, H, L, C, V): one row a session, one column a slot, NaN where missing."""
    rows = json.loads(path.read_text())
    by_day = {}
    for r in rows:
        ts = r[0]
        s = (int(ts[11:13]) * 60 + int(ts[14:16]) - 555) // 5
        if 0 <= s < SLOTS:
            by_day.setdefault(ts[:10], []).append((s, *map(float, r[1:6])))
    dates = sorted(by_day)
    A = np.full((5, len(dates), SLOTS), np.nan)
    for i, d in enumerate(dates):
        for s, o, h, lo, c, v in by_day[d]:
            A[:, i, s] = (o, h, lo, c, v)
    return [dt.date.fromisoformat(d) for d in dates], *A


def last_close(C, i):
    """Session i's last close, or NaN."""
    row = C[i][~np.isnan(C[i])]
    return row[-1] if len(row) else np.nan


def net(side, entry, exit_price, slip):
    """(pnl in rupees, net return) of one trade of about Rs 1 lakh, as live.py."""
    e = entry * (1 + side * slip)
    x = exit_price * (1 - side * slip)
    qty = int(NOTIONAL // e)
    if qty < 1:
        return np.nan, np.nan
    cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                         'buy' if side == 1 else 'sell', RATES))
    pnl = side * (x - e) * qty - cost
    return pnl, pnl / (e * qty)


def rel_volume(V, i, a, b):
    """Volume of slots a..b-1 on session i over its mean in the previous 20 complete ones."""
    now = V[i, a:b]
    if np.isnan(now).any():
        return np.nan
    past = V[max(0, i - 20):i, a:b]
    past = past[~np.isnan(past).any(axis=1)].sum(axis=1)
    if len(past) < 10 or past.mean() <= 0:
        return np.nan
    return now.sum() / past.mean()


# --- The superset of each set's calls ---------------------------------------------------

calls = {}
for name, cfg in SETS.items():
    pred = pl.read_parquet(data / cfg['pred']).select('symbol', 'day', 'gbm')
    parts = []
    for side in (1, -1):
        p = pred.filter(pl.col('gbm') * side > 0).with_columns(
            (pl.col('gbm') * side).rank('ordinal', descending=True).over('day').alias('rank'))
        parts.append(p.filter(pl.col('rank') <= SUPER).with_columns(pl.lit(side).alias('side')))
    sup = pl.concat(parts).with_columns(
        ((pl.col('rank') <= 3) & (pl.col('gbm') * pl.col('side') > GATE)).alias('pick'))
    feat = pl.read_parquet(data / cfg['feat']).select('symbol', 'day', *cfg['inputs'])
    calls[name] = sup.join(feat, on=['symbol', 'day'], how='left').sort('day', 'symbol')
    print(name, calls[name].height, 'superset rows,', calls[name]['pick'].sum(), 'picks',
          flush=True)

# --- The Nifty --------------------------------------------------------------------------

n_dates, nO, nH, nL, nC, _ = load(data / 'candles' / 'NSE_INDEX_Nifty_50.json')
n_at = {d: i for i, d in enumerate(n_dates)}


def nifty_inputs(day, e):
    """The Nifty's gap, return to the last candle before the entry, previous day, last 5."""
    i = n_at.get(day)
    if i is None or i < 6:
        return [np.nan] * 4
    closes = [last_close(nC, k) for k in range(i - 6, i)]     # sessions i-6 .. i-1
    prev = closes[-1]
    return [nO[i, 0] / prev - 1, nC[i, e - 1] / nO[i, 0] - 1, prev / closes[-2] - 1,
            prev / closes[0] - 1]


# --- Each stock: breadth, trades and checkpoints ----------------------------------------

uni = {u['trading_symbol']: u['instrument_key'] for u in
       json.loads((data / 'candles' / 'universe.json').read_text())}
entries = sorted({cfg['entry'] for cfg in SETS.values()})
breadth_rows = []
trades = {n: [] for n in SETS}
checks = {n: [] for n in SETS}
for k, (sym, key) in enumerate(sorted(uni.items())):
    path = data / 'candles' / (key.replace('|', '_') + '.json')
    if not path.exists():
        continue
    dates, O, H, L, C, V = load(path)  # noqa: E741
    at = {d: i for i, d in enumerate(dates)}
    for i in range(1, len(dates)):
        prev = last_close(C, i - 1)
        for e in entries:
            c = C[i, e - 1]
            if not np.isnan(c) and not np.isnan(prev):
                breadth_rows.append((dates[i], e, float(c > prev)))
    for name, cfg in SETS.items():
        e, slip = cfg['entry'], cfg['slip']
        rows = calls[name].filter(pl.col('symbol') == sym)
        for r in rows.iter_rows(named=True):
            i = at.get(r['day'])
            atr = r['atr_pct']
            if i is None or atr is None or np.isnan(O[i, e]):
                continue
            side, entry = r['side'], O[i, e]
            stop = entry * (1 - side * STOP_ATR * atr)
            exit_price = exit_slot = None
            stopped = False
            mae = mfe = 0.0
            mae_slot = mfe_slot = e
            path_mae, path_mfe = {}, {}          # excursions over slots e..s-1, by s
            for s in range(e, SLOTS):
                path_mae[s], path_mfe[s] = mae, mfe
                if np.isnan(O[i, s]):
                    continue
                if s >= EXIT_SLOT:
                    exit_price, exit_slot = O[i, s], s
                    break
                hit = L[i, s] <= stop if side == 1 else H[i, s] >= stop
                if hit:
                    exit_price = min(O[i, s], stop) if side == 1 else max(O[i, s], stop)
                    exit_slot, stopped = s, True
                    worst = side * (exit_price / entry - 1)
                    if worst < mae:
                        mae, mae_slot = worst, s
                    break
                adverse = (L[i, s] if side == 1 else H[i, s]) / entry - 1
                favour = (H[i, s] if side == 1 else L[i, s]) / entry - 1
                if side * adverse < mae:
                    mae, mae_slot = side * adverse, s
                if side * favour > mfe:
                    mfe, mfe_slot = side * favour, s
            if exit_price is None:
                continue
            pnl, net_ret = net(side, entry, exit_price, slip)
            if np.isnan(pnl):
                continue
            prev = last_close(C, i - 1)
            vw_v = V[i, :e]
            base = {'symbol': sym, 'day': r['day'], 'side': side}
            trades[name].append({
                **base, 'rank': r['rank'], 'pred': r['gbm'], 'pick': r['pick'],
                'entry': entry, 'stop': stop, 'exit': exit_price, 'exit_slot': exit_slot,
                'stopped': stopped, 'gross': side * (exit_price / entry - 1),
                'pnl': pnl, 'net': net_ret, 'mae': mae, 'mfe': mfe,
                'mae_atr': mae / atr, 'mfe_atr': mfe / atr,
                'mae_slot': mae_slot, 'mfe_slot': mfe_slot,
                'own_gap': O[i, 0] / prev - 1 if not np.isnan(prev) else np.nan,
                'own_sofar': C[i, e - 1] / O[i, 0] - 1,
                'own_rvol_pre': rel_volume(V, i, 0, e),
                'own_vol_pre': float(np.nansum(vw_v)),
                **dict(zip(['nd_gap', 'nd_sofar', 'nd_prev1', 'nd_r5'],
                           nifty_inputs(r['day'], e), strict=True)),
                **{c: r[c] for c in cfg['inputs']}})
            ni = n_at.get(r['day'])
            for n, c in enumerate(cfg['checks'], 1):
                if exit_slot < c or np.isnan(O[i, c]):
                    continue
                closed = C[i, e:c]
                ok = ~np.isnan(closed)
                if not ok.any():
                    continue
                lastc = closed[ok][-1]
                move = side * (lastc / entry - 1)
                pv = V[i, :c]
                pc = (H[i, :c] + L[i, :c] + C[i, :c]) / 3
                good = ~np.isnan(pv) & ~np.isnan(pc)
                vwap = (pc[good] * pv[good]).sum() / pv[good].sum() if pv[good].sum() > 0 \
                    else np.nan
                hi, lo = np.nanmax(H[i, :c]), np.nanmin(L[i, :c])
                pos = (lastc - lo) / (hi - lo) if hi > lo else 0.5
                n_move = (side * (nC[ni, c - 1] / nO[ni, e] - 1)
                          if ni is not None and not np.isnan(nC[ni, c - 1]) else np.nan)
                o3 = O[i, c - 3]
                epnl, eret = net(side, entry, O[i, c], slip)
                checks[name].append({
                    **base, 'check': n, 'minutes': (c - e) * 5,
                    'move': move, 'move_atr': move / atr,
                    'best_atr': path_mfe[c] / atr, 'worst_atr': path_mae[c] / atr,
                    'last15': side * (lastc / o3 - 1) if not np.isnan(o3) else np.nan,
                    'rvol_since': rel_volume(V, i, e, c),
                    'rvol_15': rel_volume(V, i, c - 3, c),
                    'vwap_gap': side * (lastc / vwap - 1),
                    'range_pos': pos if side == 1 else 1 - pos,
                    'nifty_move': n_move, 'rel_move': move - n_move,
                    'early_pnl': epnl, 'early_net': eret,
                    'rest': side * (exit_price / O[i, c] - 1),
                    'stop_later': stopped})
    if k % 20 == 0:
        print(k, sym, flush=True)

breadth = (pl.DataFrame(breadth_rows, schema=['day', 'entry_slot', 'up'], orient='row')
           .group_by('day', 'entry_slot').agg(pl.col('up').mean().alias('breadth')))

for name, cfg in SETS.items():
    t = pl.DataFrame(trades[name], infer_schema_length=None)
    b = breadth.filter(pl.col('entry_slot') == cfg['entry']).drop('entry_slot')
    t = t.join(b, on='day', how='left')
    # The decision-time parameters, signed for the side.
    signed = []
    for c in cfg['inputs'] + ['own_gap', 'own_sofar', 'nd_gap', 'nd_sofar', 'nd_prev1',
                              'nd_r5', 'breadth']:
        if c in SCALE:
            continue
        if c in POSITION or c == 'breadth':
            signed.append(pl.when(pl.col('side') == 1).then(pl.col(c))
                          .otherwise(1 - pl.col(c)).alias(f's_{c}'))
        else:
            signed.append((pl.col(c) * pl.col('side')).alias(f's_{c}'))
    t = t.with_columns(*signed, (pl.col('pred') * pl.col('side')).alias('conviction'),
                       pl.col('day').dt.weekday().alias('dow'))
    t.write_parquet(out / f'{name}_trades.parquet')
    ch = pl.DataFrame(checks[name], infer_schema_length=None)
    ch.write_parquet(out / f'{name}_checks.parquet')
    p = t.filter('pick')
    print(f"{name}: {t.height} trades ({p.height} picks), stop rate {t['stopped'].mean():.3f}"
          f" (picks {p['stopped'].mean():.3f}); {ch.height} checkpoint rows", flush=True)
