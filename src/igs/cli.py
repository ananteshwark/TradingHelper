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
