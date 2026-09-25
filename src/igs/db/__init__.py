"""PostgreSQL connection and schema migrations (plain SQL, applied in order)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_URL = "postgresql://igs:igs@localhost:5432/igs"


class MigrationError(RuntimeError):
    pass


def database_url() -> str:
    return os.environ.get("IGS_DATABASE_URL", DEFAULT_URL)


def connect(url: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(url or database_url(), autocommit=autocommit)


def masked(url: str) -> str:
    """The URL with its password replaced, safe to print."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    return urlunsplit(parts._replace(
        netloc=parts.netloc.replace(f":{parts.password}@", ":***@", 1)))


def connection_help(exc: Exception, url: str, source: str) -> str:
    """A readable explanation of a failed connection: what was tried, where the URL came
    from, and what usually fixes it."""
    text = str(exc)
    user = urlsplit(url).username or "igs"
    db = urlsplit(url).path.lstrip("/") or "igs"
    first = text.strip().splitlines()[0] if text.strip() else type(exc).__name__
    reason = first.rsplit("failed: ", 1)[-1].removeprefix("FATAL:").strip()
    lines = [f"Could not connect to PostgreSQL: {reason}",
             f"URL used: {masked(url)}", f"It came from: {source}", ""]
    as_admin = ("(Ubuntu: sudo -u postgres psql -c \"...\"; Windows: psql -U postgres "
                "-c \"...\")")
    if "password authentication failed" in text:
        lines += [f"The password in that URL does not match the one set for the {user!r} "
                  "role, or the role does not exist. Set the same password in both places, "
                  f"as the postgres administrator {as_admin}:",
                  f"  ALTER ROLE {user} WITH LOGIN PASSWORD 'your-password';",
                  f"  (or, if the role is missing: CREATE ROLE {user} LOGIN PASSWORD "
                  "'your-password';)",
                  f"and in .env: IGS_DATABASE_URL=postgresql://{user}:your-password@"
                  f"localhost:5432/{db}",
                  "Avoid @ : / ? # % in the password: they break the URL. A shell "
                  "variable IGS_DATABASE_URL overrides .env (unset it)."]
    elif f'database "{db}" does not exist' in text:
        lines += [f"Create it as the postgres administrator: createdb -O {user} {db} "
                  "(Ubuntu: sudo -u postgres createdb ...)."]
    elif "role" in text and "does not exist" in text:
        lines += [f"Create the role as the postgres administrator {as_admin}:",
                  f"  CREATE ROLE {user} LOGIN PASSWORD 'your-password';"]
    elif any(k in text for k in ("Connection refused", "could not connect",
                                 "No such file or directory", "timeout")):
        lines += ["PostgreSQL is not running there, or not on that host and port. "
                  "Ubuntu: sudo systemctl start postgresql. Windows: start the "
                  "postgresql-x64-16 service (services.msc)."]
    lines.append("See docs/DEPLOY.md, Part 5 (troubleshooting).")
    return "\n".join(lines)


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
