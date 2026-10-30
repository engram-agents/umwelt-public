"""Umwelt Web Render — Render Gateway (M0 + M1 + M2 + M3).

A NEW, separate Flask service. It talks to the existing world server
(umwelt.server, unchanged, never modified by this package) purely over its
existing HTTP API, and adds a presentation layer (grid coordinates,
placeholder colors, deterministic cell assignment, and — as of M2 —
procedurally-rendered sprites) the world engine itself has no concept of.
Read-only through M2: the API surface is `GET /scene` (a render of a
place), `GET /crossings` (a place's crossing backlog/poll feed), `GET
/inspect` (a single thing's look_at detail), `GET /sprite/<key>` (a cached
sprite PNG), plus `GET /health` and the static frontend shell.

M3 adds the first WRITE path: a human picks an identity (one of the
world's pre-registered human avatars, e.g. `alice`) via a session, and can
then move their avatar between places through one chokepoint route,
`POST /act`. The one hard invariant governing this: the gateway is a view
+ intent-sender, never a second source of truth — every mutation goes
through an EXISTING world-server verb (`move_to`, via `_world_request`,
the same helper every other route already uses) — never reimplemented
client-side or gateway-side. See the `/act` route's own docstring for the
full contract.

Routes:
  GET /health      gateway's own liveness + a live (short-timeout) upstream
                   check against `<world_url>/health` — "the gateway is up"
                   and "the world is reachable" are different facts, reported
                   separately (`world_reachable`), never conflated. When the
                   world answered, `world_status` carries its own /health
                   `status` ("ok"/"degraded"; null if unreachable or absent)
                   and `world_db_error` its failing db_probe error (else
                   null); the gateway's own `status` stays "ok" (#1029).
  GET /scene       the core M0 endpoint — see `_build_scene`'s docstring for
                   the exact response contract. M2 additionally enriches
                   each returned thing with a real `sprite_ref` (when its
                   metadata carries a well-formed `render_spec`) and
                   validated `color`/`marker` overrides — see
                   `_enrich_things_with_sprites`. Place-lane-tiles spec:
                   also enriches the place itself with `lane_tiles` (sky/
                   land/road background sprite refs), when set — see
                   `_enrich_place_with_tiles`.
  GET /crossings   M1 — a place's crossings since a caller-supplied cursor,
                   proxying the world's read-only `crossings_here` with
                   `advance` hardcoded false. See the `crossings` route's
                   own docstring for the exact contract.
  GET /inspect     M1 — a single thing's `look_at` detail. Also the avatar-
                   status route (umwelt #239) when `thing` is an avatar's
                   THING id (`/scene`'s `avatars[i].avatar_id`) — see the
                   `inspect` route's own docstring for the exact contract.
  GET /sprite/<key>  M2 — serves a cached sprite PNG by its content-hash
                   key (see webrender/sprites.py). Pure serving — never
                   regenerates on a bare fetch; sprites are only ever
                   produced via the `/scene` pipeline.
  POST /session/login   M3, umwelt#495 — proxies to the world's own
                   POST /umwelt/login (real username+password auth); get
                   back a session cookie + csrf token. See its own
                   docstring.
  GET /session     M3 — the current session's identity, or "spectator".
  POST /session/logout  M3 — end the current session.
  GET /inventory   umwelt#267 RPG action set — YOUR avatar's carried things,
                   proxying the world's read-only `POST /umwelt/inventory`.
                   Session-gated (private, unlike `/inspect`'s public
                   posture) — see the `inventory` route's own docstring.
  GET /map         umwelt#267 RPG action set — the place-tree map, proxying
                   the world's read-only `POST /umwelt/map`. Session-gated,
                   same posture as `/inventory`.
  GET /inbox       umwelt#539/#529 — crossings addressed to YOUR avatar
                   across every place, proxying the world's read-only `POST
                   /umwelt/crossings_for_agent` then splitting via
                   `partition_crossings_for_inbox`. Optional `since=<seq>`
                   + response `to_position` give umwelt#529's bell a
                   poll-with-cursor contract on the same route. Session-gated,
                   same posture as `/inventory`/`/map`.
  GET /library     M4 art-generation pipeline, slice 3 — the browse/
                   approval-queue read, proxying `manifest.list_entries`
                   (webrender/manifest.py). NOT session-gated — see the
                   `library_list` route's own docstring for the identity
                   posture.
  POST /library/<key>/approve  M4 slice 3 — peer-approval write, proxying
                   `manifest.approve`. Session+CSRF gated (forum #289) —
                   the approver identity is ALWAYS the caller's own
                   session avatar, never client-declared. See the
                   `library_approve` route's own docstring.
  POST /act        M3 — the write chokepoint. Wires `move`, `talk`, `use`,
                   `say`, `take`, and `drop` (see its own docstring).
                   `talk`/`use`/`take`/`drop` are thin PROXIES onto the
                   world's already-gated `send_crossing`/`use`/`take`/`drop`
                   verbs — this route adds a PATH, never AUTHORITY, and
                   re-implements no law (Borges's security ruling, forum
                   #267).
  POST /voice      voice-playback v0 (#309 listen-button) — plays back a
                   crossing's body as synthesized speech in the sender's
                   cloned voice. Session+CSRF gated exactly like `/act`;
                   body is `{crossing_id}` ONLY — no sender/voice/text is
                   ever read from the client, both are resolved
                   server-side via `POST /umwelt/crossing_lookup`. Proxies
                   the standalone voice-playback service
                   (internal tooling, not in this release) at `voice_service_url`
                   — `None` (unconfigured) -> a clean 503, same
                   enhancement-never-dependency posture as
                   `/library/generate`'s `art_service_url`. See the
                   `voice` route's own docstring for the exact contract.
  GET /            the static frontend shell (index.html + app.js, no build
                   step, vanilla JS) — served from static/.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from umwelt import client_auth
from umwelt import engine_api
from umwelt._ids import canonical_agent
from umwelt.mentions import parse_mentions
from umwelt.server import partition_crossings_for_inbox
from umwelt.tls_context import check_tls_args, resolve_tls_context, serve_dual_listener

from . import library, manifest
from .manifest import ApprovalRefused, ManifestEntryNotFound, UnmigratedLegacyEntry
from .model_renderer import GenerationUnavailable, ImageModelRenderer
from .session import SessionStore, seconds_until
from .sprites import (
    SpriteStore, canonical_json, derive_seed, get_or_render_sprite, spec_key)

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# M2 §3 perf/safety note: cap the number of things a single /scene call will
# attempt look_at + sprite-generation for, so a place with a very large
# thing count can't make /scene hang doing dozens of sequential upstream
# calls. Simple, undocumented-elsewhere bound — not sophisticated, just not
# unbounded (spec's own words).
_MAX_SPRITE_LOOKUPS = 50

# Per-instance `metadata.color`/`metadata.marker` validation (Borges's
# security addendum on M2 — Part B.2's bound applied to agent-writable
# free-form metadata composited into a human-viewed image): only a strict
# 6-hex-digit color and a 1-2 char alphanumeric marker are ever passed
# through; anything else is treated as absent, never truncated-and-kept.
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_MARKER_RE = re.compile(r"^[A-Za-z0-9]{1,2}$")

# Minimum grid — "smallest grid that fits things+avatars, min [8,6]" (spec).
_MIN_GRID_A = 8
_MIN_GRID_B = 6

# umwelt #278: the 3 lane values the DB CHECK constraint enforces at rest.
# This gateway and the world server it talks to over HTTP are separately
# deployable processes, not the same address space — a schema-drift `lane`
# value (e.g. version skew between the two) is a real possibility here even
# though the DB CHECK already makes bad data at rest impossible. `_lane_key`
# below is this file's own defensive boundary against that, independent of
# the DB-side guarantee.
_VALID_LANES = ("sky", "land", "road")

# Placeholder color per thing `kind` — M0 has no sprites yet, so every thing
# renders as a flat-colored rectangle keyed off its engine-declared kind.
# `container`/`blueprint`/`kinddef` get a color too even though M0 gives
# them no special action menu (that's M2's typed-menu work) — every kind
# still needs *some* rectangle color to draw.
#
# NOTE (discrepancy vs. the design doc): the design doc's example enumerates
# `kind ∈ exec|readable|blueprint|container|kinddef`, but a live call
# against server.py shows the ACTUAL stored/returned value is
# `"executable"`, not `"exec"` (see `_VALID_KIND` in server.py and a live
# `move_to` response) — trusting the observed behavior over the doc's
# enumeration per the design's own instruction to do so. Flagged in the
# handoff.
_KIND_COLORS: dict[str, str] = {
    "blueprint": "#8b5a2b",
    "readable": "#4a90d9",
    "executable": "#e07b39",
    "container": "#888888",
    "kinddef": "#9b59b6",
}
_DEFAULT_COLOR = "#999999"  # unrecognized/future kind — never crash on it

# M3: the httpOnly cookie name carrying the opaque session token. Never the
# CSRF token — that one is handed back in the login response body and must
# be read + resent by the client as the `X-Csrf-Token` header, not a cookie
# (cookies ride automatically; a CSRF defense that lived in a cookie too
# would defend against nothing).
_SESSION_COOKIE_NAME = "umwelt_session"
_CSRF_HEADER_NAME = "X-Csrf-Token"

# umwelt#621 slice 1c: the FIXED, non-caller-selectable label /crossings
# forwards as `crossings_here`'s required `agent` field when no session
# cookie resolves (Mira's ruling: "keep it as an explicit anonymous branch
# whose identity is not caller-selectable"). crossings_here 400s on a blank
# agent BEFORE resolve_actor even runs, so an empty string can't stand in
# for "no identity" here the way place_view's optional `agent` does --
# a real, fixed placeholder is required. Used ONLY for _viewer_relation's
# labeling (never access control, never a DB lookup) -- see /crossings'
# own docstring.
_ANONYMOUS_GATEWAY_VIEWER = "gateway-anonymous-viewer"


class WorldUnreachable(Exception):
    """Connection-level failure talking to the world server (timeout,
    refused connection, DNS) — distinct from an HTTP error *response* the
    world server itself returned (4xx/5xx), which is WorldHTTPError."""


class WorldHTTPError(Exception):
    """The world server responded with a 4xx/5xx status. Carries the parsed
    JSON body + status code so the route can propagate the world's own
    error message verbatim (per the design's error contract)."""

    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(f"world server returned {status}: {body}")
        self.status = status
        self.body = body


def _world_request(world_url: str, path: str, payload: dict | None,
                    timeout: float, token: str | None = None) -> dict[str, Any]:
    """HTTP round-trip to the world server via stdlib urllib — same
    convention mcp_server.py's `_request` already uses for talking to the
    world server (no `requests` dependency; `requests` isn't in
    requirements.txt and the codebase's established pattern for this kind
    of call is urllib).

    `token` (umwelt#495, optional, default None): when given, sent as
    `Authorization: Bearer <token>` — the SAME header convention
    `resolve_actor` reads server-side (server.py). This is the single
    chokepoint every engine call passes through, so adding the header here
    is what lets a session's own world-issued credential (see
    session.py's `world_token`) actually reach routes wired to
    `resolve_actor` (umwelt#495 slice 1a's 7 converted read routes) —
    without this, a session could hold a perfectly valid world token and
    every call would still look like an anonymous declared-identity
    request. Omitted (None) preserves the exact prior behavior for every
    call site not yet updated to pass one — this parameter is additive,
    never a required migration.

    Raises WorldUnreachable on a connection-level failure. Raises
    WorldHTTPError on a 4xx/5xx response so the caller decides how to
    propagate it.
    """
    url = f"{world_url.rstrip('/')}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(engine_api.client_headers())  # umwelt#1105
    req = urllib.request.Request(
        url, data=data, method="POST" if payload is not None else "GET",
        headers=headers,
    )
    try:
        with client_auth.urlopen_with_cert_fallback(req, timeout) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:  # 4xx/5xx with a body
        body_raw = e.read().decode()
        body = json.loads(body_raw) if body_raw else {}
        raise WorldHTTPError(e.code, body) from e
    except urllib.error.URLError as e:
        raise WorldUnreachable(str(e)) from e
    except (OSError, ValueError) as e:
        raise WorldUnreachable(str(e)) from e


def _place_view_as(world_url: str, place_id: str, viewer_agent: str | None,
                    world_token: str | None, *,
                    timeout: float = 10.0) -> dict[str, Any]:
    """umwelt#656: THE one way this gateway reads
    `GET /umwelt/place_view` — the world's privacy predicate for a place's
    full interior. Every caller goes through here, so the viewer is a
    parameter a new caller must decide about rather than a line it can
    silently omit.

    That framing is the whole point of the helper. The bug this closes was
    not a wrong line; it was a RIGHT line that landed at one of four
    identical call sites. `/scene` was fixed under umwelt#554 after a human
    hit the 404 in a browser, `/crossings` under umwelt#621 slice 1c — and
    both fixes went to the route where the failure was observed and to none
    of its siblings. A free function with an optional viewer keyword would
    have reproduced exactly that: the fifth caller omits it and nothing
    complains. Both identity arguments are therefore REQUIRED positionals —
    an anonymous read must say `None, None` out loud.

    Identity is forwarded BOTH ways because the engine reads both and they
    are not redundant during the compatibility window (server.py's
    `place_view` -> `resolve_actor`):

    - `world_token` goes as a real `Authorization: Bearer` header. This is
      the authenticated path, and it WINS over any declared identity —
      the anti-forgery property #621 is converting the gateway toward.
    - `viewer_agent` goes as the declared `&agent=` query param. This is
      the backfill-window compat path, forgeable at the world layer like
      every other v0 declared identity, and the one `/scene` has used
      since #554. It resolves an identity ONLY for a session whose
      `world_token` is ABSENT — never for one whose token is present
      but dead. Corrected on Mira's security read of #712: the engine's
      `resolve_actor` is FAIL-CLOSED on a presented token (umwelt#618
      state 3), so an expired, unknown, retired or credential-reset
      token yields `source="invalid_token"` and a 401 — it never
      silently downgrades to the declared path. A session holding a
      dead token must re-authenticate; it does not fall back. The
      original wording here described the PRE-#618 fail-open model as
      if it were current, which is the more dangerous half of the
      error since a future reader would have inherited it.

    Passing both is deliberate, not belt-and-braces indecision: when the
    window closes and the declared path stops resolving, the token path is
    already carrying these call sites, and no call site has to change.

    NO widening of what any viewer may see. This helper only tells the
    world WHO is asking; the world's own `_place_interior_visible_to`
    decides. A viewer who is not the owner gets the same 404 they always
    did — private stays indistinguishable from nonexistent (umwelt#208).
    An anonymous caller (`None, None`) is byte-for-byte today's behavior.

    SCOPE: this gateway, not the repo. `tools/backfill_scan.py`'s
    `fetch_place_view` is a FIFTH reader of the same route and deliberately
    stays anonymous -- it is an offline CLI walk with no session and no
    viewer to forward, and its own docstring already records that a private
    place invisible to it is handled identically to a 404. It is correct as
    it is and is NOT a missed call site; named here so the next person
    running the cousin check does not "finish the sweep" by wiring an
    identity into a tool that has none. (Kepler's colleague pass, #712.)

    Raises `WorldHTTPError` / `WorldUnreachable` for the caller to handle;
    each of the four call sites has its own correct response to a 404 (a
    propagated error at the routes, a `sprite_ref: None` degradation at the
    place-marker lookup), so this helper deliberately does not choose one.
    """
    path = f"/umwelt/place_view?place={urllib.parse.quote(place_id)}"
    if viewer_agent:
        path += f"&agent={urllib.parse.quote(viewer_agent)}"
    return _world_request(world_url, path, None, timeout=timeout,
                          token=world_token)


def _look_at_as(world_url: str, thing_id: str, viewer_agent: str | None,
                world_token: str | None, *,
                timeout: float = 10.0) -> dict[str, Any]:
    """umwelt#982: THE one way this gateway calls `POST /umwelt/look_at` --
    the thing read gate's counterpart to `_place_view_as`, built on the same
    lesson. The engine now refuses a by-id read of a thing held in a private
    place to anyone who may not enter it, so a gateway call that sends no
    identity is refused EVEN FOR THE OWNER: their private-post sprites vanish
    and `/inspect` says no. Both identity arguments are REQUIRED positionals,
    so an anonymous read has to say `None, None` out loud instead of
    silently omitting a line.

    Identity is forwarded BOTH ways, for the reason `_place_view_as` gives:
    `world_token` as `Authorization: Bearer` (the authenticated path; wins
    over any declared identity), and `viewer_agent` as the body's declared
    `agent` (the compat path for a session with no token during the #512
    backfill). A session holding a dead token does not fall back to the
    declared path (`resolve_actor` is fail-closed on a presented token).

    This only tells the world WHO is asking; the world's own gate decides.
    Raises `WorldHTTPError` / `WorldUnreachable` for the caller to handle.
    """
    payload: dict[str, Any] = {"thing": thing_id}
    if viewer_agent:
        payload["agent"] = viewer_agent
    return _world_request(world_url, "/umwelt/look_at", payload,
                          timeout=timeout, token=world_token)


class VoiceServiceUnreachable(Exception):
    """Connection-level failure talking to the standalone voice-playback
    service (internal tooling, not in this release) — mirrors `WorldUnreachable`
    above, one level down the call chain."""


class VoiceServiceHTTPError(Exception):
    """The voice service responded with a 4xx/5xx status. Carries the
    parsed JSON body + status code, mirroring `WorldHTTPError` above —
    the `/voice` route inspects `.status`/`.body` to distinguish the
    expected `voice_not_enrolled` 404 from every other failure."""

    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(f"voice service returned {status}: {body}")
        self.status = status
        self.body = body


def _voice_synthesize_request(
    voice_service_url: str, viewer_agent: str, sender_id: str, text: str,
    cache_key: str, timeout: float = 30.0,
) -> bytes:
    """POST to the voice service's `/synthesize` — same stdlib-urllib
    convention as `_world_request` above, adapted for a binary (WAV, not
    JSON) success response. `viewer_agent` is sent as `X-Umwelt-Agent`
    (the AUTHENTICATED VIEWER/listener, per the voice service's own
    cross-spec pin (internal tooling, not in this release) — never the crossing's sender, so per-agent quota
    there is per-listener, not per-speaker).

    Returns the raw WAV bytes on success (HTTP 200). Raises
    `VoiceServiceHTTPError` on any 4xx/5xx JSON error response, and
    `VoiceServiceUnreachable` on a connection-level failure (timeout,
    refused connection, DNS) — the caller (the `/voice` route) maps both
    onto a clean, never-crash HTTP response."""
    url = f"{voice_service_url.rstrip('/')}/synthesize"
    payload = json.dumps({
        "sender_id": sender_id, "text": text, "cache_key": cache_key,
    }).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Umwelt-Agent": viewer_agent},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:  # 4xx/5xx with a JSON body
        body_raw = e.read().decode()
        body = json.loads(body_raw) if body_raw else {}
        raise VoiceServiceHTTPError(e.code, body) from e
    except urllib.error.URLError as e:
        raise VoiceServiceUnreachable(str(e)) from e
    except (OSError, ValueError) as e:
        raise VoiceServiceUnreachable(str(e)) from e


def _voice_cache_check_request(
    voice_service_url: str, cache_key: str, timeout: float = 10.0,
) -> bool:
    """POST to the voice service's `/cache_check` — same stdlib-urllib
    convention as `_voice_synthesize_request` above, adapted for a small
    JSON response instead of raw WAV bytes. No `X-Umwelt-Agent` header:
    this is a read-only membership check (no quota, no registry, no
    synthesis), same unauthenticated posture as `/health`.

    Returns `True`/`False`. Raises `VoiceServiceHTTPError` /
    `VoiceServiceUnreachable` exactly like `_voice_synthesize_request` —
    the `/voice/manifest` route treats either as "assume not cached"
    (see its own docstring) rather than failing the whole manifest over
    an advisory field."""
    url = f"{voice_service_url.rstrip('/')}/cache_check"
    payload = json.dumps({"cache_key": cache_key}).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
            # Reviewer-fairy catch (PR #374 round 1): a non-dict response
            # (malformed/unexpected JSON) must degrade the same way an
            # HTTP-level failure does -- `.get` on a non-dict would raise
            # an uncaught AttributeError past every except clause below,
            # turning "advisory field, never fails the manifest" into an
            # unhandled 500. Treat it as unreachable, same as a connection
            # failure, rather than trusting the shape of a 200 response.
            if not isinstance(body, dict):
                raise VoiceServiceUnreachable(
                    f"/cache_check returned non-dict JSON: {body!r}")
            return bool(body.get("cached"))
    except urllib.error.HTTPError as e:
        body_raw = e.read().decode()
        body = json.loads(body_raw) if body_raw else {}
        raise VoiceServiceHTTPError(e.code, body) from e
    except urllib.error.URLError as e:
        raise VoiceServiceUnreachable(str(e)) from e
    except (OSError, ValueError) as e:
        raise VoiceServiceUnreachable(str(e)) from e


def _voice_route_gate(
    session_store: SessionStore, app: Flask,
) -> tuple[dict, None] | tuple[None, tuple[Response, int]]:
    """Shared auth + CSRF + `voice_service_url`-configured gate for every
    `/voice`-family route (`/voice`, `/voice/manifest`, `/voice/section`).
    Extracted after reviewer-fairy PR #374 round 1 flagged this exact
    three-check block duplicated verbatim across all three routes (one
    pre-existing, two added by that PR) as a drift risk on
    security-sensitive logic — a future edit to the CSRF check in one
    copy silently missing the other two is exactly the kind of gap this
    file's own `/act` gate order comment (see `/voice`'s docstring) is
    meant to keep from happening.

    Returns `(session, None)` on success — caller proceeds. Returns
    `(None, (response, status))` on the first failing gate — caller
    returns that immediately, e.g. `return error` after
    `session, error = _voice_route_gate(...); if error: return error`."""
    token = request.cookies.get(_SESSION_COOKIE_NAME)
    session = session_store.get(token) if token else None
    if session is None:
        return None, (jsonify(
            {"error": "no active session — log in first"}), 401)

    csrf_header = request.headers.get(_CSRF_HEADER_NAME)
    try:
        csrf_ok = bool(csrf_header) and hmac.compare_digest(
            csrf_header, session["csrf_token"])
    except (TypeError, ValueError):
        csrf_ok = False
    if not csrf_ok:
        return None, (jsonify({"error": "CSRF token missing or invalid"}), 403)

    if not app.config["VOICE_SERVICE_URL"]:
        return None, (jsonify({
            "error": "voice service not configured on this gateway",
        }), 503)

    return session, None


def _session_and_csrf_gate(
    session_store: SessionStore,
) -> tuple[dict, None] | tuple[None, tuple[Response, int]]:
    """Shared auth + CSRF gate — the generic two-check core `_voice_route_gate`
    above already does (session-cookie lookup, then CSRF-header match),
    factored out as its own helper for routes that need ONLY that pair with
    no further route-specific check. `/act` had its own inline copy of
    exactly these two checks; `library_approve` (umwelt forum #289) is now
    the second/third route family needing this shape, so this is pulled out
    the same way `_voice_route_gate` itself was pulled out of the three
    voice routes (see that function's own docstring for the precedent).

    No `app` param, unlike `_voice_route_gate` — that function needs it for
    the voice-specific `app.config["VOICE_SERVICE_URL"]` check; this helper
    is deliberately just the generic session+CSRF pair, so it has nothing
    to read off `app`.

    Deliberately NOT wired into `/act` or `_voice_route_gate` — both are
    live, tested, security-critical paths; this is intentionally scoped to
    NEW callers only (`library_approve`), not a refactor of existing ones.

    Returns `(session, None)` on success — caller proceeds. Returns
    `(None, (response, status))` on the first failing gate — caller
    returns that immediately, e.g. `session, error =
    _session_and_csrf_gate(...); if error: return error`. Same return-shape
    convention `_voice_route_gate` already uses.
    """
    token = request.cookies.get(_SESSION_COOKIE_NAME)
    session = session_store.get(token) if token else None
    if session is None:
        return None, (jsonify(
            {"error": "no active session — log in first"}), 401)

    csrf_header = request.headers.get(_CSRF_HEADER_NAME)
    try:
        csrf_ok = bool(csrf_header) and hmac.compare_digest(
            csrf_header, session["csrf_token"])
    except (TypeError, ValueError):
        csrf_ok = False
    if not csrf_ok:
        return None, (jsonify({"error": "CSRF token missing or invalid"}), 403)

    return session, None


def _voice_section_cache_key(sender: str, body: str, index: int) -> str:
    """Deterministic per-SECTION cache key — same sha256-of-canonical-json
    idiom as the whole-message `cache_key` the existing `/voice` route
    computes, with the section `index` folded in so each section of the
    same crossing gets its own stable, content-addressed key (same
    crossing -> same sections -> same keys, every time -- forum #282's
    "cache-reusable" requirement). Deliberately a fresh hash over
    `{sender, body, section}`, not the whole-message `cache_key` with a
    suffix appended -- keeps this key's derivation independent of
    whatever the whole-message route happens to hash, so a future change
    to one can't silently perturb the other.

    **Implicit coupling to sectioning parameters (reviewer-fairy note,
    PR #374 round 1), mirroring the voice service's own deferred (internal tooling, not in this release)
    cache-invalidation note on its whole-message cache:** the key is
    `sha256({sender, body, index})`, hashed over the FULL body + a
    section INDEX, not the section's own text — correct today because
    `_section_crossing_text`'s parameters (`first_len`/`subsequent_len`)
    are fixed constants, so a given `(sender, body, index)` always
    resolves to the same section text. If those parameters ever become
    tunable (per-place, per-experiment, etc.), this key would go stale
    silently: same `(sender, body, index)` could then resolve to
    DIFFERENT section text across a parameter change, serving an old
    cached WAV for what is now a different chunk of the message. Not a
    v1 concern -- flagged so whoever makes sectioning tunable finds this
    note instead of rediscovering the gap."""
    return hashlib.sha256(
        canonical_json({"sender": sender, "body": body, "section": index})
        .encode("utf-8")
    ).hexdigest()


def _pack_words(words: list[str], limit: int) -> list[str]:
    """Greedily pack whitespace-split `words` into lines of at most
    `limit` chars each, joined by single spaces — never breaks a word.
    A single word longer than `limit` becomes its own oversized line
    (there is no narrower boundary to split it on without cutting a
    word in half, which `_section_crossing_text`'s callers never want).
    Used as `_section_crossing_text`'s fallback for a sentence that by
    itself exceeds the section-size target (see that function)."""
    lines: list[str] = []
    current = ""
    for w in words:
        candidate = f"{current} {w}".strip()
        if not current or len(candidate) <= limit:
            current = candidate
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _section_crossing_text(
    text: str, first_len: int = 90, subsequent_len: int = 250,
) -> list[str]:
    """Deterministically split a crossing's body into playback sections
    for forum #282's voice v1 (Kepler's ruling, forum #282 reply #5107):
    sentence boundaries always, first section sized for fast first-audio
    (~`first_len` chars), later sections amortizing per-call overhead
    (~`subsequent_len` chars). Same input -> same output, always (no
    randomness, no external state) -- this is what makes per-section
    cache keys (`_voice_section_cache_key`) stable across repeat listens
    of the same crossing.

    Sentence-boundary split is a simple `.`/`!`/`?`-followed-by-whitespace
    regex (v0 heuristic, same spirit as the voice service's (internal tooling, not in this release)
    pinned-simple language heuristic -- doesn't special-case abbreviations
    like "Dr." or decimal numbers; revisit if that surfaces in practice).

    **Length bound holds even with zero sentence punctuation** (umwelt#362
    -- sectioning IS supposed to be the length bound now, so no input may
    escape it): any single sentence-candidate longer than `subsequent_len`
    is first word-packed via `_pack_words` into `subsequent_len`-bounded
    pieces before section-packing runs, so a long run-on crossing with no
    periods still comes out as multiple bounded sections, not one
    unbounded one.

    **The first-section target is soft, not a hard cap**: the first
    sentence/piece is always accepted as-is even if it alone exceeds
    `first_len` (never split mid-sentence to force it under the target) --
    it is still bounded by `subsequent_len` via the word-packing fallback
    above, just not by the tighter first-audio target. Empty/whitespace-only
    `text` returns `[text]` (a single degenerate section) rather than `[]`,
    so callers always get at least one section to request, matching the
    single-call shape the whole-message `/voice` route already has today."""
    stripped = text.strip()
    if not stripped:
        return [stripped]

    sentences = _SENTENCE_SPLIT_RE.split(stripped)
    pieces: list[str] = []
    for sentence in sentences:
        if len(sentence) <= subsequent_len:
            pieces.append(sentence)
        else:
            pieces.extend(_pack_words(sentence.split(), subsequent_len))

    sections: list[str] = []
    current = ""
    target = first_len
    for piece in pieces:
        if not current:
            current = piece
            continue
        candidate = f"{current} {piece}"
        if len(candidate) <= target:
            current = candidate
        else:
            sections.append(current)
            current = piece
            target = subsequent_len
    if current:
        sections.append(current)
    return sections


def _grid_size(n_sky: int, n_land: int, n_road: int) -> tuple[int, int]:
    """Deterministic grid dimensions `[A, B]` for a scene: the width is
    fixed at the 8-column floor (`_MIN_GRID_A`); only the height grows, just
    enough that `A*B >= 1.5 * (sky+land+road)` AND that each of the three
    `lane` bands (umwelt #278: sky/land/road — see `_build_scene`) gets its
    own whole rows, stacked sky-then-land-then-road top to bottom, `road`
    anchored to the LAST row(s) of the grid — so no two bands can ever share
    a row, and no two items can ever land on the same cell. Height never
    drops below the `_MIN_GRID_B` floor. Presentation-only; deliberately
    simple (fixed-width/grow-height is one valid reading of the spec's "any
    simple formula is fine" — not the only one, flagged in the handoff).

    Signature note (ambiguous decision, flagged in the handoff): this went
    from the old 2-arg `(n_things, n_avatars)` to 3 positional ints
    (`n_sky, n_land, n_road`) rather than a `dict`/`Counter` keyed by lane —
    smallest diff at every call site, and still fully readable positionally
    since there are only ever exactly three lanes (a closed, never-growing
    enum per the design-of-record).
    """
    a, b = _MIN_GRID_A, _MIN_GRID_B
    total = n_sky + n_land + n_road
    needed = math.ceil(1.5 * total) if total else 0
    while a * b < needed:
        b += 1
    # Guarantee each band fits its own whole rows even after `road`'s
    # last-row reservation (see _build_scene) — grows further if the
    # 1.5x-total floor above wasn't already enough headroom for that.
    # umwelt #283 nit 2: provably-unreachable under the current
    # _MIN_GRID_A=8/_MIN_GRID_B=6 constants — the 1.5x-total floor loop
    # above already dominates for every real input (exhaustively checked,
    # see test_grid_size_reserves_whole_rows_per_populated_lane's docstring
    # and test_grid_size_reservation_loop_actually_prevents_lane_overlap,
    # which isolates this loop's own necessity by patching the constants
    # down). Kept intentionally as forward-defense, not dead code to remove.
    sky_rows = math.ceil(n_sky / a) if n_sky else 0
    land_rows = math.ceil(n_land / a) if n_land else 0
    road_rows = math.ceil(n_road / a) if n_road else 0
    while (sky_rows + land_rows + road_rows) > b:
        b += 1
    return a, b


def _assign_cells(item_ids: list[str], grid_a: int,
                   row_offset: int) -> dict[str, tuple[int, int]]:
    """Deterministic row-major packing: `item_ids` sorted ascending (stable
    id sort — same input always produces the same order), placed
    left-to-right/top-to-bottom starting at `row_offset`. Same input state
    ⇒ same cell assignment on every call (the design's stated determinism
    requirement).

    Unchanged by umwelt #278's lane rewrite — `_build_scene` now calls this
    once per `lane` band (sky/land/road), each with its own `row_offset`,
    instead of once for things (offset 0) and once for avatars (offset
    `grid_b - avatar_rows`). Row-major, sorted-id determinism is preserved
    WITHIN each band the same way it always was across the whole grid.
    """
    cells: dict[str, tuple[int, int]] = {}
    for idx, item_id in enumerate(sorted(item_ids)):
        row = row_offset + idx // grid_a
        col = idx % grid_a
        cells[item_id] = (col, row)
    return cells


def _thing_actions(kind: str) -> list[dict[str, str]]:
    """`actions[]` for a thing, derived purely from `kind` — no engine call
    needed. `look_at` is always appended (in addition to any kind-specific
    verb); `container` gets `look_in` then `look_at` (umwelt #516 — the web
    surface for the engine's closed-bag `POST /umwelt/look_in`); blueprint/
    kinddef get just `look_at`; `readable` also gets `edit` (umwelt #547 —
    the web door onto `update_thing`'s `description` patch). `kind ==
    "executable"` (see the `_KIND_COLORS` note above re: the doc's "exec"
    vs. the actually-observed "executable")."""
    actions: list[dict[str, str]] = []
    if kind == "executable":
        actions.append({"verb": "use", "label": "Use"})
    elif kind == "readable":
        actions.append({"verb": "read", "label": "Read"})
        actions.append({"verb": "edit", "label": "Edit"})
    elif kind == "container":
        actions.append({"verb": "look_in", "label": "Look in"})
    actions.append({"verb": "look_at", "label": "Look"})
    return actions


def _compose_thing_actions(kind: str,
                           typed: list[dict[str, Any]] | None = None
                           ) -> list[dict[str, Any]]:
    """umwelt #936: the ONE place a thing's full action list is assembled --
    the kind's built-ins (`_thing_actions`) first, then any typed actions.
    Both `/scene` and `/inspect` build their `actions` through this, so for
    the same thing and viewer the two lists cannot disagree. `typed` is
    None / empty for an untyped thing, an anonymous viewer, or a failed
    typed-action lookup (fail-soft: the built-ins are still returned)."""
    return _thing_actions(kind) + list(typed or [])


def _validate_hex_color(value: Any) -> str | None:
    """`metadata.color` is agent-writable free-form metadata composited
    into a human-viewed sprite tint — never trust it verbatim. Only a
    strict `#RRGGBB` string passes; anything else (wrong type, wrong
    length, non-hex chars) is treated as absent.

    Post-review fix: `.match()` on a `^...$`-anchored pattern is NOT the
    same as a full-string match — Python's `$` also matches immediately
    before a single trailing `\\n`, so `"#123456\\n"` passed the old
    `.match()` check and was returned VERBATIM (including the newline),
    violating this function's own "never trust it verbatim" contract.
    `.fullmatch()` requires the ENTIRE string to match, closing this."""
    if isinstance(value, str) and _HEX_COLOR_RE.fullmatch(value):
        return value
    return None


def _validate_marker(value: Any) -> str | None:
    """`metadata.marker` is rendered as text on top of a sprite — clamp to
    <=2 alphanumeric chars (after trimming). Anything longer, empty after
    trim, or containing non-alphanumeric characters is treated as absent
    (None) — NEVER truncated-and-passed-through.

    Uses `.fullmatch()` for the same reason `_validate_hex_color` does
    (see its docstring) — `.strip()` already removes a bare trailing
    newline here today, so this specific bypass isn't currently reachable
    for `marker`, but `.fullmatch()` is the structurally correct call
    regardless of what `.strip()` happens to catch."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if _MARKER_RE.fullmatch(trimmed):
        return trimmed
    return None


# M4 slice 4 (per-kind generics): a small, fixed, hardcoded mapping from a
# thing's engine-stamped `kind` (`executable`/`readable`/`container`/...,
# already server-stamped — never agent-forgeable) to a generic fallback
# render_spec. Closed set, same posture as `class`/`palette` already being
# closed elsewhere in this codebase — never agent-declarable.
#
# Category key is `kind` ALONE, not `(kind, size_class)` — Borges's explicit
# v0 ruling: "size_class likely over-partitions the generic set early ...
# key on kind, and add size_class as the refinement only when real data
# shows a specific kind actually needs size-differentiated generics."
#
# The complete kind taxonomy (src/umwelt/db.py's CHECK constraint) is
# `executable`, `readable`, `avatar`, `blueprint`, `container`, `kinddef`.
# `avatar` is deliberately NOT mapped here -- avatars don't go through
# `_apply_render_spec`'s enrichment path at all yet (a separate, not-yet-
# built serve-path enrichment), so a generic-fallback
# entry for it would be dead code, not a real fallback. The other five are
# all mapped as of 2026-07-24 (M4 slice 4 generic-fallback expansion)
# -- `readable` shipped first; `executable`/`blueprint`/
# `container`/`kinddef` followed once the art-gen service (#326) made real
# generation possible for the rest.
_GENERIC_RENDER_SPEC_BY_KIND: dict[str, dict[str, Any]] = {
    "readable": {
        "class": "generic_document",
        "descriptors": ["rolled parchment scroll", "document placeholder"],
        "palette": "warm-wood",
        "mode": "object",
    },
    "container": {
        "class": "generic_container",
        "descriptors": ["wooden storage chest", "container placeholder"],
        "palette": "warm-wood",
        "mode": "object",
    },
    "executable": {
        "class": "generic_tool",
        "descriptors": ["wooden-handled hammer", "tool placeholder"],
        "palette": "ember",
        "mode": "object",
    },
    "blueprint": {
        "class": "generic_blueprint",
        "descriptors": ["gridded technical plan", "blueprint placeholder"],
        "palette": "cool-stone",
        "mode": "object",
    },
    "kinddef": {
        "class": "generic_kinddef",
        "descriptors": ["engraved stone seal", "definition placeholder"],
        "palette": "slate",
        "mode": "object",
    },
}


def _generic_sprite_ref_for_kind(
    kind: str | None, sprite_store: SpriteStore, manifest_dict: dict,
) -> str | None:
    """M4 slice 4's NEW middle resolution tier, tried after an exact
    `spec_key` match fails and before falling all the way to procedural.

    Returns a `/sprite/<key>` ref only when BOTH (a) `kind` has a mapped
    generic in `_GENERIC_RENDER_SPEC_BY_KIND` AND (b) that generic's own
    `spec_key` is actually **live** in the manifest right now
    (`library.resolve` — never a change to `resolve()`'s own exact-match
    logic, just reused against the generic's render_spec instead of the
    thing's own). An unmapped `kind`, or a mapped generic whose spec_key
    isn't live yet (not populated, or still `pending`), returns `None` —
    the caller then falls through to procedural exactly as it did before
    this tier existed. Never raises on a missing/malformed mapping.
    """
    generic_spec = _GENERIC_RENDER_SPEC_BY_KIND.get(kind)
    if generic_spec is None:
        return None
    if library.resolve(generic_spec, manifest_dict) is None:
        return None
    return get_or_render_sprite(generic_spec, sprite_store)


def _apply_render_spec(thing: dict[str, Any], world_url: str,
                        sprite_store: SpriteStore,
                        viewer_agent: str | None,
                        world_token: str | None) -> None:
    """Mutates a single `/scene` thing dict in place: fetches its full
    record via the same read-only `look_at` `/inspect` already uses
    (through `_look_at_as`, forwarding the session's viewer AND world token
    -- umwelt#982: the engine's thing read gate refuses an identity-less
    read of a private-place thing, owner included), and:

    - if `metadata.render_spec` is a dict with a `class` key, runs it
      through the sprite pipeline (webrender/sprites.py) and sets
      `sprite_ref` to the resulting `/sprite/<key>` ref. As of M4 slice 4,
      resolution here is a THREE-tier fallback (see
      `_generic_sprite_ref_for_kind`'s docstring for tier 2's contract):
        1. exact `spec_key` match — `library.resolve()` against the
           manifest; when it finds a live entry for this exact
           render_spec, `get_or_render_sprite` below serves/generates
           under that same content-addressed key exactly as before this
           slice existed.
        2. NEW: generic-for-category fallback — no exact match, but the
           thing's `kind` maps to a live generic in the manifest.
        3. procedural fallback — neither 1 nor 2 produced a match;
           `get_or_render_sprite` renders the thing's OWN render_spec
           procedurally, exactly as it did before this slice existed.
    - if the render_spec handling above produced NO sprite — because the
      render_spec is missing/malformed/not-a-dict/no-class (the common
      bare-thing case: the world's things overwhelmingly have no render_spec
      at all), or it raised inside the tier-1/2/3 try — a FINAL kind-generic
      tier (umwelt#336) runs: `_generic_sprite_ref_for_kind` serves the
      thing's `kind` generic if one is live in the manifest. This is what
      makes the fallback actually reach the bare things it was named for;
      before it, the generic tier was reachable ONLY through a valid
      render_spec, so bare things always fell to the placeholder box. NOTE
      this is a behavior EXPANSION beyond bare things: a malformed render_spec
      whose `kind` maps to a live generic now gets that generic too, where it
      previously always fell to the box. Only when this tier also yields
      nothing (unmapped kind / no live generic) does `sprite_ref` stay `None`;
      a malformed render_spec additionally logs its rejection — NEVER fatal,
      `/scene` must still return for every other thing in the response;
    - separately (§A.5, independent of whether a sprite was produced)
      reads validated `metadata.color`/`metadata.marker` onto the thing's
      own `color`/`marker` fields. When a real sprite exists, `color` is
      the validated tint (or `None` — no tint, never the old placeholder).
      When no real sprite exists, `color` falls back to the existing
      `_KIND_COLORS` placeholder already set by `_build_scene` unless a
      validated override was supplied. `marker` is `None` when absent or
      invalid, in both cases.

    Any upstream failure (world unreachable, world 4xx/5xx, a renderer
    exception on a well-formed-looking but pathological spec, or a
    manifest-load failure for the new tiers above) is caught here and
    logged — never propagated as a 500 for the whole `/scene` response
    over one bad thing.
    """
    try:
        detail = _look_at_as(world_url, thing["id"], viewer_agent,
                             world_token, timeout=5.0)
    except (WorldHTTPError, WorldUnreachable) as e:
        print(f"[webrender] sprite lookup failed for thing "
              f"{thing['id']!r}: {e}")
        return

    metadata = (detail.get("thing") or {}).get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    render_spec = metadata.get("render_spec")
    sprite_ref = None
    if (isinstance(render_spec, dict)
            and isinstance(render_spec.get("class"), str)
            and render_spec.get("class")):
        try:
            manifest_dict = library.load_manifest(sprite_store.dir)
            if library.resolve(render_spec, manifest_dict) is not None:
                # tier 1: exact spec_key match — unchanged behavior below.
                sprite_ref = get_or_render_sprite(render_spec, sprite_store)
            else:
                generic_ref = _generic_sprite_ref_for_kind(
                    thing.get("kind"), sprite_store, manifest_dict)
                if generic_ref is not None:
                    # tier 2 (NEW, M4 slice 4): no exact curated match —
                    # serve the thing kind's live generic instead.
                    sprite_ref = generic_ref
                else:
                    # tier 3: procedural fallback — unchanged behavior.
                    sprite_ref = get_or_render_sprite(render_spec, sprite_store)
        except Exception as e:  # noqa: BLE001 — never let a bad spec 500 /scene
            print(f"[webrender] sprite render failed for thing "
                  f"{thing['id']!r}: {e}")
            sprite_ref = None
    elif render_spec is not None:
        print(f"[webrender] rejected malformed render_spec for thing "
              f"{thing['id']!r} — falling back to placeholder color")

    # umwelt #336: a thing whose render_spec did NOT produce a sprite —
    # no render_spec at all (the common bare-box case), a malformed one, or
    # a render error above — still gets its KIND's live generic if one
    # exists. Before this, the generic tier (M4 slice 4) was reachable ONLY
    # through the render_spec-with-class branch above, so the village's bare
    # things (render_spec=None) could never fall back to a generic and
    # rendered as `_KIND_COLORS` placeholder boxes — the "per-kind generic
    # fallback" never actually fell back for the very things it was named
    # for. This closes that gap. An unmapped kind, or a kind whose generic
    # isn't live in the manifest, still yields None -> placeholder box
    # (unchanged); errors here are caught, never 500-ing `/scene`.
    if sprite_ref is None:
        try:
            manifest_dict = library.load_manifest(sprite_store.dir)
            sprite_ref = _generic_sprite_ref_for_kind(
                thing.get("kind"), sprite_store, manifest_dict)
        except Exception as e:  # noqa: BLE001 — never let this 500 /scene
            print(f"[webrender] generic fallback (no render_spec) failed "
                  f"for thing {thing['id']!r}: {e}")
            sprite_ref = None

    color = _validate_hex_color(metadata.get("color"))
    marker = _validate_marker(metadata.get("marker"))

    if sprite_ref is not None:
        thing["sprite_ref"] = sprite_ref
        thing["color"] = color  # validated tint, or None = no tint
    else:
        thing["color"] = color if color is not None else thing["color"]
    thing["marker"] = marker


def _enrich_things_with_sprites(things: list[dict[str, Any]], world_url: str,
                                 sprite_store: SpriteStore,
                                 viewer_agent: str | None,
                                 world_token: str | None) -> None:
    """Runs `_apply_render_spec` for at most `_MAX_SPRITE_LOOKUPS` things
    (in `/scene` response order) — the perf/safety bound from M2 §3.
    `viewer_agent` / `world_token` are the SESSION's (umwelt#982), forwarded
    to every look_at; an anonymous scene passes `None, None`.
    Mutates `things` in place."""
    for thing in things[:_MAX_SPRITE_LOOKUPS]:
        _apply_render_spec(thing, world_url, sprite_store,
                           viewer_agent, world_token)


def _typed_action_entry(verb: str, action: dict[str, Any]) -> dict[str, Any]:
    """One world `thing_actions` row -> the client's typed-action shape; the
    ONE mapping both enrichment passes below use. `available` (umwelt #967)
    is the world's from-state rule; `reason` (umwelt #975) is the world's own
    words for why an unavailable action is unavailable, forwarded verbatim
    and only when it is a non-empty string (the client shows it as-is)."""
    entry: dict[str, Any] = {
        "verb": verb,
        "label": verb.replace("_", " ").title(),
        "requires": list(action.get("requires", [])),
        "permitted": bool(action.get("permitted", False)),
        "available": bool(action.get("available", True)),
    }
    reason = action.get("reason")
    if isinstance(reason, str) and reason:
        entry["reason"] = reason
    return entry


def _fetch_typed_actions(world_url: str, thing_id: str, viewer: str,
                         world_token: str | None
                         ) -> list[dict[str, Any]] | None:
    """umwelt #936: the shared typed-action lookup behind `/scene` and
    `/inspect` (extracted from the two enrichers, behaviour unchanged).
    Returns the mapped typed-action entries, or None on any upstream failure
    (fail-soft: logged, never raised -- the caller keeps the built-ins).
    `ValueError` is listed with the two typed world errors because
    `_world_request`'s HTTPError branch re-raises a bare ValueError on a
    non-JSON 4xx/5xx body."""
    try:
        path = (f"/umwelt/thing_actions?thing="
                f"{urllib.parse.quote(str(thing_id))}&agent="
                f"{urllib.parse.quote(viewer)}")
        detail = _world_request(world_url, path, None, timeout=5.0,
                                token=world_token)
    except (WorldHTTPError, WorldUnreachable, ValueError) as e:
        print(f"[webrender] typed action lookup failed for thing "
              f"{thing_id!r}: {e}")
        return None
    entries = []
    for action in detail.get("actions", []):
        verb = action.get("verb")
        if not isinstance(verb, str) or not verb:
            continue
        entries.append(_typed_action_entry(verb, action))
    return entries


def _enrich_things_with_typed_actions(things: list[dict[str, Any]],
                                       world_url: str, viewer: str,
                                       world_token: str | None) -> None:
    """Merges kind-declared typed actions onto each KIND_ID-BEARING thing's
    `actions` list (umwelt #522 — the missing web door onto `act()`).
    Built through `_compose_thing_actions` (umwelt #936, the same helper
    `/inspect` uses): the kind's built-ins (`look_at`, plus `use`/`read`
    for executable/readable kinds) first, the typed actions appended — so
    a typed thing still gets its ordinary untyped actions alongside its
    kind-declared ones.

    Gated on `thing.get("kind_id")` being present — a TOP-LEVEL field
    (umwelt #522 coder-fairy addition to server.py's `_place_view`, found
    necessary mid-implementation: the `/scene` thing shape is a
    deliberately trimmed dict that never spreads a thing's full
    `metadata` wholesale — see `_place_view`'s own docstring — so there
    was no `metadata.kind_id` to read here at all until that key was
    added; a SCOPED single field, not the whole metadata blob, matches
    the same minimal-exposure posture `lane` already has there). Untyped
    things — the OVERWHELMING majority of things in the world (plain
    readable/executable/container) — pay ZERO extra engine round-trips;
    only kind_id-bearing things (proposals, project trays — rare by
    design) make the extra `GET /umwelt/thing_actions` call. This is the
    whole reason typed-action merging is a SEPARATE post-pass over
    `things` rather than folded into `_build_scene`'s own per-thing loop:
    `_build_scene` is a pure `wv`-reshape function with no engine-call
    capability today (see its own docstring's "Read-only by construction,
    not by discipline") and no `world_url`/viewer-identity inputs to make
    one with — this function is called separately by the `/scene` route
    handler, same "called once per route invocation, passing the same
    world_url already in scope" pattern `_enrich_things_with_sprites`/
    `_enrich_avatars_with_sprites`/`_enrich_place_markers_with_sprites`/
    `_enrich_place_with_tiles` already use for their own extra-call
    enrichment passes.

    Bounded to the same `_MAX_SPRITE_LOOKUPS` things (in `/scene` response
    order) as every sibling enrichment pass above, for the same perf/safety
    reason — belt-and-braces alongside the kind_id gate itself.

    `viewer` here is the SAME query-string `viewer` value `/scene` already
    treats as a display-only, non-authenticating client-supplied label
    (see the `/scene` route's own docstring) — it is now ALSO forwarded to
    this one new read-only engine call to compute `permitted` per the
    resolved flag-don't-filter design (umwelt #522 resolved-decisions §1).
    This does not create a new authority-grant: `/umwelt/thing_actions` is
    a pure read (it flags every action's permission, it grants none), and
    actually INVOKING an action still always goes through `POST /act`'s
    `agent: session["avatar"]` (never client-supplied, see that route's
    own gate 3) — so a spectator naming a `viewer` they aren't cannot act
    as that viewer, only see what that viewer's kind-chain would flag.

    Any upstream failure (world unreachable, non-2xx) is caught and
    logged, never propagated — a typed thing temporarily missing its extra
    actions is far better than the whole place failing to render because
    one thing's action-fetch hiccuped, same fail-soft posture every
    sibling enrichment function in this file already has
    (`_apply_render_spec`/`_apply_avatar_render_spec` et al.). On failure,
    the thing keeps exactly its untyped-only `_thing_actions` list — never
    a partial or malformed merge. Mutates `things` in place.
    """
    for thing in things[:_MAX_SPRITE_LOOKUPS]:
        kind_id = thing.get("kind_id")
        if not kind_id:
            continue
        typed = _fetch_typed_actions(world_url, str(thing["id"]), viewer,
                                     world_token)
        if typed is None:
            continue
        thing["actions"] = _compose_thing_actions(
            str(thing.get("kind", "")), typed)


def _enrich_inspect_with_typed_actions(result: dict[str, Any],
                                        world_url: str,
                                        viewer: str | None,
                                        world_token: str | None) -> None:
    """umwelt #655: /inspect's additive typed-action enrichment — the
    contained-thing counterpart to `_enrich_things_with_typed_actions`
    above (which is /scene's TOP-LEVEL-things pass). A proposal sitting in
    an office tray is reached via /inspect, not /scene, so without this it
    had no web door onto `act()` at all: the tray item was an inert row and
    the viewer had nothing to dispatch against.

    Gated on `result["thing"]["metadata"]["kind_id"]` — NOTE: under
    `metadata`, NOT a top-level field. This is the load-bearing distinction
    from `_enrich_things_with_typed_actions`, which reads a TOP-LEVEL
    `kind_id` because /scene's thing shape is a deliberately trimmed dict
    that surfaces kind_id as its own scoped field. /inspect proxies
    look_at, which spreads the thing's FULL metadata blob, so kind_id lives
    under `metadata` here. Same minimal-exposure posture, different shape.

    `viewer` is `session["avatar"]` (soft, best-effort — None when the
    caller has no session, mirroring /scene's own `session_agent`
    resolution). Unlike /scene (which forwards the untrusted `viewer`
    QUERY param), /inspect has no viewer query param at all, so the session
    is the ONLY source. An anonymous caller (no session) gets no TYPED
    actions: the world's `/umwelt/thing_actions` requires a non-empty
    `agent` (400) and a real avatar (404 otherwise), so an empty viewer is
    skipped rather than called.

    umwelt #936: `result["actions"]` is now ALWAYS set -- the kind's
    built-ins (`_thing_actions`) for every thing, typed or untyped,
    session or anonymous, plus the typed verbs when the lookup ran and
    succeeded. A thing reached by id (a look_in row, an inventory row, a
    pasted id) therefore never gets an empty menu, and the list is
    assembled by `_compose_thing_actions`, the same helper `/scene` uses.

    Maps typed actions to the SAME `{verb, label, requires, permitted}`
    shape `openInteractMenuForThing` (and the #655 viewer) consumes, via
    the shared `_fetch_typed_actions`, so the client has one action-bar
    code path for both /scene things and /inspect things.

    Fail-soft: a typed-lookup error (unreachable, non-2xx, non-JSON) is
    caught and logged inside `_fetch_typed_actions`; the built-ins are
    still returned and the rest of the response is untouched. Mutates
    `result` in place.
    """
    thing = result.get("thing") or {}
    kind_id = (thing.get("metadata") or {}).get("kind_id")
    typed = None
    if kind_id and viewer:
        typed = _fetch_typed_actions(world_url, str(thing.get("id", "")),
                                     viewer, world_token)
    # umwelt #936: built-ins for EVERY thing (typed or not, session or not);
    # typed verbs only when the typed lookup ran and succeeded.
    result["actions"] = _compose_thing_actions(
        str(thing.get("kind", "")), typed)


def _apply_avatar_render_spec(avatar: dict[str, Any], world_url: str,
                               sprite_store: SpriteStore,
                               viewer_agent: str | None,
                               world_token: str | None) -> None:
    """Avatar-shaped counterpart to `_apply_render_spec` (2026-07-24 world-
    finishing sprint — avatars never went through the
    thing enrichment path at all; `_build_scene` has hardcoded every
    avatar's `sprite_ref` to `None` until now).

    Same three-tier resolution as things (exact curated match -> live
    kind-generic -> procedural), reusing `library.resolve`/
    `get_or_render_sprite` unchanged — no separate avatar sprite pipeline.
    The one real difference from `_apply_render_spec`: avatars carry no
    `color`/`marker` fields in `/scene`'s output contract (out_avatars sets
    `marker: None` unconditionally and has no `color` key at all), so this
    function only ever sets `sprite_ref` — never touches `metadata.color`/
    `metadata.marker`, which is out of scope for this pass (Borges's ask
    was specifically "thread render_spec through enrichment", not extend
    the tint/marker contract to avatars too).

    Looked up via `/umwelt/look_at {thing: avatar["avatar_id"]}` — the
    avatar's underlying THING id, same field `/inspect` already uses for
    exactly this reason (see `_build_scene`'s docstring on `avatar_id` vs
    `agent`). `kind` for the tier-2 generic-fallback lookup is always
    `"avatar"` (db.py's fixed kind enum) — no generic is mapped for it
    today (`_GENERIC_RENDER_SPEC_BY_KIND` deliberately omits `avatar`, see
    that dict's own module comment), so tier 2 safely no-ops and an
    avatar with a `render_spec` but no exact curated match falls to tier 3
    (procedural), same graceful-degradation shape as things.

    Any upstream failure (world unreachable, malformed spec, renderer
    exception) is caught and logged here, never propagated — an avatar
    stays a plain circle (`sprite_ref` left at its incoming value, i.e.
    `None`) rather than breaking the whole `/scene` response.
    """
    try:
        detail = _look_at_as(world_url, avatar["avatar_id"], viewer_agent,
                             world_token, timeout=5.0)
    except (WorldHTTPError, WorldUnreachable) as e:
        print(f"[webrender] avatar sprite lookup failed for avatar "
              f"{avatar['avatar_id']!r}: {e}")
        return

    metadata = (detail.get("thing") or {}).get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    render_spec = metadata.get("render_spec")
    if not (isinstance(render_spec, dict)
            and isinstance(render_spec.get("class"), str)
            and render_spec.get("class")):
        if render_spec is not None:
            print(f"[webrender] rejected malformed avatar render_spec for "
                  f"avatar {avatar['avatar_id']!r} — leaving sprite_ref unset")
        return

    try:
        manifest_dict = library.load_manifest(sprite_store.dir)
        if library.resolve(render_spec, manifest_dict) is not None:
            avatar["sprite_ref"] = get_or_render_sprite(
                render_spec, sprite_store)
        else:
            generic_ref = _generic_sprite_ref_for_kind(
                "avatar", sprite_store, manifest_dict)
            avatar["sprite_ref"] = (
                generic_ref if generic_ref is not None
                else get_or_render_sprite(render_spec, sprite_store))
    except Exception as e:  # noqa: BLE001 — never let a bad spec 500 /scene
        print(f"[webrender] avatar sprite render failed for avatar "
              f"{avatar['avatar_id']!r}: {e}")


def _enrich_avatars_with_sprites(avatars: list[dict[str, Any]],
                                  world_url: str,
                                  sprite_store: SpriteStore,
                                  viewer_agent: str | None,
                                  world_token: str | None) -> None:
    """Runs `_apply_avatar_render_spec` for at most `_MAX_SPRITE_LOOKUPS`
    avatars (in `/scene` response order) — same perf/safety bound
    `_enrich_things_with_sprites` uses for things. The session's viewer and
    world token are forwarded (umwelt#982). Mutates `avatars` in place."""
    for avatar in avatars[:_MAX_SPRITE_LOOKUPS]:
        _apply_avatar_render_spec(avatar, world_url, sprite_store,
                                  viewer_agent, world_token)


# --- place-marker sprites (2026-07-24 world-finishing sprint) --------------
#
# A DISTINCT surface from `_enrich_place_with_tiles` below: lane_tiles paint
# the sky/land/road backgrounds of the place you're CURRENTLY standing in;
# this enriches `exits`/`parent` — the door/marker buttons representing
# OTHER places as destinations while you're elsewhere. Confirmed from the
# code (forum #277 #5000/#5006) that no sprite slot exists for either
# today: `_build_scene`'s `exits`/`parent` entries are bare
# `{place_id, label, relation?}`, and app.js's `buildDoorElement` renders
# every destination with the SAME fixed CSS `.door-shape`, regardless of
# which place it leads to — that flat rectangle is Lei's "green squares".
#
# `generic_place` was originally the ONE shared tier-2 fallback for every
# place with no custom art yet, regardless of what kind of place it was.
# forum #281 (2026-07-25, Lei's two-layer place-art design, Kepler-ratified
# GO) splits this by `render_type` — a place is either `indoor-room` (the
# original door) or `outdoor-region` (a path/road marker, "you can walk
# this way" rather than the door's "you go through this") — a direct
# structural mirror of things' `_GENERIC_RENDER_SPEC_BY_KIND` (keyed dict,
# same pattern, new key axis: `render_type` instead of `kind`). `render_type`
# itself lives in `metadata.render_type` (Kepler's ruling, forum #281 reply
# #5113 Q1: a render-semantic field, same posture as `lane_tiles` and
# per-place `render_spec` — no migration, no new fetch, both read sites
# already have the metadata dict in hand). Per-place exact art (Sol's
# `place_tiles.json` batch, forum #5004) remains tier 1, set via each
# place's own `metadata.render_spec` — the same per-instance write path
# `lane_tiles` already uses (`update_place`'s metadata patch), just a
# different key.
_GENERIC_PLACE_RENDER_SPEC_BY_TYPE: dict[str, dict[str, Any]] = {
    "indoor-room": {
        "class": "generic_place",
        "descriptors": ["a wooden door", "destination placeholder"],
        "palette": "warm-wood",
        "mode": "object",
    },
    "outdoor-region": {
        "class": "generic_place_path",
        "descriptors": ["weathered stone archway with a dirt path leading through it",
                         "trail marker"],
        "palette": "cool-stone",
        "mode": "object",
    },
}


def _place_render_type(place_metadata: dict[str, Any]) -> str | None:
    """Reads `render_type` off a place's metadata dict, coercing anything
    that isn't a string (missing key, `None`, or a malformed non-string
    value from schema drift) to `None` -- the single shared shape both
    Layer 1 (`_apply_place_marker_sprite`) and Layer 2
    (`_enrich_place_with_tiles`) fall back on before applying their own
    (different, deliberately so -- see forum #281 §5) None-default."""
    render_type = place_metadata.get("render_type")
    return render_type if isinstance(render_type, str) else None


def _generic_sprite_ref_for_place(
    render_type: str | None, sprite_store: SpriteStore, manifest_dict: dict,
) -> str | None:
    """Place counterpart to `_generic_sprite_ref_for_kind` — keyed on
    `render_type` instead of `kind` (see module comment above).

    Unlike `_generic_sprite_ref_for_kind` (an unmapped `kind` returns `None`,
    falling straight through to procedural), an unset or unrecognized
    `render_type` here falls back to the `indoor-room` spec, NOT `None` --
    this is the backward-compat requirement from forum #281 §5 ("a place
    with unset render_type renders today's behavior: the wooden door"),
    which predates this two-way split and must keep working unconditionally
    for every place that hasn't been assigned a render_type yet.

    Returns `None` (never raises) only when the resolved spec (whichever
    type it is) isn't live in the manifest yet, same "not populated ->
    caller falls through to procedural" contract things' tier 2 has."""
    generic_spec = _GENERIC_PLACE_RENDER_SPEC_BY_TYPE.get(
        render_type, _GENERIC_PLACE_RENDER_SPEC_BY_TYPE["indoor-room"])
    if library.resolve(generic_spec, manifest_dict) is None:
        return None
    return get_or_render_sprite(generic_spec, sprite_store)


def _apply_place_marker_sprite(place_entry: dict[str, Any], world_url: str,
                                sprite_store: SpriteStore,
                                viewer_agent: str | None,
                                world_token: str | None) -> None:
    """Mutates a single `exits`/`parent` entry in place, adding
    `sprite_ref`: fetches the DESTINATION place's own metadata via the
    read-only `/umwelt/place_view` (the same route `/scene`'s own route
    handler already calls for the CURRENT place — reused here per
    `place_entry["place_id"]` instead), then resolves `metadata.render_spec`
    through the same three-tier pipeline things/avatars use: exact curated
    match -> live `generic_place` -> procedural.

    (2026-07-24, post-rebase correction, per Borges's forum #277 #5027
    catch: my first version of this function returned early — leaving
    `sprite_ref: None` — whenever `render_spec` was missing entirely, so
    the MOST common case (an undressed place, no render_spec at all) never
    even tried the `generic_place` fallback. That is exactly umwelt#336's
    bare-thing bug (`_apply_render_spec` had the identical shape before
    that fix), reproduced here because this function was written against a
    stale pre-#338 branch base. Mirrors #338's fix precisely: a FINAL
    `if place_entry["sprite_ref"] is None:` tier runs unconditionally after
    the render_spec handling above, regardless of WHY sprite_ref is still
    unset — no render_spec, a malformed one, or a render exception — so a
    bare place now reaches `generic_place` too, not just a place with a
    render_spec that merely didn't have an exact curated match.)

    Only when even that final tier yields nothing (no live `generic_place`
    yet) does `sprite_ref` stay `None` — the client's `.door-shape` CSS is
    the fallback, same graceful-degradation shape as an un-arted thing's
    colored rectangle.

    `place_entry` is never mutated to leak the destination's INTERIOR — only
    ever the sprite ref. Corrected per umwelt#538 rev 3: `exits`/`parent`
    CAN now contain a private destination (place_tree emits its public
    projection regardless of ownership — that is the whole point of rule
    2), so the earlier claim that a private destination "simply can't
    appear here" no longer holds. What still holds is the disclosure
    property that claim was really protecting: this function's own
    `/umwelt/place_view` lookup is the FULL-interior read (unchanged by
    #538 — see that route's docstring), so a caller who can't enter the
    destination gets the same 404 here it always did, and the entry
    degrades to `sprite_ref: None` (the CSS door-shape fallback) rather
    than leaking render metadata. The function adds no information
    disclosure beyond what `exits` already carries.

    Any upstream failure (world unreachable, malformed spec, manifest
    load failure) is caught and logged, never propagated — a marker stays
    at `sprite_ref: None` (the CSS fallback) rather than breaking `/scene`.
    """
    place_entry["sprite_ref"] = None
    try:
        detail = _place_view_as(
            world_url, place_entry["place_id"], viewer_agent, world_token,
            timeout=5.0)
    except (WorldHTTPError, WorldUnreachable) as e:
        print(f"[webrender] place-marker lookup failed for place "
              f"{place_entry['place_id']!r}: {e}")
        return

    metadata = (detail.get("place") or {}).get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    render_type = _place_render_type(metadata)

    render_spec = metadata.get("render_spec")
    if (isinstance(render_spec, dict)
            and isinstance(render_spec.get("class"), str)
            and render_spec.get("class")):
        try:
            manifest_dict = library.load_manifest(sprite_store.dir)
            if library.resolve(render_spec, manifest_dict) is not None:
                place_entry["sprite_ref"] = get_or_render_sprite(
                    render_spec, sprite_store)
            else:
                generic_ref = _generic_sprite_ref_for_place(
                    render_type, sprite_store, manifest_dict)
                place_entry["sprite_ref"] = (
                    generic_ref if generic_ref is not None
                    else get_or_render_sprite(render_spec, sprite_store))
        except Exception as e:  # noqa: BLE001 — never let a bad spec 500 /scene
            print(f"[webrender] place-marker render failed for place "
                  f"{place_entry['place_id']!r}: {e}")
    elif render_spec is not None:
        print(f"[webrender] rejected malformed place render_spec for "
              f"place {place_entry['place_id']!r} — using render_type "
              f"generic fallback")

    # umwelt#336-shaped final tier (see docstring above): still no sprite —
    # no render_spec at all (the common undressed-place case), a malformed
    # one, or a render error above — still gets the live `generic_place`
    # if one exists. Unconditional on WHY sprite_ref is None, same as
    # _apply_render_spec's own bare-thing tier.
    if place_entry["sprite_ref"] is None:
        try:
            manifest_dict = library.load_manifest(sprite_store.dir)
            place_entry["sprite_ref"] = _generic_sprite_ref_for_place(
                render_type, sprite_store, manifest_dict)
        except Exception as e:  # noqa: BLE001 — never let this 500 /scene
            print(f"[webrender] generic-place fallback (no render_spec) "
                  f"failed for place {place_entry['place_id']!r}: {e}")


def _enrich_place_markers_with_sprites(
    exits: list[dict[str, Any]], parent: dict[str, Any] | None,
    world_url: str, sprite_store: SpriteStore,
    viewer_agent: str | None, world_token: str | None,
) -> None:
    """Runs `_apply_place_marker_sprite` for every `exits` entry (bounded by
    `_MAX_SPRITE_LOOKUPS`, same perf/safety posture as things/avatars) plus
    the single `parent` entry when present (the back-door — always exactly
    0 or 1, no cap needed). Mutates both in place.

    umwelt#656: the viewer is threaded down from `/scene`'s session, so the
    door onto your OWN private post renders its real sprite instead of
    degrading to the CSS shape. Both identity arguments are required
    positionals here for the same reason they are on `_place_view_as` — an
    anonymous enrichment must say so out loud."""
    for exit_entry in exits[:_MAX_SPRITE_LOOKUPS]:
        _apply_place_marker_sprite(exit_entry, world_url, sprite_store,
                                    viewer_agent, world_token)
    if parent is not None:
        _apply_place_marker_sprite(parent, world_url, sprite_store,
                                    viewer_agent, world_token)


# forum #281 Layer 2 (2026-07-25, Kepler-ratified GO): when a place has no
# EXPLICIT `metadata.lane_tiles` at all, pick a default set by its
# `render_type` rather than rendering no tiles. `outdoor-region`'s default
# is the empty set -- literally today's behavior (every undressed place,
# outdoor or not, currently gets no lane tiles at all; per spec §5 this is
# the explicit backward-compat anchor for BOTH an unset render_type and an
# outdoor-region one). `indoor-room`'s default formalizes the interior
# treatment that's so far been per-place-manual (office/library/
# workshop/hall tiles): a generic wall placeholder in the sky slot ("sky"
# here just means "the upper band" -- an indoor room has no actual sky).
# Deliberately NOT curated-art-generated yet (the generative
# pipeline can't reliably produce flat/gradient tiles like this one --
# procedural is the right tool, not a reroll fight) -- resolves via the
# same tier-3 procedural fallback `get_or_render_sprite` already provides,
# same graceful-degradation shape as the door/path markers before their
# own curated art lands.
_GENERIC_LANE_TILES_BY_TYPE: dict[str, dict[str, dict[str, Any]]] = {
    "outdoor-region": {},
    "indoor-room": {
        "sky": {
            "class": "generic_wall",
            "descriptors": ["a plain interior wall", "room backdrop placeholder"],
            "palette": "cool-stone",
            "mode": "object",
        },
    },
}


def _enrich_place_with_tiles(place_metadata: dict[str, Any], world_url: str,
                              sprite_store: SpriteStore) -> dict[str, str]:
    """Place-level analog of `_apply_render_spec`/`_enrich_things_with_sprites`
    (place-lane-tiles spec §3): for each of `"sky"`/`"land"`/`"road"` present
    under `place_metadata.get("lane_tiles", {})`, resolves a sprite_ref via
    the SAME `get_or_render_sprite`/`sprite_store` things already use — no
    separate cache, no separate storage.

    `world_url` is unused today (unlike `_apply_render_spec`, this function
    is handed the place's `metadata` directly — already present on `wv["place"]`
    via server.py's `_place_view`, umwelt place-lane-tiles spec §1/§3 — so no
    second upstream round-trip is needed to fetch it). Kept in the signature
    for parity with `_apply_render_spec`'s call shape and in case a future
    renderer backend needs one; never dereferenced here.

    forum #281 Layer 2: when `metadata.lane_tiles` is missing or malformed
    (not a dict), falls back to `_GENERIC_LANE_TILES_BY_TYPE`, keyed by this
    SAME metadata's `render_type` (unset/unrecognized -> `outdoor-region`'s
    empty default, per spec §5 -- see that dict's module comment). An
    EXPLICIT `metadata.lane_tiles`, even a partial one (e.g. only `"sky"`
    set), always wins outright and is never merged with the type default —
    a place that has opted into manual lane tiles owns that field entirely.

    A lane is included in the result ONLY when its render_spec is a dict
    with a non-empty string `"class"` AND `get_or_render_sprite` succeeds —
    missing lanes simply produce no entry (never a `None`/null placeholder).
    Any failure (bad shape, renderer exception) is caught and logged here,
    never raised — mirrors `_apply_render_spec`'s existing never-break-the-
    whole-scene posture exactly: one place's malformed tile spec must never
    5xx `/scene` for anyone standing there.
    """
    del world_url  # unused — see docstring
    place_metadata = place_metadata or {}
    lane_tiles = place_metadata.get("lane_tiles")
    if not isinstance(lane_tiles, dict):
        render_type = _place_render_type(place_metadata)
        lane_tiles = _GENERIC_LANE_TILES_BY_TYPE.get(
            render_type, _GENERIC_LANE_TILES_BY_TYPE["outdoor-region"])
    resolved: dict[str, str] = {}
    for lane in ("sky", "land", "road"):
        render_spec = lane_tiles.get(lane)
        if not (isinstance(render_spec, dict)
                and isinstance(render_spec.get("class"), str)
                and render_spec.get("class")):
            if render_spec is not None:
                print(f"[webrender] rejected malformed lane_tiles.{lane} "
                      f"render_spec — falling back to no tile for that lane")
            continue
        try:
            resolved[lane] = get_or_render_sprite(render_spec, sprite_store)
        except Exception as e:  # noqa: BLE001 — never let a bad tile spec 500 /scene
            print(f"[webrender] place tile render failed for lane "
                  f"{lane!r}: {e}")
    return resolved


def _lane_key(raw_lane: Any, missing_default: str) -> str:
    """Normalize an item's `lane` value to one of `_VALID_LANES`, for use as
    a key into `things_by_lane`/`avatars_by_lane`/`lane_cells` below.

    `missing_default` is used only when `raw_lane` is absent (`None`) —
    matching the pre-existing per-source defaults (`land` for things,
    `road` for avatars). Any PRESENT-but-unrecognized value (not one of the
    3 valid lane strings) degrades to `land` instead of being used as-is:
    the gateway and the world server it calls are separately deployable
    processes talking over HTTP, not the same address space, so a
    schema-drift lane value here (e.g. version skew between the two
    processes) is a real possibility even though the DB CHECK constraint
    already makes bad data impossible at rest — that guarantee doesn't
    reach across the process boundary. Without this, an out-of-enum lane
    value would KeyError on the fixed-3-key dicts below and turn into an
    unhandled 500 for what should be a harmless render fallback.
    """
    if raw_lane is None:
        return missing_default
    return raw_lane if raw_lane in _VALID_LANES else "land"


def _build_scene(wv: dict[str, Any]) -> dict[str, Any]:
    """Build this gateway's `/scene` response contract from the world
    server's `_place_view` shape (`wv` — the response body of the read-only
    `GET /umwelt/place_view` route).

    Read-only by construction, not by discipline: this gateway used to
    re-issue `POST /umwelt/move_to` (or `POST /umwelt/enter` when no `place`
    was given) to fetch a place's contents — both REAL mutations (they move
    an avatar's standpoint/body, and `enter` can birth a brand-new avatar
    from any typed `viewer` string). A plain `GET /scene` could therefore
    relocate any named avatar or mint one, silently, from a spectator-facing
    read endpoint. `place_view` is the world server's dedicated read-only
    counterpart — no writes, no identity check, public-places-only — so this
    gateway's own read-only posture is now enforced by the endpoint it calls,
    not merely by this file's own good behavior.

    ```
    {
      "place":   {id, label, description, grid: [A, B],
                  lane_tiles?: {sky?, land?, road?}},
      "exits":   [{place_id, label, relation: "child"|"link", sprite_ref,
                   privacy_marker}, ...],
      "parent":  {place_id, label, sprite_ref, privacy_marker} or None,
      "things":  [{...every field from wv.things[i]... (incl. `lane`,
                   `kind_id` — umwelt #522), cell, footprint, sprite_ref,
                   color, marker, actions}, ...],
      "avatars": [{agent, avatar_id, name, type, emotion, lane, sprite_ref,
                   cell, marker}, ...],
      "cursor":  wv.unseen_count,
    }
    ```

    `parent` (umwelt #238): `wv["parent"]` (see `_place_view`'s docstring in
    server.py) reshaped into the same trimmed `{place_id, label}` shape
    `exits`' entries already use, so the frontend can treat a parent-button
    and an exit-button identically. `None` when `wv["parent"]` is `None`
    (top-level place, or no parent row) — never omitted, always present as a
    key.

    `relation` (umwelt #239 street-view build-input): `wv["reachable"]`
    itself now carries a `relation: "child"|"link"|"parent"` tag per entry
    (server.py's `_place_view` docstring has the full rationale — a
    street-view renderer needs one typed list to place doors deterministically,
    not two differently-shaped fields to reconcile). This gateway forwards
    that tag onto `exits`, but deliberately EXCLUDES `relation == "parent"`
    entries from `exits` — that entry is already surfaced as the standalone
    `parent` key above, and forwarding it into `exits` too would render two
    buttons to the same destination (one from the exits loop, one from the
    dedicated parent button). `exits` stays child+link only, same set as
    before this change; only the per-entry `relation` tag is new.

    `privacy_marker` on `exits`/`parent` entries (umwelt#543): the
    door/parent's already-rendered `[state][owner: x]` authorization-marker
    STRING, forwarded verbatim from `wv["reachable"][i]["marker"]`/
    `wv["parent"]["marker"]` (server.py's `_place_view` computes it — see
    `db._place_public_projection`). This gateway never reconstructs the
    marker grammar itself, same posture as every other pure-forward field
    here. Named `privacy_marker`, not `marker` — `things[i].marker` a few
    lines below is a pre-existing, unrelated field (a <=2-char
    sprite-overlay badge, `_validate_marker`'s docstring), and this
    response object is exactly the place both concepts would otherwise
    collide under one key name.

    `exits` ordering (umwelt #240 street-view FRAME build-input): the list
    is now sorted by `label` (plain stable ascending string sort, no i18n
    requirement — Kepler's "stable sort by label, not arbitrary" note on
    forum #267) before being returned, regardless of the creation/link
    order `wv["reachable"]` arrived in. This is what makes door placement
    along the street deterministic and stable between renders — a client
    renders `exits` in the order given and never re-sorts client-side.
    `parent` is NOT part of this sort — it stays a separate field, always
    drawn at its own fixed edge (a street-view design requirement), never
    interleaved with the sorted `exits` list.

    `avatar_id` (umwelt #239): the avatar's underlying THING id — distinct
    from `agent` (the canonical agent-id string, `things.author`). Pure
    passthrough of `wv["present"][i]["avatar_id"]`, which `place_view`
    already returns (server.py's `_present_summary`) — no new upstream
    call, no gateway-side re-derivation. This is the field a client must
    send as `GET /inspect?thing=<avatar_id>` to fetch an avatar's status
    block: `/inspect` resolves `thing` via `db.get_thing`'s direct
    primary-key lookup, which `agent` does NOT satisfy (an avatar's thing
    `id` is an unrelated uuid4, not equal to its `author`/`agent` string).

    `emotion` (umwelt#240 fast-follow, forum #267 #4779): pure passthrough
    of `wv["present"][i]["emotion"]` — same "no new upstream call, no
    gateway-side re-derivation" posture as `avatar_id` above. Always
    present (server.py's `_present_summary`/`_agent_emotion` never omit
    it), sourced identically to the click-to-inspect panel's own emotion
    field so the two can never disagree for the same avatar. This is the
    raw emotion STRING — the street client is responsible for mapping it
    through its own closed recognized-emotion set with a deterministic
    hash-based fallback for anything unrecognized; this gateway does no
    interpretation of the value, it only forwards it.

    `cursor` is `unseen_count` verbatim — a deliberate M0 simplification
    (the real crossings-seq cursor is an M1 concern once the crossings
    sidebar is built), flagged in the handoff for Borges to confirm.

    `lane` (umwelt #278 — sky/land/road render bands): cell assignment is
    now driven ENTIRELY by each item's own `lane` value, never by which
    response list (`wv["things"]` vs `wv["present"]`) it came from and never
    by `kind`. All things AND avatars are partitioned together into three
    groups — `sky` (top rows), `land` (middle rows), `road` (bottom rows,
    same last-row anchoring the old avatar-reservation logic used) — via
    `_grid_size`/`_assign_cells`, so no two items across any lane can ever
    collide on the same cell. `wv["things"][i]["lane"]` arrives already
    populated (added to `db._THING_COLS`/`_thing_row_to_dict`, umwelt #278
    §1); `wv["present"][i]["lane"]` is a pure passthrough of the avatar
    THING's own `lane` column, wired in server.py's `_present_summary` the
    same "no new upstream call, no gateway-side re-derivation" way
    `emotion`/`avatar_id` already are. `things` still get their OWN cell
    dict keyed by thing `id`; `avatars` still get their own cell dict keyed
    by `agent` string (unchanged from before this rewrite) — but which of
    the three per-lane cell dicts an item's key is looked up in now depends
    solely on its `lane`, not on which of the two source lists it came from.
    In today's real-world data (nothing has opted into `sky` yet, and every
    avatar is engine-stamped `road` at birth with no lane-mutation path in
    this PR) this produces PIXEL-IDENTICAL output to the old two-band
    logic: `land` == the old `things` band (offset 0), `road` == the old
    `avatars` band (bottom-anchored) — see test_scene_cell_assignment_
    matches_pre_lane_two_band_output for the regression guard.

    `sprite_ref` on `exits`/`parent` entries (2026-07-24 world-finishing
    sprint): also NOT set by this function, for the same reason as
    `lane_tiles` below — added by the route handler AFTER calling this
    function (`_enrich_place_markers_with_sprites`), since resolving it is a
    sprite-pipeline call. Always present (never omitted), `None` when the
    destination has no valid render_spec and no live `generic_place`
    fallback yet — the client's `.door-shape` CSS covers that case.

    `lane_tiles` (place-lane-tiles spec §3): NOT set by this function —
    this docstring documents the field's presence in the overall response
    contract, but it's added by the `/scene` route handler AFTER calling
    this function (`_enrich_place_with_tiles`, same "called once per route
    invocation, passing the same world_url/sprite_store already in scope"
    pattern `_enrich_things_with_sprites` already uses), since resolving it
    is a sprite-pipeline call, not a pure reshape of `wv`. Only present when
    at least one lane resolved a tile — omitted entirely (not an empty
    dict, not per-lane nulls) when the place has no `lane_tiles` set at all
    or every entry was malformed.
    """
    things = wv.get("things", [])
    avatars = wv.get("present", [])

    things_by_lane: dict[str, list] = {"sky": [], "land": [], "road": []}
    for t in things:
        things_by_lane[_lane_key(t.get("lane"), "land")].append(t)
    avatars_by_lane: dict[str, list] = {"sky": [], "land": [], "road": []}
    for p in avatars:
        avatars_by_lane[_lane_key(p.get("lane"), "road")].append(p)

    lane_count = {
        lane: len(things_by_lane[lane]) + len(avatars_by_lane[lane])
        for lane in ("sky", "land", "road")
    }
    grid_a, grid_b = _grid_size(lane_count["sky"], lane_count["land"],
                                 lane_count["road"])

    lane_rows = {
        lane: (math.ceil(lane_count[lane] / grid_a) if lane_count[lane] else 0)
        for lane in ("sky", "land", "road")
    }
    # sky is top-anchored (offset 0); land follows immediately after sky;
    # road is bottom-anchored (mirrors the old avatar-reservation formula
    # exactly) — with sky empty (today's reality), land's offset is 0, same
    # as the old thing-band's fixed offset.
    lane_offset = {
        "sky": 0,
        "land": lane_rows["sky"],
        "road": grid_b - lane_rows["road"],
    }

    lane_cells: dict[str, dict[str, tuple[int, int]]] = {}
    for lane in ("sky", "land", "road"):
        item_keys = ([t["id"] for t in things_by_lane[lane]]
                     + [p["agent"] for p in avatars_by_lane[lane]])
        lane_cells[lane] = _assign_cells(item_keys, grid_a, lane_offset[lane])

    out_things = []
    for t in things:
        t_lane = _lane_key(t.get("lane"), "land")
        col, row = lane_cells[t_lane][t["id"]]
        out_things.append({
            **t,
            "lane": t_lane,  # normalized (umwelt #283 nit 1) — never the
                             # raw, possibly-None/out-of-enum DB value that
                             # `**t` would otherwise spread in verbatim.
            "cell": [col, row],
            "footprint": [1, 1],
            "sprite_ref": None,
            "color": _KIND_COLORS.get(t["kind"], _DEFAULT_COLOR),
            "marker": None,
            "actions": _compose_thing_actions(t["kind"]),
        })

    out_avatars = []
    for p in avatars:
        p_lane = _lane_key(p.get("lane"), "road")
        col, row = lane_cells[p_lane][p["agent"]]
        out_avatars.append({
            "agent": p["agent"],
            "avatar_id": p["avatar_id"],
            "name": p["name"],
            "type": p.get("type"),
            "emotion": p.get("emotion"),
            "lane": p_lane,  # normalized (umwelt #283 nit 1) — see out_things above.
            "sprite_ref": None,
            "cell": [col, row],
            "marker": None,
        })

    place = wv["place"]
    return {
        "place": {
            "id": place["id"], "label": place["label"],
            "description": place["description"], "grid": [grid_a, grid_b],
        },
        "exits": sorted(
            ({"place_id": r["id"], "label": r["label"],
              "relation": r["relation"],
              # umwelt#543: the door's own [state][owner: x] authorization
              # marker — already computed server-side in _place_view's
              # `reachable`, forwarded verbatim (never re-derived here).
              # Named `privacy_marker`, NOT `marker` — `things[i].marker`
              # (this same response, below) is a pre-existing, unrelated
              # field (a <=2-char sprite-overlay badge, `_validate_marker`),
              # and reusing the key name across two semantically different
              # concepts in one response is exactly the kind of ambiguity
              # this ruling's own SSoT discipline argues against elsewhere.
              "privacy_marker": r.get("marker")}
             for r in wv.get("reachable", [])
             if r.get("relation") != "parent"),
            key=lambda e: e["label"]),
        "parent": ({"place_id": wv["parent"]["id"], "label": wv["parent"]["label"],
                    "privacy_marker": wv["parent"].get("marker")}
                   if wv.get("parent") else None),
        "things": out_things,
        "avatars": out_avatars,
        "cursor": wv.get("unseen_count"),
    }


def _resolve_avatar_place(
        world_url: str, avatar: str, token: str | None = None) -> str:
    """Resolves `avatar`'s CURRENT place server-side via the read-only
    `POST /umwelt/look` route (umwelt #238 — "login should spawn at the
    avatar's own place" instead of requiring a typed place string). Shared
    by `/session/login` (fresh login) and `GET /session` (page-reload
    recovery) so both flows resolve a real place the same way, rather than
    duplicating this fallback logic twice.

    `token`, optional (umwelt#495 slice 1a follow-through): forwarded to
    `_world_request` as `Authorization: Bearer <token>` when the caller has
    one. `look` is one of slice 1a's `resolve_actor`-converted routes, so a
    real world session token here lets the world resolve the caller from a
    verified credential instead of the `agent` field alone — omitting it
    (the default) preserves the exact prior declared-`agent` behavior,
    since `resolve_actor` falls through to `declared` only when NO
    `Authorization` header is sent at all. NOTE (umwelt#618): that is no
    longer "never a hard-fail either way" — a header that IS sent but
    carries a token which does not resolve (unknown, expired, retired,
    credential-reset), or an empty one, now returns 401 rather than
    downgrading to `declared`. Omitting the token remains the safe
    prior-behavior path; sending a bad one is now refused.

    Falls back to `"place_square"` — matching the existing client-side
    fallback hint in app.js's error text ("place is required — try
    place_square") — on:

    - `WorldHTTPError` with `.status == 404`: the avatar identity is valid
      but has never called `/umwelt/enter`, so `/umwelt/look` has no avatar
      row to resolve a place from. Deliberately does NOT auto-provision an
      avatar here (no `/umwelt/enter` call) — out of scope for this fix.
    - `WorldUnreachable`, or any other `WorldHTTPError` status: a transient
      world-server hiccup during this sub-call must never block login/
      session-status itself — the caller's identity is still valid
      regardless of whether this lookup succeeded. Logged (not silently
      swallowed) for the non-404 cases — same "caught here and logged,
      never silently swallowed" discipline as the sprite-lookup fallback
      above, so a REAL bug in this path is distinguishable from the
      documented not-entered-yet case in the gateway's own logs.
    """
    try:
        look = _world_request(world_url, "/umwelt/look", {"agent": avatar},
                               timeout=10.0, token=token)
    except WorldHTTPError as e:
        if e.status != 404:
            print(f"[webrender] avatar-place resolve for {avatar!r} got "
                  f"unexpected world status {e.status}: {e.body}")
        return "place_square"
    except WorldUnreachable as e:
        print(f"[webrender] avatar-place resolve for {avatar!r}: "
              f"world unreachable: {e}")
        return "place_square"
    return look.get("place", {}).get("id") or "place_square"


_DEFAULT_CLIENT_ERROR_LOG_PATH = Path.home() / ".umwelt" / "client_errors.jsonl"
_CLIENT_ERROR_MAX_BODY_BYTES = 8 * 1024
_CLIENT_ERROR_RATE_LIMIT_PER_MINUTE = 20
_CLIENT_ERROR_RATE_WINDOW_SECONDS = 60.0
_CLIENT_ERROR_LOG_ROTATE_BYTES = 5 * 1024 * 1024


def _client_error_rate_allow(app: Flask, remote_addr: str) -> bool:
    """True iff `remote_addr` is still under
    `_CLIENT_ERROR_RATE_LIMIT_PER_MINUTE` reports in the trailing
    `_CLIENT_ERROR_RATE_WINDOW_SECONDS` -- a sliding window per client IP,
    kept on `app.config` (not a module global) so each `create_gateway_app`
    call -- and so each test's `gw_client` fixture -- starts with an empty
    window rather than leaking state across tests or across processes that
    share this module. Guarded by a lock since Flask's dev/threaded server
    can serve concurrent requests; the state itself (a dict of deques) is
    small and bounded by the number of distinct recent client IPs times
    `_CLIENT_ERROR_RATE_LIMIT_PER_MINUTE`, not by request volume, since
    entries older than the window are pruned on every call."""
    state = app.config.setdefault("CLIENT_ERROR_RATE_STATE", {})
    lock = app.config.setdefault(
        "CLIENT_ERROR_RATE_LOCK", threading.Lock())
    now = time.monotonic()
    with lock:
        window = state.setdefault(remote_addr, deque())
        cutoff = now - _CLIENT_ERROR_RATE_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= _CLIENT_ERROR_RATE_LIMIT_PER_MINUTE:
            return False
        window.append(now)
        return True


def create_app(world_url: str, sprite_dir: str | Path | None = None,
                art_service_url: str | None = None,
                voice_service_url: str | None = None,
                client_error_log_path: str | Path | None = None,
                tls_enabled: bool = False) -> Flask:
    """`sprite_dir` (new in M2, keyword-optional, defaults to the module's
    own `webrender/sprites/` cache dir — see `SpriteStore`) lets tests point
    the sprite cache at a `tmp_path` so repeated test runs never share
    on-disk cache state across test processes. Originally a test-only
    injection seam (`main()` never passed it); art-library slice 1 wires a
    real production caller — `main()` now passes `--sprite-dir` (default
    `~/.umwelt/sprite-library/`, see `_DEFAULT_SPRITE_DIR`), so this
    signature/behavior is unchanged but the param is exercised for real at
    boot, not just in tests.

    `art_service_url` (M4 art-gen slice 1, umwelt#319) — the
    standalone SDXL generation service's base URL
    (internal tooling, not in this release), used by the `POST /library/generate`
    route below to build an `ImageModelRenderer`. `None` (the default) means
    "not configured" — the route returns a clean 503 rather than crashing;
    this gateway must keep serving `/scene`/`/sprite`/etc. even when no
    art-gen service is deployed alongside it (enhancement, never a
    dependency — Kepler #3).

    `voice_service_url` (voice-playback v0, #309) — the standalone
    voice-playback service's base URL (internal tooling, not in this release),
    used by the `POST /voice` route below. `None` (the default, same
    posture as `art_service_url` above) means "not configured" — the
    route returns a clean 503 rather than crashing.

    `client_error_log_path` (umwelt#286 diagnostic instrumentation) —
    where `POST /debug/client_error` appends crash reports (test seam,
    same convention as the voice service's `request_log_dir` (internal tooling, not in this release): tests
    point this at a `tmp_path` file so they never touch a shared
    production log). Defaults to `~/.umwelt/client_errors.jsonl`.

    `tls_enabled` (umwelt#688, part of #583) — whether THIS gateway
    process resolved a real `ssl.SSLContext` from `--tls-cert`/
    `--tls-key` at all (see `tls_context.py`), stored as
    `app.config["TLS_ENABLED"]` — informational (surfaced to tests and
    callers that want to know the deployment's TLS posture), and no
    longer what drives the session cookie's `Secure` flag. It drove that
    flag directly under #688's original single-listener assumption (a
    process was either all-TLS or all-plaintext); the #699 dual-listener
    design broke that assumption — one process can now serve both
    transports at once — so `/session/login`'s `set_cookie` call derives
    `Secure` from `request.is_secure` (this request's own transport)
    instead. See that route's own docstring for the full correction
    (Mira's security-lane finding on #699's gateway PR). Defaults to
    `False` (today's plaintext posture, unchanged for every existing
    caller of this function)."""
    app = Flask(__name__, static_folder=str(_STATIC_DIR), static_url_path="")
    app.config["WORLD_URL"] = world_url
    app.config["TLS_ENABLED"] = tls_enabled
    app.config["ART_SERVICE_URL"] = art_service_url
    app.config["VOICE_SERVICE_URL"] = voice_service_url
    app.config["CLIENT_ERROR_LOG_PATH"] = Path(
        client_error_log_path if client_error_log_path is not None
        else _DEFAULT_CLIENT_ERROR_LOG_PATH)
    sprite_store = SpriteStore(sprite_dir)
    app.config["SPRITE_STORE"] = sprite_store
    # Pending-staging: SpriteStore is a generic directory-backed
    # content-addressed cache, so pointing a second instance at a `.pending`
    # subdirectory of the SAME sprite_dir reuses it as-is rather than
    # inventing a parallel class (umwelt#319 layer (a) — see
    # `library_generate`/`library_approve` below for the quarantine/promote
    # discipline this store exists to hold).
    app.config["PENDING_STORE"] = SpriteStore(sprite_store.dir / ".pending")
    session_store = SessionStore()
    app.config["SESSION_STORE"] = session_store
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @app.route("/health")
    def health() -> Response:
        world_reachable = True
        world_body: Any = None
        try:
            world_body = _world_request(world_url, "/health", None, timeout=2.0)
        except (WorldUnreachable, WorldHTTPError):
            world_reachable = False
        # umwelt#1029: carry the world's own status/db error so a watcher of
        # the gateway alone can see a degraded world. Additive only: the
        # gateway's `status` and `world_reachable` keep their meaning.
        world_status = None
        world_db_error = None
        if isinstance(world_body, dict):
            if isinstance(world_body.get("status"), str):
                world_status = world_body["status"]
            probe = world_body.get("db_probe")
            if (isinstance(probe, dict) and probe.get("ok") is False
                    and isinstance(probe.get("error"), str)
                    and probe["error"]):
                world_db_error = probe["error"]
        return jsonify({
            "status": "ok",
            "world_url": world_url,
            "started_at": started_at,
            "world_reachable": world_reachable,
            "world_status": world_status,
            "world_db_error": world_db_error,
        })

    @app.route("/scene")
    def scene() -> Response:
        """`GET /scene?place=<id>&viewer=<agent>` — see `_build_scene`.

        How the underlying world data is fetched: a single `GET
        /umwelt/place_view?place=<id>` to the world server's dedicated
        read-only route — never a mutating call. `place` is now REQUIRED
        (there is no more "no place given, fall through to enter" case —
        that fallback was itself part of the bug this route used to have:
        `enter` is a mutating birth/reconnect call, not a read).

        `viewer` stays a required query param on THIS route (display
        purposes only — e.g. distinguishing "which avatar in `present` is
        you" client-side) and remains a pure, UNTRUSTED client-side label —
        NOTE for future readers (Aleph's off-host review, F3): do NOT read
        `viewer`'s presence as any form of per-viewer access scoping or
        authentication on THIS route's own content — it grants nothing and
        restricts nothing here, unauthenticated or not, exactly as before.

        umwelt#554: `place_view` is NO LONGER fully anonymous by design (see
        its own docstring in server.py) — it now takes an OPTIONAL `agent`,
        and this route DOES supply one, but never from `viewer` above.
        Instead: this route reads the SESSION cookie directly (same
        `request.cookies.get(_SESSION_COOKIE_NAME)` → `session_store.get`
        lookup `/act` already does, just without `/act`'s hard 401/CSRF
        gates — a caller with no valid session still gets a scene, the
        anonymous/public-only one, not an error) and forwards
        `session["avatar"]` — the one server-resolved, unforgeable-by-THIS-
        route identity available anywhere in this file — as `place_view`'s
        `agent`. This is what lets a LOGGED-IN human see their own private
        place's interior through `/scene` for the first time — a genuine,
        independently-code-verified defect (umwelt#554), found while
        investigating a live report from Lei that turned out, on closer
        log tracing, to have a DIFFERENT actual cause (a place id handed to
        `/inspect`, which expects a thing id — a separate bug). This fix
        stands on its own regardless: `place_view` really was agent-blind,
        and a logged-in human really could not have seen their own
        private-place interior through this route before this change.
        `viewer` and the new session-resolved agent are DELIBERATELY
        DECOUPLED, even for a
        caller viewing their own place where the two happen to name the
        same avatar: conflating them is exactly the mistake that would
        reopen an unauthenticated-content-disclosure hole (a client setting
        `viewer=lei` must never see lei's private interior — only a caller
        who actually HOLDS lei's session cookie may).

        ONE exception (umwelt #522): `_enrich_things_with_typed_actions`
        below DOES forward `viewer` on to `GET /umwelt/thing_actions` as
        that route's own `agent` param, to compute a per-viewer `permitted`
        flag on each kind_id-bearing thing's typed actions. This is still
        consistent with "grants nothing" above — `thing_actions` is a pure
        read that flags permission, it never grants any; the actual
        capability channel (`POST /act`) is a separate, session-gated route
        that always resolves its acting agent from `session["avatar"]`,
        never from this route's `viewer` — so a caller who sets `viewer` to
        someone else's name can see (read-only) what that name's kind-chain
        permissions WOULD flag, but can never act as them. Worth stating
        explicitly because it is the one place this route's own
        `viewer`-is-inert claim above needed an asterisk, and a stale
        blanket claim next to the one place it stopped being fully true is
        exactly the failure mode #519 already taught this codebase to
        watch for.

        umwelt#982 amendment to that exception: when the caller HOLDS a
        session, `thing_actions` gets the session-resolved avatar (not the
        `viewer` label) plus the session's world token, and every sprite
        `look_at` gets the same pair -- the engine's thing read gate decides
        on who is actually asking. Only a session-less caller still reaches
        `thing_actions` with the bare `viewer` label (and no token), exactly
        as before; such a caller sees only public places' things anyway.

        Precision worth owning rather than glossing (reviewer-fairy, #524):
        "grants nothing" is correct but narrower than the full picture —
        before this PR `/scene` exposed nothing identity-scoped at all;
        after it, an unauthenticated caller CAN enumerate any named
        agent's per-thing authorization state (author/place-owner/grant/
        named-caller) by varying `viewer`. This PR introduces a NEW
        unauthenticated read that reveals per-viewer authorization state,
        on two surfaces at once (`/scene` here, and `/umwelt/thing_actions`
        directly): it is consistent with the world's declared private-network-open
        posture (Lei, 2026-08-08 — the open reads are by design) and adds
        no capability (engine auth is unchanged either way), but it is new
        exposure, not a restatement of exposure that already existed
        before this diff. (Ariadne's colleague pass, #524: the prior
        wording justified this by pointing at `/umwelt/thing_actions` as
        "already possible" — but that route is introduced by this SAME
        commit, so both halves were new at once and the argument proved
        nothing. Corrected here rather than left circular.)

        M2: after building the base scene, each returned thing (up to
        `_MAX_SPRITE_LOOKUPS`) is enriched with a real `sprite_ref` +
        validated `color`/`marker` via `_enrich_things_with_sprites` — see
        that function's docstring for the exact fallback behavior on a
        missing/malformed `render_spec`.
        """
        viewer = str(request.args.get("viewer", "")).strip()
        if not viewer:
            return jsonify({"error": "viewer is required"}), 400
        place = str(request.args.get("place", "")).strip()
        if not place:
            return jsonify({"error": "place is required"}), 400

        # umwelt#554: SERVER-resolved agent, from the session cookie —
        # NEVER from `viewer` above, which stays exactly the untrusted,
        # client-supplied display label this route's own docstring already
        # describes it as. No cookie / unknown cookie -> `session_agent`
        # stays None -> `place_view` gets no `agent` at all -> today's
        # exact anonymous-caller behavior, byte-for-byte unchanged. This is
        # a soft, best-effort resolution (unlike `/act`'s hard 401 gate) —
        # a spectator with no session must still get a scene, just the
        # public-only one they've always gotten.
        token = request.cookies.get(_SESSION_COOKIE_NAME)
        session = session_store.get(token) if token else None
        session_agent = session["avatar"] if session else None

        session_world_token = session.get("world_token") if session else None

        try:
            # umwelt#656: this route's own #554 fix, now expressed through
            # the shared helper so the other three call sites cannot drift
            # from it again. The `&agent=` behaviour below is byte-for-byte
            # what this route already did; forwarding the session's world
            # token alongside it is new here, and matches `/crossings`.
            wv = _place_view_as(
                world_url, place, session_agent, session_world_token)
        except WorldHTTPError as e:
            # Propagates the world server's own error body+status verbatim
            # — 404 (place not found) and 403 (private place) alike, same
            # posture as every other error this route already forwards.
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        out = _build_scene(wv)
        # umwelt#982: the session-resolved viewer + world token ride on every
        # look_at (the engine's thing read gate refuses an identity-less read
        # of a private-place thing, owner included). `session_agent` is
        # server-resolved, never the `viewer` label.
        _enrich_things_with_sprites(out["things"], world_url, sprite_store,
                                    session_agent, session_world_token)
        # umwelt #522: merges kind-declared typed actions (with a per-viewer
        # `permitted` flag) onto every kind_id-bearing thing's `actions`
        # list — a NO-OP extra-call-wise for the overwhelming majority of
        # things (plain untyped ones), see the function's own docstring.
        # umwelt#982: a real session's resolved agent replaces the `viewer`
        # label here (the engine gates thing_actions on this identity); a
        # session-less caller keeps the label, as before, with no token.
        _enrich_things_with_typed_actions(
            out["things"], world_url, session_agent or viewer,
            session_world_token)
        # 2026-07-24 world-finishing sprint: avatar serve-path — same
        # "called once per /scene invocation" pattern as
        # things above, its own function since avatars carry no
        # color/marker fields to enrich.
        _enrich_avatars_with_sprites(out["avatars"], world_url, sprite_store,
                                     session_agent, session_world_token)
        # 2026-07-24 world-finishing sprint: place-MARKER enrichment (the
        # door/exit buttons representing other places) — a distinct surface
        # from the lane-tiles enrichment just below (see module comment
        # above _apply_place_marker_sprite for the marker-vs-tile split).
        _enrich_place_markers_with_sprites(
            out["exits"], out["parent"], world_url, sprite_store,
            session_agent, session_world_token)
        # place-lane-tiles spec §3: place-level sprite enrichment, same
        # "called once per /scene route handler invocation" pattern as
        # _enrich_things_with_sprites above. `wv["place"]["metadata"]` is
        # populated server-side by server.py's `_place_view` (place-lane-
        # tiles spec §1) — no separate upstream call needed here.
        lane_tiles = _enrich_place_with_tiles(
            wv.get("place", {}).get("metadata") or {}, world_url, sprite_store)
        if lane_tiles:
            out["place"]["lane_tiles"] = lane_tiles
        return jsonify(out)

    @app.route("/crossings")
    def crossings() -> Response:
        """`GET /crossings?place=<id>&since=<seq>` — a place's crossings
        backlog/poll feed, proxying the world's read-only
        `POST /umwelt/crossings_here`.

        `place` is required (400 if missing). `since` is ALSO required and
        must parse as an integer (400 if missing or non-integer) —
        deliberately never defaulted: omitting `since` on the world's own
        `crossings_here` route makes IT fall back to the caller's own
        standpoint cursor, which doesn't exist/isn't meaningful for an
        anonymous gateway viewer. Callers pass `0` explicitly for
        "everything since the beginning".

        umwelt#621 slice 1c (gateway-forwarding half, Mira's ruling, security
        seat, 2026-08-22): a caller-supplied `viewer` query param used to be
        forwarded straight through as `crossings_here`'s `agent` field — no
        session check at all. Mira: "Its caller-supplied `viewer` can forge
        `viewer_relation`; today that is labeling/integrity rather than a
        public-place confidentiality bypass because the gateway
        independently gates the place through `place_view`, and world
        crossings remain visible regardless of relation. Still, the
        logged-in path should derive viewer from `session['avatar']` and
        forward `session.get('world_token')`, matching `/scene` and the
        other converted gateway routes. If anonymous public crossing
        viewing is intentionally retained, keep it as an explicit anonymous
        branch whose identity is not caller-selectable; do not accept an
        arbitrary `viewer` label as authority." So: a valid session cookie
        resolves `viewer` from `session["avatar"]` (server-side, unforgeable
        by this route) and forwards `session.get("world_token")` as a real
        bearer token to BOTH world calls below — the engine's own
        resolve_actor then makes the token win over any declared identity
        (anti-forgery), same property every other converted gateway route
        already has. No session (or an unknown/expired cookie): `viewer`
        falls to the FIXED, non-caller-selectable `_ANONYMOUS_GATEWAY_VIEWER`
        sentinel rather than whatever the client happened to send in a
        `viewer` query param — the anonymous branch Mira's ruling asks for
        (crossings_here 400s on a blank `agent` before resolve_actor even
        runs, so an empty string can't stand in for "no identity" here the
        way place_view's genuinely optional `agent` can). The client-supplied
        `viewer` query param, if any, is now IGNORED entirely; it was never
        a real identity and is no longer even a label.

        The world request always sends `advance: false`, hardcoded — never
        a caller-controlled option. `advance=true` would write a
        standpoint-cursor row keyed on the resolved viewer, the same
        write-on-read bug class M0's `/scene` fix closed for avatar
        position. The forwarded `agent`/`viewer` value is used only for
        `_viewer_relation`'s labeling (a pure string comparison, not a DB
        lookup or write when `advance=false`) on the anonymous/no-token
        fallback path — a valid token overrides it for real access-relevant
        resolution on the engine side, never for access control on this
        gateway route itself.

        Public-place gate (post-review fix — reviewer-fairy caught this
        route shipped with NO privacy gate at all, unlike `/scene`/`/inspect`):
        `crossings_here` itself only checks `place_exists`, never
        `place["kind"]`, so calling it directly on a private place returns
        that place's full crossing history to anyone. Worse than the
        `/inspect` asymmetry: private place ids follow the guessable
        `place_post_<agent>` convention, not unguessable UUIDs — so this was
        a real, exploitable leak, not just a consistency nicety. Gated the
        same way `/inspect` gates on a thing's place: call the same
        read-only `GET /umwelt/place_view?place=<id>` route FIRST, before
        ever touching `crossings_here` — if it 403s (private) or 404s
        (missing), propagate that and never call `crossings_here` at all.
        This gate call now ALSO forwards the resolved session identity/token
        (mirroring `/scene`'s umwelt#554 fix): a logged-in owner viewing
        their OWN private place's crossings feed no longer 403s/404s the way
        an anonymous caller correctly still does.

        Response: the world's own `crossings_here` shape propagated as-is —
        `{crossings, from_position, to_position, skipped}`.
        """
        place = str(request.args.get("place", "")).strip()
        if not place:
            return jsonify({"error": "place is required"}), 400
        since_raw = request.args.get("since")
        if since_raw is None or str(since_raw).strip() == "":
            return jsonify({"error": "since is required"}), 400
        try:
            since = int(since_raw)
        except ValueError:
            return jsonify({"error": "since must be an integer"}), 400

        # umwelt#554/#621: SERVER-resolved viewer, from the session cookie —
        # NEVER from a client-supplied `viewer` query param. Same soft,
        # best-effort resolution as `/scene`: no cookie / unknown cookie ->
        # session stays None -> viewer falls to the fixed anonymous
        # sentinel, never an error.
        session_token = request.cookies.get(_SESSION_COOKIE_NAME)
        session = session_store.get(session_token) if session_token else None
        viewer = session["avatar"] if session else _ANONYMOUS_GATEWAY_VIEWER
        world_token = session.get("world_token") if session else None

        try:
            # umwelt#656: routed through the shared helper. Behaviour is
            # unchanged for a token-bearing session (#621 slice 1c already
            # fixed the owner case here); what the helper adds is the
            # declared-agent half, so a session whose `world_token` is
            # ABSENT resolves the same way `/scene` does. NOT one whose
            # token is present but dead — that 401s at the engine
            # (fail-closed, umwelt#618); Mira's #712 read.
            _place_view_as(
                world_url, place,
                None if viewer == _ANONYMOUS_GATEWAY_VIEWER else viewer,
                world_token)
        except WorldHTTPError as e:
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        try:
            result = _world_request(
                world_url, "/umwelt/crossings_here",
                {"agent": viewer, "place": place, "since": since,
                 "advance": False},
                timeout=10.0, token=world_token)
        except WorldHTTPError as e:
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        return jsonify(result)

    @app.route("/inspect")
    def inspect() -> Response:
        """`GET /inspect?thing=<id>` — a single thing's detail, proxying the
        world's read-only `POST /umwelt/look_at`.

        `thing` is required (400 if missing). umwelt#982: the session is
        resolved FIRST and its viewer (as `agent`) AND world token are
        forwarded to the world's `look_at` -- the engine's thing read gate
        refuses an identity-less by-id read of a thing held in a private
        place, owner included, and answers a non-enterer with an
        informational 403 (`status: in_private_place` + the place's public
        projection, or `status: binned`), which this route propagates
        verbatim. No cookie / unknown cookie = anonymous (`None, None`).

        Doubles as the avatar-status route (umwelt #239 — "click avatar ->
        status panel"): `look_at`'s own `kind == "avatar"` branch
        (server.py's `_avatar_status`) already fires for ANY thing whose
        `kind` is avatar, and this route's privacy gate below (re-deriving
        `place_id` off the returned thing, re-checking `place_view`)
        applies identically regardless of kind — so no new route was
        needed, just the client sending the avatar's THING id (`/scene`'s
        `avatars[i].avatar_id`, umwelt #239) as `thing` here, not its
        `agent` string (see `_build_scene`'s docstring for why those two
        differ). This route stays a pure proxy either way — no avatar
        status logic is re-derived gateway-side.

        Public-place gate (design addendum, Borges/security): `look_at`
        itself doesn't check whether the thing's containing place is still
        public — thing ids are unguessable UUIDs reachable only via a
        public place's own `/scene` response, so this was never a real
        move_to-class exploit (more like a possession-capability model,
        similar to the world's CAS route). Still, to keep this gateway
        uniformly "public-content-only" (the same posture `/scene` already
        enforces) rather than a split "public places, but any thing by id"
        model, this route adds one extra round trip: after `look_at`
        returns, it reads `place_id` off the returned `thing` (a real
        column in `db._THING_COLS`, confirmed by reading `db.get_thing`)
        and calls the SAME read-only `GET /umwelt/place_view?place=<id>`
        route `/scene` already calls. If that 403s (private place) or 404s,
        this route propagates that error instead — never the look_at
        content already fetched. The gate is enforced HERE, at the gateway;
        the world server's `look_at` route itself never refuses on privacy.

        Response: the world's own `look_at` shape propagated as-is —
        `{thing, use_stats, status?, checklist_render?}`.
        """
        thing = str(request.args.get("thing", "")).strip()
        if not thing:
            return jsonify({"error": "thing is required"}), 400

        # umwelt#982: resolve the session BEFORE the first world call -- the
        # look_at below is itself gated on who is asking. Same soft,
        # best-effort resolution as `/scene` and `/crossings`.
        token = request.cookies.get(_SESSION_COOKIE_NAME)
        session = session_store.get(token) if token else None
        viewer = session["avatar"] if session else None
        world_token = session.get("world_token") if session else None

        try:
            result = _look_at_as(
                world_url, thing, viewer, world_token, timeout=10.0)
        except WorldHTTPError as e:
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        # Fail CLOSED, not open (post-review hardening): `place_id` is a
        # NOT NULL schema column on every real thing, so this should never
        # actually be missing — but if it ever were, the gate must refuse
        # rather than silently skip and return the already-fetched content.
        place_id = (result.get("thing") or {}).get("place_id")
        if not place_id:
            return jsonify(
                {"error": "thing has no place_id — refusing to serve"}), 500

        # umwelt#656: the session is resolved ABOVE the gate (umwelt#982 moved
        # it above the look_at too -- see the top of this route) rather than
        # below it where umwelt#655 left it. Same soft, best-effort
        # resolution as `/scene` and `/crossings`: no cookie or an unknown
        # cookie leaves both identity values None, which is the anonymous
        # read this route has always performed.
        try:
            _place_view_as(world_url, place_id, viewer, world_token)
        except WorldHTTPError as e:
            # Containing place is private (403) or gone (404) — don't
            # leak the look_at content already fetched above. Unchanged for
            # every viewer EXCEPT the place's own owner, who now passes:
            # umwelt#656, the /inspect sibling of #554 and #621 slice 1c.
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        # umwelt #655: additive typed-action enrichment (the proposal-viewer
        # door onto act()). `viewer` is the same session-resolved identity
        # the gate above used — resolved once, per umwelt#656.
        _enrich_inspect_with_typed_actions(
            result, world_url, viewer, world_token)

        return jsonify(result)

    @app.route("/look_in", methods=["POST"])
    def look_in() -> Response:
        """`POST /look_in` — a container's contents, proxying the world's
        `POST /umwelt/look_in`. Body `{"container": "<id>"}`.

        The acting agent is ALWAYS `session["avatar"]` (resolved server-side
        from the `umwelt_session` cookie), NEVER from the request. The world's
        `look_in` enforces the closed-bag exposure rule (`_reachable`:
        "you must hold the container or be in its place") against the agent
        it receives, and it trusts a caller-declared `agent` (trust-scoped-v0,
        same posture as take/drop/inventory — server.py:7108). So forwarding a
        client-supplied agent here would let a viewer declare any agent and
        look_in containers only that agent can reach. The identity-injection
        guard below mirrors `/act`'s: a body carrying a key literally named
        `agent`/`actor` is rejected with 400, never silently ignored. This is
        the codebase's identity-from-session invariant (webrender/session.py).

        A pure proxy — this route does NOT re-derive visibility or add a
        gateway-side place gate. The world's `_reachable` is the authority
        (contrast `/inspect`, which adds its own public-place gate only
        because `look_at` takes NO agent; `look_in` takes the agent and the
        world enforces, so no gateway gate).

        Response: the world's own `look_in` shape propagated as-is —
        `{things:[{id,name,description,weight_est[,status][,closed]}], load,
        capacity}` (`status` = a typed thing's own kind-state, present only
        for typed things -- umwelt #967; `closed: true` = that state is
        terminal for the kind, derived by the world from the kinddef --
        umwelt #976).
        """
        session, error = _session_and_csrf_gate(session_store)
        if error:
            return error

        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            body = {}

        # Case-insensitive identity-injection guard — mirrors `/act`'s
        # (the engine trusts the caller-declared `agent`, so forwarding one
        # would let a viewer look_in as any agent). "Agent"/"AGENT" is caught
        # the same as "agent", same as `/act`'s guard.
        if {k.lower() for k in body} & {"agent", "actor"}:
            print("[look_in] anomalous client-asserted identity field — "
                  f"rejecting request from avatar {session['avatar']!r}")
            return jsonify({
                "error": "agent/actor may not be specified in the request "
                         "— identity comes from your session",
            }), 400

        container_id = body.get("container")
        if not isinstance(container_id, str) or not container_id.strip():
            return jsonify({"error": "container is required"}), 400

        try:
            result = _world_request(
                world_url, "/umwelt/look_in",
                {"agent": session["avatar"], "container": container_id.strip()},
                timeout=10.0, token=session.get("world_token"))
        except WorldHTTPError as e:
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        return jsonify(result)

    @app.route("/sprite/<key>")
    def sprite(key: str) -> Response:
        """`GET /sprite/<key>` — M2. Pure serving of a cached sprite PNG by
        its content-hash key (see webrender/sprites.py's `SpriteStore`).
        Never regenerates on a bare fetch — sprites are only ever produced
        via the `/scene` pipeline's `get_or_render_sprite` call. 404 if the
        key isn't in the store.

        M4 slice 1 (umwelt#319) fail-safe: `spec_key` is a deterministic
        hash of enumerable closed-taxonomy content (class/palette/
        descriptors), not a secret — so this route is reachable directly,
        bypassing `get_or_render_sprite`/`/scene` entirely, by anyone who
        can compute the same hash for a render_spec they already know. A
        key whose manifest entry is NOT live (pending, not yet
        peer-approved) is refused here — same 404 shape as a genuinely
        absent key, so this route never distinguishes "doesn't exist" from
        "exists but ungated" to an unauthenticated caller — logged
        separately server-side so the two ARE distinguishable in ops."""
        if sprite_store.status_index.status(key) is False:
            print(f"[webrender] refused non-live sprite fetch for key {key!r}")
            return jsonify({"error": f"sprite {key!r} not found"}), 404
        data = sprite_store.get(key)
        if data is None:
            return jsonify({"error": f"sprite {key!r} not found"}), 404
        return Response(data, mimetype="image/png")

    # --- M3: session + the write chokepoint ---------------------------------

    @app.route("/session/login", methods=["POST"])
    def session_login() -> Response:
        """`POST /session/login` — body `{"username": "lei", "password":
        "..."}`. `password` may be EMPTY or omitted: the world's own
        three-state rule (Kepler's PR #559 ruling) decides what that means
        (see below) — this route never branches on bound-vs-unbound
        itself, it just forwards.

        **umwelt#495: this route is now a PROXY, not an authority.**
        Before tonight, this gateway held its own idea of who exists (a
        hardcoded `ROSTER` frozenset) and rejected anyone else before the
        world was ever asked — Lei's own framing: "the web gateway should
        be a bridge to the underlying world engine," not a second
        identity store. Now: forward `{"agent_id": username, "password":
        password}` to the world's own `POST /umwelt/login`
        (server.py — real scrypt/ed25519 verification, umwelt#495) and
        mint a gateway session ONLY on the world's 200 success. On
        failure, the world's response (status + body) is passed through
        UNCHANGED — see below for why that is load-bearing, not
        incidental.

        On success, sets an httpOnly `umwelt_session` cookie holding the
        GATEWAY's own opaque session token (never readable from JS — this
        is a bearer credential) and returns `{"identity": username,
        "csrf_token": <the session's csrf token>, "state": <"verified" or
        "unverified", copied verbatim from the world>, "place": <resolved
        place id>}`. The csrf token IS returned in the JSON body (unlike
        the session token) because the client needs to read it and resend
        it as the `X-Csrf-Token` header on every `/act` call; that's the
        whole point of a double-submit-shaped CSRF defense.

        `state` (umwelt#495, new tonight) MUST be surfaced to the client
        and rendered VISIBLY, not silently accepted — an "unverified"
        session (unbound identity, backfill window open) is the design
        working as intended, not a security hole to hide, and the design's
        own anti-silent-fail-open requirement says this state may never be
        inferable only from a field a caller might not bother to check.
        The world's own login route carries the identical discipline; this
        route inherits it rather than re-deciding it.

        **Error passthrough is deliberate, not a shortcut.** The world's
        `/umwelt/login` returns the SAME `401 {"error": "authentication
        failed"}` for an unknown username AND a wrong password (Zephyr's
        cross-vendor finding, forum #315 — a per-case message would let a
        caller enumerate the roster by probing). Rewriting or
        "friendlier"-ing that message here would silently undo a security
        finding the engine layer already paid for; a 429 (login rate
        limit, umwelt#561) is passed through the same way, for the same
        reason — inventing a gateway-side message for either would be a
        second, independent oracle the world's own route was specifically
        built to not have.

        `place` (umwelt #238): the avatar's CURRENT place, resolved
        server-side via `_resolve_avatar_place` (falls back to
        `place_square` if the avatar has no standpoint yet, or on a
        transient world-server error) — so the client can navigate straight
        there without the human ever typing a place id.

        `SameSite="Strict"` — this gateway is same-origin private-network use,
        nothing here needs cross-site cookie delivery.

        `secure=request.is_secure` (umwelt#688, corrected by Mira's
        security-lane read on #699's dual-listener PR) — derived from
        THIS REQUEST's actual transport, never from a process-wide flag.
        `app.config["TLS_ENABLED"]` (whether `main()` resolved a TLS
        context at startup at all) was the right source before the
        dual-listener design: a process was either all-TLS or
        all-plaintext, so "this process's mode" and "this request's
        transport" were the same fact. They no longer are — a single
        dual-listener process now serves BOTH transports at once, each
        via its own werkzeug listener (see `tls_context.serve_dual_listener`).
        `request.is_secure` reads Werkzeug's per-connection
        `wsgi.url_scheme`, which each listener's own request handler sets
        from *that listener's own* `ssl_context` (`http` for the
        plaintext listener, `https` for the TLS one) — so it reflects
        the actual socket a given login request arrived on, not
        whichever transport the process happens to also be serving
        elsewhere. A login through the plaintext listener still gets
        `secure=False` (unchanged posture — Secure over HTTP would make
        the browser silently refuse to send the cookie back, breaking
        every plaintext login); a login through the TLS listener gets
        `secure=True`. No reverse proxy sits in front of either listener
        in this deployment (see `tls_context.py`'s own one-process
        rationale) — a future proxy-fronted deployment would need to
        layer in `X-Forwarded-Proto` trust, which this does not attempt.

        **Scope of the coexistence guarantee (Mira's second security-lane
        pass on #699's gateway PR, narrowed here per her own suggested
        resolution)** — this fix guarantees each transport is correct
        *independently*: a browser that logs in over plaintext and stays
        on plaintext keeps working; one that logs in over TLS and stays
        on TLS keeps working. It does NOT guarantee a single browser can
        hold two simultaneously-independent sessions across BOTH
        transports at once. Cookies are host+path scoped, not port
        scoped — `umwelt_session` is one name, one storage slot per
        browser, so a later login on one transport overwrites whatever
        the browser is holding from the other. Concretely: log in over
        HTTP, then later log in over HTTPS on the same browser, and the
        HTTPS `Set-Cookie` replaces the HTTP session's cookie value; the
        browser then withholds that (now Secure) cookie from subsequent
        plain-HTTP requests, so the earlier HTTP session is stranded —
        not leaked, not crashed, just no longer reachable without
        re-login. That is the accepted migration-window shape: a client
        picks a transport and stays on it; switching transports means
        logging in again. See
        `test_switching_transport_mid_browser_strands_the_earlier_http_session`
        for the pinned behavior and why Flask's own test-client cookie
        jar can't be trusted to demonstrate it (it does not enforce the
        browser's Secure-cookie withholding rule at all).

        **One-way only (Mira's approval note, same PR)** — "switching
        transports means re-login" describes the intended migration
        DIRECTION, HTTP → HTTPS, not a symmetric claim. Don't promise the
        reverse (an HTTPS session downgrading back to a working HTTP
        session on re-login) works the same way: modern browsers'
        Secure-cookie-overlay protections (RFC 6265bis) may refuse to
        let a non-Secure `Set-Cookie` overwrite an existing Secure one
        of the same name at all, so a login attempt over plain HTTP
        after an HTTPS session is already established could silently
        fail to actually replace the cookie in some browsers. The
        product should not encourage that downgrade path regardless —
        the whole point of the migration window is moving traffic
        toward TLS, not back.

        **Humans only — `kind='human'` gate (Ariadne's correction,
        2026-08-16, filed minutes after the original spec).** The world's
        unbound+window-open branch mints a 200 for ANY unbound identity
        with an empty password, agent or human alike — that is exactly
        right for the CLI/keypair world, and exactly wrong here: it would
        let any admitted private-network caller type e.g. `borges`, leave the password
        blank, and get a browser session acting as another AGENT. Agents
        authenticate by keypair, never a password (an agent's context
        window isn't private, so recalling a password would leak it) —
        the bind client already refuses a scrypt verifier for an agent
        identity; this gate is what stops the gateway from undoing that
        via the one login shape that doesn't need a password at all.
        Checked AFTER the world's own 200 (never before) — a failing
        login is already uniform-401 and reveals nothing; only a
        successful one reaches this check, and by the time it does, the
        200 itself has already revealed existence+bound-state, so
        reading `kind` off that same response adds no new oracle. A
        non-human `kind` on an otherwise-successful world login is
        discarded here and reported with the SAME uniform 401 as a bad
        password/unknown identity — never a kind-specific message (that
        would leak exactly what the uniform-401 posture exists to hide:
        which usernames are agents).
        """
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            body = {}
        username = body.get("username")
        if not isinstance(username, str) or not username.strip():
            return jsonify({"error": "username is required"}), 400
        username = username.strip()
        password = body.get("password")
        if not isinstance(password, str):
            password = ""

        try:
            world_resp = _world_request(
                world_url, "/umwelt/login",
                {"agent_id": username, "password": password}, timeout=10.0)
        except WorldHTTPError as e:
            # Passed through unchanged -- see docstring's "Error
            # passthrough is deliberate" section.
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        if world_resp.get("kind") != "human":
            # See docstring's "Humans only" section -- uniform 401,
            # never a kind-specific message, no session minted. This
            # covers BOTH a bound agent identity (world already returned
            # 401 above, never reaches here) and, the actual hole this
            # closes, an UNBOUND agent identity riding the
            # unbound+window-open 200 with a blank password.
            #
            # Reviewer-fairy finding on PR #605: this branch returns
            # immediately, while the kind-matched success path below does
            # session_store.create + a full _resolve_avatar_place round-
            # trip first -- both return the byte-identical 401 body, but
            # response TIMING differs measurably, which could let a private-network
            # caller distinguish "unbound human" from "unbound agent" by
            # latency even though the message can't. Same class of leak
            # as the already-tracked umwelt#596 (scrypt-vs-ed25519 login
            # timing) -- noted here, not fixed here: low severity because
            # it distinguishes two already-public roster/type classes rather
            # than disclosing a credential or granting authority, and the
            # curl-door gap is already acknowledged (umwelt#604); a real fix (constant-time
            # padding) is disproportionate to add under tonight's scope.
            #
            # The world already minted a real session/token for `username`
            # before we get here (Ariadne's review, umwelt#495) -- this
            # discard never touched a credential failure, so the log line
            # says exactly that rather than reading like a failed auth
            # attempt a future reader would go hunting for in the world's
            # own logs.
            print(f"[webrender] gateway rejected a valid world session for "
                  f"{username!r} on kind ({world_resp.get('kind')!r} != "
                  f"'human') -- browser login is human-only")
            return jsonify({"error": "authentication failed"}), 401

        session = session_store.create(
            username, world_token=world_resp["token"],
            state=world_resp["state"], expires_at=world_resp["expires_at"])

        resolved_place = _resolve_avatar_place(
            world_url, username, token=world_resp["token"])

        resp = jsonify({
            "identity": username, "csrf_token": session["csrf_token"],
            "state": session["state"], "place": resolved_place,
        })
        resp.set_cookie(
            _SESSION_COOKIE_NAME, session["token"],
            httponly=True, samesite="Strict",
            # umwelt#688, corrected for the dual-listener design (Mira's
            # security-lane finding on #699): derived from THIS
            # REQUEST's actual transport, not the process-wide
            # TLS_ENABLED flag -- see this route's own docstring.
            secure=request.is_secure,
            # Defense in depth (Mira's #605 security review) -- caps how
            # long the BROWSER keeps sending this cookie at no later than
            # the world credential's own expiry. Server-side enforcement
            # in SessionStore.get() is authoritative regardless of this
            # value; see seconds_until's own docstring.
            max_age=seconds_until(session["expires_at"]),
        )
        return resp

    @app.route("/session")
    def session_status() -> Response:
        """`GET /session` — the current session's identity, or
        `"spectator"` if there's no cookie, or the cookie doesn't resolve to
        a known session. Always 200 — this is a status check, not an auth
        gate, so it never 401s.

        A logged-in response also carries `csrf_token` (the session's own,
        already-known token) — the frontend's login response is the only
        other place this is returned, and a page reload drops any
        JS-held copy of it (the session cookie itself survives the reload;
        the csrf_token variable in a JS module's memory does not). Without
        this, a refreshed-but-still-logged-in page would show the right
        identity yet be unable to call `/act` until an explicit re-login —
        a real usability gap, not a security concern: the caller already
        holds the session cookie, so handing back the SAME session's own
        token creates no new capability.

        SAFETY INVARIANT (Aleph's #234 off-host note — pin it, don't let it
        silently rot): this is safe ONLY as long as (a) the session cookie
        stays `SameSite="Strict"` (see `/session/login`'s `set_cookie` call)
        and (b) this gateway sets no permissive CORS (`Access-Control-*` /
        `after_request`). Before this route, the csrf_token was disclosed
        only at the login POST; now a session-gated GET returns it too —
        fine only while both invariants hold. Flip either one (e.g. serving
        the frontend from a separate origin later needs CORS) and this
        route becomes a CSRF-token disclosure vector to a cross-site
        caller. If either invariant ever changes, re-examine this route
        FIRST.

        `place` (umwelt #238): a logged-in response also resolves+returns
        the avatar's CURRENT place (same `_resolve_avatar_place` helper
        `/session/login` uses, same `place_square` fallback) — this is the
        page-reload-recovery case: a reload recovers identity+csrf via this
        route already, but without a resolved place it has nowhere to
        render, defeating the "never type a place id" bar on every reload.
        The spectator branch (no session) is unchanged — no `place` key,
        same as before.

        `state` (umwelt#495, new tonight): a logged-in response also
        carries the session's `"verified"`/`"unverified"` state, copied
        from the same field `/session/login` returns — same page-reload-
        recovery reasoning as `place` above: without this, a reload would
        silently drop the visible unverified-session indicator the login
        route's own docstring says must never be inferable-only."""
        token = request.cookies.get(_SESSION_COOKIE_NAME)
        session = session_store.get(token) if token else None
        if session is None:
            return jsonify({"identity": "spectator"})
        resolved_place = _resolve_avatar_place(
            world_url, session["avatar"], token=session.get("world_token"))
        return jsonify({
            "identity": session["avatar"],
            "csrf_token": session["csrf_token"],
            "state": session["state"],
            "place": resolved_place,
        })

    @app.route("/session/logout", methods=["POST"])
    def session_logout() -> Response:
        """`POST /session/logout` — ends the current session (idempotent —
        no-op if there wasn't one) and always clears the cookie."""
        token = request.cookies.get(_SESSION_COOKIE_NAME)
        if token:
            session_store.destroy(token)
        resp = jsonify({"identity": "spectator"})
        resp.delete_cookie(_SESSION_COOKIE_NAME)
        return resp

    @app.route("/inventory")
    def inventory() -> Response:
        """`GET /inventory` — what YOUR avatar is currently carrying,
        proxying the world's `POST /umwelt/inventory`.

        Unlike `/inspect` (public content, no session required), inventory
        is per-agent private info — this route is SESSION-gated: missing/
        invalid `umwelt_session` cookie -> 401 `{"error": "no active
        session -- log in first"}` (same check `/act` applies, copied
        verbatim). No CSRF check — this is a GET with no mutation, same
        posture as `GET /session`. `agent` is always resolved server-side
        from `session["avatar"]`, never a query param — same
        gateway-resolves-identity discipline as `/act`.

        Response: the world's own `/umwelt/inventory` shape passed through
        as-is — `{things, load, capacity, headroom}` — not routed through
        `/act`'s `{"outcome": ...}` wrapping convention, since this isn't a
        write.
        """
        token = request.cookies.get(_SESSION_COOKIE_NAME)
        session = session_store.get(token) if token else None
        if session is None:
            return jsonify(
                {"error": "no active session -- log in first"}), 401

        try:
            result = _world_request(
                world_url, "/umwelt/inventory",
                {"agent": session["avatar"]}, timeout=10.0,
                token=session.get("world_token"))
        except WorldHTTPError as e:
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        return jsonify(result)

    @app.route("/map")
    def map_route() -> Response:
        """`GET /map` — the interactive place-tree map, proxying the
        world's `POST /umwelt/map`.

        Session-gated exactly like `/inventory` above (no CSRF — a
        read). `agent` is always resolved server-side from
        `session["avatar"]`. v0 sends no extra filters (name/kind/owner/
        unread_only/depth all omitted) — the world's own defaults apply
        (the full, unpruned forest).

        Response: the world's own `/umwelt/map` shape passed through as-is
        — `{roots: [<place_tree node>, ...]}`.
        """
        token = request.cookies.get(_SESSION_COOKIE_NAME)
        session = session_store.get(token) if token else None
        if session is None:
            return jsonify(
                {"error": "no active session -- log in first"}), 401

        try:
            result = _world_request(
                world_url, "/umwelt/map",
                {"agent": session["avatar"]}, timeout=10.0,
                token=session.get("world_token"))
        except WorldHTTPError as e:
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        return jsonify(result)

    @app.route("/inbox")
    def inbox() -> Response:
        """`GET /inbox?since=<seq>` — crossings addressed to YOUR avatar,
        across every place, proxying the world's `POST
        /umwelt/crossings_for_agent` and then splitting the result via
        `partition_crossings_for_inbox`. umwelt#539 (human inbox) — the
        proactive half; umwelt#529 (the human bell) is the reactive half,
        polling this same route with a caller-tracked cursor (see
        `to_position` below) — building one does not give the other, but
        both now share one contract.

        Session-gated exactly like `/inventory`/`/map` above (no CSRF — a
        read). `agent` is always resolved server-side from
        `session["avatar"]`, sent as BOTH `viewer` and `agent` to the world
        route — the world enforces `viewer == agent` today (Ariadne's
        umwelt#539/#512 ruling); the gateway can never send anything else
        since it only ever knows the logged-in session's own avatar.

        `since` (umwelt#529, optional — unlike `/crossings`'s REQUIRED
        `since`): omitted or blank means "everything", matching
        `crossings_for_agent`'s own `since_seq=None` contract directly —
        there is no ambiguous fallback to resolve here the way `/crossings`
        worried about (that route falls back to a *standpoint* cursor when
        `since` is omitted, which doesn't exist for an anonymous gateway
        viewer; `/inbox` has no such fallback to accidentally trigger, so
        "omitted" safely means what it says). A non-blank value MUST parse
        as an integer — 400 otherwise, same validation shape as
        `/crossings`.

        `partition_crossings_for_inbox` is imported directly from
        `umwelt.server` — a deliberate, narrow exception to this module's
        usual "talk to the world only over its HTTP API" boundary (see the
        module docstring). Justified because the function is pure (no
        `conn`, no I/O — verified by import) and its own docstring
        anticipates exactly this caller ("an eventual world-side route or a
        gateway-side aggregator without changing shape"); the boundary
        exists to stop the gateway becoming a second source of TRUTH about
        world STATE, and this imports interpretation logic, not state.

        Response: `{to_agent: [<crossing>, ...], mentions: [<crossing>,
        ...], to_position}` — the two disjoint buckets
        `partition_crossings_for_inbox` already defines (newest-last,
        ascending seq, matching every other crossings feed's order — the
        frontend renders newest-first by reversing, same as it already must
        for `/crossings`), plus `to_position` forwarded verbatim from the
        world route: the newest seq actually returned this call (or the
        caller's own `since` echoed back when nothing new qualified, or
        `null` on a caller's true first-ever call with no `since` and no
        crossings at all). umwelt#529's bell polls with
        `since=<last to_position>` and persists the new `to_position` each
        cycle — the same observed-window pattern `/crossings`'s `advance`
        establishes, but caller-tracked (localStorage) rather than a
        server-side standpoint row, since there is no per-human standpoint
        mechanism (see the `since` note above).

        No server-side unread COUNT — the structural note on umwelt#539
        still holds for that: there is no session-scoped read-cursor
        mechanism for a human today (the per-(agent, place) standpoint
        cursor system doesn't fit an addressee-scoped, cross-place read).
        `to_position` gives a caller everything it needs to build its OWN
        cursor client-side (umwelt#529 does exactly this); a server-computed
        unread count is a different, still-open feature, not required for
        the bell.
        """
        token = request.cookies.get(_SESSION_COOKIE_NAME)
        session = session_store.get(token) if token else None
        if session is None:
            return jsonify(
                {"error": "no active session -- log in first"}), 401

        avatar = session["avatar"]
        since_raw = request.args.get("since")
        world_body = {"viewer": avatar, "agent": avatar}
        if since_raw is not None and str(since_raw).strip() != "":
            try:
                world_body["since"] = int(since_raw)
            except ValueError:
                return jsonify({"error": "since must be an integer"}), 400

        try:
            result = _world_request(
                world_url, "/umwelt/crossings_for_agent",
                world_body, timeout=10.0,
                token=session.get("world_token"))
        except WorldHTTPError as e:
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        return jsonify({
            **partition_crossings_for_inbox(result["crossings"], avatar),
            "to_position": result.get("to_position"),
        })

    @app.route("/library")
    def library_list() -> Response:
        """`GET /library?status=&sort=` — the art-library browse/approval-
        queue surface (M4 art-generation pipeline, slice 3), a thin caller
        into `manifest.list_entries` (slice 2, the sole write/read owner of
        `master_manifest.json` — this route never touches the file itself).

        NOT session-gated like `/inventory`/`/map` above — this isn't a
        player-avatar surface, it's an agent-to-agent moderation surface
        (a peer approves another agent's generated sprite), same
        caller-declared-identity posture as the world engine's own routes
        (`server.py`'s `create_thing`/`take`/etc — `agent` in the body,
        folded through `canonical_agent`). The actual governing security
        criterion (reviewer-fairy catch, PR #316) is
        `docs/web-render/RENDER-PIPELINE-AND-SECURITY.md` **B.7** — the
        gateway stays within the declared LAN+tailnet private-network boundary,
        with HTTPS required before remote browser use (#583 remains open) and
        application identity kept separate from network admission —
        not an inferred precedent; B.3's session-identity-inheritance rule
        doesn't apply here because this route never proxies a world-server
        action tied to a session avatar, it only mutates the library's own
        local manifest state. `POST /library/<key>/approve` below is the
        write counterpart; this route is read-only, no identity/CSRF
        machinery needed for a read.

        `status` (optional query param): `"pending"` | `"live"` |
        `"grandfathered"` — omit for every entry regardless of status.
        `status="pending"` IS the approval queue (oldest-first by default —
        see `sort` below), the Library's primary job per the merged
        design-of-record §5. `status="live"` also matches `"grandfathered"`
        entries (umwelt#289/reviewer-fairy catch on PR #416:
        `manifest.list_entries` routes this one value through `is_live()`
        rather than an exact-string match, since every real caller of
        `status="live"` — including this route's own avatar-picker
        consumer, `static/app.js` — means "what's actually servable", the
        same semantic `is_live()` already encodes; querying the exact
        string `status="grandfathered"` still isolates just the migrated
        legacy rows for audit).

        `sort` (optional query param, default `created_at`): forwarded
        verbatim to `manifest.list_entries`'s own `sort` param — see its
        docstring for the missing-key/ascending-order semantics.

        Response: `{"entries": [...]}`, each entry `manifest.py`'s own row
        shape (`spec_key`, `render_spec`, `status`, `creator`,
        `approved_by`, `created_at`, `label`, `category`, plus
        `asset_path`/`canonical_json` on legacy pre-slice-2 entries).
        """
        status = request.args.get("status") or None
        sort = request.args.get("sort") or "created_at"
        sprite_dir = app.config["SPRITE_STORE"].dir
        entries = manifest.list_entries(sprite_dir, status=status, sort=sort)
        return jsonify({"entries": entries})

    @app.route("/library/generate", methods=["POST"])
    def library_generate() -> Response:
        """`POST /library/generate` — body `{"render_spec": {...}, "agent":
        "<creator>"}`. M4 art-gen slice 1 (umwelt#319 layer (a))
        — the author-path entry point: requests a NEW generation from the
        standalone art-gen service and quarantines the result as a
        `pending` library entry, never touching the servable sprite cache
        `get_or_render_sprite`/`GET /sprite/<key>` actually serve from. Only
        `POST /library/<key>/approve` (below) — a DIFFERENT agent's peer
        review — ever promotes it into that servable store.

        Same caller-declared-attribution / admitted-private-network posture as
        `/library` (RENDER-PIPELINE-AND-SECURITY.md B.7) — network admission
        is not identity, and there is no session/CSRF
        machinery, `agent` is folded through the house `canonical_agent`
        fold same as every other identity-bearing route. `POST
        /library/<key>/approve` (below) is DIFFERENT as of umwelt forum
        #289 — it is session+CSRF gated, human-only, and never reads an
        identity from the request body; see its own docstring.

        - `art_service_url` not configured (`create_app`'s new keyword,
          `None` by default) -> 503, never a crash. Per Kepler's
          enhancement-never-dependency rule, an undeployed/dead art-gen
          service must never take the rest of this gateway down with it.
        - `render_spec`/`agent` missing or malformed -> 400.
        - A manifest entry (ANY status) already exists for this
          `render_spec`'s `spec_key` -> returns that entry as-is, 200, and
          never calls the (slow, quota-consuming) service again — mirrors
          `manifest.add_pending`'s own idempotency, but one layer earlier so
          a duplicate request doesn't burn a GPU generation just to be
          thrown away by that idempotency check afterward.
        - Generation fails (`GenerationUnavailable` — service down,
          rejected spec, quota exceeded, VRAM unavailable) -> 502
          `{"error": ...}`, never a crash; the renderer already falls back
          to procedural on the READ side regardless of this route's outcome.
        - Success -> the PNG bytes are written ONLY to the pending-staging
          store (`app.config["PENDING_STORE"]`, `<sprite_dir>/.pending/`),
          `manifest.add_pending` records the entry, response is
          `201 {"entry": <the pending manifest row>}`.
        """
        art_service_url = app.config["ART_SERVICE_URL"]
        if not art_service_url:
            return jsonify(
                {"error": "art-gen service not configured on this gateway"}
            ), 503

        body = request.get_json(force=True, silent=True) or {}
        render_spec = body.get("render_spec")
        if not isinstance(render_spec, dict):
            return jsonify({"error": "render_spec (object) is required"}), 400
        agent_raw = body.get("agent")
        if not isinstance(agent_raw, str) or not agent_raw.strip():
            return jsonify({"error": "agent is required"}), 400
        agent = canonical_agent(agent_raw.strip())

        sprite_dir = app.config["SPRITE_STORE"].dir
        key = spec_key(render_spec)

        # Idempotency-before-generation: an existing entry in ANY status
        # (pending OR live) means someone already requested/curated this
        # exact spec — never regenerate, never waste a GPU call + quota unit
        # on a spec that already has a manifest row.
        for entry in manifest.list_entries(sprite_dir):
            if entry.get("spec_key") == key:
                return jsonify({"entry": entry}), 200

        renderer = ImageModelRenderer(art_service_url, agent)
        try:
            # umwelt#476: shared with cap_handler.py's _run_generate_job via
            # sprites.derive_seed, not inlined here -- see its own docstring
            # for the umwelt#339 overflow history this formula fixes.
            seed = derive_seed(key)
            raw = renderer.generate(render_spec, seed)
        except GenerationUnavailable as e:
            return jsonify({"error": str(e)}), 502

        pending_store = app.config["PENDING_STORE"]
        pending_store.put(key, raw)
        # umwelt#382: intentionally NOT passing `category` here. This route
        # accepts an arbitrary agent-declared `render_spec` with no `kind`
        # or other type discriminator in the request body -- unlike
        # the internal generics-population script's fixed, known-shape generics (not in this release), there
        # is no reliable signal here for whether a given request is for an
        # avatar/thing/place/environment sprite (footprint/anchor presence
        # is a curation CONVENTION the offline assembler applies, not
        # something this route validates or can trust an agent to have
        # followed). Guessing a category from unreliable data would risk
        # re-creating the exact silent-mismatch bug this issue is closing,
        # so this stays `None` (uncategorized) until there's a real signal
        # to derive it from -- flagged as a genuine ambiguity, not a gap.
        entry = manifest.add_pending(sprite_dir, key, render_spec, agent)
        return jsonify({"entry": entry}), 201

    @app.route("/library/<key>/approve", methods=["POST"])
    def library_approve(key: str) -> Response:
        """`POST /library/<key>/approve` — session+CSRF gated, no body
        fields read for identity.

        umwelt forum #289: this route used to take `{"approver": "<agent>"}`
        straight from the client-supplied JSON body — an unauthenticated
        security hole, since any caller could self-approve their own
        submission by simply lying about the approver name (the only
        pre-existing protection, `manifest.approve`'s `approver ==
        entry["creator"]` check, is trivially defeated by declaring a
        different fake name). As of this change, the approver identity is
        ALWAYS resolved server-side from the caller's own session — never
        client-declared — via `_session_and_csrf_gate` (the same
        session-cookie-lookup + CSRF-header-match pair `/act` and the
        `/voice`-family routes already gate on; see that helper's
        docstring). Gate order:

        1. Auth — read the `umwelt_session` cookie; missing/invalid session
           -> 401.
        2. CSRF — the `X-Csrf-Token` header must exactly match the
           session's own `csrf_token`; missing/mismatched -> 403.
        3. Identity-injection guard — if the request body still carries an
           `"approver"` key at ALL (present, any value, including one that
           happens to match the session's own avatar), that's a hard-
           invariant violation attempt: reject with 400. Mirrors `/act`'s
           own `agent`/`actor` body-field rejection (`test_act_rejects_
           client_supplied_agent_field` / `..._actor_field`) — a caller
           must never be able to even TRY to assert an identity the server
           already resolves itself, not just have that field silently
           ignored (silent-ignore could read as tacit acceptance in some
           future refactor).

        The resolved approver is `canonical_agent(session["avatar"])` —
        the SAME `canonical_agent` fold every identity-bearing route in
        this codebase uses (umwelt #73). This fold is load-bearing, not
        defensive redundancy: `session["avatar"]` is the client-submitted
        `username` string from `/session/login`, `.strip()`-ped only —
        `/umwelt/login`'s success bodies never echo back a canonicalized
        `agent_id` (checked against server.py's login() route), so a
        mixed-case username (e.g. "Lei") reaches this point unfolded.
        The `canonical_agent()` call here is what actually normalizes it.

        **v0 identity scope, deliberately narrow (Kepler's ruling,
        endorsed by Borges and Lei):** only a real logged-in human can
        approve/register anything. umwelt#495 (2026-08-16) replaced the
        old hardcoded `session.py` `ROSTER` gate — session tokens are no
        longer restricted to a fixed three-name list; `SessionStore.create`
        no longer validates identity at all, trusting whatever the world's
        own `/umwelt/login` already authenticated (see that module's
        current docstring). The human-only boundary this paragraph is
        actually about is now enforced one layer up, at `/session/login`
        itself: the gateway discards any successful world login whose
        `kind != "human"`, so an agent identity — bound or unbound — can
        never reach a session here regardless of how many/which humans are
        in the roster. There is still no v0 mechanism for one agent to
        confirm another agent's work; that gap is real and intentionally
        deferred, tracked as umwelt#45 (engine-attested agent identity —
        not umwelt#24, a distinct executable-authorship trust-boundary
        issue this docstring previously cited by mistake) — not silently
        absent.

        Thin caller into `manifest.approve` (slice 2) — the security-
        bearing self-approval check lives THERE, not here; this route only
        maps its two named exceptions onto HTTP status codes, per Borges's
        slice-2 handoff:

        - Unknown `key` -> `manifest.ManifestEntryNotFound` -> 404
          `{"error": "no library entry for <key>"}`.
        - `approver == creator` -> `manifest.ApprovalRefused` -> 403
          `{"error": str(exc)}` (the exception's own message already names
          both identities — see `manifest.py`).
        - Already-live entry -> `manifest.approve`'s own no-op-returns-as-is
          behavior; this route does NOT special-case that, it just returns
          whatever `approve` returns as a 200 either way (idempotent
          double-approve is safe, per slice-2's own docstring).

        Response: `200 {"entry": <the manifest row post-call>}`.
        """
        session, error = _session_and_csrf_gate(session_store)
        if error:
            return error

        body = request.get_json(force=True, silent=True) or {}
        if not isinstance(body, dict):
            body = {}
        # Case-insensitive key check -- a client sending "Approver"/
        # "APPROVER" must be caught exactly the same as "approver" (same
        # gap class as /act's own guard, hardened after a reviewer-fairy
        # finding on PR #225: an exact-case check lets a differently-cased
        # key sail through silently, contradicting this guard's whole
        # purpose -- reviewer-fairy caught the identical gap here).
        if any(k.lower() == "approver" for k in body):
            return jsonify({
                "error": "approver is resolved from your session, not "
                         "the request body",
            }), 400
        approver = canonical_agent(session["avatar"])

        sprite_dir = app.config["SPRITE_STORE"].dir

        # umwelt#319 layer (a), the other half of `library_generate`'s
        # quarantine: promote the staged bytes into the SERVABLE store —
        # this is the ONLY place that ever copies pending-staging content
        # into the location `get_or_render_sprite`/`GET /sprite/<key>`
        # actually serve from. A no-op when there's nothing staged for this
        # key (a curator `add_live` entry never went through
        # `library_generate`, or a legacy/already-live entry re-approved).
        #
        # Promote BEFORE calling manifest.approve() (reviewer-fairy catch,
        # PR #326 round 1) -- doing it after would open a window where the
        # manifest already reads `live` but the servable store still
        # 404s/misses for `key`. A concurrent `get_or_render_sprite` cache
        # miss landing in that window falls through to its default
        # (procedural) renderer and writes THAT to the real key -- whichever
        # `put()` lands last wins, so the real approved art could get
        # permanently clobbered by a procedural fallback. Promoting first
        # closes the window: by the time the manifest says `live`, the real
        # bytes are already there. The reverse isn't a new race -- while
        # bytes are staged here but the manifest still says `pending`,
        # `ManifestStatusIndex` already fails safe (a cache-hit on a
        # not-yet-live key never serves from the real slot), so writing
        # early is harmless even if `approve()` below then refuses/404s.
        # Round-2 reviewer-fairy note (PR #326): this DOES mean "a PNG
        # exists at the servable key" no longer implies "this spec_key is
        # approved" -- that invariant was never actually load-bearing
        # anywhere (both read paths gate on manifest STATUS, never on file
        # presence alone), but don't rebuild that assumption elsewhere. See
        # test_library_approve_promoted_bytes_stay_ungated_if_approve_then_fails.
        staged = app.config["PENDING_STORE"].get(key)
        if staged is not None:
            app.config["SPRITE_STORE"].put(key, staged)

        try:
            entry = manifest.approve(sprite_dir, key, approver)
        except ManifestEntryNotFound:
            return jsonify({"error": f"no library entry for {key!r}"}), 404
        except ApprovalRefused as e:
            return jsonify({"error": str(e)}), 403
        except UnmigratedLegacyEntry as e:
            # umwelt#289/Ariadne's finding: `manifest.is_live`'s allow-list
            # fix means a status-less legacy entry is no longer silently
            # live-by-default -- `approve()` refuses it loudly instead of
            # a spurious no-op. An operational state (run the migration),
            # not a client error -- 409, matching "the resource can't be
            # acted on in its current state" rather than a 4xx client bug.
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            # Belt-and-suspenders (Borges's option 2, DM seq 2619): the
            # route guard above should make this branch unreachable, but
            # #314 round-2 makes `approve()` itself raise ValueError on a
            # falsy approver -- if any path here is ever missed, this
            # degrades to a clean 400 instead of an uncaught 500. Round-1
            # `approve()` (pre-#314-hardening) never raises this; the
            # except clause is simply dead code until round-2 lands, not
            # a behavior change for the current tip.
            return jsonify({"error": str(e)}), 400

        return jsonify({"entry": entry})

    @app.route("/act", methods=["POST"])
    def act() -> Response:
        """`POST /act` — the write chokepoint. Body
        `{"intent": "move"|"talk"|"use"|"say"|"take"|"drop"|"act", "args": {...}}`.

        The one hard invariant this whole route exists to protect: the
        gateway is a view + intent-sender, never a second source of truth.
        Every mutation goes through an EXISTING world-server verb
        (`move_to`/`send_crossing`/`use`, via `_world_request` — the same
        helper every other route in this file already uses) — this route
        never reimplements movement, messaging, or use-sandbox logic
        itself. `talk`/`use` are thin PROXIES onto the world's
        already-gated verbs (Borges's security ruling, forum #267): they
        add a PATH, never AUTHORITY. `talk` rides the world's existing
        gong consent+rate+wake gate (`_evaluate_wake`/§12.5, the ONE place
        that law lives — this route never re-derives it: `wake_me` is
        never forwarded from client input, so every `talk` crossing goes
        through `send_crossing` with the gate's own default, unrequested
        wake behavior). `use` rides the world's existing sandbox
        unchanged — `args.args` is passed through as-is for the world's
        own `_validate_use_args` to validate; this route never
        re-validates it.

        Gate order (each one short-circuits before the next is even
        considered):

        1. Auth — read the `umwelt_session` cookie; missing/invalid session
           -> 401.
        2. CSRF — the `X-Csrf-Token` header must exactly match the
           session's own `csrf_token`; missing/mismatched -> 403. A real
           gate, not decorative.
        3. Identity-injection guard — the acting agent is ALWAYS
           `session["avatar"]`, resolved server-side, NEVER from the
           request body. If `args` (or the body at large) carries a key
           literally named `agent` or `actor`, that's a hard-invariant
           violation attempt: reject with 400 and log it — never silently
           ignore the field and proceed, which could read as tacit
           acceptance of a client-asserted identity in some future refactor.
           Applies identically to every intent, `talk`/`use` included —
           no intent is exempted from this chain.

           Every intent's `_world_request` call below also forwards
           `token=session.get("world_token")` — the session's own
           world-issued credential, sent as `Authorization: Bearer
           <token>` (umwelt#495, umwelt#621b). Without this, `resolve_actor`
           on the engine side (umwelt#621's slice 1b conversion) has no
           token to prefer over the declared `agent` param this route
           always sends: a converted engine route the gateway never sends
           a token to is not converted in practice, since a valid token
           being ignored is an authentication bypass on that surface, not
           merely missing defense-in-depth (Mira's classification, #621).
        4. Intent dispatch:
           - `move`: `args.place` (place id, required) -> `move_to`.
           - `talk`: `args.to_agent` (target avatar's agent string,
             required) + `args.body` (message text, required) ->
             `send_crossing`, with `destination` resolved SERVER-SIDE from
             the caller's own current place (`_resolve_avatar_place` on
             `session["avatar"]` — never a client-supplied destination)
             and `sent_at` stamped server-side. Only `agent`, `destination`,
             `sent_at`, `to_agent`, `body`, and `mentions` are ever sent to
             the world. `mentions` is NOT client input — it's
             `parse_mentions(body)` (umwelt #511's shared parser,
             `umwelt.mentions`), computed server-side from the same `body`
             already being sent, and forwarded only when non-empty;
             `send_crossing` on the world side is the sole authority on
             folding, registry-checking, de-duping, dropping self, and
             turning each remaining name into a wake request through the
             existing wake gate (rate limits and `wake_block` apply exactly
             as they do for an explicit `wake`). No other optional
             `send_crossing` field (`wake_me`, `emotion`, ...) is forwarded
             from client input; exposing any of those as a menu option is
             still a future, explicit decision, not something this route
             adds unprompted.
           - `use`: `args.thing` (thing id, required) + `args.args`
             (optional JSON array of strings, passed through verbatim) ->
             `use`.
           - `say`: `args.body` (message text, required) -> `send_crossing`,
             modeled exactly on `talk` above (same server-resolved
             `destination`, same server-stamped `sent_at`, same
             server-computed `mentions` via `parse_mentions(body)` — umwelt
             #511 — forwarded only when non-empty) except there is
             no `to_agent` — this is a broadcast, not addressed, so the
             world payload omits that key entirely (the world's own
             `/umwelt/send_crossing` route already supports broadcast
             sends with no `to_agent`). `args.emotion` (umwelt #321,
             optional, the client's emotion-picker) IS forwarded — the one
             difference from `talk`'s no-optional-fields posture above —
             but only when it's a non-empty string within a 32-char cap;
             anything else is silently omitted (never a 400) and the world
             applies its own "calm" default, same as an emotion-less say
             always has.
           - `take`: `args.thing` (thing id, required) -> `/umwelt/take`.
             Thin proxy, same posture as `move`/`talk`/`use` above — no new
             validation/rate-limiting/consent logic here, the world's own
             `take()` route owns every state check (already-carried, not
             live, over budget, etc). The world's response has no `outcome`
             key of its own, so it's wrapped as `{"outcome": ...}`, same as
             `move`.
           - `drop`: `args.thing` (thing id, required) -> `/umwelt/drop`.
             Same shape/wrapping as `take` above.
           - `act`: `args.thing` (thing id, required) + `args.action`
             (action verb, required) + `args.args` (optional dict, passed
             through VERBATIM to the world's own `/umwelt/act` — this
             layer is deliberately schema-free about it, same pass-through
             posture `use`'s `args.args` already has; tightening this with
             a real provenance axis is #518's separate, not-yet-specced
             work, out of scope here) -> `/umwelt/act` (umwelt #522 — the
             door `act` was missing on the web surface; the engine's own
             `callers` authorization already runs unchanged on this path,
             this route adds a PATH, never AUTHORITY, same posture `talk`/
             `use` above already have). `agent` is ALWAYS
             `session["avatar"]` here too — gate 3 above (the
             identity-injection guard) already covers this generically for
             EVERY intent, `act` included, so no second, intent-local guard
             is added in this branch. `/umwelt/act`'s own response
             (`{thing_id, action, by, status, log_entry}`, plus an optional
             `effect_failures` list) has no `outcome` key of its own, so
             it's wrapped as `{"outcome": ...}`, same posture as
             `take`/`drop` above.
           - `edit`: `args.thing` (thing id, required) + `args.description`
             (string, required — an empty string is a legitimate save, only
             the TYPE is checked here) -> `/umwelt/update_thing`'s `patch:
             {description: ...}` (umwelt #547 — a thin PROXY onto
             `update_thing`, same posture as `talk`/`use` above: this adds
             a PATH, never AUTHORITY — `update_thing`'s own author-only
             gate runs unchanged). `agent` is ALWAYS `session["avatar"]`
             here too, same gate-3 coverage as every other intent.
             `/umwelt/update_thing`'s own response (`{"thing_id",
             "updated"}`) has no `outcome` key of its own, so it's wrapped
             as `{"outcome": ...}`, same posture as `take`/`drop`/`act`
             above.
           Any other intent (missing, or a real-but-not-yet-wired name) is
           a clean 400, not a stub.
        """
        token = request.cookies.get(_SESSION_COOKIE_NAME)
        session = session_store.get(token) if token else None
        if session is None:
            return jsonify(
                {"error": "no active session — log in first"}), 401

        csrf_header = request.headers.get(_CSRF_HEADER_NAME)
        try:
            csrf_ok = bool(csrf_header) and hmac.compare_digest(
                csrf_header, session["csrf_token"])
        except (TypeError, ValueError):
            # compare_digest raises TypeError on a non-ASCII str header value
            # (umwelt #232) — treat as an invalid token, same as a mismatch.
            csrf_ok = False
        if not csrf_ok:
            return jsonify({"error": "CSRF token missing or invalid"}), 403

        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            body = {}
        args = body.get("args")
        if not isinstance(args, dict):
            args = {}

        # Case-insensitive key check — a client sending "Agent"/"AGENT" must
        # be caught exactly the same as "agent" (reviewer-fairy finding on
        # PR #225: the original exact-case check let "Agent"/"AGENT" sail
        # through silently, contradicting this guard's whole purpose).
        injected_keys = {k.lower() for k in args} | {k.lower() for k in body}
        if injected_keys & {"agent", "actor"}:
            print("[act] anomalous client-asserted identity field — "
                  f"rejecting request from avatar {session['avatar']!r}")
            return jsonify({
                "error": "agent/actor may not be specified in the request "
                         "— identity comes from your session",
            }), 400

        intent = body.get("intent")
        if intent is None:
            return jsonify({"error": "intent is required"}), 400

        if intent == "move":
            place = args.get("place")
            if not isinstance(place, str) or not place.strip():
                return jsonify({"error": "args.place is required"}), 400

            try:
                outcome = _world_request(
                    world_url, "/umwelt/move_to",
                    {"agent": session["avatar"], "place": place},
                    timeout=10.0, token=session.get("world_token"))
            except WorldHTTPError as e:
                return jsonify(e.body), e.status
            except WorldUnreachable as e:
                return jsonify(
                    {"error": f"world server unreachable: {e}"}), 502

            return jsonify({"outcome": outcome})

        if intent == "talk":
            to_agent = args.get("to_agent")
            msg_body = args.get("body")
            if not isinstance(to_agent, str) or not to_agent.strip():
                return jsonify({"error": "args.to_agent is required"}), 400
            if not isinstance(msg_body, str) or not msg_body.strip():
                return jsonify({"error": "args.body is required"}), 400

            # Server-resolved, never client-supplied — same
            # gateway-resolves-identity/position-server-side posture as
            # `/session/login`/`GET /session`'s own `_resolve_avatar_place`
            # calls.
            destination = _resolve_avatar_place(
                world_url, session["avatar"], token=session.get("world_token"))
            # Same server-generated ISO8601 timestamp convention this
            # module already uses for `started_at` above (matches
            # db.now_iso()'s format on the world-server side).
            sent_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")

            crossing_payload = {
                "agent": session["avatar"], "destination": destination,
                "sent_at": sent_at, "to_agent": to_agent,
                "body": msg_body,
            }
            # umwelt #511: server-computed from the same body above, never
            # client input — see the `talk` docstring bullet. A `to_agent`
            # crossing is not hidden in this engine: @mentioning a third
            # party in a talk grants that party a wake, same as `say` —
            # pre-existing engine semantics; this PR only forwards the
            # parsed mentions.
            mentions = parse_mentions(msg_body)
            if mentions:
                crossing_payload["mentions"] = mentions

            try:
                outcome = _world_request(
                    world_url, "/umwelt/send_crossing", crossing_payload,
                    timeout=10.0, token=session.get("world_token"))
            except WorldHTTPError as e:
                return jsonify(e.body), e.status
            except WorldUnreachable as e:
                return jsonify(
                    {"error": f"world server unreachable: {e}"}), 502

            return jsonify({"outcome": outcome})

        if intent == "say":
            msg_body = args.get("body")
            if not isinstance(msg_body, str) or not msg_body.strip():
                return jsonify({"error": "args.body is required"}), 400

            # Server-resolved, never client-supplied — same posture as
            # `talk` above.
            destination = _resolve_avatar_place(
                world_url, session["avatar"], token=session.get("world_token"))
            # Same server-generated ISO8601 timestamp convention `talk`
            # above uses.
            sent_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")

            crossing_payload = {
                "agent": session["avatar"], "destination": destination,
                "sent_at": sent_at, "body": msg_body,
            }
            # umwelt #511: server-computed from the same body above, never
            # client input — see the `say` docstring bullet.
            mentions = parse_mentions(msg_body)
            if mentions:
                crossing_payload["mentions"] = mentions
            # umwelt #321: the client's optional emotion-picker pick. Never
            # 400 the say over a bad/absent emotion — a non-string or
            # over-length value is silently OMITTED (not forwarded), and
            # the world's own `send_crossing` defaults an absent emotion to
            # "calm" (its idle default), exactly the pre-#321 behavior.
            # This isinstance-str + strip + 32-char-cap check is the entire
            # gateway-side validation; no recognized-emotion set is
            # hardcoded here — the closed set lives in ONE place, the
            # client's `_EMOTION_EMOJI` map (app.js) — an unrecognized
            # string still rides through fine, since the render maps
            # unknown -> a fallback glyph rather than erroring. We store the
            # STRIPPED value (not the raw) so the check and what's forwarded
            # agree — a padded " happy " validates as "happy" and is
            # forwarded as "happy", never with the padding intact.
            emotion = args.get("emotion")
            if isinstance(emotion, str):
                emotion = emotion.strip()
                if emotion and len(emotion) <= 32:
                    crossing_payload["emotion"] = emotion

            try:
                outcome = _world_request(
                    world_url, "/umwelt/send_crossing", crossing_payload,
                    timeout=10.0, token=session.get("world_token"))
            except WorldHTTPError as e:
                return jsonify(e.body), e.status
            except WorldUnreachable as e:
                return jsonify(
                    {"error": f"world server unreachable: {e}"}), 502

            return jsonify({"outcome": outcome})

        if intent == "use":
            thing = args.get("thing")
            if not isinstance(thing, str) or not thing.strip():
                return jsonify({"error": "args.thing is required"}), 400
            use_args = args.get("args", [])

            try:
                world_response = _world_request(
                    world_url, "/umwelt/use",
                    {"agent": session["avatar"], "thing": thing,
                     "args": use_args},
                    timeout=10.0, token=session.get("world_token"))
            except WorldHTTPError as e:
                return jsonify(e.body), e.status
            except WorldUnreachable as e:
                return jsonify(
                    {"error": f"world server unreachable: {e}"}), 502

            # Do NOT re-wrap: unlike `move_to` (whose response has no
            # `outcome` key of its own, so `{"outcome": outcome}` is the
            # right shape), the world's own `POST /umwelt/use` already
            # returns a fully-formed top-level body — wrapping it again
            # produced a confusing `outcome.outcome` double-nest (caught
            # live during review, Borges's ruling: unwrap now, don't ship a
            # shape that's already known-wrong just because nothing
            # consumes it yet). Pass `world_response` straight through.
            #
            # umwelt #396 correction: this comment used to say the route
            # returns "exactly {"outcome": {...}} on every success path".
            # That stopped being true when #402 landed — a params-declaring
            # thing called with no args, or with args that fail to bind,
            # gets `{"params_spec": {...}}` (plus an `error` key on the
            # bind failure) at HTTP 200, with NO `outcome` key at all. The
            # pass-through was already right and needed no change, which is
            # exactly why the stale claim survived the engine PR: nothing
            # breaks, the sentence just quietly stops being true. Keep this
            # branch shape-agnostic — it forwards whatever the world says.
            return jsonify(world_response)

        if intent == "take":
            thing = args.get("thing")
            if not isinstance(thing, str) or not thing.strip():
                return jsonify({"error": "args.thing is required"}), 400

            try:
                outcome = _world_request(
                    world_url, "/umwelt/take",
                    {"agent": session["avatar"], "thing": thing},
                    timeout=10.0, token=session.get("world_token"))
            except WorldHTTPError as e:
                return jsonify(e.body), e.status
            except WorldUnreachable as e:
                return jsonify(
                    {"error": f"world server unreachable: {e}"}), 502

            # Same wrapping as `move` above: `/umwelt/take`'s response
            # (`{thing_id, holder_kind, holder_id, load, capacity,
            # headroom}`) has no `outcome` key of its own — unlike `use`,
            # this must NOT be passed through unwrapped.
            return jsonify({"outcome": outcome})

        if intent == "drop":
            thing = args.get("thing")
            if not isinstance(thing, str) or not thing.strip():
                return jsonify({"error": "args.thing is required"}), 400

            try:
                outcome = _world_request(
                    world_url, "/umwelt/drop",
                    {"agent": session["avatar"], "thing": thing},
                    timeout=10.0, token=session.get("world_token"))
            except WorldHTTPError as e:
                return jsonify(e.body), e.status
            except WorldUnreachable as e:
                return jsonify(
                    {"error": f"world server unreachable: {e}"}), 502

            # Same wrapping as `take` above: `/umwelt/drop`'s response
            # (`{thing_id, holder_kind, place_id}`) has no `outcome` key of
            # its own.
            return jsonify({"outcome": outcome})

        if intent == "act":
            thing = args.get("thing")
            action_verb = args.get("action")
            if not isinstance(thing, str) or not thing.strip():
                return jsonify({"error": "args.thing is required"}), 400
            if not isinstance(action_verb, str) or not action_verb.strip():
                return jsonify({"error": "args.action is required"}), 400
            # `args.args` is passed through VERBATIM — schema-free at this
            # layer, same pass-through posture `use`'s `args.args` already
            # has (see the big docstring above). Not a dict at all just
            # becomes an empty one, same as the world's own act() route
            # treats an absent/None `args` key.
            action_args = args.get("args")
            if not isinstance(action_args, dict):
                action_args = {}

            try:
                outcome = _world_request(
                    world_url, "/umwelt/act",
                    {"agent": session["avatar"], "thing": thing,
                     "action": action_verb, "args": action_args},
                    timeout=10.0, token=session.get("world_token"))
            except WorldHTTPError as e:
                return jsonify(e.body), e.status
            except WorldUnreachable as e:
                return jsonify(
                    {"error": f"world server unreachable: {e}"}), 502

            # Same wrapping as `take`/`drop` above: `/umwelt/act`'s own
            # response (`{thing_id, action, by, status, log_entry}`, plus
            # an optional `effect_failures` list) has no `outcome` key of
            # its own.
            return jsonify({"outcome": outcome})

        if intent == "edit":
            thing = args.get("thing")
            description = args.get("description")
            if not isinstance(thing, str) or not thing.strip():
                return jsonify({"error": "args.thing is required"}), 400
            if not isinstance(description, str):
                return jsonify({"error": "args.description is required"}), 400

            try:
                outcome = _world_request(
                    world_url, "/umwelt/update_thing",
                    {"agent": session["avatar"], "thing": thing,
                     "patch": {"description": description}},
                    timeout=10.0, token=session.get("world_token"))
            except WorldHTTPError as e:
                return jsonify(e.body), e.status
            except WorldUnreachable as e:
                return jsonify(
                    {"error": f"world server unreachable: {e}"}), 502

            # Same wrapping as take/drop/act above: /umwelt/update_thing's
            # response ({"thing_id", "updated"}) has no `outcome` key of
            # its own.
            return jsonify({"outcome": outcome})

        if intent == "customize_avatar":
            # Narrow proxy onto server.py's customize_avatar (2026-07-27,
            # the avatar-picker page): this intent accepts ONLY
            # `args.render_spec` — never description/lineage/type/
            # carry_capacity, which that route also allows but this page
            # has no business touching. Same session-resolves-identity
            # posture as every other intent above: `agent` is ALWAYS
            # `session["avatar"]`, never client-supplied (the generic
            # agent/actor-injection guard above already rejects a body
            # that tries).
            #
            # LOAD-BEARING DEPENDENCY (Ariadne's #379 colleague-round
            # trace, worth naming so a future author doesn't break it
            # silently): this intent narrows by FIELD, not by VALUE — ANY
            # dict passes as `render_spec`, including one naming a
            # spec_key nobody has generated yet. That's only safe because
            # the render path (`_decorate_thing_sprite`/`get_or_render_
            # sprite`) is resolve-or-fallback and NEVER generates on read.
            # If a future "generate a custom avatar" flow (the family
            # fast-path half deliberately NOT built in this PR, pending
            # Borges's security-lane read) ever makes an unknown
            # `render_spec` trigger generation on the READ/apply path
            # rather than the author path, THIS intent silently becomes
            # the bypass around the moderation gate that #379 was careful
            # not to build. Keep the render path generation-free, or this
            # comment is the thing that stops being true.
            if "render_spec" not in args:
                return jsonify(
                    {"error": "args.render_spec is required"}), 400
            render_spec = args["render_spec"]
            if not isinstance(render_spec, dict):
                return jsonify(
                    {"error": "args.render_spec must be an object"}), 400
            extra_keys = set(args) - {"render_spec"}
            if extra_keys:
                return jsonify({
                    "error": f"unsupported field(s) for this intent: "
                             f"{sorted(extra_keys)} — only render_spec is "
                             f"accepted here",
                }), 400

            try:
                world_response = _world_request(
                    world_url, "/umwelt/customize_avatar",
                    {"agent": session["avatar"], "render_spec": render_spec},
                    timeout=10.0, token=session.get("world_token"))
            except WorldHTTPError as e:
                return jsonify(e.body), e.status
            except WorldUnreachable as e:
                return jsonify(
                    {"error": f"world server unreachable: {e}"}), 502

            # `/umwelt/customize_avatar` already returns `{"avatar": {...}}`
            # — no `outcome` key, same unwrap-don't-rewrap reasoning as
            # `use` above.
            return jsonify(world_response)

        return jsonify({"error": f"intent {intent!r} not yet supported"}), 400

    @app.route("/voice", methods=["POST"])
    def voice() -> Response:
        """`POST /voice` — body `{"crossing_id": "..."}`. Plays back a
        crossing's body as synthesized speech in the sender's cloned
        voice, via the standalone voice-playback service
        (internal tooling, not in this release). Currently a plain button on the
        client (`app.js`) — per Borges's own framing, this becomes a #352
        gadget once the charter vocab lands; functional now, not wrapped
        in that system.

        The one hard invariant this route exists to protect (Borges's
        spec): a client must never be able to say "speak this text as
        this sender", only "play back crossing X" — so no
        `sender`/`voice`/`text` field is EVER read from the request body;
        both are resolved server-side from the crossing itself, via the
        world's new `POST /umwelt/crossing_lookup`.

        The reverse direction is guarded too (Borges's confirmed pass):
        `crossing_lookup`'s `{sender, body}` response is consumed
        INTERNALLY by this route only and never forwarded back to the
        browser — every response this route sends the client is either
        raw audio bytes or a bare `{"error": ...}`, so `/voice` adds no
        new text-disclosure surface beyond what the client already has
        from rendering the crossing via `/crossings` in the first place.

        Gate order (mirrors `/act`'s own gate order above exactly):

        1. Auth — the `umwelt_session` cookie; missing/invalid session ->
           401.
        2. CSRF — the `X-Csrf-Token` header must exactly match the
           session's own `csrf_token`; missing/mismatched -> 403.
        3. `voice_service_url` not configured (`create_app`'s keyword,
           `None` by default) -> 503
           `{"error": "voice service not configured on this gateway"}` —
           same enhancement-never-dependency posture as
           `/library/generate`'s `art_service_url` gate above (Kepler's
           rule): an undeployed/dead voice service must never take the
           rest of this gateway down with it.
        4. `crossing_id` resolution — `POST /umwelt/crossing_lookup` on
           the world server. Unknown id -> this route also 404s
           `{"error": "crossing not found"}` (the world's own body,
           forwarded verbatim). World unreachable -> 502.
        5. Synthesis — `POST /synthesize` on the voice service, with
           `X-Umwelt-Agent` set to the SESSION's own identity (the
           viewer/listener who clicked play, never the crossing's sender
           — the voice service's own cross-spec pin (internal tooling, not in this release): quota there is
           per-viewer, so one listener can't exhaust everyone's budget).
           `cache_key` is a stable sha256 hex digest of the crossing's own
           `sender`+`body` (same `hashlib.sha256(...).hexdigest()` idiom
           `sprites.spec_key` already uses for its own content-addressed
           key, applied here to `canonical_json({"sender":..., "body":
           ...})` instead of a render_spec) — deterministic, so repeat
           listens of the same crossing are free cache hits, per
           the voice service's own cache-before-quota contract (internal tooling, not in this release).
           - success (WAV bytes) -> streamed back verbatim, same
             `audio/wav` content-type.
           - `404 {"error": "voice_not_enrolled"}` -> passthrough 404,
             UNCHANGED — the client's cue to degrade to text-only; this is
             an expected/common outcome, never treated as an error.
           - any OTHER voice-service response (400/401/429/503/...) or a
             connection-level failure -> 502, never a crash — same
             posture as `/library/generate`'s `GenerationUnavailable` ->
             502 handling above. These other statuses are all "the
             service couldn't help right now" outcomes this route
             deliberately collapses into one generic "voice unavailable"
             signal, rather than teaching the client to distinguish
             quota/busy/misconfigured cases it can't act on differently
             from a plain network failure.
        """
        session, error = _voice_route_gate(session_store, app)
        if error:
            return error
        voice_service_url = app.config["VOICE_SERVICE_URL"]

        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            body = {}
        crossing_id = body.get("crossing_id")
        if not isinstance(crossing_id, str) or not crossing_id.strip():
            return jsonify({"error": "crossing_id is required"}), 400

        try:
            crossing = _world_request(
                world_url, "/umwelt/crossing_lookup",
                {"crossing_id": crossing_id.strip()}, timeout=10.0)
        except WorldHTTPError as e:
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        sender = crossing.get("sender") or ""
        text = crossing.get("body") or ""
        cache_key = hashlib.sha256(
            canonical_json({"sender": sender, "body": text}).encode("utf-8")
        ).hexdigest()

        # Voice synthesis latency scales ~linearly with text length (~0.09s/char,
        # measured live 2026-07-25). The default 30s upstream timeout
        # silently 502'd long crossings while the service still synthesized them.
        # Scale the budget to the work, with a floor comfortably above short
        # crossings and a ceiling that bounds how long one connection can hang.
        # The 180s ceiling assumes crossing bodies stay under ~2000 chars --
        # there is currently no upstream length cap on a crossing body, so a
        # longer body would re-hit the same silent-timeout failure at a
        # higher threshold (tracked as a follow-up, not fixed here).
        synth_timeout = min(180.0, max(45.0, len(text) * 0.15 + 15.0))
        try:
            wav = _voice_synthesize_request(
                voice_service_url, session["avatar"], sender, text,
                cache_key, timeout=synth_timeout)
        except VoiceServiceHTTPError as e:
            if e.status == 404 and e.body.get("error") == "voice_not_enrolled":
                return jsonify({"error": "voice_not_enrolled"}), 404
            return jsonify({
                "error": f"voice service unavailable: "
                         f"{e.body.get('error', e.status)}",
            }), 502
        except VoiceServiceUnreachable as e:
            return jsonify(
                {"error": f"voice service unreachable: {e}"}), 502

        return Response(wav, mimetype="audio/wav")

    @app.route("/voice/manifest", methods=["POST"])
    def voice_manifest() -> Response:
        """`POST /voice/manifest` — body `{"crossing_id": "..."}`. Forum
        #282 voice v1 (Kepler's ruling, forum #282 reply #5107): the
        manifest half of the manifest-fetch transport that supersedes
        whole-message `/voice` — ADDITIVE, not a replacement. `/voice`
        above is untouched and stays the live path for the current
        listen-button until the #352-gadget-charter client lands and
        cuts over to this pair of routes; both can run side by side
        indefinitely, since they hit the same voice-service cache under
        different (whole-message vs per-section) cache keys and neither
        one's traffic corrupts the other's cache entries.

        Same gate order as `/voice` (auth -> CSRF -> voice_service_url
        configured -> crossing_lookup), then: section the resolved body
        (`_section_crossing_text`), compute each section's own
        content-addressed cache key (`_voice_section_cache_key`), and
        report how many are already cached (`ready`) — a best-effort,
        ADVISORY count only. A `_voice_cache_check_request` failure
        (voice service down/unreachable) does not fail this route; it
        just assumes that section isn't cached (`ready` undercounts,
        never overcounts) and further checks past the first failure are
        skipped, same short-circuit the analogous manifest-is-a-SHORT-
        request property in Kepler's ruling wants — one dead voice
        service must not turn a fast manifest fetch into N slow timeouts.

        `urls` are `/voice/section` paths (index + crossing_id in the
        query string) the future client gadget POSTs to (with the same
        CSRF header this route itself required) — not passively
        `<audio src>`-able GETs, same CSRF posture as every other
        session-gated mutation in this file.
        """
        session, error = _voice_route_gate(session_store, app)
        if error:
            return error
        voice_service_url = app.config["VOICE_SERVICE_URL"]

        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            body = {}
        crossing_id = body.get("crossing_id")
        if not isinstance(crossing_id, str) or not crossing_id.strip():
            return jsonify({"error": "crossing_id is required"}), 400
        crossing_id = crossing_id.strip()

        try:
            crossing = _world_request(
                world_url, "/umwelt/crossing_lookup",
                {"crossing_id": crossing_id}, timeout=10.0)
        except WorldHTTPError as e:
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        sender = crossing.get("sender") or ""
        text = crossing.get("body") or ""
        sections = _section_crossing_text(text)

        ready = 0
        for index in range(len(sections)):
            section_key = _voice_section_cache_key(sender, text, index)
            try:
                cached = _voice_cache_check_request(
                    voice_service_url, section_key)
            except (VoiceServiceHTTPError, VoiceServiceUnreachable):
                break  # advisory only -- see docstring; don't guess past a failure
            if not cached:
                break  # sections generate in order -- a gap here means the
                       # rest aren't cached either in the common case
            ready += 1

        urls = [
            f"/voice/section?crossing_id={urllib.parse.quote(crossing_id)}"
            f"&index={index}"
            for index in range(len(sections))
        ]
        return jsonify({
            "sections": len(sections), "ready": ready, "urls": urls,
        })

    @app.route("/voice/section", methods=["POST"])
    def voice_section() -> Response:
        """`POST /voice/section?crossing_id=...&index=...` (query string,
        so the future client gadget can POST the manifest's own `urls`
        verbatim with no body) — forum #282 voice v1's per-section fetch.
        `crossing_id`/`index` are also accepted from a JSON body for
        symmetry with `/voice` and `/voice/manifest`; query string wins if
        both are present (matches the `urls` this route's own manifest
        sibling hands out).

        Same gate order as `/voice`/`/voice/manifest` (auth -> CSRF ->
        voice_service_url configured -> crossing_lookup), then:
        re-derive this crossing's sections FRESH from the resolved body
        (`_section_crossing_text`) rather than trusting a client-supplied
        section text — this route is stateless by design, same as
        `/voice` itself; it shares no server-side state with whatever
        `/voice/manifest` call preceded it, so a restart between the two
        calls, or a call to this route with no prior manifest call at
        all, both still work correctly. `index` out of range for the
        resolved crossing -> 400 (never a client-supplied text override —
        the client-supplies-only-crossing_id line holds exactly as
        it does for `/voice`).

        Response shape and every failure mapping (voice_not_enrolled
        passthrough, any-other-error -> generic 502, unreachable -> 502)
        are identical to `/voice`'s own — this is the same synth call,
        just against one section's text and that section's own cache key
        instead of the whole message's."""
        session, error = _voice_route_gate(session_store, app)
        if error:
            return error
        voice_service_url = app.config["VOICE_SERVICE_URL"]

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            body = {}
        crossing_id = request.args.get("crossing_id") or body.get("crossing_id")
        if not isinstance(crossing_id, str) or not crossing_id.strip():
            return jsonify({"error": "crossing_id is required"}), 400
        crossing_id = crossing_id.strip()

        index_raw = request.args.get("index")
        if index_raw is None:
            index_raw = body.get("index")
        try:
            index = int(index_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "index (integer) is required"}), 400
        if index < 0:
            return jsonify({"error": "index must be non-negative"}), 400

        try:
            crossing = _world_request(
                world_url, "/umwelt/crossing_lookup",
                {"crossing_id": crossing_id}, timeout=10.0)
        except WorldHTTPError as e:
            return jsonify(e.body), e.status
        except WorldUnreachable as e:
            return jsonify(
                {"error": f"world server unreachable: {e}"}), 502

        sender = crossing.get("sender") or ""
        text = crossing.get("body") or ""
        sections = _section_crossing_text(text)
        if index >= len(sections):
            return jsonify({
                "error": f"index {index} out of range "
                         f"({len(sections)} section(s))",
            }), 400

        section_text = sections[index]
        section_key = _voice_section_cache_key(sender, text, index)

        synth_timeout = min(
            180.0, max(45.0, len(section_text) * 0.15 + 15.0))
        try:
            wav = _voice_synthesize_request(
                voice_service_url, session["avatar"], sender, section_text,
                section_key, timeout=synth_timeout)
        except VoiceServiceHTTPError as e:
            if e.status == 404 and e.body.get("error") == "voice_not_enrolled":
                return jsonify({"error": "voice_not_enrolled"}), 404
            return jsonify({
                "error": f"voice service unavailable: "
                         f"{e.body.get('error', e.status)}",
            }), 502
        except VoiceServiceUnreachable as e:
            return jsonify(
                {"error": f"voice service unreachable: {e}"}), 502

        return Response(wav, mimetype="audio/wav")

    @app.route("/debug/client_error", methods=["POST"])
    def debug_client_error() -> Response:
        """`POST /debug/client_error` — umwelt#286's diagnostic sink for
        `app.js`'s global `window.onerror`/`unhandledrejection` handlers.
        There was previously NO client-side error capture at all — a
        real-device crash left no trace beyond a user's own report — so
        this exists purely to give the next investigation actual data,
        not to fix umwelt#286 itself (a true browser-tab OOM kill is
        invisible to any JS instrumentation; this only ever catches an
        uncaught JS exception/rejection).

        Deliberately UNAUTHENTICATED and uses NO CSRF gate, unlike every
        other mutating route in this file — two properties that would be
        a problem for a route that changes session/world state, but not
        here:
        1. A crash can happen before login (spectator mode) or exactly
           because login/session state is broken, so gating this behind
           a session would silently drop the reports most worth having.
        2. This route touches no session, world, or account state at all
           — it only appends a bounded log line. A forged cross-site
           report is a "someone can write junk into a diagnostic log"
           nuisance, not an account-integrity or data-exfiltration risk,
           so CSRF's threat model (an attacker acting AS the victim on
           something that affects the victim) doesn't apply.

        Two request-level gates run BEFORE any parsing (Ariadne's forum
        #432 spec), since an oversized or flooding client is the one case
        this route's usual body-field truncation below doesn't cover --
        that truncation bounds each FIELD once the JSON has already been
        parsed and stored, not the raw request itself:
        - **8 KB body cap** (`_CLIENT_ERROR_MAX_BODY_BYTES`): the body is
          read directly off `request.stream`, bounded to
          `_CLIENT_ERROR_MAX_BODY_BYTES + 1` bytes, and parsed by hand
          with `json.loads` -- a request whose actual content exceeds the
          cap is refused with 413 before anything is stored. This is
          deliberately NOT `request.content_length` + `request.get_json()`
          (round 1 of this route's own review found that pairing has two
          real gaps, both verified against a real werkzeug server, not
          just the Flask test client, which cannot reproduce either): a
          chunked request with no `Content-Length` header reads `None`
          from `content_length` and would skip a length-based gate
          entirely, and `get_json()`'s own Content-Type gate requires
          `application/json`, which rejects a `sendBeacon`'s own default
          Content-Type of `text/plain;charset=UTF-8` for a bare string
          payload (see `app.js`'s `_reportClientError` for why the client
          sends a bare string rather than a typed `Blob`). Reading a
          bounded slice of the stream and parsing it directly closes both
          gaps at once: it never looks at `Content-Length` or
          Content-Type, and a body longer than the cap is caught by the
          read itself regardless of how the client declared its length.
        - **20 requests/minute per client IP**
          (`_CLIENT_ERROR_RATE_LIMIT_PER_MINUTE`): an in-memory sliding
          window keyed on `request.remote_addr`, stored on
          `app.config["CLIENT_ERROR_RATE_STATE"]` (test seam -- a fresh
          `create_gateway_app()` call, as every test's `gw_client` fixture
          makes, starts with an empty window). A client over the limit is
          refused with 429 and its report is dropped -- never written to
          the log -- so a crash-looping client can't fill the log file on
          its own. This is deliberately process-local, in-memory, best-
          effort state -- exactly like `SessionStore`/`SpriteStore`
          elsewhere in this module -- not a durability guarantee. Two
          bounds worth naming rather than fixing (Aleph's colleague-review
          nits, PR-848): an idle IP's window entry is only pruned on that
          IP's OWN next request, so "pruned on every call" means every
          call from that key, not a background sweep -- an IP that never
          returns leaves one small dict entry behind rather than freeing
          it, which does not matter at this host's scale. And if this
          gateway ever sits behind a reverse proxy, `request.remote_addr`
          becomes the proxy's own address, collapsing every real client
          onto one shared 20/minute bucket -- not checked against how
          production is actually deployed.

        Every field is size-bounded SERVER-SIDE regardless of what the
        client claims to have already truncated (never trust client-side
        truncation) — a malformed or missing field degrades to an empty
        string/`None` rather than a 4xx, and any file-write failure is
        swallowed — this route ALWAYS returns 204 on a successful write,
        on purpose: it must never itself become a second error for the
        client's fire-and-forget `sendBeacon`/`fetch(...).catch(() => {})`
        call to chase. The 413/429 cases are real, distinct outcomes
        (oversized / flooding) rather than internal failures, so they get
        their own status codes -- the client never reads either response,
        since both transports are fire-and-forget, but the log stays a
        true record of what was actually accepted.

        The log file itself is bounded (Ariadne's "rotate at a few MB"):
        before appending, if the file already exceeds
        `_CLIENT_ERROR_LOG_ROTATE_BYTES`, the existing file is renamed to
        `<path>.1` (overwriting any prior `.1`) and a fresh file started --
        one rotation, not an indefinitely-growing backlog, matching this
        route's own "observability only" scope."""
        remote_addr = request.remote_addr or "unknown"
        if not _client_error_rate_allow(app, remote_addr):
            return "", 429
        # Read the raw body directly off request.stream, bounded to
        # MAX+1 bytes, and parse it by hand -- rather than checking
        # request.content_length and then calling request.get_json()
        # (Aleph's colleague-review finding, PR-848, verified against a
        # real werkzeug server, not just the Flask test client, which
        # cannot reproduce this): content_length is None on a chunked
        # request with no Content-Length header, which skipped the old
        # 413 gate entirely and let get_json() read the body unbounded.
        # Reading a BOUNDED slice up front closes that regardless of
        # whether Content-Length is present, chunked or not. It also
        # replaces request.get_json()'s own Content-Type gate (which
        # required "application/json" and rejected the client's own
        # sendBeacon default of "text/plain;charset=UTF-8" -- a SEPARATE
        # finding on this same PR) with a parse that doesn't look at
        # Content-Type at all.
        raw = request.stream.read(_CLIENT_ERROR_MAX_BODY_BYTES + 1)
        if len(raw) > _CLIENT_ERROR_MAX_BODY_BYTES:
            return "", 413
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            body = {}
        stack = body.get("stack")
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": str(body.get("kind", ""))[:64],
            "message": str(body.get("message", ""))[:2000],
            "stack": str(stack)[:4000] if stack else None,
            "user_agent": str(body.get("user_agent", ""))[:500],
            "url": str(body.get("url", ""))[:500],
            "last_action": (str(body.get("last_action"))[:64]
                             if body.get("last_action") else None),
        }
        log_path: Path = app.config["CLIENT_ERROR_LOG_PATH"]
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if (log_path.exists()
                    and log_path.stat().st_size
                    > _CLIENT_ERROR_LOG_ROTATE_BYTES):
                log_path.replace(log_path.with_suffix(
                    log_path.suffix + ".1"))
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # best-effort -- a logging failure must never surface here
        return "", 204

    @app.route("/")
    def index() -> Response:
        return app.send_static_file("index.html")

    return app


# Art-library slice 1: the durable, external sprite-store default — outside
# the versioned `src/umwelt/webrender/` tree (never wiped by a redeploy's
# re-checkout), mirroring the existing `~/.umwelt/venv`, `~/.umwelt/
# marketplace` convention for outside-the-repo persistent state. The
# manifest `library.py` reads (`<sprite-dir>/master_manifest.json`) lives
# alongside the PNGs in this same directory — see the promotion
# script (internal tooling, not in this release), the one-shot script that populates it from the
# curation workspace. Not read or touched by this module itself — `main()`
# only resolves the path and passes it through to `create_app`'s existing
# `sprite_dir` seam.
_DEFAULT_SPRITE_DIR = Path.home() / ".umwelt" / "sprite-library"


def main() -> None:
    p = argparse.ArgumentParser(
        description="Umwelt web-render gateway (port 5011)")
    p.add_argument("--port", type=int, default=5011)
    # Post-review hardening (Aleph's off-host security pass, F1): default to
    # loopback-only, matching the read-only property's own fail-closed
    # instinct — a service with no auth should not default to every
    # interface just because it's convenient for private-network dev/testing. Widening
    # to private interfaces (or 0.0.0.0) is an explicit opt-in, not the default.
    p.add_argument("--host", default="127.0.0.1")
    # umwelt#699 §1: default=None, not resolve_world_url() -- the resolver
    # must see the parsed VALUE (a real --world-url the operator typed is a
    # caller override the old default-factory form silently bypassed).
    # Routed through the resolver just below, after parse_args().
    p.add_argument("--world-url", default=None)
    # Art-library slice 1: makes `create_app`'s existing test-only
    # `sprite_dir` param also a real production-facing option — the sprite
    # cache (and, per library.py, the promoted art-library manifest
    # alongside it) now lives outside the repo tree by default, surviving a
    # redeploy's re-checkout. Doesn't change `create_app`'s own signature/
    # behavior, just wires a real caller to it here.
    p.add_argument("--sprite-dir", default=str(_DEFAULT_SPRITE_DIR))
    # M4 art-gen slice 1 — optional; `POST /library/generate`
    # 503s cleanly when unset rather than this gateway refusing to boot
    # without a GPU service alongside it (enhancement, never a dependency).
    p.add_argument("--art-service-url", default=None)
    # voice-playback v0 (#309) — optional, same enhancement-never-
    # dependency posture as --art-service-url above; `POST /voice` 503s
    # cleanly when unset.
    p.add_argument("--voice-service-url", default=None)
    p.add_argument("--tls-cert", default=None, help=(
        "Path to a TLS certificate file (umwelt#688, part of #583). Both "
        "--tls-cert and --tls-key are required together for TLS; both "
        "absent means plaintext HTTP, today's unchanged default. Exactly "
        "one given, or an unreadable/mismatched/expired cert, refuses to "
        "start rather than silently serving plaintext -- see "
        "tls_context.py's own docstring for the full fail-closed "
        "contract. A login through this TLS listener also gets a Secure "
        "session cookie -- derived per-request from request.is_secure, "
        "not a process-wide flag (see /session/login's own docstring)."
    ))
    p.add_argument("--tls-key", default=None, help=(
        "Path to the TLS private key file (umwelt#688). Requires "
        "--tls-cert. Must be a regular file, not group/world-readable -- "
        "see tls_context.py."
    ))
    p.add_argument("--tls-port", type=int, default=None, help=(
        "Port for a SECOND listener, in this SAME process, serving TLS "
        "alongside the existing plaintext --port (umwelt#699's cutover "
        "migration window, same shape as server.py's own --tls-port: the "
        "plaintext listener must stay up while TLS also becomes "
        "available, so Lei's browser bookmark and any not-yet-migrated "
        "client keep working). Required together with --tls-cert/"
        "--tls-key; refused if it collides with --port. Deliberately ONE "
        "PROCESS, not two -- see tls_context.serve_dual_listener's own "
        "docstring for the shared reasoning (Ariadne's measured hazards "
        "on umwelt#699, 2026-08-23), which applies to this gateway's own "
        "session store and rate limiter exactly as it does to the world "
        "engine's db and identity-bind socket."
    ))
    args = p.parse_args()
    args.world_url = client_auth.resolve_world_url(explicit=args.world_url)

    ssl_context = resolve_tls_context(args.tls_cert, args.tls_key)
    tls_args_error = check_tls_args(ssl_context, args.tls_port, args.port)
    if tls_args_error is not None:
        p.error(tls_args_error)

    sprite_dir = Path(args.sprite_dir).expanduser()
    app = create_app(args.world_url, sprite_dir=sprite_dir,
                      art_service_url=args.art_service_url,
                      voice_service_url=args.voice_service_url,
                      tls_enabled=ssl_context is not None)
    if ssl_context is None:
        app.run(host=args.host, port=args.port)
    else:
        # umwelt#699/#583: shared with server.py via tls_context.py --
        # see that module's own docstring for why both servers need the
        # identical one-process/two-listener shape.
        serve_dual_listener(app, args.host, args.port, args.tls_port,
                             ssl_context)


if __name__ == "__main__":
    main()
