"""GitHub reconciler for project-board tasks (umwelt #445 slice 2).

Resolves each candidate task's `gh_repo`/`gh_ref` anchor to live PR/issue
state via the `gh` CLI and, when that state means "done", writes the
transition back through the engine's own HTTP update route — never a raw
`db.update_task` call. Replaces the legacy forum-board display-only
reconciler (`src/forum/board_projects.py` in engram-alpha), which only ever
rendered a computed status, never wrote one back.

**Out-of-process, engine-external** (design decision, do not revisit): the
engine (`server.py`) is deliberately hermetic — zero outbound network, no
`gh` subprocess. This module mirrors `gc.py`'s own out-of-process shape
(run standalone, on a systemd timer, never imported by server.py):

    python -m umwelt.gh_reconcile --db ~/.umwelt/umwelt.db [--url URL]
        [--dry-run] [--verbose]

An internal systemd --user periodic-run pattern exists (a service + timer
pair, not in this release; not installed/enabled by this change).

**Writes go through `POST /umwelt/tasks/<id>/update`, never `db.update_task`
directly** (design decision, do not revisit): the done-completeness gate
(refuse a `done` transition while blocking deps / open subtasks exist) lives
in the route + `_apply_effect_project_task_set`, not in db.py. A raw-DB
writer here would be a third gate-bypassing write path — the exact bug class
`_apply_effect_project_task_set`'s own gate check had to close (server.py's
DONE-COMPLETENESS GATE comment, ~line 3031). A gate refusal (409) here means:
leave the task unchanged, record the refusal in the run report — never
retry, never fall back to a raw write.

**`--repo` always, no ambient fallback** (design decision, do not revisit):
every `gh` call passes `--repo <task.gh_repo>` explicitly. A task with
`gh_ref` set but `gh_repo` empty/missing is SKIPPED and counted
(`skipped_no_repo`) — never resolved against a default or ambient repo.
Lineage: the legacy forum reconciler's ambient-cwd bug (umwelt-side #1715 in
engram-alpha, `src/forum/board_projects.py` — "PR-22 in engram-paper
reconciled against engram-alpha's own #22"). umwelt#445 claims per-task
`gh_repo` "fixes by construction" — this module preserves that construction
by refusing to guess a repo, ever.

Never transitions a task backward: only tasks with
`status IN ('open','in_progress','review')` are selected; `done`/`cancelled`
tasks are never touched, and this module only ever writes the two terminal
truthful states: `'done'` (merged PR / completed issue) or `'cancelled'`
(PR closed without merging / issue closed as not-planned). Mapping an
abandoned PR to `done` would write a false completion onto a shared board
that this forward-only tool could never self-correct (colleague blocker,
umwelt#461 round 2); `is_task_blocked` treats the two identically for
dependency-unblocking, so the truthful state costs nothing mechanically.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

# gh_ref shape parsing moved to the shared ghref module (umwelt#463) so
# server.py's write-time validation and this reconciler's read-time
# resolution can never drift apart — the exact failure mode that let three
# conventions accrete on the live board in the first place. parse_gh_ref is
# re-exported here (rather than requiring every caller/test in this module
# to import from umwelt.ghref directly) so this remains a drop-in for
# existing callers.
from . import client_auth
from .ghref import parse_gh_ref  # noqa: F401  (re-exported for callers)

# Statuses eligible for reconciliation — see module docstring: this tool
# only ever writes the terminal states 'done'/'cancelled', never touches
# tasks already in them, and never transitions backward.
_CANDIDATE_STATUSES = ("open", "in_progress", "review")

# Tool identity used as the `agent` field on writes — a tool identity, not
# an agent name (spec's instruction; the update route has no actor/registry
# validation to conform to — see server.py's update_task, which only checks
# `agent` is a non-empty string).
_TOOL_ACTOR = "gh-reconcile"

PosterFn = Callable[[str, dict], "tuple[int, dict]"]


@dataclass
class ReconcileReport:
    """Run-report counters + per-change/per-error log lines."""

    examined: int = 0
    resolved: int = 0
    updated: int = 0
    gate_refused: int = 0
    skipped_no_repo: int = 0
    unknown: int = 0
    cancelled: int = 0
    changes: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def summary_lines(self) -> list:
        lines = [
            "gh-reconcile run report:",
            f"  examined:        {self.examined}",
            f"  resolved:        {self.resolved}",
            f"  updated:         {self.updated}",
            f"  gate_refused:    {self.gate_refused}",
            f"  skipped_no_repo: {self.skipped_no_repo}",
            f"  unknown:         {self.unknown}",
            f"  cancelled:       {self.cancelled}  (subset of updated)",
        ]
        if self.changes:
            lines.append("  changes:")
            lines.extend(f"    {c}" for c in self.changes)
        if self.errors:
            lines.append("  errors:")
            lines.extend(f"    {e}" for e in self.errors)
        return lines


def _select_candidate_tasks(conn: sqlite3.Connection) -> list:
    placeholders = ",".join("?" for _ in _CANDIDATE_STATUSES)
    rows = conn.execute(
        f"SELECT id, project_id, status, gh_repo, gh_ref FROM tasks "
        f"WHERE status IN ({placeholders}) "
        f"AND gh_ref IS NOT NULL AND gh_ref != ''",
        _CANDIDATE_STATUSES,
    ).fetchall()
    return [
        {"id": r[0], "project_id": r[1], "status": r[2], "gh_repo": r[3],
         "gh_ref": r[4]}
        for r in rows
    ]


def _gh_pr_state(number: str, repo: str) -> str:
    """Resolve a PR ref. Returns a target status: 'done' (merged) /
    'cancelled' (closed without merging) / 'open' / 'unknown'.
    Never raises — any subprocess/parse failure degrades to 'unknown'."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", number, "--repo", repo,
             "--json", "state,mergedAt"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    try:
        data = json.loads((result.stdout or "").strip() or "{}")
        state = str(data.get("state") or "").upper()
    except (json.JSONDecodeError, ValueError, AttributeError):
        return "unknown"
    if state == "MERGED":
        return "done"
    if state == "CLOSED":
        return "cancelled"  # closed without merging — abandoned, not completed
    if state == "OPEN":
        return "open"
    return "unknown"


def _gh_issue_state(number: str, repo: str) -> str:
    """Resolve an issue ref. Returns a target status: 'done' (closed as
    completed) / 'cancelled' (closed as not-planned — the issue-side
    sibling of a closed-unmerged PR) / 'open' / 'unknown'. Never raises."""
    try:
        result = subprocess.run(
            ["gh", "issue", "view", number, "--repo", repo,
             "--json", "state,stateReason"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    try:
        data = json.loads((result.stdout or "").strip() or "{}")
        state = str(data.get("state") or "").upper()
        reason = str(data.get("stateReason") or "").upper()
    except (json.JSONDecodeError, ValueError, AttributeError):
        return "unknown"
    if state == "CLOSED":
        return "cancelled" if reason == "NOT_PLANNED" else "done"
    if state == "OPEN":
        return "open"
    return "unknown"


def resolve_state(kind: str, number: str, repo: str) -> str:
    """Resolve (kind, number, repo) to 'done'|'cancelled'|'open'|'unknown'."""
    if kind == "pr":
        return _gh_pr_state(number, repo)
    if kind == "issue":
        return _gh_issue_state(number, repo)
    return "unknown"  # pragma: no cover — parse_gh_ref never emits other kinds


def default_poster(base_url: str) -> PosterFn:
    """Build a poster speaking real HTTP via stdlib urllib only (no
    third-party deps — mirrors mcp_server.py's `_request` helper, this
    repo's own precedent for a stdlib-only umwelt HTTP client).

    Returns (status_code, body_dict). status_code 0 signals a
    connection-level failure (body_dict carries {"error": str(exc)})."""

    def _post(path: str, payload: dict):
        url = f"{base_url.rstrip('/')}{path}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with client_auth.urlopen_with_cert_fallback(req, 10) as r:
                body = r.read().decode()
                return r.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            # A non-JSON error body (e.g. Flask's default HTML 500 page —
            # server.py has no @app.errorhandler) must degrade to a dict,
            # not raise: an uncaught JSONDecodeError here would abort the
            # whole reconcile batch on one bad response.
            try:
                return e.code, (json.loads(body) if body else {})
            except ValueError:
                return e.code, {"error": body[:200]}
        except urllib.error.URLError as e:
            return 0, {"error": str(e)}
        except (OSError, ValueError) as e:
            return 0, {"error": str(e)}

    return _post


def run_reconcile(conn: sqlite3.Connection, poster: PosterFn, *,
                   dry_run: bool = False, verbose: bool = False) -> ReconcileReport:
    """Core reconcile pass. `poster` is injectable so tests can wire a Flask
    test-client-backed poster instead of a real urllib round-trip (exercises
    the done-completeness gate for real, not mocked) — see
    `default_poster` for the real-HTTP implementation `main()` uses."""
    report = ReconcileReport()
    # Dedupe lookups by (gh_repo, gh_ref) — never re-resolve the same anchor
    # twice in one run, even if multiple tasks share it.
    cache: dict = {}

    tasks = _select_candidate_tasks(conn)
    report.examined = len(tasks)

    for task in tasks:
        gh_repo = (task["gh_repo"] or "").strip()
        gh_ref = (task["gh_ref"] or "").strip()

        if not gh_repo:
            report.skipped_no_repo += 1
            if verbose:
                print(f"skip-no-repo: task={task['id']} gh_ref={gh_ref!r}")
            continue

        parsed = parse_gh_ref(gh_ref)
        if parsed is None:
            report.unknown += 1
            if verbose:
                print(f"unknown-shape: task={task['id']} gh_repo={gh_repo} "
                      f"gh_ref={gh_ref!r}")
            continue
        kind, number = parsed

        cache_key = (gh_repo, gh_ref)
        if cache_key in cache:
            state = cache[cache_key]
        else:
            state = resolve_state(kind, number, gh_repo)
            cache[cache_key] = state

        if state == "unknown":
            report.unknown += 1
            if verbose:
                print(f"unknown: task={task['id']} gh_repo={gh_repo} "
                      f"gh_ref={gh_ref}")
            continue

        report.resolved += 1

        if state == "open":
            continue  # no change — spec's PR-OPEN / issue-open mapping

        # state is a terminal target from here on: 'done' or 'cancelled'.
        if dry_run:
            report.changes.append(
                f"[dry-run] task={task['id']} {gh_repo}#{gh_ref} -> {state}"
            )
            if state == "cancelled":
                report.cancelled += 1
            continue

        status_code, body = poster(
            f"/umwelt/tasks/{task['id']}/update",
            {"agent": _TOOL_ACTOR, "patch": {"status": state}},
        )
        if status_code == 200:
            report.updated += 1
            report.changes.append(f"task={task['id']} {gh_repo}#{gh_ref} -> {state}")
            if state == "cancelled":
                report.cancelled += 1
        elif status_code == 409:
            # Done-completeness gate refused — leave the task unchanged,
            # record the refusal, never fall back to a raw write.
            report.gate_refused += 1
            report.errors.append(
                f"gate-refused: task={task['id']} {gh_repo}#{gh_ref}: "
                f"{body.get('error', 'blocked')}"
            )
        else:
            report.errors.append(
                f"write-failed: task={task['id']} {gh_repo}#{gh_ref}: "
                f"status={status_code} {body.get('error', body)}"
            )

    return report


def main() -> None:  # pragma: no cover — thin CLI wrapper, exercised via run_reconcile
    p = argparse.ArgumentParser(
        description="Umwelt gh-reconcile — resolves task gh_repo/gh_ref "
                     "anchors to PR/issue state and marks tasks done via "
                     "the HTTP update route (umwelt #445 slice 2). "
                     "Out-of-process; run standalone."
    )
    p.add_argument("--db", default=os.path.expanduser("~/.umwelt/umwelt.db"))
    # umwelt#699 §1: default=None, not resolve_world_url() -- the resolver
    # must see the parsed VALUE (a real --url the operator typed is a
    # caller override the old default-factory form silently bypassed).
    # Routed through the resolver just below, after parse_args().
    p.add_argument("--url", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    args.url = client_auth.resolve_world_url(explicit=args.url)

    if shutil.which("gh") is None:
        print("gh-reconcile: `gh` CLI not found on PATH — aborting",
              file=sys.stderr)
        sys.exit(2)

    db_path = os.path.abspath(os.path.expanduser(args.db))
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        print(f"gh-reconcile: cannot open DB at {db_path} read-only "
              f"({e}) — aborting", file=sys.stderr)
        sys.exit(2)
    try:
        poster = default_poster(args.url)
        report = run_reconcile(conn, poster, dry_run=args.dry_run,
                                verbose=args.verbose)
    finally:
        conn.close()

    for line in report.summary_lines():
        print(line)
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
