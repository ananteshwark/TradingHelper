# Working on this repository

Two writers push to the same branch, `claude/india-growth-screener-32wchm` (PR #3): the
Claude Code session in the cloud, and Codex on the owner's machine. Neither sees the
other's work until it is pushed. These rules keep the branch linear and CI green.

## Before you start

```bash
git fetch origin
git pull --ff-only origin claude/india-growth-screener-32wchm
git config core.hooksPath .githooks        # once per clone: the pre-push check below
```

## Before you push

```bash
uv run python scripts/ci_local.py
```

It runs what CI runs (`.github/workflows/ci.yml`): `uv sync --locked`, ruff, the
look-ahead gate and the full suite, **with the database tests**. It needs
`IGS_TEST_DATABASE_URL` pointing at an empty database whose name contains "test"; it
says how to create one. Without it about 130 tests are skipped and the run still looks
green: that is how 660d73f went red in CI.

The pre-push hook runs the same checks. It also refuses a push that would replace
commits already on the remote branch, i.e. the other writer's work.

## When the push is rejected

The other writer pushed first.

```bash
git pull --rebase origin claude/india-growth-screener-32wchm
uv run python scripts/ci_local.py
git push
```

- Never force-push this branch, and never rewrite commits that are already pushed.
- A rebase conflict in a file the other writer changed: keep both changes, or ask the
  owner which behaviour is wanted; don't drop theirs.

## In a change

- A change in behaviour that an existing test covers updates that test in the same
  commit, saying why in the commit message.
- A change to a gated file (`src/igs/pit/gate.py`, `GATED`) needs `uv run igs gate run`.
- A new migration goes after the highest number on the branch you just pulled.
- Add a section for the change to PR #3's description, or tell the owner to.
