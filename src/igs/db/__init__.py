"""PostgreSQL connection and schema migrations (plain SQL, applied in order)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_URL = "postgresql://igs:igs@localhost:5432/igs"


class MigrationError(RuntimeError):
    pass


def database_url() -> str:
    return os.environ.get("IGS_DATABASE_URL", DEFAULT_URL)


def connect(url: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(url or database_url(), autocommit=autocommit)


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations. Returns the versions applied.

    An already-applied migration whose file has since changed is an error:
    migrations are immutable once applied, fixes go in a new file.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            create table if not exists schema_migrations (
                version     text primary key,
                sha256      char(64)    not null,
                applied_at  timestamptz not null default now()
            )
            """
        )
        cur.execute("select version, sha256 from schema_migrations")
        applied = dict(cur.fetchall())
    conn.commit()

    done: list[str] = []
    for path in _migration_files():
        version = path.stem
        sql = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(sql.encode()).hexdigest()
        if version in applied:
            if applied[version] != sha:
                raise MigrationError(f"migration {version} changed after being applied")
            continue
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "insert into schema_migrations (version, sha256) values (%s, %s)", (version, sha)
            )
        done.append(version)
    conn.commit()
    return done
