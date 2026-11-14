"""How much of the chosen intraday candidate's result is the market's own move that day."""
import datetime as dt
import json
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import intraday_model as M      # noqa: E402

pred = pl.read_parquet(sys.argv[1])
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
nifty = {}
for row in json.loads((Path(sys.argv[2]) / 'NSE_INDEX_Nifty_50.json').read_text()):
    ts = dt.datetime.fromisoformat(row[0]).astimezone(IST)
    if ts.time() in (dt.time(9, 45), dt.time(15, 15)):
        nifty.setdefault(ts.date(), {})[ts.time()] = row[1]
mkt = pl.DataFrame([{'day': d, 'mkt_pct': (v[dt.time(15, 15)] / v[dt.time(9, 45)] - 1) * 100}
                    for d, v in nifty.items() if len(v) == 2])
for period, (lo, hi) in (('dev', M.DEV), ('holdout', M.HOLDOUT)):
    p = pred.filter(pl.col('day').is_between(lo, hi))
    t = M.trades(p, 'gbm', 3, True).join(mkt, on='day', how='left')
    t = t.with_columns((pl.col('net_pct') - pl.col('side') * pl.col('mkt_pct')).alias('alpha_pct'))
    print(f'== {period}: net % a trade {t["net_pct"].mean():.3f}; market part {(t["side"]*t["mkt_pct"]).mean():.3f}; '
          f'after removing the market {t["alpha_pct"].mean():.3f} '
          f'(t {t["alpha_pct"].mean() / t["alpha_pct"].std() * t.height ** .5:.2f})')
    for side in (1, -1):
        s = t.filter(pl.col('side') == side)
        print(f'   side {side:+d}: net {s["net_pct"].mean():.3f}, market-removed {s["alpha_pct"].mean():.3f}')
    by_y = t.group_by(pl.col('day').dt.year().alias('y')).agg(
        pl.len(), pl.col('net_pct').mean().round(3), pl.col('alpha_pct').mean().round(3),
        pl.col('side').mean().round(2).alias('net_side')).sort('y')
    print(by_y)
