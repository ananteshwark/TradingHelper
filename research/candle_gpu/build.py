"""Add causal opening-candle/volume features to the established intraday feature set.

Usage: python build.py CANDLES_DIR BASE_FEATURES.parquet OUT.parquet
The base file is produced by research/ml/intraday_features.py. Only 09:15-09:40 bars
enter predictors; the 09:50 and 15:00 opens are labels, never predictors.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STARTS = tuple(dt.time(9, 15 + 5 * n) for n in range(6))


def correlation(a: list[float], b: list[float]) -> float:
    """Six-bar Pearson correlation; zero for an uninformative constant sequence."""
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    numerator = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    return numerator / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0


def opening_features(bars: list[tuple], nifty: list[tuple] | None = None) -> dict | None:
    """Predictors from six completed bars and label prices from later opens."""
    by_time = {bar[0]: bar for bar in bars}
    six = [by_time.get(t) for t in STARTS]
    entry, exit_ = by_time.get(dt.time(9, 50)), by_time.get(dt.time(15, 0))
    if any(bar is None for bar in six) or entry is None or exit_ is None:
        return None
    if any(not all(math.isfinite(v) and v > 0 for v in bar[1:5])
           or not math.isfinite(bar[5]) or bar[5] < 0 for bar in six):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (entry[1], exit_[1])):
        return None
    opening = six[0][1]
    volumes = [bar[5] for bar in six]
    total = sum(volumes)
    bodies = [bar[4] / bar[1] - 1 for bar in six]
    ranges = [(bar[2] - bar[3]) / bar[1] for bar in six]
    close_moves = [six[0][4] / opening - 1] + [
        six[i][4] / six[i - 1][4] - 1 for i in range(1, 6)]
    first, last = six[:3], six[3:]
    first_v, last_v = sum(volumes[:3]), sum(volumes[3:])
    up_v = sum(v for v, body in zip(volumes, bodies, strict=True) if body > 0)
    down_v = sum(v for v, body in zip(volumes, bodies, strict=True) if body < 0)
    first_vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in first) / first_v \
        if first_v else first[-1][4]
    last_vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in last) / last_v \
        if last_v else last[-1][4]
    result = {
        'cv_signed_volume': (up_v - down_v) / total if total else 0.0,
        'cv_up_down_volume': math.log((up_v + 1) / (down_v + 1)),
        'cv_abs_move_volume_corr': correlation([abs(x) for x in close_moves], volumes),
        'cv_signed_move_volume_corr': correlation(close_moves, volumes),
        'cv_range_volume_corr': correlation(ranges, volumes),
        'cv_volume_last_half': last_v / total if total else 0.5,
        'cv_volume_last_bar': volumes[-1] / total if total else 1 / 6,
        'cv_volume_acceleration': math.log((last_v + 1) / (first_v + 1)),
        'cv_volume_weighted_move': (
            sum(x * v for x, v in zip(close_moves, volumes, strict=True)) / total
            if total else 0.0),
        'cv_vwap_acceleration': last_vwap / first_vwap - 1,
        'cv_last_15m_return': last[-1][4] / first[-1][4] - 1,
        'cv_first_15m_return': first[-1][4] / opening - 1,
        'cv_late_breakout': last[-1][4] / max(b[2] for b in first) - 1,
        'cv_late_breakdown': last[-1][4] / min(b[3] for b in first) - 1,
        'cv_last_bar_body': bodies[-1],
        'cv_last_bar_range': ranges[-1],
    }
    for i, (move, volume) in enumerate(zip(close_moves, volumes, strict=True), 1):
        result[f'cv_move_{i}'] = move
        result[f'cv_volume_share_{i}'] = volume / total if total else 1 / 6
    if nifty is not None:
        index = {bar[0]: bar for bar in nifty}
        n_first, n_last = index.get(dt.time(9, 15)), index.get(dt.time(9, 40))
        if n_first and n_last and n_first[1] > 0:
            result['cv_relative_last15'] = result['cv_last_15m_return'] - (
                n_last[4] / index[dt.time(9, 25)][4] - 1
                if dt.time(9, 25) in index and index[dt.time(9, 25)][4] > 0 else 0)
    result['entry_0950'] = entry[1]
    result['exit_1500'] = exit_[1]
    result['target_0950_1500'] = exit_[1] / entry[1] - 1
    return result


def load_days(path: Path) -> dict[dt.date, list[tuple]]:
    days: dict[dt.date, list[tuple]] = {}
    for raw in json.loads(path.read_text()):
        when = dt.datetime.fromisoformat(raw[0]).astimezone(IST)
        days.setdefault(when.date(), []).append(
            (when.time(), *(float(value) for value in raw[1:6])))
    return days


def stock_rows(args: tuple[str, str, str]) -> list[dict]:
    directory, key, symbol = args
    path = Path(directory) / (key.replace('|', '_').replace(' ', '_') + '.json')
    if not path.exists():
        return []
    nifty = load_days(Path(directory) / 'NSE_INDEX_Nifty_50.json')
    rows = []
    for day, bars in load_days(path).items():
        found = opening_features(bars, nifty.get(day))
        if found is not None:
            rows.append({'symbol': symbol, 'day': day, **found})
    return rows


def build(directory: Path, base_path: Path, output: Path) -> None:
    universe = json.loads((directory / 'universe.json').read_text())
    args = [(str(directory), row['instrument_key'], row['trading_symbol'])
            for row in universe]
    with ProcessPoolExecutor(4) as pool:
        rows = [item for group in pool.map(stock_rows, args, chunksize=2) for item in group]
    extra = pl.DataFrame(rows, infer_schema_length=None)
    base = pl.read_parquet(base_path)
    joined = base.join(extra, on=['symbol', 'day'], how='inner', validate='1:1')
    output.parent.mkdir(parents=True, exist_ok=True)
    joined.write_parquet(output)
    print(f'{joined.height} complete stock-days, {joined["symbol"].n_unique()} stocks, '
          f'{joined["day"].n_unique()} dates; {len(extra.columns) - 2} new columns')


if __name__ == '__main__':
    build(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
