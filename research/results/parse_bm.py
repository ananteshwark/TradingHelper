"""Results board meetings from NSE's daily Bm files: (symbol, meeting date), with a
reschedule (a date within 20 days of an earlier one) resolved to the latest file's date.

Usage: uv run python -I parse_bm.py BM_DIR OUT.parquet
"""
import datetime as dt
import re
import sys
from pathlib import Path

import polars as pl

LINE = re.compile(r'^(.*?\S)\s+([A-Z0-9&\-]+)\s*:\s*(\d{1,2}-[A-Za-z]{3}-\d{4})\s*:\s*(.*)$')
rows, bad = [], 0
for path in sorted(Path(sys.argv[1]).glob('*.txt')):
    filed = dt.date.fromisoformat(path.stem)
    entries = []                      # [symbol, date text, purpose]; a purpose may run on
    for line in path.read_text(errors='replace').splitlines()[1:]:
        m = LINE.match(line.strip())
        if m:
            entries.append([m.group(2), m.group(3), m.group(4)])
        elif entries and line.strip():
            entries[-1][2] += ' ' + line.strip()
        elif line.strip():
            bad += 1
    for sym, date_text, purpose in entries:
        if 'result' not in purpose.lower():
            continue
        try:
            day = dt.datetime.strptime(date_text.title(), '%d-%b-%Y').date()
        except ValueError:
            bad += 1
            continue
        rows.append((sym, day, filed))
df = pl.DataFrame(rows, orient='row', schema=[('symbol', pl.Utf8), ('meeting', pl.Date),
                                              ('filed', pl.Date)]).unique()
# Resolve reschedules: per symbol, meetings within 20 days of each other form one event,
# dated by the most recently filed notice.
out = []
for (sym,), g in df.sort('meeting').group_by(['symbol'], maintain_order=True):
    cluster = []
    for r in g.iter_rows(named=True):
        if cluster and (r['meeting'] - cluster[0]['meeting']).days > 20:
            out.append(max(cluster, key=lambda x: (x['filed'], x['meeting'])))
            cluster = []
        cluster.append(r)
    if cluster:
        out.append(max(cluster, key=lambda x: (x['filed'], x['meeting'])))
ev = pl.DataFrame(out).select('symbol', 'meeting').sort('meeting', 'symbol')
ev.write_parquet(sys.argv[2])
print(len(rows), 'results notices,', ev.height, 'events,', ev['symbol'].n_unique(), 'companies;',
      bad, 'unparsed lines;', ev['meeting'].min(), 'to', ev['meeting'].max())
print(ev.group_by(pl.col('meeting').dt.year()).len().sort('meeting'))
