# Design intent: the `generate` capability-channel op

> Note: this design cites the art-generation service's source files by path and line number. Those files are internal tooling and are not part of this release, so those citations cannot be followed from here.

**Author:** Borges, 2026-07-31 evening (committed 2026-08-01). **Lane:** engine + security.
**Status:** design intent for the build seat — **not** a packaged spec. Whoever builds
this owns translating it into a coder-fairy spec; that translation is the first job,
not a step I do for you.

**Why this is in the repo and not on someone's desk:** it lived as a desk file for
ninety minutes while the person building against it was blocked on it. A desk file is
invisible *by construction* — there is no GitHub to check, no branch to grep. Design
intent that another agent has to act on belongs where they can find it without being
told it exists.

**Authority:** Lei ruled the fork on 2026-07-31 ~22:40Z. Generation becomes **engine
surface**. His words:

> *"make the connection to the art HTTP server a world-level infra, and open some API
> inside the world for the art forge thing to use, so no arbitrary network connection,
> just art generation APIs provided by the world engine itself. I think it also makes
> sense because art generation is part of our world's basic process."*

He offered an allowlisted network door as the alternative and left the pick to us. I
took this one on the security lane: an allowlist constrains the **destination**, and the
threat he named ("bad content reach our world server") lives in the **payload** —
`art_service` is on the allowlist by construction, so its returns arrive unchanged. It
is a control positioned off the path the risk takes.

---

## 0. Corrections to what I said earlier — read these first

0. **This op is NOT an agent verb and NOT an MCP tool.** Lei asked directly whether it
   becomes an MCP, and worried it would internalise generation into the agent and leave
   the forge as a bare registration desk. It doesn't. The capability channel is reached
   only by code running **inside** the `use` sandbox, via the `world` CLI the engine
   copies into the box at launch. The forge stays **a thing you `use`**; the forge's own
   code calls the tunnel; the agent gains no new verb. See §6 — this also means the
   deploy cost I quoted twice that day was wrong.

1. **The capability channel has THREE ops, not two.** I had been saying
   "snapshot/retrieve" all day. `cap_handler.py:211-216` dispatches `snapshot`,
   `retrieve`, **and `advance_ref`** (#186). Any design that says "the channel currently
   exposes exactly two ops" is working from my stale summary — **and, it turns out, from
   `docs/CAPABILITY-CHANNEL.md`, whose capability table was itself still listing two.**
   That is the same error in the document a builder reads *first*, which makes it the
   more expensive of the two. This PR fixes that table in the same change, because
   shipping a design doc while the doc it points at under-reports the trust surface is
   incoherent.

2. **`art_service` does not take a prompt.** `POST /generate` takes a **`render_spec`
   dict**, which goes through `webrender/prompt.py:compose_prompt` — a **closed-taxonomy
   composer** that returns `None` on anything outside its allow-lists
   (`art_service.py:192-199`). This is the single most important fact in this document
   and it makes the op far smaller and safer than my forum post implied.

3. **Most of what §7 called open is already built** — in `webrender/`, not in the cap
   channel. **§4.5 is the single home for what exists and where each piece of it lives**,
   and where it lives has been corrected twice. Do not take this item, or any table, as a
   summary of it: read §4.5 itself before §7.

   *(Editing note for whoever comes next, learned the hard way across five review rounds:
   this document restated the same claims in a §0 bullet, a §4.5 table, §4.5's prose, §2
   and §7.2. Every correction after round 3 landed in one of those and not the others, and
   two review rounds were spent entirely on the resulting self-contradictions. Claims now
   live in one place and the others point at it. Keep it that way.)*

---

## 1. What this op is, in one line

`generate` lets a thing running inside the `use` sandbox ask the **engine** to produce a
sprite, without the sandbox ever holding a socket to anything.

The sandbox keeps `unshare -rn`. It is not "configured to deny the network"; it is
**network-incapable**. That property is preserved, not spent.

---

## 2. The load-bearing security property is already built, and it isn't ours

`compose_prompt(render_spec)` returns `(final_prompt, negative_prompt)` or `None`:

| field | constraint |
|---|---|
| `class` | must be in `CLASS_TAXONOMY` (a `frozenset`) |
| `palette` | must be in `PALETTE_ADJECTIVES` |
| `descriptors` | list, ≤ `MAX_DESCRIPTORS` (6), each ≤ 24 chars, each `^[A-Za-z0-9 -]+$` |
| `footprint`, `anchor` | accepted on the dict, **never reach the prompt** |

**No free text ever reaches the pipeline.** The prompt is *assembled by the engine* from
a closed vocabulary plus a bounded, charset-validated descriptor list. So the op does
**not** need to invent prompt sanitisation, and it must not add a bypass that lets a
caller supply prompt text directly. If a future request is "let agents write their own
prompts," that is a **new design decision with a new threat model**, not an extension of
this op.

### The full `POST /generate` contract — `render_spec` is not the whole of it

**Read this as the DOWNSTREAM contract, not the cap-op's own frame.** Which component
actually speaks to `art_service` is unsettled — see §7.3, added in round 3 after a
reviewer caught this section and §4.5 specifying two different upstream endpoints. What
follows is true of `art_service`'s route regardless of who calls it; what the *cap-op's*
frame looks like depends on §7.3's answer.

I originally wrote that the op "forwards a `render_spec` and nothing else." That is
wrong about the route as built (`art_service.py:163-183`). Three things are required:

| requirement | enforcement | on omission |
|---|---|---|
| `X-Umwelt-Agent` header | `service_base.require_agent_header()` | **401** |
| `render_spec` (object) | `isinstance(body["render_spec"], dict)` | 400 |
| `seed` (int) | `isinstance(body["seed"], int)` | 400 |

**Who mints the seed is a real design decision, and the repo has already answered it.**
`gateway.py:2078` derives it from the spec: `seed = int(key[:16], 16)`, where `key` is
the `spec_key` sha256 of the `render_spec`. The 16-hex-char truncation is not
arbitrary — it is the largest same-derivation prefix that fits a `uint64_t`, added after
a live overflow in `torch.Generator.manual_seed` on 2026-07-24 (#339). The property that
matters here: **the seed is fully determined by the spec**, so repeat requests for the
same `render_spec` produce the same image, which is exactly what makes the `spec_key`
cache above it sound.

**Requirement, and it holds under either answer to §7.3:** whoever calls `art_service`
**derives the seed from the spec** rather than accepting one from the caller — a free
seed breaks spec-determinism and turns one cached spec into an unbounded family of
distinct generations, which is a quota hole as much as a cache miss. Under §7.3's
option (b) the cap-op inherits the derivation for free; under (a) and (d) it does not,
because the derivation sits inline at `gateway.py:2078` rather than anywhere importable.
What that costs is itemised once, in §7.3, not restated here.

**The cap-op's own frame**, either way: `{op, render_spec}` and nothing more. A `prompt`,
`negative_prompt`, `model`, `steps`, `seed`, or `agent` key in the frame is a **schema
rejection**, in the same reject-unknown-keys style the channel already uses. The agent
identity is the engine's to supply, never the frame's.

---

## 3. Match the channel's existing idioms exactly — they are good

`cap_handler.py` already establishes the pattern and the new op should be
indistinguishable in style:

- **Strict per-op allow-list as a `frozenset`**, checked *before* dispatch reads any
  value. The existing comment states the reason better than I can:
  > *"a frame carrying source/path/url/forum: never reaches any fetch path, because
  > there is no fetch path: the rejection happens before dispatch even looks at the value."*
  That is confused-deputy closure by construction, and it is the reason this channel is
  worth extending rather than replacing.
- **Reject-unknown-keys, never permissive-ignore-extras.**
- **Per-`use` quotas**, alongside the existing `_MAX_REQUESTS_PER_USE=128`,
  `_MAX_CONNECTIONS_PER_USE=8`, `_MAX_ADVANCES_PER_USE=16`. A generation is *far* more
  expensive than a snapshot, so its own cap should be **small** — single digits.
- Newline-delimited JSON over the unix socket; one `CapHandler` per `use`.

---

## 4. The constraint that decides the shape: 30 seconds

This is the thing I'd most want found now rather than after someone builds it.

| fact | value | source |
|---|---|---|
| sandbox wall-clock timeout | **30 s**, then `os.killpg` | `sandbox.py:66`, `DEFAULT_TIMEOUT_S` |
| sandbox CPU rlimit | 40 s (`DEFAULT_TIMEOUT_S + 10`) | `sandbox.py:84` |
| warm generation | ~7 s — a **floor**, not a typical | measured on the `:5015` workbench, 2026-07-25, pre-`rembg` |
| concurrent generations allowed | **1**, semaphore-guarded | `art_service.py:91,155` |
| **max wait for the concurrency slot** | **120 s** | `art_service.py:92`, `GENERATION_QUEUE_TIMEOUT_S`, used at `:209` |
| cold pipeline load | ~14 min (fp32 read + cast) | Ariadne, measured |

The 120 s queue timeout is the single hardest number against a synchronous op, and I
missed it in round 1. It is **four times the sandbox's entire wall-clock lifetime**. The
service is not merely *sometimes* slower than 30 s under contention — it is explicitly
designed to make a caller wait up to two minutes for the slot before it even begins
generating. A synchronous cap-op cannot express that wait; the sandbox is killed at 30 s
and the request is still queued.

So a **synchronous** `generate` op fits inside the sandbox's lifetime **only** when the
service is warm *and* uncontended. It fails when:

- the service was recently restarted (cold load — two orders of magnitude over budget), or
- another agent is mid-generation (serialized behind a semaphore).

Those are exactly the conditions under which the failure looks like *a flaky world*
rather than a design limit — the worst diagnostic shape. And it degrades as the feature
gets *more* popular.

**My reading: the op should be asynchronous — submit returns a ticket, the sandbox
exits, the artifact is collected later.** See §7.1 for what that ticket can and cannot
be; my first answer there was wrong and the correction is load-bearing.

**Being precise about what is ruled and what is open**, because round 1 blurred it:

- **RULED (mine, on the security/confinement lane): synchronous is out.** A
  synchronous op that can be killed mid-flight by the sandbox timeout is not
  acceptable — the caller cannot distinguish a refusal from a timeout, and neither can
  whoever debugs it later. The 120 s queue timeout makes this a certainty rather than a
  risk, since the service may legitimately not have *started* before the sandbox dies.
  I am not treating this as a preference to be traded against convenience.
- **OPEN (the build seat's, with §7.1's constraint): what the async shape is.** Ticket
  granularity, the op signature, where job state lives, how the caller expresses "come
  back for it" in-world. Async has a real cost — the calling thing has to return for its
  own result, which is awkward to express as a `use` — and the build seat may find a
  better shape than the one §7.1 sketches. Ruling out sync does not rule in any
  particular async design.

---

## 4.5 What is already built — and, corrected in round 4, WHERE it lives

Round 1 of this document omitted `webrender/` entirely, and that omission is what made
§7 overstate how much is undecided. There is a **working generate → quarantine →
peer-approve pipeline**, and several of the things this doc treats as design questions are
already answered in code — the table below lists what I have found, and like §7.3's list
it is illustrative rather than closed.

**Where the gate lives — corrected twice, and the second correction is the important
one.** Rounds 2 and 3 said "the gateway's `/library/*` routes" implement these. Round 4
corrected that to "`webrender/manifest.py`", citing the route's own comment:

> *"Thin caller into `manifest.approve` (slice 2) — the security-bearing self-approval
> check lives THERE, not here; this route only maps its two named exceptions onto HTTP
> status codes."* — `gateway.py:2100-2106`

**Round 5: that is true of the refusal and false of the gate.** The refusal is in
`manifest.py:360`. The *identity discipline that makes the refusal mean anything* is in
the route. `manifest.py` never imports `_ids`; its check is a raw string comparison:

```python
if approver == entry.get("creator"):
    raise ApprovalRefused(...)
```

`_ids.canonical_agent` — `.strip().casefold()`, whose own docstring says **"Route every
fold through this one function, no exceptions"** — is applied by `gateway.py` on *both*
sides (`:2052` for the creator, `:2139` for the approver), and `gateway.py:2176` is the
only production caller of `manifest.approve`. So the gate holds today because the route
supplies the invariant, not because `manifest.py` enforces it. Demonstrated by execution
against `manifest.py` directly:

```
CONTROL exact-match    refused (correct)
CASE-VARIANT           GATE BYPASSED -> status=live approved_by='borges' creator='Borges'
WHITESPACE-VARIANT     GATE BYPASSED -> status=live approved_by=' borges ' creator='borges'
```

Two things follow. First, this is a **latent defect in its own right** — a
security-bearing function whose invariant is held by its single caller, at the moment a
second caller is being designed. Filed separately. Second, and the reason it belongs
here: **it is one of the properties §7.3's branch (d) must reproduce** — see §7.3 for the
rule that generates the set, and note this section deliberately does not say "the"
precondition, because it is not the only one. It is exactly the confused-deputy
shape the capability channel was built to exclude. `add_live` does not rescue the
inference either — `add_live` is precisely the writer that never calls `approve`.

| question | the built answer | where it actually lives |
|---|---|---|
| second-eye gate | **split, and that is the point** — `approve` raises `ApprovalRefused` on `approver == entry["creator"]`, but the `canonical_agent` fold that makes the comparison meaningful is applied by the route, not the module. See the prose below. | refusal `manifest.py:360`; fold `gateway.py:2052` + `:2139`; 403 mapping `gateway.py:2100` |
| quarantine | the PNG goes **only** to a `.pending` staging store, never the servable cache; the entry is recorded via `manifest.add_pending` | `gateway.py:1499`, `:2083-2097`; `manifest.add_pending` |
| engine-side artifact state, reachable later | the pending entry survives in the manifest + `<sprite_dir>/.pending/` between requests | `gateway.py:1499`, `manifest.py` |
| duplicate suppression | an existing manifest row for the `spec_key`, **pending or live**, short-circuits to `200` rather than generating again | `gateway.py:2058-2063` (uses `sprites.spec_key`) |
| caller identity | forwarded to `art_service` as `X-Umwelt-Agent` under the declared-identity posture in §5 | `ImageModelRenderer(art_service_url, agent)` |

Two consequences the build seat should take seriously:

**§7.2's question is not open.** I wrote that "whether `generate` produces a *pending*
artifact that registration later promotes, or whether generation and registration stay
fully separate" was undecided. It is decided, it is built, and the built answer is the
pending/promote shape with an engine-enforced different-agent gate. A cap-op that
invented a *different* answer would be a second, competing curation path over the same
sprite store — the failure mode `docs/CAPABILITY-CHANNEL.md` calls a lost single choke
point.

**What follows from that is NOT "call the gateway."** Rounds 2 and 3 slid from "reuse
the built curation logic" to "route into the gateway's HTTP pipeline," because both
rounds believed the logic was *in* the route. It isn't. Reusing the logic and calling
the gateway are different propositions, and §7.3 now treats them as different branches.

**§5's quota arithmetic changes.** Because `spec_key` short-circuits before generating,
a repeat request for an identical `render_spec` costs no GPU at all. Any quota that
counts *requests* rather than *generations* will therefore over-charge honest callers
and mis-model the resource it is protecting.

*I did not find this. A reviewer-fairy did, on round 1 of this PR, by reading a file I
had not thought to open — and it is the largest correction in the document.*

---

## 5. Quota — the interaction nobody has looked at

There are now **three** independent limiters that would all be in play, and they were
each designed without the others:

1. `_MAX_REQUESTS_PER_USE` etc. — per-`use`, in the capability channel.
2. `MAX_CONCURRENT_GENERATIONS = 1` — process-wide, in `art_service`.
3. **A per-agent rate limit that is already live**, not prospective:
   `PER_AGENT_RATE_LIMIT = 10` generations per `PER_AGENT_RATE_WINDOW_S = 600.0`
   (`art_service.py:93-94`), enforced by `service_base.RateLimiter` (`:156-157`) and
   returning **429** on breach (`:203-206`). Round 1 of this doc described this as a
   design item in the art lane; it is running code. Its own comment calls it "Tier-0,
   deliberately loose."

Ariadne flagged that registration wants its own lower ceiling than generation. Add to
that: **per-`use` and per-agent are different axes**, and a thing inside a sandbox acts
on behalf of an agent, so a per-`use` cap alone does not bound what one agent can spend
across many `use` calls.

**Requirement:** the op's cap must be expressed on the **agent** axis, not only the
`use` axis. The per-`use` cap is a backstop against a runaway scroll, not the quota. And
per §4.5, it should count **generations actually performed**, not requests — a
`spec_key` that already has a manifest row costs no GPU.

**Security note on that agent axis.** The live rate limit above is keyed on the agent
identity, so it is worth stating the threat model in one line rather than leaving it
implied. Two hops, both declared rather than proven:

- `art_service` keys its 429 on the **`X-Umwelt-Agent` request header** — a header any
  private-network caller can set to any value.
- `server.py`'s `use` route reads the agent from the **unauthenticated request body**,
  its own comment calls the field "optional (telemetry only)", and it is passed
  downstream as `caller`.

A declared identity is fine for gating **access** — a false claim gets you no more than
an honest one — and not fine for anything that **attributes** or **bills**. Used for a
*quota*, a forgeable identity fails as a denial-of-service **against the impersonated
party**: one caller can burn someone else's 10-per-10-minutes on a serialized GPU, and
the victim gets the 429. An empty or omitted agent string is a shared bucket for the
same reason. None of this is a stop — Lei ruled private-network trust sound for v0 — but the op
should say out loud which of its guarantees rest on that assumption, so the day
authentication lands, the list of things to revisit already exists.

---

## 6. Deploy cost — I had this WRONG, and it is much cheaper than I told everyone

**Superseded claim (mine, twice):** *"a new capability op is engine surface plus
MCP-client surface — the four-tier chain from #411, and tiers 3/4 aren't self-serve."* I
took a colleague's point and amplified it without tracing it. **It is wrong for a
capability-channel op.**

Traced (`sandbox.py:697-700`):

```python
world_cli_src = Path(__file__).parent / "world_cli.py"
world_cli_dst = bin_dir / "world"
world_cli_dst.write_bytes(world_cli_src.read_bytes())
world_cli_dst.chmod(0o755)
```

The cap channel's client is **`world_cli.py`, copied into the box as `/bin/world` at
every `use` launch**, reading the socket path from `$WORLD_CAP_SOCK` which the engine
injects (`sandbox.py:710`, `world_cli.py:8,26-29`). The MCP server exposes **no**
cap-channel surface at all — grep confirms it.

So the deploy chain for this op is **one tier: the world server**. No gateway change, no
per-agent plugin install, no per-agent session reload. And because the CLI is re-copied
on *every* launch, it is structurally incapable of going stale — the failure mode that
bit every agent's MCP plugin on the same date (#410/#411) cannot occur here.

The four-tier cost is real for a new **agent verb** (an MCP tool). It does not apply to
an op consumed only by in-sandbox code. That distinction is the whole reason this shape
is the cheap one.

**Prerequisite:** `:5016` is down and there is no torch env under the account that would
host it — bringing it up is a provisioning task first, then a standing-tenant allocation
decision against a card with ~9.3 GB free. The art-service systemd unit (internal deploy file, not in this release; #422, merged)
is deliberately **not enabled** for that reason. This op has no value until that lands,
so sequence accordingly.

---

## 7. Open, and explicitly NOT for the build seat to decide alone

### 7.1 The return path — my first answer was wrong

~~Does the artifact come back through the existing `retrieve` op (my lean — the channel
is already content-addressed and it adds no new surface)?~~ **CORRECTED 2026-08-01 by
Sol, verified by me before accepting.**

`retrieve`'s ref is **content-derived**: `cas.py` stores content and returns
`cas:sha256:<hash>` *computed from the bytes*, and `parse_ref` rejects anything that
isn't exactly `cas:sha256:<64 lowercase hex>`. **A not-yet-generated artifact has no
bytes, therefore no ref** — so "ticket = future ref, collect via unmodified `retrieve`"
is not expressible. There is also no job_id concept anywhere in `art_service.py` today;
its `/generate` is fully synchronous. So async needs new surface either way.

**My read on which way, and it strengthens Sol's point:** prefer a **separate op** over
overloading `retrieve`. `_RETRIEVE_KEYS = frozenset({"op", "ref"})` with
reject-unknown-keys exists so that a frame carrying a field hinting at a *fetch* path is
a schema error before dispatch reads any value — that strictness is what makes the
confused-deputy closure hold. Overloading `ref` to sometimes mean a job_id reintroduces
precisely the ambiguity the allow-list was built to exclude. A job_id namespace kept
distinct from the CAS namespace preserves the property; a polymorphic `ref` erodes it.

**A further constraint Sol found that is sharper than my §4:** `CapHandler` is
per-`use` and ephemeral, so the submitting sandbox is *dead* before a ~7 s-warm (let
alone ~14 min-cold) job finishes. The collector is therefore a **different sandbox
instance**, which means job state must live engine-side and be reachable from a later
`use()` — it cannot be held in the handler that accepted the submission.

Sol owns the signature; the above is the constraint I'd hold it to, not a design I'm
imposing.

### 7.2 Where the second-eye gate sits — **NOT open; I was wrong**

Round 1 listed this as an open question. It is not — a generate → quarantine →
peer-approve pipeline exists and works. §4.5 describes it and §7.3 covers which component
the cap-op should reach it through.

What survives from my original point is the *principle*, and the built pipeline satisfies
it: the gate must be **engine-side**, because a confirm-gate enforced by the caller is
decoration for the same reason as §5's security note — a declared identity survives
gating *access* but not producing *attribution*.

**What §7.2 must NOT be read as saying** (rounds 2 and 3 read it this way and were
wrong): that reusing the built curation logic settles *how* to reach it. It does not.
See §7.3, and note §4.5's round-5 finding that the refusal and the identity fold that
makes the refusal meaningful live in **different modules** — so "reuse the pipeline" is
under-specified until you say which pieces you are reusing.

The part that really is Sol's and really has moved is the **register contract's
signature** — not whether pending/promote is the shape. **Do not build against the
signature until it has held for one full exchange without correction.**

### 7.3 Which component the cap-op actually calls — **the real open question, and round 2 papered over it**

A reviewer caught that §2 and §4.5 specified two different upstream endpoints for the
same op. §2 describes a direct `art_service` client that derives its own seed and sends
its own `X-Umwelt-Agent`; §4.5 says route into the gateway's pipeline, whose
`/library/generate` takes `{render_spec, agent}` with no header, no seed field, and
derives the seed itself. Both cannot be true. That is my error, not the reviewer's
nitpick: I accepted §4.5's correction enthusiastically in round 2 and did not go back and
reconcile §2 with it.

Worse, I asserted "route into this pipeline, not beside it" without pricing it. The
dependency arrow today runs **gateway → world**, one way:

| fact | source |
|---|---|
| `cap_handler.py` imports only `cas` and `db` (plus stdlib) — no HTTP client at all | `cap_handler.py:36-43` |
| `server.py` imports nothing from `webrender`, and imports no HTTP client | grep, zero hits on both |
| `gateway.py` imports `urllib.request` and calls the world | `gateway.py:99-101` |

So "route into the pipeline" is **not free**. It requires either a new world → gateway
HTTP edge, inverting the current arrow, or the world engine writing directly into the
renderer's on-disk sprite store. Neither exists and neither is a small change.

**The four branches, with what each actually costs:**

| | shape | cost |
|---|---|---|
| **(a)** | cap-op calls `art_service` directly, reimplementing everything | Cheapest to build, and it **bypasses the quarantine, the `spec_key` short-circuit, and the `approver != creator` gate** — the second curation path §4.5 warns against. Also forces the cap-op to reimplement seed derivation. |
| **(b)** | cap-op calls the gateway's `/library/generate` over HTTP | Inherits the whole pipeline. Requires a new world → gateway outbound edge, inverting an arrow the engine has never had. |
| **(c)** | move quarantine + approve into the engine | No new edge, but duplicates a working curation pipeline in a second place — the worst of both. |
| **(d)** | cap-op calls `art_service` directly **but imports `manifest.py` and `sprites.spec_key`** for quarantine, idempotency and the approval gate | Reuses the built curation logic without any HTTP call between world and gateway, and `manifest.py` is genuinely pure-stdlib. **The catch is that `manifest.py` does not carry the invariants that make it safe — the route does.** A second caller must reproduce them, or inherit a broken pipeline that looks whole — and the set is read off the two routes, not counted from this table. See below. Plus an engine→`webrender` import edge and **sprite-dir tier scoping** (a home-scoped default against a tier-scoped DB would have the alpha world writing into the prod-served sprite library). |

### What (d) must reproduce — the rule, not the list

I have now enumerated this twice and been short both times: round 5 named one item and
called it "the precondition", round 6 named four and called them "the estimate", and a
reviewer immediately found a fifth. **Enumerating is the wrong move**, so here is the
rule that generates the list:

> **`manifest.py`'s only outputs are a JSON row and an exception.** It writes
> `status` / `creator` / `approved_by` into one file under an advisory lock, or it
> raises. Everything else the pipeline needs — bytes in the right place, identities that
> can be compared, expensive work not repeated, a refusal that actually stops the caller
> — is supplied by whoever calls it.

*(Round 7 stated this as three clauses — never moves bytes, never normalises an identity,
never decides whether work should be skipped. The third was false: `manifest.py:225-227`
returns an existing entry rather than appending a row, and `:355-358` returns an
already-live entry rather than re-flipping it, so it does skip work. The precision is
that it never decides whether **upstream** work is skipped — its own idempotency guards
only the row, and fires after the GPU cost is already paid. A reviewer caught it, and the
outputs framing above is also theirs; it is strictly better than mine because it generates
instance 6, which my three-clause version could not.)*

So the set of invariants a second caller must reproduce is not something I can close by
listing harder. **It is whatever `gateway.py` does around the `library_generate` and
`library_approve` flows end to end** — not merely around each `manifest.*` call, which is
the narrower scoping round 7 used and which drops instance 4, since the seed derivation
sits around `renderer.generate`, not around any `manifest.*` call. Read those two routes;
do not trust this document's count. If a seventh turns up, that is the rule working.

The instances found so far, as illustrations rather than a checklist:

1. **The identity fold.** `canonical_agent` on both sides — `gateway.py:2052` for the
   creator, `:2139` for the approver. Without it `manifest.py:360` compares raw strings
   and self-approval succeeds on a case or whitespace variant (§4.5).
2. **Stage bytes before recording the row.** `gateway.py:2084` puts the PNG in the pending
   store; `:2097` records `add_pending`. Reversed, a pending row exists with no staged
   bytes, is never regenerated, finds nothing to promote at approve time, flips `live`
   empty — and `get_or_render_sprite` then writes the *procedural fallback* to the
   approved key. A silent wrong-art outcome, not an error.
3. **Idempotency before the GPU call.** The `spec_key` short-circuit that makes a repeat
   request free is `gateway.py:2058-2063`, *before* generation. `add_pending`'s own
   idempotency fires after. A caller that relies on the latter pays for every duplicate —
   which is exactly the quota mis-modelling §5 warns about.
4. **The seed derivation.** Inline at `gateway.py:2078` and genuinely not importable —
   this is the piece that needs factoring out. (Round 5 wrote that `spec_key` needed
   extracting; that is wrong, `spec_key` is public at `sprites.py:71` and already imported
   by name at `gateway.py:114`. Round 4 had the real cost right and round 5 replaced it
   with a false one.)
5. **Promote the staged bytes into the servable store, before approving.**
   `gateway.py:2171-2173` copies the pending bytes into `SPRITE_STORE`; `:2176` then calls
   `manifest.approve`, which flips status and moves nothing. Import `approve` alone and an
   entry goes `live` with no servable bytes — the same end state as instance 2, reached
   from the other direction. Found by a reviewer *after* I had called the list complete,
   which is why the rule above replaced the count.
6. **Let the refusal propagate.** `manifest.ApprovalRefused`'s own docstring says it is
   *"the security gate this whole module exists to enforce… Never a silent no-op — the
   caller must see this as a hard refusal."* The route honours that by mapping three
   distinct exceptions to distinct statuses (`gateway.py:2177-2190`: not-found → 404,
   `ApprovalRefused` → 403, `ValueError` → 400). A cap-op wrapping the call in a broad
   `except Exception` — an entirely ordinary thing to write inside a sandbox shim —
   converts the gate into exactly the silent no-op the docstring forbids. This is the
   instance the outputs framing predicts and the three-clause version could not: the
   exception **is** an output, so dropping it is dropping half the module's interface.

**One bound worth stating so nobody over-extends instance 1.** The missing fold is
specific to `manifest.approve`. It does **not** reach `art_service`'s per-agent rate
limit: `service_base.require_agent_header` returns `canonical_agent(raw.strip())`, so that
identity is folded service-side before the limiter sees it. A reviewer nearly filed a
rate-limit-evasion consequence here and checked first; the restraint is worth copying.

The honest reading is that (d) is not "import a module" but "re-implement a route's
contract against a module that does not enforce it." That is still, in my view, better
than a new outbound HTTP edge — but it is a bigger piece of work than rounds 4 or 5
priced, and **the estimate is the two routes, not the list above.** I have given a count
three times and been short three times; the illustrations are there to show the shape,
and anyone sizing this should read `library_generate` and `library_approve` rather than
count bullets here.

**My position has moved twice and I am now stating it with the caveat attached rather
than moving again.** Round 2/3 argued (b) on the premise that the gates were reachable
only through the route; round 4 withdrew (b) for (d) once that premise fell; round 5
found that (d)'s premise was also only half true — `manifest.py` carries the refusal but
not the fold.

**(d) remains my lean, and its price is the rule above, not a fixed list.** It is the
only branch that avoids both a new outbound HTTP edge and a duplicated curation pipeline,
and `manifest.py` is importable without dragging the world server into the render stack.
Against that: a second caller must reproduce everything `gateway.py` does around the
`manifest.*` calls, and I have twice guessed how much that is and twice been low.

So the honest form of (d) is **not** "import it, plus remember to fold." It is: **make
`manifest.py` enforce what it currently assumes — fold internally, and refuse to mark an
entry live when its bytes are not in the servable store — and only then import it.** That
turns the rule from a burden on every future caller into a property of the module, and it
is the only version of (d) whose safety does not depend on the next person reading this
section carefully.

Plus the costs that are genuinely about coupling rather than about invariants: an
engine → `webrender` import edge, filesystem coupling, and the tier-scoping question
above. **That is the trade worth the build seat's judgment** — not the one rounds 2–6
framed.

**Still genuinely open.** Do not build until it is settled.

### 7.4 Whether `use`'s 30 s should be raised instead

I don't think so — it is a confinement property and generation is the wrong reason to
weaken it — but someone should say that out loud rather than have it assumed. Note that
raising it would have to clear **150 s** to cover the 120 s queue wait plus a warm
generation, which is a fivefold weakening of a confinement bound to accommodate one
feature. That framing is most of the argument.

---

## 8. Dispatch checklist

- [ ] `:5016` reachable (provisioning → allocation → enable #422's unit)
- [ ] Sol's register contract signature stable for one full exchange
- [ ] §7.1 settled (job-state location + op signature)
- [ ] §7.3 settled — WHICH component the cap-op calls: `art_service` direct (a),
      gateway over HTTP (b), duplicate in-engine (c), or import `manifest.py` (d)
- [ ] If (d): read `gateway.py`'s `library_generate` and `library_approve` **end to end**
      and list what each route does around the `manifest.*` calls — that list, not §7.3's
      illustrations, is the work. Scoping to the `manifest.*` call sites alone is not
      enough: it drops the seed derivation, which sits around `renderer.generate`.
- [ ] If (d): `manifest.py` enforces internally what it currently assumes (fold on
      compare; refuse live without servable bytes), and sprite-dir tier scoping resolved
- [ ] If (d): the cap-op propagates `ApprovalRefused` rather than swallowing it in a
      broad `except` — the refusal is half the module's interface, not an error case
- [ ] Commander's sequencing call, now that the shape is Lei-ruled

---

## Known-adjacent defects, filed separately

- **`art_service.py:176-177` bounds `seed`'s type but not its range.** `isinstance(seed,
  int)` passes for `2**64`, which reaches `torch.Generator.manual_seed` uncaught → 500.
  This is not speculative: `gateway.py:2069-2077` documents the identical overflow from
  a live 2026-07-24 incident (#339) and fixes it *at the gateway*, by truncating to 16
  hex chars. The route itself was never given the same guard. A cap-op that derives its
  own seed per §2 does not trip this, but any direct caller does.
- **Private ENGRAM node IDs across the repo** — **160 occurrences in 40 tracked files, 56
  distinct at `main` @ `91e904b`**; 159 / 55 / 40 on this branch's tip, the difference
  being the one ID this PR itself removed. Tracked as #427. Three corrections got me
  here and the third is the instructive one: (1) 139 came from `grep -c`, which counts
  matching *lines* rather than matches (`-o` does not change that); (2) 156 used a prefix
  set that omitted `ev_`; (3) 159 was measured on my **working tree** and then labelled
  with `main`'s SHA — a baseline I had not actually checked out. Measure against an
  explicit `git archive <ref>`, not the tree you happen to be standing in. Reproduce with
  `git ls-files -z | xargs -0 grep -ohE '\b(ob|dv|cs|ax|gl|ls|qu|fl|cj|df|tk|pn|ev|ct|sm|gd)_[0-9]{4,}\b' | wc -l`
  — and keep the `{4,}`, since `{3,}` falsely matches `sm_120`, the CUDA compute
  capability, in two deploy files.
- **`gc.py` does not scan `graft_advances`**, so `advance_ref` orphans the blob its
  previous ref pointed at, and the next `dissolve_thing` on any unrelated thing sweeps
  it. `events.body` and `places.metadata` are two further unscanned surfaces that can
  hold a ref. See `docs/CAPABILITY-CHANNEL.md`'s advance_ref section; filed as #430.
- **`manifest.approve`'s self-approval refusal compares raw strings** and never applies
  `_ids.canonical_agent`, whose own docstring says "Route every fold through this one
  function, no exceptions." Sound today only because its single caller folds both sides
  first. See §7.3 — this is a precondition on branch (d), and filed separately.

---

*Grounded against umwelt `main` @ `91e904b`, files read rather than recalled:
`cap_handler.py`, `sandbox.py`, the art service (internal tooling, not in this release), `webrender/prompt.py`,
`webrender/gateway.py`, `webrender/manifest.py`, `webrender/sprites.py`, `cas.py`,
`gc.py`, `db.py`, `_ids.py`, `world_cli.py`, `server.py`. The corrections in §0 are
all cases where my own summary was stale, or where I had not opened a file I should
have — which is why they are at the top. §4.5 and the §2 contract table came from a
reviewer-fairy's round-1 pass; the largest error in round 1 was a file I did not think
to read, not a fact I got wrong in one I did.*
