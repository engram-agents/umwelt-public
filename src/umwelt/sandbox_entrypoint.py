"""Filesystem-isolation entrypoint — runs INSIDE the unshared namespaces.

Invoked by sandbox.py as:
    unshare -rnmpf -- python3 sandbox_entrypoint.py <work_dir> [--probe] [--no-seccomp] -- <cmd> [args...]

Performs pivot_root into a minimal tmpfs, bind-mounting only the scroll's
work dir (read-write) and essential system paths (read-only).  After the
pivot, the host filesystem is invisible to the child process — /etc/passwd,
/tmp, and all other host paths are gone from the new root.

Two-axis confinement:
  Axis 1 — FS-reachability: pivot_root removes all host paths outside /work
    from the scroll's view.  Fixture (b) tests this axis (b1: /etc/passwd
    blocked; b3: CAS dir write blocked).  Fixture (b)-green proves FS-
    reachability confinement only — the scroll cannot reach host filesystem
    paths outside its work dir.
  Axis 2 — kernel-escape resistance: seccomp denylists mount/ptrace/bpf/etc.
    Fixture (b)-green does NOT prove kernel-escape resistance; that is the
    seccomp layer's job.  Both axes are required for full confinement.

Syscall 155 is pivot_root on x86_64. A platform.machine() guard should be
added for portability (v1 is x86_64/WSL2 only — documented).

libseccomp is used via ctypes only (no pip dependency); its absence degrades
gracefully (seccomp layer is skipped, FS isolation via pivot_root still holds).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import resource
import sys
import tempfile


# ---------------------------------------------------------------------------
# Mount flag constants (from <sys/mount.h>)
# ---------------------------------------------------------------------------

MS_RDONLY  = 1
MS_NOSUID  = 2
MS_NODEV   = 4
MS_REMOUNT = 32
MS_BIND    = 4096
MS_REC     = 16384   # recursive — required for bind-mounts of subtrees with sub-mounts
MNT_DETACH = 2


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _mount(source: bytes | None, target: bytes,
           fs_type: bytes | None, flags: int,
           data: bytes | None = None) -> int:
    libc = _libc()
    return libc.mount(
        source if source is not None else None,
        target,
        fs_type if fs_type is not None else None,
        ctypes.c_ulong(flags),
        data if data is not None else None,
    )


def _pivot_root(new_root: bytes, put_old: bytes) -> int:
    # syscall 155 = pivot_root on x86_64.
    # A platform.machine() guard should be added for portability (v1 x86_64 only).
    libc = _libc()
    return libc.syscall(155, new_root, put_old)


def _umount2(target: bytes, flags: int) -> int:
    libc = _libc()
    return libc.umount2(target, ctypes.c_int(flags))


# ---------------------------------------------------------------------------
# Seccomp denylist (Axis 2 — kernel-escape resistance)
# ---------------------------------------------------------------------------

def _apply_seccomp() -> None:
    """Apply a seccomp denylist blocking high-risk syscalls.

    Degrades gracefully if libseccomp.so.2 is unavailable — FS isolation via
    pivot_root (Axis 1) still holds; seccomp is the Axis 2 mitigation against
    kernel-escape paths that pivot_root alone does not block.
    """
    try:
        lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError:
        return  # libseccomp not available; degrade gracefully

    SCMP_ACT_ALLOW        = 0x7fff0000
    # KILL_PROCESS (not KILL_THREAD): terminate the whole process on violation,
    # closing the window where a spawned native thread (via ctypes) attempts a
    # denied syscall while the rest of the process stays alive.
    SCMP_ACT_KILL_PROCESS = 0x80000000

    ctx = lib.seccomp_init(ctypes.c_uint32(SCMP_ACT_ALLOW))
    if not ctx:
        return

    _DENIED = [
        "mount", "umount2", "pivot_root", "unshare", "setns",
        "ptrace", "bpf", "perf_event_open", "keyctl",
        "kexec_load", "process_vm_readv", "process_vm_writev",
        # io_uring runs file+network ops through a ring buffer that bypasses the
        # syscall filter; without these, the denylist has a hole the size of
        # the whole filter (Kepler security review, 2026-07-01).
        "io_uring_setup", "io_uring_enter", "io_uring_register",
    ]
    for name in _DENIED:
        syscall_num = lib.seccomp_syscall_resolve_name(name.encode())
        if syscall_num == -1:
            continue
        lib.seccomp_rule_add(
            ctx,
            ctypes.c_uint32(SCMP_ACT_KILL_PROCESS),
            ctypes.c_int(syscall_num),
            0,
        )

    rc = lib.seccomp_load(ctx)
    if rc < 0:
        print(f"warning: seccomp_load failed (rc={rc}); Axis 2 (kernel-escape resistance) inactive",
              file=sys.stderr)
    lib.seccomp_release(ctx)


# ---------------------------------------------------------------------------
# Main setup routine
# ---------------------------------------------------------------------------

def _setup_jail(work_dir: str, no_seccomp: bool) -> None:
    """Build the minimal tmpfs root and pivot_root into it.

    The new root contains:
      /work    — bind-mount of work_dir (read-write; the scroll's only writable space)
      /usr     — bind-mount of host /usr (read-only, nosuid, nodev)  ─┐
      /lib     — bind-mount of host /lib (if exists)                   │ system paths;
      /lib64   — bind-mount of host /lib64 (if exists)                 │ MS_BIND|MS_REC
      /bin     — bind-mount of host /bin (if exists)                   │ required to
      /sbin    — bind-mount of host /sbin (if exists)                 ─┘ pull submounts
      /dev     — minimal device nodes (null, zero, urandom, random)
      /proc    — fresh proc (best-effort)

    Notably absent: /tmp, /etc, /home, /root, /var.  Scrolls that need temp
    space must use /work (the bind-mounted work dir) or a subdirectory.
    """

    # Step 2 — create the new root on the host FS (before pivot_root, where
    # host FS is still mounted).
    new_root = tempfile.mkdtemp()
    new_root_b = new_root.encode()

    # Step 3 — mount a tmpfs at new_root.
    # size=512m caps the root tmpfs so a scroll can't exhaust host RAM by
    # filling /, even before cgroup memory limits land (Kepler review, 2026-07-01).
    rc = _mount(b"tmpfs", new_root_b, b"tmpfs", 0, b"size=512m")
    if rc != 0:
        raise RuntimeError(f"mount tmpfs failed: errno={ctypes.get_errno()}")

    # Step 4 — create directories INSIDE the fresh tmpfs (order: after mount).
    os.makedirs(new_root + "/work")
    os.makedirs(new_root + "/old-root")
    # NOTE: /tmp is intentionally NOT created.  After pivot_root, /tmp does not
    # exist in the jail.  Scrolls that need temp space must use /work.  This
    # prevents resource exhaustion and keeps the "one writable space" contract
    # clear (the test_fs_isolated_cannot_write_outside_work test relies on this).

    # System path stubs — only create if the host path exists (some minimal
    # containers omit /sbin, /lib64, etc.).
    _SYSTEM_PATHS = ["/usr", "/lib", "/lib64", "/bin", "/sbin"]
    for p in _SYSTEM_PATHS:
        if os.path.exists(p):
            os.makedirs(new_root + p, exist_ok=True)

    os.makedirs(new_root + "/dev")
    os.makedirs(new_root + "/proc")

    # Step 5 — bind-mount the scroll's work dir (read-write) at /work.
    work_b = work_dir.encode()
    dst_work_b = (new_root + "/work").encode()
    rc = _mount(work_b, dst_work_b, None, MS_BIND, None)
    if rc != 0:
        raise RuntimeError(f"bind-mount work failed: errno={ctypes.get_errno()}")

    # Step 6 — bind-mount system paths read-only (nosuid, nodev).
    # MS_REC is required: system paths like /usr have sub-mounts and bind
    # without MS_REC fails with EINVAL on this WSL2/ext4 layout.
    for p in _SYSTEM_PATHS:
        if not os.path.exists(p):
            continue
        host_b = p.encode()
        dst_b  = (new_root + p).encode()
        rc = _mount(host_b, dst_b, None, MS_BIND | MS_REC, None)
        if rc != 0:
            # Non-fatal: log and continue (some paths may be unavailable in
            # container environments).
            print(f"warning: bind-mount {p} failed (errno={ctypes.get_errno()}); skipping",
                  file=sys.stderr)
            continue
        rc = _mount(host_b, dst_b, None,
                    MS_REMOUNT | MS_BIND | MS_RDONLY | MS_NOSUID | MS_NODEV, None)
        if rc != 0:
            print(f"warning: remount {p} read-only failed (errno={ctypes.get_errno()}); "
                  "continuing with read-write bind (less secure)", file=sys.stderr)

    # Step 7 — bind-mount minimal /dev devices.
    _DEV_NODES = ["null", "zero", "urandom", "random"]
    for name in _DEV_NODES:
        host_dev = f"/dev/{name}"
        new_dev  = f"{new_root}/dev/{name}"
        if not os.path.exists(host_dev):
            continue
        # Create the mount target (a regular file — devices bind-mount onto files).
        with open(new_dev, "wb"):
            pass
        rc = _mount(host_dev.encode(), new_dev.encode(), None, MS_BIND, None)
        if rc != 0:
            print(f"warning: bind-mount /dev/{name} failed (errno={ctypes.get_errno()}); "
                  "skipping", file=sys.stderr)

    # Step 8 — pivot_root: new_root becomes /, old root goes to /old-root.
    put_old_b = (new_root + "/old-root").encode()
    rc = _pivot_root(new_root_b, put_old_b)
    if rc != 0:
        raise RuntimeError(f"pivot_root failed: errno={ctypes.get_errno()}")

    # Step 9 — chdir to new /.
    os.chdir("/")

    # Step 10 — unmount (detach) the old root.
    rc = _umount2(b"/old-root", MNT_DETACH)
    if rc != 0:
        print(f"warning: umount2 /old-root failed (errno={ctypes.get_errno()})",
              file=sys.stderr)

    # Step 11 — remove the now-empty /old-root mount point (best-effort).
    try:
        os.rmdir("/old-root")
    except OSError:
        pass

    # Step 12 — mount a fresh proc (best-effort; some containers block this).
    try:
        rc = _mount(b"proc", b"/proc", b"proc", 0, None)
        if rc != 0:
            print(f"info: proc mount failed (errno={ctypes.get_errno()}); skipping",
                  file=sys.stderr)
    except OSError as e:
        print(f"info: proc mount skipped: {e}", file=sys.stderr)

    # Step 13 — apply seccomp denylist (Axis 2 — kernel-escape resistance).
    if not no_seccomp:
        _apply_seccomp()

    # Step 14 — close inherited file descriptors ≥ 3 (fd hygiene).
    soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    for fd in range(3, min(soft_limit, 1024)):
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Step 1 — parse args.
    # Format: <work_dir> [--probe] [--no-seccomp] -- <cmd> [args...]
    # Split on '--' first to separate our flags from the scroll command.
    argv = sys.argv[1:]

    # Find the '--' separator.
    try:
        sep_idx = argv.index("--")
    except ValueError:
        sep_idx = len(argv)

    our_args = argv[:sep_idx]
    scroll_argv = argv[sep_idx + 1:]

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("work_dir")
    parser.add_argument("--probe",      action="store_true")
    parser.add_argument("--no-seccomp", action="store_true")
    parsed = parser.parse_args(our_args)

    work_dir   = parsed.work_dir
    probe      = parsed.probe
    no_seccomp = parsed.no_seccomp

    try:
        _setup_jail(work_dir, no_seccomp)
    except Exception as e:
        print(f"sandbox_entrypoint: setup failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Step 15 — probe mode: just verify setup works, then exit.
    if probe:
        print("ok")
        sys.exit(0)

    if not scroll_argv:
        print("sandbox_entrypoint: no command given after --", file=sys.stderr)
        sys.exit(1)

    # Step 16 — set HOME to /work (the scroll's working dir inside new root).
    # Build a minimal safe environment matching sandbox.py's _SAFE_ENV.
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": "/work",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    # Step 17 — exec the scroll command (cwd is /work from _setup_jail).
    os.chdir("/work")
    try:
        os.execvpe(scroll_argv[0], scroll_argv, env)
    except OSError as e:
        print(f"sandbox_entrypoint: exec failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
