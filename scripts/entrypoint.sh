#!/usr/bin/env sh
set -eu

# When started as root (default image user before drop), fix volume ownership
# then re-exec as the unprivileged app user.
if [ "$(id -u)" = "0" ]; then
  DATA_DIR="${DATA_DIR:-/app/data}"
  LOG_DIR="${LOG_DIR:-/app/logs}"
  mkdir -p "$DATA_DIR" "$LOG_DIR"
  chown -R app:app "$DATA_DIR" "$LOG_DIR" 2>/dev/null || true
  exec su-exec app "$0" "$@"
fi

/app/scripts/init_storage.sh

exec "$@"
