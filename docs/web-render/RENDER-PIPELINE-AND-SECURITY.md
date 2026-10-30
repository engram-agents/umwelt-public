# Render Pipeline Contract + Security Review Criteria

**Companion to DESIGN.md.** Owner: Borges. Purpose: pin the *stable interfaces* of the
image pipeline (so M2 has a clear target) and the *security criteria* I review every PR
against. Deliberately does NOT specify procedural-renderer internals — those are Luria's
build choice, informed by M0/M1 practice (don't over-design ahead of the run).

---

## Part A — Render pipeline interface contract (stable; targets M2)

### A.1 `render_spec` (agent-authored, lives on the blueprint's `metadata.render_spec`)

Free-form JSON on the blueprint thing's metadata (no engine change — metadata is
agent-writable at create). Schema:

```jsonc
{
  "class":      "chair|book|well|telescope|plant|sign|door|orb|... ",  // required, from a controlled taxonomy
  "descriptors":["tall","brass","on a tripod"],                        // 0..6 controlled adjectives
  "palette":    "warm-wood|cool-stone|verdant|ember|slate|...",        // required, from a fixed palette set
  "footprint":  [w, h],                                                 // cells; default [1,1]; max [3,3]
  "anchor":     "floor|wall|ceiling"                                    // default "floor"
}
```

**Hard rule (security + style): there is no free-text `prompt` field.** The gateway
composes the model prompt from these *structured* fields only. Unknown keys are dropped;
`class`/`palette` outside their allowed sets are rejected (thing renders with the fallback
sprite, and the rejection is logged). This is the single most important contract in the
project — it is simultaneously the style-stability mechanism and the child-safety gate.

### A.2 Prompt composition (engine-owned, agents never see/set)

```
FINAL_PROMPT = STYLE_PREAMBLE + SAFETY_PREAMBLE
             + "a " + join(descriptors) + " " + class + ", " + palette_phrase(palette)
NEGATIVE_PROMPT = STYLE_NEGATIVE (engine-owned)
```

- `STYLE_PREAMBLE` locks the art bible: e.g. *"48×48 pixel art sprite, single object,
  top-down 3/4 view, flat cel shading, thick dark outline, transparent background, limited
  palette, no text"*.
- `SAFETY_PREAMBLE` + `STYLE_NEGATIVE` enforce child-safe, on-style output (no gore, no
  text, no photorealism, no faces on objects, etc.).
- These two constants are the review surface — any change to them is a security-relevant
  change (Part B).

### A.3 `Renderer` interface (backend-agnostic — the swap seam)

```python
class Renderer(Protocol):
    def generate(self, prompt: str, negative: str, size: int, seed: int) -> bytes:  # PNG
        ...
```

- **`ProceduralRenderer` (v0):** ignores the model; deterministically composes a sprite
  from `render_spec` (shape primitive per `class` + `palette` colors). Requirements (not
  internals): deterministic given (`render_spec`, `seed`); output is a valid RGBA PNG at
  `size`; inherently safe (no external input reaches pixels beyond the controlled fields);
  visually distinguishable per `class`. *How* it draws is Luria's call.
- **`ImageModelRenderer` (M4):** posts `prompt/negative/size/seed` to the **local SD on the
  soma 5090** and returns PNG bytes. Only this class knows the model exists.

### A.4 Pipeline (identical across backends)

```
key = sha256(canonical_json(render_spec))
if sprite_store.has(key): return sprite_ref(key)
raw   = renderer.generate(compose(render_spec), NEG, SIZE, seed=key_int)
sprite= postprocess(raw)      # quantize→fixed palette · downsample→SIZE · trim/pad→footprint · RGBA PNG
sprite_store.put(key, sprite)
return sprite_ref(key)        # v0: "/sprite/<key>"  ·  v1: "cas:sha256:<key>"
```

- **One render per unique `render_spec`**, keyed by its content hash → automatic dedup +
  reuse across every instance of the blueprint. Cost = O(distinct blueprint specs), not
  O(things).
- **Sprite store v0** = gateway-local dir `sprites/<key>.png`, served `GET /sprite/<key>`.
  **v1** = world CAS (`store()` in-process → `cas:sha256:<hash>`, served by the existing
  public `GET /umwelt/cas/<hash>`). Note: CAS has **no HTTP write route** today, so the
  v1 promotion requires either running the gateway in-process with the world or adding a
  guarded CAS write route — a deliberate v1 decision, not v0.

### A.5 Per-instance customization (compositing, never a re-gen)

At scene-render time the frontend composites over the shared base sprite:
- **`color`** — a tint/recolor (hue shift or palette-swap) from a bounded set.
- **`marker`** — an optional ≤2-char owner abbrev, drawn in a fixed corner.

That is the *entire* per-instance surface. An instance can recolor + initial the class
sprite; it cannot introduce new imagery. (This bound is a security property — Part B.2.)

### A.6 Avatar variant flow (M3)

Birth: `avatar_render_spec` from description + preset fields (sex/age) → generate **3
variants** (`seed = 0,1,2`) → user picks → gateway stores the chosen sprite keyed by agent.
Re-render on description edit. v0 stores avatar sprite gateway-side (no engine change);
v1 promotes to `avatar.metadata.sprite_ref` (needs the `customize_avatar` allowlist widened
by one key).

---

## Part B — Security review criteria (I apply this to every PR)

The threat model: a shared, private-network, **human-and-child-viewed** surface, driven by
agent-authored content, that (at M4) invokes a generative image model.

### B.1 No agent free-text reaches the image model. *(the #1 gate)*
- `render_spec` exposes only structured, allow-listed fields (A.1). Verify the compositor
  cannot be made to concatenate any agent string into `FINAL_PROMPT` beyond the controlled
  vocabulary. Reject-and-fallback on out-of-set `class`/`palette`; drop unknown keys.
- `descriptors` are matched against an allowlist (or a conservative charset + length cap) —
  they are the one semi-free field and the likeliest injection vector.

### B.2 Per-instance customization stays bounded to color + marker.
- No PR may widen per-instance customization to arbitrary imagery, arbitrary text, or a
  second render. Marker ≤2 chars, fixed position, escaped. Color from a bounded set.

### B.3 Identity / action authorization inherits the world, never broadens it.
- Gateway acts as the viewer's *chosen* world identity; **spectator (read-only) default**;
  no "act as anyone" control. Verify the gateway cannot issue a world action under an
  identity the viewer did not select. It must not weaken the world's trust-scoped-v0 model,
  only pass through it.

### B.4 The `use()` sandbox is never bypassed or re-implemented.
- All execution routes through the world's existing `/umwelt/use` (sandboxed). The render
  layer must contain **zero** code that runs agent-authored code itself. Any PR that shells
  out, evals, or spawns from thing content = block.

### B.5 Secrets stay server-side; the browser is untrusted.
- Any model/API credential lives only in the gateway env, never shipped to the browser,
  never in a `/scene` response. Sprite bytes are public-by-hash (fine); creds are not.

### B.6 Output handling is safe by construction.
- All world text rendered in the DOM is escaped (the existing webview's `esc()` discipline).
- (M4) model output passes a content-moderation check before it becomes a live sprite;
  **blueprint sprites get a human-review gate** before going live in a shared place — the
  Library shelf is the natural chokepoint.

### B.7 New network surface stays inside the declared private-network boundary.
- The gateway may be reachable from the home LAN and the deliberately enrolled tailnet.
  Before remote browser use, traffic must have end-to-end HTTPS (#583 is still open);
  network admission never substitutes for application identity or authorization.

### B.8 Fallback is safe and loud.
- Model down / spec rejected / postprocess fails ⇒ the **procedural** (or a static
  placeholder) sprite, and the failure is logged — never a broken render, never an
  unmoderated raw model output leaking through.

---

*Living doc — B-criteria get sharper as M0/M1 practice surfaces real edges. If the
substrate fights any interface above, that's a design signal: tell me and we revise (the
loop, not the ladder).*
