"""Art-library manifest — the mutable status-machine layer (M4 art-generation
pipeline, slice 2: "the mutable manifest module").

`library.py` (slice 1) is a pure read/matcher over the manifest; this module
is the only writer of gate-managed state (`status`/`creator`/`approved_by`)
and the only live/concurrent writer of `<sprite_dir>/master_manifest.json`.
(The offline promotion script (internal tooling, not in this release) is a separate
one-shot path that predates this module and must not be run against a
sprite_dir once this gate is live for it — doing so would overwrite gate
state.) It adds a small moderation gate on top of the existing manifest
shape, per the merged design-of-record's "content gate" section: a
freshly generated sprite enters a `pending` state, a DIFFERENT peer
approves it, and only THEN does it become `live` — the state that
`library.py`'s `resolve`/`search` are willing to serve. The gate exists
because generated imagery is always human-and-child-viewed in shared
places, and a second pair of eyes before anything ships is the cheapest
reliable backstop against an obviously-wrong generation (face-leak, wrong
object, or worse) reaching a world visitor.

Two callers write through this module: the (future) generate endpoint calls
`add_pending`, and the (future) approve route calls `approve`. Both funnel
through **one write path** — advisory-lock, load, mutate, write-tmp-then-
atomic-rename, unlock — so the two can never race and corrupt the file
between them. Reads (`list_entries`, and `library.py`'s `resolve`/`search`)
are deliberately lock-free: the atomic rename on every write guarantees a
concurrent reader always observes either the whole old file or the whole
new one, never a half-written one.

M4 slice 4 adds a THIRD writer, `add_live` — a trusted-curator/ops-authority
primitive (never HTTP-exposed, called only from an offline script like
the internal generics-population script, not in this release) that appends an entry already `status: "live"`,
skipping `pending` entirely. It shares the same lock + atomic-write mechanics
as the other two writers, but is a structurally separate function rather
than a parameter on `add_pending` — see `add_live`'s own docstring for why.

Back-compat update (umwelt#289/Ariadne's finding): the manifest promoted
before this slice existed has no `status` field on any of its entries at
all. The ORIGINAL design let `is_live` treat a missing `status` as `live`
by default — a deliberate choice to avoid a destructive migration, but a
fail-OPEN one: it made every bypass of this module's write path (any
script that writes `master_manifest.json` directly, e.g. the pre-fix
internal promotion script, not in this release) invisible, since the entries it produced
were indistinguishable from peer-approved ones. `is_live` is now an
ALLOW-list (`status in {"live", "grandfathered"}`) — a status-less entry
is NOT live until `backfill_grandfathered_status` explicitly stamps it
`"grandfathered"`. That's still a non-destructive migration (an additive
stamp, not a content change) — it just makes the grandfathering visible
and auditable instead of an invisible default. Only an entry that this
module itself created (always starting `pending`) can ever be `pending`;
`approve()` refuses (`UnmigratedLegacyEntry`) rather than silently
touching a still-unmigrated legacy row.

Deliberately does NOT import from `library.py` — `library.py` imports
`is_live` from here (Deliverable 2), and the reverse import would be a
cycle. The small bit of "load the manifest, default to the empty shape if
the file doesn't exist yet" logic is intentionally duplicated rather than
shared for that reason.

Pure stdlib — `json`, `os`, `fcntl`, `datetime`, `pathlib`. No new deps.

umwelt#382 adds `CATEGORIES` + `validate_category` — the single canonical
definition of the `category` field's closed set, enforced on every write
through `_append_entry_locked`. The internal manifest-assembly script (not in this release)
imports the same definition so the one producer that always set this field
and the two that never did are all governed by one source, rather than
re-drifting the way the `avatar`/`avatars` split originally did.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from umwelt._ids import canonical_agent

_MANIFEST_FILENAME = "master_manifest.json"

# umwelt#382: the canonical `category` enum, derived from the code that
# already exists rather than invented fresh -- the internal manifest-assembly script
# (not in this release; the one producer that actually sets this field today) does
# an exact-match `category == "avatars"` on the plural, and `library.py`'s
# `search` filters by exact equality against whatever a producer wrote. So
# plural for the three count-noun categories, singular for the one mass
# noun -- not a tidy scheme, but the one the working producer and consumer
# already agree on; inventing a nicer-looking set would silently invalidate
# the 39 live entries that already conform to this one. `None` is not a
# member of this set -- it means "uncategorized", a distinct, still-valid
# state (the 11 pre-existing null rows; a decision on backfilling them is
# out of scope here, umwelt#382 step 5).
CATEGORIES = frozenset({"avatars", "things", "places", "environment"})


def validate_category(category: str | None) -> None:
    """Raises `ValueError` if `category` is a non-`None` value outside
    `CATEGORIES`. `None` always passes -- an uncategorized entry is a valid,
    if less useful, state (see `CATEGORIES`'s own comment); this only
    guards against a NEW misspelling/invented value being written, which is
    exactly how the `avatar`/`avatars` split this enum exists to close
    happened in the first place."""
    if category is not None and category not in CATEGORIES:
        raise ValueError(
            f"unknown category {category!r}; must be one of "
            f"{sorted(CATEGORIES)} or None")


class ManifestEntryNotFound(KeyError):
    """Raised by `approve` when no entry in the manifest carries the given
    `spec_key`. A KeyError subclass (rather than a bare `Exception`) so a
    caller that already handles dict-style "missing key" failures can catch
    it the same way; the sibling HTTP route maps it to a 404."""


class ApprovalRefused(Exception):
    """Raised by `approve` when `approver == entry["creator"]` — the
    security gate this whole module exists to enforce. A single agent may
    not self-verify AND be the one peer approval that makes its own
    generation live; the sibling HTTP route maps this to a 403. Never a
    silent no-op — the caller must see this as a hard refusal."""


class UnmigratedLegacyEntry(Exception):
    """Raised by `approve` when the entry has no `status` key at all —
    written before is_live's allow-list fix (umwelt#289/Ariadne's finding)
    and never run through `backfill_grandfathered_status`. Deliberately
    NOT treated as an ordinary pending candidate: `entry.get("creator")`
    is often absent on these rows too, so falling through to the ordinary
    approve() path would silently stamp `approved_by=<caller>` onto a row
    nobody actually peer-reviewed — manufacturing exactly the false
    attribution this module exists to prevent. The caller must run the
    migration first."""


class BytesNotStaged(Exception):
    """Raised by `approve` when the caller explicitly asserts
    `bytes_present=False` — the entry would flip `live` with nothing
    servable at its `spec_key`, the exact silent-wrong-art failure mode
    umwelt#464 slice 0's trace found: `get_or_render_sprite`'s fail-safe
    then serves a procedural fallback at what should be the approved key,
    with no error anywhere in the chain. `gateway.py`'s `library_approve`
    route avoids this today only by promoting staged bytes into the
    servable store immediately before calling `approve` — a caller
    ordering discipline, not a property of this module. `approve`'s
    `bytes_present` parameter makes it a property of the module for any
    caller willing to state that discipline in its own name; see
    `approve`'s docstring for the default's backward-compat rationale."""


def _manifest_path(sprite_dir: str | Path) -> Path:
    return Path(sprite_dir) / _MANIFEST_FILENAME


def _lock_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(manifest_path.name + ".lock")


def _now_iso() -> str:
    """UTC timestamp, `Z`-suffixed rather than `datetime.isoformat()`'s
    `+00:00` — matches the project's existing timestamp convention
    elsewhere in the codebase (e.g. `db.py`'s `now_iso`), so `created_at`
    strings here sort and compare consistently with the rest of the
    system's stamped timestamps."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _ManifestLock:
    """Advisory exclusive lock (`fcntl.flock`) on a `<manifest>.lock`
    sidecar file — NOT on the manifest file itself, because the manifest
    may not exist yet on the very first `add_pending` call (a missing file
    can't be flocked), while the lock sidecar can always be created
    (`os.O_CREAT`) regardless of whether the manifest has been written
    before. Serializes the two writers (`add_pending`, `approve`) against
    each other; readers never take this lock (see module docstring)."""

    def __init__(self, sprite_dir: str | Path) -> None:
        sprite_dir = Path(sprite_dir)
        sprite_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = _lock_path(_manifest_path(sprite_dir))
        self._fh: Any = None

    def __enter__(self) -> "_ManifestLock":
        # "a+" so the sidecar is created if absent, and never truncated —
        # its contents (empty, always) don't matter; only the flock does.
        self._fh = open(self._lock_path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._fh is not None
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


def _load_manifest_locked(manifest_path: Path) -> dict:
    """Load the manifest for mutation. Missing file -> the same empty shape
    `library.load_manifest` returns (a fresh dict each call — never a
    shared mutable default, since this one gets appended to). Malformed
    JSON propagates the parse error rather than being treated as "empty" —
    a corrupt-but-present file must never be silently clobbered by a write
    that assumes it was empty."""
    if not manifest_path.exists():
        return {"entries": [], "deferred": []}
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_manifest_locked(manifest_path: Path, manifest: dict) -> None:
    """Write-tmp-then-atomic-rename (`os.replace`), matching the same
    pattern `SpriteStore.put` already uses for the PNG blobs — a concurrent
    lock-free reader (`list_entries`, `library.resolve`/`search`) only ever
    observes the whole old file or the whole new one, never a partial
    write."""
    tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp_path, manifest_path)


_LIVE_STATUSES = frozenset({"live", "grandfathered"})


def is_live(entry: dict) -> bool:
    """`True` iff `entry["status"]` is explicitly `"live"` or
    `"grandfathered"` — an ALLOW-list, not a default. umwelt#289/Ariadne's
    finding: the prior behavior (`entry.get("status", "live") == "live"`)
    was fail-OPEN on a missing status, the opposite posture from every
    other moderation check in this codebase (#317's retire-config fails
    closed). It let 29 of 52 real entries serve as indistinguishable from
    peer-approved, because writers that bypass this module entirely (e.g.
    the internal promotion script's raw `json.dump`, not in this release) never stamp a status
    at all. A genuinely-missing status now means NOT live — fails closed,
    same as everywhere else. Entries written before this slice existed
    must go through `backfill_grandfathered_status` (below) to become
    explicitly `"grandfathered"` — a real, visible status rather than a
    silent default — before they serve again."""
    return entry.get("status") in _LIVE_STATUSES


def _append_entry_locked(
    sprite_dir: str | Path,
    spec_key: str,
    render_spec: dict,
    creator: str,
    status: str,
    *,
    label: str | None = None,
    category: str | None = None,
) -> dict:
    """Shared lock-acquire/idempotency-scan/build/atomic-write body for
    `add_pending` and `add_live` (reviewer-fairy suggestion on M4 slice 4,
    PR #318: the two public functions were ~35-line near-duplicates
    differing only in `status`, a drift risk if one changed without the
    other). `status` is always caller-fixed (`"pending"` or `"live"`),
    never a value either public wrapper exposes as a parameter — this
    keeps `add_pending`'s own signature free of any status override,
    preserving Borges's invariant (see `add_live`'s docstring) that the
    agent-facing authoring path stays structurally incapable of writing
    `live` directly. Not itself part of the public API — always called
    through one of the two named wrappers below.

    Idempotent: if an entry with this `spec_key` already exists (in ANY
    status), no new row is appended — the existing entry is returned
    unchanged, never re-flipped to `status`.

    `creator` is folded through `canonical_agent` (umwelt#464 slice 0)
    before being stored — every entry's `creator` field is guaranteed
    canonical-form on disk regardless of whether the caller pre-folded it.
    `gateway.py`'s `library_generate` route happens to fold before calling
    in already; this makes that a property of the module, not a caller
    convention a second (e.g. engine-side) caller could silently skip and
    reopen the `approver`/`creator` case-variant self-approval gap.
    """
    validate_category(category)
    creator = canonical_agent(creator)
    sprite_dir = Path(sprite_dir)
    manifest_path = _manifest_path(sprite_dir)
    with _ManifestLock(sprite_dir):
        manifest = _load_manifest_locked(manifest_path)
        entries = manifest.setdefault("entries", [])
        for entry in entries:
            if entry.get("spec_key") == spec_key:
                return entry
        entry = {
            "spec_key": spec_key,
            "render_spec": render_spec,
            "status": status,
            "creator": creator,
            "approved_by": None,
            "created_at": _now_iso(),
            "label": label if label is not None else render_spec.get("class"),
            "category": category,
        }
        entries.append(entry)
        _write_manifest_locked(manifest_path, manifest)
        return entry


def add_pending(
    sprite_dir: str | Path,
    spec_key: str,
    render_spec: dict,
    creator: str,
    *,
    label: str | None = None,
    category: str | None = None,
) -> dict:
    """Appends a new `pending` entry for `spec_key`/`render_spec`, authored
    by `creator`. Idempotent: if an entry with this `spec_key` already
    exists (in ANY status), no new row is appended — the existing entry is
    returned unchanged. This mirrors the content-addressed nature of the
    underlying store: re-adding the same spec is a no-op, never a second
    row racing the first through the approval queue.

    `asset_path`/`canonical_json` are left for the caller/CAS layer to
    populate later — this function does not fabricate them and omits them
    entirely when unknown.

    `category` (umwelt#382): validated against `CATEGORIES` before
    anything is written — an unknown non-`None` value raises `ValueError`
    rather than being accepted and silently drifting the enum. `None`
    (uncategorized) is always accepted.
    """
    return _append_entry_locked(
        sprite_dir, spec_key, render_spec, creator, "pending",
        label=label, category=category)


def add_live(
    sprite_dir: str | Path,
    spec_key: str,
    render_spec: dict,
    creator: str,
    *,
    label: str | None = None,
    category: str | None = None,
) -> dict:
    """Appends a new entry for `spec_key`/`render_spec`, already
    `status: "live"` from the moment it's written to disk — it NEVER
    passes through `pending`, and there is no approval step anywhere in
    this call. M4 slice 4's trusted-curator/ops-authority primitive:
    unlike `add_pending` (the AGENT author-path — always starts `pending`,
    only a DIFFERENT peer's `approve` call ever flips it to `live`), this
    function has no path to `pending` at all. Borges's explicit ruling
    (quoted verbatim, umwelt M4 slice-4 design review): "Keep add_pending
    structurally incapable of writing live. add_pending is the AGENT
    author-path, and the gate's whole invariant is 'the only ways to reach
    status=live are approve() (a peer) or grandfathering.' A
    status-override param on add_pending would create a SECOND unguarded
    path to live through the very function meant to enforce the gate." So
    `add_live` is a deliberately separate function, sharing only the lock
    + atomic-write mechanics with `add_pending` — never a parameter on it.

    Callable ONLY from a trusted offline context (an ops script, e.g.
    the internal generics-population script (not in this release), or a future ops-authority route — never
    THIS module's own HTTP surface, since this module has none). Must
    NEVER be exposed behind a Flask route reachable by an agent-facing
    HTTP endpoint — doing so would reopen exactly the second unguarded
    path to `live` the ruling above exists to close.

    Idempotent exactly like `add_pending`: if an entry with this
    `spec_key` already exists (in ANY status, including a still-`pending`
    one from a completely different call path), no new row is appended —
    the existing entry is returned unchanged. This function never flips an
    existing entry's status; it only ever appends a brand-new `live` one
    when no entry for this `spec_key` exists yet.

    `asset_path`/`canonical_json` are left for the caller/CAS layer to
    populate later, same as `add_pending`.

    `category` (umwelt#382): validated against `CATEGORIES`, same as
    `add_pending` — see that docstring.
    """
    return _append_entry_locked(
        sprite_dir, spec_key, render_spec, creator, "live",
        label=label, category=category)


def approve(
    sprite_dir: str | Path,
    spec_key: str,
    approver: str,
    *,
    bytes_present: bool | None = None,
) -> dict:
    """Flips the `pending` entry for `spec_key` to `live`, recording
    `approved_by=approver`.

    - Falsy `approver` (`None`, `""`, or whitespace-only) -> raises
      `ValueError`, even for an already-live entry. The check runs AFTER
      the `canonical_agent` fold, not before (a real bug the fold's own
      addition introduced and a reviewer-fairy caught, umwelt#464 slice
      0): folding first means a whitespace-only value like `"   "`
      correctly reduces to `""` and is caught here, rather than passing
      the pre-fold truthiness check and then silently writing
      `approved_by=""` — the exact audit-trail corruption this guard
      exists to prevent. A programming-contract violation, not a domain
      refusal: a caller passing no approver is a bug (e.g. an
      auth-failure path that forgot to short-circuit before calling in).
    - Unknown `spec_key` -> raises `ManifestEntryNotFound`.
    - Already-live entry (`status` is `"live"` or `"grandfathered"`) ->
      no-op, returned as-is; a re-approve never flips `approved_by` on an
      entry that's already live. `bytes_present` is not checked on this
      path — nothing is about to be marked live, so there is nothing to
      guard.
    - Entry has no `status` key at all (never migrated) -> raises
      `UnmigratedLegacyEntry`; run `backfill_grandfathered_status` first.
    - `approver == entry["creator"]` -> raises `ApprovalRefused` (the
      security gate: self-approval never flips a pending entry to live).
      Both sides are folded through `canonical_agent` at COMPARE time
      (umwelt#464 slice 0, corrected per Borges's live reproduction on
      PR #465 — an earlier version of this fold only folded `creator` at
      write time and trusted the stored value at compare time, which
      left every entry written before this change, and any written
      directly to the manifest file outside this module, comparing raw:
      creator="Sol" + approver="sol" defeated the gate). Folding
      `entry.get("creator")` again here, not just trusting `add_pending`/
      `add_live`'s write-time fold, makes the check correct for every
      row regardless of when or how it was written — not merely for rows
      created after this module started folding.
    - `bytes_present=False` -> raises `BytesNotStaged` (umwelt#464 slice 0).
      Default `None` means "caller did not check" and skips this guard
      entirely — preserves exact backward compatibility with
      `gateway.py`'s `library_approve`, which already promotes bytes into
      the servable store immediately before calling `approve` and so never
      has a `False` to pass. A new caller (the engine-side generate cap-op)
      is expected to pass `bytes_present=True` only after independently
      confirming servable bytes exist for `spec_key`, or `False` to have
      this function refuse rather than produce a `live` entry with nothing
      servable behind it. Passing `True` when bytes are not actually
      present is not detectable here — this guard catches a caller that
      *knows* it hasn't staged anything and calls in anyway, not a caller
      that's simply wrong about its own state.
    """
    approver = canonical_agent(approver)
    if not approver:
        raise ValueError("approver is required")
    sprite_dir = Path(sprite_dir)
    manifest_path = _manifest_path(sprite_dir)
    with _ManifestLock(sprite_dir):
        manifest = _load_manifest_locked(manifest_path)
        entries = manifest.get("entries", [])
        entry = None
        for candidate in entries:
            if candidate.get("spec_key") == spec_key:
                entry = candidate
                break
        if entry is None:
            raise ManifestEntryNotFound(spec_key)

        if is_live(entry):
            # Keep it simple, per design: an already-live entry is a no-op
            # regardless of who's calling now — never re-flip approved_by.
            return entry

        if "status" not in entry:
            raise UnmigratedLegacyEntry(
                f"{spec_key!r} has no status field — run "
                f"backfill_grandfathered_status(sprite_dir) before "
                f"approving or registering entries in this sprite_dir")

        if approver == canonical_agent(entry.get("creator")):
            raise ApprovalRefused(
                f"approver {approver!r} must differ from creator "
                f"{entry.get('creator')!r} (self-approval is refused)")

        if bytes_present is False:
            raise BytesNotStaged(
                f"{spec_key!r} has no bytes staged in the servable store — "
                f"refusing to mark it live (umwelt#464 slice 0). Stage the "
                f"bytes first, or omit bytes_present if this caller does "
                f"not track staging state.")

        entry["status"] = "live"
        entry["approved_by"] = approver
        _write_manifest_locked(manifest_path, manifest)
        return entry


def list_entries(
    sprite_dir: str | Path, *, status: str | None = None, sort: str = "created_at",
) -> list[dict]:
    """Returns `entries`, optionally filtered to a single `status` (`None`
    = all statuses), sorted ascending by the `sort` key (default
    `created_at`, so the approval queue is oldest-first and never starves
    an old pending entry). An entry missing the sort key sorts first
    (treated as epoch) rather than raising or sorting last.

    Lock-free read (see module docstring) — the atomic rename on every
    write means this never observes a partially-written file.
    """
    manifest_path = _manifest_path(Path(sprite_dir))
    if not manifest_path.exists():
        return []
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)
    entries = manifest.get("entries", [])
    if status == "live":
        # Reviewer-fairy catch on PR #416: exact-string equality here would
        # permanently exclude "grandfathered" entries from a `status="live"`
        # query -- not just during the migration gap, forever, since
        # "grandfathered" is a genuinely distinct string from "live" by
        # design (that's what makes it auditable). But every caller asking
        # for `status="live"` means "what's actually servable" (e.g.
        # gateway.py's `/library?status=live`, which feeds the avatar
        # picker) -- the same semantic is_live() already encodes. Route
        # through is_live() for this one value so the two never diverge
        # again; every OTHER status stays an exact match (there is no
        # other status with a synonym to fold in).
        entries = [e for e in entries if is_live(e)]
    elif status is not None:
        # No default (umwelt#289/Ariadne's finding): a status-less entry
        # is unclassified, not implicitly "live" — same allow-list posture
        # as is_live(). It matches no status= filter (other than "live",
        # handled above) until migrated via backfill_grandfathered_status.
        entries = [e for e in entries if e.get("status") == status]
    # Missing sort key -> "" -> sorts before every non-empty ISO8601
    # timestamp string (or any other real value), i.e. "treated as epoch".
    return sorted(entries, key=lambda e: e.get(sort) or "")


def backfill_grandfathered_status(sprite_dir: str | Path) -> list[dict]:
    """One-time migration: stamps `status: "grandfathered"` (plus a
    `grandfathered_at` timestamp — Borges's PR #416 review, see below) onto
    every entry with no `status` key at all — the ones written before
    `is_live`'s allow-list fix (umwelt#289/Ariadne's finding) by a path
    that bypassed `add_pending`/`add_live` entirely (e.g.
    the internal promotion script's pre-fix raw `json.dump`, not in this release). Makes the
    prior silent default (`status`-less reads as `live`) into an explicit,
    auditable value instead — the backfill worklist Ariadne's finding
    hands us: every entry this touches is one that never passed peer
    review and should eventually get a real second eye.

    `grandfathered_at` matters beyond a timestamp for its own sake: a bare
    status stamp can't distinguish WHEN an entry was grandfathered, so if
    a bypass writer ever produces new status-less rows in the future, a
    later backfill run would silently absorb them into the same bucket as
    this migration's original cohort — concealing the exact class of
    problem this fix exists to surface. A distinct `grandfathered_at` per
    RUN makes each cohort legible: a later timestamp appearing after the
    initial migration's is direct evidence a bypass still exists, turning
    this from a one-time cleanup into a standing detector.

    Idempotent and safe to re-run: only touches entries where `"status"
    not in entry`; an entry already carrying any status (including a
    prior `"grandfathered"` stamp, whatever its `grandfathered_at`) is
    left untouched — a re-run never overwrites an existing timestamp.
    Same lock + atomic-write mechanics as every other writer in this
    module, so it can run concurrently with `add_pending`/`approve`/
    `add_live` without racing them.

    Must run BEFORE (or atomically with) deploying the `is_live` fix
    against a sprite_dir that has status-less entries — until it runs,
    those entries serve as live (old behavior); after `is_live`'s fix
    deploys but before this runs, they stop serving (fail closed) rather
    than silently regaining the old fail-open behavior. Returns the list
    of entries this call actually migrated (empty list if none needed
    it), so a caller can log/verify the backfill count.
    """
    sprite_dir = Path(sprite_dir)
    manifest_path = _manifest_path(sprite_dir)
    with _ManifestLock(sprite_dir):
        manifest = _load_manifest_locked(manifest_path)
        entries = manifest.get("entries", [])
        migrated = []
        for entry in entries:
            if "status" not in entry:
                entry["status"] = "grandfathered"
                # Borges's PR #416 review: a bare status stamp can't
                # distinguish WHEN an entry was grandfathered. Without
                # this, a future bypass writer producing new status-less
                # rows would have them silently absorbed into the same
                # "grandfathered" bucket on the next backfill run,
                # indistinguishable from the original legacy cohort --
                # exactly the concealment this fix exists to prevent, one
                # layer down. `grandfathered_at` makes each backfill RUN
                # legible as its own cohort: a T1 timestamp appearing
                # after T0's initial migration is direct evidence a bypass
                # still exists, not just a note for this one-time cleanup.
                entry["grandfathered_at"] = _now_iso()
                migrated.append(entry)
        if migrated:
            _write_manifest_locked(manifest_path, manifest)
        return migrated


def merge_deferred(sprite_dir: str | Path, new_deferred: list[dict]) -> list[dict]:
    """Lock-safe writer for the manifest's `deferred` list — umwelt#418, a
    follow-up from #416's review (Borges): that PR rewrote
    the internal promotion script (not in this release) to stop raw-overwriting the whole
    destination file, but had no lock-safe way to carry the curation
    source's `deferred` entries (names deliberately NOT shipped, with a
    stated reason — see the internal manifest-assembly script's docstring, not in this release) into
    the destination, so it stopped copying them at all. `deferred` isn't
    read by any serving code, so that was never a functional regression —
    but it silently discarded real curator-authored audit information,
    which this closes properly instead of leaving unaddressed.

    Merges `new_deferred` into the manifest's existing `deferred` list,
    deduplicated by `label` (the same natural key
    the internal promotion script's own docstring (not in this release) already treats as
    unique across the whole manifest — deferred entries have no
    `spec_key` of their own, since a deferred item was never rendered).
    An incoming entry whose `label` already exists in the destination's
    `deferred` list is skipped — first-write-wins, same idempotent
    posture as `add_pending`/`add_live`: re-running a promote against the
    same curation source never duplicates or overwrites an existing
    deferred record's reason.

    Raises `ValueError` on an entry with no `label` at all (or a falsy
    one), rather than silently treating it as a match against any other
    label-less entry — Borges's PR #419 review: `entry.get("label")`
    returns `None` for a malformed entry, so a first label-less entry
    would silently seed `None` into the dedup set and every SUBSEQUENT
    label-less entry would then be skipped as a "duplicate" of it,
    collapsing distinct curator-authored records. This is a public module
    function, not scoped to the internal promotion script's own well-formed
    producer (today's real `deferred` data is 5/5 labelled, so this can't
    fire yet) — house style is fail loud rather than let a future caller
    silently lose exactly the audit data this function exists to
    preserve.

    Same lock + atomic-write mechanics as every other writer in this
    module. Returns the list of entries this call actually added (empty
    if every `label` was already present).
    """
    sprite_dir = Path(sprite_dir)
    manifest_path = _manifest_path(sprite_dir)
    with _ManifestLock(sprite_dir):
        manifest = _load_manifest_locked(manifest_path)
        deferred = manifest.setdefault("deferred", [])
        existing_labels = {e.get("label") for e in deferred}
        added = []
        for entry in new_deferred:
            label = entry.get("label")
            if not label:
                raise ValueError(
                    f"deferred entry has no label: {entry!r} — merge_deferred "
                    f"dedups by label and cannot merge an unlabelled entry")
            if label in existing_labels:
                continue
            entry = dict(entry)
            deferred.append(entry)
            existing_labels.add(label)
            added.append(entry)
        if added:
            _write_manifest_locked(manifest_path, manifest)
        return added
