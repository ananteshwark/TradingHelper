"""Joining the Nifty 50 or Nifty Next 50: research/index_changes/PROTOCOL.md.

Usage:
  uv run python -I index_changes.py EVENTS_JSON ALL_PARQUET dev
  uv run python -I index_changes.py EVENTS_JSON ALL_PARQUET holdout N50,NN50

EVENTS_JSON comes from parse_releases.py; ALL_PARQUET from research/promoter/build_all.py.
Each run prints only its period.
"""
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.momentum import cost_pct      # noqa: E402

events_path, all_path, period = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
DEV = (dt.date(2011, 1, 1), dt.date(2019, 12, 31))
CONF = (dt.date(2020, 1, 1), dt.date(2026, 9, 30))
POST = 20

rows = json.loads(events_path.read_text())
for r in rows:
    r['release'] = dt.datetime.strptime(r['release'], '%b %d, %Y').date()
    r['effective'] = dt.date.fromisoformat(r['effective']) if r['effective'] else None
no_date = sorted({(r['release'], r['file']) for r in rows if r['effective'] is None})
print(f'check 3: releases with N50/NN50 rows but no readable effective date: {len(no_date)} {no_date}')
rows = [r for r in rows if r['effective'] is not None]
# A change counts once, from its first release.
first = {}
for r in sorted(rows, key=lambda r: r['release']):
    first.setdefault((r['index'], r['action'], r['symbol'], r['effective']), r)
rows = list(first.values())
out_of_n50 = {(r['release'], r['symbol']) for r in rows if r['index'] == 'N50' and r['action'] == 'exclude'}
# Amendment 1 (after the first development run, before any confirmation): some 2011-2014
# releases carry a duplicated text layer whose stray symbol lines read as inclusions or
# exclusions. A stock's first appearance in an index's section of a release decides whether it
# was included or excluded (rows are in the order of the text), and a Next 50 inclusion is
# dropped when the same release puts the stock in the Nifty 50.
first_action = {}
for r in rows:
    first_action.setdefault((r['release'], r['file'], r['index'], r['symbol']), r['action'])
into_n50 = {(r['release'], r['symbol']) for r in rows if r['index'] == 'N50' and r['action'] == 'include'}
dropped = [r for r in rows if r['action'] == 'include' and (
    first_action[(r['release'], r['file'], r['index'], r['symbol'])] != 'include'
    or (r['index'] == 'NN50' and (r['release'], r['symbol']) in into_n50))]
print('amendment 1: inclusions dropped as repeats or inconsistent:',
      sorted({(str(r['release']), r['index'], r['symbol']) for r in dropped}))
rows = [r for r in rows if r not in dropped]
ev = []
for r in rows:
    if r['action'] != 'include':
        continue
    if r['index'] == 'N50':
        ev.append({**r, 'set': 'N50'})
    elif (r['release'], r['symbol']) not in out_of_n50:
        ev.append({**r, 'set': 'NN50'})
E = pl.DataFrame([{k: v for k, v in e.items() if k in ('set', 'symbol', 'release', 'effective', 'file')}
                  for e in ev])
print('check 1: inclusions per year and set:')
print(E.group_by(pl.col('release').dt.year().alias('year'), 'set').len()
      .pivot(on='set', index='year', values='len').sort('year'))

P = pl.read_parquet(all_path)
days = pl.read_parquet(all_path.with_name('all_days.parquet'))['day'].to_list()
ND = len(days)
sids = P['sid'].unique().sort().to_list()
rowof = {s: i for i, s in enumerate(sids)}
ri = P['sid'].replace_strict(rowof, return_dtype=pl.Int64).to_numpy()
di = P['di'].to_numpy()
AO = np.full((len(sids), ND), np.nan, dtype=np.float32)
AC = np.full((len(sids), ND), np.nan, dtype=np.float32)
RO = np.full((len(sids), ND), np.nan, dtype=np.float32)
AO[ri, di], AC[ri, di], RO[ri, di] = P['aopen'].to_numpy(), P['aclose'].to_numpy(), P['open'].to_numpy()
LC = pl.DataFrame(AC.T).with_columns(pl.all().fill_nan(None).forward_fill()).to_numpy().T
sid_at = dict(zip(zip(P['symbol'].to_list(), P['di'].to_list(), strict=True), ri.tolist(), strict=True))
bench_sym = 'NIFTYBEES'
import bisect   # noqa: E402

out, unmatched = [], []
for e in E.iter_rows(named=True):
    en = bisect.bisect_right(days, e['release'])                   # first session after
    ex = bisect.bisect_left(days, e['effective']) - 1               # last session before
    if en >= ND or ex < en or ex >= ND:
        continue
    r, b = sid_at.get((e['symbol'], en - 1)), sid_at.get((bench_sym, en - 1))
    if r is None or b is None or np.isnan(AO[r, en]) or np.isnan(AO[b, en]):
        unmatched.append((e['symbol'], str(e['release'])))
        continue
    close = lambda k, x: AC[k, x] if not np.isnan(AC[k, x]) else LC[k, x]   # noqa: E731
    stock = close(r, ex) / AO[r, en] - 1
    bench = close(b, ex) / AO[b, en] - 1
    post = ((close(r, ex + POST) / close(r, ex) - 1) - (close(b, ex + POST) / close(b, ex) - 1)
            if ex + POST < ND else None)                 # reported only, not required
    cost = cost_pct(float(RO[r, en]))
    out.append({'set': e['set'], 'symbol': e['symbol'], 'release': e['release'],
                'entry': days[en], 'exit': days[ex], 'sessions': ex - en + 1,
                'gross_pct': float(stock - bench) * 100,
                'net_pct': float(stock - bench) * 100 - cost,
                'post_pct': float(post) * 100 if post is not None else None})
T = pl.DataFrame(out)
print(f'check 2: matched to a price on the entry day: {T.height} of {T.height + len(unmatched)} '
      f'({T.height / (T.height + len(unmatched)):.0%}); unmatched {unmatched}')


def summary(T, label):
    by = T.group_by('release').agg(pl.col('net_pct').mean().alias('n'))
    n = by['n'].to_numpy()
    t = n.mean() / n.std(ddof=1) * math.sqrt(len(n)) if len(n) > 1 else float('nan')
    return {'set': label, 'events': T.height, 'releases': len(n),
            'median_sessions': int(T['sessions'].median()),
            'gross_%': round(T['gross_pct'].mean(), 2), 'net_%': round(T['net_pct'].mean(), 2),
            'median_net_%': round(T['net_pct'].median(), 2),
            'beat_%': round((T['gross_pct'] > 0).mean() * 100), 't_releases': round(t, 2),
            'next_20_sessions_%': round(T['post_pct'].mean(), 2)}


pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_cols(12)
pl.Config.set_tbl_width_chars(200)
lo, hi = DEV if period == 'dev' else CONF
sets = ['N50', 'NN50'] if period == 'dev' else sys.argv[4].split(',')
P_ = T.filter(pl.col('release').is_between(lo, hi))
print(f"{'Development' if period == 'dev' else 'Confirmation'}, releases {lo} to {hi}:")
print(pl.DataFrame([summary(P_.filter(pl.col('set') == s), s) for s in sets]))
for s in sets:
    print(s, 'events:')
    print(P_.filter(pl.col('set') == s).select('symbol', 'release', 'entry', 'exit', 'sessions',
          pl.col('net_pct').round(2), pl.col('post_pct').round(2)).sort('release'))
