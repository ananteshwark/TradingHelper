"""Why Study C's LightGBM baseline differs from Study B: rerun Study B's walk-forward and
chosen rule (LightGBM, k = 3, gate) unchanged except LightGBM's random_state.

Usage: python baseline_seeds.py FEATURES.parquet SEED [SEED ...]
"""
import datetime as dt
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ml'))
import intraday_model as B  # noqa: E402

HOLDOUT = (dt.date(2025, 1, 1), dt.date(2026, 10, 8))


def gbm(train, test, seed):
    import lightgbm as lgb
    model = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.03, num_leaves=15,
                              min_child_samples=500, subsample=0.7, subsample_freq=1,
                              colsample_bytree=0.7, reg_lambda=5, random_state=seed,
                              verbose=-1)
    model.fit(train.select(B.FEATURES).to_numpy(), train['y'].to_numpy())
    return model.predict(test.select(B.FEATURES).to_numpy())


def run(features, seed):
    df = B.prepare(features)
    parts = []
    for q, nxt in B.quarters(B.DEV[0], HOLDOUT[1]):
        train = df.filter(pl.col('day') < q)
        test = df.filter((pl.col('day') >= q) & (pl.col('day') < nxt)
                         & (pl.col('day') <= HOLDOUT[1]))
        if test.is_empty():
            continue
        parts.append(test.select('symbol', 'day', 'entry', 'exit', 'target')
                     .with_columns(pl.Series('gbm', gbm(train, test, seed))))
    pred = pl.concat(parts)
    out = {'seed': seed}
    for name, (lo, hi) in (('dev', B.DEV), ('holdout', HOLDOUT)):
        p = pred.filter(pl.col('day').is_between(lo, hi))
        days = sorted(p['day'].unique())
        t = B.trades(p, 'gbm', 3, True)
        s = B.stats(t, days)
        out.update({f'{name}_Rs': s['daily_Rs'], f'{name}_t': s['t_daily'],
                    f'{name}_PF': s['PF']})
        if name == 'holdout':
            for y in (2025, 2026):
                yd = [d for d in days if d.year == y]
                out[f'{y}_Rs'] = B.stats(t.filter(pl.col('day').dt.year() == y), yd)['daily_Rs']
    return out


if __name__ == '__main__':
    rows = []
    for seed in sys.argv[2:]:
        rows.append(run(sys.argv[1], int(seed)))
        print(rows[-1], flush=True)
    df = pl.DataFrame(rows)
    with pl.Config(tbl_cols=20, tbl_width_chars=200):
        print(df)
    for col in ('dev_Rs', 'holdout_Rs', 'holdout_t'):
        print(col, 'mean', round(df[col].mean(), 2), 'min', df[col].min(),
              'max', df[col].max())
