"""The look-ahead gate.

Scoring must not run on code that has not passed the look-ahead tests.
`run_gate()` runs every test marked `lookahead` and, only if they all pass,
writes a record containing a fingerprint of the point-in-time and factor
code. `require_gate()` (called by the scoring layer before it does anything)
refuses to proceed unless a passing record exists for the code as it is now.
Editing any gated file invalidates the record until the tests are re-run.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from igs.timeutil import utc_now

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
GATED = ("pit", "factors", "normalize/adjust.py")


class GateError(RuntimeError):
    pass


@dataclass(frozen=True)
class GateRecord:
    fingerprint: str
    passed_at: str
    summary: str


def gate_path() -> Path:
    env = os.environ.get("IGS_GATE_PATH")
    return Path(env) if env else REPO_ROOT / "data" / "gates" / "lookahead.json"


def code_fingerprint(package_root: Path = PACKAGE_ROOT) -> str:
    files: list[Path] = []
    for rel in GATED:
        p = package_root / rel
        files.extend(sorted(p.rglob("*.py")) if p.is_dir() else [p])
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(package_root)).encode())
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def run_gate(tests_dir: Path | None = None) -> GateRecord:
    tests_dir = tests_dir or REPO_ROOT / "tests"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "lookahead", "-q", "-p", "no:cacheprovider",
         str(tests_dir)],
        capture_output=True, text=True, check=False,
    )
    summary = (proc.stdout.strip().splitlines() or [""])[-1]
    if proc.returncode != 0:
        raise GateError(f"look-ahead tests failed (exit {proc.returncode}): {summary}\n"
                        f"{proc.stdout[-4000:]}")
    record = GateRecord(fingerprint=code_fingerprint(), passed_at=utc_now().isoformat(),
                        summary=summary)
    path = gate_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), indent=2))
    return record


def require_gate() -> GateRecord:
    path = gate_path()
    if not path.exists():
        raise GateError(f"no look-ahead gate record at {path}; run `igs gate run` first")
    record = GateRecord(**json.loads(path.read_text()))
    current = code_fingerprint()
    if record.fingerprint != current:
        raise GateError("point-in-time or factor code changed since the look-ahead tests last "
                        "passed; run `igs gate run` again")
    return record
