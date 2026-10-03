"""Frozen-rule comparison; writes research reports, never production IC promotion files."""
import datetime as dt
import json
from pathlib import Path

from igs.backtest.engine import run_backtest
from igs.backtest.report import summarise, write_report
from igs.config import load_backtest, load_costs, load_scoring, load_universe
from igs.pit.gate import require_gate
from igs.pit.loader import load_dataset
from igs.provenance import validation_fingerprint
from igs.timeutil import utc_now
from igs.universe import price_series


def compare(conn, start: dt.date, end: dt.date, output: Path):
    require_gate()
    if start >= end or end >= utc_now().date():
        raise ValueError('validation needs start < end < today')
    sc, uc, bt, cc = load_scoring(), load_universe(), load_backtest(), load_costs()
    dataset = load_dataset(conn, dt.date(start.year-6, 1, 1), utc_now().date(),
                           series=tuple(price_series(uc)))
    # Freeze these rules before prospective evaluation. This historical comparison is
    # exploratory: the roadmap was designed after observing the available dataset.
    no_sentiment = sc.model_copy(update={
        'geopolitical': sc.geopolitical.model_copy(update={'enabled': False}),
        'sentiment': sc.sentiment.model_copy(update={
            'market': sc.sentiment.market.model_copy(update={'enabled': False}),
            'stock': sc.sentiment.stock.model_copy(update={'enabled': False})})})
    variants = [('eligible_baseline', sc, 'baseline'),
                ('early_growth', sc, 'early'), ('established_growth', sc, 'established'),
                ('volume_confirmation', sc, 'volume'),
                ('without_sentiment', no_sentiment, 'baseline')]
    result = {'generated_at': utc_now().isoformat(), 'start': str(start), 'end': str(end),
              'fingerprint': validation_fingerprint(), 'status': 'INCONCLUSIVE',
              'production_weights_changed': False, 'variants': {},
              'limitations': ['Historical comparison is exploratory, not a prospective holdout.',
                  'Insufficient history or observations cannot establish predictive value.',
                  'Sentiment exists only after real collection/assessment, never retroactively.',
                  'Reported costs/turnover use the configured simulator and charges; '
                  'delisting exits and missing liquidity follow its documented assumptions.'],
              'prospective_start': str(utc_now().date()),
              'scoring_config': sc.model_dump(), 'universe_config': uc.model_dump(),
              'cost_config': cc.model_dump()}
    for name, config, selection in variants:
        print(f'Validating {name}...', flush=True)
        res = run_backtest(dataset, start, end, 'quarterly', bt, config, uc, cc,
                           research_filter=selection)
        write_report(res, output/name, f'Research validation: {name}')
        result['variants'][name] = {**summarise(res),
            'realized_periods': res.periods.height,
            'coverage': res.universe_sizes.to_dicts(),
            'warnings': [i.message for i in res.dq.issues]}
    output.mkdir(parents=True, exist_ok=True)
    (output/'comparison.json').write_text(json.dumps(result, indent=2, default=str)+'\n')
    return result
