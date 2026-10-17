"""Walk-forward ranking weights for research/ai_tuning/PROTOCOL.md.

Usage (uv run --no-project --with polars --with numpy python -I tune.py FEATURES UNIVERSE NIFTY):
  FEATURES, UNIVERSE  research/ml_rating's features.parquet and universe500.parquet
  NIFTY               the Nifty 50's daily candles (Upstox JSON: [time, o, h, l, c, ...])
"""
import datetime as dt
import itertools
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ml_rating'))
import model as M  # noqa: E402

PILLARS = ['growth', 'quality', 'valuation', 'momentum', 'low_volatility']
MOM = {'app': 'p_momentum', '6m': 'r_risk_adj_return_6m', '12m': 'r_risk_adj_return_12m',
       '12-1': 'r_mom_12_1'}
START = dt.date(2013, 1, 1)
TEST = (dt.date(2016, 1, 1), dt.date(2026, 9, 30))
HALVES = ((dt.date(2016, 1, 1), dt.date(2020, 12, 31)), (dt.date(2021, 1, 1), dt.date(2026, 9, 30)))
GRID = [tuple(v / 5 for v in w) for w in itertools.product(range(6), repeat=5) if sum(w) == 5]
MOM_ONLY = (0.0, 0.0, 0.0, 1.0, 0.0)
BAR = 2.24
# The protocol asks for 36 months and names January 2016 as the first refit; the December
# 2015 refit has 35 month-ends before it (January 2013 to November 2015). The date is kept.
MIN_MONTHS = 35
TAX = 0.20                          # short-term gains, descriptive only


def tstat(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2 or x.std(ddof=1) == 0:
        return float('-inf')
    return float(x.mean() / x.std(ddof=1) * math.sqrt(len(x)))


class Months:
    """Month-by-month arrays: pillar percentiles, momentum variants, forward returns."""

    def __init__(self, x):
        x = x.filter(pl.col('me') >= START).sort('me', 'sid')
        self.mes = sorted(x['me'].unique().to_list())
        self.rows = []
        for me in self.mes:
            g = x.filter((pl.col('me') == me) & pl.col('fwd_1m').is_not_null())
            self.rows.append({
                'sid': g['sid'].to_numpy(),
                'fwd': g['fwd_1m'].to_numpy(),
                'tgt': g['t_1m'].to_numpy(),
                'P': g.select([f'p_{p}' for p in PILLARS]).to_numpy().astype(float),
                'mom': {k: g[c].to_numpy().astype(float) for k, c in MOM.items()}})

    def score(self, k, w):
        """The weighted mean of the non-null pillars; NaN where none of them has a value."""
        P = self.rows[k]['P']
        w = np.asarray(w)
        have = ~np.isnan(P)
        den = (have * w).sum(axis=1)
        num = (np.nan_to_num(P) * w).sum(axis=1)
        with np.errstate(invalid='ignore', divide='ignore'):
            return np.where(den > 0, num / den, np.nan)

    def series(self, score_of):
        """Net and gross top-20 excess return for each month-end, held a month."""
        held, gross, net = set(), [], []
        for k, r in enumerate(self.rows):
            s = score_of(k)
            ok = ~np.isnan(s)
            order = np.argsort(-s[ok], kind='stable')[:M.TOP_TRADE]
            idx = np.flatnonzero(ok)[order]
            names = set(r['sid'][idx].tolist())
            g = r['fwd'][idx].mean() - r['fwd'].mean()
            turn = len(names - held) / M.TOP_TRADE
            held = names
            gross.append(g)
            net.append(g - M.COST * turn)
        return np.array(gross), np.array(net)


def refit_months(mo):
    """Month-end indices where a January refit happens: the December month-ends."""
    return [k for k, me in enumerate(mo.mes) if me.month == 12]


def wfo(mo):
    """WFO: each December, the grid vector with the best t of net excess before it."""
    nets = {w: mo.series(lambda k, w=w: mo.score(k, w))[1] for w in GRID}
    chosen = {}
    for k in refit_months(mo):
        if k < MIN_MONTHS:
            continue
        best = max(GRID, key=lambda w: (tstat(nets[w][:k]), w[3]))
        chosen[k] = best
    return chosen, nets


def lw(mo):
    """LW: each December, OLS of next-month return percentile on the pillars before it."""
    chosen = {}
    for k in refit_months(mo):
        if k < MIN_MONTHS:
            continue
        X = np.vstack([np.nan_to_num(mo.rows[j]['P'], nan=0.5) for j in range(k)])
        y = np.concatenate([mo.rows[j]['tgt'] for j in range(k)])
        A = np.column_stack([np.ones(len(X)), X])
        coef = np.linalg.lstsq(A, y, rcond=None)[0][1:]
        w = np.clip(coef, 0, None)
        chosen[k] = tuple(w / w.sum()) if w.sum() > 0 else MOM_ONLY
    return chosen


def mw(mo):
    """MW: each December, the momentum definition with the best t of net excess before it."""
    nets = {m: mo.series(lambda k, m=m: mo.rows[k]['mom'][m])[1] for m in MOM}
    order = list(MOM)
    chosen = {}
    for k in refit_months(mo):
        if k < MIN_MONTHS:
            continue
        chosen[k] = max(order, key=lambda m: (tstat(nets[m][:k]), -order.index(m)))
    return chosen


def fm(mo, unit_gross):
    """FM: each month, weights proportional to the pillars' positive trailing-12 returns."""
    chosen = {}
    for k in range(len(mo.mes)):
        if k < 12:
            continue
        trail = np.array([unit_gross[p][k - 12:k].mean() for p in range(5)])
        w = np.clip(trail, 0, None)
        chosen[k] = tuple(w / w.sum()) if w.sum() > 0 else MOM_ONLY
    return chosen


def held_for(chosen, k):
    """The weights in force for the portfolio formed at month-end k: the latest refit."""
    ks = [j for j in chosen if j <= k]
    return chosen[max(ks)] if ks else None


def scores_frame(mo, name, score_of):
    parts = []
    for k, r in enumerate(mo.rows):
        s = score_of(k)
        if s is None:
            continue
        parts.append(pl.DataFrame({'sid': r['sid'], 'me': [mo.mes[k]] * len(r['sid']),
                                   'fwd_1m': r['fwd'], name: s}))
    return pl.concat(parts).with_columns(pl.col(name).fill_nan(None))


def evaluate(frames, all_rets):
    """Trading and investing stats for each candidate, from research/ml_rating's code."""
    out = {}
    for name, f in frames.items():
        tr = M.trading(f, name, *TEST)
        inv = M.investing(f, all_rets, name, *TEST)
        out[name] = {'trading': tr, 'investing': inv}
    return out


def paired(a, b):
    j = a.select('me', pl.col('net').alias('a')).join(b.select('me', pl.col('net').alias('b')),
                                                      on='me')
    d = (j['a'] - j['b']).to_numpy()
    half = []
    for lo, hi in HALVES:
        month = pl.col('me').dt.offset_by('5d')
        jj = j.filter((month >= lo) & (month <= hi))
        half.append(float((jj['a'] - jj['b']).mean()))
    return {'diff': float(d.mean()), 't': tstat(d), 'halves': half, 'months': len(d)}


def absolute(f, name, top, nifty):
    """Absolute monthly returns of the top `top`, after costs, with the universe and Nifty."""
    p = f
    rows, held = [], set()
    mes = sorted(p['me'].unique().to_list())
    for i, me in enumerate(mes):
        g = p.filter(pl.col('me') == me)
        t = g.filter(pl.col(name).is_not_null()).sort(name, descending=True).head(top)
        names = set(t['sid'].to_list())
        turn = len(names - held) / top
        held = names
        month = me + dt.timedelta(days=5)
        if not (TEST[0] <= month.replace(day=1) <= TEST[1]) or i + 1 >= len(mes):
            continue
        nxt = mes[i + 1]
        rows.append({'me': me, 'port': t['fwd_1m'].mean() - M.COST * turn,
                     'uni': g['fwd_1m'].mean(),
                     'nifty': nifty[nxt] / nifty[me] - 1 if me in nifty and nxt in nifty
                     else None})
    return pl.DataFrame(rows)


def summarise(df, col):
    r = df[col].to_numpy()
    wealth = np.cumprod(1 + r)
    years = len(r) / 12
    peak = np.maximum.accumulate(wealth)
    dd = float((wealth / peak - 1).min())
    by_year = (df.with_columns((pl.col('me') + pl.duration(days=5)).dt.year().alias('y'))
                 .group_by('y').agg(((1 + pl.col(col)).product() - 1).alias('r')).sort('y'))
    full = by_year.filter(pl.col('y') <= 2025)
    # After tax, roughly: holdings change monthly, so a year's gain is taxed as short-term
    # (20%); a losing year pays nothing and its loss is not carried forward.
    taxed = float(np.prod([1 + v - TAX * max(v, 0.0) for v in by_year['r'].to_list()]))
    return {'cagr': float(wealth[-1] ** (1 / years) - 1), 'dd': dd,
            'cagr_tax': taxed ** (1 / years) - 1,
            'worst_year': float(full['r'].min()),
            'losing': int((full['r'] < 0).sum()), 'years': full.height,
            'by_year': dict(zip(by_year['y'].to_list(), by_year['r'].to_list(), strict=True)),
            'growth': float(wealth[-1])}


def nifty_closes(path, mes):
    d = json.load(open(path))
    closes = {dt.date.fromisoformat(c[0][:10]): float(c[4]) for c in d}
    return {me: closes[me] for me in mes if me in closes}


def fmt_w(w):
    return ' '.join(f'{p[:4]} {v:.2f}' for p, v in zip(PILLARS, w, strict=True) if v > 0)


def main(features, universe, nifty_path):
    x = M.prepare(features, universe)
    mo = Months(x)
    print(f'Month-ends {mo.mes[0]} to {mo.mes[-1]}: {len(mo.mes)}; test months held '
          f'{TEST[0]} to {TEST[1]}\n')

    unit_gross = [mo.series(lambda k, p=p: mo.rows[k]['P'][:, p])[0] for p in range(5)]
    c_fm = fm(mo, unit_gross)
    c_wfo, _ = wfo(mo)
    c_lw = lw(mo)
    c_mw = mw(mo)

    def weighted(chosen):
        return lambda k: (mo.score(k, held_for(chosen, k)) if held_for(chosen, k) else None)

    frames = {
        'M': scores_frame(mo, 'M', lambda k: mo.rows[k]['mom']['app']),
        'FM': scores_frame(mo, 'FM', weighted(c_fm)),
        'WFO': scores_frame(mo, 'WFO', weighted(c_wfo)),
        'LW': scores_frame(mo, 'LW', weighted(c_lw)),
        'MW': scores_frame(mo, 'MW', lambda k: (mo.rows[k]['mom'][held_for(c_mw, k)]
                                                if held_for(c_mw, k) else None)),
    }
    all_rets = pl.read_parquet(features).select('sid', 'me', 'fwd_1m')
    res = evaluate(frames, all_rets)

    print('Trading: top 20 held a month, excess over the universe after costs')
    for name in frames:
        M.show(name, M.stats(res[name]['trading']))
    print('\nInvesting: the last 12 monthly cohorts of the top 50, excess over the universe '
          'after costs')
    for name in frames:
        M.show(name, M.stats(res[name]['investing']))

    print(f'\nAgainst fixed momentum (M), trading, paired by month; pass needs t >= {BAR}, '
          'both halves above zero and t >= 2 over the universe')
    passed = []
    for name in ('FM', 'WFO', 'LW', 'MW'):
        p = paired(res[name]['trading'], res['M']['trading'])
        t_u = M.stats(res[name]['trading'])['t']
        ok = p['diff'] > 0 and p['t'] >= BAR and all(h > 0 for h in p['halves']) and t_u >= 2
        if ok:
            passed.append((p['t'], name))
        print(f"  {name:4s} {p['diff'] * 100:+.2f}% a month (t {p['t']:5.2f}); 2016-2020 "
              f"{p['halves'][0] * 100:+.2f}%, 2021-2026 {p['halves'][1] * 100:+.2f}%; "
              f"over the universe t {t_u:4.2f}: {'PASSED' if ok else 'not passed'}")
        pi = paired(res[name]['investing'], res['M']['investing'])
        print(f"       investing {pi['diff'] * 100:+.2f}% a month (t {pi['t']:5.2f})")
    print('Adopted:', max(passed)[1] if passed else 'none; the ranking stays on fixed momentum')

    print('\nWeights chosen (in force for the year that follows each refit):')
    for label, chosen in (('WFO', c_wfo), ('LW', c_lw)):
        for k in sorted(chosen):
            print(f'  {label} {mo.mes[k].year + 1}: {fmt_w(chosen[k])}')
    for k in sorted(c_mw):
        print(f'  MW  {mo.mes[k].year + 1}: {c_mw[k]}')
    fm_df = pl.DataFrame([{'me': mo.mes[k], **dict(zip(PILLARS, w, strict=True))}
                          for k, w in c_fm.items()])
    fm_test = fm_df.filter(pl.col('me') >= dt.date(2015, 12, 1))
    print('  FM, average weight over the test: '
          + ', '.join(f'{p} {fm_test[p].mean():.2f}' for p in PILLARS)
          + f"; momentum alone in {(fm_test['momentum'] == 1).mean():.0%} of months")

    print('\nDescriptive: fixed momentum (M) in absolute terms, after costs, months held '
          f'{TEST[0]} to {TEST[1]}. Price returns: dividends are left out. The after-tax '
          'figure was added after the protocol.')
    nifty = nifty_closes(nifty_path, mo.mes)
    for top in (10, 20):
        df = absolute(frames['M'], 'M', top, nifty)
        for col, label in (('port', f'Momentum top {top}'), ('uni', 'Universe (500)'),
                           ('nifty', 'Nifty 50 (price)')):
            if col != 'port' and top == 20:
                continue
            s = summarise(df.drop_nulls(col), col)
            tax = f", {s['cagr_tax'] * 100:.1f}% after tax" if col == 'port' else ''
            print(f"  {label:18s} {s['cagr'] * 100:5.1f}% a year{tax}, "
                  f"worst fall {s['dd'] * 100:6.1f}%, "
                  f"worst year {s['worst_year'] * 100:6.1f}%, losing years {s['losing']} of "
                  f"{s['years']}; Rs 1 became Rs {s['growth']:.2f}")
            print('      ' + '  '.join(f'{y}: {v * 100:+.0f}%' for y, v in s['by_year'].items()))


if __name__ == '__main__':
    main(*sys.argv[1:4])
