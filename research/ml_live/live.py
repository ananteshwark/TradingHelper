"""The ML intraday calls with the entry and exit a real order can have (ml_live/PROTOCOL.md).

Usage: uv run python -I live.py PRED.parquet FEATURES.parquet DATA_DIR dev|holdout
"""
import datetime as dt
import json
import math
import sys
from decimal import Decimal
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ml'))
import intraday_model as M      # noqa: E402
from igs.intraday.costs import charges, intraday_rates   # noqa: E402

pred_path, feat_path, data, period = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
T = dt.time
STOP_ATR = 0.5
VARIANTS = {'L0': (T(9, 50), T(15, 15)), 'L1': (T(9, 50), T(15, 0)),
            'L2': (T(9, 55), T(15, 0)), 'L3': (T(10, 0), T(15, 0))}
rates = intraday_rates()
lo, hi = M.DEV if period == 'dev' else M.HOLDOUT
pred = pl.read_parquet(pred_path).filter(pl.col('day').is_between(lo, hi))
days = sorted(pred['day'].unique())

# The calls, exactly as the model chose them (GBM, k = 3, gate), as research/winners.
calls = []
for (day,), g in pred.group_by(['day'], maintain_order=True):
    g = g.sort('gbm')
    for side, sel in ((1, g.filter(pl.col('gbm') > M.GATE).tail(3)),
                      (-1, g.filter(pl.col('gbm') < -M.GATE).head(3))):
        calls += [(r['symbol'], day, side) for r in sel.iter_rows(named=True)]
calls = pl.DataFrame(calls, orient='row',
                     schema=[('symbol', pl.Utf8), ('day', pl.Date), ('side', pl.Int64)])
atr = pl.read_parquet(feat_path).select('symbol', 'day', 'atr_pct')
calls = calls.join(atr, on=['symbol', 'day'], how='left')

uni = {u['trading_symbol']: u['instrument_key']
       for u in json.loads((data / 'universe.json').read_text())}
bars = {}
for sym in calls['symbol'].unique():
    want = set(calls.filter(pl.col('symbol') == sym)['day'])
    for r in json.loads((data / (uni[sym].replace('|', '_') + '.json')).read_text()):
        t = dt.datetime.fromisoformat(r[0]).astimezone(IST)
        if t.date() in want:
            bars.setdefault((sym, t.date()), []).append((t.time(), *map(float, r[1:5])))
for v in bars.values():
    v.sort()


def run(name, enter_at, exit_at):
    out = []
    for sym, day, side, atr_pct in calls.iter_rows():
        b = bars.get((sym, day), [])
        after = [x for x in b if x[0] >= enter_at]
        if not after or after[0][0] != enter_at or atr_pct is None:
            continue
        entry = after[0][1]
        stop = entry * (1 - side * STOP_ATR * atr_pct)
        exit_price = None
        for t, o, h, l_, _c in after:
            if t >= exit_at:
                exit_price = o
                break
            if side == 1 and l_ <= stop:
                exit_price = min(o, stop)
                break
            if side == -1 and h >= stop:
                exit_price = max(o, stop)
                break
        if exit_price is None:
            continue
        e = entry * (1 + side * M.SLIP)
        x = exit_price * (1 - side * M.SLIP)
        qty = int(M.NOTIONAL // e)
        if qty < 1:
            continue
        cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                             'buy' if side == 1 else 'sell', rates))
        pnl = side * (x - e) * qty - cost
        out.append({'day': day, 'side': side, 'pnl': pnl, 'net_pct': pnl / (e * qty) * 100})
    t = pl.DataFrame(out)
    daily = pl.DataFrame({'day': days}).join(t.group_by('day').agg(pl.col('pnl').sum()),
                                             on='day', how='left').fill_null(0)['pnl']
    gains = t.filter(pl.col('pnl') > 0)['pnl'].sum()
    losses = -t.filter(pl.col('pnl') < 0)['pnl'].sum()
    eq = daily.cum_sum()
    return {'variant': name, 'entry': f'{enter_at:%H:%M}', 'exit': f'{exit_at:%H:%M}',
            'calls': calls.height, 'trades': t.height,
            'net_%_trade': round(t['net_pct'].mean(), 3), 'win': round((t['pnl'] > 0).mean(), 3),
            'PF': round(gains / losses, 2), 'daily_Rs': round(daily.mean()),
            't_daily': round(daily.mean() / daily.std() * math.sqrt(len(days)), 2),
            'max_drawdown_Rs': round((eq - eq.cum_max()).min()),
            'worst_trade_%': round(t['net_pct'].min(), 2),
            'shorts_net_%': round(t.filter(pl.col('side') == -1)['net_pct'].mean(), 3),
            'longs_net_%': round(t.filter(pl.col('side') == 1)['net_pct'].mean(), 3)}


print(f'{period}: {lo} to {min(hi, max(days))}, stop {STOP_ATR} x the 14-day ATR')
with pl.Config(tbl_cols=20, tbl_width_chars=250):
    print(pl.DataFrame([run(k, *v) for k, v in VARIANTS.items()]))
