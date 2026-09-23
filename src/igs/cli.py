"""Command line entry point: `igs <group> <command>`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from igs.guardrails import DISCLAIMER

REPO_ROOT = Path(__file__).resolve().parents[2]


def raw_root() -> Path:
    return Path(os.environ.get("IGS_RAW_ROOT", REPO_ROOT / "data" / "raw"))


def _db_migrate(args: argparse.Namespace) -> int:
    from igs.db import connect, migrate

    with connect() as conn:
        applied = migrate(conn)
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    return 0


def _raw_reindex(args: argparse.Namespace) -> int:
    from igs.db import connect
    from igs.ingest.raw_store import RawStore

    with connect() as conn:
        n = RawStore(raw_root()).reindex_into_db(conn)
        conn.commit()
    print(f"indexed {n} new fetch records")
    return 0


def _sources_list(args: argparse.Namespace) -> int:
    from igs.config import load_sources
    from igs.ingest.verify import latest_verification

    root = raw_root()
    print(f"{'source':32} {'tier':4} {'kind':10} {'status':10} checked_at / message")
    for s in load_sources().sources:
        v = latest_verification(root, s.id)
        status = "no url" if s.url is None else (v.status if v else "unverified")
        detail = f"{v.checked_at}  {v.message}" if v else ""
        print(f"{s.id:32} {s.tier:<4} {s.kind:10} {status:10} {detail}")
    return 0


def _sources_verify(args: argparse.Namespace) -> int:
    from igs.config import load_sources
    from igs.ingest.http import Fetcher
    from igs.ingest.raw_store import RawStore
    from igs.ingest.verify import verify_source

    cfg = load_sources()
    specs = [cfg.get(i) for i in args.ids] if args.ids else cfg.sources
    fetcher = Fetcher(RawStore(raw_root()))
    failed = 0
    for spec in specs:
        v = verify_source(spec, fetcher)
        failed += v.status != "verified"
        rows = f" rows={v.row_count}" if v.row_count is not None else ""
        print(f"{spec.id:32} {v.status.upper():9} {v.message}{rows}")
        if v.schema:
            print(f"{'':32} schema: {v.schema}")
    return 1 if failed else 0


def _gate_run(args: argparse.Namespace) -> int:
    from igs.pit.gate import GateError, run_gate

    try:
        rec = run_gate()
    except GateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"look-ahead gate PASSED ({rec.summary}); fingerprint {rec.fingerprint[:12]}")
    return 0


def _gate_check(args: argparse.Namespace) -> int:
    from igs.pit.gate import GateError, require_gate

    try:
        rec = require_gate()
    except GateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"look-ahead gate valid (passed {rec.passed_at})")
    return 0


def _date(s: str):
    import datetime as dt
    return dt.date.fromisoformat(s)


def _context(with_fetcher: bool = True):
    from igs.config import load_sources
    from igs.db import connect
    from igs.ingest.http import Fetcher
    from igs.ingest.jobs import Context
    from igs.ingest.raw_store import RawStore

    store = RawStore(raw_root())
    return Context(conn=connect(), store=store, sources=load_sources(),
                   fetcher=Fetcher(store) if with_fetcher else None)


def _finish(ctx, results) -> int:
    ctx.dq.persist(ctx.conn)
    ctx.conn.commit()
    bad = 0
    for r in results:
        flag = "ok" if r.http_status == 200 else "SKIP"
        bad += r.http_status not in (200, 404)
        print(f"{flag:4} {r.source_id:28} rows={r.rows:<7} HTTP {r.http_status} {r.url}")
    print(f"data-quality issues: {ctx.dq.count('error')} error, {ctx.dq.count('warn')} warn")
    return 1 if bad or ctx.dq.count("error") else 0


def _ingest_static(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import ingest_static
    ctx = _context()
    return _finish(ctx, [ingest_static(ctx, sid) for sid in args.ids])


def _ingest_prices(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import backfill_prices
    ctx = _context()
    return _finish(ctx, backfill_prices(ctx, _date(args.start), _date(args.end),
                                        with_delivery=not args.no_delivery))


def _ingest_range(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import ingest_range
    ctx = _context()
    return _finish(ctx, ingest_range(ctx, args.source, _date(args.start), _date(args.end)))


def _ingest_symbols(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import ingest_symbols
    ctx = _context()
    symbols = args.symbols
    if not symbols:
        with ctx.conn.cursor() as cur:
            cur.execute("select id_value from security_identifier "
                        "where id_type = 'NSE_SYMBOL' and valid_to is null order by 1")
            symbols = [r[0] for r in cur.fetchall()]
    return _finish(ctx, ingest_symbols(ctx, args.source, symbols))


def _master_rebuild(args: argparse.Namespace) -> int:
    from igs.normalize.master_db import rebuild_instrument_master
    ctx = _context(with_fetcher=False)
    with ctx.conn.transaction():
        stats = rebuild_instrument_master(ctx.conn, ctx.dq)
    ctx.dq.persist(ctx.conn)
    ctx.conn.commit()
    print(stats)
    return 0


def _rebuild(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import rebuild_from_raw
    ctx = _context(with_fetcher=False)
    counts = rebuild_from_raw(ctx)
    ctx.dq.persist(ctx.conn)
    ctx.conn.commit()
    for k, v in counts.items():
        print(f"{k:32} {v}")
    return 0


def _recon(args: argparse.Namespace) -> int:
    from igs.recon.checks import worst
    from igs.recon.run import run_reconciliation
    ctx = _context(with_fetcher=False)
    results, path = run_reconciliation(ctx.conn, _date(args.start), _date(args.end),
                                       REPO_ROOT / "reports", dq=ctx.dq)
    ctx.dq.persist(ctx.conn)
    ctx.conn.commit()
    for r in results:
        print(f"{r.status.upper():5} {r.name:24} {r.summary}")
    print(f"report: {path}")
    return 0 if worst(results) != "fail" else 1


def _import_screener(args: argparse.Namespace) -> int:
    from igs.ingest.manual import import_screener
    ctx = _context(with_fetcher=False)
    n = import_screener(ctx.conn, ctx.store, Path(args.path), ctx.dq, args.nse, args.bse)
    ctx.dq.persist(ctx.conn)
    ctx.conn.commit()
    print(f"loaded {n} enrichment values (tier 3; not used in factor math)")
    return 0


def _import_yfinance(args: argparse.Namespace) -> int:
    from igs.ingest.manual import import_yfinance
    ctx = _context(with_fetcher=False)
    n = import_yfinance(ctx.conn, ctx.store, args.symbols, _date(args.start), _date(args.end))
    print(f"loaded {n} fallback price rows (tier 3, UNVERIFIED)")
    return 0


def _ingest_documents(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import ingest_documents
    ctx = _context()
    return _finish(ctx, ingest_documents(ctx, args.kind, args.limit))


def _validate_fundamentals(args: argparse.Namespace) -> int:
    from igs.recon.checks import worst
    from igs.xbrl.report import validate_fundamentals
    ctx = _context(with_fetcher=False)
    results, path = validate_fundamentals(ctx.conn, REPO_ROOT / "reports")
    for r in results:
        print(f"{r.status.upper():5} {r.name:24} {r.summary}")
    print(f"report: {path}")
    return 0 if worst(results) != "fail" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="igs", description="IndiaGrowthScreener. " + DISCLAIMER)
    p.add_argument("-v", "--verbose", action="store_true")
    groups = p.add_subparsers(dest="group", required=True)

    db = groups.add_parser("db").add_subparsers(dest="cmd", required=True)
    db.add_parser("migrate", help="apply pending SQL migrations").set_defaults(fn=_db_migrate)

    raw = groups.add_parser("raw").add_subparsers(dest="cmd", required=True)
    raw.add_parser("reindex", help="rebuild raw_payload from fetch records on disk"
                   ).set_defaults(fn=_raw_reindex)

    src = groups.add_parser("sources").add_subparsers(dest="cmd", required=True)
    src.add_parser("list", help="registry and verification status").set_defaults(fn=_sources_list)
    ver = src.add_parser("verify", help="fetch a sample from each endpoint and record it")
    ver.add_argument("ids", nargs="*", help="source ids (default: all)")
    ver.set_defaults(fn=_sources_verify)

    ing = groups.add_parser("ingest").add_subparsers(dest="cmd", required=True)
    st = ing.add_parser("static", help="fetch and load static sources (masters, lists)")
    st.add_argument("ids", nargs="+")
    st.set_defaults(fn=_ingest_static)
    pr = ing.add_parser("prices", help="bhavcopy + delivery + index closes for a date range")
    pr.add_argument("--start", required=True)
    pr.add_argument("--end", required=True)
    pr.add_argument("--no-delivery", action="store_true")
    pr.set_defaults(fn=_ingest_prices)
    rg = ing.add_parser("range", help="date-range sources (corporate actions, announcements)")
    rg.add_argument("source")
    rg.add_argument("--start", required=True)
    rg.add_argument("--end", required=True)
    rg.set_defaults(fn=_ingest_range)
    sy = ing.add_parser("symbols", help="per-symbol sources (default: all current symbols)")
    sy.add_argument("source")
    sy.add_argument("symbols", nargs="*")
    sy.set_defaults(fn=_ingest_symbols)

    dc = ing.add_parser("documents", help="fetch and load XBRL documents from listings")
    dc.add_argument("kind", choices=["financial_results", "shareholding"])
    dc.add_argument("--limit", type=int)
    dc.set_defaults(fn=_ingest_documents)

    val = groups.add_parser("validate").add_subparsers(dest="cmd", required=True)
    val.add_parser("fundamentals", help="step 2 sign-off report (20 hand-checked companies)"
                   ).set_defaults(fn=_validate_fundamentals)

    master = groups.add_parser("master").add_subparsers(dest="cmd", required=True)
    master.add_parser("rebuild", help="rebuild instrument master from loaded prices"
                      ).set_defaults(fn=_master_rebuild)
    groups.add_parser("rebuild", help="truncate derived tables and replay the raw store"
                      ).set_defaults(fn=_rebuild)
    rc = groups.add_parser("recon", help="reconciliation report for a date range")
    rc.add_argument("--start", required=True)
    rc.add_argument("--end", required=True)
    rc.set_defaults(fn=_recon)

    imp = groups.add_parser("import").add_subparsers(dest="cmd", required=True)
    sc = imp.add_parser("screener", help="Screener.in CSV/Excel export (tier 3)")
    sc.add_argument("path")
    sc.add_argument("--nse")
    sc.add_argument("--bse")
    sc.set_defaults(fn=_import_screener)
    yf = imp.add_parser("yfinance", help="fallback price history (tier 3, unverified)")
    yf.add_argument("symbols", nargs="+")
    yf.add_argument("--start", required=True)
    yf.add_argument("--end", required=True)
    yf.set_defaults(fn=_import_yfinance)

    gate = groups.add_parser("gate").add_subparsers(dest="cmd", required=True)
    gate.add_parser("run", help="run look-ahead tests and record a pass").set_defaults(fn=_gate_run)
    gate.add_parser("check", help="is the recorded pass valid for current code?"
                    ).set_defaults(fn=_gate_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
