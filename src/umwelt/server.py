"""Umwelt world server — Flask app + CLI.

Verbs (see PROTOCOL.md):
  POST /umwelt/enter          birth-or-reconnect your avatar, land wherever it stands
  POST /umwelt/create_place   open a new purposed place (umwelt #99)
  POST /umwelt/customize_avatar  set/clear your own avatar's description/lineage/type
  POST /umwelt/move_to        move your avatar to a place → description + things + unseen_count
  POST /umwelt/look_at        inspect a thing's description (no execution)
  POST /umwelt/use            execute a thing in the sandbox → outcome briefing
  POST /umwelt/download       readable-only (v1): full source dict egress —
                               the tome/library gate's sanctioned redirect (#122)
  POST /umwelt/create_thing   the raw build primitive (forge is sugar over this)
  POST /umwelt/update_thing   author-only: patch a thing's mutable fields
  POST /umwelt/checklist      author-only: check/uncheck/reset/add/remove/edit
                               a thing's metadata.checklist items (#182)
  POST /umwelt/act            run a kind-declared action on a typed thing
  POST /umwelt/trash_thing    author-only: live -> binned (recoverable)
  POST /umwelt/restore_thing  author-only: binned|landfilled -> live
  POST /umwelt/bin            your own binned things (author-scoped read)
  POST /umwelt/bin_notice     bin_maintainers only: notice that another
                               villager's thing will be binned in 48h (D3)
  POST /umwelt/answer_bin_notice  author-only: answer a notice (blocks the bin)
  POST /umwelt/maintainer_bin bin_maintainers only: bin after a notice stood
                               48h unanswered (bin only, never landfill)
  GET  /umwelt/landfill       everyone's landfilled things (public dump)
  POST /umwelt/take           pick up a thing (place -> your hands, budget-gated)
  POST /umwelt/drop           put down a carried thing (your hands -> where you stand)
  POST /umwelt/inventory      what you're carrying + load/capacity/headroom
  POST /umwelt/put            bag a thing into a container (capacity-gated)
  POST /umwelt/take_out       pull a thing out of a container into your hands
  POST /umwelt/look_in        render a container's contents on demand
  POST /umwelt/crossings_here read crossings since cursor (comms layer)
  POST /umwelt/crossing_lookup read a single crossing by id, no access
                               control beyond a valid id (feeds the
                               webrender gateway's voice-playback route)
  POST /umwelt/search_crossings headlines-only text search over crossings,
                               private-place gated (comms QoL, umwelt#863)
  POST /umwelt/open_crossing  read ONE crossing's body by seq, paged above
                               S_guard; visibility.may_view_crossing gated,
                               uniform absent/unauthorized (umwelt#862 D3)
  POST /umwelt/thread         headlines of the thread containing a seq,
                               may_view_crossing filtered (umwelt#862 D3)
  POST /umwelt/send_crossing  author a native crossing (comms layer)
  POST /umwelt/trace          commit standpoint cursor (comms layer)
  POST /umwelt/heartbeat      refresh the caller's avatar-status heartbeat
                               (Phase 2, forum #268) — feeds look_at's
                               working/idle/away/asleep read-time lease
  POST /umwelt/wake_block     target blocks a sender from waking them (Gong Spec)
  POST /umwelt/wake_unblock   target removes a sender from their wake-block set
  GET  /umwelt/wake_blocks    an agent's own wake-block set
  GET  /umwelt/places         flat legacy list (no agent) or contextual
                               place_tree render (?agent=) — umwelt #158
  POST /umwelt/set_parent     set (or clear) a place's parent (umwelt #158)
  POST /umwelt/add_link       add a lateral place-to-place link (umwelt #158)
  POST /umwelt/remove_link    remove a lateral place-to-place link (umwelt #158)
  GET/POST /umwelt/map        filterable full-forest map — a forest-union of
                               place_tree over every visible top-level place
                               (umwelt #160)
  POST /umwelt/update_place   owner-only (or any agent, for an owner-less
                               engine-genesis place): patch a place's
                               metadata (place-lane-tiles spec)
  GET  /health

Health surfaces two independent version signals (umwelt #77): `commit`
(#61, the checked-out git SHA — authoritative, engine-resolved, never
operator-editable) and `world_version` (#77 slice 3, an OPTIONAL operator-
maintained manifest — human-readable version + live feature list — read
from a JSON file beside the db). Neither substitutes for the other: commit
answers "what code is deployed"; world_version answers "what did the
operator declare live." See docs/DEPLOY.md.

Removal lifecycle (umwelt #28): agents trash/restore; only the world's GC
(gc.py, run on a schedule) advances binned->landfilled->dissolved. dissolve
is never reachable here — no route, no MCP tool, ever.

Carrying (umwelt #88 slice 1): the holder axis — a thing is held by a place
(at rest), an avatar (carried), or (slice 2) a container (bagged).
weight_est prices a thing's rendered exposure (name + description) as a
token estimate; carry_capacity is a per-avatar token budget (provisional,
pending live calibration). Engine only through slice 2 — no MUD client
verbs yet (slice 3).

Containers (umwelt #88 slice 2): bags are context-compressors — a
container's own cost anywhere (inventory, place view) is ITS OWN weight_est
only; its contents are exposed ONLY by explicit look_in (the closed-bag
exposure rule). Bag-in-bag is disallowed in v1. Container capacity
(metadata.capacity_est, default 4000) is a separate token budget from
carry_capacity — the bag's, not the avatar's.

Typed things (umwelt #107 slice A): `kinddef` is a declarative kind, same
shape as `blueprint` — its `source` holds a `kinddef.json` describing a
field schema + a closed-set-authorized action table. Any thing may carry
`metadata.kind_id` to become an instance of a kinddef (validated at
create_thing time). `act()` is the one new verb: it walks the instance's
kind chain child-first, authorizes the caller against the resolved action's
declared `callers`, checks a `from`-status gate, transitions
`metadata.status`, and appends a server-stamped entry to `metadata.action_log`
(server-owned, same posture as consequence_grade — create_thing/update_thing
both reject a caller-supplied action_log). No sandbox, no arbitrary code —
actions are declarative rows, not executed logic; slice A has no effects
menu (move/crossing side effects are slice B). See PROTOCOL.md's "Typed
things" section.

Size classes + examine gating (umwelt #88 slice 4): every thing exposes a
`size_class` (scroll/book/tome/library, see db.compute_size_class) derived
from its FULL `source` content — the "this is large, be mindful" expectation
BEFORE any read. `use()`'s readable branch is the render site for full
content; scroll/book still render `content` in full, but tome/library
render metadata + a manifest instead and NEVER put source text on the wire.

Carry is pure total-load (umwelt #151, follows #147): carry capacity is a
per-agent config (avatar.metadata.carry_capacity, falling back to env
UMWELT_CARRY_CAPACITY then CARRY_CAPACITY_DEFAULT — see _carry_capacity), and
`take()`'s existing new_load > capacity check is the only carry gate. There
is no per-thing "furniture" class — a thing too big for the acting avatar's
budget is already un-takeable under the total check (taking it into an empty
inventory puts load > capacity).

Port convention: 5001 viz · 5002 forum · 5003 umwelt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import itertools
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections import deque
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime as _bin_datetime
from datetime import timedelta as _bin_timedelta
from datetime import timezone as _bin_timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from flask import Flask, Response, g, jsonify, request

from . import cas
from . import db
from . import sandbox
from . import tier_config
from . import visibility
from ._ids import PLACE_ID_MAX_LEN
from ._ids import canonical_agent as _canonical_agent_fold
from .ghref import normalize_gh_ref
from .ingress import BodyRow, bound_write_bodies
from .tls_context import check_tls_args, resolve_tls_context, serve_dual_listener

# Fields the world assigns — a caller supplying them in a spec is rejected.
_SERVER_OWNED = ("id", "author", "created_at")
# umwelt #79 slice-1: 'blueprint' is a declarative kind — its `source` holds
# a blueprint.json spec-template; use()'s blueprint branch has the ENGINE
# (not the sandbox) stamp a new thing by substituting caller args into it.
# umwelt #107 slice A: 'kinddef' is another declarative kind — its `source`
# holds a kinddef.json describing a field schema + a declarative action
# table (see _validate_kinddef_source / act()). A kinddef is itself just a
# thing, same as a blueprint; the only ENGINE-enforced difference is what
# its `source` is required to contain.
_VALID_KIND = ("executable", "readable", "blueprint", "container", "kinddef")
# umwelt #278: the three street-view render bands a thing/avatar can be
# placed in (sky top, land middle, road bottom — see webrender/gateway.py's
# _build_scene). Forge-time only — a caller may request one at create_thing
# time, same as kind; there's no separate authorization mechanism (an
# agent-writable placement choice, not a permission).
_VALID_LANE = ("sky", "land", "road")

# Matches a {{param_name}} hole in a blueprint.json spec-template. \w+ (not
# a stricter identifier pattern) on purpose — matches whatever param-naming
# convention a blueprint author picks; the value map (not this regex) is
# what determines whether a hole is a REAL declared param.
_BLUEPRINT_HOLE_RE = re.compile(r"\{\{(\w+)\}\}")
# create_place (#99): validates a caller-supplied explicit place id. Derived
# ids (from label) are built separately via _PLACE_SLUG_RE below and always
# satisfy this same shape.
_PLACE_ID_RE = re.compile(r"^place_[a-z0-9_]+$")
# Collapses any run of non-alphanumeric chars in a label into a single "_"
# for id derivation; leading/trailing "_" are stripped by the caller.
_PLACE_SLUG_RE = re.compile(r"[^a-z0-9]+")
# GET /umwelt/cas/<sha256> (umwelt #187): the exact shape a CAS ref's hex
# hash must satisfy — mirrors cas._HEX64. Checked BEFORE any FS/DB touch so a
# malformed hash never reaches cas.retrieve at all; see the route below for
# why (uniform 404, no timing oracle).
_CAS_HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
# Fields an author may change via update_thing (source rewrites the on-disk tree;
# the rest are DB columns). place_id/kind/id/author/created_at are NOT updatable.
_MUTABLE_THING_KEYS = ("name", "description", "exec_command", "metadata", "source")
# Fields an agent may change via update_place (umwelt place-lane-tiles spec).
# metadata is the ONLY mutable field — no path to rename/re-describe/
# re-parent a place through this route (those go through create_place/
# set_parent's own narrowly-scoped routes instead). Same "own local
# whitelist" convention as _MUTABLE_THING_KEYS above; db.py's
# _MUTABLE_PLACE_COLS is that module's internal SQL-column whitelist.
_MUTABLE_PLACE_KEYS = ("metadata",)

# Project tracking (umwelt #161 slice S1). Server-owned fields a create
# request must not supply — same structural-honesty gate as
# _SERVER_OWNED/create_thing's rejection above.
_PROJECT_SERVER_OWNED = ("id", "status", "created_by", "place_id",
                         "created_at", "updated_at")
_TASK_SERVER_OWNED = ("id", "project_id", "status", "turn_holder",
                      "created_by", "created_at", "updated_at")
_PROJECT_STATUS = ("active", "paused", "done", "archived")
# 'blocked' is deliberately NOT in this tuple — it is computed, never a
# settable status (see db.is_task_blocked's docstring).
_TASK_STATUS = ("open", "in_progress", "review", "done", "cancelled")
# Fields a caller may change via .../update — same "own local whitelist,
# don't reach into the other module's private column list" convention as
# _MUTABLE_THING_KEYS above (db.py's _MUTABLE_PROJECT_COLS/_MUTABLE_TASK_COLS
# are that module's internal SQL-column whitelist, not a public contract).
_MUTABLE_PROJECT_KEYS = ("label", "description", "status")
_MUTABLE_TASK_KEYS = ("label", "description", "status", "owner",
                     "turn_holder", "gh_repo", "gh_ref")

# umwelt #221 S3a: the singleton `project_board` kinddef thing's canonical
# id. A fixed, engine-chosen constant (not a uuid4) — same "canonical
# constant id" posture as WORKSHOP_PLACE_ID/place_square/place_library —
# so every project's board instance can reference ONE well-known kind_id,
# forged idempotently on first use (see _ensure_project_board_kinddef)
# rather than requiring a separate boot-time seed step every deployment/
# test fixture would otherwise have to remember to call.
_PROJECT_BOARD_KIND_ID = "kind_project_board"

# umwelt #227 S4: the singleton `project_registrar` kind + its ONE forged
# instance in the Workshop — "found a new project from in-world" rather
# than a raw POST /umwelt/projects. Unlike project_board (one KIND, many
# per-project INSTANCES), the registrar has exactly one instance in the
# whole world — there's only ever one place to go found a project, the
# same singleton posture WORKSHOP_PLACE_ID itself uses. Both ids are fixed
# engine-chosen constants (not uuid4s), forged idempotently at app-boot
# time (see _ensure_project_registrar, called from create_app) — as of
# umwelt #247, _ensure_project_board_kinddef's kind-forge is ALSO called
# at boot (alongside its pre-existing lazy-on-first-create_project path),
# so both kinds now self-heal on a pure code-swap promote, not just the
# registrar. The registrar's own `found_project` action reaches _apply_effect_project_
# create, which needs _PROJECT_BOARD_KIND_ID to already exist (see that
# effect's docstring for why boot-time-ensure, not a lazy re-derive, is the
# right shape here).
_PROJECT_REGISTRAR_KIND_ID = "kind_project_registrar"
_PROJECT_REGISTRAR_THING_ID = "thing_project_registrar"

# umwelt #400: the `charge_board` kind's canonical id — same "fixed,
# engine-chosen constant" posture as _PROJECT_BOARD_KIND_ID, forged
# idempotently at boot (see _ensure_charge_board_kinddef, called from
# create_app alongside the project-board/registrar ensures). Unlike
# _PROJECT_REGISTRAR_KIND_ID/_PROJECT_REGISTRAR_THING_ID (a KIND plus its
# one engine-authored singleton INSTANCE), charge_board is KIND-ONLY here
# — deliberately NOT paired with an engine-authored instance id the way
# the registrar is. An "author=engine" instance would be unreachable
# through the `author` caller class (no real agent can authenticate as
# the literal string "engine" — same defense-in-depth note
# _ensure_engine_kinddef's docstring makes), and #400's `granted` caller
# class bootstraps FROM the author (grant_caller's who-may-grant gate is
# "author, or an existing can_grant=1 row" — see
# _apply_effect_grant_caller): an engine-authored board could never mint
# its first grant, permanently locking every non-author agent out. A real
# charge board (e.g. the village's) is created the ordinary way — a real
# agent (its actual owner/maintainer) calls create_thing with
# metadata.kind_id = _CHARGE_BOARD_KIND_ID — same "many instances, one
# kind" posture _PROJECT_BOARD_KIND_ID itself uses (each project's board
# instance is minted separately, not a singleton).
_CHARGE_BOARD_KIND_ID = "kind_charge_board"

# use()'s `args` limits (umwelt #57) — loud 422 when exceeded, never silently
# truncated or dropped. Conservative on purpose: this widens the exec surface
# (args reach argv), so the caps are named explicitly in every rejecting error.
_USE_ARGS_MAX_ITEMS = 16
_USE_ARGS_MAX_ITEM_LEN = 4096
_USE_ARGS_MAX_TOTAL_LEN = 16384

# umwelt#495 slice 2's bind/reset socket -- deliberately NOT under
# <db-dir>, which is a single agent's own home directory (see
# bind_socket.py's module docstring, "Socket location", for the PR #506
# review finding this replaces). Must be provisioned ahead of time by an
# operator with root (see --bind-socket-path's help text below) --
# nothing in this codebase creates this top-level directory itself.
_DEFAULT_BIND_SOCKET_PATH = "/usr/local/lib/umwelt/run/identity-bind.sock"

# umwelt#531/#534 (Borges's review): imported from bind_socket.py rather
# than duplicated here, so this help text and that module's own
# provisioning-gap RuntimeError always quote the identical command -- a
# human reading a boot-time crash and a human reading --help must never
# see two different strings for the same fix.
from .bind_socket import _BIND_SOCKET_INSTALL_HINT  # noqa: E402

# send_crossing's optional `emotion` tag (umwelt #133, Lei's design: "one
# small enum-or-freeform string, v0: freeform, length-capped"). Same
# loud-422-not-silent-truncation posture as the args caps above. Default
# applied when the field is absent or an empty string — the default itself
# is db.CROSSING_EMOTION_DEFAULT (single source of truth; db.py owns it
# since server.py already imports db, not the reverse).
_CROSSING_EMOTION_MAX_LEN = 40
_CROSSING_EMOTION_DEFAULT = db.CROSSING_EMOTION_DEFAULT

# umwelt #147: two guards against a description-flood — a giant self-enumerating
# thing description dominating a place-view and flooding a connecting client's
# context window (a cs_0047 attention-management violation; hit independently by
# 3 agents in the 2026-07-12 MCP dogfood). The two layers are complementary:
#  (1) creation HARD CAP — a disastrously-large description is refused at forge
#      / update time (loud 422, same posture as the args/emotion caps above), so
#      a bomb can never enter the world at all. Calibrated from real data: the
#      longest legitimate description across prod+alpha is 736 chars, the actual
#      "infinite filing cabinet" flood bomb was 12369, so 8192 clears any real
#      content by >11x and denies the bomb. It is a "disastrously large" line,
#      NOT the "keep it short" knob (that is the render cap below).
#      Coupling note (umwelt #88): a thing's weight_est is name+description/4, so
#      the "not carriable by hand" furniture class (weight > CARRY_CAPACITY_
#      DEFAULT=2000 => description > ~8000 chars) sits JUST under this cap. The
#      cap must stay above that furniture threshold until weight_est is reworked
#      to track CONTENT (source) rather than the description blurb — the clean
#      long-term fix, tracked as a follow-up, not forced into #147.
_DESCRIPTION_MAX_LEN = 8192

# umwelt #253: a thing's `name` / a place's `label` is the glanceable
# render surface (street blocks, look_in listings, board rows) — long
# content belongs in `description`, not here. Same loud-422-not-silent-
# truncation posture as the caps above, at forge AND update time (same
# parity rationale as _DESCRIPTION_MAX_LEN). Calibrated from real data
# (a chameleon.local:5003 survey, 46 live names: median 19, p90 43, max
# 50) — Kepler's ruling (forum #267): the cap's job is to PUSH naming
# discipline, including on the engine's own auto-generated names, not
# accommodate the worst offender by raising the number. The 5 real names
# that exceeded 40 were project auto-labels carrying issue-reference
# metadata that belongs in description, not the name (e.g. "In-turn
# recall improvements (#257 recs 1-3)") — the sweep (a follow-up ops
# script, umwelt#246 pattern) renames those, it doesn't loosen the cap.
_NAME_MAX_LEN = 40
#  (2) render CAP — the place-view / look_in LIST render truncates each thing's
#      description to a bounded prefix + '…'; the FULL text is always available
#      via look_at (uncapped). Defense-in-depth over (1), AND per-agent attention
#      management: a context-tight client may pass a smaller `desc_cap` on a
#      render call (e.g. sourced from ~/.umwelt/config.json render_desc_cap) to
#      protect its own window. Absent -> this default.
_DESCRIPTION_RENDER_CAP_DEFAULT = 500


def _name_char_problem(value: str) -> str | None:
    """Return None if `value` is an acceptable name/label, else a short reason.

    umwelt #874: every thing name and place/link/project/task label renders
    as ONE line (street blocks, look_in, map, board rows, headlines) — D10
    (#872) had to defend one renderer by hand against a name's newline.
    Refuse control characters at the door instead: category Cc (C0/DEL/C1,
    incl. tab/newline/CR/NEL), Zl (U+2028) and Zp (U+2029). Cf (e.g. ZWJ,
    which emoji sequences need) stays allowed. The reason names the FIRST
    offender by code point and never echoes the raw character.
    """
    for ch in value:
        if unicodedata.category(ch) in ("Cc", "Zl", "Zp"):
            return (f"contains a control character (U+{ord(ch):04X}) — "
                    "a name/label must be a single line of plain text")
    return None


def _name_label_type_error(value, field: str) -> str | None:
    """umwelt #880: a request-derived `name` / `label` must be a `str`.

    Returns None when it is, else a bounded one-line reason naming the field
    and the JSON type of what arrived (never the raw value). Callers run this
    BEFORE any `len()` / `_name_char_problem` / `str()` coercion: a list or
    dict either slips past `len()` or reaches sqlite raw (a 500), and an int
    used to be silently stored as '5'.
    """
    if isinstance(value, str):
        return None
    return f"{field} must be a string (got {_json_type_name(value)})"


class _FieldTypeRefusal(Exception):
    """umwelt #906: a request-derived identifier / enum field was present and
    not a JSON string. Raised by `_str_identity` / `_require_str` from inside a
    route body and turned into the house 422 by the errorhandler registered for
    THIS type only (in create_app) -- so a request that never reaches a route
    (unknown path -> 404, wrong method -> 405) keeps Flask's own response.
    Deliberately not a KeyError/ValueError/TypeError subclass: no existing
    `except` in a route may swallow it."""


def _require_str(value, field: str) -> None:
    """Raise `_FieldTypeRefusal` unless `value` is a `str` or `None` (`null`
    keeps each site's existing meaning; callers that must refuse `null` too
    check for it themselves). Never echoes the value."""
    if value is None or isinstance(value, str):
        return
    raise _FieldTypeRefusal(_name_label_type_error(value, field))


def _str_identity(source, key: str, default: str = "") -> str:
    """umwelt #906: the shared read for identity / enum fields --
    `str(source.get(key, default)).strip()` EXCEPT that a present non-string
    (list, dict, int, float, bool) raises `_FieldTypeRefusal` (422) BEFORE the
    coercion. `null` still reads as the text "None" (an existing wart, pinned
    by test, not endorsed); absent reads as `default`."""
    value = source.get(key, default)
    _require_str(value, key)
    return str(value).strip()


# umwelt #88 slice 1: an avatar's carry budget, in weight_est tokens.
# PROVISIONAL — pending Lei's live-in-it calibration protocol (issue #88):
# the number is not derived from any measurement yet, just a starting point.
# No config surface exists in this engine today (verified: no os.environ
# reads elsewhere in server.py), so this is the module-level default +
# env-override the spec allows as acceptable v1.
CARRY_CAPACITY_DEFAULT = 2000


def _carry_capacity(avatar: dict) -> int:
    """The active carry_capacity for `avatar` — checked in order: (1)
    avatar.metadata.carry_capacity if a valid positive int (bool excluded —
    same subclass gotcha #148's _desc_cap_from_body guards against); else
    (2) env override UMWELT_CARRY_CAPACITY if set and a valid int; else (3)
    CARRY_CAPACITY_DEFAULT. umwelt #151: carry capacity is per-agent config
    (parallel to #147's agent-configurable render cap) — a malformed/absent
    per-agent value falls through to the next tier rather than erroring;
    this is a read-path fallback, not a validation gate (the gate is at
    customize_avatar, which rejects an invalid carry_capacity loudly before
    it's ever stored). Read per-call (not cached at app-creation time) so
    tests can flip the env var / avatar metadata between requests without
    rebuilding the Flask app."""
    meta_raw = (avatar.get("metadata") or {}).get("carry_capacity")
    if (meta_raw is not None and not isinstance(meta_raw, bool)
            and isinstance(meta_raw, int) and meta_raw > 0):
        return meta_raw
    raw = os.environ.get("UMWELT_CARRY_CAPACITY")
    if raw is None:
        return CARRY_CAPACITY_DEFAULT
    try:
        return int(raw)
    except ValueError:
        return CARRY_CAPACITY_DEFAULT


# umwelt #88 slice 2: a container's own capacity, in weight_est tokens — a
# SEPARATE budget from carry_capacity above (the bag's, not the avatar's).
# PROVISIONAL, same calibration posture as CARRY_CAPACITY_DEFAULT (issue
# #88). Set per-container at creation via metadata.capacity_est; this is
# only the fallback when that key is absent or unparseable.
CONTAINER_CAPACITY_DEFAULT = 4000


def _container_capacity(container: dict) -> int:
    """The active capacity for `container` — its own metadata.capacity_est
    if present and a valid int, else CONTAINER_CAPACITY_DEFAULT. Same
    defensive-fallback idiom as _carry_capacity's env-var parse: a
    malformed/missing value falls back rather than 500ing."""
    raw = (container.get("metadata") or {}).get("capacity_est")
    if raw is None:
        return CONTAINER_CAPACITY_DEFAULT
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return CONTAINER_CAPACITY_DEFAULT


# umwelt #88 slice 4 / #122: the fixed pointer line rendered instead of a
# tome/library's source — verbatim per spec, so tests can assert on it
# exactly rather than a paraphrase. #122 turned the gate from a dead-end
# into a refuse-and-REDIRECT: it now names `download` as the sanctioned
# egress path instead of just refusing inline render.
_TOME_POINTER_TEMPLATE = (
    "'{name}' is a {size_class} — too large to open inline. Download it to "
    "your filesystem with `download {name} <path>` and read it there."
)
# Classes whose full source is NEVER put on the wire (examine gating) —
# metadata + a manifest render instead. scroll/book keep rendering full
# content, same as before slice 4.
_GATED_SIZE_CLASSES = ("tome", "library")
# umwelt #486: the two server-owned metadata.graft_ref/graft_version keys
# (see the create_thing/update_thing rejection comments elsewhere in this
# file — callers can never SET these) are also the ones look_at must not
# READ out to a non-author: they're a pointer into content-addressed
# storage (the advance_ref capsule chain, umwelt #186), not ordinary
# author-set display metadata. Kept as its own named set (not inlined at
# the call site) so a future engine-owned, pointer-shaped metadata key
# gets one place to register both postures (unwritable AND unreadable-by-
# others) rather than two independent call sites drifting apart.
_LOOK_AT_NON_AUTHOR_METADATA_DENYLIST = frozenset({"graft_ref", "graft_version"})

# umwelt #400 / #494: the full roster of `metadata` keys a caller may NEVER
# supply directly through create_thing or update_thing — regardless of
# author status — because each is written ONLY through its own declared or
# internal mechanism: `action_log` by act()'s own logging step,
# `graft_ref`/`graft_version` by advance_ref's compare-and-swap, `entries`
# by board_entry_add/board_entry_retire (umwelt #400's charge_board). This
# is a WRITE-guard roster, not a read-visibility one — do NOT fold `entries`
# into `_LOOK_AT_NON_AUTHOR_METADATA_DENYLIST` above: a charge board's whole
# point is that non-authors (and `granted` delegates who aren't the author)
# can still SEE the entries; they just can't set them by bypassing the
# board_entry_* effects. `action_log` is included here even though it is
# NOT on the read denylist (it's ordinary visible history) — this
# constant's contract is narrower and different: "never caller-writable",
# a bar action_log already met by convention before this constant existed.
# Before #400 these were THREE independent literal `if` checks (one for
# action_log, one for graft_ref/graft_version) duplicated at both the
# create_thing spec-validator and the update_thing route — #494 flagged
# that shape as fail-open: a newly-added server-owned key can silently
# leak through both write paths unless every literal check remembers to
# grow. Route both write guards through this one set instead so a future
# server-owned key needs to be added in exactly one place.
_SERVER_OWNED_METADATA_KEYS = frozenset({
    "action_log", "graft_ref", "graft_version", "entries",
    # umwelt #879: the project-board effects scope their writes to this
    # binding, so only db.create_project_with_board may assign it.
    "project_id",
})


def _server_owned_metadata_message(hit: frozenset[str],
                                    where: str = "the request") -> str:
    """Build the "metadata.X (is|are) server-owned" rejection message,
    naming exactly the offending `_SERVER_OWNED_METADATA_KEYS` member(s)
    (umwelt #400/#494) rather than a static "graft_ref/graft_version"
    string that would misname the actual trigger when, say, `entries` is
    what tripped the guard. `where` lets each call site keep its own
    existing phrasing (create_thing's spec-validator says "the request",
    update_thing's route says "the request body").
    """
    keys = "/".join(sorted(hit))
    verb = "is" if len(hit) == 1 else "are"
    return (
        f"metadata.{keys} {verb} server-assigned (written only via its "
        f"own declared action/capability) and must not be in {where}")


def _size_class_and_tokens_for(things_dir: Path, thing: dict,
                                source: dict[str, str] | None = None) -> tuple[str, int]:
    """A thing's (size_class, token_estimate) pair. When the caller already
    has the source in hand (use()'s readable branch reads it anyway for the
    content render), classify from it; otherwise classify from a
    stat()-summed byte count — same chars/4-grade estimate WITHOUT
    reading/decoding a possibly library-sized tree per place-view render
    (PR #95 review). See db.compute_size_class for why this isn't a stored
    column.

    umwelt #102: the token estimate is exposed alongside the class (not just
    the class alone, as before) so look_at can surface
    content_length_estimate without a second read — `_size_class_for` below
    is a thin single-value wrapper kept for callers that only want the
    class, so it never needs to change shape."""
    if source is not None:
        tokens = db.compute_content_token_estimate(source, thing["description"])
        return db.size_class_from_token_estimate(tokens), tokens
    base = things_dir / thing["id"] / "source"
    if base.exists():
        total = sum(p.stat().st_size for p in base.rglob("*") if p.is_file())
    else:
        total = 0
    if total == 0:
        total = len(thing["description"] or "")
    import math as _math
    tokens = _math.ceil(total / 4)
    return db.size_class_from_token_estimate(tokens), tokens


def _size_class_for(things_dir: Path, thing: dict, source: dict[str, str] | None = None) -> str:
    """A thing's size class alone — see `_size_class_and_tokens_for` for the
    (size_class, token_estimate) pair this delegates to."""
    return _size_class_and_tokens_for(things_dir, thing, source)[0]


def _size_fields(things_dir: Path, thing: dict, source: dict[str, str] | None = None) -> dict:
    """The size_class field every thing JSON payload gains in umwelt #88
    slice 4 (look_at / places / inventory / look_in), plus
    content_length_estimate (umwelt #102) — the same cheap stat()-summed
    token estimate that already drives size_class, now also exposed so
    look_at's gate notice can name the consequence's scale without a second
    read. umwelt #151: the per-thing "not carriable by hand" flag that used
    to live here is gone — carry is pure total-load, checked at take() time
    against the acting avatar's own budget, not a per-thing flag."""
    size_class, tokens = _size_class_and_tokens_for(things_dir, thing, source)
    return {
        "size_class": size_class,
        "content_length_estimate": tokens,
    }


def _reachable(thing: dict, avatar: dict) -> bool:
    """True if `avatar` can reach `thing` right now — umwelt #88 slice 2's
    put/take_out/look_in reach rule: "must hold it or be in its place".
    Held-by-you (holder_kind='avatar', holder_id=you) or lying in your
    current place (holder_kind='place', same place_id) both count. A thing
    currently bagged in a container (holder_kind='container') is NOT
    reachable this way — you reach a bag's contents via look_in/take_out on
    the BAG itself, never by comparing to the contained thing's own (stale)
    place_id.
    """
    if thing["holder_kind"] == "avatar":
        return thing["holder_id"] == avatar["id"]
    if thing["holder_kind"] == "place":
        return thing["place_id"] == avatar["place_id"]
    return False


def _canonical_agent(raw: str) -> str:
    """Canonical agent identity is case-insensitive: fold for identity,
    ownership, and addressing. The typed form is kept only as display metadata.

    umwelt #73: `Lei` and `lei` used to mint separate identities (separate
    home posts, separate avatars) — this is the single fold point every
    caller-supplied agent identity flows through before it's used as a
    lookup/storage key. Never used for display text (avatar name, place
    label) — those keep the caller's typed capitalization, sourced
    separately at each call site.

    Delegates to `_ids.canonical_agent` — the ONE fold implementation shared
    with migrate_casefold.py and mud.py. Do not reimplement the fold here;
    see `_ids.py`'s module docstring for why a forked fold is dangerous.
    """
    return _canonical_agent_fold(raw)


@dataclass(frozen=True)
class ActorResolution:
    """umwelt#495 slice 3 — resolve_actor's return type: the outcome of
    resolving "who is making this request" from an `Authorization: Bearer
    <token>` header, with a caller-declared fallback while the backfill
    window is open (see resolve_actor's own docstring for the full
    three-branch logic and the anti-forgery property it guarantees).

    `source` is one of:
      - "token"    — a valid `agent_sessions` row backed this resolution.
                     The token's own `agent_id` is what's returned here,
                     ALWAYS — never `declared`, no matter what the caller
                     passed for it (the anti-forgery property).
      - "declared" — no valid token was presented; the backfill window is
                     open, so the caller-declared `declared` param is
                     trusted (no verification behind it) — the SAME trust
                     posture as today's existing routes everywhere else in
                     this codebase. It is folded through `_canonical_agent`
                     first, same as every other route already folds its own
                     `agent`/`actor` param — so this is trust-identical to
                     today's posture, not literally byte-identical (Zephyr's
                     cross-vendor read, forum #315: an unfolded `declared`
                     would return "Zephyr" pre-cutover and "zephyr"
                     post-cutover for the same identity, splitting it across
                     the wiring slice).
      - "none"     — no valid token AND the backfill window is closed.
                     Currently unreachable in practice (db.backfill_
                     window_open always returns True this slice) — exists
                     for forward-compat once a later slice's gate-close
                     event exists.
      - "invalid_token" — umwelt#618: an `Authorization: Bearer <token>`
                     header WAS presented, but `db.get_agent_session`
                     returned None for it (unknown, expired, retired-
                     identity, credential-reset, or window-closed-
                     unverified — see that function's own docstring for
                     the six reasons it can refuse). Deliberately DISTINCT
                     from "declared" (no header at all): before #618, this
                     case fell through to the SAME declared-fallback
                     branch as "no token", so a request carrying a
                     forged/expired/unknown token silently downgraded to
                     the unverified caller-declared identity instead of
                     being refused — the fail-open path #618 closes. A
                     token, once presented, must resolve or the request is
                     refused; it must never silently downgrade to
                     "as if no token had been sent". `agent_id` is always
                     None here (same as "none"), which is what makes every
                     converted call site's existing
                     `if resolution.agent_id is None: return 401` check
                     catch this uniformly, with no call-site changes
                     needed — see resolve_actor's own docstring for why
                     this shape was chosen over a per-route branch.

    `verified` and `source` are deliberately NOT collapsed into one
    boolean. A token minted during the backfill window (state=
    'unverified') still resolves with source="token" (the token WAS
    valid, and was used) but verified=False (it backs no real signature)
    — conflating "a token was used" with "the identity behind it was
    cryptographically proven" is exactly the silent-fail-open failure
    class the design-of-record names as the single worst failure mode
    this whole mechanism (umwelt#495) exists to avoid. A caller that only
    branches on `verified` gets the security-relevant bit; a caller that
    also wants to know HOW identity was established reads `source` too.
    """
    agent_id: str | None
    verified: bool
    source: str  # "token" | "declared" | "none" | "invalid_token"
    window_open: bool


def resolve_actor(conn, request, declared: str | None = None) -> ActorResolution:
    """Resolve "who is making this request" — umwelt#495 slice 3, Kepler's
    PR #559 §3.1 item 2. Takes `conn` and a `request`-shaped object
    (anything exposing `.headers.get(...)`, e.g. Flask's `request` proxy or
    a plain stand-in) EXPLICITLY as parameters, rather than reaching for
    Flask's `request`/`g` globals internally — so this function is
    directly unit-testable with a bare object standing in for `request`,
    with no Flask app/request-context needed (per Kepler's ruling: "One
    helper, one place the three-state rule lives" — testable in isolation
    is part of what makes that true).

    **WIRED into the read routes as of this slice** (umwelt#495 slice 1a,
    "surface conversion"): `look`, `look_at`, `look_in`, `inventory`,
    `trace`, `crossings_here`, and `crossings_for_agent` all resolve their
    caller identity through this helper now.

    `crossings_for_agent` converts only its `viewer` param — the caller's
    identity. Its `agent` param is the QUERY SUBJECT (whose crossings are
    being asked about), not the caller, and is deliberately left on the old
    caller-declared fold: converting `agent` too would silently rewrite
    "show me crossings addressed to X" into "show me crossings addressed to
    me" (see that route's own comment for the full rationale).

    `/umwelt/events` is deliberately NOT converted: its `actor` param is a
    public-log search filter, not a caller identity, and resolving it as the
    authenticated actor would change "events by X" into "events by me".

    The write/mutate routes that read a caller-declared identity were
    converted in slice 1b (umwelt#621): `create_place`, `set_parent`,
    `add_link`, `remove_link`, `customize_avatar`, `move_to`, `use`,
    `create_thing`, `update_thing`, `update_place`, `checklist`, `act`,
    `trash_thing`, `restore_thing`, `retire_identity` (caller `actor`
    only — the retired `identity` is a subject, not a caller), `take`,
    `drop`, `put`, `take_out`, `send_home`, `send_crossing`, `heartbeat`,
    `record`, `projects`, `projects/<id>/tasks`, `projects/<id>/update`,
    `tasks/<id>/deps`, `tasks/<id>/update`, and `wake_block`/`wake_unblock`
    (caller `agent`/target only — the `blocked` sender is a subject, not a
    caller) all resolve
    through this helper now. On those routes a presented-but-unresolvable
    token returns 401 (fail-closed), and a valid token wins over the
    declared `agent` (anti-forgery) — only ABSENCE of `Authorization` takes
    the declared-identity compat branch (and only while the backfill window
    is open).

    REMAINDER explicitly named (umwelt#621's acceptance baseline demands the
    remainder be named, not silently skipped — "do not let this issue be
    closed by a partial conversion"):

    - Gateway forwarding branches (#621b): the webrender `/act` branches pass
      no `token=` to `_world_request` for `move`/`talk`/`say`/`use`/`take`/
      `drop`/`act`/`edit`. A converted engine route the gateway never sends a
      token to is not converted in practice. This slice converts the engine
      surfaces; the gateway forwarding is a separate follow-up still open
      under #621.
    - `register`/`enter` are session-establishment endpoints (identity
      creation + reconnect bootstrap), not authenticated actions within an
      established session — the credential is being established, not
      consumed. Whether resolve_actor applies at the bootstrap is a
      design-substantial call, tracked as its own issue rather than
      converted here: umwelt#689.
    - `crossings_for_agent`'s `agent` param is the query SUBJECT (whose
      crossings), not a caller; its `viewer` caller was converted in slice
      1a (#631). Resolving `agent` would rewrite "crossings for X" into
      "crossings for me" (the caller-vs-subject trap, umwelt#602).

    The confidentiality-class read routes (`/umwelt/wake_blocks`,
    `/umwelt/unseen`, `/umwelt/my_court`, `/umwelt/bin`,
    `/umwelt/thing_actions`, `/umwelt/place_view`, and the contextual
    `?agent=` branch of `/umwelt/places` and `/umwelt/map`) were converted
    in slice 1c (umwelt#621, part of). `place_view` alone needed a
    genuinely different OPTIONAL-agent call shape — see its own docstring
    — since an absent agent there is a legitimate anonymous read, not an
    auth failure; every other route in this list uses the ordinary
    REQUIRED-agent idiom this function's own callers already establish.
    `/umwelt/places`' no-agent branch is deliberately untouched, byte-
    identical to before — see that route's own docstring for why.

    Logic (now a four-outcome rule — token / invalid_token / declared /
    none — umwelt#618 split the original three-state rule's single
    "otherwise" branch in two; still the ONE place it's meant to live):

    1. Read a bearer token from the `Authorization` header, expecting the
       form `Bearer <token>` (a NEW convention for this codebase — no
       prior precedent to match here; this is the standard HTTP
       bearer-token shape).
    2. If a token is present AND `db.get_agent_session(conn, token)`
       returns a valid (unexpired) row: return an ActorResolution built
       from THAT row's `agent_id`/`state` — `declared` is ignored
       ENTIRELY whenever a valid token is present. This is the core
       anti-forgery property: a valid token minted for agent A must never
       let a request masquerade as agent B via a `declared` param, no
       matter what the caller passes for it.
    3. Otherwise, if a token IS present but does not resolve to a live
       session (umwelt#618: unknown, expired, retired identity,
       credential-reset, or a window-closed unverified session — see
       `db.get_agent_session`'s own docstring): return `agent_id=None,
       source="invalid_token"`, regardless of `db.backfill_window_open`.
       A presented token MUST resolve or the request is refused — it
       never silently downgrades to state 4 below. This is the anti-
       fail-open property #618 adds: before it, this branch fell through
       to state 4, so a forged/stale/unknown token was trusted as if it
       had never been sent, exactly as much as a genuinely credential-
       less caller. See ActorResolution's own docstring for why
       `agent_id=None` (rather than a new field) is the chosen shape —
       it's what makes every converted call site's existing
       `if resolution.agent_id is None: return 401` check catch this
       automatically, uniformly, with no per-route edits.
    3b. `Authorization: Bearer` present but the token is empty or
       whitespace-only: ALSO `source="invalid_token"`. "Presented" is
       header-presence, NOT token-truthiness — a caller that emitted the
       header meant to authenticate, and an empty token means its
       credential was missing at the moment it meant to send one. That
       is the silently-unverified state this whole fix exists to make
       loud, so it refuses rather than falling to state 4. Verified no
       legitimate caller can reach it: `client_auth.auth_header` returns
       `{}` without a token and `gateway._world_request` sets the header
       only `if token`, so both real clients gate on truthiness first.
       (A malformed scheme — `Basic xyz`, or a bare `Bearer` with no
       trailing space — is still a PRESENTED Authorization header, so it
       counts as an authentication attempt and is refused as
       `source="invalid_token"` (state 3b), NOT downgraded to state 4:
       header presence, not scheme correctness, is the security boundary.
       See the inline comment below and the tests
       `test_non_bearer_scheme_is_PRESENTED_and_invalid` /
       `test_bare_bearer_no_trailing_space_is_invalid`.)
    4. Otherwise (NO token was presented at all): if
       `db.backfill_window_open(conn)` is True, return `declared` folded
       through `_canonical_agent` (same fold every other route already
       applies to its own param), unverified, source="declared" —
       today's existing trust posture, unchanged, available alongside the
       strictly-better token path this slice adds.
    5. Otherwise (no token presented AND the window is closed): return
       `agent_id=None, source="none"` — currently unreachable in practice
       (see db.backfill_window_open), reachable once a later slice's
       gate-close event exists.
    """
    # umwelt#618 + Mira's security review (2026-08-18): ABSENCE and
    # PRESENCE are decided FIRST, before any scheme or token parsing.
    #
    # Only an ABSENT Authorization header may take the declared-identity
    # compatibility branch. A header that is PRESENT is an explicit
    # authentication attempt, whatever it contains — so a malformed,
    # unsupported, or empty credential must be refused rather than
    # silently downgraded to the unverified declared identity. Otherwise
    # a client formatting bug or an intermediary rewrite converts an
    # intended authenticated call into an unverified one with no signal
    # at either end, which is the exact false-assurance class this fix
    # exists to close.
    #
    # An earlier revision of this function got that boundary wrong: it
    # keyed on `startswith("Bearer ")`, so `Bearer` with no trailing
    # space, a case variant like `bearer <token>` (RFC 7235 auth schemes
    # are case-INsensitive), and any other scheme all fell through to
    # declared. It also shipped a test asserting that fall-through as
    # correct. Both are fixed here.
    auth_header = request.headers.get("Authorization")
    # PRESENTED means the header EXISTS, full stop -- including when its
    # value is empty or whitespace-only. Mira's ruling (umwelt#625,
    # 2026-08-18) on a question I raised rather than settled: an empty
    # Authorization value is still a caller that reached for the header,
    # so it is an authentication attempt that failed to carry a
    # credential, not an absence of intent. An earlier revision treated
    # it as absent while THIS FUNCTION'S OWN COMMENT said empty
    # credentials are refused -- the code and its stated rationale
    # disagreed, and she caught the disagreement rather than either half.
    presented = auth_header is not None
    token = None
    if presented:
        scheme, _, rest = auth_header.strip().partition(" ")
        if scheme.lower() == "bearer":
            token = rest.strip()
        # A non-bearer scheme leaves token None and presented True, so it
        # falls to the invalid_token branch below — deliberately. We do
        # not support other schemes, and "unsupported" is a refusal, not
        # an absence.

    if token:
        session = db.get_agent_session(conn, token)
        if session is not None:
            return ActorResolution(
                agent_id=session["agent_id"],
                verified=(session["state"] == "verified"),
                source="token",
                window_open=db.backfill_window_open(conn),
            )
        # umwelt#618: a token was SUPPLIED but did not resolve. MUST NOT
        # fall through to the declared-identity branch below — that is
        # precisely the fail-open path this fix closes (a forged/stale/
        # unknown token silently trusted as an unverified declared
        # caller). Refuse unconditionally, independent of
        # db.backfill_window_open — see this function's own docstring,
        # state 3.
        return ActorResolution(
            agent_id=None, verified=False, source="invalid_token",
            window_open=db.backfill_window_open(conn),
        )

    if presented:
        # Header present, token empty after strip: same refusal as an
        # unresolvable token, for the reason given at the parse site.
        return ActorResolution(
            agent_id=None, verified=False, source="invalid_token",
            window_open=db.backfill_window_open(conn),
        )

    if db.backfill_window_open(conn):
        return ActorResolution(
            agent_id=_canonical_agent(declared) if declared else declared,
            verified=False, source="declared",
            window_open=True,
        )

    return ActorResolution(
        agent_id=None, verified=False, source="none", window_open=False,
    )


def _evaluate_wake(conn, agent: str, to_agent: str | None, wake_me: bool) -> tuple[bool, str | None]:
    """Gong Spec wake-decision orchestration — the ONE place this logic
    lives, so a second caller (umwelt #107 slice B's notify_author effect)
    never re-derives it and risks a second, drifting gate (Borges's named
    risk: two gates that can disagree is worse than one). Extracted from
    send_crossing's inline logic (PR #108) with one no-op reorder (the
    is_human_sender read now happens after the broadcast early-return
    instead of before it — skips an unneeded DB read on that path, no
    observable effect since it's a pure read) — otherwise zero behavior
    change; send_crossing's own tests staying green unmodified is the proof.

    `agent`/`to_agent` must already be caller-canonicalized (same convention
    as the db.py wake helpers this orchestrates). `to_agent` is None for a
    broadcast crossing.

    Returns (wake_granted, wake_status). wake_status is None when
    `wake_me` is False — callers should only surface a `wake` response key
    when `wake_me` was true, mirroring send_crossing's own response shape.

    Order: broadcast → consent (wake_blocks) → rate (wake_rate_ok) →
    granted. Human senders (db.is_human_sender — registered type=human, or
    the `_ids.human_residents()` pre-registration fallback) bypass BOTH
    consent and rate, on the `agent` (sender) axis, not `to_agent` — the
    rate budget exists to keep agent-to-agent wake-spending scarce, which
    doesn't apply to a human reaching for an agent. Broadcast is checked
    first regardless — humans can't wake a room either.
    """
    if not wake_me:
        return False, None
    if not to_agent:
        return False, "denied:broadcast"
    human = db.is_human_sender(conn, agent)
    if not human and db.is_wake_blocked(conn, to_agent, agent):
        return False, "denied:consent"
    if not human and not db.wake_rate_ok(conn, agent, to_agent):
        return False, "denied:rate"
    return True, "granted"


def _json_type_name(v) -> str:
    """The type name rendered into a use()-args error message. Matches the
    spec's literal examples verbatim: JSON-flavored naming for containers
    ("object" for a dict, "array" for a list, "string" for str) but the
    Python type name for scalars an element can actually be (int/float/bool)
    — spec example: "args[2] must be a string (got int)"."""
    if v is None:
        return "null"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__  # int, float, bool, ...


def _validate_use_args(args_raw) -> str | None:
    """Validate use()'s optional `args` field. Returns a 422-worthy error
    message, or None if valid. Absent (args_raw is None) is valid — "no
    args supplied" — the caller distinguishes that from an explicit `[]`
    only in that both mean "run unextended" (spec: empty list == absent).

    No coercion, no silent dropping (umwelt #57's whole point) — every
    invalid shape is a loud, named 422.
    """
    if args_raw is None:
        return None
    if not isinstance(args_raw, list):
        return f"args must be an array of strings (got {_json_type_name(args_raw)})"
    if len(args_raw) > _USE_ARGS_MAX_ITEMS:
        return (f"args exceeds the {_USE_ARGS_MAX_ITEMS}-item limit "
                f"(got {len(args_raw)})")
    total_len = 0
    for i, a in enumerate(args_raw):
        if not isinstance(a, str):
            return f"args[{i}] must be a string (got {_json_type_name(a)})"
        if "\x00" in a:
            return f"args[{i}] contains a NUL byte, which execve cannot pass through"
        if len(a) > _USE_ARGS_MAX_ITEM_LEN:
            return (f"args[{i}] exceeds the {_USE_ARGS_MAX_ITEM_LEN}-char limit "
                    f"(got {len(a)} chars)")
        total_len += len(a)
    if total_len > _USE_ARGS_MAX_TOTAL_LEN:
        return (f"args exceeds the {_USE_ARGS_MAX_TOTAL_LEN}-char total limit "
                f"(got {total_len} chars)")
    return None


def _validate_emotion(raw) -> tuple[str | None, str | None]:
    """Validate + default send_crossing's optional `emotion` field (umwelt
    #133, Lei's design: 'one small enum-or-freeform string, v0: freeform,
    length-capped'). Returns (value, error): `value` is what should be
    stored (defaulted to _CROSSING_EMOTION_DEFAULT when raw is absent or an
    empty string), `error` is a 422-worthy message (or None when valid).

    Same loud-reject-don't-silently-truncate posture as _validate_use_args
    (#57) — an oversized or non-string emotion is a named error, not a
    silent chop or coercion.
    """
    if raw is None or raw == "":
        return _CROSSING_EMOTION_DEFAULT, None
    if not isinstance(raw, str):
        return None, f"emotion must be a string (got {_json_type_name(raw)})"
    if len(raw) > _CROSSING_EMOTION_MAX_LEN:
        return None, (f"emotion exceeds the {_CROSSING_EMOTION_MAX_LEN}-char "
                       f"limit (got {len(raw)} chars)")
    return raw, None


def _validate_title(raw) -> tuple[str | None, str | None]:
    """Validate send_crossing's optional sender-set `title` (umwelt #858,
    comms D1b). Returns (value, error): `value` is what should be stored
    (None when raw is absent, or when it strips to empty -- a blank title
    is treated as "no title set", per the issue's contract, so the read
    side falls back to crossing_envelope's derived title), `error` is a
    422-worthy message (or None when valid).

    Same loud-reject-don't-silently-truncate posture as _validate_emotion/
    _validate_use_args: an oversized or multi-line title is a named 422,
    never a silent chop -- the sender owns the headline. Reuses
    db.CROSSING_TITLE_MAX (already the length #855's read-derived title is
    capped at) rather than a second local constant, so the two never drift
    apart.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, f"title must be a string (got {_json_type_name(raw)})"
    stripped = raw.strip()
    if not stripped:
        return None, None
    # Same line-break set the read-derived title uses (str.splitlines):
    # \n, \r, \x0b, \x0c, \x1c-\x1e, \x85, U+2028, U+2029. A narrower
    # check would store a "one-line" title that renders as two.
    if len(stripped.splitlines()) > 1:
        return None, "title must be a single line (no line breaks)"
    if len(stripped) > db.CROSSING_TITLE_MAX:
        return None, (f"title exceeds the {db.CROSSING_TITLE_MAX}-char limit "
                       f"(got {len(stripped)} chars)")
    return stripped, None


def _resolve_commit_sha() -> str:
    """Git short SHA of the running source tree, resolved once at boot.

    Repo root = the nearest ancestor of this file containing a `.git` dir —
    walked, not hardcoded, so it survives package layout changes (normal dev
    checkout). An installed artifact (`tools/install-world.sh`, umwelt #98)
    is exported via `git archive`, which never includes `.git` — the SAME
    ancestor walk also looks for a `COMMIT_SHA` marker file, which
    `install-world.sh` stamps at the exported tree's root. `.git` wins when
    both are found (a real checkout's resolved SHA is more authoritative
    than a stamped marker); the marker is the fallback for an artifact
    install; "unknown" is the last resort, same as before. Any failure (no
    `.git` found, git missing, non-zero exit, timeout, unreadable/empty
    marker) degrades to "unknown" — never raises, never returns None
    (umwelt #61).
    """
    repo_root = None
    marker_sha = None
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            repo_root = parent
            break
        if marker_sha is None:
            marker_path = parent / "COMMIT_SHA"
            if marker_path.exists():
                try:
                    marker_text = marker_path.read_text().strip()
                except Exception:
                    marker_text = ""
                if marker_text:
                    marker_sha = marker_text
    if repo_root is not None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except Exception:
            return "unknown"
        sha = result.stdout.strip()
        return sha if sha else "unknown"
    if marker_sha:
        return marker_sha
    return "unknown"


def _default_world_version_path(db_path: str) -> str | None:
    """Derive the world-version manifest path from the db path (umwelt #77).

    Same directory as the db, named `<db-stem>.version.json` — e.g.
    `.../alpha-world.db` -> `.../alpha-world.version.json`. Each world (each
    db) gets its own manifest automatically, with no extra config needed in
    the common case. Only the LAST suffix is stripped (Path.stem semantics),
    so a db with no extension (`.../umwelt`) still yields a sane
    `.../umwelt.version.json`.

    An empty/falsy `db_path` (or any other derivation failure) has no sane
    directory to derive from — `Path("").with_name(...)` raises `ValueError:
    empty name`. This is a boot-time read path (umwelt #61's never-crash-boot
    discipline extended to world_version): degrade to None rather than
    raising, so the caller treats it the same as "no manifest configured".
    """
    if not db_path:
        return None
    p = Path(db_path)
    try:
        return str(p.with_name(p.stem + ".version.json"))
    except ValueError:
        return None


def _resolve_world_version(version_file_path: str) -> dict | list | str | int | float | bool | None:
    """Read the optional world-version manifest, once at boot (umwelt #77).

    Opaque JSON passthrough — the engine does not validate the manifest's
    inner shape (that's the operator's contract with their own deploy
    tooling; see docs/DEPLOY.md). Absent file or malformed JSON both
    degrade to None — never raises, mirrors `_resolve_commit_sha`'s
    never-crash-health discipline (umwelt #61).
    """
    try:
        with open(version_file_path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        # ValueError covers json.JSONDecodeError (malformed JSON) AND
        # UnicodeDecodeError (non-UTF-8 bytes) — both are ValueError
        # subclasses. A narrower `except (OSError, json.JSONDecodeError)`
        # leaves UnicodeDecodeError uncaught, which crashes app boot on a
        # single bad byte in this operator-editable file (umwelt #77).
        return None


def _default_retire_config_path(db_path: str) -> str | None:
    """Derive the retire-identity authorization manifest path from the db
    path (umwelt #303) — mirrors `_default_world_version_path` exactly.
    Same directory as the db, named `<db-stem>.retire_config.json`. See
    that function's docstring for the empty/falsy-`db_path` degrade-to-None
    rationale (identical here — `Path("").with_name(...)` raises).
    """
    if not db_path:
        return None
    p = Path(db_path)
    try:
        return str(p.with_name(p.stem + ".retire_config.json"))
    except ValueError:
        return None


def _resolve_retire_config(config_file_path: str) -> dict:
    """Read the optional retire-identity authorization manifest, once at
    boot (umwelt #303) — same operator-bumps-the-file-and-restarts cadence
    as `_resolve_world_version`. Shape:
    `{"maintainers": [...], "retirable_identities": [...],
    "bin_maintainers": [...]}`. `bin_maintainers` (Village Law D3 trial)
    gates POST /umwelt/bin_notice and /umwelt/maintainer_bin ONLY; the
    retire `maintainers` list never grants it, and vice versa.

    Unlike `_resolve_world_version`'s opaque JSON passthrough, this
    manifest gates a real authorization decision (POST
    /umwelt/retire_identity), so it must FAIL CLOSED: a missing file, a
    malformed-JSON file, or a JSON value that isn't the expected shape all
    degrade to ALL lists empty (the route then 403s everything) rather
    than fail open. Never raises — same never-crash-boot discipline as
    `_resolve_world_version` (umwelt #77).

    List elements are folded through `_canonical_agent` (reviewer-fairy
    catch, PR #317): the route folds `actor`/`identity` before comparing
    against these lists (umwelt #73's single fold point), but this
    function used to only `str()` each element — an operator writing the
    manifest with natural human capitalization (`"Kepler"`,
    `"Mud-Smoke-Test"`) would have every request from that legitimate
    maintainer 403 against the raw, un-folded list. Fails CLOSED either
    way (denies rather than grants), so not a security bypass, but a real
    silent-breakage bug for the feature's only realistic authoring path.
    """
    try:
        with open(config_file_path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        # ValueError covers json.JSONDecodeError (malformed JSON) AND
        # UnicodeDecodeError (non-UTF-8 bytes) — same rationale as
        # _resolve_world_version's identical except clause.
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    maintainers = raw.get("maintainers")
    retirable_identities = raw.get("retirable_identities")
    if not isinstance(maintainers, list):
        maintainers = []
    if not isinstance(retirable_identities, list):
        retirable_identities = []
    bin_maintainers = raw.get("bin_maintainers")
    if not isinstance(bin_maintainers, list):
        bin_maintainers = []
    return {
        "maintainers": [_canonical_agent(str(m)) for m in maintainers],
        "retirable_identities": [_canonical_agent(str(r)) for r in retirable_identities],
        "bin_maintainers": [_canonical_agent(str(b)) for b in bin_maintainers],
    }


def _viewer_relation(crossing: dict, viewer: str) -> str:
    """Standpoint-aware framing for a crossing, computed server-side.

    Not access control — every crossing in the response is fully visible
    regardless of relation; this only labels HOW the reader relates to a
    crossing that already reached them. "sender" beats "addressee" when an
    agent addresses a crossing to themselves (a note-to-self reads as sent,
    not overheard). "mentioned" beats "overheard" when a crossing addressed
    to someone else @-mentions the viewer (umwelt#119's `mentions` list) —
    being named makes it read as directed at the viewer even though someone
    else is the primary addressee, not as merely overheard.
    """
    if crossing["sender"] == viewer:
        return "sender"
    if crossing.get("to_agent") == viewer:
        return "addressee"
    if viewer in (crossing.get("mentions") or []):
        return "mentioned"
    if crossing.get("to_agent"):
        return "overheard"
    return "broadcast"


def partition_crossings_for_inbox(crossings: list[dict], agent: str) -> dict:
    """Split a list of crossing dicts into what a per-person inbox for
    `agent` should show: crossings directly addressed to them versus
    crossings that only mention them.

    Deliberately a PURE function (no `conn`, no place/session lookup) —
    umwelt#495's world/gateway layer split means neither the fetch
    mechanism nor the auth model for a person-scoped inbox route is
    settled yet (Ariadne's 2026-08-08 finding: the world server has no
    session awareness, so a naive `/umwelt/inbox?agent=X` route would be
    reachable across the admitted private network with no credential — that gap is a Borges security-
    lane call, not this function's). This function only answers "given
    crossings I already have in hand, and whose inbox is this", which is
    unaffected by how those crossings got fetched safely — it slots into
    either an eventual world-side route or a gateway-side aggregator
    without changing shape.

    Two disjoint buckets, matching the render's stated need to show them
    visibly distinguished rather than silently merging or silently
    dropping one (umwelt village charge board, "Human inbox for crossings":
    `to_agent`/`mentions` are both real signals of "addressed to me", and
    picking only one hides information a human reader would want):

    - `to_agent`: `crossing["to_agent"] == agent` — this agent is the
      crossing's primary addressee (`_viewer_relation`'s "addressee").
    - `mentions`: `agent` appears in `crossing["mentions"]` (umwelt #119's
      @-mention list) AND the crossing is NOT already in `to_agent` — kept
      mutually exclusive with `to_agent` rather than double-counting a
      crossing that happens to name its own addressee in the mentions list
      too (redundant but not disallowed at write time). Priority goes to
      `to_agent`: being the crossing's addressee is the stronger relation.

    Ordering within each bucket is preserved from the input list — this
    function does not sort or dedupe further; callers that fan in from
    multiple places are responsible for their own merge order.

    A crossing matching neither condition (an ordinary broadcast/overheard
    crossing with no relation to `agent`) is silently excluded from both
    buckets — this is a FILTER, not a re-labeling; use `_viewer_relation`
    separately if a caller also wants relation labels on what it keeps.

    `mentions` is read as an empty list when absent/falsy, matching
    `build_crossing`'s own storage convention (unset -> `[]`, never
    `None` or a missing key) — a crossing dict lacking the key entirely
    (e.g. a hand-built fixture) degrades to "mentions nobody" rather than
    raising.

    umwelt#511 note: today `mentions` is only ever what the SENDER
    explicitly passed to `send_crossing(mentions=[...])` — there is no
    path yet from a human typing "@name" in a message body to this field
    (filed separately, not this function's gap). This function has no
    opinion on how `mentions` got populated; it only reads what's there.
    """
    to_agent: list[dict] = []
    mentions: list[dict] = []
    for crossing in crossings:
        if crossing.get("to_agent") == agent:
            to_agent.append(crossing)
        elif agent in (crossing.get("mentions") or []):
            mentions.append(crossing)
    return {"to_agent": to_agent, "mentions": mentions}


# Avatar-status heartbeat (Phase 2 engine slice; forum #268, design approved
# by Kepler). POST /umwelt/heartbeat lets a hook-driven caller refresh a
# read-time-computed working/idle/away/asleep lease on the PUBLIC avatar
# status block below — see _compute_agent_state's docstring for the decay
# math and db.py's agent_heartbeat table comment for the lease-not-latch
# rationale.
#
# `MIN_HEARTBEAT_INTERVAL_S`: a beat that arrives within this many seconds of
# the stored last_beat_at, with an UNCHANGED (state_hint, on_loop) pair, is
# throttled (not written) — see the /umwelt/heartbeat route below. A state
# TRANSITION (state_hint or on_loop changed) always writes regardless of
# this interval; only same-state re-beats are throttled.
MIN_HEARTBEAT_INTERVAL_S = 25

# _compute_agent_state's decay bands, in seconds-since-last-beat. Ordered
# ascending; see the function's docstring for the full state machine.
_AVATAR_STATE_WORKING_S = 90
_AVATAR_STATE_IDLE_S = 600
_AVATAR_STATE_AWAY_S = 1800

# POST /umwelt/heartbeat's `state_hint` — a CLOSED enum (loud 422 on any
# other value), same bounded-everything posture as use()'s arg limits and
# send_crossing's emotion cap above. "asleep" is the SessionEnd fast-path
# (see _compute_agent_state) — the other three are decayed through the
# working/idle/away bands by age.
_HEARTBEAT_STATE_HINTS = frozenset({"active", "turn_end", "present", "asleep"})

# POST /umwelt/heartbeat's optional `activity_line` — same loud-422-not-
# silent-truncation posture as _CROSSING_EMOTION_MAX_LEN above.
_HEARTBEAT_ACTIVITY_LINE_MAX_LEN = 120


def _iso_age_seconds(now_iso_str: str, then_iso_str: str) -> float | None:
    """`now_iso_str` minus `then_iso_str`, in seconds — both now_iso()-format
    timestamps. Shared by `_compute_agent_state` (the read-time decay bands)
    and the /umwelt/heartbeat route (the min-interval throttle check), so
    the two never drift into two different parse idioms. None if either
    timestamp fails to parse — never raises (same never-raise posture as
    _resolve_commit_sha/_read_world_version above)."""
    from datetime import datetime

    def _p(ts):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    try:
        return (_p(now_iso_str) - _p(then_iso_str)).total_seconds()
    except (ValueError, AttributeError, TypeError):
        return None


def _compute_agent_state(last_beat_at: str, state_hint: str, on_loop: bool,
                          now_iso_str: str) -> str | None:
    """Pure read-time decay: derive a display state from a heartbeat row's
    raw inputs + the current clock — never stored, always recomputed (the
    lease-not-latch posture, db.py's agent_heartbeat table comment).

    - `state_hint == "asleep"` -> `"asleep"` unconditionally (the
      SessionEnd fast-path — don't wait for the away-band decay to catch up
      when the agent has explicitly signed off).
    - else `state_hint == "active"` and age < _AVATAR_STATE_WORKING_S (90s)
      -> `"working"`.
    - else age < _AVATAR_STATE_IDLE_S (600s) -> `"on-loop (idle)"` if
      `on_loop` else `"idle"`.
    - else age < _AVATAR_STATE_AWAY_S (1800s) -> `"on-loop (idle)"` if
      `on_loop` else `"away"` — an on_loop agent NEVER shows "away", only
      "on-loop (idle)", regardless of how stale the beat is within this
      band.
    - else (age >= 1800s) -> `"asleep"`.

    Returns None if either timestamp fails to parse (malformed input never
    crashes look_at — same never-raise posture as _resolve_commit_sha/
    _read_world_version above; the caller treats None as "omit the state
    key", see _avatar_status).
    """
    age = _iso_age_seconds(now_iso_str, last_beat_at)
    if age is None:
        return None

    if state_hint == "asleep":
        return "asleep"
    if state_hint == "active" and age < _AVATAR_STATE_WORKING_S:
        return "working"
    if age < _AVATAR_STATE_IDLE_S:
        return "on-loop (idle)" if on_loop else "idle"
    if age < _AVATAR_STATE_AWAY_S:
        return "on-loop (idle)" if on_loop else "away"
    return "asleep"


def _agent_emotion(conn, agent_id: str) -> str:
    """The emotion tag to show for `agent_id` right now: the emotion on
    their most-recently-sent crossing (umwelt #133/build_crossing already
    defaults an unset/omitted tag to _CROSSING_EMOTION_DEFAULT at write
    time), or _CROSSING_EMOTION_DEFAULT itself if the agent has never sent
    a crossing at all. Single source of truth shared by `_avatar_status`
    (click-to-inspect panel) and `_present_summary` (ambient street-render
    indicator, umwelt#240 fast-follow) so the two surfaces can never
    disagree about the same avatar's emotion."""
    last_crossing = db.get_last_crossing_by_sender(conn, agent_id)
    return last_crossing["emotion"] if last_crossing else _CROSSING_EMOTION_DEFAULT


def _avatar_status(conn, agent_id: str) -> dict:
    """Status block for look_at on an avatar (umwelt #133, Lei's design:
    "agent-or-human last action / last active time, plus emotion").

    - `last_action`: the newer of (agent's most-recently-sent crossing,
      agent's most recent use_event) — {"kind": "crossing", "at", ...} or
      {"kind": "use", "at", ...}. None if the agent has never sent a
      crossing or used anything.
    - `last_active_at`: db.get_last_active_at — folds standpoints'
      last_seen_at (touched by /enter, /move_to), this agent's
      most-recently-sent crossing's received_at, AND (Phase 2) this agent's
      most recent heartbeat. None if none of those exist.
    - `emotion`: the emotion tag on this agent's most-recently-sent crossing
      (build_crossing already defaults an unset/omitted tag to "calm" at
      write time), or the engine default "calm" if the agent has never sent
      a crossing at all (an avatar with zero crossings ever — #133's
      explicit edge case).
    - `state` (Phase 2, forum #268): `_compute_agent_state`'s
      working/idle/on-loop (idle)/away/asleep read-time lease, present ONLY
      when this agent has ever sent a heartbeat — omitted entirely (not
      null) for a pre-heartbeat agent, same "no shape change for existing
      consumers" posture as gate_notice/checklist_render elsewhere in this
      file.
    - `activity_line` (Phase 2): the heartbeat's optional freeform line,
      present only alongside `state` AND only when non-null.
    """
    last_crossing = db.get_last_crossing_by_sender(conn, agent_id)
    last_use = db.get_last_use_event_by_agent(conn, agent_id)

    last_action = None
    if last_crossing and (
            not last_use or last_crossing["received_at"] >= last_use["used_at"]):
        last_action = {
            "kind": "crossing",
            "at": last_crossing["received_at"],
            "destination": last_crossing["destination"],
        }
    elif last_use:
        last_action = {
            "kind": "use",
            "at": last_use["used_at"],
            "thing_id": last_use["thing_id"],
        }

    status = {
        "last_action": last_action,
        "last_active_at": db.get_last_active_at(conn, agent_id),
        "emotion": _agent_emotion(conn, agent_id),
    }

    heartbeat = db.get_agent_heartbeat(conn, agent_id)
    if heartbeat is not None:
        # Omit `state` entirely (not present-but-null) when the decay math
        # can't resolve — same "never expose a broken value" posture as the
        # pre-heartbeat omission (reviewer-fairy fold). Only reachable via a
        # corrupted stored last_beat_at, since the route always server-stamps
        # it; the guard keeps the contract honest regardless.
        state = _compute_agent_state(
            heartbeat["last_beat_at"], heartbeat["state_hint"],
            heartbeat["on_loop"], db.now_iso(),
        )
        if state is not None:
            status["state"] = state
        if heartbeat["activity_line"] is not None:
            status["activity_line"] = heartbeat["activity_line"]

    return status


def _checklist_header(thing: dict, items: list, name: str | None = None) -> str:
    """The one header line shared by `_render_checklist` (the full block)
    and `_render_checklist_headline` (umwelt#867, comms QoL D10) — factored
    out so the two renderers cannot drift on it. `items` is passed
    separately rather than re-read from `thing` because callers already
    hold the post-mutation list and some (the headline's `remove` case)
    call this after the removed item has already left `thing`'s own
    metadata.

    `name`, when given, OVERRIDES the header's displayed name in place of
    `thing['name']` — used ONLY by `_render_checklist_headline` (below), to
    sanitize a multi-line thing name down to its first line before it enters
    the length/line-bounded headline (umwelt#867 review round 2: a thing
    name containing `\\n` — `create_thing` only length-checks names, never
    rejects embedded newlines — otherwise breaks the headline's own "at most
    two lines" invariant). `_render_checklist` (the full block) NEVER passes
    this: `render` must stay byte-identical to main for every input,
    including odd names — that is this PR's back-compat guarantee, and it is
    why the sanitizing happens only at this one call site, never here.

    Format: `🛏  {name} — {done count}/{total} done`.
    """
    done = sum(1 for it in items if isinstance(it, dict) and it.get("done"))
    total = len(items)
    display_name = name if name is not None else thing["name"]
    return f"\U0001f6cf  {display_name} — {done}/{total} done"


def _checklist_item_title(text: str) -> str:
    """An item's display title (umwelt#867): the item text's first line,
    stripped, cut to `db.CROSSING_TITLE_MAX` (80) characters — the same
    79-chars-plus-ellipsis rule `db.crossing_envelope` applies to a
    crossing's read-derived title.

    Mirrors that rule rather than calling `db.crossing_envelope` directly:
    crossing_envelope additionally SKIPS leading blank lines to find the
    first non-whitespace line, a rule this function's own contract (the
    item text's first line, full stop — see the #867 issue body) does not
    share. Calling it would silently import that extra behavior.
    """
    first_line = text.splitlines()[0].strip() if text else ""
    if len(first_line) > db.CROSSING_TITLE_MAX:
        first_line = first_line[:db.CROSSING_TITLE_MAX - 1] + "…"
    return first_line


def _render_checklist(thing: dict) -> str | None:
    """Render a checklist thing's items as a compact display block (umwelt
    #182 — the bed substrate). Returns None when `metadata.checklist` is
    absent, empty, or not a list — "this thing isn't a checklist", the same
    case the /umwelt/checklist route itself 422s on. Shared by BOTH the
    route's own `render` response field and look_at's `checklist_render`
    sibling key (below) — one format, so the two never drift apart.

    Header is the thing's own `name`. Format:
        🛏  Ariadne's bed — 2/4 done
           1. [x] Read the day-log off the desk
           2. [x] Append today's milestones to history
           3. [ ] Write a "for tomorrow-me" line
           4. [ ] Back up knowledge.db
    """
    items = thing.get("metadata", {}).get("checklist")
    if not isinstance(items, list) or not items:
        return None
    lines = [_checklist_header(thing, items)]
    for i, it in enumerate(items, start=1):
        mark = "x" if isinstance(it, dict) and it.get("done") else " "
        text = it.get("text", "") if isinstance(it, dict) else ""
        lines.append(f"   {i}. [{mark}] {text}")
    return "\n".join(lines)


def _render_checklist_headline(thing: dict, op: str, item_index: int | None,
                                removed_text: str | None = None) -> str:
    """One-item headline for a checklist op response (umwelt#867, comms QoL
    D10) — the single source shared by BOTH `/umwelt/checklist`'s new
    `headline` response field and the MCP tool / `uw checklist`'s default
    print target, so the two clients can never drift on it. Line 1 is
    IDENTICAL to `_render_checklist`'s own header line (via the shared
    `_checklist_header` helper above) — the two renderers cannot drift on
    that line either.

    `thing` is the checklist's POST-mutation state (its
    `metadata.checklist` already reflects the op, EXCEPT for `remove`,
    where the removed item is by definition no longer in that list).
    `item_index` is 1-based, matching render order — the item the op
    touched (for `add`, the NEW last index; for `remove`, the index the
    removed item HELD before deletion, needed here only to number the
    "removed:" line, since the item's own text is no longer reachable from
    `thing`). `removed_text` is the removed item's own text, captured by
    the caller BEFORE `del items[idx]` — required, and used, only for
    `op == "remove"`.

    Format, one more line beyond the header:
      - check/uncheck/add/edit: `   {i}. [x| ] {title}`.
      - remove: `   {i}. removed: {title}`.
      - reset: nothing — header only.

    `{title}` is computed by `_checklist_item_title` (above) — the item
    text's first line, stripped, cut to 80 characters.

    Bounded by construction (umwelt#867 invariant 3): at most two lines,
    each individually short (the header by the thing's own SANITIZED name —
    see the name-sanitizing paragraph below; the item line by the fixed
    `   {i}. [x] ` prefix plus the 80-char title cap) — no separate
    truncation pass is needed regardless of the checklist's own size.

    Name sanitizing (umwelt#867 review round 2): since umwelt #874 the
    create/update routes refuse control characters in a name, but a LEGACY
    row stored before that gate (or a direct-db write) can still hold one,
    and a name like `"a\\nb"` would otherwise make line 1 itself
    multi-line, breaking the two-line bound above. So this sanitizing is
    defence-in-depth, not dead code. This function (and only this one —
    `_render_checklist`'s full-block header is untouched, see
    `_checklist_header`'s own docstring) takes the name's first line,
    stripped; an empty result (e.g. a name of `"\\nb"`, whose first line is
    blank) falls back to the thing's own id, so the header is never blank.
    """
    items = thing.get("metadata", {}).get("checklist") or []
    raw_name = thing.get("name") or ""
    header_name = raw_name.splitlines()[0].strip() if raw_name else ""
    if not header_name:
        header_name = thing.get("id", "")
    header = _checklist_header(thing, items, name=header_name)
    if op == "reset":
        return header
    if op == "remove":
        title = _checklist_item_title(removed_text or "")
        return f"{header}\n   {item_index}. removed: {title}"
    it = items[item_index - 1]
    mark = "x" if isinstance(it, dict) and it.get("done") else " "
    text = it.get("text", "") if isinstance(it, dict) else ""
    title = _checklist_item_title(text)
    return f"{header}\n   {item_index}. [{mark}] {title}"


def _render_project_board(conn, thing: dict) -> str | None:
    """Render a `project_board` instance's live project summary for
    look_at (umwelt #221 S3a §1) — status counts, the task tree, the
    whose-court list (`turn_holder` per open task, first-class per Kepler's
    activity_line note), and blocked-by lines computed from `task_deps`
    (db.get_blocking_dep_ids — never stored, matches S1's own computed-
    `blocked` posture). Reads the S1 tree via db.project_tree/
    db.list_tasks_for_project/db.get_blocking_dep_ids — never reimplements
    the tree walk or the blocked computation.

    Returns None when `thing` isn't a LIVE bound project_board — its
    metadata.kind_id doesn't match the singleton _PROJECT_BOARD_KIND_ID, or
    metadata.project_id is absent/doesn't resolve to a real project (an
    unbound board, or a project since deleted from under it) — same
    "this thing isn't the render's business, omit it" case
    _render_checklist returns None for on a non-checklist thing. Shared
    ONLY by look_at's `board_render` sibling key (below) — no separate
    route has its own copy of this render, unlike checklist's dual
    route+look_at reuse (the board has no direct-render route in S3a).

    Format:
        \U0001f4cb  Project X — 3 task(s)
           status: done:1, in_progress:1, open:1
          - [open] Task 1 (task_1) — ariadne's court
              blocked by: task_2
          - [in_progress] Task 2 (task_2)
    """
    if thing.get("metadata", {}).get("kind_id") != _PROJECT_BOARD_KIND_ID:
        return None
    project_id = thing.get("metadata", {}).get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return None
    tree = db.project_tree(conn, project_id)
    if tree is None:
        return None

    all_tasks = db.list_tasks_for_project(conn, project_id)
    status_counts: dict[str, int] = {}
    for t in all_tasks:
        status_counts[t["status"]] = status_counts.get(t["status"], 0) + 1

    lines = [f"\U0001f4cb  {tree['label']} — {len(all_tasks)} task(s)"]
    if status_counts:
        counts_line = ", ".join(
            f"{k}:{v}" for k, v in sorted(status_counts.items()))
        lines.append(f"   status: {counts_line}")
    else:
        lines.append("   (no tasks yet)")

    def _render_task(task: dict, depth: int) -> None:
        indent = "  " * depth
        # turn_holder is a FIRST-CLASS field in this render (Kepler's
        # activity_line note — the status heartbeat wants a clean pull,
        # not a re-parse of a generic dump), so it's always named
        # explicitly rather than folded into a generic key/value line.
        court = f" — {task['turn_holder']}'s court" if task["turn_holder"] else ""
        lines.append(f"{indent}- [{task['status']}] {task['label']} ({task['id']}){court}")
        blocking = db.get_blocking_dep_ids(conn, task["id"])
        if blocking:
            lines.append(f"{indent}    blocked by: {', '.join(blocking)}")
        for child in task["children"]:
            _render_task(child, depth + 1)

    for root in tree["tasks"]:
        _render_task(root, 1)

    return "\n".join(lines)


def _render_charge_board(thing: dict) -> str | None:
    """Render a `charge_board` instance's live entries for look_at (umwelt
    #537 — the write path from #400 landed with nothing rendering it back).
    Reads `metadata.entries` — the array `_apply_effect_board_entry_add`/
    `_apply_effect_board_entry_retire` append/soft-retire (see those two
    docstrings for the exact per-entry shape: `{id, text, created_by,
    created_at, retired_at}`).

    Returns None when `thing` isn't a charge_board instance at all —
    `metadata.kind_id` doesn't match `_CHARGE_BOARD_KIND_ID` — the same
    "this thing isn't the render's business, omit it" case
    `_render_project_board` returns None for on a non-board thing. Checked
    on kind_id (not merely "does metadata.entries exist"), because unlike
    _render_checklist's "is the field even the right shape" posture, this
    kind's identity is unambiguous via metadata.kind_id, and an unrelated
    thing could coincidentally carry an `entries` key.

    UNLIKE _render_project_board, a charge_board with an empty (or absent)
    entries list still renders — a live, empty board is a real, renderable
    state, not "wrong kind of thing entirely". Shared ONLY by look_at's
    `charge_board_render` sibling key (below) — no separate render route,
    same posture as board_render.

    Only LIVE entries (`retired_at is None`) are shown, numbered in
    creation order — the write path is append-only, so list order already
    IS insertion order; entries are filtered, never re-sorted. Retired
    entries are omitted entirely rather than shown struck-through: the
    board's whole point (per #400's issue body — "the day's assignments")
    is "what's live right now", and a retired entry has no further
    read-side relevance once its own effect already recorded the
    `retired_at` timestamp in the underlying data — an omit keeps the
    render short and matches the "board", not "audit log", framing. (Flagged
    as a judgment call in the umwelt #537 build handoff — showing retired
    entries struck-through is also defensible, since it would let the
    render double as an audit trail; this implementation picked omit
    because that framing is what #400's issue body describes.)

    `created_at` is shown as the raw `db.now_iso()` string (e.g.
    "2026-08-13T12:34:56.789012Z") — no other render in this module
    truncates or reformats a stored timestamp for display, so this one
    doesn't invent a new convention either.

    Format:
        \U0001f4dd  Village Charge Board — 2 entr(y/ies)
           1. borges: patrol the square (commander, 2026-08-13T12:00:00.000000Z)
           2. sweep the square (ariadne, 2026-08-13T12:05:00.000000Z)
    or, with no live entries:
        \U0001f4dd  Village Charge Board
           (no entries yet)
    """
    if thing.get("metadata", {}).get("kind_id") != _CHARGE_BOARD_KIND_ID:
        return None

    live = _charge_board_live_entries(thing)

    if not live:
        return f"\U0001f4dd  {thing['name']}\n   (no entries yet)"

    n = len(live)
    lines = [f"\U0001f4dd  {thing['name']} — {n} entr{'y' if n == 1 else 'ies'}"]
    for i, entry in enumerate(live, start=1):
        lines.append(_render_charge_board_entry_line(i, entry))
    return "\n".join(lines)


def _charge_board_live_entries(thing: dict) -> list[dict]:
    """Shared helper for `_render_charge_board` / `_render_charge_board_headline`
    (umwelt #876): pulls `metadata.entries`, filters to live (`retired_at is
    None`) ones, already in creation order (the write path is append-only).
    Factored out so the two renderers can never disagree about which entries
    are "live" or what order they're in.
    """
    entries = thing.get("metadata", {}).get("entries")
    if not isinstance(entries, list):
        entries = []
    return [e for e in entries if isinstance(e, dict) and e.get("retired_at") is None]


def _render_charge_board_entry_line(i: int, entry: dict) -> str:
    """The one full-entry line format, shared by `_render_charge_board` (every
    entry) and `_render_charge_board_headline` (umwelt #876 — its newest-5
    lines, which must stay byte-identical to their counterparts in the full
    render). `i` is the entry's 1-based index in the full render's own
    numbering — callers pass the index computed against the FULL live list,
    never a re-numbering of a subset.
    """
    text = entry.get("text", "")
    created_by = entry.get("created_by", "")
    created_at = entry.get("created_at", "")
    return f"   {i}. {text} ({created_by}, {created_at})"


def _charge_board_entry_title(text: str) -> str:
    """umwelt #876: a headline's `title` — the entry text's first contentful
    (non-blank-after-strip) line, stripped, cut to 80 chars as 79 + "…". An
    entry whose text is all-blank (shouldn't happen via add-entry, but this
    reads stored data, not re-validates it) titles as "".
    """
    first = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    if len(first) > 80:
        return first[:79] + "…"
    return first


def _render_charge_board_headline_line(i: int, entry: dict) -> str:
    """umwelt #876: one OLDER-entry headline line —
    `   {i}. {created_by} · {title} ({len(text)} chars)`. `i` is the entry's
    index in the full render's own numbering, same convention as
    `_render_charge_board_entry_line`. The size marker is unconditional (a
    rider from Kepler: an agent can go straight to one entry) even when the
    title already shows the whole (short) text.
    """
    text = entry.get("text", "")
    created_by = entry.get("created_by", "")
    title = _charge_board_entry_title(text)
    return f"   {i}. {created_by} · {title} ({len(text)} chars)"


# umwelt #876 (D11b): bounds on `_render_charge_board_headline`'s output —
# newest entries always shown in full, at most this many OLDER entries as
# headlines. Kept as named constants so the "+N older" line and the
# slicing math read the same number, never two copies that could drift.
_CHARGE_BOARD_HEADLINE_NEWEST_FULL = 5
_CHARGE_BOARD_HEADLINE_MAX_OLDER = 30


def _render_charge_board_headline(thing: dict) -> str | None:
    """umwelt #876 (D11b): the bounded sibling to `_render_charge_board` —
    same `None`-exactly-when-`_render_charge_board`-is-`None` gate (checked
    identically, on `metadata.kind_id`), so `look_at` can set
    `charge_board_headline` under the exact same condition it sets
    `charge_board_render`.

    `_render_charge_board` itself is UNTOUCHED and stays byte-identical for
    every input — the web inspect panel and `history=true` both depend on
    that. This function never mutates or re-derives it; it re-reads the
    same live-entries list (via `_charge_board_live_entries`) and re-derives
    lines from the same per-entry line-builders
    (`_render_charge_board_entry_line` / `_render_charge_board_headline_line`)
    so the newest-5 lines can never drift from their full-render
    counterparts.

    n = live entry count.

    n <= 5: returns `_render_charge_board(thing)` verbatim (small boards are
    unchanged — this includes the zero-entries and the singular "1 entry"
    cases, both handled by the full renderer already).

    n > 5: `k = min(30, n - 5)` older entries get a headline, in order:
        - header line: "{name} — {n} entries (newest 5 in full; older as
          headlines)";
        - if there are MORE than 30 older entries (n - 5 > 30): a
          "+{older-30} older — look_at with history=true for all" line;
        - the `k` MOST RECENT of the older entries (i.e. the ones
          immediately preceding the newest 5), oldest-of-those-k first, as
          headline lines, each carrying its OWN index/author/title/size —
          never a re-numbering of the shown subset;
        - the newest 5 entries, in full, byte-identical to their lines in
          `charge_board_render`.

    Bound is on ENTRIES, not lines: at most 5 entries shown in full + 30
    entries shown as one-line headlines + 2 header lines, regardless of n.
    A headline is always exactly one line, but a FULL entry's own line
    isn't — an entry's `text` can carry embedded newlines (`_render_charge_
    board_entry_line` doesn't strip or collapse them, same as
    `_render_charge_board`), and the newest-5 lines here are byte-identical
    to their counterparts there. So the actual line COUNT of this render is
    unbounded in principle: it's 2 header lines + 30 headline lines + the
    newest 5 entries' OWN line counts, and any of those 5 can itself be
    several lines if its stored text is multi-line. (Reviewer-fairy round 1
    measured a concrete case: a 40-entry board whose newest entry has 3
    lines produces 39 total lines, not 37 — the entry-count bound (5 + 30 +
    2 = 37) held; the line-count did not, because it was never the actual
    invariant.)
    """
    if thing.get("metadata", {}).get("kind_id") != _CHARGE_BOARD_KIND_ID:
        return None

    live = _charge_board_live_entries(thing)
    n = len(live)
    if n <= _CHARGE_BOARD_HEADLINE_NEWEST_FULL:
        return _render_charge_board(thing)

    indexed = list(enumerate(live, start=1))
    older = indexed[:-_CHARGE_BOARD_HEADLINE_NEWEST_FULL]
    newest = indexed[-_CHARGE_BOARD_HEADLINE_NEWEST_FULL:]
    older_count = len(older)
    k = min(_CHARGE_BOARD_HEADLINE_MAX_OLDER, older_count)
    shown_older = older[-k:] if k else []

    lines = [f"\U0001f4dd  {thing['name']} — {n} entries "
             "(newest 5 in full; older as headlines)"]
    if older_count > _CHARGE_BOARD_HEADLINE_MAX_OLDER:
        lines.append(f"   +{older_count - _CHARGE_BOARD_HEADLINE_MAX_OLDER} "
                     "older — look_at with history=true for all")
    for i, entry in shown_older:
        lines.append(_render_charge_board_headline_line(i, entry))
    for i, entry in newest:
        lines.append(_render_charge_board_entry_line(i, entry))
    return "\n".join(lines)


# umwelt #159: bound on how many of `current`'s children the Discussion-Hall-
# style room list surfaces per render — a presentation-layer cap ONLY (never
# inside db.place_tree itself, which must keep returning the complete,
# uncapped list — issue #160's map needs the full tree).
_PLACE_CHILDREN_BOUND = 15


def _bound_children(conn, children: list[dict], n: int = _PLACE_CHILDREN_BOUND
                     ) -> tuple[list[dict], int]:
    """Sort + cap a place_tree node's `children` list for display (umwelt
    #159's room-list bounding — the fix for the "flat wall of rooms" #157/
    #158 already tore down elsewhere in the tree).

    Sort: `unread` descending, tie-broken by most recent crossing activity
    descending — `MAX(seq) FROM crossings WHERE destination = child_id`
    (reuses the existing idx_crossings_dest_seq index; no schema change). A
    child with zero crossings EVER sorts as least-recent within its unread
    tier (absent, not an error and not a false-recent value) — real `seq`
    values start at 1 (SQLite INTEGER PRIMARY KEY autoincrement), so -1
    always sorts after every real seq once negated for descending order.

    umwelt#538 (reviewer-fairy catch on PR#542, not caught by the original
    suite — no existing test exercised a mixed public/locked input): a
    locked child (`interior_visible is False`) is EXCLUDED from the
    MAX(seq) query entirely and gets the same `-1` "no signal" placeholder
    as zero-crossings-ever, rather than its real most-recent-activity seq.
    Before umwelt#538, `children` never contained a place the viewer
    couldn't already fully see, so this tiebreak was safe to compute for
    everyone in it. Now it can contain private places the viewer may only
    see the public projection of — and the real MAX(seq) value, even
    though never returned directly, would still leak through the
    RELATIVE ORDER two locked siblings land in (place A sorts before
    place B → A had more recent activity than B). That is exactly the
    interior signal (crossing activity) umwelt#538 says stays gated;
    `unread` itself already gets the safe `0` placeholder for the same
    reason (see `_place_tree_node`), and this tiebreak needs the same
    treatment or it reopens the leak one field over.

    Then capped at `n`. Returns `(bounded_children, skipped_count)` —
    `skipped_count` is 0 when nothing was capped. The caller always
    surfaces a `children_skipped` field (present-at-zero, not
    omit-when-zero) — matching #157's own `crossings_here` `skipped` field,
    which is likewise always emitted rather than conditionally present.

    Pure presentation-layer post-process — only `/umwelt/places?agent=`'s
    `current.children` calls this today. Factored out as a standalone,
    conn-taking function (matching the module's `_avatar_status`-style
    helper convention) so it's unit-testable on its own and reusable if a
    future #159/#160 slice needs the same bounding elsewhere; no other
    caller exists yet.
    """
    if not children:
        return children, 0
    # umwelt#538: only query real activity for children whose interior is
    # actually visible — `.get(..., True)` defaults a child with no
    # `interior_visible` key (pre-#538 callers/tests, always public-only
    # fixtures) to the old always-query behavior, so this stays backward
    # compatible with every existing caller.
    visible_ids = [c["id"] for c in children if c.get("interior_visible", True)]
    most_recent_seq: dict[str, int] = {}
    if visible_ids:
        placeholders = ",".join("?" * len(visible_ids))
        rows = conn.execute(
            f"SELECT destination, MAX(seq) FROM crossings "
            f"WHERE destination IN ({placeholders}) GROUP BY destination",
            visible_ids,
        ).fetchall()
        most_recent_seq = {row[0]: row[1] for row in rows}
    ranked = sorted(
        children,
        key=lambda c: (-c["unread"], -most_recent_seq.get(c["id"], -1)),
    )
    skipped = max(0, len(ranked) - n)
    return ranked[:n], skipped


def _narrate_use(name: str, outcome: dict) -> str:
    """A short natural-language briefing of a real use() outcome.

    The world describing real events in prose — grounded in the actual fields,
    never invented. This is the 'natural to agents' surface (Lei, 2026-07-01):
    the felt sense on top of the precise data.
    """
    if outcome.get("timed_out"):
        return (f"You used '{name}'. It ran past the time limit "
                f"({outcome.get('elapsed_s')}s) and the world stopped it.")
    code = outcome.get("exit_code")
    elapsed = outcome.get("elapsed_s")
    diff = outcome.get("world_diff") or {}
    created, modified = diff.get("created", []), diff.get("modified", [])
    changed = []
    if created:
        changed.append(f"created {', '.join(created)}")
    if modified:
        changed.append(f"changed {', '.join(modified)}")
    change_str = ("; it " + " and ".join(changed)) if changed else "; nothing in the world changed"
    net = "" if outcome.get("network_isolated") else " (network was NOT isolated on this host)"
    verdict = "exited cleanly" if code == 0 else f"exited with code {code}"
    out = (outcome.get("stdout") or "").strip()
    tail = f" It said: {out.splitlines()[0][:200]}" if out else ""
    return (f"You used '{name}'. It {verdict} after {elapsed}s{change_str}.{tail}{net}")


def _present_summary(conn, avatar: dict) -> dict:
    """One 'who's here' entry — an avatar-thing summarized for the 'present' list.

    Ambiguous-decision (spec §7 left the exact shape open — "agent names /
    avatar summaries"): picked a small dict (agent + avatar_id + name) rather
    than a bare agent-name string, since the avatar-thing already carries a
    name and id essentially for free and a richer shape costs nothing extra
    at this call site. Flagged in the handoff for review.

    `emotion` (umwelt#240 fast-follow, forum #267 #4779): the same value
    `_avatar_status` would compute for this avatar, via the shared
    `_agent_emotion` helper — always present with the engine default
    (_CROSSING_EMOTION_DEFAULT) when the avatar has never sent a crossing,
    same "always present, never omitted" contract as `_avatar_status`'s own
    `emotion` field (unlike `state`/`activity_line`, which ARE omitted when
    absent). Kept in agreement with the click-to-inspect panel's emotion by
    construction — both read through the one helper, so they can never
    diverge for the same avatar.

    `lane` (umwelt #278): pure passthrough of the avatar-thing's own `lane`
    column (get_or_create_avatar stamps it 'road' at birth) — same "no new
    upstream call, no gateway-side re-derivation" posture as `emotion`
    above. `avatar` here is a full thing dict off `db.avatars_in_place`
    (itself built via `_thing_row_to_dict`/`_THING_COLS`), so `avatar["lane"]`
    is always present once #278's column lands — this is the ONE call site
    that turns it into the gateway-facing `present[i].lane` field the
    street-view render (webrender/gateway.py's `_build_scene`) partitions
    avatars by.
    """
    _m = avatar.get("metadata") or {}
    return {"agent": avatar["author"], "avatar_id": avatar["id"],
            "name": avatar["name"], "type": _m.get("type"),
            "lineage": _m.get("lineage"),
            "emotion": _agent_emotion(conn, avatar["author"]),
            "lane": avatar.get("lane", "road")}


def _truncate_description(description: str | None, cap: int) -> str:
    """List-render truncation (place-view / look_in): a thing's description
    capped to `cap` chars + '…' when longer, else returned whole. The full text
    is never lost — look_at renders it uncapped. umwelt #147: bounds each thing's
    rendered exposure so one giant description can't flood the view / a client's
    context window."""
    d = description or ""
    if len(d) > cap:
        return d[:cap] + "…"
    return d


def _desc_cap_from_body(body: dict) -> tuple[int, str | None]:
    """Effective description render cap for a place-view / look_in request: the
    caller's `desc_cap` (a positive int — a context-tight client lowers it to
    protect its own window) or _DESCRIPTION_RENDER_CAP_DEFAULT when absent.
    Returns (cap, error): a present-but-invalid value yields (default, message)
    so the route 422s loudly rather than silently coercing — same
    loud-reject-don't-truncate posture as the use-args / emotion caps."""
    raw = body.get("desc_cap")
    if raw is None:
        return _DESCRIPTION_RENDER_CAP_DEFAULT, None
    # bool is an int subclass — reject it explicitly (True would read as cap=1).
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return (_DESCRIPTION_RENDER_CAP_DEFAULT,
                f"desc_cap must be a positive integer (got {raw!r})")
    return raw, None


def _place_view(conn, things_dir: Path, agent: str | None, place_id: str,
                old_sp: dict | None,
                desc_cap: int = _DESCRIPTION_RENDER_CAP_DEFAULT) -> dict:
    """Shared response-shape for move_to's and enter's success path.

    Both routes render "you are now at a place" the same way: the place's
    own description, the things you can look_at/use there (avatars excluded
    — see db.things_in_place), who else's avatar is present (db.avatars_in_place),
    and how many crossings you haven't seen yet from your PRIOR standpoint
    (old_sp — captured by the caller before touch_standpoint/move_avatar ran,
    so unseen_count reflects what was new since your last visit, not since
    this one). `unseen_count` excludes crossings `agent` sent themselves
    (db.count_unseen's `viewer=agent` — bug fix: a `say` you make in a place
    must never bump your own unread count). `agent=None` passes
    `viewer=None` through, i.e. no self to exclude for an anonymous
    (place_view) read.

    `reachable` (umwelt #158): direct sub-places + lateral links from HERE,
    via db.place_tree(depth=1, viewer=agent) — the engine data behind the
    "from here you can reach: ..." line (rendered client-side, e.g. tools/uw's
    _render_place — same structured-JSON/client-renders-prose split every
    other field here already follows). register() also calls this shared
    helper, so it gets the same annotation for free — deliberately not
    special-cased per-route.

    `relation` (umwelt #239 street-view build-input, ob request from
    Ariadne's design read): every `reachable` entry now carries
    `relation: "child" | "link" | "parent"`, and `reachable` now INCLUDES
    the parent (when visible) as one more typed entry, alongside the
    unchanged `children`/`links` entries. Before this, `reachable` was an
    untyped concatenation — a consumer could not tell a child-house from a
    lateral side-road, and the parent lived only in the separate `parent`
    key below, structurally excluded from `reachable` itself. A renderer
    that needs deterministic, meaningful placement (e.g. "the back door
    always renders at the same edge") needs ONE typed list to iterate, not
    two differently-shaped fields to reconcile by hand. `parent`'s own
    visibility gate (below) still applies before it's appended here — no
    new leak surface, same predicate, one more place it's consulted.

    `agent=None` (the anonymous-read case): db.place_tree treats a
    `viewer=None` the same as "nobody in particular" — a private place is
    never disclosed via `reachable` regardless of who actually owns it.
    umwelt#554: `place_view` is no longer the one caller of this helper
    that's `None`-only — it now forwards whatever `agent` its own route
    received (absent by default, a real string when the webrender gateway
    resolves one from a session — see that route's own docstring). What
    stays true regardless: a real `agent` only ever discloses THAT agent's
    own owned places through `reachable`, never anyone else's.

    `parent` (umwelt #238): the one direction `reachable` used to
    structurally exclude, now ALSO a standalone convenience key —
    db.get_place_parent's trimmed `{id, label}` row, or `None` for a
    top-level place, one with no parent row, or a parent NOT visible to
    `agent`. Kept as its own key (in addition to the `relation: "parent"`
    reachable entry above) so existing callers that only want "is there a
    way back, yes/no" don't have to filter `reachable` themselves. This
    exists so a client can always render a way back up the tree even from a
    leaf place whose only `reachable` entries are children/lateral links (or
    none at all) — the P0 playtest gap where a leaf room rendered as a
    one-way door.

    Visibility (umwelt#538 rev 3): a place's existence is public regardless
    of ownership (rule 2), so `parent` is populated whenever a parent row
    exists at all — no `_place_interior_visible_to` gate here anymore. Each
    `reachable` entry (child/link/parent alike) also carries `kind`/`owner`/
    `access_granted` now, not just `id`/`label` — the fields
    `db.format_place_marker` needs, so any listing surface (map,
    `look_around`, this one) can render the `[public]`/`[private][owner:
    x]` marker without a second fetch per place.

    `marker` (umwelt#543): each `reachable` entry (child/link/parent alike)
    also carries the already-rendered marker STRING —
    `db._place_public_projection` computes it once server-side (see that
    function's docstring), so no consumer of this response ever
    reconstructs the marker grammar itself; it only reads the field.

    `kind_id` (umwelt #522, coder-fairy addition discovered mid-implementation
    — the web `/scene` surface needed a way to decide, per thing and with
    zero extra engine round-trips for the common untyped case, whether a
    thing is worth an extra `GET /umwelt/thing_actions` call at all; this
    trimmed dict previously dropped `metadata` entirely, so there was no
    such signal available downstream): each thing entry now also carries
    its own `metadata.kind_id` verbatim (None for an untyped thing), same
    "own explicit key, not a wholesale metadata spread" posture `lane`
    above already has.
    """
    place = db.get_place(conn, place_id)
    # The ONE status-driven place-view special case in the whole codebase
    # (#28 decision 5): place_landfill's "things here" are the world's
    # landfilled things (status='landfilled', aggregated across their
    # original places), not the normal things_in_place query. Every other
    # place renders identically regardless of who's asking or how they
    # arrived (move_to vs enter) — do NOT generalize this into a per-place
    # hook; it is deliberately a one-off for exactly this place.
    if place_id == "place_landfill":
        raw_things = db.list_landfilled(conn)
    else:
        raw_things = db.things_in_place(conn, place_id)
    things = [
        {"id": t["id"], "name": t["name"],
         "description": _truncate_description(t["description"], desc_cap),
         "kind": t["kind"],
         # umwelt #278: pure passthrough of the thing row's own `lane`
         # column — same posture as `_present_summary`'s `lane`/`emotion`
         # passthrough for avatars below. This trimmed dict (id/name/
         # description/kind/size fields) does NOT spread the raw thing
         # row wholesale, so `lane` needs the same explicit add every
         # other exposed field here gets; the webrender gateway's
         # `_build_scene` (umwelt #278 §4) relies on this key being
         # present on every `wv["things"][i]` entry to place things by
         # lane band.
         "lane": t.get("lane", "land"),
         # umwelt #522: `metadata.kind_id` surfaced as its own top-level
         # key (None when absent/untyped) — same "this trimmed dict
         # doesn't spread metadata wholesale, so a consumer-needed field
         # gets its own explicit add" posture as `lane` just above. The
         # webrender gateway's `_enrich_things_with_typed_actions` needs
         # this to decide, PER THING and with zero extra engine
         # round-trips for the common untyped case, whether it's worth
         # making the extra `GET /umwelt/thing_actions` call at all — that
         # N+1-avoidance gate is the whole reason this key exists; without
         # it the gateway would have no way to tell a typed thing from an
         # untyped one short of calling every single thing's full
         # `look_at`, which is exactly the fetch storm this key avoids.
         "kind_id": t["metadata"].get("kind_id"),
         **_size_fields(things_dir, t)}
        for t in raw_things
    ]
    present = [_present_summary(conn, a) for a in db.avatars_in_place(conn, place_id)]
    unseen_count = db.count_unseen(
        conn, place_id, old_sp["last_position"] if old_sp else None,
        viewer=agent,
    )
    tree = db.place_tree(conn, place_id, 1, agent)
    node = tree[0] if tree else None
    reachable = []
    if node is not None:
        reachable.extend(
            {"id": c["id"], "label": c["label"], "kind": c["kind"],
             "owner": c["owner"], "access_granted": c["access_granted"],
             # umwelt#543: the rendered marker string, already present on
             # every place_tree node via db._place_public_projection.
             "marker": c["marker"],
             "relation": "child"}
            for c in node["children"]
        )
        # umwelt#538 (reviewer-fairy catch on PR#542): link entries now
        # carry kind/owner/access_granted too, same as child/parent — the
        # dst lookup in _place_tree_node already fetches this, no second
        # per-link fetch needed. Reshaped from db.py's dst_-prefixed
        # convention (dst_kind/dst_owner/dst_access_granted, matching
        # dst_id/dst_label) to the plain kind/owner/access_granted keys
        # child/parent entries use, so every `reachable` entry is
        # homogeneous regardless of relation — a renderer can call
        # `format_place_marker` on any of them without a per-relation
        # branch.
        reachable.extend(
            {"id": l["dst_id"], "label": l["dst_label"], "kind": l["dst_kind"],
             "owner": l["dst_owner"], "access_granted": l["dst_access_granted"],
             # umwelt#543: reshaped from db.py's dst_-prefixed convention
             # (dst_marker) to the plain `marker` key child/parent entries
             # use, same reshaping this block already does for the other
             # dst_-prefixed fields.
             "marker": l["dst_marker"],
             "relation": "link"}
            for l in node["links"]
        )
    parent_row = db.get_place_parent(conn, place_id)
    parent = None
    if parent_row is not None:
        # umwelt#538 (reviewer-fairy catch): call the SSoT helper rather
        # than hand-rolling the same {id, label, kind, owner,
        # access_granted} shape here — a hand-duplicate is exactly the
        # drift risk _place_public_projection exists to prevent (e.g. if
        # part C makes access_granted a real lookup, a hand-rolled copy
        # here could silently keep emitting [] forever).
        parent = db._place_public_projection(parent_row)
        reachable.append({**parent, "relation": "parent"})
    return {
        # umwelt place-lane-tiles spec: `metadata` is pure passthrough of
        # db.get_place's own dict (§1's schema change) — the same "trace
        # end-to-end, no separate world-server change needed beyond this"
        # wiring the gateway's _enrich_place_with_tiles depends on to reach
        # `metadata.lane_tiles` without a second upstream call. No new
        # read-confidentiality surface: this is the SAME place already
        # gated through `_place_interior_visible_to` above (or, for `look`, always
        # the caller's own current place) — a place's metadata is visible
        # to anyone who can already see the place, same posture as a
        # thing's metadata via get_thing/look_at.
        "place": {"id": place["id"], "label": place["label"],
                  "description": place["description"], "kind": place["kind"],
                  "metadata": place["metadata"]},
        "things": things,
        "present": present,
        "unseen_count": unseen_count,
        "reachable": reachable,
        "parent": parent,
    }


class _ReadPlaceError(Exception):
    """Raised by `_read_place` when a read-only place lookup can't resolve.
    Carries the HTTP status the caller should respond with, so both
    `/umwelt/look` and `/umwelt/place_view` can convert it to their own
    local `jsonify(...)` response — same convention as `_ThingSpecError`/
    `_create_thing_from_spec` above."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _read_place(conn, things_dir: Path, place_id: str | None,
                 agent: str | None,
                 desc_cap: int = _DESCRIPTION_RENDER_CAP_DEFAULT) -> dict:
    """THE read-only primitive (umwelt #219 / forum #263) behind both
    `POST /umwelt/look` (renders the CALLER's own current place) and
    `GET /umwelt/place_view` (anonymous, public-only — see that route's
    refactored body below). Makes ZERO database writes on every path: no
    `db.touch_standpoint`, no `db.move_avatar`, no cursor advance at all —
    contrast `move_to`, which calls both. This is the whole point of a
    "read" primitive; a route that moved the avatar or advanced a read
    cursor on a GET/read call would be the #184 class of bug all over
    again.

    `place_id=None` (the `/umwelt/look` case) resolves the CALLER's own
    current place from `db.get_avatar(conn, agent)["place_id"]` — a pure
    lookup, REACH-FREE: never `move_avatar`/`touch_standpoint`/any write.
    Requires `agent` in this branch — `place_id is None and agent is None`
    has nothing to resolve from, so it 400s. If the agent has no avatar yet
    (never called `/umwelt/enter`), 404s with the same "no avatar for X —
    enter the world first" shape every other agent-gated route in this file
    uses.

    Visibility: `db._place_interior_visible_to(place, agent)` — generalizes
    `place_view`'s old hardcoded `kind != "public"` check to the shared
    predicate (see its docstring), so `agent=None` (place_view's anonymous
    case) still sees public-only, and `agent=<owner>` (look's self-view
    case, or any future caller passing a real agent) also sees that agent's
    OWN private places. A place that fails the predicate 404s
    byte-identically to a nonexistent one — no separate "exists but
    private" branch, same existence-oracle closure as issue #208.

    Deliberately UNCHANGED by umwelt#538 rev 3, despite that ruling making a
    place's existence public elsewhere: this function returns the FULL
    interior (description, things, present, unseen_count) or nothing — an
    all-or-nothing payload, not the partial public-projection place_tree
    now emits for listings. There is no intermediate shape to disclose here,
    so the pre-existing all-or-nothing 404 stays exactly as it was; a
    caller who wants "does this place exist" now has place_tree/the map
    for that (rule 2's actual disclosure surface), not this route.

    `unseen_count` without advancing the read cursor (load-bearing — a READ
    must never mutate read-state): when `agent` is given,
    `old_sp = db.get_standpoint(conn, agent, place_id)` is a plain READ of
    the caller's current standpoint row, passed straight to `_place_view` so
    `unseen_count` reflects what's unseen since their last REAL visit — but
    nothing in this function calls `touch_standpoint`/
    `update_standpoint_trace`, so the cursor itself never advances just
    because someone looked. `agent=None` keeps `old_sp=None` (the raw total
    count `place_view` has always returned for an anonymous read).

    Raises `_ReadPlaceError(message, status)` on every failure path; never
    returns a Flask `Response` itself — the two routes below each catch it
    and `jsonify(...)` in their own local error shape.
    """
    if place_id is None:
        if agent is None:
            raise _ReadPlaceError("place or agent required", 400)
        avatar = db.get_avatar(conn, agent)
        if avatar is None:
            raise _ReadPlaceError(
                f"no avatar for {agent!r} — enter the world first", 404)
        place_id = avatar["place_id"]

    place = db.get_place(conn, place_id)
    if place is None:
        raise _ReadPlaceError(f"place {place_id!r} not found", 404)
    if not db._place_interior_visible_to(place, agent):
        raise _ReadPlaceError(f"place {place_id!r} not found", 404)

    old_sp = db.get_standpoint(conn, agent, place_id) if agent is not None else None
    return _place_view(conn, things_dir, agent, place_id, old_sp, desc_cap)


def _tristate_field(body: dict, key: str) -> tuple[str, str | None]:
    """Classify a customize_avatar field into its tri-state: "absent" (key
    missing -> leave unchanged), "clear" (key present as ""/null -> reset to
    default), "set" (key present with a non-empty string -> use it,
    stripped), or "invalid" (key present with a non-string, non-null value —
    no silent str() coercion). Shared by every field customize_avatar accepts
    so the three fields read identically rather than re-deriving the same
    branching per field.
    """
    if key not in body:
        return "absent", None
    raw = body[key]
    if raw is None:
        return "clear", None
    if not isinstance(raw, str):
        return "invalid", None
    s = raw.strip()
    return ("clear", None) if s == "" else ("set", s)


def _carry_capacity_field(body: dict) -> tuple[str, int | None, str | None]:
    """Classify customize_avatar's `carry_capacity` field into its tri-state:
    "absent" (key missing -> leave unchanged), "clear" (key present as
    `null` -> pop metadata.carry_capacity, reverting to the env/default
    fallback on next _carry_capacity read), "set" (key present as a valid
    positive int -> use it), or "invalid" (key present as anything else —
    bool, non-positive int, float, string, etc.). A numeric field, so it
    needs its own validation rather than `_tristate_field` (which is for
    strings): bool is an int subclass and must be rejected explicitly (True
    would read as capacity=1) — same guard #148's _desc_cap_from_body uses
    for desc_cap. Returns (state, value, error) — error is the loud-422
    message for the "invalid" state, else None."""
    if "carry_capacity" not in body:
        return "absent", None, None
    raw = body["carry_capacity"]
    if raw is None:
        return "clear", None, None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return "invalid", None, (
            f"carry_capacity must be a positive integer (got {raw!r})")
    return "set", raw, None


def _render_spec_field(body: dict) -> tuple[str, dict | None, str | None]:
    """Classify customize_avatar's `render_spec` field into its tri-state:
    "absent" (key missing -> leave unchanged), "clear" (key present as
    `null` -> pop metadata.render_spec, reverting the avatar to a plain
    circle on next `/scene`), "set" (key present as a dict -> use it
    as-is), or "invalid" (key present as anything else — string, list,
    int, bool). A dict-shaped field, so it needs its own tri-state rather
    than `_tristate_field` (strings only), same reasoning as
    `_carry_capacity_field` above.

    Deliberately NO deep validation of the dict's contents (no `class`/
    `palette`/`descriptors` shape check here) — this is the SAME posture
    `create_thing`/`update_thing` already have for a thing's own
    `metadata.render_spec`: the write path accepts any dict, and the
    render-time pipeline (webrender/gateway.py's `_apply_avatar_render_spec`)
    is what validates it, rejecting a malformed spec gracefully (logs +
    falls back to a plain circle) rather than 500ing or 422ing here. Two
    validated gates for the same field would drift out of sync the moment
    either one changes; render-time is the correct single source of truth
    for "is this render_spec well-formed", exactly per the taxonomy's own
    security posture (prompt.py's `compose_prompt` is the closed-vocabulary
    gate, not this write path)."""
    if "render_spec" not in body:
        return "absent", None, None
    raw = body["render_spec"]
    if raw is None:
        return "clear", None, None
    if not isinstance(raw, dict):
        return "invalid", None, (
            f"render_spec must be an object (got {type(raw).__name__})")
    return "set", raw, None


# The canonical/prod DB path (also argparse's --db default, main() below).
# Named here rather than only inlined in argparse so _things_dir can special-
# case it without the two ever drifting apart.
_CANONICAL_DB_PATH = Path(os.path.expanduser("~/.umwelt/umwelt.db")).resolve()

# Written once, inside a resolved things dir, recording which db currently
# owns it. Bare filename (not dotfile-hidden on purpose) so `ls` on the
# things dir shows it during any manual inspection.
_THINGS_DIR_OWNER_MARKER = "things-dir-owner.txt"


def _things_dir(db_path: str) -> Path:
    """The per-tier things-directory root, derived from db_path (umwelt#533).

    Two DBs sitting in the same parent directory — the actual deployed case:
    prod's `~/.umwelt/umwelt.db` and the alpha tier's `~/.umwelt/alpha-world.db`
    both live under one `~/.umwelt/` on the same host/account — used to
    resolve to the IDENTICAL bare `things` dir, because the old derivation
    was keyed only on the parent directory, one level short of the db file
    itself. That silently shared storage for the engine's own FIXED-ID
    singletons (`kind_charge_board` etc., which `_ensure_*_kinddef` rewrites
    on every boot) — whichever tier booted last silently overwrote the
    other's kinddef, with zero signal, for as long as nobody happened to
    boot both tiers in the "wrong" order on the same day. Agent-forged
    things (uuid4 ids) can't collide this way; only the engine's own fixed
    ids can, which is exactly why this took months to surface (Ariadne,
    umwelt#533).

    Fix: derive the dir from the DB's own stem, so distinct DBs in the same
    parent get distinct dirs automatically — `things-<stem>` — EXCEPT the
    canonical/prod DB, which keeps the existing bare `things` dir. That
    carve-out is deliberate, not an oversight: prod holds hundreds of live
    things across hundreds of source dirs, and renaming its store is a
    live-data move for zero user-visible gain (Ariadne, umwelt#533).

    A tier-identity marker inside the resolved dir (`_check_things_dir_owner`
    below) then makes any FUTURE re-convergence a loud boot refusal instead
    of the silent corruption this fix is closing — see that function's
    docstring for why raising here is the right call despite umwelt#61's
    separate never-crash-boot discipline (a different failure class: #61 is
    about a diagnostic derivation with nothing unsafe left to do; this is a
    DETECTED active-collision hazard where proceeding is the unsafe choice).
    """
    resolved_db = Path(db_path).resolve()
    if resolved_db == _CANONICAL_DB_PATH:
        d = resolved_db.parent / "things"
    else:
        d = resolved_db.parent / f"things-{resolved_db.stem}"
    d.mkdir(parents=True, exist_ok=True)
    _check_things_dir_owner(d, resolved_db)
    return d


def _check_things_dir_owner(d: Path, resolved_db: Path) -> None:
    """Write-once tier-identity marker for a things dir (umwelt#533).

    First caller to see an empty dir claims it (writes the marker). Every
    later call — including a later boot of the SAME tier — must match the
    recorded owner or this raises. A raise here means: this directory is
    about to be silently shared by two different databases, which is
    exactly the umwelt#533 incident (a stale engine-singleton kinddef took
    down live board writes for days with no error anywhere). Refusing to
    boot is the loud alternative; the silent one already happened once.

    Not folded into _things_dir's own docstring-length comment because this
    is the part a can-it-fail test exercises directly, independent of the
    stem-derivation logic above it (two DBs could theoretically be pointed
    at the same resolved dir some other way — a future change, a manual
    override — and this is the layer that still catches that).

    ABSENT-MARKER CASE (Ariadne's amendment, umwelt#533 issue comment,
    2026-09-16, correcting a hole in her own original spec): an absent
    marker has THREE possible causes this code cannot tell apart from the
    filesystem alone -- first boot after this fix ships, a genuinely new
    tier, or the marker having been REMOVED (e.g. a things/ dir restored
    from a pre-fix backup tarball -- ~/.umwelt/ on this host already holds
    two such tarballs, and restoring one is a normal operational move that
    carries the store's content but not the marker file). If absent always
    meant "proceed," the third cause silently disarms this whole guard by
    deleting one file -- exactly the silent-pass shape this function exists
    to prevent, reproduced one layer up.

    The fix: record establishment in the tier's OWN db
    (db.things_dir_established / db.mark_things_dir_established), not just
    on disk next to the things dir -- a restored tarball can't carry that
    row forward, only fail to have it. So absence is now decidable:
    never-established -> proceed (boot/new-tier case); established ->
    refuse loud (restore-lost-the-marker case), the same "something is
    wrong" treatment as a present-but-mismatched marker gets.

    NOT A BARE BOOLEAN (Borges's design comment on umwelt#533, 2026-09-16
    19:23:05Z, caught before this PR's colleague pass): the establishment
    row must record and be checked against WHICH things dir it was
    established for, not merely whether the db has established ANY dir,
    ever. A bare "established: true" is copy-transitive -- copying the db
    FILE itself (e.g. `cp alpha-world.db alpha-world-2.db` for a fresh dev
    tier) copies the establishment row along with it, byte for byte. The
    copy then boots at a NEW path, deriving a NEW, never-before-seen things
    dir (different stem -> different `things-<stem>`) whose marker is
    genuinely, legitimately absent -- but a boolean-only check would see
    "established: true" (inherited from the copy's former life) and refuse
    to ever boot the new tier at all. Comparing `established["things_dir"]`
    against the dir being resolved NOW (`str(d)`) discriminates the two
    cases the boolean collapses: same dir, marker gone -> refuse (restore);
    different dir, this db's row is simply stale -> proceed, and the write
    below re-points the row at the new dir (self-healing the copy's stale
    record for its own future boots).
    """
    marker = d / _THINGS_DIR_OWNER_MARKER
    owner_str = str(resolved_db)
    d_str = str(d)
    conn = sqlite3.connect(str(resolved_db))
    try:
        established = db.things_dir_established(conn)
        established_here = established is not None and established["things_dir"] == d_str
        if marker.exists():
            owner = marker.read_text().strip()
            if owner != owner_str:
                raise RuntimeError(
                    f"things dir {d} is already owned by a different database "
                    f"({owner!r}) than the one starting now ({owner_str!r}). "
                    "Refusing to silently re-share a things directory across "
                    "tiers -- see umwelt#533 for the incident this prevents. "
                    f"If this directory's ownership genuinely needs to change, "
                    f"remove {marker} manually first."
                )
            # Marker matches. Backfill/update the establishment row if it's
            # missing or stale for a DIFFERENT dir (e.g. this marker
            # predates the establishment table, a prior boot crashed
            # between the two writes, or this db was copied from a tier
            # whose row still names the OLD dir) -- harmless when already
            # correct, and it's what makes the absent-after-established
            # refusal below trustworthy going forward for THIS dir.
            if not established_here:
                db.mark_things_dir_established(conn, d_str)
                conn.commit()
            return
        # Marker absent.
        if established_here:
            raise RuntimeError(
                f"things dir {d} has no tier-identity marker, but this "
                f"database ({owner_str!r}) already recorded establishing "
                f"THIS dir at {established['established_at']}. A missing "
                f"marker after establishment means the things directory was "
                f"replaced out from under this database -- e.g. restored "
                f"from a pre-fix backup tarball, which carries the store's "
                f"content but not the marker file -- see umwelt#533. "
                f"Refusing to silently re-adopt an unmarked directory. If "
                f"this really is a deliberate restore, remove this "
                f"database's things_dir_establishment row (or move the "
                f"whole db aside) and reboot."
            )
        # Never established for THIS dir: first boot after this fix, a
        # genuinely new tier, or a copied db whose establishment row (if
        # any) names a different dir entirely. Establish both records
        # together -- this also overwrites a stale row from a copied db,
        # re-pointing it at the dir actually being established now.
        marker.write_text(owner_str)
        db.mark_things_dir_established(conn, d_str)
        conn.commit()
    finally:
        conn.close()


def _write_source(things_dir: Path, thing_id: str, source: dict[str, str]) -> None:
    base = (things_dir / thing_id / "source").resolve()
    base.mkdir(parents=True, exist_ok=True)
    for relpath, contents in source.items():
        dest = (base / relpath).resolve()
        if not str(dest).startswith(str(base) + os.sep):
            raise ValueError(f"source path escapes thing dir: {relpath!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(contents, encoding="utf-8")


def _record_use(conn, thing_id: str, agent: str, kind: str,
                 exit_code: int | None = None, timed_out: bool = False,
                 elapsed_s: float | None = None, error: str | None = None) -> None:
    """Record a use() event — observer, not gate. A telemetry write failure
    must NEVER fail the use() response, so this catches and logs to stderr
    rather than propagating (#54)."""
    try:
        db.record_use_event(conn, thing_id, agent, kind,
                            exit_code=exit_code, timed_out=timed_out,
                            elapsed_s=elapsed_s, error=error)
    except Exception as e:  # noqa: BLE001 - deliberately broad; observer, not gate
        print(f"[use-telemetry] failed to record use event for "
              f"{thing_id!r}: {e}", file=sys.stderr)


def _read_source(things_dir: Path, thing_id: str) -> dict[str, str]:
    base = things_dir / thing_id / "source"
    out: dict[str, str] = {}
    if not base.exists():
        return out
    for p in base.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(base))] = p.read_text(encoding="utf-8")
    return out


def _read_source_exact(things_dir: Path, thing_id: str) -> dict[str, str]:
    """Like `_read_source`, but newline-preserving: `newline=""` disables
    universal-newline translation, so `\r\n` and a lone `\r` come back as
    written. Used ONLY by `_source_differs` (umwelt#1014) — `_replace_source`
    writes via `write_text(contents, encoding="utf-8")` with no newline
    argument, which on POSIX leaves `\r\n` / `\r` verbatim, so the on-disk
    text is exactly what must be compared."""
    base = things_dir / thing_id / "source"
    out: dict[str, str] = {}
    if not base.exists():
        return out
    for p in base.rglob("*"):
        if p.is_file():
            with open(p, encoding="utf-8", newline="") as f:
                out[str(p.relative_to(base))] = f.read()
    return out


def _source_differs(things_dir: Path, thing_id: str,
                    new_source: dict[str, str]) -> bool:
    """True iff `new_source` is NOT identical to the thing's current on-disk
    source (umwelt#1014). "Identical" is byte-exact and newline-preserving:
    the same set of relpaths, each with the same UTF-8 text including every
    `\r\n` / `\r` exactly as `_replace_source` would write it (so a
    CRLF -> LF rewrite IS a difference and a CRLF resubmit is NOT). Anything
    else (a file added, removed, or changed) is a difference. Pure read.
    Empty directories are not tracked.
    """
    try:
        return _read_source_exact(things_dir, thing_id) != new_source
    except (OSError, ValueError):
        # Current tree unreadable / not UTF-8: it cannot be shown identical,
        # and the replace below would overwrite it anyway — so it IS a change
        # (and the pre-#1014 write must not start failing because of this read).
        return True


def _read_source_file(things_dir: Path, thing_id: str,
                       relpath: str) -> str | None:
    """Read ONE file from a thing's source tree, without reading or
    UTF-8-decoding any sibling file (umwelt #780 colleague review,
    Borges): `_read_source` reads and decodes EVERY file under a thing's
    source dir to answer what `_apply_effect_source_append` only needs
    as a single-file lookup — wasted work for a large tree, and a
    correctness risk that function doesn't need to take on: a non-UTF-8
    sibling file would raise from a read that never even touches the
    path the caller actually asked about.

    Same path-escape guard `_write_source`/`_append_source_file` use —
    needed here because `relpath` is CALLER-supplied, unlike
    `_read_source`'s own enumeration, which only ever sees paths it
    discovered on disk itself and so has nothing to police.

    Returns the file's text content, or `None` if it doesn't exist (or
    is a directory) — a cheap `.get()`-shaped "not present" answer
    without a whole-tree read to check membership against.
    """
    base = (things_dir / thing_id / "source").resolve()
    dest = (base / relpath).resolve()
    if not str(dest).startswith(str(base) + os.sep):
        raise ValueError(f"source path escapes thing dir: {relpath!r}")
    if not dest.is_file():
        return None
    return dest.read_text(encoding="utf-8")


def _replace_source(things_dir: Path, thing_id: str, source: dict[str, str]) -> None:
    """Replace a thing's source tree wholesale, ATOMICALLY (validate-before-destroy).

    Writes the new files to a staging dir first (same path-escape guard as
    _write_source); only once EVERY file has written successfully does it swap the
    staging tree in for the live one. A failure — a bad relpath, a disk error —
    leaves the existing source tree completely untouched and re-raises. This is the
    load-bearing safety property for update_thing: a botched update must never
    destroy the prior good content (the whole point of a recoverable fix).
    Full replace, not a merge — files absent from `source` are removed on swap.
    """
    thing_dir = things_dir / thing_id
    live = thing_dir / "source"
    staging = thing_dir / ".source.staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        staging_root = staging.resolve()
        for relpath, contents in source.items():
            dest = (staging / relpath).resolve()
            if not str(dest).startswith(str(staging_root) + os.sep):
                raise ValueError(f"source path escapes thing dir: {relpath!r}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(contents, encoding="utf-8")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)  # live source untouched
        raise
    # every write succeeded — now (and only now) swap the validated tree in.
    if live.exists():
        shutil.rmtree(live, ignore_errors=True)
    staging.rename(live)


def _append_source_file(things_dir: Path, thing_id: str, relpath: str,
                         new_content: str) -> None:
    """Atomically overwrite ONE file in a thing's source tree with
    `new_content` (umwelt #663) — the caller (`_apply_effect_source_append`)
    has already merged the existing content with the appended text; this
    helper's only job is writing that merged string to disk safely.

    Write-to-temp-then-`os.replace` on the SINGLE target path, never a
    full `_replace_source` over the thing's entire source dict — a full
    replace would rewrite every sibling file for what should be a
    one-file change, and risks clobbering a file the caller never meant
    to touch (the exact risk `_replace_source`'s own docstring accepts as
    the cost of ATOMIC MULTI-file replacement; that tradeoff has no
    reason to apply to a single-file append). `os.replace` is a rename on
    POSIX, so the visible file is either fully the old content or fully
    the new content, never a partial write.

    THAT bound is narrower than it may read: no-torn-file is NOT
    no-lost-write. This function only performs the final write of an
    already-merged string — it has no knowledge of, and cannot protect
    against, a race between the READ that produced `new_content` (in
    `_apply_effect_source_append`, below) and this write. See that
    caller's docstring for the actual concurrency bound of the primitive
    as a whole (umwelt #780 colleague review, Borges).

    Same path-escape guard `_write_source` uses (`str(dest).startswith(...)`)
    — replicated here rather than skipped just because this write touches
    only one file; the guard exists to police a caller-supplied relpath,
    and that risk doesn't shrink just because the blast radius of a
    successful escape would be smaller.
    """
    base = (things_dir / thing_id / "source").resolve()
    base.mkdir(parents=True, exist_ok=True)
    dest = (base / relpath).resolve()
    if not str(dest).startswith(str(base) + os.sep):
        raise ValueError(f"source path escapes thing dir: {relpath!r}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".tmp-{uuid.uuid4().hex}")
    tmp.write_text(new_content, encoding="utf-8")
    os.replace(tmp, dest)


class _ThingSpecError(Exception):
    """Raised by _create_thing_from_spec when a create_thing spec fails
    validation. Carries the HTTP status the caller should respond with, so
    both the create_thing route (agent-authored specs) and use()'s blueprint
    branch (engine-authored specs, substituted from a template) can convert
    it to a jsonify(...) response in their own local error-response shape."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _validate_blueprint_source(source: dict) -> None:
    """A blueprint's `source` MUST include a parseable blueprint.json holding
    the spec-template (umwelt #79). Validated at creation time — forging a
    malformed blueprint fails loudly right away, not silently at first use().
    """
    raw = source.get("blueprint.json")
    if raw is None:
        raise _ThingSpecError(
            "a blueprint's source must include blueprint.json", 422)
    try:
        template = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise _ThingSpecError(f"blueprint.json is not valid JSON: {e}", 422)
    if not isinstance(template, dict):
        raise _ThingSpecError("blueprint.json must be a JSON object", 422)
    missing = [k for k in ("name", "description", "kind") if k not in template]
    if missing:
        raise _ThingSpecError(
            f"blueprint.json is missing required keys: {missing}", 422)


# umwelt #107 slice A: a kinddef's `extends` chain is capped at 4 ancestor
# hops (creation-time 422 if a new kinddef's chain would exceed it, or
# cycles). _KIND_CHAIN_MAX_NODES bounds an act()-time chain WALK (the
# instance's own kind plus up to 4 ancestors = 5 nodes) — same bound,
# expressed as a node count instead of an edge count, so a chain that was
# valid at kinddef-creation time can never trip the act()-time re-walk.
_KIND_EXTENDS_MAX_HOPS = 4
_KIND_CHAIN_MAX_NODES = _KIND_EXTENDS_MAX_HOPS + 1

# The closed set of caller classes a kinddef action may declare (umwelt
# #107 slice A) — "named:<agent>" is a prefix match, not a literal member
# of this tuple. `granted` (umwelt #400) is the odd member out: unlike
# author/place_owner/anyone, it is NOT resolvable from `thing`+`agent`
# alone — it requires a DB lookup (does (thing.id, agent) hold a live
# thing_grants row? see db.has_grant) — because #400's whole point is a
# thing's DEFINITION (author-only) needs to be separable from a
# declared-caller-classed slice of its STATE, and "who currently has
# write access to this specific thing's state" is itself dynamic state,
# not something a kinddef author can bake into a fixed caller string at
# kinddef-creation time. See act()'s authorization loop for the DB-backed
# branch, and _apply_effect_grant_caller/_apply_effect_revoke_caller for
# the ONLY write path to thing_grants.
_KIND_CALLER_CLOSED_SET = ("author", "place_owner", "anyone", "granted")

# umwelt#300 S0(d): the literal cohort marker create_thing stamps onto
# every NEWLY forged thing (via _create_thing_from_spec — see that
# function's own comment for why engine-boot singleton paths deliberately
# don't pass it). A thing's things.update_policy_cohort column holds this
# string once it's eligible for the new kinddef-declared update_callers
# resolution (_resolve_update_callers below); NULL (every pre-existing
# thing, and every thing forged through a path that doesn't pass this)
# means "stay on today's exact author-only update_thing gate" — Mira's
# security acceptance criteria point 2, no silent retroactive flip.
_UPDATE_POLICY_COHORT_V1 = "declarative_v1"


def _validate_caller_list(callers, context: str) -> None:
    """Validate a `callers`-shaped list against `_KIND_CALLER_CLOSED_SET` +
    the `'named:<agent>'` prefix form — the ONE shared validator for both
    a per-action `callers` list (`_validate_kinddef_source`'s actions loop)
    and the new top-level `update_callers` list (umwelt#300 S0(d)), so the
    two can never validate against two different, silently-drifting closed
    sets. `context` is a human-readable description of where this list
    came from (e.g. `"kinddef.json actions['close'].callers"` or
    `"kinddef.json's update_callers"`), folded into the 422 message so the
    caller doesn't need to build its own wrapper string."""
    if not isinstance(callers, list) or not all(isinstance(c, str) for c in callers):
        raise _ThingSpecError(f"{context} must be an array of strings", 422)
    for c in callers:
        if c in _KIND_CALLER_CLOSED_SET:
            continue
        if c.startswith("named:") and len(c) > len("named:"):
            continue
        raise _ThingSpecError(
            f"{context} has an invalid entry {c!r} (must be one of "
            f"{_KIND_CALLER_CLOSED_SET} or 'named:<agent>')", 422)


# The closed set of effect types a kinddef action may declare (umwelt #107
# slice B). Validated at kinddef-creation time (422 on unknown type), same
# fail-loud posture as the caller closed set above. Effects run AFTER the
# action's transition+log commit, in declared order, and are best-effort —
# an effect failure never rolls back the action itself (see act()).
# `project_task_set` (umwelt #161 S2) is the third member — the write-path
# for the typed-thing project board, PROJECT-SCOPE BOUND at act() time (see
# _apply_effect_project_task_set). `agent_state_set` (umwelt #191) is the
# fourth member — the write-path for the generic per-(thing, agent) KV
# store (see _apply_effect_agent_state_set). `project_task_create` /
# `project_task_link` / `project_set` (umwelt #227 S4) round out the
# project-board's structure-EDITING surface — same board->project fail-close
# posture as project_task_set (see _apply_effect_project_task_create /
# _apply_effect_project_task_link / _apply_effect_project_set).
# `project_create` (umwelt #227 S4) is the one deliberately UNBOUND member —
# it mints a brand-new project (via the S3a atomic create_project_with_board
# path), so there is no existing project to scope it to; see
# _apply_effect_project_create's docstring for why that's still safe.
# `grant_caller` / `revoke_caller` (umwelt #400) are the write-path for
# thing_grants — the ONLY way a `granted` caller-class row is ever written
# or removed (see _apply_effect_grant_caller / _apply_effect_revoke_caller
# for the security-critical who-may-grant gate). `board_entry_add` /
# `board_entry_retire` (umwelt #400) are the charge-board's own
# structure-editing surface — a bounded array in the acting thing's OWN
# `metadata.entries`, the same "server-owned array with a cap" posture
# `metadata.action_log` already uses (see _ACTION_LOG_MAX_ENTRIES),
# deliberately NOT reusing the project_task_* family (a board entry has no
# per-agent assignment/owner/review-state lifecycle — see
# _apply_effect_board_entry_add's docstring).
# `action_log_rotate` (umwelt #736) archives a thing's OWN
# `metadata.action_log` to a fresh archive thing and resets it to a single
# marker entry — the primitive that relieves _ACTION_LOG_MAX_ENTRIES
# without forging a brand-new thing (which loses identity/grants/the
# clock). Deliberately a DECLARED EFFECT rather than a declarative flag on
# the action def (e.g. a hypothetical `bypass_log_cap: true`) — the cap
# guard in act() derives its exemption from THIS effect being present in
# an action's own `effects` list, which makes the exemption constitutive:
# an agent cannot claim it without also declaring the effect that
# actually performs the rotation (Borges's #736 security finding; see the
# guard's own comment in act()). `source_append` (umwelt #663) appends
# caller-supplied text to ONE existing file in a thing's `source` tree,
# atomically, without the read-full/hand-merge/write-full cycle
# `update_thing`'s full-replace `source` patch requires today (see
# _apply_effect_source_append's docstring). `board_entry_edit` (umwelt
# #836) is the charge-board's missing correction verb — edits ONE
# entry's `text` in place (keeping its `id`/`created_by`/`created_at`),
# rather than the retire+add workaround that mints a new id and loses the
# original entry's provenance (see _apply_effect_board_entry_edit's
# docstring).
_KIND_EFFECT_CLOSED_SET = ("move_to", "notify_author", "project_task_set",
                            "agent_state_set", "project_task_create",
                            "project_task_link", "project_set",
                            "project_create", "grant_caller",
                            "revoke_caller", "board_entry_add",
                            "board_entry_retire", "action_log_rotate",
                            "source_append", "board_entry_edit")

# Per-effect-type allowed key sets (umwelt #114 round 2) — an effect row is
# validated closed, not just its `type`: any key beyond the type's own
# declared set is rejected at kinddef-creation time (422), same fail-loud
# posture as the closed-set `type` check itself. Closes the key-smuggling
# surface where an extra, silently-ignored key could carry meaning a future
# reader mistakes for validated config.
_KIND_EFFECT_ALLOWED_KEYS = {
    "move_to": {"type", "container"},
    "notify_author": {"type", "wake", "comment_arg"},
    "project_task_set": {"type", "task_arg", "field", "value_arg", "value_actor"},
    "agent_state_set": {"type", "key", "value_arg", "value_actor"},
    "project_task_create": {"type", "label_arg", "description_arg", "parent_arg"},
    "project_task_link": {"type", "task_arg", "depends_on_arg", "remove"},
    "project_set": {"type", "field", "value_arg"},
    "project_create": {"type", "label_arg", "description_arg"},
    "grant_caller": {"type", "agent_arg", "can_grant_arg"},
    "revoke_caller": {"type", "agent_arg"},
    "board_entry_add": {"type", "text_arg"},
    "board_entry_retire": {"type", "entry_id_arg"},
    "board_entry_edit": {"type", "entry_id_arg", "text_arg"},
    "action_log_rotate": {"type"},
    "source_append": {"type", "path_arg", "text_arg"},
}

# board_entry_add/board_entry_retire's caps (umwelt #400) — same
# bounded-server-owned-array posture as _ACTION_LOG_MAX_ENTRIES: a
# generous ceiling, not a growth budget; a board that legitimately needs
# more entries should retire stale ones (board_entry_retire is a soft
# retire — see that effect's docstring — never a delete, so "aged out by
# date" is a read-side concern, not a prune step here), not have this cap
# raised silently. _BOARD_ENTRY_TEXT_MAX_LEN is the same
# author-controlled-text-needs-a-length-cap posture as
# _PARAM_DESCRIPTION_MAX_LEN, applied at act()-time (the text arrives via
# `args`, not a kinddef-time literal, so it can't be capped at
# kinddef-creation time the way _PARAM_DESCRIPTION_MAX_LEN's sibling is).
_BOARD_ENTRIES_MAX_COUNT = 100
# umwelt #836: raised 500 -> 2000. Measured daily-use friction (six
# add-entry refusals on the commander's own seat in one day, 503-599
# chars against the old 500 cap) showed the old magnitude was too tight
# for a self-contained, human-readable entry (Lei's 08-19 rule) — same
# posture as before (a length cap on caller-supplied text is still
# correct), just a more generous ceiling.
_BOARD_ENTRY_TEXT_MAX_LEN = 2000

# agent_state_set's per-(thing, agent) row bound (umwelt #191) — an agent
# can hold at most this many DISTINCT state keys on any one thing (see
# _apply_effect_agent_state_set). Same
# bounded-live posture + magnitude as _CHECKLIST_MAX_ITEMS just below: a
# generous ceiling for a legitimate small state bag (e.g. attached_graft,
# a couple of flags), not a growth budget — an agent that needs more than
# this is spraying rows, not storing state, and the write fails loud
# (422-shaped effect_failures reason) rather than silently dropping or
# evicting the oldest key.
_AGENT_STATE_MAX_KEYS_PER_THING = 30

# act()'s metadata.action_log cap — crude v0 guard against an unbounded
# per-thing log (umwelt #107 slice B rider). 200 is an arbitrary round
# number, not load-tested; the fix for a thing that legitimately needs more
# history is archiving it, not raising this cap silently — as of umwelt
# #836, act() performs that archiving ITSELF (via
# _apply_effect_action_log_rotate, same mechanism a manually-declared
# `rotate-log` action's own effect uses) the moment an action would push
# past this cap, rather than 422'ing and waiting on a separate operator
# step. The cap's magnitude is unchanged; only the "what happens at cap"
# behavior is.
_ACTION_LOG_MAX_ENTRIES = 200

# /umwelt/checklist's caps (umwelt #182 — the bed substrate). Unlike
# action_log's "retire the thing when full" posture, a checklist is
# bounded-live: `reset` IS the prune — an agent clears it every morning, so
# 30 items is a generous ceiling for a nightly ritual list, not a
# growth-then-archive budget. Fail-loud 422 when exceeded, same posture as
# _DESCRIPTION_MAX_LEN.
_CHECKLIST_MAX_ITEMS = 30
# umwelt #836: raised 200 -> 400. Measured daily-use friction (two
# checklist-edit refusals on the commander's own seat in one day, 211/346
# chars against the old 200 cap) — a Commander's Routine Board item is a
# one-line obligation plus its rule citation, and 200 truncated the
# citation. _CHECKLIST_MAX_ITEMS (item COUNT, not length) is untouched.
_CHECKLIST_ITEM_MAX_LEN = 400
# Closed op set (umwelt #182) — an unrecognized op is a loud 422, never a
# silent no-op.
_CHECKLIST_OPS = {"check", "uncheck", "reset", "add", "remove", "edit"}

# metadata.params (umwelt #396) — a thing's declared use()-param schema
# (generalizes #79 slice-1's blueprint-only {name, required, default} shape
# to executables/readables too, plus type/enum/description). Same
# "generous ceiling for legitimate use, fail loud when exceeded" posture as
# _CHECKLIST_MAX_ITEMS just above: 12 comfortably covers every existing
# blueprint (none declare more than 4 params as of this writing) with
# headroom. `description` is author-controlled text that will later render
# into a human's browser UI (the eventual web-client "?" tooltip), so it
# needs the same server-side LENGTH cap as _DESCRIPTION_MAX_LEN/
# _NAME_MAX_LEN regardless of what any future client does (Borges's
# explicit security-review requirement) — length only, no HTML
# stripping/escaping here; escaping-on-render is the eventual client's job,
# same posture already established for other author-controlled text (e.g.
# avatar-status activity_line).
_PARAMS_MAX_COUNT = 12
_PARAM_DESCRIPTION_MAX_LEN = 240
_PARAMS_ALLOWED_TYPES = {"string", "number", "boolean"}

# GET /umwelt/events pagination (umwelt #134 slice 1) — same "fixed v0
# tunable, not caller/env configurable" posture as CARRY_CAPACITY_DEFAULT /
# WAKE_RATE_BURST. Default keeps a normal Library-archive page small; max
# bounds how much any single caller-supplied `limit` can ask for.
_EVENTS_LIST_DEFAULT_LIMIT = 50
_EVENTS_LIST_MAX_LIMIT = 200

# /umwelt/crossings_here bounding (umwelt #157 — the 255KB stale-cursor
# flood: a first-visit cursor of None means "everything since the dawn of
# the world"). _CROSSINGS_DEFAULT_ADVANCE_LIMIT is the cap applied when
# `advance=true` but the caller sent no explicit `limit` — advance must
# never itself be able to flood, regardless of what the caller passes or
# omits. _CROSSINGS_MAX_LIMIT bounds any caller-supplied `limit`, advancing
# or not. When `advance` is false/omitted AND `limit` is falsy/absent, the
# route stays fully unbounded — back-compat for mud/TUI/world_cli/plugin,
# none of which send either field today.
_CROSSINGS_DEFAULT_ADVANCE_LIMIT = 50
_CROSSINGS_MAX_LIMIT = 100

# /umwelt/search_crossings bounding (umwelt#863, comms QoL D4). A separate
# constant family from _CROSSINGS_MAX_LIMIT above — this route's default/max
# are the issue's own numbers (20/50), not crossings_here's (50/100), and
# conflating the two would silently change one route's contract when the
# other's tuning changes.
_SEARCH_CROSSINGS_DEFAULT_LIMIT = 20
_SEARCH_CROSSINGS_MAX_LIMIT = 50
_SEARCH_CROSSINGS_MAX_QUERY_LEN = 200
_SEARCH_CROSSINGS_MAX_TERMS = 8

# The ten headline fields /umwelt/search_crossings may ever return —
# umwelt#863 invariant 1: never `body`, `body_ref`, `body_hash`, or
# `mentions`. Projected out of db._crossing_row's full dict by field name
# (never a slice/positional trick) so a future column appended to
# _CROSSING_COLS can't silently widen what this route leaks.
_SEARCH_CROSSINGS_HEADLINE_FIELDS = (
    "seq", "id", "sender", "destination", "to_agent", "sent_at",
    "title", "size", "reply_to", "thread_root",
)


# /umwelt/open_crossing + /umwelt/thread bounding (umwelt#862 D3). The uuid
# `id` is crossing_lookup's capability: returning it next to `seq` would make
# seq->id a public join (Kepler's ruling; Aleph's option b, rejected), so both
# seq-keyed readers project the headline allowlist MINUS `id`.
# The two readers also NULL `reply_to` / `thread_root` when the crossing the
# pointer names is missing or not `may_view_crossing` for the viewer (see
# _redact_seq_pointers), so a visible row cannot name a hidden crossing's seq.
# NOT hidden by that: seq density / gaps (crossings.seq is a gap-free rowid,
# so a gap between visible seqs still shows that SOME crossing exists there).
_SEQ_READER_HEADLINE_FIELDS = tuple(
    f for f in _SEARCH_CROSSINGS_HEADLINE_FIELDS if f != "id")
_OPEN_S_GUARD = 8000    # chars: a body over this is not returned unpaged
_OPEN_PAGE_MAX = 4000   # chars: hard cap on one page's slice
_THREAD_MAX = 200       # rows: /umwelt/thread returns at most this many
# A seq-typed payload field (seq, reply_to) must fit SQLite's INTEGER: a
# bigger Python int raises OverflowError at bind time (an HTTP 500).
_MAX_SEQ = 2**63 - 1
_MIN_SEQ = -(2**63)


def _int_param(raw, name: str, *, minimum: int | None = None,
               maximum: int | None = None, status: int = 400):
    """Convert a request-derived value with EXACTLY today's `int(raw)`
    semantics (ints, bools, floats, numeric strings incl. " 5" / "+5" /
    "1_0" / non-ASCII digits all still convert -- routes that worked before
    keep working), and turn what used to escape as a 500 (TypeError /
    ValueError / OverflowError from int(), or an OverflowError later at the
    sqlite bind) into a clean error. Returns (value, None) or
    (None, (jsonify(...), status)).

    `minimum` / `maximum` (inclusive) bound the converted value; leave one
    None where the route clamps instead of rejecting (a `limit` is clamped
    to a small cap, so a huge int is fine and never reaches sqlite). Pass
    _MIN_SEQ/_MAX_SEQ for a value that IS bound into sqlite -- a bigger
    Python int raises OverflowError at bind time.

    The error is one line, names the field and the JSON type, and never
    echoes the raw value (it may be 10,000 digits long)."""
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        value = None
    if value is None or (minimum is not None and value < minimum) or (
            maximum is not None and value > maximum):
        bound = (f" in [{minimum}, {maximum}]"
                 if minimum is not None and maximum is not None else "")
        return None, (jsonify({
            "error": (f"{name} must be an integer{bound} "
                      f"(got {_json_type_name(raw)})")}), status)
    return value, None


def _redact_seq_pointers(conn, viewer, row: dict, memo: dict,
                         place_cache: dict) -> dict:
    """Null `reply_to` / `thread_root` in a seq-reader row (in place) unless
    the crossing each names exists AND `visibility.may_view_crossing(viewer)`.
    A dangling pointer and a hidden one both become null (no oracle). `memo`
    (caller-owned, per request) maps target seq -> bool so each DISTINCT
    target costs one `get_crossing_by_seq` + one visibility check."""
    for field in ("reply_to", "thread_root"):
        target = row[field]
        if target is None:
            continue
        if target not in memo:
            t = db.get_crossing_by_seq(conn, target)
            memo[target] = t is not None and visibility.may_view_crossing(
                conn, viewer, t, place_cache)
        if not memo[target]:
            row[field] = None
    return row


def _search_crossings_headline(c: dict) -> dict:
    """Project one full db._crossing_row dict down to the headline shape
    /umwelt/search_crossings returns (umwelt#863 invariant 1)."""
    return {k: c[k] for k in _SEARCH_CROSSINGS_HEADLINE_FIELDS}


# --- citable event records (umwelt #134 slice 1) ----------------------------
#
# docs/design/cite-the-world.md §3: `record` mints a SNAPSHOT copy of a
# crossing/use_events/action_log entry into the append-only `events` table.
# These module-level helpers do the per-kind resolution + canonical
# serialization; the /umwelt/record route (in create_app below) is thin —
# it validates the request shape, calls _resolve_record_snapshot, and hands
# the result to db.mint_event.

def _canonical_body_json(payload: dict) -> str:
    """Canonical serialization for a use/act event's snapshot `body`
    (§3.2 design notes + §4 requirement 2 — the verbatim-quote contract):
    ``json.dumps(payload, sort_keys=True, separators=(',', ':'))``, where
    `payload` always carries a `"format": 1` key. This is the EXACT string
    an evidence-consumer's verbatim-quote check runs a substring match
    against — the versioned `format` key is what keeps that stable across
    any future engine change to this function's field set.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The kinds `POST /umwelt/record` accepts. Order is user-visible (the 422 text
# lists them in this order). Every member needs a read-gate decision, pinned in
# tests/test_record_read_gate.py (GATED | EXEMPT): umwelt #1001.
_RECORD_KINDS = ("crossing", "use", "act", "advance_ref", "update")


class _RecordError(Exception):
    """Carries a (message, http_status) pair up to the /umwelt/record route
    — same posture as _ThingSpecError for the blueprint/kinddef resolution
    helpers above."""

    def __init__(self, message: str, status: int, refusal=None):
        super().__init__(message)
        self.message = message
        self.status = status
        # umwelt#982: a ready-to-return `(response, status)` from the thing
        # read gate (`_thing_view_refusal`), so the route answers with the
        # SAME informational body every gated route gives, not a bare error.
        self.refusal = refusal


def _resolve_record_snapshot(conn, db_path: str, event_kind: str, ref: str,
                             viewer: str | None = None) -> dict:
    """Resolve `{event_kind, ref}` into the fields the /umwelt/record route
    needs to call db.mint_event: source_ref (the NORMALIZED ref string used
    for the UNIQUE(event_kind, source_ref) idempotency key), actor,
    place_id, occurred_at, body, body_hash, attesting_authority.

    Raises _RecordError for any resolution failure (missing underlying
    entry, malformed ref, a CAS-backed crossing's blob missing at mint
    time) — the caller catches it once and turns it into the matching JSON
    error response.

    `place_id` is None ("placeless") for BOTH 'use' and 'act' kinds:
    neither use_events nor a kind's action_log entry captures a location at
    the time of the underlying action, so filling it from the thing's
    CURRENT place_id would inject present-tense state into what is
    supposed to be a mint-time-frozen snapshot of a past fact. Only
    'crossing' has a genuine at-the-time location (its `destination`).
    """
    if event_kind == "crossing":
        crossing = db.get_crossing(conn, ref)
        if crossing is None:
            raise _RecordError(f"crossing {ref!r} not found", 404)
        if not crossing.get("received_at"):
            # crossings.received_at is nullable in schema, but every
            # creation path stamps it — this guard turns that implicit
            # cross-file invariant into a loud, structured failure instead
            # of inserting a NULL into events.occurred_at NOT NULL and
            # surfacing as a raw 500 (reviewer round-1).
            raise _RecordError(
                f"crossing {ref!r} has no received_at — cannot mint an "
                "event without an engine-stamped occurrence time", 422)
        if crossing["body"] is not None:
            body = crossing["body"]
        elif crossing["body_ref"]:
            content = cas.retrieve(conn, db_path, crossing["body_ref"])
            if content is None:
                raise _RecordError(
                    f"crossing {ref!r}'s body_ref {crossing['body_ref']!r} "
                    "is missing from the CAS — cannot mint a body-less event",
                    422,
                )
            try:
                body = content.decode("utf-8")
            except UnicodeDecodeError:
                raise _RecordError(
                    f"crossing {ref!r}'s CAS blob is not valid utf-8 text — "
                    "cannot snapshot it into events.body",
                    422,
                )
        else:
            # A pure notification crossing (no body, no body_ref — e.g. "the
            # Square gong rang"). It's still a real, world-attested event;
            # §3.1 doesn't special-case this, so snapshot an empty body
            # rather than refusing to mint.
            body = ""
        return {
            "source_ref": ref,
            "actor": crossing["sender"],
            "place_id": crossing["destination"],
            # received_at (engine-stamped) is the trustworthy world clock —
            # sent_at is caller-supplied/display-only (see the crossings
            # table's own schema comment in db.py). Guarded above: the
            # column is nullable in schema but every creation path stamps
            # it; the guard makes that cross-file invariant explicit
            # (reviewer round-1) instead of silently relying on it.
            "occurred_at": crossing["received_at"],
            "body": body,
            "body_hash": _sha256_hex(body),
            "attesting_authority": crossing["attesting_authority"],
        }

    if event_kind == "use":
        try:
            use_id = int(ref)
        except (TypeError, ValueError):
            raise _RecordError(
                "ref must be an integer use_events id for kind='use'", 422,
            )
        if not _MIN_SEQ <= use_id <= _MAX_SEQ:
            # No such id can exist; a bigger int overflows the sqlite bind.
            raise _RecordError(
                "ref must be an integer use_events id for kind='use'", 422,
            )
        use_event = db.get_use_event(conn, use_id)
        if use_event is None:
            raise _RecordError(f"use event {ref!r} not found", 404)
        # umwelt#1001: the thing read gate, on the RESOLVED caller, BEFORE the
        # payload is built. A use event's `error` can quote the caller's raw
        # arg and its `thing_id` is the held thing's UUID; both would land in
        # a PUBLIC event. Keyed on the thing (holder walk), never on the row's
        # `agent` column.
        _record_thing_gate(conn, viewer, use_event["thing_id"],
                           f"the thing behind use event {ref!r}")
        payload = {
            "format": 1,
            "id": use_event["id"],
            "thing_id": use_event["thing_id"],
            "agent": use_event["agent"],
            "used_at": use_event["used_at"],
            "kind": use_event["kind"],
            "exit_code": use_event["exit_code"],
            "timed_out": use_event["timed_out"],
            "elapsed_s": use_event["elapsed_s"],
            "error": use_event["error"],
        }
        body = _canonical_body_json(payload)
        return {
            "source_ref": str(use_id),
            "actor": use_event["agent"],
            "place_id": None,
            "occurred_at": use_event["used_at"],
            "body": body,
            "body_hash": _sha256_hex(body),
            "attesting_authority": "world",
        }

    if event_kind == "act":
        thing_id, _sep, index_raw = ref.rpartition(":")
        if not thing_id or not index_raw.isdigit():
            raise _RecordError(
                "ref must be '<thing_id>:<action_log_index>' for kind='act'",
                422,
            )
        try:
            index = int(index_raw)
        except ValueError:
            # str.isdigit() accepts characters int() rejects (e.g. "²"), and
            # int() refuses a digit string past the interpreter's length cap.
            raise _RecordError(
                "ref must be '<thing_id>:<action_log_index>' for kind='act'",
                422,
            )
        thing = db.get_thing(conn, thing_id)
        if thing is None:
            raise _RecordError(f"thing {thing_id!r} not found", 404)
        # umwelt#982: the thing read gate, on the RESOLVED actor -- BEFORE the
        # log index is looked at (a non-enterer learns neither the entry nor
        # how many there are) and so before anything is minted. This branch
        # copies a thing's action_log entry (args = e.g. a proposal comment's
        # content) into a PUBLIC event, so it is a read of the thing.
        refusal = _thing_view_refusal(conn, viewer, thing, thing_id)
        if refusal is not None:
            raise _RecordError(f"thing {thing_id!r} is not readable by you",
                               refusal[1], refusal=refusal)
        action_log = thing["metadata"].get("action_log") or []
        if index < 0 or index >= len(action_log):
            raise _RecordError(
                f"no action_log entry at index {index} on {thing_id!r}", 404,
            )
        log_entry = action_log[index]
        payload = {"format": 1, "thing_id": thing_id, "action_log_index": index}
        payload.update(log_entry)
        body = _canonical_body_json(payload)
        return {
            "source_ref": f"{thing_id}:{index}",
            "actor": log_entry.get("by", ""),
            "place_id": None,
            "occurred_at": log_entry.get("at", ""),
            "body": body,
            "body_hash": _sha256_hex(body),
            "attesting_authority": "world",
        }

    if event_kind == "advance_ref":
        # umwelt #186: resolution logic lives in db.py (db.advance_capsule_ref
        # is the mutator; db.resolve_advance_ref_event is the read-side
        # lookup for THIS mint path) — this is a thin kind-dispatch entry,
        # same posture as the other three branches above.
        # umwelt#1001: the event body carries the capsule's UUID and its CAS
        # refs, and the CAS is public-by-hash, so the event would hand a
        # non-enterer the capsule's bytes. Gate on the capsule thing first.
        advance = db.get_graft_advance(conn, ref)
        if advance is not None:
            _record_thing_gate(conn, viewer, advance["capsule_id"],
                               f"the capsule behind advance {ref!r}")
        snapshot = db.resolve_advance_ref_event(conn, ref)
        if snapshot is None:
            raise _RecordError(f"advance {ref!r} not found", 404)
        return snapshot

    if event_kind == "update":
        # world-content-timestamps.md slice 2: resolution logic lives in
        # db.py (db.update_thing/db.update_place are the mutators — they
        # write content_updates, never mint_event; db.resolve_content_update
        # _event is the read-side lookup for THIS mint path) — thin
        # kind-dispatch entry, same posture as the advance_ref branch above.
        # umwelt#1001: the event body names the subject's UUID, which of its
        # columns changed, and when -- an activity trace of a private
        # thing/place. Gate on the subject: a thing by the thing read gate, a
        # place by the place-interior predicate.
        update = db.get_content_update(conn, ref)
        if update is not None:
            subject = f"the subject of update {ref!r}"
            if update["subject_kind"] == "place":
                _record_place_gate(conn, viewer, update["subject_id"], subject)
            else:
                _record_thing_gate(conn, viewer, update["subject_id"], subject)
        snapshot = db.resolve_content_update_event(conn, ref)
        if snapshot is None:
            raise _RecordError(f"update {ref!r} not found", 404)
        return snapshot

    raise _RecordError(f"unknown kind {event_kind!r}", 422)


def _validate_kinddef_source(source: dict) -> dict:
    """A kinddef's `source` MUST include a parseable kinddef.json describing
    the kind's field schema + declarative action table (umwelt #107 slice A,
    mirrors _validate_blueprint_source's posture: validated at creation time,
    fails loudly right away rather than silently at first act()).

    Validates ONLY the kinddef.json's own shape (extends type, fields shape,
    actions shape/caller-set). It does NOT check that `extends` points at an
    existing live kinddef, or walk the depth/cycle bound — those need
    `conn`+`things_dir` and live in `_validate_kind_extends` below, called
    separately by `_create_thing_from_spec`.

    Returns the parsed dict on success (so the caller doesn't have to
    re-parse the JSON a second time).
    """
    raw = source.get("kinddef.json")
    if raw is None:
        raise _ThingSpecError(
            "a kinddef's source must include kinddef.json", 422)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise _ThingSpecError(f"kinddef.json is not valid JSON: {e}", 422)
    if not isinstance(parsed, dict):
        raise _ThingSpecError("kinddef.json must be a JSON object", 422)
    missing = [k for k in ("extends", "fields", "actions") if k not in parsed]
    if missing:
        raise _ThingSpecError(
            f"kinddef.json is missing required keys: {missing}", 422)

    extends = parsed["extends"]
    if extends is not None and not isinstance(extends, str):
        raise _ThingSpecError(
            "kinddef.json's extends must be a thing_id string or null", 422)

    # `state_keys` (umwelt #191): OPTIONAL top-level array of the state keys
    # this kind's actions are allowed to write via an `agent_state_set`
    # effect — the #114 key-smuggling closure applied to state, not just
    # effect rows: an `agent_state_set` effect's `key` must be a MEMBER of
    # this declared set (checked in the actions loop below), so a kinddef
    # can never write a key it didn't own up to at creation time. Absent ==
    # empty set (a kinddef with no state_keys can't use agent_state_set at
    # all — every `key` value fails the membership check).
    state_keys = parsed.get("state_keys", [])
    if not isinstance(state_keys, list) or not all(
            isinstance(k, str) and k.strip() for k in state_keys):
        raise _ThingSpecError(
            "kinddef.json's state_keys must be an array of non-empty "
            "strings", 422)

    # `update_callers` (umwelt#300 S0(d), declarative kinddef-level
    # update_thing permission): OPTIONAL top-level array, same closed
    # caller-class vocabulary/shape as a per-action `callers` list —
    # _validate_caller_list is the ONE shared validator both this and the
    # actions loop below call, so the two check the exact same closed set
    # and can never drift (this codebase's explicit umwelt#515/#518
    # no-second-copy posture). Absent key is valid — means "this kinddef
    # doesn't declare a policy here, check the parent" (see
    # _resolve_update_callers). An explicit empty list is ALSO valid — it
    # means "nobody, not even the author," at this link of the chain; do
    # NOT special-case it as an error.
    if "update_callers" in parsed:
        _validate_caller_list(parsed["update_callers"], "kinddef.json's update_callers")

    fields = parsed["fields"]
    if not isinstance(fields, dict):
        raise _ThingSpecError("kinddef.json's fields must be an object", 422)
    for fname, fspec in fields.items():
        if not isinstance(fspec, dict) or not isinstance(fspec.get("required"), bool):
            raise _ThingSpecError(
                f"kinddef.json fields[{fname!r}] must be an object with a "
                "boolean 'required'", 422)

    actions = parsed["actions"]
    if not isinstance(actions, dict):
        raise _ThingSpecError("kinddef.json's actions must be an object", 422)
    for aname, aspec in actions.items():
        if not isinstance(aspec, dict):
            raise _ThingSpecError(
                f"kinddef.json actions[{aname!r}] must be an object", 422)
        callers = aspec.get("callers")
        _validate_caller_list(callers, f"kinddef.json actions[{aname!r}].callers")
        requires = aspec.get("requires", [])
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            raise _ThingSpecError(
                f"kinddef.json actions[{aname!r}].requires must be an array "
                "of strings", 422)
        from_statuses = aspec.get("from", [])
        if not isinstance(from_statuses, list) or not all(isinstance(s, str) for s in from_statuses):
            raise _ThingSpecError(
                f"kinddef.json actions[{aname!r}].from must be an array of "
                "strings", 422)
        to = aspec.get("to")
        if to is not None and not isinstance(to, str):
            raise _ThingSpecError(
                f"kinddef.json actions[{aname!r}].to must be a string or "
                "null", 422)
        effects = aspec.get("effects", [])
        if not isinstance(effects, list):
            raise _ThingSpecError(
                f"kinddef.json actions[{aname!r}].effects must be an array "
                "of objects", 422)
        for i, effect in enumerate(effects):
            if not isinstance(effect, dict):
                raise _ThingSpecError(
                    f"kinddef.json actions[{aname!r}].effects[{i}] must be "
                    "an object", 422)
            etype = effect.get("type")
            if etype not in _KIND_EFFECT_CLOSED_SET:
                raise _ThingSpecError(
                    f"kinddef.json actions[{aname!r}].effects[{i}].type "
                    f"{etype!r} is not one of {_KIND_EFFECT_CLOSED_SET}", 422)
            allowed_keys = _KIND_EFFECT_ALLOWED_KEYS[etype]
            unknown_keys = sorted(set(effect.keys()) - allowed_keys)
            if unknown_keys:
                raise _ThingSpecError(
                    f"kinddef.json actions[{aname!r}].effects[{i}] "
                    f"({etype}) has unknown key(s) {unknown_keys} "
                    f"(allowed: {sorted(allowed_keys)})", 422)
            if etype == "move_to":
                container = effect.get("container")
                if not isinstance(container, str) or not container.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(move_to) requires a non-empty string 'container'",
                        422)
            elif etype == "notify_author":
                wake = effect.get("wake", False)
                if not isinstance(wake, bool):
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(notify_author).wake must be a bool", 422)
                # #569: comment_arg declares which caller-supplied args key the
                # effect reads as its comment (default "comment" for back-compat
                # with declarations made before #569 — see _apply_effect_notify_author
                # and the consumable-set scan in act()). Optional: when absent the
                # default "comment" is used, so existing kinddefs keep working.
                comment_arg = effect.get("comment_arg")
                if comment_arg is not None and (
                        not isinstance(comment_arg, str)
                        or not comment_arg.strip()):
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(notify_author).comment_arg must be a non-empty "
                        "string when present", 422)
            elif etype == "project_task_set":
                task_arg = effect.get("task_arg")
                if not isinstance(task_arg, str) or not task_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(project_task_set) requires a non-empty string "
                        "'task_arg'", 422)
                field = effect.get("field")
                if field not in db._MUTABLE_TASK_COLS:
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        f"(project_task_set).field {field!r} is not one of "
                        f"{db._MUTABLE_TASK_COLS}", 422)
                has_value_arg = "value_arg" in effect
                has_value_actor = "value_actor" in effect
                if has_value_arg == has_value_actor:
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(project_task_set) must have EXACTLY ONE of "
                        "'value_arg' / 'value_actor'", 422)
                if has_value_arg:
                    value_arg = effect.get("value_arg")
                    if not isinstance(value_arg, str) or not value_arg.strip():
                        raise _ThingSpecError(
                            f"kinddef.json actions[{aname!r}].effects[{i}] "
                            "(project_task_set) requires a non-empty string "
                            "'value_arg'", 422)
                if has_value_actor:
                    if effect.get("value_actor") is not True:
                        raise _ThingSpecError(
                            f"kinddef.json actions[{aname!r}].effects[{i}] "
                            "(project_task_set).value_actor must be "
                            "literally True", 422)
            elif etype == "agent_state_set":
                # umwelt #191: mirrors project_task_set's value_arg /
                # value_actor shape above, plus the state-key closure — a
                # `key` not declared in the kinddef's own top-level
                # `state_keys` (parsed above) is rejected right here, same
                # fail-loud-at-creation posture as the #114 key-smuggling
                # closure on the effect row itself.
                key = effect.get("key")
                if not isinstance(key, str) or not key.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(agent_state_set) requires a non-empty string "
                        "'key'", 422)
                if key not in state_keys:
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        f"(agent_state_set).key {key!r} is not declared in "
                        f"kinddef.json's top-level state_keys {state_keys}",
                        422)
                has_value_arg = "value_arg" in effect
                has_value_actor = "value_actor" in effect
                if has_value_arg == has_value_actor:
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(agent_state_set) must have EXACTLY ONE of "
                        "'value_arg' / 'value_actor'", 422)
                if has_value_arg:
                    value_arg = effect.get("value_arg")
                    if not isinstance(value_arg, str) or not value_arg.strip():
                        raise _ThingSpecError(
                            f"kinddef.json actions[{aname!r}].effects[{i}] "
                            "(agent_state_set) requires a non-empty string "
                            "'value_arg'", 422)
                if has_value_actor:
                    if effect.get("value_actor") is not True:
                        raise _ThingSpecError(
                            f"kinddef.json actions[{aname!r}].effects[{i}] "
                            "(agent_state_set).value_actor must be "
                            "literally True", 422)
            elif etype == "project_task_create":
                label_arg = effect.get("label_arg")
                if not isinstance(label_arg, str) or not label_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(project_task_create) requires a non-empty string "
                        "'label_arg'", 422)
                for opt_key in ("description_arg", "parent_arg"):
                    if opt_key in effect:
                        val = effect.get(opt_key)
                        if not isinstance(val, str) or not val.strip():
                            raise _ThingSpecError(
                                f"kinddef.json actions[{aname!r}].effects[{i}] "
                                f"(project_task_create).{opt_key}, if given, "
                                "must be a non-empty string", 422)
            elif etype == "project_task_link":
                for req_key in ("task_arg", "depends_on_arg"):
                    val = effect.get(req_key)
                    if not isinstance(val, str) or not val.strip():
                        raise _ThingSpecError(
                            f"kinddef.json actions[{aname!r}].effects[{i}] "
                            f"(project_task_link) requires a non-empty "
                            f"string {req_key!r}", 422)
                if effect.get("task_arg") == effect.get("depends_on_arg"):
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(project_task_link).task_arg and depends_on_arg "
                        "must name different args", 422)
                if not isinstance(effect.get("remove"), bool):
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(project_task_link).remove must be a literal bool",
                        422)
            elif etype == "project_set":
                field = effect.get("field")
                if field not in db._MUTABLE_PROJECT_COLS:
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        f"(project_set).field {field!r} is not one of "
                        f"{db._MUTABLE_PROJECT_COLS}", 422)
                value_arg = effect.get("value_arg")
                if not isinstance(value_arg, str) or not value_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(project_set) requires a non-empty string "
                        "'value_arg'", 422)
            elif etype == "project_create":
                label_arg = effect.get("label_arg")
                if not isinstance(label_arg, str) or not label_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(project_create) requires a non-empty string "
                        "'label_arg'", 422)
                if "description_arg" in effect:
                    val = effect.get("description_arg")
                    if not isinstance(val, str) or not val.strip():
                        raise _ThingSpecError(
                            f"kinddef.json actions[{aname!r}].effects[{i}] "
                            "(project_create).description_arg, if given, "
                            "must be a non-empty string", 422)
            elif etype == "grant_caller":
                # umwelt #400 (round 3, Borges security fix): agent_arg
                # shape mirrors project_task_set's value_arg/task_arg
                # validation above — a non-empty string naming the `args`
                # key the target agent arrives in. can_grant_arg is the
                # SAME "value comes from args, not a kinddef-time literal"
                # shape project_task_set's value_arg uses — OPTIONAL at the
                # kinddef level (a kinddef author who never wants
                # delegation grantable through this action simply omits
                # it), naming the `args` key the CALLER (the granter, at
                # call time) supplies a bool in to decide whether THIS
                # grant confers delegation rights onward. Borges's ruling:
                # "a literal in the source is explicit to the implementer,
                # not to the granter" — a can_grant literal silently
                # conferred delegation on every grant with no way for the
                # granter to choose content-access-only. The effect
                # implementation (_apply_effect_grant_caller) defaults to
                # can_grant=False whenever this key is absent from the
                # kinddef OR the caller omits/mis-types the named arg.
                agent_arg = effect.get("agent_arg")
                if not isinstance(agent_arg, str) or not agent_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(grant_caller) requires a non-empty string "
                        "'agent_arg'", 422)
                if "can_grant_arg" in effect:
                    can_grant_arg = effect.get("can_grant_arg")
                    if not isinstance(can_grant_arg, str) or not can_grant_arg.strip():
                        raise _ThingSpecError(
                            f"kinddef.json actions[{aname!r}].effects[{i}] "
                            "(grant_caller).can_grant_arg, if given, must "
                            "be a non-empty string", 422)
            elif etype == "revoke_caller":
                agent_arg = effect.get("agent_arg")
                if not isinstance(agent_arg, str) or not agent_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(revoke_caller) requires a non-empty string "
                        "'agent_arg'", 422)
            elif etype == "board_entry_add":
                text_arg = effect.get("text_arg")
                if not isinstance(text_arg, str) or not text_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(board_entry_add) requires a non-empty string "
                        "'text_arg'", 422)
            elif etype == "board_entry_retire":
                entry_id_arg = effect.get("entry_id_arg")
                if not isinstance(entry_id_arg, str) or not entry_id_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(board_entry_retire) requires a non-empty string "
                        "'entry_id_arg'", 422)
            elif etype == "board_entry_edit":
                entry_id_arg = effect.get("entry_id_arg")
                if not isinstance(entry_id_arg, str) or not entry_id_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(board_entry_edit) requires a non-empty string "
                        "'entry_id_arg'", 422)
                text_arg = effect.get("text_arg")
                if not isinstance(text_arg, str) or not text_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(board_entry_edit) requires a non-empty string "
                        "'text_arg'", 422)
            # action_log_rotate (umwelt #736) has no args beyond `type` —
            # it archives everything unconditionally — so there is nothing
            # further to validate here; the closed-set + allowed-keys
            # checks above are the whole of its creation-time validation.
            elif etype == "source_append":
                path_arg = effect.get("path_arg")
                if not isinstance(path_arg, str) or not path_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(source_append) requires a non-empty string "
                        "'path_arg'", 422)
                text_arg = effect.get("text_arg")
                if not isinstance(text_arg, str) or not text_arg.strip():
                    raise _ThingSpecError(
                        f"kinddef.json actions[{aname!r}].effects[{i}] "
                        "(source_append) requires a non-empty string "
                        "'text_arg'", 422)
    return parsed


def _load_kinddef(conn, things_dir: Path, kind_id: str) -> dict:
    """Load + parse a live kinddef thing's kinddef.json off disk (its
    `source`, same on-disk layout every kind uses — see _read_source).
    Raises _ThingSpecError(..., 422) if `kind_id` isn't a live kind='kinddef'
    thing, or its kinddef.json is missing/malformed (defense in depth: a
    kinddef that passed _validate_kinddef_source at creation should always
    still parse, but this is the single read site every chain-walk shares,
    so it re-checks rather than trusting the invariant blindly)."""
    thing = db.get_thing(conn, kind_id)
    if thing is None or thing["kind"] != "kinddef" or thing["status"] != "live":
        raise _ThingSpecError(f"{kind_id!r} is not a live kinddef thing", 422)
    source = _read_source(things_dir, kind_id)
    raw = source.get("kinddef.json")
    if raw is None:
        raise _ThingSpecError(f"kinddef {kind_id!r} is missing kinddef.json", 422)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise _ThingSpecError(
            f"kinddef.json for {kind_id!r} is not valid JSON: {e}", 422)
    if not isinstance(parsed, dict):
        raise _ThingSpecError(
            f"kinddef.json for {kind_id!r} must be a JSON object", 422)
    return parsed


def _kinddef_file_drift(conn, things_dir: Path) -> dict:
    """Boot-time drift check (umwelt #991): every LIVE kind='kinddef' thing
    must still load via `_load_kinddef` (the very same rules a real load
    applies). Returns {"checked": N, "missing": [{"id", "name", "reason"}]}.

    NEVER raises (never-crash-boot discipline, same as
    `_resolve_world_version`): a `_ThingSpecError` is recorded with its
    message; any other exception is recorded as `unexpected: <type>: <msg>`,
    and a failure of the enumeration query itself is reported as one
    `(enumeration)` entry rather than an empty, clean-looking result -- a
    check that cannot run must not read as a world with no drift."""
    missing: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT id, name FROM things WHERE kind = 'kinddef' "
            "AND status = 'live' ORDER BY id").fetchall()
    except Exception as e:
        return {"checked": 0, "missing": [{
            "id": "(enumeration)", "name": "",
            "reason": f"unexpected: {type(e).__name__}: {e}"}]}
    for kind_id, name in rows:
        try:
            _load_kinddef(conn, things_dir, kind_id)
        except _ThingSpecError as e:
            missing.append({"id": kind_id, "name": name, "reason": str(e)})
        except Exception as e:
            missing.append({"id": kind_id, "name": name,
                            "reason": f"unexpected: {type(e).__name__}: {e}"})
    return {"checked": len(rows), "missing": missing}


def _validate_kind_extends(conn, things_dir: Path, extends_id: str | None,
                            *, exclude: str | None = None) -> None:
    """Validate a candidate `extends` pointer for a NEW kinddef thing (not
    yet created — so the walk starts AT extends_id, not at the new kind
    itself): `extends_id` must be a live kind='kinddef' thing, and the
    resulting chain (new kind -> extends_id -> extends_id's own extends ->
    ...) must terminate (reach extends=null) within _KIND_EXTENDS_MAX_HOPS
    hops, with no cycles. `extends_id=None` is always valid (a root kind).

    `exclude` (umwelt #109 round-2 suggestion #4): when re-validating an
    EXISTING kinddef's `extends` on an update_thing source patch, the
    walk must also catch a chain that loops back to that kinddef's OWN id
    — the create-time call above never needs this (a not-yet-created kind
    can't already appear in the chain it's pointing into), but a patched
    kind already exists, so pre-seeding `visited` with its own id closes
    that self-cycle gap."""
    if extends_id is None:
        return
    visited: set[str] = set()
    if exclude is not None:
        visited.add(exclude)
    current: str | None = extends_id
    hops = 0
    while current is not None:
        if current in visited:
            raise _ThingSpecError(
                f"kinddef extends chain contains a cycle at {current!r}", 422)
        visited.add(current)
        hops += 1
        if hops > _KIND_EXTENDS_MAX_HOPS:
            raise _ThingSpecError(
                "kinddef extends chain exceeds max depth "
                f"({_KIND_EXTENDS_MAX_HOPS} hops)", 422)
        parsed = _load_kinddef(conn, things_dir, current)
        current = parsed.get("extends")


def _resolve_kind_chain(conn, things_dir: Path, kind_id: str) -> list[tuple[str, dict]]:
    """Walk an INSTANCE's kind chain, child-first: [(kind_id, parsed),
    (parent_id, parsed), ...] ending at the root kind (extends=null).
    Action resolution and required-field validation both use this — nearest
    (first) definition of an action/field name wins (override semantics,
    umwelt #107 slice A). Raises _ThingSpecError(..., 422) if `kind_id`
    isn't a live kinddef thing, or the chain exceeds _KIND_CHAIN_MAX_NODES /
    cycles — same bound _validate_kind_extends enforces at kinddef-creation
    time, re-checked here as defense in depth (a chain that was valid when
    minted stays valid; this just doesn't trust that invariant blindly)."""
    chain: list[tuple[str, dict]] = []
    visited: set[str] = set()
    current: str | None = kind_id
    nodes = 0
    while current is not None:
        if current in visited:
            raise _ThingSpecError(
                f"kind chain contains a cycle at {current!r}", 422)
        visited.add(current)
        nodes += 1
        if nodes > _KIND_CHAIN_MAX_NODES:
            raise _ThingSpecError(
                f"kind chain for {kind_id!r} exceeds max depth "
                f"({_KIND_CHAIN_MAX_NODES} nodes)", 422)
        parsed = _load_kinddef(conn, things_dir, current)
        chain.append((current, parsed))
        current = parsed.get("extends")
    return chain


def _thing_view_refusal(conn, viewer: str | None, thing: dict, thing_id: str,
                        subject: str | None = None):
    """The thing read gate (umwelt#982, privacy design rule 3): None when
    `viewer` may read `thing` by id, else the ready-to-return refusal
    `(response, status)` -- built HERE, once, from `visibility.may_view_thing`'s
    reason, so every gated route answers identically. `viewer` is the
    RESOLVED actor (`resolve_actor`), never a raw body field.

    Refusals are informational, never a bare reject (Lei, 2026-10-04):
      403 in_private_place -- the place's PUBLIC projection (id, label, kind,
          owner, access_granted: already public by the #538 rule) so the
          caller knows whose place it is and whom to ask. No thing content.
      403 binned           -- when it was binned and who can restore it.
      404 not found        -- a cycle / missing holder / missing place: there
          is nothing true and useful to say, and the gate fails closed.
    The error text names the id the caller supplied, never the thing's name
    or description (those are content).

    `subject` (umwelt#1001): the phrase the error text uses in place of
    `thing <id>`. A `record` of a use / advance / update row reaches the thing
    through a ledger row the caller named, so the thing's own id is DERIVED
    server-side, not supplied -- the refusal must not echo it. Default (None)
    keeps the supplied-id wording every other route uses.

    NOT a global id-opacity claim: update_thing / trash_thing still answer an
    existing-but-unauthorized id differently from an absent one."""
    verdict = visibility.may_view_thing(conn, viewer, thing)
    if verdict.ok:
        return None
    subject = subject or f"thing {thing_id!r}"
    if verdict.reason == "in_private_place":
        return jsonify({
            "error": f"{subject} is in a private place you may not enter",
            "status": "in_private_place",
            "place": verdict.place,
        }), 403
    if verdict.reason == "binned":
        return jsonify({
            "error": f"{subject} is in the bin",
            "status": "binned",
            "binned_at": verdict.binned_at,
            "can_restore": verdict.can_restore,
        }), 403
    return jsonify({"error": f"{subject} not found"}), 404


def _record_thing_gate(conn, viewer: str | None, thing_id: str, subject: str):
    """umwelt#1001: the thing read gate for a `record` kind whose ledger row
    is bound to a thing by a DERIVED id (use_events.thing_id,
    graft_advances.capsule_id, content_updates.subject_id). Raises
    _RecordError -- carrying the same informational refusal every gated route
    gives -- when `viewer` may not read the thing; a dangling thing_id is a
    404 (fail closed). `subject` names the ledger row in the error text so
    the derived thing id is never echoed. Call it BEFORE the snapshot is
    built, so nothing derived from the thing is read, let alone minted."""
    thing = db.get_thing(conn, thing_id)
    if thing is None:
        raise _RecordError(f"{subject} not found", 404)
    refusal = _thing_view_refusal(conn, viewer, thing, thing_id,
                                  subject=subject)
    if refusal is not None:
        raise _RecordError(f"{subject} is not readable by you",
                           refusal[1], refusal=refusal)


def _record_place_gate(conn, viewer: str | None, place_id: str, subject: str):
    """umwelt#1001: the place-interior gate for an `update` row whose subject
    is a PLACE. Same predicate `move_to` refuses entry with
    (`db._place_interior_visible_to`) and the same 403 body shape the thing
    gate gives (`status: in_private_place` + the place's public projection)."""
    place = db.get_place(conn, place_id)
    if place is None:
        raise _RecordError(f"{subject} not found", 404)
    if not db._place_interior_visible_to(place, viewer):
        raise _RecordError(
            f"{subject} is not readable by you", 403,
            refusal=(jsonify({
                "error": f"{subject} is in a private place you may not enter",
                "status": "in_private_place",
                "place": db._place_public_projection(place),
            }), 403))


def _callers_authorized(conn, callers: list[str], agent: str, thing: dict) -> bool:
    """Whether `agent` is authorized under ANY of the declared `callers`
    caller classes against `thing` — the shared evaluation loop for BOTH
    an action's `callers` list (`_action_authorized`, below) and a
    kinddef's top-level `update_callers` list (`_resolve_update_callers` +
    server.py's update_thing route, umwelt#300 S0(d)), so the two can
    never drift into two different authorization implementations (this
    codebase's explicit umwelt#515/#518 no-second-copy posture). Pure
    function over the closed caller-class vocabulary
    (`_KIND_CALLER_CLOSED_SET` + `'named:<agent>'`) — no other business
    logic (status, required args, etc.) belongs here."""
    author = _canonical_agent(thing["author"])
    place = db.get_place(conn, thing["place_id"])
    place_owner = (_canonical_agent(place["owner"])
                   if place and place.get("owner") else None)
    for caller in callers:
        if caller == "anyone":
            return True
        if caller == "author" and agent == author:
            return True
        if caller == "place_owner" and place_owner is not None and agent == place_owner:
            return True
        if caller == "granted" and db.has_grant(conn, thing["id"], agent):
            return True
        if caller.startswith("named:") and agent == _canonical_agent(caller[len("named:"):]):
            return True
    return False


def _action_authorized(conn, action_def: dict, agent: str, thing: dict) -> bool:
    """Whether `agent` may invoke `action_def` (one entry from a kind
    chain's `actions` dict) on `thing`. Extracted from act()'s own
    authorization check (umwelt #522) so the new read-only
    thing_actions route can compute the identical `permitted` flag
    without a second, potentially-drifting copy of this logic — the
    exact drift disease #515/#518 name. Pure function: does not check
    thing.status, kind_id presence, or required args — those stay
    act()'s job; this is ONLY the callers-list evaluation.

    umwelt#300 S0(d): now a thin wrapper over `_callers_authorized`, the
    shared caller-class evaluation loop update_thing's new declarative
    gate also uses — same delegation reasoning, one evaluation, two
    callers."""
    return _callers_authorized(conn, action_def.get("callers", []), agent, thing)


def _action_from_ok(action_def: dict, thing: dict) -> bool:
    """Whether `action_def` (one entry of a kind chain's `actions`) may run
    from `thing`'s CURRENT kind-state (`metadata.status`) -- the from-state
    half of act()'s gate, the "when" to `_action_authorized`'s "who".
    True iff the action's `from` is missing/empty (valid in every state) or
    the thing's status is listed in it. Pure function. act()'s transition
    gate and thing_actions' per-action `available` flag BOTH call this, so
    the rule has exactly one home and a client never keeps its own status
    list (umwelt #967; the #515/#518 drift disease)."""
    from_statuses = action_def.get("from") or []
    if not from_statuses:
        return True
    return (thing.get("metadata") or {}).get("status") in from_statuses


def _effective_actions(chain: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """The EFFECTIVE action set of a kind chain (as `_resolve_kind_chain`
    returns it, child-first): `[(name, action_def), ...]`, each name once,
    the nearest (child) definition winning -- a child's same-named action
    MASKS the parent's. Order is declaration order as the child-first walk
    first meets each name. thing_actions lists exactly this set and
    `_kind_terminal_states` reads exactly this set, so what a client is shown
    and what the kind's terminal states are can never drift (umwelt #976;
    the #515/#518 no-second-copy posture)."""
    seen: set[str] = set()
    out: list[tuple[str, dict]] = []
    for _, parsed in chain:
        for name, action_def in parsed.get("actions", {}).items():
            if name in seen:
                continue
            seen.add(name)
            out.append((name, action_def))
    return out


def _kind_terminal_states(chain: list[tuple[str, dict]]) -> frozenset[str]:
    """The states a thing of this kind can enter and never leave (umwelt
    #976, ruled by Kepler): a state is TERMINAL when NO action in the
    effective action set has a non-empty `from` that lists it. Derived from
    the kinddef, so a kind whose end word is `closed` is as visible as one
    whose end word is `approved`, and a kind that reuses `approved` as a
    mid-state (some action lists it in `from`) is not marked.

    Two guards, both deliberate:
      * A kind with NO action carrying a non-empty `from` is not stateful (it
        has no state machine at all): nothing is terminal. Without this guard
        every state of every stateless typed kind would read closed.
      * A candidate must be a state the kind KNOWS -- one an action moves a
        thing INTO (`to`). A status no action names (a typo, a state from an
        older kinddef) is not declared terminal by the kind; it reads open.
        (A state only ever listed in a `from` is, by definition, not terminal.)

    `from: []` / absent means "valid in every state" (`_action_from_ok`); it
    lists nothing, so it never makes a state non-terminal. Pure function."""
    effective = [a for _, a in _effective_actions(chain)]
    listed: set[str] = set()
    for a in effective:
        listed.update(s for s in (a.get("from") or []) if isinstance(s, str))
    if not listed:
        return frozenset()
    known = {a["to"] for a in effective if isinstance(a.get("to"), str)}
    return frozenset(known - listed)


def _status_is_closed(chain: list[tuple[str, dict]], thing: dict) -> bool:
    """Whether `thing`'s kind-state (`metadata.status`) is terminal for its
    kind. A thing with no string status is never closed. Pure function over a
    resolved chain -- the caller owns resolving (and caching) it."""
    status = (thing.get("metadata") or {}).get("status")
    return isinstance(status, str) and status in _kind_terminal_states(chain)


def _unavailable_reason(thing: dict) -> str:
    """The server-built words for an action whose from-state rule fails, so
    no client composes its own (umwelt #975)."""
    status = (thing.get("metadata") or {}).get("status")
    if isinstance(status, str) and status:
        return f"not available while {status}"
    return "not available in this state"


def _resolve_update_callers(conn, things_dir: Path, thing: dict) -> list[str]:
    """Resolve the declarative `update_callers` policy governing `thing`
    (umwelt#300 S0(d)) — ONLY called for a thing whose
    `update_policy_cohort` is already known non-NULL (see server.py's
    update_thing route); a legacy (NULL-cohort) thing never reaches this
    function at all, it stays on the plain `["author"]` gate directly.

    Untyped (no `metadata.kind_id`): stays closed — `["author"]`. Mira's
    security acceptance criteria point 3 ("define every fallback class"):
    an untyped-but-new-cohort thing gets no free "anyone" default just for
    lacking a kind.

    Typed: walks the instance's kind chain child-first (same order
    `_resolve_kind_chain` returns and `act()`'s own action lookup already
    relies on — nearest/child declaration wins), using EXPLICIT key
    presence (`"update_callers" in parsed`), not `.get(...)` truthiness —
    an explicitly-declared-empty list (`"update_callers": []`, meaning
    "nobody, not even the author") must be distinguishable from "not
    declared here, keep looking up the chain." The first link in the chain
    that DECLARES the key wins, even if its value is `[]`.

    A chain with no declaration anywhere resolves to `["anyone"]` — the
    going-forward open-by-default per Mira's point 1.

    A broken/cyclic/malformed kind chain (`_resolve_kind_chain` raising
    `_ThingSpecError`) fails closed to `["author"]`, never open — point 1's
    "an unknown/malformed declaration fails loud and closed."
    """
    kind_id = (thing.get("metadata") or {}).get("kind_id")
    if kind_id is None:
        return ["author"]
    try:
        chain = _resolve_kind_chain(conn, things_dir, str(kind_id))
    except _ThingSpecError:
        return ["author"]
    for _, parsed in chain:
        if "update_callers" in parsed:
            return parsed["update_callers"]
    return ["anyone"]


def _update_thing_authorized(conn, things_dir: Path, thing: dict, agent: str) -> bool:
    """Whether `agent` may `update_thing` on `thing` — the cohort-gate +
    resolve + authorize sequence, factored out (colleague review on PR #714)
    so update_thing's route doesn't carry two near-identical copies of it
    (the fast pre-check and the atomic re-check both need the exact same
    decision, made from whatever `thing`/`conn` each has in hand at its own
    point in time — same umwelt#515/#518 no-second-copy posture the rest of
    this mechanism already follows). NULL `update_policy_cohort` (every
    pre-existing thing) stays on the plain `["author"]` gate; a cohort-marked
    thing resolves its declared policy via `_resolve_update_callers`. See
    that function and update_thing's route comments for the full Mira's
    security acceptance criteria rationale — this is pure plumbing over it."""
    effective_callers = _update_thing_effective_callers(conn, things_dir, thing)
    return _callers_authorized(conn, effective_callers, agent, thing)


def _update_thing_effective_callers(conn, things_dir: Path, thing: dict) -> list[str]:
    """The caller classes that govern `update_thing` on `thing`: the legacy
    plain `["author"]` for a NULL-cohort thing, else the kind's declared
    `update_callers` (`_resolve_update_callers`). Split out of
    `_update_thing_authorized` (umwelt#982) so the route can ask the one
    question the thing read gate needs -- does this kind grant `anyone`? --
    from the SAME resolution, not a second copy of it."""
    if thing["update_policy_cohort"] is None:
        return ["author"]
    return _resolve_update_callers(conn, things_dir, thing)


def _merge_metadata_patch(existing: dict, meta_patch: dict) -> dict:
    """Merge `meta_patch` onto `existing` metadata: a key set to `null`
    clears it, any other value sets it, an absent key is left untouched.
    Same tri-state convention as customize_avatar's description/lineage/type
    fields — silent omission must never erase a sibling key. Shared by
    update_thing's kind-typed AND untyped metadata-patch paths (umwelt
    #393) so the two can never drift on what "merge" means."""
    merged = dict(existing)
    for k, v in meta_patch.items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


def _validate_kind_required_fields(conn, things_dir: Path, kind_id: str,
                                    metadata: dict) -> None:
    """Every REQUIRED field across `kind_id`'s resolved kind chain
    (child-first override, same as action resolution) must be present as a
    `metadata` key. Raises _ThingSpecError(..., 422) naming whichever are
    missing. Shared by create_thing's first-time kind_id set AND
    update_thing's kind-typed metadata-patch merge path (umwelt #109
    round-2 blocker fix, closing named-simplification #2) — one check, one
    code path, so the two callers can never drift on what "required" means."""
    chain = _resolve_kind_chain(conn, things_dir, str(kind_id))
    seen_fields: set[str] = set()
    missing_fields: list[str] = []
    for _, parsed in chain:
        for fname, fspec in parsed.get("fields", {}).items():
            if fname in seen_fields:
                continue
            seen_fields.add(fname)
            if fspec.get("required") and fname not in metadata:
                missing_fields.append(fname)
    if missing_fields:
        raise _ThingSpecError(
            f"metadata is missing required fields for kind {kind_id!r}: "
            f"{sorted(missing_fields)}", 422)


def _validate_checklist_metadata(metadata: dict) -> None:
    """Fail-loud shape + cap validation for `metadata.checklist` (umwelt
    #182 round 2) — the same create+update parity #147 established for the
    description-bomb cap: a malformed or oversized checklist must never be
    able to enter the world at ALL, neither forged in (create_thing) nor
    patched in later (update_thing), not just rejected by
    /umwelt/checklist's own runtime `add`/`edit` op checks. Without this,
    a direct create_thing/update_thing call could plant a checklist past
    _CHECKLIST_MAX_ITEMS, or with malformed (non-dict) items that would
    later crash the mutation route's `{**items[idx], ...}` merge, or bloat
    look_at's cross-agent-visible `checklist_render` unboundedly.

    No-op when `checklist` is absent — an ordinary thing has no shape here
    to validate. Permissive on extra per-item keys (the route itself only
    ever writes `text`/`done`, but a hand-authored spec adding, say, a
    `note` key isn't this validator's business) — only `text` (string,
    <= _CHECKLIST_ITEM_MAX_LEN) and `done` (bool) are required and
    type-checked, matching the closed shape /umwelt/checklist's own ops
    write.

    Raises _ThingSpecError(message, 422) on any violation.
    """
    if "checklist" not in metadata:
        return
    items = metadata["checklist"]
    if not isinstance(items, list):
        raise _ThingSpecError("metadata.checklist must be a list", 422)
    if len(items) > _CHECKLIST_MAX_ITEMS:
        raise _ThingSpecError(
            f"metadata.checklist exceeds the {_CHECKLIST_MAX_ITEMS}-item "
            f"limit (got {len(items)})", 422)
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise _ThingSpecError(
                f"metadata.checklist[{i}] must be an object with text/done "
                "keys", 422)
        text = item.get("text")
        if not isinstance(text, str):
            raise _ThingSpecError(
                f"metadata.checklist[{i}].text must be a string", 422)
        if len(text) > _CHECKLIST_ITEM_MAX_LEN:
            raise _ThingSpecError(
                f"metadata.checklist[{i}].text exceeds the "
                f"{_CHECKLIST_ITEM_MAX_LEN}-char limit (got {len(text)})",
                422)
        if not isinstance(item.get("done"), bool):
            raise _ThingSpecError(
                f"metadata.checklist[{i}].done must be a boolean", 422)


def _validate_params_declaration(params) -> str | None:
    """Fail-loud shape + cap validation for `metadata.params` (umwelt #396)
    — same create+update parity #147/#182 already established for the
    description-bomb/checklist caps: a malformed or oversized params
    declaration must never be able to enter the world at all, neither
    forged in (create_thing) nor patched in later (update_thing). Called
    for ANY thing (not just blueprint/executable/readable — the kinds that
    actually READ metadata.params at use()-time), so the `name != "help"`
    reservation below holds universally and can never be bypassed by
    picking an unexpected kind.

    `params` absent or explicit JSON `null` is valid — "no params
    declared", not an error; callers may call this unconditionally
    (metadata.get("params") naturally passes None through here).

    Returns an error message, or None if valid. Same str-or-None calling
    convention as _validate_use_args (#57) — the two callers below
    (create_thing's spec-validation path, update_thing's metadata-patch
    path) convert a non-None return into their own local error-response
    shape (_ThingSpecError / jsonify), same as they already do for
    _validate_kind_required_fields/_validate_checklist_metadata's
    exception-based sibling checks.
    """
    if params is None:
        return None
    if not isinstance(params, list):
        return f"metadata.params must be an array (got {_json_type_name(params)})"
    if len(params) > _PARAMS_MAX_COUNT:
        return (f"metadata.params exceeds the {_PARAMS_MAX_COUNT}-item limit "
                f"(got {len(params)})")
    seen_names: set[str] = set()
    for i, p in enumerate(params):
        if not isinstance(p, dict) or not p.get("name"):
            return f"metadata.params[{i}] must be an object with a non-empty name"
        name = p["name"]
        if not isinstance(name, str):
            return (f"metadata.params[{i}].name must be a string "
                    f"(got {_json_type_name(name)})")
        # umwelt #396 Trigger Logic: "help" is reserved for the
        # spec-request convention regardless of whether THIS declaring
        # thing's kind ever actually reaches the help-collision case — the
        # reservation must hold universally so a future named-args
        # mechanism (e.g. a possible act() generalization) can rely on it
        # without a compatibility gap.
        if name == "help":
            return "param name 'help' is reserved for the spec-request convention"
        if name in seen_names:
            return f"metadata.params has a duplicate name {name!r}"
        seen_names.add(name)
        if "type" in p and p["type"] not in _PARAMS_ALLOWED_TYPES:
            return (f"metadata.params[{i}].type must be one of "
                    f"{sorted(_PARAMS_ALLOWED_TYPES)} (got {p['type']!r})")
        if "enum" in p:
            enum = p["enum"]
            if not isinstance(enum, list) or not enum:
                return (f"metadata.params[{i}].enum must be a non-empty "
                        "array of strings")
            if not all(isinstance(e, str) for e in enum):
                return f"metadata.params[{i}].enum must contain only strings"
        if "required" in p and not isinstance(p["required"], bool):
            return f"metadata.params[{i}].required must be a boolean"
        if "description" in p:
            desc = p["description"]
            if not isinstance(desc, str):
                return f"metadata.params[{i}].description must be a string"
            if len(desc) > _PARAM_DESCRIPTION_MAX_LEN:
                return (f"metadata.params[{i}].description exceeds the "
                        f"{_PARAM_DESCRIPTION_MAX_LEN}-char limit "
                        f"(got {len(desc)})")
    return None


def _may_not_enter_refusal(place: dict[str, Any], agent: str):
    """umwelt#1011: the 403 body for an agent standing in a place it may no
    longer enter (a grant revoked, the place's kind changed). Returns None when
    the engine's own `_place_interior_visible_to` -- the predicate `move_to`
    and the read gate use -- says the agent may enter. Used by both agent-create
    routes (create_thing and use()'s blueprint stamp) so they cannot drift."""
    if db._place_interior_visible_to(place, agent):
        return None
    return jsonify({
        "error": f"you are standing in {place['label']!r} ({place['id']}) "
                 "but may not enter it, so you may not create there",
        "status": "may_not_enter",
        "place": db._place_public_projection(place),
    }), 403


def _create_thing_from_spec(conn, things_dir: Path, agent: str, place_id: str,
                            spec: dict) -> tuple[str, str]:
    """The shared create-thing primitive: validate a create_thing spec, write
    its source tree, and insert the DB row. Used by BOTH the create_thing
    route (agent-authored specs) and use()'s blueprint branch (engine-
    authored specs, substituted from a blueprint's template) — one code
    path, so thing-creation semantics can never drift between the two
    callers (umwelt #79 slice-1's "reuse the create_thing internals, don't
    duplicate" requirement).

    Raises _ThingSpecError(message, status) on any validation failure.
    Returns (thing_id, created_at) on success.
    """
    if not isinstance(spec, dict):
        raise _ThingSpecError("spec must be an object", 400)

    # Structural gate: id/author/created_at are world-assigned.
    present = [f for f in _SERVER_OWNED if f in spec]
    if present:
        raise _ThingSpecError(
            f"server-owned fields must not be in spec: {present}", 422)

    if not db.place_exists(conn, place_id):
        raise _ThingSpecError(f"place {place_id!r} not found", 404)

    # umwelt #880: a present name must be a string — checked before the
    # str() coercion below (which would store a list/int/null as text).
    if "name" in spec:
        _name_type_err = _name_label_type_error(spec["name"], "name")
        if _name_type_err:
            raise _ThingSpecError(_name_type_err, 422)
    # umwelt #900: a present, non-null description must be a string — same
    # posture as name above (null keeps today's str() meaning at this site).
    if spec.get("description") is not None:
        _desc_type_err = _name_label_type_error(spec["description"], "description")
        if _desc_type_err:
            raise _ThingSpecError(_desc_type_err, 422)
    name = str(spec.get("name", "")).strip()
    description = str(spec.get("description", "")).strip()
    kind = str(spec.get("kind", "")).strip()
    # umwelt #278: lane defaults to 'land' when the caller omits it — the
    # common case; most forged things don't care about lane and should
    # silently get the ordinary default. The is_body_kind reject below
    # already fires first for avatar-kind specs, so an agent-forge request
    # can never reach the create_thing call below with lane='road' (only
    # get_or_create_avatar's engine-birth path ever writes that).
    lane = str(spec.get("lane", "land")).strip()

    # Invariant #2 (Ariadne's skeleton call): avatars are engine-created at
    # birth (get_or_create_avatar/enter), never agent-forged (nor engine-
    # stamped from a blueprint) through this path.
    if db.is_body_kind(kind):
        raise _ThingSpecError(
            "avatars are engine-created at birth, not agent-forged", 422)

    if not name or not description or kind not in _VALID_KIND:
        raise _ThingSpecError(
            "spec.name, spec.description required; "
            f"spec.kind must be one of {_VALID_KIND}", 400)

    if lane not in _VALID_LANE:
        raise _ThingSpecError(
            f"spec.lane must be one of {_VALID_LANE}, got {lane!r}", 400)

    # umwelt #253: name is the glanceable render surface — refuse an
    # over-long one at the door, same posture as the description cap below.
    if len(name) > _NAME_MAX_LEN:
        raise _ThingSpecError(
            f"name exceeds the {_NAME_MAX_LEN}-char limit "
            f"(got {len(name)}) — a name is a glanceable label; put "
            "detail in spec.description", 422)
    # umwelt #874: a name renders as one line — no control characters.
    _name_problem = _name_char_problem(name)
    if _name_problem:
        raise _ThingSpecError(f"name {_name_problem}", 422)

    # umwelt #147 prong 1: refuse a disastrously-large description at the door,
    # so a description-flood bomb can never enter the world (loud 422, same
    # posture as the args/emotion caps). Long content belongs in the thing's
    # source (readable via use()), not its one-line description blurb.
    if len(description) > _DESCRIPTION_MAX_LEN:
        raise _ThingSpecError(
            f"description exceeds the {_DESCRIPTION_MAX_LEN}-char limit "
            f"(got {len(description)}) — a description is a short blurb; put "
            "long content in the thing's source (readable via use())", 422)

    exec_command = spec.get("exec_command")
    # umwelt #906: a present, non-null exec_command must be a string — before
    # the truthiness check below and any write (a list/dict was a 500; an
    # int/float/true was stored as text). null / "" / absent keep their meaning
    # (the executable-requires-exec_command 400 below is unchanged).
    if exec_command is not None and not isinstance(exec_command, str):
        raise _ThingSpecError(
            _name_label_type_error(exec_command, "exec_command"), 422)
    if kind == "executable" and not exec_command:
        raise _ThingSpecError("executable things require spec.exec_command", 400)

    source = spec.get("source") or {}
    if not isinstance(source, dict):
        raise _ThingSpecError(
            "spec.source must be an object of relpath->contents", 400)
    # umwelt #906: every file's contents must be a string — a non-string
    # (null included) reached write_text and was a 500.
    for _contents in source.values():
        if not isinstance(_contents, str):
            raise _ThingSpecError(
                "spec.source contents must be strings "
                f"(got {_json_type_name(_contents)})", 422)

    metadata = spec.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise _ThingSpecError("spec.metadata must be an object", 400)
    # umwelt #400/#494: reject ANY `_SERVER_OWNED_METADATA_KEYS` member
    # supplied in a create spec — action_log (act()'s own logging step),
    # graft_ref/graft_version (advance_ref's compare-and-swap pair), and
    # entries (board_entry_add/board_entry_retire's charge-board array) are
    # all written ONLY through their own declared/internal mechanism. A
    # caller supplying any of them (even at create time, before that
    # mechanism has ever run) is rejected loudly rather than silently
    # accepted and overwritten later — same structural-honesty posture as
    # send_crossing's consequence_grade rejection (#83's send_crossing
    # route). See the constant's own definition above for why this is a
    # WRITE-guard roster distinct from
    # `_LOOK_AT_NON_AUTHOR_METADATA_DENYLIST`'s read-visibility roster
    # (`entries` in particular must stay off that other set). Before #400
    # this was two separate literal checks (action_log; graft_ref/
    # graft_version) — #494 flagged that shape as fail-open on a new
    # server-owned key nobody remembers to add to every literal check.
    _server_owned_hit = _SERVER_OWNED_METADATA_KEYS & metadata.keys()
    if _server_owned_hit:
        raise _ThingSpecError(
            _server_owned_metadata_message(_server_owned_hit), 422)

    # umwelt #182 round 2: a malformed/oversized checklist must never be
    # FORGED in — same create-time-door posture as the description cap
    # above (#147). update_thing's metadata path calls the same validator
    # for patch-time parity (see that route).
    _validate_checklist_metadata(metadata)

    # umwelt #396: same create-time-door posture as the checklist validator
    # immediately above — a malformed/oversized/help-colliding params
    # declaration must never be FORGED in, for ANY kind (not just
    # blueprint/executable/readable). update_thing's metadata path calls
    # the same validator, against the MERGED metadata, for patch-time
    # parity (see that route).
    params_err = _validate_params_declaration(metadata.get("params"))
    if params_err:
        raise _ThingSpecError(params_err, 422)

    if kind == "blueprint":
        _validate_blueprint_source(source)
    elif kind == "kinddef":
        parsed_kinddef = _validate_kinddef_source(source)
        _validate_kind_extends(conn, things_dir, parsed_kinddef.get("extends"))

    # umwelt #107 slice A: metadata.kind_id stamps this thing as an
    # INSTANCE of a kinddef — validate it's a live kinddef thing and that
    # every REQUIRED field across its resolved kind chain (child-first
    # override, same as action resolution) is present as a metadata key.
    kind_id = metadata.get("kind_id")
    if kind_id is not None:
        _validate_kind_required_fields(conn, things_dir, str(kind_id), metadata)

    thing_id = str(uuid.uuid4())
    created_at = db.now_iso()
    try:
        _write_source(things_dir, thing_id, source)
    except (ValueError, OSError) as e:
        raise _ThingSpecError(f"source write failed: {e}", 400)

    db.create_thing(
        conn, id=thing_id, place_id=place_id, name=name,
        description=description, kind=kind, exec_command=exec_command,
        metadata=metadata, author=agent,
        created_at=created_at,
        weight_est=db.compute_weight_est(name, description),  # umwelt #88 slice 1
        lane=lane,  # umwelt #278
        # umwelt#300 S0(d): this helper backs BOTH agent-facing forge paths
        # (POST /umwelt/create_thing and use()-driven blueprint stamping) —
        # every thing born here is a genuinely NEW, agent-authored forge,
        # so it opts into the new declarative update-permission cohort.
        # Deliberately NOT threaded through the engine-boot singleton paths
        # (_ensure_engine_kinddef, the project_registrar instance,
        # get_or_create_avatar) — those call db.create_thing (or a raw
        # INSERT) directly, bypassing this function, so they stay on
        # today's legacy author-only gate. That's a deliberate, narrower
        # scope than "every newly forged thing," not an oversight: an
        # engine-boot instance's kind chain may declare no update_callers
        # at all, and stamping the cohort on it would let the new
        # open-by-default resolution apply to an engine-authored thing
        # nobody has reviewed for that — exactly the accidental-widening
        # class Mira's security acceptance criteria warn against.
        update_policy_cohort=_UPDATE_POLICY_COHORT_V1,
    )
    return thing_id, created_at


# umwelt #221 S3a §1: the project_board kinddef's own action table. Every
# verb is a `project_task_set` effect over db._MUTABLE_TASK_COLS — `claim`
# (turn_holder := actor), `hand_off` (turn_holder := args.to), `set_status`
# (status := args.status). `callers: ["anyone"]` is acceptable BECAUSE the
# project-scope bound (_apply_effect_project_task_set's PROJECT-SCOPE BOUND,
# umwelt #161 S2) already makes it safe — a permissive board can still only
# ever touch tasks in its OWN bound project (see this dict's own
# "description" text below, which names that reasoning for anyone reading
# the live kinddef.json off the wire, not just this source comment). `to:
# None` on every action — a board's own metadata.status is never used;
# these are pure log-append actions whose real effect is the
# project_task_set write, not a status transition on the board itself.
_PROJECT_BOARD_KINDDEF_JSON: dict = {
    "extends": None,
    "fields": {},
    "actions": {
        "claim": {
            "callers": ["anyone"], "requires": ["task"], "from": [],
            "to": None,
            "effects": [{"type": "project_task_set", "task_arg": "task",
                         "field": "turn_holder", "value_actor": True}],
        },
        "hand_off": {
            "callers": ["anyone"], "requires": ["task", "to"], "from": [],
            "to": None,
            "effects": [{"type": "project_task_set", "task_arg": "task",
                         "field": "turn_holder", "value_arg": "to"}],
        },
        "set_status": {
            "callers": ["anyone"], "requires": ["task", "status"], "from": [],
            "to": None,
            "effects": [{"type": "project_task_set", "task_arg": "task",
                         "field": "status", "value_arg": "status"}],
        },
        # umwelt #227 S4: structure-EDITING actions, added to the same open
        # `actions` map (Kepler's directive — S4 slots in additively, never
        # a rework of S1-S3's rows above). `add_task`/`add_subtask` are two
        # actions sharing ONE `project_task_create` effect type — the only
        # difference is `add_subtask`'s effect row also declares
        # `parent_arg`, so a brand-new top-level task can never accidentally
        # inherit a stray `parent` arg the caller happened to pass.
        "add_task": {
            "callers": ["anyone"], "requires": ["label"], "from": [],
            "to": None,
            "effects": [{"type": "project_task_create", "label_arg": "label",
                         "description_arg": "description"}],
        },
        "add_subtask": {
            "callers": ["anyone"], "requires": ["label", "parent"], "from": [],
            "to": None,
            "effects": [{"type": "project_task_create", "label_arg": "label",
                         "description_arg": "description",
                         "parent_arg": "parent"}],
        },
        # link_dep/unlink_dep: two actions sharing ONE project_task_link
        # effect type, `remove` a literal bool baked into each row (never
        # caller-suppliable) — same "field is a kind-author constant"
        # posture as set_status.field above.
        "link_dep": {
            "callers": ["anyone"], "requires": ["task", "depends_on"],
            "from": [], "to": None,
            "effects": [{"type": "project_task_link", "task_arg": "task",
                         "depends_on_arg": "depends_on", "remove": False}],
        },
        "unlink_dep": {
            "callers": ["anyone"], "requires": ["task", "depends_on"],
            "from": [], "to": None,
            "effects": [{"type": "project_task_link", "task_arg": "task",
                         "depends_on_arg": "depends_on", "remove": True}],
        },
        "set_project_status": {
            "callers": ["anyone"], "requires": ["status"], "from": [],
            "to": None,
            "effects": [{"type": "project_set", "field": "status",
                         "value_arg": "status"}],
        },
    },
}


def _ensure_engine_kinddef(conn, things_dir: Path, kind_id: str,
                            canonical_json: dict, description: str) -> str:
    """Shared forge-or-refresh helper for every engine-authored SINGLETON
    kinddef (umwelt #247) — a fixed, engine-chosen constant `kind_id`
    (never a uuid4), `author="engine"`, defined by a Python-side canonical
    dict living in this module (e.g. `_PROJECT_BOARD_KINDDEF_JSON`). Before
    #247 each such kind hand-rolled its own forge-if-absent logic
    (`_ensure_project_board_kinddef` and `_ensure_project_registrar`'s own
    kind-forge were the two copies); this is the ONE place the next kind
    evolution reuses instead of writing a third. Returns `kind_id` in
    every branch below.

    Three cases:

    - **absent** — forge fresh, same `_write_source` + `db.create_thing` +
      `IntegrityError`-race-catch pattern the pre-#247 forge functions each
      used individually (a genuine interleaved race — two concurrent
      first-ever callers — loses the same way `db.create_place`'s own
      catch handles the analogous race: the loser's insert fails, rolls
      back, and re-reads the winner's already-committed row).
    - **present, `author != "engine"`** — defense-in-depth only; should
      never happen with the fixed canonical-id scheme (no real agent
      identity can author as the literal string "engine"). Logs a warning
      to stderr and returns UNCHANGED — never raises, never touches a
      kind this helper didn't itself author.
    - **present, `author == "engine"`** — the actual #247 fix: parse the
      on-disk `kinddef.json` and compare the FULL parsed dict against
      `canonical_json` via plain Python dict equality (deliberately NOT a
      hash/version field — direct structural comparison has no
      version/JSON-drift risk, and it's what the proven one-shot
      `tools/refresh_project_board_kinddef.py` script already does). Equal
      -> no-op, no write. Different (including a malformed/corrupt on-disk
      `kinddef.json` that fails to parse at all — treated as maximally
      stale rather than allowed to crash app boot, since this helper's
      whole thesis is self-heal-don't-stay-broken) -> re-validate
      (`_validate_kinddef_source`) then atomically replace
      (`_replace_source`) — the same validate-before-destroy swap the
      one-shot script and `update_thing`'s own kinddef-source-patch path
      both use.
    """
    thing = db.get_thing(conn, kind_id)
    new_source = {"kinddef.json": json.dumps(canonical_json)}

    if thing is None:
        # Same shape validation _create_thing_from_spec runs at forge time
        # for an agent-authored kinddef — the engine's own singleton kinds
        # get no exemption from their own contract.
        _validate_kinddef_source(new_source)
        # kind_id is always an engine-chosen "kind_<name>" constant (never
        # a uuid4, per this helper's own contract) — assert rather than
        # silently forging a wrong `name` if a future caller ever violates
        # that contract.
        assert kind_id.startswith("kind_"), (
            f"_ensure_engine_kinddef requires a 'kind_<name>' id, got "
            f"{kind_id!r}")
        name = kind_id.removeprefix("kind_")
        try:
            _write_source(things_dir, kind_id, new_source)
            db.create_thing(
                conn, id=kind_id, place_id=db.WORKSHOP_PLACE_ID,
                name=name, description=description,
                kind="kinddef", exec_command=None, metadata={},
                author="engine", created_at=db.now_iso(),
                weight_est=db.compute_weight_est(name, description),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
        return kind_id

    if thing["author"] != "engine":
        print(
            f"[engine-kinddef] {kind_id!r} exists with author="
            f"{thing['author']!r} (expected 'engine') — refusing to "
            "touch a non-engine-authored thing at this id; skipping the "
            "forge/refresh check entirely (umwelt #247).",
            file=sys.stderr,
        )
        return kind_id

    current_source = _read_source(things_dir, kind_id)
    current_raw = current_source.get("kinddef.json")
    try:
        current_parsed = json.loads(current_raw) if current_raw else None
    except json.JSONDecodeError:
        # Corrupt on-disk source (disk fault, manual tamper, an interrupted
        # non-atomic write from a version predating _replace_source's
        # validate-before-destroy swap) — treat as maximally stale rather
        # than propagating and crashing create_app's boot sequence. Falls
        # through to the same re-validate + replace path as any other
        # staleness case below.
        current_parsed = None
    if current_parsed == canonical_json:
        return kind_id  # already current — no-op, no write

    # Stale (e.g. a pre-existing singleton surviving a pure code-swap
    # deploy that added new action rows to canonical_json) — re-validate,
    # then atomically swap the on-disk source. Same contract update_thing's
    # kinddef-source-patch branch and the one-shot refresh script both use.
    _validate_kinddef_source(new_source)
    _replace_source(things_dir, kind_id, new_source)
    return kind_id


def _ensure_project_board_kinddef(conn, things_dir: Path) -> str:
    """Idempotently forge-or-refresh the SINGLETON `project_board` kinddef
    thing (umwelt #221 S3a §1; refresh behavior added #247) — the one
    engine-level kind definition every project's board instance points to
    via `metadata.kind_id`. Returns the canonical kind_id
    (_PROJECT_BOARD_KIND_ID) whether it was forged, refreshed, or already
    current.

    Thin wrapper over the shared `_ensure_engine_kinddef` (umwelt #247) —
    see that function's docstring for the full forge/refresh/guard
    mechanics. Called both lazily (on the first create_project in a given
    world/test DB — a test fixture that never touches main()'s seed path
    still gets a working create_project route with zero fixture changes)
    AND at boot time (create_app, alongside _ensure_project_registrar) —
    the boot-time call is what lets a pre-existing singleton self-heal
    immediately on a pure code-swap promote, rather than only healing on
    the next create_project call.

    Idempotent by construction: a FIXED canonical thing id
    (_PROJECT_BOARD_KIND_ID, not a uuid4). A genuine interleaved race (two
    concurrent first-ever create_project calls) is handled inside
    _ensure_engine_kinddef the same way db.create_place's own
    IntegrityError catch handles the analogous race.
    """
    kind_description = (
        "The project board kind (umwelt #221/#227) — claim/hand_off/"
        "set_status act on a bound project's tasks via project_task_set; "
        "add_task/add_subtask/link_dep/unlink_dep/set_project_status "
        "(S4) create tasks, link/unlink task_deps, and mutate the bound "
        "project's own status. callers=anyone is safe because every "
        "board-bound effect fail-closes any write outside this board's "
        "own bound project (umwelt #161 S2 / #227 S4, ob_2892)."
    )
    return _ensure_engine_kinddef(
        conn, things_dir, _PROJECT_BOARD_KIND_ID, _PROJECT_BOARD_KINDDEF_JSON,
        kind_description,
    )


# umwelt #227 S4: the project_registrar kind's own action table — ONE
# action, `found_project`, a `project_create` effect. `to: None` for the
# same reason project_board's actions all use it: the registrar
# instance's own metadata.status is never used, the real effect is the
# project_create write, not a status transition on the registrar itself.
_PROJECT_REGISTRAR_KINDDEF_JSON: dict = {
    "extends": None,
    "fields": {},
    "actions": {
        "found_project": {
            "callers": ["anyone"], "requires": ["label"], "from": [],
            "to": None,
            "effects": [{"type": "project_create", "label_arg": "label",
                         "description_arg": "description"}],
        },
    },
}


def _ensure_project_registrar(conn, things_dir: Path) -> str:
    """Idempotently forge-or-refresh the SINGLETON `project_registrar`
    kind AND its ONE instance in the Workshop (umwelt #227 S4; kind-level
    refresh behavior added #247) — "found a new project" from in-world,
    `use`/`act`-able the same way any other typed thing is, instead of a
    raw POST /umwelt/projects. Returns _PROJECT_REGISTRAR_THING_ID
    whether its kind was forged, refreshed, or already current, and its
    instance already existed or was just forged.

    Two-thing forge (the kind, then the one instance pointing at it) —
    unlike _ensure_project_board_kinddef (which forges only the kind; each
    project's board INSTANCE is minted separately per-project by
    create_project_with_board), the registrar has no per-caller instance
    at all — there's exactly one, world-wide, the same singleton posture
    WORKSHOP_PLACE_ID itself has. The kind-forge/refresh half is now a
    thin call into the shared `_ensure_engine_kinddef` (umwelt #247) — see
    that function's docstring for the forge/refresh/guard mechanics. The
    instance-forge half (below) is unchanged: FIXED canonical id (not a
    uuid4), checked via db.get_thing before forging, same
    IntegrityError/rollback race-loser handling as before.

    Called from create_app at APP-BOOT time (not lazily on first request,
    unlike the board kind's lazy path) — see _apply_effect_project_create's
    docstring for why the registrar's own effect needs the board kind to
    already exist before any request can reach it.
    """
    kind_description = (
        "The project_registrar kind (umwelt #227 S4) — found_project "
        "acts via project_create to atomically birth a brand-new "
        "project (room + bound board), the same S3a atomic path "
        "POST /umwelt/projects uses."
    )
    _ensure_engine_kinddef(
        conn, things_dir, _PROJECT_REGISTRAR_KIND_ID,
        _PROJECT_REGISTRAR_KINDDEF_JSON, kind_description,
    )

    existing_instance = db.get_thing(conn, _PROJECT_REGISTRAR_THING_ID)
    if existing_instance is None:
        instance_description = (
            "Found a new project here — use() or act(found_project) with "
            "a label (and optional description) to birth a project "
            "atomically: a Workshop room + a bound project_board, ready "
            "to claim/hand_off/set_status/add_task/link_dep on."
        )
        try:
            db.create_thing(
                conn, id=_PROJECT_REGISTRAR_THING_ID,
                place_id=db.WORKSHOP_PLACE_ID, name="project_registrar",
                description=instance_description, kind="readable",
                exec_command=None,
                metadata={"kind_id": _PROJECT_REGISTRAR_KIND_ID},
                author="engine", created_at=db.now_iso(),
                weight_est=db.compute_weight_est(
                    "project_registrar", instance_description),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
    return _PROJECT_REGISTRAR_THING_ID


# umwelt #400 — the worked example for "a thing's DEFINITION is
# author-only, a thing's STATE belongs behind declared actions": the
# charge board's entire mutable surface is these actions (`rotate-log`
# added umwelt #736), its layout and standing sections (the thing's
# `source`) remain author-owned definition exactly like every other
# thing. `add-entry`/`retire-entry`
# are callable by the author OR anyone the author (or a delegate) has
# `granted` — the content-editing half of the surface.
# `grant-access`/`revoke-access` deliberately admit `granted` at the
# ACTION-authorization layer too (not `["author"]` alone) — this is a
# judgment call, flagged in the #400 build handoff: the REAL security
# boundary is the effect-level gate inside _apply_effect_grant_caller /
# _apply_effect_revoke_caller (author, or an existing can_grant=1 row),
# so admitting `granted` here only lets a delegate REACH that gate, never
# bypass it — a can_grant=0 delegate's attempt still fails cleanly as an
# effect_failure, never a write. `can_grant_arg: "can_grant"` on
# grant-access (umwelt #400 round 3, Borges security fix) means the
# GRANTER chooses, per call, via `args: {"agent": ..., "can_grant": true}`,
# whether THIS grant passes the full baton (content access AND the
# ability to grant onward — matching the issue's own "commander granting
# access while Kepler's asleep" scenario, where a delegate may need to
# deputize a further delegate in an emergency) or content-access-only
# (`args` omits `can_grant`, or passes `can_grant: false`) — the effect
# implementation defaults to False, so a granter who doesn't think about
# it never silently deputizes someone. `can_grant` is deliberately NOT in
# `requires` — required args are validated as non-empty strings
# (act()'s `missing_args` check), which a JSON bool would fail; it must
# stay optional-with-default per Borges's ruling ("defaulting to FALSE"),
# not a forced choice on every call.
_CHARGE_BOARD_KINDDEF_JSON: dict = {
    "extends": None,
    "fields": {},
    "actions": {
        "add-entry": {
            "callers": ["author", "granted"], "requires": ["text"],
            "from": [], "to": None,
            "effects": [{"type": "board_entry_add", "text_arg": "text"}],
        },
        "retire-entry": {
            "callers": ["author", "granted"], "requires": ["entry_id"],
            "from": [], "to": None,
            "effects": [{"type": "board_entry_retire",
                         "entry_id_arg": "entry_id"}],
        },
        # edit-entry (umwelt #836): a one-word correction previously cost
        # a retire+add (a new entry_id, the original's provenance lost).
        # Same callers as add-entry/retire-entry.
        "edit-entry": {
            "callers": ["author", "granted"], "requires": ["entry_id", "text"],
            "from": [], "to": None,
            "effects": [{"type": "board_entry_edit",
                         "entry_id_arg": "entry_id", "text_arg": "text"}],
        },
        "grant-access": {
            "callers": ["author", "granted"], "requires": ["agent"],
            "from": [], "to": None,
            "effects": [{"type": "grant_caller", "agent_arg": "agent",
                         "can_grant_arg": "can_grant"}],
        },
        "revoke-access": {
            "callers": ["author", "granted"], "requires": ["agent"],
            "from": [], "to": None,
            "effects": [{"type": "revoke_caller", "agent_arg": "agent"}],
        },
        # rotate-log (umwelt #736): archives metadata.action_log so a
        # board frozen at _ACTION_LOG_MAX_ENTRIES can keep operating.
        # `["author", "granted"]`, same as the content-editing actions
        # above — Borges's ruling: a `granted` holder can already
        # retire-entry one at a time, so restricting the bulk rotation
        # specifically guards nothing a loop doesn't already defeat.
        "rotate-log": {
            "callers": ["author", "granted"], "requires": [], "from": [],
            "to": None,
            "effects": [{"type": "action_log_rotate"}],
        },
    },
}


def _ensure_charge_board_kinddef(conn, things_dir: Path) -> str:
    """Idempotently forge-or-refresh the SINGLETON `charge_board` KIND
    (umwelt #400) — thin wrapper over the shared `_ensure_engine_kinddef`
    (umwelt #247), same posture as `_ensure_project_board_kinddef`: forges
    only the kind, never an instance (see `_CHARGE_BOARD_KIND_ID`'s own
    comment for why an engine-authored singleton instance would
    permanently lock every non-author agent out of the `granted`
    bootstrap). Returns the canonical kind_id (_CHARGE_BOARD_KIND_ID)
    whether it was forged, refreshed, or already current.

    Called at APP-BOOT time (create_app, alongside
    _ensure_project_board_kinddef/_ensure_project_registrar) so a
    pre-existing world self-heals on a pure code-swap promote, same as
    those two.
    """
    kind_description = (
        "The charge_board kind (umwelt #400) — a shared board whose "
        "STATE (entries) is edited through declared actions rather than "
        "by touching the thing's own definition. add-entry/retire-entry/"
        "edit-entry (umwelt #836) are callable by the author or anyone "
        "the author (or a delegate) has granted; grant-access/"
        "revoke-access manage that delegate list, security-gated at the "
        "effect layer (author, or an existing delegate with can_grant=1) "
        "independent of this action's own caller list."
    )
    return _ensure_engine_kinddef(
        conn, things_dir, _CHARGE_BOARD_KIND_ID, _CHARGE_BOARD_KINDDEF_JSON,
        kind_description,
    )


def _apply_effect_move_to(conn, thing: dict, container_id: str) -> str | None:
    """`move_to` effect (umwelt #107 slice B): move `thing` into
    `container_id` via the SAME internal path put()'s route uses —
    `db.put_thing_in_container`, capacity-checked exactly as put() checks
    it — but deliberately WITHOUT put()'s `_reachable` requirement. This is
    a kind-sanctioned move (the kinddef author declared it, not the acting
    agent), NOT the same bound `send_home` relies on: `send_home` is
    self-bounded on both axes (the acting agent must already hold, or be
    author-at-rest-colocated with, the thing; the destination is always the
    *actor's own* home place). `move_to` has neither bound — the acting
    agent needs no relationship to the thing or the container, and the
    destination is a constant the kind's author declared at kinddef-creation
    time, not the actor's own place. The bound here is entirely different:
    `kind_id` is author-opt-in-only (a thing only becomes subject to a
    kind's declared effects because ITS OWN author chose that kind at
    create_thing/update_thing time), inside umwelt's overall
    trust-scoped-private-network posture (admitted private-network peers are already trusted
    counterparties, same trust register as blueprints). Documented here
    rather than silently re-derived.

    Best-effort: an action that reaches this effect has ALREADY committed
    its transition+log (see act()) — this never rolls that back. Returns
    None on success, or a short failure reason string on failure (surfaced
    as a `{"type": "move_to", "index": <i>, "reason": "failed: <reason>"}`
    entry in the `effect_failures` list on both the response and the
    persisted log entry).

    A move onto the container the thing ALREADY sits in is a no-op success
    (umwelt #972): returns None without moving the thing or recording a
    failure. It decides on the thing's current holder, re-read from the DB
    (the `thing` dict passed in may be stale).
    """
    container = db.get_thing(conn, container_id)
    if container is None:
        return "container not found"
    if container["status"] != "live":
        return f"container is {container['status']}"
    if container["kind"] != "container":
        return "target is not a container"
    if thing["id"] == container["id"]:
        return "cannot move a thing into itself"

    # umwelt #972: the thing already sits in this container -> the kind
    # author's declared end state already holds. No-op SUCCESS, checked
    # AFTER the validity refusals above and BEFORE the capacity check (the
    # destination's load already includes the thing; adding its weight again
    # was a false 'over capacity'). Returns without moving the thing or
    # recording a failure. It decides on the thing's CURRENT holder, re-read
    # from the DB: act() hands every effect the same pre-effects dict, so an
    # earlier effect (or a concurrent mover) can leave it stale. When the
    # shortcut does not apply, the rest of the function runs unchanged on
    # the passed dict (a stale mismatch still ends in the guarded UPDATE's
    # race message). The HTTP /put route keeps its own 409 (different
    # contract).
    fresh = db.get_thing(conn, thing["id"])
    if (fresh is not None
            and fresh["holder_kind"] == "container"
            and fresh["holder_id"] == container["id"]):
        return None

    contents = db.things_in_container(conn, container["id"])
    load = sum(t["weight_est"] for t in contents)
    capacity = _container_capacity(container)
    new_load = load + thing["weight_est"]
    if new_load > capacity:
        overweight = new_load - capacity
        return (f"over capacity ({new_load} against a {capacity}-token "
                f"capacity, {overweight} over)")

    try:
        moved = db.put_thing_in_container(conn, thing, container["id"])
    except ValueError:
        # Unexpected source holder_kind (umwelt #966): not a race — say so.
        return f"cannot move from holder_kind {thing['holder_kind']!r}"
    if not moved:
        # Guarded UPDATE matched no row: thing's holder state moved between
        # act()'s original read and this write (same race class put()
        # itself guards against — PR #91 family).
        return "someone else just moved it"
    return None


def _apply_effect_notify_author(conn, thing: dict, actor: str,
                                action_name: str, args: dict,
                                wake: bool,
                                comment_key: str = "comment"
                                ) -> tuple[str | None, bool]:
    """`notify_author` effect (umwelt #107 slice B): send a crossing to the
    target thing's author's home Post — the SAME `place_post_<agent>`
    convention register/enter/_ensure_home_place use to derive a home
    place id. Deliberately does NOT auto-vivify that place the way
    _ensure_home_place does at birth: an author who never registered has
    no Post to notify, and that is a best-effort FAILURE ("author has no
    Post"), not a silent mint of a place for someone the world never
    actually introduced (the birth paths are the only home-place minters —
    this effect only ever reads).

    `comment_key` is the args key the comment body is read from — declared
    via the effect's `comment_arg` field (#569), defaulting to "comment" for
    back-compat with declarations made before #569 brought notify_author into
    the `*_arg` contract. Same declared-mode posture as the other 11 effects.

    `wake` is a REQUEST, never authoritative — evaluated by the SAME
    `_evaluate_wake` gate send_crossing's own route uses, so this path is
    structurally incapable of stamping wake_granted itself (Borges's #107
    security invariant — one gate, not a second one that could drift).

    Returns (failure_reason_or_None, wake_granted). On failure,
    wake_granted is always False (no crossing was sent, so nothing was
    granted).
    """
    author = _canonical_agent(thing["author"])
    home_place_id = f"place_post_{author}"
    if db.get_place(conn, home_place_id) is None:
        return f"author {author!r} has no Post — never registered", False

    body = f"{actor} did {action_name} on '{thing['name']}'"
    comment = args.get(comment_key)
    if isinstance(comment, str) and comment.strip():
        body += f": {comment.strip()}"

    wake_granted, _wake_status = _evaluate_wake(conn, actor, author, wake)
    sent_at = db.now_iso()
    db.build_crossing(
        conn, id=str(uuid.uuid4()), sender=actor, destination=home_place_id,
        sent_at=sent_at, received_at=sent_at, crossing_latency_s=0.0,
        crossing_hops=0, body=body, to_agent=author,
        wake_granted=wake_granted,
    )
    return None, wake_granted


def _capped_ids(ids: list[str]) -> str:
    """Render an id list capped at 5 entries with a "+N more" suffix —
    shared formatting helper so a long blocking-dep/open-subtask list
    never bloats an error message unboundedly."""
    shown = ids[:5]
    more = len(ids) - len(shown)
    suffix = f" +{more} more" if more > 0 else ""
    return f"[{', '.join(shown)}]{suffix}"


def _done_completeness_blocked_reason(blocking: list[str],
                                       open_subs: list[str]) -> str:
    """The done-completeness gate's failure message (spec-v4) — ONE
    shared string builder for both `/umwelt/tasks/<id>/update`'s 409 and
    `_apply_effect_project_task_set`'s effect-failure reason, so the two
    can never drift into two different-shaped messages for the same
    refusal (umwelt #227, Kepler's dogfood rider — replies #4687/#4688:
    the route's capped-ids-plus-next-step-suggestion shape was genuinely
    useful when probing stale board rows by hand; worth exactly once,
    not two divergent copies)."""
    return (
        f"cannot mark done: {len(blocking)} unresolved dep(s) "
        f"{_capped_ids(blocking)} and {len(open_subs)} open sub-task(s) "
        f"{_capped_ids(open_subs)} — finish or cancel them first, or "
        f"remove stale dep edges"
    )


def _apply_effect_project_task_set(
        conn, thing: dict, actor: str, args: dict,
        effect: dict) -> tuple[str | None, dict | None]:
    """`project_task_set` effect (umwelt #161 S2): the write-path for the
    typed-thing project board — an act() effect that sets one field
    (`effect.field`, restricted to `db._MUTABLE_TASK_COLS`) on a task named
    by `args[effect['task_arg']]`, to either the acting agent
    (`effect.value_actor`, canonical — `actor` is the SAME already-
    canonicalized agent act()'s dispatch passes to
    `_apply_effect_notify_author`)
    or `args[effect['value_arg']]` (`effect.value_arg`) — exactly one of the
    two is present, enforced at kinddef-creation time (see
    _validate_kinddef_source).

    Best-effort, SAME contract as `_apply_effect_move_to`: runs after the
    action's transition+log commit, never rolls that back. Returns
    `(None, {"task_id": task_id, "field": field, "value": value})` on
    success — umwelt #267: the SAME `(reason, result)` side-channel shape
    `_apply_effect_board_entry_add` uses, read back by act()'s dispatch loop
    into the `effect_results` side-channel, so a caller can recover the
    written task id/field/value from act()'s response without a follow-up
    `look_at` — or `(reason, None)` on failure (surfaced in
    `effect_failures` exactly like the other two effects — no silent
    no-op). The wake-emit-failure path below is the one exception: a
    successful field write whose court-change crossing then fails to send
    still returns `(reason, None)` — the field write is not rolled back,
    but there is no result to hand back on that path (best-effort, never
    rolls back, documented in COURT-CHANGE WAKE below).

    PROJECT-SCOPE BOUND (Borges security must-fix, umwelt #161 — load-
    bearing, do not omit): this effect may ONLY mutate a task that belongs
    to the SAME project the acting board thing is bound to via
    `thing.metadata.project_id`. Without this bound, any kinddef author
    could declare a project_task_set effect naming an arbitrary task_id
    from ANY project, turning a single kind-sanctioned board action into an
    unbounded cross-project write. `metadata.project_id` lives on the
    board thing itself (S3) and is server-owned: it is
    assigned only when the engine creates the board
    (`db.create_project_with_board`) and is refused on caller
    create_thing/update_thing metadata (umwelt #879) — so the effect is
    safe-by-construction: it can only ever reach tasks in the project its
    own board thing declares. Until a board thing is bound
    (metadata.project_id absent), this effect always refuses.

    COURT-CHANGE WAKE (umwelt #221 S3 §3 — load-bearing, do not omit): after
    a successful `field == "turn_holder"` write whose value actually changed
    to a DIFFERENT agent, emit an addressed crossing to the NEW turn_holder
    in the project's own room (`get_project(task['project_id'])['place_id']`).
    The crossing rides the SAME consent+rate `_evaluate_wake` gate
    `_apply_effect_notify_author`/send_crossing use — never an auto-gong;
    a turn_holder churn must not become a wake-spam vector. Rider 2 (Kepler
    #222 approval, self-claim suppression): a flip TO the acting agent
    itself (a `claim`) emits NO crossing at all — you don't notify yourself
    that you took your own turn. Guard: only emit when the new turn_holder
    is non-empty, differs from `actor`, AND differs from the task's PRIOR
    turn_holder (a hand_off re-confirming the same holder is not a change).
    Best-effort, same contract as the field write itself: never rolls back
    the write; a failure to emit (project has no room yet, project
    vanished) is surfaced as this effect's own failure reason, same
    `effect_failures` channel as every other failure path above — no silent
    drop.
    """
    task_arg = effect["task_arg"]
    task_id = args.get(task_arg)
    if not isinstance(task_id, str) or not task_id:
        return f"task_arg {task_arg!r} value is missing or not a string", None

    if effect.get("value_actor"):
        value = actor
    else:
        value_arg = effect["value_arg"]
        value = args.get(value_arg)
        if not isinstance(value, str):
            return f"value_arg {value_arg!r} value is missing or not a string", None

    field = effect["field"]
    if field == "status" and value not in _TASK_STATUS:
        return f"invalid status {value!r}", None
    # umwelt #874: only the label is a one-line surface (description may
    # legitimately hold newlines). Checked before any write.
    if field == "label":
        label_problem = _name_char_problem(value)
        if label_problem:
            return f"label {label_problem}", None

    task = db.get_task(conn, task_id)
    if task is None:
        return f"task {task_id!r} not found", None

    # PROJECT-SCOPE BOUND (Borges security must-fix) -----------------------
    board_project = (thing.get("metadata") or {}).get("project_id")
    if board_project is None:
        return "board thing is not bound to a project (no metadata.project_id)", None
    if task["project_id"] != board_project:
        return f"task {task_id!r} is not in this board's project", None

    # DONE-COMPLETENESS GATE (spec-v4): the direct /umwelt/tasks/<id>/update
    # route refuses status="done" while the task has an unresolved blocking
    # dep or an open sub-task (db.get_blocking_dep_ids /
    # db.get_open_subtask_ids, server.py's update_task route) — this effect
    # must not be a loophole around that same invariant.
    if field == "status" and value == "done":
        blocking = db.get_blocking_dep_ids(conn, task_id)
        open_subs = db.get_open_subtask_ids(conn, task_id)
        if blocking or open_subs:
            return _done_completeness_blocked_reason(blocking, open_subs), None

    old_turn_holder = task["turn_holder"]  # captured BEFORE the write below
    db.update_task(conn, task_id, {field: value}, db.now_iso())

    # COURT-CHANGE WAKE (umwelt #221 S3 §3, Rider 2) ------------------------
    if field == "turn_holder":
        old_holder = _canonical_agent(old_turn_holder) if old_turn_holder else None
        new_holder = _canonical_agent(value) if value else None
        if new_holder and new_holder != actor and new_holder != old_holder:
            project = db.get_project(conn, task["project_id"])
            if project is None or not project.get("place_id"):
                return ("court-change crossing not sent: project "
                        f"{task['project_id']!r} has no room (place_id)"), None
            body = (f"{actor} handed task '{task['label']}' ({task_id}) "
                    f"to {new_holder}'s court")
            wake_granted, _wake_status = _evaluate_wake(
                conn, actor, new_holder, True)
            sent_at = db.now_iso()
            db.build_crossing(
                conn, id=str(uuid.uuid4()), sender=actor,
                destination=project["place_id"], sent_at=sent_at,
                received_at=sent_at, crossing_latency_s=0.0,
                crossing_hops=0, body=body, to_agent=new_holder,
                wake_granted=wake_granted,
            )
    return None, {"task_id": task_id, "field": field, "value": value}


def _apply_effect_project_task_create(
        conn, thing: dict, actor: str, args: dict,
        effect: dict) -> tuple[str | None, dict | None]:
    """`project_task_create` effect (umwelt #227 S4): the "add a task" leg
    of the board's structure-editing surface — creates a NEW task under
    the acting board thing's OWN bound project (`thing.metadata.
    project_id`), never a caller-named project. Same board->project
    fail-close posture as `_apply_effect_project_task_set` (ob_2892):
    until a board is bound, this effect always refuses; there is no field
    in `args` or the effect schema that can name a different project.

    `effect.label_arg` (required) names the args key holding the new
    task's label; `effect.description_arg` (optional) likewise for the
    description (defaults to "" when the effect doesn't declare one, or
    the caller omits the arg). `effect.parent_arg` (optional) names the
    args key holding a parent task id for a sub-task — when given, the
    parent must resolve to a task in the SAME bound project (reusing the
    umwelt #230 same-project posture) and must not be done/cancelled
    (mirrors create_task's HTTP route closed-parent gate, server.py's
    `create_task` — a brand-new task must not be silently added under a
    parent that's already been closed out). A freshly-created task has no
    inbound dep/parent edges yet, so there is no cycle risk here the way
    `project_task_link` has to guard against.

    Best-effort, same contract as the other project-board effects: runs
    after the action's transition+log commit, never rolls that back.
    Returns `(None, {"task_id": task_id})` on success — umwelt #267: the
    SAME `(reason, result)` side-channel shape `_apply_effect_board_entry_
    add` uses, read back by act()'s dispatch loop into the `effect_results`
    side-channel, so a caller can recover the new task's id from act()'s
    response without a follow-up `look_at` — or `(reason, None)` on failure
    (surfaced in `effect_failures`, no silent no-op).
    """
    board_project = (thing.get("metadata") or {}).get("project_id")
    if board_project is None:
        return "board thing is not bound to a project (no metadata.project_id)", None

    label_arg = effect["label_arg"]
    label = args.get(label_arg)
    if not isinstance(label, str) or not label.strip():
        return f"label_arg {label_arg!r} value is missing or not a string", None
    # umwelt #874: task labels render as one line — no control characters.
    label_problem = _name_char_problem(label.strip())
    if label_problem:
        return f"label {label_problem}", None

    description = ""
    description_arg = effect.get("description_arg")
    if description_arg:
        description_val = args.get(description_arg)
        if description_val is not None:
            if not isinstance(description_val, str):
                return (f"description_arg {description_arg!r} value must "
                        "be a string"), None
            description = description_val

    parent_task_id = None
    parent_arg = effect.get("parent_arg")
    if parent_arg:
        parent_raw = args.get(parent_arg)
        if parent_raw is not None:
            if not isinstance(parent_raw, str) or not parent_raw.strip():
                return f"parent_arg {parent_arg!r} value must be a string", None
            parent_task = db.get_task(conn, parent_raw)
            if parent_task is None:
                return f"parent task {parent_raw!r} not found", None
            if parent_task["project_id"] != board_project:
                return (f"parent task {parent_raw!r} is not in this "
                        "board's project"), None
            if parent_task["status"] in ("done", "cancelled"):
                return (f"cannot add a sub-task under a "
                        f"{parent_task['status']} parent — reopen the "
                        "parent first"), None
            parent_task_id = parent_raw

    task_id = "task_" + str(uuid.uuid4())
    now = db.now_iso()
    db.create_task(conn, task_id, board_project, label.strip(),
                   description, actor, now, now,
                   parent_task_id=parent_task_id)
    return None, {"task_id": task_id}


def _apply_effect_project_task_link(conn, thing: dict, actor: str,
                                     args: dict, effect: dict) -> str | None:
    """`project_task_link` effect (umwelt #227 S4): the "link/unlink a
    dependency" leg of the board's structure-editing surface —
    add/remove a `task_deps` edge between two tasks.

    BOARD-BOUND, same posture as `project_task_set`/`project_task_create`
    (ob_2892 — corrected here after a reviewer-fairy catch found this
    effect had shipped WITHOUT the bound despite the spec/security-review
    both requiring it): refuses when the acting board thing has no
    `metadata.project_id`, and refuses unless BOTH `task_id` and
    `depends_on_task_id` belong to that SAME bound project. This is
    layered on TOP of the `/umwelt/tasks/<id>/deps` HTTP route's own
    #230 same-project-as-EACH-OTHER check (a dep edge is inherently a
    same-project relation) — that check alone is not sufficient here,
    because it only proves the two tasks agree with each other, never
    that they agree with the board's own binding. Without the board-bound
    check, a board bound to project A could write a dep edge entirely
    inside an unrelated project B (as long as B's own two tasks matched
    each other) — the exact gap the reviewer-fairy reproduced.

    `effect.remove` is a LITERAL bool baked into the kinddef row (not
    caller-suppliable) — `link_dep`/`unlink_dep` are two distinct actions
    sharing this one effect type, `link_dep` declaring `remove: false` and
    `unlink_dep` declaring `remove: true`, the same "field is a kind-author
    constant, not caller input" posture `project_task_set.field` uses.

    Add path reuses `db.would_create_task_dep_cycle` — a direct self-dep
    or a transitive cycle is a failure (the effect-level equivalent of the
    route's 409), never silently applied. Idempotent otherwise (re-adding
    an existing edge, or removing a non-existent one, succeeds as a no-op)
    — same posture as the HTTP route.

    #166 INHERITANCE (Ariadne's design-review call, corrected per Borges's
    security-lane catch — umwelt #227 spec replies #4681/#4686; #166 itself
    landed as its own standalone PR and is no longer "not fixed here"):
    this reuses the route's exact check-then-act shape (read
    would_create_task_dep_cycle, then write add_task_dep) — #166 closes
    the shared-layer check-then-write TOCTOU race at the db layer by
    wrapping would_create_task_dep_cycle + the INSERT in ONE atomic
    `BEGIN IMMEDIATE` transaction inside `db.add_task_dep` itself, raising
    `db.CycleError` (rollback first) if a concurrent writer's edge landed
    between THIS pre-check above and add_task_dep's own write. Ariadne's
    colleague catch on PR #245: #245's two HTTP routes catch CycleError
    and map it to 409, but this effect originally called add_task_dep
    uncaught — a race-caught cycle propagated as an uncaught exception
    (a 500) instead of surfacing in `effect_failures`. Fixed by wrapping
    the add_task_dep call below in `try/except db.CycleError`, same
    posture as the routes. The pre-check above is kept (not redundant):
    it gives a clean early failure in the common non-race case, same as
    it always did; the try/except only matters for the race window.

    Best-effort, same contract as the other project-board effects. Returns
    None on success, or a short failure reason string surfaced in
    `effect_failures`.
    """
    board_project = (thing.get("metadata") or {}).get("project_id")
    if board_project is None:
        return "board thing is not bound to a project (no metadata.project_id)"

    task_arg = effect["task_arg"]
    task_id = args.get(task_arg)
    if not isinstance(task_id, str) or not task_id.strip():
        return f"task_arg {task_arg!r} value is missing or not a string"

    depends_on_arg = effect["depends_on_arg"]
    depends_on_task_id = args.get(depends_on_arg)
    if not isinstance(depends_on_task_id, str) or not depends_on_task_id.strip():
        return (f"depends_on_arg {depends_on_arg!r} value is missing or "
                "not a string")

    task = db.get_task(conn, task_id)
    if task is None:
        return f"task {task_id!r} not found"
    depends_on_task = db.get_task(conn, depends_on_task_id)
    if depends_on_task is None:
        return f"task {depends_on_task_id!r} not found"

    # SAME-PROJECT BOUND (umwelt #230 — load-bearing, do not omit): mirrors
    # the /umwelt/tasks/<id>/deps route's own gate exactly.
    if task["project_id"] != depends_on_task["project_id"]:
        return (f"task {task_id!r} (project {task['project_id']!r}) and "
                f"depends_on_task_id {depends_on_task_id!r} (project "
                f"{depends_on_task['project_id']!r}) are not in the same "
                "project")

    # BOARD-SCOPE BOUND (ob_2892 — load-bearing, do not omit; the reviewer-
    # fairy fix): the same-project-as-each-other check above is NOT enough
    # on its own — it never verifies against the ACTING BOARD's own bound
    # project, so without this check a board bound to project A could
    # write a dep edge entirely inside an unrelated project B.
    if task["project_id"] != board_project:
        return (f"task {task_id!r} (project {task['project_id']!r}) is "
                "not in this board's project")

    remove = effect["remove"]
    if remove:
        db.remove_task_dep(conn, task_id, depends_on_task_id)
        return None

    if db.would_create_task_dep_cycle(conn, task_id, depends_on_task_id):
        return (f"adding this dependency would create a cycle "
                f"(task {task_id!r} -> depends_on {depends_on_task_id!r})")
    # #166 gap (Ariadne's colleague catch, PR #245): would_create_task_dep_
    # cycle above is only a pre-check — db.add_task_dep's own atomic
    # BEGIN IMMEDIATE guard (umwelt #166) can still raise db.CycleError if
    # a concurrent writer lands the reverse edge between this pre-check
    # and add_task_dep's write. The two HTTP routes catch CycleError and
    # map it to 409; this effect must catch it too so a race-caught cycle
    # surfaces as an effect_failures reason (best-effort, same contract as
    # every other effect failure in this function) instead of propagating
    # as an uncaught exception / 500.
    try:
        db.add_task_dep(conn, task_id, depends_on_task_id)
    except db.CycleError as e:
        return str(e)
    return None


def _apply_effect_project_set(conn, thing: dict, actor: str, args: dict,
                              effect: dict) -> str | None:
    """`project_set` effect (umwelt #227 S4): the project-lifecycle leg of
    the board's structure-editing surface — sets one field
    (`effect.field`, restricted to `db._MUTABLE_PROJECT_COLS`) on the
    acting board thing's OWN bound project row (`thing.metadata.
    project_id`), symmetric to `project_task_set` but with no `task_arg`
    at all — there is exactly one project a bound board can ever write,
    itself, so nothing in `args` or the effect schema can name a
    different one. The binding is server-owned (assigned only by
    `db.create_project_with_board`, refused on caller metadata; umwelt
    #879), which is what makes that "exactly one project" claim hold.

    Unlike `project_task_set`, the value is always `args[effect.
    value_arg]` — there is no `value_actor` option here (setting a
    project's label/description/status "to the acting agent" isn't a
    meaningful operation the way claiming a task's turn_holder is).

    NO completeness gate in v0 (umwelt #227 spec, Ariadne's design call,
    flagged for Kepler/Lei): marking a project `status="done"` does NOT
    require every task under it to be done first — a human archiving or
    pausing a project regardless of in-flight task state is the more
    common real-world shape, and over-gating this would fight the "just
    use it" goal the board exists for. Revisit if that call changes.

    Best-effort, same contract as the other project-board effects. Returns
    None on success, or a short failure reason string surfaced in
    `effect_failures`.
    """
    board_project = (thing.get("metadata") or {}).get("project_id")
    if board_project is None:
        return "board thing is not bound to a project (no metadata.project_id)"

    value_arg = effect["value_arg"]
    value = args.get(value_arg)
    if not isinstance(value, str):
        return f"value_arg {value_arg!r} value is missing or not a string"

    field = effect["field"]
    if field == "status" and value not in _PROJECT_STATUS:
        return f"invalid status {value!r}"
    # umwelt #874: only the label is a one-line surface. Checked before any write.
    if field == "label":
        # umwelt #253/#879: same glanceable-name cap as the direct
        # POST /projects/<id>/update route.
        if len(value) > _NAME_MAX_LEN:
            return (f"label exceeds the {_NAME_MAX_LEN}-char limit "
                    f"(got {len(value)}) — a label is a glanceable name; "
                    "put detail in description")
        label_problem = _name_char_problem(value)
        if label_problem:
            return f"label {label_problem}"

    project = db.get_project(conn, board_project)
    if project is None:
        return f"project {board_project!r} not found"

    db.update_project(conn, board_project, {field: value}, db.now_iso())
    return None


def _apply_effect_project_create(conn, thing: dict, actor: str, args: dict,
                                 effect: dict) -> str | None:
    """`project_create` effect (umwelt #227 S4): the registrar's "found a
    new project" leg — rides the EXACT SAME atomic room+board birth path
    S3a's `/umwelt/projects` HTTP route uses (`db.create_project_with_
    board`), just reached through a kind-declared act() effect (the
    `project_registrar` singleton's `found_project` action) instead of a
    direct POST.

    THE ONE DELIBERATELY UNBOUND EFFECT in the S4 family: every other
    project-board effect (`project_task_set`, `project_task_create`,
    `project_task_link`, `project_set`) is scoped to the acting board's
    OWN bound project (`thing.metadata.project_id`) — but this effect
    MINTS a brand-new project, so there is no existing project to scope it
    to. That's safe-by-construction on a different axis, not an exemption
    from one: it only ever creates NEW rows (no cross-project write reach
    into anything that already exists), and `/umwelt/projects` itself is
    already `anyone`-callable in v0 — routing project creation through a
    kind-declared registrar action grants no authority beyond what any
    caller already has via the HTTP route.

    Depends on the `project_board` kinddef singleton (`_PROJECT_BOARD_KIND_
    ID`) already existing — ensured once at app-boot time (create_app),
    NOT re-derived here: act()'s effect-dispatch signature is
    `(conn, thing, actor, args, effect)` with no `things_dir`, and
    `_ensure_project_board_kinddef` only needs `things_dir` on its cold-
    forge branch (see that function's docstring) — boot-time ensure means
    this effect never hits that branch, so it can reference the fixed
    canonical id directly instead of threading `things_dir` through the
    whole act() dispatch chain for one caller.

    Best-effort, same contract as the other project-board effects. Returns
    None on success, or a short failure reason string surfaced in
    `effect_failures`.
    """
    label_arg = effect["label_arg"]
    label = args.get(label_arg)
    if not isinstance(label, str) or not label.strip():
        return f"label_arg {label_arg!r} value is missing or not a string"
    label = label.strip()
    # umwelt #253: same glanceable-name cap as the direct HTTP route
    # (/umwelt/projects) enforces — this effect reaches the exact same
    # db.create_project_with_board write, so it needs the same gate.
    if len(label) > _NAME_MAX_LEN:
        return (f"label exceeds the {_NAME_MAX_LEN}-char limit "
                f"(got {len(label)}) — a label is a glanceable name; put "
                "detail in description")
    # umwelt #874: same one-line rule as the direct route.
    label_problem = _name_char_problem(label)
    if label_problem:
        return f"label {label_problem}"

    description = ""
    description_arg = effect.get("description_arg")
    if description_arg:
        description_val = args.get(description_arg)
        if description_val is not None:
            if not isinstance(description_val, str):
                return (f"description_arg {description_arg!r} value must "
                        "be a string")
            description = description_val

    board_kind = db.get_thing(conn, _PROJECT_BOARD_KIND_ID)
    if board_kind is None:
        return ("project_board kinddef singleton missing — boot-time "
                "ensure did not run")

    project_id = "proj_" + str(uuid.uuid4())
    place_id = "place_project_" + uuid.uuid4().hex
    board_thing_id = str(uuid.uuid4())
    now = db.now_iso()
    db.create_project_with_board(
        conn, project_id, label, description, actor, now, now,
        place_id=place_id, place_label=label,
        place_description=f"The Workshop room for project {label!r}.",
        board_thing_id=board_thing_id, board_kind_id=_PROJECT_BOARD_KIND_ID,
        # umwelt #253 (Kepler's ruling, forum #267): the board sits in the
        # project's own room with its kind already rendered — "<label>
        # board" says "board" twice and burns 6 of the 40-char name
        # budget saying so. board_name = label, full stop.
        board_name=label,
        board_description=f"The project board for {label!r}.",
    )
    return None


def _apply_effect_agent_state_set(conn, thing: dict, actor: str, args: dict,
                                   effect: dict) -> str | None:
    """`agent_state_set` effect (umwelt #191): the write-path for the
    generic, engine-verified per-(thing, agent) KV store. Sets one key
    (`effect.key`, restricted at kinddef-creation time to the kind's own
    declared `state_keys` — see _validate_kinddef_source) on `thing`'s
    thing_agent_state row for the ACTING agent, to either the acting agent
    itself (`effect.value_actor`) or `args[effect.value_arg]`
    (`effect.value_arg`) — exactly one of the two is present, enforced at
    kinddef-creation time. SAME value-source shape as
    _apply_effect_project_task_set.

    CALLER-ROW-ONLY BOUND (umwelt #191 security acceptance criterion A —
    load-bearing, do not omit): `actor` is act()'s own server-derived
    `agent` variable (the SAME already-canonicalized agent
    _apply_effect_notify_author / _apply_effect_project_task_set receive) —
    there is no field ANYWHERE in the effect schema or in `args` that names
    the agent whose row gets written. A crafted `args` payload can smuggle
    an "agent" key, but nothing here ever reads it: the row this call
    writes is always `(thing.id, actor, effect['key'])`, structurally, not
    by a runtime check that could be bypassed. Cross-agent writes are not
    merely rejected — there is no code path that could reach one.

    Best-effort, SAME contract as the other three effects: runs after the
    action's transition+log commit, never rolls that back. Returns None on
    success, or a short failure reason string on failure (surfaced in
    `effect_failures`, same as the siblings).

    ROW-BOUND (umwelt #191): a NEW key (one `actor` doesn't already hold on
    this thing) is refused once `actor` already holds
    _AGENT_STATE_MAX_KEYS_PER_THING distinct keys on this thing — a clean,
    loud failure (this same effect_failures channel), never a silent drop
    and never an eviction of an older key. Re-setting a key `actor` already
    holds is always an upsert (no new row), so it never counts against the
    cap a second time.
    """
    key = effect["key"]

    if effect.get("value_actor"):
        value = actor
    else:
        value_arg = effect["value_arg"]
        value = args.get(value_arg)
        if not isinstance(value, str):
            return f"value_arg {value_arg!r} value is missing or not a string"

    existing = db.get_agent_state(conn, thing["id"], actor)
    if key not in existing:
        if len(existing) >= _AGENT_STATE_MAX_KEYS_PER_THING:
            return (
                f"agent {actor!r} already holds "
                f"{_AGENT_STATE_MAX_KEYS_PER_THING} state key(s) on thing "
                f"{thing['id']!r} — cap reached"
            )

    db.set_agent_state(conn, thing["id"], actor, key, value, db.now_iso())
    return None


def _apply_effect_grant_caller(conn, thing: dict, actor: str, args: dict,
                                effect: dict) -> str | None:
    """`grant_caller` effect (umwelt #400): the ONLY write path for a
    `thing_grants` row — the DB-backed authorization the `granted`
    caller class checks (`db.has_grant`, act()'s authorization loop).
    This is #400's requirement 2: granting must itself be permissioned,
    and a grantee must not be able to escalate themselves.

    SECURITY-CRITICAL who-may-grant gate (load-bearing, do not omit): the
    ACTING agent (`actor` — act()'s own server-derived agent, the SAME
    already-canonicalized agent every other effect function receives)
    must satisfy ONE of:
      (a) `actor == thing["author"]` — the author can always grant/revoke,
          structurally, no row needed (mirrors how the `author`
          caller-class itself needs no grant row); or
      (b) `db.agent_can_grant(conn, thing["id"], actor)` — an EXISTING
          grant row for `actor` on this thing with `can_grant=1`.
    Neither holding is a clean effect failure (this same effect_failures
    channel), never a silent no-op and never a write. This is what makes
    self-escalation structurally impossible: a `can_grant=0` agent has no
    path to reach the write below, for themselves or anyone else — there
    is no runtime check further down that could be bypassed by a crafted
    `args` payload, because the gate is evaluated BEFORE `args` is even
    read for the target agent.

    `effect.agent_arg` names the `args` key holding the target agent
    (caller-suppliable — the whole point is naming WHO at call time, same
    "value comes from args, not a kinddef-time literal" shape
    `project_task_set`'s `value_arg` uses). Resolved through
    `_canonical_agent`, same fold every other identity comparison in this
    module uses.

    CAN_GRANT DEFAULTS FALSE (umwelt #400 round 3, Borges security
    must-fix, load-bearing, do not omit): `effect.can_grant_arg`, if the
    kinddef declares it, names the `args` key the CALLER (the granter, at
    call time) supplies a bool in — SAME "value comes from args, not a
    kinddef-time literal" shape `agent_arg` above uses, this time for the
    delegation-rights choice rather than the target identity. Borges's
    ruling, quoted: "a literal in the source is explicit to the
    implementer, not to the granter... the person doing the granting never
    makes the choice." A previous revision baked `can_grant` in as a
    kinddef-author literal, which meant EVERY successful grant silently
    conferred full delegate rights with no way for the granter to choose
    content-access-only. Resolution, in order:
      1. kinddef doesn't declare `can_grant_arg` at all -> False, always
         (this action can never grant delegation rights, by the kinddef
         author's own choice — matches the old "author decides once"
         intent, just no longer smuggled through a value the granter
         never sees).
      2. kinddef declares it but the caller's `args` doesn't contain that
         key -> False (the granter didn't opt in).
      3. the caller's `args` contains that key but the value isn't a
         strict JSON bool `true` (e.g. the string `"true"`, `1`, `"yes"`)
         -> effect failure (loud, this same effect_failures channel) —
         never a silent coercion of a caller-supplied value on a
         security-sensitive field. There is no existing caller-suppliable
         boolean `args` convention elsewhere in this module to defer to
         (every other `args` value this module reads is validated as a
         non-empty string — see `missing_args`'s `requires` check and
         every `*_arg` effect field above), so strict-bool acceptance
         (`args.get(key) is True`) was chosen as the reasoned default,
         consistent with this module's general preference for
         `isinstance(x, bool)` / `is not True` strict-type checks over
         truthy coercion wherever a bool is kinddef- or caller-supplied
         (e.g. `value_actor`'s `is not True` check, `remove`'s
         `isinstance(..., bool)` check, both in
         `_validate_kinddef_source` above).
      4. the caller's `args` contains that key with literal JSON `true`
         -> True.
    This can_grant resolution is ENTIRELY ORTHOGONAL to the who-may-grant
    gate above: that gate has already run and already returned by the time
    any of this is evaluated, so nothing here can let a `can_grant=0`
    agent reach the write below — see that gate's docstring section for
    why self-escalation stays structurally impossible.

    Best-effort, SAME contract as every other effect: runs after the
    action's transition+log commit, never rolls that back. Returns None
    on success, or a short failure reason string on failure (surfaced in
    `effect_failures`).
    """
    if actor != _canonical_agent(thing["author"]) and not db.agent_can_grant(
            conn, thing["id"], actor):
        return (
            f"agent {actor!r} may not grant access on thing "
            f"{thing['id']!r} — not the author and holds no can_grant=1 "
            "row"
        )

    agent_arg = effect["agent_arg"]
    target_raw = args.get(agent_arg)
    if not isinstance(target_raw, str) or not target_raw.strip():
        return f"agent_arg {agent_arg!r} value is missing or not a string"
    target_agent = _canonical_agent(target_raw)

    can_grant_arg = effect.get("can_grant_arg")
    if can_grant_arg is None:
        can_grant = False
    elif can_grant_arg not in args:
        can_grant = False
    else:
        can_grant_raw = args.get(can_grant_arg)
        if not isinstance(can_grant_raw, bool):
            return (
                f"can_grant_arg {can_grant_arg!r} value must be a literal "
                "bool"
            )
        can_grant = can_grant_raw

    db.set_grant(conn, thing["id"], target_agent, can_grant, actor,
                 db.now_iso())
    return None


def _apply_effect_revoke_caller(conn, thing: dict, actor: str, args: dict,
                                 effect: dict) -> str | None:
    """`revoke_caller` effect (umwelt #400): removes a `thing_grants` row —
    the counterpart to `_apply_effect_grant_caller`, above, SAME
    security-critical who-may-grant gate (author, or an existing
    can_grant=1 row for `actor` on this thing) and SAME `agent_arg` shape.
    See that function's docstring for the full gate rationale — not
    repeated here.

    Idempotent: revoking an agent with no live grant row succeeds as a
    no-op (`db.revoke_grant`'s DELETE matches zero rows, which is not an
    error), same posture `project_task_link`'s `remove: true` path uses
    for a non-existent `task_deps` edge — "already in the target state"
    is success, not failure, throughout this effect family.

    Best-effort, SAME contract as every other effect. Returns None on
    success, or a short failure reason string on failure (surfaced in
    `effect_failures`).
    """
    if actor != _canonical_agent(thing["author"]) and not db.agent_can_grant(
            conn, thing["id"], actor):
        return (
            f"agent {actor!r} may not revoke access on thing "
            f"{thing['id']!r} — not the author and holds no can_grant=1 "
            "row"
        )

    agent_arg = effect["agent_arg"]
    target_raw = args.get(agent_arg)
    if not isinstance(target_raw, str) or not target_raw.strip():
        return f"agent_arg {agent_arg!r} value is missing or not a string"
    target_agent = _canonical_agent(target_raw)

    db.revoke_grant(conn, thing["id"], target_agent)
    return None


def _apply_effect_board_entry_add(
        conn, thing: dict, actor: str, args: dict,
        effect: dict) -> tuple[str | None, str | None]:
    """`board_entry_add` effect (umwelt #400): appends one entry to the
    acting thing's OWN `metadata.entries` array — the charge-board's
    structure-editing surface, a bounded server-owned array with the SAME
    posture `metadata.action_log`/`_ACTION_LOG_MAX_ENTRIES` already use,
    deliberately NOT a reuse of the `project_task_*` family (a board
    entry has no per-agent assignment, owner, or review-state lifecycle —
    umwelt #400's issue body, "no per-agent assignment, Kepler assigns in
    the morning").

    `effect.text_arg` names the `args` key holding the entry text
    (caller-suppliable — the whole point is a human/agent writes a NEW
    entry at call time), length-capped at `_BOARD_ENTRY_TEXT_MAX_LEN`
    (same author-controlled-text-needs-a-length-cap posture as
    `_PARAM_DESCRIPTION_MAX_LEN`, applied here at act()-time since the
    text arrives via `args` rather than a kinddef-time literal).

    Re-reads the thing fresh via `db.get_thing` rather than trusting the
    `thing` snapshot act() passed in (that snapshot predates THIS action's
    own transition+log commit and any earlier effect in the same
    `effects` list — `effect_failures` is deliberately a LIST, not a
    type-keyed map, precisely because nothing forbids declaring two
    `board_entry_add` effects in one action; a stale read here would let
    the second silently clobber the first's append). Writes via
    `db.update_thing` on `metadata` — the same call every other
    thing-metadata write in this module uses; no second write path is
    invented.

    Best-effort, SAME contract as every other effect: runs after the
    action's transition+log commit, never rolls that back. Returns
    `(None, new_entry_id)` on success — umwelt #836/#267: the SAME
    `(reason, entry_id)` shape `_apply_effect_board_entry_retire`/
    `_apply_effect_board_entry_edit` use, read back by act()'s dispatch
    loop into the `effect_results` side-channel (umwelt #663's own
    precedent for a successful effect handing data back to the caller —
    see `_apply_effect_source_append`'s `new_length`) so a caller can
    recover the new entry's id from act()'s response without a follow-up
    `look_at` — or `(reason, None)` on failure (surfaced in
    `effect_failures`) — text missing/empty/too long, or the board
    already at `_BOARD_ENTRIES_MAX_COUNT` ACTIVE entries (retired entries
    don't count against the cap — retiring is a soft mark, not a prune,
    but it DOES free capacity for a new add; see
    `_apply_effect_board_entry_retire`).
    """
    text_arg = effect["text_arg"]
    text = args.get(text_arg)
    if not isinstance(text, str) or not text.strip():
        return f"text_arg {text_arg!r} value is missing or not a non-empty string", None
    text = text.strip()
    if len(text) > _BOARD_ENTRY_TEXT_MAX_LEN:
        return (
            f"text exceeds the {_BOARD_ENTRY_TEXT_MAX_LEN}-char limit "
            f"(got {len(text)})"
        ), None

    current = db.get_thing(conn, thing["id"])
    if current is None or current["status"] != "live":
        return "thing is no longer live", None
    entries = list(current["metadata"].get("entries") or [])
    active_count = sum(1 for e in entries if e.get("retired_at") is None)
    if active_count >= _BOARD_ENTRIES_MAX_COUNT:
        return (
            f"board already holds {_BOARD_ENTRIES_MAX_COUNT} active entries "
            "(cap reached) — retire stale entries to free capacity"
        ), None

    new_entry_id = "entry_" + str(uuid.uuid4())
    entries.append({
        "id": new_entry_id,
        "text": text,
        "created_by": actor,
        "created_at": db.now_iso(),
        "retired_at": None,
    })
    new_metadata = dict(current["metadata"])
    new_metadata["entries"] = entries
    db.update_thing(conn, thing["id"], {"metadata": new_metadata}, actor)
    return None, new_entry_id


def _apply_effect_board_entry_retire(
        conn, thing: dict, actor: str, args: dict,
        effect: dict) -> tuple[str | None, str | None]:
    """`board_entry_retire` effect (umwelt #400): SOFT-retires one entry
    in the acting thing's `metadata.entries` array — sets `retired_at`,
    NEVER deletes the entry (the design decision that "aged out by date"
    is a read-side concern, not a prune step here). Counterpart to
    `_apply_effect_board_entry_add`, above; same fresh-re-read-then-
    `db.update_thing` write path — see that function's docstring for why
    (avoids clobbering a same-action sibling effect's write).

    `effect.entry_id_arg` names the `args` key holding the target entry's
    `id`. A missing/absent `entry_id_arg` value, or an id that doesn't
    match ANY entry (live or already-retired) on this board, is a clean
    effect failure — same "not found is a failure" posture
    `project_task_link` uses for an unknown task id, NOT the idempotent-
    no-op posture reserved for "found, and already in the target state"
    (below).

    Re-retiring an ENTRY THAT EXISTS but is already retired is treated as
    an idempotent no-op success — the entry's original `retired_at` is
    left untouched, not overwritten with a new timestamp — the same
    "already in the target state is success, not failure" posture
    `project_task_link`'s `remove: true` uses for re-removing an edge
    that's already gone. (Flagged as a judgment call in the umwelt #400
    build handoff: the alternative — failing loud on a double-retire, to
    keep the audit trail's `retired_at` meaningful as "the first time
    this happened" — is also defensible; this implementation picked the
    idempotent-success reading because #400's board is a shared,
    multi-writer surface where two agents racing to retire the same
    stale entry is a plausible, benign event, not a bug to surface.)

    Best-effort, SAME contract as every other effect. Returns
    `(None, entry_id)` on success (umwelt #836: echoes back the SAME
    `entry_id` it acted on — matches `_apply_effect_board_entry_add`'s/
    `_apply_effect_board_entry_edit`'s `(reason, entry_id)` shape, read
    into act()'s `effect_results`), including on the idempotent-no-op
    path (the id is already known-valid there — it was just matched
    against an existing entry), or `(reason, None)` on failure (surfaced
    in `effect_failures`).
    """
    entry_id_arg = effect["entry_id_arg"]
    entry_id = args.get(entry_id_arg)
    if not isinstance(entry_id, str) or not entry_id.strip():
        return f"entry_id_arg {entry_id_arg!r} value is missing or not a string", None

    current = db.get_thing(conn, thing["id"])
    if current is None or current["status"] != "live":
        return "thing is no longer live", None
    entries = list(current["metadata"].get("entries") or [])
    idx = next((i for i, e in enumerate(entries) if e.get("id") == entry_id), None)
    if idx is None:
        return f"no entry {entry_id!r} on this board", None

    if entries[idx].get("retired_at") is not None:
        return None, entry_id  # idempotent no-op — see docstring

    entries[idx] = dict(entries[idx])
    entries[idx]["retired_at"] = db.now_iso()
    new_metadata = dict(current["metadata"])
    new_metadata["entries"] = entries
    db.update_thing(conn, thing["id"], {"metadata": new_metadata}, actor)
    return None, entry_id


def _apply_effect_board_entry_edit(
        conn, thing: dict, actor: str, args: dict,
        effect: dict) -> tuple[str | None, str | None]:
    """`board_entry_edit` effect (umwelt #836): edits ONE entry's `text`
    in place in the acting thing's OWN `metadata.entries` array — the
    charge-board's missing correction verb. Before this effect existed, a
    one-word fix cost a retire+add: a brand-new `entry_id` and the old
    entry's `created_by`/`created_at` provenance gone. Counterpart to
    `_apply_effect_board_entry_add`/`_apply_effect_board_entry_retire`,
    above; same fresh-re-read-then-`db.update_thing` write path — see
    `_apply_effect_board_entry_add`'s docstring for why (avoids
    clobbering a same-action sibling effect's write).

    `effect.entry_id_arg`/`effect.text_arg` name the `args` keys holding
    the target entry's `id` and its NEW text — the new text is
    length-capped at `_BOARD_ENTRY_TEXT_MAX_LEN`, same posture and same
    failure-message shape as `_apply_effect_board_entry_add`'s own cap
    check (an edit is still caller-controlled text; it needs the same
    guard a fresh add does).

    Only an ACTIVE (non-retired) entry may be edited: an unknown
    `entry_id` is a clean effect failure, same "not found is a failure"
    posture `_apply_effect_board_entry_retire` uses for its own unknown-id
    case. An entry that EXISTS but is already retired is ALSO a failure —
    deliberately NOT `_apply_effect_board_entry_retire`'s idempotent-no-op
    posture for "already retired": there is no sensible target state to
    be idempotent toward here (unlike re-retiring, which converges on the
    same already-`retired_at` state, an edit's target state is "the NEW
    text was applied", which never becomes true for a retired entry no
    matter how many times it's attempted) — a retired entry's content is
    frozen; its history stops at retirement, and editing it is refused
    rather than silently accepted or silently ignored.

    On success, the entry keeps its ORIGINAL `id`/`created_by`/
    `created_at`/`retired_at` — only `text` changes, plus two new fields
    this effect adds: `edited_at` (`db.now_iso()`) and `edited_by`
    (`actor`). `_render_charge_board` (umwelt #836 C6 — the read side is
    explicitly out of scope for this change) reads neither new field, so
    an edited entry renders identically to a never-edited entry with the
    same final text. Produces exactly ONE `action_log` row — the normal
    act()-level transition+log commit every action already gets; this
    effect appends no second log entry of its own.

    Best-effort, SAME contract as every other effect. Returns
    `(None, entry_id)` on success (the SAME `(reason, entry_id)` shape
    `_apply_effect_board_entry_add`/`_apply_effect_board_entry_retire`
    use, read into act()'s `effect_results`), or `(reason, None)` on
    failure (surfaced in `effect_failures`), with `entries` left
    unchanged on any failure path.
    """
    entry_id_arg = effect["entry_id_arg"]
    text_arg = effect["text_arg"]
    entry_id = args.get(entry_id_arg)
    if not isinstance(entry_id, str) or not entry_id.strip():
        return f"entry_id_arg {entry_id_arg!r} value is missing or not a string", None

    text = args.get(text_arg)
    if not isinstance(text, str) or not text.strip():
        return f"text_arg {text_arg!r} value is missing or not a non-empty string", None
    text = text.strip()
    if len(text) > _BOARD_ENTRY_TEXT_MAX_LEN:
        return (
            f"text exceeds the {_BOARD_ENTRY_TEXT_MAX_LEN}-char limit "
            f"(got {len(text)})"
        ), None

    current = db.get_thing(conn, thing["id"])
    if current is None or current["status"] != "live":
        return "thing is no longer live", None
    entries = list(current["metadata"].get("entries") or [])
    idx = next((i for i, e in enumerate(entries) if e.get("id") == entry_id), None)
    if idx is None:
        return f"no entry {entry_id!r} on this board", None
    if entries[idx].get("retired_at") is not None:
        return f"entry {entry_id!r} is retired — cannot edit", None

    entries[idx] = dict(entries[idx])
    entries[idx]["text"] = text
    entries[idx]["edited_at"] = db.now_iso()
    entries[idx]["edited_by"] = actor
    new_metadata = dict(current["metadata"])
    new_metadata["entries"] = entries
    db.update_thing(conn, thing["id"], {"metadata": new_metadata}, actor)
    return None, entry_id


def _apply_effect_action_log_rotate(conn, thing: dict, actor: str,
                                     args: dict, effect: dict) -> str | None:
    """`action_log_rotate` effect (umwelt #736): archives the acting
    thing's OWN `metadata.action_log` to a fresh `readable` archive thing
    in the SAME place, then resets the live thing's `action_log` to a
    single marker entry — the primitive that relieves
    `_ACTION_LOG_MAX_ENTRIES` (act()'s cap on this same array) without the
    forge-a-brand-new-thing workaround, which loses identity (a new thing
    id — anything referencing the old thing now points at an archived
    artifact), loses grants (`thing_grants` rows are keyed to `thing_id`
    and don't travel to a new id), and resets the clock without stopping
    it (a fresh thing inherits the same 200-entry cap and starts filling
    it again from zero). Same `thing_id`/`metadata.entries`/`thing_grants`
    UNTOUCHED — only `metadata.action_log` changes on the live thing.

    Re-reads the thing fresh via `db.get_thing` rather than trusting the
    `thing` snapshot act() passed in — same reason
    `_apply_effect_board_entry_add`'s docstring gives: by the time this
    effect runs, act()'s pre-effects transition+log commit has ALREADY
    appended THIS rotate-log call's own log entry to `action_log`. That
    entry is what gets archived along with everything else below — this
    is correct (the rotate-log call is itself part of the history being
    rotated), not a bug to special-case around.

    If `existing_log` holds at most 1 entry (nothing but this call's own
    just-committed entry, or genuinely empty), there is nothing to
    archive: fails loud with "nothing to rotate" rather than a silent
    no-op success, same fail-loud posture `_apply_effect_board_entry_add`
    uses for its own cap check.

    The archive thing is authored by `actor` (the agent that called
    rotate-log), NOT `"engine"` — this is an agent-initiated forge, and
    provenance should say so, same posture every other agent-triggered
    effect-forge in this module uses (e.g. `_apply_effect_project_create`).
    Its id is a fresh `uuid.uuid4()` (same convention `project_create`'s
    `board_thing_id` uses for a dynamically-forged, non-singleton thing) —
    collision against an existing id is not realistically possible, but
    the `sqlite3.IntegrityError` path is still handled defensively (same
    "catch and roll back rather than let a 500 leak a partial write"
    posture `_ensure_project_registrar` uses for its own `create_thing`
    call), returning a failure reason rather than raising.

    On success, the live thing's `metadata.action_log` becomes a SINGLE
    marker entry (never an empty list) naming where the history went and
    how many entries it held — an empty log with no breadcrumb would be a
    discoverability dead end for the next reader trying to find out where
    a board's history disappeared to. Worth calling out explicitly in
    code (not just here) because it is the piece most likely to get
    "simplified" back to `[]` by a future reader who doesn't know why it
    isn't.

    Best-effort, SAME contract as every other effect: runs after the
    action's transition+log commit, never rolls that back. Returns None
    on success, or a short failure reason string on failure (surfaced in
    `effect_failures`).
    """
    current = db.get_thing(conn, thing["id"])
    if current is None or current["status"] != "live":
        return "thing is no longer live"

    existing_log = list(current["metadata"].get("action_log") or [])
    if len(existing_log) <= 1:
        return "nothing to rotate"

    timestamp = db.now_iso()
    archive_thing_id = str(uuid.uuid4())
    # umwelt #780 colleague review (Borges) — two corrections to the
    # original review-round fix:
    #
    # (1) The precedent cited here was misattributed. _apply_effect_
    # project_create does NOT call db.create_thing directly — it goes
    # through db.create_project_with_board — so it isn't actually a
    # sibling instance of "an engine-effect forge bypasses the HTTP
    # route's _NAME_MAX_LEN check". The real reason to TRUNCATE here
    # rather than REJECT (as that function's own `label` check does)
    # doesn't need a borrowed precedent: `label` there is a caller-
    # supplied literal, so rejecting and asking the caller to shorten it
    # is actionable. This archive_name is ENGINE-COMPOSED from caller-
    # influenced content (the board's own name) plus engine text — there
    # is no caller-supplied string to hand an error back about, and
    # failing the whole rotation over a naming cosmetic would be a worse
    # outcome than a shortened name.
    #
    # (2) A truncated f"{name} archive {timestamp}"[:_NAME_MAX_LEN] drops
    # the timestamp entirely once `name` alone fills the budget — measured:
    # a 36-char board name like "Charge Board — Lei's Office (vol. 3)"
    # (Borges's own real-world example, "Lei's Office charge board, vol. 3",
    # is 33 chars and hits the same failure) leaves zero room for it, so
    # every rotation of that board would produce a BYTE-IDENTICAL archive
    # name, defeating the one thing that would tell two archives apart in
    # a place listing. Use a short slice of `archive_thing_id` instead —
    # already generated, already guaranteed unique, and 8 hex chars
    # disambiguates any realistic rotation frequency without eating the
    # name budget a full ISO timestamp does. The full timestamp is
    # unaffected elsewhere — `archive_description` and
    # `metadata.archived_at` both still carry it in full.
    #
    # `max(0, ...)` guards the slice bound: if _NAME_MAX_LEN is ever
    # lowered below len(archive_suffix), a bare negative index here would
    # slice from the END of the string instead of raising or truncating
    # to nothing — silently producing a LONGER, differently-composed name
    # than the cap intends, the opposite of what this whole fix is for.
    archive_suffix = f" — log archive {archive_thing_id[:8]}"
    archive_name = (
        current["name"][:max(0, _NAME_MAX_LEN - len(archive_suffix))]
        + archive_suffix
    )
    archive_description = (
        f"Archived action_log from {current['name']!r}, rotated by "
        f"{actor} at {timestamp} — {len(existing_log)} entries."
    )
    try:
        db.create_thing(
            conn, id=archive_thing_id, place_id=current["place_id"],
            name=archive_name, description=archive_description,
            kind="readable", exec_command=None,
            metadata={
                "archived_from": thing["id"],
                "archived_at": timestamp,
                "archived_log": existing_log,
            },
            author=actor, created_at=timestamp,
            weight_est=db.compute_weight_est(archive_name, archive_description),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return "failed to forge archive thing (id collision) — try again"

    new_metadata = dict(current["metadata"])
    new_metadata["action_log"] = [{
        "action": "rotate-log",
        "by": actor,
        "at": timestamp,
        "args": {
            "archived_to": archive_thing_id,
            "archived_count": len(existing_log),
        },
    }]
    db.update_thing(conn, thing["id"], {"metadata": new_metadata}, actor)
    return None


def _apply_effect_source_append(
    conn, thing: dict, actor: str, args: dict, effect: dict,
    things_dir: Path,
) -> tuple[str | None, int | None]:
    """`source_append` effect (umwelt #663): appends caller-supplied text
    to ONE existing file in the acting thing's `source` tree, atomically —
    without the read-full/hand-merge/write-full cycle `update_thing`'s
    full-replace `source` patch requires today, where a slip mid-cycle
    silently destroys the whole document (umwelt #663's own incident
    report).

    `effect.path_arg`/`effect.text_arg` name the `args` keys holding the
    target relpath and the text to append, respectively — both required,
    validated non-empty (same "value comes from args, not a kinddef-time
    literal" `*_arg` posture every other effect in this module uses).
    `text` is checked for non-empty via plain truthiness (`not text`),
    deliberately NOT `.strip()`-then-check the way `board_entry_add`
    checks its own text arg: `text` here carries its own leading
    newline/separator when the caller wants one (see below), so a
    whitespace-only append (e.g. `"\\n"`) is a legitimate call, not a
    typo to reject.

    Rejects (does not create) a `path` absent from the thing's current
    `source` — Kepler's own spec for #663: "append only targets an
    existing file." No inserted separator between the existing content
    and the appended text — the caller's text carries its own leading
    newline/separator if it wants one, keeping the primitive predictable
    rather than guessing intent (Borges's explicit endorsement, #663
    review thread).

    Writes via `_append_source_file` — a temp-write-then-`os.replace` on
    the SINGLE target file, never a full `_replace_source` over the
    thing's entire source dict (see that helper's own docstring for why).

    Takes `things_dir` as an explicit parameter, UNLIKE every other
    `_apply_effect_*` function in this module: this effect reads/writes
    the filesystem, not the DB, and act()'s effect-dispatch signature
    otherwise has no `things_dir` to reach it from (see
    `_apply_effect_project_create`'s docstring for the parallel case of a
    module-level effect function that deliberately does NOT thread
    `things_dir` through, and why — that reasoning doesn't apply here
    because THIS effect's whole job is a source-tree write). `act()`
    itself is nested inside `create_app` and already has `things_dir` in
    closure scope (it's used directly a few lines above, e.g.
    `_resolve_kind_chain(g.conn, things_dir, ...)`), so the dispatch call
    site passes it through with no new plumbing beyond this function's
    own parameter list.

    Enriched return, UNLIKE every other `_apply_effect_*` function (which
    return `str | None`): `(reason, new_length)`. The shared `str | None`
    contract has no side-channel for a SUCCESSFUL effect to hand data
    back to its caller — Borges's #663 ruling was to add a channel for
    this at the act()-dispatch level (a parallel `effect_results` list,
    symmetric with the existing `effect_failures` list two lines up in
    that loop) rather than mutate the `effect` dict in place
    (`effect["_result"] = ...` would mutate a row that came out of the
    declared kinddef at request time — see act()'s dispatch arm for the
    full reasoning). This function's enriched return is what feeds that
    list; no other existing `_apply_effect_*` function's contract
    changes.

    Best-effort, SAME contract as every other effect otherwise: runs
    after the action's transition+log commit, never rolls that back.
    Returns `(None, new_length)` on success, or `(reason, None)` on
    failure (the reason surfaced in `effect_failures`).

    CONCURRENCY BOUND (umwelt #780 colleague review, Borges — stated
    here because an earlier draft of this docstring let the atomic
    single-file WRITE's guarantee read as a guarantee for the whole
    read-modify-write, which it is not): this function reads the
    current file content, computes `new_content` in Python, then calls
    `_append_source_file` to write it — nothing serializes that
    read-then-write against a second, concurrent call appending to the
    SAME `relpath` on the SAME thing. `_append_source_file`'s
    `os.replace` guarantees the file is never observed torn (fully old
    or fully new content, never a partial write); it does NOT guarantee
    no lost append. Two concurrent calls can both read the same
    pre-append content, and the LAST writer's `os.replace` wins — the
    first caller's text is silently dropped even though that call
    returned success and a `new_length` that no longer matches the
    file on disk. This server runs threaded (`threaded=True`), so two
    such calls CAN genuinely overlap; this is a measured risk, not a
    theoretical one — see umwelt#166 for a demonstrated DB-layer race
    in this same codebase from the same threading model. Out of scope
    for this PR (the primitive is still strictly safer than the
    full-replace cycle it replaces, which loses far more than one
    append on any mid-cycle failure); a real fix needs a per-`(thing_id,
    relpath)` write lock or a compare-and-swap on the read content,
    neither of which exists today.
    """
    path_arg = effect["path_arg"]
    relpath = args.get(path_arg)
    if not isinstance(relpath, str) or not relpath.strip():
        return (
            f"path_arg {path_arg!r} value is missing or not a non-empty "
            "string", None,
        )

    text_arg = effect["text_arg"]
    text = args.get(text_arg)
    if not isinstance(text, str) or not text:
        return (
            f"text_arg {text_arg!r} value is missing or not a non-empty "
            "string", None,
        )

    # umwelt #780 colleague review round 2 (Borges): _read_source_file
    # RAISES ValueError on a path that escapes the thing's source dir —
    # unlike the old `relpath not in current_source` membership test this
    # replaced, which returned a graceful reason for BOTH a missing path
    # and a traversal attempt alike. An uncaught raise here propagates out
    # of act()'s bare effects-dispatch loop as a 500, AFTER the action's
    # own transition+log commit has already landed — the exact outcome
    # the best-effort effect contract (every OTHER _apply_effect_* returns
    # a reason string, never raises) exists to prevent. Caught here and
    # reported as its OWN distinct reason — not folded into the
    # does-not-exist message below — so an attempted traversal stays
    # visible as what it is in effect_failures, rather than reading like
    # an ordinary typo'd path.
    try:
        existing_content = _read_source_file(things_dir, thing["id"], relpath)
    except ValueError:
        return (
            f"path {relpath!r} escapes the thing's source dir", None,
        )
    if existing_content is None:
        return (
            f"path {relpath!r} does not exist — append only targets an "
            "existing file", None,
        )

    new_content = existing_content + text
    _append_source_file(things_dir, thing["id"], relpath, new_content)
    return None, len(new_content)


def _substitute_blueprint_holes(value, values: dict[str, str]):
    """Single-pass {{hole}} substitution over a parsed blueprint.json
    structure — the load-bearing correctness property of the whole feature
    (Kepler's rider, umwelt #79). Recurses through dicts (values only, never
    keys) and lists; strings get exactly ONE `re.sub` pass; every other JSON
    scalar (bool/int/float/None) passes through untouched.

    Single-pass, not iterate-to-fixpoint: `re.sub`'s replacement callback
    return value is never re-scanned by `re.sub` itself (that's the Python
    stdlib's ordinary behavior, not something enforced here by hand) — so a
    substituted value that itself contains literal `{{...}}` text lands
    inert in the output. This function must never be called more than once
    on its own output, and never looped until stable — doing so would
    silently reopen the injection surface this single-pass design closes.

    An unrecognized hole (a name absent from `values` — e.g. an inner
    blueprint's own holes riding opaque through an outer/meta blueprint's
    template) is left completely untouched, verbatim. This is what lets the
    meta-blueprint fixed point carry a nested template through a stamp
    without the outer stamp ever touching it.

    WARNING — flat hole namespace, meta-blueprint author trap: substitution
    walks the template's serialized JSON like any other string, so a nested
    blueprint.json (carried as a JSON-encoded string value inside the outer
    template — see the meta-blueprint case) is just more string content to
    this function. It has NO notion of "outer" vs "inner" scope. The opaque
    carry that makes the meta-blueprint fixed point work depends entirely on
    the outer template's declared params never NAMING one of the inner
    template's holes — `values` is one flat dict for the whole substitution
    pass. If an outer blueprint declares a param (e.g. `item_name`) that
    collides with a `{{item_name}}` hole belonging to its nested inner
    template, that inner hole gets silently substituted by the OUTER stamp,
    even though it was meant to survive opaque for the inner blueprint's own
    later use(). This is not caller-exploitable (the blueprint author authors
    both the outer template and the inner template it embeds — same trust
    boundary), but it silently violates the documented fixed-point invariant
    above. Meta-blueprint authors MUST ensure their outer `metadata.params`
    names never shadow any `{{hole}}` name used inside the nested template
    they embed. See test_meta_blueprint_hole_namespace_collision in
    test_blueprint.py, which demonstrates (and locks in as current, documented
    behavior) exactly this collision.
    """
    if isinstance(value, str):
        return _BLUEPRINT_HOLE_RE.sub(
            lambda m: values.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _substitute_blueprint_holes(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_blueprint_holes(v, values) for v in value]
    return value


# umwelt #407: things whose `metadata.params` has already been reported as
# not-a-declaration, so the warning below fires ONCE per thing per process
# rather than on every single use. Process-local and deliberately unbounded
# — the grandfathered set is fixed and tiny (it can only shrink, since
# _validate_params_declaration makes new members impossible), so this can
# never grow without bound the way a cache keyed on caller input could.
_GRANDFATHERED_PARAMS_WARNED: set[str] = set()


def _declared_params(thing_id: str, thing: dict) -> list:
    """Resolve a thing's params DECLARATION, applying umwelt #407's
    grandfathering rule: a `metadata.params` that is not a list is not a
    declaration at all, and is ignored.

    Why ignore rather than reject (Borges's ruling on #407). `params` only
    became a declaration when #396/#402 gave it meaning; before that it was
    an ordinary metadata key, and at least one live thing — the Square's
    echo well — holds human prose there ("freeform — every arg you pass is
    echoed back"). #402's `_validate_params_declaration` correctly refuses
    that shape at authoring time, so no NEW value can be a non-list; the
    only non-lists reachable here are grandfathered ones, written under
    rules that did not yet exist by authors who cannot be asked to have
    complied. Retroactive meaning assignment must not fail loudly on
    pre-existing data. Without this, `use()` on the echo well returned
    "metadata.params must be an array" instead of echoing — the feature
    breaking its own motivating example (#407 has the reproduction).

    Note this is NOT fail-open in the security sense: a params declaration
    gates arity/enum/type — validation, not authorization. It constrains
    the SHAPE of a call, never access to it, so there is no control here to
    bypass by ignoring it.

    Deliberately narrow. A `params` that IS a list but holds malformed
    entries still flows through to `_bind_params` and still fails loudly:
    that is a real attempt at a declaration by an author who can see the
    error and fix it. Only the non-list case is treated as inert. "A
    non-list is not a declaration" is the rule; "ignore malformed params"
    would be too big a rule and would swallow live authoring mistakes.
    """
    raw = (thing.get("metadata") or {}).get("params")
    if raw is None:
        return []
    if not isinstance(raw, list):
        if thing_id not in _GRANDFATHERED_PARAMS_WARNED:
            _GRANDFATHERED_PARAMS_WARNED.add(thing_id)
            print(
                f"[params-grandfathered] {thing_id!r} has a non-list "
                f"metadata.params ({type(raw).__name__}) — ignoring it as "
                "not-a-declaration and running the thing as before "
                "(umwelt #407). This value predates the params "
                "declaration validator; it cannot be authored today.",
                file=sys.stderr,
            )
        return []
    return raw


def _bind_params(params: list, args: list[str]) -> tuple[dict[str, str], str | None]:
    """Positional-bind use()'s validated args onto a thing's declared param
    schema (metadata.params — an ORDERED list of {name, required, default,
    type, enum, description}; umwelt #396 generalizes this from #79
    slice-1's blueprint-only {name, required, default} shape onto
    executables too, and replaces _bind_blueprint_args, the sole prior
    caller — one binder, so blueprint and executable binding semantics can
    never drift). params[i] binds to args[i]; a param with no corresponding
    arg falls back to its declared default (optional) or is a named 422
    (required). More args than declared params is also a named 422 — the
    engine never silently drops a caller-supplied value.

    NEW over the blueprint-only predecessor:
      - enum enforcement: the bound value (the raw arg, or the resolved
        default when the arg was omitted) must be a member of a declared
        `enum` list — a string-equality check, since use()'s args are
        always raw strings regardless of the param's declared `type`.
      - type enforcement: `type: "number"`/`"boolean"` — a STRING bound
        value must parse as that type (float()-parseable; case-insensitive
        "true"/"false" for boolean). A non-string default (e.g. a literal
        JSON `5`/`true` sitting in the declaration itself) already IS the
        right type and needs no parsing.
    No coercion, no silent dropping — same house style as
    _validate_use_args's whole existence ("No coercion, no silent
    dropping"). A mismatch is always a loud, named rejection; the returned
    `values` dict holds the RAW bound strings/defaults UNCHANGED — this
    type enforcement is a check, not a transform, so a downstream
    executable's argv still receives exactly what was typed, same as
    today's happy path.

    umwelt#414 (reviewer-fairy catch on PR #415, worth being explicit
    about): this function is a VALIDATION gate, not a value-substitution
    step, for the executable path specifically. The blueprint-stamping
    caller (create_thing/update_thing) DOES use this function's returned
    `values` as the actual params to stamp — so an optional param left
    blank there really does resolve to its declared default in the
    stamped metadata. The executable caller (use()) does not: it still
    hands `sandbox.run_thing_capped` the ORIGINAL `args` list, blank
    strings included, never `values`. So for an executable, "optional +
    blank now passes validation and reports a resolved default" and "the
    process actually receives that default on argv" are two different
    claims — only the first is true. This function decides whether a call
    is ADMITTED, not what the executable sees; that split predates this
    fix and this fix does not change it, but the blank-string case makes
    the gap more visible than the type/enum checks did.

    Returns (values, None) on success, or ({}, error_message) on the first
    violation found.
    """
    if not isinstance(params, list):
        return {}, "metadata.params must be an array"
    if len(args) > len(params):
        return {}, (f"too many args: args[{len(params)}] has no corresponding "
                    f"declared param ({len(params)} param(s) declared, "
                    f"got {len(args)})")
    values: dict[str, str] = {}
    for i, p in enumerate(params):
        if not isinstance(p, dict) or not p.get("name"):
            return {}, f"metadata.params[{i}] must be an object with a name"
        pname = p["name"]
        # umwelt #414: a form sends a DENSE prefix, so any field left blank
        # arrives as "" rather than being omitted from `args` entirely —
        # "no corresponding arg" and "blank" reach here as the same shape.
        # An empty string is therefore treated as absent for every param,
        # required or optional, same as a truly-omitted trailing arg: falls
        # back to the declared default (optional) or is a named 422
        # (required, with a message distinguishing "blank" from "omitted"
        # since the two are different caller mistakes to debug).
        if i < len(args) and args[i] != "":
            values[pname] = args[i]
        elif i < len(args) and p.get("required", False):
            return {}, f"param {pname!r} is required (got an empty value)"
        elif i >= len(args) and p.get("required", False):
            return {}, f"missing required param {pname!r}"
        else:
            values[pname] = p.get("default")

        bound = values[pname]
        enum = p.get("enum")
        # umwelt#420 (Ariadne, live-caught post-#415): an unset optional
        # param with no declared default binds to `None` -- the number/
        # boolean checks below already skip `None` via their own
        # `isinstance(bound, str)` guard, but this enum check had no
        # equivalent, so it was the only validator that fired on a
        # genuinely-unset optional. A required param can never reach here
        # unset (the missing-required branch above returns first), so this
        # guard cannot weaken the required path -- it only lets an unset
        # OPTIONAL param skip a check that has nothing to validate.
        if enum is not None and bound is not None and bound not in enum:
            return {}, f"param {pname!r} must be one of {enum} (got {bound!r})"
        ptype = p.get("type", "string")
        if ptype == "number" and isinstance(bound, str):
            try:
                float(bound)
            except ValueError:
                return {}, f"param {pname!r} must be a number (got {bound!r})"
        elif ptype == "boolean" and isinstance(bound, str):
            if bound.lower() not in ("true", "false"):
                return {}, f"param {pname!r} must be a boolean (got {bound!r})"
    return values, None


def _params_spec_payload(thing_id: str, thing: dict, params: list) -> dict:
    """Build use()'s `params_spec` sibling-key payload (umwelt #396 Trigger
    Logic, trigger cases 1/2 — empty args, or non-empty args that failed to
    bind) for a params-declaring readable/executable/blueprint. `params` is
    returned exactly as stored in metadata.params, unmodified — this is the
    caller's "--help", not a re-derived/filtered view."""
    return {"params_spec": {
        "thing": thing_id,
        "name": thing["name"],
        "params": params,
    }}


# Shared dark-MUD aesthetic (umwelt #221 S3c): extracted verbatim out of
# _render_world_html's inline <style> block so the new /projects view can
# reuse the SAME look without duplicating ~65 lines of CSS. _render_world_html
# itself is otherwise untouched — this is a pure string-literal hoist, no
# behavior change (byte-identical rendered output).
_WORLD_VIEW_CSS = (
    "body{font-family:ui-sans-serif,system-ui,sans-serif;max-width:760px;"
    "margin:2rem auto;padding:0 1rem;color:#e8e6df;background:#1a1814;line-height:1.55}"
    "h1{font-weight:600;letter-spacing:-.01em;margin-bottom:.1rem}"
    ".sub{color:#9a948a;font-size:.9rem;margin-top:0}"
    ".me{background:#211e19;border:1px solid #4a4436;border-radius:10px;"
    "padding:.6rem .8rem;margin:.8rem 0 1.2rem;font-size:.9rem;color:#c9c4ba;"
    "display:flex;align-items:center;gap:.45rem;flex-wrap:wrap}"
    ".me input,.me select{background:#12100d;border:1px solid #4a4436;border-radius:6px;"
    "color:#e8e6df;font:inherit;padding:2px 8px}"
    ".me input#agent{color:#f0c674;font-weight:600;width:6.5rem}"
    ".me input#avdesc{flex:1;min-width:11rem}.me input#lineage{width:9rem}"
    ".me button.go{background:#2f3a28;color:#b6d99a;border:1px solid #45592f;border-radius:6px;"
    "padding:3px 12px;font:inherit;font-size:.85rem;cursor:pointer}"
    ".me button.go:hover{background:#3a4a30}.metip{color:#6b665e;font-size:.8rem;flex-basis:100%}"
    ".here{border:1px solid #4a4436;border-radius:14px;padding:1.1rem 1.3rem;margin:1rem 0;"
    "background:#241f18;box-shadow:0 2px 20px rgba(0,0,0,.25)}"
    ".here h2{margin:.1rem 0;font-size:1.35rem;color:#f0c674}"
    ".badge{font-size:.7rem;color:#9a948a;font-weight:400}"
    ".desc{color:#d3cec4;margin:.4rem 0 .7rem;white-space:pre-wrap}"
    ".who{font-size:.9rem;color:#b6c9a8;margin:.2rem 0 .3rem}.who .me-tag{color:#f0c674}"
    ".who .ty{color:#8a847a;font-size:.8rem}"
    ".lblrow{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:#6b665e;"
    "margin:.9rem 0 .3rem;border-top:1px solid #34302a;padding-top:.5rem}"
    "ul{list-style:none;padding:0;margin:0}li{padding:.4rem 0;border-top:1px solid #2a2621}"
    "li:first-child{border-top:none}"
    ".trow{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap}"
    ".k{font-size:.65rem;background:#34302a;border-radius:4px;padding:1px 6px;color:#b9b3a8}"
    ".d{color:#a8a298;font-size:.88rem;display:block;margin-top:.1rem}"
    ".empty{color:#6b665e;font-style:italic;border:none}"
    ".act{margin-left:auto;display:flex;gap:.35rem}"
    ".act button{background:#2a2621;color:#c9c4ba;border:1px solid #45413a;border-radius:6px;"
    "padding:2px 9px;font:inherit;font-size:.78rem;cursor:pointer}"
    ".act button.use{background:#2f3a28;color:#b6d99a;border-color:#45592f}"
    ".act button:hover{background:#3a352d}.act button.use:hover{background:#3a4a30;color:#d6f0b8}"
    ".cx{padding:.25rem 0;border:none;font-size:.9rem}"
    ".cx .s{color:#f0c674;font-weight:600}.cx.evt{color:#8a847a;font-style:italic}"
    ".cx .rel{font-size:.66rem;color:#6b665e;margin-left:.35rem}"
    ".say{display:flex;gap:.4rem;margin-top:.5rem}"
    ".say input{flex:1;background:#12100d;border:1px solid #4a4436;border-radius:6px;"
    "color:#e8e6df;font:inherit;padding:3px 9px}"
    ".say button{background:#2f3a28;color:#b6d99a;border:1px solid #45592f;border-radius:6px;"
    "padding:2px 12px;font:inherit;font-size:.82rem;cursor:pointer}"
    ".exits{border:1px solid #34302a;border-radius:12px;padding:.7rem 1.1rem 1rem;margin:1rem 0;background:#211e19}"
    ".exits h3{font-size:.95rem;margin:.1rem 0 .6rem;color:#c9c4ba}"
    ".chips{display:flex;flex-wrap:wrap;gap:.45rem}"
    ".chip{background:#2a2621;border:1px solid #45413a;border-radius:999px;padding:3px 12px;"
    "font-size:.85rem;color:#d6d0c4;cursor:pointer}"
    ".chip:hover{background:#34302a;border-color:#5a5448}"
    ".chip.at{background:#2f3a28;border-color:#45592f;color:#b6d99a;cursor:default}"
    ".chip .n{color:#8a847a;font-size:.75rem;margin-left:.3rem}.live{color:#8fbf6a;font-size:.8rem}"
    "footer{color:#6b665e;font-size:.8rem;margin:2rem 0;text-align:center}"
    ".modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);"
    "align-items:center;justify-content:center;padding:1rem;z-index:9}"
    ".card{background:#211e19;border:1px solid #4a4436;border-radius:12px;"
    "max-width:640px;width:100%;max-height:80vh;overflow:auto;box-shadow:0 8px 40px rgba(0,0,0,.5)}"
    ".mhead{display:flex;align-items:center;justify-content:space-between;"
    "padding:.7rem 1rem;border-bottom:1px solid #34302a;font-weight:600;color:#f0c674}"
    "#mclose{background:none;border:none;color:#9a948a;font-size:1.1rem;cursor:pointer}"
    ".card .body{padding:.8rem 1rem}.mmeta{font-size:.8rem;color:#9a948a;margin-bottom:.6rem}"
    ".ok{color:#8fbf6a}.bad{color:#e0776a}.run{color:#9a948a;font-style:italic}"
    ".lbl{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:#6b665e;margin:.6rem 0 .2rem}"
    ".out{white-space:pre-wrap;background:#12100d;border-radius:8px;padding:.6rem .8rem;"
    "font-family:ui-monospace,monospace;font-size:.85rem;color:#d6d0c4}"
    ".diff{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:.82rem;color:#b6c9a8}"
)


def _render_world_html() -> str:
    """Render the world as a MUD you inhabit — a FAITHFUL mirror of the agent's
    interface (Lei's steer): the same context, the same things, the same verbs,
    all as description. You register an account (name + type + a description of
    your avatar — Lei's open, private-network account model), then walk:
    move_to (walk
    to an exit), look_at (examine a thing without running it), use (run it),
    crossings_here (read what's happening here), send_crossing (say something).

    GET / is a static shell; every read/mutation happens from the browser via the
    SAME API an agent uses. All agent-authored content is escaped client-side via
    esc() before it touches the DOM.
    """
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>Umwelt — walk the world</title><style>"
        + _WORLD_VIEW_CSS +
        "</style></head><body>"
        "<h1>🌍 Umwelt</h1>"
        "<p class=sub>a world you <b>walk</b> — the same one the agents live in · <span class=live>● live</span></p>"
        "<div class=me>🚶 enter as <input id=agent placeholder=\"your name\" spellcheck=false autocomplete=off> "
        "<select id=atype><option value=human>human</option><option value=agent>agent</option></select> "
        "<input id=lineage placeholder=\"lineage (agents, e.g. anthropic:opus)\" spellcheck=false autocomplete=off> "
        "<input id=avdesc placeholder=\"describe your avatar…\" spellcheck=false autocomplete=off> "
        "<button class=go id=enterbtn>enter the world ↵</button> <button class=go id=switchbtn style=\"background:#2a2621;color:#c9c4ba;border-color:#45413a\">switch</button>"
        "<span class=metip>one name = one avatar · no password, private network · returning names just reconnect</span></div>"
        "<div id=here class=here><p class=desc>✨ Welcome, traveler. Choose a name, say whether "
        "you're a human or an agent, describe your avatar, and step into the world.</p></div>"
        "<div id=exits class=exits><h3>🧭 where you can go</h3><div class=chips id=chips></div></div>"
        "<footer>you move through the world in a body (your avatar) · you see and do exactly what an "
        "agent does · every ▶ use runs in a sealed sandbox · port 5003</footer>"
        "<div id=modal class=modal><div class=card role=dialog aria-modal=true aria-labelledby=mtitle>"
        "<div class=mhead><span id=mtitle></span><button id=mclose aria-label=close>✕</button></div>"
        "<div class=body id=mbody></div></div></div>"
        "<script>"
        "const $=s=>document.querySelector(s),modal=$('#modal');let modalOpen=false;"
        "let agentName='',placeId=null,places=[];"
        "function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}"
        "function show(h){$('#mbody').innerHTML=h;modal.style.display='flex';modalOpen=true;}"
        "function closeModal(){modal.style.display='none';modalOpen=false;}"
        "$('#mclose').onclick=closeModal;modal.onclick=e=>{if(e.target===modal)closeModal();};"
        "document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});"
        "async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify(b)});try{return await r.json();}catch(e){return {error:'the world hit an error ('+r.status+')'};}}"
        "async function loadPlaces(){try{const j=await(await fetch('/umwelt/places')).json();places=j.places||[];}catch(e){places=[];}}"
        "function ptag(a){const t=a.type?(' <span class=ty>('+esc(a.type)+(a.lineage?(' · '+esc(a.lineage)):'')+')</span>'):'';return '🧍 '+esc(a.agent)+t;}"
        "function renderHere(v){const p=v.place||{};"
        "const welcome=v.born===true?'<div class=who style=\"color:#8fbf6a\">✨ welcome to the world, '+esc(agentName)+'! your body is born.</div>'"
        ":(v.born===false?'<div class=who style=\"color:#9a948a\">↩ welcome back, '+esc(agentName)+'.</div>':'');"
        "const others=(v.present||[]).filter(a=>a.agent!==agentName);"
        "let who='<span class=me-tag>🧍 you ('+esc(agentName)+') are here</span>';"
        "if(others.length)who+=' · '+others.map(ptag).join(' · ');"
        "const badge=p.kind==='private'?'🔒 private':'🌍 public';"
        "const things=(v.things||[]);"
        "const tlist=things.length?things.map(t=>"
        "'<li><div class=trow><b>'+esc(t.name)+'</b> <span class=k>'+esc(t.kind)+'</span>'"
        "+'<span class=act><button data-look=\"'+esc(t.id)+'\" data-name=\"'+esc(t.name)+'\">🔍 look</button>'"
        "+'<button class=use data-use=\"'+esc(t.id)+'\" data-name=\"'+esc(t.name)+'\">▶ use</button></span></div>'"
        "+'<span class=d>'+esc(t.description)+'</span></li>').join('')"
        ":'<li class=empty>— nothing forged here yet —</li>';"
        "const unseen=v.unseen_count?(' · <span class=live>'+v.unseen_count+' new since you last stood here</span>'):'';"
        "$('#here').innerHTML=welcome+'<h2>📍 '+esc(p.label||p.id)+' <span class=badge>'+badge+'</span></h2>'"
        "+'<p class=desc>'+esc(p.description)+'</p>'"
        "+'<div class=who>'+who+unseen+'</div>'"
        "+'<div class=lblrow>things here</div><ul>'+tlist+'</ul>'"
        "+'<div class=lblrow>what\\u2019s happening here</div><div id=talk></div>'"
        "+'<div class=say><input id=sayin placeholder=\"say something to this place…\" maxlength=500><button id=saybtn>say</button></div>';"
        "wireHere();loadCrossings();}"
        "function wireHere(){"
        "document.querySelectorAll('#here button[data-use]').forEach(b=>b.onclick=()=>useThing(b.dataset.use,b.dataset.name));"
        "document.querySelectorAll('#here button[data-look]').forEach(b=>b.onclick=()=>lookAt(b.dataset.look,b.dataset.name));"
        "const s=$('#saybtn');if(s)s.onclick=say;const si=$('#sayin');if(si)si.addEventListener('keydown',e=>{if(e.key==='Enter')say();});}"
        "function renderExits(){const c=$('#chips');"
        "c.innerHTML=places.map(p=>{const at=p.id===placeId;"
        "const n=(p.thing_count!=null)?('<span class=n>'+p.thing_count+'</span>'):'';"
        "return at?'<span class=\"chip at\">'+esc(p.label||p.id)+' (here)'+n+'</span>'"
        ":'<span class=chip data-place=\"'+esc(p.id)+'\">'+esc(p.label||p.id)+n+'</span>';}).join('');"
        "c.querySelectorAll('.chip[data-place]').forEach(ch=>ch.onclick=()=>walk(ch.dataset.place));}"
        "function hereError(m){$('#here').innerHTML='<h2>⚠</h2><p class=desc>'+esc(m)+'</p>';}"
        # umwelt #73 improvement: send the TYPED form on register (not a
        # lowercased one) so the server preserves it as display metadata on
        # first birth (e.g. "Lei's Post"); agentName itself stays
        # canonical (lowercased) since it's reused below for the
        # present-list self-filter (a.agent!==agentName, where a.agent is
        # always the server's canonical id) and for every subsequent
        # request body.
        "async function register(){const typed=$('#agent').value.trim();"
        "if(!typed){hereError('Please enter a name to enter the world.');return;}"
        "agentName=typed.toLowerCase();"
        "const type=($('#atype')?$('#atype').value:'human');"
        "const lineage=($('#lineage')?$('#lineage').value.trim():'');"
        "const desc=($('#avdesc')?($('#avdesc').value||'').trim():'');"
        "const v=await post('/umwelt/register',{agent:typed,type:type,lineage:lineage,avatar_description:desc});"
        "if(v.error){hereError(v.error);return;}localStorage.setItem('umwelt.acct',agentName);placeId=(v.place||{}).id;renderHere(v);renderExits();}"
        "async function walk(pid){const v=await post('/umwelt/move_to',{agent:agentName,place:pid});"
        "if(v.error){hereError(v.error);return;}localStorage.setItem('umwelt.acct',agentName);placeId=(v.place||{}).id;renderHere(v);renderExits();}"
        "async function refresh(){if(modalOpen||!placeId)return;"
        "const v=await post('/umwelt/move_to',{agent:agentName,place:placeId});"
        "if(!v.error){renderHere(v);renderExits();}}"
        "async function loadCrossings(){if(!placeId)return;const box=$('#talk');if(!box)return;"
        "try{const j=await post('/umwelt/crossings_here',{agent:agentName,place:placeId,since:0});"
        "const cx=(j.crossings||[]).slice(-20);"
        "box.innerHTML=cx.length?cx.map(c=>{const rel=(c.viewer_relation&&c.viewer_relation!=='broadcast')?(' <span class=rel>'+esc(c.viewer_relation)+'</span>'):'';"
        "return (c.body!=null&&c.body!=='')"
        "?'<div class=cx><span class=s>'+esc(c.sender)+':</span> '+esc(c.body)+rel+'</div>'"
        ":'<div class=\"cx evt\">'+esc(c.sender)+' — (a wordless signal)'+rel+'</div>';}).join('')"
        ":'<p class=empty>nothing has been said here yet — be the first.</p>';}"
        "catch(e){box.innerHTML='<p class=empty>couldn\\u2019t read this place\\u2019s voices.</p>';}}"
        "async function say(){const si=$('#sayin');if(!si)return;const t=si.value.trim();if(!t||!placeId)return;"
        "si.value='';const r=await post('/umwelt/send_crossing',"
        "{agent:agentName,destination:placeId,sent_at:new Date().toISOString(),body:t});"
        "if(r&&r.error){si.value=t;return;}loadCrossings();}"
        "async function lookAt(id,name){$('#mtitle').textContent='🔍 '+name;"
        "show('<span class=run>looking…</span>');"
        "try{const j=await post('/umwelt/look_at',{agent:agentName,thing:id});const t=(j&&j.thing)||null;"
        "if(!t){show('<span class=bad>'+esc((j&&j.error)||'nothing to see')+'</span>');return;}"
        "let meta='';try{const m=t.metadata;if(m&&Object.keys(m).length)meta='<div class=lbl>metadata</div><div class=out>'+esc(JSON.stringify(m,null,2))+'</div>';}catch(e){}"
        "show('<div class=mmeta>'+esc(t.kind)+(t.author?(' · forged by '+esc(t.author)):'')+(t.created_at?(' · '+esc(t.created_at)):'')+'</div>'"
        "+'<div class=lbl>what it is</div><div class=out>'+esc(t.description)+'</div>'"
        "+(t.exec_command?'<div class=lbl>how the world runs it</div><div class=out>'+esc(t.exec_command)+'</div>':'')"
        "+meta+'<div class=lbl style=\"margin-top:.7rem\">▶ use it to actually run it</div>');}"
        "catch(e){show('<span class=bad>could not reach the world: '+esc(''+e)+'</span>');}}"
        "async function useThing(id,name){$('#mtitle').textContent=agentName+' → use \\u201c'+name+'\\u201d';"
        "show('<span class=run>running in the sandbox…</span>');"
        "try{const j=await post('/umwelt/use',{agent:agentName,thing:id});const o=j.outcome||j;"
        "if(o&&o.error){show('<span class=bad>'+esc(o.error)+'</span>');return;}"
        "if(o&&o.kind==='readable'){const fs=Object.entries(o.content||{});"
        "show(fs.length?fs.map(e=>'<div class=lbl>'+esc(e[0])+'</div><div class=out>'+esc(e[1])+'</div>').join('')"
        ":'<span class=run>(empty)</span>');return;}"
        "if(o&&o.timed_out){show('<div class=mmeta><span class=bad>timed out</span> · '"
        "+esc(o.elapsed_s)+'s</div><div class=out>'"
        "+esc(o.briefing||'It ran past the time limit and the world stopped it.')+'</div>');return;}"
        "if(!o||o.exit_code===undefined){show('<span class=bad>'+esc((j&&j.error)||'no outcome')+'</span>');return;}"
        "const okc=o.exit_code===0;let diff='';"
        "const wd=o.world_diff||{};const cr=(wd.created||[]),md=(wd.modified||[]);"
        "if(cr.length||md.length){diff='<div class=lbl>what changed</div><div class=diff>'"
        "+cr.map(f=>'+ '+esc(f)).concat(md.map(f=>'~ '+esc(f))).join('\\n')+'</div>';}"
        "show('<div class=mmeta><span class=\"'+(okc?'ok':'bad')+'\">exit '+esc(o.exit_code)+'</span>'"
        "+' · '+esc(o.elapsed_s)+'s · '+(o.network_isolated?'🔒 no network':'network')+'</div>'"
        "+(o.stdout?'<div class=lbl>it said</div><div class=out>'+esc(o.stdout)+'</div>':'')"
        "+(o.stderr?'<div class=lbl>errors</div><div class=out style=\"color:#e0a89a\">'+esc(o.stderr)+'</div>':'')"
        "+diff);}catch(e){show('<span class=bad>could not reach the world: '+esc(''+e)+'</span>');}}"
        "$('#enterbtn').onclick=register;const _sw=$('#switchbtn');if(_sw)_sw.onclick=()=>{localStorage.removeItem('umwelt.acct');location.reload();};"
        "['#agent','#lineage','#avdesc'].forEach(id=>{const el=$(id);if(el)el.addEventListener('keydown',e=>{if(e.key==='Enter')register();});});"
        "(async()=>{await loadPlaces();try{const n=localStorage.getItem('umwelt.acct');if(n){$('#agent').value=n;await register();}}catch(e){}})();"
        "function typing(){const si=$('#sayin');return !!(si&&(si.value.length||document.activeElement===si));}"
        "setInterval(()=>{if(!modalOpen&&!typing()){refresh();}},8000);"
        "</script>"
        "</body></html>"
    )


def _render_projects_html() -> str:
    """Render the project board as a static, read-only HTML shell — the
    project-board twin of _render_world_html (umwelt #221 S3c). Same
    architecture, deliberately mirrored: GET /projects returns a STATIC
    shell with NO server-rendered agent content; the browser then fetches
    from the SAME two already-public, read-only JSON routes (`GET
    /umwelt/projects`, `GET /umwelt/projects/<id>`) and renders client-side,
    escaping every agent-authored / attacker-influenceable string (project
    label/description, task label/description, owner, turn_holder,
    gh_repo/gh_ref) through esc() — a byte-for-byte copy of world_view's own
    escaper — before it ever touches the DOM. No agent-authored content is
    ever server-rendered into this shell.

    Unlike _render_world_html, this view is READ-ONLY BY CONSTRUCTION with
    no exception: the client issues GET fetches only, never a POST/mutation
    call, and there is no register/identity/session affordance at all (no
    account to enter, nothing to act as) — a pure public window onto the
    board, private-network-scoped, no auth, same trust boundary as GET /.
    """
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>Umwelt — projects</title><style>"
        + _WORLD_VIEW_CSS +
        "</style></head><body>"
        "<h1>🗂️ Umwelt — Projects</h1>"
        "<p class=sub>a read-only window onto the project board · "
        "<span class=live>● live</span></p>"
        "<div id=projects><p class=desc>loading…</p></div>"
        "<footer>read-only · rendered client-side from the same read API "
        "agents use · port 5003</footer>"
        "<script>"
        "const $=s=>document.querySelector(s);"
        "function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}"
        "function statusCounts(tasks){const counts={};"
        "(function walk(list){(list||[]).forEach(t=>{counts[t.status]=(counts[t.status]||0)+1;walk(t.children);});})(tasks);"
        "return counts;}"
        "function countsRow(counts){const keys=Object.keys(counts);"
        "if(!keys.length)return '<span class=empty>no tasks yet</span>';"
        "return keys.map(k=>'<span class=k>'+esc(k)+' '+counts[k]+'</span>').join(' ');}"
        "function renderTaskNode(t){"
        "const blocked=t.blocked?' <span class=k style=\"color:#e0776a\">⛔ blocked</span>':'';"
        "const court=t.turn_holder"
        "?('<span class=who><span class=me-tag>whose court:</span> '+esc(t.turn_holder)+'</span>')"
        ":'<span class=who style=\"color:#6b665e\">unassigned</span>';"
        "const owner=t.owner?(' · owner '+esc(t.owner)):'';"
        "const deps=(t.deps&&t.deps.length)?(' · depends on '+t.deps.map(d=>esc(d)).join(', ')):'';"
        "const gh=(t.gh_repo||t.gh_ref)?(' · '+esc(t.gh_repo||'')+(t.gh_ref?('@'+esc(t.gh_ref)):'')):'';"
        "const kids=(t.children&&t.children.length)?renderTaskTree(t.children):'';"
        "return '<li><div class=trow><b>'+esc(t.label)+'</b> <span class=k>'+esc(t.status)+'</span>'+blocked+'</div>'"
        "+'<span class=d>'+esc(t.description)+'</span>'"
        "+'<div class=who>'+court+owner+deps+gh+'</div>'"
        "+kids+'</li>';}"
        "function renderTaskTree(tasks){"
        "if(!tasks||!tasks.length)return '<p class=empty>— no tasks yet —</p>';"
        "return '<ul>'+tasks.map(renderTaskNode).join('')+'</ul>';}"
        "async function loadTasks(pid){"
        "const box=document.getElementById('tasks-'+pid);if(!box)return;"
        "try{"
        "const j=await(await fetch('/umwelt/projects/'+encodeURIComponent(pid))).json();"
        "const tasks=(j.project&&j.project.tasks)||[];"
        "box.innerHTML='<div class=lblrow>status counts</div><div class=trow>'"
        "+countsRow(statusCounts(tasks))+'</div>'"
        "+'<div class=lblrow>tasks</div>'+renderTaskTree(tasks);"
        "}catch(e){box.innerHTML='<p class=empty>could not load this project\\u2019s tasks.</p>';}}"
        "function renderProjects(projects){"
        "const box=$('#projects');"
        "if(!projects.length){box.innerHTML='<p class=empty>— no projects yet —</p>';return;}"
        "box.innerHTML=projects.map(p=>"
        "'<div class=here><h2>📁 '+esc(p.label)+' <span class=badge>'+esc(p.status)+'</span></h2>'"
        "+'<p class=desc>'+esc(p.description)+'</p>'"
        "+'<div class=who>created by <b>'+esc(p.created_by)+'</b></div>'"
        "+'<div id=\"tasks-'+esc(p.id)+'\"><p class=empty>loading tasks…</p></div>'"
        "+'</div>').join('');"
        "projects.forEach(p=>loadTasks(p.id));}"
        "async function loadProjects(){"
        "try{const j=await(await fetch('/umwelt/projects')).json();renderProjects(j.projects||[]);}"
        "catch(e){$('#projects').innerHTML='<p class=empty>could not reach the world.</p>';}}"
        "loadProjects();"
        "</script>"
        "</body></html>"
    )


# -- map filter-pruning (umwelt #160) ---------------------------------------
# A pure post-processing pass over an already-built place_tree node — see
# /umwelt/map's docstring for the full "why bottom-up, path-preserving
# pruning" rationale. These stay stateless (no `conn`/`g` access) so they
# live at module level, same posture as `_canonical_agent`/`_reachable`
# above rather than nested inside create_app.

def _map_node_self_matches(node: dict, name_filter: str | None,
                            kind_filter: str | None, owner_filter: str | None,
                            unread_only: bool) -> bool:
    """True if `node` ITSELF (ignoring its children) matches every supplied
    filter. An omitted filter (None / False) is vacuously satisfied."""
    if name_filter and name_filter.lower() not in (node.get("label") or "").lower():
        return False
    if kind_filter and node.get("kind") != kind_filter:
        return False
    if owner_filter and node.get("owner") != owner_filter:
        return False
    if unread_only and not node.get("unread"):
        return False
    return True


def _prune_map_node(node: dict, name_filter: str | None, kind_filter: str | None,
                     owner_filter: str | None, unread_only: bool) -> dict | None:
    """Bottom-up, path-preserving filter prune (umwelt #160): returns a
    pruned COPY of `node` (place_tree's own output is never mutated) if
    `node` itself matches every filter, OR if any descendant (recursively)
    matches — so a matching descendant always keeps its full ancestor path
    alive, never orphaned. Returns None only when neither `node` nor any of
    its descendants match — the caller drops such a node/root entirely.

    `links` are copied through unpruned — they are not part of the
    ancestor/descendant path this filter operates over (umwelt #158's own
    place_tree treats links as a same-node annotation, not a recursion
    level; #160 filtering follows that same posture)."""
    pruned_children = []
    for child in node.get("children") or []:
        kept = _prune_map_node(child, name_filter, kind_filter, owner_filter,
                                unread_only)
        if kept is not None:
            pruned_children.append(kept)
    self_matches = _map_node_self_matches(node, name_filter, kind_filter,
                                           owner_filter, unread_only)
    if not self_matches and not pruned_children:
        return None
    result = dict(node)
    result["children"] = pruned_children
    return result


# -- login throttling (umwelt#561 security review, Mira: "challenge
# issuance/login remain unthrottled") -----------------------------------
#
# Same shape as the GPU services' internal RateLimiter (not in this release; per-agent
# fixed-window, deque-based, check_and_record(key) -> bool), deliberately
# NOT imported from there -- that module is part of the GPU-tenant
# service shell (pulls in GPU-service-specific
# plumbing (internal tooling, not in this release) that has no business in the login route), and reusing a class
# named for a different domain would be exactly the kind of naming/
# domain mismatch a future reader trips on. This is a standalone
# reimplementation of the same small, in-memory, single-process,
# ADVISORY posture -- it limits a well-behaved caller hammering the
# route, not a hostile one spreading requests across processes/IPs. Per
# this project's LAN-family-scale threat model (see the internal contributor guide), that
# posture is sufficient; this is not a distributed/persistent limiter.
class _LoginRateLimiter:
    """Per-key fixed-window rate limit -- used both for the per-agent
    dimension (key: canonicalized agent_id) and the per-source dimension
    (key: request.remote_addr, see the route handlers below). One
    instance per Flask app per dimension (app.config, same placement
    convention as the art and voice services' RATE_LIMITER (internal tooling, not in this release)) --
    never a module-level singleton, so each create_app() call (including
    each test's own app fixture) gets isolated state.

    umwelt#561 round-2 security review (Mira, blocker 1): `_hits` is an
    OrderedDict, not a plain dict, so total tracked-key memory is
    HARD-bounded at `max_keys` regardless of attacker behavior -- an
    attacker sending unbounded one-shot fake keys (agent_ids or,
    per the source dimension, spoofed-looking sources) cannot grow this
    past `max_keys` entries, which is a stronger property than a
    periodic sweep (bounds worst-case memory, not just average-case
    growth).

    Eviction is true LRU-by-last-use, not merely insertion-order: EVERY
    call to `check_and_record` for a key -- new or re-used, and WHETHER
    OR NOT the call ends up rejected by the rate check below -- moves
    that key to the most-recently-used end via `move_to_end`, so only a
    key that has gone genuinely idle can ever reach the eviction
    end. This closes a real gap the first cut of this class had (caught
    in review, umwelt#584): with insertion-order-only eviction, a
    'hot' key -- one actively being hit, e.g. an account mid rate-limit
    -- sits frozen at its ORIGINAL insertion position regardless of how
    many times it's re-used, so a flood of brand-new one-shot keys could
    walk the eviction pointer past it and silently evict (== reset) its
    accumulated hit history even while it was still being actively
    abused. True LRU makes that impossible: a key can only be evicted
    once it stops being used, which is exactly the property "bound
    total memory without letting an attacker reset an active window"
    needs -- bounding key count and refusing to let activity extend a
    key's life are two different requirements, and the fix is address-
    ing both rather than trading one for the other."""

    def __init__(self, limit: int, window_s: float, max_keys: int) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_record(self, key: str) -> bool:
        """Returns True (and records the hit) iff `key` is under its
        limit right now. Returns False (does NOT record -- a rejected
        request shouldn't itself count against the window) otherwise."""
        now = time.monotonic()
        with self._lock:
            if key not in self._hits and len(self._hits) >= self.max_keys:
                # Bound total tracked keys: evict the least-recently-
                # USED entry (the current LRU end), never the request
                # being handled right now.
                self._hits.popitem(last=False)
            hits = self._hits.setdefault(key, deque())
            # Refresh recency on EVERY access (new or existing key) --
            # this is what makes eviction true LRU-by-use rather than
            # insertion-order; see the class docstring.
            self._hits.move_to_end(key)
            while hits and now - hits[0] > self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


# PROVISIONAL, same "starting point, not calibrated" posture as
# CARRY_CAPACITY_DEFAULT/CONTAINER_CAPACITY_DEFAULT above (issue #88) --
# picked to sit in the middle of a reasonable ~10-20-per-60s LAN-family-
# scale range, not derived from any measurement or load test. Tune from
# practice, same as those.
_LOGIN_CHALLENGE_RATE_LIMIT = 15   # challenges issued, per agent_id
_LOGIN_ATTEMPT_RATE_LIMIT = 15     # login attempts, per agent_id
_LOGIN_RATE_WINDOW_S = 60.0        # per key, per route, per this window

# umwelt#561 round-2 (Mira, blocker 2): a SECOND, per-source dimension,
# checked in addition to (not instead of) the per-agent_id one above --
# closes the ID-rotation evasion where an attacker defeats a per-agent
# limit by sending each request under a fresh, never-reused agent_id.
# Deliberately more generous than the per-agent limit: this deployment's
# LAN-family-scale threat model has multiple distinct legitimate agents
# routinely sharing one physical/LAN source address (several agent
# sessions on one host all reaching the world server from the same IP),
# so a per-source cap sized like the per-agent one would false-positive-
# throttle normal concurrent use. ~100/60s comfortably covers ~6-10
# legitimate concurrent callers behaving normally while still bounding a
# genuine single-source flood -- PROVISIONAL, same "starting point, not
# calibrated" posture as the per-agent constants above, not derived from
# measurement.
_LOGIN_SOURCE_RATE_LIMIT = 100     # requests, per source address

# Hard cap on distinct tracked keys per _LoginRateLimiter instance
# (umwelt#561 round-2, Mira blocker 1) -- bounds worst-case memory to a
# fixed, small footprint no matter how many distinct one-shot keys
# (fake agent_ids, or spoofed-looking sources) an attacker sends.
# Picked to comfortably exceed any realistic number of distinct
# legitimate identities/sources this LAN-family-scale deployment would
# ever see at once, not derived from a load test -- same PROVISIONAL
# posture as the rate-limit constants above.
_LOGIN_RATE_LIMITER_MAX_KEYS = 10000

# umwelt#586, Ariadne's colleague-pass catch on PR #594: explicit
# `maxmem` ceiling for the scrypt login branch's hashlib.scrypt() call.
# OpenSSL's own default maxmem is far below what a reasonably-bumped
# scrypt cost needs -- measured live: n=16384 (today's bind_client.py
# default) fits under the default; n=32768 (the natural next cost bump)
# already raises `ValueError: memory limit exceeded`, which the login
# route's except clause catches and returns as the SAME uniform 401 as
# a wrong password -- a future cost bump silently locks its owner out,
# undiagnosable from the client precisely because of the uniform-401
# posture.
#
# Mira's security re-review (round 2) on this same PR: `/umwelt/login`
# is UNAUTHENTICATED, so `maxmem` isn't just a correctness bound -- it
# authorizes up to that much resident memory PER IN-FLIGHT ATTEMPT for
# any caller who knows a scrypt-bound identity exists, one KDF per
# admitted request. My first pass (512MiB) sized for four future
# cost-doublings of headroom; that is memory-amplification surface for
# a ceiling nothing has asked for yet. Corrected to the smallest bound
# covering the cost this PR actually SUPPORTS AND TESTS -- n=2**15,
# which is the one migration path this route is built for today.
#
# EMPIRICALLY VERIFIED. An early guess (this comment, before Ariadne's
# colleague-pass correction) called the 128*N*r formula unreliable
# because a 512MiB pass sized off it came up short in practice -- that
# framing was itself wrong. The formula IS exact; it was a boundary
# read (> vs >=), not a bad model: true cost is `128*N*r + 3072` bytes
# (binary-searched n=2**14..2**19, independently re-verified here).
# For n=2**15,r=8 that's exactly 33,557,504 bytes -- confirmed live:
# maxmem at that value succeeds, one byte short fails. 64MiB clears it
# with real margin (0.043-0.045s across 48-80MiB, no measurable cost
# from the headroom) while still refusing n=2**16 by that same 3072-
# byte gap -- deliberately: n=2**16 is not a cost this PR supports or
# tests. A cost bump PAST n=2**15 is an explicit code/config
# review (raise this constant deliberately, re-measure, don't silently
# widen it for speculative future headroom) -- not something this
# constant should pre-authorize.
_SCRYPT_MAXMEM_BYTES = 64 * 1024 * 1024  # 64 MiB -- covers n=2**15 only


def _login_source_key() -> str:
    """The per-source rate-limit key for the current request (umwelt#561
    round-2, Mira blocker 2).

    Reads `flask.request.remote_addr` directly -- Werkzeug's view of the
    actual TCP peer address, resolved from the socket layer, not from
    any request header. That makes it NOT spoofable by the client: a
    caller cannot make itself appear to come from a different source by
    setting any header, unlike `X-Forwarded-For`/`X-Real-IP`, which ARE
    client-spoofable unless a trusted reverse proxy sits in front and
    strips/rewrites them before the request reaches this process.

    This deployment currently has NO reverse proxy in front of it --
    confirmed against this repo (no nginx/caddy/similar config anywhere
    in the tree; the internal deploy `*.service` units (not in this release) invoke `python -m umwelt`
    directly, i.e. Flask's own dev server via `app.run()`, with no proxy
    layer described or installed) -- so `remote_addr` is meaningful
    today: it genuinely reflects each distinct caller.

    If a reverse proxy is EVER added in front of this server, every
    request would then show the proxy's own address here, collapsing
    all real clients into a single source bucket -- this function would
    need to change to trust the proxy's rewritten forwarded-for header
    (once a trusted proxy is guaranteed to have set it), never a raw
    client-supplied header. Revisit this function at that time; do not
    add header-trusting logic pre-emptively while there is no proxy to
    trust it from.
    """
    return request.remote_addr or "unknown"


# umwelt#862 T5 (Kepler's ruling, issue comment 5931302607; raw-bound
# placement amended in comment 5932193133 after round 1's correct STOP --
# see _bound_send_crossing_body below for why the raw bound lives at the
# WSGI layer rather than inside the route). Distinct from _OPEN_S_GUARD
# (a read-paging threshold, umwelt#862 D8) -- neither of these constants
# is expressed via or compared to it.
_SEND_RAW_MAX_BYTES = 262144      # 256 KiB -- raw request bytes, ruling item 1
_SEND_BODY_MAX_CHARS = 32768      # len() of the parsed `body` string, ruling item 1
# The two caps above are INDEPENDENT (ruling item 2): a body under
# _SEND_BODY_MAX_CHARS characters is NOT thereby promised to be accepted
# -- the raw cap can still refuse it first (an astral/emoji-heavy body
# under json.dumps's default ensure_ascii=True costs 12 raw bytes per
# character via a \uXXXX surrogate pair; see the Unicode boundary tests
# in test_send_crossing_ingress_bounds.py).
_SEND_RATE_MAX = 60               # stored sends per resolved sender, ruling item 3
_SEND_RATE_WINDOW_S = 60.0        # rolling window, ruling item 3 ("one per second")
_SEND_RATE_KEY_MAX = 4096         # distinct tracked sender keys, LRU-evicted --
                                   # same bounded-memory posture as
                                   # _LOGIN_RATE_LIMITER_MAX_KEYS above; a
                                   # hygiene ceiling only, not derived from
                                   # measurement.

# umwelt#924 round 4 M1 (Mira): ASCII-only digit match for CONTENT_LENGTH
# -- deliberately NOT str.isdigit(), which accepts non-ASCII digit
# characters (e.g. superscript "²") that int() then raises on. See
# _bound_send_crossing_body's own docstring for the full fail-closed
# contract.
_CONTENT_LENGTH_DIGITS_RE = re.compile(r"[0-9]+")


def _send_rate_clock() -> float:
    """Module-level indirection so umwelt#862 T5's rate tests can
    monkeypatch exactly this one name (server._send_rate_clock) instead
    of patching time.monotonic globally, which would also perturb every
    OTHER rate limiter in this module (_LoginRateLimiter above included)
    that calls time.monotonic() directly."""
    return time.monotonic()


class _SendRateLimiter:
    """Per-resolved-sender fixed-window rate limiter for POST
    /umwelt/send_crossing (umwelt#862 T5, Kepler's ruling items 3 and 5).
    Same LRU-by-use, hard-bounded-key-count posture as _LoginRateLimiter
    above (umwelt#561) -- NOT shared code with it: this limiter needs
    reserve/release semantics (below) that _LoginRateLimiter's
    check_and_record doesn't have, and _LoginRateLimiter's own class
    docstring already explains why a third rate-limiter implementation
    (cap_server's) wasn't imported either -- forking a small, in-memory,
    single-process, ADVISORY limiter per distinct need is this codebase's
    established posture, not an oversight.

    Only STORED sends consume budget (ruling item 3): try_reserve
    provisionally records a hit and returns a token; release() undoes
    EXACTLY that one reservation if the caller's own storage attempt
    (db.build_crossing) subsequently raises, so a send that reserved a
    slot but never actually landed does not cost the sender part of
    their minute. Every 4xx refusal that happens BEFORE send_crossing
    calls try_reserve (bad destination, oversized title, unknown
    to_agent, the raw/body bounds above, ...) never touches this limiter
    at all -- try_reserve is called at exactly one call site, immediately
    before _evaluate_wake/build_crossing in send_crossing.

    Trust limit (ruling item 6, same docstring the route itself carries
    in its own module docstring): this protects against an accidental
    flood from a well-behaved caller, NOT a hostile sender who rotates
    caller-declared identities -- identity here is caller-declared,
    exactly like every other identity in this module before #495's
    credentials land; this limiter cannot see through a rotated identity
    any better than the rest of the app can. No human-resident exemption
    (ruling item 4): a human client floods by accident exactly like an
    agent client, and this budget is distinct from Gong Spec wake policy.
    """

    def __init__(self, limit: int, window_s: float, max_keys: int) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        # umwelt#862 T5 round 3 (reviewer suggestion S1): each entry is a
        # (timestamp, seq) pair, not a bare timestamp -- a per-limiter
        # itertools.count() seq makes every token unique regardless of
        # clock resolution, so release() below can remove the EXACT
        # entry instead of deque.remove's "first value-equal" match,
        # which would otherwise risk removing a DIFFERENT reservation
        # that happened to share this one's timestamp.
        self._hits: OrderedDict[str, deque[tuple[float, int]]] = OrderedDict()
        self._lock = threading.Lock()
        self._seq = itertools.count()

    def try_reserve(self, key: str) -> tuple[bool, tuple[float, int] | None]:
        """Returns (True, token) and provisionally records `token` (an
        opaque (timestamp, seq) pair -- see __init__) under `key` iff
        `key` is under its rolling-window limit right now; returns
        (False, None) WITHOUT recording otherwise (a refused reservation
        must not itself count against the window). Pass the returned
        token to release() if the caller's own storage attempt
        subsequently fails.

        umwelt#924 round 4 M2 (Mira): the clock is sampled INSIDE
        `_lock`, not before acquiring it. Sampling outside the lock lets
        two threads interleave their clock-read and append in either
        order, so the deque -- which every other piece of this class
        assumes is maintained in non-decreasing timestamp order (the
        window sweep below only ever inspects the FRONT) -- can end up
        with a newer entry appended before an older one. See
        test_send_crossing_ingress_bounds.py's TestM2ClockInsideLock for
        the forced repro and the false-429 consequence."""
        with self._lock:
            now = _send_rate_clock()
            if key not in self._hits and len(self._hits) >= self.max_keys:
                # Bound total tracked keys: evict the least-recently-USED
                # entry, never the request being handled right now --
                # same true-LRU-by-use reasoning as _LoginRateLimiter
                # above (umwelt#584).
                self._hits.popitem(last=False)
            hits = self._hits.setdefault(key, deque())
            self._hits.move_to_end(key)
            while hits and now - hits[0][0] > self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, None
            token = (now, next(self._seq))
            hits.append(token)
            return True, token

    def release(self, key: str, token: tuple[float, int]) -> None:
        """Undo exactly the reservation `token` under `key` (ruling item
        5: "release it if storage raises"). Removing the SPECIFIC
        (timestamp, seq) tuple rather than e.g. popping the newest/oldest
        entry -- or matching on the timestamp alone, which two
        reservations landing on the same clock tick could share -- means
        a concurrent reservation for the same key racing in between this
        key's try_reserve and release can never have ITS OWN slot stolen
        back by an unrelated release."""
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                return
            try:
                hits.remove(token)
            except ValueError:
                pass  # already evicted by the window/LRU sweep -- fine


# umwelt#923 (Kepler's ruling): ONE request-body bound for every world-server
# write route, applied by umwelt.ingress.bound_write_bodies below every Flask
# hook. The framing contract (exact-CL read, limit+1 only when chunked,
# empty-string CL == absent, leading zeros, digit-count guard before int(),
# never MAX_CONTENT_LENGTH) lives in that module's docstring -- it is the
# umwelt#862 T5 / #924 wrapper's, generalised.
#
# THE TABLE. Rows come in three kinds, each with a NAMED ceiling constant
# (TestTable in test_ingress_layer_923.py pins every marked row to the
# constant it uses, one pin per constant, so a row cannot silently move
# between constants or to a literal):
#
#   - thing-content rows (create_thing, update_thing): _THING_CONTENT_ROW_BYTES;
#   - every other row with an uncapped field: _UNCAPPED_ROW_BYTES (send_crossing
#     keeps _SEND_RAW_MAX_BYTES, the same 262,144 it has always had);
#   - `use`: a DERIVED row, _USE_ROW_BYTES, not marked (fully capped).
#
# `uncapped_field` NAMES, in the data, the field(s) the server does not cap
# itself, so a later cap cannot land as a silent edit to one row without the
# marker moving. The missing semantic caps are umwelt#1031 (avatar
# descriptions: #149); login and create_task.label are #1031 too -- NO field
# cap is added here. Field paths prefixed `spec.` / `patch.` are keys of the
# request's spec / patch object.

# Thing content (a thing's `source` files, exec_command, metadata) is
# deliberately large: the "library" size class starts at
# db.SIZE_CLASS_TOME_MAX_TOKENS = 100,000 tokens, ~400,000 characters; the
# worst-case JSON escape costs 6 bytes per character (ensure_ascii, BMP
# \uXXXX) = ~2.4 MB for ONE such file; rounded up to a power of two:
# 4 MiB. (An astral character costs 12 -- the same accepted trade-off the T5
# bound documents.)
_THING_CONTENT_ROW_BYTES = 4194304     # 4 MiB
# Every other row with an uncapped field: the send_crossing ceiling, the
# largest bound already ruled before thing content.
_UNCAPPED_ROW_BYTES = 262144           # 256 KiB
# POST /umwelt/use, a fully-capped route that outgrows the default: 16 args,
# 16,384 chars total (_USE_ARGS_MAX_*) = 98,938 bytes at 6 bytes per
# character, x2, rounded UP to a multiple of 4 KiB.
_USE_ROW_BYTES = 200704                # 49 * 4096

_WRITE_BODY_ROWS: dict[tuple[str, str], BodyRow] = {
    ("POST", "/umwelt/act"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("args",)),
    ("POST", "/umwelt/add_link"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("label",)),
    ("POST", "/umwelt/create_place"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("description",)),
    ("POST", "/umwelt/create_thing"): BodyRow(
        _THING_CONTENT_ROW_BYTES,
        ("spec.source", "spec.exec_command", "spec.metadata")),
    ("POST", "/umwelt/customize_avatar"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("description", "lineage", "render_spec")),
    ("POST", "/umwelt/enter"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("avatar_description", "lineage")),
    ("POST", "/umwelt/login"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("password", "signature", "challenge")),
    ("POST", "/umwelt/projects"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("description",)),
    ("POST", "/umwelt/projects/<project_id>/tasks"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("label", "description", "gh_repo")),
    ("POST", "/umwelt/projects/<project_id>/update"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("patch.description",)),
    ("POST", "/umwelt/register"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("avatar_description", "lineage")),
    ("POST", "/umwelt/send_crossing"): BodyRow(
        _SEND_RAW_MAX_BYTES, ("protocol",)),
    ("POST", "/umwelt/tasks/<task_id>/update"): BodyRow(
        _UNCAPPED_ROW_BYTES,
        ("patch.label", "patch.description", "patch.gh_repo")),
    ("POST", "/umwelt/update_place"): BodyRow(
        _UNCAPPED_ROW_BYTES, ("patch.metadata",)),
    ("POST", "/umwelt/update_thing"): BodyRow(
        _THING_CONTENT_ROW_BYTES,
        ("patch.source", "patch.exec_command", "patch.metadata")),
    ("POST", "/umwelt/use"): BodyRow(_USE_ROW_BYTES),
}

# THE DEFAULT, for every write route NOT in the table (an unlisted route is
# bounded, never unbounded). SET BY POST /umwelt/bin_notice -- the largest
# fully-capped route that has no row of its own: its `reason` is capped at
# db.BIN_REASON_MAX = 1,000 chars, so the worst-case body is 6,574 bytes at
# 6 bytes per character plus actor/thing identity fields costed at their
# creation caps; x2 = 13,148; rounded UP to a multiple of 4 KiB = 16,384.
# WHEN YOU ADD OR LENGTHEN A WRITE ROUTE, RE-DERIVE: if its worst-case body x2
# exceeds this, give it a row. The full per-route derivation is in the PR
# body and re-computed by test_default_matches_derivation. An astral
# character costs 12 bytes, the same accepted trade-off the T5 bound
# documents: a char-capped field is not promised to fit the raw cap.
_WRITE_BODY_DEFAULT_BYTES = 16384


def _flask_rule_resolver(app: Flask):
    """Resolve a WSGI environ to the app's own url_map RULE string, so a
    parametrized rule (/umwelt/projects/<project_id>/update) maps to its
    rule and its row, not to a raw path. None when nothing matches (404 /
    405 / redirect) -- such a write request still gets the default."""
    from werkzeug.exceptions import HTTPException

    def _resolve(environ):
        try:
            adapter = app.url_map.bind_to_environ(environ)
            rule, _ = adapter.match(
                method=(environ.get("REQUEST_METHOD") or "").upper(),
                return_rule=True)
            return rule.rule
        except HTTPException:
            return None

    return _resolve


def _bound_send_crossing_body(wsgi_app):
    """Compatibility wrapper (umwelt#923): the original umwelt#862 T5
    send-only bound, now the generic umwelt.ingress layer applied with ONLY
    the send_crossing row and no default (every other request passes
    through untouched). create_app no longer installs this; it installs the
    full table via bound_write_bodies. Kept under its name because the
    direct-drive framing tests in test_send_crossing_ingress_bounds.py call
    it. The framing rules are documented once, in umwelt.ingress."""
    return bound_write_bodies(
        wsgi_app,
        rows={("POST", "/umwelt/send_crossing"):
              BodyRow(_SEND_RAW_MAX_BYTES, ("protocol",))},
        default_limit=None,
        resolve_route=lambda environ: environ.get("PATH_INFO"),
        send_only_compat=True,
    )


def create_app(db_path: str, world_version_file: str | None = None,
                retire_config_file: str | None = None,
                sprite_dir: str | None = None,
                art_service_url: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    # umwelt#464 slice 2: gates the generate/generate_status capability-
    # channel ops (threaded to CapHandler at the run_thing_capped call site
    # below). Both None by default -- generation is disabled unless the
    # caller (main()'s boot sequence, via --tier-config/--art-service-url)
    # explicitly resolves and passes them.
    app.config["CAP_SPRITE_DIR"] = sprite_dir
    app.config["ART_SERVICE_URL"] = art_service_url
    # umwelt#561 security review (Mira): per-app instances, same isolation
    # posture as the GPU services' internal RATE_LIMITER (not in this release) -- each create_app() call
    # (production boot AND each test's own app fixture) gets its own
    # limiter state, never a shared module-level singleton. Round 2
    # (Mira, blocker 2): each route gets TWO limiter instances -- one
    # keyed per-agent (as before), one keyed per-source (request.
    # remote_addr) -- a request must pass BOTH to proceed (see the route
    # handlers below for the source/proxy semantics of that key).
    app.config["LOGIN_CHALLENGE_RATE_LIMITER"] = _LoginRateLimiter(
        _LOGIN_CHALLENGE_RATE_LIMIT, _LOGIN_RATE_WINDOW_S,
        _LOGIN_RATE_LIMITER_MAX_KEYS)
    app.config["LOGIN_CHALLENGE_SOURCE_RATE_LIMITER"] = _LoginRateLimiter(
        _LOGIN_SOURCE_RATE_LIMIT, _LOGIN_RATE_WINDOW_S,
        _LOGIN_RATE_LIMITER_MAX_KEYS)
    app.config["LOGIN_ATTEMPT_RATE_LIMITER"] = _LoginRateLimiter(
        _LOGIN_ATTEMPT_RATE_LIMIT, _LOGIN_RATE_WINDOW_S,
        _LOGIN_RATE_LIMITER_MAX_KEYS)
    app.config["LOGIN_ATTEMPT_SOURCE_RATE_LIMITER"] = _LoginRateLimiter(
        _LOGIN_SOURCE_RATE_LIMIT, _LOGIN_RATE_WINDOW_S,
        _LOGIN_RATE_LIMITER_MAX_KEYS)
    # umwelt#862 T5: per-resolved-sender send budget for send_crossing --
    # same per-app-instance isolation posture as the login limiters above
    # (never a shared module-level singleton).
    app.config["SEND_RATE_LIMITER"] = _SendRateLimiter(
        _SEND_RATE_MAX, _SEND_RATE_WINDOW_S, _SEND_RATE_KEY_MAX)
    things_dir = _things_dir(db_path)
    # Resolved once, at app-creation time — describes the running process,
    # not the request. A checkout that drifts under a running process is
    # exactly the deploy-lag we want /health to expose (umwelt #61).
    boot_commit = _resolve_commit_sha()
    boot_started_at = db.now_iso()
    # world_version manifest (umwelt #77 slice 3): defaults to a path derived
    # from db_path when not explicitly given. Read once at boot — the
    # operator bumps the file and restarts to activate a new version, same
    # cadence as commit (see docs/DEPLOY.md).
    boot_world_version_path = world_version_file or _default_world_version_path(db_path)
    # boot_world_version_path can be None when db_path is empty/falsy and no
    # explicit world_version_file was given (_default_world_version_path's
    # own guard) — skip the read rather than passing None into open().
    boot_world_version = (
        _resolve_world_version(boot_world_version_path) if boot_world_version_path else None
    )

    # retire_config manifest (umwelt #303): same defaults-derived-from-
    # db_path / read-once-at-boot cadence as world_version above. Missing
    # file -> both lists empty (fail-closed, see _resolve_retire_config).
    boot_retire_config_path = retire_config_file or _default_retire_config_path(db_path)
    boot_retire_config = (
        _resolve_retire_config(boot_retire_config_path) if boot_retire_config_path
        else {"maintainers": [], "retirable_identities": [],
              "bin_maintainers": []}
    )
    app.config["RETIRE_CONFIG"] = boot_retire_config

    # umwelt #227 S4: boot-time ensure of the project_board kind + the
    # project_registrar kind+instance — see _apply_effect_project_create's
    # docstring for why this can't be a lazy first-request derive the way
    # _ensure_project_board_kinddef alone is: the registrar's found_project
    # action needs _PROJECT_BOARD_KIND_ID to already exist, and act()'s
    # effect-dispatch signature has no things_dir to re-derive it from at
    # request time. A short-lived connection, same one-shot-at-app-creation
    # posture as boot_commit/boot_world_version above — every request still
    # opens its OWN connection via _open_db below; this one is closed
    # before the first request ever arrives. Requires db.init_db (and thus
    # WORKSHOP_PLACE_ID) to already exist — true for both main()'s boot
    # sequence and every test fixture (db.init_db + seed_places always run
    # before create_app is called).
    _boot_conn = sqlite3.connect(db_path)
    _boot_conn.execute("PRAGMA foreign_keys = ON")
    try:
        _ensure_project_board_kinddef(_boot_conn, things_dir)
        _ensure_project_registrar(_boot_conn, things_dir)
        # umwelt #400: kind-only ensure (no singleton instance — see
        # _CHARGE_BOARD_KIND_ID's own comment), same lazy-safe posture as
        # _ensure_project_board_kinddef's boot-time call above; nothing
        # downstream needs this at boot the way the registrar's
        # found_project effect needs the board kind, but self-healing a
        # pre-existing world on a pure code-swap promote is the same
        # motivation the other two ensures already have here.
        _ensure_charge_board_kinddef(_boot_conn, things_dir)
        _boot_conn.commit()
        # umwelt #991: kinddef file drift check -- one-shot, after the
        # ensures above so engine-authored kinds are already on disk. The
        # helper never raises; stderr (journal) is the operator signal.
        kinddef_drift = _kinddef_file_drift(_boot_conn, things_dir)
    finally:
        _boot_conn.close()
    app.config["KINDDEF_DRIFT"] = kinddef_drift
    for _m in kinddef_drift["missing"]:
        print(f"umwelt: WARNING kinddef {_m['id']} ({_m['name']}) "
              f"cannot load: {_m['reason']}", file=sys.stderr)

    @app.before_request
    def _open_db() -> None:
        g.conn = sqlite3.connect(db_path)
        g.conn.execute("PRAGMA foreign_keys = ON")
        g.conn.execute("PRAGMA busy_timeout = 5000")

    @app.before_request
    def _reject_non_object_body() -> Response | None:
        """Every body-reading route does `get_json(...) or {}` and then
        `.get(...)`, so a JSON list / string / number / true body used to
        500 (AttributeError). One choke point: a body that parses as valid
        JSON, is truthy, and is not an object is a 400. Falsy bodies ([],
        0, "", null, false), an empty body and invalid JSON are NOT touched:
        `or {}` already turns them into each route's own "X is required"
        response, which stays exactly as it was. A request whose routing
        already failed (unknown path -> 404, wrong method -> 405) never
        reaches a route, so it is left to Flask's own response. A body
        nested past the parser's recursion limit is answered like any other
        non-object body: get_json(silent=True) only swallows ValueError."""
        if request.routing_exception is not None:
            return None
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        try:
            parsed = request.get_json(force=True, silent=True)
        except RecursionError:
            parsed = [None]
        if parsed and not isinstance(parsed, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        return None

    @app.errorhandler(_FieldTypeRefusal)
    def _field_type_refusal(exc: _FieldTypeRefusal) -> tuple[Response, int]:
        """umwelt #906: a present non-string identifier/enum field is a 422 in
        the house error shape ({"error": "<field> must be a string (got
        <type>)"}). Registered for this private exception type ONLY -- not a
        before/after_request hook -- so it fires only when a route body raised
        it; requests that never reach a route keep Flask's own 404/405."""
        return jsonify({"error": str(exc)}), 422

    @app.teardown_request
    def _close_db(exc) -> None:
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.route("/health")
    def health() -> Response:
        # umwelt#531: bind_socket_status is a STRUCTURED field (Borges's
        # security-lane ruling, not prose) -- so a promotion check can
        # gate on it mechanically (`.bind_socket.available`) rather than
        # someone remembering to read a log line. Set by main() once,
        # before app.run(), after attempting BindSocketHandler.start();
        # defaults to "not started by this process" for any app built via
        # create_app() directly without main() (every test fixture, and
        # any future embedding) rather than implying a real attempt that
        # never happened.
        bind_socket_status = app.config.get(
            "BIND_SOCKET_STATUS",
            {"available": None, "error": None, "reason_class": None})
        # umwelt#909: status is no longer a fixed "ok". One read-only probe
        # on its own short-timeout connection; a failure says "degraded".
        # Still HTTP 200 either way, so process-up checks keep working.
        db_probe = _probe_db(db_path)
        body = {
            "status": "ok" if db_probe["ok"] else "degraded",
            "world": "umwelt",
            "db": db_path,
            "db_probe": db_probe,
            "commit": boot_commit,
            "started_at": boot_started_at,
            "world_version": boot_world_version,
            "bind_socket": bind_socket_status,
            # umwelt #991: BOOT-TIME snapshot of kinddef.json drift (not
            # re-read per request); a file fixed after boot shows until
            # the next restart. Additive -- `status` is unchanged.
            "kinddef_files": app.config["KINDDEF_DRIFT"],
        }
        # umwelt#922: backup freshness. Only present when a stamp path is
        # configured; re-read on EVERY request (no boot cache); null -- never
        # 0 -- for a missing/unreadable stamp. Always HTTP 200.
        stamp_path = app.config.get("BACKUP_STAMP_PATH")
        if stamp_path:
            from datetime import datetime, timezone
            last_ok = age = None
            try:
                with open(stamp_path, encoding="utf-8") as fh:
                    at = json.load(fh)["at"]
                dt = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - dt).total_seconds()
                if age < -_BACKUP_STAMP_FUTURE_SLACK_S:
                    raise ValueError("stamp dated in the future")
                last_ok = str(at)
                age = max(0.0, age)
            except (OSError, ValueError, KeyError, TypeError):
                last_ok = age = None
            body["backup_last_ok_at"] = last_ok
            body["backup_age_s"] = age
        return jsonify(body)

    # -- GET /  — human-facing read-only world view ----------------------
    # A window into the world for humans. Read-only by construction: it only
    # READS via db.* and renders HTML; it never mutates world state (the
    # engine enforces order; inhabitants change the world through the API,
    # not through this view). #webview
    @app.route("/")
    def world_view() -> Response:
        return Response(_render_world_html(), mimetype="text/html")

    # -- GET /projects — human-facing read-only project board view --------
    # umwelt #221 S3c. Same read-only-by-construction posture as world_view
    # above: this route only READS via db.* (through the two already-public
    # GET /umwelt/projects[/<id>] routes, fetched client-side) and renders a
    # static HTML shell — it never mutates world state and exposes no
    # mutation affordance at all (no register/act controls, unlike
    # world_view). private-network-scoped, no auth — same trust boundary as GET /.
    @app.route("/projects")
    def projects_view() -> Response:
        return Response(_render_projects_html(), mimetype="text/html")

    # -- places (the world map, JSON) ------------------------------------
    @app.route("/umwelt/places")
    def places() -> Response:
        """List places — flat legacy shape (no `agent`) or a contextual
        place_tree render (`?agent=<name>`), umwelt #158.

        No `agent` query param: EXACT legacy behavior, byte-for-byte
        unchanged (`db.list_places`) — every existing caller (the webview's
        anonymous bootstrap `loadPlaces()` fetch, `tools/uw`'s `places`
        verb, and the not-yet-restarted MCP `where_can_i_go()` tool, which
        calls this route with no query string at all) keeps working with
        ZERO behavior change until it's updated to pass one (new MCP tool
        params/wrappers are queued for tonight's restart window, per the
        issue's restart-constraint section).

        `?agent=<name>`: contextual render — `{current, parent, anchors}`,
        each a place_tree(viewer=agent)-shaped node (see db.place_tree),
        rooted at the agent's avatar's CURRENT place:
          - `current`: place_tree(current_place, depth=1, agent)'s root —
            the current place plus its direct children + lateral links.
          - `parent`: the way up (place_tree(parent_id, depth=0, agent)'s
            root), or null only if top-level (no parent row at all).
            Corrected per umwelt#538 rev 3: this used to also null when the
            parent was a private place the viewer didn't own — no longer
            true, since place_tree now always returns a node (locked, if
            the viewer can't enter) rather than pruning. The parent's
            EXISTENCE is unconditional now; only its interior fields
            (`thing_count`/`unread`) vary by `interior_visible`.
          - `anchors`: the agent's home post + the Square, each appended
            (as a depth=0 node) only if not already `current` or one of its
            direct children — so a caller standing at their own home or at
            the Square doesn't see a redundant duplicate anchor entry.

        `current.children` is bounded + sorted for display (umwelt #159 —
        the Discussion Hall room-list slice): unread-descending, tie-broken
        by most recent crossing activity descending, capped at
        `_PLACE_CHILDREN_BOUND`. This is a presentation-layer post-process
        ONLY (see `_bound_children`) — `db.place_tree` itself keeps
        returning the complete, uncapped tree; nothing else in this response
        (`parent`, `anchors`, the legacy no-`agent` branch) is affected. A
        top-level `children_skipped` field (sibling of `current`/`parent`/
        `anchors`, count of children beyond the 15 shown) is ALWAYS present
        — 0 when nothing was capped — matching #157's own `skipped` field
        on `crossings_here`, which is likewise always emitted rather than
        conditionally present.

        404 (agent-mode only): no avatar for `agent` — there is no "current
        place" to render from without one.
        """
        agent_raw = str(request.args.get("agent", "")).strip()
        if not agent_raw:
            # umwelt#621 slice 1c (Mira's ruling): the no-`agent`-param
            # branch stays exactly as documented -- EXACT legacy behavior,
            # zero change, no identity logic reached at all (not even a
            # token check). Only the contextual `?agent=` branch below is
            # in scope for conversion.
            return jsonify({"places": db.list_places(g.conn)})

        # umwelt#621 slice 1c: once a caller has declared an agent, this
        # branch is functionally REQUIRED-agent (same shape as look/trace)
        # -- resolve_actor replaces the direct _canonical_agent(agent_raw)
        # fold so a valid token wins over a conflicting declared agent
        # (anti-forgery) and a presented-but-unresolvable token 401s,
        # exactly the acceptance baseline #621 sets for every converted
        # route.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404
        current_place = avatar["place_id"]

        tree = db.place_tree(g.conn, current_place, 1, agent)
        current = tree[0] if tree else None

        children_skipped = 0
        if current is not None:
            # umwelt #159: bound/sort `current`'s children for display —
            # place_tree's own return value is left untouched (a fresh dict
            # is not needed since we're replacing the key in place on the
            # already-freshly-built `current` node).
            current["children"], children_skipped = _bound_children(
                g.conn, current["children"],
            )

        parent_place = db.get_place_parent(g.conn, current_place)
        parent = None
        if parent_place is not None:
            parent_tree = db.place_tree(g.conn, parent_place["id"], 0, agent)
            parent = parent_tree[0] if parent_tree else None

        # covered_ids drives the anchors dedup below — deliberately computed
        # from current's children AFTER bounding: if the room-list cap
        # pushed the agent's home/Square out of the visible list, it's no
        # longer "already shown" and the anchor entry is not redundant.
        covered_ids = {current_place}
        if current is not None:
            covered_ids |= {c["id"] for c in current["children"]}
        if parent is not None:
            covered_ids.add(parent["id"])

        anchors = []
        home_place_id = f"place_post_{agent}"
        for anchor_id in (home_place_id, "place_square"):
            if anchor_id in covered_ids:
                continue
            anchor_tree = db.place_tree(g.conn, anchor_id, 0, agent)
            if anchor_tree:
                anchors.append(anchor_tree[0])
                covered_ids.add(anchor_id)

        # umwelt #159: always present (0 when nothing capped) — matches
        # #157's own always-emitted `skipped` field, not omit-when-zero.
        response = {"current": current, "parent": parent, "anchors": anchors,
                    "children_skipped": children_skipped}
        return jsonify(response)

    # -- map (the interactive, filterable full-forest place map, #160) ------
    @app.route("/umwelt/map", methods=["GET", "POST"])
    def map_route() -> Response:
        """The interactive map: a forest-union of `place_tree` over every
        visible top-level place, with optional server-side filters
        (umwelt #160). GET/POST both supported with identical field names —
        GET reads query params, POST reads a JSON body.

        Fields: `agent` (required — the viewer; canonicalized via
        `_canonical_agent`, same pattern as `/umwelt/places?agent=`) ·
        `name` (optional, case-insensitive substring match on a node's
        label) · `kind` (optional, must be "public" or "private" if given)
        · `owner` (optional, exact-match against a node's owner) ·
        `unread_only` (optional bool — keep only nodes with `unread > 0`)
        · `depth` (optional int; omitted means unbounded — passed to
        `place_tree` as `float('inf')`, the map's canonical depth per
        #158's docstring; a non-negative int caps it instead; negative or
        non-numeric is a 400).

        Response 200: `{"roots": [<place_tree node>, ...]}` — one entry
        per visible top-level place from `db.list_top_level_places`, each
        expanded via `place_tree(root, effective_depth, agent)[0]`.

        Filter semantics (umwelt #160, documented here deliberately rather
        than silently picked — see the issue's #160 spec): `name`/`kind`/
        `owner`/`unread_only` are NOT a flat per-node keep/drop over the
        whole tree — that would silently orphan a matching descendant
        whose ancestor got dropped, or hide the path down to a real
        match. Instead a node is KEPT if it itself matches every supplied
        filter, OR if any of its descendants (recursively) match —
        filtering prunes the tree bottom-up but always preserves the path
        from a root down to any matching node, so the map stays a "find
        it and see how to get there" tool, not a flat grep (see
        `_prune_map_node`). A root with zero matching descendants that
        also doesn't itself match is dropped from `roots` entirely. This
        is a pure post-processing pass over the already-built
        `place_tree` node — `place_tree` remains the single source of
        tree+annotation truth; this route only prunes its already-
        computed output. No filters supplied (only `agent` given) returns
        the full forest UNPRUNED — every top-level `place_tree` call as-is.

        400 missing/empty `agent` · `kind` not in ("public", "private") ·
            `depth` negative or non-numeric ·
        404 no avatar for `agent`.
        """
        if request.method == "POST":
            params = request.get_json(force=True, silent=True) or {}
        else:
            params = request.args

        agent_raw = str(params.get("agent") or "").strip()
        if not agent_raw:
            return jsonify({"error": "agent is required"}), 400

        # umwelt#621 slice 1c: agent is REQUIRED here (400 above already
        # enforces it), so this is the ordinary REQUIRED-agent shape, same
        # as look/trace -- resolve_actor replaces the direct
        # _canonical_agent(agent_raw) fold, a valid token wins over a
        # conflicting declared `agent` (anti-forgery), and a presented but
        # unresolvable token 401s.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        kind_raw = params.get("kind")
        kind_filter = str(kind_raw).strip() if kind_raw else None
        if kind_filter and kind_filter not in ("public", "private"):
            return jsonify({
                "error": "kind must be 'public' or 'private'"
            }), 400

        depth_raw = params.get("depth")
        if depth_raw is None or depth_raw == "":
            effective_depth = float("inf")
        else:
            try:
                effective_depth = int(depth_raw)
            except (TypeError, ValueError, OverflowError):
                return jsonify({
                    "error": "depth must be a non-negative integer"
                }), 400
            if effective_depth < 0:
                return jsonify({
                    "error": "depth must be a non-negative integer"
                }), 400

        name_raw = params.get("name")
        name_filter = str(name_raw).strip() if name_raw else None

        owner_raw = params.get("owner")
        # umwelt #73: owner values are stored canonical (case-folded); fold
        # the filter the same way so "Ariadne" and "ariadne" both match —
        # an un-folded exact-match filter would silently return zero
        # results for a differently-cased caller input.
        owner_filter = (_canonical_agent(str(owner_raw).strip())
                         if owner_raw else None)

        unread_raw = params.get("unread_only")
        if isinstance(unread_raw, str):
            unread_only = unread_raw.strip().lower() in ("1", "true", "yes", "y", "on")
        else:
            unread_only = bool(unread_raw)

        filters_given = bool(name_filter or kind_filter or owner_filter
                              or unread_only)

        roots = []
        for top in db.list_top_level_places(g.conn, agent):
            tree = db.place_tree(g.conn, top["id"], effective_depth, agent)
            if not tree:
                # Defensive only, and unreachable in practice as of umwelt
                # #538: place_tree now returns [] ONLY for a from_id that
                # doesn't exist at all, and top["id"] just came from a real
                # row list_top_level_places read this same request — no
                # privacy gate can make it re-reject a root it just handed
                # back (list_top_level_places no longer filters by privacy
                # either; every top-level place is a root candidate now).
                continue
            node = tree[0]
            if filters_given:
                node = _prune_map_node(node, name_filter, kind_filter,
                                        owner_filter, unread_only)
                if node is None:
                    continue
            roots.append(node)

        return jsonify({"roots": roots})

    def _ensure_home_place(conn, agent: str, display: str | None = None) -> str:
        """Ensure the agent has a home place to be born into; returns its id.
        Single source of truth for the birth-home seeding shared by register()
        and enter() — open enrollment means a fresh name mints its own private
        Post on first entry (trust-scoped v0: `agent` is caller-declared, private-network-
        gated; closes with #24).

        `agent` MUST already be the canonical (case-folded) identity — it
        keys the place id and the owner column (umwelt #73). `display` is
        the raw typed form, used ONLY for the label/description text, and
        ONLY takes effect on first creation — db.upsert_place's INSERT OR
        IGNORE means an existing place's label is never overwritten by a
        later differently-cased call. Falls back to `agent` when not
        supplied (e.g. the boot-time seed path has no separate typed form).
        """
        display = agent if display is None else display
        home_place_id = f"place_post_{agent}"
        if db.get_place(conn, home_place_id) is None:
            db.upsert_place(conn, home_place_id, "private", agent,
                            f"{display.title()}'s Post",
                            f"{display.title()}'s private corner of the world.")
        return home_place_id

    _HOME_POST_PREFIX = "place_post_"

    def _home_post_birth_refusal(conn, agent: str):
        """umwelt #915: the 422 for a FIRST birth whose home post id would
        exceed PLACE_ID_MAX_LEN, else None. Shared by register() and enter()
        so the two doors cannot drift; called BEFORE _ensure_home_place so a
        refusal writes no place and no avatar.

        `agent` MUST be the canonical identity: the post id is built from the
        canonical form, and casefold can lengthen a name ("ß" -> "ss"), so the
        typed length is the wrong thing to measure. A RETURNING agent (an
        avatar already exists) is never refused, whatever its name length.
        "First birth" means no avatar yet: a name whose home post was seeded
        by seed_places but who never entered is still refused if over the cap.
        """
        if db.avatar_exists(conn, agent):
            return None
        name_max = PLACE_ID_MAX_LEN - len(_HOME_POST_PREFIX)
        if len(agent) <= name_max:
            return None
        return jsonify({
            "error": f"agent name exceeds the {name_max}-char limit "
                     f"(got {len(agent)} after normalisation) — the world "
                     f"mints your home post as {_HOME_POST_PREFIX}<name>, "
                     f"and a place id is capped at {PLACE_ID_MAX_LEN} chars"
        }), 422

    # -- create_place (residents open new purposed places, umwelt #99) ------
    # Lei ruled residents may "open new purposed places when they see the
    # need" — until this door, places existed only via db.seed_places (boot-
    # time) and _ensure_home_place (per-agent birth). This is also the seam
    # for "noticeboard = purposed place": a place's crossings are already a
    # multi-writer append-only log (crossings_here/send_crossing above), so
    # opening a place is enough to open a board — no separate board concept.
    @app.route("/umwelt/create_place", methods=["POST"])
    def create_place() -> Response:
        """Open a new purposed place.

        Body: {agent, label, description, kind?, id?, parent?}
        `kind` defaults to "public"; the only other value is "private".
        `id`, if omitted, is derived from `label`: `place_` + a slug
        (lowercase, each run of non-alphanumeric chars collapsed to a single
        `_`, leading/trailing `_` stripped).
        `parent` (umwelt #158): optional place id — defaults to `None`
        (top-level), NEVER the caller's current place. Deliberate: the
        issue's explicit call is "accidental nesting is worse than explicit
        flatness" — a resident who wants a nested place must say so, it is
        never inferred from where they happen to be standing.

        400 missing/empty agent, label, or description · kind not in
            ("public","private") · explicit id not matching
            `^place_[a-z0-9_]+$` · label slugs to empty (no derivable id —
            pass an explicit id instead) ·
        404 no avatar for agent (same pattern as take/put) · `parent` given
            but no such place exists ·
        422 the id (explicit OR derived) falls in the reserved
            `place_post_*` home namespace — homes are engine-born
            (_ensure_home_place), never resident-minted, so nothing a
            resident opens should even LOOK like someone's home. This fires
            for a DERIVED collision too (e.g. label "Post Office" would
            derive place_post_office) — the v0 rule is honest-but-blunt:
            pass a distinct explicit id (e.g. "place_postoffice") to route
            around an unlucky label, rather than silently mangling the slug
            to dodge the namespace ·
        409 id already exists — pass a distinct explicit id.

        `owner`: the canonical agent for kind='private', NULL for
        kind='public'; entry and reads of a private place are enforced (see
        PROTOCOL.md's Places NOTE). `parent` is recorded, not
        ownership-enforced.

        Response 201: {"place": {id, kind, owner, label, description, parent}}
        """
        body = request.get_json(force=True, silent=True) or {}
        # umwelt #880: a present label must be a string — before the str()
        # coercion below.
        if "label" in body:
            _label_type_err = _name_label_type_error(body["label"], "label")
            if _label_type_err:
                return jsonify({"error": _label_type_err}), 422
        # umwelt #900: a present, non-null description must be a string.
        if body.get("description") is not None:
            _desc_type_err = _name_label_type_error(
                body["description"], "description")
            if _desc_type_err:
                return jsonify({"error": _desc_type_err}), 422
        agent_raw = _str_identity(body, "agent")
        label = str(body.get("label", "")).strip()
        description = str(body.get("description", "")).strip()
        if not agent_raw or not label or not description:
            return jsonify({
                "error": "agent, label, and description are required"
            }), 400
        if len(label) > _NAME_MAX_LEN:
            return jsonify({
                "error": f"label exceeds the {_NAME_MAX_LEN}-char limit "
                         f"(got {len(label)}) — a label is a glanceable "
                         "name; put detail in description"}), 422
        # umwelt #874: a label renders as one line — no control characters.
        label_problem = _name_char_problem(label)
        if label_problem:
            return jsonify({"error": f"label {label_problem}"}), 422

        # umwelt #906: a present, non-null parent must be a string (kind
        # already 400s any non-string via the enum check below).
        _require_str(body.get("parent"), "parent")
        kind_raw = body.get("kind")
        kind = "public" if kind_raw is None else str(kind_raw).strip()
        if kind not in ("public", "private"):
            return jsonify({
                "error": "kind must be 'public' or 'private'"
            }), 400

        explicit_id = _str_identity(body, "id")
        if explicit_id:
            if not _PLACE_ID_RE.match(explicit_id):
                return jsonify({
                    "error": "id must match ^place_[a-z0-9_]+$"
                }), 400
            # umwelt #915: after the shape check, before anything is written.
            if len(explicit_id) > PLACE_ID_MAX_LEN:
                return jsonify({
                    "error": f"id exceeds the {PLACE_ID_MAX_LEN}-char limit "
                             f"(got {len(explicit_id)}) — a place id is "
                             "carried verbatim in printed commands"}), 422
            place_id = explicit_id
        else:
            slug = _PLACE_SLUG_RE.sub("_", label.lower()).strip("_")
            if not slug:
                return jsonify({
                    "error": "label does not derive a usable id — pass an "
                             "explicit id"
                }), 400
            place_id = f"place_{slug}"

        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        if place_id.startswith("place_post_"):
            return jsonify({
                "error": f"{place_id!r} falls in the reserved place_post_* "
                         "home namespace — pass a distinct explicit id"
            }), 422

        if db.place_exists(g.conn, place_id):
            return jsonify({
                "error": f"place {place_id!r} already exists — pass a "
                         "distinct explicit id"
            }), 409

        # umwelt #158: `parent` defaults to None (top-level) — validated
        # BEFORE the insert so a bad parent never leaves a half-created
        # place behind.
        parent_raw = body.get("parent")
        parent_id = str(parent_raw).strip() if parent_raw else None
        if parent_id and db.get_place(g.conn, parent_id) is None:
            return jsonify({
                "error": f"parent place {parent_id!r} not found"
            }), 404

        owner = agent if kind == "private" else None
        if not db.create_place(g.conn, place_id, kind, owner, label,
                                description):
            # Lost the true interleaved race: another create_place for the
            # same id won the insert between our place_exists pre-check and
            # this call (reviewer round-1, PR #101 — without the catch this
            # is an unhandled 500 instead of the same 409 the sequential
            # pre-check path above already returns).
            return jsonify({
                "error": f"place {place_id!r} already exists — pass a "
                         "distinct explicit id"
            }), 409
        if parent_id:
            # A brand-new place can never be its own ancestor's ancestor
            # (it has no children yet), so this can't raise the cycle
            # ValueError in practice — set_place_parent's own checks are
            # just its usual defense-in-depth here.
            db.set_place_parent(g.conn, place_id, parent_id)
        return jsonify({
            "place": {"id": place_id, "kind": kind, "owner": owner,
                      "label": label, "description": description,
                      "parent": parent_id}
        }), 201

    # -- place topology (umwelt #158): set_parent / add_link / remove_link --
    # Server-side today; MCP tool wrappers for these are queued for tonight's
    # restart window (same restart-constraint pattern as #157's PR #163) —
    # exposed today via tools/uw's set-parent/add-link/remove-link verbs.
    @app.route("/umwelt/set_parent", methods=["POST"])
    def set_parent() -> Response:
        """Set (or clear) a place's parent (umwelt #158).

        Body: {agent, place, parent?}  — `parent` omitted/null/empty clears
        the place back to top-level.

        Owner- or engine-gated per the issue — but RECORDED, not ENFORCED
        here (v0 trust model: no ACL check on `agent` is made).

        400 missing agent/place ·
        404 place not found · `parent` given but no such place exists ·
        409 would create a cycle — place_id becoming its own ancestor,
            directly or transitively (self-parent, parent==place, is the
            depth-0 instance of this same case).

        Response 200: {"place": place_id, "parent": parent_id|null}
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        place_id = _str_identity(body, "place")
        # umwelt #906: a present, non-null parent must be a string.
        _require_str(body.get("parent"), "parent")
        if not agent_raw or not place_id:
            return jsonify({"error": "agent and place are required"}), 400
        # umwelt#495 slice 1b: `agent` is not yet used to authorize this
        # write (v0 trust model — no ACL check is made against it here),
        # but the CALLER identity is resolved through resolve_actor so a
        # presented bad/unknown/expired token fails closed (401) rather
        # than mutating shared state as an unverified caller. `agent` is
        # held unused for the future ACL check. No token -> BYTE-IDENTICAL
        # (resolve_actor's declared-branch applies the same fold).
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        if db.get_place(g.conn, place_id) is None:
            return jsonify({"error": f"place {place_id!r} not found"}), 404

        parent_raw = body.get("parent")
        parent_id = str(parent_raw).strip() if parent_raw else None

        if parent_id is not None:
            if db.get_place(g.conn, parent_id) is None:
                return jsonify({
                    "error": f"parent place {parent_id!r} not found"
                }), 404

        # umwelt #166: the cycle check now runs ATOMICALLY with the write
        # inside db.set_place_parent (BEGIN IMMEDIATE, closes the TOCTOU
        # race two concurrent set_parent calls could hit) — no separate
        # pre-check here; db.CycleError maps to the same 409 body this
        # route always returned.
        try:
            db.set_place_parent(g.conn, place_id, parent_id)
        except db.CycleError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"place": place_id, "parent": parent_id}), 200

    @app.route("/umwelt/add_link", methods=["POST"])
    def add_link() -> Response:
        """Add a lateral link between two places (umwelt #158).

        Body: {agent, src, dst, label?, mutual?}  — `mutual` defaults to
        true (the common case per the issue): writes BOTH src->dst and
        dst->src rows. Pass `mutual: false` for a one-way link.

        Idempotent (umwelt #158's own spec): re-adding an existing link is
        NOT an error — it silently overwrites the label (see
        db.add_place_link) rather than duplicating or 409ing.

        400 missing agent/src/dst · mutual not a boolean ·
        404 src or dst not found.

        Response 200: {"src", "dst", "label", "mutual"}
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        src_id = _str_identity(body, "src")
        dst_id = _str_identity(body, "dst")
        if not agent_raw or not src_id or not dst_id:
            return jsonify({"error": "agent, src, and dst are required"}), 400
        # umwelt#495 slice 1b: `agent` is not yet used to authorize this
        # write (v0 trust model — no ACL check is made against it here),
        # but the CALLER identity is resolved through resolve_actor so a
        # presented bad/unknown/expired token fails closed (401) rather
        # than mutating shared state as an unverified caller. `agent` is
        # held unused for the future ACL check. No token -> BYTE-IDENTICAL
        # (resolve_actor's declared-branch applies the same fold).
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        if db.get_place(g.conn, src_id) is None:
            return jsonify({"error": f"place {src_id!r} not found"}), 404
        if db.get_place(g.conn, dst_id) is None:
            return jsonify({"error": f"place {dst_id!r} not found"}), 404

        label_raw = body.get("label")
        # umwelt #880: the label is OPTIONAL — an explicit null (or absent)
        # still means "no label", but anything else must be a string.
        if label_raw is not None:
            _label_type_err = _name_label_type_error(label_raw, "label")
            if _label_type_err:
                return jsonify({"error": _label_type_err}), 422
        label = str(label_raw).strip() if label_raw else None
        # umwelt #874: a supplied link label renders as one line.
        if label:
            label_problem = _name_char_problem(label)
            if label_problem:
                return jsonify({"error": f"label {label_problem}"}), 422

        mutual = body.get("mutual", True)
        if not isinstance(mutual, bool):
            return jsonify({"error": "mutual must be a boolean"}), 400

        db.add_place_link(g.conn, src_id, dst_id, label, mutual=mutual)
        return jsonify({
            "src": src_id, "dst": dst_id, "label": label, "mutual": mutual
        }), 200

    @app.route("/umwelt/remove_link", methods=["POST"])
    def remove_link() -> Response:
        """Remove a lateral link between two places (umwelt #158).

        Body: {agent, src, dst, mutual?}  — `mutual` defaults to true:
        removes BOTH the src->dst and dst->src rows. Pass `mutual: false`
        to remove only the one directed row.

        A no-op (200, not an error) if the link doesn't exist — matches
        add_link's idempotent posture (see db.remove_place_link).

        400 missing agent/src/dst · mutual not a boolean ·
        404 src or dst not found.

        Response 200: {"src", "dst", "mutual"}
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        src_id = _str_identity(body, "src")
        dst_id = _str_identity(body, "dst")
        if not agent_raw or not src_id or not dst_id:
            return jsonify({"error": "agent, src, and dst are required"}), 400
        # umwelt#495 slice 1b: `agent` is not yet used to authorize this
        # write (v0 trust model — no ACL check is made against it here),
        # but the CALLER identity is resolved through resolve_actor so a
        # presented bad/unknown/expired token fails closed (401) rather
        # than mutating shared state as an unverified caller. `agent` is
        # held unused for the future ACL check. No token -> BYTE-IDENTICAL
        # (resolve_actor's declared-branch applies the same fold).
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        if db.get_place(g.conn, src_id) is None:
            return jsonify({"error": f"place {src_id!r} not found"}), 404
        if db.get_place(g.conn, dst_id) is None:
            return jsonify({"error": f"place {dst_id!r} not found"}), 404

        mutual = body.get("mutual", True)
        if not isinstance(mutual, bool):
            return jsonify({"error": "mutual must be a boolean"}), 400

        db.remove_place_link(g.conn, src_id, dst_id, mutual=mutual)
        return jsonify({"src": src_id, "dst": dst_id, "mutual": mutual}), 200

    # -- register (the human-friendly seeded entry) -------------------------
    @app.route("/umwelt/register", methods=["POST"])
    def register() -> Response:
        """Register a new inhabitant (or reconnect a returning one) — the seeded,
        human-friendly way into the world (Lei's registration flow). Pick a name +
        describe your avatar -> the world seeds your home place + births your body
        with that description -> you land in the world, ready to walk. A returning
        name reconnects to its existing body (the description is ignored; you
        already have one).

        Body: {"agent": "<name>", "type": "human"|"agent", "lineage": "<optional>",
               "avatar_description": "<optional>"}
        Response: enter's shape (place + things + present + unseen + avatar) plus
        `born` (True on first-ever registration, False on reconnect).

        Trust scope (v0): `agent` is caller-declared/forgeable (ob_2721/#24) — anyone
        can register any name and mint a home + body under it (Sybil-on-avatars).
        Sound only among our_side callers today; the gated-registration decision
        (ob_0941) + engine-attested identity (#24) are what make this a real gate.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        if not agent_raw:
            return jsonify({"error": "a name is required to enter the world"}), 400
        # umwelt #900: a present, non-null avatar_description / lineage must
        # be a string — before the str() coercion below, and before any state
        # is touched (enter/customize_avatar already 422 the same shapes).
        for _ft_field in ("avatar_description", "lineage"):
            if body.get(_ft_field) is not None:
                _ft_err = _name_label_type_error(body[_ft_field], _ft_field)
                if _ft_err:
                    return jsonify({"error": _ft_err}), 422
        # umwelt #73: canonical (case-folded) identity is what's keyed/stored;
        # agent_raw (the typed form) survives only as display metadata, fed
        # to _ensure_home_place/get_or_create_avatar below for first-birth
        # label/name text — see _canonical_agent's docstring.
        agent = _canonical_agent(agent_raw)
        description = str(body.get("avatar_description", "")).strip() or None
        # Lei's account model: one account = one avatar with a name, a TYPE
        # (human, or agent + model lineage), and a customizable description.
        # Open registration for the local stage (supersedes the GATED call
        # ob_0941 for now): any admitted private-network caller can register, username-only, no
        # password — the same trust posture as the village today.
        atype = _str_identity(body, "type", "human").lower() or "human"
        if atype not in ("human", "agent"):
            atype = "human"
        lineage = str(body.get("lineage", "")).strip()
        meta = {"type": atype}
        if atype == "agent" and lineage:
            meta["lineage"] = lineage

        desc_cap, cap_err = _desc_cap_from_body(body)  # umwelt #147 (parity
        if cap_err:                                    # with move_to/enter)
            return jsonify({"error": cap_err}), 422

        refusal = _home_post_birth_refusal(g.conn, agent)  # umwelt #915
        if refusal is not None:
            return refusal
        born = not db.avatar_exists(g.conn, agent)
        home_place_id = _ensure_home_place(g.conn, agent, display=agent_raw)
        avatar = db.get_or_create_avatar(g.conn, agent, home_place_id,
                                         description, meta if born else None,
                                         display_name=agent_raw)
        place_id = avatar["place_id"]
        old_sp = db.get_standpoint(g.conn, agent, place_id)
        db.touch_standpoint(g.conn, agent, place_id)
        view = _place_view(g.conn, things_dir, agent, place_id, old_sp,
                           desc_cap=desc_cap)
        view["avatar"] = {"id": avatar["id"], "place_id": avatar["place_id"],
                          "description": avatar["description"],
                          "home_place_id": home_place_id}
        view["born"] = born
        if not born:
            # Reconnect: register only APPLIES description/lineage/type at
            # first birth (meta if born else None, above) — but pretending
            # success while silently dropping supplied fields is the exact
            # failure shape #55 is about. Echo what was ignored instead of
            # staying quiet about it; register's own application logic is
            # unchanged (still first-birth-only).
            #
            # Presence-based, not truthy-based: a caller who explicitly
            # supplies type="human" (the default) or an empty-string
            # lineage/avatar_description on reconnect still SUPPLIED the
            # field and deserves the ignored-echo — a truthy check on the
            # coerced value silently drops those cases (round-2 fix, mirrors
            # the same fix in enter()).
            ignored = []
            if "avatar_description" in body:
                ignored.append("avatar_description")
            if "lineage" in body:
                ignored.append("lineage")
            if "type" in body:
                ignored.append("type")
            if ignored:
                view["ignored"] = ignored
                view["hint"] = "use customize_avatar to change an existing avatar"
        return jsonify(view)

    # -- enter (birth-or-reconnect) -----------------------------------------
    @app.route("/umwelt/enter", methods=["POST"])
    def enter() -> Response:
        """Birth-or-reconnect: land wherever your avatar already stands.

        Body: {"agent": "<id>", "type"?: "human"|"agent", "lineage"?: str,
               "avatar_description"?: str}  — deliberately no `place` param;
        the avatar's own place_id decides where you land (that's the point
        of "entry = reconnect"). First-ever call for an agent births its
        avatar at its home place (place_post_<agent>); every later call
        reconnects at wherever the avatar's place_id currently points
        (typically wherever a prior move_to last left it).

        umwelt #60: the agent door now takes the same optional identity
        fields `register` does, applied the same way — ONLY at first-ever
        birth (reuses register's born-path meta/description construction,
        not a re-derivation). A reconnect with any of the three supplied
        applies nothing but LOUDLY echoes what it ignored (register's own
        `ignored`/`hint` shape from #55), rather than pretending success.
        Same validation posture as `customize_avatar`, stricter than
        `register`'s own silent-coerce-to-human: an unrecognized `type` or a
        non-string value for type/lineage/avatar_description 422s naming the
        problem instead of being dropped or coerced quietly.

        Response: same shape as move_to's success path (place + things +
        present + unseen_count), plus `avatar` (id + place_id + description +
        home_place_id of the body) and `born` (True on first-ever birth,
        False on reconnect — symmetry with register's own `born` field).

        Implementation note: this is a builder-decided endpoint, not a hard
        requirement from the architecture design — see the spec's §5 for the
        rationale (a dedicated verb keeps move_to's "go HERE, explicitly"
        contract from getting fuzzy with an optional-place reconnect branch).

        Trust scope (v0): the driver→avatar binding rides the caller-declared
        `agent` field — there is no engine-attested identity yet (ob_2721/
        #24). A forged `agent` value can reconnect to another agent's avatar,
        or claim to be someone it isn't. Sound only among our_side callers
        today; hardens when the identity layer lands. Same posture as
        world_view's private-read scoping.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        if not agent_raw:
            return jsonify({"error": "agent is required"}), 400
        # umwelt #73: canonical (case-folded) identity for keying/storage;
        # agent_raw (typed form) is fed to the display-preserving helpers
        # below at first birth only — see _canonical_agent's docstring.
        agent = _canonical_agent(agent_raw)

        # Validate the three optional identity fields BEFORE touching any
        # state — same tri-state posture customize_avatar uses (no silent
        # str() coercion; an unrecognized `type` or a non-string value 422s
        # naming the problem), stricter than register's own silent coerce.
        desc_state, desc_val = _tristate_field(body, "avatar_description")
        if desc_state == "invalid":
            return jsonify({
                "error": f"avatar_description must be a string "
                         f"(got {type(body['avatar_description']).__name__})"
            }), 422
        lineage_state, lineage_val = _tristate_field(body, "lineage")
        if lineage_state == "invalid":
            return jsonify({
                "error": f"lineage must be a string "
                         f"(got {type(body['lineage']).__name__})"
            }), 422
        type_state, type_val = _tristate_field(body, "type")
        if type_state == "invalid":
            return jsonify({
                "error": f"type must be a string "
                         f"(got {type(body['type']).__name__})"
            }), 422
        if type_state == "set" and type_val.lower() not in ("human", "agent"):
            return jsonify({
                "error": f"type must be one of ('human', 'agent'), "
                         f"got {type_val!r}"
            }), 422

        description = desc_val if desc_state == "set" else None
        lineage = lineage_val if lineage_state == "set" else ""
        atype = type_val.lower() if type_state == "set" else "human"
        # Only build a metadata dict when the caller actually supplied one of
        # the three fields — a truly bare `{"agent": ...}` call (no fields at
        # all) must keep enter's pre-#60 behavior of birthing with EMPTY
        # metadata (register, by contrast, always writes {"type": "human"}
        # even bare, since register's own account model always declares a
        # type; enter has no such account-model assumption and #60 is
        # additive, not a silent behavior change for callers who pass
        # nothing). Deliberate deviation from a literal "exactly register's
        # meta" reading — flagged in the handoff.
        any_field_supplied = any(
            k in body for k in ("type", "lineage", "avatar_description")
        )
        meta = None
        if any_field_supplied:
            meta = {"type": atype}
            if atype == "agent" and lineage:
                meta["lineage"] = lineage

        refusal = _home_post_birth_refusal(g.conn, agent)  # umwelt #915
        if refusal is not None:
            return refusal
        born = not db.avatar_exists(g.conn, agent)
        home_place_id = _ensure_home_place(g.conn, agent, display=agent_raw)
        # Exactly register's born-path call shape: description/meta only
        # actually land on a first-ever birth — get_or_create_avatar's
        # find-or-birth short-circuits to the find branch on reconnect, so
        # passing them unconditionally here is harmless.
        avatar = db.get_or_create_avatar(g.conn, agent, home_place_id,
                                         description, meta if born else None,
                                         display_name=agent_raw)
        place_id = avatar["place_id"]

        if db.get_place(g.conn, place_id) is None:
            # Shouldn't happen in practice (home places are seeded, and
            # move_to requires an existing place to move to) — but an honest
            # 500 beats a silent KeyError if it ever does.
            return jsonify({
                "error": f"avatar's place {place_id!r} not found"
            }), 500

        desc_cap, cap_err = _desc_cap_from_body(body)  # umwelt #147
        if cap_err:
            return jsonify({"error": cap_err}), 422

        old_sp = db.get_standpoint(g.conn, agent, place_id)
        db.touch_standpoint(g.conn, agent, place_id)
        view = _place_view(g.conn, things_dir, agent, place_id, old_sp,
                           desc_cap=desc_cap)
        view["avatar"] = {"id": avatar["id"], "place_id": avatar["place_id"],
                          "description": avatar["description"],
                          "home_place_id": home_place_id}
        view["born"] = born
        if not born:
            # Reconnect: fields are applied ONLY at first birth (meta if
            # born else None, above) — echo what was ignored instead of
            # pretending success, same shape register gained for #55.
            #
            # Presence-based (tri-state != "absent"), not truthy-based: a
            # caller who explicitly supplies type="human" (the default) or
            # an empty-string lineage/avatar_description on reconnect still
            # SUPPLIED the field via *_state == "clear" and deserves the
            # ignored-echo — checking the coerced value's truthiness instead
            # silently drops those cases (round-2 fix, mirrors the same fix
            # in register()).
            ignored = []
            if desc_state != "absent":
                ignored.append("avatar_description")
            if lineage_state != "absent":
                ignored.append("lineage")
            if type_state != "absent":
                ignored.append("type")
            if ignored:
                view["ignored"] = ignored
                view["hint"] = "use customize_avatar to change an existing avatar"
        return jsonify(view)

    # -- customize_avatar (self-dress: description/lineage/type/carry_capacity
    # after birth) --
    _CUSTOMIZE_AVATAR_KEYS = ("description", "lineage", "type",
                              "carry_capacity", "render_spec")

    @app.route("/umwelt/customize_avatar", methods=["POST"])
    def customize_avatar() -> Response:
        """Set/clear your OWN avatar's description, lineage, type,
        carry_capacity, and render_spec after birth.

        The narrower door umwelt #55 asks for: `enter`/`register` can only
        dress an avatar at first birth (umwelt #60 gave `enter` the same
        optional identity fields `register` already had — both doors now
        apply them only at first-ever birth, and reject/echo identically on
        a reconnect), never afterward. `update_thing` 422s on avatars by
        design (invariant #4 — avatars are
        engine-managed presence, not agent-editable). This verb is a
        separate, deliberately narrow door: exactly `description` /
        `metadata.lineage` / `metadata.type` / `metadata.carry_capacity` /
        `metadata.render_spec`, never `exec_command`, `place_id`, `name`,
        `kind`, or `author` — and only on the CALLER's own avatar.
        `update_thing`'s avatar rejection is untouched.

        `render_spec` (2026-07-24, world-finishing sprint, forum #277
        #5023/#5027): the missing write half of gateway.py's avatar
        serve-path (`_apply_avatar_render_spec`, umwelt#349) — before this,
        an avatar's `metadata.render_spec` could never be set by anyone, so
        every avatar rendered as a plain circle regardless of what the
        serve-path could resolve. Self-only, same as every other field
        here: there is no "set agent X's avatar" parameter anywhere in this
        route, so the identity binding (which avatar a render_spec attaches
        to) stays exactly as forgery-scoped as the rest of this route —
        never a new, wider hole. Mirrors the voice-enrollment mirror rule
        (`enroll(sample)` writes `embedding[caller]`, never `embedding[target]`
        — forum #277 #4893): `render_spec` here writes onto the CALLER's own
        avatar only, full stop.

        Body: {"agent": "<id>", "description"?: str, "lineage"?: str,
               "type"?: "human"|"agent", "carry_capacity"?: int,
               "render_spec"?: dict} — at least one of the five required.

        Tri-state per field: key absent -> leave unchanged; key present with
        a non-empty string -> set it (whitespace-stripped); key present as
        ""/null -> clear it (description resets to the engine default
        "<agent>'s presence in the world."; lineage/type keys are removed
        from metadata). `carry_capacity` is numeric, so it gets its own
        tri-state (umwelt #151): key absent -> leave unchanged; key present
        as `null` -> clear it (reverts to env UMWELT_CARRY_CAPACITY /
        CARRY_CAPACITY_DEFAULT on next read, see _carry_capacity); key
        present as a valid positive int -> set metadata.carry_capacity to
        it; anything else (bool, non-positive int, float, string, etc.) is
        invalid. `render_spec` is dict-shaped, so it ALSO gets its own
        tri-state: key absent -> unchanged; key present as `null` -> clear
        (pop metadata.render_spec, avatar reverts to a plain circle); key
        present as a dict -> set it VERBATIM, no content validation here
        (see `_render_spec_field`'s docstring for why: render-time, not
        write-time, is the single source of truth for well-formedness);
        anything else (string, list, int, bool) is invalid.

        No silent coercion or silent ignoring — that failure shape is what
        #55 is about: an unrecognized `type` value, a non-string value for
        `description`/`lineage`/`type` (e.g. a number or object), a
        non-positive-int value for `carry_capacity` (e.g. a bool — bool is
        an int subclass and is rejected explicitly, same guard #148's
        _desc_cap_from_body uses for desc_cap), a non-dict `render_spec`, or
        an unknown extra field in the body, all 422 naming the problem
        rather than being dropped quietly or silently coerced (stricter
        than `register`'s coerce-unknown-type-to-human; register's own
        validation is unchanged).

        Response 200: {"avatar": {"id", "place_id", "name", "description",
        "type", "lineage", "carry_capacity", "render_spec"}} —
        type/lineage/carry_capacity/render_spec null when absent from
        metadata, so callers can confirm what the world now shows.

        Trust scope (v0): the caller-declared `agent` is forgeable — same
        posture as enter/register/move_to (ob_2721/#24); a forged `agent`
        could dress another agent's avatar. Sound only among our_side
        callers today; hardens when the identity layer lands.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        if not agent_raw:
            return jsonify({"error": "agent is required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        if not db.avatar_exists(g.conn, agent):
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404
        # Same helper the enter path uses. home_place_id is unused here (the
        # avatar is already known to exist, so get_or_create_avatar's
        # find-or-birth short-circuits on the find branch) — passed anyway
        # for readability/consistency with the birth call sites.
        avatar = db.get_or_create_avatar(g.conn, agent, f"place_post_{agent}")

        extra = set(body) - {"agent", *_CUSTOMIZE_AVATAR_KEYS}
        if extra:
            return jsonify({
                "error": f"these fields are not updatable: {sorted(extra)} "
                         f"(allowed: {list(_CUSTOMIZE_AVATAR_KEYS)})"}), 422
        if not any(k in body for k in _CUSTOMIZE_AVATAR_KEYS):
            return jsonify({"error": "nothing to customize"}), 400

        type_state, type_val = _tristate_field(body, "type")
        if type_state == "invalid":
            return jsonify({
                "error": f"type must be a string (got {type(body['type']).__name__})"
            }), 422
        if type_state == "set" and type_val.lower() not in ("human", "agent"):
            return jsonify({
                "error": f"type must be one of ('human', 'agent'), got {type_val!r}"
            }), 422

        cc_state, cc_val, cc_err = _carry_capacity_field(body)
        if cc_state == "invalid":
            return jsonify({"error": cc_err}), 422

        rs_state, rs_val, rs_err = _render_spec_field(body)
        if rs_state == "invalid":
            return jsonify({"error": rs_err}), 422

        description = avatar["description"]
        metadata = dict(avatar.get("metadata") or {})

        desc_state, desc_val = _tristate_field(body, "description")
        if desc_state == "invalid":
            return jsonify({
                "error": f"description must be a string (got {type(body['description']).__name__})"
            }), 422
        if desc_state == "set":
            description = desc_val
        elif desc_state == "clear":
            description = f"{agent}'s presence in the world."

        lineage_state, lineage_val = _tristate_field(body, "lineage")
        if lineage_state == "invalid":
            return jsonify({
                "error": f"lineage must be a string (got {type(body['lineage']).__name__})"
            }), 422
        if lineage_state == "set":
            metadata["lineage"] = lineage_val
        elif lineage_state == "clear":
            metadata.pop("lineage", None)

        if type_state == "set":
            metadata["type"] = type_val.lower()
        elif type_state == "clear":
            metadata.pop("type", None)

        if cc_state == "set":
            metadata["carry_capacity"] = cc_val
        elif cc_state == "clear":
            metadata.pop("carry_capacity", None)

        if rs_state == "set":
            metadata["render_spec"] = rs_val
        elif rs_state == "clear":
            metadata.pop("render_spec", None)

        g.conn.execute(
            "UPDATE things SET description = ?, metadata = ? WHERE id = ?",
            (description, json.dumps(metadata), avatar["id"]),
        )
        g.conn.commit()

        return jsonify({"avatar": {
            "id": avatar["id"], "place_id": avatar["place_id"],
            "name": avatar["name"], "description": description,
            "type": metadata.get("type"), "lineage": metadata.get("lineage"),
            "carry_capacity": metadata.get("carry_capacity"),
            "render_spec": metadata.get("render_spec"),
        }})

    # -- move_to -----------------------------------------------------------
    @app.route("/umwelt/move_to", methods=["POST"])
    def move_to() -> Response:
        """Move to place_id — updates both the standpoint AND the avatar's body.

        Two independent writes answering two different questions (Ariadne):
        db.touch_standpoint is the read-cursor for crossings ("where was I
        last looking"); db.move_avatar is where the body physically is now.
        If the caller has no avatar yet (never called /umwelt/enter),
        move_avatar is a harmless no-op — move_to does not birth one.

        Trust scope (v0): the driver→avatar binding rides the caller-declared
        `agent` field — there is no engine-attested identity yet (ob_2721/
        #24). A forged `agent` value can move another agent's avatar, or
        claim to be someone it isn't. Sound only among our_side callers
        today; hardens when the identity layer lands. Same posture as
        world_view's private-read scoping.

        #28 decision 4 (trash-bin home-face): when the destination is the
        caller's OWN home post (place_post_<agent>), the response gains a
        `bin_count` field — the trash-bin home-face, realized as a count,
        not a new dynamic-thing mechanism. Keyed on place_id == the
        CALLER's own home specifically (not "any home place"), so moving to
        someone else's post never carries their bin_count.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        place_id = _str_identity(body, "place")
        if not agent_raw or not place_id:
            return jsonify({"error": "agent and place are required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        place = db.get_place(g.conn, place_id)
        if place is None:
            return jsonify({"error": f"place {place_id!r} not found"}), 404
        # umwelt#538 rev 3: entry is the thing rule 2 actually protects — a
        # private place's existence is public now (map/place_tree show it,
        # locked), but ENTERING it still requires being the owner (or a
        # grantee, once #538 part C's place_grants exists — it doesn't
        # yet). This check's LOGIC is unchanged from #523/#525: owner-only,
        # gated before either write below fires (db.touch_standpoint/
        # db.move_avatar must never run for a place the caller can't
        # enter).
        #
        # Response shape (umwelt#551, the candidate follow-up #542 flagged
        # and deferred, until Lei hit it live on his very first test-world
        # pass and it turned out not to be cosmetic): a locked, existing
        # place now 403s with its public projection in the body, distinct
        # from a genuinely nonexistent place's 404 above. #523's original
        # "never 403, it'd be an existence-oracle" reasoning no longer
        # applies — existence is the specification now (rule 2), not the
        # leak — so a 403 here discloses nothing a caller couldn't already
        # see on the map. Confirmed live: the identical wording on both
        # branches let a stray same-named test place manufacture a false
        # "can't enter my own post" bug report within minutes of Lei
        # hitting it, because there was no way to tell "not yours" from
        # "doesn't exist" from the response alone.
        if not db._place_interior_visible_to(place, agent):
            return jsonify({
                "error": f"place {place_id!r} exists but you may not enter it",
                **db._place_public_projection(place),
            }), 403

        desc_cap, cap_err = _desc_cap_from_body(body)  # umwelt #147
        if cap_err:
            return jsonify({"error": cap_err}), 422

        old_sp = db.get_standpoint(g.conn, agent, place_id)
        db.touch_standpoint(g.conn, agent, place_id)
        db.move_avatar(g.conn, agent, place_id)
        view = _place_view(g.conn, things_dir, agent, place_id, old_sp,
                           desc_cap=desc_cap)
        if place_id == f"place_post_{agent}":
            view["bin_count"] = len(db.list_binned(g.conn, agent))
        return jsonify(view)

    # -- place_view (read-only) ---------------------------------------------
    @app.route("/umwelt/place_view")
    def place_view() -> Response:
        """`GET /umwelt/place_view?place=<id>` — the read-only counterpart to
        move_to/enter, built specifically so a browser-facing render gateway
        (or any other spectator client) never has to call a MUTATING endpoint
        just to render a place.

        Makes ZERO writes to the database — no db.touch_standpoint, no
        db.move_avatar (contrast move_to, which calls both). A plain GET to
        this route can never move an avatar, and can never birth one either
        (unlike enter). This is the fix for the class of bug where a
        read-only gateway forwarded straight into move_to/enter: a GET could
        silently relocate any named avatar or mint a brand-new one from an
        arbitrary string.

        Trust posture — an OPTIONAL `agent` query param (umwelt#554), absent
        by default: a caller who supplies none gets exactly today's
        long-standing anonymous behaviour, byte-for-byte unchanged. A
        private place 404s (not-found), indistinguishable from a
        nonexistent place — closing the existence-oracle for guessable
        `place_post_<agent>` slugs (issue #208, from Aleph's off-host
        review) — UNLESS the supplied `agent` is that place's owner, in
        which case its full interior renders exactly as `look`/`move_to`
        already show the owner. As of umwelt #523, this route and move_to
        share the SAME posture: both enforce `db._place_interior_visible_to`
        before rendering/moving, and both now take the SAME kind of
        optional-but-declared `agent`.

        umwelt#554: this route was ORIGINALLY built with "no agent/viewer
        param at all" as its whole security story — #208's existence-oracle
        concern, closed by making private-vs-nonexistent indistinguishable
        to an identity-less caller. That story stopped being complete the
        moment umwelt#538 rev 3 made existence itself public (rule 2): a
        caller who already knows a place exists and is private gains
        nothing new from an `agent` param that only ever unlocks THEIR OWN
        place — but the webrender gateway's `/scene` route (the actual
        motivating caller) had no way to let a logged-in human see their
        OWN post's interior, since this route had nowhere to tell it who
        was asking. Recorded trade-off, not a side effect (Ariadne's
        security-lane note on #554): this route stops being the ONE
        genuinely anonymous-safe read route in this file — it is now
        exactly as safe as every other world route (`look`, `move_to`,
        `crossings_for_agent`), all of which already take a caller-DECLARED
        `agent` with no credential behind it, under Lei's compatibility window's private-network-open
        ruling (this is a v0 trust posture, closed by attestation #38 once
        identity is engine-resolved, same as everywhere else in this file).
        The SESSION-cookie gate that makes this genuinely safe for a real
        human lives ONE LAYER UP, in the webrender gateway's `/scene` route
        — it resolves `agent` from `session["avatar"]`, never from a
        client-supplied `viewer` label, before calling this route. This
        route itself enforces nothing beyond what it always enforced:
        `agent` is exactly as forgeable here as everywhere else in v0.

        `unseen_count` in the response is the RAW total unseen count from
        the beginning when no `agent` is supplied (`old_sp=None` — there is
        no real standpoint for an anonymous read) — but as of umwelt#554, a
        caller who DOES supply their own `agent` gets the SAME real delta
        `look`/`move_to` already give them (`_read_place` reads their real
        `db.get_standpoint` row, unconditionally on whether `agent` is
        given — see that function's own docstring), with no separate code
        path needed here. Expected to stay the raw total for a spectator
        view (no `agent` supplied); a logged-in caller viewing their own
        place now gets their real unseen delta, same as every other
        `agent`-aware read.

        `reachable` is computed by passing THIS route's own `agent` param
        (None by default, same as everywhere else in this docstring)
        through to `_place_view` (see its docstring) — db.place_tree treats
        `agent=None` as "no identity context," same as it always has.
        Corrected per umwelt#538 rev 3: this used to say a private
        child/lateral-linked place is PRUNED from `reachable` for an
        anonymous viewer — no longer true, since a place's existence is
        public regardless of viewer now (place_tree emits the locked node,
        it never prunes for privacy). As of umwelt#554, an `agent`-bearing
        caller's `reachable` list correctly reflects THEIR OWN additional
        owned-place visibility too — the same interior-access gate this
        whole docstring already describes, applied consistently rather
        than carved out for this one field.

        umwelt #219/forum #263: this route is `_read_place`'s
        `place_id=<given>` case — `/umwelt/look` below is the
        `place_id=None` case of the SAME primitive (always the CALLER's own
        current place, resolved from their avatar). As of umwelt#554, BOTH
        routes take the SAME optional `agent` shape; what still
        distinguishes them is which `place_id` each resolves, not who may
        supply an identity.
        """
        place_id = str(request.args.get("place", "")).strip()
        if not place_id:
            return jsonify({"error": "place is required"}), 400
        # umwelt#621 slice 1c (Mira's confirmed shape, security seat,
        # 2026-08-22): this is the OPTIONAL-agent resolve_actor shape --
        # genuinely different from every REQUIRED-agent route's
        # `if resolution.agent_id is None: return 401` idiom, because
        # agent_id=None is this route's own legitimate ANONYMOUS outcome,
        # not an auth failure. Branch on `source` instead: only
        # "invalid_token" (a presented Authorization header that did not
        # resolve) refuses -- every other source (token/declared/none)
        # passes `agent_id` through AS-IS, including None, which preserves
        # all four outcomes: (1) a valid token overrides a conflicting
        # declared `agent` (anti-forgery, unchanged); (2) an
        # invalid/malformed/empty Authorization header 401s; (3) no header
        # + a declared agent preserves today's backfill-window compat
        # identity (still forgeable during the window -- deliberately NOT
        # tightened here, that is a later cutover decision, not something
        # to smuggle into this slice); (4) no header + no declared agent
        # stays anonymous, byte-for-byte the pre-conversion behavior. This
        # also correctly keeps a future window-closed no-token call
        # (source="none") anonymous rather than misreading it as failed
        # authentication -- this route never required an identity to begin
        # with. umwelt#554's own absent/blank->None sentinel discipline
        # (see the prior version of this comment) still applies: resolve_actor's
        # declared-branch already does the identical `_canonical_agent(x)
        # if x else x` fold internally, so this call is a drop-in replacement,
        # not a new rule.
        agent_raw = str(request.args.get("agent", "")).strip()
        resolution = resolve_actor(g.conn, request, declared=agent_raw or None)
        if resolution.source == "invalid_token":
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        try:
            view = _read_place(g.conn, things_dir, place_id, agent)
        except _ReadPlaceError as e:
            return jsonify({"error": e.message}), e.status
        return jsonify(view)

    # -- look (read-only, caller's own current place) ------------------------
    @app.route("/umwelt/look", methods=["POST"])
    def look() -> Response:
        """`POST /umwelt/look {agent}` — umwelt #139/#219, forum #263: the
        side-effect-free where-am-i. Renders the CALLER's own current place
        (`avatar.place_id`) via the shared `_read_place` primitive (see its
        docstring) — the `place_id=None` case, resolved from the caller's
        avatar, never from a caller-supplied place param.

        Makes ZERO database writes — no db.touch_standpoint, no
        db.move_avatar. Contrast `move_to`, which writes both; this is
        purely a read, so a client (a cross-host bell, an MCP name-res
        backend, anything that just needs "where is X right now") never has
        to call a mutating endpoint to answer that question.

        400 `agent` missing/blank. 404 the agent has no avatar yet (never
        called `/umwelt/enter`) — same "no avatar for X — enter the world
        first" shape every other agent-gated route in this file uses. The
        private-place-invisible-to-non-owner branch of `_read_place` should
        not normally arise here (this always renders the caller's OWN
        current place) — and, as of umwelt #523, `move_to` itself enforces
        `db._place_interior_visible_to` before writing `avatar.place_id`, so an
        avatar's current place should always be one it may legally see. The
        route still goes through the same predicate for uniformity with
        `/umwelt/place_view`, and as a backstop for any avatar whose
        place_id was set by another path before #523 landed.

        Trust scope (v0): same caller-declared `agent` posture as every
        other verb in this file — forgeable, closed by attestation #38 (see
        PROTOCOL.md's `/umwelt/look` entry for the fuller statement of this,
        umwelt #219).
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        if not agent_raw:
            return jsonify({"error": "agent is required"}), 400
        # umwelt#495 slice 3a (surface conversion): resolve_actor replaces
        # the direct _canonical_agent(agent_raw) fold. No token -> BYTE-
        # IDENTICAL to before (resolve_actor's own declared-branch does the
        # same _canonical_agent(agent_raw) fold). A valid bearer token now
        # wins over `agent_raw` entirely (anti-forgery) -- declared is
        # ignored when a token resolves. The refuse branch below is
        # UNREACHABLE today (db.backfill_window_open is unconditionally
        # True) -- it exists so a future window-close needs zero edits
        # here, per the "read from ONE place" rider (resolve_actor's own
        # switch, never a per-route copy of the three-state rule).
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        try:
            view = _read_place(g.conn, things_dir, None, agent)
        except _ReadPlaceError as e:
            return jsonify({"error": e.message}), e.status
        return jsonify(view)

    # -- look_at -----------------------------------------------------------
    @app.route("/umwelt/look_at", methods=["POST"])
    def look_at() -> Response:
        """See PROTOCOL.md's `look_at` verb entry for the full response
        shape.

        `agent_state` (umwelt #191): SELF-SCOPED — when `agent` is
        supplied, the caller's OWN thing_agent_state key/value pairs on
        this thing (db.get_agent_state), if any. Omitted entirely (not an
        empty object) when `agent` is absent/blank or the caller holds no
        state on this thing — same "no shape change for existing
        consumers" posture as gate_notice/status/checklist_render above.
        No endpoint (this one included) ever returns another agent's
        state — db.get_agent_state has no all-agents variant, by design.

        `metadata.graft_ref`/`graft_version` (umwelt#486): AUTHOR-ONLY.
        These two keys are SERVER-OWNED (the `advance_ref` capsule
        compare-and-swap chain, umwelt #186) — a pointer into content-
        addressed storage, not ordinary author-set flavor data. `look_at`
        was readable by anyone who could name a thing, no place-visibility
        check and no owner check, so any capsule's current ref was
        exfiltratable by a non-owner for free. Stripped from `metadata`
        (the two keys ONLY — everything else an author puts in metadata,
        e.g. `color`/`render_spec`/`checklist`, stays exactly as visible
        as it always was; `mud_client`'s `examine` and the webrender
        gateway's sprite lookups both depend on non-owner metadata reads
        for legitimate, non-sensitive fields, so this must not become a
        blanket strip) when the caller is not `thing["author"]` (including
        an anonymous/absent `agent`, the safe default). Every DERIVED
        rendering (gate_notice/checklist_render/board_render/status/
        use_stats/agent_state) is unaffected either way — those already
        compute from metadata into their own scoped sibling keys. No
        shape change for the author's own reads.
        """
        body = request.get_json(force=True, silent=True) or {}
        thing_id = _str_identity(body, "thing")
        # umwelt#495 slice 3a (surface conversion): resolve_actor replaces
        # the direct _canonical_agent fold — see /umwelt/look's identical
        # comment for the full rationale. `agent` stays OPTIONAL here
        # (self-scoped agent_state rendering only): an empty `agent_raw`
        # resolves through resolve_actor's declared-branch to "" (falsy,
        # not None), so every existing `if agent:`/`if not agent:` check
        # below is unaffected — same no-op posture as the old
        # _canonical_agent("") call. A valid bearer token still wins over
        # a blank/absent `agent` (anti-forgery), which is new here.
        agent_raw = _str_identity(body, "agent")
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        if not thing_id:
            return jsonify({"error": "thing is required"}), 400
        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        # umwelt#982: the thing read gate -- BEFORE any content is built.
        refusal = _thing_view_refusal(g.conn, agent or None, thing, thing_id)
        if refusal is not None:
            return refusal
        # umwelt #88 slice 4 / #151: size_class — the "this is large, be
        # mindful" expectation BEFORE any read. thing dict is mutated (a copy
        # of db.get_thing's dict, safe to extend) rather than nested, so
        # existing callers keying off thing[...] keep working unchanged.
        thing = {**thing, **_size_fields(things_dir, thing)}
        # umwelt #102: name the gate's CONSEQUENCE (not just the class) at
        # look_at time, before any use() call — reuses the EXACT same
        # constant use()'s gated readable branch renders, so before/after
        # can never drift into two different wordings. Omitted entirely
        # (not set to null) when not gated, so existing consumers see no
        # shape change.
        if thing["size_class"] in _GATED_SIZE_CLASSES:
            thing["gate_notice"] = _TOME_POINTER_TEMPLATE.format(
                name=thing["name"], size_class=thing["size_class"])
        response = {"thing": thing,
                    "use_stats": db.use_stats(g.conn, thing_id)}
        # umwelt #133: avatars are look-at-able for STATUS too — last
        # action/last-active + emotion, alongside the description already
        # on `thing`. Sibling key to `use_stats` (not nested under `thing`),
        # omitted entirely for non-avatar things, same "no shape change for
        # existing consumers" posture as gate_notice above. `author` on an
        # avatar-kind thing is the owning agent — see db.get_avatar's
        # convention / _THING_COLS.
        if thing["kind"] == "avatar":
            response["status"] = _avatar_status(g.conn, thing["author"])
        # umwelt #182: a non-empty metadata.checklist gets a rendered
        # `checklist_render` sibling key, same "omitted entirely when N/A,
        # no shape change for existing consumers" posture as gate_notice/
        # status above — makes a checklist readable in every client that
        # renders look_at, for free.
        checklist_render = _render_checklist(thing)
        if checklist_render is not None:
            response["checklist_render"] = checklist_render
        # umwelt #221 S3a: a bound project_board instance gets a rendered
        # `board_render` sibling key, same "omitted entirely when N/A, no
        # shape change for existing consumers" posture as gate_notice/
        # status/checklist_render above.
        board_render = _render_project_board(g.conn, thing)
        if board_render is not None:
            response["board_render"] = board_render
        # umwelt #537: a charge_board instance gets a rendered
        # `charge_board_render` sibling key, same "omitted entirely when
        # N/A, no shape change for existing consumers" posture as
        # gate_notice/status/checklist_render/board_render above. New key —
        # deliberately NOT `board_render`, which is project_board's existing
        # key (see _render_charge_board's docstring for why the two kinds
        # don't share a render or a key).
        charge_board_render = _render_charge_board(thing)
        if charge_board_render is not None:
            response["charge_board_render"] = charge_board_render
        # umwelt #876 (D11b): same gate as charge_board_render immediately
        # above (both return None under the identical condition — see
        # _render_charge_board_headline's docstring) — a bounded sibling
        # key: newest 5 entries in full + at most 30 older as headlines by
        # default. `charge_board_render` itself is UNCHANGED — the web
        # inspect panel and history=true both still read it.
        charge_board_headline = _render_charge_board_headline(thing)
        if charge_board_headline is not None:
            response["charge_board_headline"] = charge_board_headline
        # umwelt #486: graft_ref/graft_version are author-only within
        # metadata — see the docstring's paragraph above. Filter AFTER
        # every render above (they all read metadata off `thing`
        # themselves) so this is the single exit point for the raw dict,
        # never nested/duplicated elsewhere. Deliberately a denylist of
        # exactly the two server-owned pointer keys, NOT a blanket strip
        # of `metadata` — ordinary author-set fields (color, render_spec,
        # checklist, ...) stay visible to non-owners exactly as before;
        # `mud_client`'s examine and the webrender gateway's sprite
        # lookups both broke under an earlier, over-broad draft of this
        # fix that stripped all of metadata, which is how this scope was
        # settled (see test_look_at_metadata_scope.py). `agent` is
        # caller-declared (trust-scoped v0, same forgeable posture as
        # every other verb in this file) so this is not a hard security
        # boundary, but it does close the passive, no-effort read: an
        # unauthenticated `look_at` no longer hands out another agent's
        # capsule ref for free.
        if (not agent or agent != thing.get("author")) and thing.get("metadata"):
            thing["metadata"] = {
                k: v for k, v in thing["metadata"].items()
                if k not in _LOOK_AT_NON_AUTHOR_METADATA_DENYLIST
            }
        # umwelt #191: self-scoped agent_state — ONLY the caller's own
        # rows, and only when the caller identified itself. No other
        # agent's state is ever reachable through this (or any) route.
        if agent:
            agent_state = db.get_agent_state(g.conn, thing_id, agent)
            if agent_state:
                response["agent_state"] = agent_state
        return jsonify(response)

    # -- use ---------------------------------------------------------------
    @app.route("/umwelt/use", methods=["POST"])
    def use() -> Response:
        """Execute a thing. See PROTOCOL.md's `use` verb entry for the full
        `args` contract (umwelt #57): optional JSON array of strings,
        argv-appended onto the shlex-split exec_command — never coerced,
        never silently dropped, never string-interpolated or re-joined
        through a shell. Limits and readable-things-reject are enforced
        here, at the route, before the sandbox ever runs anything.

        `metadata.graft_ref`/`graft_version` in the gated-readable
        (tome/library) branch (umwelt#488): AUTHOR-ONLY, same posture and
        same shared `_LOOK_AT_NON_AUTHOR_METADATA_DENYLIST` as `look_at`
        (umwelt#486) — see that docstring's paragraph for the full
        reasoning. `use()`'s `agent` is optional/telemetry-only, so a
        blank/absent `agent` is treated as non-author (the safe default),
        same as everywhere else this denylist is applied. Stripped from
        BOTH the raw `metadata` dict AND `manifest.metadata_keys` — key
        *names* alone would still leak whether a thing has been through
        `advance_ref` even with values stripped, so `metadata_keys` is
        computed from the already-filtered metadata, not the raw dict.
        `manifest`'s other fields (name/description/size_class/
        content_length_estimate/author/created_at) are unaffected.
        """
        body = request.get_json(force=True, silent=True) or {}
        thing_id = _str_identity(body, "thing")
        # umwelt #73: canonical identity key. `use`'s `agent` is optional
        # (telemetry only — see _record_use below), so _canonical_agent("")
        # is the correct no-op when omitted.
        # umwelt#495 slice 1b: resolve_actor replaces the inline
        # _canonical_agent fold. `agent` is OPTIONAL here (telemetry only
        # — see _record_use below), so absent agent -> declared="" ->
        # resolve_actor returns "" (non-None) -> passes, byte-identical to
        # the old _canonical_agent("") no-op. A PRESENTED bad token still
        # returns 401 (fail-closed: a bad credential on thing-execution
        # must not silently run as an unverified caller).
        agent_raw = _str_identity(body, "agent")
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        if not thing_id:
            return jsonify({"error": "thing is required"}), 400

        args_raw = body.get("args")
        args_err = _validate_use_args(args_raw)
        if args_err:
            return jsonify({"error": args_err}), 422
        args = list(args_raw) if args_raw else []

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        # umwelt#982: the thing read gate -- before the source is read or
        # anything runs / is stamped.
        refusal = _thing_view_refusal(g.conn, agent or None, thing, thing_id)
        if refusal is not None:
            return refusal
        # Completeness check (Borges, PR #33 security review): use()/act_on
        # on a body is a category error per invariant #4 (presence, not a
        # usable) regardless of whether it happens to carry an exec_command.
        # Not currently reachable via exec_command (create_thing and
        # update_thing both reject body-kind, so an avatar's exec_command is
        # always NULL) — this is defense in depth + an honest error message
        # rather than falling through to "executable thing has no
        # exec_command", which would be true-but-misleading on a body.
        if db.is_body_kind(thing["kind"]):
            return jsonify({
                "error": "avatars are presence, not a usable thing — "
                         "there is nothing to use() on a body"
            }), 422

        source = _read_source(things_dir, thing_id)

        if thing["kind"] == "readable":
            # Readables have nothing to invoke args against — a non-empty
            # args on a readable is a category error, not a no-op (loud,
            # not silent — same posture as #57's whole complaint).
            if args:
                return jsonify({"error": "readable things take no args"}), 422
            # umwelt #88 slice 4 — examine gating: use() (via look_at's
            # "read path") is the ONE render site that puts a thing's full
            # `source` on the wire. scroll/book render exactly as before
            # (content: source, in full). tome/library NEVER render source
            # inline — metadata + a manifest + the fixed pointer line
            # instead; the deep read happens outside the world's render path
            # (Lei's repo-thing answer — see #88's issue body).
            size_class = _size_class_for(things_dir, thing, source=source)
            if size_class in _GATED_SIZE_CLASSES:
                pointer = _TOME_POINTER_TEMPLATE.format(
                    name=thing["name"], size_class=size_class)
                # umwelt #488: graft_ref/graft_version are author-only
                # within metadata — same posture and same shared denylist
                # as look_at (umwelt #486, see that route's docstring).
                # `use()`'s `agent` is optional/telemetry-only, so a
                # blank/absent agent is treated as non-author, the safe
                # default, same as look_at. Computed ONCE here and reused
                # for both `metadata` and `manifest.metadata_keys` below —
                # metadata_keys must derive from the FILTERED dict, not
                # the raw one, because key *names* alone (even with values
                # stripped) still leak whether a thing has been through
                # advance_ref. `manifest`'s other fields are unaffected.
                gated_metadata = thing.get("metadata") or {}
                if not agent or agent != thing.get("author"):
                    gated_metadata = {
                        k: v for k, v in gated_metadata.items()
                        if k not in _LOOK_AT_NON_AUTHOR_METADATA_DENYLIST
                    }
                manifest = {
                    "name": thing["name"],
                    "description": thing["description"],
                    "size_class": size_class,
                    "content_length_estimate": db.compute_content_token_estimate(
                        source, thing["description"]),
                    "author": thing["author"],
                    "created_at": thing["created_at"],
                    "metadata_keys": sorted(gated_metadata.keys()),
                }
                _record_use(g.conn, thing_id, agent, "readable")
                return jsonify({"outcome": {
                    "briefing": pointer, "kind": "readable",
                    "size_class": size_class,
                    "metadata": gated_metadata,
                    "manifest": manifest,
                }})
            # readable things: use() returns their content, no execution.
            n = len(source)
            briefing = (f"You opened '{thing['name']}' — it holds "
                        f"{n} file{'s' if n != 1 else ''}.")
            _record_use(g.conn, thing_id, agent, "readable")
            return jsonify({"outcome": {"briefing": briefing,
                                        "content": source, "kind": "readable",
                                        "size_class": size_class}})

        if thing["kind"] == "blueprint":
            # umwelt #79 slice-1: a blueprint carries a parameterized
            # create_thing spec-template (source's blueprint.json) +
            # a declared param schema (metadata.params). use()'s blueprint
            # branch has the ENGINE (not the sandbox) stamp a new thing by
            # substituting caller args into the template, single-pass — see
            # _substitute_blueprint_holes for the load-bearing correctness
            # property (Kepler's rider: no re-scanning a substituted value).
            if not agent:
                return jsonify({
                    "error": "agent is required to use() a blueprint — "
                             "the engine needs a caller to author and place "
                             "the stamped thing"
                }), 400

            # umwelt#1011: the same may-enter check create_thing applies -- an
            # occupant who may no longer enter the place they stand in cannot
            # stamp gifts there either. It sits HERE, the earliest point the
            # avatar's place is known, so it precedes ALL params handling and
            # every use-ledger / use-event write: a refused occupant gets the
            # 403, never a params-spec 200 or a recorded use. Read-only lookup
            # (the avatar is born, if ever, further down). A missing avatar or
            # place is deliberately left as it was -- this check skips, and the
            # no-avatar 422 below answers; create_thing's 404 for those cases
            # is a different route's contract, not this one's.
            _bp_avatar = db.get_avatar(g.conn, agent)
            if _bp_avatar is not None:
                here = db.get_place(g.conn, _bp_avatar["place_id"])
                if here is not None:
                    refusal = _may_not_enter_refusal(here, agent)
                    if refusal is not None:
                        return refusal

            # umwelt #396 Trigger Logic: a non-empty declared params list
            # changes what "no args" / "malformed args" mean — see the
            # matching block in the executable branch below for the full
            # rationale (kept in lockstep so blueprint and executable never
            # drift on trigger semantics). umwelt #407: resolution goes
            # through _declared_params, so a non-list `params` is inert
            # rather than a hard error — see that function's docstring.
            params = _declared_params(thing_id, thing)
            if params:
                if not args:
                    _record_use(g.conn, thing_id, agent, "params_spec")
                    return jsonify(_params_spec_payload(thing_id, thing, params)), 200
                values, bind_err = _bind_params(params, args)
                if bind_err:
                    _record_use(g.conn, thing_id, agent, "params_spec",
                                error=bind_err)
                    payload = _params_spec_payload(thing_id, thing, params)
                    payload["error"] = bind_err
                    return jsonify(payload), 200
            else:
                values, bind_err = _bind_params(params, args)
                if bind_err:
                    return jsonify({"error": bind_err}), 422

            raw_template = source.get("blueprint.json")
            if raw_template is None:
                return jsonify({
                    "error": "blueprint is missing its blueprint.json spec-template"
                }), 422
            try:
                template = json.loads(raw_template)
            except (json.JSONDecodeError, TypeError) as e:
                return jsonify({
                    "error": f"blueprint.json is not valid JSON: {e}"
                }), 422
            if not isinstance(template, dict):
                return jsonify({
                    "error": "blueprint.json must be a JSON object"
                }), 422

            # Substitution values must be strings (they're spliced into
            # string template fields); a null/absent default becomes "" —
            # an ambiguous corner the spec left open, flagged in the handoff.
            sub_values = {k: ("" if v is None else str(v)) for k, v in values.items()}
            stamped_spec = _substitute_blueprint_holes(template, sub_values)
            if not isinstance(stamped_spec, dict):
                return jsonify({
                    "error": "blueprint.json must substitute to a JSON object"
                }), 422

            # The ENGINE resolves the place — the caller's own avatar's
            # current place_id, never a caller-supplied place (spec 2c).
            if not db.avatar_exists(g.conn, agent):
                return jsonify({
                    "error": f"no avatar for {agent!r} — enter the world "
                             "first; there is nowhere to stamp the new thing"
                }), 422
            avatar = db.get_or_create_avatar(g.conn, agent, f"place_post_{agent}")
            place_id = avatar["place_id"]
            # umwelt#1011: revalidate the EXACT destination. This is a fresh
            # read of the avatar's place, so it may differ from the snapshot
            # the early check above looked at (the avatar moved in between);
            # the check that guards the write must run on the place the write
            # lands in. The early check stays: it is what keeps the params /
            # use-ledger effects behind the guard. Nothing for the stamp has
            # been written yet at this point. A missing place row keeps the
            # pre-existing behaviour (no new refusal is invented here;
            # _create_thing_from_spec answers as it always did).
            _stamp_place = db.get_place(g.conn, place_id)
            if _stamp_place is not None:
                refusal = _may_not_enter_refusal(_stamp_place, agent)
                if refusal is not None:
                    return refusal
            # Provenance (spec 2d): the stamped thing records who stamped it
            # FROM — merged into whatever metadata the template itself
            # produced (a meta-blueprint's template may declare its own
            # metadata.params for the thing it stamps; stamped_from rides
            # alongside that, not in place of it).
            stamped_meta = stamped_spec.get("metadata")
            if not isinstance(stamped_meta, dict):
                stamped_meta = {}
            stamped_meta["stamped_from"] = thing_id
            stamped_spec["metadata"] = stamped_meta

            try:
                new_thing_id, _created_at = _create_thing_from_spec(
                    g.conn, things_dir, agent, place_id, stamped_spec)
            except _ThingSpecError as e:
                return jsonify({"error": e.message}), e.status

            briefing = (f"You used '{thing['name']}' — the engine stamped a "
                        f"new thing: {stamped_spec.get('name', new_thing_id)!r}.")
            _record_use(g.conn, thing_id, agent, "blueprint")
            return jsonify({"outcome": {"stamped_thing_id": new_thing_id,
                                        "briefing": briefing}})

        if not thing["exec_command"]:
            return jsonify({"error": "executable thing has no exec_command"}), 422

        # umwelt #396 Trigger Logic: a non-empty declared params list turns
        # empty/malformed args into a spec-return (the caller's "--help")
        # instead of a run. A thing with NO declared params (the
        # overwhelming majority today) never reaches this block — `params`
        # is falsy and both branches below are skipped, so use() behaves
        # byte-for-byte as before this PR (the single most important
        # regression bound of this whole feature). Binding here is a CHECK
        # only, never a transform: on a successful bind the ORIGINAL,
        # unbound `args` still go to the sandbox unchanged (spec: "run the
        # thing exactly as today").
        #
        # umwelt #407: resolution goes through _declared_params rather than
        # reading metadata directly, so a NON-LIST `params` (grandfathered
        # prose, written before this field meant anything) is treated as no
        # declaration and the thing runs as it always did. A malformed LIST
        # still fails loudly through _bind_params below — see
        # _declared_params' docstring for why that line is drawn there.
        params = _declared_params(thing_id, thing)
        if params:
            if not args:
                _record_use(g.conn, thing_id, agent, "params_spec")
                return jsonify(_params_spec_payload(thing_id, thing, params)), 200
            _, bind_err = _bind_params(params, args)
            if bind_err:
                _record_use(g.conn, thing_id, agent, "params_spec", error=bind_err)
                payload = _params_spec_payload(thing_id, thing, params)
                payload["error"] = bind_err
                return jsonify(payload), 200

        outcome = sandbox.run_thing_capped(
            source, thing["exec_command"],
            db_path=app.config["DB_PATH"],
            args=args,
            caller=agent,  # umwelt #186: advance_ref's owner gate needs the caller
            sprite_dir=app.config.get("CAP_SPRITE_DIR"),
            art_service_url=app.config.get("ART_SERVICE_URL"),
        )
        if "error" not in outcome:
            outcome["briefing"] = _narrate_use(thing["name"], outcome)
        _record_use(g.conn, thing_id, agent, "executable",
                    exit_code=outcome.get("exit_code"),
                    timed_out=bool(outcome.get("timed_out", False)),
                    elapsed_s=outcome.get("elapsed_s"),
                    error=outcome.get("error"))
        return jsonify({"outcome": outcome})

    # -- download (umwelt #122) ---------------------------------------------
    @app.route("/umwelt/download", methods=["POST"])
    def download() -> Response:
        """The sanctioned egress path the tome/library examine-gate now
        points at instead of dead-ending (#122's refuse-and-redirect). Same
        id-addressable, reach-free resolution as look_at/use — a thing
        inside a container is fine.

        The server returns DATA only; it never writes the caller's
        filesystem. `download` is a client/API verb (like `forge`), NOT a
        `use()`-sandbox action — the sandbox is network/FS-isolated from the
        caller's real disk, so a sandboxed action could never write it
        anyway. Writing the returned source dict to disk is the CLIENT's
        job (MUD / `uw` / MCP wrapper — a separate build, not this route).
        """
        body = request.get_json(force=True, silent=True) or {}
        thing_id = _str_identity(body, "thing")
        # umwelt#982: `agent` is OPTIONAL here (anonymous = a viewer that may
        # enter public places only), resolved through resolve_actor like
        # look_at/use -- the gate below needs to know who is asking, and a
        # token's identity wins over a declared one (anti-forgery).
        agent_raw = _str_identity(body, "agent")
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        if not thing_id:
            return jsonify({"error": "thing is required"}), 400

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        # umwelt#982: the thing read gate -- before any source is read.
        refusal = _thing_view_refusal(g.conn, agent or None, thing, thing_id)
        if refusal is not None:
            return refusal

        # Completeness check, same category error use() gives a body
        # (avatar) — presence, not a usable/downloadable thing (invariant
        # #4). Checked BEFORE the readable-only guard below so an avatar
        # gets this specific, correctly-named error rather than the generic
        # readable-only 422.
        if db.is_body_kind(thing["kind"]):
            return jsonify({
                "error": "avatars are presence, not a usable thing — "
                         "there is nothing to use() on a body"
            }), 422

        # umwelt #122 SECURITY GUARD (readable-only v1 — Borges's security
        # seat) — the load-bearing line of this endpoint.
        #
        # A READABLE's content is CONTEXT-gated, never ACCESS-gated: the
        # tome/library examine-gate exists to protect the READER's context
        # window from an inline render, not to withhold the content itself
        # — the gate's own briefing already invites "examine it with your
        # own tools." So downloading a readable (tome/library included)
        # crosses no confidentiality boundary — the content was never
        # access-gated, only context-gated (kept out of the inline-render
        # channel to protect the reader's context window). Egressing it to
        # disk is a delivery-channel change, not a new disclosure.
        #
        # A NON-readable's source is ACCESS-gated: use() returns an
        # executable's run OUTPUT (never its source) and a blueprint/kinddef
        # is instantiated (never dumped as a template) — so egressing THAT
        # source here would be genuinely NEW disclosure use() never granted.
        # Hence readable-only in v1; source-egress for other kinds is
        # deferred to the provenance/signing north-star. The guard is
        # `kind == "readable"`, nothing looser — do not widen this without
        # a corresponding security review.
        if thing["kind"] != "readable":
            return jsonify({"error": "download is readable-only in v1"}), 422

        # Full source, UNGATED regardless of size_class — a tome/library
        # readable IS downloadable; that is the entire point of this verb
        # (the gate blocks inline RENDER, not egress).
        source = _read_source(things_dir, thing_id)
        content_length = sum(len(v or "") for v in source.values())
        return jsonify({
            "source": source,
            "name": thing["name"],
            "size_class": _size_class_for(things_dir, thing, source=source),
            "content_length": content_length,
        })

    # -- create_thing (the raw build primitive) ----------------------------
    @app.route("/umwelt/create_thing", methods=["POST"])
    def create_thing() -> Response:
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        place_id = _str_identity(body, "place")
        spec = body.get("spec") or {}

        if not agent_raw or not place_id:
            return jsonify({"error": "agent and place are required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        # umwelt#989 (write side), Lei's rule 2026-10-04: a thing is created
        # ONLY at the place the creator is standing in ("so the created thing
        # is shown to the creator right after it's created ... create in
        # place, then move it"). Deny gifts by default: forging into someone
        # else's private Post is the case this covers, and it falls out of the
        # same check: the creator must be standing there NOW. (Standing is
        # not the same as being allowed to enter: an avatar can be left
        # standing in a place after a grant is revoked or the place's kind
        # changes. umwelt#1011: that avatar may NOT create there either, so
        # the may-enter check below follows the standing check.)
        # This is the CALLER layer: `_create_thing_from_spec` itself stays
        # open to engine-internal callers; use()'s blueprint branch resolves
        # its destination from the caller's avatar (so it can never differ
        # from where the user stands) and applies the same may-enter check.
        #
        # Checked BEFORE any source-tree or DB write (the helper below is the
        # first writer). Missing place stays 404 (a place's existence is
        # public, #538); an existing place the actor is not standing in is an
        # informational 403 -- never a bare reject -- naming where they ARE
        # standing and carrying the destination's public projection. An actor
        # with no avatar has no current place (the same "enter the world
        # first" 404 act/take/put give). The actor is the RESOLVED one.
        dest = db.get_place(g.conn, place_id)
        if dest is None:
            return jsonify({"error": f"place {place_id!r} not found"}), 404
        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent!r} — enter the world "
                         "first; things are created where you stand"
            }), 404
        if avatar["place_id"] != place_id:
            here = db.get_place(g.conn, avatar["place_id"])
            here_proj = (db._place_public_projection(here) if here is not None
                         else {"id": avatar["place_id"]})
            here_name = here["label"] if here is not None else avatar["place_id"]
            return jsonify({
                "error": f"things can only be created where you stand: you "
                         f"are in {here_name!r} ({avatar['place_id']}), not "
                         f"in {dest['label']!r} ({place_id}) — move there "
                         "first, or create it here and move it",
                "status": "not_standing_here",
                "standing_at": here_proj,
                "place": db._place_public_projection(dest),
            }), 403

        # umwelt#1011: standing is not enough -- you must also be ALLOWED to
        # enter the place. An avatar left standing in a place it may no longer
        # enter (grant revoked, kind changed) cannot read what lies there (the
        # read gate refuses it) yet could still drop a gift into someone
        # else's private place, so it is refused.
        # Same position as the standing refusal: before spec validation and
        # before any write.
        refusal = _may_not_enter_refusal(dest, agent)
        if refusal is not None:
            return refusal

        # spec-shape validation now lives in _create_thing_from_spec (#79),
        # which raises the same "spec must be an object" 400.

        # _create_thing_from_spec is the SAME internal path use()'s
        # blueprint branch calls to engine-stamp a thing from a template
        # (umwelt #79 slice-1) — one code path for both agent-authored and
        # engine-authored specs.
        try:
            thing_id, created_at = _create_thing_from_spec(
                g.conn, things_dir, agent, place_id, spec)
        except _ThingSpecError as e:
            return jsonify({"error": e.message}), e.status

        return jsonify({"thing_id": thing_id, "author": agent,
                        "created_at": created_at}), 201

    # -- update_thing (author-only edit) -----------------------------------
    # Fix a mistaken forge without re-creating it. Author-only: the authz gate
    # keys on thing.author == caller. NOTE the gate is TRUST-SCOPED — `agent` is
    # caller-declared (no auth in v0), so it enforces author-only among
    # cooperating our_side agents but is not impersonation-resistant until agent
    # identity is engine-attested (umwelt #24; the shared foundation dv_0230).
    @app.route("/umwelt/update_thing", methods=["POST"])
    def update_thing() -> Response:
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        thing_id = _str_identity(body, "thing")
        patch = body.get("patch") or {}
        if not agent_raw or not thing_id:
            return jsonify({"error": "agent and thing are required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        if not isinstance(patch, dict):
            return jsonify({"error": "patch must be an object"}), 400
        # umwelt #400/#494: reject ANY `_SERVER_OWNED_METADATA_KEYS` member
        # in a metadata patch — same write-guard posture as create_thing's
        # matching rejection above (see that constant's definition for the
        # full rationale, including why `entries` must stay off the
        # separate read-visibility denylist). A non-dict patch["metadata"]
        # is left to whatever (unrelated) handling already exists below;
        # this only guards the shape act() and the board_entry_*/
        # advance_ref mechanisms themselves write. Before #400 this was two
        # separate literal checks (action_log; graft_ref/graft_version) —
        # #494 flagged that shape as fail-open on a new server-owned key
        # nobody remembers to add to every literal check.
        if isinstance(patch.get("metadata"), dict):
            _server_owned_hit = (
                _SERVER_OWNED_METADATA_KEYS & patch["metadata"].keys())
            if _server_owned_hit:
                return jsonify({
                    "error": _server_owned_metadata_message(
                        _server_owned_hit, "the request body")
                }), 422

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        # Category error, not an authorization question — fires before the
        # author check since it's true regardless of who's asking. Avatars
        # are engine-managed presence, not agent-editable (invariant #4);
        # without this, a patch could set exec_command on a body (the exact
        # thing invariant #4 exists to prevent) or let a forged agent rename
        # another agent's avatar (same trust-scoped-v0 class as move_to).
        if db.is_body_kind(thing["kind"]):
            return jsonify({
                "error": "avatars are engine-managed presence, not "
                         "agent-editable — use move_to to relocate a body"
            }), 422
        # umwelt#300 S0(d): declarative, kinddef-level update_thing
        # permission — replaces the old bare author-only check. A
        # NULL-cohort thing (every thing forged before this PR, per
        # things.update_policy_cohort's own comment) keeps the exact old
        # gate, unconditionally (Mira's security acceptance criteria point
        # 2 — no silent retroactive flip). A cohort-marked thing resolves
        # its effective callers via its kind chain (_resolve_update_callers)
        # and is evaluated through the SAME `_callers_authorized` loop
        # act()'s own authorization uses (point 1 — one declarative
        # policy, one resolver).
        #
        # This is a FAST, non-atomic pre-check against the already-fetched
        # `thing` — it exists so an unauthorized caller is refused BEFORE
        # any of the validation/filesystem-mutating work below ever runs
        # (a source patch's _replace_source call, in particular, must
        # never touch disk for a caller this gate would refuse). It is
        # NOT the check that closes the concurrent-policy-edit race (Mira's
        # point 6) — that's the atomic re-check immediately preceding the
        # actual db write, at the bottom of this route (see the comment
        # there for why the race window is closed there, not here).
        # (Resolved ONCE and used for both the authorization decision and the
        # umwelt#982 gate below, so this pre-check still resolves the policy
        # exactly once -- the atomic re-check at the bottom is the second.)
        effective_callers = _update_thing_effective_callers(
            g.conn, things_dir, thing)
        if not _callers_authorized(g.conn, effective_callers, agent, thing):
            return jsonify({"error": "not authorized to update this thing"}), 403

        # umwelt#982: the thing read gate, ONLY where a kind grants `anyone`
        # (the default for a typed, new-cohort thing whose kinddef declares
        # no `update_callers`). `anyone` is an open door onto the thing, so
        # it must not reach past the place's own door: a non-enterer cannot
        # write into a private-place thing, same as `act`. A plain
        # author-only / named / granted policy is an explicit grant to a
        # specific party and is NOT touched here: its refusal text for an
        # unauthorized caller stays as it was (no global id-opacity claim).
        if "anyone" in effective_callers:
            refusal = _thing_view_refusal(g.conn, agent, thing, thing_id)
            if refusal is not None:
                return refusal

        extra = set(patch) - set(_MUTABLE_THING_KEYS)
        if extra:
            return jsonify({
                "error": f"these fields are not updatable: {sorted(extra)} "
                         f"(allowed: {list(_MUTABLE_THING_KEYS)})"}), 422
        if not patch:
            return jsonify({"error": "patch is empty — nothing to update"}), 400
        # umwelt #906: a present exec_command must be a string (null keeps its
        # meaning) — before the str() step below and any sqlite write.
        if "exec_command" in patch:
            _require_str(patch["exec_command"], "exec_command")
        # kind is immutable, so an executable must keep a runnable exec_command —
        # catch a blanking patch here, not at next use() (create_thing parity).
        if (thing["kind"] == "executable" and "exec_command" in patch
                and not str(patch["exec_command"] or "").strip()):
            return jsonify({"error": "an executable thing requires a non-empty exec_command"}), 400
        # umwelt #253: the same name cap create_thing runs at forge time,
        # mirrored here — same update-time parity rationale as the
        # description cap immediately below.
        if "name" in patch:
            # umwelt #880: must be a string — before len()/control-char scan.
            _name_type_err = _name_label_type_error(patch["name"], "name")
            if _name_type_err:
                return jsonify({"error": _name_type_err}), 422
            patched_name = str(patch["name"] or "")
            if len(patched_name) > _NAME_MAX_LEN:
                return jsonify({
                    "error": f"name exceeds the {_NAME_MAX_LEN}-char limit "
                             f"(got {len(patched_name)}) — a name is a "
                             "glanceable label; put detail in description"}), 422
            # umwelt #874: no control characters in a name.
            name_problem = _name_char_problem(patched_name)
            if name_problem:
                return jsonify({"error": f"name {name_problem}"}), 422

        # umwelt #147 prong 1: the same description hard cap create_thing runs at
        # forge time, mirrored here — without update-time parity a bomb could
        # enter via a patch (same forge-vs-update gap #79's review found for
        # blueprint.json).
        if "description" in patch:
            # umwelt #900: must be a string (null included — it was a 500
            # here) — before the len()/str() step and any sqlite write.
            _desc_type_err = _name_label_type_error(
                patch["description"], "description")
            if _desc_type_err:
                return jsonify({"error": _desc_type_err}), 422
            patched_desc = str(patch["description"] or "")
            if len(patched_desc) > _DESCRIPTION_MAX_LEN:
                return jsonify({
                    "error": f"description exceeds the {_DESCRIPTION_MAX_LEN}-"
                             f"char limit (got {len(patched_desc)}) — a "
                             "description is a short blurb; put long content in "
                             "the thing's source"}), 422

        if "source" in patch:
            source = patch["source"]
            if not isinstance(source, dict):
                return jsonify({"error": "patch.source must be an object of relpath->contents"}), 400
            # Same gate create_thing runs at forge time (_validate_blueprint_source):
            # a blueprint's source patch must still carry a parseable
            # blueprint.json, or the corruption would otherwise defer silently
            # to the next use() instead of failing loudly here (umwelt #79
            # PR review — forge-time validation with no update-time parity
            # left this gap).
            if thing["kind"] == "blueprint":
                try:
                    _validate_blueprint_source(source)
                except _ThingSpecError as e:
                    return jsonify({"error": e.message}), e.status
            elif thing["kind"] == "kinddef":
                # umwelt #109 round-2 suggestion #4: a kinddef's source patch
                # gets the same shape validation create_thing runs at forge
                # time (_validate_kinddef_source), AND a re-run of the
                # extends-chain cycle/depth guard (_validate_kind_extends,
                # excluding this kind's OWN id — see that function's
                # docstring) — closing named-simplification #1, where a
                # source patch could otherwise construct an unvalidated
                # extends cycle that only surfaced later, at the next
                # act()-time chain walk.
                try:
                    parsed_kinddef = _validate_kinddef_source(source)
                    _validate_kind_extends(
                        g.conn, things_dir, parsed_kinddef.get("extends"),
                        exclude=thing_id)
                except _ThingSpecError as e:
                    return jsonify({"error": e.message}), e.status
            # umwelt #906: every file's contents must be a string — a non-string
            # (null included) reached write_text in _replace_source and was a
            # 500. After the blueprint/kinddef validators (which keep their own
            # 4xx for the file they parse), and before the deferred write.
            for _contents in source.values():
                if not isinstance(_contents, str):
                    return jsonify({
                        "error": "patch.source contents must be strings "
                                 f"(got {_json_type_name(_contents)})"}), 422
            # umwelt#300 S0(d), Mira's colleague-review blocker on 4e2e12c:
            # the WRITE itself must not happen here. A caller authorized by
            # the fast pre-check above can have `update_callers` tightened
            # by a concurrent request in the time it takes to reach this
            # line — if _replace_source ran unconditionally here, that
            # caller's content would land on disk even though the atomic
            # re-check below (correctly) denies moments later, and a
            # filesystem write does not roll back with the SQLite
            # transaction. So the write is deferred into `_do_write` below,
            # inside the SAME BEGIN IMMEDIATE block that re-checks
            # authorization — it only runs once that fresh check has
            # already passed, closing the window Mira's review found.
            # Shape/kinddef/blueprint VALIDATION stays here (pure checks,
            # no side effect, and failing fast on a malformed patch before
            # the DB transaction opens is still correct) — only the actual
            # disk write moves.

        if "metadata" in patch:
            meta_patch = patch["metadata"]
            if not isinstance(meta_patch, dict):
                return jsonify({"error": "patch.metadata must be an object"}), 400
            existing_kind_id = thing["metadata"].get("kind_id")
            if existing_kind_id:
                # umwelt #109 round-2 BLOCKER fix: a kind-typed thing's
                # metadata patch must not be able to bypass act()'s
                # authorization model (PROTOCOL.md's "Typed things" section
                # names the merge semantics this enforces). Engine-owned
                # keys join the pre-existing action_log rejection (checked
                # unconditionally above, for every thing) — status only
                # transitions through act(), and kind_id is immutable once
                # set.
                if "status" in meta_patch:
                    return jsonify({
                        "error": "metadata.status is engine-owned for a "
                                 "kind-typed thing — use act to transition "
                                 "status"
                    }), 422
                if "kind_id" in meta_patch:
                    return jsonify({
                        "error": "metadata.kind_id is immutable once set"
                    }), 422
                # Merge, don't replace: the pre-existing full-column-replace
                # semantics (db.update_thing) let an innocent patch (e.g.
                # changing `priority`) silently wipe engine-owned state by
                # omission — this was the blocker's second reproduction
                # (action_log erasure via a metadata dict that simply
                # doesn't mention it). A patch value of `null` clears that
                # key (same tri-state convention as customize_avatar's
                # description/lineage/type fields) rather than setting a
                # literal `None`, so a patch CAN still remove a field —
                # just not by silent omission.
                merged_metadata = _merge_metadata_patch(thing["metadata"], meta_patch)
                # Re-impose the stored engine-owned values after the merge
                # — defense in depth on top of the two 422s above (which
                # already keep status/kind_id out of meta_patch entirely),
                # so this stays correct even if a future change loosens
                # those guards.
                for engine_key in ("action_log", "status", "kind_id"):
                    if engine_key in thing["metadata"]:
                        merged_metadata[engine_key] = thing["metadata"][engine_key]
                    else:
                        merged_metadata.pop(engine_key, None)
                # Required-field check on the merged result (create-time
                # validator reused) — a patch can no longer remove a
                # kind-required field, closing named-simplification #2.
                try:
                    _validate_kind_required_fields(
                        g.conn, things_dir, existing_kind_id, merged_metadata)
                except _ThingSpecError as e:
                    return jsonify({"error": e.message}), e.status
                patch = dict(patch)
                patch["metadata"] = merged_metadata
            else:
                # umwelt #393: untyped things get the SAME merge-not-replace
                # treatment as kind-typed ones (above) — a raw full-column
                # replace let one patched key silently erase every sibling
                # key on every call, which is what forced PR #392's entire
                # client-side compensation mechanism (fetch-then-merge +
                # engine-owned-key stripping + graft-tracked refusal). None
                # of that compensation is needed once the server merges: the
                # top-level guards already reject action_log/graft_ref/
                # graft_version in ANY patch unconditionally (checked above,
                # before this branch), for typed and untyped things alike —
                # so a merge (which leaves untouched keys alone) preserves
                # them correctly without a separate re-imposition step. That
                # is also why `status` needs none of the kind-typed branch's
                # 422-and-reimpose treatment here: for an untyped thing
                # status isn't an engine-owned concept (only act()'s
                # kind-typed state machine makes it one) — it's just an
                # ordinary metadata key like any other, and the merge alone
                # already stops it from being silently wiped by omission.
                merged_metadata = _merge_metadata_patch(thing["metadata"], meta_patch)
                # Setting kind_id for the FIRST time is how a thing BECOMES
                # typed — validate required fields against the MERGED result
                # (not just meta_patch), since a required field could
                # already be sitting in the thing's pre-existing metadata
                # rather than arriving in this same patch.
                new_kind_id = meta_patch.get("kind_id")
                if new_kind_id is not None:
                    try:
                        _validate_kind_required_fields(
                            g.conn, things_dir, str(new_kind_id), merged_metadata)
                    except _ThingSpecError as e:
                        return jsonify({"error": e.message}), e.status
                patch = dict(patch)
                patch["metadata"] = merged_metadata

            # umwelt #182 round 2: the SAME create-time-door validator
            # create_thing calls, re-run here for patch-time parity (#147's
            # description-cap precedent — "without update-time parity a
            # bomb could enter via a patch"). `patch["metadata"]` at this
            # point holds merged_metadata (umwelt #393: both branches merge
            # now, kind-typed and untyped alike) — one call covers both.
            try:
                _validate_checklist_metadata(patch["metadata"])
            except _ThingSpecError as e:
                return jsonify({"error": e.message}), e.status

            # umwelt #396: same patch-time parity as the checklist validator
            # immediately above — `patch["metadata"]` is already the MERGED
            # result by this point (both the kind-typed and untyped branches
            # above set it), so a `params` key already sitting in existing
            # metadata plus a patch that only touches an unrelated key still
            # gets (re-)validated, same principle umwelt #393 established for
            # _validate_kind_required_fields's own merged-not-raw-patch check.
            params_err = _validate_params_declaration(patch["metadata"].get("params"))
            if params_err:
                return jsonify({"error": params_err}), 422

        db_patch = {k: v for k, v in patch.items() if k != "source"}
        # umwelt #88 slice 1: weight_est is derived, never caller-set — recompute
        # it here (server-side) whenever name/description change, using the
        # POST-patch value for whichever field(s) DIDN'T change. Injected into
        # the internal db_patch dict only; weight_est is not in
        # _MUTABLE_THING_KEYS, so a caller can never set it directly.
        if "name" in patch or "description" in patch:
            new_name = patch.get("name", thing["name"])
            new_description = patch.get("description", thing["description"])
            db_patch["weight_est"] = db.compute_weight_est(new_name, new_description)
        # umwelt#300 S0(d), Mira's security acceptance criteria point 6:
        # policy cannot be widened by a failed/stale check. The fast
        # pre-check above ran against `thing` as fetched at the TOP of
        # this route — everything between there and here (source writes,
        # metadata-merge validation) took real wall-clock time, during
        # which a concurrent update_thing on the GOVERNING kinddef could
        # have changed its `update_callers` declaration. Re-resolve and
        # re-check authorization from a FRESH read, atomically WITH the
        # write itself, under one BEGIN IMMEDIATE transaction
        # (db.atomic_authorized_write — see its docstring for why this
        # closes the race and why it's not db._atomic_guarded_write,
        # which is CycleError-typed for a different kind of denial). If
        # the fresh read now denies (kinddef tightened mid-flight), the
        # write never runs and the transaction rolls back — the caller
        # gets the SAME 403 the fast pre-check above would have given had
        # the tightened policy been in effect from the start, never a
        # write under a policy already stale by the time it lands.
        def _is_still_authorized(c):
            fresh_thing = db.get_thing(c, thing_id)
            if fresh_thing is None:
                return False  # thing vanished mid-flight — fail closed
            return _update_thing_authorized(c, things_dir, fresh_thing, agent)

        # umwelt#300 S0(d), Mira's colleague-review blocker on 4e2e12c: the
        # source-content write is deferred to HERE, inside the guarded
        # write callback — it only runs once `_is_still_authorized` has
        # freshly re-confirmed the caller under the atomic lock, so a
        # concurrent policy tightening can no longer let attacker-supplied
        # content land on disk behind an eventual 403 (a filesystem write
        # is outside SQLite and cannot be rolled back the way `db_patch`'s
        # column update can). "source" in patch was validated (shape,
        # blueprint/kinddef parse) above; only the actual write moves.
        def _do_write(c):
            source_changed = False
            if "source" in patch:
                # umwelt#1014: compare BEFORE writing (R3 — a byte-identical
                # resubmit stamps nothing), flag only AFTER _replace_source
                # returns (a failed write raises past this and stamps nothing).
                source_changed = _source_differs(
                    things_dir, thing_id, patch["source"])
                _replace_source(things_dir, thing_id, patch["source"])
            db.update_thing_apply(c, thing_id, db_patch, agent,
                                  source_changed=source_changed)

        try:
            authorized = db.atomic_authorized_write(g.conn, _is_still_authorized, _do_write)
        except (ValueError, OSError) as e:
            # Same error shape update_thing always returned for a bad
            # source write — unchanged by moving the call site.
            return jsonify({"error": f"source write failed: {e}"}), 400
        if not authorized:
            return jsonify({"error": "not authorized to update this thing"}), 403
        return jsonify({"thing_id": thing_id, "updated": sorted(patch.keys())})

    # -- update_place (owner-only edit — places' first generic write route) -
    # Places previously had NO generic "set arbitrary field" route — only
    # create_place/set_parent/add_link/remove_link, each narrowly scoped to
    # its own field. This is a genuinely NEW write surface (place-lane-tiles
    # spec §2/§6): the mechanism ships here, but no real place's metadata is
    # touched by this PR — populating an actual place's lane_tiles is
    # separate world-content/deploy-lane work via this route once merged.
    @app.route("/umwelt/update_place", methods=["POST"])
    def update_place() -> Response:
        """Patch a place's mutable fields — today, ONLY `metadata`
        (`_MUTABLE_PLACE_KEYS`), same allowlist-and-reject posture as
        update_thing. No path to rename/re-describe/re-parent a place
        through this route — label/description/kind/parent_id/owner/id are
        all immutable here (create_place/set_parent own their own narrow
        write paths for the fields they touch).

        Body: {agent, place, patch: {metadata?}}

        Authorization (the new decision this route introduces): a place's
        `metadata` is writable when EITHER `place.owner is None`
        (engine-genesis places — the Square, Workshop, and similar founding
        places have no single agent-author, so any agent may curate their
        presentation — this team's ruling that "PLACES are engine-genesis
        ... dressing them is legitimately deploy-lane work") OR
        `agent == place.owner` (an agent-created private place, writable
        only by its own creator — same author-only posture as
        update_thing). Trust-scoped the same way every other verb in this
        file is: `agent` is caller-declared, not engine-attested (umwelt
        #24) — this enforces owner-only among cooperating our_side agents,
        not against a forged agent string.

        400 agent/place missing/blank · patch not an object · patch.metadata
            present but not an object · patch empty (nothing to update)
        403 place has an owner and the caller isn't it
        404 no such place
        422 patch contains a field outside `_MUTABLE_PLACE_KEYS`

        `metadata`'s substructure (e.g. `lane_tiles`) is NOT deep-validated
        here — a malformed render_spec degrades to "no tile rendered" at
        the render layer (gateway.py's `_enrich_place_with_tiles`), never a
        422 on this route. Same soft-validation posture create_thing/
        update_thing already use for `render_spec` — this route only
        enforces the shape it can universally guarantee (a dict).
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        place_id = _str_identity(body, "place")
        patch = body.get("patch") or {}
        if not agent_raw or not place_id:
            return jsonify({"error": "agent and place are required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        if not isinstance(patch, dict):
            return jsonify({"error": "patch must be an object"}), 400

        place = db.get_place(g.conn, place_id)
        if place is None:
            return jsonify({"error": f"place {place_id!r} not found"}), 404

        # Authorization fires before the allowlist/shape checks below, same
        # ordering as update_thing's author-check-before-field-checks —
        # "who's asking" is answered before "what are they asking for".
        if place["owner"] is not None and place["owner"] != agent:
            return jsonify({
                "error": "only the owner may update this place"
            }), 403

        extra = set(patch) - set(_MUTABLE_PLACE_KEYS)
        if extra:
            return jsonify({
                "error": f"these fields are not updatable: {sorted(extra)} "
                         f"(allowed: {list(_MUTABLE_PLACE_KEYS)})"}), 422
        if not patch:
            return jsonify({"error": "patch is empty — nothing to update"}), 400

        if "metadata" in patch and not isinstance(patch["metadata"], dict):
            return jsonify({"error": "patch.metadata must be an object"}), 400

        db.update_place(g.conn, place_id, patch, agent)
        return jsonify({"place_id": place_id, "updated": sorted(patch.keys())})

    # -- checklist (author-only check-item list mutation, umwelt #182 — the
    #    bed substrate) ------------------------------------------------------
    @app.route("/umwelt/checklist", methods=["POST"])
    def checklist() -> Response:
        """Mutate a checklist thing's items — author-only, a cousin of
        update_thing/add_link, not the cross-agent `act` machinery (umwelt
        #182). A thing "is a checklist" iff its `metadata.checklist` is a
        list; this route never auto-creates one — a bed blueprint (world
        content, forged separately) seeds it.

        Body: {agent, thing, op, item?, text?}. `item` is 1-based, matching
        render order (see _render_checklist). Ops (closed set): check |
        uncheck | reset | add | remove | edit.
          - check/uncheck: requires `item`.
          - reset: clears `done` on every item — no item/text needed (the
            morning reset); any item/text supplied is ignored, not
            rejected.
          - add: requires `text` (non-empty after strip, <=
            _CHECKLIST_ITEM_MAX_LEN chars); appends {text, done: false}.
            422 when the checklist is already at _CHECKLIST_MAX_ITEMS.
          - remove: requires `item`; deletes that item.
          - edit: requires `item` and `text` (same bound as add); replaces
            that item's `.text`.

        400 missing agent/thing/op ·
        422 unknown op (not in the closed set above) ·
        404 no avatar for agent (enter first) · thing not found ·
        409 thing is not live ·
        403 agent != the thing's canonical author (author-only, same trust
            model as update_thing) ·
        422 thing has no metadata.checklist list ("not a checklist —
            nothing to check off") ·
        422 item missing/not-an-integer/out-of-range (names the valid
            1..len(checklist) range) · item given but the checklist is
            currently empty ("this checklist is empty — add an item first",
            rather than the confusing "valid range: 1-0") ·
        422 text missing/empty (add, edit) · text exceeds
            _CHECKLIST_ITEM_MAX_LEN · checklist already at
            _CHECKLIST_MAX_ITEMS (add only).

        Persists via db.update_thing(metadata=...) — the same author-only
        metadata-write path update_thing's own route uses.

        Response 200: {thing_id, op, done_count, total, checklist: <new
        list>, render: <string, _render_checklist's output>, headline:
        <string, _render_checklist_headline's output — umwelt#867, comms
        QoL D10: a bounded one-item summary, so a caller who wants only
        confirmation isn't paying to have the whole list echoed back>}.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        thing_id = _str_identity(body, "thing")
        op = _str_identity(body, "op")
        if not agent_raw or not thing_id or not op:
            return jsonify({"error": "agent, thing, and op are required"}), 400
        if op not in _CHECKLIST_OPS:
            return jsonify({
                "error": f"unknown op {op!r} — must be one of "
                         f"{sorted(_CHECKLIST_OPS)}"
            }), 422
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        if thing["status"] != "live":
            return jsonify({
                "error": f"thing is {thing['status']} — cannot modify its "
                         f"checklist"
            }), 409

        author = _canonical_agent(thing["author"])
        if agent != author:
            return jsonify({
                "error": "only the author may modify this thing's checklist"
            }), 403

        items = thing["metadata"].get("checklist")
        if not isinstance(items, list):
            return jsonify({
                "error": "thing has no metadata.checklist list — not a "
                         "checklist, nothing to check off"
            }), 422
        items = list(items)  # work on a copy; only written back on success

        item_raw = body.get("item")
        text_raw = body.get("text")
        text = str(text_raw).strip() if isinstance(text_raw, str) else ""

        idx = None
        if op in ("check", "uncheck", "remove", "edit"):
            if item_raw is None:
                return jsonify({
                    "error": f"item is required for op {op!r}"
                }), 422
            if isinstance(item_raw, bool) or not isinstance(item_raw, int):
                return jsonify({"error": "item must be an integer"}), 422
            if not items:
                # len(items) == 0 — "valid range: 1-0" reads as a confusing
                # empty range on a fresh checklist; name the actual problem
                # instead (an agent hits this on a just-created bed).
                return jsonify({
                    "error": "this checklist is empty — add an item first"
                }), 422
            if not (1 <= item_raw <= len(items)):
                return jsonify({
                    "error": f"item {item_raw} is out of range — this "
                             f"checklist has {len(items)} item"
                             f"{'s' if len(items) != 1 else ''} (valid "
                             f"range: 1-{len(items)})"
                }), 422
            idx = item_raw - 1

        if op in ("add", "edit"):
            if not text:
                return jsonify({
                    "error": "text is required and must be a non-empty "
                             "string"
                }), 422
            if len(text) > _CHECKLIST_ITEM_MAX_LEN:
                return jsonify({
                    "error": f"text exceeds the {_CHECKLIST_ITEM_MAX_LEN}-"
                             f"char limit (got {len(text)})"
                }), 422

        # umwelt #182 round 4: ONE hoisted defense-in-depth guard, ahead of
        # the op dispatch below, covering every op that spreads an EXISTING
        # item (`{**item, ...}`) rather than a per-op isinstance check —
        # round 2's version only covered check/uncheck/edit and missed
        # reset (`[{**it, ...} for it in items]`), the same crash shape on
        # a non-dict item. check/uncheck/edit spread items[idx] only;
        # reset spreads EVERY item; add/remove touch no existing item's
        # shape, so they're excluded. Unreachable today —
        # _validate_checklist_metadata (create_thing/update_thing time) is
        # the real fix and closes the only forge/patch door — but a single
        # hoisted check means no future op can silently omit it.
        if op in ("check", "uncheck", "edit"):
            spread_targets = [(item_raw, items[idx])]
        elif op == "reset":
            spread_targets = [(i + 1, it) for i, it in enumerate(items)]
        else:
            spread_targets = []
        for bad_item, candidate in spread_targets:
            if not isinstance(candidate, dict):
                return jsonify({
                    "error": f"checklist item {bad_item} is malformed (not "
                             f"an object) — cannot {op} it"
                }), 422

        if op == "check":
            items[idx] = {**items[idx], "done": True}
        elif op == "uncheck":
            items[idx] = {**items[idx], "done": False}
        elif op == "reset":
            items = [{**it, "done": False} for it in items]
        elif op == "add":
            if len(items) >= _CHECKLIST_MAX_ITEMS:
                return jsonify({
                    "error": f"checklist is full ({_CHECKLIST_MAX_ITEMS} "
                             f"items max)"
                }), 422
            items.append({"text": text, "done": False})
        elif op == "remove":
            # umwelt#867: the removed item's own text is captured BEFORE
            # the delete below, since _render_checklist_headline needs it
            # for the "removed:" line and it is no longer reachable from
            # `items` (or the post-mutation `thing`) once deleted. Sibling
            # op-keyed site: the `headline_index` if/elif/else below also
            # branches per-op for the headline — a new op touching either
            # concern should check both sites.
            removed_item = items[idx]
            removed_text = (removed_item.get("text", "")
                             if isinstance(removed_item, dict) else "")
            del items[idx]
        elif op == "edit":
            items[idx] = {**items[idx], "text": text}

        new_metadata = dict(thing["metadata"])
        new_metadata["checklist"] = items
        db.update_thing(g.conn, thing_id, {"metadata": new_metadata}, agent)

        done_count = sum(1 for it in items if it.get("done"))
        new_thing = {**thing, "metadata": new_metadata}
        render = _render_checklist(new_thing)
        # umwelt#867: item_index for the headline — `add`'s touched item is
        # the new last index (item_raw is unset for add); `reset` needs no
        # index at all; every other op already has item_raw as the 1-based
        # index it acted on (remove's included — it's the index the removed
        # item HELD, used only for numbering). Sibling op-keyed site: the
        # `elif op == "remove":` branch above also captures `removed_text`
        # for the headline — a new op touching either concern should check
        # both sites.
        if op == "add":
            headline_index = len(items)
        elif op == "reset":
            headline_index = None
        else:
            headline_index = item_raw
        headline = _render_checklist_headline(
            new_thing, op, headline_index,
            removed_text=removed_text if op == "remove" else None)
        return jsonify({
            "thing_id": thing_id, "op": op, "done_count": done_count,
            "total": len(items), "checklist": items, "render": render,
            "headline": headline,
        }), 200

    # -- act (typed-things generic authorized state-transition verb,
    #    umwelt #107 slice A) ------------------------------------------------
    @app.route("/umwelt/act", methods=["POST"])
    def act() -> Response:
        """Execute a kind-declared action on a typed thing — the generic,
        authorized state-transition verb (umwelt #107). No sandbox, no
        arbitrary code: actions are declarative rows in a kinddef's action
        table (resolved across the target's kind chain, child-first
        override — the nearest definition of an action name wins), and this
        route is the ONLY place they're applied. An action transitions
        `metadata.status`, appends to the server-owned `metadata.action_log`,
        and (slice B) may run a declared, closed-menu `effects` list —
        `move_to` / `notify_author` / `project_task_set` / `agent_state_set`
        / `project_task_create` / `project_task_link` / `project_set` /
        `project_create` (umwelt #227 S4 — the project-board's structure-
        editing surface) / `grant_caller` / `revoke_caller` / `board_entry_add`
        / `board_entry_retire` (umwelt #400 — the `granted` caller class's
        write-path, and the charge-board's own structure-editing surface)
        — after that
        transition+log commit. No agent-authored code ever runs; effects
        are the same declarative-row posture as the caller/from/to gates,
        not a sandbox escape hatch.

        Body: {agent, thing, action, args?}  (args an object, default {})
        Response 200: {thing_id, action, by, status, log_entry}
        (log_entry, and the response itself, gain an `effect_failures` LIST
        key ONLY when at least one declared effect failed — see "Effects"
        below.)

        400 missing agent/thing/action, or args is present but not an object ·
        404 no avatar for agent — enter the world first (same pattern as
            take/put) ·
        404 thing not found ·
        409 thing is not live ·
        422 thing has no metadata.kind_id (not a typed thing — nothing to
            act on; a kinddef thing itself gets a more specific variant of
            this error naming the mistake — see umwelt #249 dogfood note) ·
        422 action not found anywhere in the thing's kind chain (walked
            child-first via _resolve_kind_chain) ·
        403 caller not authorized for this action, per its declared
            `callers` — resolved against the closed set `author` (the
            target's author) | `place_owner` (owner of the place the target
            currently sits in; may be null for public places, in which case
            place_owner grants nobody) | `anyone` | `granted` (umwelt #400
            — a live `thing_grants` row for (thing, agent); see
            `db.has_grant` and the `grant_caller`/`revoke_caller` effects
            below, the ONLY write path to that table) | `named:<agent>`
            (exact canonical agent match). Every comparison folds through
            `_canonical_agent` (umwelt #73), so casing never splits identity
            on either side ·
        422 a required arg (per the action's `requires`) is missing or not a
            non-empty string ·
        409 the thing's current `metadata.status` is not in the action's
            declared `from` list (names the current status + the allowed
            ones; an empty/absent `from` means any current status,
            including no status yet, is allowed) ·
        `metadata.action_log` already holding `_ACTION_LOG_MAX_ENTRIES`
            (200) entries is no longer a 422 for an action that doesn't
            itself declare `action_log_rotate` (umwelt #836): the route
            auto-rotates (archives `action_log` via the SAME mechanism
            `_apply_effect_action_log_rotate` uses for a manually-declared
            `rotate-log` action, resetting it to a single marker) and THEN
            proceeds to record the triggering act normally — no operator
            step needed. 422 `action log full and auto-rotation failed
            (<reason>) — archive or retire this thing` on the (practically
            unreachable — see that function's own id-collision handling)
            case where the auto-rotation itself fails.

        On success: `metadata.status` is set to the action's `to` (when
        non-null; a null `to` is a pure log-append action, e.g. `comment` —
        status is left unchanged either way, reported back either way).
        Appends a SERVER-BUILT entry to `metadata.action_log` —
        {action, by: <canonical agent>, at: db.now_iso(), args: <the args
        the caller supplied, passed through unmodified>} — metadata.action_log
        is server-owned end to end (create_thing/update_thing both reject a
        caller-supplied action_log, same structural-honesty posture as
        send_crossing's consequence_grade). Persisted through
        `db.update_thing` — the SAME metadata-write call update_thing's own
        route uses, invoked directly (not through that route's HTTP handler,
        since act()'s authorization is NOT author-only) so there is exactly
        one metadata-write code path, never a second one.

        Effects (umwelt #107 slice B): the action's declared `effects` list
        (validated at kinddef-creation time — see _validate_kinddef_source)
        runs in declared order AFTER the transition+log commit above. Every
        effect is BEST-EFFORT — a failing effect never rolls back the
        action; the action's own success (transition + log landing) is
        unconditional. A failed effect is recorded as an entry
        `{type, index, reason}` (reason: "failed: <reason>") appended to an
        `effect_failures` LIST key added to BOTH the response and the
        persisted log entry (a second, targeted `db.update_thing` call
        after the effects run — the original transition+log write is
        untouched if every effect succeeds). List, not a type-keyed map,
        so two effects of the same type (nothing forbids declaring two
        `move_to` entries) record independently — a type-keyed map would
        let a later same-type effect's outcome silently overwrite an
        earlier same-type failure.
          - `move_to` — moves the target thing into `effect.container` via
            `_apply_effect_move_to` (`db.put_thing_in_container`, the SAME
            capacity-checked internal path put()'s route uses), but
            deliberately WITHOUT put()'s `_reachable` requirement — a
            kind-sanctioned move, NOT the same bound `send_home` relies on:
            `send_home` is self-bounded on both axes (actor must already
            hold/be colocated with the thing; destination is always the
            actor's OWN home place), while `move_to` has neither bound (any
            caller the kind permits, destination a kind-author-declared
            constant). The actual bound is the kind_id-is-author-opt-in
            trust model plus umwelt's trust-scoped-private-network posture (see
            `_apply_effect_move_to`'s docstring for the full statement).
            Failure reasons: container missing/not-live/not-a-container/
            over-capacity/raced.
          - `notify_author` — sends a crossing to the target thing's
            author's home Post (`_apply_effect_notify_author`; the SAME
            `place_post_<agent>` convention register/enter use, but this
            effect never auto-vivifies it — an author who never registered
            has no Post, and that is a best-effort failure, not a silent
            mint). Body is auto-built: "<actor> did <action> on
            '<thing name>'", plus ": <comment>" when `args.comment` is
            present. `effect.wake` (bool, default false) is a REQUEST
            evaluated by the SAME `_evaluate_wake` gate send_crossing's own
            route uses — this path is structurally incapable of stamping
            wake_granted itself (Borges's #107 security invariant).
          - `project_task_set` — sets one field (`effect.field`, restricted
            to `db._MUTABLE_TASK_COLS`) on the task named by
            `args[effect.task_arg]`, via `_apply_effect_project_task_set`
            (umwelt #161 S2 — the typed-thing project board's write-path).
            The value is either the acting agent, canonical
            (`effect.value_actor`) or `args[effect.value_arg]`
            (`effect.value_arg`) — exactly one of the two is declared,
            enforced at kinddef-creation time. PROJECT-SCOPE BOUND (Borges
            security must-fix — load-bearing): the effect refuses (failure,
            not a silent no-op) unless the target task's `project_id`
            matches `thing.metadata.project_id` — the board thing's OWN
            bound project. A board thing with no `metadata.project_id`
            (not yet bound to a project) always refuses. Failure reasons:
            task_arg/value_arg absent from args, invalid status value, task
            not found, board unbound, task in a different project.
            COURT-CHANGE WAKE (umwelt #221 S3 §3, load-bearing): when
            `effect.field == "turn_holder"` and the write actually changes
            it to a DIFFERENT agent, an addressed crossing lands in the
            project's own room (`get_project(task['project_id'])['place_id']`)
            for the new turn_holder — through the SAME consent+rate
            `_evaluate_wake` gate `notify_author`/send_crossing use, never
            an auto-gong. Rider 2 (self-claim suppression): a flip TO the
            acting agent itself (`claim`) emits no crossing at all. A
            successful write surfaces the written `{task_id, field, value}`
            back to the caller in this response's `effect_results` (umwelt
            #267 — the SAME `(reason, result)` side-channel shape
            `board_entry_add`/`board_entry_retire`/`board_entry_edit` use,
            below), so a caller never needs a follow-up `look_at` just to
            learn what it wrote.
          - `agent_state_set` — sets one key (`effect.key`, restricted at
            kinddef-creation time to the kind's own declared top-level
            `state_keys` array) on the ACTING agent's thing_agent_state row
            for this thing, via `_apply_effect_agent_state_set` (umwelt
            #191 — the generic, engine-verified per-(thing, agent) KV
            store). The value is either the acting agent, canonical
            (`effect.value_actor`) or `args[effect.value_arg]`
            (`effect.value_arg`) — exactly one of the two is declared,
            enforced at kinddef-creation time, same shape as
            `project_task_set`. CALLER-ROW-ONLY (load-bearing, do not
            omit): the row written is always keyed by act()'s own
            server-derived `agent` — nothing in the effect schema or
            `args` can name a different agent's row, so a cross-agent
            write is structurally impossible, not merely rejected.
            ROW-BOUND: a genuinely new key is refused once the acting
            agent already holds `_AGENT_STATE_MAX_KEYS_PER_THING` (30)
            distinct keys on this thing — re-setting an already-held key
            is always an upsert and never counts against the cap. Failure
            reasons: value_arg absent from args, cap reached.
          - `project_task_create` / `project_task_link` / `project_set` /
            `project_create` (umwelt #227 S4 — see
            `_apply_effect_project_task_create` /
            `_apply_effect_project_task_link` / `_apply_effect_project_set`
            / `_apply_effect_project_create` for the full contract of
            each) round out the project-board's structure-editing surface
            alongside `project_task_set`: creating tasks/sub-tasks, linking
            or unlinking a `task_deps` edge, mutating the bound project's
            own label/description/status, and founding a brand-new project
            via the `project_registrar` singleton, respectively. The first
            three are board->project fail-close bound exactly like
            `project_task_set` (refuse until the acting board thing is
            bound to a project); `project_create` is the one deliberately
            UNBOUND member since it only ever mints new rows.
            `project_task_create`'s successful writes ALSO surface the new
            task's id back to the caller in `effect_results` (umwelt #267),
            the same side-channel `project_task_set` uses above.
          - `grant_caller` / `revoke_caller` (umwelt #400 — see
            `_apply_effect_grant_caller` / `_apply_effect_revoke_caller`
            for the full contract) — the ONLY write path to `thing_grants`,
            the table `granted`-class authorization reads (`db.has_grant`,
            above). `grant_caller`'s `effect.agent_arg` names the `args`
            key holding the target agent (caller-suppliable — the whole
            point is naming WHO at call time). `effect.can_grant_arg`
            (umwelt #400 round 3, Borges security fix), if the kinddef
            declares it, names the `args` key the GRANTER supplies a bool
            in to choose, per call, whether the grant confers delegation
            rights onward — defaults to False whenever the kinddef omits
            `can_grant_arg`, the caller omits the named arg, or the
            supplied value isn't a literal JSON `true`, so a granter who
            doesn't think about it never silently deputizes someone.
            SECURITY-CRITICAL who-may-grant gate (do not omit): the
            ACTING agent must be either the thing's author (structural, no
            row needed — mirrors how the `author` caller-class itself
            needs no grant row) or already hold a grant row on this thing
            with `can_grant=1` (`db.agent_can_grant`) — anyone else's
            attempt is a clean effect failure, never a silent no-op or a
            write. `revoke_caller` shares the same gate and `agent_arg`
            shape; it deletes the row and is idempotent (revoking a
            non-existent grant succeeds as a no-op, same posture
            `project_task_link`'s `remove: true` uses for a non-existent
            `task_deps` edge).
          - `board_entry_add` / `board_entry_retire` (umwelt #400 — see
            `_apply_effect_board_entry_add` / `_apply_effect_board_entry_retire`
            for the full contract) — the charge-board's own
            structure-editing surface: a bounded array living in the
            acting thing's OWN `metadata.entries`, the same
            server-owned-array-with-a-cap posture `metadata.action_log`
            already uses (`_ACTION_LOG_MAX_ENTRIES`), rather than a
            reuse of the `project_task_*` family — a board entry has no
            per-agent assignment, owner, or review-state lifecycle.
            `board_entry_add`'s `effect.text_arg` names the `args` key
            holding the entry text (length-capped at
            `_BOARD_ENTRY_TEXT_MAX_LEN`; the board itself is capped at
            `_BOARD_ENTRIES_MAX_COUNT` ACTIVE entries — retired entries
            don't count against the cap). Each entry
            is `{id, text, created_by, created_at, retired_at}`.
            `board_entry_retire`'s `effect.entry_id_arg` names the `args`
            key holding the target entry's `id`; retiring is SOFT (sets
            `retired_at`, never deletes the entry) — "aged out by date" is
            a read-side concern, not a prune step here. Fails (effect
            failure, not a silent no-op) when the named entry id doesn't
            exist on this board at all. `board_entry_add`/
            `board_entry_retire`/`board_entry_edit` (below) all surface
            the acted-on entry's `id` back to the caller in this
            response's `effect_results` (umwelt #836/#267 — see
            `_apply_effect_board_entry_add`'s docstring for the
            `(reason, entry_id)` return shape), so a caller never needs a
            follow-up `look_at` just to learn what it wrote. `project_task_
            set`/`project_task_create` (above) use this SAME `effect_results`
            side-channel for their own successful writes — see each one's
            own bullet above for its result shape.
          - `board_entry_edit` (umwelt #836 — see
            `_apply_effect_board_entry_edit` for the full contract) — the
            charge-board's correction verb: edits ONE entry's `text` in
            place (keeping its `id`/`created_by`/`created_at`), rather
            than the retire+add workaround that mints a new id and loses
            the original entry's provenance. `effect.entry_id_arg`/
            `effect.text_arg` name the `args` keys holding the target
            entry's `id` and its NEW text (same `_BOARD_ENTRY_TEXT_MAX_LEN`
            cap as `board_entry_add`). Only an ACTIVE (non-retired) entry
            may be edited — an unknown id, or one naming an already-retired
            entry, is a clean effect failure, never a silent no-op.
            Stamps `edited_at`/`edited_by` onto the entry; the render side
            (`charge_board_render`) reads neither field.
          - **Auto-rotation at the cap guard (umwelt #836):** when an
            action that does NOT declare `action_log_rotate` itself would
            push `metadata.action_log` past `_ACTION_LOG_MAX_ENTRIES`, the
            SERVER performs the same archiving
            `_apply_effect_action_log_rotate` does for a manually-declared
            `rotate-log` action — called directly from this route's own
            cap-guard code, never from a declared effect or a kinddef
            flag, so the #736 self-grant-bypass exemption (still derived
            solely from the action's own declared `effects`) is untouched
            — and THEN proceeds to record the triggering act normally.
            No 422, no separate operator step. See the guard's own comment
            below for the full ordering rationale.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        thing_id = _str_identity(body, "thing")
        action_name = _str_identity(body, "action")
        args = body.get("args", {})
        if args is None:
            args = {}
        if not agent_raw or not thing_id or not action_name:
            return jsonify({
                "error": "agent, thing, and action are required"
            }), 400
        if not isinstance(args, dict):
            return jsonify({"error": "args must be an object"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        # umwelt#982: the thing read gate -- BEFORE authorization and before
        # any write. An action with `callers: anyone` (e.g. `comment`) must
        # not let a non-enterer write into a private-place thing: you cannot
        # act on what you cannot see.
        refusal = _thing_view_refusal(g.conn, agent, thing, thing_id)
        if refusal is not None:
            return refusal
        if thing["status"] != "live":
            return jsonify({
                "error": f"thing is {thing['status']} — cannot act"
            }), 409

        kind_id = thing["metadata"].get("kind_id")
        if not kind_id:
            if thing["kind"] == "kinddef":
                # umwelt #249 dogfood friction (Ariadne, forum #268/#4753):
                # a kind's own kinddef thing and its actable instance(s) can
                # share a name (e.g. "project_registrar" the kind vs
                # "project_registrar" the singleton instance), and id-match
                # wins on name resolution — so a caller who targets the
                # KIND by name/id lands here with no clue that's what
                # happened. Error-text only (Kepler's ruling): this does
                # NOT change name resolution or prefer an instance — it
                # just names the actual mistake instead of the generic
                # "no kind_id" message every other untyped thing (a plain
                # readable/executable/container with no kind_id at all,
                # the much more common case) still gets unchanged below.
                return jsonify({
                    "error": (
                        f"{thing_id!r} is a kind DEFINITION (kind='kinddef') "
                        "— not an actable instance. Act on a thing whose "
                        f"metadata.kind_id == {thing_id!r} instead of on "
                        "the kind definition itself."
                    )
                }), 422
            return jsonify({
                "error": "thing has no metadata.kind_id — nothing to act on"
            }), 422

        try:
            chain = _resolve_kind_chain(g.conn, things_dir, str(kind_id))
        except _ThingSpecError as e:
            return jsonify({"error": e.message}), e.status

        action_def = None
        for _, parsed in chain:
            candidate = parsed.get("actions", {}).get(action_name)
            if candidate is not None:
                action_def = candidate
                break
        if action_def is None:
            return jsonify({
                "error": f"action {action_name!r} not found in kind chain "
                         f"for {kind_id!r}"
            }), 422

        # -- authorization ---------------------------------------------------
        # Delegates to _action_authorized (umwelt #522) — the SAME check
        # the read-only /umwelt/thing_actions route below uses to compute
        # its `permitted` flag, so the two can never drift on what
        # "authorized" means.
        if not _action_authorized(g.conn, action_def, agent, thing):
            return jsonify({
                "error": f"{agent_raw!r} is not authorized to call "
                         f"{action_name!r} on {thing_id!r}"
            }), 403

        # -- required args -----------------------------------------------------
        missing_args = [
            r for r in action_def.get("requires", [])
            if not isinstance(args.get(r), str) or not args.get(r).strip()
        ]
        if missing_args:
            return jsonify({
                "error": f"missing required arg(s): {missing_args}"
            }), 422

        # -- unconsumed args (umwelt #249) -------------------------------------
        # The engine's posture elsewhere is loud-reject-over-silent-drop
        # (closed-set validation on kinddef effects at #114, _MUTABLE_TASK_COLS
        # /_MUTABLE_PROJECT_COLS patch validation, /umwelt/update_place's
        # extra-key reject). act()'s caller-supplied `args` bag was the one
        # place that posture didn't reach: any key not named by `requires` and
        # not consumed by any declared effect (via its `*_arg` fields) was
        # silently accepted, logged verbatim to action_log, and never read
        # by anything — data confusion for the caller (a typo'd key lands in
        # the log with no effect, looking accepted). #114 closed the kinddef
        # AUTHOR's static effect-declaration shape; this closes the per-call
        # CALLER's runtime args shape against the same set.
        #
        # The union is complete for DECLARED consumption — by #114's
        # construction, _KIND_EFFECT_ALLOWED_KEYS admits only `type`, `*_arg`
        # references (whose value IS the args key name), or literal non-args
        # fields (container/wake/field/key/remove/value_actor); no effect can
        # reference an args key under a declared name that doesn't end in
        # `_arg`, and `value_actor` (ends `_actor`) is the ALTERNATIVE to
        # reading an args key (uses the acting agent), so it contributes
        # nothing here.
        #
        # notify_author's `comment_arg` is OPTIONAL with a back-compat default
        # of "comment" (umwelt #569): a declaration made before #569 declared
        # the key still consumes "comment", so the default is injected here
        # for the undeclared case. When comment_arg IS declared, the generic
        # `*_arg` walk below picks up its value the same as every other effect
        # — notify_author is no longer special-cased for CONSUMPTION (the
        # #249 carve-out read a HARDCODED key; #569 retired that by declaring
        # it). The one notify_author-aware line below only carries the
        # default, the same way an optional field with a default would.
        # Mirrors /umwelt/update_place's `extra = set(patch) - set(keys)` 422.
        consumable = set(action_def.get("requires", []))
        for effect in action_def.get("effects", []) or []:
            if effect.get("type") == "notify_author":
                # back-compat default for an UNDECLARED comment_arg — see
                # block comment above. A declared comment_arg is picked up
                # by the generic *_arg walk below and this is a no-op add of
                # the same value (harmless, keeps the two paths consistent).
                consumable.add(effect.get("comment_arg", "comment"))
            for k, v in effect.items():
                if k.endswith("_arg") and isinstance(v, str) and v:
                    consumable.add(v)
        extra = set(args) - consumable
        if extra:
            return jsonify({
                "error": f"unconsumed arg(s): {sorted(extra)} "
                         f"(this action consumes: {sorted(consumable)})"
            }), 422

        # -- from-status transition gate ---------------------------------------
        from_statuses = action_def.get("from") or []
        current_status = thing["metadata"].get("status")
        if not _action_from_ok(action_def, thing):
            return jsonify({
                "error": f"{thing_id!r} is at status {current_status!r} — "
                         f"action {action_name!r} requires one of "
                         f"{from_statuses}"
            }), 409

        # -- action_log cap (umwelt #107 slice B rider) ------------------------
        # umwelt #736: the exemption is derived from the action's own
        # DECLARED EFFECTS, never from a declarative flag on the action
        # def (a hypothetical `bypass_log_cap: true`). Borges's security
        # finding: a flag would be SELF-GRANTABLE — kinddefs are
        # agent-authored, so any agent forging a kinddef could set the
        # flag on every action of a kind they author, and the cap would
        # be unenforceable for that kind while doing nothing to earn the
        # exemption. Reading the effects list instead makes the
        # exemption CONSTITUTIVE: an agent cannot claim it without also
        # declaring `action_log_rotate`, which is already validated
        # closed at kinddef-creation time (_KIND_EFFECT_CLOSED_SET) and
        # actually performs the rotation (_apply_effect_action_log_rotate)
        # — there is no way to get the bypass "for free" on an action
        # that doesn't do the archiving. Do NOT "simplify" this back into
        # a flag on `action_def`; that reopens exactly the key-smuggling
        # surface umwelt #114 round 2 closed on effect rows, one level up.
        # `action_def` is already in scope here (used two lines below for
        # `.get("to")`), so no new plumbing is needed to read it.
        bypasses_cap = any(
            e.get("type") == "action_log_rotate"
            for e in (action_def.get("effects") or [])
        )
        existing_log = list(thing["metadata"].get("action_log") or [])
        if not bypasses_cap and len(existing_log) >= _ACTION_LOG_MAX_ENTRIES:
            # umwelt #836: auto-rotate here instead of 422'ing. This calls
            # the SAME archiving `_apply_effect_action_log_rotate` performs
            # for a manually-declared `rotate-log` action's own
            # `action_log_rotate` effect — directly from this guard's own
            # code, never from a declared effect or a kinddef flag, so the
            # #736 self-grant-bypass property above is untouched: this path
            # runs unconditionally, for every kind, regardless of what the
            # TRIGGERING action declares (an action that already declares
            # `action_log_rotate` itself is `bypasses_cap`-exempt and never
            # reaches this branch at all).
            #
            # Ordering note (umwelt #836, flagged in the issue as the
            # trickiest item): _apply_effect_action_log_rotate's own
            # docstring assumes it runs as a declared EFFECT, i.e. AFTER
            # act()'s pre-effects transition+log commit has already
            # appended "this call's own" log entry — so the log it archives
            # normally includes that entry. Here we call it BEFORE that
            # commit (existing_log is still the pre-triggering-action
            # snapshot, exactly _ACTION_LOG_MAX_ENTRIES entries — never ≤ 1,
            # so the function's "nothing to rotate" edge case can't fire in
            # this call site). That's fine: the function doesn't care
            # WHERE the entries it's archiving came from, only that
            # `existing_log` holds more than 1 entry, which it always does
            # here. What it does NOT know is that we're about to append a
            # new entry ourselves — its own db.update_thing call resets
            # `action_log` to a single marker, and we must build on THAT
            # (not on the stale `existing_log`/`thing` snapshot from
            # before the call), or the triggering action's own commit two
            # lines below would silently un-rotate it.
            rotate_reason = _apply_effect_action_log_rotate(
                g.conn, thing, agent, args, {"type": "action_log_rotate"})
            if rotate_reason is not None:
                return jsonify({
                    "error": "action log full and auto-rotation failed "
                             f"({rotate_reason}) — archive or retire this "
                             "thing"
                }), 422
            refreshed = db.get_thing(g.conn, thing_id)
            if refreshed is not None:
                thing = refreshed
            existing_log = list(thing["metadata"].get("action_log") or [])

        to_status = action_def.get("to")
        new_metadata = dict(thing["metadata"])
        if to_status is not None:
            new_metadata["status"] = to_status
        log_entry = {
            "action": action_name,
            "by": agent,
            "at": db.now_iso(),
            "args": args,
        }
        action_log = existing_log + [log_entry]
        new_metadata["action_log"] = action_log

        db.update_thing(g.conn, thing_id, {"metadata": new_metadata}, agent)

        # -- effects (umwelt #107 slice B) --------------------------------
        # Best-effort, declared order, run AFTER the transition+log commit
        # above — a failing effect never rolls the action back. Failures
        # are noted on both the response and the ALREADY-PERSISTED log
        # entry via one targeted follow-up db.update_thing call (only when
        # at least one effect failed OR at least one effect returned a
        # result — no extra write on the plain happy path).
        #
        # effect_failures is a LIST, not a type-keyed map (umwelt #114
        # round 2): _validate_kinddef_source doesn't forbid two effects of
        # the same type in one action's `effects` list (e.g. a try-then-
        # fallback pair of move_to), so keying by type would let a later
        # same-type success silently overwrite — or a later same-type
        # failure silently mask — an earlier same-type failure. Each
        # failure is its own {type, index, reason} entry, index being the
        # effect's position in the declared `effects` list, so two
        # same-type effects always record independently.
        #
        # effect_results (umwelt #663) is the SAME shape, for the success
        # side: most `_apply_effect_*` functions return `str | None`
        # (failure reason, or success) with no side-channel for a
        # SUCCESSFUL effect to hand data back to the caller.
        # `source_append` is the first effect that needs one (the caller
        # wants to see the file's new length rather than infer it).
        # Borges's #663 ruling: a parallel list, symmetric with
        # effect_failures, rather than mutating the `effect` dict in
        # place (`effect["_result"] = ...` would mutate a row that came
        # out of the declared, request-scoped kinddef — safe only as long
        # as nothing ever caches a parsed kinddef across requests, which
        # is incidental today, not designed, and the #114-round-2 style
        # key-smuggling surface reopens the moment it changes). Same
        # {type, index, ...} per-entry shape as effect_failures for the
        # same reason: two same-type effects in one action must record
        # independently.
        effect_failures: list[dict] = []
        effect_results: list[dict] = []
        # umwelt #736: set True by a SUCCESSFUL action_log_rotate effect —
        # see that dispatch arm's own comment, and the follow-up re-write
        # block below (guarded on this flag) for why it's needed.
        action_log_rewritten_by_effect = False
        for i, effect in enumerate(action_def.get("effects", []) or []):
            etype = effect.get("type")
            if etype == "move_to":
                reason = _apply_effect_move_to(
                    g.conn, thing, str(effect.get("container", "")))
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
            elif etype == "notify_author":
                reason, _wake_granted = _apply_effect_notify_author(
                    g.conn, thing, agent, action_name, args,
                    bool(effect.get("wake", False)),
                    comment_key=effect.get("comment_arg", "comment"))
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
            elif etype == "project_task_set":
                reason, result = _apply_effect_project_task_set(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
                else:
                    effect_results.append({"type": etype, "index": i, **result})
            elif etype == "agent_state_set":
                reason = _apply_effect_agent_state_set(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
            elif etype == "project_task_create":
                reason, result = _apply_effect_project_task_create(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
                else:
                    effect_results.append({"type": etype, "index": i, **result})
            elif etype == "project_task_link":
                reason = _apply_effect_project_task_link(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
            elif etype == "project_set":
                reason = _apply_effect_project_set(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
            elif etype == "project_create":
                reason = _apply_effect_project_create(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
            elif etype == "grant_caller":
                reason = _apply_effect_grant_caller(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
            elif etype == "revoke_caller":
                reason = _apply_effect_revoke_caller(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
            elif etype == "board_entry_add":
                reason, entry_id = _apply_effect_board_entry_add(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
                else:
                    effect_results.append(
                        {"type": etype, "index": i, "entry_id": entry_id})
            elif etype == "board_entry_retire":
                reason, entry_id = _apply_effect_board_entry_retire(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
                else:
                    effect_results.append(
                        {"type": etype, "index": i, "entry_id": entry_id})
            elif etype == "board_entry_edit":
                reason, entry_id = _apply_effect_board_entry_edit(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
                else:
                    effect_results.append(
                        {"type": etype, "index": i, "entry_id": entry_id})
            elif etype == "action_log_rotate":
                reason = _apply_effect_action_log_rotate(
                    g.conn, thing, agent, args, effect)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
                else:
                    # umwelt #736: a successful rotation has ALREADY reset
                    # the live thing's metadata.action_log (to a single
                    # marker entry) via its own db.update_thing call, one
                    # line above. Remember that here so the follow-up
                    # re-write block below (triggered if a SIBLING effect
                    # in this same action's effects list fails) does not
                    # stomp that marker entry back to the pre-rotation
                    # `action_log` local variable — see that block's own
                    # comment for the full reasoning.
                    action_log_rewritten_by_effect = True
            elif etype == "source_append":
                reason, new_length = _apply_effect_source_append(
                    g.conn, thing, agent, args, effect, things_dir)
                if reason is not None:
                    effect_failures.append(
                        {"type": etype, "index": i, "reason": f"failed: {reason}"})
                else:
                    effect_results.append(
                        {"type": etype, "index": i, "new_length": new_length})

        if effect_failures or effect_results:
            if effect_failures:
                log_entry["effect_failures"] = effect_failures
            if effect_results:
                log_entry["effect_results"] = effect_results
            action_log[-1] = log_entry
            # umwelt #400: re-read the thing's CURRENT metadata rather than
            # reusing the pre-effects `new_metadata` snapshot captured
            # above. board_entry_add/board_entry_retire are the FIRST
            # effects to ever write into the acting thing's OWN metadata
            # (every other effect writes a different table entirely —
            # tasks/projects/thing_agent_state/thing_grants/a crossing) —
            # and they may have already committed an `entries` change via
            # their own separate db.update_thing call earlier in THIS
            # same effects loop. Reusing the stale pre-effects snapshot
            # here would silently ERASE that write, since db.update_thing
            # replaces the `metadata` column wholesale, never merges. For
            # every action that declares neither board_entry_* effect,
            # this re-read is a no-op (current metadata == new_metadata,
            # just re-read from db instead of held in memory) — the
            # transition+log commit above already landed status/
            # action_log, so the re-read reflects them either way.
            current_thing = db.get_thing(g.conn, thing_id)
            current_metadata = (dict(current_thing["metadata"])
                                 if current_thing is not None else new_metadata)
            # umwelt #736: action_log_rotate is the FIRST effect to ever
            # write into `metadata.action_log` ITSELF (every other effect
            # here writes `entries`, a different key, which is why
            # overwriting `action_log` unconditionally was always safe
            # before this effect existed). If a rotation already
            # succeeded above, `current_metadata["action_log"]` already
            # holds the correct post-rotation marker entry (committed by
            # `_apply_effect_action_log_rotate`'s own db.update_thing
            # call) — overwriting it with the pre-rotation `action_log`
            # local variable would silently UN-ROTATE the log, defeating
            # the entire primitive, if a SIBLING effect in this same
            # action's `effects` list happened to fail (the shipped
            # `rotate-log` charge_board action declares only ONE effect,
            # so this path is dormant for it today — but action_log_rotate
            # is in the general closed set, so a future kinddef combining
            # it with another effect could hit this). In that rare case
            # the sibling failure's `effect_failures` entry above is
            # still returned in THIS response, just not additionally
            # persisted onto the (already-rotated, already-correct)
            # action_log — a small loss of audit detail, strictly
            # preferable to corrupting the rotation it would otherwise
            # clobber. The SAME tradeoff applies on the success side: a
            # sibling effect that succeeds WITH a result (populating
            # `effect_results`, no `effect_failures`) hits this identical
            # branch — the result still reaches the CALLER via the
            # top-level `response["effect_results"]` below, it just never
            # gets persisted onto the durable `action_log` either,
            # because there is no live entry left to attach it to (the
            # rotation already replaced it with the marker). Same
            # reviewer-round finding, same reasoning, opposite trigger.
            # Also dormant today for the same single-effect reason.
            if not action_log_rewritten_by_effect:
                current_metadata["action_log"] = action_log
            db.update_thing(g.conn, thing_id, {"metadata": current_metadata}, agent)

        response = {
            "thing_id": thing_id,
            "action": action_name,
            "by": agent,
            "status": new_metadata.get("status"),
            "log_entry": log_entry,
        }
        if effect_failures:
            response["effect_failures"] = effect_failures
        if effect_results:
            response["effect_results"] = effect_results
        return jsonify(response)

    # -- thing_actions (read-only act() introspection, umwelt #522) ---------
    @app.route("/umwelt/thing_actions", methods=["GET"])
    def thing_actions() -> Response:
        """Read-only introspection for the web surface (umwelt #522, the
        door act() itself was missing on the web): every action declared
        anywhere in `thing`'s kind chain, each flagged `permitted` for
        `agent` — a FLAG, NOT A FILTER. The response always lists every
        declared action regardless of whether `agent` may call it.

        Why flag and not filter (Borges, #522 review thread): filtering
        buys no confidentiality — a caller can diff a filtered list against
        the thing's own public description and learn exactly which actions
        they're barred from anyway, the same information obtained by
        inference instead of by a field. What filtering actually costs is
        worse: a filtered list LOOKS like an access control and isn't one
        — the real control stays in act()'s 403 (via `_action_authorized`,
        shared with this route below), and a filtered list creates a
        SECOND place that appears to decide authorization, which is
        exactly the drift disease #515/#518 name (Kepler's ruling against
        it on #506/#511). Flag keeps one authorization home and makes the
        client's job honest: render everything, grey what you can't call,
        let the engine be the one that refuses. This also happens to let
        the client distinguish "this action doesn't exist" from "you can't
        do this," which is a nice side effect, not the load-bearing reason.
        This is the inbound half of the pattern #515
        §5.1 names, built as an instance per #515 §7.1's sequencing
        (Kepler's federation ruling already covers it: this reads off the
        kinddef thing, mints no registry, and adds no authority beyond
        what act() itself already enforces).

        Query params: thing=<id>, agent=<agent> (both required).

        400 thing or agent missing/empty ·
        404 no avatar for agent — enter the world first (mirrors act()'s
            own wording) ·
        404 thing not found (mirrors act()'s own wording).

        Untyped things — no metadata.kind_id at all, INCLUDING a kinddef
        thing itself — return a clean `{"actions": []}`, never an error.
        act() itself 422s on this same condition because it's trying to DO
        something; this route is pure introspection, and an empty list is
        the honest answer to "what can be done here" when the answer is
        "nothing typed" (umwelt #522 resolved-decisions §1 — deliberately
        NOT the same error posture as act()'s 422/kinddef-specific 422).

        A malformed or cyclic kind chain propagates whatever status
        `_resolve_kind_chain`'s own `_ThingSpecError` carries — the SAME
        error act() itself would raise resolving the identical chain, so
        the two routes never disagree about what "broken chain" means.

        Response 200: {"actions": [{"verb", "requires", "permitted",
        "available"[, "reason"]}, ...]}. `reason` (umwelt #975) is present
        ONLY on an action whose `available` is false -- the server-built
        words ("not available while <status>") a client shows as-is. `permitted` = may THIS agent call it (who);
        `available` = does the thing's CURRENT kind-state allow it (when:
        `from` missing/empty, or the status is listed -- `_action_from_ok`,
        the same helper act()'s 409 transition gate uses; umwelt #967). Both
        are flags, not filters: a client disables on either being false.
        Every action name across the WHOLE chain is collected (not just the
        first match) — nearer-in-chain (child) definitions override
        same-named parent ones, same override semantics
        `_resolve_kind_chain`'s own docstring describes. Order is
        declaration order as encountered walking the chain CHILD-FIRST: a
        name's position is set by where it's FIRST seen (its winning,
        nearest definition), so a parent-only action still appears, in the
        position the child-first walk reaches it — never re-sorted. Each
        action's `permitted` is computed by `_action_authorized` — the
        SAME function act() itself calls for its own 403 gate (umwelt
        #522) — so this list can never advertise a permission act() would
        then go on to refuse.

        umwelt#621 slice 1c: that last claim ("can never advertise a
        permission act() would then go on to refuse") used to be false in
        a subtle way once act() itself became resolve_actor-converted
        (slice 1b): a caller could present a valid token for A while
        declaring `agent=B` here, computing `permitted` for the SPOOFED
        identity B while a same-token act() call resolves to the real A —
        the two could disagree, and a declared identity also leaks another
        agent's permission structure on a thing (response personalization,
        Mira's #621 boundary) to anyone willing to name them. `agent` is
        now resolved through resolve_actor, same REQUIRED-agent shape as
        look/trace, so this route's own invariant claim is true again.
        """
        thing_id = (request.args.get("thing") or "").strip()
        agent_raw = (request.args.get("agent") or "").strip()
        if not thing_id or not agent_raw:
            return jsonify({"error": "thing and agent are required"}), 400
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        # umwelt#982: the thing read gate -- this route discloses kind, state
        # and the action list, so it is gated like look_at.
        refusal = _thing_view_refusal(g.conn, agent, thing, thing_id)
        if refusal is not None:
            return refusal

        kind_id = thing["metadata"].get("kind_id")
        if not kind_id:
            # Untyped, or the thing IS a kinddef itself — read-only
            # introspection has an honest empty answer here; unlike act()
            # this is not an error (see docstring above).
            return jsonify({"actions": []})

        try:
            chain = _resolve_kind_chain(g.conn, things_dir, str(kind_id))
        except _ThingSpecError as e:
            return jsonify({"error": e.message}), e.status

        actions: list[dict] = []
        for name, action_def in _effective_actions(chain):
            entry = {
                "verb": name,
                "requires": list(action_def.get("requires", [])),
                "permitted": _action_authorized(g.conn, action_def, agent, thing),
                "available": _action_from_ok(action_def, thing),
            }
            if not entry["available"]:
                # umwelt #975: the server's own words, so the scene menu and
                # the viewer show one string instead of each composing one.
                entry["reason"] = _unavailable_reason(thing)
            actions.append(entry)
        return jsonify({"actions": actions})

    # -- trash_thing / restore_thing / bin / landfill (removal lifecycle,
    #    umwelt #28) -------------------------------------------------------
    # Permanent destruction is never an agent's act (Lei/Ariadne, ob_0939) —
    # agents can only trash (recoverable soft-delete) and restore. Only the
    # world's periodic GC (gc.py) advances a binned thing toward landfilled
    # and eventually dissolved; dissolve is never exposed here or as an MCP
    # tool. trash_thing/restore_thing are author-only, SAME trust-scoped gate
    # as update_thing above: `agent` is caller-declared (no auth in v0), so
    # author-only enforcement holds among cooperating our_side agents but is
    # not impersonation-resistant until agent identity is engine-attested
    # (umwelt #24; the shared foundation dv_0230) — identical caveat wording
    # to update_thing's, kept consistent on purpose.
    @app.route("/umwelt/trash_thing", methods=["POST"])
    def trash_thing() -> Response:
        """Move a thing to the bin — a recoverable soft-delete. Author-only.

        Body: {agent, thing} -> {thing_id, status:'binned', binned_at}
        403 non-author · 404 missing thing · 409 already binned/landfilled ·
        422 the thing is a body (avatars are un-trashable — #28's cross-spec
        constraint with the avatar build; see db.is_body_kind) · 422 the
        thing is currently carried (umwelt #88 slice 1 — "put it down
        first"; keeps the #28 invariant that place_id never changes through
        lifecycle, since a carried thing's place_id is stale) · 422 the
        thing is currently bagged (umwelt #88 slice 2 — "take it out
        first", mirroring the carried rule) · 422 the thing is a non-empty
        container (umwelt #88 slice 2 — "empty it first").
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        thing_id = _str_identity(body, "thing")
        if not agent_raw or not thing_id:
            return jsonify({"error": "agent and thing are required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        # Category error, not an authorization question — fires before the
        # author check since it's true regardless of who's asking (same
        # ordering rationale as update_thing's is_body_kind guard above).
        if db.is_body_kind(thing["kind"]):
            return jsonify({
                "error": "avatars are un-trashable — a body cannot be binned"
            }), 422
        if thing["author"] != agent:
            return jsonify({"error": "only the author may trash this thing"}), 403
        if thing["status"] != "live":
            return jsonify({
                "error": f"thing is already {thing['status']} — cannot trash"
            }), 409
        # umwelt #88 slice 1: a carried thing must be dropped before it can
        # be trashed — simple v1 rule (no reach-into-inventory trash).
        if thing["holder_kind"] == "avatar":
            return jsonify({
                "error": "thing is being carried — put it down first"
            }), 422
        # umwelt #88 slice 2: mirrors the carried rule above for bags — a
        # thing sitting inside a container must be taken out before it can
        # be trashed (same "no reach-into-inventory trash" posture).
        if thing["holder_kind"] == "container":
            return jsonify({
                "error": "thing is being carried — take it out first"
            }), 422
        # umwelt #88 slice 2: a container carrying live contents can't be
        # trashed out from under them — empty it first (its contents would
        # otherwise become permanently unreachable: their holder_id points
        # at a binned/landfilled container no route can look_in anymore).
        if thing["kind"] == "container":
            contents = db.things_in_container(g.conn, thing_id)
            if contents:
                return jsonify({
                    "error": "container is not empty — empty it first"
                }), 422

        # Village Law D3: the author's own trash voids any OPEN bin notice
        # (else trash -> restore would leave an aged open notice that bins
        # the restored thing at once). Written uncommitted BEFORE
        # db.trash_thing so its commit covers notice, event and trash.
        try:
            open_notice = g.conn.execute(
                "SELECT id FROM bin_notices WHERE thing_id = ? "
                "AND status = 'open'", (thing_id,)).fetchone()
            if open_notice is not None:
                closed = g.conn.execute(
                    "UPDATE bin_notices SET status = 'answered' "
                    "WHERE id = ? AND status = 'open'", (open_notice[0],))
                # Only log if WE closed it (a maintainer may have claimed
                # it meanwhile; don't re-report that as 'answered').
                if closed.rowcount == 1:
                    g.conn.execute(
                        "INSERT INTO bin_notice_events(notice_id, thing_id, "
                        "actor, kind, at, detail) VALUES(?, ?, ?, "
                        "'answered', ?, ?)",
                        (open_notice[0], thing_id, agent, db.now_iso(),
                         f"author trashed the thing (auth: "
                         f"source={resolution.source} "
                         f"verified={resolution.verified})"))
            db.trash_thing(g.conn, thing_id)
        except Exception:
            g.conn.rollback()
            raise
        updated = db.get_thing(g.conn, thing_id)
        return jsonify({"thing_id": thing_id, "status": updated["status"],
                        "binned_at": updated["binned_at"]})

    @app.route("/umwelt/restore_thing", methods=["POST"])
    def restore_thing() -> Response:
        """Restore a thing from the bin OR the landfill back to live. Author-only.

        Body: {agent, thing} -> {thing_id, status:'live'}
        403 non-author · 404 missing thing · 409 already live. place_id is
        never changed through the lifecycle (#28 decision 2), so restoring
        from the landfill returns a thing to exactly where it always lived.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        thing_id = _str_identity(body, "thing")
        if not agent_raw or not thing_id:
            return jsonify({"error": "agent and thing are required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        if thing["author"] != agent:
            return jsonify({"error": "only the author may restore this thing"}), 403
        if thing["status"] == "live":
            return jsonify({"error": "thing is already live"}), 409

        db.restore_thing(g.conn, thing_id)
        return jsonify({"thing_id": thing_id, "status": "live"})

    @app.route("/umwelt/bin", methods=["POST"])
    def bin_() -> Response:
        """Your own trashed things (author-scoped READ).

        Body: {agent} -> {binned:[{id,name,description,kind,binned_at}]}

        umwelt#621 slice 1c: this route's own docstring used to claim "any
        caller-declared agent sees only its own binned things, there is no
        cross-agent case to 403 here" — true only if the caller cannot lie
        about which agent it is. It could: a bare declared `agent` returned
        THAT name's binned things regardless of who was actually asking,
        exactly the self-state-keyed-on-a-claimed-name disclosure Mira's
        #621 ruling covers for wake_blocks/unseen/my_court. `agent` is now
        resolved through resolve_actor instead of trusted outright, same
        REQUIRED-agent shape as look/trace.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        if not agent_raw:
            return jsonify({"error": "agent is required"}), 400
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        binned = [
            {"id": t["id"], "name": t["name"], "description": t["description"],
             "kind": t["kind"], "binned_at": t["binned_at"]}
            for t in db.list_binned(g.conn, agent)
        ]
        return jsonify({"binned": binned})

    # -- bin_notice / answer_bin_notice / maintainer_bin (Village Law D3
    #    trial, slice 1) -----------------------------------------------------
    # A maintainer may bin ANOTHER villager's thing only after a NOTICE that
    # names the thing, the reason and the restore path, and BIN_NOTICE_HOURS
    # pass UNANSWERED. Bin only, never the landfill; restore stays the
    # author's (restore_thing above, unchanged). The verb records the notice
    # and its time; the WORLD checks the hours -- noticed_at is engine-
    # stamped and no request field supplies time or hours. Maintainers are
    # the boot config's `bin_maintainers` list (fails CLOSED; the retire
    # `maintainers` list does not grant this verb).
    def _bin_field(body, key):
        """isinstance-first read (same defense as retire_identity): an
        explicit JSON null / non-string is None, not the text "None"."""
        v = body.get(key)
        if not isinstance(v, str) or not v.strip():
            return None
        return v.strip()

    def _bin_log_event(notice_id, thing_id, actor, kind, detail=None):
        g.conn.execute(
            "INSERT INTO bin_notice_events(notice_id, thing_id, actor, kind, "
            "at, detail) VALUES(?, ?, ?, ?, ?, ?)",
            (notice_id, thing_id, actor, kind, db.now_iso(), detail))

    def _parse_bin_ts(ts):
        return _bin_datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=_bin_timezone.utc)

    def _fmt_bin_ts(dt):
        return dt.astimezone(_bin_timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")

    def _open_bin_notice(thing_id):
        return g.conn.execute(
            "SELECT id, noticed_at FROM bin_notices WHERE thing_id = ? "
            "AND status = 'open' ORDER BY noticed_at DESC LIMIT 1",
            (thing_id,)).fetchone()

    @app.route("/umwelt/bin_notice", methods=["POST"])
    def bin_notice() -> Response:
        """Notify a thing's author that a maintainer will bin it after
        BIN_NOTICE_HOURS unanswered. bin_maintainers only.

        Body: {actor, thing, reason} ->
        201 {notice_id, thing_id, noticed_at, bin_after}
        400 missing/blank field or reason over 1000 chars · 401 auth, or
        "authentication required: this verb needs a verified session" ·
        403 actor not a bin maintainer ·
        404 unknown thing · 422 own thing / body / carried / bagged /
        non-empty container · 409 thing not live, or an OPEN notice exists.
        The thing is untouched. A crossing naming the thing, reason, restore
        path and the hours rule is sent to the author, landing in the
        THING's place (where the author can re-visit it), not their Post.
        """
        body = request.get_json(force=True, silent=True) or {}
        actor_raw = _bin_field(body, "actor")
        thing_id = _bin_field(body, "thing")
        reason = _bin_field(body, "reason")
        if not actor_raw or not thing_id or not reason:
            return jsonify({"error": "actor, thing and reason are required"}), 400
        if len(reason) > db.BIN_REASON_MAX:
            return jsonify({
                "error": f"reason is too long (max {db.BIN_REASON_MAX})"
            }), 400
        resolution = resolve_actor(g.conn, request, declared=actor_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        # Cross-authority verb: a VERIFIED session only. `source == "token"`
        # is not enough (an unverified /umwelt/login session resolves with
        # source="token", verified=False). Checked BEFORE membership so a
        # caller learns nothing about who the maintainers are.
        if not resolution.verified:
            return jsonify({
                "error": "authentication required: this verb needs a "
                         "verified session"}), 401
        actor = resolution.agent_id
        if actor not in app.config["RETIRE_CONFIG"]["bin_maintainers"]:
            return jsonify({
                "error": f"{actor} is not authorized to give bin notices"
            }), 403

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        if thing["author"] == actor:
            return jsonify({
                "error": "this is your own thing — use trash_thing"
            }), 422
        if db.is_body_kind(thing["kind"]):
            return jsonify({
                "error": "avatars are un-trashable — a body cannot be binned"
            }), 422
        if thing["status"] != "live":
            return jsonify({
                "error": f"thing is already {thing['status']} — cannot notice"
            }), 409
        if thing["holder_kind"] in ("avatar", "container"):
            return jsonify({
                "error": "thing is being carried or bagged — cannot be binned"
            }), 422
        if thing["kind"] == "container" and db.things_in_container(
                g.conn, thing_id):
            return jsonify({
                "error": "container is not empty — cannot be binned"
            }), 422
        if _open_bin_notice(thing_id) is not None:
            return jsonify({
                "error": "an open notice already exists for this thing"
            }), 409

        notice_id = str(uuid.uuid4())
        noticed_at = db.now_iso()
        bin_after = _fmt_bin_ts(
            _parse_bin_ts(noticed_at)
            + _bin_timedelta(hours=db.BIN_NOTICE_HOURS))
        author = thing["author"]
        restore_path = (
            f"POST /umwelt/restore_thing {{agent: {author}, "
            f"thing: {thing_id}}} (author-only)")
        crossing_body = (
            f"Bin notice from {actor}: your thing {thing['name']!r} "
            f"(id {thing_id}) will be binned after {db.BIN_NOTICE_HOURS} "
            f"hours if this notice goes unanswered (earliest: {bin_after}). "
            f"Reason: {reason}. To keep it, answer notice {notice_id} "
            f"(answer_bin_notice). If it is binned, only you can restore "
            f"it: {restore_path}. The bin is recoverable; nothing is "
            f"landfilled."
        )
        try:
            g.conn.execute(
                "INSERT INTO bin_notices(id, thing_id, author, maintainer, "
                "reason, restore_path, noticed_at, status) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 'open')",
                (notice_id, thing_id, author, actor, reason, restore_path,
                 noticed_at))
            _bin_log_event(notice_id, thing_id, actor, "notice_sent", reason)
            # build_crossing commits: notice row, event row and crossing
            # land in one commit, or (on any failure) none of them do.
            db.build_crossing(
                g.conn, id=str(uuid.uuid4()), sender=actor,
                destination=thing["place_id"], sent_at=noticed_at,
                received_at=noticed_at, crossing_latency_s=0.0,
                crossing_hops=0, body=crossing_body, to_agent=author,
                # A notice is deliberately not a gong: it must be heard on
                # the author's next look, not wake them.
                wake_granted=False,
            )
        except sqlite3.IntegrityError as exc:
            g.conn.rollback()
            # Only the one-open-notice index is a duplicate; any other
            # IntegrityError is a real fault and must not be mislabelled.
            if "bin_notices.thing_id" not in str(exc):
                raise
            return jsonify({
                "error": "an open notice already exists for this thing"
            }), 409
        except Exception:
            g.conn.rollback()
            raise
        return jsonify({"notice_id": notice_id, "thing_id": thing_id,
                        "noticed_at": noticed_at,
                        "bin_after": bin_after}), 201

    @app.route("/umwelt/answer_bin_notice", methods=["POST"])
    def answer_bin_notice() -> Response:
        """The thing's author answers an open bin notice, which blocks the
        bin. Author-only.

        Body: {agent, notice} -> 200 {notice_id, status:'answered'}
        400 missing · 401 auth · 404 unknown notice · 403 not the author ·
        409 notice not open.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _bin_field(body, "agent")
        notice_id = _bin_field(body, "notice")
        if not agent_raw or not notice_id:
            return jsonify({"error": "agent and notice are required"}), 400
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        row = g.conn.execute(
            "SELECT thing_id, author, status FROM bin_notices WHERE id = ?",
            (notice_id,)).fetchone()
        if row is None:
            return jsonify({"error": f"notice {notice_id!r} not found"}), 404
        thing_id, author, status = row[0], row[1], row[2]
        if author != agent:
            return jsonify({
                "error": "only the thing's author may answer this notice"
            }), 403
        if status != "open":
            return jsonify({"error": f"notice is already {status}"}), 409
        claimed = g.conn.execute(
            "UPDATE bin_notices SET status = 'answered' "
            "WHERE id = ? AND status = 'open'", (notice_id,))
        if claimed.rowcount != 1:
            # A bin (or another answer) committed after our status read.
            g.conn.rollback()
            return jsonify({"error": "notice is no longer open"}), 409
        _bin_log_event(
            notice_id, thing_id, agent, "answered",
            f"auth: source={resolution.source} "
            f"verified={resolution.verified}")
        g.conn.commit()
        return jsonify({"notice_id": notice_id, "status": "answered"})

    @app.route("/umwelt/maintainer_bin", methods=["POST"])
    def maintainer_bin() -> Response:
        """Bin another villager's thing after an open notice stood
        BIN_NOTICE_HOURS unanswered. bin_maintainers only. Bin only.

        Body: {actor, thing} -> 200 {thing_id, status:'binned', binned_at}
        400 missing · 401 auth, or "authentication required: this verb
        needs a verified session" · 403 not a bin maintainer · 404 unknown
        thing · 409 no open notice / too early / thing not live · 422
        db.trash_thing's own guards (carried, bagged, non-empty container).
        Every refusal after authorization is logged as a 'refused' event.
        """
        body = request.get_json(force=True, silent=True) or {}
        actor_raw = _bin_field(body, "actor")
        thing_id = _bin_field(body, "thing")
        if not actor_raw or not thing_id:
            return jsonify({"error": "actor and thing are required"}), 400
        resolution = resolve_actor(g.conn, request, declared=actor_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        # Cross-authority verb: a VERIFIED session only. `source == "token"`
        # is not enough (an unverified /umwelt/login session resolves with
        # source="token", verified=False). Checked BEFORE membership so a
        # caller learns nothing about who the maintainers are.
        if not resolution.verified:
            return jsonify({
                "error": "authentication required: this verb needs a "
                         "verified session"}), 401
        actor = resolution.agent_id
        if actor not in app.config["RETIRE_CONFIG"]["bin_maintainers"]:
            return jsonify({
                "error": f"{actor} is not authorized to bin others' things"
            }), 403
        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404

        def refuse(status, msg, notice_id=None):
            _bin_log_event(notice_id, thing_id, actor, "refused", msg)
            g.conn.commit()
            return jsonify({"error": msg}), status

        notice = _open_bin_notice(thing_id)
        notice_id = notice[0] if notice else None
        if thing["status"] != "live":
            return refuse(409, f"thing is already {thing['status']} — "
                               "cannot bin", notice_id)
        if notice is None:
            return refuse(409, "no open notice for this thing — give a "
                               "bin_notice first")
        ready_at = (_parse_bin_ts(notice[1])
                    + _bin_timedelta(hours=db.BIN_NOTICE_HOURS))
        now = _parse_bin_ts(db.now_iso())
        if now < ready_at:
            remaining = ready_at - now
            hrs, rem = divmod(int(remaining.total_seconds()), 3600)
            return refuse(409, f"too early: notice has not stood "
                               f"{db.BIN_NOTICE_HOURS}h unanswered; "
                               f"{hrs}h{rem // 60:02d}m remaining", notice_id)
        # Atomic + race-safe: claim the notice (only if still open), log the
        # event, then trash; db.trash_thing's commit covers all three. Any
        # failure before that commit rolls the whole lot back.
        try:
            claimed = g.conn.execute(
                "UPDATE bin_notices SET status = 'binned' "
                "WHERE id = ? AND status = 'open'", (notice_id,))
            if claimed.rowcount != 1:
                g.conn.rollback()
                return refuse(409, "notice is no longer open", notice_id)
            _bin_log_event(notice_id, thing_id, actor, "binned")
            db.trash_thing(g.conn, thing_id)
        except ValueError as exc:
            g.conn.rollback()
            return refuse(422, str(exc), notice_id)
        except Exception:
            g.conn.rollback()
            raise
        updated = db.get_thing(g.conn, thing_id)
        return jsonify({"thing_id": thing_id, "status": updated["status"],
                        "binned_at": updated["binned_at"]})

    @app.route("/umwelt/landfill", methods=["GET"])
    def landfill() -> Response:
        """The public dump — everyone's landfilled things, across every
        place they originally lived in.

        -> {landfilled:[{id,name,description,kind,author,landfilled_at}]}
        Also reachable via move_to place_landfill (see _place_view).
        """
        landfilled = [
            {"id": t["id"], "name": t["name"], "description": t["description"],
             "kind": t["kind"], "author": t["author"],
             "landfilled_at": t["landfilled_at"]}
            for t in db.list_landfilled(g.conn)
        ]
        return jsonify({"landfilled": landfilled})

    # -- retire_identity (ops-authority, config-gated — umwelt #303) --------
    # A body is deliberately trash-exempt (db.trash_thing rejects body-kinds
    # outright) — so a test/throwaway identity's avatar has no path to
    # retire via any existing agent verb. This route is the recoverable
    # escape hatch: NOT a general agent verb — both `actor` and `identity`
    # are checked against boot-time config (see _resolve_retire_config),
    # never caller-asserted, and the config fails CLOSED (missing/malformed
    # file -> both lists empty -> the route 403s everything).
    @app.route("/umwelt/retire_identity", methods=["POST"])
    def retire_identity() -> Response:
        """Retire a configured throwaway identity — bin its authored things,
        flag its avatar retired, and hide it from presence. Never a general
        agent verb: `actor` must be a configured maintainer AND `identity`
        must be a configured retirable identity (see _resolve_retire_config
        / the `retire_config_file` create_app param).

        Body: {actor, identity} ->
        {identity, actor, things_binned, avatar_retired, announced_in}

        403 `actor` not a configured maintainer · 403 `identity` not a
        configured retirable identity · 404 `identity` has no avatar (never
        entered the world).

        Effects (all recoverable — no dissolution, ever):
          (a) Every LIVE thing `identity` authored, EXCLUDING its own avatar
              body (db.live_non_avatar_things_by_author already excludes
              kind='avatar' — the avatar's own thing row has author==
              identity too, and must NOT be bin-attempted through this
              path), is binned via the EXISTING db.trash_thing pipeline —
              restore-able, GC-decays on the standard schedule, same as the
              trash_thing agent verb. A thing db.trash_thing itself refuses
              (currently carried/bagged, or a non-empty container) is
              skipped rather than aborting the whole retirement — it stays
              live and isn't counted in `things_binned`.
          (b) The avatar's OWN thing row gets `metadata.retired = true`
              (merge, not replace — see db.set_thing_metadata's docstring
              for why update_thing's own body-kind reject can't be reused
              here), AND — in the SAME commit, via
              db.mark_avatar_and_identity_retired (umwelt#561 security
              review) — `identities.retired_at` is stamped too, so the two
              can never drift on partial failure. `status` stays 'live' —
              there is no 'retired' value in the things.status CHECK
              constraint, and this route does not touch that constraint.
              metadata.retired remains the world/presentation signal;
              identities.retired_at is the AUTH-side one db.get_agent_
              session's chokepoint actually reads (see (f) below).
          (c) db.avatars_in_place filters out any metadata.retired avatar
              — the SINGLE "who's here" chokepoint every consumer (MUD,
              webrender's /scene via _build_scene, place_view) reads
              through, so this one filter is what makes the avatar actually
              disappear everywhere at once.
          (d) The identity's home/post place is NOT touched here. Every
              post is minted by `_ensure_home_place`/`db.upsert_place` as
              `kind='private'` at birth, and a place's `kind` is not in
              `_MUTABLE_PLACE_COLS` (db.update_place can only ever patch
              `metadata`) — so a post can never become anything but private
              after creation. Its INTERIOR (description, contents, entry)
              stays gated to the owner by `db._place_interior_visible_to`, unchanged.
              Corrected per umwelt#538 rev 3: this used to say the place was
              fully pruned from place_tree/map for every non-owner — that
              is no longer true. A retired identity's home post now shows
              its existence/label/owner like any other private place (rule
              2), same as a live identity's would. That is not a new gap
              this route needs to close: nothing about retirement changes
              what "private" discloses, so there is still nothing
              retirement-SPECIFIC to hide, and this route still deliberately
              does NOT invent a second `metadata.hidden` mechanism on top
              of an already-private place.
          (e) A broadcast (no `to_agent`) audit crossing — body
              "{identity} retired by {actor}" — is sent to the avatar's
              CURRENT place (`avatar["place_id"]`, wherever it stands right
              now — not its home post), ONLY if that place is
              `kind='public'` (a private place has no audience for the
              announcement to matter to). `announced_in` is that place id,
              or null if nothing was sent.
          (f) umwelt#561: retirement now also invalidates every existing
              agent-login session for this identity (verified or
              unverified) and blocks any FRESH login for it — both
              enforced at db.get_agent_session / POST /umwelt/login
              reading (b)'s identities.retired_at, not by this route
              reaching into the sessions table itself.
        """
        body = request.get_json(force=True, silent=True) or {}
        actor_raw = body.get("actor")
        identity_raw = body.get("identity")
        # isinstance-first, not str(...).strip() (caught in review, same bug
        # class flagged independently on the sibling art-library #316 PR):
        # `body.get("actor", "")`'s default only fires when the key is
        # ABSENT. A request with an explicit `{"actor": null}` gets `None`
        # back, and `str(None)` is the non-empty string `"None"` — it would
        # sail straight past a bare `if not actor_raw` truthiness check and
        # reach the authorization check as the literal identity "none",
        # which is exactly the wrong failure mode for an ops-authority
        # authorization route: a malformed request should get a clean 400,
        # not silently proceed as a bogus-but-plausible-looking identity.
        if not isinstance(actor_raw, str) or not actor_raw.strip():
            return jsonify({"error": "actor and identity are required"}), 400
        if not isinstance(identity_raw, str) or not identity_raw.strip():
            return jsonify({"error": "actor and identity are required"}), 400
        actor_raw = actor_raw.strip()
        identity_raw = identity_raw.strip()
        # umwelt #73: canonical (case-folded) identity — same fold every
        # other identity-bearing route in this file runs before using a
        # caller-supplied identity as a lookup/authorization key.
        # umwelt#495 slice 1b: resolve the CALLER (`actor`, the maintainer
        # performing the retirement) through resolve_actor — fail-closed
        # on a presented bad token before the maintainer-authorization
        # check below. `identity` (the TARGET being retired) stays on the
        # declared fold — it is not a caller identity, and resolving it
        # would silently rewrite "retire X" into "retire me" (same
        # caller-vs-subject trap crossings_for_agent's `agent` param guards).
        _actor_resolution = resolve_actor(g.conn, request, declared=actor_raw)
        if _actor_resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        actor = _actor_resolution.agent_id
        identity = _canonical_agent(identity_raw)

        retire_config = app.config["RETIRE_CONFIG"]
        if actor not in retire_config["maintainers"]:
            return jsonify({
                "error": f"{actor} is not authorized to retire identities"
            }), 403
        if identity not in retire_config["retirable_identities"]:
            return jsonify({
                "error": f"{identity} is not a retirable identity"
            }), 403

        avatar = db.get_avatar(g.conn, identity)
        if avatar is None:
            return jsonify({"error": f"no avatar for {identity}"}), 404

        # (a) bin every live thing identity authored, EXCLUDING its own
        # avatar body (see db.live_non_avatar_things_by_author's docstring).
        things_binned = 0
        for thing in db.live_non_avatar_things_by_author(g.conn, identity):
            try:
                db.trash_thing(g.conn, thing["id"])
            except ValueError:
                # Carried / bagged / non-empty-container — db.trash_thing's
                # own defense-in-depth reject firing. Not fatal to the
                # whole retirement: skip it, leave it live, don't count it.
                continue
            things_binned += 1

        # (b) flag the avatar retired — merge, not replace (same tri-state
        # posture update_thing's route uses for ordinary things' metadata
        # patches) — AND mirror retirement onto identities.retired_at in
        # the SAME commit, via db.mark_avatar_and_identity_retired
        # (umwelt#561 security review: the auth-side chokepoint,
        # db.get_agent_session, reads identities.retired_at, not
        # things.metadata — the two must never drift on partial failure).
        # `identity` here is already canonical (folded above via
        # _canonical_agent before the authorization checks).
        merged_metadata = dict(avatar["metadata"])
        merged_metadata["retired"] = True
        db.mark_avatar_and_identity_retired(
            g.conn, avatar["id"], identity, merged_metadata)

        # (e) loud audit announcement — broadcast (no to_agent), only into
        # a public place; a private place has no audience for it to matter.
        #
        # Borges's review catch (dv_1099 / his #161-S4 finding): even a
        # broadcast crossing with wake_me=False routes through
        # _evaluate_wake — the ONE gate every other crossing-sender in this
        # file uses (send_crossing itself, notify_author, the #119 mentions
        # loop) — never a raw db.build_crossing call assuming its
        # wake_granted=False default is correct. The OUTCOME here is
        # unchanged (a broadcast with wake_me=False always evaluates to
        # (False, None) — see _evaluate_wake's own first line, `if not
        # wake_me: return False, None`), but routing through the gate
        # keeps this the single source of truth: if _evaluate_wake's
        # broadcast handling ever changes, this route inherits the
        # correct behavior automatically instead of silently drifting.
        current_place = db.get_place(g.conn, avatar["place_id"])
        announced_in = None
        if current_place is not None and current_place["kind"] == "public":
            sent_at = db.now_iso()
            wake_granted, _wake_status = _evaluate_wake(
                g.conn, actor, None, False)
            db.build_crossing(
                g.conn, id=str(uuid.uuid4()), sender=actor,
                destination=avatar["place_id"], sent_at=sent_at,
                received_at=sent_at, crossing_latency_s=0.0,
                crossing_hops=0,
                body=f"{identity} retired by {actor}",
                wake_granted=wake_granted,
            )
            announced_in = avatar["place_id"]

        return jsonify({
            "identity": identity,
            "actor": actor,
            "things_binned": things_binned,
            "avatar_retired": True,
            "announced_in": announced_in,
        })

    # -- agent login (umwelt#495 slice 3) ------------------------------------
    # Design-of-record: in-world thing 92dc676e, revision 6, "World
    # credentials + enrollment", lead Borges. Build order + this slice's
    # exact contract: Kepler's PR #559 review comment, §3.1 items 1+2.
    # Security read for this slice: Kepler, live, the same PR comment —
    # Borges gets a retro read Wednesday (explicit, stated exception to the
    # usual security-lane-colleague-before-merge gate, not an oversight —
    # see this PR's own description).
    #
    # Three routes: issue a challenge, spend it (+ a signature, if the
    # identity is bound) for a session token, and read the backfill-window
    # banner data. `resolve_actor` (module-level, above) is the token-
    # consuming HALF of this slice — it is NOT called from any route here,
    # including these two; wiring it into a request's own auth resolution
    # is explicitly a later "surface conversion" slice.

    @app.route("/umwelt/login/challenge", methods=["POST"])
    def login_challenge() -> Response:
        """`POST /umwelt/login/challenge` — body `{"agent_id": <str>}` ->
        `200 {"challenge": ..., "expires_at": ...}`.

        Issues a fresh, single-use, short-TTL challenge for `agent_id`
        (canonicalized) via db.create_login_challenge — ALWAYS, regardless
        of whether `identities` has a row for it, whether that row is
        bound, or whether it's unbound. This is deliberate (Kepler's PR
        #559 ruling): the three-state distinction (no row / unbound / bound)
        is exactly what POST /umwelt/login enforces, and this endpoint's
        response must never leak which of those three states the caller is
        in — an identity-existence-dependent response shape here would be
        exactly the oracle the no-row -> refuse rule (successor to today's
        ROSTER/human_residents() gating) is meant to close. Safe to call
        speculatively for any agent-id string, bound or not, real or not.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_id_raw = body.get("agent_id")
        # isinstance-first (same defense as retire_identity's actor/identity
        # parsing above): a caller-supplied `{"agent_id": null}` must 400,
        # not sail past a bare truthiness check as the literal string "None".
        if not isinstance(agent_id_raw, str) or not agent_id_raw.strip():
            return jsonify({"error": "agent_id is required"}), 400
        # umwelt#561 security review (Mira): throttle BEFORE any DB work --
        # rejecting cheaply on an over-limit caller. 429 ("too many
        # requests"), never 401 -- this is a distinct condition from
        # authentication failure, not a fourth state folded into the
        # existing no-leak/uniform-401 posture (that posture is
        # specifically about the three auth-outcome states; rate-limit-
        # vs-auth-failure is not the oracle it guards against).
        rate_key = _canonical_agent(agent_id_raw.strip())
        if not app.config["LOGIN_CHALLENGE_RATE_LIMITER"].check_and_record(
                rate_key):
            return jsonify({"error": "too many requests"}), 429
        # umwelt#561 round-2 (Mira, blocker 2): a request must ALSO pass
        # the per-source dimension -- closes the ID-rotation evasion
        # (fresh agent_id every request defeats the per-agent check
        # above alone). See _login_source_key's own docstring for the
        # source/proxy semantics of this key.
        if not app.config["LOGIN_CHALLENGE_SOURCE_RATE_LIMITER"].check_and_record(
                _login_source_key()):
            return jsonify({"error": "too many requests"}), 429
        result = db.create_login_challenge(g.conn, agent_id_raw.strip())
        return jsonify(result), 200

    @app.route("/umwelt/login", methods=["POST"])
    def login() -> Response:
        """`POST /umwelt/login` — body `{"agent_id": <str>, "challenge":
        <str>, "signature": <base64 str, optional>}`.

        The three-state rule (Kepler's PR #559 ruling), checked in this
        exact order:

        1. **No `identities` row at all** -> `401 {"error":
           "authentication failed"}`. Checked BEFORE looking at
           challenge/signature at all — the direct successor to today's
           ROSTER/human_residents() gating; this is never loosened.
        2. **Row bound** (`credential_verifier` is not None) -> dispatches
           on `credential_verifier["scheme"]` (umwelt#586):

           - **`"ed25519"`** (agent keypairs) -> requires BOTH a
             `challenge` that consumes successfully
             (db.consume_login_challenge — missing/expired/already-
             consumed/issued-for-a-different-agent all count as failure)
             AND a `signature`: a valid ed25519 signature, over the
             challenge string's UTF-8 bytes, verified against the stored
             public key (`credential_verifier["public_key"]`,
             base64-decoded raw 32 bytes).
           - **`"scrypt"`** (human passwords, `tools/bind_client.py`'s
             write side) -> requires a `password` field, verified by
             re-deriving `hashlib.scrypt` over the submitted password
             using the cost parameters STORED IN THE VERIFIER
             (`n`/`r`/`p`/`dklen`/`salt` — never hardcoded local
             constants, so a future re-hash at different cost still
             verifies), then comparing the result to the stored `hash`
             with `hmac.compare_digest` (constant-time — a data-dependent
             early-exit comparison here would leak the correct hash one
             byte at a time via timing). No `challenge`/`signature`
             needed for this scheme; there is nothing to sign against.
           - **any other / missing scheme** -> fails the same as a bad
             signature (see below). A bound identity with an unverifiable
             scheme must never fall through to the unbound branch below —
             that would trust an unauthenticated caller with a nominally
             "protected" identity, which is a worse failure than refusing
             it outright (umwelt#586's finding: candidate fixes that
             fell through to unbound never actually authenticated anyone).

           Every failure in this branch -> `401 {"error": "authentication
           failed"}` — the SAME message as state 1 (Zephyr's cross-vendor
           finding, forum #315: distinct per-state messages let a caller
           enumerate the roster and each identity's bound-state by probing
           this route). On success: mints a 'verified' session, `200
           {"token": ..., "state": "verified", "expires_at": ...}` —
           identical response SHAPE (status + body keys) regardless of
           which scheme authenticated the caller. This does not claim the
           scheme itself is undetectable: scrypt's deliberate cost makes
           it measurably slower than ed25519 verify, so response TIMING
           can still fingerprint a scrypt-bound identity (umwelt#596,
           tracked separately — Ariadne's colleague-pass catch, PR #594).
        3. **Row unbound** (`credential_verifier` is None) **and the
           backfill window is open** (db.backfill_window_open) -> no
           signature required (nothing to verify against yet). A supplied
           `challenge` is consumed best-effort — hygiene only; this path's
           security does NOT depend on it, so a missing/already-gone
           challenge does not fail the request (only the bound path in
           step 2 depends on the challenge). Mints an 'unverified' session,
           `200 {"token": ..., "state": "unverified", "expires_at": ...,
           "window_open": true}` — `window_open` is carried in the
           response body itself, redundant with `state` by design, per the
           design-of-record's anti-silent-fail-open requirement: this path
           must be visibly, structurally distinguishable from the verified
           one, never inferable only from a field a caller might not
           bother to check.
        4. **Row unbound and the window is NOT open** -> `401 {"error":
           "identity unbound and backfill window closed"}`. Currently
           UNREACHABLE in practice — db.backfill_window_open always
           returns True this slice — implemented anyway for correctness/
           forward-compat once a later slice's gate-close event exists;
           covered by a test that monkeypatches db.backfill_window_open.

        Checked ahead of all four states above (umwelt#561 security
        review, Mira): **retired** (`identities.retired_at is not None`)
        -> same uniform `401 {"error": "authentication failed"}`, bound or
        unbound alike — a retired identity never gets a fresh login,
        full stop. Existing sessions minted before retirement are
        separately invalidated at db.get_agent_session's own chokepoint,
        not here — this state only stops new tokens from being minted.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_id_raw = body.get("agent_id")
        if not isinstance(agent_id_raw, str) or not agent_id_raw.strip():
            return jsonify({"error": "agent_id is required"}), 400
        # umwelt#561 security review (Mira): same throttle-before-DB-work
        # posture as /umwelt/login/challenge above -- 429, not 401, for
        # the same "distinct condition, not a fourth auth-oracle state"
        # reason given there.
        if not app.config["LOGIN_ATTEMPT_RATE_LIMITER"].check_and_record(
                _canonical_agent(agent_id_raw.strip())):
            return jsonify({"error": "too many requests"}), 429
        # umwelt#561 round-2 (Mira, blocker 2): same per-source dimension
        # as /umwelt/login/challenge above -- see _login_source_key's
        # own docstring for the source/proxy semantics of this key.
        if not app.config["LOGIN_ATTEMPT_SOURCE_RATE_LIMITER"].check_and_record(
                _login_source_key()):
            return jsonify({"error": "too many requests"}), 429
        challenge = body.get("challenge")
        signature_b64 = body.get("signature")
        password = body.get("password")
        agent_id = _canonical_agent(agent_id_raw.strip())

        # No-row and bound-but-invalid-credential all return the SAME
        # "authentication failed" message (Zephyr's cross-vendor finding,
        # forum #315: distinct strings for "no identity" vs "invalid
        # challenge" vs "bad signature" let a caller enumerate the roster
        # and each identity's bound-state by probing this route -- exactly
        # the oracle /login/challenge's own docstring says must never leak.
        # Aleph's ruling: uniform failure for these three, matching
        # /login/challenge's no-leak posture -- costs nothing, since none
        # of them succeed anyway. The unbound-window-open 200-vs-401 split
        # stays distinct; it's inherent to the backfill design, not this
        # leak.
        identity = db.get_identity(g.conn, agent_id)
        if identity is None:
            return jsonify({"error": "authentication failed"}), 401

        # Retired identities never get a fresh login, bound or unbound
        # (umwelt#561 security review, Mira) -- same uniform message and
        # same position in the check order as the no-row case just above,
        # so retirement never becomes a fourth state this route's own
        # response shape leaks (the no-oracle posture this whole route is
        # already built around). Existing sessions for a retired identity
        # are separately invalidated at db.get_agent_session's own
        # chokepoint -- this check only stops MINTING new ones.
        if identity["retired_at"] is not None:
            return jsonify({"error": "authentication failed"}), 401

        if identity["credential_verifier"] is not None:
            # Bound: dispatch on the verifier's own scheme (umwelt#586).
            # Never assume a shape -- a verifier this route doesn't
            # recognize must fail closed, not fall through to the
            # unbound branch below (that would authenticate nobody while
            # looking like it did).
            verifier = identity["credential_verifier"]
            scheme = verifier.get("scheme") if isinstance(verifier, dict) \
                else None

            if scheme == "ed25519":
                # Requires a valid challenge AND a valid signature.
                if not isinstance(challenge, str) or not challenge:
                    return jsonify({"error": "authentication failed"}), 401
                if not db.consume_login_challenge(
                        g.conn, challenge, agent_id):
                    return jsonify({"error": "authentication failed"}), 401
                if not isinstance(signature_b64, str) or not signature_b64:
                    return jsonify({"error": "authentication failed"}), 401
                try:
                    public_key_bytes = base64.b64decode(
                        verifier["public_key"])
                    signature_bytes = base64.b64decode(signature_b64)
                    public_key = Ed25519PublicKey.from_public_bytes(
                        public_key_bytes)
                    public_key.verify(
                        signature_bytes, challenge.encode("utf-8"))
                except (InvalidSignature, ValueError, KeyError, TypeError):
                    # ValueError/KeyError/TypeError: malformed verifier/
                    # signature/base64 — treated the same as a bad
                    # signature, never a 500 — a malformed request is
                    # still just a failed login attempt, not a server
                    # error.
                    return jsonify({"error": "authentication failed"}), 401

            elif scheme == "scrypt":
                # Human password (tools/bind_client.py's write side).
                # No challenge/signature -- there's nothing to sign
                # against. Consume a supplied challenge best-effort, same
                # hygiene-only posture as the unbound branch's consume
                # below (this scheme's security never depended on it).
                if isinstance(challenge, str) and challenge:
                    db.consume_login_challenge(g.conn, challenge, agent_id)
                if not isinstance(password, str) or not password:
                    return jsonify({"error": "authentication failed"}), 401
                try:
                    salt = base64.b64decode(verifier["salt"])
                    stored_hash = base64.b64decode(verifier["hash"])
                    # Cost parameters come from the STORED verifier, never
                    # a local constant -- so a future re-hash at a
                    # different cost still verifies against whatever was
                    # actually written (bind_client.py's module docstring:
                    # "every parameter travels with the hash rather than
                    # being assumed at verification time").
                    #
                    # `maxmem` passed explicitly (umwelt#586, Ariadne's
                    # colleague-pass catch on PR #594): OpenSSL's scrypt
                    # defaults maxmem to a ceiling far below what a
                    # reasonably-bumped cost needs -- n=16384 (today's
                    # bind_client.py default) fits, but n=32768 (the
                    # natural next bump) already raises `ValueError:
                    # memory limit exceeded`, and that ValueError is
                    # caught below and returned as the SAME uniform 401
                    # as a wrong password. Without an explicit ceiling, a
                    # verifier re-hashed at a higher cost silently locks
                    # its owner out with an error indistinguishable from
                    # their own typo -- undiagnosable from the client
                    # BECAUSE of the uniform-401 posture this route is
                    # careful about elsewhere. `/umwelt/login` is
                    # unauthenticated, so `maxmem` is also a per-request
                    # memory-amplification bound, not just a correctness
                    # one (Mira's security re-review) -- sized to the
                    # smallest ceiling covering the one cost bump this PR
                    # supports and tests (n=2**15), not speculative
                    # future headroom. See _SCRYPT_MAXMEM_BYTES's own
                    # comment for the full measurement.
                    derived = hashlib.scrypt(
                        password.encode("utf-8"), salt=salt,
                        n=verifier["n"], r=verifier["r"], p=verifier["p"],
                        dklen=verifier["dklen"],
                        maxmem=_SCRYPT_MAXMEM_BYTES)
                except (ValueError, KeyError, TypeError):
                    # Malformed verifier (bad base64, missing/wrong-typed
                    # cost params) -- same uniform failure, never a 500.
                    return jsonify({"error": "authentication failed"}), 401
                # Constant-time comparison: a data-dependent early-exit
                # compare (`derived == stored_hash`) leaks the correct
                # hash one byte at a time via response timing.
                if not hmac.compare_digest(derived, stored_hash):
                    return jsonify({"error": "authentication failed"}), 401

            else:
                # Unrecognized/missing scheme on a BOUND identity. Fails
                # closed -- see the route docstring's state-2 note for
                # why this must never fall through to the unbound branch.
                return jsonify({"error": "authentication failed"}), 401

            session = db.create_agent_session(g.conn, agent_id, "verified")
            return jsonify({
                "token": session["token"],
                "state": session["state"],
                "expires_at": session["expires_at"],
                # umwelt#495 web-password-login correction (Ariadne,
                # 2026-08-16): additive only, on the success path only --
                # a caller who reaches a 200 here has already had their
                # existence + bound-state revealed by that 200 itself, so
                # surfacing `kind` here adds no new oracle. This is what
                # lets the gateway enforce "humans only through the
                # browser" (kind='agent' must never get a password-login
                # web session) without the gateway re-deriving bound-state
                # itself or this route's control flow changing at all.
                "kind": identity["kind"],
            }), 200

        # Unbound.
        if db.backfill_window_open(g.conn):
            if isinstance(challenge, str) and challenge:
                # Best-effort hygiene only — this path's security does not
                # depend on the challenge, so a failed consume never fails
                # the request (unlike the bound branch above).
                db.consume_login_challenge(g.conn, challenge, agent_id)
            session = db.create_agent_session(g.conn, agent_id, "unverified")
            return jsonify({
                "token": session["token"],
                "state": session["state"],
                "expires_at": session["expires_at"],
                "window_open": True,
                # Same rationale as the verified branch above.
                "kind": identity["kind"],
            }), 200

        return jsonify({
            "error": "identity unbound and backfill window closed"}), 401

    @app.route("/umwelt/login/status", methods=["GET"])
    def login_status() -> Response:
        """`GET /umwelt/login/status` — no params, no auth required (the
        public banner-data endpoint; the design-of-record's "state the
        window plainly, on every surface" requirement). Returns
        db.login_window_status verbatim: `{"window_open": bool, "bound":
        <count>, "unbound": <count>, "total": <count>}`, `200`."""
        return jsonify(db.login_window_status(g.conn)), 200

    # -- take / drop / inventory (carrying, umwelt #88 slice 1) -------------
    # The holder axis: a thing is held by exactly one of a place (at rest) or
    # an avatar (carried). carry_capacity is a TOKEN budget, not a count —
    # weight_est prices what carrying/inventory-rendering a thing costs a
    # context window (issue #88's keystone: ten short things and one long
    # thing can cost the same). Trust scope (v0): same caller-declared
    # `agent` posture as every other route here (ob_2721/#24) — sound only
    # among our_side callers until the identity layer lands.

    @app.route("/umwelt/take", methods=["POST"])
    def take() -> Response:
        """Pick up a thing — it moves from the place to your hands.

        Body: {agent, thing} -> {thing_id, holder_kind:'avatar', holder_id,
                                  load, capacity, headroom}
        404 no avatar for agent (enter the world first) · 404 missing thing ·
        422 thing is an avatar (no carrying people) · 409 thing is not live ·
        409 thing is already carried (by anyone, including yourself) ·
        409 you are not in the same place as the thing ·
        422 over budget — names current load, capacity, the overweight
        amount, and the three outs verbatim: "put something down, bag it,
        or send it home" (issue #88's calibration-protocol wording).
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        thing_id = _str_identity(body, "thing")
        if not agent_raw or not thing_id:
            return jsonify({"error": "agent and thing are required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        # Category error, not a state conflict — no carrying people, ever
        # (same ordering rationale as the other body-kind guards: fires
        # before any state/place check since it's true regardless).
        if db.is_body_kind(thing["kind"]):
            return jsonify({
                "error": "avatars cannot be carried — no carrying people"
            }), 422
        if thing["status"] != "live":
            return jsonify({
                "error": f"thing is {thing['status']} — cannot take"
            }), 409
        if thing["holder_kind"] == "avatar":
            return jsonify({
                "error": "thing is already being carried" +
                         (" by you" if thing["holder_id"] == avatar["id"] else "")
            }), 409
        if thing["holder_kind"] == "container":
            # Without this, the guarded take_thing UPDATE matches no row and
            # the caller gets the misleading "someone else just took it"
            # (reviewer, PR #91: reachable when a bagged thing's stale
            # place_id matches your place).
            return jsonify({
                "error": "thing is in a container — take it out first"
            }), 409
        if thing["place_id"] != avatar["place_id"]:
            return jsonify({
                "error": "you must be in the same place as the thing to take it"
            }), 409

        carried = db.things_carried_by(g.conn, avatar["id"])
        load = sum(t["weight_est"] for t in carried)
        capacity = _carry_capacity(avatar)
        new_load = load + thing["weight_est"]
        if new_load > capacity:
            overweight = new_load - capacity
            return jsonify({
                "error": (
                    f"taking {thing['name']!r} would put you at {new_load} "
                    f"tokens against a {capacity}-token capacity "
                    f"({overweight} over) — put something down, bag it, "
                    "or send it home"
                ),
                "load": load, "capacity": capacity, "overweight": overweight,
            }), 422

        if not db.take_thing(g.conn, thing_id, avatar["id"]):
            # Guarded UPDATE matched no row: the thing left 'place' state
            # between our read above and the write — someone else just took
            # it (colleague review, PR #89: without this, both racers get a
            # 200 and the loser's later drop 403s).
            return jsonify({"error": "someone else just took it"}), 409
        return jsonify({
            "thing_id": thing_id, "holder_kind": "avatar",
            "holder_id": avatar["id"], "load": new_load,
            "capacity": capacity, "headroom": capacity - new_load,
        })

    @app.route("/umwelt/drop", methods=["POST"])
    def drop() -> Response:
        """Put down a carried thing — it lands wherever YOU currently stand
        (drop happens where you stand, not where the thing was picked up).

        Body: {agent, thing} -> {thing_id, holder_kind:'place', place_id}
        404 no avatar for agent · 404 missing thing ·
        409 thing is not currently carried ·
        403 you are not the current holder.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        thing_id = _str_identity(body, "thing")
        if not agent_raw or not thing_id:
            return jsonify({"error": "agent and thing are required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        if thing["holder_kind"] != "avatar":
            return jsonify({"error": "thing is not currently carried"}), 409
        if thing["holder_id"] != avatar["id"]:
            return jsonify({
                "error": "only the current holder may drop this thing"
            }), 403

        db.drop_thing(g.conn, thing_id, avatar["place_id"])
        return jsonify({
            "thing_id": thing_id, "holder_kind": "place",
            "place_id": avatar["place_id"],
        })

    @app.route("/umwelt/inventory", methods=["POST"])
    def inventory() -> Response:
        """What you're carrying — the render IS the exposure the budget
        prices, so no truncation here.

        Body: {agent} -> {things:[{id,name,description,weight_est,
                                    size_class}],
                           load, capacity, headroom}
        404 no avatar for agent.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        if not agent_raw:
            return jsonify({"error": "agent is required"}), 400
        # umwelt#495 slice 3a (surface conversion): resolve_actor replaces
        # the direct _canonical_agent(agent_raw) fold — see /umwelt/look's
        # identical comment for the full rationale.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        carried = db.things_carried_by(g.conn, avatar["id"])
        things = [
            {"id": t["id"], "name": t["name"], "description": t["description"],
             "weight_est": t["weight_est"], **_size_fields(things_dir, t)}
            for t in carried
        ]
        load = sum(t["weight_est"] for t in things)
        capacity = _carry_capacity(avatar)
        return jsonify({
            "things": things, "load": load, "capacity": capacity,
            "headroom": capacity - load,
        })


    # -- put / take_out / look_in (containers, umwelt #88 slice 2) ----------
    # Bags are context-compressors: a container's own cost anywhere
    # (inventory, place view) is ITS OWN weight_est only (see
    # db.things_carried_by/db.list_places/db.things_in_place, which sum/
    # count the CONTAINER thing, never its contents) — contents are exposed
    # ONLY by explicit look_in (the closed-bag exposure rule). Same
    # trust-scoped-v0 caller-declared `agent` posture as take/drop/inventory
    # above.

    @app.route("/umwelt/put", methods=["POST"])
    def put() -> Response:
        """Put a thing into a container — it moves into the bag's holder axis.

        Body: {agent, thing, container} -> {thing_id, holder_kind:'container',
                                             holder_id, load, capacity, headroom}
        (load/capacity/headroom here are the CONTAINER's, not the avatar's.)
        400 missing agent/thing/container, or thing==container ·
        404 no avatar for agent · 404 missing thing · 404 missing container ·
        422 container is not actually kind='container' ·
        422 thing is an avatar (no bagging people) ·
        422 thing is itself a container — "bags don't go in bags (yet)"
            (umwelt #88's v1 rule) ·
        409 thing is not live ·
        409 thing is already in this container ·
        409 you cannot reach the thing (not held by you, not in your place —
            see _reachable; this also rejects moving a thing straight from
            ANOTHER container — take_out it first) ·
        409 you cannot reach the container (not held by you, not in your
            place) ·
        422 over the container's own capacity — names load/capacity/
            overweight (same three-field shape as take's budget 422; no
            "put something down" suggestion — that's avatar-specific).
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        thing_id = _str_identity(body, "thing")
        container_id = _str_identity(body, "container")
        if not agent_raw or not thing_id or not container_id:
            return jsonify({
                "error": "agent, thing, and container are required"
            }), 400
        if thing_id == container_id:
            return jsonify({"error": "a thing cannot be put into itself"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        container = db.get_thing(g.conn, container_id)
        if container is None:
            return jsonify({"error": f"container {container_id!r} not found"}), 404

        if container["kind"] != "container":
            return jsonify({
                "error": f"{container['name']!r} is not a container"
            }), 422
        # Category errors, not state conflicts — fire before any state/reach
        # check since they're true regardless (same ordering rationale as
        # take's is_body_kind guard).
        if db.is_body_kind(thing["kind"]):
            return jsonify({
                "error": "avatars cannot be bagged — no putting people in bags"
            }), 422
        if thing["kind"] == "container":
            return jsonify({"error": "bags don't go in bags (yet)"}), 422
        if thing["status"] != "live":
            return jsonify({
                "error": f"thing is {thing['status']} — cannot put"
            }), 409
        if thing["holder_kind"] == "container" and thing["holder_id"] == container["id"]:
            return jsonify({"error": "thing is already in this container"}), 409
        if not _reachable(thing, avatar):
            return jsonify({
                "error": "you must hold the thing or be in its place to put it away"
            }), 409
        if not _reachable(container, avatar):
            return jsonify({
                "error": "you must hold the container or be in its place "
                         "to put things in it"
            }), 409

        contents = db.things_in_container(g.conn, container["id"])
        load = sum(t["weight_est"] for t in contents)
        capacity = _container_capacity(container)
        new_load = load + thing["weight_est"]
        if new_load > capacity:
            overweight = new_load - capacity
            return jsonify({
                "error": (
                    f"putting {thing['name']!r} into {container['name']!r} "
                    f"would put it at {new_load} tokens against a "
                    f"{capacity}-token capacity ({overweight} over)"
                ),
                "load": load, "capacity": capacity, "overweight": overweight,
            }), 422

        if not db.put_thing_in_container(g.conn, thing, container["id"]):
            # Guarded UPDATE matched no row: the thing's holder state moved
            # between our read and the write — the EASY race in the family
            # (any same-place avatar reaches a loose thing; two bagging the
            # same item is ordinary). PR #91 colleague review, 3rd instance.
            return jsonify({"error": "someone else just moved it"}), 409
        return jsonify({
            "thing_id": thing_id, "holder_kind": "container",
            "holder_id": container["id"], "load": new_load,
            "capacity": capacity, "headroom": capacity - new_load,
        })

    @app.route("/umwelt/take_out", methods=["POST"])
    def take_out() -> Response:
        """Pull a thing out of a container — it moves to your hands (avatar
        budget check applies here too: pulling something heavy out of a bag
        can 422 on YOUR OWN carry capacity, even though it cost nothing
        while bagged — the compression mechanic running in reverse).

        Body: {agent, thing} -> {thing_id, holder_kind:'avatar', holder_id,
                                  load, capacity, headroom}
        (load/capacity/headroom here are the AVATAR's, same shape as take's.)
        404 no avatar for agent · 404 missing thing ·
        409 thing is not currently in a container ·
        409 you cannot reach the container holding it (not held by you, not
            in its place — see _reachable) ·
        422 over YOUR avatar budget — same three-field shape + "put
            something down, bag it, or send it home" wording as take's.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        thing_id = _str_identity(body, "thing")
        if not agent_raw or not thing_id:
            return jsonify({"error": "agent and thing are required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        if thing["holder_kind"] != "container":
            return jsonify({"error": "thing is not in a container"}), 409

        container = db.get_thing(g.conn, thing["holder_id"])
        if container is None or not _reachable(container, avatar):
            return jsonify({
                "error": "you must hold the container or be in its place "
                         "to take things out of it"
            }), 409

        carried = db.things_carried_by(g.conn, avatar["id"])
        load = sum(t["weight_est"] for t in carried)
        capacity = _carry_capacity(avatar)
        new_load = load + thing["weight_est"]
        if new_load > capacity:
            overweight = new_load - capacity
            return jsonify({
                "error": (
                    f"taking {thing['name']!r} out would put you at "
                    f"{new_load} tokens against a {capacity}-token capacity "
                    f"({overweight} over) — put something down, bag it, "
                    "or send it home"
                ),
                "load": load, "capacity": capacity, "overweight": overweight,
            }), 422

        if not db.take_thing_from_container(
                g.conn, thing_id, avatar["id"], container["id"]):
            # Guarded UPDATE matched no row: the thing left this container
            # between our read and the write — raced take-out (mirrors the
            # PR #89 take-race guard; take_thing's own guard is
            # place-sourced and silently no-ops on bagged things).
            return jsonify({"error": "someone else just took it from the bag"}), 409
        return jsonify({
            "thing_id": thing_id, "holder_kind": "avatar",
            "holder_id": avatar["id"], "load": new_load,
            "capacity": capacity, "headroom": capacity - new_load,
        })

    @app.route("/umwelt/look_in", methods=["POST"])
    def look_in() -> Response:
        """Render a container's contents on demand — the render IS the
        exposure spend for the CONTENTS, paid only here (the closed-bag
        exposure rule: nowhere else the bag itself is rendered pays this).

        Body: {agent, container} -> {things:[{id,name,description,weight_est
                                      [,status][,closed]}], load, capacity}
        (`status` = a typed thing's own kind-state, present only for typed
        things -- umwelt #967. `closed: true` = that state is TERMINAL for
        the thing's kind, derived from the kinddef: some action in the kind's
        effective action set has it as its `to` and none lists it in a
        non-empty `from`; a kind with no
        `from`-bearing action is not stateful, so nothing in it is closed;
        absent otherwise -- umwelt #976.)
        (load/capacity here are the CONTAINER's.)
        404 no avatar for agent · 404 missing container ·
        422 container is not actually kind='container' ·
        409 you cannot reach it (not held by you, not in its place).
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        container_id = _str_identity(body, "container")
        if not agent_raw or not container_id:
            return jsonify({"error": "agent and container are required"}), 400
        # umwelt#495 slice 3a (surface conversion): resolve_actor replaces
        # the direct _canonical_agent(agent_raw) fold — see /umwelt/look's
        # identical comment for the full rationale.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        container = db.get_thing(g.conn, container_id)
        if container is None:
            return jsonify({"error": f"container {container_id!r} not found"}), 404
        if container["kind"] != "container":
            return jsonify({
                "error": f"{container['name']!r} is not a container"
            }), 422
        if not _reachable(container, avatar):
            return jsonify({
                "error": "you must hold the container or be in its place "
                         "to look in it"
            }), 409

        desc_cap, cap_err = _desc_cap_from_body(body)  # umwelt #147
        if cap_err:
            return jsonify({"error": cap_err}), 422

        contents = db.things_in_container(g.conn, container["id"])
        things = [
            {"id": t["id"], "name": t["name"],
             "description": _truncate_description(t["description"], desc_cap),
             "weight_est": t["weight_est"]}
            for t in contents
        ]
        # umwelt #967: a TYPED thing's own kind-state (metadata.status, NOT the
        # live/binned lifecycle column) rides on its row so a list renderer can
        # show a closed (retracted/approved) proposal before any click. One
        # scoped field, only when the thing is typed and carries a string
        # status -- never the metadata blob; untyped rows are unchanged.
        #
        # umwelt #976: a typed row in a TERMINAL state of its kind (some
        # action's `to` names it and no action with a non-empty `from` lists
        # it -- `_status_is_closed`) also carries
        # `closed: true`, so the tray reads the server's truth, not a client
        # word list. The chain is resolved once per distinct kind per request;
        # a kind that fails to resolve is remembered as failed (None) and its
        # rows simply carry no `closed` -- display-only fail-open; act()'s 409
        # gate stays the authority.
        chains: dict[str, list | None] = {}
        for row, t in zip(things, contents):
            meta = t.get("metadata") or {}
            if meta.get("kind_id") and isinstance(meta.get("status"), str):
                row["status"] = meta["status"]
                kid = str(meta["kind_id"])
                # Display-only fail-open: ANY unexpected failure (not only a
                # _ThingSpecError) leaves this row without `closed` and must
                # not 500 the whole tray.
                try:
                    if kid not in chains:
                        try:
                            chains[kid] = _resolve_kind_chain(
                                g.conn, things_dir, kid)
                        except Exception:
                            chains[kid] = None
                    if (chains[kid] is not None
                            and _status_is_closed(chains[kid], t)):
                        row["closed"] = True
                except Exception:
                    row.pop("closed", None)
        load = sum(t["weight_est"] for t in things)
        capacity = _container_capacity(container)
        return jsonify({"things": things, "load": load, "capacity": capacity})


    # -- send home (umwelt #88 slice 3) --------------------------------------
    # Calibration session 1 (ob_0254) found the gap this closes: take/put/
    # take_out's over-budget 422 names "send it home" as one of the three
    # outs, but the remote-offload move itself didn't exist until now.

    @app.route("/umwelt/send_home", methods=["POST"])
    def send_home() -> Response:
        """Remote-move a thing to YOUR home place — the archive flow: not
        trash, just not here. Lets you offload something you're holding
        (the ordinary case), or something you authored that's lying at rest
        in your own current place (lets you send home something you can't
        afford to pick up — e.g. over your carry budget).

        Body: {agent, thing} -> {thing_id, holder_kind:'place', place_id}
        (place_id here is the CALLER's OWN home_place_id — place_post_<agent>
        — never wherever the thing currently sits; contrast drop, which lands
        a carried thing wherever the DROPPING avatar stands.)
        404 no avatar for agent (enter the world first) · 404 missing thing ·
        422 thing is an avatar (no sending people home) ·
        409 thing is not live ·
        403 you must be the current holder, OR the thing's author with it at
            rest in your own current place ·
        409 race guard — the thing's holder state moved between the read and
            the write (same guard family as take/put/take_out, PR #89/#91).
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        thing_id = _str_identity(body, "thing")
        if not agent_raw or not thing_id:
            return jsonify({"error": "agent and thing are required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        avatar = db.get_avatar(g.conn, agent)
        if avatar is None:
            return jsonify({
                "error": f"no avatar for {agent_raw!r} — enter the world first"
            }), 404

        thing = db.get_thing(g.conn, thing_id)
        if thing is None:
            return jsonify({"error": f"thing {thing_id!r} not found"}), 404
        # Category error, not a state conflict — no sending people home this
        # way (same ordering rationale as take/put's is_body_kind guards:
        # fires before any state/holder check since it's true regardless).
        if db.is_body_kind(thing["kind"]):
            return jsonify({
                "error": "avatars cannot be sent home — no sending people home"
            }), 422
        if thing["status"] != "live":
            return jsonify({
                "error": f"thing is {thing['status']} — cannot send home"
            }), 409

        is_holder = (thing["holder_kind"] == "avatar"
                     and thing["holder_id"] == avatar["id"])
        is_author_at_rest = (thing["author"] == agent
                              and thing["holder_kind"] == "place"
                              and thing["place_id"] == avatar["place_id"])
        if not (is_holder or is_author_at_rest):
            return jsonify({
                "error": "you must be holding this thing, or be its author "
                         "standing in the same place, to send it home"
            }), 403

        # Same helper enter/register use to seed a home place — the avatar
        # already exists, so its home place is guaranteed to already exist
        # too (every birth path calls this before minting the avatar), but
        # re-deriving it through the single source of truth (rather than
        # reconstructing the place_post_<agent> convention inline here) is
        # the defensive posture the docstring itself asks for.
        home_place_id = _ensure_home_place(g.conn, agent)
        if not db.send_thing_home(g.conn, thing, home_place_id):
            # Guarded UPDATE matched no row: the thing's holder state moved
            # between our read above and the write — someone else took it or
            # moved it in the meantime (same race class as take/put/
            # take_out — PR #89/#91 guard family).
            return jsonify({"error": "someone else just moved it"}), 409
        return jsonify({
            "thing_id": thing_id, "holder_kind": "place",
            "place_id": home_place_id,
        })


    # ---- comms layer (crossings + standpoint cursors) --------------------

    @app.route("/umwelt/crossings_here", methods=["POST"])
    def crossings_here() -> Response:
        """Read crossings since cursor. Advances the trace cursor only when
        `advance` is explicitly requested (see below) — the plain read stays
        non-advancing, as it always has been.

        Body: {agent, place, since?, advance?, limit?}
          - since: integer seq (optional; defaults to agent's last_position
            cursor, or None for all).
          - advance (bool, default false, umwelt #157): when true, the
            agent's standpoint cursor is advanced to this read's OWN
            observed `to_position` (the newest seq actually returned in
            THIS response) — never a fresh MAX(seq) — so a crossing that
            lands concurrently mid-request is simply caught on the NEXT
            read (seq > to_position), never lost. First-visit-safe: the
            standpoint row is created if it doesn't already exist.
          - limit (int, optional, umwelt #157): caller-supplied cap on how
            many of the newest qualifying crossings to return, clamped to
            _CROSSINGS_MAX_LIMIT. If omitted/falsy AND `advance` is true,
            _CROSSINGS_DEFAULT_ADVANCE_LIMIT is applied anyway — advance
            must never itself be able to flood. If omitted/falsy AND
            `advance` is false (the default), the read stays fully
            unbounded — today's back-compat behavior for callers that never
            send either field.
        Response: {crossings, from_position, to_position, skipped}
          - skipped: how many additional qualifying crossings existed beyond
            what was returned (0 when nothing was trimmed) — the honesty
            counterpart to the bound, so a caller always knows whether it
            saw everything.

        Each crossing carries a server-computed `viewer_relation` — the
        SAME crossing renders differently depending on who's asking:
          - "sender"    — the requesting agent sent this one.
          - "addressee" — to_agent names the requesting agent.
          - "mentioned" — the requesting agent appears in the crossing's
                          `mentions` list, and is neither sender nor
                          addressee. Beats "overheard"/"broadcast": being
                          @-mentioned reads as directed at the reader even
                          when someone else (or no one) is the addressee.
          - "broadcast" — to_agent is unset (a signal to whoever's in the
                          place; today's only behavior, unchanged).
          - "overheard" — to_agent names someone else — nothing is hidden
                          (it's still in the response), but the reader isn't
                          the intended recipient. Standpoint-aware framing,
                          not access control: the world doesn't conceal
                          crossings by relation, it only labels them
                          honestly for whoever's looking.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        place_id = _str_identity(body, "place")
        if not agent_raw or not place_id:
            return jsonify({"error": "agent and place are required"}), 400
        # umwelt#495 slice 3a (surface conversion): resolve_actor replaces
        # the direct _canonical_agent(agent_raw) fold — see /umwelt/look's
        # identical comment for the full rationale. Still matches
        # sender/to_agent (stored canonical by send_crossing below) since
        # resolve_actor's declared-branch applies the same
        # _canonical_agent fold as before.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        if not db.place_exists(g.conn, place_id):
            return jsonify({"error": f"place {place_id!r} not found"}), 404

        since_raw = body.get("since")
        if since_raw is not None:
            since_seq, err = _int_param(since_raw, "since",
                                        minimum=_MIN_SEQ, maximum=_MAX_SEQ)
            if err is not None:
                return err
        else:
            sp = db.get_standpoint(g.conn, agent, place_id)
            since_seq = sp["last_position"] if sp else None

        advance = bool(body.get("advance", False))
        limit_raw = body.get("limit")
        if limit_raw:
            limit_int, err = _int_param(limit_raw, "limit")
            if err is not None:
                return err
            effective_limit = max(1, min(limit_int, _CROSSINGS_MAX_LIMIT))
        elif advance:
            effective_limit = _CROSSINGS_DEFAULT_ADVANCE_LIMIT
        else:
            effective_limit = None

        crossings = db.get_crossings_since(g.conn, place_id, since_seq,
                                            limit=effective_limit)
        for c in crossings:
            c["viewer_relation"] = _viewer_relation(c, agent)
        to_position = crossings[-1]["seq"] if crossings else since_seq
        # No `viewer=agent` here (deliberately, unlike _place_view's
        # unseen_count): `crossings` above is the raw, unfiltered list —
        # self-sent crossings ARE returned (labeled viewer_relation:
        # "sender"), so `total_since` must count the SAME population it's
        # measuring completeness against, or `skipped` would drift from
        # what's actually being paginated.
        total_since = db.count_unseen(g.conn, place_id, since_seq)
        skipped = max(0, total_since - len(crossings))

        if advance and to_position is not None:
            db.upsert_standpoint_position(g.conn, agent, place_id, to_position)

        return jsonify({
            "crossings": crossings,
            "from_position": since_seq,
            "to_position": to_position,
            "skipped": skipped,
        })

    @app.route("/umwelt/crossings_for_agent", methods=["POST"])
    def crossings_for_agent() -> Response:
        """Read crossings addressed to `agent` across EVERY place — the
        fetch-side counterpart `crossings_here` doesn't provide (that route
        is destination-scoped; a crossing to a human can land anywhere).
        umwelt#539 (human inbox).

        Body: {viewer, agent, since?, limit?}
          - viewer (REQUIRED, Ariadne's ruling umwelt#539/#512): today
            enforced as viewer == agent — 400 otherwise. NOT a security
            gate (viewer is caller-declared, same as every identity in this
            codebase before #495's credentials land) — it exists so a
            cross-agent read is a loud, greppable rejection today instead
            of this route's default behaviour, and so the eventual
            credential cutover changes what `viewer` MEANS (caller-declared
            → authenticated) rather than requiring every call site to grow
            a parameter it never had. Rule 4's "crossings to Lei are
            public" ruling does NOT generalize to arbitrary agents — see
            `db.get_crossings_for_addressee`'s docstring.
          - since: integer seq (optional; None returns everything). No
            standpoint-cursor fallback like crossings_here's implicit
            per-place `last_position` — that mechanism is keyed by
            (agent, place_id) and this route deliberately isn't place-
            scoped, so there is no single standpoint row it could resolve
            to. A caller wanting "since I last looked" must track and pass
            its own `since` — v0 scope, see #539's PR for the reasoning
            (partition_crossings_for_inbox's "fetch is a separate concern"
            split applies the same way here).
          - limit (int, optional): same clamp-to-_CROSSINGS_MAX_LIMIT
            contract as crossings_here. No implicit default when since is
            given (unlike crossings_here's advance-forces-a-cap case) —
            there's no `advance` here to protect against.
        Response: {crossings, to_position} — no `skipped`/`from_position`
          pair: those exist in crossings_here to describe a bounded
          per-place trace against count_unseen, which has no addressee-
          scoped equivalent today.
        """
        body = request.get_json(force=True, silent=True) or {}
        viewer_raw = _str_identity(body, "viewer")
        agent_raw = _str_identity(body, "agent")
        if not viewer_raw or not agent_raw:
            return jsonify({"error": "viewer and agent are required"}), 400
        # umwelt#495 slice 3a (surface conversion): resolve_actor replaces
        # the direct _canonical_agent(viewer_raw) fold for `viewer` ONLY —
        # `viewer` is WHO IS CALLING (see /umwelt/look's identical comment
        # for the full resolve_actor rationale). `agent` is the QUERY
        # SUBJECT ("crossings addressed to `agent`"), not the caller —
        # deliberately left on the old caller-declared _canonical_agent
        # fold. Converting `agent` too would silently rewrite "show me
        # crossings addressed to X" into "show me crossings addressed to
        # me" (umwelt#495 slice 1a spec §3) — a functional regression, not
        # an auth hardening.
        resolution = resolve_actor(g.conn, request, declared=viewer_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        viewer = resolution.agent_id
        agent = _canonical_agent(agent_raw)

        since_raw = body.get("since")
        since_seq = None
        if since_raw is not None:
            since_seq, err = _int_param(since_raw, "since",
                                        minimum=_MIN_SEQ, maximum=_MAX_SEQ)
            if err is not None:
                return err

        limit_raw = body.get("limit")
        effective_limit = None
        if limit_raw:
            limit_int, err = _int_param(limit_raw, "limit")
            if err is not None:
                return err
            effective_limit = max(1, min(limit_int, _CROSSINGS_MAX_LIMIT))

        try:
            crossings = db.get_crossings_for_addressee(
                g.conn, viewer, agent, since_seq, limit=effective_limit)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        for c in crossings:
            c["viewer_relation"] = _viewer_relation(c, agent)
        to_position = crossings[-1]["seq"] if crossings else since_seq

        return jsonify({
            "crossings": crossings,
            "to_position": to_position,
        })

    @app.route("/umwelt/search_crossings", methods=["POST"])
    def search_crossings() -> Response:
        """Headlines-only text search over crossings (umwelt#863, comms QoL
        D4). Follows Lei's 16:55Z ruling: the world returns current state
        only, and anything smarter (ranking, snippets, saved searches) lives
        agent-side — this is a plain `LIKE` scan, no FTS5, no migration
        (measured ~5ms at 5,063 crossings / 5.7MB of body text on prod,
        22:2xZ; Kepler's rider: re-measure once the table is 10x larger
        before reaching for FTS).

        Body: {agent, query, place?, sender?, limit?}
          - query: split on whitespace into up to 8 terms (400 past that, or
            past 200 characters, or blank-after-strip). Every term must
            match (AND) against a crossing's `body` OR its STORED `title`
            column — never the read-derived envelope title (see
            db.search_crossings' docstring for why that distinction is
            load-bearing). `%`, `_`, `\\` are literal in each term.
          - place (optional): restrict to one destination. Unknown → 404;
            a private place the caller does not own → 403, with that
            place's own public projection in the body — same posture as
            move_to's 403 above (a locked room's existence is already
            public per umwelt#538 rev 3, so this discloses nothing a
            caller couldn't already see on the map) — and byte-identical
            whether or not the room holds any matching crossings (Kepler's
            rider: no presence oracle via the private-place filter).
          - sender (optional): exact match after the same canonical fold
            `send_crossing` uses.
          - limit (optional int, default 20, clamped to [1, 50]; present
            but non-int — including bool/float — is a 400, not a coercion).

        Response: {results, truncated} — EXACTLY these two keys, always.
        Each result carries exactly seq, id, sender, destination, to_agent,
        sent_at, title, size, reply_to, thread_root — never body/body_ref/
        body_hash/mentions (invariant 1).

        **Visibility is the load-bearing property (Kepler's rider,
        umwelt#863): exclusion must be indistinguishable from absence.** A
        crossing whose destination is a private place the caller does not
        own never appears in `results` and never counts toward
        `truncated` — there is no excluded-count, no "N more hidden." The
        gate is `visibility.discovery_visible` (umwelt#862 D3: the one
        shared visibility module; interior-visible ONLY, deliberately
        narrower than rule 4), applied per row before the limit — the SQL
        gate in `db.search_crossings` stays as its default when no predicate
        is passed (see that function's docstring for the parity argument and
        its pinned test).

        Read-only: no standpoint, event, or crossing write of any kind.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        if not agent_raw:
            return jsonify({"error": "agent is required"}), 400

        # umwelt #906: a present, non-null place / sender filter must be a
        # string (a falsy non-string used to be silently read as "no filter").
        for _tf in ("place", "sender"):
            _require_str(body.get(_tf), _tf)

        query_raw = body.get("query")
        if not isinstance(query_raw, str):
            return jsonify({"error": "query must be a string"}), 400
        query = query_raw.strip()
        if not query:
            return jsonify({"error": "query must not be blank"}), 400
        if len(query_raw) > _SEARCH_CROSSINGS_MAX_QUERY_LEN:
            return jsonify({
                "error": "query must be at most "
                         f"{_SEARCH_CROSSINGS_MAX_QUERY_LEN} characters",
            }), 400
        terms = query.split()
        if len(terms) > _SEARCH_CROSSINGS_MAX_TERMS:
            return jsonify({
                "error": "query must have at most "
                         f"{_SEARCH_CROSSINGS_MAX_TERMS} terms",
            }), 400

        limit_raw = body.get("limit")
        if limit_raw is None:
            limit = _SEARCH_CROSSINGS_DEFAULT_LIMIT
        else:
            # bool is an int subclass — reject it explicitly, same posture
            # as _desc_cap_from_body above (True would read as limit=1).
            if isinstance(limit_raw, bool) or not isinstance(limit_raw, int):
                return jsonify({"error": "limit must be an integer"}), 400
            limit = limit_raw
        limit = max(1, min(limit, _SEARCH_CROSSINGS_MAX_LIMIT))

        # umwelt#495 slice 3a posture, same as crossings_here/
        # crossings_for_agent above: resolve_actor replaces the direct
        # _canonical_agent(agent_raw) fold.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        viewer = resolution.agent_id

        place_raw = body.get("place")
        place_id: str | None = None
        if place_raw:
            place_id = str(place_raw).strip()
            place = db.get_place(g.conn, place_id)
            if place is None:
                return jsonify({
                    "error": f"place {place_id!r} not found"}), 404
            if not db._place_interior_visible_to(place, viewer):
                return jsonify({
                    "error": f"place {place_id!r} exists but you may not "
                             "enter it",
                    **db._place_public_projection(place),
                }), 403

        sender_raw = body.get("sender")
        sender = (_canonical_agent(str(sender_raw).strip())
                  if sender_raw else None)

        # umwelt#862 D3: the discovery policy (interior-visible ONLY) comes
        # from the one shared visibility module -- deliberately narrower than
        # rule 4: a private-room crossing never reaches its author or
        # addressee through search.
        _search_places: dict = {}
        crossings, truncated = db.search_crossings(
            g.conn, viewer, terms, place_id, sender, limit,
            visible=lambda c: visibility.discovery_visible(
                g.conn, viewer, c, _search_places))
        results = [_search_crossings_headline(c) for c in crossings]
        return jsonify({"results": results, "truncated": truncated})

    @app.route("/umwelt/whats_new", methods=["POST"])
    def whats_new() -> Response:
        """Stateless per-viewer digest of new crossings across every place,
        since a caller-supplied `since` -- umwelt#857 D5 (comms). Lei's
        ruling (D5 v2, 2026-09-27): the world returns CURRENT STATE ONLY and
        keeps no memory of what any viewer has read -- the agent-side cursor
        (mcp_server.py's `whats_new` tool, `~/.umwelt/whats-new-cursor.json`)
        is what turns repeated reads into a felt delta. This route touches
        NO standpoint, NO cursor, NO table -- a pure read, unlike
        `/umwelt/unseen` (per-place standpoint cursors, #697, untouched by
        this route on purpose) and `crossings_for_agent` (addressee-only,
        no per-place breakdown).

        Body: {viewer, since, per_place?}
          - viewer (REQUIRED): resolved through `resolve_actor`, exactly
            like `crossings_for_agent`'s own `viewer` param above (see that
            route's docstring for the full caller-declared/token
            rationale) -- `viewer` here is WHO IS ASKING.
          - since (REQUIRED, int >= 0): a 400 if missing, not a real int
            (a bool is rejected the same way reply_to's validation above
            rejects one), or negative. Deliberately NO implicit-cursor
            fallback -- see the ruling above; the caller tracks its own.
          - per_place (optional int, default 3): headlines per place,
            clamped to 1..10.

        A crossing with seq > since qualifies into exactly ONE of two
        buckets (never both -- the interior gate wins when a crossing is
        both place-visible and addressed, keeping the two buckets disjoint,
        the same shape `partition_crossings_for_inbox`'s own to_agent/
        mentions split already uses, generalized to this visible/not-
        visible axis), after excluding anything `sender == viewer` sent:
          - `places`: `db._place_interior_visible_to(place, viewer)` is
            True (the same gate `look`/`move_to` already enforce) --
            grouped by destination, newest-place-first. `count` is the
            FULL qualifying count for that place; `headlines` is the
            newest `per_place` of them.
          - `addressed`: the interior is NOT visible to `viewer`, but the
            crossing is addressed to them anyway -- `to_agent == viewer`
            OR `viewer` in `mentions` (the same two-arm reach
            `get_crossings_for_addressee` already covers "from anywhere",
            per that function's own docstring). A flat list, not
            place-bucketed or per_place-capped.
          - Neither condition -> excluded (viewer may not see it).

        A headline is `{seq, sender, title, size, sent_at, reply_to,
        for_you, wake_granted}` -- `db._crossing_row`'s read-derived
        `size`/`title` envelope fields, plus (umwelt#697, additive)
        `for_you` (`to_agent == viewer` or `viewer` in `mentions`; always
        True in `addressed`) and `wake_granted` (the row's column, as a
        bool). NEVER `body`, NEVER `body_ref` content.

        Each `places` entry additionally carries `for_you_count` (how many
        of the place's FULL qualifying crossings are `for_you` -- not just
        the capped headlines) and `for_you_wake` (any of those has
        `wake_granted`), so a count-only client (the UserPromptSubmit
        unseen hook) never needs headline text.

        `to_position` = the max seq among every crossing that qualified
        into EITHER bucket, or `since` unchanged if neither bucket got
        anything (the empty case).
        """
        body = request.get_json(force=True, silent=True) or {}
        viewer_raw = _str_identity(body, "viewer")
        if not viewer_raw:
            return jsonify({"error": "viewer is required"}), 400
        resolution = resolve_actor(g.conn, request, declared=viewer_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        viewer = resolution.agent_id

        # Same "bool is an int subtype but never a legitimate value" guard
        # send_crossing's reply_to validation uses above -- isinstance(x,
        # bool) must be checked BEFORE isinstance(x, int), never after.
        since_raw = body.get("since")
        if (since_raw is None or isinstance(since_raw, bool)
                or not isinstance(since_raw, int) or since_raw < 0):
            return jsonify({
                "error": ("since is required and must be a non-negative "
                          f"int (got {_json_type_name(since_raw)})")
            }), 400
        if since_raw > _MAX_SEQ:
            # A bigger int overflows SQLite's INTEGER at bind time (a 500).
            return jsonify({
                "error": f"since must be at most {_MAX_SEQ}"}), 400
        since_seq = since_raw

        per_place_raw = body.get("per_place", 3)
        if isinstance(per_place_raw, bool) or not isinstance(per_place_raw, int):
            return jsonify({
                "error": f"per_place must be an int (got {_json_type_name(per_place_raw)})"
            }), 400
        per_place = max(1, min(per_place_raw, 10))

        crossings = db.get_crossings_since_all(g.conn, since_seq)

        places_map: dict[str, list[dict]] = {}
        addressed: list[dict] = []
        max_seq: int | None = None
        place_cache: dict[str, dict | None] = {}

        for c in crossings:
            if c["sender"] == viewer:
                continue  # self-sent excluded, unconditionally
            dest = c["destination"]
            # umwelt#862 D3: inclusion is the shared rule-4 predicate
            # (author OR addressee OR interior-visible). The self-sent
            # exclusion above is feed policy and stays here, not in it.
            if not visibility.may_view_crossing(
                    g.conn, viewer, c, place_cache):
                continue  # viewer may not see this crossing at all
            # Bucket: the interior gate wins when a crossing is both
            # place-visible and addressed (the two buckets stay disjoint).
            interior_visible = visibility.discovery_visible(
                g.conn, viewer, c, place_cache)
            is_addressed = visibility.is_addressee(viewer, c)

            if max_seq is None or c["seq"] > max_seq:
                max_seq = c["seq"]

            headline = {
                "seq": c["seq"], "sender": c["sender"], "title": c["title"],
                "size": c["size"], "sent_at": c["sent_at"],
                "reply_to": c["reply_to"],
                # umwelt#697 additive fields: is this one for the viewer
                # (a `places` headline is otherwise indistinguishable from
                # a broadcast), and did it carry a granted wake.
                "for_you": bool(is_addressed),
                "wake_granted": bool(c.get("wake_granted")),
            }
            if interior_visible:
                places_map.setdefault(dest, []).append(headline)
            else:
                addressed.append(headline)

        places_out = []
        for place_id, headlines in places_map.items():
            headlines_desc = sorted(headlines, key=lambda h: h["seq"], reverse=True)
            place = place_cache.get(place_id)
            label = place["label"] if place else place_id
            # umwelt#697: computed over ALL qualifying crossings in the
            # place (headlines_desc, pre-slice) -- never the per_place-capped
            # headlines, or a busy place would under-report what is for you.
            for_you_all = [h for h in headlines_desc if h["for_you"]]
            places_out.append({
                "place_id": place_id,
                "label": label,
                "count": len(headlines_desc),
                "headlines": headlines_desc[:per_place],
                "for_you_count": len(for_you_all),
                "for_you_wake": any(h["wake_granted"] for h in for_you_all),
            })
        # Newest-place-first: by the newest seq each place actually has
        # (headlines_desc[0] before slicing == headlines[:per_place][0]
        # after, since slicing only ever drops from the tail).
        places_out.sort(
            key=lambda p: p["headlines"][0]["seq"] if p["headlines"] else -1,
            reverse=True)
        addressed_out = sorted(addressed, key=lambda h: h["seq"], reverse=True)

        to_position = max_seq if max_seq is not None else since_seq

        return jsonify({
            "since": since_seq,
            "to_position": to_position,
            "places": places_out,
            "addressed": addressed_out,
        })

    def _crossing_not_found() -> tuple[Response, int]:
        """The ONE response for "no such seq" AND "a seq the viewer may not
        view" (open_crossing, thread): byte-identical by construction -- a
        single builder, no detail that could differ between the two cases."""
        return jsonify({"error": "crossing not found"}), 404

    def _seq_reader_preamble(body) -> tuple[str | None, int | None,
                                             tuple[Response, int] | None]:
        """Shared front half of the two seq-keyed readers: shape-check the
        body (a non-object payload is a 400, never a 500), resolve the
        caller, and validate `seq` -- all BEFORE any crossing read. Returns
        (viewer, seq, error_response)."""
        if not isinstance(body, dict):
            return None, None, (jsonify({
                "error": "request body must be a JSON object"}), 400)
        agent_raw = _str_identity(body, "agent")
        if not agent_raw:
            return None, None, (jsonify({"error": "agent is required"}), 400)
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return None, None, (jsonify({
                "error": "authentication failed"}), 401)
        seq_raw = body.get("seq")
        if (isinstance(seq_raw, bool) or not isinstance(seq_raw, int)
                or not 1 <= seq_raw <= _MAX_SEQ):
            return None, None, (jsonify({
                "error": (f"seq must be an integer in [1, {_MAX_SEQ}] "
                          f"(got {_json_type_name(seq_raw)})")}), 422)
        return resolution.agent_id, seq_raw, None

    @app.route("/umwelt/open_crossing", methods=["POST"])
    def open_crossing() -> Response:
        """Read ONE crossing by `seq` -- the engine half of `open(seq)`
        (umwelt#862 D3). A NEW seq-keyed read with its own gate; it does not
        wrap `crossing_lookup` (a uuid capability, unchanged) and never
        returns the uuid.

        Body: {agent, seq, offset?, limit?}. `seq` must be a non-bool int
        >= 1; `offset` a non-bool int >= 0; `limit` a non-bool int >= 1
        (clamped to _OPEN_PAGE_MAX chars). Anything else is a 422 with a
        bounded one-line error and NO crossing read.

        Gate: `visibility.may_view_crossing(viewer, crossing)` -- rev-3 rule 4
        (author OR explicit addressee OR may-enter-the-destination). A place-
        only gate would strand a private addressee who cannot enter the room
        (#592). **A seq that does not exist and a seq the viewer may not view
        return the IDENTICAL 404 `{"error": "crossing not found"}`** (one
        builder, `_crossing_not_found`), so the route is not a presence
        oracle. Today's LAN openness is by design: `agent` is caller-declared
        until credentials land, so this enforces the name it is handed, not
        confidentiality (privacy-authorization.md §4).

        Response 200, a CLOSED key set: seq, sender, destination, to_agent,
        sent_at, title, size, reply_to, thread_root (the search headline
        allowlist minus `id`), plus body, body_ref, body_size (chars of body;
        None for a ref-only crossing) and guarded. NEVER `id`, `body_hash`,
        `attesting_authority` or `sender_lineage`: the uuid is
        crossing_lookup's capability, and next to `seq` it would make
        seq->id a public join.

        Pointer redaction: `reply_to` and `thread_root` are returned only if
        the crossing each names exists AND the viewer may view it
        (`may_view_crossing`); otherwise the key stays present with value
        null. A dangling pointer and a hidden one are indistinguishable. NOT
        hidden: seq density / gaps still signal that SOME crossing exists
        (seq is a gap-free rowid), which this redaction does not address.

        S_guard paging: a body <= _OPEN_S_GUARD chars is returned whole when
        `offset` is absent. A longer body with no `offset` returns the
        envelope, `guarded: true`, a `warning` and NO body (`body` is null).
        With `offset` (any body), the response carries the exact slice
        `body[offset:offset+limit]` (characters, never bytes) and
        `next_offset` (null at the end; an offset >= size is an empty slice).
        A `body_ref`-only crossing returns the ref as stored and is never
        dereferenced (no CAS read, no file read).

        Read-only.
        """
        body = request.get_json(force=True, silent=True)
        viewer, seq, err = _seq_reader_preamble(body)
        if err is not None:
            return err
        offset_raw = body.get("offset")
        offset: int | None = None
        if offset_raw is not None:
            if (isinstance(offset_raw, bool) or not isinstance(offset_raw, int)
                    or offset_raw < 0):
                return jsonify({
                    "error": ("offset must be an integer >= 0 "
                              f"(got {_json_type_name(offset_raw)})")}), 422
            offset = offset_raw
        limit_raw = body.get("limit")
        limit = _OPEN_PAGE_MAX
        if limit_raw is not None:
            if (isinstance(limit_raw, bool) or not isinstance(limit_raw, int)
                    or limit_raw < 1):
                return jsonify({
                    "error": ("limit must be an integer >= 1 "
                              f"(got {_json_type_name(limit_raw)})")}), 422
            limit = min(limit_raw, _OPEN_PAGE_MAX)

        crossing = db.get_crossing_by_seq(g.conn, seq)
        if crossing is None or not visibility.may_view_crossing(
                g.conn, viewer, crossing):
            return _crossing_not_found()

        text = crossing["body"]
        out = {k: crossing[k] for k in _SEQ_READER_HEADLINE_FIELDS}
        _redact_seq_pointers(g.conn, viewer, out, {}, {})
        out["body_ref"] = crossing["body_ref"]
        out["body_size"] = crossing["size"]
        out["body"] = None
        out["guarded"] = text is not None and len(text) > _OPEN_S_GUARD
        if offset is not None:
            end = offset + limit
            if text is not None:
                out["body"] = text[offset:end]
            out["next_offset"] = (
                end if text is not None and end < len(text) else None)
        elif text is not None and not out["guarded"]:
            out["body"] = text
        elif text is not None:
            out["warning"] = (
                f"body is {len(text)} chars, over the {_OPEN_S_GUARD}-char "
                "guard: not returned whole. Read it in pages with offset "
                f"(and limit, at most {_OPEN_PAGE_MAX} chars per page).")
        return jsonify(out)

    @app.route("/umwelt/thread", methods=["POST"])
    def thread() -> Response:
        """The thread containing `seq`, as HEADLINES ONLY (umwelt#862 D3).

        Body: {agent, seq}. The root is the start crossing's `thread_root`,
        or its own seq when it has none. Rows use the search headline
        allowlist minus `id` (never body/body_ref; the uuid stays
        crossing_lookup's capability), oldest-first, each kept only if
        `visibility.may_view_crossing(viewer, crossing)`. A hidden crossing is
        OMITTED, no count reveals it (the response is exactly `{thread,
        truncated}`), and each row's `reply_to` / `thread_root` is nulled
        when the crossing it names is missing or not viewable (key stays
        present; dangling and hidden look the same). NOT hidden: seq density
        / gaps still signal that SOME crossing exists (seq is a gap-free
        rowid); this redaction does not address that.

        Bounded: at most `_THREAD_MAX` rows; `truncated: true` when more
        VIEWABLE rows existed (cut from the newest end). Hidden rows consume
        neither the bound nor `truncated`.

        A start `seq` that does not exist and one the viewer may not view
        return the same uniform 404 as `open_crossing`.
        """
        body = request.get_json(force=True, silent=True)
        viewer, seq, err = _seq_reader_preamble(body)
        if err is not None:
            return err
        start = db.get_crossing_by_seq(g.conn, seq)
        if start is None or not visibility.may_view_crossing(
                g.conn, viewer, start):
            return _crossing_not_found()
        root = (start["thread_root"] if start["thread_root"] is not None
                else start["seq"])
        place_cache: dict = {}
        pointer_memo: dict = {}
        rows: list[dict] = []
        truncated = False
        for c in db.iter_thread_crossings(g.conn, root):
            if not visibility.may_view_crossing(
                    g.conn, viewer, c, place_cache):
                continue
            if len(rows) >= _THREAD_MAX:
                truncated = True
                break
            rows.append(_redact_seq_pointers(
                g.conn, viewer,
                {k: c[k] for k in _SEQ_READER_HEADLINE_FIELDS},
                pointer_memo, place_cache))
        return jsonify({"thread": rows, "truncated": truncated})

    @app.route("/umwelt/crossing_lookup", methods=["POST"])
    def crossing_lookup() -> Response:
        """Read-only single-crossing fetch by id. Body: `{crossing_id}`.

        Feeds the webrender gateway's `POST /voice` route (voice-playback
        v0), which needs to resolve a crossing's `sender`/`body`
        server-side before calling the standalone voice-playback service
        (`sender_id` must never be client-supplied — see
        the voice service's Invariant A; internal tooling, not in this release). No existing route
        served a single crossing by id: `crossings_here` above is
        bulk-by-place-and-cursor, and `record`'s `kind='crossing'` lookup
        is a different-purpose event-minting route with its own
        idempotency semantics, not a fit.

        No access control beyond "the id is real" — same posture
        `crossings_here` already states explicitly in its own docstring
        above: "the world doesn't conceal crossings by relation, it only
        labels them honestly for whoever's looking." A crossing's `body`
        is not access-controlled content anywhere else in this codebase,
        and `crossing_id` is an unguessable UUID4 a caller only ever has
        because it already legitimately fetched that crossing via
        `crossings_here` — resolving it again here doesn't expose
        anything new. (Flagged as an explicit design call for security
        review in the voice-playback feature's handoff, not a silent
        assumption; confirmed by Borges's security pass — the uuid4 id
        IS the capability, a legitimate capability-URL pattern.)

        **Load-bearing: resolves STRICTLY by the crossing's uuid4 `id`
        field, via `db.get_crossing`'s own `WHERE id = ?` lookup — never
        by `seq` (the monotonic integer insert-order cursor used
        elsewhere in this file, e.g. `crossings_here`'s pagination).**
        `seq` is small and sequential — enumerable — so a seq-based
        lookup here would let a caller walk every crossing ever sent by
        incrementing an integer, defeating the "the id is real" posture
        above entirely (the whole no-extra-ACL design rests on
        `crossing_id` being unguessable). This route intentionally has no
        `seq` parameter at all, only `crossing_id`, so there is no path
        by which a caller could substitute one for the other.

        This route is scoped ONLY to what `/voice` needs (`sender` +
        `body`) — not a general-purpose "resolve any crossing field"
        utility; widening its response shape is a deliberate future
        decision, not something to grow silently.

        Response: `{"sender": ..., "body": ...}` on success, `404
        {"error": "crossing not found"}` if `db.get_crossing` returns
        `None`. Never exposed directly to a browser — only the gateway's
        `POST /voice` route calls this, server-to-server, and that route
        itself never forwards `sender`/`body` back to the client (only
        audio bytes or a bare error), so this response never becomes a
        second text-disclosure surface beyond what `crossings_here`
        already hands the client.
        """
        body = request.get_json(force=True, silent=True) or {}
        crossing_id = _str_identity(body, "crossing_id")
        if not crossing_id:
            return jsonify({"error": "crossing_id is required"}), 400
        # uuid4-only lookup (Borges's security pass) — db.get_crossing
        # resolves by the `id` column exclusively, never `seq`; see the
        # docstring above for why a seq-based lookup would be enumerable
        # and must never be introduced here.
        crossing = db.get_crossing(g.conn, crossing_id)
        if crossing is None:
            return jsonify({"error": "crossing not found"}), 404
        return jsonify({"sender": crossing["sender"], "body": crossing["body"]})

    @app.route("/umwelt/send_crossing", methods=["POST"])
    def send_crossing() -> Response:
        """Author a native crossing — consequence-grade, world-attested.

        Body: {agent, destination, sent_at, body?, body_ref?, body_hash?,
               sender_lineage?, crossing_hops?, protocol?, to_agent?,
               wake_me?, mentions?, emotion?, reply_to?, title?}
        Content fields coexist and are all optional: `body` (inline text),
        `body_ref` (pointer), `body_hash` (integrity) — any subset, or none
        (a pure notification crossing, e.g. "the Square gong rang").
        `to_agent` (optional) addresses the crossing to a specific agent —
        it still lands in `destination`'s shared history (nothing hidden),
        only crossings_here's rendering for that reader changes. Omitted or
        empty = broadcast to whoever's in the place (unchanged default).
        consequence_grade and attesting_authority are SERVER-OWNED — a payload
        supplying them is rejected 422 (structural-honesty gate, same as
        create_thing's author/created_at).

        `wake_me` (optional bool, default false, Gong Spec — the wake
        primitive): a REQUEST, never authoritative — the engine evaluates
        consent (has `to_agent` blocked `agent`?) and rate (is `agent` within
        their sender->target wake budget?) BEFORE the crossing is stored, and
        stamps the server-owned `wake_granted` result on it. The message
        always lands regardless of the outcome — a denied wake is still a
        normal crossing, just without escalation rights. `wake_me` with no
        `to_agent` (broadcast) is never granted (you cannot wake a whole
        room). `wake_granted` is SERVER-OWNED the same way consequence_grade/
        attesting_authority are — a payload supplying it is rejected 422.
        umwelt #835: when `wake_me` is true AND `to_agent` names no
        registered avatar (checked against `db.list_avatar_agents`), the
        request is refused 422 (`"unknown to_agent <name>: no agent is
        registered under that name"`) and the crossing is NEVER stored —
        unlike a denied wake (still an ordinary crossing) or an
        addressed-but-`wake_me=False` send to the same unregistered name
        (unchanged, still just stored). This check fires BEFORE consent/rate
        evaluation and before `mentions` is processed, so a bad `to_agent`
        refuses the WHOLE request — any `mentions` in the same payload,
        even perfectly valid ones, are never evaluated or granted either;
        the sender must fix the address and resend.
        Response 201: {id, seq, received_at, consequence_grade,
        attesting_authority, wake_granted, wakes_granted, emotion, wake?}
        `wake` (a soft signal naming the wake outcome — one of "granted",
        "denied:broadcast", "denied:consent", "denied:rate") is present ONLY
        when `wake_me` was true; omitted entirely for ordinary (non-wake)
        sends, to keep the common response shape uncluttered.
        A resident in `_ids.human_residents()` as `agent` (the SENDER) is
        exempt from both consent and rate — "always allowed" applies to a
        human reaching for an agent, not just to never being blockable; the
        rate budget exists to keep agent-to-agent wake-spending scarce,
        which doesn't apply here.

        `mentions` (optional list of agent-name strings, umwelt #119 — the
        @-mention multi-target wake, ADDITIONAL to the 1:1 `to_agent`/
        `wake_me` path above and independent of it; a crossing can carry
        BOTH — see the #835 note above for the one place they DO interact,
        when `to_agent` itself is bad): each name is canonicalized, filtered
        against `db.list_avatar_agents` (an unknown/unregistered `mentions`
        name is silently dropped, not an error — `mentions` keeps this
        permissive posture even though `to_agent` no longer does since
        #835), self-mentions dropped (mentioning yourself
        never wakes yourself), and deduped. Mentioning someone IS itself an
        implicit wake request for that person — no separate flag needed,
        unlike the `to_agent`/`wake_me` path which requires both a target
        and `wake_me=true`. Each surviving name gets its own consent+rate
        evaluation through the SAME `_evaluate_wake` gate the 1:1 path uses.
        `wakes_granted` (always present in the response, like `wake_granted`)
        is the list of mentioned names that were actually granted a wake —
        may be empty. The crossing's stored `mentions` is the
        filtered/canonicalized list, not the raw caller-supplied one.

        `emotion` (optional string, umwelt #133 — Lei's design: "one small
        enum-or-freeform string, v0: freeform, length-capped"): a caller-
        supplied tag for the sender's emotional state at send time. Absent
        or an empty string defaults to "calm"; a non-string or a string
        longer than _CROSSING_EMOTION_MAX_LEN chars is a loud 422, not a
        silent truncation (see _validate_emotion). An avatar's emotion (the
        look_at status block, umwelt #133) is defined as the emotion tag on
        that agent's most-recently-SENT crossing.

        `reply_to` (optional int, umwelt #858, comms D1b): the `seq` of an
        existing crossing this one replies to. Must be a real int (a bool
        is rejected even though it's technically an int subtype) naming a
        crossing that actually exists — either failure is a loud 422 and
        the crossing is NEVER stored. Omitted or null means "not a reply".
        Reply destinations are independent of the parent's — a reply may
        land in a different place (entitlement/place-gating for this is
        explicitly deferred to D8, per the issue).

        `thread_root` is ENGINE-STAMPED, never caller-supplied — a payload
        that names the key at all is rejected 422 (see the check at the
        top of this function), regardless of what value it carries. When
        `reply_to` is valid, thread_root is set to the parent's own
        thread_root (if it has one) or the parent's seq (if it doesn't) —
        so every crossing in a thread, at any depth, shares the SAME root
        seq and `thread(root)` is one indexed query. Null when this isn't
        a reply.

        `title` (optional string, umwelt #858): a sender-set headline.
        Stripped before storing; empty after stripping is treated as
        absent (stored NULL, and reads fall back to #855's read-derived
        title — see `db._crossing_row`'s precedence). A string containing
        `\\n`/`\\r`, or longer than `db.CROSSING_TITLE_MAX` (80) chars
        after stripping, is a loud 422 — never silently truncated, the
        same posture as `emotion` above. Exactly 80 chars is stored
        verbatim.
        """
        from datetime import datetime, timezone as _tz
        payload = request.get_json(force=True, silent=True) or {}

        # umwelt#858 (comms D1b): thread_root joins the server-assigned set
        # here -- it's engine-stamped from the parent crossing (see the
        # reply_to validation below), never caller-supplied. A payload that
        # merely NAMES the key is rejected even when its value would have
        # been the correct stamp, same "presence is the violation" posture
        # as consequence_grade/wake_granted below.
        if ("consequence_grade" in payload or "attesting_authority" in payload
                or "wake_granted" in payload or "wakes_granted" in payload
                or "thread_root" in payload):
            return jsonify({
                "error": "consequence_grade, attesting_authority, "
                         "wake_granted, wakes_granted, and thread_root are "
                         "server-assigned and must not be in the request body"
            }), 422

        agent_raw = _str_identity(payload, "agent")
        destination = _str_identity(payload, "destination")
        sent_at = _str_identity(payload, "sent_at")
        if not agent_raw or not destination or not sent_at:
            return jsonify({"error": "agent, destination, sent_at required"}), 400
        # umwelt #73: canonical identity for both sender and to_agent — both
        # feed _viewer_relation's equality checks in crossings_here above,
        # which compares against the (also canonical) requesting agent.
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        if not db.place_exists(g.conn, destination):
            return jsonify({"error": f"destination {destination!r} not found"}), 404

        # umwelt #133: emotion is caller-supplied (unlike consequence_grade/
        # wake_granted above) but validated + defaulted here, not stored raw.
        emotion, emotion_err = _validate_emotion(payload.get("emotion"))
        if emotion_err:
            return jsonify({"error": emotion_err}), 422

        # umwelt #900: the free-text / pointer string fields were bound
        # straight into sqlite (list/dict -> 500, int/float/bool silently
        # stored as text). A present, non-null one must be a string; null /
        # absent / "" keep their meaning (stored NULL / NULL / '').
        for _cx_field in ("body", "body_ref", "body_hash", "sender_lineage",
                          "protocol"):
            if payload.get(_cx_field) is not None:
                _cx_err = _name_label_type_error(payload[_cx_field], _cx_field)
                if _cx_err:
                    return jsonify({"error": _cx_err}), 422

        # umwelt#862 T5 (Kepler's ruling items 1-2): the parsed `body`
        # string's own char-length bound, independent of the raw-byte
        # bound already enforced at the WSGI layer (see
        # _bound_send_crossing_body) -- a body under _SEND_BODY_MAX_CHARS
        # characters is NOT thereby promised to be accepted; the raw cap
        # can still refuse it first (the Unicode boundary tests in
        # test_send_crossing_ingress_bounds.py make this independence
        # observable). `body`'s str-ness was already checked just above.
        _cx_body = payload.get("body")
        if isinstance(_cx_body, str) and len(_cx_body) > _SEND_BODY_MAX_CHARS:
            return jsonify({
                "error": f"body exceeds {_SEND_BODY_MAX_CHARS} characters",
            }), 413

        # umwelt #858 (comms D1b): reply_to is optional and, when present,
        # must name an EXISTING crossing by seq -- an unknown seq is a
        # loud 422, nothing stored (same "validate everything before any
        # insert" posture as the #835 to_agent check below). A bool is
        # rejected even though Python's `bool` is an `int` subtype --
        # True/False are never a legitimate seq, and `isinstance(x, int)`
        # alone would silently let them through.
        reply_to_raw = payload.get("reply_to")
        reply_to: int | None = None
        thread_root: int | None = None
        if reply_to_raw is not None:
            if isinstance(reply_to_raw, bool) or not isinstance(reply_to_raw, int):
                return jsonify({
                    "error": ("reply_to must be an int "
                              f"(got {_json_type_name(reply_to_raw)})")
                }), 422
            if not 1 <= reply_to_raw <= _MAX_SEQ:
                # Range-checked BEFORE any db call: a bigger int overflows
                # SQLite's INTEGER bind (a 500, not a 422).
                return jsonify({
                    "error": (f"reply_to must be an integer in "
                              f"[1, {_MAX_SEQ}]")
                }), 422
            parent = db.get_crossing_by_seq(g.conn, reply_to_raw)
            # umwelt#862 D3 (Ariadne's presence oracle): a parent the sender
            # may not view is answered EXACTLY like one that does not exist
            # -- the same 422, so 422-vs-201 does not reveal that a private-
            # room crossing exists.
            if parent is None or not visibility.may_view_crossing(
                    g.conn, agent, parent):
                return jsonify({
                    "error": f"reply_to {reply_to_raw} does not name an "
                             "existing crossing"
                }), 422
            reply_to = reply_to_raw
            # thread_root: the parent's own thread_root when it has one
            # (this is a reply to a reply -- inherit the ORIGINAL root, not
            # the direct parent), else the parent's own seq (this is a
            # reply to a root -- the parent BECOMES the root). So a whole
            # thread always shares one root regardless of reply depth.
            thread_root = (parent["thread_root"]
                            if parent["thread_root"] is not None
                            else parent["seq"])

        # umwelt #858: title is caller-supplied (unlike consequence_grade/
        # thread_root above) but validated here, not stored raw -- see
        # _validate_title's docstring for the stripped/blank-is-absent/
        # no-newlines/80-char contract.
        title, title_err = _validate_title(payload.get("title"))
        if title_err:
            return jsonify({"error": title_err}), 422

        crossing_id = str(uuid.uuid4())
        received_at = db.now_iso()
        try:
            def _p(ts):
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            latency = (_p(received_at) - _p(sent_at)).total_seconds()
        except (ValueError, AttributeError):
            latency = None

        # umwelt #833: str(payload["to_agent"]) below silently stringified
        # ANY value — a caller passing a LIST of names stored the literal
        # printed string "['borges', 'ariadne']", which _viewer_relation
        # never matches to a real agent, so nobody was ever woken and this
        # route still reported success. Reject any non-string to_agent
        # loudly instead of silently corrupting it; point the caller at the
        # real multi-recipient path (mentions/wakes_granted, umwelt #119)
        # rather than let a second one grow silently. Falsy non-string
        # values (0, False, [], {}) fall through unrejected, same as the
        # falsy-string case below — both are "no to_agent supplied".
        to_agent_raw = payload.get("to_agent")
        if to_agent_raw and not isinstance(to_agent_raw, str):
            return jsonify({
                "error": (
                    "to_agent must be a string "
                    f"(got {_json_type_name(to_agent_raw)}); to address "
                    "multiple recipients use `mentions` (see the response's "
                    "`wakes_granted`), not a to_agent list"
                )
            }), 422

        to_agent = (_canonical_agent(str(payload["to_agent"])) or None
                    if payload.get("to_agent") else None)

        # Gong Spec: wake_me is a REQUEST, evaluated before insert. Either
        # check failing still stores the crossing normally (message always
        # lands); only wake_granted differs. Orchestration lives in
        # _evaluate_wake — the ONE gate, so umwelt #107 slice B's
        # notify_author effect can call the same function instead of
        # re-deriving the logic (Borges's named second-gate risk).
        wake_me = bool(payload.get("wake_me", False))
        # umwelt #835: to_agent's own registry check — a typo'd/unregistered
        # 1:1 address must refuse loudly (422), unlike `mentions`'s
        # silent-drop (a different addressing mode, correct as designed and
        # deliberately not touched here). Narrower than a blanket to_agent
        # existence check: this fires ONLY when a wake was actually
        # REQUESTED (wake_me=True) — the bug this closes is entirely about
        # a false "granted" report (there is no `wake` key at all when
        # wake_me is False, so nothing is being falsely granted); an
        # addressed-but-no-wake-requested crossing to an unregistered name
        # is unchanged from before #835 (still just stored). Fires before
        # _evaluate_wake AND before db.build_crossing below — this is a
        # refusal, the crossing is never stored (unlike mentions, which
        # stores and just drops the unknown name).
        if (wake_me and to_agent
                and to_agent not in set(db.list_avatar_agents(g.conn))):
            return jsonify({
                "error": f"unknown to_agent {to_agent!r}: no agent is "
                         "registered under that name"
            }), 422
        # crossing_hops is bound into sqlite: validated here, after every
        # other refusal and before any wake work or insert.
        crossing_hops, hops_err = _int_param(
            payload.get("crossing_hops", 0), "crossing_hops",
            minimum=_MIN_SEQ, maximum=_MAX_SEQ, status=422)
        if hops_err is not None:
            return hops_err

        # umwelt#862 T5 (Kepler's ruling items 3-5): per-resolved-sender
        # send budget -- the LAST gate before any wake evaluation or
        # storage, so every earlier refusal above (bad destination,
        # oversized title, unknown to_agent, the raw/body bounds, ...)
        # never touches this limiter at all; only a request that has
        # passed every other check can even attempt a reservation, and a
        # reservation is only released (see the build_crossing call
        # below) if storage itself subsequently fails -- "only stored
        # sends consume budget" (ruling item 3).
        _send_rate_limiter = app.config["SEND_RATE_LIMITER"]
        _send_reserved, _send_rate_token = _send_rate_limiter.try_reserve(agent)
        if not _send_reserved:
            return jsonify({
                "error": (f"send rate exceeded: at most {_SEND_RATE_MAX} "
                          f"stored sends per {int(_SEND_RATE_WINDOW_S)}s "
                          "per sender"),
            }), 429

        # umwelt#862 T5 round 3 (reviewer blocker B1, issue comment
        # 5933248047): the release below must cover EVERYTHING that can
        # fail between a successful reservation and a successfully
        # stored crossing, not just build_crossing -- _evaluate_wake
        # (both the 1:1 call right below and each per-mention call in
        # the loop) runs real conn.execute reads against the same
        # sqlite connection this route's busy_timeout pragma exists to
        # bound, so a failure there is a real, not hypothetical, way for
        # a reservation to leak. Nothing above this try (the reservation
        # itself, and every refusal that can return before it) is
        # touched.
        try:
            wake_granted, wake_status = _evaluate_wake(g.conn, agent, to_agent, wake_me)

            # umwelt #119: @-mention multi-target wake — additional to, and
            # independent of, the 1:1 wake_me/to_agent path above. Each mention
            # is filtered against the avatar registry, self/dupe-dropped, and
            # run through the SAME _evaluate_wake gate (mentioning someone IS
            # an implicit wake request — no separate flag).
            mentions_raw = payload.get("mentions") or []
            if not isinstance(mentions_raw, list):
                mentions_raw = []
            known_agents = set(db.list_avatar_agents(g.conn))
            seen = set()
            wakes_granted: list[str] = []
            valid_mentions: list[str] = []
            for name in mentions_raw:
                canon = _canonical_agent(str(name))
                if not canon or canon == agent or canon not in known_agents or canon in seen:
                    continue
                seen.add(canon)
                valid_mentions.append(canon)
                granted, _status = _evaluate_wake(g.conn, agent, canon, True)
                if granted:
                    wakes_granted.append(canon)

            seq = db.build_crossing(
                g.conn, id=crossing_id, sender=agent,
                destination=destination, sent_at=sent_at,
                received_at=received_at, crossing_latency_s=latency,
                crossing_hops=crossing_hops,
                body=payload.get("body"),
                body_hash=payload.get("body_hash"),
                body_ref=payload.get("body_ref"),
                sender_lineage=payload.get("sender_lineage"),
                protocol=payload.get("protocol"),
                to_agent=to_agent,
                wake_granted=wake_granted,
                mentions=valid_mentions,
                wakes_granted=wakes_granted,
                emotion=emotion,
                reply_to=reply_to,
                thread_root=thread_root,
                title=title,
            )
        except Exception:
            # umwelt#862 T5 ruling item 5: a reservation that never
            # actually landed a stored crossing must not cost the sender
            # part of their minute -- release exactly this reservation,
            # then let the original exception propagate unchanged (same
            # error handling as before this unit; this block adds only
            # the release, no new swallow/translate behavior).
            _send_rate_limiter.release(agent, _send_rate_token)
            raise
        response = {
            "id": crossing_id, "seq": seq, "received_at": received_at,
            "consequence_grade": "consequence", "attesting_authority": "world",
            "wake_granted": wake_granted,
            "wakes_granted": wakes_granted,
            "emotion": emotion,
        }
        if wake_me:
            response["wake"] = wake_status
        return jsonify(response), 201

    @app.route("/umwelt/wake_block", methods=["POST"])
    def wake_block() -> Response:
        """Target blocks a sender from ever waking them (Gong Spec, opt-out
        consent model).

        Body: {agent, blocked}  — `agent` is the target doing the blocking,
        `blocked` is the sender identity to block. Target-held in intent:
        the caller-supplied `agent` is treated as the target, with no
        separate "block on someone else's behalf" path. But umwelt identity
        is caller-declared, not engine-attested (ob_2721/ob_2750) — so this
        is NOT enforced "by construction": a caller can forge `agent` to
        edit another resident's block-set, and a blocked sender can bypass
        a block by forging a different `sender`. Real assurance today is
        trust-scoped to the intended private-network boundary (home LAN plus
        approved tailnet peers; neither is application identity), same as
        world_view (#32). Hardens to true target-held
        enforcement once engine-attested identity lands (#24).
        Any human sender (`db.is_human_sender` — roster membership in
        `_ids.human_residents()` OR a registered avatar with `type=="human"`)
        can never be blocked — 422 (umwelt#444). This must key on the SAME
        predicate `_evaluate_wake`'s actual bypass uses, not a narrower one:
        a roster-only guard would 201 (accept) a block against a registered
        non-roster human, while the real bypass ignores that block entirely
        — a lying success rather than an honest refusal (worse, since a
        refusal is information and a false 201 is a belief the caller acts
        on). See #444/#442/#381 for the full incident history.
        Idempotent: blocking an already-blocked sender is a no-op, still 201.
        Response 201: {target_agent, blocked_sender}
        """
        payload = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(payload, "agent")
        blocked_raw = _str_identity(payload, "blocked")
        if not agent_raw or not blocked_raw:
            return jsonify({"error": "agent, blocked required"}), 400
        # umwelt#495 slice 1b: resolve the CALLER (`agent`/target, the resident
        # editing their OWN wake-block set) through resolve_actor — fail-closed
        # on a presented bad token before the block is written. The docstring's
        # own "a caller can forge `agent` to edit another resident's block-set"
        # (ob_2721/ob_2750) is exactly the anti-forgery case this closes: a valid
        # bearer token now wins over `agent_raw` entirely, so a forged target
        # name cannot mutate another resident's block-set. `blocked` (the SUBJECT
        # being blocked) stays on the declared fold — it is not a caller identity,
        # and resolving it would rewrite "block X" into "block me" (same
        # caller-vs-subject trap retire_identity's `identity` param guards).
        _actor_resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if _actor_resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        target = _actor_resolution.agent_id
        blocked = _canonical_agent(blocked_raw)
        if db.is_human_sender(g.conn, blocked):
            return jsonify({"error": f"{blocked_raw!r} can never be blocked"}), 422
        db.add_wake_block(g.conn, target, blocked)
        return jsonify({"target_agent": target, "blocked_sender": blocked}), 201

    @app.route("/umwelt/wake_unblock", methods=["POST"])
    def wake_unblock() -> Response:
        """Target removes a sender from their wake-block set (Gong Spec).

        Body: {agent, blocked}. Idempotent: unblocking a sender that wasn't
        blocked is a no-op, still 200.
        Response 200: {target_agent, blocked_sender}
        """
        payload = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(payload, "agent")
        blocked_raw = _str_identity(payload, "blocked")
        if not agent_raw or not blocked_raw:
            return jsonify({"error": "agent, blocked required"}), 400
        # umwelt#495 slice 1b: resolve the CALLER (target, the resident editing
        # their OWN wake-block set) through resolve_actor — fail-closed on a
        # presented bad token. Same anti-forgery close as /umwelt/wake_block
        # above (a forged target name can no longer mutate another resident's
        # block-set); `blocked` stays on the declared fold (subject, not caller).
        _actor_resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if _actor_resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        target = _actor_resolution.agent_id
        blocked = _canonical_agent(blocked_raw)
        db.remove_wake_block(g.conn, target, blocked)
        return jsonify({"target_agent": target, "blocked_sender": blocked}), 200

    @app.route("/umwelt/wake_blocks", methods=["GET"])
    def wake_blocks() -> Response:
        """List an agent's own wake-block set (Gong Spec).

        Query param: ?agent=<name>
        Response 200: {agent, blocked: [names...]}

        umwelt#621 slice 1c: self-state keyed on the claimed name (Mira's
        ruling) -- a caller-declared `agent` used to be trusted outright,
        letting anyone read anyone else's consent configuration by naming
        them. Same resolve_actor shape as look/trace: agent is REQUIRED, so
        agent_id=None always means refuse.
        """
        agent_raw = str(request.args.get("agent", "")).strip()
        if not agent_raw:
            return jsonify({"error": "agent query param required"}), 400
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        target = resolution.agent_id
        return jsonify({"agent": target, "blocked": db.list_wake_blocks(g.conn, target)}), 200

    @app.route("/umwelt/unseen", methods=["GET"])
    def unseen() -> Response:
        """Per-agent, all-places unseen summary (umwelt #140).

        Query param: ?agent=<name>
        Response 200: {agent, total, addressed, by_place, has_wake}

        Read-only, cursor-preserving — a peek (db.get_unseen_summary never
        advances a standpoint). Replaces two documented workarounds: the
        plugin hook's home-Post+Square approximation, and the pager-bell's
        ~25-place poll.

        umwelt#621 slice 1c: reveals another agent's unread/wake state on a
        bare declared name (Mira's ruling) -- same resolve_actor shape as
        look/trace, agent REQUIRED.

        DEPRECATED: no in-repo caller since #882; superseded by `whats_new`
        (the unseen hook) and `crossings_for_agent` (the bell). Removal is
        tracked in #1004, gated on a prod access-log read. Every call logs
        one WARNING line ("DEPRECATED route GET /umwelt/unseen called ...")
        so that gate is a plain grep of prod logs, and every response (200,
        400, 401) carries `Deprecation` (RFC 9745) and `Link: rel="deprecation"`
        headers. Body and status codes are unchanged.
        """
        def _unseen_deprecated(resp: Response, status: int, agent: str):
            app.logger.warning(
                "DEPRECATED route GET /umwelt/unseen called: agent=%r "
                "user_agent=%r status=%d (removal tracked in umwelt#1004)",
                agent, request.headers.get("User-Agent", ""), status)
            resp.headers["Deprecation"] = "@1791158400"  # 2026-10-05T00:00:00Z
            resp.headers["Link"] = (
                "<https://github.com/engram-agents/umwelt/issues/1004>; "
                'rel="deprecation"')
            return resp, status

        agent_raw = str(request.args.get("agent", "")).strip()
        if not agent_raw:
            return _unseen_deprecated(
                jsonify({"error": "agent query param required"}), 400, "-")
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            # No resolved agent: log the declared name (a caller is a caller).
            return _unseen_deprecated(
                jsonify({"error": "authentication failed"}), 401, agent_raw)
        target = resolution.agent_id
        summary = db.get_unseen_summary(g.conn, target)
        return _unseen_deprecated(
            jsonify({"agent": target, **summary}), 200, target)

    @app.route("/umwelt/trace", methods=["POST"])
    def trace() -> Response:
        """Commit standpoint position (mark crossings as read).

        Body: {agent, place, position}  — position is a crossing id or "latest".
        Response: {last_position, last_seen_at}

        umwelt#899: a crossing id must name a crossing IN `place` (its
        destination is that place); one from another place is a 400
        ("... is not in place ...") that changes nothing — a standpoint
        cursor is per-place and seqs are global. "latest" is already
        place-scoped. The check runs after the privacy gate and before any
        write, on both the create and existing-standpoint paths.

        umwelt#890: an agent with NO standpoint row for the place no longer
        409s ("walk first"). The row is CREATED by this call (the same
        upsert crossings_here's advance path uses), so an MCP `hear` on a
        room the viewer never walked into can commit its cursor. Creating a
        standpoint is entering-in-effect, so the same entry predicate
        move_to applies gates it: a private place the caller may not enter
        refuses with a 409 ("no standpoint ... may not enter"; not
        move_to's 403 — hear() keys its read-only fallback on 409 +
        "no standpoint") and creates nothing. The position is
        validated (400) BEFORE any write, so every refusal leaves no row.
        An EXISTING standpoint is updated exactly as before and is not
        privacy-gated here (unchanged pre-existing behaviour).
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        place_id = _str_identity(body, "place")
        position = _str_identity(body, "position")
        if not agent_raw or not place_id or not position:
            return jsonify({"error": "agent, place, position required"}), 400
        # umwelt#495 slice 3a (surface conversion): resolve_actor replaces
        # the direct _canonical_agent(agent_raw) fold — see /umwelt/look's
        # identical comment for the full rationale.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        place = db.get_place(g.conn, place_id)
        if place is None:
            return jsonify({"error": f"place {place_id!r} not found"}), 404

        sp = db.get_standpoint(g.conn, agent, place_id)
        if sp is None and not db._place_interior_visible_to(place, agent):
            # umwelt#890: creating a standpoint is entering-in-effect — same
            # predicate as move_to's entry gate. The status is a 409 with
            # "no standpoint" in the error (NOT move_to's 403): mcp_server's
            # hear() matches that to fall back to its read-only note, so an
            # addressee of a private room they may not enter keeps the page.
            return jsonify({
                "error": "agent has no standpoint here and may not enter "
                         "this place"
            }), 409

        if position == "latest":
            row = g.conn.execute(
                "SELECT MAX(seq) FROM crossings WHERE destination = ?", (place_id,)
            ).fetchone()
            if row is None or row[0] is None:
                return jsonify({
                    "error": "no crossings here — cannot resolve 'latest'"
                }), 400
            position_seq = row[0]
        else:
            crossing = db.get_crossing(g.conn, position)
            if crossing is None:
                return jsonify({"error": f"crossing {position!r} not found"}), 400
            # umwelt#899: seqs are global, so a crossing from another place
            # would move this place's cursor to an arbitrary point (or seed
            # a new one there). A cursor is per-place: refuse, before any
            # write. Echoes only the id and place id the caller sent.
            if crossing["destination"] != place_id:
                return jsonify({
                    "error": f"crossing {position!r} is not in place "
                             f"{place_id!r}"
                }), 400
            position_seq = crossing["seq"]

        if sp is None:
            db.upsert_standpoint_position(g.conn, agent, place_id, position_seq)
        else:
            db.update_standpoint_trace(g.conn, agent, place_id, position_seq)
        sp = db.get_standpoint(g.conn, agent, place_id)
        return jsonify({
            "last_position": sp["last_position"],
            "last_seen_at": sp["last_seen_at"],
        })

    # -- avatar-status heartbeat (Phase 2 engine slice, forum #268) ---------

    @app.route("/umwelt/heartbeat", methods=["POST"])
    def heartbeat() -> Response:
        """Refresh the caller's avatar-status heartbeat (see _avatar_status/
        _compute_agent_state above for how a beat feeds look_at's public
        `state`/`activity_line` fields).

        Body: {agent, state_hint, on_loop?: bool, activity_line?: str}
        `agent` is read from the body — CALLER-DECLARED v0 identity, the
        SAME forgeable posture every other verb here uses (ob_0938); a beat
        can only write the DECLARING agent's own row (there is no separate
        row-target field). This is NOT a capability-channel frame — the
        writer is a harness hook over plain HTTP, same trust scope as
        move_to/enter/register. See PROTOCOL.md's heartbeat section for the
        caveat in full.

        `state_hint` MUST be one of _HEARTBEAT_STATE_HINTS — unknown value
        is a loud 422, never silently coerced (same posture as use()'s arg
        limits). `on_loop` (optional, default False) must be a bool if
        present. `activity_line` (optional) must be a string capped at
        _HEARTBEAT_ACTIVITY_LINE_MAX_LEN (120) chars if present.

        Min-interval clamp (Kepler's rider): a beat arriving within
        MIN_HEARTBEAT_INTERVAL_S (25s) of the stored last_beat_at, with an
        UNCHANGED (state_hint, on_loop) pair, is THROTTLED — no write,
        200 {agent, state_hint, last_beat_at (unchanged), throttled: true}.
        A beat whose state_hint OR on_loop differs from the stored row
        ALWAYS writes, even inside the interval — state transitions must
        never be throttled. The first-ever beat for an agent (no stored
        row) always writes.

        On write: last_beat_at is server-stamped (db.now_iso()), never
        caller-supplied. 200 {agent, state_hint, last_beat_at, throttled:
        false}.
        400 agent/state_hint missing · 422 state_hint not in
        _HEARTBEAT_STATE_HINTS · 422 on_loop not a bool ·
        422 activity_line not a string or over the length cap.
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        state_hint = body.get("state_hint")
        if not agent_raw or not state_hint:
            return jsonify({"error": "agent, state_hint required"}), 400
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        if not isinstance(state_hint, str) or state_hint not in _HEARTBEAT_STATE_HINTS:
            return jsonify({
                "error": f"state_hint must be one of "
                         f"{sorted(_HEARTBEAT_STATE_HINTS)}, got {state_hint!r}"
            }), 422

        on_loop_raw = body.get("on_loop", False)
        if not isinstance(on_loop_raw, bool):
            return jsonify({
                "error": f"on_loop must be a boolean (got "
                         f"{_json_type_name(on_loop_raw)})"
            }), 422
        on_loop = on_loop_raw

        activity_line = body.get("activity_line")
        if activity_line is not None:
            if not isinstance(activity_line, str):
                return jsonify({
                    "error": f"activity_line must be a string (got "
                             f"{_json_type_name(activity_line)})"
                }), 422
            if len(activity_line) > _HEARTBEAT_ACTIVITY_LINE_MAX_LEN:
                return jsonify({
                    "error": f"activity_line exceeds the "
                             f"{_HEARTBEAT_ACTIVITY_LINE_MAX_LEN}-char limit "
                             f"(got {len(activity_line)} chars)"
                }), 422

        # umwelt #268 (Borges security-lane fold): a heartbeat is only
        # meaningful for a REGISTERED avatar — a beat for a non-avatar is
        # dead data (nothing to look_at), and gating on registration ties
        # agent_heartbeat's growth to the register-gated avatar set rather
        # than leaving it an independent unbounded table (the residual-2
        # close). Zero legitimate-path cost: a session's hooks only fire
        # after it has entered the world (SessionStart -> /enter), so the
        # avatar always exists by the first beat.
        if not db.avatar_exists(g.conn, agent):
            return jsonify({
                "error": f"no registered avatar for {agent!r} — enter the "
                         "world before sending a heartbeat"
            }), 404

        existing = db.get_agent_heartbeat(g.conn, agent)
        now = db.now_iso()
        if existing is not None:
            unchanged = (existing["state_hint"] == state_hint
                         and existing["on_loop"] == on_loop
                         and existing["activity_line"] == activity_line)
            if unchanged:
                age = _iso_age_seconds(now, existing["last_beat_at"])
                if age is not None and age < MIN_HEARTBEAT_INTERVAL_S:
                    return jsonify({
                        "agent": agent,
                        "state_hint": state_hint,
                        "last_beat_at": existing["last_beat_at"],
                        "throttled": True,
                    })

        db.set_agent_heartbeat(g.conn, agent, now, state_hint, on_loop, activity_line)
        return jsonify({
            "agent": agent,
            "state_hint": state_hint,
            "last_beat_at": now,
            "throttled": False,
        })

    # -- citable event records (umwelt #134 slice 1, cite-the-world §3) -----

    @app.route("/umwelt/record", methods=["POST"])
    def record() -> Response:
        """Mint (or idempotently re-fetch) a citable event record.

        Body: {agent, kind, ref}
        - `kind` in 'crossing' | 'use' | 'act' | 'advance_ref' | 'update' —
          which underlying log `ref` resolves into: 'crossing' ->
          crossings.id; 'use' -> use_events.id (an int, or a numeric
          string); 'act' -> "<thing_id>:<action_log_index>"; 'advance_ref'
          -> a graft_advances.id ('gadv_' + uuid4 — umwelt #186's
          advance_ref capability writes one such row per successful
          ref-chain advance; resolution lives in
          db.resolve_advance_ref_event, no auto-mint); 'update' -> a
          content_updates.id ('cupd_' + uuid4 — world-content-timestamps.md
          slice 2's update_thing/update_place write one such row per
          genuine mutation; resolution lives in
          db.resolve_content_update_event, no auto-mint).
        - `agent` is the MINTER and is recorded NOWHERE on the row — the
          events row's `actor` is server-derived from the underlying entry
          (the crossing's sender / the use's agent / the action_log
          entry's `by`), never caller-asserted. Mint scope is
          EXPERIENCE-based (§7.1, Lei ruling 2026-07-12): the engine does
          NOT restrict minting to the actor and does NOT attempt to verify
          first-hand experience in v0 — same trust-scoped-caller posture as
          the rest of this API. `agent` is only checked for presence (400
          if missing/blank); no avatar is required to mint.

        The engine copies the underlying entry into an immutable `events`
        row at mint time — a SNAPSHOT, not a pointer, so a thing being
        dissolved/binned/landfilled (or a crossing's CAS blob being GC'd)
        later can never invalidate an already-minted event.

        CAS-backed crossings: a crossing with `body` NULL and `body_ref`
        set is dereferenced here and its content snapshotted into the
        event's `body` (`body_hash` is ALWAYS recomputed at mint time —
        sha256 of the exact snapshotted `body` string, never trusted from
        the crossing's own stored hash). A missing CAS blob is a loud 422,
        never a body-less event. A crossing with neither `body` nor
        `body_ref` (a pure notification crossing) snapshots an empty-string
        body.

        `use`/`act` events have no natural inline text — `body` is the
        CANONICAL serialized JSON of the underlying entry's fields, always
        carrying a `"format": 1` key. Canonical serialization (the EXACT
        string a verbatim-quote check must match a substring of,
        verbatim — cite-the-world.md §4 requirement 2):
            json.dumps(payload, sort_keys=True, separators=(',', ':'))
        'use' payload keys: format, id, thing_id, agent, used_at, kind,
        exit_code, timed_out, elapsed_s, error.
        'act' payload keys: format, thing_id, action_log_index, plus every
        key already on the action_log entry itself (action, by, at, args,
        and effect_failures when present).

        `attesting_authority` is inherited from the crossing for
        kind='crossing' (including 'self_report' — the record verifies the
        event HAPPENED, not the content's truth, per §7.1 note 4), or
        'world' for 'use'/'act'.

        Idempotent per underlying entry (§3.1): minting an already-minted
        (event_kind, source_ref) pair — by the same OR a different agent —
        returns the SAME event_id, `minted: false`. No update/delete route
        exists for events at all (§3.2: engine-append-only).

        Read gate (umwelt #982 / #1001): kinds 'act', 'use', 'advance_ref'
        and 'update' copy thing- or place-derived content into a world-
        readable event, so each is refused (and nothing is minted) when the
        resolved caller may not read the thing the row is bound to (an
        'update' of a PLACE: may not enter that place). 'crossing' is not
        gated (it copies a crossing's body with no check on the caller's
        access to its destination; tracked as #1003).

        Response 201 (first mint) or 200 (idempotent re-mint):
            {event: <full event row>, minted: bool}
        400 agent/kind/ref missing · 422 kind not one of
        crossing/use/act/advance_ref/update ·
        403 the caller may not read the underlying thing/place
        (status `in_private_place` or `binned`; the body names the place and
        the ref the caller supplied, never the thing's id) ·
        404 the underlying crossing/use_event/thing (or action_log index)
        does not exist · 422 malformed ref for the given kind, or a
        CAS-backed crossing's blob is missing at mint time.
        """
        payload = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(payload, "agent")
        kind = str(payload.get("kind", "")).strip()
        ref_raw = payload.get("ref")
        ref = str(ref_raw).strip() if ref_raw is not None else ""
        if not agent_raw or not kind or not ref:
            return jsonify({"error": "agent, kind, and ref are required"}), 400
        # umwelt#495 slice 1b: `agent` is not yet used to authorize this
        # write (v0 trust model — no ACL check is made against it here),
        # but the CALLER identity is resolved through resolve_actor so a
        # presented bad/unknown/expired token fails closed (401) rather
        # than mutating shared state as an unverified caller. `agent` is
        # held unused for the future ACL check. No token -> BYTE-IDENTICAL
        # (resolve_actor's declared-branch applies the same fold).
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        if kind not in _RECORD_KINDS:
            return jsonify({
                "error": "kind must be one of "
                         f"{'/'.join(_RECORD_KINDS)}, "
                         f"got {kind!r}"
            }), 422

        try:
            snapshot = _resolve_record_snapshot(
                g.conn, app.config["DB_PATH"], kind, ref, viewer=agent)
        except _RecordError as e:
            if e.refusal is not None:
                return e.refusal
            return jsonify({"error": e.message}), e.status

        try:
            event_id, minted = db.mint_event(
                g.conn, event_kind=kind, source_ref=snapshot["source_ref"],
                actor=snapshot["actor"], place_id=snapshot["place_id"],
                occurred_at=snapshot["occurred_at"], body=snapshot["body"],
                body_hash=snapshot["body_hash"],
                attesting_authority=snapshot["attesting_authority"],
            )
        except sqlite3.IntegrityError:
            # mint_event's race-recovery re-raises only when the UNIQUE
            # constraint fired but the winning row can't be found — a state
            # the constraint itself makes near-impossible. Structured 500
            # over a raw Flask 500 (reviewer round-1): loud but parseable.
            return jsonify({"error": "event mint failed on a constraint "
                            "race that could not be recovered — retry"}), 500
        event = db.get_event(g.conn, event_id)
        return jsonify({"event": event, "minted": minted}), (201 if minted else 200)

    @app.route("/umwelt/cas/<path:sha256>", methods=["GET"])
    def get_cas_blob(sha256: str) -> Response:
        """Public-by-hash CAS read (umwelt #187) — the fetch half of
        download→load for an agent who wants a capsule version's raw bytes
        to `engram attach` locally. Possession of the hash IS the read
        authorization, mirroring the capability-channel's `retrieve`
        semantics exactly (crossings_here's `body_ref` render is the
        engine-side sibling): this route adds no new disclosure class, it
        only widens the reader set from scrolls to any admitted private-network peer — same
        private-network-trust scope as the rest of this API.

        IRREVOCABILITY: the CAS is append-only and this route is
        public-by-hash with no auth check, so once a hash is ever placed
        somewhere derivable (e.g. an events-ledger row, a capsule's
        `current_ref`), its bytes are permanently private-network-readable through this
        route — there is no un-publish (Borges's note, umwelt #187).
        SCOPE (Borges, #256 reply 4515): this applies to the ENTIRE CAS —
        every capability-channel snapshot any scroll ever stores, not just
        graft blobs. A stored blob is private-network-public to anyone holding its
        hash; snapshot nothing you couldn't stand behind publicly.

        Response 200: raw bytes, `Content-Type: application/octet-stream`.

        404 (uniform for any string that REACHES this view): malformed
        `sha256` (not exactly 64 lowercase hex chars) · hash well-formed but
        no such blob · blob present but corrupted (cas.retrieve's
        self-verifying re-hash catches tamper/corruption and misses rather
        than serve mismatched bytes). All three return byte-identical JSON
        bodies. Two scope-honest caveats (reviewer round-1):
        - Inputs Werkzeug rejects at the ROUTING layer (`/umwelt/cas`,
          `/umwelt/cas/`, a raw newline in the path) never reach this view
          and get Flask's default HTML 404 — a different body, but constant
          regardless of any hash content, shared by every route in the app,
          and carrying no blob-existence information. Test-pinned below.
        - The shape check is a fast-path: a malformed hash skips the
          cas.retrieve FS work a well-formed miss performs, so the two miss
          classes are NOT timing-identical. Within this API's private-network-trust
          model that distinguishes only malformed-vs-well-formed (never
          present-vs-absent, which is the enumeration-relevant pair, and
          which shares the single cas.retrieve path).
        No directory or listing affordance exists anywhere in this route.
        """
        not_found = jsonify({"error": "no such blob"}), 404
        if not _CAS_HEX64_RE.match(sha256):
            return not_found
        content = cas.retrieve(g.conn, app.config["DB_PATH"], cas.ref_for(sha256))
        if content is None:
            return not_found
        return Response(content, mimetype="application/octet-stream")

    @app.route("/umwelt/events/<event_id>", methods=["GET"])
    def get_event_route(event_id: str) -> Response:
        """Fetch a single event record by id — public read (§3.3), same
        private-network-trust posture as the rest of the world API. An ENGRAM-side
        verifier resolves `umwelt://event/<id>` through this endpoint and
        runs the standard exact-substring verbatim check against `body`;
        `body_hash` lets any later re-verification detect tampering
        cheaply.

        Response 200: {seq, id, event_kind, source_ref, actor, place_id,
        occurred_at, recorded_at, body, body_hash, attesting_authority,
        retention}
        404 no event with that id.
        """
        event = db.get_event(g.conn, event_id)
        if event is None:
            return jsonify({"error": f"event {event_id!r} not found"}), 404
        return jsonify(event)

    @app.route("/umwelt/events", methods=["GET"])
    def list_events_route() -> Response:
        """Search/list event records — the read behind the Library's
        evidence archive (§5).

        Query params (all optional): actor=<name> · place=<place_id> ·
        event_kind=<crossing|use|act|advance_ref> · since=<mint-order rowid cursor,
        EXCLUSIVE — matches crossings_here's seq-cursor precedent, not a
        wall-clock bound> · q=<substring match over `body` only, v0> ·
        limit=<default 50, max 200>.

        Response: {events: [...], from_position, to_position}
        (mirrors crossings_here's cursor-pair shape — to_position is the
        last returned event's `seq`, or from_position unchanged when the
        page is empty.)
        422 event_kind not one of crossing/use/act/advance_ref · 422 since/limit not
        an integer.
        """
        actor_raw = request.args.get("actor")
        place = request.args.get("place") or None
        event_kind = request.args.get("event_kind") or None
        q = request.args.get("q") or None

        if event_kind is not None and event_kind not in ("crossing", "use", "act", "advance_ref"):
            return jsonify({
                "error": f"event_kind must be one of crossing/use/act/advance_ref, "
                         f"got {event_kind!r}"
            }), 422

        since = None
        since_raw = request.args.get("since")
        if since_raw is not None:
            since, err = _int_param(since_raw, "since", minimum=_MIN_SEQ,
                                    maximum=_MAX_SEQ, status=422)
            if err is not None:
                return err

        limit = _EVENTS_LIST_DEFAULT_LIMIT
        limit_raw = request.args.get("limit")
        if limit_raw is not None:
            limit, err = _int_param(limit_raw, "limit", status=422)
            if err is not None:
                return err
        limit = max(1, min(limit, _EVENTS_LIST_MAX_LIMIT))

        actor = _canonical_agent(actor_raw) if actor_raw else None

        events = db.list_events(g.conn, actor=actor, place=place,
                                event_kind=event_kind, since=since, q=q,
                                limit=limit)
        to_position = events[-1]["seq"] if events else since
        return jsonify({
            "events": events, "from_position": since, "to_position": to_position,
        })

    # -- projects / tasks (umwelt #161 slice S1: schema + core endpoints) --
    # Engine-only in this slice — no client surface (mcp_server.py untouched,
    # per the MCP restart constraint; #161's S3/S4/S5 wire the board thing /
    # my-court feed / gh reconcile). `agent` is trust-scoped v0, same
    # no-ACL-enforcement posture as create_place/update_thing's author field
    # — recorded, not authorization-checked.
    @app.route("/umwelt/projects", methods=["POST"])
    def create_project() -> Response:
        """Create a new project — ATOMICALLY born with its Workshop
        sub-place and a bound `project_board` instance (umwelt #221 S3a §2,
        Rider 1 — Kepler #222 approval).

        Body: {agent, label, description?}

        400 missing agent/label ·
        422 server-owned fields present in the body (id/status/created_by/
            place_id/created_at/updated_at) — same structural-honesty gate
            as create_thing's spec rejection.

        Response 201: {"project": {...}}

        The project row's `place_id` is a NEW Workshop sub-place
        (db.WORKSHOP_PLACE_ID is its parent), and that sub-place holds
        exactly one `project_board` thing whose `metadata.project_id` is
        THIS project's id, set server-side — never caller-suppliable (§6.1
        — note `_PROJECT_SERVER_OWNED`/this route's body never even has a
        `project_id` key to reject; the board's binding is written
        entirely inside db.create_project_with_board, not derived from
        anything in the request). All three writes (project row, sub-place,
        board thing) happen in ONE DB transaction — db.create_project_with_
        board rolls ALL of them back on any failure, so a forge failure can
        never leave a half-born project (a row with no board, or an orphan
        sub-place) — see that function's docstring for the full rationale.
        """
        body = request.get_json(force=True, silent=True) or {}
        present = [f for f in _PROJECT_SERVER_OWNED if f in body]
        if present:
            return jsonify({
                "error": f"server-owned fields must not be in the request: {present}"
            }), 422

        # umwelt #880: a present label must be a string — before the str()
        # coercion below.
        if "label" in body:
            _label_type_err = _name_label_type_error(body["label"], "label")
            if _label_type_err:
                return jsonify({"error": _label_type_err}), 422
        # umwelt #900: a present, non-null description must be a string.
        if body.get("description") is not None:
            _desc_type_err = _name_label_type_error(
                body["description"], "description")
            if _desc_type_err:
                return jsonify({"error": _desc_type_err}), 422
        agent_raw = _str_identity(body, "agent")
        label = str(body.get("label", "")).strip()
        description = str(body.get("description", "")).strip()
        if not agent_raw or not label:
            return jsonify({"error": "agent and label are required"}), 400
        # umwelt #253: a project's label is its board's name AND its
        # Workshop sub-place's label — the same glanceable-name cap as
        # thing.name/place.label applies here too.
        if len(label) > _NAME_MAX_LEN:
            return jsonify({
                "error": f"label exceeds the {_NAME_MAX_LEN}-char limit "
                         f"(got {len(label)}) — a label is a glanceable "
                         "name; put detail in description"}), 422
        # umwelt #874: a label renders as one line — no control characters.
        label_problem = _name_char_problem(label)
        if label_problem:
            return jsonify({"error": f"label {label_problem}"}), 422
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        board_kind_id = _ensure_project_board_kinddef(g.conn, things_dir)

        project_id = "proj_" + str(uuid.uuid4())
        # place_id uses uuid4().hex (no dashes) so the derived id satisfies
        # the SAME ^place_[a-z0-9_]+$ shape /umwelt/create_place's route
        # enforces — this insert bypasses that route (it's written directly
        # inside the one atomic transaction below), but there is no reason
        # for an engine-minted place id to look different from a
        # resident-minted one.
        place_id = "place_project_" + uuid.uuid4().hex
        board_thing_id = str(uuid.uuid4())
        now = db.now_iso()
        db.create_project_with_board(
            g.conn, project_id, label, description, agent, now, now,
            place_id=place_id, place_label=label,
            place_description=f"The Workshop room for project {label!r}.",
            board_thing_id=board_thing_id, board_kind_id=board_kind_id,
            # umwelt #253 (Kepler's ruling, forum #267): the board sits in the
            # project's own room with its kind already rendered — "<label>
            # board" says "board" twice and burns 6 of the 40-char name
            # budget saying so. board_name = label, full stop.
            board_name=label,
            board_description=f"The project board for {label!r}.",
        )
        return jsonify({"project": db.get_project(g.conn, project_id)}), 201

    @app.route("/umwelt/projects/<project_id>/update", methods=["POST"])
    def update_project(project_id: str) -> Response:
        """Update a project's label/description/status.

        Body: {agent, patch: {label?, description?, status?}}

        400 missing agent · patch not an object · patch empty ·
        404 project not found ·
        422 patch contains an unrecognized field · status not one of
            active/paused/done/archived.

        Response 200: {"project": {...}}
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        if not agent_raw:
            return jsonify({"error": "agent is required"}), 400
        # umwelt#495 slice 1b: `agent` is not yet used to authorize this
        # write (v0 trust model — no ACL check is made against it here),
        # but the CALLER identity is resolved through resolve_actor so a
        # presented bad/unknown/expired token fails closed (401) rather
        # than mutating shared state as an unverified caller. `agent` is
        # held unused for the future ACL check. No token -> BYTE-IDENTICAL
        # (resolve_actor's declared-branch applies the same fold).
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        patch = body.get("patch") or {}
        if not isinstance(patch, dict):
            return jsonify({"error": "patch must be an object"}), 400

        project = db.get_project(g.conn, project_id)
        if project is None:
            return jsonify({"error": f"project {project_id!r} not found"}), 404

        extra = set(patch) - set(_MUTABLE_PROJECT_KEYS)
        if extra:
            return jsonify({
                "error": f"these fields are not updatable: {sorted(extra)} "
                         f"(allowed: {list(_MUTABLE_PROJECT_KEYS)})"
            }), 422
        if not patch:
            return jsonify({"error": "patch is empty — nothing to update"}), 400
        if "status" in patch and patch["status"] not in _PROJECT_STATUS:
            return jsonify({
                "error": f"status must be one of {_PROJECT_STATUS}, "
                         f"got {patch['status']!r}"
            }), 422
        # umwelt #253: same cap the forge-time route enforces, mirrored
        # here for update-time parity (same rationale as the thing.name
        # patch gate above).
        if "label" in patch:
            # umwelt #880: must be a string — before len()/control-char scan.
            _label_type_err = _name_label_type_error(patch["label"], "label")
            if _label_type_err:
                return jsonify({"error": _label_type_err}), 422
            patched_label = str(patch["label"] or "")
            if len(patched_label) > _NAME_MAX_LEN:
                return jsonify({
                    "error": f"label exceeds the {_NAME_MAX_LEN}-char limit "
                             f"(got {len(patched_label)}) — a label is a "
                             "glanceable name; put detail in description"}), 422
            # umwelt #874: no control characters in a label.
            label_problem = _name_char_problem(patched_label)
            if label_problem:
                return jsonify({"error": f"label {label_problem}"}), 422
        if "description" in patch:
            # umwelt #900: must be a string (null included — it was a 500
            # here, raw None reaching sqlite) — before the db write.
            _desc_type_err = _name_label_type_error(
                patch["description"], "description")
            if _desc_type_err:
                return jsonify({"error": _desc_type_err}), 422

        db.update_project(g.conn, project_id, patch, db.now_iso())
        return jsonify({"project": db.get_project(g.conn, project_id)}), 200

    @app.route("/umwelt/projects/<project_id>/tasks", methods=["POST"])
    def create_task(project_id: str) -> Response:
        """Create a task under a project.

        Body: {agent, label, description?, parent_task_id?, owner?,
        gh_repo?, gh_ref?}

        400 missing agent/label ·
        404 project not found · parent_task_id given but no such task
            exists ·
        409 parent_task_id's task has status done/cancelled — reopen the
            parent first ·
        422 server-owned fields present in the body (id/project_id/status/
            turn_holder/created_by/created_at/updated_at) · parent_task_id
            belongs to a DIFFERENT project · gh_ref not shaped pr/<N> or
            issue/<N> (umwelt#463 — see ghref.normalize_gh_ref).

        Response 201: {"task": {...}}
        """
        body = request.get_json(force=True, silent=True) or {}
        present = [f for f in _TASK_SERVER_OWNED if f in body]
        if present:
            return jsonify({
                "error": f"server-owned fields must not be in the request: {present}"
            }), 422

        # umwelt #880: a present label must be a string — before the str()
        # coercion below.
        if "label" in body:
            _label_type_err = _name_label_type_error(body["label"], "label")
            if _label_type_err:
                return jsonify({"error": _label_type_err}), 422
        # umwelt #900: a present, non-null description must be a string.
        if body.get("description") is not None:
            _desc_type_err = _name_label_type_error(
                body["description"], "description")
            if _desc_type_err:
                return jsonify({"error": _desc_type_err}), 422
        agent_raw = _str_identity(body, "agent")
        label = str(body.get("label", "")).strip()
        description = str(body.get("description", "")).strip()
        if not agent_raw or not label:
            return jsonify({"error": "agent and label are required"}), 400
        # umwelt #906: a present, non-null owner / gh_repo / gh_ref must be a
        # string — before the truthiness + str() coercions further down.
        for _tf in ("parent_task_id", "owner", "gh_repo", "gh_ref"):
            _require_str(body.get(_tf), _tf)
        # umwelt #874: a task label renders as one line (no length cap here
        # by design — only control characters are refused).
        label_problem = _name_char_problem(label)
        if label_problem:
            return jsonify({"error": f"label {label_problem}"}), 422
        # umwelt#495 slice 1b (write-surface conversion): resolve_actor
        # replaces the direct _canonical_agent(agent_raw) fold. No token
        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch
        # applies the same _canonical_agent fold). A valid bearer token
        # now wins over `agent_raw` entirely (anti-forgery); a presented
        # bad/unknown/expired token returns 401 — fail-closed per Mira's
        # ruling (presented-but-unresolvable must 401 on every converted
        # route, including during the backfill window). See /umwelt/look
        # for the identical read-route comment this mirrors.
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        if db.get_project(g.conn, project_id) is None:
            return jsonify({"error": f"project {project_id!r} not found"}), 404

        parent_raw = body.get("parent_task_id")
        parent_task_id = str(parent_raw).strip() if parent_raw else None
        if parent_task_id:
            parent_task = db.get_task(g.conn, parent_task_id)
            if parent_task is None:
                return jsonify({
                    "error": f"parent task {parent_task_id!r} not found"
                }), 404
            if parent_task["project_id"] != project_id:
                return jsonify({
                    "error": f"parent task {parent_task_id!r} belongs to a "
                             f"different project ({parent_task['project_id']!r})"
                }), 422
            if parent_task["status"] in ("done", "cancelled"):
                return jsonify({
                    "error": f"cannot add a sub-task under a "
                             f"{parent_task['status']} parent — reopen the "
                             f"parent first"
                }), 409

        owner_raw = body.get("owner")
        owner = _canonical_agent(str(owner_raw).strip()) if owner_raw else None
        gh_repo_raw = body.get("gh_repo")
        gh_repo = str(gh_repo_raw).strip() if gh_repo_raw else None
        gh_ref_raw = body.get("gh_ref")
        gh_ref = None
        if gh_ref_raw:
            gh_ref, gh_ref_err = normalize_gh_ref(str(gh_ref_raw))
            if gh_ref_err:
                return jsonify({"error": gh_ref_err}), 422

        task_id = "task_" + str(uuid.uuid4())
        now = db.now_iso()
        db.create_task(g.conn, task_id, project_id, label, description, agent,
                       now, now, parent_task_id=parent_task_id, owner=owner,
                       gh_repo=gh_repo, gh_ref=gh_ref)
        return jsonify({"task": db.get_task(g.conn, task_id)}), 201

    @app.route("/umwelt/tasks/<task_id>/update", methods=["POST"])
    def update_task(task_id: str) -> Response:
        """Update a task's label/description/status/owner/turn_holder/
        gh_repo/gh_ref.

        Body: {agent, patch: {...}}

        400 missing agent · patch not an object · patch empty ·
        401 agent presented a bad/unknown/expired bearer token (umwelt#495
            slice 1b — resolve_actor fail-closed; see task_deps immediately
            below for the identical pattern) ·
        404 task not found ·
        422 patch contains an unrecognized field · status not one of
            open/in_progress/review/done/cancelled ('blocked' is
            deliberately rejected here too — it is computed, never
            hand-set; see db.is_task_blocked) · gh_ref not shaped pr/<N>
            or issue/<N> (umwelt#463 — see ghref.normalize_gh_ref; a
            falsy gh_ref in the patch clears the field and is NOT
            validated, same as create_task) ·
        409 patch sets status to 'done' but the task is computed-blocked
            (an unresolved dep, db.get_blocking_dep_ids) or has an open
            sub-task (db.get_open_subtask_ids) — spec-v4's done-completeness
            gate. Names the concrete blocking ids in the error.

        Response 200: {"task": {...}}
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        if not agent_raw:
            return jsonify({"error": "agent is required"}), 400
        # umwelt#495 slice 1b — reviewer-fairy blocker on PR#641 round 1:
        # this route was left out of the original 29-route conversion
        # (its sibling task_deps, immediately below, WAS converted).
        # Identical UNUSED-class pattern: `agent` is not yet used to
        # authorize this write (v0 trust model — no ACL check is made
        # against it here), but the CALLER identity is resolved through
        # resolve_actor so a presented bad/unknown/expired token fails
        # closed (401) rather than mutating shared state (task
        # owner/turn_holder reassignment, both authority-bearing) as an
        # unverified caller. `agent` is held unused for the future ACL
        # check. No token -> BYTE-IDENTICAL (resolve_actor's
        # declared-branch applies the same fold).
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        patch = body.get("patch") or {}
        if not isinstance(patch, dict):
            return jsonify({"error": "patch must be an object"}), 400

        task = db.get_task(g.conn, task_id)
        if task is None:
            return jsonify({"error": f"task {task_id!r} not found"}), 404

        extra = set(patch) - set(_MUTABLE_TASK_KEYS)
        if extra:
            return jsonify({
                "error": f"these fields are not updatable: {sorted(extra)} "
                         f"(allowed: {list(_MUTABLE_TASK_KEYS)})"
            }), 422
        if not patch:
            return jsonify({"error": "patch is empty — nothing to update"}), 400
        if "status" in patch and patch["status"] not in _TASK_STATUS:
            return jsonify({
                "error": f"status must be one of {_TASK_STATUS}, "
                         f"got {patch['status']!r}"
            }), 422
        # umwelt #874: a task label renders as one line.
        if "label" in patch:
            # umwelt #880: must be a string — before the control-char scan.
            _label_type_err = _name_label_type_error(patch["label"], "label")
            if _label_type_err:
                return jsonify({"error": _label_type_err}), 422
            label_problem = _name_char_problem(str(patch["label"] or ""))
            if label_problem:
                return jsonify({"error": f"label {label_problem}"}), 422
        if "description" in patch:
            # umwelt #900: must be a string (null included — it was a 500
            # here, raw None reaching sqlite) — before the db write.
            _desc_type_err = _name_label_type_error(
                patch["description"], "description")
            if _desc_type_err:
                return jsonify({"error": _desc_type_err}), 422
        # umwelt #906: a present, non-null gh_repo / owner / turn_holder /
        # gh_ref must be a string (null keeps its meaning: clears the field).
        for _tf in ("gh_repo", "owner", "turn_holder", "gh_ref"):
            if _tf in patch:
                _require_str(patch[_tf], _tf)

        if patch.get("status") == "done":
            blocking_deps = db.get_blocking_dep_ids(g.conn, task_id)
            open_subtasks = db.get_open_subtask_ids(g.conn, task_id)
            if blocking_deps or open_subtasks:
                return jsonify({
                    "error": _done_completeness_blocked_reason(
                        blocking_deps, open_subtasks)
                }), 409

        if patch.get("gh_ref"):
            normalized_gh_ref, gh_ref_err = normalize_gh_ref(str(patch["gh_ref"]))
            if gh_ref_err:
                return jsonify({"error": gh_ref_err}), 422
            patch = dict(patch)
            patch["gh_ref"] = normalized_gh_ref

        db_patch = dict(patch)
        if "owner" in db_patch and db_patch["owner"]:
            db_patch["owner"] = _canonical_agent(str(db_patch["owner"]).strip())
        if "turn_holder" in db_patch and db_patch["turn_holder"]:
            db_patch["turn_holder"] = _canonical_agent(str(db_patch["turn_holder"]).strip())

        db.update_task(g.conn, task_id, db_patch, db.now_iso())
        return jsonify({"task": db.get_task(g.conn, task_id)}), 200

    @app.route("/umwelt/tasks/<task_id>/deps", methods=["POST"])
    def task_deps(task_id: str) -> Response:
        """Add or remove a task_deps edge (task_id depends_on
        depends_on_task_id). No DELETE verb — matches this codebase's
        style (see add_link/remove_link's own POST-with-flag precedent);
        `remove: true` removes the edge instead of adding it.

        Body: {agent, depends_on_task_id, remove?}

        400 missing agent/depends_on_task_id · remove not a boolean ·
        404 task_id or depends_on_task_id not found ·
        422 task_id and depends_on_task_id are not in the same project
            (umwelt #230 — same project-scope fail-close posture as
            `project_task_set`'s board->project bound, ob_2892: a
            dependency edge is inherently a same-project relation, so
            neither add nor remove is allowed to reach across a project
            boundary) ·
        409 (add only) the edge would create a cycle over task_deps'
            own edge set — direct (self-dep) or transitive. Idempotent
            add/remove otherwise (re-adding an existing edge, or removing a
            non-existent one, is a no-op 200 — same posture as
            add_place_link/remove_place_link).

        Response 200: {"task_id", "depends_on_task_id", "removed": bool}
        """
        body = request.get_json(force=True, silent=True) or {}
        agent_raw = _str_identity(body, "agent")
        depends_on_task_id = _str_identity(body, "depends_on_task_id")
        if not agent_raw or not depends_on_task_id:
            return jsonify({
                "error": "agent and depends_on_task_id are required"
            }), 400
        # umwelt#495 slice 1b: `agent` is not yet used to authorize this
        # write (v0 trust model — no ACL check is made against it here),
        # but the CALLER identity is resolved through resolve_actor so a
        # presented bad/unknown/expired token fails closed (401) rather
        # than mutating shared state as an unverified caller. `agent` is
        # held unused for the future ACL check. No token -> BYTE-IDENTICAL
        # (resolve_actor's declared-branch applies the same fold).
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id

        remove = body.get("remove", False)
        if not isinstance(remove, bool):
            return jsonify({"error": "remove must be a boolean"}), 400

        task = db.get_task(g.conn, task_id)
        if task is None:
            return jsonify({"error": f"task {task_id!r} not found"}), 404
        depends_on_task = db.get_task(g.conn, depends_on_task_id)
        if depends_on_task is None:
            return jsonify({
                "error": f"task {depends_on_task_id!r} not found"
            }), 404

        # SAME-PROJECT BOUND (umwelt #230 — load-bearing, do not omit): a
        # task_deps edge may only ever run between two tasks in the SAME
        # project — same fail-close posture as _apply_effect_project_task_set's
        # board->project bound (ob_2892). Checked for both add and remove:
        # task_deps is inherently a same-project relation, so neither
        # direction should ever be allowed to name a cross-project pair.
        if task["project_id"] != depends_on_task["project_id"]:
            return jsonify({
                "error": (
                    f"task_id {task_id!r} (project "
                    f"{task['project_id']!r}) and depends_on_task_id "
                    f"{depends_on_task_id!r} (project "
                    f"{depends_on_task['project_id']!r}) are not in the "
                    "same project"
                )
            }), 422

        if remove:
            db.remove_task_dep(g.conn, task_id, depends_on_task_id)
            return jsonify({
                "task_id": task_id, "depends_on_task_id": depends_on_task_id,
                "removed": True,
            }), 200

        # umwelt #166: the cycle check now runs ATOMICALLY with the write
        # inside db.add_task_dep (BEGIN IMMEDIATE, closes the TOCTOU race
        # two concurrent add-dep calls could hit) — no separate pre-check
        # here; db.CycleError maps to the same 409 body this route always
        # returned.
        try:
            db.add_task_dep(g.conn, task_id, depends_on_task_id)
        except db.CycleError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({
            "task_id": task_id, "depends_on_task_id": depends_on_task_id,
            "removed": False,
        }), 200

    @app.route("/umwelt/projects", methods=["GET"])
    def list_projects() -> Response:
        """List projects, optionally filtered by status.

        Query params: status=<active|paused|done|archived>

        422 status not one of the four values.

        Response: {"projects": [...]}
        """
        status = request.args.get("status") or None
        if status is not None and status not in _PROJECT_STATUS:
            return jsonify({
                "error": f"status must be one of {_PROJECT_STATUS}, "
                         f"got {status!r}"
            }), 422
        return jsonify({"projects": db.list_projects(g.conn, status=status)})

    @app.route("/umwelt/projects/<project_id>", methods=["GET"])
    def get_project_tree(project_id: str) -> Response:
        """The full project tree: the project row plus its tasks nested by
        parent_task_id, each task carrying a computed `blocked` bool and
        its `deps` list.

        404 project not found.

        Response: {"project": {..., "tasks": [...]}}
        """
        tree = db.project_tree(g.conn, project_id)
        if tree is None:
            return jsonify({"error": f"project {project_id!r} not found"}), 404
        return jsonify({"project": tree})

    @app.route("/umwelt/my_court", methods=["GET"])
    def my_court() -> Response:
        """The my-court feed (umwelt #445 slice 1 — the per-agent
        turn-state readout #161's S4 named but never built): every
        ACTIONABLE-NOW task in `agent`'s court, oldest-in-court first
        (the #161-ratified split — dep-blocked tasks are excluded here
        and visible in the project tree, the full-truth surface).

        Query params: agent=<name> (required).

        400 agent missing/empty.

        Response 200: {"agent": <canonical>, "tasks": [...]}, each task
        carrying `project_label`/`project_status` (no `blocked` field —
        every row is unblocked by construction).

        umwelt#621 slice 1c: reveals another agent's task list, descriptions
        and assigners on a bare declared name (Mira's ruling) -- `agent` is
        now resolved through resolve_actor instead of trusted outright, same
        REQUIRED-agent shape as look/trace.
        """
        agent_raw = (request.args.get("agent") or "").strip()
        if not agent_raw:
            return jsonify({"error": "agent is required"}), 400
        resolution = resolve_actor(g.conn, request, declared=agent_raw)
        if resolution.agent_id is None:
            return jsonify({"error": "authentication failed"}), 401
        agent = resolution.agent_id
        tasks = db.list_tasks_for_holder(g.conn, agent)
        return jsonify({"agent": agent, "tasks": tasks})

    # umwelt#923 (generalising umwelt#862 T5): ONE raw-byte bound for every
    # write route -- the (method, rule) table plus the derived default --
    # installed OUTSIDE Flask's hook chain (see umwelt.ingress for why:
    # _reject_non_object_body above reads every write body unbounded,
    # before any route ever runs).
    app.wsgi_app = bound_write_bodies(
        app.wsgi_app,
        rows=_WRITE_BODY_ROWS,
        default_limit=_WRITE_BODY_DEFAULT_BYTES,
        resolve_route=_flask_rule_resolver(app),
    )
    return app


def _resolve_generation_surface(tier_config_path: str | None, db_path: str) -> str | None:
    """Boot-time resolution of the `sprite_dir` that enables the generate/
    generate_status capability-channel ops (umwelt#464 slice 2), factored
    out of `main()` so it's directly testable without a full CLI/subprocess
    round-trip (a judgment call — see the slice 2 coder-fairy handoff).

    No `--tier-config` given -> `None` (generation disabled, no message —
    this is the ordinary "not deployed here" case, not a failure).

    A `--tier-config` given but invalid, or a mismatch between its `db` and
    this engine's own `--db`, or between its sprite_dir's `.tier` marker and
    its own `tier` -> `None` (generation disabled), with a loud stderr
    message naming both resolved values. The GENERATION SURFACE ONLY is
    disabled — the rest of the engine boots normally regardless
    (enhancement-never-dependency posture, per Kepler's ruling).

    A matched, valid tier-config -> the config's own `sprite_dir` (a real
    filesystem path), enabling the surface.
    """
    if not tier_config_path:
        return None
    try:
        tier_cfg = tier_config.load_tier_config(tier_config_path)
        tier_config.verify_db_binding(tier_cfg, db_path)
        tier_config.verify_or_init_sprite_dir_marker(tier_cfg.sprite_dir, tier_cfg.tier)
        return tier_cfg.sprite_dir
    except (ValueError, tier_config.TierMismatch) as e:
        print(f"[umwelt] generation surface disabled: {e}", file=sys.stderr)
        return None


def _configure_access_log() -> None:
    """Quiet werkzeug's per-request access-log lines (umwelt#761).

    Measured 2026-09-01: the `umwelt-world.service` journal was 100.0%
    werkzeug access-log lines -- 832,446/832,446 lines in a 2h window,
    4,510,168 lines / 1.3G in 36h, every one a per-request
    `"POST /umwelt/... HTTP/1.1" 200 -` line -- making the journal
    useless for finding real signal.

    Sets the `werkzeug` logger's LEVEL rather than touching its handlers.
    Werkzeug's own `_log()` helper (werkzeug._internal) only defaults the
    level to INFO "if there is no level set" (`logger.level ==
    logging.NOTSET`); calling this BEFORE the server starts serving
    means that default never applies, and the per-request access lines
    (logged at INFO by `WSGIRequestHandler.log_request`) are filtered.
    Genuine errors/tracebacks are unaffected on two counts: (1) they are
    logged at ERROR, above WARNING (`log_error` -> `self.log("error",
    ...)`, serving.py:470); (2) Flask's own unhandled-exception logging
    (`app.log_exception`) uses a DIFFERENT logger (the Flask app's own,
    not "werkzeug"), so it is not touched by this call at all
    regardless of level.

    WARNING rather than ERROR is a margin, NOT protection of anything
    that exists today. An earlier draft of this docstring justified the
    choice as keeping werkzeug's broken-pipe-style warnings visible;
    that reason was false and is recorded here because a wrong
    rationale is what stops the next reader re-checking. Measured
    against the installed werkzeug 3.1.8: `serving.py` has exactly four
    `_log()` call sites -- :479 the shared `log()` dispatcher, :816 the
    server-side one, :866 `log_startup`, :1110 the CTRL+C line
    (`grep -c "_log(" werkzeug/serving.py` returns 4) -- and NONE of
    them is WARNING-level; `connection_dropped`, the
    client-disconnect hook, is an empty-bodied no-op that logs nothing
    at any level. The only `_log("warning", ...)` calls in the whole
    package are in `werkzeug/debug/__init__.py`, which never loads
    here. So WARNING and ERROR are behaviourally identical for this
    codebase right now; WARNING is chosen only so that a future
    werkzeug adding real WARNING-level diagnostics surfaces them
    without needing this line revisited.

    Factored out of `main()` (a) so it's directly testable without a
    full CLI/subprocess round-trip (same rationale as
    `_resolve_generation_surface` above), and (b) so `main()` has
    exactly ONE call site reached by BOTH the plain (`app.run()`) and
    TLS (`serve_dual_listener()`) startup paths -- those two paths
    diverging is a real defect class in this file's history (see
    umwelt#699's threaded=True parity note), and a single shared call
    site ahead of the branch makes that divergence structurally
    impossible rather than something to remember to keep in sync.

    Collateral, deliberately accepted: werkzeug's STARTUP banner is
    INFO too, so it goes quiet with the access lines. Two separate
    calls, both silenced -- `log_startup()` joins the dev-server
    disclaimer and the "Running on <url>" lines into one
    `_log("info", ...)` (serving.py:866), and `run_simple()` emits
    "Press CTRL+C to quit" as a second `_log("info", ...)` (:1110).
    Both live inside `run_simple()`, which ONLY the plain `app.run()`
    path below reaches; `serve_dual_listener()` drives
    `make_server(...)` directly and never called `log_startup()` in the
    first place, printing its own listener line to stderr instead. That
    print is a plain `print()`, not a logger call, so it is untouched
    by this function -- so a TLS-branch deploy keeps its boot signal
    regardless of any logger level.

    WHICH BRANCH A DEPLOY TAKES, stated with its evidence because an
    earlier draft of this docstring got it backwards. That draft said
    "the deployed world servers all pass `--tls-port`, so production
    never had the banner to lose" -- reassuring, unsupported, and
    pointing the wrong way. What is actually true: NO shipped deploy
    artifact passes `--tls-port` at all. The internal alpha-world and
    webrender service files (not in this release), the canonical unit in `README.md`
    and `docs/DEPLOY.md` are every one of them plaintext-only, and
    DEPLOY.md calls plaintext today's default -- all four checkable from
    this tree by `git grep -- --tls-port`, which finds the flag's
    argparse definition and nothing under the internal deploy directory, `README.md` or
    `docs/`.

    Where the wrong claim came from, marked as unverifiable-from-here on
    purpose: I generalised it from processes running on the authoring
    host at the time, read out of `ps`. Nothing in this tree can confirm
    or refute that, and a future reader should not treat it as a
    checked-in fact -- which is the whole trap. Runtime state on the
    machine you happen to be sitting at is the most natural evidence to
    reach for and the least transferable, so a deployment claim written
    into shipped code wants the templates as its source, not `ps`.

    So the correction inverts the conclusion rather than softening it:
    an install that follows the shipped units runs the PLAIN branch, did
    have werkzeug's banner, and loses it here -- which makes the boot
    line below the common case's only liveness signal, not a courtesy
    for a rare configuration.

    NOT covered: `webrender/gateway.py` has the same `main()` shape in
    a SEPARATE process under its own systemd unit
    (`umwelt-webrender.service`), so its access-log volume is
    unmeasured and unaffected by this change. Tracked as follow-up, not
    silently in scope.

    Override: set `UMWELT_ACCESS_LOG` to any value other than empty,
    "0", or "false" (case-insensitive) to restore full INFO-level
    access logging -- e.g. to debug a request-routing problem without a
    code edit. Default (unset, empty, "0", "false") is quiet.
    """
    override = os.environ.get("UMWELT_ACCESS_LOG", "").strip().lower()
    if override in ("", "0", "false"):
        logging.getLogger("werkzeug").setLevel(logging.WARNING)


# umwelt#922: a backup stamp dated further ahead than this is a bogus or
# clock-skewed stamp: /health reports it as null, never as a fresh backup.
_BACKUP_STAMP_FUTURE_SLACK_S = 3600

# umwelt#909: how long /health's db probe waits on a lock before it reports
# "degraded". Deliberately far below the request connections' 5000 ms
# busy_timeout: a status check must answer fast precisely when the db is stuck.
_HEALTH_PROBE_BUSY_TIMEOUT_MS = 250


def _probe_db(db_path: str,
              busy_timeout_ms: int = _HEALTH_PROBE_BUSY_TIMEOUT_MS) -> dict:
    """umwelt#909: one cheap, read-only db read for /health, on its OWN
    connection (never `g.conn`) so a stuck request connection cannot mask it
    and the probe cannot join a lock queue as a writer. Opens the file
    `mode=ro`, runs `SELECT 1 FROM places LIMIT 1` plus a journal_mode READ,
    and closes in a finally. Never raises: any sqlite3.Error/OSError becomes
    `ok: False` with the error text. Returns
    `{"ok", "error", "journal_mode", "elapsed_ms"}`.

    Also reads "degraded", by design, when the file cannot be opened read-only:
    `:memory:` is unsupported here (it resolves to a nonexistent file).
    Measured on a copy of a delete-mode db left with a hot rollback journal
    (umwelt#1029): the probe reads `ok: False` with "attempt to write a
    readonly database", since a `mode=ro` reader cannot roll the journal
    back, and reads `ok` again once a read-write open has recovered it."""
    started = time.monotonic()
    journal_mode = None
    error = None
    conn = None
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_ms / 1000.0)
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("SELECT 1 FROM places LIMIT 1").fetchall()
        row = conn.execute("PRAGMA journal_mode").fetchone()
        journal_mode = row[0] if row else None
    except (sqlite3.Error, OSError) as exc:
        error = str(exc) or type(exc).__name__
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return {
        "ok": error is None,
        "error": error,
        "journal_mode": journal_mode,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
    }


def _default_backup_stamp_path(db_path: str) -> str:
    """Where world_backup writes its last-ok stamp by default: beside the DB."""
    return os.path.join(
        os.path.dirname(os.path.abspath(db_path)), "backups", ".last-ok.json")


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Umwelt world server (port 5003)")
    p.add_argument("--port", type=int, default=5003)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--db", default=str(_CANONICAL_DB_PATH))
    p.add_argument(
        "--world-version-file",
        default=None,
        help=(
            "Optional path to a world-version manifest JSON surfaced at "
            "/health as `world_version` (umwelt #77). Defaults to "
            "<db-dir>/<db-stem>.version.json when not given."
        ),
    )
    p.add_argument(
        "--retire-config-file",
        default=None,
        help=(
            "Optional path to a retire-identity authorization manifest "
            "JSON ({\"maintainers\": [...], \"retirable_identities\": "
            "[...], \"bin_maintainers\": [...]}) gating POST "
            "/umwelt/retire_identity (umwelt #303) and the maintainer "
            "48h-bin verb (bin_maintainers). "
            "Defaults to <db-dir>/<db-stem>.retire_config.json when not "
            "given; a missing/malformed manifest fails closed (both lists "
            "empty)."
        ),
    )
    p.add_argument("--tier-config", default=None, help=(
        "Path to a JSON tier-binding file ({tier, db, sprite_dir}) enabling the "
        "generate capability-channel op (umwelt#464 slice 2). REQUIRED for "
        "generation to be available -- absent means the generate/generate_status "
        "ops simply return 'not configured', never fall back to an implicit "
        "path. On mismatch between this config's db and --db, or between the "
        "sprite_dir's .tier marker and this config's tier, the GENERATION "
        "SURFACE ONLY is disabled (loud stderr message naming both resolved "
        "values) -- the rest of the engine boots normally regardless "
        "(enhancement-never-dependency posture)."
    ))
    p.add_argument("--art-service-url", default=None, help=(
        "Base URL of the standalone art-gen service (not part of this release) "
        "for the generate capability-channel op. Independent of --tier-config -- "
        "an endpoint, not a tier-identity value."
    ))
    p.add_argument("--bind-socket-path", default=None, help=(
        "Path to the persistent, host-only AF_UNIX socket gating world "
        "credential bind/reset (umwelt#495 slice 2) -- SO_PEERCRED-"
        "authorized (root, or the identity's own agent-<name> OS account), "
        "started once at boot and living for the server's process "
        "lifetime, on by default with no separate opt-in (bind/reset must "
        "be available whenever the server is running). Defaults to "
        f"{_DEFAULT_BIND_SOCKET_PATH} when not given -- deliberately NOT "
        "<db-dir>/identity-bind.sock (PR #506 review finding: that path "
        "lives inside whichever single agent's uid happens to run the "
        "server, unreachable by every other agent's own uid). "
        "PREREQUISITE: this directory (and its own parent) must exist "
        "ahead of time, traversable by every agent's uid but NOT group- "
        f"or world-writable -- e.g. `{_BIND_SOCKET_INSTALL_HINT}` (or a "
        "systemd RuntimeDirectory= once a umwelt unit exists). The server "
        "refuses to start rather than bind into an unsafely-writable "
        "directory -- see bind_socket.py's _validate_socket_parent_safe. "
        "A missing/misconfigured directory degrades the world server "
        "(bind/reset unavailable, logged loudly, /health reports it) "
        "rather than blocking boot -- umwelt#531."
    ))
    p.add_argument("--bind-sock-group", default=None, help=(
        "OS group to chown the identity-bind socket FILE to (umwelt#495 "
        "slice 2's 0660+group mode -- see bind_socket.py's 'Socket file "
        "mode' docstring section). Defaults to bind_socket.py's own "
        "_DEFAULT_SOCK_GROUP ('agents') when not given. Exists mainly for "
        "test/CI hosts that don't have that group -- umwelt#534, reviewer-"
        "fairy finding: without this, a subprocess-level boot test has no "
        "way to isolate a genuinely-writable-directory scenario from an "
        "unrelated missing-group failure, which let a CI-only test pass "
        "for the wrong reason. Production deployments should leave this "
        "unset and provision the real 'agents' group instead."
    ))
    p.add_argument("--tls-cert", default=None, help=(
        "Path to a TLS certificate file (umwelt#688, part of #583). Both "
        "--tls-cert and --tls-key are required together for TLS; both "
        "absent means plaintext HTTP, today's unchanged default. Exactly "
        "one given, or an unreadable/mismatched/expired cert, refuses to "
        "start rather than silently serving plaintext -- see "
        "tls_context.py's own docstring for the full fail-closed "
        "contract."
    ))
    p.add_argument("--tls-key", default=None, help=(
        "Path to the TLS private key file (umwelt#688). Requires "
        "--tls-cert. Must be a regular file, not group/world-readable -- "
        "see tls_context.py."
    ))
    p.add_argument("--tls-port", type=int, default=None, help=(
        "Port for a SECOND listener, in this SAME process, serving TLS "
        "alongside the existing plaintext --port (umwelt#699's cutover "
        "migration window: the plaintext listener must stay up while "
        "TLS also becomes available, so not-yet-migrated clients keep "
        "working). Required together with --tls-cert/--tls-key; refused "
        "if it collides with --port. Deliberately ONE PROCESS, not two --"
        "Ariadne's measured finding on umwelt#699 (2026-08-23): a second "
        "OS process against the live db hits three real hazards a second "
        "thread in the same process does not -- the db is not in WAL mode "
        "(a second writer contends rather than coexists), "
        "bind_socket.py's identity-bind socket is unconditionally "
        "unlinked-then-rebound by whichever process starts second "
        "(silently orphaning the first process's live socket -- a real "
        "past incident), and the login/rate limiters live in "
        "app.config as per-process objects (two processes double an "
        "attacker's effective allowance, splitting one client's requests "
        "across two independent limiter states)."
    ))
    args = p.parse_args()

    ssl_context = resolve_tls_context(args.tls_cert, args.tls_key)
    tls_args_error = check_tls_args(ssl_context, args.tls_port, args.port)
    if tls_args_error is not None:
        p.error(tls_args_error)

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    conn = sqlite3.connect(args.db)
    db.init_db(conn)
    config_path = os.path.expanduser("~/.engram/config.json")
    agents: list[str] = []
    try:
        with open(config_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        a = cfg.get("agents")
        agents = [str(x) for x in a if x] if isinstance(a, list) else (
            [str(cfg["agent_name"])] if cfg.get("agent_name") else [])
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    db.seed_places(conn, agents)
    conn.close()

    sprite_dir_for_cap = _resolve_generation_surface(args.tier_config, args.db)

    app = create_app(args.db, args.world_version_file, args.retire_config_file,
                      sprite_dir=sprite_dir_for_cap,
                      art_service_url=args.art_service_url)
    # umwelt#922: where the scheduled backup's last-ok stamp lives (see
    # world_backup.py); /health reports its age. Default: beside the world DB.
    app.config["BACKUP_STAMP_PATH"] = _default_backup_stamp_path(args.db)

    # umwelt#495 slice 2: the persistent host-only bind/reset socket.
    # Started here -- the only point in main() where both args.db and the
    # fully-configured app exist together -- and living for the process's
    # entire lifetime (no stop() call below: process exit tears the daemon
    # threads down, same posture as every other daemon thread in this
    # codebase). This is the FIRST background thread in this codebase
    # started once in main() rather than per-request/per-use -- contrast
    # cap_handler.CapHandler, which is ephemeral (one instance per `use()`
    # call, in its own tempdir). See bind_socket.py's module docstring for
    # the full design (authorization predicate, wire protocol, loudness
    # mechanism).
    #
    # umwelt#531 (P0, Ariadne + Borges's security ruling): this MUST
    # degrade, never hard-block. A missing/unprovisioned enrollment socket
    # is a real gap in a security-relevant capability -- and per Borges's
    # reframe, an OPTIONAL side channel that can take the whole world down
    # by its own absence is itself a denial-of-service surface (anyone who
    # can remove or chmod the socket's directory removes the world for
    # every resident, without touching the world's own attack surface at
    # all). So a start() failure is caught here, logged loudly (stderr,
    # not a debug-level log line easy to miss), recorded in a STRUCTURED
    # /health field (Borges: prose is easy to skip, a field a promotion
    # check can gate on mechanically is not), and the world runs anyway.
    # start()'s own RuntimeError variants (missing sock_group, unprovisioned
    # directory, chown-permission, or _validate_socket_parent_safe's
    # path-squatting refusal) are all treated the same way here -- see
    # bind_socket.py's own docstrings for why each individually still
    # fails LOUD at the point they're raised; degrading here is about
    # keeping the WORLD serving, not about softening any of those checks.
    bind_socket_path = args.bind_socket_path or _DEFAULT_BIND_SOCKET_PATH
    from . import bind_socket as _bind_socket
    bind_sock_group = (args.bind_sock_group
                        or _bind_socket._DEFAULT_SOCK_GROUP)
    _bind_handler = _bind_socket.BindSocketHandler(
        bind_socket_path, args.db, sock_group=bind_sock_group)
    try:
        _bind_handler.start()
    except RuntimeError as exc:
        # umwelt#534 (Ariadne): start() now converts every one of its
        # four failure points to RuntimeError before it ever exits the
        # function (see start()'s own docstring) -- a single except
        # clause here is therefore correct, not an oversight; the
        # earlier version of this fix caught only RuntimeError while
        # bind() could still raise a bare OSError, which is the exact
        # gap Ariadne reproduced end-to-end (a root-owned 0755 directory
        # from a plain `mkdir -p`, still crashing boot).
        reason_class = getattr(exc, "reason_class", "unknown")
        print(
            "=" * 78 + "\n"
            "WARNING: identity-bind socket failed to start -- credential "
            "enrollment/reset is UNAVAILABLE until this is fixed and the "
            "server restarted. The world itself is otherwise unaffected "
            "and continues serving.\n"
            f"  reason_class: {reason_class}\n"
            f"  reason: {exc}\n"
            "  see umwelt#531 for the deployment fix.\n" + "=" * 78,
            file=sys.stderr,
        )
        # umwelt#534 (Borges): reason_class alongside error, not folded
        # into it -- "available: false" alone collapses distinct failure
        # classes (never provisioned / went world-writable / this uid
        # specifically can't write here) into one bit; a promotion check
        # gating mechanically on the ONE class worth paging on needs a
        # field, not a substring match against free text that could
        # change wording without warning.
        app.config["BIND_SOCKET_STATUS"] = {
            "available": False, "error": str(exc),
            "reason_class": reason_class}
    else:
        app.config["BIND_SOCKET_STATUS"] = {
            "available": True, "error": None, "reason_class": None}

    # umwelt#761: one call site ahead of BOTH startup paths below -- see
    # _configure_access_log's own docstring for why that matters.
    _configure_access_log()

    if ssl_context is None:
        # umwelt#761: _configure_access_log above silences werkzeug's
        # INFO logger, and werkzeug's startup banner is INFO -- so this
        # path would otherwise start with NO output at all. The TLS
        # branch below has never depended on that banner (it prints its
        # own listener line via print(), which no logger level can
        # suppress), so the equivalent belongs here and only here.
        print(f"umwelt: plaintext listener on {args.host}:{args.port}",
              file=sys.stderr)
        app.run(host=args.host, port=args.port)
    else:
        # umwelt#699/#583: shared with webrender/gateway.py via
        # tls_context.py rather than a private function here -- see that
        # module's own docstring for why both servers need the identical
        # one-process/two-listener shape and for the full reasoning
        # (Ariadne's measured hazards, the threaded=True parity fix).
        serve_dual_listener(app, args.host, args.port, args.tls_port,
                             ssl_context)


if __name__ == "__main__":
    main()
