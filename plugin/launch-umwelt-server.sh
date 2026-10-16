#!/usr/bin/env bash
# plugin/launch-umwelt-server.sh — Claude Code plugin entry point for the
# umwelt MCP server. No CLI args (registered verbatim in .mcp.json); all
# configuration comes from ~/.umwelt/config.json and/or env vars.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$HOME/.umwelt/venv/bin/python3"

if [ ! -x "$VENV_PY" ]; then
    echo "umwelt: venv python not found at $VENV_PY" >&2
    echo "  Run setup:" >&2
    echo "    python3 -m venv ~/.umwelt/venv" >&2
    echo "    ~/.umwelt/venv/bin/pip install fastmcp" >&2
    exit 1
fi

CONFIG="$HOME/.umwelt/config.json"
if [ -f "$CONFIG" ]; then
    AGENT="$("$VENV_PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("agent",""))' "$CONFIG" 2>/dev/null || true)"
    WORLD_URL="$("$VENV_PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("world_url",""))' "$CONFIG" 2>/dev/null || true)"
    [ -n "$AGENT" ] && export UMWELT_AGENT="$AGENT"
    [ -n "$WORLD_URL" ] && export UMWELT_URL="$WORLD_URL"
fi

if [ -z "${UMWELT_AGENT:-}" ]; then
    echo "umwelt: no agent identity configured — set one in $CONFIG (e.g. {\"agent\": \"your-name\", \"world_url\": \"http://localhost:5003\"}), or export UMWELT_AGENT" >&2
    exit 1
fi

export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$VENV_PY" -m umwelt.mcp_server
