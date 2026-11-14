"""Part 1 for both studies: which parameters go with adverse moves (PROTOCOL.md).

Usage:
  uv run python -I drivers.py PATHS_DIR B_ROWS dev
  uv run python -I drivers.py PATHS_DIR B_ROWS holdout

PATHS_DIR is where paths.py wrote Study A's tables; B_ROWS is momentum.py's table of
pick-months (`b_rows.parquet`). `dev` reads only development (Study A 2023-2024, Study B
months ending 2016-2020); `holdout` reports both periods and the confirmed drivers.

For each parameter, among the picks (Study A: the app's calls; Study B: M's 20 a month):
the mean among adverse and other rows and the standardised difference, the adverse rate
in each fifth, the single-parameter AUC (above 0.5: higher values go with adverse moves),
and the top-minus-bottom-fifth difference in the adverse rate with its t (two
proportions). A confirmed driver: that difference has the same sign in both periods and
|t| >= 2 in the holdout.

Fixed here before any of it was run, for the hypotheses:
- H1: "against the Nifty" is the Nifty's return from its open to the last candle before
  the entry, signed for the side, below 0.
- H2: relative volume at the decision is `rvol30` (09:45 calls) or `rvol_last30`
  (evening). Its top fifth against its bottom fifth: the mean |MAE| and the mean MFE, in %
  of the entry, each with a Welch t.
- H3: at the first checkpoint, the trade 0.25 ATR or more against it with relative volume
  since entry above 1, against every other trade open then: the later stop rate.
- H4: the picks' weaker half by conviction against the stronger half.
- H5: the top fifth by distance from VWAP in the trade's direction (`s_vwap_dist`, or
  `s_close_vwap` for the evening picks, the previous close's) against the other four.
- `own_*` parameters (the stock's own gap, move and volume before the entry) aren't in the
  protocol; they are shown apart, as exploratory.
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

paths, b_rows, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
A_PERIODS = {'dev': (dt.date(2023, 1, 1), dt.date(2024, 12, 31)),
             'holdout': (dt.date(2025, 1, 1), dt.date(2026, 10, 23))}
B_PERIODS = {'dev': (dt.date(2016, 1, 1), dt.date(2020, 12, 31)),
             'holdout': (dt.date(2021, 1, 1), dt.date(2026, 9, 30))}
PERIODS = ['dev'] if mode == 'dev' else ['dev', 'holdout']
PATH_COLS = ['move_atr', 'best_atr', 'worst_atr', 'last15', 'rvol_since', 'rvol_15',
             'vwap_gap', 'range_pos', 'nifty_move', 'rel_move']


def auc(x, y):
    """P(x of an adverse row > x of another), ties half."""
    order = np.argsort(x, kind='mergesort')
    ranks = np.empty(len(x))
    xs = x[order]
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    n1, n0 = y.sum(), len(y) - y.sum()
    if n1 == 0 or n0 == 0:
        return float('nan')
    return (ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def two_prop(a, b):
    """(difference of proportions, t) of boolean arrays a and b."""
    if len(a) == 0 or len(b) == 0:
        return float('nan'), float('nan')
    pa, pb = a.mean(), b.mean()
    p = (a.sum() + b.sum()) / (len(a) + len(b))
    se = math.sqrt(p * (1 - p) * (1 / len(a) + 1 / len(b)))
    return pa - pb, (pa - pb) / se if se > 0 else float('nan')


def welch(a, b):
    se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return a.mean() - b.mean(), (a.mean() - b.mean()) / se if se > 0 else float('nan')


def describe(df, label, cols):
    """One row a parameter."""
    out = []
    y_all = df[label].cast(pl.Int8).to_numpy()
    for c in cols:
        x = df[c].cast(pl.Float64).to_numpy()
        ok = ~np.isnan(x)
        x, y = x[ok], y_all[ok]
        if len(x) < 50 or x.std() == 0:
            continue
        a, o = x[y == 1], x[y == 0]
        sd = math.sqrt((a.var(ddof=1) + o.var(ddof=1)) / 2) if len(a) > 1 and len(o) > 1 \
            else float('nan')
        q = np.quantile(x, [0.2, 0.4, 0.6, 0.8])
        fifth = np.searchsorted(q, x, side='right')
        rates = [y[fifth == k].mean() if (fifth == k).any() else float('nan')
                 for k in range(5)]
        d, t = two_prop(y[fifth == 4], y[fifth == 0])
        out.append({'parameter': c, 'n': len(x), 'adverse': round(float(a.mean()), 4),
                    'other': round(float(o.mean()), 4),
                    'std_diff': round((a.mean() - o.mean()) / sd, 3) if sd else None,
                    'auc': round(auc(x, y), 3),
                    'fifths': ' '.join(f'{r:.2f}' for r in rates),
                    'top_bottom': round(d, 3), 't': round(t, 2)})
    return pl.DataFrame(out)


def show(title, df):
    print(f'\n{title}')
    with pl.Config(tbl_rows=80, tbl_cols=12, tbl_width_chars=200, fmt_str_lengths=40):
        print(df)


def confirm(tables):
    """Parameters whose top-bottom difference has one sign in both periods, |t| >= 2 later."""
    if 'holdout' not in tables:
        return
    d, h = tables['dev'], tables['holdout']
    j = d.select('parameter', pl.col('top_bottom').alias('dev'), pl.col('t').alias('t_dev')) \
        .join(h.select('parameter', pl.col('top_bottom').alias('hold'),
                       pl.col('t').alias('t_hold')), on='parameter')
    j = j.filter((pl.col('dev') * pl.col('hold') > 0) & (pl.col('t_hold').abs() >= 2))
    show('Confirmed drivers (same sign in both periods, |t| >= 2 in the holdout)',
         j.sort(pl.col('t_hold').abs(), descending=True))


# --- Study A ----------------------------------------------------------------------------

for name in ('m0945', 'evening'):
    trades = pl.read_parquet(paths / f'{name}_trades.parquet').filter('pick')
    checks = pl.read_parquet(paths / f'{name}_checks.parquet').join(
        trades.select('symbol', 'day', 'side'), on=['symbol', 'day', 'side'])
    decision = [c for c in trades.columns if c.startswith('s_') and not c.startswith('s_own_')]
    decision += [c for c in ('range30_atr', 'rvol30', 'atr_pct', 'log_value20',
                             'rvol_last30') if c in trades.columns]
    decision += ['conviction', 'rank', 'side', 'dow']
    own = ['s_own_gap', 's_own_sofar', 'own_rvol_pre']
    rvol = 'rvol30' if name == 'm0945' else 'rvol_last30'
    vw = 's_vwap_dist' if name == 'm0945' else 's_close_vwap'
    tables, ctables = {}, {}
    for period in PERIODS:
        lo, hi = A_PERIODS[period]
        t = trades.filter(pl.col('day').is_between(lo, hi))
        c = checks.filter(pl.col('day').is_between(lo, hi))
        print(f'\n======== Study A, {name}, {period}: {lo} to {hi} ========')
        print(f"{t.height} picks, stopped {t['stopped'].mean():.3f}; "
              f"net {t['net'].mean() * 100:+.3f}% a trade; "
              f"buys stopped {t.filter(pl.col('side') == 1)['stopped'].mean():.3f}, "
              f"shorts {t.filter(pl.col('side') == -1)['stopped'].mean():.3f}")
        for lab, g in (('stopped', t.filter('stopped')),
                       ('not stopped', t.filter(~pl.col('stopped')))):
            print(f"  {lab:11s}: MAE median {g['mae_atr'].median():+.2f} ATR "
                  f"({g['mae'].median() * 100:+.2f}%), MFE median {g['mfe_atr'].median():+.2f}"
                  f" ATR ({g['mfe'].median() * 100:+.2f}%), net {g['net'].mean() * 100:+.3f}%")
        st = t.filter('stopped')
        bins = [0, 3, 6, 9, 15, 27, 60]            # slots after the entry
        e = 7 if name == 'm0945' else 1
        after = (st['exit_slot'] - e).to_numpy()
        shares = [((after >= bins[k]) & (after < bins[k + 1])).mean()
                  for k in range(len(bins) - 1)]
        print('  when the stops came (minutes after entry): ' + ', '.join(
            f'{bins[k] * 5}-{bins[k + 1] * 5}: {v:.0%}' for k, v in enumerate(shares)))
        tables[period] = describe(t, 'stopped', decision)
        show('At the decision: parameter, adverse (stopped) and other means, ...',
             tables[period].sort('auc'))
        show('Exploratory, not in the protocol: the stock before the entry',
             describe(t, 'stopped', own))
        for n in range(1, 6):
            cn = c.filter(pl.col('check') == n)
            if cn.is_empty():
                continue
            tab = describe(cn, 'stop_later', PATH_COLS)
            ctables.setdefault(n, {})[period] = tab
            show(f"Checkpoint {n} ({cn['minutes'][0]} min after entry): {cn.height} open, "
                 f"{cn['stop_later'].mean():.3f} stopped later", tab.sort('auc'))
        # The hypotheses.
        y = t['stopped'].to_numpy()
        nd = t['s_nd_sofar'].to_numpy()
        ok = ~np.isnan(nd)
        print('\nHypotheses:')
        dd, tt = two_prop(y[ok & (nd < 0)], y[ok & (nd >= 0)])
        print(f'  H1 against the Nifty: stop rate {dd:+.3f} (t {tt:.2f})')
        x = t[rvol].to_numpy()
        ok = ~np.isnan(x)
        qq = np.quantile(x[ok], [0.2, 0.8])
        top, bot = t.filter(pl.col(rvol) >= qq[1]), t.filter(pl.col(rvol) <= qq[0])
        dm, tm = welch(-top['mae'].to_numpy(), -bot['mae'].to_numpy())
        df_, tf = welch(top['mfe'].to_numpy(), bot['mfe'].to_numpy())
        print(f'  H2 volume at the decision ({rvol}), top fifth less bottom: |MAE| '
              f'{dm * 100:+.3f}% (t {tm:.2f}), MFE {df_ * 100:+.3f}% (t {tf:.2f})')
        c1 = c.filter(pl.col('check') == 1)
        grp = (c1['move_atr'] <= -0.25) & (c1['rvol_since'] > 1)
        dd, tt = two_prop(c1.filter(grp)['stop_later'].to_numpy(),
                          c1.filter(~grp)['stop_later'].to_numpy())
        print(f'  H3 against by 0.25 ATR on volume at checkpoint 1 ({grp.sum()} of '
              f'{c1.height}): later stop rate {dd:+.3f} (t {tt:.2f})')
        cv = t['conviction'].to_numpy()
        med = np.median(cv)
        dd, tt = two_prop(y[cv <= med], y[cv > med])
        print(f'  H4 weaker half by conviction: stop rate {dd:+.3f} (t {tt:.2f})')
        x = t[vw].to_numpy()
        ok = ~np.isnan(x)
        q80 = np.quantile(x[ok], 0.8)
        dd, tt = two_prop(y[ok & (x >= q80)], y[ok & (x < q80)])
        print(f'  H5 extended from VWAP ({vw}), top fifth: stop rate {dd:+.3f} (t {tt:.2f})')
    confirm(tables)
    for n, tb in ctables.items():
        if 'holdout' in tb:
            print(f'\nCheckpoint {n}:', end='')
            confirm(tb)

# --- Study B ----------------------------------------------------------------------------

if b_rows.exists():
    rows = pl.read_parquet(b_rows).filter(pl.col('top20'))
    month_cols = [c for c in rows.columns if c.startswith('f_')]
    after_cols = [c for c in rows.columns if (c.startswith('a5_') or c.startswith('a10_'))
                  and not c.endswith('_rest')]      # `_rest` is the outcome B2 predicts
    tables, a_tables = {}, {}
    for period in PERIODS:
        lo, hi = B_PERIODS[period]
        r = rows.filter(pl.col('held_to').is_between(lo, hi))
        print(f'\n======== Study B, momentum M, {period}: months ending {lo} to {hi} ========')
        print(f"{r.height} pick-months, adverse {r['adverse'].mean():.3f}, "
              f"drawdown -15% or worse {r['dd15'].mean():.3f}")
        tables[period] = describe(r, 'adverse', month_cols)
        show('At the month end: parameter, adverse and other means, ...',
             tables[period].sort('auc'))
        show('Drawdown of 15% or worse within the month (secondary)',
             describe(r, 'dd15', month_cols).sort('auc'))
        a_tables[period] = describe(r, 'adverse', after_cols)
        show('After the entry, at sessions 5 and 10', a_tables[period].sort('auc'))
    confirm(tables)
    confirm(a_tables)
