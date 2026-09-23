"""Ingestion jobs: fetch (behind the verification gate), land, parse, load.

Every source has one handler `handler(ctx, record, content) -> rows`. The same
handler runs for live ingestion and for `rebuild_from_raw`, which truncates
the derived tables and replays every landed payload in dependency order. That
is the proof that the database is fully rebuildable from the raw store.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import polars as pl
import psycopg

from igs.config import SourcesConfig, SourceSpec
from igs.dq import DQLog
from igs.ingest.http import Fetcher
from igs.ingest.raw_store import FetchRecord, RawStore
from igs.ingest.sources import render_url
from igs.ingest.verify import PROBE_NOTE, check_fingerprint, require_verified
from igs.normalize import nse
from igs.normalize.load import (
    load_announcements,
    load_corporate_actions,
    load_delivery,
    load_prices,
    load_simple,
)
from igs.normalize.masters import parse_angel_master, parse_bse_scrips
from igs.timeutil import IST, utc_now
from igs.xbrl.listing import ref_to_params
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
    "nse_financial_results_index": _listing,
    "nse_integrated_filing_index": _listing,
    "nse_shareholding_index": _listing,
}
POST_MASTER_HANDLERS: dict[str, Handler] = {"nse_quote_equity": _quote,
                                            "nse_xbrl_document": _document}
DOCUMENT_SOURCE = "nse_xbrl_document"


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


def ingest_symbols(ctx: Context, source_id: str, symbols: Iterable[str]) -> list[JobResult]:
    spec = ctx.sources.get(source_id)
    return [fetch_and_load(ctx, spec, render_url(spec, symbol=s), {"symbol": s})
            for s in symbols]


def ingest_documents(ctx: Context, filing_type: str,
                     limit: int | None = None) -> list[JobResult]:
    """Fetch and load XBRL documents listed in filing_ref that are not loaded yet.

    Documents are reached only through a verified listing: the listing source
    for the reference's filing system must be verified, and every document
    must be well-formed XBRL (strict parse) or it is reported and skipped.
    """
    if ctx.fetcher is None:
        raise RuntimeError("context has no fetcher")
    by_system = {s.options.get("filing_system"): s for s in ctx.sources.sources
                 if s.options.get("filing_system")}
    out = []
    for ref in pending_refs(ctx.conn, filing_type, limit):
        listing = by_system.get(ref["filing_system"])
        if listing is None:
            raise KeyError(f"no listing source for filing system {ref['filing_system']}")
        require_verified(ctx.store.root, listing)
        rec = ctx.fetcher.get(DOCUMENT_SOURCE, ref["document_url"], "none",
                              params=ref_to_params(ref))
        ctx.store.index_record(ctx.conn, rec)
        ctx.conn.commit()
        rows = 0
        if rec.http_status == 200:
            with ctx.conn.transaction():
                rows = _document(ctx, rec, ctx.store.read_bytes(rec))
        out.append(JobResult(DOCUMENT_SOURCE, ref["document_url"], rec.http_status, rows,
                             rec.fetch_id))
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
    "nse_equity_list", "bse_scrip", "broker_instrument", "announcement",
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
        cur.execute("truncate " + ", ".join(DERIVED_TABLES))
    counts = _replay(ctx, HANDLERS)
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
