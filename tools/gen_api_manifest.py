#!/usr/bin/env python3
"""tools/gen_api_manifest.py -- generate api_manifest.json (umwelt#1105 M).

The manifest is the fingerprint of the engine API a client can rely on: every
route (rule + sorted methods), plus a digest of the engine-authored kinddefs.
It is GENERATED, never hand-edited, and deterministic (no timestamps, no paths):

    {"engine_api": N,
     "routes": [[rule, [methods sorted]], ...sorted by rule],
     "kinddef_digest": "<sha256>",
     "digest": "<sha256 of the canonical JSON of routes + kinddef_digest>"}

``engine_api`` is deliberately NOT inside the digest: the digest answers "did
the API change?", ``engine_api`` answers "did anyone bump the number?", and the
test suite compares the two through api_history.json (append-only,
``{"1": "<digest>", ...}``): ``history[str(ENGINE_API)] == manifest["digest"]``.

    python tools/gen_api_manifest.py            # write api_manifest.json (and
                                                # append history[N] if absent)
    python tools/gen_api_manifest.py --stdout   # print the manifest, write nothing

Writing REFUSES (exit 2, nothing written) when history already holds an entry
for this ENGINE_API with a DIFFERENT digest: that is a route or kinddef change
that was not accompanied by a bump of ENGINE_API in src/umwelt/engine_api.py.
Bump the number, then regenerate. An existing history entry is never rewritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

MANIFEST_PATH = REPO_ROOT / "api_manifest.json"
HISTORY_PATH = REPO_ROOT / "api_history.json"

_KINDDEF_SUFFIX = "_KINDDEF_JSON"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def build_test_app():
    """A fresh app on a throwaway db: the existing ``db.init_db`` +
    ``create_app`` fixture pattern. The temp dir lives as long as the app."""
    from umwelt import db
    from umwelt.server import create_app

    tmp = tempfile.TemporaryDirectory(prefix="umwelt-api-manifest-")
    path = str(Path(tmp.name) / "umwelt.db")
    conn = sqlite3.connect(path)
    db.init_db(conn)
    conn.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    app._manifest_tmpdir = tmp          # keep the directory alive with the app
    return app


def collect_kinddefs() -> dict:
    """``{constant name: dict}`` for every ``_*_KINDDEF_JSON`` constant in
    ``umwelt.server``, read at CALL time (a patched constant is seen)."""
    from umwelt import server

    return {name: value for name, value in sorted(vars(server).items())
            if name.startswith("_") and name.endswith(_KINDDEF_SUFFIX)
            and isinstance(value, dict)}


def kinddef_digest(kinddefs: dict) -> str:
    return hashlib.sha256(_canonical(kinddefs).encode()).hexdigest()


def compute_digest(routes, kinddef_digest_: str) -> str:
    """sha256 of the canonical JSON of ``routes`` and ``kinddef_digest`` only."""
    return hashlib.sha256(_canonical(
        {"routes": routes, "kinddef_digest": kinddef_digest_}).encode()).hexdigest()


def build_manifest(app, kinddefs: dict | None = None,
                   engine_api: int | None = None) -> dict:
    from umwelt import engine_api as _engine_api

    if kinddefs is None:
        kinddefs = collect_kinddefs()
    if engine_api is None:
        engine_api = _engine_api.ENGINE_API
    methods_by_rule: dict[str, set] = {}
    for rule in app.url_map.iter_rules():
        methods_by_rule.setdefault(rule.rule, set()).update(
            rule.methods - {"HEAD", "OPTIONS"})
    routes = [[rule, sorted(methods)] for rule, methods in sorted(methods_by_rule.items())]
    kd = kinddef_digest(kinddefs)
    return {
        "engine_api": engine_api,
        "routes": routes,
        "kinddef_digest": kd,
        "digest": compute_digest(routes, kd),
    }


def render(manifest: dict) -> str:
    """The exact text written to api_manifest.json."""
    return json.dumps(manifest, indent=1) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stdout", action="store_true",
                        help="print the manifest instead of writing files")
    args = parser.parse_args(argv)

    manifest = build_manifest(build_test_app())
    text = render(manifest)
    if args.stdout:
        sys.stdout.write(text)
        return 0

    history = json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else {}
    key = str(manifest["engine_api"])
    if key in history and history[key] != manifest["digest"]:
        print(f"gen_api_manifest: api_history.json already records a different "
              f"digest for ENGINE_API {key}; the routes or kinddefs changed "
              f"without a bump. Bump ENGINE_API in src/umwelt/engine_api.py, "
              f"then regenerate. Nothing written.", file=sys.stderr)
        return 2
    MANIFEST_PATH.write_text(text)
    if key not in history:
        history[key] = manifest["digest"]
        HISTORY_PATH.write_text(json.dumps(history, indent=1) + "\n")
        print(f"gen_api_manifest: appended history[{key}]")
    print(f"gen_api_manifest: wrote {MANIFEST_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
