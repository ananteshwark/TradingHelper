"""Price-adjustment events from NSE's daily corporate-action files (Bc*.csv in the PR archive):
bonuses, face-value splits and consolidations, for series EQ and BE.

The factor multiplies prices before the ex-date. A bonus a:b gives b/(a+b); a split or
consolidation from face value X to Y gives Y/X. When the text can't be parsed (NSE truncates
it to 25 characters), or the parsed factor is more than 25% away from the stock's overnight
move on the ex-date, the overnight move snapped to the nearest simple fraction is used.

Usage: uv run python -I corp_actions.py BC_DIR PARSED_DIR
"""
import csv
import datetime as dt
import re
import sys
from fractions import Fraction
from pathlib import Path

import polars as pl

KEYWORDS = re.compile(r'BONUS|BON\s*-?\s*\d|SPLIT|SPLT|FV\s*SPL|FVS|CONSOL|SUB-DIV|SUBDIV|CAP\. ?REDUC', re.I)
BONUS = re.compile(r'BON(?:US)?\s*-?\s*(\d+)\s*:\s*(\d+)', re.I)
SPLIT = re.compile(r'(?:SPLIT|SPLT|FV\s*SPL|FVS|CONSOLIDATION|CONSOL)\D*?(\d+(?:\.\d+)?)\D+?(\d+(?:\.\d+)?)', re.I)
NUM = r'\d+(?:\.\d+)?'


def parse(purpose):
    """Factor from the text, or None."""
    f = 1.0
    found = False
    for m in BONUS.finditer(purpose):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            f *= b / (a + b)
            found = True
    rest = BONUS.sub(' ', purpose)
    m = SPLIT.search(rest)
    if m:
        x, y = float(m.group(1)), float(m.group(2))
        if x > 0 and y > 0 and x != y:
            f *= y / x
            found = True
    return f if found else None


def snap(ratio):
    best = min((Fraction(p, q) for p in range(1, 21) for q in range(1, 21)),
               key=lambda fr: abs(float(fr) - ratio))
    return float(best) if abs(float(best) / ratio - 1) < 0.05 else None


if __name__ == '__main__':
    bc_dir, parsed = Path(sys.argv[1]), Path(sys.argv[2])
    events = {}
    for path in sorted(bc_dir.glob('*.csv')):
        for row in csv.DictReader(path.read_text(errors='replace').splitlines()):
            row = {k.strip(): (v or '').strip() for k, v in row.items() if isinstance(k, str) and isinstance(v, (str, type(None)))}
            if row.get('SERIES') not in ('EQ', 'BE') or not KEYWORDS.search(row.get('PURPOSE', '')):
                continue
            ex = None
            for fmt in ('%d/%m/%Y', '%Y-%m-%d'):          # ISO from 2025
                try:
                    ex = dt.datetime.strptime(row['EX_DT'], fmt).date()
                except ValueError:
                    pass
            if ex is None:
                continue
            events[(row['SYMBOL'], ex)] = row['PURPOSE']
    bhav = pl.read_parquet(parsed / 'bhav.parquet').select('day', 'symbol', 'open', 'close') \
        .sort('symbol', 'day')
    bhav = bhav.with_columns(pl.col('close').shift(1).over('symbol').alias('prev_close'),
                             pl.col('day').shift(1).over('symbol').alias('prev_day'))
    ev = pl.DataFrame([(s, d, pu) for (s, d), pu in events.items()], orient='row',
                      schema=[('symbol', pl.Utf8), ('ex', pl.Date), ('purpose', pl.Utf8)]).sort('ex')
    hits = ev.join_asof(bhav.sort('day'), left_on='ex', right_on='day', by='symbol',
                        strategy='forward')
    rows, unresolved = [], []
    for sym, ex, purpose, day, o, prev_close in hits.select(
            'symbol', 'ex', 'purpose', 'day', 'open', 'prev_close').iter_rows():
        if day is None or prev_close is None or (day - ex).days > 10:
            continue
        observed = o / prev_close
        parsed_f = parse(purpose)
        if parsed_f is not None and abs(parsed_f / observed - 1) <= 0.25:
            rows.append((sym, day, parsed_f, purpose, 'text', observed))
            continue
        if parsed_f is not None:
            near = bhav.filter((pl.col('symbol') == sym) & pl.col('day').is_between(
                ex - dt.timedelta(days=7), ex + dt.timedelta(days=21)) & pl.col('prev_close').is_not_null())
            near = near.with_columns((pl.col('open') / pl.col('prev_close')).alias('obs')).filter(
                ((pl.col('obs') / parsed_f) - 1).abs() <= 0.10)
            if near.height == 1:
                rows.append((sym, near['day'][0], parsed_f, purpose, 'text, moved', near['obs'][0]))
                continue
        snapped = snap(observed) if abs(observed - 1) > 0.15 else None
        if snapped is not None:
            rows.append((sym, day, snapped, purpose, 'snapped', observed))
        else:
            unresolved.append((sym, str(day), purpose, round(observed, 3), parsed_f))
    out = pl.DataFrame(rows, orient='row', schema=[('symbol', pl.Utf8), ('day', pl.Date),
                       ('factor', pl.Float64), ('purpose', pl.Utf8), ('how', pl.Utf8),
                       ('observed', pl.Float64)])
    out.write_parquet(parsed / 'corp_actions.parquet')
    print(len(events), 'events in the files;', out.height, 'adjustments',
          out.group_by('how').len().rows(), '; unresolved', len(unresolved))
    for u in unresolved[:40]:
        print('  unresolved', u)
