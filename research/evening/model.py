"""Study D (PROTOCOL.md): walk-forward ridge and five-seed LightGBM, and the candidates.

  predict FEATURES.parquet PRED.parquet
  evaluate PRED.parquet dev
  evaluate PRED.parquet holdout MODEL K GATE|nogate

Trades and statistics are Study B's (research/ml/intraday_model.py) with 0.05% slippage.
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ml'))
import intraday_model as B  # noqa: E402

FEATURES = ['prev_r1', 'prev_clv', 'prev_last_hour', 'prev_intraday', 'r5', 'r21', 'atr_pct',
            'dist20h', 'dist20l', 'log_value20', 'ovn5', 'intra5', 'last30', 'rvol_last30',
            'close_vwap', 'n_prev_r1', 'n_last_hour', 'n_r5', 'n_r21', 'n_last30',
            'rel1'] + [f'dow_{i}' for i in range(5)]
SEEDS = (1, 2, 3, 4, 5)
SLIP = 0.0005
DEV = B.DEV
HOLDOUT = (dt.date(2025, 1, 1), dt.date(2026, 10, 8))
AFTER = (dt.date(2026, 10, 9), dt.date(2026, 10, 24))


def prepare(path):
    df = pl.read_parquet(path).filter(pl.col('value20') >= B.MIN_VALUE)
    df = df.with_columns([(pl.col('dow') == i).cast(pl.Float64).alias(f'dow_{i}')
                          for i in range(5)])
    return df.with_columns(pl.col('target').clip(-0.05, 0.05).alias('y'))


def fit_predict(train, test):
    import lightgbm as lgb
    from sklearn.linear_model import Ridge
    X, y = train.select(FEATURES).to_numpy(), train['y'].to_numpy()
    Xt = test.select(FEATURES).to_numpy()
    mu, sd = np.nanmean(X, 0), np.nanstd(X, 0)
    sd[sd == 0] = 1
    z = lambda a: np.clip(np.nan_to_num((a - mu) / sd), -5, 5)  # noqa: E731
    ridge = Ridge(alpha=1.0).fit(z(X), y).predict(z(Xt))
    seeds = []
    for s in SEEDS:
        gbm = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.03, num_leaves=15,
                                min_child_samples=500, subsample=0.7, subsample_freq=1,
                                colsample_bytree=0.7, reg_lambda=5, random_state=s,
                                verbose=-1)
        seeds.append(gbm.fit(X, y).predict(Xt))
    return ridge, seeds


def predict(src, out):
    df = prepare(src)
    parts = []
    for q, nxt in B.quarters(DEV[0], df['day'].max()):
        train = df.filter(pl.col('day') < q)
        test = df.filter((pl.col('day') >= q) & (pl.col('day') < nxt))
        if test.is_empty():
            continue
        ridge, seeds = fit_predict(train, test)
        parts.append(test.select('symbol', 'day', 'entry', 'exit', 'target').with_columns(
            pl.Series('ridge', ridge), pl.Series('gbm', np.mean(seeds, 0)),
            *[pl.Series(f'gbm_s{s}', p) for s, p in zip(SEEDS, seeds, strict=True)]))
        print(q, 'train', train.height, 'test', test.height, flush=True)
    pl.concat(parts).write_parquet(out)


def rank_ic(pred, col):
    ics = [g.select(pl.corr(col, 'target', method='spearman')).item()
           for _, g in pred.group_by('day') if g.height >= 10]
    ics = [x for x in ics if x is not None and not math.isnan(x)]
    m, sd = float(np.mean(ics)), float(np.std(ics, ddof=1))
    return round(m, 4), round(m / (sd / math.sqrt(len(ics))), 1)


def row_for(pred, model, k, gate, days):
    t = B.trades(pred, model, k, gate)
    row = {'model': model, 'k': k, 'gate': gate, **B.stats(t, days)}
    for y in sorted({d.year for d in days}):
        yd = [d for d in days if d.year == y]
        row[f'{y}_daily_Rs'] = B.stats(t.filter(pl.col('day').dt.year() == y), yd)['daily_Rs']
    for side, name in ((1, 'long'), (-1, 'short')):
        s = t.filter(pl.col('side') == side)
        row[f'{name}_net_%'] = round(s['net_pct'].mean(), 3) if s.height else None
    return row


def show(rows):
    with pl.Config(tbl_cols=30, tbl_width_chars=250):
        print(pl.DataFrame(rows))


def evaluate(path, period, only=None):
    B.SLIP = SLIP
    lo, hi = DEV if period == 'dev' else HOLDOUT
    pred = pl.read_parquet(path).filter(pl.col('day').is_between(lo, hi))
    days = sorted(pred['day'].unique())
    print(period, lo, hi, len(days), 'days; slippage', SLIP)
    for col in ('ridge', 'gbm'):
        print('rank IC', col, rank_ic(pred, col))
    if period == 'dev':
        show([row_for(pred, m, k, g, days) for m in ('ridge', 'gbm') for k in (3, 10)
              for g in (False, True)])
        return
    model, k, gate = only
    chosen = row_for(pred, model, k, gate, days)
    show([chosen])
    passed = ((chosen['t_daily'] or 0) >= 2 and chosen['daily_Rs'] > 0
              and (chosen['PF'] or 0) >= 1.1 and chosen['trades'] >= 100
              and chosen.get('2025_daily_Rs', 0) > 0 and chosen.get('2026_daily_Rs', 0) > 0)
    print('PASS' if passed else 'FAIL', '(Study B bar at 0.05% slippage)')
    B.SLIP = 0.0002
    print('at 0.02% slippage (reported):')
    show([row_for(pred, model, k, gate, days)])
    B.SLIP = SLIP
    if model == 'gbm':
        print('seeds:', [row_for(pred, f'gbm_s{s}', k, gate, days)['daily_Rs'] for s in SEEDS])
    after = pl.read_parquet(path).filter(pl.col('day').is_between(*AFTER))
    if not after.is_empty():
        ad = sorted(after['day'].unique())
        print('after the holdout (too few to judge):', len(ad), 'days')
        show([row_for(after, model, k, gate, ad)])


if __name__ == '__main__':
    if sys.argv[1] == 'predict':
        predict(sys.argv[2], sys.argv[3])
    elif sys.argv[3] == 'dev':
        evaluate(sys.argv[2], 'dev')
    else:
        evaluate(sys.argv[2], 'holdout', (sys.argv[4], int(sys.argv[5]), sys.argv[6] == 'gate'))
