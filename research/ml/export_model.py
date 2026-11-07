"""Train the chosen intraday model (LightGBM, PROTOCOL.md settings) on every row to date and
export it to the compact JSON that igs.intraday.ml.Model reads; check the two agree."""
import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import intraday_model as M      # noqa: E402

df = M.prepare(sys.argv[1])
X, y = df.select(M.FEATURES).to_numpy(), df['y'].to_numpy()
gbm = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.03, num_leaves=15, min_child_samples=500,
                        subsample=0.7, subsample_freq=1, colsample_bytree=0.7, reg_lambda=5,
                        random_state=7, verbose=-1).fit(X, y)
dump = gbm.booster_.dump_model()


def compact(node):
    if 'leaf_value' in node:
        return round(node['leaf_value'], 12)
    assert node['decision_type'] == '<=' and node.get('missing_type') == 'None'
    return [node['split_feature'], node['threshold'], compact(node['left_child']),
            compact(node['right_child'])]


out = {
    'name': 'intraday-ml-v1',
    'features': M.FEATURES,
    'trained_on': f"{df['day'].min()} to {df['day'].max()}",
    'rows': df.height,
    'target': 'return from the 09:45 candle open to the 15:15 candle open, clipped at +/-5%',
    'settings': 'LightGBM 4.7: 400 trees, learning rate 0.03, 15 leaves, 500 rows a leaf, '
                '70% rows and features, L2 5, seed 7',
    'gate': M.GATE, 'k': 3,
    'trees': [compact(t['tree_structure']) for t in dump['tree_info']],
}
Path(sys.argv[2]).write_text(json.dumps(out, separators=(',', ':')))

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.intraday.ml import Model   # noqa: E402
m = Model(json.loads(Path(sys.argv[2]).read_text()))
rows = df.sample(3000, seed=3)
a = gbm.predict(rows.select(M.FEATURES).to_numpy())
b = np.array([m.predict(r) for r in rows.iter_rows(named=True)])
print('rows', df.height, out['trained_on'], 'bytes', Path(sys.argv[2]).stat().st_size,
      'max abs diff', np.abs(a - b).max())
