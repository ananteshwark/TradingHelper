"""Train Study D's chosen model (LightGBM, five seeds averaged, PROTOCOL.md settings) on
every row to date and export it to the JSON igs.intraday.evening.Ensemble reads; check that
the app's evaluator agrees with LightGBM.

Usage: python export_model.py FEATURES.parquet OUT.json
"""
import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import model as M  # noqa: E402


def compact(node):
    if 'leaf_value' in node:
        return round(node['leaf_value'], 12)
    # 'NaN' only routes missing values; the app never scores a row with a missing input.
    assert node['decision_type'] == '<=' and node.get('missing_type') in ('None', 'NaN')
    return [node['split_feature'], node['threshold'], compact(node['left_child']),
            compact(node['right_child'])]


df = M.prepare(sys.argv[1])
X, y = df.select(M.FEATURES).to_numpy(), df['y'].to_numpy()
boosters = []
for seed in M.SEEDS:
    boosters.append(lgb.LGBMRegressor(
        n_estimators=400, learning_rate=0.03, num_leaves=15, min_child_samples=500,
        subsample=0.7, subsample_freq=1, colsample_bytree=0.7, reg_lambda=5,
        random_state=seed, verbose=-1).fit(X, y))
out = {
    'name': 'intraday-evening-v1',
    'features': M.FEATURES,
    'trained_on': f"{df['day'].min()} to {df['day'].max()}",
    'rows': df.height,
    'target': 'return from the 09:15 candle open to the 15:15 candle open of the next '
              'session, clipped at +/-5%',
    'settings': 'LightGBM 4.7: 400 trees, learning rate 0.03, 15 leaves, 500 rows a leaf, '
                '70% rows and features, L2 5; mean of seeds 1-5',
    'gate': 0.0015, 'k': 3, 'slippage': M.SLIP,
    'ensemble': [[compact(t['tree_structure']) for t in b.booster_.dump_model()['tree_info']]
                 for b in boosters],
}
Path(sys.argv[2]).write_text(json.dumps(out, separators=(',', ':')))

from igs.intraday.evening import Ensemble  # noqa: E402

m = Ensemble(json.loads(Path(sys.argv[2]).read_text()))
rows = df.sample(3000, seed=3)
Xs = rows.select(M.FEATURES).to_numpy()
a = np.mean([b.predict(Xs) for b in boosters], 0)
nan_free = ~np.isnan(Xs).any(1)
b = np.array([m.predict(r) for r in rows.iter_rows(named=True)])
print('rows', df.height, out['trained_on'], 'bytes', Path(sys.argv[2]).stat().st_size,
      'max abs diff (complete rows)', np.abs(a - b)[nan_free].max(),
      'rows with a missing input', int((~nan_free).sum()))
