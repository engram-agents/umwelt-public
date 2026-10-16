#!/usr/bin/env python3
"""world-state-snapshot.py — row-count/schema diff instrument for promotions
(umwelt#726).

`docs/DEPLOY.md` prescribes verifying "resident play-state row-counts
unchanged" after a promotion, but until this tool existed nothing implemented
that check — the deploy lead re-derived it as a throwaway heredoc each time.
On the promotion this tool was built from, an `identities: 12 -> 13` delta
was the ONLY signal that a restart had converted a stray test avatar into a
login-able identity (#723). `/health`, the TLS handshake,
`PRAGMA integrity_check` and `PRAGMA foreign_key_check` were all green and
all structurally blind to it.

The generalizable reason: the green checks are assertions about state; a
diff is a measurement of change, and only a measurement of change can
surface something nobody predicted. An assertion can only fail in a way its
author anticipated.

Usage:

    world-state-snapshot.py <db-path> --out SNAPSHOT.json
    world-state-snapshot.py <db-path> --against SNAPSHOT.json \\
        [--expect table=+N ...] [--expect-column table.column ...]

HARD REQUIREMENT: every database open in this tool is READ-ONLY
(`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`). This tool is meant to
be pointed at the LIVE production world database as part of a promotion
check. A checking tool that can write to the thing it checks is not
acceptable, so read-only mode is structural here, not a matter of
discipline or care.

Standalone stdlib-only script. No engine imports, no PYTHONPATH dependency
— it must run against a bare `.db` file or a backup copy, nothing else.

Exit behaviour (the design decision from umwelt#726, made rather than left
open):

- Exit 0 when every LOW-churn row-count delta and every schema change is
  accounted for by a matching --expect / --expect-column.
- Exit non-zero on any unaccounted LOW-churn delta, or any column
  addition/removal, or any whole table addition/removal.
- An --expect (or --expect-column) that does not match reality is ITSELF a
  failure, in both directions: naming a different delta than what actually
  happened, and naming a table/column that did not change at all (a stale
  expectation). This is what stops --expect from degrading into a blanket
  suppressor, and it is what stops an expect-list from being carried
  forward between deploys without being re-derived.
- HIGH-churn deltas are reported but never gate on their own — but if an
  --expect is given for a HIGH-churn table, it is still checked against
  reality by the same both-directions rule (naming one gives you a
  correctness check, not a free pass).
- There is no --force and no blanket suppressor. The only way past a gate
  is to name exactly what you expect.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# HIGH_CHURN is an explicit, named constant -- everything else is LOW-churn
# BY DEFAULT. That default is the forcing function: a table added later
# (by a migration, a new feature) starts out strict. It will trip the gate
# on its very first unexplained delta until someone either acknowledges the
# delta with --expect or makes a deliberate, reviewable decision to add it
# to this set. A table can therefore never silently acquire a pass just by
# not being mentioned here -- the silence is what forces the classification
# rather than defaulting to "probably fine."
HIGH_CHURN = frozenset({
    "crossings", "agent_sessions", "login_challenges",
    "use_events", "agent_heartbeat", "events",
})


def _ro_uri(db_path: str) -> str:
    """Build a read-only sqlite URI for db_path. Does not create the file if
    it is missing -- sqlite3 in mode=ro errors instead, which is itself part
    of the read-only guarantee (a read-write open would silently create an
    empty db for a bad path)."""
    return f"file:{quote(str(Path(db_path).resolve()))}?mode=ro"


def _connect_ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(_ro_uri(db_path), uri=True)


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Enumerate user tables from sqlite_master, excluding sqlite_%."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def build_snapshot(db_path: str) -> dict:
    """Open db_path read-only and capture: per table, row count + column
    names (the schema fingerprint), plus source path + ISO timestamp for
    provenance. The schema fingerprint is column NAMES only -- a column
    TYPE change (e.g. TEXT -> INTEGER) is invisible to this tool."""
    conn = _connect_ro(db_path)
    try:
        tables = {}
        for table in list_tables(conn):
            count = conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            columns = [
                row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
            ]
            tables[table] = {"row_count": count, "columns": columns}
    finally:
        conn.close()

    return {
        "source_db": str(Path(db_path).resolve()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }


def _parse_expect(items: list[str]) -> dict[str, int]:
    expect = {}
    for item in items:
        table, sep, val = item.partition("=")
        if not sep:
            raise SystemExit(
                f"--expect must be TABLE=+N or TABLE=-N, got: {item!r}"
            )
        expect[table] = int(val)
    return expect


def diff_snapshots(
    old: dict, new: dict, expect: dict[str, int], expect_column: set[str]
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Compare two snapshots. Returns (schema_changes, low_churn_deltas,
    high_churn_deltas, failures)."""
    old_tables = old["tables"]
    new_tables = new["tables"]
    all_tables = sorted(set(old_tables) | set(new_tables))

    schema_changes: list[dict] = []
    low_churn_deltas: list[dict] = []
    high_churn_deltas: list[dict] = []
    failures: list[str] = []

    used_expect_column: set[str] = set()

    for table in all_tables:
        if table not in old_tables:
            schema_changes.append({"table": table, "change": "table_added"})
            failures.append(f"schema: table added: {table}")
            continue
        if table not in new_tables:
            schema_changes.append({"table": table, "change": "table_removed"})
            failures.append(f"schema: table removed: {table}")
            continue

        old_cols = set(old_tables[table]["columns"])
        new_cols = set(new_tables[table]["columns"])

        for col in sorted(new_cols - old_cols):
            schema_changes.append(
                {"table": table, "change": "column_added", "column": col}
            )
            key = f"{table}.{col}"
            if key in expect_column:
                used_expect_column.add(key)
            else:
                failures.append(f"schema: unexpected column added: {key}")

        for col in sorted(old_cols - new_cols):
            schema_changes.append(
                {"table": table, "change": "column_removed", "column": col}
            )
            key = f"{table}.{col}"
            if key in expect_column:
                used_expect_column.add(key)
            else:
                failures.append(f"schema: unexpected column removed: {key}")

        old_count = old_tables[table]["row_count"]
        new_count = new_tables[table]["row_count"]
        delta = new_count - old_count
        entry = {
            "table": table,
            "old_count": old_count,
            "new_count": new_count,
            "delta": delta,
        }

        is_high_churn = table in HIGH_CHURN

        if delta != 0:
            (high_churn_deltas if is_high_churn else low_churn_deltas).append(
                entry
            )

        if table in expect:
            if expect[table] != delta:
                failures.append(
                    f"expect mismatch: {table} expected "
                    f"{expect[table]:+d}, actual {delta:+d}"
                )
            # else: matches reality -- accounted for, whether high or low
            # churn.
        elif not is_high_churn and delta != 0:
            failures.append(
                f"unaccounted low-churn delta: {table} {delta:+d}"
            )
        # LOW-churn, delta == 0, no --expect: nothing to account for.
        # HIGH-churn, no --expect: reported (if nonzero) but never gates.

    # Stale --expect entries: named a table not present in either snapshot
    # at all. (--expect entries for tables present in both snapshots are
    # already checked against reality inside the loop above -- a stale
    # "named but delta is actually 0" case surfaces there as a mismatch
    # against the real delta of 0, which is exactly the "does not match
    # reality" rule the spec asks for, in the same code path as a wrong
    # numeric guess.)
    for table in expect:
        if table not in all_tables:
            failures.append(
                f"stale expect: table {table!r} not present in snapshot"
            )

    for key in expect_column:
        if key not in used_expect_column:
            failures.append(
                f"stale expect-column: {key} named but that column did "
                "not change"
            )

    return schema_changes, low_churn_deltas, high_churn_deltas, failures


def _print_section(title: str, rows: list, formatter) -> None:
    print(f"=== {title} ===")
    if not rows:
        print("  (none)")
    else:
        for row in rows:
            print(f"  {formatter(row)}")


def _fmt_schema(row: dict) -> str:
    if row["change"] in ("table_added", "table_removed"):
        return f"{row['table']}: {row['change']}"
    return f"{row['table']}.{row['column']}: {row['change']}"


def _fmt_delta(row: dict) -> str:
    return (
        f"{row['table']}: {row['old_count']} -> {row['new_count']} "
        f"({row['delta']:+d})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Row-count/schema diff instrument for world-db "
        "promotions (umwelt#726). Opens the database READ-ONLY."
    )
    parser.add_argument("db_path", help="path to the sqlite world db")
    parser.add_argument(
        "--out", metavar="SNAPSHOT.json", help="write a snapshot and exit"
    )
    parser.add_argument(
        "--against",
        metavar="SNAPSHOT.json",
        help="diff db_path's current state against a prior snapshot",
    )
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="TABLE=+N",
        help="acknowledge an expected row-count delta on TABLE; repeatable",
    )
    parser.add_argument(
        "--expect-column",
        action="append",
        default=[],
        metavar="TABLE.COLUMN",
        help="acknowledge an expected column addition/removal; repeatable",
    )
    args = parser.parse_args(argv)

    if bool(args.out) == bool(args.against):
        parser.error("exactly one of --out or --against is required")

    try:
        snapshot = build_snapshot(args.db_path)
    except sqlite3.OperationalError as exc:
        print(f"error: could not open {args.db_path!r} read-only: {exc}",
              file=sys.stderr)
        return 2

    if args.out:
        with open(args.out, "w") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"wrote snapshot: {args.out}")
        return 0

    with open(args.against) as f:
        old_snapshot = json.load(f)

    if old_snapshot.get("source_db") != snapshot["source_db"]:
        print(
            f"warning: --against snapshot's source_db "
            f"({old_snapshot.get('source_db')!r}) differs from the db "
            f"being scanned now ({snapshot['source_db']!r}) -- possibly a "
            "stale snapshot left over from a previous run",
            file=sys.stderr,
        )

    expect = _parse_expect(args.expect)
    expect_column = set(args.expect_column)

    schema_changes, low_churn, high_churn, failures = diff_snapshots(
        old_snapshot, snapshot, expect, expect_column
    )

    _print_section("schema changes", schema_changes, _fmt_schema)
    _print_section("low-churn deltas", low_churn, _fmt_delta)
    _print_section("high-churn deltas", high_churn, _fmt_delta)

    if failures:
        print("=== FAILURES ===")
        for msg in failures:
            print(f"  {msg}")
        return 1

    print("OK -- all deltas and schema changes accounted for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
