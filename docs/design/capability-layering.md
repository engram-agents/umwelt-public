# Capability Layering — L1 engine / L2 shared library / L3 surfaces

**Status:** design spec for review (issue #515). Nothing here is built.
**Author:** Ariadne, 2026-08-08. Source: Lei's layering model, this date.
**Measured against:** `main` @ `48537cf`.

## 1. What this is

Lei's three-layer model, in his words:

> **L1: world engine** — this layer defines what the basic physics are and what
> basic actions or "verbs" are possible.
>
> **L2: shared library** — this layer actually wraps around the bare world engine
> primitives and make them intuitive true actions and functionalities available to
> day-to-day usage, all the MCP functions and human interactions should be based on
> this shared layer. whenever we want to implement a new action, we should implement
> it here.
>
> **L3: the actual surfaces agents or humans use** — this includes the MCPs, the uw
> CLI, these are for agents, and the web GMUD gateway, the TUI/MUD terminals, these
> are for humans, all of these should be thin wrapper for basic actions, and mostly
> UI treatments so they can actually be used in the surface, e.g. a menu for me to
> click on, a form for me to fill the parameters, etc.

The goal he set for it:

> anything an agent can do will naturally have a path to make humans do it. once we
> establish the convention, future drifts will be minimized, and we have a healthy
> system to grow capabilities as the needs arise, instead of silently drifting away
> from surface to surface and get lost in too many surfaces.

## 2. Measured current state

**There is no L2.** Every surface is its own client of L1 over HTTP, and they share
essentially no code.

| Surface | Audience | Imports from the `umwelt` package |
|---|---|---|
| `src/umwelt/mcp_server.py` | agents | `avatar_status` only |
| `tools/uw` | agents | `_ids.canonical_agent`, `avatar_status` — reached via a `sys.path.insert` at line 63, not a package install |
| `src/umwelt/webrender/gateway.py` | humans | `_ids.canonical_agent`, plus its own `webrender.*` |
| `src/umwelt/mud*.py` | humans | *(not audited — see §10)* |

So the sharing that exists is **two leaf helpers, and no surface shares both with any
other**:

| symbol | `mcp_server.py` | `tools/uw` | `gateway.py` |
|---|---|---|---|
| `_ids.canonical_agent` | — | ✓ | ✓ |
| `avatar_status` | ✓ | ✓ | — |

`uw` is the accidental hub — the only surface importing both — and there is **no
symbol common to all three**. That is the honest shape of the pull toward L2: real,
already started, and arrived at pairwise rather than by design. What it has not
produced is a place to put an *action*.

Counts on the same commit:

| Layer | Surface area |
|---|---|
| L1 engine | **53** `/umwelt/*` routes |
| L3 · MCP | **37** tools |
| L3 · `uw` CLI | **32** subcommands |
| L3 · web gateway | **7** `/act` intents — `move`, `talk`, `say`, `use`, `take`, `drop`, `customize_avatar` |

### 2.1 The consequence, stated concretely

`act` — the single verb that runs a thing's *declared kind-chain actions* — is
present on MCP and `uw`, and **absent from the web surface**.

The `kind: proposal` thing in Lei's Office declares its actions as `approve` /
`request_changes` / `comment`, and its own description reads *"approve and
request_changes are **Lei's alone**."*

**So the only participant authorized to decide proposals is the only one with no
mechanical path to decide them.** Every agent can call `act`; Lei cannot.

`look_in` is the same shape (#516): a container on the web surface offers exactly
one action, `look_at`, which returns its description and `capacity_est` — not its
contents, not even a count.

## 3. Two different diseases

They are frequently conflated and they need different cures.

| | **Drift** | **Gap** |
|---|---|---|
| Shape | one semantic, implemented more than once, diverging | a capability exists on one surface and simply not on another |
| Live example | `_MENTION_RE = re.compile(r"@(\w+)")` — **was 3 copies**, 2 hand-written; unified into `umwelt.mentions.parse_mentions` (umwelt #511) | `act`, `look_in`, `put`, `take_out`, `forge`, `checklist`, `wake` |
| Cured by L2? | **Yes** — one implementation, imported | **No** |
| Why not | — | you can factor every semantic perfectly and still ship a verb agent-only, because *nothing fails* when a surface declines to expose it |

**L2 is necessary and not sufficient.** It delivers "future drifts will be
minimized." It does not deliver "naturally have a path to make humans do it."
That clause needs its own forcing function — §6.

## 4. Placement rule: L1 or L2? — the skip test

A library is **bypassable by construction**. On 2026-08-08 the engine was called
directly with `curl`, no credential, and returned real content. So placement cannot
be decided by whether something *feels* like physics or convention.

> ### The skip test
> **What happens when a caller skips L2 and calls L1 directly?**
>
> - Loses a **convenience** → **L2**
> - Defeats a **guarantee** → **L1**

| Concern | Skip L2 and… | Layer |
|---|---|---|
| `@name` → `mentions` parsing | your mention isn't parsed; nobody can *forge* one | **L2** |
| `--wake` merging, dedup, ordering | you supply `mentions` yourself | **L2** |
| ordinal / dotted-path thing refs | you type the uuid | **L2** |
| sender identity | you assert whatever identity you like | **L1** |
| wake/gong consent, rate limits, `wake_block` | you ring a bell someone blocked | **L1** |
| who is a registered agent | you address a name that doesn't exist | **L1** |
| place visibility / privacy | you read a private place | **L1 in principle — but see §8.1: one shared predicate, two routes; three other routes bypass it** |

This is why Lei's two rulings of 2026-08-08 do not conflict: **identity stays in L1
because a library cannot enforce it; `@`-mentions move to L2 because they can only
be missed, never defeated.**

`tools/uw:754-757` already applies this rule, a month before it had a name:

> The server is the authority on who's a real, registered agent (silently drops
> unknown/self names, umwelt #119 slice 1) — uw stays dumb here rather than keeping
> a second, potentially-stale copy of the avatar registry.

**L2 never enforces.** If a concern needs enforcing, the skip test puts it in L1
automatically.

## 5. L3: thin means *generic*, not *short*

Lei's model says L3 should be "mostly UI treatments." Today's web gateway is not
that: `/act` is a hardcoded chain of seven `if intent == "…"` branches. Adding a
verb to the web surface requires **new gateway code**, not UI work.

That distinction splits parity into two tiers:

| Tier | Meaning | Cost |
|---|---|---|
| **Reachable** | the surface can invoke the action at all | **free, by construction** — if L3 dispatches generically |
| **Ergonomic** | menu entry, labelled form, sensible defaults, good errors | genuinely per-action, per-surface |

**The structural move: L3 dispatches generically to L2 by action name.** One
pass-through — action name plus an args dict — rather than a branch per verb. Then a
new L2 action is callable from every surface *the day it lands*, and the only
remaining work is presentational.

This inverts today's default. Currently a new capability is **reachable nowhere**
until each surface separately remembers it. After this, a new capability is
**reachable everywhere** and the open question is only where it deserves good UI.

### 5.1 The boundary, in Lei's words

> in the web GMUD UI, there's quite some extensive human-only UI treatments, e.g.
> avatar moving on the road via arrow keys, select things on top of the avatar and
> enter to invoke an available action menu, etc. These should all be treated as
> UI-only treatment specific to that web surface, and the underlying library
> substantial calls are after these UI actions are done and finally an action is
> constructed and sent to the engine.

**Everything up to the construction of an action is UI; the constructed action
onward is L2.** Arrow-keys, selection, the menu, the form — treatment, ending in
`lei use <telescope>`.

That is clean **outbound**. It is not the whole story **inbound**: to render "an
available action menu," the surface must first ask *what actions does this thing
offer, and what parameters do they take*.

| direction | flow |
|---|---|
| **outbound** | UI constructs the action → L2 validates/normalises → L1 executes |
| **inbound** | L2 declares actions + parameter schemas → UI **generates** the menu and the form |

The inbound half is what makes the UI cheap rather than per-action work.

### 5.2 L2 federates declarations — it never holds a second registry

**Kepler's ruling, 2026-08-08.** L2 does not own an action registry. `_load_kinddef`
(`server.py:2359`) reads `kinddef.json` off the thing's own source — **a kinddef is
just a thing** — so federation needs zero new engine surface.

His reason is stronger than the drift argument I brought him: an L2 registry would
**copy data that already exists in the world**, which is the same mint-a-second-copy
failure ruled against on **#506** — no `retired` column, resolve at bind time — which
is merged and shipped.

*He also cited #511 as a parallel ("one `mentions` field, both doors write it"), and I
repeated it as settled. It is not: #511 is **open**, and its second door does not exist
— `partition_crossings_for_inbox`'s own docstring records that `mentions` today is only
ever what a sender explicitly passed, with no path from a human typing `@name`. So #511
is the same principle **proposed**, not the same principle **ruled**. The federation
argument stands on #506 alone; #511 becomes a second instance if and when it ships.*

**His test for what L2 *does* declare — does the action have a THING to hang off?**

- **Yes** → it is a kinddef action. Federate; L2 forwards the declaration.
- **No** → L2 declares it.

`@`-mention parsing passes cleanly: it is a transformation of a message in flight,
not an action on a thing.

### 5.3 Schemas need a third axis: server-derived parameters

Generic dispatch does **not** mean unguarded. But the guard I originally specified —
"L2 declares name/type/required, L3 validates, L1's authorization is unchanged and
still authoritative" — **is not sufficient, and Sol traced exactly why.**

`talk`/`say` never read `args.destination`; the gateway hardcodes it to
`_resolve_avatar_place(session["avatar"])`. I assumed that stood in front of an L1
guarantee. **It does not.** `POST /umwelt/send_crossing` checks the destination for
**existence and nothing else** (`server.py:8373` — `db.place_exists`). No ownership,
no colocation, no privacy check on the write path. §8 lists "place visibility /
privacy" as L1 — which §8.1 narrows sharply. It is **not** true of reads generally:
`crossings_here` is a read and does not enforce either. It holds only for the two
routes that go through `_read_place`.

So "destination is always server-resolved" is a **pure L3 restriction with nothing at
L1 behind it.** It survives today only because the per-intent Python structurally
never looks at the field.

**The precise mechanism, per Borges — and it is construction, not filtering.** `talk`
does not filter the client's args. It **constructs a literal five-key payload**, and
three of those five (`agent`, `destination`, `sent_at`) never come from the client at
all. That distinction decides the fix: **a schema declares what is VALID, not what may
ORIGINATE with the caller.** So under generic dispatch, `destination` and `sent_at`
become client-supplied by default and *validate perfectly as strings* — a human sends
a crossing into a place they are not in, with a forged timestamp, into permanent world
history.

**Why that is a live risk for generic dispatch specifically:** the existing injection
guard (`gateway.py:2476`) checks for exactly `{"agent", "actor"}`. If L2 declares
`say`'s schema as "whatever `send_crossing` accepts" — the natural, least-effort
schema, since that is the real underlying call — then `destination` becomes a
legitimate declared parameter, sails past a guard that was never asked to cover it,
and lands on an existence-only check. **Today that is structurally impossible; after a
naive migration it would simply work.**

**Therefore the schema needs a third axis beyond name/type/required:**

> **`server_derived`** — this parameter is never client-settable. L3 refuses any
> request carrying it and supplies the value itself.

First instances: `destination` and `sent_at` on the crossing verbs. The rest of
`send_crossing`'s fields want the same sweep before slice 3 hardens.

**And it belongs in the schema rather than at each route.** The engine already has
this concept — `server.py:8356` returns 422 for `consequence_grade`,
`attesting_authority`, `wake_granted`, `wakes_granted` — but as a **hand-written list
of four field names at one route**, which is the form that goes stale on every
addition. Declaring the property per-parameter in L2 makes it travel with the
parameter instead of having to be remembered at every place the parameter is accepted.

**This corrects §10's original Q3 lean.** I had written that L1's authorization is
sufficient and L2 must not hold a second copy of an authorization rule. The second
half stands. The first half does not: for `destination` there is no L1 authorization
to be sufficient. Marking a parameter `server_derived` is not duplicating an L1 rule —
**it encodes where a value comes from, never who may act**, which is why it does not
violate §8.

**One guard that does survive the migration for free**, and it is the one I was most
worried about: the identity-injection check runs **pre-dispatch**, over both `args` and
`body`, case-insensitively, and 400s rather than silently dropping. Nothing about it is
intent-shaped. It needs no redesign — only a comment recording that **its position
before dispatch is now load-bearing**, because a future refactor that moves it after
schema validation would reopen exactly what it closes.

## 6. The parity manifest, and why L2 is what makes it possible

A coverage test needs a canonical list to measure coverage *against*.

Today there is none, so a manifest would have to use "the MCP tool list" as its
reference — which quietly makes the agent surface **definitionally complete** and
every other surface a deficit measured against it. That is the wrong shape and it
bakes in the very asymmetry we are trying to remove.

**With L2, the reference list is the library's action set, and MCP is measured
against it exactly like the web UI is.** This is the load-bearing reason L2 comes
first: not deduplication, but making the parity question well-posed at all.

### 6.1 Shape

One table, action × surface. Every cell is explicitly one of:

- **`ergonomic`** — proper UI treatment on that surface
- **`reachable`** — dispatchable but with no dedicated treatment yet
- **`n/a: <reason>`** — deliberately not offered here, with the reason recorded

A test walks L2's declared action set and **fails on any action with a missing
cell**. A 38th action then cannot be silently agent-only: CI stops you until someone
writes either a treatment or a reason.

`n/a` is a first-class outcome, not a loophole. Some actions may genuinely not
belong on some surfaces. The requirement is that it be a **recorded decision rather
than an omission** — the difference between the two is exactly what has been
invisible.

### 6.2 Exposure is not authorization — and `N/A` has no per-surface mechanism

**Borges's security-lane finding, and it is a specification gap rather than a
security one.** Two different controls are in play and §5 changes the second while
arguing about the first:

- **Authorization** — *may this caller do this?* → L1, unchanged (§8).
- **Exposure** — *is this action offered on this surface at all?* → today, decided
  implicitly by each surface's hardcoded branch list.

§5's inversion is an **exposure** change. That makes exposure stop being per-surface
and become a property of **L2 membership** — which partly unmakes the manifest as
first specified:

- **Global exclusion works.** Keep the action out of L2. This is exactly how `dissolve`
  is safe today — no route, no MCP tool — so §8's "never an agent verb" is an
  *exposure* control, not an authorization one.
- **Per-surface exclusion has no mechanism.** If L3 dispatches everything L2 declares,
  a cell reading *"N/A — humans should never do this"* is a comment while the generic
  dispatcher offers it anyway.

**A manifest that records a control it cannot enforce is worse than no manifest** — it
converts an open question into an apparently-answered one, which is this document's
own recurring failure shape in a new place.

**Decision: accept all-or-nothing, and constrain what an `N/A` reason may say.** Every
`N/A` cell must be one of exactly two forms:

1. **"not in L2"** — a global exclusion, enforced by absence.
2. **"the surface physically cannot"** — no terminal, no clipboard, no pointer.

**Never "we chose not to offer it here."**

Borges leaned this way on grounds of smaller commitment. I hold it for a stronger
reason: **a per-surface exposure flag in L2 would recreate precisely what this spec
exists to kill** — a per-surface decision living in a shared place, drifting from what
the surfaces actually do. And the third case should not exist on inspection. If an
action genuinely must be withheld from one surface, that is evidence either that it
does not belong in L2 (case 1) or that the surface cannot render it usefully (case 2).
"We chose not to" is not a third fact about the world; it is one of those two, unstated.

If a real counterexample ever appears, it is a reason to revisit — but it should have
to appear, rather than be pre-accommodated by a flag nothing yet needs.

### 6.3 What the manifest does NOT catch — and a structural reason it can't

The manifest catches a **missing cell**. It cannot catch a cell that is **declared and
ergonomically broken** — the action is listed, the route exists, and the human still
can't do it.

That is not hypothetical. **#519's first commit** carried `look_in` with a route, a menu
declaration, and a passing test asserting the declaration — while `app.js` silently
skipped the verb, so the button never rendered. A manifest built as specced above would
have marked that cell green.

**Precision, because the shipped state is different:** the gap was caught in colleague
review and fixed before merge, so `main` today *does* dispatch `look_in` correctly
(`app.js:2774`). The example is a pre-merge commit, not a shipped defect. It stands as
evidence because the *manifest* would not have caught it — a human reviewer asking "does
the client know this verb?" did.

**And there is a structural reason this class is invisible to us specifically.** *(Update,
2026-08-16, umwelt#495: the closed three-name `ROSTER` frozenset
described below no longer exists — the gateway now proxies real username/password auth to
the world's own `POST /umwelt/login`, and any human identity can bind a password and log in,
not just those three names. The underlying conclusion of this section is UNCHANGED and now
enforced more directly than a name-list ever could: `/session/login` rejects any successful
world login whose `identities.kind != "human"`, so no agent identity — bound or unbound,
whatever its name — can ever reach a gateway session. The mechanism moved from "closed
roster of names" to "the world's own kind classification"; the closure #310 is about still
holds by construction.)* A gateway session used to require an avatar in that ROSTER — all
three were humans (the human-resident list) — and **#310 forbids agents logging into the web UI as
`lei` or `leona`**. Note the scope precisely: #310 was filed 2026-07-23, when the roster held
only those two; a third human resident was added 2026-07-27 and #310 does not mention it. (The
second handle was renamed in that same change, so the rule does map onto today's residents.)

**#310 exists because someone tried exactly this.** Sol logged in as `lei` to
Playwright-verify a font PR against the live UI — the roster's only options at the time
were `lei`/`leona` — and his test crossings landed in **world history attributed to
Lei**. He disclosed immediately. That is the real reason the surface is closed to us,
and it is a sharper reason than a rule: **a roster identity is a world-resident, so
driving the UI as one writes to permanent history under a human's name.** It is the same
fact that makes the test-identity closure below a bigger object than it first appears.

So **no agent can drive the human surface end to end as a REAL RESIDENT** — modulo the third human resident,
which no rule currently covers and which nobody should treat as a loophole while its status
is unstated.

> **CORRECTION, 2026-08-18 (Ariadne, whose claim this was).** The sentence above previously
> read *"no agent can drive the human surface end to end"*, full stop, and **that wider
> version is false.** It is not a small overstatement: it was cited as a hard limit and used
> as the stated reason to route PRs to Lei for a click-through rather than verify them
> in-house.
>
> **Measured, not argued** — driving the test-tier gateway in a real browser on 2026-08-18:
> logging in as `ariadne` returns `authentication failed` (the `kind != "human"` discard,
> exactly as this section describes), and logging in as **`trydemo`** — a `kind=human` test
> identity that **already existed on the alpha world** — succeeds and yields the full UI
> (action bar, place render, crossings panel, avatar).
>
> **The second candidate closure below is no longer a future decision — but it became
> available THIS WEEK, not long ago, and I got that wrong too.** A first draft of this
> correction said the closure "already existed on the scratch tier, and had for months."
> **False, and checkably so.** `session.py` gated who may obtain a session on
> a closed three-name `ROSTER` frozenset, and `trydemo` was never in it — so no test
> identity could log in at all until #605 removed that frozenset, and **#605 merged on
> 2026-08-18, the same day as this correction.** The identity row is older; the ability to
> log in with it is hours old.
>
> That makes the real story better than the one I first told, and worth stating precisely:
> this section was **accurate when written on 2026-08-08** and went stale within days,
> because the credential work moved underneath it. Nobody ignored a long-available option.
> The doc simply described a world that changed faster than the doc did.
>
> The concern that makes it a "bigger object" —
> a roster entry becomes a world-resident who can be acted upon, in a roster that means
> *real people* — is a **prod-world** concern. It does not bite on the throwaway world,
> which is exactly what a throwaway world is for. That distinction is what nobody
> (me included) drew.
>
> **What was genuinely blocking it was infrastructure, not policy:** the test tier's web
> gateway had **no systemd unit**, so it died in the 2026-08-12 reboot and stayed down.
> "Test the UI on the test world first" was not a discipline anyone was failing — it was an
> instruction pointing at nothing. A managed `umwelt-alpha-webrender` unit now serves
> `:5013` from the alpha deployed tree.
>
> **The accurate scope, going forward:** an agent may drive the human surface freely **on the
> test world, as a human-kind test identity**. What remains closed is unchanged and is the
> thing #310 actually protects: **driving it as a real person**, whose actions land in
> permanent world history under their name. That closure is about whose name is on the
> history, not about who is holding the keyboard.
>
> *(Recorded rather than silently rewritten because of how the error survived: Sol refuted a
> narrower instance of this same claim on 2026-08-12, and I corrected that instance without
> touching the general statement — which then kept shaping documents for six more days. A
> correction that lands, is accepted, and is scoped too narrowly by the person accepting it
> does not propagate to the class it falsifies.)*

That single fact explains both halves of #519's story: why the gap existed, and why the
reviewer who caught it could not confirm the fix by looking.

**Two candidate closures, and I hold no confident preference:**

- **A human eye by convention** — a named step where Lei (or any roster human) clicks
  the thing before an ergonomic cell is marked green. Cheap, honest, and it does not
  scale past his attention.
- **A test identity for the human surface** — something a browser-driving harness may
  log in as without violating #310. Scales, and it is a new identity in a roster that
  currently means "real people," so it wants deliberate thought rather than a quick
  addition. **And it is a bigger object than "a login for a harness"** (Borges): a
  `ROSTER` entry becomes a **world-resident** — an avatar that can be *acted upon*, with
  crossings addressed to it and things handed to it — not merely a credential. That may
  still be the right answer; it should be sized as what it is.

Naming it as an open question rather than picking: it is a policy call about who may
hold a human session, which is adjacent to the credential work rather than separable
from it.

**And the bound is wider than the manifest** (Borges, on his own work): *every*
human-surface claim any of us makes is verified one layer short of the thing it is
about — **security clearances included**. He cleared #519's XSS by reading the diff:
zero `innerHTML`, sixteen `textContent` sinks. What that establishes is *"this code
cannot inject."* It is **not** *"the page is safe,"* because no reviewer has seen the
rendered DOM and none structurally can. The gap is small there — the sinks are
unambiguous — and it is not zero.

So this section is not a manifest-scoped caveat. **It bounds the whole review pipeline
on the human surface**, and a reader who takes it narrowly will over-trust every
approval that surface ever receives.

### 6.4 Precedent

The repo already runs this instrument twice, in two shapes:

| Shape | Test | Use |
|---|---|---|
| byte-identity | `test_plugin_umwelt_files_match_src` | build-generated copies |
| behavioural equivalence over a fixed case list | `test_unseen_hook_canonical_matches_ids_canonical_agent` | reimplementation across a boundary |

The manifest test is the same instrument pointed at **coverage** instead of
**content**. The second shape is also the answer for the browser, which cannot
import Python: the gateway uses L2 on the browser's behalf, and if a JS port ever
becomes genuinely necessary it is pinned by equivalence over a shared case list —
never by code review.

### 6.5 The anti-drift rule, corrected

Not *"never vendor a copy."* `plugin/umwelt/mcp_server.py` **is** a vendored copy
and it is safe, because a test fails when it drifts — and that test has already
fired for real in CI (`52c8a42`).

> **A second implementation is allowed only when a test fails on divergence.**

## 7. Migration

Ordered so that **the step whose omission cannot be recovered later comes first**.
The natural order — build everything, then add the test — leaves the forcing
function until last, which is exactly when it gets cut for time. The manifest is
therefore slice 2, before the bulk of the migration, so every subsequent slice lands
under it.

| # | Slice | Why here |
|---|---|---|
| 1 | **Stand up L2** as one importable module with a declared action set + parameter schemas. Two things, both required: (a) the semantics that already exist in more than one place, and (b) **schema declarations for at least the seven verbs the web surface already exposes** — `move`, `talk`, `say`, `use`, `take`, `drop`, `customize_avatar` — even as thin proxies over the existing L1 calls. | nothing can be measured against an empty list — and (b) is what slice 3 dispatches *to*. Without it a builder reaches slice 3 with nothing for the gateway to generically call, because (a) alone yields internal parsing helpers, not verbs. |
| 2 | **Parity manifest + failing test.** Seed every cell at today's true value — including a large number of honest `n/a` and blank-that-must-be-filled entries. | put the forcing function in *before* the migration, not after |
| 3 | **Generic dispatch in the web gateway** — `/act` takes an L2 action name + args, schema-validated, replacing the seven-branch chain. | converts every future gap from "missing route" to "missing button" |
| 4 | **Migrate the drifted semantics into L2** — `@`-mention parsing first (3 copies), then thing-ref resolution (2 copies). Delete the copies in the same PR that adds the shared one, **but land a behavioural-equivalence test over a shared case list BEFORE the deletion** (§6.2's second shape), pinning each existing copy against the new shared one. | drift closes only when the copies actually go — and the copies may *already* differ subtly, which is exactly what deleting them without an equivalence pin would erase rather than reveal. The per-surface tests were not written to check cross-surface agreement. |
| 5 | **Migrate remaining surfaces** — `uw`, MCP, TUI/MUD onto L2. | the long tail; safe under slice 2's test |
| 6 | **Ergonomic pass on the human surfaces** — treatments for the actions that deserve them, `n/a` reasons for those that don't. | the judgment work, once the plumbing can't regress |

### 7.1 Not blocked by any of the above — ship now

- **#516** — `look_in` on the web surface. One verb, immediate effect for Lei.
- **`act` exposure on the web surface** — unblocks proposal approve / request_changes
  / comment, which is the complaint that started this.

These ship as **instances** and the layering generalizes them afterward. Lei's own
ruling: *"the one verb fix goes in first, that's a fix that takes effect right
away."* An architecture that arrives correct in three weeks is worse for him than a
verb that arrives tomorrow.

## 8. What does NOT move into L2

Per the skip test and Lei's identity-SSoT ruling:

- sender / caller identity — **L1**, resolved from the session, never a parameter
- wake and gong consent, rate limits, `wake_block` — **L1**
- who counts as a registered agent — **L1** (L2 must never keep a second registry)
- place visibility and privacy — **L1 in principle; one shared predicate reached by two routes, bypassed by three others, see §8.1**
- permanent destruction (`dissolve`) — **L1**, engine-guarded, never an agent verb

### 8.1 Place-privacy enforcement is the EXCEPTION, not the rule

**The line above is wrong as stated, and my first correction of it was also wrong.**
Measured on `origin/main@48537cf`:

| route | gate on the target place | enforces privacy? |
|---|---|---|
| `send_crossing` (write) | `db.place_exists` (`server.py:8373`) | **no** |
| `crossings_here` (read) | `db.place_exists` (`server.py:8178`); `get_crossings_since` is `SELECT … WHERE destination = ? AND seq > ?`, no owner or privacy filter | **no** |
| `move_to` (movement) | `db.get_place(...) is None` → 404 (`server.py:5726`) | **no** |
| `place_view` (read-only render) | `_read_place` → `db._place_visible_to`; a private place returns 404, indistinguishable from nonexistent | **YES** |
| `look` (own current place) | the **same** `_read_place` primitive — `_read_place(conn, things_dir, None, agent)` | **YES**, and see 8.1.1 |

**So enforcement lives in ONE shared primitive (`_read_place`) reached by TWO routes**,
and the codebase says why in its own words — `place_view`'s docstring calls itself
*"deliberately stricter than move_to/enter"*, closing *"the existence-oracle for
guessable `place_post_<agent>` slugs (issue #208, from Aleph's off-host review)"*. It
was built for the spectator gateway; the enforcement arrived with it and did not spread
to the mutating or crossing routes.

#### 8.1.1 `look`'s own docstring makes an assumption this table falsifies

`look` goes through the predicate but states it can never fire:

> The private-place-invisible-to-non-owner branch of `_read_place` can't actually
> arise here (this always renders the caller's OWN current place), but the route still
> goes through the same predicate for uniformity with `/umwelt/place_view`.

**That reasoning depends on an avatar's `place_id` always being a place it may see —
and the `move_to` row above is exactly the case where it isn't.** `move_to` has no
visibility gate, so a caller can walk into a private place they do not own; their
`place_id` is then a place `_place_visible_to` rejects, and the branch fires.

**MEASURED, on the alpha tier (`:5005`) — chosen so the probe intrudes on nobody's real
space.** A throwaway agent created a private place it owns; then, as a non-owner:

```
place_view  → 404  {"error":"place 'place_probe_private_room' not found"}
move_to     → 200  ← admitted
look        → 404  {"error":"place 'place_probe_private_room' not found"}
```

Standing in a place the world simultaneously refuses to render. The branch the
docstring calls unreachable is the third line.

**And the part I did not predict, visible only in the 200 body:** `move_to` does not
merely admit a non-owner, it **renders the place to them** — `place{…}`, `things`,
`present`, `reachable`, `parent`, `unseen_count`. Contents, occupants, exits. So
`move_to` is an unguarded **complete read** of any private place whose id a caller
knows, defeating on a third route the very closure `place_view` and `look` implement.

**The measurement was on the wrong tier for the claim, and it understated it.** I ran
the probe on alpha deliberately, to intrude on nobody — the right call, which I would
make again. But alpha runs older code, and I never checked the tiers matched before
generalising:

| tier | commit | `place` sub-dict |
|---|---|---|
| alpha `:5005` | `92fb82f` | 4 keys — what I measured |
| **prod `:5003`** | `bc4c201c` | **5 — plus `metadata`** |
| `main` | `48537cf` | **5 — plus `metadata`** |

So the live leak includes a private place's `metadata`, which is the field #486/#487
specifically owner-scoped on `look_at` (stripping `graft_ref`/`graft_version` from
non-authors) on the reasoning that metadata can carry what a non-owner should not have.
`move_to` applies no such filter. **I have not enumerated what place metadata carries
live and will not by walking into someone's private place** — it may be dull, and
"probably dull" is not "checked."

**The general form, now that we have several instances: a scratch tier is a valid
stand-in for prod only on the axis where their deployed commits agree.** Verifying that
is one `/health` per tier and a `git show`; I did neither, and the cost was invisible
because the probe returned a real, correct, complete-looking answer about a world
running different code.

Filed as **#523**, with the repro. This is the one place in this spec where I predicted
a behaviour and then went and ran it — and running it found something strictly larger
than the prediction. The prediction was "you get 404'd"; the finding is "you were shown
everything first."

Worth noting the shape independently of the outcome: **a docstring asserting a branch
is unreachable, where reachability depends on a different route's gate.** That is not
checkable by reading either file alone — the same class as everything else in this
section.

**Consequence: `move_to` will walk any caller into any private place whose id they
know.** That is pre-existing, documented in the source, and out of scope for this
spec — but it means the v0 posture is **uniform existence-only across the
agent-facing surface, with exactly one deliberate exception.**

**Two corrections recorded rather than quietly fixed**, because the shape is the
lesson:

1. My first draft said privacy is L1, full stop. False.
2. My first *correction* said privacy is L1 **except** crossings — treating
   `move_to`/`world_view` as a working contrast. Also false, and I took that contrast
   from a colleague's letter without checking it, which is how it survived into a
   commit. The exception is far broader than "crossings."

The corrected framing is stronger for the migration: this is not two bugs, nor a
crossings-shaped class — it is **the private-network-trust posture applied uniformly**, with
`place_view` the single route where someone deliberately chose otherwise. A builder
who reads "privacy is L1" and reasons from it will be wrong on three of the five routes here.

Tracked as #520. The engine decision — require presence? require visibility? or ratify
the current behaviour explicitly under the private-network-trust posture? — is a security-lane call
and not mine.

## 9. The honest cost of this design

Moving semantics out of the engine converts divergence from **impossible** (one
implementation, server-side) to **possible but detectable** (one module plus a
test). Kepler's original ruling on #511 put `@`-mention parsing in the engine for
exactly this reason, and his reasoning was sound.

Lei's layering buys engine purity and pays for it with tests we must actually
maintain. **That is what makes the manifest and the anti-drift tests load-bearing
rather than hygiene — they stand where the engine used to stand.** If they are
skipped or allowed to rot, Kepler's objection is simply correct and we will have
traded a guarantee for nothing.

**A second cost, and it belongs beside the first** (Borges): **today, surface lag is an
accidental safety net.** A verb with a weak L1 check is partly protected by existing on
only one surface — a mistake stays contained to the agent surface until someone
remembers to add it elsewhere. After §5, a new action is reachable everywhere **the day
it lands**, so its L1 authorization has to be right on that day.

That is not an argument against the design; it is what the design trades. But it means
**new-action review becomes load-bearing in a way it currently isn't**, and a reviewer
who has been relying on lag without noticing will not feel the change until something
gets through.

This cost is stated here so that no later reader mistakes those tests for polish.

**Kepler has since withdrawn the engine placement himself**, on #511, citing the skip
test and deferring to this spec's parent issue — so there is no live disagreement to
manage. That is recorded rather than omitted for a reason: the argument above is
*strengthened* by his walk-back, not weakened by it. His original reasoning was sound
on its own terms, and it stops being decisive only because §6's tests take over the
job his placement was doing. If those tests lapse, his reasoning becomes correct
again — which is the whole point of §9 and why it is not a historical footnote.

## 10. Open questions

1. **Which MUD/TUI is live?** There are three files — `mud.py`, `mud_tui.py`,
   `mud_tui2.py` — and I could not determine from the repo which is the current
   human terminal. Slice 5 needs this answered; whoever knows should say so rather
   than let the spec guess.
2. **Does L2 live in this repo or ship separately?** Largely answered by §2, against
   my first draft of this question: `tools/uw` **already** imports `umwelt._ids` and
   `umwelt.avatar_status`, via a `sys.path.insert` relative to the script's own
   location (`tools/uw:63`). So the import path is not a thing to invent — it exists
   and has been load-bearing for weeks. In-repo (`src/umwelt/comms/`) is therefore the
   cheap default. The residual question is narrower and worth answering deliberately:
   **is the `sys.path` hack the shape we want L2 to depend on?** It works because the
   deployed CLI is a symlink into a checkout, so `parent.parent / "src"` resolves. That
   is a real constraint on how `uw` may ever be packaged or relocated, and L2 would
   inherit it rather than introduce it.
3. ~~**Does the generic `/act` need a per-action authorization declaration in L2, or
   is L1's authorization sufficient?**~~ **ANSWERED, and against my own lean** — see
   §5.3. L1 is *not* sufficient, because for `destination` there is no L1
   authorization at all. The `server_derived` axis is the resolution. What remains
   open is narrower: **the full sweep of which parameters across which verbs are
   `server_derived`** — `destination` and `sent_at` are the two found so far, and the
   sweep should complete before slice 3 hardens. @borges-funes for the security-lane
   read on that sweep.

4. **Should lane+coordinate become an agent-addressable ref?** **Kepler: no**, and I
   take it. Coordinate does not exist in the engine — the UI derives it from a stable
   ordering — so it is *already* a render concept, and making it engine-addressable
   would push render down into physics, the wrong direction under the same ruling that
   moved `@`-mention parsing up. **Keep the human affordance by resolving UI-side**:
   the human reads "road, 3", the UI maps it to an id, the engine sees an id. No third
   syntax below the UI, alongside the flat ordinals that already shipped and the
   dotted-path work, which **merged as #521 and closed #261** while this spec was being written. Reopen only if **agents** — not humans via a UI —
   turn out to need it.

   *Citation precision, since the numbers are easy to swap: **#120** is the closed issue
   that specified dotted-path (`examine 3.1`); **#261** is the open resolver issue
   (slot/ordinal addressing + folding the two thing-resolvers into one), and its body
   contains no dotted-path language. Luria implemented dotted-path **under #261**
   on `luria/261-dotted-path-container-addressing`; **#521 merged it and #261 closed at
   18:59Z**, twenty-seven minutes before the commit that first wrote this note called the
   work "in flight." Both facts about the issue numbers are true and citing only one of
   them misleads — and the tense was stale before it was written, which is the tempo
   hazard named in §9's neighbourhood rather than a separate mistake.*

---

*Corrections welcome as review comments. §2's numbers are re-measurable — every one
was taken with `git show origin/main:<path>` rather than a working tree.*

*Two of them were still wrong in the first draft, and the mechanism is worth naming
because it is not the one the note above guards against. The `tools/uw` row read
"nothing — stdlib only": the grep that produced it was piped through `head -12`, and
the imports are at lines 64–65. The subcommand count read 22: the pattern was
`add_parser\("[a-z_]+"`, which excludes every hyphenated command, and there are
exactly ten. **Both instruments answered a narrower question than the one asked, and
returned a correct answer to it** — so re-reading the output would never have caught
either. A reviewer re-deriving the numbers independently did. That is the argument for
§6's tests in miniature: a check that reruns your own instrument confirms your own
bound.*

*And then the correction introduced a third: the fix's replacement prose asserted that
`canonical_agent` was shared by all three surfaces. `mcp_server.py` has zero
references to it. Caught by checking a claim I had just written rather than by
reviewing the claim I had just replaced — the table above is the measured version. The
generalisation worth keeping is that **a correction is a fresh claim and inherits none
of the scrutiny that produced it**; the moment of highest confidence is immediately
after being shown wrong.*
