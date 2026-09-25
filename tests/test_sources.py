from __future__ import annotations

import datetime as dt
import io
import json
import re
import zipfile
from pathlib import Path

import httpx
import pytest

from igs.config import SourceSpec, load_sources
from igs.ingest.http import Fetcher, FetchError
from igs.ingest.raw_store import RawStore
from igs.ingest.sources import SourceNotReady, recent_weekdays, render_url
from igs.ingest.verify import (
    ProbeError,
    SourceNotVerified,
    check_fingerprint,
    latest_verification,
    probe,
    require_verified,
    verify_source,
)


def spec(**kw) -> SourceSpec:
    base = dict(id="t", tier=1, description="d", url="https://x/{ddmmyyyy}.csv",
                kind="date_file", format="csv", session="none")
    base.update(kw)
    return SourceSpec(**base)


def test_registry_loads_and_every_url_is_https_or_null():
    cfg = load_sources()
    assert len(cfg.sources) >= 15
    for s in cfg.sources:
        assert s.url is None or s.url.startswith("https://"), s.id


def test_render_date_templates():
    cfg = load_sources()
    d = dt.date(2024, 7, 5)
    assert render_url(cfg.get("nse_cm_bhavcopy_udiff"), day=d).endswith(
        "BhavCopy_NSE_CM_0_0_0_20240705_F_0000.csv.zip")
    assert render_url(cfg.get("nse_cm_bhavcopy_legacy"), day=d).endswith(
        "/2024/JUL/cm05JUL2024bhav.csv.zip")
    assert render_url(cfg.get("nse_sec_bhavdata_full"), day=d).endswith("_05072024.csv")
    rng = render_url(cfg.get("nse_corporate_actions"), start=dt.date(2024, 1, 1), end=d)
    assert "from_date=01-01-2024&to_date=05-07-2024" in rng


def test_null_url_is_not_ready():
    with pytest.raises(SourceNotReady):
        render_url(spec(url=None, kind="static"))


def test_recent_weekdays_skips_weekends():
    days = recent_weekdays(dt.date(2024, 7, 8), 3)   # a Monday
    assert days == [dt.date(2024, 7, 5), dt.date(2024, 7, 4), dt.date(2024, 7, 3)]


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def test_probe_formats():
    assert probe("csv", b"A, B\n1,2\n3,4\n") == (["A", "B"], 2)
    assert probe("zip_csv", _zip("x.csv", "A,B\n1,2\n")) == (["A", "B"], 1)
    assert probe("json", json.dumps([{"b": 1, "a": 2}]).encode()) == (["a", "b"], 1)
    assert probe("json", json.dumps({"data": [{"k": 1}, {"k": 2}], "x": 0}).encode()) == (
        ["data", "x", "data.k"], 2)
    assert probe("text", b"title line\n10,MTO,01012024,1,x\nRecord,Sr,Name,Qty,Deliv\n")[0][0] \
        == "Record"


@pytest.mark.parametrize("fmt,body", [
    ("csv", b""), ("csv", b"A,B\n"), ("json", b"[]"), ("json", b'{"data": [], "page": 0}'),
    ("json", b"{not json"),
    ("zip_csv", b"PK not really"), ("csv", b"<!DOCTYPE html><html>Access Denied</html>"),
])
def test_probe_rejects_bad_payloads(fmt, body):
    with pytest.raises(ProbeError):
        probe(fmt, body)


def _fetcher(tmp_path: Path, handler) -> Fetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Fetcher(RawStore(tmp_path), min_interval_s=0, client=client, host_min_interval_s={},
                   sleep=lambda s: None)


def test_verify_success_lands_payload_and_records(tmp_path):
    f = _fetcher(tmp_path, lambda req: httpx.Response(200, content=b"SYMBOL,ISIN\nA,B\n"))
    v = verify_source(spec(kind="static", url="https://x/list.csv"), f)
    assert v.status == "verified" and v.schema == ["SYMBOL", "ISIN"] and v.row_count == 1
    assert f.store.read_bytes(v.fetch_id) == b"SYMBOL,ISIN\nA,B\n"
    assert latest_verification(tmp_path, "t") == v


def test_verify_date_file_falls_back_over_holidays(tmp_path):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if "04072024" in str(req.url):
            return httpx.Response(200, content=b"A,B\n1,2\n")
        return httpx.Response(404, content=b"")

    v = verify_source(spec(), _fetcher(tmp_path, handler), today=dt.date(2024, 7, 6))
    assert v.status == "verified"
    assert [u.rsplit("/", 1)[1] for u in seen] == ["05072024.csv", "04072024.csv"]
    # The 404 body was landed too: every response is kept verbatim.
    assert len(list(RawStore(tmp_path).iter_records())) == 2


def test_verify_html_block_page_fails(tmp_path):
    f = _fetcher(tmp_path, lambda req: httpx.Response(200, content=b"<html>denied</html>"))
    v = verify_source(spec(kind="static", url="https://x/a.csv"), f)
    assert v.status == "failed" and "HTML" in v.message


def test_verify_network_error_fails_without_landing(tmp_path):
    def handler(req):
        raise httpx.ConnectError("CONNECT tunnel failed, response 403")

    v = verify_source(spec(), _fetcher(tmp_path, handler), today=dt.date(2024, 7, 6))
    assert v.status == "failed" and "403" in v.message
    assert list(RawStore(tmp_path).iter_records()) == []


def test_ingestion_gate(tmp_path):
    s = spec(kind="static", url="https://x/a.csv")
    with pytest.raises(SourceNotVerified, match="never verified"):
        require_verified(tmp_path, s)
    f = _fetcher(tmp_path, lambda req: httpx.Response(200, content=b"A,B\n1,2\n"))
    v = verify_source(s, f)
    assert require_verified(tmp_path, s) == v
    check_fingerprint(s, b"A,B\n9,9\n", v)
    with pytest.raises(SourceNotVerified, match="schema changed"):
        check_fingerprint(s, b"A,B,C\n1,2,3\n", v)
    with pytest.raises(SourceNotVerified, match="URL changed"):
        require_verified(tmp_path, spec(kind="static", url="https://x/moved.csv"))


def test_empty_page_passes_the_fingerprint_only_in_the_verified_envelope(tmp_path):
    """Past its last page a paged listing answers {"data": []}: no rows to fingerprint.
    Verification never accepts that; later fetches do, if the envelope is the verified one."""
    s = spec(kind="paged", format="json", url="https://x/list?page={page}&size={size}",
             options={"first_page": 1, "page_size": 2, "max_pages": 5})
    rows = {"data": [{"a": 1}, {"a": 2}], "page": 0, "size": 2, "totalCount": 2}
    empty = {"data": [], "page": 1, "size": 2, "totalCount": 2}
    f = _fetcher(tmp_path, lambda req: httpx.Response(
        200, content=json.dumps(rows if req.url.params["page"] == "1" else empty).encode()))
    assert render_url(s, page=3) == "https://x/list?page=3&size=2"
    v = verify_source(s, f)
    assert v.status == "verified" and v.url == "https://x/list?page=1&size=2"
    check_fingerprint(s, json.dumps(empty).encode(), v)
    check_fingerprint(s, b"[]", v)
    with pytest.raises(SourceNotVerified, match="schema changed"):
        check_fingerprint(s, json.dumps({"data": [], "error": "x"}).encode(), v)
    with pytest.raises(ValueError, match="needs options"):
        spec(kind="paged", format="json", url="https://x/list?page={page}&size={size}")
    with pytest.raises(ValueError, match=r"needs \{page\} and \{size\}"):
        spec(kind="paged", format="json", url="https://x/list",
             options={"first_page": 1, "page_size": 2, "max_pages": 5})


ORDER_WRITE_PATTERNS = [
    r"place_?order", r"modify_?order", r"cancel_?order", r"/orders?\b", r"order/place",
    r"\b(?:client|httpx|requests|session)\.(?:post|put|patch|delete)\(",
    r"method\s*=\s*[\"'](?:post|put|patch|delete)[\"']",
]


# The single allowed mutating HTTP call: Telegram sendMessage (a notification to the
# user's own chat). Order-endpoint patterns are still checked in this file.
MUTATING_VERB_ALLOWED = {"alerts/delivery.py"}


def test_no_order_write_path_exists():
    """Broker integration is read-only: no order endpoints, no mutating HTTP verbs."""
    root = Path(__file__).resolve().parents[1] / "src" / "igs"
    hits = []
    for p in root.rglob("*.py"):
        rel = str(p.relative_to(root))
        for pat in ORDER_WRITE_PATTERNS:
            if rel in MUTATING_VERB_ALLOWED and "post|put" in pat:
                continue
            if re.search(pat, p.read_text(), re.IGNORECASE):
                hits.append(f"{rel}: {pat}")
    assert hits == []
    delivery = (root / "alerts" / "delivery.py").read_text()
    assert delivery.count(".post(") == 1 and "api.telegram.org" in delivery


def test_transient_failures_are_retried_and_every_attempt_landed(tmp_path):
    answers = iter([httpx.Response(503, content=b"busy"),
                    httpx.Response(429, content=b"slow down", headers={"Retry-After": "7"}),
                    httpx.Response(200, content=b"SYMBOL,ISIN\nA,B\n")])
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(lambda req: next(answers)))
    f = Fetcher(RawStore(tmp_path), min_interval_s=0, client=client, sleep=sleeps.append,
                backoff_s=1.0)
    rec = f.get("t", "https://x/list.csv")
    assert rec.http_status == 200 and f.store.read_bytes(rec) == b"SYMBOL,ISIN\nA,B\n"
    landed = sorted(r.http_status for r in f.store.iter_records())
    assert landed == [200, 429, 503]
    assert 1.0 <= sleeps[0] <= 1.5 and sleeps[1] == 7.0      # backoff, then Retry-After


def test_real_answers_are_not_retried_and_retries_give_up(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(404, content=b"no")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    f = Fetcher(RawStore(tmp_path), min_interval_s=0, client=client, sleep=lambda s: None)
    assert f.get("t", "https://x/missing.csv").http_status == 404 and len(calls) == 1

    def down(req):
        raise httpx.ConnectTimeout("timed out", request=req)
    f = Fetcher(RawStore(tmp_path), min_interval_s=0, max_attempts=3, sleep=lambda s: None,
                client=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(FetchError, match="after 3 attempts"):
        f.get("t", "https://x/list.csv")


def test_nse_cookie_expiry_reprimes_once(tmp_path):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.path == "/":
            return httpx.Response(200, content=b"home")
        data_calls = [u for u in seen if not u.endswith(".com/")]
        return httpx.Response(401 if len(data_calls) == 1 else 200, content=b"[]")
    f = Fetcher(RawStore(tmp_path), min_interval_s=0, sleep=lambda s: None,
                client=httpx.Client(transport=httpx.MockTransport(handler)))
    rec = f.get("t", "https://www.nseindia.com/api/x", session="nse_cookie")
    assert rec.http_status == 200
    assert seen.count("https://www.nseindia.com/") == 2          # primed, then re-primed


def test_akamai_throttle_is_waited_out_once(tmp_path):
    denied = b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>Reference #18.x</BODY></HTML>"
    answers = iter([httpx.Response(403, content=denied), httpx.Response(200, content=b"[]"),
                    httpx.Response(403, content=denied), httpx.Response(403, content=denied)])
    sleeps: list[float] = []
    f = Fetcher(RawStore(tmp_path), min_interval_s=0, sleep=sleeps.append,
                throttle_wait_s=330.0, host_min_interval_s={},
                client=httpx.Client(transport=httpx.MockTransport(lambda req: next(answers))))
    assert f.get("t", "https://www.nseindia.com/api/x").http_status == 200
    assert 330.0 in sleeps and sorted(r.http_status for r in f.store.iter_records()) == [200, 403]
    # Still denied after the one wait: the 403 is returned (and landed), not retried forever.
    assert f.get("t", "https://www.nseindia.com/api/y").http_status == 403
    assert sleeps.count(330.0) == 2
    # ... and that endpoint is not asked again in this run, whatever the query.
    with pytest.raises(FetchError, match="not asked again"):
        f.get("t", "https://www.nseindia.com/api/y?symbol=TCS")
    assert f.blocked_paths == {"www.nseindia.com/api/y"} and not f.blocked_hosts


DENIED = b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>Reference #18.x</BODY></HTML>"


def _nse(tmp_path, handler, sleeps: list[float]) -> Fetcher:
    seen: list[str] = []

    def record(req):
        seen.append(req.url.path)
        return handler(req)
    f = Fetcher(RawStore(tmp_path), min_interval_s=0, sleep=sleeps.append,
                throttle_wait_s=330.0, host_min_interval_s={},
                client=httpx.Client(transport=httpx.MockTransport(record)))
    f.seen = seen
    return f


def test_one_refused_endpoint_does_not_stop_the_others(tmp_path):
    """As on 2026-09-23: the quote API was denied while the listings on the same host
    answered seconds later. Giving up the whole host would have lost every listing."""
    sleeps: list[float] = []
    f = _nse(tmp_path, lambda req: httpx.Response(403, content=DENIED)
             if req.url.path == "/api/quote-equity" else httpx.Response(200, content=b"[]"),
             sleeps)
    assert f.get("q", "https://www.nseindia.com/api/quote-equity?symbol=RELIANCE"
                 ).http_status == 403
    for path in ("corporates-corporateActions", "corporates-financial-results",
                 "corporate-share-holdings-master", "corporate-announcements"):
        assert f.get("s", f"https://www.nseindia.com/api/{path}").http_status == 200
    assert sleeps.count(330.0) == 1 and not f.blocked_hosts
    with pytest.raises(FetchError, match="not asked again"):
        f.get("q", "https://www.nseindia.com/api/quote-equity?symbol=TCS")
    assert f.seen.count("/api/quote-equity") == 2              # before and after the wait


def test_a_host_refusing_everything_is_given_up_after_one_wait(tmp_path):
    sleeps: list[float] = []
    f = _nse(tmp_path, lambda req: httpx.Response(403, content=DENIED), sleeps)
    for name in ("a", "b", "c"):                 # b and c: no second wait
        assert f.get("s", f"https://www.nseindia.com/api/{name}").http_status == 403
    assert sleeps.count(330.0) == 1 and f.blocked_hosts == {"www.nseindia.com"}
    with pytest.raises(FetchError, match="3 requests in a row"):
        f.get("s", "https://www.nseindia.com/api/d")
    assert f.seen == ["/api/a", "/api/a", "/api/b", "/api/c"]
    # Another host is unaffected.
    f.get("s", "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv")
    assert f.seen[-1] == "/content/equities/EQUITY_L.csv"


def test_refused_home_page_is_not_reprimed_and_nse_is_paced(tmp_path):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(403 if req.url.path == "/" else 401, content=b"no")
    sleeps: list[float] = []
    f = Fetcher(RawStore(tmp_path), min_interval_s=0, sleep=sleeps.append,
                client=httpx.Client(transport=httpx.MockTransport(handler)))
    f.get("t", "https://www.nseindia.com/api/a", session="nse_cookie")
    f.get("t", "https://www.nseindia.com/api/b", session="nse_cookie")
    assert seen == ["/", "/api/a", "/api/b"]          # one priming attempt, no re-prime
    assert sleeps and all(s > 4.0 for s in sleeps)     # >= 5 s between NSE requests
