"""Download each universe stock's standalone quarterly filings named in the listings
(ranges/*.json to mid-2025, ifpages/*.json after): one per quarter, its first broadcast.
Repeats until both listing logs say done. Usage: fetch_files2.py RATING_DIR PART PARTS"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx
import polars as pl

R, part, parts = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
FILES = R / 'files'
c = httpx.Client(timeout=60, follow_redirects=True, headers={
    'User-Agent': 'IndiaGrowthScreener research (ananteshwark/TradingHelper)'})
FIRST = dt.date(2010, 12, 31)
by_isin = {}
for sid, isin, _ in pl.read_parquet(R / 'sid_isins.parquet').iter_rows():
    by_isin.setdefault(isin, set()).add(sid)
by_symbol = {}
for sid, symbol, first, last in pl.read_parquet(R / 'sid_symbols.parquet').iter_rows():
    by_symbol.setdefault(symbol, []).append((sid, first, last))


def match(symbol, isin, when):
    near = [sid for sid, first, last in by_symbol.get(symbol, [])
            if first - dt.timedelta(days=400) <= when.date() <= last + dt.timedelta(days=400)]
    hits = by_isin.get(isin, set())
    if len(hits) == 1:
        return next(iter(hits))
    if len(near) == 1:
        return near[0]
    both = hits & set(near)
    return next(iter(both)) if len(both) == 1 else None


def when_of(v, fmt='%d-%b-%Y %H:%M:%S'):
    try:
        return dt.datetime.strptime(v, fmt)
    except (TypeError, ValueError):
        return None


def wanted():
    out = {}

    def keep(sid, end, when, url, source):
        if sid is None or sid % parts != part or end < FIRST or when is None or not url:
            return
        k = (sid, end)
        if k not in out or when < out[k][0]:
            out[k] = (when, url, source)

    for p in sorted((R / 'ranges').glob('*.json')):
        for r in json.loads(p.read_text()):
            if r.get('consolidated') != 'Non-Consolidated' or \
                    r.get('cumulative') != 'Non-cumulative':
                continue
            when = when_of(r.get('broadCastDate')) or when_of(r.get('filingDate'), '%d-%b-%Y %H:%M')
            end = when_of(r.get('toDate'), '%d-%b-%Y')
            url = r.get('xbrl') if r.get('xbrl') and not r['xbrl'].endswith('/-') else \
                r.get('resultDetailedDataLink')
            if end and when:
                keep(match(r.get('symbol'), r.get('isin'), when), end.date(), when, url, 'nse')
    for p in sorted((R / 'ifpages').glob('*.json')) if (R / 'ifpages').exists() else []:
        for r in json.loads(p.read_text()).get('data', []):
            if r.get('consolidated') != 'Standalone':
                continue
            when = when_of(r.get('broadcast_Date')) or when_of(r.get('creation_Date'))
            end = when_of((r.get('qe_Date') or '').title(), '%d-%b-%Y')
            if end and when:
                keep(match(r.get('symbol'), None, when), end.date(), when, r.get('xbrl'), 'if')
    return out


def finished():
    return all((R / f).exists() and 'done' in (R / f).read_text()[-300:]
               for f in ('ranges.log', 'if.log'))


while True:
    last_pass = finished()
    todo = wanted()
    metas = {}
    for (sid, end), (when, url, source) in sorted(todo.items()):
        d = FILES / str(sid)
        d.mkdir(parents=True, exist_ok=True)
        ext = 'xml' if url.endswith('.xml') else 'html'
        target = d / f'{end}.{ext}'
        metas.setdefault(sid, {})[str(end)] = {'broadcast': when.isoformat(), 'url': url,
                                               'file': target.name, 'source': source}
        if target.exists() or (d / f'{end}.missing').exists():
            continue
        for attempt in range(4):
            try:
                r = c.get(url)
            except httpx.HTTPError:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 200:
                target.write_bytes(r.content)
            elif r.status_code == 404:
                (d / f'{end}.missing').write_text(url)
            else:
                print('status', r.status_code, url, flush=True)
                time.sleep(15 * (attempt + 1))
                continue
            break
        time.sleep(0.2)
    for sid, meta in metas.items():
        (FILES / str(sid) / 'meta.json').write_text(json.dumps(meta))
    print('pass', len(todo), flush=True)
    if last_pass:
        break
    time.sleep(120)
print('done', flush=True)
