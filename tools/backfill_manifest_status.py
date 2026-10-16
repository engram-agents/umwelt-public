#!/usr/bin/env python3
"""ONE-SHOT ops command: run `manifest.backfill_grandfathered_status`
against a real sprite_dir. Exists so the umwelt#289/Ariadne migration is a
step an operator RUNS, not one they have to remember (Borges's PR #416
review) -- `backfill_grandfathered_status` itself has no CLI entry point
and is never called from any read path (by design: a read path shouldn't
write, and auto-backfilling would silently re-grandfather rows a future
bypass introduces).

What it does: stamps `status: "grandfathered"` onto every entry in
`<sprite_dir>/master_manifest.json` that currently has no `status` field
at all. Lock-safe, atomic, idempotent -- safe to re-run; a second run
reports 0 migrated.

Deploy ordering (Borges's ruling, umwelt#289): this MUST run against the
real sprite_dir BEFORE (or atomically with) deploying code built on
manifest.is_live's allow-list fix -- after that fix deploys but before
this runs, every status-less entry stops serving (fails closed) until
migrated. `sprites.py`'s `ManifestStatusIndex` prints a one-shot stderr
warning naming this exact command if it detects unmigrated entries on the
live serve path, so a wrong deploy order announces itself.

Run:
    PYTHONPATH=src python3 tools/backfill_manifest_status.py \\
        --sprite-dir ~/.umwelt/sprite-library
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from umwelt.webrender import manifest as _manifest  # noqa: E402

DEFAULT_SPRITE_DIR = Path.home() / ".umwelt" / "sprite-library"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python tools/backfill_manifest_status.py",
        description="One-shot migration: stamp status:\"grandfathered\" "
                     "onto every status-less entry in a sprite_dir's "
                     "master_manifest.json. Idempotent -- safe to re-run.",
    )
    parser.add_argument(
        "--sprite-dir", type=Path, default=DEFAULT_SPRITE_DIR,
        help=f"sprite_dir whose master_manifest.json to migrate (default: "
             f"{DEFAULT_SPRITE_DIR})")
    args = parser.parse_args(argv)

    sprite_dir = args.sprite_dir.expanduser()
    migrated = _manifest.backfill_grandfathered_status(sprite_dir)

    if migrated:
        print(f"[backfill-manifest-status] migrated {len(migrated)} "
              f"entry(ies) in {sprite_dir} to status=\"grandfathered\" "
              f"at {migrated[0].get('grandfathered_at')}:")
        for entry in migrated:
            print(f"[backfill-manifest-status]   {entry.get('spec_key')} "
                  f"({entry.get('label')!r})")
    else:
        print(f"[backfill-manifest-status] nothing to migrate in "
              f"{sprite_dir} -- every entry already has a status.")


if __name__ == "__main__":
    main()
