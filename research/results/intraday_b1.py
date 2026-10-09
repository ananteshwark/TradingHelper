"""R1 (results/PROTOCOL.md): follow or fade the early move on the session after results.

Usage: uv run python -I intraday_b1.py EVENTS.parquet DATA_DIR OUT.parquet   (build trades)
       uv run python -I intraday_b1.py report OUT.parquet dev|holdout [RULE THRESHOLD]
"""
import datetime as dt
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

import polars as pl

T = dt.time
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SLIP, NOTIONAL, MIN_VALUE = 0.0002, 100000, 50e7


def sessions(path):
    days = {}
    for r in json.loads(path.read_text()):
        if min(r[1:5]) <= 0:
            continue
        t = dt.datetime.fromisoformat(r[0]).astimezone(IST)
        days.setdefault(t.date(), []).append((t.time(), *map(float, r[1:6])))
    out = {}
    for d, b in days.items():
        b.sort()
        if b[0][0] == T(9, 15) and len(b) >= 70:
            out[d] = b
    return out


def stock(args):
    sym, key, data, meetings = args
    days = sessions(Path(data) / (key.replace('|', '_') + '.json'))
    dates = sorted(days)
    b1 = set()
    for m in meetings:                        # the first session after each meeting day
        nxt = next((d for d in dates if d > m), None)
        if nxt is not None and (nxt - m).days <= 7:
            b1.add(nxt)
    out = []
    for n, d in enumerate(dates):
        if n < 21:
            continue
        prev = [days[x] for x in dates[n - 20:n]]
        value20 = sum(sum(b[4] * b[5] for b in s) for s in prev) / 20
        if value20 < MIN_VALUE:
            continue
        bars = {b[0]: b for b in days[d]}
        if T(9, 40) not in bars or T(9, 50) not in bars or T(15, 15) not in bars:
            continue
        prev_close = days[dates[n - 1]][-1][4]
        move = bars[T(9, 40)][4] / prev_close - 1
        out.append({'symbol': sym, 'day': d, 'b1': d in b1, 'move': move,
                    'entry': bars[T(9, 50)][1], 'exit': bars[T(15, 15)][1]})
    return out


def build(events_path, data, out_path):
    ev = pl.read_parquet(events_path)
    uni = json.loads((Path(data) / 'universe.json').read_text())
    by = {s: sorted(g['meeting'].to_list()) for (s,), g in ev.group_by(['symbol'])}
    jobs = [(u['trading_symbol'], u['instrument_key'], data, by.get(u['trading_symbol'], []))
            for u in uni]
    rows = []
    with ProcessPoolExecutor(4) as pool:
        for part in pool.map(stock, jobs, chunksize=4):
            rows.extend(part)
    df = pl.DataFrame(rows)
    df.write_parquet(out_path)
    print(df.height, 'stock-days;', df['b1'].sum(), 'sessions after results;',
          df.filter('b1')['symbol'].n_unique(), 'stocks with results')


def report(path, period, only=None):
    sys.path.insert(0, '/home/user/TradingHelper/src')
    from igs.intraday.costs import charges, intraday_rates
    rates = intraday_rates()
    lo, hi = (dt.date(2022, 1, 1), dt.date(2024, 12, 31)) if period == 'dev' else \
        (dt.date(2025, 1, 1), dt.date(2026, 12, 31))
    df = pl.read_parquet(path).filter(pl.col('day').is_between(lo, hi))

    def run(sel, rule):
        out = []
        for move, entry, exit_ in sel.select('move', 'entry', 'exit').iter_rows():
            side = (1 if move > 0 else -1) * (1 if rule == 'follow' else -1)
            e, x = entry * (1 + side * SLIP), exit_ * (1 - side * SLIP)
            qty = int(NOTIONAL // e)
            if qty < 1:
                continue
            cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                                 'buy' if side == 1 else 'sell', rates))
            pnl = side * (x - e) * qty - cost
            out.append(pnl / (e * qty) * 100)
        s = pl.Series(out)
        gains, losses = s.filter(s > 0).sum(), -s.filter(s < 0).sum()
        return {'trades': len(s), 'net_%': round(s.mean(), 3),
                't': round(s.mean() / s.std() * math.sqrt(len(s)), 2) if len(s) > 1 else None,
                'PF': round(gains / losses, 2) if losses else None, 'win': round((s > 0).mean(), 3)}

    rows = []
    combos = [only] if only else [(r, th) for r in ('follow', 'fade') for th in (0.01, 0.02)]
    for rule, th in combos:
        big = df.filter(pl.col('move').abs() >= th)
        rows.append({'rule': f'{rule} >= {th*100:.0f}%', 'days': 'after results',
                     **run(big.filter('b1'), rule)})
        rows.append({'rule': f'{rule} >= {th*100:.0f}%', 'days': 'other days (control)',
                     **run(big.filter(~pl.col('b1')), rule)})
    with pl.Config(tbl_rows=20, tbl_width_chars=200):
        print(pl.DataFrame(rows))


if __name__ == '__main__':
    if sys.argv[1] == 'report':
        report(sys.argv[2], sys.argv[3], (sys.argv[4], float(sys.argv[5])) if len(sys.argv) > 5 else None)
    else:
        build(sys.argv[1], sys.argv[2], sys.argv[3])
