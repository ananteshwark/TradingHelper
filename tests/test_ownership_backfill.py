from types import SimpleNamespace

import pytest
from test_screener_backfill import seed

from igs import ownership_backfill as job
from igs.ingest.jobs import JobResult


@pytest.mark.db
def test_archive_discovery_is_prioritized_resumable_and_bounded(db_conn,monkeypatch):
    seed(db_conn)
    assert [symbol for _,symbol in job.candidates(db_conn)]==['CALL','HIGH','LOW']
    spec=SimpleNamespace(url='https://www.nseindia.com/api/test?index=equities')
    ctx=SimpleNamespace(conn=db_conn,sources=SimpleNamespace(get=lambda _:spec))
    seen=[]
    def fetch(ctx,spec,url,params):
        seen.append((url,params))
        return JobResult('nse_shareholding_index',url,200,5,'test')
    def documents(ctx,kind,limit,**kwargs):
        assert kind=='shareholding' and limit==20
        assert kwargs['newest_first'] and kwargs['since'] is not None
        return []
    monkeypatch.setattr(job,'fetch_and_load',fetch)
    monkeypatch.setattr(job,'ingest_documents',documents)
    assert len(job.run(ctx,1,20))==1
    assert seen[0][0].endswith('&symbol=CALL')
    assert [symbol for _,symbol in job.candidates(db_conn)]==['HIGH','LOW']
    assert db_conn.execute('select last_error from ownership_backfill').fetchone()==(None,)


@pytest.mark.db
def test_archive_network_failure_backs_off_and_stops_batch(db_conn,monkeypatch):
    from igs.ingest.http import FetchError
    seed(db_conn)
    spec=SimpleNamespace(id='nse_shareholding_index',url='https://nse.test/?index=equities')
    ctx=SimpleNamespace(conn=db_conn,sources=SimpleNamespace(get=lambda _:spec))
    def fail(*args): raise FetchError('network unavailable')
    monkeypatch.setattr(job,'fetch_and_load',fail)
    monkeypatch.setattr(job,'ingest_documents',lambda *args,**kwargs:[])
    out=job.run(ctx,3,20)
    assert len(out)==1 and out[0].http_status is None
    assert [symbol for _,symbol in job.candidates(db_conn)]==['HIGH','LOW']
