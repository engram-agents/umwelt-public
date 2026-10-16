#!/usr/bin/env python3
"""ONE-SHOT ops command: normalize every live project-board task whose
gh_ref is a bare number (e.g. "1796") to the canonical pr/<N> or issue/<N>
shape (umwelt#463 item 3, tracked as #468). Kepler's ruling (forum #293):
this backfill GATES #445 slice 3 -- until these rows are normalized, the
gh_reconcile timer resolves nothing (every candidate task is unrecognized
by parse_gh_ref, so `examined > 0, resolved == 0` forever).

Root cause of the bare-number rows: before #463 (umwelt PR #466), the
task create/update routes accepted any string for gh_ref, so `1796`
(instead of `pr/1796`) slipped straight onto the board.

What it does, per task with a bare-number gh_ref:
  1. Fetches the task's gh_repo (empty gh_repo -> skipped, logged as
     needs-human-input -- never guessed against a default repo, same
     no-ambient-fallback rule gh_reconcile.py already follows).
  2. Resolves whether the number names a PR or an issue in that repo via
     real `gh` queries -- GitHub's shared PR/issue numbering makes this
     determinable, never guessed. A number found in NEITHER (deleted,
     inaccessible, wrong repo) -> skipped, logged as needs-human-input.
  3. Writes the normalized gh_ref (pr/<N> or issue/<N>) through the real
     task-update HTTP route (server.py's now-shape-validating write path,
     post-#466) -- never a raw DB write. Uses gh_reconcile.default_poster,
     the same stdlib-urllib HTTP client umwelt's own reconciler uses.

Deliberately does NOT touch task status -- resolving/transitioning status
is gh_reconcile.py's job, on its own schedule, once gh_ref is shaped
correctly. This script's only effect is the shape of the gh_ref string.

Idempotent -- safe to re-run. A second run finds zero bare-number rows
left to fix (every prior run's writes now match a canonical shape,
which parse_gh_ref recognizes, so they're no longer selected).

Run:
    PYTHONPATH=src python3 tools/backfill_gh_ref_bare_numbers.py \\
        --url <board base url>
    PYTHONPATH=src python3 tools/backfill_gh_ref_bare_numbers.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from umwelt import client_auth  # noqa: E402
from umwelt.gh_reconcile import default_poster  # noqa: E402

_TOOL_ACTOR = "gh-backfill"


def _is_bare_number(gh_ref: str) -> bool:
    return gh_ref.strip().isdigit()


def default_fetch_json(url: str) -> dict:
    """Real HTTP GET, JSON-decoded. Injectable (mirrors default_poster's
    role for writes) so tests can drive _walk_bare_number_tasks against a
    fake tree with no network call."""
    req = urllib.request.Request(url)
    with client_auth.urlopen_with_cert_fallback(req, 10) as r:
        return json.loads(r.read().decode())


def _walk_tasks(tasks: list[dict]):
    """Yield every task in a project_tree's `tasks` list AND every nested
    sub-task (db.project_tree nests sub-tasks under each task's own
    `children` list, recursed to full depth -- a project_tree response's
    top-level `tasks` holds ONLY the roots, per its own docstring). A
    bare-number gh_ref on a sub-task is exactly as mis-shaped as one on a
    root task, so this must recurse, not just scan the first level."""
    for t in tasks:
        yield t
        yield from _walk_tasks(t.get("children") or [])


def _fetch_bare_number_tasks(base_url: str, fetch_json=default_fetch_json) -> list[dict]:
    """Walk every project's FULL tree (roots + all nested sub-tasks, see
    _walk_tasks) and return tasks whose gh_ref is a bare number, regardless
    of status -- unlike gh_reconcile.py's candidate selection, a shape fix
    applies to a task in ANY status (a done task with a bare-number gh_ref
    is just as mis-shaped as an open one)."""
    projects = fetch_json(f"{base_url.rstrip('/')}/umwelt/projects")["projects"]

    found = []
    for p in projects:
        url = f"{base_url.rstrip('/')}/umwelt/projects/{p['id']}"
        tree = fetch_json(url)["project"]
        for t in _walk_tasks(tree.get("tasks", [])):
            gh_ref = (t.get("gh_ref") or "").strip()
            if gh_ref and _is_bare_number(gh_ref):
                found.append(t)
    return found


def default_resolve_kind(number: str, repo: str) -> str | None:
    """Return 'pr' or 'issue' if `number` names one in `repo`, else None
    (never guessed). Tries pr first, then issue -- GitHub's shared
    PR/issue numbering means at most one of these can exist. Real `gh`
    subprocess calls -- tests inject a fake instead."""
    for kind in ("pr", "issue"):
        try:
            result = subprocess.run(
                ["gh", kind, "view", number, "--repo", repo, "--json", "number"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode == 0:
            return kind
    return None


def run_backfill(tasks, poster, resolve_kind=default_resolve_kind, *,
                  dry_run: bool = False) -> tuple[list, list]:
    """Core backfill pass over an already-fetched task list. `poster` and
    `resolve_kind` are injectable (mirrors gh_reconcile.run_reconcile's
    `poster` param) so tests exercise the real logic against a fake gh CLI
    and a real Flask test-client-backed poster, with no network/subprocess.

    Returns (fixed, needs_human) — each a list of tuples describing what
    happened (or would happen, under dry_run) per task."""
    fixed, needs_human = [], []

    for t in tasks:
        task_id, gh_ref, gh_repo = t["id"], t["gh_ref"].strip(), (t.get("gh_repo") or "").strip()
        label = t.get("label", "")

        if not gh_repo:
            needs_human.append((task_id, label, gh_ref, "empty gh_repo -- cannot resolve"))
            continue

        kind = resolve_kind(gh_ref, gh_repo)
        if kind is None:
            needs_human.append((task_id, label, gh_ref,
                                f"#{gh_ref} is neither a PR nor an issue in {gh_repo}"))
            continue

        normalized = f"{kind}/{gh_ref}"
        print(f"[backfill-gh-ref] {task_id} ({label!r}): "
              f"{gh_ref!r} -> {normalized!r}  (verified: {kind} #{gh_ref} in {gh_repo})")

        if dry_run:
            fixed.append((task_id, gh_ref, normalized))
            continue

        status, body = poster(f"/umwelt/tasks/{task_id}/update", {
            "agent": _TOOL_ACTOR,
            "patch": {"gh_ref": normalized},
        })
        if status != 200:
            needs_human.append((task_id, label, gh_ref,
                                f"write failed (status={status}): {body}"))
            continue
        fixed.append((task_id, gh_ref, normalized))

    return fixed, needs_human


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python tools/backfill_gh_ref_bare_numbers.py",
        description="One-shot normalization: bare-number gh_ref -> pr/<N> "
                     "or issue/<N> on the live project board. Idempotent.",
    )
    # umwelt#699 §1: default=None, not resolve_world_url() -- the resolver
    # must see the parsed VALUE (a real --url the operator typed is a
    # caller override the old default-factory form silently bypassed).
    # Routed through the resolver just below, after parse_args().
    parser.add_argument("--url", default=None,
                        help="Board base URL (default: see "
                             "client_auth.resolve_world_url)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and print what would change; write nothing.")
    args = parser.parse_args(argv)
    args.url = client_auth.resolve_world_url(explicit=args.url)

    # A mutating one-shot write path with a resolved (not literal) default --
    # print it before writing so "ran against the wrong board because
    # config/env wasn't set" is an immediately visible fact, not something
    # only discoverable by reading the board afterward. Reviewer-fairy
    # finding on umwelt PR#679.
    print(f"[backfill-gh-ref] target: {args.url}")

    tasks = _fetch_bare_number_tasks(args.url)
    if not tasks:
        print("[backfill-gh-ref] nothing to migrate -- no bare-number gh_ref rows found.")
        return

    fixed, needs_human = run_backfill(
        tasks, default_poster(args.url), dry_run=args.dry_run)

    verb = "would fix" if args.dry_run else "fixed"
    print(f"[backfill-gh-ref] {verb} {len(fixed)} of {len(tasks)} bare-number row(s).")
    if needs_human:
        print(f"[backfill-gh-ref] {len(needs_human)} row(s) need human input:")
        for task_id, label, gh_ref, reason in needs_human:
            print(f"[backfill-gh-ref]   {task_id} ({label!r}, gh_ref={gh_ref!r}): {reason}")


if __name__ == "__main__":  # pragma: no cover
    main()
