"""How many intraday traders end a year in profit with no skill at all?

Every trade is a coin flip: a random liquid Nifty 200 stock on a random 2025 trading day, a
random direction, entered at the open of a random 5-minute candle between 09:30 and 14:30
and closed at the 15:15 candle's open, after 0.02% slippage a side and Upstox intraday
charges. Traders differ only in how many trades they make in the year (SEBI's buckets) and
trade size (SEBI's FY23 averages). Then the same with a real edge added to each trade.

Usage: uv run python -I luck.py DATA_DIR OUT_DIR
"""
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.intraday.costs import charges, intraday_rates   # noqa: E402

DATA, OUT = Path(sys.argv[1]), Path(sys.argv[2])
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
rng = np.random.default_rng(11)
rates = intraday_rates()

# One pool of possible trades: (gross return of a long, entry price), 2025, liquid stocks.
pool_ret, pool_px = [], []
uni = json.loads((DATA / 'universe.json').read_text())
for u in uni:
    rows = json.loads((DATA / (u['instrument_key'].replace('|', '_') + '.json')).read_text())
    days = {}
    for r in rows:
        t = dt.datetime.fromisoformat(r[0]).astimezone(IST)
        if t.year == 2025:
            days.setdefault(t.date(), {})[t.time()] = (r[1], r[4], r[5])
    for d, bars in days.items():
        ex = bars.get(dt.time(15, 15))
        if not ex:
            continue
        value = sum(c * v for _, c, v in bars.values())
        if value < 50e7:                  # liquid that day: Rs 50 crore traded
            continue
        for tm, (o, _, _) in bars.items():
            if dt.time(9, 30) <= tm <= dt.time(14, 30):
                pool_ret.append(ex[0] / o - 1)
                pool_px.append(o)
pool_ret, pool_px = np.array(pool_ret), np.array(pool_px)
print('pool', len(pool_ret), 'trades; mean long return %.4f%%, sd %.3f%%'
      % (pool_ret.mean() * 100, pool_ret.std() * 100))


def cost_pct(size, px):
    """Round-trip charges as % of the trade, for a trade of about `size` rupees."""
    qty = max(1, int(size // px))
    c = float(charges(qty, Decimal(str(round(px, 2))), Decimal(str(round(px, 2))), 'buy', rates))
    return c / (qty * px) + 2 * 0.0002


def simulate(n_trades, size, edge=0.0, traders=20000):
    idx = rng.integers(0, len(pool_ret), size=(traders, n_trades))
    side = rng.choice([-1, 1], size=(traders, n_trades))
    gross = side * pool_ret[idx] + edge
    costs = np.vectorize(lambda p: cost_pct(size, p))(pool_px[idx[:, :1]])  # per trader, approx
    net = gross - costs
    pnl = (net * size).sum(axis=1)
    return pnl


rows = []
for label, n, size in (('under 10 trades', 5, 21551), ('10-50', 25, 21551), ('50-100', 75, 21551),
                       ('100-500', 250, 265664), ('over 500', 742, 265664)):
    for edge in (0.0, 0.0005, 0.0013):
        pnl = simulate(n, size, edge)
        win = pnl > 0
        rows.append({'bucket': label, 'trades': n, 'size_rs': size, 'edge_pct': edge * 100,
                     'profitable_pct': round(win.mean() * 100, 1),
                     'avg_profit_of_winners': round(pnl[win].mean()) if win.any() else None,
                     'avg_loss_of_losers': round(pnl[~win].mean()) if (~win).any() else None})
        print(rows[-1], flush=True)
(OUT / 'luck.json').write_text(json.dumps(rows, indent=1))
sd = pool_ret.std()
for edge in (0.0005, 0.0013):
    n = (2 * sd / edge) ** 2
    print(f'trades needed before an edge of {edge*100:.2f}% a trade shows at t = 2: about {n:,.0f}')
