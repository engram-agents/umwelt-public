"""Art-library matcher (M4 prep, umwelt art-library slice 1).

Reads the curated master manifest (built by internal art-curation tooling,
not in this release, reviewed by Ariadne) and answers two questions against it:

- `resolve` — "do I already have curated art for this EXACT render_spec?" (the
  manifest-level analog of `SpriteStore.has`, keyed the SAME way — imports
  `spec_key`/`canonical_json` from `sprites.py` rather than reimplementing the
  hash, so this can never drift from what the gateway actually computes).
- `search` — "what's the closest curated art to this spec, by keyword overlap?"
  a Jaccard-similarity ranking over `class` + `descriptors`, scoped to an
  optional `category`.

Pure read/matcher module — no data-movement side effects. The manifest lives
ALONGSIDE its assets in the sprite-store's own external directory
(`<sprite_dir>/master_manifest.json`), copied there by the separate one-shot
promotion script (internal tooling, not in this release) — this module never reaches into the
`/home/agents-shared/` curation workspace itself (see that script's docstring
and the design-of-record for why: the runtime gateway must not couple to a
same-host curation path that can change under it).

`resolve` is now wired into `gateway.py`'s `/scene` render path as of M4
slice 4 (`_apply_render_spec`'s tier-1 exact-match check) — this module
itself is unchanged by that wiring, still a pure read/matcher with no
data-movement side effects. `search` (keyword-similarity ranking) remains
unwired anywhere in `gateway.py` — the `/library` browse route (slice 3)
calls `manifest.list_entries` directly, not this module's `search`; wiring
`search` into an actual keyword-lookup surface is still open future work.

M4 slice 2 (`manifest.py`) adds a pending/live status machine on top of this
same manifest file; `resolve`/`search` here import `is_live` from it and
only ever serve `live` entries — see `manifest.py`'s module docstring for
the moderation gate this exists to enforce.

No third-party imports — stdlib only (`json`, `pathlib`, set operations).
"""

from __future__ import annotations

import json
from pathlib import Path

from .manifest import is_live
from .sprites import spec_key

# The empty shape `load_manifest` returns for a fresh install with no
# manifest promoted yet — same `entries`/`deferred` keys the real manifest
# uses, just empty, so callers never need a special-case branch for "no
# manifest on disk yet" vs. "manifest with zero entries".
_EMPTY_MANIFEST: dict = {"entries": [], "deferred": []}


def load_manifest(sprite_dir: str | Path) -> dict:
    """Reads `<sprite_dir>/master_manifest.json` and returns the parsed dict.

    Returns the empty manifest shape (`{"entries": [], "deferred": []}`)
    without crashing if the file doesn't exist yet — e.g. a fresh install
    before the promotion script (internal tooling, not in this release) has ever been run.
    """
    manifest_path = Path(sprite_dir) / "master_manifest.json"
    if not manifest_path.exists():
        return dict(_EMPTY_MANIFEST)
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def resolve(render_spec: dict, manifest: dict) -> dict | None:
    """Exact match: compute `spec_key(render_spec)` (imported from
    `sprites.py`, never reimplemented) and look up the manifest entry whose
    own `spec_key` matches it.

    Returns the matching entry dict, or `None` if there's no exact match.
    This is the "already have curated art for this exact spec" fast path —
    the manifest-level analog of `SpriteStore.has`. Never considers
    `deferred` entries (they carry no `spec_key` to match against anyway).

    Only considers **live** entries (`manifest.is_live`, M4 slice 2's
    moderation gate) — a `pending` entry, not yet peer-approved, never
    resolves here regardless of `spec_key` match, so the world falls back
    to procedural rendering until a peer approves it. A status-less legacy
    entry counts as live (back-compat grandfather rule).
    """
    key = spec_key(render_spec)
    for entry in manifest.get("entries", []):
        if entry.get("spec_key") == key and is_live(entry):
            return entry
    return None


def _tokenize(class_: str, descriptors: list[str]) -> set[str]:
    """Turn a `class` string + a list of descriptor phrases into a lowercase
    token set for the Jaccard match.

    Tokenization choice (flagged as a judgment call — the spec left this
    open): lowercase everything, split BOTH the class string and each
    descriptor phrase on whitespace into individual words, union all of them
    into one flat set. E.g. `class_="chair"`,
    `descriptors=["one single empty wooden chair", "no person"]` tokenizes to
    `{"chair", "one", "single", "empty", "wooden", "no", "person"}` (dedup
    folds the repeated "chair" from the class into the one from the
    descriptor). This isn't precision-critical — it's a keyword heuristic by
    design (ship keyword, measure in real use, add embeddings only if keyword
    demonstrably fails).
    """
    tokens: set[str] = set()
    for phrase in [class_, *descriptors]:
        if not phrase:
            continue
        tokens.update(phrase.lower().split())
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    """|A ∩ B| / |A ∪ B|. Two empty sets score 0.0 (never divide by zero —
    there's no meaningful overlap to report between two empty queries)."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def search(class_: str, descriptors: list[str], category: str | None,
           manifest: dict, top_n: int = 5) -> list[dict]:
    """Keyword-overlap ranking over `manifest["entries"]` (never
    `manifest["deferred"]` — those entries carry no `render_spec` to score
    against and are skipped entirely), optionally scoped to `category`.

    Only considers **live** entries (`manifest.is_live`, M4 slice 2's
    moderation gate) — a `pending` entry is excluded from the ranking
    entirely, the same as an out-of-category one, so it can never be the
    closest curated match served to a caller. A status-less legacy entry
    counts as live (back-compat grandfather rule).

    Score is Jaccard similarity (not raw overlap-count), per the converged
    design: for a query token set built from `class_` + `descriptors`, and
    each candidate entry's own `render_spec["class"]` +
    `render_spec["descriptors"]` (tokenized the same way — see `_tokenize`),
    `score = |query ∩ candidate| / |query ∪ candidate|`. Raw overlap-count
    would reward over-tagged entries (more tags = more chances to overlap
    with the query, regardless of how much of the CANDIDATE's own tag set is
    actually relevant); Jaccard folds specificity into the score, so a
    sprite tagged with exactly the query's terms beats one tagged with many
    terms that merely happen to include them.

    Ties are broken by manifest order (the entry's original index position
    in `entries`) — deterministic and stable, since there's no reliable
    timestamp to sort by instead.

    Returns the top `top_n` entries (dicts, same shape as manifest entries).
    """
    query_tokens = _tokenize(class_, descriptors)
    entries = manifest.get("entries", [])

    scored: list[tuple[float, int, dict]] = []
    for idx, entry in enumerate(entries):
        if not is_live(entry):
            continue
        if category is not None and entry.get("category") != category:
            continue
        render_spec = entry.get("render_spec") or {}
        candidate_tokens = _tokenize(
            render_spec.get("class", ""), render_spec.get("descriptors", []))
        score = _jaccard(query_tokens, candidate_tokens)
        scored.append((score, idx, entry))

    # Sort by score descending; ties broken by manifest order (idx)
    # ascending — stable/deterministic, never recency.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [entry for _score, _idx, entry in scored[:top_n]]
