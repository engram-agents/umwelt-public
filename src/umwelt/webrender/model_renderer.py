"""Umwelt Web Render — `ImageModelRenderer` (M4 art-gen slice 1).

A THIN HTTP CLIENT for the standalone SDXL/LoRA generation service
(internal tooling, not in this release) — this module has no torch/
diffusers/GPU dependency itself and never touches a model directly. Talks
stdlib `urllib` over HTTP intended for a private-network deployment, with a
caller-declared attribution header. This client does not enforce network
reachability; host firewall/routing policy owns that boundary. Same convention `gateway.py`'s own
`_world_request` already uses for the world server (no `requests` dependency
— not in `requirements.txt`, and urllib is this codebase's established
pattern for exactly this kind of call).

**Renderer-protocol shape, author-path ONLY.** `generate(render_spec, seed)
-> bytes` matches `render.py`'s `generate()` signature exactly, so it is
swap-compatible wherever a `Renderer` callable is expected — BUT per the M4
design-of-record §1 (the render-vs-author split, `dv_1159`), this class must
NEVER be wired as `get_or_render_sprite`'s `renderer` parameter on the hot
`/scene` render path. That path is exact-match-or-procedural-fallback ONLY
and never invokes a model — un-DoS-able by construction. `ImageModelRenderer`
is invoked ONLY from the author path (a novel `render_spec` entering the
world), where a ~5-6s generation is expected and the result is gated behind
moderation (`manifest.add_pending` + peer `approve`) before it's servable —
see the author-path wiring in `gateway.py`, not this module.

Client-side `prompt.compose_prompt` pre-validation here is a fast-fail
OPTIMIZATION only (skip a header-attributed network round-trip for an obviously
invalid spec) — it is NOT the security boundary. The service validates
independently and is the only party that can be trusted to enforce §B.1;
never assume a caller of this class did the client-side check.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from . import prompt as _prompt


class GenerationUnavailable(Exception):
    """The art-gen service could not produce a sprite for this render_spec
    right now — connection failure, timeout, non-2xx response, or a
    service-side rejection (invalid spec, VRAM budget exhausted, quota
    exceeded). Callers MUST catch this and fall back (procedural/cached) —
    per Kepler's enhancement-never-dependency rule, a dead or overloaded
    5090 must cost the world beauty, never function. Never let this
    exception reach an end user as a 500."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class ImageModelRenderer:
    """Author-path client for one art-gen service instance.

    `agent_id` is sent as the caller-declared private-network-trust v0 identity header
    (`X-Umwelt-Agent`) on every request — the service's header-required front door
    uses it for per-agent B.9 quota + attribution + generation logging. This
    class does not itself enforce anything about `agent_id`'s truthfulness;
    that is exactly the caller-declared-private-network-trust posture the M4
    design-of-record states explicitly (§6.3) — art-gen tolerates it
    (worst case: mis-attributed art, moderated + recoverable), unlike
    voice's stricter crossing-bound/self-bound hold.
    """

    def __init__(self, service_url: str, agent_id: str, timeout: float = 60.0) -> None:
        self.service_url = service_url.rstrip("/")
        self.agent_id = agent_id
        self.timeout = timeout

    def generate(self, render_spec: dict[str, Any], seed: int) -> bytes:
        """Renderer-protocol shape: `(render_spec, seed) -> PNG bytes`.

        Raises `GenerationUnavailable` on ANY failure — never returns
        partial/invalid bytes, never raises a bare urllib/network exception
        (callers should only ever need to catch this one type)."""
        composed = _prompt.compose_prompt(render_spec)
        if composed is None:
            # Fast-fail: skip the round-trip entirely for a spec the
            # service would reject anyway under the identical closed
            # taxonomy/charset rules. Not a security check (see module
            # docstring) — purely saves a network hop.
            raise GenerationUnavailable(
                f"render_spec rejected by compose_prompt (class/palette/"
                f"descriptors outside allow-list): {render_spec!r}")

        url = f"{self.service_url}/generate"
        payload = json.dumps({"render_spec": render_spec, "seed": seed}).encode()
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Umwelt-Agent": self.agent_id,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                content_type = r.headers.get("Content-Type", "")
                data = r.read()
        except urllib.error.HTTPError as e:
            body_raw = e.read()
            message = body_raw.decode(errors="replace")
            try:
                parsed = json.loads(message)
                if isinstance(parsed, dict) and "error" in parsed:
                    message = parsed["error"]
            except (json.JSONDecodeError, ValueError):
                pass
            raise GenerationUnavailable(
                f"art-gen service returned {e.code}: {message}",
                status=e.code) from e
        except urllib.error.URLError as e:
            raise GenerationUnavailable(
                f"art-gen service unreachable: {e}") from e
        except (OSError, ValueError) as e:
            raise GenerationUnavailable(
                f"art-gen service request failed: {e}") from e

        if not content_type.startswith("image/png"):
            raise GenerationUnavailable(
                f"art-gen service returned unexpected content-type "
                f"{content_type!r} instead of image/png")
        if not data:
            raise GenerationUnavailable(
                "art-gen service returned an empty response body")
        return data
