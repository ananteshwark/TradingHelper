"""F1, F2 and F3 of research/fno/PROTOCOL.md.

Usage:
  uv run python -I fno.py FO_DIR BUILD_DIR EVENTS check
  uv run python -I fno.py FO_DIR BUILD_DIR EVENTS f1|f2 dev|holdout
  uv run python -I fno.py FO_DIR BUILD_DIR EVENTS f3a|f3b dev
  uv run python -I fno.py FO_DIR BUILD_DIR EVENTS f3a|f3b holdout D W

Each invocation prints only the test and period it is asked for.
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

fo, build, events_path, test = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
NIFTY_NAMES = {'Nifty 50', 'CNX Nifty', 'S&P CNX Nifty'}
period = sys.argv[5] if len(sys.argv) > 5 else None
D = dt.date

# ------------------------------------------------------------------ costs (% of notional)
GST = 0.18


def fut_cost(slip: float) -> float:
    """Round trip on one futures position, % of notional (protocol: today's rates)."""
    exch, sebi, brok = 0.00173 * 2, 0.0001 * 2, 40 / 15e5 * 100
    return 0.05 + exch + sebi + 0.002 + brok + GST * (exch + sebi + brok) + 2 * slip


STOCK_FUT, NIFTY_FUT = fut_cost(0.03), fut_cost(0.01)

# ------------------------------------------------------------------ data
futs = pl.read_parquet(fo / 'futures.parquet')
days = pl.read_parquet(build / 'days.parquet')['day'].to_list()
di_of = {d: i for i, d in enumerate(days)}
px = pl.read_parquet(build / 'prices.parquet')
adj = px.select('symbol', 'day', (pl.col('aclose') / pl.col('close')).alias('f'))


def fut_prices(rows: pl.DataFrame) -> pl.DataFrame:
    """Futures open on the entry and exit days for (key, symbol, kind, expiry, entry, exit)
    rows, adjusted for corporate actions with the equity's factor on each day."""
    f = futs.select('day', 'kind', 'symbol', 'expiry', 'open', 'traded')
    out = (rows.join(f.rename({'day': 'entry', 'open': 'f_in', 'traded': 'traded_in'}),
                     on=['entry', 'kind', 'symbol', 'expiry'], how='left')
           .join(f.rename({'day': 'exit', 'open': 'f_out', 'traded': 'traded_out'}),
                 on=['exit', 'kind', 'symbol', 'expiry'], how='left'))
    out = (out.join(adj.rename({'day': 'entry', 'f': 'a_in'}), on=['symbol', 'entry'], how='left')
           .join(adj.rename({'day': 'exit', 'f': 'a_out'}), on=['symbol', 'exit'], how='left'))
    return out.with_columns(
        pl.when(pl.col('kind') == 'IDF').then(1.0).otherwise(pl.col('a_in')).alias('a_in'),
        pl.when(pl.col('kind') == 'IDF').then(1.0).otherwise(pl.col('a_out')).alias('a_out'))


def contract(rows: pl.DataFrame) -> pl.DataFrame:
    """The contract each row trades: the nearest expiry on or after its exit day among the
    contracts on its symbol that traded on its entry day."""
    live = futs.filter(pl.col('traded') > 0).select(
        pl.col('day').alias('entry'), 'kind', 'symbol', 'expiry')
    c = rows.join(live, on=['entry', 'kind', 'symbol'], how='inner').filter(
        pl.col('expiry') >= pl.col('exit'))
    return c.sort('expiry').group_by('key', maintain_order=True).first()


def monthly_t(df: pl.DataFrame, col: str, date_col: str) -> float | None:
    m = df.group_by(pl.col(date_col).dt.strftime('%Y-%m')).agg(pl.col(col).mean())[col]
    return round(m.mean() / m.std() * math.sqrt(m.len()), 2) if m.len() > 2 else None


def by_year(df: pl.DataFrame, col: str, date_col: str) -> str:
    y = df.group_by(pl.col(date_col).dt.year().alias('y')).agg(pl.col(col).mean()).sort('y')
    return ' '.join(f'{a}:{b:+.2f}' for a, b in y.rows())


def span(lo_hi):
    return (D(2013, 1, 1), D(2020, 12, 31)) if lo_hi == 'dev' else (D(2021, 1, 1), D(2026, 9, 30))


# ------------------------------------------------------------------ F1
def f1():
    panel = pl.read_parquet(build / 'panel.parquet').select('sid', 'symbol', 'di')
    pxs = px.sort('sid', 'di')
    sids = pxs['sid'].unique().sort().to_list()
    row = {s: i for i, s in enumerate(sids)}
    AC = np.full((len(sids), len(days)), np.nan)
    AC[pxs['sid'].replace_strict(row, return_dtype=pl.Int64).to_numpy(), pxs['di'].to_numpy()] = \
        pxs['aclose'].to_numpy()
    members = {d: np.array([row[s] for s in g['sid']]) for (d,), g in panel.group_by(['di'])}
    day_ix = np.array([d.toordinal() for d in days])
    ev = pl.read_parquet(events_path)
    o = ev['meeting'].map_elements(lambda d: d.toordinal(), return_dtype=pl.Int64).to_numpy()
    ev = ev.with_columns(pl.Series('bm1', np.searchsorted(day_ix, o, side='left') - 1),
                         pl.Series('bp1', np.searchsorted(day_ix, o, side='right')))
    ev = ev.filter((pl.col('bm1') >= 0) & (pl.col('bp1') + 1 + 21 < len(days)))
    ev = ev.with_columns(pl.col('bp1').cast(pl.Int32)).join(
        panel, left_on=['symbol', 'bp1'], right_on=['symbol', 'di'], how='inner')
    recs = []
    for sym, bm1, bp1, sid in ev.select('symbol', 'bm1', 'bp1', 'sid').iter_rows():
        r = row[sid]
        c0, c1 = AC[r, bm1], AC[r, bp1]
        if np.isnan(c0) or np.isnan(c1):
            continue
        mem = members[bp1]
        ab = (c1 / c0 - 1) - np.nanmean(AC[mem, bp1] / AC[mem, bm1] - 1)
        if ab <= -0.05:
            recs.append({'symbol': sym, 'entry': days[bp1 + 1], 'exit': days[bp1 + 22],
                         'reaction': ab * 100})
    E = pl.DataFrame(recs).with_row_index('key')
    lo, hi = span(period)
    E = E.filter(pl.col('entry').is_between(lo, hi))
    stock = contract(E.with_columns(pl.lit('STF').alias('kind')))
    nifty = contract(E.select('key', 'entry', 'exit').with_columns(
        pl.lit('IDF').alias('kind'), pl.lit('NIFTY').alias('symbol')))
    s = fut_prices(stock).filter(pl.col('f_in').is_not_null() & pl.col('f_out').is_not_null()
                                 & (pl.col('f_in') > 0) & (pl.col('f_out') > 0)
                                 & pl.col('a_in').is_not_null() & pl.col('a_out').is_not_null())
    n = fut_prices(nifty).select('key', (pl.col('f_out') / pl.col('f_in') - 1).alias('nifty'))
    s = s.with_columns(((pl.col('f_out') * pl.col('a_out')) / (pl.col('f_in') * pl.col('a_in'))
                        - 1).alias('r')).join(n, on='key', how='inner')
    s = s.with_columns((-pl.col('r') * 100 - STOCK_FUT).alias('naked'),
                       ((-pl.col('r') + pl.col('nifty')) * 100 - STOCK_FUT - NIFTY_FUT).alias('hedged'))
    print(f'F1 {period}: {E.height} events of -5% or worse; {s.height} with stock futures '
          f'on the entry day; costs a round trip {STOCK_FUT:.3f}% (stock), {NIFTY_FUT:.3f}% (Nifty)')
    for col in ('hedged', 'naked'):
        print(f'  {col:7s} net % an event: mean {s[col].mean():+.2f}, median {s[col].median():+.2f}, '
              f't (monthly) {monthly_t(s, col, "entry")}, worst {s[col].min():+.1f}, '
              f'best {s[col].max():+.1f}')
        print(f'          by year: {by_year(s, col, "entry")}')
    print(f'  gross (before costs) hedged {(s["hedged"] + STOCK_FUT + NIFTY_FUT).mean():+.2f}%, '
          f'naked {(s["naked"] + STOCK_FUT).mean():+.2f}%')


# ------------------------------------------------------------------ F2
def f2():
    pxs = px.sort('sid', 'di')
    sids = pxs['sid'].unique().sort().to_list()
    row = {s: i for i, s in enumerate(sids)}
    AC = np.full((len(sids), len(days)), np.nan)
    AC[pxs['sid'].replace_strict(row, return_dtype=pl.Int64).to_numpy(), pxs['di'].to_numpy()] = \
        pxs['aclose'].to_numpy()
    sid_of = dict(zip(zip(pxs['symbol'].to_list(), pxs['di'].to_list(), strict=True),
                      pxs['sid'].to_list(), strict=True))
    starts = [i for i in range(1, len(days)) if days[i].month != days[i - 1].month
              and D(2013, 2, 1) <= days[i] <= D(2026, 10, 1)]
    live = futs.filter((pl.col('kind') == 'STF') & (pl.col('traded') > 0))
    live_on = {d: set(g['symbol']) for (d,), g in live.group_by(['day'])}
    rows = []
    for k in range(len(starts) - 1):
        e, x = starts[k], starts[k + 1]
        s = e - 1
        if s - 252 < 0:
            continue
        cands = []
        for sym in live_on.get(days[s], ()):
            sid = sid_of.get((sym, s))
            if sid is None:
                continue
            a, b = AC[row[sid], s - 252], AC[row[sid], s - 21]
            if a > 0 and b > 0:
                cands.append((b / a - 1, sym))
        cands.sort()
        for side, picks in ((-1, cands[:10]), (1, cands[-10:])):
            rows += [{'key': len(rows) + i, 'symbol': sym, 'side': side, 'entry': days[e],
                      'exit': days[x], 'universe': len(cands)} for i, (_, sym) in enumerate(picks)]
    R = pl.DataFrame(rows)
    lo, hi = span(period)
    R = R.filter(pl.col('entry').is_between(lo, hi))
    c = fut_prices(contract(R.with_columns(pl.lit('STF').alias('kind'))))
    c = c.filter(pl.col('f_in').is_not_null() & pl.col('f_out').is_not_null()
                 & pl.col('a_in').is_not_null() & pl.col('a_out').is_not_null()
                 & (pl.col('f_in') > 0) & (pl.col('f_out') > 0))
    c = c.with_columns((((pl.col('f_out') * pl.col('a_out')) / (pl.col('f_in') * pl.col('a_in'))
                         - 1) * 100 * pl.col('side') - STOCK_FUT).alias('net'))
    m = c.group_by('entry', 'side').agg(pl.col('net').mean(), pl.len().alias('n')).pivot(
        on='side', index='entry', values=['net', 'n']).sort('entry')
    m = m.rename({'net_1': 'long', 'net_-1': 'short', 'n_1': 'n_long', 'n_-1': 'n_short'}).with_columns(
        (pl.col('long') + pl.col('short')).alias('ls'))
    print(f'F2 {period}: {m.height} months, universe {R["universe"].min()}-{R["universe"].max()} '
          f'stocks with futures; positions priced {c.height} of {R.height}; cost a round trip '
          f'{STOCK_FUT:.3f}%')
    for col in ('ls', 'long', 'short'):
        x = m[col].to_numpy()
        nav = np.cumprod(1 + x / 100)
        dd = (nav / np.maximum.accumulate(nav) - 1).min() * 100
        print(f'  {col:5s} net % a month: mean {x.mean():+.2f}, t {x.mean() / x.std(ddof=1) * math.sqrt(len(x)):.2f}, '
              f'a year {((1 + x.mean() / 100) ** 12 - 1) * 100:+.1f}%, Sharpe {x.mean() / x.std(ddof=1) * math.sqrt(12):.2f}, '
              f'worst month {x.min():+.1f}, max drawdown {dd:.1f}%')
    print(f'  long-short by year: {by_year(m, "ls", "entry")}')


# ------------------------------------------------------------------ F3
def nifty_closes() -> dict:
    """The official Nifty 50 closing value each day (NSE's ind_close_all files): the final
    settlement price of index options on an expiry day."""
    out = {}
    for path in sorted((fo / 'idx').glob('*.csv')):
        for line in path.read_text(errors='replace').splitlines()[1:]:
            parts = line.split(',')
            if parts and parts[0].strip().strip('"') in NIFTY_NAMES:
                try:
                    out[dt.date.fromisoformat(path.stem)] = float(parts[5])
                except (ValueError, IndexError):
                    pass
                break
    return out


def settlement_prices() -> dict:
    """The final settlement price on each day: the official Nifty close, else the final
    settlement of a Nifty future expiring that day, else the underlying value NSE prints
    in the UDiFF files."""
    nf = futs.filter((pl.col('kind') == 'IDF') & (pl.col('symbol') == 'NIFTY'))
    out = {d: u for d, u in nf.filter(pl.col('underlying').is_not_null())
           .group_by('day').agg(pl.col('underlying').first()).iter_rows()}
    out |= {d: v for d, v in nf.filter(pl.col('day') == pl.col('expiry'))
            .select('day', 'settle').iter_rows()}
    return out | nifty_closes()


def condors(weekly: bool, d: float, w: float) -> pl.DataFrame:
    opts = pl.read_parquet(fo / 'nifty_options.parquet')
    nf = futs.filter((pl.col('kind') == 'IDF') & (pl.col('symbol') == 'NIFTY'))
    monthly = sorted(set(nf['expiry'].to_list()))
    exps = sorted(set(opts['expiry'].to_list()))
    if weekly:
        first = min(e for e in exps if e not in set(monthly) and e >= D(2019, 2, 1))
        exps = [e for e in exps if e >= first]
    else:
        exps = [e for e in monthly if e in set(exps)]
    od = {k: g for k, g in opts.group_by(['day', 'expiry'])}
    closes = settlement_prices()
    nfd = {k[0]: g.sort('expiry') for k, g in nf.group_by(['day'])}
    out, skipped = [], 0
    for prev, exp in zip(exps, exps[1:], strict=False):
        later = [x for x in days if x > prev]
        if not later or later[0] >= exp:
            skipped += 1
            continue
        entry = later[0]
        chain = od.get((entry, exp))
        fut = nfd.get(entry)
        if chain is None or fut is None or exp not in closes:
            skipped += 1
            continue
        fut = fut.filter(pl.col('expiry') >= exp)
        if fut.is_empty():
            skipped += 1
            continue
        F = fut['close'][0]
        traded = chain.filter((pl.col('traded') > 0) & (pl.col('close') > 0))

        def pick(opt, target, beyond=None):
            c = traded.filter(pl.col('opt') == opt)
            if beyond is not None:
                c = c.filter(pl.col('strike') > beyond if opt == 'CE' else pl.col('strike') < beyond)
            if c.is_empty():
                return None
            c = c.with_columns((pl.col('strike') - target).abs().alias('gap')).sort('gap')
            return c['strike'][0], c['close'][0]
        sc = pick('CE', F * (1 + d))
        sp = pick('PE', F * (1 - d))
        if sc is None or sp is None:
            skipped += 1
            continue
        lc = pick('CE', F * (1 + d + w), beyond=sc[0])
        lp = pick('PE', F * (1 - d - w), beyond=sp[0])
        if lc is None or lp is None:
            skipped += 1
            continue
        S = closes[exp]                                    # final settlement price
        credit = sc[1] + sp[1] - lc[1] - lp[1] - 4 * 0.5
        pay = (-max(S - sc[0], 0) - max(sp[0] - S, 0) + max(S - lc[0], 0) + max(lp[0] - S, 0))
        prem = sc[1] + sp[1] + lc[1] + lp[1]
        exch = prem * (0.03503 + 0.0001) / 100
        cost = (0.0015 * (sc[1] + sp[1]) + 0.0015 * (max(S - lc[0], 0) + max(lp[0] - S, 0))
                + exch * (1 + GST) + 0.00003 * (lc[1] + lp[1]) + 1.3)
        cap = (lc[0] - sc[0] + sp[0] - lp[0]) / 2          # the average wing width, points
        out.append({'entry': entry, 'expiry': exp, 'F': F, 'S': S, 'credit': credit,
                    'pnl': credit + pay - cost, 'ret': (credit + pay - cost) / cap * 100,
                    'cap_pts': cap})
    return pl.DataFrame(out), skipped


def f3(weekly: bool):
    lo, hi = ((D(2013, 1, 1), D(2020, 12, 31)) if not weekly else (D(2019, 2, 1), D(2022, 12, 31)))
    if period == 'holdout':
        lo, hi = ((D(2021, 1, 1), D(2026, 9, 30)) if not weekly else (D(2023, 1, 1), D(2026, 10, 8)))
        variants = [(float(sys.argv[6]), float(sys.argv[7]))]
    else:
        variants = [(0.03, 0.02), (0.05, 0.03)] if not weekly else [(0.015, 0.01), (0.025, 0.015)]
    name = 'F3b weekly' if weekly else 'F3a monthly'
    for d, w in variants:
        t, skipped = condors(weekly, d, w)
        t = t.filter(pl.col('expiry').is_between(lo, hi))
        x = t['ret'].to_numpy()
        years = (hi - lo).days / 365.25
        per_year = len(x) / years
        cum = np.cumsum(x)
        dd = (cum - np.maximum.accumulate(np.concatenate([[0], cum]))[1:]).min()
        print(f'{name} {period} d={d:.3f} w={w:.3f}: {len(x)} periods ({per_year:.1f} a year), '
              f'skipped {skipped} in all years')
        print(f'  return on capital at risk a period: mean {x.mean():+.2f}%, median {np.median(x):+.2f}%, '
              f't {x.mean() / x.std(ddof=1) * math.sqrt(len(x)):.2f}, won {np.mean(x > 0):.0%}, '
              f'worst {x.min():+.1f}%, best {x.max():+.1f}%')
        print(f'  a year (mean x periods): {x.mean() * per_year:+.1f}%; max drawdown (sum of '
              f'returns) {dd:.1f}%; average credit {t["credit"].mean():.1f} pts on '
              f'{t["cap_pts"].mean():.0f} pts at risk')
        print(f'  by year: {by_year(t, "ret", "expiry")}')


def check():
    """Data checks only (no strategy results): the official Nifty close against the Nifty
    futures' final settlement on monthly expiries, coverage of expiry days, and futures."""
    closes = nifty_closes()
    nf = futs.filter((pl.col('kind') == 'IDF') & (pl.col('symbol') == 'NIFTY')
                     & (pl.col('day') == pl.col('expiry')))
    diffs = [(day, closes[day] - settle_f) for day, settle_f in nf.select('day', 'settle').iter_rows()
             if day in closes]
    dif = np.array([abs(x) for _, x in diffs])
    print(f'{len(closes)} days with the Nifty close; {len(diffs)} monthly expiries: '
          f'|close - futures final settlement| median {np.median(dif):.2f} pts, max {dif.max():.2f}')
    exps = set(pl.read_parquet(fo / 'nifty_options.parquet')['expiry'].to_list())
    settle = settlement_prices()
    print(f'option expiries in the data {len(exps)}; without a Nifty close: '
          f'{len([e for e in exps if e not in closes and e <= D(2026, 10, 8)])}; without any '
          f'settlement price: {sorted(e for e in exps if e not in settle and e <= D(2026, 10, 8))}')
    print(futs.group_by(pl.col('day').dt.year()).agg(pl.col('day').n_unique().alias('days'),
                                                     pl.col('symbol').n_unique().alias('symbols')).sort('day'))


{'check': check, 'f1': f1, 'f2': f2, 'f3a': lambda: f3(False), 'f3b': lambda: f3(True)}[test]()
