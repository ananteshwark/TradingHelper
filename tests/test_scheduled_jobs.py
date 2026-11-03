"""The scheduled jobs: what scripts/install-schedules.sh installs, and the Settings page's
view of them with Run now (igs.schedules)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from igs import schedules

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install-schedules.sh"


# --------------------------------------------------------------------------- the installer


@pytest.fixture
def install(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"
    fake = bin_dir / "systemctl"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    fake.chmod(0o755)
    units = tmp_path / "config" / "systemd" / "user"

    def run():
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                   XDG_CONFIG_HOME=str(tmp_path / "config"))
        log.write_text("")
        result = subprocess.run(["bash", str(INSTALL)], env=env, capture_output=True,
                                text=True, timeout=30)
        return result, log.read_text()
    return run, units


@pytest.mark.skipif(os.name == "nt", reason="the installer is for Linux's systemd")
def test_the_installer_installs_every_scheduled_job_and_switches_its_timer_on(install):
    run, units = install
    result, calls = run()
    assert result.returncode == 0, result.stderr
    shipped = sorted(p.name for p in (ROOT / "scripts" / "systemd").glob("igs-*.timer")
                     if p.with_suffix(".service").is_file())
    assert {"igs-daily.timer", "igs-verify.timer", "igs-maintenance.timer",
            "igs-news.timer"} <= set(shipped)
    for timer in shipped:
        assert (units / timer).read_bytes() == (ROOT / "scripts/systemd" / timer).read_bytes()
        assert (units / timer.replace(".timer", ".service")).is_file()
    enabled = next(c for c in calls.splitlines() if c.startswith("--user enable --now"))
    assert sorted(enabled.split()[3:]) == shipped
    assert not (units / "igs-trade-monitor.service").exists()   # not a timer; opt-in


@pytest.mark.skipif(os.name == "nt", reason="the installer is for Linux's systemd")
def test_an_installed_daily_job_keeps_its_own_schedule_and_others_are_backed_up(install):
    run, units = install
    units.mkdir(parents=True)
    own = "[Timer]\nOnCalendar=*-*-* 00/4:30:00 Asia/Kolkata\n"
    (units / "igs-daily.timer").write_text(own)
    (units / "igs-news.timer").write_text("[Timer]\nOnCalendar=hourly\n")
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert "Kept the installed igs-daily" in result.stdout
    assert (units / "igs-daily.timer").read_text() == own
    assert not (units / "igs-daily.service").exists()          # left as the owner set it
    enabled = next(c for c in calls.splitlines() if c.startswith("--user enable --now"))
    assert "igs-daily.timer" not in enabled and "igs-verify.timer" in enabled
    backups = list(units.glob("igs-news.timer.backup.*"))
    assert len(backups) == 1 and backups[0].read_text() == "[Timer]\nOnCalendar=hourly\n"
    result, _ = run()                                          # rerun: nothing changes
    assert result.returncode == 0 and len(list(units.glob("igs-news.timer.backup.*"))) == 1


# --------------------------------------------------------------------------- the jobs


def test_every_shipped_timer_is_a_job_with_its_command():
    found = {j.name: j for j in schedules.jobs()}
    assert "igs-code-watch" not in found                       # restarts the app itself
    assert found["igs-daily"].command == ("daily",)
    assert found["igs-verify"].command == ("sources", "verify")
    assert found["igs-news"].command == ("news", "process", "--limit", "10")
    assert found["igs-maintenance"].command == ("maintenance", "cleanup", "--trigger",
                                                "scheduled")
    assert found["igs-daily"].calendar == "Mon..Fri 20:30 Asia/Kolkata"
    assert found["igs-notify"].calendar == "every 1min after its last run"
    assert found["igs-news"].log == ROOT / "logs" / "news.log"
    assert all(j.description and j.calendar for j in found.values())


SHOW = """Id=igs-news.timer
LoadState=loaded
ActiveState=active
Result=success
NextElapseUSecRealtime=Tue 2026-11-03 10:05:00 IST
LastTriggerUSec=Tue 2026-11-03 09:50:00 IST

Id=igs-news.service
LoadState=loaded
ActiveState=inactive
Result=exit-code
ExecMainStartTimestamp=Tue 2026-11-03 09:50:01 IST

Id=igs-daily.timer
LoadState=not-found
ActiveState=inactive

Id=igs-daily.service
LoadState=not-found
ActiveState=inactive
"""


def test_systemd_is_asked_once_for_every_job(monkeypatch):
    found = [j for j in schedules.jobs() if j.name in ("igs-news", "igs-daily")]
    calls = []

    def systemctl(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=SHOW, stderr="")
    monkeypatch.setattr(schedules, "_systemctl", systemctl)
    got = schedules.states(found)
    assert len(calls) == 1 and set(got) == {"igs-news"}       # daily isn't installed here
    news = next(j for j in found if j.name == "igs-news")
    daily = next(j for j in found if j.name == "igs-daily")
    assert schedules.summary(news, got["igs-news"]) == (
        "Last run Tue 2026-11-03 09:50:01 IST (exit-code). Next Tue 2026-11-03 10:05:00 IST.")
    assert schedules.summary(daily, None) == (
        "Not installed here; its timer: Mon..Fri 20:30 Asia/Kolkata.")
    running = got["igs-news"] | {"service.ActiveState": "activating"}
    assert schedules.summary(news, running).startswith("Running now.")
    off = got["igs-news"] | {"timer.ActiveState": "inactive"}
    assert schedules.summary(news, off).endswith("Its timer is off.")
    monkeypatch.setattr(schedules, "_systemctl", lambda *a, **k: None)
    assert schedules.states(found) == {}                       # no systemd: nothing known


def test_run_now_goes_through_systemd_where_the_job_is_installed(monkeypatch):
    news = next(j for j in schedules.jobs() if j.name == "igs-news")
    calls = []

    def systemctl(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(schedules, "_systemctl", systemctl)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("not the script"))
    assert "through systemd" in schedules.run_now(news, {"service.LoadState": "loaded"})
    assert calls == [("start", "--no-block", "igs-news.service")]
    busy = {"service.LoadState": "loaded", "service.ActiveState": "activating"}
    assert "running already" in schedules.run_now(news, busy)
    assert len(calls) == 1                                     # not started twice
    monkeypatch.setattr(schedules, "_systemctl", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="Unit igs-news.service is masked."))
    with pytest.raises(RuntimeError, match="masked"):
        schedules.run_now(news, {"service.LoadState": "loaded"})


def test_run_now_starts_the_job_script_where_systemd_has_no_such_job(monkeypatch):
    verify = next(j for j in schedules.jobs() if j.name == "igs-verify")
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: started.append((args, k)))
    assert "logs/sources.log" in schedules.run_now(verify, None)
    (args, kwargs), = started
    assert args[1:] == ["sources", "verify"] and args[0].endswith(
        "igs-job.cmd" if os.name == "nt" else "igs-job.sh")
    assert kwargs["cwd"] == schedules.REPO_ROOT


def test_the_last_log_entry_starts_at_its_first_line(tmp_path, monkeypatch):
    monkeypatch.setattr(schedules, "REPO_ROOT", tmp_path)
    news = schedules.Job("igs-news", "news", ("news", "process"), "hourly")
    assert schedules.last_entry(news) == ""
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "news.log").write_text(
        "=== 2026-11-03 09:35:00 IST igs news process\nold\n"
        "=== 2026-11-03 09:50:00 IST igs news process\ncollected 4\nassessed 2\n")
    assert schedules.last_entry(news) == (
        "=== 2026-11-03 09:50:00 IST igs news process\ncollected 4\nassessed 2")


@pytest.mark.db
def test_the_schedules_tab_runs_a_job_now(db_conn, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    monkeypatch.setattr(schedules, "_systemctl", lambda *a, **k: None)     # no systemd
    ran = []

    def run_now(job, state):
        ran.append((job.name, state))
        return f"Started `{job.shown}` with igs-job.sh"
    monkeypatch.setattr(schedules, "run_now", run_now)
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(ROOT / "src/igs/ui/app.py"), default_timeout=60).run()
    at.sidebar.radio(key="page").set_value("Settings").run()
    assert not at.exception, at.exception
    tab = next(t for t in at.tabs if t.label == "Schedules")
    assert any("systemd has none of these jobs" in i.value for i in tab.info)
    assert any("Not installed here" in c.value and "igs news process" in c.value
               for c in tab.caption)
    at.button(key="sched_run:igs-news").click().run()
    assert not at.exception, at.exception
    assert ran == [("igs-news", None)]
    assert any("Started `igs news process --limit 10`" in s.value for s in at.success)
