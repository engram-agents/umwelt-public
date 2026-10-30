#!/usr/bin/env python3
"""umwelt-session-start-hook — SessionStart: walk the agent back into the world.

Reads ~/.umwelt/config.json ({"agent", "world_url"?, "description"?}), POSTs
/umwelt/enter, and drops a one-line place-snapshot into additionalContext —
a proactive nudge toward the umwelt-login skill, which is the only thing that
can actually arm the reactive bell (a hook process can't drive a session's
Monitor tool). This hook does NOT arm anything itself.

Golden rule: NEVER block or crash the session. Every failure mode (config
absent/malformed, world unreachable, bad JSON, timeout) is a silent no-op —
print nothing (or an empty additionalContext) and exit 0.
"""
from __future__ import annotations

import json
import sys
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
TIMEOUT_S = 3

# Session cache (umwelt #495/#646), now backed by an opt-in ON-DISK cache
# across invocations (umwelt #805) — see umwelt-unseen-hook.py's identical
# comment.
_SESSION = client_auth.AgentSession()


# Ariadne's colleague-pass finding on PR#650: "LOUDLY to stderr" was an
# unverified claim about REACH. For a SessionStart/UserPromptSubmit hook,
# stderr is the harness's own hook-log channel (see ENGRAM's
# engram-baton-prompt-hook.py: "stderr (the hook log channel). Stdout is
# reserved for prompt context.") -- not surfaced to the model, not reliably
# surfaced to a human. The one channel that reaches a reader is stdout's
# `additionalContext`, the same one this hook already uses for its normal
# place-snapshot line. `_LAST_AUTH_FAILURE` lets main() surface the failure
# through THAT channel.
_LAST_AUTH_FAILURE: str | None = None

# umwelt#1105 ruling 1: the world's engine-API gate answered /umwelt/enter with
# its 409. This hook is the best place in the system for that to surface, so
# `_enter` records the refusal body here and main() puts ONE line into
# additionalContext (the channel that reaches a reader).
_LAST_MISMATCH: dict | None = None
_REMEDY_CUT = 300


def _mismatch_context(body: dict) -> str:
    """`Umwelt: engine API mismatch (client <c>, engine <e>). <remedy>`;
    `none` for a null client_api, remedy cut at 300 characters."""
    client = body.get("client_api")
    return (f"Umwelt: engine API mismatch "
            f"(client {'none' if client is None else client}, "
            f"engine {body.get('engine_api')}). "
            f"{str(body.get('remedy'))[:_REMEDY_CUT]}")


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

    This hook's golden rule is still NEVER block or crash the session, so
    AuthFailure is caught here rather than re-raised — but the caller MUST
    check for `None` and skip the network call entirely (see `_enter`)
    rather than treating a truthy-empty dict and an abort as the same
    thing. `_LAST_AUTH_FAILURE` still lets main() surface the failure via
    additionalContext (see the module comment above)."""
    global _LAST_AUTH_FAILURE
    try:
        return client_auth.get_auth_header(world_url, agent, _SESSION, cache=True)
    except client_auth.AuthFailure as e:
        _LAST_AUTH_FAILURE = str(e)
        print(f"umwelt-session-start-hook: auth failure for {agent}: {e}",
              file=sys.stderr)
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


def _enter(world_url: str, agent: str) -> dict | None:
    """POST /umwelt/enter — same urllib pattern as mcp_server.py's _request."""
    auth_headers = _auth_headers(world_url, agent)
    if auth_headers is None:
        # umwelt#650 security fix (Mira): AuthFailure means ABORT before
        # urlopen, never send this request unauthenticated. None here is
        # the present-but-failing-credential case, distinct from {} (no
        # credential configured, still fine to send per today's compat
        # mode).
        return None
    url = f"{world_url.rstrip('/')}/umwelt/enter"
    payload = json.dumps({"agent": agent}).encode()
    headers = {"Content-Type": "application/json"}
    headers.update(auth_headers)
    headers.update(engine_api.client_headers())  # umwelt#1105
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    try:
        with client_auth.urlopen_with_cert_fallback(req, TIMEOUT_S) as r:
            body = r.read().decode()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        refusal = engine_api.refusal_from_http_error(e)  # umwelt#1105
        if refusal is not None:
            global _LAST_MISMATCH
            _LAST_MISMATCH = refusal
            print(engine_api.render_mismatch(refusal), file=sys.stderr)
        # Fixed in review (PR #828) -- see umwelt-unseen-hook.py's identical
        # comment: urlopen() raises HTTPError for a non-2xx response, so the
        # previous `if r.status == 401` check inside the `with` block was
        # dead code, silently absorbed by the URLError handler below.
        if e.code == 401 and auth_headers:
            # umwelt#805 condition #3 -- see umwelt-unseen-hook.py's
            # identical comment: drop the (possibly on-disk-cached)
            # token so the NEXT invocation self-heals; no in-process
            # retry given this hook's tight timeout budget.
            _SESSION.invalidate()
        return None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def _build_context(resp: dict) -> str | None:
    place = resp.get("place") or {}
    label = place.get("label")
    if not label:
        return None
    present = resp.get("present") or []
    unseen_count = resp.get("unseen_count") or 0
    n_present = len(present)
    return (
        f"\U0001F30D Umwelt: you're at {label} — {n_present} present, "
        f"{unseen_count} unseen. Run the umwelt-login skill to arm your senses."
    )


def main() -> None:
    _read_stdin_payload()  # not otherwise needed, but read+discard per contract

    config = _load_config()
    if config is None:
        return

    agent = config["agent"].strip()
    try:
        world_url = client_auth.resolve_world_url()
    except client_auth.WorldUrlError as e:
        # umwelt#699/#710 (Ariadne's colleague-pass finding): resolve_world_url()
        # now raises on a malformed config or a plaintext non-loopback value,
        # where it used to silently fall through -- and this hook's own outer
        # `except Exception: pass` (the golden NEVER-crash-the-session rule)
        # would otherwise swallow that raise entirely, converting a config
        # error into total silence rather than the plaintext-default fallback
        # this hook used to get. Same channel/pattern as the AuthFailure
        # surfacing just below -- additionalContext is what a reader sees.
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    f"⚠️ umwelt session-start-hook: world URL "
                    f"config error ({e}) -- could not enter the world this "
                    f"session; fix ~/.umwelt/config.json."
                ),
            }
        }))
        return

    resp = _enter(world_url, agent)
    if resp is None:
        if _LAST_MISMATCH is not None:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": _mismatch_context(_LAST_MISMATCH),
                }
            }))
            return
        # Ariadne's PR#650 finding: an AuthFailure must not fall through
        # this branch silently — surface it via additionalContext (the
        # channel that reaches a reader), not stderr alone.
        if _LAST_AUTH_FAILURE:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": (
                        f"\U0001F512 umwelt session-start-hook: auth "
                        f"failure ({_LAST_AUTH_FAILURE}) -- could not "
                        f"enter the world this session; re-enroll via "
                        f"tools/bind_client_agent.py if this persists."
                    ),
                }
            }))
        return

    context = _build_context(resp)
    if not context:
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Belt-and-suspenders: the golden rule is NEVER crash the session.
        pass
