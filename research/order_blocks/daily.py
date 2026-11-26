"""Daily order blocks for investing (PROTOCOL.md, test C).

Usage: uv run python -I daily.py PRICES PANEL BHAV PERIOD OUT_PARQUET
PRICES and PANEL are research/ml's adjusted daily prices and point-in-time universe (the
250 most-traded stocks each day); BHAV the parsed bhavcopies, for highs and lows.
PERIOD is dev (entries 2013-2019) or holdout (2020 - July 2026).
"""
import datetime as dt
import sys

import numpy as np
import polars as pl

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.momentum import cost_pct  # noqa: E402

prices_path, panel_path, bhav_path, period, out = sys.argv[1:6]
FIRST, LAST = {'dev': (dt.date(2013, 1, 1), dt.date(2019, 12, 31)),
               'holdout': (dt.date(2020, 1, 1), dt.date(2026, 7, 31))}[period]
HOLD, WINDOW, THROUGH = 63, 20, 0.0002

prices = pl.read_parquet(prices_path)
bhav = (pl.scan_parquet(bhav_path).filter(pl.col('series').is_in(['EQ', 'BE']))
        .select('day', 'symbol', 'high', 'low').unique(['day', 'symbol']).collect())
prices = prices.join(bhav, on=['day', 'symbol'], how='left').with_columns(
    (pl.col('aclose') / pl.col('close')).alias('f'))
prices = prices.with_columns((pl.col('high') * pl.col('f')).alias('ahigh'),
                             (pl.col('low') * pl.col('f')).alias('alow'))
days = prices.select('di', 'day').unique().sort('di')
day_of = dict(days.iter_rows())
D = int(prices['di'].max()) + 1

panel = pl.read_parquet(panel_path, columns=['sid', 'di'])
members = {}
for sid, di in panel.iter_rows():
    members.setdefault(di, []).append(sid)
in_uni = set(panel.iter_rows())

# The equal-weighted universe, open to open, as an index over market days.
ao_all = {}
for sid, grp in prices.group_by('sid'):
    a = np.full(D, np.nan)
    a[grp['di'].to_numpy()] = grp['aopen'].to_numpy()
    ao_all[sid[0]] = a
u = np.zeros(D)
for d in range(D - 1):
    rets = [ao_all[s][d + 1] / ao_all[s][d] - 1 for s in members.get(d, [])
            if not np.isnan(ao_all[s][d]) and not np.isnan(ao_all[s][d + 1])]
    u[d] = np.mean(rets) if rets else 0.0
index = np.concatenate([[1.0], np.cumprod(1 + u)])[:D]

rows = []
for (sid,), g in prices.sort('sid', 'di').group_by('sid', maintain_order=True):
    g = g.drop_nulls(['ahigh', 'alow', 'aopen', 'aclose'])
    if g.height < 30:
        continue
    di = g['di'].to_numpy()
    o, h, lo, c = (g[k].to_numpy() for k in ('aopen', 'ahigh', 'alow', 'aclose'))
    raw_open = g['open'].to_numpy()
    sym = g['symbol'].to_list()
    n = len(di)
    swing = np.zeros(n, bool)
    swing[2:n - 2] = ((h[2:n - 2] > h[:n - 4]) & (h[2:n - 2] > h[1:n - 3])
                      & (h[2:n - 2] > h[3:n - 1]) & (h[2:n - 2] > h[4:]))
    rng = h - lo
    busy = {'C0': -1, 'C1': -1}       # position up to which each variant is engaged
    latest = np.nan
    for t in range(3, n - 1):
        m = t - 3                    # a swing at m is known once m + 2 closed, before t
        if swing[m]:
            latest = h[m]
        if np.isnan(latest) or not (c[t] > latest >= c[t - 1]):
            continue
        # Periods go by entry day; a break up to 20 sessions earlier can enter in one.
        if not (FIRST - dt.timedelta(days=45) <= day_of[di[t]] <= LAST) or \
                (sid, di[t]) not in in_uni:
            continue
        reds = [b for b in range(max(0, t - 10), t) if c[b] < o[b]]
        if not reds:
            continue
        b = reds[-1]
        gap = any(lo[k] > h[k - 2] for k in range(b + 2, t + 1))
        if not (gap or c[t] - h[b] >= rng[max(0, t - 14):t].mean()):
            continue
        z, s1 = h[b], lo[b]
        for variant in ('C0', 'C1'):
            if t <= busy[variant]:
                continue
            e, entry = None, None
            if variant == 'C0':
                e, entry = t + 1, o[t + 1]
            else:
                for s in range(t + 1, min(n, t + 1 + WINDOW)):
                    if o[s] < s1:
                        break
                    if lo[s] <= z * (1 - THROUGH):
                        e, entry = s, min(z, o[s])
                        break
                if e is None:
                    busy[variant] = min(n - 1, t + WINDOW)
                    continue
            last = e + HOLD
            if last >= n:
                break
            x, kind = last, 'time'
            for q in range(e, last):
                if c[q] < s1:
                    x, kind = q + 1, 'stop'
                    break
            busy[variant] = x
            if not FIRST <= day_of[di[e]] <= LAST:
                continue
            ret = o[x] / entry - 1
            bench = index[di[x]] / index[di[e]] - 1
            fee = cost_pct(raw_open[e]) / 100
            rows.append({'variant': variant, 'sid': sid, 'symbol': sym[t],
                         'break_day': day_of[di[t]], 'entry_day': day_of[di[e]],
                         'exit_day': day_of[di[x]], 'exit_kind': kind, 'z': z, 's1': s1,
                         'entry': entry, 'exit': o[x], 'ret': ret, 'bench': bench,
                         'cost': fee, 'excess': ret - bench - fee,
                         'risk': (entry - s1) / entry})

res = pl.DataFrame(rows)
res.write_parquet(out)
for variant in ('C0', 'C1'):
    r = res.filter(pl.col('variant') == variant)
    monthly = r.group_by(pl.col('entry_day').dt.truncate('1mo')).agg(pl.col('excess').mean())
    x = monthly['excess']
    t = x.mean() / x.std() * np.sqrt(x.len())
    print(f"{period} {variant}: {r.height} trades in {x.len()} months; a trade "
          f"{r['ret'].mean() * 100:+.2f}% against the universe's "
          f"{r['bench'].mean() * 100:+.2f}%, net excess {r['excess'].mean() * 100:+.2f}% "
          f"(median {r['excess'].median() * 100:+.2f}%), {(r['excess'] > 0).mean():.0%} beat "
          f"it; monthly t {t:.2f}; stopped {(r['exit_kind'] == 'stop').mean():.0%}")
