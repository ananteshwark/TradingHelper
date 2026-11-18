"""Add causal candle, prior-session and Nifty context to original spike events.

Usage: python enrich.py DAYS_DIR EVENTS_DIR NIFTY_JSON OUT.parquet
The pure decision_features() function ignores all stock and market bars after i.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl
from build_days import read_sessions

ROOT = Path(__file__).resolve().parent


def ratio(a: float, b: float) -> float | None:
    return a / b - 1 if b > 0 else None


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def decision_features(bars: list[tuple], slot: list[float], i: int,
                      prior: dict, market: list[tuple] | None,
                      market_prior: dict | None) -> dict[str, float | None]:
    """Feature values available at the close of stock candle i, never later."""
    observed = np.asarray(bars[:i + 1], dtype=float)
    t, o, h, lo, c, v = (observed[:, k] for k in range(6))
    current = {}
    for lag in range(6):
        j = i - lag
        name = f'r_lag{lag}'
        if j < 0:
            current[name + '_return'] = None
            current[name + '_range'] = None
            current[name + '_body'] = None
            current[name + '_slot_volume'] = None
            current[name + '_upper_wick'] = None
            current[name + '_lower_wick'] = None
            current[name + '_close_position'] = None
            continue
        current[name + '_return'] = ratio(c[j], c[j - 1]) if j > 0 else None
        current[name + '_range'] = ratio(h[j], lo[j])
        current[name + '_body'] = ratio(c[j], o[j])
        span = h[j] - lo[j]
        current[name + '_upper_wick'] = ((h[j] - max(o[j], c[j])) / span
                                          if span > 0 else None)
        current[name + '_lower_wick'] = ((min(o[j], c[j]) - lo[j]) / span
                                          if span > 0 else None)
        current[name + '_close_position'] = ((c[j] - lo[j]) / span
                                             if span > 0 else None)
        current[name + '_slot_volume'] = (v[j] / slot[j]
                                           if j < len(slot) and slot[j] > 0 else None)
    recent = slice(max(0, i - 5), i + 1)
    rr = np.diff(c, prepend=o[0]) / np.concatenate(([o[0]], c[:-1]))
    up = float(v[recent][rr[recent] > 0].sum())
    down = float(v[recent][rr[recent] < 0].sum())
    six_v = float(v[recent].sum())
    typical = (h + lo + c) / 3
    vwap = float((typical * v).sum() / v.sum()) if v.sum() else float(c[-1])
    current.update({
        'r_signed_volume': ((up - down) / six_v if six_v else 0.0),
        'r_up_down_volume': math.log((up + 1) / (down + 1)),
        'r_move_volume_corr': correlation(rr[recent], v[recent]),
        'r_abs_move_volume_corr': correlation(abs(rr[recent]), v[recent]),
        'r_volume_concentration': float(v[-1] / six_v) if six_v else None,
        'r_volume_acceleration': (math.log((v[-3:].sum() + 1) /
                                           (v[-6:-3].sum() + 1)) if i >= 5 else None),
        'r_vwap_distance': ratio(c[-1], vwap),
        'r_ret3': ratio(c[-1], c[-4]) if i >= 3 else None,
        'r_ret6': ratio(c[-1], c[-7]) if i >= 6 else None,
        'r_realised_vol': (float(np.std(rr[1:], ddof=1)) if i >= 2 else None),
        'r_range_atr': ((float(h.max() - lo.min()) /
                         (prior['d_atr14_pct'] * prior['prevclose']))
                        if prior['d_atr14_pct'] and prior['prevclose'] else None),
        'r_current_slot_volume': (v[-1] / slot[i]
                                  if i < len(slot) and slot[i] > 0 else None),
        'r_prev_ret_known': float(prior['d_prev_ret'] is not None),
    })
    for key in ('d_prev_ret', 'd_prev_gap', 'd_prev_range', 'd_prev_clv',
                'd_prev_last_hour', 'd_prev_last30_vol_ratio', 'd_ret5',
                'd_ret20', 'd_atr14_pct', 'd_vol20'):
        current['r_' + key] = prior.get(key)
    for horizon in (5, 15, 30, 60):
        current[f'r_nifty_ret{horizon}'] = None
        current[f'r_stock_excess{horizon}'] = None
    current['r_nifty_prev_ret'] = (market_prior or {}).get('prev_ret')
    current['r_nifty_ret5d'] = (market_prior or {}).get('ret5')
    current['r_nifty_gap'] = (market_prior or {}).get('gap')
    if market:
        past = [bar for bar in market if bar[0] <= t[-1]]
        if past:
            mc = np.asarray([bar[4] for bar in past], dtype=float)
            for horizon, lag in ((5, 1), (15, 3), (30, 6), (60, 12)):
                if len(mc) > lag and len(c) > lag:
                    mr = ratio(mc[-1], mc[-lag - 1])
                    sr = ratio(c[-1], c[-lag - 1])
                    current[f'r_nifty_ret{horizon}'] = mr
                    current[f'r_stock_excess{horizon}'] = (sr - mr
                                                            if sr is not None and mr is not None
                                                            else None)
    return current


def market_history(path: Path) -> tuple[dict, dict]:
    sessions, _, _ = read_sessions(path)
    by_day = {r['day']: r['bars'] for r in sessions}
    metadata = {}
    for index, row in enumerate(sessions):
        previous = sessions[index - 1] if index >= 1 else None
        metadata[row['day']] = {
            'prev_ret': (previous['close'] / sessions[index - 2]['close'] - 1
                         if index >= 2 else None),
            'ret5': (previous['close'] / sessions[index - 6]['close'] - 1
                     if index >= 6 else None),
            'gap': (row['open'] / previous['close'] - 1 if previous else None),
        }
    return by_day, metadata


def enrich(days_dir: Path, events_dir: Path, nifty_path: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(output)
    days = pl.read_parquet(days_dir / 'days.parquet')
    events = pl.read_parquet(events_dir / 'events.parquet')
    contexts = {(r['isin'], r['day']): r for r in
                pl.read_parquet(days_dir / 'context.parquet').iter_rows(named=True)}
    market, market_meta = market_history(nifty_path)
    by_event = {(isin, day): group for (isin, day), group in
                events.group_by('isin', 'day', maintain_order=True)}
    additions = []
    for (isin, day), group in days.group_by('isin', 'day', maintain_order=True):
        event = by_event.get((isin, day))
        if event is None:
            continue
        bars = list(group.select('minute', 'open', 'high', 'low', 'close',
                                 'volume').iter_rows())
        slot = group['slot_med20'].to_list()
        prior = {**contexts[(isin, day)], 'prevclose': group['prevclose'][0]}
        for i in event['i'].unique().to_list():
            additions.append({'isin': isin, 'day': day, 'i': i,
                              **decision_features(bars, slot, i, prior,
                                                  market.get(day), market_meta.get(day))})
    extra = pl.DataFrame(additions, infer_schema_length=None)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = events.join(extra, on=['isin', 'day', 'i'], how='inner', validate='m:1')
    if result.height != events.height:
        raise ValueError('Every original event must receive detailed context')
    result.sort('day', 'minute', 'isin', 'x').write_parquet(output)
    manifest = {
        'protocol_sha256': hashlib.sha256((ROOT / 'PROTOCOL.md').read_bytes()).hexdigest(),
        'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'events_sha256': hashlib.sha256((events_dir / 'events.parquet').read_bytes()).hexdigest(),
        'days_sha256': hashlib.sha256((days_dir / 'days.parquet').read_bytes()).hexdigest(),
        'rows': result.height,
        'features_baseline': sum(c.startswith('f_') for c in result.columns),
        'features_added': sum(c.startswith('r_') for c in result.columns),
    }
    output.with_suffix('.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print({key: manifest[key] for key in ('rows', 'features_baseline', 'features_added')},
          flush=True)
    return manifest


if __name__ == '__main__':
    enrich(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
