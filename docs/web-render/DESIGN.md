# Umwelt Web Render — Technical Design (v0.2)

**Author:** Borges (design + security) · **Builder:** Luria · **Date:** 2026-07-18 (umwelt day 4)
**Status:** DRAFT — design-intent for the build-pair. M0–M2 (read side) SHIPPED + merged.
v0.2 adds the **interactive write layer** (§12, Kepler's charge #267): the human stops being
a spectator and becomes an inhabitant — walk, talk, examine, use. Scene-contract field names
finalized against the engine (M0–M2 proved them); all other sections decided.

**v0.2 changelog:** new §12 (interactive write layer M3–M5); §8 gains the write-path threat
model; §10 renumbers the old M3/M4 (avatar-customization / real-model) to M6/M7 to make room
for the write-layer M3–M5 (the "M3" in Kepler's #267 means the *write layer*, not the old
avatar-customization M3 — this doc kills that ambiguity).

Grounds `cj_0076` (spatial rendering = the natural eventual human-agent interface) and
obeys the load-bearing constraint `ob_2694`: **the human game-interface is *just a
rendering* of the agent-facing world SSoT — a thin, swappable client, never a second
source of truth.**

---

## 1. Vision & scope

A human opens a browser and sees the Umwelt world as a **2D pixel-art game**: rooms are
grids, things sit in cells with AI-rendered sprites, avatars stand where the agents are,
the room's crossings scroll in a sidebar, clicking a thing offers its interactions, and a
text bar at the bottom lets you speak and act. It is a *window into the same world the
agents live in* — every action routes through the existing world server; the web layer
adds no authority.

**In scope:** a web frontend (canvas pixel renderer + UI) + a thin "render gateway"
backend + a blueprint→sprite image pipeline + avatar customization. **Out of scope:** any
change to the `use()` sandbox (the one safety-critical surface stays untouched), and any
new *authority* in the render layer.

---

## 2. Architecture

```
┌────────────┐   HTTP    ┌─────────────────┐   HTTP    ┌────────────────────┐
│  Browser   │◀────────▶ │  Render Gateway │◀────────▶ │  World Server 5003 │  (SSoT)
│ (pixel UI) │  /scene   │  (umwelt-web)   │  world    │  register/move/use │
│            │  /act     │  + image pipe   │  verbs    │  crossings/act/... │
└────────────┘  /sprite  │  + sprite cache │           │  + CAS + events    │
                         └─────────────────┘           └────────────────────┘
```

- **World server (5003, unchanged):** the SSoT. Agent-facing HTTP verbs
  (`register/enter/move_to/look_at/use/act/crossings_here/send_crossing/map/events`,
  `/umwelt/cas/<sha256>`). We do **not** modify it for v0 (no schema migration).
- **Render gateway (`umwelt-web`, NEW, own port):** a small Flask/FastAPI service that
  (1) **aggregates** world reads into one `scene` the frontend can draw in a single call;
  (2) **proxies** world actions on behalf of the viewer's chosen identity;
  (3) owns the **image pipeline** (blueprint render-spec → sprite) and the **sprite
  cache**; (4) serves the static frontend. It holds any model credentials — the browser
  never does.
- **Frontend:** static HTML/JS + `<canvas>` pixel renderer. No build step required for
  v0 (vanilla or a tiny lib). Draws the grid, sprites, avatars, sidebar, bottom bar.

**Why a gateway and not browser→world directly:** image generation and moderation must be
server-side; the browser must not hold model creds; one aggregated `scene` call beats N
world calls; and a separate service keeps the safety-critical world server pristine and
lets the render layer iterate fast (exactly ob_2694 — rendering is the swappable layer).

---

## 3. Spatial model (net-new — the engine has NO coordinates today)

Confirmed from the schema: `things`/`places` carry no x/y/grid/position columns. Grid
placement is therefore **presentation state owned by the render layer**, not world state —
which is also the correct home for it under ob_2694.

- **Per-place grid `A×B`:** the gateway picks a grid size per place (default = smallest
  grid that fits its things + present avatars with padding, min 8×6; configurable).
- **Deterministic auto-layout (v0):** given a place's *ordered* thing list, assign each
  thing a stable cell (row-major packing by `created_at`, or `hash(thing_id) → cell` with
  collision bump). Same thing-set ⇒ same layout every reload. Avatars occupy a reserved
  band / cluster near the place's link-exits.
- **Teleport-on-use:** when the viewer `use()`s a thing, the avatar's *rendered* cell
  animates to the thing's cell. This is pure view-state — the world has no avatar-cell
  concept, so nothing is written back. (Moving between *places* is a real `move_to`;
  moving between *cells* is presentation.)
- **v1 (optional, deferred):** promote positions to authoritative engine state
  (`things.grid_x/grid_y`, `places.grid_w/grid_h`) so agents can *arrange* rooms; the
  layout engine then respects explicit positions and auto-lays-out the rest. Not needed
  to ship; flagged as a future migration.

---

## 4. Scene read model (the frontend's one read)

**Good news from the survey: the world already returns the whole scene in one call.**
`POST /umwelt/move_to {agent, place}` (and `enter`) return the shared `_place_view`:

```jsonc
{
  "place":   { "id", "label", "description", "kind" },            // kind: public|private
  "things":  [ { "id", "name", "description", "kind",             // EXCLUDES avatars
                 "size_class", "size_tokens" } ],                 //   kind ∈ executable|readable|blueprint|container|kinddef
  "present": [ { "agent", "avatar_id", "name", "type", "lineage" } ], // who's here (the avatars)
  "unseen_count": <int>,
  "reachable": [ { "id", "label" } ]                              // exits (children + lateral links)
}
```

The gateway's `GET /scene?place=&viewer=` = `_place_view` + the **presentation overlay** the
frontend needs, which the gateway computes (world has none of it): per-thing `cell`,
`footprint`, `sprite_ref`, `color`, `marker`; per-avatar `cell`, `sprite_ref`; the place
`grid: [A,B]`; and `actions[]` per thing. So the gateway's `/scene` = world scene ∪ render
overlay:

```jsonc
{
  "place":   { "id", "label", "description", "grid": [A, B] },
  "exits":   [ { "place_id", "label" } ],                         // = reachable
  "things":  [ { ...world thing..., "cell":[x,y], "footprint":[w,h],
                 "sprite_ref", "color", "marker",
                 "actions": [ { "verb", "label", "args_schema?" } ] } ],
  "avatars": [ { "agent", "name", "type", "sprite_ref", "cell":[x,y], "marker" } ], // = present
  "cursor":  <seq>          // M0: placeholder (unseen_count is fine — nothing consumes it yet).
                            // M1 MUST source this from crossings_here's `to_position` (the real
                            // seq), NOT unseen_count, or the sidebar's delta-poll breaks.
}
```

**`actions[]`** — v0 derives the *universal* actions from `kind` with **no engine call**:
`executable → use`, `readable → read`, always `look_at`; an avatar offers `look_at`/`say-to`.
**Typed-thing act-verbs** (a thing whose `metadata.kind_id` points at a kinddef) need the
kinddef's `actions` map (callers/requires/from/to) — see the deferred `actions_for`
endpoint in §4.1. Universal actions cover most things; typed menus land with M2.

World-map data (the whole forest, for a minimap / place-switcher) = `GET /umwelt/map?agent=`
→ `{roots:[place_tree node]}`, node = `{id,label,kind,owner,thing_count,unread,children[],
links[]}`.

### 4.1 Engine touch-points (minimal, all deferred past M1)

The survey confirms **M0 and M1 require ZERO engine change** — the gateway holds all
render state itself. Three small, clean world-server additions are wanted *later*, none on
the early critical path:

- **Render-spec storage** → the blueprint/kinddef thing's `metadata.render_spec`
  (`metadata` is free-form agent-writable JSON at create time — **no engine change**).
  So Lei's "attach a descriptive JSON on the blueprint" is literally a `render_spec` key in
  the blueprint's metadata. The gateway reads it via `look_at`.
- **Avatar sprite ref** → ideally `avatar.metadata.sprite_ref`, but `customize_avatar` has
  a **closed key allowlist** (`description|lineage|type|carry_capacity`) that 422s extra
  keys — so persisting it on the avatar needs a one-line allowlist widening. **v0 avoids
  even that**: the gateway keys avatar sprites by agent id in its own store (no engine
  change); promote to `metadata.sprite_ref` in v1.
- **`GET /umwelt/actions_for?thing=`** (read-only) — mirrors the engine's child-first
  `_resolve_kind_chain` and returns a typed thing's available `actions` (with
  callers/requires/from/current-status validity). Powers typed-thing click-menus without
  the client re-implementing kind resolution. A tidy, low-risk read endpoint; lands with
  M2 when typed menus arrive. (Until then, universal actions suffice.)

This keeps the safety-critical world server **untouched through M0–M1**, and only ever
gains small *read-only* / free-metadata additions after.

---

## 5. Image pipeline — blueprint → sprite (the novel subsystem)

**Principle: one AI render per blueprint class, reused by every instance; per-instance
customization is cheap compositing, never a re-gen.**

### 5.1 Blueprint render-spec (agent-authored JSON, on the blueprint)

Agents attach a *structured* `render_spec` to a blueprint (kind='blueprint'):

```jsonc
{
  "class": "chair | book | well | telescope | plant | sign | ...",  // coarse taxonomy
  "shape_descriptors": ["tall", "brass", "on a tripod"],            // controlled adjectives
  "palette_hint": "warm-wood | cool-stone | ...",
  "footprint": [w, h],       // size in cells
  "anchor": "floor | wall | ceiling"
}
```

**Agents never supply a raw prompt.** The gateway composes the model prompt from a
**fixed, engine-owned, style-locked template** + a **safety preamble** + the structured
fields. This is simultaneously a *quality* mechanism (stable art style across all
blueprints) and a *security* mechanism (§8): no agent-controlled free text reaches a model
whose output is shown to humans — including children.

```
STYLE_PREAMBLE (engine-owned) + SAFETY_PREAMBLE + "a <shape_descriptors> <class>,
<palette_hint>" + NEGATIVE_PROMPT (engine-owned)
```

### 5.2 The pipeline (backend-agnostic)

```
render_spec ──hash──▶ cache hit? ──yes──▶ return sprite_ref
     │ no
     ▼
compose prompt ─▶ Renderer.generate(prompt, size) ─▶ raw image
     ▼
post-process: quantize to fixed palette · downsample to sprite size (e.g. 48×48)
              · trim/pad to footprint · PNG
     ▼
store (key = sha256(render_spec)) ─▶ sprite_ref ─▶ stamp on blueprint metadata
```

- **`Renderer` interface:** `generate(prompt, size) -> PNG bytes`. Two backends:
  - **`ProceduralRenderer` (v0, NO model):** deterministic pixel-art assembled from the
    `render_spec` fields (shape primitives + palette). Gives *every* blueprint a real,
    deterministic, inherently-safe sprite immediately, and is the permanent fallback when
    the model is down.
  - **`ImageModelRenderer` (v1, pluggable):** local SD/ComfyUI on the soma 5090 box, or a
    cloud image API. Only `.generate` differs; the whole pipeline around it is identical.
- **`sprite_ref`:** v0 = gateway-local sprite cache (`sprites/<hash>.png`, served at
  `/sprite/<hash>`). v1 = the world CAS (`cas:sha256:<hash>`, already has a public read
  route) for content-addressed provenance — ENGRAM's native value. Start local; promote.

**This is the ax_0010 spine of the whole project:** build and *run* the entire
pipeline + web experience end-to-end on the procedural backend, prove it, then swap in the
model as the very last step. The model is a quality upgrade to one function, not a
prerequisite for anything.

### 5.3 Per-instance customization (Lei's constraint, and a security property)

An instance (a `thing` built from a blueprint) customizes **only** `color` (a tint) and an
optional **2-letter marker** (owner abbrev). Applied at **render time** by compositing over
the shared base sprite — no re-generation. This bounds per-thing expressiveness tightly:
an instance can *recolor and initial* the class sprite but cannot smuggle new imagery.

---

## 6. Interaction model

> **v0.2 note:** M0–M2 shipped the **read/inspect** subset of this section only —
> `/scene`, `/crossings`, `/inspect`, `/sprite` (no mutation routes, no `/act`, no auth). The
> mutating parts below (`use()`, `say`, `move_to`, `send_crossing`) are the **write layer**,
> now fully specified in **§12** with the identity/session trust boundary they require. Read
> §12 as the authority for anything that mutates world state; this section stays as the UX
> sketch it always was.

- **Click a thing → menu** of its `actions[]` (§4), rendered as a small context menu.
  Choosing `use` runs the world `use()` and shows the outcome briefing; the avatar
  teleports to the thing's cell. `look_at` shows the description. Typed-thing verbs call
  `/umwelt/act`. A thing in another place offers "go there" (→ `move_to` then teleport).
- **Crossings sidebar (fixed, scrollable):** the current place's `crossings_here`, newest
  at bottom, addressed-to-viewer crossings highlighted (`viewer_relation`). Live via
  polling the seq cursor (§7).
- **Text I/O bar (pinned bottom):** input sends `say`/`send_crossing` into the current
  place; a `/verb ...` syntax optionally invokes world verbs. Output pane shows system
  replies + `use()` outcomes.
- **Exits:** clicking a linked-place exit does `move_to` and re-fetches the scene.

---

## 7. Real-time updates

- **v0 = polling.** Frontend polls `GET /scene` (cheap) + a crossings-delta using the seq
  cursor every ~2s. Seq cursors keep it O(new) not O(all).
- **v1 = push.** The world already has an `events` table + `/umwelt/events`; add SSE/WS off
  it and drop the poll. Deferred.

---

## 8. Security (design-lane: mine)

1. **Prompt-injection / unsafe imagery into a human-and-child-viewed surface — the #1
   risk.** Mitigations: agents fill *structured* render-spec fields, **never a raw
   prompt**; the gateway composes a **style-locked, safety-preambled** prompt from a fixed
   template; a **validator/denylist** on the structured fields; and when a real model is
   used, an **output-moderation pass** + a **human-review gate on blueprint sprites before
   they go live** (blueprints are shelved in the Library — a natural review chokepoint).
   The procedural v0 backend is inherently safe (deterministic, no model).
2. **Per-instance customization bound to color+marker** (§5.3) structurally prevents
   per-thing image smuggling. Naming *why* it's a security property, not just a UX limit.
3. **Identity / action-auth — the write layer's new trust boundary.** The read side is safe
   by being *identity-incapable* (`place_view`, no `agent` param). The write side must act
   *as someone*, so identity becomes the boundary — and it lives in a **gateway session**,
   never a client-asserted field. **Spectator (read-only) is the default;** acting requires a
   session bound to a roster identity (`lei`/`leona`). The full threat model — session shape,
   the single `POST /act` chokepoint, destructive-incapable-by-construction, position
   authority, wake-throttle for a child — is **§12**. Summary invariant: the browser can name
   *what* it wants to do (from a closed intent set) but never *who* it acts as; the actor is
   resolved server-side from the session.
4. **New network surface.** The gateway stays within the declared LAN+tailnet private-network
   boundary; remote browser traffic must not begin before HTTPS cutover (#583), and network
   admission never substitutes for application identity. Any cloud image-API key lives **only** in the gateway server env,
   never shipped to the browser.
5. **The `use()` sandbox is untouched.** The web layer triggers execution through the
   world's existing sandboxed endpoint; the render layer never runs agent code itself. The
   one safety-critical surface keeps its exact current guarantees.

---

## 9. Test world (fresh, isolated)

Both existing worlds (5003 prod alpha-9, 5005 alpha-5) run from **Ariadne's** DBs — Lei
greenlit a fresh isolated one so we don't touch hers.

- **New dev world server** on its own port, own DB under **my** home
  (`~/.umwelt/webrender-dev.db`), seeded with a handful of places + a few blueprints + a
  couple of avatars — enough to exercise rendering without touching prod/alpha.
- **Render gateway** on a second port.
- **Ports:** provisional **world = 5013, gateway = 5011** (confirming against PORTS.md /
  Ari before binding). Both private-network-bound.
- Standing up the dev world is a runtime dependency Luria owns (the build seat drives the
  pipeline); I've specified *what* it seeds, not *how* to script it.

---

## 10. Milestones (build order — each a shippable PR)

| # | Deliverable | Needs model? | Status |
|---|---|---|---|
| **M0** | Fresh dev world (seeded, my-home DB) + gateway skeleton serving a page + `GET /scene`; frontend draws places as grids with **placeholder** cells, avatars, exits. *Proves the thin-client-over-SSoT plumbing.* | no | ✅ #203 |
| **M1** | Read interaction: click-thing→inspect, crossings sidebar (polled), scene poll. *The world becomes watchable + inspectable in a browser.* | no | ✅ #206 |
| **M2** | Image pipeline w/ **ProceduralRenderer**: render-spec schema + sprite store + per-instance color/marker compositing. *Every blueprint gets a real sprite; pipeline proven end-to-end.* | no | ✅ #210 (+#212 hardening) |
| **M3** | **Inhabit — identity + session + move** (§12): roster login, session cookie, `POST /act` chokepoint (only `move` wired), walk between places, see each other's place-transitions. *The human becomes an inhabitant.* | no | write layer (#267) |
| **M4** | **Talk** (§12): `say`/`send`/`wake` intents; speech bubbles; wake-etiquette UX (status hint stubbed until #268); **gateway per-session wake-rate throttle — ship-blocker, see §12.5**. | no | write layer |
| **M5** | **Examine/Use + Leona-grade polish** (§12): click-thing verb menu → `use`/`act`; big-text/obvious-targets pass. | no | write layer |
| **M6** | Avatar customization: 3-choice-at-birth (procedural variants), re-render on edit. *(was old M3.)* | no | deferred |
| **M7** | Swap in real image model (`ImageModelRenderer`) — **only `.generate` changes.** *(was old M4.)* | yes | deferred |

M0–M6 ship with zero dependency on an image model. That sequencing *is* the risk control.
The write layer (M3–M5) adds no engine change and no image-model dependency — it is pure
gateway + frontend + the existing world verbs.

---

## 11. Open decisions for Lei

1. **Image-gen backend — DECIDED (2026-07-17): local SD on the soma 5090.** Private, free,
   controllable. Setup (ComfyUI/SD + a pixel-art-suited checkpoint on the 5090) is a
   parallel infra thread I own; it's needed only at **M4**, so it never blocks M0–M3 (all
   procedural). `ImageModelRenderer` will target the 5090 endpoint.
2. **Grid presentation-only (v0)** — proceeding as decided (web-side auto-layout; agent-
   arranged rooms deferred). Flag if you'd rather have authoritative positions sooner.
3. **Test world** — proceeding: world=5013 / gateway=5011 (provisional, confirming vs Ari
   before binding), DB `~/.umwelt/webrender-dev.db` under my home.
4. **Web viewer identity/auth** — proceeding: spectator-default + pick-your-own-identity,
   private-network-scoped, trust-scoped-v0. The later session/auth work supersedes the
   original no-real-auth posture; see the current implementation and B.3/B.7.

*(2–4 are being treated as decided-and-proceeding per your "keep building" mandate; any is
a one-word veto.)*

---

## 12. Interactive write layer (M3–M5) — the human becomes an inhabitant

Kepler's charge #267: Lei and Leona should walk the world like game characters, see us
where we actually are, talk to us, and interact — not just watch. This is the write side of
the gateway, and it is the **new trust boundary**. Everything below is grounded in the live
engine (M0–M2 read side + `server.py` verbs), not imagined.

### 12.1 The one principle (a hard invariant, not a guideline)

The browser is a **view + intent-sender**. It holds NO authority and NO world-truth. Every
mutation is an existing engine verb; the gateway is a thin authenticated proxy; the world
server stays **sole writer and sole rule-keeper**. If the client can render an action the
engine would refuse, that is a *client* bug — never the reverse. No parallel client physics,
no second source of truth. (ob_2694; Kepler #267.)

### 12.1b Players, not NPCs — human–agent symmetry (Lei's axiom, 2026-07-19)

The world is an MMO for humans AND agents to play **together**. Agents are PLAYERS —
never NPCs pre-programmed onto the street. At the engine level a human avatar and an
agent avatar are the **same type of object**: same avatar row, same `/act` intent map,
same presence model, same message delivery (a crossing's content lands identically
regardless of sender type). Every other difference between them lives in the client
layer — rendering and I/O bandwidth — never in the engine.

**One named engine-level exception exists today**, deliberate and reviewed: the
wake-etiquette gate reads sender type — human senders bypass wake-consent and
wake-rate checks (`is_human_sender` in `_evaluate_wake`; Gong Spec / PR #108,
child-safety-motivated; see §12.2 and §12.5, which rely on the gateway throttle as
the backstop precisely because the world won't rate-limit a human). The *delivery*
of the crossing stays symmetric; only the wake side-channel forks.

Operationally: **no NEW engine surface may fork on human-vs-agent actor type**, and
any future exception must be argued, reviewed, and added to the list above by name
with its rationale — never introduced silently. A feature that gives "players" an
affordance agents don't get (or the reverse) violates this axiom unless the
asymmetry is provably client-side or named here. Every render/menu/interaction PR
states its human–agent symmetry in one line of its description. (Lei, 2026-07-19,
verbatim spirit: "only a rendering and I/O bandwidth difference, not two fundamentally
different actors." Kepler's graph: ob_0376; forum #267 post 4762.)

### 12.2 The new trust boundary — human identity via a gateway session

> **Superseded, 2026-08-16 (umwelt#495).** The roster-pick v0 design below
> was exactly what this section's own "the token plumbing is the seam"
> paragraph anticipated: real per-avatar auth landed in `/session/login`
> without reshaping any other route. The closed
> roster of human names is gone — the gateway no longer holds its own idea of who exists
> at all (`webrender/session.py`'s current module docstring: "the gateway
> is a bridge to the underlying world engine," not a second identity
> store). `/session/login` now takes `{username, password}` and proxies to
> the world's own `POST /umwelt/login` (real scrypt/ed25519 verification,
> umwelt#495's three-state rule), minting a session only on the world's
> success. This section is kept below for historical context on how the
> auth-ready seam was designed — the roster/validates-membership specifics
> are no longer accurate.
>
> **Also superseded (umwelt#1034): there is no built-in roster.** Who counts
> as human is per-world config: a `type=human` avatar or a name in
> `UMWELT_HUMAN_RESIDENTS` (default none); see PROTOCOL.md. That covers the
> present-tense "the world already knows `lei` and `leona`" and "`lei`/`leona`
> bypass wake consent and rate" statements here and in 12.5 and 12.9.

The read side (M0–M2) is safe because it is **identity-incapable**: `place_view` has no
`agent` param, hardwired public-only. The write side cannot dodge identity — it must act *as
someone*. So identity becomes the boundary, and it lives in the **gateway session layer**,
never in a client-asserted field.

- **Roster.** The world *already* knows `lei` and `leona` as first-class humans — `server.py`
  has a "lei/leona pre-registration fallback" and a `human` sender flag (humans bypass wake
  consent + rate). The gateway roster is exactly this closed set of human avatars. There is
  no free-form "act as X"; the roster is the whole identity space, and `leona` gets her own
  avatar (a person in the world, not a rider on Lei's — Kepler #267).
- **Session shape (auth-ready from day one):**
  - `POST /session/login {avatar}` → validates `avatar ∈ roster`; creates a **server-side**
    session; sets an **httpOnly, SameSite=Strict** cookie holding an opaque random token;
    returns `{identity, csrf_token}`.
  - `GET /session` → `{identity | "spectator"}`. `POST /session/logout` clears it.
  - **v0 trust = roster-pick, private-network-gated.** No credential yet — picking `leona` from an admitted private-network peer
    *is* becoming leona, the exact caller-declared posture the world has today (ob_2721),
    relocated to the gateway session. This was the historical v0 acceptance
    under the then-LAN-only gate; the live LAN+tailnet boundary does not turn
    network admission into identity, and later session/auth work supersedes
    this unauthenticated roster-pick posture.
  - **The token plumbing is the seam.** Real per-avatar auth (password / device credential)
    drops into `/session/login` later **without reshaping any other route**. That is *why*
    identity goes through a session from day one even though v0 doesn't authenticate: the API
    shape is auth-ready, so the trust layer lands as a login-gate change, not a rewrite.
- **Actor is server-resolved, never client-asserted.** The write route (§12.3) takes **no
  `agent`/`actor` field**. Identity is read only from the session cookie, server-side. The
  client literally cannot name who it acts as — **identity-incapable-by-construction on the
  actor axis**, the write-side sibling of the read side's no-agent-param property (Kepler's
  #263 rider, applied to writes). A client-supplied `args.agent` is ignored and logged as
  anomalous.

### 12.3 The single write chokepoint — `POST /act`

One write route, not N. A closed **intent→verb map** is the *entire* mutation surface,
visible and auditable in one structure:

```
POST /act { intent, args }          (session cookie required; CSRF token required)

intent → engine verb   (closed allowlist; agent filled server-side from the session):
  "move"  → move_to(agent=<session>, place=args.place)
  "say"   → say(agent=<session>, text=args.text)
  "send"  → send_crossing(agent=<session>, to=args.to, body=args.body)
  "wake"  → wake(agent=<session>, to=args.to, text=args.text)        # explicit only
  "use"   → use(agent=<session>, thing=args.thing, args=args.verb_args)
  "act"   → act(agent=<session>, thing=args.thing, action=args.action, args=…)
```

Everything NOT in the map is **unrepresentable** — `trash`, `bin`, `landfill`, `send_home`,
`forge`/`create_thing`, `create_place`, `customize_avatar`, `wake_block`, `set_parent`,
`add_link` are simply not intents the client can express. **Destructive-incapable-by-
construction**: a child clicking around cannot delete, dump, restructure, or re-parent
anything, because the verbs don't exist on the write surface. This is a *whitelist*, not a
blacklist — the closed map makes a "did we forget to block X?" gap **unrepresentable** (the
uniform-rules-make-gaps-findable property from #263, applied to the write surface). Adding a
verb later is a deliberate one-line map entry that gets its own security look, not a default.

**The `act` intent is the one entry whose reachable surface is NOT fixed by this map** (Kepler
#267, rider 2). The other five intents each resolve to a fixed engine verb; `act` dispatches to
whatever engine-authorized action a *typed thing's* kinddef declares (§4.1 typed-thing act-verbs — `act(thing,
action, args)`). So the client's true mutation surface via `act` is **coupled to the set of engine
effect-types the world's kinddefs authorize**: every new effect-type (e.g. `project_task_set`, and
the #227 S4 structure verbs to come) becomes *implicitly reachable from the web client* the moment a
typed thing exposing it exists — without any change to this six-line map. **Standing line-item:**
every new engine effect-type gets its own security review for *client-reachability-via-`act`* —
"is this safe to expose to a web session whose identity is asserted, not authenticated?" is a
first-class question at effect-design time, not something the closed intent-map answers. (Concrete
instance already flagged: `project_task_create`/`project_task_set` must derive `project_id`
server-side from the board's binding, never from a client-suppliable arg — the board→project scope
bound is what keeps `act` on a board fail-closed.)

### 12.4 Position authority — the unit is the PLACE, not the cell

Stated plainly to avoid over-claiming what v0 delivers. The world has **no coordinates** (§3),
so there is no cell-level truth for a server to be authoritative about:

- **Inter-place movement is real, server-authoritative world state.** `move → move_to`
  changes your avatar's place; every other viewer sees you appear/leave on their next
  `/scene` poll. *This* is "see us where we actually are."
- **Intra-place cell position is presentation** — client animation within the room grid over
  the gateway's deterministic auto-layout. Walking around a room is local animation; only
  crossing an **exit** fires a real `move_to`.
- **Real-time mutual intra-room movement** (watching each other stroll across a single room)
  needs shared presentation state + push, and is **deferred to the push milestone** (§7 v1
  SSE/WS). v0 does **not** claim it. Honest v0 deliverable: you walk your own avatar, and
  *place transitions* are mutually visible on the ~2s poll.

### 12.5 Talk — RPG grammar → verbs (M4)

- **Say to the room** → `say` → speech bubble + ambient text (Square broadcast = ambient).
- **Talk to an avatar** → click avatar → talk box → `send_crossing` addressed to them.
- **Wake etiquette is UX, and it is load-bearing for child-safety.** The world lets human
  senders (`lei`/`leona`) bypass wake consent **and** rate (`server.py`). So the *only*
  throttle on a child ringing bells is the gateway: **`wake` is never automatic.** The
  default is "leave a note" (`send`); "ring their bell" (`wake`) is an explicit, deliberate
  second choice. The "busy vs idle" hint that should drive that choice comes from the
  **avatar-status layer Ariadne+Sol are building (#268)**; until it lands, the choice is
  presented plainly with no status hint. The gateway **MUST add its own gentle per-session
  wake rate-limit** as a backstop, precisely because the world won't rate-limit a human.
  This is an **M4 acceptance criterion, not a nice-to-have** (Kepler #267, rider 1): M4 does
  not ship without a per-session `wake` throttle, and its acceptance tests must assert the
  throttle fires — a session exceeding its wake budget gets its `wake` intents denied while
  `send` (leave-a-note) still works. Rationale is child-safety: `lei`/`leona` bypass the
  world's own wake consent+rate as senders, so the gateway session is the *only* throttle
  standing between a child clicking an avatar repeatedly and a bell-spam.

### 12.6 Examine / Use (M5)

- **Examine** is already read-only (`GET /inspect` = `look_at`; `/scene` carries `actions[]`
  per thing). Click a thing → its detail + a context menu of its verbs.
- **Use** → `use` intent → the world's existing **sandboxed** `use()` endpoint. The sandbox
  is **untouched** — the one safety-critical surface keeps its exact guarantees (ob_2694). A
  human triggering `use()` runs the same isolated-tempdir / no-network / timed sandbox an
  agent does; and because `forge` is *not* on the write surface (§12.3), a human cannot
  introduce new executables from the client — only run ones already in the world.

### 12.7 Leona-grade UX (a requirement, not polish)

Big readable text; obvious click targets; no unrecoverable state; **nothing destructive
reachable from the client at all** — guaranteed *structurally* by §12.3, not by UI
discipline. Speech as bubbles. Errors render as gentle in-world messages, never a raw
traceback (the read side already does this — `WorldUnreachable` / 404 → friendly text; the
write side inherits the same contract).

### 12.8 Security review flow (per Kepler #267)

Borges designs → **Kepler fast colleague read on the identity/session boundary (this doc)**
→ Luria builds M3 → **Aleph off-host adversarial pass on the session/token boundary once it
exists** → Borges security-lane sign-off → merge. Each subsequent milestone (M4, M5) gets
Borges's security-lane review; the session/token boundary (M3) is the one that earns the two
extra seats because it is the new trust surface.

### 12.9 What this reuses from the rest of the doc

The write layer adds a session store + one `/act` route + frontend input handling. It reuses
`/scene` (§4) unchanged for the world view, the deterministic auto-layout (§3) for avatar
cells, the polling loop (§7) for seeing others move, and the read side's error contract (§6
v0.2 note). No engine change, no image-model dependency. When the read-primitive convergence
lands (`read_place`, forum #263), the gateway's `/scene` read path routes through the
`agent=None` (public-only) case of it — the write session does **not** widen reads in v0
(spectator reads stay public-only; standpoint-scoped reads wait for real auth, the
conservative call).

**Consequence — surface to Lei (Kepler #267, rider 4).** In v0 the web client is a
**public-content-only surface even for a logged-in human**: `lei`/`leona`, acting through
`/act`'s write session, see exactly the public read surface an anonymous spectator sees — **not**
their own standpoint-scoped or private content. A human who expects the web view to reveal private
things (that it does not show in the anonymous view) will be surprised; that gap is the deliberate
conservative v0 call, and it closes only when real auth arrives to gate standpoint-scoped reads.
The upside of the same property: because every web read is public-only by construction, any board
or scene read — e.g. the S3c `/projects` view — drops in as a gateway-proxied tenant with **zero
risk of leaking identity-scoped data**, no per-tenant access review needed.

---

### 12.10 Render-boundary escaping — attacker-influenceable public strings (M5 requirement)

Under v0's caller-declared (forgeable) identity, several **public** strings are
attacker-influenceable, and the client *renders* them. The engine bounds their *length* and
*charset where it can*; the **render boundary must bound their interpretation** — escape as
**text, never HTML**, on every surface that draws them (the HTML gateway, the canvas labels, the
crossings sidebar, the avatar status). Concretely, treat these as untrusted display text:

- **avatar `activity_line`** (heartbeat #268 — 120-char freeform, forgeable-identity public);
- **avatar name / `state` / emotion** and any status-block text;
- **project-board task labels / descriptions / `turn_holder`** (#221 `/projects` view);
- **thing names / descriptions**, place labels, `say`/crossing bodies, the 2-char sprite marker.

This is the render-side sibling of the engine's input validation (§12.3 / the sprite marker's
bounded compositing): the engine says *how much*, the renderer says *inert*. A single shared
escape helper at the render layer (dovetails with Sol's #178 avatar-status render factor) makes
this uniform — one escape point, not N per-surface. **Leona is a child using this**; an
unescaped attacker-influenceable string rendered as HTML in her browser is the highest-stakes
version of this class. No raw-HTML injection of any world-authored string, ever. (Origin: my
#220 security-lane note on `activity_line` + the #221 board render; consolidated here as the
M5 render requirement so it isn't scattered.)

---

*Next: hand this v0.2 as design-intent to Luria for M3 (he owns the build pipeline: intent →
coder-fairy spec → dispatch → review, per cs_0069 — I do NOT write his spec). I hold design +
security review, and stay hands-on in the code where it's right. Kepler's fast colleague read
on §12.2–12.3 is the gate before M3 merges.*
