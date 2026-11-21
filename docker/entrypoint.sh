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

# The setpriv group option for the app user. A root-group (gid 0) node would
# hand the app root's group, so it gets no group; gid 0 nodes must be
# world-accessible instead (warned about below if they aren't).
groups_option() {
    gid="$(render_gid)"
    if [ -n "$gid" ] && [ "$gid" != "0" ]; then
        echo "--groups=$gid"
    else
        echo "--clear-groups"
    fi
}

# Warn when the node is root-only: the app (no groups) can't open it.
warn_render_node() {
    [ -e "$RENDER_NODE" ] || return 0
    [ "$(render_gid)" = "0" ] || return 0
    other="$(( $(stat -c %a "$RENDER_NODE") % 10 ))"
    if [ "$(( other & 6 ))" != "6" ]; then
        echo "entrypoint: render node is root-only; Fast encodes won't be able to use the GPU - set /dev/dri permissions on the host" >&2
    fi
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
    warn_render_node
    groups_opt="$(groups_option)"
    exec setpriv --reuid="$PUID" --regid="$PGID" "$groups_opt" \
        --no-new-privs --bounding-set=-all --inh-caps=-all "$@"
fi

umask "$UMASK"
exec "$@"
