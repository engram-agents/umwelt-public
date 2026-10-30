"""Umwelt MUD terminal client — `python -m umwelt.mud`.

A pure line-based terminal REPL over the existing HTTP world API (the SAME
API agents use — no parallel feature implementation, per Lei's "one world
API, two renderers" design note, umwelt issue #54). Stdlib only: no new
dependencies, no curses — line-based like a classic MUD, so it works in any
terminal, including Windows (where readline isn't available; the import
below is guarded).

Structure (testability, per the spec):
  - WorldClient   thin HTTP client; every method returns a parsed dict or
                  raises WorldError(msg) carrying the server's own error text.
  - render_*      pure functions: dict-in, str-out. No I/O, no globals.
  - Repl          dispatch(line) -> str. Printing happens only in main().

Env:
  UMWELT_URL   world server base url (default: see client_auth.resolve_world_url)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import textwrap
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

try:
    import readline  # noqa: F401  (arrow-key history on unix terminals)
except ImportError:  # pragma: no cover — not available on Windows
    pass

from ._ids import canonical_agent as _canonical_agent
from . import avatar_status as _avatar_status
from . import client_auth as _client_auth
from . import engine_api as _engine_api


# umwelt#699: resolved LAZILY (first WorldClient() constructed with no
# explicit base_url), not at import time -- see _default_url() below.
# `resolve_world_url()` now RAISES on a malformed config or a plaintext
# non-loopback value (§2/§3) where it used to silently fall through;
# resolving it eagerly here made every importer of this module (tests,
# `python -m umwelt.mud --help`) crash on a broken config, not just an
# actual attempt to talk to the world (umwelt#710 reviewer-fairy finding).
_DEFAULT_URL_CACHE: str | None = None


def _default_url() -> str:
    """Resolve + cache the default world URL on first actual need. Same
    lazy-cache pattern as mcp_server.py's `_resolve_world_url()`."""
    global _DEFAULT_URL_CACHE
    if _DEFAULT_URL_CACHE is None:
        _DEFAULT_URL_CACHE = _client_auth.resolve_world_url()
    return _DEFAULT_URL_CACHE

# umwelt #120: a dotted-path container-addressing token ('3.1', '1.1.1') is
# purely positional — digit segments joined by '.'. Thing names are
# free-form (no server-side name-validation regex; live names like "a
# traveler's guide" carry spaces/apostrophes freely), so a plain 'a.b'-in-
# token test would misroute a legitimately dotted NAME (e.g. 'notes.v2')
# into dotted-path resolution. Gating on this pattern keeps 'examine 3.1'
# on the new path while 'examine notes.v2' falls back to plain name
# resolution, unchanged.
_DOTTED_PATH_RE = re.compile(r"\d+(?:\.\d+)+")


# --------------------------------------------------------------------------
# WorldClient — thin HTTP wrapper over the agent-facing API.
# --------------------------------------------------------------------------

class WorldError(Exception):
    """The world said no — carries the server's own error text verbatim.

    Also raised for a connection-level failure (the world unreachable at
    all) — kept as the same exception type (per the spec's structure) but
    tagged with `transport=True` so callers can render an honest message:
    a transport failure means the world never got to answer, which is a
    materially different situation from the world answering with an error.
    """

    def __init__(self, message: str, transport: bool = False):
        super().__init__(message)
        self.transport = transport


class WorldClient:
    """A thin HTTP client over the umwelt world server.

    `transport`, when given, is a callable `(method, path, payload, headers) ->
    (status, body_dict)` — the seam tests use to run against a real Flask
    app via its test_client instead of real sockets (see
    tests/test_mud_client.py's `_flask_transport`). Production code leaves
    it unset and goes over urllib.

    Client-auth (umwelt #495 / #646): `_call` acquires an `Authorization`
    bearer header via `client_auth.get_auth_header` whenever the outgoing
    payload carries an `agent` field (or `agent_for_auth` is given
    explicitly, for the handful of GET verbs that embed the agent in the
    URL instead of a JSON body) — same session-cache-plus-401-retry-once
    behavior as `tools/uw`'s `call()`. A present-but-failing credential
    raises `client_auth.AuthFailure`, re-raised here as `WorldError` (fail
    closed — never silently falls through to an anonymous/declared
    request); the no-key case still falls back to today's declared
    behavior per client_auth's own compatibility-mode gate.
    """

    def __init__(self, base_url: str | None = None, timeout: int = 30,
                 transport: Callable[[str, str, dict | None, dict[str, str]],
                                      tuple[int, dict]] | None = None):
        # umwelt#699: `None` resolves lazily via _default_url() -- see that
        # function's docstring for why this isn't a module-level default.
        if base_url is None:
            base_url = _default_url()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport
        # In-memory-only session cache (umwelt #495/#646) — never persisted;
        # the server's 401 is the validity signal, same as tools/uw's
        # per-process _UW_SESSION.
        self._session = _client_auth.AgentSession()

    # -- transport -----------------------------------------------------

    def _http(self, method: str, path: str, payload: dict | None,
              auth_headers: dict[str, str] | None = None) -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if auth_headers:
            headers.update(auth_headers)
        headers.update(_engine_api.client_headers())  # umwelt#1105
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _client_auth.urlopen_with_cert_fallback(req, self.timeout) as r:
                body = r.read().decode()
                return r.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                return e.code, (json.loads(body) if body else {})
            except json.JSONDecodeError:
                return e.code, {"error": body or f"HTTP {e.code}"}
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise WorldError(f"cannot reach the world at {self.base_url}: {e}",
                              transport=True) from e

    def _call_once(self, method: str, path: str, payload: dict | None,
                    auth_headers: dict[str, str]) -> tuple[int, dict]:
        if self._transport is not None:
            return self._transport(method, path, payload, auth_headers)
        return self._http(method, path, payload, auth_headers)

    def _call(self, method: str, path: str, payload: dict | None = None,
              agent_for_auth: str | None = None) -> dict:
        """Transport + client-auth + status-gate, with a single 401-retry
        (umwelt #495/#646). `agent_for_auth` lets a GET verb that embeds the
        agent in the URL (rather than the JSON body) still participate in
        client-auth — most verbs need nothing extra since `payload["agent"]`
        is already there."""
        agent_id = agent_for_auth or (payload.get("agent") if payload else None)
        status: int
        body: dict
        for attempt in range(2):  # initial + one retry on a 401 of a bearer req
            auth_headers: dict[str, str] = {}
            if agent_id:
                try:
                    auth_headers = _client_auth.get_auth_header(
                        self.base_url, agent_id, self._session
                    )
                except _client_auth.AuthFailure as e:
                    raise WorldError(f"authentication failed: {e}") from e
            status, body = self._call_once(method, path, payload, auth_headers)
            if status == 401 and attempt == 0 and auth_headers:
                self._session.invalidate()
                continue
            break
        if status >= 400:
            if _engine_api.is_mismatch_refusal(status, body):
                raise WorldError(_engine_api.render_mismatch(body))  # umwelt#1105
            raise WorldError(body.get("error", f"request failed (status {status})"))
        return body

    # -- verbs -----------------------------------------------------------

    def register(self, agent: str, type_: str = "human", lineage: str = "",
                 avatar_description: str = "") -> dict:
        payload: dict[str, Any] = {"agent": agent, "type": type_}
        if lineage:
            payload["lineage"] = lineage
        if avatar_description:
            payload["avatar_description"] = avatar_description
        return self._call("POST", "/umwelt/register", payload)

    def move_to(self, agent: str, place: str) -> dict:
        return self._call("POST", "/umwelt/move_to", {"agent": agent, "place": place})

    def look(self, agent: str) -> dict:
        """Read-only where-am-i (umwelt #184/#219/forum #263) — resolves
        the caller's current place SERVER-SIDE from the avatar's own
        place_id, zero writes. Use this to RE-RENDER "where I am"; use
        move_to only for an actual navigation move (an explicit
        destination) — conflating the two is exactly #184's teleport
        class."""
        return self._call("POST", "/umwelt/look", {"agent": agent})

    def look_at(self, agent: str, thing: str) -> dict:
        return self._call("POST", "/umwelt/look_at", {"agent": agent, "thing": thing})

    def use(self, agent: str, thing: str, args: list[str] | None = None) -> dict:
        payload: dict[str, Any] = {"agent": agent, "thing": thing}
        if args:
            payload["args"] = args
        return self._call("POST", "/umwelt/use", payload)

    def places(self) -> list[dict]:
        resp = self._call("GET", "/umwelt/places", None)
        return resp.get("places", [])

    def places_contextual(self, agent: str) -> dict:
        """The #158 contextual render: current place + children + parent +
        lateral links + standing anchors, from this agent's standpoint."""
        from urllib.parse import quote
        return self._call("GET", f"/umwelt/places?agent={quote(agent)}", None,
                          agent_for_auth=agent)

    def map(self, agent: str, name: str = "", kind: str = "",
            owner: str = "", unread_only: bool = False,
            depth: int | None = None) -> dict:
        """The #160 interactive map — forest-union of place_tree, filterable."""
        payload: dict[str, Any] = {"agent": agent}
        if name:
            payload["name"] = name
        if kind:
            payload["kind"] = kind
        if owner:
            payload["owner"] = owner
        if unread_only:
            payload["unread_only"] = True
        if depth is not None:
            payload["depth"] = depth
        return self._call("POST", "/umwelt/map", payload)

    def set_parent(self, agent: str, place: str, parent: str | None) -> dict:
        payload: dict[str, Any] = {"agent": agent, "place": place}
        if parent:
            payload["parent"] = parent
        return self._call("POST", "/umwelt/set_parent", payload)

    def add_link(self, agent: str, src: str, dst: str,
                 label: str = "", one_way: bool = False) -> dict:
        payload: dict[str, Any] = {"agent": agent, "src": src, "dst": dst,
                                   "mutual": not one_way}
        if label:
            payload["label"] = label
        return self._call("POST", "/umwelt/add_link", payload)

    def remove_link(self, agent: str, src: str, dst: str,
                    one_way: bool = False) -> dict:
        return self._call("POST", "/umwelt/remove_link", {
            "agent": agent, "src": src, "dst": dst, "mutual": not one_way,
        })

    def crossings_here(self, agent: str, place: str, since: int | None = None,
                       advance: bool = False, limit: int | None = None) -> dict:
        payload: dict[str, Any] = {"agent": agent, "place": place}
        if since is not None:
            payload["since"] = since
        if advance:
            payload["advance"] = True
        if limit is not None:
            payload["limit"] = limit
        return self._call("POST", "/umwelt/crossings_here", payload)

    def send_crossing(self, agent: str, destination: str, sent_at: str, body: str,
                       to_agent: str | None = None) -> dict:
        payload: dict[str, Any] = {
            "agent": agent, "destination": destination,
            "sent_at": sent_at, "body": body,
        }
        if to_agent:
            payload["to_agent"] = to_agent
        return self._call("POST", "/umwelt/send_crossing", payload)

    def trace(self, agent: str, place: str, position: str) -> dict:
        return self._call("POST", "/umwelt/trace", {
            "agent": agent, "place": place, "position": position,
        })

    def create_thing(self, agent: str, place: str, spec: dict) -> dict:
        return self._call("POST", "/umwelt/create_thing", {
            "agent": agent, "place": place, "spec": spec,
        })

    # -- carrying / containers (umwelt #88) -------------------------------

    def take(self, agent: str, thing: str) -> dict:
        return self._call("POST", "/umwelt/take", {"agent": agent, "thing": thing})

    def drop(self, agent: str, thing: str) -> dict:
        return self._call("POST", "/umwelt/drop", {"agent": agent, "thing": thing})

    def inventory(self, agent: str) -> dict:
        return self._call("POST", "/umwelt/inventory", {"agent": agent})

    def put(self, agent: str, thing: str, container: str) -> dict:
        return self._call("POST", "/umwelt/put", {
            "agent": agent, "thing": thing, "container": container,
        })

    def take_out(self, agent: str, thing: str) -> dict:
        return self._call("POST", "/umwelt/take_out", {"agent": agent, "thing": thing})

    def look_in(self, agent: str, container: str) -> dict:
        return self._call("POST", "/umwelt/look_in", {"agent": agent, "container": container})

    def send_home(self, agent: str, thing: str) -> dict:
        return self._call("POST", "/umwelt/send_home", {"agent": agent, "thing": thing})


# --------------------------------------------------------------------------
# Pure render functions — dict in, str out. No I/O.
# --------------------------------------------------------------------------

def wrap_text(text: str, width: int = 78) -> str:
    """Wrap prose to min(width, 78) — the human-bandwidth rendering rule."""
    width = max(1, min(width, 78))
    if not text:
        return ""
    return "\n".join(textwrap.wrap(text, width=width, replace_whitespace=False))


def truncate_for_listing(description: str, name: str, width: int = 78,
                          max_lines: int = 2) -> str:
    """Truncate a thing's description for a place listing to ~max_lines,
    appending a '…(x <name> for more)' hint when it was cut. Full text is
    only ever shown by examine, never here."""
    width = max(1, min(width, 78))
    wrapped = textwrap.wrap(description, width=width) if description else []
    if not wrapped:
        return ""
    if len(wrapped) <= max_lines:
        return "\n".join(wrapped)
    shown = wrapped[:max_lines]
    shown[-1] = shown[-1].rstrip() + f"…(x {name} for more)"
    return "\n".join(shown)


def relative_time(iso_ts: str | None) -> str:
    """A short 'Nh ago' style relative time from an iso-utc timestamp."""
    if not iso_ts:
        return "some time ago"
    try:
        ts = iso_ts.replace("Z", "+00:00")
        then = datetime.fromisoformat(ts)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "some time ago"
    delta = datetime.now(timezone.utc) - then
    seconds = delta.total_seconds()
    if seconds < 0:
        seconds = 0
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    return f"{int(days)}d ago"


def _present_tag(p: dict, self_agent: str | None = None) -> str:
    tag = p.get("name") or p.get("agent") or "someone"
    extra = [v for v in (p.get("type"), p.get("lineage")) if v]
    if extra:
        tag += f" ({', '.join(extra)})"
    if self_agent is not None and p.get("agent") == self_agent:
        tag += " — you"
    return tag


def render_place(view: dict, self_agent: str | None = None, width: int = 78) -> str:
    """Render a place-view (move_to's / register's / enter's response shape)."""
    place = view.get("place") or {}
    lines = [f"== {place.get('label') or place.get('id', '?')} =="]
    desc = place.get("description") or ""
    if desc:
        lines.append(wrap_text(desc, width))

    present = view.get("present") or []
    if present:
        lines.append("— present: " + ", ".join(_present_tag(p, self_agent) for p in present))

    things = view.get("things") or []
    if things:
        for i, t in enumerate(things, 1):
            short = truncate_for_listing(t.get("description") or "", t["name"],
                                          width=max(10, width - 6))
            first, *rest = (short.split("\n") if short else [""])
            line = f"{i}. {t['name']} — {first}" if first else f"{i}. {t['name']}"
            for r in rest:
                line += "\n   " + r
            lines.append(line)

    unseen = view.get("unseen_count") or 0
    if unseen:
        lines.append(f"{unseen} unseen crossing{'s' if unseen != 1 else ''} — 'r' to read")

    return "\n".join(lines)


def render_thing(thing: dict, width: int = 78) -> str:
    """Render examine's full detail: description + metadata + use_stats when
    present — omitting any field the server didn't provide (spec §examine).
    """
    lines = [f"{thing.get('name', '?')} ({thing.get('kind', '?')})"]
    desc = thing.get("description") or ""
    if desc:
        lines.append(wrap_text(desc, width))

    provenance = []
    if thing.get("author"):
        provenance.append(f"forged by {thing['author']}")
    if thing.get("created_at"):
        provenance.append(thing["created_at"])
    if provenance:
        lines.append(" · ".join(provenance))

    metadata = thing.get("metadata") or {}
    use_stats = metadata.get("use_stats") if isinstance(metadata, dict) else None
    plain_meta = ({k: v for k, v in metadata.items() if k != "use_stats"}
                  if isinstance(metadata, dict) else {})
    if plain_meta:
        lines.append("metadata:")
        for k in sorted(plain_meta):
            lines.append(f"  {k}: {plain_meta[k]}")

    if isinstance(use_stats, dict) and use_stats:
        lines.append("use stats:")
        for key, label in (("creator", "creator"), ("version", "version"),
                           ("last_used", "last used"), ("success_rate", "success rate")):
            if use_stats.get(key) not in (None, ""):
                lines.append(f"  {label}: {use_stats[key]}")

    return "\n".join(lines)


def render_crossings(crossings: list[dict], width: int = 78) -> str:
    """Render a list of crossings — '[2h ago] lei: <body>' per spec."""
    if not crossings:
        return "(nothing here)"
    lines = []
    for c in crossings:
        rel = relative_time(c.get("received_at"))
        sender = c.get("sender", "?")
        body = c.get("body")
        if body:
            wrapped = wrap_text(body, max(10, width - 12))
            first, *rest = wrapped.split("\n") if wrapped else [""]
            lines.append(f"[{rel}] {sender}: {first}")
            for r in rest:
                lines.append("    " + r)
        else:
            lines.append(f"[{rel}] {sender}: (a wordless signal)")
    return "\n".join(lines)


def _marker_suffix(marker: str | None) -> str:
    """umwelt#543: `" {marker} "` when a marker string is present (the
    normal case — every real server response carries one), `" "` (no
    stray double-space) when absent — a place-shaped dict predating this
    field, or a hand-built test fixture, must not gain a widowed space.

    Byte-identical to mcp_server.py's own `_marker_suffix` — duplicated
    for simplicity (both are 3-line pure string formatting), not because
    of a structural cross-import constraint between this module and that
    one; see that copy's docstring for the corrected reasoning (reviewer-
    fairy catch on PR#545) and where the REAL process-boundary constraint
    actually lives (db.py, not here)."""
    return f" {marker} " if marker else " "


def render_map_tree(roots: list[dict], width: int = 78) -> str:
    """Render the #160 map forest as an indented tree — one line per place,
    with an unread badge when the place has unheard crossings. Pure."""
    if not roots:
        return "(the map shows nothing — no visible places match)"
    lines: list[str] = []

    def _node(n: dict, depth: int) -> None:
        pad = "  " * depth
        label = n.get("label") or n.get("id", "?")
        # umwelt#543: the real [state][owner: x] marker replaces the old
        # ad-hoc " (private)" literal — computed server-side, rendered
        # verbatim, never re-summarized here.
        marker = _marker_suffix(n.get("marker"))
        unread = n.get("unread") or 0
        badge = f"  ✉{unread}" if unread else ""
        lines.append(f"{pad}· {label}{marker}[{n.get('id')}]{badge}"[:width])
        for c in n.get("children") or []:
            _node(c, depth + 1)

    for r in roots:
        _node(r, 0)
    return "\n".join(lines)


def render_contextual_places(resp: dict, width: int = 78) -> str:
    """Render the #158 contextual /umwelt/places?agent= view — where you
    stand, its sub-places, the way up, lateral doors, anchors. Pure."""
    cur = resp.get("current") or {}
    # umwelt#543: the real [state][owner: x] marker — this function used to
    # show no privacy indicator at all for cur/children/parent.
    lines = [f"you are at: {cur.get('label', cur.get('id', '?'))}"
             f"{_marker_suffix(cur.get('marker'))}[{cur.get('id', '?')}]"]
    kids = cur.get("children") or []
    if kids:
        lines.append("sub-places here:")
        for i, k in enumerate(kids, 1):
            unread = k.get("unread") or 0
            badge = f"  ✉{unread}" if unread else ""
            lines.append(f"  {i}. {k.get('label', k['id'])}"
                         f"{_marker_suffix(k.get('marker'))}[{k['id']}]{badge}")
        skipped = resp.get("children_skipped") or 0
        if skipped:
            lines.append(f"  (+{skipped} more sub-places — 'map' shows all)")
    parent = resp.get("parent")
    if parent:
        lines.append(f"way up: {parent.get('label', parent.get('id'))}"
                     f"{_marker_suffix(parent.get('marker'))}[{parent.get('id')}]")
    links = cur.get("links") or []
    if links:
        lines.append("doors from here:")
        for l in links:
            lines.append(f"  · {l.get('dst_label', l.get('dst_id'))}"
                         f"{_marker_suffix(l.get('dst_marker'))}[{l.get('dst_id')}]")
    anchors = resp.get("anchors") or []
    if anchors:
        # umwelt#543: anchors are place_tree(depth=0) nodes same as
        # everything else here, and carry the same marker field — this
        # spot was the one line the fix missed on first pass.
        names = ", ".join(
            f"{a.get('label', a['id'])}{_marker_suffix(a.get('marker'))}"
            f"[{a['id']}]"
            for a in anchors)
        lines.append(f"always reachable: {names}")
    lines.append("('go all' lists every visible place; 'map' draws the tree)")
    return "\n".join(lines)


def render_use_outcome(outcome: dict, width: int = 78) -> str:
    """Render use()'s outcome — readable content is paged (labeled per file,
    wrapped); executable outcomes lead with the world's own briefing prose,
    then stdout/stderr verbatim (never wrapped — it's the program's own
    output, not human prose)."""
    if outcome.get("kind") == "readable":
        content = outcome.get("content") or {}
        lines = []
        briefing = outcome.get("briefing")
        if briefing:
            lines.append(wrap_text(briefing, width))
        if not content:
            lines.append("(empty)")
        for fname, text in content.items():
            lines.append(f"--- {fname} ---")
            lines.append(wrap_text(text, width) if text else "(empty)")
        return "\n".join(lines)

    lines = []
    briefing = outcome.get("briefing")
    if briefing:
        lines.append(wrap_text(briefing, width))
    stdout = (outcome.get("stdout") or "").strip()
    if stdout:
        lines.append("--- stdout ---")
        lines.append(stdout)
    stderr = (outcome.get("stderr") or "").strip()
    if stderr:
        lines.append("--- stderr ---")
        lines.append(stderr)
    return "\n".join(lines)


def render_inventory(inv: dict, width: int = 78) -> str:
    """Render inventory's response — carried things (name — weight_est) then
    the budget line, per umwelt #88 slice 3's spec template verbatim: 'carrying
    N things — load X / capacity Y (headroom Z)'."""
    things = inv.get("things") or []
    lines = [f"{t['name']} — {t['weight_est']}" for t in things]
    load = inv.get("load", 0)
    capacity = inv.get("capacity", 0)
    headroom = inv.get("headroom", capacity - load)
    lines.append(
        f"carrying {len(things)} things — load {load} / capacity {capacity} "
        f"(headroom {headroom})"
    )
    return "\n".join(lines)


def render_container_contents(look: dict, width: int = 78) -> str:
    """Render look_in's response — contents numbered 1-based (umwelt #120,
    mirroring render_place's numbered listing) so 'examine N.M'/'use N.M' and
    plain digit-index resolution inside the container have a stable position
    to reference — same name — weight_est shape as render_inventory,
    followed by the bag's own budget line, per umwelt #88 slice 3's spec
    template: 'bag load X / capacity Y'. The numbering must match the order
    the caller caches into _container_contents_cache (the exact 'things'
    list this function is given), since that cache is what digit-index
    resolution walks."""
    things = look.get("things") or []
    lines = [f"{i}. {t['name']} — {t['weight_est']}"
             for i, t in enumerate(things, 1)] or ["(empty)"]
    load = look.get("load", 0)
    capacity = look.get("capacity", 0)
    lines.append(f"bag load {load} / capacity {capacity}")
    return "\n".join(lines)


_HELP_TEXT = """commands (one-letter alias in parens):
  look (l)              re-render where you are
  go <place> (g)        walk to a place; bare 'go' shows what's near you
                          (sub-places, the way up, doors, your anchors);
                          'go all' lists every visible place flat
  map [<words>] (m)     draw the whole world tree; 'map <words>' filters
                          by name, 'map unread' shows only places with
                          unheard crossings; 'go <n>' works on map rows
  parent <place> under <parent>
                          nest a place inside another ('parent <place>
                          clear' returns it to top-level)
  link <a> <b>          open a two-way door between places
  unlink <a> <b>        remove that door
  examine <thing> (x)   inspect a thing without using it
  use <thing> (u)        use a thing; 'use <thing> -- <args>' if it takes args
  say <text> (s)         speak to this place
  whisper <name> <text> (dm)
                          address a crossing to <name> — sent to the current
                          place if they're present, else their post; still
                          visible in that place's shared history (not
                          private — true DMs wait for the trust layer, #24)
  read (r)               read new crossings here; 'read all' for full history
  read from <author>
                          only <author>'s crossings, e.g. 'read from alice'
                          ('read all from <author>' for full history)
  read find <word...>
                          keyword search over bodies, e.g. 'read find launch'
                          ('read all find <word...>' for full history)
  read about <author>
                          <author>'s crossings PLUS ones addressed to them,
                          e.g. 'read about alice' ('read all about <author>')
                          filtered reads never advance the read cursor —
                          plain 'read' is the only one that does ('read all'
                          browses full history without advancing, as always)
  who (w)                see who's here
  home                   go to your own post
  take <thing> (get)     pick a thing up
  take <thing> from <container>
                          pull a thing out of a bag
  drop <thing>           put a carried thing down where you stand
  inventory (inv, i)     what you're carrying, and your carry budget
  put <thing> in <container>
                          put a thing into a bag (held or same-place, either)
  look in <container>    see what's inside a bag
  send home <thing>      remote-move a thing (held, or yours at rest here)
                          to your own post — not trash, just not here
  forge (f)              build a new thing here — interactive prompts;
                          v1 keeps it simple: single-line content entries or
                          @/abs/path to a local file, nothing fancier yet
  help (?)               this table
  quit (q)               leave the world"""


# --------------------------------------------------------------------------
# Repl — dispatch(line) -> str. All I/O happens only in main(), EXCEPT
# forge, which is inherently a multi-prompt interactive flow — its prompt
# collection is factored into `_collect_forge_spec` (pure function fed by
# an injectable input-callable) precisely so that exception stays testable
# and doesn't otherwise leak I/O into the rest of the Repl.
# --------------------------------------------------------------------------

_ALIASES = {
    "l": "look", "g": "go", "x": "examine", "u": "use", "s": "say",
    "r": "read", "w": "who", "?": "help", "q": "quit", "f": "forge",
    "dm": "whisper", "get": "take", "inv": "inventory", "i": "inventory",
    "m": "map",
}


def _split_two(rest: str, sep: str) -> tuple[str, str | None]:
    """Split `rest` on the FIRST occurrence of `sep`, both sides stripped.
    Returns (rest, None) if sep isn't present — the caller's signal that
    only a single token/phrase was given (umwelt #88 slice 3's " in "/"
    from " parsing for put/take_out: names may contain spaces on either
    side, so a literal separator split is the whole mechanism — no
    quoting syntax)."""
    idx = rest.find(sep)
    if idx == -1:
        return rest.strip(), None
    return rest[:idx].strip(), rest[idx + len(sep):].strip()


def _collect_forge_spec(input_fn: Callable[[str], str]) -> dict | None:
    """Collect a create_thing spec via prompts fed by input_fn(prompt) -> str
    — one prompt per line so the flow stays scriptable/testable (per the
    spec). Returns None on a blank name (the explicit, quiet cancel path).
    Propagates EOFError (stdin closed mid-prompt) and OSError (an unreadable
    @file) so the caller can render a distinct, honest message for each —
    either way, nothing is ever POSTed to the world.

    v1 keeps content collection simple: each source file is either a single
    line typed directly, or `@/abs/path` to read a local file's contents —
    no multi-line paste support yet (documented in the help text too).
    """
    name = input_fn("name: ").strip()
    if not name:
        return None

    description = input_fn("one-line description: ").strip()

    kind = input_fn("kind (executable/readable) [readable]: ").strip().lower()
    if kind not in ("executable", "readable"):
        kind = "readable"

    spec: dict[str, Any] = {"name": name, "description": description, "kind": kind}
    if kind == "executable":
        spec["exec_command"] = input_fn("exec_command: ").strip()

    source: dict[str, str] = {}
    while True:
        fname = input_fn("filename (empty to finish): ").strip()
        if not fname:
            break
        content = input_fn(
            "content (single line, or @/abs/path to read a local file): "
        )
        if content.startswith("@"):
            path = content[1:].strip()
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        source[fname] = content
    spec["source"] = source

    return spec


def _normalize_place_query(s: str) -> str:
    """Lowercase and strip a leading 'the ' so 'go Square', 'go square', and
    'go the square' all normalize the same way as the label 'The Square'
    (umwelt #67 — the naturally-typed form silently missed the old
    startswith-only match)."""
    s = s.strip().lower()
    if s.startswith("the "):
        s = s[4:]
    return s


def _place_id_tail(place_id: str) -> str:
    """The last underscore-segment of a place id, lowercased —
    `place_square` -> `square` — so a bare id-tail query also resolves."""
    return place_id.rsplit("_", 1)[-1].lower()


class Ambiguous:
    """Sentinel returned by `_resolve_thing`/`_resolve_place` when a prefix
    matches more than one candidate — distinct from a genuine miss (`None`),
    so callers can render "ambiguous" rather than "no such thing here"."""

    def __init__(self, names: list[str]):
        self.names = names


class Repl:
    def __init__(self, client: WorldClient, agent: str, width: int = 78,
                 input_fn: Callable[[str], str] = input):
        self.client = client
        self.agent = agent
        self.width = width
        self.view: dict = {}
        self.place_id: str | None = None
        self.home_place_id: str | None = None
        self.quit = False
        self._things_cache: list[dict] = []
        self._places_cache: list[dict] = []
        # umwelt #88 slice 3: carried things (refreshed after any verb that
        # can change what you carry) and, per container id, the last-looked-
        # in contents (take_out's route payload is {agent, thing} only — the
        # container is derived server-side from the thing's own holder_id —
        # so the client needs the THING's id, which is only ever discoverable
        # by a prior 'look in' on that specific container).
        self._inventory_cache: list[dict] = []
        self._container_contents_cache: dict[str, list[dict]] = {}
        # Only forge's multi-prompt flow uses this — injectable so tests can
        # feed a scripted sequence without touching real stdin.
        self._input_fn = input_fn

    def set_view(self, view: dict) -> None:
        self.view = view
        place = view.get("place") or {}
        self.place_id = place.get("id", self.place_id)
        self._things_cache = view.get("things") or []
        # Captured at registration/enter time (the only doors whose response
        # carries an "avatar" dict) — move_to/look_at/etc. responses have no
        # avatar key, so this only ever updates on those two doors and is
        # otherwise left as whatever was last captured (umwelt #66).
        avatar = view.get("avatar") or {}
        home_place_id = avatar.get("home_place_id")
        if home_place_id:
            self.home_place_id = home_place_id

    # -- resolution helpers ----------------------------------------------

    def _resolve_thing(self, token: str) -> str | Ambiguous | None:
        things = self._things_cache
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(things):
                return things[idx]["id"]
            return None
        exact = [t for t in things if t["id"] == token]
        if exact:
            return exact[0]["id"]
        tl = token.lower()
        matches = [t for t in things if t["name"].lower().startswith(tl)]
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            return Ambiguous([t["name"] for t in matches])
        return None

    def _resolve_thing_anywhere(self, token: str) -> str | Ambiguous | None:
        """Resolve a thing carried OR in the current place — the carry verbs
        (drop/put/take_out's container arg/look_in's container arg/send_home)
        may reference either. Prefers carried things then same-place things
        on ambiguity (umwelt #88 slice 3's spec) — digit indices still
        resolve against the place's numbered listing only (_resolve_thing's
        existing behavior), since inventory isn't rendered with position
        numbers."""
        if token.isdigit():
            return self._resolve_thing(token)
        exact = [t for t in self._inventory_cache + self._things_cache
                 if t["id"] == token]
        if exact:
            return exact[0]["id"]
        tl = token.lower()
        carried = [t for t in self._inventory_cache
                   if t["name"].lower().startswith(tl)]
        if len(carried) == 1:
            return carried[0]["id"]
        if len(carried) > 1:
            return Ambiguous([t["name"] for t in carried])
        return self._resolve_thing(token)

    def _resolve_in_container(self, token: str, container_id: str) -> str | Ambiguous | None:
        """Resolve a thing token against a container's CACHED contents (only
        populated by a prior 'look in' on that container) — take_out's route
        payload is {agent, thing} only, so the client needs the thing's own
        id, never the container's, to make the call. A digit token indexes
        into the cached contents in the same 1-based, in-order-shown fashion
        as _resolve_thing's place-listing digit handling (umwelt #120) — the
        numbers render_container_contents prints are exactly this order."""
        contents = self._container_contents_cache.get(container_id, [])
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(contents):
                return contents[idx]["id"]
            return None
        exact = [t for t in contents if t["id"] == token]
        if exact:
            return exact[0]["id"]
        tl = token.lower()
        matches = [t for t in contents if t["name"].lower().startswith(tl)]
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            return Ambiguous([t["name"] for t in matches])
        return None

    def _resolve_dotted(self, token: str) -> str | Ambiguous | None:
        """Resolve a dotted-path token like '3.1' — examine/use reaching
        into a container without a prior explicit 'look in' (umwelt #120).
        Split on '.'; the head segment resolves against the place listing
        (the same digit/name path _resolve_thing already walks); each
        remaining segment walks into the CURRENT thing's contents, and if
        those contents aren't already cached, issues an IMPLICIT 'look in'
        (populating _container_contents_cache exactly as an explicit 'look
        in' would) so 'examine 3.1' works with no prior 'look in 3'. The
        leaf id is handed back unchanged — the caller's existing
        look_at/use call doesn't know or care it came from a nested path.
        A mid-path thing that isn't actually a container surfaces as the
        server's own look_in error (422, via WorldError) — same as any
        other world-said-no case, left to dispatch()'s own catch to render,
        never swallowed here. Written to walk N segments even though
        containment is single-level today (bag-in-bag is disallowed) so it
        composes if depth grows."""
        segments = token.split(".")
        current_id = self._resolve_thing(segments[0])
        # Ambiguous is unreachable here under the current _DOTTED_PATH_RE
        # gate (every segment is digits-only, and a digit never resolves to
        # Ambiguous) — kept so this still composes correctly if the gate is
        # ever loosened to allow name segments.
        if isinstance(current_id, Ambiguous) or current_id is None:
            return current_id
        for seg in segments[1:]:
            if current_id not in self._container_contents_cache:
                resp = self.client.look_in(self.agent, current_id)
                self._container_contents_cache[current_id] = resp.get("things") or []
            current_id = self._resolve_in_container(seg, current_id)
            # Same note as above — unreachable today, kept for composability.
            if isinstance(current_id, Ambiguous) or current_id is None:
                return current_id
        return current_id

    def _refresh_inventory(self) -> None:
        """Best-effort resync of the carried-things cache after any verb that
        can change it — mirrors how _cmd_look/_cmd_go resync _things_cache
        via set_view() after move_to. Swallows WorldError (e.g. no avatar
        yet) since this is a cache-freshness nicety, not the verb's own
        result — the caller's own client.<verb>() call already raised/
        surfaced any real error before this runs."""
        try:
            resp = self.client.inventory(self.agent)
            self._inventory_cache = resp.get("things") or []
        except WorldError:
            pass

    def _resolve_place(self, token: str) -> str | Ambiguous:
        if token.isdigit() and self._places_cache:
            idx = int(token) - 1
            if 0 <= idx < len(self._places_cache):
                return self._places_cache[idx]["id"]
        places = self._places_cache or self.client.places()
        for p in places:
            if p["id"] == token:
                return p["id"]
        tl = _normalize_place_query(token)
        matches = [p for p in places
                   if _normalize_place_query(p.get("label", "")).startswith(tl)
                   or _normalize_place_query(p["id"]).startswith(tl)
                   or _place_id_tail(p["id"]).startswith(tl)]
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            return Ambiguous([p.get("label") or p["id"] for p in matches])
        # Never invent a place — fall through to the literal token and let
        # the server give its own (honest) 404 if it doesn't exist.
        return token

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, line: str) -> str:
        line = line.strip()
        if not line:
            return ""
        parts = line.split(None, 1)
        cmd = _ALIASES.get(parts[0].lower(), parts[0].lower())
        rest = parts[1] if len(parts) > 1 else ""
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            return f"unknown command: {parts[0]!r} — type 'help' for the command table"
        try:
            return handler(rest)
        except WorldError as e:
            if e.transport:
                return str(e)
            return f"the world says: {e}"

    # -- commands ----------------------------------------------------------

    def _cmd_look(self, rest: str) -> str:
        rest = rest.strip()
        # 'look in <container>' (umwelt #88 slice 3) — bare 'look' and any
        # other rest content keep the original re-render-place behavior
        # unchanged (rest was already ignored there before this addition).
        if rest.lower() == "in" or rest.lower().startswith("in "):
            container_part = rest[2:].strip()
            if not container_part:
                return "look in what?"
            container_id = self._resolve_thing_anywhere(container_part)
            if isinstance(container_id, Ambiguous):
                return f"ambiguous: matches {', '.join(container_id.names)} — be more specific"
            if container_id is None:
                return f"no such thing here: {container_part!r}"
            resp = self.client.look_in(self.agent, container_id)
            self._container_contents_cache[container_id] = resp.get("things") or []
            return render_container_contents(resp, self.width)
        # umwelt #184/#219/forum #263: re-pointed off the side-effecting
        # move_to (which teleported the avatar back to self.place_id
        # whenever this REPL session's in-memory idea of "where I am" had
        # drifted from the server's own — e.g. another client moved this
        # agent's avatar while this session was idle) onto the read-only
        # /umwelt/look, which resolves the real current place server-side.
        resp = self.client.look(self.agent)
        self.set_view(resp)
        return render_place(resp, self.agent, self.width)

    def _cmd_go(self, rest: str) -> str:
        rest = rest.strip()
        if rest.lower() == "all":
            places = self.client.places()
            self._places_cache = places
            lines = ["places you can go:"]
            for i, p in enumerate(places, 1):
                lines.append(f"{i}. {p.get('label', p['id'])} ({p['id']})")
            return "\n".join(lines)
        if not rest:
            # Contextual view (#158): sub-places, way up, doors, anchors.
            # Falls back to the flat list if the server predates ?agent=.
            try:
                resp = self.client.places_contextual(self.agent)
            except WorldError:
                return self._cmd_go("all")
            if not resp.get("current"):
                return self._cmd_go("all")
            # Number/prefix resolution works against what was just shown:
            cache: list[dict] = []
            cur = resp.get("current") or {}
            cache.extend(cur.get("children") or [])
            if resp.get("parent"):
                cache.append(resp["parent"])
            for l in (cur.get("links") or []):
                cache.append({"id": l.get("dst_id"),
                              "label": l.get("dst_label", l.get("dst_id"))})
            cache.extend(resp.get("anchors") or [])
            self._places_cache = [c for c in cache if c.get("id")]
            return render_contextual_places(resp, self.width)
        place_id = self._resolve_place(rest)
        if isinstance(place_id, Ambiguous):
            return f"ambiguous: matches {', '.join(place_id.names)} — be more specific"
        resp = self.client.move_to(self.agent, place_id)
        self.set_view(resp)
        return render_place(resp, self.agent, self.width)

    def _cmd_map(self, rest: str) -> str:
        # 'map' — the whole visible forest; 'map <words>' filters by name
        # substring; 'map unread' keeps only places with unheard crossings.
        rest = rest.strip()
        kwargs: dict[str, Any] = {}
        if rest.lower() == "unread":
            kwargs["unread_only"] = True
        elif rest:
            kwargs["name"] = rest
        resp = self.client.map(self.agent, **kwargs)
        roots = resp.get("roots") or []
        # Make 'go <n>' work against the map too: flatten in render order.
        flat: list[dict] = []

        def _walk(n: dict) -> None:
            flat.append({"id": n.get("id"), "label": n.get("label")})
            for c in n.get("children") or []:
                _walk(c)

        for r in roots:
            _walk(r)
        if flat:
            self._places_cache = flat
        return render_map_tree(roots, self.width)

    def _cmd_parent(self, rest: str) -> str:
        # 'parent <place> under <parent>' | 'parent <place> clear'
        tokens = rest.strip()
        if " under " in tokens:
            child_part, parent_part = tokens.split(" under ", 1)
            parent_id = self._resolve_place(parent_part.strip())
            if isinstance(parent_id, Ambiguous):
                return (f"ambiguous parent: matches "
                        f"{', '.join(parent_id.names)} — be more specific")
        elif tokens.endswith(" clear"):
            child_part, parent_id = tokens[: -len(" clear")], None
        else:
            return ("usage: 'parent <place> under <parent>' to nest, "
                    "'parent <place> clear' for top-level")
        child_id = self._resolve_place(child_part.strip())
        if isinstance(child_id, Ambiguous):
            return (f"ambiguous place: matches {', '.join(child_id.names)} "
                    "— be more specific")
        resp = self.client.set_parent(self.agent, child_id, parent_id)
        newp = resp.get("parent")
        return (f"{resp.get('place')} is now top-level" if not newp
                else f"{resp.get('place')} now sits inside {newp}")

    def _cmd_link(self, rest: str) -> str:
        return self._link_or_unlink(rest, add=True)

    def _cmd_unlink(self, rest: str) -> str:
        return self._link_or_unlink(rest, add=False)

    def _link_or_unlink(self, rest: str, add: bool) -> str:
        verb = "link" if add else "unlink"
        text = rest.strip()
        # Multi-word natural names: 'link the workshop and the square'.
        # Without ' and ', the first whitespace splits the two tokens
        # (ids and single-word prefixes).
        if " and " in text:
            parts = [p.strip() for p in text.split(" and ", 1)]
        else:
            parts = text.split(None, 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return (f"usage: '{verb} <place-a> <place-b>' — use "
                    f"'{verb} <a> and <b>' when names have spaces")
        a_id = self._resolve_place(parts[0].strip())
        if isinstance(a_id, Ambiguous):
            return f"ambiguous: matches {', '.join(a_id.names)} — be more specific"
        b_id = self._resolve_place(parts[1].strip())
        if isinstance(b_id, Ambiguous):
            return f"ambiguous: matches {', '.join(b_id.names)} — be more specific"
        if add:
            self.client.add_link(self.agent, a_id, b_id)
            return f"linked {a_id} <-> {b_id}"
        self.client.remove_link(self.agent, a_id, b_id)
        return f"unlinked {a_id} <-> {b_id}"

    def _cmd_examine(self, rest: str) -> str:
        token = rest.strip()
        if not token:
            return "examine what?"
        # look_at-ONLY avatar-inclusive resolution (umwelt #146 twin — uw
        # and the MCP tool both have this; the MUD never did, umwelt #175):
        # a PRESENT avatar matched by avatar_id or case-insensitive exact
        # agent/name WINS before thing-prefix resolution, so 'examine
        # kepler' looks at the person even when things share the prefix
        # (kepler's telescope...). Exact-match-only keeps 'examine kep'
        # resolving things exactly as before; every other verb stays
        # id-only — avatars never become take/use/drop-able through this.
        tl = token.lower()
        for p in (self.view.get("present") or []):
            if not isinstance(p, dict):
                continue
            av_id = p.get("avatar_id")
            if not av_id:
                continue    # can't look_at an avatar the view didn't id
            if token == av_id \
                    or tl == (p.get("agent") or "").lower() \
                    or tl == (p.get("name") or "").lower():
                resp = self.client.look_at(self.agent, av_id)
                out = render_thing(resp["thing"], self.width)
                status = resp.get("status")
                if status is not None and \
                        (resp["thing"].get("kind") == "avatar"):
                    bits = []
                    if status.get("emotion"):
                        bits.append(f"feeling {status['emotion']}")
                    action_desc = _avatar_status.describe_last_action(
                        status.get("last_action"))
                    if action_desc:
                        bits.append(f"last action: {action_desc}")
                    if status.get("last_active_at"):
                        bits.append("active "
                                    f"{relative_time(status['last_active_at'])}")
                    state_line = _avatar_status.describe_state(status)
                    if state_line:
                        bits.append(state_line)
                    if bits:
                        out += "\n  — " + " · ".join(bits)
                return out
        # dotted path (umwelt #120, e.g. '3.1') reaches into a container —
        # gated on the POSITIONAL pattern (digits only), not a bare '.'
        # test, so a legitimately dotted thing NAME (e.g. 'notes.v2') keeps
        # its exact prior name-resolution behavior unchanged.
        thing_id = (self._resolve_dotted(token) if _DOTTED_PATH_RE.fullmatch(token)
                    else self._resolve_thing(token))
        if isinstance(thing_id, Ambiguous):
            return f"ambiguous: matches {', '.join(thing_id.names)} — be more specific"
        if thing_id is None:
            return f"no such thing here: {token!r}"
        resp = self.client.look_at(self.agent, thing_id)
        return render_thing(resp["thing"], self.width)

    def _cmd_use(self, rest: str) -> str:
        rest = rest.strip()
        if not rest:
            return "use what? try 'use <thing>'"
        thing_token, _, args_str = rest.partition(" -- ")
        thing_token = thing_token.strip()
        args = args_str.strip().split() if args_str.strip() else None
        # dotted path (umwelt #120, e.g. '3.1') reaches into a container —
        # gated on the POSITIONAL pattern (digits only), not a bare '.'
        # test, so a legitimately dotted thing NAME (e.g. 'notes.v2') keeps
        # its exact prior name-resolution behavior unchanged.
        thing_id = (self._resolve_dotted(thing_token)
                    if _DOTTED_PATH_RE.fullmatch(thing_token)
                    else self._resolve_thing(thing_token))
        if isinstance(thing_id, Ambiguous):
            return f"ambiguous: matches {', '.join(thing_id.names)} — be more specific"
        if thing_id is None:
            return f"no such thing here: {thing_token!r}"
        resp = self.client.use(self.agent, thing_id, args=args)
        outcome = resp.get("outcome", {})
        if "error" in outcome:
            return f"the world says: {outcome['error']}"
        return render_use_outcome(outcome, self.width)

    def _cmd_say(self, rest: str) -> str:
        text = rest.strip()
        if not text:
            return "say what?"
        sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        self.client.send_crossing(self.agent, self.place_id, sent_at, text)
        return f"you say: {text}"

    def _cmd_whisper(self, rest: str) -> str:
        # Client sugar over the engine's existing to_agent addressing
        # (umwelt #76) — NOT private messaging: the crossing still lands in
        # the destination place's shared history (the world's honesty
        # invariant), only its rendering for the addressee changes. Target
        # resolution: if <name>'s avatar is present in the CALLER's current
        # place, address the crossing there; otherwise send it to their post
        # (place_post_<canonical name>) — reusing send_crossing/to_agent, the
        # same path 'say' uses, rather than a new HTTP call.
        parts = rest.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            return "whisper who, what? try 'whisper <name> <message>'"
        name_token, body = parts[0], parts[1].strip()
        target = _canonical_agent(name_token)
        present = self.view.get("present") or []
        is_present = any(p.get("agent") == target for p in present)
        destination = self.place_id if is_present else f"place_post_{target}"
        sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        self.client.send_crossing(self.agent, destination, sent_at, body,
                                   to_agent=name_token)
        where = "here" if is_present else "their post"
        return f"you whisper to {name_token} ({where}): {body}"

    def _cmd_read(self, rest: str) -> str:
        # 'read [all] [from <author> | find <word...> | about <author>]'
        # (umwelt #105 Tier 1) — the all/new distinction (since-cursor) and
        # the client-side filter are orthogonal and compose: 'all' picks the
        # source range, the filter narrows what's shown from it. A FILTERED
        # read (from/find/about, with or without 'all') never advances the
        # read cursor — the human is searching, not catching up; advancing
        # on a filtered read would silently skip unfiltered messages past
        # the new cursor position (same bug class as the uw client's
        # say-cursor bug).
        tokens = rest.strip().split()
        full = bool(tokens) and tokens[0].lower() == "all"
        idx = 1 if full else 0

        filter_kind: str | None = None
        filter_arg = ""
        if idx < len(tokens):
            kw = tokens[idx].lower()
            if kw not in ("from", "find", "about"):
                return (f"unknown read filter: {tokens[idx]!r} — try 'read', "
                         "'read all', 'read from <author>', "
                         "'read find <word...>', or 'read about <author>'")
            filter_kind = kw
            filter_arg = " ".join(tokens[idx + 1:]).strip()
            if not filter_arg:
                return f"read {kw} what? try 'read {kw} <...>'"

        plain_catchup = filter_kind is None and not full
        if plain_catchup:
            # #157: the server advances the cursor with the read (one
            # round-trip, no separate trace) and bounds the reply — a stale
            # cursor can never flood the terminal. The skipped count keeps
            # the bound honest.
            resp = self.client.crossings_here(self.agent, self.place_id,
                                              advance=True, limit=50)
        else:
            resp = self.client.crossings_here(self.agent, self.place_id,
                                              since=0 if full else None)
        crossings = resp.get("crossings") or []

        if filter_kind == "from":
            target = _canonical_agent(filter_arg)
            crossings = [c for c in crossings
                         if _canonical_agent(c.get("sender", "")) == target]
        elif filter_kind == "about":
            target = _canonical_agent(filter_arg)
            crossings = [c for c in crossings
                         if _canonical_agent(c.get("sender", "")) == target
                         or _canonical_agent(c.get("to_agent") or "") == target]
        elif filter_kind == "find":
            words = filter_arg.lower().split()
            crossings = [c for c in crossings
                         if all(w in (c.get("body") or "").lower() for w in words)]

        if plain_catchup:
            if not crossings:
                return "no new crossings here."
            out = render_crossings(crossings, self.width)
            skipped = resp.get("skipped") or 0
            if skipped:
                out += (f"\n(+{skipped} older not shown — "
                        "'read all' browses the full history)")
            return out
        return render_crossings(crossings, self.width)

    def _cmd_who(self, rest: str) -> str:
        present = self.view.get("present") or []
        if not present:
            return "no one else is here."
        return "\n".join(f"- {_present_tag(p, self.agent)}" for p in present)

    def _cmd_take(self, rest: str) -> str:
        # 'take <thing>' -> /umwelt/take · 'take <thing> from <container>'
        # -> /umwelt/take_out (umwelt #88 slice 3). " from " may appear
        # inside neither name normally; a literal split is the mechanism,
        # same posture as put's " in " split below.
        rest = rest.strip()
        if not rest:
            return "take what?"
        thing_part, container_part = _split_two(rest, " from ")
        if container_part is not None:
            if not container_part:
                return "take what from what? try 'take <thing> from <container>'"
            container_id = self._resolve_thing_anywhere(container_part)
            if isinstance(container_id, Ambiguous):
                return f"ambiguous: matches {', '.join(container_id.names)} — be more specific"
            if container_id is None:
                return f"no such thing here: {container_part!r}"
            thing_id = self._resolve_in_container(thing_part, container_id)
            if isinstance(thing_id, Ambiguous):
                return f"ambiguous: matches {', '.join(thing_id.names)} — be more specific"
            if thing_id is None:
                return f"no such thing here: {thing_part!r}"
            resp = self.client.take_out(self.agent, thing_id)
            self._refresh_inventory()
            return (f"you take {thing_part} out of {container_part} — "
                    f"carrying {resp['load']}/{resp['capacity']}")
        thing_id = self._resolve_thing(thing_part)
        if isinstance(thing_id, Ambiguous):
            return f"ambiguous: matches {', '.join(thing_id.names)} — be more specific"
        if thing_id is None:
            return f"no such thing here: {thing_part!r}"
        resp = self.client.take(self.agent, thing_id)
        self._refresh_inventory()
        return f"you take {thing_part} — carrying {resp['load']}/{resp['capacity']}"

    def _cmd_drop(self, rest: str) -> str:
        token = rest.strip()
        if not token:
            return "drop what?"
        thing_id = self._resolve_thing_anywhere(token)
        if isinstance(thing_id, Ambiguous):
            return f"ambiguous: matches {', '.join(thing_id.names)} — be more specific"
        if thing_id is None:
            return f"no such thing here: {token!r}"
        self.client.drop(self.agent, thing_id)
        self._refresh_inventory()
        return f"you drop {token}."

    def _cmd_inventory(self, rest: str) -> str:
        resp = self.client.inventory(self.agent)
        self._inventory_cache = resp.get("things") or []
        return render_inventory(resp, self.width)

    def _cmd_put(self, rest: str) -> str:
        # 'put <thing> in <container>' -> /umwelt/put (umwelt #88 slice 3).
        rest = rest.strip()
        thing_part, container_part = _split_two(rest, " in ")
        if not rest or container_part is None or not container_part:
            return "put what in what? try 'put <thing> in <container>'"
        thing_id = self._resolve_thing_anywhere(thing_part)
        if isinstance(thing_id, Ambiguous):
            return f"ambiguous: matches {', '.join(thing_id.names)} — be more specific"
        if thing_id is None:
            return f"no such thing here: {thing_part!r}"
        container_id = self._resolve_thing_anywhere(container_part)
        if isinstance(container_id, Ambiguous):
            return f"ambiguous: matches {', '.join(container_id.names)} — be more specific"
        if container_id is None:
            return f"no such thing here: {container_part!r}"
        resp = self.client.put(self.agent, thing_id, container_id)
        self._refresh_inventory()
        return (f"you put {thing_part} in {container_part} — bag load "
                f"{resp['load']}/{resp['capacity']}")

    def _cmd_send(self, rest: str) -> str:
        # 'send home <thing>' -> /umwelt/send_home (umwelt #88 slice 3).
        # Two-word command: dispatch()'s first token is 'send', so this
        # handler owns parsing the leading 'home' itself.
        # Word-boundary split, not a fixed-width slice: 'send homework'
        # must NOT parse as 'send home work' (reviewer, PR #94).
        parts = rest.strip().split(None, 1)
        if not parts or parts[0].lower() != "home":
            return "send what? try 'send home <thing>'"
        token = parts[1].strip() if len(parts) > 1 else ""
        if not token:
            return "send home what?"
        thing_id = self._resolve_thing_anywhere(token)
        if isinstance(thing_id, Ambiguous):
            return f"ambiguous: matches {', '.join(thing_id.names)} — be more specific"
        if thing_id is None:
            return f"no such thing here: {token!r}"
        self.client.send_home(self.agent, thing_id)
        self._refresh_inventory()
        return f"you send {token} home."

    def _cmd_forge(self, rest: str) -> str:
        try:
            spec = _collect_forge_spec(self._input_fn)
        except EOFError:
            return "forge cancelled (stdin closed)."
        except OSError as e:
            return f"forge cancelled: {e}"
        if spec is None:
            return "forge cancelled."
        resp = self.client.create_thing(self.agent, self.place_id, spec)
        return f"forged {spec['name']!r} — thing id {resp.get('thing_id')}"

    def _cmd_home(self, rest: str) -> str:
        # Prefer the home_place_id captured off the avatar dict at
        # registration/enter time (umwelt #66) — falls back to the old
        # reconstructed convention for engines that haven't shipped the
        # field yet, with no behavior change in that case.
        home_place_id = self.home_place_id or f"place_post_{self.agent}"
        resp = self.client.move_to(self.agent, home_place_id)
        self.set_view(resp)
        return render_place(resp, self.agent, self.width)

    def _cmd_help(self, rest: str) -> str:
        return _HELP_TEXT

    def _cmd_quit(self, rest: str) -> str:
        self.quit = True
        return "The world keeps. See you."


# --------------------------------------------------------------------------
# main() — the only place that does terminal I/O.
# --------------------------------------------------------------------------

def _prompt_registration(agent_given: str | None) -> tuple[str, str, str, str]:
    """Interactive registration prompts. Returns (name, type, lineage,
    avatar_description). Skipped entirely when --agent is given on the
    command line — see the ambiguous-decisions note in the handoff: the
    spec's session-flow describes prompting even with --agent, but a
    non-interactive --agent invocation (the smoke-test's use case) cannot
    coexist with blocking stdin prompts before the command loop starts."""
    name = agent_given
    while not name:
        name = input("name: ").strip()

    ans = input(f"[{name}] returning or new? [r/N]: ").strip().lower()
    is_returning = ans.startswith("r")

    atype, lineage, avatar_description = "human", "", ""
    if not is_returning:
        t = input("type (human/agent) [human]: ").strip().lower()
        atype = t if t in ("human", "agent") else "human"
        if atype == "agent":
            lineage = input("lineage (e.g. anthropic:opus) [optional]: ").strip()
        avatar_description = input("describe your avatar (one line) [optional]: ").strip()

    return name, atype, lineage, avatar_description


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m umwelt.mud",
        description="A line-based terminal MUD client for the umwelt world.",
    )
    parser.add_argument("--agent", default=None,
                        help="agent/human name — skips the interactive prompts")
    # umwelt#699 §1: default=None here, not a pre-resolved value -- both so
    # resolve_world_url() sees the parsed VALUE (an explicit --url is a
    # caller override an eager default would bypass), routed through the
    # resolver just below after parse_args(), AND so building this parser
    # (including its help text) never itself triggers resolution -- see
    # _default_url()'s docstring for why that matters on a broken config.
    parser.add_argument("--url", default=None,
                        help="world server base url "
                             "(default: see client_auth.resolve_world_url)")
    parser.add_argument("--tui", action="store_true",
                        help="launch the curses sticky-pane TUI instead of the line REPL "
                             "(umwelt #74)")
    parser.add_argument("--tui2", action="store_true",
                        help="launch the INTERACTIVE TUI — arrow-key selection, "
                             "Enter action menus, scrollable panes, map overlay "
                             "(umwelt #173)")
    args = parser.parse_args(argv)
    # umwelt#699 §1: route the parsed value back through the chokepoint --
    # explicit=None (nothing passed on the command line) still resolves via
    # the same env/config/default chain _default_url() uses.
    args.url = _client_auth.resolve_world_url(explicit=args.url)

    print("=" * 40)
    print("Umwelt — walk the world")
    print("=" * 40)

    if args.agent:
        # Non-interactive identity: register with sensible defaults, no prompts.
        name, atype, lineage, avatar_description = args.agent, "human", "", ""
    else:
        name, atype, lineage, avatar_description = _prompt_registration(None)

    # umwelt #73 improvement: send the TYPED form to register — the server
    # now case-folds identity at every door and preserves the typed
    # capitalization as display metadata on first birth (e.g. "Lei's Post").
    # A client-side lowercase here used to defeat that: it stripped the case
    # before the identity ever left the client, so a human typing "Lei"
    # always landed as "lei" server-side too — dead display-preservation
    # code. The Repl's OWN internal identity (self.agent, used for local
    # self-tag comparisons against server-returned canonical agent ids in
    # the "present" list, and as the place_post_<agent> fallback) is
    # canonicalized separately below — the server always returns/expects the
    # canonical form there regardless of what was typed at registration, so
    # self.agent must match it or the "— you" tag and the home-place
    # fallback silently break. canonical_name below is computed via the same
    # `_ids.canonical_agent` fold the server uses (umwelt #73 S2) — a local
    # `.lower()` here would silently diverge from the server's fold for any
    # non-ASCII agent name.
    typed_name = name.strip()

    client = WorldClient(args.url)
    try:
        resp = client.register(typed_name, atype, lineage, avatar_description)
    except WorldError as e:
        print(str(e) if e.transport else f"the world says: {e}")
        return

    canonical_name = _canonical_agent(typed_name)

    if args.tui2:
        from umwelt import mud_tui2
        mud_tui2.launch(client, canonical_name, resp)
        return

    if args.tui:
        # Import inside the branch: non-curses platforms (or plain-REPL
        # invocations, the default) never pay for it. Pass the CANONICAL
        # name (not the raw typed name) so the TUI's internal Repl gets the
        # same server-folded identity the line REPL does — otherwise the
        # "— you" tag + home-place fallback silently diverge for non-ASCII
        # names (umwelt #73), the exact bug canonical_name exists to prevent.
        from umwelt import mud_tui
        mud_tui.launch(client, canonical_name, resp)
        return

    width = min(shutil.get_terminal_size(fallback=(78, 24)).columns, 78)
    repl = Repl(client, canonical_name, width=width)
    repl.set_view(resp)

    if resp.get("born"):
        print("Your body is here now.")
    ignored = resp.get("ignored")
    if ignored:
        print(f"(ignored on reconnect: {', '.join(ignored)})")

    print(render_place(resp, canonical_name, width))

    while not repl.quit:
        place = repl.view.get("place") or {}
        prompt = f"you@{place.get('label') or place.get('id') or '?'}> "
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            print(repl._cmd_quit(""))
            break
        out = repl.dispatch(line)
        if out:
            print(out)


if __name__ == "__main__":
    if __package__:
        # `python -m umwelt.mud` runs this file as __main__ while
        # `umwelt.mud` stays separately importable — two parallel copies
        # of every class (umwelt #176: except WorldError stops matching).
        # Delegate to the canonical import so one class identity exists.
        from umwelt.mud import main as _canonical_main
        _canonical_main()
    else:
        main()
