"""Unweighted research measures; dated source facts, no inferred missing inputs."""
from __future__ import annotations

import datetime as dt

import polars as pl

from igs.factors import base as b
from igs.factors.momentum import _recent
from igs.factors.registry import factor

SUSTAINABLE = ('eps_diluted_yoy', 'eps_dilution_pct', 'cash_profit_1y',
               'free_cash_flow_margin', 'capex_revenue', 'incremental_ebit_return')
SECTOR = ('advances_yoy', 'gnpa_pct', 'nnpa_pct', 'capital_adequacy_pct')
VOLUME = ('relative_volume_20d', 'volume_breakout_60d', 'up_down_volume_20d')
RESEARCH = (*SUSTAINABLE, *SECTOR, *VOLUME)


def _panel(view, kind, concepts):
    v, ids = b._wide(view, kind, 'research_'+kind)
    cols = ['company_id', 'period_end']
    return v.select(*cols, *[b._col(v, c).alias(c) for c in concepts]).join(
        ids.select(*cols, b._ids(ids, concepts).alias('ids')), on=cols)


def _pair(view, kind, concepts, max_age):
    panel = _panel(view, kind, concepts)
    now = panel.sort('period_end').group_by('company_id').agg(pl.all().last()).filter(
        pl.col('period_end') >= view.as_of_date-dt.timedelta(days=max_age))
    before = panel.rename({c: c+'_prev' for c in panel.columns if c != 'company_id'})
    pairs = now.join(before, on='company_id').with_columns(
        ((pl.col('period_end')-pl.col('period_end_prev')).dt.total_days()-365).abs()
          .alias('distance')).filter(pl.col('distance') <= 10)
    return pairs.sort('distance').unique('company_id', keep='first')


def _corporate_actions(view, days):
    if not view.has('corporate_actions'):
        return []
    ca = view.table('corporate_actions').filter(
        (pl.col('ex_date') >= view.as_of_date-dt.timedelta(days=days))
        & pl.col('action_type').is_in(['split', 'bonus', 'rights']))
    return ca['security_id'].to_list()


def _nonfin(view):
    return b.financials(view)


@factor('eps_diluted_yoy', 'growth', True,
        'Reported diluted quarterly EPS YoY; suppressed around split/bonus/rights events')
def eps_diluted_yoy(view):
    j = _pair(view, 'Q', ['eps_diluted', 'face_value'], 200)
    ids = b.primary_prices(view).filter(
        pl.col('security_id').is_in(_corporate_actions(view, 570)))['company_id'].unique()
    valid = ((pl.col('eps_diluted_prev') >= 0.1) & (pl.col('eps_diluted') > 0)
             & ~pl.col('company_id').is_in(ids.implode())
             & (pl.col('face_value') > 0)
             & (pl.col('face_value') == pl.col('face_value_prev')))
    j = j.with_columns(pl.when(valid).then(pl.col('eps_diluted') /
                        pl.col('eps_diluted_prev')-1).alias('v'),
                        pl.concat_list('ids', 'ids_prev').alias('all_ids'))
    return b.finish(j, 'v', ['period_end', 'period_end_prev', 'eps_diluted',
                            'eps_diluted_prev'], 'all_ids', universe=b.companies(view))


@factor('eps_dilution_pct', 'quality', False,
        '1 minus diluted/basic EPS, same reported quarter; positive EPS only')
def eps_dilution_pct(view):
    p = _panel(view, 'Q', ['eps_basic', 'eps_diluted']).sort('period_end').group_by(
        'company_id').agg(pl.all().last()).filter(
            pl.col('period_end') >= view.as_of_date-dt.timedelta(days=200))
    p = p.with_columns(pl.when((pl.col('eps_basic') > 0) & (pl.col('eps_diluted') > 0)
                               & (pl.col('eps_diluted') <= pl.col('eps_basic')))
                        .then(1-pl.col('eps_diluted')/pl.col('eps_basic')).alias('v'))
    return b.finish(p, 'v', ['period_end', 'eps_basic', 'eps_diluted'], 'ids',
                    universe=b.companies(view))


def _cash(view, metric):
    p = _panel(view, 'FY', ['cfo', 'capex', 'revenue', 'pat', 'pat_owners']).sort(
        'period_end').group_by('company_id').agg(pl.all().last()).filter(
            pl.col('period_end') >= view.as_of_date-dt.timedelta(days=550))
    profit = pl.coalesce('pat_owners', 'pat')
    den = profit if metric == 'cash_profit_1y' else pl.col('revenue')
    num = {'cash_profit_1y': pl.col('cfo'),
           'free_cash_flow_margin': pl.col('cfo')-pl.col('capex').abs(),
           'capex_revenue': pl.col('capex').abs()}[metric]
    p = p.with_columns(pl.when(den > 0).then(num/den).alias('v'))
    return b.finish(p, 'v', ['period_end', 'cfo', 'capex', 'revenue', 'pat', 'pat_owners'],
                    'ids', universe=b.companies(view), not_applicable=_nonfin(view))


for _metric in ('cash_profit_1y', 'free_cash_flow_margin', 'capex_revenue'):
    def _make(metric=_metric):
        return lambda view: _cash(view, metric)
    factor(_metric, 'quality', True,
           'Latest filed fiscal-year cash-flow measure; capex is an outflow magnitude; '
           'capex intensity is descriptive, not a standalone quality ranking')(_make())


@factor('incremental_ebit_return', 'quality', True,
        'Change in TTM EBIT / positive change in equity plus borrowings; not marginal ROIC')
def incremental_ebit_return(view):
    p = _pair(view, 'INSTANT', ['total_equity', 'borrowings_current',
                               'borrowings_noncurrent'], 400)
    # Debt missing is not debt-free. Both dates must disclose every required line.
    delta = sum(pl.col(c)-pl.col(c+'_prev') for c in
                ['total_equity', 'borrowings_current', 'borrowings_noncurrent'])
    p = p.join(b.ttm(view, 'ebit').rename({'ids': 'current_ids'}), on='company_id')
    p = p.join(b.ttm(view, 'ebit', 4).rename({'ebit_ttm': 'ebit_prev', 'ids': 'old_ids'}),
               on='company_id').join(b.latest_quarter(view), on='company_id')
    p = p.with_columns(pl.when((delta > 0) & (pl.col('period_end') == pl.col('last_period_end')))
                        .then((pl.col('ebit_ttm')-pl.col('ebit_prev'))/delta).alias('v'),
                        pl.concat_list('ids', 'ids_prev', 'current_ids', 'old_ids')
                          .alias('all_ids'))
    return b.finish(p, 'v', ['period_end', 'ebit_ttm', 'ebit_prev'], 'all_ids',
                    universe=b.companies(view), not_applicable=_nonfin(view))


def _bank(view, concept, change):
    if change:
        p = _pair(view, 'INSTANT', [concept], 400).with_columns(
            pl.when(pl.col(concept+'_prev') > 0)
              .then(pl.col(concept)/pl.col(concept+'_prev')-1).alias('v'),
            pl.concat_list('ids', 'ids_prev').alias('all_ids'))
        ids = 'all_ids'
    else:
        # Ratios are duration facts in some taxonomies, instant facts in others.
        p = pl.concat([_panel(view, k, [concept]) for k in ('Q', 'INSTANT')])
        p = p.filter(pl.col(concept).is_not_null()).sort('period_end').group_by(
            'company_id').agg(pl.all().last()).filter(
                pl.col('period_end') >= view.as_of_date-dt.timedelta(days=200))
        p = p.with_columns(pl.when(pl.col(concept).is_between(0, 100))
                            .then(pl.col(concept)).alias('v'))
        ids = 'ids'
    nonbank = b.modules(view).filter(~pl.col('module').is_in(['bank', 'nbfc']))
    return b.finish(p, 'v', ['period_end', concept], ids, universe=b.companies(view),
                    not_applicable=nonbank)


for _metric, _concept, _change, _higher in (
    ('advances_yoy', 'advances', True, True), ('gnpa_pct', 'gnpa_pct', False, False),
    ('nnpa_pct', 'nnpa_pct', False, False),
    ('capital_adequacy_pct', 'capital_adequacy', False, True),
):
    def _make_bank(concept=_concept, change=_change):
        return lambda view: _bank(view, concept, change)
    factor(_metric, 'quality', _higher,
           'Bank/NBFC disclosed measure; missing data is never treated as a pass')(_make_bank())


def _volume(view, metric):
    px = _recent(view).filter(~pl.col('security_id').is_in(_corporate_actions(view, 120)))
    rows = []
    for (cid,), g in px.group_by('company_id'):
        g = g.sort('trade_date').tail(61)
        required = 61 if metric == 'volume_breakout_60d' else 21
        if g.height < required or 'volume' not in g.columns:
            continue
        vol = g['volume'].to_list()
        close = g['adj_close'].to_list()
        if any(v is None or v <= 0 for v in vol[-required:]):
            continue
        # Thin/sparse trading is not a volume breakout. Require recent calendar density.
        if (g['trade_date'][-1]-g['trade_date'][-required]).days > required*2:
            continue
        baseline = sum(vol[-21:-1])/20
        ratio = vol[-1]/baseline
        value = ratio
        if metric == 'volume_breakout_60d':
            if ('turnover_inr' not in g.columns
                    or g['turnover_inr'].tail(20).count() < 20
                    or g['turnover_inr'].tail(20).median() < 1e7):
                continue
            value = float(close[-1] > max(close[:-1]) and ratio >= 1.5)
        elif metric == 'up_down_volume_20d':
            value = sum(v*(1 if c > p else -1 if c < p else 0)
                        for v, c, p in zip(vol[-20:], close[-20:], close[-21:-1], strict=True))
            value /= sum(vol[-20:])
        rows.append({'company_id': cid, 'v': value, 'relative_volume': ratio,
                     'date': g['trade_date'][-1]})
    frame = pl.DataFrame(rows, schema={'company_id': pl.Int64, 'v': pl.Float64,
                                       'relative_volume': pl.Float64, 'date': pl.Date})
    return b.finish(frame, 'v', ['date', 'relative_volume'], universe=b.companies(view))


for _metric in VOLUME:
    def _make_volume(metric=_metric):
        return lambda view: view.memo('research_'+metric, lambda: _volume(view, metric))
    factor(_metric, 'momentum', True,
           'Volume research; excluded after recent split/bonus/rights, never imputed')(
               _make_volume())
