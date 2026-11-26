"""Rebuild the Nifty200 Momentum 30 from the engine's data and walk from it to the Rankings
order's backtest (M), one change at a time (PROTOCOL.md).

Usage: uv run --no-project --with polars --with numpy python -I gap.py \
         PRICES UNIVERSE PRED INDEX_PARQUET PR_ZIP
PRICES is research/promoter/build_all.py's panel; UNIVERSE and PRED are
research/momentum_upgrades/inputs.py's; INDEX_PARQUET NSE's month-end index closes
(fetch_index.py); PR_ZIP NSE's PR archive of 30 October 2026 (its mcap file).
"""
import csv
import datetime as dt
import io
import math
import sys
import zipfile

import numpy as np
import polars as pl

prices_path, uni_path, pred_path, index_path, pr_zip = sys.argv[1:6]
FIRST, LAST = dt.date(2020, 12, 31), dt.date(2026, 9, 30)
COST = 0.00195
N_INDEX, N_ALWAYS, N_KEEP = 30, 15, 45
CAP_ABS, CAP_MULT = 0.05, 5.0

# ------------------------------------------------------------------- shares (Oct 2026)
z = zipfile.ZipFile(pr_zip)
member = next(n for n in z.namelist() if n.lower().startswith('mcap'))
shares_by_symbol = {}
for r in csv.DictReader(io.StringIO(z.read(member).decode('latin-1'))):
    r = {k.strip(): (v or '').strip() for k, v in r.items()}
    if r['Series'] and float(r['Issue Size'] or 0) > 0:
        shares_by_symbol[r['Symbol']] = float(r['Issue Size'])

# ------------------------------------------------------------------- prices and features
px = (pl.scan_parquet(prices_path).filter(pl.col('series') == 'EQ')
      .select('sid', 'symbol', 'day', 'close', 'aclose', 'value').collect()
      .unique(['sid', 'day']).sort('sid', 'day'))
last_day = px['day'].max()
# A stock's share count applies to the sid trading under that symbol on the last day.
latest = px.filter(pl.col('day') == last_day).select('sid', 'symbol')
shares = {s: shares_by_symbol[sym] for s, sym in latest.iter_rows() if sym in shares_by_symbol}
# Adjusted closes are on the latest basis (checked below), so shares x aclose is the market
# cap on any earlier date.
basis = px.filter(pl.col('day') == last_day)
print(f"adjusted = unadjusted on {last_day}: "
      f"{(basis['aclose'] - basis['close']).abs().lt(1e-6).mean():.1%} of stocks")
print(f"share counts for {len(shares)} of {latest.height} stocks trading on {last_day}")

days = px.select('day').unique().sort('day')
MES = (days.with_columns(pl.col('day').dt.strftime('%Y-%m').alias('m'))
       .group_by('m').agg(pl.col('day').max()).sort('m')['day'].to_list())
px = px.with_columns(
    (pl.col('aclose') / pl.col('aclose').shift(1).over('sid')).log().alias('lr'),
    pl.int_range(pl.len()).over('sid').alias('row'),
    pl.col('aclose').rolling_mean(126, min_samples=100).over('sid').alias('mean126'))
px = px.with_columns(
    (pl.col('lr').rolling_std(252, min_samples=200).over('sid') * math.sqrt(252)).alias('vol'),
    pl.col('aclose').shift(126).over('sid').alias('c126'),
    pl.col('aclose').shift(252).over('sid').alias('c252'))
me_of = pl.DataFrame({'me': MES}).with_columns(pl.col('me').dt.strftime('%Y-%m').alias('m'))
pxm = px.with_columns(pl.col('day').dt.strftime('%Y-%m').alias('m')).join(me_of, on='m')
last_in_month = (pxm.group_by('sid', 'm').agg(pl.col('day').max()).join(me_of, on='m')
                 .filter(pl.col('day') >= pl.col('me').dt.offset_by('-5d')))
at_me = pxm.join(last_in_month.select('sid', 'day'), on=['sid', 'day']).select(
    'sid', 'symbol', 'me', 'aclose', 'vol', 'row', 'mean126',
    (pl.col('aclose') / pl.col('c126') - 1).alias('ret126'),
    (pl.col('aclose') / pl.col('c252') - 1).alias('ret252'))
tgt = pl.DataFrame({'me': MES}).with_columns(pl.col('me').shift(-1).alias('to')).drop_nulls()
a = at_me.select('sid', 'me', 'aclose').join(tgt, on='me')
a = a.sort('to').join_asof(px.select('sid', pl.col('day').alias('last_day'),
                                     pl.col('aclose').alias('end')).sort('last_day'),
                           left_on='to', right_on='last_day', by='sid', strategy='backward')
a = a.filter(pl.col('last_day') >= pl.col('me'))
fwd = a.select('sid', 'me', (pl.col('end') / pl.col('aclose') - 1).alias('fwd'))
feat = at_me.join(fwd, on=['sid', 'me'], how='left').with_columns(
    (pl.col('ret126') / pl.col('vol')).alias('ra6'),
    (pl.col('ret252') / pl.col('vol')).alias('ra12'),
    pl.col('sid').replace_strict(shares, default=None, return_dtype=pl.Float64)
      .alias('shares'))
feat = feat.with_columns((pl.col('shares') * pl.col('aclose')).alias('mcap'),
                         (pl.col('shares') * pl.col('mean126')).alias('avg_mcap'))
F = {me: g for (me,), g in feat.group_by('me')}
FWD = {me: dict(zip(g['sid'].to_list(), g['fwd'].to_list(), strict=True)) for me, g in F.items()}

uni500 = pl.read_parquet(uni_path).select(pl.col('day').alias('me'), 'sid')
pred = pl.read_parquet(pred_path).select('sid', 'me', 'fwd_1m', 'p_momentum')
U500 = {me: g.join(pred, on=['sid', 'me'], how='left')
        for (me,), g in uni500.group_by('me')}
MED = (pl.scan_parquet(prices_path).filter(pl.col('series') == 'EQ')
       .select('sid', 'day', 'med126').collect())


# ------------------------------------------------------------------- universes and scores

def cap200(me):
    """The 200 largest by average full market cap over 126 sessions, among stocks with a
    year of sessions and a share count."""
    g = F[me].filter((pl.col('row') >= 252) & pl.col('avg_mcap').is_not_null()
                     & pl.col('ra6').is_not_null() & pl.col('ra12').is_not_null())
    return g.sort(['avg_mcap', 'sid'], descending=[True, False]).head(200)


def most_traded(me):
    """M's universe at me, with M's own momentum percentile."""
    g = U500[me].join(F[me].select('sid', 'ra6', 'ra12', 'mcap', 'avg_mcap'), on='sid',
                      how='left')
    return g


def nse_score(g):
    def zs(c):
        x = g[c].to_numpy()
        return np.clip((x - np.nanmean(x)) / np.nanstd(x), -3, 3)
    zz = 0.5 * zs('ra6') + 0.5 * zs('ra12')
    return np.where(zz >= 0, 1 + zz, 1 / (1 - zz))


def app_score(g):
    """research/momentum_upgrades' p_momentum, computed within universe g."""
    def pct(c):
        s = pl.Series(g[c]).rank('average').to_numpy()
        n = np.sum(~np.isnan(g[c].to_numpy()))
        return (s - 1) / (n - 1)
    return 0.5 * pct('ra6') + 0.5 * pct('ra12')


def select(g, score, n, prev=None, buffer=False):
    ids = g['sid'].to_list()
    # Stocks without a score are left out before sorting: a NaN in the sort key would
    # scramble the whole order (the first run's bug, README).
    have = [i for i in range(len(ids)) if not np.isnan(score[i])]
    order = [ids[i] for i in sorted(have, key=lambda i: (-score[i], ids[i]))]
    if not buffer or prev is None:
        return order[:n]
    rank = {s: r + 1 for r, s in enumerate(order)}
    picked = order[:N_ALWAYS]
    picked += [s for s in order if s in prev and s not in picked and rank[s] <= N_KEEP]
    picked = sorted(picked, key=lambda s: rank[s])[:n]
    picked += [s for s in order if s not in picked][:n - len(picked)]
    return picked


def cap_weights(g, score, chosen):
    """Market cap x score, capped at the lower of 5% and 5x the universe weight."""
    mc = dict(zip(g['sid'].to_list(), g['mcap'].to_list(), strict=True))
    sc = dict(zip(g['sid'].to_list(), score.tolist(), strict=True))
    total = sum(v for v in mc.values() if v)
    raw = {s: mc[s] * sc[s] for s in chosen}
    cap = {s: min(CAP_ABS, CAP_MULT * mc[s] / total) for s in chosen}
    w = {s: v / sum(raw.values()) for s, v in raw.items()}
    for _ in range(100):
        over = {s for s in w if w[s] > cap[s] + 1e-12}
        if not over:
            break
        excess = sum(w[s] - cap[s] for s in over)
        free = {s for s in w if s not in over and w[s] < cap[s]}
        for s in over:
            w[s] = cap[s]
        base = sum(w[s] for s in free)
        if base <= 0:
            break
        for s in free:
            w[s] += excess * w[s] / base
    return w


# ------------------------------------------------------------------- the simulation

MONTHS = [(a, b) for a, b in zip(MES[:-1], MES[1:], strict=True) if FIRST <= a and b <= LAST]


def simulate(target, semiannual, costs=False):
    """Month-end portfolio values from FIRST. target(me, prev) gives the weights to hold
    from me's close. Semi-annual: rebalanced at June and December month ends with the
    previous month end's data; monthly: at every month end with that month end's data."""
    w, prev, rets, contrib = {}, None, [], []
    for me, _nxt in MONTHS:
        if semiannual:
            if me.month in (6, 12):
                cut = MES[MES.index(me) - 1]
                new = target(cut, prev)
            else:
                new = None
        else:
            new = target(me, prev)
        cost = 0.0
        if new is not None:
            if costs:
                turnover = sum(abs(new.get(s, 0) - w.get(s, 0)) for s in set(new) | set(w))
                cost = COST * turnover
            w, prev = dict(new), set(new)
        f = FWD.get(me, {})
        r = {s: (f.get(s) if f.get(s) is not None and not math.isnan(f.get(s)) else 0.0)
             for s in w}
        port = sum(w[s] * r[s] for s in w)
        rets.append((1 - cost) * (1 + port) - 1)
        contrib += [(me, s, w[s], r[s], w[s] * r[s]) for s in w]
        grown = {s: w[s] * (1 + r[s]) for s in w}
        tot = sum(grown.values())
        w = {s: v / tot for s, v in grown.items()} if tot > 0 else w
    return np.array(rets), contrib


def ann(r):
    return float(np.prod(1 + r) ** (12 / len(r)) - 1)


def strategy(universe, score_fn, n, weights, buffer):
    def target(me, prev):
        g = universe(me)
        if weights == 'cap':      # only a stock with a share count can have a cap weight
            g = g.filter(pl.col('mcap').is_not_null())
        sc = score_fn(g)
        chosen = select(g, sc, n, prev, buffer)
        if weights == 'equal':
            return {s: 1 / len(chosen) for s in chosen}
        return cap_weights(g, sc, chosen)
    return target


def m_score(g):
    return g['p_momentum'].to_numpy().astype(float)


# ------------------------------------------------------------------- the index
idx = pl.read_parquet(index_path).filter(pl.col('index') == 'Nifty200 Momentum 30')
level = dict(zip(idx['day'].to_list(), idx['close'].to_list(), strict=True))
A = np.array([level[b] / level[a] - 1 for a, b in MONTHS])

steps = {
    'B0 the index rebuilt': (strategy(cap200, nse_score, N_INDEX, 'cap', True), True, False),
    'B0 without the buffer': (strategy(cap200, nse_score, N_INDEX, 'cap', False), True, False),
    'B1 equal weights': (strategy(cap200, nse_score, N_INDEX, 'equal', True), True, False),
    'B2 20 stocks, no buffer': (strategy(cap200, nse_score, 20, 'equal', False), True, False),
    'B3 monthly at the signal close': (strategy(cap200, nse_score, 20, 'equal', False), False,
                                       False),
    "B4 M's score": (strategy(cap200, app_score, 20, 'equal', False), False, False),
    "B5 M's universe (M before costs)": (strategy(most_traded, m_score, 20, 'equal', False),
                                         False, False),
    'B6 costs (M)': (strategy(most_traded, m_score, 20, 'equal', False), False, True),
}
alt = {
    "U  the index on M's universe": (strategy(most_traded, nse_score, N_INDEX, 'cap', True),
                                     True, False),
    'U1 equal weights': (strategy(most_traded, nse_score, N_INDEX, 'equal', True), True,
                         False),
    'U2 20 stocks, no buffer': (strategy(most_traded, nse_score, 20, 'equal', False), True,
                                False),
    'U3 monthly at the signal close': (strategy(most_traded, nse_score, 20, 'equal', False),
                                       False, False),
    "U4 M's score (= B5)": (strategy(most_traded, m_score, 20, 'equal', False), False,
                            False),
}

print(f"\nMonths: {MONTHS[0][0]} to {MONTHS[-1][1]}, {len(MONTHS)}")
print(f"{'A  Nifty200 Momentum 30 (price)':42s} {ann(A):+7.1%}")
results, contribs = {}, {}
for block in (steps, alt):
    prev_ann = None
    for name, (target, semi, costs) in block.items():
        r, c = simulate(target, semi, costs)
        results[name], contribs[name] = r, c
        a_ = ann(r)
        extra = ''
        if name.startswith('B0'):
            d = r - A
            te = float(np.std(d, ddof=1) * math.sqrt(12))
            extra = (f"  vs A {a_ - ann(A):+6.1%}, corr {np.corrcoef(r, A)[0, 1]:.2f}, "
                     f"tracking error {te:.1%}")
        elif prev_ann is not None:
            extra = f"  step {a_ - prev_ann:+6.1%}"
        print(f"{name:42s} {a_:+7.1%}{extra}")
        if name != 'B0 without the buffer':
            prev_ann = a_
    print()

# ------------------------------------------------------------------- years
print('By calendar year (price returns):')
names = ['A', 'B0 the index rebuilt', 'B2 20 stocks, no buffer', 'B3 monthly at the signal close',
         "B5 M's universe (M before costs)"]
series = {'A': A, **{n: results[n] for n in names[1:]}}
for y in range(2021, 2027):
    ks = [i for i, (a_, b_) in enumerate(MONTHS) if b_.year == y]
    print(y, '  '.join(f"{n[:3]} {np.prod(1 + series[n][ks]) - 1:+7.1%}" for n in names))

# ------------------------------------------------------------------- survivorship
gaps = []
for me in sorted({a_ for a_, b_ in MONTHS} | {MES[MES.index(MONTHS[0][0]) - 1]}):
    top = (MED.filter(pl.col('day') == me).sort('med126', descending=True, nulls_last=True)
           .head(200)['sid'].to_list())
    gaps.append(sum(1 for s in top if s not in shares))
print(f"\nOf the 200 most traded at each month end, without an October 2026 share count: "
      f"{min(gaps)} to {max(gaps)} (median {int(np.median(gaps))})")

# ------------------------------------------------------------------- M's largest moves
c = pl.DataFrame(contribs["B5 M's universe (M before costs)"],
                 schema=['me', 'sid', 'w', 'r', 'contribution'], orient='row')
sym = px.group_by('sid').agg(pl.col('symbol').last())
big = c.sort('contribution', descending=True).head(15).join(sym, on='sid', how='left')
print("\nB5's 15 largest single-stock monthly contributions:")
for row in big.iter_rows(named=True):
    nxt = MES[MES.index(row['me']) + 1]
    seg = px.filter((pl.col('sid') == row['sid']) & (pl.col('day') >= row['me'])
                    & (pl.col('day') <= nxt)).sort('day')
    raw = seg['close'][-1] / seg['close'][0] - 1
    print(f"  {row['me']} {row['symbol']:12s} weight {row['w']:.1%} return {row['r']:+7.1%} "
          f"contribution {row['contribution']:+.2%}; unadjusted close {seg['close'][0]:.2f} -> "
          f"{seg['close'][-1]:.2f} ({raw:+.1%}), {seg.height} sessions")
pl.DataFrame({n: r for n, r in results.items()} | {'A': A,
             'month_end': [b_ for _, b_ in MONTHS]}).write_parquet('gap_monthly.parquet')
