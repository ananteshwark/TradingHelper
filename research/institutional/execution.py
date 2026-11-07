"""Execution study of research/institutional/PROTOCOL.md: the momentum rebalance's trades at
the 09:15 open against VWAP and TWAP schedules and the close, on Upstox 5-minute candles.

Usage: uv run python -I execution.py CANDLE_DIR TRADES.parquet
"""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

src, trades_path = Path(sys.argv[1]), sys.argv[2]
universe = json.loads((src / 'universe.json').read_text())
key_of = {u['trading_symbol']: u['instrument_key'] for u in universe}
START, END = dt.date(2022, 3, 1), dt.date(2026, 10, 8)
SCHEDULES = ('vwap_1h', 'vwap_day', 'twap_day', 'close_30m')


def day_prices(path: Path) -> pl.DataFrame:
    """Per session: the 09:15 open and the price of each schedule."""
    c = pl.DataFrame(json.loads(path.read_text()), schema=['ts', 'o', 'h', 'l', 'c', 'v', 'oi'],
                     orient='row')
    c = c.with_columns(pl.col('ts').str.slice(0, 19).str.to_datetime('%Y-%m-%dT%H:%M:%S'))
    c = c.with_columns(pl.col('ts').dt.date().alias('day'), pl.col('ts').dt.time().alias('t'),
                       ((pl.col('h') + pl.col('l') + pl.col('c')) / 3).alias('tp'))
    c = c.filter(pl.col('day').is_between(START, END) & (pl.col('t') >= dt.time(9, 15))
                 & (pl.col('t') <= dt.time(15, 25))).sort('ts')

    def vwap(cond):
        return ((pl.col('tp') * pl.col('v')).filter(cond).sum()
                / pl.col('v').filter(cond).sum())
    return c.group_by('day').agg(
        pl.col('o').first().alias('open'), pl.col('t').first().alias('first'),
        vwap(pl.col('t') < dt.time(10, 15)).alias('vwap_1h'),
        vwap(pl.lit(True)).alias('vwap_day'),
        pl.col('c').mean().alias('twap_day'),
        vwap(pl.col('t') >= dt.time(15, 0)).alias('close_30m'),
        pl.col('v').sum().alias('volume')).filter(
        (pl.col('first') == dt.time(9, 15)) & (pl.col('volume') > 0))


trades = pl.read_parquet(trades_path).filter(pl.col('day') >= START)
cache: dict[str, pl.DataFrame] = {}
rows, missing = [], set()
for day, symbol, side in trades.iter_rows():
    key = key_of.get(symbol)
    if key is None:
        missing.add(symbol)
        continue
    if symbol not in cache:
        cache[symbol] = day_prices(src / (key.replace('|', '_') + '.json'))
    d = cache[symbol].filter(pl.col('day') == day)
    if d.is_empty():
        continue
    r = d.row(0, named=True)
    sign = 1 if side == 'buy' else -1          # saving: buy below the open, sell above it
    rows.append({'day': day, 'symbol': symbol, 'side': side,
                 **{s: sign * (r['open'] - r[s]) / r['open'] * 100 for s in SCHEDULES},
                 **{f'dev_{s}': abs(r[s] / r['vwap_day'] - 1) * 100
                    for s in ('open', 'vwap_1h', 'close_30m')}})
T = pl.DataFrame(rows)
print(f'Rebalance trades from {START}: {trades.height}; with 5-minute data: {T.height} '
      f'({T.filter(pl.col("side") == "buy").height} buys, '
      f'{T.filter(pl.col("side") == "sell").height} sells). '
      f'Not in the 197-stock candle set: {len(missing)} symbols.')


def summary(df: pl.DataFrame, label: str) -> None:
    print(f'\n{label}: saving against the 09:15 open, % (positive = better than the open)')
    for s in SCHEDULES:
        x = df[s].to_numpy()
        se = x.std(ddof=1) / np.sqrt(len(x))
        print(f'  {s:10s} mean {x.mean():+.3f} (se {se:.3f})  median {np.median(x):+.3f}  '
              f'sd {x.std(ddof=1):.2f}')


summary(T, 'All rebalance trades')
summary(T.filter(pl.col('side') == 'buy'), 'Buys')
summary(T.filter(pl.col('side') == 'sell'), 'Sells')
print('\nDistance from the session VWAP, mean absolute %, rebalance trades:')
for s in ('open', 'vwap_1h', 'close_30m'):
    print(f'  {s:10s} {T[f"dev_{s}"].mean():.3f}')

# All stock-days, for reference: the same prices for every stock and session.
alls = []
for u in universe:
    d = cache.get(u['trading_symbol'])
    if d is None:
        d = day_prices(src / (u['instrument_key'].replace('|', '_') + '.json'))
    alls.append(d.select('day', *[((pl.col('open') - pl.col(s)) / pl.col('open') * 100).alias(s)
                                  for s in SCHEDULES],
                         *[((pl.col(s) / pl.col('vwap_day') - 1).abs() * 100).alias(f'dev_{s}')
                           for s in ('open', 'vwap_1h', 'close_30m')]))
A = pl.concat(alls)
print(f'\nAll stock-days ({A.height}): open minus the schedule price, % of the open '
      f'(a buyer\'s saving; a seller\'s is the negative)')
for s in SCHEDULES:                  # sessions without candles after 15:00 have no close_30m
    x = A[s].to_numpy()
    x = x[~np.isnan(x)]
    print(f'  {s:10s} mean {x.mean():+.3f}  median {np.median(x):+.3f}  sd {x.std(ddof=1):.2f}'
          f'  ({len(x)} stock-days)')
print('Distance from the session VWAP, mean absolute %, all stock-days:')
for s in ('open', 'vwap_1h', 'close_30m'):
    print(f'  {s:10s} {np.nanmean(A[f"dev_{s}"].to_numpy()):.3f}')
by_year = T.group_by(pl.col('day').dt.year().alias('year'), 'side').agg(
    pl.len().alias('trades'), pl.col('vwap_1h').mean().round(3), pl.col('vwap_day').mean().round(3)
).sort('year', 'side')
print('\nBy year (saving of the first-hour and session VWAP against the open, %):')
with pl.Config(tbl_rows=20):
    print(by_year)
