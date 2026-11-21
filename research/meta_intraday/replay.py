"""The intraday-v4 calls of research/meta_intraday/PROTOCOL.md, with their inputs and results.

Usage: uv run python -I replay.py CANDLES OUT_PARQUET
CANDLES holds research/ml's Upstox 5-minute candles: NSE_EQ_<isin>.json per stock and
NSE_INDEX_Nifty_50.json, each a list of [time, open, high, low, close, volume, oi].
The calls are made and resolved as igs.intraday.backtest.replay does.
"""
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from decimal import Decimal
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, '/home/user/TradingHelper/src')
import polars as pl  # noqa: E402

from igs.intraday.backtest import by_day  # noqa: E402
from igs.intraday.costs import charges, intraday_rates  # noqa: E402
from igs.intraday.engine import BAR, evaluate  # noqa: E402
from igs.intraday.outcomes import resolve  # noqa: E402
from igs.timeutil import IST  # noqa: E402

SRC, OUT = Path(sys.argv[1]), Path(sys.argv[2])
TICK = Decimal('0.05')
NOTIONAL = Decimal(100000)
RATES = intraday_rates()
INDEX = by_day(json.loads((SRC / 'NSE_INDEX_Nifty_50.json').read_text()))
IDX_DATES = sorted(INDEX)
IDX_CLOSE = {d: INDEX[d][-1].close for d in IDX_DATES}


def inputs(result, bars, i, day, dates, n, days):
    """The protocol's inputs, signed in the trade's direction."""
    s = 1 if result['action'] == 'buy' else -1
    last, today = bars[i], bars[:i + 1]
    atr, close, prev = result['atr'], last.close, result['prev_close']
    hi, lo = max(b.high for b in today), min(b.low for b in today)
    rng = last.high - last.low
    loc = (close - last.low) / rng if rng > 0 else 0.5
    edge = result['opening_high'] if s == 1 else result['opening_low']
    closes = [days[d][-1].close for d in dates[max(0, n - 6):n]]
    k = IDX_DATES.index(day)
    nifty_yday = (IDX_CLOSE[IDX_DATES[k - 1]] / IDX_CLOSE[IDX_DATES[k - 2]] - 1) * 100 \
        if k >= 2 else None
    return {
        'side': 1 if s == 1 else 0,
        'minutes': (last.start.astimezone(IST) - dt.datetime.combine(
            day, dt.time(9, 15), IST)).total_seconds() / 60,
        'volume_jump': math.log(result['rvol']),
        'momentum': s * result['momentum_pct'],
        'from_vwap': s * (close - result['vwap']) / atr if atr else None,
        'day_move': s * result['day_move_pct'],
        'gap': s * (today[0].open / prev - 1) * 100,
        'stop_width': abs(close - float(result['stop'])) / close * 100,
        'market': s * result['market_return_pct'],
        'against_market': s * (result['day_move_pct'] - result['market_return_pct']),
        'close_in_candle': loc if s == 1 else 1 - loc,
        'candle_size': rng / atr if atr else None,
        'past_opening_range': s * (close - edge) / atr if atr else None,
        'day_range': (hi - lo) / prev * 100,
        'room_to_extreme': ((hi - close) if s == 1 else (close - lo)) / atr if atr else None,
        'turnover': math.log(max(result['turnover_cr'], 1e-6) * 1e7),
        'price': math.log(close),
        'yesterday': s * (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else None,
        'five_days': s * (closes[-1] / closes[0] - 1) * 100 if len(closes) >= 6 else None,
        'market_yesterday': s * nifty_yday if nifty_yday is not None else None,
        'weekday': day.weekday(),
    }


def replay(path):
    days = by_day(json.loads(path.read_text()))
    dates = sorted(days)
    out = []
    for n, day in enumerate(dates):
        bars = days[day]
        if n == 0 or day not in INDEX or len(bars) < 70:
            continue
        closing = days[dates[n - 1]][-1:]
        slot = defaultdict(list)
        for d in dates[max(0, n - 25):n]:
            if day - dt.timedelta(days=30) <= d:
                for c in days[d]:
                    slot[c.start.astimezone(IST).time()].append(c)
        for i in range(5, len(bars)):
            last = bars[i]
            now = last.start + BAR + dt.timedelta(seconds=20)
            if now.astimezone(IST).time() >= dt.time(15, 15):
                break
            result = evaluate(bars[:i + 1], slot[last.start.astimezone(IST).time()] + closing,
                              now, INDEX[day][:i + 1], (), tick=TICK)
            if result['action'] not in ('buy', 'sell'):
                continue
            got = resolve(result, bars, complete=True)
            row = {'stock': path.stem.removeprefix('NSE_EQ_'), 'day': day,
                   'action': result['action'], 'outcome': got['outcome'],
                   'r': got.get('r_multiple'), 'r_net': None, 'net_rupees': None,
                   **inputs(result, bars, i, day, dates, n, days)}
            quantity = int(NOTIONAL // Decimal(str(result['reference'])))
            # A share above the position size (Rs 1 lakh) can't be traded at it.
            if got.get('exit_price') is not None and quantity > 0:
                entry, stop, exit_price = (Decimal(str(x)) for x in (
                    result['reference'], result['stop'], got['exit_price']))
                direction = 1 if result['action'] == 'buy' else -1
                gross = direction * (exit_price - entry) * quantity
                cost = charges(quantity, entry, exit_price, result['action'], RATES)
                row['net_rupees'] = float(gross - cost)
                row['r_net'] = float((gross - cost) / (abs(entry - stop) * quantity))
            out.append(row)
            break                    # one trade per stock per day, as orders allow
    return out


if __name__ == '__main__':
    files = sorted(SRC.glob('NSE_EQ_*.json'))
    rows = []
    with Pool(4) as pool:
        for k, part in enumerate(pool.imap_unordered(replay, files)):
            rows += part
            if k % 20 == 0:
                print(k, len(rows), flush=True)
    pl.DataFrame(rows, infer_schema_length=None).write_parquet(OUT)
    print('calls', len(rows), 'days', len({r['day'] for r in rows}), flush=True)
