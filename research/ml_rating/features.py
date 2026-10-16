"""Month-end inputs and forward returns for research/ml_rating/PROTOCOL.md.

Usage: uv run python -I features.py QUARTERS.parquet PRICES.parquet DELIVERY.parquet
                                     SID_SYMBOLS.parquet UNIVERSE.parquet OUT.parquet

QUARTERS: parse.py's standalone quarterly P&L lines in rupees, with each filing's first
broadcast. PRICES: research/ml's adjusted daily prices (sid, day, close, aclose, value,
med126). A month-end reading uses results broadcast before that day and prices to its
close. One row per stock (any of the universe's 1,440) and month end.
"""
import math
import sys

import polars as pl

QPATH, PPATH, DPATH, SPATH, UPATH, OUT = sys.argv[1:7]
MAX_COVERAGE = 100.0
LAGS = 24


def month_ends(prices):
    days = prices.select('day').unique().sort('day')
    return (days.with_columns(pl.col('day').dt.strftime('%Y-%m').alias('m'))
                .group_by('m').agg(pl.col('day').max()).sort('m')['day'])


# ----------------------------------------------------------------- prices
universe = pl.read_parquet(UPATH)
sids = universe['sid'].unique().implode()
px = (pl.scan_parquet(PPATH).filter((pl.col('series') == 'EQ') & pl.col('sid').is_in(sids))
        .select('sid', 'day', 'close', 'aclose', 'value', 'med126').collect()
        .unique(['sid', 'day']).sort('sid', 'day'))
MES = [d for d in month_ends(px).to_list()]
px = px.with_columns(
    (pl.col('aclose') / pl.col('aclose').shift(1).over('sid')).log().alias('lr'))
px = px.with_columns(
    (pl.col('lr').rolling_std(252, min_samples=200).over('sid') * math.sqrt(252)).alias('vol'),
    pl.col('aclose').rolling_max(252, min_samples=200).over('sid').alias('hi252'),
    pl.col('aclose').shift(21).over('sid').alias('c21'),
    pl.col('aclose').shift(126).over('sid').alias('c126'),
    pl.col('aclose').shift(252).over('sid').alias('c252'))
# Each stock's last session of each month, if within 5 days of the month's last session.
me_of = pl.DataFrame({'me': MES}).with_columns(pl.col('me').dt.strftime('%Y-%m').alias('m'))
px = (px.with_columns(pl.col('day').dt.strftime('%Y-%m').alias('m'))
        .join(me_of, on='m'))
last_in_month = (px.group_by('sid', 'm').agg(pl.col('day').max())
                   .join(me_of, on='m')
                   .filter(pl.col('day') >= pl.col('me').dt.offset_by('-5d')))
at_me = px.join(last_in_month.select('sid', 'day'), on=['sid', 'day']).select(
    'sid', 'me', 'close', 'aclose', 'med126', 'vol', 'hi252',
    (pl.col('aclose') / pl.col('c126') - 1).alias('ret126'),
    (pl.col('aclose') / pl.col('c252') - 1).alias('ret252'),
    (pl.col('c21') / pl.col('c252') - 1).alias('mom_12_1'),
    (pl.col('aclose') / pl.col('c21') - 1).alias('ret_1m'))

# Forward returns: to the close of the next (and 12th) month end, or the last close
# before it when the stock stopped trading (delisting), from this month end's close.
me_df = pl.DataFrame({'me': MES}).with_row_index('k')
last_close = px.select('sid', 'day', 'aclose').sort('day')
fwd = []
for h, name in ((1, 'fwd_1m'), (12, 'fwd_12m')):
    tgt = me_df.with_columns(pl.col('me').shift(-h).alias('to')).drop_nulls('to')
    a = at_me.select('sid', 'me', 'aclose').join(tgt.select('me', 'to'), on='me')
    a = a.sort('to').join_asof(last_close.rename({'day': 'last_day', 'aclose': 'end'}),
                                left_on='to', right_on='last_day', by='sid',
                                strategy='backward')
    a = a.filter(pl.col('last_day') >= pl.col('me'))
    fwd.append(a.select('sid', 'me', (pl.col('end') / pl.col('aclose') - 1).alias(name)))
at_me = at_me.join(fwd[0], on=['sid', 'me'], how='left').join(fwd[1], on=['sid', 'me'],
                                                                how='left')

# Delivery %: 20 sessions against a year.
sym = pl.read_parquet(SPATH)
dl = pl.scan_parquet(DPATH).filter(pl.col('series') == 'EQ').select('day', 'symbol',
                                                                       'deliv_pct').collect()
dl = (dl.join(sym, on='symbol').filter(pl.col('day').is_between(pl.col('first'), pl.col('last')))
        .select('sid', 'day', 'deliv_pct').unique(['sid', 'day']).sort('sid', 'day')
        .with_columns((pl.col('deliv_pct').rolling_mean(20, min_samples=15).over('sid')
                       / pl.col('deliv_pct').rolling_mean(252, min_samples=150).over('sid'))
                      .alias('deliv_ratio')))
at_me = at_me.join(dl.select('sid', pl.col('day').alias('me'), 'deliv_ratio'),
                   on=['sid', 'me'], how='left')

# ----------------------------------------------------------------- results
q = pl.read_parquet(QPATH).sort('broadcast').unique(['sid', 'period_end'], keep='first')
# A quarter whose revenue is over 20 times off its neighbours' median (a misread unit)
# loses its amounts.
MONEY = ['revenue', 'other_income', 'finance_costs', 'depreciation', 'pbt_pre_exc', 'pbt',
         'pat', 'paid_up', 'exceptional']
q = q.sort('sid', 'period_end').with_columns(
    pl.concat_list([pl.col('revenue').shift(k).over('sid') for k in (-2, -1, 1, 2)])
    .list.drop_nulls().list.median().alias('rev_near'))
odd = (pl.col('revenue') / pl.col('rev_near') > 20) | (pl.col('revenue') / pl.col('rev_near') < 0.05)
print('quarters dropped as a misread unit:', q.filter(odd).height, 'of', q.height)
q = q.with_columns([pl.when(odd).then(None).otherwise(pl.col(c)).alias(c) for c in MONEY])
q = q.with_columns(
    (pl.col('period_end').dt.year() * 4 + (pl.col('period_end').dt.month() - 1) // 3)
    .alias('qidx'),
    pl.col('broadcast').dt.date().alias('known'),
    pl.col('other_income').fill_null(0.0))
nonbank = ~pl.col('bank').fill_null(False)
q = q.with_columns(
    pl.when(nonbank).then(pl.col('pbt_pre_exc') + pl.col('finance_costs')
                          + pl.col('depreciation') - pl.col('other_income')).alias('ebitda'),
    pl.when(nonbank).then(pl.col('pbt_pre_exc') + pl.col('finance_costs')).alias('ebit'),
    pl.when(nonbank).then(pl.col('finance_costs')).alias('fc'),
    (pl.col('paid_up') / pl.col('face_value')).alias('shares'))

# The latest quarter known at each month end (results first broadcast before that day).
grid = at_me.select('sid', 'me')
known = q.select('sid', 'known', 'qidx').sort('known').with_columns(
    pl.col('qidx').cum_max().over('sid').alias('last_q'))
grid = grid.sort('me').join_asof(known.select('sid', 'known', 'last_q'), left_on='me',
                                 right_on='known', by='sid', strategy='backward',
                                 allow_exact_matches=False)
grid = grid.rename({'known': 'last_known'})
grid = grid.drop_nulls('last_q')
vals = q.select('sid', 'qidx', 'known', 'revenue', 'pat', 'ebitda', 'ebit', 'fc', 'shares')
f = grid
for lag in range(LAGS):
    j = (grid.select('sid', 'me', (pl.col('last_q') - lag).alias('qidx'))
             .join(vals, on=['sid', 'qidx'])
             .filter(pl.col('known') < pl.col('me'))
             .select('sid', 'me', *[pl.col(c).alias(f'{c}_{lag}') for c in
                                    ('revenue', 'pat', 'ebitda', 'ebit', 'fc', 'shares')]))
    f = f.join(j, on=['sid', 'me'], how='left')


def ttm(col, start):
    cols = [f'{col}_{k}' for k in range(start, start + 4)]
    return pl.when(pl.all_horizontal([pl.col(c).is_not_null() for c in cols])) \
             .then(pl.sum_horizontal(cols))


f = f.with_columns(
    [ttm(m, s_).alias(f'{m}_t{s_}') for m in ('revenue', 'pat', 'ebitda', 'ebit', 'fc')
     for s_ in (0, 4, 12, 20)]
    + [pl.col(f'revenue_{k}').alias(f'rev_{k}') for k in range(16)]
    + [pl.col(f'ebitda_{k}').alias(f'ebd_{k}') for k in range(8)]
    + [pl.coalesce([pl.when(pl.col(f'shares_{k}') > 0).then(pl.col(f'shares_{k}'))
                    for k in range(LAGS)]).alias('shares')])


def cagr(now, base, years):
    return (pl.when((pl.col(now) > 0) & (pl.col(base) > 0))
              .then((pl.col(now) / pl.col(base)) ** (1 / years) - 1)
              .when(pl.col(now) <= 0).then(-1.0))       # a loss now: the worst


def change(now, base):
    return pl.when(pl.col(base) > 0).then(pl.col(now) / pl.col(base) - 1)


yoy = [change(f'rev_{k}', f'rev_{k + 4}').alias(f'y{k}') for k in range(12)]
f = f.with_columns(yoy)
f = f.with_columns(
    change('revenue_t0', 'revenue_t4').alias('revenue_ttm_yoy'),
    pl.when(pl.col('pat_t4') > 0).then((pl.col('pat_t0') - pl.col('pat_t4'))
                                       / pl.col('pat_t4')).alias('pat_ttm_yoy'),
    cagr('revenue_t0', 'revenue_t12', 3).alias('revenue_cagr_3y'),
    cagr('revenue_t0', 'revenue_t20', 5).alias('revenue_cagr_5y'),
    cagr('ebitda_t0', 'ebitda_t12', 3).alias('ebitda_cagr_3y'),
    cagr('ebitda_t0', 'ebitda_t20', 5).alias('ebitda_cagr_5y'),
    cagr('pat_t0', 'pat_t12', 3).alias('pat_cagr_3y'),
    cagr('pat_t0', 'pat_t20', 5).alias('pat_cagr_5y'),
    pl.col('y0').alias('revenue_quarter_yoy'),
    pl.when(pl.col('pat_4') != 0).then((pl.col('pat_0') - pl.col('pat_4'))
                                       / pl.col('pat_4').abs()).alias('pat_quarter_yoy'),
    pl.when((pl.col('rev_0') > 0) & (pl.col('rev_4') > 0))
      .then(pl.col('ebd_0') / pl.col('rev_0') - pl.col('ebd_4') / pl.col('rev_4'))
      .alias('opm_quarter_yoy'),
    pl.when(pl.concat_list([f'y{k}' for k in range(8)]).list.drop_nulls().list.len() == 8)
      .then(pl.mean_horizontal([f'y{k}' for k in range(4)])
            - pl.mean_horizontal([f'y{k}' for k in range(4, 8)]))
      .alias('growth_acceleration_4q'),
    pl.when(pl.concat_list([f'y{k}' for k in range(12)]).list.drop_nulls().list.len() == 12)
      .then(pl.sum_horizontal([(pl.col(f'y{k}') > 0).cast(pl.Float64) for k in range(12)]))
      .alias('growth_consistency_12q'),
    pl.when(pl.col('revenue_t0') > 0).then(pl.col('ebitda_t0') / pl.col('revenue_t0'))
      .alias('opm_level'),
    pl.when(pl.col('fc_t0') <= 0).then(MAX_COVERAGE)
      .otherwise(pl.min_horizontal(pl.col('ebit_t0') / pl.col('fc_t0'), pl.lit(MAX_COVERAGE)))
      .alias('interest_coverage'))

# OPM trend: slope of quarterly margin over the last 8 quarters (all 8 needed).
opm = [pl.when(pl.col(f'rev_{k}') > 0).then(pl.col(f'ebd_{k}') / pl.col(f'rev_{k}'))
       for k in range(8)]
f = f.with_columns([o.alias(f'o{k}') for k, o in enumerate(opm)])
t = [-k + 3.5 for k in range(8)]                     # time, centred
f = f.with_columns(
    pl.when(pl.concat_list([f'o{k}' for k in range(8)]).list.drop_nulls().list.len() == 8)
      .then(pl.sum_horizontal([pl.col(f'o{k}') * t[k] for k in range(8)])
            / sum(x * x for x in t)).alias('opm_trend_8q'))

# Profit surprise: the latest quarter's YoY change against the 8 before (at least 4).
d = [pl.col(f'pat_{k}') - pl.col(f'pat_{k + 4}') for k in range(9)]
f = f.with_columns([x.alias(f'd{k}') for k, x in enumerate(d)])
prior = pl.concat_list([f'd{k}' for k in range(1, 9)]).list.drop_nulls()
f = f.with_columns(
    pl.when((prior.list.len() >= 4) & (prior.list.std() > 0))
      .then((pl.col('d0') - prior.list.mean()) / prior.list.std()).alias('sue'))

# ----------------------------------------------------------------- together
x = at_me.join(f, on=['sid', 'me'], how='left')
x = x.with_columns((pl.col('close') * pl.col('shares')).alias('mcap'))
x = x.with_columns(
    pl.when(pl.col('pat_t0') > 0).then(pl.col('mcap') / pl.col('pat_t0')).alias('pe'),
    (pl.col('pat_t0') <= 0).alias('pe_unfav'),
    pl.when(pl.col('ebitda_t0') > 0).then(pl.col('mcap') / pl.col('ebitda_t0'))
      .alias('mcap_ebitda'),
    (pl.col('ebitda_t0') <= 0).alias('mcap_ebitda_unfav'),
    (pl.col('ret126') / pl.col('vol')).alias('risk_adj_return_6m'),
    (pl.col('ret252') / pl.col('vol')).alias('risk_adj_return_12m'),
    pl.col('vol').alias('volatility_1y'),
    (pl.col('aclose') / pl.col('hi252') - 1).alias('dist_52w_high'),
    pl.col('mcap').log().alias('log_mcap'),
    pl.col('med126').log().alias('log_value'),
    ((pl.col('me') - pl.col('last_known')).dt.total_days()).alias('days_since_results'))

# P/E against the stock's own month-end P/Es of the previous 5 years (at least 24).
pe = x.select('sid', 'me', 'pe').sort('sid', 'me')
hist = (pe.join(pe.rename({'me': 'me_h', 'pe': 'pe_h'}), on='sid')
          .filter((pl.col('me_h') < pl.col('me'))
                  & (pl.col('me_h') >= pl.col('me').dt.offset_by('-5y'))
                  & pl.col('pe_h').is_not_null())
          .group_by('sid', 'me').agg(pl.col('pe_h').median().alias('pe_med'),
                                     pl.len().alias('pe_n')))
x = x.join(hist, on=['sid', 'me'], how='left').with_columns(
    pl.when(pl.col('pe_n') >= 24).then(pl.col('pe') / pl.col('pe_med'))
      .alias('pe_vs_own_5y_median'),
    pl.when((pl.col('pe') > 0) & (pl.col('pat_cagr_3y') > 0))
      .then(pl.col('pe') / (pl.col('pat_cagr_3y') * 100)).alias('peg_trailing'),
    ((pl.col('pat_t0') <= 0) | (pl.col('pat_cagr_3y') <= 0)).alias('peg_unfav'))

keep = ['sid', 'me', 'fwd_1m', 'fwd_12m', 'med126', 'revenue_cagr_3y', 'revenue_cagr_5y',
        'ebitda_cagr_3y', 'ebitda_cagr_5y', 'pat_cagr_3y', 'pat_cagr_5y', 'revenue_ttm_yoy',
        'pat_ttm_yoy', 'growth_acceleration_4q', 'growth_consistency_12q',
        'revenue_quarter_yoy', 'pat_quarter_yoy', 'opm_quarter_yoy', 'opm_level',
        'opm_trend_8q', 'interest_coverage', 'pe_vs_own_5y_median', 'pe_unfav',
        'peg_trailing', 'peg_unfav', 'mcap_ebitda', 'mcap_ebitda_unfav',
        'risk_adj_return_6m', 'risk_adj_return_12m', 'volatility_1y', 'mom_12_1', 'sue',
        'days_since_results', 'ret_1m', 'dist_52w_high', 'log_mcap', 'log_value',
        'deliv_ratio']
x = x.select(keep).with_columns(pl.col(pl.Float64).fill_nan(None))
x.write_parquet(OUT)
u = x.join(universe.select('sid', pl.col('day').alias('me')), on=['sid', 'me'])
print('rows', x.height, 'universe rows', u.height)
with pl.Config(tbl_cols=40, tbl_width_chars=250):
    print(u.group_by(pl.col('me').dt.year().alias('year')).agg(
        pl.len(), *[pl.col(c).is_not_null().mean().round(2)
                    for c in ('revenue_ttm_yoy', 'revenue_cagr_3y', 'revenue_cagr_5y',
                              'opm_level', 'pe_vs_own_5y_median', 'sue', 'mom_12_1',
                              'deliv_ratio', 'fwd_1m', 'fwd_12m')]).sort('year'))
