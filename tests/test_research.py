"""Research additions: exact arithmetic, missing data and dated evidence boundaries."""
import datetime as dt
import json
from types import SimpleNamespace

import polars as pl
import pytest
from test_factors import AS_OF, QS, _dataset, _filed, run

from igs.config import load_scoring
from igs.forward import document_text, validate_claims
from igs.pit import PitDataset
from igs.sentiment import _news_signals


def research_dataset():
    ds = _dataset()
    rows = []
    def add(cid, date, kind, concept, value):
        rows.append({'fact_id': 10000+len(rows), 'filing_id': 10000+len(rows),
                     'company_id': cid, 'statement_basis': 'consolidated', 'period_end': date,
                     'period_type': kind, 'concept': concept, 'value': float(value),
                     'filed_at': _filed(date).astimezone(dt.UTC)})
    for date, eps in ((QS[-5], 2), (QS[-1], 3)):
        add(1,date,'Q','eps_diluted',eps)
        add(1,date,'Q','eps_basic',eps*1.25)
        add(1,date,'Q','face_value',10)
    for concept, value in {'cfo': 120, 'capex': -20, 'revenue': 1000, 'pat': 100}.items():
        add(1,QS[-1],'FY',concept,value)
    for date, advance in ((QS[-5],100), (QS[-1],120)):
        add(2,date,'INSTANT','advances',advance)
    add(2,QS[-1],'Q','gnpa_pct',2.5)
    add(2,QS[-1],'Q','nnpa_pct',0.8)
    tables = dict(ds.tables)
    added = PitDataset.from_frames(facts=pl.DataFrame(rows)).tables['facts']
    tables['facts'] = pl.concat([tables['facts'], added], how='diagonal_relaxed')
    tables['corporate_actions'] = tables['corporate_actions'].clear()
    return PitDataset(tables)


@pytest.mark.parametrize('metric,cid,expected', [
    ('eps_diluted_yoy',1,.5), ('eps_dilution_pct',1,.2), ('cash_profit_1y',1,1.2),
    ('free_cash_flow_margin',1,.1), ('capex_revenue',1,.02),
    ('advances_yoy',2,.2), ('gnpa_pct',2,2.5), ('nnpa_pct',2,.8),
])
def test_research_exact_values(metric,cid,expected):
    result = run(research_dataset(),metric)[cid]
    assert result['value'] == pytest.approx(expected), result
    assert result['source_fact_ids']


def test_missing_cash_flow_and_bank_applicability():
    ds = _dataset()
    assert run(ds,'cash_profit_1y')[1]['value'] is None
    assert run(ds,'cash_profit_1y')[2]['status'] == 'not_applicable'
    assert run(ds,'gnpa_pct')[1]['status'] == 'not_applicable'


def test_volume_excludes_event_period_and_uses_prior_twenty_sessions():
    ds = _dataset()
    assert run(ds,'relative_volume_20d')[1]['value'] is None
    tables = dict(ds.tables)
    tables['corporate_actions'] = tables['corporate_actions'].clear()
    prices = tables['prices']
    tables['prices'] = prices.with_columns(
        pl.when(pl.col('trade_date') == prices['trade_date'].max())
          .then(3000).otherwise(pl.col('volume')).alias('volume'))
    assert run(PitDataset(tables),'relative_volume_20d')[1]['value'] == 3
    assert run(PitDataset(tables),'up_down_volume_20d')[1]['value'] <= 1


def test_claim_requires_verbatim_quote_number_and_publication_consistency():
    claim = dict(metric='capacity', kind='guidance', value=100, unit='MW', scope='Plant A',
                 period_end='2027-03-31', quote='Plant A will reach 100 MW by March 2027.',
                 confidence=.9)
    published = dt.datetime(2026,10,1,tzinfo=dt.UTC)
    assert len(validate_claims({'items':[claim]},claim['quote'],published)) == 1
    assert not validate_claims({'items':[{**claim,'value':200}]},claim['quote'],published)
    assert not validate_claims({'items':[claim]},'Nothing to support this',published)
    assert not validate_claims({'items':[{**claim,'kind':'reported'}]},claim['quote'],published)
    with pytest.raises(ValueError):
        document_text('http://127.0.0.1/secrets','body')


def test_syndicated_news_counts_once_and_neutral_is_not_missing():
    now = AS_OF.astimezone(dt.UTC)
    rows = [dict(company_id=1,tone_id=i,published_at=now,title='Company opens a new factory',
                 quote='The company has opened a new factory in Gujarat today.',
                 tone=0.,confidence=.9,reason='neutral',url=f'https://publisher{i}.example')
            for i in (1,2)]
    view = SimpleNamespace(as_of=now,table=lambda _:pl.DataFrame(rows))
    result = _news_signals(view,load_scoring().sentiment.stock.news)
    assert result[1]['signal'] == 0
    assert len(result[1]['items']) == 1


@pytest.mark.db
def test_forward_evidence_known_at_and_delivery(db_conn):
    import db_market

    from igs.forward import evidence
    db_market.load(db_conn)
    ann = db_conn.execute('select ann_id from announcement limit 1').fetchone()[0]
    claim = {'metric':'capacity','kind':'guidance','value':100,'unit':'MW','scope':'Plant A',
             'period_end':'2027-03-31','quote':'Plant A will reach 100 MW by March 2027.',
             'confidence':.9}
    t = dt.datetime(2026,10,1,tzinfo=dt.UTC)
    db_conn.execute('''insert into forward_document(ann_id,company_id,published_at,received_at,
        assessed_at,claims) values(%s,1,%s,%s,%s,%s)''',
        (ann,t,t,t+dt.timedelta(days=1),json.dumps([claim])))
    assert evidence(db_conn,1,t) == []
    assert evidence(db_conn,1,t+dt.timedelta(days=2))[0]['delivery']['status'].startswith('No ')


@pytest.mark.parametrize('url', [None, 'https://example.com/company.pdf',
                                'https://nsearchives.nseindia.com.evil.example/a.pdf'])
def test_forward_rejects_non_exchange_sources_and_body_fallback(url):
    with pytest.raises(ValueError):
        document_text(url, 'Private body must never be used as a fallback.')
