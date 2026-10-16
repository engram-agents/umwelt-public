"""One crossing-visibility module, two named policies (umwelt#862 D3;
docs/design/privacy-authorization.md rev 3, crossing rule 4).

  may_view_crossing(conn, viewer, crossing)
      Rule 4: a crossing is viewable by its author, by an explicit addressee,
      and by anyone who may ENTER the place it was posted in. Used by
      open/thread/reply and by whats_new's inclusion.

  discovery_visible(conn, viewer, crossing)
      The deliberately narrower policy for search: the enter-the-destination
      arm ONLY. A discovery surface never reveals private-room traffic by
      addressee or author (those still reach them through crossings_for_agent
      and open). Widening it is a separate ruling, not a drifted copy.

  may_view_thing(conn, viewer, thing)
      Rule 3 (umwelt#982): a thing is viewable by whoever may ENTER the place
      that holds it, resolved transitively through its holders. Returns a
      REASON (`ThingView`), not a bare bool, so every by-id read route builds
      the same informational refusal from one place. Rests on the same
      `db._place_interior_visible_to` predicate as the crossing rules above,
      so the two cannot drift apart.

Both are built on `_interior_visible`, so the restriction is stated, not
re-derived. "Explicit addressee" is `to_agent == viewer` OR `viewer` in the
crossing's `mentions`: the same two-arm reach whats_new's addressed bucket
and get_crossings_for_addressee already use.

`viewer` is whatever identity the caller resolved (today caller-declared, so
this enforces the name it is handed -- see privacy-authorization.md §4: it
cannot be confidentiality until credentials land). `viewer=None` sees only
interior-visible (public-place) crossings. Neither function raises on a
malformed crossing or a failed destination lookup (sqlite3.Error, KeyError,
TypeError): it simply is not viewable (fail closed, so a swallowed database
error reads as "not found", not as a server error). Any OTHER exception is a
bug and propagates.
"""
from __future__ import annotations

import sqlite3
from typing import Any, NamedTuple

from . import db
from ._ids import canonical_agent


def _interior_visible(conn, viewer: str | None, crossing: Any,
                      place_cache: dict | None = None) -> bool:
    """May `viewer` enter the crossing's destination place? A destination
    that does not resolve to a place counts as NOT interior-visible."""
    if not isinstance(crossing, dict):
        return False
    dest = crossing.get("destination")
    if not isinstance(dest, str):
        return False
    try:
        if place_cache is not None and dest in place_cache:
            place = place_cache[dest]
        else:
            place = db.get_place(conn, dest)
            if place_cache is not None:
                place_cache[dest] = place
        return place is not None and bool(
            db._place_interior_visible_to(place, viewer))
    except (sqlite3.Error, KeyError, TypeError):
        # Fail closed, but only on what a lookup can legitimately raise (a
        # DB error, a place row missing a key, an unhashable destination);
        # anything else is a bug and must surface, not read as "not visible".
        return False


def _is_author(viewer: str | None, crossing: dict) -> bool:
    return viewer is not None and crossing.get("sender") == viewer


def is_addressee(viewer: str | None, crossing: Any) -> bool:
    """Is `viewer` an explicit addressee: `to_agent == viewer` OR `viewer`
    in `mentions`? Public so a feed's `for_you` flag reads the same arm
    `may_view_crossing` does, not a second copy of it."""
    if viewer is None or not isinstance(crossing, dict):
        return False
    if crossing.get("to_agent") == viewer:
        return True
    mentions = crossing.get("mentions")
    return isinstance(mentions, (list, tuple)) and viewer in mentions


def may_view_crossing(conn, viewer: str | None, crossing: Any,
                      place_cache: dict | None = None) -> bool:
    """Rule 4: author OR explicit addressee OR may-enter-the-destination.

    `place_cache` (optional, caller-owned) memoizes destination lookups for
    a caller that checks many crossings in one request (whats_new)."""
    if not isinstance(crossing, dict):
        return False
    return (_is_author(viewer, crossing)
            or is_addressee(viewer, crossing)
            or _interior_visible(conn, viewer, crossing, place_cache))


def discovery_visible(conn, viewer: str | None, crossing: Any,
                      place_cache: dict | None = None) -> bool:
    """The search policy: may-enter-the-destination ONLY."""
    return _interior_visible(conn, viewer, crossing, place_cache)


class ThingView(NamedTuple):
    """The verdict of `may_view_thing`: a reason, not a bare bool.

    reason: "ok" | "in_private_place" | "binned" | "unresolvable".
    `place` is the PUBLIC projection of the place the holder walk ended at
    (set for "in_private_place" only). `binned_at` / `can_restore` are set for
    "binned" only (`can_restore` is the list of agents who may un-bin it:
    today `restore_thing` is author-only, so the author is the one name)."""
    ok: bool
    reason: str
    place: dict | None = None
    binned_at: str | None = None
    can_restore: list | None = None


_OK = ThingView(True, "ok")
_UNRESOLVABLE = ThingView(False, "unresolvable")


def may_view_thing(conn, viewer: str | None, thing: Any) -> ThingView:
    """May `viewer` read `thing` by id? (umwelt#982, privacy design rule 3.)

    Holder walk, with a visited set:
      - holder_kind 'place'     -> the thing's own place_id;
      - holder_kind 'avatar'    -> the place that avatar stands in NOW (the
        carried thing follows its carrier; an avatar is itself a thing whose
        own holder is a place);
      - holder_kind 'container' -> the container's holder, recursively.
    "May enter" is `db._place_interior_visible_to`, the predicate `move_to`
    refuses entry with. A cycle, a missing holder, a missing place or a
    malformed row is "unresolvable": FAIL CLOSED, caught only on what a lookup
    can legitimately raise (sqlite3.Error, KeyError, TypeError); anything else
    is a bug and propagates.

    Lifecycle: a LANDFILLED thing is public (the landfill is a public place;
    Lei's ruling named binned only) and is not walked at all. A BINNED thing
    read by anyone other than its author is refused as "binned" -- after the
    place check, so a binned thing in a private place still reports the
    place, which is public, rather than who may un-bin it.

    `viewer` is whatever identity the caller RESOLVED (`resolve_actor`), never
    a raw body field. None / "" sees public places only.
    """
    viewer = viewer or None
    try:
        if not isinstance(thing, dict):
            return _UNRESOLVABLE
        if thing["status"] == "landfilled":
            return _OK
        current = thing
        seen: set = set()
        place = None
        while place is None:
            tid = current["id"]
            if tid in seen:
                return _UNRESOLVABLE  # container / carrier cycle
            seen.add(tid)
            kind = current["holder_kind"]
            if kind == "place":
                place = db.get_place(conn, current["place_id"])
                if place is None:
                    return _UNRESOLVABLE  # dangling place
            elif kind in ("avatar", "container"):
                current = db.get_thing(conn, current["holder_id"])
                if current is None:
                    return _UNRESOLVABLE  # dangling holder
            else:
                return _UNRESOLVABLE
        if not db._place_interior_visible_to(place, viewer):
            return ThingView(False, "in_private_place",
                             place=db._place_public_projection(place))
        if thing["status"] == "binned" and (
                viewer is None
                or viewer != canonical_agent(thing["author"])):
            return ThingView(False, "binned", binned_at=thing["binned_at"],
                             can_restore=[canonical_agent(thing["author"])])
        return _OK
    except (sqlite3.Error, KeyError, TypeError):
        return _UNRESOLVABLE
