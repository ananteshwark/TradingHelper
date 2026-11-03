"""The app's scheduled jobs, for the Settings page: what each runs and when, its state in
systemd, its last log entry, and Run now (docs/SCHEDULES.md).

The jobs are the timers in scripts/systemd: each igs-X.timer with its igs-X.service, whose
command runs through scripts/igs-job.sh. scripts/install-schedules.sh installs them.

Run now starts the job's own service through systemd where it is installed. The job then
runs outside the app, so restarting the app doesn't stop it, the deployment script waits
for it, and its output goes to the job's log as on its timer. Where systemd has no such
unit (the schedules aren't installed, or Windows), it starts the job script itself.
Either way the job script's lock keeps a job from running twice at once.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UNITS = REPO_ROOT / "scripts" / "systemd"
INTERNAL = ("igs-code-watch",)        # not a job: it restarts the app after a deployment
PROPERTIES = ("Id", "LoadState", "ActiveState", "Result", "ExecMainStartTimestamp",
              "NextElapseUSecRealtime", "LastTriggerUSec")
RUNNING = ("active", "activating", "reloading", "deactivating")


@dataclass(frozen=True)
class Job:
    name: str                  # the units' name, e.g. igs-news
    description: str           # the service's Description
    command: tuple[str, ...]   # what igs-job.sh runs, e.g. ("news", "process", "--limit", "10")
    calendar: str              # when its timer fires, as shipped

    @property
    def log(self) -> Path:
        return REPO_ROOT / "logs" / f"{self.command[0]}.log"

    @property
    def shown(self) -> str:
        return "igs " + " ".join(self.command)


def _keys(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith(("#", ";")):
            out.setdefault(key.strip(), value.strip())
    return out


def _when(timer: dict[str, str]) -> str:
    """A timer's OnCalendar, or for one that fires a while after its last run, that."""
    if timer.get("OnCalendar"):
        return timer["OnCalendar"]
    if timer.get("OnUnitInactiveSec"):
        return f"every {timer['OnUnitInactiveSec']} after its last run"
    return timer.get("Description", "")


def jobs(directory: Path | None = None) -> list[Job]:
    """Every timer-driven job in scripts/systemd, by name."""
    directory = directory or UNITS
    out = []
    for timer in sorted(directory.glob("igs-*.timer")):
        name, service = timer.stem, timer.with_suffix(".service")
        if name in INTERNAL or not service.is_file():
            continue
        unit = _keys(service)
        args = shlex.split(unit.get("ExecStart", ""))
        at = next((i for i, a in enumerate(args) if a.endswith("igs-job.sh")), None)
        if at is None or len(args) <= at + 1:
            continue
        out.append(Job(name, unit.get("Description", name), tuple(args[at + 1:]),
                       _when(_keys(timer))))
    return out


def _systemctl(*args: str, timeout: float = 15) -> subprocess.CompletedProcess | None:
    """`systemctl --user ...`, or None where there is no systemd to ask."""
    exe = shutil.which("systemctl")
    if exe is None or os.name == "nt":
        return None
    try:
        return subprocess.run([exe, "--user", *args], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def states(found: list[Job]) -> dict[str, dict[str, str]]:
    """Each installed job's timer and service as systemd sees them, in one call:
    {name: {"timer.NextElapseUSecRealtime": ..., "service.ActiveState": ...}}. A job
    systemd has no service for is left out; so is everything where it can't be asked."""
    units = [f"{j.name}.{kind}" for j in found for kind in ("timer", "service")]
    if not units:
        return {}
    got = _systemctl("show", *units, *(f"--property={p}" for p in PROPERTIES), timeout=5)
    if got is None or got.returncode != 0:
        return {}
    blocks: dict[str, dict[str, str]] = {}
    for block in got.stdout.strip().split("\n\n"):
        props = dict(line.partition("=")[::2] for line in block.splitlines() if "=" in line)
        if "Id" in props:
            blocks[props["Id"]] = props
    out = {}
    for job in found:
        timer, service = blocks.get(f"{job.name}.timer"), blocks.get(f"{job.name}.service")
        if service and service.get("LoadState") == "loaded":
            out[job.name] = ({f"timer.{k}": v for k, v in (timer or {}).items()}
                             | {f"service.{k}": v for k, v in service.items()})
    return out


def summary(job: Job, state: dict[str, str] | None) -> str:
    """One line: running, last and next run, from systemd; else the shipped schedule."""
    if not state:
        return f"Not installed here; its timer: {job.calendar or 'none'}."
    if state.get("service.ActiveState") in RUNNING:
        now = "Running now"
    else:
        last = state.get("service.ExecMainStartTimestamp") or ""
        result = state.get("service.Result") or ""
        now = (f"Last run {last}" + (f" ({result})" if result and result != "success" else "")
               if last else "Not run yet")
    nxt = state.get("timer.NextElapseUSecRealtime") or ""
    if state.get("timer.ActiveState") != "active":
        return f"{now}. Its timer is off."
    return f"{now}. Next {nxt}." if nxt else f"{now}."


def last_entry(job: Job, lines: int = 15) -> str:
    """The end of the job's log, from its last run's first line."""
    try:
        with job.log.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64_000))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    start = max((i for i, t in enumerate(tail) if t.startswith("=== ")), default=0)
    return "\n".join(tail[start:][-lines:])


def run_now(job: Job, state: dict[str, str] | None) -> str:
    """Start the job now and say how. Raises RuntimeError if it couldn't be started."""
    if state and state.get("service.ActiveState") in RUNNING:
        return f"{job.name} is running already; its output goes to logs/{job.log.name}."
    if state:
        got = _systemctl("start", "--no-block", f"{job.name}.service")
        if got is not None and got.returncode == 0:
            return (f"Started {job.name} through systemd; its output goes to "
                    f"logs/{job.log.name}.")
        raise RuntimeError((got.stderr.strip() if got is not None else "")
                           or f"systemd didn't start {job.name}.service")
    script = REPO_ROOT / "scripts" / ("igs-job.cmd" if os.name == "nt" else "igs-job.sh")
    extra: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
    try:
        subprocess.Popen([str(script), *job.command], cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **extra)
    except OSError as exc:
        raise RuntimeError(f"couldn't start {script.name}: {exc}") from exc
    return f"Started `{job.shown}` with {script.name}; its output goes to logs/{job.log.name}."
