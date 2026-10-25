"""The app's evening features (igs.intraday.evening) against the research table, on real data.

Usage: python check_features.py DATA_DIR FEATURES.parquet STEP   (every STEP-th stock)
"""
import datetime as dt
import json
import sys
from pathlib import Path

import polars as pl

from igs.intraday import evening, ml
from igs.intraday.engine import Candle

data, table = Path(sys.argv[1]), pl.read_parquet(sys.argv[2])
uni = json.loads((data / 'universe.json').read_text())[:: int(sys.argv[3])]
COLS = [c for c in evening.model().features if not c.startswith('dow_')] + ['value20']


BAD = 0


def sessions(path):
    """Candles the app's Candle accepts (the live feed rejects a response with a bad one;
    here the bad candle is dropped and counted, as research/ml/check_ml_features.py does)."""
    global BAD
    out = []
    for r in json.loads(path.read_text()):
        try:
            out.append(Candle(dt.datetime.fromisoformat(r[0]), *map(float, r[1:6])))
        except ValueError:
            BAD += 1
    return ml.sessions(out)


idx = sessions(data / 'NSE_INDEX_Nifty_50.json')
idx_dates = sorted(idx)
idx_sum = {d: evening.summary(idx[d]) for d in idx_dates}
compared = mismatched = missing = 0
worst = 0.0
for u in uni:
    rows = {r['day']: r for r in
            table.filter(pl.col('symbol') == u['trading_symbol']).iter_rows(named=True)}
    days = sessions(data / (u['instrument_key'].replace('|', '_') + '.json'))
    dates = sorted(days)
    summ = {d: evening.summary(days[d]) for d in dates}
    for n, day in enumerate(dates):
        ref = rows.get(day)
        if ref is None or n < evening.HISTORY:
            continue
        k = idx_dates.index(day) if day in idx else None
        index = (evening.index_features([idx_sum[x] for x in idx_dates[k - 22:k]])
                 if k and k >= 22 else None)
        got = evening.stock_features([summ[x] for x in dates[n - 22:n]], index, day)
        if got is None:
            missing += 1
            continue
        compared += 1
        diff = max(abs(got[c] - ref[c]) for c in COLS)
        worst = max(worst, diff)
        if diff > 1e-9:
            mismatched += 1
            if mismatched <= 5:
                print(u['trading_symbol'], day,
                      {c: (got[c], ref[c]) for c in COLS if abs(got[c] - ref[c]) > 1e-9})
print(f'{len(uni)} stocks, {compared} days compared, {mismatched} differ (worst {worst:.2e}), '
      f'{missing} research rows not scored by the app (a missing input); '
      f'{BAD} candles the app rejects')
