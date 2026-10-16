"""Single source of truth for umwelt's agent-identity case-fold (umwelt #73).

`Lei` and `lei` used to mint separate identities (separate home posts,
separate avatars) — the fix folds every caller-supplied agent identity to
one canonical form before it's used as a lookup/storage key. That fold has
to be computed EXACTLY the same way everywhere it happens: at every live
server door (server.py), by the one-time repair migration
(migrate_casefold.py) that merges pre-fix duplicates, and by any client that
needs to predict the server's canonical form locally (mud.py). If the fold
ever forks — even by one call site quietly using `.lower()` instead of this
function, or skipping the `.strip()` — the migration merges duplicates under
one rule while a live door (or client) folds under a slightly different one,
and split identities can reappear post-migration. Route every fold through
this one function, no exceptions.
"""

from __future__ import annotations

import os
import re


# umwelt #915: the longest place id the server will mint from caller input.
# Since #914 the MCP client carries a place id verbatim inside the commands a
# capped reply prints (crossings_here(place=...), trace(...), hear(place=...))
# up to this many chars, and prints a placeholder beyond it -- so an id over
# the cap is the one case where a printed command cannot be run as printed.
# mcp_server._CROSSINGS_CMD_ID_MAX IS this constant (imported, not copied), so
# the two cannot drift.
PLACE_ID_MAX_LEN = 64


def canonical_agent(raw: str | None) -> str:
    """The single canonical agent-identity fold.

    `.casefold()` (not `.lower()`) is the Unicode-correct case fold — it
    matches `.lower()` byte-for-byte on the ASCII identities in use today
    (no test breakage), but is the correct choice for any future non-ASCII
    agent id (e.g. German ß, Turkish dotted/dotless I), where `.lower()` and
    `.casefold()` can diverge.
    """
    return (raw or "").strip().casefold()


# umwelt #381 / #1034: the human roster, as WORLD CONFIG rather than engine
# code. The engine ships with NO built-in humans; a world names its own by
# setting `UMWELT_HUMAN_RESIDENTS` to a comma-separated list of handles.
# Values come back in CANONICAL (casefolded) form, matching `canonical_agent`'s
# output -- every comparison site is expected to fold its input before testing
# membership, not to `.lower()` this set. The env is read at CALL time, so a
# test (or an operator restarting the world) sees the current value without a
# module reload.
#
# This is the FALLBACK layer, not the primary human check. The primary path
# is a registered avatar's `type == "human"` metadata (db.is_human_sender);
# this exists only to cover the window before a human has called
# /umwelt/register, when there's no avatar to read a `type` off. Once
# registration is guaranteed before any consent-or-rate decision can be
# reached, this fallback should be deleted rather than grown further.
#
# Read by the engine's fallback checks (db.is_human_sender and the identity
# `kind` seeding in db.py). History: webrender/session.py once kept its own
# `ROSTER` copy of this fact and the two drifted apart (#379); umwelt#495
# removed `ROSTER` entirely, so nothing is left to drift against.


def human_residents() -> frozenset[str]:
    """The configured human-resident handles, canonicalised (see above).

    Unset, empty or whitespace-only -> `frozenset()`. Entries are stripped,
    folded with `canonical_agent`, and empty entries dropped. No other
    validation and no raise: a handle the world never registers is just a
    name that matches nothing.
    """
    raw = os.environ.get("UMWELT_HUMAN_RESIDENTS") or ""
    return frozenset(
        name for name in (canonical_agent(part) for part in raw.split(",")) if name
    )


# umwelt #495: the default on-disk path for an agent's ed25519 identity key.
#
# This is the ONE place the default path string lives. Both sides of the
# login flow import it from here so writer and reader cannot drift:
#   - tools/bind_client_agent.py writes the key here (it stages the keypair
#     during an identity bind).
#   - src/umwelt/client_auth.py reads it here (it loads the private key to
#     sign login challenges).
#
# PLACEHOLDER pending ratification by the login-design seat — the path may
# move when the enrollment-location question is settled. Both consumers
# override it their own way (binder: --key-path CLI flag; reader:
# UMWELT_IDENTITY_KEY env var), so moving the default is a one-line change
# here, not a two-file hunt.
DEFAULT_IDENTITY_KEY_PATH = os.path.expanduser("~/.umwelt/identity_key")


# umwelt #185 rule 4 / #978: thing ids are uuid4, unprefixed (db.py mints them
# via uuid.uuid4()). A ref shaped like one but absent from a client's fetched
# candidate view is trusted through to the server unchanged -- the server stays
# the authority on whether it resolves (e.g. a thing nested inside a container
# the view does not enumerate). ONE definition, shared by mcp_server's
# `_resolve_thing_ref` / `_resolve_look_at_target` and tools/uw's
# `_resolve_look_at_target`, so the shape cannot drift between clients.
# `\A...\Z`, not `^...$`: `$` also matches before a trailing newline, which
# would admit "<uuid>\n" (same tightening as umwelt #295 made for the ordinal
# regex). IGNORECASE is intended: an UPPERCASE uuid is id-shaped.
THING_ID_SHAPE_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE)


def looks_like_thing_id(ref: str) -> bool:
    return bool(THING_ID_SHAPE_RE.match(ref))
