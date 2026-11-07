"""Full backups of the app and restoring one (igs.backup), with the download and upload
routes (igs.ui.backup_routes)."""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from igs import backup

pytestmark = pytest.mark.skipif(shutil.which("pg_dump") is None or os.name == "nt",
                                reason="needs PostgreSQL's client tools (pg_dump, psql)")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A repository's local state in tmp_path: data/, reports/, .streamlit/, the raw
    store, the backups folder; settings and .env from conftest's isolated ones."""
    from igs.config import settings_dir
    from igs.envfile import default_path
    root = tmp_path / "root"
    (root / "data" / "gates").mkdir(parents=True)
    (root / "data" / "gates" / "lookahead.json").write_text('{"ok": true}')
    (root / "data" / "intraday").mkdir()
    (root / "data" / "intraday" / "cache.json").write_text("cache")
    (root / "reports").mkdir()
    (root / "reports" / "coverage.json").write_text("{}")
    (root / ".streamlit").mkdir()
    (root / ".streamlit" / "secrets.toml").write_text('cookie_secret = "s"')
    raw = tmp_path / "raw"
    (raw / "blobs" / "ab").mkdir(parents=True)
    (raw / "blobs" / "ab" / "abc").write_bytes(b"downloaded")
    monkeypatch.setenv("IGS_RAW_ROOT", str(raw))
    monkeypatch.setenv("IGS_BACKUP_DIR", str(tmp_path / "backups"))
    settings_dir().mkdir(parents=True, exist_ok=True)
    (settings_dir() / "plan.yaml").write_text("drift_points: 3\n")
    default_path().write_text("IGS_TELEGRAM_TOKEN=old-secret\n")
    return SimpleNamespace(root=root, raw=raw, settings=settings_dir(), env=default_path(),
                           url=os.environ.get("IGS_TEST_DATABASE_URL", ""))


def _names(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as tar:
        return tar.getnames()


@pytest.mark.db
def test_a_backup_holds_the_database_and_files_and_the_options_only_when_asked(db_conn,
                                                                               home):
    db_conn.execute("insert into company(name) values('Kept')")
    db_conn.commit()
    plain = backup.create(home.url, root=home.root, log=lambda s: None)
    names = _names(plain)
    assert names[0] == "manifest.json" and "database.dump" in names
    assert {"files/settings/plan.yaml", "files/data/gates/lookahead.json",
            "files/reports/coverage.json"} <= set(names)
    assert not any(n.startswith(("secrets/", "raw/", "files/data/intraday",
                                 "files/data/backups")) for n in names)
    m = backup.read_manifest(plain)
    assert m["migrations"] == backup.code_migrations() and not m["with_secrets"]
    assert oct(plain.stat().st_mode & 0o777) == "0o600"
    full = backup.create(home.url, root=home.root, with_secrets=True, with_raw=True,
                         label="full", log=lambda s: None)
    names = _names(full)
    assert {"secrets/.env", "secrets/secrets.toml", "raw/blobs/ab/abc"} <= set(names)
    assert [b["name"] for b in backup.backups()] == [full.name, plain.name]
    assert "secrets, downloads" in backup.describe(backup.backups()[0])


@pytest.mark.db
def test_restoring_gives_back_the_database_and_settings_and_undoes_nothing_else(db_conn,
                                                                                home):
    db_conn.execute("insert into company(name) values('Before')")
    db_conn.commit()
    made = backup.create(home.url, root=home.root, with_secrets=True, with_raw=True,
                         log=lambda s: None)
    # Changes after the backup, all undone by the restore except the ones not asked for.
    db_conn.execute("insert into company(name) values('After')")
    db_conn.execute("create table made_later(x int)")
    db_conn.commit()
    (home.settings / "plan.yaml").write_text("drift_points: 9\n")
    (home.settings / "costs.yaml").write_text("capital_inr: 1\n")      # a later change
    (home.root / "data" / "gates" / "lookahead.json").unlink()
    home.env.write_text("IGS_TELEGRAM_TOKEN=new-secret\n")
    (home.raw / "blobs" / "ab" / "abc").unlink()
    (home.raw / "blobs" / "ab" / "new").write_bytes(b"later download")
    db_conn.rollback()
    lines = []
    backup.restore(made, home.url, root=home.root, with_raw=True, log=lines.append)
    assert db_conn.execute("select array_agg(name) from company").fetchone()[0] == ["Before"]
    assert db_conn.execute("select to_regclass('made_later')").fetchone()[0] is None
    assert (home.settings / "plan.yaml").read_text() == "drift_points: 3\n"
    assert not (home.settings / "costs.yaml").exists()       # as it was: the default
    assert (home.root / "data" / "gates" / "lookahead.json").exists()
    assert home.env.read_text() == "IGS_TELEGRAM_TOKEN=new-secret\n"   # not asked for
    assert (home.raw / "blobs" / "ab" / "abc").read_bytes() == b"downloaded"
    assert (home.raw / "blobs" / "ab" / "new").exists()        # downloads are only added
    safety = [b for b in backup.backups() if b["name"].endswith("-before-restore.tar.gz")]
    assert len(safety) == 1 and safety[0]["manifest"]["with_secrets"]
    assert any("Database restored" in line for line in lines)
    db_conn.rollback()
    backup.restore(made, home.url, root=home.root, with_secrets=True, safety=False,
                   log=lambda s: None)
    assert home.env.read_text() == "IGS_TELEGRAM_TOKEN=old-secret\n"
    # The safety backup undoes the restore.
    db_conn.rollback()
    backup.restore(safety[0]["path"], home.url, root=home.root, safety=False,
                   log=lambda s: None)
    names = db_conn.execute("select array_agg(name order by name) from company").fetchone()
    assert names[0] == ["After", "Before"]


@pytest.mark.db
def test_a_failed_replay_leaves_the_database_as_it_was(db_conn, home, tmp_path,
                                                       monkeypatch):
    db_conn.execute("insert into company(name) values('Before')")
    db_conn.commit()
    made = backup.create(home.url, root=home.root, log=lambda s: None)
    db_conn.execute("insert into company(name) values('After')")
    db_conn.commit()
    db_conn.rollback()
    fake = tmp_path / "pg_restore"
    fake.write_text('#!/bin/sh\ncase "$*" in *--list*) exec pg_restore "$@";; esac\n'
                    'echo "create table halfway(x int);"\nexit 1\n')
    fake.chmod(0o755)
    monkeypatch.setattr(backup, "PG_RESTORE", str(fake))
    with pytest.raises(backup.BackupError, match="pg_restore failed"):
        backup.restore(made, home.url, root=home.root, safety=False, log=lambda s: None)
    names = db_conn.execute("select array_agg(name order by name) from company").fetchone()
    assert names[0] == ["After", "Before"]
    assert db_conn.execute("select to_regclass('halfway')").fetchone()[0] is None


def _tar(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _manifest(**extra) -> bytes:
    return json.dumps({"format": 1, "name": "igs-backup-20261101T000000Z.tar.gz",
                       "created_at": "2026-11-01T00:00:00+00:00", "migrations": [],
                       **extra}).encode()


def test_files_that_are_not_backups_or_are_unsafe_are_refused(home, tmp_path):
    with pytest.raises(backup.BackupError, match="isn't an app backup"):
        backup.read_manifest(_tar(tmp_path / "a.tar.gz", {"other.txt": b"x"}))
    (tmp_path / "b.tar.gz").write_bytes(b"not a tar")
    with pytest.raises(backup.BackupError, match="can't be read"):
        backup.read_manifest(tmp_path / "b.tar.gz")
    with pytest.raises(backup.BackupError, match="newer app code"):
        backup.check(json.loads(_manifest(migrations=["999_from_the_future"])))
    no_sum = _tar(tmp_path / "e.tar.gz", {"manifest.json": _manifest()})
    with pytest.raises(backup.BackupError, match="no database checksum"):
        backup.restore(no_sum, "postgresql://unused/igs_test", root=home.root,
                       log=lambda s: None)
    evil = _tar(tmp_path / "c.tar.gz", {"manifest.json": _manifest(),
                                       "files/data/../../escaped": b"x"})
    with pytest.raises(backup.BackupError, match="unsafe name"):
        backup._extract(evil, wanted=lambda n: True, root=home.root, overwrite=True,
                        log=lambda s: None)
    sneaky = _tar(tmp_path / "d.tar.gz", {"manifest.json": _manifest(),
                                         "files/data/raw/blobs/ab/abc": b"replaced"})
    assert backup._extract(sneaky, wanted=lambda n: True, root=home.root, overwrite=True,
                           log=lambda s: None) == 0
    assert (home.raw / "blobs" / "ab" / "abc").read_bytes() == b"downloaded"


def test_only_the_newest_backups_are_kept(home, tmp_path):
    folder = backup.backup_dir()
    folder.mkdir(parents=True)
    for i in range(5):
        path = _tar(folder / f"igs-backup-2026110{i}T000000Z.tar.gz",
                    {"manifest.json": _manifest()})
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
    (folder / "upload-x.partial").write_bytes(b"stale")
    os.utime(folder / "upload-x.partial", (1_000_000, 1_000_000))
    gone = backup.prune(2)
    assert sorted(p.name for p in folder.iterdir()) == [
        "igs-backup-20261103T000000Z.tar.gz", "igs-backup-20261104T000000Z.tar.gz"]
    assert "upload-x.partial" in gone and len(gone) == 4


# --------------------------------------------------------------------------- the routes


@pytest.fixture
def client(home, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from igs.ui import backup_routes
    who = {"role": None}
    monkeypatch.setattr(backup_routes, "current", lambda token: SimpleNamespace(
        role=who["role"]) if who["role"] else None)
    app = Starlette(routes=backup_routes.routes())
    return TestClient(app, base_url="https://stocks.example"), who


def test_only_an_administrator_downloads_a_backup(client, tmp_path):
    http, who = client
    folder = backup.backup_dir()
    folder.mkdir(parents=True)
    _tar(folder / "igs-backup-20261101T000000Z.tar.gz", {"manifest.json": _manifest()})
    url = "/backups/download/igs-backup-20261101T000000Z.tar.gz"
    assert http.get(url, follow_redirects=False).status_code == 303   # to /signin
    who["role"] = "viewer"
    assert http.get(url, follow_redirects=False).status_code == 303
    who["role"] = "admin"
    got = http.get(url)
    assert got.status_code == 200 and "attachment" in got.headers["content-disposition"]
    assert got.content == (folder / "igs-backup-20261101T000000Z.tar.gz").read_bytes()
    assert http.get("/backups/download/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert http.get("/backups/download/other.tar.gz").status_code == 404


def test_an_upload_needs_the_pages_token_and_keeps_only_a_backup(client, tmp_path):
    http, who = client
    who["role"] = "admin"
    page = http.get("/backups/upload")
    assert page.status_code == 200 and "nonce-" in page.headers["content-security-policy"]
    token = page.cookies.get("igs_backup_upload")
    assert token and f'"{token}"' in page.text
    good = _tar(tmp_path / "mine.tar.gz", {"manifest.json": _manifest()}).read_bytes()
    assert http.post("/backups/upload", content=good).status_code == 400   # no token
    bad = http.post("/backups/upload", content=good, headers={
        "X-Upload-Token": token, "Origin": "https://evil.example"})
    assert bad.status_code == 400
    got = http.post("/backups/upload", content=good, headers={"X-Upload-Token": token})
    assert got.status_code == 200, got.text
    assert got.json()["name"] == "igs-backup-20261101T000000Z-uploaded.tar.gz"
    again = http.post("/backups/upload", content=good, headers={"X-Upload-Token": token})
    assert again.json()["name"] == "igs-backup-20261101T000000Z-uploaded-2.tar.gz"
    junk = http.post("/backups/upload", content=b"not a backup",
                     headers={"X-Upload-Token": token})
    assert junk.status_code == 422 and "can't be read" in junk.json()["error"]
    assert sorted(p.name for p in backup.backup_dir().iterdir()) == [
        "igs-backup-20261101T000000Z-uploaded-2.tar.gz",
        "igs-backup-20261101T000000Z-uploaded.tar.gz"]
    who["role"] = None
    assert http.post("/backups/upload", content=good,
                     headers={"X-Upload-Token": token}).status_code == 403


# --------------------------------------------------------------------------- the page


@pytest.mark.db
def test_the_maintenance_tab_makes_and_restores_backups(db_conn, home, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest

    from igs import schedules
    monkeypatch.setenv("IGS_DATABASE_URL", home.url)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home.root))
    started = []
    monkeypatch.setattr(schedules, "start_detached",
                        lambda unit, command: started.append(command) or "Started")
    folder = backup.backup_dir()
    folder.mkdir(parents=True)
    _tar(folder / "igs-backup-20261101T000000Z.tar.gz", {"manifest.json": _manifest(
        migrations=backup.code_migrations(), host="server", with_secrets=True)})
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    app = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"
    at = AppTest.from_file(str(app), default_timeout=60).run()
    at.sidebar.radio(key="page").set_value("Settings").run()
    tab = next(t for t in at.tabs if t.label == "Maintenance")
    assert any("igs-backup-20261101T000000Z.tar.gz" in m.value for m in tab.markdown)
    next(c for c in tab.checkbox if c.label.startswith("Include the secrets")).check()
    next(b for b in tab.button if b.label == "Make a backup now").click().run()
    assert not at.exception, at.exception
    assert started == [("backup", "create", "--with-secrets")]
    at.selectbox(key="backup_pick").select("igs-backup-20261101T000000Z.tar.gz").run()
    tab = next(t for t in at.tabs if t.label == "Maintenance")
    next(t for t in tab.text_input if t.label == "Type RESTORE to confirm").input("restore")
    next(b for b in tab.button if b.label == "Restore this backup").click().run()
    assert any("type RESTORE" in e.value for e in at.error) and len(started) == 1
    tab = next(t for t in at.tabs if t.label == "Maintenance")
    next(c for c in tab.checkbox if c.label.startswith("Also restore its secrets")).check()
    next(t for t in tab.text_input if t.label == "Type RESTORE to confirm").input("RESTORE")
    next(b for b in tab.button if b.label == "Restore this backup").click().run()
    assert not at.exception, at.exception
    assert started[-1] == ("backup", "restore", "igs-backup-20261101T000000Z.tar.gz",
                           "--yes", "--with-secrets")


def test_the_server_serves_the_backup_routes_beside_signin():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "server", Path(__file__).resolve().parents[1] / "src/igs/ui/server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    paths = {r.path for r in server.app._user_routes}       # st.App's own routes list
    assert {"/signin", "/backups/download/{name}", "/backups/upload"} <= paths


def test_restoring_from_the_command_line_needs_yes(home, capsys):
    from igs import cli
    folder = backup.backup_dir()
    folder.mkdir(parents=True)
    _tar(folder / "igs-backup-20261101T000000Z.tar.gz", {"manifest.json": _manifest()})
    assert cli.main(["backup", "restore", "igs-backup-20261101T000000Z.tar.gz"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert cli.main(["backup", "restore", "no-such-backup"]) == 1
    assert cli.main(["backup", "list"]) == 0
    assert "igs-backup-20261101T000000Z.tar.gz" in capsys.readouterr().out


@pytest.mark.db
def test_the_daily_cleanup_keeps_the_newest_backups_but_not_during_one(db_conn, home,
                                                                       tmp_path, monkeypatch):
    from igs import maintenance
    from igs.config import load_maintenance
    folder = backup.backup_dir()
    folder.mkdir(parents=True)
    for i in range(5):
        path = _tar(folder / f"igs-backup-2026110{i}T000000Z.tar.gz",
                    {"manifest.json": _manifest()})
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
    monkeypatch.setattr(backup, "job_running", lambda: True)
    busy = maintenance.cleanup(db_conn, load_maintenance(), logs=tmp_path / "logs",
                               units=tmp_path, raw=home.raw)
    assert "full backups: a backup or restore is running" in busy.skipped
    assert len(list(folder.iterdir())) == 5
    monkeypatch.setattr(backup, "job_running", lambda: False)
    rep = maintenance.cleanup(db_conn, load_maintenance(), logs=tmp_path / "logs",
                              units=tmp_path, raw=home.raw)
    assert rep.removed["app_backups"] == 2 and len(list(folder.iterdir())) == 3
    assert "2 old full backups" in maintenance.describe(rep.removed)
