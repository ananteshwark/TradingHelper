"""Study A, Part 2: models trained on the drivers of adverse moves (PROTOCOL.md).

Usage:
  uv run python -I models_a.py PATHS_DIR dev
  uv run python -I models_a.py PATHS_DIR holdout SET=A1,A2[,AB] ...   e.g. m0945=V33,E0

PATHS_DIR is where paths.py wrote <set>_trades.parquet and <set>_checks.parquet.
`dev` fits, evaluates and reports every candidate on July 2023 - 2024, and reads nothing
after 2024. `holdout` runs only the candidates CHOICE.md names, once, on 2025 - 23 October
2026.

Fixed here before any of it was run:
- A1's parameters are the decision-time ones: every input of the model that made the
  pick (signed as paths.py signs them), the prediction signed for the side
  (`conviction`), its rank, the side, the day of the week, the Nifty's inputs at the
  last candle before the entry and breadth, both signed. The stock's own gap and volume
  before the entry (`own_*`) are not among them: the protocol doesn't list them.
- A2 is one model across the five checkpoints, with the minutes since entry among its
  parameters, beside A1's and the path's.
- A trade A2 closes early takes the checkpoint row's `early_pnl`; it is closed at the
  first checkpoint where the rule says so.
- The t-statistic of a daily difference counts every day with predictions in the period,
  zero on a day with no picks.
- SHAP: LightGBM's own contributions (pred_contrib) on the period's picks, from the
  model fitted for each quarter; the direction is the sign of the correlation between a
  parameter and its contribution.
"""
import datetime as dt
import math
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

paths, mode = Path(sys.argv[1]), sys.argv[2]
DEV = (dt.date(2023, 7, 1), dt.date(2024, 12, 31))
HOLDOUT = (dt.date(2025, 1, 1), dt.date(2026, 10, 23))
FIRST = dt.date(2023, 1, 1)
lo, hi = DEV if mode == 'dev' else HOLDOUT
SEEDS = (7, 8, 9, 10) if mode == 'dev' else (7,)
CLIP = 0.03
SETS = ('m0945', 'evening')
PATH_COLS = ['minutes', 'move', 'move_atr', 'best_atr', 'worst_atr', 'last15', 'rvol_since',
             'rvol_15', 'vwap_gap', 'range_pos', 'nifty_move', 'rel_move']
A1_NAMES, A2_NAMES = ('V20', 'V33', 'VR'), ('E0', 'E10', 'C50', 'C60')

chosen = {}
if mode == 'holdout':
    for arg in sys.argv[3:]:
        name, cands = arg.split('=')
        chosen[name] = cands.split(',')


def params(seed, leaf):
    return dict(n_estimators=200, learning_rate=0.03, num_leaves=15, min_child_samples=leaf,
                colsample_bytree=0.8, subsample=0.8, subsample_freq=1, random_state=seed,
                verbose=-1)


def quarters(a, b):
    q = dt.date(a.year, (a.month - 1) // 3 * 3 + 1, 1)
    out = []
    while q <= b:
        nxt = dt.date(q.year + (q.month + 2) // 12, (q.month + 2) % 12 + 1, 1)
        out.append((q, min(nxt - dt.timedelta(days=1), b)))
        q = nxt
    return out


def decision_cols(t):
    signed = [c for c in t.columns if c.startswith('s_') and not c.startswith('s_own_')]
    scale = [c for c in ('range30_atr', 'rvol30', 'atr_pct', 'log_value20', 'rvol_last30')
             if c in t.columns]
    return signed + scale + ['conviction', 'rank', 'side', 'dow']


def stats(day_list, taken, f_daily):
    """Summary of one variant: `taken` has day, pnl, net, stopped, early."""
    daily = (pl.DataFrame({'day': day_list})
             .join(taken.group_by('day').agg(pl.col('pnl').sum()), on='day', how='left')
             .fill_null(0)['pnl'].to_numpy())
    diff = daily - f_daily
    eq = np.cumsum(daily)
    gains = taken.filter(pl.col('pnl') > 0)['pnl'].sum()
    losses = -taken.filter(pl.col('pnl') < 0)['pnl'].sum()
    years = np.array([d.year for d in day_list])
    by_year = {int(y): round(float(diff[years == y].mean()), 1) for y in sorted(set(years))}
    sd = diff.std(ddof=1)
    return {'trades': taken.height,
            'stop_rate': round(taken['stopped'].mean(), 3) if taken.height else None,
            'early': round(taken['early'].mean(), 3) if taken.height else None,
            'net_%': round(taken['net'].mean() * 100, 3) if taken.height else None,
            'PF': round(gains / losses, 2) if losses else None,
            'Rs_day': round(float(daily.mean())),
            'diff_day': round(float(diff.mean()), 1),
            't_diff': round(float(diff.mean() / sd * math.sqrt(len(diff))), 2) if sd > 0
            else None,
            'max_dd': round(float((eq - np.maximum.accumulate(eq)).min())),
            'worst_day': round(float(daily.min())),
            'diff_by_year': by_year}


def run(name):
    trades = pl.read_parquet(paths / f'{name}_trades.parquet').filter(
        pl.col('day').is_between(FIRST, hi))
    checks = pl.read_parquet(paths / f'{name}_checks.parquet').filter(
        pl.col('day').is_between(FIRST, hi))
    feats = decision_cols(trades)
    checks = checks.join(trades.select('symbol', 'day', 'side',
                                       *[f for f in feats if f != 'side']),
                         on=['symbol', 'day', 'side'], how='inner')
    a2_feats = feats + PATH_COLS
    trades = trades.with_columns(pl.col('net').clip(-CLIP, CLIP).alias('y_net'))
    checks = checks.with_columns(pl.col('rest').clip(-CLIP, CLIP).alias('y_rest'))
    day_list = sorted(trades.filter(pl.col('day').is_between(lo, hi))['day'].unique())
    picks = trades.filter(pl.col('pick') & pl.col('day').is_between(lo, hi))
    f_taken = picks.select('day', 'pnl', 'net', 'stopped', pl.lit(False).alias('early'))
    f_daily = (pl.DataFrame({'day': day_list})
               .join(f_taken.group_by('day').agg(pl.col('pnl').sum()), on='day', how='left')
               .fill_null(0)['pnl'].to_numpy())
    want_a1 = A1_NAMES if mode == 'dev' else [c for c in chosen.get(name, []) if c in A1_NAMES]
    want_a2 = A2_NAMES if mode == 'dev' else [c for c in chosen.get(name, []) if c in A2_NAMES]
    results, shap = {}, {}
    for seed in SEEDS:
        veto = {c: set() for c in A1_NAMES}
        exits = {c: {} for c in A2_NAMES}          # (symbol, day, side) -> (pnl, net)
        contribs, contrib_x = [], []
        for q0, q1 in quarters(lo, hi):
            train = trades.filter(pl.col('day') < q0)
            test = picks.filter(pl.col('day').is_between(q0, q1))
            if test.is_empty():
                continue
            X, Xt = train.select(feats).to_numpy(), test.select(feats).to_numpy()
            keys = list(zip(test['symbol'], test['day'], test['side'], strict=True))
            if want_a1:
                clf = lgb.LGBMClassifier(**params(seed, 200)).fit(X, train['stopped'].to_numpy())
                p_in = clf.predict_proba(X)[:, 1]
                p = clf.predict_proba(Xt)[:, 1]
                for cand, pct in (('V20', 80), ('V33', 67)):
                    cut = np.percentile(p_in, pct)
                    veto[cand] |= {k for k, v in zip(keys, p, strict=True) if v > cut}
                reg = lgb.LGBMRegressor(**params(seed, 200)).fit(X, train['y_net'].to_numpy())
                veto['VR'] |= {k for k, v in zip(keys, reg.predict(Xt), strict=True) if v < 0}
                if seed == 7:
                    contribs.append(clf.booster_.predict(Xt, pred_contrib=True)[:, :-1])
                    contrib_x.append(Xt)
            if want_a2:
                ctrain = checks.filter(pl.col('day') < q0)
                ctest = checks.filter(pl.col('day').is_between(q0, q1)).join(
                    test.select('symbol', 'day', 'side'), on=['symbol', 'day', 'side'])
                if ctest.is_empty():
                    continue
                CX, CXt = ctrain.select(a2_feats).to_numpy(), ctest.select(a2_feats).to_numpy()
                reg2 = lgb.LGBMRegressor(**params(seed, 200)).fit(
                    CX, ctrain['y_rest'].to_numpy())
                clf2 = lgb.LGBMClassifier(**params(seed, 200)).fit(
                    CX, ctrain['stop_later'].to_numpy())
                ctest = ctest.with_columns(pl.Series('r_hat', reg2.predict(CXt)),
                                           pl.Series('p_hat', clf2.predict_proba(CXt)[:, 1]))
                rules = {'E0': pl.col('r_hat') < 0, 'E10': pl.col('r_hat') < -0.001,
                         'C50': pl.col('p_hat') >= 0.5, 'C60': pl.col('p_hat') >= 0.6}
                for cand, rule in rules.items():
                    first = (ctest.filter(rule).sort('check')
                             .group_by('symbol', 'day', 'side', maintain_order=True).first())
                    for r in first.iter_rows(named=True):
                        exits[cand][(r['symbol'], r['day'], r['side'])] = (
                            r['early_pnl'], r['early_net'])

        def variant(v_set, e_map):
            rows = []
            for r in picks.iter_rows(named=True):
                k = (r['symbol'], r['day'], r['side'])
                if k in v_set:
                    continue
                if k in e_map:
                    pnl, net = e_map[k]
                    rows.append((r['day'], pnl, net, False, True))
                else:
                    rows.append((r['day'], r['pnl'], r['net'], r['stopped'], False))
            return pl.DataFrame(rows, schema=['day', 'pnl', 'net', 'stopped', 'early'],
                                orient='row')

        res = {'F': stats(day_list, f_taken, f_daily)}
        for c in want_a1:
            res[c] = stats(day_list, variant(veto[c], {}), f_daily)
        for c in want_a2:
            res[c] = stats(day_list, variant(set(), exits[c]), f_daily)
        if mode == 'holdout' and len(want_a1) == 1 and len(want_a2) == 1:
            res['AB'] = stats(day_list, variant(veto[want_a1[0]], exits[want_a2[0]]), f_daily)
        if mode == 'dev':
            for c1 in A1_NAMES:
                for c2 in A2_NAMES:
                    res[f'AB:{c1}+{c2}'] = stats(day_list, variant(veto[c1], exits[c2]),
                                                 f_daily)
        results[seed] = res
        if contribs:
            sv, xv = np.vstack(contribs), np.vstack(contrib_x)
            for j, f in enumerate(feats):
                ok = ~np.isnan(xv[:, j])
                corr = np.corrcoef(xv[ok, j], sv[ok, j])[0, 1] if ok.sum() > 10 and \
                    xv[ok, j].std() > 0 and sv[ok, j].std() > 0 else float('nan')
                shap[f] = (float(np.abs(sv[:, j]).mean()), corr)
    return results, shap


print(f'Study A, Part 2, {mode}: {lo} to {hi}')
for name in SETS:
    if mode == 'holdout' and name not in chosen:
        print(f'\n{name}: no candidate chosen on development; not run')
        continue
    results, shap = run(name)
    print(f'\n=== {name} ===')
    base = results[7]
    rows = [{'variant': k, **{kk: vv for kk, vv in v.items() if kk != 'diff_by_year'},
             'by_year': str(v['diff_by_year'])} for k, v in base.items()]
    with pl.Config(tbl_rows=60, tbl_cols=20, tbl_width_chars=250, fmt_str_lengths=60):
        print(pl.DataFrame(rows))
    if len(results) > 1:
        print('t of the daily difference from F by seed (7, 8, 9, 10):')
        for k in base:
            if k == 'F':
                continue
            print(f'  {k:12s}', [results[s][k]['t_diff'] for s in results])
    if shap:
        print('A1 stop classifier: mean |SHAP| (direction: + raises the stop probability)')
        for f, (m, corr) in sorted(shap.items(), key=lambda x: -x[1][0])[:20]:
            print(f'  {f:18s} {m:.4f}  {"+" if corr > 0 else "-" if corr < 0 else "?"}'
                  f' ({corr:+.2f})')
