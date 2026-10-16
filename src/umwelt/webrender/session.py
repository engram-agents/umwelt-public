"""Umwelt Web Render — session store (M3, extended for end-to-end password
login per umwelt#495).

M3's first write-capable milestone needs SOME notion of "which human is
sitting at this browser" before `/act` can resolve an acting agent
server-side (never trusting a client-supplied `agent`/`actor` field — see
gateway.py's `/act` route docstring). This module is the in-memory session
layer that backs that: a human logs in with a username + password,
`/session/login` proxies that to the world's own `POST /umwelt/login`
(real scrypt/ed25519 verification, umwelt#495), and only on the WORLD's
success does this store mint a gateway session — the gateway hands back an
opaque session cookie + a separate CSRF token, and every subsequent
`/act` call resolves its actor from the cookie, never the request body.

**The ROSTER frozenset is GONE (umwelt#495).** Before tonight, this module
held its own hardcoded idea of who exists (a closed set of three names) and
refused anyone else at the session layer, before the world was ever asked.
That was the gateway holding authority it should never have had — Lei's
own framing: "the web gateway should be a bridge to the underlying world
engine." The world's `identities` table is now the only roster; this store
no longer validates identity at all, because by the time `create()` is
called, `/session/login` has already received a 200 from
`POST /umwelt/login` — this store's only job is to remember what the
world just told it.

**New fields, carrying the world's own credential forward:**

- `world_token`: the token `POST /umwelt/login` returned. Threaded back
  out to `_world_request` on every session-gated call as
  `Authorization: Bearer <world_token>` — this is what lets
  `resolve_actor` (umwelt#495 slice 1a) resolve identity from a real
  credential instead of a caller-declared field, once a route is wired
  to it. This gateway session cookie and the world's own session token
  are TWO DIFFERENT bearer credentials protecting two different layers
  (browser<->gateway vs gateway<->world) — never conflate them, never
  let the world token leak into the browser (it stays server-side in
  this store, keyed by the gateway's own opaque cookie token).
- `state`: `"verified"` or `"unverified"`, copied verbatim from the
  world's own response (umwelt#495's three-state rule, Kepler's PR #559
  ruling). MUST be surfaced to the client, visibly — an unverified
  session is not a security hole to hide, it is the backfill window
  doing exactly what it is designed to do, and the design's own
  anti-silent-fail-open requirement says this state may never be
  inferable only from a field a caller might not bother to check.
- `expires_at`: the world's own session TTL (`AGENT_SESSION_TTL_SECONDS`,
  currently 24h), copied verbatim — NOT independently computed here.
  **Now ENFORCED** (Mira's #605 security review): `get()` invalidates a
  session once `expires_at` has passed, rather than only storing the
  field for display. Before this fix, the gateway session silently
  outlived the world credential it carries — past the world token's own
  TTL, `resolve_actor` server-side rejects the (now-expired) world token
  and falls through to the request's caller-declared identity, while
  this store kept handing back a session whose cached `state` might
  still read `"verified"`. See `get()`'s own docstring for the mechanism.

v0 scope, deliberately narrow (private-network-only, ephemeral, matches the
LAN+tailnet design posture for this milestone):

- Process-local, in-memory only — no persistence, no cross-process sharing.
  A gateway restart drops every session; that's acceptable for v0.
- Sessions live until `expires_at` (enforced by `get()`), an explicit
  `destroy()` (i.e. a logout), or the process restarts. No refresh
  mechanism — forcing re-login at expiry is the deliberate v0 answer
  (Mira's review: "I do not require a token refresh mechanism in this
  PR ... forcing re-login at world-token expiry is sufficient and safer").
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any


def seconds_until(expires_at: str) -> int:
    """Seconds from now until `expires_at` (same ISO-8601-with-`Z` shape
    `SessionStore` stores, matching `db.now_iso()`'s own format elsewhere
    in this codebase), floored at 0 — never negative, never raises.

    Used to cap the BROWSER's own cookie `max_age` no later than the
    world credential's expiry (Mira's #605 review: "cookie max-age no
    later than the world token expiry, as defence in depth"). This is
    defense in depth only — `SessionStore.get()`'s own enforcement is
    authoritative regardless of what the cookie's lifetime claims; a
    cookie that outlives its session token buys an attacker nothing,
    since `get()` will refuse the expired token anyway. A missing or
    unparseable `expires_at` returns 0 (treat as already expired) rather
    than some default TTL — silently trusting an unreadable expiry is
    exactly the failure mode this fix exists to close.
    """
    if not expires_at:
        return 0
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(remaining))


class SessionStore:
    """In-memory session store: opaque session token -> session record.

    Each record is `{"avatar": str, "csrf_token": str, "created_at": str,
    "world_token": str, "state": str, "expires_at": str}`. `csrf_token` is
    generated independently of the session token itself (a separate
    `secrets.token_urlsafe` call, never derived from the session token) —
    the two tokens protect against different threats (session token:
    bearer-credential theft; csrf_token: cross-site request forgery) and
    must not be linkable to each other. `world_token` is a THIRD, entirely
    separate credential (see module docstring) — never returned to the
    browser, only ever forwarded server-side to the world.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    def create(
        self, avatar: str, world_token: str, state: str, expires_at: str,
    ) -> dict[str, Any]:
        """Create a new gateway session for `avatar`, carrying forward the
        WORLD's own credential (`world_token`) and auth state (`state`).

        No identity validation happens here — by the time this is called,
        the caller (`/session/login`) has already received a 200 from
        `POST /umwelt/login`, i.e. the world itself authenticated `avatar`.
        This store's only job is to remember what the world just said.
        """
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session = {
            "avatar": avatar,
            "csrf_token": csrf_token,
            "created_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"),
            "world_token": world_token,
            "state": state,
            "expires_at": expires_at,
        }
        self._sessions[token] = session
        return {"token": token, **session}

    def get(self, token: str) -> dict[str, Any] | None:
        """Look up a session by its token. `None` if absent/invalid/EXPIRED
        — never raises on a bogus/unknown token.

        This is the single chokepoint EVERY session-gated gateway route
        already calls (`token = ...; session = session_store.get(token) if
        token else None; if session is None: <401 / spectator>`), so
        enforcing `expires_at` here — rather than adding a check to each
        of those call sites individually — means every existing
        fail-closed branch now correctly treats an expired session
        exactly like an absent one, with zero per-route changes (Mira's
        #605 security review). An expired session is also evicted from
        the store here, not merely masked at read time, so it does not
        silently accumulate in memory."""
        session = self._sessions.get(token)
        if session is None:
            return None
        if seconds_until(session.get("expires_at", "")) <= 0:
            del self._sessions[token]
            return None
        return session

    def destroy(self, token: str) -> None:
        """Remove a session. Idempotent — removing an absent/unknown token
        is not an error."""
        self._sessions.pop(token, None)
