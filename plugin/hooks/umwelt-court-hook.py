#!/usr/bin/env python3
"""umwelt-court-hook — UserPromptSubmit: surface the agent's in-world court
(umwelt #445 slice 1).

The turn-state system (projects/tasks with server-owned `turn_holder`, built
in #161/#221/#227/#247) had no prompt-time surfacing before this slice — an
agent only learned "a task is in my court" by polling. This hook renders
GET /umwelt/my_court at every prompt.

Design decision (load-bearing, do not relitigate — see the #445 slice-1
spec): the render is DELTA-FIRST, not level-first. A readout that prints the
same "N in your court" every prompt trains its reader to treat it as
furniture — that exact failure kept two bugs alive 31 days in a sibling
repo. So the render logic (see `decide_render`, the pure function this hook
factors the policy into) always distinguishes "the court actually changed
since I last looked" from "still the same" from "empty":

  - Fingerprint (sorted [task_id, status, turn_holder, updated_at] per
    task) CHANGED vs the hook's own state file -> full render, one line per
    task.
  - Fingerprint UNCHANGED and N > 0 -> a compact one-line reminder, at most
    once per 30 minutes (tracked via last_render_ts in state) — silent
    otherwise, so an unchanged court doesn't spam every prompt.
  - N == 0 -> silent, EXCEPT the one transition from non-zero to zero,
    which renders a single "emptied" line once.
  - First-ever run (no state file yet): treated as a changed fingerprint,
    i.e. a full render if there's anything in court. UNLIKE the unseen
    hook's seed-silent convention (that hook's first poll of a place is
    deliberately silent, because it's approximating a backlog it has no
    business replaying) — a my-court snapshot is standing state, not a
    backlog stream, so there is no replay concern here: what's in court
    right now is exactly what should render the first time this hook ever
    looks. (An empty court on the very first run still renders nothing —
    there is nothing to say and no non-zero state existed to have
    "emptied".)

Golden rule: NEVER block or crash the session. Every failure mode (config
absent/malformed, world unreachable, bad JSON, timeout) is a silent no-op.
Own state file (~/.umwelt/court-hook-state.json) — never touches the unseen
hook's, the bell's, or `uw`'s own cursor files.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
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
STATE_PATH = Path.home() / ".umwelt" / "court-hook-state.json"
TIMEOUT_S = 1.5  # single GET round-trip; keep well under the hooks.json
                 # UserPromptSubmit timeout
_REMINDER_INTERVAL_S = 30 * 60  # compact "unchanged" reminder cadence

# Session cache (umwelt #495/#646), now backed by an opt-in ON-DISK cache
# across invocations (umwelt #805) — see umwelt-unseen-hook.py's identical
# comment; this hook makes exactly one auth-header acquisition per
# invocation (my_court is polled once), so the in-memory layer mostly
# documented intent — the disk layer is what actually collapses repeat
# invocations to ~one login per token lifetime.
_SESSION = client_auth.AgentSession()


# Ariadne's colleague-pass finding on PR#650: "LOUDLY to stderr" was an
# unverified claim about REACH, and it does not hold. For a UserPromptSubmit
# hook, stderr is the harness's own hook-log channel (see e.g. ENGRAM's
# engram-baton-prompt-hook.py: "stderr (the hook log channel). Stdout is
# reserved for prompt context.") -- it is not surfaced to the model and is
# not reliably surfaced to a human either. The ONE channel this hook (or any
# UserPromptSubmit hook) can use to actually reach a reader is stdout's
# `hookSpecificOutput.additionalContext` -- the same channel this hook
# already uses for its normal court-status line. `_LAST_AUTH_FAILURE` lets
# main() surface the failure through THAT channel instead of relying on
# stderr alone, which -- worse than merely quiet -- was the exact silent-
# failure class #646 exists to kill, reproduced one layer over.
_LAST_AUTH_FAILURE: str | None = None


def _auth_headers(world_url: str, agent: str) -> dict[str, str] | None:
    """Authorization header via client_auth (umwelt #495/#646), or the abort
    sentinel `None` on AuthFailure (umwelt#650 security fix, Mira).

    Mira's security-lane finding on the original version of this function:
    returning `{}` on AuthFailure was indistinguishable from the genuine
    no-credential-configured case, which is meant to fall through to
    today's declared-identity compatibility branch. While the backfill
    window is open the server does not 401 a headerless request — it
    accepts it as declared identity — so a PRESENT-BUT-FAILING credential
    was silently downgraded to an UNAUTHENTICATED one, the exact silent
    downgrade #646 exists to prevent, reproduced at this call site instead
    of the bell's.

    Three distinct outcomes now, not two:
      - success            -> {"Authorization": "Bearer ..."}   (send it)
      - no credential at all -> {}                              (send it — today's compat behavior, unchanged)
      - AuthFailure (credential present but invalid) -> None     (ABORT — caller must not send)

    This hook's golden rule is still NEVER block or crash the session, so
    AuthFailure is caught here rather than re-raised; but the caller MUST
    check for `None` and skip the network call entirely (see `_my_court`)
    rather than treating a truthy-empty dict and an abort as the same
    thing. `_LAST_AUTH_FAILURE` still lets main() surface the failure via
    additionalContext (the channel that actually reaches a reader — see
    the module comment above)."""
    global _LAST_AUTH_FAILURE
    try:
        return client_auth.get_auth_header(world_url, agent, _SESSION, cache=True)
    except client_auth.AuthFailure as e:
        _LAST_AUTH_FAILURE = str(e)
        print(f"umwelt-court-hook: auth failure for {agent}: {e}", file=sys.stderr)
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


def _load_state() -> dict | None:
    """None means "no state file yet" (first-ever run) — distinct from an
    empty dict, which would mean a corrupt/truncated read. Mirrors the
    _POLL_FAILED-vs-None distinction the unseen hook makes for its own
    reasons: here the distinction is "first run" vs "every subsequent run",
    which decide_render's changed-vs-unchanged branch depends on."""
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_state(state: dict) -> None:
    tmp = str(STATE_PATH) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass  # never let a state-write failure crash the hook


def _my_court(world_url: str, agent: str) -> list | None:
    """GET /umwelt/my_court?agent=<agent>. Returns the `tasks` list on
    success, or None on any failure (unreachable, non-200, bad JSON, or an
    AuthFailure — see below) — the caller must treat None as "poll failed,
    don't touch state"."""
    headers = _auth_headers(world_url, agent)
    if headers is None:
        # umwelt#650 security fix (Mira): AuthFailure means ABORT before
        # urlopen, never send this request unauthenticated. A None here is
        # NOT "no headers" (that's {}, still fine to send) — it's the
        # present-but-failing-credential case, which must never be silently
        # downgraded to a declared-identity request.
        return None
    qs = urllib.parse.urlencode({"agent": agent})
    url = f"{world_url.rstrip('/')}/umwelt/my_court?{qs}"
    headers = {**headers, **engine_api.client_headers()}  # umwelt#1105
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with client_auth.urlopen_with_cert_fallback(req, TIMEOUT_S) as r:
            body = r.read().decode()
            data = json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        line = engine_api.mismatch_line_from_http_error(e)  # umwelt#1105 C2
        if line:
            print(line, file=sys.stderr)
        # Fixed in review (PR #828) -- see umwelt-unseen-hook.py's identical
        # comment: urlopen() raises HTTPError for a non-2xx response, so the
        # previous `if r.status == 401` check inside the `with` block was
        # dead code, silently absorbed by the URLError handler below.
        if e.code == 401 and headers:
            # umwelt#805 condition #3 -- see umwelt-unseen-hook.py's
            # identical comment: drop the (possibly on-disk-cached)
            # token so the NEXT invocation self-heals; no in-process
            # retry given this hook's tight timeout budget.
            _SESSION.invalidate()
        return None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return None
    return data["tasks"]


def _age_str(updated_at, now: float) -> str:
    """Humanized age of an ISO-8601 timestamp relative to `now` (epoch
    seconds). Any parse failure degrades to "?" — golden rule, never raise."""
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        secs = max(0, int(now - ts.timestamp()))
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
        return f"{secs // 86400}d{(secs % 86400) // 3600}h"
    except Exception:
        return "?"


def _fingerprint(tasks: list) -> list:
    # The feed is actionable-now (blocked tasks are excluded server-side per
    # the #161-ratified split), so a dependency completing elsewhere makes a
    # task APPEAR here — membership change IS the fingerprint change; no
    # blocked field exists to track (colleague round 2 on umwelt#454
    # dissolved the earlier blocked-in-fingerprint branch entirely).
    return sorted(
        [t.get("id"), t.get("status"), t.get("turn_holder"), t.get("updated_at")]
        for t in tasks
    )


def _full_render(tasks: list) -> str:
    lines = [f"\U0001F3BE {len(tasks)} task(s) in your in-world court:"]
    for t in tasks:
        blocked = ""  # feed is actionable-now; no blocked rows by construction
        gh = ""
        if t.get("gh_repo") and t.get("gh_ref"):
            gh = f" · {t['gh_repo']}#{t['gh_ref']}"
        lines.append(
            f"  - {t.get('project_label')} · {t.get('label')} "
            f"[{t.get('status')}]{blocked}{gh} — in court since "
            f"{t.get('updated_at')}"
        )
    lines.append(
        "  ↳ act: claim / hand_off / set_status on the project board "
        "(uw or MCP act)."
    )
    return "\n".join(lines)


def decide_render(prev_state: dict | None, tasks: list, now: float):
    """Pure render-decision function — the whole delta-first policy lives
    here, factored out for testability/clarity (see #445 slice-1 spec item
    6). Returns (text_or_None, new_state).

    prev_state: whatever _load_state returned last time (None on first-ever
    run; otherwise the dict this function itself returned as new_state on
    some earlier call — {"fingerprint": [...], "last_render_ts": float |
    None}).
    tasks: the `tasks` list from a successful /umwelt/my_court poll (caller
    must not call this on a failed poll — same "don't touch state on
    failure" posture as the unseen hook's _POLL_FAILED handling).
    now: caller-supplied clock (time.time()) — passed in, not read inside,
    so this function stays a pure function or the reminder-cadence branch
    is trivially unit-testable.
    """
    fingerprint = _fingerprint(tasks)
    n = len(tasks)
    first_run = prev_state is None
    prev_fingerprint = prev_state.get("fingerprint") if prev_state else None
    prev_last_render_ts = prev_state.get("last_render_ts") if prev_state else None
    changed = first_run or (fingerprint != prev_fingerprint)

    new_state = {
        "fingerprint": fingerprint,
        "last_render_ts": prev_last_render_ts,
    }

    if changed:
        if n == 0:
            prev_n = len(prev_fingerprint) if prev_fingerprint is not None else 0
            if not first_run and prev_n > 0:
                new_state["last_render_ts"] = now
                return "\U0001F3BE in-world court emptied.", new_state
            return None, new_state  # first-ever run with nothing in court
        new_state["last_render_ts"] = now
        return _full_render(tasks), new_state

    # Unchanged fingerprint.
    if n == 0:
        return None, new_state
    if prev_last_render_ts is None or (now - prev_last_render_ts) >= _REMINDER_INTERVAL_S:
        new_state["last_render_ts"] = now
        # The reminder must carry a DELTA-shaped handle, not a level: "N
        # unchanged" at a 30-min period is the same furniture failure one
        # octave down (colleague round 2 on umwelt#454). Tasks arrive
        # oldest-in-court first, so tasks[0] is the longest-waiting item;
        # its AGE moves every firing, and its name is an actionable handle.
        oldest = tasks[0]
        age = _age_str(oldest.get("updated_at"), now)
        label = (oldest.get("label") or oldest.get("id") or "?")
        return (
            f"\U0001F3BE in-world court: {n} task(s); oldest \u00b7 "
            f"{label} \u2014 waiting {age}."
        ), new_state
    return None, new_state


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
                    f"⚠️ umwelt court-hook: world URL config error "
                    f"({e}) -- court status not checked this turn; fix "
                    f"~/.umwelt/config.json."
                ),
            }
        }))
        return

    tasks = _my_court(world_url, agent)
    if tasks is None:
        # Ariadne's PR#650 finding: an AuthFailure must not fall through
        # this branch silently — a 401 IS a failed poll by _my_court's own
        # contract, so without this check a bad credential is
        # indistinguishable from a routine outage. Surface it via
        # additionalContext (the channel that reaches a reader), not stderr.
        if _LAST_AUTH_FAILURE:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        f"\U0001F512 umwelt court-hook: auth failure "
                        f"({_LAST_AUTH_FAILURE}) -- court status not "
                        f"checked this turn; re-enroll via "
                        f"tools/bind_client_agent.py if this persists."
                    ),
                }
            }))
        return  # failed/unparseable poll — leave state untouched

    prev_state = _load_state()
    text, new_state = decide_render(prev_state, tasks, time.time())
    _save_state(new_state)

    if not text:
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
