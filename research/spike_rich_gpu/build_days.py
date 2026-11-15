"""Build validated, prior-session-only spike candidate days from local candles.

Usage: python build_days.py CANDLES_DIR OUT_DIR
OUT_DIR must not already exist. The original spike event builder consumes days.parquet.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import polars as pl

START, END = 9 * 60 + 15, 15 * 60 + 25
MINUTES = tuple(range(START, END + 1, 5))
MIN_VALUE = 20_000_000
ROOT = Path(__file__).resolve().parent


def read_sessions(path: Path) -> tuple[list[dict], str, int]:
    raw = path.read_bytes()
    grouped: dict[str, list[tuple]] = defaultdict(list)
    for row in json.loads(raw):
        minute = int(row[0][11:13]) * 60 + int(row[0][14:16])
        if START <= minute <= END:
            grouped[row[0][:10]].append((minute, *(float(x) for x in row[1:6])))
    accepted, bad = [], 0
    for text_day, bars in grouped.items():
        bars.sort(key=lambda b: b[0])
        if (tuple(b[0] for b in bars) != MINUTES
                or any(not all(map(math.isfinite, b[1:])) or b[5] < 0
                       or not 0 < b[3] <= min(b[1], b[4]) <= max(b[1], b[4]) <= b[2]
                       for b in bars)):
            bad += 1
            continue
        prices = np.asarray(bars, dtype=float)
        opening, high, low, close, volume = (
            prices[:, 1], prices[:, 2], prices[:, 3], prices[:, 4], prices[:, 5])
        accepted.append({
            'day': dt.date.fromisoformat(text_day), 'bars': bars,
            'open': float(opening[0]), 'high': float(high.max()),
            'low': float(low.min()), 'close': float(close[-1]),
            'value': float((close * volume).sum()),
            'v_last30': float(volume[-6:].sum()),
            'last_hour': float(close[-1] / opening[MINUTES.index(14 * 60 + 30)] - 1),
            'volume': volume,
        })
    accepted.sort(key=lambda r: r['day'])
    return accepted, hashlib.sha256(raw).hexdigest(), bad


def atr_pct(previous: list[dict]) -> float:
    recent = previous[-15:]
    tr = [max(now['high'] - now['low'],
              abs(now['high'] - before['close']), abs(now['low'] - before['close']))
          for before, now in zip(recent[:-1], recent[1:], strict=True)]
    return statistics.mean(tr) / previous[-1]['close']


def one(stock: dict, directory: str, parts: str) -> dict:
    symbol, isin = stock['trading_symbol'], stock['isin']
    path = Path(directory) / (stock['instrument_key'].replace('|', '_') + '.json')
    sessions, digest, invalid = read_sessions(path)
    bars_out, context = [], []
    for index in range(21, len(sessions)):
        today = sessions[index]
        history = sessions[index - 21:index]
        prior, before = history[-1], history[-2]
        last20 = history[-20:]
        med20 = statistics.median(r['value'] for r in last20)
        if med20 < MIN_VALUE or today['high'] / today['low'] - 1 < .05:
            continue
        slot_med = np.median(np.stack([r['volume'] for r in last20]), axis=0)
        prior_volume = [r['v_last30'] for r in last20[:-1]]
        prior_high, prior_low = prior['high'], prior['low']
        daily_returns = [b['close'] / a['close'] - 1
                         for a, b in zip(history[:-1], history[1:], strict=True)]
        context.append({
            'isin': isin, 'day': today['day'], 'symbol': symbol,
            'd_prev_ret': prior['close'] / before['close'] - 1,
            'd_prev_gap': prior['open'] / before['close'] - 1,
            'd_prev_range': prior_high / prior_low - 1,
            'd_prev_clv': ((prior['close'] - prior_low) / (prior_high - prior_low)
                            if prior_high > prior_low else .5),
            'd_prev_last_hour': prior['last_hour'],
            'd_prev_last30_vol_ratio': (prior['v_last30'] / statistics.median(prior_volume)
                                        if statistics.median(prior_volume) > 0 else None),
            'd_ret5': prior['close'] / history[-6]['close'] - 1,
            'd_ret20': prior['close'] / history[0]['close'] - 1,
            'd_atr14_pct': atr_pct(history),
            'd_vol20': statistics.stdev(daily_returns),
            'd_value20': med20,
        })
        for bar, slot in zip(today['bars'], slot_med, strict=True):
            bars_out.append((isin, symbol, today['day'], True, med20, prior['close'],
                             prior['close'] / before['close'] - 1, *bar, float(slot)))
    if bars_out:
        cols = ('isin', 'symbol', 'day', 'nifty200', 'med20', 'prevclose', 'prev_ret',
                'minute', 'open', 'high', 'low', 'close', 'volume', 'slot_med20')
        frame = pl.DataFrame(bars_out, schema=cols, orient='row').with_columns(
            pl.col('day').cast(pl.Date), pl.col('minute').cast(pl.Int16))
        frame.write_parquet(Path(parts) / f'{isin}.parquet')
        pl.DataFrame(context).write_parquet(Path(parts) / f'{isin}.context.parquet')
    return {'symbol': symbol, 'sha256': digest, 'valid_sessions': len(sessions),
            'invalid_sessions': invalid, 'candidate_days': len(context)}


def build(directory: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    parts = output / 'parts'
    parts.mkdir()
    universe = json.loads((directory / 'universe.json').read_text())
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, universe, [str(directory)] * len(universe),
                                [str(parts)] * len(universe)))
    day_files = sorted(parts.glob('*.parquet'))
    context_files = sorted(parts.glob('*.context.parquet'))
    day_files = [p for p in day_files if not p.name.endswith('.context.parquet')]
    days = pl.concat([pl.read_parquet(p) for p in day_files], how='vertical').sort(
        'isin', 'day', 'minute')
    contexts = pl.concat([pl.read_parquet(p) for p in context_files], how='vertical').sort(
        'isin', 'day')
    days.write_parquet(output / 'days.parquet')
    contexts.write_parquet(output / 'context.parquet')
    index, index_hash, invalid_index = read_sessions(directory / 'NSE_INDEX_Nifty_50.json')
    calendar = [str(r['day']) for r in index]
    (output / 'calendar.json').write_text(json.dumps(calendar) + '\n')
    manifest = {'protocol_sha256': hashlib.sha256((ROOT / 'PROTOCOL.md').read_bytes()).hexdigest(),
                'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'stock_sources': results, 'nifty_sha256': index_hash,
                'invalid_nifty_sessions': invalid_index,
                'nifty_dates': len(calendar), 'stock_days': contexts.height,
                'candle_rows': days.height,
                'candidate_dates': contexts['day'].n_unique(),
                'excluded_invalid_stock_sessions': sum(r['invalid_sessions'] for r in results)}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print({key: manifest[key] for key in ('nifty_dates', 'stock_days', 'candle_rows',
          'candidate_dates', 'excluded_invalid_stock_sessions')}, flush=True)
    return manifest


if __name__ == '__main__':
    build(Path(sys.argv[1]), Path(sys.argv[2]))
