#!/usr/bin/env python3
"""comms-exposure-report.py -- how much of an agent's context the world's
comms channels consume, from Claude Code session transcripts.

Practice-measurement instrument for the umwelt comms round (design
principle P0: the agent controls its token exposure in layers). This is a
READ-ONLY reporter: it never modifies transcripts and never talks to the
world server -- it only reads Claude Code JSONL transcript files and
tabulates.

Usage:
    comms-exposure-report.py [PATH ...] [--since ISO8601] [--until ISO8601]
                              [--json] [--bucket {hour,day}]

With no PATH arguments, every ``*.jsonl`` file directly under
``~/.claude/projects/*/`` (not recursive into subdirectories) is read.

Contract (see the dispatching spec / test suite for the authoritative
statement):

  C1  bell attribution is by the `pager-bell` substring in a
      task-notification's `<summary>` only.
  C2  world-tool-call/result attribution is by tool_use_id mapping, never
      by inspecting result text.
  C3  bucketing is UTC hour (or day) from the line's `timestamp`; `--since`
      is inclusive, `--until` is exclusive.
  C4  a malformed line increments `skipped_lines` and processing continues;
      the tool always exits 0.
  C5  a tool_result whose id was never seen as a world tool_use increments
      `unmapped`.
  C6  an empty window produces zero-row tables and null chars_per_addressed.
  C7  this tool never opens a file for writing.
  C8  a bell can also arrive MID-TURN, as a `type: "attachment"` line whose
      `attachment.type == "queued_command"` and `attachment.commandMode ==
      "task-notification"` carries the notification text in
      `attachment.prompt` -- counted the same as a between-turns bell, by
      the same pager-bell-substring rule (C1).
  C9  a `type: "queue-operation"` line (enqueue / dequeue / remove, incl. a
      `remove` carrying `reason: absorbed_mid_turn`) is pure bookkeeping
      that repeats a bell's text; it is NEVER counted toward any counter,
      not even `skipped_lines`.
  C10 `bell_event_chars` (the `<event>...</event>` span) and
      `bell_injected_chars` (the actual context-cost: for a between-turns
      bell, the full message-content text; for a mid-turn attachment, the
      top-level `rendered` field's text when present, else `attachment.
      prompt`) are tracked as two separate columns. `world_chars_total`
      is computed from `bell_injected_chars`, not `bell_event_chars`. Each
      file result carries `injected_basis`: "rendered" / "prompt" / "mixed"
      / None, reporting which basis its mid-turn bells actually used.
  C11 hook-injected world context (SessionStart place snapshot, the
      per-prompt umwelt place line) is NOT part of world_chars_total --
      it's a distinct injection channel from bell + MCP tool results.
      Optionally, a `hook_additional_context` attachment whose text starts
      with "\U0001F4EC Umwelt:" or "\U0001F30D Umwelt:" is tallied
      separately into `hook_umwelt_chars`, which stays excluded from
      `world_chars_total`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

WORLD_TOOL_PREFIX = "mcp__plugin_umwelt_umwelt__"
BELL_SUMMARY_SUBSTRING = "pager-bell"
TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>(.*?)</task-notification>", re.DOTALL
)
SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
EVENT_RE = re.compile(r"<event>(.*?)</event>", re.DOTALL)


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def _parse_iso8601(value: str) -> datetime:
    """Parse an ISO8601 timestamp (with a trailing 'Z' meaning UTC) into an
    aware UTC datetime."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bucket_key(dt: datetime, bucket: str) -> str:
    if bucket == "day":
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%dT%H:00Z")


def default_since_until() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    since = now.replace(microsecond=0) - _timedelta_24h()
    return since, now


def _timedelta_24h():
    from datetime import timedelta

    return timedelta(hours=24)


# --------------------------------------------------------------------------
# Default transcript discovery
# --------------------------------------------------------------------------

def default_transcript_root() -> Path:
    return Path.home() / ".claude" / "projects"


def discover_default_transcripts() -> list[Path]:
    root = default_transcript_root()
    if not root.exists():
        return []
    found: list[Path] = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        for f in sorted(project_dir.glob("*.jsonl")):
            if f.is_file():
                found.append(f)
    return found


# --------------------------------------------------------------------------
# Bucket accumulator
# --------------------------------------------------------------------------

def _new_bucket() -> dict[str, Any]:
    return {
        "bell_events": 0,
        "bell_addressed": 0,
        "bell_event_chars": 0,
        "bell_injected_chars": 0,
        "hook_umwelt_chars": 0,
        "calls": {},
        "result_chars": {},
    }


def _add_call(bucket: dict[str, Any], verb: str) -> None:
    bucket["calls"][verb] = bucket["calls"].get(verb, 0) + 1


def _add_result_chars(bucket: dict[str, Any], verb: str, n: int) -> None:
    bucket["result_chars"][verb] = bucket["result_chars"].get(verb, 0) + n


# --------------------------------------------------------------------------
# Content-block text extraction
# --------------------------------------------------------------------------

def _iter_text_blocks(content: Any) -> Iterable[str]:
    """Yield every literal text string carried by a message's `content`,
    whether content is a bare string or a list of blocks. Only `text`-typed
    blocks (or the bare-string case) contribute -- other block types (e.g.
    tool_use, tool_reference) are skipped here; tool_use/tool_result are
    handled by their own dedicated walkers below."""
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    yield text


def _tool_result_text_len(content: Any) -> int:
    """Character count of a tool_result block's `content`, summing `text`
    fields when content is a list of blocks (non-text blocks contribute 0)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    total += len(text)
        return total
    return 0


def _iter_tool_uses(content: Any):
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def _iter_tool_results(content: Any):
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield block


# --------------------------------------------------------------------------
# Bell (task-notification) parsing
# --------------------------------------------------------------------------

def _iter_bell_events(text: str) -> Iterable[str]:
    """Yield the `<event>` text of every task-notification in `text` whose
    `<summary>` contains the pager-bell substring."""
    for m in TASK_NOTIFICATION_RE.finditer(text):
        body = m.group(1)
        summary_m = SUMMARY_RE.search(body)
        if not summary_m or BELL_SUMMARY_SUBSTRING not in summary_m.group(1):
            continue
        event_m = EVENT_RE.search(body)
        if event_m:
            yield event_m.group(1)


HOOK_UMWELT_PREFIXES = ("\U0001F4EC Umwelt:", "\U0001F30D Umwelt:")  # 📬 / 🌍


def _rendered_text_len(rendered: Any) -> int:
    """Character length of an attachment line's top-level `rendered` field
    -- the actual system-reminder text injected mid-turn. Seen on live
    transcripts as a list of blocks each carrying a `content` string; also
    accept a bare string defensively."""
    if isinstance(rendered, str):
        return len(rendered)
    if isinstance(rendered, list):
        total = 0
        for block in rendered:
            if isinstance(block, dict):
                c = block.get("content")
                if isinstance(c, str):
                    total += len(c)
            elif isinstance(block, str):
                total += len(block)
        return total
    return 0


# --------------------------------------------------------------------------
# Per-file analysis
# --------------------------------------------------------------------------

def analyze_file(
    path: Path,
    since: datetime | None,
    until: datetime | None,
    bucket_kind: str,
) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {}
    skipped_lines = 0
    counted_lines = 0
    unmapped = 0
    unparsed_hook_attachments = 0
    # id -> verb, across the whole file (a tool_use may appear in an
    # earlier message than its matching tool_result).
    id_to_verb: dict[str, str] = {}
    # ids seen as a tool_use but NOT a world tool -- these are simply
    # ignored on tool_result, never counted as unmapped.
    non_world_ids: set[str] = set()
    # bases actually used for mid-turn bell_injected_chars, this file only.
    basis_seen: set[str] = set()

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped_lines += 1
                continue
            if not isinstance(obj, dict):
                skipped_lines += 1
                continue
            line_type = obj.get("type")

            if line_type == "queue-operation":
                # C9: pure bookkeeping that repeats a bell's text -- never
                # counted toward anything, not even skipped_lines.
                continue

            if line_type == "attachment":
                # C8/C10/C11: mid-turn events. These lines carry no
                # `message` field -- their own top-level `timestamp` and
                # `attachment` dict are the whole of it.
                attachment = obj.get("attachment")
                timestamp = obj.get("timestamp")
                if not isinstance(attachment, dict) or not isinstance(timestamp, str):
                    skipped_lines += 1
                    continue
                try:
                    dt = _parse_iso8601(timestamp)
                except (ValueError, TypeError):
                    skipped_lines += 1
                    continue
                if since is not None and dt < since:
                    continue
                if until is not None and dt >= until:
                    continue

                att_type = attachment.get("type")
                if att_type == "queued_command":
                    prompt = attachment.get("prompt")
                    if not isinstance(prompt, str):
                        continue
                    matched_events = list(_iter_bell_events(prompt))
                    if not matched_events:
                        # C8: a non-bell queued_command attachment is not
                        # counted at all.
                        continue
                    counted_lines += 1
                    key = _bucket_key(dt, bucket_kind)
                    b = buckets.setdefault(key, _new_bucket())
                    for event_text in matched_events:
                        b["bell_events"] += 1
                        b["bell_event_chars"] += len(event_text)
                        if event_text.lstrip().startswith(("📬", "🔔")):
                            b["bell_addressed"] += 1
                    rendered = obj.get("rendered")
                    if rendered is not None:
                        b["bell_injected_chars"] += _rendered_text_len(rendered)
                        basis_seen.add("rendered")
                    else:
                        b["bell_injected_chars"] += len(prompt)
                        basis_seen.add("prompt")
                elif att_type == "hook_additional_context":
                    content = attachment.get("content")
                    # C11: the real shape is a LIST of strings (one hook's
                    # line can share a `content` list with other hooks'
                    # unrelated text) -- normalise a bare string to a
                    # one-item list; anything else is an unparsed shape,
                    # counted explicitly rather than silently read as 0.
                    if isinstance(content, str):
                        items = [content]
                    elif isinstance(content, list):
                        items = [i for i in content if isinstance(i, str)]
                    else:
                        items = None
                    if items is None:
                        unparsed_hook_attachments += 1
                    else:
                        matching = [i for i in items if i.startswith(HOOK_UMWELT_PREFIXES)]
                        if matching:
                            counted_lines += 1
                            key = _bucket_key(dt, bucket_kind)
                            b = buckets.setdefault(key, _new_bucket())
                            for item in matching:
                                b["hook_umwelt_chars"] += len(item)
                continue

            if line_type not in ("user", "assistant"):
                # Valid JSON, but not a message line (e.g. mode,
                # file-history-snapshot, summary) -- not an error, but not
                # counted as a message either. Spec: "Lines of other `type`
                # values ... are not messages" -- these are silently
                # skipped without incrementing skipped_lines, since they
                # are well-formed, just not messages.
                continue
            message = obj.get("message")
            timestamp = obj.get("timestamp")
            if not isinstance(message, dict) or not isinstance(timestamp, str):
                skipped_lines += 1
                continue
            try:
                dt = _parse_iso8601(timestamp)
            except (ValueError, TypeError):
                skipped_lines += 1
                continue

            # First pass on tool_use blocks: register id -> verb mapping
            # (or mark as a non-world id) regardless of window membership,
            # since a matching tool_result in a LATER message may fall
            # inside the window even if this tool_use line does not.
            content = message.get("content")
            for tu in _iter_tool_uses(content):
                tool_id = tu.get("id")
                name = tu.get("name")
                if not isinstance(tool_id, str) or not isinstance(name, str):
                    continue
                if name.startswith(WORLD_TOOL_PREFIX):
                    verb = name[len(WORLD_TOOL_PREFIX):]
                    id_to_verb[tool_id] = verb
                else:
                    non_world_ids.add(tool_id)

            if since is not None and dt < since:
                continue
            if until is not None and dt >= until:
                continue

            counted_lines += 1
            key = _bucket_key(dt, bucket_kind)
            b = buckets.setdefault(key, _new_bucket())

            # Bell events (task-notification text blocks, between turns).
            for text in _iter_text_blocks(content):
                matched_events = list(_iter_bell_events(text))
                if not matched_events:
                    continue
                for event_text in matched_events:
                    b["bell_events"] += 1
                    b["bell_event_chars"] += len(event_text)
                    if event_text.lstrip().startswith(("📬", "🔔")):
                        b["bell_addressed"] += 1
                # C10: the injected cost is the full message-content text
                # carrying the notification(s), counted once per text
                # block (not once per event) to avoid inflating it when a
                # block groups multiple notifications together.
                b["bell_injected_chars"] += len(text)

            # World tool_result attribution.
            for tr in _iter_tool_results(content):
                tool_use_id = tr.get("tool_use_id")
                if not isinstance(tool_use_id, str):
                    continue
                verb = id_to_verb.get(tool_use_id)
                if verb is not None:
                    _add_call(b, verb)
                    n = _tool_result_text_len(tr.get("content"))
                    _add_result_chars(b, verb, n)
                elif tool_use_id in non_world_ids:
                    # known id, but not a world tool -- ignored per spec.
                    pass
                else:
                    unmapped += 1

    if not basis_seen:
        injected_basis = None
    elif basis_seen == {"rendered"}:
        injected_basis = "rendered"
    elif basis_seen == {"prompt"}:
        injected_basis = "prompt"
    else:
        injected_basis = "mixed"

    return {
        "path": str(path),
        "buckets": buckets,
        "skipped_lines": skipped_lines,
        "counted_lines": counted_lines,
        "unmapped": unmapped,
        "injected_basis": injected_basis,
        "unparsed_hook_attachments": unparsed_hook_attachments,
    }


def _merge_bucket(dst: dict[str, Any], src: dict[str, Any]) -> None:
    dst["bell_events"] += src["bell_events"]
    dst["bell_addressed"] += src["bell_addressed"]
    dst["bell_event_chars"] += src["bell_event_chars"]
    dst["bell_injected_chars"] += src["bell_injected_chars"]
    dst["hook_umwelt_chars"] += src["hook_umwelt_chars"]
    for verb, n in src["calls"].items():
        dst["calls"][verb] = dst["calls"].get(verb, 0) + n
    for verb, n in src["result_chars"].items():
        dst["result_chars"][verb] = dst["result_chars"].get(verb, 0) + n


def _totals_for_file(file_result: dict[str, Any]) -> dict[str, Any]:
    totals: dict[str, Any] = {"buckets": {}, "skipped_lines": 0, "unmapped": 0}
    for key, b in file_result["buckets"].items():
        totals["buckets"][key] = dict(b)
        totals["buckets"][key]["calls"] = dict(b["calls"])
        totals["buckets"][key]["result_chars"] = dict(b["result_chars"])
    totals["skipped_lines"] = file_result["skipped_lines"]
    totals["unmapped"] = file_result["unmapped"]
    totals["unparsed_hook_attachments"] = file_result.get("unparsed_hook_attachments", 0)
    return totals


def analyze_files(
    paths: list[Path],
    since: str | None,
    until: str | None,
    bucket: str,
) -> dict[str, Any]:
    since_dt = _parse_iso8601(since) if since else None
    until_dt = _parse_iso8601(until) if until else None

    files_out = []
    total_buckets: dict[str, dict[str, Any]] = {}
    total_skipped = 0
    total_unmapped = 0
    total_unparsed_hook_attachments = 0

    for path in paths:
        fr = analyze_file(path, since_dt, until_dt, bucket)
        files_out.append(fr)
        total_skipped += fr["skipped_lines"]
        total_unmapped += fr["unmapped"]
        total_unparsed_hook_attachments += fr["unparsed_hook_attachments"]
        for key, b in fr["buckets"].items():
            tb = total_buckets.setdefault(key, _new_bucket())
            _merge_bucket(tb, b)

    return {
        "files": files_out,
        "total": {
            "buckets": total_buckets,
            "skipped_lines": total_skipped,
            "unmapped": total_unmapped,
            "unparsed_hook_attachments": total_unparsed_hook_attachments,
        },
    }


# --------------------------------------------------------------------------
# Derived metrics
# --------------------------------------------------------------------------

def _world_chars_total(b: dict[str, Any]) -> int:
    # C10/C11: world_chars_total is bell injections + MCP tool results
    # only -- hook-injected world context (hook_umwelt_chars) is a
    # distinct, deliberately-excluded channel.
    return b["bell_injected_chars"] + sum(b["result_chars"].values())


def _chars_per_addressed(b: dict[str, Any]) -> float | None:
    if b["bell_addressed"] == 0:
        return None
    return _world_chars_total(b) / b["bell_addressed"]


def _verbs_sorted_by_chars_desc(buckets: dict[str, dict[str, Any]]) -> list[str]:
    totals: dict[str, int] = {}
    for b in buckets.values():
        for verb, n in b["result_chars"].items():
            totals[verb] = totals.get(verb, 0) + n
        for verb in b["calls"]:
            totals.setdefault(verb, totals.get(verb, 0))
    return sorted(totals.keys(), key=lambda v: (-totals.get(v, 0), v))


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _fmt_chars_per_addressed(v: float | None) -> str:
    return "null" if v is None else f"{v:.1f}"


def _render_table_md(title: str, buckets: dict[str, dict[str, Any]]) -> str:
    verbs = _verbs_sorted_by_chars_desc(buckets)
    headers = [
        "bucket",
        "bell_events",
        "bell_addressed",
        "bell_event_chars",
        "bell_injected_chars",
        "hook_umwelt_chars",
    ]
    for verb in verbs:
        headers.append(f"{verb} calls/chars")
    headers += ["world_chars_total", "chars_per_addressed"]

    lines = [f"### {title}", "", "| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for key in sorted(buckets.keys()):
        b = buckets[key]
        row = [
            key,
            str(b["bell_events"]),
            str(b["bell_addressed"]),
            str(b["bell_event_chars"]),
            str(b["bell_injected_chars"]),
            str(b["hook_umwelt_chars"]),
        ]
        for verb in verbs:
            calls = b["calls"].get(verb, 0)
            chars = b["result_chars"].get(verb, 0)
            row.append(f"{calls}/{chars}")
        row.append(str(_world_chars_total(b)))
        row.append(_fmt_chars_per_addressed(_chars_per_addressed(b)))
        lines.append("| " + " | ".join(row) + " |")
    if not buckets:
        lines.append("| _(no data in window)_ |" + " |" * (len(headers) - 1))
    return "\n".join(lines)


def render_markdown(result: dict[str, Any]) -> str:
    parts = []
    for fr in result["files"]:
        parts.append(_render_table_md(fr["path"], fr["buckets"]))
        parts.append(
            f"\nskipped_lines: {fr['skipped_lines']}  \n"
            f"unmapped: {fr['unmapped']}  \n"
            f"unparsed_hook_attachments: {fr.get('unparsed_hook_attachments', 0)}  \n"
            f"injected_basis: {fr.get('injected_basis')}\n"
        )
    parts.append(_render_table_md("TOTAL", result["total"]["buckets"]))
    parts.append(
        f"\nskipped_lines: {result['total']['skipped_lines']}  \n"
        f"unmapped: {result['total']['unmapped']}  \n"
        f"unparsed_hook_attachments: {result['total'].get('unparsed_hook_attachments', 0)}\n"
    )
    return "\n".join(parts)


def _bucket_to_json(b: dict[str, Any]) -> dict[str, Any]:
    return {
        "bell_events": b["bell_events"],
        "bell_addressed": b["bell_addressed"],
        "bell_event_chars": b["bell_event_chars"],
        "bell_injected_chars": b["bell_injected_chars"],
        "hook_umwelt_chars": b["hook_umwelt_chars"],
        "calls": dict(b["calls"]),
        "result_chars": dict(b["result_chars"]),
        "world_chars_total": _world_chars_total(b),
        "chars_per_addressed": _chars_per_addressed(b),
    }


def render_json(result: dict[str, Any]) -> dict[str, Any]:
    out_files = []
    for fr in result["files"]:
        out_files.append(
            {
                "path": fr["path"],
                "buckets": {k: _bucket_to_json(v) for k, v in fr["buckets"].items()},
                "skipped_lines": fr["skipped_lines"],
                "unmapped": fr["unmapped"],
                "injected_basis": fr.get("injected_basis"),
                "unparsed_hook_attachments": fr.get("unparsed_hook_attachments", 0),
            }
        )
    total = {
        "buckets": {k: _bucket_to_json(v) for k, v in result["total"]["buckets"].items()},
        "skipped_lines": result["total"]["skipped_lines"],
        "unmapped": result["total"]["unmapped"],
        "unparsed_hook_attachments": result["total"].get("unparsed_hook_attachments", 0),
    }
    return {"files": out_files, "total": total}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Measure how much of an agent's context the world's comms "
            "channels consume, from Claude Code session transcripts. "
            "world_chars_total = bell injections + MCP tool results; "
            "hook-injected world context (SessionStart place snapshot, the "
            "per-prompt Umwelt place line) is NOT included there -- see "
            "the optional hook_umwelt_chars column."
        )
    )
    p.add_argument("paths", nargs="*", help="transcript JSONL path(s)")
    p.add_argument("--since", default=None, help="ISO8601 UTC, inclusive (default: 24h ago)")
    p.add_argument("--until", default=None, help="ISO8601 UTC, exclusive (default: now)")
    p.add_argument("--json", action="store_true", dest="as_json", help="emit JSON instead of markdown")
    p.add_argument("--bucket", choices=["hour", "day"], default="hour")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.paths:
        paths = [Path(p) for p in args.paths]
    else:
        paths = discover_default_transcripts()

    since = args.since
    until = args.until
    if since is None and until is None:
        since_dt, until_dt = default_since_until()
        since = since_dt.isoformat().replace("+00:00", "Z")
        until = until_dt.isoformat().replace("+00:00", "Z")

    result = analyze_files(paths, since=since, until=until, bucket=args.bucket)

    if args.as_json:
        print(json.dumps(render_json(result), indent=2, sort_keys=False))
    else:
        print(render_markdown(result))

    return 0


if __name__ == "__main__":
    sys.exit(main())
