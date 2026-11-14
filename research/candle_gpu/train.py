"""GPU ablation and after-cost historical comparison for opening candle/volume features.

Usage: uv run --with xgboost-cu12==3.4.1 python train.py DATA.parquet OUT_DIR
The same rows, dates, learner and fixed trading rule are used for both feature sets.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import xgboost as xgb

from igs.intraday.costs import charges, intraday_rates

BASE = (
    'gap', 'r30', 'prev_r1', 'r5', 'range30_atr', 'rvol30', 'vwap_dist', 'clv30',
    'prev_clv', 'prev_last_hour', 'prev_intraday', 'r21', 'atr_pct', 'dist20h',
    'dist20l', 'log_value20', 'ovn5', 'intra5', 'n_gap', 'n_r30', 'n_prev_r1',
    'n_r5', 'rel30', 'dow',
)
PARAMS = {
    'objective': 'reg:squarederror', 'tree_method': 'hist', 'device': 'cuda',
    'max_depth': 5, 'max_bin': 128, 'min_child_weight': 50,
    'eta': 0.03, 'subsample': 0.8, 'colsample_bytree': 0.8,
    'reg_lambda': 10, 'seed': 1729, 'nthread': 4,
}
ROUNDS = 300
MIN_VALUE = 50e7
NOTIONAL = 100_000
GATE = 0.0015


def matrix(frame: pl.DataFrame, names: list[str]) -> np.ndarray:
    values = frame.select(names).to_numpy().astype(np.float32)
    values[~np.isfinite(values)] = np.nan
    return values


def date_ic(frame: pl.DataFrame, feature: str) -> dict:
    """One cross-sectional rank correlation per date; a market-wide move is removed."""
    grouped = frame.group_by('day').agg(
        pl.len().alias('n'), pl.corr(feature, 'target_0950_1500', method='spearman').alias('ic'))
    usable = grouped.filter((pl.col('n') >= 10) & pl.col('ic').is_finite())
    return {'dates': usable.height, 'mean_ic': usable['ic'].mean() if usable.height else None}


def portfolio(frame: pl.DataFrame, slip: float) -> dict:
    """Fixed three-per-side threshold, no calls on days without enough signal."""
    rates = intraday_rates()
    pnl, trades, wins, gross, daily = [], 0, 0, [], []
    for date, day in frame.group_by('day', maintain_order=True):
        ranked = day.sort('prediction')
        chosen = ((1, ranked.filter(pl.col('prediction') > GATE).tail(3)),
                  (-1, ranked.filter(pl.col('prediction') < -GATE).head(3)))
        day_pnl = 0.0
        side_pnl = {'buy': 0.0, 'short': 0.0}
        side_trades = {'buy': 0, 'short': 0}
        for side, picks in chosen:
            for row in picks.iter_rows(named=True):
                entry = row['entry_0950'] * (1 + side * slip)
                exit_ = row['exit_1500'] * (1 - side * slip)
                qty = int(NOTIONAL // entry)
                if qty < 1:
                    continue
                fee = float(charges(qty, Decimal(str(round(entry, 2))),
                                    Decimal(str(round(exit_, 2))),
                                    'buy' if side == 1 else 'sell', rates))
                result = side * (exit_ - entry) * qty - fee
                day_pnl += result
                label = 'buy' if side == 1 else 'short'
                side_pnl[label] += result
                side_trades[label] += 1
                trades += 1
                wins += result > 0
                gross.append(side * row['target_0950_1500'])
        pnl.append(day_pnl)
        daily.append({'date': str(date[0]), 'net_inr': day_pnl,
                      'side_net_inr': side_pnl,
                      'side_trades': side_trades})
    values = np.asarray(pnl, dtype=float)
    error = values.std(ddof=1) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return {
        'dates': len(values), 'trades': trades, 'win_rate': wins / trades if trades else None,
        'mean_gross_return': float(np.mean(gross)) if gross else None,
        'mean_daily_net_rupees': float(values.mean()) if len(values) else None,
        'daily_t': float(values.mean() / error) if error else None,
        'total_net_rupees': float(values.sum()),
        'daily': daily,
    }


def calibration(frame: pl.DataFrame) -> list[dict]:
    """Prediction deciles, retaining the signed raw return target."""
    ordered = frame.sort('prediction').with_row_index('rank')
    ordered = ordered.with_columns(
        (pl.col('rank') * 10 // ordered.height).alias('decile'))
    return [
        {'decile': row['decile'], 'rows': row['rows'],
         'mean_prediction': row['mean_prediction'], 'mean_actual': row['mean_actual']}
        for row in ordered.group_by('decile').agg(
            pl.len().alias('rows'), pl.col('prediction').mean().alias('mean_prediction'),
            pl.col('target_0950_1500').mean().alias('mean_actual'))
        .sort('decile').iter_rows(named=True)
    ]


def paired_daily_delta(first: dict, second: dict) -> dict:
    """Expanded minus baseline net result on the same decision dates."""
    a, b = first['daily'], second['daily']
    if [d['date'] for d in a] != [d['date'] for d in b]:
        raise ValueError('Models must cover identical decision dates')
    difference = np.asarray([y['net_inr'] - x['net_inr'] for x, y in zip(a, b, strict=True)],
                            dtype=float)
    error = difference.std(ddof=1) / math.sqrt(len(difference)) if len(difference) > 1 else 0.0
    return {'dates': len(difference), 'mean_daily_delta_inr': float(difference.mean()),
            'daily_t': float(difference.mean() / error) if error else None}


def fit_predict(train: pl.DataFrame, later: pl.DataFrame, names: list[str]):
    labels = train['target_0950_1500'].clip(-0.05, 0.05).to_numpy().astype(np.float32)
    model = xgb.train(PARAMS, xgb.QuantileDMatrix(matrix(train, names), label=labels,
                                                  feature_names=names,
                                                  max_bin=PARAMS['max_bin']),
                      num_boost_round=ROUNDS)
    device = json.loads(model.save_config())['learner']['generic_param']['device']
    if not device.startswith('cuda'):
        raise RuntimeError(f'GPU training requested, but XGBoost used {device}')
    predictions = model.predict(xgb.DMatrix(matrix(later, names), feature_names=names))
    return model, predictions, device


def study(source: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f'Experiment output already exists: {output}')
    data = pl.read_parquet(source).filter(
        (pl.col('value20') >= MIN_VALUE)
        & pl.col('target_0950_1500').is_finite()
        & (pl.col('entry_0950') > 0)
        & (pl.col('exit_1500') > 0))
    enhanced = list(BASE) + sorted(name for name in data.columns if name.startswith('cv_'))
    train = data.filter(pl.col('day') < dt.date(2025, 1, 1))
    val = data.filter(pl.col('day').is_between(dt.date(2025, 1, 1), dt.date(2025, 12, 31)))
    test = data.filter(pl.col('day') >= dt.date(2026, 1, 1))
    if any(part['day'].n_unique() < 100 for part in (train, val, test)):
        raise ValueError('Need at least 100 distinct dates in each chronological period')
    print('rows', [part.height for part in (train, val, test)], flush=True)
    later = pl.concat([val, test]).sort(['day', 'symbol'])
    output.mkdir(parents=True)
    results = {}
    for name, features in (('baseline', list(BASE)), ('augmented', enhanced)):
        model, pred, device = fit_predict(train, later, features)
        model.save_model(str(output / f'{name}.json'))
        scored = later.select('symbol', 'day', 'target_0950_1500', 'entry_0950',
                              'exit_1500').with_columns(pl.Series('prediction', pred))
        scored.write_parquet(output / f'{name}-predictions.parquet')
        report = {'device': device, 'features': features, 'periods': {}}
        for period, lo, hi in (('2025', dt.date(2025, 1, 1), dt.date(2025, 12, 31)),
                               ('2026', dt.date(2026, 1, 1), dt.date(2026, 12, 31))):
            part = scored.filter(pl.col('day').is_between(lo, hi))
            report['periods'][period] = {
                'rows': part.height, 'rank_ic': date_ic(part, 'prediction'),
                'calibration': calibration(part),
                'after_costs': {str(slip): portfolio(part, slip)
                                for slip in (0.0002, 0.0005, 0.001)},
            }
        results[name] = report
        print(name, {period: {'rank_ic': value['rank_ic'],
                              'net_0.0002': value['after_costs']['0.0002']['mean_daily_net_rupees']}
                     for period, value in report['periods'].items()}, flush=True)
    paired = {}
    for period in ('2025', '2026'):
        paired[period] = {
            slip: paired_daily_delta(results['baseline']['periods'][period]['after_costs'][slip],
                                     results['augmented']['periods'][period]['after_costs'][slip])
            for slip in ('0.0002', '0.0005', '0.001')}
    correlations = {}
    for period, part in (('train', train), ('2025', val), ('2026', test)):
        correlations[period] = {
            name: date_ic(part, name) for name in enhanced if name.startswith('cv_')}
    manifest = {
        'protocol_sha256': hashlib.sha256(
            (Path(__file__).parent / 'PROTOCOL.md').read_bytes()).hexdigest(),
        'feature_code_sha256': hashlib.sha256(
            (Path(__file__).parent / 'build.py').read_bytes()).hexdigest(),
        'training_code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'data_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'xgboost_version': xgb.__version__, 'parameters': PARAMS, 'rounds': ROUNDS,
        'split': {'train': 'before 2025', 'validation': '2025', 'exploratory': '2026'},
        'limitations': ['2025-2026 were examined by earlier studies; neither is an '
                        'independent holdout.',
                        'Current Nifty 200 members create survivorship bias.',
                        'Historical fills and charges are simulated; no server model is changed.'],
        'models': results, 'paired_daily_delta': paired,
        'candle_volume_correlations': correlations,
    }
    (output / 'report.json').write_text(json.dumps(manifest, indent=2, allow_nan=False) + '\n')
    return manifest


if __name__ == '__main__':
    study(Path(sys.argv[1]), Path(sys.argv[2]))
