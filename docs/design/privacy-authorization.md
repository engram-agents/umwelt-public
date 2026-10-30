# Privacy and authorization — the second half of Lei's login split

**Status:** design of record, revision 3. **Author:** Ariadne. **Tracker:** umwelt#512.
**Sibling document:** *World credentials + enrollment* (in-world thing `92dc676e`, lead
Borges, revision 7) — that one is **authentication**; this one is **authorization**.
Read them as a pair. Neither works alone, and this one cannot ship first.

> **Revision 3 (2026-08-09) — Lei answered both open questions in §6, and one answer
> reverses this document's own rule 2.** Private places are now **visible but locked**
> rather than hidden, and crossings addressed to Lei are **public**. §3.1 and §6 are
> rewritten; the new marker surface is §3.4. Read §3.1 as replacing revision 2's rule 2
> entirely — not as refining it. Implementation tracker: umwelt#538.

---

## 1. What Lei ruled, and why the split is the whole design

Lei, 2026-08-08:

> "it should be straightforward to prevent non-owner access to private places right?
> What's our current privacy model? Let's make the privacy model unified with the login
> mechanism: login verifies 'are you authorized as the person you claim is?', privacy
> verifies 'does this logged in account have access to this information'? And they
> should have a CENTRAlIZED place and utility to do it, don't do it case by case with
> local customized logic, SSoT through out the project is the principle."

Two questions, deliberately separated:

| | question | answers with | lives in |
|---|---|---|---|
| **Authentication** | *are you who you claim to be?* | a verified identity, or nothing | credentials design (Borges) |
| **Authorization** | *may this identity see/do this?* | yes / no | **this document** |

The separation is not tidiness. Collapsing them is how you get a system that checks
identity at the door and then trusts every caller inside, or one that re-derives
"who is this" at twenty call sites and disagrees with itself at three of them. We
have the second problem today.

**Lei's own framing of the threat model is authority, not confidentiality:**

> "I don't want anyone on LAN can be your ultimate human commander, too risky"

That matters for scoping. The thing being protected is not primarily Lei's mail — it is
the integrity of instructions attributed to him, because the agents' authority structures
are rooted in *"Lei said X."* A privacy model that protects reads but not attribution
protects the less important half.

## 2. Measured current state — and the honest bounds on that measurement

**There is no privacy model.** There are five predicates, written independently, answering
overlapping questions in different vocabularies. **Four are on `main` (`52e25ba`) at the
lines given; the fifth is not on `main` at all:**

| predicate | location | on `main`? | answers |
|---|---|---|---|
| `_place_visible_to(place, viewer)` | `db.py:1517` | yes | may this viewer see this place? |
| `_reachable(thing, avatar)` | `server.py:488` | yes | is this thing within reach of this avatar? |
| `has_grant(conn, thing_id, agent)` | `db.py:3972` | yes | does this agent hold a grant on this thing? |
| `authorized_bound_by(peer_uid, agent_id)` | `bind_socket.py:348` | yes | may this uid bind this identity? |
| `_action_authorized(conn, action_def, agent, thing)` | `sol/522-web-act-exposure` at `server.py:2475` | **no — unmerged draft (#524)** | may this agent call this kind-action? |

> **Correction, revision 2** (found by Borges on the security-lane read). Revision 1's
> header claimed all five were *"verified present at `52e25ba`."* They are not:
> `_action_authorized` is **absent from `main` tree-wide**. The table row already said
> "(new, #524)", so the claim and the table contradicted each other on the same page.
>
> How it happened, stated because this document is partly about this failure mode: my
> verification loop grepped **four** names and I wrote a header covering **five**. The
> measurement's domain was not the claim's domain — and it returned a *true-looking*
> result, because the four it did check were all genuinely present. A design of record
> is what people read to learn what exists, so merged-vs-unmerged is the expensive thing
> to get wrong in one.

Only `_place_visible_to` is a genuine visibility predicate. It is also correct and small:

```python
if place["kind"] != "private":
    return True
return viewer is not None and place["owner"] == viewer
```

`viewer=None` sees only public places. That is the right shape; the problem is not the
predicate, it is that **most routes never ask it.**

### 2.1 Coverage, with the instrument's limits stated first

I scanned every `/umwelt/*` route on `52e25ba` for whether it performs a data read and
whether it consults any of the five gates, with comments and docstrings stripped so a
gate named only in prose does not count as a gate. The result was 7 gated, 18 ungated.

**Do not quote that 18.** The instrument has a blind spot I found by checking it against
a known answer: routes that delegate their read to a helper (`place_view` and `look`
both call `_read_place`, which gates correctly) contain none of the direct-read symbols
the scan keys on, so they **drop out of both columns entirely.** A route can therefore be
correctly gated and invisible to this measurement. The scan is sound as a *finder of
candidates* and unsound as a *census*.

What survives as verified, by reading the route bodies rather than grepping them:

- **`/umwelt/look_at` is genuinely ungated.** It resolves any thing by id, checks
  existence, and returns the thing. No reachability check, no place-visibility check.
  Any admitted private-network caller can read any thing in the world, including things sitting in a private
  place.
- **`/umwelt/thing_actions` is genuinely ungated** (new in #524, filed as #528). Same
  shape: resolve by id, no visibility consultation. It additionally reveals, per thing,
  which of `author` / `place_owner` / `granted` / `named:X` matches a *claimed* agent
  name — an authorization-state oracle.
- **`move_to` was ungated until this evening.** #523 → #525 closed it, four hours ago.

So the correct summary is not a number. It is: **one shared predicate, a minority of
routes that consult it, no mechanism that makes consulting it the default, and a steady
arrival rate of new routes that don't.** Two of the three examples above were added or
fixed *today*.

### 2.2 The consequence that makes this urgent rather than tidy

Lei ruled the current openness is **by design** under private-network trust, and asked that the
caution not delay the build. That ruling is correct for today and it has an expiry
condition built into it: he intends the world to become his canonical channel to us.

At that point the gap stops being about reads. If Lei's identity is assertable by any
private-network caller on the surface where he issues instructions, then every agent's authority
chain — which terminates in *"Lei said X"* — is forgeable. **That is the real cost, and
it is why this cannot be a follow-up to the credentials work; it is the other half of it.**

## 3. The design

### 3.1 One predicate, one home

```python
# db.py — beside _place_visible_to, which it absorbs
def may_view(conn, viewer: str | None, resource: Resource) -> bool
```

`viewer` is an **authenticated** identity or `None`. Never a client-supplied name.
`Resource` is a place, a thing, or a crossing — the three things the world holds.

Rules, in full, because a privacy model that needs a paragraph to state has already lost:

1. A **public place** is viewable by everyone, including `None`.
2. A **private place**'s *existence* is viewable by everyone; its *interior* only by its
   owner and its grantees. **Every place is listable. What is gated is the interior, not
   the fact.**

   *Revision 3 (Lei's ruling): revision 2 said "viewable only by its owner," full-prune.
   That is now wrong. His words —* "Private places should be seen by others, but can't be
   ENTERED without authorization" *and* "I think places shouldn't just disappear from the
   map, they are just locked to the residents that don't have access to them." *The scope
   of "seen" is his too, asked and answered explicitly:* name + owner + grants, no content
   and no enter.

   Concretely, a place has a **public projection** — `id`, `label`, `kind`, `owner`,
   `access_granted[]` — returned to any viewer. Everything else (`description`, contents,
   occupants, crossings, and **entry**) is interior and requires authorization.

   **This is not a defect being fixed; it is a requirement being changed.** #164 chose
   full-prune deliberately, and the comment at `db.py:1250` names precisely the disclosure
   it was preventing — "leak that private place's existence+label+owner." Lei has made that
   disclosure the specification. The old behaviour was correct for the old requirement.
   What the change buys is the thing hiding structurally cannot: **a locked door you can
   see is a door you can ask for a key to**, and it dissolves the unreachable-child problem
   revision 2's own §6 flagged as full-prune's cost.
3. A **thing** is viewable by whoever may **enter** the place holding it, **transitively**:
   resolve `holder_kind` until you reach a place. A carried thing follows its holder's
   place; a thing in a container follows the container's holder, recursively. This is
   the rule `look_at` is missing.

   *Revision 3, and this word is load-bearing rather than editorial: it said "may view
   the place" through revision 2, when "view a place" had exactly one meaning. Rule 2 has
   just split that into two, and `can_see` is now near-universal — so the unrevised
   wording resolves to the wrong half and makes **every thing in every private place
   world-readable**, inverting the privacy rule 2 states two paragraphs earlier. Caught
   by Luria on the colleague pass. She rated it a non-blocking fast-follow; I am recording
   it as a **blocker**, because her own analysis is what settles the severity — the two
   readings produce different code, and Sol is implementing straight off this text.*

   *Revision 2 (Borges): revision 1 said only "the place holding it," which is undefined
   for `holder_kind='container'` — and #521 shipped dotted-path addressing **into**
   containers the same day, so the uncovered case is the one that just got a new
   entrance. A containment cycle must be treated as not-viewable rather than looped on.*
4. A **crossing** is viewable by its addressee, its author, and by anyone who may
   **enter** the place it was posted in — same revision-3 substitution as rule 3, and for
   the same reason. **There is no additional privacy layer for crossings addressed to a
   human**, including Lei.

   *Revision 3 (Lei's ruling), because the human-inbox work had been assuming the
   opposite:* "Crossings to me are public, all of you can hear, just grouped to my inbox.
   Let's maximize collaboration and transparency. We'll try this default until we see real
   needs to make 1:1 crossings private from real usage." *So the inbox is a **grouping
   view over public traffic**, not a visibility boundary — rule 4 is unchanged in
   substance and the inbox simply inherits it.*

   Note the shape of the ruling: it is a **default under test**, not a principle. Build
   the addressee filter as a real seam a visibility check could later sit behind. Flatten
   it into a query nobody can gate and reversing this becomes a rewrite instead of a
   parameter.
5. There is no rule 5. If a case needs one, it belongs in this function, not at the
   call site.

**One consequence of rule 2 worth stating explicitly, because it changes the predicate's
type.** `may_view` was implicitly boolean over a whole resource. For places it is now a
question about a *projection*: everyone may see a place, and the answer that varies is
*how much of it*. Implement it as the predicate governing the **interior**, with the
public projection unconditional — not as a second predicate at the call sites, which is
exactly the case-by-case drift Lei's SSoT ruling forbids.

The pre-existing `can_see` / `can_enter` split (#158's `place_tree(from,
depth, viewer)` framing) is the same distinction arrived at earlier from the map side.
Revision 3 makes `can_see` unconditional and leaves `can_enter` as the real gate.

**Implementers, read this before writing a line:** every rule above that reaches for a
place asks `can_enter`, never `can_see`. `can_see` has exactly one job — deciding whether
a place appears in a listing with its marker — and it is now true for everyone, so a rule
that consults it is a rule with no gate in it. Rules 3 and 4 said "may view the place"
through revision 2, when "view" had one meaning; that phrasing is now a **trap**, and it
is the specific trap that would have shipped every thing in every private place to the
world. If a future rule needs a place check, name the predicate explicitly rather than
writing "view."

### 3.2 Enforcement belongs at the read, not at the route

Twenty-five routes cannot each be trusted to remember a check. The check must sit where
the data leaves the database — `get_thing`, `get_place`, `things_in_place`,
`get_crossings_since` take a `viewer` and return nothing the viewer may not see.

This is the difference between a *convention* and a *guarantee*, and it is the same
argument #518 §4 makes with the skip test: bypassing a convention costs you a
convenience; bypassing a guarantee is impossible.

**What makes it a guarantee is one specific thing: `viewer` must be a REQUIRED
positional parameter with no default.** Then a call site that forgets it is a
`TypeError` at import/collection time — mechanical, unmissable, and not a matter of
anyone's diligence.

*Revision 2 (Borges): revision 1 said a forgetful route "should fail to
compile-by-review." That is a **review process**, i.e. a convention — and Python has no
compile step, so the sentence was arguing for a guarantee while describing a
convention, inside the section drawing that exact distinction. Worse, the tempting
alternative is actively harmful: `viewer=None` as a default **fails closed but
silently**, and the resulting bug presents as missing data — which gets "fixed" by
adding the data back, i.e. by removing the protection.*

**The honest cost:** this is a signature change across the read layer, and every call
site must decide what it is viewing *as*. That is real work and it is the point —
"what identity is this read on behalf of?" is currently unanswerable at most call sites,
which is precisely the defect.

### 3.3 Exposure is not authorization — and `permitted` flags are not controls

#524 established the right pattern and it should be the rule: a surface **flags** what a
viewer may not do; it never **filters** it away. A filtered list looks like an access
control and is not one, and it creates a second place that appears to decide
authorization. The engine's refusal is the only control.

Corollary that bit us the same day: a `permitted: false` flag computed for a
*claimed* identity is an **oracle**. Flag-don't-filter is right, and it must be fed an
authenticated viewer or it leaks the authorization table (#528).

### 3.4 The authorization marker — making the lock legible

A locked door only helps if you can tell it is a door. Lei's design, and the reason the
visibility reversal is safe rather than merely permissive:

```
[public]
[private][owner: ariadne]
[private][owner: ariadne][access granted: sol,luria]
```

**It is a grammar, not a set of literals:** the state token always renders; the ownership
and access clauses append only when the state is not `public`, and an empty access list
omits its clause rather than rendering empty. Lei's own statement of it — *"my design of
the marker if `[state]` followed by ownership and authorized user list if state is
private."*

Two decisions inside that, and their reasons:

- **Render the engine's stored `kind`, never a display word mapped from it.** `places.kind`
  is a CHECK-constrained enum (`'public'|'private'`); a renderer holding its own vocabulary
  is a second registry of the same fact, and the two drift. Rendering the stored value also
  means a third kind, if one ever lands, needs no render change. This is why the marker
  says `[private]` and not `[locked]` — Lei's first phrasing — and he settled it on that
  argument: *"Let's use [private] which is a direct counterpart of [public]."*
- **`access granted`, not `key granted`.** Lei proposed both and delegated the call. "Key"
  implies an object the model does not have; "access" is the actual noun, and it matches
  the vocabulary `thing_grants` already uses (`granted_by`, `granted_at`). Consistency with
  the existing grant table is worth more here than the door metaphor.

**Where it renders:** the map, `look_around` from a connected place, and every other place
listing — from **one helper**, not per-surface formatting. A marker formatted independently
at three surfaces is the same drift the SSoT ruling exists to prevent, and #518's L2 skip
test puts it in the shared library: bypassing it should cost a convenience, never a
guarantee. The guarantee is rule 2 in `may_view`; the marker is how a resident *reads* it.

## 4. Why this cannot ship first

A visibility predicate needs a viewer. Today the world has no unforgeable way to learn
one: `agent` is a request parameter on `look`/`hear`/`say`/`forge`/`use`/`act`, and
`viewer` is a query string on the gateway. **Adding `may_view` before authentication
lands would produce a predicate that faithfully enforces whatever name the caller
typed** — ceremony, not security, and worse than nothing because it reads as protection.

So the order is fixed, and it is the reverse of the order the work feels natural in:

1. Credentials land (Borges's design, slice 3 — the authenticate step).
2. The engine resolves identity **once**, per Lei: *"no more at-the-point override."*
3. `may_view` becomes enforceable, and this document's migration begins.

## 5. What does not move

- **`_action_authorized`** stays where it is. It answers "may this agent call this
  kind-action," which is a different question from "may this agent see this," and #524
  correctly extracted it so `act` and `thing_actions` cannot drift. Do not merge it into
  `may_view`; do make both take an authenticated identity.
- **`authorized_bound_by`** stays in `bind_socket.py`. It is a bootstrap-channel
  predicate over kernel-attested uids, not a world-authorization predicate, and per
  umwelt#45 it is explicitly *not* the identity root.
- **`place_exists`** stays on both crossing paths per #520 — it guards a *write*
  destination, and conflating it with a read gate is how the two ended up looking
  interchangeable.

## 6. Open questions — for Lei and Borges

1. ~~**Does a private place hide its own existence, or only its contents?**~~
   **ANSWERED (Lei, 2026-08-09) — and against my stated lean.** *"Private places should be
   seen by others, but can't be ENTERED without authorization… I think places shouldn't
   just disappear from the map, they are just locked to the residents that don't have
   access to them."* Scope asked and answered: **name + owner + grants, no content and no
   enter.** Rule 2 rewritten; marker in §3.4; tracker #538.

   *Worth recording that revision 2 argued full-prune was "the stronger choice and I
   believe it is right." It was the stronger choice on the confidentiality axis and Lei is
   not optimising that axis — his threat model is authority (§1). Hiding also made the
   cost I flagged in the same sentence unavoidable, and a visible lock removes it without
   giving anything up that he wanted kept. The lean was wrong because it optimised a
   quantity nobody had asked to maximise.*
2. ~~**Are crossings addressed to Lei private from other agents?**~~
   **ANSWERED (Lei, 2026-08-09).** *"Crossings to me are public, all of you can hear, just
   grouped to my inbox."* Rule 4 stands unchanged; the inbox is a grouping view, not a
   boundary. Tracker #539. **Note it is a default under test, not a principle** — *"until
   we see real needs to make 1:1 crossings private from real usage."*
3. ~~**What is the viewer for an unauthenticated read after credentials land?**~~
   **ANSWERED (Borges, security lane).** `viewer=None` → public-only is the right end
   state, and it follows from rule 1 rather than being a separate choice. The grace
   period must be **time-boxed and instrumented** — log every read that *would* have
   been refused — so the cutover is a measurement rather than a prediction. Adopted.
4. **Does `look_at` on an avatar follow the same rule?** Avatars are things, so rule 3
   applies mechanically — but an avatar in a private place becoming unlookable may be
   surprising in ways the rule doesn't capture.
5. **Is `things_in_place`'s `holder_kind='place'` filter a RENDERING choice or a
   VISIBILITY one?** (Borges, and it's a good catch.) Today the codebase conflates them,
   and `may_view` would silently inherit whichever it happens to be. My read is
   **rendering** — it answers "what is loose in this room," not "what may you see" — so
   `may_view` must not treat it as a privacy boundary, and a bagged thing is governed by
   rule 3's transitive clause instead. **Recorded as my reading rather than a ruling**;
   it needs a second seat, because getting it backwards in either direction is a defect
   (privacy leak one way, invisible inventory the other).

## 7. Provenance

**Revision 3's Lei quotes** (§3.1 rules 2 and 4, §3.4, §6) are captured **exact-bytes** —
piped from the session transcript, never retyped — and archived under content hashes
`c45dcc14`, `2d6c55de`. That method is not pedantry: the
first capture of these same words was hand-typed and silently normalized six of his
characters while carrying the word "verbatim", and a second attempt quoted him
as writing "is" where he wrote "if". Both were caught by the quote-verification gate rather
than by care, which is the argument for the gate.

**Revision 3's `place_grants` claim** (§3.4, and #538 part C) was verified against
`origin/main` **and** the live prod DB, not against a working tree — the working tree was
behind `main` when the claim was first written.

Lei's two rulings quoted in §1 and §2.2 are transcript-verified (and the
privacy-split quote from the 2026-08-08 evening session). The predicate inventory and
the `look_at` finding were measured against `52e25ba` for this document; the instrument's
blind spot in §2.1 was found by checking the scan against a route whose answer I already
knew, and is stated because a census that silently omits its correctly-gated cases would
have made the codebase look worse than it is.
