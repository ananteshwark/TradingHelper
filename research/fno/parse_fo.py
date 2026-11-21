"""NSE F&O bhavcopies to two tables: every futures contract (stock and index), and Nifty
options with strikes within 20% of the nearest Nifty future and expiries within 75 days.

Usage: uv run python -I parse_fo.py ZIP_DIR OUT_DIR
"""
import datetime as dt
import io
import sys
import zipfile
from pathlib import Path

import polars as pl

src, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
fut_parts, opt_parts, bad = [], [], []


def old(raw: bytes, day: dt.date) -> pl.DataFrame:
    d = pl.read_csv(io.BytesIO(raw), infer_schema_length=0, truncate_ragged_lines=True)
    d = d.rename({c: c.strip() for c in d.columns})
    return d.select(
        pl.lit(day).alias('day'),
        pl.col('INSTRUMENT').str.strip_chars().replace(
            {'FUTIDX': 'IDF', 'FUTSTK': 'STF', 'OPTIDX': 'IDO', 'OPTSTK': 'STO'}).alias('kind'),
        pl.col('SYMBOL').str.strip_chars().alias('symbol'),
        pl.col('EXPIRY_DT').str.strip_chars().str.to_date('%d-%b-%Y', strict=False).alias('expiry'),
        pl.col('STRIKE_PR').cast(pl.Float64, strict=False).alias('strike'),
        pl.col('OPTION_TYP').str.strip_chars().alias('opt'),
        *[pl.col(a).cast(pl.Float64, strict=False).alias(b) for a, b in
          (('OPEN', 'open'), ('HIGH', 'high'), ('LOW', 'low'), ('CLOSE', 'close'),
           ('SETTLE_PR', 'settle'), ('CONTRACTS', 'traded'), ('OPEN_INT', 'oi'))],
        pl.lit(None, dtype=pl.Float64).alias('underlying'),
        pl.lit(None, dtype=pl.Float64).alias('lot'))


def udiff(raw: bytes, day: dt.date) -> pl.DataFrame:
    d = pl.read_csv(io.BytesIO(raw), infer_schema_length=0)
    return d.select(
        pl.lit(day).alias('day'), pl.col('FinInstrmTp').alias('kind'),
        pl.col('TckrSymb').alias('symbol'),
        pl.col('XpryDt').str.to_date('%Y-%m-%d', strict=False).alias('expiry'),
        pl.col('StrkPric').cast(pl.Float64, strict=False).alias('strike'),
        pl.col('OptnTp').alias('opt'),
        *[pl.col(a).cast(pl.Float64, strict=False).alias(b) for a, b in
          (('OpnPric', 'open'), ('HghPric', 'high'), ('LwPric', 'low'), ('ClsPric', 'close'),
           ('SttlmPric', 'settle'), ('TtlTradgVol', 'traded'), ('OpnIntrst', 'oi'),
           ('UndrlygPric', 'underlying'), ('NewBrdLotQty', 'lot'))])


for path in sorted(src.glob('*.zip')):
    day = dt.date.fromisoformat(path.stem)
    try:
        with zipfile.ZipFile(path) as z:
            raw = z.read(z.namelist()[0])
        d = old(raw, day) if raw[:10].startswith(b'INSTRUMENT') else udiff(raw, day)
    except Exception as exc:  # noqa: BLE001
        bad.append((day, type(exc).__name__))
        continue
    fut = d.filter(pl.col('kind').is_in(['IDF', 'STF']))
    fut_parts.append(fut.drop('strike', 'opt'))
    near = fut.filter(pl.col('symbol') == 'NIFTY').sort('expiry')
    if near.height:
        f = near['close'][0]
        o = d.filter((pl.col('kind') == 'IDO') & (pl.col('symbol') == 'NIFTY')
                     & (pl.col('expiry') <= day + dt.timedelta(days=75))
                     & pl.col('strike').is_between(f * 0.8, f * 1.2))
        opt_parts.append(o.drop('kind', 'symbol', 'open', 'high', 'low', 'oi'))
futs = pl.concat(fut_parts, how='vertical_relaxed')
opts = pl.concat(opt_parts, how='vertical_relaxed')
futs.write_parquet(out / 'futures.parquet')
opts.write_parquet(out / 'nifty_options.parquet')
print(f'{len(fut_parts)} days; futures rows {futs.height}, Nifty option rows {opts.height}; '
      f'{futs["day"].min()} to {futs["day"].max()}; unreadable: {bad[:10]} ({len(bad)})')
