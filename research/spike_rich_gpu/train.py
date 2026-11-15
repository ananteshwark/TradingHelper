"""Fit the frozen three-arm spike comparison and save dated simulated trades.

Usage: uv run --with xgboost-cu12==3.4.1 --with lightgbm python train.py \
  RICH.parquet CALENDAR.json OUT_DIR
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl
import xgboost as xgb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'spike_ml'))
import model as original  # noqa: E402

ROOT = Path(__file__).resolve().parent
TARGETS = (1, 2, 3, 4, 5)
PARAMS = {'objective': 'reg:squarederror', 'device': 'cuda', 'tree_method': 'hist',
          'max_depth': 4, 'eta': .03, 'min_child_weight': 30, 'max_bin': 128,
          'subsample': .8, 'colsample_bytree': .8, 'reg_lambda': 10,
          'seed': 1729, 'nthread': 4}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def daily(t: pl.DataFrame, sessions: list[dt.date]) -> np.ndarray:
    sums = dict(t.group_by('day').agg(pl.col('pnl').sum()).iter_rows()) if t.height else {}
    return np.array([sums.get(day, 0.) for day in sessions], dtype=float)


def metrics(t: pl.DataFrame, sessions: list[dt.date]) -> dict:
    values = daily(t, sessions)
    mean = float(values.mean())
    sd = float(values.std(ddof=1)) if len(values) > 1 else 0.
    wins = float(t.filter(pl.col('pnl') > 0)['pnl'].sum()) if t.height else 0.
    losses = -float(t.filter(pl.col('pnl') < 0)['pnl'].sum()) if t.height else 0.
    short = t.filter(pl.col('side') == 1)
    buy = t.filter(pl.col('side') == -1)
    worst_i = int(np.argmin(values)) if len(values) else None
    busy = (t.group_by('day').agg(pl.len().alias('n')).filter(pl.col('n') >= 20).height
            if t.height else 0)
    return {'sessions': len(sessions), 'trades': t.height,
            'net_rupees': round(float(values.sum()), 2),
            'daily_rupees': round(mean, 2),
            't_daily': round(mean / (sd / math.sqrt(len(values))), 3) if sd else None,
            'net_pct_trade': round(float(t['net'].mean()), 3) if t.height else None,
            'win_pct': round(100 * float((t['pnl'] > 0).mean()), 1) if t.height else None,
            'profit_factor': round(wins / losses, 3) if losses else None,
            'short_trades': short.height, 'short_rupees': round(float(short['pnl'].sum()), 2),
            'buy_trades': buy.height, 'buy_rupees': round(float(buy['pnl'].sum()), 2),
            'worst_date': str(sessions[worst_i]) if worst_i is not None else None,
            'worst_rupees': round(float(values[worst_i]), 2) if worst_i is not None else None,
            'dates_20plus': busy, 'locked_exits': t.filter(pl.col('locked')).height}


def capacity(t: pl.DataFrame) -> pl.DataFrame:
    if not t.height:
        return t
    return (t.sort('day', 'minute', 'isin').with_columns(
        pl.col('day').cum_count().over('day').alias('day_rank'))
        .filter(pl.col('day_rank') <= 10).drop('day_rank'))


def paired(a: pl.DataFrame, b: pl.DataFrame, sessions: list[dt.date]) -> dict:
    delta = daily(a, sessions) - daily(b, sessions)
    sd = float(delta.std(ddof=1)) if len(delta) > 1 else 0.
    mean = float(delta.mean())
    return {'daily_rupees': round(mean, 2), 'total_rupees': round(float(delta.sum()), 2),
            't_daily': round(mean / (sd / math.sqrt(len(delta))), 3) if sd else None,
            'positive_days': int((delta > 0).sum()), 'negative_days': int((delta < 0).sum())}


def fit_xgb(train: pl.DataFrame, features: list[str], label: str) -> xgb.Booster:
    t = train.filter(pl.col(label).is_not_nan() & pl.col(label).is_not_null())
    matrix = xgb.QuantileDMatrix(t.select(features).to_numpy().astype(np.float32),
                                 label=t[label].to_numpy(), feature_names=features,
                                 max_bin=PARAMS['max_bin'])
    model = xgb.train(PARAMS, matrix, num_boost_round=300)
    device = json.loads(model.save_config())['learner']['generic_param']['device']
    if not device.startswith('cuda:'):
        raise RuntimeError('XGBoost did not train with CUDA')
    return model


def evaluate(ev: pl.DataFrame, models: dict, features: list[str], kind: str) -> np.ndarray:
    matrix = ev.select(features).to_numpy().astype(np.float32)
    if kind == 'xgb':
        dmatrix = xgb.DMatrix(matrix, feature_names=features)
        return np.column_stack([models[k].predict(dmatrix) for k in TARGETS])
    return np.column_stack([models[k].predict(matrix) for k in TARGETS])


def run(source: Path, calendar_path: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    ev = original.distinct(pl.read_parquet(source))
    baseline = [c for c in ev.columns if c.startswith('f_')]
    rich = baseline + [c for c in ev.columns if c.startswith('r_')]
    train = ev.filter(pl.col('day') < dt.date(2025, 1, 1))
    evaluation = ev.filter(pl.col('day') >= dt.date(2025, 1, 1))
    if train.is_empty() or evaluation.is_empty():
        raise ValueError('Training or evaluation decision points missing')
    calendar = [dt.date.fromisoformat(d) for d in json.loads(calendar_path.read_text())]
    report = {'protocol_sha256': sha(ROOT / 'PROTOCOL.md'),
              'source_sha256': sha(source), 'calendar_sha256': sha(calendar_path),
              'train_code_sha256': sha(Path(__file__)), 'xgboost': xgb.__version__,
              'lightgbm': lgb.__version__, 'gpu_devices': [],
              'train_rows': train.height, 'evaluation_rows': evaluation.height,
              'features_baseline': baseline, 'features_rich': rich, 'periods': {}}
    arms = {'lightgbm_24': (baseline, 'lgb'), 'xgboost_24': (baseline, 'xgb'),
            'xgboost_rich': (rich, 'xgb')}
    predictions = {}
    for arm, (features, kind) in arms.items():
        models = {}
        for k in TARGETS:
            label = f'net_{k}_15'
            models[k] = (original.fit(train, features, label) if kind == 'lgb'
                         else fit_xgb(train, features, label))
            path = output / f'{arm}_target{k}.json'
            if kind == 'lgb':
                models[k].save_model(str(path))
            else:
                device = json.loads(models[k].save_config())['learner']['generic_param']['device']
                report['gpu_devices'] = sorted(set([*report['gpu_devices'], device]))
                models[k].save_model(path)
        predictions[arm] = evaluate(evaluation, models, features, kind)
        print(f'fit {arm}: {len(features)} features, {len(models)} targets', flush=True)
    saved = []
    for year in (2025, 2026):
        sessions = [d for d in calendar if d.year == year]
        period = evaluation.filter(pl.col('day').dt.year() == year)
        indexes = evaluation.with_row_index().filter(pl.col('day').dt.year() == year)['index']
        report['periods'][str(year)] = {'sessions': len(sessions), 'rows': period.height,
                                        'arms': {}, 'paired_vs_lightgbm': {},
                                        'paired_rich_vs_xgboost24': None}
        trades = {}
        for arm, pred in predictions.items():
            subset = pred[indexes.to_numpy()]
            t = original.trades(period, subset, gate=.5, slip=15)
            trades[arm] = t
            frame = t.select('day', 'isin', 'minute', 'side', 'x', 't_star',
                             'best', 'pnl', 'net', 'why', 'locked')
            frame.write_parquet(output / f'{arm}_{year}_trades.parquet')
            saved.append(output / f'{arm}_{year}_trades.parquet')
            report['periods'][str(year)]['arms'][arm] = {
                'base': metrics(t, sessions),
                'first10': metrics(capacity(t), sessions),
                'slip_005': metrics(original.trades(period, subset, .5, 5), sessions),
                'slip_030': metrics(original.trades(period, subset, .5, 30), sessions),
            }
        p = report['periods'][str(year)]
        for arm in ('xgboost_24', 'xgboost_rich'):
            p['paired_vs_lightgbm'][arm] = paired(trades[arm], trades['lightgbm_24'],
                                                  sessions)
        p['paired_rich_vs_xgboost24'] = paired(trades['xgboost_rich'],
                                               trades['xgboost_24'], sessions)
        print(year, {arm: p['arms'][arm]['base'] for arm in arms}, flush=True)
    report['model_hashes'] = {p.name: sha(p) for p in output.glob('*target*.json')}
    report['trade_hashes'] = {p.name: sha(p) for p in saved}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    run(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
