#!/usr/bin/env bash
# Install this file root-owned at /usr/local/libexec/igs-research-ssh-gate.
# It is the forced command for the existing export-only SSH key.
set -euo pipefail
umask 077
case "${SSH_ORIGINAL_COMMAND:-}" in
  export)
    exec /usr/sbin/runuser -u postgres -- /usr/lib/postgresql/18/bin/pg_dump \
      -Fc --no-owner --no-acl igs
    ;;
  'upload '*)
    day=${SSH_ORIGINAL_COMMAND#upload }
    [[ $day =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || exit 2
    exec /usr/sbin/runuser -u anant -- \
      /home/anant/TradingHelper/.venv/bin/python -m igs.hybrid_analytics receive --date "$day"
    ;;
  *)
    echo 'Only export or dated research-report upload is allowed.' >&2
    exit 2
    ;;
esac
