"""Fill rates and results by order setting, from fills.py's output.
Usage: uv run python -I summary.py FILLS.parquet"""
import sys

import polars as pl

d = pl.read_parquet(sys.argv[1])
filled = pl.col('outcome').is_in(['stop', 'target', 'time'])
g = d.group_by('levels', 'latency', 'life').agg(
    pl.len().alias('calls'), filled.sum().alias('filled'),
    filled.mean().mul(100).round(1).alias('fill_pct'),
    ((pl.col('outcome') == 'target').sum() / filled.sum() * 100).round(0).alias('target_pct'),
    pl.col('gross').mean().round(0).alias('gross_per_fill'),
    pl.col('net').mean().round(0).alias('net_per_fill'),
    (pl.col('net').fill_null(0).sum() / pl.len()).round(1).alias('net_per_call'),
).sort('latency', 'levels', 'life')
print('Order live: latency 0 = at the call candle\'s close, 1 = five minutes later, 2 = ten.')
print('Life: call = until the call expires (ten minutes after its candle), N = minutes after')
print('the order is live, day = until 15:15. Rupees per Rs 1 lakh order, after charges.\n')
for r in g.iter_rows(named=True):
    print(f"{r['levels']:<31} live+{r['latency'] * 5:>2}min life {r['life']:<4} "
          f"filled {r['filled']:>4}/{r['calls']} ({r['fill_pct']:>5}%) "
          f"target first {r['target_pct']:>3.0f}% gross/fill {r['gross_per_fill']:>5.0f} "
          f"net/fill {r['net_per_fill']:>5.0f} net/call {r['net_per_call']:>6.1f}")
