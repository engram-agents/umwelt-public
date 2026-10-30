"""Umwelt Web Render — prompt composition (RENDER-PIPELINE-AND-SECURITY.md
§A.2; docs/web-render/ART-GENERATION-PIPELINE-M4.md §3/§6), for
`ImageModelRenderer` (M4 art-gen slice 1).

This module implements ONLY prompt assembly — it never calls a model. That
split matters for review: `compose_prompt` is a pure function of a
`render_spec` dict, so it's fully reviewable and testable without any GPU,
network, or model dependency.

Security posture (§B.1, "the #1 gate" — no agent free-text reaches the image
model): every token that lands in `FINAL_PROMPT` traces to one of three
closed, validated sources — the `class` taxonomy, the `palette` set, or the
`descriptors` list, itself charset+length capped (see `_validate_descriptor`
below). `class`/`palette` outside their allow-lists, or a `descriptors` entry
failing the charset/length check, means `compose_prompt` returns `None` —
the caller's job is only to compose; REJECTING and falling back to the
procedural sprite on `None` is the caller's (gateway/service's)
responsibility, same reject-and-fallback pattern `gateway.py`'s
`_apply_render_spec` already uses for a malformed `render_spec`.

**Provenance:** this is a fresh authoring against current `main`,
not a rebase of `origin/webrender/m4-prompt-composition` (Luria's original,
commit `d8a819e`, which predates `manifest.py`/`library.py` and is stale
against them). Kepler's 2026-07-24 ruling (forum #277): port the *taxonomy*
as the reference spec (the closed `class`/`palette` sets, the token-budget-
aware ordering that commit `d8a819e` itself fixed — palette+class immediately
after "a", descriptors trailing last, since CLIP silently truncates at 77
tokens and the optional tail should absorb any truncation, never the
required subject), not the old branch's code verbatim.

**One deliberate deviation from the old branch's `_DESCRIPTOR_RE`/cap:** the
M4 design-of-record (`docs/web-render/ART-GENERATION-PIPELINE-M4.md` §6.1,
Kepler-endorsed 2026-07-23) supersedes the old branch's
lowercase-only `^[a-z0-9 '-]+$` / 20-char cap with `^[A-Za-z0-9 -]+$` (mixed
case, apostrophe dropped as a DOM-quote-injection risk) / 24-char cap. This
module implements the DoR's newer charset, not the old branch's — the DoR
is the more current, explicitly-endorsed source where the two disagree. The
DoR also names this validator as its own build slice (§8 slice 6, "at the
author/forge boundary") — the check here is defense-in-depth at the
composition boundary, not a replacement for that forge-time validator.

`SPRITE_SIZE = 64` per the DoR §9 Q2 (Lei, 2026-07-23) — independent of the
descriptive "48x48" wording baked into `STYLE_PREAMBLE` below, which is
prompt text (a hint to the model), not the actual output resolution control.
Left unchanged from the taxonomy source rather than silently "fixed" to
match; a real re-tune of that wording is a separate, deliberate call.
"""

from __future__ import annotations

import re

# --- controlled vocabularies (§A.1) -----------------------------------

# Mirrored render.py's _SHAPE_DRAWERS keys exactly as of this module's
# original authoring -- the taxonomy the procedural renderer drew at the
# time. Kept as a SEPARATE constant (not imported from render.py) on
# purpose: this is the model-path's OWN allow-list, reviewed under §B.1's
# stricter bar (an unrecognized class here is a hard reject, not render.py's
# graceful geometric-fallback path). The two lists are EXPECTED TO DIVERGE
# as model-only classes are added here (umwelt#339's `figure`/`die` are the
# first case) -- render.py's `_SHAPE_DRAWERS.get(cls)` already degrades a
# class it doesn't recognize to a generic labeled-shape fallback (`None` ->
# `_draw_fallback`, never a crash), so a class existing here without a
# matching procedural drawer is a safe, expected state, not a gap to close.
CLASS_TAXONOMY = frozenset({
    "chair", "well", "book", "plant", "orb", "door", "sign", "telescope",
    "figure", "die",
})

# umwelt#339, Borges's ruling (2026-07-24, forum #277 reply #4983): named
# taxonomy additions (a1), not an open-charset `class` field (a2) -- keeping
# `class` a hard enum preserves the enumerable-vocabulary property
# closed-membership gives (every class that can ever reach the prompt is
# auditable at any moment), which charset-capping alone doesn't replace.
# `figure` (person/NPC-shaped, e.g. a village greeter) and `die` (a
# polyhedral/six-sided die) are the two members added for named,
# recurrence-justified landmarks (umwelt#336's mismatched-art worklist) --
# revisit the one-validation-regime (a2) alternative only if class
# additions become frequent (rule-of-three); at 2, they're not.

# Mirrors render.py's _PALETTE_COLORS keys exactly, for the same
# don't-silently-couple reason as CLASS_TAXONOMY above.
#
# Short ADJECTIVAL form (not a trailing clause) -- see the token-budget
# note on compose_prompt's assembly order below for why this shape matters,
# not just its wording.
PALETTE_ADJECTIVES: dict[str, str] = {
    "warm-wood": "warm wood-toned",
    "cool-stone": "cool stone-toned",
    "verdant": "verdant",
    "ember": "ember-toned",
    "slate": "slate-toned",
}

SPRITE_SIZE = 64  # DoR §9 Q2 (Lei, 2026-07-23) -- see module docstring.

MAX_DESCRIPTORS = 6
MAX_DESCRIPTOR_LEN = 24  # DoR (was 20 in the old branch)
# Mixed-case letters, digits, spaces, hyphens. Apostrophe deliberately
# excluded (DoR -- DOM/quote-injection risk in the Library browser's
# display, per ART-GENERATION-PIPELINE-M4.md §6.2). No other punctuation --
# nothing that could plausibly be prompt-injection or markup syntax.
_DESCRIPTOR_RE = re.compile(r"^[A-Za-z0-9 -]+$")

# --- engine-owned constants (§A.2 -- any change here is security-relevant,
# review under Part B, same as the source module's docstring states) -------

STYLE_PREAMBLE = (
    "48x48 pixel art sprite, single object, top-down 3/4 view, "
    "flat cel shading, thick dark outline, transparent background, "
    "limited palette, no text"
)

SAFETY_PREAMBLE = (
    "child-safe, wholesome, simple, family-friendly"
)

STYLE_NEGATIVE = (
    "text, watermark, signature, photorealistic, photograph, realistic, "
    "3d render, gore, blood, violence, weapon, nsfw, nudity, face, "
    "person, human figure, scary, disturbing, low quality, blurry, "
    "noisy, jpeg artifacts, multiple objects, cropped, out of frame"
)

# umwelt#339 (Borges's implementation note, forum #277 reply #4983):
# `figure` is the one taxonomy class that SHOULD render person-shaped, so
# the anti-humanoid negatives above (which protect every OTHER class from
# accidentally drifting into a person -- a star, a pedestal) would fight
# the model for this one class specifically if left in. Content-safety for
# `figure` (clothed, non-explicit) stays on SAFETY_PREAMBLE + the human
# peer-review gate -- the right layer for it, per Borges's ruling -- not on
# a blanket anti-person negative. `nudity`/`nsfw` stay excluded either way.
FIGURE_STYLE_NEGATIVE = (
    "text, watermark, signature, photorealistic, photograph, realistic, "
    "3d render, gore, blood, violence, weapon, nsfw, nudity, "
    "scary, disturbing, low quality, blurry, "
    "noisy, jpeg artifacts, multiple objects, cropped, out of frame"
)


def _validate_descriptor(d: object) -> bool:
    """A single descriptor is valid iff it's a str, non-empty, at or under
    the length cap, and matches the closed charset -- see this module's
    docstring for why this (not a strict allow-list) is the interim
    choice."""
    return (isinstance(d, str) and 0 < len(d) <= MAX_DESCRIPTOR_LEN
            and _DESCRIPTOR_RE.fullmatch(d) is not None)


def compose_prompt(render_spec: dict) -> tuple[str, str] | None:
    """Compose `(FINAL_PROMPT, NEGATIVE_PROMPT)` from a `render_spec` dict,
    per §A.2's formula. Returns `None` if `class`/`palette` is outside its
    allow-list, or any `descriptors` entry fails validation -- the caller
    is responsible for falling back to the procedural sprite and logging
    the rejection on `None`, same posture `gateway.py`'s
    `_apply_render_spec` already has for a malformed `render_spec`.

    `footprint`/`anchor` (also part of the `render_spec` schema) are
    grid-presentation concerns, not prompt content -- read `render.py`'s own
    `generate()` docstring for the identical reasoning; they're accepted on
    the input dict but never reach the prompt.
    """
    cls = render_spec.get("class")
    if not isinstance(cls, str) or cls not in CLASS_TAXONOMY:
        return None

    palette = render_spec.get("palette")
    if not isinstance(palette, str) or palette not in PALETTE_ADJECTIVES:
        return None

    descriptors = render_spec.get("descriptors", [])
    if not isinstance(descriptors, list) or len(descriptors) > MAX_DESCRIPTORS:
        return None
    for d in descriptors:
        if not _validate_descriptor(d):
            return None

    # Token-budget-aware ordering (found live 2026-07-20 on the PoC, fixed
    # in commit d8a819e -- not a style choice): CLIP's text encoder silently
    # TRUNCATES at 77 tokens -- a prompt that overflows doesn't error, it
    # just drops everything past the limit. STYLE_PREAMBLE + SAFETY_PREAMBLE
    # + the LoRA trigger phrase were already found to consume most of that
    # budget on the actual model, so `palette` and `class` -- the two
    # REQUIRED, closed-vocabulary fields -- go immediately after "a",
    # before `descriptors`. `descriptors` (optional, agent-supplied
    # richness) trails last: if anything gets silently cut by the 77-token
    # limit, it should be the optional tail, never the required subject.
    subject = f"a {PALETTE_ADJECTIVES[palette]} {cls}"
    if descriptors:
        subject += f", {' '.join(descriptors)}"

    final_prompt = f"{STYLE_PREAMBLE}, {SAFETY_PREAMBLE}, {subject}"
    negative_prompt = FIGURE_STYLE_NEGATIVE if cls == "figure" else STYLE_NEGATIVE
    return final_prompt, negative_prompt
