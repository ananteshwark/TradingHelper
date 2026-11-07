"""The app module's features (igs.intraday.ml) against the research table, on real data."""
import datetime as dt
import json
import sys
from pathlib import Path

import polars as pl

from igs.intraday import ml
from igs.intraday.engine import Candle

data, table = Path(sys.argv[1]), pl.read_parquet(sys.argv[2])
uni = json.loads((data / 'universe.json').read_text())[:: int(sys.argv[3])]


def candles(path):
    out, bad = [], 0
    for r in json.loads(path.read_text()):
        try:
            out.append(Candle(dt.datetime.fromisoformat(r[0]), *map(float, r[1:6])))
        except ValueError:
            bad += 1
    return out, bad


idx_c, _ = candles(data / 'NSE_INDEX_Nifty_50.json')
idx = ml.sessions(idx_c)
idx_dates = sorted(idx)
idx_sum = {d: ml.summary(idx[d]) for d in idx_dates}
compared = mismatched = missing = 0
worst = 0.0
cols = [c for c in table.columns if c not in ('symbol', 'day', 'entry', 'exit', 'target', 'dow')]
for u in uni:
    rows = {r['day']: r for r in table.filter(pl.col('symbol') == u['trading_symbol']).iter_rows(named=True)}
    cs, bad = candles(data / (u['instrument_key'].replace('|', '_') + '.json'))
    days = ml.sessions(cs)
    dates = sorted(days)
    summ = {d: ml.summary(days[d]) for d in dates}
    for n, d in enumerate(dates):
        if d not in rows:
            continue
        k = idx_dates.index(d) if d in idx else None
        index = ml.index_features([idx_sum[x] for x in idx_dates[:k]], idx[d]) if k else None
        got = ml.stock_features([summ[x] for x in dates[:n]], days[d], index, d)
        if got is None:
            missing += 1
            continue
        compared += 1
        ref = rows[d]
        diff = max(abs(got[c] - ref[c]) for c in cols if c in got)
        worst = max(worst, diff)
        if diff > 1e-9:
            mismatched += 1
            if mismatched <= 6:
                print(u['trading_symbol'], d, 'bad candles in file', bad,
                      {c: (got[c], ref[c]) for c in cols if c in got and abs(got[c] - ref[c]) > 1e-9})
print(f'{len(uni)} stocks, {compared} days compared, {mismatched} differ (worst {worst:.2e}), '
      f'{missing} research rows the module could not rebuild')
