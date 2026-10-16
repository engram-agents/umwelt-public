"""Umwelt Web Render — `ProceduralRenderer` (M2).

The no-model image pipeline's `Renderer` backend (RENDER-PIPELINE-AND-
SECURITY.md §A.3): given a blueprint's `render_spec` dict + a deterministic
`seed`, produce a valid RGBA PNG sprite. No external input reaches pixels
beyond the controlled `class`/`palette`/`descriptors`/`footprint`/`anchor`
fields — `descriptors` may only influence *numeric* deterministic
perturbation (a size/lightness jitter), never text or arbitrary drawing.
This is the security-relevant boundary from Part B.1, applied to the
procedural path even though B.1 is framed around the (future, M4) model path.

Determinism strategy (a judgment call — flagged in the M2 handoff): every
drawing decision is derived by hashing the RELEVANT slice of `render_spec`
content itself (`class` string for the unknown-class fallback shape,
`palette` string for the unknown-palette fallback color, the joined
`descriptors` string + `seed` for the minor jitter) via `hashlib.sha256` —
never Python's salted `hash()`, which is not stable across processes. The
`seed` parameter is folded into the jitter (not ignored) so that, per
RENDER-PIPELINE-AND-SECURITY.md §A.6's future avatar-variant flow
(`seed=0,1,2` → 3 distinct variants), rendering the SAME `render_spec` with
a DIFFERENT seed yields a visibly different (but still recognizably
same-class) sprite — useful headroom for M3, at no cost to M2's own
determinism requirement (same render_spec + same seed ⇒ byte-identical,
trivially, since the whole pipeline is a pure function of its inputs).
"""

from __future__ import annotations

import hashlib
import io
import math
from typing import Any

from PIL import Image, ImageDraw

_CANVAS_SIZE = 48

# palette -> flat RGB fill color. Concrete choices (flagged in the handoff
# for review — this mapping is a designed, not merely built, decision):
#   warm-wood  -> a mid brown (wood grain, without going near-black)
#   cool-stone -> a blue-gray (masonry/stone)
#   verdant    -> a leafy green
#   ember      -> a warm orange-red (coals/fire)
#   slate      -> a dark neutral gray
_PALETTE_COLORS: dict[str, tuple[int, int, int]] = {
    "warm-wood": (150, 101, 56),
    "cool-stone": (120, 144, 156),
    "verdant": (67, 160, 71),
    "ember": (216, 67, 21),
    "slate": (55, 71, 79),
}

# Unrecognized-`class` fallback shapes (outline-only, to visually read as
# "generic placeholder" rather than one of the named-primitive fills).
_FALLBACK_SHAPES = ("square", "triangle", "diamond", "hexagon")


def _hash_bytes(s: str) -> bytes:
    """Stable (process-independent) hash of a string. Never Python's
    built-in `hash()` — that's salted per-process and would break the
    determinism contract."""
    return hashlib.sha256(s.encode("utf-8")).digest()


def _fallback_color(key: str) -> tuple[int, int, int]:
    """Deterministic RGB for an unrecognized palette value, derived from a
    hash of the palette string itself (never a raw palette-string-derived
    color the browser could be tricked into rendering as text — this is
    numbers only). Channels are clamped to [40, 215] so the fallback is
    never near-black or near-white (stays visually legible against the
    dark UI background)."""
    h = _hash_bytes(f"palette:{key}")
    return (40 + h[0] % 176, 40 + h[1] % 176, 40 + h[2] % 176)


def _resolve_palette(palette: Any) -> tuple[int, int, int]:
    if isinstance(palette, str) and palette in _PALETTE_COLORS:
        return _PALETTE_COLORS[palette]
    return _fallback_color(palette if isinstance(palette, str) else repr(palette))


def _darker(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, int(c * factor)) for c in color)  # type: ignore[return-value]


def _apply_light(color: tuple[int, int, int], delta: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c + delta))) for c in color)  # type: ignore[return-value]


def _variation(seed: int, descriptors: list[Any]) -> tuple[float, float]:
    """A small deterministic (scale, lightness_delta) perturbation from
    `seed` + the joined `descriptors` string. `descriptors` influences ONLY
    these two numeric parameters — never text, never a drawing branch —
    per the security-relevant boundary in the module docstring.

    scale in [0.9, 1.1] (±10% of a shape's key dimensions, applied WITHIN
    the fixed canvas — the canvas itself always stays `_CANVAS_SIZE` square,
    so "valid RGBA PNG at the target size" always holds regardless of
    descriptors); lightness_delta in [-15, 15] applied per RGB channel.
    """
    desc_str = "|".join(str(d) for d in (descriptors or []))
    h = hashlib.sha256(f"{seed}:{desc_str}".encode("utf-8")).digest()
    scale = 0.9 + (h[0] / 255.0) * 0.2
    light_delta = -15.0 + (h[1] / 255.0) * 30.0
    return scale, light_delta


def _regular_polygon(cx: float, cy: float, r: float, n: int,
                      rot_deg: float) -> list[tuple[float, float]]:
    pts = []
    for i in range(n):
        theta = math.radians(rot_deg + 360.0 * i / n)
        pts.append((cx + r * math.cos(theta), cy + r * math.sin(theta)))
    return pts


# --- named shape primitives, one per taxonomy class in the spec ----------

def _draw_chair(draw: ImageDraw.ImageDraw, size: int,
                 color: tuple[int, int, int], scale: float) -> None:
    cx = size / 2
    seat_w = size * 0.55 * scale
    seat_h = size * 0.13
    seat_top = size * 0.52
    seat_left = cx - seat_w / 2
    seat_right = seat_left + seat_w
    draw.rectangle([seat_left, seat_top, seat_right, seat_top + seat_h],
                    fill=color)

    back_w = size * 0.36 * scale
    back_h = size * 0.34
    back_top = size * 0.14
    back_left = cx - back_w / 2
    draw.rectangle([back_left, back_top, back_left + back_w, back_top + back_h],
                    fill=color)

    leg_w = max(1.5, size * 0.045)
    leg_h = size * 0.20
    leg_y0 = seat_top + seat_h
    leg_y1 = leg_y0 + leg_h
    leg_xs = [seat_left + leg_w * 0.5, seat_left + seat_w * 0.30,
              seat_left + seat_w * 0.62, seat_right - leg_w * 1.5]
    for lx in leg_xs:
        draw.rectangle([lx, leg_y0, lx + leg_w, leg_y1], fill=color)


def _draw_well(draw: ImageDraw.ImageDraw, size: int,
                color: tuple[int, int, int], scale: float) -> None:
    cx = cy = size / 2
    outer_r = size * 0.40 * scale
    inner_r = size * 0.20 * scale
    draw.ellipse([cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r],
                  fill=color)
    dark = _darker(color, 0.35)
    draw.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r],
                  fill=dark)


def _draw_book(draw: ImageDraw.ImageDraw, size: int,
                color: tuple[int, int, int], scale: float) -> None:
    w = size * 0.60 * scale
    h = size * 0.44 * scale
    left = size / 2 - w / 2
    top = size / 2 - h / 2
    draw.rectangle([left, top, left + w, top + h], fill=color)
    spine = _darker(color, 0.55)
    spine_w = max(2.0, w * 0.12)
    spine_x = size / 2 - spine_w / 2
    draw.rectangle([spine_x, top, spine_x + spine_w, top + h], fill=spine)


def _draw_plant(draw: ImageDraw.ImageDraw, size: int,
                 color: tuple[int, int, int], scale: float) -> None:
    pot_w = size * 0.36 * scale
    pot_h = size * 0.22
    pot_left = size / 2 - pot_w / 2
    pot_top = size * 0.68
    pot_color = _darker(color, 0.5)
    draw.rectangle([pot_left, pot_top, pot_left + pot_w, pot_top + pot_h],
                    fill=pot_color)
    base_x, base_y = size / 2, pot_top
    leaf_len = size * 0.32 * scale
    width_px = max(1, int(size * 0.03))
    for a in (-60, -30, 0, 30, 60):
        rad = math.radians(a - 90)  # spread fanning mostly upward
        dx = leaf_len * math.cos(rad)
        dy = leaf_len * math.sin(rad)
        draw.line([base_x, base_y, base_x + dx, base_y + dy],
                   fill=color, width=width_px)


def _draw_orb(draw: ImageDraw.ImageDraw, size: int,
               color: tuple[int, int, int], scale: float) -> None:
    r = size * 0.30 * scale
    cx = cy = size / 2
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


def _draw_door(draw: ImageDraw.ImageDraw, size: int,
                color: tuple[int, int, int], scale: float) -> None:
    w = size * 0.40 * scale
    h = size * 0.72
    left = size / 2 - w / 2
    top = size * 0.14
    draw.rectangle([left, top, left + w, top + h], fill=color)
    knob_r = max(1.5, size * 0.035)
    knob_x = left + w * 0.78
    knob_y = top + h * 0.52
    dark = _darker(color, 0.3)
    draw.ellipse([knob_x - knob_r, knob_y - knob_r,
                  knob_x + knob_r, knob_y + knob_r], fill=dark)


def _draw_sign(draw: ImageDraw.ImageDraw, size: int,
                color: tuple[int, int, int], scale: float) -> None:
    board_w = size * 0.5 * scale
    board_h = size * 0.26
    board_left = size / 2 - board_w / 2
    board_top = size * 0.18
    draw.rectangle([board_left, board_top,
                     board_left + board_w, board_top + board_h], fill=color)
    post_w = max(2.0, size * 0.06)
    post_left = size / 2 - post_w / 2
    post_top = board_top + board_h
    post_bottom = size * 0.86
    post_color = _darker(color, 0.5)
    draw.rectangle([post_left, post_top, post_left + post_w, post_bottom],
                    fill=post_color)


def _draw_telescope(draw: ImageDraw.ImageDraw, size: int,
                     color: tuple[int, int, int], scale: float) -> None:
    length = size * 0.62 * scale
    width = size * 0.14
    cx, cy = size * 0.42, size * 0.40
    angle = math.radians(-40)
    dx_l, dy_l = math.cos(angle) * length / 2, math.sin(angle) * length / 2
    dx_w, dy_w = (math.cos(angle + math.pi / 2) * width / 2,
                  math.sin(angle + math.pi / 2) * width / 2)
    pts = [
        (cx - dx_l - dx_w, cy - dy_l - dy_w),
        (cx + dx_l - dx_w, cy + dy_l - dy_w),
        (cx + dx_l + dx_w, cy + dy_l + dy_w),
        (cx - dx_l + dx_w, cy - dy_l + dy_w),
    ]
    draw.polygon(pts, fill=color)

    apex_x, apex_y = size * 0.5, size * 0.82
    leg_len = size * 0.18
    width_px = max(1, int(size * 0.03))
    for a_off in (-30, 0, 30):
        rad = math.radians(90 + a_off)
        lx = apex_x + leg_len * math.cos(rad)
        ly = apex_y + leg_len * math.sin(rad)
        draw.line([apex_x, apex_y, lx, ly], fill=color, width=width_px)


_SHAPE_DRAWERS = {
    "chair": _draw_chair,
    "well": _draw_well,
    "book": _draw_book,
    "plant": _draw_plant,
    "orb": _draw_orb,
    "door": _draw_door,
    "sign": _draw_sign,
    "telescope": _draw_telescope,
}


def _draw_fallback(draw: ImageDraw.ImageDraw, size: int, cls: str,
                    color: tuple[int, int, int], scale: float) -> None:
    """Deterministic geometric fallback for any `class` not in the named
    taxonomy — derived from a hash of the `class` string itself (NOT the
    seed), so the same unrecognized class always renders as the same shape
    regardless of which blueprint/descriptors reference it, while two
    different unrecognized classes (e.g. "lamp" vs "vase") reliably land on
    different shapes/sizes/rotations."""
    h = _hash_bytes(f"class:{cls}")
    shape = _FALLBACK_SHAPES[h[0] % len(_FALLBACK_SHAPES)]
    frac = 0.30 + (h[1] / 255.0) * 0.25
    r = size * frac * scale
    cx = size / 2 + (h[2] / 255.0 - 0.5) * size * 0.15
    cy = size / 2 + (h[3] / 255.0 - 0.5) * size * 0.15
    rot = (h[4] / 255.0) * 360.0
    width_px = max(2, int(size * 0.06))

    if shape == "square":
        pts = _regular_polygon(cx, cy, r, 4, rot + 45)
    elif shape == "triangle":
        pts = _regular_polygon(cx, cy, r, 3, rot)
    elif shape == "diamond":
        pts = _regular_polygon(cx, cy, r, 4, rot)
    else:  # hexagon
        pts = _regular_polygon(cx, cy, r, 6, rot)
    draw.polygon(pts, outline=color, width=width_px)


def generate(render_spec: dict, seed: int, size: int = _CANVAS_SIZE) -> bytes:
    """`ProceduralRenderer.generate` — the `Renderer` backend for M2.

    Deterministic given (`render_spec`, `seed`): same input always produces
    byte-identical PNG output. Always a valid RGBA PNG at `size`x`size`.
    Inherently safe — draws only from `class`/`palette`/`descriptors`
    (descriptors influence only a numeric jitter, see `_variation`);
    `footprint`/`anchor` are accepted on the input dict (per the interface
    contract) but not consumed by this v0 shape logic — they're grid-
    presentation concerns owned by the gateway, not the sprite pixels.
    """
    cls = render_spec.get("class")
    palette = render_spec.get("palette")
    descriptors = render_spec.get("descriptors")
    if not isinstance(descriptors, list):
        descriptors = []

    base_color = _resolve_palette(palette)
    scale, light_delta = _variation(seed, descriptors)
    color = _apply_light(base_color, light_delta)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    drawer = _SHAPE_DRAWERS.get(cls) if isinstance(cls, str) else None
    if drawer is not None:
        drawer(draw, size, color, scale)
    else:
        _draw_fallback(draw, size, cls if isinstance(cls, str) else repr(cls),
                        color, scale)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
