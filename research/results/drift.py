"""R2 (results/PROTOCOL.md): drift after results on the survivorship-free daily panel.

Usage: uv run python -I drift.py EVENTS.parquet BUILD_DIR dev|holdout [THRESHOLD H]
"""
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.momentum import cost_pct      # noqa: E402

events = pl.read_parquet(sys.argv[1])
base, period = Path(sys.argv[2]), sys.argv[3]
only = (float(sys.argv[4]), int(sys.argv[5])) if len(sys.argv) > 5 else None
days = pl.read_parquet(base / 'days.parquet')['day'].to_list()
panel = pl.read_parquet(base / 'panel.parquet').select('sid', 'symbol', 'di')
px = pl.read_parquet(base / 'prices.parquet').sort('sid', 'di')

sids = px['sid'].unique().sort().to_list()
row = {s: i for i, s in enumerate(sids)}
D = len(days)
AO = np.full((len(sids), D), np.nan)
AC = np.full((len(sids), D), np.nan)
RO = np.full((len(sids), D), np.nan)
ri = px['sid'].replace_strict(row, return_dtype=pl.Int64).to_numpy()
di = px['di'].to_numpy()
AO[ri, di], AC[ri, di], RO[ri, di] = px['aopen'].to_numpy(), px['aclose'].to_numpy(), px['open'].to_numpy()
# The value of a holding on day d: that day's open, or the last close before it.
lastc = pl.DataFrame(AC.T).fill_null(np.nan).with_columns(pl.all().fill_nan(None).forward_fill()).to_numpy().T
prevc = np.full_like(lastc, np.nan)
prevc[:, 1:] = lastc[:, :-1]
V = np.where(np.isnan(AO), prevc, AO)
members = {d: np.array([row[s] for s in g['sid']]) for (d,), g in panel.group_by(['di'])}

# Map each meeting to sessions: B-1 (last session before), B+1 (first after), B+2.
day_ix = np.array([d.toordinal() for d in days])
ev = events.with_columns(pl.col('meeting').map_elements(lambda d: d.toordinal(), return_dtype=pl.Int64).alias('o'))
o = ev['o'].to_numpy()
b_minus = np.searchsorted(day_ix, o, side='left') - 1
b_plus = np.searchsorted(day_ix, o, side='right')
ev = ev.with_columns(pl.Series('bm1', b_minus), pl.Series('bp1', b_plus)).filter(
    (pl.col('bm1') >= 0) & (pl.col('bp1') + 2 + 63 < D))
ev = ev.with_columns(pl.col('bp1').cast(pl.Int32)).join(
    panel, left_on=['symbol', 'bp1'], right_on=['symbol', 'di'], how='inner')  # liquid on B+1

recs = []
for sym, meeting, bm1, bp1, sid in ev.select('symbol', 'meeting', 'bm1', 'bp1', 'sid').iter_rows():
    r = row[sid]
    c0, c1 = AC[r, bm1], AC[r, bp1]
    if np.isnan(c0) or np.isnan(c1) or np.isnan(AO[r, bp1 + 1]):
        continue
    mem = members[bp1]
    uni = np.nanmean(AC[mem, bp1] / AC[mem, bm1] - 1)
    rec = {'symbol': sym, 'meeting': meeting, 'entry_day': days[bp1 + 1],
           'reaction': (c1 / c0 - 1) - uni, 'cost': cost_pct(RO[r, bp1 + 1]) if RO[r, bp1 + 1] > 0 else 0.39}
    for H in (5, 21, 63):
        e, x = bp1 + 1, bp1 + 1 + H
        ret = V[r, x] / V[r, e] - 1
        base_ret = np.nanmean(V[mem, x] / V[mem, e] - 1)
        rec[f'ex{H}'] = (ret - base_ret) * 100
    recs.append(rec)
R = pl.DataFrame(recs)
lo, hi = (2013, 2020) if period == 'dev' else (2021, 2026)
R = R.filter(pl.col('entry_day').dt.year().is_between(lo, hi))
print(f'{period}: {R.height} results events in the liquid universe, '
      f'{R["symbol"].n_unique()} companies; reaction sd {R["reaction"].std() * 100:.1f}%')


def stats(sel, H, label, costs=True):
    g = sel.with_columns((pl.col(f'ex{H}') - (pl.col('cost') if costs else 0)).alias('net'))
    m = g.group_by(pl.col('entry_day').dt.strftime('%Y-%m').alias('m')).agg(pl.col('net').mean()).sort('m')
    y = g.group_by(pl.col('entry_day').dt.year().alias('y')).agg(pl.col('net').mean()).sort('y')
    t = m['net'].mean() / m['net'].std() * math.sqrt(m.height) if m.height > 2 else None
    return {'candidate': label, 'events': g.height, 'gross_excess_%': round(g[f'ex{H}'].mean(), 2),
            'net_excess_%': round(g['net'].mean(), 2), 't_monthly': round(t, 2) if t else None,
            'years_positive': f"{(y['net'] > 0).sum()}/{y.height}",
            'by_year': ' '.join(f'{a}:{b:+.1f}' for a, b in y.rows())}


rows = []
combos = [only] if only else [(th, H) for th in (0.03, 0.05, 0.08) for H in (5, 21, 63)]
for th, H in combos:
    rows.append(stats(R.filter(pl.col('reaction') >= th), H, f'reaction >= +{th*100:.0f}%, hold {H}'))
for th, H in combos:
    # Reported: an avoid/sell signal for holdings, so no cost of its own.
    rows.append(stats(R.filter(pl.col('reaction') <= -th), H,
                      f'reaction <= -{th*100:.0f}%, hold {H} (reported, before costs)', costs=False))
with pl.Config(tbl_rows=40, tbl_cols=10, tbl_width_chars=230, fmt_str_lengths=90):
    print(pl.DataFrame(rows))
