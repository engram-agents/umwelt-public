#!/usr/bin/env python3
"""Playwright-driven visual regression check for avatar-overlap legibility
(umwelt #275 / #277 / #282).

umwelt#275/PR#277 fixed a bug where a self-cursor avatar landing exactly on
top of another avatar in the two-lane street was rendered fully invisible:
the CSS treatment (opacity + dashed outline on `.avatar-overlapped`) is
computed correctly, but has ZERO visible effect whenever the self-cursor's
`getBoundingClientRect()` is byte-for-byte identical to the occluded
avatar's rect — a same-size, fully-opaque element painted exactly on top of
another erases any opacity/outline styling on the one underneath, no matter
how "correct" the class toggling is. PR#277's fix adds
`transform: translateX(-14px)` to `.avatar-overlapped` so the occluded
avatar's rect is always visibly offset from the self-cursor's rect.

This class of bug is INVISIBLE to this repo's existing pytest coverage
(the two `test_*avatar_overlap*` tests in the internal webrender test file, not in this release):
those only assert that the served `app.js`/`index.html` TEXT contains the
right function/class names — they were green on the broken code, because
"the CSS rule exists in the served file" and "the CSS rule has any visible
effect once actually laid out and painted by a real browser" are different
facts. Catching the second fact requires an actual browser computing real
layout — hence this standalone Playwright tool rather than a pytest test.

Per Kepler's 2026-07-22 ruling on umwelt#282 (after discussion with Borges):
adding a headless-Chromium dependency to the PER-PR CI pipeline would be an
ongoing cost increase against this repo's standing "CI quota is tight/
expensive, keep it minimal" directive (the internal CI config's own
comment, not in this release) — not something to add unilaterally. So this tool is NOT wired
into `ci.yml`. Instead:

  - Run it locally whenever a change touches `.avatar-cursor` /
    `.avatar-overlapped` / `.avatar-overlapping` CSS or the overlap-
    detection JS (`updateAvatarOverlaps` in app.js) — same
    local-run-is-the-primary-gate discipline this repo already uses for
    the pytest suite (see the Merge-readiness section of the internal contributor guide).
  - Or fire the `render-regression` GitHub Action
    (an internal CI workflow, not in this release) by hand
    (`workflow_dispatch` only — it never runs on push/PR) when a reviewer's
    local environment has no browser to test with.

See the internal webrender test file (search "umwelt #282"; not in this release) for the
pointer left next to the served-source tests this tool complements, and
umwelt issue #282 for the full tracking discussion.

What this script does:
  1. Spawns the webrender gateway (`umwelt.webrender.gateway`'s `create_app`
     via `python3 -m umwelt.webrender`) as a real subprocess on a free local
     port, pointed at a throwaway (never-contacted) `--world-url` -- this
     check never hits `/scene` or any other gateway route that talks to the
     world server, so no real world server is needed.
  2. Loads the gateway's served `/` page in a real (headless) Chromium tab
     via Playwright, sets `state.identity` to a mock agent, and calls the
     page's own global `render()` function directly with a small synthetic
     scene (multiple avatars, one upper-lane thing) -- the same scene shape
     used during PR#277's original live-render investigation, reproduced
     here as a first-class, reusable check.
  3. Reads back `getBoundingClientRect()` for the self-cursor
     (`.avatar-cursor`) and for the first non-self avatar in DOM order
     (the one PR#277's bug made invisible, since `cursorIndex` defaults to
     0 on every fresh scene and both lanes share the same left origin).
  4. Asserts the two rects are NOT byte-for-byte identical -- if they are,
     the occluded avatar is being fully painted-over again, i.e. the
     regression this tool exists to catch has come back.
  5. Also prints `getComputedStyle()` (opacity/outline/transform) for the
     occluded avatar so a human reviewer sees the actual applied CSS
     treatment, not just a boolean pass/fail.

Usage:
  # local run (no arguments required -- picks a free port, sets
  # PYTHONPATH itself for the spawned gateway subprocess)
  python3 tools/rect_overlap_regression.py

  # pin a port instead of auto-selecting one (debugging only)
  python3 tools/rect_overlap_regression.py --port 5511

Exit codes:
  0  pass -- the occluded avatar's rect is visibly distinct from self's.
  1  regression detected -- the two rects are byte-for-byte identical (the
     bug this tool exists to catch).
  2  Playwright itself (the package, or a cached Chromium build) is not
     available in this environment -- install it and re-run:
       pip install playwright && playwright install chromium
  3  the gateway subprocess never became ready (environment/startup
     problem unrelated to the regression itself).
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_MISSING_DEPENDENCY = 2
EXIT_GATEWAY_NOT_READY = 3

# Same scene shape proven (during PR#277's original manual investigation)
# to reproduce the bug: `cursorIndex` defaults to 0 on every fresh scene,
# targeting the single upper-lane thing below -- and since the self-cursor
# avatar (removed from #street-lower's normal flex flow) and the first
# *other* avatar left in #street-lower's flex flow both align to the same
# left origin, the self-cursor's computed position lands exactly on top of
# that first other avatar.
MOCK_SCENE: dict[str, Any] = {
    "place": {"label": "The Square", "description": "test"},
    "parent": None,
    "things": [{"id": "t1", "name": "sign", "kind": "sign", "sprite_ref": None}],
    "exits": [],
    "avatars": [
        {"agent": a, "avatar_id": f"a-{a}", "name": a.capitalize(), "emotion": "neutral"}
        for a in ["luria", "kepler", "borges", "ariadne", "sol"]
    ],
}

# The mock scene's `identity` (self) and the expected first-non-self avatar
# by DOM order (avatars list order, minus self) -- "kepler" here.
SELF_IDENTITY = "luria"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _check_playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "ERROR: the `playwright` package is not installed in this "
            "environment.\n  pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
    except Exception as exc:  # noqa: BLE001 - any launch failure means "not usable"
        print(
            "ERROR: Playwright's Chromium browser isn't installed/cached in "
            f"this environment ({exc}).\n"
            "  pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return False
    return True


def _wait_ready(url: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.1)
    return False


def _start_gateway(port: int) -> subprocess.Popen:
    """Spawns the gateway (`python -m umwelt.webrender`) as a real
    subprocess, pointed at a throwaway `--world-url` this check never
    contacts (it drives `render()` directly, in-page, never `/scene`).
    """
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(SRC_DIR) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    )
    return subprocess.Popen(
        [
            sys.executable, "-m", "umwelt.webrender",
            "--port", str(port),
            "--world-url", "http://127.0.0.1:1",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def run_check(port: int) -> int:
    from playwright.sync_api import sync_playwright

    base_url = f"http://127.0.0.1:{port}"
    gateway = _start_gateway(port)
    try:
        if not _wait_ready(f"{base_url}/", timeout=15.0):
            gateway.poll()
            output = ""
            if gateway.stdout is not None:
                output = gateway.stdout.read().decode("utf-8", errors="replace")
            print(
                f"ERROR: gateway at {base_url} never became ready.\n"
                f"--- gateway output ---\n{output}",
                file=sys.stderr,
            )
            return EXIT_GATEWAY_NOT_READY

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"{base_url}/")
                page.evaluate(
                    "(identity) => { state.identity = identity; }", SELF_IDENTITY,
                )
                page.evaluate("(scene) => { render(scene); }", MOCK_SCENE)
                # `.avatar-cursor` carries `transition: left .12s ease, top
                # .12s ease` -- give any transition triggered by this
                # render() call a moment to settle before measuring, so a
                # mid-transition rect never gets mistaken for the resting
                # position.
                page.wait_for_timeout(200)

                self_rect = page.eval_on_selector(
                    ".avatar-cursor",
                    "el => { const r = el.getBoundingClientRect(); "
                    "return {left: r.left, top: r.top, right: r.right, bottom: r.bottom}; }",
                )
                overlapped_rect = page.eval_on_selector(
                    "#street-lower .item.avatar",
                    "el => { const r = el.getBoundingClientRect(); "
                    "return {left: r.left, top: r.top, right: r.right, bottom: r.bottom}; }",
                )
                overlapped_style = page.eval_on_selector(
                    "#street-lower .item.avatar",
                    "el => { const cs = getComputedStyle(el); "
                    "return {opacity: cs.opacity, outline: cs.outline, "
                    "transform: cs.transform}; }",
                )
            finally:
                browser.close()

        print(f"self-cursor (.avatar-cursor) rect:      {self_rect}")
        print(f"occluded avatar (first in #street-lower) rect: {overlapped_rect}")
        print(f"occluded avatar computed style:          {overlapped_style}")

        identical = (
            self_rect["left"] == overlapped_rect["left"]
            and self_rect["top"] == overlapped_rect["top"]
            and self_rect["right"] == overlapped_rect["right"]
            and self_rect["bottom"] == overlapped_rect["bottom"]
        )
        if identical:
            print(
                "\nFAIL: the self-cursor's rect and the occluded avatar's "
                "rect are byte-for-byte identical. This is umwelt#275's "
                "original bug: the occluded avatar's opacity/outline "
                "styling is computed correctly (see the printed computed "
                "style above) but has ZERO visible effect because it is "
                "painted in the exact same screen position as the "
                "self-cursor sitting on top of it. Expected the fix's "
                "`transform: translateX(-14px)` on `.avatar-overlapped` "
                "to offset the occluded avatar's rect -- it did not.",
                file=sys.stderr,
            )
            return EXIT_REGRESSION

        print(
            "\nPASS: the occluded avatar's rect is visibly distinct from "
            "the self-cursor's rect (offset present) -- the umwelt#275/"
            "#277 fix is in effect."
        )
        return EXIT_OK
    finally:
        if gateway.poll() is None:
            gateway.terminate()
            try:
                gateway.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gateway.kill()
                gateway.wait(timeout=5)
        if gateway.stdout is not None:
            gateway.stdout.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Playwright-driven avatar-overlap-legibility regression check "
            "(umwelt #275/#277/#282). See this file's module docstring for "
            "the full contract."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="port for the spawned gateway subprocess (default: an "
             "automatically-selected free local port).",
    )
    args = parser.parse_args()

    if not _check_playwright_available():
        return EXIT_MISSING_DEPENDENCY

    port = args.port if args.port is not None else _free_port()
    return run_check(port)


if __name__ == "__main__":
    sys.exit(main())
