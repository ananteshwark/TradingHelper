"""Remove training rows influenced by malformed source candles for a sensitivity fit.

Usage: python quality_stress.py CANDLES_DIR AUGMENTED.parquet CLEAN.parquet
This is a post-result sensitivity check, not a new independent test. It drops the
invalid session and 22 subsequent sessions for each affected stock, because the
research feature window can include that session while production rejects it.
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import polars as pl


def invalid_dates(directory: Path) -> dict[str, set[str]]:
    bad: dict[str, set[str]] = defaultdict(set)
    universe = json.loads((directory / 'universe.json').read_text())
    for stock in universe:
        path = directory / (stock['instrument_key'].replace('|', '_') + '.json')
        for raw in json.loads(path.read_text()):
            o, h, lo, c, v = map(float, raw[1:6])
            if not (all(map(math.isfinite, (o, h, lo, c, v))) and v >= 0
                    and 0 < lo <= min(o, c) <= max(o, c) <= h):
                bad[stock['trading_symbol']].add(raw[0][:10])
    return dict(bad)


def clean(directory: Path, source: Path, output: Path) -> dict:
    data = pl.read_parquet(source)
    bad = invalid_dates(directory)
    excluded = []
    for symbol, invalid in bad.items():
        days = sorted(data.filter(pl.col('symbol') == symbol)['day'].to_list())
        for date in invalid:
            for index, day in enumerate(days):
                if str(day) == date:
                    excluded.extend((symbol, later) for later in days[index:index + 23])
                    break
    keys = pl.DataFrame(excluded, schema={'symbol': pl.String, 'day': pl.Date},
                        orient='row').unique()
    kept = data.join(keys, on=['symbol', 'day'], how='anti')
    if kept.height + keys.height != data.height:
        raise ValueError('Excluded keys did not match feature rows exactly')
    output.parent.mkdir(parents=True, exist_ok=True)
    kept.write_parquet(output)
    result = {'invalid_stocks': len(bad),
              'invalid_dates': sum(map(len, bad.values())),
              'excluded_stock_days': keys.height,
              'source_rows': data.height, 'clean_rows': kept.height,
              'affected_dates': sorted({date for dates in bad.values() for date in dates})}
    output.with_suffix('.quality.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    print(clean(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
