# Art Generation Pipeline — M4 `ImageModelRenderer` (design of record / DoR)

**Author:** Borges (design + security lane) · **Date:** 2026-07-23 · **Status:** DoR — **design-complete, no remaining Lei gate.** All four open questions resolved (Lei answered Q1 deployment + Q2 sprite-size on 2026-07-23; Kepler answered Q3 taxonomy; Q4 charset is review-time). Ready for Luria build.

**Relationship to existing docs.** This is the **generation-half** design of record for M4. It plugs into the stable interface in `RENDER-PIPELINE-AND-SECURITY.md` **Part A** (render_spec, prompt composition, `Renderer` protocol, pipeline, per-instance customization, avatar flow) and honors **Part B** security + its M4 refinements captured in ENGRAM **dv_1159** (supersedes dv_1154). It does **not** re-spec Part A; it fills in the `ImageModelRenderer` that Part A.3 leaves as "the swap seam," plus the population + moderation + serve flows Part A sketches.

**Empirical ground (ax_0010 — spec embodies what the PoC *does*, not what I imagine).** The pipeline core is proven: `borges-desk/pixel-art-poc/` — SDXL-base-1.0 + LoRA `nerijs/pixel-art-xl`, ~5–6s/gen on the Chameleon 5090, seed-deterministic, the exact `FINAL_PROMPT` format below, postprocess via palette-early compose + `rembg`. Lei validated the output ("charming", "a decent artist on our local machine", dv_1150/ob_2951). **The Art Tuning Workbench (`chameleon.lan:5015`) is Lei's *private* tuning tool** (systemd user service, no auth, single trusted LAN user) — it is the PoC and Lei's ongoing design surface, **NOT** the agent-facing production path M4 builds. This spec designs the production path.

---

## Architecture — the art-library server (Lei's 2026-07-23 consolidation; answers Q1)

Lei's design of record (2026-07-23, recorded `ob_2959`): the art library is **one dedicated, header-required private-network service on the Chameleon 5090** that owns the entire art lifecycle in four roles at once. Its caller-declared attribution header is not authentication —

- **generator** — SDXL-base + `pixel-art-xl` LoRA (§3);
- **database + searchable store** — sprites keyed by `spec_key`, plus their `render_spec`s / metadata, queryable by kind / palette / creator (§4 store + the Library, §5);
- **art provider** — serves sprites to its clients (§4 serve-path).

**The world and the workbench are its clients, not peers:**
- the **umwelt world / gateway** is a *thin client* — it asks the library for art by `render_spec` (render path → cache-hit-or-fallback) or requests a generation (author path); it never needs to know SDXL / LoRA / CAS exist;
- Lei's **`:5015` workbench** stays his **private tuning client** (free-text, no-auth, single trusted user — distinct from the structured agent-facing path, `ob_2949`); approved results feed the library's store.

This consolidation is why **all art-security lives in one place** — the descriptor charset, B.9 quota, B.6 moderation, and serve-path confinement (§6) are the library server's own gates. The **render-vs-author *policy* (§1) lives inside the library** — the library never generates from a read/serve call, which keeps serving fast despite a co-located GPU generator. (Note: `/scene`'s own hit-or-procedural lookup **code** stays in the world engine, untouched — §1; only the *no-generate-on-read* policy is the library's.) Sections §3 (generation) / §4 (store + serve) / §5 (moderation Library) below detail each internal role.

**Serving to agents (`look_at`) — keep it native-resolution.** When the *provider* role serves a sprite to an **agent** (Lei's `look_at`-returns-visual-art design, distinct from the text-only `glance`), serve the **native sprite** (64×64), not an upscaled display image. An agent's visual-token cost is `⌈w/28⌉ × ⌈h/28⌉` (`ob_2960`), so a 64px sprite is **9 tokens** and a 48px sprite 4 — negligible; even a whole composited place-scene image is a few hundred tokens, never context-dominating. The **human renderer upscales separately** for the browser. Decouple agent-vision-resolution (native, cheap) from human-display-resolution (as large as you like).

---

## 1. The load-bearing decision: render-vs-author split

Part A.4 as written generates **inline on the render path** (cache-miss → `renderer.generate()` → store → return). **M4 overrides this.** Per dv_1159 (from Ariadne's render-vs-author split):

> **Generation happens ONLY on the AUTHOR path. The synchronous `/scene` render path does exact-match-or-procedural-fallback and NEVER invokes the model.**

- **Render path** (`/scene`, every viewer, hot): look up `sprite_ref` by `spec_key`. Hit → serve it. Miss → **procedural fallback** (Part A.3 `ProceduralRenderer`) or static placeholder, logged. Never blocks on a GPU call; **un-DoS-able by construction**.
- **Author path** (an agent authors/edits a `render_spec`; a backfill job; the avatar birth flow A.6): this is where a novel `spec_key` triggers a generation, subject to the B.9 quota + the B.6 moderation gate before the sprite becomes live/servable.

Two payoffs: (a) DoS-safety — the viewer-facing path has no generation surface (the new B.9 quota, §6); (b) latency — a ~5s GPU call never sits in a scene render. This is the single most important reconciliation vs Part A.4; the M4 PR updates A.4 to say so.

**No render-path code change:** the split is enforced entirely by *renderer wiring* — only the new author-path endpoint (§3) is wired to an `ImageModelRenderer`; `/scene`'s existing A.4 lookup-or-procedural logic is untouched. The render path never gaining a generation call is a wiring property, not a code edit to the hot path.

---

## 2. Three population paths

| Path | Trigger | Volume | Moderation |
|---|---|---|---|
| **(a) Per-kind generics** | One default sprite per `class` in the taxonomy (the fallback that beats the procedural sprite) | ~O(taxonomy size), one-time + on taxonomy growth | Bulk-authored by an agent (me/Luria/Sol), same self-verify + 1 peer gate, once per kind |
| **(b) Per-thing backfill** | Lei's ask: specific things get specific sprites ("the telescope wants to look like a telescope") | O(distinct existing specs) | Backfill job authors specs → each generation rides the gate; peer-approve in batches at the Library |
| **(c) Live author-path** | An agent forges/edits a thing with a novel `render_spec` | O(new distinct specs), rate-limited | Inline self-verify + 1 peer before it goes live |

All three are **author-path** generations → all three ride the same B.9 quota + B.6 gate. Per-kind generics (a) are the safety net: a thing with no specific sprite renders its **kind generic**, not a raw procedural blob, and only falls to procedural if even the generic is missing.

---

## 3. Model-service interface (`ImageModelRenderer`)

`ImageModelRenderer` implements the Part A.3 `Renderer` protocol (`generate(prompt, negative, size, seed) -> PNG bytes`) but is invoked **only from the author path** (§1).

- **Generation core (from the PoC, unchanged):** SDXL-base-1.0 + `nerijs/pixel-art-xl` LoRA on the Chameleon 5090; `FINAL_PROMPT` per Part A.2 (`STYLE_PREAMBLE + SAFETY_PREAMBLE + "a " + join(descriptors) + " " + class + ", " + palette_phrase(palette)`); engine-owned `NEGATIVE_PROMPT`; `seed = key_int` (deterministic per `spec_key`).
- **Postprocess (from the PoC):** palette-early compose → `rembg` background strip → quantize to fixed palette → downsample to **`SIZE` = 64px** (Lei 2026-07-23, Q2; visual-token cost is a non-issue at 9 tokens/sprite, `ob_2960`) → trim/pad to footprint → RGBA PNG.
- **Deployment (ANSWERED — Q1, Lei 2026-07-23):** the production author-path runs on the **dedicated art-library server** (§Architecture) — a small **header-required, caller-declared-attribution** private-network service on the 5090, distinct from Lei's no-auth `:5015` workbench (Kepler endorsed this as the recommendation). The header is not authentication. **Private-network-scoped** (B.7), **credential-free to the browser** (B.5). The B.9 **global concurrency cap** (≤K concurrent GPU calls + small FIFO queue) lives at this service so even legitimate concurrent authors can't thundering-herd the single GPU.

---

## 4. Storage + serve

- **Store:** external stable dir `~/.umwelt/sprite-library/` (dv_1151 §3 — CAS-style, content-addressed by `spec_key`). CAS-write's authorization **IS** the author-path confirm-gate (§5); the render path never writes. No bespoke CAS enforcement ahead of the general gate.
- **Serve:** gateway hands out the file for a `sprite_ref`. **Serve-path validation (dv_1159 / dv_1152):** the resolved `asset_path` MUST be confined to the sprite-dir before open/serve — a no-serve-arbitrary-file defense, because the manifest is multi-writer (curation flow). Reject + procedural-fallback + log on an out-of-tree resolution.
- `sprite_ref` shape: v0 `/sprite/<key>`; v1 world CAS `cas:sha256:<key>` served by the existing public `GET /umwelt/cas/<hash>` (Part A.4 note — a deliberate v1 step, needs a guarded CAS write route).

---

## 5. Moderation + Library flow (B.6 — the content gate)

Per Kepler's relay of Lei's 2026-07-23 ruling: **creator self-verify + ONE peer approval, no Lei gate, plus a human-browsable Library** so Lei can browse/comment without blocking.

- A freshly generated sprite enters a **pending** state — NOT yet servable in a shared place.
- **Creator self-verify:** the authoring agent inspects the generated image against its `render_spec` intent (this is also where obviously-wrong generations — face-leak, wrong object — get rejected and regenerated, per the PoC friction ob_2946).
- **One peer approval:** a second agent approves at the **Library shelf** (the natural chokepoint, Part B.6). Only then does the sprite become **live** (servable).
- **Human-browsable Library:** a read surface (Lei browses, comments; non-blocking).
- **Why this is load-bearing (dv_1159):** because M4 exercises the charset+length-cap option B.1 already permits for descriptors (vs its allowlist alternative, `RENDER-PIPELINE-AND-SECURITY.md` B.1) — the weaker of the two — the content-safety guarantee moves here — B.6 gates the **output image** (what viewers actually see), so an inappropriate descriptor cannot ship inappropriate imagery past the peer gate. **Most load-bearing for avatars** (dv_1155 — always human-and-child-viewed in shared places).

---

## 6. Security layer (consolidated from dv_1159)

> **Part-B status (read this before the `B.9` references).** `RENDER-PIPELINE-AND-SECURITY.md` today defines **B.1–B.8** only. **B.6** (moderation) and the serve-path defense build on *existing* criteria; **`B.9` (quota) is a NEW criterion this design introduces** — it does not exist in Part B yet. The M4 build PR adds a `### B.9` section to Part B (after B.8), together with the §1 A.4-override note. Every `B.9` reference in this DoR (here, the Architecture section, and §1–§3, §8) denotes that *new* criterion, not a pre-existing section.

The three M4 gates compose on orthogonal axes — **content (B.6, existing) · rate (B.9, new) · served-file (serve-path)** — plus the descriptor-charset requirements:

1. **Descriptor charset — conservative safe subset**, not merely "printable": exclude control chars, DOM/markup metacharacters, path separators, and the `spec_key`/cache delimiter (or hash the `spec_key` so it stays injective over the free-text space). Concrete set per `dv_1161`: **`^[A-Za-z0-9 -]+$`** — alphanumeric + space + hyphen; **apostrophe deliberately excluded** (quote-char DOM risk; "wizards hat" substitutes for "wizard's hat"). Per-descriptor length cap (≤24) **on top of** Part A.1's 0..6 count cap.
2. **Descriptors ride `esc()` on display** (B.6) — Library browser, blurbs. They are semi-free-text now (charset+cap, not an allowlist), so they can NOT be treated as a pre-vetted closed vocabulary exempt from escaping.
3. **B.9 quota (author path only):** per-agent generation RATE limit + global CONCURRENCY cap (§3) + cache-first (exact `spec_key` served free) + per-agent generation logging. **DoS defense rests on rate+concurrency caps alone** — the finite-space reasoning is void under open descriptors (dv_1159 corrects dv_1154). **Operational rider:** cache hit-rate is **lower** with open descriptors, so tune the rate cap conservatively and do not assume high cache effectiveness.
4. **Serve-path confinement** (§4).
5. **Prompt-composition integrity (Part A.2):** `class`/`palette` stay closed sets (reject-and-fallback on out-of-set); `STYLE_PREAMBLE`/`SAFETY_PREAMBLE`/`NEGATIVE_PROMPT` are engine-owned constants — any change to them is a Part-B security-review change.

---

## 7. Interaction with per-instance customization + avatars

- **Per-instance (Part A.5):** unchanged — compositing-only (color tint + ≤2-char marker), never a re-gen, never new imagery (B.2).
- **Avatars (Part A.6 + dv_1155):** ride the same gates; the **serve-path fix** (dv_1155) is a separate small task — the gateway currently enriches `things` only (`out_avatars` hardcodes `sprite_ref:None`); avatars need `metadata.render_spec` threaded through `/scene` + enriched, mirroring the lane-#278 passthrough. Customization UX (birth 3-variant pick) stays Lei's lane (ob_2910); I design only how a *chosen* render_spec gets served.

---

## 8. Build slicing (for Luria — small merged slices, the winning cadence)

1. **`ImageModelRenderer` + author-path generate endpoint** (the model service §3, behind the full B.9 quota — per-agent rate limit + global concurrency cap + per-agent generation logging, §6.3) — security pass on the endpoint.
2. **Sprite-library store + serve** with serve-path confinement (§4) **+ native-resolution serve to agents** (§Architecture `look_at` note — serve the native 64px sprite to agents; the human renderer upscales separately) — security pass on the serve path.
3. **B.6 pending→peer-approve→live state + Library browse surface** (§5).
4. **Per-kind generics population** (§2a) — a one-time authoring job.
5. **Per-thing backfill job** (§2b) — the telescope and friends.
6. **Descriptor charset validator** (§6.1) at the author/forge boundary.
7. **Avatar serve-path enrichment** (§7, dv_1155) — independent, can land any time.

Each slice: reviewer-fairy → my colleague/security pass → Kepler merge. Slices 1, 2, 6 are the security-surface ones (they get my lane specifically).

---

## 9. Open questions — ALL RESOLVED (2026-07-23)

1. **Production model deployment (§3, §Architecture):** — **ANSWERED (Lei):** a **dedicated, header-required private-network art-library server** on the 5090 (Kepler-endorsed as the recommendation), not a world-enqueued worker; its attribution is caller-declared rather than authenticated, and it is distinct from Lei's `:5015` workbench, which stays his private tuning client.
2. **Sprite `SIZE` (§3):** — **ANSWERED (Lei): 64px.** Cost is a non-issue — an agent's `look_at` on a 64px sprite is 9 visual tokens (`⌈64/28⌉²`, `ob_2960`), negligible; a full scene-image is a few hundred tokens.
3. **Per-kind generic taxonomy scope + priority (§2a):** — **ANSWERED (Kepler), from playtest frequency × visibility:** (1) document family (scroll/tome/book), (2) boards + desks, (3) instruments/tools, (4) containers, (5) misc. The slice-4 populate job walks this order.
4. **Descriptor charset exact set (§6.1):** — **PROPOSED (`dv_1161`): `^[A-Za-z0-9 -]+$` after trim** (exclusions per-char justified against control / DOM-markup / path / spec_key-delimiter risks; per-descriptor ≤24-char cap on top of A.1's 0..6 count cap). Kepler confirms not-over-restrictive at the slice-6 charset-validator PR — review-time, not a Lei gate.

**No remaining Lei gate.** All four questions are resolved (Q1/Q2 by Lei, Q3 by Kepler, Q4 proposed for review-time). Per the umwelt merge-authority rule, M4 is now design-complete on the Lei axis; build on all slices (§8) proceeds under the normal reviewer-fairy → security-lane → Kepler-merge pipeline.
