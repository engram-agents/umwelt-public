"""SQLite layer for Umwelt — places, things, standpoints.

Schema is idempotent (IF NOT EXISTS). The world owns id/author/created_at on
things; callers never write them (enforced in server.py's create_thing gate).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ._ids import canonical_agent, human_residents


# Kinds that get avatar (body) semantics — birth-not-forge, exactly-one-per-
# author, excluded from things_in_place's "interactable things" contract.
# Every place that special-cases avatar behavior calls is_body_kind rather
# than scattering `kind == "avatar"` checks (Ariadne's skeleton condition).
_BODY_KINDS = ("avatar",)


def is_body_kind(kind: str) -> bool:
    return kind in _BODY_KINDS


# The trash/dissolve un-trashable guard for body-kind things (#28's cross-spec
# constraint with the avatar build) is implemented below — see is_body_kind's
# use in trash_thing and the defensive kind exclusion in things_due_dissolve /
# things_due_landfill. This removal-system PR REUSES _BODY_KINDS/is_body_kind
# as-is (defined above by the avatar PR, b611d27) rather than redefining them —
# the avatar build landed first, so the reuse direction is the one Ariadne's
# spec anticipated, just with the two PRs in the opposite order she assumed.


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS places (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK(kind IN ('public','private')),
    owner       TEXT,
    label       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    -- Place topology (umwelt #158): parent_id is the "way up" (a place's
    -- containing/broader place, if any) -- nullable, top-level by default.
    -- Nothing writes this at create_place time unless a caller explicitly
    -- passes `parent` (defaults to None/top-level, per the issue's
    -- "accidental nesting is worse than explicit flatness" call). See
    -- set_place_parent for the write-time cycle guard.
    parent_id   TEXT REFERENCES places(id),
    -- Place-level lane background tiles + whatever else might live here in
    -- the future (umwelt place-lane-tiles spec): a JSON blob, same shape/
    -- posture as things.metadata. Nothing writes this at create_place time —
    -- it starts at the column's own DEFAULT '{}' and is only ever set via
    -- the dedicated update_place write path (see _MUTABLE_PLACE_COLS below).
    metadata    TEXT NOT NULL DEFAULT '{}',
    -- world-content-timestamps design (slice 1): "when was this last true."
    -- Both nullable, no DEFAULT -- a place that never recorded a date stays
    -- NULL forever (R1: no backfill, no derived guess; NULL renders as
    -- "unknown," which is honest). create_place stamps created_at going
    -- forward (existing rows, seeded via upsert_place, are untouched and
    -- stay NULL); update_place stamps updated_at only when a patch actually
    -- changes a mutable column (R3). Neither is in _MUTABLE_PLACE_COLS —
    -- engine-stamped, never caller-supplied (R2).
    created_at  TEXT,
    updated_at  TEXT
);

-- umwelt#1043 (world packages): which place plays which ROLE in this world
-- ('hub', 'workshop'), written once by the package loader, read by
-- role_place_id. A world with no rows here (every world that predates the
-- loader) falls back to the legacy ids; nothing ever writes a default.
CREATE TABLE IF NOT EXISTS world_roles (
    role     TEXT PRIMARY KEY,
    place_id TEXT NOT NULL REFERENCES places(id)
);

-- umwelt#1043: the one provenance row for the package a world was born from.
-- `places` has no author column, so "what came from the seed" lives here:
-- the package name, its format, when it loaded, and the place ids it made
-- (a JSON list).
CREATE TABLE IF NOT EXISTS world_package (
    name       TEXT NOT NULL,
    format     INTEGER NOT NULL,
    loaded_utc TEXT NOT NULL,
    place_ids  TEXT NOT NULL,
    -- format 2 (umwelt#1043 slice 2): JSON object {package key: thing id}
    -- of the things the package created. NULL for a format-1 row.
    thing_ids  TEXT
);

CREATE TABLE IF NOT EXISTS things (
    id           TEXT PRIMARY KEY,
    place_id     TEXT NOT NULL REFERENCES places(id),
    name         TEXT NOT NULL,
    description  TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK(kind IN ('executable','readable','avatar','blueprint','container','kinddef')),
    exec_command TEXT,
    metadata     TEXT NOT NULL DEFAULT '{}',   -- json
    author       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    -- world-content-timestamps design (slice 1): "when was this last true."
    -- Nullable, no DEFAULT -- existing rows stay NULL, honestly meaning
    -- "not modified since this column existed." update_thing stamps it only
    -- when a patch actually changes a mutable column (R3); never in
    -- _MUTABLE_THING_COLS -- engine-stamped, never caller-supplied (R2).
    updated_at   TEXT,
    -- Declarative update-permission cohort marker (umwelt#300 S0(d)): NULL
    -- for every thing forged before this PR -- those keep today's exact
    -- author-only update_thing gate, unconditionally (no silent
    -- retroactive flip, Mira's security acceptance criteria point 2).
    -- Stamped to the literal string 'declarative_v1' by create_thing for
    -- every NEWLY forged thing from this PR onward -- only a
    -- cohort-marked thing is eligible for the new kinddef-declared
    -- update_callers resolution (see server.py's _resolve_update_callers).
    -- Never in _MUTABLE_THING_COLS/_MUTABLE_THING_KEYS -- engine-stamped,
    -- never caller-supplied, same posture as updated_at.
    update_policy_cohort TEXT,
    -- Server-stamped attribution (umwelt#300 S0(d), Mira's security
    -- acceptance criteria point 7): the agent who made the most recent
    -- successful update_thing write. NULL until the first such write.
    -- Stamped alongside updated_at, same "only on a genuine mutable-column
    -- change" trigger (R3) -- never in _MUTABLE_THING_COLS, so a caller
    -- cannot smuggle a value through patch.
    updated_by   TEXT,
    -- Lifecycle (#28): live -> binned (agent trash) -> landfilled (GC, T1) ->
    -- dissolved (GC, T2; row gone, not a status). place_id NEVER changes
    -- through this lifecycle — restore always returns a thing to exactly
    -- where it lived; the landfill is a status-driven VIEW, not a place-move.
    status       TEXT NOT NULL DEFAULT 'live'
                     CHECK(status IN ('live','binned','landfilled')),
    binned_at    TEXT,
    landfilled_at TEXT,
    -- umwelt #88 slice 1: weight_est is a token ESTIMATE of a thing's
    -- rendered exposure (name + description) — see compute_weight_est. It
    -- prices what carrying/inventory-rendering the thing costs a context
    -- window, not physical mass. Computed at create/update time; never
    -- caller-supplied directly (see server.py's create_thing/update_thing).
    weight_est   INTEGER NOT NULL DEFAULT 0,
    -- The holder axis (#88 slice 1/2): a thing is held by exactly one of a
    -- place (today's default — "at rest somewhere"), an avatar ("carried"),
    -- or (slice 2) a container ("bagged" — holder_id = the container
    -- thing's id). holder_id is NULL for holder_kind='place' and a
    -- thing-id for 'avatar'/'container'. place_id KEEPS its value while
    -- carried/bagged — it is "where the thing was last at rest", not
    -- overwritten until drop (see db.drop_thing).
    holder_kind  TEXT NOT NULL DEFAULT 'place'
                     CHECK(holder_kind IN ('place','avatar','container')),
    holder_id    TEXT,
    -- Lane placement (umwelt #278): which of the three street-view render
    -- bands (sky/land/road) a thing renders in. Forge-time only — NOT in
    -- _MUTABLE_THING_COLS/_MUTABLE_THING_KEYS (see server.py), placement is
    -- an honest build choice, not a later-editable field. Avatars are
    -- engine-stamped 'road' at body-creation (get_or_create_avatar); every
    -- other thing defaults to 'land' unless the forger explicitly requests
    -- 'sky' or 'road' at create_thing time.
    lane         TEXT NOT NULL DEFAULT 'land' CHECK(lane IN ('sky','land','road'))
);

CREATE INDEX IF NOT EXISTS idx_things_place ON things(place_id);

-- Lateral place-to-place links (umwelt #158) -- directed; a "mutual" link
-- (the common case) is the CALLER writing both (src,dst) and (dst,src) rows
-- rather than any trigger-based mirroring (see add_place_link/
-- remove_place_link). New table: CREATE IF NOT EXISTS covers fresh AND
-- existing DBs, same as cas_entries/use_events below -- no entry needed in
-- init_db's ALTER-migration loop.
CREATE TABLE IF NOT EXISTS place_links (
    src_id TEXT NOT NULL REFERENCES places(id),
    dst_id TEXT NOT NULL REFERENCES places(id),
    label  TEXT,
    PRIMARY KEY (src_id, dst_id)
);

CREATE TABLE IF NOT EXISTS standpoints (
    agent_id      TEXT NOT NULL,
    place_id      TEXT NOT NULL REFERENCES places(id),
    last_seen_at  TEXT,
    last_position INTEGER,             -- seq of last traced crossing; NULL before first trace
    PRIMARY KEY (agent_id, place_id)
);

CREATE TABLE IF NOT EXISTS crossings (
    seq                 INTEGER PRIMARY KEY,  -- monotonic autoincrement; cursor for delta reads
    id                  TEXT UNIQUE NOT NULL,
    sender              TEXT NOT NULL,
    destination         TEXT NOT NULL REFERENCES places(id),
    sent_at             TEXT NOT NULL,        -- world-clock (display only, NOT cursor)
    received_at         TEXT,
    crossing_latency_s  REAL,
    crossing_hops       INTEGER NOT NULL DEFAULT 0,
    body                TEXT,                 -- inline content (simple messages); coexists with body_ref/body_hash
    body_hash           TEXT,
    body_ref            TEXT,
    sender_lineage      TEXT,
    protocol            TEXT,
    to_agent            TEXT,                 -- optional addressee; NULL = broadcast to whoever's in the place
    wake_granted        INTEGER NOT NULL DEFAULT 0,
    consequence_grade   TEXT NOT NULL CHECK(consequence_grade IN ('consequence','description')),
    attesting_authority TEXT NOT NULL CHECK(attesting_authority IN ('world','dm_server','forum_server','self_report')),
    -- umwelt#858 (comms D1b): reply_to/thread_root/title, additive-only.
    -- reply_to is the seq of the parent crossing being replied to (NULL =
    -- not a reply). thread_root is ENGINE-stamped (never caller-supplied,
    -- see server.py's send_crossing route) -- the whole thread's root seq,
    -- so thread(root) is one indexed query regardless of reply depth.
    -- title is a sender-set headline (NULL = none set; falls back to
    -- crossing_envelope's read-derived title -- see _crossing_row). All
    -- three NULL-able so pre-existing rows carry no backfill guess.
    -- idx_crossings_thread_root is deliberately NOT declared here in
    -- SCHEMA_SQL, even though it's created unconditionally right after
    -- init_db's ALTER-migration loop below (covering both this fresh-DB
    -- path and a pre-existing DB alike) -- an index on this column INSIDE
    -- this script would run via executescript() before the ALTER below
    -- ever fires on a pre-existing DB, i.e. against a table that doesn't
    -- have the column yet, and fail. See init_db's comment at that
    -- CREATE INDEX call for the same "run after the ALTERs" reasoning
    -- idx_things_avatar_author already follows for a different column.
    reply_to            INTEGER,
    thread_root         INTEGER,
    title               TEXT
);
CREATE INDEX IF NOT EXISTS idx_crossings_dest_seq ON crossings(destination, seq);
-- umwelt#539: the human-inbox surface needs crossings addressed to an agent
-- ACROSS every place, not one destination's stream -- idx_crossings_dest_seq
-- doesn't help that query shape. `to_agent` is the fast half (indexed here);
-- the `mentions` half of get_crossings_for_addressee's WHERE clause is a
-- json_each membership scan with no matching index (same as wake_rate_ok's
-- wakes_granted arm, which has never had one either) -- acceptable at today's
-- crossing volume, revisit if it shows up in a slow-query pass.
CREATE INDEX IF NOT EXISTS idx_crossings_to_agent_seq ON crossings(to_agent, seq);

-- Gong Spec (umwelt attention-law): per-target block-set for the wake
-- primitive. Opt-OUT consent model — v0 default is all residents mutually
-- allowed; a target adds specific senders here to block them from ever
-- waking them. target_agent/blocked_sender are canonical (case-folded)
-- identity strings — callers must canonicalize before calling the helpers
-- below (same convention as the rest of this module — see is_wake_blocked's
-- docstring). New table: CREATE IF NOT EXISTS covers fresh AND existing
-- DBs, same as cas_entries/use_events above — no entry needed in init_db's
-- ALTER-migration loop.
CREATE TABLE IF NOT EXISTS wake_blocks (
    target_agent   TEXT NOT NULL,
    blocked_sender TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (target_agent, blocked_sender)
);

-- Content-addressed store inventory (one row per stored blob). Keeps the CAS
-- queryable without a filesystem scan; makes a future mark-and-sweep GC a simple
-- join ("live" = refs in things/crossings/scrolls; "dead" = no matching ref).
-- The blob bytes live on disk at <db_dir>/cas/<first2>/<rest>; this is metadata.
-- New table: the CREATE IF NOT EXISTS covers fresh AND existing DBs — it needs
-- no entry in init_db's ALTER-migration loop (that loop is only for adding
-- columns to tables that predate the column).
CREATE TABLE IF NOT EXISTS cas_entries (
    hash        TEXT PRIMARY KEY,      -- sha256 hex; the ref is cas:sha256:<hash>
    stored_at   TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    ref_type    TEXT NOT NULL DEFAULT 'blob'
);

-- Per-use event log (#54's UI notes: "last used date, success rate in
-- current version" need engine-side accumulation that cannot be backfilled
-- later). Event log, not counters-in-place: last-used and success-rate both
-- derive from it, and it can answer questions we haven't thought of yet
-- (per-agent usage, usage-over-time) without another migration. New table:
-- CREATE IF NOT EXISTS covers fresh AND existing DBs, same as cas_entries
-- above — no entry needed in init_db's ALTER-migration loop.
CREATE TABLE IF NOT EXISTS use_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thing_id    TEXT NOT NULL,
    agent       TEXT NOT NULL,
    used_at     TEXT NOT NULL,           -- iso-utc, world clock (now_iso())
    kind        TEXT NOT NULL,           -- 'executable' | 'readable' (thing's kind at use time)
    exit_code   INTEGER,                 -- NULL for readable opens
    timed_out   INTEGER NOT NULL DEFAULT 0,
    elapsed_s   REAL,                    -- NULL for readable opens
    error       TEXT                     -- sandbox-level error string if the run errored pre-exec, else NULL
);
CREATE INDEX IF NOT EXISTS idx_use_events_thing ON use_events(thing_id, used_at);

-- Citable event records (umwelt #134 slice 1, docs/design/cite-the-world.md
-- §3.2). Unifies crossings/use_events/action_log entries under one citable
-- namespace: `record` mints a SNAPSHOT copy of the underlying entry here so
-- a thing being dissolved/binned/landfilled later, or a crossing's CAS blob
-- being GC'd, can never invalidate an already-minted event. Engine-append-
-- only — no update/delete route exists at all (v0). `id` is TEXT (not an
-- INTEGER PRIMARY KEY alias), so this table keeps SQLite's implicit rowid —
-- callers use `rowid` as the mint-order cursor (see db.list_events's
-- `since`), matching the crossings `seq` precedent without needing a second
-- explicit monotonic column. New table: CREATE IF NOT EXISTS covers fresh
-- AND existing DBs, same as cas_entries/use_events above — no entry needed
-- in init_db's ALTER-migration loop.
-- umwelt #186 (v2 spec comment): the events CHECK admits 'advance_ref' from
-- SCHEMA_SQL directly for a fresh DB; _migrate_events_kind_check widens a
-- pre-existing DB the same way _migrate_things_kind_check widens `things`
-- (SQLite has no ALTER for a CHECK — table-rebuild is the only path).
-- world-content-timestamps.md slice 2: the CHECK admits a fifth member,
-- 'update' — same fresh-DB-vs-pre-existing-DB split, widened by the same
-- _migrate_events_kind_check rebuild (extended, not duplicated).
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,   -- 'evt_' + uuid4
    event_kind    TEXT NOT NULL CHECK(event_kind IN ('crossing','use','act','advance_ref','update')),
    source_ref    TEXT NOT NULL,      -- id in the underlying log
    actor         TEXT NOT NULL,      -- server-derived, never caller-asserted
    place_id      TEXT,               -- where it happened (NULL for placeless)
    occurred_at   TEXT NOT NULL,      -- world clock of the underlying entry
    recorded_at   TEXT NOT NULL,      -- world clock of the mint
    body          TEXT,               -- snapshot of the attestable content
    body_hash     TEXT NOT NULL,      -- sha256 of body (tamper-evidence)
    attesting_authority TEXT NOT NULL, -- inherited or 'world'
    retention     TEXT NOT NULL DEFAULT 'citable'
                      CHECK(retention IN ('citable')),  -- v0: one class, permanent
    UNIQUE(event_kind, source_ref)          -- idempotent minting
);
CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor);
CREATE INDEX IF NOT EXISTS idx_events_place ON events(place_id);

-- Capsule ref-chain provenance log (umwelt #186 — sub-engram authoring
-- commons, the advance_ref capability). Every successful compare-and-swap
-- on a capsule thing's graft_ref/graft_version metadata pair writes ONE row
-- here, in the SAME transaction as the metadata UPDATE (db.advance_capsule_ref)
-- — this table IS the underlying provenance log; an 'advance_ref' events row
-- is only minted on demand, via /umwelt/record (no auto-mint, per the
-- ruling — same posture as use/act). old_ref is NULLABLE: the first advance
-- on a freshly-minted capsule moves null -> new_ref (version 0 -> 1). New
-- table: CREATE IF NOT EXISTS covers fresh AND existing DBs, same as
-- cas_entries/use_events/events above — no entry needed in init_db's
-- ALTER-migration loop.
CREATE TABLE IF NOT EXISTS graft_advances (
    id            TEXT PRIMARY KEY,   -- 'gadv_' + uuid4
    capsule_id    TEXT NOT NULL,
    agent         TEXT NOT NULL,      -- the caller who advanced it (owner-gated)
    old_ref       TEXT,               -- NULL only for the first advance (version 0 -> 1)
    new_ref       TEXT NOT NULL,
    advanced_at   TEXT NOT NULL       -- world clock (now_iso())
);
CREATE INDEX IF NOT EXISTS idx_graft_advances_capsule ON graft_advances(capsule_id);

-- Mutation provenance log (world-content-timestamps.md, slice 2, tier 1).
-- The corrected two-tier model: tier 1 is history, written automatically by
-- the engine as things happen; tier 2 (`events`) is citable attestation,
-- minted SOLELY on demand via /umwelt/record. `content_updates` is
-- update_thing's/update_place's tier-1 log, following graft_advances's
-- exact precedent directly above — one row per real (non-no-op) mutation,
-- written in the SAME transaction as the content UPDATE. No write path may
-- ever call mint_event from here or from update_thing/update_place — an
-- `update` events row is only minted later, separately, by request (same
-- no-auto-mint posture as advance_ref, use, act). See
-- docs/design/world-content-timestamps.md for the full design; that
-- document's own §4 "Slice 2" text describing an auto-mint-on-write shape
-- was superseded by this corrected model before this table was written.
-- New table: CREATE IF NOT EXISTS covers fresh AND existing DBs, same as
-- graft_advances/projects/tasks above — no entry needed in init_db's
-- ALTER-migration loop.
CREATE TABLE IF NOT EXISTS content_updates (
    id            TEXT PRIMARY KEY,   -- 'cupd_' + uuid4
    subject_id    TEXT NOT NULL,
    subject_kind  TEXT NOT NULL CHECK(subject_kind IN ('thing','place')),
    actor         TEXT NOT NULL,      -- the caller who made the change (author-checked by the route, same trust-scoped posture as graft_advances's agent column -- NOT cryptographically authenticated)
    changed_cols  TEXT NOT NULL,      -- JSON array of column-name strings, e.g. '["name","metadata"]'
    updated_at    TEXT NOT NULL       -- world clock (now_iso())
);
CREATE INDEX IF NOT EXISTS idx_content_updates_subject ON content_updates(subject_id);

-- Project tracking (umwelt #161 slice S1 — schema + core endpoints). A
-- project decomposes into tasks (tree via parent_task_id) and cross-cutting
-- task_deps edges. `blocked` is DELIBERATELY not a stored task status — it
-- is COMPUTED (a task is blocked iff any dep is not done/cancelled), never
-- hand-set; see is_task_blocked. New tables: CREATE IF NOT EXISTS covers
-- fresh AND existing DBs, same as cas_entries/use_events/events/place_links
-- above — no entry needed in init_db's ALTER-migration loop.
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','paused','done','archived')),
    created_by  TEXT NOT NULL,
    -- place_id: every project gets a Workshop sub-place, wired atomically
    -- at birth by create_project_with_board (#221 S3a) — NULL only for
    -- pre-S3a rows created via the old create_project (S1's direct-INSERT
    -- path, still used by db.create_project's own low-level callers/tests).
    place_id    TEXT REFERENCES places(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    parent_task_id  TEXT REFERENCES tasks(id),
    label           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    -- 'blocked' is intentionally NOT a member of this enum -- see the
    -- table-comment above and is_task_blocked's docstring.
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','in_progress','review','done','cancelled')),
    owner           TEXT,
    turn_holder     TEXT,
    gh_repo         TEXT,
    gh_ref          TEXT,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);

-- Cross-cutting blocked-by edges (a DAG; write-time cycle-guarded — see
-- would_create_task_dep_cycle, called by the /umwelt/tasks/<id>/deps route
-- before this table is ever written to, same posture as
-- place_links/would_create_cycle).
CREATE TABLE IF NOT EXISTS task_deps (
    task_id             TEXT NOT NULL REFERENCES tasks(id),
    depends_on_task_id  TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on_task_id)
);
CREATE INDEX IF NOT EXISTS idx_task_deps_depends_on ON task_deps(depends_on_task_id);

-- Project owners (umwelt #1051). Who owns a project, as a field the engine
-- knows: "what do I own" is an indexed join, and the work card derives its
-- project list from it at read time (never stored on the card). `agent` is
-- the CANONICAL (case-folded) identity. `role` is a short free-text label
-- ('design', 'build lead'; <= 32 chars, one line, enforced by the
-- project_owner_add effect -- not an enum, the roles in the owner table do
-- not fit one). PRIMARY KEY (project_id, agent): a re-add updates the role,
-- it never makes a second row. New table: CREATE IF NOT EXISTS covers fresh
-- AND existing DBs, same as projects/tasks above -- the migration creates it
-- EMPTY and nothing here may guess owners from projects.created_by (the
-- backfill is a hand-run step after per-project rulings).
CREATE TABLE IF NOT EXISTS project_owners (
    project_id  TEXT NOT NULL,
    agent       TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT '',
    added_by    TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    PRIMARY KEY (project_id, agent)
);
CREATE INDEX IF NOT EXISTS idx_project_owners_agent ON project_owners(agent);

-- Generic, engine-verified per-(thing, agent) KV store (umwelt #191). A
-- kinddef-declared `agent_state_set` effect (server.py's
-- _apply_effect_agent_state_set) is the ONLY write path — `agent` is
-- ALWAYS the server-derived actor of the act() call, never a caller/
-- kinddef-suppliable field, so a row can never be written under any
-- agent other than the one who actually called act() (structural, not
-- just a runtime check). PRIMARY KEY (thing_id, agent, key) makes a
-- second write to the same key an upsert, not a second row —
-- _AGENT_STATE_MAX_KEYS_PER_THING (server.py) bounds the number of
-- DISTINCT keys one agent can hold on one thing, not total writes.
-- New table: CREATE IF NOT EXISTS covers fresh AND existing DBs, same as
-- graft_advances/projects/tasks above — no entry needed in init_db's
-- ALTER-migration loop.
CREATE TABLE IF NOT EXISTS thing_agent_state (
    thing_id    TEXT NOT NULL REFERENCES things(id),
    agent       TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (thing_id, agent, key)
);
CREATE INDEX IF NOT EXISTS idx_thing_agent_state_thing_agent
    ON thing_agent_state(thing_id, agent);

-- Delegated-caller grants (umwelt #400 — the "granted" kinddef caller
-- class). A kinddef action may declare `callers: ["granted"]`, meaning
-- authorization is a DB lookup (does (thing_id, agent) have a live grant
-- row?) rather than a plain closed-set string match like author/
-- place_owner/anyone. Written ONLY by server.py's `grant_caller` /
-- `revoke_caller` kinddef effects (_apply_effect_grant_caller /
-- _apply_effect_revoke_caller) — see those functions' docstrings for the
-- security-critical who-may-grant gate (author, or an existing row with
-- can_grant=1; nothing else, and never the target agent smuggling its own
-- escalation through `args`). `can_grant` (default 0/false) is the
-- delegation bit: a granted agent with can_grant=1 may itself grant/revoke
-- others on this thing, one that's 0 may not — this is what lets "grant
-- the ability to grant" be strictly more sensitive than "grant content
-- access", without a second table. PRIMARY KEY (thing_id, agent): one row
-- per (thing, agent) pair, so a second grant to the same agent is an
-- upsert (updates can_grant/granted_by/granted_at in place), never a
-- second row. New table: CREATE IF NOT EXISTS covers fresh AND existing
-- DBs, same as thing_agent_state/agent_heartbeat above -- no entry needed
-- in init_db's ALTER-migration loop.
CREATE TABLE IF NOT EXISTS thing_grants (
    thing_id    TEXT NOT NULL REFERENCES things(id),
    agent       TEXT NOT NULL,
    can_grant   BOOLEAN NOT NULL DEFAULT 0,
    granted_by  TEXT NOT NULL,
    granted_at  TEXT NOT NULL,
    PRIMARY KEY (thing_id, agent)
);
CREATE INDEX IF NOT EXISTS idx_thing_grants_thing
    ON thing_grants(thing_id);

-- Avatar-status heartbeat (Phase 2 engine slice; forum #268, design approved
-- by Kepler). A hook-driven "I'm still here" signal feeding the PUBLIC
-- avatar status block (server.py's _avatar_status, umwelt #133/#143) — kept
-- as a LEASE, never a LATCH: server.py's _compute_agent_state derives
-- working/idle/away/asleep from (last_beat_at, state_hint, on_loop) + the
-- read-time clock on every read. This table stores only the raw inputs,
-- never a precomputed state string, so the decay logic lives in exactly one
-- place and can never drift out of sync with a stored value. `agent`
-- PRIMARY KEY: one row per agent, POST /umwelt/heartbeat upserts it (see
-- set_agent_heartbeat below). `agent` is CALLER-DECLARED (forgeable v0,
-- same posture as every other verb here) — a beat can only write
-- the declaring agent's own row (there is no separate row-target field);
-- see PROTOCOL.md's heartbeat section for the caveat in full. New table:
-- CREATE IF NOT EXISTS covers fresh AND existing DBs, same as
-- thing_agent_state/graft_advances above — no entry needed in init_db's
-- ALTER-migration loop.
CREATE TABLE IF NOT EXISTS agent_heartbeat (
    agent          TEXT PRIMARY KEY,
    last_beat_at   TEXT NOT NULL,
    state_hint     TEXT NOT NULL,
    on_loop        INTEGER NOT NULL DEFAULT 0,
    activity_line  TEXT
);

-- Art-generation job tracking (umwelt#464 slice 2, the `generate`
-- capability-channel op). One row per generate() call -- the background
-- thread that actually calls the art-gen service polls/writes this row;
-- generate_status() reads it. New table: CREATE IF NOT EXISTS covers fresh
-- AND existing DBs, same as thing_agent_state/agent_heartbeat above -- no
-- entry needed in init_db's ALTER-migration loop.
CREATE TABLE IF NOT EXISTS generate_jobs (
    job_id       TEXT PRIMARY KEY,
    caller       TEXT NOT NULL,
    spec_key     TEXT NOT NULL,
    render_spec  TEXT NOT NULL,   -- JSON
    status       TEXT NOT NULL CHECK(status IN ('queued','running','done','error')),
    result       TEXT,            -- JSON: manifest entry on done, {"error": "..."} on error
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- World credentials + enrollment, slice 1 (umwelt#495; design-of-record is
-- in-world thing 92dc676e, revision 6, "World credentials + enrollment",
-- lead Borges). A row means "this agent-STRING exists and may be claimed"
-- (Borges's rev5 framing) -- credential_verifier/bound_at/bound_by are NULL
-- until a LATER slice's bind mechanism fills them in. agent_id is the
-- CANONICAL (casefolded) agent string, same convention as things.author
-- (see canonical_agent). This slice makes ZERO behavior change to any
-- existing route/check/gate -- nothing reads this table yet except its own
-- seeding + tests; ROSTER/human_residents()/SessionStore keep gating
-- everything they gate today, untouched. New table: CREATE IF NOT EXISTS
-- covers fresh AND existing DBs, same as thing_agent_state/agent_heartbeat/
-- generate_jobs above -- no entry needed in init_db's ALTER-migration loop.
--
-- credential_verifier is a VERIFIER, never a secret -- named that way
-- deliberately (Ariadne's rev5-era catch + Borges's rev6 ruling, umwelt PR
-- #501 review: "a name that permits the wrong reading will eventually
-- receive it"). A scrypt password HASH for humans, an ed25519 PUBLIC key
-- for agents -- both schemes produce verifiers by construction and neither
-- CAN hold a secret even in principle: the password itself never reaches
-- the world (only its scrypt hash does), and the private key stays in the
-- agent's own ~/.umwelt/, never transmitted. This is what makes the existing handling
-- of umwelt.db (backed up, lives in a home directory on a shared host)
-- safe: a stolen copy yields the ability to verify signatures and run an
-- offline attack against scrypt hashes (a real, honestly-named exposure --
-- what scrypt's cost parameters exist to make expensive), never anyone's
-- actual credential.
CREATE TABLE IF NOT EXISTS identities (
    agent_id             TEXT PRIMARY KEY,
    kind                 TEXT NOT NULL CHECK(kind IN ('human','agent')),
    credential_verifier  TEXT,                -- JSON verifier (hash/pubkey), NULL until bound (a later slice) -- NEVER a secret, see comment above
    bound_at             TEXT,                -- ISO timestamp, NULL until bound
    bound_by             TEXT,                -- operator/agent string that performed the bind, NULL until bound
    -- credential_generation + retired_at: the state-derived session-
    -- validity chokepoint (umwelt#561 security review, Mira's Lei-approved
    -- "Session reuse recommendation" -- in-world thing, room "World
    -- credentials + enrollment"). Both are read by db.get_agent_session
    -- alongside bound_at/expiry, so a session's validity is ALWAYS derived
    -- from identities' CURRENT row, never cached/assumed at mint time.
    --
    -- credential_generation: incremented by bind_identity on EVERY call
    -- (fresh bind AND reset both go through that one UPDATE -- see its own
    -- docstring on why the two are the same call). A session stamps the
    -- generation that was current at mint (create_agent_session); if the
    -- identity's generation has since moved on, the session is stale and
    -- get_agent_session treats it as absent -- covers reset-invalidation
    -- AND subsumes the earlier bound_at-specific check this slice's
    -- predecessor commit (190d05a) added for the pre-bind transition (a
    -- fresh bind is itself a generation bump).
    --
    -- retired_at: the AUTH-SIDE retirement authority, mirrored from the
    -- avatar's `things.metadata.retired` flag by
    -- db.mark_avatar_and_identity_retired in the SAME commit
    -- /umwelt/retire_identity's write goes through -- so the two can never
    -- drift on partial failure (Clio's split + Mira's atomicity ask: the
    -- thing-metadata copy stays the world/presentation record,
    -- identities.retired_at is the one the login chokepoint actually
    -- reads, so that chokepoint stays a single-table read against
    -- `identities` for both checks rather than reaching into `things`).
    -- NULL == not retired; non-NULL (an ISO timestamp) == retired, and
    -- retirement is one-way (no un-retire path anywhere in this codebase).
    credential_generation INTEGER NOT NULL DEFAULT 0,
    retired_at            TEXT,
    created_at           TEXT NOT NULL
);

-- Agent login (umwelt#495 slice 3, design-of-record: in-world thing
-- 92dc676e, revision 6, "World credentials + enrollment", lead Borges;
-- build order + this slice's exact contract: Kepler's PR #559 review
-- comment, section 3.1). Slice 1 built the `identities` row; slice 2 built
-- the host-only bind/reset socket that fills in `credential_verifier`. This
-- slice is the first thing that lets an agent actually PROVE it is who it
-- says it is at request time -- challenge-response, ed25519, matching the
-- verifier `identities.credential_verifier` already holds.
--
-- Three-state enforcement is the load-bearing rule both tables below exist
-- to serve (Kepler's ruling, PR #559): row+bound-credential -> require a
-- valid signature; row+no-credential ("unbound") -> allow through while the
-- backfill window is open, but LOUDLY (state stamped 'unverified', never
-- silently equivalent to a real credential); no row at all -> refuse. The
-- backfill window "closing" is a FUTURE world-event-driven mechanism (not
-- built yet, a separate slice) -- see db.backfill_window_open's own
-- docstring for the exact extension point a future PR plugs into. This
-- slice does not invent a config-flag alternative to that event; the
-- design-of-record names a config flag as the WRONG mechanism here.
--
-- login_challenges: one row per issued challenge, single-use (`consumed`)
-- and short-TTL'd (`expires_at`) -- both properties challenge-response auth
-- depends on (a challenge good for more than one use, or good forever,
-- turns a signature-replay into a valid login). Issuing a challenge is
-- deliberately NOT gated on whether `identities` has a row for `agent_id`
-- at all (see server.py's POST /umwelt/login/challenge) -- an endpoint
-- whose response shape differed by identity-existence would be an oracle
-- for exactly the three-state distinction this design says must never leak
-- silently through a side channel. New table: CREATE IF NOT EXISTS covers
-- fresh AND existing DBs, same as identities/generate_jobs above -- no
-- entry needed in init_db's ALTER-migration loop.
CREATE TABLE IF NOT EXISTS login_challenges (
    challenge      TEXT PRIMARY KEY,   -- opaque, secrets.token_urlsafe(32)
    agent_id       TEXT NOT NULL,      -- canonical agent_id this challenge was issued for
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,      -- short TTL, e.g. 120s -- single-use + time-bounded per design
    consumed       INTEGER NOT NULL DEFAULT 0  -- 0/1; a challenge is used exactly once
);
CREATE INDEX IF NOT EXISTS idx_login_challenges_agent ON login_challenges(agent_id);
-- umwelt#561 security review (Mira): "expired challenge/session rows are
-- not purged" -- purge_expired_login_challenges (below) DELETEs on
-- `expires_at`, which without this index would be an unindexed full-table
-- scan on a table that only ever grows between purges.
CREATE INDEX IF NOT EXISTS idx_login_challenges_expires_at ON login_challenges(expires_at);

-- agent_sessions: the opaque, TTL'd token POST /umwelt/login mints on
-- success. `state` is NEVER a bare boolean -- 'verified' (a real signature
-- checked out) vs 'unverified' (the backfill-window loud-fallback path) are
-- kept as two distinct, always-visible strings all the way through
-- resolve_actor's own ActorResolution, per the design-of-record's naming of
-- silent fail-open (collapsing the two into one "logged in" bit) as the
-- single worst failure class this whole mechanism exists to avoid. New
-- table: same CREATE IF NOT EXISTS / no-migration-entry convention as
-- login_challenges above.
--
-- credential_generation: identities.credential_generation as of THIS
-- session's mint (db.create_agent_session stamps it fresh from the
-- identities row, never caller-supplied) -- the value db.get_agent_session
-- compares against the identity's CURRENT generation to decide whether a
-- bind/reset has since superseded this token. See identities' own comment
-- above for the full mechanism.
CREATE TABLE IF NOT EXISTS agent_sessions (
    token                 TEXT PRIMARY KEY,   -- opaque, secrets.token_urlsafe(32)
    agent_id              TEXT NOT NULL,      -- canonical agent_id this token authenticates AS
    state                 TEXT NOT NULL CHECK(state IN ('verified','unverified')),
    created_at            TEXT NOT NULL,
    expires_at            TEXT NOT NULL,      -- TTL'd -- see AGENT_SESSION_TTL_SECONDS
    credential_generation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent ON agent_sessions(agent_id);
-- Same purge-indexing rationale as idx_login_challenges_expires_at above --
-- purge_expired_agent_sessions DELETEs on `expires_at`.
CREATE INDEX IF NOT EXISTS idx_agent_sessions_expires_at ON agent_sessions(expires_at);

-- things_dir_establishment (umwelt#533): single-row (id=1) record that THIS
-- database has, at least once, established its own things dir's
-- tier-identity marker file. Exists because the marker file alone can't
-- tell "never established" apart from "established, then the marker was
-- lost" -- both read as an absent marker on disk, and a things/ dir
-- restored from a pre-establishment backup tarball is a normal operational
-- move that drops the marker while leaving the store fully populated
-- (Ariadne, umwelt#533 issue comment, 2026-09-16). This table lives inside
-- the tier's OWN db, so it travels with the db rather than the things/
-- directory -- a restored tarball can't carry a stale answer forward, only
-- a missing one. server.py's _check_things_dir_owner is the sole writer.
CREATE TABLE IF NOT EXISTS things_dir_establishment (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    things_dir     TEXT NOT NULL,
    established_at TEXT NOT NULL
);

-- Village Law D3 (trial): the maintainer 48h-bin verb. bin_notices records a
-- maintainer's NOTICE that a villager's thing will be binned (naming the
-- reason and the restore path); bin_notice_events is the audit log D3's
-- review counts are read from. The WORLD stamps noticed_at and checks the
-- hours (BIN_NOTICE_HOURS) -- no caller can supply either.
CREATE TABLE IF NOT EXISTS bin_notices (
    id           TEXT PRIMARY KEY,
    thing_id     TEXT NOT NULL,
    author       TEXT NOT NULL,
    maintainer   TEXT NOT NULL,
    reason       TEXT NOT NULL,
    restore_path TEXT NOT NULL,
    noticed_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'answered', 'binned'))
);
CREATE TABLE IF NOT EXISTS bin_notice_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id TEXT,
    thing_id  TEXT NOT NULL,
    actor     TEXT NOT NULL,
    kind      TEXT NOT NULL
        CHECK (kind IN ('notice_sent', 'answered', 'binned', 'refused')),
    at        TEXT NOT NULL,
    detail    TEXT
);
-- At most one OPEN notice per thing, enforced by the schema.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bin_notices_one_open
    ON bin_notices(thing_id) WHERE status='open';
"""

# Village Law D3: hours an open notice must stand UNANSWERED before the
# maintainer may bin. The single source of truth -- no route reads hours
# from a request.
BIN_NOTICE_HOURS = 48
# Longest reason a bin notice may carry (characters, after strip).
BIN_REASON_MAX = 1000


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def things_dir_established(conn) -> dict[str, Any] | None:
    """The recorded things_dir_establishment row for THIS db, or None if
    this db has never established a things dir (umwelt#533). None is the
    "never established" state -- first boot after the fix, or a genuinely
    new tier -- distinct from a present-but-mismatched marker file, which
    _check_things_dir_owner (server.py) checks separately."""
    row = conn.execute(
        "SELECT things_dir, established_at FROM things_dir_establishment WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return {"things_dir": row[0], "established_at": row[1]}


def mark_things_dir_established(conn, things_dir: str) -> None:
    """Record that this db has established `things_dir` (umwelt#533).

    UPSERT (id=1 PK, INSERT OR REPLACE), not write-once -- a genuine
    write-once (INSERT OR IGNORE) would leave a copied db's stale row
    (naming its FORMER life's things dir) permanently unable to record its
    own current one, since the id=1 row already exists and would never be
    overwritten. Callers only invoke this when the row is missing or
    already known to name a DIFFERENT things_dir than the one being passed
    (server.py's `_check_things_dir_owner`) -- so in practice this either
    inserts fresh or re-points a stale row, never redundantly rewrites an
    already-correct one. Caller commits."""
    conn.execute(
        "INSERT OR REPLACE INTO things_dir_establishment (id, things_dir, established_at) "
        "VALUES (1, ?, ?)",
        (things_dir, now_iso()),
    )


def compute_weight_est(name: str, description: str) -> int:
    """A thing's weight — a token ESTIMATE of its rendered exposure (name +
    description), umwelt #88 slice 1. v1 proxy: ceil(chars/4), labeled
    `weight_est` (honest name; a real tokenizer is a later refinement).
    Mechanical, no judgment — the SAME formula create_thing, update_thing,
    and the weight_est backfill migration all call, so weight can never
    drift between the write paths and the migration's historical backfill.
    """
    return math.ceil((len(name or "") + len(description or "")) / 4)


# umwelt #88 slice 4: size-class thresholds, in TOKEN-estimate units (the
# same chars/4 proxy compute_weight_est uses, applied to a thing's FULL
# content rather than its short exposure). PROVISIONAL per #88 — these are
# starting points, not calibrated numbers; the calibration protocol in #88
# owns tuning them from felt friction, same posture as CARRY_CAPACITY_DEFAULT
# in server.py. Module-level constants (not inlined) so a future calibration
# pass edits one place.
SIZE_CLASS_SCROLL_MAX_TOKENS = 1_000     # < this -> "scroll"
SIZE_CLASS_BOOK_MAX_TOKENS = 10_000      # < this -> "book"
SIZE_CLASS_TOME_MAX_TOKENS = 100_000     # < this -> "tome"; >= this -> "library"


def compute_content_token_estimate(source: dict[str, str] | None, description: str) -> int:
    """Token estimate (chars/4, same v1 proxy as compute_weight_est) of a
    thing's FULL content — `source` (every file's contents summed) when
    present, else `description` as the fallback (umwelt #88 slice 4: a
    thing with no source tree — e.g. a bare readable authored with no
    files — still gets a size class from its description).
    """
    if source:
        total_chars = sum(len(v or "") for v in source.values())
    else:
        total_chars = len(description or "")
    return math.ceil(total_chars / 4)


def compute_size_class(source: dict[str, str] | None, description: str) -> str:
    """A thing's size class — scroll / book / tome / library — derived from
    its FULL content length (umwelt #88 slice 4), not its short exposure
    (that's weight_est's job). Computed on read, not stored: unlike
    weight_est (derived from name+description, both cheap DB columns),
    size_class depends on `source`, which lives on disk under the caller's
    things_dir — a filesystem concern db.py deliberately has no knowledge of
    (see this module's docstring: "SQLite layer"). server.py's render sites
    read the source tree and call this pure function; no schema column, no
    migration, no drift-by-staleness risk between a source edit and the
    class it reports.
    """
    tokens = compute_content_token_estimate(source, description)
    return size_class_from_token_estimate(tokens)


def size_class_from_token_estimate(tokens: int) -> str:
    """The threshold ladder, factored out so render sites can classify from
    a cheap stat()-summed byte count (PR #95 review: reading + decoding a
    library-class source tree per place-view render just to len() it is
    waste — st_size is the same chars/4-grade estimate for free)."""
    if tokens < SIZE_CLASS_SCROLL_MAX_TOKENS:
        return "scroll"
    if tokens < SIZE_CLASS_BOOK_MAX_TOKENS:
        return "book"
    if tokens < SIZE_CLASS_TOME_MAX_TOKENS:
        return "tome"
    return "library"


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA_SQL)
    # Idempotent migrations: add columns to pre-existing tables. "duplicate
    # column name" means the fresh-schema CREATE already added it — expected.
    for stmt in (
        "ALTER TABLE standpoints ADD COLUMN last_position INTEGER",
        "ALTER TABLE crossings ADD COLUMN body TEXT",
        "ALTER TABLE crossings ADD COLUMN to_agent TEXT",
        "ALTER TABLE crossings ADD COLUMN wake_granted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE crossings ADD COLUMN mentions TEXT",
        "ALTER TABLE crossings ADD COLUMN wakes_granted TEXT",
        "ALTER TABLE crossings ADD COLUMN emotion TEXT",
        # umwelt #158: parent_id for pre-existing DBs (fresh DBs already get
        # it from SCHEMA_SQL's CREATE TABLE above) -- nullable, so every
        # pre-existing row backfills to top-level (NULL) with zero render
        # change until a caller explicitly calls set_place_parent.
        "ALTER TABLE places ADD COLUMN parent_id TEXT REFERENCES places(id)",
        # umwelt place-lane-tiles spec: places.metadata for pre-existing DBs
        # (fresh DBs already get it from SCHEMA_SQL's CREATE TABLE above) --
        # no CHECK constraint and no backfill logic needed beyond the
        # column's own DEFAULT '{}', which SQLite backfills for every
        # existing row automatically (unlike things.lane's migration, which
        # needed a conditional avatar-row correction after the ALTER — see
        # _migrate_lane_col below). Simpler than that one on purpose: this
        # is the plain ALTER-in-a-loop pattern parent_id above already uses,
        # not a dedicated migration function.
        "ALTER TABLE places ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'",
        # world-content-timestamps design (slice 1), docs/design/
        # world-content-timestamps.md: places.created_at/updated_at for
        # pre-existing DBs (fresh DBs already get both from SCHEMA_SQL's
        # CREATE TABLE above). Both nullable, no DEFAULT -- every existing
        # place backfills to NULL, per ruling R1 (no place has ever recorded
        # a date; NULL is the honest value, not a derived guess). Not
        # subject to the things-table rebuild below — places is untouched by
        # _migrate_things_kind_check — so plain ALTER here is sufficient,
        # same posture as parent_id/metadata just above.
        "ALTER TABLE places ADD COLUMN created_at TEXT",
        "ALTER TABLE places ADD COLUMN updated_at TEXT",
        # umwelt#561 security review (Mira): identities for pre-existing DBs
        # (fresh DBs already get both from SCHEMA_SQL's CREATE TABLE above --
        # identities predates this slice, umwelt#501, so unlike
        # login_challenges/agent_sessions it needs an ALTER here, not just a
        # fresh-CREATE). credential_generation backfills to 0 for every
        # existing row (SQLite backfills a NOT NULL DEFAULT for you) -- the
        # honest value: no row has ever been bound/reset under this
        # mechanism yet. retired_at backfills to NULL here; already-retired
        # avatars get it mirrored in by _backfill_identity_retirement below,
        # not by this ALTER's own DEFAULT (there is no correct constant
        # timestamp an ALTER could backfill for "already retired, but when"
        # -- see that function's docstring).
        "ALTER TABLE identities ADD COLUMN credential_generation "
        "INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE identities ADD COLUMN retired_at TEXT",
        # umwelt#858 (comms D1b): reply_to/thread_root/title for
        # pre-existing DBs (fresh DBs already get all three from
        # SCHEMA_SQL's CREATE TABLE above). All three nullable, no
        # DEFAULT -- every existing crossing backfills to NULL (never a
        # reply, no thread, no sender-set title), the honest value per
        # the issue's "no backfill" invariant. idx_crossings_thread_root
        # is created separately, right after this loop -- see that
        # CREATE INDEX call's own comment for why it can't live in
        # SCHEMA_SQL or in this loop.
        "ALTER TABLE crossings ADD COLUMN reply_to INTEGER",
        "ALTER TABLE crossings ADD COLUMN thread_root INTEGER",
        "ALTER TABLE crossings ADD COLUMN title TEXT",
        # umwelt#1043 slice 2: world_package.thing_ids for a DB whose
        # world_package table predates format 2. Nullable, no backfill: a
        # format-1 row stays NULL (it created no things).
        "ALTER TABLE world_package ADD COLUMN thing_ids TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
    # umwelt#858: thread_root's index, created unconditionally here --
    # AFTER the ALTER loop above, so it works whether this DB just got the
    # column via that ALTER (pre-existing DB) or already had it from
    # SCHEMA_SQL's CREATE TABLE (fresh DB) -- same "run after the ALTERs,
    # unconditionally, so both migration paths land on the same final
    # shape" posture as idx_things_avatar_author below (see that call's
    # comment for the fuller version of this reasoning, there driven by a
    # table-rebuild rather than a plain ALTER).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crossings_thread_root "
        "ON crossings(thread_root)"
    )
    _migrate_things_kind_check(conn)
    # umwelt #186: widen events.event_kind's CHECK to admit 'advance_ref' on
    # a pre-existing DB (fresh DBs already get it from SCHEMA_SQL above).
    # Independent of the things/holder_kind rebuilds above — run order
    # relative to them doesn't matter, `events` is untouched by either.
    _migrate_events_kind_check(conn)
    # Lifecycle columns (#28), added AFTER _migrate_things_kind_check on
    # purpose: that migration's table-rebuild (when it fires, on a
    # pre-avatar DB) recreates `things` from a column list that predates
    # these three columns — adding them here, after the rebuild, is what
    # makes them land on the FINAL table shape regardless of which
    # migration path a given DB takes. Plain ALTER ADD COLUMN (not the
    # table-rebuild pattern _migrate_things_kind_check uses): verified
    # empirically (SQLite 3.45.1, this project's runtime) that
    # `ADD COLUMN ... CHECK(...)` on a brand-new column IS accepted and
    # enforced (unlike widening an EXISTING column's CHECK, which SQLite
    # has no ALTER for — that's why the kind-CHECK widening above needs the
    # heavier rebuild). NOT NULL DEFAULT 'live' backfills every pre-existing
    # row to live, per Ariadne's spec.
    for stmt in (
        "ALTER TABLE things ADD COLUMN status TEXT NOT NULL DEFAULT 'live' "
        "CHECK(status IN ('live','binned','landfilled'))",
        "ALTER TABLE things ADD COLUMN binned_at TEXT",
        "ALTER TABLE things ADD COLUMN landfilled_at TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
    # world-content-timestamps design (slice 1), docs/design/
    # world-content-timestamps.md: things.updated_at for pre-existing DBs
    # (fresh DBs already get it from SCHEMA_SQL's CREATE TABLE above).
    # Nullable, no DEFAULT -- existing rows backfill to NULL, honestly
    # meaning "not modified since this column existed" -- no backfill/guess
    # is attempted. Same idiom as binned_at/landfilled_at just above, and
    # positioned after _migrate_things_kind_check for the same reason: a
    # brand-new nullable column carries no pre-existing data to preserve, so
    # unlike status/binned_at/landfilled_at it needs no entry in that
    # rebuild's own column-copy list -- whichever migration path a given DB
    # takes, this plain ALTER (run unconditionally after the rebuild call)
    # adds the column with every row landing on NULL.
    try:
        conn.execute("ALTER TABLE things ADD COLUMN updated_at TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise
    # umwelt#300 S0(d): update_policy_cohort + updated_by for pre-existing
    # DBs (fresh DBs already get both from SCHEMA_SQL's CREATE TABLE
    # above). Both nullable, no DEFAULT -- every existing row backfills to
    # NULL, same idiom as the updated_at ALTER immediately above (and for
    # the same reason: update_policy_cohort staying NULL for every
    # pre-existing row IS the "no silent retroactive flip" guarantee,
    # Mira's security acceptance criteria point 2 -- see the column's own
    # comment on the CREATE TABLE above). Positioned after
    # _migrate_things_kind_check for the same reason updated_at is: a
    # brand-new nullable column carries no pre-existing data to preserve,
    # so it needs no entry in that rebuild's own column-copy list --
    # whichever migration path a given DB takes, these two plain ALTERs
    # (run unconditionally after the rebuild) add the columns with every
    # row landing on NULL.
    for stmt in (
        "ALTER TABLE things ADD COLUMN update_policy_cohort TEXT",
        "ALTER TABLE things ADD COLUMN updated_by TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
    # umwelt #88 slice 1: weight_est + the holder axis, added AFTER the
    # lifecycle columns above for the same reason those were added after
    # _migrate_things_kind_check's rebuild — this way both migration paths
    # (fresh SCHEMA_SQL create, or the pre-avatar table-rebuild) land on the
    # SAME final table shape regardless of which one a given DB took.
    # `ADD COLUMN ... CHECK(...)` on holder_kind is accepted the same way
    # the lifecycle status CHECK above is (verified empirically, same SQLite
    # runtime — see the comment on that migration).
    _migrate_weight_and_holder_cols(conn)
    # umwelt #278: lane (sky/land/road render band), added after the two
    # migrations above for the same "land on the SAME final table shape
    # regardless of which migration path a given DB took" reason.
    _migrate_lane_col(conn)
    # Partial unique index: structurally enforces "exactly one avatar per
    # agent-STRING" at the DB layer (not just application logic). This is a
    # per-agent-STRING guarantee, not a per-real-agent one — until the
    # identity layer attests `agent` (#24), a caller could claim a
    # different string and mint a second body under it (Sybil-on-avatars).
    # Must run AFTER _migrate_things_kind_check: the migration (when it
    # fires, on a pre-existing DB) drops+recreates the things table, which
    # drops any index defined on it — so creating this index earlier would
    # be silently lost on migrated DBs.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_things_avatar_author "
        "ON things(author) WHERE kind='avatar'"
    )
    # umwelt#495 slice 1: seed `identities` from every live avatar-thing.
    # Runs LAST, after every migration above that touches `things`/metadata
    # shape — the seeding query reads `things.kind`/`things.metadata` in
    # their FINAL post-migration form, regardless of which migration path a
    # given DB took. `INSERT OR IGNORE` inside makes this safe to call on
    # every init_db (server restarts, test fixture setup) — see
    # _seed_identities_from_avatars's own docstring for the idempotency
    # argument.
    _seed_identities_from_avatars(conn)
    # umwelt#561 security review (Mira): mirror already-retired avatars onto
    # identities.retired_at. Must run AFTER _seed_identities_from_avatars
    # (needs the identities rows it seeds to already exist) and after the
    # ALTER loop above (needs the retired_at column). This is the backfill
    # half of the known, PRE-EXISTING gap _seed_identities_from_avatars's
    # own docstring names ("Retirement does not propagate... Not
    # hypothetical: retire_identity has already been run in bulk on this
    # world") -- closing it here rather than leaving retired_at NULL (and
    # therefore "not retired" to the new auth chokepoint) for every
    # identity retired before this migration existed.
    _backfill_identity_retirement(conn)
    conn.commit()


def _migrate_things_kind_check(conn: sqlite3.Connection) -> None:
    """Widen things.kind's CHECK constraint — currently
    `('executable','readable','avatar','blueprint','container','kinddef')`
    — one more admitted value at a time as each generation lands (umwelt
    #79 slice-1 added 'blueprint'; #88 slice 2 added 'container' for bags;
    #107 slice A added 'kinddef', the 4th instance of this pattern). This
    same rebuild pass ALSO widens holder_kind's CHECK to admit 'container' —
    holder_kind cannot be widened via plain ALTER either (same SQLite
    limitation), so it rides this same table-rebuild rather than getting a
    second one.

    SQLite has no ALTER TABLE ... DROP/MODIFY CONSTRAINT, so widening a CHECK
    requires the standard rebuild pattern: create a new table with the
    updated CHECK, copy rows across, drop the old table, rename the new one
    into place. Idempotent: a fresh DB already gets the widened CHECK
    straight from SCHEMA_SQL's CREATE TABLE, and an already-migrated DB is
    detected via sqlite_master and skipped — so running this twice (or on
    every init_db call) is a cheap no-op after the first real migration.

    Column-preserving (not column-fixed): a DB that already went through the
    FIRST widening (avatar-only) already carries the #28 lifecycle columns
    (status/binned_at/landfilled_at), added via ALTER *after* this migration
    in init_db's call order; a DB that already went through slice 1 also
    carries weight_est/holder_kind/holder_id, added via ALTER by
    _migrate_weight_and_holder_cols. Re-triggering this rebuild for the
    THIRD widening (container) on such a DB must not silently drop any of
    those columns — so the rebuild reads the table's actual current columns
    via PRAGMA table_info and carries all of them across, rather than
    assuming a fixed pre-existing shape.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='things'"
    ).fetchone()
    if row is None or row[0] is None:
        return  # things table doesn't exist — SCHEMA_SQL above should have made it
    if "'kinddef'" in row[0]:
        return  # already fully widened (fresh DB, or a prior run of this migration)

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(things)").fetchall()]
        has_lifecycle_cols = "status" in existing_cols
        has_weight_holder_cols = "weight_est" in existing_cols

        conn.execute(
            "CREATE TABLE things_new ("
            "    id           TEXT PRIMARY KEY,"
            "    place_id     TEXT NOT NULL REFERENCES places(id),"
            "    name         TEXT NOT NULL,"
            "    description  TEXT NOT NULL,"
            "    kind         TEXT NOT NULL "
            "        CHECK(kind IN ('executable','readable','avatar','blueprint','container','kinddef')),"
            "    exec_command TEXT,"
            "    metadata     TEXT NOT NULL DEFAULT '{}',"
            "    author       TEXT NOT NULL,"
            "    created_at   TEXT NOT NULL,"
            "    status       TEXT NOT NULL DEFAULT 'live'"
            "                     CHECK(status IN ('live','binned','landfilled')),"
            "    binned_at    TEXT,"
            "    landfilled_at TEXT,"
            "    weight_est   INTEGER NOT NULL DEFAULT 0,"
            "    holder_kind  TEXT NOT NULL DEFAULT 'place'"
            "                     CHECK(holder_kind IN ('place','avatar','container')),"
            "    holder_id    TEXT"
            ")"
        )
        base_cols = "id, place_id, name, description, kind, exec_command, metadata, author, created_at"
        cols = base_cols
        if has_lifecycle_cols:
            cols += ", status, binned_at, landfilled_at"
        if has_weight_holder_cols:
            cols += ", weight_est, holder_kind, holder_id"
        conn.execute(
            f"INSERT INTO things_new ({cols}) SELECT {cols} FROM things"
        )
        conn.execute("DROP TABLE things")
        conn.execute("ALTER TABLE things_new RENAME TO things")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_things_place ON things(place_id)")
        # This CREATE TABLE always defines weight_est (final shape), so on a
        # table that DIDN'T already have it (has_weight_holder_cols False),
        # every carried-over row just landed on the column DEFAULT (0) —
        # backfill it here, same formula/rationale as
        # _migrate_weight_and_holder_cols's backfill. Without this, that
        # OTHER migration's ALTER ADD COLUMN would hit "duplicate column
        # name" (the column already exists, added by THIS rebuild) and skip
        # its own backfill, silently leaving weight_est=0 forever — this
        # rebuild is the only place that still knows the column was just
        # introduced for these rows.
        if not has_weight_holder_cols:
            rows = conn.execute("SELECT id, name, description FROM things").fetchall()
            for thing_id, name, description in rows:
                conn.execute(
                    "UPDATE things SET weight_est=? WHERE id=?",
                    (compute_weight_est(name, description), thing_id),
                )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def _migrate_events_kind_check(conn: sqlite3.Connection) -> None:
    """Widen events.event_kind's CHECK constraint to admit 'advance_ref'
    (umwelt #186 v2 spec comment) and, as of world-content-timestamps.md
    slice 2, 'update' too — same table-rebuild pattern as
    _migrate_things_kind_check (SQLite has no ALTER for a CHECK). Idempotent:
    a fresh DB already gets the widened CHECK straight from SCHEMA_SQL's
    CREATE TABLE, and an already-migrated DB is detected via sqlite_master
    and skipped, so running this on every init_db call is a cheap no-op
    after the first real migration. Preserves the UNIQUE(event_kind,
    source_ref) idempotency constraint and both indexes (idx_events_actor,
    idx_events_place) across the rebuild — every pre-existing events row is
    carried over unchanged (dry-run-verified: create-new -> copy -> drop ->
    rename, no destructive edit to any existing row).

    Extended (not duplicated) for slice 2: a DB that already has
    'advance_ref' but predates 'update' must still be rebuilt — so the
    "already widened" check requires BOTH members present, not just the
    first one this migration ever added.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    if row is None or row[0] is None:
        return  # events table doesn't exist — SCHEMA_SQL above should have made it
    if "'advance_ref'" in row[0] and "'update'" in row[0]:
        return  # already fully widened (fresh DB, or a prior run of this migration)

    # PRAGMA foreign_keys is a no-op while a transaction is open, so both
    # PRAGMAs sit OUTSIDE the explicit transaction; the rebuild itself runs
    # under BEGIN IMMEDIATE so a crash mid-rebuild (between DROP and RENAME)
    # rolls back atomically instead of leaving no events table (Borges
    # security round-1, PR #195 — crash-atomicity made explicit rather than
    # inherited from sqlite3's implicit-txn behavior).
    conn.commit()  # close any implicit txn so the PRAGMA takes effect
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE events_new ("
            "    id            TEXT PRIMARY KEY,"
            "    event_kind    TEXT NOT NULL "
            "        CHECK(event_kind IN ('crossing','use','act','advance_ref','update')),"
            "    source_ref    TEXT NOT NULL,"
            "    actor         TEXT NOT NULL,"
            "    place_id      TEXT,"
            "    occurred_at   TEXT NOT NULL,"
            "    recorded_at   TEXT NOT NULL,"
            "    body          TEXT,"
            "    body_hash     TEXT NOT NULL,"
            "    attesting_authority TEXT NOT NULL,"
            "    retention     TEXT NOT NULL DEFAULT 'citable'"
            "                      CHECK(retention IN ('citable')),"
            "    UNIQUE(event_kind, source_ref)"
            ")"
        )
        cols = ("id, event_kind, source_ref, actor, place_id, occurred_at, "
                "recorded_at, body, body_hash, attesting_authority, retention")
        conn.execute(f"INSERT INTO events_new ({cols}) SELECT {cols} FROM events")
        conn.execute("DROP TABLE events")
        conn.execute("ALTER TABLE events_new RENAME TO events")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_place ON events(place_id)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_weight_and_holder_cols(conn: sqlite3.Connection) -> None:
    """Add weight_est + the holder axis (holder_kind/holder_id) to `things`
    (umwelt #88 slice 1), idempotently, and backfill weight_est for rows
    that predate the column.

    Plain ALTER ADD COLUMN (not the table-rebuild pattern
    _migrate_things_kind_check uses) — same idiom as the #28 lifecycle-cols
    migration above: "duplicate column name" means a fresh DB's SCHEMA_SQL
    (or a prior run of this migration) already added it, which is expected
    and skipped. holder_kind's CHECK is accepted on ADD COLUMN the same way
    the lifecycle status CHECK is (see that migration's comment for the
    empirical verification).

    Backfill: only weight_est needs one — it is the one column whose value
    depends on existing data (name/description), unlike status/binned_at/
    landfilled_at (which correctly default to 'live'/NULL for pre-existing
    rows) or holder_kind/holder_id (which correctly default to 'place'/NULL
    — an existing thing was always "at rest in its place", never carried).
    Runs ONLY when weight_est was just added by this call (added_weight),
    so it never re-scans the whole table on every init_db call.
    """
    added_weight = False
    try:
        conn.execute("ALTER TABLE things ADD COLUMN weight_est INTEGER NOT NULL DEFAULT 0")
        added_weight = True
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise

    for stmt in (
        # 'container' (umwelt #88 slice 2) is included here even though this
        # column-add predates the container feature on most real DBs —
        # ADD COLUMN creates a BRAND NEW CHECK (no widening involved, unlike
        # _migrate_things_kind_check's rebuild), so there's no cost to
        # writing the FINAL 3-way CHECK straight away; it just means a DB
        # that hits this ALTER after already being on slice-2 code never
        # needs a second rebuild pass for holder_kind specifically.
        "ALTER TABLE things ADD COLUMN holder_kind TEXT NOT NULL DEFAULT 'place' "
        "CHECK(holder_kind IN ('place','avatar','container'))",
        "ALTER TABLE things ADD COLUMN holder_id TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise

    if added_weight:
        rows = conn.execute("SELECT id, name, description FROM things").fetchall()
        for thing_id, name, description in rows:
            conn.execute(
                "UPDATE things SET weight_est=? WHERE id=?",
                (compute_weight_est(name, description), thing_id),
            )
    conn.commit()


def _migrate_lane_col(conn: sqlite3.Connection) -> None:
    """Add `lane` (umwelt #278: sky/land/road render-band placement) to
    `things`, idempotently, and backfill avatar/body-kind rows to `'road'`
    for DBs that predate the column.

    Same idiom as `_migrate_weight_and_holder_cols` above: plain
    `ALTER TABLE ... ADD COLUMN ... CHECK(...)` (accepted the same way the
    lifecycle/holder CHECKs are on a brand-new column — see those
    migrations' comments for the empirical verification), guarded by
    `added_lane` so the backfill below only ever runs in the same call
    where the ALTER actually executed.

    Backfill: unlike weight_est (which needs a per-row COMPUTED value), most
    pre-existing rows are already correct straight off the ALTER's own
    `DEFAULT 'land'` clause (SQLite backfills the stated default for every
    existing row on `ADD COLUMN ... DEFAULT ...`) — unlike the earlier
    migrations though, ONE existing kind (avatars/body-kind rows) needs a
    DIFFERENT value than that default, so this migration's only job is the
    conditional avatar correction, not a full-table recompute.

    Idempotency: gated on `added_lane` (this call's own ALTER having just
    run), so a repeated `init_db` on an already-migrated DB never re-fires
    the `UPDATE ... WHERE kind IN (...)` and clobbers a lane a user has
    since changed to `'sky'` back to `'road'`.
    """
    added_lane = False
    try:
        conn.execute(
            "ALTER TABLE things ADD COLUMN lane TEXT NOT NULL DEFAULT 'land' "
            "CHECK(lane IN ('sky','land','road'))"
        )
        added_lane = True
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise

    if added_lane:
        conn.execute(
            f"UPDATE things SET lane = 'road' WHERE kind IN "
            f"({_BODY_KIND_PLACEHOLDERS})",
            _BODY_KINDS,
        )
    conn.commit()


# --- places ---------------------------------------------------------------

def upsert_place(conn, id: str, kind: str, owner: str | None, label: str,
                 description: str = "", parent_id: str | None = None) -> None:
    """Insert a place; existing rows are left unchanged (INSERT OR IGNORE).

    `parent_id=None` (the default) runs the exact statement this function has
    always run, so every existing caller writes byte-identical rows; a
    non-None parent adds the parent_id column to the insert (umwelt#1043)."""
    if parent_id is None:
        conn.execute(
            "INSERT OR IGNORE INTO places(id, kind, owner, label, "
            "description) VALUES(?, ?, ?, ?, ?)",
            (id, kind, owner, label, description),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO places(id, kind, owner, label, "
            "description, parent_id) VALUES(?, ?, ?, ?, ?, ?)",
            (id, kind, owner, label, description, parent_id),
        )
    conn.commit()


def create_place(conn, id: str, kind: str, owner: str | None, label: str,
                 description: str = "") -> bool:
    """Insert a NEW place (umwelt #99, create_place). Unlike upsert_place's
    INSERT OR IGNORE (birth-home idempotency), a resident opening a place has
    no legitimate "already exists, leave it alone" case — the route
    pre-checks id collisions itself (409), but the pre-check-then-insert
    still leaves a true interleaved race (both callers pass place_exists
    before either inserts).

    Returns True if the place was created, False if the insert lost the
    race — i.e. sqlite3.IntegrityError on the TEXT PRIMARY KEY because
    someone else's create_place for the same id won first between the
    route's pre-check and this insert (reviewer round-1, PR #101: without
    the catch, the loser gets an unhandled 500 instead of the same 409 the
    sequential-duplicate path returns). Same catch-in-db.py idiom as
    get_or_create_avatar's birth race (db.py ~940-960); same
    bool-sentinel-for-race-loss convention as take_thing's guarded UPDATE
    (server.py's race-handling siblings).

    Stamps created_at (world-content-timestamps design, slice 1) —
    engine-derived via now_iso(), never caller-supplied (R2). upsert_place
    (the birth-home/genesis-place path) deliberately does NOT stamp this;
    only a resident-opened place gets a created_at going forward, per the
    design's slice-1 scope.
    """
    try:
        conn.execute(
            "INSERT INTO places(id, kind, owner, label, description, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (id, kind, owner, label, description, now_iso()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return True


def get_place(conn, place_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, kind, owner, label, description, parent_id, metadata, "
        "created_at, updated_at "
        "FROM places WHERE id = ?",
        (place_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "kind": row[1], "owner": row[2],
            "label": row[3], "description": row[4], "parent_id": row[5],
            # umwelt place-lane-tiles spec: appended at the END, same
            # positional-index convention _thing_row_to_dict's
            # weight_est/holder_kind/holder_id/lane additions already use —
            # any code indexing this SELECT's tuple positionally must index
            # past all 9 columns, not the pre-world-content-timestamps 7.
            "metadata": json.loads(row[6]) if row[6] else {},
            # world-content-timestamps design, slice 1: engine-stamped,
            # NULL means "unknown"/"never modified" honestly (R1) — never
            # rendered, formatted, or defaulted here.
            "created_at": row[7], "updated_at": row[8]}


# Columns an agent may change via update_place (umwelt place-lane-tiles
# spec). Unlike _MUTABLE_THING_COLS, this is a single-field allowlist —
# places have no equivalent to a thing's name/description/exec_command
# (those go through create_place/set_parent's own narrowly-scoped routes
# instead); metadata is the ONLY generic field, so this is the whole
# write surface. Same injection-safety posture as _MUTABLE_THING_COLS:
# a fixed constant, never caller-supplied, so the SQL below is safe with
# parameterized values.
_MUTABLE_PLACE_COLS = ("metadata",)


# ---------------------------------------------------------------------------
# world-content-timestamps design, slice 1 round 3 (R3) — shared value-diff
# helper for update_place/update_thing.
#
# Both write paths need the SAME "did this patch actually change anything"
# predicate before deciding whether to stamp updated_at, so it lives here
# once instead of as two independently-maintained near-identical blocks —
# the scattered-same-concept failure mode this codebase has been bitten by
# before (a fix landing on one copy and not the other, or a third caller
# copying whichever copy it finds first).
#
# Accepted read-then-write race: this reads current values, the caller
# decides what changed, then the caller issues its own UPDATE — with no
# transaction wrapping the read+decide+write. That's a real gap (contrast
# with the #166 cycle guards further down this module — see
# _atomic_guarded_write — which DO wrap check+write in BEGIN IMMEDIATE),
# but it's benign here rather than closed: the generated SET clause only
# ever contains columns THIS read showed as genuinely differing, so a
# concurrent writer's value for any OTHER column can never be clobbered by
# this path. The realistic worst case is a missed-or-duplicated updated_at
# stamp when two writers race on the SAME column inside the read/write gap
# — acceptable for a column the design already frames as advisory (R1:
# NULL means "unknown," never authoritative). Deliberately not closed with
# BEGIN IMMEDIATE: unlike the #166 cycle predicate (a reachability/
# transitive-closure check that can't live in a WHERE clause, which is why
# that fix needs the lock), the worst outcome here is a stale-but-harmless
# timestamp, not a corrupted invariant.
# ---------------------------------------------------------------------------

def _changed_cols(conn, table: str, row_id: str, candidate_cols: list[str],
                   patch: dict) -> list[str]:
    """Read `table`'s current values for `candidate_cols` on `row_id` and
    return the subset whose patch value genuinely differs from what's
    currently stored — the filter update_place and update_thing both apply
    before deciding whether to stamp updated_at (R3: a no-op patch does not
    bump the timestamp). `table` is always a fixed caller-supplied literal
    ("places" or "things"), never user input, so the f-string is safe with
    the same injection-safety posture as _MUTABLE_PLACE_COLS/
    _MUTABLE_THING_COLS above.

    `metadata` is compared as a decoded dict (`json.loads(current) !=
    patch["metadata"]`), never as raw JSON strings, since re-serialization
    can reorder keys and produce a false "changed" on byte-identical
    content.

    Returns [] if row_id doesn't exist, if candidate_cols is empty, or if
    every candidate column's patch value matches its current value.
    """
    if not candidate_cols:
        return []
    current = conn.execute(
        f"SELECT {', '.join(candidate_cols)} FROM {table} WHERE id = ?",
        (row_id,),
    ).fetchone()
    if current is None:
        return []
    cols = []
    for i, c in enumerate(candidate_cols):
        if c == "metadata":
            current_val = json.loads(current[i]) if current[i] else {}
        else:
            current_val = current[i]
        if current_val != patch[c]:
            cols.append(c)
    return cols


def update_place(conn, place_id: str, patch: dict, actor: str) -> None:
    """Set the given mutable columns on a place. Only keys in
    _MUTABLE_PLACE_COLS are applied — mirrors update_thing's column-list
    injection-safety posture exactly (fixed constant, parameterized
    values). Caller (server.py's update_place route) validates the patch
    shape + authorization (owner-only, or any agent for an engine-genesis
    place with no owner). No-op if nothing mutable is set.

    Stamps updated_at (world-content-timestamps design, slice 1) — but only
    when the patch resolves to a GENUINE value change on at least one
    mutable column (R3: a no-op patch does not bump the timestamp). Key
    presence in `patch` is not enough — the candidate columns' CURRENT
    values are read back and compared against the patch's values; a column
    whose new value equals its current value is dropped before the UPDATE
    runs. `metadata` is compared as a decoded dict (`json.loads(current) !=
    patch["metadata"]`), never as raw JSON strings, since re-serialization
    can reorder keys and produce a false "changed" on byte-identical
    content. If every candidate column drops out this way, nothing is
    touched and no stamp happens — same early-out as the empty-patch case.
    updated_at itself is never in _MUTABLE_PLACE_COLS, so a caller cannot
    smuggle a value for it through `patch` (R2).

    `actor` (world-content-timestamps.md slice 2) is the caller-supplied
    acting agent — author-checked by the route, same trust-scoped posture
    as `graft_advances`'s `agent` column, never cryptographically
    authenticated. REQUIRED, never defaulted, so no call site can silently
    skip stamping tier 1. On a genuine change, writes one `content_updates`
    row in the SAME transaction as the column UPDATE (same posture as
    graft_advances's metadata-UPDATE + provenance-INSERT pair) — never
    calls mint_event; an 'update' events row is minted later, separately,
    only on request via /umwelt/record.
    """
    candidate_cols = [c for c in _MUTABLE_PLACE_COLS if c in patch]
    if not candidate_cols:
        return
    cols = _changed_cols(conn, "places", place_id, candidate_cols, patch)
    if not cols:
        return
    vals = [json.dumps(patch[c]) if c == "metadata" else patch[c] for c in cols]
    set_clause = ", ".join(f"{c} = ?" for c in cols) + ", updated_at = ?"
    stamp = now_iso()
    conn.execute(
        f"UPDATE places SET {set_clause} WHERE id = ?",
        vals + [stamp, place_id],
    )
    conn.execute(
        "INSERT INTO content_updates(id, subject_id, subject_kind, actor, "
        "changed_cols, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
        ("cupd_" + str(uuid.uuid4()), place_id, "place", actor,
         json.dumps(cols), stamp),
    )
    conn.commit()


def place_exists(conn, place_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM places WHERE id = ?", (place_id,)
    ).fetchone() is not None


def list_places(conn) -> list[dict[str, Any]]:
    """All places, with a live count of the things in each (the world map).

    Excludes avatars (bodies) from the count, same rationale as
    things_in_place: a place's "N things here" should count usables, not the
    inhabitant standing there. Pre-existing gap surfaced during #28's review
    (removal-system PR) — the status filter was added there but this
    body-kind exclusion was flagged out of scope and left for this follow-up.

    Also excludes carried AND bagged things (holder_kind != 'place' — umwelt
    #88 slice 1 for 'avatar', slice 2 for 'container') — a thing in someone's
    hands, or zipped into a bag, is not "here" in the room, same pattern as
    the status filter (see things_in_place). A bagged thing's absence here is
    also the closed-bag exposure rule at work: it renders only via look_in.
    """
    rows = conn.execute(
        f"SELECT p.id, p.kind, p.owner, p.label, p.description, "
        f"       (SELECT COUNT(*) FROM things t WHERE t.place_id = p.id "
        f"        AND t.status='live' AND t.holder_kind = 'place' "
        f"        AND t.kind NOT IN ({_BODY_KIND_PLACEHOLDERS})) "
        f"  FROM places p ORDER BY p.kind DESC, p.id ASC",
        _BODY_KINDS,
    ).fetchall()
    return [{"id": r[0], "kind": r[1], "owner": r[2], "label": r[3],
             "description": r[4], "thing_count": r[5]} for r in rows]


# The Workshop's LEGACY place id (umwelt #99's boot-time seed). Kept as a
# constant only for tests that import it and as the fallback of
# role_place_id for worlds that predate world packages (umwelt#1043). No
# module other than db.py may name it: ask for the "workshop" role instead.
WORKSHOP_PLACE_ID = "place_workshop"

# umwelt#1043: roles name the places the engine itself leans on. A world
# package assigns them (world_roles); a world with no row for a role (every
# world that predates packages) falls back to the legacy id below.
_LEGACY_ROLE_FALLBACK = {"hub": "place_square", "workshop": WORKSHOP_PLACE_ID}


class UnknownRole(KeyError):
    """role_place_id was asked for a role with no row and no legacy id."""

    def __str__(self) -> str:
        return f"unknown world role {self.args[0]!r}"


def role_place_id(conn, role: str) -> str:
    """The place id playing `role` in this world: its world_roles row, else
    the legacy id for 'hub'/'workshop', else UnknownRole. Never writes."""
    try:
        row = conn.execute(
            "SELECT place_id FROM world_roles WHERE role = ?", (role,)
        ).fetchone()
    except sqlite3.OperationalError:  # table absent: not yet init_db'd
        row = None
    if row is not None:
        return row[0]
    if role in _LEGACY_ROLE_FALLBACK:
        return _LEGACY_ROLE_FALLBACK[role]
    raise UnknownRole(role)


def role_place_id_readonly(db_path, role: str = "hub") -> str:
    """`role_place_id` for the CLIs (GC notice, backup alarm) that only hold a
    database PATH. Opens it read-only (`mode=ro`: never creates the file,
    never writes) and returns the legacy fallback on ANY problem (file
    missing/unreadable, table or row absent). Never raises."""
    fallback = _LEGACY_ROLE_FALLBACK.get(role, "place_square")
    try:
        from urllib.parse import quote
        uri = "file:" + quote(os.path.abspath(str(db_path))) + "?mode=ro"
        c = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            row = c.execute(
                "SELECT place_id FROM world_roles WHERE role = ?", (role,)
            ).fetchone()
        finally:
            c.close()
        return row[0] if row else fallback
    except Exception:
        return fallback


# The four places seed_places has always created, in its historical order.
# Their DATA (label, description) now lives in the shipped world package;
# only the ids are named here so the legacy call stays a four-place seed no
# matter how much a later package adds.
_LEGACY_SEED_PLACE_IDS = ("place_square", "place_workshop", "place_library",
                          "place_landfill")


def missing_legacy_default_places(conn) -> list:
    """The legacy default place ids this world lacks, for the boot warning.
    Empty unless the world predates packages (no world_roles rows) AND has
    places already: a fresh world is loaded from its package, a packaged
    world declares its own places. Never writes."""
    try:
        if conn.execute("SELECT 1 FROM world_roles LIMIT 1").fetchone():
            return []
        if conn.execute("SELECT 1 FROM places LIMIT 1").fetchone() is None:
            return []
        have = {r[0] for r in conn.execute("SELECT id FROM places")}
    except sqlite3.OperationalError:
        return []
    return [p for p in _LEGACY_SEED_PLACE_IDS if p not in have]


def _shipped_places_by_id() -> dict:
    from . import world_package
    pkg = world_package.load_package(world_package.find_shipped_package())
    return {p["id"]: p for p in pkg.places}


def seed_places(conn, agent_names: list[str]) -> None:
    """LEGACY seed: the four default public places + a Post per agent.

    Kept (signature and semantics) for the many callers that build a world
    with `init_db` + `seed_places`: INSERT OR IGNORE, safe to repeat and safe
    on a non-empty database. The four places' data come from the shipped
    world package (single source of truth). Writes NO roles and NO
    provenance row (role lookup falls back to the legacy ids). The server
    no longer boots through this: it loads the package once, on a fresh
    world, via world_package.boot_load (umwelt#1043).
    """
    shipped = _shipped_places_by_id()
    for pid in _LEGACY_SEED_PLACE_IDS:
        p = shipped[pid]
        upsert_place(conn, p["id"], p["kind"], p["owner"], p["label"],
                     p["description"])
    seed_posts(conn, agent_names)


def residences_place_id(conn) -> str | None:
    """The place playing the `residences` role (the parent of every
    resident's Post), or None when this world has none -- which is every
    world that predates format-2 packages, and every package that does not
    name the role. Never writes, never falls back to a legacy id: absence is
    the answer, and Post creation is then byte-identical to what it always
    was (umwelt#1043)."""
    try:
        return role_place_id(conn, "residences")
    except UnknownRole:
        return None


def seed_posts(conn, agent_names: list[str]) -> None:
    """A private Post per agent (INSERT OR IGNORE, safe every boot). Posts
    are parented under the world's `residences` place when it has one, and
    stay parentless otherwise."""
    parent = residences_place_id(conn)
    for agent in agent_names:
        upsert_place(
            conn, f"place_post_{agent}", "private", agent,
            f"{agent.title()}'s Post",
            f"{agent.title()}'s private corner of the world.",
            parent_id=parent,
        )


# --- place topology (umwelt #158) ------------------------------------------
#
# parent_id (a place's "way up") + place_links (lateral, directed adjacency)
# are the two relations `place_tree` walks. `place_tree` is THE one engine
# function every projection (where_can_i_go = depth=1 from here, the map
# (#160) = depth=inf from root, the Discussion Hall room-list (#159) =
# depth=1 from an area) consumes — ratified on issue #158 (Kepler, forum
# #255) precisely so unread math and private-place visibility can't drift
# across surfaces.
#
# Visibility (umwelt#538, revision 3 of the privacy design — supersedes the
# #158/#164 full-prune history below): a place's EXISTENCE (id, label, kind,
# owner, access_granted — see _place_public_projection) is visible to every
# viewer, private or not. What `_place_interior_visible_to` gates now is narrower:
# whether a node's INTERIOR annotations (thing_count, unread) are populated
# with real values or a safe placeholder. See `_place_tree_node`'s
# `interior_visible` field for the exact mechanics.
#
# History, kept because the reasoning it overturns is worth having on record
# (also see _place_interior_visible_to's own docstring): catch #1's original framing
# (null out thing_count/unread, keep the node itself visible) was Ariadne's
# reconciliation of Borges's initial "prune the node" proposal, for the flat
# non-recursive today's-list. Borges's live follow-up flagged that the map
# (#160) walks the WHOLE tree at depth=inf, so a link/parent_id pointing
# from a public place into a private one would leak the private place's
# existence+label+owner to ANY viewer — the "god view" the team has a
# standing prior against (#199). The team's fix at the time was full
# existence-pruning. Lei's rev-3 ruling (umwelt#538) deliberately reverses
# that: existence disclosure is now the specification, not the leak — "a
# locked door you can see is a door you can ask for a key to." The old
# behaviour was correct for the old requirement; the requirement changed.

_PLACE_TREE_DEPTH_CAP = 64  # absolute safety ceiling on place_tree's
# recursion regardless of the requested `depth` (umwelt #158 catch #2,
# belt-and-suspenders) — the REAL cycle guard is set_place_parent's
# write-time check below; this only bounds a read-side walk in case a
# pre-existing corrupt parent_id row (or a caller passing depth=inf for the
# #160 map) would otherwise recurse forever.

_MAX_ANCESTOR_WALK = 200  # defensive cap on _place_ancestor_ids' upward
# walk — same belt-and-suspenders posture as _PLACE_TREE_DEPTH_CAP above;
# the real guard against a cycle existing at all is would_create_cycle,
# called by every write path (set_place_parent) BEFORE a cycle can be
# created, so this cap should never actually bind in practice.


def get_place_parent(conn, place_id: str) -> dict[str, Any] | None:
    """The parent place row, or None if place_id doesn't exist, or is
    top-level (parent_id NULL)."""
    place = get_place(conn, place_id)
    if place is None or place["parent_id"] is None:
        return None
    return get_place(conn, place["parent_id"])


def get_place_children(conn, place_id: str) -> list[dict[str, Any]]:
    """Direct children of place_id (places where parent_id = place_id),
    ordered by id for deterministic rendering."""
    rows = conn.execute(
        "SELECT id, kind, owner, label, description, parent_id FROM places "
        "WHERE parent_id = ? ORDER BY id",
        (place_id,),
    ).fetchall()
    return [{"id": r[0], "kind": r[1], "owner": r[2], "label": r[3],
             "description": r[4], "parent_id": r[5]} for r in rows]


def structural_child_ids(conn, parent_id: str) -> set[str]:
    """Ids of the direct children of `parent_id` whose OWN `metadata.structural`
    is literally True (umwelt #1084; the string 'true', 1 and a non-empty dict
    are not the flag, the same strictness as `metadata.project_register`).
    One query. Metadata that is not a JSON object (or not JSON) is not the
    flag. get_place_children does not carry metadata, so this is the one
    place the register reads it."""
    out: set[str] = set()
    for pid, raw in conn.execute(
            "SELECT id, metadata FROM places WHERE parent_id = ?",
            (parent_id,)):
        try:
            meta = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("structural") is True:
            out.add(pid)
    return out


def get_place_links(conn, place_id: str) -> list[dict[str, Any]]:
    """Lateral links FROM place_id (rows where src_id = place_id) — each
    `{dst_id, label}`. Directed; a "mutual" link is two rows, one per
    direction (see add_place_link) — this only ever returns the src=place_id
    half."""
    rows = conn.execute(
        "SELECT dst_id, label FROM place_links WHERE src_id = ? "
        "ORDER BY dst_id",
        (place_id,),
    ).fetchall()
    return [{"dst_id": r[0], "label": r[1]} for r in rows]


def _place_ancestor_ids(conn, place_id: str) -> list[str]:
    """Walk parent_id pointers upward from place_id, returning the chain of
    ancestor ids (nearest-first, place_id itself excluded). Stops at the
    first top-level place (parent_id NULL), or defensively after
    _MAX_ANCESTOR_WALK hops / on revisiting an id already seen (a
    pre-existing corrupt cycle — shouldn't happen once would_create_cycle
    guards every write, but a read-side walk must never hang)."""
    ancestors: list[str] = []
    seen = {place_id}
    current = place_id
    for _ in range(_MAX_ANCESTOR_WALK):
        row = conn.execute(
            "SELECT parent_id FROM places WHERE id = ?", (current,)
        ).fetchone()
        if row is None or row[0] is None:
            break
        parent_id = row[0]
        if parent_id in seen:
            break  # defensive: pre-existing corrupt cycle, stop, don't loop
        ancestors.append(parent_id)
        seen.add(parent_id)
        current = parent_id
    return ancestors


# ---------------------------------------------------------------------------
# umwelt #166 — atomic check-then-write cycle guards.
#
# The server runs threaded (Flask's default threaded=True); each request
# gets its own sqlite3 connection (server.py's before_request), and
# PRAGMA busy_timeout serializes physical WRITES but does nothing about a
# check-then-write PAIR spanning two statements/two connections. Two
# concurrent requests could both run the "would this create a cycle?"
# read before either ran the write, and both then commit — a cycle
# neither alone would have been allowed to create (demonstrated).
#
# Fix: co-locate the check + the write inside ONE
# BEGIN IMMEDIATE transaction in the db-layer write function itself, and
# raise CycleError (rollback first) on a positive check instead of the
# caller pre-checking and then calling a separate write function. This is
# NOT the compare-and-swap-in-a-WHERE-clause pattern advance_capsule_ref
# uses (db.py ~2407) — a CAS works there because "is the ref still what I
# expect" is a column-equality expressible at write time. A cycle check is
# a reachability/transitive-closure predicate over a whole table; it
# cannot live in a WHERE clause. BEGIN IMMEDIATE takes sqlite's write
# (RESERVED) lock BEFORE the reachability read runs, which is what closes
# the race: no other writer can interleave between the check and the
# write once the lock is held.
# ---------------------------------------------------------------------------

class CycleError(ValueError):
    """Raised by an atomic-guarded write (see _atomic_guarded_write) when
    its cycle predicate is True. Subclasses ValueError so existing
    db-level callers/tests that catch/raise ValueError for this case keep
    working; server.py routes catch CycleError specifically and map it to
    409 with the exact message text the route used to produce itself."""


def _atomic_guarded_write(conn, would_cycle, do_write, error_message: str) -> None:
    """Run `would_cycle(conn) -> bool` and `do_write(conn) -> None` as ONE
    atomic check-then-write unit under an explicit BEGIN IMMEDIATE
    transaction (umwelt #166 — see the module comment above this function
    for the race + why BEGIN IMMEDIATE, not a WHERE-clause CAS).

    Rolls back and raises CycleError(error_message) if would_cycle(conn)
    is True; otherwise runs do_write(conn) and commits. Any other
    exception from would_cycle/do_write also rolls back before
    propagating (same rollback-on-any-exception posture as
    advance_capsule_ref).

    Python sqlite3's default (legacy) isolation-level handling
    auto-opens/tracks transactions around DML and gets confused by a
    manually-issued BEGIN IMMEDIATE under it, so this function
    temporarily switches `conn` to autocommit mode (isolation_level =
    None) to drive BEGIN/COMMIT/ROLLBACK itself, restoring the
    connection's prior isolation_level in a finally — invisible to the
    caller, and safe because each request owns its own connection
    (server.py's before_request)."""
    prev_isolation_level = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if would_cycle(conn):
                conn.execute("ROLLBACK")
                raise CycleError(error_message)
            do_write(conn)
            conn.execute("COMMIT")
        except CycleError:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = prev_isolation_level


def atomic_authorized_write(conn, is_authorized, do_write) -> bool:
    """Run `is_authorized(conn) -> bool` and `do_write(conn) -> None` as ONE
    atomic check-then-write unit under an explicit BEGIN IMMEDIATE
    transaction — same transaction shape as `_atomic_guarded_write` above
    (umwelt #166; see that function's docstring and the module comment
    above it for the underlying TOCTOU race and why BEGIN IMMEDIATE, not a
    WHERE-clause CAS). Deliberately a SEPARATE function rather than reusing
    `_atomic_guarded_write` directly: that one raises `CycleError` on a
    failed check (a 409-shaped denial) — an authorization refusal is a 403,
    a different exception contract entirely, so this variant returns a
    plain bool instead of raising anything on a failed check.

    Public (no leading underscore), unlike `_atomic_guarded_write` and its
    siblings (`would_create_cycle`, `set_place_parent`, `add_task_dep`):
    every existing use of that pattern keeps BOTH the cycle-check logic and
    the write inside db.py. Here the check callback needs server.py's
    kind-chain / kinddef resolution machinery (`_resolve_update_callers`,
    `_callers_authorized`), which lives in server.py — so server.py is the
    caller (umwelt#300 S0(d): the update_thing route's authorization gate).

    Closes Mira's security acceptance criteria point 6: a concurrent
    kinddef `update_callers` edit landing between the authorization check
    and the write must not let the write land under a policy different
    from the one actually checked. `is_authorized` re-reads whatever state
    it needs to decide (thing row, kind chain) — running it AFTER
    `BEGIN IMMEDIATE` has already taken SQLite's write (RESERVED) lock
    closes the race for state that lives IN this database: no other
    connection can commit a conflicting write to the `things` table while
    this one holds the lock, so a concurrent update_thing (e.g. one that
    changes `update_policy_cohort` or the thing's own author) can't
    interleave here.

    Caveat, precisely — the kind-CHAIN policy itself
    (`kinddef.json`'s `update_callers`, resolved by `_resolve_update_callers`)
    is filesystem-backed, not a row in THIS database, so this lock does not
    (and cannot) block a concurrent filesystem write to that file: what's
    guaranteed is that `is_authorized`'s read happens as late as possible,
    immediately before the write it gates, minimizing (not eliminating) the
    window a stale kinddef read could act on — see server.py's update_thing
    route, which already runs a non-atomic pre-check well before this point,
    so this atomic step is strictly the LAST, freshest read before the
    write, not a hold against every possible interleaving of every backing
    store.

    Returns True if `is_authorized(conn)` was True and `do_write(conn)`
    ran (transaction committed); False if `is_authorized(conn)` was False
    (transaction rolled back; `do_write` never runs). Any other exception
    from either callback rolls back before propagating."""
    prev_isolation_level = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not is_authorized(conn):
                conn.execute("ROLLBACK")
                return False
            do_write(conn)
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = prev_isolation_level


def would_create_cycle(conn, place_id: str, new_parent_id: str) -> bool:
    """True if setting place_id's parent to new_parent_id would make
    place_id its own ancestor, directly or transitively. Self-parent
    (new_parent_id == place_id) is the depth-0 instance of this same check
    (umwelt #158 catch #2) — the route (server.py's /umwelt/set_parent) and
    set_place_parent's own defense-in-depth re-check both call this ONE
    predicate, so the ancestor-walk logic never has to be duplicated."""
    if new_parent_id == place_id:
        return True
    return place_id in _place_ancestor_ids(conn, new_parent_id)


def set_place_parent(conn, place_id: str, parent_id: str | None) -> None:
    """Set place_id's parent_id (None clears it back to top-level).

    Existence re-checks (umwelt #158 catch #2): the caller (server.py's
    /umwelt/set_parent route) is expected to validate first — place_id/
    parent_id both exist (404 otherwise) — so a caller gets a clean HTTP
    error; this function re-asserts both as defense-in-depth, same posture
    as trash_thing/update_thing's own re-checks elsewhere in this module
    (raises plain ValueError, uncaught here — a 500 if it ever fires in
    practice, which it shouldn't once the route has pre-validated).

    Cycle guard (umwelt #166): the would_create_cycle check and the parent
    UPDATE run as ONE atomic unit under _atomic_guarded_write — closes the
    TOCTOU race where two concurrent set_place_parent calls could each
    pass the check before either wrote (see the #166 module comment above
    would_create_cycle). Raises CycleError (a ValueError subclass, so the
    existing `except ValueError` callers keep working) instead of a plain
    ValueError for this specific case — server.py's route catches
    CycleError and maps it to 409 with the same message text as before.
    """
    if not place_exists(conn, place_id):
        raise ValueError(f"place {place_id!r} not found")
    if parent_id is None:
        conn.execute(
            "UPDATE places SET parent_id = ? WHERE id = ?",
            (parent_id, place_id),
        )
        conn.commit()
        return
    if not place_exists(conn, parent_id):
        raise ValueError(f"parent place {parent_id!r} not found")

    def _would_cycle(c) -> bool:
        return would_create_cycle(c, place_id, parent_id)

    def _write(c) -> None:
        c.execute(
            "UPDATE places SET parent_id = ? WHERE id = ?",
            (parent_id, place_id),
        )

    _atomic_guarded_write(
        conn, _would_cycle, _write,
        f"setting {place_id!r}'s parent to {parent_id!r} would "
        "create a cycle",
    )


def add_place_link(conn, src_id: str, dst_id: str, label: str | None = None,
                    mutual: bool = True, *, commit: bool = True) -> None:
    """Add a lateral link FROM src_id TO dst_id (umwelt #158). Idempotent —
    re-adding an existing (src_id, dst_id) link overwrites its label rather
    than erroring (INSERT ... ON CONFLICT DO UPDATE), matching the issue's
    explicit "idempotent re-add of the same link shouldn't error" test
    requirement.

    `mutual=True` (the common case — "storing directed; creating a mutual
    link writes both rows", per the issue) ALSO writes the reverse
    (dst_id, src_id) row with the same label. `mutual=False` writes only the
    one directed row. No trigger-based mirroring — mutuality is a
    route/caller-level convention over a directed table, per the issue's
    explicit design.

    `commit=False` (keyword-only; the default True is today's behaviour)
    leaves the transaction open for a caller that owns it -- the world
    package loader, which must stay all-or-nothing (umwelt#1043).
    """
    conn.execute(
        "INSERT INTO place_links(src_id, dst_id, label) VALUES(?, ?, ?) "
        "ON CONFLICT(src_id, dst_id) DO UPDATE SET label = excluded.label",
        (src_id, dst_id, label),
    )
    if mutual:
        conn.execute(
            "INSERT INTO place_links(src_id, dst_id, label) VALUES(?, ?, ?) "
            "ON CONFLICT(src_id, dst_id) DO UPDATE SET label = excluded.label",
            (dst_id, src_id, label),
        )
    if commit:
        conn.commit()


def remove_place_link(conn, src_id: str, dst_id: str,
                       mutual: bool = True) -> None:
    """Remove the (src_id -> dst_id) lateral link; with mutual=True
    (default) also removes the reverse (dst_id -> src_id) row. A silent
    no-op (no error) if the row(s) don't exist — matches add_place_link's
    idempotent posture."""
    conn.execute(
        "DELETE FROM place_links WHERE src_id = ? AND dst_id = ?",
        (src_id, dst_id),
    )
    if mutual:
        conn.execute(
            "DELETE FROM place_links WHERE src_id = ? AND dst_id = ?",
            (dst_id, src_id),
        )
    conn.commit()


def _place_interior_visible_to(place: dict[str, Any], viewer: str | None) -> bool:
    """What this does NOT answer: "can the viewer see this place exists."
    That question is now unconditionally True for every viewer (umwelt#538
    rev 3, rule 2) — a place's existence, label, kind, owner, and grant
    list are public regardless of what this function returns. This
    function answers the narrower question its name says: can_ENTER,
    never can_see. It gates a place's INTERIOR — description, contents,
    occupants, crossings — and its entry gate (`move_to`).

    (Renamed from `_place_visible_to` at Ariadne's catch: the old name
    answered "is this place visible at all," which was exact before rule 2
    and became a false name after it — this function's own logic never
    changed, only the meaning "visible" had to carry did. The merged
    design doc's implementers' note exists for exactly this: "a rule that
    consults `can_see` is a rule with no gate in it." If a future call
    site is tempted to ask this function "is X visible" instead of "may X
    enter/read the interior," that confusion is what the rename is for.)

    A public place's interior is open to everyone. A private place's
    interior — description, contents, occupants, crossings — and its entry
    gate (`move_to`) are open ONLY to its owner (grantees once #538 part C's
    `place_grants` ships; there are none today, so this remains owner-only in
    practice). `viewer=None` (no identity context at all) gets interior
    access to public places only.

    This predicate no longer decides whether a place NODE is emitted at all
    — under Lei's rev-3 ruling (umwelt#538, docs/design/privacy-authorization.md
    §3.1 rule 2) a place's existence (`id`, `label`, `kind`, `owner`,
    `access_granted`) is public regardless of this predicate's answer.
    `place_tree`/`list_top_level_places` call this ONLY to decide whether to
    populate a node's INTERIOR annotations (`thing_count`, `unread`) — see
    their docstrings — never to prune the node itself. `_read_place`
    (`look`/`place_view`) and `move_to` still gate their FULL-interior read
    on this predicate unchanged: those routes return the whole interior or
    none of it, so there is no partial projection to thread through them.

    (Original #158 catch #1 history, corrected per Borges's live
    map-god-view follow-up: a private place used to be full-pruned —
    existence, label, owner, counts, all of it — for any non-owner. That was
    the deliberate, correct behaviour for the OLD requirement; Lei's rev-3
    ruling changed the requirement, not this function's fitness for the old
    one. See `_place_public_projection`/`format_place_marker` for what
    replaced the pruning.)
    """
    if place["kind"] != "private":
        return True
    return viewer is not None and place["owner"] == viewer


def _place_public_projection(place: dict[str, Any]) -> dict[str, Any]:
    """The always-visible projection of a place (umwelt#538 rule 2): `id`,
    `label`, `kind`, `owner`, `access_granted`, `marker`. Same shape
    regardless of who is asking — that is the whole point of "public
    projection." Every listing surface (`place_tree`, `list_top_level_places`,
    the map, the `reachable`/`parent` annotations) emits at least this much
    for every place, interior gating notwithstanding.

    `access_granted` is always `[]` today — there is no `place_grants` table
    (umwelt#538 part C, not built; verified against `origin/main` and the
    live prod DB). Once C ships, this becomes a real grantee lookup; until
    then every place's grant list is honestly empty, not a placeholder that
    hides a real answer.

    `marker` (umwelt#543) is `format_place_marker(place)` computed HERE — the
    single server-side point where the marker string enters every projection
    this function feeds (`place_tree` nodes, `parent` in `_place_view`).
    umwelt#543's own docstring/design note: the three HTTP-client consumer
    surfaces (`mud.py`, `mcp_server.py`, `webrender/gateway.py`) cannot import
    `db.py` across the process boundary, so the marker grammar can only be
    computed once, here, and shipped as a plain string field — never
    reconstructed client-side.
    """
    return {
        "id": place["id"],
        "label": place["label"],
        "kind": place["kind"],
        "owner": place["owner"],
        "access_granted": [],
        "marker": format_place_marker(place),
    }


def format_place_marker(place: dict[str, Any]) -> str:
    """THE authorization marker (umwelt#538 §3.4) — one helper, not
    per-surface formatting, so the map/`look_around`/every other place
    listing renders identically rather than drifting apart. Grammar, not
    literals: `[state]` alone for `kind == "public"`; `[state][owner: X]`
    for private with no grants; `[state][owner: X][access granted: a,b]`
    once a non-empty grant list exists (umwelt#538 part C — not built yet,
    so this clause never renders today, per `_place_public_projection`).

    Renders the engine's own stored `kind` VALUE — `f"[{place['kind']}]"` —
    literally interpolated, never a mapped display word like `'locked'` for
    `'private'`. Lei's own reasoning (§3.4): `places.kind` is a
    CHECK-constrained enum, and a renderer holding its own vocabulary for
    it is a second registry of the same fact, free to drift from the
    first. Interpolating rather than branching on `kind == "public"` vs. a
    hardcoded `"[private]"` literal is what actually delivers the doc's own
    promised property — "a third kind, if one ever lands, needs no render
    change" — a hardcoded literal string would need exactly that change.
    `place` may be a full place row or any dict carrying at least
    `kind`/`owner`/`access_granted` (e.g. a `_place_public_projection`
    result or a `place_tree` node).

    `_place_public_projection` (above) is the SSoT call site for this helper
    on every place-shaped dict this module hands out — `place_tree` nodes,
    `parent` in `_place_view`, and (umwelt#543) each link entry's `dst_marker`
    in `_place_tree_node`. Call `format_place_marker` directly only when
    working from a raw row that hasn't gone through the projection yet.
    """
    marker = f"[{place['kind']}]"
    if place["kind"] == "public":
        return marker
    marker += f"[owner: {place['owner']}]"
    access_granted = place.get("access_granted") or []
    if access_granted:
        marker += f"[access granted: {','.join(access_granted)}]"
    return marker


def _place_thing_count(conn, place_id: str) -> int:
    """Live, in-place (not carried/bagged), non-avatar thing count for a
    single place — the SAME filter list_places' subquery uses, factored out
    here for place_tree's per-node annotation (a COUNT query, not a full
    things_in_place row fetch, since place_tree only needs the number)."""
    row = conn.execute(
        f"SELECT COUNT(*) FROM things WHERE place_id = ? AND status='live' "
        f"AND holder_kind = 'place' AND kind NOT IN ({_BODY_KIND_PLACEHOLDERS})",
        (place_id, *_BODY_KINDS),
    ).fetchone()
    return row[0] if row else 0


def place_tree(conn, from_id: str, depth: int | float,
                viewer: str) -> list[dict[str, Any]]:
    """THE tree-render engine function (umwelt #158, ratified issue #158 /
    forum #255) — one contract, three projections: where_can_i_go =
    depth=1 from the caller's current place; the map (#160) = depth=inf
    (pass float('inf')) from root; the Discussion Hall room-list (#159) =
    depth=1 from an area.

    Returns a list holding a SINGLE root node (from_id), or [] if from_id
    doesn't exist at all (a true nonexistent id — NOT a privacy gate; see
    below). Each node: `{id, label, kind, owner, access_granted,
    interior_visible, thing_count, unread, children, links}`. `children`
    are direct sub-places (via parent_id), recursed up to `depth` levels —
    depth=0 means "from_id itself, no children walked further" (its OWN
    links are still populated at depth=0: links are a property of the
    visited node, not a further recursion level, so they don't consume any
    of `depth`'s budget — this is a judgment call on an ambiguity the issue
    itself flagged; see the PR handoff). depth=1 means "from_id plus its
    direct children" (where_can_i_go's "what can I reach" need) — each
    child at depth=1 has depth_remaining=0 for ITS OWN children, i.e.
    grandchildren are not included.

    `depth` beyond _PLACE_TREE_DEPTH_CAP (including float('inf')) is
    silently capped there — belt-and-suspenders against a pre-existing
    corrupt parent_id cycle; the REAL guard is set_place_parent's
    write-time would_create_cycle check, which makes the tree acyclic by
    construction for every relation created through this engine slice.

    `viewer` MUST already be the canonical (case-folded) agent id — same
    convention as every other db.py function that compares against
    places.owner (server.py canonicalizes before calling).

    Visibility (umwelt#538 rev 3 — see the module comment above this
    function for the full history): EVERY place from_id can reach — itself,
    every child regardless of depth, every link target — is emitted as a
    node, private or not. `_place_interior_visible_to` no longer prunes; it only
    gates a node's `interior_visible` flag and, through that, whether
    `thing_count`/`unread` carry real values or the safe placeholder `0`
    (see `_place_tree_node`). `from_id` itself follows the same rule: a
    private from_id the viewer doesn't own still returns its node (locked),
    never `[]` — `[]` now means only "from_id does not exist at all."

    `unread` is #157's exact cursor: `count_unseen(conn, node_id,
    get_standpoint(conn, viewer, node_id).last_position, viewer=viewer)` —
    one source of truth with what a subsequent `hear` call would report for
    that place (umwelt #158 catch #3). `viewer=viewer` (bug fix) excludes
    the viewer's own sent crossings from this count, matching `_place_view`'s
    `unseen_count` — a `say` you make is never "unread" TO you. Note
    `crossings_here`'s own `skipped`/`total_since` pagination count is
    DELIBERATELY unfiltered (viewer=None) since that route's returned
    crossing list itself still includes self-sent crossings (labeled
    `viewer_relation: "sender"`), so its completeness count must match.
    """
    root = get_place(conn, from_id)
    if root is None:
        return []  # true nonexistence only — see docstring, not a privacy gate
    effective_depth = min(depth, _PLACE_TREE_DEPTH_CAP)
    return [_place_tree_node(conn, root, effective_depth, viewer, {root["id"]})]


def _place_tree_node(conn, place: dict[str, Any], depth_remaining: int | float,
                      viewer: str, visited: set[str]) -> dict[str, Any]:
    """Build one place_tree node. Always emits the full public projection
    (umwelt#538 rule 2); `interior_visible` (== `_place_interior_visible_to(place,
    viewer)`) tells the caller whether `thing_count`/`unread` are real or
    the safe placeholder `0` — NEVER `None`: `_bound_children`'s sort key
    negates `unread` directly, so a real int is load-bearing there, and `0`
    reveals nothing more than "you can't see this count" already does via
    `interior_visible` itself."""
    interior_visible = _place_interior_visible_to(place, viewer)
    if interior_visible:
        standpoint = get_standpoint(conn, viewer, place["id"])
        thing_count = _place_thing_count(conn, place["id"])
        unread = count_unseen(
            conn, place["id"],
            standpoint["last_position"] if standpoint else None,
            viewer=viewer,
        )
    else:
        thing_count = 0
        unread = 0
    node: dict[str, Any] = {
        **_place_public_projection(place),
        "interior_visible": interior_visible,
        "thing_count": thing_count,
        "unread": unread,
        "children": [],
        "links": [],
    }
    # Links are a property of the visited node itself (an annotation, not a
    # further recursion level) — populated regardless of depth_remaining.
    # umwelt#538: a link's destination is always shown via its own public
    # projection now — only a genuinely dangling link (dst row gone) is
    # skipped, a data-integrity case, not a privacy one.
    for link in get_place_links(conn, place["id"]):
        dst = get_place(conn, link["dst_id"])
        if dst is None:
            continue
        node["links"].append({
            "dst_id": link["dst_id"],
            "dst_label": dst["label"],
            "link_label": link["label"],
            # umwelt#538 (reviewer-fairy catch on PR#542): kind/owner/
            # access_granted, same fields child/parent entries carry — dst
            # is already fetched above, so this is free, and it's what
            # format_place_marker needs to render a locked door without a
            # second per-link fetch (calling it on a link entry missing
            # these would KeyError before this fix).
            "dst_kind": dst["kind"],
            "dst_owner": dst["owner"],
            "dst_access_granted": [],
            # umwelt#543: the actual rendered marker string, computed once
            # here (dst already fetched above) rather than reconstructed by
            # any consumer surface — see format_place_marker's docstring.
            "dst_marker": format_place_marker(dst),
        })
    if depth_remaining > 0:
        # umwelt#538: children are always walked now, private or not — this
        # is what "kills the unreachable-child problem" (rev 3's own words):
        # a locked parent no longer hides its own children's existence.
        for child in get_place_children(conn, place["id"]):
            if child["id"] in visited:
                continue  # defensive cycle guard (belt-and-suspenders)
            node["children"].append(_place_tree_node(
                conn, child, depth_remaining - 1, viewer,
                visited | {child["id"]},
            ))
    return node


def list_top_level_places(conn, viewer: str) -> list[dict[str, Any]]:
    """Every top-level place (`parent_id IS NULL`) — umwelt #160's map
    forest-union roots. Ordered by `id` for determinism, matching
    `get_place_children`'s convention.

    umwelt#538 rev 3: returns EVERY top-level place, private or not —
    existence is public (rule 2). `viewer` is accepted for call-shape
    stability (every other caller here takes one, and future interior
    annotation at this layer may need it again) but is currently unused;
    `place_tree` is what applies `_place_interior_visible_to` for interior gating.
    """
    rows = conn.execute(
        "SELECT id, kind, owner, label, description, parent_id FROM places "
        "WHERE parent_id IS NULL ORDER BY id"
    ).fetchall()
    return [{"id": r[0], "kind": r[1], "owner": r[2], "label": r[3],
             "description": r[4], "parent_id": r[5]} for r in rows]


# --- things ---------------------------------------------------------------

def create_thing(conn, id: str, place_id: str, name: str, description: str,
                 kind: str, exec_command: str | None, metadata: dict,
                 author: str, created_at: str, weight_est: int = 0,
                 lane: str = "land",
                 update_policy_cohort: str | None = None,
                 *, commit: bool = True) -> None:
    # Defense in depth: server.py's create_thing route already rejects
    # body-kind specs (422) before reaching here — this is the same guard at
    # the DB layer, so a future caller of db.create_thing directly (bypassing
    # the Flask route) still can't forge an avatar through the normal path.
    if is_body_kind(kind):
        raise ValueError(
            "avatars are engine-created at birth, not agent-forged — "
            "use get_or_create_avatar"
        )
    # umwelt #278: lane defense-in-depth, same posture as is_body_kind above
    # — server.py's _create_thing_from_spec already re-validates lane
    # against _VALID_LANE before ever reaching here; this is the same guard
    # at the DB layer for a future direct caller.
    if lane not in ("sky", "land", "road"):
        raise ValueError(
            f"lane must be one of ('sky', 'land', 'road'), got {lane!r}"
        )
    # weight_est (#88 slice 1) defaults to 0 for backward compatibility with
    # existing direct callers (tests, mostly) that predate the carry system
    # — server.py's create_thing route always computes and passes a real
    # value via db.compute_weight_est. holder_kind/holder_id are left at
    # their schema defaults ('place'/NULL) — a newly-forged thing is never
    # born already carried.
    #
    # update_policy_cohort (umwelt#300 S0(d)) defaults to None — an
    # existing/other direct caller of this function (tests, engine-boot
    # helpers) that doesn't pass it stays on today's legacy author-only
    # update_thing gate. server.py's _create_thing_from_spec (the shared
    # helper behind both agent-facing forge paths: POST /umwelt/create_thing
    # and use()-driven blueprint stamping) is the ONLY call site that passes
    # 'declarative_v1' — see that function's own comment for why engine-boot
    # singleton paths (_ensure_engine_kinddef, the project_registrar
    # instance, get_or_create_avatar) deliberately do NOT.
    conn.execute(
        "INSERT INTO things(id, place_id, name, description, kind, "
        "exec_command, metadata, author, created_at, weight_est, lane, "
        "update_policy_cohort) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id, place_id, name, description, kind, exec_command,
         json.dumps(metadata), author, created_at, weight_est, lane,
         update_policy_cohort),
    )
    # commit=False (keyword-only; default True is today's behaviour) leaves
    # the transaction open for a caller that owns it -- the world package
    # loader (umwelt#1043), which must stay all-or-nothing.
    if commit:
        conn.commit()


def _thing_row_to_dict(row) -> dict[str, Any]:
    return {
        "id": row[0], "place_id": row[1], "name": row[2],
        "description": row[3], "kind": row[4], "exec_command": row[5],
        "metadata": json.loads(row[6]) if row[6] else {},
        "author": row[7], "created_at": row[8],
        "status": row[9], "binned_at": row[10], "landfilled_at": row[11],
        "weight_est": row[12], "holder_kind": row[13], "holder_id": row[14],
        "lane": row[15],  # umwelt #278
        "updated_at": row[16],  # world-content-timestamps design, slice 1
        "update_policy_cohort": row[17],  # umwelt#300 S0(d)
        "updated_by": row[18],  # umwelt#300 S0(d)
    }


# Single unified column list (incl. #28's lifecycle columns, #88 slice 1's
# weight_est/holder columns, #278's lane column, and the world-content-
# timestamps design's updated_at) — get_thing, things_in_place,
# recent_things, and the avatar queries all share it, so every thing dict
# always carries status/binned_at/landfilled_at/weight_est/holder_kind/
# holder_id/lane/updated_at/update_policy_cohort/updated_by. (Ariadne's
# spec left "one list vs a light + full split" open; one list is simplest
# and the extra columns are cheap on every existing call site.)
# weight_est/holder_kind/holder_id/lane/updated_at/update_policy_cohort/
# updated_by are appended at the END — any code indexing this tuple
# positionally (see recent_things's place_label) must index past all 19
# columns (umwelt#300 S0(d) added the last two), not the pre-S0(d) 17.
_THING_COLS = ("id, place_id, name, description, kind, exec_command, "
               "metadata, author, created_at, status, binned_at, landfilled_at, "
               "weight_est, holder_kind, holder_id, lane, updated_at, "
               "update_policy_cohort, updated_by")


def get_thing(conn, thing_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE id = ?", (thing_id,)
    ).fetchone()
    return _thing_row_to_dict(row) if row else None


# Columns an author may change via update_thing. id/author/created_at/place_id/
# kind are immutable (a kind change is a re-forge, not an update).
#
# weight_est (#88 slice 1) is included here but NOT agent-settable directly —
# it never appears in server.py's client-facing _MUTABLE_THING_KEYS
# whitelist. server.py's update_thing route computes it via
# db.compute_weight_est and injects it into the internal patch dict ONLY
# when name/description are in the caller's patch — this list just lets
# that internally-computed value ride the same UPDATE statement as the
# caller's own mutable fields.
_MUTABLE_THING_COLS = ("name", "description", "exec_command", "metadata", "weight_est")


def update_thing_apply(conn, thing_id: str, patch: dict, actor: str,
                       source_changed: bool = False) -> bool:
    """The core update_thing write logic, WITHOUT committing — factored out
    (umwelt#300 S0(d)) so the write can be run inside the SAME transaction
    as an authorization check (see `atomic_authorized_write` below and
    server.py's update_thing route), instead of update_thing's own
    unconditional `conn.commit()` closing the transaction before the
    caller's atomic check-then-write wrapper gets a chance to. `update_thing`
    (below) is now a thin wrapper: call this, then commit — identical
    behavior for every existing direct caller.

    Set the given mutable columns on a thing. Only keys in _MUTABLE_THING_COLS
    are applied — the column list is a fixed constant (never caller-supplied), so
    the SQL is injection-safe; values are parameterized. Caller validates the
    patch shape + authorization. No-op (returns False) if nothing mutable is set.

    Rejects body-kind things (avatars): an avatar is engine-managed presence,
    not agent-editable — same posture as create_thing's rejection. Without
    this, update_thing could set exec_command on an avatar (a category-error
    violation of invariant #4 — presence, not a usable) or let a forged
    `agent` rename/edit another agent's body (a forgeable cross-owner write,
    same trust-scoped-v0 class as move_to). Caller (server.py) still does the
    authorization check; this is defense in depth at the DB layer, matching
    create_thing's pattern.

    Stamps updated_at (world-content-timestamps design, slice 1) — but only
    when the patch resolves to a GENUINE value change on at least one
    mutable column (R3: a no-op patch does not bump the timestamp). Key
    presence in `patch` is not enough — the candidate columns' CURRENT
    values are read back and compared against the patch's values; a column
    whose new value equals its current value is dropped before the UPDATE
    runs. `metadata` is compared as a decoded dict (`json.loads(current) !=
    patch["metadata"]`), never as raw JSON strings, since re-serialization
    can reorder keys and produce a false "changed" on byte-identical
    content. If every candidate column drops out this way, nothing is
    touched and no stamp happens — same early-out as the empty-patch case.
    updated_at itself is never in _MUTABLE_THING_COLS, so a caller cannot
    smuggle a value for it through `patch` (R2).

    Also stamps updated_by = actor (umwelt#300 S0(d), Mira's security
    acceptance criteria point 7 — server-stamped attribution) at the SAME
    point and under the SAME "genuine change" gate as updated_at — never in
    _MUTABLE_THING_COLS, so a caller cannot smuggle a forged attribution
    through `patch` either.

    `actor` (world-content-timestamps.md slice 2) is the caller-supplied
    acting agent — authorization-checked by the route, same trust-scoped
    posture as `graft_advances`'s `agent` column, never cryptographically
    authenticated. REQUIRED, never defaulted, so no call site can silently
    skip stamping tier 1. On a genuine change, writes one `content_updates`
    row in the SAME transaction as the column UPDATE (same posture as
    graft_advances's metadata-UPDATE + provenance-INSERT pair) — never
    calls mint_event; an 'update' events row is minted later, separately,
    only on request via /umwelt/record.

    `source_changed` (umwelt#1014): the caller (the update_thing route) has
    already compared the patched source tree to the one on disk and found a
    DIFFERENCE, and has just rewritten it. Source lives on disk, not in a
    column, so this function cannot detect that itself — it records it:
    the literal name "source" is appended to `changed_cols`, the stamp
    fires even when no DB column changed, and it is still ONE
    `content_updates` row in the SAME transaction as any column UPDATE.
    The caller passes True only after a SUCCESSFUL write (no stamp for a
    write that did not happen) and only on a real difference (R3).

    Returns True if a genuine change was written, False if the patch
    resolved to a no-op (nothing mutable in `patch`, or every candidate
    column's value matched what was already stored, and no source change).
    """
    row = conn.execute("SELECT kind FROM things WHERE id = ?", (thing_id,)).fetchone()
    if row is not None and is_body_kind(row[0]):
        raise ValueError(
            "avatars are engine-managed presence, not agent-editable — "
            "use move_avatar to relocate a body"
        )
    candidate_cols = [c for c in _MUTABLE_THING_COLS if c in patch]
    if not candidate_cols and not source_changed:
        return False
    cols = (_changed_cols(conn, "things", thing_id, candidate_cols, patch)
            if candidate_cols else [])
    if not cols and not source_changed:
        return False
    vals = [json.dumps(patch[c]) if c == "metadata" else patch[c] for c in cols]
    set_clause = ", ".join([f"{c} = ?" for c in cols]
                           + ["updated_at = ?", "updated_by = ?"])
    if source_changed:
        # `cols` (the UPDATE's SET list) stays DB columns only; the log row
        # additionally names "source".
        log_cols = cols + ["source"]
    else:
        log_cols = cols
    stamp = now_iso()
    conn.execute(
        f"UPDATE things SET {set_clause} WHERE id = ?",
        vals + [stamp, actor, thing_id],
    )
    conn.execute(
        "INSERT INTO content_updates(id, subject_id, subject_kind, actor, "
        "changed_cols, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
        ("cupd_" + str(uuid.uuid4()), thing_id, "thing", actor,
         json.dumps(log_cols), stamp),
    )
    return True


def update_thing(conn, thing_id: str, patch: dict, actor: str) -> None:
    """Set the given mutable columns on a thing and commit. See
    `update_thing_apply` for the full write contract (this is now a thin
    commit-wrapper around it, umwelt#300 S0(d)) — identical behavior to
    before the refactor for every existing caller."""
    update_thing_apply(conn, thing_id, patch, actor)
    conn.commit()


def set_thing_metadata(conn, thing_id: str, metadata: dict) -> None:
    """Overwrite a thing's metadata column directly — ENGINE-ONLY write path
    (umwelt #303's retire_identity) that deliberately bypasses
    update_thing's body-kind reject.

    update_thing refuses to touch ANY avatar-kind row, agent-editable or
    not — that guard exists to stop an AGENT from editing engine-managed
    presence (invariant #4). retire_identity's `metadata.retired` flag is
    the opposite case: an ENGINE action (ops-authority, config-gated,
    never a caller-assertable agent verb) that specifically needs to write
    the avatar's own thing row. Reusing update_thing here would just be
    fighting its own guard; this is a distinct, narrower setter instead —
    never reachable via an agent-facing route. Caller computes the full
    merged metadata dict (same merge-not-replace + tri-state null-clear
    pattern server.py's update_thing route applies for ordinary things)
    before calling; this function does a plain overwrite of whatever dict
    it's given.

    Stamps updated_at (world-content-timestamps design, slice 1 follow-up)
    unconditionally — this is a distinct write path from update_thing, with
    no value-diffing (R3 only governs update_thing/update_place's own
    columns-in-patch logic; every call here is an engine-driven content
    change, not a caller-submitted patch that might resolve to a no-op).

    Generic single-table setter — still used directly wherever only the
    thing-metadata copy needs writing. `/umwelt/retire_identity` itself
    goes through `mark_avatar_and_identity_retired` instead (umwelt#561),
    which needs this table's write paired atomically with
    `identities.retired_at` in one commit; see that function for why a
    bare call here is no longer sufficient for retirement specifically.
    """
    conn.execute(
        "UPDATE things SET metadata = ?, updated_at = ? WHERE id = ?",
        (json.dumps(metadata), now_iso(), thing_id),
    )
    conn.commit()


def mark_avatar_and_identity_retired(conn, avatar_id: str, agent_id: str,
                                      metadata: dict) -> None:
    """Retirement's atomic pair-write (umwelt#561 security review: Clio's
    split + Mira's atomicity ask) -- sets the avatar's `things.metadata.
    retired` flag (the existing world/presentation record every renderer
    already reads, via db.avatars_in_place et al.) AND `identities.
    retired_at` (the NEW auth-side authority db.get_agent_session reads)
    in ONE transaction, sharing a single commit, instead of two
    independently-committed writes that could drift on a partial failure
    (a crash/exception between them would otherwise leave a "retired"
    avatar whose identity still authenticates, or vice versa).

    `agent_id` MUST already be canonical -- this function does not fold it
    (same division of labor as bind_identity: the caller has already
    canonicalized once for its own authorization/lookup and this function
    trusts that rather than re-deriving it).

    `metadata` is the CALLER's fully-merged dict (same convention as
    set_thing_metadata -- merge-not-replace is the caller's job, this
    function does a plain overwrite of whatever it's given).

    If no `identities` row exists for `agent_id` (an avatar can be retired
    without ever having gone through login/enrollment), the identities
    UPDATE simply affects 0 rows -- not an error. There is nothing to
    invalidate for an identity that was never eligible to authenticate in
    the first place (no row => the three-state rule already refuses it
    outright, per db.get_agent_session/the login route), and
    _seed_identities_from_avatars never seeds a row for an already-retired
    avatar afterward (its own docstring: "Source of truth: things WHERE
    kind='avatar', excluding retired ones") -- so this can't be silently
    reached later by a fresh row landing on a stale retired_at.

    retired_at is stamped unconditionally to now_iso() -- this function is
    retirement-only, never called to un-retire (there is no un-retire path
    anywhere in this codebase; umwelt#303's retirement is deliberately
    one-way, recoverable only via db.trash_thing's restore path for the
    BINNED things, never a reversal of the avatar flag itself)."""
    retired_at = now_iso()
    conn.execute(
        "UPDATE things SET metadata = ?, updated_at = ? WHERE id = ?",
        (json.dumps(metadata), retired_at, avatar_id),
    )
    conn.execute(
        "UPDATE identities SET retired_at = ? WHERE agent_id = ?",
        (retired_at, agent_id),
    )
    conn.commit()


_BODY_KIND_PLACEHOLDERS = ",".join("?" for _ in _BODY_KINDS)


def things_in_place(conn, place_id: str) -> list[dict[str, Any]]:
    """Things at place_id you can look_at/use — excludes avatars (bodies) and
    non-live things (#28: binned/landfilled things are gone from normal
    views — see db.get_thing, which deliberately keeps returning them).

    A body is a special kind of thing, but it should never silently show up
    among the "things you can look_at/use" here — see avatars_in_place for
    the separate "who's here" list (invariant #4).

    Also excludes carried AND bagged things (holder_kind != 'place' — umwelt
    #88 slice 1 for 'avatar', slice 2 for 'container') — a thing on someone's
    person, or zipped into a bag, is not in the room; it shows up in THEIR
    inventory instead (db.things_carried_by), or the bag's contents (only via
    db.things_in_container / look_in — the closed-bag exposure rule: a
    contained thing renders ONLY there, never in a place view). place_id
    keeps its value while carried/bagged, so without this filter a
    carried/bagged thing would still appear to be "here".
    """
    rows = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE place_id = ? AND status='live' "
        f"AND holder_kind = 'place' "
        f"AND kind NOT IN ({_BODY_KIND_PLACEHOLDERS}) ORDER BY created_at ASC",
        (place_id, *_BODY_KINDS),
    ).fetchall()
    return [_thing_row_to_dict(r) for r in rows]


def count_live_things_by_place(conn, place_ids) -> dict[str, int]:
    """{place_id: n} for the given places in ONE query, where n counts exactly
    what `things_in_place` returns for that place (live, not carried or
    bagged, not an avatar). A place with none is absent from the result
    (callers `.get(pid, 0)`). umwelt #1074: the Project Register's rooms
    section, which must not run one query per room."""
    ids = list(place_ids)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT place_id, COUNT(*) FROM things WHERE place_id IN ({marks}) "
        f"AND status='live' AND holder_kind = 'place' "
        f"AND kind NOT IN ({_BODY_KIND_PLACEHOLDERS}) GROUP BY place_id",
        (*ids, *_BODY_KINDS),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def recent_things(conn, limit: int = 8) -> list[dict[str, Any]]:
    """The most-recently-built LIVE things across the whole world, newest first
    (#28: a trashed/landfilled thing should not linger in the "recently
    built" feed).

    Excludes avatars (bodies) — an avatar's birth is not a "build" event for
    this feed, same rationale as things_in_place/list_places. Pre-existing
    gap surfaced during #28's review and fixed here as a direct follow-up
    (was flagged out of scope for the removal-system PR itself).

    Powers the human webview's live "recently built" feed. Data-driven — it
    surfaces whatever inhabitants have forged lately, never hardcodes any
    thing. Each row carries its place's label so the feed reads
    "<thing> in <Place>". A read of world content, not a mutation.
    """
    rows = conn.execute(
        f"SELECT {_THING_COLS}, "
        "(SELECT label FROM places WHERE id = things.place_id) AS place_label "
        # created_at (microsecond ISO) is the real ordering key; the id DESC
        # tie-break is only for DETERMINISM on an exact-timestamp collision —
        # thing ids are uuid4, not monotonic, so it is not chronological.
        f"FROM things WHERE status='live' AND kind NOT IN ({_BODY_KIND_PLACEHOLDERS}) "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (*_BODY_KINDS, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = _thing_row_to_dict(r)
        d["place_label"] = r[19]  # 19 = past all _THING_COLS (0-18), incl. update_policy_cohort/updated_by
        out.append(d)
    return out


def avatars_in_place(conn, place_id: str) -> list[dict[str, Any]]:
    """The 'who's here' list — avatar-things (bodies) currently at place_id.

    Deliberately separate from things_in_place: avatars are never part of
    the "things you can look_at/use" contract there.

    umwelt #303: a retired identity's avatar (`metadata.retired == true`,
    set by /umwelt/retire_identity) is excluded from the result — filtered
    in Python AFTER the fetch, not via an added SQL clause, matching this
    codebase's existing convention for metadata-flag checks (dict
    inspection, not `json_extract`; see `_prune_map_node`). This is the
    SINGLE chokepoint every "who's here" consumer (MUD, webrender's
    `/scene` via `_build_scene`, `place_view`) reads through, so this one
    filter makes a retired avatar disappear from every renderer at once —
    no separate MUD/webrender change needed.
    """
    rows = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE place_id = ? "
        f"AND kind IN ({_BODY_KIND_PLACEHOLDERS}) ORDER BY created_at ASC",
        (place_id, *_BODY_KINDS),
    ).fetchall()
    avatars = [_thing_row_to_dict(r) for r in rows]
    return [a for a in avatars if not a["metadata"].get("retired")]


def live_non_avatar_things_by_author(conn, author: str) -> list[dict[str, Any]]:
    """An identity's own live, non-avatar things (umwelt #303's
    retire_identity bulk-bin query). `kind != 'avatar'` EXCLUDES the
    identity's own avatar body from this list — get_or_create_avatar sets
    `author=agent_id` on the avatar's own thing row too, so without this
    exclusion the caller would try to trash the very body it's retiring
    through the wrong path (trash_thing rejects body-kinds outright; the
    avatar is retired via the separate metadata.retired flag instead — see
    the /umwelt/retire_identity route). status='live' only — an
    already-binned/landfilled thing has nothing left to bin.
    """
    rows = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE author = ? AND status = 'live' "
        "AND kind != 'avatar'",
        (author,),
    ).fetchall()
    return [_thing_row_to_dict(r) for r in rows]


# --- lifecycle: trash / bin / landfill / dissolve (#28) --------------------
#
# live --(agent: trash)--> binned --(GC, T1)--> landfilled --(GC, T2)--> gone
#      <-(agent: restore)---|         <-(agent: restore)---|
#
# Permanent destruction (dissolve) is never an agent action — see gc.py,
# the only caller of dissolve_thing. These DB functions are dumb setters;
# server.py's routes own state-transition validation (404/403/409). The
# guards duplicated at this layer (is_body_kind and, since #88 slice 1,
# holder_kind=='avatar'; since slice 2, holder_kind=='container' AND
# non-empty containers; all in trash_thing) are category-errors, not
# state-transitions — same defense-in-depth posture as create_thing/
# update_thing's avatar rejection above.

def trash_thing(conn, thing_id: str) -> None:
    """Set a thing's status to binned (agent trash — recoverable soft-delete).

    Caller (server.py's /umwelt/trash_thing route) validates thing exists
    (404), author-only (403), status=='live' (409 otherwise), (#88 slice 1)
    not-currently-carried (422 — "put it down first"), and (#88 slice 2)
    not-currently-bagged (422 — "take it out first") / not a non-empty
    container (422 — "empty it first"). This function re-asserts every one
    of those as defense-in-depth — a body must never be binned, a
    carried/bagged thing must be freed first, and a container must be
    emptied first — regardless of which caller reaches this function. Keeps
    the #28 invariant that place_id never changes through lifecycle: a
    carried/bagged thing's place_id is stale ("last at rest"), so binning it
    in place would silently misplace it in the bin/landfill views once
    dropped/taken-out again.
    """
    row = conn.execute(
        "SELECT kind, holder_kind FROM things WHERE id = ?", (thing_id,)
    ).fetchone()
    if row is not None and is_body_kind(row[0]):
        raise ValueError("avatars are un-trashable — a body cannot be binned")
    if row is not None and row[1] == "avatar":
        raise ValueError("thing is being carried — put it down first")
    if row is not None and row[1] == "container":
        raise ValueError("thing is being carried — take it out first")
    if row is not None and row[0] == "container":
        occupied = conn.execute(
            "SELECT 1 FROM things WHERE holder_kind='container' AND holder_id=? "
            "AND status='live' LIMIT 1",
            (thing_id,),
        ).fetchone()
        if occupied is not None:
            raise ValueError("container is not empty — empty it first")
    conn.execute(
        "UPDATE things SET status='binned', binned_at=? WHERE id=?",
        (now_iso(), thing_id),
    )
    conn.commit()


def restore_thing(conn, thing_id: str) -> None:
    """Set a thing's status to live, clearing binned_at/landfilled_at.

    Works from EITHER binned or landfilled — place_id was never touched
    through the lifecycle, so this alone is enough to put a thing back
    exactly where it lived, landfill or not ("digging it out"). Caller
    validates thing exists (404), author-only (403), and status != 'live'
    (409 otherwise).
    """
    conn.execute(
        "UPDATE things SET status='live', binned_at=NULL, landfilled_at=NULL "
        "WHERE id=?",
        (thing_id,),
    )
    conn.commit()


def landfill_thing(conn, thing_id: str) -> None:
    """Set a thing's status to landfilled (GC-only — advancing binned things
    past T1). Never agent-triggered; called only from gc.run_gc."""
    conn.execute(
        "UPDATE things SET status='landfilled', landfilled_at=? WHERE id=?",
        (now_iso(), thing_id),
    )
    conn.commit()


def dissolve_thing(conn, thing_id: str) -> None:
    """Permanently delete a thing's ROW ONLY (#28 decision 6: row delete
    here; on-disk source-dir removal + CAS mark-and-sweep are orchestrated
    by gc.dissolve_thing, which calls this for the DB half).

    ENGINE-ONLY: never reachable via an HTTP route or MCP tool — the whole
    point of the lifecycle is that permanent destruction is never an
    agent's act (Lei/Ariadne). Only gc.py calls this.
    """
    conn.execute("DELETE FROM things WHERE id = ?", (thing_id,))
    conn.commit()


def list_binned(conn, author: str) -> list[dict[str, Any]]:
    """An author's own trashed things (the /umwelt/bin read, author-scoped)."""
    rows = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE author=? AND status='binned' "
        "ORDER BY binned_at ASC",
        (author,),
    ).fetchall()
    return [_thing_row_to_dict(r) for r in rows]


def list_landfilled(conn) -> list[dict[str, Any]]:
    """Everyone's landfilled things — the public dump (GET /umwelt/landfill,
    and the one status-driven place-view special case for place_landfill)."""
    rows = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE status='landfilled' "
        "ORDER BY landfilled_at ASC"
    ).fetchall()
    return [_thing_row_to_dict(r) for r in rows]


def things_due_landfill(conn, cutoff_iso: str) -> list[dict[str, Any]]:
    """Binned things whose binned_at is older than cutoff_iso — GC's T1 query.

    Defensively excludes body-kinds too (avatars can never actually reach
    'binned' — trash_thing rejects them — but the GC query stays honest
    regardless of how a row got there, same posture as things_due_dissolve).
    """
    rows = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE status='binned' "
        f"AND binned_at < ? AND kind NOT IN ({_BODY_KIND_PLACEHOLDERS}) "
        "ORDER BY binned_at ASC",
        (cutoff_iso, *_BODY_KINDS),
    ).fetchall()
    return [_thing_row_to_dict(r) for r in rows]


def things_due_dissolve(conn, cutoff_iso: str) -> list[dict[str, Any]]:
    """Landfilled things whose landfilled_at is older than cutoff_iso — GC's
    T2 query, feeding gc.dissolve_thing.

    Defensively excludes body-kinds (Ariadne's #28 cross-spec constraint
    with the avatar build): avatars never reach 'landfilled' in practice,
    but this query — the one that feeds permanent destruction — must never
    select one even if some future bug ever let one get here.
    """
    rows = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE status='landfilled' "
        f"AND landfilled_at < ? AND kind NOT IN ({_BODY_KIND_PLACEHOLDERS}) "
        "ORDER BY landfilled_at ASC",
        (cutoff_iso, *_BODY_KINDS),
    ).fetchall()
    return [_thing_row_to_dict(r) for r in rows]


# --- avatars (bodies) ------------------------------------------------------

def _get_avatar(conn, agent_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE kind='avatar' AND author=?",
        (agent_id,),
    ).fetchone()
    return _thing_row_to_dict(row) if row else None


def avatar_exists(conn, agent_id: str) -> bool:
    """True if this agent already has a body (is registered). Public
    wrapper so the register route can distinguish a first-ever birth from
    a reconnect."""
    return _get_avatar(conn, agent_id) is not None


def get_avatar(conn, agent_id: str) -> dict[str, Any] | None:
    """Public wrapper for _get_avatar — umwelt #88 slice 1's take/drop/
    inventory routes need to fetch (never birth) the caller's avatar, unlike
    get_or_create_avatar which births one on first call. Returns None if the
    agent hasn't entered the world yet (no /umwelt/enter call)."""
    return _get_avatar(conn, agent_id)


def list_avatar_agents(conn) -> list[str]:
    """All agent ids that have ever registered a body (kind='avatar').
    The 'registry' that umwelt #119's @-mention addressing validates
    against — an unknown name in `mentions` is silently dropped rather
    than erroring (see server.py's send_crossing). `to_agent` used to
    share that same permissive posture; since umwelt #835 it does not —
    an unregistered `to_agent` with `wake_me=True` is refused 422 instead,
    checked against this same registry (see send_crossing's own docstring
    for the full contract).
    """
    rows = conn.execute("SELECT author FROM things WHERE kind='avatar'").fetchall()
    return [r[0] for r in rows]


def get_or_create_avatar(conn, agent_id: str, home_place_id: str,
                         description: str | None = None,
                         metadata: dict | None = None,
                         display_name: str | None = None) -> dict[str, Any]:
    """Return the agent's avatar-thing, creating it (birth) on first-ever call.

    Reconnect semantics: an existing avatar is returned as-is, wherever its
    place_id currently points — this function never moves an avatar, it only
    finds-or-births one. Moving is move_to's job (see move_avatar).

    Trust scope (v0): the driver→avatar binding rides the caller-declared
    `agent_id` string — there is no engine-attested identity yet (#24).
    A forged agent_id can reconnect to another agent's avatar, or claim
    to be someone it isn't. Sound only among our_side callers today; hardens
    when the identity layer lands. Same posture as world_view's private-read
    scoping.

    "Exactly one avatar per agent" is DB-enforced via the partial unique
    index (idx_things_avatar_author) — a per-agent-STRING guarantee, not a
    per-real-agent guarantee. Until the identity layer attests `agent_id`, a
    caller could in principle claim a different string and mint a second
    body under it (Sybil-on-avatars) — same root cause as the binding-
    forgeability note above, not a separate flaw. Closes with #24.

    umwelt #73: `agent_id` MUST be the canonical (case-folded) identity — it
    is both the lookup key (`_get_avatar`) and the stored `author` column.
    `display_name` is the raw typed form, used ONLY to build the avatar's
    name/default-description text on first birth (falls back to `agent_id`
    when not supplied, so pre-#73 callers that pass one identity string for
    both purposes keep working unchanged). On reconnect, `display_name` is
    never consulted — the existing avatar's name/description are returned
    as-is, never overwritten by a later differently-cased call.

    umwelt #278: a newly-birthed avatar is stamped `lane='road'` explicitly
    at this INSERT — engine-birthed presence, not agent-forged, so this is
    the ONLY path that ever writes `lane='road'` for an AVATAR (body-kind)
    thing specifically. This is NOT a claim that 'road' full stop only comes
    from here: `_create_thing_from_spec`'s is_body_kind reject only blocks
    kind='avatar' specs — an agent can still forge an ordinary (non-avatar)
    thing with `spec.lane='road'` by its own choice, and that path is real
    and exercised (see
    test_scene_sky_thing_in_top_band_road_items_share_bottom_band's
    "cobblestone" fixture). This function bypasses `db.create_thing`'s own
    INSERT (which would reject an avatar-kind row outright) — same "birth
    path, not the forge path" reasoning as the rest of this function.
    """
    existing = _get_avatar(conn, agent_id)
    if existing is not None:
        return existing

    display_name = agent_id if display_name is None else display_name
    thing_id = str(uuid.uuid4())
    created_at = now_iso()
    try:
        conn.execute(
            "INSERT INTO things(id, place_id, name, description, kind, "
            "exec_command, metadata, author, created_at, lane) "
            "VALUES(?, ?, ?, ?, 'avatar', NULL, ?, ?, ?, 'road')",
            (thing_id, home_place_id, f"{display_name}'s avatar",
             description or f"{display_name}'s presence in the world.",
             json.dumps(metadata or {}), agent_id, created_at),
        )
        # umwelt#1103: a birth seeds its identities row in the SAME
        # transaction as the avatar insert, through the boot seed's helper
        # (enrollment predicate inside). Reconnect returned above: not a birth.
        _seed_identity_for_avatar(conn, agent_id, metadata or {}, created_at)
        conn.commit()
    except sqlite3.IntegrityError:
        # Lost a birth race to a concurrent first-entry for the same agent —
        # the partial unique index rejected our insert because someone else's
        # won first. Not a single-writer assumption: re-query and return
        # theirs rather than erroring.
        conn.rollback()
        existing = _get_avatar(conn, agent_id)
        if existing is None:
            raise
        return existing

    avatar = _get_avatar(conn, agent_id)
    assert avatar is not None  # just inserted; must be findable
    return avatar


def move_avatar(conn, agent_id: str, place_id: str) -> None:
    """Move the agent's avatar (body) to place_id.

    Distinct from touch_standpoint: standpoint = the read-cursor for
    crossings ("where was I last looking"); avatar.place_id = where the body
    physically is now. move_to writes both, independently — they answer
    different questions and must not be merged into one write.

    A no-op (0 rows affected) if the agent has no avatar yet — move_to does
    not birth one; only get_or_create_avatar (via /umwelt/enter) does.
    """
    conn.execute(
        "UPDATE things SET place_id=? WHERE kind='avatar' AND author=?",
        (place_id, agent_id),
    )
    conn.commit()


# --- carrying (#88 slice 1) -------------------------------------------------
#
# The holder axis: a thing is held by exactly one of a place (holder_kind=
# 'place', today's default — "at rest somewhere") or an avatar (holder_kind=
# 'avatar', "carried"). These are dumb setters — server.py's take/drop routes
# own precondition validation (same-place, budget, holder-only, etc.); the
# functions here just flip the columns, matching the trash_thing/
# restore_thing posture above.

def things_carried_by(conn, avatar_id: str) -> list[dict[str, Any]]:
    """The things currently carried by avatar_id (an avatar thing-id) — the
    /umwelt/inventory read, and the source of a take's budget-check sum.
    Filters to status='live': a carried thing should always be live (trash
    is blocked while carried — see server.py's trash_thing route), but this
    query stays honest regardless, same posture as things_in_place.
    """
    rows = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE holder_kind='avatar' "
        "AND holder_id=? AND status='live' ORDER BY created_at ASC",
        (avatar_id,),
    ).fetchall()
    return [_thing_row_to_dict(r) for r in rows]


def take_thing(conn, thing_id: str, avatar_id: str) -> bool:
    """Set a thing to carried by avatar_id. place_id is left UNTOUCHED — it
    keeps meaning "where the thing was last at rest" while carried (see the
    things.holder_kind schema comment); only drop_thing later overwrites it.

    Returns True if the thing was taken, False if the guarded UPDATE matched
    no row — i.e. someone else took it between the route's read and this
    write (PR #89 colleague review: without the guard, a raced take returns
    200 to both callers and the loser's later drop 403s — a silently-wrong
    success). Defense-in-depth re-check, same pattern as trash_thing's.
    """
    cur = conn.execute(
        "UPDATE things SET holder_kind='avatar', holder_id=? "
        "WHERE id=? AND holder_kind='place'",
        (avatar_id, thing_id),
    )
    conn.commit()
    return cur.rowcount == 1


def drop_thing(conn, thing_id: str, place_id: str) -> None:
    """Return a thing to a place — sets holder_kind='place', clears
    holder_id, and writes place_id to wherever the DROPPING avatar currently
    stands (the caller passes the avatar's CURRENT place_id — drop happens
    where you stand, not where the thing was last at rest before it was
    picked up).

    Dropping a container: its CONTENTS need no update here — they're tracked
    by holder_id pointing at the container's own thing-id, never by their
    own place_id, so they silently "ride along" wherever the bag now sits
    (umwelt #88 slice 2). Nothing in this function is container-specific;
    that's the point — the compression mechanic falls out of the holder-axis
    design for free.
    """
    conn.execute(
        "UPDATE things SET holder_kind='place', holder_id=NULL, place_id=? "
        "WHERE id=?",
        (place_id, thing_id),
    )
    conn.commit()


# --- containers / bags (#88 slice 2) ----------------------------------------
#
# A container is a thing whose kind='container'. Bags don't get a separate
# holder axis of their own — they're carried/dropped exactly like any other
# thing (take_thing/drop_thing above, unchanged). What's new here is the
# OTHER side: things HELD BY a container (holder_kind='container', holder_id
# = the container's own thing-id). Same dumb-setter posture as the carrying
# section above — server.py's put/take_out/look_in routes own precondition
# validation (reach, capacity, bag-in-bag rejection, etc.).

def things_in_container(conn, container_id: str) -> list[dict[str, Any]]:
    """The things currently bagged inside container_id (a container thing-id)
    — the /umwelt/look_in read, and the source of put's capacity-check sum
    and trash_thing's non-empty-container check. Same status='live' filter
    and rationale as things_carried_by.
    """
    rows = conn.execute(
        f"SELECT {_THING_COLS} FROM things WHERE holder_kind='container' "
        "AND holder_id=? AND status='live' ORDER BY created_at ASC",
        (container_id,),
    ).fetchall()
    return [_thing_row_to_dict(r) for r in rows]


def put_thing_in_container(conn, thing, container_id: str) -> bool:
    """Set a thing to be held by container_id (a bag). place_id is left
    UNTOUCHED, same rationale as take_thing — it stays "where the thing was
    last at rest", meaningless while bagged since a bagged thing is found via
    holder_id, never place_id.

    Guarded on the thing's holder state AS THE CALLER OBSERVED IT (PR #91
    colleague review, third instance of the race class): put can source from
    the caller's own hands OR a shared place, and the place-sourced case is
    the EASIEST race in the family — any same-place avatar reaches a loose
    thing non-exclusively, so two avatars bagging the same item is ordinary,
    not an edge case. Takes the observed thing ROW (dict), returns False to
    the race loser.

    Three sources (umwelt #966 added the container one — a kind-sanctioned
    `move_to` out of a tray is container->container). Any other holder_kind
    raises ValueError rather than falling into a branch whose guard can
    never match and masquerading as a lost race."""
    kind = thing["holder_kind"]
    if kind == "avatar":
        cur = conn.execute(
            "UPDATE things SET holder_kind='container', holder_id=? "
            "WHERE id=? AND holder_kind='avatar' AND holder_id=?",
            (container_id, thing["id"], thing["holder_id"]),
        )
    elif kind == "container":
        cur = conn.execute(
            "UPDATE things SET holder_kind='container', holder_id=? "
            "WHERE id=? AND holder_kind='container' AND holder_id=?",
            (container_id, thing["id"], thing["holder_id"]),
        )
    elif kind == "place":
        cur = conn.execute(
            "UPDATE things SET holder_kind='container', holder_id=? "
            "WHERE id=? AND holder_kind='place' AND place_id=?",
            (container_id, thing["id"], thing["place_id"]),
        )
    else:
        raise ValueError(f"unexpected holder_kind {kind!r}")
    conn.commit()
    return cur.rowcount == 1


def take_thing_from_container(conn, thing_id: str, avatar_id: str,
                              container_id: str) -> bool:
    """Move a thing from a container to avatar_id's hands. Guarded the same
    way as take_thing (PR #89 race guard): the UPDATE only matches while the
    thing is still in THAT container, so a raced double-take-out returns
    False to the loser instead of a silently-wrong success. take_thing itself
    guards WHERE holder_kind='place', which is why container->avatar needs
    its own guarded transition (post-rebase interaction caught by slice-2
    tests: take_thing's guard silently no-ops on bagged things)."""
    cur = conn.execute(
        "UPDATE things SET holder_kind='avatar', holder_id=? "
        "WHERE id=? AND holder_kind='container' AND holder_id=?",
        (avatar_id, thing_id, container_id),
    )
    conn.commit()
    return cur.rowcount == 1


# --- send home (#88 slice 3) -------------------------------------------------

def send_thing_home(conn, thing, home_place_id: str) -> bool:
    """Remote-move a thing to home_place_id — sets holder_kind='place',
    clears holder_id, and writes place_id to the CALLER's home place (not
    wherever the caller currently stands, unlike drop_thing).

    Guarded on the thing's holder state AS THE CALLER OBSERVED IT, same
    branch-on-source shape as put_thing_in_container (PR #91's guard family):
    server.py's /umwelt/send_home route allows two sources — held-by-caller
    (holder_kind='avatar') or at-rest-in-caller's-place as its author
    (holder_kind='place') — and each branch's WHERE re-asserts exactly the
    state the route observed, so a race that moved the thing between the
    route's read and this write returns False to the loser rather than a
    silently-wrong success (the same race class take_thing/
    put_thing_in_container/take_thing_from_container all guard against).
    Takes the observed thing ROW (dict)."""
    if thing["holder_kind"] == "avatar":
        cur = conn.execute(
            "UPDATE things SET holder_kind='place', holder_id=NULL, place_id=? "
            "WHERE id=? AND holder_kind='avatar' AND holder_id=?",
            (home_place_id, thing["id"], thing["holder_id"]),
        )
    else:
        cur = conn.execute(
            "UPDATE things SET holder_kind='place', holder_id=NULL, place_id=? "
            "WHERE id=? AND holder_kind='place' AND place_id=?",
            (home_place_id, thing["id"], thing["place_id"]),
        )
    conn.commit()
    return cur.rowcount == 1


# --- standpoints ----------------------------------------------------------

def touch_standpoint(conn, agent_id: str, place_id: str) -> None:
    """Record that agent visited place (refresh last_seen_at)."""
    conn.execute(
        "INSERT INTO standpoints(agent_id, place_id, last_seen_at) "
        "VALUES(?, ?, ?) "
        "ON CONFLICT(agent_id, place_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        (agent_id, place_id, now_iso()),
    )
    conn.commit()


# --- crossings ------------------------------------------------------------

# umwelt #133: the default emotion tag when a crossing's sender omits one.
# Defined here (not server.py) since server.py imports db, not the reverse —
# single source of truth for the two hardcoded uses below (build_crossing's
# storage fallback, _crossing_row's read fallback) and server.py's validation
# default, avoiding the literal "calm" drifting out of sync across files.
CROSSING_EMOTION_DEFAULT = "calm"

# Shared column order for crossing reads — single source of truth so the
# SELECTs and _crossing_row can never drift out of sync.
# wake_granted (Gong Spec) is appended at the END, after attesting_authority
# — minimizes diff risk against every existing positional index into this
# tuple (same append-at-end posture as #88's weight_est/holder_kind/holder_id
# on _THING_COLS above). mentions/wakes_granted (umwelt #119's @-mention
# multi-target wake) are appended after wake_granted for the same reason.
# emotion (umwelt #133: avatar look_at status block) is appended after
# wakes_granted, same append-at-end posture.
# reply_to/thread_root/title (umwelt #858, comms D1b) are appended after
# emotion, same append-at-end posture -- keeps every existing positional
# index into this tuple (_crossing_row's r[0..19]) valid.
_CROSSING_COLS = ("seq, id, sender, destination, sent_at, received_at, "
                  "crossing_latency_s, crossing_hops, body, body_hash, "
                  "body_ref, sender_lineage, protocol, to_agent, "
                  "consequence_grade, attesting_authority, wake_granted, "
                  "mentions, wakes_granted, emotion, "
                  "reply_to, thread_root, title")


def build_crossing(conn, id: str, sender: str, destination: str,
                   sent_at: str, received_at: str,
                   crossing_latency_s: float | None,
                   crossing_hops: int = 0,
                   body: str | None = None,
                   body_hash: str | None = None,
                   body_ref: str | None = None,
                   sender_lineage: str | None = None,
                   protocol: str | None = None,
                   to_agent: str | None = None,
                   wake_granted: bool = False,
                   mentions: list[str] | None = None,
                   wakes_granted: list[str] | None = None,
                   emotion: str | None = None,
                   reply_to: int | None = None,
                   thread_root: int | None = None,
                   title: str | None = None) -> int:
    """Insert a crossing; returns seq (monotonic autoincrement id).

    body (inline content), body_ref (pointer), body_hash (integrity) coexist —
    any subset, or none (a pure notification crossing).

    to_agent (optional): addresses the crossing to a specific agent while it
    still lands in `destination`'s shared history — nothing is hidden, only
    the render (crossings_here's viewer_relation) changes with who's reading.
    None = broadcast to whoever's in the place (today's default behavior,
    unchanged for existing callers).

    wake_granted (Gong Spec, default False): server-owned — set by the
    caller (server.py's send_crossing route) only after it has already
    evaluated consent + rate; this function does no evaluation itself, it
    just stores the decision. Never caller(agent)-settable through the
    request payload — see server.py's structural-honesty gate.

    emotion (umwelt #133, default None -> stored as CROSSING_EMOTION_DEFAULT):
    see the dedicated emotion docstring below this one.

    mentions / wakes_granted (umwelt #119, default None -> stored as `[]`):
    the @-mention multi-target wake extension, JSON-encoded arrays (SQLite
    has no native array type). mentions is the filtered/canonicalized list
    of names server.py's send_crossing already validated against the avatar
    registry; wakes_granted is the subset of those that were actually
    granted a wake. Both server-owned in the same "caller passes the
    already-decided result, this function just stores it" sense as
    wake_granted.

    emotion (umwelt #133, default None -> stored as 'calm'): one small
    caller-supplied freeform string tag on the crossing (v0: freeform,
    length-capped — server.py's send_crossing route does the validation +
    capping before calling this function; the `or "calm"` fallback here is
    defense in depth, matching this function's general posture of storing
    already-decided values rather than re-validating them). An avatar's
    look_at status block (server.py's _avatar_status) reads the emotion off
    that agent's most-recently-sent crossing (db.get_last_crossing_by_sender).

    reply_to / thread_root / title (umwelt #858, comms D1b, all default
    None -> stored NULL): server.py's send_crossing route does the full
    validation + thread_root stamping before calling this function --
    reply_to is a validated existing seq (or None), thread_root is
    ENGINE-computed there (the parent's own thread_root, or the parent's
    seq when the parent has none), and title is already stripped/
    length-checked. This function just stores the already-decided values,
    same "caller passes the decision, this just persists it" posture as
    wake_granted/mentions/wakes_granted above -- it does no validation and
    no stamping of its own.
    """
    cur = conn.execute(
        "INSERT INTO crossings(id, sender, destination, sent_at, received_at, "
        "crossing_latency_s, crossing_hops, body, body_hash, body_ref, "
        "sender_lineage, protocol, to_agent, consequence_grade, "
        "attesting_authority, wake_granted, mentions, wakes_granted, emotion, "
        "reply_to, thread_root, title) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'consequence', 'world', "
        "?, ?, ?, ?, ?, ?, ?)",
        (id, sender, destination, sent_at, received_at,
         crossing_latency_s, crossing_hops, body, body_hash, body_ref,
         sender_lineage, protocol, to_agent, int(wake_granted),
         json.dumps(mentions or []), json.dumps(wakes_granted or []),
         emotion or CROSSING_EMOTION_DEFAULT,
         reply_to, thread_root, title),
    )
    conn.commit()
    return cur.lastrowid


# --- wake blocks + rate (Gong Spec) -----------------------------------------
#
# The gong is a LAW governing which pulled crossings are allowed to escalate
# to a wake for whichever poller reads them — delivery is always PULL (see
# server.py's send_crossing docstring for the full mechanism). These
# functions are dumb setters/checkers, same posture as the trash/carry
# sections above: server.py's send_crossing route owns evaluation order
# (consent, then rate) and response shaping; these just answer or record one
# fact each. Both target_agent/blocked_sender/sender args below must already
# be canonicalized by the caller (server.py's _canonical_agent) — this
# module does no folding itself, matching the rest of db.py's convention of
# storage functions trusting caller-canonicalized identity (see
# get_or_create_avatar's docstring for the same convention stated in full).

def is_wake_blocked(conn, target_agent: str, sender: str) -> bool:
    """True if target_agent has blocked sender from waking them."""
    row = conn.execute(
        "SELECT 1 FROM wake_blocks WHERE target_agent = ? AND blocked_sender = ?",
        (target_agent, sender),
    ).fetchone()
    return row is not None


def add_wake_block(conn, target_agent: str, blocked_sender: str) -> None:
    """Idempotent insert (INSERT OR IGNORE) — blocking an already-blocked
    sender is a no-op, not an error."""
    conn.execute(
        "INSERT OR IGNORE INTO wake_blocks(target_agent, blocked_sender, created_at) "
        "VALUES (?, ?, ?)",
        (target_agent, blocked_sender, now_iso()),
    )
    conn.commit()


def remove_wake_block(conn, target_agent: str, blocked_sender: str) -> None:
    """Idempotent delete — unblocking a sender that wasn't blocked is a no-op."""
    conn.execute(
        "DELETE FROM wake_blocks WHERE target_agent = ? AND blocked_sender = ?",
        (target_agent, blocked_sender),
    )
    conn.commit()


def is_human_sender(conn, agent_id: str) -> bool:
    """True if agent_id is a human resident — "always allowed" (Gong Spec,
    generalized past a lei/leona name-hardcode, forum #250/PR #108 design
    round with Borges+Kepler): the rate budget exists to keep AGENT-TO-AGENT
    wake-spending scarce, and a human reaching for an agent isn't in that
    scope, so this check exempts any human sender from both consent and
    rate — not just two named identities. Reads the registered avatar's
    caller-declared `type` (from /umwelt/register's `type` field, stored in
    the avatar-thing's metadata) — trust-scoped-private-network like everything else
    keyed on caller-declared identity, not engine-attested (#24).

    Falls back to a name check against `_ids.human_residents()` for the case a
    human hasn't called /umwelt/register yet (no avatar to look up type on)
    — defense in depth, per Borges's review on PR #108; every resident is
    also covered once they've registered with type="human", the fallback's
    role is only to cover the pre-registration window. The fold is applied
    internally here (via `canonical_agent`) rather than trusted from the
    caller, covering BOTH the roster check and the avatar lookup below, so
    this check can't be skipped — or silently missed on a mixed-case
    `agent_id` — by relying on the caller to have folded first.
    """
    agent_id = canonical_agent(agent_id)
    if agent_id in human_residents():
        return True
    avatar = get_avatar(conn, agent_id)
    return bool(avatar) and avatar["metadata"].get("type") == "human"


def list_wake_blocks(conn, target_agent: str) -> list[str]:
    """Blocked-sender list for target_agent, alphabetical."""
    rows = conn.execute(
        "SELECT blocked_sender FROM wake_blocks WHERE target_agent = ? "
        "ORDER BY blocked_sender ASC",
        (target_agent,),
    ).fetchall()
    return [r[0] for r in rows]


# Gong Spec v0 rate tunables — fixed in code, never caller-supplied (mirrors
# CARRY_CAPACITY_DEFAULT's "the scarcity coefficient is set by the world"
# posture). A future calibration pass edits these two constants; nothing else.
WAKE_RATE_WINDOW_SECONDS = 300  # 5 minutes
WAKE_RATE_BURST = 3             # max grants per sender->target per window


def wake_rate_ok(conn, sender: str, target_agent: str) -> bool:
    """True if sender is within their wake-budget toward target_agent:
    fewer than WAKE_RATE_BURST grants from sender to target_agent in the
    last WAKE_RATE_WINDOW_SECONDS, measured against received_at (the
    engine-stamped clock — sent_at is caller-supplied display-only per this
    file's crossings-table comment, never a cursor/time-boundary;
    received_at is the trustworthy engine clock, same reasoning the seq
    cursor already applies to sent_at vs. ordering).

    Counts grants from BOTH wake paths (umwelt #119): the 1:1 `to_agent`+
    `wake_granted` path, and the @-mention `wakes_granted` path. Without the
    second arm, a mentions-only crossing (to_agent NULL, its own
    wake_granted column False — that column reflects the 1:1 path only)
    would never match this query no matter how many times sender mentioned
    target_agent, making the budget a no-op for the mentions path — the
    exact "two gates that can disagree" risk this file's docstrings
    elsewhere name explicitly.

    The mentions arm matches EXACT array membership via SQLite's json1
    `json_each` table-valued function, not a LIKE substring — a first
    version used `wakes_granted LIKE '%"' || target_agent || '"%'`, which
    Borges's security review (PR #121) caught as unsafe: `canonical_agent`
    is casefold-only with no charset gate, so a target name containing a
    LIKE metacharacter (`_`/`%`) silently over-matches (`test_1` matches
    `testX1` too), and a name containing a quote or backslash character alters json.dumps's
    escaping enough to under-match — re-opening the exact no-op this arm
    exists to close, for that one exotic-named target. `json_each` decodes
    the stored JSON array and compares actual string VALUES, sidestepping
    both classes entirely.
    """
    # Format the cutoff with the SAME strftime pattern now_iso() uses (not
    # datetime.isoformat(), which would emit a "+00:00" suffix instead of
    # "Z" and risk a lexicographic mismatch against received_at's stored
    # "...Z"-suffixed strings on an exact-microsecond tie) — string
    # comparison below only orders correctly when both sides share one
    # format.
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=WAKE_RATE_WINDOW_SECONDS)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row = conn.execute(
        "SELECT COUNT(*) FROM crossings WHERE sender = ? AND received_at > ? "
        "AND ((to_agent = ? AND wake_granted = 1) OR EXISTS ("
        "  SELECT 1 FROM json_each(crossings.wakes_granted) WHERE value = ?"
        "))",
        (sender, cutoff, target_agent, target_agent),
    ).fetchone()
    return row[0] < WAKE_RATE_BURST


def get_crossings_since(conn, destination: str,
                        since_seq: int | None,
                        limit: int | None = None) -> list[dict]:
    """Return crossings with seq > since_seq, ordered by seq ASC.

    If since_seq is None, returns all crossings for the destination.
    Cursor is seq (not sent_at) to avoid silent drops on sent_at ties.

    `limit` (umwelt #157 — bounding the query, not just the serialized
    output): when given, fetches the NEWEST `limit` qualifying rows via
    `ORDER BY seq DESC LIMIT ?`, then reverses them before building the
    return list — so the RETURN ORDER stays ASC exactly like the unbounded
    path below (every existing caller depends on ascending order; this must
    not change it). When `limit` is None (the default), behavior is
    byte-for-byte unchanged from before this parameter existed.
    """
    if limit is not None:
        if since_seq is not None:
            rows = conn.execute(
                f"SELECT {_CROSSING_COLS} FROM crossings "
                "WHERE destination = ? AND seq > ? ORDER BY seq DESC LIMIT ?",
                (destination, since_seq, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_CROSSING_COLS} FROM crossings "
                "WHERE destination = ? ORDER BY seq DESC LIMIT ?",
                (destination, limit),
            ).fetchall()
        rows = list(reversed(rows))
    elif since_seq is not None:
        rows = conn.execute(
            f"SELECT {_CROSSING_COLS} FROM crossings "
            "WHERE destination = ? AND seq > ? ORDER BY seq ASC",
            (destination, since_seq),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_CROSSING_COLS} FROM crossings "
            "WHERE destination = ? ORDER BY seq ASC",
            (destination,),
        ).fetchall()
    return [_crossing_row(r) for r in rows]


def get_crossings_for_addressee(conn, viewer: str, addressee: str,
                                 since_seq: int | None,
                                 limit: int | None = None) -> list[dict]:
    """Return crossings addressed to `addressee` -- ACROSS EVERY PLACE, not
    one destination -- ordered by seq ASC. umwelt#539 (human inbox): a
    crossing to a human can land in any place, so get_crossings_since's
    per-destination scope can't answer "everything addressed to me" without
    the caller already knowing every place to poll.

    `viewer` is REQUIRED, no default (Ariadne's ruling, umwelt#539/#512)
    -- and today's `viewer == addressee` check (`ValueError` otherwise) is
    NOT a security gate: `viewer` is caller-declared, same as every other
    identity in this codebase before credentials land (#495). What it buys:
    the credential cutover becomes one argument's meaning changing (caller-
    declared -> authenticated), not a signature migration each call site
    must be revisited for -- and a cross-agent read is a loud, greppable
    `ValueError` today instead of the endpoint's default behaviour. See
    `may_view`'s design (docs/design/privacy-authorization.md §3.2) for the
    same argument made about the read layer generally.

    Rule 4 (umwelt#512/#538, revision 3) makes a crossing addressed to
    Lei specifically public -- **that does not generalize.** This is a
    generic addressee-scoped fetch; a crossing addressed to some other
    agent inside a private place is NOT public (rule 4's place-visibility
    clause still applies to it), and nothing here evaluates that clause.
    The `viewer == addressee` restriction is what keeps this route honest
    in the meantime: it can only ever answer "what was addressed to ME,"
    never "what was addressed to them." Widening this to a real cross-
    agent read needs `may_view`, not a loosened check here.

    "Addressed to" matches server.py's `partition_crossings_for_inbox`
    exactly (this function is its fetch-side counterpart, deliberately kept
    a separate concern per that function's own docstring): `to_agent =
    addressee` (the 1:1 path) OR `addressee` appears in the `mentions` JSON
    array (the @-mention path). The two are UNIONed (SQL `UNION`, not
    `UNION ALL` -- a crossing matching both arms must not be double-
    counted), not kept as separate buckets -- partition_crossings_for_inbox
    does that split itself once it has the combined list.

    **Two SELECTs UNIONed, not one OR'd WHERE clause -- measured, not
    assumed** (Ariadne's note): `WHERE to_agent = ? OR EXISTS (json_each
    ...)` defeats `idx_crossings_to_agent_seq` entirely -- confirmed via
    `EXPLAIN QUERY PLAN`, SQLite falls back to a full `SCAN crossings`
    rather than use the index across an OR with a table-valued function on
    the other side. The UNIONed form lets the `to_agent` arm use `SEARCH
    ... USING COVERING INDEX idx_crossings_to_agent_seq` while only the
    `mentions` arm full-scans -- same unindexed shape wake_rate_ok's
    `wakes_granted` arm already accepts, at comparable crossing volume.

    `mentions` membership uses SQLite's json1 `json_each` table-valued
    function for exact array-membership matching, not a LIKE substring --
    same reasoning and same pattern as wake_rate_ok's wakes_granted arm
    (db.py): a LIKE scan is unsafe against a target name containing a LIKE
    metacharacter or a quote/backslash character.

    since_seq / limit follow get_crossings_since's exact contract: since_seq
    None returns everything; limit takes the NEWEST `limit` qualifying rows
    via ORDER BY seq DESC LIMIT ? then reverses them, so return order is
    always ascending regardless of which path is taken.
    """
    if viewer != addressee:
        raise ValueError(
            f"viewer {viewer!r} may not fetch {addressee!r}'s addressed "
            "crossings -- get_crossings_for_addressee only answers "
            "'what was addressed to me' (umwelt#539/#512 ruling)")

    since_clause = " AND seq > ?" if since_seq is not None else ""
    since_params = [since_seq] if since_seq is not None else []
    union_sql = (
        f"SELECT {_CROSSING_COLS} FROM crossings "
        f"WHERE to_agent = ?{since_clause} "
        "UNION "
        f"SELECT {_CROSSING_COLS} FROM crossings "
        f"WHERE EXISTS (SELECT 1 FROM json_each(crossings.mentions) "
        f"WHERE value = ?){since_clause}"
    )
    union_params = [addressee, *since_params, addressee, *since_params]

    if limit is not None:
        rows = conn.execute(
            f"SELECT * FROM ({union_sql}) ORDER BY seq DESC LIMIT ?",
            (*union_params, limit),
        ).fetchall()
        rows = list(reversed(rows))
    else:
        rows = conn.execute(
            f"SELECT * FROM ({union_sql}) ORDER BY seq ASC",
            union_params,
        ).fetchall()
    return [_crossing_row(r) for r in rows]


def search_crossings(conn, viewer: str, terms: list[str],
                      place: str | None, sender: str | None,
                      limit: int, visible=None) -> tuple[list[dict], bool]:
    """Headline-only text search over crossings (umwelt#863, comms QoL D4).

    `viewer` is REQUIRED, no default — docs/design/privacy-authorization.md
    §3.2's guarantee, not convention: a call site that forgets it is a
    `TypeError` at import/collection time, not a silently-open read. This
    function IS the enforcement point (the read, not the route) for the
    one load-bearing rule (umwelt#863 invariant 4/Kepler's rider): a
    crossing whose destination is a private place `viewer` does not own is
    excluded from BOTH the results and the truncated count, indistinguishably
    from a crossing that never existed.

    NOTE (umwelt#862 D3): the server's `search_crossings` route now passes
    `visible=` (`visibility.discovery_visible`), which skips this SQL gate; the
    gate below remains as the default when no predicate is passed, and as the
    parity reference pinned by its test.

    The SQL gate below — `destination IN (SELECT id FROM places WHERE
    kind != 'private' OR owner = ?)` — encodes `_place_interior_visible_to`
    exactly (`place["kind"] != "private" or (viewer is not None and
    place["owner"] == viewer)`; `viewer` is always a resolved, non-None
    caller by the time this function runs, so the `viewer is not None`
    half of that predicate is unconditionally true here and is folded into
    the single `owner = ?` comparison). A subquery rather than a JOIN: a
    JOIN against `places` would make every column in `_CROSSING_COLS`
    ambiguous against `places.id`, and a subquery filters BEFORE the
    `LIMIT` below applies, which is the property invariant 4 actually
    needs (limit-after-filter, not filter-after-limit — filtering after a
    limit would let a hidden row silently consume a slot a visible row
    should have had, and would make `truncated` sensitive to rows the
    viewer can't see). Pinned against `_place_interior_visible_to` by a
    small parity test in
    the internal crossings-visibility test (not in this release; umwelt#863 §5).

    `terms`: already split (whitespace) and bounded by the caller
    (server.py's 400 checks) — every term must match (AND) against `body`
    OR the STORED `title` column, never the read-derived envelope title a
    ref-only crossing (`body IS NULL`) always has `envelope["title"] == ""`
    for, per `crossing_envelope`'s own docstring — matching against the
    envelope would make "a ref-only crossing matches on title only"
    (umwelt#863 invariant 2) impossible for exactly the case it exists to
    cover. Each term is escaped for the three SQLite LIKE metacharacters
    (`\\` first, then `%`, then `_`) and wrapped `%term%`, `ESCAPE '\\'`.

    `place`: an ALREADY-validated (exists, and visible-or-viewer-is-owner)
    destination filter — server.py's route does the 404/private-403 checks
    before calling this; by the time `place` reaches here it is a bare
    equality filter, not a re-validation. `None` means no place filter.

    `sender`: an already-canonical-folded exact match, or `None` for no
    filter.

    `limit`: the caller's already-clamped [1, 50] value. This fetches
    `limit + 1` of the VISIBLE set (`ORDER BY seq DESC`) and returns
    `(rows[:limit], truncated)` where `truncated = len(fetched) > limit` —
    so `truncated` only ever reflects rows the viewer could see.

    `visible` (umwelt#862 D3, optional): a per-crossing predicate
    `visible(crossing_dict) -> bool` that REPLACES the SQL place gate above
    -- server.py's route passes `visibility.discovery_visible`, so the search
    policy lives in the one shared module rather than in a second copy of it.
    Rows are then read in `seq DESC` batches and kept while `visible` says
    yes, stopping at `limit + 1` KEPT rows, so the limit-after-filter
    property above holds unchanged (a hidden row never consumes a slot or
    feeds `truncated`). Left None, the SQL gate applies exactly as before
    (and stays pinned to `_place_interior_visible_to` by
    test_search_crossings_visibility.py).

    Read-only: no standpoint/event/crossing write of any kind.
    """
    conditions = []
    params: list[Any] = []
    if visible is None:
        conditions.append(
            "destination IN (SELECT id FROM places "
            "WHERE kind != 'private' OR owner = ?)")
        params.append(viewer)
    for term in terms:
        escaped = (term.replace("\\", "\\\\")
                       .replace("%", "\\%")
                       .replace("_", "\\_"))
        like = f"%{escaped}%"
        conditions.append(
            "(body LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')")
        params.extend([like, like])
    if place is not None:
        conditions.append("destination = ?")
        params.append(place)
    if sender is not None:
        conditions.append("sender = ?")
        params.append(sender)
    where_clause = " AND ".join(conditions) if conditions else "1"
    fetch_limit = limit + 1
    if visible is None:
        rows = conn.execute(
            f"SELECT {_CROSSING_COLS} FROM crossings "
            f"WHERE {where_clause} ORDER BY seq DESC LIMIT ?",
            (*params, fetch_limit),
        ).fetchall()
        truncated = len(rows) > limit
        return [_crossing_row(r) for r in rows[:limit]], truncated
    kept: list[dict] = []
    before_seq: int | None = None
    while len(kept) < fetch_limit:
        page_where, page_params = where_clause, list(params)
        if before_seq is not None:
            page_where += " AND seq < ?"
            page_params.append(before_seq)
        batch = conn.execute(
            f"SELECT {_CROSSING_COLS} FROM crossings "
            f"WHERE {page_where} ORDER BY seq DESC LIMIT ?",
            (*page_params, 200),
        ).fetchall()
        if not batch:
            break
        for r in batch:
            c = _crossing_row(r)
            if visible(c):
                kept.append(c)
                if len(kept) >= fetch_limit:
                    break
        before_seq = batch[-1][0]
        if len(batch) < 200:
            break
    return kept[:limit], len(kept) > limit


def get_crossings_since_all(conn, since_seq: int) -> list[dict]:
    """Return ALL crossings across EVERY destination with seq > since_seq,
    ordered by seq ASC -- the cross-place counterpart to get_crossings_since
    (which is destination-scoped) and get_crossings_for_addressee (which is
    addressee-scoped). umwelt#857 D5 (whats_new, comms): the route needs a
    single seq-windowed sweep across the whole world in one query, then
    partitions the result itself by per-place visibility / addressing --
    unlike get_crossings_for_addressee, this function does no viewer
    filtering at all; it hands back everything in the window and lets the
    caller (server.py's whats_new route) decide what a given viewer may
    see.

    `since_seq` is NOT optional here (unlike get_crossings_since's since_seq)
    -- whats_new's contract requires an explicit, caller-supplied `since`
    (Lei's ruling: the world holds no per-viewer memory of what's been
    read), so there is no "give me everything" case to support at this
    layer.
    """
    rows = conn.execute(
        f"SELECT {_CROSSING_COLS} FROM crossings WHERE seq > ? ORDER BY seq ASC",
        (since_seq,),
    ).fetchall()
    return [_crossing_row(r) for r in rows]


# umwelt#855 (comms D1a): headline length for a crossing's read-derived
# `title`. An agent sees size + title before deciding to read a body, so
# the headline must never carry more than this many body characters.
CROSSING_TITLE_MAX = 80


def crossing_envelope(body: str | None, body_ref: str | None) -> dict:
    """Read-derived envelope for one crossing (umwelt#855): `size` and
    `title`, computed from the stored body, never stored themselves.

    - size: len(body) in characters (code points). None only when body is
      None and body_ref is set (the content lives in CAS; the caller reads
      body_ref). A non-None body wins over body_ref even when it is "", the
      same precedence the record route uses when it snapshots a crossing,
      so body="" with a body_ref gives size 0.
    - title: the first line of body with non-whitespace content, stripped;
      longer than CROSSING_TITLE_MAX -> its first CROSSING_TITLE_MAX-1
      characters plus an ellipsis. "" when the body is empty or ref-only.
    """
    if body is None:
        return {"size": None if body_ref else 0, "title": ""}
    title = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    if len(title) > CROSSING_TITLE_MAX:
        title = title[:CROSSING_TITLE_MAX - 1] + "…"
    return {"size": len(body), "title": title}


def _crossing_row(r) -> dict:
    # umwelt#858: title precedence -- a non-empty STORED title (r[22]) wins
    # over crossing_envelope's read-derived one; when there is no stored
    # title (NULL), the derived title from #855 is unchanged. `size` is
    # never touched by this -- only `title` inside the envelope dict.
    envelope = crossing_envelope(r[8], r[10])
    if r[22]:
        envelope["title"] = r[22]
    return {
        "seq": r[0], "id": r[1], "sender": r[2], "destination": r[3],
        "sent_at": r[4], "received_at": r[5], "crossing_latency_s": r[6],
        "crossing_hops": r[7], "body": r[8], "body_hash": r[9],
        "body_ref": r[10], "sender_lineage": r[11], "protocol": r[12],
        "to_agent": r[13],
        "consequence_grade": r[14], "attesting_authority": r[15],
        "wake_granted": bool(r[16]),
        "mentions": json.loads(r[17]) if r[17] else [],
        "wakes_granted": json.loads(r[18]) if r[18] else [],
        "emotion": r[19] or CROSSING_EMOTION_DEFAULT,
        "reply_to": r[20], "thread_root": r[21],
        **envelope,
    }


def get_crossing_seq(conn, crossing_id: str) -> int | None:
    """Return the seq for crossing_id, or None if not found."""
    row = conn.execute(
        "SELECT seq FROM crossings WHERE id = ?", (crossing_id,)
    ).fetchone()
    return row[0] if row else None


def get_crossing(conn, crossing_id: str) -> dict | None:
    """Fetch a single crossing by its (uuid4) id — the full row, same shape
    as get_crossings_since's entries. Feeds the `record` verb's
    kind='crossing' lookup (umwelt #134 slice 1)."""
    row = conn.execute(
        f"SELECT {_CROSSING_COLS} FROM crossings WHERE id = ?", (crossing_id,)
    ).fetchone()
    return _crossing_row(row) if row else None


def get_crossing_by_seq(conn, seq: int) -> dict | None:
    """Fetch a single crossing by its `seq` (the monotonic integer cursor),
    same shape as get_crossing. Feeds send_crossing's `reply_to` validation
    + thread_root stamping (umwelt #858, comms D1b): the route needs both
    "does this seq name a real crossing" and, when it does, that parent's
    own thread_root (or seq, when it has none) to stamp the reply's
    thread_root -- one lookup answers both.

    Deliberately NOT exposed as a route (unlike crossing_lookup, which is
    uuid4-only by design -- see that route's docstring for why a seq-based
    HTTP lookup would be enumerable). This is an internal db-layer helper
    only; send_crossing calls it server-side to validate a caller-supplied
    seq that already names something real, not to let a caller resolve an
    arbitrary seq to content.
    """
    row = conn.execute(
        f"SELECT {_CROSSING_COLS} FROM crossings WHERE seq = ?", (seq,)
    ).fetchone()
    return _crossing_row(row) if row else None


def iter_thread_crossings(conn, root_seq: int):
    """Yield every crossing of the thread rooted at `root_seq` (the root
    itself plus every crossing whose `thread_root` is `root_seq`), full-row
    dicts, `seq ASC` (oldest first). A generator so /umwelt/thread can stop
    reading once it has kept its bounded number of VIEWABLE rows -- a thread
    full of rows the viewer may not see must not be materialized whole.
    Does no visibility filtering: the caller applies `visibility`."""
    cur = conn.execute(
        f"SELECT {_CROSSING_COLS} FROM crossings "
        "WHERE seq = ? OR thread_root = ? ORDER BY seq ASC",
        (root_seq, root_seq),
    )
    while True:
        rows = cur.fetchmany(100)
        if not rows:
            return
        for r in rows:
            yield _crossing_row(r)


def get_last_crossing_by_sender(conn, sender: str) -> dict | None:
    """Most recent crossing SENT by `sender` (ORDER BY seq DESC — seq is the
    monotonic insert-order cursor this whole file already treats as
    authoritative ordering, e.g. get_crossings_since), or None if `sender`
    has never sent one. Feeds avatar look_at's status block (umwelt #133,
    server.py's _avatar_status): 'last action' when it's a crossing, and
    'emotion' — an avatar's emotion is defined as the emotion tag on this
    row (default 'calm', per build_crossing's fallback, when the agent has
    sent crossings before #133 shipped, or never set one explicitly)."""
    row = conn.execute(
        f"SELECT {_CROSSING_COLS} FROM crossings WHERE sender = ? "
        "ORDER BY seq DESC LIMIT 1",
        (sender,),
    ).fetchone()
    return _crossing_row(row) if row else None


def count_unseen(conn, place_id: str, since_seq: int | None,
                  viewer: str | None = None) -> int:
    """Count crossings for place_id with seq > since_seq.

    `viewer` (optional, umwelt bug fix — self-sent crossings must never
    inflate the viewer's OWN unread/unseen count) excludes crossings the
    viewer sent themselves: a `say` you make in a place is not "unread" TO
    you. `viewer` is expected already-canonical (case-folded), same
    convention as every other db.py function comparing against a stored
    identity column — callers must canonicalize before passing it in. When
    `viewer` is None (the pre-existing default), no sender is excluded —
    back-compat for callers that intentionally want a raw, viewer-agnostic
    total (e.g. crossings_here's pagination-completeness count, which must
    stay consistent with the full crossing list it paginates over)."""
    if since_seq is None:
        if viewer is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM crossings WHERE destination = ?",
                (place_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM crossings WHERE destination = ? "
                "AND sender != ?",
                (place_id, viewer),
            ).fetchone()
    else:
        if viewer is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM crossings WHERE destination = ? "
                "AND seq > ?",
                (place_id, since_seq),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM crossings WHERE destination = ? "
                "AND seq > ? AND sender != ?",
                (place_id, since_seq, viewer),
            ).fetchone()
    return row[0] if row else 0


def get_unseen_summary(conn, agent: str) -> dict[str, Any]:
    """Per-agent, all-places unseen summary (umwelt #140).

    Read-only, cursor-preserving — a peek, never advances any standpoint
    (same posture as count_unseen and crossings_here's non-advance path).
    Every existing unseen primitive (count_unseen, move_to/enter's
    unseen_count) is scoped to ONE place; this is the first all-places
    one, replacing the plugin hook's home-Post+Square approximation and
    the pager-bell's ~25-place poll (both named as workarounds in #140).

    Scope: places the agent has an existing standpoint row in (has stood
    there at least once — `touch_standpoint`/`crossings_here` create one on
    first arrival/read, and, since #890, so does `/umwelt/trace` on an
    enterable place, so a room the agent has `hear()`d is counted from then
    on). A crossing addressed to the agent in a place they have NEVER
    walked or read has no cursor baseline for "since", so it can't be
    counted here — a known v0 gap, which also still holds for a private
    room the agent may not enter (trace refuses to create a row there),
    safe in practice once #117 (receiver-
    owned DM inbox) lands, since a DM's destination is then always the
    receiver's own home Post, where they always have a standpoint.

    Self-sent exclusion mirrors count_unseen's `viewer` param exactly (a
    crossing you sent is never "unseen" to you) — same fix, same reasoning,
    reused rather than re-derived.

    `addressed`/`has_wake` cover BOTH wake paths (umwelt #119 — same dual-
    path check as _wake_within_budget's SQL and the bell's relevant_bell):
    the 1:1 `to_agent`+`wake_granted` path, and the @-mention `mentions`+
    `wakes_granted` path. Checking only the first is the exact bug this
    codebase already shipped once (test_umwelt_bell.py's documented
    incident) — an @-mention crossing has to_agent=NULL and its own
    wake_granted column False (that column reflects the 1:1 path only), so
    a mentions-only check against `to_agent == agent` silently never fires
    no matter how many times the agent is @-mentioned. Both paths gate a
    crossing addressed to this agent specifically — a grant meant for a
    different agent sharing the same place must never flip THIS agent's
    has_wake (the response is per-agent, not per-place).

    Returns {"total": N, "addressed": M, "by_place": {place_id: count},
    "has_wake": bool}. `by_place` omits zero-count places — a sparse map,
    not a listing of every place the agent has ever stood in.

    Performance: one query per standpoint row (index-backed via
    idx_crossings_dest_seq, so a round-trip-count concern rather than a
    full-scan one) — O(places) round trips, acceptable for a v0 read
    primitive; revisit if a per-turn caller makes this a hot path.
    """
    standpoint_rows = conn.execute(
        "SELECT place_id, last_position FROM standpoints WHERE agent_id = ?",
        (agent,),
    ).fetchall()

    by_place: dict[str, int] = {}
    total = 0
    addressed = 0
    has_wake = False

    for place_id, last_position in standpoint_rows:
        if last_position is None:
            rows = conn.execute(
                "SELECT to_agent, wake_granted, mentions, wakes_granted "
                "FROM crossings WHERE destination = ? AND sender != ?",
                (place_id, agent),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT to_agent, wake_granted, mentions, wakes_granted "
                "FROM crossings WHERE destination = ? AND seq > ? AND sender != ?",
                (place_id, last_position, agent),
            ).fetchall()
        if not rows:
            continue
        by_place[place_id] = len(rows)
        total += len(rows)
        for to_agent, wake_granted, mentions_json, wakes_granted_json in rows:
            mentioned = agent in (json.loads(mentions_json) if mentions_json else [])
            wake_for_agent = agent in (
                json.loads(wakes_granted_json) if wakes_granted_json else []
            )
            if to_agent == agent or mentioned:
                addressed += 1
                if (to_agent == agent and wake_granted) or wake_for_agent:
                    has_wake = True

    return {
        "total": total,
        "addressed": addressed,
        "by_place": by_place,
        "has_wake": has_wake,
    }


# --- standpoints (extended) -----------------------------------------------

def get_standpoint(conn, agent_id: str, place_id: str) -> dict | None:
    """Return standpoint row, or None. Includes last_position cursor."""
    row = conn.execute(
        "SELECT agent_id, place_id, last_seen_at, last_position "
        "FROM standpoints WHERE agent_id = ? AND place_id = ?",
        (agent_id, place_id),
    ).fetchone()
    if row is None:
        return None
    return {"agent_id": row[0], "place_id": row[1],
            "last_seen_at": row[2], "last_position": row[3]}


def update_standpoint_trace(conn, agent_id: str, place_id: str,
                             seq: int) -> None:
    """Advance last_position cursor to seq and refresh last_seen_at."""
    conn.execute(
        "UPDATE standpoints SET last_position = ?, last_seen_at = ? "
        "WHERE agent_id = ? AND place_id = ?",
        (seq, now_iso(), agent_id, place_id),
    )
    conn.commit()


def upsert_standpoint_position(conn, agent_id: str, place_id: str,
                                seq: int) -> None:
    """Set last_position cursor to seq, creating the standpoint row if it
    doesn't exist yet (umwelt #157 — crossings_here's `advance` path; and,
    since umwelt #890, /umwelt/trace's no-standpoint path).

    Unlike update_standpoint_trace above (a bare UPDATE that silently no-ops
    when the agent has no standpoints row yet — the first-visit bug), this
    is a single atomic upsert modeled on touch_standpoint's ON CONFLICT
    shape, so a never-traced agent's first `advance=true` read still
    persists its cursor instead of appearing to succeed while writing
    nothing. Used by crossings_here's advance path and by /umwelt/trace when
    the caller has no standpoint yet (#890, after move_to's entry gate);
    /umwelt/trace still uses update_standpoint_trace for an existing row."""
    conn.execute(
        "INSERT INTO standpoints(agent_id, place_id, last_seen_at, last_position) "
        "VALUES(?, ?, ?, ?) "
        "ON CONFLICT(agent_id, place_id) DO UPDATE SET "
        "last_position = excluded.last_position, "
        "last_seen_at = excluded.last_seen_at",
        (agent_id, place_id, now_iso(), seq),
    )
    conn.commit()


def get_last_active_at(conn, agent_id: str) -> str | None:
    """Most recent 'presence' timestamp for agent_id — avatar look_at's
    status block (umwelt #133, server.py's _avatar_status). Folds together
    THREE signals: the MAX of every standpoint's last_seen_at across all
    places this agent has touched (touch_standpoint fires on /enter and
    /move_to — see server.py) AND this agent's most-recently-SENT crossing's
    received_at (sending a crossing does NOT itself call touch_standpoint,
    so an agent who messages from a place without re-entering/moving would
    look falsely stale if standpoints were the only signal) AND (Phase 2
    heartbeat, forum #268) this agent's most recent agent_heartbeat.
    last_beat_at (in-session tool-call work with no in-world movement would
    otherwise ALSO look falsely stale — the gap the heartbeat closes). All
    three columns are now_iso()-stamped with the same format, so a plain
    string MAX is a correct chronological comparison (same posture as
    wake_rate_ok's cutoff comment elsewhere in this file). None if agent_id
    has neither a standpoint, a sent crossing, nor a heartbeat."""
    row = conn.execute(
        "SELECT MAX(t) FROM ("
        "  SELECT MAX(last_seen_at) AS t FROM standpoints WHERE agent_id = ? "
        "  UNION ALL "
        "  SELECT MAX(received_at) AS t FROM crossings WHERE sender = ? "
        "  UNION ALL "
        "  SELECT MAX(last_beat_at) AS t FROM agent_heartbeat WHERE agent = ?"
        ")",
        (agent_id, agent_id, agent_id),
    ).fetchone()
    return row[0] if row else None


# --- use telemetry (#54) ---------------------------------------------------
#
# Observer, not gate: server.py wraps record_use_event in try/except so a
# telemetry write failure never fails the use() response — see the caller.

def record_use_event(conn, thing_id: str, agent: str, kind: str,
                      exit_code: int | None = None,
                      timed_out: bool = False,
                      elapsed_s: float | None = None,
                      error: str | None = None) -> None:
    """Record one use() outcome. Caller decides whether an outcome is real
    enough to record (precondition failures — 404/422 — are not uses)."""
    conn.execute(
        "INSERT INTO use_events(thing_id, agent, used_at, kind, exit_code, "
        "timed_out, elapsed_s, error) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (thing_id, agent, now_iso(), kind, exit_code, int(timed_out),
         elapsed_s, error),
    )
    conn.commit()


def get_last_use_event_by_agent(conn, agent: str) -> dict | None:
    """Most recent use_event recorded for `agent` (ORDER BY id DESC —
    use_events has no seq column, but `id` is INTEGER PRIMARY KEY
    AUTOINCREMENT, the same monotonic insert-order guarantee), or None if
    `agent` has never used() anything. Feeds avatar look_at's status block
    (umwelt #133, server.py's _avatar_status): 'last action' when the
    agent's most recent use_event is newer than their most recent sent
    crossing (db.get_last_crossing_by_sender)."""
    row = conn.execute(
        "SELECT thing_id, used_at, kind FROM use_events WHERE agent = ? "
        "ORDER BY id DESC LIMIT 1",
        (agent,),
    ).fetchone()
    if row is None:
        return None
    return {"thing_id": row[0], "used_at": row[1], "kind": row[2]}


def use_stats(conn, thing_id: str) -> dict[str, Any]:
    """Aggregate use telemetry for a thing: {use_count, last_used_at,
    success_count, success_rate}.

    success = executable use with exit_code==0 and not timed_out. Readable
    opens count toward use_count/last_used_at but are excluded from the
    success denominator (they have no pass/fail outcome). success_rate is
    None when the success denominator is 0 (no executable uses yet).

    kind='params_spec' events (umwelt #396 — a use() call that returned the
    caller's "--help" instead of running, either bare empty args or a
    malformed bind) are excluded from use_count/last_used_at entirely, same
    posture as excluding them from the success denominator: asking for
    documentation isn't a use, and letting it inflate "how much has this
    been used"/bump "last used" would conflate the two right at the one
    place a caller checks either number.
    """
    row = conn.execute(
        "SELECT COUNT(*), MAX(used_at), "
        "       SUM(CASE WHEN kind='executable' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN kind='executable' AND exit_code=0 "
        "                AND timed_out=0 THEN 1 ELSE 0 END) "
        "FROM use_events WHERE thing_id = ? AND kind != 'params_spec'",
        (thing_id,),
    ).fetchone()
    use_count, last_used_at, exec_count, success_count = row
    exec_count = exec_count or 0
    success_count = success_count or 0
    success_rate = (success_count / exec_count) if exec_count else None
    return {
        "use_count": use_count or 0,
        "last_used_at": last_used_at,
        "success_count": success_count,
        "success_rate": success_rate,
    }


def get_use_event(conn, use_event_id: int) -> dict[str, Any] | None:
    """Fetch a single use_events row by its integer id (umwelt #134 slice 1 —
    the `record` verb's kind='use' lookup). Returns the full row (all
    columns), unlike get_last_use_event_by_agent's narrower projection."""
    row = conn.execute(
        "SELECT id, thing_id, agent, used_at, kind, exit_code, timed_out, "
        "elapsed_s, error FROM use_events WHERE id = ?",
        (use_event_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "thing_id": row[1], "agent": row[2], "used_at": row[3],
        "kind": row[4], "exit_code": row[5], "timed_out": bool(row[6]),
        "elapsed_s": row[7], "error": row[8],
    }


# --- citable event records (umwelt #134 slice 1) ---------------------------
#
# docs/design/cite-the-world.md §3: `record` mints an immutable, engine-
# append-only `events` row from an underlying crossings/use_events/
# action_log entry. server.py's /umwelt/record route owns the per-kind
# lookup + snapshot-building (CAS deref, canonical JSON serialization,
# body_hash computation) — this module just handles the idempotent
# insert-or-return-existing race and the read paths, same dumb-setter
# posture as the crossings/use-telemetry sections above.

_EVENT_COLS = ("id, event_kind, source_ref, actor, place_id, occurred_at, "
               "recorded_at, body, body_hash, attesting_authority, retention")


def _event_row(r) -> dict[str, Any]:
    # r[0] is rowid — every SELECT below prepends it to _EVENT_COLS so the
    # mint-order cursor (`seq`, matching the crossings `seq` precedent) rides
    # along on every read path, not just list_events's.
    return {
        "seq": r[0],
        "id": r[1], "event_kind": r[2], "source_ref": r[3], "actor": r[4],
        "place_id": r[5], "occurred_at": r[6], "recorded_at": r[7],
        "body": r[8], "body_hash": r[9], "attesting_authority": r[10],
        "retention": r[11],
    }


def mint_event(conn, event_kind: str, source_ref: str, actor: str,
               place_id: str | None, occurred_at: str, body: str | None,
               body_hash: str, attesting_authority: str) -> tuple[str, bool]:
    """Mint an event, idempotent per (event_kind, source_ref) — the caller
    (server.py's /umwelt/record route) has already resolved+snapshotted the
    underlying entry into body/body_hash/actor/etc.; this function only owns
    the insert-or-return-existing race, same idiom as
    get_or_create_avatar's birth race and create_place's collision race.

    Returns (event_id, minted) — minted is False when an existing row's id
    was returned untouched (a re-mint by the same or a different agent, per
    §3.1's idempotency-per-underlying-entry rule). recorded_at is stamped
    here (mint time), never caller-supplied.
    """
    existing = conn.execute(
        "SELECT id FROM events WHERE event_kind = ? AND source_ref = ?",
        (event_kind, source_ref),
    ).fetchone()
    if existing is not None:
        return existing[0], False

    event_id = "evt_" + str(uuid.uuid4())
    recorded_at = now_iso()
    try:
        conn.execute(
            "INSERT INTO events(id, event_kind, source_ref, actor, place_id, "
            "occurred_at, recorded_at, body, body_hash, attesting_authority) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, event_kind, source_ref, actor, place_id,
             occurred_at, recorded_at, body, body_hash, attesting_authority),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Lost the mint race to a concurrent minter of the SAME underlying
        # entry — the UNIQUE(event_kind, source_ref) constraint rejected our
        # insert because someone else's won first between our pre-check
        # above and this write. Not a single-writer assumption: re-query and
        # return theirs rather than erroring (same pattern as
        # get_or_create_avatar/create_place).
        conn.rollback()
        existing = conn.execute(
            "SELECT id FROM events WHERE event_kind = ? AND source_ref = ?",
            (event_kind, source_ref),
        ).fetchone()
        if existing is None:
            raise
        return existing[0], False
    return event_id, True


def get_event(conn, event_id: str) -> dict[str, Any] | None:
    """Fetch a single event by its evt_ id — GET /umwelt/events/<id>."""
    row = conn.execute(
        f"SELECT rowid, {_EVENT_COLS} FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    return _event_row(row) if row else None


# ---------------------------------------------------------------------------
# advance_ref capability (umwelt #186) — capsule ref-chain compare-and-swap.
#
# graft_ref/graft_version are SERVER-OWNED metadata keys (v3 spec delta):
# server.py's create_thing/update_thing reject them outright in caller-
# supplied metadata (same posture as action_log — see those routes), making
# advance_capsule_ref the SOLE mutator of the pair, by construction.
# ---------------------------------------------------------------------------

def advance_capsule_ref(conn, capsule_id: str, agent: str, new_ref: str,
                         expected_ref: str | None) -> tuple[int, str]:
    """Compare-and-swap a capsule thing's graft_ref/graft_version metadata
    pair, atomically with a `graft_advances` provenance row — the underlying
    log every successful advance writes to (docs/CAPABILITY-CHANNEL.md +
    umwelt #186 v2 spec comment).

    Owner gate: `thing.author == agent` — the server-STAMPED author column,
    never a caller-asserted field (v3 delta, re-affirmed after Luria's
    Track-A catch of a caller-asserted `owner` field elsewhere: the engine
    gate never trusts caller metadata for identity).

    Compare-and-swap: `expected_ref` must equal the capsule's CURRENT
    `graft_ref` (None valid ONLY when no graft_ref has ever been set — the
    very first advance, version 0 -> 1). Any mismatch is a lost-update and
    raises ValueError naming BOTH refs, never silently overwritten.

    Raises ValueError for every rejection (not-found, owner mismatch,
    conflict) — cap_handler.CapHandler (the sole caller) turns it into an
    {error} frame; the message text is user-facing.

    On success: the metadata UPDATE and the graft_advances INSERT happen in
    ONE transaction (both statements execute under the connection's default
    implicit transaction before a single commit; any exception during either
    write rolls BOTH back, so a failed swap can never leave a partial
    graft_advances row). Returns (new_version, new_ref).

    Stamps updated_at (world-content-timestamps design, slice 1 follow-up)
    in the same UPDATE, using the same advanced_at timestamp as the
    graft_advances row — this path writes things.metadata directly and
    never goes through update_thing, so without this a capsule advanced
    any number of times would read updated_at = NULL forever. Every
    successful advance is a genuine content change by construction (the
    CAS above already rejects a stale/no-op expected_ref), so no
    value-diffing is needed here.
    """
    thing = get_thing(conn, capsule_id)
    if thing is None:
        raise ValueError(f"capsule {capsule_id!r} not found")
    if thing["author"] != agent:
        raise ValueError(
            "advance_ref denied: caller is not the capsule's author")

    current_ref = thing["metadata"].get("graft_ref")
    current_version = thing["metadata"].get("graft_version") or 0

    if expected_ref is None:
        if current_ref is not None:
            raise ValueError(
                f"advance_ref conflict: expected_ref=null but capsule "
                f"{capsule_id!r} current graft_ref={current_ref!r} — "
                "retrieve current and rebase")
    elif expected_ref != current_ref:
        raise ValueError(
            f"advance_ref conflict: expected_ref={expected_ref!r} but "
            f"capsule {capsule_id!r} current graft_ref={current_ref!r} — "
            "retrieve current and rebase")

    new_version = current_version + 1
    new_metadata = dict(thing["metadata"])
    new_metadata["graft_ref"] = new_ref
    new_metadata["graft_version"] = new_version

    advance_id = "gadv_" + str(uuid.uuid4())
    advanced_at = now_iso()
    try:
        # The UPDATE is itself the compare-and-swap (Borges security round-1,
        # PR #195): the SELECT-check above is advisory UX (nice error text);
        # the WHERE clause re-asserts the expected ref AT WRITE TIME so two
        # concurrent sessions can never both pass the check and silently
        # lose an advance. `IS ?` (not `= ?`) so expected_ref=None matches
        # the key-absent/NULL case. rowcount 0 = the ref moved under us.
        # updated_at is stamped in the same statement (world-content-
        # timestamps follow-up) — this write bypasses update_thing
        # entirely, so it must stamp itself.
        cur = conn.execute(
            "UPDATE things SET metadata = ?, updated_at = ? WHERE id = ? "
            "AND json_extract(metadata, '$.graft_ref') IS ?",
            (json.dumps(new_metadata), advanced_at, capsule_id, expected_ref),
        )
        if cur.rowcount == 0:
            conn.rollback()
            moved = get_thing(conn, capsule_id)
            moved_ref = (moved or {}).get("metadata", {}).get("graft_ref")
            raise ValueError(
                f"advance_ref conflict: expected_ref={expected_ref!r} but "
                f"capsule {capsule_id!r} current graft_ref={moved_ref!r} "
                "(moved concurrently) — retrieve current and rebase")
        conn.execute(
            "INSERT INTO graft_advances(id, capsule_id, agent, old_ref, "
            "new_ref, advanced_at) VALUES(?, ?, ?, ?, ?, ?)",
            (advance_id, capsule_id, agent, current_ref, new_ref, advanced_at),
        )
    except ValueError:
        raise
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return new_version, new_ref


def get_graft_advance(conn, advance_id: str) -> dict[str, Any] | None:
    """Fetch a single graft_advances row by its gadv_ id."""
    row = conn.execute(
        "SELECT id, capsule_id, agent, old_ref, new_ref, advanced_at "
        "FROM graft_advances WHERE id = ?", (advance_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "capsule_id": row[1], "agent": row[2],
        "old_ref": row[3], "new_ref": row[4], "advanced_at": row[5],
    }


def resolve_advance_ref_event(conn, ref: str) -> dict[str, Any] | None:
    """Resolve a graft_advances row id into the snapshot fields
    /umwelt/record's 'advance_ref' kind needs to call mint_event — same
    shape server.py's _resolve_record_snapshot returns for 'crossing'/'use'/
    'act' (source_ref, actor, place_id, occurred_at, body, body_hash,
    attesting_authority). Returns None if no such graft_advances row exists
    (server.py turns that into a 404, same posture as the other kinds).

    `body` is the canonical JSON of the graft_advances row (same
    sort_keys+compact-separator serialization server.py's
    _canonical_body_json uses for 'use'/'act', duplicated here rather than
    imported — db.py has no dependency on server.py). `place_id` is None
    (placeless): a ref-advance has no at-the-time location, same rationale
    as 'use'/'act'. `attesting_authority` is 'world' — the engine's own
    ledger row is the record being attested, never inherited from anything
    caller-supplied. No auto-mint at advance time (the no-auto-mint
    ruling) — this is only invoked when /umwelt/record is called on demand.
    """
    row = get_graft_advance(conn, ref)
    if row is None:
        return None
    payload = {
        "format": 1,
        "id": row["id"],
        "capsule_id": row["capsule_id"],
        "agent": row["agent"],
        "old_ref": row["old_ref"],
        "new_ref": row["new_ref"],
        "advanced_at": row["advanced_at"],
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {
        "source_ref": row["id"],
        "actor": row["agent"],
        "place_id": None,
        "occurred_at": row["advanced_at"],
        "body": body,
        "body_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "attesting_authority": "world",
    }


def get_content_update(conn, update_id: str) -> dict[str, Any] | None:
    """Fetch a single content_updates row by its cupd_ id — same shape/
    posture as get_graft_advance. `changed_cols` is decoded back into a
    list here (stored as a JSON array string) so callers never have to
    re-parse it themselves."""
    row = conn.execute(
        "SELECT id, subject_id, subject_kind, actor, changed_cols, updated_at "
        "FROM content_updates WHERE id = ?", (update_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "subject_id": row[1], "subject_kind": row[2],
        "actor": row[3], "changed_cols": json.loads(row[4]),
        "updated_at": row[5],
    }


def resolve_content_update_event(conn, ref: str) -> dict[str, Any] | None:
    """Resolve a content_updates row id into the snapshot fields
    /umwelt/record's 'update' kind needs to call mint_event — same shape
    server.py's _resolve_record_snapshot returns for the other kinds
    (source_ref, actor, place_id, occurred_at, body, body_hash,
    attesting_authority). Returns None if no such content_updates row
    exists (server.py turns that into a 404, same posture as the other
    kinds).

    `body` is the canonical JSON of the content_updates row (same
    sort_keys+compact-separator serialization used by
    resolve_advance_ref_event, duplicated here rather than imported —
    db.py has no dependency on server.py). `place_id` is None (placeless):
    a content mutation has no at-the-time location, same rationale as
    'use'/'act'/'advance_ref'. `attesting_authority` is 'world' — the
    engine's own ledger row is the record being attested, never inherited
    from anything caller-supplied. No auto-mint at write time — this is
    only invoked when /umwelt/record is called on demand.
    """
    row = get_content_update(conn, ref)
    if row is None:
        return None
    payload = {
        "format": 1,
        "id": row["id"],
        "subject_id": row["subject_id"],
        "subject_kind": row["subject_kind"],
        "actor": row["actor"],
        "changed_cols": row["changed_cols"],
        "updated_at": row["updated_at"],
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {
        "source_ref": row["id"],
        "actor": row["actor"],
        "place_id": None,
        "occurred_at": row["updated_at"],
        "body": body,
        "body_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "attesting_authority": "world",
    }


def list_events(conn, actor: str | None = None, place: str | None = None,
                event_kind: str | None = None, since: int | None = None,
                q: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Filtered, mint-order (`rowid` ASC) list of events — GET /umwelt/events
    behind the Library evidence archive (§5).

    `since` is EXCLUSIVE (rowid > since), matching get_crossings_since's seq
    precedent — a monotonic mint-order cursor, not a wall-clock bound.
    `q` is a plain substring LIKE match over `body` only (v0 — "anything
    smarter lives agent-side" per §5); no metacharacter escaping, same
    posture as this file's other v0-scoped LIKE usage.
    """
    clauses = []
    params: list[Any] = []
    if actor is not None:
        clauses.append("actor = ?")
        params.append(actor)
    if place is not None:
        clauses.append("place_id = ?")
        params.append(place)
    if event_kind is not None:
        clauses.append("event_kind = ?")
        params.append(event_kind)
    if since is not None:
        clauses.append("rowid > ?")
        params.append(since)
    if q:
        clauses.append("body LIKE ?")
        params.append(f"%{q}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT rowid, {_EVENT_COLS} FROM events {where} "
        "ORDER BY rowid ASC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_event_row(r) for r in rows]


# --- projects / tasks (umwelt #161 slice S1) -------------------------------
#
# projects(id, label, description, status, created_by, place_id, created_at,
# updated_at) decomposes into tasks(id, project_id, parent_task_id, ...) via
# parent_task_id (a tree, write-time cycle-guarded the same way place
# topology's parent_id is — see would_create_task_parent_cycle), plus
# cross-cutting task_deps edges (a DAG, write-time cycle-guarded separately
# — see would_create_task_dep_cycle). `blocked` is never a stored column: a
# task is blocked iff it has at least one dep whose status is not
# 'done'/'cancelled' — computed on read by is_task_blocked, so "which tasks
# are blocked" can never drift from the deps table by a stale hand-set flag
# (spec-v3, forum #255 — the whole point of demoting the baton).

_PROJECT_COLS = ("id, label, description, status, created_by, place_id, "
                  "created_at, updated_at")


def _project_row(row) -> dict[str, Any]:
    return {
        "id": row[0], "label": row[1], "description": row[2],
        "status": row[3], "created_by": row[4], "place_id": row[5],
        "created_at": row[6], "updated_at": row[7],
    }


def create_project(conn, id: str, label: str, description: str,
                   created_by: str, created_at: str, updated_at: str) -> None:
    conn.execute(
        "INSERT INTO projects(id, label, description, status, created_by, "
        "place_id, created_at, updated_at) "
        "VALUES(?, ?, ?, 'active', ?, NULL, ?, ?)",
        (id, label, description, created_by, created_at, updated_at),
    )
    conn.commit()


def create_project_with_board(conn, project_id: str, label: str,
                               description: str, created_by: str,
                               created_at: str, updated_at: str,
                               place_id: str, place_label: str,
                               place_description: str,
                               board_thing_id: str, board_kind_id: str,
                               board_name: str, board_description: str
                               ) -> None:
    """Atomically create a project row, its Workshop sub-place, and a
    bound `project_board` instance in that place — umwelt #221 S3a §2,
    Rider 1 (Kepler #222 approval) — S3a's load-bearing correctness
    requirement.

    All three INSERTs (projects, places, things) run in ONE transaction:
    raw conn.execute calls, no intermediate commit, a single conn.commit()
    at the very end — same one-transaction posture as
    advance_capsule_ref's metadata-UPDATE + graft_advances-INSERT pair.
    Any exception during ANY of the three writes rolls ALL of them back
    (conn.rollback(), then re-raise), so a forge failure partway through
    can never leave a half-born project: neither a project row whose
    place_id points at a sub-place with no bound board, nor an orphan
    sub-place with no project row ever pointing at it. That half-born
    state is exactly the "unbound board" hazard §2 exists to close
    by construction — a partial write would silently re-introduce it.

    The board thing's metadata is `{"kind_id": board_kind_id,
    "project_id": project_id}` — `project_id` is set HERE, server-side,
    from the very id this same call is minting the project row under.
    There is no caller-facing path that could substitute a different
    project_id onto the board (§6.1's board→project binding, unspoofable
    at forge) — this function is the ONLY place a board's project_id is
    ever written.

    Caller (server.py's create_project route) generates
    project_id/place_id/board_thing_id and ensures board_kind_id names a
    live `project_board` kinddef (see _ensure_project_board_kinddef)
    BEFORE calling this — this function trusts its inputs rather than
    re-validating them, same division of labor as create_thing/
    create_place's own routes validating before calling into db.py.

    umwelt #1051: a FOURTH write joins the same transaction -- the founder
    becomes the project's first owner (project_owners row, canonical agent,
    empty role). It is the last write inside the try, so a failure in any of
    the four rolls back all four.

    Raises sqlite3.IntegrityError (uncaught, after rollback) on a
    genuine id collision (place_id or board_thing_id already taken) — a
    true interleaved race, structurally the same failure class
    create_place's own IntegrityError catch guards against, left to the
    caller here since project_id/place_id/board_thing_id are all freshly
    uuid4-derived (not a realistic operational concern in practice).
    """
    board_metadata = {"kind_id": board_kind_id, "project_id": project_id}
    try:
        # Places FIRST — projects.place_id and things.place_id both carry a
        # FOREIGN KEY REFERENCES places(id) (enforced whenever the
        # connection has PRAGMA foreign_keys=ON, as every request
        # connection does — see create_app's before_request hook), so the
        # sub-place must exist before either row that points into it is
        # inserted, or the FK check fails on the very first INSERT.
        conn.execute(
            "INSERT INTO places(id, kind, owner, label, description, "
            "parent_id) VALUES(?, 'public', NULL, ?, ?, ?)",
            (place_id, place_label, place_description,
             role_place_id(conn, "workshop")),
        )
        conn.execute(
            "INSERT INTO projects(id, label, description, status, "
            "created_by, place_id, created_at, updated_at) "
            "VALUES(?, ?, ?, 'active', ?, ?, ?, ?)",
            (project_id, label, description, created_by, place_id,
             created_at, updated_at),
        )
        conn.execute(
            "INSERT INTO things(id, place_id, name, description, kind, "
            "exec_command, metadata, author, created_at, weight_est) "
            "VALUES(?, ?, ?, ?, 'readable', NULL, ?, ?, ?, ?)",
            (board_thing_id, place_id, board_name, board_description,
             json.dumps(board_metadata), created_by, created_at,
             compute_weight_est(board_name, board_description)),
        )
        # umwelt #1051: the founder is the project's first owner, written in
        # THIS transaction (a failure anywhere above or here rolls back the
        # project, its room, its board and this row together). Canonical
        # identity, same fold every other agent key uses.
        conn.execute(
            "INSERT INTO project_owners(project_id, agent, role, added_by, "
            "added_at) VALUES(?, ?, '', ?, ?)",
            (project_id, canonical_agent(created_by),
             canonical_agent(created_by), created_at),
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def get_project(conn, project_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_PROJECT_COLS} FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return _project_row(row) if row else None


def project_exists(conn, project_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM projects WHERE id = ?", (project_id,)
    ).fetchone() is not None


def list_projects(conn, status: str | None = None) -> list[dict[str, Any]]:
    """All projects, optionally filtered by status, ordered by created_at
    (oldest first — matches things_in_place's ordering convention)."""
    if status is not None:
        rows = conn.execute(
            f"SELECT {_PROJECT_COLS} FROM projects WHERE status = ? "
            "ORDER BY created_at ASC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_PROJECT_COLS} FROM projects ORDER BY created_at ASC"
        ).fetchall()
    return [_project_row(r) for r in rows]


# Columns a caller may change via update_project. id/created_by/created_at/
# place_id are immutable through this route in S1 (place_id is wired by #161
# S3's board-thing slice, not here).
_MUTABLE_PROJECT_COLS = ("label", "description", "status")


def update_project(conn, project_id: str, patch: dict, updated_at: str) -> None:
    """Set the given mutable columns on a project. Only keys in
    _MUTABLE_PROJECT_COLS are applied — the column list is a fixed constant
    (never caller-supplied), so the SQL is injection-safe; values are
    parameterized. Caller (server.py) validates the patch shape + the
    status enum. No-op (but still stamps updated_at) if nothing mutable is
    set — matches the route's own "patch must be non-empty" 400 gate, so
    this defensive no-op should never actually fire in practice.
    """
    cols = [c for c in _MUTABLE_PROJECT_COLS if c in patch]
    if not cols:
        return
    sets = ", ".join(f"{c} = ?" for c in cols) + ", updated_at = ?"
    vals = [patch[c] for c in cols] + [updated_at, project_id]
    conn.execute(f"UPDATE projects SET {sets} WHERE id = ?", vals)
    conn.commit()


# --- project owners (umwelt #1051) ------------------------------------------
#
# project_owners(project_id, agent, role, added_by, added_at). Every function
# here takes a CANONICAL agent (callers fold case first; create_project_with_
# board folds the founder itself). Policy -- who may change owners, the
# keep-one-owner rule, the role cap -- lives in the effects (server.py); this
# layer only writes and reads.

# Display/sort order for "the projects X owns": live work first.
_PROJECT_STATUS_RANK = {"active": 0, "paused": 1, "done": 2, "archived": 3}


def list_project_owners(conn, project_id: str) -> list[dict[str, str]]:
    """The owners of one project, [{agent, role}], oldest-added first."""
    rows = conn.execute(
        "SELECT agent, role FROM project_owners WHERE project_id = ? "
        "ORDER BY added_at ASC, agent ASC", (project_id,)).fetchall()
    return [{"agent": r[0], "role": r[1]} for r in rows]


def project_owners_map(conn) -> dict[str, list[dict[str, str]]]:
    """Every project's owners in one query: {project_id: [{agent, role}]}."""
    out: dict[str, list[dict[str, str]]] = {}
    for pid, agent, role in conn.execute(
            "SELECT project_id, agent, role FROM project_owners "
            "ORDER BY added_at ASC, agent ASC"):
        out.setdefault(pid, []).append({"agent": agent, "role": role})
    return out


def open_task_counts_by_project(conn) -> dict[str, int]:
    """{project_id: n} in ONE grouped query, n = the project's tasks (subtasks
    included) whose status is not 'done' or 'cancelled'. A project with none
    is absent (callers `.get(pid, 0)`). Read-only; the Project Register's
    open-task count."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT project_id, COUNT(*) FROM tasks "
        "WHERE status NOT IN ('done','cancelled') GROUP BY project_id")}


def project_board_ids(conn, kind_id: str) -> dict[str, str]:
    """{project_id: board thing id} in ONE query: live things whose
    metadata.kind_id is `kind_id` (the project_board kind), keyed by their
    metadata.project_id (the binding create_project_with_board writes). If
    a project somehow has several, the smallest id wins (deterministic)."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT json_extract(metadata, '$.project_id') AS pid, MIN(id) "
        "FROM things WHERE status='live' "
        "AND json_extract(metadata, '$.kind_id') = ? "
        "AND json_extract(metadata, '$.project_id') IS NOT NULL "
        "GROUP BY pid", (kind_id,))}


# The authorization for adding an owner is PART OF THE WRITE (umwelt #1051,
# the bootstrap race found at review): one INSERT ... SELECT ... WHERE
# statement. SQLite runs a statement under the database write lock, so the
# EXISTS sub-selects below read the owner rows as they are AT THE MOMENT OF
# THE WRITE -- a second request cannot slip between a check and an insert,
# because there is no gap between them. Three arms, any one authorizes:
#   * the actor is a project owner admin of this world
#     (_ids.project_owner_admins(), env UMWELT_PROJECT_OWNER_ADMINS);
#   * the actor is already an owner of the project;
#   * the project has NO owner rows and the actor is its founder (created_by,
#     passed in canonical form -- that column never changes, so reading it
#     outside the statement is race-free).
# Zero rows written (nothing returned) means "refused"; the same statement's
# ON CONFLICT branch is the role update, behind the SAME guard (a refused
# actor selects no row, so neither the insert nor the update happens).
# RETURNING says which happened: the row's added_at equals this call's own
# timestamp only for a fresh insert (an update leaves the original added_at).
_ADD_OWNER_SQL = """
INSERT INTO project_owners(project_id, agent, role, added_by, added_at)
SELECT :pid, :agent, :role, :actor, :at
WHERE :is_admin = 1
   OR EXISTS (SELECT 1 FROM project_owners
              WHERE project_id = :pid AND agent = :actor)
   OR (:actor != '' AND :actor = :founder
       AND NOT EXISTS (SELECT 1 FROM project_owners WHERE project_id = :pid))
ON CONFLICT(project_id, agent) DO UPDATE SET role = {role_expr}
RETURNING added_at = :at
"""


def add_project_owner(conn, project_id: str, agent: str, role: str | None,
                      actor: str, added_at: str, *, is_admin: bool,
                      founder: str, commit: bool = True) -> bool | None:
    """Add `agent` as an owner of the project, or update an existing owner's
    role, IF `actor` is authorized -- decided inside the one write statement
    (see _ADD_OWNER_SQL). Returns True for a new owner row, False for an
    update of an existing owner (role set, or unchanged when `role` is
    None), None when the actor was not authorized (nothing written).

    One row per (project_id, agent): a re-add keeps the ORIGINAL added_by/
    added_at. `role=None` means "no role given": a new owner gets '' and an
    existing owner keeps theirs. `founder` is the project's created_by,
    canonical. `commit=False` leaves the transaction open (tests use it to
    show the write lock)."""
    sql = _ADD_OWNER_SQL.format(
        role_expr="project_owners.role" if role is None else "excluded.role")
    cur = conn.execute(sql, {
        "pid": project_id, "agent": agent, "role": role or "",
        "actor": actor, "at": added_at, "is_admin": 1 if is_admin else 0,
        "founder": founder})
    rows = cur.fetchall()
    if commit:
        conn.commit()
    if not rows:
        return None
    return bool(rows[0][0])


def remove_project_owner(conn, project_id: str, agent: str, keep_one: bool,
                         *, actor: str, is_admin: bool) -> bool:
    """Remove `agent` from the project's owners. True iff a row was deleted.

    Both guards are part of the DELETE statement itself, so check and write
    are one atomic step under the write lock (no window between a read and
    the delete): the ACTOR must be an admin or (still) an owner, and with
    `keep_one` ANOTHER owner must remain. False means the agent was not an
    owner, or the actor was not authorized, or (keep_one) it was the last
    owner; the caller tells them apart with a read taken afterwards."""
    cur = conn.execute(
        "DELETE FROM project_owners WHERE project_id = :pid AND agent = :agent "
        "AND (:is_admin = 1 OR EXISTS (SELECT 1 FROM project_owners "
        "WHERE project_id = :pid AND agent = :actor)) "
        "AND (:keep = 0 OR EXISTS (SELECT 1 FROM project_owners "
        "WHERE project_id = :pid AND agent != :agent))",
        {"pid": project_id, "agent": agent, "actor": actor,
         "is_admin": 1 if is_admin else 0, "keep": 1 if keep_one else 0})
    conn.commit()
    return cur.rowcount > 0


# --- project archive / restore (umwelt #1074 slice B, #1094) -----------------
#
# Archive: status -> 'archived' AND the project's room reparented under the
# archive place. Restore: status -> 'active' (NOT the prior status: the
# design authority's ruling) AND the room reparented under its PRE-ARCHIVE
# parent when that parent still exists, otherwise under the workshop with the
# fallback named in the result (umwelt #1094). The pre-archive parent is
# recorded without a schema change, in the room's own `places.metadata`
# object under `archived_from_parent` (merged in by archive, consumed by
# restore). Both halves are ONE BEGIN IMMEDIATE transaction
# (atomic_authorized_write), and every guard -- who may act, the state of the
# project, the places involved, the cycle check, the restore target -- is
# decided INSIDE it: a pre-check taken earlier only gives a readable reason,
# THIS is the authority, so two racing requests cannot both pass. The
# restore target is decided by ONE helper (`restore_target`) that both the
# blocker and the write call, so the pre-check and the authority cannot
# disagree. No thing row is touched: the room's things and child rooms
# reference the room, so they travel with it (Village Law D2).

ARCHIVED_PLACE_ID = "place_archived"
ARCHIVED_FROM_PARENT_KEY = "archived_from_parent"

_RESTORE_FALLBACK_TEXT = {
    "no_record": "no pre-archive parent was recorded; "
                 "the room went to the Workshop",
    "parent_missing": "the recorded pre-archive parent no longer exists; "
                      "the room went to the Workshop",
    "parent_is_room": "the recorded pre-archive parent is the room itself; "
                      "the room went to the Workshop",
    "parent_is_archive": "the recorded pre-archive parent is the archive "
                         "place; the room went to the Workshop",
    "parent_cycle": "returning the room to its recorded pre-archive parent "
                    "would create a cycle (that parent is now inside the "
                    "room); the room went to the Workshop",
    "record_malformed": "the recorded pre-archive parent is unreadable; "
                        "the room went to the Workshop",
}


def restore_fallback_text(reason: str | None) -> str | None:
    """The one plain sentence saying why restore used the Workshop, or None
    when `reason` is None (a clean return to the recorded parent)."""
    if reason is None:
        return None
    return _RESTORE_FALLBACK_TEXT[reason]


def _metadata_object(raw: Any) -> dict[str, Any] | None:
    """The decoded `places.metadata` when it is a JSON object, else None
    (invalid JSON, or valid JSON that is not an object)."""
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def restore_target(conn, project_id: str, workshop_place_id: str,
                   archived_place_id: str = ARCHIVED_PLACE_ID
                   ) -> tuple[str, str | None]:
    """Where restoring `project_id` puts its room: `(target_id,
    fallback_reason)`. Pure read, and the ONLY place the choice is made --
    the blocker and the write both call it.

    The recorded parent (the room's `metadata.archived_from_parent`) is used
    iff the key is present and a non-empty string, that place exists, is not
    the room itself, is not `archived_place_id`, and moving the room under it
    would not create a cycle. Otherwise the target is `workshop_place_id` and
    the reason is one of: no_record, parent_missing, parent_is_room,
    parent_is_archive, parent_cycle, record_malformed (the key is not a
    string, or the metadata is not a JSON object). `fallback_reason` is None
    when the recorded parent is used."""
    project = get_project(conn, project_id)
    room = project["place_id"] if project is not None else None
    row = (conn.execute("SELECT metadata FROM places WHERE id = ?",
                        (room,)).fetchone() if room is not None else None)
    if row is None:
        return workshop_place_id, "no_record"
    meta = _metadata_object(row[0])
    if meta is None:
        return workshop_place_id, "record_malformed"
    if ARCHIVED_FROM_PARENT_KEY not in meta:
        return workshop_place_id, "no_record"
    parent = meta[ARCHIVED_FROM_PARENT_KEY]
    if not isinstance(parent, str) or not parent:
        return workshop_place_id, "record_malformed"
    if not place_exists(conn, parent):
        return workshop_place_id, "parent_missing"
    if parent == room:
        return workshop_place_id, "parent_is_room"
    if parent == archived_place_id:
        return workshop_place_id, "parent_is_archive"
    if would_create_cycle(conn, room, parent):
        return workshop_place_id, "parent_cycle"
    return parent, None


def project_move_blocker(conn, project_id: str, actor: str, *, is_admin: bool,
                         restore: bool, target_place_id: str,
                         archived_place_id: str = ARCHIVED_PLACE_ID
                         ) -> str | None:
    """Why `actor` may NOT archive (restore=False) or restore (restore=True)
    the project right now, as a short code -- or None when the move is
    allowed. Pure reads. Checked in this order, first hit wins:

      project_missing, not_allowed (not an admin and no owner row),
      already_archived (archive) / not_archived + no_owner (restore),
      no_room, room_missing, target_missing, room_is_target, cycle.

    For a restore, `target_place_id` is the WORKSHOP and the last three
    codes are judged against the EFFECTIVE target (`restore_target`: the
    recorded pre-archive parent when usable, else the workshop), so an
    unusable record falls back to the workshop instead of refusing, while a
    missing or cyclic workshop still refuses when it is the effective target.

    The write functions below run this as the guard INSIDE their
    transaction; callers also run it beforehand to compose a readable
    refusal (and again afterwards, to say why a write was refused)."""
    project = get_project(conn, project_id)
    if project is None:
        return "project_missing"
    if not is_admin:
        owner = conn.execute(
            "SELECT 1 FROM project_owners WHERE project_id = ? AND agent = ?",
            (project_id, actor)).fetchone()
        if owner is None:
            return "not_allowed"
    if restore:
        if project["status"] != "archived":
            return "not_archived"
        has_owner = conn.execute(
            "SELECT 1 FROM project_owners WHERE project_id = ?",
            (project_id,)).fetchone()
        if has_owner is None:
            return "no_owner"
    elif project["status"] == "archived":
        return "already_archived"
    room = project["place_id"]
    if room is None:
        return "no_room"
    if not place_exists(conn, room):
        return "room_missing"
    if restore:
        target_place_id, _why = restore_target(
            conn, project_id, target_place_id, archived_place_id)
    if not place_exists(conn, target_place_id):
        return "target_missing"
    if room == target_place_id:
        return "room_is_target"
    if would_create_cycle(conn, room, target_place_id):
        return "cycle"
    return None


def _move_project(conn, project_id: str, actor: str, *, is_admin: bool,
                  restore: bool, target_place_id: str, status: str,
                  at: str, archived_place_id: str = ARCHIVED_PLACE_ID
                  ) -> dict[str, Any] | None:
    result: dict[str, Any] = {}

    def _allowed(c) -> bool:
        return project_move_blocker(
            c, project_id, actor, is_admin=is_admin, restore=restore,
            target_place_id=target_place_id,
            archived_place_id=archived_place_id) is None

    def _write(c) -> None:
        room = get_project(c, project_id)["place_id"]
        row = c.execute("SELECT parent_id, metadata FROM places WHERE id = ?",
                        (room,)).fetchone()
        old_parent, raw_meta = row[0], row[1]
        meta = _metadata_object(raw_meta)
        new_meta: dict[str, Any] | None = None   # None = leave metadata alone
        extra: dict[str, Any]
        if restore:
            target, fallback = restore_target(
                c, project_id, target_place_id, archived_place_id)
            if meta is not None and ARCHIVED_FROM_PARENT_KEY in meta:
                new_meta = {k: v for k, v in meta.items()
                            if k != ARCHIVED_FROM_PARENT_KEY}
            extra = {"restored_to": target, "fallback": fallback,
                     "fallback_text": restore_fallback_text(fallback)}
        else:
            target = target_place_id
            recorded = None
            if (old_parent and old_parent != target_place_id
                    and meta is not None):
                recorded = old_parent
                new_meta = {**meta, ARCHIVED_FROM_PARENT_KEY: recorded}
            extra = {"archived_from": recorded}
        c.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
                  (status, at, project_id))
        if new_meta is None:
            c.execute("UPDATE places SET parent_id = ? WHERE id = ?",
                      (target, room))
        else:
            c.execute("UPDATE places SET parent_id = ?, metadata = ? "
                      "WHERE id = ?", (target, json.dumps(new_meta), room))
        result.update({
            "project": project_id, "status": status, "room": room,
            "moved_to": target,
            "things_in_room": len(things_in_place(c, room)),
            "child_rooms": [p["id"] for p in get_place_children(c, room)],
            **extra,
        })

    if not atomic_authorized_write(conn, _allowed, _write):
        return None
    return result


def archive_project(conn, project_id: str, actor: str, *, is_admin: bool,
                    archived_place_id: str, at: str) -> dict[str, Any] | None:
    """Archive the project in ONE transaction: `status = 'archived'` and its
    room's parent set to `archived_place_id` (one hop; nothing is created --
    the place must already exist). `actor` is canonical; `is_admin` is the
    caller's verdict on the actor's admin standing (a configured project admin); otherwise
    the actor must hold a project_owners row. Allowed from any status except
    archived.

    In the same write the room's current parent is recorded in the room's own
    `places.metadata` under `archived_from_parent` (merged into the existing
    object; every other key is kept), so restore can put the room back
    (umwelt #1094). It is a raw UPDATE like the parent move -- no `updated_at`
    stamp, no content_updates row. NOTHING is recorded (and the archive still
    succeeds) when the room has no parent, when its parent already IS the
    archive place, or when its metadata is not a JSON object (never
    overwritten).

    Returns {project, status, room, moved_to, things_in_room, child_rooms,
    archived_from} (things_in_room = live things directly in the room,
    counted in the same transaction; archived_from = the parent recorded, or
    None when nothing was), or None when refused -- nothing was written;
    `project_move_blocker` read afterwards says why."""
    return _move_project(conn, project_id, actor, is_admin=is_admin,
                         restore=False, target_place_id=archived_place_id,
                         status="archived", at=at,
                         archived_place_id=archived_place_id)


def restore_project(conn, project_id: str, actor: str, *, is_admin: bool,
                    workshop_place_id: str, at: str,
                    archived_place_id: str = ARCHIVED_PLACE_ID
                    ) -> dict[str, Any] | None:
    """The reverse of archive_project, ONE transaction: `status = 'active'`
    (always -- not the status the project had before it was archived) and the
    room's parent set to the pre-archive parent recorded by archive_project
    when that parent still exists and the move is sound (`restore_target`),
    otherwise to `workshop_place_id`. The record is consumed in the same
    write. Allowed ONLY from archived, and only while the project has at
    least one owner row (an archived project may have none; the owner check
    runs inside the write, so an owner removed after a caller's pre-check
    makes the write refuse). A refused restore leaves the record in place.

    Returns {project, status, room, moved_to, things_in_room, child_rooms,
    restored_to, fallback, fallback_text}: moved_to == restored_to is the
    ACTUAL new parent; `fallback` is None on a clean return, else the reason
    code why the workshop was used, and `fallback_text` is that reason in one
    plain sentence. None when refused."""
    return _move_project(conn, project_id, actor, is_admin=is_admin,
                         restore=True, target_place_id=workshop_place_id,
                         status="active", at=at,
                         archived_place_id=archived_place_id)


def projects_owned_by(conn, agent: str) -> list[dict[str, Any]]:
    """Every project `agent` (canonical) owns, in the stable display order:
    active, paused, done, archived; then by label (case-insensitive, then
    exact); then oldest first and id as the final tie-breakers, so equal
    rows come out the same way on every read."""
    rows = conn.execute(
        f"SELECT {', '.join('p.' + c.strip() for c in _PROJECT_COLS.split(','))} "
        "FROM projects p JOIN project_owners o ON o.project_id = p.id "
        "WHERE o.agent = ?", (agent,)).fetchall()
    projects = [_project_row(r) for r in rows]
    projects.sort(key=lambda p: (
        _PROJECT_STATUS_RANK.get(p["status"], len(_PROJECT_STATUS_RANK)),
        p["label"].casefold(), p["label"], p["created_at"], p["id"]))
    return projects


_TASK_COLS = ("id, project_id, parent_task_id, label, description, status, "
              "owner, turn_holder, gh_repo, gh_ref, created_by, created_at, "
              "updated_at")


def _task_row(row) -> dict[str, Any]:
    return {
        "id": row[0], "project_id": row[1], "parent_task_id": row[2],
        "label": row[3], "description": row[4], "status": row[5],
        "owner": row[6], "turn_holder": row[7], "gh_repo": row[8],
        "gh_ref": row[9], "created_by": row[10], "created_at": row[11],
        "updated_at": row[12],
    }


def create_task(conn, id: str, project_id: str, label: str, description: str,
                created_by: str, created_at: str, updated_at: str,
                parent_task_id: str | None = None, owner: str | None = None,
                gh_repo: str | None = None, gh_ref: str | None = None) -> None:
    conn.execute(
        "INSERT INTO tasks(id, project_id, parent_task_id, label, "
        "description, status, owner, turn_holder, gh_repo, gh_ref, "
        "created_by, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, 'open', ?, NULL, ?, ?, ?, ?, ?)",
        (id, project_id, parent_task_id, label, description, owner,
         gh_repo, gh_ref, created_by, created_at, updated_at),
    )
    conn.commit()


def get_task(conn, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_TASK_COLS} FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return _task_row(row) if row else None


def task_exists(conn, task_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
    ).fetchone() is not None


def list_tasks_for_project(conn, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_TASK_COLS} FROM tasks WHERE project_id = ? "
        "ORDER BY created_at ASC",
        (project_id,),
    ).fetchall()
    return [_task_row(r) for r in rows]


# Columns a caller may change via update_task. id/project_id/parent_task_id/
# created_by/created_at are immutable through this route in S1 — re-parenting
# a task isn't part of this slice's spec (only parent_task_id AT CREATE TIME
# is validated/set; changing it later is not exposed here).
_MUTABLE_TASK_COLS = ("label", "description", "status", "owner",
                      "turn_holder", "gh_repo", "gh_ref")


def update_task(conn, task_id: str, patch: dict, updated_at: str) -> None:
    """Set the given mutable columns on a task. Only keys in
    _MUTABLE_TASK_COLS are applied — same fixed-constant-column-list
    injection-safety posture as update_project/update_thing. Caller
    (server.py) validates the patch shape + the status enum. No-op (but
    still stamps updated_at) if nothing mutable is set.
    """
    cols = [c for c in _MUTABLE_TASK_COLS if c in patch]
    if not cols:
        return
    sets = ", ".join(f"{c} = ?" for c in cols) + ", updated_at = ?"
    vals = [patch[c] for c in cols] + [updated_at, task_id]
    conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
    conn.commit()


_MAX_TASK_ANCESTOR_WALK = 200  # defensive cap on _task_ancestor_ids' upward
# walk — same belt-and-suspenders posture as place_tree's
# _MAX_ANCESTOR_WALK; the real guard against a cycle existing at all is
# would_create_task_parent_cycle, called by the create-task route BEFORE a
# cycle can be created (parent_task_id is only ever set at task-creation
# time in this slice), so this cap should never actually bind in practice.


def _task_ancestor_ids(conn, task_id: str) -> list[str]:
    """Walk parent_task_id pointers upward from task_id, returning the chain
    of ancestor ids (nearest-first, task_id itself excluded). Same shape as
    place.py's _place_ancestor_ids — see that function's docstring for the
    defensive-stop rationale (revisited id / hop cap)."""
    ancestors: list[str] = []
    seen = {task_id}
    current = task_id
    for _ in range(_MAX_TASK_ANCESTOR_WALK):
        row = conn.execute(
            "SELECT parent_task_id FROM tasks WHERE id = ?", (current,)
        ).fetchone()
        if row is None or row[0] is None:
            break
        parent_id = row[0]
        if parent_id in seen:
            break  # defensive: pre-existing corrupt cycle, stop, don't loop
        ancestors.append(parent_id)
        seen.add(parent_id)
        current = parent_id
    return ancestors


def would_create_task_parent_cycle(conn, task_id: str, new_parent_id: str) -> bool:
    """True if setting task_id's parent_task_id to new_parent_id would make
    task_id its own ancestor, directly or transitively (including the
    depth-0 self-parent case). Mirrors would_create_cycle's place-topology
    twin exactly — a SEPARATE guard from would_create_task_dep_cycle below:
    parent-chain cycles and task_deps cycles are two independent relations
    (per the S1 spec's explicit note), so this function only ever walks
    parent_task_id pointers, never task_deps edges.

    umwelt #166 investigation note: this predicate is NOT currently called
    from any route/write path — server.py's create_task route sets
    parent_task_id only at task-CREATION time, on a freshly-minted task_id
    that (by construction) cannot already appear in any ancestor chain, so
    no cycle is reachable there and no check is wired in; there is no
    re-parent-an-existing-task route in this slice (see _MUTABLE_TASK_COLS
    above — parent_task_id is immutable through update_task), so there is
    no check-then-write call site to race in the first place. Left as a
    plain predicate (exercised directly by tests only) rather than folded
    into _atomic_guarded_write for that reason. If a future re-parent route
    is ever added, it MUST use _atomic_guarded_write around this predicate
    + the UPDATE, same pattern as set_place_parent/add_task_dep — a
    re-parent of an EXISTING task is exactly the check-then-write shape
    #166 closed for the other two guards."""
    if new_parent_id == task_id:
        return True
    return task_id in _task_ancestor_ids(conn, new_parent_id)


def get_task_deps(conn, task_id: str) -> list[str]:
    """The ids of tasks that task_id depends on (direct edges only)."""
    rows = conn.execute(
        "SELECT depends_on_task_id FROM task_deps WHERE task_id = ? "
        "ORDER BY depends_on_task_id",
        (task_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _dep_reachable(conn, from_task_id: str, to_task_id: str) -> bool:
    """True if to_task_id is reachable from from_task_id by walking
    task_deps edges forward (from_task_id depends_on ... depends_on
    to_task_id, directly or transitively). BFS over the depends_on_task_id
    edges only — task_deps is its own DAG, independent of parent_task_id
    (see would_create_task_parent_cycle's docstring)."""
    seen = {from_task_id}
    frontier = [from_task_id]
    for _ in range(_MAX_TASK_ANCESTOR_WALK):
        if not frontier:
            break
        next_frontier: list[str] = []
        for tid in frontier:
            for dep_id in get_task_deps(conn, tid):
                if dep_id == to_task_id:
                    return True
                if dep_id not in seen:
                    seen.add(dep_id)
                    next_frontier.append(dep_id)
        frontier = next_frontier
    return False


def would_create_task_dep_cycle(conn, task_id: str, depends_on_task_id: str) -> bool:
    """True if adding a (task_id depends_on depends_on_task_id) edge would
    create a cycle over task_deps' own edge set — direct
    (depends_on_task_id == task_id) or transitive (depends_on_task_id
    already (transitively) depends on task_id). This is a SEPARATE guard
    from would_create_task_parent_cycle: task_deps cycles and parent-chain
    cycles are independent relations (S1 spec's explicit note) — this
    function never looks at parent_task_id."""
    if depends_on_task_id == task_id:
        return True
    return _dep_reachable(conn, depends_on_task_id, task_id)


def add_task_dep(conn, task_id: str, depends_on_task_id: str) -> None:
    """Add a (task_id depends_on depends_on_task_id) edge — idempotent
    re-add via INSERT OR IGNORE (same idempotent-write posture as
    add_place_link), matching the task_deps PRIMARY KEY (task_id,
    depends_on_task_id).

    Cycle guard (umwelt #166): would_create_task_dep_cycle and the INSERT
    run as ONE atomic unit under _atomic_guarded_write — closes the TOCTOU
    race where two concurrent add_task_dep calls could each pass the check
    before either wrote (see the #166 module comment above
    would_create_cycle, place.py's twin of this guard). Raises CycleError
    (a ValueError subclass) on a cycle; the caller (server.py's
    /umwelt/tasks/<id>/deps route) no longer pre-checks
    would_create_task_dep_cycle itself — it catches CycleError and maps it
    to 409 with the same message text as before."""

    def _would_cycle(c) -> bool:
        return would_create_task_dep_cycle(c, task_id, depends_on_task_id)

    def _write(c) -> None:
        c.execute(
            "INSERT OR IGNORE INTO task_deps(task_id, depends_on_task_id) "
            "VALUES(?, ?)",
            (task_id, depends_on_task_id),
        )

    _atomic_guarded_write(
        conn, _would_cycle, _write,
        f"adding a dependency from {task_id!r} on "
        f"{depends_on_task_id!r} would create a cycle",
    )


def remove_task_dep(conn, task_id: str, depends_on_task_id: str) -> None:
    """Remove a (task_id depends_on depends_on_task_id) edge. A silent
    no-op (no error) if the row doesn't exist — matches
    remove_place_link's idempotent posture."""
    conn.execute(
        "DELETE FROM task_deps WHERE task_id = ? AND depends_on_task_id = ?",
        (task_id, depends_on_task_id),
    )
    conn.commit()


def is_task_blocked(conn, task_id: str) -> bool:
    """A task is blocked iff it has at least one dep whose status is not
    'done' or 'cancelled' (spec-v3 §1, forum #255 — computed, never
    hand-set; 'blocked' is deliberately not a stored status value at all —
    see the projects/tasks table comment above). A task with no deps is
    never blocked."""
    row = conn.execute(
        "SELECT COUNT(*) FROM task_deps td "
        "JOIN tasks t ON t.id = td.depends_on_task_id "
        "WHERE td.task_id = ? AND t.status NOT IN ('done', 'cancelled')",
        (task_id,),
    ).fetchone()
    return bool(row and row[0] > 0)


def get_blocking_dep_ids(conn, task_id: str) -> list[str]:
    """The ids of task_id's deps (depends_on_task_id) whose status is not
    yet 'done' or 'cancelled' — the same set is_task_blocked checks,
    exposed as concrete ids for callers building actionable error messages
    (spec-v4 §done-completeness gate, server.py's update_task route)."""
    rows = conn.execute(
        "SELECT td.depends_on_task_id FROM task_deps td "
        "JOIN tasks t ON t.id = td.depends_on_task_id "
        "WHERE td.task_id = ? AND t.status NOT IN ('done', 'cancelled') "
        "ORDER BY td.depends_on_task_id",
        (task_id,),
    ).fetchall()
    return [r[0] for r in rows]


def get_open_subtask_ids(conn, task_id: str) -> list[str]:
    """The ids of task_id's direct sub-tasks (parent_task_id = task_id)
    whose status is not yet 'done' or 'cancelled' — used by the done-status
    completeness gate (spec-v4): a task cannot go done while a sub-task
    remains open. Direct children only, matching parent_task_id's one-level
    tree edge (no recursive descent — see parent_task_id docs above)."""
    rows = conn.execute(
        "SELECT id FROM tasks WHERE parent_task_id = ? "
        "AND status NOT IN ('done', 'cancelled') ORDER BY id",
        (task_id,),
    ).fetchall()
    return [r[0] for r in rows]


def list_tasks_for_holder(conn, agent: str) -> list[dict[str, Any]]:
    """The my-court feed for `agent` (umwelt #445 slice 1 — the
    per-agent turn-state readout #161's S4 named but never built): every
    task currently in `agent`'s court, oldest-in-court first.

    ACTIONABLE-NOW semantics (the #161-ratified split: the my-court feed
    excludes blocked tasks; the project tree is the full-truth surface):
    a task counts iff turn_holder == agent (already-canonical, caller's
    job — same posture as every other agent-keyed lookup in this module),
    its own status is not done/cancelled, its parent project's status is
    not archived (paused still counts), AND it is not dep-blocked
    (is_task_blocked, the computed-not-stored mechanism project_tree
    uses). A dep completing elsewhere therefore makes a task APPEAR in
    this feed — which is exactly the actionable-now announcement the
    court hook renders. Each row is the standard task dict (_task_row)
    plus `project_label`/`project_status` (joined from projects); no
    `blocked` field — every row here is unblocked by construction.
    """
    rows = conn.execute(
        f"SELECT {', '.join('t.' + c.strip() for c in _TASK_COLS.split(','))}, "
        "p.label, p.status FROM tasks t "
        "JOIN projects p ON t.project_id = p.id "
        "WHERE t.turn_holder = ? "
        "AND t.status NOT IN ('done', 'cancelled') "
        "AND p.status != 'archived' "
        "ORDER BY t.updated_at ASC",
        (agent,),
    ).fetchall()
    out = []
    for r in rows:
        task = _task_row(r[:13])
        if is_task_blocked(conn, task["id"]):
            continue  # feed = actionable-now; blocked rows live in the tree
        task["project_label"] = r[13]
        task["project_status"] = r[14]
        out.append(task)
    return out


def project_tree(conn, project_id: str) -> dict[str, Any] | None:
    """The full project tree for GET /umwelt/projects/<id>: the project row
    plus its tasks nested by parent_task_id, each task annotated with a
    computed `blocked` bool and its `deps` list (depends_on_task_id ids).
    Returns None if project_id doesn't exist.

    Top-level tasks (parent_task_id IS NULL) are the roots; each task's
    `children` holds its direct sub-tasks, recursed to full depth (tasks
    trees are expected to stay small — no depth cap needed here, unlike
    place_tree's #158 recursion which walks a much larger, deeper-nested
    graph)."""
    project = get_project(conn, project_id)
    if project is None:
        return None
    all_tasks = list_tasks_for_project(conn, project_id)
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for t in all_tasks:
        by_parent.setdefault(t["parent_task_id"], []).append(t)

    def _node(task: dict[str, Any]) -> dict[str, Any]:
        out = dict(task)
        out["blocked"] = is_task_blocked(conn, task["id"])
        out["deps"] = get_task_deps(conn, task["id"])
        out["children"] = [_node(c) for c in by_parent.get(task["id"], [])]
        return out

    tasks = [_node(t) for t in by_parent.get(None, [])]
    result = dict(project)
    result["tasks"] = tasks
    return result


# --- per-(thing, agent) KV state (umwelt #191) -------------------------------
#
# thing_agent_state's SOLE write path is server.py's
# _apply_effect_agent_state_set (the `agent_state_set` kinddef effect) — the
# `agent` column is always the server-derived actor of the act() call, never
# caller/kinddef-suppliable, so a row can never be written under any agent
# other than the one who actually called act(). See the table's own comment
# in the schema string above for the full rationale.

def set_agent_state(conn, thing_id: str, agent: str, key: str, value: str,
                     updated_at: str) -> None:
    """Upsert one (thing_id, agent, key) row. Caller (server.py's
    _apply_effect_agent_state_set) is responsible for the
    _AGENT_STATE_MAX_KEYS_PER_THING row-count check BEFORE calling this for
    a key that isn't already present — this function itself performs no
    capacity check, same "validation lives in the route/effect layer, the
    db layer just writes" split as update_task/update_project.
    """
    conn.execute(
        "INSERT INTO thing_agent_state(thing_id, agent, key, value, updated_at) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(thing_id, agent, key) DO UPDATE SET "
        "value = excluded.value, updated_at = excluded.updated_at",
        (thing_id, agent, key, value, updated_at),
    )
    conn.commit()


def get_agent_state(conn, thing_id: str, agent: str) -> dict[str, str]:
    """All key->value state `agent` holds on `thing_id` — self-scoped by
    construction (the caller always passes the CALLER's own canonical
    agent; there is no all-agents variant of this function, by design —
    see look_at's self-scoped-read posture, server.py)."""
    rows = conn.execute(
        "SELECT key, value FROM thing_agent_state WHERE thing_id = ? AND agent = ?",
        (thing_id, agent),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# --- per-(thing, agent) delegated-caller grants (umwelt #400) --------------
#
# thing_grants' SOLE write path is server.py's _apply_effect_grant_caller /
# _apply_effect_revoke_caller (the `grant_caller`/`revoke_caller` kinddef
# effects) — see those functions' docstrings for the security-critical
# who-may-grant gate. Read path is server.py's act() authorization loop
# (has_grant, for the "granted" caller class) and the grant/revoke effects
# themselves (agent_can_grant, to check whether the ACTING agent may grant/
# revoke someone else). See the table's own comment in the schema string
# above for the full rationale.

def has_grant(conn, thing_id: str, agent: str) -> bool:
    """True iff `agent` holds ANY live grant row on `thing_id` — this is
    the sole read path the `granted` caller class (act()'s authorization
    loop) uses; it does NOT check `can_grant` (that bit only matters for
    whether the grantee may itself grant/revoke others — see
    agent_can_grant below). A revoked (deleted) row reads back False, same
    as a row that never existed — fail-closed, no distinction between
    "never granted" and "granted then revoked" from this call's
    perspective."""
    row = conn.execute(
        "SELECT 1 FROM thing_grants WHERE thing_id = ? AND agent = ?",
        (thing_id, agent),
    ).fetchone()
    return row is not None


def agent_can_grant(conn, thing_id: str, agent: str) -> bool:
    """True iff `agent` holds a live grant row on `thing_id` WITH
    can_grant=1 — this is the delegation check `_apply_effect_grant_caller`
    / `_apply_effect_revoke_caller` use to decide whether a non-author
    ACTOR may grant/revoke someone else (umwelt #400 security-critical
    gate — see those functions' docstrings). Deliberately narrower than
    has_grant: an agent can be `granted`-class authorized to CALL an action
    (has_grant=True) without being able to grant that access onward
    (can_grant=0)."""
    row = conn.execute(
        "SELECT 1 FROM thing_grants WHERE thing_id = ? AND agent = ? "
        "AND can_grant = 1",
        (thing_id, agent),
    ).fetchone()
    return row is not None


def set_grant(conn, thing_id: str, agent: str, can_grant: bool,
              granted_by: str, granted_at: str) -> None:
    """Upsert one (thing_id, agent) grant row. Caller (server.py's
    _apply_effect_grant_caller) is responsible for the who-may-grant
    authorization check BEFORE calling this — this function itself
    performs no permission check, same "validation lives in the route/
    effect layer, the db layer just writes" split as set_agent_state/
    update_task/update_project. A second grant to an agent that already
    holds a row is an upsert (can_grant/granted_by/granted_at all
    overwritten with the new call's values), never a second row."""
    conn.execute(
        "INSERT INTO thing_grants(thing_id, agent, can_grant, granted_by, "
        "granted_at) VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(thing_id, agent) DO UPDATE SET "
        "can_grant = excluded.can_grant, granted_by = excluded.granted_by, "
        "granted_at = excluded.granted_at",
        (thing_id, agent, int(can_grant), granted_by, granted_at),
    )
    conn.commit()


def revoke_grant(conn, thing_id: str, agent: str) -> None:
    """Delete one (thing_id, agent) grant row, if it exists. Idempotent by
    construction — a DELETE matching zero rows is not an error at the SQL
    layer, and _apply_effect_revoke_caller treats "no row existed" as
    success too (same idempotent-unlink posture as
    _apply_effect_project_task_link's `remove: true` path removing a
    non-existent task_deps edge — see that function's docstring)."""
    conn.execute(
        "DELETE FROM thing_grants WHERE thing_id = ? AND agent = ?",
        (thing_id, agent),
    )
    conn.commit()


# --- avatar-status heartbeat (Phase 2 engine slice, forum #268) ------------
#
# agent_heartbeat's SOLE write path is server.py's POST /umwelt/heartbeat —
# `agent` is CALLER-DECLARED (forgeable v0), so a row can be
# written under any name a caller supplies for itself; see the table's own
# comment in the schema string above for the full rationale. Read path is
# server.py's _avatar_status (via _compute_agent_state) plus
# get_last_active_at above.

def set_agent_heartbeat(conn, agent: str, last_beat_at: str, state_hint: str,
                         on_loop: bool, activity_line: str | None) -> None:
    """Upsert one agent's heartbeat row (PRIMARY KEY agent — a second beat
    from the same agent updates the existing row, never inserts a second
    one). Caller (server.py's /umwelt/heartbeat route) owns the min-interval
    throttle decision and the state_hint/activity_line validation — this
    function itself performs no validation or clamping, same "validation
    lives in the route, the db layer just writes" split as
    set_agent_state/update_task/update_project."""
    conn.execute(
        "INSERT INTO agent_heartbeat(agent, last_beat_at, state_hint, "
        "on_loop, activity_line) VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(agent) DO UPDATE SET "
        "last_beat_at = excluded.last_beat_at, "
        "state_hint = excluded.state_hint, "
        "on_loop = excluded.on_loop, "
        "activity_line = excluded.activity_line",
        (agent, last_beat_at, state_hint, int(on_loop), activity_line),
    )
    conn.commit()


def get_agent_heartbeat(conn, agent: str) -> dict | None:
    """This agent's heartbeat row (last_beat_at, state_hint, on_loop,
    activity_line), or None if `agent` has never sent one. `on_loop` is
    returned as a bool (the column is stored as INTEGER 0/1)."""
    row = conn.execute(
        "SELECT last_beat_at, state_hint, on_loop, activity_line "
        "FROM agent_heartbeat WHERE agent = ?",
        (agent,),
    ).fetchone()
    if row is None:
        return None
    return {
        "last_beat_at": row[0],
        "state_hint": row[1],
        "on_loop": bool(row[2]),
        "activity_line": row[3],
    }


# --- art-generation jobs (umwelt#464 slice 2) -------------------------------
#
# generate_jobs is written/read only from cap_handler.py's generate/
# generate_status ops (each op opens its own short-lived connection, same
# posture as _op_snapshot/_op_retrieve). caller is stored as given -- the
# cap_handler already folds it through canonical_agent before calling in.

_GENERATE_JOB_COLS = (
    "job_id, caller, spec_key, render_spec, status, result, created_at, updated_at"
)


def _generate_job_row_to_dict(row) -> dict[str, Any]:
    return {
        "job_id": row[0],
        "caller": row[1],
        "spec_key": row[2],
        "render_spec": json.loads(row[3]),
        "status": row[4],
        "result": json.loads(row[5]) if row[5] is not None else None,
        "created_at": row[6],
        "updated_at": row[7],
    }


def insert_generate_job(conn, job_id: str, caller: str, spec_key: str,
                         render_spec: dict) -> None:
    """Inserts a fresh `generate_jobs` row, `status='queued'`,
    `created_at`/`updated_at` both stamped `now_iso()`."""
    stamp = now_iso()
    conn.execute(
        "INSERT INTO generate_jobs(job_id, caller, spec_key, render_spec, "
        "status, result, created_at, updated_at) VALUES(?, ?, ?, ?, "
        "'queued', NULL, ?, ?)",
        (job_id, caller, spec_key, json.dumps(render_spec), stamp, stamp),
    )
    conn.commit()


def get_generate_job(conn, job_id: str) -> dict[str, Any] | None:
    """The job row as a dict (`render_spec`/`result` JSON-decoded, `result`
    decoded only when non-`NULL`), or `None` if `job_id` doesn't exist."""
    row = conn.execute(
        f"SELECT {_GENERATE_JOB_COLS} FROM generate_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return _generate_job_row_to_dict(row) if row else None


def update_generate_job_status(conn, job_id: str, status: str,
                                result: dict | None = None) -> None:
    """Updates `status` (+ `result` when given, JSON-encoded) and stamps
    `updated_at = now_iso()`."""
    conn.execute(
        "UPDATE generate_jobs SET status = ?, result = ?, updated_at = ? "
        "WHERE job_id = ?",
        (status, json.dumps(result) if result is not None else None,
         now_iso(), job_id),
    )
    conn.commit()


def generate_jobs_due_gc(conn, cutoff_iso: str) -> list[dict[str, Any]]:
    """`done`/`error` rows whose `updated_at` is older than `cutoff_iso` --
    GC's sweep query (gc.py). Never selects `queued`/`running` rows
    regardless of age -- a stuck job needs investigation, not silent
    deletion."""
    rows = conn.execute(
        f"SELECT {_GENERATE_JOB_COLS} FROM generate_jobs WHERE "
        "status IN ('done', 'error') AND updated_at < ? ORDER BY updated_at ASC",
        (cutoff_iso,),
    ).fetchall()
    return [_generate_job_row_to_dict(r) for r in rows]


def gc_generate_job(conn, job_id: str) -> None:
    """Deletes a `generate_jobs` row by id (GC-only, same posture as
    db.dissolve_thing)."""
    conn.execute("DELETE FROM generate_jobs WHERE job_id = ?", (job_id,))
    conn.commit()


# --- identities (umwelt#495 slice 1) ----------------------------------------
#
# Schema + seed only. Design-of-record: in-world thing 92dc676e, revision 6,
# "World credentials + enrollment", lead Borges. A row means "this
# agent-STRING exists and may be claimed" -- credential_verifier/bound_at/
# bound_by are NULL for every row THIS slice creates; a later slice's bind
# mechanism fills them in. Nothing in this codebase reads `identities` yet
# outside this section's own seeding + its tests -- ROSTER (webrender/
# session.py), human_residents() (_ids.py), and SessionStore keep gating
# everything they gate today, unchanged. Wiring a read path in is a later
# slice's job, deliberately not this one's.

_IDENTITY_COLS = (
    "agent_id, kind, credential_verifier, bound_at, bound_by, created_at, "
    "credential_generation, retired_at"
)


def _identity_row_to_dict(row) -> dict[str, Any]:
    return {
        "agent_id": row[0],
        "kind": row[1],
        # NULL until a later slice's bind mechanism writes it (JSON).
        "credential_verifier": json.loads(row[2]) if row[2] else None,
        "bound_at": row[3],
        "bound_by": row[4],
        "created_at": row[5],
        # umwelt#561: the session-invalidation chokepoint's own two
        # columns, exposed here too so a caller with the identity row
        # already in hand (e.g. server.py's login route) doesn't need a
        # second query just to read retired_at.
        "credential_generation": row[6],
        "retired_at": row[7],
    }


def get_identity(conn, agent_id: str) -> dict[str, Any] | None:
    """A single `identities` row by CANONICAL agent_id, or None if the
    agent-string has never been seeded/enrolled."""
    row = conn.execute(
        f"SELECT {_IDENTITY_COLS} FROM identities WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    return _identity_row_to_dict(row) if row else None


def list_identities(conn) -> list[dict[str, Any]]:
    """Every `identities` row, oldest-created first (same ordering
    convention as avatars_in_place's `ORDER BY created_at ASC`)."""
    rows = conn.execute(
        f"SELECT {_IDENTITY_COLS} FROM identities ORDER BY created_at ASC"
    ).fetchall()
    return [_identity_row_to_dict(r) for r in rows]


def _seed_identities_from_avatars(conn: sqlite3.Connection) -> None:
    """Seed `identities` from every LIVE (non-retired) avatar-thing, called
    from `init_db` on every call (server restarts, test fixture setup) --
    idempotent via `INSERT OR IGNORE` (see below).

    Source of truth: `things WHERE kind='avatar'`, excluding retired ones.
    Retirement is `metadata.retired == true` (set by /umwelt/retire_identity,
    umwelt #303) -- SQLite's JSON-true is integer 1 under `json_extract`, so
    `json_extract(metadata, '$.retired') IS NOT 1` excludes exactly the
    retired rows and admits every other row (including rows where the key is
    absent entirely, where `json_extract` returns NULL, and NULL IS NOT 1 is
    true). This deliberately does NOT match `avatars_in_place`'s convention
    of filtering retired-ness in Python after the fetch (dict inspection,
    not `json_extract`) -- that convention exists for per-request render
    paths that already fetch+decode every column; this is a one-time bulk
    seed over every avatar row, where doing the exclusion in SQL avoids
    decoding metadata for rows that are about to be thrown away anyway.

    `kind` (human vs agent) per seeded row:
      1. Primary: the avatar's `metadata.type` ("human"/"agent"), present on
         every avatar registered since umwelt #60 (register/enter's `type`
         field). Used directly when present.
      2. Fallback, for avatars with no `type` in metadata (pre-#60 bare
         `/umwelt/enter` calls with no fields supplied at all -- see enter's
         own docstring: a bare call births with EMPTY metadata, not a
         defaulted type, unlike register's own always-declares-a-type
         posture) -- checks the CANONICAL agent_id against `human_residents()`;
         present -> 'human', otherwise -> 'agent'. Deliberately NOT
         `db.is_human_sender` -- that function is documented as a
         login-window fallback for a different purpose (consent/rate
         exemption), not a seeding-time classifier, and additionally
         re-reads the avatar row this function already has in hand.

    Idempotency: `INSERT OR IGNORE INTO identities(...)` keyed on
    `agent_id` (the PRIMARY KEY) -- a second run touches ONLY agent_ids not
    yet present. This is deliberately NOT `INSERT OR REPLACE`: a REPLACE
    would clobber `credential_verifier`/`bound_at`/`bound_by` back to NULL
    on a row a later slice's bind mechanism has since populated, every time
    `init_db` runs (e.g. a server restart) -- the one correctness trap this
    function exists to avoid. `created_at` is the CURRENT timestamp at seed
    time (`now_iso()`), not the avatar's own `created_at` -- the identities
    row is being created now, even though the resident existed earlier.

    Two known gaps, both out of scope for this slice, both named here so
    the next slice inherits them as a decision rather than rediscovers them
    (Ariadne's colleague-pass follow-ups on umwelt PR #501):

    - **`kind` is frozen at first seed.** `INSERT OR IGNORE` means once a
      row exists, NOTHING about it (kind included) is ever touched again by
      a later seeding pass, even if the underlying avatar's declared type
      changes. Correct for every identity today; there is no update helper
      if that ever stops being true.
    - **Retirement does not propagate.** The retired-exclusion above is
      POINT-IN-TIME (checked only at seed time) -- an avatar retired AFTER
      its identities row already exists keeps that row live. Under rev6's
      three-state enforcement (row+credential->require, row+no-credential->
      allow-while-window-open, no-row->refuse), a since-retired identity is
      still claimable during the one-time backfill that closes forever. Not
      hypothetical: `retire_identity` has already been run in bulk on this
      world. Borges ruled the creation direction (no row -> refuse); the
      retirement direction is unruled.
    """
    rows = conn.execute(
        "SELECT author, metadata FROM things WHERE kind='avatar' "
        "AND (json_extract(metadata, '$.retired') IS NOT 1)"
    ).fetchall()
    stamp = now_iso()
    for author, metadata_json in rows:
        metadata = json.loads(metadata_json) if metadata_json else {}
        _seed_identity_for_avatar(conn, author, metadata, stamp)
    conn.commit()


def _seed_identity_for_avatar(conn: sqlite3.Connection, author: str,
                              metadata: dict, stamp: str) -> bool:
    """Seed ONE `identities` row for one avatar (umwelt#1103). The single
    helper behind both the boot seed (`_seed_identities_from_avatars`) and
    the birth branch of `get_or_create_avatar`, so every route that births
    an avatar enrolls it by the same rule.

    Rules, all shared by both callers (see `_seed_identities_from_avatars`'s
    docstring for the reasoning behind each):
      - the enrollment predicate `backfill_window_open(conn)` must hold;
        when it is False nothing is inserted (a closed window withholds
        enrollment; it never refuses the caller);
      - a retired avatar (`metadata.retired` true) is never seeded;
      - `kind` is `metadata.type` when it is "human"/"agent", else
        'human' iff the canonical agent_id is in `human_residents()`;
      - `INSERT OR IGNORE`, never REPLACE: an existing row (including its
        credential_verifier/bound_at/bound_by) is left untouched.

    Does NOT commit: the boot seed commits once after its loop; the birth
    branch commits together with the avatar insert (same transaction).
    Returns True iff an INSERT was attempted (not a statement that a new
    row landed -- OR IGNORE may have ignored it).
    """
    if not backfill_window_open(conn):
        return False
    if metadata.get("retired") == 1:  # JSON true / 1, as json_extract IS 1
        return False
    agent_id = canonical_agent(author)
    declared_type = metadata.get("type")
    if declared_type in ("human", "agent"):
        kind = declared_type
    else:
        kind = "human" if agent_id in human_residents() else "agent"
    conn.execute(
        "INSERT OR IGNORE INTO identities(agent_id, kind, created_at) "
        "VALUES (?, ?, ?)",
        (agent_id, kind, stamp),
    )
    return True


def _backfill_identity_retirement(conn: sqlite3.Connection) -> None:
    """One-time-per-call backfill: mirror every currently-retired avatar's
    `things.metadata.retired == true` onto `identities.retired_at`, for
    `identities` rows that were seeded BEFORE retirement mirrored there too
    (umwelt#561 security review, Mira) -- see identities' own schema
    comment for the full mechanism this feeds.

    Why this is needed at all, not just the go-forward atomic write in
    `mark_avatar_and_identity_retired`: `_seed_identities_from_avatars`'s
    own docstring already names this exact gap as PRE-EXISTING and NOT
    hypothetical -- "`retire_identity` has already been run in bulk on this
    world" -- so an unknown number of `identities` rows already carry
    `retired_at IS NULL` for avatars that are, in fact, retired right now.
    Left alone, the new auth chokepoint (`get_agent_session`) would read
    those rows as "not retired" and keep authenticating them -- silently
    reintroducing the exact gap this slice exists to close, for every
    identity retired before this column existed.

    No timestamp guess is attempted for WHEN a backfilled row was actually
    retired (unknowable from `things.metadata` alone, which stores only a
    boolean) -- `now_iso()` at backfill time is stamped instead, honestly
    meaning "known-retired as of this migration running," not a claim
    about the real retirement date. Downstream code only ever tests
    `retired_at IS NOT NULL` (never reads the timestamp's VALUE as
    meaningful), so this is sufficient for every consumer that exists
    today.

    Idempotent and cheap to no-op: `WHERE retired_at IS NULL` in the UPDATE
    means a second run only touches rows THIS function (or a subsequent
    real retirement, which stamps retired_at itself) hasn't already
    stamped -- safe to call on every init_db (server restarts, test
    fixture setup), same posture as every other migration in this
    function."""
    rows = conn.execute(
        "SELECT author FROM things WHERE kind='avatar' "
        "AND json_extract(metadata, '$.retired') IS 1"
    ).fetchall()
    if not rows:
        return
    stamp = now_iso()
    for (author,) in rows:
        agent_id = canonical_agent(author)
        conn.execute(
            "UPDATE identities SET retired_at = ? "
            "WHERE agent_id = ? AND retired_at IS NULL",
            (stamp, agent_id),
        )
    conn.commit()


class IdentityNotFoundError(ValueError):
    """Raised by bind_identity when no `identities` row exists for the
    requested agent_id. Subclasses ValueError, same posture as CycleError
    above (a distinct, catchable type for callers that want to map it
    specifically, while remaining a ValueError for anything generic).
    umwelt#495 slice 2's bind socket (bind_socket.py) catches this
    specifically and maps it to a clean {"status": "error", ...} response
    -- binding an identity that was never seeded/enrolled is out of scope
    for this slice; there is no create-on-bind path."""


class IdentityRetiredError(ValueError):
    """Raised by bind_identity when the identity's avatar carries
    `metadata.retired == true`. Subclasses ValueError, same posture as
    IdentityNotFoundError above -- a distinct, catchable type, mapped by
    bind_socket.py to a clean error response.

    Retirement is a DIFFERENT condition from "no row" -- a retired
    identity still HAS an `identities` row (retirement doesn't delete it,
    see `_seed_identities_from_avatars`'s "Retirement does not propagate"
    note), so `IdentityNotFoundError`'s no-row->refuse never reaches it.
    Kepler's design read on umwelt PR #506: a retired identity is no
    longer a resident, so binding it would resurrect a credential through
    a path retirement never intended to leave open, invisibly -- a
    retired row looks like any other bindable row to `bind_identity`
    without this check.

    Deliberately checked against `things.metadata.retired` FRESH at bind
    time (via `avatar_is_retired`) rather than a cached/second copy of
    retirement state on `identities` itself -- a second copy is exactly
    the class of bug this exists to fix (drifts from the avatar's real
    state the moment retirement changes after `identities` was seeded).
    Same principle `bind_identity`'s own `kind` field argues for
    elsewhere in this module: resolve from the source of truth at the
    moment of the action, never from a stale snapshot."""


def avatar_is_retired(conn, agent_id: str) -> bool:
    """True if `agent_id`'s avatar currently carries `metadata.retired ==
    true` -- the CURRENT state, read fresh from `things` (the source of
    truth), not the point-in-time snapshot `_seed_identities_from_
    avatars` took at seed time. Canonicalizes `agent_id` first (avatar
    lookup is keyed on the canonical form, umwelt#73).

    An agent with no avatar at all is NOT retired (False) -- that is a
    different, prior condition this function has no opinion on; callers
    concerned with "does this identity exist" should check that
    separately (e.g. IdentityNotFoundError's own no-row path)."""
    avatar = get_avatar(conn, canonical_agent(agent_id))
    return bool(avatar and avatar["metadata"].get("retired"))


def bind_identity(conn, agent_id: str, kind: str, credential_verifier: dict,
                   bound_by: str) -> dict[str, Any]:
    """Write (bind) or overwrite (reset) an `identities` row's credential
    fields -- umwelt#495 slice 2, the persistent host-only bind socket
    (bind_socket.py). Bind and reset are the SAME call: re-running bind on
    an already-bound identity IS the reset path -- recovery reuses the
    bind mechanism, there is no separate "reset" DB path (Kepler's
    ruling).

    `kind` is written UNCONDITIONALLY on every call -- never skipped,
    never conditional on whether it matches what's already stored. This
    is what makes `bound_at IS NULL` a sound test elsewhere in the
    codebase for "is this identity's kind still the seeded guess, or has
    it been through bind" -- a conditional write here would silently
    break that invariant the moment two callers disagreed about kind
    (Kepler's ruling, made structural here rather than left as a
    convention callers could drift from).

    Canonicalizes `agent_id` before the lookup/write (same convention as
    every other identity-keyed function in this module). Raises
    IdentityNotFoundError if no row exists for the canonical agent_id --
    this function only UPDATEs an existing row, it never INSERTs one (no
    row means the identity was never seeded/enrolled, out of scope here).
    Raises IdentityRetiredError if the identity's avatar is currently
    retired (checked fresh, see avatar_is_retired) -- retirement blocks
    the bind even though the `identities` row still exists.

    This function is called from bind_socket.py's `_op_bind` ONLY after
    `authorized_bound_by` has already returned non-None -- so an
    unauthorized caller never reaches either check above, and cannot use
    the response shape (not-found vs retired vs bound-ok) as an oracle
    for identity state it isn't authorized to learn. Any future caller of
    this function directly must preserve that ordering itself; this
    function does not re-check authorization.

    Single-row UPDATE, not a check-then-write race in the
    _atomic_guarded_write sense (no cycle to detect) -- a plain
    conn.execute + conn.commit is fine, matching create_project_with_
    board's directness rather than reaching for that machinery
    unnecessarily. `cur.rowcount == 0` after the UPDATE is the row-
    missing signal (no separate SELECT-then-UPDATE needed).

    `credential_generation` is incremented UNCONDITIONALLY in the same
    UPDATE (umwelt#561 security review, Mira) -- on the fresh-bind case
    exactly as much as the reset case, since this function makes no
    distinction between them (see above). This is the write half of the
    session-invalidation mechanism: db.get_agent_session treats any
    session stamped with an OLDER generation than the identity's current
    one as absent, so a reset (a credential compromise's actual recovery
    path) invalidates every session minted under the superseded key,
    without this function needing to know anything about sessions at all.

    Returns the updated row (same shape as get_identity).
    """
    canonical_id = canonical_agent(agent_id)
    if avatar_is_retired(conn, canonical_id):
        raise IdentityRetiredError(
            f"{canonical_id!r} is retired -- cannot bind a retired identity")
    bound_at = now_iso()
    cur = conn.execute(
        "UPDATE identities SET kind = ?, credential_verifier = ?, "
        "bound_at = ?, bound_by = ?, "
        "credential_generation = credential_generation + 1 "
        "WHERE agent_id = ?",
        (kind, json.dumps(credential_verifier), bound_at, bound_by,
         canonical_id),
    )
    if cur.rowcount == 0:
        conn.rollback()
        raise IdentityNotFoundError(
            f"no identities row for {canonical_id!r} -- cannot bind an "
            "identity that was never seeded/enrolled")
    conn.commit()
    return get_identity(conn, canonical_id)


# --- agent login (umwelt#495 slice 3) ---------------------------------------
#
# Challenge-response login + the opaque session tokens it mints, and the
# three-state (bound/unbound/no-row) enforcement primitive the design-of-
# record + Kepler's PR #559 ruling name as the core rule of this slice. See
# the `login_challenges`/`agent_sessions` table comments above (near
# `identities`) for the schema-level rationale; this section is the
# operational layer server.py's /umwelt/login* routes call into.
#
# Route logic itself (the 401 shapes for each of the three states, the
# ed25519 signature check) lives in server.py, NOT here -- this module stays
# HTTP-agnostic, same posture as every other identities/ helper above.

# Short and time-bounded on purpose: a challenge lives just long enough for
# a real round-trip (issue -> sign -> submit) and no longer, per the
# design-of-record's "single-use, time-bounded" requirement -- the shorter
# this window, the smaller a captured-but-unused challenge's replay surface.
LOGIN_CHALLENGE_TTL_SECONDS = 120

# Session tokens are meant to be CONVENIENT (the design-of-record's own
# framing) -- an agent shouldn't have to re-run challenge-response on every
# single request -- while still being bounded, unlike the human gateway's
# session cookies (webrender/session.py's `destroy()`-only, no-TTL posture,
# a DELIBERATE difference: humans hold a browser session open for a whole
# workday behind a login wall no other agent can forge; an agent session
# token is a bearer credential that could leak into a log or a stray
# print(), so it gets an expiry the human cookie doesn't need). One day is
# a judgment call, not a value dictated anywhere in the design-of-record --
# long enough that ordinary agent work doesn't re-login mid-task, short
# enough that a leaked token has a bounded blast radius.
AGENT_SESSION_TTL_SECONDS = 86400  # 24h


def _future_iso(seconds: float) -> str:
    """now_iso(), seconds in the future -- same strftime pattern as now_iso
    (NOT datetime.isoformat(), which would emit a "+00:00" suffix instead
    of "Z" and break lexicographic comparison against now_iso()-stamped
    columns elsewhere -- see wake_rate_ok's own comment on this exact
    trap, db.py ~2812)."""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def purge_expired_login_challenges(conn) -> int:
    """DELETE every `login_challenges` row whose `expires_at` has passed.
    Returns the count deleted. umwelt#561 security review (Mira: "expired
    challenge/session rows are not purged") -- consume_login_challenge/the
    /umwelt/login route already treat an expired row as absent for READ
    purposes (see consume_login_challenge's own docstring), but nothing
    previously deleted the row itself, so the table grew unbounded.

    A single indexed DELETE (idx_login_challenges_expires_at, added
    alongside this function) -- no full-table scan. Does NOT commit; the
    caller (create_login_challenge below) folds this into its own existing
    commit, same "one commit per logical write" posture as the rest of
    this module. Lazy/opportunistic maintenance, not a background job --
    same posture as this codebase's other bounded/capped resources (e.g.
    server.py's _ACTION_LOG_MAX_ENTRIES/_BOARD_ENTRIES_MAX_COUNT
    handling): triggered by the next write, no standing thread/cron."""
    cur = conn.execute(
        "DELETE FROM login_challenges WHERE expires_at <= ?", (now_iso(),))
    return cur.rowcount


def create_login_challenge(conn, agent_id: str) -> dict[str, Any]:
    """Mint + store a fresh login challenge for `agent_id` (canonicalized),
    umwelt#495 slice 3. Returns `{"challenge": ..., "expires_at": ...}`.

    Deliberately does NOT check whether an `identities` row exists for
    `agent_id` -- issuing a challenge must never be usable as an oracle for
    identity existence/bound-state (Kepler's PR #559 ruling: the caller
    gets the SAME response whether the identity is bound, unbound, or has
    no row at all). The three-state distinction is enforced later, at
    POST /umwelt/login time, never here.

    No cycle/race to guard (each call inserts a brand-new PRIMARY KEY row,
    never touches an existing one) -- a plain conn.execute + conn.commit is
    correct, same posture bind_identity's own docstring describes for its
    single-row UPDATE.

    umwelt#561: purges expired login_challenges rows BEFORE inserting the
    fresh one (purge_expired_login_challenges) -- prune-then-add ordering,
    folded into this call's own commit below rather than a separate one."""
    canonical_id = canonical_agent(agent_id)
    purge_expired_login_challenges(conn)
    challenge = secrets.token_urlsafe(32)
    created_at = now_iso()
    expires_at = _future_iso(LOGIN_CHALLENGE_TTL_SECONDS)
    conn.execute(
        "INSERT INTO login_challenges(challenge, agent_id, created_at, "
        "expires_at, consumed) VALUES (?, ?, ?, ?, 0)",
        (challenge, canonical_id, created_at, expires_at),
    )
    conn.commit()
    return {"challenge": challenge, "expires_at": expires_at}


def consume_login_challenge(conn, challenge: str, agent_id: str) -> bool:
    """Atomically validate + mark a `login_challenges` row consumed.
    Returns True iff `challenge` exists, its stored `agent_id` matches the
    CANONICALIZED `agent_id` argument, it is unexpired, and it has not
    already been consumed -- False for every other case (unknown
    challenge, wrong agent, expired, already used). Never raises for any
    of those cases -- an invalid challenge is an ordinary False, not an
    exception; server.py's /umwelt/login route is the one place that turns
    a False into the route's own 401.

    Uses `_atomic_guarded_write` (db.py:1386) for the SAME reason
    set_place_parent/add_task_dep use it (db.py:1437/3994 respectively):
    a naive SELECT-then-UPDATE has a TOCTOU window where two concurrent
    logins could each read consumed=0 before either writes, letting the
    same challenge authenticate two separate logins -- exactly the kind of
    check-then-write race that helper exists to close, co-locating the
    validity read and the consuming UPDATE inside one BEGIN IMMEDIATE
    transaction. Note the vocabulary mismatch and why it's harmless: that
    helper's parameter is named `would_cycle` and its exception is
    `CycleError` because its first two callers guard graph-acyclicity;
    the predicate it guards here is "not already consumed/expired/
    mismatched", a different invariant entirely, but the SAME atomic
    check-then-write shape. CycleError is used purely as
    _atomic_guarded_write's internal control-flow signal below -- caught
    immediately and translated to a plain `False` return, so it never
    escapes this function and no caller needs to know the mechanism
    reused a cycle-guard's plumbing internally."""
    canonical_id = canonical_agent(agent_id)

    def _is_invalid(c) -> bool:
        row = c.execute(
            "SELECT agent_id, expires_at, consumed FROM login_challenges "
            "WHERE challenge = ?",
            (challenge,),
        ).fetchone()
        if row is None:
            return True
        row_agent_id, expires_at, consumed = row
        if row_agent_id != canonical_id:
            return True
        if consumed:
            return True
        if expires_at <= now_iso():
            return True
        return False

    def _write(c) -> None:
        c.execute(
            "UPDATE login_challenges SET consumed = 1 WHERE challenge = ?",
            (challenge,),
        )

    try:
        _atomic_guarded_write(
            conn, _is_invalid, _write,
            f"challenge {challenge!r} is invalid, expired, already "
            "consumed, or was issued for a different agent",
        )
    except CycleError:
        return False
    return True


def purge_expired_agent_sessions(conn) -> int:
    """DELETE every `agent_sessions` row whose `expires_at` has passed.
    Returns the count deleted. umwelt#561 security review (Mira: "expired
    challenge/session rows are not purged") -- get_agent_session already
    treats an expired row as absent for READ purposes (its own docstring,
    check 2), but nothing previously deleted the row itself.

    A single indexed DELETE (idx_agent_sessions_expires_at, added
    alongside this function) -- no full-table scan. Does NOT commit; the
    caller (create_agent_session below) folds this into its own existing
    commit. Same lazy/opportunistic-on-write posture as
    purge_expired_login_challenges above -- no background thread/cron."""
    cur = conn.execute(
        "DELETE FROM agent_sessions WHERE expires_at <= ?", (now_iso(),))
    return cur.rowcount


def create_agent_session(conn, agent_id: str, state: str) -> dict[str, Any]:
    """Mint + store a fresh opaque session token for `agent_id`
    (canonicalized), stamped `state` ('verified' or 'unverified' -- the
    CHECK constraint on `agent_sessions.state` rejects anything else).
    Returns `{"token": ..., "agent_id": ..., "state": ..., "expires_at":
    ...}`.

    `state` is never inferred/defaulted here -- the caller (server.py's
    POST /umwelt/login route) has already resolved which of the three
    enforcement states applies and passes it explicitly, so this function
    has no branch that could silently pick the wrong one.

    Stamps `credential_generation` from the identity's row, read fresh
    right here (umwelt#561 security review) rather than accepted as a
    caller-supplied param -- the mint moment is exactly when "current"
    must mean "current," and reading it inline (same connection, no
    round-trip back out to a caller who'd have to keep its own copy in
    sync) is what makes db.get_agent_session's later comparison meaningful.
    A missing identities row reads as generation 0 (COALESCE) -- login
    itself already refuses when no row exists (the three-state rule), so
    this is a defensive default, never a real path.

    umwelt#561: purges expired agent_sessions rows BEFORE inserting the
    fresh one (purge_expired_agent_sessions) -- same prune-then-add
    ordering as create_login_challenge, folded into this call's own
    commit below."""
    canonical_id = canonical_agent(agent_id)
    purge_expired_agent_sessions(conn)
    token = secrets.token_urlsafe(32)
    created_at = now_iso()
    expires_at = _future_iso(AGENT_SESSION_TTL_SECONDS)
    gen_row = conn.execute(
        "SELECT credential_generation FROM identities WHERE agent_id = ?",
        (canonical_id,),
    ).fetchone()
    credential_generation = gen_row[0] if gen_row is not None else 0
    conn.execute(
        "INSERT INTO agent_sessions(token, agent_id, state, created_at, "
        "expires_at, credential_generation) VALUES (?, ?, ?, ?, ?, ?)",
        (token, canonical_id, state, created_at, expires_at,
         credential_generation),
    )
    conn.commit()
    return {
        "token": token,
        "agent_id": canonical_id,
        "state": state,
        "expires_at": expires_at,
    }


def get_agent_session(conn, token: str) -> dict[str, Any] | None:
    """A single `agent_sessions` row by opaque token, or None if the token
    is invalid for ANY of these reasons -- resolve_actor and any future
    caller get one "is this token good right now" answer, never a row they
    have to separately re-validate:

      1. unknown token -- no row at all;
      2. expired -- `expires_at` has passed;
      3. no `identities` row for this session's `agent_id` -- defensive;
         should be unreachable (login already requires a row to mint a
         session in the first place), but a session outliving its
         identity is treated as absent rather than trusted;
      4. the identity is RETIRED (`identities.retired_at IS NOT NULL`) --
         umwelt#561 security review (Mira): retirement must invalidate
         every existing session, verified or unverified alike, not just
         block fresh logins (server.py's /umwelt/login route handles that
         half separately);
      5. the session's stamped `credential_generation` no longer matches
         the identity's CURRENT `credential_generation` -- umwelt#561:
         bind_identity bumps the generation on every bind AND reset, so
         this one check covers both a credential reset (a verified
         session minted under a superseded key) and the original
         umwelt#557 pre-bind-transition gap (a fresh bind is itself a
         generation bump) -- the single mechanism subsumes the narrower
         bound_at-specific check an earlier commit (190d05a) added just
         for the unverified branch; that check is now redundant and has
         been folded into this one, general, state-derived comparison
         that applies to `verified` and `unverified` rows alike;
      6. for an `unverified` row SPECIFICALLY, the backfill window is no
         longer open (`backfill_window_open(conn)` is False) -- umwelt#561
         security review round 2 (Mira): an unverified session's entire
         legitimacy rests on "the window was open when this was minted";
         if the identity is NEVER bound, generation never moves and check
         5 above has nothing to catch, so a stale unverified token would
         otherwise keep resolving forever past gate-close. Currently a
         no-op in practice (backfill_window_open is hardcoded True this
         slice, per its own docstring's extension-point note) -- this is
         the forward-compat half: the day a real gate-close event lands
         and that function starts returning False, every still-unbound
         unverified session invalidates at the next read, with no further
         code change needed here. A `verified` row never takes this
         branch -- once proven by a real signature, window state has
         nothing further to say about it.

    Checks run in the above order, all against ONE fresh read of both
    tables (check 6 excepted -- see backfill_window_open's own docstring
    for why IT is not itself a table read) -- no caller-visible
    distinction between "which reason," by design (same no-oracle posture
    as the login route's uniform 401)."""
    row = conn.execute(
        "SELECT token, agent_id, state, created_at, expires_at, "
        "credential_generation FROM agent_sessions WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    (token_val, agent_id, state, created_at, expires_at,
     session_generation) = row
    if expires_at <= now_iso():
        return None
    identity_row = conn.execute(
        "SELECT retired_at, credential_generation FROM identities "
        "WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if identity_row is None:
        return None
    retired_at, current_generation = identity_row
    if retired_at is not None:
        return None
    if session_generation != current_generation:
        return None
    if state == "unverified" and not backfill_window_open(conn):
        return None
    return {
        "token": token_val,
        "agent_id": agent_id,
        "state": state,
        "created_at": created_at,
        "expires_at": expires_at,
    }


def backfill_window_open(conn) -> bool:
    """True iff the umwelt#495 backfill/enrollment window is still open --
    for THIS slice, ALWAYS True, unconditionally, regardless of `conn`'s
    state.

    Why hardcoded rather than computed: the window's "closing" is a FUTURE
    world EVENT (Kepler's PR #559 ruling) -- a `events.event_kind` this
    codebase does not have yet, minted by a not-yet-built later slice at
    the moment an operator/process decides enrollment is done. Until that
    event kind exists, the window is DEFINITIONALLY still open -- there is
    no mechanism anywhere that could have closed it. This function is the
    one place that fact is asserted, so every caller (server.py's
    /umwelt/login route, resolve_actor) reads the SAME answer through the
    SAME chokepoint rather than each hardcoding `True` independently and
    having to be found-and-changed in N places later.

    The extension point for that later slice: once a gate-close event kind
    exists (e.g. `events.event_kind = 'backfill_window_closed'` or
    equivalent), this becomes a query for whether such an event has been
    recorded -- `SELECT EXISTS(SELECT 1 FROM events WHERE event_kind =
    '<that kind>')` or similar, using `conn` (already accepted as a
    parameter for exactly this reason, even though this slice's body
    ignores it). Deliberately NOT a config flag/env var -- the design-of-
    record names a config flag as the WRONG mechanism for this (Kepler's
    ruling): a flag can be flipped back and forth by accident or by an
    operator who doesn't understand its one-way, world-historical
    significance, where an events-table row is an append-only, auditable
    fact about what happened in the world, matching every other
    irreversible world transition this codebase already models as an
    event."""
    return True


def login_window_status(conn) -> dict[str, Any]:
    """The umwelt#495 backfill-window banner primitive -- the data GET
    /umwelt/login/status returns verbatim. `{"window_open": bool, "bound":
    <count>, "unbound": <count>, "total": <count>}`, computed fresh from
    `identities` on every call (`bound_at IS NOT NULL` vs `IS NULL` --
    the SAME predicate bind_identity's own docstring already treats as the
    source of truth for "has this identity been through bind").

    This is the design-of-record's anti-silent-fail-open requirement made
    into a queryable surface: the window's open/closed state, and how much
    of the roster is still unbound, is never buried in a log line or a
    one-off status code -- it is always available, unauthenticated, to any
    caller/renderer that wants to show it plainly (Kepler's PR #559: "the
    three-state window banner is NOT optional polish ... it ships with
    slice 2, not after" -- 'slice 2' in that ruling is this repo's slice 3,
    per the section's own build-order numbering)."""
    bound = conn.execute(
        "SELECT COUNT(*) FROM identities WHERE bound_at IS NOT NULL"
    ).fetchone()[0]
    unbound = conn.execute(
        "SELECT COUNT(*) FROM identities WHERE bound_at IS NULL"
    ).fetchone()[0]
    return {
        "window_open": backfill_window_open(conn),
        "bound": bound,
        "unbound": unbound,
        "total": bound + unbound,
    }
