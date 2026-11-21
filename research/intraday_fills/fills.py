"""How often intraday orders fill, and what filled orders earn, under different entry
offsets, stops and order lifetimes. The first intraday-v4 call of each stock and day on
the 300-stock Upstox sample (1 Jul - 7 Oct 2026), as an order would allow.

Usage: uv run python -I fills.py DATA_DIR OUT.parquet; then summary.py OUT.parquet.
DATA_DIR holds one Upstox five-minute candle file per stock (a JSON list of Upstox's candle
rows), NSE_INDEX_Nifty_50.json and universe.json (instrument_key, trading_symbol,
tick_size in paise): the 300-stock sample behind docs/INTRADAY.md's backtest.

A limit fills when a candle that starts while it is open trades at or through it (at the
candle's open if it opens through). From the fill, the stop in the same or a later candle
comes first, as in igs.intraday.outcomes; a stop gapped through exits at the open. Neither
by 15:15 exits at the last close. "Market at approval" fills at the open of the first
candle the order is live in, keeping the stop distance.
"""
import datetime as dt
import json
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

import polars as pl

from igs.intraday.costs import charges, intraday_rates
from igs.intraday.engine import BAR, Candle, evaluate, to_tick
from igs.timeutil import IST

DATA = Path(sys.argv[1])
FIRST = dt.date(2026, 7, 1)
NOTIONAL = Decimal('100000')
RATES = intraday_rates()
EXIT = dt.time(15, 15)
# (name, entry offset %, stop: 'call' or % beyond entry, None = marketable at the open)
LEVELS = [('call price, call stop', Decimal(0), 'call'),
          ('call price, stop 1%', Decimal(0), Decimal(1)),
          ('0.25% better, stop 1%', Decimal('0.25'), Decimal(1)),
          ('0.5% better, stop 1%', Decimal('0.5'), Decimal(1)),
          ('1% better, stop 1% (now)', Decimal(1), Decimal(1)),
          ('market at approval, call stop', None, 'call'),
          ('market at approval, stop 1%', None, Decimal(1))]
LATENCY = (0, 1, 2)         # candles after the call's candle before the order is live
LIFE = ('call', 10, 30, 'day')   # until the call's expiry, N minutes after placing, 15:15


def load(path):
    days = defaultdict(list)
    for row in json.loads(path.read_text()):
        start = dt.datetime.fromisoformat(row[0])
        try:
            days[start.astimezone(IST).date()].append(Candle(start, *map(float, row[1:6])))
        except ValueError:
            continue
    return {d: sorted(v, key=lambda c: c.start) for d, v in days.items()}


def simulate(bars, i_live, until, action, entry, stop, target, market):
    """(outcome, entry fill, exit price) from the candle at i_live; None if unfilled."""
    d = 1 if action == 'buy' else -1
    day = bars[0].start.astimezone(IST).date()
    last_start = dt.datetime.combine(day, EXIT, IST) - BAR
    later = [c for c in bars[i_live:] if c.start <= last_start]
    if not later:
        return None
    if market:
        fill_i, price = 0, later[0].open
        risk = (entry - stop) if d == 1 else (stop - entry)      # keep the stop distance
        stop_px = price - d * risk
        if not (d * (target - price) > 0 and d * (price - stop_px) > 0):
            return ('skipped', None, None)
    else:
        fill_i = next((k for k, c in enumerate(later) if c.start < until
                       and (c.low <= entry if d == 1 else c.high >= entry)), None)
        if fill_i is None:
            return None
        price, stop_px = entry, stop
        # A candle that opens through the limit fills at its open, the better price.
        o = later[fill_i].open
        if (d == 1 and o < entry) or (d == -1 and o > entry):
            price = o
    for k in range(fill_i, len(later)):
        c = later[k]
        if (c.low <= stop_px) if d == 1 else (c.high >= stop_px):
            return ('stop', price, min(stop_px, c.open) if d == 1 else max(stop_px, c.open))
        if k != fill_i and ((c.high >= target) if d == 1 else (c.low <= target)):
            return ('target', price, target)
    return ('time', price, later[-1].close)


def net(action, entry, exit_price):
    entry, exit_price = Decimal(str(entry)), Decimal(str(exit_price))
    qty = int(NOTIONAL // entry)
    d = 1 if action == 'buy' else -1
    gross = d * (exit_price - entry) * qty
    cost = charges(qty, entry, exit_price, action, RATES)
    return float(gross), float(gross - cost)


def run_stock(args):
    key, symbol, tick = args
    path = DATA / (key.replace('|', '_').replace(' ', '_') + '.json')
    if not path.exists():
        return []
    days = load(path)
    nifty = load(DATA / 'NSE_INDEX_Nifty_50.json')
    dates = sorted(days)
    tick = Decimal(str(tick)) / 100
    out = []
    for n, day in enumerate(dates):
        if day < FIRST or day not in nifty:
            continue
        bars = days[day]
        if len(bars) < 70 or bars[0].start.astimezone(IST).time() != dt.time(9, 15):
            continue
        prev = [x for x in dates[:n] if x < day]
        if not prev:
            continue
        closing = [days[prev[-1]][-1]]
        slot = defaultdict(list)
        for x in prev:
            if day - dt.timedelta(days=30) <= x:
                for c in days[x]:
                    slot[c.start.astimezone(IST).time()].append(c)
        for i in range(5, len(bars)):
            last = bars[i]
            now = last.start + BAR + dt.timedelta(seconds=20)
            if now.astimezone(IST).time() >= EXIT:
                break
            res = evaluate(bars[:i + 1], slot[last.start.astimezone(IST).time()] + closing,
                           now, nifty[day][:i + 1], (), tick=tick)
            if res['action'] not in ('buy', 'sell'):
                continue
            action, d = res['action'], (1 if res['action'] == 'buy' else -1)
            ref = Decimal(str(res['reference']))
            call_stop, target = Decimal(str(res['stop'])), float(res['target'])
            expires = dt.datetime.fromisoformat(res['expires_at'])
            end = last.start + BAR
            for name, off, stop_rule in LEVELS:
                market = off is None
                entry = ref if market else to_tick(ref * (1 - d * off / 100), tick, d)
                stop = (call_stop if stop_rule == 'call'
                        else to_tick(entry * (1 - d * stop_rule / 100), tick, d))
                for lat in LATENCY:
                    placed = end + lat * BAR
                    for life in LIFE:
                        if market and life != 'call':
                            continue
                        if life == 'call':
                            if placed >= expires:
                                continue
                            until = expires
                        elif life == 'day':
                            until = dt.datetime.combine(day, EXIT, IST)
                        else:
                            until = placed + dt.timedelta(minutes=life)
                        got = simulate(bars, i + 1 + lat, until, action, float(entry),
                                       float(stop), target, market)
                        row = {'symbol': symbol, 'day': day, 'time': last.start.astimezone(
                            IST).time(), 'action': action, 'levels': name, 'latency': lat,
                            'life': str(life), 'outcome': 'not_filled', 'gross': None,
                            'net': None, 'risk_pct': float(abs(entry - stop) / entry * 100)}
                        if got and got[0] != 'skipped':
                            row['outcome'] = got[0]
                            row['gross'], row['net'] = net(action, got[1], got[2])
                        elif got:
                            row['outcome'] = 'skipped'
                        out.append(row)
            break                     # one order per stock per day
    return out


if __name__ == '__main__':
    universe = json.loads((DATA / 'universe.json').read_text())
    jobs = [(u['instrument_key'], u['trading_symbol'], u['tick_size']) for u in universe]
    rows = []
    with ProcessPoolExecutor(4) as pool:
        for part in pool.map(run_stock, jobs, chunksize=4):
            rows.extend(part)
    df = pl.DataFrame(rows, infer_schema_length=None)
    df.write_parquet(sys.argv[2])
    print(df.height, 'rows;', df.select('symbol', 'day').unique().height, 'calls')
