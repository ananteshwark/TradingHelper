"""S1, S2 and S3 of research/futures/PROTOCOL.md.

Usage:
  uv run python -I futures.py FO_DIR BUILD_DIR dev
  uv run python -I futures.py FO_DIR BUILD_DIR holdout S2=a|b|none S3=a|b|none

FO_DIR holds futures.parquet and idx/ (NSE's ind_close_all files) from research/fno.
Each invocation prints only the period it is asked for.
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.momentum import cost_pct      # noqa: E402

fo, build, period = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
choice = dict(a.split('=') for a in sys.argv[4:])
D = dt.date
GST = 0.18
CASH_YEAR = 0.06


def fut_cost(slip: float) -> float:
    exch, sebi, brok = 0.00173 * 2, 0.0001 * 2, 40 / 15e5 * 100
    return 0.05 + exch + sebi + 0.002 + brok + GST * (exch + sebi + brok) + 2 * slip


STOCK_FUT, INDEX_FUT = fut_cost(0.03), fut_cost(0.01)

# ------------------------------------------------------------------ data
futs = pl.read_parquet(fo / 'futures.parquet')
days = pl.read_parquet(build / 'days.parquet')['day'].to_list()
px = pl.read_parquet(build / 'prices.parquet').sort('sid', 'di')
adj = px.select('symbol', 'day', (pl.col('aclose') / pl.col('close')).alias('f'))
sids = px['sid'].unique().sort().to_list()
row = {s: i for i, s in enumerate(sids)}
AC = np.full((len(sids), len(days)), np.nan)
AO = np.full((len(sids), len(days)), np.nan)
RO = np.full((len(sids), len(days)), np.nan)
ri, di = px['sid'].replace_strict(row, return_dtype=pl.Int64).to_numpy(), px['di'].to_numpy()
AC[ri, di], AO[ri, di], RO[ri, di] = px['aclose'].to_numpy(), px['aopen'].to_numpy(), px['open'].to_numpy()
LC = pl.DataFrame(AC.T).with_columns(pl.all().fill_nan(None).forward_fill()).to_numpy().T.astype(float)
sid_of = dict(zip(zip(px['symbol'].to_list(), px['di'].to_list(), strict=True),
                  px['sid'].to_list(), strict=True))


def index_closes(names: set[str]) -> np.ndarray:
    """An index's official close on each trading day, carried forward over missing files."""
    out = np.full(len(days), np.nan)
    pos = {d: i for i, d in enumerate(days)}
    for path in (fo / 'idx').glob('*.csv'):
        d = dt.date.fromisoformat(path.stem)
        if d not in pos:
            continue
        for line in path.read_text(errors='replace').splitlines()[1:]:
            parts = line.split(',')
            if parts and parts[0].strip().strip('"') in names:
                try:
                    out[pos[d]] = float(parts[5])
                except (ValueError, IndexError):
                    pass
                break
    return pl.Series(out).fill_nan(None).forward_fill().to_numpy().astype(float)


NIFTY = index_closes({'Nifty 50', 'CNX Nifty', 'S&P CNX Nifty'})
BANK = index_closes({'Nifty Bank', 'CNX Bank', 'Bank Nifty'})

starts = [i for i in range(1, len(days)) if days[i].month != days[i - 1].month
          and D(2013, 2, 1) <= days[i] <= D(2026, 10, 1)]
months = list(zip(starts, starts[1:], strict=False))
lo, hi = (D(2013, 1, 1), D(2020, 12, 31)) if period == 'dev' else (D(2021, 1, 1), D(2026, 9, 30))
months = [(e, x) for e, x in months if lo <= days[e] <= hi]


def contract_price(kind: str, symbol: str, entry: dt.date, exit_: dt.date):
    """(entry open, exit open) of the contract expiring on or after the exit day among those
    that traded on the entry day; None if either price is missing."""
    live = FUT.get((entry, kind, symbol))
    if not live:
        return None
    exps = sorted(e for e in live if e >= exit_)
    if not exps:
        return None
    e = exps[0]
    a, b = live[e], FUT_ALL.get((exit_, kind, symbol, e))
    if not a or not b:
        return None
    return a, b


FUT, FUT_ALL = {}, {}
for d, kind, sym, exp, o, traded in futs.select('day', 'kind', 'symbol', 'expiry', 'open',
                                                'traded').iter_rows():
    if not o or o <= 0:
        continue
    FUT_ALL[(d, kind, sym, exp)] = o
    if traded and traded > 0:
        FUT.setdefault((d, kind, sym), {})[exp] = o
ADJ = dict(zip(zip(adj['symbol'].to_list(), adj['day'].to_list(), strict=True),
               adj['f'].to_list(), strict=True))
live_on = {}
for (d, kind, sym) in FUT:
    if kind == 'STF':
        live_on.setdefault(d, set()).add(sym)


def momentum_top(s: int, n: int = 10) -> list[tuple[str, int]]:
    cands = []
    for sym in live_on.get(days[s], ()):
        sid = sid_of.get((sym, s))
        if sid is None:
            continue
        a, b = AC[row[sid], s - 252], AC[row[sid], s - 21]
        if a > 0 and b > 0:
            cands.append((b / a - 1, sym, row[sid]))
    cands.sort(reverse=True)
    return [(sym, r) for _, sym, r in cands[:n]], len(cands)


def stock_fut_return(sym: str, e: int, x: int) -> float | None:
    p = contract_price('STF', sym, days[e], days[x])
    fa, fb = ADJ.get((sym, days[e])), ADJ.get((sym, days[x]))
    if p is None or not fa or not fb:
        return None
    return (p[1] * fb) / (p[0] * fa) - 1


def index_fut_return(sym: str, e: int, x: int) -> float | None:
    p = contract_price('IDF', sym, days[e], days[x])
    return None if p is None else p[1] / p[0] - 1


def beta(r: int, s: int) -> float | None:
    a = AC[r, s - 252:s + 1]
    n = NIFTY[s - 252:s + 1]
    ra, rn = a[1:] / a[:-1] - 1, n[1:] / n[:-1] - 1
    ok = ~np.isnan(ra) & ~np.isnan(rn)
    if ok.sum() < 200:
        return None
    return float(np.cov(ra[ok], rn[ok])[0, 1] / np.var(rn[ok], ddof=1))


def cash(e: int, x: int) -> float:
    return (1 + CASH_YEAR) ** ((days[x] - days[e]).days / 365) - 1


# ------------------------------------------------------------------ the strategies
rows, held_before = [], set()
for e, x in months:
    s = e - 1
    top, universe = momentum_top(s)
    fr = [(sym, r, stock_fut_return(sym, e, x)) for sym, r in top]
    fr = [t for t in fr if t[2] is not None]
    long_fut = np.mean([t[2] for t in fr]) - STOCK_FUT / 100 if fr else np.nan
    # Shares: the same ten, open to open, delivery costs on the names newly bought.
    sh, costs = [], 0.0
    for sym, r in top:
        a = AO[r, e]
        b = AO[r, x] if not np.isnan(AO[r, x]) else LC[r, x - 1]
        if np.isnan(a) or np.isnan(b):
            continue
        sh.append(b / a - 1)
        if sym not in held_before and RO[r, e] > 0:
            costs += cost_pct(RO[r, e]) / 100
    held_before = {sym for sym, _ in top}
    shares = np.mean(sh) - costs / max(len(sh), 1) if sh else np.nan
    betas = [b for b in (beta(r, s) for _, r in top) if b is not None]
    hb = float(np.clip(np.mean(betas), 0.5, 1.5)) if betas else 1.0
    nf = index_fut_return('NIFTY', e, x)
    bf = index_fut_return('BANKNIFTY', e, x)
    rec = {'entry': days[e], 'universe': universe, 'priced': len(fr), 'cash': cash(e, x),
           'S1_fut_total': long_fut + cash(e, x), 'S1_shares': shares, 'beta': hb}
    for v, h in (('a', hb), ('b', 1.0)):
        rec[f'S2{v}'] = (long_fut - h * nf - h * INDEX_FUT / 100) if nf is not None else np.nan
    for v in ('a', 'b'):
        legs = []
        for close, ret in ((NIFTY, nf), (BANK, bf)):
            if ret is None:
                continue
            up = (close[s] / close[s - 252] - 1 > 0) if v == 'a' else (close[s] > np.nanmean(close[s - 199:s + 1]))
            legs.append((1 if up else -1) * ret - INDEX_FUT / 100)
        rec[f'S3{v}'] = np.mean(legs) if legs else np.nan
    rec['long_both'] = np.mean([r for r in (nf, bf) if r is not None]) - INDEX_FUT / 100
    rec['nifty_fut'] = nf if nf is not None else np.nan
    rows.append(rec)
M = pl.DataFrame(rows)


def stats(col: str, label: str, over_cash: bool) -> dict:
    x = M[col].to_numpy() * 100
    ok = ~np.isnan(x)
    x, n = x[ok], M['nifty_fut'].to_numpy()[ok] * 100
    total = x + (M['cash'].to_numpy()[ok] * 100 if over_cash else 0)
    nav = np.cumprod(1 + total / 100)
    dd = (nav / np.maximum.accumulate(nav) - 1).min() * 100
    ok_n = ~np.isnan(n)
    return {'strategy': label, 'months': len(x), 'mean_%': round(x.mean(), 2),
            't': round(x.mean() / x.std(ddof=1) * math.sqrt(len(x)), 2),
            'total_a_year_%': round((nav[-1] ** (12 / len(x)) - 1) * 100, 1),
            'Sharpe': round(x.mean() / x.std(ddof=1) * math.sqrt(12), 2) if over_cash else None,
            'worst_month_%': round(total.min(), 1), 'max_dd_%': round(dd, 1),
            'corr_nifty': round(float(np.corrcoef(x[ok_n], n[ok_n])[0, 1]), 2)}


print(f'{period}: {len(months)} months, universe {M["universe"].min()}-{M["universe"].max()} '
      f'stocks with futures, {M["priced"].mean():.1f} of 10 priced a month on average; costs a '
      f'round trip {STOCK_FUT:.3f}% (stock futures), {INDEX_FUT:.3f}% (index futures); cash '
      f'{CASH_YEAR:.0%} a year; average hedge beta {M["beta"].mean():.2f}')
want = ([('S2a', 'S2a hedged, h = beta'), ('S2b', 'S2b hedged, h = 1'),
         ('S3a', 'S3a trend, 12-month return'), ('S3b', 'S3b trend, 200-day average')]
        if period == 'dev' else
        [(f"S2{choice['S2']}", f"S2{choice['S2']} hedged momentum")] * (choice['S2'] != 'none')
        + [(f"S3{choice['S3']}", f"S3{choice['S3']} trend")] * (choice['S3'] != 'none'))
table = [stats(c, lab, True) for c, lab in want]
# 'mean_%' for S1 rows is the total return a month (futures with cash; shares outright).
table += [stats('S1_fut_total', 'S1 momentum via futures, with cash', False),
          stats('S1_shares', 'S1 momentum via shares', False),
          stats('long_both', 'Nifty + Bank Nifty futures held long', True)]
with pl.Config(tbl_rows=12, tbl_cols=10, tbl_width_chars=200):
    print(pl.DataFrame(table))
yr = M.group_by(pl.col('entry').dt.year().alias('y')).agg(
    [(pl.col(c).mean() * 100).round(2) for c, _ in want] + [(pl.col('S1_fut_total') - pl.col('S1_shares')).mean().mul(100).round(2).alias('S1_fut_minus_shares')]).sort('y')
print('Mean monthly return by year, %:')
with pl.Config(tbl_rows=12, tbl_cols=10):
    print(yr)
