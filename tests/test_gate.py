from __future__ import annotations

import json
import shutil

import pytest

from igs.pit import gate


def test_fingerprint_changes_when_gated_code_changes(tmp_path):
    pkg = tmp_path / "igs"
    shutil.copytree(gate.PACKAGE_ROOT, pkg,
                    ignore=shutil.ignore_patterns("__pycache__", "*.sql"))
    before = gate.code_fingerprint(pkg)
    assert before == gate.code_fingerprint(pkg)
    (pkg / "pit" / "view.py").write_text((pkg / "pit" / "view.py").read_text() + "\n# edit\n")
    assert gate.code_fingerprint(pkg) != before


def test_require_gate(tmp_path, monkeypatch):
    path = tmp_path / "gate.json"
    monkeypatch.setenv("IGS_GATE_PATH", str(path))
    with pytest.raises(gate.GateError, match="no look-ahead gate record"):
        gate.require_gate()
    path.write_text(json.dumps({"fingerprint": "stale", "passed_at": "x", "summary": ""}))
    with pytest.raises(gate.GateError, match="changed since"):
        gate.require_gate()
    path.write_text(json.dumps({"fingerprint": gate.code_fingerprint(), "passed_at": "x",
                                "summary": ""}))
    assert gate.require_gate().passed_at == "x"


def test_run_gate_records_only_on_pass(tmp_path, monkeypatch):
    path = tmp_path / "gate.json"
    monkeypatch.setenv("IGS_GATE_PATH", str(path))
    tests = tmp_path / "t"
    tests.mkdir()
    (tests / "test_x.py").write_text(
        "import pytest\n@pytest.mark.lookahead\ndef test_fails():\n    assert False\n")
    with pytest.raises(gate.GateError, match="failed"):
        gate.run_gate(tests)
    assert not path.exists()
    (tests / "test_x.py").write_text(
        "import pytest\n@pytest.mark.lookahead\ndef test_ok():\n    assert True\n")
    rec = gate.run_gate(tests)
    assert path.exists() and rec.fingerprint == gate.code_fingerprint()
