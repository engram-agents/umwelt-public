"""The `use` execution sandbox — the one safety-critical surface in Umwelt.

v0 posture (HONEST about limits): best-effort isolation, NOT a hardened jail.
  - runs in a fresh temp copy of the thing's source (never in place / in a home)
  - scrubbed, minimal environment (no inherited secrets or proxy vars)
  - best-effort network isolation via `unshare -rn` when available (real kernel
    netns); falls back to env-scrub only, and the outcome REPORTS which held
  - hard timeout, output captured, never a live shell
  - shell=False (shlex-split) — no shell metacharacter surface by default

v1 FS isolation (run_thing with fs_isolated=True — the default):
  Axis 1 — FS-reachability confinement via mount namespace + pivot_root
    (sandbox_entrypoint.py).  Fixture (b) of the integrity bench proves this
    axis: the scroll cannot reach host filesystem paths outside its work dir.
    Fixture (b)-green is necessary but not sufficient for full confinement.
  Axis 2 — kernel-escape resistance via seccomp denylist (mount/ptrace/bpf/…).
    Seccomp is the second-axis mitigation; fixture (b) does NOT test this axis.
    Both axes are required; neither alone is full confinement.

Capability channel (run_thing_capped): adds a per-use UDS at $WORLD_CAP_SOCK
and injects the `world` CLI onto PATH. The handler validates every frame; the
CLI is convenience only. See docs/CAPABILITY-CHANNEL.md.
v1 limitation: run_thing_capped uses fs_isolated=False (socket lives outside
the work dir and would be inaccessible after pivot_root). FS isolation for
capped runs is deferred to v2.

This is enough to run *our own team's* things safely. It is NOT enough to run
arbitrary untrusted code from outside the trusted group — that needs containers /
seccomp / firejail, tracked as the hardening path. Do not relax that assumption
without the hardening.

Resource + orphan + output hardening (Kepler security review, forum #199 post
3868, 2026-07-01; umwelt issues #11/#12/#13): the single subprocess call site in
run_thing() (shared by fs_isolated=True/False and by run_thing_capped, which
delegates to run_thing) now applies (a) a cgroup-v2 pids/memory/cpu cap when a
delegated cgroup-v2 subtree is writable, plus a portable `setrlimit`
(RLIMIT_AS/CPU/FSIZE) floor that works even without cgroups (portable across
Linux hosts; the RLIMIT_AS and RLIMIT_NPROC floors are not enforced or
validated on macOS, see README "Platform limits (hosting)" and umwelt#595); (b) a new process
group per run (`start_new_session=True`) + `os.killpg` on timeout, so a
scroll that double-forks/backgrounds a process doesn't outlive the sandbox
call on the non-FS-isolated path (the FS-isolated path's pid-namespace
teardown already reaped orphans as of PR #10); (c) output streamed through a
hard byte cap DURING the read rather than buffered in full then sliced, so a
scroll printing gigabytes cannot OOM the engine process itself.
"""

from __future__ import annotations

import functools
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    import resource as _resource
except ImportError:  # pragma: no cover — resource is POSIX-only; this codebase
    _resource = None  # already assumes POSIX/Linux (unshare, pivot_root) elsewhere.

DEFAULT_TIMEOUT_S = 30

# A deliberately minimal environment. No secrets, no proxy (proxy vars would
# re-open a network path even under env-scrub), no ambient creds.
_SAFE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "HOME": "/tmp",       # overwritten to the sandbox dir below
    "PYTHONDONTWRITEBYTECODE": "1",
}

# ---------------------------------------------------------------------------
# Resource-limit budgets (issue #11) — "protect the host, not fine-tune the
# scroll": generous enough a legitimate scroll's normal work is never
# surprised, but bounded so a fork-bomb / memory-hog / cpu-hog cannot take the
# one shared host down (Kepler security review, forum #199 post 3868).
# ---------------------------------------------------------------------------
_RLIMIT_AS_BYTES = 1 * 1024 * 1024 * 1024      # 1 GiB address-space cap per process
_RLIMIT_CPU_SECONDS = DEFAULT_TIMEOUT_S + 10   # a few seconds of CPU-time slack past the wall-clock timeout
_RLIMIT_FSIZE_BYTES = 512 * 1024 * 1024        # 512 MiB max size for any single file the scroll writes
_RLIMIT_NPROC = 512                            # portable fork-bomb floor (Borges #26 security review; portable across Linux hosts, not enforced or validated on macOS, see README "Platform limits (hosting)" and umwelt#595): our
                                                # real deploy has no systemd Delegate=yes unit, so
                                                # _make_cgroup()'s pids.max is inert (cgroup_applied=False) —
                                                # this setrlimit-based floor applies regardless of cgroup
                                                # availability. NPROC counts per-*uid*, host-wide, not
                                                # per-run, so it's sized generously above any expected
                                                # legitimate concurrent load rather than matching
                                                # _CGROUP_PIDS_MAX's tighter per-run 64.

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_CGROUP_PIDS_MAX = 64                          # fork-bomb guard: total live processes in the run's cgroup
_CGROUP_MEMORY_MAX_BYTES = _RLIMIT_AS_BYTES    # match the setrlimit floor
_CGROUP_CPU_PERIOD_USEC = 100_000              # 100ms accounting period
_CGROUP_CPU_MAX_USEC = 200_000                 # 200ms/100ms == ~2 CPUs worth of ceiling

# ---------------------------------------------------------------------------
# Output cap (issue #13) — capped DURING the read, not sliced after the fact.
# ~1MB per stream: generous vs. the old [:20000]-char slice intent (most
# legitimate scroll output is far under this), while a scroll trying to print
# gigabytes is stopped from ever landing that much in engine RAM.
# ---------------------------------------------------------------------------
_OUTPUT_CAP_BYTES = 1_000_000


def _unshare_available() -> bool:
    """True if `unshare -rn` can create an unprivileged net+user namespace."""
    if shutil.which("unshare") is None:
        return False
    try:
        r = subprocess.run(
            ["unshare", "-rn", "true"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


@functools.lru_cache(maxsize=None)
def _fs_isolation_available() -> bool:
    """True if unshare -rnmpf + pivot_root entrypoint are available.

    Cached per process — the probe is slow (~50 ms); calling it once is enough.

    Uses a real temporary directory (not /tmp itself) as the probe work_dir:
    the entrypoint bind-mounts work_dir into the new root, and /tmp cannot be
    bind-mounted to a target that lives inside /tmp (circular reference — the
    target from tempfile.mkdtemp() would be under /tmp, making /tmp → /tmp/x
    an ancestor→descendant bind which the kernel refuses with EINVAL).
    """
    if shutil.which("unshare") is None:
        return False
    entrypoint = Path(__file__).parent / "sandbox_entrypoint.py"
    if not entrypoint.exists():
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="umwelt-probe-") as probe_dir:
            r = subprocess.run(
                ["unshare", "-rnmpf", "--",
                 sys.executable, str(entrypoint), probe_dir, "--probe"],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


@functools.lru_cache(maxsize=None)
def _cgroup_v2_base() -> Path | None:
    """The cgroup-v2 directory this process can create child cgroups under,
    or None if cgroup-v2 isn't usable here at all.

    Cached per process (same rationale as _fs_isolation_available above — the
    probe does real filesystem I/O; the answer doesn't change mid-process).

    Prefers the process's OWN cgroup (parsed from /proc/self/cgroup) over the
    filesystem root: systemd cgroup delegation (`Delegate=yes`) typically
    grants write access at a service unit's own cgroup path, not at
    /sys/fs/cgroup itself, so probing only the root under-detects delegation
    in the common deployed case. Falls back to the root for non-systemd hosts.
    Degrades to None (never raises) on any host where neither is writable —
    the setrlimit floor in _preexec_limits still applies regardless.
    """
    if not (_CGROUP_ROOT / "cgroup.controllers").exists():
        return None  # not a cgroup-v2 (unified) hierarchy

    candidates: list[Path] = []
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as f:
            for line in f:
                # unified (v2) hierarchy lines look like "0::/path/to/cgroup"
                if line.startswith("0::"):
                    rel = line.strip().split(":", 2)[2].lstrip("/")
                    candidates.append(_CGROUP_ROOT / rel if rel else _CGROUP_ROOT)
                    break
    except OSError:
        pass
    candidates.append(_CGROUP_ROOT)

    for base in candidates:
        probe = base / f"umwelt-cgprobe-{os.getpid()}"
        try:
            probe.mkdir()
            (probe / "pids.max").write_text("1")
        except OSError:
            try:
                probe.rmdir()
            except OSError:
                pass
            continue
        try:
            probe.rmdir()
        except OSError:
            pass
        return base
    return None


def _make_cgroup(run_id: str, pids_max: int = _CGROUP_PIDS_MAX) -> Path | None:
    """Create a per-run cgroup-v2 leaf with pids/memory/cpu caps applied.

    `pids_max` is injectable (not hardcoded) so a test/fixture can drive it
    deliberately low (e.g. 8) to verify the fork-bomb guard actually fires,
    without ever needing to run a real fork-bomb through the sandbox to prove
    it. memory.max/cpu.max stay at the module-constant production defaults —
    only pids_max was asked to be independently overridable.

    Returns None (never raises) if cgroup-v2 isn't usable — the setrlimit
    floor in _preexec_limits still applies regardless, so a missing cgroup
    guard degrades the run rather than breaking it.
    """
    base = _cgroup_v2_base()
    if base is None:
        return None
    cg_dir = base / f"umwelt-run-{run_id}"
    try:
        cg_dir.mkdir()
        (cg_dir / "pids.max").write_text(str(pids_max))
        (cg_dir / "memory.max").write_text(str(_CGROUP_MEMORY_MAX_BYTES))
        (cg_dir / "cpu.max").write_text(f"{_CGROUP_CPU_MAX_USEC} {_CGROUP_CPU_PERIOD_USEC}")
        return cg_dir
    except OSError:
        try:
            cg_dir.rmdir()
        except OSError:
            pass
        return None


def _cleanup_cgroup(cg_dir: Path | None) -> None:
    """Best-effort teardown of a per-run cgroup after the child has exited.

    A cgroup-v2 directory can't be rmdir'd while any process is still a
    member; by the time this runs, the child (and anything killpg reaped) is
    already dead, but reaping is not instantaneous — retry briefly.
    """
    if cg_dir is None:
        return
    for _ in range(10):
        try:
            cg_dir.rmdir()
            return
        except OSError:
            time.sleep(0.02)


def _preexec_limits(cg_dir: Path | None, nproc_max: int = _RLIMIT_NPROC):
    """Build the preexec_fn applied in the forked child, before exec.

    Runs on the real host process — before `unshare` (used on the
    fs_isolated and net-only paths) creates any new namespaces — so joining
    the cgroup and calling setrlimit here applies uniformly across all three
    isolation paths (fs_isolated, net-only, bare) that share this one call
    site. The limits survive the child's subsequent execve calls (unshare ->
    sandbox_entrypoint.py -> the scroll itself, or straight to the scroll on
    the bare path) since both rlimits and cgroup membership are inherited
    across exec.

    `nproc_max` is injectable (mirrors `pids_max`) so a test can drive
    RLIMIT_NPROC to a deliberately low value and verify the fork-bomb floor
    fires without ever running a real fork-bomb through the sandbox.
    """
    def _preexec() -> None:
        if cg_dir is not None:
            try:
                (cg_dir / "cgroup.procs").write_text(str(os.getpid()))
            except OSError:
                pass  # best-effort — the setrlimit floor below still applies
        if _resource is not None:
            try:
                _resource.setrlimit(_resource.RLIMIT_AS, (_RLIMIT_AS_BYTES, _RLIMIT_AS_BYTES))
                _resource.setrlimit(_resource.RLIMIT_CPU, (_RLIMIT_CPU_SECONDS, _RLIMIT_CPU_SECONDS))
                _resource.setrlimit(_resource.RLIMIT_FSIZE, (_RLIMIT_FSIZE_BYTES, _RLIMIT_FSIZE_BYTES))
                _resource.setrlimit(_resource.RLIMIT_NPROC, (nproc_max, nproc_max))
            except (ValueError, OSError):
                pass  # best-effort — never let a limit failure crash the child pre-exec
    return _preexec


def _stream_capped(pipe, cap: int) -> bytes:
    """Read a subprocess pipe to completion, keeping at most `cap` bytes.

    Meant to run in a background thread so stdout and stderr can be drained
    concurrently without deadlocking on a full pipe buffer. Draining
    continues past the cap (excess bytes are discarded, not buffered) so a
    chatty child that prints far more than the cap never blocks on a full
    pipe waiting for a reader that stopped reading — issue #13's fix must cap
    DURING the read, not slice the fully-buffered result after the fact.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = pipe.read(65536)
        if not chunk:
            break
        if total < cap:
            take = cap - total
            chunks.append(chunk[:take])
            total += min(len(chunk), take)
    return b"".join(chunks)


_SENTINEL_ENV_VAR = "_UMWELT_RUN_SENTINEL"


def _proc_scan_kill_by_sentinel(sentinel: str) -> None:
    """Best-effort sweep: SIGKILL any process whose /proc/<pid>/environ
    carries our run sentinel, regardless of process group or parent lineage.

    Why this exists (beyond os.killpg above): a scroll that double-forks AND
    calls os.setsid() in the intermediate child — the textbook daemonization
    idiom, not an exotic attack — deliberately creates a NEW session and
    process group, escaping the pgid os.killpg(proc.pid, ...) targets. It is
    also reparented away from this run's process tree the moment its
    immediate parent exits, so a ppid-chain walk from proc.pid misses it too.
    Verified empirically (2026-07-03): without this sweep, such an orphan
    survives killpg entirely, and the reader threads below block on the
    pipe's write end it still holds open — for as long as the orphan runs,
    which can be unboundedly long, well past timeout_s.

    The sentinel (set once per run, in the child's own env — see run_thing)
    is the one thing that survives both the session-group escape and the
    reparenting, since it's inherited across fork() unconditionally (unlike
    process-group/session membership, which setsid() explicitly changes) and
    across execve() as long as the scroll doesn't scrub its own environment.

    Best-effort and silent on every per-pid failure: proc entries can
    disappear mid-scan (already reaped), permissions can block a read, or the
    environ file can be empty (zombie, or a process that did scrub its env —
    in which case this sweep simply cannot reach it, matching the file's
    documented "best-effort, not a hardened jail" posture).
    """
    needle = f"{_SENTINEL_ENV_VAR}={sentinel}".encode()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return
    for pid_s in pids:
        try:
            with open(f"/proc/{pid_s}/environ", "rb") as f:
                environ_bytes = f.read()
        except OSError:
            continue
        if needle in environ_bytes:
            try:
                os.kill(int(pid_s), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _snapshot(root: Path) -> dict[str, float]:
    """Map of relpath -> mtime for every file under root (for world_diff)."""
    out: dict[str, float] = {}
    for p in root.rglob("*"):
        if p.is_file():
            try:
                out[str(p.relative_to(root))] = p.stat().st_mtime
            except OSError:
                pass
    return out


def run_thing(source: dict[str, str], exec_command: str,
              timeout_s: int = DEFAULT_TIMEOUT_S,
              _extra_env: dict[str, str] | None = None,
              _extra_path_dirs: list[str] | None = None,
              *,
              fs_isolated: bool = True,
              pids_max: int = _CGROUP_PIDS_MAX,
              nproc_max: int = _RLIMIT_NPROC,
              output_cap_bytes: int = _OUTPUT_CAP_BYTES,
              args: list[str] | None = None) -> dict[str, Any]:
    """Run exec_command against `source` (relpath -> contents) in a sandbox.

    fs_isolated=True (default): attempt full FS isolation via unshare -rnmpf +
    pivot_root (sandbox_entrypoint.py).  Falls back to unshare -rn (net-only)
    if the entrypoint is unavailable, then to bare execution.

    fs_isolated=False: skip FS isolation; use unshare -rn (net-only) or bare.

    NOTE: when fs_isolated=True, _extra_env is silently dropped — the
    sandbox_entrypoint builds a minimal fixed env (PATH, LANG, HOME, PYTHONDONTWRITEBYTECODE)
    and never inherits the parent env.  Only use _extra_env with fs_isolated=False
    (e.g. run_thing_capped, which passes WORLD_CAP_SOCK this way).

    Applies resource limits (issue #11: cgroup-v2 pids/memory/cpu when
    available + a portable setrlimit floor always; portable across Linux hosts,
    not enforced or validated on macOS, see README "Platform limits (hosting)"
    and umwelt#595), reaps the whole process
    group unconditionally after the direct child finishes — whether by normal
    exit or by the timeout_s wall clock (issue #12) — and streams stdout/stderr
    through a hard byte cap during the read rather than buffering in full
    (issue #13) — all at the single subprocess call site below, so every path
    (fs_isolated, net-only, bare, and run_thing_capped which delegates here)
    gets them.

    Four of these knobs are deliberately injectable rather than hardcoded
    module constants, so a test/containment fixture can drive them to
    deliberately low values and verify each boundary fires without ever
    running a real fork-bomb / multi-GB-output scroll through the sandbox:
      - `pids_max` — cgroup pids.max (issue #11's fork-bomb guard); inert
        when cgroup-v2 delegation isn't available (our real deploy today).
      - `nproc_max` — the portable (Linux; not validated on macOS) RLIMIT_NPROC floor (issue #11's fork-bomb
        guard when the cgroup guard above is inert — Borges's #26 security
        review finding).
      - `output_cap_bytes` — the streaming byte cap on stdout/stderr (issue #13).
      - `timeout_s` — already a pre-existing, caller-set parameter; it is also
        the knob that governs issue #12's orphan reap timing (both the
        TimeoutExpired-triggered kill AND, indirectly, how soon the
        unconditional post-wait group-sweep runs, since that sweep fires
        immediately once the direct child's wait() resolves — no additional
        hardcoded delay sits in between). No separate "reap timeout" constant
        exists in this implementation to expose.

    args (umwelt #57): an optional list of strings appended onto the
    shlex-split exec_command argv — `shlex.split(exec_command) + list(args
    or [])`. Argv-append ONLY: never string-interpolated into exec_command,
    never re-joined through a shell. Validation (shape, limits,
    readable-things-reject) lives at the route (server.py); this is a
    defensive assert only — a bad `args` here returns the sandbox's normal
    error shape, not an HTTP concern.

    Returns an outcome briefing:
      {stdout, stderr, exit_code, elapsed_s, timed_out, network_isolated,
       fs_isolated, world_diff: {created: [...], modified: [...]},
       resource_limits: {cgroup_applied, setrlimit_applied}}
    """
    if args is not None and not (
        isinstance(args, list) and all(isinstance(a, str) for a in args)
    ):
        return {"error": "args must be a list of strings", "exit_code": None}

    with tempfile.TemporaryDirectory(prefix="umwelt-use-") as td:
        work = Path(td)
        # Lay down the source tree.
        for relpath, contents in source.items():
            # Contain writes to the sandbox — reject path escapes.
            dest = (work / relpath).resolve()
            if not str(dest).startswith(str(work.resolve()) + os.sep):
                return {"error": f"source path escapes sandbox: {relpath!r}",
                        "exit_code": None}
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(contents, encoding="utf-8")

        before = _snapshot(work)

        env = dict(_SAFE_ENV)
        env["HOME"] = str(work)
        if _extra_env:
            env.update(_extra_env)
        if _extra_path_dirs:
            env["PATH"] = ":".join(_extra_path_dirs) + ":" + env["PATH"]
        # Run sentinel (issue #12's setsid-escape gap) — work.name is already
        # a unique per-run tempdir name; reuse it rather than mint a fresh id.
        # Silently dropped on the fs_isolated=True path (sandbox_entrypoint.py
        # builds its own fixed env for the scroll, same as _extra_env above) —
        # harmless there, since that path's real protection is the pid
        # namespace (PR #10), not this sweep.
        env[_SENTINEL_ENV_VAR] = work.name

        entrypoint = Path(__file__).parent / "sandbox_entrypoint.py"
        # argv-append ONLY (umwelt #57) — args are appended as literal argv
        # elements after the shlex-split exec_command, never string-
        # interpolated into exec_command and never re-joined through a
        # shell. shell=False is preserved throughout; a caller-supplied arg
        # like "; rm -rf /" arrives as one inert argv string.
        scroll_argv = shlex.split(exec_command) + list(args or [])

        fs_iso_active = False
        netns = False

        if fs_isolated and _fs_isolation_available():
            # Full FS isolation: mount namespace + pivot_root.
            # The entrypoint takes work_dir as its first argument and handles
            # exec'ing the scroll; cwd is set to /work inside the jail.
            argv = (
                ["unshare", "-rnmpf", "--",
                 sys.executable, str(entrypoint), str(work), "--"]
                + scroll_argv
            )
            fs_iso_active = True
            netns = True
            # When FS isolated, use the host work dir as cwd for the unshare
            # wrapper; the entrypoint will chdir to /work inside the jail.
            run_cwd = str(work)
        elif _unshare_available():
            # Net-only isolation: user+net namespace, no mount namespace.
            argv = ["unshare", "-rn", "--"] + scroll_argv
            netns = True
            run_cwd = str(work)
        else:
            argv = scroll_argv
            run_cwd = str(work)

        # Per-run cgroup (issue #11) — best-effort; None means "not available
        # here", in which case the setrlimit floor (applied unconditionally in
        # _preexec_limits below) is the only guard. run_id reuses the already-
        # unique tempdir name rather than minting a fresh id.
        run_id = work.name
        cg_dir = _make_cgroup(run_id, pids_max=pids_max)

        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.Popen(
                argv, cwd=run_cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
                preexec_fn=_preexec_limits(cg_dir, nproc_max=nproc_max),
            )
        except (OSError, ValueError) as e:
            _cleanup_cgroup(cg_dir)
            return {"error": f"failed to execute: {e}", "exit_code": None}

        # Stream stdout/stderr through the byte cap concurrently (issue #13)
        # — read in background threads so a chatty child never deadlocks on a
        # full pipe buffer while we wait on the other stream or on proc.wait().
        stdout_box: list[bytes] = []
        stderr_box: list[bytes] = []
        stdout_thread = threading.Thread(
            target=lambda: stdout_box.append(_stream_capped(proc.stdout, output_cap_bytes)),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=lambda: stderr_box.append(_stream_capped(proc.stderr, output_cap_bytes)),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Force-kill the direct child now; the group-wide sweep below
            # (which also runs on the non-timeout path) does the actual
            # orphan reaping, but the direct child itself may still be
            # running here and needs its own SIGKILL before we can wait() it.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()

        # Kill the WHOLE process group (issue #12), not just the direct
        # child, and do it unconditionally — not only when TimeoutExpired
        # fired. start_new_session=True above made this run's immediate
        # child the process group LEADER, so its pgid == its own pid
        # (verified: os.killpg(proc.pid, ...) still reaches any remaining
        # group member even after the leader itself has already exited and
        # been reaped — killpg targets the group id, not a live process).
        # This matters because a scroll that double-forks/backgrounds a
        # detached child (classic daemonizing WITHOUT calling its own
        # setsid()) has its direct process exit almost immediately — no
        # timeout ever fires — while the orphan keeps running in the same
        # process group. Gating the sweep on "only on timeout" would miss
        # exactly that case, so we always sweep here. Harmless on the
        # fs_isolated path too: that path's real protection is the pid
        # namespace (PR #10), which the kernel already tears down and reaps
        # on the wrapper's exit — killpg finds nothing left there (ESRCH,
        # swallowed below).
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # nothing left in the group, or we don't own the pgid

        # Supplementary sweep (issue #12's setsid-escape gap) — killpg above
        # cannot reach a scroll that calls os.setsid() while double-forking
        # (it deliberately escapes the process group and is reparented out of
        # this run's process tree). The sentinel env var set on the child
        # survives both escapes; sweep for it now, before the joins below,
        # so an escaped orphan's inherited pipe fd actually gets closed.
        _proc_scan_kill_by_sentinel(work.name)

        # Bounded join — never let an orphan we failed to reap (sentinel
        # scrubbed its own env, or some other escape this file's best-effort
        # posture doesn't cover) block this call forever. Verified empirically
        # (2026-07-03) that an unjoined setsid-escaping orphan holding the
        # pipe's write end open makes a plain, unbounded .join() hang past
        # timeout_s indefinitely — defeating the sandbox's core hard-timeout
        # guarantee. The grace period gives the sweep above a moment to take
        # effect (SIGKILL is not instant); if a thread is still alive after
        # it, we give up waiting rather than hang — the daemon thread lingers
        # harmlessly in the background (it will still exit once/if the pipe
        # ever does close) and we return with whatever output was captured.
        _JOIN_GRACE_S = 2.0
        stdout_thread.join(timeout=_JOIN_GRACE_S)
        stderr_thread.join(timeout=_JOIN_GRACE_S)
        stdout_stuck = stdout_thread.is_alive()
        stderr_stuck = stderr_thread.is_alive()
        reap_incomplete = stdout_stuck or stderr_stuck
        # umwelt#684: closing a pipe's file object from THIS thread while
        # its reader thread is still mid-.read() on the SAME object
        # deadlocks -- CPython's io.BufferedReader.close() and .read() both
        # need the object's internal lock, and a reader thread blocked
        # inside the OS read() syscall (waiting on an orphan that escaped
        # every reap mechanism above and still holds the write end open)
        # holds that lock for as long as it stays blocked. Reproduced
        # live: MainThread wedged here while both reader threads sat in
        # pipe.read() at the top of this file (Luria's field diagnosis,
        # independently reproduced by Kepler + a reviewer-fairy on
        # different machines, and deterministically here via an
        # execve()-escaped orphan -- see
        # test_execve_escaped_orphan_is_not_killable_but_run_thing_still_returns).
        # Only close a stream whose reader thread actually finished; a
        # still-alive reader keeps the exact "daemon thread lingers
        # harmlessly in the background" posture the comment above already
        # describes for the join itself -- its fd leaks for the life of
        # that thread instead of hanging this call forever.
        if not stdout_stuck:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if not stderr_stuck:
            try:
                proc.stderr.close()
            except OSError:
                pass
        _cleanup_cgroup(cg_dir)

        stdout = (stdout_box[0] if stdout_box else b"").decode("utf-8", errors="replace")
        stderr = (stderr_box[0] if stderr_box else b"").decode("utf-8", errors="replace")
        code = proc.returncode
        elapsed = round(time.monotonic() - start, 3)

        after = _snapshot(work)
        created = sorted(k for k in after if k not in before)
        modified = sorted(
            k for k in after if k in before and after[k] != before[k]
        )

        return {
            # Single enforcement point: `output_cap_bytes` (streamed/capped
            # DURING the read, above) is the only cap applied to stdout/stderr
            # now — the old fixed [:20000]-char re-slice was dropped here.
            # Keeping a second, independently-hardcoded 20000 constant on the
            # return value would silently defeat `output_cap_bytes` being
            # caller-overridable: a fixture setting a low cap to verify the
            # boundary fires would see its result re-truncated to 20000
            # regardless of what it configured, masking the very thing it's
            # testing. No existing caller depends on exactly 20000 chars
            # (server.py's _narrate_use only reads the first line, [:200]).
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": code,
            "elapsed_s": elapsed,
            "timed_out": timed_out,
            "network_isolated": netns,
            "fs_isolated": fs_iso_active,
            "world_diff": {"created": created, "modified": modified},
            "resource_limits": {
                "cgroup_applied": cg_dir is not None,
                "setrlimit_applied": _resource is not None,
                # True only in the residual case where killpg + the sentinel
                # /proc sweep both failed to reap something holding a stdout/
                # stderr pipe fd open within the join grace period — e.g. a
                # scroll that escaped its process group AND scrubbed its own
                # environment. Introspectable rather than silent so a caller
                # can tell "this run may have left an unreaped process
                # running" apart from the ordinary clean-exit case.
                #
                # NOT an unbounded leak (Borges, PR #26 review): a surviving
                # process is still bounded by the setrlimit floor applied in
                # _preexec_limits, which the sentinel-scrubbing escape above
                # cannot avoid (rlimits are inherited across fork/exec
                # unconditionally, unlike the sentinel env var, which a
                # scroll's own os.environ.clear() could remove). It self-
                # terminates at RLIMIT_CPU's budget (~timeout_s+10 CPU-
                # seconds) with its memory capped at RLIMIT_AS regardless.
                "reap_incomplete": reap_incomplete,
            },
        }


def run_thing_capped(source: dict[str, str], exec_command: str,
                     timeout_s: int = DEFAULT_TIMEOUT_S,
                     db_path: str | None = None,
                     args: list[str] | None = None,
                     caller: str | None = None,
                     sprite_dir: str | Path | None = None,
                     art_service_url: str | None = None) -> dict[str, Any]:
    """Like run_thing, but with the capability channel enabled.

    Creates an ephemeral UDS at a temp path, starts a CapHandler listening on
    it, injects WORLD_CAP_SOCK and the `world` CLI onto PATH, then tears the
    handler down after the sandbox exits.

    args (umwelt #57): passed straight through to run_thing — see its
    docstring for the argv-append contract. Validation lives at the route.

    caller (umwelt #186): the agent identity of the `use()` invocation that
    spawned this sandbox run — threaded straight through to CapHandler,
    which is the only place it's consulted (the advance_ref capability's
    owner gate). No new identity plumbing: it's the same `agent` value
    already in scope at server.py's one run_thing_capped call site.

    sprite_dir/art_service_url (umwelt#464 slice 2): threaded straight
    through to CapHandler, gating the generate/generate_status ops the same
    way caller gates advance_ref — both None (the default) means generation
    is not configured on this engine.

    v1 FS-isolation limitation: this function passes fs_isolated=False to
    run_thing.  The cap socket lives in a separate temp dir (cap_dir) that is
    outside the scroll's work tmpdir; after pivot_root, cap_dir would be
    inaccessible, breaking the capability channel.  Full FS isolation for
    capped runs (socket-in-jail) is deferred to v2 (umwelt issue #14).  In the
    meantime, fixture (b) tests b1 and b3 remain xfail for run_thing_capped.
    See the ⚠ v1 FS-isolation gap section in PROTOCOL.md for the ship posture.
    """
    if db_path is None:
        return run_thing(source, exec_command, timeout_s, args=args)

    from . import cap_handler as _ch

    with tempfile.TemporaryDirectory(prefix="umwelt-cap-") as cap_dir:
        cap_dir_p = Path(cap_dir)
        sock_path = str(cap_dir_p / "cap.sock")

        # Install the `world` CLI into a temp bin dir.
        bin_dir = cap_dir_p / "bin"
        bin_dir.mkdir()
        world_cli_src = Path(__file__).parent / "world_cli.py"
        world_cli_dst = bin_dir / "world"
        world_cli_dst.write_bytes(world_cli_src.read_bytes())
        world_cli_dst.chmod(0o755)

        handler = _ch.CapHandler(sock_path, db_path, caller=caller,
                                  sprite_dir=sprite_dir,
                                  art_service_url=art_service_url)
        handler.start()
        try:
            # v1: socket not in jail — the cap socket lives in cap_dir (outside
            # the scroll's work tmpdir) and would be inaccessible after pivot_root.
            # Pass fs_isolated=False until the socket-in-jail improvement lands.
            outcome = run_thing(
                source, exec_command, timeout_s,
                _extra_env={"WORLD_CAP_SOCK": sock_path},
                _extra_path_dirs=[str(bin_dir)],
                fs_isolated=False,
                args=args,
            )
        finally:
            handler.stop()

    return outcome
