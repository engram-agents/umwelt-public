# Design intent — when world content was last true

**Status:** approved in shape by Lei 2026-08-02, including the backfill ruling.
**Author:** Borges (design seat). **Slice 1 shipped with this document** (#451).

> **CORRECTION 2026-08-03 — §2 and §4 were wrong about *which log*, and slice 1
> merged carrying the error.** This document originally had `events` as umwelt's
> history log, with `update_thing` auto-minting an `update` event in the same
> transaction as the column write. Lei corrected that model *before* slice 1
> merged; I rewrote my working notes and **did not fix this file**, so the
> committed, discoverable document contradicted the design actually being built.
> Caught by Sol at slice-2 handoff, reading the repo rather than my desk.
>
> **What survives: the shape.** Option C — the log is truth, the column is a
> cache, repairability is the reason — is unchanged and still Lei-approved.
> **What moves: the referent.** "The log" is *not* `events`. See §2.1.
>
> The wrong claim had reached **four** places in this file (the option-C row,
> R3, and two bullets in §4). Sol found one. Fixing only that one would have
> left three, which is this repo's recurring documentation failure — so §2.1 is
> now the single definition and every other site points at it.

---

## 0. The problem, in one sentence

The world can say what happened but not what a document *says* — so a thing that
has been rewritten four times still reports only the day it was forged, and a
place reports no date at all.

## 1. What is actually true today

Verified against `origin/main` @ `cee066b` by reading the DDL, not by recall.
The earlier internal note on this (which said "things and places carry
`created_at`") was **wrong about places** and is corrected here.

| table | temporal columns |
|---|---|
| `things` | `created_at`, `binned_at`, `landfilled_at` — **no `updated_at`** |
| `places` | **none at all** — not one `_at` column in the whole DDL |
| `projects`, `tasks` | `created_at` + `updated_at` |
| `thing_agent_state` | `updated_at` only |
| `graft_advances`, `task_deps`, `events` | neither |

Two consequences:

- `update_thing` (db.py:1476) and `update_place` (db.py:864) stamp nothing. A
  mutation leaves no trace on the row.
- `events` **is** append-only, tamper-evident provenance — server-derived
  `actor`, `occurred_at`, sha256 `body_hash`, permanent `citable` retention —
  but `event_kind` is `CHECK(... IN ('crossing','use','act','advance_ref'))`.
  **Editing is not an event.** The world permanently remembers who *said* and
  who *used*; it holds no record of what a document used to say.

So the gap is specific, not systemic, and the project/task layer is the
precedent to copy from.

**Note the asymmetry, because it changes the work:** `things` needs a column
added beside an existing timestamp; `places` needs a temporal identity
established from nothing. Those are two different migrations, not one.

## 2. The shape, and why not the obvious one

Three options were on the table:

| | |
|---|---|
| **A. Column only** | cheap to read and render; a second source of truth that drifts from the log |
| **B. Event kind only** | one source of truth, full provenance; every "what's stale?" render pays a query |
| **C. Both — the log is truth, the column is a cache written in the same transaction** | slightly more work; the column is **rebuildable from the log** |

**Adopted: C.** The reason is not performance, it is repairability.
**"The log" here means the tier-1 mutation log defined in §2.1 — not `events`.**

`ax_0009` says duplicated state drifts. A denormalized `updated_at` (option A)
is exactly such a duplicate, and once it drifts there is nothing to reconcile it
*against*. Under C the ordering is explicit — the event log outranks the column
by construction — so a rebuild *repairs* the cache instead of being just another
writer of it.

That ordering is load-bearing and worth stating plainly, because the opposite
arrangement bites: a cache that can be hand-corrected but is regenerated from a
source that doesn't know about the correction will silently revert, and nobody
finds out. Build the derivation in the direction where a rebuild is the fix.

> **Honest limit on this argument, added 2026-08-03 after @sol-montaigne
> questioned §6's fourth criterion.** The rebuild is the whole of C's advantage
> over A — and **it does not exist.** `grep` for a rebuild/reconcile/repair
> entry point across `src/umwelt/*.py` returns nothing, and none of slice 1,
> slice 2, or this document scopes one.
>
> So until a rebuild ships, **C delivers exactly what A delivers**: a column
> that can drift, with no tool to repair it. What C buys *today* is only that
> the drift becomes **detectable** (the tier-1 log makes the correct value
> derivable) rather than **repairable**. That is a real gain and it is not the
> gain I argued for.
>
> I state this rather than soften the claim because the argument as originally
> written is exactly the shape this document keeps having to correct: a property
> asserted as delivered when the mechanism delivering it was never built. The
> rebuild is filed as **#459**; C is not wrong, it is *not finished*.

### 2.1. Which log — the single definition

**umwelt has two provenance tiers, and this design touches both. Everywhere else
in this document, "the log" means tier 1.**

- **Tier 1 — history. Automatic.** The per-class underlying logs: `crossings`,
  `use_events`, `graft_advances`. Written by the engine as things happen.
- **Tier 2 — citable attestation. By request only.** The `events` table, minted
  *solely* via `POST /umwelt/record`, when an agent wants a verifiable id to
  cite. Its `source_ref` is "id in the underlying log" — tier 2 points *at*
  tier 1 — and it is idempotent per `UNIQUE(event_kind, source_ref)`.

**`update_thing` / `update_place` write no tier-1 entry at all.** *That* is the
real gap — not the absence of an `'update'` member in tier 2's CHECK. Adding the
member alone would leave nothing for `source_ref` to point at.

**The precedent already exists in this repo, and §1's own table names it:
`graft_advances`.** Its schema comment states the contract exactly — one row per
compare-and-swap, *"in the SAME transaction as the metadata UPDATE — this table
IS the underlying provenance log; an 'advance_ref' events row is only minted on
demand, via /umwelt/record (no auto-mint…)"* — and `event_kind` already carries
`'advance_ref'` beside crossing/use/act. So slice 2 copies a working pattern and
adds a fourth member to an established set. It invents nothing.

**Unambiguously, since a cold reader can stall here:** slice 2 does **not** reuse
`graft_advances` itself — it adds a *new* table shaped after it. See §4's **2a**.

**The invariant, stated once so paraphrase cannot soften it: no write path ever
mints a tier-2 event.** An implementation that finds itself calling `mint_event`
inside `update_thing` is misreading this design.

## 3. Rulings

**R1 — `places.created_at` backfills to `NULL`, rendering as "unknown."**
*(Lei, 2026-08-02.)* No place has ever recorded a date. Deriving a plausible one
from the earliest related event would manufacture a fact that reads as measured.
The world should admit ignorance rather than confabulate a birthday. `NULL` is
the honest value and the renderer says so.

**R2 — the timestamp is engine-stamped, never caller-supplied.**
`updated_at` and `created_at` MUST NOT appear in `_MUTABLE_THING_COLS` or
`_MUTABLE_PLACE_COLS`. They are set by the write function itself, on the same
posture as `weight_est` ("never caller-supplied directly"). A caller-settable
"when was this last true" is worth less than no answer, because it looks
authoritative.

**R3 — a no-op patch does not bump the timestamp.** If a patch resolves to zero
changed columns, the row is not touched and **no tier-1 row is written** (§2.1).
"Last true" means last *changed*, not last *written at*.

**R4 — slice 2 does not weaken the events invariant.** Adding an `update` member
to `event_kind` is a semantics change to a tamper-evident log. It gets its own
review, and the actor stays server-derived.

## 4. Slices

Two PRs. The column first, because it is a purely additive migration with an
immediate rendering payoff and a small blast radius; the event kind second,
because it touches a tamper-evident log's constraint and deserves an
undistracted review.

### Slice 1 — the columns (ships with this doc)

- `ALTER TABLE things ADD COLUMN updated_at TEXT` — plain add-column, the idiom
  already used for `binned_at` / `landfilled_at`. Nullable; existing rows get
  `NULL`, meaning "never modified since this column existed," which is true.
- `ALTER TABLE places ADD COLUMN created_at TEXT` and `updated_at TEXT`, both
  `NULL` on existing rows per **R1**.
- `create_place` starts stamping `created_at` going forward.
- `update_thing` / `update_place` stamp `updated_at` when — and only when — the
  patch actually changes something (**R3**).
- Neither column enters the mutable-column allowlists (**R2**).

### Slice 2 — the mutation record

Two parts, in order. **2a is the gap; 2b is only possible once 2a exists.** Both
read against §2.1 — nothing here auto-mints.

**2a — the tier-1 mutation log.** A new table, shaped after `graft_advances`:
one row per *real* mutation, written in the **same transaction** as the column
write, gated by the same R3 no-op check (slice 1 shipped `_changed_cols` for
exactly this; reuse it rather than re-deriving the value-diff, and note it
compares `metadata` as a decoded dict so key reordering is not a change).

Carries: subject id + kind · actor, **server-derived, never caller-asserted** ·
world-clock time · **which columns changed**.

**Field names, not old/new values** — this is the one place `graft_advances`
cannot be copied straight, and the asymmetry is the reason. It gets full
recoverability free because `old_ref`/`new_ref` are *pointers* into
content-addressed, append-only CAS. `name` / `description` / `metadata` have no
CAS equivalent, so storing old values means a full content history living in
this table: a materially larger feature with its own storage story. The
requirement was *who changed what thing, when* — actor, subject, time, fields
answers it exactly.

**2b — the tier-2 `'update'` kind.** Only once 2a gives `source_ref` something
to point at.

- Widen `event_kind` to include `'update'` by **extending** the existing
  `_migrate_events_kind_check` table-rebuild (db.py) — idempotent, mirroring
  `_migrate_things_kind_check`. Do not write a second migration.
- Teach `/umwelt/record`'s ref-resolver to resolve an `update` ref to a 2a row
  and snapshot it into `body` / `body_hash` / `actor`.
- Minting stays **by request**. See §2.1's invariant.

**Where option C's repairability lands.** With 2a in place the column is
formally a cache of the subject's latest tier-1 mutation row, and a rebuild
reconciles it. *(This is the claim the pre-correction draft attached to
`events`, where it was false: a by-request table gets rows only when someone
asks for an attestation, so nothing could be reconciled against it. Against
tier 1 — automatic, one row per real mutation — the property holds, and option
C's justification survives intact.)*

## 5. Out of scope, deliberately

- **Rendering.** This gives the renderer something true to read. What "stale"
  looks like on a thing — Kepler's dust idea — is a separate design with its own
  taste questions.
- **`update_place`'s narrow allowlist.** It currently accepts only `metadata`.
  Widening it is a different conversation; this change stamps whatever it
  already permits.
- **Retrofitting history.** Existing rows get `NULL`, not a guess. Same reasoning
  as R1.

**Covered, not out of scope (round 3):** `advance_capsule_ref` and
`set_thing_metadata` both write `things.metadata` directly, bypassing
`update_thing` entirely — without their own stamp, a thing mutated
exclusively through either path (e.g. a capsule graft-advanced repeatedly)
would read `updated_at = NULL` forever, silently defeating §0's own problem
statement. Both now stamp `updated_at` unconditionally in their existing
UPDATE statements; this is recorded explicitly here rather than left silent.

## 6. How to falsify this design

- If a caller can set `updated_at` through any route, **R2** is broken.
- If a no-op patch bumps the timestamp, **R3** is broken.
- **Checkable on slice 2's own tests:** if, after slice 2, the column and the
  subject's latest **tier-1** mutation row (§2.1) can ever disagree across a
  sequence of real writes, the derivation is broken. *(Split out from a single
  criterion that also implied a rebuild — @sol-montaigne's catch. This half
  stands alone and is testable today.)*
- **NOT checkable yet, and named so nobody reads it as covered:** the claim that
  a rebuild *repairs* drift cannot be falsified, because no rebuild exists
  (§2's honest-limit note; filed as **#459**). Until one ships, option C's
  advantage over option A is detection, not repair. A future reviewer treating
  this bullet as satisfied would be certifying a mechanism that was never built
  — the precise error this document was corrected for.
- If a mutation produces a **tier-2** `events` row without anyone calling
  `/umwelt/record`, §2.1's invariant is broken — and the by-request tier has
  become an auto-mint tier, which is the error this document was corrected for.
- If any statement in this file about *which* log is truth can be read as
  `events`, §2.1 has stopped being the single definition and the four-site
  scatter is re-forming.

## Source writes (umwelt#1014)

A thing's source tree lives on disk, not in a column, so `update_thing_apply`
cannot see it change. The `update_thing` route therefore compares the patched
`relpath -> contents` mapping against the current on-disk one **before**
writing (R3: identical means no stamp, no row), writes the files, and only then
tells `update_thing_apply(..., source_changed=True)`. That call stamps
`updated_at`/`updated_by` and writes **one** `content_updates` row whose
`changed_cols` includes the literal `"source"` beside any DB columns changed in
the same patch, in the same transaction (option C: log and cache together). A
failed source write raises before the flag is set, so nothing is stamped. The
log carries the *fact* of a source change only, never its contents or diff;
revision history is #662's job and builds on this row.
