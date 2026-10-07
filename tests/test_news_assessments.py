import datetime as dt
import os

import pytest
from streamlit.testing.v1 import AppTest

from igs.news import assessment_results
from igs.timeutil import IST


@pytest.mark.db
def test_both_news_pipelines_display_stored_reasoning_and_confidence(db_conn, monkeypatch):
    stamp = dt.datetime(2026, 10, 4, 10, tzinfo=IST)
    cid = db_conn.execute("insert into company(name) values('Example Company') returning company_id"
                          ).fetchone()[0]
    nid = db_conn.execute('''insert into geopolitical_news(url,title,body,published_at,
        content_hash,companies) values('https://example.com/geo','Energy headline',
        'Energy costs could rise for importers',%s,'geo-test','[]') returning news_id''',
        (stamp,)).fetchone()[0]
    db_conn.execute('''insert into geopolitical_assessment(news_id,company_id,impact,confidence,
        rationale,evidence,channel,model,prompt_version,assessed_at)
        values(%s,%s,-0.4,0.65,'Higher input costs may reduce margins; demand is uncertain.',
        'Energy costs could rise','energy','stored-model','geo-test',%s)''', (nid, cid, stamp))
    aid = db_conn.execute('''insert into broker_article(url,title,body,published_at,feed_name,
        candidate) values('https://example.com/stock','Orders headline','Orders rose this quarter',
        %s,'Test feed',false) returning article_id''', (stamp,)).fetchone()[0]
    db_conn.execute('''insert into stock_news_tone(article_id,company_text,tone,confidence,
        reason,quote,model,prompt_version,assessed_at) values(%s,'Unmatched Company',0.7,0.8,
        'The article describes stronger orders.','Orders rose this quarter',
        'sentiment-model','tone-test',%s)''', (aid, stamp+dt.timedelta(minutes=1)))
    db_conn.commit()
    results = assessment_results(db_conn)
    assert len(results) == 2
    assert results[0]['kind'] == 'Stock news sentiment'
    assert results[0]['company_id'] is None
    assert results[1]['confidence'] == .65
    assert len(assessment_results(db_conn, 1)) == 1
    assert len(assessment_results(db_conn, kind='Stock news sentiment')) == 1
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    from igs.ui.news_assessments import load
    load.clear()
    at = AppTest.from_string('''from igs.ui.news_assessments import render
from igs.db import connect
render(connect(autocommit=True))''').run()
    assert not at.exception
    assert any('The article describes stronger orders.' == t.value for t in at.text)
    assert any('not matched' in i.value for i in at.info)
    at.selectbox(key='news_assessment_type').set_value('Geopolitical impact').run()
    assert not at.exception
    assert any(m.value == '65%' for m in at.metric)
    assert any('Higher input costs' in t.value for t in at.text)
    assert any(t.value == 'Energy costs could rise' for t in at.text)


@pytest.mark.db
def test_empty_assessment_view_is_explicit(db_conn, monkeypatch):
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    from igs.ui.news_assessments import load
    load.clear()
    at = AppTest.from_string('''from igs.ui.news_assessments import render
from igs.db import connect
render(connect(autocommit=True))''').run()
    assert not at.exception
    assert any('No AI news assessments stored yet' in item.value for item in at.info)
