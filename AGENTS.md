# Working on this repository

Two writers work on this repository: the Claude Code session in the cloud, and Codex on
the owner's machine. Neither sees the other's work until it is pushed. These rules keep
each branch linear and CI green.

## The branches

| Branch | What it is | Who pushes to it |
|---|---|---|
| `codex/server-app` | **The server:** the app hosted on the production server. `scripts/deploy-from-github.sh` deploys this branch, and only this one. | Codex; the cloud session when the owner says so |
| `codex/local-app` | **The local computer:** the app on the owner's own machine, for ML training. It adds the research mirror and the training console. | Codex; the cloud session when the owner says so |
| `claude/india-growth-screener-32wchm` | **The cloud session's work:** research and changes, with its pull request into `main`. Nothing deploys from it. | the cloud session |

**What goes where:**
- **Local-only work stays on `codex/local-app`.** That means the research mirror, the
  training console and local GPU work. It never goes to the server.
- **A fix the server needs goes to `codex/server-app`.** It is then merged into
  `codex/local-app`, so the local app has it too.
- **The cloud session's changes are cherry-picked** from its branch onto `codex/server-app`
  or `codex/local-app`, when the owner says which. A commit the target branch already has
  in another form is left out.
- **The production server is never edited directly.** Its changes come only from
  `codex/server-app` on GitHub (docs/GITHUB_DEPLOYMENT.md).

## What the app is for

The owner's two goals; weigh every change against them:

1. **Investing:** find stocks whose business is improving before the price has caught up,
   and book the profit near the top of the move.
2. **Trading:** make money, measured after all charges.

The README's "What it is for" says what the app's own tests found about both. A feature
that can't show it serves one of the goals, through a record measured against the market
or after charges, doesn't belong.

## Before you start

On the branch you will work on (`BRANCH` below):

```bash
git fetch origin
git pull --ff-only origin BRANCH
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

The pre-push hook runs the same checks, on whichever branch is pushed. It also refuses a
push that would replace commits already on the remote branch, i.e. the other writer's
work.

## When the push is rejected

The other writer pushed first.

```bash
git pull --rebase origin BRANCH
uv run python scripts/ci_local.py
git push
```

- Never force-push any of these branches, and never rewrite commits that are already
  pushed.
- A rebase conflict in a file the other writer changed: keep both changes, or ask the
  owner which behaviour is wanted; don't drop theirs.

## In a change

- **Deployment and configuration work** that changes tracked files must be committed and
  pushed to `codex/server-app` after the required checks pass. Keep the deployed checkout
  and the repository in sync; do not leave deployment-only source changes.
- **A change in behaviour that an existing test covers** updates that test in the same
  commit, saying why in the commit message.
- **A change to a gated file** (`src/igs/pit/gate.py`, `GATED`) needs `uv run igs gate run`.
- **A new migration** goes after the highest number on every branch it will reach.
  Migrations must not collide between `codex/server-app` and `codex/local-app`, since
  changes move between them.
- **The pull request:** add a section for the change to the description of the cloud
  branch's open pull request into `main`, or tell the owner to. When the last one has been
  merged, the branch carries on: open a new pull request from it for the next changes.
