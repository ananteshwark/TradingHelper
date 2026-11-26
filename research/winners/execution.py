"""The winners' habits on the frozen ML intraday calls (winners/PROTOCOL.md): passive limit
entries and stops, nine variants.

Usage: uv run python -I execution.py PRED.parquet FEATURES.parquet DATA_DIR dev|holdout [ENTRY EXIT]
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
only = (sys.argv[5], sys.argv[6]) if len(sys.argv) > 6 else None
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
T = dt.time
rates = intraday_rates()
lo, hi = M.DEV if period == 'dev' else M.HOLDOUT
pred = pl.read_parquet(pred_path).filter(pl.col('day').is_between(lo, hi))
days = sorted(pred['day'].unique())

# The calls, exactly as the model chose them (GBM, k = 3, gate), before costs.
calls = []
for (day,), g in pred.group_by(['day'], maintain_order=True):
    g = g.sort('gbm')
    for side, sel in ((1, g.filter(pl.col('gbm') > M.GATE).tail(3)),
                      (-1, g.filter(pl.col('gbm') < -M.GATE).head(3))):
        calls += [(r['symbol'], day, side) for r in sel.iter_rows(named=True)]
calls = pl.DataFrame(calls, orient='row', schema=[('symbol', pl.Utf8), ('day', pl.Date), ('side', pl.Int64)])
atr = pl.read_parquet(feat_path).select('symbol', 'day', 'atr_pct')
calls = calls.join(atr, on=['symbol', 'day'], how='left')

uni = {u['trading_symbol']: u['instrument_key'] for u in json.loads((data / 'universe.json').read_text())}
bars = {}
for sym in calls['symbol'].unique():
    want = set(calls.filter(pl.col('symbol') == sym)['day'])
    for r in json.loads((data / (uni[sym].replace('|', '_') + '.json')).read_text()):
        t = dt.datetime.fromisoformat(r[0]).astimezone(IST)
        if t.date() in want:
            bars.setdefault((sym, t.date()), []).append((t.time(), *map(float, r[1:5])))
for v in bars.values():
    v.sort()


def run(entry_rule, exit_rule):
    out = []
    for sym, day, side, atr_pct in calls.iter_rows():
        b = bars.get((sym, day), [])
        c40 = next((x[4] for x in b if x[0] == T(9, 40)), None)
        if c40 is None:
            continue
        after = [x for x in b if x[0] >= T(9, 50)]
        entry = k_entry = None
        if entry_rule == 'E0':
            if after and after[0][0] == T(9, 50):
                entry, k_entry, slip_in = after[0][1], 0, M.SLIP
        else:
            off = 0.001 if entry_rule == 'E1' else 0.0025
            limit = c40 * (1 - side * off)
            for k, (t, o, h, l_, c) in enumerate(after):
                if t > T(10, 25):
                    break
                if (side == 1 and l_ <= limit) or (side == -1 and h >= limit):
                    entry = min(o, limit) if side == 1 else max(o, limit)
                    k_entry, slip_in = k, 0.0
                    break
        if entry is None:
            continue
        mult = {'X0': None, 'X1': 0.5, 'X2': 1.0}[exit_rule]
        stop = entry * (1 - side * mult * atr_pct) if mult else None
        exit_price = None
        for t, o, h, l_, c in after[k_entry:]:
            if t >= T(15, 15):
                exit_price = o
                break
            if stop is not None:
                if side == 1 and l_ <= stop:
                    exit_price = min(o, stop)
                    break
                if side == -1 and h >= stop:
                    exit_price = max(o, stop)
                    break
        if exit_price is None:
            continue
        e = entry * (1 + side * slip_in)
        x = exit_price * (1 - side * M.SLIP)
        qty = int(M.NOTIONAL // e)
        if qty < 1:
            continue
        cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                             'buy' if side == 1 else 'sell', rates))
        pnl = side * (x - e) * qty - cost
        out.append({'day': day, 'side': side, 'pnl': pnl, 'net_pct': pnl / (e * qty) * 100})
    t = pl.DataFrame(out)
    daily = pl.DataFrame({'day': days}).join(t.group_by('day').agg(pl.col('pnl').sum()), on='day',
                                             how='left').fill_null(0)['pnl']
    gains, losses = t.filter(pl.col('pnl') > 0)['pnl'].sum(), -t.filter(pl.col('pnl') < 0)['pnl'].sum()
    eq = daily.cum_sum()
    return {'entry': entry_rule, 'exit': exit_rule, 'calls': calls.height, 'trades': t.height,
            'filled_%': round(t.height / calls.height * 100, 1),
            'net_%_trade': round(t['net_pct'].mean(), 3), 'win': round((t['pnl'] > 0).mean(), 3),
            'PF': round(gains / losses, 2), 'daily_Rs': round(daily.mean()),
            't_daily': round(daily.mean() / daily.std() * math.sqrt(len(days)), 2),
            'max_drawdown_Rs': round((eq - eq.cum_max()).min()),
            'worst_trade_%': round(t['net_pct'].min(), 2),
            'shorts_net_%': round(t.filter(pl.col('side') == -1)['net_pct'].mean(), 3),
            'longs_net_%': round(t.filter(pl.col('side') == 1)['net_pct'].mean(), 3)}


combos = [only] if only else [(e, x) for e in ('E0', 'E1', 'E2') for x in ('X0', 'X1', 'X2')]
if only and only != ('E0', 'X0'):
    combos = [('E0', 'X0'), only]
rows = [run(e, x) for e, x in combos]
with pl.Config(tbl_cols=20, tbl_width_chars=250):
    print(pl.DataFrame(rows))
