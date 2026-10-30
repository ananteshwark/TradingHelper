"""Study B (PROTOCOL.md): walk-forward predictions and the eight trading-rule candidates.

  predict FEATURES.parquet PRED.parquet    quarterly expanding-window ridge and LightGBM
  evaluate PRED.parquet dev                all eight candidates, 2023-2024
  evaluate PRED.parquet holdout MODEL K GATE   the chosen candidate only, 2025 onward

Implementation details fixed before any result: the ridge model's features are standardised
on its training rows and clipped at +/-5 standard deviations; the day of the week is one-hot.
"""
import datetime as dt
import math
import sys
from decimal import Decimal

import numpy as np
import polars as pl

FEATURES = ['gap', 'r30', 'prev_r1', 'r5', 'range30_atr', 'rvol30', 'vwap_dist', 'clv30',
            'prev_clv', 'prev_last_hour', 'prev_intraday', 'r21', 'atr_pct', 'dist20h',
            'dist20l', 'log_value20', 'ovn5', 'intra5', 'n_gap', 'n_r30', 'n_prev_r1', 'n_r5',
            'rel30'] + [f'dow_{i}' for i in range(5)]
MIN_VALUE = 50e7
SLIP = 0.0002
NOTIONAL = 100000
GATE = 0.0015
DEV = (dt.date(2023, 1, 1), dt.date(2024, 12, 31))
HOLDOUT = (dt.date(2025, 1, 1), dt.date(2026, 12, 31))


def prepare(path):
    df = pl.read_parquet(path).filter(pl.col('value20') >= MIN_VALUE)
    df = df.with_columns([(pl.col('dow') == i).cast(pl.Float64).alias(f'dow_{i}') for i in range(5)])
    return df.with_columns(pl.col('target').clip(-0.05, 0.05).alias('y'))


def quarters(first, last):
    q = dt.date(first.year, 3 * ((first.month - 1) // 3) + 1, 1)
    while q <= last:
        nxt = dt.date(q.year + (q.month + 3 > 12), (q.month + 2) % 12 + 1, 1)
        yield q, nxt
        q = nxt


def fit_predict(train, test):
    import lightgbm as lgb
    from sklearn.linear_model import Ridge
    X, y = train.select(FEATURES).to_numpy(), train['y'].to_numpy()
    Xt = test.select(FEATURES).to_numpy()
    mu, sd = np.nanmean(X, 0), np.nanstd(X, 0)
    sd[sd == 0] = 1
    z = lambda a: np.clip(np.nan_to_num((a - mu) / sd), -5, 5)
    ridge = Ridge(alpha=1.0).fit(z(X), y)
    gbm = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.03, num_leaves=15,
                            min_child_samples=500, subsample=0.7, subsample_freq=1,
                            colsample_bytree=0.7, reg_lambda=5, random_state=7, verbose=-1)
    gbm.fit(X, y)
    return ridge.predict(z(Xt)), gbm.predict(Xt)


def predict(src, out):
    df = prepare(src)
    parts = []
    for q, nxt in quarters(DEV[0], df['day'].max()):
        train = df.filter(pl.col('day') < q)
        test = df.filter((pl.col('day') >= q) & (pl.col('day') < nxt))
        if test.is_empty():
            continue
        p_ridge, p_gbm = fit_predict(train, test)
        parts.append(test.select('symbol', 'day', 'entry', 'exit', 'target')
                     .with_columns(pl.Series('ridge', p_ridge), pl.Series('gbm', p_gbm)))
        print(q, 'train', train.height, 'test', test.height, flush=True)
    pl.concat(parts).write_parquet(out)


def trades(pred, model, k, gate):
    from igs.intraday.costs import charges, intraday_rates
    rates = intraday_rates()
    thr = GATE if gate else 0.0
    picks = []
    for (day,), g in pred.group_by(['day'], maintain_order=True):
        g = g.sort(model)
        longs = g.filter(pl.col(model) > thr).tail(k)
        shorts = g.filter(pl.col(model) < -thr).head(k)
        for side, sel in ((1, longs), (-1, shorts)):
            for r in sel.iter_rows(named=True):
                e = r['entry'] * (1 + side * SLIP)
                x = r['exit'] * (1 - side * SLIP)
                qty = int(NOTIONAL // e)
                if qty < 1:
                    continue                 # priced above the notional, as in s5.py
                cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                                     'buy' if side == 1 else 'sell', rates))
                pnl = side * (x - e) * qty - cost
                picks.append({'day': day, 'symbol': r['symbol'], 'side': side, 'pnl': pnl,
                              'gross_pct': side * (r['exit'] / r['entry'] - 1) * 100,
                              'net_pct': pnl / (e * qty) * 100})
    return pl.DataFrame(picks, schema={'day': pl.Date, 'symbol': pl.Utf8, 'side': pl.Int64,
                                       'pnl': pl.Float64, 'gross_pct': pl.Float64,
                                       'net_pct': pl.Float64})


def stats(t, days):
    daily = pl.DataFrame({'day': days}).join(t.group_by('day').agg(pl.col('pnl').sum()),
                                             on='day', how='left').fill_null(0)['pnl']
    n = t.height
    gains = t.filter(pl.col('pnl') > 0)['pnl'].sum()
    losses = -t.filter(pl.col('pnl') < 0)['pnl'].sum()
    sd = daily.std()
    return {'trades': n, 'trades_per_day': round(n / len(days), 2),
            'gross_%': round(t['gross_pct'].mean(), 3) if n else None,
            'net_%': round(t['net_pct'].mean(), 3) if n else None,
            'win': round((t['pnl'] > 0).mean(), 3) if n else None,
            'PF': round(gains / losses, 2) if losses else None,
            'daily_Rs': round(daily.mean(), 0),
            't_daily': round(daily.mean() / (sd / math.sqrt(len(days))), 2) if sd else None}


def evaluate(path, period, only=None):
    lo, hi = DEV if period == 'dev' else HOLDOUT
    pred = pl.read_parquet(path).filter(pl.col('day').is_between(lo, hi))
    days = sorted(pred['day'].unique())
    combos = [only] if only else [(m, k, g) for m in ('ridge', 'gbm') for k in (3, 10)
                                  for g in (False, True)]
    rows = []
    for model, k, gate in combos:
        t = trades(pred, model, k, gate)
        row = {'model': model, 'k': k, 'gate': gate, **stats(t, days)}
        for y in sorted({d.year for d in days}):
            yd = [d for d in days if d.year == y]
            ys = stats(t.filter(pl.col('day').dt.year() == y), yd)
            row[f'{y}_daily_Rs'] = ys['daily_Rs']
        for side, name in ((1, 'long'), (-1, 'short')):
            s = t.filter(pl.col('side') == side)
            row[f'{name}_net_%'] = round(s['net_pct'].mean(), 3) if s.height else None
        rows.append(row)
    with pl.Config(tbl_cols=30, tbl_width_chars=250):
        print(pl.DataFrame(rows))
    return rows


if __name__ == '__main__':
    if sys.argv[1] == 'predict':
        predict(sys.argv[2], sys.argv[3])
    elif sys.argv[3] == 'dev':
        evaluate(sys.argv[2], 'dev')
    else:
        evaluate(sys.argv[2], 'holdout',
                 (sys.argv[4], int(sys.argv[5]), sys.argv[6] == 'gate'))
