#!/usr/bin/env python3
"""decisions-board-staleness -- the day-end Decisions Board staleness print.

Contract: https://github.com/engram-agents/umwelt/issues/1139

Mechanises Practices B9 (Lei's 2026-10-26 evening convention): the day-end
prints the Decisions Board's rows that are unconfirmed for N days (default 3)
and rows whose referenced issue or PR has closed, so each can be replaced or
retired that night. Until now the commander ran this print by hand.

Input: a charge-board-shaped thing whose `metadata.entries[]` are
`{id, text, created_at, created_by, retired_at}`, read through the repo's own
CLI (`tools/uw --json look_at <thing-id>`). The thing id is a required
argument; no world id is baked into this tool. The CLI needs UMWELT_AGENT in
the environment: this tool passes its environment through and does not set
it, so a missing-identity refusal is just another fetch failure (exit 3).

All decision logic lives in one pure function, `judge()`. External calls (the
`uw` board read, the `gh` ref-state reads) are isolated in a thin fetch layer
whose every function takes an injectable `runner` callable, so tests need no
network and no subprocess. No stored constants that can go stale: the repo-word
map is the only table, and it maps names, not states or dates.

A CLOSED-REF line is a prompt to RE-CONFIRM the ask: drop or mark the closed
ref in the row, or retire the row if the ask is in fact done. The tool never
decides which; a closed ref does not by itself mean the decision is done.

Repo words: a bare `#N` inherits the most recent recognised repo word earlier
in the SAME row (`umwelt #1112, #1105` -> both umwelt); with none earlier it
takes the repo word of the row's head (the text before the first ` · `,
matched case-insensitively: `UMWELT WORLD LANE`), else defaults to
engram-alpha. The body scan stays case-sensitive, but a repo word written
directly before `#N` is honoured in any case: `Soma lane · Umwelt #1112` is
umwelt#1112, beating the head seed, and it also becomes the sticky repo for
the bare refs after it (`see Umwelt #7 and #8` -> both umwelt). A bare `#N` directly preceded by a word
that is neither a repo word nor a known connector (`website #25`, `Website #25`;
the word is lowercased before both lookups) is UNPARSED, never resolved against
a guessed repo.

Dates: `today` defaults to the UTC date. The commander's day-end runs in the
New York evening, which is already the next UTC day after 8 PM New York time,
so a day-end run passes `--today` as the New York calendar date. The
`Confirmed YYYY-MM-DD` tokens are written in New York calendar dates. A
malformed token (`Confirmed 2026-13-45`) is skipped for the confirmation date
and reported as UNPARSED with the literal token.

Summary counts: `active (N header)` is the board population; `stale` and `closed-ref` count ROWS flagged (the CLOSED-REF
lines still list every closed ref); `unparsed` and `unreadable` count tokens.
Identical unparsed tokens within one row are listed and counted once (as refs
are), so `website #25` cited twice prints once and counts as 1.

Exit codes: 0 clean; 1 when any row is STALE or has a CLOSED ref; 2 usage;
3 the board fetch failed (reason on stderr, nothing on stdout; a runner
timeout of 60 s counts as a failure, and for a ref it is UNREADABLE). UNPARSED and
UNREADABLE print but do not fail: a run where every `gh` call fails prints
`N unreadable` and exits 0 by contract, so a script consumer must read the
summary line (or `--json`), not only the exit code.

BOUND: a ref this tool cannot read is UNREADABLE, never assumed open; a
`#`-shaped token it cannot parse is UNPARSED, never dropped. It cannot see
refs written in a form the patterns below do not know (e.g. a full URL).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

OWNER = "engram-agents"
HEADER_PREFIX = "HOW THIS BOARD WORKS"
DEFAULT_DAYS = 3

# Repo word -> repository name under OWNER.
REPO_MAP = {
    "umwelt": "umwelt",
    "soma": "soma",
    "tls-front": "tls-front",
    "local-model-lab": "local-model-lab",
    "lab": "local-model-lab",
    "engram-paper-public": "engram-paper-public",
    "paper-public": "engram-paper-public",
    "apprentice-study": "apprentice-study",
    "engram-alpha": "engram-alpha",
    "alpha": "engram-alpha",
}
# Longest words first so `engram-paper-public` wins over `paper-public`, etc.
_WORDS = "|".join(sorted((re.escape(w) for w in REPO_MAP), key=len, reverse=True))
_WB = r"(?<![\w-])"

# (a) `<repo-word> #N`, with (c) an optional `/#M/#K` tail (`#8/#10/#11`).
_RE_WORD_HASH = re.compile(
    _WB + r"(?P<word>" + _WORDS + r")\s*#(?P<n>\d+)(?P<tail>(?:/#?\d+)*)(?!\w)")
# (b) `<repo-word> PRs 33/34/35`.
_RE_WORD_PRS = re.compile(
    _WB + r"(?P<word>" + _WORDS + r")\s+PRs?\s+(?P<list>\d+(?:/\d+)+)(?!\w)")
# (d) bare `#N` -> the sticky repo of its row, else engram-alpha.
_RE_BARE = re.compile(r"(?<![\w/&#])#(?P<n>\d+)(?!\w)")
# Every recognised repo word, for the sticky lookup and the unknown-word guard.
_RE_ANY_WORD = re.compile(_WB + r"(?P<word>" + _WORDS + r")(?![\w-])")
# Same, case-insensitive: only for seeding from a row's head (`UMWELT WORLD LANE`).
_RE_HEAD_WORD = re.compile(
    _WB + r"(?P<word>" + _WORDS + r")(?![\w-])", re.IGNORECASE)
_HEAD_SEP = " · "
# The word (any case) sitting immediately (one space) before a `#N`.
_RE_WORD_BEFORE = re.compile(r"(?<![\w-])(?P<w>[A-Za-z][A-Za-z-]*) $")
# Common English connectors that may precede a bare `#N` without being a repo
# guess. One place; extend here.
CONNECTOR_WORDS = frozenset({
    "and", "or", "of", "in", "on", "at", "to", "as", "see", "ref", "refs",
    "issue", "issues", "pr", "prs", "alpha", "closes", "part", "the", "a",
    "an", "with", "after", "per", "via", "by", "under", "from", "for", "since",
    "before", "until", "than", "into", "over", "about", "against",
})
# (e) whatever `#`-adjacent token is left.
_RE_LEFTOVER = re.compile(r"#[^\s\]\[(){}<>,;:.!?\"'`]+")
_RE_CONFIRMED = re.compile(r"Confirmed (\d{4}-\d{2}-\d{2})")
RUNNER_TIMEOUT = 60
_NEEDS = " · NEEDS FROM LEI"


def _full_repo(word: str) -> str:
    return f"{OWNER}/{REPO_MAP[word]}"


def _consume(text: str, rx: re.Pattern, handler) -> str:
    """Run `handler(match)` on each match and blank it out so one token is
    parsed once."""
    def sub(m):
        handler(m)
        return " " * (m.end() - m.start())
    return rx.sub(sub, text)


def parse_refs(text: str) -> tuple[list[tuple[str, int]], list[str]]:
    """Return ([(owner/repo, number)...] in order of appearance per pass, [unparsed])."""
    refs: list[tuple[str, int]] = []

    def word_hash(m):
        repo = _full_repo(m["word"])
        refs.append((repo, int(m["n"])))
        for k in re.findall(r"\d+", m["tail"]):
            refs.append((repo, int(k)))

    def word_prs(m):
        repo = _full_repo(m["word"])
        for k in m["list"].split("/"):
            refs.append((repo, int(k)))

    t = _consume(text, _RE_WORD_PRS, word_prs)
    t = _consume(t, _RE_WORD_HASH, word_hash)
    unparsed: list[str] = []
    # _consume blanks matches in place (length preserved), so offsets in `t`
    # are offsets in `text`: repo words and the word-before check read `text`.
    words = [(m.start(), m["word"]) for m in _RE_ANY_WORD.finditer(text)]

    # Head seed: the text before the first ` · ` names the row's lane/project.
    # Its first recognised repo word (any case) is the sticky repo until a
    # body repo word (case-sensitive scan) overrides it. No separator, or no
    # repo word in the head: seeds nothing.
    seed = None
    if _HEAD_SEP in text:
        hm = _RE_HEAD_WORD.search(text[:text.index(_HEAD_SEP)])
        if hm:
            seed = hm["word"].lower()

    adjacent: list[tuple[int, str]] = []

    def bare(m):
        pre = _RE_WORD_BEFORE.search(text[:m.start()])
        pw = pre["w"].lower() if pre else None
        if pre and pw not in REPO_MAP and pw not in CONNECTOR_WORDS:
            unparsed.append(f"{pw} #{m['n']}")
            return
        if pw in REPO_MAP:
            # An adjacent repo word, in any case, decides this ref and becomes
            # the sticky repo for the refs after it.
            word = pw
            adjacent.append((m.start(), pw))
        else:
            prior = sorted([(pos, w) for pos, w in words if pos < m.start()]
                           + [(pos, w) for pos, w in adjacent if pos < m.start()])
            word = prior[-1][1] if prior else seed
        repo = _full_repo(word) if word else f"{OWNER}/engram-alpha"
        refs.append((repo, int(m["n"])))

    t = _consume(t, _RE_BARE, bare)
    unparsed.extend(_RE_LEFTOVER.findall(t))
    return refs, unparsed


def _head(text: str) -> str:
    one = " ".join(text.split())
    cut = one.find(_NEEDS)
    if cut != -1:
        one = one[:cut]
    return one[:80].rstrip()


def _created_date(created_at: str) -> date:
    return date.fromisoformat(created_at[:10])


def judge(entries: list[dict], today: date, ref_state, days: int = DEFAULT_DAYS) -> dict:
    """Every decision. `ref_state(owner_repo, number)` returns
    {"state": "open"|"closed", "closed_at": str|None} or {"error": str}; it is
    called at most once per (repo, number) per judge() call."""
    cache: dict[tuple[str, int], dict] = {}

    def lookup(repo: str, n: int) -> dict:
        key = (repo, n)
        if key not in cache:
            cache[key] = ref_state(repo, n)
        return cache[key]

    headers = 0
    rows = []
    for e in entries:
        if e.get("retired_at"):
            continue
        text = e["text"]
        if text.startswith(HEADER_PREFIX):
            headers += 1
            continue
        last = _created_date(e["created_at"])
        bad_tokens = []
        for cm in _RE_CONFIRMED.finditer(text):
            try:
                last = max(last, date.fromisoformat(cm[1]))
            except ValueError:
                bad_tokens.append(cm[0])
        stale_days = (today - last).days
        flags = []
        if stale_days >= days:
            flags.append("STALE")

        parsed, unparsed = parse_refs(text)
        unparsed = list(dict.fromkeys(bad_tokens + unparsed))
        refs = []
        seen = set()
        for repo, n in parsed:
            if (repo, n) in seen:
                continue
            seen.add((repo, n))
            st = lookup(repo, n)
            if "error" in st:
                refs.append({"repo": repo, "number": n, "state": None,
                             "closed_at": None, "error": str(st["error"])})
            else:
                refs.append({"repo": repo, "number": n, "state": st.get("state"),
                             "closed_at": st.get("closed_at")})
        if any(r["state"] == "closed" for r in refs):
            flags.append("CLOSED-REF")
        if any("error" in r for r in refs):
            flags.append("UNREADABLE")
        if unparsed:
            flags.append("UNPARSED")
        rows.append({
            "entry_id": e["id"], "head": _head(text),
            "last_confirmed": last.isoformat(), "stale_days": stale_days,
            "refs": refs, "unparsed": unparsed, "flags": flags,
        })
    return {"active": len(rows), "headers": headers, "rows": rows}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def counts(result: dict) -> dict:
    rows = result["rows"]
    return {
        "stale": sum(1 for r in rows if "STALE" in r["flags"]),
        "closed_ref": sum(1 for r in rows if "CLOSED-REF" in r["flags"]),
        "unparsed": sum(len(r["unparsed"]) for r in rows),
        "unreadable": sum(1 for r in rows for x in r["refs"] if "error" in x),
    }


def format_lines(result: dict) -> list[str]:
    out = []
    for r in result["rows"]:
        tail = f"{r['head']}  (entry {r['entry_id']})"
        if "STALE" in r["flags"]:
            out.append(f"STALE       {r['stale_days']} days  {tail}")
        for x in r["refs"]:
            ref = f"{x['repo']}#{x['number']}"
            if x["state"] == "closed":
                when = (x["closed_at"] or "unknown")[:10]
                out.append(f"CLOSED-REF  {ref} closed {when}  {tail}")
            elif "error" in x:
                reason = " ".join(x["error"].split())[:60]
                out.append(f"UNREADABLE  {ref} {reason}  {tail}")
        for tok in r["unparsed"]:
            out.append(f"UNPARSED    {tok}  {tail}")
    return out


def summary_line(result: dict) -> str:
    c = counts(result)
    return (f"decisions-board: {result['active']} active ({result['headers']} header) · "
            f"{c['stale']} stale · "
            f"{c['closed_ref']} closed-ref · {c['unparsed']} unparsed · "
            f"{c['unreadable']} unreadable")


# ---------------------------------------------------------------------------
# Fetch layer -- every external call behind an injectable runner
# ---------------------------------------------------------------------------

def _run_uw(args: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(args, capture_output=True, text=True,
                       timeout=RUNNER_TIMEOUT)
    return p.returncode, p.stdout, p.stderr


def _run_gh(args: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(["gh"] + args, capture_output=True, text=True,
                       timeout=RUNNER_TIMEOUT)
    return p.returncode, p.stdout, p.stderr


def fetch_board(thing_id: str, runner=_run_uw) -> tuple[list[dict] | None, str | None]:
    """(entries, None) on success; (None, reason) on ANY failure."""
    uw = str(Path(__file__).resolve().parent / "uw")
    try:
        rc, out, err = runner([sys.executable, uw, "--json", "look_at", thing_id])
    except Exception as e:  # runner raising is a fetch failure, not a crash
        return None, f"runner raised {type(e).__name__}: {e}"
    if rc != 0:
        head = " ".join((err or out or "").split())[:200]
        return None, f"uw exited {rc}: {head}"
    try:
        entries = json.loads(out)["thing"]["metadata"]["entries"]
    except (ValueError, KeyError, TypeError) as e:
        return None, f"unexpected uw output ({type(e).__name__}: {e})"
    if not isinstance(entries, list):
        return None, "metadata.entries is not a list"
    for i, e in enumerate(entries):
        ok = (isinstance(e, dict) and isinstance(e.get("id"), str)
              and isinstance(e.get("text"), str) and isinstance(e.get("created_at"), str))
        if ok:
            try:
                _created_date(e["created_at"])
            except ValueError:
                ok = False
        if not ok:
            return None, f"entry {i} lacks id/text/created_at or has a bad date"
    return entries, None


def make_ref_state(runner=_run_gh):
    """Production `ref_state` over `gh api` (the issues endpoint answers for
    PRs too). Any failure is {"error": ...} -- never treated as open."""
    def ref_state(owner_repo: str, number: int) -> dict:
        try:
            rc, out, err = runner([
                "api", f"repos/{owner_repo}/issues/{number}",
                "--jq", "{state: .state, closed_at: .closed_at}"])
        except Exception as e:
            return {"error": f"runner raised {type(e).__name__}: {e}"}
        if rc != 0:
            return {"error": f"rc={rc} {' '.join((err or '').split())[:50]}".strip()}
        try:
            d = json.loads(out)
            state = d["state"]
        except (ValueError, KeyError, TypeError):
            return {"error": "unparseable gh output"}
        if state not in ("open", "closed"):
            return {"error": f"unexpected state {state!r}"}
        return {"state": state, "closed_at": d.get("closed_at")}
    return ref_state


# ---------------------------------------------------------------------------

EPILOG = (
    "A CLOSED-REF line is a prompt to RE-CONFIRM the ask: drop or mark the "
    "closed ref in the row, or retire the row if the ask is in fact done. "
    "The tool never decides which.")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="decisions_board_staleness", description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--thing", required=True, help="the board thing id (required)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"staleness threshold in days (default {DEFAULT_DAYS}; must be >= 1)")
    ap.add_argument("--json", action="store_true", help="dump the judged rows as one JSON document on stdout; the "
                    "summary line goes to stderr in this mode")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (default: UTC today; a New York evening day-end run "
                    "passes the New York calendar date)")
    return ap


def main(argv: list[str] | None = None, board_runner=_run_uw, gh_runner=_run_gh) -> int:
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    if args.days < 1:
        print("decisions-board: --days must be >= 1", file=sys.stderr)
        return 2
    try:
        today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
    except ValueError:
        print("decisions-board: --today must be YYYY-MM-DD", file=sys.stderr)
        return 2

    entries, reason = fetch_board(args.thing, runner=board_runner)
    if entries is None:
        print(f"decisions-board: fetch failed: {reason}", file=sys.stderr)
        return 3

    result = judge(entries, today, make_ref_state(gh_runner), days=args.days)
    if args.json:
        print(json.dumps(result, indent=2))
        print(summary_line(result), file=sys.stderr)
    else:
        for line in format_lines(result):
            print(line)
        print(summary_line(result))
    c = counts(result)
    return 1 if (c["stale"] or c["closed_ref"]) else 0


if __name__ == "__main__":
    sys.exit(main())
