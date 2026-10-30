"""World GC — the engine-only lifecycle advancer (umwelt #28).

Periodic job (run_gc) advances the removal lifecycle by time alone:

    binned --(older than T1)--> landfilled --(older than T2)--> dissolved

This module is the ONLY caller of db.dissolve_thing and the only place that
removes a thing's on-disk source dir or reclaims CAS blobs. Permanent
destruction is never an agent's act (Lei/Ariadne, "the world's slow,
time-driven, recoverable decay") — dissolve is not an HTTP route, not an MCP
tool, just a function this module calls on a schedule.

Run standalone (engine side, not inside a sandbox):
    python -m umwelt.gc --db ~/.umwelt/umwelt.db [--t1-days 7] [--t2-days 30]

An internal systemd --user daily-run pattern exists (a service + timer pair,
not in this release; not installed/enabled by this change).
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cas
from . import db
from . import server

# Matches a CAS ref embedded anywhere in free text (things.metadata JSON,
# crossings.body/body_ref, graft_advances.old_ref/new_ref) — see
# _referenced_cas_hashes for why this is deliberately a loose text scan
# rather than a structured-field lookup.
_CAS_REF_RE = re.compile(r"cas:sha256:([0-9a-f]{64})")

# umwelt#464 slice 2: retention window for settled (done/error)
# generate_jobs rows. Separate constant from t1/t2_days above -- this sweep
# is keyed on updated_at, not the binned/landfilled lifecycle those two
# govern, and has no reason to share their default.
GENERATE_JOB_RETENTION_DAYS = 7


def _cutoff_iso(days: int) -> str:
    """ISO-UTC timestamp `days` ago, in the same microsecond format db.now_iso
    uses — so a plain string comparison against binned_at/landfilled_at
    (both stamped by now_iso) sorts correctly."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _remove_source_dir(things_dir: Path, thing_id: str) -> None:
    d = things_dir / thing_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def _referenced_cas_hashes(conn: sqlite3.Connection) -> set[str]:
    """Conservative reference scan for the CAS mark-and-sweep.

    Scope (deliberately explicit — see the CAS-sweep judgment call in the
    PR's handoff): scans the reference surfaces that actually exist in this
    schema today —
      - things.metadata (free-form JSON; an agent-declared blob ref could
        live anywhere in it — this is a raw text scan, not a JSON-schema
        lookup, on purpose: metadata has no fixed shape for where a ref
        would sit)
      - crossings.body_ref (the dedicated CAS-pointer column) and
        crossings.body (defensive — inline text could itself mention a ref)
      - graft_advances.old_ref/new_ref (umwelt#430 — advance_ref moves a
        capsule's current ref out of things.metadata as it advances, so the
        version chain's prior refs would otherwise be invisible to this scan
        the moment a later advance replaces them; scanning both columns of
        every row is what keeps the retained-history blobs alive)
    There is NO persisted "scrolls" table in this codebase (a scroll is an
    ephemeral sandbox invocation, not a stored row) — the db.py comment
    anticipating "things/crossings/scrolls" as the three reference classes
    has nothing to scan for the third one. Noted here explicitly rather than
    silently skipped: if a future scrolls-persistence layer lands, it needs
    its own line in this scan.

    "Conservative" cuts the direction that matters: a false positive (an
    orphaned blob we mistakenly think is still referenced) just delays
    reclaim by one more dissolve; a false negative (a live reference we
    fail to see) would delete a blob still in use. This scan is built to
    over-count references, never under-count them.
    """
    referenced: set[str] = set()
    for (metadata,) in conn.execute("SELECT metadata FROM things"):
        if metadata:
            referenced.update(_CAS_REF_RE.findall(metadata))
    for body_ref, body in conn.execute("SELECT body_ref, body FROM crossings"):
        for text in (body_ref, body):
            if text:
                referenced.update(_CAS_REF_RE.findall(text))
    for old_ref, new_ref in conn.execute(
            "SELECT old_ref, new_ref FROM graft_advances"):
        for text in (old_ref, new_ref):
            if text:
                referenced.update(_CAS_REF_RE.findall(text))
    return referenced


def dissolve_thing(conn: sqlite3.Connection, things_dir: Path, db_path: str,
                    thing_id: str) -> None:
    """Fully dissolve a thing: DB row + on-disk source dir + orphaned CAS blobs.

    Engine-only, same as db.dissolve_thing — never reachable via a route or
    MCP tool. Order matters: the row is deleted FIRST, so the reference scan
    below no longer counts this thing's own (now-gone) metadata as a
    reference to its own blobs — otherwise a thing referencing only its own
    blob could never have that blob reclaimed.
    """
    db.dissolve_thing(conn, thing_id)
    _remove_source_dir(things_dir, thing_id)

    referenced = _referenced_cas_hashes(conn)
    all_hashes = [r[0] for r in conn.execute("SELECT hash FROM cas_entries")]
    orphaned = [h for h in all_hashes if h not in referenced]
    for h in orphaned:
        path = cas.cas_dir(db_path) / h[:2] / h[2:]
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        conn.execute("DELETE FROM cas_entries WHERE hash = ?", (h,))
    conn.commit()


def _describe(t: dict) -> dict:
    return {"id": t["id"], "name": t["name"], "owner": t["author"]}


def _run_gc_impl(conn, things_dir, db_path, t1_days, t2_days, described):
    """Body of run_gc. `described` is None (plain run) or a
    {"landfilled": [], "dissolved": []} dict this fills with
    {"id","name","owner"} per moved thing (umwelt #929)."""
    things_dir = Path(things_dir)

    landfilled: list[str] = []
    for t in db.things_due_landfill(conn, _cutoff_iso(t1_days)):
        db.landfill_thing(conn, t["id"])
        landfilled.append(t["id"])
        if described is not None:
            described["landfilled"].append(_describe(t))

    dissolved: list[str] = []
    for t in db.things_due_dissolve(conn, _cutoff_iso(t2_days)):
        # `t` was read BEFORE the row goes away: name + owner are captured
        # here because dissolve_thing deletes the row (a later lookup would
        # find nothing).
        if described is not None:
            described["dissolved"].append(_describe(t))
        dissolve_thing(conn, things_dir, db_path, t["id"])
        dissolved.append(t["id"])

    # umwelt#464 slice 2: settled (done/error) generate_jobs rows past
    # GENERATE_JOB_RETENTION_DAYS. The row holds render_spec + result
    # (content, not just lifecycle metadata) -- noted for future
    # retention-policy review, not actionable now.
    for job in db.generate_jobs_due_gc(conn, _cutoff_iso(GENERATE_JOB_RETENTION_DAYS)):
        db.gc_generate_job(conn, job["job_id"])

    return {"landfilled": landfilled, "dissolved": dissolved}


def run_gc(conn: sqlite3.Connection, things_dir: Path | str, db_path: str,
           t1_days: int = 7, t2_days: int = 30) -> dict[str, list[str]]:
    """Advance the lifecycle by time: binned->landfilled (T1), then
    landfilled->dissolved (T2).

    Returns {"landfilled": [thing_id, ...], "dissolved": [thing_id, ...]} —
    every thing this run advanced, in the order it processed them.
    """
    return _run_gc_impl(conn, things_dir, db_path, t1_days, t2_days, None)


def run_gc_described(conn: sqlite3.Connection, things_dir: Path | str,
                     db_path: str, t1_days: int = 7, t2_days: int = 30):
    """run_gc plus, for every thing it moved, {"id", "name", "owner"}
    (owner = the thing's author column), captured before any dissolve.

    Returns (run_gc's result, {"landfilled": [...], "dissolved": [...]}).
    """
    described: dict[str, list[dict]] = {"landfilled": [], "dissolved": []}
    result = _run_gc_impl(conn, things_dir, db_path, t1_days, t2_days,
                          described)
    return result, described


# --- the one-notice-per-moving-run post (umwelt #929) ----------------------

NOTICE_MAX_THINGS = 25
NOTICE_MAX_CHARS = 3500
NOTICE_NAME_CHARS = 60
NOTICE_EXIT_CODE = 3
_RECIPIENT_RE = re.compile(r"[A-Za-z0-9_-]+")
_TRAILER_RESERVE = 40  # room kept for the final "+K more not listed" line


def _clean(text) -> str:
    """Neutralise free text before it enters the notice: control characters
    and newlines become a space, every '@' becomes '(at)' (so a thing named
    '@someone' can neither ring a bell nor forge a cc line), then truncate."""
    s = "".join(" " if (ord(c) < 32 or 0x7F <= ord(c) < 0xA0
                        or c in "  ") else c
                for c in str(text))
    return s.replace("@", "(at)")[:NOTICE_NAME_CHARS]


def parse_recipients(raw, sender):
    """(recipients, None) or (None, reason). Comma/whitespace separated;
    every name must match [A-Za-z0-9_-]+; the sender (case-insensitive) is
    removed; nothing left means not configured."""
    names = [n for n in re.split(r"[,\s]+", (raw or "").strip()) if n]
    if not names:
        return None, "UMWELT_GC_NOTICE_TO is not set"
    if any(not _RECIPIENT_RE.fullmatch(n) for n in names):
        return None, "UMWELT_GC_NOTICE_TO has a name outside [A-Za-z0-9_-]"
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        k = n.lower()
        if k == sender.lower() or k in seen:
            continue
        seen.add(k)
        out.append(n)
    if not out:
        return None, "UMWELT_GC_NOTICE_TO holds only the sender"
    return out, None


def build_notice(landfilled, dissolved, recipients):
    """(to_agent, body). to_agent = first recipient; the others are cc'd on
    the body's first line (for readers); _post_notice also sends them as the
    payload's `mentions`, which is what the world acts on. TOTALS in the
    header are always true; listing is capped (NOTICE_MAX_THINGS things,
    NOTICE_MAX_CHARS characters) with a final '+K more not listed' line."""
    to = recipients[0]
    lines: list[str] = []
    if len(recipients) > 1:
        lines.append("cc " + " ".join("@" + r for r in recipients[1:]))
    lines.append(f"world-gc: landfilled {len(landfilled)}, "
                 f"dissolved {len(dissolved)}")
    lines.append("Owner is the thing's author, who binned it (the "
                 "retire-identity route is the one exception).")
    used = sum(len(x) + 1 for x in lines)
    listed = 0
    total = len(landfilled) + len(dissolved)
    full = False
    for title, items in (("Landfilled:", landfilled),
                         ("Dissolved:", dissolved)):
        started = False
        for it in items:
            entry = (f"- {_clean(it['name'])} ({_clean(it['id'])}) -- "
                     f"{_clean(it['owner'])}")
            cost = len(entry) + 1 + (0 if started else len(title) + 1)
            if (listed >= NOTICE_MAX_THINGS
                    or used + cost + _TRAILER_RESERVE > NOTICE_MAX_CHARS):
                full = True
                break
            if not started:
                lines.append(title)
                started = True
            lines.append(entry)
            used += cost
            listed += 1
        if full:
            break
    if listed < total:
        lines.append(f"+{total - listed} more not listed")
    return to, "\n".join(lines)


def _post_notice(described, opener, db_path=None):
    """(posted_ok, reason). Never raises. The destination is
    $UMWELT_GC_NOTICE_PLACE, else the world's hub role read from `db_path`
    (read-only; place_square when it cannot be read), umwelt#1043."""
    sender = os.environ.get("UMWELT_GC_NOTICE_FROM", "").strip() or "world-gc"
    if not _RECIPIENT_RE.fullmatch(sender):
        return False, ("UMWELT_GC_NOTICE_FROM has a character outside "
                       "[A-Za-z0-9_-]")
    recipients, why = parse_recipients(
        os.environ.get("UMWELT_GC_NOTICE_TO"), sender)
    if recipients is None:
        return False, why
    place = (os.environ.get("UMWELT_GC_NOTICE_PLACE", "").strip()
             or db.role_place_id_readonly(db_path, "hub"))
    to, body = build_notice(described["landfilled"], described["dissolved"],
                            recipients)
    try:
        from . import client_auth, world_backup
        rc = world_backup.alarm(client_auth.resolve_world_url(explicit=None),
                                agent=sender, to_agent=to, destination=place,
                                message=body, opener=opener,
                                mentions=recipients[1:])
    except Exception as exc:  # the moves already happened; report, don't die
        return False, f"post raised {type(exc).__name__}: {exc}"
    if rc != 0:
        return False, f"post failed (exit {rc})"
    return True, ""


def _unregistered_recipients(known):
    """Configured recipients (as typed) whose canonical form is not in
    `known`, the world's registered avatar agents. Same fold the
    send_crossing route applies to to_agent / mentions."""
    sender = os.environ.get("UMWELT_GC_NOTICE_FROM", "").strip() or "world-gc"
    if not _RECIPIENT_RE.fullmatch(sender):
        return []
    recipients, _why = parse_recipients(
        os.environ.get("UMWELT_GC_NOTICE_TO"), sender)
    if recipients is None:
        return []
    return [r for r in recipients if server._canonical_agent(r) not in known]


def main(argv=None, *, opener=None) -> int:
    """CLI. Exit 0 normally; 3 when something moved but the notice was not
    (or could not be) posted (umwelt #929) -- visible in the journal."""
    import argparse

    p = argparse.ArgumentParser(
        description="Umwelt world GC — advances binned->landfilled->dissolved "
                     "by age. Engine-side; run daily."
    )
    p.add_argument("--db", default=os.path.expanduser("~/.umwelt/umwelt.db"))
    p.add_argument("--t1-days", type=int, default=7,
                    help="binned -> landfilled after this many days (default 7)")
    p.add_argument("--t2-days", type=int, default=30,
                    help="landfilled -> dissolved after this many days (default 30)")
    args = p.parse_args(argv)

    db_path = os.path.abspath(os.path.expanduser(args.db))
    # Same convention as server.py's main(): don't assume the data dir
    # already exists (a GC run could in principle be the very first thing
    # ever pointed at a fresh --db path).
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    db.init_db(conn)
    # umwelt#533 fast-follow: this was `Path(db_path).parent / "things"`,
    # unconditionally -- for a non-canonical tier that silently pointed GC
    # at the WRONG things dir (often the canonical/prod one, since both
    # resolved to the same bare `things` under the old formula), exactly
    # the cross-tier collision #533 exists to prevent. server._things_dir
    # is the single source of truth for this derivation now; duplicating
    # its formula here is what broke it the first time. Called after
    # db.init_db so the tier-identity marker check inside it has a real
    # schema to read/write against.
    things_dir = server._things_dir(db_path)
    try:
        result, described = run_gc_described(
            conn, things_dir, db_path,
            t1_days=args.t1_days, t2_days=args.t2_days)
        known = set(db.list_avatar_agents(conn))
    finally:
        conn.close()

    print(f"world-gc: {len(result['landfilled'])} landfilled, "
          f"{len(result['dissolved'])} dissolved")
    for tid in result["landfilled"]:
        print(f"  landfilled: {tid}")
    for tid in result["dissolved"]:
        print(f"  dissolved:  {tid}")

    moved = len(result["landfilled"]) + len(result["dissolved"])
    if moved == 0:
        return 0
    ok, reason = _post_notice(described, opener, db_path=db_path)
    code = 0
    if not ok:
        print(f"world-gc: moved {moved} things but no notice posted: "
              f"{reason}", file=sys.stderr)
        code = NOTICE_EXIT_CODE
    # The world drops unregistered `mentions` and stores an unregistered
    # `to_agent` without complaint, so a typo'd recipient would exit 0.
    missing = _unregistered_recipients(known)
    if missing:
        print("world-gc: notice recipients not registered (will not be "
              f"notified): {', '.join(missing)}", file=sys.stderr)
        code = NOTICE_EXIT_CODE
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
