import datetime as dt
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import xlsx_files

from igs import screener_backfill as job
from igs.dq import DQLog
from igs.ingest.raw_store import RawStore
from igs.ingest.screener_download import AccessLimited, Client, LoginRequired


def test_authenticated_export_keeps_credentials_on_origin_and_checks_identity():
    requests = []
    blob = b'PK workbook fixture'
    def handler(r):
        requests.append(r)
        if r.url.path == '/login/':
            if r.method == 'GET':
                return httpx.Response(200, text='<input name="csrfmiddlewaretoken" value="token">')
            body = parse_qs(r.content.decode())
            assert body['username'] == ['owner'] and body['password'] == ['private']
            return httpx.Response(302,
                headers={'location': '/', 'set-cookie': 'sessionid=ok; Path=/'})
        if r.url.path == '/':
            return httpx.Response(200, text='Account')
        if r.url.path.startswith('/company/'):
            return httpx.Response(200, text='''<input name="csrfmiddlewaretoken" value="other">
                <a href="https://www.nseindia.com/get-quotes/equity?symbol=TEST">NSE</a>
                <button formaction="/user/company/export/12/">Export</button>''')
        assert r.url.path == '/user/company/export/12/' and r.method == 'POST'
        assert 'password' not in r.content.decode()
        return httpx.Response(200, content=blob)
    client = Client(httpx.Client(transport=httpx.MockTransport(handler)), delay=0)
    client.login('owner', 'private')
    assert client.download('TEST')[0] == blob
    assert all(r.url.host=='www.screener.in' for r in requests)


def test_external_redirect_is_not_followed_and_login_failure_is_safe():
    hits=[]
    def external(r):
        hits.append(r)
        return httpx.Response(302,headers={'location':'https://external.invalid/'})
    client=Client(httpx.Client(transport=httpx.MockTransport(external)),delay=0)
    with pytest.raises(AccessLimited):
        client.request('POST','/login/',data={'password':'private'})
    assert len(hits)==1
    client=Client(httpx.Client(transport=httpx.MockTransport(lambda r:
        httpx.Response(200,text='<input name="csrfmiddlewaretoken" value="x">'
                               '<input name="password">private'))),delay=0)
    with pytest.raises(LoginRequired) as error:
        client.login('owner','private')
    assert 'private' not in str(error.value)


def seed(conn):
    for name in ('LOW','HIGH','CALL'):
        cid=conn.execute('insert into company(name) values(%s) returning company_id',
                         (name+' Ltd',)).fetchone()[0]
        sid=conn.execute('insert into security(company_id) values(%s) returning security_id',
                         (cid,)).fetchone()[0]
        conn.execute('''insert into security_identifier(security_id,id_type,id_value,
            valid_from,evidence) values(%s,'NSE_SYMBOL',%s,'2020-01-01','test')''',(sid,name))
    rid=conn.execute('''insert into score_run(as_of,gate_fingerprint,config)
        values(now(),'test','{}') returning run_id''').fetchone()[0]
    ids={name:cid for cid,name in conn.execute('select company_id,name from company')}
    for name,score in [('LOW Ltd',20),('HIGH Ltd',90),('CALL Ltd',10)]:
        conn.execute('''insert into score_result(run_id,company_id,composite,tier,explanation)
            values(%s,%s,%s,'test','test')''',(rid,ids[name],score))
    conn.execute('''insert into broker_call(company_id,stock_name,broker,stance,rating,kind,
        called_on,source,dedupe_key) values(%s,'CALL','B','buy','buy','research',current_date,
        'manual','test')''',(ids['CALL Ltd'],))
    conn.commit()
    return ids


@pytest.mark.db
def test_priority_import_resumption_and_no_pit_facts(db_conn,tmp_path,monkeypatch):
    ids=seed(db_conn)
    assert [r['symbol'] for r in job.candidates(db_conn)]==['CALL','HIGH','LOW']
    monkeypatch.setenv('SCREENER_EMAIL','owner')
    monkeypatch.setenv('SCREENER_PASSWORD','private')
    periods={dt.date(2023+y,month,30 if month in (6,9) else 31):(10,2)
             for y in (0,1) for month in (3,6,9,12)}
    class Fake:
        statement_basis="consolidated"
        def login(self,*args): pass
        def close(self): pass
        def download(self,symbol,id_type):
            return xlsx_files.screener_export(symbol+' Ltd',periods,{}), 'https://www.screener.in/export'
    ctx=SimpleNamespace(conn=db_conn,store=RawStore(tmp_path),dq=DQLog())
    assert job.run(ctx,1,Fake())['downloaded']==1
    assert [r['symbol'] for r in job.candidates(db_conn)]==['HIGH','LOW']
    assert db_conn.execute('select count(*) from fundamental_fact').fetchone()[0]==0
    assert db_conn.execute('select status from screener_download where company_id=%s',
                           (ids['CALL Ltd'],)).fetchone()[0]=='downloaded'
    assert db_conn.execute('select origin from raw_payload').fetchone()[0]=='http'
    # Complete quarterly exports must refresh too: annual inputs can still be missing.
    # They follow the companies still short of eight quarters.
    db_conn.execute("update screener_download set next_attempt_at=now()-interval '1 day'")
    assert [r['symbol'] for r in job.candidates(db_conn)]==['HIGH','LOW','CALL']



@pytest.mark.db
def test_login_failure_pauses_without_repeated_attempts(db_conn,tmp_path,monkeypatch):
    seed(db_conn)
    monkeypatch.setenv('SCREENER_EMAIL','owner')
    monkeypatch.setenv('SCREENER_PASSWORD','private')
    class Fake:
        calls=0
        def login(self,*args):
            self.calls+=1
            raise LoginRequired('Sign-in required')
        def close(self): pass
    fake=Fake()
    ctx=SimpleNamespace(conn=db_conn,store=RawStore(tmp_path),dq=DQLog())
    assert job.run(ctx,1,fake)['paused']
    job.run(ctx,1,fake)
    assert fake.calls==1


@pytest.mark.db
def test_latest_ai_action_and_bse_only_identity_are_prioritized(db_conn):
    ids=seed(db_conn)
    rid=db_conn.execute('select max(run_id) from score_run').fetchone()[0]
    def call(action):
        db_conn.execute('''insert into ai_call(company_id,symbol,run_id,action,confidence,
            horizon_months,summary,reasons,risks,buy_when,sell_when,data_gaps,inputs,
            model,prompt_version,trigger) values(%s,'LOW',%s,%s,.5,12,'test','[]','[]',
            '[]','[]','[]','{}','test','test','manual')''',(ids['LOW Ltd'],rid,action))
    call('sell')
    assert [r['symbol'] for r in job.candidates(db_conn)][:2]==['LOW','CALL']
    call('hold')
    assert [r['symbol'] for r in job.candidates(db_conn)]==['CALL','HIGH','LOW']
    sid=db_conn.execute('select security_id from security where company_id=%s',
                        (ids['HIGH Ltd'],)).fetchone()[0]
    db_conn.execute("update security_identifier set valid_to=current_date "
                    "where security_id=%s",(sid,))
    db_conn.execute('''insert into security_identifier(security_id,id_type,id_value,valid_from,
        evidence) values(%s,'BSE_CODE','123456','2020-01-01','test')''',(sid,))
    assert job.candidates(db_conn)[1]['symbol']=='123456'


@pytest.mark.db
def test_identity_failure_backs_off_and_does_not_import(db_conn,tmp_path,monkeypatch):
    seed(db_conn)
    monkeypatch.setenv('SCREENER_EMAIL','owner')
    monkeypatch.setenv('SCREENER_PASSWORD','private')
    class Wrong:
        def login(self,*args): pass
        def close(self): pass
        def download(self,*args):
            return xlsx_files.screener_export('OTHER LTD',{},{}), 'https://www.screener.in/export'
    ctx=SimpleNamespace(conn=db_conn,store=RawStore(tmp_path),dq=DQLog())
    result=job.run(ctx,1,Wrong())
    assert result['downloaded']==0
    assert db_conn.execute('select count(*) from screener_enrichment').fetchone()[0]==0
    assert db_conn.execute('select status,next_attempt_at>now() from screener_download'
                           ).fetchone()==('failed',True)
    assert job.candidates(db_conn)[0]['symbol']=='HIGH'


@pytest.mark.parametrize('consolidated_status', [200, 404])
def test_standalone_fallback_for_missing_consolidated_export(consolidated_status):
    hits=[]
    identity='<a href="https://www.nseindia.com/get-quotes/equity?symbol=TEST">NSE</a>'
    def handler(r):
        hits.append(r.url.path)
        if r.url.path.endswith('/consolidated/'):
            return httpx.Response(consolidated_status,text=identity)
        if r.url.path=='/company/TEST/':
            return httpx.Response(200,text=identity +
                '<input name="csrfmiddlewaretoken" value="t">'
                '<button formaction="/user/company/export/12/">Export</button>')
        assert r.method=='POST' and r.url.path=='/user/company/export/12/'
        return httpx.Response(200,content=b'PK workbook')
    client=Client(httpx.Client(transport=httpx.MockTransport(handler)),delay=0)
    assert client.download('TEST')[0]==b'PK workbook'
    assert hits==['/company/TEST/consolidated/','/company/TEST/', '/user/company/export/12/']


def test_access_limit_does_not_trigger_standalone_fallback():
    hits=[]
    def handler(r):
        hits.append(r.url.path)
        return httpx.Response(429)
    client=Client(httpx.Client(transport=httpx.MockTransport(handler)),delay=0)
    with pytest.raises(AccessLimited):
        client.download('TEST')
    assert len(hits)==1


@pytest.mark.db
@pytest.mark.parametrize('master,workbook',[
    ("Dr. Reddys Laboratories Limited", "Dr Reddy's Laboratories Ltd"),
    ('Deepak Fertilizers and Petrochemicals Corporation Limited',
     'DEEPAK FERTILISERS & PETROCHEMICALS CORP LTD'),
])
def test_expected_company_name_comparison_handles_punctuation(
        db_conn,tmp_path,monkeypatch,master,workbook):
    ids=seed(db_conn)
    db_conn.execute('update company set name=%s where company_id=%s',
                    (master,ids['CALL Ltd']))
    db_conn.commit()
    monkeypatch.setenv('SCREENER_EMAIL','owner')
    monkeypatch.setenv('SCREENER_PASSWORD','private')
    class Fake:
        statement_basis='consolidated'
        def login(self,*args): pass
        def close(self): pass
        def download(self,*args):
            return xlsx_files.screener_export(workbook,
                {dt.date(2024,9,30):(10,2)},{}),'https://www.screener.in/export'
    ctx=SimpleNamespace(conn=db_conn,store=RawStore(tmp_path),dq=DQLog())
    assert job.run(ctx,1,Fake())['downloaded']==1


QUARTER_ENDS = [dt.date(2023, month, 30 if month in (6, 9) else 31) for month in (3, 6, 9, 12)]


def exchange_quarters(conn, cid, basis, ends):
    """Exchange-filed quarterly revenue for `ends`, in `basis`."""
    fetch = conn.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,
        content_sha256,size_bytes,blob_path,origin) values(%s,'s',now(),repeat('a',64),1,
        'blobs/aa/a','http') returning fetch_id""", (f's__q{cid}',)).fetchone()[0]
    filing = conn.execute("""insert into filing(company_id,exchange,filing_system,
        filing_type,filed_at,ingested_at,content_sha256,source_fetch_id) values(%s,'NSE',
        'test','financial_results','2025-01-01',now(),lpad(%s::text,64,'0'),%s)
        returning filing_id""", (cid, cid, fetch)).fetchone()[0]
    for end in ends:
        conn.execute("""insert into fundamental_fact(filing_id,company_id,statement_basis,
            period_end,period_type,concept,source_element,value,unit,filed_at,ingested_at)
            values(%s,%s,%s,%s,'Q','revenue','x',100,'INR','2025-01-01',now())""",
                     (filing, cid, basis, end))


@pytest.mark.db
def test_stocks_short_of_the_quarter_minimum_are_exported_first(db_conn):
    ids = seed(db_conn)
    all_ends = QUARTER_ENDS + [d.replace(year=2024) for d in QUARTER_ENDS]
    for name in ('HIGH Ltd', 'CALL Ltd'):
        exchange_quarters(db_conn, ids[name], 'consolidated', all_ends)
    db_conn.commit()
    rows = job.candidates(db_conn)
    # LOW has no quarters: first, ahead of a buy/sell call and a higher score.
    assert [(r['symbol'], r['covered_quarters'], r['needs_quarters']) for r in rows] == [
        ('LOW', 0, True), ('CALL', 8, False), ('HIGH', 8, False)]
    # The minimum is the universe's min_filing_quarters.
    assert [r['symbol'] for r in job.candidates(db_conn, min_quarters=9)] == [
        'CALL', 'HIGH', 'LOW']


@pytest.mark.db
def test_export_quarters_count_only_in_the_companys_reporting_basis(
        db_conn, tmp_path, monkeypatch):
    ids = seed(db_conn)
    exchange_quarters(db_conn, ids['CALL Ltd'], 'consolidated',
                      QUARTER_ENDS + [d.replace(year=2024) for d in QUARTER_ENDS])
    exchange_quarters(db_conn, ids['LOW Ltd'], 'consolidated', QUARTER_ENDS)
    exchange_quarters(db_conn, ids['HIGH Ltd'], 'standalone', QUARTER_ENDS)
    db_conn.commit()
    monkeypatch.setenv('SCREENER_EMAIL', 'owner')
    monkeypatch.setenv('SCREENER_PASSWORD', 'private')
    periods = {end: (10, 2) for end in QUARTER_ENDS + [d.replace(year=2024)
                                                        for d in QUARTER_ENDS]}

    class Fake:
        statement_basis = 'consolidated'
        def login(self, *args): pass
        def close(self): pass
        def download(self, symbol, id_type):
            return (xlsx_files.screener_export(symbol + ' Ltd', periods, {}),
                    'https://www.screener.in/export')

    ctx = SimpleNamespace(conn=db_conn, store=RawStore(tmp_path), dq=DQLog())
    assert job.run(ctx, 2, Fake())['downloaded'] == 2      # HIGH and LOW, both short
    db_conn.execute("update screener_download set next_attempt_at=now()-interval '1 day'")
    rows = {r['symbol']: r for r in job.candidates(db_conn)}
    # LOW: four exchange quarters plus four more from its consolidated export.
    assert (rows['LOW']['covered_quarters'], rows['LOW']['needs_quarters']) == (8, False)
    # HIGH reports standalone, so its consolidated export cannot fill its history.
    assert (rows['HIGH']['covered_quarters'], rows['HIGH']['needs_quarters']) == (4, True)
    assert next(iter(rows)) == 'HIGH'


@pytest.mark.db
def test_larger_traded_value_leads_among_unscored_companies(db_conn):
    ids = seed(db_conn)
    db_conn.execute('delete from score_result')
    db_conn.execute('delete from broker_call')
    db_conn.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,
        size_bytes,blob_path,origin) values('s__px','s',now(),repeat('a',64),1,'blobs/aa/a',
        'http')""")
    for name, isin, turnover in (('LOW Ltd', 'INE000000LOW', 5e7),
                                 ('HIGH Ltd', 'INE00000HIGH', 1e6)):
        sid = db_conn.execute('select security_id from security where company_id=%s',
                              (ids[name],)).fetchone()[0]
        db_conn.execute("""insert into security_identifier(security_id,id_type,id_value,
            valid_from,evidence) values(%s,'ISIN',%s,'2020-01-01','test')""", (sid, isin))
        db_conn.execute("""insert into price_eod(exchange,trade_date,isin,symbol,series,close,
            turnover_inr,source_fetch_id) values('NSE',current_date-1,%s,%s,'EQ',100,%s,
            's__px')""", (isin, name.split()[0], turnover))
    db_conn.commit()
    assert [r['symbol'] for r in job.candidates(db_conn)] == ['LOW', 'HIGH', 'CALL']
