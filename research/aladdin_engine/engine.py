"""An Aladdin-style engine: a composite score turned into weights by a linear factor risk
model and an optimizer with costs and limits, against the Rankings order (PROTOCOL.md).

Usage: uv run --no-project --with cvxpy --with polars --with numpy python -I engine.py \
         PRED FEATURES PRICES UNIVERSE INDUSTRIES_CSV,INDUSTRIES_CSV PERIOD OUT_PARQUET
PRED, FEATURES and UNIVERSE are research/ml_rating's; PRICES research/promoter's all-stock
adjusted daily prices; PERIOD dev (2016-2020) or holdout (2021 - September 2026).
"""
import datetime as dt
import math
import sys

import cvxpy as cp
import numpy as np
import polars as pl

pred_path, feat_path, prices_path, uni_path, ind_paths, period, out = sys.argv[1:8]
FIRST, LAST = {'dev': (dt.date(2016, 1, 1), dt.date(2020, 12, 31)),
               'holdout': (dt.date(2021, 1, 1), dt.date(2026, 9, 30))}[period]
IC, HALF_LIFE, WINDOW, GAP = 0.05, 63, 252, 5
COST = 0.00195                      # one way, half of 0.39% a round trip
CAP, IND_BAND = 0.05, 0.10
LAMBDAS = (10, 25, 60)
PILLARS = ['p_growth', 'p_quality', 'p_valuation', 'p_momentum', 'p_low_volatility']
STYLES = ['log_mcap', 'mom_12_1', 'volatility_1y', 'p_valuation']
TOP = 20
SHOW = 0.005                        # a holding counts from 0.5%

pred = pl.read_parquet(pred_path).select('sid', 'me', 'fwd_1m', *PILLARS)
feat = pl.read_parquet(feat_path, columns=['sid', 'me', 'log_mcap', 'mom_12_1',
                                           'volatility_1y'])
uni = pl.read_parquet(uni_path).select('sid', pl.col('day').alias('me'), 'isin', 'symbol')
x = uni.join(pred, on=['sid', 'me'], how='left').join(feat, on=['sid', 'me'], how='left')
# Unit errors in the share counts give some impossible market caps (infinite, or above any
# Indian company's): above Rs 30 lakh crore counts as missing.
x = x.with_columns(pl.when(pl.col('log_mcap').is_finite() & (pl.col('log_mcap') <= math.log(3e13)))
                   .then(pl.col('log_mcap')).alias('log_mcap'))

industry_of_isin, industry_of_symbol = {}, {}
for path in ind_paths.split(','):
    t = pl.read_csv(path)
    industry_of_isin.update(zip(t['ISIN Code'].to_list(), t['Industry'].to_list(),
                                strict=True))
    industry_of_symbol.update(zip(t['Symbol'].to_list(), t['Industry'].to_list(),
                                  strict=True))
x = x.with_columns(pl.struct('isin', 'symbol').map_elements(
    lambda r: industry_of_isin.get(r['isin']) or industry_of_symbol.get(r['symbol'])
    or 'Unclassified', return_dtype=pl.String).alias('industry'))

# Month ends whose holding month lies in the period: formed at me, held to the next me.
mes = sorted(x['me'].unique().to_list())
months = [(a, b) for a, b in zip(mes[:-1], mes[1:], strict=True) if FIRST <= b <= LAST]

# Daily returns of every stock ever in the universe, on market days.
sids = sorted(x['sid'].unique().to_list())
col = {s: j for j, s in enumerate(sids)}
px = (pl.scan_parquet(prices_path)
      .filter(pl.col('sid').is_in(sids),
              pl.col('day') >= months[0][0] - dt.timedelta(days=420),
              pl.col('day') <= months[-1][0])
      .select('sid', 'di', 'day', 'aclose').collect().sort('sid', 'di'))
px = px.with_columns(
    (pl.col('aclose') / pl.col('aclose').shift(1).over('sid') - 1).alias('r'),
    (pl.col('di') - pl.col('di').shift(1).over('sid')).alias('gap'))
px = px.filter(pl.col('r').is_not_null() & (pl.col('gap') <= GAP))
dis = sorted(px['di'].unique().to_list())
row = {d: i for i, d in enumerate(dis)}
R = np.full((len(dis), len(sids)), np.nan)
R[px['di'].replace_strict(row).to_numpy(), px['sid'].replace_strict(col).to_numpy()] = \
    px['r'].to_numpy()
di_of_day = dict(pl.scan_parquet(prices_path).select('day', 'di').unique()
                 .collect().iter_rows())


def zscore(v):
    v = np.asarray(v, float)
    ok = ~np.isnan(v)
    z = np.zeros_like(v)
    if ok.sum() > 1 and np.nanstd(v) > 0:
        z[ok] = np.clip((v[ok] - np.nanmean(v)) / np.nanstd(v), -3, 3)
    return z


def risk_model(g):
    """(X, F, D, groups): exposures, factor covariance and specific variance, a month."""
    inds = sorted(g['industry'].unique().to_list())
    X_ind = np.array([[1.0 if i == k else 0.0 for k in inds]
                      for i in g['industry'].to_list()])
    X_sty = np.column_stack([zscore(g[s].to_numpy()) for s in STYLES])
    X = np.hstack([X_ind, X_sty])
    me_di = di_of_day[g['me'][0]]
    days = [d for d in dis if me_di - WINDOW < d <= me_di]
    r = R[[row[d] for d in days]][:, [col[s] for s in g['sid'].to_list()]]
    T, n = r.shape
    f = np.full((T, X.shape[1]), np.nan)
    u = np.full((T, n), np.nan)
    for t in range(T):
        ok = ~np.isnan(r[t])
        if ok.sum() < 3 * X.shape[1]:
            continue
        coef, *_ = np.linalg.lstsq(X[ok], r[t, ok], rcond=None)
        f[t] = coef
        u[t, ok] = r[t, ok] - X[ok] @ coef
    wt = 0.5 ** ((T - 1 - np.arange(T)) / HALF_LIFE)
    good = ~np.isnan(f).any(axis=1)
    fw, ww = f[good], wt[good] / wt[good].sum()
    mean = ww @ fw
    F = ((fw - mean).T * ww) @ (fw - mean) * 21
    obs = ~np.isnan(u)
    w2 = np.where(obs, wt[:, None], 0.0)
    D = np.where(obs.sum(0) >= HALF_LIFE,
                 (np.nan_to_num(u) ** 2 * w2).sum(0) / np.maximum(w2.sum(0), 1e-12) * 21,
                 np.nan)
    ind_arr = np.array(g['industry'].to_list())
    for k in inds:
        m = ind_arr == k
        med = np.nanmedian(D[m]) if np.isfinite(D[m]).any() else np.nanmedian(D)
        D[m & np.isnan(D)] = med
    D = np.maximum(D, np.nanpercentile(D, 5))
    return X, F, D, ind_arr, inds


def optimise(alpha, X, F, D, b, w0, lam, ind_arr, inds):
    n = len(alpha)
    vals, vecs = np.linalg.eigh(F)
    L = vecs * np.sqrt(np.clip(vals, 0, None))
    w = cp.Variable(n)
    active = w - b
    risk = cp.sum_squares(L.T @ (X.T @ active)) + cp.sum_squares(cp.multiply(np.sqrt(D), active))
    cons = [w >= 0, cp.sum(w) == 1, w <= CAP]
    for k in inds:
        m = (ind_arr == k).astype(float)
        bk = float(m @ b)
        cons += [m @ w <= bk + IND_BAND, m @ w >= max(0.0, bk - IND_BAND)]
    prob = cp.Problem(cp.Maximize(alpha @ w - lam / 2 * risk - COST * cp.norm1(w - w0)),
                      cons)
    for solver in ('CLARABEL', 'OSQP', 'SCS'):
        try:
            prob.solve(solver=solver)
        except cp.error.SolverError:
            continue
        if prob.status in ('optimal', 'optimal_inaccurate') and w.value is not None:
            v = np.clip(w.value, 0, None)
            return v / v.sum(), True
    return None, False


strategies = [f'E1_{lam}' for lam in LAMBDAS] + [f'E2_{lam}' for lam in LAMBDAS] + \
    ['M', 'S', 'B_cap', 'B_eq']
held = {s: {} for s in strategies}           # sid -> weight after last month's returns
rows, failures = [], 0
for me, nxt in months:
    g = x.filter(pl.col('me') == me).sort('sid')
    sid_list = g['sid'].to_list()
    in_month = set(sid_list)
    fwd = g['fwd_1m'].fill_null(0.0).to_numpy()
    X, F, D, ind_arr, inds = risk_model(g)
    sigma = np.sqrt(np.einsum('ij,jk,ik->i', X, F, X) + D)
    pillars = g.select(PILLARS).to_numpy()
    comp = np.where((~np.isnan(pillars)).sum(1) >= 3, np.nanmean(pillars, axis=1), np.nan)
    z = {'E1': zscore(comp), 'E2': zscore(g['p_momentum'].to_numpy())}
    mcap = np.exp(g['log_mcap'].fill_null(-np.inf).to_numpy())
    b = mcap / mcap.sum()
    Sigma_te = lambda v: float(np.sqrt(max(0.0, (X.T @ v) @ F @ (X.T @ v) + (D * v * v).sum())))  # noqa: E731

    def top_equal(score):
        s = np.where(np.isnan(score), -np.inf, score)
        w = np.zeros(len(s))
        w[np.argsort(-s)[:TOP]] = 1 / TOP
        return w

    for name in strategies:
        prev = held[name]
        w0 = np.array([prev.get(s, 0.0) for s in sid_list])
        leaving = sum(v for s, v in prev.items() if s not in in_month)
        ok = True
        if name.startswith('E'):
            kind, lam = name.split('_')
            alpha = IC * sigma * z[kind]
            w, ok = optimise(alpha, X, F, D, b, w0, int(lam), ind_arr, inds)
            if not ok:
                failures += 1
                w = w0 / w0.sum() if w0.sum() > 0 else b.copy()
        elif name == 'M':
            w = top_equal(g['p_momentum'].to_numpy())
        elif name == 'S':
            w = top_equal(comp)
        elif name == 'B_cap':
            w = b.copy()
        else:
            w = np.full(len(sid_list), 1 / len(sid_list))
        traded = np.abs(w - w0).sum() + leaving
        gross = float(w @ fwd)
        cost = COST * traded
        rows.append({'strategy': name, 'me': me, 'held_to': nxt, 'gross': gross,
                     'cost': cost, 'net': gross - cost, 'turnover': traded / 2,
                     'holdings': int((w >= SHOW).sum()),
                     'buys': int(((w >= SHOW) & (w0 < SHOW)).sum()),
                     'sells': int(((w < SHOW) & (w0 >= SHOW)).sum()),
                     'te_ex_ante': Sigma_te(w - b) * math.sqrt(12),
                     'bench_cap': float(b @ fwd), 'bench_eq': float(fwd.mean()),
                     'solved': ok})
        grown = w * (1 + fwd)
        held[name] = dict(zip(sid_list, (grown / grown.sum()).tolist(), strict=True)) \
            if grown.sum() > 0 else {}
    print(me, 'done', flush=True)

res = pl.DataFrame(rows)
res.write_parquet(out)
print('solver failures', failures)
