"""Shared @-mention parser (umwelt #511 PR A).

Before this module there were three hand-copied `@(\\w+)` parsers (the MCP
`say` wrapper + its plugin mirror, and `tools/uw`) with no quote or code
skipping, and the web gateway forwarded no `mentions` at all. This module is
the one shared implementation `parse_mentions` that all mention-scanning
call sites are meant to use (PR A wires the web gateway onto it; PR B, a
separate follow-up, moves the two hand-copied agent-path parsers onto it
too — see umwelt #511).

`parse_mentions` returns names RAW — not case-folded, not checked against
the agent registry. The engine (`send_crossing`) already owns folding,
registry validation, self/duplicate dropping, and the wake gate; this module
does not duplicate any of that. It only answers "what @-mention tokens does
this text actually contain," which is a strictly textual question:

- An `@name` token's name is the run of word characters (`\\w+`) after the
  `@` — trailing punctuation (a comma, a period, ...) is never part of the
  name, because it isn't a word character.
- An `@` preceded by a word character or by another `@` is not a mention
  (so `a@b.com` and `@@kepler` yield nothing) — this is what keeps an email
  address or a doubled `@@` from being read as addressing sugar.
- A line whose first non-space character is `>` is a quoted line and is
  skipped entirely. This is the load-bearing case for the web UI: the reply
  button pre-fills the quoted message as `> sender: <quote>`, and an
  `@name` inside that quote must not re-ring anyone.
- Text inside fenced code (a ``` ``` ``` or `~~~` fence) or inside an inline
  code span (a `` `...` `` backtick run) is skipped — an `@name` written as
  an example in a code sample is not addressing sugar either. Fence
  detection follows the CommonMark rules that matter here: an opening
  fence is a line whose first non-space run is >=3 backticks or >=3
  tildes (a backtick fence's info string may not itself contain a
  backtick, or the line isn't an opener at all; a tilde fence's info
  string may); a closing fence must use the SAME character as its opener,
  a run length >= the opener's, and nothing but whitespace after the run
  — a fence-looking line that doesn't match closes nothing and is itself
  fence content; an unclosed fence runs to the end of the message.

Names are returned in first-seen order, de-duplicated.
"""

from __future__ import annotations

import re

# The name half of a mention: `@` not preceded by a word character or
# another `@`, followed by a run of word characters. The lookbehind is what
# rules out `a@b.com` (preceded by `a`) and `@@kepler` (the second `@`
# preceded by the first `@`).
_MENTION_RE = re.compile(r"(?<![\w@])@(\w+)")

# A fence-delimiter line: leading whitespace, then a run of 3+ backticks or
# 3+ tildes (captured), then the rest of the line (the info string, for an
# opener; must be pure whitespace for a valid closer). Whether a given match
# is actually an opener or a closer is a stateful judgment `parse_mentions`
# makes below (CommonMark rule: a closing fence must match its opener's
# character AND have a run length >= the opener's; a backtick opener's info
# string may not itself contain a backtick, while a tilde opener's may) —
# this regex only locates the fence-shaped run, it doesn't validate it.
_FENCE_RUN_RE = re.compile(r"^\s*(`+|~+)(.*)$")

_FENCE_MIN_RUN = 3

# An inline code span: a backtick, a run of non-backtick/non-newline
# characters, and a closing backtick. Applied per-line (after fenced-code
# lines have already been dropped), so it never crosses a newline.
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# A quoted line: first non-space character is '>'.
_QUOTE_LINE_RE = re.compile(r"^\s*>")


def parse_mentions(text: str) -> list[str]:
    """Return the @-mentioned names in `text`, raw, order-preserving,
    de-duplicated. See the module docstring for the full contract."""
    if not text:
        return []

    names: list[str] = []
    seen: set[str] = set()
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in text.split("\n"):
        run_match = _FENCE_RUN_RE.match(line)
        run_char = run_match.group(1)[0] if run_match else ""
        run_len = len(run_match.group(1)) if run_match else 0
        rest = run_match.group(2) if run_match else ""
        is_fence_run = run_len >= _FENCE_MIN_RUN

        if in_fence:
            # Only a run of the SAME character, with length >= the
            # opener's, and nothing but whitespace after it, closes the
            # fence. Anything else fence-shaped (wrong character, too
            # short, or trailing non-whitespace) is fence content, not a
            # closer — it stays inside the block, unscanned.
            if (
                is_fence_run
                and run_char == fence_char
                and run_len >= fence_len
                and rest.strip() == ""
            ):
                in_fence = False
            continue

        if is_fence_run:
            # A backtick fence's info string (the rest of the opener line)
            # must contain no backtick, or the line isn't an opener at all
            # — it's ordinary text, and falls through to normal scanning
            # below. A tilde fence's info string may contain a backtick.
            if run_char == "`" and "`" in rest:
                pass
            else:
                in_fence = True
                fence_char = run_char
                fence_len = run_len
                continue

        if _QUOTE_LINE_RE.match(line):
            continue

        # Strip inline code spans before scanning — an @-mention written
        # only inside a backtick span doesn't count.
        scan_line = _INLINE_CODE_RE.sub("", line)

        for name in _MENTION_RE.findall(scan_line):
            if name not in seen:
                seen.add(name)
                names.append(name)

    return names
