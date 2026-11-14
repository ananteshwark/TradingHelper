"""Exit rules of research/exits/PROTOCOL.md on the frozen ML intraday calls.

Usage:
  uv run --with lightgbm python -I exits.py CANDLES PRED FEATURES dev
  uv run --with lightgbm python -I exits.py CANDLES PRED FEATURES holdout T=.. B=.. S=.. M=.. D=..

Each invocation prints only the period it is asked for. A choice of 'none' skips a family.
"""
import datetime as dt
import math
import sys
from decimal import Decimal

import numpy as np
import polars as pl

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.intraday.costs import charges, intraday_rates      # noqa: E402

candles_path, pred_path, feat_path, period = sys.argv[1:5]
choice = dict(a.split('=') for a in sys.argv[5:])
SLIP, GATE, NOTIONAL = 0.0002, 0.0015, 100_000
ENTRY_K, EXIT_K = 7, 72                    # the 09:50 and 15:15 candles
CHECKS = list(range(15, 64, 6))            # 10:30, 11:00, ... 14:30
K = 75
rates = intraday_rates()
DEV, HOLD = (dt.date(2023, 1, 1), dt.date(2024, 12, 31)), (dt.date(2025, 1, 1), dt.date(2026, 12, 31))

# ------------------------------------------------------------------ dense candle arrays
c = pl.read_parquet(candles_path)
nifty = c.filter(pl.col('symbol') == 'NIFTY')
c = c.filter(pl.col('symbol') != 'NIFTY')
keys = c.select('symbol', 'day').unique().sort('symbol', 'day').with_row_index('sd')
c = c.join(keys, on=['symbol', 'day'])
N = keys.height
arr = {}
for col in ('o', 'h', 'l', 'c', 'v'):
    a = np.full((N, K), np.nan, dtype=np.float64)
    a[c['sd'].to_numpy(), c['k'].to_numpy()] = c[col].to_numpy()
    arr[col] = a
O, H, L, C, V = (arr[k] for k in 'ohlcv')
sd_of = {(s, d): i for i, s, d in keys.select('sd', 'symbol', 'day').iter_rows()}
day_of_sd = keys['day'].to_list()
ndays = sorted(nifty['day'].unique())
nrow = {d: i for i, d in enumerate(ndays)}
NC = np.full((len(ndays), K), np.nan)
NO = np.full((len(ndays), K), np.nan)
ni = nifty['day'].replace_strict(nrow, return_dtype=pl.Int64).to_numpy()
NC[ni, nifty['k'].to_numpy()] = nifty['c'].to_numpy()
NO[ni, nifty['k'].to_numpy()] = nifty['o'].to_numpy()


def ffill_row(a: np.ndarray) -> np.ndarray:
    """Carry each row's last value forward over missing candles."""
    out = a.copy()
    for k in range(1, a.shape[1]):
        m = np.isnan(out[:, k])
        out[m, k] = out[m, k - 1]
    return out


Cf = ffill_row(C)
NCf = ffill_row(NC)

# ------------------------------------------------------------------ the ML exit model
FEATS = ['u', 'mfe', 'mae', 'r5', 'r15', 'r30', 'vwap_dist', 'range_pos', 'minutes_left',
         'atr', 'nifty_u', 'nifty_r30']


def features(sds: np.ndarray, atr: np.ndarray, k: int) -> np.ndarray:
    e = O[sds, ENTRY_K]
    last = Cf[sds, k - 1]
    with np.errstate(all='ignore'):
        hi = np.nanmax(H[sds, ENTRY_K:k], axis=1)
        lo = np.nanmin(L[sds, ENTRY_K:k], axis=1)
        tp = (H[sds, :k] + L[sds, :k] + C[sds, :k]) / 3
        vw = np.nansum(tp * V[sds, :k], axis=1) / np.nansum(V[sds, :k], axis=1)
        dhi = np.nanmax(H[sds, :k], axis=1)
        dlo = np.nanmin(L[sds, :k], axis=1)
        nd = np.array([nrow.get(day_of_sd[s], -1) for s in sds])
        ok = nd >= 0
        nu = np.full(len(sds), np.nan)
        nr = np.full(len(sds), np.nan)
        nu[ok] = NCf[nd[ok], k - 1] / NO[nd[ok], ENTRY_K] - 1
        nr[ok] = NCf[nd[ok], k - 1] / NCf[nd[ok], k - 7] - 1
        return np.column_stack([
            last / e - 1, hi / e - 1, lo / e - 1,
            last / Cf[sds, k - 2] - 1, last / Cf[sds, k - 4] - 1, last / Cf[sds, k - 7] - 1,
            last / vw - 1, (last - dlo) / (dhi - dlo), np.full(len(sds), (EXIT_K - k) * 5.0),
            atr, nu, nr])


feat_tbl = pl.read_parquet(feat_path).select('symbol', 'day', 'atr_pct')
rows_tbl = feat_tbl.join(keys, on=['symbol', 'day'], how='inner')
rows_tbl = rows_tbl.filter(pl.Series(~np.isnan(O[rows_tbl['sd'].to_numpy(), ENTRY_K])
                                     & ~np.isnan(O[rows_tbl['sd'].to_numpy(), EXIT_K])))


def training_set(lo: dt.date, hi: dt.date):
    t = rows_tbl.filter(pl.col('day').is_between(lo, hi))
    sds, atr = t['sd'].to_numpy(), t['atr_pct'].to_numpy()
    X, y = [], []
    for k in CHECKS:
        ok = ~np.isnan(O[sds, k])
        X.append(features(sds[ok], atr[ok], k))
        y.append(np.clip(O[sds[ok], EXIT_K] / O[sds[ok], k] - 1, -0.05, 0.05))
    return np.vstack(X), np.concatenate(y)


def model(train_to: int):
    import lightgbm as lgb
    X, y = training_set(dt.date(2022, 1, 1), dt.date(train_to, 12, 31))
    m = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.03, num_leaves=31,
                          min_child_samples=1000, colsample_bytree=0.8, subsample=0.8,
                          subsample_freq=1, random_state=7, verbose=-1)
    m.fit(X, y)
    return m, len(y)


# ------------------------------------------------------------------ the calls
lo, hi = DEV if period == 'dev' else HOLD
pred = pl.read_parquet(pred_path).filter(pl.col('day').is_between(lo, hi))
calls = []
for (day,), g in pred.group_by(['day'], maintain_order=True):
    g = g.sort('gbm')
    for side, sel in ((1, g.filter(pl.col('gbm') > GATE).tail(3)),
                      (-1, g.filter(pl.col('gbm') < -GATE).head(3))):
        calls += [(r['symbol'], day, side) for r in sel.iter_rows(named=True)]
calls = (pl.DataFrame(calls, orient='row', schema=[('symbol', pl.Utf8), ('day', pl.Date),
                                                   ('side', pl.Int64)])
         .join(feat_tbl, on=['symbol', 'day'], how='left'))
calls = calls.with_columns(pl.struct('symbol', 'day').map_elements(
    lambda r: sd_of.get((r['symbol'], r['day']), -1), return_dtype=pl.Int64).alias('sd'))
calls = calls.filter((pl.col('sd') >= 0) & pl.col('atr_pct').is_not_null())
calls = calls.filter(pl.Series(~np.isnan(O[calls['sd'].to_numpy(), ENTRY_K])
                               & ~np.isnan(O[calls['sd'].to_numpy(), EXIT_K])))
days = sorted(pred['day'].unique())

# Predictions of the exit model for every call and decision time, walk-forward by year.
PRED = {}
if period == 'dev' or choice.get('M', 'none') != 'none':
    years = [(2023, 2022), (2024, 2023)] if period == 'dev' else [(None, 2024)]
    for year, train_to in years:
        m, n = model(train_to)
        print(f'exit model trained on 2022-{train_to}: {n} rows', flush=True)
        sel = calls if year is None else calls.filter(pl.col('day').dt.year() == year)
        sds, atr = sel['sd'].to_numpy(), sel['atr_pct'].to_numpy()
        for k in CHECKS:
            p = m.predict(features(sds, atr, k))
            for s, v in zip(sds.tolist(), p.tolist(), strict=True):
                PRED[(s, k)] = v


# ------------------------------------------------------------------ simulation
def simulate(variant: str):
    fam, par = variant[0], variant[1:]
    fixed = variant != 'X0' and fam != 'T'
    out = []
    for day, g in calls.group_by('day', maintain_order=True):
        day = day[0]
        tr = []
        for sym, side, atr, sd in g.select('symbol', 'side', 'atr_pct', 'sd').iter_rows():
            e = O[sd, ENTRY_K]
            m = {'T': float(par or 0) / 10 if fam == 'T' else 0}
            stop = (e * (1 - side * 0.5 * atr) if fixed else
                    e * (1 - side * m['T'] * atr) if fam == 'T' else None)
            tr.append({'sym': sym, 'side': side, 'atr': atr, 'sd': sd, 'e': e, 'stop': stop,
                       'best': e, 'x': None, 'early': False})
        flag = False
        for k in range(ENTRY_K, EXIT_K + 1):
            for t in tr:
                if t['x'] is not None:
                    continue
                o = O[t['sd'], k]
                if k == EXIT_K:
                    t['x'] = o
                    continue
                if np.isnan(o):
                    continue
                side, e = t['side'], t['e']
                if k > ENTRY_K:                     # decisions at the open
                    losing = side * (Cf[t['sd'], k - 1] / e - 1) < 0
                    if (fam == 'S' and k == {'12': 33, '13': 45}[par] and losing) or flag:
                        t['x'], t['early'] = o, True
                        continue
                    if fam == 'M' and k in CHECKS:
                        tau = {'0': 0.0, '10': -0.001}[par]
                        if side * PRED[(t['sd'], k)] < tau:
                            t['x'], t['early'] = o, True
                            continue
                if t['stop'] is not None:           # the stop during the candle
                    if side == 1 and L[t['sd'], k] <= t['stop']:
                        t['x'], t['early'] = min(o, t['stop']), True
                        continue
                    if side == -1 and H[t['sd'], k] >= t['stop']:
                        t['x'], t['early'] = max(o, t['stop']), True
                        continue
                # After the candle: move a trailing or break-even stop.
                ext = H[t['sd'], k] if side == 1 else L[t['sd'], k]
                if not np.isnan(ext):
                    t['best'] = max(t['best'], ext) if side == 1 else min(t['best'], ext)
                if fam == 'T':
                    new = t['best'] * (1 - side * m_of(par) * t['atr'])
                    t['stop'] = max(t['stop'], new) if side == 1 else min(t['stop'], new)
                if fam == 'B' and side * (t['best'] / e - 1) >= {'05': 0.5, '10': 1.0}[par] * t['atr']:
                    t['stop'] = max(t['stop'], e) if side == 1 else min(t['stop'], e)
            if fam == 'D' and not flag and k < EXIT_K:
                marks = [t['side'] * ((t['x'] if t['x'] is not None else Cf[t['sd'], k]) / t['e'] - 1)
                         for t in tr]
                flag = np.nanmean(marks) <= {'05': -0.005, '10': -0.010}[par]
        for t in tr:
            side = t['side']
            e = t['e'] * (1 + side * SLIP)
            x = t['x'] * (1 - side * SLIP)
            qty = int(NOTIONAL // e)
            if qty < 1:
                continue
            cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                                 'buy' if side == 1 else 'sell', rates))
            pnl = side * (x - e) * qty - cost
            out.append({'day': day, 'side': side, 'pnl': pnl, 'net_pct': pnl / (e * qty) * 100,
                        'early': t['early']})
    return pl.DataFrame(out)


def m_of(par: str) -> float:
    return {'05': 0.5, '10': 1.0}[par]


def daily(t: pl.DataFrame) -> np.ndarray:
    return (pl.DataFrame({'day': days}).join(t.group_by('day').agg(pl.col('pnl').sum()),
                                             on='day', how='left').fill_null(0)['pnl'].to_numpy())


def summary(name: str, t: pl.DataFrame) -> dict:
    d = daily(t)
    eq = np.cumsum(d)
    return {'variant': name, 'trades': t.height, 'net_%_trade': round(t['net_pct'].mean(), 3),
            'win': round((t['pnl'] > 0).mean(), 3), 'daily_Rs': round(d.mean()),
            't_daily': round(d.mean() / d.std(ddof=1) * math.sqrt(len(d)), 2),
            'max_drawdown_Rs': round((eq - np.maximum.accumulate(eq)).min()),
            'worst_day_Rs': round(d.min()), 'worst_trade_%': round(t['net_pct'].min(), 2),
            'early_exit_%': round(t['early'].mean() * 100)}


def boot(a: np.ndarray, b: np.ndarray, block: int = 21, draws: int = 2000) -> float:
    rng = np.random.default_rng(7)
    n, wins = len(a), 0
    for _ in range(draws):
        starts = rng.integers(0, n, size=math.ceil(n / block))
        take = ((starts[:, None] + np.arange(block)[None, :]) % n).ravel()[:n]
        wins += a[take].mean() > b[take].mean()
    return round(wins / draws, 3)


names = (['X0', 'F', 'T05', 'T10', 'B05', 'B10', 'S12', 'S13', 'M0', 'M10', 'D05', 'D10']
         if period == 'dev' else
         ['X0', 'F'] + [choice[f] for f in 'TBSMD' if choice.get(f, 'none') != 'none'])
print(f'{period}: {len(days)} sessions, {calls.height} calls', flush=True)
results = {n: simulate(n) for n in names}
base = daily(results['F'])
rows = []
for n in names:
    r = summary(n, results[n])
    if n not in ('X0', 'F'):
        r['boot_vs_F'] = boot(daily(results[n]), base)
    rows.append(r)
with pl.Config(tbl_rows=20, tbl_cols=14, tbl_width_chars=220):
    print(pl.DataFrame(rows))
