"""OrderBlock's board and 5-minute order blocks (PROTOCOL.md, tests A and B).

Usage: uv run python -I intraday.py CANDLES BHAV FUTURES UNIVERSE PERIOD OUT_PARQUET
PERIOD is dev (March 2022 - 2024) or holdout (2025 - 8 October 2026).
CANDLES is research/exits' table (symbol, day, k, o, h, l, c, v); BHAV and FUTURES are
the parsed NSE bhavcopies; UNIVERSE is research/ml/universe_nifty200.json.
"""
import datetime as dt
import json
import math
import sys
from collections import defaultdict

import numpy as np
import polars as pl

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.intraday.costs import charges, intraday_rates  # noqa: E402

candles_path, bhav_path, fut_path, uni_path, period, out = sys.argv[1:7]
FIRST, LAST = {'dev': (dt.date(2022, 3, 1), dt.date(2024, 12, 31)),
               'holdout': (dt.date(2025, 1, 1), dt.date(2026, 10, 8))}[period]
DECIDE, ENTER, EXIT, LAST_ENTRY = 6, 7, 72, 68
SLIP, THROUGH = 0.0002, 0.0002
RATES = intraday_rates()

universe = json.loads(open(uni_path).read())
by_symbol = {u['trading_symbol']: u for u in universe}
isins = {u['isin']: u['trading_symbol'] for u in universe}

# --- The exchange's daily rows for each stock: previous close (adjusted for corporate
# actions on the ex-date), sigma50, the previous session's high and low.
bhav = (pl.scan_parquet(bhav_path)
        .filter(pl.col('day') >= dt.date(2021, 6, 1), pl.col('series') == 'EQ')
        .filter(pl.col('isin').is_in(list(isins)) | pl.col('symbol').is_in(list(by_symbol)))
        .collect())
bhav = bhav.with_columns(
    pl.when(pl.col('isin').is_in(list(isins)))
    .then(pl.col('isin').replace_strict(isins, default=None))
    .otherwise(pl.col('symbol')).alias('stock'),
    pl.col('isin').is_in(list(isins)).alias('by_isin'))
bhav = (bhav.sort('stock', 'day', 'by_isin', descending=[False, False, True])
        .unique(['stock', 'day'], keep='first', maintain_order=True).sort('stock', 'day'))
bhav = bhav.with_columns(
    (pl.col('close') / pl.col('prevclose') - 1).abs().alias('absret'),
    (pl.col('prevclose') / pl.col('close').shift(1).over('stock')).alias('adj'))
bhav = bhav.with_columns(
    pl.col('absret').shift(1).rolling_mean(50).over('stock').alias('sigma50'),
    pl.col('absret').shift(1).rolling_mean(50).over('stock').is_not_null().alias('has50'),
    (pl.col('high').shift(1).over('stock') * pl.col('adj')).alias('prev_high'),
    (pl.col('low').shift(1).over('stock') * pl.col('adj')).alias('prev_low'))
daily = {(r['stock'], r['day']): r for r in bhav.filter(
    pl.col('day').is_between(FIRST, LAST)).iter_rows(named=True)}

fno = (pl.scan_parquet(fut_path).filter(pl.col('kind') == 'STF',
                                        pl.col('day').is_between(FIRST, LAST))
       .select('day', 'symbol').unique().collect())
fno = set(fno.iter_rows())

# --- 5-minute candles as dense arrays, one row a stock-day (today's and the previous
# session's, for its average candle range).
cand = (pl.scan_parquet(candles_path)
        .filter(pl.col('symbol') != 'NIFTY',
                pl.col('day').is_between(FIRST - dt.timedelta(days=10), LAST))
        .collect())
keys = cand.select('symbol', 'day').unique().sort('symbol', 'day').with_row_index('sd')
cand = cand.join(keys, on=['symbol', 'day'])
n = keys.height
arr = {}
for col in 'ohlcv':
    a = np.full((n, 75), np.nan)
    a[cand['sd'].to_numpy(), cand['k'].to_numpy()] = cand[col].to_numpy()
    arr[col] = a
O, H, L, C, V = (arr[k] for k in 'ohlcv')
sd_of = {(s, d): i for i, s, d in keys.select('sd', 'symbol', 'day').iter_rows()}
prev_sd = {}
for sym, grp in keys.group_by('symbol'):
    rows = grp.sort('day')
    for a, b in zip(rows['sd'].to_list()[:-1], rows['sd'].to_list()[1:], strict=True):
        prev_sd[b] = a
market_days = sorted(d for d in keys['day'].unique().to_list() if FIRST <= d <= LAST)


def cost(qty, entry, exit_price, side, slip_entry, slip_exit):
    action = 'buy' if side == 1 else 'sell'
    gross = side * (exit_price - entry) * qty
    slip = SLIP * qty * (entry * slip_entry + exit_price * slip_exit)
    return gross, gross - slip - float(charges(qty, entry, exit_price, action, RATES))


# --- The board at 09:50, each day.
boards = []
for day in market_days:
    rows = []
    for sym, u in by_symbol.items():
        i = sd_of.get((sym, day))
        b = daily.get((sym, day))
        if i is None or b is None or not b['has50'] or (day, b['symbol']) not in fno:
            continue
        o, c = O[i, :DECIDE + 1], C[i, :DECIDE + 1]
        if np.isnan(o[0]) or np.isnan(c).any() or not b['prevclose']:
            continue
        move = c[-1] / b['prevclose'] - 1
        path = np.abs(np.diff(np.concatenate([[o[0]], c]))).sum()
        er = abs(c[-1] - o[0]) / path if path > 0 else 0.0
        strength = move / b['sigma50'] if b['sigma50'] else 0.0
        choppy = er < 0.3 or np.sign(c[-1] - o[0]) != np.sign(move)
        rows.append({'day': day, 'symbol': sym, 'sector': u['sector'], 'sd': i,
                     'move': move, 'er': er, 'strength': strength,
                     'score': 0.0 if choppy else strength * er,
                     'event': move <= -3 * b['sigma50'],
                     'prev_high': b['prev_high'], 'prev_low': b['prev_low']})
    if not rows:
        continue
    up = sum(r['move'] > 0 for r in rows)
    down = sum(r['move'] < 0 for r in rows)
    dominant = 1 if up >= 0.6 * (up + down) else -1 if down >= 0.6 * (up + down) else 0
    for side in (1, -1):
        ranked = sorted((r for r in rows if side * r['score'] > 0 and not r['event']),
                        key=lambda r: -side * r['score'])
        picked, sectors = [], defaultdict(int)
        for r in ranked:
            if sectors[r['sector']] < 2:
                picked.append(r)
                sectors[r['sector']] += 1
            if len(picked) == 3:
                break
        for rank, r in enumerate(picked, 1):
            boards.append(r | {'side': side, 'rank': rank, 'dominant': dominant,
                               'breadth_up': up, 'breadth_down': down})
    # The day's F&O stocks' returns 09:50 open -> 15:15 open, for the hit rate.
    for r in rows:
        i = r['sd']
        if not (np.isnan(O[i, ENTER]) or np.isnan(O[i, EXIT])):
            boards.append({'day': day, 'symbol': r['symbol'], 'side': 0, 'sd': i,
                           'universe_ret': O[i, EXIT] / O[i, ENTER] - 1})


# --- Test B: the first return to the order block, one pick at a time. A bearish setup is
# run as a bullish one on negated prices (x = -price): its last green candle becomes the
# last "red" one, its swing lows swing highs, and every rule reads the same.
def swing_levels(x, upto):
    """Swing highs of x (a high above the 2 candles each side) known before candle
    `upto`: their 2 later candles have closed."""
    return [x[m] for m in range(2, upto - 2)
            if not np.isnan(x[m - 2:m + 3]).any()
            and x[m] > max(x[m - 2], x[m - 1], x[m + 1], x[m + 2])]


def zone_trade(i, side, prev_high, prev_low):
    p = prev_sd.get(i)
    if p is None:
        return None
    avg_range = np.nanmean(H[p] - L[p])
    if side == 1:
        xo, xh, xl, xc = O[i], H[i], L[i], C[i]
        prev_level = prev_high
    else:
        xo, xh, xl, xc = -O[i], -L[i], -H[i], -C[i]
        prev_level = -prev_low if prev_low is not None else None
    v = V[i]
    typical = (H[i] + L[i] + C[i]) / 3
    block, trade = None, None
    for j in range(3, 75):
        if np.isnan(xc[j]):
            continue
        # 1. The first return to the current block, from 09:50 to 14:55.
        if (block is not None and not block['used'] and ENTER <= j <= LAST_ENTRY
                and j > block['j'] and xl[j] <= block['z']):
            block['used'] = True
            z, s1 = block['z'], block['s1']
            if xo[j] < s1:
                block = None
            else:
                vol = np.nansum(v[:j])
                vwap = np.nansum(typical[:j] * v[:j]) / vol if vol > 0 else np.nan
                x_vwap = vwap if side == 1 else -vwap
                r1 = np.nanmax(xh[block['b']:j])
                above = [lvl for lvl in swing_levels(xh, j) if lvl > r1]
                if prev_level is not None and not math.isnan(prev_level) and prev_level > r1:
                    above.append(prev_level)
                r2 = min(above) if above else r1
                rr = (r2 - z) / (z - s1) if z > s1 else 0.0
                if z >= x_vwap and rr >= 1.5 and xl[j] <= z - abs(z) * THROUGH:
                    trade = {'entry_k': j, 'z': z, 's1': s1, 'r2': r2, 'rr': rr,
                             'entry': min(z, xo[j])}
                    break
        # 2. A close beyond the far edge breaks the block.
        if block is not None and xc[j] < block['s1']:
            block = None
        # 3. A close above the latest swing high (or the first 15 minutes' high) with
        # force makes a new block.
        known = swing_levels(xh, j)
        level = known[-1] if known else np.nanmax(xh[0:3])
        if xc[j] > level and (np.isnan(xc[j - 1]) or xc[j - 1] <= level):
            reds = [b for b in range(max(0, j - 12), j) if xc[b] < xo[b]]
            if reds:
                b = reds[-1]
                gap = any(xl[m] > xh[m - 2] for m in range(b + 2, j + 1))
                if gap or xc[j] - xh[b] >= avg_range:
                    block = {'b': b, 'j': j, 'z': xh[b], 's1': xl[b], 'used': False}
    if trade is None:
        return None
    # Exit: the target; a close beyond S1, out at the next open; else the 15:15 open.
    z, s1, r2, entry = trade['z'], trade['s1'], trade['r2'], trade['entry']
    exit_price, kind = None, 'time'
    for m in range(trade['entry_k'], EXIT):
        if m > trade['entry_k'] and not np.isnan(xh[m]) and xh[m] >= r2:
            exit_price, kind = (r2 if np.isnan(xo[m]) else max(r2, xo[m])), 'target'
            break
        if not np.isnan(xc[m]) and xc[m] < s1:
            nxt = next((xo[q] for q in range(m + 1, 75) if not np.isnan(xo[q])), None)
            if nxt is not None:
                exit_price, kind = nxt, 'stop'
            break
    if exit_price is None:
        if np.isnan(xo[EXIT]):
            return None
        exit_price, kind = xo[EXIT], 'time'
    real = 1 if side == 1 else -1
    return {'entry_k': trade['entry_k'], 'entry': real * entry, 'exit': real * exit_price,
            'z': real * z, 's1': real * s1, 'r2': real * r2, 'rr': trade['rr'],
            'exit_kind': kind}


out_rows = []
for r in boards:
    if r['side'] == 0:
        out_rows.append({'day': r['day'], 'symbol': r['symbol'], 'test': 'U',
                         'universe_ret': r['universe_ret']})
        continue
    i, side = r['sd'], r['side']
    base = {'day': r['day'], 'symbol': r['symbol'], 'side': side, 'rank': r['rank'],
            'dominant': r['dominant'], 'score': r['score'], 'move': r['move'],
            'er': r['er']}
    if not (np.isnan(O[i, ENTER]) or np.isnan(O[i, EXIT])):
        entry, exit_price = O[i, ENTER], O[i, EXIT]
        qty = math.floor(1e5 / entry)
        gross, net = cost(qty, entry, exit_price, side, 1, 1)
        out_rows.append(base | {'test': 'A', 'entry': entry, 'exit': exit_price, 'qty': qty,
                                'gross': gross, 'net': net,
                                'ret': side * (exit_price / entry - 1)})
    t = zone_trade(i, side, r['prev_high'], r['prev_low'])
    if t:
        qty = math.floor(1e5 / t['entry'])
        slip_exit = 0 if t['exit_kind'] == 'target' else 1
        gross, net = cost(qty, t['entry'], t['exit'], side, 0, slip_exit)
        chase = (side * (O[i, EXIT] / O[i, ENTER] - 1)
                 if not (np.isnan(O[i, ENTER]) or np.isnan(O[i, EXIT])) else None)
        out_rows.append(base | {'test': 'B', 'qty': qty, 'gross': gross, 'net': net,
                                'ret': side * (t['exit'] / t['entry'] - 1),
                                'chase_ret': chase} | t)

res = pl.DataFrame(out_rows, infer_schema_length=None)
res.write_parquet(out)
print(period, 'board days', len({r['day'] for r in boards}), 'rows', res.height)
