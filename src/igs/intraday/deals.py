"""NSE bulk/block disclosures. Names are classified only by the admin's exact-name register."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
from collections import defaultdict
from decimal import Decimal, InvalidOperation

import httpx

from igs.ingest.http import _BROWSER_HEADERS
from igs.intraday.upstox import FeedError
from igs.timeutil import IST, utc_now

BASE = 'https://nsearchives.nseindia.com/content/equities/'
REQUIRED = {'Date', 'Symbol', 'Security Name', 'Client Name', 'Buy/Sell', 'Quantity Traded'}


def parse(body, now):
    reader = csv.DictReader(io.StringIO(body.lstrip('\ufeff')))
    if not REQUIRED <= set(reader.fieldnames or []):
        raise ValueError('NSE bulk/block CSV schema changed')
    net = defaultdict(Decimal)
    for row in reader:
        day = dt.datetime.strptime(row['Date'].strip(), '%d-%b-%Y').date()  # noqa: DTZ007
        if not now.astimezone(IST).date()-dt.timedelta(days=7) <= day <= now.astimezone(IST).date():
            continue
        side = row['Buy/Sell'].strip().upper()
        if side not in ('BUY', 'SELL'):
            raise ValueError('Invalid NSE deal side')
        quantity = Decimal(row['Quantity Traded'].replace(',', '').strip())
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError('Invalid NSE deal quantity')
        investor, symbol = row['Client Name'].strip().upper(), row['Symbol'].strip().upper()
        if not investor or not symbol:
            raise ValueError('Missing NSE deal identity')
        net[day, symbol, investor] += quantity if side == 'BUY' else -quantity
    return [(day, symbol, name, 'buy' if qty > 0 else 'sell', abs(qty))
            for (day, symbol, name), qty in net.items() if qty]


def collect(conn, *, client=None, now=None):
    now = now or utc_now()
    own = client is None
    client = client or httpx.Client(headers=_BROWSER_HEADERS, timeout=15)
    count = 0
    try:
        for kind in ('bulk', 'block'):
            url = BASE + kind + '.csv'
            try:
                response = client.get(url)
            except httpx.HTTPError:
                raise FeedError('NSE bulk/block download failed') from None
            if response.status_code != 200:
                raise FeedError(f'NSE {kind} deals returned HTTP {response.status_code}')
            body = response.text
            digest = hashlib.sha256((url+body).encode()).hexdigest()
            if conn.execute('select 1 from intraday_deal_fetch where content_hash=%s',
                            (digest,)).fetchone():
                continue
            try:
                deals = parse(body, now)
            except (ValueError, InvalidOperation):
                raise FeedError('NSE bulk/block format changed; no rows loaded') from None
            conn.execute('insert into intraday_deal_fetch(content_hash,source_url,body,fetched_at) '
                         'values(%s,%s,%s,%s)', (digest, url, body, now))
            for day, symbol, name, side, qty in deals:
                row = conn.execute('''select s.company_id from security_identifier i
                    join security s using(security_id) where id_type='NSE_SYMBOL' and id_value=%s
                    and valid_from<=%s and (valid_to is null or valid_to>%s)''',
                    (symbol, day, day)).fetchone()
                if not row:
                    continue
                category = conn.execute('select category from intraday_investor_watch '
                                        'where investor=%s', (name,)).fetchone()
                detail = (f'NSE {kind} deals: net {qty} shares. First observed at collection; '
                          f'exchange file supplies only trade date. Raw snapshot {digest}.')
                added = conn.execute('''insert into intraday_investor_event(company_id,investor,
                    category,side,trade_date,published_at,received_at,source_url,evidence)
                    values(%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing returning event_id''',
                    (row[0], name, category[0] if category else 'large', side, day, now, now,
                     url, detail)).fetchone()
                count += bool(added)
            conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        if own:
            client.close()
