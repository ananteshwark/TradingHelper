"""H1-H5 of research/institutional/PROTOCOL.md on the survivorship-free daily panel.

Usage:
  uv run python -I portfolios.py BUILD_DIR dev
  uv run python -I portfolios.py BUILD_DIR holdout H1=<63|126|none> H2=<a|b|none> H3=<a|b|none> [TRADES_OUT]

Each invocation prints only the period it is asked for.
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.momentum import cost_pct      # noqa: E402

base, period = Path(sys.argv[1]), sys.argv[2]
choice = dict(a.split('=') for a in sys.argv[3:] if '=' in a)
trades_out = next((a for a in sys.argv[3:] if '=' not in a), None)

CASH = 1.06 ** (1 / 250) - 1
TOP = 10

days = pl.read_parquet(base / 'days.parquet')['day'].to_list()
D = len(days)
panel = pl.read_parquet(base / 'panel.parquet').select('sid', 'symbol', 'di', 'mom')
px = pl.read_parquet(base / 'prices.parquet').sort('sid', 'di')
sids = px['sid'].unique().sort().to_list()
row = {s: i for i, s in enumerate(sids)}
N = len(sids)
AO = np.full((N, D), np.nan)
AC = np.full((N, D), np.nan)
RO = np.full((N, D), np.nan)
ri = px['sid'].replace_strict(row, return_dtype=pl.Int64).to_numpy()
di = px['di'].to_numpy()
AO[ri, di], AC[ri, di], RO[ri, di] = (px['aopen'].to_numpy(), px['aclose'].to_numpy(),
                                      px['open'].to_numpy())
symbol_of = dict(zip(px['sid'].to_list(), px['symbol'].to_list(), strict=False))
# Last close carried forward: the value of a holding on a day it didn't trade.
LC = pl.DataFrame(AC.T).with_columns(pl.all().fill_nan(None).forward_fill()).to_numpy().T
LC = LC.astype(float)
prevLC = np.full_like(LC, np.nan)
prevLC[:, 1:] = LC[:, :-1]
R = AC / prevLC - 1                       # daily return, NaN on days without a trade
nifty = row[next(s for s, y in symbol_of.items() if y == 'NIFTYBEES')]

# Month starts: entry sessions; the signal is the session before.
entries = [i for i in range(1, D) if days[i].month != days[i - 1].month
           and dt.date(2013, 2, 1) <= days[i] <= dt.date(2026, 10, 1)]
members = {d: g for (d,), g in panel.group_by(['di'])}


def window_stats(d: int, n: int, need: int) -> tuple[np.ndarray, np.ndarray]:
    w = R[:, max(0, d - n + 1):d + 1]
    cnt = np.sum(~np.isnan(w), axis=1)
    with np.errstate(all='ignore'):
        sd = np.nanstd(w, axis=1, ddof=1) * math.sqrt(250)
    sd[cnt < need] = np.nan
    return sd, cnt


def universe(sig: int) -> pl.DataFrame:
    g = members[sig].filter(pl.col('mom').is_not_null() & pl.col('mom').is_finite())
    r = np.array([row[s] for s in g['sid']])
    vol252, cnt = window_stats(sig, 252, 200)
    vol63, _ = window_stats(sig, 63, 50)
    g = g.with_columns(pl.Series('r', r), pl.Series('vol252', vol252[r]),
                       pl.Series('vol63', vol63[r]), pl.Series('n', cnt[r]))
    return g.filter((pl.col('n') >= 200) & pl.col('vol252').is_not_nan())


def pct(s: pl.Series) -> np.ndarray:
    return s.rank('average').to_numpy() / s.len()


def picks(sig: int) -> dict[str, dict[int, float]]:
    """Target stock weights (summing to 1) per portfolio at a signal."""
    u = universe(sig)
    by_mom = u.sort('mom', descending=True)
    out = {}
    out['M'] = {r: 1 / TOP for r in by_mom['r'][:TOP]}
    out['H5'] = {r: 1 / 30 for r in by_mom['r'][:30]}
    comp = u.with_columns(pl.Series('c', (pct(u['mom']) + pct(-u['vol252'])) / 2))
    out['H2a'] = {r: 1 / TOP for r in comp.sort('c', descending=True)['r'][:TOP]}
    calm = u.filter(pl.col('vol252') < u['vol252'].median()).sort('mom', descending=True)
    out['H2b'] = {r: 1 / TOP for r in calm['r'][:TOP]}
    out['LV'] = {r: 1 / TOP for r in u.sort('vol252')['r'][:TOP]}
    top = by_mom[:TOP]
    inv = 1 / np.where(np.isnan(top['vol63'].to_numpy()), top['vol252'].to_numpy(),
                       top['vol63'].to_numpy())
    out['H4'] = dict(zip(top['r'].to_list(), (inv / inv.sum()).tolist(), strict=True))
    out['basket'] = {r: 1 / u.height for r in u['r']}
    return out


def simulate(targets: list[dict[int, float]], shares: list[float], costs: bool = True):
    """Daily net returns from the first entry to the close before 1 October 2026, given
    each rebalance's stock weights and the share of the portfolio in stocks."""
    nav, units, cash = 1.0, {}, 1.0
    rets, invested, trades, skipped = [], [], [], 0
    first, last = entries[0], entries[-1]
    prev_close_nav = 1.0
    k = 0
    for d in range(first, last):
        if k < len(entries) and d == entries[k]:
            # Value the old holdings at today's open (else the last close).
            val = {r: u * (AO[r, d] if not np.isnan(AO[r, d]) else LC[r, d - 1])
                   for r, u in units.items()}
            nav = sum(val.values()) + cash
            share = shares[k]
            target = {r: w for r, w in targets[k].items() if not np.isnan(AO[r, d])}
            skipped += len(targets[k]) - len(target)
            scale = share * (sum(targets[k].values()) and 1 / sum(targets[k].values()))
            want = {r: nav * w * scale for r, w in target.items()}
            cost = 0.0
            for r, v in want.items():
                buy = v - val.get(r, 0.0)
                if buy > 0:
                    if costs:
                        cost += buy * cost_pct(RO[r, d]) / 100
                    trades.append((days[d], symbol_of[sids[r]], 'buy'))
            for r in val:
                if r not in want:
                    trades.append((days[d], symbol_of[sids[r]], 'sell'))
            units = {r: v / AO[r, d] for r, v in want.items()}
            cash = nav - sum(want.values()) - cost
            k += 1
        cash *= 1 + CASH
        close_nav = sum(u * LC[r, d] for r, u in units.items()) + cash
        rets.append(close_nav / prev_close_nav - 1)
        invested.append(1 - cash / close_nav)
        prev_close_nav = close_nav
    return np.array(rets), np.array(invested), trades, skipped


def metrics(r: np.ndarray, inv: np.ndarray) -> dict:
    nav = np.cumprod(1 + r)
    dd = nav / np.maximum.accumulate(nav) - 1
    return {'CAGR_%': round((nav[-1] ** (250 / len(r)) - 1) * 100, 1),
            'vol_%': round(r.std(ddof=1) * math.sqrt(250) * 100, 1),
            'Sharpe': round((r - CASH).mean() / r.std(ddof=1) * math.sqrt(250), 2),
            'maxDD_%': round(dd.min() * 100, 1),
            'invested_%': round(inv.mean() * 100)}


def boot(a: np.ndarray, b: np.ndarray, block: int = 63, draws: int = 2000) -> float:
    rng = np.random.default_rng(7)
    n = len(a)
    idx = np.arange(n)
    wins = 0
    for _ in range(draws):
        starts = rng.integers(0, n, size=math.ceil(n / block))
        take = ((starts[:, None] + np.arange(block)[None, :]) % n).ravel()[:n]
        x, y = a[idx[take]], b[idx[take]]
        sa = (x - CASH).mean() / x.std(ddof=1)
        sb = (y - CASH).mean() / y.std(ddof=1)
        wins += sa > sb
    return round(wins / draws, 3)


# ---------------------------------------------------------------- build every portfolio
sim_days = days[entries[0]:entries[-1]]
P = [picks(e - 1) for e in entries]
results = {}
for name in ('M', 'H2a', 'H2b', 'LV', 'H4', 'H5'):
    results[name] = simulate([p[name] for p in P], [1.0] * len(entries))
results['basket'] = simulate([p['basket'] for p in P], [1.0] * len(entries), costs=False)

# H1: scale M by min(1, target / sigma), sigma from M's past daily returns.
m_rets = results['M'][0]
pos = {d: i for i, d in enumerate(range(entries[0], entries[-1]))}
for L in (63, 126):
    sig_hist, shares = [], []
    for e in entries:
        i = pos[e - 1] if e - 1 in pos else None
        if i is None or i + 1 < L:
            shares.append(1.0)
            continue
        s = m_rets[i - L + 1:i + 1].std(ddof=1) * math.sqrt(250)
        shares.append(min(1.0, float(np.median(sig_hist)) / s) if len(sig_hist) >= 6 else 1.0)
        sig_hist.append(s)
    results[f'H1_{L}'] = simulate([p['M'] for p in P], shares)

# H3: hold M only in a market uptrend.
nb = LC[nifty]
for v in ('a', 'b'):
    shares = []
    for e in entries:
        s = e - 1
        if v == 'a':
            up = nb[s] > np.nanmean(AC[nifty, s - 199:s + 1])
        else:
            up = nb[s] / nb[s - 252] - 1 > 0
        shares.append(1.0 if up else 0.0)
    results[f'H3{v}'] = simulate([p['M'] for p in P], shares)

# NIFTYBEES held throughout, for context.
nb_ret = np.array([LC[nifty, d] / LC[nifty, d - 1] - 1 for d in range(entries[0], entries[-1])])
results['NIFTYBEES'] = (nb_ret, np.ones_like(nb_ret), [], 0)

# ---------------------------------------------------------------- report one period
lo, hi = ((dt.date(2013, 1, 1), dt.date(2020, 12, 31)) if period == 'dev'
          else (dt.date(2021, 1, 1), dt.date(2026, 9, 30)))
mask = np.array([lo <= d <= hi for d in sim_days])
print(f'{period}: {sim_days[int(np.argmax(mask))]} to {sim_days[len(mask) - 1 - int(np.argmax(mask[::-1]))]}, '
      f'{mask.sum()} sessions; cash {((1 + CASH) ** 250 - 1) * 100:.1f}% a year')
names = (['M', 'H1_63', 'H1_126', 'H2a', 'H2b', 'LV', 'H3a', 'H3b', 'H4', 'H5'] if period == 'dev'
         else ['M', *([f"H1_{choice['H1']}"] if choice['H1'] != 'none' else []),
               *([f"H2{choice['H2']}"] if choice['H2'] != 'none' else []), 'LV',
               *([f"H3{choice['H3']}"] if choice['H3'] != 'none' else []), 'H4', 'H5'])
names += ['basket', 'NIFTYBEES']
table = []
for n in names:
    r, inv, _, skipped = results[n]
    row_ = {'portfolio': n, **metrics(r[mask], inv[mask])}
    if n not in ('M', 'basket', 'NIFTYBEES'):
        row_['boot_vs_M'] = boot(r[mask], results['M'][0][mask])
    if n.startswith('H2'):
        row_['boot_vs_LV'] = boot(r[mask], results['LV'][0][mask])
    row_['skipped'] = skipped
    table.append(row_)
with pl.Config(tbl_rows=30, tbl_cols=12, tbl_width_chars=200):
    print(pl.DataFrame(table))
# Years, for the record.
yrs = sorted({d.year for d, m in zip(sim_days, mask, strict=True) if m})
print('Calendar-year returns, %:')
for n in names:
    r = results[n][0]
    out = []
    for y in yrs:
        sel = np.array([d.year == y for d in sim_days]) & mask
        out.append(f'{y}:{(np.prod(1 + r[sel]) - 1) * 100:+.0f}')
    print(f'  {n:10s} ' + ' '.join(out))
if trades_out:
    pl.DataFrame(results['M'][2], schema=['day', 'symbol', 'side'], orient='row').write_parquet(trades_out)
