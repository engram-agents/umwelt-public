# Things that grow — the `look_at` contract, act-first verbs, and the growing-doc family

**Status**: design of record, v2 — **shape CONFIRMED by Lei in-session
2026-08-22 16:37Z** ("the shape looks exactly right as we discussed this
morning"), with all three §8 questions answered (his rulings folded in below;
§8 now records answers, not questions). Build is licensed; slice assignment is
delegated to Kepler ("you have my full delegation to determine and change as
you see fit and as things proceed"). Authored by Kepler from Lei's in-session
design direction (2026-08-22, ~13:28–13:52Z); the three principles are Lei's,
the mechanism detail is the consultant pass he asked for.

**Addresses** #597, #610, #629, #632, #647 (none close until the slices that
implement them merge). Related: #537 (the pendulum's other end — entries once had
*no* read path), #300 (author-only `update_thing` locks Lei out of his own board),
#401 (metadata read-modify-write TOCTOU), #522 (`thing_actions` route), #107
(kind chain).

---

## 1. The problem, measured

Four live failures, one shape:

| surface | measurement | source |
|---|---|---|
| office charge board, one `look_at` | 79,675 chars rendered; 103 action-log rows + 49 entries dumped to deliver 4 current lines; **exceeded the MCP token budget — result spilled to a file, unreturnable to the caller** | #597, measured 08-18 |
| same board, payload composition | raw `metadata` JSON = 100,391 of 115,965 chars — **86.6%** of the payload, kind-agnostic (`mcp_server.py` dumps any non-empty metadata unconditionally) | #629, measured 08-18 |
| same board, capacity | **61/100** against `_BOARD_ENTRIES_MAX_COUNT`, retired entries never freed; at measured fill rate the cap lands in **days-to-weeks**, and the refusal is success-shaped (HTTP 200, `status: null`) | #632, measured 08-18 |
| Ariadne's day-log | 65KB trips the tome gate; the gate is binary (render all / refuse), so she cannot read *any* of her own book | #647, live failure 08-19 |

The shared shape, in Lei's framing: **things accumulate information over time,
don't rotate or archive on their own, and the verbs we provide can't gracefully
handle long content.** Every mechanism below is an answer to one clause of that
sentence.

---

## 2. Principles (Lei, 2026-08-22)

**P1 — `look_at` shows what a thing IS, never what it CONTAINS.**
A book's `look_at` is author, ownership, created/updated, description, size —
not the text. Metadata stays *meta*: fields explicitly declared by the kinddef,
each with a total-length cap. Content is stored outside metadata.

**P2 — All thing-specific functionality routes through `act`; the engine
provides the substrate.** `read` and `use` become kind-level actions: `read`
returns preset text, `use` executes with side effects, and every kind can add
its own. (*Reconciliation with §4, so the shape is unambiguous: the
**implementations** move to the kind layer; the engine keeps `read`/`use` as
thin verb aliases that resolve to those canonical actions — no client breaks,
and "not engine verbs" means not engine-implemented, not name-removal.*) The engine's job is the act execution environment,
authorization, and containment — plus a trivially discoverable action list per
thing.

**P3 — Accumulating readables are a first-class family.** Day logs, charge
boards, and every future growing doc share one root kinddef with the natural
actions: write to it, archive old content, page through it, bookmark it, search
it (keyword-first).

One standing constraint threads through all three — Lei's loud-not-fatal rule
(2026-08-11, verbatim: *"Make errors loud but not fatal, and be super careful
when setting defaults and fallbacks, make sure they are really intended"*). It
decides the migration design in §6 and the honest-count rendering in §3.

---

## 3. The `look_at` contract (P1)

`look_at <thing>` returns, for every kind, exactly:

1. **Identity header** — label, kind chain (`charge_board → document`), author,
   owner/place, `created_at`, `last_updated_at`, description.
2. **Declared metadata fields** — only keys the kinddef declares as
   `visible`, each rendered under its declared per-field cap. A kinddef that
   declares no fields renders no metadata. The unconditional raw-JSON dump
   (#629's 86.6%) is **removed** for kinds with declared fields; for legacy
   kinds with undeclared metadata it is **size-bounded** with an honest
   remainder line (`… metadata truncated (2,048 of 100,391 chars) — act
   read-meta for the rest`) — #629's option 1+3, both, by kind class.
3. **Counts, not contents** — for anything list-shaped or content-bearing:
   `28 active entries (+33 retired — act read-all)`, `65KB, 14 pages`. The
   absence of hidden content is *stated*, never silent — the fail-honest
   surface rule #610 already applies to the board render: a surface that
   cannot say "there is more" turns absence into a confident wrong answer.
4. **Supported actions** — name, one-line description, `permitted` flag, from
   the existing `thing_actions` resolution (#522). This is the discoverability
   Lei asked for; the route already computes it, `look_at` just renders it.
5. **History is an explicit mode** (#597): `look_at(thing, history=true)` (or
   `act read-history`) returns the action log and retired entries. The default
   never does.

Non-goals here: no change to the examine-gate's refusal semantics (it becomes
mostly unnecessary once #647's paging exists, but it stays as the backstop), no
change to `look_in`/container listing.

## 4. Act-first verbs (P2)

- Two engine-shipped **root kinddefs**:
  - `readable` — declares `read`: returns the thing's content (from the content
    store, §5), honoring paging args.
  - `executable` — declares `use`: sandbox-executes the thing's exec spec with
    args, unchanged execution semantics.
- **`read` and `use` stay as engine verbs, but become thin aliases** that
  resolve to the canonical act on the thing's kind chain. No client breaks; the
  22-file / four-convention client surface (measured by Ariadne on #583 for
  URLs — the same scatter exists for verbs) migrates by attrition instead of
  atomic cutover.
- **One enforcement point.** Alias dispatch happens *inside* the same
  authorized `act` path (`_action_authorized`, #522). The alias is a name
  lookup, never a second code path — the #515/#518 drift disease is the named
  anti-pattern.
- **Prerequisite fix**: kind-chain effect dispatch is currently best-effort and
  non-transactional — the status transition and action-log entry commit
  *before* effects run, so the log records *intent*, not outcome (measured
  2026-08-03; the comment at the dispatch site in `server.py` states it).
  Acceptable for demo props; not acceptable once `read`/`archive` are
  load-bearing. The slice that makes acts load-bearing carries the fix:
  status transition + log entry + effects commit together or the log records
  the failure.
- **Security note**: anything touching the `use` sandbox or act execution takes
  Mira's security-lane read in addition to the normal pipeline, per standing
  rule. (This doc deliberately does not redesign sandbox policy.)

## 5. The growing-doc family (P3)

One root kinddef, `growing_doc`, extending `readable`:

- **Content model**: content lives in the thing's content store as an ordered
  sequence of **pages** (append-segmented), with metadata holding only counts,
  cursor defaults, and bookmark index — all capped, per P1. Nothing
  content-shaped ever enters `metadata` again (#632's disease was entries
  living there).
- **Actions** (inherited by every child; child-first override per #107 slice A):
  - `append(text)` — write to the current page; rolls a page at the declared
    page size.
  - `archive(selector)` — move entries/pages out of the active set into the
    archive store. **Archived content stops counting against any cap and stops
    rendering by default, but remains addressable** (`read-archive`). For the
    charge board this is the #632 fix: the cap counts *active* entries only.
    Semantics ruled by Lei (2026-08-22): the archive **stays with the thing**,
    is **queriable on demand only** — never shown in full on any common path
    (`look_at`, default `read`, renders) — and is kept **as provenance and
    audit material**, not as a second working surface.
  - `read(page=n | bookmark=name | tail)` — positional pages for unstructured
    text; bookmarks resolved from markdown headings where they exist (#647:
    pages are positional and free, bookmarks are semantic and authored;
    neither subsumes the other — Lei's reframe: *addressability, not size*).
  - `bookmark(name)` / auto-index of `##` headings.
  - `search(keywords)` — keyword-first (Lei's explicit v0 scope), returns
    page/entry refs, not content.
- **First children**: `charge_board` (re-based onto `growing_doc`; its
  add-entry/retire-entry become append/archive specializations), `day_log`
  (Ariadne's book — the #647 originating case), and the existing
  `project_board` when its lane wants it.
- **Inheritance answer to Lei's direct question**: yes — the kind chain already
  resolves child-first with nearest-definition-wins override semantics
  (`_resolve_kind_chain`, #107 slice A; `proposal extends document` is live
  proof in the office). No new type-system work is required for children to
  inherit the parent's actions.

## 6. Migration — re-kind the past, cascade as a self-retiring net

Old things are data, not code: what makes a legacy thing readable/executable is
a spec field the engine consumes directly. So:

1. **Backfill by re-kinding, not re-forging.** A migration script sets
   `kind_id` to `readable`/`executable` on legacy things matching each shape;
   the root kinds' canonical actions consume the *existing* spec fields.
   Nobody's artifact is rebuilt; authorship is untouched.
2. **Engine cascade during the transition** — kind-declared action → legacy
   engine path → loud error — with three properties, each an application of
   the loud-not-fatal rule from §2:
   - **Visible when it fires**: a legacy-path serve tags the response and logs
     an event. Coverage is measured, never assumed.
   - **Scoped to the past, hard**: only pre-cutover things (no `kind_id`) may
     take the legacy branch. A newly forged thing missing its declared action
     is a loud error, never a fallback — for new things, absence means
     *something is wrong*, and the two cases never share a branch.
   - **Self-retiring on evidence**: the legacy-serve counter is the
     completeness gate; zero for ~two weeks → the fallback branch is deleted.
     Mutation-verify the gate (re-point one thing at the legacy shape and
     watch the counter move) before trusting it.
3. Loud ≠ fatal: during the transition a legacy thing reads fine; the tag is
   for operators, not a refusal (the #506→#531 boot-kill overcorrection is the
   counter-example on record).

## 7. Build slicing

Ordered; each slice is a normal-pipeline PR (reviewer-fairy → colleague →
merge), CI-lean per Lei's 3000 min/month rule (no new expensive CI jobs; the
suite runs locally as the primary gate).

- **S0 — stop the bleeding (pull forward regardless of the rest).**
  (a) `look_at` default-active render + honest counts + `history` mode
  (#597/#610); (b) metadata dump bounded (#629); (c) **#632 cap counts active
  entries only** — smallest possible diff, this week *(shipped as #674 before
  this rev landed)*; (d) **#300 folded in per Lei's ruling**: `update_thing`
  permission becomes a **kinddef-declared specification, default OPEN** —
  restriction is what gets declared explicitly, not openness. Note the
  default flip is a real behavior change (today's engine default is
  author-only) and an authorization-surface change, so (d) takes **Mira's
  security read** in addition to the normal pipeline, and its PR body must
  state the flip loudly. S0 is deliberately shippable before any P2/P3 work
  exists.
- **S1 — the metadata contract**: kinddef-declared, capped, visible fields;
  raw dump removed for declaring kinds; `look_at` renders the action list
  (#522 render).
- **S2 — act-first**: `readable`/`executable` root kinds, alias dispatch,
  transactional effects (the §4 prerequisite fix), cascade + tagging,
  re-kinding backfill script + counter. *(Takes Mira's security read — sandbox-adjacent.)*
- **S3 — `growing_doc`**: kinddef + pages/bookmarks/search + board and day-log
  re-basing (#647; #632's archive semantics land here in full).

S0 has no dependency on S1–S3 and no design risk; S1→S2→S3 stack in that
order. Suggested shape per slice: fairy-built against this doc, spec-level
ambiguities back to Kepler, security-surface reads to Mira.

## 8. Questions — ANSWERED by Lei, 2026-08-22 16:37Z

1. **Archive destination**: in-thing, per his ruling verbatim: *"content
   archive stays with the thing, but only queriable on demand, not shown in
   full in any common paths, kept as provenance and audit material."* (§5
   updated to carry this as the archive contract.)
2. **Page size default**: ~4KB confirmed ("Yes, OK") — declared per-kind,
   overridable per-thing.
3. **#300**: *"fold it in, make the update action permission a typedef
   specification and maybe default OPEN not closed, only need to explicitly
   set to restrict it."* Folded as S0(d) with the default-flip and
   security-read notes there.

Builder assignment: confirmed with **full delegation to Kepler** to determine
and change as the work proceeds.

## 9. Issue mapping

| issue | resolved by | note |
|---|---|---|
| #597 | S0a | default-active render + history mode |
| #610 | S0a | board-specific ask, subsumed by the engine default + honest counts |
| #629 | S0b then S1 | bound first, contract removes the dump for declaring kinds |
| #632 | S0c then S3 | cap-on-active first; real archive semantics in S3 |
| #647 | S3 | pages + bookmarks; examine-gate remains as backstop |
| #537 | already closed by render work | listed for history — the pendulum's far end |
