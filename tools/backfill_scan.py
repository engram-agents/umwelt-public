#!/usr/bin/env python3
"""backfill_scan.py — the READ-half of the client-side world-backfill harness
(Lei's "no more circles and squares" charge). Walks the world from the
running agent's OWN standpoint, classifies every thing/place by art-status
(two honest axes: what renders TODAY vs what's DURABLE in the curated
manifest), proposes a render_spec class for NO_ART gaps, and emits a
structured worklist JSON + a human summary on stdout.

The GENERATE-half (call the art-gen API -> add_pending) is OUT OF SCOPE
here — it wires on top of this worklist once the art-gen service is green.
Off-host-runnable: every agent runs this as themselves, over HTTP only, for
their own view of the world.

--- Off-host HTTP fix (2026-07-24) --------------------------------------

Luria, running the merged scanner off-host, found BOTH art-status axes were
silently lying: `durable` came from `library.resolve(render_spec, manifest)`
where `manifest = library.load_manifest(SERVE_DIR)` — the LOCAL disk's
`~/.umwelt/sprite-library/master_manifest.json`, which only exists on-host,
so off-host every `durable` came back False and the worklist reported
`REAL_DURABLE: 0` no matter how much curated art actually existed.
`renders_today` had the same disease: `(serve_dir / f"{key}.png").exists()`
checked the RUNNING AGENT's own stale local PNG cache, not what the gateway
actually serves. Both axes now go through the render gateway's HTTP API
(`GET /library` for the manifest, `GET /sprite/<key>` for the cache-hit
check) — ONE code path, on-host and off-host identical, no local-disk
fallback. See `fetch_library_manifest`/`sprite_cached` below. A gateway that
is genuinely unreachable now FAILS THE WHOLE SCAN LOUD (`BackfillScanError`
from `run_scan`) rather than silently reporting the same false
`REAL_DURABLE: 0` — that silent-zero was the actual incident, not a
cosmetic inaccuracy: it would have driven real re-generation effort against
art that already existed.

--- PRE-VERIFICATION corrections against server.py HEAD (2026-07-24) -------

The original spec for this tool assumed a `POST /umwelt/look {agent, place}`
endpoint (place optional, defaulting to the caller's current place). Reading
server.py's actual routes found three corrections, all load-bearing for the
walk's shape:

1. `/umwelt/look` takes ONLY `{agent}` — there is no `place` parameter at
   all. It ALWAYS renders the caller's own current place (resolved from
   `db.get_avatar(agent)["place_id"]`). To read an ARBITRARY place (i.e.
   every place visited during the walk other than the start), the real
   read-only primitive is `GET /umwelt/place_view?place=<id>` — but that
   route is always anonymous (`agent=None` internally), so it only ever
   sees PUBLIC places, never the walking agent's own private ones (e.g.
   their home post) beyond what the one `places?agent=` bootstrap call
   already resolved for them. This tool therefore uses `/umwelt/look` ONLY
   for the walk's start place, and `/umwelt/place_view` for every other
   place — and a private place discovered several hops out (not part of
   the agent's own bootstrap neighborhood) 404s there and is skipped, same
   as any other per-place read failure (see the Networking section below).
   This never reads the flat, unauthenticated `db.list_places` god-view
   either endpoint sits on top of `_read_place`/`_place_view`, which always
   composes through `db._place_visible_to` — the exact property Kepler's
   #164 ruling requires.

2. Neither `/umwelt/look` nor `/umwelt/place_view`'s bulk `things` list
   carries a thing's `metadata` at all (`_place_view` trims each thing down
   to `id/name/description/kind/lane/size fields` — no `metadata` key,
   confirmed by reading `_place_view` in server.py). So a thing's
   `metadata.render_spec` is NOT available from the bulk read; this tool
   calls `POST /umwelt/look_at {thing}` once per non-kinddef thing to get
   the full record (`db.get_thing` output, which DOES carry `metadata`).

3. Avatars never appear in a place's `things` array at all —
   `db.things_in_place` explicitly excludes body-kind (avatar) things. The
   "who's here" data lives in the SAME look/place_view response's separate
   `present` list (`_present_summary`: `{agent, avatar_id, name, type,
   lineage, emotion, lane}`) — this tool sources the `avatars` bucket from
   `present`, not from `things`.

Field-name corrections against the spec's guesses:
  - the place-tree node's lateral-link field is `links` (not `laterals`).
  - `/umwelt/places?agent=` response's top-level shape (`current`, `parent`,
    `anchors`, `children_skipped`) was confirmed exactly as guessed.
  - `/umwelt/look` and `/umwelt/place_view` both return `{place, things,
    present, unseen_count, reachable, parent}` — `reachable` is a typed list
    (`{id, label, relation}`, relation in child/link/parent) that is the
    walk's frontier-expansion mechanism for every place beyond the initial
    bootstrap neighborhood (see `run_scan`'s docstring for the full walk
    design).

--- Config -------------------------------------------------------------
`WORLD_URL`/`AGENT` default from `~/.umwelt/config.json` (`world_url`/
`agent` keys) — same file `tools/uw` and the MCP wrapper read. `RENDER_URL`
is the render gateway (normally :5011, the process that owns the
moderation-of-record `master_manifest.json` and the servable sprite cache)
— it defaults by swapping `WORLD_URL`'s port (`_derive_render_url`), or
reads an explicit `render_url` key from the same config file for a
non-standard topology. Both art-status axes go through this gateway's HTTP
API only (`GET /library` + `GET /sprite/<key>`) — there is no local-disk
`SERVE_DIR` anymore; see the fix note above.

Module constants are read fresh (never captured as function-default
argument values) so tests can monkeypatch `backfill_scan.AGENT` /
`backfill_scan.WORLD_URL` / `backfill_scan.RENDER_URL` and have every
downstream call see the patched value.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

# tools/backfill_scan.py lives outside the `umwelt` package (repo-root ops
# tooling, same posture as tools/uw) — make `src/` importable without
# requiring the caller to have set PYTHONPATH themselves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from umwelt import client_auth as _client_auth  # noqa: E402
from umwelt import engine_api as _engine_api  # noqa: E402
from umwelt.webrender import library  # noqa: E402
from umwelt.webrender.sprites import spec_key  # noqa: E402


# --- config (monkeypatchable module-level seams) ----------------------------

def _load_client_config() -> dict:
    path = Path.home() / ".umwelt" / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


_CLIENT_CONFIG = _load_client_config()

# umwelt#699: resolved LAZILY (first main()/run_scan() call), not at import
# time -- see _ensure_urls_resolved() below. `resolve_world_url()` now
# RAISES on a malformed config or a plaintext non-loopback value (§2/§3)
# where it used to silently fall through; resolving it eagerly here made
# merely importing this module crash on a broken config, not just an
# actual scan attempt (umwelt#710 reviewer-fairy finding). Left as real,
# monkeypatchable module attributes (per this module's own documented
# seam above) -- the guard only fills them in when still `None`.
WORLD_URL: str | None = None
AGENT = _CLIENT_CONFIG.get("agent")
RENDER_URL: str | None = None


def _derive_render_url(world_url: str) -> str:
    """The render gateway (:5011) always runs on the world's own host —
    derive its URL by swapping the world's port. A `world_url` that isn't
    the standard `:5003` falls through UNCHANGED (nothing to swap), so a
    non-standard topology (gateway on a different host/port than the
    default pairing) must set `render_url` explicitly in
    `~/.umwelt/config.json` rather than rely on this heuristic."""
    return world_url.replace(":5003", ":5011")


def _ensure_urls_resolved() -> None:
    """Resolve WORLD_URL/RENDER_URL on first actual use. Idempotent, and
    a direct monkeypatch of either (this module's own documented seam)
    still works -- this only fills in whichever is still unset."""
    global WORLD_URL, RENDER_URL
    if WORLD_URL is None:
        WORLD_URL = _client_auth.resolve_world_url()
    if RENDER_URL is None:
        RENDER_URL = _CLIENT_CONFIG.get("render_url") or _derive_render_url(WORLD_URL)

_DEFAULT_WORKLIST_PATH = "/tmp/backfill_worklist.json"
_LANES = ("sky", "land", "road")


class BackfillScanError(Exception):
    """Raised for a FATAL scan-level failure (no self-standpoint to walk
    from at all) — never for a single place/thing read failure, which is
    handled per-item (log + skip, see the Networking section of the
    module docstring)."""


def _log(msg: str) -> None:
    print(f"[backfill_scan] {msg}", file=sys.stderr)


# In-memory-only session cache (umwelt #495/#646) — a single scan run makes
# many world calls against ONE agent identity, so this genuinely caches
# across the whole BFS walk. Never persisted; the server's 401 is the
# validity signal, same as tools/uw's per-process _UW_SESSION.
_SESSION = _client_auth.AgentSession()


# --- networking (read-only; the six monkeypatchable HTTP seams) ------------
#
# Four target the world (:5003, via WORLD_URL): fetch_agent_places/
# fetch_look/fetch_place_view/fetch_look_at. Two target the render gateway
# (:5011, via RENDER_URL): fetch_library_manifest/sprite_cached, below —
# the world and the gateway are independently reachable/unreachable
# processes, so these are deliberately separate seams, not folded into the
# world-facing four.
#
# Client-auth (umwelt #495/#646): `_http_request` acquires an Authorization
# bearer header via client_auth whenever the caller passes `agent=` — the
# render-gateway call sites (fetch_library_manifest/sprite_cached) never do,
# since the gateway is a different service with no identity concept. Of the
# four world-facing fetchers, `fetch_agent_places`/`fetch_look`/
# `fetch_look_at` pass `agent=`; `fetch_place_view` deliberately does NOT.
# Reviewer-fairy correction on PR#650: the module docstring's correction #1
# ("no `agent` param exists on that route at all") is stale as of umwelt#554
# (merged 2026-08-09) — `/umwelt/place_view` now takes an OPTIONAL, declared
# `agent` that changes owner-visibility and the unseen-count delta. The real
# reason to leave it unwired is different: that route never calls
# `resolve_actor` (grep confirms), so it never token-verifies whatever
# `agent` it's given — a bearer token attached here would be silently inert,
# not merely unused. A present-but-failing credential
# raises `client_auth.AuthFailure`, re-raised here as `BackfillScanError`
# (fail closed — this tool already treats that exception class as fatal at
# the one call site that must not degrade, `fetch_agent_places`, and as a
# per-item skip everywhere else via each fetch_* wrapper's own try/except).

def _http_request_once(method: str, path: str, *, params: dict | None,
                        payload: dict | None, base: str,
                        auth_headers: dict[str, str]) -> tuple[int, dict]:
    """Single low-level GET/POST -> (status, body_dict) against `base`, with
    `auth_headers` merged in. Raises BackfillScanError on a transport
    failure (server unreachable) — never on an HTTP error status, which is
    returned as a normal (status, body) pair for the caller to interpret.

    Only ever used for JSON-body routes — `json.loads(body)` on every
    response would crash against a binary payload (e.g. `/sprite/<key>`'s
    PNG bytes), which is exactly why the gateway's sprite-existence check
    below uses the separate `_http_status` HEAD helper instead of this
    function."""
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    headers.update(auth_headers)
    headers.update(_engine_api.client_headers())  # umwelt#1105
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _client_auth.urlopen_with_cert_fallback(req, 30) as r:
            body = r.read().decode("utf-8")
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, (json.loads(body) if body else {})
        except json.JSONDecodeError:
            return e.code, {"error": body or f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BackfillScanError(f"cannot reach {base}: {e}") from e


def _http_request(method: str, path: str, *, params: dict | None = None,
                   payload: dict | None = None,
                   world_url: str | None = None,
                   base_url: str | None = None,
                   agent: str | None = None) -> tuple[int, dict]:
    """Client-auth + transport + single 401-retry (umwelt #495/#646) wrapper
    around `_http_request_once`. `agent`, when given, acquires a bearer
    token via client_auth and retries exactly once on a 401 of a
    bearer-authed request (invalidating the cached session first) — same
    behavior as tools/uw's call(). `agent=None` (the render-gateway call
    sites) sends no auth header at all, unchanged from before.

    `base_url` is the general override (defaults to `world_url or
    WORLD_URL`); it exists so this same low-level helper can also target
    the render gateway (RENDER_URL) rather than the world, without a
    parallel copy of the GET/POST/error-handling plumbing. `world_url`
    stays as the pre-existing param name for the world-facing callers
    below (fetch_agent_places/fetch_look/fetch_place_view/fetch_look_at)
    so their call sites are unchanged."""
    if base_url is None and world_url is None:
        # umwelt#699: only resolve WORLD_URL when actually falling back to
        # it -- a caller passing base_url= or world_url= explicitly never
        # triggers resolution (round-2 reviewer-fairy finding on PR#710:
        # this guard was missing here, so calling any of the four
        # world-facing fetch_* functions directly with no explicit URL and
        # no prior run_scan() call raised a bare AttributeError on
        # WORLD_URL still being None, instead of the intended WorldUrlError).
        _ensure_urls_resolved()
    base = (base_url or world_url or WORLD_URL).rstrip("/")
    status: int
    body: dict
    for attempt in range(2):  # initial + one retry on a 401 of a bearer req
        auth_headers: dict[str, str] = {}
        if agent:
            try:
                auth_headers = _client_auth.get_auth_header(base, agent, _SESSION)
            except _client_auth.AuthFailure as e:
                raise BackfillScanError(
                    f"authentication failed for {agent!r}: {e}") from e
        status, body = _http_request_once(method, path, params=params,
                                          payload=payload, base=base,
                                          auth_headers=auth_headers)
        if status == 401 and attempt == 0 and auth_headers:
            _SESSION.invalidate()
            continue
        break
    return status, body


def _http_status(path: str, *, base_url: str) -> int:
    """HEAD {base_url}{path} -> status code ONLY, no body parsing at all.

    A dedicated minimal helper, deliberately NOT built on top of
    `_http_request` — that function's success path does `json.loads(body)`
    unconditionally, which would crash the moment it's pointed at a route
    serving binary content (`GET /sprite/<key>` serves a PNG, not JSON).
    Flask auto-supports HEAD on a route registered for GET (no server-side
    change needed), so a HEAD here gets the same 200/404 status a GET would
    without transferring the PNG body at all — the cheapest possible
    existence check.

    Raises BackfillScanError on a transport failure, mirroring
    `_http_request`'s unreachable behavior — the one caller (`sprite_cached`)
    catches it and degrades to `False` rather than propagating, since by
    the time this is called the gateway is already known-up (the manifest
    fetch in `fetch_library_manifest` succeeded first)."""
    base = base_url.rstrip("/")
    url = f"{base}{path}"
    req = urllib.request.Request(url, method="HEAD",
                                 headers=_engine_api.client_headers())  # umwelt#1105
    try:
        with _client_auth.urlopen_with_cert_fallback(req, 30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BackfillScanError(f"cannot reach {base}: {e}") from e


def fetch_agent_places(agent: str, world_url: str | None = None) -> dict:
    """GET /umwelt/places?agent=<agent> — the ONE self-standpoint bootstrap
    call this walk is rooted in (Kepler's #164 ruling: compose with the
    world model, never read the flat `db.list_places` god-view). FATAL on
    failure — without this there is no self-standpoint to walk from at
    all, so this is the one call allowed to abort the whole scan rather
    than degrade per-item."""
    status, body = _http_request("GET", "/umwelt/places",
                                  params={"agent": agent}, world_url=world_url,
                                  agent=agent)
    if status >= 400:
        raise BackfillScanError(
            f"GET /umwelt/places?agent={agent} failed ({status}): "
            f"{body.get('error', body)}")
    return body


def fetch_look(agent: str, world_url: str | None = None) -> dict | None:
    """POST /umwelt/look {agent} — renders the CALLER's own current place
    (no `place` param exists on this route — see module docstring
    correction #1). Used ONLY for the walk's start place. Returns None
    (logged) on any failure — per-place graceful degrade, not fatal."""
    try:
        status, body = _http_request("POST", "/umwelt/look",
                                      payload={"agent": agent}, world_url=world_url,
                                      agent=agent)
    except BackfillScanError as e:
        _log(str(e))
        return None
    if status >= 400:
        _log(f"POST /umwelt/look {{agent: {agent!r}}} failed ({status}): "
             f"{body.get('error', body)}")
        return None
    return body


def fetch_place_view(place_id: str, world_url: str | None = None) -> dict | None:
    """GET /umwelt/place_view?place=<id> — the read-only arbitrary-place
    primitive (anonymous; public-only, see module docstring correction
    #1). Used for every visited place other than the walk's start. Returns
    None (logged) on any failure — a private place invisible to this
    anonymous read is exactly the same shape of failure as a genuine 404 or
    network error, and is handled identically (log + skip, walk
    continues)."""
    try:
        status, body = _http_request("GET", "/umwelt/place_view",
                                      params={"place": place_id}, world_url=world_url)
    except BackfillScanError as e:
        _log(str(e))
        return None
    if status >= 400:
        _log(f"GET /umwelt/place_view?place={place_id} failed ({status}): "
             f"{body.get('error', body)}")
        return None
    return body


def fetch_look_at(thing_id: str, world_url: str | None = None,
                   agent: str | None = None) -> dict | None:
    """POST /umwelt/look_at {thing} — the per-thing detail read that
    carries `metadata` (bulk look/place_view's `things` list does not, see
    module docstring correction #2). Returns None (logged) on failure.
    `agent`, when given, acquires a client-auth bearer token for the
    request (umwelt #495/#646) — `/umwelt/look_at` doesn't take an `agent`
    body param itself (it resolves the caller from the token/declared
    identity server-side), but the token still needs the identity to
    request it for."""
    try:
        status, body = _http_request("POST", "/umwelt/look_at",
                                      payload={"thing": thing_id}, world_url=world_url,
                                      agent=agent)
    except BackfillScanError as e:
        _log(str(e))
        return None
    if status >= 400:
        _log(f"POST /umwelt/look_at {{thing: {thing_id!r}}} failed ({status}): "
             f"{body.get('error', body)}")
        return None
    return body


def fetch_library_manifest(render_url: str | None = None) -> dict | None:
    """GET {gateway}/library — the ENTIRE art-library manifest, deliberately
    with NO `status` filter. `library.resolve`'s grandfather rule treats a
    status-less legacy entry as live (back-compat) — filtering by status
    here would silently hide those legacy entries from that rule, which is
    exactly the kind of quiet behavior-narrowing this fix is trying to
    eliminate, not reintroduce. Returns `{"entries": [...]}`, the EXACT
    shape `library.resolve`/`compute_missing_lanes` already consume (no
    reshaping needed — this is the whole point of feeding the real
    `library.resolve` unchanged).

    Returns `None` (logged) if the gateway is unreachable OR responds
    >=400 — deliberately NOT swallowed into an empty `{"entries": []}`.
    `None` here is a DIFFERENT signal than "manifest with zero entries":
    it means "art-status is UNAVAILABLE for this whole run", and the one
    caller (`run_scan`) treats it as FATAL (raises `BackfillScanError`)
    rather than per-item-skip — the fix for the silent-`REAL_DURABLE: 0`
    incident this module's docstring describes above."""
    if render_url is None:
        _ensure_urls_resolved()  # umwelt#699 -- see _http_request's own guard
    base = render_url or RENDER_URL
    try:
        status, body = _http_request("GET", "/library", base_url=base)
    except BackfillScanError as e:
        _log(str(e))
        return None
    if status >= 400:
        _log(f"GET {base}/library failed ({status}): {body.get('error', body)}")
        return None
    return {"entries": body.get("entries", [])}


def sprite_cached(key: str, render_url: str | None = None) -> bool:
    """Existence check for a cached sprite PNG: a HEAD request to
    `{gateway}/sprite/{key}` (200 -> True, 404 -> False). Uses
    `_http_status`, NOT `_http_request` — `/sprite/<key>` serves a BINARY
    PNG on a GET, and `_http_request`'s `json.loads(body)` would crash on
    those bytes; `_http_status` never reads/parses a body at all, so it's
    safe against any content type.

    Unreachable -> treated as `False` (not cached), NOT fatal: by the time
    this is called, `fetch_library_manifest` has already succeeded against
    this SAME gateway (see `run_scan`), so a failure here is a one-off
    transient blip against an otherwise-known-up gateway, not the
    gateway-down condition `fetch_library_manifest`'s `None` return
    signals. A cache-miss is also a perfectly ordinary, expected outcome
    (most gaps have no cached PNG at all yet) — collapsing "miss" and
    "transient error" into the same `False` here is a deliberate,
    low-stakes simplification, unlike the manifest axis where the same
    collapse would be dishonest."""
    if render_url is None:
        _ensure_urls_resolved()  # umwelt#699 -- see _http_request's own guard
    base = render_url or RENDER_URL
    try:
        status = _http_status(f"/sprite/{key}", base_url=base)
    except BackfillScanError as e:
        _log(str(e))
        return False
    return status == 200


# --- class-proposal (Luria's ratified rules — VERBATIM, do not modify) ------
#
# Word-boundary tokenization is load-bearing: a substring `in` check would
# mis-match "de**sign**" -> sign, "as**sign**ment" -> sign — the exact bug
# this fixes.

SIGN_WORDS = {"sign", "notice", "poster"}
BOOK_WORDS = {"book", "notebook", "tome", "logbook", "journal", "handbook",
              "manual", "guide", "spec", "schematic", "proposal", "design",
              "note", "log", "report", "runbook"}


def propose_class(name: str, kind: str, description: str) -> str | None:
    n = name.lower()
    tokens = set(re.findall(r"[a-z0-9']+", f"{name} {description}".lower()))
    if "well" in tokens:
        return "well"            # name-only, any kind (echo well, question well)
    if "sofa" in tokens or "沙发" in f"{name}{description}":
        return "chair"
    if kind == "readable":
        if tokens & SIGN_WORDS:
            return "sign"
        if tokens & BOOK_WORDS:
            return "book"
        return None               # unrecognized readable -> Tier-A generic
    return None                   # everything else -> Tier-A generic


# --- art-status (two honest axes, BOTH now HTTP against the gateway) -------

def classify_render_spec(render_spec: dict | None, manifest: dict,
                          render_url: str | None = None) -> tuple[str, bool]:
    """Returns (status, renders_today).

    status is one of:
      - "NO_ART" — no render_spec with a non-empty class.
      - "REAL_DURABLE" — a live manifest entry exists (`library.resolve`).
      - "UNMANIFESTED" — a render_spec exists but isn't durable (may or may
        not have a cached sprite TODAY — see `renders_today`).

    `manifest` is the HTTP-fetched `{"entries": [...]}` dict returned by
    `fetch_library_manifest` — `library.resolve` is called UNCHANGED
    against it (no reimplementation, grandfather rule preserved verbatim).
    `renders_today` is now a `sprite_cached` HTTP existence check against
    the gateway, not a local-disk `.exists()` — see the module docstring's
    off-host HTTP fix note for why the local-disk version was wrong.
    """
    if not isinstance(render_spec, dict) or not render_spec.get("class"):
        return "NO_ART", False
    key = spec_key(render_spec)
    renders_today = sprite_cached(key, render_url=render_url)
    durable = library.resolve(render_spec, manifest) is not None
    if durable:
        return "REAL_DURABLE", renders_today
    return "UNMANIFESTED", renders_today


def compute_missing_lanes(lane_tiles: dict | None, manifest: dict,
                           render_url: str | None = None) -> list[str]:
    """A place's `metadata.lane_tiles` is `{sky?, land?, road?}`, each a
    render_spec. For each of the three lanes: absent/empty render_spec ->
    bare lane name (plainly missing); present-but-not-durable -> lane name
    + a `(procedural)` marker (no cached sprite yet — would render
    on-the-fly) or `(unmanifested)` marker (cached sprite exists but no
    live manifest entry) depending on `renders_today`. A durable lane is
    not included at all."""
    lane_tiles = lane_tiles or {}
    missing: list[str] = []
    for lane in _LANES:
        spec = lane_tiles.get(lane)
        if not isinstance(spec, dict) or not spec.get("class"):
            missing.append(lane)
            continue
        status, renders_today = classify_render_spec(
            spec, manifest, render_url=render_url)
        if status == "REAL_DURABLE":
            continue
        marker = "unmanifested" if renders_today else "procedural"
        missing.append(f"{lane} ({marker})")
    return missing


# --- the walk ----------------------------------------------------------------

def run_scan(agent: str | None = None, world_url: str | None = None,
             render_url: str | None = None) -> dict:
    """Self-standpoint BFS over the world, read-only, never mutating.

    Bootstrap: ONE call to `GET /umwelt/places?agent=<agent>` — the
    self-standpoint seed (Kepler's #164 ruling: compose with the world
    model, never the flat god-view). Seeds the frontier with the agent's
    current place, its children (+ each child's own lateral links), its
    own lateral links, its parent (+ parent's own links), and its anchors
    (+ each anchor's own links) — every neighbor field the bootstrap
    response actually carries at depth 0/1. `children_skipped > 0` here is
    accumulated into `places_skipped_by_cap`, never silently dropped.

    Beyond that one bootstrap call, `/umwelt/places?agent=` cannot be
    re-queried for an arbitrary place (it always resolves to the CALLER's
    actual avatar position, not a parameterized place — see module
    docstring correction #1). So further BFS expansion uses each
    subsequently-visited place's own `reachable` (`{id, label, relation}`,
    relation in child/link/parent) + `parent` fields, returned by every
    `/umwelt/look`/`/umwelt/place_view` response — still fully
    world-model-composed (never `db.list_places`), just sourced from a
    different (but equally privacy-respecting) read primitive than the one
    bootstrap call.

    The walk's START place is read via `/umwelt/look` (the only route that
    can render the caller's own current place, private or not); every
    OTHER visited place is read via `/umwelt/place_view` (anonymous,
    public-only — a private place beyond the bootstrap neighborhood 404s
    there and is skipped, logged, same as any other per-place failure).

    Dedups visited place-ids (the graph has cycles + anchors repeat).
    Never mutates the world: every call here is a GET or a read-only POST
    (`look`/`place_view`/`look_at`) — none of them touch avatar position or
    read cursors.

    Art-status manifest fetch (the OTHER fatal call, alongside the
    bootstrap standpoint call above): `fetch_library_manifest(render_url)`
    is called ONCE, up front, against the render gateway (independent
    process from the world at `world_url` — it can be down while the world
    is up, or vice versa). A `None` return means BOTH art-status axes are
    unavailable for the ENTIRE walk, not one place/thing — reporting a
    worklist under that condition would silently show `REAL_DURABLE: 0`
    as if every gap were real, so this raises `BackfillScanError` instead
    of proceeding. This is the off-host HTTP fix's honesty guarantee — see
    the module docstring's fix note.
    """
    agent = agent or AGENT
    if not agent:
        raise BackfillScanError(
            "no agent identity — set ~/.umwelt/config.json's \"agent\" key "
            "or pass agent= explicitly")
    if world_url is None or render_url is None:
        # umwelt#699: only resolve the module-level fallbacks when actually
        # falling back to them -- a caller passing both explicitly (e.g. a
        # test) never triggers resolution at all. See _ensure_urls_resolved.
        _ensure_urls_resolved()
    world_url = world_url or WORLD_URL
    render_url = render_url or RENDER_URL

    manifest = fetch_library_manifest(render_url)
    if manifest is None:
        raise BackfillScanError(
            f"cannot reach the render gateway at {render_url} — art-status "
            f"axes unavailable; re-run when the gateway is reachable")

    bootstrap = fetch_agent_places(agent, world_url=world_url)
    current_node = bootstrap.get("current")
    if current_node is None:
        raise BackfillScanError(
            f"agent {agent!r}'s current place is not visible to itself — "
            "cannot establish a self-standpoint to walk from")
    current_id = current_node["id"]
    places_skipped_by_cap = bootstrap.get("children_skipped", 0) or 0

    visited: set[str] = set()
    queued: set[str] = set()
    queue: deque[str] = deque()
    # Places enqueued from a privacy-respecting bootstrap/reachable field but
    # UNREADABLE on visit (a 404) — most importantly the running agent's OWN
    # non-current private Posts: they are visible to the agent in the place
    # tree, but the only arbitrary-place read primitive (`/umwelt/place_view`)
    # is ANONYMOUS (public-only), so a private place the agent isn't currently
    # STANDING in cannot have its things read. This is an ENGINE API GAP (no
    # standpoint-aware arbitrary-place read), not a bug in this tool — tracked
    # so the coverage limitation is HONEST in the summary, not hidden as a
    # bare "places_private: 0". Owner private-Post backfill therefore needs
    # either the agent standing in their Post (making it the /look start
    # place) or that future standpoint-aware read primitive.
    unreadable: set[str] = set()

    def enqueue(place_id: str | None) -> None:
        if not place_id or place_id in visited or place_id in queued:
            return
        queued.add(place_id)
        queue.append(place_id)

    def enqueue_node_and_links(node: dict | None) -> None:
        if node is None:
            return
        enqueue(node.get("id"))
        for link in node.get("links", []) or []:
            enqueue(link.get("dst_id"))

    enqueue_node_and_links(current_node)
    for child in current_node.get("children", []) or []:
        enqueue_node_and_links(child)
    enqueue_node_and_links(bootstrap.get("parent"))
    for anchor in bootstrap.get("anchors", []) or []:
        enqueue_node_and_links(anchor)

    things_out: list[dict[str, Any]] = []
    things_by_status: dict[str, int] = {"NO_ART": 0, "UNMANIFESTED": 0,
                                         "REAL_DURABLE": 0}
    avatars_out: dict[str, dict[str, Any]] = {}
    places_out: list[dict[str, Any]] = []

    while queue:
        place_id = queue.popleft()
        queued.discard(place_id)
        if place_id in visited:
            continue
        visited.add(place_id)

        if place_id == current_id:
            view = fetch_look(agent, world_url=world_url)
        else:
            view = fetch_place_view(place_id, world_url=world_url)
        if view is None:
            unreadable.add(place_id)  # 404/private-to-anonymous — honest count below
            continue  # already logged by the fetch_* call

        place = view.get("place") or {}
        lane_tiles = (place.get("metadata") or {}).get("lane_tiles")
        missing_lanes = compute_missing_lanes(
            lane_tiles, manifest, render_url=render_url)
        places_out.append({
            "id": place.get("id", place_id),
            "name": place.get("label"),
            "private": place.get("kind") == "private",
            "missing_lanes": missing_lanes,
        })

        for t in view.get("things", []) or []:
            if t.get("kind") == "kinddef":
                continue
            detail = fetch_look_at(t["id"], world_url=world_url, agent=agent)
            if detail is None:
                continue  # already logged
            meta = (detail.get("thing") or {}).get("metadata") or {}
            render_spec = meta.get("render_spec")
            status, renders_today = classify_render_spec(
                render_spec, manifest, render_url=render_url)
            things_by_status[status] = things_by_status.get(status, 0) + 1
            proposed_class = None
            if status == "NO_ART":
                proposed_class = propose_class(
                    t.get("name", ""), t.get("kind", ""),
                    t.get("description") or "")
            if status != "REAL_DURABLE":
                things_out.append({
                    "id": t["id"], "name": t.get("name"), "kind": t.get("kind"),
                    "place_id": place_id, "status": status,
                    "renders_today": renders_today,
                    "proposed_class": proposed_class,
                })

        for p in view.get("present", []) or []:
            avatar_id = p.get("avatar_id")
            if not avatar_id:
                continue
            avatars_out.setdefault(avatar_id, {
                "id": avatar_id, "name": p.get("name"),
                "status": "owner_self_served",
            })

        for r in view.get("reachable", []) or []:
            enqueue(r.get("id"))
        parent = view.get("parent")
        if parent:
            enqueue(parent.get("id"))

    avatars_list = list(avatars_out.values())
    summary = {
        "things_gaps": len(things_out),
        "things_by_status": things_by_status,
        "avatars_gaps": len(avatars_list),
        "places_gaps": sum(1 for p in places_out if p["missing_lanes"]),
        "places_private": sum(1 for p in places_out if p["private"]),
        "places_skipped_by_cap": places_skipped_by_cap,
        "places_unreadable": len(unreadable),
        # Renamed from `serve_dir_manifest_entries` (the pre-fix local-disk
        # read) — the manifest is now the render gateway's HTTP `/library`
        # response, not a local `SERVE_DIR` file, so the old name would be
        # actively misleading.
        "library_manifest_entries": len(manifest.get("entries", [])),
    }
    return {
        "things": things_out,
        "avatars": avatars_list,
        "places": places_out,
        "summary": summary,
    }


# --- CLI ----------------------------------------------------------------

def _print_summary(worklist: dict) -> None:
    s = worklist["summary"]
    print(f"things scanned: {sum(s['things_by_status'].values())} "
          f"({s['things_by_status']})")
    print(f"things gaps (NO_ART + UNMANIFESTED): {s['things_gaps']}")
    print(f"avatars noted (owner self-served, not class-proposed): "
          f"{s['avatars_gaps']}")
    print(f"places visited: {len(worklist['places'])} "
          f"(gaps: {s['places_gaps']}, private: {s['places_private']})")
    print(f"library manifest entries: {s['library_manifest_entries']}")
    if s.get("places_unreadable"):
        print(f"NOTE: {s['places_unreadable']} place(s) were visible in the "
              f"place tree but UNREADABLE on visit (anonymous place_view is "
              f"public-only) — incl. this agent's own non-current private "
              f"Post(s). Owner private-Post backfill needs the agent standing "
              f"in the Post, or a standpoint-aware arbitrary-place read API.")
    if s["places_skipped_by_cap"]:
        print(f"WARNING: {s['places_skipped_by_cap']} child place(s) were "
              f"skipped by the display-bound cap on the walk's start place "
              f"and not further expanded from — see places?agent="
              f"'s children_skipped field.")


_USAGE = (
    "usage: backfill_scan.py [WORKLIST_PATH]\n\n"
    "Walk the world from the configured agent's own standpoint\n"
    "(~/.umwelt/config.json: world_url + agent) and write an art-gap\n"
    f"worklist JSON to WORKLIST_PATH (default {_DEFAULT_WORKLIST_PATH}).\n"
    "Read-only: never mutates world state.")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Special-case -h/--help BEFORE treating argv[0] as a path — else
    # `--help` would silently run a full live scan and write a file named
    # `--help` (reviewer-fairy footgun catch, PR #329).
    if argv and argv[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    out_path = Path(argv[0]) if argv else Path(_DEFAULT_WORKLIST_PATH)

    try:
        worklist = run_scan()
    except BackfillScanError as e:
        _log(f"FATAL: {e}")
        return 1

    out_path.write_text(json.dumps(worklist, indent=2), encoding="utf-8")
    _print_summary(worklist)
    print(f"worklist written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
