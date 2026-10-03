import datetime as dt
import json

import db_market
import polars as pl
import pytest
import xlsx_files
from polars.testing import assert_frame_equal

from igs import service
from igs.config import load_red_flags, load_scoring, load_universe
from igs.dq import DQLog
from igs.factors import base
from igs.ingest.manual import import_screener_bytes
from igs.ingest.raw_store import RawStore
from igs.pit.loader import load_dataset
from igs.pit.screener import SCHEMA
from igs.pit.view import PitDataset, PitView
from igs.score.run import explanations, score

UTC=dt.UTC
BEFORE=dt.datetime(2024,11,1,tzinfo=UTC)
AFTER=dt.datetime(2024,11,3,tzinfo=UTC)
OBSERVED=dt.datetime(2024,11,2,tzinfo=UTC)
PERIOD=dt.date(2024,9,30)


def fact(fid,concept,value,basis='consolidated',observed=OBSERVED,period=PERIOD):
    return (fid,None,1,basis,None,period,'Q',concept,value,observed,
            'export' if fid<0 else None)


@pytest.mark.lookahead
def test_fallback_is_known_only_after_observation_and_never_overwrites_exchange():
    official=pl.DataFrame([fact(1,'revenue',100,observed=BEFORE)],schema=SCHEMA,orient='row')
    fallback=pl.DataFrame([fact(-1,'revenue',900),fact(-2,'pat',10),
                          fact(-3,'pbt',15,basis='standalone'),
                          fact(-4,'revenue',999,period=dt.date(2025,3,31))],
                         schema=SCHEMA,orient='row')
    ds=PitDataset.from_frames(facts=official,screener_facts=fallback)
    assert PitView(ds,BEFORE).facts().height==1
    now=PitView(ds,AFTER)
    out=now.facts().sort('concept')
    assert dict(out.select('concept','value').iter_rows())=={'pat':10,'revenue':100}
    assert_frame_equal(PitView(ds.poison_future(BEFORE),BEFORE).facts(),
                       PitView(ds,BEFORE).facts())
    assert_frame_equal(PitView(ds.truncate(BEFORE),BEFORE).facts(),
                       PitView(ds,BEFORE).facts())
    now.audit.assert_clean()


@pytest.mark.lookahead
def test_revised_export_affects_only_subsequent_scores():
    old=pl.DataFrame([fact(-1,'revenue',100)],schema=SCHEMA,orient='row')
    new=pl.DataFrame([fact(-2,'revenue',200,observed=AFTER+dt.timedelta(days=1))],
                     schema=SCHEMA,orient='row')
    ds=PitDataset.from_frames(screener_facts=pl.concat([old,new]))
    assert PitView(ds,AFTER).facts()['value'].to_list()==[100]
    assert PitView(ds,AFTER+dt.timedelta(days=2)).facts()['value'].to_list()==[200]


@pytest.mark.db
@pytest.mark.lookahead
def test_imported_quarters_make_stock_eligible_with_auditable_sources(db_conn,tmp_path,monkeypatch):
    db_market.load(db_conn)
    # GROW keeps just one official quarter; imports provide the missing history.
    db_conn.execute("alter table fundamental_fact disable trigger user")
    db_conn.execute("delete from fundamental_fact where company_id=1 and period_type='Q' "
                    "and period_end<'2024-09-30'")
    db_conn.execute("alter table fundamental_fact enable trigger user")
    obs=db_market.AS_OF-dt.timedelta(days=1)
    monkeypatch.setattr("igs.ingest.raw_store.utc_now",lambda:obs)
    periods={dt.date(y,m,30 if m in (6,9) else 31):(100+(y-2022)*20+m,10+m/2)
             for y in (2022,2023,2024) for m in (3,6,9,12)
             if dt.date(y,m,1)<dt.date(2024,10,1)}
    got=import_screener_bytes(db_conn,RawStore(tmp_path),
        xlsx_files.screener_export('Grow Industries Limited',periods,{}),'GROW.xlsx',DQLog())
    start=dt.date(2017,1,1)
    # Unknown basis cannot influence scoring.
    ds=load_dataset(db_conn,start,db_market.AS_OF.date())
    assert base.quarterly(PitView(ds,db_market.AS_OF)).filter(pl.col('company_id')==1).height==1
    db_conn.execute('''insert into screener_export_context
        (source_fetch_id,company_id,statement_basis,verified_at) values(%s,1,'consolidated',%s)''',
        (got.fetch_id,obs))
    ds=load_dataset(db_conn,start,db_market.AS_OF.date())
    view=PitView(ds,db_market.AS_OF)
    q=base.quarterly(view).filter(pl.col('company_id')==1)
    assert q.height==11
    assert q.filter(pl.col('period_end')==dt.date(2023,3,31))['top_line'][0]==123*1e7
    assert q.filter(pl.col('period_end')==dt.date(2023,3,31))['ebitda'][0] is None
    assert len(service.financials_8q(db_conn,1,db_market.AS_OF))==8
    assert service.readiness(db_conn,8)['results_enough']>=1
    uc=load_universe().model_copy(update={'min_market_cap_cr':0.0})
    result=score(ds,db_market.AS_OF,load_scoring(),uc,load_red_flags(),check_gate=False)
    company=result.universe.filter(pl.col('company_id')==1).row(0,named=True)
    assert company['included'] and company['quarters_filed']==11
    details=[json.loads(r['detail']) for r in result.factors.filter(pl.col('company_id')==1)
             .iter_rows(named=True)]
    sources=[s for d in details for s in d.get('screener_sources',[])]
    assert sources and sources[0]['fetch_id']==got.fetch_id
    assert all(i>0 for ids in result.factors['source_filing_ids'] for i in ids)
    assert 'Screener.in quarterly exports' in explanations(result,ds.tables['filings'])[1]
    before=PitView(ds,obs-dt.timedelta(seconds=1))
    assert not before.facts().filter(pl.col('fact_id')<0).height
    assert db_conn.execute('select count(*) from fundamental_fact where fact_id<0').fetchone()[0]==0
    past=obs-dt.timedelta(seconds=1)
    baseline=score(ds,past,load_scoring(),uc,load_red_flags(),check_gate=False)
    for changed in (ds.truncate(past),ds.poison_future(past)):
        checked=score(changed,past,load_scoring(),uc,load_red_flags(),check_gate=False)
        assert_frame_equal(baseline.results,checked.results)
