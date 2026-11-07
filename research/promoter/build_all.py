"""All NSE EQ/BE stocks, linked across renames and adjusted for bonuses and splits as in
research/build_daily.py, with the investability fields of the promoter protocol.

Usage: uv run python -I build_all.py PARSED_DIR OUT_PARQUET
"""
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import polars as pl

src, out = Path(sys.argv[1]), Path(sys.argv[2])
LINK_GAP = 10
bhav = (pl.read_parquet(src / 'bhav.parquet')
        .sort('day', 'symbol', 'value', descending=[False, False, True])
        .unique(['day', 'symbol'], keep='first', maintain_order=True))
counts = bhav.group_by('day').len()
days = sorted(counts.filter(pl.col('len') >= 500)['day'])
bhav = bhav.filter(pl.col('day').is_in(days))
day_idx = {d: i for i, d in enumerate(days)}
sym_map, isin_map, last_seen, sid_col, next_sid = {}, {}, {}, [], 0
for d, sym, isin in bhav.select('day', 'symbol', 'isin').iter_rows():
    i = day_idx[d]
    sid = None
    for cand in (sym_map.get(sym), isin_map.get(isin) if isin else None):
        if cand is not None and i - last_seen[cand] <= LINK_GAP and last_seen[cand] < i:
            sid = cand
            break
    if sid is None:
        sid, next_sid = next_sid, next_sid + 1
    sym_map[sym] = sid
    if isin:
        isin_map[isin] = sid
    last_seen[sid] = i
    sid_col.append(sid)
p = bhav.with_columns(pl.Series('sid', sid_col),
                      pl.col('day').replace_strict(day_idx, return_dtype=pl.Int32).alias('di')
                      ).sort('sid', 'di')
di, sid = p['di'].to_numpy(), p['sid'].to_numpy()
cnt = np.empty(len(di), dtype=np.int32)
bounds = np.flatnonzero(np.diff(sid)) + 1
for lo, hi in zip(np.r_[0, bounds], np.r_[bounds, len(sid)]):
    x = di[lo:hi]
    cnt[lo:hi] = np.arange(hi - lo) - np.searchsorted(x, x - 251) + 1
p = p.with_columns(pl.Series('rows252', cnt),
                   pl.col('value').rolling_median(126, min_samples=100).over('sid').alias('med126'))
ca = pl.read_parquet(src / 'corp_actions.parquet').select('symbol', 'day', pl.col('factor').alias('f'))
ca = ca.unique(['symbol', 'day'], keep='first')
p = p.join(ca, on=['symbol', 'day'], how='left').with_columns(pl.col('f').fill_null(1.0)).sort('sid', 'di')
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
    p = p.join(ex, on=['sid', 'di'], how='left').with_columns(pl.coalesce('f2', 'f').alias('f')).drop('f2')
print('series', next_sid, 'rows', p.height, 'snapped', len(extra), 'of', flag.height, 'flagged')
p = p.drop('_ovn').sort('sid', 'di')
p = p.with_columns((pl.col('f').log().reverse().cum_sum().reverse().over('sid')
                    - pl.col('f').log()).exp().alias('mult'))
p = p.with_columns([(pl.col(c) * pl.col('mult')).alias('a' + c) for c in ('open', 'close')])
p.select('sid', 'symbol', 'isin', 'series', 'day', 'di', 'open', 'close', 'aopen', 'aclose',
         'value', 'rows252', 'med126').write_parquet(out)
pl.DataFrame({'day': days}).write_parquet(out.with_name('all_days.parquet'))
print('written', out)
