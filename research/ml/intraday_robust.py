"""Robustness of the chosen intraday candidate (reported, not used to change it)."""
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import intraday_model as M                     # noqa: E402
from igs.intraday.costs import charges, intraday_rates   # noqa: E402

pred = pl.read_parquet(sys.argv[1])
data = Path(sys.argv[2])
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
rates = intraday_rates()

for period, (lo, hi) in (('dev', M.DEV), ('holdout', M.HOLDOUT)):
    p = pred.filter(pl.col('day').is_between(lo, hi))
    days = sorted(p['day'].unique())
    t = M.trades(p, 'gbm', 3, True)
    daily = pl.DataFrame({'day': days}).join(t.group_by('day').agg(pl.col('pnl').sum()), on='day',
                                             how='left').fill_null(0)
    eq = daily['pnl'].cum_sum()
    dd = (eq - eq.cum_max()).min()
    monthly = daily.group_by(pl.col('day').dt.strftime('%Y-%m').alias('m')).agg(pl.col('pnl').sum()).sort('m')
    print(f'\n== {period}: {t.height} trades, total Rs {t["pnl"].sum():,.0f}, max drawdown Rs {dd:,.0f}, '
          f'months positive {(monthly["pnl"] > 0).sum()}/{monthly.height}, worst month Rs {monthly["pnl"].min():,.0f}, '
          f'worst trade {t["net_pct"].min():.2f}%, best {t["net_pct"].max():.2f}%')
    for side in (1, -1):
        s = t.filter(pl.col('side') == side)
        by_y = s.group_by(pl.col('day').dt.year().alias('y')).agg(pl.len(), pl.col('net_pct').mean().round(3)).sort('y')
        print(' side', side, s.height, 'trades, net %', round(s['net_pct'].mean(), 3), by_y.rows())
    for slip in (0.0005, 0.001):
        extra = (slip - M.SLIP) * 2 * 100
        print(f' slippage {slip*100:.2f}% a side: net % a trade about {t["net_pct"].mean() - extra:.3f}')
    if period == 'holdout':
        with pl.Config(tbl_rows=30):
            print(monthly)
        # One candle later: enter at the 09:50 open.
        uni = {u['trading_symbol']: u['instrument_key'] for u in json.loads((data / 'universe.json').read_text())}
        want = t.select('symbol', 'day', 'side')
        later = {}
        for sym in want['symbol'].unique():
            f = data / (uni[sym].replace('|', '_').replace(' ', '_') + '.json')
            ds = set(want.filter(pl.col('symbol') == sym)['day'])
            for row in json.loads(f.read_text()):
                ts = dt.datetime.fromisoformat(row[0]).astimezone(IST)
                if ts.date() in ds and ts.time() in (dt.time(9, 50), dt.time(15, 15)):
                    later.setdefault((sym, ts.date()), {})[ts.time()] = row[1]
        out = []
        for r in want.iter_rows(named=True):
            px = later.get((r['symbol'], r['day']), {})
            if dt.time(9, 50) not in px or dt.time(15, 15) not in px:
                continue
            side = r['side']
            e = px[dt.time(9, 50)] * (1 + side * M.SLIP)
            x = px[dt.time(15, 15)] * (1 - side * M.SLIP)
            qty = int(M.NOTIONAL // e)
            if qty < 1:
                continue
            cost = float(charges(qty, Decimal(str(round(e, 2))), Decimal(str(round(x, 2))),
                                 'buy' if side == 1 else 'sell', rates))
            out.append((side * (x - e) * qty - cost) / (e * qty) * 100)
        print(f' entry one candle later (09:50): {len(out)} trades, net % a trade {sum(out)/len(out):.3f}')
