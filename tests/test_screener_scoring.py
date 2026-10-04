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
    assert 'Screener.in financial exports' in explanations(result,ds.tables['filings'])[1]
    before=PitView(ds,obs-dt.timedelta(seconds=1))
    assert not before.facts().filter(pl.col('fact_id')<0).height
    assert db_conn.execute('select count(*) from fundamental_fact where fact_id<0').fetchone()[0]==0
    past=obs-dt.timedelta(seconds=1)
    baseline=score(ds,past,load_scoring(),uc,load_red_flags(),check_gate=False)
    for changed in (ds.truncate(past),ds.poison_future(past)):
        checked=score(changed,past,load_scoring(),uc,load_red_flags(),check_gate=False)
        assert_frame_equal(baseline.results,checked.results)


def test_statement_mapping_units_aggregates_and_ambiguous_totals():
    from igs.pit.screener import load
    fields = {
        'Quarters': {'Sales': 100, 'Net profit': 10, 'Expenses': 80},
        'PROFIT & LOSS': {'Sales': 400, 'Net profit': 40, 'Profit before tax': 60,
                         'Interest': 5, 'Depreciation': 10, 'Other Income': 2},
        'CASH FLOW': {'Cash from Operating Activity': 70},
        'BALANCE SHEET': {'Equity Share Capital': 10, 'Reserves': 90, 'Borrowings': 30,
                         'Other Liabilities': 20, 'Total': 150, 'Total (2)': 150,
                         'Net Block': 80, 'Capital Work in Progress': 10,
                         'Investments': 20, 'Other Assets': 40, 'Cash & Bank': 12,
                         'Inventory': 8, 'Face value': 10, 'No. of Equity Shares': 1e7}}
    rows = [(1,1,'consolidated','2024-03-31',line,value,OBSERVED,'export',section)
            for section, lines in fields.items() for line,value in lines.items()]
    # Interim P&L/cash flow must not be used as a whole financial year.
    rows += [(1,1,'consolidated','2024-06-30','Sales',999,OBSERVED,'export','PROFIT & LOSS')]
    class Conn:
        def execute(self,*args): return self
        def fetchall(self): return rows
    facts=load(Conn(),AFTER.date())
    view=PitView(PitDataset.from_frames(screener_facts=facts),AFTER)
    annual=base.annual(view).row(0,named=True)
    assert annual['cfo']==70e7 and annual['ebitda']==73e7
    assert facts['fact_id'].n_unique()==facts.height
    assert facts.filter(pl.col('period_type')=='FY')['period_end'].unique().to_list()==[
        dt.date(2024,3,31)]
    bs=base.balance_sheet(view).row(0,named=True)
    assert bs['total_assets']==150e7 and bs['total_equity']==100e7
    assert bs['borrowings_total']==30e7 and bs['borrowings_current'] is None
    assert facts.filter(pl.col('concept')=='total_expenses').is_empty()
    assert not facts.filter(pl.col('concept').str.contains('shares|face')).height
    # A malformed balance sheet must not supply a confidently misidentified Total.
    rows=[(*r[:5],999,*r[6:]) if r[4]=='Total (2)' else r for r in rows]
    assert load(Conn(),AFTER.date()).filter(pl.col('concept')=='total_assets').is_empty()


@pytest.mark.lookahead
def test_annual_cagr_fallback_is_explicit_and_observation_dated():
    from igs.factors.growth import _cagr_factor
    rows=[fact(-1,'revenue',10)]
    for year,rev in [(2019,100),(2021,121),(2024,161.051)]:
        rows.append((-year,None,1,'consolidated',None,dt.date(year,3,31),'FY',
                     'revenue',rev,OBSERVED,'export'))
    ds=PitDataset.from_frames(screener_facts=pl.DataFrame(rows,schema=SCHEMA,orient='row'))
    out=_cagr_factor(PitView(ds,AFTER),'top_line',3).row(0,named=True)
    assert out['value']==pytest.approx(0.1)
    assert json.loads(out['detail'])['comparison']=='matched_fiscal_years'
    assert -2021 in out['source_fact_ids'] and -2024 in out['source_fact_ids']
    for changed in (ds.truncate(BEFORE),ds.poison_future(BEFORE)):
        assert_frame_equal(_cagr_factor(PitView(ds,BEFORE),'top_line',3),
                           _cagr_factor(PitView(changed,BEFORE),'top_line',3))
    stale=PitView(ds,dt.datetime(2026,1,1,tzinfo=UTC))
    assert _cagr_factor(stale,'top_line',3)['status'].to_list()==['insufficient_data']


def test_aggregate_debt_and_cash_are_not_double_counted():
    bs=pl.DataFrame({'borrowings_noncurrent':[20.,None,None],
        'borrowings_current':[10.,None,None], 'borrowings_total':[90.,90.,None],
        'cash':[2.,None,None], 'bank_balances':[3.,None,None],
        'cash_and_bank':[80.,80.,None], 'current_investments':[1.,None,None]})
    assert bs.select(base.net_debt()).to_series().to_list()==[24.,10.,None]


def test_shareholding_ratio_conversion_requires_filing_total_anchor():
    from igs.pit.shareholding import percentages
    frame=pl.DataFrame({'filing_id':[1,1,2,2], 'category':['total','promoter']*2,
        'shares':[1000.,500.,1000.,5.], 'pct_of_total':[1.,.5,100.,.5],
        'pledged_shares':[None,50.,None,None], 'pledged_pct':[None,.1,None,None]})
    out=percentages(frame)
    assert out['pct_of_total'].to_list()==[100.,50.,100.,.5]
    assert out['pledged_pct'].to_list()==[None,10.,None,None]
    assert_frame_equal(percentages(out),out)


def test_annual_fallback_does_not_hide_invalid_recent_ttm_growth():
    from igs.factors.growth import _cagr_factor
    rows=[]
    for year,month in [(y,m) for y in (2021,2024) for m in (3,6,9)]+[(2020,12),(2023,12)]:
        date=dt.date(year,month,30 if month in (6,9) else 31)
        revenue=-10 if year>=2023 else 10
        rows.append(fact(-len(rows)-1,'revenue',revenue,period=date))
    for year,revenue in [(2021,100),(2024,150)]:
        rows.append((-year,None,1,'consolidated',None,dt.date(year,3,31),'FY',
                     'revenue',revenue,OBSERVED,'export'))
    view=PitView(PitDataset.from_frames(screener_facts=pl.DataFrame(
        rows,schema=SCHEMA,orient='row')),AFTER)
    assert _cagr_factor(view,'top_line',3)['status'].to_list()==['insufficient_data']


def test_peg_uses_verified_annual_growth_when_quarterly_history_is_short(monkeypatch):
    from igs.factors.valuation import peg_trailing
    rows=[]
    for year,month in [(2023,12),(2024,3),(2024,6),(2024,9)]:
        date=dt.date(year,month,30 if month in (6,9) else 31)
        for concept,value in [('revenue',100),('pat',10)]:
            rows.append(fact(-len(rows)-1,concept,value,period=date))
    for year,profit in [(2021,10),(2024,13.31)]:
        for concept,value in [('revenue',100),('pat',profit)]:
            rows.append((-100-len(rows),None,1,'consolidated',None,dt.date(year,3,31),'FY',
                         concept,value,OBSERVED,'export'))
    view=PitView(PitDataset.from_frames(screener_facts=pl.DataFrame(
        rows,schema=SCHEMA,orient='row')),AFTER)
    monkeypatch.setattr(base,'market_cap',lambda v: pl.DataFrame({'company_id':[1],'mcap':[800.]}))
    result=peg_trailing(view).row(0,named=True)
    assert result['value']==pytest.approx(2.0)  # P/E 20 divided by 10% CAGR
    assert json.loads(result['detail'])['growth_comparison']=='matched_fiscal_years'
    assert any(i < -100 for i in result['source_fact_ids'])
