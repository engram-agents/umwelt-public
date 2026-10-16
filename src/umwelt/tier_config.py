"""Tier-config binding (umwelt#464 slice 2, the `generate` capability-channel
op's enablement gate).

Today an engine process's tier is expressed only through its `--db` argv,
and webrender's own `--sprite-dir` argv is a wholly separate, home-scoped-
by-default value -- nothing anywhere enforces the two actually describe the
same tier. A caller could point one engine's `--db` at tier-1 while its
sprite library on disk is really tier-2's, and nothing would notice.

This module makes a small JSON file (`{"tier": ..., "db": ..., "sprite_dir":
...}`) the SINGLE source of truth binding a deployed engine process to a
specific sprite library. `load_tier_config` reads and validates that file;
`verify_db_binding` checks it against the engine's own `--db` argv;
`verify_or_init_sprite_dir_marker` checks it against a `.tier` marker file
stamped inside the sprite_dir itself (catching the case `verify_db_binding`
alone cannot: a correctly-configured db binding whose sprite_dir was
previously initialized under a DIFFERENT tier's config).

Server.py's boot sequence is the only caller (umwelt#464 slice 2) -- see its
`--tier-config` CLI flag. A missing/invalid config, or a verification
failure, disables the generation surface only; the rest of the engine boots
normally (enhancement-never-dependency posture, per Kepler's ruling).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TierConfig:
    tier: str
    db: str
    sprite_dir: str


class TierMismatch(Exception):
    """Raised by `verify_db_binding`/`verify_or_init_sprite_dir_marker` when
    the tier-config's own claims don't line up with the engine's actual
    `--db` argv or the sprite_dir's own `.tier` marker. Never a silent
    no-op -- the caller (server.py's boot sequence) catches this and
    disables the generation surface only, never the rest of the engine."""


_REQUIRED_KEYS = ("tier", "db", "sprite_dir")


def load_tier_config(path: str) -> TierConfig:
    """Reads + validates the tier-binding JSON file at `path`.

    Raises `ValueError` (with a message naming the specific problem) on:
    file not found, malformed JSON, a non-object top-level value, or a
    missing/wrong-typed `tier`/`db`/`sprite_dir` key -- all three are
    required and must be strings.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"tier-config file not found: {path!r}")

    try:
        with p.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"tier-config file {path!r} is not valid JSON: {e}") from e

    if not isinstance(raw, dict):
        raise ValueError(
            f"tier-config file {path!r} must contain a JSON object, got "
            f"{type(raw).__name__}"
        )

    missing = [k for k in _REQUIRED_KEYS if k not in raw]
    if missing:
        raise ValueError(
            f"tier-config file {path!r} missing required key(s): {sorted(missing)!r}"
        )

    for key in _REQUIRED_KEYS:
        if not isinstance(raw[key], str):
            raise ValueError(
                f"tier-config file {path!r} key {key!r} must be a string, "
                f"got {type(raw[key]).__name__}"
            )

    return TierConfig(tier=raw["tier"], db=raw["db"], sprite_dir=raw["sprite_dir"])


def verify_db_binding(tier_config: TierConfig, db_path: str) -> None:
    """No-op (returns `None`) iff `tier_config.db` and `db_path` resolve to
    the same absolute path. Otherwise raises `TierMismatch` naming both
    resolved paths and `tier_config.tier`."""
    configured = Path(tier_config.db).resolve()
    actual = Path(db_path).resolve()
    if configured != actual:
        raise TierMismatch(
            f"tier {tier_config.tier!r} tier-config db {str(configured)!r} "
            f"does not match engine --db {str(actual)!r}"
        )


def verify_or_init_sprite_dir_marker(sprite_dir: str | Path, tier: str) -> None:
    """Checks/writes the sprite_dir's own `.tier` marker file (plain text,
    just the tier string). Distinguishes a directory THIS PROCESS made from
    one it merely FOUND — auto-stamping a found, already-populated directory
    would certify a possible original operator mistake (tier-config pointed
    at the wrong pre-existing sprite library) rather than catch one, so only
    an empty-or-nonexistent directory is ever silently stamped.

    - `sprite_dir` doesn't exist yet -> create it (`parents=True`), stamp
      the marker, proceed.
    - `sprite_dir` exists and already has a `.tier` marker -> compare
      (stripped) content to `tier`. No-op on match. Raises `TierMismatch`
      naming the sprite_dir, the marker's actual content, and the expected
      tier on mismatch — the case `verify_db_binding` alone cannot catch: a
      correct db binding whose sprite_dir was previously bound to a
      DIFFERENT tier.
    - `sprite_dir` exists, has NO marker, and is EMPTY (no entries at all)
      -> treated the same as nonexistent ("this process made the directory,
      just hadn't written to it yet"): stamp the marker, proceed.
    - `sprite_dir` exists, has NO marker, and is NON-EMPTY -> a pre-existing
      populated directory found here, not made by this process. Raises
      `TierMismatch` naming the path and requiring an explicit one-time
      adopt step: after confirming the directory genuinely belongs to
      `tier`, an operator writes `<sprite_dir>/.tier` themselves (no new
      CLI flag needed — the human act of writing that file IS the adopt
      step; the next boot then hits the marker-present/match branch above
      and proceeds normally). Never auto-stamped.
    """
    sprite_dir = Path(sprite_dir)
    marker_path = sprite_dir / ".tier"

    if not sprite_dir.exists():
        sprite_dir.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(tier, encoding="utf-8")
        return

    if marker_path.exists():
        actual = marker_path.read_text(encoding="utf-8").strip()
        if actual != tier:
            raise TierMismatch(
                f"sprite_dir {str(sprite_dir)!r} marker tier {actual!r} does "
                f"not match expected tier {tier!r}"
            )
        return

    if not any(sprite_dir.iterdir()):
        marker_path.write_text(tier, encoding="utf-8")
        return

    raise TierMismatch(
        f"sprite_dir {str(sprite_dir)!r} is a pre-existing, non-empty "
        f"directory with no .tier marker -- refusing to auto-stamp it as "
        f"tier {tier!r} (that would certify a possible original mistake, "
        f"not verify one). One-time adopt step required: after confirming "
        f"this directory genuinely belongs to tier {tier!r}, write "
        f"{str(marker_path)!r} yourself containing exactly {tier!r}, then "
        f"restart."
    )
