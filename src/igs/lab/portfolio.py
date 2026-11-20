"""Model-ranked, long-only research portfolio using the existing execution simulator.

Consumes frozen prices and PIT risk/liquidity metadata, never future label availability
to choose stocks. Hard concentration limits are independent of model predictions.
No order placement, production promotion or changes to the existing live gateway.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import polars as pl

from igs.backtest.engine import BacktestResult, _simulate
from igs.backtest.report import summarise
from igs.config import load_costs
from igs.lab.artifacts import bundle, digest, seal, verify, write_json
from igs.lab.dataset import KEYS
from igs.provenance import validation_fingerprint
from igs.timeutil import utc_now


def select(frame: pl.DataFrame, size: int = 20, industry_cap: float = .25,
           held: set[int] | frozenset = frozenset(), buffer: float = 2.0) -> pl.DataFrame:
    """Up to `size` names by prediction, each to be held at 1/size, with at most
    size x industry_cap per industry. A name already held stays while it ranks within the
    best buffer x size eligible names, which cuts turnover. When fewer names qualify the
    rest stays in cash: weights never rise above 1/size to fill the portfolio."""
    if not 10 <= size <= 100 or not 0 < industry_cap <= .5 or buffer < 1:
        raise ValueError('Portfolio needs 10..100 names, an industry cap in (0, 0.5] '
                         'and a buffer of at least 1')
    max_per_industry = int(size*industry_cap)
    eligible = frame.filter(pl.col('risk_clear') & pl.col('prediction').is_finite()
                            & pl.col('adv').is_finite() & (pl.col('adv') > 0)
                            & pl.col('industry').is_not_null()
                            & ~pl.col('industry').str.to_lowercase()
                            .is_in(['', 'unknown', 'miscellaneous']))
    ranked = eligible.sort(['prediction', 'company_id'], descending=[True, False]).to_dicts()
    kept = [r for i, r in enumerate(ranked) if r['company_id'] in held and i < buffer * size]
    kept_ids = {r['company_id'] for r in kept}
    picks, counts = [], {}
    for row in kept + [r for r in ranked if r['company_id'] not in kept_ids]:
        industry = row['industry']
        if counts.get(industry, 0) >= max_per_industry:
            continue
        counts[industry] = counts.get(industry, 0)+1
        picks.append(row)
        if len(picks) == size:
            break
    return pl.DataFrame(picks, schema=frame.schema).with_columns(
        pl.lit(1 / size).alias('weight'))


def backtest(source: Path, predictions: Path, output: Path, *, size=20, industry_cap=.25):
    from igs.pit.gate import require_gate
    require_gate()
    data, forecast = verify(source), verify(predictions)
    if data.get('kind') != 'igs-feature-dataset' or forecast.get('kind') != 'igs-paper-predictions':
        raise ValueError('Need a feature dataset and complete paper prediction snapshot')
    if forecast.get('dataset_manifest_hash') != digest(source/'manifest.json'):
        raise ValueError('Prediction and execution datasets differ')
    if data['frequency'] != 'monthly':
        raise ValueError('Costed model comparison currently requires monthly snapshots')
    frame = pl.read_parquet(predictions/'predictions.parquet')
    metadata = pl.read_parquet(source/'execution.parquet')
    if (frame.height != metadata.height or frame.select(KEYS).is_duplicated().any()
            or frame.join(metadata, on=KEYS, how='anti').height):
        raise ValueError('Predictions must cover every feature identity, including missing labels')
    frame = frame.join(metadata, on=KEYS, validate='1:1').with_columns(
        pl.col('as_of').dt.convert_time_zone('Asia/Kolkata').dt.date().alias('date'))
    scheduled = [dt.date.fromisoformat(r['date']) for r in
                 json.loads((source/'coverage.json').read_text())]
    if len(scheduled) < 2:
        raise ValueError('Need at least two scheduled monthly signal dates')
    selected, held = [], set()
    for date in scheduled:
        picks = select(frame.filter(pl.col('date') == date), size, industry_cap, held)
        held = set(picks['company_id'].to_list())
        selected.append(picks)
    scores = pl.concat(selected).rename({'prediction': 'composite'})
    prices = pl.read_parquet(source/'prices.parquet')
    benchmark = pl.read_parquet(source/'benchmark.parquet')
    days = [dt.date.fromisoformat(d) for d in json.loads((source/'calendar.json').read_text())]
    costs = load_costs()
    reports = {}
    with bundle(output) as path:
        scores.write_parquet(path/'selected.parquet')
        # Costs are recomputed from orders at both levels, not subtracted from labels.
        for name, cc in [('configured_costs', costs), ('double_impact', costs.model_copy(update={
            'impact': costs.impact.model_copy(update={
                'half_spread_bps': {k: 2*v for k, v in costs.impact.half_spread_bps.items()},
                'sqrt_coefficient': 2*costs.impact.sqrt_coefficient,
                'max_bps': 2*costs.impact.max_bps})}))]:
            periods = _simulate(scores, prices, benchmark, days, 1, cc, scheduled)
            periods.write_parquet(path/f'{name}.parquet')
            result = BacktestResult(frequency='monthly', dates=scheduled, scores=scores,
                factor_z=pl.DataFrame(), forward=pl.DataFrame(), ic=pl.DataFrame(),
                ic_summary=pl.DataFrame(), ic_status=pl.DataFrame(), quantiles=pl.DataFrame(),
                periods=periods, selection=pl.DataFrame(), universe_sizes=pl.DataFrame(),
                benchmark_name=data['benchmark_name'])
            reports[name] = summarise(result)
        write_json(path/'summary.json', reports)
        return seal(path, {'kind': 'igs-model-portfolio-research', 'paper_only': True,
            'created_at': utc_now().isoformat(), 'code_hash': validation_fingerprint(),
            'dataset_manifest_hash': digest(source/'manifest.json'),
            'prediction_manifest_hash': digest(predictions/'manifest.json'),
            'size': size, 'max_initial_name_weight': 1/size, 'industry_cap': industry_cap,
            'hold_buffer': 2.0,
            'costs': costs.model_dump(), 'benchmark_name': data['benchmark_name'],
            'limitations': ['Research only: historical comparisons are not promotion evidence.',
                'Concentration limits apply at target rebalance, not continuously after drift.',
                'Each name is held at 1/size; when fewer qualify the rest is cash, and missed '
                'entries leave allocations cash.',
                'A held name stays while it ranks within the best buffer x size names.',
                'Simulator retains fixed-capital additive costs and unresolved missing exits.']})
