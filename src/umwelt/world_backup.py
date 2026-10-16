"""Scheduled backup of the prod world DB (umwelt #922).

    python -m umwelt.world_backup [--db P] [--backup-dir D] [--keep N] [--stamp S]
    python -m umwelt.world_backup --alarm [--url U] [--agent A] [--to-agent B]
    (sender: $UMWELT_BACKUP_ALARM_FROM, else the generic "world-backup";
     recipient: $UMWELT_BACKUP_ALARM_TO, else empty = unaddressed crossing;
     destination place id: $UMWELT_BACKUP_ALARM_PLACE, else place_office)

Contract (pinned by tests/test_world_backup.py):

* The copy is SQLite's online backup API, never a file copy (prod runs a
  rollback journal; a `cp` mid-write can capture a torn file).
* `PRAGMA quick_check` runs ON THE COPY; a copy that fails is deleted and the
  run returns non-zero.
* Copies are named `umwelt-YYYYmmddTHHMMSSZ.db` (UTC). Keep N by count; prune
  only AFTER the new copy verified, and only files matching that exact name
  pattern. keep < 1 is refused inside run(): nothing written, nothing pruned.
* The last-ok stamp {at, path, bytes} is written atomically (temp + replace),
  only on verified success. A failure never touches it.
* `/health` (server.py) reads the stamp on every request.
* `alarm()` posts ONE addressed crossing; a failed post returns non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^umwelt-\d{8}T\d{6}Z\.db$")
_TMP_PREFIX = ".umwelt-backup-tmp-"
# Wall-clock bound on the online copy. A source held under an exclusive lock
# makes Connection.backup retry forever with no error, so a hang would defeat
# OnFailure=. The world-backup systemd unit's (internal deploy file, not in this release) TimeoutStartSec must stay
# strictly larger (pinned by a test): this is the primary bound, that the
# backstop.
DEFAULT_DEADLINE_S = 600
_BACKUP_PAGES = 256
_BACKUP_SLEEP_S = 0.05
_SOURCE_CONNECT_TIMEOUT_S = 5.0


class _DeadlineExceeded(Exception):
    pass


_STAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"
_TMP_RE = re.compile(r"^" + re.escape(_TMP_PREFIX) + r"(\d+)\.db$")


def _rm(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError, OSError):
        return True  # cannot tell: treat as alive, never delete
    return True


def _sweep_stale_tmp(backup_dir: str) -> None:
    """Remove tmp copies orphaned by a killed run (dead pid only)."""
    # Best-effort: the sweep must never be the reason a backup does not run.
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return
    for n in names:
        m = _TMP_RE.match(n)
        if not m:
            continue
        try:
            if not _pid_alive(int(m.group(1))):
                _rm(os.path.join(backup_dir, n))
        except (OverflowError, ValueError, OSError):
            continue


def _unsafe_dir_reason(path: str):
    """Why `path` (checked as a LEAF: ancestor symlinks are fine) is not a
    safe place for tokens-bearing files, or None if it is safe."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return f"cannot stat: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return "is a symlink"
    if not stat.S_ISDIR(st.st_mode):
        return "is not a directory"
    if st.st_uid != os.geteuid():
        return "is not owned by the running user (owner mismatch)"
    if st.st_mode & 0o022:
        return "is group- or world-writable"
    return None


def _write_stamp(stamp_path: str, at: datetime, path: str, nbytes: int) -> None:
    d = os.path.dirname(os.path.abspath(stamp_path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = stamp_path + ".tmp"
    # A stale temp (killed run) or a planted one: unlink removes a symlink
    # LINK, never its target; then create exclusively and without following.
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"at": at.strftime(_STAMP_FMT), "path": path,
                   "bytes": nbytes}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, stamp_path)


def run(db_path, backup_dir, *, keep=14, stamp_path=None, now=None,
        deadline_s=DEFAULT_DEADLINE_S) -> int:
    """Back up db_path into backup_dir. Returns 0 on a verified copy."""
    if keep < 1:
        print(f"world_backup: refusing keep={keep} (must be >= 1)",
              file=sys.stderr)
        return 2
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    backup_dir = str(backup_dir)
    # Backups hold agent_sessions tokens in plaintext: create the dir 0700
    # (a pre-existing dir's mode is the operator's, left alone).
    try:
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    except OSError as exc:
        print(f"world_backup: unsafe backup dir {backup_dir}: {exc}",
              file=sys.stderr)
        return 1
    # Fail closed on a pre-existing dir another user could write into or
    # redirect (symlink): nothing is written, pruned or stamped.
    why = _unsafe_dir_reason(backup_dir)
    if why:
        print(f"world_backup: unsafe backup dir {backup_dir}: {why}; refusing",
              file=sys.stderr)
        return 1
    if stamp_path is None:
        stamp_path = os.path.join(backup_dir, ".last-ok.json")
    else:
        sdir = os.path.dirname(os.path.abspath(str(stamp_path)))
        if os.path.lexists(sdir) and not os.path.samefile(sdir, backup_dir):
            why = _unsafe_dir_reason(sdir)
            if why:
                print(f"world_backup: unsafe stamp dir {sdir}: {why}; "
                      "refusing", file=sys.stderr)
                return 1
    _sweep_stale_tmp(backup_dir)
    name = "umwelt-" + now.strftime("%Y%m%dT%H%M%SZ") + ".db"
    final = os.path.join(backup_dir, name)
    tmp = os.path.join(backup_dir, f"{_TMP_PREFIX}{os.getpid()}.db")
    _rm(tmp)

    # 1. online backup into a temp name (never a matching name until verified)
    src = dst = None
    try:
        src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                              uri=True, timeout=_SOURCE_CONNECT_TIMEOUT_S)
        # The backup TARGET is opened as a bare Connection (same object
        # connect() returns); the verification below uses connect() so the
        # quick_check is a separate, fresh open of the finished file.
        # Pre-create the tmp 0600 so the in-progress copy of agent_sessions
        # tokens is never world-readable (an empty file is a valid target).
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        dst = sqlite3.Connection(tmp)
        limit = time.monotonic() + deadline_s

        def _check(status, remaining, total):
            # Fires on every busy-retry too, so it is a real deadline hook.
            if time.monotonic() > limit:
                raise _DeadlineExceeded
        src.backup(dst, pages=_BACKUP_PAGES, progress=_check,
                   sleep=_BACKUP_SLEEP_S)
    except _DeadlineExceeded:
        print(f"world_backup: backup did not finish within {deadline_s}s "
              "(source locked?); aborted", file=sys.stderr)
        for c in (dst, src):
            if c is not None:
                c.close()
        _rm(tmp)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"world_backup: backup failed: {exc}", file=sys.stderr)
        for c in (dst, src):
            if c is not None:
                c.close()
        _rm(tmp)
        return 1
    dst.close()
    src.close()

    # 2. quick_check ON THE COPY
    try:
        chk = sqlite3.connect(tmp)
        try:
            result = chk.execute("PRAGMA quick_check").fetchone()
        finally:
            chk.close()
        ok = result is not None and result[0] == "ok"
    except sqlite3.Error as exc:
        print(f"world_backup: quick_check error: {exc}", file=sys.stderr)
        ok = False
    if not ok:
        print("world_backup: quick_check failed on the copy; discarded",
              file=sys.stderr)
        _rm(tmp)
        return 1

    # 3. publish, stamp, then prune (prune only after a verified copy)
    os.chmod(tmp, 0o600)
    os.replace(tmp, final)
    stamp_err = None
    try:
        _write_stamp(stamp_path, now, final, os.path.getsize(final))
    except OSError as exc:
        stamp_err = exc
    names = sorted(n for n in os.listdir(backup_dir) if NAME_RE.match(n))
    for old in (names[:-keep] if len(names) > keep else []):
        _rm(os.path.join(backup_dir, old))
    if stamp_err is not None:
        print(f"world_backup: copy verified ({final}) but the last-ok stamp "
              f"could not be written: {stamp_err}", file=sys.stderr)
        return 1
    print(f"world_backup: ok {final}")
    return 0


def alarm(world_url, *, agent, to_agent, destination, message,
          opener=None, mentions=None) -> int:
    """Post ONE crossing (addressed if to_agent is non-empty, else
    unaddressed). Non-zero if it could not be posted; never skips the post.
    `mentions` (optional list of agent names) is sent as the payload's
    "mentions" field only when non-empty; otherwise the payload is unchanged."""
    opener = opener or urllib.request.urlopen
    payload = {
        "agent": agent,
        "destination": destination,
        "sent_at": datetime.now(timezone.utc).strftime(_STAMP_FMT),
        "body": message,
    }
    if mentions:
        payload["mentions"] = list(mentions)
    if to_agent:
        payload["to_agent"] = to_agent
    else:
        print("NOTE: UMWELT_BACKUP_ALARM_TO is empty; posting an "
              "unaddressed crossing", file=sys.stderr)
    req = urllib.request.Request(
        world_url.rstrip("/") + "/umwelt/send_crossing",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with opener(req, timeout=15) as resp:
            status = getattr(resp, "status", 200)
        if not 200 <= int(status) < 300:
            print(f"world_backup: alarm post got HTTP {status}",
                  file=sys.stderr)
            return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"world_backup: alarm post failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _env_deadline():
    raw = os.environ.get("UMWELT_BACKUP_TIMEOUT_S")
    try:
        return float(raw) if raw else DEFAULT_DEADLINE_S
    except ValueError:
        return DEFAULT_DEADLINE_S


def main(argv=None) -> int:
    home = os.path.expanduser("~/.umwelt")
    p = argparse.ArgumentParser(prog="umwelt.world_backup",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--db", default=os.path.join(home, "umwelt.db"))
    p.add_argument("--backup-dir", default=os.path.join(home, "backups"))
    p.add_argument("--keep", type=int, default=14)
    p.add_argument("--stamp", default=None,
                   help="last-ok stamp path (default: <backup-dir>/.last-ok.json)")
    p.add_argument("--timeout", type=float, dest="deadline_s",
                   default=_env_deadline(),
                   help="seconds before an unfinished copy is aborted "
                        "(default $UMWELT_BACKUP_TIMEOUT_S or "
                        f"{DEFAULT_DEADLINE_S})")
    p.add_argument("--alarm", action="store_true",
                   help="post a failure crossing instead of backing up")
    # umwelt#678/#699: no hardcoded world URL here; resolved through the one
    # resolver (explicit > UMWELT_URL > ~/.umwelt/config.json > default).
    p.add_argument("--url", default=None)
    # Sender: a generic service name unless the install-time drop-in sets one.
    p.add_argument("--agent",
                   default=os.environ.get("UMWELT_BACKUP_ALARM_FROM")
                   or "world-backup")
    # Recipient comes from the install-time drop-in; empty = unaddressed.
    p.add_argument("--to-agent",
                   default=os.environ.get("UMWELT_BACKUP_ALARM_TO", ""))
    # A world PLACE id (not an agent); the issue names place_office.
    p.add_argument("--destination",
                   default=os.environ.get("UMWELT_BACKUP_ALARM_PLACE")
                   or "place_office")
    p.add_argument("--message",
                   default="umwelt world-DB backup FAILED -- see "
                           "`journalctl --user -u umwelt-world-backup.service`")
    a = p.parse_args(argv)
    if a.alarm:
        from umwelt import client_auth
        a.url = client_auth.resolve_world_url(explicit=a.url)
        return alarm(a.url, agent=a.agent, to_agent=a.to_agent,
                     destination=a.destination, message=a.message)
    if a.deadline_s <= 0:
        p.error("--timeout must be > 0")
    return run(a.db, a.backup_dir, keep=a.keep, stamp_path=a.stamp,
               deadline_s=a.deadline_s)


if __name__ == "__main__":
    sys.exit(main())
