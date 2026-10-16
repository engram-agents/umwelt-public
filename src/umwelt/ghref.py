"""ghref.py — shared gh_ref shape parsing/validation (umwelt#463).

Single source of truth for the project-board `gh_ref` convention, imported
by both `server.py` (write-time validation, engine-side) and
`gh_reconcile.py` (read-time resolution, out-of-process reconciler side) so
the two can never drift apart — the exact failure mode #463 was filed to
close (three conventions accreted because the write route accepted any
string, and the reconciler only ever recognized one of them).

**Deliberately stdlib-only** (just `re`) — this module must be safe for
`server.py` to import without pulling `gh_reconcile.py`'s `subprocess`/
`urllib` imports into every engine start. `server.py` is hermetic by design
(zero outbound network, no `gh` subprocess) and never imports
`gh_reconcile.py` directly — see that module's own docstring. Both files
import from here instead, so the dependency arrows point engine→shared and
tool→shared, never engine→tool.
"""

from __future__ import annotations

import re

# Canonical accepted/parsed shapes: "pr/<N>" and "issue/<N>", case-insensitive.
_CANONICAL_RE = re.compile(r"^(pr|issue)/(\d+)$", re.IGNORECASE)

# The live board's actually-used issue convention (umwelt#463 design round,
# Ariadne's census: 3/3 live issue anchors are "issue#<N>", 0/0 are
# "issue/<N>"). Unambiguous — it names its own kind — so recognizing it
# carries none of the bare-number guess-or-halt hazard. "pr#<N>" is
# deliberately NOT accepted here: unobserved on the live board, and this
# module tracks observed conventions, not anticipated ones.
_ISSUE_HASH_RE = re.compile(r"^issue#(\d+)$", re.IGNORECASE)


def parse_gh_ref(gh_ref: str):
    """Parse a gh_ref into (kind, number), kind in {'pr', 'issue'}
    (lowercased). Returns None for any unrecognized shape — callers must
    treat None as unresolvable ('unknown'), never guess a kind.

    Recognizes "pr/<N>", "issue/<N>", and "issue#<N>" (the read side must
    still recognize issue#<N> for rows written before #463's write-time
    normalization existed, and for the window before the backfill,
    umwelt#463 item 3, normalizes the 5 live bare-number rows separately).
    """
    if not gh_ref:
        return None
    stripped = gh_ref.strip()
    m = _CANONICAL_RE.match(stripped)
    if m:
        return m.group(1).lower(), m.group(2)
    m = _ISSUE_HASH_RE.match(stripped)
    if m:
        return "issue", m.group(1)
    return None


def normalize_gh_ref(raw: str):
    """Validate + normalize a gh_ref value supplied at write time.

    Returns (normalized_value, None) on success, or (None, error_message)
    on rejection — callers surface error_message in a 422.

    Accepted:
      - "pr/<N>" / "issue/<N>" (case-insensitive) — passed through,
        lowercased to the canonical `kind/N` form.
      - "issue#<N>" (case-insensitive) — silently normalized to
        "issue/<N>" (not named as "rejected" in any error text, since it
        is accepted, just reshaped).
    Rejected, with a clear error naming the accepted shapes:
      - a bare number — ambiguous (pr or issue?); the writer must say
        which kind. GitHub's shared PR/issue numbering makes a bare
        number *resolvable* by querying, but umwelt#463 treats any such
        auto-resolution as a deliberate, separate, logged step — never a
        silent guess at write time.
      - anything else (typos, "pr#N", "pull/N", arbitrary strings).
    """
    value = raw.strip()
    m = _CANONICAL_RE.match(value)
    if m:
        return f"{m.group(1).lower()}/{m.group(2)}", None
    m = _ISSUE_HASH_RE.match(value)
    if m:
        return f"issue/{m.group(1)}", None
    return None, (
        f"gh_ref must be pr/<N> or issue/<N> (got {raw!r}) — a bare "
        "number doesn't say which kind, and no other shape is recognized"
    )
