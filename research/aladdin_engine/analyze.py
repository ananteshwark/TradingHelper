"""Summary of engine.py's months (PROTOCOL.md): Sharpe, return, risk, and the pass test.

Usage: uv run --no-project --with polars --with numpy python -I analyze.py MONTHS_PARQUET
       [CANDIDATE]
With CANDIDATE (an engine variant), Memmel's test of its Sharpe against M's.
"""
import math
import sys

import numpy as np
import polars as pl

CASH = 0.06 / 12
res = pl.read_parquet(sys.argv[1]).sort('strategy', 'me')
candidate = sys.argv[2] if len(sys.argv) > 2 else None


def sharpe(r):
    ex = r - CASH
    return ex.mean() / ex.std(ddof=1)


def summary(s):
    r = s['net'].to_numpy()
    wealth = np.cumprod(1 + r)
    drawdown = (wealth / np.maximum.accumulate(wealth) - 1).min()
    years = len(r) / 12
    over_cap = r - s['bench_cap'].to_numpy()
    over_eq = r - s['bench_eq'].to_numpy()
    te = over_cap.std(ddof=1) * math.sqrt(12)
    return {'months': len(r), 'a_year': wealth[-1] ** (1 / years) - 1,
            'vol': r.std(ddof=1) * math.sqrt(12), 'sharpe': sharpe(r) * math.sqrt(12),
            'worst_fall': drawdown, 'over_cap': over_cap.mean() * 12,
            't_over_cap': over_cap.mean() / over_cap.std(ddof=1) * math.sqrt(len(r)),
            'over_eq': over_eq.mean() * 12, 'ir_cap': over_cap.mean() * 12 / te if te else 0,
            'te_real': te, 'te_ex_ante': s['te_ex_ante'].mean(),
            'holdings': s['holdings'].mean(), 'turnover': s['turnover'].sum() / years,
            'buys': s['buys'].mean(), 'sells': s['sells'].mean(),
            'failed': int((~s['solved']).sum())}


print(f"{'':7s} {'months':>6s} {'a year':>7s} {'vol':>6s} {'Sharpe':>6s} {'worst':>6s} "
      f"{'vs cap':>7s} {'t':>5s} {'vs eq':>6s} {'IR':>5s} {'TE':>5s} {'TE ex':>5s} "
      f"{'held':>5s} {'turn':>5s} {'buys':>5s} {'sells':>5s}")
for (name,), s in res.group_by('strategy', maintain_order=True):
    m = summary(s)
    print(f"{name:7s} {m['months']:6d} {m['a_year']:+7.1%} {m['vol']:6.1%} {m['sharpe']:6.2f} "
          f"{m['worst_fall']:6.0%} {m['over_cap']:+7.1%} {m['t_over_cap']:5.2f} "
          f"{m['over_eq']:+6.1%} {m['ir_cap']:5.2f} {m['te_real']:5.1%} {m['te_ex_ante']:5.1%} "
          f"{m['holdings']:5.0f} {m['turnover']:5.1f} {m['buys']:5.1f} {m['sells']:5.1f}"
          + (f"  ({m['failed']} solves failed)" if m['failed'] else ''))

years = res.with_columns(pl.col('held_to').dt.year().alias('y')).group_by('strategy', 'y').agg(
    ((pl.col('net') + 1).product() - 1).alias('ret')).sort('y')
print('\nNet return by calendar year:')
print(years.pivot(on='strategy', index='y', values='ret').sort('y')
      .with_columns(pl.exclude('y').round(3)))

if candidate:
    a = res.filter(pl.col('strategy') == candidate).sort('me')['net'].to_numpy()
    m = res.filter(pl.col('strategy') == 'M').sort('me')['net'].to_numpy()
    s1, s2 = sharpe(a), sharpe(m)
    rho = np.corrcoef(a, m)[0, 1]
    T = len(a)
    v = (2 - 2 * rho + 0.5 * (s1 ** 2 + s2 ** 2 - 2 * s1 * s2 * rho ** 2)) / T
    z = (s1 - s2) / math.sqrt(v)
    print(f"\n{candidate} against M: monthly Sharpe {s1:.3f} vs {s2:.3f} "
          f"(annualized {s1 * math.sqrt(12):.2f} vs {s2 * math.sqrt(12):.2f}), correlation "
          f"{rho:.2f}, Memmel z {z:.2f}: {'PASSES' if z >= 1.65 else 'does not pass'} "
          f"(needs 1.65)")
