#!/usr/bin/env bash
# Run as root on the production host. Source changes arrive through GitHub only.
set -Eeuo pipefail

main() {
  local mode=${1:-deploy}
  if [[ $# -gt 1 || ( "$mode" != deploy && "$mode" != --check ) ]]; then
    echo "Usage: $0 [--check]" >&2
    return 2
  fi
  [[ $EUID -eq 0 ]] || { echo 'Run this script as root.' >&2; return 1; }
  umask 077
  exec 9>/run/lock/igs-deploy.lock
  flock -n 9 || { echo 'Another deployment is running.' >&2; return 1; }

  local repo=/home/anant/TradingHelper
  local branch=claude/india-growth-screener-32wchm
  local target deadline timer_units
  app_uid=
  backup=
  # Timers a deployment stopped and has not yet restarted. A failed run leaves the file
  # behind, so the next run restarts them even though they are no longer active.
  pending=/home/anant/deploy-backups/stopped-timers.txt
  phase=preflight
  timers=()
  app_uid=$(id -u anant)
  cd "$repo"
  appgit() { runuser -u anant -- git -C /home/anant/TradingHelper "$@"; }
  svc() {
    runuser -u anant -- env XDG_RUNTIME_DIR="/run/user/$app_uid" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$app_uid/bus" systemctl --user "$@"
  }
  jobs_running() {
    local units
    units=$(svc list-units --type=service --state=running,activating,deactivating \
      --no-legend --plain --full 'igs-*') || return 2
    [[ -n $(awk '$1 != "igs-ui.service" {print $1}' <<< "$units") ]]
  }
  failed() {
    local result=$?
    trap - EXIT
    if (( result != 0 )); then
      echo "Deployment stopped during: $phase" >&2
      if [[ $phase == waiting || $phase == backup ]]; then
        if ((${#timers[@]} == 0)) || svc start "${timers[@]}"; then rm -f "$pending"; fi
      elif [[ $phase != preflight ]]; then
        # Keep consumers stopped when code/schema validation has failed.
        if ((${#timers[@]})); then svc stop "${timers[@]}" || true; fi
        svc stop igs-ui.service || true
        echo "App and schedules remain stopped. Backup and timer list: $backup" >&2
        echo 'The next successful deployment restarts these schedules.' >&2
      fi
    fi
    exit "$result"
  }
  trap failed EXIT

  [[ $(appgit branch --show-current) == "$branch" ]] || {
    echo 'Unexpected branch; refusing deployment.' >&2; return 1;
  }
  [[ -z $(appgit status --porcelain) ]] || {
    echo 'Local changes found; preserve/reconcile them before deploying.' >&2; return 1;
  }
  appgit fetch origin "$branch"
  target=$(appgit rev-parse "origin/$branch")
  appgit merge-base --is-ancestor HEAD "$target" || {
    echo 'History diverged; deployment requires a fast-forward.' >&2; return 1;
  }
  svc is-active --quiet igs-ui.service
  runuser -u anant -- bash -lc 'command -v uv >/dev/null'
  for tool in pg_dump pg_restore curl tar; do command -v "$tool" >/dev/null; done
  echo "GitHub target: $target"
  [[ $mode != --check ]] || { echo 'Preflight passed; nothing deployed.'; return 0; }

  install -d -m 700 /home/anant/deploy-backups
  backup=$(mktemp -d /home/anant/deploy-backups/deploy-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX)
  timer_units=$(svc list-units --type=timer --state=active \
    --no-legend --plain --full 'igs-*')
  mapfile -t timers < <({ awk '{print $1}' <<< "$timer_units"; cat "$pending" 2>/dev/null \
    || true; } | sed '/^$/d' | sort -u)
  printf '%s\n' "${timers[@]}" > "$backup/active-timers.txt"
  printf '%s\n' "${timers[@]}" > "$pending"
  appgit rev-parse HEAD > "$backup/previous-commit.txt"
  printf '%s\n' "$target" > "$backup/target-commit.txt"
  phase=waiting
  if ((${#timers[@]})); then svc stop "${timers[@]}"; fi
  # Let existing orders/ingestion finish; never kill an order worker mid-request.
  deadline=$((SECONDS + 1800))
  while true; do
    local running=0
    jobs_running || running=$?
    [[ $running != 2 ]] || return 1
    [[ $running != 1 ]] || break
    ((SECONDS < deadline)) || { echo 'Jobs still running after 30 minutes.' >&2; return 1; }
    sleep 5
  done
  phase=backup
  runuser -u postgres -- pg_dump -Fc igs > "$backup/igs.dump"
  pg_restore --list "$backup/igs.dump" > "$backup/dump-contents.txt"
  appgit archive HEAD | gzip > "$backup/source.tar.gz"
  echo "Backup: $backup"

  phase=update
  svc stop igs-ui.service
  # Pin the fetched commit; another writer can push while validation is running.
  appgit merge --ff-only "$target"
  runuser -u anant -- bash -lc 'cd /home/anant/TradingHelper && uv sync --locked --all-groups'
  phase=migration
  runuser -u anant -- bash -lc 'cd /home/anant/TradingHelper && uv run --all-groups igs db migrate'
  phase=validation
  runuser -u anant -- bash -lc 'cd /home/anant/TradingHelper && uv run --all-groups igs gate run'
  phase=restart
  svc daemon-reload
  svc start igs-ui.service
  curl --fail --silent --show-error --max-time 10 --retry 10 \
    --retry-connrefused --retry-delay 2 http://127.0.0.1:8501/_stcore/health
  if ((${#timers[@]})); then svc start "${timers[@]}"; fi
  rm -f "$pending"
  phase=complete
  echo
  echo "Deployed $target successfully."
}

# Define the whole script before execution: updating this file cannot change the
# commands of the deployment that is already running.
main "$@"
