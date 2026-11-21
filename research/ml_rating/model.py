"""Walk-forward ratings and portfolios for research/ml_rating/PROTOCOL.md.

Usage (uv run --with lightgbm --with scikit-learn python -I model.py ...):
  model.py FEATURES UNIVERSE predict PRED     ratings for every month from 2015
  model.py PRED dev                           development 2016-2020, both models
  model.py PRED holdout MODEL_1M MODEL_12M    holdout 2021-2026, run once
"""
import datetime as dt
import math
import sys

import numpy as np
import polars as pl

COST = 0.0039                       # a round trip, delivery (research/ml)
TOP_TRADE, TOP_INVEST = 20, 50
DEV = (dt.date(2016, 1, 1), dt.date(2020, 12, 31))
HOLD = (dt.date(2021, 1, 1), dt.date(2026, 9, 30))

GROWTH = {'revenue_cagr_3y': 1, 'revenue_cagr_5y': 1, 'ebitda_cagr_3y': 1,
          'ebitda_cagr_5y': 1, 'pat_cagr_3y': 1, 'pat_cagr_5y': 1, 'revenue_ttm_yoy': 1,
          'pat_ttm_yoy': 1, 'growth_acceleration_4q': 1, 'growth_consistency_12q': 1}
QUALITY = {'opm_level': 1, 'opm_trend_8q': 1, 'interest_coverage': 1}
VALUATION = {'pe_vs_own_5y_median': 1, 'peg_trailing': 1, 'mcap_ebitda': 1}
MOMENTUM = {'risk_adj_return_6m': 1, 'risk_adj_return_12m': 1}
LOWVOL = {'volatility_1y': 1}
# The app's pillar weights without ownership (config/scoring.yaml), and direction:
# +1 higher is better, -1 lower is better.
PILLARS = {'growth': (0.15, GROWTH, 1), 'quality': (0.20, QUALITY, 1),
           'valuation': (0.20, VALUATION, -1), 'momentum': (0.20, MOMENTUM, 1),
           'low_volatility': (0.20, LOWVOL, -1)}
UNFAV = {'pe_vs_own_5y_median': 'pe_unfav', 'peg_trailing': 'peg_unfav',
         'mcap_ebitda': 'mcap_ebitda_unfav'}
EXTRA = ['sue', 'days_since_results', 'ret_1m', 'dist_52w_high', 'log_mcap', 'log_value',
         'deliv_ratio', 'mom_12_1', 'revenue_quarter_yoy', 'pat_quarter_yoy',
         'opm_quarter_yoy']
FACTORS = [f for _, fs, _ in PILLARS.values() for f in fs]
INPUTS = FACTORS + EXTRA


def pct(col):
    """Percentile rank within the month, 0 to 1; nulls stay null."""
    r = pl.col(col).rank('average').over('me')
    n = pl.col(col).count().over('me')
    return pl.when(n > 1).then((r - 1) / (n - 1))


def prepare(features, universe):
    x = (pl.read_parquet(features)
           .join(pl.read_parquet(universe).select('sid', pl.col('day').alias('me')),
                 on=['sid', 'me']))
    # Unfavourable valuations (a loss, shrinking profit) rank with the dearest.
    x = x.with_columns([pl.when(pl.col(u)).then(1e12).otherwise(pl.col(f)).alias(f)
                        for f, u in UNFAV.items()])
    x = x.with_columns([pct(c).alias(f'r_{c}') for c in INPUTS]
                       + [pct('fwd_1m').alias('t_1m'), pct('fwd_12m').alias('t_12m')])
    # The rule score: each factor's rank in its better direction, pillars weighted.
    pillar_cols = []
    for name, (w, fs, sign) in PILLARS.items():
        parts = [(pl.col(f'r_{f}') if sign == 1 else 1 - pl.col(f'r_{f}')) * wf
                 for f, wf in fs.items()]
        have = [pl.col(f'r_{f}').is_not_null().cast(pl.Float64) * wf for f, wf in fs.items()]
        pillar_cols.append(pl.when(pl.sum_horizontal(have) > 0)
                           .then(pl.sum_horizontal(parts) / pl.sum_horizontal(have))
                           .alias(f'p_{name}'))
    x = x.with_columns(pillar_cols)
    num = pl.sum_horizontal([pl.col(f'p_{n}').fill_null(0) * w for n, (w, _, _) in PILLARS.items()])
    den = pl.sum_horizontal([pl.col(f'p_{n}').is_not_null().cast(pl.Float64) * w
                             for n, (w, _, _) in PILLARS.items()])
    return x.with_columns(pl.when(den > 0).then(num / den).alias('rule'),
                          pl.col('mom_12_1').alias('momentum'))


def fit_predict(train, test, target):
    import lightgbm as lgb
    from sklearn.linear_model import Ridge
    cols = [f'r_{c}' for c in INPUTS]
    train = train.filter(pl.col(target).is_not_null())
    X = train.select(cols).to_numpy().astype(float)
    y = train[target].to_numpy()
    Xt = test.select(cols).to_numpy().astype(float)
    gbm = lgb.LGBMRegressor(n_estimators=500, learning_rate=0.03, num_leaves=31,
                            min_child_samples=200, subsample=0.8, subsample_freq=1,
                            colsample_bytree=0.8, random_state=7, verbose=-1)
    gbm.fit(X, y)
    ridge = Ridge(alpha=1.0).fit(np.nan_to_num(X, nan=0.5), y)
    return gbm.predict(Xt), ridge.predict(np.nan_to_num(Xt, nan=0.5)), gbm


def predict(features, universe, out):
    x = prepare(features, universe).sort('me', 'sid')
    mes = sorted(x['me'].unique().to_list())
    k_of = {m: k for k, m in enumerate(mes)}
    x = x.with_columns(pl.col('me').replace_strict(k_of).alias('k'))
    parts, importance = [], {}
    for year in range(2015, 2027):
        start = dt.date(year, 1, 1)
        test = x.filter(pl.col('me').dt.year() == year)
        if test.is_empty():
            continue
        got = {}
        for h, target in ((1, 't_1m'), (12, 't_12m')):
            # Training months whose horizon ended before the year began.
            ends = [mes[k + h] if k + h < len(mes) else None for k in range(len(mes))]
            ok = [k for k, e in enumerate(ends) if e is not None and e < start]
            train = x.filter(pl.col('k').is_in(ok))
            g, r, gbm = fit_predict(train, test, target)
            got[f'gbm_{h}m'], got[f'ridge_{h}m'] = g, r
            importance[(year, h)] = dict(zip(INPUTS, gbm.booster_.feature_importance('gain'),
                                             strict=True))
            print(year, f'{h}m', 'train', train.height, 'test', test.height, flush=True)
        parts.append(test.with_columns([pl.Series(k, v) for k, v in got.items()]))
    pred = pl.concat(parts)
    pred.select('sid', 'me', 'fwd_1m', 'fwd_12m', 'rule', 'momentum', 'gbm_1m', 'ridge_1m',
                'gbm_12m', 'ridge_12m', *[f'p_{n}' for n in PILLARS]).write_parquet(out)
    # Every stock's monthly returns, in the universe or not: a cohort keeps its names.
    pl.read_parquet(features).select('sid', 'me', 'fwd_1m').write_parquet(rets_path(out))
    last = max(importance)
    print('\nLightGBM gain, the', last[0], 'model:')
    for h in (1, 12):
        imp = importance[(last[0], h)]
        tot = sum(imp.values())
        top = sorted(imp.items(), key=lambda kv: -kv[1])[:10]
        print(f'  {h}m: ' + ', '.join(f'{k} {v / tot:.0%}' for k, v in top))


def trading(pred, rating, lo, hi):
    """Monthly excess return after costs of the top 20, held a month."""
    p = pred.filter(pl.col('me').is_between(lo - dt.timedelta(days=40), hi)
                    & pl.col('fwd_1m').is_not_null())
    rows, held = [], set()
    for (me,), g in p.sort('me').group_by(['me'], maintain_order=True):
        top = g.filter(pl.col(rating).is_not_null()).sort(rating, descending=True).head(TOP_TRADE)
        names = set(top['sid'].to_list())
        turnover = len(names - held) / TOP_TRADE
        held = names
        month = (me + dt.timedelta(days=5))       # the month the return is earned in
        if not (lo <= month.replace(day=1) <= hi) or me >= hi:
            continue
        rows.append({'me': me, 'gross': top['fwd_1m'].mean() - g['fwd_1m'].mean(),
                     'net': top['fwd_1m'].mean() - g['fwd_1m'].mean() - COST * turnover})
    return pl.DataFrame(rows)


def investing(pred, all_rets, rating, lo, hi):
    """Monthly excess return after costs of the last 12 cohorts of the top 50."""
    p = pred.filter(pl.col(rating).is_not_null())
    cohorts = {me: set(g.sort(rating, descending=True).head(TOP_INVEST)['sid'].to_list())
               for (me,), g in p.group_by(['me'])}
    rets = {me: dict(zip(g['sid'].to_list(), g['fwd_1m'].fill_null(0.0).to_list(),
                         strict=True)) for (me,), g in all_rets.group_by(['me'])}
    uni = {me: g['fwd_1m'].mean() for (me,), g in
           pred.filter(pl.col('fwd_1m').is_not_null()).group_by(['me'])}
    mes = sorted(uni)
    rows = []
    for i, me in enumerate(mes):
        month = me + dt.timedelta(days=5)
        if not (lo <= month.replace(day=1) <= hi) or me >= hi or i < 11:
            continue
        active = [cohorts[m] for m in mes[i - 11:i + 1] if m in cohorts]
        if len(active) < 12:
            continue
        r = rets.get(me, {})
        port = sum(sum(r.get(s, 0.0) for s in c) / len(c) for c in active) / len(active)
        rows.append({'me': me, 'gross': port - uni[me], 'net': port - uni[me] - COST / 12})
    return pl.DataFrame(rows)


def stats(df):
    x = df['net'].to_numpy()
    yrs = (df.with_columns((pl.col('me') + pl.duration(days=5)).dt.year().alias('y'))
             .group_by('y').agg(pl.col('net').mean()).sort('y'))
    return {'months': len(x), 'gross': float(df['gross'].mean()), 'net': float(x.mean()),
            't': float(x.mean() / x.std(ddof=1) * math.sqrt(len(x))),
            'years': dict(zip(yrs['y'].to_list(), yrs['net'].to_list(), strict=True))}


def show(name, s):
    yrs = '  '.join(f'{y}: {v * 100:+.2f}' for y, v in s['years'].items())
    print(f"{name:22s} {s['months']:3d} months  gross {s['gross'] * 100:+.2f}%  "
          f"net {s['net'] * 100:+.2f}% a month  t {s['t']:5.2f}\n{'':23s}{yrs}")


def rets_path(pred_path):
    return pred_path.replace('.parquet', '_rets.parquet')


def run(pred_path, lo, hi, which):
    pred = pl.read_parquet(pred_path)
    all_rets = pl.read_parquet(rets_path(pred_path))
    out = {}
    print(f'Trading: top {TOP_TRADE} held a month, excess over the universe after costs')
    for name in which[1]:
        out[('1m', name)] = s = stats(trading(pred, name, lo, hi))
        show(name, s)
    print(f'\nInvesting: the last 12 monthly cohorts of the top {TOP_INVEST}, excess over the '
          'universe after costs')
    for name in which[12]:
        out[('12m', name)] = s = stats(investing(pred, all_rets, name, lo, hi))
        show(name, s)
    ic = (pred.filter(pl.col('me').is_between(lo, hi))
              .group_by('me').agg(*[pl.corr(c, 'fwd_1m', method='spearman').alias(c)
                                    for c in which[1]]))
    print('\nMean monthly rank correlation with the next month: '
          + ', '.join(f'{c} {ic[c].mean():+.3f}' for c in which[1]))
    return out


if __name__ == '__main__':
    mode = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] == 'predict' else sys.argv[2]
    if mode == 'predict':
        predict(sys.argv[1], sys.argv[2], sys.argv[4])
    elif mode == 'dev':
        print(f'Development: months held {DEV[0]} to {DEV[1]}\n')
        out = run(sys.argv[1], *DEV, {1: ['gbm_1m', 'ridge_1m', 'rule', 'momentum'],
                                      12: ['gbm_12m', 'ridge_12m', 'rule', 'momentum']})
        for h in ('1m', '12m'):
            cands = [(out[(h, m)]['t'], m) for m in (f'gbm_{h}', f'ridge_{h}')
                     if out[(h, m)]['net'] > 0]
            print(f'Chosen for {h}:', max(cands)[1] if cands else 'none')
    elif mode == 'holdout':
        m1, m12 = sys.argv[3], sys.argv[4]
        print(f'Holdout: months held {HOLD[0]} to {HOLD[1]}, run once\n')
        out = run(sys.argv[1], *HOLD, {1: [m1, 'rule', 'momentum'],
                                       12: [m12, 'rule', 'momentum']})
        for h, m, years in (('1m', m1, range(2021, 2027)), ('12m', m12, range(2021, 2026))):
            s = out[(h, m)]
            need = 4 if h == '1m' else 3
            ok = (s['net'] > 0 and s['t'] >= 2
                  and s['net'] > out[(h, 'rule')]['net'] and s['net'] > out[(h, 'momentum')]['net']
                  and sum(s['years'].get(y, 0) > 0 for y in years) >= need)
            print(f'{h}: {m}', 'PASSED' if ok else 'NOT PASSED')
