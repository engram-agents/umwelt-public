#!/usr/bin/env python3
"""umwelt-unseen-hook — UserPromptSubmit: nudge on unseen crossings (v0 approx).

umwelt#697: this hook makes ONE stateless call, POST /umwelt/whats_new
`{viewer, since, per_place: 1}`, an all-places read. The world keeps no
memory of what this agent has read; the cursor is AGENT-SIDE, in this hook's
OWN state file (~/.umwelt/unseen-hook-state.json, `{"whats_new_since": N}`,
one global mark since seqs are global), kept strictly separate from the
agent's real read-cursor (uw's ~/.umwelt-client-state.json) and from the
bell's own cursor file — this hook must never advance or interfere with
either, and never touches a world-side standpoint.

The hook consumes COUNTS AND FLAGS ONLY, never headline text (this line is
injected on every prompt; it must stay inside the hook-injection bound):
  - "{A} for you"  = len(addressed) + sum(places[].for_you_count)
  - "{B} in the Square" = place_square's count - for_you_count
  - bell = any places[].for_you_wake, or any addressed[].wake_granted

Old-world fallback: if the world answers whats_new with HTTP 404 (a world
that predates the route), the hook falls back to the original two-place
crossings_here peek (the agent's home Post, place_post_<canonical-agent>,
and the Square), each with an explicit per-place `since` kept under the
state file's separate `fallback_places` key so the two paths never corrupt
each other's marks.

Golden rule: NEVER block or crash the session. Every failure mode (config
absent/malformed, world unreachable, bad JSON, timeout) is a silent no-op
that leaves the saved mark untouched (an auth failure is surfaced through
additionalContext instead). Silent at zero unseen too — no "0 unseen" line.

The very first poll (no `whats_new_since` yet: no file, empty file, or an
old per-place-format file) is also silent, no matter what it returns: it
polls with since=0 and only seeds the mark, same convention as the login
skill's proactive `hear` owning the *initial* backlog. Every SUBSEQUENT poll
counts normally. This keeps a fresh install's first prompt from dumping an
entire backlog as if it were fresh activity.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# umwelt #495/#646: client_auth is a sibling package under plugin/umwelt/ —
# add plugin/ (this hook's grandparent dir) to sys.path so a standalone
# invocation (no umwelt package on PYTHONPATH) can still `from umwelt import
# client_auth`. client_auth is deliberately stdlib-only at import time (see
# its own module docstring), so this does not compromise this hook's own
# "must run standalone" posture — no heavy dependency is pulled in unless a
# real identity key file is later found.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from umwelt import client_auth  # noqa: E402
from umwelt import engine_api  # noqa: E402

CONFIG_PATH = Path.home() / ".umwelt" / "config.json"
STATE_PATH = Path.home() / ".umwelt" / "unseen-hook-state.json"
TIMEOUT_S = 1.5  # the normal path is ONE whats_new call; against an old
                 # world the fallback makes two sequential polls (home Post +
                 # Square). Either must fit under the hooks.json
                 # UserPromptSubmit timeout with room to spare
SQUARE_PLACE_ID = "place_square"

# Session cache (umwelt #495/#646), now backed by an opt-in ON-DISK cache
# across invocations (umwelt #805, Kepler's 2026-09-18 security ruling):
# this hook is a fresh process per invocation, so an in-memory-only session
# previously only helped within one run (once per place polled below) --
# every invocation re-ran the full challenge->sign->login round-trip. The
# disk cache (client_auth.get_auth_header(..., cache=True), below) collapses
# that to ~once per token lifetime across invocations; see client_auth.py's
# module docstring for the five conditions this must (and does) satisfy.
_SESSION = client_auth.AgentSession()


# Ariadne's colleague-pass finding on PR#650: "LOUDLY to stderr" was an
# unverified claim about REACH. For a UserPromptSubmit hook, stderr is the
# harness's own hook-log channel (see ENGRAM's engram-baton-prompt-hook.py:
# "stderr (the hook log channel). Stdout is reserved for prompt context.")
# -- not surfaced to the model, not reliably surfaced to a human. The one
# channel that reaches a reader is stdout's `additionalContext`, the same
# one this hook already uses for its normal unseen-count line.
# `_LAST_AUTH_FAILURE` lets main() surface the failure through THAT channel.
_LAST_AUTH_FAILURE: str | None = None


def _auth_headers(world_url: str, agent: str) -> dict[str, str] | None:
    """Authorization header via client_auth (umwelt #495/#646), or the abort
    sentinel `None` on AuthFailure (umwelt#650 security fix, Mira).

    Mira's security-lane finding: returning `{}` on AuthFailure was
    indistinguishable from the genuine no-credential-configured case (which
    legitimately falls through to today's declared-identity compatibility
    branch). While the backfill window is open the server does not 401 a
    headerless request — it accepts it as declared identity — so a
    PRESENT-BUT-FAILING credential was silently downgraded to an
    UNAUTHENTICATED one. Three outcomes now:
      - success              -> {"Authorization": "Bearer ..."}  (send it)
      - no credential at all -> {}                               (send it — unchanged)
      - AuthFailure           -> None                             (ABORT — caller must not send)

    This hook's golden rule is still NEVER block or crash the session — so
    unlike `tools/uw` and `mud.py`, AuthFailure is caught here rather than
    re-raised — but the caller MUST check for `None` and skip the network
    call entirely (see `_crossings_here`) rather than treating a
    truthy-empty dict and an abort as the same thing.
    `_LAST_AUTH_FAILURE` still lets main() surface the failure via
    additionalContext (see the module comment above)."""
    global _LAST_AUTH_FAILURE
    try:
        return client_auth.get_auth_header(world_url, agent, _SESSION, cache=True)
    except client_auth.AuthFailure as e:
        _LAST_AUTH_FAILURE = str(e)
        print(f"umwelt-unseen-hook: auth failure for {agent}: {e}",
              file=sys.stderr)
        return None


def _canonical(agent: str) -> str:
    """Same fold as umwelt._ids.canonical_agent: strip + casefold. Duplicated
    here (not imported) since this hook must run standalone, without the
    umwelt package necessarily being importable. Keep in lockstep with
    src/umwelt/_ids.py::canonical_agent if that ever changes."""
    return (agent or "").strip().casefold()


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


def _post_json(world_url: str, agent: str, path: str,
               payload: dict) -> tuple[dict | None, int | None]:
    """POST `payload` to `path` with this hook's auth. Returns
    (parsed_response_or_None, http_error_code_or_None): the code is set only
    when the world answered with a non-2xx HTTP status (the caller needs it
    to tell an old world's 404 from every other failure); every other
    failure is (None, None). Read-only server-side for both routes this
    hook uses (never advances a real cursor)."""
    auth_headers = _auth_headers(world_url, agent)
    if auth_headers is None:
        # umwelt#650 security fix (Mira): AuthFailure means ABORT before
        # urlopen, never send this request unauthenticated. None here is
        # the present-but-failing-credential case, distinct from {} (no
        # credential configured, still fine to send per today's compat
        # mode).
        return None, None
    url = f"{world_url.rstrip('/')}{path}"
    headers = {"Content-Type": "application/json"}
    headers.update(auth_headers)
    headers.update(engine_api.client_headers())  # umwelt#1105
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST", headers=headers,
    )
    try:
        with client_auth.urlopen_with_cert_fallback(req, TIMEOUT_S) as r:
            body = r.read().decode()
            return (json.loads(body) if body else None), None
    except urllib.error.HTTPError as e:
        line = engine_api.mismatch_line_from_http_error(e)  # umwelt#1105 C2
        if line:
            print(line, file=sys.stderr)
        # Fixed in review (PR #828): urlopen() RAISES HTTPError for any
        # non-2xx response rather than returning a response object with
        # .status set -- the previous `if r.status == 401` / `if r.status
        # >= 400` branches inside the `with` block were both dead code,
        # silently absorbed by the generic URLError handler below (HTTPError
        # is a URLError subclass), so a real 401 never invalidated the
        # cache. Mirrors tools/uw's call_once(), which catches HTTPError
        # explicitly and gets this right.
        if e.code == 401 and auth_headers:
            # umwelt#805 condition #3: a 401 against a bearer-authed
            # request drops the (now possibly on-disk-cached) token --
            # this hook does not retry within the same invocation (tight
            # timeout budget, two places polled per run already; see the
            # module's "golden rule: NEVER block" posture), so this is a
            # self-heal for the NEXT invocation rather than an in-process
            # retry like uw's. Without this the on-disk cache would keep
            # serving the same dead token forever.
            _SESSION.invalidate()
        return None, e.code
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None, None


def _crossings_here(world_url: str, agent: str, place_id: str,
                     since: int | None) -> dict | None:
    """POST /umwelt/crossings_here (the old-world fallback path). Returns the
    parsed response dict, or None on any failure. Read-only server-side
    (never advances a real cursor); `since` is always passed explicitly by
    the caller so this hook never depends on (or mutates) the agent's own
    standpoint cursor."""
    payload = {"agent": agent, "place": place_id}
    if since is not None:
        payload["since"] = since
    return _post_json(world_url, agent, "/umwelt/crossings_here", payload)[0]


_NOT_FOUND = object()  # sentinel: the world answered whats_new with HTTP 404
                       # (an old world without the route) -> use the fallback.


def _whats_new(world_url: str, agent: str, since: int):
    """POST /umwelt/whats_new `{viewer, since, per_place: 1}` (stateless,
    all places; the cursor is this hook's own, passed explicitly).

    Returns (places, addressed, to_position) on a successful, well-formed
    response; `_NOT_FOUND` on HTTP 404 OR an old-shape 200 (a places[] entry
    without an int for_you_count, or an addressed[] entry without a bool
    wake_granted) -> caller falls back;
    None on ANY other failure (network, timeout, non-200, bad JSON, missing
    or mistyped places/addressed/to_position, auth abort) -- the caller must
    not touch the saved mark in that case."""
    resp, code = _post_json(world_url, agent, "/umwelt/whats_new",
                            {"viewer": agent, "since": since, "per_place": 1})
    if code == 404:
        return _NOT_FOUND
    if not isinstance(resp, dict):
        return None
    places, addressed = resp.get("places"), resp.get("addressed")
    to_position = resp.get("to_position")
    if (not isinstance(places, list) or not isinstance(addressed, list)
            or isinstance(to_position, bool) or not isinstance(to_position, int)):
        return None
    # OLD-SHAPE guard: a world that serves whats_new but predates the
    # for_you fields would make us report every Square item as broadcast and
    # count only addressed[] as for-you, silently. Rollout order is not
    # guaranteed (the plugin can reach seats before the world is promoted),
    # so any places[] entry lacking an int for_you_count routes exactly like
    # a 404 -- BEFORE any state is saved. An empty places list has nothing to
    # undercount and stays on this path. The same holds for addressed[]: an
    # entry that is not a dict or lacks a bool wake_granted comes from a
    # world predating that flag, which would silently lose the bell even
    # when places is empty.
    for p in places:
        fyc = p.get("for_you_count") if isinstance(p, dict) else None
        if isinstance(fyc, bool) or not isinstance(fyc, int):
            return _NOT_FOUND
    for a in addressed:
        if not isinstance(a, dict) or not isinstance(a.get("wake_granted"), bool):
            return _NOT_FOUND
    return places, addressed, to_position


def _int_field(d, key: str) -> int:
    """A non-negative int field off a response entry, 0 when absent or
    mistyped (a bool is not a count)."""
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


_POLL_FAILED = object()  # sentinel: distinguishes "no crossings ever" (a
                          # legitimate to_position of None) from "the poll
                          # itself failed" — dict.get(key, default) only
                          # applies its default when the key is ABSENT, not
                          # when the value is None, so a plain None return
                          # would conflate the two.


def _unseen_at(agent: str, world_url: str, place_id: str,
               low_water_mark: int | None):
    """Peek crossings at one place since low_water_mark.

    Returns (crossings, new_high_water_mark) on a successful, parsed poll —
    new_high_water_mark may legitimately be None (the place has never had a
    crossing). Returns (_POLL_FAILED, _POLL_FAILED) on any failed/unparseable
    poll — caller must NOT update state in that case. This is the old-world
    fallback only: it runs solely from _poll_fallback, when the world answers
    whats_new with HTTP 404 (a world that predates the route). The live path
    reads whats_new and never calls this. (GET /umwelt/unseen is deprecated
    and not a seam for this hook; see umwelt#883/#1004.)
    """
    resp = _crossings_here(world_url, agent, place_id, low_water_mark)
    if resp is None or not isinstance(resp.get("crossings"), list):
        return _POLL_FAILED, _POLL_FAILED
    crossings = resp["crossings"]
    to_position = resp.get("to_position", low_water_mark)
    return crossings, to_position


def main() -> None:
    _read_stdin_payload()  # not otherwise needed, but read+discard per contract

    config = _load_config()
    if config is None:
        return

    agent_raw = config["agent"].strip()
    agent = _canonical(agent_raw)
    try:
        world_url = client_auth.resolve_world_url()
    except client_auth.WorldUrlError as e:
        # umwelt#699/#710 (Ariadne's colleague-pass finding) -- see
        # session-start-hook.py's identical guard for the full rationale:
        # this hook's own outer `except Exception: pass` would otherwise
        # swallow the raise entirely, converting a config error into total
        # silence.
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    f"⚠️ umwelt unseen-hook: world URL config error "
                    f"({e}) -- unseen count not checked this turn; fix "
                    f"~/.umwelt/config.json."
                ),
            }
        }))
        return

    state = _load_state()
    result = _poll_whats_new(agent, world_url, state)
    if result is _NOT_FOUND:
        # Old world (no whats_new route): today's two-place peek, unchanged.
        addressed, broadcast, any_wake_granted = _poll_fallback(
            agent, world_url, state)
    elif result is None:
        # Failed/unparseable poll: the mark was NOT touched. Zero counts, so
        # the shared tail below stays silent (or surfaces an auth failure).
        addressed, broadcast, any_wake_granted = 0, 0, False
    else:
        addressed, broadcast, any_wake_granted = result

    total = addressed + broadcast
    if total <= 0:
        # Ariadne's PR#650 finding: a poll that failed because of an
        # AuthFailure must not read as "genuinely zero unseen" — surface it
        # via additionalContext (the channel that reaches a reader) rather
        # than falling through silently, same as a routine zero-unseen poll
        # would (and should continue to) do.
        if _LAST_AUTH_FAILURE:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        f"\U0001F512 umwelt unseen-hook: auth failure "
                        f"({_LAST_AUTH_FAILURE}) -- unseen count not "
                        f"checked this turn; re-enroll via "
                        f"tools/bind_client_agent.py if this persists."
                    ),
                }
            }))
        return

    bell = " \U0001F514" if any_wake_granted else ""
    context = (
        f"\U0001F4EC Umwelt: {addressed} for you{bell} · {broadcast} in "
        f"the Square — `uw hear`"
    )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


def _poll_whats_new(agent: str, world_url: str, state: dict):
    """The stateless path. Returns (addressed, broadcast, any_wake_granted)
    on a successful poll (after saving the new mark); `_NOT_FOUND` when the
    world has no whats_new route; None on any other failure (mark and state
    file left untouched)."""
    since_raw = state.get("whats_new_since")
    # First-ever = no usable mark: no file, empty file, or an OLD-format
    # per-place file (a dict without `whats_new_since`). Poll from 0 to seed
    # the mark; count nothing (the login skill's `hear` owns the backlog).
    first_ever = (isinstance(since_raw, bool) or not isinstance(since_raw, int)
                  or since_raw < 0)
    polled = _whats_new(world_url, agent, 0 if first_ever else since_raw)
    if polled is _NOT_FOUND or polled is None:
        return polled
    places, addressed_list, to_position = polled

    new_state: dict = {"whats_new_since": to_position}
    fallback = state.get("fallback_places")
    if isinstance(fallback, dict):
        new_state["fallback_places"] = fallback  # not ours to drop
    _save_state(new_state)
    if first_ever:
        return 0, 0, False

    # COUNTS AND FLAGS ONLY -- never headline text (see module docstring).
    addressed = len(addressed_list)
    broadcast = 0
    any_wake_granted = any(
        isinstance(a, dict) and a.get("wake_granted") is True
        for a in addressed_list)
    for p in places:
        if not isinstance(p, dict):
            continue
        for_you = _int_field(p, "for_you_count")
        addressed += for_you
        if p.get("for_you_wake") is True:
            any_wake_granted = True
        if p.get("place_id") == SQUARE_PLACE_ID:
            broadcast = max(0, _int_field(p, "count") - for_you)
    return addressed, broadcast, any_wake_granted


_RESERVED_STATE_KEYS = ("whats_new_since", "fallback_places")


def _poll_fallback(agent: str, world_url: str, state: dict):
    """The old-world path (umwelt#697 fallback): today's two-place
    crossings_here peek with per-place low-water-marks, unchanged except
    that the marks live under the state file's `fallback_places` key. A
    legacy file (place ids at the top level, no `whats_new_since`) is read
    as the per-place dict so an upgrade keeps its marks, and is rewritten in
    the new layout. Returns (addressed, broadcast, any_wake_granted)."""
    whole_state = state
    fallback_state = whole_state.get("fallback_places")
    if not isinstance(fallback_state, dict):
        fallback_state = {} if "whats_new_since" in whole_state else {
            k: v for k, v in whole_state.items()
            if k not in _RESERVED_STATE_KEYS}
    state = fallback_state  # everything below is the original per-place logic

    home_place_id = f"place_post_{agent}"
    places = [home_place_id, SQUARE_PLACE_ID]

    addressed = 0
    broadcast = 0
    any_wake_granted = False
    new_state = dict(state)

    for place_id in places:
        # Third distinction (beyond _POLL_FAILED vs a legitimate `to_position:
        # None`): has this hook EVER polled this place before? `place_id not
        # in state` means no — this is the very first sighting of the place,
        # and its backlog is not "unseen since my last prompt", it's the
        # entire history. Poll it anyway (to seed a real low-water-mark), but
        # don't count anything from it — same convention as the login
        # skill's proactive `hear` owning the initial-backlog surface. A
        # place already seen once (even with a stored mark of None, meaning
        # zero crossings ever) is NOT first-ever on the next call.
        first_ever_poll = place_id not in state
        low_water_mark = state.get(place_id)
        crossings, new_mark = _unseen_at(agent, world_url, place_id, low_water_mark)
        if crossings is _POLL_FAILED:
            # Failed/unparseable poll for this place — skip, preserve state.
            continue
        if not first_ever_poll:
            for c in crossings:
                if c.get("viewer_relation") == "sender":
                    # Self-authored crossing (server-computed: "sender" beats
                    # every other relation for a note-to-self) — never counts
                    # as unseen/unread TO the very agent who sent it (bug
                    # fix: a `say` you make must never bump your own
                    # notification counts). Checked before the
                    # "addressee"/Square branches below since "sender" is
                    # mutually exclusive with both.
                    continue
                if c.get("viewer_relation") == "addressee":
                    addressed += 1
                    if c.get("wake_granted"):
                        any_wake_granted = True
                elif place_id == SQUARE_PLACE_ID:
                    # Non-addressed crossing at the Square is what "{B} in
                    # the Square" means — count it. The same non-addressed
                    # shape at the agent's own home Post is an oddity (a
                    # broadcast sitting at your own Post isn't addressed to
                    # you and isn't "in the Square" either); drop it, `uw
                    # hear` catches it.
                    broadcast += 1
        new_state[place_id] = new_mark

    out_state: dict = {"fallback_places": new_state}
    if "whats_new_since" in whole_state:
        out_state["whats_new_since"] = whole_state["whats_new_since"]
    _save_state(out_state)
    return addressed, broadcast, any_wake_granted


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
