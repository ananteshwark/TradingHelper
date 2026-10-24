"""Parse the NSE bhavcopies (old and UDiFF formats) and MTO delivery files into two parquet
files: rows for series EQ and BE only.

Usage: uv run python -I parse_bhav.py NSE_DIR OUT_DIR
"""
import csv
import datetime as dt
import io
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl

SERIES = {'EQ', 'BE'}


def num(x):
    x = (x or '').strip()
    return float(x) if x else None


def parse_cm(path):
    day = dt.date.fromisoformat(path.stem)
    with zipfile.ZipFile(path) as z:
        text = z.read(z.namelist()[0]).decode('utf-8', 'replace')
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {k.strip(): v for k, v in r.items() if k}
        if 'SYMBOL' in r:
            series, sym, isin = r['SERIES'].strip(), r['SYMBOL'].strip(), r.get('ISIN', '').strip()
            vals = (r['OPEN'], r['HIGH'], r['LOW'], r['CLOSE'], r['PREVCLOSE'], r['TOTTRDQTY'],
                    r['TOTTRDVAL'], r.get('TOTALTRADES'))
        else:
            series, sym, isin = r['SctySrs'].strip(), r['TckrSymb'].strip(), r['ISIN'].strip()
            vals = (r['OpnPric'], r['HghPric'], r['LwPric'], r['ClsPric'], r['PrvsClsgPric'],
                    r['TtlTradgVol'], r['TtlTrfVal'], r['TtlNbOfTxsExctd'])
        if series not in SERIES:
            continue
        o, h, l, c, pc, q, v, n = (num(x) for x in vals)
        if not c or c <= 0 or not o or o <= 0:
            continue
        rows.append((day, sym, series, isin, o, h, l, c, pc, q, v, n))
    return rows


def parse_mto(path):
    day = dt.date.fromisoformat(path.stem)
    rows = []
    for line in path.read_text(errors='replace').splitlines():
        parts = line.split(',')
        if len(parts) >= 7 and parts[0] == '20' and parts[3].strip() in SERIES:
            try:
                rows.append((day, parts[2].strip(), parts[3].strip(), float(parts[4]),
                             float(parts[5]), float(parts[6])))
            except ValueError:
                pass
    return rows


if __name__ == '__main__':
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    with ProcessPoolExecutor(4) as pool:
        cm = [r for part in pool.map(parse_cm, sorted((src / 'cm').glob('*.zip')), chunksize=20)
              for r in part]
        mto = [r for part in pool.map(parse_mto, sorted((src / 'mto').glob('*.dat')), chunksize=20)
               for r in part]
    bhav = pl.DataFrame(cm, orient='row', schema=[
        ('day', pl.Date), ('symbol', pl.Utf8), ('series', pl.Utf8), ('isin', pl.Utf8),
        ('open', pl.Float64), ('high', pl.Float64), ('low', pl.Float64), ('close', pl.Float64),
        ('prevclose', pl.Float64), ('qty', pl.Float64), ('value', pl.Float64), ('trades', pl.Float64)])
    deliv = pl.DataFrame(mto, orient='row', schema=[
        ('day', pl.Date), ('symbol', pl.Utf8), ('series', pl.Utf8), ('traded_qty', pl.Float64),
        ('deliv_qty', pl.Float64), ('deliv_pct', pl.Float64)])
    bhav.sort('day', 'symbol').write_parquet(out / 'bhav.parquet')
    deliv.sort('day', 'symbol').write_parquet(out / 'deliv.parquet')
    print('bhav', bhav.height, bhav['day'].n_unique(), 'days', bhav['day'].min(), bhav['day'].max())
    print('deliv', deliv.height, deliv['day'].n_unique(), 'days')
