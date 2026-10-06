"""Run what CI runs (.github/workflows/ci.yml) before pushing, database tests included.

Two writers push to the same branch: the Claude Code session in the cloud and Codex on
the owner's machine. CI failed on 660d73f because a database test was skipped where the
change was checked: without IGS_TEST_DATABASE_URL, pytest skips about 130 tests and the
run still looks green. So this script:

- stops first when origin has commits on this branch that this copy lacks;
- refuses to run without a test database, and makes a missing one fail the tests rather
  than skip them (IGS_REQUIRE_DB=1);
- then runs CI's steps in CI's order.

    uv run python scripts/ci_local.py              # everything CI runs
    uv run python scripts/ci_local.py --sync-only  # only "is this copy up to date?"

The pre-push hook (.githooks/pre-push, enabled with `git config core.hooksPath
.githooks`) runs it with --pre-push: it also refuses a push that would replace commits
on the remote branch that this copy doesn't have, i.e. the other writer's work.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterable

ZERO = "0" * 40
STEPS = (["uv", "sync", "--locked", "--all-groups"],
         ["uv", "run", "ruff", "check", "src", "tests"],
         ["uv", "run", "pytest", "-m", "lookahead"],
         ["uv", "run", "pytest"])
NO_DB = """IGS_TEST_DATABASE_URL is not set, so about 130 database tests would be skipped and
CI could still fail. Point it at an empty database whose name contains "test":
  Ubuntu:   sudo -u postgres createdb -O igs igs_test
            export IGS_TEST_DATABASE_URL=postgresql://igs:PASSWORD@localhost:5432/igs_test
  Windows:  createdb -U postgres -O igs igs_test
            $env:IGS_TEST_DATABASE_URL = "postgresql://igs:PASSWORD@localhost:5432/igs_test\""""


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD")


def behind_origin(name: str) -> str | None:
    """Why this copy is not up to date with origin/<name>, or None when it is."""
    try:
        git("fetch", "--quiet", "origin", name)
    except subprocess.CalledProcessError as exc:
        if "couldn't find remote ref" in (exc.stderr or ""):
            return None                                   # a branch not pushed yet
        return f"could not fetch origin/{name}: {(exc.stderr or '').strip()}"
    ahead, behind = (int(n) for n in
                     git("rev-list", "--left-right", "--count",
                         f"HEAD...origin/{name}").split())
    if not behind:
        return None
    how = (f"git pull --rebase origin {name}" if ahead
           else f"git pull --ff-only origin {name}")
    return (f"origin/{name} has {behind} commit(s) this copy doesn't have"
            + (f" and this copy has {ahead} of its own" if ahead else "")
            + f". Run `{how}`, then check again.")


def refused_updates(lines: Iterable[str],
                    contains: Callable[[str, str], bool]) -> list[str]:
    """Pushes that would drop commits already on the remote branch.

    Each line is what git gives a pre-push hook: "<local ref> <local sha> <remote ref>
    <remote sha>". `contains(old, new)` says whether commit `old` is in `new`'s history.
    """
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if remote_sha == ZERO or local_sha == ZERO:       # a new branch, or a deletion
            continue
        if not contains(remote_sha, local_sha):
            name = remote_ref.removeprefix("refs/heads/")
            out.append(f"{remote_ref} has commits that {local_ref} doesn't (the other "
                       f"writer's work); pushing would replace them. Run `git pull "
                       f"--rebase origin {name}`, check again and push without --force.")
    return out


def _contains(old: str, new: str) -> bool:
    """Unknown objects count as not contained: fetch first, then decide."""
    return subprocess.run(["git", "merge-base", "--is-ancestor", old, new],
                          capture_output=True).returncode == 0


# Git exports these to hooks so that git commands find the pushing repository. Tests
# must never inherit them: a test that runs git in its own temporary repository would
# otherwise act on the real one (from a linked worktree GIT_DIR is absolute), and on
# 6 Oct 2026 tests/test_ci_local.py pushed its scratch branch "work" to GitHub that way.
REPOSITORY_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                        "GIT_PREFIX", "GIT_NAMESPACE", "GIT_QUARANTINE_PATH")


def check_env() -> dict[str, str]:
    """The environment for CI's steps: a database is required, the hook's repository is
    not visible."""
    env = {k: v for k, v in os.environ.items() if k not in REPOSITORY_VARIABLES}
    return {**env, "IGS_REQUIRE_DB": "1"}


def run_checks() -> int:
    if not os.environ.get("IGS_TEST_DATABASE_URL"):
        print(NO_DB, file=sys.stderr)
        return 1
    env = check_env()
    for step in STEPS:
        print("$", " ".join(step), flush=True)
        if subprocess.run(step, env=env).returncode:
            print(f"failed: {' '.join(step)}", file=sys.stderr)
            return 1
    print("All of CI's checks pass here.")
    return 0


def main(argv: list[str]) -> int:
    if "--pre-push" in argv:
        refused = refused_updates(sys.stdin.read().splitlines(), _contains)
        for message in refused:
            print(message, file=sys.stderr)
        if refused:
            return 1
        if os.environ.get("IGS_SKIP_CHECKS") == "1":
            print("IGS_SKIP_CHECKS=1: CI's checks not run before this push.")
            return 0
        return run_checks()
    problem = behind_origin(branch())
    if problem:
        print(problem, file=sys.stderr)
        return 1
    if "--sync-only" in argv:
        print("Up to date with origin.")
        return 0
    return run_checks()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
