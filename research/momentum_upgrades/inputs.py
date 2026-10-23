"""The study's inputs, rebuilt from NSE's archives with the definitions of
research/ml_rating (its scratch files were lost with a cloud container):

- universe500.parquet: at each month end from June 2012, the 500 stocks with the highest
  median traded value over 126 sessions. ml_rating's PROTOCOL.md also asks for 200 of the
  past 252 sessions, but its data didn't apply that: only without it does this rebuild
  start in June 2012 and leave the momentum percentile missing for the same share of
  stocks each year (recent listings), as the original files did;
- pred.parquet: each universe stock's momentum percentile and return to the next month
  end, as research/ml_rating/features.py and model.py compute them;
- quarters.parquet: each stock's quarters with their first broadcast, chosen from NSE's
  results listings as research/ml_rating/fetch_files.py chooses them.

Usage: uv run --no-project --with polars --with numpy python -I inputs.py \
         ALL_PARQUET RANGES_DIR IFPAGES_DIR OUT_DIR
ALL_PARQUET is research/promoter/build_all.py's output; RANGES_DIR and IFPAGES_DIR
research/ml_rating's fetch_ranges.py and fetch_if.py listings.
"""
import datetime as dt
import json
import math
import sys
from pathlib import Path

import polars as pl

all_path, ranges, ifpages, out = (Path(a) for a in sys.argv[1:5])
TOP = 500
FIRST = dt.date(2010, 12, 31)

# ------------------------------------------------------------------ the universe
px_all = pl.scan_parquet(all_path)
sessions = pl.read_parquet(all_path.with_name('all_days.parquet'))['day'].sort()
month_end = (pl.DataFrame({'day': sessions})
             .group_by(pl.col('day').dt.strftime('%Y-%m').alias('m')).agg(pl.col('day').max())
             .sort('m')['day'].to_list())
uni = (px_all.filter(pl.col('day').is_in(month_end) & (pl.col('day') >= dt.date(2012, 6, 29))
                     & pl.col('med126').is_not_null())
       .select('day', 'sid', 'symbol', 'isin', 'med126').collect()
       .sort(['day', 'med126', 'sid'], descending=[False, True, False])
       .with_columns(pl.int_range(1, pl.len() + 1).over('day').cast(pl.UInt32).alias('r'))
       .filter(pl.col('r') <= TOP))
uni.write_parquet(out / 'universe500.parquet')
print('universe', uni['day'].min(), uni['day'].max(), uni['day'].n_unique(), 'month ends',
      uni.height, 'rows', uni['sid'].n_unique(), 'stocks', flush=True)

# ------------------------------------------------------- momentum and forward returns
# research/ml_rating/features.py, its price section.
sids = uni['sid'].unique().implode()
px = (px_all.filter((pl.col('series') == 'EQ') & pl.col('sid').is_in(sids))
      .select('sid', 'day', 'close', 'aclose', 'value', 'med126').collect()
      .unique(['sid', 'day']).sort('sid', 'day'))
days = px.select('day').unique().sort('day')
MES = (days.with_columns(pl.col('day').dt.strftime('%Y-%m').alias('m'))
       .group_by('m').agg(pl.col('day').max()).sort('m')['day'].to_list())
px = px.with_columns(
    (pl.col('aclose') / pl.col('aclose').shift(1).over('sid')).log().alias('lr'))
px = px.with_columns(
    (pl.col('lr').rolling_std(252, min_samples=200).over('sid') * math.sqrt(252)).alias('vol'),
    pl.col('aclose').shift(126).over('sid').alias('c126'),
    pl.col('aclose').shift(252).over('sid').alias('c252'))
me_of = pl.DataFrame({'me': MES}).with_columns(pl.col('me').dt.strftime('%Y-%m').alias('m'))
px = (px.with_columns(pl.col('day').dt.strftime('%Y-%m').alias('m'))
        .join(me_of, on='m'))
last_in_month = (px.group_by('sid', 'm').agg(pl.col('day').max())
                   .join(me_of, on='m')
                   .filter(pl.col('day') >= pl.col('me').dt.offset_by('-5d')))
at_me = px.join(last_in_month.select('sid', 'day'), on=['sid', 'day']).select(
    'sid', 'me', 'aclose', 'vol',
    (pl.col('aclose') / pl.col('c126') - 1).alias('ret126'),
    (pl.col('aclose') / pl.col('c252') - 1).alias('ret252'))
me_df = pl.DataFrame({'me': MES})
last_close = px.select('sid', 'day', 'aclose').sort('day')
tgt = me_df.with_columns(pl.col('me').shift(-1).alias('to')).drop_nulls('to')
a = at_me.select('sid', 'me', 'aclose').join(tgt.select('me', 'to'), on='me')
a = a.sort('to').join_asof(last_close.rename({'day': 'last_day', 'aclose': 'end'}),
                           left_on='to', right_on='last_day', by='sid', strategy='backward')
a = a.filter(pl.col('last_day') >= pl.col('me'))
fwd = a.select('sid', 'me', (pl.col('end') / pl.col('aclose') - 1).alias('fwd_1m'))
feat = at_me.join(fwd, on=['sid', 'me'], how='left').with_columns(
    (pl.col('ret126') / pl.col('vol')).alias('risk_adj_return_6m'),
    (pl.col('ret252') / pl.col('vol')).alias('risk_adj_return_12m'))


# research/ml_rating/model.py: percentile ranks within the universe's month, and the
# momentum pillar as their mean.
def pct(c):
    r = pl.col(c).rank('average').over('me')
    n = pl.col(c).count().over('me')
    return pl.when(n > 1).then((r - 1) / (n - 1))


x = feat.join(uni.select('sid', pl.col('day').alias('me')), on=['sid', 'me'])
x = x.with_columns(pct('risk_adj_return_6m').alias('r6'), pct('risk_adj_return_12m').alias('r12'))
have = pl.col('r6').is_not_null().cast(pl.Float64) + pl.col('r12').is_not_null().cast(pl.Float64)
x = x.with_columns(pl.when(have > 0).then(
    (pl.col('r6').fill_null(0) + pl.col('r12').fill_null(0)) / have).alias('p_momentum'))
x.select('sid', 'me', 'fwd_1m', 'p_momentum').sort('me', 'sid').write_parquet(out / 'pred.parquet')
print('pred', x.height, 'rows; momentum known for', f"{x['p_momentum'].is_not_null().mean():.1%}",
      flush=True)

# ------------------------------------------------------------------ results broadcasts
# research/ml_rating/fetch_files.py's choice: each quarter's first standalone filing.
ids = px_all.select('sid', 'symbol', 'isin', 'day').collect()
by_isin = {}
for sid, isin in ids.select('sid', 'isin').unique().drop_nulls().iter_rows():
    by_isin.setdefault(isin, set()).add(sid)
by_symbol = {}
spans = ids.group_by('sid', 'symbol').agg(pl.col('day').min().alias('f'),
                                         pl.col('day').max().alias('l'))
for sid, symbol, first, last in spans.iter_rows():
    by_symbol.setdefault(symbol, []).append((sid, first, last))


def match(symbol, isin, when):
    near = [sid for sid, first, last in by_symbol.get(symbol, [])
            if first - dt.timedelta(days=400) <= when.date() <= last + dt.timedelta(days=400)]
    hits = by_isin.get(isin, set())
    if len(hits) == 1:
        return next(iter(hits))
    if len(near) == 1:
        return near[0]
    both = hits & set(near)
    return next(iter(both)) if len(both) == 1 else None


def when_of(v, fmt='%d-%b-%Y %H:%M:%S'):
    try:
        return dt.datetime.strptime(v, fmt)  # noqa: DTZ007 (NSE's times are IST, kept naive)
    except (TypeError, ValueError):
        return None


universe_sids = set(uni['sid'].unique().to_list())
first_seen = {}


def keep(sid, end, when):
    if sid is None or sid not in universe_sids or end < FIRST or when is None:
        return
    k = (sid, end)
    if k not in first_seen or when < first_seen[k]:
        first_seen[k] = when


for p in sorted(ranges.glob('*.json')):
    for r in json.loads(p.read_text()):
        if r.get('consolidated') != 'Non-Consolidated' or \
                r.get('cumulative') != 'Non-cumulative':
            continue
        when = when_of(r.get('broadCastDate')) or when_of(r.get('filingDate'), '%d-%b-%Y %H:%M')
        end = when_of(r.get('toDate'), '%d-%b-%Y')
        url = r.get('xbrl') if r.get('xbrl') and not r['xbrl'].endswith('/-') else \
            r.get('resultDetailedDataLink')
        if end and when and url:
            keep(match(r.get('symbol'), r.get('isin'), when), end.date(), when)
for p in sorted(ifpages.glob('*.json')):
    for r in json.loads(p.read_text()).get('data', []):
        if r.get('consolidated') != 'Standalone':
            continue
        when = when_of(r.get('broadcast_Date')) or when_of(r.get('creation_Date'))
        end = when_of((r.get('qe_Date') or '').title(), '%d-%b-%Y')
        if end and when and r.get('xbrl'):
            keep(match(r.get('symbol'), None, when), end.date(), when)
q = pl.DataFrame([(s, e, w) for (s, e), w in first_seen.items()], orient='row',
                 schema=[('sid', pl.Int64), ('period_end', pl.Date), ('broadcast', pl.Datetime)])
q.sort('sid', 'period_end').write_parquet(out / 'quarters.parquet')
print('quarters', q.height, 'for', q['sid'].n_unique(), 'stocks', flush=True)
