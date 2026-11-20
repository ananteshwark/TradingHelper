"""Prominent institutions' (FII and DII) trades in NSE's bulk and block deal files, on
Telegram as soon as a file is collected (docs/INSTITUTIONAL_DEALS.md).

Who counts is config/institutions.yaml: name fragments collected from AMFI, IRDAI, PFRDA,
the largest foreign portfolio investors and the names in 2013-2026 deal files; then SEBI's
registers of foreign portfolio investors and mutual funds, refreshed every week
(igs.institution_register). Their takeover-code (SAST) filings are sent too. The
"possible impact" line quotes research/institutional_deals: how prices moved after these
institutions' block deals (config/institution_impact.json). It is history, not a forecast.

These classifications are separate from Settings > Investors, which the intraday scan
uses: they change no call or order.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from igs.timeutil import IST

PRICE = 'Trade Price / Wght. Avg. Price'
MIN_EVENTS = 30             # fewer past deals than this: too few to say
SIZES = ((0, 1, 'under 1x'), (1, 5, '1-5x'), (5, math.inf, '5x and more'))
SIZE_WORDS = {'under 1x': "under a day's trading", '1-5x': "1–5 days' trading",
              '5x and more': "5 or more days' trading", 'all': '(any size)'}
GROUP_NOTES = {'Foreign bank or broker': 'its desks often trade for clients or arbitrage'}
DEFAULTS = {'enabled': True, 'types': ['FII', 'DII'], 'min_value_cr': 0.0, 'register': True,
            'register_min_value_cr': 5.0, 'filings': True}


@dataclass(frozen=True)
class Institution:
    name: str
    type: str
    group: str
    fragments: tuple[str, ...]


def _norm(text) -> str:
    return re.sub(r'[^A-Z0-9]', '', str(text).upper())


@lru_cache(maxsize=4)
def register(path: Path | None = None) -> tuple[Institution, ...]:
    """config/institutions.yaml, in its order: the first entry that matches wins."""
    import yaml

    from igs.config import config_dir
    entries = yaml.safe_load((path or config_dir() / 'institutions.yaml')
                             .read_text(encoding='utf-8'))['institutions']
    out = []
    for e in entries:
        frags = tuple(_norm(m) for m in e.get('match') or ())
        if e.get('type') not in ('FII', 'DII') or not e.get('name') or not all(frags) \
                or not frags:
            raise ValueError(f'config/institutions.yaml: bad entry {e!r}')
        out.append(Institution(e['name'], e['type'], e.get('group') or '', frags))
    return tuple(out)


def identify(client: str, reg: tuple[Institution, ...] | None = None) -> Institution | None:
    """The institution a deal file's client name belongs to, if it is in the register."""
    name = _norm(client)
    for inst in reg if reg is not None else register():
        if any(f in name for f in inst.fragments):
            return inst
    return None


def rule(cfg=None) -> dict:
    """rules.institution_deals in config/alerts.yaml over DEFAULTS."""
    if cfg is None:
        from igs.config import load_alerts
        cfg = load_alerts()
    return {**DEFAULTS, **(cfg.rules.get('institution_deals') or {})}


def parse(body: str, now: dt.datetime, reg: tuple[Institution, ...] | None = None,
          extra=None) -> list[dict]:
    """The registered institutions' net trades in one NSE bulk or block CSV, from the last
    seven days: one per institution, stock and day, its client names added up, at the
    volume-weighted price of the net side. `extra`, given a client name the hand list
    doesn't know, returns the institution SEBI's registers say it is, or None."""
    today = now.astimezone(IST).date()
    trades = defaultdict(lambda: {'buy': [Decimal(0), Decimal(0)],
                                  'sell': [Decimal(0), Decimal(0)], 'clients': set()})
    for row in csv.DictReader(io.StringIO(body.lstrip('﻿'))):
        date = (row.get('Date') or '').strip()
        if not date or date.upper() == 'NO RECORDS':
            continue
        day = dt.datetime.strptime(date, '%d-%b-%Y').date()  # noqa: DTZ007
        if not today - dt.timedelta(days=7) <= day <= today:
            continue
        inst = identify(row['Client Name'], reg)
        if inst is None and extra is not None:
            inst = extra(row['Client Name'])
        if inst is None:
            continue
        side = row['Buy/Sell'].strip().lower()
        qty = Decimal(row['Quantity Traded'].replace(',', '').strip())
        price = Decimal(row[PRICE].replace(',', '').strip())
        if side not in ('buy', 'sell') or not qty > 0 or not price > 0:
            raise ValueError('Invalid NSE deal row')
        key = (day, row['Symbol'].strip().upper(), inst)
        t = trades[key]
        t[side][0] += qty
        t[side][1] += qty * price
        t['clients'].add(row['Client Name'].strip().upper())
        t['security'] = (row.get('Security Name') or '').strip()
    out = []
    for (day, symbol, inst), t in trades.items():
        net = t['buy'][0] - t['sell'][0]
        if not net:
            continue
        side = 'buy' if net > 0 else 'sell'
        qty, value = t[side]
        price = value / qty
        out.append({'day': day, 'symbol': symbol, 'security': t['security'],
                    'institution': inst, 'clients': '; '.join(sorted(t['clients'])),
                    'side': side, 'quantity': abs(net), 'price': price,
                    'value': abs(net) * price})
    return out


def _company(conn, symbol: str, day: dt.date) -> int | None:
    row = conn.execute('''select s.company_id from security_identifier i
        join security s using (security_id) where id_type='NSE_SYMBOL' and id_value=%s
        and valid_from<=%s and (valid_to is null or valid_to>%s)''',
                       (symbol, day, day)).fetchone()
    return row[0] if row else None


def _shares(conn, company_id: int, day: dt.date) -> float | None:
    """Total shares from the latest shareholding filing for a quarter ending by `day`."""
    row = conn.execute('''select shares::float8 from shareholding where company_id=%s
        and category='total' and period_end<=%s and shares>0
        order by period_end desc, filed_at desc limit 1''', (company_id, day)).fetchone()
    return row[0] if row else None


def _adv(conn, company_id: int, day: dt.date) -> float | None:
    """Average daily traded value over the 20 sessions before `day`."""
    from igs.assistant.calls import _price_rows
    rows = [r for r in _price_rows(conn, company_id, day - dt.timedelta(days=45),
                                   day - dt.timedelta(days=1)) if r[3]]
    values = [r[3] for r in rows[-20:]]
    return sum(values) / len(values) if len(values) >= 10 else None


def record(conn, kind: str, body: str, source_url: str, now: dt.datetime) -> int:
    """Keep the registered institutions' new trades from one collected file. A trade
    already seen (same file kind, day, stock and institution) is left as first seen."""
    added = 0
    extra = None
    if rule()['register']:
        from igs.institution_register import lookup
        extra = lookup(conn)
    for d in parse(body, now, extra=extra):
        inst: Institution = d['institution']
        company_id = _company(conn, d['symbol'], d['day'])
        shares = _shares(conn, company_id, d['day']) if company_id else None
        adv = _adv(conn, company_id, d['day']) if company_id else None
        row = conn.execute('''insert into institution_deal(kind, trade_date, symbol,
                security_name, company_id, institution, investor_type, investor_group,
                client_names, side, quantity, price, value_inr, shares_outstanding,
                pct_of_shares, adv_inr, source_url, first_seen_at, from_register)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict do nothing returning deal_id''',
            (kind, d['day'], d['symbol'], d['security'] or None, company_id, inst.name,
             inst.type, inst.group, d['clients'], d['side'], d['quantity'], d['price'],
             d['value'], shares,
             float(d['quantity']) / shares * 100 if shares else None, adv, source_url,
             now, getattr(inst, 'source', 'config') == 'register')).fetchone()
        added += bool(row)
    return added


@lru_cache(maxsize=2)
def impact_table(path: Path | None = None) -> dict:
    from igs.config import config_dir
    return json.loads((path or config_dir() / 'institution_impact.json')
                      .read_text(encoding='utf-8'))


def size_group(adv_multiple: float | None) -> str | None:
    if adv_multiple is None:
        return None
    return next(label for lo, hi, label in SIZES if lo <= adv_multiple < hi)


def impact(investor_type: str, side: str, adv_multiple: float | None,
           table: dict | None = None) -> str:
    """The 'possible impact' line: how prices moved after past block deals of the same
    kind and size (all sizes if that group had too few)."""
    groups = (table or impact_table()).get(investor_type, {}).get(side, {})
    size = size_group(adv_multiple)
    s = groups.get(size) if size else None
    if not s or s['n'] < MIN_EVENTS:
        size, s = 'all', groups.get('all')
    who = f"{investor_type}s {'bought' if side == 'buy' else 'sold'}"
    if not s or s['n'] < MIN_EVENTS:
        n = s['n'] if s else 0
        return (f'Possible impact: too few past cases to say ({n} block deals in which '
                f'{who}, 2013–2026).')
    d0, d1, d21 = s['d0'], s['d1'], s['d21']
    if d21['t'] >= 2 and d21['mean'] > 0:
        after = 'such stocks kept beating the market, though the cases are few'
    elif d21['t'] <= -2 and d21['mean'] < 0:
        after = 'such stocks lagged the market'
    else:
        after = 'there was no reliable further move'
    return (f"Possible impact (history, not a forecast): after {s['n']} block deals in which "
            f"{who} {SIZE_WORDS[size]}, 2013–2026, the stock beat the market by "
            f"{d0['mean'] * 100:+.1f}% on the deal day on average (already in the price). "
            f"After it: {d1['mean'] * 100:+.1f}% the next session and "
            f"{d21['mean'] * 100:+.1f}% over 21 sessions (median {d21['median'] * 100:+.1f}%,"
            f" t {d21['t']:.1f}), so {after}.")


def _group(n: float) -> str:
    """Indian digit grouping: 1234567 -> '12,34,567'."""
    s = str(int(round(n)))
    if len(s) <= 3:
        return s
    head, groups = s[:-3], []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ','.join([head, *groups, s[-3:]])


def message(conn, deal: dict, table: dict | None = None) -> str:
    """The Telegram message for one stored deal (a row of institution_deal as a dict)."""
    from igs.assistant.calls import _price_rows
    action = 'BOUGHT' if deal['side'] == 'buy' else 'SOLD'
    stock = (f"{deal['security_name']} ({deal['symbol']})" if deal.get('security_name')
             else deal['symbol'])
    group = deal['investor_group'] + (f": {GROUP_NOTES[deal['investor_group']]}"
                                      if deal['investor_group'] in GROUP_NOTES else '')
    if deal.get('from_register'):
        group += ", on SEBI's register"
    value = float(deal['value_inr'])
    lines = [f"{deal['investor_type']} {action}: {stock}",
             f"{deal['institution']} ({group})",
             f"NSE {deal['kind']} deal on {deal['trade_date']:%d %b %Y}: "
             f"{'bought' if deal['side'] == 'buy' else 'sold'} "
             f"{_group(float(deal['quantity']))} shares at ₹{float(deal['price']):,.2f} = "
             f"₹{value / 1e7:,.1f} crore"]
    size = []
    if deal.get('pct_of_shares') is not None:
        size.append(f"{float(deal['pct_of_shares']):.2f}% of the company's shares")
    adv_x = value / float(deal['adv_inr']) if deal.get('adv_inr') else None
    if adv_x is not None:
        size.append(f"{adv_x:.1f}× its average daily trading (20 sessions)")
    if size:
        lines.append('; '.join(size))
    if deal.get('company_id'):
        day = [r for r in _price_rows(conn, deal['company_id'], deal['trade_date'],
                                      deal['trade_date']) if r[2]]
        if day:
            lines.append(f"On the day the stock closed {(day[0][1] / day[0][2] - 1) * 100:+.1f}%"
                         f" at ₹{day[0][1]:,.2f}.")
    earlier = conn.execute('''select side, trade_date, pct_of_shares::float8,
            value_inr::float8 from institution_deal where institution=%s and symbol=%s
            and deal_id<>%s and trade_date<=%s order by trade_date desc limit 3''',
                           (deal['institution'], deal['symbol'], deal['deal_id'],
                            deal['trade_date'])).fetchall()
    if earlier:
        lines.append('Earlier here since collection began: ' + '; '.join(
            f"{'bought' if s == 'buy' else 'sold'} "
            + (f'{p:.2f}% of shares' if p is not None else f'₹{v / 1e7:,.1f} crore')
            + f' on {d:%d %b %Y}' for s, d, p, v in earlier) + '.')
    lines.append(impact(deal['investor_type'], deal['side'], adv_x, table))
    lines.append(f"Client name: {deal['client_names']}")
    return '\n'.join(lines)


def alert(conn, notify=None, cfg=None, table: dict | None = None,
          now: dt.datetime | None = None) -> int:
    """Send each new deal once, oldest first; returns how many were sent. A deal stays
    unsent (and is retried on the next collection) while Telegram is unavailable; one
    first seen more than three days ago is no longer sent, and one already sent from the
    other file (bulk or block) is marked, not sent again."""
    from igs.alerts.delivery import send_deals
    from igs.timeutil import utc_now
    r = rule(cfg)
    if not r['enabled']:
        return 0
    notify = notify or send_deals
    rows = conn.execute('''select * from institution_deal where alerted_at is null
            and first_seen_at > %s - interval '3 days' and investor_type = any(%s)
            and value_inr >= case when from_register then %s else %s end
            order by first_seen_at, deal_id''',
                        (now or utc_now(), list(r['types']),
                         max(float(r['min_value_cr']), float(r['register_min_value_cr'])) * 1e7,
                         float(r['min_value_cr']) * 1e7))
    columns = [c.name for c in rows.description]
    sent = 0
    for values in rows.fetchall():
        deal = dict(zip(columns, values, strict=True))
        # The same trade in both the bulk and the block file is sent once.
        same = conn.execute('''select kind from institution_deal where trade_date=%s
                and symbol=%s and institution=%s and side=%s and quantity=%s and kind<>%s
                and alerted_at is not null''',
                            (deal['trade_date'], deal['symbol'], deal['institution'],
                             deal['side'], deal['quantity'], deal['kind'])).fetchone()
        if same:
            conn.execute('update institution_deal set alerted_at=now(), alert_text=%s '
                         'where deal_id=%s', (f'Not sent: the same trade as the {same[0]} '
                                              'deal already sent.', deal['deal_id']))
            conn.commit()
            continue
        text = message(conn, deal, table)
        if not notify(text):
            break
        conn.execute('update institution_deal set alerted_at=now(), alert_text=%s '
                     'where deal_id=%s', (text, deal['deal_id']))
        conn.commit()
        sent += 1
    return sent


def outcomes(conn, deal: dict) -> dict:
    """The stock's move against the Nifty 500 from the deal day's close (the first close
    after the deal is known) to the next session's and the 21st session's, in percent;
    a window not yet complete is left out."""
    from igs.assistant.calls import _index_series, _price_rows, price_index
    if not deal.get('company_id'):
        return {}
    day = deal['trade_date']
    rows = _price_rows(conn, deal['company_id'], day, day + dt.timedelta(days=45))
    levels = price_index(rows)
    if not levels or levels[0][0] != day:
        return {}
    nifty = dict(_index_series(conn, day, day + dt.timedelta(days=45)))
    out = {}
    for n, key in ((1, 'next session'), (21, '21 sessions')):
        if len(levels) > n and day in nifty and levels[n][0] in nifty:
            own = levels[n][1] / levels[0][1] - 1
            bench = nifty[levels[n][0]] / nifty[day] - 1
            out[key] = round((own - bench) * 100, 2)
    return out


# --------------------------------------------------------------------------- filings

# "<who> has submitted to the Exchange a copy of Disclosure under Regulation 29(2) ...":
# the filer is the text before the verb.
_FILER = re.compile(r'^\s*(?:M/S\.?\s*)?(.+?)\s+(?:has|have)\s+(?:submitted|informed|filed|made)\b',
                    re.IGNORECASE)


def _filer(subject: str, extra) -> Institution | None:
    """The institution an announcement's subject names as its filer, if any."""
    m = _FILER.search(subject)
    who = m.group(1) if m else ''
    if who:
        inst = identify(who)
        if inst is None and extra is not None:
            inst = extra(who)
        return inst
    return identify(subject)


def record_filings(conn, now: dt.datetime | None = None) -> int:
    """Takeover-code (SAST Regulation 29) filings by an FII or a DII, in announcements loaded
    in the last three days and not recorded yet."""
    from psycopg.rows import dict_row

    from igs.promoter_trades import NOT_A_TRADE, SAST_TEXT, _side_in_text
    from igs.timeutil import utc_now
    now = now or utc_now()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            select a.ann_id, a.exchange, a.symbol, a.company_id, c.name, a.filed_at,
                   a.category, a.subject, a.attachment_url
            from announcement a left join company c using (company_id)
            where a.ingested_at > %s
              and not exists (select 1 from institution_filing f
                              where (f.exchange, f.symbol, f.filed_at, f.subject)
                                  = (a.exchange, a.symbol, a.filed_at, a.subject))
            order by a.filed_at, a.ann_id""", (now - dt.timedelta(days=3),))
        rows = [r for r in cur.fetchall()
                if SAST_TEXT.search(f"{r['category']} {r['subject']}")
                and not NOT_A_TRADE.search(r['subject'])]
    if not rows:
        return 0
    extra = None
    if rule()['register']:
        from igs.institution_register import lookup
        extra = lookup(conn)
    added = 0
    from igs.institution_register import norm
    for r in rows:
        inst = _filer(r['subject'], extra)
        if inst is None:
            continue
        # The company filing about its own shares (an insurer listed itself, say) names no
        # institution that traded.
        own = norm(r['name'])
        if own and own in re.sub(r'[^A-Z0-9]', '', r['subject'].upper())[:len(own) + 40]:
            continue
        got = conn.execute("""insert into institution_filing (exchange, company_id, symbol,
                company_name, institution, investor_type, investor_group, side, subject, link,
                filed_at, first_seen_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (exchange, symbol, filed_at, subject) do nothing
            returning filing_id""",
                           (r['exchange'], r['company_id'], r['symbol'], r['name'], inst.name,
                            inst.type, inst.group, _side_in_text(r['subject']), r['subject'],
                            r['attachment_url'], r['filed_at'], now)).fetchone()
        added += got is not None
    conn.commit()
    return added


def filing_message(f: dict) -> str:
    stock = f"{f['company_name']} ({f['symbol']})" if f.get('company_name') else f['symbol']
    lines = [f"{f['investor_type']} FILING (takeover code): {stock}",
             f"{f['institution']} ({f['investor_group']}) filed under SEBI's takeover code "
             f"(Regulation 29) on {f['filed_at'].astimezone(IST):%d %b %Y %H:%M} IST:",
             f['subject']]
    if f['side'] != 'unknown':
        lines.append(f"It reports shares {'acquired' if f['side'] == 'buy' else 'disposed of'};"
                     " the figures are in the filing.")
    else:
        lines.append('The figures are in the filing: a holding of 5% or more, or a change '
                     'of 2% or more.')
    if f.get('link'):
        lines.append(f['link'])
    return '\n'.join(lines)


def alert_filings(conn, notify=None, cfg=None, now: dt.datetime | None = None) -> int:
    """Send each recorded filing once, oldest first; one stays unsent while Telegram is
    unavailable, and one first seen more than three days ago is no longer sent."""
    from igs.timeutil import utc_now
    r = rule(cfg)
    if not r['enabled'] or not r['filings']:
        return 0
    if notify is None:
        from igs.alerts.delivery import send_deals as notify
    cur = conn.execute('''select * from institution_filing where alerted_at is null
            and first_seen_at > %s - interval '3 days' and investor_type = any(%s)
            order by first_seen_at, filing_id''', (now or utc_now(), list(r['types'])))
    columns = [c.name for c in cur.description]
    sent = 0
    for values in cur.fetchall():
        f = dict(zip(columns, values, strict=True))
        text = filing_message(f)
        if not notify(text):
            break
        conn.execute('update institution_filing set alerted_at = now(), alert_text = %s '
                     'where filing_id = %s', (text, f['filing_id']))
        conn.commit()
        sent += 1
    return sent


def filings_step(conn, notify=None, now: dt.datetime | None = None) -> str:
    """The data check's part: record new filings by FIIs and DIIs and send them."""
    from igs.alerts.delivery import TelegramError
    found = record_filings(conn, now)
    try:
        sent = alert_filings(conn, notify=notify, now=now)
    except TelegramError as exc:
        conn.rollback()
        return f'{found} institution filings found; alerts wait for the next run: {exc}'
    return f'{found} institution filings found, {sent} sent'
