"""Parts 1 and 2 of research/meta_intraday/PROTOCOL.md.

Usage (uv run --with lightgbm --with scikit-learn python -I analyze.py ...):
  analyze.py CALLS describe                 Part 1, entries before 2025
  analyze.py CALLS predict PRED             walk-forward predictions from January 2023
  analyze.py CALLS dev PRED                 the six candidates on 2023-2024, and the choice
  analyze.py CALLS holdout PRED MODEL THR   the chosen candidate on 2025 on, run once
"""
import datetime as dt
import math
import sys

import numpy as np
import polars as pl

FEATURES = ['side', 'minutes', 'volume_jump', 'momentum', 'from_vwap', 'day_move', 'gap',
            'stop_width', 'market', 'against_market', 'close_in_candle', 'candle_size',
            'past_opening_range', 'day_range', 'room_to_extreme', 'turnover', 'price',
            'yesterday', 'five_days', 'market_yesterday', 'weekday']
START, HOLD = dt.date(2023, 1, 1), dt.date(2025, 1, 1)
THRESHOLDS = (0.0, 0.10, 0.25)

calls = pl.read_parquet(sys.argv[1]).sort('day', 'stock')
mode = sys.argv[2]
filled = calls.filter((pl.col('outcome') != 'not_filled') & pl.col('r_net').is_not_null())


def describe():
    dev = filled.filter(pl.col('day') < HOLD)
    n = dev.height
    print(f"Development calls (entries before 2025): {calls.filter(pl.col('day') < HOLD).height:,}"
          f", filled {n:,}")
    for o in ('target', 'stop', 'time_exit'):
        sub = dev.filter(pl.col('outcome') == o)
        print(f"  {o:9s} {sub.height / n:6.1%}  average R after charges "
              f"{sub['r_net'].mean():+.3f}")
    print(f"  all       win rate {(dev['r_net'] > 0).mean():.1%}, average R after charges "
          f"{dev['r_net'].mean():+.3f}, rupees a call {dev['net_rupees'].mean():+.0f}\n")
    t, s = dev.filter(pl.col('outcome') == 'target'), dev.filter(pl.col('outcome') == 'stop')
    rows = []
    for f in FEATURES:
        a, b = t[f].drop_nulls().to_numpy(), s[f].drop_nulls().to_numpy()
        pooled = math.sqrt((a.var(ddof=1) * (len(a) - 1) + b.var(ddof=1) * (len(b) - 1))
                           / (len(a) + len(b) - 2))
        rows.append((f, float(np.median(a)), float(np.median(b)),
                     (a.mean() - b.mean()) / pooled if pooled else 0.0))
    print('Target first against stop first: medians and the standardized difference of means')
    print(f"{'input':20s} {'target median':>14s} {'stop median':>12s} {'difference':>11s}")
    for f, ma, mb, d in sorted(rows, key=lambda r: -abs(r[3])):
        print(f'{f:20s} {ma:14.3f} {mb:12.3f} {d:+11.3f}')
    print('\nWin rate and average R after charges by quintile of each input (1 = lowest)')
    for f, *_ in sorted(rows, key=lambda r: -abs(r[3])):
        d = dev.filter(pl.col(f).is_not_null())
        q = d.with_columns(((pl.col(f).rank('ordinal') - 1) * 5 // d.height + 1).alias('q'))
        g = (q.group_by('q').agg(pl.len().alias('n'), (pl.col('r_net') > 0).mean().alias('win'),
                                 pl.col('r_net').mean().alias('r'),
                                 pl.col(f).min().alias('lo'), pl.col(f).max().alias('hi'))
             .sort('q'))
        print(f'{f}: ' + '  '.join(f"q{r['q']} [{r['lo']:.2f},{r['hi']:.2f}] {r['win']:.0%} "
                                   f"{r['r']:+.3f}" for r in g.iter_rows(named=True)))


def quarters(start, end):
    q = start
    while q <= end:
        nxt = dt.date(q.year + (q.month + 2) // 12, (q.month + 2) % 12 + 1, 1)
        yield q, nxt
        q = nxt


def fit_predict(train, test):
    import lightgbm as lgb
    from sklearn.linear_model import Ridge
    X, y = train.select(FEATURES).to_numpy().astype(float), train['r_net'].to_numpy()
    Xt = test.select(FEATURES).to_numpy().astype(float)
    mu, sd = np.nanmean(X, 0), np.nanstd(X, 0)
    sd[sd == 0] = 1
    z = lambda a: np.clip(np.nan_to_num((a - mu) / sd), -5, 5)   # noqa: E731
    ridge = Ridge(alpha=1.0).fit(z(X), y)
    gbm = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.03, num_leaves=15,
                            min_child_samples=200, subsample=0.7, subsample_freq=1,
                            colsample_bytree=0.7, reg_lambda=5, random_state=7, verbose=-1)
    gbm.fit(X, y)
    return ridge.predict(z(Xt)), gbm.predict(Xt)


def predict(out):
    parts = []
    for q, nxt in quarters(START, calls['day'].max()):
        train = filled.filter(pl.col('day') < q)
        test = calls.filter((pl.col('day') >= q) & (pl.col('day') < nxt))
        if test.is_empty():
            continue
        p_ridge, p_gbm = fit_predict(train, test)
        parts.append(test.with_columns(pl.Series('ridge', p_ridge), pl.Series('gbm', p_gbm)))
        print(q, 'train', train.height, 'test', test.height, flush=True)
    pl.concat(parts).write_parquet(out)


def stats(taken):
    """Each day with a taken, filled call once, at the sum of its rupees after charges."""
    t = taken.filter((pl.col('outcome') != 'not_filled') & pl.col('net_rupees').is_not_null())
    if t.height < 2:
        return None
    daily = t.group_by('day').agg(pl.col('net_rupees').sum().alias('rs')).sort('day')
    x = daily['rs'].to_numpy()
    sd = x.std(ddof=1)
    years = (daily.with_columns(pl.col('day').dt.year().alias('y')).group_by('y')
             .agg(pl.col('rs').mean()).sort('y'))
    return {'calls': t.height, 'days': len(x), 'per_day': float(x.mean()),
            't': float(x.mean() / sd * math.sqrt(len(x))) if sd else 0.0,
            'win': float((t['r_net'] > 0).mean()), 'r': float(t['r_net'].mean()),
            'years': dict(zip(years['y'].to_list(), years['rs'].to_list(), strict=True))}


def show(name, s):
    yrs = '  '.join(f'{y}: {v:+,.0f}' for y, v in s['years'].items())
    print(f"{name:12s} calls {s['calls']:6,d} on {s['days']:4d} days  win {s['win']:5.1%}  "
          f"R after charges {s['r']:+.3f}  rupees a day {s['per_day']:+8,.0f}  t {s['t']:5.2f}"
          f"\n             {yrs}")


def dev(pred):
    p = pl.read_parquet(pred).filter(pl.col('day') < HOLD)
    print('Development 2023-2024 (walk-forward predictions)')
    show('all calls', stats(p))
    best = None
    for model in ('ridge', 'gbm'):
        for thr in THRESHOLDS:
            s = stats(p.filter(pl.col(model) >= thr))
            if s is None:
                print(f'{model}>={thr}: too few calls')
                continue
            show(f'{model}>={thr}', s)
            if s['per_day'] > 0 and s['calls'] >= 100 and (best is None or s['t'] > best[2]):
                best = (model, thr, s['t'])
    print('\nChosen:', 'none' if best is None else f'{best[0]} >= {best[1]}')


def holdout(pred, model, thr):
    p = pl.read_parquet(pred).filter(pl.col('day') >= HOLD)
    print(f"Holdout {HOLD} to {p['day'].max()}, run once: {model} >= {thr}")
    show('all calls', stats(p))
    s = stats(p.filter(pl.col(model) >= thr))
    show(f'{model}>={thr}', s)
    ok = (s['per_day'] > 0 and s['t'] >= 2 and s['calls'] >= 150
          and all(s['years'].get(y, 0) > 0 for y in (2025, 2026)))
    print('PASSED' if ok else 'NOT PASSED')


if mode == 'describe':
    describe()
elif mode == 'predict':
    predict(sys.argv[3])
elif mode == 'dev':
    dev(sys.argv[3])
elif mode == 'holdout':
    holdout(sys.argv[3], sys.argv[4], float(sys.argv[5]))
