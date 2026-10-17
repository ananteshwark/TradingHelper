"""Price moves after prominent institutions' block deals, 2013-2026
(research/institutional_deals/PROTOCOL.md).

Usage (uv run --no-project --with polars --with numpy --with pyyaml python -I impact.py
       BLOCK_DIR PRICES BHAV UNIVERSE REGISTER OUT_JSON):
  BLOCK_DIR  research/move_starts' monthly block-deal JSON files
  PRICES     research/ml's adjusted closes (sid, symbol, day, aclose)
  BHAV       the parsed bhavcopies (day, symbol, series, value)
  UNIVERSE   research/ml_rating's universe500.parquet (sid, day: month-end)
  REGISTER   config/institutions.yaml
"""
import datetime as dt
import glob
import json
import math
import re
import sys

import numpy as np
import polars as pl
import yaml

BUCKETS = ((0, 1, 'under 1x'), (1, 5, '1-5x'), (5, math.inf, '5x and more'))
HALVES = ((dt.date(2013, 1, 1), dt.date(2019, 12, 31), '2013-2019'),
          (dt.date(2020, 1, 1), dt.date(2026, 12, 31), '2020-2026'))
MIN_EVENTS = 30


def norm(s):
    return re.sub(r'[^A-Z0-9]', '', str(s).upper())


def register(path):
    entries = yaml.safe_load(open(path))['institutions']
    return [(e['name'], e['type'], [norm(m) for m in e['match']]) for e in entries]


def match(reg, client):
    n = norm(client)
    for name, kind, frags in reg:
        if any(f in n for f in frags):
            return name, kind
    return None


def events(block_dir, reg):
    rows = []
    for f in sorted(glob.glob(f'{block_dir}/*.json')):
        for r in json.load(open(f))['data']:
            m = match(reg, r['BD_CLIENT_NAME'])
            if not m:
                continue
            q = float(r['BD_QTY_TRD'])
            rows.append({'day': dt.datetime.strptime(r['BD_DT_DATE'], '%d-%b-%Y').date(),  # noqa: DTZ007
                         'symbol': r['BD_SYMBOL'].strip().upper(), 'institution': m[0],
                         'type': m[1], 'qty': q if r['BD_BUY_SELL'].upper() == 'BUY' else -q,
                         'price': float(r['BD_TP_WATP'])})
    d = pl.DataFrame(rows)
    d = d.with_columns(pl.when(pl.col('qty') > 0).then(pl.lit('buy')).otherwise(pl.lit('sell'))
                       .alias('leg'))
    net = (d.group_by('day', 'symbol', 'institution', 'type')
             .agg(pl.col('qty').sum().alias('net'),
                  # the volume-weighted price of each side, chosen below by the net's sign
                  ((pl.col('qty').clip(lower_bound=0) * pl.col('price')).sum()
                   / pl.col('qty').clip(lower_bound=0).sum()).alias('p_buy'),
                  ((-pl.col('qty').clip(upper_bound=0) * pl.col('price')).sum()
                   / (-pl.col('qty').clip(upper_bound=0)).sum()).alias('p_sell'))
             .filter(pl.col('net') != 0))
    return net.with_columns(
        pl.when(pl.col('net') > 0).then(pl.lit('buy')).otherwise(pl.lit('sell')).alias('side'),
        pl.when(pl.col('net') > 0).then(pl.col('p_buy')).otherwise(pl.col('p_sell'))
        .alias('price')).with_columns((pl.col('net').abs() * pl.col('price')).alias('value'))


def main(block_dir, prices_path, bhav_path, universe_path, register_path, out_json):
    reg = register(register_path)
    ev = events(block_dir, reg)
    print(f'Institutional block-deal events (institution, stock, day): {ev.height}')

    px = pl.read_parquet(prices_path).select('sid', 'symbol', 'day', 'aclose')
    days = sorted(px['day'].unique().to_list())
    di = {d: i for i, d in enumerate(days)}
    sids = sorted(px['sid'].unique().to_list())
    si = {s: i for i, s in enumerate(sids)}
    close = np.full((len(days), len(sids)), np.nan)
    close[[di[d] for d in px['day'].to_list()], [si[s] for s in px['sid'].to_list()]] = \
        px['aclose'].to_numpy()

    # The deal's stock: its sid on the deal day, by the symbol it traded under then.
    ev = ev.join(px.select('symbol', 'day', 'sid'), on=['symbol', 'day'], how='inner')
    # Average daily traded value over the 20 sessions before the deal.
    bhav = (pl.scan_parquet(bhav_path).filter(pl.col('series') == 'EQ')
              .select('symbol', 'day', 'value').collect().unique(['symbol', 'day'])
              .sort('symbol', 'day')
              .with_columns(pl.col('value').rolling_mean(20, min_samples=15).shift(1)
                            .over('symbol').alias('adv')))
    ev = ev.join(bhav.select('symbol', 'day', 'adv'), on=['symbol', 'day'], how='left')
    ev = ev.with_columns((pl.col('value') / pl.col('adv')).alias('adv_x'))

    uni = pl.read_parquet(universe_path).select('sid', pl.col('day').alias('me'))
    months = sorted(uni['me'].unique().to_list())
    members = {m: np.array([si[s] for s in g['sid'].to_list() if s in si])
               for (m,), g in uni.group_by(['me'])}

    def window(i, a, b, cols):
        """Returns from day index i+a to i+b for columns `cols` (NaN where missing)."""
        if i + a < 0 or i + b >= len(days):
            return np.full(len(cols), np.nan)
        return close[i + b, cols] / close[i + a, cols] - 1

    out = []
    for r in ev.iter_rows(named=True):
        i, s = di[r['day']], si[r['sid']]
        prior = [m for m in months if m < r['day']]
        if not prior:
            continue
        cols = members[prior[-1]]
        row = {'day': r['day'], 'type': r['type'], 'side': r['side'],
               'institution': r['institution'], 'symbol': r['symbol'],
               'adv_x': r['adv_x'], 'value': r['value']}
        for name, a, b in (('d0', -1, 0), ('d1', 0, 1), ('d21', 0, 21)):
            own = window(i, a, b, np.array([s]))[0]
            bench = np.nanmean(window(i, a, b, cols))
            row[name] = own - bench if np.isfinite(own) and np.isfinite(bench) else None
        out.append(row)
    res = pl.DataFrame(out)
    both = (res.group_by('day', 'symbol').agg(pl.col('side').n_unique().alias('n'))
               .filter(pl.col('n') == 2).height)
    print(f'Events with prices: {res.height}; stock-days with a registered buyer and a '
          f'registered seller: {both}\n')

    def stats(g):
        n = g.height
        out = {'n': n}
        for w in ('d0', 'd1', 'd21'):
            x = g.drop_nulls(w)
            if x.height == 0:
                continue
            monthly = (x.with_columns(pl.col('day').dt.truncate('1mo').alias('m'))
                        .group_by('m').agg(pl.col(w).mean())[w].to_numpy())
            t = (monthly.mean() / monthly.std(ddof=1) * math.sqrt(len(monthly))
                 if len(monthly) > 1 and monthly.std(ddof=1) > 0 else float('nan'))
            out[w] = {'mean': float(x[w].mean()), 'median': float(x[w].median()),
                      'positive': float((x[w] > 0).mean()), 't': float(t), 'n': x.height}
        return out

    def show(label, s):
        if s['n'] == 0:
            return
        parts = []
        for w, name in (('d0', 'deal day'), ('d1', 'next session'), ('d21', '21 sessions')):
            if w in s:
                v = s[w]
                parts.append(f"{name} {v['mean'] * 100:+.2f}% (median {v['median'] * 100:+.2f}%,"
                             f" t {v['t']:.1f}, {v['positive']:.0%} up)")
        print(f"  {label:34s} {s['n']:5d}  " + '; '.join(parts))

    table = {}
    for kind in ('FII', 'DII'):
        for side in ('buy', 'sell'):
            g = res.filter((pl.col('type') == kind) & (pl.col('side') == side))
            print(f'{kind} {side}s')
            s = stats(g)
            show('all', s)
            table.setdefault(kind, {})[side] = {'all': s}
            for lo, hi, label in BUCKETS:
                gb = g.filter((pl.col('adv_x') >= lo) & (pl.col('adv_x') < hi))
                sb = stats(gb)
                show(f'size {label} of daily value', sb)
                table[kind][side][label] = sb
            for lo, hi, label in HALVES:
                show(label, stats(g.filter(pl.col('day').is_between(lo, hi))))
            print()
    table['_about'] = {
        'source': 'research/institutional_deals (NSE block deals 2013-2026)',
        'window': 'excess over the equal-weighted 500 most-traded stocks, from the deal '
                  'day close', 'min_events': MIN_EVENTS,
        'buckets': [label for _, _, label in BUCKETS]}
    with open(out_json, 'w') as f:
        json.dump(table, f, indent=1, sort_keys=True)


if __name__ == '__main__':
    main(*sys.argv[1:7])
