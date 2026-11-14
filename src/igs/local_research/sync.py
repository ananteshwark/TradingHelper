"""Pull a consistent server dump into an isolated local research database.

The server SSH key has a forced, read-only pg_dump command. This program never
connects to the production database with a write-capable URL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / "data" / "research-sync"
CLUSTER = ROOT / "data" / "research-pg" / "data"
SOCKET = ROOT / "data" / "research-pg" / "socket"
DB = "igs_research"
PREVIOUS = "igs_research_previous"
STAGE = "igs_research_stage"


def connection(database: str = "postgres") -> psycopg.Connection:
    conn = psycopg.connect(
        dbname=database,
        host=str(SOCKET),
        port=55438,
        autocommit=True,
    )
    actual = Path(conn.execute("select current_setting('data_directory')").fetchone()[0])
    if actual.resolve() != CLUSTER.resolve():
        conn.close()
        raise RuntimeError("Refusing a database outside the isolated research cluster")
    return conn


def status(details: dict) -> None:
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = STATE / "status.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(details, indent=2, default=str) + "\n")
    temp.chmod(0o600)
    os.replace(temp, path)


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def db_exists(conn: psycopg.Connection, name: str) -> bool:
    return (
        conn.execute("select 1 from pg_database where datname=%s", (name,)).fetchone() is not None
    )


def fetch(host: str, key: Path, archive: Path) -> None:
    if not host or not key.is_file():
        raise ValueError("Set --host and provide the dedicated SSH export key")
    partial = archive.with_suffix(".dump.partial")
    try:
        with partial.open("wb") as output:
            partial.chmod(0o600)
            proc = subprocess.run(
                [
                    "ssh",
                    "-T",
                    "-i",
                    str(key),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "ClearAllForwardings=yes",
                    host,
                    "export",
                ],
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=3600,
            )
            output.flush()
            os.fsync(output.fileno())
        if proc.returncode:
            raise RuntimeError(f"SSH export failed: {proc.stderr.decode(errors='replace')[-500:]}")
        if partial.stat().st_size < 1024:
            raise RuntimeError("Server returned an empty database dump")
        subprocess.run(
            ["pg_restore", "--list", str(partial)], check=True, stdout=subprocess.DEVNULL
        )
        os.replace(partial, archive)
    finally:
        partial.unlink(missing_ok=True)


def install(archive: Path) -> dict:
    if not archive.is_file():
        raise FileNotFoundError(archive)
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    subprocess.run(["pg_restore", "--list", str(archive)], check=True, stdout=subprocess.DEVNULL)
    with connection() as admin:
        if db_exists(admin, STAGE):
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(STAGE)))
        admin.execute(
            sql.SQL("create database {} template template0").format(sql.Identifier(STAGE))
        )
    try:
        run(
            "pg_restore",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            "--jobs=4",
            "--host",
            str(SOCKET),
            "--port",
            "55438",
            "--dbname",
            STAGE,
            str(archive),
        )
        with connection(STAGE) as candidate:
            migrations = candidate.execute("select count(*) from schema_migrations").fetchone()[0]
            tables = candidate.execute(
                "select count(*) from pg_class where relkind='r' "
                "and relnamespace='public'::regnamespace"
            ).fetchone()[0]
            if migrations < 50 or tables < 50:
                raise RuntimeError("Restored database lacks expected schema")
            paper = {}
            for table in ("ml_intraday_pick", "evening_pick", "intraday_spike_ml"):
                paper[table] = candidate.execute(
                    sql.SQL("select count(*) from {}").format(sql.Identifier(table))
                ).fetchone()[0]
        with connection() as admin:
            if db_exists(admin, DB):
                active = admin.execute(
                    "select count(*) from pg_stat_activity "
                    "where datname=%s and pid<>pg_backend_pid()",
                    (DB,),
                ).fetchone()[0]
                if active:
                    raise RuntimeError("Local research database is in use; retry after jobs finish")
            if db_exists(admin, PREVIOUS):
                admin.execute(
                    sql.SQL("drop database {} with (force)").format(sql.Identifier(PREVIOUS))
                )
            had_current = db_exists(admin, DB)
            if had_current:
                admin.execute(
                    sql.SQL("alter database {} rename to {}").format(
                        sql.Identifier(DB), sql.Identifier(PREVIOUS)
                    )
                )
            try:
                admin.execute(
                    sql.SQL("alter database {} rename to {}").format(
                        sql.Identifier(STAGE), sql.Identifier(DB)
                    )
                )
            except Exception:
                if had_current:
                    admin.execute(
                        sql.SQL("alter database {} rename to {}").format(
                            sql.Identifier(PREVIOUS), sql.Identifier(DB)
                        )
                    )
                raise
        result = {
            "synced_at": dt.datetime.now(dt.UTC).isoformat(),
            "archive_sha256": digest,
            "archive_bytes": archive.stat().st_size,
            "migrations": migrations,
            "tables": tables,
            "paper_rows": paper,
            "database": DB,
            "state": "ready",
        }
        status(result)
        return result
    except Exception:
        with connection() as admin:
            if db_exists(admin, STAGE):
                admin.execute(
                    sql.SQL("drop database {} with (force)").format(sql.Identifier(STAGE))
                )
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="SSH export host, e.g. root@server")
    parser.add_argument("--key", type=Path, default=STATE / "id_ed25519")
    parser.add_argument(
        "--archive", type=Path, help="Restore an already downloaded custom-format dump"
    )
    args = parser.parse_args(argv)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = STATE / "sync.lock"
    import fcntl

    with lock.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Research sync is already running", file=sys.stderr)
            return 2
        try:
            archive = args.archive or STATE / "latest.dump"
            if args.archive is None:
                fetch(args.host, args.key, archive)
            result = install(archive)
            print(json.dumps(result, indent=2))
            return 0
        except Exception as exc:
            existing = STATE / "status.json"
            previous = json.loads(existing.read_text()) if existing.is_file() else {}
            previous.update(
                state="ready" if previous.get("synced_at") else "failed",
                last_sync_failed_at=dt.datetime.now(dt.UTC).isoformat(),
                last_sync_error=str(exc),
            )
            status(previous)
            print(f"Research sync failed: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
