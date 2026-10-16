#!/usr/bin/env bash
# tools/alpha-world.sh — lifecycle manager for the umwelt ALPHA/staging world
# (#77): the delete-record staging tier at :5005 where new world-engine
# features activate FIRST, get tested live, and only THEN get enabled on the
# real world (:5003). Full pipeline: docs/DEPLOY.md.
#
# Wraps `systemctl --user ... umwelt-alpha-world.service` (the unit template
# and install steps are internal deploy files, not in this release).
# Requires the unit to already be installed (`systemctl --user status
# umwelt-alpha-world.service` should resolve, even if inactive).
#
# Usage:
#   tools/alpha-world.sh start           # systemctl --user start
#   tools/alpha-world.sh stop            # systemctl --user stop
#   tools/alpha-world.sh status          # systemctl --user status
#   tools/alpha-world.sh reset           # prompts for confirmation, then resets
#   tools/alpha-world.sh reset --yes     # non-interactive: stop, rm db, start
#
# reset = stop + rm -f "$ALPHA_DB" + start — a fresh, empty alpha world
# ("run wild, tear down freely" — Lei's #77 ruling). This DELETES the alpha
# world's database; there is no undo. Guarded by a confirmation prompt unless
# --yes is passed (e.g. for non-interactive/scripted use).
#
# Overridable via env:
#   ALPHA_DB       db path to manage      (default: ~/.umwelt/alpha-world.db)
#   ALPHA_SERVICE  systemd --user unit    (default: umwelt-alpha-world.service)
set -euo pipefail

ALPHA_DB="${ALPHA_DB:-$HOME/.umwelt/alpha-world.db}"
ALPHA_SERVICE="${ALPHA_SERVICE:-umwelt-alpha-world.service}"

usage() {
    echo "Usage: $0 {start|stop|status|reset [--yes]}" >&2
    exit 1
}

cmd_start()  { systemctl --user start  "$ALPHA_SERVICE"; }
cmd_stop()   { systemctl --user stop   "$ALPHA_SERVICE"; }
cmd_status() { systemctl --user status "$ALPHA_SERVICE"; }

cmd_reset() {
    local yes="${1:-}"
    if [[ "$yes" != "--yes" ]]; then
        echo "This will STOP $ALPHA_SERVICE, DELETE $ALPHA_DB, and START it fresh."
        read -r -p "Reset the alpha world? Type 'yes' to confirm: " reply
        if [[ "$reply" != "yes" ]]; then
            echo "Aborted — alpha world left untouched."
            exit 1
        fi
    fi
    echo "🗑️  Resetting alpha world (db: ${ALPHA_DB})"
    cmd_stop
    rm -f "$ALPHA_DB"
    cmd_start
    echo "✅  Alpha world reset — fresh db at ${ALPHA_DB}"
}

case "${1:-}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    reset)  cmd_reset "${2:-}" ;;
    *)      usage ;;
esac
