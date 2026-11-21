#!/bin/sh
# Container start: as root, create device nodes for the optical drives found
# in sysfs, then drop to PUID:PGID and run the web app.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-0002}"
PORT="${PORT:-8580}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
RENDER_NODE="${RENDER_NODE:-/dev/dri/renderD128}"

# Print the gid owning the GPU render node; print nothing if it doesn't exist.
render_gid() {
    [ -e "$RENDER_NODE" ] && stat -c %g "$RENDER_NODE" || true
}

# Tests source this file for render_gid without running the entrypoint.
[ "${ENTRYPOINT_LIB:-}" = "1" ] && return 0

# Open /events streams never close on their own; without a timeout uvicorn would
# wait on them at shutdown until Docker kills it, and running jobs would never be
# marked interrupted.
set -- uvicorn disc_ripper.app:create_app --factory --host 0.0.0.0 --port "$PORT" \
    --timeout-graceful-shutdown 10

if [ "$(id -u)" = "0" ]; then
    python -m disc_ripper.devnodes
    mkdir -p "$CONFIG_DIR"
    chown "$PUID:$PGID" "$CONFIG_DIR"
    umask "$UMASK"
    # Drop every capability for good (bounding set) and block setuid binaries
    # (no_new_privs), so the app can never get back to root's CAP_MKNOD.
    # setpriv --clear-groups would also drop a --group-add given to the container,
    # so the app gets the render node's group explicitly (VAAPI encodes).
    gid="$(render_gid)"
    if [ -n "$gid" ]; then groups_opt="--groups=$gid"; else groups_opt="--clear-groups"; fi
    exec setpriv --reuid="$PUID" --regid="$PGID" "$groups_opt" \
        --no-new-privs --bounding-set=-all --inh-caps=-all "$@"
fi

umask "$UMASK"
exec "$@"
