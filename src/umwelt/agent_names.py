"""Reserved agent-name rule (umwelt#1043, slice 2).

A LEAF module (stdlib-only). Agent names starting with `world:` are RESERVED
for the authors a world package gives its things (`world:<package name>`), so
no resident can ever claim to be the world. One rule, one place: the birth
doors (`/umwelt/enter`, `/umwelt/register`, through their shared refusal
helper) and the `package` caller class both read it.

Kept out of `_ids.py` on purpose: that file is vendored byte-for-byte into the
plugin (test_plugin_sync), and this rule is server-side only.
"""

from __future__ import annotations

import unicodedata

RESERVED_AGENT_PREFIX = "world:"


def has_reserved_agent_prefix(raw: str | None) -> bool:
    """True iff `raw`, NFKC-normalised and casefolded, starts with
    `RESERVED_AGENT_PREFIX`. NFKC first so a fullwidth colon (U+FF1A) or
    fullwidth letters fold to the ASCII spelling; casefold after it, then a
    final NFKC pass because casefolding can itself produce compatibility
    characters. A name that merely resembles the prefix without the colon
    (`world`, `worldly`) is not reserved."""
    folded = unicodedata.normalize("NFKC", (raw or "").strip())
    folded = unicodedata.normalize("NFKC", folded.casefold()).casefold()
    return folded.startswith(RESERVED_AGENT_PREFIX)
