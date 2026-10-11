"""Event flags and move labels for research/move_starts/PROTOCOL.md.

Usage: uv run python -I build_events.py SCRATCH OUT_NPZ
SCRATCH holds promoter/all.parquet (+ all_days.parquet), research/parsed/deliv.parquet,
research/results/events.parquet, ideas/pit/*.json, ideas/index/events.json,
fo/parsed/futures.parquet, moves/ann/*.jsonl and moves/deals/*.json; the keyword rules
for announcements and deals are in events_rules.py beside this script.
Saves boolean event matrices (stock x session), the investable mask, prices and labels.
"""
import bisect
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
from events_rules import announcement_group, is_institution   # noqa: E402

S, out = Path(sys.argv[1]), Path(sys.argv[2])
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OPEN = dt.time(9, 15)

P = pl.read_parquet(S / 'ideas/promoter/all.parquet')
days = pl.read_parquet(S / 'ideas/promoter/all_days.parquet')['day'].to_list()
ND = len(days)
pos = {d: i for i, d in enumerate(days)}
starts = [dt.datetime.combine(d, OPEN, tzinfo=IST) for d in days]
sids = P['sid'].unique().sort().to_list()
rowof = {s: i for i, s in enumerate(sids)}
NS = len(sids)
P = P.with_columns(pl.col('sid').replace_strict(rowof, return_dtype=pl.Int64).alias('r'))

deliv = (pl.read_parquet(S / 'research/parsed/deliv.parquet')
         .select('day', 'symbol', 'series', 'deliv_qty'))
P = P.join(deliv, on=['day', 'symbol', 'series'], how='left').sort('r', 'di')
P = P.with_columns((pl.col('deliv_qty') * pl.col('close')).alias('dval'))


def mat(col, dtype=np.float32):
    m = np.full((NS, ND), np.nan, dtype=dtype)
    m[P['r'].to_numpy(), P['di'].to_numpy()] = P[col].fill_null(np.nan).to_numpy()
    return m


AO, AC, RO, VAL = mat('aopen'), mat('aclose'), mat('open'), mat('value')
MED = mat('med126')
OK = np.zeros((NS, ND), dtype=bool)
OK[P['r'].to_numpy(), P['di'].to_numpy()] = ((P['rows252'] >= 200) & (P['close'] >= 20)
                                             & (P['med126'].fill_null(0) >= 2e7)).to_numpy()
LC = pl.DataFrame(AC.T).with_columns(pl.all().fill_nan(None).forward_fill()).to_numpy().T
LC = LC.astype(np.float32)
sid_at = dict(zip(zip(P['symbol'].to_list(), P['di'].to_list(), strict=True),
                  P['r'].to_list(), strict=True))
print('stocks', NS, 'sessions', ND, 'investable stock-days', int(OK.sum()), flush=True)

# ------------------------------------------------------------------ price and volume events
w = lambda e: e.over('r')     # noqa: E731
prev50 = lambda c: w(pl.col(c).shift(1).rolling_mean(50, min_samples=40))   # noqa: E731
F = P.select('r', 'di',
             (pl.col('value') >= 3 * prev50('value')).alias('VOL3'),
             (pl.col('dval') >= 3 * prev50('dval')).alias('DLV3'),
             (pl.col('aclose') > w(pl.col('aclose').shift(1).rolling_max(250, min_samples=200)))
             .alias('HI250'),
             (pl.col('aclose') / w(pl.col('aclose').shift(1)) - 1).alias('ret1'),
             (pl.col('aopen') / w(pl.col('aclose').shift(1)) - 1).alias('gap'))
ret1 = np.full((NS, ND), np.nan, dtype=np.float32)
ret1[F['r'].to_numpy(), F['di'].to_numpy()] = F['ret1'].fill_null(np.nan).to_numpy()
mkt = np.array([np.nanmean(ret1[OK[:, d - 1], d]) if d and OK[:, d - 1].any() else 0.0
                for d in range(ND)], dtype=np.float32)
E = {}


def flag(name, rows, cols):
    m = E.setdefault(name, np.zeros((NS, ND), dtype=bool))
    m[np.asarray(rows, dtype=np.int64), np.asarray(cols, dtype=np.int64)] = True


for c in ('VOL3', 'DLV3', 'HI250'):
    g = F.filter(pl.col(c).fill_null(False))
    flag(c, g['r'], g['di'])
ab = ret1 - mkt[None, :]
E['UP5'] = np.nan_to_num(ab) >= 0.05
E['DN5'] = np.nan_to_num(ab) <= -0.05
g = F.filter(pl.col('gap') >= 0.03)
flag('GAPU3', g['r'], g['di'])
g = F.filter(pl.col('gap') <= -0.03)
flag('GAPD3', g['r'], g['di'])
print('price events done', flush=True)


def session_on_or_before(day):
    i = bisect.bisect_right(days, day) - 1
    return i if i >= 0 else None


def session_known(when):
    """The last session whose 09:15 open came before `when` (the event's session t)."""
    i = bisect.bisect_right(starts, when) - 1
    return i if i >= 0 else None


def add(name, symbol, t):
    if t is None:
        return False
    r = sid_at.get((symbol, t))
    if r is None:
        return False
    flag(name, [r], [t])
    return True


# ------------------------------------------------------------------ results days
res = pl.read_parquet(S / 'research/results/events.parquet')
n = sum(add('RES', s, session_on_or_before(m)) for s, m in res.iter_rows())
print('results meetings matched', n, 'of', res.height, flush=True)

# ------------------------------------------------------------------ promoter trades
seen = set()
counts = {'PBUY': 0, 'PSELL': 0}
for f in sorted((S / 'ideas/pit').glob('*.json')):
    for x in json.loads(f.read_text()).get('data', []):
        if x.get('personCategory') not in ('Promoters', 'Promoter Group'):
            continue
        kind = ('PBUY' if (x.get('acqMode'), x.get('tdpTransactionType')) == ('Market Purchase', 'Buy')
                else 'PSELL' if (x.get('acqMode'), x.get('tdpTransactionType')) == ('Market Sale', 'Sell')
                else None)
        key = (x.get('pid'), x.get('did'), x.get('symbol'), x.get('date'))
        if kind is None or key in seen:
            continue
        seen.add(key)
        try:
            when = dt.datetime.strptime(x['date'].strip(), '%d-%b-%Y %H:%M').replace(tzinfo=IST)
        except (ValueError, KeyError, AttributeError):
            continue
        counts[kind] += add(kind, x['symbol'].strip(), session_known(when))
print('promoter trades matched', counts, flush=True)

# ------------------------------------------------------------------ index inclusions
rows = json.loads((S / 'ideas/index/events.json').read_text())
first = {}
for r in rows:
    first.setdefault((r['release'], r['file'], r['index'], r['symbol']), r['action'])
n = 0
for r in rows:
    if r['action'] == 'include' and first[(r['release'], r['file'], r['index'], r['symbol'])] == 'include':
        d = dt.datetime.strptime(r['release'], '%b %d, %Y').date()
        n += add('IDXIN', r['symbol'], session_on_or_before(d))
print('index inclusions matched', n, flush=True)

# ------------------------------------------------------------------ futures open interest
fut = (pl.read_parquet(S / 'fo/parsed/futures.parquet').filter(pl.col('kind') == 'STF')
       .group_by('day', 'symbol').agg(pl.col('oi').sum()).sort('symbol', 'day'))
fut = fut.with_columns((pl.col('oi') / pl.col('oi').shift(5).over('symbol') - 1).alias('doi'))
FUT = np.zeros((NS, ND), dtype=bool)             # stocks with futures that day
for sym, d in fut.select('symbol', 'day').iter_rows():
    t = pos.get(d)
    r = sid_at.get((sym, t)) if t is not None else None
    if r is not None:
        FUT[r, t] = True
for sym, d, doi in fut.filter(pl.col('doi') >= 0.25).select('symbol', 'day', 'doi').iter_rows():
    t = pos.get(d)
    r = sid_at.get((sym, t)) if t is not None else None
    if r is None or t < 5 or np.isnan(AC[r, t]) or np.isnan(LC[r, t - 5]):
        continue
    flag('OILONG' if AC[r, t] > LC[r, t - 5] else 'OISHORT', [r], [t])
print('futures build-ups', int(E.get('OILONG', np.zeros(1)).sum()),
      int(E.get('OISHORT', np.zeros(1)).sum()), flush=True)

# ------------------------------------------------------------------ announcements
groups, seen_ann = {}, set()
for f in sorted((S / 'moves/ann').glob('*.jsonl')):
    for line in f.read_text().splitlines():
        x = json.loads(line)
        key = (x.get('symbol'), x.get('an_dt'), x.get('desc'), x.get('text'))
        if key in seen_ann:            # a week fetched twice after an interruption
            continue
        seen_ann.add(key)
        try:
            when = dt.datetime.strptime(x['an_dt'], '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
        except (ValueError, TypeError, KeyError):
            continue
        g = announcement_group(x.get('desc') or '', x.get('text') or '')
        if g is None:
            continue
        ok = add('A_' + g, x.get('symbol') or '', session_known(when))
        groups[g] = groups.get(g, 0) + ok
print('announcements matched by group', dict(sorted(groups.items())), flush=True)

# ------------------------------------------------------------------ block deals (bulk deals can't be collected whole)
net = {}
for f in sorted((S / 'moves/block').glob('*.json')):
    for x in json.loads(f.read_text()).get('data', []):
        try:
            d = dt.datetime.strptime(x['BD_DT_DATE'], '%d-%b-%Y').date()
        except (ValueError, KeyError):
            continue
        q = float(x.get('BD_QTY_TRD') or 0) * (1 if x.get('BD_BUY_SELL') == 'BUY' else -1)
        key = (x.get('BD_SYMBOL'), d, x.get('BD_CLIENT_NAME') or '')
        net[key] = net.get(key, 0.0) + q
deal_counts = {}
for (sym, d, client), q in net.items():
    if q == 0:
        continue                    # bought and sold the same day: a trader, not a holder
    inst = is_institution(client)
    name = ('D_INST_' if inst else 'D_ANY_') + ('BUY' if q > 0 else 'SELL')
    t = pos.get(d)
    ok = add(name, sym, t)
    if inst and ok:                 # an institution is also "anyone"
        add('D_ANY_' + ('BUY' if q > 0 else 'SELL'), sym, t)
    deal_counts[name] = deal_counts.get(name, 0) + ok
print('net deals matched', dict(sorted(deal_counts.items())), flush=True)

# ------------------------------------------------------------------ labels (Part 1 only)
bench63 = np.full(ND, np.nan, dtype=np.float32)
for t in range(ND - 63):
    rows_ = np.flatnonzero(OK[:, t])
    if len(rows_):
        bench63[t] = np.nanmean(LC[rows_, t + 63] / AC[rows_, t] - 1)
fwd63 = LC[:, np.minimum(np.arange(ND) + 63, ND - 1)] / AC - 1 - bench63[None, :]
fwd63[:, ND - 63:] = np.nan
pad = np.full((NS, 10), np.nan, dtype=np.float32)
Ap = np.concatenate([pad, AC, pad], axis=1)
win = np.lib.stride_tricks.sliding_window_view(Ap, 21, axis=1)
lo, hi = np.nanmin(win, axis=2), np.nanmax(win, axis=2)
with np.errstate(invalid='ignore'):
    UPSTART = OK & (fwd63 >= 0.30) & (AC <= lo)
    DNSTART = OK & (fwd63 <= -0.25) & (AC >= hi)
print('up-move starts', int(UPSTART.sum()), 'down-move starts', int(DNSTART.sum()), flush=True)

np.savez_compressed(out, days=np.array([d.toordinal() for d in days]), OK=OK, AO=AO, AC=AC,
                    RO=RO, LC=LC, MED=MED, VAL=VAL, FUT=FUT, UPSTART=UPSTART, DNSTART=DNSTART,
                    names=np.array(sorted(E)), **{f'E_{k}': v for k, v in E.items()})
print('saved', out, sorted(E), flush=True)
