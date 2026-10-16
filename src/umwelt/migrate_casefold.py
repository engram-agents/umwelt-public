"""umwelt #73 migration — fold case-variant agent-identity duplicates.

Before umwelt #73's server-side fix, agent identity was whatever the caller
typed, verbatim — so `Lei` and `lei` minted two separate identities (separate
home posts, separate avatars, separate standpoints). This script repairs
existing data created under that bug: it finds every case-variant duplicate
of an agent identity across the tables that carry a caller-supplied agent
string, and merges each group into a single lowercase canonical identity.

Tables covered:
  - places       (id/owner, for `place_post_<agent>` home posts)
  - things       (author — avatars get a collision-aware merge, since
                  idx_things_avatar_author enforces exactly one avatar per
                  author; non-avatar things are a plain fold, multiple things
                  per author is normal and not a collision)
  - crossings    (sender, to_agent)
  - use_events   (agent)
  - standpoints  (agent_id — PK is (agent_id, place_id), so a fold can
                  collide; merged by keeping the more-advanced read cursor)

Standalone and idempotent — safe to run twice; the second run finds nothing
left to do. `--dry-run` executes the full migration inside a transaction and
rolls it back at the end, printing exactly what would have changed — so the
printed log is never speculative, it's what the migration actually computed.

Run:
    python -m umwelt.migrate_casefold --db <path> [--dry-run]

Do NOT point this at a live DB without a fresh backup — always verify with
--dry-run first. Deliberately never run automatically; a human invokes it.
"""

from __future__ import annotations

import argparse
import sqlite3

from ._ids import canonical_agent as _canonical


# `_canonical` is `_ids.canonical_agent` — the ONE fold implementation shared
# with server.py's `_canonical_agent` and mud.py's canonical-name computation.
# See `_ids.py`'s module docstring: the migration and every live door must
# fold identically, or merged duplicates can reappear post-migration.


# --- places: place_post_<agent> home-post duplicates ------------------------

def _migrate_places(conn: sqlite3.Connection, log: list[str]) -> None:
    rows = conn.execute(
        "SELECT id, kind, owner, label, description FROM places "
        "WHERE id LIKE 'place_post_%'"
    ).fetchall()
    groups: dict[str, list[tuple]] = {}
    for row in rows:
        name = row[0][len("place_post_"):]
        groups.setdefault(_canonical(name), []).append(row)

    for canon, variants in groups.items():
        canonical_id = f"place_post_{canon}"
        keep = next((v for v in variants if v[0] == canonical_id), None)
        renamed_from: str | None = None
        if keep is None:
            # No variant already has the canonical id — pick one
            # deterministically (lowest id string) and rename it in place.
            # The row's OWN id changes here, but every OTHER table's FK
            # columns (things.place_id, crossings.destination,
            # standpoints.place_id) still point at the pre-rename id and
            # must be re-pointed below just like a dropped duplicate would
            # be — renamed_from tracks that so the FK-repoint loop below
            # covers this row too (bugfix: previously this branch left
            # those FKs dangling, since `keep` held the OLD row tuple and
            # `drop_ids` (== ids != keep[0]) was always empty here).
            keep = sorted(variants, key=lambda v: v[0])[0]
            renamed_from = keep[0]
            conn.execute(
                "UPDATE places SET id=?, owner=? WHERE id=?",
                (canonical_id, canon, renamed_from),
            )
            log.append(f"places: renamed {renamed_from!r} -> {canonical_id!r} "
                       f"(owner folded to {canon!r})")
        elif keep[2] != canon:
            conn.execute("UPDATE places SET owner=? WHERE id=?", (canon, canonical_id))
            log.append(f"places: folded owner of {canonical_id!r} to {canon!r}")

        # Every variant id other than canonical_id needs its FK references
        # re-pointed — whether that id belongs to a duplicate row still
        # sitting in `places` (dropped below) or to the row that was just
        # renamed in place above (its places row is gone under the old id,
        # but things/crossings/standpoints still reference it until we fix
        # them here).
        fk_repoint_ids = [v[0] for v in variants if v[0] != canonical_id]
        for old_id in fk_repoint_ids:
            conn.execute("UPDATE things SET place_id=? WHERE place_id=?",
                        (canonical_id, old_id))
            conn.execute("UPDATE crossings SET destination=? WHERE destination=?",
                        (canonical_id, old_id))
            sp_rows = conn.execute(
                "SELECT agent_id, last_seen_at, last_position FROM standpoints "
                "WHERE place_id=?", (old_id,),
            ).fetchall()
            for agent_id, last_seen_at, last_position in sp_rows:
                existing = conn.execute(
                    "SELECT last_position FROM standpoints "
                    "WHERE agent_id=? AND place_id=?", (agent_id, canonical_id),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "UPDATE standpoints SET place_id=? "
                        "WHERE agent_id=? AND place_id=?",
                        (canonical_id, agent_id, old_id),
                    )
                else:
                    keep_pos = existing[0] if existing[0] is not None else -1
                    new_pos = last_position if last_position is not None else -1
                    if new_pos > keep_pos:
                        conn.execute(
                            "UPDATE standpoints SET last_seen_at=?, last_position=? "
                            "WHERE agent_id=? AND place_id=?",
                            (last_seen_at, last_position, agent_id, canonical_id),
                        )
                    conn.execute(
                        "DELETE FROM standpoints WHERE agent_id=? AND place_id=?",
                        (agent_id, old_id),
                    )
            if old_id == renamed_from:
                # The places row at old_id no longer exists (it WAS the
                # rename target, above) — nothing to drop, just log the
                # FK re-point.
                log.append(f"places: re-pointed things/crossings/standpoints "
                           f"from renamed {old_id!r} to {canonical_id!r}")
            else:
                conn.execute("DELETE FROM places WHERE id=?", (old_id,))
                log.append(f"places: merged duplicate {old_id!r} into "
                           f"{canonical_id!r} (re-pointed things/crossings/"
                           "standpoints, dropped the duplicate row)")


# --- things: avatar author collisions (one avatar per canonical author) -----

def _migrate_avatars(conn: sqlite3.Connection, log: list[str]) -> None:
    rows = conn.execute(
        "SELECT id, author, created_at FROM things WHERE kind='avatar'"
    ).fetchall()
    groups: dict[str, list[tuple]] = {}
    for tid, author, created_at in rows:
        groups.setdefault(_canonical(author), []).append((tid, author, created_at))

    for canon, variants in groups.items():
        if len(variants) == 1:
            tid, author, _created = variants[0]
            if author != canon:
                conn.execute("UPDATE things SET author=? WHERE id=?", (canon, tid))
                log.append(f"things(avatar): folded author of {tid!r} from "
                           f"{author!r} to {canon!r}")
            continue

        # Multiple avatars fold to the same canonical author — a real
        # collision the pre-#73 bug could produce (e.g. 'lei' and 'Lei' each
        # had their own body). The earliest-created avatar is the PRIMARY
        # tie-break: the spurious duplicate minted by the case-sensitivity
        # bug is always the LATER one (whatever case it happened to be
        # typed in), so earliest-created is the more reliable survivor rule
        # than "prefer the already-canonically-cased variant" — that older
        # rule would have picked the wrong survivor whenever the original
        # identity was typed in mixed case and a later, differently-cased
        # call minted the duplicate. Every other variant is dropped — this
        # is an INFERENCE from creation order + the known bug shape, not
        # certainty; see the handoff's "Ambiguous decisions" for this
        # tie-break policy.
        keep = sorted(variants, key=lambda v: v[2])[0]

        # Drop the losing duplicates FIRST, before folding the keeper's
        # author. Order matters: idx_things_avatar_author is a partial
        # UNIQUE index on author, so if a dropped duplicate already holds
        # the exact canonical author string (e.g. keep is the older
        # mixed-case row and a newer already-canonical row is being
        # dropped), updating keep's author to canon BEFORE the duplicate is
        # gone would collide with that still-live row and raise
        # IntegrityError. Deleting first frees the canonical author string.
        for tid, author, _created in variants:
            if tid == keep[0]:
                continue
            conn.execute("DELETE FROM things WHERE id=?", (tid,))
            log.append(f"things(avatar): dropped later duplicate avatar {tid!r} "
                       f"(author {author!r}) under earliest-created tie-break — "
                       "assumed bug-minted; verify before applying")

        if keep[1] != canon:
            conn.execute("UPDATE things SET author=? WHERE id=?", (canon, keep[0]))
            log.append(f"things(avatar): folded KEPT avatar {keep[0]!r} author "
                       f"{keep[1]!r} -> {canon!r}")


# --- things: non-avatar author fold (no collision — multiple things per
#     author is normal) ------------------------------------------------------

def _migrate_thing_authors(conn: sqlite3.Connection, log: list[str]) -> None:
    rows = conn.execute(
        "SELECT id, author FROM things WHERE kind != 'avatar' "
        "AND author IS NOT NULL AND author != lower(author)"
    ).fetchall()
    for tid, author in rows:
        canon = _canonical(author)
        conn.execute("UPDATE things SET author=? WHERE id=?", (canon, tid))
        log.append(f"things: folded author of {tid!r} from {author!r} to {canon!r}")


# --- crossings: sender / to_agent --------------------------------------------

def _migrate_crossings(conn: sqlite3.Connection, log: list[str]) -> None:
    rows = conn.execute(
        "SELECT id, sender FROM crossings WHERE sender != lower(sender)"
    ).fetchall()
    for cid, sender in rows:
        canon = _canonical(sender)
        conn.execute("UPDATE crossings SET sender=? WHERE id=?", (canon, cid))
        log.append(f"crossings: folded sender of {cid!r} from {sender!r} to {canon!r}")

    rows = conn.execute(
        "SELECT id, to_agent FROM crossings "
        "WHERE to_agent IS NOT NULL AND to_agent != lower(to_agent)"
    ).fetchall()
    for cid, to_agent in rows:
        canon = _canonical(to_agent)
        conn.execute("UPDATE crossings SET to_agent=? WHERE id=?", (canon, cid))
        log.append(f"crossings: folded to_agent of {cid!r} from {to_agent!r} "
                   f"to {canon!r}")


# --- use_events: agent --------------------------------------------------------

def _migrate_use_events(conn: sqlite3.Connection, log: list[str]) -> None:
    rows = conn.execute(
        "SELECT id, agent FROM use_events WHERE agent != lower(agent)"
    ).fetchall()
    for uid, agent in rows:
        canon = _canonical(agent)
        conn.execute("UPDATE use_events SET agent=? WHERE id=?", (canon, uid))
        log.append(f"use_events: folded agent of use_event {uid} from "
                   f"{agent!r} to {canon!r}")


# --- standpoints: agent_id (PK collision-aware merge) ------------------------

def _migrate_standpoints(conn: sqlite3.Connection, log: list[str]) -> None:
    rows = conn.execute(
        "SELECT agent_id, place_id, last_seen_at, last_position FROM standpoints"
    ).fetchall()
    groups: dict[tuple[str, str], list[tuple]] = {}
    for agent_id, place_id, last_seen_at, last_position in rows:
        groups.setdefault((_canonical(agent_id), place_id), []).append(
            (agent_id, place_id, last_seen_at, last_position)
        )

    for (canon_agent, place_id), variants in groups.items():
        if len(variants) == 1 and variants[0][0] == canon_agent:
            continue  # already canonical, nothing to merge

        def _rank(v: tuple) -> tuple:
            pos = v[3] if v[3] is not None else -1
            return (pos, v[2] or "")

        keep = max(variants, key=_rank)
        for agent_id, pid, _seen, _pos in variants:
            conn.execute(
                "DELETE FROM standpoints WHERE agent_id=? AND place_id=?",
                (agent_id, pid),
            )
        conn.execute(
            "INSERT INTO standpoints(agent_id, place_id, last_seen_at, last_position) "
            "VALUES(?, ?, ?, ?)",
            (canon_agent, place_id, keep[2], keep[3]),
        )
        log.append(f"standpoints: merged {len(variants)} case-variant row(s) "
                   f"for agent {canon_agent!r} at {place_id!r} -> kept "
                   f"last_position={keep[3]!r}, last_seen_at={keep[2]!r}")


# --- orchestration -----------------------------------------------------------

def run_migration(conn: sqlite3.Connection, dry_run: bool = False) -> list[str]:
    """Execute the full case-fold migration against `conn`.

    Returns the ordered list of human-readable change-log lines describing
    every write made. If `dry_run`, every write is rolled back at the end —
    the returned log describes exactly what WOULD have happened (computed by
    actually running the migration against a transaction that's then
    discarded, not by a separate speculative code path that could drift out
    of sync with the real one).

    Commits ONLY on a clean run; any exception raised mid-migration rolls
    back every write made so far and re-raises — never a partial commit
    against a live DB. (A bare `finally: commit()` would commit even on an
    exception, since `finally` always runs regardless of how the `try` block
    exited — that was the bug: a crash partway through a non-dry-run left
    the DB permanently half-migrated.)

    Order matters: places must merge first (things/crossings/standpoints
    place_id columns get re-pointed as a side effect), then avatar-author
    collisions, then the remaining plain column folds.

    Runs with `foreign_keys = OFF` (needed to reorder writes across the six
    steps above without tripping FK enforcement mid-migration) — which means
    a missed FK repoint would otherwise orphan rows SILENTLY: no constraint
    violation would ever fire to say so. Before the commit/rollback decision,
    `PRAGMA foreign_key_check` is run as a loud integrity guard: if it finds
    ANY row that would be orphaned (in either a real run or a dry-run — a
    dry-run's whole point is to surface exactly what a real run would do,
    including this), the migration raises and rolls back rather than
    committing (or reporting a clean dry-run) over a silent gap.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    log: list[str] = []
    try:
        _migrate_places(conn, log)
        _migrate_avatars(conn, log)
        _migrate_thing_authors(conn, log)
        _migrate_crossings(conn, log)
        _migrate_use_events(conn, log)
        _migrate_standpoints(conn, log)

        orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
        if orphans:
            raise RuntimeError(f"migration would orphan FK rows: {orphans}")

        conn.rollback() if dry_run else conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return log


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m umwelt.migrate_casefold",
        description="umwelt #73 — fold case-variant agent-identity "
                    "duplicates to a single lowercase canonical identity.",
    )
    parser.add_argument("--db", required=True, help="path to the umwelt sqlite db")
    parser.add_argument("--dry-run", action="store_true",
                        help="print planned changes; write nothing to disk")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        log = run_migration(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    tag = "[dry-run]" if args.dry_run else "[migrate]"
    if not log:
        print(f"{tag} nothing to do — no case-variant duplicates found.")
        return
    for line in log:
        print(f"{tag} {line}")
    if args.dry_run:
        print(f"{tag} {len(log)} change(s) planned; nothing written to disk "
             "(re-run without --dry-run to apply).")
    else:
        print(f"{tag} {len(log)} change(s) applied.")


if __name__ == "__main__":
    main()
