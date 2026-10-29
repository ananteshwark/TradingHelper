"""Train the spike-picks model once on every decision point to date (PROTOCOL.md settings)
and export it to the compact JSON that igs.intraday.spike_ml.Picker reads (PAPER.md).

Two checks before it is used: the app's feature code (spike_ml.points) reproduces
events.py's features on sampled stock-days, and the exported trees reproduce LightGBM.

Usage: uv run python -I export_model.py EVENTS_DIR DAYS_DIR NIFTY_JSON OUT_JSON
"""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

from igs.intraday import spike_ml
from igs.intraday.engine import Candle
from igs.timeutil import IST

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model as M  # noqa: E402

events, days_dir, nifty_json, out = (Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3],
                                     Path(sys.argv[4]))
ev = M.distinct(pl.read_parquet(events / 'events.parquet'))
features = [c for c in ev.columns if c.startswith('f_')]
assert features == spike_ml.FEATURES, features


def compact(node):
    if 'leaf_value' in node:
        return round(node['leaf_value'], 12)
    assert node['decision_type'] == '<=' and node.get('missing_type') == 'None'
    return [node['split_feature'], node['threshold'], compact(node['left_child']),
            compact(node['right_child'])]


boosters = {k: M.fit(ev, features, f'net_{k}_{M.SLIP}') for k in M.TS}
data = {'name': 'spike-ml-v1', 'features': features, 'targets': list(M.TS), 'gate': 0.5,
        'trained_on': f"{ev['day'].min()} to {ev['day'].max()}", 'rows': ev.height,
        'target': 'net % of a fade with each target, no stop, out at 15:15, after charges '
                  'and 0.15% slippage a side (research/spike_ml/PROTOCOL.md)',
        'settings': 'LightGBM 4.7: 400 rounds, learning rate 0.03, 15 leaves, 200 rows a '
                    'leaf, 80% rows and features, L2 1, seed 7',
        'models': [[compact(t['tree_structure']) for t in boosters[k].dump_model()['tree_info']]
                   for k in M.TS]}
out.write_text(json.dumps(data, separators=(',', ':')))

# The exported trees against LightGBM.
picker = spike_ml.Picker(json.loads(out.read_text()))
sample = ev.sample(3000, seed=3)
a = np.column_stack([boosters[k].predict(sample.select(features).to_numpy()) for k in M.TS])
b = np.array([picker.predict(r) for r in sample.iter_rows(named=True)])
print('rows', ev.height, data['trained_on'], 'bytes', out.stat().st_size,
      'max abs prediction difference', float(np.abs(a - b).max()))
assert np.abs(a - b).max() < 1e-9

# The app's features against events.py's, on sampled stock-days.
nifty = {}
for ts, o, h, lo, c, v, *_ in json.loads(Path(nifty_json).read_text()):
    nifty.setdefault(ts[:10], []).append(Candle(dt.datetime.fromisoformat(ts), o, h, lo, c, v))
keys = ev.select('isin', 'day').unique().sort('isin', 'day').sample(400, seed=5)
days = pl.read_parquet(days_dir / 'days.parquet').join(keys, on=['isin', 'day'])
worst, compared, invalid = 0.0, 0, 0
for (isin, day), g in days.sort('isin', 'day', 'minute').group_by(['isin', 'day'],
                                                                   maintain_order=True):
    try:
        bars = [Candle(dt.datetime.combine(day, dt.time(m // 60, m % 60), IST), o, h, lo, c, v)
                for m, o, h, lo, c, v in g.select('minute', 'open', 'high', 'low', 'close',
                                                  'volume').iter_rows()]
    except ValueError:            # an inconsistent candle: the live feed refuses the day too
        invalid += 1
        continue
    r0 = g.row(0, named=True)
    pts = spike_ml.points(bars, r0['prevclose'], r0['prev_ret'], r0['med20'],
                          sorted(nifty.get(str(day), []), key=lambda b: b.start))
    want = ev.filter((pl.col('isin') == isin) & (pl.col('day') == day))
    for p in pts:
        # One decision a candle, its lowest threshold (model.distinct): a candle that rose
        # 5% and fell 7% within 30 minutes is a rise at 5% and a fall at 7%.
        w = want.filter((pl.col('i') == p['i']) & (pl.col('x') == p['x']))
        if not w.height:
            continue                  # a higher threshold, or no entry candle before 15:15
        w = w.row(0, named=True)
        for f in features[:-2]:       # the crowd's two are counted across stocks
            worst = max(worst, abs(p['features'][f] - w[f]))
        compared += 1
print('stock-days with an inconsistent candle, skipped', invalid, '; decision points '
      'compared', compared, 'max abs feature difference', worst)
assert compared > 300 and worst < 1e-9
