"""What the chosen intraday model picks: feature importance and the typical long and short."""
import sys
from pathlib import Path

import lightgbm as lgb
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import intraday_model as M      # noqa: E402

df = M.prepare(sys.argv[1])
train = df.filter(pl.col('day') < pl.date(2025, 1, 1))
gbm = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.03, num_leaves=15, min_child_samples=500,
                        subsample=0.7, subsample_freq=1, colsample_bytree=0.7, reg_lambda=5,
                        random_state=7, verbose=-1, importance_type='gain')
gbm.fit(train.select(M.FEATURES).to_numpy(), train['y'].to_numpy())
imp = sorted(zip(M.FEATURES, gbm.feature_importances_), key=lambda x: -x[1])
tot = sum(v for _, v in imp)
print('gain share:', [(f, round(v / tot * 100, 1)) for f, v in imp[:12]])
pred = pl.read_parquet(sys.argv[2]).filter(pl.col('day') >= pl.date(2023, 1, 1))
t = M.trades(pred, 'gbm', 3, True).join(df.select('symbol', 'day', 'gap', 'r30', 'rvol30', 'vwap_dist',
                                                   'prev_r1', 'r5', 'atr_pct', 'clv30', 'rel30'),
                                         on=['symbol', 'day'])
all_ = df.filter(pl.col('day') >= pl.date(2023, 1, 1))
cols = ['gap', 'r30', 'rel30', 'rvol30', 'vwap_dist', 'clv30', 'prev_r1', 'r5', 'atr_pct']
summ = pl.concat([
    all_.select([pl.col(c).median() for c in cols]).with_columns(pl.lit('all stock-days').alias('group')),
    t.filter(pl.col('side') == 1).select([pl.col(c).median() for c in cols]).with_columns(pl.lit('longs').alias('group')),
    t.filter(pl.col('side') == -1).select([pl.col(c).median() for c in cols]).with_columns(pl.lit('shorts').alias('group'))])
with pl.Config(tbl_cols=20, tbl_width_chars=200, float_precision=4):
    print(summ.select('group', *cols))
