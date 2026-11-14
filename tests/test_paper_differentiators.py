"""Guard side separation, chronological evidence, and day-level comparisons."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'paper_differentiators', ROOT / 'research' / 'paper_differentiators' / 'analyze.py')
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


def row(day, pnl, value, side=1):
    return {'date': day, 'net_inr': pnl, 'side': side,
            'features': {'prev_r1': value, 'n_r5': value}}


def test_same_day_comparison_removes_market_day_composition():
    rows = [row('a', 10, 5)] * 20 + [row('a', -1, 5)]
    rows += [row('b', 1, 1), row('b', -10, 1)]
    assert study.describe(rows, 'n_r5')['median_gap'] > 0
    daily = study.within_day(rows, 'n_r5')
    assert daily['mixed_outcome_days'] == 2
    assert daily['nonzero_gap_days'] == 0
    assert daily['mean_daily_gap'] == 0


def test_daily_association_does_not_count_repeated_stocks_as_days():
    rows = [row('a', 1, 1), row('b', 3, 2), row('c', 2, 3)]
    original = study.daily_association(rows, 'n_r5')
    repeated = study.daily_association(rows + [rows[0]] * 30, 'n_r5')
    assert repeated == original
    assert original['days'] == 3
    assert original['spearman_mean_daily_pnl'] == pytest.approx(.5)


def test_opposite_sides_are_not_merged_and_empty_later_side_is_not_confirmed():
    days = ['a', 'b', 'c']
    rows = [row(d, p, x, side) for d in days for side in (1, -1)
            for p, x in ((1, 10 * side), (-1, 0))]
    # Shorts occur only in the earlier block, as in the evening sample.
    rows = [r for r in rows if not (r['date'] == 'c' and r['side'] == -1)]
    result = study.analyze({'strategies': {'example': {'days': days, 'rows': rows}}})
    groups = result['example']['groups']
    assert groups['buy']['features']['prev_r1']['all']['median_gap'] == 10
    assert groups['short']['features']['prev_r1']['all']['median_gap'] == -10
    assert groups['short']['later_trades'] == 0
    assert not groups['short']['features']['prev_r1']['same_direction_in_both_blocks']


def test_missing_features_are_not_treated_as_zero():
    rows = [row('a', 1, None), row('b', -1, float('nan')), row('c', 1, 2)]
    d = study.describe(rows, 'prev_r1')
    assert d['winners'] == 1 and d['losers'] == 0
    assert d['loser_median'] is None
    assert d['standardized_mean_gap'] is None
