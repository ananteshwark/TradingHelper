"""scripts/ci_local.py: the check both writers run before pushing to the shared branch.
Real git repositories in a temporary directory stand in for origin and two clones."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_local", ROOT / "scripts" / "ci_local.py")
ci_local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_local)
BRANCH = "work"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(clone: Path, name: str) -> str:
    (clone / name).write_text(name)
    _git(clone, "add", name)
    _git(clone, "commit", "-q", "-m", name)
    return _git(clone, "rev-parse", "HEAD")


@pytest.fixture
def clones(tmp_path, monkeypatch):
    """origin plus two clones of it on BRANCH: one for each writer."""
    for key, value in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                       "GIT_CONFIG_GLOBAL": os.devnull}.items():
        monkeypatch.setenv(key, value)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", BRANCH, str(origin))
    a, b = tmp_path / "a", tmp_path / "b"
    _git(tmp_path, "clone", "-q", str(origin), str(a))
    _git(a, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
    _commit(a, "base")
    _git(a, "push", "-q", "origin", BRANCH)
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(origin), str(b))
    return a, b


def test_a_copy_behind_origin_is_told_to_pull_first(clones, monkeypatch):
    a, b = clones
    monkeypatch.chdir(a)
    assert ci_local.behind_origin(BRANCH) is None
    _commit(b, "theirs")
    _git(b, "push", "-q", "origin", BRANCH)
    assert ci_local.behind_origin(BRANCH) == (
        f"origin/{BRANCH} has 1 commit(s) this copy doesn't have. "
        f"Run `git pull --ff-only origin {BRANCH}`, then check again.")
    _commit(a, "mine")                                   # both sides moved
    assert "and this copy has 1 of its own. Run `git pull --rebase origin work`" in (
        ci_local.behind_origin(BRANCH))
    _git(a, "pull", "-q", "--rebase", "origin", BRANCH)
    assert ci_local.behind_origin(BRANCH) is None
    assert ci_local.main(["--sync-only"]) == 0


def test_a_push_that_would_replace_the_other_writers_commits_is_refused(clones,
                                                                         monkeypatch):
    a, b = clones
    old = _git(a, "rev-parse", "HEAD")
    theirs = _commit(b, "theirs")
    _git(b, "push", "-q", "origin", BRANCH)
    mine = _commit(a, "mine")
    monkeypatch.chdir(a)
    _git(a, "fetch", "-q", "origin")
    line = f"refs/heads/{BRANCH} {mine} refs/heads/{BRANCH} {theirs}"
    [message] = ci_local.refused_updates([line], ci_local._contains)
    assert "the other writer's work" in message and "git pull --rebase origin work" in message
    # After the rebase, theirs is in this copy's history and the push goes ahead.
    _git(a, "pull", "-q", "--rebase", "origin", BRANCH)
    rebased = _git(a, "rev-parse", "HEAD")
    assert ci_local.refused_updates(
        [f"refs/heads/{BRANCH} {rebased} refs/heads/{BRANCH} {theirs}"],
        ci_local._contains) == []
    # A new branch, a deletion and a plain fast-forward are not refused.
    zero = ci_local.ZERO
    assert ci_local.refused_updates([f"refs/heads/x {mine} refs/heads/x {zero}",
                                     f"(delete) {zero} refs/heads/x {old}",
                                     f"refs/heads/{BRANCH} {theirs} refs/heads/{BRANCH} {old}"],
                                    ci_local._contains) == []


def test_the_hook_refuses_a_force_push_over_the_other_writers_commits(clones):
    """Through git itself: the hook as installed, with the checks skipped."""
    a, b = clones
    hooks = a / ".githooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text(
        f"#!/bin/sh\nexec {sys.executable} {ROOT / 'scripts' / 'ci_local.py'} --pre-push \"$@\"\n")
    (hooks / "pre-push").chmod(0o755)
    _git(a, "config", "core.hooksPath", ".githooks")
    _commit(b, "theirs")
    _git(b, "push", "-q", "origin", BRANCH)
    _commit(a, "mine")
    env = {**os.environ, "IGS_SKIP_CHECKS": "1"}
    pushed = subprocess.run(["git", "push", "--force", "origin", BRANCH], cwd=a, env=env,
                            capture_output=True, text=True)
    assert pushed.returncode != 0 and "the other writer's work" in pushed.stderr
    _git(a, "pull", "-q", "--rebase", "origin", BRANCH)
    pushed = subprocess.run(["git", "push", "origin", BRANCH], cwd=a, env=env,
                            capture_output=True, text=True)
    assert pushed.returncode == 0, pushed.stderr
    assert _git(b, "ls-remote", "origin", BRANCH).split()[0] == _git(a, "rev-parse", "HEAD")


def test_the_checks_need_a_test_database(monkeypatch, capsys):
    monkeypatch.delenv("IGS_TEST_DATABASE_URL", raising=False)
    assert ci_local.run_checks() == 1
    assert "about 130 database tests would be skipped" in capsys.readouterr().err
    assert ci_local.STEPS[-1] == ["uv", "run", "pytest"]
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for step in ci_local.STEPS[1:]:                       # the same commands as CI
        assert f"run: {' '.join(step)}" in ci
    assert 'IGS_REQUIRE_DB: "1"' in ci
