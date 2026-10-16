#!/usr/bin/env python3
"""bell-freshness-check — deploy-freshness self-check for the umwelt bell.

Part of the fleet-wide deploy-freshness reporter (design: Ariadne, alpha#2491
-- note the cross-repo pointer: the design issue lives in engram-alpha, not
this repo, a slip in how this lane was dispatched). This is the ONE
artifact-class script for the umwelt bell (`umwelt-bell.py`), per the
self-check architecture decision: a central reader cannot perform the
execution-signature probe at all (it cannot read another process's stdout,
only its files), so centralizing would silently downgrade every row to the
weaker byte probe. This script therefore checks ONLY the LOCAL seat's own
bell -- it has no cross-seat/remote-read capability, and none should be added
here.

Two probes, and every result declares which one(s) it used:

  * byte probe    -- does the DEPLOYED bell file's content match the REPO's
                      tip content for the bell's tracked path? (sha256, not a
                      byte diff dump.)
  * execution probe -- does the RUNNING bell process's start time postdate
                      every LOCAL file it loads at import time (the entry
                      script + its resolved `from umwelt import X` /
                      `import umwelt.X` top-level imports)? This is Ariadne's
                      MEASURED method (not just proposed -- see alpha#2491's
                      2026-09-02 update): a negative control (a live bell,
                      correctly read in-sync) and a positive control (a
                      scratch sleeper, confirmed in-sync, then touched and
                      re-checked -> correctly read stale) were both run before
                      this method was adopted.

Explicitly OUT of scope (see alpha#2491 and this lane's own dispatch spec):
  * Whether the LOCAL git checkout itself is stale relative to its remote --
    this tool never fetches. `--ref` (default HEAD) is resolved against
    whatever the local checkout's HEAD happens to point at right now.
  * Plugin-tree freshness for anything other than the bell (marketplace
    source vs cache snapshot vs the running MCP server process) -- a named
    open research question in alpha#2491, not this artifact class.
  * Any cross-seat / remote-read capability -- a deliberate architecture
    decision, not an oversight; do not add one here.
  * Touching, restarting, or signaling the bell process in any way -- this is
    a read-only observer. It verifies a pid; it never signals one.

Five states (exactly):

  fresh (execution)          -- verified live bell process; execution probe
                                 says in-sync; byte probe says deployed
                                 content matches repo tip.
  stale (execution)          -- verified live bell process; execution probe
                                 says stale, OR byte probe says mismatch (or
                                 both -- both probe results are always
                                 reported so a reader can tell which).
  possibly stale (ambiguous) -- verified live bell process; execution probe
                                 says stale, BUT byte probe says the deployed
                                 content matches repo tip exactly. This is
                                 Ariadne's predicted-but-unmeasured false-stale
                                 mode: `install-local-marketplace-umwelt.sh`
                                 may rewrite a file's mtime without changing
                                 its content (e.g. a companion import file,
                                 not just the entry script), which would make
                                 the execution probe fire on a no-op reinstall.
                                 Rendered distinctly rather than folded into
                                 plain `stale` -- a false-stale a reader can
                                 see beats one they can't.
  unobserved                 -- no live, verified bell process was found
                                 (pidfile missing, stale/malformed, points at
                                 a dead pid, or the pid exists but fails the
                                 identity check). Nothing to observe; not a
                                 failure of this tool.
  UNKNOWN                    -- this tool itself could not complete a check it
                                 should be able to (the pid liveness/identity
                                 lookup unavailable/erroring, a pidfile
                                 unreadable for a permissions reason, the
                                 repo path doesn't exist, etc). Genuinely
                                 distinct from `unobserved`.

  A verified live bell whose byte probe is UNAVAILABLE (umwelt#769 -- the
  normal case for a plugin-deployed copy, which has no git root) does NOT
  collapse to bare UNKNOWN: the execution verdict is fully known and is
  reported as `fresh (execution) · byte UNKNOWN (<reason>)` or its stale
  twin. Per-cell UNKNOWN, not per-row -- a probe being unavailable is a fact
  about THAT probe, not grounds to discard a different probe's real answer.
  Overall bare `fresh (execution)` still requires byte evidence; it is never
  returned from the byte-unavailable path.

IMPORTANT -- order matters right after a rebuild (measured, not theoretical:
Ariadne's own first live activation of this tool hit this): rebuild → re-arm
→ check. A rebuild rewrites the deployed bell's mtime; if you check BEFORE
re-arming the bell process, the still-running (pre-rebuild) process now
predates the just-rewritten file, and the tool will correctly report
`possibly stale (ambiguous)` on what is about to be a perfectly healthy bell.
That is not a false alarm -- it is a genuinely accurate description of a
genuinely transient state -- but reading it as "broken" rather than "needs a
re-arm" is the wrong response. Re-arm, then check.

Usage:
    python3 plugin/tools/bell-freshness-check.py             # human-readable
    python3 plugin/tools/bell-freshness-check.py --json
    python3 plugin/tools/bell-freshness-check.py --self-test  # positive-control run

Output is always a JSON-array SHAPE (one element today) even though this
script checks exactly one artifact on one seat -- so a future aggregator
reading many seats' reports doesn't need a different parser for the
single-artifact case. The same array is written to the report file
(default `~/.umwelt/freshness-report-bell.json`, `--report-file` override).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# --- constants ---------------------------------------------------------

# The bell's tracked path in the repo. NOTE: this lane's dispatch spec said
# "tools/umwelt-bell.py", but the actual git-tracked source lives at
# plugin/tools/umwelt-bell.py -- verified against both `git ls-files` and
# tools/install-local-marketplace-umwelt.sh's own copy step, which strips the
# leading "plugin/" when assembling the deployed marketplace tree (the same
# plugin-restructures-paths discipline engram-alpha's internal contributor guide documents for
# its own plugin). Corrected here rather than left to silently resolve to a
# nonexistent path at `git show` time.
DEFAULT_REPO_TRACKED_PATH = "plugin/tools/umwelt-bell.py"

DEFAULT_DEPLOYED_PATH = (
    Path.home() / ".umwelt" / "marketplace" / "plugins" / "umwelt" / "tools"
    / "umwelt-bell.py"
)
DEFAULT_CURSOR_FILE = Path.home() / ".umwelt" / "bell-cursors.json"
DEFAULT_CONFIG_PATH = Path.home() / ".umwelt" / "config.json"
DEFAULT_REPORT_FILE = Path.home() / ".umwelt" / "freshness-report-bell.json"

# The literal token umwelt-bell.py's own `_is_bell_process()` matches against
# (see plugin/tools/umwelt-bell.py). Named here so --self-test's synthetic
# entry script, which must be named exactly this, and the verification logic
# below stay obviously in sync.
BELL_SCRIPT_NAME = "umwelt-bell.py"

ARTIFACT_CLASS = "bell"


# --- subprocess helpers --------------------------------------------------

def _run(args: list[str], cwd: str | None = None, timeout: float | None = None):
    """Run a command, capturing text output. Returns (returncode, stdout, stderr).
    Never raises for a nonzero exit; raises only if the executable itself
    can't be invoked (FileNotFoundError etc), left to the caller to catch."""
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _run_bytes(args: list[str], cwd: str | None = None, timeout: float | None = None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def repo_root_for(script_path: Path) -> str | None:
    """Resolve the git repo root containing `script_path`'s directory. Returns
    None if that fails (not a repo, git unavailable, etc) -- caller treats
    that as an UNKNOWN-worthy condition for the byte probe."""
    try:
        rc, out, _ = _run(["git", "rev-parse", "--show-toplevel"],
                          cwd=str(script_path.resolve().parent))
    except OSError:
        return None
    if rc != 0:
        return None
    return out.strip()


# --- pidfile / process identity ---------------------------------------
#
# NOTE this is now a DELIBERATE DIVERGENCE from plugin/tools/umwelt-bell.py's
# own proc_cmdline()/_is_bell_process(), not a hand-kept copy of it. That
# file's version stays `ps`-based because the bell needs macOS support; this
# tool is Linux-only by design (see module docstring), so it reads
# /proc/<pid>/cmdline directly instead. This also fixes a real bug the `ps`
# approach had here: `ps -o args=` space-joins argv into one string, and a
# naive `cmd.split()` re-split can manufacture a false argv boundary out of
# a single token that itself contains a space or slash-looking substring
# (e.g. a bash wrapper's `eval '...'` argument) -- measured live by a
# colleague review on PR#767 as a false "ends with umwelt-bell.py" match on
# a wrapper process. Reading /proc/<pid>/cmdline's NUL-delimited fields gives
# the TRUE argv boundaries, so `proc_cmdline()` now returns the argv list
# directly rather than a re-joinable string -- the string form is exactly
# what let the bug happen, so keeping it around under a different fetch
# mechanism would leave the same hazard for the next caller. If
# umwelt-bell.py's identity-predicate LOGIC changes (what makes something
# "a bell"), this file's `_is_bell_process()` needs a matching update; the
# argv-FETCH mechanism itself is now intentionally NOT shared. -------------

def proc_cmdline(pid: int) -> tuple[str, list[str]]:
    """Look up a pid's true argv via /proc/<pid>/cmdline (Linux only -- see
    module docstring; no macOS fallback is added here on purpose).
    Returns (status, argv):
      "ok"      -- argv is the process's TRUE, NUL-delimited argv list (may
                    be an empty list, e.g. for a zombie process)
      "gone"    -- the pid does not exist (no /proc/<pid>/cmdline)
      "unknown" -- could not determine (permission denied, or some other
                    unexpected OSError -- including the pid being reaped
                    between the caller's liveness check and this read, a
                    narrow race that existed with `ps` too)
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return ("gone", [])
    except OSError:
        # permission denied, or the pid was reaped between the caller's
        # liveness check and this read -- no error-message slot in this
        # tuple's second element (it's argv, not a message), matching the
        # ("unknown", "") shape the ps-based version returned.
        return ("unknown", [])
    return ("ok", _split_cmdline_bytes(raw))


def _split_cmdline_bytes(raw: bytes) -> list[str]:
    """Split a /proc/<pid>/cmdline-shaped byte string on NUL, discarding the
    single trailing empty element the format's trailing NUL produces.
    Factored out of proc_cmdline() so the actual boundary-preserving parse
    logic can be exercised directly against a real byte fixture in tests,
    without needing a live /proc/<pid> to point at."""
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts = parts[:-1]  # trailing NUL produces one empty tail element
    return [p.decode(errors="replace") for p in parts]


def _is_bell_process(argv: list[str] | None) -> bool:
    """True iff argv is a python interpreter running the bell script.
    Conceptually the same predicate as umwelt-bell.py's own
    `_is_bell_process()` (argv[0] is a python interpreter, checked
    case-insensitively -- macOS's python.org FRAMEWORK build reports argv[0]
    as capital-P ``.../MacOS/Python``, which a case-sensitive check misses,
    so a live framework-Python bell is never reaped (umwelt #955) -- AND
    some later argv token is/ends-with "umwelt-bell.py"), but taking the
    TRUE argv list directly -- no string join+resplit, which is exactly the
    false-positive mechanism this lane fixes (see the block comment above)."""
    if not argv:
        return False
    if not os.path.basename(argv[0]).lower().startswith("python"):
        return False
    return any(
        a == BELL_SCRIPT_NAME or a.endswith("/" + BELL_SCRIPT_NAME)
        for a in argv[1:]
    )


def get_pid_status(cursor_file: Path) -> dict:
    """Read <cursor_file>.pid and verify it points at a live bell process,
    conceptually the same verification chain as umwelt-bell.py's own
    reap_prior(): read pidfile -> look up the pid's argv -> confirm it's a
    live bell. (The lookup itself is /proc-based here, not `ps`-based -- see
    proc_cmdline()'s docstring.)

    Returns a dict with at least {pidfile, pid_status, pid, process_cmd} and
    optionally {error}. pid_status is one of: no_pidfile, pidfile_unreadable
    (permissions -- UNKNOWN-worthy), pidfile_malformed (not an int --
    unobserved-worthy, same as a stale/dead pointer), dead_pid, wrong_process,
    ps_unknown (the pid lookup itself failed -- UNKNOWN-worthy; named
    ps_unknown for state-contract continuity even though the lookup is now
    /proc-based, not `ps`-based), found_verified.
    """
    pidfile = Path(str(cursor_file) + ".pid")
    try:
        raw = pidfile.read_text()
    except FileNotFoundError:
        return {"pidfile": str(pidfile), "pid_status": "no_pidfile",
                "pid": None, "process_cmd": None}
    except PermissionError as e:
        return {"pidfile": str(pidfile), "pid_status": "pidfile_unreadable",
                "pid": None, "process_cmd": None,
                "error": f"permission denied reading pidfile: {e}"}
    except OSError as e:
        return {"pidfile": str(pidfile), "pid_status": "no_pidfile",
                "pid": None, "process_cmd": None,
                "error": f"could not read pidfile: {e}"}

    raw = raw.strip()
    try:
        pid = int(raw)
    except ValueError:
        return {"pidfile": str(pidfile), "pid_status": "pidfile_malformed",
                "pid": None, "process_cmd": None,
                "error": f"pidfile did not contain an integer pid: {raw!r}"}

    status, cmd = proc_cmdline(pid)
    if status == "gone":
        return {"pidfile": str(pidfile), "pid_status": "dead_pid",
                "pid": pid, "process_cmd": None}
    if status == "unknown":
        return {"pidfile": str(pidfile), "pid_status": "ps_unknown",
                "pid": pid, "process_cmd": None,
                "error": "the pid liveness/identity lookup was unavailable "
                         f"or errored while checking pid {pid}"}
    # status == "ok"
    if _is_bell_process(cmd):
        return {"pidfile": str(pidfile), "pid_status": "found_verified",
                "pid": pid, "process_cmd": cmd}
    return {"pidfile": str(pidfile), "pid_status": "wrong_process",
            "pid": pid, "process_cmd": cmd}


# --- import resolution (ast-based, one level deep) ------------------------

def resolve_local_imports(entry_script: Path) -> tuple[list[Path], list[str]]:
    """Parse `entry_script` (as it actually sits on disk -- the DEPLOYED
    copy) for top-level `from umwelt import X` / `import umwelt.X`
    statements, and resolve each to a file under the local `umwelt/` package
    that sits alongside `entry_script`'s tools/ directory -- i.e.
    entry_script.parent.parent / "umwelt", the exact layout umwelt-bell.py's
    own `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` +
    `from umwelt import client_auth` (line 69) rely on.

    Deliberately does NOT hardcode "client_auth.py" -- resolution is generic
    over whatever `umwelt.*` names the entry script imports at module level,
    so this stays correct if the bell adds another local import later.

    LIMITATION (stated, not silent): only ONE level of resolution is
    performed -- the resolved files' OWN imports are not recursively parsed.
    Sufficient for the bell's current single-hop `from umwelt import
    client_auth`; a deeper import chain would need a recursive walk this
    function does not do.

    Returns (resolved_paths, warnings). A recognized-but-unresolvable import
    (module renamed/removed) is a warning, not a hard failure -- it doesn't
    block determining freshness of the files that DID resolve.
    """
    warnings: list[str] = []
    resolved: list[Path] = []
    try:
        src = entry_script.read_text()
    except OSError as e:
        return [], [f"could not read entry script {entry_script} for import "
                     f"parsing: {e}"]
    try:
        tree = ast.parse(src, filename=str(entry_script))
    except SyntaxError as e:
        return [], [f"could not parse entry script {entry_script} as "
                     f"python: {e}"]

    package_root = entry_script.resolve().parent.parent
    umwelt_pkg_dir = package_root / "umwelt"

    def _candidate(dotted: str) -> Path:
        if not dotted:
            return umwelt_pkg_dir / "__init__.py"
        return umwelt_pkg_dir / (dotted.replace(".", "/") + ".py")

    for node in tree.body:  # top-level only, deliberately
        if isinstance(node, ast.ImportFrom):
            if node.level != 0 or not node.module:
                continue  # relative import, or a "from . import x" shape
            if node.module != "umwelt" and not node.module.startswith("umwelt."):
                continue
            submodule_prefix = node.module[len("umwelt"):].lstrip(".")
            for alias in node.names:
                dotted = ".".join(p for p in (submodule_prefix, alias.name) if p)
                candidate = _candidate(dotted)
                if candidate.exists():
                    resolved.append(candidate)
                else:
                    warnings.append(
                        f"could not resolve 'from {node.module} import "
                        f"{alias.name}' to a file at {candidate}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != "umwelt" and not alias.name.startswith("umwelt."):
                    continue
                dotted = alias.name[len("umwelt"):].lstrip(".")
                candidate = _candidate(dotted)
                if candidate.exists():
                    resolved.append(candidate)
                else:
                    warnings.append(
                        f"could not resolve 'import {alias.name}' to a file "
                        f"at {candidate}")
    return resolved, warnings


# --- probes ---------------------------------------------------------------

def run_byte_probe(deployed_path: Path, repo_root: str | None, ref: str,
                    tracked_path: str) -> dict:
    """Compare the DEPLOYED file's content hash against the REPO tip's
    content hash for `tracked_path` at `ref`. Never fetches -- staleness of
    the local checkout itself relative to its remote is out of scope."""
    result = {
        "deployed_path": str(deployed_path),
        "tracked_path": tracked_path,
        "ref": ref,
        "deployed_sha256": None,
        "repo_tip_sha256": None,
        "match": None,
        "error": None,
    }
    try:
        deployed_bytes = deployed_path.read_bytes()
    except OSError as e:
        result["error"] = f"could not read deployed file {deployed_path}: {e}"
        return result
    result["deployed_sha256"] = hashlib.sha256(deployed_bytes).hexdigest()

    if repo_root is None:
        result["error"] = "could not resolve a git repo root for the byte probe"
        return result

    try:
        rc, out, err = _run_bytes(
            ["git", "-C", repo_root, "show", f"{ref}:{tracked_path}"])
    except OSError as e:
        result["error"] = f"could not invoke git: {e}"
        return result
    if rc != 0:
        result["error"] = (
            f"git -C {repo_root} show {ref}:{tracked_path} failed: "
            f"{err.decode(errors='replace').strip()}")
        return result
    result["repo_tip_sha256"] = hashlib.sha256(out).hexdigest()
    result["match"] = result["deployed_sha256"] == result["repo_tip_sha256"]
    return result


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# Index of the `starttime` field (field 22, 1-indexed per proc(5)) within
# the whitespace-split remainder of a /proc/<pid>/stat line AFTER the
# comm-field's closing ')' -- i.e. that remainder's field 1 is /proc/<pid>/
# stat's field 3 (state). 22 - 3 = 19, 0-indexed. Verified against real
# /proc/<pid>/stat lines (both /proc/self/stat and a spawned child's) before
# committing to this index -- do not trust this arithmetic without
# re-checking it against this host's actual /proc layout, per this lane's
# own dispatch spec.
_STAT_STARTTIME_INDEX_AFTER_COMM = 19


def _parse_stat_starttime_ticks(line: str) -> int:
    """Parse the `starttime` field (field 22, 1-indexed, proc(5)) out of a
    /proc/<pid>/stat line's raw text. Raises ValueError if the line can't be
    parsed. Factored out of _process_start_epoch() so this arithmetic can be
    exercised directly against a hand-crafted, KNOWN line in tests -- rather
    than only indirectly, through a live pid.

    comm (field 2) is parenthesized and can itself contain spaces or parens
    (a process can rename itself via prctl/argv[0] tricks) -- split on the
    LAST ')' in the line to find the true end of that field, per proc(5),
    rather than naively whitespace-splitting from the start.
    """
    close_paren = line.rfind(")")
    if close_paren == -1:
        raise ValueError(f"no ')' found in stat line: {line!r}")
    fields_after_comm = line[close_paren + 1:].split()
    if len(fields_after_comm) <= _STAT_STARTTIME_INDEX_AFTER_COMM:
        raise ValueError(
            f"stat line had fewer fields than expected (got "
            f"{len(fields_after_comm)} after the comm field, needed more "
            f"than {_STAT_STARTTIME_INDEX_AFTER_COMM}): {line!r}")
    try:
        return int(fields_after_comm[_STAT_STARTTIME_INDEX_AFTER_COMM])
    except ValueError as e:
        raise ValueError(
            f"could not parse starttime field from stat line: {line!r}") from e


def _process_start_epoch(pid: int) -> tuple[float | None, str | None]:
    """Absolute process-start epoch, read from /proc/<pid>/stat's
    `starttime` field (clock ticks since boot, CLOCK_BOOTTIME domain per
    proc(5)) combined with a FRESHLY-read (time.time(), CLOCK_BOOTTIME) pair
    from this process -- NOT /proc/stat's `btime` line. Deliberately
    /proc-based, NOT `ps -o lstart=` -- this tool is Linux-only by design
    (module docstring), so the macOS-portability reason to avoid /proc
    (which umwelt-bell.py's own proc_cmdline() has) does not apply here.

    This ALSO fixes a real bug: `ps -o lstart=` truncates to whole seconds,
    while the mtimes it gets compared against (os.stat()) carry sub-second
    precision -- so genuine wall-clock skew between process spawn and a
    file's last write could ADD to that truncation error under real load,
    intermittently exceeding a tolerance sized to absorb only the
    truncation (measured directly: a colleague review's --self-test runs
    found this exceeding 1.0s in 3 of 20 runs, one example delta
    1.135726s -- see PR#767 colleague review). Reading starttime directly
    removes the truncation term from that arithmetic ENTIRELY rather than
    budgeting for it.

    DEVIATION FROM THIS LANE'S OWN DISPATCH SPEC (flagged per its own "if
    your own measurement disagrees, trust it and say so" clause): the spec
    suggested deriving boot walltime from /proc/stat's `btime` line
    (`btime_epoch + starttime_ticks / SC_CLK_TCK`). Measured directly on
    this host (WSL2, Linux 6.6.87.2-microsoft-standard-WSL2): /proc/stat's
    `btime` is offset from `time.time() - time.clock_gettime(CLOCK_BOOTTIME)`
    by a REPRODUCIBLE ~0.17-0.19s (5 direct measurements, all in that band --
    not noise). That is large enough, on its own, to make a genuinely fresh
    bell misreport as stale: a --self-test 20-run loop using the
    btime-file approach failed 20/20 with exactly this magnitude of skew
    before this fix. Reading (time.time(), CLOCK_BOOTTIME) together, right
    here, instead of trusting the kernel's separately-maintained `btime`
    value, reduces the same measurement to ~10-16ms (three runs) --
    consistent with ordinary Python interpreter startup latency, not a
    systematic clock-domain skew. So THIS function does not read
    /proc/stat at all. CLOCK_BOOTTIME (not CLOCK_MONOTONIC) is the correct
    domain match -- per proc(5), /proc/<pid>/stat's `starttime` field has,
    since Linux 2.6.something, accounted for suspend time the same way
    CLOCK_BOOTTIME does (unlike CLOCK_MONOTONIC, which does not).
    """
    try:
        line = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None, (f"/proc/{pid}/stat does not exist (pid may have "
                       f"exited between liveness check and this call)")
    except OSError as e:
        return None, f"could not read /proc/{pid}/stat: {e}"

    try:
        starttime_ticks = _parse_stat_starttime_ticks(line)
    except ValueError as e:
        return None, f"could not parse /proc/{pid}/stat: {e}"

    clk_tck = os.sysconf("SC_CLK_TCK")
    starttime_seconds = starttime_ticks / clk_tck

    # Fresh, self-consistent (wall-clock, boot-relative) pairing -- see the
    # DEVIATION note above for why this is used instead of /proc/stat's
    # own `btime` line.
    now_realtime = time.time()
    now_boottime = time.clock_gettime(time.CLOCK_BOOTTIME)
    boot_wall_epoch = now_realtime - now_boottime

    return boot_wall_epoch + starttime_seconds, None


def run_execution_probe(entry_script: Path, pid_info: dict) -> dict:
    """Compare the verified bell process's start time against the newest
    mtime among every local file it loads (entry script + resolved
    top-level `umwelt.*` imports, one level deep). Only meaningful when
    pid_info['pid_status'] == 'found_verified'; called unconditionally so the
    result always has a stable shape, but `in_sync` stays None otherwise."""
    result = {
        "entry_script": str(entry_script),
        "pid": pid_info.get("pid"),
        "process_cmd": pid_info.get("process_cmd"),
        "process_start_epoch": None,
        "process_start_iso": None,
        "checked_files": [],
        "import_warnings": [],
        "max_mtime_epoch": None,
        "max_mtime_iso": None,
        "max_mtime_file": None,
        "in_sync": None,
        "error": None,
    }
    if pid_info["pid_status"] != "found_verified":
        result["error"] = (
            f"no verified live bell process (pid_status="
            f"{pid_info['pid_status']!r}) -- execution probe not applicable")
        return result

    pid = pid_info["pid"]
    start_epoch, start_err = _process_start_epoch(pid)
    if start_err is not None:
        result["error"] = start_err
        return result
    # Re-verify identity immediately before trusting this start time
    # (reviewer-fairy round 1 on umwelt PR#767): get_pid_status() verified
    # identity once, but pid recycling in the window between that check and
    # this /proc read -- widened by the git work the byte probe does in
    # between -- could otherwise let an unrelated process's start time be
    # silently misattributed to the bell. Re-checking costs one more
    # /proc/<pid>/cmdline read and turns a silent mislabel into a loud,
    # honest error.
    recheck_status, recheck_cmd = proc_cmdline(pid)
    if recheck_status != "ok" or not _is_bell_process(recheck_cmd):
        result["error"] = (
            f"pid {pid} no longer verifies as a live bell process "
            f"immediately before trusting its start time (recheck status="
            f"{recheck_status!r}) -- likely recycled to an unrelated "
            f"process between the identity check and this probe")
        return result
    result["process_start_epoch"] = start_epoch
    result["process_start_iso"] = _iso(start_epoch)

    resolved_imports, warnings = resolve_local_imports(entry_script)
    result["import_warnings"] = warnings
    local_files = [entry_script] + resolved_imports
    result["checked_files"] = [str(f) for f in local_files]

    max_mtime = None
    max_mtime_file = None
    for f in local_files:
        try:
            mtime = f.stat().st_mtime
        except OSError as e:
            result["error"] = f"could not stat local file {f}: {e}"
            return result
        if max_mtime is None or mtime > max_mtime:
            max_mtime = mtime
            max_mtime_file = str(f)
    result["max_mtime_epoch"] = max_mtime
    result["max_mtime_iso"] = _iso(max_mtime)
    result["max_mtime_file"] = max_mtime_file
    # _process_start_epoch() now reads /proc/<pid>/stat's starttime directly
    # instead of the whole-second-truncated `ps -o lstart=`, which removes
    # the dominant error term (measured up to 1.135726s in a colleague
    # review's --self-test runs, see that function's docstring) from this
    # comparison ENTIRELY -- there is no longer a truncation term to budget
    # for. What remains is only the residual quantization of
    # /proc/<pid>/stat's starttime itself: it's reported in whole clock
    # ticks since boot (SC_CLK_TCK, typically 100Hz/10ms on Linux), so the
    # true start time can be up to one tick LATER than the reported value.
    # A small tolerance -- an order of magnitude above one tick, so it
    # absorbs that quantization plus ordinary read/write scheduling jitter
    # without needing to be tied to the live SC_CLK_TCK value -- keeps this
    # comparison honest without reintroducing a budget sized for an error
    # source (whole-second truncation) that no longer exists. Any genuine
    # staleness gap (a redeploy followed by a restart, or vice versa) is, per
    # the tool's own design, seconds-to-minutes -- far larger than this.
    _START_TIME_TOLERANCE_S = 0.1
    result["in_sync"] = start_epoch >= (max_mtime - _START_TIME_TOLERANCE_S)
    return result


# --- state determination ---------------------------------------------------

_INTERNAL_ERROR_PID_STATUSES = {"pidfile_unreadable", "ps_unknown"}
_UNOBSERVED_PID_STATUSES = {"no_pidfile", "pidfile_malformed", "dead_pid",
                            "wrong_process"}


def determine_state(pid_info: dict, byte_result: dict, exec_result: dict) -> str:
    """Per-cell UNKNOWN, not per-row (umwelt#769, per-axis fix): the byte
    probe and execution probe answer different questions, and one being
    unavailable is a fact about THAT probe, not a reason to discard a
    perfectly good verdict from the other. This matters concretely once the
    checker ships as part of the plugin (umwelt#769): a deployed copy has no
    git root, so the byte probe is ALWAYS unavailable there -- collapsing
    that to a bare "UNKNOWN" would make the tool say "I don't know" on
    every single run on exactly the seats it was shipped to help, even
    though the execution probe (which needs no repo at all) has a
    perfectly good, fully-computed answer to the question that matters most
    for a per-seat liveness check. Caught by Ariadne re-reading her own
    spec's own rule ("UNKNOWN attaches to the cell, not the row") against
    the code that violated it, before the ship-it change was written."""
    pid_status = pid_info["pid_status"]
    if pid_status in _INTERNAL_ERROR_PID_STATUSES:
        return "UNKNOWN"
    if pid_status in _UNOBSERVED_PID_STATUSES:
        return "unobserved"
    # pid_status == "found_verified" from here on
    if exec_result.get("error") is not None or exec_result.get("in_sync") is None:
        return "UNKNOWN"

    in_sync = exec_result["in_sync"]

    if byte_result.get("error") is not None or byte_result.get("match") is None:
        # Byte probe unavailable (e.g. a plugin-deployed copy with no git
        # root) -- the execution verdict is fully known and real, so report
        # it, with the byte axis marked UNKNOWN and its actual reason (not a
        # canned phrase -- the unavailable-byte case has more than one real
        # cause: no repo root, a git failure, an unreadable deployed file).
        # This can never collapse into a bare "fresh (execution)": overall
        # `fresh` still requires byte evidence, per the ship-it ruling.
        # The ambiguous carve-out below also needs byte evidence (it
        # specifically requires byte MATCH to distinguish itself from plain
        # stale), so it is correctly unreachable here too -- an honest
        # narrower claim, not a missing refinement.
        execution_bit = "fresh (execution)" if in_sync else "stale (execution)"
        byte_reason = byte_result.get("error") or "byte probe unavailable"
        return f"{execution_bit} · byte UNKNOWN ({byte_reason})"

    match = byte_result["match"]
    # Ariadne's false-stale mode: execution says stale, but the byte probe
    # (which only checks the entry script) says the deployed content matches
    # repo tip exactly -- a companion imported file's mtime was bumped
    # without a content change (e.g. a reinstall script rewriting files it
    # didn't actually change). Carved out BEFORE the general stale rule so it
    # never collapses into plain `stale (execution)`.
    if (not in_sync) and match:
        return "possibly stale (ambiguous)"
    if (not in_sync) or (not match):
        return "stale (execution)"
    return "fresh (execution)"


# --- orchestration ----------------------------------------------------------

def read_seat(config_path: Path) -> str | None:
    try:
        data = json.loads(config_path.read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        agent = data.get("agent")
        if isinstance(agent, str) and agent.strip():
            return agent.strip()
    return None


def check_bell(*, deployed_path: Path, cursor_file: Path, repo_root: str | None,
                ref: str, tracked_path: str, seat: str | None) -> dict:
    pid_info = get_pid_status(cursor_file)
    byte_result = run_byte_probe(deployed_path, repo_root, ref, tracked_path)
    exec_result = run_execution_probe(deployed_path, pid_info)
    state = determine_state(pid_info, byte_result, exec_result)
    return {
        "seat": seat,
        "artifact_class": ARTIFACT_CLASS,
        "state": state,
        "probes": {
            "byte": byte_result,
            "execution": {**exec_result, "pid_status": pid_info["pid_status"],
                          "pidfile": pid_info["pidfile"]},
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def render_human(record: dict) -> str:
    seat = record["seat"] or "<unknown seat>"
    byte_p = record["probes"]["byte"]
    exec_p = record["probes"]["execution"]
    byte_bit = "match" if byte_p.get("match") else (
        "mismatch" if byte_p.get("match") is False else
        f"unknown ({byte_p.get('error')})")
    exec_bit = "in-sync" if exec_p.get("in_sync") else (
        "stale" if exec_p.get("in_sync") is False else
        f"unknown ({exec_p.get('error') or exec_p.get('pid_status')})")
    return (f"{record['artifact_class']} ({seat}): {record['state']}"
            f" -- byte: {byte_bit}; execution: {exec_bit}"
            f" [checked_at={record['checked_at']}]")


def write_report(report_file: Path, records: list[dict]) -> bool:
    """Write the report file. Returns True on success. A failure here (e.g.
    a permission error on the default path) is reported to stderr rather
    than left to raise an unhandled traceback past the check's own
    already-printed stdout/JSON output (reviewer-fairy round 1, PR#767)."""
    try:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = report_file.with_suffix(report_file.suffix + ".tmp")
        tmp.write_text(json.dumps(records, indent=2) + "\n")
        os.replace(tmp, report_file)
        return True
    except OSError as e:
        print(f"bell-freshness-check: could not write report file "
              f"{report_file}: {e}", file=sys.stderr)
        return False


# --- --self-test -------------------------------------------------------

_FAKE_ENTRY_SCRIPT = '''\
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from umwelt import client_auth  # noqa: F401,E402
time.sleep(60)
'''

_FAKE_CLIENT_AUTH = "# fake companion module for bell-freshness-check --self-test\n"


def _spawn_fake_bell(scratch: Path, proc_holder: list) -> tuple[subprocess.Popen, Path, Path]:
    """`proc_holder` is a caller-owned list this appends the spawned Popen to
    immediately after it starts -- before any later step in this function
    (writing the pidfile, etc.) that could still raise. Binding only via
    this function's return value would leave a real spawned child
    unreachable to the caller's cleanup if such a later step failed after
    Popen() succeeded (reviewer-fairy round 1, PR#767)."""
    tools_dir = scratch / "tools"
    pkg_dir = scratch / "umwelt"
    tools_dir.mkdir(parents=True, exist_ok=True)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text("")
    client_auth_path = pkg_dir / "client_auth.py"
    client_auth_path.write_text(_FAKE_CLIENT_AUTH)
    entry_path = tools_dir / BELL_SCRIPT_NAME
    entry_path.write_text(_FAKE_ENTRY_SCRIPT)

    # A real git repo so the byte probe has a genuine tip to compare against
    # -- exercises the same code path main() uses, not a stubbed-out one.
    _run(["git", "init", "-q"], cwd=str(scratch))
    _run(["git", "-C", str(scratch), "config", "user.email", "self-test@example.invalid"])
    _run(["git", "-C", str(scratch), "config", "user.name", "bell-freshness-check self-test"])
    _run(["git", "-C", str(scratch), "add", "-A"])
    _run(["git", "-C", str(scratch), "commit", "-q", "-m", "seed"])

    proc = subprocess.Popen([sys.executable, str(entry_path)])
    proc_holder.append(proc)  # bound immediately -- see docstring
    cursor_file = scratch / "bell-cursors.json"
    pidfile = Path(str(cursor_file) + ".pid")
    pidfile.write_text(str(proc.pid))
    return proc, entry_path, cursor_file


def run_self_test() -> int:
    """Positive-control discipline (alpha#2491): a reporter's most likely
    real output is "everything fresh", a null result that is evidence only
    once the instrument has proven it can produce a non-null on a KNOWN
    positive. Builds a fully synthetic bell in a scratch dir, spawns it as a
    REAL subprocess with a real pidfile (so the pid-verification path is
    exercised for real, not mocked), and runs the actual check_bell()
    against it -- the same function --json/default use, not a special-cased
    copy.

    NOTE on the fourth assertion below: this lane's own dispatch spec's
    self-test recipe describes touching "the fake import file's mtime
    forward" and asserts the result reads `stale (execution)`. Per the
    state-definitions section of the SAME spec (and Ariadne's own
    false-stale rationale in alpha#2491), that exact scenario -- a companion
    import file's mtime bumped with NO content change, entry-script content
    untouched -- is precisely what `possibly stale (ambiguous)` exists to
    name; asserting plain `stale (execution)` for it would make this
    self-test assert a state its own tool would never legitimately produce
    for that input. Resolved in favor of the more detailed, worked-through
    state-definitions section (flagged in this lane's handoff for review):
    the import-mtime-only bump is asserted as `possibly stale (ambiguous)`,
    and a further step also edits the deployed entry script's own bytes (a
    genuine content divergence from the committed tip) to demonstrate an
    unambiguous `stale (execution)` as well -- so both named states this
    scenario class can produce are actually exercised, not just one.
    """
    scratch = Path(tempfile.mkdtemp(prefix="bell-freshness-selftest-"))
    proc: subprocess.Popen | None = None
    proc_holder: list[subprocess.Popen] = []
    failures: list[str] = []
    try:
        proc, entry_path, cursor_file = _spawn_fake_bell(scratch, proc_holder)
        # Give the child a moment to actually start (so its ps lstart is a
        # real, queryable timestamp) before we read it back.
        time.sleep(0.3)

        def _check():
            return check_bell(
                deployed_path=entry_path,
                cursor_file=cursor_file,
                repo_root=str(scratch),
                ref="HEAD",
                tracked_path="tools/" + BELL_SCRIPT_NAME,
                seat="self-test",
            )

        # 1) Negative control: freshly spawned, untouched -> fresh (execution).
        record = _check()
        if record["state"] != "fresh (execution)":
            failures.append(
                f"negative control: expected 'fresh (execution)', got "
                f"{record['state']!r} -- {json.dumps(record, indent=2)}")

        # 2) Positive control, ambiguous branch: bump the companion import
        #    file's mtime forward with NO content change.
        future = time.time() + 5
        os.utime(scratch / "umwelt" / "client_auth.py", (future, future))
        record = _check()
        if record["state"] != "possibly stale (ambiguous)":
            failures.append(
                f"positive control (mtime-only bump): expected 'possibly "
                f"stale (ambiguous)', got {record['state']!r} -- "
                f"{json.dumps(record, indent=2)}")

        # 3) Positive control, unambiguous branch: also change the deployed
        #    entry script's own BYTES (not just mtime) so the byte probe
        #    genuinely diverges from the committed tip.
        with open(entry_path, "a") as f:
            f.write("# self-test-induced drift\n")
        os.utime(entry_path, (future, future))
        record = _check()
        if record["state"] != "stale (execution)":
            failures.append(
                f"positive control (byte drift): expected 'stale "
                f"(execution)', got {record['state']!r} -- "
                f"{json.dumps(record, indent=2)}")

        if failures:
            print("bell-freshness-check --self-test: FAILED", file=sys.stderr)
            for f in failures:
                print(f" - {f}", file=sys.stderr)
            return 1
        print("bell-freshness-check --self-test: PASSED "
              "(negative control + two positive controls confirmed)")
        return 0
    finally:
        # Prefer the outer `proc` binding (set on a normal return from
        # _spawn_fake_bell), but fall back to proc_holder for the narrow
        # case where _spawn_fake_bell raised after Popen() succeeded but
        # before its tuple-unpack return completed -- otherwise that child
        # would never be reachable here and would leak (reviewer-fairy
        # round 1, PR#767).
        proc = proc if proc is not None else (proc_holder[0] if proc_holder else None)
        if proc is not None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(scratch, ignore_errors=True)


# --- CLI ---------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="bell-freshness-check",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--deployed-path", default=str(DEFAULT_DEPLOYED_PATH),
                    help="path to the DEPLOYED bell script "
                         f"(default: {DEFAULT_DEPLOYED_PATH})")
    ap.add_argument("--ref", default="HEAD",
                    help="git ref to compare the deployed file against "
                         "(default: HEAD). This tool never fetches -- "
                         "staleness of the local checkout's HEAD relative to "
                         "its remote is out of scope; see the module "
                         "docstring.")
    ap.add_argument("--cursor-file", default=str(DEFAULT_CURSOR_FILE),
                    help="bell cursor file whose <cursor-file>.pid is read "
                         f"for pid verification (default: {DEFAULT_CURSOR_FILE})")
    ap.add_argument("--seat", default=None,
                    help="override the seat name (default: read "
                         f"~/.umwelt/config.json's 'agent' field)")
    ap.add_argument("--report-file", default=str(DEFAULT_REPORT_FILE),
                    help=f"where to write the JSON report (default: {DEFAULT_REPORT_FILE})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--self-test", action="store_true",
                    help="run the built-in positive-control self-test and exit")
    args = ap.parse_args()

    if args.self_test:
        return run_self_test()

    deployed_path = Path(args.deployed_path)
    cursor_file = Path(args.cursor_file)
    report_file = Path(args.report_file)
    seat = args.seat or read_seat(DEFAULT_CONFIG_PATH)
    repo_root = repo_root_for(Path(__file__))

    record = check_bell(
        deployed_path=deployed_path,
        cursor_file=cursor_file,
        repo_root=repo_root,
        ref=args.ref,
        tracked_path=DEFAULT_REPO_TRACKED_PATH,
        seat=seat,
    )
    records = [record]

    if args.json:
        print(json.dumps(records, indent=2))
    else:
        for r in records:
            print(render_human(r))

    if not write_report(report_file, records):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
