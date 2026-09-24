"""The Integrated Filing listing: a paged endpoint, newest first, walked page by page.

Pages are built from the real first page NSE served (tests/fixtures/real); the fake
exchange behind them can gain a new filing at the top between runs, or ignore the page
number altogether."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from igs.config import load_sources
from igs.dq import DQLog
from igs.ingest import jobs
from igs.ingest.http import Fetcher
from igs.ingest.raw_store import RawStore
from igs.ingest.sources import render_url
from igs.ingest.verify import verify_source

pytestmark = pytest.mark.db

SOURCES = load_sources()
SPEC = SOURCES.get("nse_integrated_filing_index")
REAL = json.loads((Path(__file__).parent / "fixtures" / "real" /
                   "integrated_filing_listing_p1.json").read_bytes())
SIZE = SPEC.options["page_size"]


def _copy(row: dict, n: int) -> dict:
    """An older filing shaped like a real row, with its own sequence id and document."""
    seq = str(100000 + n)
    return {**row, "seq_Id": seq,
            "xbrl": row["xbrl"].replace("_WEB.xml", f"_{seq}_WEB.xml")}


class Exchange:
    """Serves `rows` newest first in pages of SIZE; page numbers start at 1."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.ignore_page = False
        self.requested: list[int] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if str(req.url) == "https://www.nseindia.com/":
            return httpx.Response(200, content=b"<html>home</html>")
        page = int(req.url.params["page"])
        self.requested.append(page)
        start = 0 if self.ignore_page else (page - 1) * SIZE
        body = {"data": self.rows[start:start + SIZE], "size": SIZE, "page": page - 1,
                "totalCount": len(self.rows)}
        return httpx.Response(200, content=json.dumps(body).encode())


@pytest.fixture
def setup(tmp_path, db_conn):
    ex = Exchange(REAL["data"] + [_copy(REAL["data"][i % 20], i) for i in range(25)])  # 45
    store = RawStore(tmp_path / "raw")
    fetcher = Fetcher(store, min_interval_s=0, host_min_interval_s={}, sleep=lambda s: None,
                      client=httpx.Client(transport=httpx.MockTransport(ex.handler)))
    ctx = jobs.Context(conn=db_conn, store=store, sources=SOURCES, fetcher=fetcher,
                       dq=DQLog())
    assert verify_source(SPEC, fetcher, today=dt.date(2026, 9, 23)).status == "verified"
    ex.requested.clear()
    return ctx, ex


def _refs(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("select count(*) from filing_ref where filing_system = 'nse_integrated_filing'")
        return cur.fetchone()[0]


def test_url_is_the_request_seen_in_the_browser():
    assert render_url(SPEC) == ("https://www.nseindia.com/api/integrated-filing-results?"
                                "&type=Integrated%20Filing-%20Financials&page=1&size=20")


def test_walks_to_the_end_then_stops_when_caught_up(setup):
    ctx, ex = setup
    res = jobs.ingest_pages(ctx, SPEC.id)
    assert ex.requested == [1, 2, 3] and [r.rows for r in res] == [20, 20, 5]
    assert res[-1].note == "end: short page" and _refs(ctx.conn) == 45

    ex.requested.clear()                         # nothing new: one page, then stop
    res = jobs.ingest_pages(ctx, SPEC.id)
    assert ex.requested == [1] and res[-1].note == "caught up: no new rows"

    # A revision of an old quarter is listed first; the rest shift down a place.
    ex.rows.insert(0, {**_copy(REAL["data"][1], 999), "creation_Date": "24-Sep-2026 10:00:00"})
    ex.requested.clear()
    res = jobs.ingest_pages(ctx, SPEC.id)
    assert ex.requested == [1, 2] and [r.rows for r in res] == [1, 0]
    assert _refs(ctx.conn) == 46 and ctx.dq.count("error") == 0


def test_page_number_ignored_is_an_error_not_a_loop(setup):
    ctx, ex = setup
    ex.ignore_page = True
    res = jobs.ingest_pages(ctx, SPEC.id, until_known=False)
    assert ex.requested == [1, 2] and res[-1].note == "stopped: page repeats the previous one"
    assert [i.category for i in ctx.dq.issues if i.severity == "error"] == [
        "paging_not_advancing"]


def test_page_limit_is_reported_with_where_to_resume(setup):
    ctx, ex = setup
    res = jobs.ingest_pages(ctx, SPEC.id, max_pages=2, until_known=False)
    assert ex.requested == [1, 2] and sum(r.rows for r in res) == 40
    warn = [i for i in ctx.dq.issues if i.category == "paging_limit"]
    assert len(warn) == 1 and "resume from page 3" in warn[0].message
    ex.requested.clear()                         # the backfill resumes where it stopped
    res = jobs.ingest_pages(ctx, SPEC.id, start_page=3, until_known=False)
    assert ex.requested == [3] and res[-1].note == "end: short page" and _refs(ctx.conn) == 45


REAL_DOCS = {"INTEGRATED_FILING_INDAS_1726290_22092026024242_WEB.xml":
             "results_integrated_PNCINFRA_2026Q4_consolidated.xml",
             "INTEGRATED_FILING_NBFC_INDAS_1726055_21092026060620_WEB.xml":
             "results_integrated_ARIHANTCAP_2026Q4_consolidated_nbfc.xml",
             "INTEGRATED_FILING_INDAS_1726803_23092026025108_WEB.xml":
             "results_integrated_ANNU_2026Q1_standalone.xml"}


def test_real_listing_to_point_in_time_facts(tmp_path, db_conn):
    """The real listing page and three of the real documents it links to, end to end."""
    real = Path(__file__).parent / "fixtures" / "real"
    ex = Exchange(REAL["data"])

    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        if req.url.host == "nsearchives.nseindia.com":
            return (httpx.Response(200, content=(real / REAL_DOCS[name]).read_bytes())
                    if name in REAL_DOCS else httpx.Response(404, content=b""))
        return ex.handler(req)

    store = RawStore(tmp_path / "raw")
    fetcher = Fetcher(store, min_interval_s=0, host_min_interval_s={}, sleep=lambda s: None,
                      client=httpx.Client(transport=httpx.MockTransport(handler)))
    ctx = jobs.Context(conn=db_conn, store=store, sources=SOURCES, fetcher=fetcher)
    with db_conn.cursor() as cur:
        for sym in ("PNCINFRA", "ARIHANTCAP", "ANNU"):
            cur.execute("insert into company (name) values (%s) returning company_id", (sym,))
            cur.execute("insert into security (company_id) values (%s) returning security_id",
                        (cur.fetchone()[0],))
            cur.execute("""insert into security_identifier (security_id, id_type, id_value,
                               valid_from, evidence) values (%s, 'NSE_SYMBOL', %s,
                               '2010-01-01', 'test')""", (cur.fetchone()[0], sym))
    db_conn.commit()
    assert verify_source(SPEC, fetcher, today=dt.date(2026, 9, 23)).status == "verified"

    res = jobs.ingest_pages(ctx, SPEC.id)
    assert [r.rows for r in res] == [20, 0] and res[-1].note == "end: empty page"
    docs = jobs.ingest_documents(ctx, "financial_results")
    assert len(docs) == 20 and sorted(d.http_status for d in docs).count(200) == 3
    assert ctx.dq.count("error") == 0 and ctx.dq.count("warn") == 0

    with db_conn.cursor() as cur:
        cur.execute("""select c.name, f.statement_basis, f.period_end, f.results_format,
                              f.filed_at
                       from filing f join company c using (company_id) order by c.name""")
        filings = cur.fetchall()
        cur.execute("""select value::float8 from facts_as_of(%s::timestamptz) f
                       join company c using (company_id)
                       where c.name = 'PNCINFRA' and concept = 'revenue'
                         and period_type = 'FY'""", ("2026-09-22 14:42:43+05:30",))
        before = cur.fetchall()
        cur.execute("""select value::float8 from facts_as_of(%s::timestamptz) f
                       join company c using (company_id)
                       where c.name = 'PNCINFRA' and concept = 'revenue'
                         and period_type = 'FY'""", ("2026-09-22 14:42:44+05:30",))
        after = cur.fetchall()
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert [(n, b, p, r) for n, b, p, r, _ in filings] == [
        ("ANNU", "standalone", dt.date(2026, 6, 30), "default"),
        ("ARIHANTCAP", "consolidated", dt.date(2026, 3, 31), "nbfc"),
        ("PNCINFRA", "consolidated", dt.date(2026, 3, 31), "default")]
    # Known from the listing's creation time, to the second.
    assert filings[2][4] == dt.datetime(2026, 9, 22, 14, 42, 44, tzinfo=ist)
    assert before == [] and after == [(53_681_016_000.0,)]
