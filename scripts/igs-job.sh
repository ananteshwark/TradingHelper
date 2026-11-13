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
# also use database locks shared with the UI and daily job. The wait is for the app's
# own momentary probes of this lock (is the job running?), never for a running job,
# which holds it until its end.
exec 9>"logs/$1.lock"
if ! flock -w 5 9; then
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z') skipped igs $*: already running" >> "logs/$1.log"
    exit 0
fi
uv="$(command -v uv || echo "$HOME/.local/bin/uv")"
log="logs/$1.log"
# Which job this is: igs maintenance trims every other job's log only while it holds that
# job's lock, and this one's while it holds it itself.
export IGS_JOB="$1"
{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') igs $*"
    "$uv" run --all-groups igs "$@"
} >> "$log" 2>&1
