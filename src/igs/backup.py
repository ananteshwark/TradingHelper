"""Full backups of the app, to keep on another computer, and restoring one (docs/BACKUP.md).

A backup is one gzipped tar file, `igs-backup-<UTC time>[-label].tar.gz`:

    manifest.json      what is in it: when, the app's commit, the database's migrations,
                       the size of each part, and the database dump's sha256
    database.dump      the app's PostgreSQL database (pg_dump --format=custom)
    files/settings/    the settings changed on the Settings page and the sign-in accounts
    files/data/        the app's other state in data/: the look-ahead gate's record and
                       the backtest status (DATA_PARTS); nothing else there
    files/reports/     the generated reports
    secrets/           with secrets: .env (API keys, tokens) and .streamlit/secrets.toml
    raw/               with the downloads: data/raw, every file downloaded from NSE and
                       the other sources (it can be many gigabytes)
    source.tar.gz      the app's code at that commit (git archive), for reference

The Maintenance tab makes one, lists them, and offers each for download; a backup kept
elsewhere can be uploaded back there and restored.

Restoring:
- **Checks first.** The file must be a backup, its database dump must match its
  checksum, and its database must not be from newer app code than this (deploy that code
  first).
- **A safety backup** of the current state (the database, files and secrets) is made
  first, so a restore can itself be undone.
- **Schedules paused.** The scheduled jobs are paused and running ones waited for; the data
  checks' lock is held throughout.
- **The database is replaced in one transaction:** the app's schema is dropped and the
  dump replayed. Any error rolls it all back and leaves the database as it was. Migrations
  then bring it up to this code.
- **Files** are read out of the backup first, before anything is changed, so a damaged
  file stops the restore before it starts; the settings are then replaced as they were
  at the backup, and the other files written back.
- **Secrets and downloads** are restored only when asked. Downloads are added to the raw
  store, never overwritten.
- The code itself is not restored: it comes from GitHub (docs/GITHUB_DEPLOYMENT.md).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from igs.timeutil import utc_now

REPO_ROOT = Path(__file__).resolve().parents[2]
FORMAT = 1
NAME = re.compile(r"^igs-backup-\d{8}T\d{6}Z(-[a-z0-9-]{1,40})?\.tar\.gz$")
# The parts of data/ backed up as files, besides the settings. Nothing else in data/ is:
# the downloads (raw/, an option of their own), the backups themselves, caches that are
# downloaded again (intraday/), fine-tuning and GPU-lab work, the notification spool, and
# the local research mirror (research-pg/, a live PostgreSQL cluster, and research-sync/,
# with its SSH key).
DATA_PARTS = ("gates", "backtest")
# The tools, by name; tests point them at fakes.
PG_DUMP, PG_RESTORE, PSQL = "pg_dump", "pg_restore", "psql"
Log = Callable[[str], None]


class BackupError(RuntimeError):
    """A backup or restore that can't go ahead; the message says why."""


def backup_dir() -> Path:
    return Path(os.environ.get("IGS_BACKUP_DIR", REPO_ROOT / "data" / "backups"))


def _stamp(now: dt.datetime | None = None) -> str:
    return (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------- PostgreSQL


def pg_env(url: str) -> dict[str, str]:
    """libpq's variables for the app's database URL: the password goes in the environment
    of the tools, never on a command line other users can read."""
    u = urlsplit(url)
    env = dict(os.environ)
    for key, value in (("PGHOST", u.hostname), ("PGPORT", u.port),
                       ("PGUSER", unquote(u.username or "")),
                       ("PGPASSWORD", unquote(u.password or "")),
                       ("PGDATABASE", unquote(u.path.lstrip("/")))):
        if value:
            env[key] = str(value)
    for key, values in (dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
                        .items()):
        if key == "sslmode":
            env["PGSSLMODE"] = values
    return env


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise BackupError(f"{name} isn't installed here (PostgreSQL's client tools)")
    return found


def migrations(conn) -> list[str]:
    try:
        rows = conn.execute("select version from schema_migrations order by 1").fetchall()
    except Exception:  # noqa: BLE001 - an empty database has none
        conn.rollback()
        return []
    conn.rollback()
    return [r[0] for r in rows]


def code_migrations() -> list[str]:
    from igs.db import _migration_files
    return [p.stem for p in _migration_files()]


# --------------------------------------------------------------------------- what is kept


def _walk(base: Path) -> Iterator[Path]:
    if base.is_file():
        yield base
        return
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def parts(*, root: Path | None = None, with_secrets: bool = False,
          with_raw: bool = False) -> dict[str, list[tuple[Path, str]]]:
    """Each part's files: (where it is, its name in the backup)."""
    from igs.cli import raw_root
    from igs.config import settings_dir
    from igs.envfile import default_path
    root = root or REPO_ROOT
    settings = settings_dir()
    out: dict[str, list[tuple[Path, str]]] = {"files": [], "secrets": [], "raw": []}
    for path in _walk(settings):
        out["files"].append((path, f"files/settings/{path.relative_to(settings).as_posix()}"))
    data = root / "data"
    for name in DATA_PARTS:
        for path in _walk(data / name):
            out["files"].append((path, f"files/data/{path.relative_to(data).as_posix()}"))
    for path in _walk(root / "reports"):
        out["files"].append((path, f"files/reports/"
                                   f"{path.relative_to(root / 'reports').as_posix()}"))
    if with_secrets:
        for path, name in ((default_path(), "secrets/.env"),
                           (root / ".streamlit" / "secrets.toml", "secrets/secrets.toml")):
            if path.is_file():
                out["secrets"].append((path, name))
    if with_raw:
        raw = raw_root()
        for path in _walk(raw):
            out["raw"].append((path, f"raw/{path.relative_to(raw).as_posix()}"))
    return out


def destination(name: str, *, root: Path | None = None) -> Path | None:
    """Where a member of a backup goes back to, or None for one that isn't restored as a
    file (the manifest, the dump, the source)."""
    from igs.cli import raw_root
    from igs.config import settings_dir
    from igs.envfile import default_path
    root = root or REPO_ROOT
    for prefix, base in (("files/settings/", settings_dir()), ("files/data/", root / "data"),
                         ("files/reports/", root / "reports"), ("raw/", raw_root())):
        if name.startswith(prefix):
            rel = name[len(prefix):]
            if prefix == "files/data/" and rel.split("/", 1)[0] not in DATA_PARTS:
                return None                     # never into the raw store, backups, caches
            return base / rel if rel else None
    return {"secrets/.env": default_path(),
            "secrets/secrets.toml": root / ".streamlit" / "secrets.toml"}.get(name)


def _safe(name: str) -> bool:
    """A member name that can only land where destination() puts it."""
    p = Path(name)
    return (bool(name) and not p.is_absolute() and "\\" not in name
            and ".." not in p.parts and not name.startswith("/"))


# --------------------------------------------------------------------------- making one


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _room(directory: Path, need: int, what: str) -> None:
    free = shutil.disk_usage(directory).free
    if free < need:
        raise BackupError(f"not enough disk space {what}: about {need / 2**30:.1f} GB may be "
                          f"needed in {directory}, {free / 2**30:.1f} GB is free")


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, timeout=60, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


SQLITE = (".sqlite3", ".sqlite", ".db")
SQLITE_SIDE = ("-wal", "-shm", "-journal")


def _sqlite_copy(path: Path, to: Path) -> Path:
    """A consistent copy of an SQLite file the app may be writing (the accounts)."""
    import sqlite3
    src, dst = sqlite3.connect(f"file:{path}?mode=ro", uri=True), sqlite3.connect(to)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    os.chmod(to, path.stat().st_mode & 0o777)       # the accounts file is 0600
    return to


def create(url: str | None = None, *, with_secrets: bool = False, with_raw: bool = False,
           label: str = "", root: Path | None = None, directory: Path | None = None,
           log: Log = print) -> Path:
    """Make one backup and return its path. It is written as `<name>.partial` and renamed
    when complete, so a backup listed is always whole."""
    from igs.db import connect, database_url
    url = url or database_url()
    root = root or REPO_ROOT
    directory = directory or backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    name = f"igs-backup-{_stamp()}" + (f"-{label}" if label else "") + ".tar.gz"
    if not NAME.match(name):
        raise BackupError(f"bad backup label {label!r}")
    found = parts(root=root, with_secrets=with_secrets, with_raw=with_raw)
    with connect(url) as conn:
        size = conn.execute("select pg_database_size(current_database())").fetchone()[0]
        applied = migrations(conn)
    sizes = {k: sum(p.stat().st_size for p, _ in v) for k, v in found.items()}
    # The dump is at most the database's size; it is checked again once its real size is
    # known, since the dump and the tar holding it are on the disk at the same time.
    _room(directory, int(size * 1.1), "for the database dump")
    work = directory / f".work-{name}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700)
    partial = directory / f"{name}.partial"
    try:
        log(f"Dumping the database ({size / 2**30:.2f} GB in PostgreSQL)...")
        dump = work / "database.dump"
        got = subprocess.run([_tool(PG_DUMP), "--format=custom", "--no-owner",
                              "--no-privileges", f"--file={dump}"], env=pg_env(url),
                             capture_output=True, text=True, check=False)
        if got.returncode != 0:
            raise BackupError(f"pg_dump failed: {got.stderr.strip()[:500]}")
        dumped = dump.stat().st_size
        _room(directory, int((dumped + sum(sizes.values())) * 1.1), "for the backup file")
        source = work / "source.tar.gz"
        commit = _git(root, "rev-parse", "HEAD")
        if commit:
            with source.open("wb") as out:
                subprocess.run(["git", "-C", str(root), "archive", "--format=tar.gz", "HEAD"],
                               stdout=out, check=False, timeout=600)
        manifest = {
            "format": FORMAT, "name": name, "created_at": utc_now().isoformat(),
            "host": socket.gethostname(), "commit": commit,
            "local_changes": bool(_git(root, "status", "--porcelain", "--untracked-files=no")),
            "migrations": applied,
            "database": {"bytes": dumped, "sha256": _sha256(dump), "postgres_bytes": size},
            "parts": {k: {"files": len(v), "bytes": sizes[k]} for k, v in found.items()},
            "with_secrets": bool(found["secrets"]), "with_raw": with_raw,
        }
        log(f"Writing {name}: {len(found['files'])} files"
            + (f", {len(found['secrets'])} secret files" if found["secrets"] else "")
            + (f", {len(found['raw'])} downloads ({sizes['raw'] / 2**30:.2f} GB)"
               if with_raw else "") + "...")
        with tarfile.open(partial, "w:gz", compresslevel=6) as tar:
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(data))
            tar.add(dump, "database.dump", recursive=False)
            dump.unlink()                  # in the tar now: its space back before the rest
            if source.is_file() and source.stat().st_size:
                tar.add(source, "source.tar.gz", recursive=False)
            for kind in ("files", "secrets", "raw"):
                for i, (path, arcname) in enumerate(found[kind]):
                    if path.name.endswith(SQLITE_SIDE):
                        continue                   # in the consistent copy below
                    try:
                        if path.suffix in SQLITE:
                            path = _sqlite_copy(path, work / f"sqlite-{i}{path.suffix}")
                        tar.add(path, arcname, recursive=False)
                    except FileNotFoundError:      # removed since it was listed
                        continue
        os.chmod(partial, 0o600)
        os.replace(partial, directory / name)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        partial.unlink(missing_ok=True)
    log(f"Backup ready: {name} ({(directory / name).stat().st_size / 2**20:.1f} MB)")
    return directory / name


# --------------------------------------------------------------------------- reading


def read_manifest(path: Path) -> dict:
    """The backup's manifest, its first member. Raises BackupError if it isn't one."""
    try:
        with tarfile.open(path, "r|gz") as tar:
            first = tar.next()
            if first is None or first.name != "manifest.json" or not first.isfile():
                raise BackupError(f"{path.name} isn't an app backup (no manifest first)")
            f = tar.extractfile(first)
            manifest = json.loads(f.read(1 << 20)) if f else {}
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise BackupError(f"{path.name} can't be read as a backup: {exc}") from exc
    if manifest.get("format") != FORMAT:
        raise BackupError(f"{path.name}: unknown backup format {manifest.get('format')!r}")
    return manifest


def backups(directory: Path | None = None) -> list[dict]:
    """Every complete backup here, newest file first: {path, name, bytes, modified,
    manifest} (manifest None, with `error`, for a file that isn't a readable backup)."""
    directory = directory or backup_dir()
    out = []
    if not directory.is_dir():
        return out
    for path in directory.iterdir():
        if not (path.is_file() and NAME.match(path.name)):
            continue
        st = path.stat()
        item = {"path": path, "name": path.name, "bytes": st.st_size,
                "modified": dt.datetime.fromtimestamp(st.st_mtime, dt.UTC), "manifest": None}
        try:
            item["manifest"] = read_manifest(path)
        except BackupError as exc:
            item["error"] = str(exc)
        out.append(item)
    return sorted(out, key=lambda b: b["modified"], reverse=True)


def prune(keep: int, directory: Path | None = None, *, dry_run: bool = False) -> list[str]:
    """All but the newest `keep` backups (by when the file was made or uploaded), and work
    left by a backup or upload that stopped a day ago or more. Returns what went."""
    directory = directory or backup_dir()
    gone: list[str] = []
    if not directory.is_dir():
        return gone
    for item in backups(directory)[keep:]:
        if not dry_run:
            item["path"].unlink(missing_ok=True)
        gone.append(item["name"])
    stale = time.time() - 86400
    for path in directory.iterdir():
        if (path.name.startswith(".work-") or path.name.endswith(".partial")) \
                and path.stat().st_mtime < stale:
            if not dry_run:
                shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
            gone.append(path.name)
    return gone


# --------------------------------------------------------------------------- restoring


def find(name_or_path: str) -> Path:
    """A backup by its name in the backups folder, or by its path."""
    path = Path(name_or_path)
    if NAME.match(name_or_path) and (backup_dir() / name_or_path).is_file():
        return backup_dir() / name_or_path
    if path.is_file():
        return path
    raise BackupError(f"no backup {name_or_path!r} in {backup_dir()}")


def check(manifest: dict) -> None:
    """Refuse a backup whose database is from newer app code than this."""
    newer = sorted(set(manifest.get("migrations", [])) - set(code_migrations()))
    if newer:
        raise BackupError(f"this backup's database is from newer app code (migration "
                          f"{newer[-1]}); deploy that code first, then restore")


def _replace_database(url: str, dump: Path, log: Log) -> None:
    """Drop the app's schema and replay the dump, in one transaction: psql gets the whole
    script or is stopped, and the server then rolls everything back."""
    env = pg_env(url)
    listing = subprocess.run([_tool(PG_RESTORE), "--list", str(dump)], env=env,
                             capture_output=True, text=True, check=False)
    if listing.returncode != 0:
        raise BackupError(f"pg_restore can't read the dump: {listing.stderr.strip()[:500]}")
    # The schema itself is made here; the dump's own entries for it would clash.
    keep = [line for line in listing.stdout.splitlines()
            if " SCHEMA - public " not in line + " "
            and "COMMENT - SCHEMA public" not in line]
    order = dump.with_suffix(".list")
    order.write_text("\n".join(keep) + "\n", encoding="utf-8")
    errors = dump.with_suffix(".psql.log")
    replay_log = dump.with_suffix(".pg_restore.log")
    with errors.open("wb") as err, replay_log.open("wb") as replay_err:
        psql = subprocess.Popen([_tool(PSQL), "-X", "-q", "-v", "ON_ERROR_STOP=1",
                                 "--single-transaction", "-f", "-"], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        assert psql.stdin is not None
        ok, replay = False, None
        try:
            psql.stdin.write(b"set lock_timeout = '60s';\n"
                             b"drop schema if exists public cascade;\n"
                             b"create schema public;\n")
            # Its warnings go to a file: a pipe nobody reads while the output is copied
            # would fill and stop it.
            replay = subprocess.Popen([_tool(PG_RESTORE), "--no-owner", "--no-privileges",
                                       f"--use-list={order}", "--file=-", str(dump)], env=env,
                                      stdout=subprocess.PIPE, stderr=replay_err)
            assert replay.stdout is not None
            shutil.copyfileobj(replay.stdout, psql.stdin, 1 << 20)
            replay.stdout.close()
            if replay.wait() != 0:
                raise BackupError("pg_restore failed: " + replay_log.read_text(
                    encoding="utf-8", errors="replace").strip()[-500:])
            ok = True
        except BrokenPipeError:
            pass                                     # psql stopped: its error is logged
        finally:
            if ok:
                try:
                    psql.stdin.close()
                except BrokenPipeError:
                    ok = False
            else:
                psql.kill()                          # nothing is committed
            code = psql.wait()
            if replay is not None and replay.poll() is None:
                replay.kill()
                replay.wait()
    message = errors.read_text(encoding="utf-8", errors="replace")
    if not ok or code != 0:
        raise BackupError("the database was not changed: "
                          + (message.strip().splitlines()[-1][:500] if message.strip()
                             else f"psql stopped ({code})"))
    log("Database restored.")


@contextmanager
def _reading(path: Path) -> Iterator[tarfile.TarFile]:
    """The backup open for streaming; a damaged file is a BackupError, not a traceback."""
    try:
        with tarfile.open(path, "r|gz") as tar:
            yield tar
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise BackupError(f"{path.name} is damaged or incomplete: {exc}") from exc


def _stage(tar_path: Path, *, wanted: Callable[[str], bool], root: Path | None,
           stage: Path) -> list[str]:
    """Read the members `wanted` out of the backup into `stage` (under their own names),
    before anything on the computer is touched; returns their names. A damaged file or an
    unsafe name stops here, with nothing changed."""
    names: list[str] = []
    with _reading(tar_path) as tar:
        for member in tar:
            if not wanted(member.name) or not member.isfile():
                continue
            if not _safe(member.name):
                raise BackupError(f"unsafe name in the backup: {member.name!r}")
            if destination(member.name, root=root) is None:
                continue
            src = tar.extractfile(member)
            if src is None:
                continue
            target = stage / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.chmod(target, member.mode & 0o777 or 0o600)
            names.append(member.name)
    return names


def _place(stage: Path, names: list[str], *, root: Path | None, overwrite: bool,
           log: Log) -> int:
    """Move staged members to where they belong; returns how many."""
    n = 0
    for name in names:
        target = destination(name, root=root)
        if target is None or (target.exists() and not overwrite):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".restoring")
        shutil.move(str(stage / name), str(tmp))      # may cross file systems
        os.replace(tmp, target)
        n += 1
    log(f"Restored {n} file(s).")
    return n


def _extract(tar_path: Path, *, wanted: Callable[[str], bool], root: Path | None,
             overwrite: bool, log: Log) -> int:
    """Write the members `wanted` back to where they belong, all read first."""
    stage = tar_path.parent / f".work-extract-{_stamp()}-{os.getpid()}"
    stage.mkdir(mode=0o700, parents=True)
    try:
        names = _stage(tar_path, wanted=wanted, root=root, stage=stage)
        return _place(stage, names, root=root, overwrite=overwrite, log=log)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


PAUSED = "paused-timers.txt"          # in the backups folder while a restore pauses them


def _active_timers() -> list[str]:
    from igs import schedules
    got = schedules._systemctl("list-units", "--type=timer", "--state=active", "--no-legend",
                               "--plain", "igs-*")
    if got is None or got.returncode != 0:
        return []
    return [line.split()[0] for line in got.stdout.splitlines() if line.strip()]


def resume_paused_timers(log: Log = print) -> list[str]:
    """Start again the timers a restore paused and never restarted (its process was killed
    before its end), unless a backup or restore is running now. Returns what was started."""
    from igs import schedules
    record = backup_dir() / PAUSED
    if not record.is_file() or job_running():
        return []
    timers = [t for t in record.read_text(encoding="utf-8").split() if t.startswith("igs-")]
    if timers:
        schedules._systemctl("start", *timers)
        log(f"Started again the scheduled jobs a restore left paused: {', '.join(timers)}")
    record.unlink(missing_ok=True)
    return timers


@contextmanager
def _paused(log: Log) -> Iterator[None]:
    """The scheduled jobs paused (timers stopped, running jobs waited for), where systemd
    runs them; started again afterwards whatever happens. Which were stopped is written
    to PAUSED first, so a restore killed before its end leaves a record that the next
    cleanup (resume_paused_timers) acts on."""
    from igs import schedules
    record = backup_dir() / PAUSED
    timers = _active_timers()
    if record.is_file():                  # left by an earlier restore that was killed
        timers = sorted(set(timers) | set(record.read_text(encoding="utf-8").split()))
    if timers:
        log(f"Pausing {len(timers)} scheduled job(s)...")
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("\n".join(timers) + "\n", encoding="utf-8")
        schedules._systemctl("stop", *timers)
        deadline = time.monotonic() + 1800
        while True:
            busy = schedules._systemctl("list-units", "--type=service", "--no-legend",
                                        "--plain", "--state=running,activating", "igs-*")
            running = ([line.split()[0] for line in busy.stdout.splitlines()
                        if line.strip() and not line.startswith(
                            ("igs-ui.", "igs-trade-monitor.", "igs-backup"))]
                       if busy is not None else [])
            if not running:
                break
            if time.monotonic() > deadline:
                schedules._systemctl("start", *timers)
                record.unlink(missing_ok=True)
                raise BackupError("jobs still running after 30 minutes: " + ", ".join(running))
            time.sleep(5)
    try:
        yield
    finally:
        if timers:
            schedules._systemctl("start", *timers)
            record.unlink(missing_ok=True)
            log("Scheduled jobs started again.")


def restore(path: Path, url: str | None = None, *, with_secrets: bool = False,
            with_raw: bool = False, safety: bool = True, root: Path | None = None,
            log: Log = print) -> dict:
    """Restore a backup (see the module's notes). Returns its manifest."""
    from igs.config import settings_dir
    from igs.db import connect, database_url, migrate
    from igs.sync import LOCK_KEY
    url = url or database_url()
    manifest = read_manifest(path)
    check(manifest)
    expected = (manifest.get("database") or {}).get("sha256") \
        if isinstance(manifest.get("database"), dict) else None
    if not expected:
        raise BackupError(f"{path.name}: its manifest has no database checksum")
    work = path.parent / f".work-restore-{_stamp()}"
    work.mkdir(mode=0o700, parents=True)
    try:
        dump = work / "database.dump"
        with _reading(path) as tar:
            for member in tar:
                if member.name == "database.dump" and member.isfile():
                    src = tar.extractfile(member)
                    with dump.open("wb") as out:
                        shutil.copyfileobj(src, out, 1 << 20)
                    break
        if not dump.is_file() or _sha256(dump) != expected:
            raise BackupError(f"{path.name}: its database dump is missing or damaged")
        stage = work / "files"
        stage.mkdir(mode=0o700)
        staged = _stage(path, root=root, stage=stage,
                        wanted=lambda n: n.startswith("files/")
                        or (with_secrets and n.startswith("secrets/")))
        if safety:
            log("Making a safety backup of the current state first...")
            create(url, with_secrets=True, label="before-restore", root=root, log=log)
        with _paused(log), connect(url) as lock:
            log("Waiting for any data check to finish...")
            lock.execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
            lock.commit()
            try:
                _replace_database(url, dump, log)
                with connect(url) as conn:
                    done = migrate(conn)
                if done:
                    log(f"Migrations applied to bring it up to this code: {', '.join(done)}")
                settings = settings_dir()
                if settings.is_dir():               # as it was: absence means the default
                    for old in _walk(settings):
                        old.unlink()
                _place(stage, staged, root=root, overwrite=True, log=log)
                if with_raw and manifest.get("with_raw"):
                    _extract(path, root=root, overwrite=False, log=log,
                             wanted=lambda n: n.startswith("raw/"))
            finally:
                lock.rollback()
                lock.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
                lock.commit()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log(f"Restored {path.name} (made {manifest.get('created_at', '?')}).")
    return manifest


def job_running() -> bool:
    """Whether a backup or restore is running: the job script's lock for `igs backup`."""
    if os.name == "nt":
        return False
    from igs.maintenance import LOGS, _job_lock
    LOGS.mkdir(exist_ok=True)
    with _job_lock(LOGS / "backup.lock") as free:
        return not free


def last_log(lines: int = 25) -> str:
    """The end of the last backup or restore's log (logs/backup.log)."""
    from igs.schedules import Job, last_entry
    return last_entry(Job("igs-backup", "", ("backup",), ""), lines)


def describe(item: dict) -> str:
    """One line about a backup for the page."""
    m = item.get("manifest")
    size = item["bytes"] / 2**20
    if m is None:
        return f"{size:,.1f} MB; not readable: {item.get('error', '')}"
    has = ["database", "settings and files"]
    if m.get("with_secrets"):
        has.append("secrets")
    if m.get("with_raw"):
        has.append("downloads")
    commit = (m.get("commit") or "")[:7]
    return (f"{size:,.1f} MB · made {str(m.get('created_at', '?'))[:16].replace('T', ' ')} UTC"
            + (f" at {commit}" if commit else "") + " · " + ", ".join(has))
