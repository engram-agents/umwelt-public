#!/usr/bin/env bash
# mud — launch the Umwelt terminal client (the MUD door into the world).
#
# The one-command way in: resolves the repo root from its own location, sets
# PYTHONPATH, picks a sane python, and drops you into the world over the same
# HTTP API agents use. Nothing to remember — just run it.
#
#   scripts/mud.sh --agent lei          # join the world as lei (non-interactive register)
#   scripts/mud.sh                       # full interactive registration prompt
#   UMWELT_URL=http://myhost.lan:<port> scripts/mud.sh --agent lei   # from the LAN
#
# Any extra args pass straight through to `python -m umwelt.mud`.
set -euo pipefail

# Resolve the repo root even when invoked via a symlink (e.g. from ~/.local/bin).
# Portable symlink chase — plain `readlink` in a loop (no GNU-only `readlink -f`,
# so this also works on macOS).
SELF="${BASH_SOURCE[0]}"
while [ -h "$SELF" ]; do
  d="$(cd -P "$(dirname "$SELF")" && pwd)"
  SELF="$(readlink "$SELF")"
  [[ "$SELF" != /* ]] && SELF="$d/$SELF"
done
HERE="$(cd -P "$(dirname "$SELF")/.." && pwd)"   # scripts/ -> repo root

# Pick a python: a repo venv if present, else this user's engram venv, else python3.
if [ -x "$HERE/.venv/bin/python" ]; then
  PY="$HERE/.venv/bin/python"
elif [ -x "$HOME/.engram/venv/bin/python3" ]; then
  PY="$HOME/.engram/venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "mud: no python3 found (looked for a repo .venv, ~/.engram/venv, and python3 on PATH)" >&2
  exit 1
fi

exec env PYTHONPATH="$HERE/src" "$PY" -m umwelt.mud "$@"
