"""Reversed calls (PROTOCOL.md): each intraday model's chosen picks, as chosen and reversed,
after slippage and Upstox charges on Rs 1 lakh a trade.

Usage: uv run python -I reverse.py PRED_0945 PRED_EVENING
PRED_0945: research/ml/intraday_model.py predict's output; PRED_EVENING:
research/evening/model.py predict's output.
"""
import datetime as dt
import math
import sys
from decimal import Decimal
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ml'))
import intraday_model as B  # noqa: E402

from igs.intraday.costs import charges, intraday_rates  # noqa: E402

DEV = (dt.date(2023, 1, 1), dt.date(2024, 12, 31))
HOLDOUT = (dt.date(2025, 1, 1), dt.date(2026, 10, 8))
AFTER = (dt.date(2026, 10, 9), dt.date(2026, 10, 24))
MODELS = {'09:45 calls': (0.0002, 'gbm'), 'Evening picks': (0.0005, 'gbm')}
K, GATE = 3, True


def trades(pred, model, slip, flip):
    """B.trades' picks; with `flip`, each one taken on the other side."""
    rates = intraday_rates()
    rows = []
    for (day,), g in pred.group_by(['day'], maintain_order=True):
        g = g.sort(model)
        longs = g.filter(pl.col(model) > B.GATE).tail(K)
        shorts = g.filter(pl.col(model) < -B.GATE).head(K)
        for called, sel in ((1, longs), (-1, shorts)):
            side = -called if flip else called
            for r in sel.iter_rows(named=True):
                e = r['entry'] * (1 + side * slip)
                x = r['exit'] * (1 - side * slip)
                qty = int(B.NOTIONAL // e)
                if qty < 1:
                    continue
                cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                                     'buy' if side == 1 else 'sell', rates))
                pnl = side * (x - e) * qty - cost
                rows.append({'day': day, 'called': called, 'pnl': pnl,
                             'gross_pct': side * (r['exit'] / r['entry'] - 1) * 100,
                             'net_pct': pnl / (e * qty) * 100})
    return pl.DataFrame(rows, schema={'day': pl.Date, 'called': pl.Int64, 'pnl': pl.Float64,
                                      'gross_pct': pl.Float64, 'net_pct': pl.Float64})


def stats(t, days):
    s = B.stats(t, days)
    return {k: s[k] for k in ('trades', 'gross_%', 'net_%', 'PF', 'daily_Rs', 't_daily')}


def table(rows):
    with pl.Config(tbl_cols=20, tbl_width_chars=200, tbl_rows=100, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True):
        print(pl.DataFrame(rows))


for (name, (slip, model)), path in zip(MODELS.items(), sys.argv[1:3], strict=True):
    pred = pl.read_parquet(path)
    print(f'\n=== {name}: {model}, k {K}, gate {GATE}, slippage {slip:.2%} a side ===')
    periods = {'development 2023-2024': DEV, 'holdout 2025 - 8 Oct 2026': HOLDOUT,
               '9-24 Oct 2026 (too few to judge)': AFTER}
    for y in (2023, 2024, 2025, 2026):
        periods[str(y)] = (dt.date(y, 1, 1), min(dt.date(y, 12, 31), HOLDOUT[1]))
    rows = []
    for label, (lo, hi) in periods.items():
        p = pred.filter(pl.col('day').is_between(lo, hi))
        days = sorted(p['day'].unique())
        if not days:
            continue
        for flip, how in ((False, 'as called'), (True, 'reversed')):
            t = trades(p, model, slip, flip)
            row = {'period': label, 'calls': how, 'days': len(days), **stats(t, days)}
            for called, side in ((1, 'buys'), (-1, 'sells')):
                s = t.filter(pl.col('called') == called)
                row[f'called {side} net %'] = round(s['net_pct'].mean(), 3) if s.height else None
            rows.append(row)
    table(rows)
    # The pass rule (PROTOCOL.md) on the reversed calls.
    dev, hold = (next(r for r in rows if r['period'].startswith(w) and r['calls'] == 'reversed')
                 for w in ('development', 'holdout'))
    passed = (dev['daily_Rs'] > 0 and hold['daily_Rs'] > 0 and (hold['t_daily'] or 0) >= 2.5
              and (hold['PF'] or 0) >= 1.1)
    print(f'Reversed: development Rs {dev["daily_Rs"]:+,.0f} a day; holdout Rs '
          f'{hold["daily_Rs"]:+,.0f} a day, t {hold["t_daily"]}, profit factor {hold["PF"]}: '
          + ('PASSES' if passed else 'does not pass'))
    print('net % for "called buys/sells" is the trade as taken: for the reversed calls, the '
          'called buys were short sells and the called sells were buys.')
    assert not math.isnan(hold['daily_Rs'])
