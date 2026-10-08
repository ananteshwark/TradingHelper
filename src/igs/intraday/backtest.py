"""Replay the intraday rules on Upstox's public historical five-minute candles.

`igs intraday-backtest` takes a seeded sample of Upstox MIS-eligible NSE equities, downloads
their five-minute candles and the Nifty 50's (cached under data/intraday/backtest/; the
endpoint needs no token), and runs engine.evaluate on every closed candle as the scanner
would: 20 seconds after it closes, with the prior 30 days' same-time candles, the previous
session's last candle and the Nifty up to that moment. The first call of each stock and day
is resolved on the rest of the session as the paper record does (outcomes.resolve), in R
and net of the charges on a Rs 1 lakh position. No news is replayed, so the news veto never
fires, and the trading-settings check is not applied. A measurement of the rules on past
data, not a forecast.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import time
from collections import defaultdict
from decimal import Decimal
from urllib.parse import quote

import httpx

from igs.intraday import eligibility
from igs.intraday.costs import charges, intraday_rates
from igs.intraday.engine import BAR, Candle, evaluate
from igs.intraday.outcomes import resolve
from igs.timeutil import IST

NIFTY = 'NSE_INDEX|Nifty 50'
WARMUP = dt.timedelta(days=45)       # the same-time volume baseline needs 30 days back
NOTIONAL = Decimal(100000)


def chunks(start, end):
    """(to, from) date pairs of at most one calendar month: Upstox's limit for 5-minute
    candles per request."""
    out, first = [], start
    while first <= end:
        nxt = (first.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        last = min(nxt - dt.timedelta(days=1), end)
        out.append((last, first))
        first = last + dt.timedelta(days=1)
    return out


def fetch(client, key, start, end, cache, *, pause=time.sleep):
    """Candles of `key` from `start` to `end`, from the cache file when it covers them."""
    path = cache / (key.replace('|', '_').replace(' ', '_') + '.json')
    if path.is_file():
        saved = json.loads(path.read_text())
        if saved['from'] <= start.isoformat() and saved['to'] >= end.isoformat():
            return saved['candles']
    rows = []
    for to, frm in chunks(start, end):
        response = client.get(f'/v3/historical-candle/{quote(key, safe="")}/minutes/5/'
                              f'{to}/{frm}')
        response.raise_for_status()
        rows += response.json()['data']['candles']
        pause(0.35)                  # one request at a time, about three a second
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'from': start.isoformat(), 'to': end.isoformat(),
                                'candles': rows}))
    return rows


def by_day(rows):
    days = defaultdict(list)
    for row in rows:
        start = dt.datetime.fromisoformat(row[0])
        try:
            candle = Candle(start, *map(float, row[1:6]))
        except ValueError:
            continue                 # e.g. a zero-priced closing-session print
        days[start.astimezone(IST).date()].append(candle)
    return {d: sorted(v, key=lambda c: c.start) for d, v in days.items()}


def replay(days, index, tick, first_day, rates):
    """The first call of each session from `first_day`, resolved: one dict per call."""
    dates = sorted(days)
    out = []
    for n, day in enumerate(dates):
        bars = days[day]
        if day < first_day or n == 0 or day not in index or len(bars) < 70:
            continue
        closing = days[dates[n - 1]][-1:]
        slot = defaultdict(list)
        for d in dates[:n]:
            if day - dt.timedelta(days=30) <= d:
                for c in days[d]:
                    slot[c.start.astimezone(IST).time()].append(c)
        for i in range(5, len(bars)):
            last = bars[i]
            now = last.start + BAR + dt.timedelta(seconds=20)
            if now.astimezone(IST).time() >= dt.time(15, 15):
                break
            result = evaluate(bars[:i + 1], slot[last.start.astimezone(IST).time()] + closing,
                              now, index[day][:i + 1], (), tick=tick)
            if result['action'] not in ('buy', 'sell'):
                continue
            got = resolve(result, bars, complete=True)
            row = {'day': day, 'time': last.start.astimezone(IST).time(),
                   'action': result['action'], 'rvol': result['rvol'],
                   'day_move_pct': result.get('day_move_pct'), 'outcome': got['outcome'],
                   'r': got.get('r_multiple'), 'r_net': None}
            if got.get('exit_price') is not None:
                entry, stop, exit_price = (Decimal(str(x)) for x in (
                    result['reference'], result['stop'], got['exit_price']))
                quantity = int(NOTIONAL // entry)
                direction = 1 if result['action'] == 'buy' else -1
                gross = direction * (exit_price - entry) * quantity
                cost = charges(quantity, entry, exit_price, result['action'], rates)
                row['r_net'] = float((gross - cost) / (abs(entry - stop) * quantity))
            out.append(row)
            break                    # one trade per stock per day, as orders allow
    return out


def summarise(rows):
    """By month and overall: calls filled, share stopped, reaching the target, winning
    after charges, and average and total R after charges."""
    groups = defaultdict(list)
    for row in rows:
        if row['outcome'] != 'not_filled' and row['r_net'] is not None:
            groups[row['day'].strftime('%Y-%m')].append(row)
            groups['All'].append(row)
    out = []
    for label in sorted(groups, key=lambda k: (k == 'All', k)):
        rs = groups[label]
        out.append({'Period': label, 'Calls': len(rs),
                    'Stop first': round(sum(r['outcome'] == 'stop' for r in rs) / len(rs), 2),
                    'Target first': round(sum(r['outcome'] == 'target' for r in rs) / len(rs),
                                          2),
                    'Win rate': round(sum(r['r_net'] > 0 for r in rs) / len(rs), 2),
                    'Average R net': round(sum(r['r_net'] for r in rs) / len(rs), 3),
                    'Total R net': round(sum(r['r_net'] for r in rs), 1)})
    return out


def run(*, stocks, start, end, seed, cache, client=None, ticks=None, pause=time.sleep):
    """Sample `stocks` MIS-eligible equities (seeded), replay `start`..`end`."""
    ticks = ticks if ticks is not None else eligibility.tick_sizes(force=True)
    keys = sorted(k for k in ticks if k.startswith('NSE_EQ|INE'))       # no ETFs
    sample = random.Random(seed).sample(keys, min(stocks, len(keys)))
    own = client is None
    client = client or httpx.Client(base_url='https://api.upstox.com', timeout=30,
                                    headers={'Accept': 'application/json'})
    rates = intraday_rates()
    rows = []
    try:
        index = by_day(fetch(client, NIFTY, start - WARMUP, end, cache, pause=pause))
        for key in sample:
            days = by_day(fetch(client, key, start - WARMUP, end, cache, pause=pause))
            rows += replay(days, index, ticks[key], start, rates)
    finally:
        if own:
            client.close()
    return rows
