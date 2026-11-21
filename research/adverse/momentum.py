"""Study B: adverse months of the momentum Rankings order, and models and rules to cut them
(PROTOCOL.md).

Usage: uv run --no-project --with polars --with numpy --with lightgbm --with scikit-learn \
         python -I momentum.py INPUTS PRICES DELIVERY OUT_DIR dev|confirm [B1 B2]

INPUTS holds research/momentum_upgrades' inputs (pred.parquet, universe500.parquet,
quarters.parquet); PRICES research/promoter's adjusted daily prices (all.parquet);
DELIVERY the parsed delivery table (deliv.parquet). `dev` reports every candidate on months
ending 2016-2020 and writes OUT_DIR/b_rows.parquet (each month's 60 highest by momentum,
with their parameters and labels, for drivers.py); it reads nothing ending after 2020.
`confirm` runs only the chosen B1 and B2 (or `-` for none) on January 2021 - September
2026, once.

Fixed here before any of it was run:
- Sessions, adjusted closes and the 5-session carry of a missing close are
  research/momentum_upgrades/upgrades.py's; a month's return is its `fwd_1m`, a missing
  one 0, as there.
- The 500's daily equal-weighted return on a session: the mean daily return of the
  stocks in the 500 at the latest month end before it. The 500's month return: the mean
  `fwd_1m` of the 500 at the month end.
- A pick's return within the month, at session k after the month end: its close then
  (carried) over its month-end close. A stock with no close by then has no check.
- The rest of the month against the 500 from session k: (1 + month) / (1 + to k) - 1 for
  the pick, less the same for the 500's mean, the 500's to k being the mean of its
  members' returns to k.
- Volume: traded value. "Since entry" means sessions 1 to k after the month end.
- The model for the months held in year y is fitted on every row whose month ended by 31
  December of y - 1.
- B2 sells at session k's close, pays 0.195%, and holds the money in cash to the month
  end; a holding that is sold doesn't return in that month.
- A missing parameter stays missing for LightGBM; a candidate that ranks by one (BH, BV,
  BX) puts a stock without it last.
"""
import bisect
import datetime as dt
import math
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

inputs, prices_path, deliv_path, out_dir, mode = (Path(sys.argv[1]), sys.argv[2], sys.argv[3],
                                                  Path(sys.argv[4]), sys.argv[5])
out_dir.mkdir(parents=True, exist_ok=True)
PERIOD = {'dev': (dt.date(2016, 1, 1), dt.date(2020, 12, 31)),
          'confirm': (dt.date(2021, 1, 1), dt.date(2026, 9, 30))}[mode]
FIRST_ROWS = dt.date(2013, 1, 1)
COST = 0.00195
GAP = 5
TOP, POOL, ROWS = 20, 30, 60
CHECKS = (5, 10)
B1_NAMES, B2_NAMES = ('BM', 'BH', 'BV', 'BX'), ('XR', 'XM3', 'XM5')
chosen_b1 = sys.argv[6] if mode == 'confirm' else None
chosen_b2 = sys.argv[7] if mode == 'confirm' else None


def params(seed):
    return dict(n_estimators=200, learning_rate=0.03, num_leaves=15, min_child_samples=100,
                colsample_bytree=0.8, subsample=0.8, subsample_freq=1, random_state=seed,
                verbose=-1)


uni = pl.read_parquet(inputs / 'universe500.parquet').select(pl.col('day').alias('me'), 'sid')
pred = pl.read_parquet(inputs / 'pred.parquet').select('sid', 'me', 'fwd_1m', 'p_momentum')
x = uni.join(pred, on=['sid', 'me'], how='left')
mes = sorted(x['me'].unique().to_list())
mes = [m for m in mes if m <= PERIOD[1]] + [m for m in mes if m > PERIOD[1]][:1]
k_of = {me: k for k, me in enumerate(mes)}
months = [(a, b) for a, b in zip(mes[:-1], mes[1:], strict=True) if b >= FIRST_ROWS
          and b <= PERIOD[1]]
members = {me: g.sort('sid') for (me,), g in x.group_by('me')}

# --- Daily grids ------------------------------------------------------------------------

sess = pl.scan_parquet(prices_path).select('di', 'day').unique().collect().sort('di')
days = sess['day'].to_list()
pos_of_di = {d: i for i, d in enumerate(sess['di'].to_list())}
S = len(days)
sids = sorted(x['sid'].unique().to_list())
col = {s: j for j, s in enumerate(sids)}
px = (pl.scan_parquet(prices_path).filter(pl.col('sid').is_in(sids))
      .select('sid', 'di', 'day', 'symbol', 'aclose', 'aopen', 'value').collect()
      .sort('sid', 'di'))
px = px.with_columns(pl.col('di').replace_strict(pos_of_di).alias('pos'),
                     pl.col('sid').replace_strict(col).alias('j'))
P, J = px['pos'].to_numpy(), px['j'].to_numpy()


def grid(values):
    g = np.full((S, len(sids)), np.nan)
    g[P, J] = values
    return g


C, VAL = grid(px['aclose'].to_numpy()), grid(px['value'].to_numpy())
dl = (pl.scan_parquet(deliv_path).filter(pl.col('series') == 'EQ')
      .select('day', 'symbol', 'deliv_pct').collect())
dl = px.select('day', 'symbol', 'pos', 'j').join(dl, on=['day', 'symbol'], how='inner')
DLV = np.full((S, len(sids)), np.nan)
DLV[dl['pos'].to_numpy(), dl['j'].to_numpy()] = dl['deliv_pct'].to_numpy()
px2 = px.with_columns(
    (pl.col('aclose') / pl.col('aclose').shift(1).over('sid') - 1).alias('r'),
    (pl.col('aopen') / pl.col('aclose').shift(1).over('sid') - 1).alias('g'),
    (pl.col('pos') - pl.col('pos').shift(1).over('sid')).alias('gap'))
px2 = px2.filter(pl.col('r').is_not_null() & (pl.col('gap') <= GAP))
RD = np.full((S, len(sids)), np.nan)
RD[px2['pos'].to_numpy(), px2['j'].to_numpy()] = px2['r'].to_numpy()
GD = np.full((S, len(sids)), np.nan)
GD[px2['pos'].to_numpy(), px2['j'].to_numpy()] = px2['g'].to_numpy()
del px, px2, dl


def pos_at(day):
    return bisect.bisect_right(days, day) - 1


def carried(p, js):
    """Each column's close at session p, or its last within GAP sessions before."""
    block = C[max(0, p - GAP):p + 1][:, js]
    out = np.full(block.shape[1], np.nan)
    for row in block:
        ok = ~np.isnan(row)
        out[ok] = row[ok]
    return out


# The 500's daily equal-weighted return.
MKTD = np.full(S, np.nan)
for a, b in zip(mes[:-1], mes[1:], strict=True):
    js = [col[s] for s in members[a]['sid'].to_list()]
    for p in range(pos_at(a) + 1, pos_at(b) + 1):
        MKTD[p] = np.nanmean(RD[p, js])

q = (pl.read_parquet(inputs / 'quarters.parquet').select('sid', 'broadcast').drop_nulls()
     .unique().sort('sid', 'broadcast'))
broadcasts = {s: [b.date() for b in g['broadcast'].to_list()] for (s,), g in q.group_by('sid')}

# --- Parameters and labels, each month's 60 highest by momentum -------------------------

rows = []
for me, nxt in months:
    g = members[me].filter(pl.col('p_momentum').is_not_null())
    g = g.sort([pl.col('p_momentum'), pl.col('sid')], descending=[True, False])
    allj = [col[s] for s in members[me]['sid'].to_list()]
    fwd_all = members[me]['fwd_1m'].fill_null(0.0).to_numpy()
    mkt_month = float(fwd_all.mean())
    p0, p1 = pos_at(me), pos_at(nxt)
    c_me_all = carried(p0, allj)
    # Market state at the month end.
    m252 = float(np.nanprod(1 + MKTD[p0 - 251:p0 + 1]) - 1)
    m21 = float(np.nanprod(1 + MKTD[p0 - 20:p0 + 1]) - 1)
    mvol = float(np.nanstd(MKTD[p0 - 62:p0 + 1], ddof=1) * math.sqrt(252))
    r1m_all = c_me_all / carried(pos_at(mes[k_of[me] - 1]), allj) - 1 if k_of[me] else \
        np.full(len(allj), np.nan)
    disp = float(np.nanstd(r1m_all, ddof=1))
    # The 500's return to sessions k after the month end.
    mkt_to = {k: float(np.nanmean(carried(p0 + k, allj) / c_me_all - 1)) if p0 + k <= p1
              else float('nan') for k in CHECKS}
    mk = MKTD[p0 - 125:p0 + 1]
    for rank, r in enumerate(g.head(ROWS).iter_rows(named=True), 1):
        s, j = r['sid'], col[r['sid']]
        c0 = carried(p0, [j])[0]
        if np.isnan(c0):
            continue

        def ret(n, j=j, c0=c0, p0=p0):
            past = carried(p0 - n, [j])[0]
            return c0 / past - 1 if not np.isnan(past) else np.nan

        rd = RD[p0 - 251:p0 + 1, j]
        cl = C[p0 - 251:p0 + 1, j]
        v126, v21 = VAL[p0 - 125:p0 + 1, j], VAL[p0 - 20:p0 + 1, j]
        d126, d21 = DLV[p0 - 125:p0 + 1, j], DLV[p0 - 20:p0 + 1, j]
        r126 = RD[p0 - 125:p0 + 1, j]
        ok = ~np.isnan(r126) & ~np.isnan(mk)
        if ok.sum() >= 100:
            beta, alpha = np.polyfit(mk[ok], r126[ok], 1)
            ivol = float(np.std(r126[ok] - (alpha + beta * mk[ok]), ddof=2) * math.sqrt(252))
        else:
            beta = ivol = np.nan
        bc = broadcasts.get(s, [])
        i = bisect.bisect_right(bc, me)
        since = (me - bc[i - 1]).days if i else np.nan
        med126 = np.nanmedian(v126) if (~np.isnan(v126)).sum() >= 60 else np.nan
        mean_d126 = np.nanmean(d126) if (~np.isnan(d126)).sum() >= 60 else np.nan
        row = {
            'me': me, 'held_to': nxt, 'sid': s, 'rank': rank, 'top20': rank <= TOP,
            'top30': rank <= POOL, 'fwd': r['fwd_1m'] if r['fwd_1m'] is not None else 0.0,
            'mkt': mkt_month,
            'f_mom': r['p_momentum'], 'f_rank': rank,
            'f_r21': ret(21), 'f_r126': ret(126), 'f_r252': ret(251),
            'f_vol252': float(np.nanstd(rd, ddof=1) * math.sqrt(252))
            if (~np.isnan(rd)).sum() >= 150 else np.nan,
            'f_max21': float(np.nanmax(RD[p0 - 20:p0 + 1, j]))
            if (~np.isnan(RD[p0 - 20:p0 + 1, j])).any() else np.nan,
            'f_near52': float(c0 / np.nanmax(cl)) if (~np.isnan(cl)).sum() >= 150 else np.nan,
            'f_dist50': float(c0 / np.nanmean(C[p0 - 49:p0 + 1, j]) - 1),
            'f_vsurge': float(np.nanmedian(v21) / med126) if med126 and med126 > 0 else np.nan,
            'f_dlv21': float(np.nanmean(d21)) if (~np.isnan(d21)).sum() >= 10 else np.nan,
            'f_dlv_chg': float(np.nanmean(d21) - mean_d126)
            if (~np.isnan(d21)).sum() >= 10 and not np.isnan(mean_d126) else np.nan,
            'f_up126': float(np.nanmean(r126 > 0)) if (~np.isnan(r126)).sum() >= 100
            else np.nan,
            'f_beta': beta, 'f_ivol': ivol,
            'f_gaps': float(np.nansum(np.abs(GD[p0 - 125:p0 + 1, j]) > 0.05)),
            'f_size': float(math.log(med126)) if med126 and med126 > 0 else np.nan,
            'f_days_results': since, 'f_due': float(since > 75) if not np.isnan(since) else np.nan,
            'f_m252': m252, 'f_m21': m21, 'f_mvol63': mvol, 'f_disp': disp,
        }
        # The month's path on daily closes.
        path = []
        last = c0
        worst = 0.0
        for p in range(p0 + 1, p1 + 1):
            c = C[p, j]
            if not np.isnan(c):
                last = c
            worst = min(worst, last / c0 - 1)
            path.append(last)
        row['dd'] = worst
        row['dd15'] = worst <= -0.15
        row['adverse'] = row['fwd'] - mkt_month <= -0.10
        for k in CHECKS:
            pk = p0 + k
            ck = carried(pk, [j])[0] if pk <= p1 else np.nan
            if np.isnan(ck):
                for name in ('ret', 'rel', 'worst', 'val', 'dlv', 'rest'):
                    row[f'a{k}_{name}'] = np.nan
                continue
            to_k = ck / c0 - 1
            vk = VAL[p0 + 1:pk + 1, j]
            dk = DLV[p0 + 1:pk + 1, j]
            row[f'a{k}_ret'] = to_k
            row[f'a{k}_rel'] = to_k - mkt_to[k]
            row[f'a{k}_worst'] = min(0.0, min(v / c0 - 1 for v in path[:k]))
            row[f'a{k}_val'] = float(np.nanmean(vk) / med126) if med126 and med126 > 0 and \
                (~np.isnan(vk)).any() else np.nan
            row[f'a{k}_dlv'] = float(np.nanmean(dk) - mean_d126) if (~np.isnan(dk)).any() and \
                not np.isnan(mean_d126) else np.nan
            row[f'a{k}_rest'] = ((1 + row['fwd']) / (1 + to_k) - 1) - \
                ((1 + mkt_month) / (1 + mkt_to[k]) - 1)
        rows.append(row)
    if me.month == 12:
        print(me, 'rows done', flush=True)

R = pl.DataFrame(rows, infer_schema_length=None)
F_COLS = [c for c in R.columns if c.startswith('f_')]
R.filter(pl.col('held_to') <= PERIOD[1]).write_parquet(
    out_dir / ('b_rows.parquet' if mode == 'dev' else 'b_rows_all.parquet'))

# --- Models: B1's adverse classifier, B2's rest-of-month regression ----------------------

years = sorted({nxt.year for _, nxt in months if PERIOD[0] <= nxt <= PERIOD[1]})
p_adverse, rest_hat = {}, {}
for y in years:
    train = R.filter(pl.col('held_to') <= dt.date(y - 1, 12, 31))
    test = R.filter(pl.col('held_to').is_between(dt.date(y, 1, 1), dt.date(y, 12, 31)))
    clf = lgb.LGBMClassifier(**params(7)).fit(train.select(F_COLS).to_numpy(),
                                              train['adverse'].to_numpy())
    for (me, s), p in zip(test.select('me', 'sid').iter_rows(),
                          clf.predict_proba(test.select(F_COLS).to_numpy())[:, 1], strict=True):
        p_adverse[(me, s)] = p
    for k in CHECKS:
        a_cols = F_COLS + [f'a{k}_{n}' for n in ('ret', 'rel', 'worst', 'val', 'dlv')]
        tr = train.filter(pl.col(f'a{k}_rest').is_not_null() & pl.col(f'a{k}_rest').is_not_nan())
        reg = lgb.LGBMRegressor(**params(7)).fit(tr.select(a_cols).to_numpy(),
                                                 tr[f'a{k}_rest'].to_numpy())
        te = test.filter(pl.col(f'a{k}_ret').is_not_null() & pl.col(f'a{k}_ret').is_not_nan())
        for (me, s), v in zip(te.select('me', 'sid').iter_rows(),
                              reg.predict(te.select(a_cols).to_numpy()), strict=True):
            rest_hat[(me, s, k)] = v

# --- The portfolios ---------------------------------------------------------------------


def rebalance(book, targets):
    """Equal weights in `targets`, everything else sold; costs on what is traded."""
    V = sum(book['h'].values()) + book['cash']
    n = len(targets)
    Vp = V
    for _ in range(30):
        t = Vp / n
        turnover = sum(abs(t - book['h'].get(s, 0.0)) for s in targets) + \
            sum(v for s, v in book['h'].items() if s not in targets)
        new = V - COST * turnover
        if abs(new - Vp) < 0.01:
            break
        Vp = new
    book['h'] = {s: Vp / n for s in targets}
    book['cash'] = 0.0
    return V


def pick_b1(name, g):
    """20 of the month's 30 highest by momentum."""
    pool = g.filter(pl.col('top30'))
    if name == 'BM':
        sc = [(p_adverse.get((r['me'], r['sid']), 0.0), r['rank'], r['sid'])
              for r in pool.iter_rows(named=True)]
        return [s for _, _, s in sorted(sc)[:TOP]]
    col_, best_high = {'BH': ('f_near52', True), 'BV': ('f_vsurge', False),
                       'BX': ('f_max21', False)}[name]
    sc = []
    for r in pool.iter_rows(named=True):
        v = r[col_]
        missing = v is None or (isinstance(v, float) and math.isnan(v))
        sc.append((1 if missing else 0, (-v if best_high else v) if not missing else 0,
                   r['rank'], r['sid']))
    return [s for *_, s in sorted(sc)[:TOP]]


def sells(name, r, k):
    """Whether B2 rule `name` sells holding row r at session k."""
    if r[f'a{k}_ret'] is None or math.isnan(r[f'a{k}_ret']):
        return False
    if name == 'XR':
        val = r[f'a{k}_val']
        return r[f'a{k}_ret'] <= -0.08 and val is not None and not math.isnan(val) and val >= 1.5
    hat = rest_hat.get((r['me'], r['sid'], k))
    return hat is not None and hat < {'XM3': -0.03, 'XM5': -0.05}[name]


def simulate(target_of, b2=None):
    """Monthly returns after costs, before tax, from the first month end of the period."""
    # Rs 10 lakh, as upgrades.py: rebalance's 0.01 tolerance is in rupees. (From 1.0 it
    # stopped before charging any cost: the first development run's error, CHOICE.md.)
    book = {'h': {}, 'cash': 1_000_000.0}
    rets, ms = [], []
    for me, nxt in months:
        if not PERIOD[0] <= nxt <= PERIOD[1]:
            continue
        g = R.filter(pl.col('me') == me)
        targets = target_of(g)
        v0 = rebalance(book, targets)
        by_sid = {r['sid']: r for r in g.iter_rows(named=True)}
        cash_in = 0.0
        for s in list(book['h']):
            r = by_sid[s]
            sold = False
            if b2:
                for k in CHECKS:
                    if sells(b2, r, k):
                        cash_in += book['h'][s] * (1 + r[f'a{k}_ret']) * (1 - COST)
                        del book['h'][s]
                        sold = True
                        break
            if not sold:
                book['h'][s] *= 1 + r['fwd']
        book['cash'] += cash_in
        rets.append((sum(book['h'].values()) + book['cash']) / v0 - 1)
        ms.append(nxt)
    return np.array(rets), ms


def summary(r, ms, base):
    yrs = len(r) / 12
    w = np.cumprod(1 + r)
    d = r - base
    halves = {}
    if mode == 'confirm':
        for lab, (a, b) in (('2021-2023', (2021, 2023)), ('2024-2026', (2024, 2026))):
            sel = np.array([a <= m.year <= b for m in ms])
            halves[lab] = round(float(d[sel].mean() * 100), 3)
    else:
        for lab, (a, b) in (('2016-2018', (2016, 2018)), ('2019-2020', (2019, 2020))):
            sel = np.array([a <= m.year <= b for m in ms])
            halves[lab] = round(float(d[sel].mean() * 100), 3)
    sd = d.std(ddof=1)
    return {'a_year': round(float(np.prod(1 + r) ** (1 / yrs) - 1) * 100, 1),
            'vol': round(float(r.std(ddof=1) * math.sqrt(12)) * 100, 1),
            'max_dd': round(float((w / np.maximum.accumulate(w) - 1).min()) * 100, 1),
            'worst_month': round(float(r.min()) * 100, 1),
            'excess_month_%': round(float(d.mean()) * 100, 3),
            't_excess': round(float(d.mean() / sd * math.sqrt(len(d))), 2) if sd > 0 else None,
            'halves': str(halves)}


def top20(g):
    return g.filter(pl.col('top20'))['sid'].to_list()


m_ret, ms = simulate(top20)
res = [{'variant': 'M', **summary(m_ret, ms, m_ret)}]
want_b1 = B1_NAMES if mode == 'dev' else [c for c in [chosen_b1] if c and c != '-']
want_b2 = B2_NAMES if mode == 'dev' else [c for c in [chosen_b2] if c and c != '-']
for name in want_b1:
    r, _ = simulate(lambda g, n=name: pick_b1(n, g))
    res.append({'variant': name, **summary(r, ms, m_ret)})
for name in want_b2:
    r, _ = simulate(top20, b2=name)
    res.append({'variant': name, **summary(r, ms, m_ret)})
print(f'\nStudy B, {mode}: months ending {ms[0]} to {ms[-1]} ({len(ms)}), after costs, '
      f'before tax')
with pl.Config(tbl_rows=20, tbl_cols=12, tbl_width_chars=200, fmt_str_lengths=60):
    print(pl.DataFrame(res))
held = R.filter(pl.col('top20') & pl.col('held_to').is_between(*PERIOD))
for k in CHECKS:
    for name in want_b2:
        n = sum(sells(name, r, k) for r in held.iter_rows(named=True))
        print(f'{name} would sell at session {k}: {n} of {held.height} pick-months '
              f'(before any earlier sale)')
print(f"M's adverse pick-months: {held['adverse'].mean():.3f}; drawdown -15%: "
      f"{held['dd15'].mean():.3f}")
