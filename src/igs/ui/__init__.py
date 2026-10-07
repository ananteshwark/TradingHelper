"""The Streamlit UI (app.py) and its charts."""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


def code_stamp() -> float:
    """The newest modification time of the app's Python files."""
    return max(p.stat().st_mtime for p in PACKAGE.rglob("*.py"))


# When this process loaded the app's modules. Streamlit re-runs app.py from disk on every
# interaction but keeps the modules it imported, so after an update (git pull) while the
# app runs, the page and the code under it disagree until the app is restarted.
LOADED_AT = code_stamp()
