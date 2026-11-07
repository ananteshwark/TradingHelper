"""Study A panel (PROTOCOL.md): link each stock across renames and ISIN changes, adjust
prices for corporate actions, set the universe each day, compute features and targets.

Usage: uv run python -I build_daily.py PARSED_DIR NIFTY_JSON OUT_DIR
(PARSED_DIR holds bhav, deliv and corp_actions parquet files.)
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

src, nifty_path, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
UNIVERSE = 250
LINK_GAP = 10           # trading days a series may skip and still be continued
HORIZONS = (5, 21)

bhav = pl.read_parquet(src / 'bhav.parquet')
deliv = pl.read_parquet(src / 'deliv.parquet')
bhav = (bhav.join(deliv.select('day', 'symbol', 'series', 'deliv_qty', 'deliv_pct'),
                  on=['day', 'symbol', 'series'], how='left')
        .sort('day', 'symbol', 'value', descending=[False, False, True])
        .unique(['day', 'symbol'], keep='first', maintain_order=True))
counts = bhav.group_by('day').len()
days = sorted(counts.filter(pl.col('len') >= 500)['day'])
bhav = bhav.filter(pl.col('day').is_in(days))
day_idx = {d: i for i, d in enumerate(days)}
print('market days', len(days), days[0], days[-1])

# --- Link rows into series: same symbol or same ISIN seen within LINK_GAP days.
sym_map, isin_map, last_seen = {}, {}, {}
sid_col = []
next_sid = 0
for d, sym, isin in bhav.select('day', 'symbol', 'isin').iter_rows():
    i = day_idx[d]
    sid = None
    for cand in (sym_map.get(sym), isin_map.get(isin) if isin else None):
        if cand is not None and i - last_seen[cand] <= LINK_GAP and last_seen[cand] < i:
            sid = cand
            break
    if sid is None:
        sid = next_sid
        next_sid += 1
    sym_map[sym] = sid
    if isin:
        isin_map[isin] = sid
    last_seen[sid] = i
    sid_col.append(sid)
bhav = bhav.with_columns(pl.Series('sid', sid_col),
                         pl.col('day').replace_strict(day_idx, return_dtype=pl.Int32).alias('di'))
print('series', next_sid)

# --- Eligibility (all series): 126-day median traded value, rows in the last 252 days.
bhav = bhav.sort('sid', 'di')
di = bhav['di'].to_numpy()
sid = bhav['sid'].to_numpy()
cnt = np.empty(len(di), dtype=np.int32)
start = 0
bounds = np.flatnonzero(np.diff(sid)) + 1
for lo, hi in zip(np.r_[0, bounds], np.r_[bounds, len(sid)]):
    x = di[lo:hi]
    cnt[lo:hi] = np.arange(hi - lo) - np.searchsorted(x, x - 251) + 1
bhav = bhav.with_columns(pl.Series('rows252', cnt),
                         pl.col('value').rolling_median(126, min_samples=100).over('sid').alias('med126'))
elig = bhav.filter((pl.col('rows252') >= 200) & (pl.col('close') >= 20) & pl.col('med126').is_not_null())
elig = elig.with_columns(pl.col('med126').rank('ordinal', descending=True).over('di').alias('liq_rank'))
uni = elig.filter(pl.col('liq_rank') <= UNIVERSE).select('sid', 'di', pl.lit(True).alias('in_uni'))
ever = uni['sid'].unique()
p = bhav.filter(pl.col('sid').is_in(ever.implode())).join(uni, on=['sid', 'di'], how='left') \
    .with_columns(pl.col('in_uni').fill_null(False)).sort('sid', 'di')
print('series ever in the universe', ever.len(), 'rows', p.height)

# --- Adjustment factors from NSE's corporate-action files (corp_actions.py; Amendment 1).
ca = pl.read_parquet(src / 'corp_actions.parquet').select('symbol', 'day', pl.col('factor').alias('f'))
ca = ca.unique(['symbol', 'day'], keep='first')
p = p.join(ca, on=['symbol', 'day'], how='left').with_columns(pl.col('f').fill_null(1.0)).sort('sid', 'di')
# An overnight move beyond -40% / +60% with no recorded event: an unrecorded bonus or split,
# snapped to the nearest simple fraction (within 5%).
from fractions import Fraction    # noqa: E402
FRACS = sorted({Fraction(a, b) for a in range(1, 21) for b in range(1, 21)})
p = p.with_columns((pl.col('open') / pl.col('close').shift(1).over('sid')).alias('_ovn'))
flag = p.filter((pl.col('f') == 1) & ((pl.col('_ovn') < 0.6) | (pl.col('_ovn') > 1.6)))
extra = {}
for sid_, di_, r in flag.select('sid', 'di', '_ovn').iter_rows():
    best = min(FRACS, key=lambda fr: abs(float(fr) - r))
    if abs(float(best) / r - 1) < 0.05:
        extra[(sid_, di_)] = float(best)
if extra:
    ex = pl.DataFrame([(k[0], k[1], v) for k, v in extra.items()], orient='row',
                      schema=[('sid', p['sid'].dtype), ('di', p['di'].dtype), ('f2', pl.Float64)])
    p = p.join(ex, on=['sid', 'di'], how='left').with_columns(
        pl.coalesce('f2', 'f').alias('f')).drop('f2')
print('unrecorded moves snapped', len(extra), 'of', flag.height, 'flagged:', sorted((s, str(d), round(r, 3)) for s, d, r in flag.select('symbol', 'day', '_ovn').iter_rows()))
p = p.drop('_ovn').sort('sid', 'di')
# adjusted = raw x product of the factors of all later rows
p = p.with_columns((pl.col('f').log().reverse().cum_sum().reverse().over('sid')
                    - pl.col('f').log()).exp().alias('mult'))
print('adjustments', p.filter(pl.col('f') != 1).height)
p = p.with_columns([(pl.col(c) * pl.col('mult')).alias('a' + c) for c in ('open', 'high', 'low', 'close')])

# --- Nifty 50 (Upstox, already adjusted).
nifty = pl.DataFrame([{'day': r[0][:10], 'nclose': r[4]} for r in json.loads(nifty_path.read_text())]) \
    .with_columns(pl.col('day').str.to_date()).sort('day')
nifty = nifty.filter(pl.col('day').is_in(days)).with_columns(
    pl.col('day').replace_strict(day_idx, return_dtype=pl.Int32).alias('di'),
    (pl.col('nclose') / pl.col('nclose').shift(1) - 1).alias('mret'))
nifty = nifty.with_columns(
    (pl.col('nclose') / pl.col('nclose').shift(5) - 1).alias('m_r5'),
    (pl.col('nclose') / pl.col('nclose').shift(21) - 1).alias('m_r21'),
    (pl.col('nclose') / pl.col('nclose').shift(63) - 1).alias('m_r63'),
    pl.col('mret').rolling_std(21).alias('m_vol21'))

# --- Features (row windows over each series' own trading days).
c, o, h, l = pl.col('aclose'), pl.col('aopen'), pl.col('ahigh'), pl.col('alow')
ret1 = c / c.shift(1) - 1
p = p.join(nifty.select('di', 'mret'), on='di', how='left').sort('sid', 'di')
p = p.with_columns(ret1.over('sid').alias('ret1'),
                   (o / c.shift(1)).log().over('sid').alias('ovn'),
                   (c / o).log().alias('intr'),
                   ((c - l) / (h - l)).fill_nan(0.5).fill_null(0.5).alias('clv'),
                   (pl.col('deliv_qty') * pl.col('close')).alias('dval'))
w = lambda e: e.over('sid')
r, m = pl.col('ret1'), pl.col('mret')
p = p.with_columns(
    w(c / c.shift(1) - 1).alias('r1'), w(c / c.shift(5) - 1).alias('r5'),
    w(c / c.shift(21) - 1).alias('r21'), w(c / c.shift(63) - 1).alias('r63'),
    w(c / c.shift(126) - 1).alias('r126'), w(c.shift(21) / c.shift(252) - 1).alias('mom'),
    w(r.rolling_std(21)).alias('vol21'), w(r.rolling_std(63)).alias('vol63'),
    w(r.rolling_max(21)).alias('max1'),
    w(c / h.rolling_max(252) - 1).alias('hi52'), w(c / l.rolling_min(252) - 1).alias('lo52'),
    w(c / c.rolling_mean(50) - 1).alias('sma50'), w(c / c.rolling_mean(200) - 1).alias('sma200'),
    w(pl.col('ovn').rolling_sum(21)).alias('ovn21'), w(pl.col('intr').rolling_sum(21)).alias('intra21'),
    w(o / c.shift(1) - 1).alias('gap1'), pl.col('clv').alias('clv1'),
    w(pl.col('clv').rolling_mean(5)).alias('clv5'),
    w(pl.col('value').rolling_median(21)).log().alias('logtv'),
    w(pl.col('value').rolling_mean(5) / pl.col('value').rolling_mean(63)).alias('tvr'),
    w((r.abs() / pl.col('value')).rolling_mean(21)).log().alias('amihud'),
    pl.col('close').log().alias('logpx'),
    w(pl.col('deliv_pct').rolling_mean(21, min_samples=10)).alias('dlv21'),
    w(pl.col('deliv_pct').rolling_mean(5, min_samples=3)
      - pl.col('deliv_pct').rolling_mean(63, min_samples=30)).alias('dlv_chg'),
    w(pl.col('dval').rolling_mean(21, min_samples=10)).log().alias('dlv_val'),
    w((r * m).rolling_mean(63) - r.rolling_mean(63) * m.rolling_mean(63)).alias('_cov'),
    w(m.rolling_var(63)).alias('_mvar'), w(r.rolling_var(63)).alias('_rvar'),
    w(pl.int_range(pl.len())).clip(0, 756).alias('age'))
p = p.with_columns((pl.col('_cov') / pl.col('_mvar')).alias('beta63'))
p = p.with_columns((pl.col('_rvar') - pl.col('beta63') ** 2 * pl.col('_mvar')).clip(0).sqrt().alias('idio63'))

# --- Targets: next open to the open H days later; last close if the stock stopped trading.
p = p.sort('sid', 'di')
di, sid = p['di'].to_numpy(), p['sid'].to_numpy()
ao, ac, ro = p['aopen'].to_numpy(), p['aclose'].to_numpy(), p['open'].to_numpy()
bounds = np.flatnonzero(np.diff(sid)) + 1
entry_adj = np.full(len(di), np.nan)
entry_raw = np.full(len(di), np.nan)
fwd = {H: np.full(len(di), np.nan) for H in HORIZONS}
last_day = len(days) - 1
for lo, hi in zip(np.r_[0, bounds], np.r_[bounds, len(sid)]):
    x = di[lo:hi]
    pos_entry = np.searchsorted(x, x + 1)
    ok = (pos_entry < len(x))
    ok[ok] &= x[pos_entry[ok]] == x[ok] + 1
    e_adj = np.where(ok, ao[lo:hi][np.minimum(pos_entry, len(x) - 1)], np.nan)
    entry_adj[lo:hi] = e_adj
    entry_raw[lo:hi] = np.where(ok, ro[lo:hi][np.minimum(pos_entry, len(x) - 1)], np.nan)
    for H in HORIZONS:
        j = x + 1 + H
        pos = np.searchsorted(x, j)
        hit = (pos < len(x))
        hit[hit] &= x[pos[hit]] == j[hit]
        exit_px = np.where(hit, ao[lo:hi][np.minimum(pos, len(x) - 1)],
                           ac[lo:hi][np.clip(pos - 1, 0, len(x) - 1)])
        valid = ok & (j <= last_day)
        fwd[H][lo:hi] = np.where(valid, exit_px / e_adj - 1, np.nan)
p = p.with_columns(pl.Series('entry_adj', entry_adj), pl.Series('entry_raw', entry_raw),
                   *[pl.Series(f'fwd{H}', fwd[H]) for H in HORIZONS])

STOCK = ['r1', 'r5', 'r21', 'r63', 'r126', 'mom', 'vol21', 'vol63', 'max1', 'hi52', 'lo52',
         'sma50', 'sma200', 'ovn21', 'intra21', 'gap1', 'clv1', 'clv5', 'logtv', 'tvr', 'amihud',
         'logpx', 'dlv21', 'dlv_chg', 'dlv_val', 'beta63', 'idio63', 'age']
u = p.filter(pl.col('in_uni'))
u = u.with_columns(pl.when(pl.col('sma50') > 0).then(1.0).otherwise(0.0).mean().over('di').alias('m_breadth'),
                   pl.col('r21').std().over('di').alias('m_disp'))
u = u.join(nifty.select('di', 'm_r5', 'm_r21', 'm_r63', 'm_vol21'), on='di', how='left')
u = u.with_columns([((pl.col(f).rank('average').over('di') - 1)
                     / (pl.col(f).count().over('di') - 1) - 0.5).fill_null(0).fill_nan(0).alias('x_' + f)
                    for f in STOCK])
for H in HORIZONS:
    u = u.with_columns((pl.col(f'fwd{H}') - pl.col(f'fwd{H}').mean().over('di')).alias(f'ex{H}'))
    u = u.with_columns(((pl.col(f'ex{H}').rank('average').over('di') - 1)
                        / (pl.col(f'ex{H}').count().over('di') - 1) - 0.5).alias(f'y{H}'))
u = u.with_columns(pl.col('di').replace_strict(dict(enumerate(days)), return_dtype=pl.Date).alias('day'))
keep = ['sid', 'symbol', 'day', 'di', 'close', 'entry_adj', 'entry_raw', 'mom'] + \
       ['x_' + f for f in STOCK] + ['m_r5', 'm_r21', 'm_r63', 'm_vol21', 'm_breadth', 'm_disp'] + \
       [f'{k}{H}' for H in HORIZONS for k in ('fwd', 'ex', 'y')]
u.select(keep).write_parquet(out / 'panel.parquet')
p.select('sid', 'symbol', 'di', 'day', 'open', 'close', 'aopen', 'aclose').write_parquet(out / 'prices.parquet')
pl.DataFrame({'di': list(range(len(days))), 'day': days}).write_parquet(out / 'days.parquet')
print('panel', u.height, 'rows; universe per day', u.group_by('di').len()['len'].describe())
