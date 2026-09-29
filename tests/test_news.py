"""Automatic news ingestion, matching and AI retries without external requests."""
import datetime as dt
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from igs.config import NewsFeed, load_news
from igs.news import MAX_BYTES, collect_news, match_companies, news_status, parse_feed
from igs.timeutil import utc_now


def rss(url='https://publisher.example/trade', title='Import tariffs disrupt pharma trade'):
    return f'''<rss><channel><item><title>{title}</title><link>{url}</link>
      <pubDate>{format_datetime(utc_now()-dt.timedelta(hours=1))}</pubDate>
      <description><![CDATA[<p>New tariffs may increase pharmaceutical import costs.
      Exporters could face delays.</p>]]></description></item></channel></rss>'''.encode()


def config():
    return load_news().model_copy(update={'feeds': [
        NewsFeed(name='Good feed', url='https://publisher.example/rss'),
        NewsFeed(name='Other feed', url='https://other.example/rss')]})


def test_rss_atom_dates_html_and_xml_protection():
    now = utc_now()
    items, skipped = parse_feed(rss(), now, 21)
    assert len(items) == 1 and skipped == 0 and '<p>' not in items[0]['body']
    atom = f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Tariffs disrupt global trade</title><link rel="self" href="https://wrong.example"/>
        <link rel="alternate" href="https://publisher.example/article"/>
        <published>{(now-dt.timedelta(hours=1)).isoformat()}</published>
        <summary>Trade restrictions increased costs for pharmaceutical importers.</summary>
        </entry></feed>'''.encode()
    assert parse_feed(atom, now, 21)[0][0]['url'] == 'https://publisher.example/article'
    assert not parse_feed(atom.replace(b'<published>', b'<updated>').replace(
        b'</published>', b'</updated>'), now, 21)[0]  # updated != publication
    assert not parse_feed(rss('file:///etc/passwd'), now, 21)[0]
    assert not parse_feed(rss(), now+dt.timedelta(days=22), 21)[0]
    assert not parse_feed(rss(), now-dt.timedelta(days=1), 21)[0]
    for raw in (b'<!DOCTYPE rss><rss/>', b'<html/>', b'x'*(MAX_BYTES+1), b'\x00<rss/>'):
        with pytest.raises(ValueError):
            parse_feed(raw, now, 21)


def test_matching_requires_a_topic_and_prioritizes_watchlist():
    companies = [{'company_id': i, 'symbol': f'PH{i}', 'name': f'Example Pharma {i}',
                  'industry': 'Pharmaceuticals', 'sector': 'Healthcare',
                  'source_url': 'https://nse.example/industry', 'watched': i==2, 'rank': i}
                 for i in (1, 2, 3)]
    cfg = config().model_copy(update={'companies_per_article': 1})
    assert match_companies({'body': 'Ordinary quarterly earnings report'}, companies, cfg) == []
    matched = match_companies({'body': 'New tariffs on exports'}, companies, cfg)
    assert len(matched) == 1 and matched[0]['company_id'] == 2
    assert matched[0]['basis'] == 'industry_proxy'
    assert 'No direct geographic' in matched[0]['description']


@pytest.mark.db
def test_collection_partial_failure_cooldown_dedupe_and_later_matching(db_conn):
    import db_market

    calls = []

    def serve(request):
        calls.append(request.url.host)
        return httpx.Response(503) if request.url.host == 'other.example' else \
            httpx.Response(200, content=rss())

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = collect_news(db_conn, config(), client=client)
        assert result.imported == 1 and result.errors and result.matched == 0
        stored = db_conn.execute('select payload from geopolitical_feed_fetch '
                                 'where http_status=200').fetchone()[0]
        assert b'<rss>' in bytes(stored)
        # A master loaded after collection can bind the already collected articles.
        db_market.load(db_conn)
        again = collect_news(db_conn, config(), client=client)
        assert len(calls) == 2 and again.imported == 0 and again.matched == 1
        assert collect_news(db_conn, config(), client=client, force=True).imported == 0
    assert db_conn.execute('select count(*) from geopolitical_news').fetchone()[0] == 1
    assert news_status(db_conn)['articles'][0]['status'] == 'Awaiting AI'
    assert news_status(db_conn)['feeds'][0]['http_status'] in (200, 503)


@pytest.mark.db
def test_rss_assessment_cap_and_failed_article_does_not_block_others(db_conn):
    import db_market

    from igs.assistant.geopolitical import assess_pending

    db_market.load(db_conn)
    feed = rss().replace(b'</channel>', rss('https://publisher.example/second',
                       'Tariffs raise import prices').split(b'<channel>')[1]
                        .split(b'</channel>')[0]+b'</channel>')
    cfg = config().model_copy(update={'feeds': config().feeds[:1]})
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=feed))
    with httpx.Client(transport=transport) as c:
        assert collect_news(db_conn, cfg, client=c).imported == 2
    import json
    calls = []

    def structured(*args, **kwargs):
        data = json.loads(kwargs['prompt'])
        calls.append(data)
        return {'items': [{'company_id': company['company_id'], 'impact': -0.5,
                          'confidence': 0.99, 'channel': 'trade',
                          'rationale': 'Higher costs could reduce margins this quarter; '
                                       'passing on prices may offset the effect.',
                          'evidence': 'invented evidence' if data['title'].startswith('Import')
                                      else 'New tariffs may increase pharmaceutical import costs.'}
                         for company in data['companies']]}, SimpleNamespace(model='test-model')

    assistant = SimpleNamespace(conn=db_conn, structured=structured)
    assert assess_pending(assistant) > 0
    assert db_conn.execute('select max(confidence) from geopolitical_assessment'
                          ).fetchone()[0] == 0.65
    failure = db_conn.execute('select assessment_attempts,assessment_error '
                             'from geopolitical_news where assessment_error is not null').fetchone()
    assert failure[0] == 1 and failure[1]
    n = len(calls)
    assess_pending(assistant)
    assert len(calls) == n  # due time prevents immediate repeat charges
    for _ in range(3):
        db_conn.execute('update geopolitical_news set next_assessment_at=now()')
        db_conn.commit()
        assess_pending(assistant)
    assert len(calls) == n+2  # three attempts total, not an infinite spending loop
    assert 'Failed (retry limit)' in [a['status'] for a in news_status(db_conn)['articles']]


def test_default_feeds_and_prompt_use_indian_context():
    from igs.assistant.geopolitical import SYSTEM

    cfg = load_news()
    assert len(cfg.feeds) == 3
    assert all(f.url.host == 'economictimes.indiatimes.com' for f in cfg.feeds)
    assert "India's perspective" in SYSTEM and 'INR/USD' in SYSTEM


@pytest.mark.db
def test_nse_company_context_does_not_use_other_exchange_labels(db_conn):
    import db_market

    from igs.news import company_context

    db_market.load(db_conn)
    db_conn.execute('truncate industry_classification')
    db_conn.execute("""insert into announcement
        (exchange,symbol,filed_at,category,subject,source_fetch_id,ingested_at,industry_label)
        select 'BSE','BANK',now(),'other','Different exchange',fetch_id,now(),'Shipping'
        from raw_payload limit 1""")
    rows = company_context(db_conn)
    assert next(r for r in rows if r['symbol']=='BANK')['industry'] != 'Shipping'
