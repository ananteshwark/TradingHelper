"""Settings from a `.env` file, so interactive shells and scheduled jobs on Windows and
Linux read the same values. Variables already set in the environment win; the file only
fills in what is missing.

Format: one KEY=VALUE per line; blank lines and lines starting with # are ignored; an
optional leading `export ` and matching surrounding quotes are dropped. Nothing is
expanded or executed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def default_path() -> Path:
    """The repository's .env, or IGS_ENV_FILE."""
    return Path(os.environ.get("IGS_ENV_FILE", REPO_ROOT / ".env"))


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


def _rewrite(path: Path, key: str, line: str | None) -> None:
    """Replace every KEY= line with `line` (or drop them), keeping everything else; the file
    is readable by its owner only, since it holds credentials."""
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    kept = [ln for ln in lines if not pattern.match(ln)]
    if line is not None:
        kept.append(line)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # never world-readable
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + ("\n" if kept else ""))
    os.chmod(tmp, 0o600)                                    # also when tmp already existed
    os.replace(tmp, path)


def set_value(path: Path, key: str, value: str) -> None:
    """Write KEY=value to the file and to this process's environment."""
    if not key.isidentifier():
        raise ValueError(f"not a variable name: {key!r}")
    if not value or any(c.isspace() for c in value) or "#" in value:
        raise ValueError(f"{key} must be a single word without spaces or #")
    _rewrite(path, key, f"{key}={value}")
    os.environ[key] = value


def unset(path: Path, key: str) -> None:
    """Remove KEY from the file and from this process's environment."""
    _rewrite(path, key, None)
    os.environ.pop(key, None)
