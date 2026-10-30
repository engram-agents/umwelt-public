#!/usr/bin/env bash
# tools/build-umwelt-plugin.sh — resyncs plugin/umwelt/ from the live
# src/umwelt/ MCP-client files (umwelt #132). Idempotent: safe to re-run any
# time these files change, so the plugin tree never silently drifts from
# the source of truth.
#
# Copies the files the MCP client actually needs: __init__.py, mcp_server.py,
# avatar_status.py, plus the login-client module chain mcp_server now imports
# (umwelt #495 item 3): client_auth.py (acquires+sends the session token) and
# _ids.py (the shared DEFAULT_IDENTITY_KEY_PATH constant client_auth reads).
# None of them import Flask or the rest of the world app — client_auth LAZY-
# imports cryptography only inside _load_private_key (so a no-key host never
# pays that dependency), and _ids.py is stdlib `os`/`re` only. avatar_status.py
# joined this list in umwelt #178 (mcp_server.py's shared avatar-status render
# helper); it's pure stdlib logic, so it's safe to vendor the same way.
# mentions.py joined in umwelt #511 PR B: mcp_server's `say` wrapper imports
# the shared @-mention parser instead of a hand-copied regex; it's stdlib
# `re` only, same vendoring posture as the others.
# engine_api.py joined in umwelt #1105: every client sends its ENGINE_API
# number (X-Umwelt-Client-API) and renders the gate's refusal through it;
# stdlib `re` only, same vendoring posture.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
dest="$repo_root/plugin/umwelt"

mkdir -p "$dest"
rm -f "$dest"/*.py
cp "$repo_root/src/umwelt/__init__.py" "$dest/__init__.py"
cp "$repo_root/src/umwelt/mcp_server.py" "$dest/mcp_server.py"
cp "$repo_root/src/umwelt/avatar_status.py" "$dest/avatar_status.py"
cp "$repo_root/src/umwelt/client_auth.py" "$dest/client_auth.py"
cp "$repo_root/src/umwelt/_ids.py" "$dest/_ids.py"
cp "$repo_root/src/umwelt/mentions.py" "$dest/mentions.py"
cp "$repo_root/src/umwelt/engine_api.py" "$dest/engine_api.py"

echo "build-umwelt-plugin: synced src/umwelt/{__init__.py,mcp_server.py,avatar_status.py,client_auth.py,_ids.py,mentions.py,engine_api.py} -> plugin/umwelt/"
