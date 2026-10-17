"""Calling the bottom: research/bottoms/PROTOCOL.md.

Usage: uv run python -I bottoms.py SCRATCH dev
       uv run python -I bottoms.py SCRATCH confirm RULE=<R1..R6> H=<63|126>
SCRATCH holds ideas/promoter/all.parquet (+ all_days.parquet) and
research/parsed/deliv.parquet (see research/move_starts/README.md). Each run prints only
its period.
"""
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.momentum import cost_pct  # noqa: E402

S, mode = Path(sys.argv[1]), sys.argv[2]
args = dict(a.split('=') for a in sys.argv[3:])
RULES = ('R1', 'R2', 'R3', 'R4', 'R5', 'R6')
HS = (63, 126)

# ------------------------------------------------------------------ the panel
P = pl.read_parquet(S / 'ideas/promoter/all.parquet')
days = pl.read_parquet(S / 'ideas/promoter/all_days.parquet')['day'].to_list()
ND = len(days)
deliv = (pl.read_parquet(S / 'research/parsed/deliv.parquet')
         .select('day', 'symbol', 'series', 'deliv_qty'))
P = (P.join(deliv, on=['day', 'symbol', 'series'], how='left')
     .unique(['sid', 'di'], keep='first').sort('sid', 'di'))
sids = P['sid'].unique().sort().to_list()
rowof = {s: i for i, s in enumerate(sids)}
NS = len(sids)
P = P.with_columns(pl.col('sid').replace_strict(rowof, return_dtype=pl.Int64).alias('r'),
                   (pl.col('deliv_qty') * pl.col('close')).alias('dval'),
                   ((pl.col('rows252') >= 200) & (pl.col('close') >= 20)
                    & (pl.col('med126').fill_null(0) >= 2e7)).alias('ok'))

c = pl.col('aclose')
w = lambda e: e.over('r')                    # noqa: E731 - each stock's own sessions
P = P.with_columns(
    w(c.shift(1)).alias('c1'),
    w(c.rolling_max(250, min_samples=1)).alias('hi250'),
    w(c.rolling_min(250, min_samples=1)).alias('lo250'),
    w(c.rolling_min(120, min_samples=1)).alias('lo120'),
    w(c.rolling_mean(150, min_samples=150)).alias('ma150'),
    w(c.rolling_mean(50, min_samples=50)).alias('ma50'),
    w(pl.col('value').shift(1).rolling_mean(50, min_samples=50)).alias('vavg50'),
    w(pl.col('dval').rolling_mean(20, min_samples=15)).alias('d20'),
    w(pl.col('dval').shift(20).rolling_mean(120, min_samples=90)).alias('d120'),
    w(c.shift(1).rolling_max(30, min_samples=30)).alias('hi30'),
    w(c.shift(1).rolling_min(30, min_samples=30)).alias('lo30'),
    w(c.shift(20)).alias('c20'),
    w(c.shift(21)).alias('c21'),
    w(c.shift(252)).alias('c252'),
)
P = P.with_columns(
    w(pl.col('ma150').shift(1)).alias('ma150_1'),
    w(pl.col('ma150').shift(20)).alias('ma150_20'),
    w(pl.col('ma50').shift(10)).alias('ma50_10'),
    (pl.col('lo120') <= 0.70 * pl.col('hi250')).alias('fall'),
    (c / pl.col('c1') - 1).alias('ret1'),
)
# The market's day: the equal-weighted return of stocks investable on the day before.
mkt = (P.filter(w(pl.col('ok').shift(1)).fill_null(False))
       .group_by('di').agg(pl.col('ret1').mean().alias('mret')))
P = P.join(mkt, on='di', how='left').sort('r', 'di')

# Rules R1, R2, R4, R5 and R6 as columns; R3 below.
off = c >= 1.25 * pl.col('lo120')
P = P.with_columns(off.alias('off'))
P = P.with_columns(
    (pl.col('fall') & (c > pl.col('ma150')) & (pl.col('c1') <= pl.col('ma150_1'))
     & (pl.col('ma150') >= pl.col('ma150_20'))
     & (pl.col('value') >= 1.5 * pl.col('vavg50'))).alias('R1'),
    (pl.col('fall') & pl.col('off')
     & (w(pl.col('off').cast(pl.Int8).shift(1).rolling_max(63, min_samples=1)) == 0)
     & (c > pl.col('ma50')) & (pl.col('ma50') > pl.col('ma50_10'))).alias('R2'),
    (pl.col('fall') & (c <= 1.15 * pl.col('lo250')) & (pl.col('d20') >= 2 * pl.col('d120'))
     & (c >= pl.col('c20'))).alias('R4'),
    (pl.col('fall') & (pl.col('hi30') <= 1.15 * pl.col('lo30')) & (c > pl.col('hi30'))
     & (pl.col('value') >= 2 * pl.col('vavg50'))).alias('R5'),
    ((pl.col('c21') / pl.col('c252') - 1).alias('mom121')),
    ((c / pl.col('c21') - 1).alias('mom1')),
)
ranked = (P.filter(pl.col('ok') & pl.col('mom121').is_not_null() & pl.col('mom1').is_not_null())
          .with_columns(pl.col('mom121').rank().over('di').alias('k121'),
                        pl.col('mom1').rank().over('di').alias('k1'),
                        pl.len().over('di').alias('n'))
          .select('r', 'di', ((pl.col('k121') / pl.col('n') <= 0.30)
                              & (pl.col('k1') / pl.col('n') >= 0.70)).alias('rebound')))
P = P.join(ranked, on=['r', 'di'], how='left').sort('r', 'di').with_columns(
    pl.col('rebound').fill_null(False))
P = P.with_columns((pl.col('fall') & pl.col('rebound')
                    & ~w(pl.col('rebound').shift(1)).fill_null(False)).alias('R6'))


def mat(col, dtype=np.float32, fill=np.nan):
    m = np.full((NS, ND), fill, dtype=dtype)
    v = P[col].fill_null(False if dtype is bool else fill).to_numpy()
    m[P['r'].to_numpy(), P['di'].to_numpy()] = v
    return m


AO, AC, RO, MED = mat('aopen'), mat('aclose'), mat('open'), mat('med126')
OK = mat('ok', bool, False)
LC = pl.DataFrame(AC.T).with_columns(pl.all().fill_nan(None).forward_fill()).to_numpy().T
LC = LC.astype(np.float32)
E = {k: mat(k, bool, False) & OK for k in ('R1', 'R2', 'R4', 'R5', 'R6')}

# R3: a capitulation day, then a higher low and a new high since it, 5-40 sessions later.
cap = (pl.col('fall') & (c <= pl.col('lo250')) & (pl.col('value') >= 3 * pl.col('vavg50'))
       & ((pl.col('ret1') - pl.col('mret')) <= -0.05))
caps = P.filter(cap & pl.col('ok')).select('r', 'di').to_numpy()
R3 = np.zeros((NS, ND), bool)
for r, d in caps:
    base = AC[r, d]
    if not np.isfinite(base):
        continue
    for t in range(d + 1, min(d + 41, ND)):
        x = LC[r, d + 1:t + 1]
        if np.nanmin(x) < base:
            break
        if t - d >= 5 and LC[r, t] >= 1.10 * base and LC[r, t] >= np.nanmax(x) and OK[r, t]:
            R3[r, t] = True
            break
E['R3'] = R3

year = np.array([d.year for d in days])
DEV = (year >= 2013) & (year <= 2020)
CONF = year >= 2021

# ------------------------------------------------------------------ trading
bench_cache: dict = {}


def bench(e, H):
    if (e, H) not in bench_cache:
        rows = np.flatnonzero(OK[:, e - 1] & ~np.isnan(AO[:, e]))
        x = e + H
        b = np.where(np.isnan(AO[rows, x]), LC[rows, x - 1], AO[rows, x])
        bench_cache[(e, H)] = float(np.nanmean(b / AO[rows, e] - 1))
    return bench_cache[(e, H)]


def slip(med):
    return 0.3 if med < 1e8 else 0.1 if med < 5e8 else 0.0


def trades(Ev, H, period):
    """[(entry session, net %, gross abnormal %, row, signal session)] after each signal."""
    out, last = [], {}
    rs, ts = np.nonzero(Ev)
    for r, t in sorted(zip(rs.tolist(), ts.tolist(), strict=True), key=lambda x: x[1]):
        e = t + 1
        if e + H >= ND or not period[e] or np.isnan(AO[r, e]):
            continue
        if r in last and e < last[r] + H:
            continue
        last[r] = e
        x = e + H
        px = AO[r, x] if not np.isnan(AO[r, x]) else LC[r, x - 1]
        ab = (float(px / AO[r, e]) - 1 - bench(e, H)) * 100
        if not math.isfinite(ab):
            continue
        net = ab - cost_pct(float(RO[r, e])) - slip(float(MED[r, t]))
        out.append((e, net, ab, r, t))
    return out


def stats(tr):
    if len(tr) < 2:
        return None
    by: dict = {}
    for e, net, *_ in tr:
        by.setdefault((days[e].year, days[e].month), []).append(net)
    m = np.array([np.mean(v) for v in by.values()])
    yrs: dict = {}
    for e, net, *_ in tr:
        yrs.setdefault(days[e].year, []).append(net)
    t = m.mean() / m.std(ddof=1) * math.sqrt(len(m)) if len(m) > 1 and m.std(ddof=1) else 0
    return {'n': len(tr), 'per_trade': float(np.mean([x[1] for x in tr])),
            'monthly': float(m.mean()), 't': float(t), 'months': len(m),
            'years': {y: float(np.mean(v)) for y, v in sorted(yrs.items())}}


# ------------------------------------------------------------------ timing
def close_bench(s, H):
    rows = np.flatnonzero(OK[:, s] & np.isfinite(AC[:, s]))
    return float(np.nanmean(LC[rows, s + H] / AC[rows, s] - 1))


def up_starts(period):
    """Up-move starts of research/move_starts: +30% abnormal over 63 sessions from a close
    that is the lowest of the 21 around it."""
    out = []
    cache: dict = {}
    for s in np.flatnonzero(period):
        if s < 10 or s + 63 >= ND:
            continue
        rows = np.flatnonzero(OK[:, s] & np.isfinite(AC[:, s]))
        if s not in cache:
            cache[s] = close_bench(s, 63)
        ab = LC[rows, s + 63] / AC[rows, s] - 1 - cache[s]
        for r in rows[ab >= 0.30]:
            win = LC[r, s - 10:s + 11]
            if np.isfinite(win).all() and AC[r, s] <= win.min():
                out.append((r, s))
    return out


def timing(Ev, tr126, ups):
    dist, near, fell = [], 0, 0
    for e, _, _, r, t in tr126:
        lo = np.nanmin(LC[r, max(0, t - 63):t + 64])
        d = AO[r, e] / lo - 1
        dist.append(d)
        near += d <= 0.10
        fell += np.nanmin(LC[r, e:e + 63]) <= 0.9 * AO[r, e]
    big = sum(ab >= 30 for _, _, ab, _, _ in tr126)
    caught = 0
    for r, s in ups:
        hits = np.flatnonzero(Ev[r, s:s + 22])
        if any(np.isfinite(AO[r, s + h + 1]) and AO[r, s + h + 1] <= 1.15 * AC[r, s]
               for h in hits if s + h + 1 < ND):
            caught += 1
    n = len(tr126)
    return {'median_above_low': float(np.median(dist)) if dist else None,
            'within_10pct': near / n if n else None,
            'fell_10pct_first': fell / n if n else None,
            'big_rise_126': big / n if n else None,
            'ups_caught': caught / len(ups) if ups else None}


def show(name, s):
    yrs = ' '.join(f"{y}:{v:+.1f}" for y, v in s['years'].items())
    pos = sum(v > 0 for v in s['years'].values())
    print(f"{name:8s} trades {s['n']:6,d}  per trade {s['per_trade']:+6.2f}%  "
          f"monthly {s['monthly']:+6.2f}%  t {s['t']:5.2f}  months {s['months']:3d}  "
          f"positive years {pos}/{len(s['years'])}\n         {yrs}")


if mode == 'dev':
    print(f"Development 2013-2020. Investable stock-days: {int((OK & DEV[None, :]).sum()):,}")
    for k in RULES:
        print(f"{k}: signals {int((E[k] & DEV[None, :]).sum()):,}")
    print()
    best = None
    res = {}
    for k in RULES:
        for H in HS:
            s = stats(trades(E[k], H, DEV))
            res[(k, H)] = s
            if s is None:
                print(f'{k}:{H} too few trades')
                continue
            show(f'{k}:{H}', s)
            if s['monthly'] > 0 and (best is None or s['t'] > res[best]['t']):
                best = (k, H)
    print()
    ups = up_starts(DEV)
    print(f'Timing (126-session trades); up-move starts in 2013-2020: {len(ups):,}')
    print(f"{'rule':5s} {'median entry above the low':>27s} {'within 10% of it':>17s} "
          f"{'fell 10% more first':>20s} {'+30% vs market in 126':>22s} {'big rises caught':>17s}")
    for k in RULES:
        tm = timing(E[k], trades(E[k], 126, DEV), ups)
        print(f"{k:5s} {tm['median_above_low']:27.1%} {tm['within_10pct']:17.1%} "
              f"{tm['fell_10pct_first']:20.1%} {tm['big_rise_126']:22.1%} "
              f"{tm['ups_caught']:17.2%}")
    print()
    print('Chosen:', 'none (no positive mean)' if best is None else f'{best[0]}:{best[1]}')
elif mode == 'confirm':
    k, H = args['RULE'], int(args['H'])
    s = stats(trades(E[k], H, CONF))
    print(f'Confirmation 2021 to {days[-1]}, {k}:{H}, run once')
    show(f'{k}:{H}', s)
    pos = sum(v > 0 for v in s['years'].values())
    print('PASSED' if s['monthly'] > 0 and s['t'] >= 2 and pos >= 4 else 'NOT PASSED')
