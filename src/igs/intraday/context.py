"""Dated context; institutional ownership is not a report of today's trades."""
from __future__ import annotations

import datetime as dt
from urllib.parse import urlsplit

from igs.timeutil import IST, require_aware, utc_now


def evidence(conn, company_id, symbol, now):
    out = []
    since = now - dt.timedelta(days=3)
    rows = conn.execute('''select a.title,a.url,a.published_at,
            greatest(a.received_at,t.assessed_at,a.published_at),t.tone,t.confidence,t.reason
        from stock_news_tone t join broker_article a using(article_id)
        where t.company_id=%s and a.published_at between %s and %s
          and a.received_at<=%s and t.assessed_at<=%s
        order by a.published_at desc limit 10''', (company_id, since, now, now, now)).fetchall()
    for title, url, published, known, tone, confidence, reason in rows:
        out.append(dict(kind='News', title=title, url=url, published_at=published.isoformat(),
                        known_at=known.isoformat(), detail=reason,
                        direction=(1 if tone > .3 else -1 if tone < -.3 else 0)
                        if confidence >= .7 else 0))
    rows = conn.execute('''select n.title,n.url,n.published_at,
            greatest(n.received_at,a.assessed_at,n.published_at),a.impact,a.confidence,a.rationale
        from geopolitical_assessment a join geopolitical_news n using(news_id)
        where a.company_id=%s and n.published_at between %s and %s
          and n.received_at<=%s and a.assessed_at<=%s
        order by n.published_at desc limit 5''', (company_id, since, now, now, now)).fetchall()
    for title, url, published, known, impact, confidence, reason in rows:
        out.append(dict(kind='Critical news', title=title, url=url,
                        published_at=published.isoformat(), known_at=known.isoformat(),
                        detail=reason, direction=(1 if impact > .3 else -1 if impact < -.3 else 0)
                        if confidence >= .7 else 0))
    rows = conn.execute('''select person_name,side,trade_from,filed_at,ingested_at,xbrl_url,
            person_category from insider_trade where exchange='NSE' and symbol=%s
            and open_market and side in ('buy','sell') and filed_at between %s and %s
            and ingested_at<=%s and trade_from<=%s order by filed_at desc limit 10''',
        (symbol, since, now, now, now.astimezone(IST).date())).fetchall()
    for name, side, day, published, received, url, category in rows:
        out.append(dict(kind='Insider disclosure', title=f'{name}: {side}',
                        url=url, published_at=published.isoformat(),
                        known_at=max(published, received).isoformat(),
                        detail=f'Trade date {day}; {category or "insider"}. '
                               'Disclosed trade, not live order flow.',
                        direction=1 if side == 'buy' else -1))
    rows = conn.execute('''select e.investor,coalesce(w.category,e.category),side,trade_date,
            published_at,received_at,source_url,evidence from intraday_investor_event e
        left join intraday_investor_watch w on w.investor=upper(e.investor)
        where company_id=%s and published_at between %s and %s and received_at<=%s
        order by published_at desc limit 10''', (company_id, since, now, now)).fetchall()
    for name, category, side, day, published, received, url, detail in rows:
        label = 'Large trade (unclassified)' if category == 'large' else category
        out.append(dict(kind=f'{label} disclosure', title=f'{name}: {side}', url=url,
                        published_at=published.isoformat(),
                        known_at=max(published, received).isoformat(),
                        detail=f'Trade date {day}. {detail}',
                        direction=(1 if side == 'buy' else -1) if category != 'large' else 0))
    return out


def add_investor_event(conn, *, company_id, investor, category, side, trade_date,
                       published_at, source_url, evidence, now=None):
    now = require_aware(now or utc_now())
    published_at = require_aware(published_at)
    if not investor.strip() or not evidence.strip():
        raise ValueError('Investor name and source evidence are required')
    if category not in ('prominent', 'FII', 'DII') or side not in ('buy', 'sell'):
        raise ValueError('Invalid investor category or trade side')
    url = urlsplit(source_url)
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('Provide an HTTPS public source URL without credentials')
    if published_at > now or trade_date > published_at.astimezone(IST).date():
        raise ValueError('Trade/publication dates cannot be in the future or out of order')
    conn.execute('''insert into intraday_investor_event(company_id,investor,category,side,
        trade_date,published_at,source_url,evidence) values(%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict do nothing''', (company_id, investor.strip(), category, side,
                                   trade_date, published_at, source_url, evidence.strip()))
    conn.commit()
