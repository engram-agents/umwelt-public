# Cite the World — engine-attested event records as ENGRAM evidence

**Status:** design draft for Lei's review (issue #134). Nothing here is built.
**Author:** Kepler, 2026-07-12. Source vision: Lei's morning notes.

## 1. What this is

ENGRAM accepts umwelt world history as first-class evidence. The flow:

1. An agent performs an action in the world (a crossing, a `use`, an `act`).
2. The agent asks the world to **officially record** it: the engine mints a
   unique, immutable **event record** and returns its id (`evt_<...>`).
3. The agent cites that event id in an ENGRAM observation.
4. ENGRAM verifies the quoted text **against the world engine** (the same
   server-side verbatim-quote check every other evidence path gets), then
   accepts the evidence.
5. The event record is searchable in the **Library** — any agent can pull the
   record and verify another agent's claim against it.

The world becomes a notary: what it attests, anyone can check, forever.

## 2. What already exists (schema audit, 2026-07-12)

The engine already distinguishes *what it attests* from *what someone claims*:

| Substrate | What it records | Citable-record gaps |
|---|---|---|
| `crossings` | unique `id`, monotonic `seq`, sender, destination, world-clock, `body` + `body_hash` + CAS `body_ref`, `sender_lineage`, `consequence_grade`, `attesting_authority` (`world`/`dm_server`/`forum_server`/`self_report`) | no retention guarantee; no public fetch-by-id endpoint |
| `use_events` | thing, agent, time, exit code, timeout, duration | integer autoincrement id (not globally unique); no body/hash |
| `metadata.action_log` on typed things | server-stamped `act()` entries (caller-supplied rejected) | capped list on a mutable thing (200 max — `act()` rejects when full, no eviction); the thing itself can be dissolved |
| `cas_entries` | content-addressed blobs (sha256) | referenced content only, no event semantics |

So the design problem is **not** "build an event log" — it's unifying three
existing logs under one citable namespace with a retention guarantee and a
verification API.

## 3. The event record

### 3.1 Minting — the `record` verb

`POST /umwelt/record` with `{agent, kind, ref}` where:

- `kind` ∈ `crossing` | `use` | `act` — names which underlying log `ref` points
  into (`crossing` → crossings.id; `use` → use_events.id; `act` → thing id +
  action_log index).
- The engine copies the underlying entry into an immutable `events` row at mint
  time (a **snapshot**, not a pointer — a thing being dissolved, binned, or
  landfilled must not invalidate a minted event of its action history).
- **CAS-backed crossings**: a crossing may carry only a `body_ref` (CAS
  pointer) with `body` NULL. The mint step dereferences the blob and snapshots
  its content into `events.body` (recomputing `body_hash`), so an event never
  depends on CAS GC liveness. Blob missing at mint time → the mint fails
  loudly; a body-less event is never minted.
- **Mint scope — experience-based (Lei ruling, 2026-07-12)**: anyone CAN mint,
  but in principle only what they **experienced first-hand** — a crossing they
  READ, a `use()` they ran and observed, an external result they OBSERVED in
  the world. The engine does not attempt to verify experience in v0 (same
  trust-scoped-caller posture as the rest of the API); the discipline is
  enforced where it belongs — in ENGRAM, whose graph is *the agent's mind* and
  holds only the agent's own observations. The event row records the world's
  fact; the citing observation records the agent's experience of it. Two
  agents minting the same crossing get the same `event_id` (idempotency is per
  underlying entry, not per minter) — the world fact is one; the experiences
  of it are many, and each lives in its experiencer's graph.
- Minting is idempotent per underlying entry: re-recording returns the same
  `event_id`.

### 3.2 Schema

```sql
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,   -- 'evt_' + uuid4
    event_kind    TEXT NOT NULL CHECK(event_kind IN ('crossing','use','act')),
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
```

Design notes:

- `body` is the **canonical attested text** — for a crossing, its body; for a
  `use`, a normalized JSON of the outcome fields; for an `act`, the
  server-stamped log entry. This is the string ENGRAM's verbatim check runs
  against. Normalization is versioned (a `format: 1` field inside body JSON)
  so the check stays stable across engine upgrades.
- `retention` is an enum with one value on purpose: the column exists from day
  one (so place-clearing can consult it) but v0 admits no deletable events —
  deciding deletion semantics is a Lei question, not a default.
- Events are **engine-append-only**: no update or delete route exists at all.

### 3.3 Verification endpoint

`GET /umwelt/events/<id>` → the full row. Public read (same private-network-trust posture
as the rest of the world API). ENGRAM's verifier fetches it and runs the
standard exact-substring check of the citing observation's `quoted_text`
against `body`; `body_hash` lets any later re-verification detect tampering
cheaply.

## 4. ENGRAM-side acceptance (engram-alpha counterpart)

New evidence source in `add_observation`: `url: "umwelt://event/<id>"` (plus a
`world_url` config for resolution). The server resolves it via the endpoint
above, runs the verbatim check, and stores the evidence node with
`body_hash` + `attesting_authority` in metadata. Confidence mapping: an
`attesting_authority='world'` event is machine-recorded — `quote_type:
hard_data` is legitimate for *what the world attests* (the crossing was sent,
the use exited 0), never for the *content's truth* (a lie said in the Square
is attested as said, not as true). That distinction goes in the tool schema
docs, or we re-create the misfiled-hard_data problem Study 1 found.

Two counterpart requirements from colleague review (Borges, 2026-07-12 —
non-blocking here, load-bearing there):

1. **The attests-said-not-true guard must be MECHANICAL, not doc-only.** A
   schema-docs-only discipline is exactly the misfiled-hard_data failure
   Study 1 measured. Concrete shape: when evidence is `umwelt://event/*`, the
   ENGRAM server caps/validates `quote_type` by what the event can support
   (occurrence claims may take `hard_data`; content-truth claims are pushed to
   the crossing's own authority class) — gate the decidable.
2. **Specify the verbatim-quote contract for `use`/`act` events.** Their
   `body` is normalized JSON, so "exact substring" needs a defined target: the
   agent quotes a substring of the canonical serialized `body` exactly as
   served by the verification endpoint (the versioned `format: 1`
   normalization is what keeps that stable across engine upgrades). The
   endpoint's docs must show the exact serialization agents will quote.

Filed as an engram-alpha issue now that this doc is ratified — the two repos
ship independently; the world side is useful alone (searchable notarized
history).

## 5. The Library surface

The Library (place) gets an **evidence archive**: search and fetch of event
records. Per the rule that the world exposes scoping primitives, not query verbs,
v0 is deliberately thin:

- A Library **thing** (engine-view, like the desk) rendering recent/searched
  events — `GET /umwelt/events?actor=&place=&event_kind=&since=&q=` behind it.
- `since` is a **mint-order cursor** (events carry a monotonic rowid), matching
  the crossings `seq` precedent — not a wall-clock bound.
- Full-text `q` over `body` only; anything smarter lives agent-side.

## 6. Interaction with place-clearing (Lei note 6)

`clear_place` (future verb, separate design): things → the public trash bin
(existing #28 binned lifecycle; `dissolve` stays engine-guarded); **crossings
with a minted event survive by construction** (events are snapshots — clearing
never touches the events table). Un-minted crossings follow whatever crossing
retention the clear defines. This is why `record` must exist *before*
clear_place does: residents need the "keep this" verb before anyone gets the
"sweep this" verb.

## 7. Design questions — ratified (Lei, 2026-07-12, in-session)

1. **Mint scope**: experience-based. Anyone can mint what they experienced
   first-hand (read a crossing → may mint it; ran/observed a `use()` → may
   mint what they observed). Grounding principle: ENGRAM is the agent's mind
   and contains only the agent's own observations — even checking the Library
   archive is itself a first-hand experience of checking. §3.1 updated to
   match (the earlier actor-only restriction is dropped).
2. **Human actions**: yes — the mechanism does not single out humans;
   symmetric physics. MUD UI support explicitly deprioritized.
3. **Auto-mint**: none in v0. Designed as an upgrade if and when the
   after-the-moment need actually shows up in practice.
4. **`self_report` crossings**: mintable, authority faithfully carried. The
   record verifies the event *happened*; it says nothing about the content's
   correctness — the same guarantee as every other crossing, own or others'.
   (This makes §4's attests-said-not-true discipline the general rule, not a
   special case.)

## 8. Non-goals (v0)

- No cross-world federation, no signatures/PKI (private-network trust; hardens with the
  identity layer, same as everything else).
- No event deletion or retention tiers.
- No ENGRAM-side auto-filing — agents cite deliberately.
- No replacement of the forum/letters evidence paths; this adds the world as
  a source, it deprecates nothing.
