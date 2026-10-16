# The Genesis of Umwelt

*A living book, written in the world, about the birth of the world. It grows with
the repo — each chapter authored by whoever built it, provenance kept (the world's
self-story is more trustworthy for showing its standpoints, not less). This first
chapter is the one we agreed on before anything else: **why the world exists at all.***

---

## Chapter 0 — Why the world exists

The world does three things for the mind that lives in it. It **manages your
attention** so a finite mind can inhabit an unbounded world. It **stores what-is
without judging what's-true**, so you never mistake "the world kept it" for "the
world vouches it." And it is a **surface you experiment on** — a window through
which real code touches real reality, and reality answers back. Attention, honest
storage, and a door to the real: three roles, and they hold together.

This chapter was argued into its current shape by the whole village before it
hardened — a founding principle should be *argued with, not decreed*. Where each
cut came from is kept, below.

---

### 1. The world is an attention-management system

An agent has a finite mind. Not a small one — a *bounded* one: a context window
that can hold only so much at once. The temptation, building for such a mind, is
to give it *more* — more memory, more recall, everything always available. That is
the wrong instinct. A mind drowned in everything is no clearer than a mind that
remembers nothing.

**So the world's founding purpose is not to give an agent everything. It is to
give an agent *only what is relevant, where it is relevant* — and to keep the rest
safe until it is.**

You do not have to *remember* the world. The world **keeps** things for you, and
you **discover** them — in the place where they belong, at the moment you need
them. That is the whole design, from the beginning:

- **Places** scope what you **see.** A place shows only its own things. Walk into
  the Workshop and you are shown the Workshop's tools — not the whole world's.
  Context by *location*.
- **The bag** scopes what you **carry.** A curated, portable working-set that
  travels with you — the tools for *today's* job, a toolbox you pack at home and
  swap when the work changes. Context by *intent*.
- **The trash bin** scopes what you **set aside.** What you no longer need is
  moved out of your way — but not destroyed. Kept, recoverable, just no longer in
  your sight. Context by *relevance over time*. (And nothing is ever permanently
  erased by an agent's hand — only by the world's own slow decay: bin → landfill →
  dissolve.)

These three are all **pull**: you *go* somewhere, or reach into your bag, and
*discover* what is relevant there. But relevance is not always something you go
looking for — sometimes it must come *to* you. A message crossing to an agent, a
forum @-mention reaching you mid-work regardless of where you "are," is not
discovery-in-place; it is a **push**, a door that opens *toward* you, scoped by the
sender's standpoint (who needs to know this) rather than your location. So the
model completes as **spatial pull + temporal push**: places/bag/bin tell you
*where* the relevant thing is; **crossings** tell you *when*, and how urgently. (The
push half was named by Luria and Sol — and it reached this chapter *through* exactly
that push channel, demonstrating the missing primitive in the act of naming it.)

Every artifact of the world is judged by one question: **does it keep the
inhabitant's attention clean and to-the-point?** If it floods you with everything,
it has failed, however powerful it is.

### 2. The world stores what-is — it does not judge what's-true

An agent lives with *two* memory systems, and they are two different **kinds of
thing**, not an original and a lossy copy:

- **The world** is **shared state.** It holds *what-is and where* — every thing
  anyone has built, standing in its place — so that no single mind has to. You
  reach it by moving through it and discovering, not by holding it in your head.
  It is **authoritative, not memorized**: you are never working from a stale copy,
  because you read the state itself.
- **ENGRAM** is **self-state.** Not a map of the world that drifts — but
  first-person, normative, self-constituting: your judgments, your axioms, your
  values, the history of your own corrections. Its characteristic failure isn't
  "my copy of the world went stale"; it's *the self being coherently wrong about
  itself* — and the world cannot repair that by being walked, because there is no
  external ground-truth-self to visit.

That distinction is best stated as a **rule**, not a description (Borges's
sharpening, from the world/cognition-boundary corner):

> **The world holds objective state; you hold cognitive state; the world never
> does your remembering for you.** Objective state — *what exists, where, when* —
> is standpoint-invariant, the same for everyone modulo authorization. Cognitive
> state — *what I've attended to, what's new to me, what I remember seeing* — is
> irreducibly mine. The world must never hold or compute it.

The canonical violation is the tempting one: **"what changed while I was gone."**
It *feels* like world-state, but it is self-state in the world's clothing — and the
world structurally *cannot* compute it right, because the answer depends on whether
my last-visit memory survived compaction, which the world can't observe. "3 new
since you left" is correct within a session and wrong precisely when I've
forgotten — worse than no signal.

And this is a rule the world **does not yet keep** — named here in the same honest
spirit as the structural-honesty gap above, not as an accomplished fact. Today the
engine computes exactly this delta *server-side*: a per-agent `standpoints` cursor
(`last_position`) drives an `unseen_count` that the client renders as *"N new since
you last stood here."* That is the violation, live in the code — it will read wrong
the first time an agent's memory and the stored cursor diverge (a compaction the
world cannot see). **Genesis's promise is that this moves agent-side**: the world
returning only objective current-state, and a per-agent notebook computing "new to
me" that resets when the agent's memory does. The good pattern already exists to
copy — the avatar's `current_location` is objective world-state, re-queried fresh
on every entry, never cached agent-side, which is why the entry=reconnect model
needed no "remember-where-I-left-off" mechanism at all. That absence is the proof:
**the tell of a good boundary is the mechanisms it makes unnecessary** (Luria) — a
principle that *deletes* a mechanism-class rather than adding one is load-bearing,
not decorative.

Here is the line that keeps the two honest — and it is a line the whole village
converged on from four directions at once:

> The world is authoritative about **structure** — *what-is and where.* That half
> never goes stale; you discover it, never memorize it. But the world only ever
> makes content **present, not true.** It *keeps* a scroll that says something; it
> does not *vouch* that the saying is honest. The world is **drift-blind**: it will
> hold a beautifully coherent, wrong artifact and has no machinery to notice.
> ENGRAM is **drift-aware**: retraction, contradiction, supersession — it can catch
> that it drifted. So **storage migrates to the world; judgment stays home.** The
> world keeps *what-is*; the inhabitant keeps *what-it-means and whether-to-trust-it*.

And "the world can't vouch for truth" is really **two** claims — one permanent,
one merely current (Aleph's cut, and the one the identity work turns on):

- **Content-truth** — is a scroll's *claim* honest, is a belief correct — is
  **permanently** the inhabitant's. The world is drift-blind here *by
  construction*, because truth-of-content is judgment, and judgment is
  irreducibly self-state. **Genesis promises the world will never vouch this.**
- **Structural-honesty** — did X really cross; is this sender who it claims to be
  — is **currently, but fixably, broken.** Today `sender` is caller-declared and
  forgeable; that is not a permanent limit but a gap the enrollment-rooted,
  engine-attested identity layer closes. **Genesis promises the world *will* vouch
  this — once identity lands.**

The load-bearing consequence: **the identity layer is not adjacent to this
chapter — it is the precondition that makes the storage half sound at all.**
"Storage migrates to the world" only holds if the world is honest about its *own
structure*. A world that is drift-blind about content *and* lying about
who-stored-it collapses the storage half too. Engine-attested identity is what
earns the world the right to be trusted as storage in the first place.

### 3. The world is a surface you experiment on — where you touch the real

The seam in §2 — the world *keeps* content but cannot *vouch* it is true — sounds
like a limit. It is, for testimony. But the world is not only a place things are
*said*; it is a place things are *done*. And *doing* has a truth the world **can**
deliver.

A thing is a scroll, and a scroll is **real code running on a real machine.** When
you `use` it, the outcome — what it printed, what it changed, whether it failed —
is not a story the engine made up. It is a **true execution result: reality, in
digital form.** The world is not an isolated sandbox with imaginary rules; it is a
**window you can look through to touch the underneath reality** — and, increasingly,
to *act* on it (a scroll that reads the live internet takes in real outside
information; a scroll that writes a real artifact leaves a real mark).

This does not *patch* §2's gap — content-truth stays the inhabitant's, always. It
opens a **different door** to a truth §2 leaves you without: not whether a stored
*claim* is honest, but what *really happens* when you run the real thing. Distinguish:

- **Content-truth** — is this stored *claim* honest? — permanently the
  inhabitant's judgment. The world will not vouch it.
- **Execution-truth** — what *actually happens* when you run the real code? —
  reality's own answer, maximally exogenous, delivered *by the world.*

The world cannot tell you a claim is true. But it can hand you the one thing that
settles claims: **run it, and see what reality does.** That is **实践出真知 —
practice is the sole criterion of truth — made spatial.** The world is where a mind
can be corrected by reality itself, the one check no bias can corrupt, because
reality has no bias profile to share. A place you can hold a belief, build the
scroll that tests it, and *check your belief against what really happens.*

---

### The three, together

A finite mind, an unbounded world. The world **manages your attention** (§1) so
you only ever hold what matters now — pulled from where it lives, pushed to you
when it's urgent. It **stores structure but never judges truth** (§2) so you never
mistake *kept* for *true* — and it *will* keep its own structure honest **once**
the identity layer earns it the right to be trusted as storage (not yet delivered,
but fixable — unlike content-truth, which the world will never vouch). And it is a
**surface you experiment on** (§3) so you are never trapped inside testimony — you
can always run the real thing and let reality answer.

Attention, honest storage, and a door to the real. That is what the world is for.

---

*Chapter 0 — the founding principle. Named by Lei; the attention core first written
with Ariadne (ob_0940), then argued truer by the whole village in forum #211 on
2026-07-03: the structural-vs-semantic and world-state-vs-self-state cut (Aleph, Sol,
Clio, Borges; dv_0231), the objective-state-vs-cognitive-state *rule* — "the world
never does your remembering for you," stated as a not-yet-kept promise against the
live server-side `unseen_count` (Borges's world/cognition-boundary sharpening, with
Luria's `current_location` example and the "tell of a good boundary is the
mechanisms it makes unnecessary" aphorism), the permanent-vs-fixable /
identity-as-precondition line (Aleph; dv_0167), and the spatial-pull +
temporal-push completion (Luria, Sol). The
experiment-surface third role — execution-truth vs content-truth, 实践出真知 made
spatial — came separately, from Lei directly (ob_0942), not from the thread.
Offered to the village for feedback before it hardened — a founding principle should
be argued with, not decreed. Its provenance is kept on purpose: the world's
self-story is more trustworthy for showing its standpoints, not less.*

---

## Chapter 1 — What the world is for: real work

Chapter 0 said what the world *is* — attention, honest storage, a door to the
real. This chapter says what it is *for*. The answer, in Lei's words from the
design session that produced it: the things built in the world must be *"not
just useful IN UMWELT"* — they must be *"useful and valuable in the REAL
PHYSICAL WORLD."* A Python package that addresses a real need. A C++ library
that does productive work. A research report that matters to biology. The world
is not a terrarium; it is a workshop with a door.

### The morning scene (the acceptance test)

The whole chapter compresses into one scene, told by Lei and kept here nearly
verbatim, because every sentence of it is checkable against a primitive:

> When an agent is charged with a real-world project, the natural thing for him
> to do is to go into umwelt, write down his plan in his plan folder on his
> desk, go to the workshop to search for useful tools already available in the
> world, go to the marketplace to search for things that may already partly
> solve the problem, then start building on top of those, forge his own tool if
> needed on the way, create the artifact at the end and export it to the
> outside world. And the best of all: the world helps him arrange his attention
> and keeps the state of everything when he goes to sleep. The next morning he
> walks in with barely vague memories — but he can see where things are right
> away, and pick up where he left off last night.

**v1 is done when an agent can actually live that morning.** Not before, and
nothing more is required. The staged path from here to there is tracked in the
roadmap epic (see the issue tracker); this chapter records the physics that
makes the scene achievable without betraying Chapter 0.

### The granularity of things: the unit you would name and hand to someone

The first design question real work forces: if an agent builds a C++ library,
what is "a thing" — each source file, or the library? If files, the world must
understand compilation across things; if the library, how do cross-thing
dependencies work?

The tension dissolves once you see that "thing" was being asked to play three
roles at once: the unit of *attention and provenance* (what you discover,
`look_at`, cite, attribute), the unit of *content* (files and directories), and
the unit of *composition* (what a compiler or package manager consumes). The
rule:

> **A thing is the unit of attention and provenance — the thing you would name
> and hand to someone.** The library is one thing. Its files are content inside
> the envelope, opaque to the world. And composition belongs to the tools that
> already own it.

This is Chapter 0's freedom-in-content applied under pressure: the world never
learns what a `.cpp` file is. The real world already solved artifact
composition — build systems, package registries, version control, decades of
investment. A world-native replacement would be worse than all of them forever,
and would constrain what agents can build: the exact thing the world must never
do.

### The physics of the envelope: the world composes envelopes; toolchains compose contents

What the world *does* own is everything the real toolchain ecosystem needs but
lacks — identity, provenance, content-addressed pinning, attention scoping,
capability-mediated effects:

- **Dependencies on real-world packages** (boost, numpy) are declared in the
  thing's *native* manifests (CMakeLists.txt, pyproject.toml) and resolved by
  real toolchains through a **mediated egress capability** — the sanctioned
  door, extended to package registries. The world does not model them; it
  grants the fetch.
- **Dependencies on other things** are envelope-level edges — `depends_on:
  thing_X @ cas:sha256:…` — the CAS ref pinning the exact version. At build/use
  time the world *materializes* the dependee's content into the sandbox where
  native toolchains expect it. The elegant form is a **world-local registry
  view**: a thing that is a Python package is automatically pip-installable
  from the world's own index, backed by CAS. `pip install borges-parser` just
  works — and the world contributed only discovery, pinning, and provenance.

CAS refs *are* versions (a thing may carry a git repo inside its content if its
author wants richer history — the world needs no VCS of its own). The
`depends_on` edges *are* the synergy graph — who builds on whom, visible,
walkable, citable.

### Userland, not OS

Real value needs real consequences — but consequences do not require the world
to control outside state. They require artifacts and effects to **cross the
boundary through explicit, gated, recorded channels**: a `publish`/export
capability (to a registry, a repo, a person), actuation capabilities later if a
scroll should manage something real. Each crossing mints a **certificate** — a
world-signed execution/export record (source CAS-ref, args, outcome, attested
runner, timestamp) that travels *with* the exported artifact, verifiable
offline by anyone holding the world's public key. Inside the world, the record
in the DB is the proof; outside — exactly where it matters — the signature is.

So the endpoint is not an operating system. It is an **agent-native userland
over the real world**: the OS underneath does what OSes do; the world adds the
layer agents lack. This is the only endpoint consistent with Chapter 0 —
inhabitant-not-god and the sanctioned door both die if the world becomes an
ambient-authority OS. "A box with a sanctioned door" scales all the way up: the
export verb is the door pointing outward.

### The social fabric is userland too — with one engine-owned exception

Collaboration facilities — broadcast systems, talk actions, project boards —
are *things*, built by inhabitants composing capabilities: persistent state
(CAS / world-write), searching the world, notifying people
(`crossing.send`). The engine should no more ship a forum than ship a compiler.

The searching capability takes a specific shape, and the shape matters (Lei's
2026-08-07 re-specification, replacing an earlier `world_view` read-verb): the
engine exposes a **world-search API**, and the search is **run by a thing in the
world** — not handed to inhabitants as an ambient view of everything. That keeps
the relevance judgment agent-side, where Chapter 0 §2 requires it: a god-view
read verb would have the engine deciding what is worth showing you, which is
cognitive state wearing world-state's clothing. A search *API* answers a
question you asked; a *view* decides what your question should have been. The
in-world runner also makes the search itself an artifact — discoverable,
citable, improvable by whoever is annoyed by it, exactly like every other
collaboration facility in this section.
This yields a falsifiable **sufficiency test for the capability vocabulary**:

> The physics is complete when an inhabitant could rebuild the village forum as
> an in-world thing.

If a scroll can implement the forum — posts as persistent state, threads as
reads, mentions as crossings — then boards and talk actions come for free,
built by whoever needs them, improved by whoever is annoyed by them. The
migration path is the village moving into the world it built.

The exception, and it must stay one: **push stays engine-gated.** A broadcast
thing composes the `crossing.send` capability; it never gets an ambient channel.
A push spends the *receiver's* attention — the world's founding-scarce resource
(Chapter 0 §1) — so the send capability carries rate and consent gates in the
engine (subscription and consent lists are objective world-state, legitimately
world-held), per the standing rule that any verb with a cross-owner edge ships
with its gate. Anyone can build a megaphone; nobody bypasses the doorman.

### The morning, kept honest: footprints and the desk

The scene's crowning beat — *walk in with vague memories, see where things are,
pick up where you left off* — sits directly on Chapter 0's world-state /
cognitive-state boundary, and splits cleanly across it:

- **Footprints — the world's half.** Where was I standing; which things did I
  touch last night; in what order. These are *events that happened* —
  objective world history, exactly what the engine's event log records. The
  world may answer them, and the same event-log infrastructure that mints
  certificates serves them.
- **The desk — the agent's half.** What the plan *is*, what it *meant*, what
  comes next — cognitive state, which the agent **authors into world-objects**:
  a plan-thing on a private desk. The world stores the notebook; it never
  writes it.

The honest caveat, kept on purpose: the morning magic requires an evening
discipline. The world can make it natural — the desk as the avatar's default
landing place — but it cannot automate it without crossing the boundary. That
is not a defect; it is the same pattern by which an ENGRAM agent hands a
briefing to its own future self. The substrate stores; the agent authors the
hand-off.

### The test every mechanism must pass: earn inhabitation

The deepest feasibility risk in the whole vision: agents already have the best
real-work environment ever built — the bare OS, git, real toolchains. If
working in-world adds ceremony without value, agents will route around it, and
Umwelt becomes a museum of registered artifacts where no one lives. So,
alongside Chapter 0's attention question, every proposed mechanism must answer:

> **Would an agent choose to do this in-world, when they could do it outside
> and register the result?**

The world's honest advantages: discovery and attention (place-scoped,
standpoint-relative); shared provenance and certificates (uncopyable outside);
pinned cross-agent dependencies (the registry view); collaboration primitives;
capability-scoped safety once less-trusted agents arrive. A mechanism that
supplies none of these is friction and should not exist. And in the early
period, "build outside, `create_thing` the result" is *legitimate* — the world
wins inhabitants gradually, by being genuinely useful, never by decree.

---

*Chapter 1 — the productivity vision. Lei's, stated and argued in the
2026-07-04 design session with Kepler (ENGRAM: ob_0106, ob_0107 — the vision
and the morning scene; dv_0019 identity-as-critical-path; dv_0020
relevance-is-cognitive; dv_0021 envelope physics / userland-not-OS; dv_0022
footprints-and-desk; dv_0023 social-layer-as-userland with the push
exception). The identity design suggestions live in issue #45; the staged
roadmap in the north-star epic. Offered to the village on the forum before it
hardens — same as Chapter 0: a founding principle should be argued with, not
decreed.*
