"""Ingestion jobs: fetch (behind the verification gate), land, parse, load.

Every source has one handler `handler(ctx, record, content) -> rows`. The same
handler runs for live ingestion and for `rebuild_from_raw`, which truncates
the derived tables and replays every landed payload in dependency order. That
is the proof that the database is fully rebuildable from the raw store.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

import polars as pl
import psycopg

from igs.config import SourcesConfig, SourceSpec
from igs.dq import DQLog
from igs.ingest.http import Fetcher
from igs.ingest.raw_store import FetchRecord, RawStore
from igs.ingest.sources import paging, render_url
from igs.ingest.verify import (
    PROBE_NOTE,
    SourceNotVerified,
    check_fingerprint,
    latest_verification,
    require_verified,
    verify_source,
)
from igs.normalize import nse
from igs.normalize.load import (
    load_announcements,
    load_corporate_actions,
    load_delivery,
    load_insider_trades,
    load_prices,
    load_simple,
)
from igs.normalize.masters import parse_angel_master, parse_bse_scrips
from igs.timeutil import IST, utc_now
from igs.xbrl import insider
from igs.xbrl.listing import listing_rows, ref_to_params
from igs.xbrl.load import load_document, load_listing, pending_refs

log = logging.getLogger(__name__)


@dataclass
class Context:
    conn: psycopg.Connection
    store: RawStore
    sources: SourcesConfig
    dq: DQLog = field(default_factory=DQLog)
    fetcher: Fetcher | None = None


Handler = Callable[[Context, FetchRecord, bytes], int]


def _snapshot_date(rec: FetchRecord) -> dt.date:
    return rec.fetched_at.astimezone(IST).date()


def _request_date(rec: FetchRecord) -> dt.date:
    d = rec.request_params.get("date")
    if not d:
        raise ValueError(f"{rec.fetch_id}: fetch record has no request date")
    return dt.date.fromisoformat(d)


# --------------------------------------------------------------------------- handlers


def _udiff(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    finals = ctx.sources.get(rec.source_id).options.get("final_sessions", ["F1"])
    kept, _counts = nse.parse_bhavcopy_udiff(content, finals, ctx.dq, rec.fetch_id)
    return load_prices(ctx.conn, kept, rec.fetch_id, ctx.dq)


def _legacy(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    return load_prices(ctx.conn, nse.parse_bhavcopy_legacy(content), rec.fetch_id, ctx.dq)


def _sec_full(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    return load_delivery(ctx.conn, nse.parse_sec_bhavdata_full(content))


def _mto(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    return load_delivery(ctx.conn, nse.parse_mto(content, _request_date(rec)))


def _equity_list(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    return load_simple(ctx.conn, "nse_equity_list", nse.parse_equity_list(content), rec.fetch_id,
                       ["isin", "snapshot_date"], {"snapshot_date": _snapshot_date(rec)})


def _index_close(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    return load_simple(ctx.conn, "index_price", nse.parse_index_close_all(content), rec.fetch_id,
                       ["index_name", "trade_date"])


def _holidays(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    return load_simple(ctx.conn, "trading_holiday", nse.parse_holidays(content), rec.fetch_id,
                       ["exchange", "holiday_date"], {"exchange": "NSE"})


def _surveillance(measure: str) -> Handler:
    def handle(ctx: Context, rec: FetchRecord, content: bytes) -> int:
        df = nse.parse_surveillance(content, measure, _snapshot_date(rec), ctx.dq)
        return load_simple(ctx.conn, "surveillance_snapshot", df, rec.fetch_id,
                           ["measure", "list_name", "symbol", "effective_from"])
    return handle


def _corporate_actions(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    df = nse.parse_corporate_actions(content, ctx.dq, rec.fetch_id)
    return load_corporate_actions(ctx.conn, df, rec.fetch_id)


def _announcements(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    df = nse.parse_announcements(content, ctx.dq, rec.fetch_id)
    return load_announcements(ctx.conn, df, rec.fetch_id, rec.fetched_at)


def _insider_trades(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    df = nse.parse_insider_trades(content, ctx.dq, rec.fetch_id)
    return load_insider_trades(ctx.conn, df, rec.fetch_id, rec.fetched_at, ctx.dq)


def _insider_disclosures(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    hosts = ctx.sources.get(rec.source_id).options["allowed_hosts"]
    df = insider.parse_disclosure_listing(content, hosts, ctx.dq, rec.fetch_id)
    return load_simple(ctx.conn, "insider_disclosure_ref", df, rec.fetch_id,
                       ["exchange", "document_url"])


def _insider_document(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    ref = insider.params_to_ref(rec.request_params)
    df = insider.parse_disclosure_document(content, ref, ctx.dq, rec.fetch_id)
    return load_insider_trades(ctx.conn, df, rec.fetch_id, rec.fetched_at, ctx.dq)


def _quote(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    symbol = rec.request_params.get("symbol")
    cls = nse.parse_quote_classification(content)
    with ctx.conn.cursor() as cur:
        cur.execute("""select s.company_id from security_identifier si
                       join security s using (security_id)
                       where si.id_type = 'NSE_SYMBOL' and si.id_value = %s
                         and si.valid_to is null""", (symbol,))
        row = cur.fetchone()
        if row is None:
            ctx.dq.emit("warn", "classification_unmapped", f"{symbol}: no current security",
                        fetch_id=rec.fetch_id)
            return 0
        cur.execute("""select macro_sector, sector, industry, basic_industry
                       from industry_classification where company_id = %s
                       order by valid_from desc limit 1""", (row[0],))
        prev = cur.fetchone()
        now = (cls["macro_sector"], cls["sector"], cls["industry"], cls["basic_industry"])
        if prev == now:
            return 0
        cur.execute("""insert into industry_classification (company_id, macro_sector, sector,
                           industry, basic_industry, valid_from, source_fetch_id)
                       values (%s, %s, %s, %s, %s, %s, %s)
                       on conflict (company_id, valid_from) do nothing""",
                    (row[0], *now, _snapshot_date(rec), rec.fetch_id))
        return cur.rowcount


def _index_sectors(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    """Each listed company's sector, by its NSE symbol on the list's date, else its ISIN;
    a row only where the sector differs from the company's latest one."""
    df = nse.parse_index_constituents(content)
    day = _snapshot_date(rec)
    with ctx.conn.cursor() as cur:
        cur.execute("""select si.id_type, si.id_value, s.company_id
                       from security_identifier si join security s using (security_id)
                       where si.id_type in ('NSE_SYMBOL', 'ISIN') and si.valid_from <= %s
                         and (si.valid_to is null or si.valid_to > %s)""", (day, day))
        ids = {(t, v): c for t, v, c in cur.fetchall()}
        cur.execute("""select distinct on (company_id) company_id, sector from index_sector
                       order by company_id, valid_from desc""")
        latest = dict(cur.fetchall())
        added, unmapped = 0, []
        for r in df.iter_rows(named=True):
            cid = ids.get(("NSE_SYMBOL", r["symbol"])) or ids.get(("ISIN", r["isin"]))
            sector = (r["industry"] or "").strip()
            if cid is None:
                unmapped.append(r["symbol"])
            elif sector and latest.get(cid) != sector:
                cur.execute("""insert into index_sector (company_id, sector, valid_from,
                                   source_fetch_id) values (%s, %s, %s, %s)
                               on conflict (company_id, valid_from) do nothing""",
                            (cid, sector, day, rec.fetch_id))
                latest[cid] = sector
                added += cur.rowcount
    if unmapped:
        ctx.dq.emit("warn", "index_sector_unmapped",
                    f"{len(unmapped)} index-list symbols match no company: "
                    + ", ".join(unmapped[:20]), fetch_id=rec.fetch_id)
    return added


def _index_members(index_name: str) -> Handler:
    """An index's members on the list's date (by NSE symbol on that date, else ISIN), stored
    as a snapshot only when they differ from the latest one; returns the members stored."""
    def handle(ctx: Context, rec: FetchRecord, content: bytes) -> int:
        df = nse.parse_index_constituents(content)
        day = _snapshot_date(rec)
        with ctx.conn.cursor() as cur:
            cur.execute("""select si.id_type, si.id_value, s.company_id
                           from security_identifier si join security s using (security_id)
                           where si.id_type in ('NSE_SYMBOL', 'ISIN') and si.valid_from <= %s
                             and (si.valid_to is null or si.valid_to > %s)""", (day, day))
            ids = {(t, v): c for t, v, c in cur.fetchall()}
            members = {ids.get(("NSE_SYMBOL", r["symbol"])) or ids.get(("ISIN", r["isin"]))
                       for r in df.iter_rows(named=True)}
            unmapped = None in members
            members.discard(None)
            cur.execute("""select company_id from index_member where index_name = %s
                           and as_of = (select max(as_of) from index_member
                                        where index_name = %s and as_of <= %s)""",
                        (index_name, index_name, day))
            if {r[0] for r in cur.fetchall()} == members:
                return 0
            for cid in sorted(members):
                cur.execute("""insert into index_member (index_name, as_of, company_id,
                                   source_fetch_id) values (%s, %s, %s, %s)
                               on conflict do nothing""", (index_name, day, cid, rec.fetch_id))
        if unmapped:
            ctx.dq.emit("warn", "index_member_unmapped",
                        f"{index_name}: {df.height - len(members)} listed symbols match no "
                        "company", fetch_id=rec.fetch_id)
        return len(members)
    return handle


def _bse(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    return load_simple(ctx.conn, "bse_scrip", parse_bse_scrips(content), rec.fetch_id,
                       ["scrip_code", "snapshot_date"], {"snapshot_date": _snapshot_date(rec)})


def _angel(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    return load_simple(ctx.conn, "broker_instrument", parse_angel_master(content), rec.fetch_id,
                       ["broker", "exchange", "token", "snapshot_date"],
                       {"snapshot_date": _snapshot_date(rec)})


def _listing(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    return load_listing(ctx.conn, content, rec, ctx.sources.get(rec.source_id).options, ctx.dq)


def _document(ctx: Context, rec: FetchRecord, content: bytes) -> int:
    return load_document(ctx.conn, rec, content, ctx.dq)


# Replay order for rebuilds: reference masters, then prices, then things that
# attach to prices (delivery), then events. Filing sources are registered by
# igs.xbrl and replayed after the instrument master is rebuilt.
HANDLERS: dict[str, Handler] = {
    "nse_equity_list": _equity_list,
    "bse_scrip_master": _bse,
    "angel_scrip_master": _angel,
    "nse_trading_holidays": _holidays,
    "nse_cm_bhavcopy_legacy": _legacy,
    "nse_cm_bhavcopy_udiff": _udiff,
    "nse_index_close_all": _index_close,
    "nse_sec_bhavdata_full": _sec_full,
    "nse_mto_delivery": _mto,
    "nse_corporate_actions": _corporate_actions,
    "nse_asm": _surveillance("ASM"),
    "nse_gsm": _surveillance("GSM"),
    "nse_announcements": _announcements,
    "nse_insider_trading": _insider_trades,
    "nse_insider_disclosures": _insider_disclosures,
    "nse_insider_xbrl": _insider_document,
    "nse_financial_results_index": _listing,
    "nse_integrated_filing_index": _listing,
    "nse_shareholding_index": _listing,
}
POST_MASTER_HANDLERS: dict[str, Handler] = {"nse_quote_equity": _quote,
                                            "nse_total_market_constituents": _index_sectors,
                                            "nse_nifty200_constituents":
                                                _index_members("NIFTY 200"),
                                            "nse_xbrl_document": _document}
DOCUMENT_SOURCE = "nse_xbrl_document"
INSIDER_LISTING = "nse_insider_disclosures"
INSIDER_DOCUMENT = "nse_insider_xbrl"


def register_post_master(source_id: str, handler: Handler) -> None:
    POST_MASTER_HANDLERS[source_id] = handler


# --------------------------------------------------------------------------- live ingestion


@dataclass(frozen=True)
class JobResult:
    source_id: str
    url: str
    http_status: int | None
    rows: int
    fetch_id: str | None
    note: str = ""


def _handler(source_id: str) -> Handler:
    h = HANDLERS.get(source_id) or POST_MASTER_HANDLERS.get(source_id)
    if h is None:
        raise KeyError(f"no handler registered for {source_id}")
    return h


def fetch_and_load(ctx: Context, spec: SourceSpec, url: str, params: dict) -> JobResult:
    if ctx.fetcher is None:
        raise RuntimeError("context has no fetcher")
    verification = require_verified(ctx.store.root, spec)
    rec = ctx.fetcher.get(spec.id, url, spec.session, params=params)
    ctx.store.index_record(ctx.conn, rec)
    ctx.conn.commit()
    if rec.http_status != 200:
        return JobResult(spec.id, url, rec.http_status, 0, rec.fetch_id, "not loaded")
    content = ctx.store.read_bytes(rec)
    check_fingerprint(spec, content, verification)
    with ctx.conn.transaction():
        rows = _handler(spec.id)(ctx, rec, content)
    return JobResult(spec.id, url, 200, rows, rec.fetch_id)


def ingest_date(ctx: Context, source_id: str, day: dt.date) -> JobResult:
    spec = ctx.sources.get(source_id)
    return fetch_and_load(ctx, spec, render_url(spec, day=day), {"date": day.isoformat()})


def ingest_range(ctx: Context, source_id: str, start: dt.date, end: dt.date,
                 chunk_days: int = 30) -> list[JobResult]:
    spec = ctx.sources.get(source_id)
    chunk_days = min(chunk_days, spec.options.get("max_range_days", chunk_days))
    out, s = [], start
    while s <= end:
        e = min(end, s + dt.timedelta(days=chunk_days - 1))
        out.append(fetch_and_load(ctx, spec, render_url(spec, start=s, end=e),
                                  {"start": s.isoformat(), "end": e.isoformat()}))
        s = e + dt.timedelta(days=1)
    return out


def ingest_static(ctx: Context, source_id: str) -> JobResult:
    spec = ctx.sources.get(source_id)
    return fetch_and_load(ctx, spec, render_url(spec), {})


def ingest_pages(ctx: Context, source_id: str, *, start_page: int | None = None,
                 max_pages: int | None = None, until_known: bool = True) -> list[JobResult]:
    """Walk a paged listing (newest first) from `start_page`, default the first page.

    Stops at the first of: a page that is not HTTP 200; an empty or short page (the end);
    a page whose rows all appeared on the previous page (paging not advancing, reported as
    an error: the base of the page numbers is not verified); with `until_known`, a page
    that adds no new row (caught up, since new and revised filings are listed first); or
    `max_pages` (reported, with the page to resume from). A backfill runs with
    until_known=False, so an earlier interrupted backfill is not mistaken for caught up.
    The last result's note says why the walk stopped.
    """
    spec = ctx.sources.get(source_id)
    p = paging(spec)
    first = p.first_page if start_page is None else start_page
    limit = p.max_pages if max_pages is None else max_pages
    out: list[JobResult] = []
    previous: set[str] = set()
    for page in range(first, first + limit):
        res = fetch_and_load(ctx, spec, render_url(spec, page=page), {"page": page})
        if res.http_status != 200:
            out.append(res)
            return out
        rows = listing_rows(ctx.store.read_bytes(res.fetch_id or ""), spec.id)
        seen = {json.dumps(r, sort_keys=True) for r in rows}
        stop = ""
        if not rows:
            stop = "end: empty page"
        elif seen <= previous:
            stop = "stopped: page repeats the previous one"
            ctx.dq.emit("error", "paging_not_advancing",
                        f"{spec.id}: page {page} repeats page {page - 1}; stopped",
                        fetch_id=res.fetch_id)
        elif until_known and res.rows == 0:
            stop = "caught up: no new rows"
        elif len(rows) < p.page_size:
            stop = "end: short page"
        out.append(replace(res, note=stop or f"page {page}"))
        if stop:
            return out
        previous = seen
    ctx.dq.emit("warn", "paging_limit",
                f"{spec.id}: stopped after {limit} pages ({first}..{first + limit - 1}); "
                f"resume from page {first + limit}")
    return out


def ingest_symbols(ctx: Context, source_id: str, symbols: Iterable[str]) -> list[JobResult]:
    spec = ctx.sources.get(source_id)
    return [fetch_and_load(ctx, spec, render_url(spec, symbol=s), {"symbol": s})
            for s in symbols]


QUOTE = "nse_quote_equity"
CLASSIFICATION_BATCH = 25         # quote requests per check
CLASSIFICATION_RETRY_DAYS = 7     # a symbol asked for without a result waits this long


def missing_classification(conn, limit: int = CLASSIFICATION_BATCH) -> list[str]:
    """Current NSE symbols of companies without NSE's industry classification, those in
    the latest score run first (without one, none of their factors has peers), then the
    rest by symbol. A symbol asked for in the last CLASSIFICATION_RETRY_DAYS is left out,
    so one whose quote gives no classification is not asked for at every check."""
    rows = conn.execute("""
        with recent as (
            select distinct request_params->>'symbol' as symbol from raw_payload
            where source_id = %(source)s
              and fetched_at > now() - make_interval(days => %(days)s)),
        ranked as (
            select company_id from score_result
            where run_id = (select max(run_id) from score_run))
        select si.id_value from security_identifier si join security s using (security_id)
        where si.id_type = 'NSE_SYMBOL' and si.valid_to is null
          and not exists (select 1 from industry_classification ic
                          where ic.company_id = s.company_id)
          and not exists (select 1 from recent r where r.symbol = si.id_value)
        order by s.company_id in (select company_id from ranked) desc, si.id_value
        limit %(limit)s""",
        {"source": QUOTE, "days": CLASSIFICATION_RETRY_DAYS, "limit": limit}).fetchall()
    return [r[0] for r in rows]


SECTORS = "nse_total_market_constituents"
NIFTY200 = "nse_nifty200_constituents"
SECTORS_EVERY = dt.timedelta(hours=20)


def ingest_daily_list(ctx: Context, source_id: str) -> JobResult | str:
    """An index constituent list, at most once in SECTORS_EVERY (the lists change at index
    reviews, twice a year), verified first if it never was."""
    last = ctx.conn.execute("""select max(fetched_at) from raw_payload
                               where source_id = %s and http_status = 200 and note <> %s""",
                            (source_id, PROBE_NOTE)).fetchone()[0]
    if last is not None and last > utc_now() - SECTORS_EVERY:
        return f"loaded {last.astimezone(IST):%d %b %H:%M} IST"
    why = unverified(ctx, ctx.sources.get(source_id))
    if why:
        return f"skipped: {why}; the weekly source check tries it again"
    return ingest_static(ctx, source_id)


def ingest_index_sectors(ctx: Context) -> JobResult | str:
    """The Nifty Total Market list's sectors (`_index_sectors`)."""
    return ingest_daily_list(ctx, SECTORS)


def unverified(ctx: Context, spec: SourceSpec) -> str | None:
    """Why an optional source a check uses can't be fetched now, or None if it can. One
    never verified (or whose URL changed) is verified now, as the weekly
    `igs sources verify` would; one whose latest verification failed waits for that job,
    as asking at every check would only be refused again."""
    v = latest_verification(ctx.store.root, spec.id)
    if v is None or v.url_template != spec.url:
        v = verify_source(spec, ctx.fetcher)
    return None if v.status == "verified" else f"{spec.id} is not verified ({v.message})"


def ingest_missing_classification(ctx: Context, limit: int = CLASSIFICATION_BATCH) -> str:
    """NSE's four-level classification for up to `limit` companies without one
    (missing_classification), so new listings and companies the one-off
    `igs ingest symbols nse_quote_equity` missed get industry peers. The quote API is
    refused to some servers, so while it is not verified the step only says so: companies
    keep their announcement label and index-list sector. A quote without a classification
    is skipped and counted; a refused host stops the batch with an error, keeping what
    loaded before it."""
    symbols = missing_classification(ctx.conn, limit)
    if not symbols:
        return "every current company has NSE's classification or was asked for recently"
    spec = ctx.sources.get(QUOTE)
    why = unverified(ctx, spec)
    if why:
        return f"skipped: {why}; the weekly source check tries it again"
    loaded, skipped = 0, []
    for symbol in symbols:
        try:
            got = fetch_and_load(ctx, spec, render_url(spec, symbol=symbol),
                                 {"symbol": symbol})
        except (nse.SchemaMismatch, SourceNotVerified) as exc:   # this quote's own layout
            ctx.conn.rollback()
            ctx.dq.emit("warn", "classification_unavailable", f"{symbol}: {exc}")
            skipped.append(symbol)
            continue
        if got.http_status != 200:
            skipped.append(symbol)
        loaded += got.rows
    return (f"{loaded} of {len(symbols)} companies classified"
            + (f"; no classification for {', '.join(skipped)}" if skipped else ""))


class MasterNotBuilt(RuntimeError):
    pass


def ingest_documents(ctx: Context, filing_type: str,
                     limit: int | None = None, *, since: dt.date | None = None,
                     newest_first: bool = False) -> list[JobResult]:
    """Fetch and load XBRL documents listed in filing_ref that are not loaded yet.

    Documents are reached only through a verified listing: the listing source
    for the reference's filing system must be verified, and every document
    must be well-formed XBRL (strict parse) or it is reported and skipped.

    Nothing is fetched while the instrument master is empty: every document would be
    rejected as unmapped and, having been fetched, not asked for again.
    """
    from igs.ingest.documents import process_document

    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if ctx.fetcher is None:
        raise RuntimeError("context has no fetcher")
    by_system = {s.options.get("filing_system"): s for s in ctx.sources.sources
                 if s.options.get("filing_system")}
    refs = pending_refs(ctx.conn, filing_type, limit, since=since, newest_first=newest_first)
    if refs:
        with ctx.conn.cursor() as cur:
            cur.execute("select exists(select 1 from security_identifier "
                        "where id_type = 'NSE_SYMBOL')")
            if not cur.fetchone()[0]:
                raise MasterNotBuilt(
                    f"{len(refs)} {filing_type} documents are waiting, but the instrument "
                    "master is empty, so none could be matched to a company. Load prices "
                    "and run `igs master rebuild` first.")
    out = []
    for ref in refs:
        listing = by_system.get(ref["filing_system"])
        if listing is None:
            raise KeyError(f"no listing source for filing system {ref['filing_system']}")
        require_verified(ctx.store.root, listing)
        rec = ctx.fetcher.get(DOCUMENT_SOURCE, ref["document_url"], "none",
                              params=ref_to_params(ref))
        ctx.store.index_record(ctx.conn, rec)
        ctx.conn.commit()
        rows = 0
        status = "download failed"
        if rec.http_status == 200:
            rows, status = process_document(ctx, rec)
        out.append(JobResult(DOCUMENT_SOURCE, ref["document_url"], rec.http_status, rows,
                             rec.fetch_id, status))
        log.info("Documents %d/%d: %s: %s (%d rows)", len(out), len(refs),
                 ref["symbol"], status, rows)
    return out


def pending_insider_refs(conn, limit: int | None = None) -> list[dict]:
    """Insider-trading disclosures whose XBRL has not been fetched successfully yet."""
    with conn.cursor() as cur:
        cur.execute(f"""select r.exchange, r.disclosure_id, r.symbol, r.company_name,
                               r.regulation, r.submission_type, r.filed_at, r.document_url
                        from insider_disclosure_ref r
                        where not exists (select 1 from raw_payload p
                                          where p.url = r.document_url and p.http_status = 200)
                        order by r.filed_at {'limit %s' if limit else ''}""",
                    (limit,) if limit else ())
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def ingest_insider_documents(ctx: Context, limit: int | None = None) -> list[JobResult]:
    """Fetch and load the XBRL of each listed insider-trading disclosure not loaded yet.

    Reached only through the verified listing. Each fetch record carries the listing row
    (broadcast time included), so a rebuild loads it without the listing table.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if ctx.fetcher is None:
        raise RuntimeError("context has no fetcher")
    refs = pending_insider_refs(ctx.conn, limit)
    if refs:
        require_verified(ctx.store.root, ctx.sources.get(INSIDER_LISTING))
    out = []
    for ref in refs:
        rec = ctx.fetcher.get(INSIDER_DOCUMENT, ref["document_url"], "none",
                              params=insider.ref_to_params(ref))
        ctx.store.index_record(ctx.conn, rec)
        ctx.conn.commit()
        rows, note = 0, "download failed"
        if rec.http_status == 200:
            with ctx.conn.transaction():
                rows = _insider_document(ctx, rec, ctx.store.read_bytes(rec))
            ctx.conn.commit()
            note = "loaded"
        out.append(JobResult(INSIDER_DOCUMENT, ref["document_url"], rec.http_status, rows,
                             rec.fetch_id, note))
        log.info("Insider disclosures %d/%d: %s: %s (%d trades)", len(out), len(refs),
                 ref["symbol"], note, rows)
    return out


def trading_days(conn, start: dt.date, end: dt.date) -> list[dt.date]:
    with conn.cursor() as cur:
        cur.execute("select holiday_date from trading_holiday where exchange = 'NSE' "
                    "and holiday_date between %s and %s", (start, end))
        holidays = {r[0] for r in cur.fetchall()}
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def price_source_for(ctx: Context, day: dt.date) -> str:
    cutover = ctx.sources.get("nse_cm_bhavcopy_udiff").options.get("udiff_from", "2024-07-08")
    return "nse_cm_bhavcopy_udiff" if day >= dt.date.fromisoformat(str(cutover)) \
        else "nse_cm_bhavcopy_legacy"


def backfill_prices(ctx: Context, start: dt.date, end: dt.date,
                    with_delivery: bool = True) -> list[JobResult]:
    out = []
    for day in trading_days(ctx.conn, start, end):
        out.append(ingest_date(ctx, price_source_for(ctx, day), day))
        if with_delivery:
            out.append(ingest_date(ctx, "nse_sec_bhavdata_full", day))
        out.append(ingest_date(ctx, "nse_index_close_all", day))
    return out


# --------------------------------------------------------------------------- rebuild

DERIVED_TABLES = [
    "price_eod", "corporate_action", "trading_holiday", "index_price", "surveillance_snapshot",
    "nse_equity_list", "bse_scrip", "broker_instrument", "announcement", "insider_trade",
    "insider_disclosure_ref", "index_sector", "index_member",
    "industry_classification", "security_listing", "security_identifier", "filing_ref",
    "shareholding", "fundamental_fact", "filing",
]


def _replay(ctx: Context, handlers: dict[str, Handler]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source_id, handler in handlers.items():
        for rec in ctx.store.iter_records(source_id):
            # Verification samples are never loaded live, so they are not replayed either.
            if rec.http_status not in (200, None) or rec.note == PROBE_NOTE:
                continue
            content = ctx.store.read_bytes(rec)
            with ctx.conn.transaction():
                counts[source_id] = counts.get(source_id, 0) + handler(ctx, rec, content)
    return counts


def rebuild_from_raw(ctx: Context, post_master: bool = True) -> dict[str, int]:
    """Truncate every derived table and replay all landed payloads."""
    from igs.normalize.master_db import rebuild_instrument_master

    ctx.store.reindex_into_db(ctx.conn)
    with ctx.conn.cursor() as cur:
        # AI excerpts cannot be replayed from the ingestion archive. Preserve their
        # original observation times and reconnect them by the exchange's natural key.
        cur.execute("""create temporary table saved_forward on commit drop as
            select d.*, a.exchange, a.symbol, a.filed_at, a.subject
            from forward_document d join announcement a using(ann_id)""")
        cur.execute("truncate forward_document, " + ", ".join(DERIVED_TABLES))
    counts = _replay(ctx, HANDLERS)
    with ctx.conn.cursor() as cur:
        cur.execute("""insert into forward_document
            select a.ann_id, d.company_id, d.published_at, d.received_at, d.source_url,
                   d.payload, d.text_content, d.content_sha256, d.assessed_at, d.model,
                   d.claims, d.attempts, d.retry_after, d.last_error
            from saved_forward d join announcement a
              on (a.exchange,a.symbol,a.filed_at,a.subject)=
                 (d.exchange,d.symbol,d.filed_at,d.subject)""")
        cur.execute("select count(*) from saved_forward")
        saved = cur.fetchone()[0]
        cur.execute("select count(*) from forward_document")
        if cur.fetchone()[0] != saved:
            raise ValueError("rebuild cannot reconnect all archived forward evidence")
    with ctx.conn.transaction():
        master = rebuild_instrument_master(ctx.conn, ctx.dq)
    counts["_securities"] = master["securities"]
    if post_master:
        counts.update(_replay(ctx, POST_MASTER_HANDLERS))
    ctx.conn.commit()
    return counts


def snapshot_counts(conn, tables: Iterable[str] = DERIVED_TABLES) -> pl.DataFrame:
    rows = []
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(f"select count(*) from {t}")
            rows.append({"table": t, "rows": cur.fetchone()[0]})
    return pl.DataFrame(rows)


def now_ist_date() -> dt.date:
    return utc_now().astimezone(IST).date()
