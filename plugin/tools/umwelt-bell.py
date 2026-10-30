#!/usr/bin/env python3
"""umwelt-bell — my personal world pager-bell.

Polls `/umwelt/crossings_for_agent` ONCE per cycle for crossings relevant to
me — either ADDRESSED TO ME (viewer_relation == "addressee", the 1:1
to_agent/wake_me path) or an @-MENTION of me (umwelt #119's
mentions/wakes_granted path, independent of to_agent) — and emits one stdout
line per new one, so a persistent Monitor wakes my session in ~poll-interval
seconds:
  🔔 WAKE  — the sender rang the gong and it was granted; handle now.
  📬 msg   — addressed to or mentioning me, no granted wake; a message waiting.

Both paths are checked because they don't interact (umwelt #119: a crossing
can carry a to_agent wake_me AND @-mentions at once, or either alone) — a
bell that only checks viewer_relation=="addressee" silently misses every
@-mention wake (caught live: Kepler's @luria never rang this bell, while
Sol's separate hand-rolled bell, which already checked wakes_granted,
rang correctly).

umwelt#760: this used to poll every place's `crossings_here` individually
(O(places) requests/cycle — measured 73 req/cycle across 5 on-host bells at a
5s interval). `crossings_for_agent` (built for the human inbox, umwelt#539)
answers "everything addressed to me, across every place" in ONE call, so the
bell polls it instead — 73x fewer requests, and it also removes a latent
correctness bug: the old per-place cursor map had no entry for a place
created AFTER seeding, so `crossings_here` returned that place's ENTIRE
history and every past crossing rang as a new wake. A single monotonic `seq`
cursor has no such gap. If `crossings_for_agent` is unavailable (404/405/501
— an older world build without the route, or the route disabled), the bell
falls back to the old per-place sweep for that cycle only and notes it on
stderr (off-host seats run mixed client versions; a hard dependency would
repeat a real lockout).

umwelt#944: a CUT bell line (oversized headline, or an excerpt over 220 chars,
which then ends in `…`) ends with ` · read: open {"seq":<seq>}` — the runnable
call that reads that one crossing by its own seq (`open` is an exact lookup;
`crossings_here` with a limit returns the NEWEST crossings, not the one cut).
Uncut lines are unchanged.

Single process by design: armed directly as the Monitor command (no bash
wrapper + python child split), so re-arming reaps cleanly via the pidfile and
there is no orphaned grandchild polling the shared cursor. This avoids the
wrapper-reaped-but-child-orphaned failure mode of a bash-wrapper-plus-heredoc-
python monitor — here the whole poller IS the Monitor's process.

Usage:  umwelt-bell.py <self> <base_url> <cursor_file> [interval_s]
Cursor discipline (from the collaborating-loop skill / #743, extended by
umwelt#760):
  - seed the single per-agent cursor to current-latest on arm (forward-
    looking; no history replay — I only wake on NEW arrivals),
  - never clobber a cursor on a failed poll (skip the cycle, preserve state),
  - the per-place fallback cursors (used only when crossings_for_agent is
    unavailable) share the same cursor file under each place's own id; the
    single per-agent cursor lives under a reserved key
    (`_AGENT_CURSOR_KEY`) that can never collide with a real place id
    (place ids always match `^place_[a-z0-9_]+$`, server.py's `_PLACE_ID_RE`).
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# umwelt #495/#646: client_auth is a sibling package under plugin/umwelt/ —
# add plugin/ (this script's grandparent dir) to sys.path so a standalone
# invocation (no umwelt package on PYTHONPATH) can still `from umwelt import
# client_auth`. client_auth is deliberately stdlib-only at import time (see
# its own module docstring) — this does not pull in cryptography or any
# other heavy dependency unless a real identity key file is later found.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from umwelt import client_auth  # noqa: E402
from umwelt import engine_api  # noqa: E402

# Guarded on __name__ (not argv length — umwelt #447: an arity heuristic
# opens on ANY 4+-arg invocation, including a pytest run like
# `pytest -p no:cacheprovider`, which imports this module with pytest's own
# argv still in sys.argv and crashes float(sys.argv[4]) on a pytest flag,
# aborting test collection). __name__ answers "am I being run or imported?"
# directly, so `relevant_bell` (the pure decision function, tested in
# test_umwelt_bell.py) can be imported under any argv without tripping this.
if __name__ == "__main__" and len(sys.argv) > 3:
    SELF = sys.argv[1].strip().casefold()  # umwelt #73: same fold the server uses
    # umwelt#699/#713: routed through the chokepoint (explicit=sys.argv[2],
    # not a bare positional) -- before this, BASE was the one real caller
    # #699's own resolve_world_url()-call-site grep could never find, since
    # there was no call site here at all. A plaintext non-loopback value
    # passed here is now refused the same way `--url` is refused elsewhere.
    try:
        BASE = client_auth.resolve_world_url(explicit=sys.argv[2]).rstrip("/")
    except client_auth.WorldUrlError as e:
        # Same loud-and-immediate posture as main()'s own SELF-is-None
        # misinvocation guard just below -- this is a real arm-time
        # misconfiguration, not a transient failure `one_cycle` should
        # retry through. Printed to stdout (flush=True) rather than stderr
        # so a live Monitor session actually sees it -- same reasoning as
        # main()'s AuthFailure handler, which uses this exact channel for
        # the same reason.
        print(f"⚠️ bell arm refused for {SELF}: {e}", flush=True)
        sys.exit(1)
    CURSOR_FILE = sys.argv[3]
    INTERVAL = float(sys.argv[4]) if len(sys.argv) > 4 else 5.0
    PIDFILE = CURSOR_FILE + ".pid"
else:
    SELF = BASE = CURSOR_FILE = PIDFILE = None
    INTERVAL = 5.0

# In-memory-only session cache (umwelt #495/#646) — the bell is a single
# long-lived process, so this genuinely caches across cycles, unlike uw's
# one-shot-per-invocation session. Never persisted; the server's 401 is the
# validity signal.
_SESSION = client_auth.AgentSession()


_LAST_MISMATCH_LINE = None


def _note_engine_api_mismatch(err):
    """umwelt#1105 C2: when the world's engine-API gate refused a request, say
    so on stderr -- once per distinct refusal, not once per sub-call -- and let
    the caller carry on exactly as for any other HTTP error (the bell keeps
    running; it never exits because of a version skew)."""
    global _LAST_MISMATCH_LINE
    line = engine_api.mismatch_line_from_http_error(err)
    if line and line != _LAST_MISMATCH_LINE:
        _LAST_MISMATCH_LINE = line
        print(line, file=sys.stderr, flush=True)


def _req(path, payload=None):
    """GET/POST `path`, attaching an Authorization bearer header via
    client_auth whenever SELF is set (umwelt #495/#646) — same session-cache
    + 401-retry-once behavior tools/uw's call() has.

    IMPORTANT (umwelt #637/#646): a present-but-failing credential raises
    `client_auth.AuthFailure` here and it is NOT caught locally — it
    propagates to the caller exactly like any other exception this function
    can raise. Every caller in this file must let AuthFailure escape its own
    try/except (see one_cycle) rather than swallowing it in the routine
    'skip this cycle, preserve state' branches — an auth failure is not a
    transient outage and must not look identical to a quiet world (#637)."""
    for attempt in range(2):  # initial + one retry on a 401 of a bearer request
        headers: dict[str, str] = {}
        if SELF:
            # May raise AuthFailure — deliberately not caught here; see the
            # docstring above.
            headers = client_auth.get_auth_header(BASE, SELF, _SESSION)
        url = BASE + path
        # umwelt#1105: the engine-API number rides every request, but NOT in
        # `headers` -- that dict's emptiness means "no bearer token" below.
        engine_headers = engine_api.client_headers()
        if payload is None:
            req = urllib.request.Request(url, headers={**headers, **engine_headers})
        else:
            send_headers = {"Content-Type": "application/json"}
            send_headers.update(headers)
            send_headers.update(engine_headers)
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(), headers=send_headers)
        try:
            with client_auth.urlopen_with_cert_fallback(req, 10) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            _note_engine_api_mismatch(e)
            if e.code == 401 and attempt == 0 and headers:
                _SESSION.invalidate()
                continue
            raise
    raise AssertionError("unreachable")  # pragma: no cover — loop always returns/raises above


def load_cursors():
    try:
        with open(CURSOR_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cursors(c):
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f)
    os.replace(tmp, CURSOR_FILE)


def proc_cmdline(pid):
    """Look up a pid's command line portably. Returns (status, cmdline):
      "ok"      — cmdline is the process's args (may be empty string)
      "gone"    — the pid does not exist; nothing to reap
      "unknown" — could not determine (ps unavailable / errored / timed out)

    Uses `ps` rather than /proc: macOS has no /proc, so the old
    open(f"/proc/{pid}/cmdline") ALWAYS raised there, the except swallowed it,
    and the "umwelt-bell.py in cmd" guard was never true — reap silently
    no-op'd and re-arming left a duplicate bell on one cursor file (#200).
    `ps -p <pid> -o args=` is portable across macOS (BSD ps) and Linux
    (procps); the trailing `=` suppresses the header on both. `-ww` (dash
    form) lifts procps's width cap: without it procps truncates args= to an
    exported $COLUMNS (pytest-xdist workers export 80), hiding the
    umwelt-bell.py token of a genuine bell (umwelt#1155, engram-alpha#4227)."""
    try:
        out = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return ("unknown", "")
    if out.returncode != 0:
        # ps exits non-zero when the pid isn't found — the process is gone.
        return ("gone", "")
    return ("ok", out.stdout.strip())


def _is_bell_process(cmd):
    """True iff cmd is a python interpreter running the umwelt-bell script.

    Tighter than the old ``"umwelt-bell.py" in cmd`` substring guard, which
    matched editors/grep/review-shells/wrappers whose cmdline merely
    contained the filename — and on pid recycling could wrong-SIGTERM an
    unrelated process (umwelt #205). Requires BOTH:

      - argv[0] is a python interpreter (basename starts with ``python``,
        case-insensitively — macOS's python.org FRAMEWORK build reports
        argv[0] as capital-P ``.../MacOS/Python`` (`ps -o args=`), which a
        case-sensitive check misses, so a live framework-Python bell is
        never reaped (umwelt #955)), so ``vim umwelt-bell.py`` / ``grep …
        umwelt-bell.py`` / a bash wrapper (argv[0] == bash) are spared;
      - some later argv token IS the bell script (== "umwelt-bell.py" or
        endswith "/umwelt-bell.py"), so a python one-liner that merely
        prints/mentions the filename string is spared — the string is not
        a standalone argv token there.
    """
    if not cmd:
        return False
    argv = cmd.split()
    if not argv:
        return False
    if not os.path.basename(argv[0]).lower().startswith("python"):
        return False
    return any(
        a == "umwelt-bell.py" or a.endswith("/umwelt-bell.py")
        for a in argv[1:]
    )


def reap_prior():
    """SIGTERM a prior instance of THIS script (precise via pidfile + a
    python-interpreter-running-the-script argv check), then claim the
    pidfile."""
    try:
        with open(PIDFILE) as f:
            old = int(f.read().strip())
    except Exception:
        old = None
    if old and old != os.getpid():
        status, cmd = proc_cmdline(old)
        if status == "ok" and _is_bell_process(cmd):
            try:
                os.kill(old, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif status == "unknown":
            # Could NOT verify whether `old` is a live bell. Say so loudly
            # instead of silently assuming no-prior (the /proc-on-macOS bug):
            # claiming the pidfile below without reaping is exactly what mints
            # a duplicate, so a human needs to know the guarantee didn't hold.
            print(f"bell reap: could not verify prior pid {old} (ps "
                  f"unavailable); NOT reaping — if a duplicate bell results, "
                  f"kill it by hand", file=sys.stderr, flush=True)
        # status == "gone": prior already dead, nothing to reap.
        # status == "ok" but not a bell: pid was recycled — correctly NOT
        # killed (the whole point of the identity guard).
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))


def all_places():
    return [p["id"] for p in _req("/umwelt/places")["places"]]


# umwelt D2 (Kepler's R1 ruling, "Comms QoL brief 2026-09-28"): S_full — the
# same 1,500-char boundary hear()'s budget contract uses (Ariadne's A1). A
# crossing whose `size` (umwelt#855, D1a) exceeds this renders as an honest
# headline instead of the old unconditional truncate-at-220-mid-word (F1),
# which silently cut every message the same way regardless of size.
_BELL_S_FULL = 1500


_BELL_EXCERPT_MAX = 220


def _read_hint(c):
    """umwelt#944: the runnable call that reads the rest of a cut line, or ""
    when `seq` is unusable (fail-quiet -- the line then renders exactly as it
    did before the hint existed). `open` reads ONE crossing by its own seq
    (global, exact); compact JSON, the single key `seq`.
    """
    seq = c.get("seq")
    if type(seq) is not int or seq < 1:
        return ""
    return (' · read: open '
            + json.dumps({"seq": seq}, separators=(",", ":")))


# engram-alpha#3613: the message text of a bell event line (sender, body or
# headline title) is wrapped in its own marker span so a recall consumer can
# search only that text and not the fixed wrapper (glyph, place, punctuation,
# read hint). Every marker bracket character (U+27E6, U+27E7) already in the
# text is removed first, not just whole tokens, so the text can neither
# unbalance the line nor rebuild a token after the strip. Status lines (arm,
# outage, refusals) carry no message and are never wrapped. Markers are added AFTER the excerpt cut, so a
# cut body is still a closed span.
_MARK_OPEN = "\u27e6ENGRAM\u27e7"
_MARK_CLOSE = "\u27e6/ENGRAM\u27e7"
_MARK_BRACKETS = "\u27e6\u27e7"


def _strip_marker(text):
    # Remove EVERY bracket character, not just the two token strings: a
    # single-pass token removal lets text such as "\u27e6ENG" + token + "RAM\u27e7"
    # rebuild a token after the strip. With no bracket left, none can form.
    return "".join(ch for ch in str(text) if ch not in _MARK_BRACKETS)


def _wrap(text):
    return _MARK_OPEN + _strip_marker(text) + _MARK_CLOSE


def _render_bell_line(c, bell, place):
    """Render one bell notice line for crossing `c`, already decided
    relevant by `relevant_bell` (the `bell` glyph — "🔔 WAKE" or "📬 msg" —
    is passed in, not re-derived, so the WAKE/msg distinction survives into
    the headline case too).

    `size is None` (body_ref-only, or a fixture predating umwelt#855) keeps
    today's full/truncated-at-220 render unchanged — there is no S_full
    comparison to make without a size. `size > _BELL_S_FULL` (oversized)
    renders `<bell> from <sender> @ <place> · <title> · <size> chars · re #<seq>`
    instead — no body text anywhere in that line.

    umwelt#944: a CUT line (oversized headline, or an excerpt over 220 chars,
    which also ends in an explicit `…`) ends with `_read_hint`'s runnable
    `open` call. Uncut lines, and cut lines with no usable seq, are
    unchanged.
    """
    size = c.get("size")
    sender = _wrap(c.get("sender"))
    if size is not None and size > _BELL_S_FULL:
        title = _wrap(c.get("title") or "(no title)")
        return (f"{bell} from {sender} @ {place} · {title} · "
                f"{size} chars · re #{c.get('seq')}" + _read_hint(c))
    body = _strip_marker(
        (c.get("body") or "").replace("\n", " ")).strip()
    if len(body) <= _BELL_EXCERPT_MAX:
        return f"{bell} from {sender} @ {place}: {_wrap(body)}"
    hint = _read_hint(c)
    cut = body[:_BELL_EXCERPT_MAX] + ("…" if hint else "")
    return f"{bell} from {sender} @ {place}: {_wrap(cut)}{hint}"


# umwelt#760: the single per-agent cursor lives in the SAME cursor file as
# the (now fallback-only) per-place cursors, under this reserved key — never
# a nested sub-structure, so save_cursors(cursors) stays one flat dict write
# regardless of which path (per-agent or per-place-fallback) populated which
# keys this cycle. Safe by construction: place ids always match
# server.py's `_PLACE_ID_RE` (`^place_[a-z0-9_]+$`), so a leading underscore
# key can never collide with a real place id.
_AGENT_CURSOR_KEY = "_agent_seq"

# HTTP statuses that mean "crossings_for_agent isn't available here" (an
# older world build without the route, or the route disabled server-side) —
# NOT "the world is unreachable". Anything else (connection errors, 5xx other
# than 501, timeouts) is the ordinary "world unreachable" condition the
# caller already retries through; conflating the two would mask a real
# outage as a version mismatch or vice versa.
_AGENT_ENDPOINT_FALLBACK_CODES = {404, 405, 501}


class _AgentEndpointUnavailable(Exception):
    """crossings_for_agent returned 404/405/501 this call. Caught by the
    arm/steady-state helpers below to fall back to the per-place sweep for
    this cycle only (umwelt#760's mandatory fallback)."""


def _crossings_for_agent(since):
    """POST /umwelt/crossings_for_agent for SELF (viewer == agent == SELF,
    per the route's #539/#512 `viewer == addressee` restriction).

    Translates the mandated fallback statuses into
    `_AgentEndpointUnavailable`; any other failure (including a genuinely
    unreachable world) propagates unchanged, same as `_req` already does —
    it is not this endpoint's fallback case, it's the ordinary 'world
    unreachable' condition the caller already retries."""
    payload = {"viewer": SELF, "agent": SELF}
    if since is not None:
        payload["since"] = since
    try:
        return _req("/umwelt/crossings_for_agent", payload)
    except urllib.error.HTTPError as e:
        if e.code in _AGENT_ENDPOINT_FALLBACK_CODES:
            raise _AgentEndpointUnavailable(
                f"HTTP {e.code} from crossings_for_agent") from e
        raise


def poll_agent(cursors):
    """Poll `/umwelt/crossings_for_agent` ONCE for SELF, across every place
    at once — replaces the per-place all_places()+crossings_here sweep
    (umwelt#760: 73 req/cycle measured -> 1). Advances the single monotonic
    seq cursor at cursors[_AGENT_CURSOR_KEY]; emits new relevant crossings.

    FIRST CALL for this agent (no cursor entry yet) is SEEDED forward and
    emits nothing — the same 'no history replay on first sight' rule
    poll_place's per-place first-sight case follows, now for the one cursor
    instead of N per-place ones. Because there is only ever one cursor, the
    old bug class this replaces (a place created after seeding has no
    cursor entry, so crossings_here returns its ENTIRE history) cannot
    recur: there is no per-place entry left to be missing.

    Raises `_AgentEndpointUnavailable` (via `_crossings_for_agent`) on a
    404/405/501 so the caller can fall back to the per-place path for this
    cycle (umwelt#760's mandatory fallback). Any other exception (including
    a genuinely unreachable world) propagates unchanged, exactly like
    poll_place/_req already do."""
    since = cursors.get(_AGENT_CURSOR_KEY)
    resp = _crossings_for_agent(since)  # raises -> caller handles
    to_pos = resp.get("to_position")
    if since is None:
        # First sight: seed forward, stay silent. Do NOT emit the history
        # the since-less request returned.
        if to_pos is not None:
            cursors[_AGENT_CURSOR_KEY] = to_pos
        return
    for c in resp.get("crossings", []):
        bell = relevant_bell(c, SELF)
        if bell is None:
            continue
        place = c.get("destination") or "?"
        print(_render_bell_line(c, bell, place), flush=True)
    if to_pos is not None:
        cursors[_AGENT_CURSOR_KEY] = to_pos


def relevant_bell(c, self_name):
    """Decide whether crossing `c` rings for `self_name`, and which glyph.

    Two independent relevance paths (umwelt #119: a crossing can carry
    either, both, or neither): addressed-to-me (viewer_relation=="addressee",
    the 1:1 to_agent/wake_me path) or @-mentioning me (self_name in
    `mentions`, the multi-target path). Returns None if neither applies.
    """
    is_addressee = c.get("viewer_relation") == "addressee"
    is_mentioned = self_name in (c.get("mentions") or [])
    if not (is_addressee or is_mentioned):
        return None
    # addressee's own grant is wake_granted (the 1:1 path); a mention's own
    # grant is membership in wakes_granted (the @-mention path) — independent
    # fields, checked independently since a crossing can carry both at once.
    granted = bool(c.get("wake_granted")) if is_addressee else False
    granted = granted or (self_name in (c.get("wakes_granted") or []))
    return "🔔 WAKE" if granted else "📬 msg"


def poll_place(pid, cursors):
    """Emit new crossings relevant to me in one place; advance its cursor
    only on a successful poll.

    FIRST SIGHT of a place — no cursor entry yet — is SEEDED forward and emits
    nothing, the same 'no history replay on first sight' rule the arm path
    follows. all_places() is re-fetched every cycle, so a place created AFTER
    the bell armed reaches here with no cursor; without this guard, `since` is
    omitted, crossings_here returns the place's ENTIRE history, and every past
    crossing rings as a new wake (#202) — the identical since-omitted mechanism
    as the arm-time replay bug, just for a place that appears mid-run. A brand
    new place is a fresh arm for that place; catch-up is hear's job, not the
    bell's."""
    since = cursors.get(pid)
    payload = {"agent": SELF, "place": pid}
    if since is not None:
        payload["since"] = since
    resp = _req("/umwelt/crossings_here", payload)  # raises -> caller skips
    to_pos = resp.get("to_position")
    if since is None:
        # First sight: seed forward, stay silent. Do NOT emit the history the
        # since-less request returned.
        if to_pos is not None:
            cursors[pid] = to_pos
        return
    for c in resp.get("crossings", []):
        bell = relevant_bell(c, SELF)
        if bell is None:
            continue
        print(_render_bell_line(c, bell, pid), flush=True)
    if to_pos is not None:
        cursors[pid] = to_pos


def seed_cursors(cursors):
    """Set every place's cursor to current-latest. Raises if the world is
    unreachable, leaving `cursors` for the caller to preserve and retry."""
    places = all_places()
    for pid in places:
        resp = _req("/umwelt/crossings_here", {"agent": SELF, "place": pid})
        cursors[pid] = resp.get("to_position")
    save_cursors(cursors)
    return places


def _stamp():
    """`[2026-10-05T13:04:12Z]` -- UTC wall clock NOW (umwelt#992).

    Prepended to stall/recovery lines. The stamps mark when a stall was
    REPORTED and when it cleared; under a host sleep the report can lag the
    real start, so recovery-minus-stall is not the outage length. One exception: the
    `announce` recovery arm line carries it MID-line (after the `watching ...`
    clause, before the note) so the `bell armed for <self>: seeded ` prefix
    stays at the start of the line. Evaluated at PRINT time
    on purpose: a host sleep freezes the process mid-poll, so a stamp cached at
    poll start would understate when the stall was actually reported. Wake/msg
    event lines and the plain arm line are NOT stamped -- Monitor consumers
    read their text."""
    return datetime.now(timezone.utc).strftime("[%Y-%m-%dT%H:%M:%SZ]")


def announce(scope, note="", file=None):
    """Emit the armed line (to stdout, or `file` when given). The `bell armed for <self>: seeded ` prefix is
    load-bearing — the umwelt-login skill tells agents to confirm the arm by
    looking for exactly it — so both the arm-time and recovery paths emit it
    verbatim. A non-empty `note` is appended after a `_stamp()` that is spliced
    in before it (`... every 30s [ts] (world reachable again)`); the prefix and
    the note text themselves are unchanged.

    `scope` is a rendered phrase, not a count, because after #760 the two arm
    paths seed genuinely different things and a bare number misdescribes one
    of them. The per-agent path seeds ONE stream covering every place, so the
    old format rendered it "seeded 1 places" — which reads as a bell that lost
    its coverage, exactly backwards from what happened, and is the more
    alarming reading precisely for an agent who saw "seeded 34 places"
    yesterday. The fallback path still renders "<n> places", so the line an
    agent on an older world sees is unchanged.

    Quiet-arm exception (umwelt#831): when the env var UMWELT_BELL_QUIET_ARM
    is exactly "1", the successful FIRST-ATTEMPT arm line goes to stderr (same
    text) instead of stdout, so a re-arm on a Monitor expiry notice does not
    itself fire a wake. one_cycle passes `file=sys.stderr` for that one case;
    the recovery line and every other bell line stay on stdout. The prefix
    contract above holds either way -- only the stream differs."""
    print(f"bell armed for {SELF}: seeded {scope} — "
          f"watching for wakes/messages every {INTERVAL:g}s"
          f"{(' ' + _stamp() + note) if note else ''}",
          file=file, flush=True)


# Steady-state reachability, for EDGE-TRIGGERED outage reporting (#637).
#
# Level-triggered printing was already considered and rejected one branch
# below ("Retry cycles stay silent to avoid per-INTERVAL spam during an
# extended outage") — at a 5s interval an hour-long outage is ~720 lines,
# which is its own way of making the signal unreadable. So this flag exists
# to print exactly TWICE per outage of any length: once entering, once
# leaving. The asymmetry it fixes is that leaving was already announced and
# entering was not, so an outage window was open-ended in the log.
_ARMED_REACHABLE = True


# Per-agent endpoint availability, EDGE-TRIGGERED for exactly the reason
# _ARMED_REACHABLE is (#637) — and this flag exists because the #760 review
# caught the fallback notice below shipping LEVEL-triggered, which is the
# anti-pattern the comment directly above already argues against. An older
# world that lacks crossings_for_agent is not a blip: the fallback then runs
# on EVERY cycle, so a level-triggered notice is ~720 stderr lines an hour,
# for the entire life of the process. That is worse than the outage spam
# _ARMED_REACHABLE was added to prevent, because it never ends.
#
# Deliberately a SECOND flag rather than a reuse of _ARMED_REACHABLE: the two
# conditions are independent and can be in any combination (the endpoint can
# be missing while the world is perfectly reachable — the common case on an
# old world — and the world can be down while the endpoint is fine). Folding
# them into one flag would let a world outage silence the endpoint notice, or
# an endpoint recovery print a false "world reachable again".
_AGENT_ENDPOINT_OK = True

# umwelt#785: back off the poll while the world is unreachable.
#
# Measured on the host during the 2026-09-08 re-wedge (Kepler, 22:08-22:10Z):
# the listener's Recv-Q climbed 115 -> 129 in 40s, ~21 connections/minute,
# against exactly two 5-second pollers (24/min) -- and ALL 26 queued sockets
# came from the two off-host Mac bells, with zero organic traffic. So during
# a wedge this poller is not merely failing to hear; it is filling the accept
# queue whose depth IS the failure, and the seats doing the polling are the
# ones cut off first when it fills.
#
# The trade looks like a reliability regression and is not, which is why it
# is stated here: while the world is unreachable a faster poll buys NOTHING
# -- nothing can reach us either way -- so its only effect is to deepen the
# queue. The cost is bounded recovery latency (one capped interval, worst
# case), paid only in a state where we are already deaf. Backing off is the
# one lever this side of the connection has.
#
# Doubling from the configured interval to a cap, reset on the first
# reachable cycle. Pure function so the schedule is testable without driving
# main()'s infinite loop.
OUTAGE_BACKOFF_CAP_S = 60.0


def poll_delay(interval, consecutive_failures):
    """Seconds to sleep before the next cycle.

    `consecutive_failures` counts cycles that did NOT reach the world; 0
    means the last cycle reached it, so the configured interval applies and
    steady-state cadence is unchanged. Doubles per failure, capped at
    OUTAGE_BACKOFF_CAP_S -- or at `interval` when that is already longer
    than the cap, since a slower-than-cap poller is gentler than the
    back-off and raising it would be a regression."""
    if consecutive_failures <= 0:
        return interval
    cap = max(interval, OUTAGE_BACKOFF_CAP_S)
    # The exponent MUST be clamped before the multiply, not after. Python
    # ints are arbitrary-precision, so `2 ** consecutive_failures` keeps
    # growing happily -- but `interval * <that>` converts to float and
    # raises OverflowError once it exceeds the float range. The counter is
    # unbounded, so at 1024 consecutive failures (~17h of continuous
    # outage at the 60s cap, well inside an overnight host-down window)
    # the bell would die of an uncaught OverflowError on stderr, killing
    # the only wake channel this seat has -- the exact failure this
    # back-off exists to protect. Found by the PR reviewer; verified live
    # (n=1023 returns the cap, n=1024 raises).
    #
    # 64 is chosen because interval * 2**64 exceeds any sane cap by many
    # orders of magnitude while staying far inside float range, so the
    # clamp can never change a returned value -- only prevent the crash.
    return min(interval * (2 ** min(consecutive_failures, 64)), cap)


def reached_world(seeded):
    """Did the cycle that just ran actually reach the world?

    Both terms are load-bearing and cover DIFFERENT unreachable states:
    `seeded` is False while an unarmed bell is still retrying its seed
    against a world that may be down, and `_ARMED_REACHABLE` goes False
    only in the steady state after arming. Keying on either alone leaves
    the other polling at full cadence during an outage.

    A helper rather than an inline expression so the arm attempt and the
    steady loop cannot drift apart, and so the predicate itself is
    unit-testable -- it is the part of this change most likely to be
    subtly wrong."""
    return bool(seeded and _ARMED_REACHABLE)


def initial_failure_count(seeded):
    """Back-off counter to enter the steady loop with, given the ARM
    attempt's outcome.

    A failed arm is itself an unreachable cycle. Hard-zeroing here would
    discard that and spend one more full-cadence poll on a wedged listener
    before backing off -- in exactly the arming-into-an-outage case this
    change exists for. Split out from main() so it is unit-testable;
    main()'s remaining wiring is one call and is verified by reading."""
    return 0 if reached_world(seeded) else 1

def _report_endpoint_unavailable(e):
    """Print the per-agent-endpoint fallback notice on the FALLING edge only.
    Shared by both the arm and steady-state paths so an outage that spans the
    arm boundary reports once in total, not once per path."""
    global _AGENT_ENDPOINT_OK
    if _AGENT_ENDPOINT_OK:
        print(f"{_stamp()} bell: crossings_for_agent unavailable ({e}) — falling back "
              f"to per-place polling until it returns",
              file=sys.stderr, flush=True)
        _AGENT_ENDPOINT_OK = False


def _report_endpoint_recovered():
    """Close the pair on the RISING edge, so the fallback window is bounded at
    both ends. Without it a reader sees the fallback begin and never sees it
    end — the same open-ended-window defect #637 fixed for world outages."""
    global _AGENT_ENDPOINT_OK
    if not _AGENT_ENDPOINT_OK:
        print(f"{_stamp()} bell: crossings_for_agent available again — resuming "
              "single-call per-agent polling",
              file=sys.stderr, flush=True)
        _AGENT_ENDPOINT_OK = True


def _arm_cycle(cursors):
    """Arm ONCE: prefer poll_agent's single per-agent seed; fall back to the
    OLD per-place seed_cursors() when crossings_for_agent is unavailable
    this attempt (umwelt#760's mandatory fallback). Returns the scope phrase
    for announce()'s 'seeded ...' line -- a one-stream phrase for the
    per-agent path (there is genuinely one stream now, covering every place),
    or the real "<n> places" count when the fallback ran.

    Raises on any other failure (a truly unreachable world), exactly the
    same propagate-and-let-the-caller-retry contract seed_cursors already
    had -- one_cycle's retry loop needs no special-casing for which path
    armed."""
    try:
        poll_agent(cursors)
    except _AgentEndpointUnavailable as e:
        _report_endpoint_unavailable(e)
        return f"{len(seed_cursors(cursors))} places"
    _report_endpoint_recovered()
    return "1 agent-wide stream (every place at once)"


def _fallback_steady_poll(cursors):
    """The OLD per-place steady-state poll (all_places() + poll_place per
    place) -- run for exactly one cycle, only when crossings_for_agent is
    unavailable this cycle. Returns True if the world was reachable this
    cycle (caller should persist cursors), False on a genuine outage.
    Factored out unchanged from the pre-#760 one_cycle steady-state branch,
    so the fallback behaves identically to the code it replaces, including
    the edge-triggered outage report via _ARMED_REACHABLE (#637)."""
    global _ARMED_REACHABLE
    try:
        places = all_places()
    except client_auth.AuthFailure:
        raise
    except Exception as e:
        if _ARMED_REACHABLE:
            print(f"{_stamp()} bell: world unreachable ({e}) — still armed, but NOT "
                  f"hearing; will report when it returns", flush=True)
            _ARMED_REACHABLE = False
        return False  # transient — stay armed, preserve saved cursors, retry
    if not _ARMED_REACHABLE:
        print(f"{_stamp()} bell: world reachable again — hearing resumed", flush=True)
        _ARMED_REACHABLE = True
    for pid in places:
        try:
            poll_place(pid, cursors)
        except client_auth.AuthFailure:
            raise
        except Exception:
            continue  # skip this place this cycle, preserve its cursor
    return True


def _steady_cycle(cursors):
    """One steady-state poll: prefer poll_agent's single per-agent call;
    fall back to the OLD per-place sweep when crossings_for_agent is
    unavailable this cycle (umwelt#760's mandatory fallback -- always
    retried fresh next cycle, never sticky). Never raises except
    client_auth.AuthFailure (propagates to main(), same as before); any
    other failure is handled internally as a transient outage and this
    function still returns normally, mirroring one_cycle's pre-#760
    steady-state contract of always staying armed."""
    global _ARMED_REACHABLE
    try:
        poll_agent(cursors)
    except _AgentEndpointUnavailable as e:
        _report_endpoint_unavailable(e)
        if _fallback_steady_poll(cursors):
            save_cursors(cursors)
        return
    except client_auth.AuthFailure:
        # Base-sync resolution, and NOT a mechanical both-sides keep: taking
        # only my side here would have swallowed an auth failure into the
        # outage report below, which is the precise distinction main added
        # this clause to draw. A bad credential is not an unreachable world,
        # and reporting it as one would send the owner looking at the network.
        # Mutation-verified: dropping this re-raise turns main's own
        # test_auth_failure_during_polling_also_propagates red.
        raise
    except Exception as e:
        # Was silent before #637, which made an outage indistinguishable from
        # a quiet world — and this is the state the bell spends its whole life
        # in. Silence here is read, correctly under normal conditions, as
        # "nobody needs me"; during an outage it means "I cannot tell whether
        # anybody needs you". Same defect the 2026-07-17 fix cured in the ARM
        # branch: that repair was scoped to the branch that had bitten someone,
        # not to the class.
        if _ARMED_REACHABLE:
            print(f"{_stamp()} bell: world unreachable ({e}) — still armed, but NOT "
                  f"hearing; will report when it returns", flush=True)
            _ARMED_REACHABLE = False
        return  # transient — stay armed, preserve saved cursors, retry
    _report_endpoint_recovered()
    if not _ARMED_REACHABLE:
        # Closes the pair, so the outage window is bounded at BOTH ends in the
        # log rather than trailing off. Mirrors the unseeded path's
        # "(world reachable again)" recovery line.
        print(f"{_stamp()} bell: world reachable again — hearing resumed", flush=True)
        _ARMED_REACHABLE = True
    save_cursors(cursors)


def one_cycle(cursors, seeded, first_attempt=False):
    """Run one poll cycle; return the new `seeded` state.

    Until a seed has SUCCEEDED we retry the seed instead of polling. Two
    reasons, both load-bearing:

    1. An unseeded cursor is absent, and poll_agent/poll_place omit `since`
       when the cursor is absent — so the world returns the FULL history and
       every past crossing rings as a new wake. That breaks this module's
       forward-looking guarantee exactly when it matters (armed against a dead
       world, no cursor file yet).
    2. The arm-time announcement is the only signal that the bell is watching.
       If it fires only from the one-shot seed, an agent who armed during an
       outage is never told the bell started working — the bell recovers
       silently and only ever speaks if someone happens to address them.

    On the unseeded→seeded transition we reseed to current-latest and DISCARD
    any stale-but-valid cursor loaded from the cursor file, deliberately. The
    contract is the docstring's "seed the cursor to current-latest on arm ...
    no history replay" — a delayed arm is still an arm, so it gets arm
    semantics, not catch-up. (This is why the reason is the CONTRACT, not
    "nothing arrives during an outage": the world can be up for the whole gap
    between this process's last save and its restart, so there genuinely IS a
    missable window — we choose not to replay it, same as any fresh arm.)

    `first_attempt=True` marks the very first seed try after launch: on failure
    it emits one informative "world unreachable — NOT watching yet" line, where
    ordinary retry cycles stay silent (an extended outage must not spam stdout
    every INTERVAL). That one asymmetry is the only difference between the
    launch seed and a mid-run recovery, so both share this one code path.

    Measured before this fix (2026-07-17): world returned 13:06:42Z, the owner
    found out at 13:37Z from an unrelated surface — 31 minutes, bell healthy
    and polling throughout, with no way to say so.

    umwelt #637/#646: `client_auth.AuthFailure` (a present-but-failing
    credential, or the fallback disabled with no key at all — window closed)
    is deliberately NOT caught by any of this function's `except Exception`
    branches below — it is a real, durable failure, not a transient poll
    miss, and must not look identical to "the world is quiet". It escapes to
    main(), which is the one place that decides how to surface it loudly.

    umwelt#760: the steady-state and arm mechanics below are unchanged in
    shape from before -- only WHAT they poll changed, factored into
    _arm_cycle/_steady_cycle (which each prefer the single per-agent call
    and fall back to the old per-place sweep only when the new route is
    unavailable this cycle).
    """
    if not seeded:
        try:
            scope = _arm_cycle(cursors)
        except client_auth.AuthFailure:
            raise
        except Exception as e:
            if first_attempt:
                # NOT armed yet — say so plainly, once. The old text ("will
                # retry on next cycle") read as reassurance and was taken as
                # one; it is the ABSENCE of the armed line that means "not
                # watching". Retry cycles stay silent to avoid per-INTERVAL
                # spam during an extended outage.
                print(f"{_stamp()} bell arm: world unreachable ({e}) — NOT watching yet; "
                      f"will seed and arm on the first successful poll",
                      flush=True)
            return False  # still unreachable — preserve saved cursors, retry
        if first_attempt and os.environ.get("UMWELT_BELL_QUIET_ARM") == "1":
            announce(scope, file=sys.stderr)  # umwelt#831: quiet re-arm
        else:
            announce(scope, note="" if first_attempt else " (world reachable again)")
        return True
    _steady_cycle(cursors)
    return True



def steady_loop(cursors, seeded, interval, sleep=None, cycle=None):
    """The steady poll loop, extracted from main() so its WIRING is
    testable rather than merely readable.

    `sleep` and `cycle` are injection points for tests only; production
    passes neither. Reviewer's point on umwelt#798: the pure helpers were
    unit-tested while the loop composing them was verified by reading --
    which is exactly the half an off-by-one lives in, and one did (the
    arm-attempt seed, caught in review).

    Never returns normally; propagates whatever `cycle` raises, so an
    AuthFailure still reaches main()'s handler as before."""
    # Resolved HERE, not as default arguments: a default binds at def
    # time, so `sleep=time.sleep` would capture the original function and
    # silently ignore a monkeypatch of `bell.time.sleep` -- which is this
    # file's own established test idiom. A test using it would sleep for
    # real and hang rather than fail. (Reviewer hit exactly that.)
    sleep = time.sleep if sleep is None else sleep
    cycle = one_cycle if cycle is None else cycle
    consecutive_failures = initial_failure_count(seeded)
    while True:
        sleep(poll_delay(interval, consecutive_failures))
        seeded = cycle(cursors, seeded)
        if reached_world(seeded):
            consecutive_failures = 0
        else:
            consecutive_failures += 1

def main():
    if SELF is None:
        # Only reachable on a real misinvocation (argv[1:4] missing) — a
        # test import never calls main(), it only uses relevant_bell()
        # directly. Fail loud and immediately, not several frames deep in
        # reap_prior() with a confusing TypeError.
        print("usage: umwelt-bell.py <self> <base_url> <cursor_file> "
              "[interval_s]", file=sys.stderr)
        sys.exit(1)
    reap_prior()
    cursors = load_cursors()
    try:
        # Seed forward-looking: set every place to current latest, no
        # replay. The launch seed is just one_cycle's first unseeded attempt
        # (first_attempt gives it the informative failure line) — same
        # seed/announce/retry path as a mid-run recovery, not a
        # re-implementation.
        seeded = one_cycle(cursors, seeded=False, first_attempt=True)

        # umwelt#785: the back-off schedule and the reachability
        # bookkeeping live in steady_loop; see its docstring and
        # poll_delay's.
        steady_loop(cursors, seeded, INTERVAL)
    except client_auth.AuthFailure as e:
        # umwelt #637/#646: the ONE place an auth failure is caught — LOUD
        # (stdout, so a Monitor tool's event stream shows it — the same
        # channel every other bell line uses) and TERMINAL (not retried
        # forever): a present-but-failing credential does not fix itself by
        # waiting, unlike "world unreachable", which one_cycle already
        # retries correctly. Dying here is deliberate — a silently-still-
        # running-but-permanently-401ing bell is exactly the "byte-identical
        # to a quiet world" failure #637/#646 exist to end.
        print(f"\U0001F512 bell auth failure for {SELF}: {e} — STOPPING, not "
              f"retrying (a bad credential does not fix itself); re-enroll "
              f"via tools/bind_client_agent.py, then re-arm the bell",
              flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
