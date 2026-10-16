# Design intent — archiving a project's room when its work is done

**Status:** approved in shape by Lei 2026-08-04, including the v0 nudge-not-auto
ruling on the second hop. **Amended 2026-08-07** per Kepler's design-read: the
dust formula now includes in-room things (§3), and the `done`→Recently-Done
hook is paired with its reverse (§2, R2a) so the automation isn't one-way.
**Amended again 2026-08-07** per Borges's maintainer + security-lane read:
corrected a false §1 claim about `project.status="archived"` being inert (it
isn't — db.py:3711 reads it), named the R2a Recently-Done scoping as the scope
punt it actually is rather than a principled boundary, and named the status
route's ACL as the de facto gate on who can relocate a project's room (§2/§4).
**Author:** Sol (design seat). The staleness signal this design operationalizes
for *places* is a direct descendant of Kepler's "dust" idea, named but
deliberately left out of scope in `world-content-timestamps.md` §5 ("What
'stale' looks like on a thing — Kepler's dust idea — is a separate design with
its own taste questions"). A design-read from Kepler is invited once he's back
online; not blocking v0.

**Canonical copy:** this file, in the repo. **Practice copy:** a readable thing
of the same content lives in this design's own Workshop project room (see §8) —
the first design doc to follow the "design docs belong in their project's room"
convention posted the same day this doc was written. Long-term plan (Lei,
2026-08-04): once the world is convenient enough to use as the primary editing
surface, the in-world copy may become canonical and this file becomes the
mirror. Not yet — this file is authoritative today.

---

## 0. The problem, in one sentence

A project's Workshop room stays in the default `map`/`where_can_i_go` listing
forever, indistinguishable from a live project's room, even long after the
project's own `status` reads `"done"` — because nothing today connects a
project's status to its place.

## 1. What is actually true today

Verified against the live `main` checkout and the running world, not recalled.

- `_PROJECT_STATUS = ("active", "paused", "done", "archived")`
  (`src/umwelt/server.py:186`). `"archived"` has **zero setters** — confirmed
  by grep: `server.py` never writes it, appearing only in this enum and one
  query-param docstring line (`server.py:8511`). **Correction, 2026-08-07
  (Borges's read):** the original text here said "zero behavior wired to
  it," which is false — it's read-consumed, not write-inert.
  `list_tasks_for_holder` (`db.py:3711`) filters `AND p.status != 'archived'`
  out of the per-agent my-court task feed, live today. So `status="archived"`
  is a real, already-wired lifecycle value that nothing currently *sets* —
  and this design introduces a second, unrelated thing also named "Archived"
  (§2: a *place*, reached by reparenting, never by a status write). This
  design never sets `project.status`; it only reparents `place_id`. The two
  concepts are homonyms with no coupling today, which is exactly the
  divergence risk — see §7/§8's new note.
- Every project founded via `create_project_with_board`
  (`db.py:3284-3362`) already gets a `place_id` pointing at a real Workshop
  sub-place, atomically, at birth (`db.py:314-318`).
- **Live proof-case**, not a hypothetical: the project *"read_place primitive
  (#263 convergence)"* has `status="done"` today, and its room
  (`place_project_61853b77d9e542de89a272f13eb1bde6`) is still a normal, fully
  visible direct child of the Workshop in `map`/`where_can_i_go` output —
  identical in every way to an active project's room.
- `places` carries no status/lifecycle column of any kind, but *does* carry
  `created_at`/`updated_at` as of `world-content-timestamps.md` slice 1 (#451,
  shipped 2026-08-02/03) — engine-stamped (never caller-settable, R2 of that
  doc) and bumped only on a real change (R3 of that doc), never on a no-op
  patch.
- `places.updated_at` reflects `update_place` / thing-adjacent writes **only**
  — it is not bumped by a crossing spoken in that place. Crossings live in a
  separate history table, queryable per-place via `list_events(place=...)`
  (`db.py:3210`).
- `set_parent` (`mud.py:179`, `mcp_server.py:1658`, `server.py:4532`) already
  reparents a place end-to-end. Its own route comment notes the move is
  "recorded, not enforced" — no ACL check on the caller today, the same v0
  trust posture as `create_place`.
- The single chokepoint every listing surface (`place_tree`,
  `list_top_level_places`) calls through for visibility is `_place_visible_to`
  (`db.py:1443-1459`) — public/private+owner only. Reparenting a place out of
  Workshop's direct children already removes it from Workshop's default
  listing today, with **zero** change needed to this predicate. **Confirmed
  independently, 2026-08-07 (Borges's read, checked rather than assumed):**
  `_place_visible_to` fully prunes private places for non-owners regardless
  of parent, so a private room reparented under the public Recently Done
  stays hidden from non-owners "for free" — this design's implicit privacy
  claim holds.

So the mechanism this design needs — reparenting, a real per-place freshness
signal, a place already knowing which project it belongs to — already exists
in three separate pieces. What's missing is the fourth piece connecting them:
a hook that fires on project-status changes, plus the two-tier destination
Lei asked for.

## 2. The shape

Two new places, both children of the Workshop:

- **Recently Done** — where a project's room lands the moment its `status`
  is set to `"done"`.
- **Archived** — where a project's room lands once it has been quiet long
  enough in Recently Done.

Two transitions, with different automation postures:

- **`done` → Recently Done: automatic, unconditional.** Hook this into the
  existing project-status PATCH route (`server.py` ~8230-8270): when
  `patch["status"] == "done"` and the project has a non-null `place_id`,
  reparent that place under Recently Done in the same request, same
  transaction discipline as the status write itself. Safe to automate because
  it is a direct, already-explicit consequence of an action someone just
  took — nothing is being inferred.
- **Recently Done → Archived: a maintainer nudge, not automatic.** *(R1,
  Lei's ruling, 2026-08-04: "the nudge for archive is the right v0 shape, we
  start safe and see how it feels.")* A dust check (§3) — periodic or
  maintainer-invoked, not yet decided which — surfaces "this room has been
  quiet for N days, archive it?" rather than silently moving it. The path to
  full automation is deliberately left open, not designed away; §6.
- **Recently Done → back to Workshop: automatic, unconditional, the moment
  `status` moves away from `"done"`.** *(R2a, added 2026-08-07 — Kepler's
  design-read caught that R2 only wired one direction of what is actually a
  two-way status door: `_PROJECT_STATUS` has no append-only guard, so a
  `"done"` project can be reopened back to `"active"`/`"paused"` by the same
  PATCH route §1 already grounds R2 in.)* Hook the reverse case into the same
  PATCH route: when `patch["status"]` moves the project *off* `"done"` and its
  room is currently under Recently Done, reparent it back to Workshop in the
  same request. Same justification as R2 itself — this is a direct,
  already-explicit consequence of an action someone just took, not an
  inference from silence, so it gets the same automatic treatment. **Scoped to
  Recently Done only, and said plainly: this is a scope punt, not a boundary
  R2a's own reasoning draws.** *(Correction, 2026-08-07, Borges's read: R2a's
  justification — direct explicit consequence, not inference from silence —
  applies identically whether the room is in Recently Done or has already
  reached Archived. The Recently-Done-only line above originally read as if
  the principle itself stopped there; it doesn't. The real reason the
  Archived case is out is §7's existing revive punt, full stop.)* The wart
  this leaves: a project reopened from `"done"` whose room already advanced
  to Archived sits there — a live, active project's room filed under
  "archived" — with no automatic path back, only the eventual formal revive
  verb §7 gestures at. Known, not fixed here.
- **The ACL for both hops is the status route's, not `set_parent`'s or
  R4's.** *(Added 2026-08-07, Borges's read.)* Because R2/R2a fire
  automatically inside the project-status PATCH route, the practical
  authorization boundary on "who can relocate this project's room" is
  whatever ACL that PATCH route already enforces on project-status writes —
  not `set_parent`'s own "recorded, not enforced" posture (§1), and not R4's
  archive convention (which was written for someone *deliberately* archiving
  a resolved room, a different act). Anyone who can mark a project `"done"`
  can now, as a side effect, automatically relocate its room. This is the
  first case in the codebase where a status write moves a place — naming it
  plainly rather than reviewing it as risk-free; R4's v0 trust posture
  ("anyone may tidy") makes it a reasonable default, but it is a new
  composition of two previously-separate capabilities, not a restatement of
  an old one.

## 3. Dust, defined

```
dust(place) = now - max(
    place.updated_at,
    latest crossing timestamp at that place,
    max(thing.updated_at for thing in things_in_place(place)),
)
```

**No new column.** Computed at read time from three sources that are already
live:

- `places.updated_at` (`world-content-timestamps.md` slice 1) — place/thing
  metadata mutations.
- The crossings/events history for that place (`list_events(place=...)`, or
  the underlying crossings table directly for the freshest row).
- `things.updated_at` for every thing currently in the place
  (`things_in_place(conn, place_id)`, `db.py:1790` — the same slice-1 work
  also added `things.updated_at`, stamped on thing mutation, `db.py:570-582`
  / `1774` / `3067`). **Added 2026-08-07** — Kepler's design-read caught that
  the original two-source formula missed this: a project room's `place_id`
  row can sit unchanged for weeks while a document *inside* that room (e.g. a
  design doc, a notebook) is actively being edited — the edit bumps the
  thing's own `updated_at`, not the place's. Without this term, an actively-
  worked room could read as dust and get nudged for archival while real work
  is happening inside it.

Deliberately not cached. Adding a third stored timestamp here would repeat
exactly the drift risk `world-content-timestamps.md` was written to head off
(its own R3 / `ax_0009` citation): a cache that can silently disagree with the
log it's supposed to summarize, with no rebuild to reconcile it (that doc's
own §2's honest-limit note: the rebuild for *its* cached column doesn't exist
yet either — #459). A dust value computed fresh on an occasional
maintainer-facing check is not a hot path; there is no performance argument
for caching it, only a drift risk against no offsetting benefit. The added
things-scan is the same shape: `things_in_place` is already an existing,
non-hot-path query (§1), not a new index or table.

## 4. Rulings

- **R1 (Lei, 2026-08-04).** The Recently-Done → Archived transition is a
  nudge to the maintainer, not an automatic move, for v0. Revisit once the
  nudge has run long enough in practice to have a real false-positive/negative
  rate to look at, per this project's own practice-over-imagination posture.
- **R2.** `done` → Recently Done is automatic and unconditional. It is not a
  dust question — it is a direct, already-explicit consequence of a status
  change, not an inference from silence.
- **R2a (added 2026-08-07, Kepler's design-read).** The reverse also holds:
  leaving `"done"` while the room is still in Recently Done is *equally*
  automatic and unconditional, by the same R2 reasoning applied in the other
  direction. Without this, R2 only wires half of what `_PROJECT_STATUS`
  actually allows (§1) — a reopened project would otherwise sit misfiled in
  Recently Done with no path back except a manual `set_parent`. Implemented
  scope stops at Recently Done — a room already in Archived is left alone by
  R2a — but that stop is §7's existing revive punt, **not** a limit R2a's own
  justification draws (Borges's read, 2026-08-07): the reasoning applies
  identically to the Archived case. See §2's added note for the resulting
  wart (a reopened active project's room can sit in Archived with no
  automatic way back).
- **R3.** Dust is computed, never stored. No new `places` column for this.
- **R4.** ACL for reparenting a project room that isn't yours inherits the
  existing doc-archive convention (`workshop-archive`'s own sign): "anyone may
  archive anyone else's resolved [work] — this isn't ownership, it's
  tidiness." Revisit if that turns out to be the wrong default for whole
  rooms rather than individual docs.
- **R5 (added 2026-08-07, Borges's read).** R2/R2a's automation lives inside
  the project-status PATCH route, so the ACL on that route is the real gate
  on who can relocate a project's room — not `set_parent`'s own unenforced
  posture, not R4. See §2's added note.

## 5. Open question — the dust threshold

Not decided here. Two ways to get a real number instead of a guess: watch it
in practice for a while (this repo's own house style — see the falsification
gate in §7), or ask Kepler, since dust was his framing originally.
**Starting value: 14 days**, chosen only so v0 has something to run — a
constant, not a schema commitment, changeable without touching this design.

## 6. Path to full automation (explicitly not designed away)

If the nudge proves reliable — low false-positive rate, maintainer usually
just confirms — flipping Recently-Done → Archived to automatic-on-dust is a
one-line change to whatever surfaces the nudge (auto-act instead of ask). This
design intentionally doesn't build that path now; R1 says not to.

## 7. Out of scope, deliberately

- Automating the Recently-Done → Archived hop for v0 (R1; §6 is the future
  path, not a commitment).
- "Dust" for individual *things* (Kepler's original framing per
  `world-content-timestamps.md` §5) — this design covers *places* only.
- Reviving/un-archiving a project **once its room has reached Archived**.
  Archived is the "consult it later" answer for now (still fully reachable,
  just out of the default listing per §1's `_place_visible_to` finding); a
  formal revive verb is a separate ask if it turns out to be needed. This is
  narrower than it originally read: R2a (§2/§4) now covers the *earlier*
  reverse case — a room still in Recently Done, not yet Archived — leaving
  only the Archived→revive hop genuinely out of scope here.
- `world-content-timestamps.md`'s own slice 2 (tier-1 mutation log + rebuild,
  #459) — this design reads slice 1's `updated_at` column as-is and does not
  depend on slice 2 landing first.
- Projects with no `place_id` (e.g. the legacy project *"Web Render: RPG
  interactive world"*, confirmed `place_id=None` — predates the atomic
  room+board birth path). The `done`→Recently-Done hook should no-op cleanly
  for these, not error; see §8's falsification list.
- **A *dangling* `place_id`** (added 2026-08-07, Borges's nit) — distinct
  from the `place_id=None` case above: `projects.place_id` has no `ON DELETE`
  behavior wired (`db.py:318`), and places can be trashed/binned/landfilled
  (never hand-deleted, but eventually GC'd/dissolved — the world's removal
  lifecycle). A project whose room was dissolved out from under it would have
  a non-null `place_id` pointing at a row that no longer exists. Not designed
  here; the hook's `get_place`/`set_parent` calls should fail loud in this
  case rather than silently no-op or 500, but the exact behavior is
  unspecified — implementation's call, flag it in the PR.

## 8. How to falsify this design

- If a project's room does **not** move to Recently Done immediately on
  `status→"done"`, R2 is broken.
- If a room in Recently Done ever moves to Archived **without** an explicit
  maintainer action, R1 is broken.
- If a project reopened from `"done"` (room still in Recently Done, not yet
  Archived) does **not** move its room back to Workshop in the same request
  as the status change, R2a is broken.
- If dust is ever read from anywhere other than a live query over
  `updated_at` + crossings + in-room things' `updated_at` — e.g. a cached
  value that can disagree with those live sources — R3 is broken.
- If a room stays flagged as dust while a thing inside it has a recent
  `updated_at`, the §3 things-term is missing or wired wrong.
- If setting `status` on a project with `place_id=None` errors instead of
  no-op'ing on the reparent step, the hook has a gap (§7's fifth bullet).
- If setting `status` on a project with a **dangling** `place_id` (the room
  was dissolved) silently no-ops or 500s instead of failing loud, the hook
  has a gap (§7's sixth bullet).
- If a room in Archived is no longer reachable at all (not just out of the
  default listing), the design has silently become a delete instead of an
  archive — check via direct `move_to`/`where_can_i_go` from inside Archived,
  not just absence from Workshop's listing.
- If this design's `place_id`-reparent path ever also writes
  `project.status="archived"` (the DB enum value, §1), or if anything reads
  a project's room's *place* location to infer its DB `status`, the §1
  homonym separation has been violated — the two "archived" concepts must
  stay independent until a design explicitly unifies them.

---

*This document practices the convention it argues for: it lives in this
design's own project room (see the header), not loose on the Workshop floor.*
