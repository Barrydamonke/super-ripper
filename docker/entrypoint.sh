#!/bin/sh
# Container start: as root, create device nodes for the optical drives found
# in sysfs, then drop to PUID:PGID and run the web app.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-0002}"
PORT="${PORT:-8580}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

set -- uvicorn disc_ripper.app:create_app --factory --host 0.0.0.0 --port "$PORT"

if [ "$(id -u)" = "0" ]; then
    python -m disc_ripper.devnodes
    mkdir -p "$CONFIG_DIR"
    chown "$PUID:$PGID" "$CONFIG_DIR"
    umask "$UMASK"
    # Drop every capability for good (bounding set) and block setuid binaries
    # (no_new_privs), so the app can never get back to root's CAP_MKNOD.
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups \
        --no-new-privs --bounding-set=-all --inh-caps=-all "$@"
fi

umask "$UMASK"
exec "$@"
