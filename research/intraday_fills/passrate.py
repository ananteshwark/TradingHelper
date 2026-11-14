"""Share of first intraday-v4 calls (300 stocks, Jul-Oct 2026) whose order passes the
trading settings' reward-to-risk check, by order levels and amount per trade.
Usage: uv run python -I passrate.py DATA_DIR"""
import datetime as dt
import json
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

from igs.intraday.costs import intraday_rates
from igs.intraday.engine import BAR, Candle, evaluate
from igs.intraday.trading import order_check
from igs.timeutil import IST

DATA = Path(sys.argv[1])
RATES = intraday_rates()
LEVELS = [(1, 1), (Decimal('0.5'), 1), (Decimal('0.25'), 1), (0, 1), (0, 0)]
SIZES = [(10000, 100), (100000, 1000)]


def load(path):
    days = defaultdict(list)
    for row in json.loads(path.read_text()):
        start = dt.datetime.fromisoformat(row[0])
        try:
            days[start.astimezone(IST).date()].append(Candle(start, *map(float, row[1:6])))
        except ValueError:
            continue
    return {d: sorted(v, key=lambda c: c.start) for d, v in days.items()}


def run_stock(args):
    key, symbol, tick = args
    path = DATA / (key.replace('|', '_').replace(' ', '_') + '.json')
    if not path.exists():
        return []
    days, nifty = load(path), load(DATA / 'NSE_INDEX_Nifty_50.json')
    dates, tick, out = sorted(days), Decimal(str(tick)) / 100, []
    for n, day in enumerate(dates):
        bars = days[day]
        if (day < dt.date(2026, 7, 1) or day not in nifty or len(bars) < 70
                or bars[0].start.astimezone(IST).time() != dt.time(9, 15) or n == 0):
            continue
        closing, slot = [days[dates[n - 1]][-1]], defaultdict(list)
        for x in dates[:n]:
            if day - dt.timedelta(days=30) <= x:
                for c in days[x]:
                    slot[c.start.astimezone(IST).time()].append(c)
        for i in range(5, len(bars)):
            last = bars[i]
            now = last.start + BAR + dt.timedelta(seconds=20)
            if now.astimezone(IST).time() >= dt.time(15, 15):
                break
            res = evaluate(bars[:i + 1], slot[last.start.astimezone(IST).time()] + closing,
                           now, nifty[day][:i + 1], (), tick=tick)
            if res['action'] not in ('buy', 'sell'):
                continue
            row = {}
            for off, stop in LEVELS:
                for amount, loss in SIZES:
                    cfg = {'enabled': True, 'auto_high_volume_enabled': False,
                           'max_trade_rupees': Decimal(amount), 'max_risk_rupees': Decimal(loss),
                           'min_net_reward_risk': Decimal('1.5'),
                           'auto_min_net_reward_risk': Decimal('2.5'),
                           'max_price_deviation_pct': Decimal('0.5'),
                           'entry_offset_pct': Decimal(off), 'stop_pct': Decimal(stop)}
                    row[(str(off), str(stop), amount)] = order_check(res, key, tick, cfg,
                                                                     RATES)['ok']
                    cfg['min_net_reward_risk'] = Decimal('2.5')
                    row[(str(off), str(stop), amount, 'auto')] = order_check(
                        res, key, tick, cfg, RATES)['ok']
            out.append(row)
            break
    return out


if __name__ == '__main__':
    universe = json.loads((DATA / 'universe.json').read_text())
    jobs = [(u['instrument_key'], u['trading_symbol'], u['tick_size']) for u in universe]
    rows = []
    with ProcessPoolExecutor(4) as pool:
        for part in pool.map(run_stock, jobs, chunksize=4):
            rows.extend(part)
    print(len(rows), 'calls')
    for k in rows[0]:
        print(k, round(sum(r[k] for r in rows) / len(rows) * 100, 1), '%')
