#!/usr/bin/env python3
"""Restart the dashboard after deployed source files settle (systemd user timer)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUIET_SECONDS = 20


def fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    paths = [root / 'pyproject.toml', root / 'uv.lock']
    for directory in ('src', 'scripts', 'config', '.streamlit'):
        base = root / directory
        if base.exists():
            paths.extend(p for p in base.rglob('*') if p.is_file()
                         and '__pycache__' not in p.parts
                         and p.suffix not in {'.pyc', '.pyo', '.tmp', '.swp'})
    for path in sorted(paths):
        if path.exists():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def check(state: dict, signature: str, now: float, restart) -> dict:
    if state.get('applied') == signature:
        return {'applied': signature}
    if state.get('pending') != signature or now < state.get('since', now):
        return {**state, 'pending': signature, 'since': now}
    if now - state['since'] < QUIET_SECONDS:
        return state
    restart()  # Failure leaves the persisted pending state intact for the next run.
    print('Deployed code settled; dashboard restart check completed.', flush=True)
    return {'applied': signature}


def main():
    import fcntl

    directory = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'igs'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'app-code.json'
    with (directory / 'app-code.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            state = json.loads(path.read_text())
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        signature = fingerprint(ROOT)
        state = check(state, signature, time.time(), lambda: subprocess.run(
            ['systemctl', '--user', 'try-restart', 'igs-ui.service'], check=True, timeout=90))
        fd, name = tempfile.mkstemp(dir=directory, prefix='.app-code-')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(state, output)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


if __name__ == '__main__':
    main()
