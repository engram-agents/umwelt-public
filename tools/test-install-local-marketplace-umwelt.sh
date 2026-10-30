#!/usr/bin/env bash
# tools/test-install-local-marketplace-umwelt.sh — exercises
# tools/install-local-marketplace-umwelt.sh (umwelt #132, codex follow-on)
# against a
# throwaway marketplace root. Never touches the real ~/.umwelt/marketplace:
# UMWELT_MARKETPLACE_ROOT is overridden to a mktemp dir for the whole run,
# and default-target invocations pass --skip-add so no live Claude Code state
# is touched. Codex-target routing is exercised via a fake `codex`
# executable against throwaway HOME/CODEX_HOME roots.
set -euo pipefail

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() {
    echo "ok - $*"
}

# Resolve a path through every symlink. Any comparison against a symlink-
# following result must resolve BOTH sides: on macOS mktemp -d returns
# /var/folders/..., and /var is a symlink to private/var.
resolve_path() {
    python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
installer="$script_dir/install-local-marketplace-umwelt.sh"

[[ -x "$installer" ]] || fail "tools/install-local-marketplace-umwelt.sh not found or not executable"

tmp_market="$(mktemp -d)"
marketplace_root="$tmp_market/marketplace"

cleanup() {
    rm -rf "$tmp_market"
}
trap cleanup EXIT

export UMWELT_MARKETPLACE_ROOT="$marketplace_root"
# Every successful build writes $UMWELT_HOME/.deployed-version (umwelt #1104).
# Point it at the throwaway dir so a harness run never writes the live marker.
export UMWELT_HOME="$tmp_market/umwelt-home"

# --- 0. --help exits 0, prints usage, no side effects ---
help_out="$(bash "$installer" --help)"
echo "$help_out"
[[ "$help_out" == *"Usage: install-local-marketplace-umwelt.sh"* ]] || fail "--help did not print usage"
[[ ! -d "$marketplace_root" ]] || fail "--help had a side effect: $marketplace_root was created"
pass "--help exits 0, prints usage, and has no side effects"

# --- 1. Fresh install with --skip-add (never touch the real claude CLI state) ---
out1="$(bash "$installer" --allow-untagged --allow-dirty --skip-add --skip-venv)"
echo "$out1"
grep -q '^umwelt_untagged_override=1$' "$UMWELT_HOME/.deployed-version" \
    || fail "install did not write the deployed-version marker under UMWELT_HOME"
pass "install writes the deployed-version marker under the throwaway UMWELT_HOME"

# umwelt #1105 D1: the marker records the engine API the tree was built with,
# under the umwelt_-prefixed key. The number is read from the source of truth
# (src/umwelt/engine_api.py), never hard-coded here, so a bump needs no edit;
# the 1104 placeholder `engine_api=unknown` is retired.
expected_engine_api="$(sed -n 's/^ENGINE_API: int = \([0-9][0-9]*\)$/\1/p' "$repo_root/src/umwelt/engine_api.py")"
[[ -n "$expected_engine_api" ]] || fail "could not read ENGINE_API from src/umwelt/engine_api.py"
grep -qx "umwelt_engine_api=$expected_engine_api" "$UMWELT_HOME/.deployed-version" \
    || fail "marker lacks umwelt_engine_api=$expected_engine_api"
[[ "$(grep -c '^umwelt_engine_api=' "$UMWELT_HOME/.deployed-version")" == "1" ]] \
    || fail "marker has more than one umwelt_engine_api line"
! grep -q '^engine_api=' "$UMWELT_HOME/.deployed-version" \
    || fail "marker still carries the retired unprefixed engine_api key"
pass "marker records umwelt_engine_api=$expected_engine_api (and not the retired engine_api placeholder)"

marketplace_json="$marketplace_root/.claude-plugin/marketplace.json"
plugin_dir="$marketplace_root/plugins/umwelt"
codex_manifest="$plugin_dir/.codex-plugin/plugin.json"

[[ -f "$marketplace_json" ]] || fail "marketplace.json missing after install"
pass "marketplace.json exists"

[[ -f "$plugin_dir/plugin.json" ]] || fail "plugins/umwelt/plugin.json missing after install"
pass "plugins/umwelt/plugin.json exists"

[[ -f "$plugin_dir/.mcp.json" ]] || fail "plugins/umwelt/.mcp.json missing after install"
pass "plugins/umwelt/.mcp.json exists"

claude_mcp_command="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mcpServers"]["umwelt"]["command"])' "$plugin_dir/.mcp.json")"
[[ "$claude_mcp_command" == '${CLAUDE_PLUGIN_ROOT}/launch-umwelt-server.sh' ]] \
    || fail "Claude/default assembly rewrote .mcp.json unexpectedly: $claude_mcp_command"
pass "Claude/default assembly preserves the \${CLAUDE_PLUGIN_ROOT} launcher placeholder"

[[ -f "$codex_manifest" ]] || fail "plugins/umwelt/.codex-plugin/plugin.json missing after install"
pass "plugins/umwelt/.codex-plugin/plugin.json exists"

[[ -x "$plugin_dir/launch-umwelt-server.sh" ]] || fail "plugins/umwelt/launch-umwelt-server.sh missing or not executable"
pass "plugins/umwelt/launch-umwelt-server.sh exists and is executable"

[[ -f "$plugin_dir/umwelt/mcp_server.py" ]] || fail "plugins/umwelt/umwelt/mcp_server.py missing after install"
pass "plugins/umwelt/umwelt/mcp_server.py exists"

# --- 2. marketplace and both assembled manifests use the deterministic
#        commit-derived version, while tracked source keeps its release base. ---
python3 -c "import json; json.load(open('$marketplace_json'))" \
    || fail "marketplace.json is not valid JSON"
pass "marketplace.json is valid JSON"

base_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$repo_root/plugin/plugin.json")"
source_commit="$(git -C "$repo_root" rev-parse HEAD)"
source_short_commit="$(git -C "$repo_root" rev-parse --short=7 HEAD)"
source_commit_epoch="$(git -C "$repo_root" show -s --format=%ct HEAD)"
source_commit_utc="$(python3 -c 'import datetime,sys; print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).strftime("%Y%m%d%H%M%S"))' "$source_commit_epoch")"
market_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$marketplace_json")"
repo_version="$market_version"
expected_clean_version="${base_version}-dev.${source_commit_utc}.g${source_short_commit}"
[[ "$repo_version" == "$expected_clean_version" || "$repo_version" == "$expected_clean_version".dirty.d* ]] \
    || fail "checkout version has unexpected form: $repo_version"
assembled_plugin_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$plugin_dir/plugin.json")"
[[ "$assembled_plugin_version" == "$repo_version" ]] || fail "assembled plugin.json version mismatch: $assembled_plugin_version"
[[ "$base_version" == "0.1.0" ]] || fail "tracked source release base changed unexpectedly: $base_version"
[[ "$out1" == *"derived version -> $repo_version"* ]] || fail "installer did not print derived version"
[[ "$out1" == *"source commit -> $source_commit"* ]] || fail "installer did not print full source commit"
pass "marketplace and both assembled manifests share derived version $repo_version; source remains $base_version"

python3 - <<'PY' "$codex_manifest" "$repo_version"
import json, sys
manifest = json.load(open(sys.argv[1]))
assert manifest["name"] == "umwelt"
assert manifest["version"] == sys.argv[2]
assert manifest["skills"] == "./skills/"
assert manifest["mcpServers"] == "./.mcp.json"
assert "hooks" not in manifest
assert manifest["interface"]["displayName"] == "Umwelt"
PY
pass "Codex manifest has the expected identity, component paths, and interface"

# --- 3. Re-running the SAME install -> idempotent, no error, same tree ---
before_listing="$(find "$marketplace_root" -type f | sort)"
out2="$(bash "$installer" --allow-untagged --allow-dirty --skip-add --skip-venv)"
echo "$out2"
after_listing="$(find "$marketplace_root" -type f | sort)"
[[ "$before_listing" == "$after_listing" ]] || fail "re-running the installer changed the file listing"
pass "re-running the same install is idempotent (no error, same file tree)"

# --- 4. --skip-build: skips the resync step, still assembles the marketplace
#        from whatever's already in plugin/umwelt/ ---
skip_build_root="$tmp_market/marketplace-skip-build"
out_skip_build="$(UMWELT_MARKETPLACE_ROOT="$skip_build_root" bash "$installer" --allow-untagged --allow-dirty --skip-build --skip-add --skip-venv)"
[[ "$out_skip_build" == *"assuming plugin/umwelt/ is already fresh"* ]] \
    || fail "--skip-build did not print the skip-build acknowledgement"
[[ -f "$skip_build_root/plugins/umwelt/plugin.json" ]] || fail "--skip-build still did not assemble the marketplace"
pass "--skip-build skips the resync step and still assembles the marketplace"

# --- 5. Codex target routes through `codex plugin marketplace add` then
#        `codex plugin add umwelt@umwelt-local`, with no real Codex state
#        touched (fake executable + throwaway HOME/CODEX_HOME only).
fake_codex_bin="$tmp_market/fake-codex-bin"
mkdir -p "$fake_codex_bin"
codex_log="$tmp_market/codex.log"
cat > "$fake_codex_bin/codex" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_CODEX_LOG"

# The compatibility tests opt into a narrow simulation of Codex replacing the
# versioned cache during `plugin add`. Other routing tests stay log-only.
if [[ "$*" == "plugin add umwelt@umwelt-local" && -n "${FAKE_CODEX_CACHE_PARENT:-}" ]]; then
    if [[ -n "${FAKE_CODEX_DELETE_PATH:-}" ]]; then
        rm -rf -- "$FAKE_CODEX_DELETE_PATH"
    fi
    version="$(python3 - "$FAKE_CODEX_MARKETPLACE/.claude-plugin/marketplace.json" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1]))["plugins"][0]["version"])
PY
)"
    mkdir -p "$FAKE_CODEX_CACHE_PARENT/$version"
fi
STUB
chmod +x "$fake_codex_bin/codex"

codex_home="$(mktemp -d)"
alt_codex_home="$(mktemp -d)"
codex_market="$codex_home/.umwelt/marketplace"
codex_out="$(PATH="$fake_codex_bin:$PATH" FAKE_CODEX_LOG="$codex_log" \
    HOME="$codex_home" CODEX_HOME="$alt_codex_home" \
    UMWELT_MARKETPLACE_ROOT="$codex_market" \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-venv)"
echo "$codex_out"
[[ -f "$codex_market/plugins/umwelt/.codex-plugin/plugin.json" ]] \
    || fail "codex target did not assemble the plugin tree"
codex_mcp_command="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mcpServers"]["umwelt"]["command"])' "$codex_market/plugins/umwelt/.mcp.json")"
expected_codex_command="$alt_codex_home/plugins/cache/umwelt-local/umwelt/$repo_version/launch-umwelt-server.sh"
[[ "$codex_mcp_command" == "$expected_codex_command" ]] \
    || fail "codex target did not rewrite .mcp.json to the expected absolute cache launcher path: got $codex_mcp_command expected $expected_codex_command"
[[ "$(sed -n '1p' "$codex_log")" == "plugin marketplace add $codex_market" ]] \
    || fail "codex target did not call 'codex plugin marketplace add <marketplace_root>' first"
[[ "$(sed -n '2p' "$codex_log")" == "plugin add umwelt@umwelt-local" ]] \
    || fail "codex target did not call 'codex plugin add umwelt@umwelt-local' second"
[[ "$codex_out" == *"Codex install is already enabled"* ]] \
    || fail "codex target next steps did not say the install is already enabled"
[[ "$codex_out" == *"Start a new Codex session (or restart Codex)"* ]] \
    || fail "codex target next steps did not mention a new Codex session / restart"
[[ "$codex_out" != *"/plugin install umwelt"* ]] \
    || fail "codex target output should not tell the user to run Claude /plugin commands"
source_mcp_command="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mcpServers"]["umwelt"]["command"])' "$repo_root/plugin/.mcp.json")"
[[ "$source_mcp_command" == '${CLAUDE_PLUGIN_ROOT}/launch-umwelt-server.sh' ]] \
    || fail "codex target mutated the source plugin .mcp.json: $source_mcp_command"
pass "codex target rewrites only the assembled .mcp.json, honors alternate CODEX_HOME, and keeps Codex-specific next steps"

# --- 6. In-session Codex upgrades preserve hook paths after the fake `plugin
#        add` deletes the active cache. Consecutive dev versions repoint the
#        old compatibility link, including an already-stale link; real cache
#        directories are never replaced. ---
compat_home="$tmp_market/compat-codex-home"
compat_codex_home="$compat_home/.codex"
compat_cache_parent="$compat_codex_home/plugins/cache/umwelt-local/umwelt"
compat_market="$compat_home/.umwelt/marketplace"
old_compat_version="0.1.0-dev.20000101000000.goldcache"
old_compat_cache="$compat_cache_parent/$old_compat_version"
stale_compat_cache="$compat_cache_parent/0.1.0-dev.20000101000001.gstalecache"
mkdir -p "$old_compat_cache"
printf 'old live cache\n' > "$old_compat_cache/marker"
ln -s "$compat_cache_parent/missing-old-cache" "$stale_compat_cache"

compat_first_out="$(PATH="$fake_codex_bin:$PATH" FAKE_CODEX_LOG="$tmp_market/compat-codex-first.log" \
    FAKE_CODEX_CACHE_PARENT="$compat_cache_parent" FAKE_CODEX_DELETE_PATH="$old_compat_cache" \
    FAKE_CODEX_MARKETPLACE="$compat_market" HOME="$compat_home" CODEX_HOME="$compat_codex_home" \
    CLAUDE_PLUGIN_ROOT="$old_compat_cache" UMWELT_MARKETPLACE_ROOT="$compat_market" \
    UMWELT_INSTALL_TESTING=1 UMWELT_INSTALL_TEST_SHORT_COMMIT=1111111 \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-venv)"
echo "$compat_first_out"
compat_first_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$compat_market/.claude-plugin/marketplace.json")"
compat_first_cache="$compat_cache_parent/$compat_first_version"
[[ -L "$old_compat_cache" ]] || fail "deleted active cache was not restored as a compatibility symlink"
[[ "$(resolve_path "$old_compat_cache")" == "$(resolve_path "$compat_first_cache")" ]] \
    || fail "old active cache link did not resolve to newly installed cache"
[[ "$(resolve_path "$stale_compat_cache")" == "$(resolve_path "$compat_first_cache")" ]] \
    || fail "already-stale compatibility link was not refreshed to the new cache"
pass "Codex deletion of the active cache leaves an old-path symlink to the new cache"

compat_second_out="$(PATH="$fake_codex_bin:$PATH" FAKE_CODEX_LOG="$tmp_market/compat-codex-second.log" \
    FAKE_CODEX_CACHE_PARENT="$compat_cache_parent" FAKE_CODEX_DELETE_PATH="$old_compat_cache" \
    FAKE_CODEX_MARKETPLACE="$compat_market" HOME="$compat_home" CODEX_HOME="$compat_codex_home" \
    CLAUDE_PLUGIN_ROOT="$old_compat_cache" UMWELT_MARKETPLACE_ROOT="$compat_market" \
    UMWELT_INSTALL_TESTING=1 UMWELT_INSTALL_TEST_SHORT_COMMIT=2222222 \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-venv)"
echo "$compat_second_out"
compat_second_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$compat_market/.claude-plugin/marketplace.json")"
compat_second_cache="$compat_cache_parent/$compat_second_version"
[[ "$compat_first_version" != "$compat_second_version" ]] \
    || fail "consecutive synthetic dev upgrades did not produce distinct cache versions"
[[ -L "$old_compat_cache" ]] || fail "consecutive upgrade did not restore the prior compatibility link"
[[ "$(resolve_path "$old_compat_cache")" == "$(resolve_path "$compat_second_cache")" ]] \
    || fail "consecutive upgrade did not repoint old cache link to the second new cache"
[[ "$(resolve_path "$stale_compat_cache")" == "$(resolve_path "$compat_second_cache")" ]] \
    || fail "consecutive upgrade did not refresh the stale compatibility link"
[[ -d "$compat_first_cache" && ! -L "$compat_first_cache" ]] \
    || fail "existing real prior cache was destructively replaced during consecutive upgrade"
pass "consecutive dev upgrades repoint compatibility links without replacing real cache directories"

protected_compat_version="0.1.0-dev.20000101000002.gprotected"
protected_compat_cache="$compat_cache_parent/$protected_compat_version"
mkdir -p "$protected_compat_cache"
printf 'keep real directory\n' > "$protected_compat_cache/marker"
compat_safety_out="$(PATH="$fake_codex_bin:$PATH" FAKE_CODEX_LOG="$tmp_market/compat-codex-safety.log" \
    FAKE_CODEX_CACHE_PARENT="$compat_cache_parent" FAKE_CODEX_MARKETPLACE="$compat_market" \
    HOME="$compat_home" CODEX_HOME="$compat_codex_home" CLAUDE_PLUGIN_ROOT="$protected_compat_cache" \
    UMWELT_MARKETPLACE_ROOT="$compat_market" UMWELT_INSTALL_TESTING=1 \
    UMWELT_INSTALL_TEST_SHORT_COMMIT=3333333 \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-venv)"
echo "$compat_safety_out"
[[ -d "$protected_compat_cache" && ! -L "$protected_compat_cache" ]] \
    || fail "existing real active cache directory was replaced"
[[ "$(cat "$protected_compat_cache/marker")" == "keep real directory" ]] \
    || fail "existing real active cache directory was modified"
pass "existing real active cache directory is preserved without destructive replacement"

# --- 7. Compatibility capture rejects unsafe roots while retaining its narrow
#        fallback for an unset root. Every fixture uses the fake Codex client:
#        no live plugin state is touched. ---
unset_compat_home="$tmp_market/compat-unset-root-home"
unset_compat_codex_home="$unset_compat_home/.codex"
unset_compat_cache_parent="$unset_compat_codex_home/plugins/cache/umwelt-local/umwelt"
unset_compat_market="$unset_compat_home/.umwelt/marketplace"
unset_compat_old="$unset_compat_cache_parent/0.1.0-dev.20000101000003.gfallback"
mkdir -p "$unset_compat_old"
printf 'fallback cache sentinel\n' > "$unset_compat_old/marker"
unset_compat_out="$(env -u CLAUDE_PLUGIN_ROOT PATH="$fake_codex_bin:$PATH" \
    FAKE_CODEX_LOG="$tmp_market/compat-unset-root.log" \
    FAKE_CODEX_CACHE_PARENT="$unset_compat_cache_parent" FAKE_CODEX_DELETE_PATH="$unset_compat_old" \
    FAKE_CODEX_MARKETPLACE="$unset_compat_market" HOME="$unset_compat_home" CODEX_HOME="$unset_compat_codex_home" \
    UMWELT_MARKETPLACE_ROOT="$unset_compat_market" UMWELT_INSTALL_TESTING=1 \
    UMWELT_INSTALL_TEST_SHORT_COMMIT=4444444 \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-venv)"
unset_compat_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$unset_compat_market/.claude-plugin/marketplace.json")"
unset_compat_new="$unset_compat_cache_parent/$unset_compat_version"
[[ "$unset_compat_out" == *"capturing active Codex cache path for in-session compat: $unset_compat_old"* ]] \
    || fail "unset CLAUDE_PLUGIN_ROOT did not select the direct-child fallback cache"
[[ -L "$unset_compat_old" && "$(resolve_path "$unset_compat_old")" == "$(resolve_path "$unset_compat_new")" ]] \
    || fail "unset CLAUDE_PLUGIN_ROOT did not restore the fallback cache as a compatibility link"
[[ -d "$unset_compat_new" && ! -L "$unset_compat_new" ]] \
    || fail "unset CLAUDE_PLUGIN_ROOT did not complete the simulated Codex install"
pass "unset CLAUDE_PLUGIN_ROOT safely falls back to and restores the newest direct cache child"

outside_compat_home="$tmp_market/compat-outside-root-home"
outside_compat_codex_home="$outside_compat_home/.codex"
outside_compat_cache_parent="$outside_compat_codex_home/plugins/cache/umwelt-local/umwelt"
outside_compat_market="$outside_compat_home/.umwelt/marketplace"
outside_compat_root="$outside_compat_home/outside-cache-sentinel"
mkdir -p "$outside_compat_root"
printf 'outside root sentinel\n' > "$outside_compat_root/marker"
outside_compat_out="$(PATH="$fake_codex_bin:$PATH" FAKE_CODEX_LOG="$tmp_market/compat-outside-root.log" \
    FAKE_CODEX_CACHE_PARENT="$outside_compat_cache_parent" FAKE_CODEX_MARKETPLACE="$outside_compat_market" \
    HOME="$outside_compat_home" CODEX_HOME="$outside_compat_codex_home" CLAUDE_PLUGIN_ROOT="$outside_compat_root" \
    UMWELT_MARKETPLACE_ROOT="$outside_compat_market" UMWELT_INSTALL_TESTING=1 \
    UMWELT_INSTALL_TEST_SHORT_COMMIT=5555555 \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-venv)"
outside_compat_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$outside_compat_market/.claude-plugin/marketplace.json")"
[[ "$outside_compat_out" != *"capturing active Codex cache path for in-session compat:"* ]] \
    || fail "CLAUDE_PLUGIN_ROOT outside the exact Umwelt cache parent was captured"
[[ -d "$outside_compat_cache_parent/$outside_compat_version" ]] \
    || fail "outside-root rejection did not complete the simulated Codex install"
[[ -d "$outside_compat_root" && ! -L "$outside_compat_root" && "$(cat "$outside_compat_root/marker")" == "outside root sentinel" ]] \
    || fail "outside CLAUDE_PLUGIN_ROOT sentinel was mutated"
pass "CLAUDE_PLUGIN_ROOT outside the exact Umwelt cache parent is ignored without mutation"

traversal_compat_home="$tmp_market/compat-traversal-root-home"
traversal_compat_codex_home="$traversal_compat_home/.codex"
traversal_compat_cache_parent="$traversal_compat_codex_home/plugins/cache/umwelt-local/umwelt"
traversal_compat_market="$traversal_compat_home/.umwelt/marketplace"
traversal_compat_outside="$(dirname "$traversal_compat_cache_parent")/outside-traversal-sentinel"
mkdir -p "$traversal_compat_outside"
printf 'traversal sentinel\n' > "$traversal_compat_outside/marker"
traversal_compat_out="$(PATH="$fake_codex_bin:$PATH" FAKE_CODEX_LOG="$tmp_market/compat-traversal-root.log" \
    FAKE_CODEX_CACHE_PARENT="$traversal_compat_cache_parent" FAKE_CODEX_MARKETPLACE="$traversal_compat_market" \
    HOME="$traversal_compat_home" CODEX_HOME="$traversal_compat_codex_home" \
    CLAUDE_PLUGIN_ROOT="$traversal_compat_cache_parent/../outside-traversal-sentinel/." \
    UMWELT_MARKETPLACE_ROOT="$traversal_compat_market" UMWELT_INSTALL_TESTING=1 \
    UMWELT_INSTALL_TEST_SHORT_COMMIT=6666666 \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-venv)"
[[ "$traversal_compat_out" != *"capturing active Codex cache path for in-session compat:"* ]] \
    || fail "traversal-normalized CLAUDE_PLUGIN_ROOT outside the direct-child set was captured"
[[ -d "$traversal_compat_outside" && ! -L "$traversal_compat_outside" && "$(cat "$traversal_compat_outside/marker")" == "traversal sentinel" ]] \
    || fail "traversal-normalized outside sentinel was mutated"

nested_compat_home="$tmp_market/compat-nested-root-home"
nested_compat_codex_home="$nested_compat_home/.codex"
nested_compat_cache_parent="$nested_compat_codex_home/plugins/cache/umwelt-local/umwelt"
nested_compat_market="$nested_compat_home/.umwelt/marketplace"
nested_compat_root="$nested_compat_cache_parent/non-version-parent/nested-sentinel"
mkdir -p "$nested_compat_root"
printf 'nested sentinel\n' > "$nested_compat_root/marker"
nested_compat_out="$(PATH="$fake_codex_bin:$PATH" FAKE_CODEX_LOG="$tmp_market/compat-nested-root.log" \
    FAKE_CODEX_CACHE_PARENT="$nested_compat_cache_parent" FAKE_CODEX_MARKETPLACE="$nested_compat_market" \
    HOME="$nested_compat_home" CODEX_HOME="$nested_compat_codex_home" CLAUDE_PLUGIN_ROOT="$nested_compat_root" \
    UMWELT_MARKETPLACE_ROOT="$nested_compat_market" UMWELT_INSTALL_TESTING=1 \
    UMWELT_INSTALL_TEST_SHORT_COMMIT=7777777 \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-venv)"
[[ "$nested_compat_out" != *"capturing active Codex cache path for in-session compat:"* ]] \
    || fail "nested CLAUDE_PLUGIN_ROOT was captured as an active direct child"
[[ -d "$nested_compat_root" && ! -L "$nested_compat_root" && "$(cat "$nested_compat_root/marker")" == "nested sentinel" ]] \
    || fail "nested CLAUDE_PLUGIN_ROOT sentinel was mutated"
pass "traversal-normalized and nested CLAUDE_PLUGIN_ROOT paths are ignored without mutation"

symlink_compat_home="$tmp_market/compat-symlink-root-home"
symlink_compat_codex_home="$symlink_compat_home/.codex"
symlink_compat_cache_parent="$symlink_compat_codex_home/plugins/cache/umwelt-local/umwelt"
symlink_compat_market="$symlink_compat_home/.umwelt/marketplace"
symlink_external_target="$symlink_compat_home/external-cache-target"
symlink_compat_old="$symlink_compat_cache_parent/0.1.0-dev.20000101000004.gsymlink"
mkdir -p "$symlink_external_target" "$(dirname "$symlink_compat_old")"
printf 'external symlink target sentinel\n' > "$symlink_external_target/marker"
ln -s "$symlink_external_target" "$symlink_compat_old"
symlink_compat_out="$(PATH="$fake_codex_bin:$PATH" FAKE_CODEX_LOG="$tmp_market/compat-symlink-root.log" \
    FAKE_CODEX_CACHE_PARENT="$symlink_compat_cache_parent" \
    FAKE_CODEX_MARKETPLACE="$symlink_compat_market" HOME="$symlink_compat_home" CODEX_HOME="$symlink_compat_codex_home" \
    CLAUDE_PLUGIN_ROOT="$symlink_compat_old" UMWELT_MARKETPLACE_ROOT="$symlink_compat_market" \
    UMWELT_INSTALL_TESTING=1 UMWELT_INSTALL_TEST_SHORT_COMMIT=8888888 \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-venv)"
symlink_compat_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$symlink_compat_market/.claude-plugin/marketplace.json")"
symlink_compat_new="$symlink_compat_cache_parent/$symlink_compat_version"
[[ "$symlink_compat_out" == *"capturing active Codex cache path for in-session compat: $symlink_compat_old"* ]] \
    || fail "direct-child symlink was not exercised by the compatibility capture path"
[[ -L "$symlink_compat_old" && "$(resolve_path "$symlink_compat_old")" == "$(resolve_path "$symlink_compat_new")" ]] \
    || fail "direct-child symlink was not repointed as a compatibility link after the simulated Codex install"
[[ -d "$symlink_external_target" && ! -L "$symlink_external_target" && "$(cat "$symlink_external_target/marker")" == "external symlink target sentinel" ]] \
    || fail "compatibility cleanup followed a direct-child symlink into its external target"
pass "direct-child symlink compatibility repoint does not mutate its external target"

# --- 8. Codex target + --skip-add remains a no-live-state escape hatch.
skip_codex_log="$tmp_market/codex-skip.log"
skip_codex_out="$(PATH="$fake_codex_bin:$PATH" FAKE_CODEX_LOG="$skip_codex_log" \
    HOME="$codex_home" CODEX_HOME="$alt_codex_home" \
    UMWELT_MARKETPLACE_ROOT="$codex_home/.umwelt/marketplace-skip-add" \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-add --skip-venv)"
echo "$skip_codex_out"
[[ ! -f "$skip_codex_log" ]] || fail "codex --skip-add should not have invoked the fake codex executable"
skip_codex_mcp_command="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mcpServers"]["umwelt"]["command"])' "$codex_home/.umwelt/marketplace-skip-add/plugins/umwelt/.mcp.json")"
[[ "$skip_codex_mcp_command" == "$expected_codex_command" ]] \
    || fail "codex --skip-add did not assemble the absolute cache launcher path: got $skip_codex_mcp_command expected $expected_codex_command"
[[ "$skip_codex_out" == *"codex plugin marketplace add"* ]] \
    || fail "codex --skip-add output did not include the manual marketplace-add command"
[[ "$skip_codex_out" == *"codex plugin add umwelt@umwelt-local"* ]] \
    || fail "codex --skip-add output did not include the manual plugin-add command"
pass "codex --skip-add avoids live Codex state, still assembles the absolute launcher path, and prints manual commands"

# --- 9. Relative CODEX_HOME still assembles an absolute launcher path. ---
relative_codex_log="$tmp_market/codex-relative.log"
relative_codex_market="$codex_home/.umwelt/marketplace-relative-codex-home"
relative_codex_out="$(cd "$codex_home" && PATH="$fake_codex_bin:$PATH" FAKE_CODEX_LOG="$relative_codex_log" \
    HOME="$codex_home" CODEX_HOME=".codex-relative" \
    UMWELT_MARKETPLACE_ROOT="$relative_codex_market" \
    bash "$installer" --allow-untagged --allow-dirty --target codex --skip-build --skip-add --skip-venv)"
echo "$relative_codex_out"
relative_codex_mcp_command="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mcpServers"]["umwelt"]["command"])' "$relative_codex_market/plugins/umwelt/.mcp.json")"
expected_relative_codex_command="$codex_home/.codex-relative/plugins/cache/umwelt-local/umwelt/$repo_version/launch-umwelt-server.sh"
[[ "$(resolve_path "$relative_codex_mcp_command")" == "$(resolve_path "$expected_relative_codex_command")" ]] \
    || fail "relative CODEX_HOME did not canonicalize to the expected absolute launcher path: got $relative_codex_mcp_command expected $expected_relative_codex_command"
[[ "$relative_codex_mcp_command" = /* ]] \
    || fail "relative CODEX_HOME produced a non-absolute launcher path: $relative_codex_mcp_command"
pass "relative CODEX_HOME still assembles an absolute cache launcher path"

# --- 10. Commit identity is deterministic, changes with HEAD, and rejects a
#        dirty tree unless the caller uses the explicit override. A clone
#        keeps these git-state tests isolated from this checkout. ---
identity_repo="$tmp_market/identity-repo"
git clone -q "$repo_root" "$identity_repo"
identity_installer="$identity_repo/tools/install-local-marketplace-umwelt.sh"
# When this test runs against uncommitted installer work, make the isolated
# fixture exercise that exact script while retaining a clean-checkout baseline.
if ! cmp -s "$installer" "$identity_installer"; then
    cp "$installer" "$identity_installer"
    git -C "$identity_repo" add tools/install-local-marketplace-umwelt.sh
    GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid \
    GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid \
        git -C "$identity_repo" commit -q -m 'test current installer implementation'
fi
# Genuine default-policy happy path: clean checkout, normal build, no dirty
# override. This guards against tests accidentally masking the safe default.
clean_build_market="$tmp_market/clean-build-market"
clean_build_out="$(UMWELT_MARKETPLACE_ROOT="$clean_build_market" bash "$identity_installer" --allow-untagged --skip-add --skip-venv)"
[[ "$clean_build_out" == *"build-umwelt-plugin: synced"* ]] \
    || fail "clean normal install did not run the plugin build"
[[ -f "$clean_build_market/plugins/umwelt/plugin.json" ]] \
    || fail "clean normal install did not assemble the plugin"
[[ -z "$(git -C "$identity_repo" status --porcelain)" ]] \
    || fail "normal build unexpectedly dirtied a fresh current checkout"
pass "clean checkout completes normal build/install without --allow-dirty"

# The derivation appends a prerelease segment, so a source version with build
# metadata (or any other non-release suffix) must be rejected before assembly.
python3 - "$identity_repo/plugin/plugin.json" "$identity_repo/plugin/.codex-plugin/plugin.json" <<'PY'
import json
import sys

for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as source:
        manifest = json.load(source)
    manifest["version"] = "0.1.0+local"
    with open(path, "w", encoding="utf-8") as target:
        json.dump(manifest, target, indent=2)
        target.write("\n")
PY
invalid_base_err="$tmp_market/invalid-base-version.err"
if UMWELT_MARKETPLACE_ROOT="$tmp_market/invalid-base-version" \
    bash "$identity_installer" --allow-untagged --allow-dirty --skip-build --skip-add --skip-venv >/dev/null 2>"$invalid_base_err"; then
    fail "source version with build metadata should be rejected"
fi
grep -q "source manifest version is not a plain SemVer release: 0.1.0+local" "$invalid_base_err" \
    || fail "invalid source version rejection was not explicit"
git -C "$identity_repo" checkout -q -- plugin/plugin.json plugin/.codex-plugin/plugin.json
pass "source version with build metadata is rejected before derivation"

identity_market_1="$tmp_market/identity-market-1"
identity_market_2="$tmp_market/identity-market-2"
identity_out_1="$(UMWELT_MARKETPLACE_ROOT="$identity_market_1" bash "$identity_installer" --allow-untagged --skip-build --skip-add --skip-venv)"
identity_out_2="$(UMWELT_MARKETPLACE_ROOT="$identity_market_2" bash "$identity_installer" --allow-untagged --skip-build --skip-add --skip-venv)"
identity_version_1="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$identity_market_1/.claude-plugin/marketplace.json")"
identity_version_2="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$identity_market_2/.claude-plugin/marketplace.json")"
[[ "$identity_version_1" == "$identity_version_2" ]] \
    || fail "same commit produced different versions: $identity_version_1 vs $identity_version_2"
pass "same commit deterministically produces $identity_version_1"

printf '\nidentity test commit\n' >> "$identity_repo/README.md"
git -C "$identity_repo" add README.md
GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid \
GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid \
GIT_AUTHOR_DATE='2030-01-02T03:04:05Z' GIT_COMMITTER_DATE='2030-01-02T03:04:05Z' \
    git -C "$identity_repo" commit -q -m 'identity test commit'
identity_market_3="$tmp_market/identity-market-3"
UMWELT_MARKETPLACE_ROOT="$identity_market_3" bash "$identity_installer" --allow-untagged --skip-build --skip-add --skip-venv >/dev/null
identity_version_3="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$identity_market_3/.claude-plugin/marketplace.json")"
[[ "$identity_version_3" != "$identity_version_1" ]] \
    || fail "changed HEAD retained old version identity: $identity_version_3"
[[ "$identity_version_3" == *"dev.20300102030405."* ]] \
    || fail "changed HEAD did not use UTC commit timestamp: $identity_version_3"
pass "changed HEAD changes version identity ($identity_version_3)"

dirty_tracked_file="$identity_repo/plugin/skills/umwelt-login/SKILL.md"
printf '\ndirty identity test\n' >> "$dirty_tracked_file"
dirty_err="$tmp_market/dirty.err"
if UMWELT_MARKETPLACE_ROOT="$tmp_market/identity-dirty" \
    bash "$identity_installer" --allow-untagged --skip-build --skip-add --skip-venv >/dev/null 2>"$dirty_err"; then
    fail "dirty-tree install should fail without --allow-dirty"
fi
grep -q "working tree is dirty before build; refusing" "$dirty_err" \
    || fail "dirty-tree failure did not explain the safety refusal"
dirty_override_err="$tmp_market/dirty-override.err"
UMWELT_MARKETPLACE_ROOT="$tmp_market/identity-dirty-override" \
    bash "$identity_installer" --allow-untagged --allow-dirty --skip-build --skip-add --skip-venv >/dev/null 2>"$dirty_override_err"
grep -q "WARNING — dirty-tree override enabled" "$dirty_override_err" \
    || fail "--allow-dirty did not warn that the assembly has a dirty fingerprint"
dirty_version_1="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$tmp_market/identity-dirty-override/.claude-plugin/marketplace.json")"
printf 'another dirty byte\n' >> "$dirty_tracked_file"
UMWELT_MARKETPLACE_ROOT="$tmp_market/identity-dirty-override-2" \
    bash "$identity_installer" --allow-untagged --allow-dirty --skip-build --skip-add --skip-venv >/dev/null 2>/dev/null
dirty_version_2="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$tmp_market/identity-dirty-override-2/.claude-plugin/marketplace.json")"
[[ "$dirty_version_1" == *.dirty.d* && "$dirty_version_1" != "$dirty_version_2" ]] \
    || fail "dirty contents did not get distinct cache identities: $dirty_version_1 / $dirty_version_2"
pass "dirty tree fails loudly by default; override fingerprints actual dirty contents"

# Assembly is intentionally tracked-only. Untracked and ignored files,
# including a FIFO that a recursive copier could block on or install, neither
# change identity nor enter the marketplace.
git -C "$identity_repo" reset --hard -q HEAD
tracked_only_base="$tmp_market/tracked-only-base"
UMWELT_MARKETPLACE_ROOT="$tracked_only_base" \
    bash "$identity_installer" --allow-untagged --skip-build --skip-add --skip-venv >/dev/null
tracked_only_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$tracked_only_base/.claude-plugin/marketplace.json")"
printf 'untracked\n' > "$identity_repo/plugin/untracked-file"
printf 'plugin/ignored-file\n' >> "$identity_repo/.git/info/exclude"
printf 'ignored\n' > "$identity_repo/plugin/ignored-file"
mkfifo "$identity_repo/plugin/untracked-fifo"
tracked_only_market="$tmp_market/tracked-only-noise"
UMWELT_MARKETPLACE_ROOT="$tracked_only_market" \
    bash "$identity_installer" --allow-untagged --skip-build --skip-add --skip-venv >/dev/null
tracked_only_noise_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$tracked_only_market/.claude-plugin/marketplace.json")"
[[ "$tracked_only_noise_version" == "$tracked_only_version" ]] \
    || fail "untracked/ignored plugin noise changed clean identity"
[[ ! -e "$tracked_only_market/plugins/umwelt/untracked-file" \
   && ! -e "$tracked_only_market/plugins/umwelt/ignored-file" \
   && ! -e "$tracked_only_market/plugins/umwelt/untracked-fifo" ]] \
    || fail "untracked, ignored, or FIFO entry entered tracked-only assembly"
rm -f "$identity_repo/plugin/untracked-file" "$identity_repo/plugin/ignored-file" "$identity_repo/plugin/untracked-fifo"
pass "untracked/ignored files and FIFOs neither perturb identity nor enter assembly"

# Git-tracked symlinks are copied as links (never dereferenced).
ln -s "../plugin.json" "$identity_repo/plugin/skills/tracked-manifest-link"
git -C "$identity_repo" add plugin/skills/tracked-manifest-link
GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid \
GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid \
    git -C "$identity_repo" commit -q -m 'tracked plugin symlink fixture'
tracked_symlink_market="$tmp_market/tracked-symlink"
UMWELT_MARKETPLACE_ROOT="$tracked_symlink_market" \
    bash "$identity_installer" --allow-untagged --skip-build --skip-add --skip-venv >/dev/null
[[ -L "$tracked_symlink_market/plugins/umwelt/skills/tracked-manifest-link" ]] \
    || fail "tracked plugin symlink was dereferenced or omitted"
[[ "$(readlink "$tracked_symlink_market/plugins/umwelt/skills/tracked-manifest-link")" == "../plugin.json" ]] \
    || fail "tracked plugin symlink target text changed"
pass "tracked plugin symlink is preserved without dereferencing"

# A tracked regular path replaced in the worktree by a special file is rejected
# even under --allow-dirty; no special file can enter the assembled tree.
special_path="$identity_repo/plugin/hooks/umwelt-unseen-hook.py"
rm "$special_path"
mkfifo "$special_path"
special_err="$tmp_market/tracked-special.err"
if UMWELT_MARKETPLACE_ROOT="$tmp_market/tracked-special" \
    bash "$identity_installer" --allow-untagged --allow-dirty --skip-build --skip-add --skip-venv >/dev/null 2>"$special_err"; then
    fail "tracked path replaced by FIFO should be rejected"
fi
grep -Eq "unsupported worktree type \(special file\)|tracked regular file became a special file" "$special_err" \
    || fail "tracked special-file rejection was not explicit"
rm "$special_path"
git -C "$identity_repo" checkout -q -- plugin/hooks/umwelt-unseen-hook.py
pass "tracked special file is rejected rather than copied"

if grep -Eq 'sort -z|sha256sum' "$identity_installer"; then
    fail "dirty fingerprint still relies on GNU-only sort -z or sha256sum"
fi
pass "dirty fingerprint remains portable without GNU-only tools"

# A clean checkout whose generated plugin copy is stale is clean before build
# and dirty after it. The post-build safety gate must catch that transition.
post_build_repo="$tmp_market/post-build-repo"
git clone -q "$repo_root" "$post_build_repo"
printf '\n# source-only post-build safety fixture\n' >> "$post_build_repo/src/umwelt/avatar_status.py"
git -C "$post_build_repo" add src/umwelt/avatar_status.py
GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid \
GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid \
    git -C "$post_build_repo" commit -q -m 'leave generated plugin stale'
[[ -z "$(git -C "$post_build_repo" status --porcelain)" ]] || fail "post-build fixture was not clean before build"
post_build_err="$tmp_market/post-build.err"
if UMWELT_MARKETPLACE_ROOT="$tmp_market/post-build-market" \
    bash "$post_build_repo/tools/install-local-marketplace-umwelt.sh" --allow-untagged --skip-add --skip-venv >/dev/null 2>"$post_build_err"; then
    fail "build-dirtied checkout should fail without --allow-dirty"
fi
grep -q "working tree is dirty after build; refusing" "$post_build_err" \
    || fail "post-build refusal did not identify the after-build safety gate"
pass "clean-before-build checkout that build dirties is refused by default"

# The formatter prefixes even an all-digit, leading-zero synthetic SHA, so it
# remains a valid SemVer prerelease identifier rather than numeric `0123456`.
leading_zero_market="$tmp_market/leading-zero-market"
UMWELT_INSTALL_TESTING=1 UMWELT_INSTALL_TEST_SHORT_COMMIT=0123456 \
    UMWELT_MARKETPLACE_ROOT="$leading_zero_market" \
    bash "$identity_installer" --allow-untagged --allow-dirty --skip-build --skip-add --skip-venv >/dev/null 2>/dev/null
leading_zero_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$leading_zero_market/.claude-plugin/marketplace.json")"
[[ "$leading_zero_version" == *".g0123456" || "$leading_zero_version" == *".g0123456.dirty.d"* ]] \
    || fail "leading-zero SHA was not prefixed as a SemVer-safe identifier: $leading_zero_version"
pass "leading-zero synthetic SHA formats as SemVer-safe g0123456"

# Dirty digests are also prefixed: an all-digit leading-zero fingerprint must
# never become an invalid numeric SemVer prerelease identifier.
leading_zero_dirty_market="$tmp_market/leading-zero-dirty-market"
printf '\nleading-zero dirty hash fixture\n' >> "$identity_repo/plugin/skills/umwelt-login/SKILL.md"
UMWELT_INSTALL_TESTING=1 UMWELT_INSTALL_TEST_DIRTY_HASH=012345678901 \
    UMWELT_MARKETPLACE_ROOT="$leading_zero_dirty_market" \
    bash "$identity_installer" --allow-untagged --allow-dirty --skip-build --skip-add --skip-venv >/dev/null 2>/dev/null
leading_zero_dirty_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$leading_zero_dirty_market/.claude-plugin/marketplace.json")"
[[ "$leading_zero_dirty_version" == *".dirty.d012345678901" ]] \
    || fail "leading-zero dirty digest was not prefixed as a SemVer-safe identifier: $leading_zero_dirty_version"
git -C "$identity_repo" checkout -q -- plugin/skills/umwelt-login/SKILL.md
pass "leading-zero synthetic dirty digest formats as SemVer-safe d012345678901"

# --- 11. Missing plugin/plugin.json -> installer fails loudly, not silently ---
# Temporarily moves the REAL repo's plugin/plugin.json aside (this worktree's
# own copy only — git worktrees are separate checkouts, so this never touches
# another worktree or the real ~/.umwelt). Guarded by its own restore-on-exit
# so a failure mid-test can't leave the repo checkout without plugin.json.
plugin_json_real="$repo_root/plugin/plugin.json"
plugin_json_backup="$tmp_market/plugin.json.bak"
mv "$plugin_json_real" "$plugin_json_backup"
restore_plugin_json() {
    [[ -f "$plugin_json_backup" ]] && mv "$plugin_json_backup" "$plugin_json_real"
}
trap 'restore_plugin_json; cleanup' EXIT
missing_root="$tmp_market/marketplace-missing-plugin-json"
if UMWELT_MARKETPLACE_ROOT="$missing_root" bash "$installer" --allow-untagged --allow-dirty --skip-build --skip-add --skip-venv >/dev/null 2>&1; then
    fail "installer should have failed with plugin/plugin.json missing"
fi
restore_plugin_json
trap cleanup EXIT
pass "installer fails loudly when plugin/plugin.json is missing"

# --- 12. Omitting --skip-venv actually creates ~/.umwelt/venv (real network +
#        venv tooling required — genuinely exercises the venv-creation step
#        rather than trusting the flag alone). Isolated to a scratch $HOME
#        so it never touches this session's real ~/.umwelt. Bounded to one
#        test case since a real venv + pip install costs real wall-clock
#        time (observed ~8s in this sandbox).
venv_home="$(mktemp -d)"
venv_marketplace_root="$venv_home/.umwelt/marketplace"
if env HOME="$venv_home" UMWELT_MARKETPLACE_ROOT="$venv_marketplace_root" \
    bash "$installer" --allow-untagged --allow-dirty --skip-build --skip-add >/dev/null; then
    [[ -x "$venv_home/.umwelt/venv/bin/python3" ]] \
        || fail "installer (no --skip-venv) did not create \$HOME/.umwelt/venv/bin/python3"
    pass "omitting --skip-venv creates \$HOME/.umwelt/venv/bin/python3"

    [[ -f "$venv_home/.umwelt/venv/pyvenv.cfg" ]] \
        || fail "installer (no --skip-venv) did not write a real venv (pyvenv.cfg missing)"
    pass "omitting --skip-venv writes a real venv (pyvenv.cfg present)"

    # fastmcp importability is a softer check — network/pip access can be
    # unreliable in some sandboxes, and venv creation itself (asserted
    # above) is the load-bearing behavior this test guards.
    if "$venv_home/.umwelt/venv/bin/python3" -c "import fastmcp" >/dev/null 2>&1; then
        pass "omitting --skip-venv leaves fastmcp importable in the new venv"
    else
        echo "note: fastmcp not importable in the freshly-created venv (network/pip may be limited in this sandbox) — venv creation itself is still verified above" >&2
    fi
else
    echo "note: installer run without --skip-venv did not complete (network/venv tooling may be unavailable in this sandbox) — skipping venv-creation verification" >&2
fi
rm -rf "$venv_home" "$codex_home"

# --- 13. Venv-creation failure degrades (warn + continue + exit 0), rather
#        than a raw `set -e` abort with no Next-steps guidance — the exact
#        "degrade without warning" shape Ariadne's re-review flagged.
#        Shadows `python3` on PATH so `python3 -m venv` fails, delegating
#        every other invocation (incl. this script's own JSON parsing) to
#        the real interpreter.
real_python3="$(command -v python3)"
stub_bin="$(mktemp -d)"
cat > "$stub_bin/python3" <<STUB
#!/usr/bin/env bash
if [[ "\$1" == "-m" && "\$2" == "venv" ]]; then
    echo "stub python3: simulated venv-creation failure" >&2
    exit 1
fi
exec "$real_python3" "\$@"
STUB
chmod +x "$stub_bin/python3"

degrade_home="$(mktemp -d)"
degrade_market="$degrade_home/.umwelt/marketplace"
degrade_out="$(PATH="$stub_bin:$PATH" env HOME="$degrade_home" \
    UMWELT_MARKETPLACE_ROOT="$degrade_market" \
    bash "$installer" --allow-untagged --allow-dirty --skip-build --skip-add)" \
    || fail "installer should exit 0 on a degraded venv-creation failure (not abort)"
echo "$degrade_out"
[[ "$degrade_out" == *"WARNING"* ]] || fail "degraded venv failure did not print a WARNING"
[[ "$degrade_out" == *"Next steps:"* ]] || fail "degraded venv failure skipped the Next-steps printout"
[[ ! -e "$degrade_home/.umwelt/venv/bin/python3" ]] \
    || fail "degraded venv failure should not have left a working venv"
pass "venv-creation failure degrades to a warning + Next-steps guidance, exit 0"
rm -rf "$stub_bin" "$degrade_home"

rm -rf "$alt_codex_home"

echo "ALL TESTS PASSED"
