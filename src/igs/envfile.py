"""Settings from a `.env` file, so interactive shells and scheduled jobs on Windows and
Linux read the same values. Variables already set in the environment win; the file only
fills in what is missing.

Format: one KEY=VALUE per line; blank lines and lines starting with # are ignored; an
optional leading `export ` and matching surrounding quotes are dropped. Nothing is
expanded or executed.
"""

from __future__ import annotations

import os
from pathlib import Path


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.isidentifier():
            raise ValueError(f"line {n}: expected KEY=VALUE, got {raw!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load(path: Path) -> list[str]:
    """Set every variable from `path` that is not already set. Returns the names set."""
    if not path.is_file():
        return []
    values = parse(path.read_text(encoding="utf-8-sig"))    # Notepad may add a BOM
    new = [k for k in values if k not in os.environ]
    for k in new:
        os.environ[k] = values[k]
    return new
