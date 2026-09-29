"""Bounded RSS/Atom collection with publisher attribution and auditable raw responses."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

import httpx
from pydantic import HttpUrl, TypeAdapter

from igs.config import NewsConfig, load_news, load_scoring
from igs.timeutil import utc_now

MAX_BYTES = 2_000_000
LOCK_KEY = 7215460013
URL = TypeAdapter(HttpUrl)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(text: str) -> str:
    parser = _Text()
    parser.feed(text)
    return ' '.join(' '.join(parser.parts).split())


def parse_feed(payload: bytes, now: dt.datetime, days: int) -> tuple[list[dict], int]:
    if len(payload) > MAX_BYTES or b'\x00' in payload or any(
            x in payload.upper() for x in (b'<!DOCTYPE', b'<!ENTITY')):
        raise ValueError('feed is oversized or contains unsupported XML declarations')
    root = ET.fromstring(payload)
    if root.tag.rsplit('}', 1)[-1] not in ('rss', 'feed', 'RDF'):
        raise ValueError('response is not RSS or Atom')
    articles, skipped = [], 0
    for item in root.iter():
        if item.tag.rsplit('}', 1)[-1] not in ('item', 'entry'):
            continue
        fields = {c.tag.rsplit('}', 1)[-1]: ''.join(c.itertext()) for c in item}
        links = [c for c in item if c.tag.rsplit('}', 1)[-1] == 'link']
        link = next((c.get('href') for c in links if c.get('href')
                     and c.get('rel', 'alternate') == 'alternate'), fields.get('link', ''))
        try:
            url = str(URL.validate_python(link))
            stamp = fields.get('pubDate') or fields.get('published') or fields.get('date')
            if not stamp:
                raise ValueError('missing publication timestamp')
            try:
                published = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            except ValueError:
                published = parsedate_to_datetime(stamp)
            if (published.tzinfo is None or published > now
                    or published < now-dt.timedelta(days=days)):
                raise ValueError('undated, stale or future article')
            title = plain(fields.get('title', ''))[:500]
            summary = plain(fields.get('description') or fields.get('summary')
                            or fields.get('content') or '')[:10000]
            if len(title) < 10 or len(summary) < 30:
                raise ValueError('insufficient feed text')
            articles.append({'url': url, 'title': title, 'body': title+'\n'+summary,
                             'published_at': published})
        except (ValueError, TypeError, OverflowError):
            skipped += 1
    return sorted(articles, key=lambda a: a['published_at'], reverse=True), skipped


def company_context(conn) -> list[dict]:
    """Current observed labels; no country, revenue or supplier exposures are invented."""
    rows = conn.execute("""select distinct on (c.company_id) c.company_id, c.name,
        i.id_value, coalesce(ic.industry, a.industry_label, ''), coalesce(ic.sector, ''),
        coalesce(p.url, a.attachment_url, 'https://www.nseindia.com'),
        exists(select 1 from watchlist w where w.company_id=c.company_id), r.rank
        from company c join security s using(company_id)
        join security_identifier i on i.security_id=s.security_id and i.id_type='NSE_SYMBOL'
            and i.valid_from<=current_date and (i.valid_to is null or i.valid_to>current_date)
        left join lateral (select * from industry_classification ic
            where ic.company_id=c.company_id and ic.valid_from<=current_date
            order by valid_from desc limit 1) ic on true
        left join raw_payload p on p.fetch_id=ic.source_fetch_id
        left join lateral (select a.industry_label,a.attachment_url from announcement a
            where a.exchange='NSE' and a.symbol=i.id_value and a.filed_at<=now()
            and a.industry_label is not null
            and a.filed_at::date>=i.valid_from
            order by a.filed_at desc limit 1) a on true
        left join score_result r on r.company_id=c.company_id
            and r.run_id=(select max(run_id) from score_run)
        order by c.company_id, i.id_value""").fetchall()
    names = ('company_id', 'name', 'symbol', 'industry', 'sector', 'source_url', 'watched', 'rank')
    return [dict(zip(names, row, strict=True)) for row in rows]


def match_companies(article: dict, companies: list[dict], cfg: NewsConfig) -> list[dict]:
    text = article['body'].casefold()
    topics = [name for name, rule in cfg.topics.items()
              if any(re.search(r'\b'+re.escape(term.casefold())+r'\b', text)
                     for term in rule.terms)]
    if not topics:
        return []
    matches = []
    for company in companies:
        label = f"{company['industry']} {company['sector']}".casefold()
        name = re.sub(r'\b(limited|ltd|pvt)\b\.?', '', company['name'].casefold()).strip()
        direct = len(name) >= 8 and re.search(r'\b'+re.escape(name)+r'\b', text) is not None
        related = [t for t in topics if any(word.casefold() in label
                                            for word in cfg.topics[t].industries)]
        if not direct and not related:
            continue
        description = (f"Observed industry: {company['industry'] or 'unavailable'}; "
                       f"sector: {company['sector'] or 'unavailable'}. "
                       + ('Company name appears in the feed summary. ' if direct else
                          'Industry-level candidate for '+', '.join(related)+'. ')
                       + 'No direct geographic, revenue or supplier exposure has been verified.')
        matches.append((not direct, not company['watched'], company['rank'] or 10**9,
                        company['company_id'], {
                            'company_id': company['company_id'], 'symbol': company['symbol'],
                            'description': description, 'source_url': company['source_url'],
                            'basis': 'feed_name_match' if direct else 'industry_proxy'}))
    return [m[-1] for m in sorted(matches, key=lambda m: m[:4])[:cfg.companies_per_article]]


def bind_pending(conn, cfg: NewsConfig | None = None) -> int:
    cfg = cfg or load_news()
    companies = company_context(conn)
    articles = conn.execute("""select news_id,body from geopolitical_news
        where intake='rss' and companies='[]'::jsonb
        and published_at>=now() - %s*interval '1 day'""",
        (load_scoring().geopolitical.max_age_days,)).fetchall()
    matched = 0
    for news_id, body in articles:
        context = match_companies({'body': body}, companies, cfg)
        if context:
            cur = conn.execute("""update geopolitical_news set companies=%s
                where news_id=%s and companies='[]'::jsonb""", (json.dumps(context), news_id))
            matched += cur.rowcount
    return matched


def prune_news(conn, days: int, max_age_days: int) -> tuple[int, int]:
    """Delete feed articles published more than `days` ago that no rating used, and feed
    responses older than that which no remaining article came from. Returns (articles,
    responses) deleted.

    An article no rating used has no AI assessment: no company matched, or the assessment
    never ran or failed. Articles are assessed only within `max_age_days` of publication,
    so past that they stay unused for good. Assessed articles are kept, even at zero impact:
    they were part of past ratings, and re-scoring a past date must see them. Articles
    imported by hand are the user's own and are kept."""
    if days <= max_age_days:
        raise ValueError(f'news is deleted after {days} days, within the {max_age_days}-day '
                         'assessment window; set delete_unassessed_after_days higher')
    with conn.transaction():
        articles = conn.execute("""delete from geopolitical_news n
            where n.intake = 'rss' and n.published_at < now() - %s * interval '1 day'
              and not exists (select 1 from geopolitical_assessment a
                              where a.news_id = n.news_id)""", (days,)).rowcount
        responses = conn.execute("""delete from geopolitical_feed_fetch f
            where f.fetched_at < now() - %s * interval '1 day'
              and not exists (select 1 from geopolitical_news n
                              where n.feed_fetch_id = f.fetch_id)""", (days,)).rowcount
    return articles, responses


@dataclass
class Collection:
    imported: int = 0
    skipped: int = 0
    matched: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self):
        return (f'{self.imported} new articles, {self.matched} articles matched to companies, '
                f'{self.skipped} skipped, {self.deleted} old unused articles deleted; '
                + ('; '.join(self.errors) or 'no feed errors'))


def collect_news(conn, cfg: NewsConfig | None = None, *, force=False,
                 client: httpx.Client | None = None) -> Collection:
    cfg = cfg or load_news()
    out = Collection()
    if not cfg.enabled or not load_scoring().geopolitical.enabled:
        return out
    if not conn.execute('select pg_try_advisory_lock(%s)', (LOCK_KEY,)).fetchone()[0]:
        conn.commit()
        out.errors.append('another news collection is running')
        return out
    conn.commit()
    own_client = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False,
                                    headers={'User-Agent': 'TradingHelper-RSS/1.0'})
    try:
        for feed in cfg.feeds:
            url = str(feed.url)
            recent = conn.execute("""select exists(select 1 from geopolitical_feed_fetch
                where feed_url=%s and fetched_at>now()-%s*interval '1 minute')""",
                (url, cfg.min_interval_minutes)).fetchone()[0]
            conn.commit()
            if recent and not force:
                continue
            status, payload, error, imported, skipped = None, b'', None, 0, 0
            try:
                with client.stream('GET', url) as response:
                    status = response.status_code
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError('feed exceeds 2 MB limit')
                        chunks.append(chunk)
                    payload = b''.join(chunks)
                articles, skipped = parse_feed(payload, utc_now(),
                                                load_scoring().geopolitical.max_age_days)
            except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
                # Do not store exception URLs: custom feeds can contain access tokens.
                error = f'{type(exc).__name__}: feed fetch or parse failed'
                articles = []
                out.errors.append(f'{feed.name}: {error}')
            with conn.transaction():
                fetch_id = conn.execute("""insert into geopolitical_feed_fetch
                    (feed_name,feed_url,http_status,payload,error) values (%s,%s,%s,%s,%s)
                    returning fetch_id""", (feed.name,url,status,payload,error)).fetchone()[0]
                for article in articles[:cfg.max_items_per_feed]:
                    digest = hashlib.sha256(' '.join(article['body'].split()).encode()).hexdigest()
                    cur = conn.execute("""insert into geopolitical_news
                        (url,title,body,published_at,content_hash,companies,intake,feed_fetch_id)
                        values (%s,%s,%s,%s,%s,'[]','rss',%s) on conflict do nothing""",
                        (article['url'],article['title'],article['body'],article['published_at'],
                         digest,fetch_id))
                    imported += cur.rowcount
                    skipped += 1-cur.rowcount
                skipped += max(0, len(articles)-cfg.max_items_per_feed)
                conn.execute('update geopolitical_feed_fetch set imported=%s,skipped=%s '
                             'where fetch_id=%s', (imported,skipped,fetch_id))
            out.imported += imported
            out.skipped += skipped
        out.matched = bind_pending(conn, cfg)
        conn.commit()
        out.deleted = prune_news(conn, cfg.delete_unassessed_after_days,
                                 load_scoring().geopolitical.max_age_days)[0]
        return out
    finally:
        conn.rollback()
        conn.execute('select pg_advisory_unlock(%s)', (LOCK_KEY,))
        conn.commit()
        if own_client:
            client.close()


def collection_step(conn) -> str:
    report = collect_news(conn)
    if report.errors:
        raise RuntimeError(str(report))
    return str(report)


def news_status(conn) -> dict:
    def rows(sql):
        cur = conn.execute(sql)
        return [dict(zip([d.name for d in cur.description], row, strict=True))
                for row in cur.fetchall()]
    return {'feeds': rows('''select distinct on (feed_url) feed_name, fetched_at,
                http_status, imported, skipped, error from geopolitical_feed_fetch
                order by feed_url, fetched_at desc, fetch_id desc'''),
            'articles': rows('''select n.title,n.url,n.published_at,n.intake,
                jsonb_array_length(n.companies) as matched_companies,
                case when exists(select 1 from geopolitical_assessment a where a.news_id=n.news_id)
                     then 'Assessed' when n.assessment_error is not null
                     then case when n.assessment_attempts>=3 and n.intake='rss'
                               then 'Failed (retry limit)' else 'Retry pending' end
                     when n.companies='[]'::jsonb then 'No company match'
                     else 'Awaiting AI' end as status,
                n.assessment_error
                from geopolitical_news n order by n.received_at desc,n.news_id desc limit 30''')}
