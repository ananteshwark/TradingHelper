"""Experimental business-growth evidence, independent of sentiment and price ranks."""
import json

import polars as pl

from igs.factors.growth import short_growth
from igs.factors.registry import REGISTRY
from igs.factors.research import RESEARCH


def assess(view, results: pl.DataFrame, flags: pl.DataFrame) -> pl.DataFrame:
    specs = {'revenue_quarter_yoy': ('top_line', 1, False),
             'pat_quarter_yoy': ('pat', 1, False),
             'revenue_2q_yoy': ('top_line', 2, False),
             'pat_2q_yoy': ('pat', 2, False),
             'opm_quarter_yoy': ('ebitda', 1, True)}
    readings = {name: {r['company_id']: r for r in short_growth(view, *args).to_dicts()}
                for name, args in specs.items()}
    for name in ('revenue_cagr_3y', 'pat_cagr_3y', 'growth_consistency_12q', *RESEARCH):
        readings[name] = {r['company_id']: r for r in REGISTRY[name].fn(view).to_dicts()}
    rejected = set(flags.filter((pl.col('status') == 'tripped')
                               & (pl.col('severity') == 'reject'))['company_id'].to_list())
    output = []
    for cid in results['company_id']:
        evidence = {name: rows[cid] for name, rows in readings.items() if cid in rows}
        def value(name, evidence=evidence):
            return evidence.get(name, {}).get('value')
        core = [value(n) for n in ('revenue_quarter_yoy', 'pat_quarter_yoy',
                                  'revenue_2q_yoy', 'pat_2q_yoy')]
        reasons = []
        if any(v is None for v in core):
            profile = 'Insufficient growth evidence'
            reasons.append('Need two matched year-ago quarters of revenue and positive profit; '
                           'profit base margin must be at least 2%.')
        elif not all(0 < v <= cap for v, cap in zip(core, (10, 20, 10, 20), strict=True)):
            profile = 'Not qualified'
            reasons.append('Growth is non-positive or outside the experimental '
                           'plausibility limits.')
        else:
            profile = 'Early growth'
            long = [value(n) for n in ('revenue_cagr_3y', 'pat_cagr_3y')]
            consistent = value('growth_consistency_12q')
            if all(v is not None and 0 < v <= 3 for v in long) and consistent is not None \
                    and consistent >= 9:
                profile = 'Established growth'
        if cid in rejected:
            reasons.append('A rejection-level risk check is tripped; review the stock risk checks.')
            profile = 'Risk blocked'
        output.append(json.dumps({'version': 1, 'experimental': True, 'profile': profile,
                                  'growth_strength': min(core) if all(v is not None for v in core)
                                  else None,
                                  'coverage': sum(v is not None for v in core) / 4,
                                  'reasons': reasons, 'evidence': evidence}))
    return results.with_columns(pl.Series('growth_profile', output, dtype=pl.Utf8))
