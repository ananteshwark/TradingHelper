#!/usr/bin/env bash
# Run one igs command for a scheduler (cron or a systemd timer) and append its output to
# logs/COMMAND.log in the repository, e.g.
#   scripts/igs-job.sh daily
#   scripts/igs-job.sh sources verify
# Settings come from the repository's .env file (read by igs itself).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
# Do not overlap invocations of the same scheduled command. News collection and AI
# also use database locks shared with the UI and daily job.
exec 9>"logs/$1.lock"
if ! flock -n 9; then
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z') skipped igs $*: already running" >> "logs/$1.log"
    exit 0
fi
uv="$(command -v uv || echo "$HOME/.local/bin/uv")"
log="logs/$1.log"
{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') igs $*"
    "$uv" run --all-groups igs "$@"
} >> "$log" 2>&1
