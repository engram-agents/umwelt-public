#!/usr/bin/env bash
# tools/test-install-world.sh — exercises tools/install-world.sh (#98) against
# a throwaway runtime base. Never touches the real ~/.umwelt or any live
# systemd unit: UMWELT_RUNTIME_BASE is overridden to a mktemp dir for the
# whole run, and a stub `systemctl` shadows the real binary on PATH so that
# even a bug that calls systemctl unconditionally cannot reach the real
# service manager.
set -euo pipefail

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() {
    echo "ok - $*"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
installer="$script_dir/install-world.sh"

[[ -x "$installer" ]] || fail "tools/install-world.sh not found or not executable"

tmp_base="$(mktemp -d)"
stub_bin="$(mktemp -d)"
systemctl_marker="$stub_bin/systemctl-calls.log"

# extra_bg_pid: set by test 11 (umwelt #576 round 3/4) to a
# SPACE-SEPARATED list of PIDs of short-lived background processes it
# starts to give the version-report feature REAL /proc/<pid>/cwd targets
# to read -- round 4 needs TWO simultaneous processes with distinct cwds
# so a cross-unit PID mix-up becomes observable. Killed here by their
# captured PIDs -- never `pkill -f`, which on a shared host can match
# other agents' processes or this script's own shell.
extra_bg_pid=""

# scratch_repo / scratch_runtime_base: set by tests 13-16 (umwelt #799,
# outside-archive-boundary report) to a throwaway git repo + runtime base
# built with CONTROLLED commit content -- declared here (empty) so cleanup()
# can unconditionally remove them even if a test fails before reaching its
# own teardown.
scratch_repo=""
scratch_runtime_base=""

cleanup() {
    if [[ -n "$extra_bg_pid" ]]; then
        for _bg_pid in $extra_bg_pid; do
            kill "$_bg_pid" 2>/dev/null || true
            wait "$_bg_pid" 2>/dev/null || true
        done
    fi
    rm -rf "$tmp_base" "$stub_bin" "$scratch_repo" "$scratch_runtime_base"
}
trap cleanup EXIT

export UMWELT_RUNTIME_BASE="$tmp_base"

# Stub systemctl: shadow the real binary via PATH so no test run can ever
# reach the real service manager, regardless of --activate usage. Every
# invocation is logged (unchanged guarantee). It also answers:
#   - $UMWELT_TEST_BUS_UNREACHABLE (any non-empty value): EVERY invocation
#     fails exactly like a real unreachable systemd --user bus does (rc=1,
#     "Failed to connect to bus: ..." on stderr) -- simulates a non-login
#     shell with XDG_RUNTIME_DIR unset, checked FIRST because on a real
#     unreachable bus every systemctl subcommand fails identically, not
#     just the ones install-world.sh happens to call (umwelt #576 round 2).
#   - `--user show --property=Version` (the once-before-the-loop bus probe):
#     answers success once the unreachable-bus branch above doesn't apply.
#   - `--user cat <unit>` (per-unit existence check): a unit is "absent"
#     only if its name is listed (space-separated) in
#     $UMWELT_TEST_MISSING_UNITS, read at invocation time so different test
#     cases can simulate different absent units without touching the stub
#     file itself.
#   - `--user show <unit> -p MainPID --value` (post-restart version report):
#     answers, in order: (1) $UMWELT_TEST_MAIN_PID_MAP if the unit is
#     listed in it -- a space-separated list of "unit=pid" pairs, so the
#     world unit and the gateway unit can be given DIFFERENT real PIDs
#     with different cwds (umwelt #576 round 4 -- a single shared PID for
#     both units made a cross-unit PID mix-up unobservable: the reviewer
#     hardcoded the lookup to always report units[0]'s PID and the suite
#     didn't notice, because both units' expected output was identical);
#     (2) $UMWELT_TEST_MAIN_PID if set (a single REAL pid, kept for
#     backward compatibility with tests that don't need per-unit
#     distinction); (3) otherwise empty. A real pid means /proc/<pid>/cwd
#     resolves for real; empty means install-world.sh must treat it as
#     "unknown", not a failure.
#   - `--user restart <unit>` (the actual restart): fails (rc=1, a
#     systemd-shaped stderr message) if the unit is listed
#     (space-separated) in $UMWELT_TEST_RESTART_FAIL, read at invocation
#     time -- umwelt #576 round 4 item 2, so a test can pin "does the
#     version report for an EARLIER unit still print before the script
#     exits non-zero on a LATER unit's restart failure" without needing a
#     real failing unit.
cat > "$stub_bin/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "$systemctl_marker"
if [[ -n "\${UMWELT_TEST_BUS_UNREACHABLE:-}" ]]; then
    echo "Failed to connect to bus: No medium found" >&2
    exit 1
fi
if [[ "\$1" == "--user" && "\$2" == "cat" ]]; then
    unit="\$3"
    for missing in \$UMWELT_TEST_MISSING_UNITS; do
        if [[ "\$unit" == "\$missing" ]]; then
            exit 1
        fi
    done
    exit 0
fi
if [[ "\$1" == "--user" && "\$2" == "show" && "\$3" == "--property=Version" ]]; then
    echo "Version=250"
    exit 0
fi
if [[ "\$1" == "--user" && "\$2" == "show" ]]; then
    unit="\$3"
    for pair in \$UMWELT_TEST_MAIN_PID_MAP; do
        key="\${pair%%=*}"
        val="\${pair#*=}"
        if [[ "\$unit" == "\$key" ]]; then
            echo "\$val"
            exit 0
        fi
    done
    echo "\${UMWELT_TEST_MAIN_PID:-}"
    exit 0
fi
if [[ "\$1" == "--user" && "\$2" == "restart" ]]; then
    unit="\$3"
    for failing in \$UMWELT_TEST_RESTART_FAIL; do
        if [[ "\$unit" == "\$failing" ]]; then
            echo "Job for \$unit failed. See \"systemctl --user status \$unit\" for details." >&2
            exit 1
        fi
    done
    exit 0
fi
exit 0
EOF
chmod +x "$stub_bin/systemctl"
export PATH="$stub_bin:$PATH"

head_sha="$(git -C "$repo_root" rev-parse HEAD)"
prev_sha="$(git -C "$repo_root" rev-parse HEAD~1)"

[[ "$head_sha" != "$prev_sha" ]] || fail "HEAD and HEAD~1 resolved to the same SHA — need two distinct refs to test re-pointing"

runtime_root="$tmp_base/runtime-alpha"

# --- 1. Fresh install of HEAD to --target alpha (no --activate) ---
out1="$(bash "$installer" --target alpha --ref HEAD)"
echo "$out1"

version_dir_head="$runtime_root/versions/$head_sha"
[[ -d "$version_dir_head/src/umwelt" ]] || fail "versions/$head_sha/src/umwelt missing after install"
[[ -n "$(ls -A "$version_dir_head/src/umwelt" 2>/dev/null)" ]] || fail "versions/$head_sha/src/umwelt is empty"
pass "versions/<sha>/src/umwelt/ exists and is non-empty"

[[ -L "$runtime_root/live" ]] || fail "live is not a symlink"
live_target="$(readlink -f "$runtime_root/live")"
[[ "$live_target" == "$(readlink -f "$version_dir_head")" ]] || fail "live does not point at versions/$head_sha (points at $live_target)"
pass "live symlink points at versions/$head_sha"

[[ -f "$runtime_root/.installed-version" ]] || fail ".installed-version missing"
grep -q "\"sha\": \"$head_sha\"" "$runtime_root/.installed-version" || fail ".installed-version does not record sha $head_sha"
pass ".installed-version records the correct sha"

[[ -f "$version_dir_head/COMMIT_SHA" ]] || fail "COMMIT_SHA marker missing for versions/$head_sha"
[[ "$(cat "$version_dir_head/COMMIT_SHA")" == "$head_sha" ]] || fail "COMMIT_SHA marker content wrong for versions/$head_sha"
pass "COMMIT_SHA marker stamped with the correct sha"

[[ -f "$version_dir_head/.export-complete" ]] || fail ".export-complete marker missing for versions/$head_sha"
pass ".export-complete marker written after a successful export"

# --- 2. Re-run the SAME install -> idempotent ---
out2="$(bash "$installer" --target alpha --ref HEAD)"
echo "$out2"
live_target2="$(readlink -f "$runtime_root/live")"
[[ "$live_target2" == "$(readlink -f "$version_dir_head")" ]] || fail "re-run changed live's target unexpectedly"
pass "re-running the same install is idempotent (no error, live unchanged)"

# --- 3. Install a SECOND ref (HEAD~1) -> live re-points, prior version stays ---
out3="$(bash "$installer" --target alpha --ref HEAD~1)"
echo "$out3"

version_dir_prev="$runtime_root/versions/$prev_sha"
[[ -d "$version_dir_prev/src/umwelt" ]] || fail "versions/$prev_sha/src/umwelt missing after second install"
pass "second install exported versions/$prev_sha"

live_target3="$(readlink -f "$runtime_root/live")"
[[ "$live_target3" == "$(readlink -f "$version_dir_prev")" ]] || fail "live did not re-point to versions/$prev_sha"
pass "live re-points to the newly-installed version"

[[ -d "$version_dir_head" ]] || fail "prior version dir (versions/$head_sha) was deleted — rollback would be impossible"
pass "prior version dir survives -> rollback-able via re-pointing live"

grep -q "\"sha\": \"$prev_sha\"" "$runtime_root/.installed-version" || fail ".installed-version not updated to sha $prev_sha"
pass ".installed-version updated to the new sha"

[[ -f "$version_dir_prev/COMMIT_SHA" ]] || fail "COMMIT_SHA marker missing for versions/$prev_sha"
[[ "$(cat "$version_dir_prev/COMMIT_SHA")" == "$prev_sha" ]] || fail "COMMIT_SHA marker content wrong for versions/$prev_sha"
pass "COMMIT_SHA marker stamped correctly on the second install too"

# --- 4. systemctl must NEVER be invoked without --activate ---
[[ ! -f "$systemctl_marker" ]] || fail "systemctl was invoked without --activate: $(cat "$systemctl_marker")"
pass "systemctl was never invoked across any install without --activate"

# --- 5. Partial-export guard: a dir without .export-complete is never
#        trusted, even if non-empty -> gets cleared + fully re-exported ---
third_sha="$(git -C "$repo_root" rev-parse HEAD~2)"
version_dir_third="$runtime_root/versions/$third_sha"
mkdir -p "$version_dir_third"
echo "leftover junk from a crashed export" > "$version_dir_third/PARTIAL_JUNK"
[[ ! -f "$version_dir_third/.export-complete" ]] || fail "test setup bug: partial dir already has a completion marker"

out5="$(bash "$installer" --target alpha --ref HEAD~2)"
echo "$out5"

[[ -f "$version_dir_third/.export-complete" ]] || fail "a dir without .export-complete was not re-exported to completion"
[[ -f "$version_dir_third/COMMIT_SHA" ]] || fail "COMMIT_SHA missing after re-export of a previously-partial dir"
[[ "$(cat "$version_dir_third/COMMIT_SHA")" == "$third_sha" ]] || fail "COMMIT_SHA wrong after re-export of a previously-partial dir"
[[ ! -f "$version_dir_third/PARTIAL_JUNK" ]] || fail "stale partial-export junk survived re-export (dir was not cleared first)"
pass "a directory without .export-complete is never trusted -- cleared and safely re-exported"

# --- 6. --activate --target alpha restarts BOTH alpha units, world first
#        (umwelt #576) ---
rm -f "$systemctl_marker"
out6="$(bash "$installer" --target alpha --ref HEAD --activate 2>&1)"
echo "$out6"

[[ -f "$systemctl_marker" ]] || fail "--activate --target alpha did not invoke systemctl at all"
grep -q '^--user restart umwelt-alpha-world.service$' "$systemctl_marker" \
    || fail "--activate --target alpha did not restart umwelt-alpha-world.service"
grep -q '^--user restart umwelt-alpha-webrender.service$' "$systemctl_marker" \
    || fail "--activate --target alpha did not restart umwelt-alpha-webrender.service"
pass "--activate --target alpha restarts both umwelt-alpha-world.service and umwelt-alpha-webrender.service"

world_line_alpha="$(grep -n '^--user restart umwelt-alpha-world.service$' "$systemctl_marker" | head -1 | cut -d: -f1)"
gateway_line_alpha="$(grep -n '^--user restart umwelt-alpha-webrender.service$' "$systemctl_marker" | head -1 | cut -d: -f1)"
[[ "$world_line_alpha" -lt "$gateway_line_alpha" ]] \
    || fail "world unit restart (log line $world_line_alpha) did not precede gateway unit restart (log line $gateway_line_alpha) for alpha"
pass "the world unit is restarted before the gateway unit (alpha)"

# --- 7. --activate --target real restarts BOTH real units, world first ---
rm -f "$systemctl_marker"
out7="$(bash "$installer" --target real --ref HEAD --activate 2>&1)"
echo "$out7"

grep -q '^--user restart umwelt-world.service$' "$systemctl_marker" \
    || fail "--activate --target real did not restart umwelt-world.service"
grep -q '^--user restart umwelt-webrender.service$' "$systemctl_marker" \
    || fail "--activate --target real did not restart umwelt-webrender.service"
pass "--activate --target real restarts both umwelt-world.service and umwelt-webrender.service"

world_line_real="$(grep -n '^--user restart umwelt-world.service$' "$systemctl_marker" | head -1 | cut -d: -f1)"
gateway_line_real="$(grep -n '^--user restart umwelt-webrender.service$' "$systemctl_marker" | head -1 | cut -d: -f1)"
[[ "$world_line_real" -lt "$gateway_line_real" ]] \
    || fail "world unit restart (log line $world_line_real) did not precede gateway unit restart (log line $gateway_line_real) for real"
pass "the world unit is restarted before the gateway unit (real)"

# --- 8. THE REGRESSION TEST (umwelt #576): the gateway unit is ABSENT --
#        install-world.sh must still exit 0, still restart the world unit,
#        and print a clearly-marked warning naming the skipped unit. A test
#        that only checked the exit code would pass against the original
#        bug (a silent skip) -- assert on the warning text itself. ---
rm -f "$systemctl_marker"
set +e
out8="$(UMWELT_TEST_MISSING_UNITS="umwelt-alpha-webrender.service" bash "$installer" --target alpha --ref HEAD --activate 2>&1)"
exit8=$?
set -e
echo "$out8"

[[ "$exit8" -eq 0 ]] || fail "--activate with an absent gateway unit did not exit 0 (exit $exit8): $out8"
pass "a missing gateway unit does not turn --activate into a hard failure (exit 0)"

grep -q '^--user restart umwelt-alpha-world.service$' "$systemctl_marker" \
    || fail "world unit was not restarted even though only the gateway unit was reported absent"
pass "the world unit still restarts when the gateway unit is absent"

if grep -q '^--user restart umwelt-alpha-webrender.service$' "$systemctl_marker"; then
    fail "the absent gateway unit was restarted anyway -- the existence check did not skip it"
fi
pass "the absent gateway unit's restart is skipped, not attempted"

echo "$out8" | grep -qi 'umwelt-alpha-webrender.service' \
    || fail "no message naming the skipped unit umwelt-alpha-webrender.service was printed -- a silent skip"
echo "$out8" | grep -qi 'warn' \
    || fail "the skipped-unit message was not clearly marked as a warning"
pass "a clearly-marked warning naming the skipped unit is printed -- not a silent skip"

# --- 9. THE ROUND-2 BLOCKER (umwelt #576): the systemd --user bus itself is
#        UNREACHABLE. `cat` returning rc=1 for a genuinely-absent unit is
#        indistinguishable from rc=1 for an unreachable bus -- without a
#        separate up-front probe, this path would warn "not found on this
#        host" for every unit and exit 0: a promotion that restarted
#        nothing reporting success. Assert exit non-zero, NO restart line
#        logged, and -- the actual point -- the output does NOT contain the
#        "not found on this host" wording, since a version that still
#        misattributes the cause would pass a check that only looked at the
#        exit code. ---
rm -f "$systemctl_marker"
set +e
out9="$(UMWELT_TEST_BUS_UNREACHABLE=1 bash "$installer" --target alpha --ref HEAD --activate 2>&1)"
exit9=$?
set -e
echo "$out9"

[[ "$exit9" -ne 0 ]] || fail "--activate with an unreachable systemd --user bus exited 0 -- a promotion that restarted nothing must not report success"
pass "an unreachable systemd --user bus is a hard failure (non-zero exit)"

if [[ -f "$systemctl_marker" ]] && grep -q '^--user restart ' "$systemctl_marker"; then
    fail "a restart was attempted despite the bus being unreachable"
fi
pass "no restart is attempted when the bus is unreachable"

if echo "$out9" | grep -qi 'not found on this host'; then
    fail "an unreachable bus was misreported using the unit-absent wording -- the two failure causes must not share a message"
fi
pass "an unreachable bus is reported as its own cause, not misattributed as a missing unit"

# --- 10. Both units of a tier ABSENT (bus reachable): exit 0, a warning for
#         EACH unit, and the main restart loop -- which restarts AND
#         reports a unit's version in the same iteration (umwelt #576
#         round 3; there is no separate restarted_units array or trailing
#         report loop to worry about) -- simply completes having done
#         nothing for either unit, under `set -euo pipefail`. ---
rm -f "$systemctl_marker"
out10="$(UMWELT_TEST_MISSING_UNITS="umwelt-alpha-world.service umwelt-alpha-webrender.service" bash "$installer" --target alpha --ref HEAD --activate 2>&1)"
echo "$out10"

if [[ -f "$systemctl_marker" ]] && grep -q '^--user restart ' "$systemctl_marker"; then
    fail "a restart was logged even though both units were reported absent"
fi
echo "$out10" | grep -qi 'umwelt-alpha-world.service' \
    || fail "no message naming the absent world unit was printed when both units are absent"
echo "$out10" | grep -qi 'umwelt-alpha-webrender.service' \
    || fail "no message naming the absent gateway unit was printed when both units are absent"
pass "both units of a tier can be reported absent with a warning each, no restart attempted, the restart-and-report loop does not error with nothing to do"

# --- 11. THE FEATURE THIS PR IS NAMED FOR (umwelt #576 round 3/4): the
#         /proc/<MainPID>/cwd version report. Previously untested -- the
#         stub always answered MainPID empty, so every run took the
#         "unknown" fallback and no assertion ever looked at the printed
#         line's CONTENT, so a broken prefix-strip could ship silently
#         (proven true live: the round-2 reviewer mutated that line and
#         got ALL TESTS PASSED unchanged). Round 3 fixed that with ONE
#         real background process -- but round 4's reviewer found that a
#         SINGLE shared PID for both units makes a CROSS-UNIT mix-up
#         unobservable: it hardcoded report_unit_version's lookup to
#         always report units[0]'s (the world unit's) PID -- i.e. "report
#         the world unit's version for the gateway", label left correct
#         -- and the suite still passed, because both units' expected
#         output was byte-for-byte identical. Fixed by giving the two
#         units genuinely DIFFERENT real PIDs with DIFFERENT cwds (two
#         already-installed version dirs, versions/<head_sha> and
#         versions/<prev_sha>), so a cross-unit swap now changes the
#         printed content and becomes observable. ---
( cd "$version_dir_head" && exec sleep 30 ) &
bg_pid_world=$!
# Register for cleanup IMMEDIATELY, not after both processes exist (umwelt
# #576 round 5): a process must be in extra_bg_pid from the moment it
# exists, not from the moment its sibling does -- a single assignment
# after both spawns leaves a window where an abort between the two spawns
# (e.g. a future between-spawns assertion) would leak the first process
# unregistered. Confirmed reachable by the reviewer in isolation; not
# reachable in this file as written today, which is exactly why it is a
# footgun for the next edit rather than a live bug -- don't "simplify"
# this back into one assignment.
extra_bg_pid="$bg_pid_world"
( cd "$version_dir_prev" && exec sleep 30 ) &
bg_pid_gateway=$!
extra_bg_pid="$extra_bg_pid $bg_pid_gateway"

[[ -d "/proc/$bg_pid_world" ]] || fail "test setup: background process $bg_pid_world (world) did not start"
[[ "$(readlink -f "/proc/$bg_pid_world/cwd")" == "$(readlink -f "$version_dir_head")" ]] \
    || fail "test setup: background process $bg_pid_world's /proc cwd is not versions/$head_sha"
[[ -d "/proc/$bg_pid_gateway" ]] || fail "test setup: background process $bg_pid_gateway (gateway) did not start"
[[ "$(readlink -f "/proc/$bg_pid_gateway/cwd")" == "$(readlink -f "$version_dir_prev")" ]] \
    || fail "test setup: background process $bg_pid_gateway's /proc cwd is not versions/$prev_sha"
[[ "$head_sha" != "$prev_sha" ]] || fail "test setup bug: head_sha and prev_sha are identical -- the two expected outputs would not diverge"

rm -f "$systemctl_marker"
out11="$(UMWELT_TEST_MAIN_PID_MAP="umwelt-alpha-world.service=$bg_pid_world umwelt-alpha-webrender.service=$bg_pid_gateway" bash "$installer" --target alpha --ref HEAD --activate 2>&1)"
echo "$out11"

kill "$bg_pid_world" "$bg_pid_gateway" 2>/dev/null || true
wait "$bg_pid_world" "$bg_pid_gateway" 2>/dev/null || true
extra_bg_pid=""

echo "$out11" | grep -E "umwelt-alpha-world\.service[[:space:]]+->[[:space:]]+versions/$head_sha$" >/dev/null \
    || fail "the world unit's version-report line did not print versions/$head_sha for its own real process/cwd -- got: $out11"
echo "$out11" | grep -E "umwelt-alpha-webrender\.service[[:space:]]+->[[:space:]]+versions/$prev_sha$" >/dev/null \
    || fail "the gateway unit's version-report line did not print versions/$prev_sha for its own real process/cwd (got the world unit's PID/cwd instead?) -- got: $out11"
pass "the /proc/<MainPID>/cwd version report prints the REAL, DISTINCT, correctly-stripped versions/<sha> per unit -- a cross-unit PID mix-up would be caught"

# --- 12. REPORT-BEFORE-EXIT HAS COVERAGE (umwelt #576 round 4 item 2,
#         hardened round 6 item 1): the round-3 fix moved the version
#         report inside the restart loop so an EARLIER unit's report
#         survives a LATER unit's restart failure -- verified by hand by
#         the round-3 reviewer, but with zero automated coverage. Force
#         the gateway's restart to fail; the world unit restarts and
#         reports FIRST, so its report line must still be present in the
#         output even though the script then exits non-zero on the
#         gateway. Without the PRESENCE assertion, a future refactor that
#         moved reporting back into a trailing loop would regress
#         silently. Round 6: the colleague found that presence alone is
#         vacuous the same way rounds 2 and 4's gaps were -- it never
#         checked the FAILING unit's report line is ABSENT. He moved
#         `report_unit_version "$unit"` to run unconditionally BEFORE the
#         restart check (a plausible refactor, not a contrived edit), and
#         the suite still passed, with the gateway's own "-> unknown"
#         line printing despite its restart failing one line later. Not
#         reachable today (report_unit_version sits after an
#         `if ! restart; then exit 1; fi`), but a reorder ships silently
#         without this. The absence assertion below matches on the
#         REPORT line's own shape (`<unit>` then whitespace then `->`),
#         not on any mention of the unit's name -- the restart-attempt
#         line and the systemd failure message both name the gateway
#         unit too, and neither of those has a trailing `->`. ---
rm -f "$systemctl_marker"
set +e
out12="$(UMWELT_TEST_RESTART_FAIL="umwelt-alpha-webrender.service" bash "$installer" --target alpha --ref HEAD --activate 2>&1)"
exit12=$?
set -e
echo "$out12"

[[ "$exit12" -ne 0 ]] || fail "--activate with a failing gateway restart exited 0 -- a unit that exists but fails to restart must be a hard failure"
pass "a unit that exists but fails to restart is still a hard failure (non-zero exit) even after an earlier unit restarted fine"

echo "$out12" | grep -E "umwelt-alpha-world\.service[[:space:]]+->" >/dev/null \
    || fail "the world unit's version-report line was not printed before the script exited on the gateway's restart failure -- the round-3 report-before-exit fix has regressed"
pass "an earlier unit's version-report line prints BEFORE the script exits on a LATER unit's restart failure"

if echo "$out12" | grep -E "umwelt-alpha-webrender\.service[[:space:]]+->" >/dev/null; then
    fail "the gateway unit's version-report line WAS printed even though its own restart failed -- report_unit_version must run only for a unit that actually restarted, never unconditionally before the restart check"
fi
pass "the failing unit's version-report line is NOT printed -- report_unit_version does not run for a unit whose restart failed"

# --- 13-16. THE OUTSIDE-ARCHIVE-BOUNDARY REPORT (umwelt #799) -- these need
#         CONTROLLED diff content (entirely-inside-archive vs.
#         some-file-outside-archive), which the real invoking repo's actual
#         history (used by tests 1-12 above, via head_sha/prev_sha) cannot
#         guarantee. Build a tiny throwaway git repo with the installer
#         script copied into it -- install-world.sh resolves its own
#         repo_root from BASH_SOURCE's own directory, so invoking the COPY
#         under tools/ inside this scratch repo makes the script operate
#         against the scratch repo's history, not the real umwelt checkout --
#         and construct three commits with known, deliberate diff shapes.
scratch_repo="$(mktemp -d)"
scratch_runtime_base="$(mktemp -d)"

git -C "$scratch_repo" init -q
git -C "$scratch_repo" config user.email "test@example.com"
git -C "$scratch_repo" config user.name "Test Harness"

mkdir -p "$scratch_repo/src/umwelt" "$scratch_repo/tools" "$scratch_repo/plugin"
cp "$installer" "$scratch_repo/tools/install-world.sh"
chmod +x "$scratch_repo/tools/install-world.sh"
echo "print('hello')" > "$scratch_repo/src/umwelt/__init__.py"
echo "flask" > "$scratch_repo/requirements.txt"
echo "# protocol" > "$scratch_repo/PROTOCOL.md"
echo "# plugin stub" > "$scratch_repo/plugin/stub.py"
git -C "$scratch_repo" add -A
git -C "$scratch_repo" commit -q -m "commit A: baseline"
commit_a="$(git -C "$scratch_repo" rev-parse HEAD)"

# Commit B: change ONLY inside the archive paths (requirements.txt).
echo "flask==3.0" > "$scratch_repo/requirements.txt"
git -C "$scratch_repo" add -A
git -C "$scratch_repo" commit -q -m "commit B: inside-archive-only change"
commit_b="$(git -C "$scratch_repo" rev-parse HEAD)"

# Commit C: change a file OUTSIDE the archive paths (plugin/stub.py), plus an
# inside-archive change too, so the report's FILTERING (not just its
# presence) is exercised -- it must name the outside file and not the
# inside one.
echo "# plugin stub v2" > "$scratch_repo/plugin/stub.py"
echo "print('hello v2')" > "$scratch_repo/src/umwelt/__init__.py"
git -C "$scratch_repo" add -A
git -C "$scratch_repo" commit -q -m "commit C: outside-archive + inside-archive change"
commit_c="$(git -C "$scratch_repo" rev-parse HEAD)"

scratch_installer="$scratch_repo/tools/install-world.sh"

# --- 13. First-ever install (no prior manifest) -> no report, no error ---
out13="$(UMWELT_RUNTIME_BASE="$scratch_runtime_base" bash "$scratch_installer" --target alpha --ref "$commit_a" 2>&1)"
echo "$out13"
if echo "$out13" | grep -qi 'fall OUTSIDE the runtime archive'; then
    fail "a first-ever install (no prior manifest) printed an outside-archive-boundary report -- there is no prev_sha to diff against"
fi
pass "a first-ever install prints no outside-archive-boundary report (no prior manifest to diff against)"

# --- 14. Installing a SECOND sha whose diff is ENTIRELY inside the archive
#         paths -> no report (silence is the informative signal for a fully-
#         covered deploy) ---
out14="$(UMWELT_RUNTIME_BASE="$scratch_runtime_base" bash "$scratch_installer" --target alpha --ref "$commit_b" 2>&1)"
echo "$out14"
if echo "$out14" | grep -qi 'fall OUTSIDE the runtime archive'; then
    fail "a diff entirely inside the archive paths (requirements.txt only) printed an outside-archive-boundary report"
fi
pass "a diff entirely inside the archive paths prints no outside-archive-boundary report"

# --- 15. Installing a THIRD sha whose diff includes a file OUTSIDE the
#         archive paths -> the report prints and names that specific file,
#         but NOT the inside-archive file changed in the same commit ---
out15="$(UMWELT_RUNTIME_BASE="$scratch_runtime_base" bash "$scratch_installer" --target alpha --ref "$commit_c" 2>&1)"
echo "$out15"
echo "$out15" | grep -q 'fall OUTSIDE the runtime archive' \
    || fail "a diff including a file outside the archive paths (plugin/stub.py) did not print the outside-archive-boundary report"
echo "$out15" | grep -qF 'plugin/stub.py' \
    || fail "the outside-archive-boundary report did not name the specific outside-archive file plugin/stub.py"
if echo "$out15" | grep -qF 'src/umwelt/__init__.py'; then
    fail "the outside-archive-boundary report incorrectly named an inside-archive file (src/umwelt/__init__.py) changed in the same commit"
fi
pass "a diff including an outside-archive file prints the report and names exactly that file, excluding the inside-archive file changed in the same commit"

# --- 16. REGRESSION: refactoring the archive path list from inline
#         `git archive` arguments to the runtime_archive_paths array must not
#         change what actually gets exported -- versions/<sha>/ contains
#         EXACTLY src/umwelt/, requirements.txt, PROTOCOL.md (plus the
#         script's own COMMIT_SHA/.export-complete markers) and nothing
#         else -- in particular, plugin/ must NOT be exported. ---
scratch_version_dir_c="$scratch_runtime_base/runtime-alpha/versions/$commit_c"
actual_top_level="$(LC_ALL=C ls -A "$scratch_version_dir_c" | LC_ALL=C sort | tr '\n' ' ')"
expected_top_level="$(printf '%s\n' COMMIT_SHA PROTOCOL.md requirements.txt src .export-complete | LC_ALL=C sort | tr '\n' ' ')"
[[ "$actual_top_level" == "$expected_top_level" ]] \
    || fail "exported versions/$commit_c top level is '$actual_top_level', expected '$expected_top_level' -- the archive-path refactor changed what gets exported"
[[ ! -e "$scratch_version_dir_c/plugin" ]] \
    || fail "plugin/ was exported into versions/$commit_c -- the archive path list leaked something it shouldn't"
pass "the runtime_archive_paths refactor exports exactly src/umwelt/, requirements.txt, PROTOCOL.md (plus the script's own COMMIT_SHA/.export-complete markers) and nothing else"

# --- 17. NO-OP RE-INSTALL (umwelt #799): installing the SAME --ref twice in
#         a row (prev_sha == sha) must print NO outside-archive-boundary
#         report -- the diff-and-report logic only fires when
#         prev_sha != sha, which is correct by construction today per
#         install-world.sh's own guard but was untested directly until now.
#         Reuses the scratch repo/runtime from tests 13-16, still live here. ---
out17="$(UMWELT_RUNTIME_BASE="$scratch_runtime_base" bash "$scratch_installer" --target alpha --ref "$commit_c" 2>&1)"
echo "$out17"
if echo "$out17" | grep -qi 'fall OUTSIDE the runtime archive'; then
    fail "re-installing the SAME sha ($commit_c) a second time in a row printed an outside-archive-boundary report -- prev_sha == sha should produce no diff report"
fi
pass "installing the same --ref twice in a row (prev_sha == sha) prints no outside-archive-boundary report"

rm -rf "$scratch_repo" "$scratch_runtime_base"
scratch_repo=""
scratch_runtime_base=""

echo "ALL TESTS PASSED"
