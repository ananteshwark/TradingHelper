"""Promoters buying their own shares: research/promoter/PROTOCOL.md.

Usage:
  uv run python -I promoter.py PIT_DIR ALL_PARQUET dev
  uv run python -I promoter.py PIT_DIR ALL_PARQUET holdout V-all|V-1cr 21|63

PIT_DIR holds NSE's corporates-pit downloads, a JSON file a month; ALL_PARQUET is
build_all.py's output (all_days.parquet beside it). Each run prints only its period.
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

pit_dir, all_path, period = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OPEN = dt.time(9, 15)
DEV = (dt.date(2015, 12, 1), dt.date(2020, 12, 31))
HOLD_FROM, PANEL_END = dt.date(2021, 1, 1), dt.date(2026, 10, 8)

# ------------------------------------------------------------------ disclosures
rows = []
for f in sorted(pit_dir.glob('*.json')):
    rows += json.loads(f.read_text()).get('data', [])
print(f'disclosure rows {len(rows)} in {len(list(pit_dir.glob("*.json")))} monthly files')


def ddate(s):
    try:
        return dt.datetime.strptime(s.strip(), '%d-%b-%Y').date()
    except (ValueError, AttributeError):
        return None


def num(s):
    try:
        return float(str(s).replace(',', ''))
    except (ValueError, TypeError):
        return None


ev = []
seen = set()
for r in rows:
    if (r.get('personCategory') not in ('Promoters', 'Promoter Group')
            or r.get('acqMode') != 'Market Purchase' or r.get('tdpTransactionType') != 'Buy'
            or r.get('secType') != 'Equity Shares'):
        continue
    key = (r.get('pid'), r.get('did'), r.get('symbol'), r.get('acqName'), r.get('secAcq'),
           r.get('date'))
    if key in seen:                       # the same row in two monthly files
        continue
    seen.add(key)
    value = num(r.get('secVal'))
    try:
        when = dt.datetime.strptime(r['date'].strip(), '%d-%b-%Y %H:%M').replace(tzinfo=IST)
    except (ValueError, KeyError, AttributeError):
        when = None
    if not value or value <= 0 or when is None:
        continue
    ev.append({'symbol': r['symbol'].strip(), 'when': when, 'value': value,
               'acq_from': ddate(r.get('acqfromDt')), 'acq_to': ddate(r.get('acqtoDt')),
               'name': r.get('acqName')})
E = pl.DataFrame(ev)
print(f'promoter market purchases: {E.height} rows, {E["symbol"].n_unique()} symbols, '
      f'{E["when"].min():%b %Y} to {E["when"].max():%b %Y}')

# Check 1 and 2.
early = E.filter(pl.col('acq_from').is_not_null()
                 & (pl.col('when').dt.date() < pl.col('acq_from')))
print(f'check 1: broadcast before the acquisition started: {early.height} rows, dropped')
E = E.join(early.select('symbol', 'when', 'name', pl.lit(True).alias('bad')),
           on=['symbol', 'when', 'name'], how='left').filter(pl.col('bad').is_null()).drop('bad')
lag = E.filter(pl.col('acq_to').is_not_null()).select(
    (pl.col('when').dt.date() - pl.col('acq_to')).dt.total_days().alias('lag'))['lag']
print(f'check 2: days from the acquisition end to the broadcast: median {lag.median():.0f}, '
      f'75th percentile {lag.quantile(0.75):.0f}, 95th {lag.quantile(0.95):.0f}')

# ------------------------------------------------------------------ prices
P = pl.read_parquet(all_path)
days = pl.read_parquet(all_path.with_name('all_days.parquet'))['day'].to_list()
ND = len(days)
sids = P['sid'].unique().sort().to_list()
rowof = {s: i for i, s in enumerate(sids)}
NS = len(sids)
ri = P['sid'].replace_strict(rowof, return_dtype=pl.Int64).to_numpy()
di = P['di'].to_numpy()
AO = np.full((NS, ND), np.nan, dtype=np.float32)
AC = np.full((NS, ND), np.nan, dtype=np.float32)
RO = np.full((NS, ND), np.nan, dtype=np.float32)
OK = np.zeros((NS, ND), dtype=bool)          # investable at that session's close
MED = np.full((NS, ND), np.nan, dtype=np.float32)
AO[ri, di], AC[ri, di], RO[ri, di] = P['aopen'].to_numpy(), P['aclose'].to_numpy(), P['open'].to_numpy()
MED[ri, di] = P['med126'].fill_null(np.nan).to_numpy()
OK[ri, di] = ((P['rows252'] >= 200) & (P['close'] >= 20)
              & (P['med126'].fill_null(0) >= 2e7)).to_numpy()
LC = pl.DataFrame(AC.T).with_columns(pl.all().fill_nan(None).forward_fill()).to_numpy().T
sid_at = dict(zip(zip(P['symbol'].to_list(), P['di'].to_list(), strict=True),
                  ri.tolist(), strict=True))
del P


def exit_price(r, x):
    """Adjusted open at session x, else the last close before it."""
    return AO[r, x] if not np.isnan(AO[r, x]) else LC[r, x - 1]


# Entry session: the first whose 09:15 start is after the broadcast.
starts = [dt.datetime.combine(d, OPEN, tzinfo=IST) for d in days]
import bisect   # noqa: E402
E = E.with_columns(pl.col('when').map_elements(lambda w: bisect.bisect_right(starts, w),
                                               return_dtype=pl.Int64).alias('e'))
E = E.filter((pl.col('e') >= 1) & (pl.col('e') < ND))
# Combine rows for the same symbol and entry session.
E = (E.group_by('symbol', 'e').agg(pl.col('value').sum(), pl.col('when').min(),
                                   pl.col('name').n_unique().alias('buyers'))
     .sort('e', 'symbol'))
rr = [sid_at.get((s, e - 1)) for s, e in E.select('symbol', 'e').iter_rows()]
E = E.with_columns(pl.Series('r', rr, dtype=pl.Int64))
matched = E.filter(pl.col('r').is_not_null())
print(f'events (symbol x entry session): {E.height}; check 3: matched to a price the session '
      f'before entry {matched.height / E.height:.1%}')
E = matched
inv = np.array([OK[r, e - 1] and not np.isnan(AO[r, e]) for r, e in E.select('r', 'e').iter_rows()])
E = E.filter(pl.Series(inv))
print(f'investable at entry (200 of 252 sessions, close >= Rs 20, median value >= Rs 2 crore, '
      f'traded at the entry open): {E.height}')

bench_cache = {}


def bench(e, H):
    k = (e, H)
    if k not in bench_cache:
        x = e + H
        rows_ = np.flatnonzero(OK[:, e - 1] & ~np.isnan(AO[:, e]))
        a = AO[rows_, e]
        b = np.where(np.isnan(AO[rows_, x]), LC[rows_, x - 1], AO[rows_, x])
        bench_cache[k] = float(np.nanmean(b / a - 1))
    return bench_cache[k]


def slip(med):
    return 0.3 if med < 1e8 else 0.1 if med < 5e8 else 0.0


def study(H, min_value, lo, hi):
    last = {}
    out = []
    for s, e, value, r, buyers in E.select('symbol', 'e', 'value', 'r', 'buyers').iter_rows():
        if not lo <= days[e] <= hi or e + H >= ND or value < min_value:
            continue
        if r in last and e < last[r] + H:
            continue
        last[r] = e
        ret = float(exit_price(r, e + H) / AO[r, e] - 1)
        if not np.isfinite(ret):
            continue
        b = bench(e, H)
        cost = cost_pct(float(RO[r, e])) + slip(float(MED[r, e - 1]))
        out.append({'symbol': s, 'entry': days[e], 'value_cr': value / 1e7, 'buyers': buyers,
                    'gross_pct': (ret - b) * 100, 'net_pct': (ret - b) * 100 - cost,
                    'ret_pct': ret * 100, 'bench_pct': b * 100, 'cost_pct': cost})
    return pl.DataFrame(out)


def summarize(T, label):
    m = (T.group_by(pl.col('entry').dt.strftime('%Y-%m').alias('m'))
         .agg(pl.col('net_pct').mean().alias('n'), pl.col('gross_pct').mean().alias('g')))
    n = m['n'].to_numpy()
    t = n.mean() / n.std(ddof=1) * math.sqrt(len(n)) if len(n) > 1 else float('nan')
    return {'variant': label, 'events': T.height, 'months': len(n),
            'net_per_event_%': round(T['net_pct'].mean(), 2),
            'gross_per_event_%': round(T['gross_pct'].mean(), 2),
            'median_net_%': round(T['net_pct'].median(), 2),
            'beat_bench_%': round((T['gross_pct'] > 0).mean() * 100, 1),
            'monthly_mean_net_%': round(n.mean(), 2), 't_calendar': round(t, 2),
            'avg_cost_%': round(T['cost_pct'].mean(), 2)}


def by_year(T):
    return (T.group_by(pl.col('entry').dt.year().alias('year'))
            .agg(pl.len().alias('events'), pl.col('net_pct').mean().round(2).alias('net_%'),
                 pl.col('gross_pct').mean().round(2).alias('gross_%')).sort('year'))


VARIANTS = {('V-all', 21): (0, 21), ('V-all', 63): (0, 63), ('V-1cr', 21): (1e7, 21),
            ('V-1cr', 63): (1e7, 63)}
pl.Config.set_tbl_rows(30)
pl.Config.set_tbl_cols(12)
pl.Config.set_tbl_width_chars(200)
if period == 'dev':
    res = []
    for (v, H), (mv, h) in VARIANTS.items():
        T = study(h, mv, *DEV)
        res.append(summarize(T, f'{v}, {H} sessions'))
        if (v, H) == ('V-all', 21):
            yr = by_year(T)
    print('Development, entries 1 Dec 2015 to 31 Dec 2020:')
    print(pl.DataFrame(res))
    print('V-all, 21 sessions, by year:')
    print(yr)
else:
    v, H = sys.argv[4], int(sys.argv[5])
    mv, h = VARIANTS[(v, H)]
    T = study(h, mv, HOLD_FROM, PANEL_END)
    print(f'Confirmation, entries 1 Jan 2021 to {T["entry"].max()}:')
    print(pl.DataFrame([summarize(T, f'{v}, {H} sessions')]))
    print(by_year(T))
