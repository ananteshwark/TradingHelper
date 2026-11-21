"""Promoters buying and selling their own company's shares, in every listed company, sent
on Telegram as soon as the app's data shows it (docs/PROMOTER_TRADES.md).

Three sources, each recorded once in `promoter_trade`:

- **Insider-trading disclosures** (SEBI PIT), loaded by the 30-minute data check: trades
  by a promoter or a member of the promoter group, bought or sold through the market.
  Transfers that move no shares through the market (inter-se transfers, gifts, pledges,
  ESOPs, allotments) are left out. A mode the app doesn't know (a "Block Deal", say) is
  kept, named as disclosed.
- **NSE's bulk and block deal files**, collected every 15 minutes: a deal whose client is
  one of the company's known promoters, on the deal day itself, a day or two before the
  disclosure. A promoter is known from the names in the company's earlier insider-trading
  disclosures, matched exactly after spelling is normalised ("Mr", "Pvt Ltd" and
  punctuation dropped).
- **Takeover-code disclosures** (SEBI SAST Regulation 29: a holding of 5% or more, or a
  change of 2% or more), from NSE's announcements, when the announcement names a known
  promoter or says "promoter". Their figures are in the attached PDF, so the message
  carries its link. Pledges and other encumbrances (Regulation 31) are not trades.

Messages go to the institutional deals chat (Telegram), by rules.promoter_trades in
config/alerts.yaml: every company or only the watchlist, buys and sells, and a minimum
value (watchlist companies need none).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from psycopg.rows import dict_row

from igs.timeutil import IST, utc_now

DEFAULTS = {'enabled': True, 'scope': 'all', 'min_value_cr': 0.5, 'include_buys': True,
            'include_sells': True, 'sast': True}
MAX_AGE = dt.timedelta(days=3)          # a trade first seen earlier than this isn't sent
DEAL_DAYS = 7                           # deal file rows older than this are ignored
PRICE = 'Trade Price / Wght. Avg. Price'
# Modes that move no shares through the market: igs.normalize.nse's vocabulary, and the
# words of its other spellings ("Off-Market Transfer", "Inter se transfer of shares").
NOT_MARKET = {'others', 'other', '-', ''}
NOT_MARKET_WORDS = ('off market', 'off-market', 'offmarket', 'inter', 'gift', 'pledge',
                    'esop', 'esos', 'allot', 'bonus', 'right', 'transmission', 'amalgamation',
                    'merger', 'arrangement', 'conversion', 'preferential', 'invocation',
                    'revocation', 'revokation', 'buyback', 'buy back', 'inheritance', 'will')
HONORIFICS = {'MR', 'MRS', 'MS', 'MISS', 'SHRI', 'SHRIMATI', 'SMT', 'SRI', 'DR', 'KUMARI',
              'KUM', 'M/S', 'MESSRS'}
SUFFIXES = {'PRIVATE', 'PVT', 'LIMITED', 'LTD', 'LLP', 'PTE', 'INC', 'HUF', 'AND', 'CO'}
SAST_TEXT = re.compile(r'regulation\s*29|substantial\s+acquisition|\bsast\b|takeover',
                       re.IGNORECASE)
# The regulations' own name, which says "Acquisition" whatever the filing reports.
SAST_NAME = re.compile(r'substantial\s+acquisition\s+of\s+shares\s*(and|&)?\s*takeovers?',
                       re.IGNORECASE)
NOT_A_TRADE = re.compile(r'regulation\s*31|encumbrance|pledge', re.IGNORECASE)
VERB = {'buy': 'bought', 'sell': 'sold'}
HEAD = {'buy': 'PROMOTER BOUGHT', 'sell': 'PROMOTER SOLD', 'unknown': 'PROMOTER FILING'}


def norm_name(name) -> str:
    """A person's or company's name for matching: capitals and digits only, without
    honorifics or legal suffixes. '' when too short to match safely."""
    words = re.sub(r'[^A-Z0-9/ ]', ' ', str(name or '').upper()).split()
    while words and words[0] in HONORIFICS:
        words.pop(0)
    while words and words[-1] in SUFFIXES:
        words.pop()
    out = ''.join(re.sub(r'[^A-Z0-9]', '', w) for w in words)
    return out if len(out) >= 6 else ''


def is_promoter(category, role=None) -> bool:
    return role == 'promoter' or 'promoter' in str(category or '').lower()


def through_market(mode, open_market) -> bool:
    """Bought or sold through the exchange, as far as the disclosure says. A mode the app
    doesn't know, without a word of a transfer outside the market, counts."""
    if open_market:
        return True
    m = str(mode or '').strip().lower()
    return m not in NOT_MARKET and not any(re.search(rf'\b{re.escape(w)}', m)
                                           for w in NOT_MARKET_WORDS)


def rule(cfg=None) -> dict:
    """rules.promoter_trades in config/alerts.yaml over DEFAULTS."""
    if cfg is None:
        from igs.config import load_alerts
        cfg = load_alerts()
    return {**DEFAULTS, **(cfg.rules.get('promoter_trades') or {})}


def _company(conn, symbol: str, day: dt.date) -> int | None:
    row = conn.execute('''select s.company_id from security_identifier i
        join security s using (security_id) where id_type='NSE_SYMBOL' and id_value=%s
        and valid_from<=%s and (valid_to is null or valid_to>%s)''',
                       (symbol, day, day)).fetchone()
    return row[0] if row else None


def known_promoters(conn, company_ids) -> dict[int, dict[str, str]]:
    """{company_id: {normalised name: name}} of the people and entities each company's
    insider-trading disclosures name as promoters or promoter group."""
    ids = sorted({c for c in company_ids if c})
    if not ids:
        return {}
    rows = conn.execute("""
        select distinct s.company_id, t.person_name
        from insider_trade t
        join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = t.symbol
         and t.filed_at::date >= si.valid_from
         and (si.valid_to is null or t.filed_at::date < si.valid_to)
        join security s on s.security_id = si.security_id
        where s.company_id = any(%s)
          and (t.insider_role = 'promoter' or t.person_category ilike '%%promoter%%')""",
                        (ids,)).fetchall()
    out: dict[int, dict[str, str]] = defaultdict(dict)
    for cid, person in rows:
        key = norm_name(person)
        if key:
            out[cid].setdefault(key, person)
    return dict(out)


def _insert(conn, row: dict) -> bool:
    cols = list(row)
    got = conn.execute(f"""insert into promoter_trade ({', '.join(cols)})
        values ({', '.join(['%s'] * len(cols))}) on conflict (source, ref) do nothing
        returning trade_id""", [row[c] for c in cols]).fetchone()
    return got is not None


# --------------------------------------------------------------------------- sources


def record_disclosures(conn, now: dt.datetime | None = None) -> int:
    """Promoters' market trades in insider-trading disclosures loaded in the last three
    days and not recorded yet."""
    now = now or utc_now()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            select t.insider_trade_id, t.symbol, t.company_name, t.person_name,
                   t.person_category, t.insider_role, t.side, t.quantity::float8 as quantity,
                   t.value_inr::float8 as value_inr,
                   t.holding_before_pct::float8 as holding_before_pct,
                   t.holding_after_pct::float8 as holding_after_pct, t.acquisition_mode,
                   t.open_market, t.trade_from, t.trade_to, t.filed_at, t.xbrl_url,
                   t.submission_type, s.company_id, c.name
            from insider_trade t
            left join security_identifier si on si.id_type = 'NSE_SYMBOL'
             and si.id_value = t.symbol and t.filed_at::date >= si.valid_from
             and (si.valid_to is null or t.filed_at::date < si.valid_to)
            left join security s on s.security_id = si.security_id
            left join company c on c.company_id = s.company_id
            where t.ingested_at > %s and t.side in ('buy', 'sell')
              and (t.insider_role = 'promoter' or t.person_category ilike '%%promoter%%')
              and (t.security_type is null or lower(t.security_type) like 'equity%%')
              and not exists (select 1 from promoter_trade p where p.source = 'disclosure'
                              and p.ref = t.insider_trade_id::text)
            order by t.filed_at, t.insider_trade_id""", (now - MAX_AGE,))
        rows = cur.fetchall()
    added = 0
    for r in rows:
        if not through_market(r['acquisition_mode'], r['open_market']):
            continue
        qty, value = r['quantity'], r['value_inr']
        notes = []
        if r['submission_type'] == 'Revision':
            notes.append('a revised disclosure')
        lag = (r['filed_at'].astimezone(IST).date() - r['trade_from']).days
        if lag > 10:
            notes.append(f'traded {lag} days before it was disclosed')
        added += _insert(conn, {
            'source': 'disclosure', 'ref': str(r['insider_trade_id']),
            'company_id': r['company_id'], 'symbol': r['symbol'],
            'company_name': r['name'] or r['company_name'], 'person': r['person_name'],
            'category': r['person_category'], 'side': r['side'], 'quantity': qty,
            'price': value / qty if value and qty else None, 'value_inr': value,
            'holding_before_pct': r['holding_before_pct'],
            'holding_after_pct': r['holding_after_pct'], 'mode': r['acquisition_mode'],
            'trade_date': r['trade_from'], 'filed_at': r['filed_at'],
            'detail': '; '.join(notes), 'link': r['xbrl_url'], 'first_seen_at': now})
    conn.commit()
    return added


def _side_in_text(text: str) -> str:
    t = SAST_NAME.sub(' ', text).lower()
    buy = 'acqui' in t
    sell = any(w in t for w in ('dispos', ' sale', ' sold', 'divest'))
    return 'buy' if buy and not sell else 'sell' if sell and not buy else 'unknown'


def record_sast(conn, now: dt.datetime | None = None) -> int:
    """Takeover-code (SAST Regulation 29) disclosures in announcements loaded in the last
    three days that name one of the company's known promoters, or say "promoter"."""
    now = now or utc_now()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            select a.ann_id, a.symbol, a.company_id, c.name, a.filed_at, a.category,
                   a.subject, a.attachment_url
            from announcement a left join company c using (company_id)
            where a.ingested_at > %s and a.company_id is not null
              and not exists (select 1 from promoter_trade p where p.source = 'sast'
                              and p.ref = a.ann_id::text)
            order by a.filed_at, a.ann_id""", (now - MAX_AGE,))
        rows = [r for r in cur.fetchall()
                if SAST_TEXT.search(f"{r['category']} {r['subject']}")
                and not NOT_A_TRADE.search(r['subject'])]
    known = known_promoters(conn, [r['company_id'] for r in rows])
    added = 0
    for r in rows:
        text = norm_name(r['subject']) or re.sub(r'[^A-Z0-9]', '', r['subject'].upper())
        names = known.get(r['company_id'], {})
        person = next((names[k] for k in sorted(names, key=len, reverse=True)
                       if len(k) >= 8 and k in text), None)
        if person is None and 'promoter' not in r['subject'].lower():
            continue
        added += _insert(conn, {
            'source': 'sast', 'ref': str(r['ann_id']), 'company_id': r['company_id'],
            'symbol': r['symbol'], 'company_name': r['name'],
            'person': person or 'a promoter (named in the filing)',
            'category': 'known promoter' if person else 'promoter, per the announcement',
            'side': _side_in_text(r['subject']), 'mode': 'takeover-code disclosure',
            'trade_date': r['filed_at'].astimezone(IST).date(), 'filed_at': r['filed_at'],
            'detail': r['subject'][:1000], 'link': r['attachment_url'],
            'first_seen_at': now})
    conn.commit()
    return added


def deal_rows(body: str, today: dt.date) -> list[dict]:
    """Every row of one NSE bulk or block deal CSV from the last DEAL_DAYS days."""
    out = []
    for row in csv.DictReader(io.StringIO(body.lstrip('﻿'))):
        date = (row.get('Date') or '').strip()
        if not date or date.upper() == 'NO RECORDS':
            continue
        day = dt.datetime.strptime(date, '%d-%b-%Y').date()  # noqa: DTZ007
        if not today - dt.timedelta(days=DEAL_DAYS) <= day <= today:
            continue
        side = (row.get('Buy/Sell') or '').strip().lower()
        qty = Decimal((row.get('Quantity Traded') or '').replace(',', '').strip())
        price = Decimal((row.get(PRICE) or '').replace(',', '').strip())
        if side not in ('buy', 'sell') or not qty > 0 or not price > 0:
            raise ValueError('Invalid NSE deal row')
        out.append({'day': day, 'symbol': (row.get('Symbol') or '').strip().upper(),
                    'security': (row.get('Security Name') or '').strip(),
                    'client': (row.get('Client Name') or '').strip(), 'side': side,
                    'quantity': qty, 'price': price})
    return out


def record_deals(conn, now: dt.datetime | None = None) -> int:
    """Known promoters' net trades in the bulk and block deal files collected in the last
    three days and not checked yet: one per file kind, day, stock and client."""
    now = now or utc_now()
    files = conn.execute("""select f.content_hash, f.source_url, f.body from intraday_deal_fetch f
        where f.fetched_at > %s and not exists (select 1 from promoter_deal_scan s
                                                where s.content_hash = f.content_hash)
        order by f.fetched_at""", (now - MAX_AGE,)).fetchall()
    added = 0
    today = now.astimezone(IST).date()
    for digest, url, body in files:
        kind = 'bulk' if 'bulk' in url.lower() else 'block'
        found = 0
        try:
            rows = deal_rows(body, today)
        except (ValueError, KeyError, InvalidOperation):
            from igs.alerts.operations import record_issue
            record_issue('promoter-deals', 'unreadable deal file')
            rows = []
        companies = {}
        for r in rows:
            key = (r['symbol'], r['day'])
            if key not in companies:
                companies[key] = _company(conn, r['symbol'], r['day'])
        known = known_promoters(conn, companies.values())
        trades = defaultdict(lambda: {'buy': [Decimal(0), Decimal(0)],
                                      'sell': [Decimal(0), Decimal(0)]})
        for r in rows:
            cid = companies[(r['symbol'], r['day'])]
            person = known.get(cid, {}).get(norm_name(r['client']))
            if person is None:
                continue
            t = trades[(r['day'], r['symbol'], cid, r['client'].upper(), person,
                        r['security'])]
            t[r['side']][0] += r['quantity']
            t[r['side']][1] += r['quantity'] * r['price']
        for (day, symbol, cid, client, person, security), t in trades.items():
            net = t['buy'][0] - t['sell'][0]
            if not net:
                continue
            side = 'buy' if net > 0 else 'sell'
            qty, value = t[side]
            price = value / qty
            name = conn.execute('select name from company where company_id=%s',
                                (cid,)).fetchone()
            found += _insert(conn, {
                'source': kind, 'ref': f'{kind}|{day}|{symbol}|{norm_name(client)}',
                'company_id': cid, 'symbol': symbol,
                'company_name': name[0] if name else security or None, 'person': client,
                'category': ('a promoter, per its insider-trading disclosures'
                             if person.upper() == client else
                             f'a promoter, "{person}" in its insider-trading disclosures'),
                'side': side, 'quantity': abs(net), 'price': price,
                'value_inr': abs(net) * price, 'mode': f'{kind} deal', 'trade_date': day,
                'filed_at': now, 'detail': '', 'link': url, 'first_seen_at': now})
        conn.execute('insert into promoter_deal_scan (content_hash, scanned_at, found) '
                     'values (%s, %s, %s) on conflict do nothing', (digest, now, found))
        conn.commit()
        added += found
    return added


# --------------------------------------------------------------------------- messages


def _shares(conn, company_id, day) -> float | None:
    if not company_id:
        return None
    from igs.institutions import _shares
    return _shares(conn, company_id, day)


def message(conn, t: dict) -> str:
    """The Telegram message for one recorded promoter trade (a promoter_trade row)."""
    from igs.assistant.calls import _price_rows
    from igs.institutions import _group
    stock = f"{t['company_name']} ({t['symbol']})" if t.get('company_name') else t['symbol']
    side = t['side']
    head = HEAD[side] + (f" (NSE {t['source']} deal)" if t['source'] in ('bulk', 'block')
                         else '')
    lines = [f'{head}: {stock}']
    who = t['person'] + (f" ({t['category']})" if t.get('category') else '')
    if t['source'] == 'sast':
        lines.append(f"Takeover-code disclosure (SEBI SAST) filed "
                     f"{t['filed_at'].astimezone(IST):%d %b %Y %H:%M} IST, naming {who}:")
        lines.append(t['detail'])
        if side != 'unknown':
            lines.append(f"It reports shares {VERB[side]}; the figures are in the filing.")
        else:
            lines.append('The figures are in the filing.')
    else:
        qty = float(t['quantity'])
        amount = ''
        if t.get('value_inr'):
            amount = f" for ₹{float(t['value_inr']) / 1e7:,.2f} crore"
            if t.get('price'):
                amount += f" (₹{float(t['price']):,.2f} a share)"
        how = (f"in an NSE {t['source']} deal" if t['source'] in ('bulk', 'block')
               else f"through the market ({t['mode']})")
        lines.append(f"{who} {VERB[side]} {_group(qty)} shares {how}{amount} on "
                     f"{t['trade_date']:%d %b %Y}.")
        if t.get('holding_before_pct') is not None and t.get('holding_after_pct') is not None:
            lines.append(f"Their holding: {float(t['holding_before_pct']):.2f}% → "
                         f"{float(t['holding_after_pct']):.2f}%.")
        else:
            shares = _shares(conn, t.get('company_id'), t['trade_date'])
            if shares:
                lines.append(f"{qty / shares * 100:.2f}% of the company's shares.")
        if t['source'] == 'disclosure':
            lines.append(f"Disclosed under SEBI's insider-trading rules on "
                         f"{t['filed_at'].astimezone(IST):%d %b %Y %H:%M} IST.")
        else:
            lines.append("From NSE's deal file; the client is named as a promoter in the "
                         "company's earlier insider-trading disclosures. The disclosure "
                         "follows within about two trading days.")
    if t.get('detail') and t['source'] != 'sast':
        lines.append(f"Note: {t['detail']}.")
    if t.get('company_id') and t.get('trade_date'):
        day = [r for r in _price_rows(conn, t['company_id'], t['trade_date'], t['trade_date'])
               if r[2]]
        if day:
            lines.append(f"On {t['trade_date']:%d %b} the stock closed "
                         f"{(day[0][1] / day[0][2] - 1) * 100:+.1f}% at ₹{day[0][1]:,.2f}.")
    earlier = conn.execute("""select side, trade_date, source from promoter_trade
        where company_id = %s and trade_id <> %s and side <> 'unknown' and trade_date <= %s
        order by trade_date desc, trade_id desc limit 3""",
                           (t.get('company_id'), t['trade_id'], t['trade_date'])).fetchall() \
        if t.get('company_id') else []
    if earlier:
        lines.append('Earlier here: ' + '; '.join(
            f"{VERB[s]} on {d:%d %b %Y} ({src if src not in ('bulk', 'block') else src + ' deal'})"
            for s, d, src in earlier) + '.')
    if t.get('link'):
        lines.append(t['link'])
    return '\n'.join(lines)


def due(conn, r: dict, now: dt.datetime) -> list[dict]:
    """Unsent trades the rule `r` sends, oldest first."""
    sides = [s for s in ('buy', 'sell') if r.get(f'include_{s}s', True)] + ['unknown']
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            select p.*, w.company_id is not null as on_watchlist
            from promoter_trade p left join watchlist w using (company_id)
            where p.alerted_at is null and p.first_seen_at > %s and p.side = any(%s)
              and (%s or p.source <> 'sast')
            order by p.first_seen_at, p.trade_id""", (now - MAX_AGE, sides, bool(r['sast'])))
        rows = cur.fetchall()
    floor = float(r['min_value_cr']) * 1e7
    out = []
    for t in rows:
        if r['scope'] == 'watchlist' and not t['on_watchlist']:
            continue
        if not t['on_watchlist'] and t['value_inr'] is not None \
                and float(t['value_inr']) < floor:
            continue
        out.append(t)
    return out


def alert(conn, notify=None, cfg=None, now: dt.datetime | None = None) -> int:
    """Send each recorded promoter trade once, oldest first; returns how many were sent.
    One stays unsent (and is retried next time) while Telegram is unavailable."""
    r = rule(cfg)
    if not r['enabled']:
        return 0
    if notify is None:
        from igs.alerts.delivery import send_deals as notify
    sent = 0
    for t in due(conn, r, now or utc_now()):
        text = message(conn, t)
        if not notify(text):
            break
        conn.execute('update promoter_trade set alerted_at = now(), alert_text = %s '
                     'where trade_id = %s', (text, t['trade_id']))
        conn.commit()
        sent += 1
    return sent


def step(conn, *, deals: bool = False, notify=None, now: dt.datetime | None = None) -> str:
    """Record what is new and send it: the disclosures and takeover-code filings (the
    data check), or the deal files (the deals job)."""
    from igs.alerts.delivery import TelegramError
    now = now or utc_now()
    found = (record_deals(conn, now) if deals
             else record_disclosures(conn, now) + record_sast(conn, now))
    try:
        sent = alert(conn, notify=notify, now=now)
    except TelegramError as exc:
        conn.rollback()
        return f'{found} promoter trades found; alerts wait for the next run: {exc}'
    return f'{found} promoter trades found, {sent} sent'
