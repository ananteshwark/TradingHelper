"""Reproducibility metadata and compatibility checks for validation artifacts."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from igs.config import config_dir

ROOT = Path(__file__).resolve().parents[2]


def _hash(paths) -> str:
    h = hashlib.sha256()
    for path in sorted(paths):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def validation_fingerprint() -> str:
    files = list((ROOT / "src" / "igs").rglob("*.py"))
    files += [config_dir() / name for name in
              ("scoring.yaml", "universe.yaml", "red_flags.yaml", "backtest.yaml",
               "costs.yaml", "xbrl_concepts.yaml")]
    return _hash(files)


def run_provenance(conn) -> dict:
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                         stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    count, latest = conn.execute("select count(*), max(fetched_at) from raw_payload").fetchone()
    return {"commit": commit, "source_and_config_hash": validation_fingerprint(),
            "dependency_lock_hash": _hash([ROOT / "uv.lock"]),
            "mapping_hash": _hash([config_dir() / "xbrl_concepts.yaml"]),
            "raw_payload_count": count, "latest_fetch_at": latest}
