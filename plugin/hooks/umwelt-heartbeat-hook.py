#!/usr/bin/env python3
"""umwelt-heartbeat-hook — debounced avatar-status heartbeat (umwelt #268
Phase 2 client slice). Fires on SessionStart/UserPromptSubmit/PreToolUse/
Stop/SessionEnd, each wired in hooks.json to a different state_hint via a
CLI arg, so the agent never has to remember to report its own status —
the harness hooks already fire on genuine activity; this just relays that.

    SessionStart      -> present
    UserPromptSubmit  -> active
    PreToolUse        -> active
    Stop              -> turn_end
    SessionEnd        -> asleep

POSTs {agent, state_hint, on_loop, activity_line?} to POST /umwelt/heartbeat
(umwelt #220's engine slice — see server.py's `heartbeat()` route for the
full contract this hook builds against). `on_loop` is whether
~/.engram/loop-mode.json exists right now; `activity_line` is that marker's
`topic` field (capped to the server's 120-char limit) when on_loop, else
omitted.

Debounce: a local state file remembers the last (state_hint, on_loop,
activity_line) triple beat and when. A beat only POSTs if any of those
three changed, or >=30s have passed since the last successful beat — this
mirrors the server's own 25s min-interval clamp (which fires on ANY field
change too, per Borges's fold), so client and server never disagree about
what counts as "new". The server-side clamp is the actual correctness
backstop (a throttled beat is cheap: 200, no write); this debounce exists
to keep a chatty session from POSTing on every single tool call.

Golden rule: NEVER block or crash the session. Every failure mode (no
argv, config absent/malformed, world unreachable, bad JSON, timeout,
non-2xx) is a silent no-op — this hook never prints anything, on any path;
it is pure side-effect plumbing, invisible by design (the whole point is
"no remember-to-update", including no visible confirmation clutter).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# umwelt #495/#646: client_auth is a sibling package under plugin/umwelt/ —
# add plugin/ (this hook's grandparent dir) to sys.path so a standalone
# invocation (no umwelt package on PYTHONPATH) can still `from umwelt import
# client_auth`. client_auth is deliberately stdlib-only at import time (see
# its own module docstring), so this does not compromise this hook's own
# "must run standalone" posture.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from umwelt import client_auth  # noqa: E402
from umwelt import engine_api  # noqa: E402

CONFIG_PATH = Path.home() / ".umwelt" / "config.json"
STATE_PATH = Path.home() / ".umwelt" / "heartbeat-hook-state.json"
LOOP_MODE_PATH = Path.home() / ".engram" / "loop-mode.json"
TIMEOUT_S = 2
DEBOUNCE_INTERVAL_S = 30
ACTIVITY_LINE_MAX_LEN = 120  # mirrors server.py's _HEARTBEAT_ACTIVITY_LINE_MAX_LEN
VALID_STATE_HINTS = frozenset({"active", "turn_end", "present", "asleep"})

# Session cache (umwelt #495/#646), now backed by an opt-in ON-DISK cache
# across invocations (umwelt #805) — see umwelt-unseen-hook.py's identical
# comment.
_SESSION = client_auth.AgentSession()


def _auth_headers(world_url: str, agent: str) -> dict[str, str] | None:
    """Authorization header via client_auth (umwelt #495/#646), or the abort
    sentinel `None` on AuthFailure (umwelt#650 security fix, Mira — this
    hook was her worst case: it MUTATES presence state, unauthenticated,
    and stayed invisible about it by design).

    Reporting stays silent — see the design note below — but the network
    call itself is a SEPARATE question, and Mira's finding is squarely
    about the call: returning `{}` on AuthFailure was indistinguishable
    from the genuine no-credential-configured case (which legitimately
    falls through to today's declared-identity compatibility branch).
    While the backfill window is open the server does not 401 a headerless
    request — it accepts it as declared identity — so a
    PRESENT-BUT-FAILING credential was silently downgraded to an
    UNAUTHENTICATED write. Three outcomes now:
      - success              -> {"Authorization": "Bearer ..."}  (send it)
      - no credential at all -> {}                               (send it — unchanged)
      - AuthFailure           -> None                             (ABORT — caller must not send)

    This hook's golden rule is still NEVER block or crash the session (it
    is "invisible by design" per the module docstring), so AuthFailure is
    caught here rather than re-raised and only logged to stderr — but the
    caller MUST check for `None` and skip the network call entirely (see
    `_post_heartbeat`) rather than treating a truthy-empty dict and an
    abort as the same thing.

    Deliberately NOT surfaced via additionalContext, unlike this PR's fix to
    the other three hooks (Ariadne's PR#650 colleague-pass finding that
    "LOUDLY to stderr" doesn't reach a reader): this hook's own module
    docstring predates this PR and states its silence is intentional across
    EVERY failure mode ("this hook never prints anything, on any path...
    invisible by design ... no visible confirmation clutter"), not specific
    to auth. Breaking that contract just for AuthFailure would be a bigger
    design change than a mechanical fix, and one already-silent failure mode
    joining the rest is not the same regression class as the OTHER three
    hooks, which do have a visible-on-success channel this specifically
    fails to use. If the heartbeat's own invisibility is ever revisited,
    that is a separate design conversation, not a PR#650 fixup. Reporting
    (silent) and the network call (must abort) are orthogonal; this
    docstring is careful to only change the second."""
    try:
        return client_auth.get_auth_header(world_url, agent, _SESSION, cache=True)
    except client_auth.AuthFailure as e:
        print(f"umwelt-heartbeat-hook: auth failure for {agent}: {e}", file=sys.stderr)
        return None


def _read_stdin_payload() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def _load_config() -> dict | None:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    agent = data.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        return None
    return data


def _read_loop_mode() -> dict | None:
    """~/.engram/loop-mode.json — same marker engram-loop writes/reads.
    Absent/malformed -> not on-loop, never a crash."""
    try:
        with open(LOOP_MODE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _on_loop_and_activity_line() -> tuple[bool, str | None]:
    loop = _read_loop_mode()
    if loop is None:
        return False, None
    topic = loop.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return True, None
    return True, topic[:ACTIVITY_LINE_MAX_LEN]


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    tmp = str(STATE_PATH) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass  # never let a state-write failure crash the hook


def _should_beat(state: dict, state_hint: str, on_loop: bool,
                  activity_line: str | None, now: float) -> bool:
    if not state:
        return True  # never beaten before
    changed = (state.get("state_hint") != state_hint
               or state.get("on_loop") != on_loop
               or state.get("activity_line") != activity_line)
    if changed:
        return True
    last_beat_at = state.get("last_beat_at")
    if not isinstance(last_beat_at, (int, float)):
        return True
    return (now - last_beat_at) >= DEBOUNCE_INTERVAL_S


def _post_heartbeat(world_url: str, agent: str, state_hint: str,
                     on_loop: bool, activity_line: str | None) -> bool:
    """POST /umwelt/heartbeat. Returns True on a 2xx response, False on any
    failure (unreachable, timeout, non-2xx) — caller must not persist state
    on False, so the next fire retries against the same 'changed' decision."""
    try:
        auth_headers = _auth_headers(world_url, agent)
        if auth_headers is None:
            # umwelt#650 security fix (Mira, the sharpest instance of the
            # finding): AuthFailure means ABORT before urlopen, never send
            # this WRITE unauthenticated. None here is the
            # present-but-failing-credential case, distinct from {} (no
            # credential configured, still fine to send per today's compat
            # mode). Same False-on-failure contract as any other failure —
            # caller doesn't persist state, next fire retries.
            return False
        url = f"{world_url.rstrip('/')}/umwelt/heartbeat"
        body = {"agent": agent, "state_hint": state_hint, "on_loop": on_loop}
        if activity_line is not None:
            body["activity_line"] = activity_line
        headers = {"Content-Type": "application/json"}
        headers.update(auth_headers)
        headers.update(engine_api.client_headers())  # umwelt#1105
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST", headers=headers,
        )
        try:
            with client_auth.urlopen_with_cert_fallback(req, TIMEOUT_S) as r:
                return r.status < 300
        except urllib.error.HTTPError as e:
            line = engine_api.mismatch_line_from_http_error(e)  # umwelt#1105 C2
            if line:
                print(line, file=sys.stderr)
            # Fixed in review (PR #828) -- see umwelt-unseen-hook.py's
            # identical comment: urlopen() raises HTTPError for a non-2xx
            # response, so the previous `if r.status == 401` check inside
            # the `with` block was dead code (silently absorbed as a
            # generic failure by the URLError handler below). Any HTTPError
            # means `r.status < 300` would have been False anyway -- only
            # the invalidate() call was actually broken. Nested one level
            # deeper than the other three hooks' fix because `auth_headers`
            # is acquired earlier in THIS function's own outer try, and this
            # inner scope is where it's guaranteed bound.
            if e.code == 401 and auth_headers:
                # umwelt#805 condition #3 -- see umwelt-unseen-hook.py's
                # identical comment: drop the (possibly on-disk-cached)
                # token so the NEXT invocation self-heals; no in-process
                # retry given this hook's tight timeout budget.
                _SESSION.invalidate()
            return False
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main() -> None:
    _read_stdin_payload()  # not otherwise needed, but read+discard per contract

    if len(sys.argv) < 2 or sys.argv[1] not in VALID_STATE_HINTS:
        return
    state_hint = sys.argv[1]

    config = _load_config()
    if config is None:
        return
    agent = config["agent"].strip()
    # umwelt#699/#710: resolve_world_url() can now raise WorldUrlError on a
    # malformed config, where it used to silently fall through. Caught here
    # and swallowed SILENTLY (no additionalContext) -- deliberately UNLIKE
    # the identical guard added to the other three hooks
    # (session-start/court/unseen), which surface it. This hook's own
    # "silent always" contract already predates this change and covers
    # EVERY failure mode uniformly (see _auth_headers's docstring above) --
    # one more silent failure mode joining that set is not a new regression
    # class, whereas the other three hooks have a visible-on-success channel
    # this specifically fails to use. Caught explicitly INSIDE main() rather
    # than left to the outer `except Exception: pass` wrapper so main() is
    # safe to call directly (as the test suite does), matching how
    # `_auth_headers` already handles AuthFailure internally rather than
    # relying on that same outer wrapper. If heartbeat's own invisibility is
    # ever revisited, that is a separate design conversation.
    try:
        world_url = client_auth.resolve_world_url()
    except client_auth.WorldUrlError:
        return

    on_loop, activity_line = _on_loop_and_activity_line()
    state = _load_state()
    now = time.time()
    if not _should_beat(state, state_hint, on_loop, activity_line, now):
        return

    ok = _post_heartbeat(world_url, agent, state_hint, on_loop, activity_line)
    if ok:
        _save_state({
            "state_hint": state_hint,
            "on_loop": on_loop,
            "activity_line": activity_line,
            "last_beat_at": now,
        })
    # Failure: leave state untouched, no output either way — silent always.


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Belt-and-suspenders: the golden rule is NEVER crash the session.
        pass
