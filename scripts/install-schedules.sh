#!/usr/bin/env bash
# Install the additional timers; preserve the user's existing daily/verify schedules.
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
for unit in igs-sync.service igs-sync.timer igs-news.service igs-news.timer \
            igs-call-reviews.service igs-call-reviews.timer igs-notify.service igs-notify.timer \
            igs-models.service igs-models.timer \
            igs-code-watch.service igs-code-watch.timer \
            igs-screener.service igs-screener.timer \
            igs-ownership.service igs-ownership.timer; do
    if [[ -f "$unit_dir/$unit" ]] && ! cmp -s "$repo_dir/scripts/systemd/$unit" "$unit_dir/$unit"; then
        cp -p "$unit_dir/$unit" "$unit_dir/$unit.backup.$(date +%Y%m%d%H%M%S)"
    fi
    install -m 0644 "$repo_dir/scripts/systemd/$unit" "$unit_dir/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now igs-sync.timer igs-news.timer igs-call-reviews.timer igs-notify.timer igs-models.timer
systemctl --user enable --now igs-code-watch.timer igs-screener.timer igs-ownership.timer
systemctl --user list-timers 'igs-*' --all --no-pager
