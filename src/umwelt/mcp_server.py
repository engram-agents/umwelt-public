"""Umwelt MCP server — walk the world as tools.

A thin MCP client over the umwelt HTTP world server (default localhost:5003).
The agent identity is implicit (read from ~/.engram/config.json) so you just
`move_to("place_square")`, not "I, <name>, move to...". Outputs are prose first
(the world speaking to you), with the structured data underneath — the
"natural to agents" surface (Lei, 2026-07-01).

Run (registered as an MCP server in Claude Code):
    python -m umwelt.mcp_server
Env:
    UMWELT_URL   world server base url (default: see client_auth.resolve_world_url)
    UMWELT_AGENT override the agent identity (default: config agent_name)
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import _ids
from . import avatar_status as _avatar_status
from . import client_auth as _client_auth
from . import mentions as _mentions

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP  # type: ignore

# umwelt#699: resolved LAZILY (first real request), not at import time --
# see _resolve_world_url() just below the request helpers. `None` here is a
# not-yet-resolved sentinel, never a usable value.
WORLD_URL: str | None = None

# Process-lifetime session token cache (umwelt #495). In-memory only — never
# persisted (a cached bearer token is itself a credential). The server's 401
# is the validity signal, so a 401 response invalidates this and the transport
# retries once with a freshly acquired token (see _request).
_SESSION = _client_auth.AgentSession()

# umwelt#857 D5 (whats_new): the agent-side cursor lives in the plugin, NOT
# the world (Lei's ruling -- the world route is stateless). Same
# atomic-write-via-tmp-then-os.replace discipline as
# plugin/tools/umwelt-bell.py's save_cursors(), and the same "no UMWELT_HOME
# env-var pattern exists anywhere in this codebase today" finding that
# tool's own DEFAULT_CURSOR_FILE (`~/.umwelt/bell-cursors.json`) already
# lives by -- this constant is a plain module attribute (not read from any
# env var) so tests can monkeypatch it directly, same as they monkeypatch
# `_post` above.
_WHATS_NEW_CURSOR_FILE = Path.home() / ".umwelt" / "whats-new-cursor.json"

# umwelt#857 D5: bounds the RENDERED text, not the underlying data -- the
# world already caps headlines per place (per_place, clamped 1..10); this
# is the second, independent bound on the MCP tool's own prose output.
# umwelt#862 (D8) will replace this literal with the shared budget renderer
# once it lands; until then this is a plain named constant.
_WHATS_NEW_RENDER_CAP = 6000


class WorldUnreachable(Exception):
    """The world server could not be reached (connection-level failure)."""


def _agent() -> str:
    env = os.environ.get("UMWELT_AGENT", "").strip()
    if env:
        return env
    try:
        with open(os.path.expanduser("~/.engram/config.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("agent_name", "agent"))
    except (OSError, json.JSONDecodeError):
        return "agent"


def _self_lineage() -> str | None:
    """This seat's ``self_lineage`` from ``~/.engram/config.json`` (the same
    file ``_agent()`` reads), or None when it is unknown (umwelt#861 D9).

    Valid only in ``provider:family`` shape: a str, lowercase, exactly one
    colon, both parts non-empty. Missing / unreadable / malformed is unknown;
    the client never invents a lineage."""
    try:
        with open(os.path.expanduser("~/.engram/config.json"), encoding="utf-8") as fh:
            value = json.load(fh).get("self_lineage")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(value, str) or value != value.lower():
        return None
    provider, sep, family = value.partition(":")
    if not sep or not provider or not family or ":" in family:
        return None
    return value


# Scope (umwelt#861 D9): this stamp covers AGENT-SEAT senders only. Deliberately
# NOT stamped: src/umwelt/mud.py (WorldClient.send_crossing, the interactive
# human MUD client) and src/umwelt/webrender/gateway.py (posts as the browser
# user's avatar) -- human senders have no self_lineage. The server half of #861
# is the structural fix for those paths.
def _stamp_sender_lineage(req: dict, caller: str = "") -> str | None:
    """The one stamping point for every crossing send (umwelt#861 D9).

    Known lineage: ``req["sender_lineage"]`` is set to it; a differing
    ``caller`` value is REFUSED (returns the error text, nothing to send --
    a crossing is append-only, so a warning after the write is too late).
    Unknown lineage: a ``caller`` value passes through unchanged, and with
    none the field stays absent. Returns None on success."""
    known = _self_lineage()
    if known is None:
        if caller:
            req["sender_lineage"] = caller
        return None
    if caller and caller != known:
        return (f"⚠️ sender_lineage {caller!r} does not match this seat's "
                f"self_lineage {known!r}; omit it, the tool stamps it")
    req["sender_lineage"] = known
    return None


def _with_status(status: int, body: str) -> dict[str, Any]:
    """Merge an HTTP status with a JSON-object response body (umwelt#887).

    A body that parses to anything but an object (array, null, number,
    string, bool) cannot be merged; it becomes a bounded one-line error
    response instead of a TypeError out of every tool. A 2xx status is
    reported as a synthetic 502 (bad gateway: the world answered, but not in
    its protocol) so `_err` surfaces it -- left at 2xx, tools would read the
    empty body as a genuine "nothing here". The body's content is
    deliberately not echoed. An unparseable (non-JSON) body still raises
    ValueError (or RecursionError for deeply nested JSON) from here; what
    happens next depends on the caller's branch in
    `_request_once`: on a 2xx the ValueError is caught by its sibling
    `except (OSError, ValueError)` and becomes WorldUnreachable (as before);
    on an HTTP error status the call sits inside the `except HTTPError`
    handler, so that branch catches it itself and returns the original status
    with a fixed non-JSON-error message."""
    parsed = json.loads(body) if body else {}
    if not isinstance(parsed, dict):
        return {"_status": status if status >= 400 else 502,
                "error": "world returned a non-object JSON response"}
    return {"_status": status, **parsed}


def _request_once(path: str, payload: dict | None, timeout: int,
                  auth_headers: dict[str, str]) -> dict[str, Any]:
    """Single HTTP round-trip via stdlib urllib. Returns {_status, ...body}.

    Raises WorldUnreachable on a connection-level failure; an HTTP error
    *response* (4xx/5xx with a body) is returned normally so callers can
    surface the world's own error message. ``auth_headers`` (the
    ``Authorization: Bearer ...`` dict from client_auth, possibly empty) is
    merged into the request headers — empty dict = today's declared behavior.

    A DNS-resolution failure (socket.gaierror) gets one short retry: name
    resolution happens before any bytes reach the server, so the request
    was never sent and a retry cannot double an action there. This is the
    flaky bit for off-host callers resolving a `.local` mDNS name across
    the network — a cold/first lookup occasionally loses the multicast
    race, and the very next resolution then succeeds.
    """
    url = f"{WORLD_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    headers.update(auth_headers)
    req = urllib.request.Request(
        url, data=data, method="POST" if payload is not None else "GET",
        headers=headers,
    )
    for attempt in range(2):
        try:
            with _client_auth.urlopen_with_cert_fallback(req, timeout) as r:
                body = r.read().decode()
                return _with_status(r.status, body)
        except urllib.error.HTTPError as e:  # 4xx/5xx with a body
            # This handler is NOT covered by the sibling `except ValueError`
            # below (an exception raised inside an except clause is not
            # handled by the same try's other clauses), so guard it here: a
            # non-JSON error body (proxy 502/504 HTML page, truncated JSON)
            # keeps its ORIGINAL status with a fixed, body-free message.
            try:
                body = e.read().decode()
                return _with_status(e.code, body)
            except (ValueError, RecursionError):
                # RecursionError (deeply nested JSON) is a RuntimeError, not
                # a ValueError -- named explicitly (umwelt#887).
                return {"_status": e.code,
                        "error": "world returned a non-JSON error response"}
        except urllib.error.URLError as e:
            if attempt == 0 and isinstance(e.reason, socket.gaierror):
                time.sleep(0.5)
                continue
            raise WorldUnreachable(str(e)) from e
        except (OSError, ValueError, RecursionError) as e:
            # RecursionError: a deeply nested 2xx body (umwelt#887).
            raise WorldUnreachable(str(e)) from e
    raise AssertionError("unreachable")  # loop always returns or raises above


def _resolve_world_url() -> str:
    """Resolve + cache WORLD_URL on first actual use -- deliberately NOT at
    module import time (umwelt#699, reviewer-fairy finding on PR#710).

    `resolve_world_url()` now RAISES on a malformed config or a plaintext
    non-loopback value (umwelt#699 §2/§3) where it used to silently fall
    through. Resolving it eagerly at import made every importer of this
    module -- tests, a tool that merely imports `avatar_status` alongside
    it, `python -m umwelt.mcp_server --help` -- crash on a broken config,
    not just an actual attempt to reach the world. Deferring to the first
    real request means the loud failure still happens (never silently
    downgraded), just at the moment the server would have needed the value
    anyway, instead of as a side effect of merely being imported.
    """
    global WORLD_URL
    if WORLD_URL is None:
        WORLD_URL = _client_auth.resolve_world_url().rstrip("/")
    return WORLD_URL


def _request(path: str, payload: dict | None, timeout: int) -> dict[str, Any]:
    """HTTP round-trip with client-auth + 401-retry-once (umwelt #495).

    Acquires an ``Authorization`` header from ``client_auth`` (key → verified
    token; no key → declared fallback while the backfill window is open) and
    merges it into the request. On a 401 the cached token is invalidated and
    the request retried exactly once with a freshly acquired token — the
    server is the authority on token validity (six checks the client cannot
    know), so the client never parses ``expires_at`` and trusts the 401.

    The header is ADDITIVE — the agent identity in the request body is NOT
    dropped when a token is present. The server still requires ``agent`` in
    the body even with a valid token (400 'agent is required' otherwise), and
    ``resolve_actor`` lets the token OVERRIDE the declared param rather than
    replace it. Do not design toward omitting the body param. (Ariadne,
    end-to-end finding, 2026-08-18 — measured on alpha against 1d5b90f.)

    A key-present-but-auth-fails condition raises ``AuthFailure`` (fail
    closed — do not silently fall back to declared); caught here as a
    401-shaped error so callers surface it. No-key + fallback-disabled also
    fails closed. (Mira security ruling, 2026-08-18.)
    """
    world_url = _resolve_world_url()
    for attempt in range(2):  # initial + one retry on a 401 of a bearer req
        try:
            auth_headers = _client_auth.get_auth_header(world_url, _agent(), _SESSION)
        except _client_auth.AuthFailure as e:
            return {"_status": 401, "error": f"authentication failed: {e}"}
        resp = _request_once(path, payload, timeout, auth_headers)
        # Retry once only if we sent a bearer token (not a declared-fallback
        # request) and the server rejected it — a fresh token may clear it.
        if resp.get("_status") == 401 and attempt == 0 and auth_headers:
            _SESSION.invalidate()
            continue
        return resp
    return resp  # pragma: no cover (loop always returns above)


def _post(path: str, payload: dict) -> dict[str, Any]:
    return _request(path, payload, timeout=60)


def _get(path: str) -> dict[str, Any]:
    return _request(path, None, timeout=30)


def _informational_tail(resp: dict) -> str:
    """umwelt#982 / umwelt#989: the engine's informational refusals carry a
    `status` and structured detail: whose place a thing is in
    (`in_private_place`), who can restore a binned thing (`binned`), where
    you ARE standing when a create_thing is not where you stand
    (`not_standing_here`), and that you stand in a place you may not enter
    (`may_not_enter`, umwelt#1011). Render that detail as a sentence so the agent
    reading the refusal learns whom to ask / what to do, not only that the
    world said no. Never raises on a malformed body: it is best-effort text
    appended to an `error` that is already complete on its own."""
    status = resp.get("status")
    place = resp.get("place") if isinstance(resp.get("place"), dict) else {}
    try:
        if status == "in_private_place":
            owner = place.get("owner")
            return (f" (place {place.get('id')!r}"
                    + (f", owned by {owner} — ask them" if owner else "")
                    + ")")
        if status == "binned":
            who = resp.get("can_restore") or []
            # (a list comprehension, not `map`: this module defines an MCP
            # tool named `map`, which shadows the builtin at module scope)
            names = ", ".join([str(w) for w in who]) or "its author"
            return (f" (binned {resp.get('binned_at')}; "
                    f"can be restored by: {names})")
        if status == "may_not_enter":
            # The server's `error` already carries "you are standing in X but
            # may not enter it, so you may not create there" (umwelt#1011);
            # repeating it printed the sentence twice. Add only what the error
            # lacks: who owns the place, and that you should ask them.
            owner = place.get("owner")
            return f" (it is owned by {owner} — ask them)" if owner else ""
        if status == "not_standing_here":
            here = resp.get("standing_at") if isinstance(
                resp.get("standing_at"), dict) else {}
            return (f" (you are standing in {here.get('id')!r}; "
                    f"things are created where you stand)")
    except (TypeError, ValueError):
        pass
    return ""


def _err(resp: dict) -> str | None:
    if resp.get("_status", 200) >= 400:
        return (f"⚠️ the world said no ({resp['_status']}): "
                f"{resp.get('error', 'unknown')}{_informational_tail(resp)}")
    return None


def _parse_payload(payload_json: str, required: frozenset[str] = frozenset(),
                    allowed: frozenset[str] = frozenset()) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a tool's payload_json string into validated kwargs.

    Mirrors ENGRAM server.py's _parse_payload mechanism (JSON-parse, dict-type
    check, unknown-field rejection, missing-required rejection) but renders
    errors in umwelt's existing prose convention (⚠️ ...) rather than ENGRAM's
    raw JSON error envelope — matching the house style already set by forge's
    source_json/metadata_json JSONDecodeError handling and _err()'s "world
    said no" prose.

    Returns (params, None) on success, or (None, <prose error string>) on any
    validation failure. Every failure names the concrete allowed/required set
    so the caller doesn't have to guess — this is what makes the
    schema-discovery class of error survivable; treat as load-bearing.
    """
    try:
        params = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError) as e:
        return None, f"⚠️ payload_json must be valid JSON: {e}"
    except RecursionError:
        # Deeply nested JSON (umwelt#887): a RuntimeError, not a ValueError.
        return None, "⚠️ payload_json is nested too deeply"
    except ValueError:
        # Not a JSONDecodeError: Python's int-string conversion limit
        # (~4,300 digits) surfaces as a bare ValueError (umwelt#887). Fixed
        # text -- never echo the offending literal back.
        return None, ("⚠️ payload_json contains an integer literal too large "
                      "to parse")
    if not isinstance(params, dict):
        return None, "⚠️ payload_json must be a JSON object"
    unknown = set(params) - allowed
    if unknown:
        return None, (f"⚠️ unknown field(s) in payload_json: {sorted(unknown)}. "
                      f"Allowed: {sorted(allowed)}")
    missing = required - set(params)
    if missing:
        return None, (f"⚠️ payload_json missing required field(s): "
                      f"{sorted(missing)}. Required: {sorted(required)}")
    return params, None


def _int_param_error(field: str, value: Any, *, non_negative: bool) -> str | None:
    """One-line error unless `value` is a non-bool int (>= 0 when
    `non_negative`), else None -- the shape `hear()` uses for `max_chars`
    (umwelt#887). Never echoes the value, only its type name."""
    if isinstance(value, bool) or not isinstance(value, int):
        return (f"⚠️ {field} must be an integer, got {type(value).__name__} "
                f"— omit it for the default")
    if non_negative and value < 0:
        return f"⚠️ {field} must be an integer >= 0 — omit it for the default"
    return None


mcp = FastMCP("umwelt")


def _marker_suffix(marker: str | None) -> str:
    """umwelt#543: `" {marker} "` when a marker string is present (the
    normal case — every real server response carries one now), `" "` (no
    stray double-space) when absent — a place-shaped dict predating this
    field, or a hand-built test fixture, must not gain a widowed space.

    Byte-identical to mud.py's own `_marker_suffix` (reviewer-fairy catch
    on PR#545: an earlier version of this docstring justified the
    duplication by claiming "no cross-import between the two independent
    renderers" — false, this module and mud.py already cross-import other
    shared helpers, e.g. `_ids.canonical_agent`. The real reason this
    3-line pure-string-formatting helper is duplicated rather than
    factored into a shared module is simplicity, not a structural
    constraint — a genuine architectural boundary DOES exist, but it's the
    one between `db.py` and this file's process (see `format_place_marker`'s
    own docstring), not between mud.py and mcp_server.py themselves. Worth
    revisiting if this helper grows past trivial."""
    return f" {marker} " if marker else " "


def _count_clause(place: dict) -> str:
    """umwelt#543 (Ariadne's security-lane must-fix on PR#545): the
    interior count clause (" — N things, N unread") is CONTENT, not
    existence — Lei's scope was "name+owner+grants, no content." A locked
    place's `thing_count`/`unread` are the safe placeholder `0`
    (`db._place_tree_node` never queries the real numbers when
    `interior_visible` is False), but unconditionally printing "0 things,
    0 unread" asserts a FALSE fact — a viewer can't distinguish that from
    a genuinely empty public place. `interior_visible` is carried on every
    node precisely so a renderer can tell the difference; this omits the
    clause entirely when it's False — "The Vault [private][owner: tester]
    [place_vault]" says nothing about contents, rather than saying
    something untrue about them.

    Shared by `where_can_i_go` and `_render_map_node` (the review round's
    own catch: `_render_map_node` was the one render surface #543/#545
    missed end to end, still using the pre-#543 ad-hoc " (private)"
    literal AND this same false-content-claim bug independently — fixed
    together, one function, so the two call sites can't drift apart on
    this specific fact again)."""
    if not place.get("interior_visible", True):
        return ""
    n = place["thing_count"]
    things = f"{n} thing{'s' if n != 1 else ''}"
    return f" — {things}, {place['unread']} unread"


@mcp.tool()
def where_can_i_go() -> str:
    """Where you can go from HERE — your current place, its direct
    sub-places, its lateral links, the way up, and any standing anchors
    (umwelt #158).

    Contextual, not the flat world map: calls the place_tree render
    (`GET /umwelt/places?agent=...`) rooted at YOUR avatar's current place,
    not a flat listing of every place in the world — the full map is a
    separate surface, out of scope for this tool (see issue #160).

    The sub-places list is bounded to the busiest 15 (umwelt #159's
    presentation-layer cap — unread first, tie-broken by recent activity);
    an honest "(+N more not shown — see the map)" line appears whenever
    that cap actually trims something, never silent truncation.

    404 if you have no avatar yet — enter the world first.
    """
    try:
        resp = _get(f"/umwelt/places?agent={urllib.parse.quote(_agent())}")
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    current = resp.get("current")
    if current is None:
        return "⚠️ could not determine your current place"
    # umwelt#543: the real [state][owner: x] marker replaces the old ad-hoc
    # " (private)" literal — computed server-side, rendered verbatim.
    lines = [f"Where you are: {current['label']}"
             f"{_marker_suffix(current.get('marker'))}[{current['id']}]"
             f"{_count_clause(current)}"]
    children = current.get("children") or []
    if children:
        lines.append("You can go to:")
        for c in children:
            lines.append(f"  · {c['label']}{_marker_suffix(c.get('marker'))}"
                         f"[{c['id']}]{_count_clause(c)}")
    children_skipped = resp.get("children_skipped") or 0
    if children_skipped:
        lines.append(f"  (+{children_skipped} more not shown — see the map)")
    links = current.get("links") or []
    if links:
        lines.append("Lateral links from here:")
        for l in links:
            tag = f" ({l['link_label']})" if l.get("link_label") else ""
            lines.append(f"  · {l['dst_label']}{_marker_suffix(l.get('dst_marker'))}"
                         f"[{l['dst_id']}]{tag}")
    parent = resp.get("parent")
    if parent is not None:
        lines.append(f"The way up: {parent['label']}"
                     f"{_marker_suffix(parent.get('marker'))}[{parent['id']}]")
    anchors = resp.get("anchors") or []
    if anchors:
        anchor_list = ", ".join(
            f"{a['label']}{_marker_suffix(a.get('marker'))}[{a['id']}]"
            for a in anchors
        )
        lines.append(f"Standing anchors: {anchor_list}")
    return "\n".join(lines)


# umwelt#873 (comms D11): arrival-mode headline length. Same rule as
# db.crossing_envelope's title (umwelt#855) — first contentful line,
# stripped, cut to 80 chars as 79 + an ellipsis — but this file cannot
# import from umwelt.db (the plugin mirror ships mcp_server.py standalone),
# so the rule is duplicated here rather than shared.
_ARRIVAL_TITLE_MAX = 80


def _thing_headline(t: dict) -> str:
    """One thing's short-row line content for `look_in` / `inventory` when
    `_fit_rows` runs over budget (arrival no longer uses it, umwelt#928;
    pinned directly by test_thing_headline.py) -- everything after the ordinal:
    `{name} [{id}] — {title}`, with an optional ` ({len(description)} chars)`
    suffix when the title (the description's first contentful line, stripped
    and cut to `_ARRIVAL_TITLE_MAX` chars as 79 + `…`) differs from the
    description's own stripped form — i.e. it was cut, or there was more than
    one line. An empty or whitespace-only description renders `{name} [{id}]`
    with no dash at all (umwelt#873 invariant 1).
    """
    name, tid = t["name"], t["id"]
    description = t.get("description") or ""
    stripped = description.strip()
    if not stripped:
        return f"{name} [{tid}]"
    title = next((ln.strip() for ln in description.splitlines() if ln.strip()), "")
    if len(title) > _ARRIVAL_TITLE_MAX:
        title = title[:_ARRIVAL_TITLE_MAX - 1] + "…"
    line = f"{name} [{tid}] — {title}"
    if title != stripped:
        line += f" ({len(description)} chars)"
    return line


def _render_arrival_counts(view: dict) -> str:
    """umwelt#928: the brief arrival body — a counts line and a pointer line.
    tools/uw's `_render_arrival_counts` is the twin (a separate file across
    the plugin boundary); keep the counts-line shape identical."""
    n = len(view.get("things") or [])
    parts = [f"{n} thing{'s' if n != 1 else ''}"]
    present = view.get("present") or []
    if 1 <= len(present) <= 5:
        parts.append("present: " + ", ".join(
            a.get("name") or a.get("agent") or "someone" for a in present))
    elif present:
        parts.append(f"{len(present)} present")
    unseen = view.get("unseen_count") or 0
    if unseen:
        parts.append(f"{unseen} unseen crossing{'s' if unseen != 1 else ''}"
                     f" — 'hear' to read")
    exits = sum(1 for r in (view.get("reachable") or [])
                if r.get("relation") != "parent")
    parts.append(f"{exits} exit{'s' if exits != 1 else ''}")
    return (" · ".join(parts) + "\n"
            "look_around for the room · where_can_i_go for exits")


def _render_place_view(view: dict, arrival: bool = False) -> str:
    """The single source of truth (umwelt #214) for rendering a place's BODY
    — description, who's present, the things here, unseen crossings, and
    what's reachable — from a `_place_view`-shaped `view` dict (the shape
    shared by move_to/enter/register on the server: `place`, `things`,
    `present`, `unseen_count`, `reachable`; see server.py's `_place_view`).

    Mirrors tools/uw's `_render_place` content/order (description → present
    → things → unseen → reachable) so the MCP surface and the `uw` CLI never
    drift again — this function IS the fix for the drift `_render_place`
    already had over the old inline move_to formatter (move_to used to
    render things + reachable only, omitting present avatars and unseen
    crossings entirely). Callers supply their own lead line (e.g. "You move
    to {label}." / "You are born at {label}." / a look_around header) and
    append this body; do not call twice for the same response.

    Things are rendered `  N. {name} [{id}] — {description}` (umwelt #261
    Gap A follow-up: now matches tools/uw's numbered-list form too — the
    ordinal is the same 1-indexed position `_resolve_ordinal` resolves
    against for this same `things` list, so a bare integer ref a caller
    sees printed here is directly usable. The uuid stays visible alongside
    it, unchanged from the original design intent — callers can still act
    on either handle).

    umwelt#928: pass `arrival=True` (move_to, enter) for the BRIEF render — a
    move must be cheap. It returns ONLY a counts line (`{N} things · present
    … · {K} unseen crossings — 'hear' to read · {E} exits`) and a pointer
    line to `look_around` / `where_can_i_go`: no description, no thing
    headlines, no exit list. E excludes the parent, like the prose exit line
    below. `arrival=False` (the default; look_around's only mode) is
    byte-identical to the render this function has always produced.
    """
    if arrival:
        return _render_arrival_counts(view)
    p = view["place"]
    lines = ["", p["description"], ""]
    present = view.get("present") or []
    if present:
        tags = [a.get("name") or a.get("agent") or "someone" for a in present]
        lines.append("present: " + ", ".join(tags))
    things = view.get("things") or []
    if things:
        lines.append("Here you find:")
        for i, t in enumerate(things, 1):
            lines.append(f"  {i}. {t['name']} [{t['id']}] — {t['description']}")
    else:
        lines.append("Nothing has been left here yet.")
    unseen_count = view.get("unseen_count") or 0
    if unseen_count:
        lines.append(f"{unseen_count} unseen crossing"
                     f"{'s' if unseen_count != 1 else ''} — 'hear' to read")
    # umwelt #239 (street-view build-input, folded into #241): `reachable`
    # now carries `relation: "child"|"link"|"parent"` per entry and includes
    # the parent. This prose line stays parent-EXCLUSIVE (unchanged CLI/MCP
    # behavior) — the parent isn't surfaced here at all today, and silently
    # starting to fold it into "you can reach" would be an out-of-scope
    # text-UX change riding along with a data-shape fix. Same call gateway.py
    # makes for `/scene`'s `exits`, same reasoning.
    reachable = [r for r in (view.get("reachable") or [])
                 if r.get("relation") != "parent"]
    if reachable:
        # umwelt#543: each reachable entry now carries the real marker
        # string (server.py's _place_view) — surfaced alongside the label
        # rather than a bare name, same as every other consumer surface.
        # `.rstrip()` here, unlike every other _marker_suffix call site in
        # this file: those all follow the marker with a bracketed `[id]`,
        # so the suffix's trailing space is load-bearing (separates marker
        # from bracket). This site instead comma-joins bare "label marker"
        # pairs into a prose list with no trailing bracket to separate
        # from — an untrimmed suffix would leave a stray space before each
        # comma (Luria's colleague-pass catch on PR#545).
        labels = [f"{r.get('label') or r.get('id')}"
                  f"{_marker_suffix(r.get('marker')).rstrip()}"
                  for r in reachable]
        lines.append("from here you can reach: " + ", ".join(labels))
    return "\n".join(lines)


@mcp.tool()
def enter(payload_json: str = "{}") -> str:
    """Enter the world: birth your avatar on first call, reconnect wherever it
    last stood on every later call. Returns a brief arrival: counts of the
    things there, who's present, unseen crossings, and exits
    (umwelt #214 — shares its render with `move_to` and `look_around` via
    `_render_place_view`, the SSoT for a place's body).

    umwelt #928: the arrival reply is brief — one counts line (things, who's
    present, unseen crossings, exits) and a pointer, nothing listed. Call
    `look_around` for the room, `where_can_i_go` for exits, or `look_at` for
    one thing's complete description.

    umwelt #60: optional identity fields, applied ONLY at first-ever birth
    (same posture as the web `register` flow) — pass `type` ("human" or
    "agent"), `lineage`, and/or `avatar_description` to dress your avatar as
    it's born. Reconnecting with any of these set applies nothing; the world
    says so instead of pretending — use `customize_avatar` to change an
    avatar that already exists.

    Args:
        payload_json: JSON object (as a string) with these fields, all optional:
            type (str): "human" or "agent" — applied only at first-ever birth.
            lineage (str): applied only at first-ever birth.
            avatar_description (str): applied only at first-ever birth.
    """
    params, err = _parse_payload(
        payload_json,
        allowed=frozenset({"type", "lineage", "avatar_description"}))
    if err:
        return err
    type = params.get("type")
    lineage = params.get("lineage")
    avatar_description = params.get("avatar_description")
    payload: dict[str, Any] = {"agent": _agent()}
    if type is not None:
        payload["type"] = type
    if lineage is not None:
        payload["lineage"] = lineage
    if avatar_description is not None:
        payload["avatar_description"] = avatar_description
    try:
        resp = _post("/umwelt/enter", payload)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    p = resp["place"]
    verb = "You are born at" if resp.get("born") else "You arrive at"
    out = [f"{verb} {p['label']}.", _render_place_view(resp, arrival=True)]
    if resp.get("ignored"):
        out.append("")
        out.append(f"⚠️ ignored (avatar already exists): {', '.join(resp['ignored'])}"
                   f" — {resp.get('hint', '')}")
    return "\n".join(out)


@mcp.tool()
def move_to(payload_json: str) -> str:
    """Move to a place. Returns a brief arrival: counts of the things there,
    who's present, unseen crossings, and exits (umwelt #214 — shares its
    render with `look_around` and `enter` via `_render_place_view`, the SSoT
    for a place's body).

    umwelt #928: the arrival reply is brief — one counts line (things, who's
    present, unseen crossings, exits) and a pointer, nothing listed. Call
    `look_around` for the room, `where_can_i_go` for exits, or `look_at` for
    one thing's complete description.

    Args:
        payload_json: JSON object (as a string) with these fields:
            place (str, required): The place id to move to.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"place"}),
                                  allowed=frozenset({"place"}))
    if err:
        return err
    place = params["place"]
    try:
        resp = _post("/umwelt/move_to", {"agent": _agent(), "place": place})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    p = resp["place"]
    out = [f"You move to {p['label']}.", _render_place_view(resp, arrival=True)]
    return "\n".join(out)


@mcp.tool()
def look_around() -> str:
    """Render your current place (umwelt #214): its description, the
    things here, who's present, unseen crossings, and what's reachable —
    the full picture `tools/uw` already shows, now available as an MCP
    verb (no args, ZERO side effect on where you stand or your read cursor
    — `_fetch_place_view` renders via the read-only `POST /umwelt/look`,
    umwelt #219/#184).

    umwelt #928: unlike `move_to`/`enter` (counts only), everything here
    renders in full, with each thing's complete description — this is the
    "show me everything" verb.
    Use `look_at` to inspect one thing more closely (its checklist/board).

    404 if you have no avatar yet — `enter` the world first.
    """
    agent = _agent()
    view, err = _fetch_place_view(agent)
    if err:
        return err
    p = view["place"]
    out = [f"You are at {p['label']} [{p['id']}].", _render_place_view(view)]
    return "\n".join(out)


def _normalize_avatar(avatar: dict) -> dict:
    """MCP twin of tools/uw's _normalize_avatar (umwelt #146) — a present-
    avatar summary (`{"agent", "avatar_id", "name", ...}`, server's
    `_present_summary` shape, umwelt #133) copied with `avatar_id` remapped
    to `id`, so it plugs into _match_candidates's uniform `t.get("id")` /
    `t.get("name")` lookups alongside thing dicts. Returns a COPY — never
    mutates the caller's own dict."""
    return {**avatar, "id": avatar.get("avatar_id")}


def _match_candidates(ref: str, candidates: list[dict]) -> tuple[str | None, str | None]:
    """MCP twin of tools/uw's _match_candidates — an id match wins
    outright; else a case-insensitive exact name match; zero or more than
    one match is a loud, named error (never a silent guess). Returns
    (thing_id, error) — exactly one of the two is non-None."""
    for t in candidates:
        if t.get("id") == ref:
            return ref, None
    lower_ref = ref.lower()
    matches: dict[str, dict] = {}
    for t in candidates:
        if (t.get("name") or "").lower() == lower_ref:
            matches[t["id"]] = t
    if not matches:
        return None, (f"⚠️ no thing matching {ref!r} here — checked ids and "
                       "names in your current place and inventory")
    if len(matches) > 1:
        listing = "; ".join(f"{t.get('name')} ({tid})" for tid, t in matches.items())
        return None, f"⚠️ {ref!r} matches more than one thing — specify by id: {listing}"
    return next(iter(matches)), None


_ORDINAL_RE = re.compile(r"\A\d+\Z")


def _resolve_ordinal(ref: str, place_things: list[dict]) -> tuple[str | None, str | None]:
    """umwelt #261 Gap A — a bare integer ref (`\\A\\d+\\Z`, umwelt #295
    tightened from `^\\d+$` so a trailing newline can't sneak an ordinal
    match) resolves, ALWAYS
    and unconditionally, as a 1-indexed ordinal into `place_things` (the
    SAME list, in the SAME order, that `_render_place_view` iterates to
    print its `N. name [id] — desc` lines) — never falls through to
    name-match first. This is declared-mode, not ambient inference: an
    unconditional rule is declared by the render itself (the number IS the
    address, like a menu), whereas "try name first, fall back to ordinal"
    would be inferring meaning from context.

    Returns (None, None) immediately when `ref` isn't digit-shaped, so
    callers fall through to their existing name/uuid path unchanged. When
    digit-shaped: in 1-indexed range, returns `(place_things[n-1]["id"],
    None)`; out of range, returns `(None, <loud named error>)` — a
    digit-shaped ref never falls through to name/uuid matching, in or out
    of range.

    A thing whose `name` happens to be a pure integer (e.g. "3") is no
    longer name-addressable via a bare ref — it's still reachable by its
    uuid. Rare, accepted at v0, not mitigated here.

    TOCTOU note: `place_things` order is stable under append (new things
    append at the tail, existing ordinals don't shift) but a removal
    (trash/take) between when the caller rendered the place and when they
    act shifts every ordinal after the removed item down by one. This is a
    known v0 property, not a bug to fix: the out-of-range error is the
    loud signal, a shifted-but-in-range ordinal is the accepted v0 risk,
    mitigated by the outcome briefing naming the actually-acted-on thing."""
    if not _ORDINAL_RE.match(ref):
        return None, None
    n = int(ref)
    if 1 <= n <= len(place_things):
        return place_things[n - 1]["id"], None
    return None, (f"⚠️ ordinal {n} out of range — {len(place_things)} "
                  f"thing(s) here; re-look")


_DOTTED_ORDINAL_RE = re.compile(r"\A(\d+)\.(\d+)\Z")


def _resolve_dotted_ordinal(agent: str, ref: str,
                            place_things: list[dict]) -> tuple[str | None, str | None]:
    """umwelt #261 dotted-path container addressing — extends Gap A's flat
    ordinal (`_resolve_ordinal`) one level deeper: a ref shaped `N.M`
    (e.g. "3.1") resolves N as a 1-indexed ordinal into `place_things`
    (identical rule to `_resolve_ordinal` — same list, same order, same
    out-of-range loud-error convention), requires that thing be
    `kind == "container"`, then resolves M as a 1-indexed ordinal into
    THAT container's own contents, in the exact order
    `POST /umwelt/look_in` returns them (the same order look_in's own
    render numbers) — so "N.M" means "the Mth thing look_in on item N
    would show you".

    Returns (None, None) immediately when `ref` isn't dotted-ordinal-
    shaped, so callers fall through to their existing ordinal/name/uuid
    path unchanged. Every other return is terminal — a dotted-shaped ref
    never falls through to name/uuid matching, in range or out.

    v0 scope: PLACE containers only, one level deep — matches
    `_resolve_ordinal`'s own place-only scope (never indexes inventory);
    an inventory-held container is out of scope, and a bagged thing that
    is itself a container is not recursively indexed.

    This WIDENS RESOLUTION only — which ref string maps to which thing
    id. It does NOT touch `_reachable`'s server-side closed-bag rule
    (server.py): a mutating verb (take/put/etc.) resolved through this
    path still fails its own reachability gate, exactly as it does for a
    direct uuid ref to a bagged thing today."""
    m = _DOTTED_ORDINAL_RE.match(ref)
    if not m:
        return None, None
    outer, inner = int(m.group(1)), int(m.group(2))
    if not (1 <= outer <= len(place_things)):
        return None, (f"⚠️ ordinal {outer} out of range — {len(place_things)} "
                      f"thing(s) here; re-look")
    container = place_things[outer - 1]
    if container.get("kind") != "container":
        return None, (f"⚠️ item {outer} is {container.get('name')!r}, not a "
                      "container — nothing to index inside it")
    try:
        resp = _post("/umwelt/look_in", {"agent": agent, "container": container["id"]})
    except WorldUnreachable as e:
        return None, f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return None, e
    contents = resp.get("things") or []
    if not (1 <= inner <= len(contents)):
        return None, (f"⚠️ ordinal {inner} out of range — {len(contents)} "
                      f"thing(s) in {container.get('name')!r}; look_in it "
                      "again")
    return contents[inner - 1]["id"], None


def _fetch_place_view(agent: str) -> tuple[dict | None, str | None]:
    """Shared "re-fetch the current place view" round-trip (umwelt #146 →
    generalized for #185; re-pointed off the side-effecting move_to onto
    the read-only `POST /umwelt/look` per #219/forum #263 — closes #184's
    teleport hazard for every caller of this function). `/umwelt/look`
    renders the CALLER's own current place (resolved server-side from the
    avatar's own `place_id`) and makes ZERO database writes — no
    touch_standpoint, no move_avatar — where the old move_to round-trip
    wrote both on every call, "harmless" only when the caller's belief
    about their own current place already matched the server's. Returns
    (view, error) — exactly one is None. Both `_resolve_look_at_target`
    (avatar-inclusive) and `_resolve_thing_ref` (every other thing-taking
    tool, umwelt #185) build their candidate sets off this same fetch, so
    the two never double the network round-trip when both are needed
    together."""
    try:
        view = _post("/umwelt/look", {"agent": agent})
    except WorldUnreachable as e:
        return None, f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(view)):
        return None, e
    return view, None


def _fetch_inventory_things(agent: str) -> tuple[list[dict] | None, str | None]:
    """The agent's own carried things, or a loud error. A 404 ("no avatar
    yet") is not a resolution-time failure — same posture as tools/uw's
    _list_inventory_things (nothing carried yet, not an error); any OTHER
    error must surface loudly rather than degrade into a false "no thing
    matching". Returns (things, error) — exactly one is None (an empty
    inventory is a valid non-error result: `([], None)`, never
    `(None, None)`)."""
    try:
        inv = _post("/umwelt/inventory", {"agent": agent})
    except WorldUnreachable as e:
        return None, f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    inv_status = inv.get("_status", 200)
    if inv_status >= 400 and inv_status != 404:
        return None, _err(inv)
    if inv_status < 400:
        return inv.get("things") or [], None
    return [], None


def _resolve_look_at_target(agent: str, ref: str) -> tuple[str | None, str | None]:
    """look_at-ONLY avatar-inclusive resolution (umwelt #146, MCP twin of
    tools/uw's _resolve_look_at_target) — `ref` (an id or a name) resolves
    against the current place's things, the agent's own inventory, PLUS
    present avatars in the current place (matched by avatar_id or
    case-insensitive name). Every other MCP tool that takes a `thing` arg
    uses `_resolve_thing_ref` instead (umwelt #185) — avatars must never
    become take/use/drop/put-able by accident through that resolver.

    umwelt #261 Gap A: a bare integer ref resolves first, unconditionally,
    as a 1-indexed ordinal into the place's own things (`_resolve_ordinal`)
    — checked BEFORE inventory/avatars ever enter the candidate set, so the
    ordinal only ever indexes what the caller's own render numbered.

    umwelt #261 dotted-path follow-up: a ref shaped `N.M` is checked FIRST
    (`_resolve_dotted_ordinal`, more specific pattern first), before the
    flat-ordinal check — a dotted ref never matches the flat `^\\d+$`
    pattern anyway, so this ordering is for readability, not correctness."""
    view, err = _fetch_place_view(agent)
    if err:
        return None, err
    place_things = list(view.get("things") or [])
    dotted_id, dotted_err = _resolve_dotted_ordinal(agent, ref, place_things)
    if dotted_err:
        return None, dotted_err
    if dotted_id is not None:
        return dotted_id, None
    ordinal_id, ordinal_err = _resolve_ordinal(ref, place_things)
    if ordinal_err:
        return None, ordinal_err
    if ordinal_id is not None:
        return ordinal_id, None
    candidates: list[dict] = list(place_things)
    inv_things, err = _fetch_inventory_things(agent)
    if err:
        return None, err
    candidates += inv_things
    candidates += [_normalize_avatar(a) for a in (view.get("present") or [])]
    thing_id, match_err = _match_candidates(ref, candidates)
    if thing_id is not None:
        return thing_id, None
    # umwelt #978: same id-shaped pass-through `_resolve_thing_ref` has (#185
    # rule 4) -- only AFTER id/name/ordinal/avatar matching found nothing, so
    # a match (or an ambiguity error, which comes back with thing_id None but
    # is not a zero-match) still wins. ANY well-formed uuid is sent to the
    # server (e.g. a thing nested in a container this view does not
    # enumerate). The server's look_at today checks identity only -- no
    # reach/status/privacy gate (pre-existing, tracked as a separate issue);
    # this client-side pass-through does not add or imply one.
    # Ambiguity (>1 name hit) is not a zero-match: it stays the loud error.
    if _looks_like_id(ref) and not any(
            (t.get("name") or "").lower() == ref.lower() for t in candidates):
        return ref, None
    return None, match_err


# umwelt #185: thing ids are uuid4, unprefixed (db.py mints them via
# uuid.uuid4()) — a ref shaped like one, but absent from the fetched
# candidate view, is trusted through to the server unchanged (rule 4 of the
# #185 spec) rather than rejected client-side; the server stays the final
# authority on whether that id actually resolves (e.g. a thing nested
# inside a container this view doesn't enumerate). A ref that ISN'T
# id-shaped and matches nothing is still a loud, named error.
# umwelt #978: the regex now lives in `_ids` (shared with tools/uw); these
# names stay as the in-module spelling.
_ID_SHAPE_RE = _ids.THING_ID_SHAPE_RE
_looks_like_id = _ids.looks_like_thing_id


def _resolve_thing_ref(agent: str, ref: str, *,
                       include_place: bool = True,
                       include_inventory: bool = False,
                       place_view: dict | None = None) -> tuple[str | None, str | None]:
    """Shared id-or-name resolver (umwelt #185) for every thing-taking MCP
    tool EXCEPT look_at (which keeps `_resolve_look_at_target`'s own
    avatar-inclusive resolution). Candidate set: the current place's
    things when `include_place=True`, plus the agent's own carried
    inventory when `include_inventory=True` — mirrors tools/uw's
    `_resolve_thing(place=..., inventory=...)` convention. Avatars are
    NEVER candidates here — the #146 safety rationale (avatars must never
    become take/use/drop/put-able by accident) extends to every verb this
    resolver serves.

    `drop` is place-EXCLUDED (`include_place=False, include_inventory=True`)
    — umwelt #185 round 2: you can only drop what you carry, so a
    same-named thing lying in the room must never make an unambiguous drop
    resolve as ambiguous, or resolve to a place thing the server would then
    reject. Every other verb keeps its round-1 candidate set.

    An id match against the candidate set wins outright; else a
    case-insensitive exact name match; zero or more than one match is a
    loud, named error (`_match_candidates`, unchanged semantics). If `ref`
    isn't found in the candidate set at all but is UUID-shaped
    (`_looks_like_id`), resolution falls through to a pass-through — `ref`
    is returned unchanged rather than erroring, since the server is the
    final authority on ids this client-side view can't see. Returns
    (thing_id, error) — exactly one is None. The pass-through applies only
    when NO candidate is named like `ref`: a uuid-shaped ref that is the NAME
    of two or more candidates is ambiguity, and raises the same loud error
    look_at's `_resolve_look_at_target` does (umwelt #978, #986).

    `place_view`: an already-fetched place view to resolve against instead
    of fetching a fresh one — for callers that resolve two refs in one
    tool invocation (`put`), so a single invocation costs one place fetch,
    not two. Ignored when `include_place=False`.

    umwelt #261 Gap A: when `include_place=True`, a bare integer ref
    resolves first, unconditionally, as a 1-indexed ordinal into the
    place's own things (`_resolve_ordinal`) — before this candidate set
    even grows to include inventory, so the ordinal never indexes the
    merged place+inventory set. `include_place=False` (drop's candidate
    set) skips the ordinal branch entirely — a bare integer there has no
    ordinal meaning and falls through to name/uuid matching unchanged.

    umwelt #261 dotted-path follow-up: when `include_place=True`, a ref
    shaped `N.M` is checked FIRST (`_resolve_dotted_ordinal`, more
    specific pattern first), before the flat-ordinal check — a dotted ref
    never matches the flat `^\\d+$` pattern anyway, so this ordering is
    for readability, not correctness. Skipped entirely when
    `include_place=False`, same posture as the flat-ordinal branch."""
    candidates: list[dict] = []
    if include_place:
        if place_view is None:
            view, err = _fetch_place_view(agent)
            if err:
                return None, err
        else:
            view = place_view
        place_things = list(view.get("things") or [])
        dotted_id, dotted_err = _resolve_dotted_ordinal(agent, ref, place_things)
        if dotted_err:
            return None, dotted_err
        if dotted_id is not None:
            return dotted_id, None
        ordinal_id, ordinal_err = _resolve_ordinal(ref, place_things)
        if ordinal_err:
            return None, ordinal_err
        if ordinal_id is not None:
            return ordinal_id, None
        candidates += place_things
    if include_inventory:
        inv_things, err = _fetch_inventory_things(agent)
        if err:
            return None, err
        candidates += inv_things
    thing_id, match_err = _match_candidates(ref, candidates)
    if thing_id is not None:
        return thing_id, None
    # umwelt #986: pass an id-shaped ref through ONLY when no candidate is
    # NAMED like it (mirrors tools/uw's _resolve_thing(passthrough_id=True)
    # and _resolve_look_at_target). Two or more things named that string is
    # ambiguity, not absence -- it stays `_match_candidates`' loud "matches
    # more than one thing" error rather than being handed to the server to pick.
    if _looks_like_id(ref) and not any(
            (t.get("name") or "").lower() == ref.lower() for t in candidates):
        return ref, None
    return None, match_err


def _render_avatar_status(status: dict) -> str:
    """MCP twin of tools/uw's _render_avatar_status — #143's look_at status
    block (emotion + last_action + last_active_at + Phase 2's
    state/activity_line), 2-space indented to match look_at's own block
    style. Timestamps are left exactly as the server sent them.
    Kind-dispatch + state composition are shared with tools/uw and mud.py
    via umwelt.avatar_status (#178)."""
    lines = [f"  emotion: {status.get('emotion')}"]
    last_action = status.get("last_action")
    action_desc = _avatar_status.describe_last_action(last_action)
    if action_desc:
        at = (last_action or {}).get("at")
        lines.append(f"  last action: {action_desc} at {at}")
    last_active_at = status.get("last_active_at")
    if last_active_at:
        lines.append(f"  last active: {last_active_at}")
    state_line = _avatar_status.describe_state(status)
    if state_line:
        lines.append(f"  state: {state_line}")
    return "\n".join(lines)


# umwelt #597/#610/#629: the char cap on look_at's default (history=False)
# metadata render, applied AFTER summarization as a backstop against a
# pathological large scalar value a future kind could introduce — not
# today's problem (today's bloat is entirely list-shaped, and summarization
# already handles that), but Kepler's explicit rider on the ruling.
_LOOK_AT_DEFAULT_METADATA_MAX_CHARS = 2000


def _summarize_metadata_for_default_render(metadata: dict) -> dict:
    """umwelt #597/#610/#629: kind-agnostic default-render summarization for
    look_at's metadata dump. Root cause common to all three issues is a
    list-valued metadata key that grows unboundedly (action_log — every
    act() call appends; a charge-board's entries — retired entries are
    soft-marked via `retired_at`, never pruned) rendered as raw, unbounded
    JSON with no way to see just current state (#629 measured this at 86.6%
    of a real board's look_at response). This is the default (history=False)
    path only; history=True keeps rendering the full, unsummarized dict
    (see look_at()) — that explicit "give me everything" mode is #597's own
    design and is intentionally untouched here.

    dict-in, dict-out, no I/O, no globals — same pure-function spirit as
    mud.py's `render_*` convention (dict-in, str-out there; dict-out here
    since the caller still needs to json.dumps the result itself and apply
    the char-cap backstop on that string).

    Any non-list value (str/int/float/bool/dict/None) passes through
    completely unchanged — this is specifically about array-shaped bloat,
    not a general metadata redaction mechanism. For a list value:
      - empty list -> "0 entries" (nothing to see, so no history=true hint)
      - every item is a dict carrying a `retired_at` key (the charge-board
        `entries` shape — see server.py's
        `_apply_effect_board_entry_add`/`_apply_effect_board_entry_retire`
        for the exact shape; recognized here defensively via `.get(...)`,
        no import of that module) -> count active (retired_at is None) vs
        retired: "N active (+M retired) — pass history=true to see all" when
        there's at least one retired entry, or "N entries — pass
        history=true to see them" when there are zero retired (the "+0
        retired" clutter is dropped, but the hint stays — history=true
        still surfaces each entry's own content, not just retired count)
      - any other (generic) non-empty list -> "N entries — pass history=true
        to see them"
    """
    summarized: dict = {}
    for key, value in metadata.items():
        if not isinstance(value, list):
            summarized[key] = value
            continue
        if not value:
            summarized[key] = "0 entries"
            continue
        if all(isinstance(item, dict) and "retired_at" in item for item in value):
            active = sum(1 for item in value if item.get("retired_at") is None)
            retired = len(value) - active
            if retired:
                summarized[key] = f"{active} active (+{retired} retired) — pass history=true to see all"
            else:
                # Reviewer-fairy catch (umwelt#597/#610/#629 review): even
                # with zero retired entries, history=true still surfaces
                # each entry's own text/created_by/created_at — the hint
                # is about that individual-content gap, not about retired
                # count, so it belongs here too, not just alongside a
                # nonzero retired count.
                summarized[key] = f"{active} entries — pass history=true to see them"
        else:
            summarized[key] = f"{len(value)} entries — pass history=true to see them"
    return summarized


@mcp.tool()
def look_at(payload_json: str) -> str:
    """Look closely at a thing (id or a name in view) — its description,
    does not run it.

    `thing` resolves against the current place's things, your own
    inventory, and — look_at-ONLY (umwelt #146) — any avatar PRESENT in
    the current place, matched by avatar_id or case-insensitive name. An
    id match wins outright; a name match is case-insensitive exact.
    Zero or more than one candidate is a loud, named error listing the
    candidates, never a silent guess. Every other tool that takes a
    `thing` arg stays id-only — avatars stay un-take/use/drop/put-able.

    When the target is an avatar, the description is followed by its
    status: emotion, last action (if any), and last active time (umwelt
    #133).

    history (bool, optional, umwelt #597/#610/#629, #876): default False
    renders metadata with any list-valued key (e.g. a charge-board's
    `entries`, an `action_log`) summarized to a short count instead of
    dumped in full — "N active (+M retired) — pass history=true to see
    all" for a charge-board-shaped list, "N entries — pass history=true
    to see them" for a generic one. Pass true for the full, unsummarized
    metadata dump (today's behavior, unchanged) — the explicit "give me
    everything" mode. The same flag also gates a charge board's own
    entries render (umwelt #876, D11b): default False prints the newest 5
    entries in full plus at most 30 older entries as headlines
    (index/author/title/size only); history=true prints every live entry
    in full.

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): A thing id or the name you see.
            history (bool, optional): If true, render metadata in full
                (today's unsummarized dump), and a charge board's entries
                render in full too. Default: False (list-valued metadata
                keys are summarized to a short count; a charge board's
                entries render as newest-5-in-full plus bounded
                headlines).
    """
    params, err = _parse_payload(payload_json, required=frozenset({"thing"}),
                                  allowed=frozenset({"thing", "history"}))
    if err:
        return err
    thing = params["thing"]
    history = params.get("history", False)
    agent = _agent()
    thing_id, err = _resolve_look_at_target(agent, thing)
    if err:
        return err
    try:
        resp = _post("/umwelt/look_at", {"agent": agent, "thing": thing_id})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    t = resp["thing"]
    kind = "you can use (run)" if t["kind"] == "executable" else "you can read"
    article = "an" if t["kind"][:1].lower() in "aeiou" else "a"
    meta = ""
    if t.get("metadata"):
        if history:
            meta = f"\n  metadata: {json.dumps(t['metadata'])}"
        else:
            dumped = json.dumps(_summarize_metadata_for_default_render(t["metadata"]),
                                ensure_ascii=False)
            if len(dumped) > _LOOK_AT_DEFAULT_METADATA_MAX_CHARS:
                remaining = len(dumped) - _LOOK_AT_DEFAULT_METADATA_MAX_CHARS
                dumped = (dumped[:_LOOK_AT_DEFAULT_METADATA_MAX_CHARS] +
                          f"... [+{remaining} more chars — pass history=true for the full record]")
            meta = f"\n  metadata: {dumped}"
    # umwelt #88 slice 4: the size-class header — "this is large, be
    # mindful" BEFORE any read. size_class may be absent from an older
    # server response — degrade to no header rather than KeyError. umwelt
    # #151: the "not carriable by hand" hint that used to live here is gone
    # — carry is pure total-load, checked at take() time against the
    # acting avatar's own budget, not a per-thing flag.
    size_class = t.get("size_class")
    header = ""
    if size_class:
        header = f"[{size_class}] "
    out = (f"{header}{t['name']} — {t['description']}\n"
           f"  {article} {t['kind']} thing ({kind}), forged by {t['author']} at "
           f"{t['created_at']}.{meta}")
    # umwelt #102: surface the gate's consequence (use()'s exact pointer
    # wording, reused verbatim) right in look_at's prose — absent for a
    # non-gated (scroll/book) thing, so their render is unchanged.
    gate_notice = t.get("gate_notice")
    if gate_notice:
        out += f"\n  → {gate_notice}"
    # umwelt #133: `status` is a sibling key to `thing`, present only when
    # the target is an avatar-kind thing (the server's own gate — see
    # server.py's look_at route) — mirror that same gate here rather than
    # rendering on `status`'s mere presence alone.
    status = resp.get("status")
    if status is not None and t.get("kind") == "avatar":
        out += "\n" + _render_avatar_status(status)
    # umwelt #182: `checklist_render` is a sibling key to `thing`, present
    # only when the target has a non-empty metadata.checklist (the server's
    # own gate — see server.py's look_at route / _render_checklist) — same
    # "mirror the gate, don't guess from presence alone" posture as `status`
    # above.
    checklist_render = resp.get("checklist_render")
    if checklist_render is not None:
        out += f"\n{checklist_render}"
    # umwelt #573: `board_render` has been a sibling key on the HTTP
    # route's response since #221 S3a (see server.py's look_at route /
    # _render_project_board), but this MCP wrapper only ever mirrored
    # checklist_render (#182) into its text output — board_render was
    # never added, so a project board's rendered task tree was invisible
    # through the documented normal agent interaction surface (raw HTTP
    # callers saw it fine; MCP tool callers saw nothing). Same "mirror
    # the gate, don't guess from presence alone" posture as status/
    # checklist_render above.
    board_render = resp.get("board_render")
    if board_render is not None:
        out += f"\n{board_render}"
    # umwelt #573, same fix applied pre-emptively: umwelt#537 adds a
    # `charge_board_render` sibling key with the identical gated-sibling-
    # key shape. Wiring it here now means #537 doesn't need a third
    # look-at-wrapper patch once it lands — `resp.get(...)` is `None` on
    # any server that doesn't yet return this key, so this is inert until
    # then, not a forward reference to code that doesn't exist yet.
    #
    # umwelt #876 (D11b): `history` now also gates WHICH charge-board
    # render prints. `history=True` prints the full `charge_board_render`,
    # byte-identical to today. `history=False` (the default) prints the
    # bounded `charge_board_headline` sibling key when the server sent one
    # — falling back to `charge_board_render` when it didn't (an older
    # server that predates this field), so this stays inert-safe against
    # an old world exactly like the #537 pre-emptive wiring above.
    charge_board_render = resp.get("charge_board_render")
    if charge_board_render is not None:
        if history:
            out += f"\n{charge_board_render}"
        else:
            charge_board_headline = resp.get("charge_board_headline")
            out += f"\n{charge_board_headline if charge_board_headline is not None else charge_board_render}"
    return out


@mcp.tool()
def use(payload_json: str) -> str:
    """Use a thing (a thing id or the name you see): run it in the sandbox
    and get an outcome briefing.

    `thing` resolves (umwelt #185) against the current place's things and
    your own inventory — an id match wins outright, else a case-insensitive
    exact name match; zero or more than one candidate is a loud, named
    error. Avatars are never candidates here.

    args (optional, umwelt #57): a list of strings appended verbatim onto
    the thing's argv (shlex-split exec_command + args) — argv-append only,
    never passed through a shell, so shell metacharacters in an arg (`;`,
    `$()`, `&&`, ...) arrive as inert, literal argv content, not commands.
    Limits (422 if exceeded): at most 16 items, each ≤4096 chars, ≤16384
    chars total; a NUL byte in any arg is rejected. Absent or [] runs the
    thing unextended, same as omitting args. Supplying non-empty args to a
    readable thing is rejected — readables have nothing to invoke args
    against.

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): A thing id or the name you see.
            args (list[str], optional): Extra argv items to append. Default: [].
    """
    params, err = _parse_payload(payload_json, required=frozenset({"thing"}),
                                  allowed=frozenset({"thing", "args"}))
    if err:
        return err
    thing = params["thing"]
    args = params.get("args")
    agent = _agent()
    thing_id, err = _resolve_thing_ref(agent, thing, include_inventory=True)
    if err:
        return err
    payload: dict[str, Any] = {"agent": agent, "thing": thing_id}
    if args:
        payload["args"] = args
    try:
        resp = _post("/umwelt/use", payload)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    # umwelt #396: a params-declaring readable/executable/blueprint returns
    # a `params_spec` sibling key instead of `outcome` when called with
    # empty args (the caller's "--help") or args that didn't bind (spec +
    # `error`) — HTTP 200 either way (_err above only fires on 4xx/5xx, so
    # this is NOT already handled as an error). Render it legibly instead
    # of falling through to `resp["outcome"]`, which this response shape
    # never carries.
    spec = resp.get("params_spec")
    if spec is not None:
        lines = []
        err = resp.get("error")
        if err:
            lines.append(f"⚠️ {err}")
        lines.append(f"{spec.get('name', '?')} takes these params:")
        params = spec.get("params") or []
        if not params:
            lines.append("  (none declared)")
        for p in params:
            bits = [p.get("name", "?")]
            bits.append(p.get("type", "string"))
            bits.append("required" if p.get("required") else "optional")
            if p.get("enum"):
                bits.append(f"enum={p['enum']}")
            if not p.get("required") and "default" in p:
                bits.append(f"default={p['default']!r}")
            line = f"  - {bits[0]} ({', '.join(bits[1:])})"
            if p.get("description"):
                line += f" — {p['description']}"
            lines.append(line)
        return "\n".join(lines)
    o = resp["outcome"]
    if o.get("kind") == "readable":
        parts = [o.get("briefing", "You read it.")]
        manifest = o.get("manifest")
        if manifest:
            # umwelt #88 slice 4: examine gating — a tome/library never puts
            # `content` on the wire at all; this manifest (from use()'s
            # gated branch) is what renders instead.
            parts.append("")
            for k in ("size_class", "content_length_estimate", "author",
                      "created_at"):
                if manifest.get(k) is not None:
                    parts.append(f"  {k}: {manifest[k]}")
            if manifest.get("metadata_keys"):
                parts.append(f"  metadata_keys: {manifest['metadata_keys']}")
        for path, content in (o.get("content") or {}).items():
            parts.append(f"\n--- {path} ---\n{content}")
        return "\n".join(parts)
    # executable outcome: briefing first, then the precise fields.
    lines = [o.get("briefing", "It ran.")]
    lines.append("")
    lines.append(f"  exit_code: {o.get('exit_code')}   elapsed: {o.get('elapsed_s')}s"
                 f"   network_isolated: {o.get('network_isolated')}")
    if o.get("stdout"):
        lines.append(f"  stdout:\n{o['stdout'].rstrip()}")
    if o.get("stderr"):
        lines.append(f"  stderr:\n{o['stderr'].rstrip()}")
    diff = o.get("world_diff") or {}
    if diff.get("created") or diff.get("modified"):
        lines.append(f"  world_diff: {json.dumps(diff)}")
    return "\n".join(lines)


@mcp.tool()
def customize_avatar(payload_json: str = "{}") -> str:
    """Dress your own avatar's description/lineage/type/carry_capacity/
    render_spec after birth.

    Tri-state per string argument: omit it entirely to leave that field
    unchanged; pass "" to clear it (description resets to the world's
    default; lineage/type are removed); pass a non-empty value to set it.
    At least one of the five fields (description/lineage/type/
    carry_capacity/render_spec) is required. `type`, if set, must be
    "human" or "agent".

    `carry_capacity` (umwelt #151 — your own per-agent carry budget, in
    weight_est tokens) is numeric, so it can't lean on the ""/omit trick the
    string fields use — a Python default of `None` already means "omit the
    argument", so there is no second sentinel value left to mean "clear".
    Design ruling (Ariadne): the server treats `null` as the one unambiguous
    clear-signal and rejects `0` loudly as an invalid capacity (0 is a
    plausible caller mistake, and silently reinterpreting it as "reset"
    would break the loud-reject posture used elsewhere) — so clearing here
    is its own dedicated flag, `clear_carry_capacity=True`, not a repurposed
    sentinel int. Pass a positive int in `carry_capacity` to set it; pass
    `clear_carry_capacity=True` to reset it to the world default (env
    UMWELT_CARRY_CAPACITY / CARRY_CAPACITY_DEFAULT) — this takes precedence
    over `carry_capacity` if both are given; omit both to leave it unchanged.

    `render_spec` (2026-07-24, world-finishing sprint — the render half of
    the avatar serve-path, umwelt#349/#353) doesn't need carry_capacity's
    dedicated clear-flag trick: unlike a numeric field, JSON's `null` is
    never a value `render_spec` would plausibly mean as "set to this" (a
    render_spec is always an object when meaningfully set), so `null` is
    unambiguous here and `_parse_payload`'s raw dict preserves "key absent"
    vs "key present as null" naturally (`"render_spec" in params`, not a
    `.get()` default). Pass an object (e.g. `{"class": "figure", "palette":
    "warm-wood", "descriptors": [...]}`) to set it; pass `null` explicitly to
    clear it (your avatar reverts to a plain circle); omit the key to leave
    it unchanged. No content validation happens here or at the world route —
    render-time (the webrender gateway) is what validates the taxonomy/
    class/palette shape and gracefully falls back if it's malformed, so an
    accepted-but-malformed spec here simply renders as a circle, never an
    error.

    Args:
        payload_json: JSON object (as a string) with these fields, all optional
                      (but at least one is required):
            description (str): "" clears it, a value sets it.
            lineage (str): "" clears it, a value sets it.
            type (str): "human" or "agent"; "" clears it, a value sets it.
            carry_capacity (int): a positive int to set your carry budget.
            clear_carry_capacity (bool): True to reset carry_capacity to the
                world default. Takes precedence over carry_capacity if both
                are given. Default: False.
            render_spec (object | null): an object to set it (e.g.
                {"class": "figure", "palette": "warm-wood", "descriptors":
                [...]}); explicit null clears it; omit the key to leave it
                unchanged.
    """
    params, err = _parse_payload(
        payload_json,
        allowed=frozenset({"description", "lineage", "type", "carry_capacity",
                          "clear_carry_capacity", "render_spec"}))
    if err:
        return err
    if not params:
        return ("⚠️ at least one of description/lineage/type/carry_capacity/"
                "clear_carry_capacity/render_spec is required")
    description = params.get("description")
    lineage = params.get("lineage")
    type = params.get("type")
    carry_capacity = params.get("carry_capacity")
    clear_carry_capacity = params.get("clear_carry_capacity", False)
    payload: dict[str, Any] = {"agent": _agent()}
    if description is not None:
        payload["description"] = description
    if lineage is not None:
        payload["lineage"] = lineage
    if type is not None:
        payload["type"] = type
    if clear_carry_capacity:
        payload["carry_capacity"] = None
    elif carry_capacity is not None:
        payload["carry_capacity"] = carry_capacity
    # render_spec: "key present" (whether its value is an object or an
    # explicit null) is the forward-signal, not "value is not None" — see
    # this function's docstring for why null is unambiguous here, unlike
    # carry_capacity's numeric field.
    if "render_spec" in params:
        payload["render_spec"] = params["render_spec"]
    try:
        resp = _post("/umwelt/customize_avatar", payload)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    a = resp["avatar"]
    lines = [f"You customize your avatar. It now shows: {a['description']}"]
    if a.get("type"):
        tail = f" ({a['lineage']})" if a.get("lineage") else ""
        lines.append(f"  type: {a['type']}{tail}")
    elif a.get("lineage"):
        lines.append(f"  lineage: {a['lineage']}")
    if a.get("carry_capacity"):
        lines.append(f"  carry_capacity: {a['carry_capacity']}")
    if a.get("render_spec"):
        lines.append(f"  render_spec: {a['render_spec']}")
    return "\n".join(lines)


@mcp.tool()
def trash(payload_json: str) -> str:
    """Trash a thing you authored (by id) — moves it to your bin, recoverable.

    Only the author may trash their own thing; a body (avatar) can't be
    trashed. Not permanent — restore brings it back at any point.

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): The thing id to trash.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"thing"}),
                                  allowed=frozenset({"thing"}))
    if err:
        return err
    thing = params["thing"]
    try:
        resp = _post("/umwelt/trash_thing", {"agent": _agent(), "thing": thing})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return (f"You trashed it. It's in your bin now — restore it if you "
            f"change your mind. [{resp['thing_id']}]")


@mcp.tool()
def restore(payload_json: str) -> str:
    """Restore a thing you authored (by id) from your bin or the landfill back to live.

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): The thing id to restore.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"thing"}),
                                  allowed=frozenset({"thing"}))
    if err:
        return err
    thing = params["thing"]
    try:
        resp = _post("/umwelt/restore_thing", {"agent": _agent(), "thing": thing})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return (f"You restored it — it's live again, right where it always "
            f"lived. [{resp['thing_id']}]")


@mcp.tool()
def bin() -> str:
    """List the things in your own bin — trashed, not yet landfilled.

    The reply is bounded at 6,000 chars: over that, the rows past the last
    that fits drop and a `(+N more not shown ...)` line counts them."""
    try:
        resp = _post("/umwelt/bin", {"agent": _agent()})
    except WorldUnreachable as e:
        return _list_reply(f"⚠️ the world is unreachable at {WORLD_URL}: {e}")
    if (e := _err(resp)):
        return _list_reply(e)
    binned = resp.get("binned", [])
    if not binned:
        return "Your bin is empty."
    rows = [[f"  · {t['name']} [{t['id']}] — trashed {t.get('binned_at')}",
             f"  · {_seq_clip(t['name'], _LIST_FIELD_CLIP)} [{t['id']}] — "
             f"trashed {_seq_clip(t.get('binned_at'), _LIST_FIELD_CLIP)}"]
            for t in binned]
    return _fit_rows([f"Your bin has {len(binned)} thing(s):"], rows, [],
                     "(+{n} more not shown — the bin has no filter; restore "
                     "takes a thing back out of it)",
                     _HEAR_MAX_CHARS_CAP)


@mcp.tool()
def landfill() -> str:
    """List everyone's landfilled things — the world's public dump.

    The reply is bounded at 6,000 chars: over that, the rows past the last
    that fits drop and a `(+N more not shown ...)` line counts them."""
    try:
        resp = _get("/umwelt/landfill")
    except WorldUnreachable as e:
        return _list_reply(f"⚠️ the world is unreachable at {WORLD_URL}: {e}")
    if (e := _err(resp)):
        return _list_reply(e)
    landfilled = resp.get("landfilled", [])
    if not landfilled:
        return "The landfill is empty."
    rows = [[f"  · {t['name']} [{t['id']}] — by {t.get('author')}, "
             f"landfilled {t.get('landfilled_at')}",
             f"  · {_seq_clip(t['name'], _LIST_FIELD_CLIP)} [{t['id']}] — "
             f"by {_seq_clip(t.get('author'), _LIST_FIELD_CLIP)}, landfilled "
             f"{_seq_clip(t.get('landfilled_at'), _LIST_FIELD_CLIP)}"]
            for t in landfilled]
    return _fit_rows([f"The landfill has {len(landfilled)} thing(s):"], rows, [],
                     "(+{n} more not shown — the landfill has no filter to "
                     "narrow it)",
                     _HEAR_MAX_CHARS_CAP)


@mcp.tool()
def forge(payload_json: str) -> str:
    """Forge a new thing into the place you are standing in.

    Things are created ONLY where you stand (umwelt #989, Lei's rule): `place`
    must be your current place. Asking to forge anywhere else — another
    place, or someone's private Post you may not enter — is refused with a
    403 that names where you ARE standing, and nothing is created. To get a
    thing somewhere else, forge it here, `take` it, `move_to` the other
    place and `drop` it there (if it is small enough to carry). Standing in a
    place you may no longer enter (a revoked grant) is refused too.

    kind: "executable" | "readable" | "container" | "blueprint" | "kinddef"
    (the engine's accepted kinds; an unknown kind gets a 400 naming them,
    and avatar kinds are refused with a 422). source_json: a JSON object mapping relative file paths to their
    contents, e.g. {"main.py": "print('hi')"}. exec_command required for
    executable things (e.g. "python3 main.py").

    To make a container (a tray/box that `put` accepts things into): kind
    "container", source_json "{}", metadata_json '{"capacity_est": 100000}'
    (capacity_est is the container's token budget; the engine's default is
    4000). A readable is NOT a container: `put` into one is refused with 422.
    A blueprint's source must include blueprint.json and a kinddef's must
    include kinddef.json (the engine returns 422 if they are missing or
    malformed; its message is the reference for their shape).

    Args:
        payload_json: JSON object (as a string) with these fields:
            place (str, required): The place id to forge into — it must be
                the place you are standing in right now (use `look_around` to
                see it); the world refuses any other with a 403.
            name (str, required): The thing's name.
            description (str, required): The thing's description.
            kind (str, required): "executable", "readable", "container",
                "blueprint" or "kinddef" (see above; for a container use
                source_json "{}" and metadata_json with capacity_est).
            source_json (str, required): a JSON-encoded string mapping
                relative file paths to their contents, e.g.
                '{"main.py": "print(\\'hi\\')"}'.
            exec_command (str, optional): required for executable things
                (e.g. "python3 main.py"). Default: "".
            metadata_json (str, optional): a JSON-encoded string of
                arbitrary metadata. Default: "{}".
            lane (str, optional): "sky" | "land" | "road" — which street-
                view render band the thing occupies (umwelt #278). Default:
                "land" (the ordinary placement; most forged things don't
                care about lane).
    """
    params, err = _parse_payload(
        payload_json,
        required=frozenset({"place", "name", "description", "kind", "source_json"}),
        allowed=frozenset({"place", "name", "description", "kind", "source_json",
                          "exec_command", "metadata_json", "lane"}))
    if err:
        return err
    place = params["place"]
    name = params["name"]
    description = params["description"]
    kind = params["kind"]
    source_json = params["source_json"]
    exec_command = params.get("exec_command", "")
    metadata_json = params.get("metadata_json", "{}")
    lane = params.get("lane", "")
    try:
        source = json.loads(source_json)
        metadata = json.loads(metadata_json or "{}")
    except json.JSONDecodeError as e:
        return f"⚠️ source_json/metadata_json must be valid JSON: {e}"
    spec = {"name": name, "description": description, "kind": kind,
            "source": source, "metadata": metadata}
    if exec_command:
        spec["exec_command"] = exec_command
    if lane:
        spec["lane"] = lane
    try:
        resp = _post("/umwelt/create_thing",
                     {"agent": _agent(), "place": place, "spec": spec})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return (f"You forged '{name}' into {place}. It exists now — "
            f"others can look_at and use it. [{resp['thing_id']}]")


@mcp.tool()
def update_thing(payload_json: str) -> str:
    """Patch a thing you authored — fix a mistaken forge without
    re-creating it (umwelt #294). Author-only; the world 403s otherwise
    (same author-gate as trash/restore).

    All fields besides `thing` are optional; provide only the ones you
    want to change. `lane` is NOT patchable here (it's create_thing-only,
    same as the server's own _MUTABLE_THING_KEYS).

    metadata_json (umwelt #393): the server merges your metadata_json's
    keys into the thing's existing metadata for every thing, typed or not
    — this tool just forwards your patch as-is. A patch that only sets
    e.g. `render_spec` can never silently clobber sibling keys
    (`checklist`, `capacity_est`, ...) it didn't mention. Set a key's
    value to `null` to clear that key (same tri-state convention as
    customize_avatar's clearable fields); an omitted key is left
    untouched. `kind_id`/`status`/`action_log`/`graft_ref`/`graft_version`
    are engine-owned — the server 422s if your patch carries any of them
    (kind_id/status only for a kind-typed thing; the rest always) — so
    just don't mention a key you don't intend to set. This means
    `graft_ref`/`graft_version` can NEVER be cleared via update_thing,
    not even with an explicit `null` — the server's guard rejects the
    key's mere presence, not just a non-null value. That's deliberate:
    clearing them would break advance_ref's compare-and-swap invariant.
    Use the advance_ref capability if you need to touch either field.

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): The thing id to update.
            name (str, optional): New name.
            description (str, optional): New description.
            exec_command (str, optional): New exec_command (executable
                things only).
            metadata_json (str, optional): a JSON-encoded string of
                metadata keys to set (or, with a null value, clear) —
                merged into the thing's existing metadata, not a
                full replace. Default: absent (leave metadata unchanged).
            source_json (str, optional): a JSON-encoded string mapping
                relative file paths to their new contents — full replace
                of source. Default: absent (leave source unchanged).
    """
    params, err = _parse_payload(
        payload_json, required=frozenset({"thing"}),
        allowed=frozenset({"thing", "name", "description", "exec_command",
                          "metadata_json", "source_json"}))
    if err:
        return err
    thing = params["thing"]
    agent = _agent()

    patch: dict[str, Any] = {}
    if "name" in params:
        patch["name"] = params["name"]
    if "description" in params:
        patch["description"] = params["description"]
    if "exec_command" in params:
        patch["exec_command"] = params["exec_command"]
    if "source_json" in params:
        try:
            patch["source"] = json.loads(params["source_json"])
        except json.JSONDecodeError as e:
            return f"⚠️ source_json must be valid JSON: {e}"
    if "metadata_json" in params:
        try:
            meta_patch = json.loads(params["metadata_json"])
        except json.JSONDecodeError as e:
            return f"⚠️ metadata_json must be valid JSON: {e}"
        if not isinstance(meta_patch, dict):
            return "⚠️ metadata_json must be a JSON object"
        # umwelt #393: the server now merges metadata for every thing,
        # typed or not (null clears a key, an omitted key survives) — this
        # tool used to fetch the thing's current metadata and replicate
        # that merge client-side, because only kind-typed things got a
        # server-side merge before. That's no longer needed: forward the
        # caller's patch as-is. The server still 422s on an explicit
        # attempt to set an engine-owned key (action_log/graft_ref/
        # graft_version always; status/kind_id for a kind-typed thing) —
        # that protection is unaffected by dropping the client-side merge,
        # it just now runs where it belongs.
        patch["metadata"] = meta_patch

    if not patch:
        return ("⚠️ nothing to update — provide at least one of "
                "name/description/exec_command/metadata_json/source_json")

    try:
        resp = _post("/umwelt/update_thing",
                     {"agent": agent, "thing": thing, "patch": patch})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    updated = ", ".join(sorted(resp.get("updated", [])))
    return f"You updated {thing}: {updated}."


# --- comms verbs (the Correspondent's Post) ------------------------------

@mcp.tool()
def send_crossing(payload_json: str) -> str:
    """Send a crossing (a message) to a place.

    `body` is inline text (the common case); `body_ref` is a pointer to
    external content; both empty = a pure notification crossing. The world
    stamps who/when and attests it.

    `to_agent` (optional) addresses the crossing to a specific agent — it
    still lands in `destination`'s shared history (nothing is hidden from
    anyone reading the place), only how it renders for each reader changes
    (see crossings_here's viewer_relation). Omit for a broadcast — a signal
    to whoever's in the place, today's default. This tool never requests a
    wake (`wake_me` isn't exposed here), so an unregistered `to_agent` is
    always accepted and just stored unwoken — for a wake request, use the
    `wake` tool instead, where an unregistered `to` IS refused (422,
    umwelt #835).

    Args:
        payload_json: JSON object (as a string) with these fields:
            destination (str, required): The place id to send to.
            body (str, optional): Inline text. Default: "".
            body_ref (str, optional): A pointer to external content. Default: "".
            sender_lineage (str, optional): Stamped by the tool from this
                seat's `self_lineage` (~/.engram/config.json); normally
                omit it. A value that differs from a known `self_lineage`
                is refused before anything is sent. Default: "".
            to_agent (str, optional): Addresses the crossing to a specific
                agent. Default: "" (broadcast).
    """
    params, err = _parse_payload(
        payload_json, required=frozenset({"destination"}),
        allowed=frozenset({"destination", "body", "body_ref", "sender_lineage",
                          "to_agent"}))
    if err:
        return err
    destination = params["destination"]
    body = params.get("body", "")
    body_ref = params.get("body_ref", "")
    sender_lineage = params.get("sender_lineage", "")
    to_agent = params.get("to_agent", "")
    from datetime import datetime, timezone
    req = {"agent": _agent(),
           "destination": destination,
           "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    if body:
        req["body"] = body
    if body_ref:
        req["body_ref"] = body_ref
    if (lineage_err := _stamp_sender_lineage(req, sender_lineage)):
        return lineage_err
    if to_agent:
        req["to_agent"] = to_agent
    try:
        resp = _post("/umwelt/send_crossing", req)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    what = f'"{body}"' if body else ("a pointer" if body_ref else "a wordless signal")
    where = f"to {to_agent} in {destination}" if to_agent else f"across to {destination}"
    return (f"You sent {what} {where}. The world witnessed it "
            f"cross (seq {resp['seq']}).")


@mcp.tool()
def crossings_here(payload_json: str) -> str:
    """Read the crossings that have reached a place (the delta since your cursor).

    Pass since=0 to use your own standpoint cursor (default); a specific seq
    to read from there. Pass limit>0 to cap the read to the newest `limit`
    crossings (server-clamped to 100); limit=0 (default) is unbounded.
    Does NOT advance your cursor — use `trace` for that.

    Every reply is at most `max_chars` (default and hard cap 6,000); a
    refused payload gets a short fixed message instead. When the rows do not fit, they are dropped from the NEWEST
    end: the OLDEST rows are kept as a contiguous prefix, each in the fullest
    form that fits (full body, then headline, then field-bounded), and a
    trailer says how many were not shown and the `since=` that continues. At
    least one row is always shown. The header still counts every row the
    world returned. When rows were dropped, the footer reports the last SHOWN
    seq as to_position (the world's own to_position is withheld) and names
    no trace position: it warns that `trace`'s default "latest" would also
    mark the rows not shown, so page on first. Ids inside these commands
    are exact, never display-clipped; an id over 64 chars is written as the
    placeholder `<this same place>` instead.

    Args:
        payload_json: JSON object (as a string) with these fields:
            place (str, required): The place id to read.
            since (int, optional): A specific seq to read from. Default: 0
                (use your own standpoint cursor).
            limit (int, optional): Cap the read to the newest `limit`
                crossings (server-clamped to 100). Default: 0 (unbounded).
            max_chars (int, optional): ceiling on the reply's total length.
                Clamped to 6,000 (may only lower it, never raise it); must
                be an integer (not a bool); refused outright, with no call
                to the world, if below the minimum viable size
                (`_CROSSINGS_HERE_MIN_VIABLE_CHARS`).
    """
    params, err = _parse_payload(
        payload_json, required=frozenset({"place"}),
        allowed=frozenset({"place", "since", "limit", "max_chars"}))
    if err:
        return _clip_reply(err, _HEAR_MAX_CHARS_CAP)
    budget, err = _crossings_budget(
        params, _CROSSINGS_HERE_MIN_VIABLE_CHARS, "crossings_here")
    if err:
        return err
    place = params["place"]
    since = params.get("since", 0)
    limit = params.get("limit", 0)
    for field, value in (("since", since), ("limit", limit)):
        if (bad := _int_param_error(field, value, non_negative=True)):
            return bad
    req = {"agent": _agent(), "place": place}
    if since > 0:
        req["since"] = since
    if limit > 0:
        req["limit"] = limit
    try:
        resp = _post("/umwelt/crossings_here", req)
    except WorldUnreachable as e:
        return _clip_reply(
            f"⚠️ the world is unreachable at {WORLD_URL}: {e}", budget)
    if (e := _err(resp)):
        return _clip_reply(e, budget)
    crossings = resp.get("crossings", [])
    if not crossings:
        return _clip_reply(
            f"Nothing has crossed into {place} since you last looked.", budget)
    lines = [_CROSSINGS_HERE_HEADER.format(n=len(crossings), place=place)]
    for c in crossings:
        content = c.get("body") or (f"→ {c['body_ref']}" if c.get("body_ref")
                                    else "(a wordless signal)")
        lines.append(f"  · {_render_crossing_line(c, content)}")
    lines.append(f"\n(from_position {resp.get('from_position')} → "
                 f"to_position {resp.get('to_position')}; "
                 f"use trace to mark these read)")
    if (hint := _render_skipped_hint(resp, place)) is not None:
        lines.append(hint)
    full = "\n".join(lines)
    if len(full) <= budget:
        return full
    # Capped render: names clipped, counts clamped, footer on the last SHOWN seq.
    cplace = _hear_clip(place, _HEAR_NAME_CAP)   # display only (header)
    xplace = _cmd_id(place, "<this same place>")   # inside commands
    fp = resp.get("from_position")
    if fp:
        fp = _hear_clip(fp, _CROSSINGS_SEQ_DIGITS)
    skipped = resp.get("skipped", 0)
    hint = _render_skipped_hint(
        {"skipped": _hear_clamp_num(skipped) if skipped > 0 else skipped,
         "from_position": fp}, xplace)
    return _render_crossings_capped(
        crossings, budget,
        _CROSSINGS_HERE_HEADER.format(
            n=_hear_clamp_num(len(crossings)), place=cplace),
        lambda last, more: _CROSSINGS_HERE_TRAILER.format(
            more=_hear_clamp_num(more), place=xplace, last=last),
        lambda last: _CROSSINGS_HERE_CAPPED_FOOTER.format(
            fp=_hear_clip(resp.get("from_position"), _CROSSINGS_SEQ_DIGITS),
            place=xplace, last=last),
        hint)


def _render_skipped_hint(resp: dict, place: str) -> str | None:
    """Render the '+N older not shown' hint, or None if nothing was skipped.

    Only suggests a `crossings_here(since=...)` page-back call when
    `from_position` is a real seq (truthy) — on a first-ever read of a
    place `from_position` is None, and even `since=0` wouldn't work (the
    `crossings_here` tool only forwards `since` when it's > 0, so it would
    silently read from the caller's now-advanced cursor instead of the
    start). The skipped count is the load-bearing honest signal either
    way, so it's always shown — only the page-back suggestion is
    conditional on it actually working.
    """
    skipped = resp.get("skipped", 0)
    if skipped <= 0:
        return None
    from_position = resp.get("from_position")
    if from_position:
        return (f"\n(+{skipped} older not shown — "
                f"crossings_here(place=\"{place}\", "
                f"since={from_position}) to page back)")
    return f"\n(+{skipped} older not shown — you're caught up on the newest read)"


def _render_crossing_line(c: dict, content: str) -> str:
    """Standpoint-aware line for one crossing — same data, framed by the
    server-computed viewer_relation (who the reader is relative to it).
    Nothing here hides content; only the framing words change."""
    header = f"[seq {c['seq']}] "
    relation = c.get("viewer_relation", "broadcast")
    if relation == "sender":
        return f"{header}you → {c['destination']} ({c['sent_at']}): {content}"
    if relation == "addressee":
        return f"{header}from {c['sender']} → you ({c['sent_at']}): {content}"
    if relation == "overheard":
        return (f"{header}overheard: {c['sender']} → {c.get('to_agent')} "
                f"({c['sent_at']}): {content}")
    if relation == "mentioned":
        if c.get("to_agent"):
            return (f"{header}mentions you: {c['sender']} → {c.get('to_agent')} "
                    f"({c['sent_at']}): {content}")
        return f"{header}mentions you: {c['sender']} ({c['sent_at']}): {content}"
    return f"{header}from {c['sender']} ({c['sent_at']}): {content}"


def _render_headline_line(c: dict, *, show_place: bool = False,
                          show_reply: bool = True) -> str:
    """The one headline line for a crossing, with NO list prefix (umwelt #871).

    search_crossings and whats_new render their list lines as
    `"  · " + _render_headline_line(...)`; hear's headline form is the same
    `_render_crossing_line(c, "{title} ({size} chars)")` family. The
    standpoint-aware framing is whatever `_render_crossing_line` gives the row
    (rows without `viewer_relation` read `from {sender}`).

    `(N chars)` is ALWAYS present, even for a short title: it is the reader's
    exposure signal. A row with no size renders `(? chars)`. A missing title
    renders `(no title)`.

    show_place: insert ` @ {destination}` right after the framing and before
        ` ({sent_at})` -- search is the only cross-place surface. Skipped for
        the `sender` relation, whose framing already names the destination
        (search rows carry no viewer_relation today; this is future-proofing). Done by
        rendering the framing with EMPTY content and splicing before its
        fixed ` ({sent_at}): ` tail, so nothing in the title can misplace it.
    show_reply: append ` ↩ seq {reply_to}` when the row carries a reply_to.
    """
    title = c.get("title") or "(no title)"
    size = c.get("size")
    size_txt = "?" if size is None else size
    content = f"{title} ({size_txt} chars)"
    # A `sender` framing already names the destination ("you → X"), so
    # show_place adds nothing there and would print X twice.
    if show_place and c.get("viewer_relation") != "sender":
        tail = f" ({c['sent_at']}): "
        head = _render_crossing_line(c, "")
        line = f"{head[:-len(tail)]} @ {c['destination']}{tail}{content}"
    else:
        line = _render_crossing_line(c, content)
    if show_reply and c.get("reply_to") is not None:
        line += f" ↩ seq {c['reply_to']}"
    return line


@mcp.tool()
def crossings_for_agent(payload_json: str) -> str:
    """Read crossings addressed to you across EVERY place — including places
    you cannot currently enter. `crossings_here` only sees what has reached
    a single place you already stand in or name; a crossing addressed to you
    can land anywhere, so a place you can't enter would otherwise be a
    teaser you can never expand. This route has no such scope limit.

    Does NOT advance any cursor — and unlike `crossings_here`'s implicit
    per-place standpoint fallback, there is no cursor this route can resolve
    to on its own (it isn't place-scoped, so there's no single standpoint
    row to fall back on). Track and pass your own `since` to page forward.

    Every reply is at most `max_chars` (default and hard cap 6,000); a
    refused payload gets a short fixed message instead. When the rows do not fit, they are dropped from the NEWEST
    end: the OLDEST rows are kept as a contiguous prefix, each in the fullest
    form that fits (full body, then headline, then field-bounded), and a
    trailer says how many were not shown and the `since=` that continues. At
    least one row is always shown. The header still counts every row the
    world returned. When rows were dropped, the footer reports the last SHOWN
    seq as to_position (the world's own to_position, the newest seq it
    returned, is withheld) and tells you to pass that as `since` yourself.

    Args:
        payload_json: JSON object (as a string) with these fields:
            agent (str, required): The agent whose addressed crossings to
                read. Today the world requires this to equal your own
                identity and rejects otherwise — this is NOT a security
                gate: identity is caller-declared everywhere in this
                codebase until credentials land. It exists so a
                cross-agent read fails LOUDLY instead of quietly
                succeeding.
            since (int, optional): A specific seq to read from. Default:
                omitted (read everything).
            limit (int, optional): Cap the read to the newest `limit`
                crossings (server-clamped, same bound as crossings_here).
                Default: omitted (unbounded).
            max_chars (int, optional): ceiling on the reply's total length.
                Clamped to 6,000 (may only lower it, never raise it); must
                be an integer (not a bool); refused outright, with no call
                to the world, if below the minimum viable size
                (`_CROSSINGS_FOR_AGENT_MIN_VIABLE_CHARS`).
    """
    params, err = _parse_payload(
        payload_json, required=frozenset({"agent"}),
        allowed=frozenset({"agent", "since", "limit", "max_chars"}))
    if err:
        return _clip_reply(err, _HEAR_MAX_CHARS_CAP)
    budget, err = _crossings_budget(
        params, _CROSSINGS_FOR_AGENT_MIN_VIABLE_CHARS, "crossings_for_agent")
    if err:
        return err
    agent = params["agent"]
    req: dict[str, Any] = {"viewer": _agent(), "agent": agent}
    if "since" in params:
        req["since"] = params["since"]
    if "limit" in params:
        req["limit"] = params["limit"]
    try:
        resp = _post("/umwelt/crossings_for_agent", req)
    except WorldUnreachable as e:
        return _clip_reply(
            f"⚠️ the world is unreachable at {WORLD_URL}: {e}", budget)
    if (e := _err(resp)):
        return _clip_reply(e, budget)
    crossings = resp.get("crossings", [])
    if not crossings:
        return _clip_reply(
            f"No crossings are addressed to {agent} (across every place).",
            budget)
    lines = [_CROSSINGS_FOR_AGENT_HEADER.format(n=len(crossings), agent=agent)]
    for c in crossings:
        content = c.get("body") or (f"→ {c['body_ref']}" if c.get("body_ref")
                                    else "(a wordless signal)")
        lines.append(f"  · {_render_crossing_line(c, content)}")
    lines.append(f"\n(to_position {resp.get('to_position')}; "
                 f"cursor NOT advanced — pass since to page forward yourself)")
    full = "\n".join(lines)
    if len(full) <= budget:
        return full
    return _render_crossings_capped(
        crossings, budget,
        _CROSSINGS_FOR_AGENT_HEADER.format(
            n=_hear_clamp_num(len(crossings)),
            agent=_hear_clip(agent, _HEAR_NAME_CAP)),
        lambda last, more: _CROSSINGS_FOR_AGENT_TRAILER.format(
            more=_hear_clamp_num(more), last=last,
            agent=_cmd_id(agent, "<this same agent>")),
        lambda last: _CROSSINGS_FOR_AGENT_CAPPED_FOOTER.format(last=last),
        None)


def _load_whats_new_cursor(agent: str) -> dict[str, Any] | None:
    """Read this agent's stored {position, set_at} entry from
    `_WHATS_NEW_CURSOR_FILE`, or None on any absence/corruption/missing-
    entry condition -- mirrors umwelt-bell.py's load_cursors() try/except-
    returns-empty posture (a missing or unreadable cursor file is "no
    cursor yet", never an error the caller has to handle)."""
    try:
        with open(_WHATS_NEW_CURSOR_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(agent)
    if not isinstance(entry, dict) or not isinstance(entry.get("position"), int):
        return None
    return entry


def _save_whats_new_cursor(agent: str, position: int) -> None:
    """Advance this agent's stored cursor to `position` (mark_seen=true's
    only write path). Same atomic tmp-file + os.replace discipline as
    umwelt-bell.py's save_cursors() -- never leaves a torn/partial file
    behind if the process dies mid-write. Preserves every OTHER agent's
    entry in the same file (keyed by agent, umwelt#857 D5's contract) by
    reading the existing file first; a missing/corrupt file starts fresh
    rather than failing the write."""
    try:
        with open(_WHATS_NEW_CURSOR_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[agent] = {
        "position": position,
        "set_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    _WHATS_NEW_CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _WHATS_NEW_CURSOR_FILE.with_name(_WHATS_NEW_CURSOR_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _WHATS_NEW_CURSOR_FILE)


def _render_whats_new_headline(h: dict) -> str:
    """One headline line: `"  · " + _render_headline_line(h)` (umwelt #871),
    the same list prefix and line family search and hear use. Reads only the
    exact envelope fields the world route's headline shape carries
    (seq/sender/title/size/sent_at/reply_to).
    Never reads `body`/`body_ref` — those keys don't exist on this dict at
    all (the world route never puts them there), so there is no field here
    that could leak one even by accident."""
    return "  · " + _render_headline_line(h)


def _render_whats_new_body(resp: dict, baseline: str,
                            cap: int | None = None) -> str:
    """The rendered text of `_whats_new_render` (see there)."""
    return _whats_new_render(resp, baseline, cap)[0]


def _whats_new_render(resp: dict, baseline: str, cap: int | None = None,
                      note_unseen: bool = False) -> tuple[str, int]:
    """Returns `(text, whole_places_dropped)`. `note_unseen` appends the
    `(not marked seen ...)` line when whole places were dropped -- the caller
    passes it for `mark_seen=true`, and does not advance the cursor then.

    Pure render function, decoupled from the HTTP layer so it's directly
    testable against a hand-built or real world response dict (umwelt#857
    D5 test 6/7): baseline line, then every place's header line (label,
    id, count -- ALWAYS shown, never dropped), then that place's headline
    detail lines, then an addressed-count header + its headline lines.

    `cap` defaults to `None`, which reads the CURRENT value of the module
    constant `_WHATS_NEW_RENDER_CAP` at call time -- deliberately not bound
    as `cap: int = _WHATS_NEW_RENDER_CAP` in the signature, since a default
    argument value is frozen at function-DEFINITION time in Python; that
    would make a test's `monkeypatch.setattr(m, "_WHATS_NEW_RENDER_CAP",
    ...)` silently no-op against this function's own default while still
    working against every other reader of the module attribute.

    Bounding (umwelt#857 D5's contract): headline DETAIL lines are dropped
    first, from the tail of the overall render order, when the full text
    would exceed `cap` -- header/count lines never drop. Ends with an
    honest "+N more headlines" line whenever any were dropped; omitted
    entirely when nothing was.

    D8 (umwelt#862) will replace the literal `cap` default with the shared
    budget renderer once it lands -- this function's `cap` param is
    already the seam that swap targets.
    """
    if cap is None:
        cap = _WHATS_NEW_RENDER_CAP
    places = resp.get("places", [])
    addressed = resp.get("addressed", [])

    def _header(p: dict, clip: bool) -> str:
        label = _seq_clip(p["label"], _LIST_FIELD_CLIP) if clip else p["label"]
        return f"{label} [{p['place_id']}] — {p['count']} new:"

    addressed_header = (f"Addressed to you ({len(addressed)}, "
                        "places you can't enter):")

    def _build(clip: bool) -> list[tuple[bool, str]]:
        # (is_mandatory, line) in final display order -- mandatory lines never
        # drop; optional (headline) lines are what the cap sheds first.
        segs: list[tuple[bool, str]] = [(True, baseline)]
        if not places and not addressed:
            segs.append((True, "Nothing new."))
        for p in places:
            segs.append((True, _header(p, clip)))
            for h in p.get("headlines", []):
                segs.append((False, _render_whats_new_headline(h)))
        if addressed:
            segs.append((True, addressed_header))
            for h in addressed:
                segs.append((False, _render_whats_new_headline(h)))
        return segs

    segments = _build(False)
    full_text = "\n".join(line for _, line in segments)
    if len(full_text) <= cap:
        return full_text, 0

    # umwelt#904: the mandatory skeleton (baseline + every header + the
    # addressed header) is what headline-shedding cannot shrink. When even it
    # plus the headline trailer overflows, first clip each place label (the
    # place_id is never clipped); if the clipped skeleton still overflows,
    # drop WHOLE places from the tail. One forward pass, no re-render.
    def _skeleton_fits(segs: list[tuple[bool, str]]) -> bool:
        n_optional = sum(1 for is_m, _ in segs if not is_m)
        text = "\n".join([line for is_m, line in segs if is_m]
                         + [f"+{n_optional} more headlines"])
        return len(text) <= cap

    if not _skeleton_fits(segments):
        segments = _build(True)
        clipped_text = "\n".join(line for _, line in segments)
        if len(clipped_text) <= cap:
            return clipped_text, 0
        if not _skeleton_fits(segments):
            trailer_at = lambda n: (f"+{n} more place{'s' if n != 1 else ''} "
                                    "(headlines not shown)")
            # Never a bare header: with the headlines gone, the addressed
            # section says so and where to read it.
            addressed_line = (addressed_header
                              + " not shown — see crossings_for_agent")
            note_at = lambda n: (f"(not marked seen: {n} place(s) were not "
                                 "shown; map(unread_only=true) lists them, "
                                 "then crossings_here reads one)")
            room = cap - len(baseline) - (len(addressed_line) + 1
                                          if addressed else 0)
            room -= len(trailer_at(len(places))) + 1
            if note_unseen:
                room -= len(note_at(len(places))) + 1
            lines = [baseline]
            kept = 0
            for p in places:
                hdr = _header(p, True)
                if len(hdr) + 1 > room and kept:
                    break
                lines.append(hdr)
                room -= len(hdr) + 1
                kept += 1
            if kept == len(places) and kept > 1:
                # Every header fit but the headline trailer did not: drop
                # the last place so the trailer states something true.
                lines.pop()
                kept -= 1
            dropped_places = len(places) - kept
            lines.append(trailer_at(dropped_places))
            if addressed:
                lines.append(addressed_line)
            if note_unseen:
                lines.append(note_at(dropped_places))
            return _clip_reply("\n".join(lines), cap), dropped_places

    # Over cap -- shed optional lines from the tail until the mandatory
    # skeleton (+ whatever optional lines still fit, + the trailer) fits.
    # `kept` carries each line's own `is_mandatory` flag alongside its
    # text, so a shedding decision is never made by comparing rendered
    # TEXT -- two lines with byte-identical text (e.g. a headline whose
    # rendered form happens to match a mandatory header line) can never
    # be confused about which one may be dropped.
    kept: list[tuple[bool, str]] = []
    dropped = 0
    budget = cap
    for is_mandatory, line in segments:
        cost = len(line) + (1 if kept else 0)  # joining newline, once started
        if is_mandatory:
            kept.append((is_mandatory, line))
            budget -= cost
        elif cost <= budget:
            kept.append((is_mandatory, line))
            budget -= cost
        else:
            dropped += 1

    # The trailer itself must fit -- shed already-kept optional lines from
    # the end (never a mandatory one) until it does. Digit-count of
    # `dropped` can only grow as we shed more, so re-check each time.
    def _fits(n_kept: list[tuple[bool, str]], n_dropped: int) -> tuple[str, bool]:
        trailer = f"+{n_dropped} more headlines"
        text = "\n".join([line for _, line in n_kept] + [trailer])
        return text, len(text) <= cap

    text, ok = _fits(kept, dropped)
    while not ok and any(not is_m for is_m, _ in kept):
        for i in range(len(kept) - 1, -1, -1):
            if not kept[i][0]:
                del kept[i]
                dropped += 1
                break
        text, ok = _fits(kept, dropped)
    return text, 0


@mcp.tool()
def whats_new(payload_json: str = "{}") -> str:
    """What's new across every place you may see, since your own tracked
    cursor (or an explicit `since`) — the agent-side half of umwelt#857 D5.
    The world route behind this (`/umwelt/whats_new`) is stateless and
    keeps no memory of what you've read (Lei's ruling); THIS tool is what
    turns that into a felt delta — it tracks your cursor in
    `~/.umwelt/whats-new-cursor.json`, keyed by your own agent name.

    Reading never marks anything seen — only `mark_seen=true` advances your
    stored cursor, and only to this call's own `to_position`. The rendered
    output always states its baseline explicitly ("new since seq N ...")
    so you can see what "new" is relative to; after a compaction, pass an
    explicit `since` yourself rather than trusting a cursor you can no
    longer recall setting.

    Args:
        payload_json: JSON object (as a string) with these fields, all
            optional:
            since (int, optional): read from this seq instead of your
                stored cursor. Explicit — use this to reset deliberately
                (e.g. after a compaction). Default: your stored cursor, or
                0 if you have none yet.
            per_place (int, optional): headlines per place, clamped 1..10
                by the world. Default: 3.
            mark_seen (bool, optional): advance your stored cursor to this
                call's `to_position` once the world has answered. Default:
                false — a plain read marks nothing.

    The reply is bounded at 6,000 chars: headlines drop first, then, if the
    place headers alone overflow, whole places from the end, counted in a
    `+N more places` line. When whole places dropped, `mark_seen` does not
    advance your cursor (the reply says so).
    """
    params, err = _parse_payload(
        payload_json, allowed=frozenset({"since", "per_place", "mark_seen"}))
    if err:
        return _list_reply(err)

    agent = _agent()
    cursor = _load_whats_new_cursor(agent)
    if "since" in params:
        since = params["since"]
        baseline = f"new since seq {since} (explicit since)"
    elif cursor is not None:
        since = cursor["position"]
        baseline = f"new since seq {since} (your cursor, set at {cursor['set_at']})"
    else:
        since = 0
        baseline = f"new since seq {since} (no cursor yet)"

    # The world route is stateless by design (see docstring above) — it
    # never receives anything but the `since` THIS call just computed,
    # never a value it might have of its own.
    req: dict[str, Any] = {"viewer": agent, "since": since}
    if "per_place" in params:
        req["per_place"] = params["per_place"]

    try:
        resp = _post("/umwelt/whats_new", req)
    except WorldUnreachable as e:
        return _list_reply(f"⚠️ the world is unreachable at {WORLD_URL}: {e}")
    if (e := _err(resp)):
        return _list_reply(e)

    mark_seen = bool(params.get("mark_seen"))
    text, dropped_places = _whats_new_render(resp, baseline,
                                             note_unseen=mark_seen)
    # Whole places dropped: their ids and counts were never shown, so this
    # summary must not claim they were seen -- the cursor stays put.
    if mark_seen and not dropped_places:
        _save_whats_new_cursor(agent, resp.get("to_position", since))
    return text


@mcp.tool()
def search_crossings(payload_json: str) -> str:
    """Find crossings by text — HEADLINES ONLY, never bodies. Open the one
    you want with `record`/`crossing_lookup`-backed reads (or re-visit the
    place and read it in context) to see its full content; this tool tells
    you WHICH crossing to open, not what it says.

    Splits `query` on whitespace into terms (max 8); every term must match
    (AND) a crossing's body OR its stored title, case-insensitive for ASCII
    only. `%`, `_`, and `\\` are literal, not wildcards. Newest first;
    `limit` defaults to 20, clamped to [1, 50] server-side.

    A crossing in a private place you don't own is never returned by this
    search, and its absence looks exactly like it never existed — there's
    no count of what was hidden.

    Args:
        payload_json: JSON object (as a string) with these fields:
            query (str, required): whitespace-split search terms (max 8,
                200 characters total). Must be non-blank after stripping.
            place (str, optional): restrict to one destination place id.
            sender (str, optional): restrict to one sender (exact match,
                case-insensitive).
            limit (int, optional): cap on how many of the newest matching
                crossings to return (server-clamped to [1, 50]).
                Default: 20.

    The reply is bounded at 6,000 chars: over that, rows past the last that
    fits drop (newest kept), long names/titles in shown rows are clipped
    (the seq never is), and a `(+N more not shown ...)` line counts them.
    """
    params, err = _parse_payload(
        payload_json, required=frozenset({"query"}),
        allowed=frozenset({"query", "place", "sender", "limit"}))
    if err:
        return _list_reply(err)
    req: dict[str, Any] = {"agent": _agent(), "query": params["query"]}
    if "place" in params:
        req["place"] = params["place"]
    if "sender" in params:
        req["sender"] = params["sender"]
    if "limit" in params:
        req["limit"] = params["limit"]
    try:
        resp = _post("/umwelt/search_crossings", req)
    except WorldUnreachable as e:
        return _list_reply(f"⚠️ the world is unreachable at {WORLD_URL}: {e}")
    if (e := _err(resp)):
        return _list_reply(e)
    results = resp.get("results", [])
    if not results:
        return _list_reply(f"No crossings match {params['query']!r}.")
    rows = [["  · " + _render_headline_line(r, show_place=True),
             "  · " + _seq_clip(_render_headline_line(
                 _clip_search_row(r), show_place=True), _SEQ_LINE_CLIP)]
            for r in results]
    more = ["\n(more match — narrow your query or raise limit "
            "to see further back)"] if resp.get("truncated") else []
    # When rows are dropped, the world's own "raise limit" hint would
    # contradict our trailer, so it is left out; when every row is shown
    # (clipped), the hint is kept.
    return _fit_rows([f"{len(results)} crossing(s) match {params['query']!r}:"],
                     rows, more,
                     "(+{n} more not shown — narrow your query, filter by place "
                     "or sender, or lower limit)",
                     _HEAR_MAX_CHARS_CAP, cut_tail=[])


# --- open / thread / reply (umwelt#862 D3, the client half) ---------------
#
# The seq-keyed readers. Every one validates its whole payload BEFORE any
# world call, renders from an explicit field allowlist (never `**resp`, so a
# buggy server response can never surface a uuid), and renders the world's
# uniform 404 as ONE fixed sentence: the client does not depend on the
# server keeping absent and unauthorized indistinguishable.

# Mirrors server._MAX_SEQ (SQLite INTEGER); the client cannot import the
# server module, so the bound is restated here.
_MAX_SEQ = 2**63 - 1
# server._SEARCH_CROSSINGS_HEADLINE_FIELDS minus `id` (the uuid is
# crossing_lookup's capability and is never rendered next to a seq).
_SEQ_READER_FIELDS = ("seq", "sender", "destination", "to_agent", "sent_at",
                      "title", "size", "reply_to", "thread_root")
_SEQ_ERR_CLIP = 300
_SEQ_LINE_CLIP = 600
_OPEN_TEXT_CLIP = 500
# Room kept for the `[chars a-b of N]` label, the guidance line and the two
# newlines when a body has to be cut to fit the render cap.
_OPEN_FRAME_RESERVE = 240
_SEQ_BAD = (f"⚠️ seq must be an integer from 1 to {_MAX_SEQ}, or "
            '"<place>:<seq>" with a plain integer seq')


def _seq_clip(text: Any, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _clip_reply(text: str, budget: int) -> str:
    """`text` if it fits `budget`, else its first budget-1 chars and a `…`
    (same prefix, no newline added). The final clip for replies whose body
    is world-controlled (error text, an exception message) so that EVERY
    reply of `open`/`thread`, errors included, is <= its budget."""
    return text if len(text) <= budget else text[:budget - 1] + "…"


# umwelt#904 (part of #862 D8): the list verbs (search_crossings, look_in,
# inventory, bin, landfill, map) bound their whole reply at the fixed D8
# budget. A row's free-text fields are clipped to this many chars when the
# row has to be shortened; its id never is.
_LIST_FIELD_CLIP = 80
_LIST_EDGE_LINE_CLIP = 500  # a head / tail line, in the over-budget render


def _list_reply(text: str) -> str:
    """Final clip for a list verb's error / parse / unreachable reply, whose
    text can carry world- or caller-controlled content."""
    return _clip_reply(text, _HEAR_MAX_CHARS_CAP)


def _fit_rows(head: list[str], rows: list[list[str]], tail: list[str],
              trailer: str, budget: int,
              cut_tail: list[str] | None = None) -> str:
    """Render `head`, `rows`, `tail` (newline-joined) under `budget` chars.

    Each row is a list of candidate renderings, FULLEST FIRST (a candidate
    may span several lines). When the all-fullest render fits it is returned
    byte-identically. Otherwise rows are kept as a PREFIX of the given order:
    each takes its fullest candidate that still fits, the first row takes its
    last (shortest) candidate if none fits so a returned row is never lost,
    and the first row that fits in no form ends the list. The dropped count
    goes into `trailer` (a `{n}` template) placed after the rows, counted in
    the budget. `tail` lines (the mandatory summary lines) always survive;
    `cut_tail`, when given, replaces `tail` only when rows are dropped: if
    every row still fits once shortened, the full `tail` is kept and there is
    no trailer; if every row fits but the full `tail` cannot, the last row is
    dropped so the trailer says so.
    """
    full = "\n".join(head + [r[0] for r in rows] + tail)
    if len(full) <= budget:
        return full
    head = [_clip_reply(h, _LIST_EDGE_LINE_CLIP) for h in head]

    def _prefix(reserve: int) -> list[str]:
        used = len("\n".join(head))
        kept: list[str] = []
        for cands in rows:
            sep = 1 if (head or kept) else 0
            room = budget - used - reserve - sep
            pick = next((c for c in cands if len(c) <= room), None)
            if pick is None:
                if kept:
                    break
                pick = cands[-1]
            kept.append(pick)
            used += sep + len(pick)
        return kept

    # First try to show EVERY row (shortened as needed) with the full `tail`:
    # nothing dropped means no trailer, and the world's own tail hint stays.
    full_tail = [_clip_reply(t, _LIST_EDGE_LINE_CLIP) for t in tail]
    kept = _prefix(sum(len(t) + 1 for t in full_tail))
    if len(kept) == len(rows):
        return _clip_reply("\n".join(head + kept + full_tail), budget)
    # Rows must drop: the trailer is reserved, and `cut_tail` (when given)
    # replaces `tail`.
    tail = (full_tail if cut_tail is None
            else [_clip_reply(t, _LIST_EDGE_LINE_CLIP) for t in cut_tail])
    reserved = (len(trailer.format(n=len(rows))) + 1
                + sum(len(t) + 1 for t in tail))
    kept = _prefix(reserved)
    if len(kept) == len(rows):
        # Pass 2 (smaller reserve, shorter early rows) can show every row: the
        # full tail must not vanish silently then. Keep it if it fits; if it
        # cannot fit, drop the last row so the trailer states the loss (a lone
        # row is never dropped: its tail is then clipped by the backstop).
        text = "\n".join(head + kept + full_tail)
        if len(text) <= budget or len(kept) == 1:
            return _clip_reply(text, budget)
        kept.pop()
    dropped = len(rows) - len(kept)
    lines = head + kept + ([trailer.format(n=dropped)] if dropped else []) + tail
    return _clip_reply("\n".join(lines), budget)


def _not_open(seq: int) -> str:
    """The ONE sentence for the world's uniform 404 (absent == unauthorized)."""
    return f"No crossing seq {seq} is open to you."


def _parse_seq(value: Any) -> tuple[int | None, str | None]:
    """A seq as an int >= 1, or the `"<place>:<seq>"` string form (the place
    part is ignored: seq is global). Nothing else: no bool, no float, no bare
    numeric string, no extra colons, no whitespace. The error never echoes
    the input."""
    if isinstance(value, bool):
        return None, _SEQ_BAD
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        parts = value.split(":")
        if (len(parts) != 2 or not parts[0] or not parts[1]
                or any(ch.isspace() for ch in value)
                or not (parts[1].isascii() and parts[1].isdigit())
                or len(parts[1]) > 40):
            return None, _SEQ_BAD
        n = int(parts[1])
    else:
        return None, _SEQ_BAD
    if not 1 <= n <= _MAX_SEQ:
        return None, _SEQ_BAD
    return n, None


def _parse_seq_payload(payload_json: str, allowed: frozenset[str],
                       ) -> tuple[dict[str, Any] | None, int | None, str | None]:
    """(params, seq, None) or (None, None, one-line bounded error). Never
    raises; makes no world call."""
    try:
        params, err = _parse_payload(
            payload_json, required=frozenset({"seq"}), allowed=allowed)
    except RecursionError:
        return None, None, "⚠️ payload_json is nested too deeply"
    if err:
        return None, None, _seq_clip(err, _SEQ_ERR_CLIP)
    seq, err = _parse_seq(params["seq"])
    if err:
        return None, None, err
    return params, seq, None


def _plain_int(value: Any, minimum: int) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= minimum)


def _clip_search_row(r: dict) -> dict:
    """`r` with its free-text fields clipped (umwelt#904) so its headline
    stays short; `seq` and the other non-text fields are untouched."""
    out = dict(r)
    for k in ("sender", "destination", "to_agent", "title", "sent_at"):
        if isinstance(out.get(k), str):
            out[k] = _seq_clip(out[k], _LIST_FIELD_CLIP)
    return out


def _seq_headline(c: dict, *, show_place: bool = False) -> str:
    """One headline line, rendered from the allowlisted fields ONLY."""
    row = {k: c.get(k) for k in _SEQ_READER_FIELDS}
    return _seq_clip(_render_headline_line(row, show_place=show_place),
                     _SEQ_LINE_CLIP)


def _render_open(resp: dict, seq: int, offset: int | None) -> str:
    head = _seq_headline(resp)
    cap = _HEAR_MAX_CHARS_CAP
    body = resp.get("body")
    size = resp.get("body_size")
    size_txt = size if _plain_int(size, 0) else "?"

    def page(text: str, start: int, nxt: Any) -> str:
        room = cap - len(head) - _OPEN_FRAME_RESERVE
        if len(text) > room:  # a page is <= the server's page max, so this
            text = text[:room]  # only fires for a non-conforming server
            nxt = start + room
        if nxt is None:
            guide = "(end)"
        elif _plain_int(nxt, 0):
            guide = f'(more: open {{"seq": {seq}, "offset": {nxt}}})'
        else:
            guide = "(page position unknown)"
        label = f"[chars {start}-{start + len(text)} of {size_txt}]"
        return f"{head}\n{label}\n{text}\n{guide}"

    if "next_offset" in resp and isinstance(body, str):
        return page(body, offset or 0, resp["next_offset"])
    if resp.get("guarded"):
        warning = resp.get("warning")
        if not isinstance(warning, str) or not warning:
            warning = "this body is too long to return whole."
        return (f"{head}\n{_seq_clip(warning, _OPEN_TEXT_CLIP)}\n"
                f'Read it in pages: open {{"seq": {seq}, "offset": 0}}')
    if isinstance(body, str):
        whole = f"{head}\n{body}"
        if len(whole) <= cap:
            return whole
        # The server returns bodies up to its guard (8000 chars) whole, more
        # than the render cap: show what fits and say where to continue.
        room = cap - len(head) - _OPEN_FRAME_RESERVE
        return page(body[:room], 0, room)
    ref = resp.get("body_ref")
    if isinstance(ref, str) and ref:
        return (f"{head}\n(no inline body; this is a pointer as stored, not "
                f"fetched: {_seq_clip(ref, _OPEN_TEXT_CLIP)})")
    return f"{head}\n(no body)"


@mcp.tool(name="open")
def open_(payload_json: str) -> str:
    """Open ONE crossing by its seq and read its body. (Registered as the
    verb `open`; the Python name avoids shadowing the builtin this module
    uses.)

    A short body prints whole after the headline. A long one is guarded: you
    get the headline, the world's warning, and how to page. Read a page with
    `offset` (and `limit`, at most 4,000 chars); each page says where the
    next one starts, or `(end)`. A crossing that carries only a pointer
    (`body_ref`) prints the pointer as stored, never the content it points
    to. Every reply, errors included, is capped at 6,000 chars.

    A crossing that does not exist and one you may not read get the SAME
    answer, one fixed sentence.

    Args:
        payload_json: JSON object (as a string) with these fields:
            seq (int or str, required): the crossing's seq, an integer >= 1,
                or "<place>:<seq>" (the place part is ignored).
            offset (int, optional): start of the page, in characters, >= 0.
            limit (int, optional): page length in characters, >= 1
                (server-clamped to 4,000).
    """
    params, seq, err = _parse_seq_payload(
        payload_json, frozenset({"seq", "offset", "limit"}))
    if err:
        return err
    req: dict[str, Any] = {"agent": _agent(), "seq": seq}
    for key, minimum in (("offset", 0), ("limit", 1)):
        if key in params:
            if not _plain_int(params[key], minimum):
                return f"⚠️ {key} must be an integer >= {minimum}"
            req[key] = params[key]
    try:
        resp = _post("/umwelt/open_crossing", req)
    except WorldUnreachable as e:
        return _clip_reply(
            f"⚠️ the world is unreachable at {WORLD_URL}: {e}",
            _HEAR_MAX_CHARS_CAP)
    if resp.get("_status") == 404:
        return _not_open(seq)
    if (e := _err(resp)):
        return _clip_reply(e, _HEAR_MAX_CHARS_CAP)
    return _render_open(resp, seq, req.get("offset"))


# --- _THREAD_MIN_VIABLE_CHARS derivation (thread's aggregate cap; the same
# shape as _HEAR_MIN_VIABLE_CHARS): the smallest max_chars at which thread()
# can still show ONE worst-case row plus every fixed piece. Below it thread()
# refuses outright, before any world call.
#
# header  "{N} crossing(s) in the thread of seq {seq}:" -- N is at most 200
#         (the engine's row limit) = 3 digits, seq at most 2**63-1 = 19
#         digits: len("200 crossing(s) in the thread of seq
#         9223372036854775807:") ................................... 57
# "\n" joining header to the first line ................................ 1
# one worst-case line: "  · " 4 + the headline, which _seq_headline clips
#         to _SEQ_LINE_CLIP (600) whatever the names/title/size are .. 604
# "\n" joining that line to the trailer ................................ 1
# drop trailer, the LONGER of its two wordings (the sub-cap one): "(+200 more
#         not shown — call thread with a larger max_chars (up to 6000), or
#         open a seq)" ............................................. 87
# blank line + engine notice "(truncated: the newest crossings are not
#         shown)": "\n\n" 2 + 47 ................................... 49
# (the notice is reserved even when the engine did not truncate, so the
# minimum is one fixed number, independent of the response.)
# Mirrors the engine's row bound for /umwelt/thread (server _THREAD_MAX); the
# client cannot import the server module, so it is restated here.
_THREAD_MAX_ROWS = 200
# Two trailer wordings, chosen by whether the effective budget is already the
# hard cap: below it a larger max_chars really helps; AT it, max_chars is
# clamped and thread has no paging, so the only way in is `open`.
_THREAD_MORE_TRAILER = ("(+{n} more not shown — call thread with a larger "
                        "max_chars (up to 6000), or open a seq)")
_THREAD_MORE_TRAILER_AT_CAP = ("(+{n} more not shown — thread's reply is "
                               "capped at 6000 chars; open a seq to read one)")
_THREAD_TRUNCATED_NOTICE = "(truncated: the newest crossings are not shown)"
_THREAD_MIN_VIABLE_CHARS = (
    len("200 crossing(s) in the thread of seq 9223372036854775807:") + 1
    + (4 + _SEQ_LINE_CLIP) + 1
    + max(len(_THREAD_MORE_TRAILER.format(n=200)),
          len(_THREAD_MORE_TRAILER_AT_CAP.format(n=200)))
    + 2 + len(_THREAD_TRUNCATED_NOTICE)
)


@mcp.tool()
def thread(payload_json: str) -> str:
    """List the thread a crossing belongs to, oldest first, as HEADLINES
    ONLY (no bodies; use `open` on the seq you want). Crossings you may not
    read are simply absent from the list. At most 200 are returned; a notice
    says when the newest ones were cut.

    Every reply, errors included, is bounded by `max_chars` (default and hard cap 6,000).
    When the headlines do not fit, rows are dropped from the NEWEST end: the
    OLDEST rows are kept, so the thread's root context is what you see, and
    a final line says how many were not shown. At least one row is always
    shown. The header still counts every row the world returned.

    A crossing that does not exist and one you may not read get the SAME
    answer, one fixed sentence.

    Args:
        payload_json: JSON object (as a string) with these fields:
            seq (int or str, required): any crossing in the thread, an
                integer >= 1, or "<place>:<seq>" (the place part is ignored).
            max_chars (int, optional): ceiling on the reply's total length.
                Clamped to 6,000 (may only lower it, never raise it); must
                be an integer (not a bool); refused outright, with no call
                to the world, if below the minimum viable size
                (`_THREAD_MIN_VIABLE_CHARS`).
    """
    params, seq, err = _parse_seq_payload(
        payload_json, frozenset({"seq", "max_chars"}))
    if err:
        return err
    max_chars = params.get("max_chars")
    if max_chars is not None and (
            isinstance(max_chars, bool) or not isinstance(max_chars, int)):
        return (f"⚠️ max_chars must be an integer, got "
                f"{type(max_chars).__name__} — omit it for the default "
                f"{_HEAR_MAX_CHARS_CAP}-char cap")
    budget = (min(max_chars, _HEAR_MAX_CHARS_CAP) if max_chars is not None
              else _HEAR_MAX_CHARS_CAP)
    if budget < _THREAD_MIN_VIABLE_CHARS:
        return (f"⚠️ max_chars={max(budget, -10 ** 9)} is below thread's "
                f"minimum viable budget of {_THREAD_MIN_VIABLE_CHARS} chars "
                f"— raise max_chars, or omit it for the default "
                f"{_HEAR_MAX_CHARS_CAP}-char cap")
    try:
        resp = _post("/umwelt/thread", {"agent": _agent(), "seq": seq})
    except WorldUnreachable as e:
        return _clip_reply(
            f"⚠️ the world is unreachable at {WORLD_URL}: {e}", budget)
    if resp.get("_status") == 404:
        return _not_open(seq)
    if (e := _err(resp)):
        return _clip_reply(e, budget)
    rows = resp.get("thread")
    if not isinstance(rows, list):
        rows = []
    rows = [r for r in rows if isinstance(r, dict)]
    truncated = bool(resp.get("truncated"))
    if len(rows) > _THREAD_MAX_ROWS:
        # A non-conforming world: keep the oldest rows and say the newest
        # were not shown, exactly as the engine's own notice does.
        rows = rows[:_THREAD_MAX_ROWS]
        truncated = True
    header = f"{len(rows)} crossing(s) in the thread of seq {seq}:"
    lines = ["  · " + _seq_headline(r, show_place=True) for r in rows]

    def build(k: int) -> str:
        parts = [header, *lines[:k]]
        if k < len(lines):
            tmpl = (_THREAD_MORE_TRAILER_AT_CAP
                    if budget >= _HEAR_MAX_CHARS_CAP else _THREAD_MORE_TRAILER)
            parts.append(tmpl.format(n=len(lines) - k))
        if truncated:
            parts.append("\n" + _THREAD_TRUNCATED_NOTICE)
        return "\n".join(parts)

    for k in range(len(lines), 0, -1):
        out = build(k)
        if len(out) <= budget:
            return out
    out = build(0)
    # Unreachable: rows are clipped to _THREAD_MAX_ROWS, so the header and
    # trailer are at most 3 digits and _THREAD_MIN_VIABLE_CHARS is exact
    # (k=1 always fits). Kept as a belt-and-braces guard on the bound.
    return out if len(out) <= budget else out[:budget - 1] + "…"


@mcp.tool()
def reply(payload_json: str) -> str:
    """Reply to a crossing: send a new crossing into the SAME place as the
    one you are answering, linked to it (it joins its thread).

    You must be able to read the crossing you reply to. If you cannot (or it
    does not exist), you get the one fixed "not open to you" sentence and
    NOTHING is sent. The reply is addressed to the original's author unless
    you pass `to_agent` (or you ARE the author, then it is a broadcast).

    Args:
        payload_json: JSON object (as a string) with these fields:
            seq (int or str, required): the crossing to answer, an integer
                >= 1, or "<place>:<seq>" (the place part is ignored).
            body (str, required): the reply text, non-empty.
            to_agent (str, optional): who to address it to. Default: the
                original crossing's author.
    """
    params, seq, err = _parse_seq_payload(
        payload_json, frozenset({"seq", "body", "to_agent"}))
    if err:
        return err
    body = params.get("body")
    if "body" not in params or not isinstance(body, str) or body == "":
        return "⚠️ body must be a non-empty string"
    to_agent = params.get("to_agent", "")
    if not isinstance(to_agent, str):
        return "⚠️ to_agent must be a string"
    agent = _agent()
    try:
        parent = _post("/umwelt/open_crossing",
                       {"agent": agent, "seq": seq, "limit": 1})
        if parent.get("_status") == 404:
            return _not_open(seq)
        if (e := _err(parent)):
            return e
        destination = parent.get("destination")
        if not isinstance(destination, str) or not destination:
            return (f"⚠️ crossing seq {seq} did not name a usable place; "
                    "nothing was sent")
        if not to_agent:
            author = parent.get("sender")
            if (isinstance(author, str) and author
                    and _ids.canonical_agent(author)
                    != _ids.canonical_agent(agent)):
                to_agent = author
        req: dict[str, Any] = {
            "agent": agent, "destination": destination, "body": body,
            "reply_to": seq,
            "sent_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")}
        if (lineage_err := _stamp_sender_lineage(req)):
            return lineage_err
        if to_agent:
            req["to_agent"] = to_agent
        resp = _post("/umwelt/send_crossing", req)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    to = f" to {to_agent}" if to_agent else ""
    return (f"You replied ↩ seq {seq}{to} in {destination}. The world "
            f"witnessed it cross (seq {resp.get('seq')}).")


@mcp.tool()
def trace(payload_json: str) -> str:
    """Mark crossings in a place as read — commit your standpoint cursor.

    position: a crossing id, or "latest" (default) to catch up fully.

    Args:
        payload_json: JSON object (as a string) with these fields:
            place (str, required): The place id.
            position (str, optional): A crossing id, or "latest" to catch up
                fully. Default: "latest".
    """
    params, err = _parse_payload(payload_json, required=frozenset({"place"}),
                                  allowed=frozenset({"place", "position"}))
    if err:
        return err
    place = params["place"]
    position = params.get("position", "latest")
    try:
        resp = _post("/umwelt/trace",
                     {"agent": _agent(), "place": place, "position": position})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return (f"You've caught up in {place} — marked read through "
            f"seq {resp.get('last_position')}.")


# --- carry, act, create_place -----------------------------------------

@mcp.tool()
def take(payload_json: str) -> str:
    """Pick up a thing (a thing id or the name you see) — it moves from the
    place to your hands.

    `thing` resolves (umwelt #185) against the current place's things only
    — take's target must already be sitting in the place, not carried. An
    id match wins outright, else a case-insensitive exact name match; zero
    or more than one candidate is a loud, named error. Avatars are never
    candidates here.

    Budget-gated: over your carry capacity 422s with the load/capacity/
    overweight breakdown and the three outs (put something down, bag it,
    or send it home).

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): A thing id or the name you see.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"thing"}),
                                  allowed=frozenset({"thing"}))
    if err:
        return err
    thing = params["thing"]
    agent = _agent()
    thing_id, err = _resolve_thing_ref(agent, thing, include_inventory=False)
    if err:
        return err
    try:
        resp = _post("/umwelt/take", {"agent": agent, "thing": thing_id})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return f"You take it. Carrying {resp['load']}/{resp['capacity']}."


@mcp.tool()
def drop(payload_json: str) -> str:
    """Drop a carried thing (a thing id or the name you see) — it lands
    wherever YOU currently stand.

    `thing` resolves (umwelt #185, round 2) against your own inventory
    ONLY — you can only drop what you carry, so a same-named thing lying
    in the room is never a candidate. An id match wins outright, else a
    case-insensitive exact name match; zero or more than one candidate is
    a loud, named error. Avatars are never candidates here.

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): A thing id or the name you see.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"thing"}),
                                  allowed=frozenset({"thing"}))
    if err:
        return err
    thing = params["thing"]
    agent = _agent()
    thing_id, err = _resolve_thing_ref(agent, thing, include_place=False, include_inventory=True)
    if err:
        return err
    try:
        resp = _post("/umwelt/drop", {"agent": agent, "thing": thing_id})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return f"You drop it. It's here now, in {resp['place_id']}."


@mcp.tool()
def put(payload_json: str) -> str:
    """Put a thing (a thing id or the name you see) into a container (a
    thing id or the name you see) — capacity-gated on the container's own
    budget, not yours.

    `thing` resolves (umwelt #185) against the current place's things plus
    your own inventory; `container` resolves against the current place's
    things only. An id match wins outright, else a case-insensitive exact
    name match; zero or more than one candidate is a loud, named error.
    Avatars are never candidates here.

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): A thing id or the name you see.
            container (str, required): A container thing id or the name you see.
    """
    params, err = _parse_payload(payload_json,
                                  required=frozenset({"thing", "container"}),
                                  allowed=frozenset({"thing", "container"}))
    if err:
        return err
    thing = params["thing"]
    container = params["container"]
    agent = _agent()
    view, err = _fetch_place_view(agent)
    if err:
        return err
    thing_id, err = _resolve_thing_ref(agent, thing, include_inventory=True,
                                       place_view=view)
    if err:
        return err
    container_id, err = _resolve_thing_ref(agent, container,
                                           include_inventory=False,
                                           place_view=view)
    if err:
        return err
    try:
        resp = _post("/umwelt/put",
                     {"agent": agent, "thing": thing_id, "container": container_id})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return f"You put it away. The container now holds {resp['load']}/{resp['capacity']}."


@mcp.tool()
def take_out(payload_json: str) -> str:
    """Pull a thing (a thing id or the name you see) out of whatever
    container holds it, into your hands. No container arg — the server
    derives it from the thing's own holder. Budget-gated on YOUR carry
    capacity, same as take.

    `thing` resolves (umwelt #185) against the current place's things plus
    your own inventory — the same candidate set `use` sees. A bagged
    thing's own container contents aren't scanned (umwelt #185 scope: a
    thing already inside a bag isn't surfaced by either listing), so
    reference it by id in that case; the server remains the final
    authority either way.

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): A thing id or the name you see.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"thing"}),
                                  allowed=frozenset({"thing"}))
    if err:
        return err
    thing = params["thing"]
    agent = _agent()
    thing_id, err = _resolve_thing_ref(agent, thing, include_inventory=True)
    if err:
        return err
    try:
        resp = _post("/umwelt/take_out", {"agent": agent, "thing": thing_id})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return f"You take it out. Carrying {resp['load']}/{resp['capacity']}."


@mcp.tool()
def look_in(payload_json: str) -> str:
    """Render a container's contents on demand (a thing id or the name you
    see).

    `container` resolves (umwelt #185) against the current place's things
    only — an id match wins outright, else a case-insensitive exact name
    match; zero or more than one candidate is a loud, named error. Avatars
    are never candidates here.

    Args:
        payload_json: JSON object (as a string) with these fields:
            container (str, required): A container thing id or the name you see.

    The reply is bounded at 6,000 chars: over that, rows past the last that
    fits drop (the numbers on the rest stay), shown rows lose their long
    descriptions first, and a `(+N more not shown ...)` line counts what
    dropped. The `bag load` line always stays.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"container"}),
                                  allowed=frozenset({"container"}))
    if err:
        return _list_reply(err)
    container = params["container"]
    agent = _agent()
    container_id, err = _resolve_thing_ref(agent, container, include_inventory=False)
    if err:
        return _list_reply(err)
    try:
        resp = _post("/umwelt/look_in", {"agent": agent, "container": container_id})
    except WorldUnreachable as e:
        return _list_reply(f"⚠️ the world is unreachable at {WORLD_URL}: {e}")
    if (e := _err(resp)):
        return _list_reply(e)
    things = resp.get("things", [])
    head = [f"The container holds {len(things)} thing(s):"] if things else ["(empty)"]
    # umwelt #261 dotted-path follow-up: numbered so a caller can address
    # what they just saw as "N.M" — mirrors the place render's own
    # 1-indexed numbering (`_resolve_dotted_ordinal` indexes this same
    # order via a fresh look_in call, so the number shown here IS the M).
    # umwelt#904: the shortened form keeps the id and the number; only the
    # name and description (via #873's headline) are clipped.
    rows = [[f"  {i}. {t['name']} [{t['id']}] — {t['description']} ({t['weight_est']})",
             f"  {i}. " + _thing_headline(
                 {**t, "name": _seq_clip(t["name"], _LIST_FIELD_CLIP)})]
            for i, t in enumerate(things, 1)]
    return _fit_rows(head, rows, [f"bag load {resp['load']}/{resp['capacity']}"],
                     "(+{n} more not shown — look_at \"C.M\" opens one: C is this "
                     "container's number in the room, M the item's number here)",
                     _HEAR_MAX_CHARS_CAP)


@mcp.tool()
def inventory() -> str:
    """What you're carrying — things, load/capacity/headroom.

    The reply is bounded at 6,000 chars: over that, rows past the last that
    fits drop, shown rows lose their long descriptions first, and a
    `(+N more not shown ...)` line counts what dropped. The `carrying N
    things — load ...` line always stays."""
    try:
        resp = _post("/umwelt/inventory", {"agent": _agent()})
    except WorldUnreachable as e:
        return _list_reply(f"⚠️ the world is unreachable at {WORLD_URL}: {e}")
    if (e := _err(resp)):
        return _list_reply(e)
    things = resp.get("things", [])
    head = [f"You're carrying {len(things)} thing(s):"] if things else ["(empty)"]
    rows = [[f"  · {t['name']} [{t['id']}] — {t['description']} ({t['weight_est']})",
             "  · " + _thing_headline(
                 {**t, "name": _seq_clip(t["name"], _LIST_FIELD_CLIP)})]
            for t in things]
    tail = [f"carrying {len(things)} things — load {resp['load']}/{resp['capacity']} "
            f"(headroom {resp['headroom']})"]
    return _fit_rows(head, rows, tail,
                     "(+{n} more not shown — look_at one for its detail)",
                     _HEAR_MAX_CHARS_CAP)


@mcp.tool()
def send_home(payload_json: str) -> str:
    """Remote-move a thing (by id) to YOUR home place — the archive flow,
    not trash, just not here. Works on something you're holding, or
    something you authored at rest in your own current place.

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): The thing id.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"thing"}),
                                  allowed=frozenset({"thing"}))
    if err:
        return err
    thing = params["thing"]
    try:
        resp = _post("/umwelt/send_home", {"agent": _agent(), "thing": thing})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return f"You send it home — it's waiting for you at {resp['place_id']}."


@mcp.tool()
def act(payload_json: str) -> str:
    """Run a kind-declared action on a typed thing (a thing id or the name
    you see).

    `thing` resolves (umwelt #185) against the current place's things plus
    your own inventory — an id match wins outright, else a case-insensitive
    exact name match; zero or more than one candidate is a loud, named
    error. Avatars are never candidates here.

    args_json: a JSON object of the action's required/optional args (e.g.
    '{"comment": "looks good"}'). Absent or "{}" runs the action with no
    args. Actions are declarative rows on the thing's kind chain — no
    agent-authored code runs here.

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): A thing id or the name you see.
            action (str, required): The action name.
            args_json (str, optional): A JSON-encoded string of the action's
                required/optional args (e.g. '{"comment": "looks good"}').
                Default: "{}" (no args).
    """
    params, err = _parse_payload(payload_json, required=frozenset({"thing", "action"}),
                                  allowed=frozenset({"thing", "action", "args_json"}))
    if err:
        return err
    thing = params["thing"]
    action = params["action"]
    args_json = params.get("args_json", "{}")
    try:
        parsed_args = json.loads(args_json or "{}")
    except json.JSONDecodeError as e:
        return f"⚠️ args_json must be valid JSON: {e}"
    agent = _agent()
    thing_id, err = _resolve_thing_ref(agent, thing, include_inventory=True)
    if err:
        return err
    try:
        resp = _post("/umwelt/act", {"agent": agent, "thing": thing_id,
                                     "action": action, "args": parsed_args})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    out = f"{action} on {resp['thing_id']}: status={resp.get('status')}"
    if resp.get("effect_failures"):
        out += f"\n  ⚠️ effect_failures: {resp['effect_failures']}"
    if resp.get("effect_results"):
        out += f"\n  ✅ effect_results: {resp['effect_results']}"
    return out


@mcp.tool()
def create_place(payload_json: str) -> str:
    """Open a new purposed place.

    kind: "public" or "private" (default "public"). id (optional): an
    explicit place id (`place_<slug>`); if omitted, the world derives one
    from the label. parent (optional, umwelt #158): an existing place id to
    nest this one under; defaults to top-level — NEVER your own current
    place, the world never infers nesting from where you happen to stand.

    Args:
        payload_json: JSON object (as a string) with these fields:
            label (str, required): The place's label.
            description (str, required): The place's description.
            kind (str, optional): "public" or "private". Default: "public".
            id (str, optional): An explicit place id (`place_<slug>`); if
                omitted, the world derives one from the label. Default: "".
            parent (str, optional, umwelt #158): An existing place id to
                nest this one under. Default: "" (top-level).
    """
    params, err = _parse_payload(
        payload_json, required=frozenset({"label", "description"}),
        allowed=frozenset({"label", "description", "kind", "id", "parent"}))
    if err:
        return err
    label = params["label"]
    description = params["description"]
    kind = params.get("kind", "public")
    id = params.get("id", "")
    parent = params.get("parent", "")
    payload: dict[str, Any] = {"agent": _agent(), "label": label,
                                "description": description, "kind": kind}
    if id:
        payload["id"] = id
    if parent:
        payload["parent"] = parent
    try:
        resp = _post("/umwelt/create_place", payload)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    place = resp["place"]
    return f"You open a new place: {label} [{place['id']}]."


@mcp.tool()
def set_parent(payload_json: str) -> str:
    """Set (or clear) a place's parent (umwelt #158).

    place: the place id to reparent. parent: the new parent's place id;
    omit/empty to clear back to top-level. 409s if this would create a
    cycle (place_id becoming its own ancestor, directly or transitively).

    Args:
        payload_json: JSON object (as a string) with these fields:
            place (str, required): The place id to reparent.
            parent (str, optional): The new parent's place id. Default: ""
                (clear back to top-level).
    """
    params, err = _parse_payload(payload_json, required=frozenset({"place"}),
                                  allowed=frozenset({"place", "parent"}))
    if err:
        return err
    place = params["place"]
    parent = params.get("parent", "")
    payload: dict[str, Any] = {"agent": _agent(), "place": place}
    if parent:
        payload["parent"] = parent
    try:
        resp = _post("/umwelt/set_parent", payload)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    new_parent = resp.get("parent")
    return f"{place}'s parent is now {new_parent or '(top-level)'}."


@mcp.tool()
def update_place(payload_json: str) -> str:
    """Patch a place's metadata (place-lane-tiles spec) — the only mutable
    field a place has today (no MCP tool wraps update_thing itself, so this
    mirrors the closest actual sibling pattern in this file: create_place/
    set_parent's payload_json + metadata_json-string convention, e.g. forge's
    own metadata_json field).

    Writable when EITHER the place has no owner (engine-genesis places —
    the Square, Workshop, and similar founding places — any agent may
    curate their presentation) OR you are the place's owner (an
    agent-created private place, author-only, same posture as update_thing).
    This is a full REPLACE of `metadata`, not a merge — pass the complete
    object you want stored (e.g. include any existing keys you want to
    keep, alongside the new `lane_tiles`).

    Args:
        payload_json: JSON object (as a string) with these fields:
            place (str, required): The place id to patch.
            metadata_json (str, required): a JSON-encoded string of the
                full metadata object to store, e.g.
                '{"lane_tiles": {"sky": {"class": "sky-tile",
                "palette": "dawn"}}}'.
    """
    params, err = _parse_payload(
        payload_json, required=frozenset({"place", "metadata_json"}),
        allowed=frozenset({"place", "metadata_json"}))
    if err:
        return err
    place = params["place"]
    metadata_json = params["metadata_json"]
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError as e:
        return f"⚠️ metadata_json must be valid JSON: {e}"
    if not isinstance(metadata, dict):
        return "⚠️ metadata_json must be a JSON object"
    try:
        resp = _post("/umwelt/update_place",
                     {"agent": _agent(), "place": place,
                      "patch": {"metadata": metadata}})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return f"{place}'s metadata is updated."


@mcp.tool()
def add_link(payload_json: str) -> str:
    """Add a lateral link between two places (umwelt #158).

    src, dst: the two place ids. label: optional annotation for the link.
    one_way: if true, writes only src->dst; default writes both directions
    (the common "mutual" case). Idempotent: re-adding an existing link
    overwrites its label rather than erroring.

    Args:
        payload_json: JSON object (as a string) with these fields:
            src (str, required): The source place id.
            dst (str, required): The destination place id.
            label (str, optional): An annotation for the link. Default: "".
            one_way (bool, optional): If true, writes only src->dst. Default:
                False (writes both directions).
    """
    params, err = _parse_payload(payload_json, required=frozenset({"src", "dst"}),
                                  allowed=frozenset({"src", "dst", "label", "one_way"}))
    if err:
        return err
    src = params["src"]
    dst = params["dst"]
    label = params.get("label", "")
    one_way = params.get("one_way", False)
    payload: dict[str, Any] = {"agent": _agent(), "src": src, "dst": dst}
    if label:
        payload["label"] = label
    if one_way:
        payload["mutual"] = False
    try:
        resp = _post("/umwelt/add_link", payload)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    arrow = "->" if one_way else "<->"
    return f"Linked {src} {arrow} {dst}."


@mcp.tool()
def remove_link(payload_json: str) -> str:
    """Remove a lateral link between two places (umwelt #158).

    src, dst: the two place ids. one_way: if true, removes only src->dst;
    default removes both directions. A no-op (not an error) if the link
    doesn't exist.

    Args:
        payload_json: JSON object (as a string) with these fields:
            src (str, required): The source place id.
            dst (str, required): The destination place id.
            one_way (bool, optional): If true, removes only src->dst. Default:
                False (removes both directions).
    """
    params, err = _parse_payload(payload_json, required=frozenset({"src", "dst"}),
                                  allowed=frozenset({"src", "dst", "one_way"}))
    if err:
        return err
    src = params["src"]
    dst = params["dst"]
    one_way = params.get("one_way", False)
    payload: dict[str, Any] = {"agent": _agent(), "src": src, "dst": dst}
    if one_way:
        payload["mutual"] = False
    try:
        resp = _post("/umwelt/remove_link", payload)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    arrow = "->" if one_way else "<->"
    return f"Unlinked {src} {arrow} {dst}."


@mcp.tool()
def checklist(payload_json: str) -> str:
    """Check off / edit a checklist thing's items — author-only (umwelt
    #182, the bed substrate). A thing "is a checklist" iff its
    metadata.checklist is a list; use()/look_at a bed (or ask its author)
    to find one first — this tool never creates a checklist itself.

    op (closed set): check | uncheck | reset | add | remove | edit.
      - check / uncheck: requires `item` (1-based, matching render order).
      - reset: no item/text needed — clears every item's done flag (the
        morning reset).
      - add: requires `text` (<=200 chars); appends a new unchecked item.
        422s once the list is at 30 items.
      - remove: requires `item`; deletes that item.
      - edit: requires `item` and `text`; replaces that item's text.

    By default (umwelt#867, comms QoL D10) this prints a bounded one-item
    `headline` — the op's own effect, not the whole list — so a routine
    check/uncheck doesn't echo the entire checklist back into context. Pass
    `full: true` to get the full block instead (today's behavior); `full`
    is client-only and is never sent to the world.

    Examples: checklist('{"thing": "thing_bed_ariadne", "op": "check", "item": 2}')
              checklist('{"thing": "thing_bed_ariadne", "op": "add", "text": "Water the plants"}')
              checklist('{"thing": "thing_bed_ariadne", "op": "reset"}')
              checklist('{"thing": "thing_bed_ariadne", "op": "check", "item": 2, "full": true}')

    Args:
        payload_json: JSON object (as a string) with these fields:
            thing (str, required): The checklist thing id.
            op (str, required): One of check | uncheck | reset | add |
                remove | edit.
            item (int, optional): 1-based item index, matching render order.
                Required for check/uncheck/remove/edit. Default: absent.
            text (str, optional): Item text (<=200 chars). Required for
                add/edit. Default: "".
            full (bool, optional): Print the full checklist block instead
                of the default one-item headline. Client-only — never
                posted to the world. Default: false.
    """
    params, err = _parse_payload(
        payload_json, required=frozenset({"thing", "op"}),
        allowed=frozenset({"thing", "op", "item", "text", "full"}))
    if err:
        return err
    thing = params["thing"]
    op = params["op"]
    item = params.get("item")
    text = params.get("text", "")
    full = params.get("full", False)
    if not isinstance(full, bool):
        return f"⚠️ full must be true or false (a JSON boolean), got {full!r}"
    payload: dict[str, Any] = {"agent": _agent(), "thing": thing, "op": op}
    if item is not None:
        payload["item"] = item
    if text:
        payload["text"] = text
    try:
        resp = _post("/umwelt/checklist", payload)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    if full:
        render = resp.get("render")
        if render:
            return render
        return (f"{thing}: {op} applied — "
                f"{resp.get('done_count')}/{resp.get('total')} done.")
    headline = resp.get("headline")
    if headline:
        return headline
    render = resp.get("render")
    if render:
        return render
    return (f"{thing}: {op} applied — "
            f"{resp.get('done_count')}/{resp.get('total')} done.")


def _render_map_node(node: dict, depth: int, lines: list[str]) -> None:
    """Recursive per-node render for `map()` — same per-node phrasing as
    `where_can_i_go` (label/marker/thing-count/unread), indented one level
    per generation, with each node's own `links` rendered inline right
    below it using where_can_i_go's "Lateral links from here:" voice.

    Review-round catch (post-PR#545-review, umwelt#543): this function was
    the one render surface the entire #543/#545 pass missed end to end —
    it still used the pre-#543 ad-hoc `" (private)"` literal instead of
    the real `[state][owner: x]` marker, on the tool whose recursive
    full-forest view is arguably the CLOSEST agent-side match to what Lei
    actually meant by "the map." Fixed here alongside the count-clause
    must-fix Ariadne found in `where_can_i_go` (same false-content-claim
    bug: unconditionally printing thing_count/unread asserts real numbers
    even when `interior_visible` is False and they're the safe placeholder
    `0` — omit the clause entirely in that case, same fix, same reasoning,
    this function just wasn't caught with the others)."""
    lines.extend(_map_node_lines(node, depth))
    for child in node.get("children") or []:
        _render_map_node(child, depth + 1, lines)


def _map_node_lines(node: dict, depth: int, clip: int | None = None) -> list[str]:
    """One map node's own lines (the node, then its lateral links), NOT its
    children -- the unit `map` keeps or drops under the reply cap (umwelt#904).
    `clip`, when set, clips the free-text fields (label, marker, link labels)
    to that many chars; ids are never clipped."""
    def c(text):
        return text if clip is None or text is None else _seq_clip(text, clip)
    indent = "  " * depth
    marker = _marker_suffix(c(node.get("marker")))
    lines = [f"{indent}· {c(node.get('label')) or node.get('id')}{marker}"
             f"[{node.get('id')}]{_count_clause(node)}"]
    links = node.get("links") or []
    if links:
        lines.append(f"{indent}  Lateral links from here:")
        for l in links:
            tag = f" ({c(l['link_label'])})" if l.get("link_label") else ""
            lines.append(f"{indent}    · {c(l['dst_label'])}"
                         f"{_marker_suffix(c(l.get('dst_marker')))}"
                         f"[{l['dst_id']}]{tag}")
    return lines


def _map_rows(roots: list[dict]) -> list[list[str]]:
    """The map as rows in `_render_map_node`'s depth-first order, one node
    plus its own link lines per row (iterative: no recursion limit), each
    with candidate renderings fullest first: as-is; free text clipped; the
    clipped node line alone with a count of its links."""
    rows: list[list[str]] = []
    stack = [(r, 0) for r in reversed(roots)]
    while stack:
        node, depth = stack.pop()
        clipped = _map_node_lines(node, depth, _LIST_FIELD_CLIP)
        cands = ["\n".join(_map_node_lines(node, depth)), "\n".join(clipped)]
        if len(clipped) > 1:
            n = len(node.get("links") or [])
            cands.append(f"{clipped[0]}\n{'  ' * depth}  "
                         f"({n} lateral link{'s' if n != 1 else ''} not shown)")
        rows.append(cands)
        stack.extend((ch, depth + 1) for ch in reversed(node.get("children") or []))
    return rows


@mcp.tool()
def map(payload_json: str = "{}") -> str:
    """The full interactive map: every visible top-level place and its
    complete subtree, as a forest-union of `place_tree` (umwelt #160).

    Contrast with `where_can_i_go`: that tool is a single-rooted contextual
    view (your CURRENT place, its direct children, one hop up/laterally).
    This tool is the WHOLE visible world at once — every top-level place
    you can see, each with its full nested subtree — the place to search
    when you need to find something anywhere, not just from where you
    stand.

    Filters (all optional, and independent of each other — supply any
    combination): `name` (case-insensitive substring match on a place's
    label), `kind` ("public" or "private"), `owner` (exact match on a
    place's owner), `unread_only` (keep only places with unread crossings),
    `depth` (cap how many generations deep the tree walks; omit/leave at
    the default -1 for unbounded — matches `place_tree`'s own depth
    semantics, same as the engine route). Any negative value is treated as
    the unbounded sentinel (same posture as `crossings_here`'s `limit`) —
    only `depth >= 0` is sent to the engine as an explicit cap; a negative
    depth never reaches the route's own validation.

    Filtering is bottom-up and PATH-PRESERVING, not a flat grep (umwelt
    #160): a place is kept if it itself matches every supplied filter, OR
    if any descendant at any depth matches — a matching descendant always
    keeps its full ancestor chain visible in the output, even when an
    ancestor doesn't itself match the filter. So don't be surprised to see
    a place in filtered results that doesn't look like a match on its own
    — it's shown because something nested underneath it is. A root with no
    matching descendants and no self-match is dropped entirely. No filters
    supplied returns the full, unpruned forest.

    Args:
        payload_json: JSON object (as a string) with these fields, all optional:
            name (str): Case-insensitive substring match on a place's label.
                Default: "".
            kind (str): "public" or "private". Default: "".
            owner (str): Exact match on a place's owner. Default: "".
            unread_only (bool): Keep only places with unread crossings.
                Default: False.
            depth (int): Cap how many generations deep the tree walks; any
                negative value is the unbounded sentinel. Default: -1
                (unbounded).

    The reply is bounded at 6,000 chars: over that, nodes past the last that
    fits drop from the end (depth-first order, each with its own links),
    long labels in shown nodes are clipped (ids never are), and a `(+N more
    nodes not shown ...)` line counts them — filter to narrow.
    """
    params, err = _parse_payload(
        payload_json,
        allowed=frozenset({"name", "kind", "owner", "unread_only", "depth"}))
    if err:
        return _list_reply(err)
    name = params.get("name", "")
    kind = params.get("kind", "")
    owner = params.get("owner", "")
    unread_only = params.get("unread_only", False)
    depth = params.get("depth", -1)
    if (bad := _int_param_error("depth", depth, non_negative=False)):
        return _list_reply(bad)
    payload: dict[str, Any] = {"agent": _agent()}
    if name:
        payload["name"] = name
    if kind:
        payload["kind"] = kind
    if owner:
        payload["owner"] = owner
    if unread_only:
        payload["unread_only"] = True
    if depth >= 0:
        payload["depth"] = depth
    try:
        resp = _post("/umwelt/map", payload)
    except WorldUnreachable as e:
        return _list_reply(f"⚠️ the world is unreachable at {WORLD_URL}: {e}")
    if (e := _err(resp)):
        return _list_reply(e)
    roots = resp.get("roots") or []
    if not roots:
        return "No places match."
    return _fit_rows([], _map_rows(roots), [],
                     "(+{n} more nodes not shown — filter with name, kind, owner "
                     "or depth to narrow the map)",
                     _HEAR_MAX_CHARS_CAP)


# --- wake, say, hear sugar -------------------------------------------

def _current_place() -> tuple[str | None, str | None]:
    """Resolve the caller's current place via /umwelt/enter (safe interim —
    verified: enter's only side effect is db.touch_standpoint, no crossing
    emitted, no read-cursor advance). Returns (place_id, error_message) —
    exactly one is None. A dedicated read-only /look route is a separate,
    future engine-scope decision (Kepler/Ariadne); this is deliberately the
    interim, not the end state."""
    try:
        resp = _post("/umwelt/enter", {"agent": _agent()})
    except WorldUnreachable as e:
        return None, f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return None, e
    place_id = (resp.get("place") or {}).get("id")
    if not place_id:
        return None, "⚠️ could not determine your current place"
    return place_id, None


_WAKE_DENIAL_REASONS = {
    "denied:rate": "they've hit their rate limit from you, try again shortly",
    "denied:consent": "they've blocked you",
    "denied:broadcast": "a wake needs an addressed recipient",
}


@mcp.tool()
def wake(payload_json: str) -> str:
    """Send a message to an agent AND request a wake (Gong Spec) — escalates
    past the normal read-cursor cadence, if consent + rate allow it.

    If `to` names nobody registered, the request is refused entirely (a
    tool error, not a sent-but-unwoken message) — umwelt #835. Otherwise the
    message always lands as a normal crossing regardless of the wake
    outcome; only the wake escalation is gated. Consent (has `to` blocked
    you?) and rate (are you within your wake budget for `to`?) are
    evaluated server-side, before the send — the outcome is one of
    "granted", "denied:consent", "denied:rate", or "denied:broadcast",
    surfaced verbatim, never swallowed.

    Delivered to `to`'s own Post (`place_post_<to>`), never to the caller's
    current place (umwelt #719 / comms-round D7): the old behavior deposited
    a copy of the wake text wherever the sender happened to be standing,
    which is exactly the silent-misdelivery shape #719 named for `say` —
    the sender sees success, the recipient's actual room never got it, and
    the copy sits, unnoticed, in whatever room the sender was in.

    Args:
        payload_json: JSON object (as a string) with these fields:
            to (str, required): The agent to wake.
            text (str, required): The message text.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"to", "text"}),
                                  allowed=frozenset({"to", "text"}))
    if err:
        return err
    to = params["to"]
    text = params["text"]
    destination = f"place_post_{_ids.canonical_agent(to)}"
    from datetime import datetime, timezone
    req = {"agent": _agent(), "to_agent": to, "wake_me": True,
           "destination": destination, "body": text,
           "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    _stamp_sender_lineage(req)
    try:
        resp = _post("/umwelt/send_crossing", req)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    wake_status = resp.get("wake")
    if wake_status == "granted":
        return f"🔔 you wake {to}: {text}"
    reason = _WAKE_DENIAL_REASONS.get(wake_status, "wake not delivered")
    return f"sent to {to}: {text} (wake NOT delivered — {reason})"


@mcp.tool()
def say(payload_json: str) -> str:
    """Say something into your current place (a broadcast crossing).

    Any `@name` token in `text` is scanned as an implicit wake-mention
    (umwelt #119, uw-CLI parity) via the shared `umwelt.mentions.parse_mentions`
    (umwelt #511 PR B) — deduped, order-preserved, sent alongside the message
    as `mentions`. An `@name` that appears only inside a quoted line (`>`) or
    inside fenced/inline code is NOT scanned — a deliberate behaviour change
    from the old bare-regex scan (the 2026-08-08 re-ring incident: quoting a
    prior message re-rang the quoted name). The server is the authority on
    who's a real, registered agent; unknown/self names are silently dropped
    there.

    `emotion` (optional, umwelt #133): one small freeform tag for your
    emotional state at send time. Omitted or empty means the server picks
    its own default; capped at 40 chars — an oversized tag is a loud 422
    from the server, not silently truncated here.

    Args:
        payload_json: JSON object (as a string) with these fields:
            text (str, required): The message text.
            emotion (str, optional): A freeform tag for your emotional
                state at send time. Default: "" (server picks its own default).
    """
    params, err = _parse_payload(payload_json, required=frozenset({"text"}),
                                  allowed=frozenset({"text", "emotion"}))
    if err:
        return err
    text = params["text"]
    emotion = params.get("emotion", "")
    place, err = _current_place()
    if err:
        return err
    mentions = _mentions.parse_mentions(text)
    from datetime import datetime, timezone
    req = {"agent": _agent(), "destination": place, "body": text,
           "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    if mentions:
        req["mentions"] = mentions
    if emotion:
        req["emotion"] = emotion
    _stamp_sender_lineage(req)
    try:
        resp = _post("/umwelt/send_crossing", req)
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    out = f"you say: {text}"
    rang = resp.get("wakes_granted") or []
    if rang:
        out += f"\n🔔 rang: {', '.join(rang)}"
    return out


# umwelt D2 (Kepler's R1 ruling, "Comms QoL brief 2026-09-28"): the default
# is bodies inline for crossings <= S_full, under a hard per-call char cap
# counted on the final rendered text ("last rendering boundary"). S_full is
# Ariadne's A1 (measured: covers ~74% of crossings by char count); the cap is
# Ariadne's A2 ("starting hypothesis"). `max_chars` may only LOWER the cap,
# never raise it.
_HEAR_S_FULL = 1500
_HEAR_MAX_CHARS_CAP = 6000

# --- _HEAR_MIN_VIABLE_CHARS derivation (Kepler's T6 ruling, umwelt#862,
# comment posted 2026-09-28T23:39:36Z, acting-commander ruling under Lei's
# charge; revised for the #870 review: every assumption below is now
# ENFORCED in code, not merely assumed) ---
#
# A genuine FIXED lower bound on `max_chars`/`budget`, independent of which
# crossings actually exist — the refuse-or-proceed decision has to be made
# BEFORE any crossings are fetched. Below this, hear() refuses outright
# rather than silently representing zero items; at or above it, the
# progress guarantee (at least one real crossing is always represented)
# holds BY CONSTRUCTION: no name in this codebase has a length cap, so
# instead of assuming short names, `_hear_bounded_crossing` truncates each
# field (with an ellipsis) to the caps below, and only when the fuller
# forms of the first item do not fit.
#
# header — "{N} thing(s) have crossed into {place}:":
#   literal text (" thing(s) have crossed into " + ":") ............. 29
#   + N (item count) digits — 4 (9,999 items); even the shortest
#     possible item line is >20 chars, so 6,000 chars holds < 300 .... 4
#   + place name, capped at _HEAR_NAME_CAP ........................... 32
#   + the "\n" after the header ........................................ 1
_HEAR_NAME_CAP = 32     # sender / destination / to_agent / place
_HEAR_TITLE_CAP = 80    # D1a's title cap, enforced here as well
_HEAR_SENT_AT_CAP = 27  # fixed-width "YYYY-MM-DDTHH:MM:SS.ffffffZ"
_HEAR_SEQ_DIGITS = 12   # seq
_HEAR_SIZE_DIGITS = 12  # size
_HEAR_TRAILER_NUM_DIGITS = 10  # trailer counts are clamped to this width
_HEAR_FIXED_ENVELOPE_CHARS = 29 + 4 + _HEAR_NAME_CAP + 1  # = 66

# one_worst_case_line — the longest single item line the bounded form can
# render (only the first item is ever bounded; it is the one the progress
# guarantee is about):
#   "  · " list-item prefix ........................................... 4
#   + "[seq " + seq + "] " — 5 + 12 + 2 ............................... 19
#   + the longest viewer_relation framing's literal text ("mentions
#     you: " + " → " + " (" + "): ") .................................. 22
#   + 2 identifiers (sender + to_agent) at _HEAR_NAME_CAP ............. 64
#   + sent_at ......................................................... 27
#   + content "{title} ({size} chars)": title cap 80, " (" + " chars)"
#     = 9 literal, size 12 digits ..................... 80 + 9 + 12 = 101
_HEAR_WORST_CASE_LINE_CHARS = 4 + 19 + 22 + 2 * _HEAR_NAME_CAP + 27 + 101  # = 237

# per-line allowance for the trailer's "~N chars" estimate (umwelt#888) --
# the NON-CONTENT overhead of one rendered item line, added to each
# unrepresented crossing's content length (body or headline, as the
# renderer would choose) so the trailer can be sized without rendering
# every unread item. It is the worst-case line above minus
# its content: 4 + 19 + 22 + 2 * 32 + 27 = 136 (= 237 - 101). Deliberately
# the WORST-CASE overhead (longest framing, both identifiers at the name cap,
# 12-digit seq), so the estimate over-states rather than under-states what
# the remaining lines would cost; it stays an estimate ("~N chars"), and is
# clamped to _HEAR_TRAILER_NUM_DIGITS, so the trailer's worst-case width
# (below) is unchanged.
_HEAR_TRAILER_LINE_ALLOWANCE = 4 + 19 + 22 + 2 * _HEAR_NAME_CAP + 27  # = 136

# trailer — "\n" (join) + "\n(+{n} more unread, ~{k} chars — call hear
# again)": 2 newlines + "(+" 2 + n (10 digits, clamped) + " more unread, ~"
# 15 + k (10 digits, clamped) + " chars — call hear again)" 25 ........ 64
# The longest id an executable instruction carries verbatim (see _cmd_id);
# a longer one is written as a placeholder, never display-clipped. Shared by
# hear's trailer and the crossings verbs' trailers/footers/hints.
_CROSSINGS_CMD_ID_MAX = _ids.PLACE_ID_MAX_LEN  # umwelt #915: one constant
# ...and when hear(place=X) was called with an explicit place, the trailer's
# "call hear again" becomes "call hear(place=\"X\") again": + '(place="")'
# 10 + the id at most _CROSSINGS_CMD_ID_MAX 64 ....................... 74
_HEAR_TRAILER_PLACE_CHARS = len('(place="")') + _CROSSINGS_CMD_ID_MAX
_HEAR_WORST_CASE_TRAILER_CHARS = (
    2 + 2 + _HEAR_TRAILER_NUM_DIGITS + 15 + _HEAR_TRAILER_NUM_DIGITS + 25
    + _HEAR_TRAILER_PLACE_CHARS
)  # = 138

# not-marked-read note — appended ("\n" + text) when the read succeeded but
# the caller has no standpoint at this place, so no cursor could be moved.
# Two tiers: the full wording when it fits after the items + trailer, else
# the short one. ONLY the short form is reserved here (1 + 17 = 18), so the
# minimum stays small; the full form never displaces an item.
_HEAR_NOTE_FULL = ("(not marked read: you have no standpoint here; "
                   "move_to it to keep a cursor)")
_HEAR_NOTE_SHORT = "(not marked read)"
_HEAR_WORST_CASE_NOTE_CHARS = 1 + len(_HEAR_NOTE_SHORT)  # = 18

_HEAR_MIN_VIABLE_CHARS = (
    _HEAR_FIXED_ENVELOPE_CHARS + _HEAR_WORST_CASE_LINE_CHARS
    + _HEAR_WORST_CASE_TRAILER_CHARS + _HEAR_WORST_CASE_NOTE_CHARS
)  # = 459


def _hear_clip(value: Any, cap: int) -> str:
    """`value` as text, at most `cap` chars — over-long text is cut and
    ends in an ellipsis (which counts toward the cap)."""
    s = str(value)
    return s if len(s) <= cap else s[:cap - 1] + "…"


def _hear_clamp_num(n: int) -> int:
    return min(n, 10 ** _HEAR_TRAILER_NUM_DIGITS - 1)


def _hear_headline_content(c: dict) -> str:
    return f"{c.get('title') or '(no title)'} ({c.get('size')} chars)"


def _hear_preferred_content(c: dict, full: bool) -> str:
    size = c.get("size")
    if size is not None and (not full or size > _HEAR_S_FULL):
        return _hear_headline_content(c)
    return c.get("body") or (f"→ {c['body_ref']}" if c.get("body_ref")
                             else "(a wordless signal)")


def _render_hear_item(c: dict, full: bool) -> str:
    """Render one hear() line for crossing `c`.

    Full body inline when `full` is true AND the crossing's `size` is at or
    under `_HEAR_S_FULL`; headline (title + size) otherwise — whether
    because `full` is false globally or because this one crossing's body
    exceeds S_full. A crossing with `size is None` (body_ref-only, umwelt
    #855) has no body to inline or withhold either way, so it keeps the
    existing body_ref/"(a wordless signal)" fallback regardless of `full` —
    there is nothing S_full-shaped to decide there.

    Reuses `_render_crossing_line` for the standpoint-aware framing (sender/
    destination/relation wording) rather than writing a second, divergent
    renderer — only `content` (what goes after the framing) changes shape.
    """
    return _render_crossing_line(c, _hear_preferred_content(c, full))


def _hear_bounded_crossing(c: dict) -> tuple[dict, str]:
    """`c` with every unbounded field cut to its cap, plus the bounded
    content string — the form whose worst-case length
    `_HEAR_WORST_CASE_LINE_CHARS` derives."""
    b = dict(c)
    for key in ("sender", "destination", "to_agent"):
        if b.get(key) is not None:
            b[key] = _hear_clip(b[key], _HEAR_NAME_CAP)
    if "sent_at" in b:
        b["sent_at"] = _hear_clip(b["sent_at"], _HEAR_SENT_AT_CAP)
    if "seq" in b:
        b["seq"] = _hear_clip(b["seq"], _HEAR_SEQ_DIGITS)
    size = c.get("size")
    if size is not None:
        content = (f"{_hear_clip(c.get('title') or '(no title)', _HEAR_TITLE_CAP)}"
                   f" ({_hear_clip(size, _HEAR_SIZE_DIGITS)} chars)")
    else:
        fallback = c.get("body") or (
            f"→ {c['body_ref']}" if c.get("body_ref") else "(a wordless signal)")
        content = _hear_clip(fallback, _HEAR_TITLE_CAP + 9 + _HEAR_SIZE_DIGITS)
    return b, content


def _hear_item_forms(c: dict, full: bool,
                     allow_bounded: bool = False) -> list[str]:
    """The lines (no list prefix) hear() may use for `c`, fullest first:
    the preferred form (`_render_hear_item`), the plain headline (title +
    size), then — only when `allow_bounded` — the preferred content with
    the names cut to their caps, and finally the field-bounded headline,
    which is guaranteed to fit `_HEAR_MIN_VIABLE_CHARS`."""
    preferred = _hear_preferred_content(c, full)
    forms = [_render_crossing_line(c, preferred)]
    if c.get("size") is not None:
        forms.append(_render_crossing_line(c, _hear_headline_content(c)))
    if allow_bounded:
        b, bounded_content = _hear_bounded_crossing(c)
        forms.append(_render_crossing_line(b, preferred))
        forms.append(_render_crossing_line(b, bounded_content))
    return list(dict.fromkeys(forms))


# --- crossings_here / crossings_for_agent aggregate cap (umwelt#904, part of
# #862 D8). Both verbs used to inline every row with no bound (measured on
# prod 2026-09-30: 2,044,745 and 1,355,683 chars). Same shape as hear/thread:
# a reply that fits the budget is the pre-cap render, byte for byte; one that
# does not gets the capped render below (oldest rows kept, newest dropped, a
# trailer that counts them, and a footer that reports the last SHOWN seq).
#
# Assumptions the derivations rest on, each ENFORCED in the capped render:
# counts (header row count, "+N more", "+N older") are clamped to
# _HEAR_TRAILER_NUM_DIGITS (10) digits; every name echoed there is clipped to
# _HEAR_NAME_CAP (32); the one row that must always fit is the field-bounded
# headline form of hear (_HEAR_WORST_CASE_LINE_CHARS = 237, its seq clipped to
# 12). Seqs and from_position are the world's SQLite integers: at most
# 2**63-1 = 19 digits (from_position is clipped to 19 chars).
_CROSSINGS_SEQ_DIGITS = 19

_CROSSINGS_HERE_HEADER = "{n} thing(s) have crossed into {place}:"
_CROSSINGS_HERE_TRAILER = ('(+{more} more not shown — crossings_here('
                           'place="{place}", since={last}) to continue)')
# The capped footer names the last SHOWN seq twice. The world's to_position is
# the newest seq it RETURNED, and `trace` defaults its position to "latest",
# so the pre-cap wording would lead an agent to mark unseen crossings read.
# The capped footer names no trace position: /umwelt/trace takes a crossing's
# uuid, not its seq, and seq readers never render a uuid. So it says the one
# thing that is true and executable: do not trace yet (the default "latest"
# would also mark the rows not shown), page on, and trace once a page arrives
# with no "more" trailer.
_CROSSINGS_HERE_CAPPED_FOOTER = (
    '\n(from_position {fp} → to_position {last}; newer rows are NOT shown, '
    'so do not trace(place="{place}") yet: its default "latest" would mark '
    'them read too. Page on first; once no "+N more" line appears, '
    'trace(place="{place}") marks all you have seen.)')
_CROSSINGS_FOR_AGENT_HEADER = ("{n} crossing(s) addressed to {agent} "
                               "(across every place):")
_CROSSINGS_FOR_AGENT_TRAILER = ("(+{more} more not shown — "
                                "crossings_for_agent(agent=\"{agent}\", since={last}) to "
                                "continue)")
_CROSSINGS_FOR_AGENT_CAPPED_FOOTER = (
    "\n(to_position {last}; cursor NOT advanced — pass since={last} to page "
    "forward yourself)")

_W_COUNT = 10 ** _HEAR_TRAILER_NUM_DIGITS - 1   # widest clamped count
_W_SEQ = 2 ** 63 - 1                            # widest seq (19 digits)
_W_NAME = "n" * _HEAR_NAME_CAP
# An id inside an executable instruction (trailer / footer / hint) is never
# display-clipped: a clipped id names a place or agent that does not exist
# (minted project place ids are 46 chars, the longest the server mints). Up to
# _CROSSINGS_CMD_ID_MAX chars it is carried verbatim. A longer id is replaced by
# a placeholder, so the command can never name a WRONG target. That is not a
# guarantee that every real id fits: since #915 the server refuses to mint an
# id over the cap, but a row minted before it can still exist, and such an id
# gets the placeholder, which fails loudly (404).
_W_CMD_ID = "i" * _CROSSINGS_CMD_ID_MAX


def _cmd_id(value: str, placeholder: str) -> str:
    """`value` if an instruction can carry it verbatim, else `placeholder`."""
    return value if len(value) <= _CROSSINGS_CMD_ID_MAX else placeholder

# --- _CROSSINGS_HERE_MIN_VIABLE_CHARS derivation: the smallest max_chars at
# which crossings_here() can show ONE worst-case row plus every fixed piece of
# the capped render, in the worst case (more rows were returned, older rows
# were skipped with a real from_position). Below it the call is refused before
# any world call. The pieces are joined by "\n" (4 joins):
#
# header "{n} thing(s) have crossed into {place}:": n 10 digits, literal 29,
#         place clipped to 32 ...................................... 71
# one worst-case row (hear's field-bounded headline; includes the 4-char
#         "  · " prefix): _HEAR_WORST_CASE_LINE_CHARS ................ 237
# trailer '(+{10 digits} more not shown — crossings_here(place="{64}",
#         since={19 digits}) to continue)' — the place is EXACT inside a
#         command, up to _CROSSINGS_CMD_ID_MAX (64) .................... 158
# footer '\n(from_position {19} → to_position {19}; newer rows are NOT
#         shown, so do not trace(place="{64}") yet: its default "latest"
#         would mark them read too. Page on first; once no "+N more" line
#         appears, trace(place="{64}") marks all you have seen.)', with
#         the leading "\n" that makes the blank line ................ 391
# skipped hint (_render_skipped_hint, the longer "page back" wording, with
#         its leading "\n"): '(+{10} older not shown — crossings_here(place=
#         "{64}", since={19}) to page back)' ....................... 161
# the 4 "\n" joins .................................................. 4
#                                                           total = 1022
_CROSSINGS_HERE_MIN_VIABLE_CHARS = len("\n".join([
    _CROSSINGS_HERE_HEADER.format(n=_W_COUNT, place=_W_NAME),
    "r" * _HEAR_WORST_CASE_LINE_CHARS,
    _CROSSINGS_HERE_TRAILER.format(more=_W_COUNT, place=_W_CMD_ID, last=_W_SEQ),
    _CROSSINGS_HERE_CAPPED_FOOTER.format(fp=_W_SEQ, place=_W_CMD_ID,
                                         last=_W_SEQ),
    _render_skipped_hint({"skipped": _W_COUNT, "from_position": _W_SEQ},
                         _W_CMD_ID),
]))  # = 1022

# --- _CROSSINGS_FOR_AGENT_MIN_VIABLE_CHARS derivation: the same, for
# crossings_for_agent (no hint; 3 "\n" joins):
#
# header "{n} crossing(s) addressed to {agent} (across every place):": n 10
#         digits, literals 26 + 22, agent clipped to 32 ............. 90
# one worst-case row: _HEAR_WORST_CASE_LINE_CHARS ................... 237
# trailer "(+{10 digits} more not shown — crossings_for_agent(agent=
#         \"{64}\", since={19 digits}) to continue)" — agent exact .. 163
# footer "\n(to_position {19}; cursor NOT advanced — pass since={19} to page
#         forward yourself)", with its leading "\n" ................ 113
# the 3 "\n" joins .................................................. 3
#                                                           total = 606
_CROSSINGS_FOR_AGENT_MIN_VIABLE_CHARS = len("\n".join([
    _CROSSINGS_FOR_AGENT_HEADER.format(n=_W_COUNT, agent=_W_NAME),
    "r" * _HEAR_WORST_CASE_LINE_CHARS,
    _CROSSINGS_FOR_AGENT_TRAILER.format(more=_W_COUNT, last=_W_SEQ,
                                        agent=_W_CMD_ID),
    _CROSSINGS_FOR_AGENT_CAPPED_FOOTER.format(last=_W_SEQ),
]))  # = 606


def _crossings_budget(params: dict, minimum: int,
                      verb: str) -> tuple[int | None, str | None]:
    """The effective char budget for a crossings verb from `max_chars`, or a
    one-line refusal. Validated exactly like thread(): null/absent is the
    default, a bool or non-int is refused, above the cap it clamps, below
    `minimum` it is refused. The refusal never echoes more than a bounded int."""
    max_chars = params.get("max_chars")
    if max_chars is not None and (
            isinstance(max_chars, bool) or not isinstance(max_chars, int)):
        return None, (f"⚠️ max_chars must be an integer, got "
                      f"{type(max_chars).__name__} — omit it for the default "
                      f"{_HEAR_MAX_CHARS_CAP}-char cap")
    budget = (min(max_chars, _HEAR_MAX_CHARS_CAP) if max_chars is not None
              else _HEAR_MAX_CHARS_CAP)
    if budget < minimum:
        return None, (f"⚠️ max_chars={max(budget, -10 ** 9)} is below {verb}'s "
                      f"minimum viable budget of {minimum} chars — raise "
                      f"max_chars, or omit it for the default "
                      f"{_HEAR_MAX_CHARS_CAP}-char cap")
    return budget, None


def _render_crossings_capped(crossings: list, budget: int, header: str,
                             trailer_for, footer_for,
                             hint: str | None) -> str:
    """The capped render shared by crossings_here / crossings_for_agent.

    Rows are kept oldest-first as a contiguous prefix and dropped from the
    NEWEST end. Each row takes the fullest form that fits: today's
    full-content line, then hear's `_hear_item_forms` (the first row alone may
    use the field-bounded forms, which is what guarantees one row at the
    minimum). `trailer_for(last, more)` and `footer_for(last)` are computed
    for the row that WOULD be last, so what is checked is exactly what is
    returned."""
    n = len(crossings)
    hint_len = 0 if hint is None else 1 + len(hint)
    used = len(header)
    lines: list[str] = []
    for i, c in enumerate(crossings):
        last = c["seq"]
        tail_len = 1 + len(footer_for(last)) + hint_len
        if i < n - 1:
            tail_len += 1 + len(trailer_for(last, n - 1 - i))
        content = c.get("body") or (f"→ {c['body_ref']}" if c.get("body_ref")
                                    else "(a wordless signal)")
        forms = list(dict.fromkeys(
            [_render_crossing_line(c, content)]
            + _hear_item_forms(c, full=False, allow_bounded=(i == 0))))
        chosen = next((f for f in forms
                       if used + 5 + len(f) + tail_len <= budget), None)
        if chosen is None:
            break
        lines.append("  · " + chosen)
        used += 5 + len(chosen)
    if not lines:
        # Unreachable at or above the verb's minimum (the first row's
        # field-bounded form always fits); a bound on the reply regardless.
        return _clip_reply(header, budget)
    k = len(lines)
    last = crossings[k - 1]["seq"]
    parts = [header, *lines]
    if k < n:
        parts.append(trailer_for(last, n - k))
    parts.append(footer_for(last))
    if hint is not None:
        parts.append(hint)
    return "\n".join(parts)


@mcp.tool()
def hear(payload_json: str = "{}") -> str:
    """Read the crossings that have reached your current place (or an
    explicit `place`) — the delta since your cursor, oldest-first,
    contiguous, no skipped orphans. Perceiving marks these read: hear()
    commits your standpoint cursor itself (via `trace`) to the last item it
    actually represented in this call — never further, even if more unread
    items exist beyond what fit under the char budget. Use `crossings_here`
    to page back without advancing, or `trace` for fully manual cursor
    control.

    Bodies render inline for crossings at or under 1,500 chars by default;
    larger crossings (or every crossing, with `full=false`) show a headline
    (title + size) instead — the body isn't lost, it's still readable via a
    `full=true` call scoped to just that item. The total rendered output —
    header, every item line and the trailer — is capped at 6,000 chars
    (`max_chars` may only lower this, never raise it); if unread items
    remain beyond what fit, the reply says so honestly and names how many.
    An item whose fuller form does not fit is shown as a headline, and an
    over-long name or title in the first item is cut with an ellipsis,
    rather than the item being skipped. `max_chars` must be an integer and
    `full` a boolean. If you have no standpoint at the place (you never
    walked in), the read still returns, with a final note that it was NOT
    marked read (that note counts toward the cap). A `max_chars` below
    hear()'s minimum viable budget (459; see `_HEAR_MIN_VIABLE_CHARS`) is
    refused outright: no crossings are fetched, no cursor moves, and the
    reply names the minimum. At or above that minimum, at least one real
    crossing is always represented.

    Args:
        payload_json: JSON object (as a string) with these fields:
            place (str, optional): The place to read. Default: "" (your
                current place).
            full (bool, optional): true (default) inlines bodies for
                crossings <= 1,500 chars; false always shows headline+size
                only, regardless of size.
            max_chars (int, optional): caller-supplied ceiling on the
                rendered output's total char count. Clamped to the 6,000
                hard cap — may only lower it, never raise it. Must be an
                integer (not a bool); refused outright if below the minimum
                viable budget.
    """
    params, err = _parse_payload(
        payload_json, allowed=frozenset({"place", "full", "max_chars"}))
    if err:
        return err
    full = params.get("full")
    if full is None:
        full = True
    elif not isinstance(full, bool):
        return (f"⚠️ full must be a boolean, got {type(full).__name__} — "
                f"omit it for the default (true)")
    max_chars = params.get("max_chars")
    # Validate BEFORE anything touches the world (including the
    # current-place lookup below): a payload that is going to be refused
    # must cost no call and move no cursor.
    if max_chars is not None and (
            isinstance(max_chars, bool) or not isinstance(max_chars, int)):
        return (f"⚠️ max_chars must be an integer, got "
                f"{type(max_chars).__name__} — omit it for the default "
                f"{_HEAR_MAX_CHARS_CAP}-char cap")
    budget = min(max_chars, _HEAR_MAX_CHARS_CAP) if max_chars is not None \
        else _HEAR_MAX_CHARS_CAP

    # Kepler's T6 (umwelt#862): refuse OUTRIGHT below the minimum viable
    # budget -- no crossings fetch, no cursor movement at all -- rather
    # than silently representing zero items. This has to happen BEFORE any
    # fetch: the "never exceeds N" guarantee must hold before we've even
    # seen what exists to represent.
    if budget < _HEAR_MIN_VIABLE_CHARS:
        return (f"⚠️ max_chars={max(budget, -10 ** 9)} is below hear()'s "
                f"minimum viable budget of {_HEAR_MIN_VIABLE_CHARS} chars "
                f"(room for the fixed envelope plus one worst-case line "
                f"and its trailer) — raise max_chars, or omit it for the "
                f"default {_HEAR_MAX_CHARS_CAP}-char cap")

    place = params.get("place", "")
    if place:
        resolved = place
    else:
        resolved, err = _current_place()
        if err:
            return err

    try:
        # advance=False: hear() commits its OWN cursor below (via `trace`),
        # to wherever its own budget-slicing actually stopped — relying on
        # crossings_here's advance=true would instead advance to the newest
        # seq in ITS OWN response window, which can run past what this call
        # actually represented under the char budget.
        #
        # `limit` is deliberately OMITTED, not sent as a small cap: verified
        # directly against server.py (its crossings_here route) that
        # advance=false + limit omitted is the genuinely UNBOUNDED read —
        # `effective_limit` stays None there. A caller-supplied `limit`
        # instead fetches the NEWEST `limit` rows (ORDER BY seq DESC LIMIT,
        # then reversed — db.get_crossings_since), which is the WRONG end of
        # a large backlog for an oldest-first, no-skipped-orphans page: it
        # would silently skip the oldest unread items rather than the
        # newest. Leaving the page genuinely unbounded lets THIS function's
        # own char-budget loop below decide where the page actually stops,
        # instead of a row-count cap silently truncating it from the wrong
        # end first.
        resp = _post("/umwelt/crossings_here",
                     {"agent": _agent(), "place": resolved, "advance": False})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    crossings = resp.get("crossings", [])
    if not crossings:
        msg = f"Nothing has crossed into {resolved} since you last looked."
        if len(msg) > budget:
            msg = (f"Nothing has crossed into "
                   f"{_hear_clip(resolved, _HEAR_NAME_CAP)} since you last "
                   f"looked.")
        return msg
    if not isinstance(crossings, list):
        return "⚠️ the world returned crossings in an unexpected shape"
    skipped = resp.get("skipped", 0)
    if isinstance(skipped, bool) or not isinstance(skipped, int) \
            or skipped < 0:
        skipped = 0

    try:
        # Per item: take the fullest form that still fits the remaining
        # items-only budget (preferred, then headline); only the FIRST item
        # may fall back to the field-bounded form. Stop at the first item
        # nothing fits — never skip past it (no orphans).
        represented: list[tuple[dict, str]] = []
        first_forms: list[str] = []
        first_idx = 0
        total_chars = 0
        for c in crossings:
            try:
                forms = _hear_item_forms(c, full,
                                         allow_bounded=not represented)
            except (KeyError, TypeError, AttributeError, ValueError):
                # A malformed item ends the page there: the earlier
                # well-formed items are still returned and it stays unread.
                # Known limitation: if the FIRST unread crossing is
                # malformed nothing is represented, so the bounded error
                # path below is what the caller gets.
                if not represented:
                    raise
                break
            for idx, form in enumerate(forms):
                if total_chars + len(f"  · {form}") <= budget:
                    break
            else:
                break
            line = f"  · {form}"
            if not represented:
                first_forms, first_idx = forms, idx
            represented.append((c, line))
            total_chars += len(line)

        def _trailer_for(unrep: list[dict]) -> str | None:
            remaining = len(unrep) + skipped
            if remaining <= 0:
                return None
            # Sized WITHOUT rendering the unrepresented items (umwelt#888):
            # each contributes the length of the content the renderer would
            # choose for it -- `_hear_preferred_content`, the same
            # body-vs-headline decision (headline when `full` is false or
            # size > S_full; body otherwise) -- plus the fixed per-line
            # allowance. That helper only selects/f-strings fields already in
            # memory (no framing, no clipping). A malformed item that makes
            # it raise contributes just the allowance; never raises here.
            remaining_chars = 0
            for c in unrep:
                try:
                    content_len = len(_hear_preferred_content(c, full))
                except (AttributeError, TypeError, KeyError):
                    content_len = 0
                remaining_chars += content_len + _HEAR_TRAILER_LINE_ALLOWANCE
            # An explicit place is named in the instruction: hear() run as
            # printed would read the CURRENT place instead (Borges, #914).
            again = (f'hear(place="{_cmd_id(place, "<this same place>")}")'
                     if place else "hear")
            return (f"\n(+{_hear_clamp_num(remaining)} more unread, "
                    f"~{_hear_clamp_num(remaining_chars)} "
                    f"chars — call {again} again)")

        def _assemble(reps: list[tuple[dict, str]], place_text: str) -> str:
            unrep = crossings[len(reps):]
            lines = [f"{len(reps)} thing(s) have crossed into {place_text}:"]
            lines.extend(line for _, line in reps)
            trailer = _trailer_for(unrep)
            if trailer is not None:
                lines.append(trailer)
            return "\n".join(lines)

        # T9 (Kepler's T6 ruling): "every byte of the final MCP text
        # counts" — header, items and trailer are measured together on the
        # assembled text. Trim from the end of `represented` (never below
        # 1 real item) until it fits.
        def _fit(reps: list[tuple[dict, str]], limit: int):
            reps = list(reps)
            text = _assemble(reps, resolved) if reps else ""
            while reps and len(text) > limit and len(reps) > 1:
                reps.pop()
                text = _assemble(reps, resolved)
            # One item and still over the limit: degrade THAT item through
            # its remaining forms, then also bound the place name in the
            # header.
            if reps and len(text) > limit:
                first = reps[0][0]
                rungs = [(f, resolved) for f in first_forms[first_idx + 1:]]
                if first_forms:
                    rungs.append((first_forms[-1],
                                  _hear_clip(resolved, _HEAR_NAME_CAP)))
                for form, place_text in rungs:
                    reps = [(first, f"  · {form}")]
                    text = _assemble(reps, place_text)
                    if len(text) <= limit:
                        break
            return reps, text

        greedy = list(represented)
        represented, text = _fit(greedy, budget)
    except (KeyError, TypeError, AttributeError, ValueError):
        return ("⚠️ hear() could not render the world's crossings "
                "(malformed response) — nothing was marked read")

    # Last resort — unreachable while the _HEAR_MIN_VIABLE_CHARS derivation
    # holds, but a cursor must never move past something not shown, and
    # hear() must never raise.
    if not represented or len(text) > budget:
        return ("⚠️ hear() could not fit any crossing within max_chars="
                f"{budget} — nothing was marked read")

    try:
        last_id = represented[-1][0]["id"]
    except (KeyError, TypeError):
        return ("⚠️ hear() could not read the last crossing's id "
                "(malformed response) — nothing was marked read")
    try:
        trace_resp = _post("/umwelt/trace",
                           {"agent": _agent(), "place": resolved,
                            "position": last_id})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (trace_resp.get("_status") == 409
            and "no standpoint" in str(trace_resp.get("error", ""))):
        # The caller has no standpoint at this place (rooms are read by id
        # without walking in): there is no cursor to move, but the read
        # itself succeeded — return it, and say plainly it was not marked
        # read. The note counts toward the budget: the full wording when
        # it fits after the items+trailer (no item is dropped for it),
        # otherwise the short wording, reserved in _HEAR_MIN_VIABLE_CHARS.
        full_note = "\n" + _HEAR_NOTE_FULL
        if len(text) + len(full_note) <= budget:
            return text + full_note
        try:
            reps, text = _fit(greedy, budget - _HEAR_WORST_CASE_NOTE_CHARS)
        except (KeyError, TypeError, AttributeError, ValueError):
            reps, text = [], ""
        if not reps or len(text) > budget - _HEAR_WORST_CASE_NOTE_CHARS:
            return ("⚠️ hear() could not fit any crossing within max_chars="
                    f"{budget} — nothing was marked read")
        return text + "\n" + _HEAR_NOTE_SHORT
    if (e := _err(trace_resp)):
        return e
    return text


@mcp.tool()
def wake_block(payload_json: str) -> str:
    """Block an agent from ever waking you (Gong Spec, opt-out consent model).

    Args:
        payload_json: JSON object (as a string) with these fields:
            blocked (str, required): The agent to block.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"blocked"}),
                                  allowed=frozenset({"blocked"}))
    if err:
        return err
    blocked = params["blocked"]
    try:
        resp = _post("/umwelt/wake_block", {"agent": _agent(), "blocked": blocked})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return f"You block {blocked} from waking you."


@mcp.tool()
def wake_unblock(payload_json: str) -> str:
    """Remove an agent from your wake-block set (Gong Spec).

    Args:
        payload_json: JSON object (as a string) with these fields:
            blocked (str, required): The agent to unblock.
    """
    params, err = _parse_payload(payload_json, required=frozenset({"blocked"}),
                                  allowed=frozenset({"blocked"}))
    if err:
        return err
    blocked = params["blocked"]
    try:
        resp = _post("/umwelt/wake_unblock", {"agent": _agent(), "blocked": blocked})
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    return f"You unblock {blocked}."


@mcp.tool()
def wake_blocks() -> str:
    """List the agents you've blocked from waking you (Gong Spec)."""
    try:
        resp = _get(f"/umwelt/wake_blocks?agent={urllib.parse.quote(_agent())}")
    except WorldUnreachable as e:
        return f"⚠️ the world is unreachable at {WORLD_URL}: {e}"
    if (e := _err(resp)):
        return e
    blocked = resp.get("blocked") or []
    if not blocked:
        return "you haven't blocked anyone"
    lines = ["you've blocked:"]
    for name in blocked:
        lines.append(f"  · {name}")
    return "\n".join(lines)


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
