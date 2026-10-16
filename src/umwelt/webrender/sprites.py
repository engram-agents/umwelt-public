"""Umwelt Web Render — sprite pipeline + sprite store (M2).

Implements RENDER-PIPELINE-AND-SECURITY.md §A.4's pipeline, backend-agnostic
(the `renderer` callable is swappable — `ProceduralRenderer.generate` today,
an `ImageModelRenderer` at M4):

    key = sha256(canonical_json(render_spec))
    if store.has(key): return sprite_ref
    raw = renderer(render_spec, seed=key_as_int)
    store.put(key, raw)
    return sprite_ref  # "/sprite/<key>"

Sprite store v0 (per the doc): a gateway-local directory,
`src/umwelt/webrender/sprites/`, created on demand
(`os.makedirs(..., exist_ok=True)` via `Path.mkdir`). This directory holds
ONLY runtime-generated cache content — never committed (see `.gitignore`).

**M4 slice 1 addition (umwelt#319, `ob_0483`/`ob_0484`) — the moderation-gate
serve-path fix.** Before this, `get_or_render_sprite`'s cache-hit was a pure
file-exists check with no awareness of `manifest.py`'s pending/live status —
the B.6 moderation gate (manifest.py's `add_pending`/`approve`) governed the
manifest's own bookkeeping, but nothing stopped a cache-hit at a PENDING
spec_key's slot from being served as if it were approved. `ManifestStatusIndex`
closes that: an in-memory `spec_key -> is_live` view, invalidated by the
manifest file's own mtime (not a push signal — the manifest may be written by
a different process/worker, so an in-process invalidation hook alone would be
wrong under concurrent gateway workers; stat-ing the file is cheap, and a full
reload only happens when the file actually changed). `get_or_render_sprite`
consults it on every cache-hit; `gateway.py`'s `GET /sprite/<key>` route
(the OTHER, more direct path to the same bytes — spec_key is a deterministic
hash of enumerable closed-taxonomy content, so it is not a secret, and a
direct GET bypasses `get_or_render_sprite`/`/scene` entirely) consults it too.
Both fail safe on a non-live hit — see each call site's own comment.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from . import manifest as _manifest
from .render import generate as _procedural_generate

# Default gateway-local cache dir, per §A.4 ("sprites/<key>.png"). Created
# lazily by SpriteStore.__init__, not at import time (so importing this
# module never has a filesystem side effect).
_DEFAULT_SPRITE_DIR = Path(__file__).resolve().parent / "sprites"

# `spec_key` always produces a sha256 hex digest — exactly 64 lowercase hex
# characters. Post-review hardening (Aleph's off-host review, umwelt #211):
# `GET /sprite/<key>` used to build a filesystem path straight from the
# URL-supplied `key` with no validation that it actually looks like a
# content-hash. Traversal (`..`) and an absolute-path replace were blocked
# only incidentally — Flask's default `<key>` route converter refuses a
# literal `/`, which is safe-by-framework, not safe-by-construction (the
# same shape as F1/F2 earlier today). A non-hex-64 key is definitionally
# not a real sprite key, so `SpriteStore` now refuses to even build a path
# for one — the filesystem is never touched with an unvalidated string.
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(render_spec: dict) -> str:
    """Sorted-keys, no-whitespace JSON — key ordering never affects the
    content hash."""
    return json.dumps(render_spec, sort_keys=True, separators=(",", ":"))


def spec_key(render_spec: dict) -> str:
    """The sha256 hex digest of `render_spec`'s canonical JSON — the sprite
    store's content-addressed key."""
    return hashlib.sha256(canonical_json(render_spec).encode("utf-8")).hexdigest()


def derive_seed(key: str) -> int:
    """The deterministic generation seed for a `spec_key`, shared by every
    caller of the art-gen service (`gateway.py`'s `library_generate` route,
    `cap_handler.py`'s `_run_generate_job`) so the same spec always derives
    the same seed regardless of which caller requested it — factored out
    (umwelt#476) after the two call sites carried this formula inline,
    independently, and could have silently drifted.

    umwelt#339: only the first 16 hex chars (64 bits) — the full 64-char
    digest (256 bits) overflows `torch.Generator.manual_seed`'s uint64_t
    range. Still fully determined by `key` — repeat requests for the same
    render_spec always get the same seed."""
    return int(key[:16], 16)


# umwelt#289/Borges's review of PR #416: is_live's allow-list fix made a
# status-less entry fail CLOSED instead of open -- correct, but silent on
# this specific hot path (approve() is loud, ManifestStatusIndex wasn't).
# One-shot-per-sprite_dir stderr warning, same idiom as server.py's
# `_GRANDFATHERED_PARAMS_WARNED` (shipped for the identical shape of
# problem: a value predates a rule and needs a one-time remedy, not a
# per-call nag). Keyed on the resolved manifest path rather than a bare
# flag so multiple sprite_dirs in one process (e.g. tests) warn
# independently; the set can only grow to the number of distinct
# sprite_dirs a process ever touches, never unbounded on caller input.
_UNMIGRATED_STATUS_WARNED: set[Path] = set()


class ManifestStatusIndex:
    """In-memory `spec_key -> is_live` view over `<sprite_dir>/master_manifest.json`
    (M4 slice 1, umwelt#319 — see module docstring). Lazily built and
    mtime-invalidated: cheap on the hot path (a `stat()` call) except when
    the manifest file has actually changed since the last read, in which
    case it does one full `manifest.list_entries` load.

    `status(key)` returns:
      - `True`  — a live manifest entry exists for this key: `status` is
        explicitly `"live"` or `"grandfathered"` (umwelt#289/Ariadne's
        finding fixed the prior fail-open default — a status-less entry
        is now `False` here until `manifest.backfill_grandfathered_status`
        runs, not silently served).
      - `False` — a manifest entry exists but is NOT live (pending,
        status-less/unmigrated, or any other non-live status) — the
        caller must not serve this key's bytes.
      - `None`  — no manifest entry at all for this key. Not gated content
        (pure-procedural output never touches the manifest, only the sprite
        cache) — callers treat this the same as before this index existed.
    """

    def __init__(self, sprite_dir: str | Path) -> None:
        self._manifest_path = Path(sprite_dir) / "master_manifest.json"
        self._fingerprint: tuple[int, int] | None = None
        self._live_by_key: dict[str, bool] = {}
        # Sentinel distinct from `None` (no file yet) so a first real
        # `stat()` failure and "never checked" aren't conflated.
        self._checked = False

    def _refresh_if_stale(self) -> None:
        try:
            st = self._manifest_path.stat()
            # (inode, mtime_ns) rather than mtime alone: `manifest.py`
            # writes via write-tmp-then-`os.replace` (atomic rename), which
            # means every write lands the path on a FRESH inode -- a signal
            # that survives even when two writes race the same mtime clock
            # tick (observed live: `add_pending` immediately followed by
            # `approve` in the same test/process can share an `st_mtime`
            # depending on filesystem timestamp resolution; the inode never
            # collides that way since it's a genuinely different underlying
            # file each time).
            fingerprint = (st.st_ino, st.st_mtime_ns)
        except FileNotFoundError:
            fingerprint = None
        if self._checked and fingerprint == self._fingerprint:
            return
        self._checked = True
        self._fingerprint = fingerprint
        if fingerprint is None:
            self._live_by_key = {}
            return
        entries = _manifest.list_entries(self._manifest_path.parent)
        self._live_by_key = {
            entry["spec_key"]: _manifest.is_live(entry)
            for entry in entries
            if entry.get("spec_key")
        }
        unmigrated = sum(1 for e in entries if "status" not in e)
        if unmigrated and self._manifest_path not in _UNMIGRATED_STATUS_WARNED:
            _UNMIGRATED_STATUS_WARNED.add(self._manifest_path)
            print(
                f"[manifest-unmigrated] {unmigrated} entries in "
                f"{self._manifest_path} have no `status` and are NOT being "
                "served. Run `PYTHONPATH=src python3 "
                "tools/backfill_manifest_status.py --sprite-dir "
                f"{self._manifest_path.parent}` to stamp them "
                '"grandfathered" (umwelt#289).',
                file=sys.stderr,
            )

    def status(self, key: str) -> bool | None:
        self._refresh_if_stale()
        return self._live_by_key.get(key)


class SpriteStore:
    """Gateway-local directory-backed sprite cache, keyed by `spec_key`."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.dir = Path(directory) if directory is not None else _DEFAULT_SPRITE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.status_index = ManifestStatusIndex(self.dir)

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.png"

    def has(self, key: str) -> bool:
        if not _KEY_RE.fullmatch(key):
            return False
        return self._path(key).exists()

    def get(self, key: str) -> bytes | None:
        if not _KEY_RE.fullmatch(key):
            return None
        p = self._path(key)
        if not p.exists():
            return None
        return p.read_bytes()

    def put(self, key: str, data: bytes) -> None:
        # Write-then-rename so a concurrent GET /sprite/<key> never observes
        # a partially-written file.
        p = self._path(key)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)


def get_or_render_sprite(
    render_spec: dict,
    store: SpriteStore,
    renderer: Callable[[dict, int], bytes] = _procedural_generate,
) -> str:
    """§A.4's pipeline. `renderer` defaults to `ProceduralRenderer.generate`
    but is swappable (dependency-injected) — both for a future
    `ImageModelRenderer` backend and for test-time call counting to prove
    the dedup path never re-renders on a cache hit.

    M4 slice 1 (umwelt#319) fail-safe: a cache-hit whose `store.status_index`
    says NOT live (a pending, not-yet-approved generation — this should never
    happen given the write-side discipline that quarantines pending bytes to
    a separate staging path before `approve()` promotes them here, but this
    is the read-side backstop for that invariant) is never returned. Instead
    this renders PROCEDURALLY (never the caller's `renderer` — this branch IS
    the safety fallback, so it must not re-invoke whatever produced the
    tainted content in the first place) under a DISTINCT, deterministically
    derived key, so the fallback is itself cacheable/idempotent and — just as
    important — never overwrites the pending slot it's guarding against.
    """
    key = spec_key(render_spec)
    if store.has(key):
        if store.status_index.status(key) is False:
            fallback_key = hashlib.sha256(
                f"{key}:procedural-fallback".encode("utf-8")).hexdigest()
            if not store.has(fallback_key):
                seed = int(fallback_key, 16)
                raw = _procedural_generate(render_spec, seed)
                store.put(fallback_key, raw)
            return f"/sprite/{fallback_key}"
        return f"/sprite/{key}"
    seed = int(key, 16)
    raw = renderer(render_spec, seed)
    store.put(key, raw)
    return f"/sprite/{key}"
