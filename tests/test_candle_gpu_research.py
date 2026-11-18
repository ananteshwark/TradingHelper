"""The local candle study must never use later entry/exit prices as predictors."""
from __future__ import annotations

import datetime as dt
import runpy
from pathlib import Path

opening_features = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / 'research/candle_gpu/build.py'))[
        'opening_features']


def bars():
    rows = []
    for minute in range(15, 45, 5):
        price = 100 + (minute - 15) / 10
        rows.append((dt.time(9, minute), price, price + 1, price - 1,
                     price + 0.5, 100 + minute))
    rows += [(dt.time(9, 50), 104, 105, 103, 104.5, 300),
             (dt.time(15, 0), 105, 106, 104, 105.5, 400)]
    return rows


def test_future_price_changes_label_but_no_opening_feature():
    first = opening_features(bars())
    changed = bars()
    changed[-2] = (dt.time(9, 50), 120, 121, 119, 120.5, 100_000)
    changed[-1] = (dt.time(15, 0), 90, 91, 89, 90.5, 100_000)
    second = opening_features(changed)
    assert first is not None and second is not None
    assert {key: value for key, value in first.items() if key.startswith('cv_')} == {
        key: value for key, value in second.items() if key.startswith('cv_')}
    assert first['target_0950_1500'] != second['target_0950_1500']


def test_incomplete_opening_sequence_has_no_example():
    assert opening_features(bars()[1:]) is None
