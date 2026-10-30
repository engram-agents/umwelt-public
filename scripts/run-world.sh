#!/usr/bin/env bash
# Start the Umwelt world server — ONE process that serves BOTH surfaces:
#   • the agent API (the world engine): /umwelt/register, move_to, look_at, use, …
#   • the human web view (the walk):    GET /  →  http://localhost:<port>
# There is no separate "web app" to start — the walk IS this server's front page.
#
# Handles the fiddly bits (PYTHONPATH=src for the src-layout package, the db dir,
# sane defaults) so you don't have to remember them. Override via env:
#   UMWELT_HOST (default 127.0.0.1, loopback; set 0.0.0.0 to listen on every interface)
#   UMWELT_PORT (default 5003) · UMWELT_DB (default ~/.umwelt/umwelt.db) · PYTHON
#
# Usage:  scripts/run-world.sh
set -euo pipefail
cd "$(dirname "$0")/.."                       # repo root

HOST="${UMWELT_HOST:-127.0.0.1}"
PORT="${UMWELT_PORT:-5003}"
DB="${UMWELT_DB:-$HOME/.umwelt/umwelt.db}"
PY="${PYTHON:-python3}"

mkdir -p "$(dirname "$DB")"
echo "🌍 Umwelt starting"
echo "   web view (walk it):  http://localhost:${PORT}/"
echo "   agent API:           http://localhost:${PORT}/umwelt/…   (health: /health)"
echo "   bound host:          ${HOST}"
echo "   db:                  ${DB}"
echo
exec env PYTHONPATH=src "$PY" -m umwelt --host "${HOST}" --port "${PORT}" --db "${DB}"
