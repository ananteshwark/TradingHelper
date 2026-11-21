"""Train Study A's models on every superset trade to date and export them for the app's
shadow predictions (docs/TRACKING.md); check that the app's evaluator agrees with
LightGBM, missing values included.

Usage: uv run --no-project --with polars --with numpy --with lightgbm --with scikit-learn \
         python -I export_models.py PATHS_DIR OUT_DIR

Writes OUT_DIR/adverse_<set>_v1.json for m0945 and evening, each holding four models with
the protocol's settings (seed 7):
- a1_net: A1's VR regression, the trade's net return after costs, clipped at +/-3%;
- a1_stop: A1's classifier, the probability that the stop is hit;
- a2_rest: A2's regression, the rest of the trade from a checkpoint, before costs;
- a2_stop: A2's classifier, the probability the stop is hit later.
A node is [feature, threshold, left, right, default_left, missing]; missing is 0 (None:
a missing value counts as 0), 1 (Zero: 0 and missing take the default side) or 2 (NaN:
missing takes the default side), as LightGBM routes them.
"""
import json
import math
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
paths, out = Path(sys.argv[1]), Path(sys.argv[2])
CLIP = 0.03
PATH_COLS = ['minutes', 'move', 'move_atr', 'best_atr', 'worst_atr', 'last15', 'rvol_since',
             'rvol_15', 'vwap_gap', 'range_pos', 'nifty_move', 'rel_move']
MISSING = {'None': 0, 'Zero': 1, 'NaN': 2}
SETTINGS = ('LightGBM 4: 200 trees, learning rate 0.03, 15 leaves, 200 rows a leaf, 80% rows '
            'and features, seed 7 (research/adverse/PROTOCOL.md)')


def params():
    return dict(n_estimators=200, learning_rate=0.03, num_leaves=15, min_child_samples=200,
                colsample_bytree=0.8, subsample=0.8, subsample_freq=1, random_state=7,
                verbose=-1)


def compact(node):
    if 'leaf_value' in node:
        return round(node['leaf_value'], 12)
    assert node['decision_type'] == '<='
    return [node['split_feature'], node['threshold'], compact(node['left_child']),
            compact(node['right_child']), bool(node['default_left']),
            MISSING[node['missing_type']]]


def evaluate(trees, x):
    """The app's evaluator (igs.tracking.Trees), here to check it."""
    total = 0.0
    for node in trees:
        while isinstance(node, list):
            f, thr, left, right, default_left, missing = node
            v = x[f]
            if v is None or (isinstance(v, float) and math.isnan(v)):
                if missing == 2:
                    node = left if default_left else right
                    continue
                v = 0.0
            if missing == 1 and v == 0.0:
                node = left if default_left else right
                continue
            node = left if v <= thr else right
        total += node
    return total


def decision_cols(t):
    signed = [c for c in t.columns if c.startswith('s_') and not c.startswith('s_own_')]
    scale = [c for c in ('range30_atr', 'rvol30', 'atr_pct', 'log_value20', 'rvol_last30')
             if c in t.columns]
    return signed + scale + ['conviction', 'rank', 'side', 'dow']


for name in ('m0945', 'evening'):
    trades = pl.read_parquet(paths / f'{name}_trades.parquet')
    checks = pl.read_parquet(paths / f'{name}_checks.parquet')
    feats = decision_cols(trades)
    checks = checks.join(trades.select('symbol', 'day', 'side',
                                       *[f for f in feats if f != 'side']),
                         on=['symbol', 'day', 'side'], how='inner')
    a2 = feats + PATH_COLS
    X, CX = trades.select(feats).to_numpy(), checks.select(a2).to_numpy()
    fits = {
        'a1_net': (lgb.LGBMRegressor(**params()).fit(
            X, trades['net'].clip(-CLIP, CLIP).to_numpy()), feats, 'regression', X),
        'a1_stop': (lgb.LGBMClassifier(**params()).fit(X, trades['stopped'].to_numpy()),
                    feats, 'logistic', X),
        'a2_rest': (lgb.LGBMRegressor(**params()).fit(
            CX, checks['rest'].clip(-CLIP, CLIP).to_numpy()), a2, 'regression', CX),
        'a2_stop': (lgb.LGBMClassifier(**params()).fit(CX, checks['stop_later'].to_numpy()),
                    a2, 'logistic', CX),
    }
    doc = {'name': f'adverse-{name}-v1', 'call_set': name,
           'trained_on': f"{trades['day'].min()} to {trades['day'].max()}",
           'trades': trades.height, 'checkpoint_rows': checks.height, 'settings': SETTINGS,
           'models': {}}
    for key, (m, cols, link, data) in fits.items():
        trees = [compact(t['tree_structure']) for t in m.booster_.dump_model()['tree_info']]
        doc['models'][key] = {'features': cols, 'link': link, 'trees': trees}
        rng = np.random.default_rng(3)
        rows = data[rng.choice(len(data), 3000, replace=False)].copy()
        holes = rows[:1000]                       # a fifth of their inputs made missing
        holes[rng.random(holes.shape) < 0.2] = np.nan
        raw = m.booster_.predict(rows, raw_score=True)
        ours = np.array([evaluate(trees, list(r)) for r in rows])
        print(f'{name} {key}: {len(trees)} trees, max |diff| {np.abs(raw - ours).max():.2e}, '
              f'rows with a missing input {int(np.isnan(rows).any(1).sum())}', flush=True)
        assert np.abs(raw - ours).max() < 1e-9
    path = out / f'adverse_{name}_v1.json'
    path.write_text(json.dumps(doc, separators=(',', ':')))
    print(path, path.stat().st_size, 'bytes')
