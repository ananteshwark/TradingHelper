import sys, datetime as dt
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
sys.argv = ['m', 'pred.parquet', 'x']
import polars as pl
import model as M
pred = pl.read_parquet('pred.parquet')
rets = pl.read_parquet('pred_rets.parquet')
for lo, hi, name in ((*M.DEV, 'Development 2016-2020'), (*M.HOLD, 'Holdout 2021-2026')):
    print(name)
    for p in ['p_growth', 'p_quality', 'p_valuation', 'p_momentum', 'p_low_volatility', 'rule']:
        t = M.stats(M.trading(pred, p, lo, hi)); i = M.stats(M.investing(pred, rets, p, lo, hi))
        print(f"  {p:18s} trading {t['net']*100:+.2f}% (t {t['t']:5.2f})   investing {i['net']*100:+.2f}% (t {i['t']:5.2f})")
