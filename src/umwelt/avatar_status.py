"""Shared avatar-status render logic (umwelt #178).

Factors the kind-dispatch for `last_action` and the Phase 2 (#268)
`state`/`activity_line` composition out of `tools/uw`, `mcp_server.py`, and
`mud.py`'s three near-identical hand rolls — found by PR #177's
reviewer-fairy: "if `last_action` ever grows a new kind, three call sites
must change and one will be missed."

Each client keeps its own PRESENTATION: `tools/uw` and `mcp_server.py` use a
multi-line labeled block ("last action: used X at T"); `mud.py` uses a
compact single-line bullet joined by " · " with a relative timestamp
instead. This module only factors the shared INTERPRETATION — what a
`last_action` dict or a `state`/`activity_line` pair actually MEANS — never
how a specific renderer lays it out. That's why `describe_last_action`
returns the descriptive core only ("used X"), not a full line: the label
prefix and the timestamp suffix are exactly the parts that legitimately
differ per client.

`activity_line` is caller-declared public text (the #268 heartbeat
endpoint, forgeable v0 identity). This module never interprets it
as markup, only ever returns it as a plain string for a caller to place
directly into terminal/tool-result text. Any future HTML consumer (the
web-render gateway, forum #267/M3/M5) MUST escape it before inserting into
markup — this module's plain-text output does not by itself make that
consumer safe.
"""
from __future__ import annotations


def describe_last_action(last_action: dict | None) -> str | None:
    """Kind-dispatch for #143's `last_action` shape — `None`,
    `{"kind": "crossing", "at", "destination"}`, or
    `{"kind": "use", "at", "thing_id"}`. Returns the descriptive core only
    ("spoke in X" / "used Y" / the raw kind for a forward-compat unknown
    kind), never the raw JSON (#146) and never a full "last action: ..."
    line — callers own the label and any timestamp suffix.

    Guards against a non-dict `last_action` (e.g. a stray truthy string) —
    the current server contract only ever emits dict-or-None, so this can't
    fire today, but this function is now the single funnel all 3 clients
    share, and mud.py's pre-refactor inline check had this guard (Ariadne's
    colleague review on #226)."""
    if not isinstance(last_action, dict) or not last_action.get("kind"):
        return None
    kind = last_action.get("kind")
    if kind == "crossing":
        return f"spoke in {last_action.get('destination')}"
    if kind == "use":
        return f"used {last_action.get('thing_id')}"
    return str(kind)


def describe_state(status: dict) -> str | None:
    """Phase 2 (#268): the working/idle/on-loop (idle)/away/asleep display
    state, with its optional `activity_line` folded in as "<state> —
    <line>" when present. None when the agent has never sent a heartbeat —
    `status` has no `state` key at all (server.py's `_avatar_status` omits
    it, never nulls it, for a pre-heartbeat agent), same "no shape change
    for existing consumers" posture as gate_notice/checklist_render."""
    state = status.get("state")
    if not state:
        return None
    activity_line = status.get("activity_line")
    if activity_line:
        return f"{state} — {activity_line}"
    return state
