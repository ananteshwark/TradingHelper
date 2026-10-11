"""Illustrative (not a pre-registered test): how the momentum core combines with gold and
cash, 2013-2026, on the survivorship-free panel. Uses research/institutional/portfolios.py.

Usage: uv run python -I combo.py BUILD_DIR > combo.txt
"""
import contextlib, io, math, runpy, sys
import numpy as np
sys.argv = ['portfolios.py', sys.argv[1], 'dev']
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path('/home/user/TradingHelper/research/institutional/portfolios.py')
res, days, LC, sids, sym, entries = (g[k] for k in ('results', 'sim_days', 'LC', 'sids', 'symbol_of', 'entries'))
gold = next(i for i, s in enumerate(sids) if sym[s] == 'GOLDBEES')
d0 = entries[0]
gr = np.array([LC[gold, d] / LC[gold, d - 1] - 1 for d in range(d0, d0 + len(days))])
cash = 1.06 ** (1 / 250) - 1
H4, M, N = res['H4'][0], res['M'][0], res['NIFTYBEES'][0]

def mix(weights, series):
    """Rebalanced to the weights at each month start."""
    nav, val, out = 1.0, None, []
    for i, d in enumerate(days):
        if val is None or d.month != days[i - 1].month:
            val = [nav * w for w in weights]
        val = [v * (1 + s[i]) for v, s in zip(val, series)]
        new = sum(val); out.append(new / nav - 1); nav = new
    return np.array(out)

def stats(r, label):
    nav = np.cumprod(1 + r); dd = (nav / np.maximum.accumulate(nav) - 1).min()
    yrs = sorted({d.year for d in days})
    yr = []
    for y in yrs:
        m = np.array([d.year == y for d in days]); yr.append(np.prod(1 + r[m]) - 1)
    print(f'{label:52s} {(nav[-1] ** (250 / len(r)) - 1) * 100:5.1f}%/yr  worst fall {dd * 100:6.1f}%  '
          f'worst year {min(yr) * 100:+5.0f}%  losing years {sum(x < 0 for x in yr)}/{len(yr)}')

c = np.full(len(days), cash)
print(f'{days[0]} to {days[-1]}, after costs on the momentum legs; illustrative')
stats(N, 'Nifty 50 (NIFTYBEES)')
stats(gr, 'Gold (GOLDBEES)')
stats(M, 'Momentum top 10, equal weights')
stats(H4, 'Momentum top 10, risk-weighted')
stats(mix([0.7, 0.2, 0.1], [H4, gr, c]), '70% risk-weighted momentum, 20% gold, 10% cash')
stats(mix([0.6, 0.25, 0.15], [H4, gr, c]), '60% risk-weighted momentum, 25% gold, 15% cash')
stats(mix([0.5, 0.2, 0.3], [H4, gr, N]), '50% momentum, 20% gold, 30% Nifty')
print('correlation of daily returns, momentum vs gold:', round(float(np.corrcoef(H4, gr)[0, 1]), 2))
