"""Spike recovery (PROTOCOL.md): the LightGBM models, the gate and the reports.

Usage:
  uv run python -I model.py validate EVENTS_DIR BHAV_PARQUET
  uv run python -I model.py test EVENTS_DIR BHAV_PARQUET GATE
EVENTS_DIR holds events.parquet (events.py).
"""
import datetime as dt
import math
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

TRAIN_V, VALID = (dt.date(2022, 1, 1), dt.date(2023, 12, 31)), (dt.date(2024, 1, 1),
                                                                dt.date(2024, 12, 31))
TRAIN_T, TEST = (dt.date(2022, 1, 1), dt.date(2024, 12, 31)), (dt.date(2025, 1, 1),
                                                               dt.date(2026, 10, 8))
TS = (1, 2, 3, 4, 5)                      # targets, %
GATES = (0.0, 0.25, 0.5, 0.75, 1.0)       # predicted net % a trade
SLIP, SLIPS = 15, (5, 30)                 # slippage tags, in hundredths of a percent
PARAMS = {'objective': 'regression', 'learning_rate': 0.03, 'num_leaves': 15,
          'min_data_in_leaf': 200, 'feature_fraction': 0.8, 'bagging_fraction': 0.8,
          'bagging_freq': 1, 'lambda_l2': 1.0, 'seed': 7, 'verbose': -1, 'num_threads': 4,
          'deterministic': True}
ROUNDS = 400


def between(ev, period):
    return ev.filter(pl.col('day').is_between(*period))


def distinct(ev):
    """One decision point a candle: several thresholds reached on one candle are the same
    decision (the lowest X is kept)."""
    return ev.sort('day', 'minute', 'isin', 'x').unique(subset=['isin', 'day', 'i'],
                                                         keep='first', maintain_order=True)


def fit(train, features, label):
    t = train.filter(pl.col(label).is_not_nan() & pl.col(label).is_not_null())
    data = lgb.Dataset(t.select(features).to_numpy(), t[label].to_numpy(),
                       feature_name=features, free_raw_data=False)
    return lgb.train(PARAMS, data, num_boost_round=ROUNDS)


def stats(t, sessions, pnl='pnl'):
    daily = dict(t.group_by('day').agg(pl.col(pnl).sum()).iter_rows()) if t.height else {}
    xs = np.array([daily.get(d, 0.0) for d in sessions])
    mean, sd = xs.mean(), xs.std(ddof=1)
    if not t.height:
        return {'trades': 0, 'daily_Rs': 0.0, 't_daily': None}
    net = t['net'] if 'net' in t.columns else None
    wins = t.filter(pl.col(pnl) > 0)[pnl].sum()
    losses = -t.filter(pl.col(pnl) < 0)[pnl].sum()
    return {'trades': t.height,
            'net_%': round(net.mean(), 3) if net is not None else None,
            'Rs_trade': round(t[pnl].mean()), 'win_%': round(100 * (t[pnl] > 0).mean(), 1),
            'PF': round(wins / losses, 2) if losses else None,
            'daily_Rs': round(mean, 1),
            't_daily': round(mean / (sd / math.sqrt(len(xs))), 2) if sd else None}


def table(rows, title):
    print(f'\n--- {title} ---')
    with pl.Config(tbl_cols=30, tbl_width_chars=260, tbl_rows=200, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True, fmt_str_lengths=40):
        print(pl.DataFrame(rows) if isinstance(rows, list) else rows)


def describe(ev, label):
    rows = []
    for x in sorted(ev['x'].unique()):
        s = ev.filter(pl.col('x') == x)
        r = s['y_recovery']
        rows.append({'move': f'{x:.0%}', 'points': s.height,
                     'mean recovery %': round(r.mean(), 2),
                     'median recovery %': round(r.median(), 2),
                     **{f'reached {k}%': f'{100 * (r >= k).mean():.0f}%' for k in TS},
                     'median minutes to it': round(s['y_minutes'].median())})
    table(rows, f'{label}: the maximum recovery against the move before 15:15, before costs')


def fixed_rules(ev, sessions, label):
    rows = []
    for x in sorted(ev['x'].unique()):
        s = ev.filter(pl.col('x') == x)
        for k in TS:
            t = s.select('day', pl.col(f'pnl_{k}_{SLIP}').alias('pnl'),
                         pl.col(f'net_{k}_{SLIP}').alias('net')).drop_nulls().drop_nans()
            rows.append({'move': f'{x:.0%}', 'target': f'{k}%', **stats(t, sessions)})
    table(rows, f'{label}: fixed rules, every point at that move, fade at once, no stop')


def trades(ev, preds, gate, slip=SLIP):
    """The rule: each stock-day's first decision point whose best prediction exceeds the
    gate, with the target that gave it."""
    best = preds.max(axis=1)
    pick = preds.argmax(axis=1)
    e = ev.with_columns(best=pl.Series(best), t_star=pl.Series(np.array(TS)[pick]))
    e = (e.filter(pl.col('best') > gate).sort('isin', 'day', 'minute', 'x')
         .group_by('isin', 'day', maintain_order=True).first())
    pnl = [r[f'pnl_{t}_{slip}'] for r, t in zip(e.iter_rows(named=True), e['t_star'],
                                                 strict=True)]
    net = [r[f'net_{t}_{slip}'] for r, t in zip(e.iter_rows(named=True), e['t_star'],
                                                 strict=True)]
    why = [r[f'why_{t}'] for r, t in zip(e.iter_rows(named=True), e['t_star'], strict=True)]
    locked = [r[f'locked_{t}'] for r, t in zip(e.iter_rows(named=True), e['t_star'],
                                               strict=True)]
    return (e.with_columns(pnl=pl.Series(pnl), net=pl.Series(net), why=pl.Series(why),
                           locked=pl.Series(locked))
            .filter(pl.col('pnl').is_not_nan()))


def accuracy(model, ev, features, label):
    p = model.predict(ev.select(features).to_numpy())
    y = ev['y_recovery'].to_numpy()
    r = float(np.corrcoef(p, y)[0, 1])
    d = ev.with_columns(pred=pl.Series(p)).with_columns(
        decile=(pl.col('pred').rank('ordinal') * 10 / pl.len()).ceil().cast(pl.Int8))
    rows = (d.group_by('decile').agg(pl.len().alias('points'),
                                     pl.col('pred').mean().round(2).alias('predicted %'),
                                     pl.col('y_recovery').mean().round(2).alias('actual %'),
                                     (pl.col('y_recovery') >= 3).mean().round(3)
                                     .alias('reached 3%'))
            .sort('decile'))
    table(rows, f'{label}: the recovery model, by decile of prediction (correlation {r:.3f})')


def importance(model, features, title):
    gain = model.feature_importance('gain')
    rows = sorted(zip(features, gain, strict=True), key=lambda kv: -kv[1])
    total = gain.sum()
    table([{'feature': f, 'gain %': round(100 * g / total, 1)} for f, g in rows], title)


def main():
    mode, events, bhav = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    ev = pl.read_parquet(events / 'events.parquet')
    features = [c for c in ev.columns if c.startswith('f_')]
    train_p, eval_p = (TRAIN_V, VALID) if mode == 'validate' else (TRAIN_T, TEST)
    days = pl.read_parquet(bhav, columns=['day'])['day'].unique()
    sessions = sorted(d for d in days if eval_p[0] <= d <= eval_p[1])
    name = 'validation 2024' if mode == 'validate' else 'test 2025 - 8 Oct 2026'
    described = (TRAIN_T, '2022-2024') if mode == 'validate' else (TEST, '2025 - 8 Oct 2026')
    describe(between(ev, described[0]), described[1])
    train, evaluate = distinct(between(ev, train_p)), distinct(between(ev, eval_p))
    print(f'\n{name}: {len(sessions)} sessions; trained on {train.height} decision points, '
          f'evaluated on {evaluate.height}; {len(features)} features')
    fixed_rules(between(ev, eval_p), sessions, name)
    models = {k: fit(train, features, f'net_{k}_{SLIP}') for k in TS}
    recovery = fit(train, features, 'y_recovery')
    accuracy(recovery, evaluate, features, name)
    importance(recovery, features, 'what the recovery model used (gain)')
    importance(models[3], features, 'what the 3%-target model used (gain)')
    preds = np.column_stack([models[k].predict(evaluate.select(features).to_numpy())
                             for k in TS])
    if mode == 'validate':
        rows = []
        for g in GATES:
            t = trades(evaluate, preds, g)
            rows.append({'gate %': g, **stats(t, sessions)})
        table(rows, f'{name}: the rule at each gate, 0.15% slippage')
        best = max(rows, key=lambda r: r['t_daily'] if r['t_daily'] is not None else -1e9)
        print(f"\nCandidate (highest validation t): gate {best['gate %']}%: t {best['t_daily']}; "
              + ('meets validation t >= 2.5' if (best['t_daily'] or 0) >= 2.5
                 else 'validation t below 2.5: does not pass'))
        gate = best['gate %']
    else:
        gate = float(sys.argv[4])
    t = trades(evaluate, preds, gate)
    s = stats(t, sessions)
    table([{'gate %': gate, **s}], f'{name}: the chosen gate')
    if mode == 'test':
        passed = s['daily_Rs'] > 0 and (s['t_daily'] or 0) >= 2.0 and s['trades'] >= 100
        print('Test: ' + ('PASSES' if passed else 'does not pass')
              + f" (mean Rs {s['daily_Rs']:+,.1f} a day, t {s['t_daily']}, {s['trades']} trades)")
    rows = [{'group': f'target {k}%', **stats(t.filter(pl.col('t_star') == k), sessions)}
            for k in TS]
    rows += [{'group': f'move {x:.0%}', **stats(t.filter(pl.col('x') == x), sessions)}
             for x in sorted(t['x'].unique())]
    rows += [{'group': name_, **stats(sub, sessions)} for name_, sub in (
        ('after a rise (short sales)', t.filter(pl.col('side') == 1)),
        ('after a fall (buys)', t.filter(pl.col('side') == -1)),
        ('Rs 2-10 crore a day', t.filter(pl.col('med20') < 1e8)),
        ('Rs 10 crore or more', t.filter(pl.col('med20') >= 1e8)),
        ("today's Nifty 200", t.filter(pl.col('nifty200'))))]
    table(rows, f'{name}, gate {gate}%: breakdowns')
    lk = t.filter(pl.col('locked'))
    print(f'exit candle without a price range (a lock): {lk.height} trades, '
          f'net {lk["net"].mean() if lk.height else 0:+.2f}% each')
    busy = (t.group_by('day').agg(pl.len().alias('trades'), pl.col('pnl').sum().round(0))
            .sort('trades', descending=True).head(8))
    table(busy, f'{name}, gate {gate}%: the busiest days')
    for slip in SLIPS:
        print(f'at {slip / 100:.2f}% slippage: ', stats(trades(evaluate, preds, gate, slip),
                                                        sessions))


if __name__ == '__main__':
    main()
