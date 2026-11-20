"""Results of intraday.py's tests A and B (PROTOCOL.md).

Usage: uv run python -I analyze.py RESULTS_PARQUET
"""
import math
import sys

import polars as pl

res = pl.read_parquet(sys.argv[1])
trades = res.filter(pl.col('test') != 'U')
univ = res.filter(pl.col('test') == 'U')


def t_stat(x: pl.Series) -> float:
    n = x.len()
    sd = x.std()
    return x.mean() / sd * math.sqrt(n) if n > 1 and sd else float('nan')


def report(name: str, t: pl.DataFrame) -> None:
    if t.is_empty():
        print(f'{name}: no trades')
        return
    daily = t.group_by('day').agg(pl.col('net').sum())['net']
    print(f"{name}: {t.height} trades on {daily.len()} days; a trade: net "
          f"{t['net'].mean() / 1000:+.3f}% (gross {t['gross'].mean() / 1000:+.3f}%), "
          f"{(t['net'] > 0).mean():.0%} won; daily net Rs {daily.mean():+.0f}, "
          f"t {t_stat(daily):.2f}")


variants = {
    'A1 board, both sides, at 09:50': trades.filter(pl.col('test') == 'A'),
    'A2 board, dominant side only': trades.filter(pl.col('test') == 'A',
                                                  pl.col('side') == pl.col('dominant')),
    'B1 first return to the block': trades.filter(pl.col('test') == 'B'),
    'B2 the same, dominant side only': trades.filter(pl.col('test') == 'B',
                                                     pl.col('side') == pl.col('dominant')),
}
for name, t in variants.items():
    report(name, t)
for name in ('A1 board, both sides, at 09:50', 'B1 first return to the block'):
    for side, label in ((1, 'buys'), (-1, 'sells')):
        report(f'  {name[:2]} {label}', variants[name].filter(pl.col('side') == side))

# Hit rate: picks ending in the top fifth of the day's F&O stocks, on their own side.
a = variants['A1 board, both sides, at 09:50']
cut = univ.group_by('day').agg(pl.col('universe_ret').quantile(0.8).alias('q80'),
                               pl.col('universe_ret').quantile(0.2).alias('q20'),
                               pl.col('universe_ret').mean().alias('mean'))
h = a.join(cut, on='day').with_columns(
    pl.when(pl.col('side') == 1).then(pl.col('ret') >= pl.col('q80'))
    .otherwise(-pl.col('ret') <= pl.col('q20')).alias('top'),
    (pl.col('ret') - pl.col('side') * pl.col('mean')).alias('excess'))
print(f"Hit rate: {h['top'].mean():.0%} of picks in the day's top fifth for their side "
      f"(chance 20%); gross excess over the F&O average {h['excess'].mean() * 100:+.3f}% "
      f"a pick (t {t_stat(h['excess']):.2f})")
for side, label in ((1, 'buys'), (-1, 'sells')):
    s = h.filter(pl.col('side') == side)
    print(f"  {label}: {s['top'].mean():.0%} in the top fifth; excess "
          f"{s['excess'].mean() * 100:+.3f}% (t {t_stat(s['excess']):.2f})")

b = variants['B1 first return to the block'].drop_nulls('chase_ret')
print(f"Zone against chase, same {b.height} picks, gross: zone {b['ret'].mean() * 100:+.3f}%,"
      f" 09:50 entry {b['chase_ret'].mean() * 100:+.3f}% a trade")
print('B exits:', dict(variants['B1 first return to the block']
                       .group_by('exit_kind').len().iter_rows()))
print(f"B median R:R at entry {variants['B1 first return to the block']['rr'].median():.2f}")
