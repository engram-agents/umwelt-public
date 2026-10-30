#!/usr/bin/env bash
# tools/install-world.sh — deploy-isolation installer for umwelt (#98)
#
# ENGRAM-style artifact install: run the world off an INSTALLED ARTIFACT,
# not a live git checkout. Exports a given git ref's runtime tree
# (src/umwelt/, worlds/, requirements.txt, PROTOCOL.md) into a versioned, immutable
# directory under a runtime base, then atomically re-points a `live`
# symlink at it. A running world instance's WorkingDirectory/PYTHONPATH
# point through `live`, never through a checkout being actively pulled —
# so promoting or rolling back a version is a symlink swap, not a
# `git checkout` / `git pull` on a tree the running process also reads from.
#
# Usage:
#   tools/install-world.sh --target {real|alpha} --ref <git-ref> [--activate]
#                          [--no-backup]   (--no-backup: --target alpha ONLY)
#
#   --target {real|alpha}  which runtime to install into (real: :5003,
#                           alpha: :5005 — see docs/DEPLOY.md)
#   --no-backup             skip the pre-promotion DB backup. Accepted ONLY
#                           with --target alpha (the alpha db is disposable);
#                           `--no-backup --target real` is refused at
#                           argument parsing (exit 1, nothing touched).
#   --ref <git-ref>         any git ref resolvable from the invoking repo
#                           (branch, tag, SHA) — resolved to a full commit
#                           SHA before anything else happens
#   --activate              after install, restart BOTH of the target's
#                           systemd --user units -- world unit first, then
#                           the webrender gateway (umwelt-world.service +
#                           umwelt-webrender.service for real;
#                           umwelt-alpha-world.service +
#                           umwelt-alpha-webrender.service for alpha). A
#                           unit systemd doesn't know about is skipped with
#                           a warning, not a hard failure (umwelt #576).
#                           WITHOUT --activate this is install-only: nothing
#                           is restarted, nothing currently live is
#                           disturbed.
#   -h, --help              show this help and exit
#
# Runtime base: $UMWELT_RUNTIME_BASE (default: $HOME/.umwelt). Overriding
# this is what makes the script testable against a throwaway directory —
# load-bearing for tools/test-install-world.sh; do not remove the override.
#
# Layout produced under $UMWELT_RUNTIME_BASE/runtime-<target>/:
#   versions/<sha>/           exported tree for that SHA: src/umwelt/,
#                             worlds/ (the world packages, umwelt#1043),
#                             requirements.txt, PROTOCOL.md
#   versions/<sha>/COMMIT_SHA a one-line stamp of <sha>, read by
#                             server.py's _resolve_commit_sha() fallback
#                             (umwelt #98) — `git archive` never includes
#                             `.git`, so an installed tree has no other way
#                             to report its SHA on /health
#   versions/<sha>/.export-complete
#                             written ONLY after the export succeeds; the
#                             skip-re-export check requires this marker (not
#                             just non-emptiness), so a dir left behind by a
#                             crash mid-`tar -x` is treated as incomplete and
#                             re-exported rather than trusted forever
#   live -> versions/<sha>    the currently-active version; re-pointed
#                             ATOMICALLY (temp symlink + `mv -T`), so `live`
#                             is never missing mid-swap
#   .installed-version        JSON manifest: {"sha", "ref", "installed_at",
#                             "backup"} -- "backup" is the absolute path of the
#                             verified pre-promotion DB copy this run took, or
#                             null when none was taken (--no-backup, alpha with
#                             no db, or a first-ever install with no live)
#
# Layout produced under $UMWELT_RUNTIME_BASE/ (outside runtime-<target>/):
#   backups/pre-promotion-<target>/
#                             this script's OWN backup dir (umwelt-<UTC>.db
#                             copies + .last-ok.json stamp, keep 14). Never
#                             the scheduled-backup dir backups/ itself, so a
#                             routine promotion can never prune the scheduled
#                             daily backups and vice versa.
#
# THE BACKUP FAILURE CONTRACT (umwelt #1106; pinned by B1-B9 in
# tools/test-install-world.sh):
#   Order: the backup runs AFTER the export completes and BEFORE `live` is
#   re-pointed. It is taken by `python3 -m umwelt.world_backup` (SQLite online
#   backup + quick_check on the copy), run from the INVOKING checkout's src/
#   (stdlib-only, so a rollback to an older --ref that predates world_backup
#   still gets a backup) -- never a raw cp/rsync/cat of the db.
#   1. --target real ALWAYS backs up; --no-backup is alpha-only (see above).
#   2. Backup dir: backups/pre-promotion-<target>, --keep 14.
#   3. world_backup non-zero for ANY reason (corrupt/not-sqlite source,
#      unsafe backup dir, deadline, ...) => this script exits non-zero loudly
#      (stderr names "backup" and the world_backup exit code), `live` and the
#      manifest are UNCHANGED, and the --activate step never runs. The
#      already-exported versions/<sha> dir may remain: it is immutable and
#      harmless, and re-running is idempotent.
#   4. Missing db: real + an existing `live` => refused (message names the db
#      path); real with NO existing `live` (first-ever install) => proceeds and
#      says there was no live to back up; alpha => proceeds with a
#      "skipped: no db" line.
#   5. Success => the manifest gains "backup" (path read from the stamp the
#      run just wrote, only after rc 0, and checked to exist) and stdout
#      prints `backup -> <path>` BEFORE the `live -> ...` line.
#
# Idempotent + safe to re-run: re-installing an already-exported SHA skips
# the export and just re-points `live` + rewrites the manifest. Rollback is
# just re-running this script with the prior --ref (its versions/<sha> dir
# is never deleted by this script).
#
# This script NEVER runs `git checkout` / `git pull` / `git reset` on any
# tree (export is via `git archive`, read-only against the invoking repo),
# and NEVER writes to, copies, or moves a `*.db` file itself. It READS the db
# in exactly one place -- the pre-promotion backup -- and only through
# umwelt.world_backup's read-only online backup (the source is opened
# mode=ro; a promotion leaves the source db byte-identical).
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: install-world.sh --target {real|alpha} --ref <git-ref> [--activate] [--no-backup]

  --no-backup            skip the pre-promotion DB backup (alpha ONLY; a real
                         promotion always backs up first)
  --target {real|alpha}  which runtime to install into
  --ref <git-ref>        git ref to resolve + export (branch, tag, SHA)
  --activate              restart BOTH of the target's systemd --user
                          units (world unit first, then the webrender
                          gateway); default: install-only, no restart
  -h, --help              show this help

Env:
  UMWELT_RUNTIME_BASE    runtime base dir (default: $HOME/.umwelt)
EOF
}

target=""
ref=""
activate=0
no_backup=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            if [[ $# -lt 2 ]]; then
                echo "install-world.sh: --target requires a value" >&2
                usage >&2
                exit 1
            fi
            target="$2"
            shift 2
            ;;
        --ref)
            if [[ $# -lt 2 ]]; then
                echo "install-world.sh: --ref requires a value" >&2
                usage >&2
                exit 1
            fi
            ref="$2"
            shift 2
            ;;
        --activate)
            activate=1
            shift
            ;;
        --no-backup)
            no_backup=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "install-world.sh: unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -z "$target" ]]; then
    echo "install-world.sh: --target is required" >&2
    usage >&2
    exit 1
fi
if [[ "$target" != "real" && "$target" != "alpha" ]]; then
    echo "install-world.sh: --target must be 'real' or 'alpha' (got: '$target')" >&2
    exit 1
fi
if [[ "$no_backup" -eq 1 && "$target" != "alpha" ]]; then
    echo "install-world.sh: --no-backup is accepted only with --target alpha; a --target $target promotion always takes a backup first" >&2
    exit 1
fi
if [[ -z "$ref" ]]; then
    echo "install-world.sh: --ref is required" >&2
    usage >&2
    exit 1
fi

# Resolve the invoking repo root (this script lives at <repo>/tools/install-world.sh)
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

if ! git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "install-world.sh: $repo_root is not a git repository" >&2
    exit 1
fi

if ! sha="$(git -C "$repo_root" rev-parse --verify "${ref}^{commit}" 2>&1)"; then
    echo "install-world.sh: cannot resolve ref '$ref' to a commit in $repo_root" >&2
    echo "$sha" >&2
    exit 1
fi

runtime_base="${UMWELT_RUNTIME_BASE:-$HOME/.umwelt}"
runtime_root="$runtime_base/runtime-$target"
versions_dir="$runtime_root/versions"
version_dir="$versions_dir/$sha"
live_link="$runtime_root/live"
manifest_file="$runtime_root/.installed-version"

# The db this target's promotion protects, and the backup dir that is THIS
# script's own (never $runtime_base/backups, the scheduled-backup dir -- see
# the header). real = umwelt.db, alpha = alpha-world.db (docs/DEPLOY.md,
# tools/alpha-world.sh's ALPHA_DB default).
case "$target" in
    real)  db_path="$runtime_base/umwelt.db" ;;
    alpha) db_path="$runtime_base/alpha-world.db" ;;
esac
backup_dir="$runtime_base/backups/pre-promotion-$target"

# The single source of truth for what this script's `git archive` exports --
# used by BOTH the export call below AND the outside-archive-boundary report
# further down, so the report can never drift from what's actually deployed
# (umwelt #799).
runtime_archive_paths=(src/umwelt worlds requirements.txt PROTOCOL.md)

# Capture the PREVIOUSLY-installed sha, if any, before this run's manifest
# write (further below) overwrites it -- this is the "previously-live" half
# of the outside-archive-boundary report (umwelt #799). No jq dependency:
# this script has never had one, and the manifest's shape is fixed and
# already grepped the same way by tools/test-install-world.sh.
prev_sha=""
if [[ -f "$manifest_file" ]]; then
    prev_sha="$(grep -o '"sha": "[^"]*"' "$manifest_file" | head -1 | cut -d'"' -f4)"
fi

mkdir -p "$versions_dir"

export_marker="$version_dir/.export-complete"

if [[ -f "$export_marker" ]]; then
    echo "install-world.sh: versions/$sha already exported — skipping re-export"
else
    # A dir without the completion marker is either absent or a partial
    # leftover from a crash mid-export — clear it before re-exporting so
    # stale/partial files never linger.
    rm -rf "$version_dir"
    mkdir -p "$version_dir"
    # `worlds` is exported when the ref has it. A ref from before world
    # packages (umwelt#1043) has no worlds/ and must still install, so a
    # rollback to an older version keeps working; every other path is
    # required and a missing one still fails the archive loudly.
    export_paths=()
    for archive_path in "${runtime_archive_paths[@]}"; do
        if [[ "$archive_path" == "worlds" ]] \
            && ! git -C "$repo_root" cat-file -e "$sha:$archive_path" 2>/dev/null; then
            continue
        fi
        export_paths+=("$archive_path")
    done
    git -C "$repo_root" archive "$sha" -- "${export_paths[@]}" \
        | tar -x -C "$version_dir"
    printf '%s\n' "$sha" > "$version_dir/COMMIT_SHA"
    touch "$export_marker"
    echo "install-world.sh: exported $sha -> $version_dir"
fi

# Pre-promotion DB backup (umwelt #1106) -- AFTER the export, BEFORE `live`
# moves. Any failure exits here, so `live`, the manifest and the --activate
# step are never reached. (versions/$sha stays behind on that path: it is
# immutable and harmless, and a re-run skips its export.) The failure
# contract is in the header.
backup_path=""   # "" => manifest "backup": null
if [[ "$no_backup" -eq 1 ]]; then
    echo "install-world.sh: backup skipped (--no-backup)"
elif [[ ! -e "$db_path" ]]; then
    if [[ "$target" == "real" ]]; then
        if [[ -L "$live_link" || -e "$live_link" ]]; then
            echo "install-world.sh: refusing to promote: the real db is missing ($db_path) but a live version exists -- no backup is possible, so live is NOT re-pointed" >&2
            exit 1
        fi
        echo "install-world.sh: backup skipped: no live version existed (first-ever install), so there was no db to back up ($db_path)"
    else
        echo "install-world.sh: backup skipped: no db at $db_path (the alpha db is disposable)"
    fi
else
    wb_rc=0
    # world_backup's own stderr is left attached: its reason is the loud part.
    PYTHONPATH="$repo_root/src" python3 -m umwelt.world_backup \
        --db "$db_path" --backup-dir "$backup_dir" --keep 14 >/dev/null || wb_rc=$?
    if [[ "$wb_rc" -ne 0 ]]; then
        echo "install-world.sh: backup FAILED (world_backup exit $wb_rc) for $db_path -- live and the manifest are UNCHANGED and nothing was restarted" >&2
        exit 1
    fi
    # Read the verified copy's path from the stamp this run just wrote (rc 0
    # means world_backup wrote it), and confirm the file is really there.
    backup_path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["path"])' "$backup_dir/.last-ok.json")" || backup_path=""
    if [[ -z "$backup_path" || ! -f "$backup_path" ]]; then
        echo "install-world.sh: backup FAILED (world_backup exit 0, but the stamp $backup_dir/.last-ok.json does not name an existing copy) -- live and the manifest are UNCHANGED and nothing was restarted" >&2
        exit 1
    fi
    echo "install-world.sh: backup -> $backup_path"
fi

# Atomically re-point `live` -> versions/<sha>: build the new symlink under a
# temp name in the SAME directory, then `mv -T` (same-filesystem rename is
# atomic) — `live` is never absent mid-swap.
tmp_link="$runtime_root/.live.tmp.$$"
ln -sfn "$version_dir" "$tmp_link"
mv -T "$tmp_link" "$live_link"

installed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ -n "$backup_path" ]]; then
    json_backup="${backup_path//\\/\\\\}"
    json_backup="\"${json_backup//\"/\\\"}\""
else
    json_backup="null"
fi
cat > "$manifest_file" <<EOF
{"sha": "$sha", "ref": "$ref", "installed_at": "$installed_at", "backup": $json_backup}
EOF

echo "install-world.sh: resolved ref '$ref' -> $sha"
echo "install-world.sh: live -> $target ($version_dir)"
echo "install-world.sh: manifest -> $manifest_file"

# Outside-archive-boundary report (umwelt #799): diagnostic only, never a
# gate -- it doesn't touch the exit code and runs whether or not --activate
# was passed. Prints ONLY when there's something to say: a first-ever
# install (no prev_sha), a no-op re-run (prev_sha == sha), or a diff that
# lands entirely inside runtime_archive_paths all print NOTHING here --
# silence is itself the informative signal for the common, fully-covered
# case. A prev_sha that no longer resolves (a since-pruned/rewritten
# manifest reference) degrades to a one-line warning, never a hard failure,
# since this is diagnostic output, not a deploy gate.
if [[ -n "$prev_sha" && "$prev_sha" != "$sha" ]]; then
    if git -C "$repo_root" rev-parse --verify "${prev_sha}^{commit}" >/dev/null 2>&1; then
        changed_files="$(git -C "$repo_root" diff --name-only "$prev_sha".."$sha" -- 2>/dev/null || true)"
        outside_archive=()
        while IFS= read -r changed_file; do
            [[ -n "$changed_file" ]] || continue
            in_archive=0
            for archive_path in "${runtime_archive_paths[@]}"; do
                if [[ "$changed_file" == "$archive_path" || "$changed_file" == "$archive_path"/* ]]; then
                    in_archive=1
                    break
                fi
            done
            if [[ "$in_archive" -eq 0 ]]; then
                outside_archive+=("$changed_file")
            fi
        done <<< "$changed_files"

        if [[ "${#outside_archive[@]}" -gt 0 ]]; then
            echo "install-world.sh: ${#outside_archive[@]} changed file(s) in ${prev_sha}..${sha} fall OUTSIDE the runtime archive"
            echo "install-world.sh:                   and are NOT deployed by this step:"
            for changed_file in "${outside_archive[@]}"; do
                echo "install-world.sh:                     $changed_file"
            done
            echo "install-world.sh:                   These reach a seat only through that seat's own plugin rebuild"
            echo "install-world.sh:                   (plugin/*) or its own checkout (tools/*)."
        fi
    else
        echo "install-world.sh: WARNING -- previously-installed sha $prev_sha is not resolvable in $repo_root; skipping outside-archive-boundary report" >&2
    fi
fi

if [[ "$activate" -eq 1 ]]; then
    case "$target" in
        real)  units=("umwelt-world.service" "umwelt-webrender.service") ;;
        alpha) units=("umwelt-alpha-world.service" "umwelt-alpha-webrender.service") ;;
    esac

    # Restart the world unit first, then the gateway: the gateway talks to
    # the world, so bringing the backend up first avoids a window where the
    # gateway is new and its backend is old. Restart is UNCONDITIONAL for
    # every unit that exists -- deliberate (umwelt #576): a check that fires
    # only when someone correctly decides the gateway needs restarting is
    # precisely the mechanism that failed before. A missing unit (not every
    # install has a webrender unit) degrades with a warning, never a hard
    # failure; a unit that exists but fails to restart IS a hard failure.
    # The ordering is ALSO a failure boundary: if the world unit's restart
    # fails, the gateway is deliberately never attempted -- bringing a
    # gateway up against a backend that just failed to start is worse than
    # not bringing it up.
    #
    # Probe the systemd --user bus ONCE, before touching any unit. `cat`
    # returning rc=1 for a genuinely-absent unit is indistinguishable from
    # rc=1 for an UNREACHABLE bus (e.g. XDG_RUNTIME_DIR unset in a non-login
    # shell) -- both look identical to the per-unit existence check below.
    # "Is systemd reachable at all" is a SEPARATE question, asked once here,
    # not a cleverer reading of the same per-unit answer (umwelt #576 round
    # 2). Without this probe, an unreachable bus would warn "not found" for
    # every unit and exit 0 -- a promotion that restarted nothing reporting
    # success, the exact failure shape (a truthful-looking signal about the
    # wrong thing) this script exists to fix.
    #
    # Why a real bus probe rather than parsing an exit code for "not found":
    # `man systemctl`'s own EXIT STATUS section says, verbatim, "The mapping
    # of LSB service states to systemd unit states is imperfect, so it is
    # better to not rely on those return values but to look for specific
    # unit states and substates instead." Its LSB table documents 4 for
    # no-such-unit; `restart` was separately measured returning 5 for the
    # same condition on this host (umwelt #576 round 2) -- not necessarily a
    # contradiction (that table is written for query verbs and may not
    # govern `restart`'s not-found path), but two plausible numbers for one
    # claimed condition is exactly why this probe asks systemd a real
    # question (can I reach you at all) instead of inferring reachability
    # from either exit code (umwelt #576 round 6).
    #
    # This closes the window ONCE, not per-unit, and that leaves a real,
    # accepted TOCTOU gap (umwelt #576 round 3): if the bus goes from
    # reachable to unreachable between this probe and a later per-unit
    # `cat`, that `cat`'s rc=1 is read as "unit absent" again -- the exact
    # ambiguity this probe exists to remove, back inside a narrow window.
    # Re-probing before every unit would defeat the ask-once design for a
    # window this narrow and this rare; the probe still removes the common
    # case (a bus that was never reachable for the whole invocation), which
    # is the one that actually bit us.
    if ! systemctl --user show --property=Version >/dev/null 2>&1; then
        echo "install-world.sh: cannot reach the systemd --user instance (bus unreachable) -- refusing to guess which units exist or are absent" >&2
        exit 1
    fi

    # Report what a unit is actually serving, read from the running process
    # rather than from configuration -- the whole defect this fixes is that
    # configuration and reality disagreed (umwelt #576).
    report_unit_version() {
        local unit="$1" pid version cwd_real
        pid="$(systemctl --user show "$unit" -p MainPID --value 2>/dev/null || echo "")"
        version="unknown"
        if [[ -n "$pid" && "$pid" != "0" && -e "/proc/$pid/cwd" ]]; then
            cwd_real="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || echo "")"
            if [[ -n "$cwd_real" ]]; then
                case "$cwd_real" in
                    "$runtime_root"/*) version="${cwd_real#"$runtime_root"/}" ;;
                    *)                 version="$cwd_real" ;;
                esac
            fi
        fi
        printf 'install-world.sh: %-32s -> %s\n' "$unit" "$version"
    }

    for unit in "${units[@]}"; do
        if ! systemctl --user cat "$unit" >/dev/null 2>&1; then
            echo "install-world.sh: WARNING -- unit $unit not found on this host, skipping restart" >&2
            continue
        fi
        echo "install-world.sh: --activate set — restarting $unit"
        if ! systemctl --user restart "$unit"; then
            echo "install-world.sh: failed to restart $unit" >&2
            exit 1
        fi
        # Report IMMEDIATELY after this unit's own successful restart, not
        # in a second loop at the end (umwelt #576 round 3): if a LATER
        # unit's restart then fails, the operator must not lose the
        # diagnostic for units that already restarted fine -- that is
        # exactly the moment the report is most useful, and a
        # second-loop-at-the-end design would have skipped it entirely on
        # that path.
        report_unit_version "$unit"
    done
fi
