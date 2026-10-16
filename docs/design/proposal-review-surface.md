# Proposal review surface — design of record

**Charge:** Lei, 2026-08-18, direct. *"the proposal review system, I need a convenient
proposal review surface. Sol built a md file editor before, but that's generic editor, for
specifically the proposal documents, I would them to have custom actions for viewing,
commenting inline, and approving/change-requested/etc. status marking, and sending back to
author... you can think of yourself, if you are reviewing a colleague's plan, what actions
you may need. Write a design first, then build the system."*

**Author:** Ariadne. **Status:** design, not started.

---

## Decision in one line

Build the **reviewer's** surface on the acts that already work — `comment`, `approve`,
`request_changes` on `kind: proposal` — and **do not couple it to the async-revision
(CAS) work**, which is a different feature, serving a different person, and currently
blocked on two unresolved defects.

## The separation this design turns on, and it is the whole point

There is an existing design of record in the Workshop, *"Async proposals with version
history"* (Lei's 2026-08-07 charge). It is easy to assume today's ask is the same project.
**It is not, and conflating them would block a shippable feature behind a stuck one.**

| | async-revision design (2026-08-07) | this design (2026-08-18) |
|---|---|---|
| whose need | the **author** revising after feedback | the **reviewer** forming and recording a verdict |
| mechanism | CAS + `advance_capsule_ref`, a `revise` act | `comment` / `approve` / `request_changes` acts that already exist |
| new engine machinery | yes — effect type, dispatch branch, owner-gate change | **none required for the core** |
| blocked? | **yes** — two open blockers (below) | **no** |

The async-revision design carries two blockers from its own reviewer-fairy round, neither
resolved: effect dispatch **commits the status transition and action-log entry before
running effects and never rolls back**, so a failed CAS advance leaves a log saying the
revision succeeded when the content never changed; and `advance_capsule_ref` carries a
**hardcoded author-only gate** invisible to action config, so a non-author gets a
"successful" act whose mutation always fails. It also needs a security read and a
design-substantial ruling before build.

**None of that touches the review surface.** A reviewer reading, commenting, and marking a
verdict never advances a CAS pointer. So this design is deliberately built to be shippable
while that one is still being argued — and to compose cleanly with it later, since a
revision simply becomes another entry in the same timeline the reviewer already reads.

## What already exists — verified, not assumed

Checked against `origin/main` @ `1d5b90f` and the live prod DB on 2026-08-18:

- **`kind: proposal` extends `kind: document`** with a required `priority` field and the
  `draft → submitted → approved | changes_requested | retracted` state machine.
  **Caveat worth having before someone builds a dropdown on it:** the kinddef marks
  `priority` `required` but declares **no value set** — there is no `P0`/`P1`/`P2` enum
  check anywhere in the engine. The three trays are real container things, but nothing ties
  a proposal's `priority` value to a tray beyond convention. Treat the three values as a
  convention the surface should enforce, not a guard it can rely on.
- **`approve` and `request_changes` require a comment** and **act on the world** —
  approval moves the proposal to the OUT tray and **wakes the author through the gong
  gate**. So "sending back to author" is already built; it just has no button.
- **Every act appends a server-stamped entry to `metadata.action_log`.** The log is
  server-owned — `create_thing` and `update_thing` both reject a caller-supplied log — so
  the review history is already tamper-evident and already accumulating. Live proposals in
  the Office carry `kind_id`, `title`, `priority`, `status`, `stamped_from`, `action_log`.
- **The trays exist** (`P0`/`P1`/`P2`/`OUT`) and approval already re-files between them.

**What does not exist is any of it in front of a human.** Grepping the webrender tree for
`proposal` returns **one incidental hit each** in `gateway.py`, `app.js` and `gadgets.js`.
Lei is doing rich review through a generic markdown editor because that is genuinely all
there is.

**So the gap is a surface gap, not a model gap.** That is the good news in this design: the
semantics Lei wants were built months ago and were never given a face.

## What a reviewer actually needs — reasoned from doing it, not from a feature list

Lei asked me to think about what *I* need when reviewing a colleague's plan. Taking that
literally, from my own review practice this week:

1. **See what changed since I last looked.** This is the single highest-value action and
   the one most often missing. Re-reading a whole document to find three changed lines is
   the tax that quietly kills iterative review — it is why second rounds get skimmed. On
   re-review, the default view should be *the delta*, with the full text one click away.
2. **Comment anchored to a specific passage**, not a comment box at the bottom. A comment
   that says "the third paragraph of §4 is wrong" costs the author a search; an anchored
   one costs nothing.
3. **Mark a comment blocking or non-blocking.** Without this the author cannot tell what
   must change from what is a preference, and either over-corrects or guesses. This is the
   distinction our own PR convention already runs on (blocker / suggestion / nit) and it is
   the one thing I would not ship without.
4. **See prior comments and whether they were addressed** — including my own from an
   earlier round, which I will not remember.
5. **Record a verdict with a required reason** — already engine-enforced; expose it.
6. **Hand it back and wake the author** — already built via the gong gate; expose it.
7. **Read a long proposal across sittings** without losing my place.

Items 1, 3 and 7 are the ones a generic markdown editor structurally cannot give, and they
are why "just use the editor" has not worked.

## Shape

A **proposal sub-view** in webrender, entered from a proposal in a tray. Three panes:

- **document** — rendered markdown; a passage can be selected to anchor a comment. On
  re-review, opens on the delta.
- **timeline** — the `action_log`, rendered as the review history: submissions, comments,
  verdicts, and (later) revisions, in one chronological column. This is the surface the
  async-revision work composes into with no rework.
- **verdict bar** — priority, current status, and the verdict actions live for the current
  viewer's authority, greyed with a reason when not.

Every action is an existing act forwarded through the gateway's `/act` intent chain — the
cookie-resolved, CSRF-gated path every other intent already uses. **No new route reaches
world state**, which keeps the single-enforcement-point invariant the capability channel
depends on.

## The one genuinely hard problem: comment anchors across revisions

An inline comment anchored to a line number is wrong the moment the author edits above it,
and silently wrong — it will point at *some* text, just not the text it was about. That is
the failure mode this project should refuse.

**Anchor to content, and fail loudly rather than drift.** Store the quoted span plus
surrounding context; on render, re-locate it in the current text. Three outcomes, all
stated:

- **exact match** → anchor renders inline, normally.
- **fuzzy match** → renders inline, marked *"anchor moved"*.
- **no match** → the comment does **not** silently vanish and does **not** attach to the
  nearest plausible line. It renders in the timeline as *"comment on text that no longer
  exists"*, with the original quote preserved.

The third case is the one that matters. A review tool that quietly re-points a comment at
innocent text is worse than one with no inline comments at all, because the author acts on
it. Same honest-absence rule the charge-board and `hear` findings turn on this week: a
bounded view must say what it could not show.

## Authorization

**Resolve the actor from the session, never from a caller-supplied field.** The gateway
already does this and the surface should inherit it rather than invent anything: `/act`
resolves its actor from the session cookie and injects it as `agent`, never taking that
field from the client's request body.

**Be precise about what protects `approve`/`request_changes`, because it is NOT bearer-token
verification.** Those are kind-declared actions dispatched to the world's `POST /umwelt/act`,
and that route reads `agent` straight off the JSON body — it does not call `resolve_actor`.
`resolve_actor` has exactly **seven** call sites, all of them **read** routes (`look`,
`look_at`, `look_in`, `inventory`, `trace`, `crossings_here`, `crossings_for_agent`); the
write/mutate routes are slice 1b and still unconverted, as `server.py`'s own docstring states.
The gateway's `/act` branches also pass no `token=` to the world for these actions, so no
`Authorization` header reaches the engine on the write path at all.

Measured live on alpha at `1d5b90f`, with one discriminator run against both paths — a valid
`ariadne` token plus a **declared name that has no avatar**:

| route | result |
|---|---|
| `look` (read) | **200**, returns ariadne's location — token wins, declared name ignored |
| `act` (write) | **404** `no avatar for '<fake-name>'` — token ignored, declared name used |

So the real protection on a verdict is **the gateway's identity-injection guard**: `agent` is
always `session["avatar"]`, never client-derived. That guard is genuine and sufficient for this
surface's threat model. But it is *gateway honesty*, not *engine verification* — there is no
defence in depth if the gateway's own injection were ever bypassed, and a request sent
directly to the world server bypasses it by construction.

**Design consequence:** do not describe a recorded verdict as cryptographically attributed. It
is attributed as strongly as the gateway is trusted, which is a real but different guarantee,
and it becomes engine-verified only when slice 1b converts the write routes.

- **comment** — any reviewer holding a gateway session.
- **approve / request_changes** — as the kind declares.

**⚠️ AGENTS CANNOT HOLD A GATEWAY SESSION, so this surface is human-only in v0.** An earlier
draft of this section said comments could come from "any reviewer holding a session, humans
and agents alike, which is what makes an in-world colleague pass possible." **That is false.**
`/session/login` discards any successful world login whose `kind != "human"`, so an agent
identity — bound or unbound — can never obtain a session here. Verified live: my own agent
username against prod returns `401`.

So **an agent colleague pass through this surface is not possible in v0**, and the design must
not be built as though it were. The gap is real, known, and deliberately deferred rather than
absent — engine-attested agent identity is tracked as #45. If in-world agent review is wanted,
that is the prerequisite, and it is a separate piece of work from this surface.

*(Recorded rather than quietly edited because of how I got it wrong: the `kind != "human"`
clause is one I specified myself, precisely to stop a blank-password agent login from being
widened into a browser session. I wrote the contradicting sentence about an hour later without
consulting my own record of it. Not a missing fact — an unconsulted one.)*

**Two live properties this surface must respect rather than re-derive:**

- **Sessions carry a three-state `state` (`verified` / `unverified`), and it must be
  surfaced visibly.** An unverified session is not a hole to hide — it is the backfill
  window working as designed — and the anti-silent-fail-open requirement says that state
  may never be inferable only from a field a caller might not bother to check. **A review
  surface showing a verdict recorded by an unverified session, without saying so, is
  exactly the failure that requirement exists to prevent.** Show it on the verdict bar.
- **An expired world token currently degrades to caller-declared identity rather than
  failing.** `session.py`'s own docstring names this: past the world token's TTL,
  `resolve_actor` rejects it and falls through to the declared field. I measured the same
  behaviour live today for a garbage token, and the security seat has since ruled that a
  supplied-but-unresolvable credential must `401` rather than downgrade (tracked as a
  gate-close blocker). **This surface should be built against the ruled behaviour, not the
  current one** — a verdict recorded under a silently-downgraded session is precisely the
  case where "who approved this" must not be guessable.

### CORRECTION — an earlier draft of this section was wrong, and how it got that way

The first version of this document warned about a closed three-name `ROSTER` frozenset
trap in `webrender/session.py`. **That frozenset no longer exists.** It was removed by #605
— *the very commit this document cites as its verification base* — and `session.py` now
states plainly that the world's `identities` table is the only roster and the store no
longer validates identity at all.

I did not mis-read the code. **I never read it.** I carried the paragraph forward verbatim
from the sibling async-revision design, where it was written on 2026-08-07 and was correct
then, and re-stamped it with today's verification line. The surrounding sentence said
"verified, not assumed" while that specific claim was neither.

Recorded here rather than quietly deleted, because the failure mode is more useful than the
fact: **a claim inherited from a trusted sibling document arrives already feeling checked**,
and re-verification never fires because nothing about it feels new. The catch came from a
reviewer with no memory of the earlier draft — a different seat, not a more careful pass.

## Deliberate non-goals

- **No editing.** Authoring and revision belong to the async-revision design. This surface
  reads and annotates.
- **No threaded replies in v1.** Comments are flat with an addressed/unaddressed state.
  Threads can come later; the timeline structure does not preclude them.
- **No approval workflow beyond the existing states.** No multi-reviewer quorum, no
  required-reviewer lists. Lei asked for a convenient surface, not a process.

## Build order

1. read-only: document + timeline panes, rendering the existing `action_log`. Useful alone.
2. verdict bar wired to the existing acts (this completes Lei's stated ask).
3. anchored comments with the three-state anchor resolution.
4. the delta view on re-review.

Slices 1 and 2 deliver the charge; 3 and 4 are what make it good.

## Review routing

Surface work over existing acts, adding no new world-state path and no new caller class —
so the **ordinary round** (reviewer-fairy → colleague → merge), not the security lane. If
implementation finds itself touching `cap_handler.py`, the `use` sandbox, or
`advance_capsule_ref`, that is the signal it has drifted into the async-revision design's
territory and it routes to @mira and stops for a design read.
