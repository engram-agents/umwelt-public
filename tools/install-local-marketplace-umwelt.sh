#!/usr/bin/env bash
# tools/install-local-marketplace-umwelt.sh — assemble a local Claude Code
# plugin marketplace for the umwelt MCP plugin (umwelt #132) and register it.
#
# Mirrors the SHAPE of ENGRAM's tools/install-local-marketplace.sh, but
# drastically simplified for umwelt's stateless plugin: no build-tier flags,
# no multi-agent flags, no Codex target, no CLAUDE.md/identity-layer
# templating, no version-marker/hook-change-detection machinery. Just:
#
#   1. (re)build plugin/umwelt/ from src/umwelt/ (tools/build-umwelt-plugin.sh)
#   2. assemble ~/.umwelt/marketplace/ (marketplace.json + a copy of plugin/)
#   3. validate the written marketplace.json
#   4. register it with the selected client (`claude` or `codex`, unless skipped)
#   5. create + populate ~/.umwelt/venv (unless skipped) — the same venv
#      plugin/launch-umwelt-server.sh hard-requires, so install -> enable ->
#      restart doesn't land a dead MCP server (umwelt #132 colleague review)
#
# Usage:
#   tools/install-local-marketplace-umwelt.sh [--target claude|codex] [--skip-build] [--skip-add] [--skip-venv] [--allow-dirty] [--allow-untagged]
#
#   --skip-build   don't run tools/build-umwelt-plugin.sh first (assumes
#                  plugin/umwelt/ is already fresh)
#   --target       install target (`claude` by default; `codex` for Codex)
#   --skip-add     assemble the marketplace tree but don't touch the selected
#                  client's live plugin state (useful for testing)
#   --skip-venv    don't create/populate the ~/.umwelt/venv MCP-server venv
#                  (useful for testing — avoids a real venv + a real network
#                  pip install on every test run)
#   --allow-dirty  explicitly assemble uncommitted source; the stamped
#                  version includes a hash of the dirty contents
#   --allow-untagged  build although HEAD has no deployable tag (deploy/* or
#                  v*) merged into origin/main; the marker records the override
#   -h, --help     show this help and exit
#
# Env:
#   UMWELT_HOME               state dir for the .deployed-version marker
#                             (default: $HOME/.umwelt)
#   UMWELT_MARKETPLACE_ROOT   marketplace dir to assemble (default:
#                             $HOME/.umwelt/marketplace). Overriding this is
#                             what makes the script testable against a
#                             throwaway directory — load-bearing for
#                             tools/test-install-local-marketplace-umwelt.sh;
#                             do not remove the override.
#   UMWELT_LSREMOTE_TIMEOUT   seconds before the guard's `git ls-remote` query of
#                             origin is cut off (default: 8)
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: install-local-marketplace-umwelt.sh [--target claude|codex] [--skip-build] [--skip-add] [--skip-venv] [--allow-dirty] [--allow-untagged]

  --target       install target (`claude` by default; `codex` to register +
                 install into Codex)
  --skip-build   don't (re)build plugin/umwelt/ from src/umwelt/ first
  --skip-add     assemble the marketplace tree but don't touch the selected
                 client's live plugin state
  --skip-venv    don't create/populate the ~/.umwelt/venv MCP-server venv
  --allow-dirty  assemble despite uncommitted changes; identity includes a diff hash
  --allow-untagged
                 build although HEAD has no deployable tag. The rule:
                 deployable tags = deploy/* and v*, and the tagged commit must
                 be reachable from origin/main. Without this flag the installer
                 refuses (no tag, tag not merged into origin/main, or a tag
                 origin has but this clone lacks: git fetch --tags). The
                 override is recorded in the marker (umwelt_untagged_override=1).
  -h, --help     show this help

A build writes $UMWELT_HOME/.deployed-version (default
$HOME/.umwelt/.deployed-version) once the assembled tree validates,
before registration. It does not attest registration (--skip-add or a
missing claude/codex leaves none, exit 0). A refused build writes nothing.

Env:
  UMWELT_MARKETPLACE_ROOT   marketplace dir to assemble (default:
                            $HOME/.umwelt/marketplace)
  UMWELT_HOME               state dir for the marker (default: $HOME/.umwelt)
  UMWELT_LSREMOTE_TIMEOUT   seconds before the guard's `git ls-remote` query of
                            origin is cut off (default: 8)
EOF
}

skip_build=0
skip_add=0
skip_venv=0
allow_dirty=0
allow_untagged=0
target="claude"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            [[ $# -ge 2 ]] || { echo "install-local-marketplace-umwelt.sh: --target requires claude or codex" >&2; exit 1; }
            target="$2"
            case "$target" in
                claude|codex) ;;
                *)
                    echo "install-local-marketplace-umwelt.sh: unsupported target: $target (expected claude or codex)" >&2
                    exit 1
                    ;;
            esac
            shift 2
            ;;
        --skip-build)
            skip_build=1
            shift
            ;;
        --skip-add)
            skip_add=1
            shift
            ;;
        --skip-venv)
            skip_venv=1
            shift
            ;;
        --allow-dirty)
            allow_dirty=1
            shift
            ;;
        --allow-untagged)
            allow_untagged=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "install-local-marketplace-umwelt.sh: unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

if ! git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "install-local-marketplace-umwelt.sh: $repo_root is not a git repository" >&2
    exit 1
fi

source_commit="$(git -C "$repo_root" rev-parse HEAD)"
source_short_commit="$(git -C "$repo_root" rev-parse --short=7 HEAD)"
if [[ "${UMWELT_INSTALL_TESTING:-0}" == "1" && -n "${UMWELT_INSTALL_TEST_SHORT_COMMIT:-}" ]]; then
    source_short_commit="$UMWELT_INSTALL_TEST_SHORT_COMMIT"
fi
source_commit_epoch="$(git -C "$repo_root" show -s --format=%ct HEAD)"
source_commit_utc="$(python3 - "$source_commit_epoch" <<'PY'
import datetime
import sys

print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).strftime("%Y%m%d%H%M%S"))
PY
)"

dirty_fingerprint() {
    if [[ "${UMWELT_INSTALL_TESTING:-0}" == "1" && -n "${UMWELT_INSTALL_TEST_DIRTY_HASH:-}" ]]; then
        printf '%s\n' "$UMWELT_INSTALL_TEST_DIRTY_HASH"
        return
    fi
    # Python is already a hard installer dependency (manifest parsing and UTC
    # formatting above). Keep this portable across GNU and BSD/macOS hosts,
    # and never dereference or read special untracked files.
    python3 - "$repo_root" <<'PY'
import hashlib
import os
import subprocess
import sys

root = os.fsencode(sys.argv[1])
digest = hashlib.sha256()


def frame(tag, value):
    """Hash an unambiguous binary field."""
    digest.update(tag)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


# The assembled plugin and the inputs which can regenerate its vendored Python
# are the complete identity surface.  Untracked/ignored files are deliberately
# absent: tracked-only assembly below can never install them.
surface = [
    b"plugin",
    b"src/umwelt/__init__.py",
    b"src/umwelt/mcp_server.py",
    b"src/umwelt/avatar_status.py",
    b"tools/build-umwelt-plugin.sh",
]

# Git's binary diff covers staged/unstaged tracked modifications, mode changes,
# symlink targets, and deletions. Disable external diff drivers so local
# configuration cannot change the identity.
diff_result = subprocess.run(
    [b"git", b"-C", root, b"diff", b"--no-ext-diff", b"--binary", b"HEAD", b"--", *surface],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
if diff_result.returncode:
    raise SystemExit(
        "installer: tracked assembly/build path has an unsupported worktree "
        "type (special file); refusing to fingerprint or copy it"
    )
tracked = diff_result.stdout
frame(b"D", tracked)

print(digest.hexdigest()[:12])
PY
}

check_dirty_tree() {
    local phase="$1"
    if [[ -z "$(git -C "$repo_root" status --porcelain --untracked-files=no -- \
        plugin src/umwelt/__init__.py src/umwelt/mcp_server.py \
        src/umwelt/avatar_status.py tools/build-umwelt-plugin.sh)" ]]; then
        return 0
    fi
    if [[ "$allow_dirty" -eq 0 ]]; then
        echo "install-local-marketplace-umwelt.sh: working tree is dirty $phase; refusing to stamp uncommitted contents as commit $source_commit" >&2
        echo "install-local-marketplace-umwelt.sh: commit/stash the changes, or rerun with --allow-dirty to accept a non-reproducible assembly" >&2
        exit 1
    fi
}

# --- Deploy-tag guard (umwelt #1104) ---------------------------------------
# Every commit a deployment is built from carries a deployable tag
# (`deploy/YYYY-MM-DD-<name>` or `v*`), and that commit is reachable from
# origin/main. This runs BEFORE the build and before any marketplace or marker
# write, so a refusal leaves no trace. Plain `git` only.
#
#   A. no deployable tag at HEAD                   -> refuse
#   B. deployable tag at HEAD, but HEAD is not reachable from origin/main
#      (tag on an unmerged commit, or origin/main not available locally)
#                                                  -> refuse, distinct message
#   C. no deployable tag locally, but origin has one on this commit
#      (stale local tags)                          -> refuse, names `git fetch --tags`
#
# --allow-untagged bypasses ALL THREE refusals (A, B and C) and repeats the
# guard message as a WARNING; the marker then records umwelt_tag=none and
# umwelt_untagged_override=1. There is deliberately no automatic fetch.
deployable_tag_rule="deployable tags = deploy/* and v*"
deploy_tag=""
untagged_override=0
guard_reason=""
guard_hint=""

# Limit (seconds) for the one network call the guard makes. A stock Mac has
# neither `timeout` nor `gtimeout`, so the last branch uses python3 (already a
# hard installer dependency); it kills the child on expiry and exits 124.
run_with_timeout() {
    local limit="${UMWELT_LSREMOTE_TIMEOUT:-8}"
    if command -v timeout >/dev/null 2>&1; then
        timeout "$limit" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$limit" "$@"
    else
        python3 -c '
import subprocess
import sys

try:
    raise SystemExit(subprocess.run(sys.argv[2:], timeout=float(sys.argv[1])).returncode)
except subprocess.TimeoutExpired:
    raise SystemExit(124)
' "$limit" "$@"
    fi
}

evaluate_tag_guard() {
    local tags_at_head
    tags_at_head="$(git -C "$repo_root" tag --points-at HEAD --list 'deploy/*' 'v*' | LC_ALL=C sort | sed -n 1p)"
    if [[ -n "$tags_at_head" ]]; then
        if git -C "$repo_root" merge-base --is-ancestor HEAD refs/remotes/origin/main >/dev/null 2>&1; then
            deploy_tag="$tags_at_head"
            return 0
        fi
        guard_reason="tag $tags_at_head is at HEAD $source_short_commit, but the tagged commit is not confirmed merged into origin/main ($deployable_tag_rule)"
        guard_hint="run \`git fetch origin\` if origin/main is stale, or merge the commit to main first"
        return 1
    fi

    local nearest remote_tag ls_status ls_out ls_note=""
    nearest="$(git -C "$repo_root" describe --tags --abbrev=0 2>/dev/null || true)"
    [[ -n "$nearest" ]] || nearest="none"

    # Stale local tags: does origin already have a deployable tag on this commit?
    ls_status=0
    ls_out="$(GIT_TERMINAL_PROMPT=0 run_with_timeout git -C "$repo_root" ls-remote --tags origin 'refs/tags/deploy/*' 'refs/tags/v*' 2>/dev/null)" || ls_status=$?
    if [[ "$ls_status" -ne 0 ]]; then
        ls_note=" [could not query origin for tags (git ls-remote failed)]"
    else
        # A lightweight tag lists the commit itself; an annotated tag lists the
        # tag object and, on a second `^{}` line, the peeled commit.
        remote_tag="$(printf '%s\n' "$ls_out" | awk -v head="$source_commit" '$1 == head { sub(/^refs\/tags\//, "", $2); sub(/\^\{\}$/, "", $2); print $2; exit }')"
        if [[ -n "$remote_tag" ]]; then
            guard_reason="no deployable tag at HEAD locally ($deployable_tag_rule), but origin has $remote_tag on this commit; \`git fetch --tags\` brings it local"
            guard_hint="run \`git fetch --tags\` and rebuild"
            return 1
        fi
    fi
    guard_reason="no deployable tag at HEAD ($deployable_tag_rule): HEAD $source_short_commit, nearest tag: $nearest$ls_note"
    guard_hint="tag the commit (deploy/YYYY-MM-DD-<name>) and rebuild"
    return 1
}

if ! evaluate_tag_guard; then
    if [[ "$allow_untagged" -eq 1 ]]; then
        echo "install-local-marketplace-umwelt.sh: WARNING — --allow-untagged override: $guard_reason" >&2
        untagged_override=1
        deploy_tag=""
    else
        echo "install-local-marketplace-umwelt.sh: refusing to build: $guard_reason" >&2
        echo "install-local-marketplace-umwelt.sh: $guard_hint, or pass --allow-untagged to build anyway (recorded in the marker as an override)" >&2
        exit 1
    fi
fi

check_dirty_tree "before build"

if [[ "$skip_build" -eq 0 ]]; then
    bash "$script_dir/build-umwelt-plugin.sh"
else
    echo "install-local-marketplace-umwelt.sh: --skip-build set — assuming plugin/umwelt/ is already fresh"
fi

# The build copies source into tracked generated files. A checkout can be clean
# before the build but become dirty when those generated files were stale at
# HEAD, so this second gate is the load-bearing safety check.
check_dirty_tree "after build"

plugin_src="$repo_root/plugin"
[[ -d "$plugin_src" ]] || { echo "install-local-marketplace-umwelt.sh: $plugin_src not found" >&2; exit 1; }
[[ -f "$plugin_src/plugin.json" ]] || { echo "install-local-marketplace-umwelt.sh: $plugin_src/plugin.json not found" >&2; exit 1; }
[[ -f "$plugin_src/.codex-plugin/plugin.json" ]] || { echo "install-local-marketplace-umwelt.sh: $plugin_src/.codex-plugin/plugin.json not found" >&2; exit 1; }

base_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$plugin_src/plugin.json")"
codex_base_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$plugin_src/.codex-plugin/plugin.json")"
[[ "$base_version" == "$codex_base_version" ]] || {
    echo "install-local-marketplace-umwelt.sh: source manifest versions differ: plugin.json=$base_version .codex-plugin/plugin.json=$codex_base_version" >&2
    exit 1
}
if ! python3 - "$base_version" <<'PY'
import re
import sys

# The installer appends its own prerelease segment, so the source manifest is
# deliberately a plain SemVer release rather than a prerelease/build version.
raise SystemExit(0 if re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", sys.argv[1]) else 1)
PY
then
    echo "install-local-marketplace-umwelt.sh: source manifest version is not a plain SemVer release: $base_version" >&2
    exit 1
fi
version="${base_version}-dev.${source_commit_utc}.g${source_short_commit}"
if [[ -n "$(git -C "$repo_root" status --porcelain --untracked-files=no -- \
    plugin src/umwelt/__init__.py src/umwelt/mcp_server.py \
    src/umwelt/avatar_status.py tools/build-umwelt-plugin.sh)" ]]; then
    dirty_hash="$(dirty_fingerprint)"
    version="${version}.dirty.d${dirty_hash}"
    echo "install-local-marketplace-umwelt.sh: WARNING — dirty-tree override enabled; version includes content fingerprint $dirty_hash" >&2
fi

marketplace_root="${UMWELT_MARKETPLACE_ROOT:-$HOME/.umwelt/marketplace}"
plugins_dest="$marketplace_root/plugins/umwelt"
marketplace_json="$marketplace_root/.claude-plugin/marketplace.json"

mkdir -p "$marketplace_root/.claude-plugin"
rm -rf "$plugins_dest"
mkdir -p "$plugins_dest"

# Assemble exactly the Git-tracked plugin surface.  A recursive filesystem
# copy can accidentally install ignored files, sockets, or FIFOs which Git did
# not fingerprint.  This copier streams regular files, preserves executable
# bits and symlink text without following links, and rejects every special
# worktree object (including a tracked path replaced by a FIFO during a dirty
# development install).
python3 - "$repo_root" "$plugins_dest" <<'PY'
import os
import shutil
import stat
import subprocess
import sys

root = os.fsencode(sys.argv[1])
dest = os.fsencode(sys.argv[2])
listing = subprocess.run(
    [b"git", b"-C", root, b"ls-files", b"-s", b"-z", b"--", b"plugin"],
    check=True,
    stdout=subprocess.PIPE,
).stdout

for record in (item for item in listing.split(b"\0") if item):
    metadata, path = record.split(b"\t", 1)
    mode, _object_id, stage = metadata.split(b" ", 2)
    if stage != b"0":
        raise SystemExit(f"installer: unmerged tracked plugin path: {os.fsdecode(path)}")
    prefix = b"plugin/"
    if not path.startswith(prefix):
        raise SystemExit(f"installer: unexpected tracked plugin path: {os.fsdecode(path)}")
    relative = path[len(prefix):]
    source = os.path.join(root, path)
    target = os.path.join(dest, relative)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        raise SystemExit(f"installer: tracked plugin path is missing: {os.fsdecode(path)}")

    if mode in (b"100644", b"100755"):
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(f"installer: tracked regular file became a special file: {os.fsdecode(path)}")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(source, flags)
        except OSError as error:
            raise SystemExit(f"installer: cannot safely open tracked file {os.fsdecode(path)}: {error}")
        with os.fdopen(descriptor, "rb") as input_file:
            if not stat.S_ISREG(os.fstat(input_file.fileno()).st_mode):
                raise SystemExit(f"installer: tracked path changed type while copying: {os.fsdecode(path)}")
            with open(target, "wb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        os.chmod(target, 0o755 if mode == b"100755" else 0o644)
    elif mode == b"120000":
        if not stat.S_ISLNK(info.st_mode):
            raise SystemExit(f"installer: tracked symlink changed type: {os.fsdecode(path)}")
        link_text = os.readlink(source)
        os.symlink(link_text, target)
    else:
        raise SystemExit(
            f"installer: unsupported tracked plugin mode {os.fsdecode(mode)}: {os.fsdecode(path)}"
        )
PY

# Source manifests carry the human release base. Only the assembled copy gets
# a commit-derived cache identity, so developers never hand-edit cachebusters.
python3 - "$plugins_dest/plugin.json" "$plugins_dest/.codex-plugin/plugin.json" "$version" <<'PY'
import json
import sys

for path in sys.argv[1:3]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["version"] = sys.argv[3]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
PY

# Codex can remove the old versioned cache while `codex plugin add` installs
# the new one. Capture a direct child of this plugin's cache before that call,
# so a live session's hook paths can be preserved afterward. This is scoped to
# umwelt-local/umwelt and never treats a nested or unrelated path as eligible.
old_codex_plugin_cache_dir=""
codex_cache_parent=""
if [[ "$target" == "codex" ]]; then
    codex_home="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "${CODEX_HOME:-$HOME/.codex}")"
    codex_cache_parent="$codex_home/plugins/cache/umwelt-local/umwelt"
    if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
        normalized_plugin_root="$(python3 - "$CLAUDE_PLUGIN_ROOT" <<'PY'
import os
import sys
print(os.path.abspath(os.path.normpath(sys.argv[1])))
PY
)"
        if [[ "$(dirname "$normalized_plugin_root")" == "$codex_cache_parent" ]] && [[ -d "$normalized_plugin_root" ]]; then
            old_codex_plugin_cache_dir="$normalized_plugin_root"
        fi
    fi
    if [[ -z "$old_codex_plugin_cache_dir" && -d "$codex_cache_parent" ]]; then
        old_codex_plugin_cache_dir="$(python3 - "$codex_cache_parent" <<'PY'
import os
import re
import sys

root = sys.argv[1]

def version_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.split(r"[^0-9]+", name) if part)

candidates = [
    os.path.join(root, name)
    for name in os.listdir(root)
    if re.fullmatch(r".+-dev\..+", name) and os.path.isdir(os.path.join(root, name))
]
if candidates:
    candidates.sort(key=lambda candidate: (version_key(os.path.basename(candidate)), os.path.basename(candidate)))
    print(candidates[-1])
PY
)"
    fi
    if [[ -n "$old_codex_plugin_cache_dir" ]]; then
        echo "install-local-marketplace-umwelt.sh: capturing active Codex cache path for in-session compat: $old_codex_plugin_cache_dir"
    fi

    codex_launcher="$codex_cache_parent/$version/launch-umwelt-server.sh"

    # Codex starts MCP servers from the installed cache path, and current
    # Codex does not expand ${CLAUDE_PLUGIN_ROOT} inside plugin .mcp.json.
    # Rewrite only the assembled marketplace copy for Codex so the source
    # plugin tree (and Claude installs) keep the placeholder form.
    python3 - "$plugins_dest/.mcp.json" "$codex_launcher" <<'PY'
import json
import sys

path, command = sys.argv[1:3]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
data.setdefault("mcpServers", {}).setdefault("umwelt", {})["command"] = command
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
fi

cat > "$marketplace_json" <<EOF
{
  "name": "umwelt-local",
  "owner": {
    "name": "engram-agents"
  },
  "plugins": [
    {
      "name": "umwelt",
      "source": "./plugins/umwelt",
      "version": "$version",
      "description": "Umwelt — walk the agent-native world as native tool-calls (MCP wrapper over the umwelt HTTP world server)."
    }
  ]
}
EOF

python3 -c "import json; json.load(open('$marketplace_json'))" \
    || { echo "install-local-marketplace-umwelt.sh: wrote invalid JSON to $marketplace_json" >&2; exit 1; }
python3 -c "import json; json.load(open('$plugins_dest/.codex-plugin/plugin.json'))" \
    || { echo "install-local-marketplace-umwelt.sh: copied invalid JSON to $plugins_dest/.codex-plugin/plugin.json" >&2; exit 1; }

# --- Deployed-version marker (umwelt #1104) -------------------------------
# Written on EVERY successful assembly (also with --skip-add), never on a
# refused one (the guard above exits first). It is written HERE, right after
# the assembled tree validates and BEFORE registration: the marker describes
# what was assembled, so a failed registration (which aborts under `set -e`)
# leaves the assembled tree and its marker in agreement, never a marker naming
# the previous deploy beside a rewritten tree. key=value lines; the
# umwelt-owned keys carry the `umwelt_` prefix so a reader of another system's
# marker never confuses the two. Atomic: write a temp file in the same
# directory, then mv.
umwelt_state_home="${UMWELT_HOME:-$HOME/.umwelt}"
marker_path="$umwelt_state_home/.deployed-version"
mkdir -p "$umwelt_state_home"
marker_tmp="$(mktemp "$umwelt_state_home/.deployed-version.XXXXXX")"
marker_branch="$(git -C "$repo_root" rev-parse --abbrev-ref HEAD)"
marker_dirty="clean"
if [[ -n "$(git -C "$repo_root" status --porcelain --untracked-files=no)" ]]; then
    marker_dirty="dirty"
fi
marker_tag="${deploy_tag:-none}"
# umwelt_engine_api is the #1105 field (umwelt-owned, so `umwelt_`-prefixed; the
# unprefixed `engine_api` key of #1104 is retired). The key is ALWAYS present: its
# value is the ENGINE_API number of the tree being deployed, read from that tree's
# src/umwelt/engine_api.py (the one place the number lives -- never hard-coded
# here), or `unknown` if the file or the line is missing.
marker_engine_api="$(sed -n 's/^ENGINE_API: int = \([0-9][0-9]*\)$/\1/p' "$repo_root/src/umwelt/engine_api.py" 2>/dev/null | head -n 1)" || marker_engine_api=""
if [[ -z "$marker_engine_api" ]]; then
    marker_engine_api="unknown"
    echo "install-local-marketplace-umwelt.sh: WARNING could not read ENGINE_API from $repo_root/src/umwelt/engine_api.py; recording umwelt_engine_api=unknown" >&2
fi
{
    printf 'umwelt_sha=%s\n' "$source_commit"
    printf 'umwelt_branch=%s\n' "$marker_branch"
    printf 'umwelt_tag=%s\n' "$marker_tag"
    if [[ "$untagged_override" -eq 1 ]]; then
        printf 'umwelt_untagged_override=1\n'
    fi
    printf 'umwelt_dirty=%s\n' "$marker_dirty"
    printf 'deployed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'deployed_from=%s\n' "$repo_root"
    printf 'umwelt_engine_api=%s\n' "$marker_engine_api"
} > "$marker_tmp" || { rm -f "$marker_tmp"; echo "install-local-marketplace-umwelt.sh: could not write $marker_path" >&2; exit 1; }
chmod 644 "$marker_tmp"
mv -f "$marker_tmp" "$marker_path"
echo "install-local-marketplace-umwelt.sh: deployed-version marker -> $marker_path"

echo "install-local-marketplace-umwelt.sh: assembled marketplace at $marketplace_root"
echo "install-local-marketplace-umwelt.sh: derived version -> $version"
echo "install-local-marketplace-umwelt.sh: source commit -> $source_commit"
echo "install-local-marketplace-umwelt.sh: marketplace.json -> $marketplace_json"
echo "install-local-marketplace-umwelt.sh: plugin tree -> $plugins_dest"

if [[ "$skip_add" -eq 0 ]]; then
    if [[ "$target" == "claude" ]]; then
        if command -v claude >/dev/null 2>&1; then
            echo "install-local-marketplace-umwelt.sh: registering marketplace with claude plugin marketplace add"
            claude plugin marketplace add "$marketplace_root"
        else
            echo "install-local-marketplace-umwelt.sh: 'claude' not found on PATH — register it manually:" >&2
            echo "    claude plugin marketplace add \"$marketplace_root\"" >&2
        fi
    else
        if command -v codex >/dev/null 2>&1; then
            echo "install-local-marketplace-umwelt.sh: registering marketplace with codex plugin marketplace add"
            codex plugin marketplace add "$marketplace_root"
            echo "install-local-marketplace-umwelt.sh: installing umwelt with codex plugin add umwelt@umwelt-local"
            codex plugin add umwelt@umwelt-local

            # Keep existing in-session hook paths usable if Codex removed the
            # active cache directory during the install. Existing real files
            # and directories are deliberately left untouched; only missing
            # paths and compatibility symlinks are created/repointed.
            new_codex_plugin_cache_dir="$codex_cache_parent/$version"
            if [[ -d "$new_codex_plugin_cache_dir" ]]; then
                new_codex_cache_base="$(basename "$new_codex_plugin_cache_dir")"

                # Earlier upgrades may have left stale compatibility links.
                # Refresh only symlinks within this plugin's dev-cache set;
                # never replace a real cache directory or file.
                shopt -s nullglob
                for stale_codex_cache_dir in "$codex_cache_parent"/*-dev.*; do
                    [[ -L "$stale_codex_cache_dir" ]] || continue
                    [[ "$(basename "$stale_codex_cache_dir")" == "$new_codex_cache_base" ]] && continue
                    [[ "$stale_codex_cache_dir" == "$old_codex_plugin_cache_dir" ]] && continue
                    echo "install-local-marketplace-umwelt.sh: refreshing stale Codex cache symlink for in-session compat: $(basename "$stale_codex_cache_dir") -> $new_codex_cache_base"
                    rm -- "$stale_codex_cache_dir"
                    ln -s "$new_codex_plugin_cache_dir" "$stale_codex_cache_dir"
                done
                shopt -u nullglob

                if [[ -n "$old_codex_plugin_cache_dir" && "$old_codex_plugin_cache_dir" != "$new_codex_plugin_cache_dir" ]]; then
                    if [[ -e "$old_codex_plugin_cache_dir" && ! -L "$old_codex_plugin_cache_dir" ]]; then
                        echo "install-local-marketplace-umwelt.sh: preserving existing real Codex cache path: $old_codex_plugin_cache_dir"
                    else
                        if [[ -L "$old_codex_plugin_cache_dir" ]]; then
                            rm -- "$old_codex_plugin_cache_dir"
                        fi
                        echo "install-local-marketplace-umwelt.sh: symlinking prior Codex cache for in-session compat: $(basename "$old_codex_plugin_cache_dir") -> $new_codex_cache_base"
                        ln -s "$new_codex_plugin_cache_dir" "$old_codex_plugin_cache_dir"
                    fi
                fi
            else
                echo "install-local-marketplace-umwelt.sh: WARNING — could not locate installed Codex cache for in-session compatibility: $new_codex_plugin_cache_dir" >&2
            fi
        else
            echo "install-local-marketplace-umwelt.sh: 'codex' not found on PATH — register/install it manually:" >&2
            echo "    codex plugin marketplace add \"$marketplace_root\"" >&2
            echo "    codex plugin add umwelt@umwelt-local" >&2
        fi
    fi
else
    echo "install-local-marketplace-umwelt.sh: --skip-add set — not touching $target plugin state"
fi

# --- Ensure the MCP-server venv exists, same path/interpreter
#     plugin/launch-umwelt-server.sh hard-requires — otherwise install ->
#     enable -> restart lands a dead MCP server until someone hand-builds it.
#     Degrade, don't abort: a no-network environment must still finish the
#     install (marketplace registration already succeeded above) and get
#     clear manual-fallback guidance, not a raw `set -e` traceback with no
#     Next-steps printout at all (the exact degrade-without-warning shape
#     this whole carryover was about — Ariadne's re-review, 2026-07-12).
venv_dir="$HOME/.umwelt/venv"
venv_py="$venv_dir/bin/python3"
venv_warning=""

if [[ "$skip_venv" -eq 0 ]]; then
    if [[ ! -x "$venv_py" ]]; then
        echo "install-local-marketplace-umwelt.sh: creating venv at $venv_dir"
        if ! python3 -m venv "$venv_dir"; then
            echo "install-local-marketplace-umwelt.sh: WARNING — could not create $venv_dir; continuing without it" >&2
            venv_warning=1
        fi
    fi
    if [[ -z "$venv_warning" ]]; then
        if "$venv_py" -c "import fastmcp" >/dev/null 2>&1; then
            echo "install-local-marketplace-umwelt.sh: fastmcp already importable in $venv_dir"
        else
            echo "install-local-marketplace-umwelt.sh: installing fastmcp into $venv_dir"
            if ! "$venv_dir/bin/pip" install -r "$repo_root/requirements-mcp.txt"; then
                echo "install-local-marketplace-umwelt.sh: WARNING — could not install fastmcp into $venv_dir; continuing without it" >&2
                venv_warning=1
            fi
        fi
    fi
else
    echo "install-local-marketplace-umwelt.sh: --skip-venv set — not creating/populating $venv_dir"
fi

if [[ "$target" == "claude" ]]; then
    if [[ "$skip_add" -eq 0 ]]; then
        cat <<EOF

Next steps:
  1. In Claude Code: /plugin install umwelt
  2. Then:           /plugin enable umwelt
  3. Restart Claude Code so the umwelt MCP server and hooks are picked up.
  4. Create ~/.umwelt/config.json with your agent identity, e.g.:
       {"agent": "your-name", "world_url": "http://localhost:5003"}
EOF
    else
        cat <<EOF

Next steps:
  1. Register the marketplace manually:
       claude plugin marketplace add "$marketplace_root"
  2. In Claude Code: /plugin install umwelt
  3. Then:           /plugin enable umwelt
  4. Restart Claude Code so the umwelt MCP server and hooks are picked up.
  5. Create ~/.umwelt/config.json with your agent identity, e.g.:
       {"agent": "your-name", "world_url": "http://localhost:5003"}
EOF
    fi
else
    if [[ "$skip_add" -eq 0 ]]; then
        cat <<EOF

Next steps:
  1. Codex install is already enabled for this plugin in your config.
  2. Start a new Codex session (or restart Codex) so the umwelt MCP server and hooks are picked up.
  3. Create ~/.umwelt/config.json with your agent identity, e.g.:
       {"agent": "your-name", "world_url": "http://localhost:5003"}
EOF
    else
        cat <<EOF

Next steps:
  1. Register the marketplace manually:
       codex plugin marketplace add "$marketplace_root"
  2. Install the plugin manually:
       codex plugin add umwelt@umwelt-local
  3. Start a new Codex session (or restart Codex) so the umwelt MCP server and hooks are picked up.
  4. Create ~/.umwelt/config.json with your agent identity, e.g.:
       {"agent": "your-name", "world_url": "http://localhost:5003"}
EOF
    fi
fi

if [[ "$target" == "claude" ]]; then
    venv_followup_phrase="Before restarting Claude Code"
else
    venv_followup_phrase="Before starting a new Codex session"
fi

if [[ "$skip_venv" -eq 1 ]]; then
    cat <<EOF
  NOTE: --skip-venv was set — $venv_dir was NOT created/updated. $venv_followup_phrase, run:
       python3 -m venv $venv_dir
       $venv_dir/bin/pip install -r $repo_root/requirements-mcp.txt
EOF
elif [[ -n "$venv_warning" ]]; then
    cat <<EOF
  WARNING: $venv_dir could not be fully set up automatically (see the
           WARNING line(s) above — likely no network access right now).
           $venv_followup_phrase, run manually when online:
       python3 -m venv $venv_dir
       $venv_dir/bin/pip install -r $repo_root/requirements-mcp.txt
EOF
fi
