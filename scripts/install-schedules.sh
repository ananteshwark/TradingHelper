#!/usr/bin/env bash
# Install every scheduled job in scripts/systemd (each igs-*.timer with its .service) as a
# user timer, and switch the timers on. Settings > Schedules shows them and runs each now.
# - A unit that differs from the installed one is backed up first (the daily cleanup
#   keeps the newest few copies).
# - The daily job and the weekly source check are installed only where neither of their
#   units is yet: an installed one keeps its own schedule (the server runs the daily job
#   every four hours) and its on/off state.
# - The trade monitor (igs-trade-monitor.service) is not a timer and is not installed
#   here (docs/TRADE_MONITOR.md).
# Safe to rerun.
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
units="$repo_dir/scripts/systemd"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
timers=()
for timer in "$units"/igs-*.timer; do
    name=$(basename "$timer" .timer)
    [[ -f "$units/$name.service" ]] || continue
    case $name in
        igs-daily|igs-verify)
            if [[ -f "$unit_dir/$name.timer" || -f "$unit_dir/$name.service" ]]; then
                echo "Kept the installed $name, with its own schedule."
                continue
            fi ;;
    esac
    for unit in "$name.service" "$name.timer"; do
        if [[ -f "$unit_dir/$unit" ]] && ! cmp -s "$units/$unit" "$unit_dir/$unit"; then
            cp -p "$unit_dir/$unit" "$unit_dir/$unit.backup.$(date +%Y%m%d%H%M%S)"
        fi
        install -m 0644 "$units/$unit" "$unit_dir/$unit"
    done
    timers+=("$name.timer")
done
systemctl --user daemon-reload
systemctl --user enable --now "${timers[@]}"
systemctl --user list-timers 'igs-*' --all --no-pager
