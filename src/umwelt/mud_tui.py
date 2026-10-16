"""Umwelt MUD TUI mode — `python -m umwelt.mud --tui`.

A curses-based sticky-pane view over the SAME world API and the SAME
`Repl`/`WorldClient` the line REPL (`umwelt.mud`) uses — this is a THIRD
renderer over one world API, not a parallel implementation (per Lei's
"one world API, multiple renderers" design note, umwelt #54, and the
layout sketch in umwelt #74 — Lei's live first-session request).

Layout — v2 (umwelt #129 slice A, Lei's redesign constitution: "focus
window = the immediate result of the LAST ACTION; sticky windows = ambient
environment info"):

    ┌─ <place label> ─────────────────────────────────┐
    │ <wrapped description, truncated with …>          │  place pane
    ├─ here: name · name · you ────────────────────────┤  presence strip
    │ things              │ FOCUS                      │
    │ 1 <name>            │ (last dispatched action's   │  things | FOCUS
    │ 2 <name>            │  output renders here)        │
    ├──────────────────────────────────────────────────┤
    │ [l]ook [g]o [x]amine [u]se [s]ay [r]ead [f]orge   │  command bar
    │ [h]istory [q]uit                                  │
    │ > _                                               │  input line
    └───────────────────────────────────────────────────┘

    [h]/[h]istory opens a FULL-SCREEN overlay over the whole layout above —
    the old always-on event feed (crossings + every action's output,
    accumulated) — closed by [esc]. FOCUS shows the output of every
    dispatched action, unconditionally — no exclusions, even when
    redundant with a sticky pane (Lei's final refinement mid-slice: the
    earlier allowlist and then denylist both hid useful output, e.g. bare
    `go`'s places-list, from FOCUS); there is no pin mode (v0
    constitution — see #129).

Design:
  - layout_regions / render_*  — PURE functions (dict/str in, list[str]/str
    out; no curses, no I/O). This is where all layout and wrapping logic
    lives, and it's what the test suite exercises directly (no tty needed).
  - The curses loop itself is kept thin: it calls the pure functions to
    decide what characters go where, and does nothing else layout-wise.
  - Unicode-tolerant: wrapping/truncation here is a naive `len()`-based
    fallback, not a true wcwidth computation — wide (e.g. CJK) characters
    may occupy more terminal cells than `len()` counts, so panes may run
    slightly ragged with such content. Documented limitation, not a crash
    risk (curses writes are wrapped in a safe try/except regardless).
  - Clean teardown: `launch()` runs the whole session inside
    `curses.wrapper`, which guarantees `endwin()` runs even on exception,
    Ctrl-C, or `q` — the terminal is never left in raw/cbreak state.

Env:
  UMWELT_URL   world server base url (default: see client_auth.resolve_world_url) —
               same as umwelt.mud; the TUI takes an already-constructed
               WorldClient from mud.py, it doesn't read the env itself.
"""

from __future__ import annotations

import curses
import sys
import textwrap
from dataclasses import dataclass, field

from umwelt.mud import Repl, WorldClient, WorldError


def _client_classes(client):
    """Return `(Repl, WorldError)` from the module the `client` was built in
    — NOT this module's imports.

    Load-bearing: `mud --tui` runs `python -m umwelt.mud`, which loads mud.py
    as the `__main__` module (where `main()` builds the client). But this
    file does `from umwelt.mud import Repl, WorldError`, which triggers a
    SECOND load of mud.py as `umwelt.mud` — so this module's `WorldError` is
    a DIFFERENT class object than the `__main__.WorldError` the client
    raises. A Repl built from this module's `Repl` would catch the wrong
    class in `dispatch()`, letting a normal world-refusal (e.g. `use` on a
    container) escape and crash the whole curses session. Building the Repl
    from the client's OWN module keeps every class identity matched
    regardless of entry point (`-m umwelt.mud`, `import umwelt.mud`, tests).
    """
    mod = sys.modules.get(type(client).__module__)
    # Fall back to this module's imports if the client's module can't be
    # resolved (shouldn't happen in practice) — no worse than before.
    return (getattr(mod, "Repl", Repl), getattr(mod, "WorldError", WorldError))

# --------------------------------------------------------------------------
# Layout constants
# --------------------------------------------------------------------------

PLACE_H_DEFAULT = 3          # title line + up to 2 wrapped description lines
_FIXED_ROWS = 4              # presence(1) + divider(1) + bar(1) + input(1)
_MIN_MID_H = 3               # minimum usable height for the things|focus split
MIN_H = PLACE_H_DEFAULT + _FIXED_ROWS + _MIN_MID_H   # 10
MIN_W = 40
_THINGS_W_MIN = 10
_FOCUS_W_MIN = 10

_BAR_TEXT = "[l]ook [g]o [x]amine [u]se [s]ay [r]ead [f]orge [h]istory [q]uit"

# ESC (curses reports a bare Escape as this string via get_wch when no
# further escape-sequence bytes follow within the terminal's escape timing —
# standard with stdscr.keypad(True) already set at launch, per umwelt #129).
_ESC = "\x1b"


# --------------------------------------------------------------------------
# Pure layout / render functions — no curses, no I/O. This is what's tested.
# --------------------------------------------------------------------------

def layout_regions(h: int, w: int) -> dict:
    """Compute pane rectangles for a (h, w) terminal size.

    Returns `{"too_small": True, "min_h", "min_w", "h", "w"}` when the
    terminal can't fit the minimum layout; otherwise a dict with
    `place` / `presence` / `things` / `focus` / `bar` / `input` rects
    (each `{"y","x","h","w"}`), plus `divider_y` (the horizontal rule row
    between the presence strip and the things|focus split) and
    `vdivider_x` (the column of the vertical rule between things and
    focus) for the caller to draw the separator characters. (umwelt #129
    slice A: this rect was named `events` pre-redesign — the FULL-SCREEN
    history overlay draws over the whole layout separately, see
    `render_history_overlay_lines` / `_draw`, and does not use this rect.)
    """
    if h < MIN_H or w < MIN_W:
        return {"too_small": True, "min_h": MIN_H, "min_w": MIN_W, "h": h, "w": w}

    place_h = PLACE_H_DEFAULT
    mid_h = h - place_h - _FIXED_ROWS
    if mid_h < _MIN_MID_H:
        # A slightly-short terminal: shrink the place pane first rather
        # than giving up immediately.
        shrink = _MIN_MID_H - mid_h
        place_h = max(1, place_h - shrink)
        mid_h = h - place_h - _FIXED_ROWS
    if mid_h < 1:
        return {"too_small": True, "min_h": MIN_H, "min_w": MIN_W, "h": h, "w": w}

    things_w = max(_THINGS_W_MIN, (w - 1) * 2 // 5)
    focus_w = w - things_w - 1
    if focus_w < _FOCUS_W_MIN:
        things_w = max(_THINGS_W_MIN, w - _FOCUS_W_MIN - 1)
        focus_w = w - things_w - 1

    y = 0
    place = {"y": y, "x": 0, "h": place_h, "w": w}
    y += place_h
    presence = {"y": y, "x": 0, "h": 1, "w": w}
    y += 1
    divider_y = y
    y += 1
    things = {"y": y, "x": 0, "h": mid_h, "w": things_w}
    focus = {"y": y, "x": things_w + 1, "h": mid_h, "w": max(1, focus_w)}
    y += mid_h
    bar = {"y": y, "x": 0, "h": 1, "w": w}
    y += 1
    input_ = {"y": y, "x": 0, "h": 1, "w": w}

    return {
        "too_small": False,
        "place": place, "presence": presence, "divider_y": divider_y,
        "vdivider_x": things_w, "things": things, "focus": focus,
        "bar": bar, "input": input_,
    }


def render_too_small_message(regions: dict) -> str:
    """The minimum-size message layout_regions' too_small case calls for."""
    return (f"terminal too small: need at least {regions['min_h']}x{regions['min_w']}, "
            f"have {regions['h']}x{regions['w']}")


def render_place_lines(view: dict, width: int, height: int = PLACE_H_DEFAULT) -> list[str]:
    """Place pane: label line + wrapped description, truncated with … to
    fit `height` total lines."""
    width = max(1, width)
    place = view.get("place") or {}
    label = place.get("label") or place.get("id") or "?"
    lines = [label[:width]]
    if height <= 1:
        return lines[:height]

    desc = place.get("description") or ""
    avail = height - 1
    if desc and avail > 0:
        wrapped = textwrap.wrap(desc, width=width) or []
        if len(wrapped) > avail:
            shown = wrapped[:avail]
            last = shown[-1]
            if len(last) >= width:
                last = last[:max(0, width - 1)]
            shown[-1] = last.rstrip() + "…"
            wrapped = shown
        lines.extend(wrapped)
    return lines[:height]


def render_presence_line(view: dict, self_agent: str | None, width: int) -> str:
    """Presence strip: 'here: name · name · you'-style, per #74's sketch."""
    width = max(1, width)
    present = view.get("present") or []
    names = []
    for p in present:
        name = p.get("name") or p.get("agent") or "someone"
        if self_agent is not None and p.get("agent") == self_agent:
            name = f"{name} (you)"
        names.append(name)
    line = "here: " + (" · ".join(names) if names else "(no one)")
    if len(line) > width:
        line = line[:max(0, width - 1)] + "…"
    return line


def render_things_lines(view: dict, width: int, height: int) -> list[str]:
    """Left pane: numbered things list, truncated with a '+N more' tail
    line when the list overflows the pane height."""
    width = max(1, width)
    things = view.get("things") or []
    if not things:
        return ["(nothing here)"]
    lines = []
    for i, t in enumerate(things, 1):
        line = f"{i} {t.get('name', '?')}"
        if len(line) > width:
            line = line[:max(0, width - 1)] + "…"
        lines.append(line)
    if height and len(lines) > height:
        shown = lines[:max(0, height - 1)]
        shown.append(f"…(+{len(lines) - len(shown)} more)")
        return shown
    return lines


def render_events_lines(feed: list[str], width: int, height: int) -> list[str]:
    """The event feed's line-wrapping — crossings + every action's output,
    newest at the bottom. `feed` is a list of already-formatted (possibly
    multi-line) entries, oldest first. Pre-#129 this rendered directly into
    an always-on side pane; now it's the body of the full-screen history
    overlay (`render_history_overlay_lines`) — kept as its own function
    since the wrap+newest-at-bottom logic is exactly what the overlay needs
    unchanged, just at full-screen width/height instead of a side pane's."""
    width = max(1, width)
    wrapped_all: list[str] = []
    for entry in feed:
        for sub in entry.split("\n"):
            wrapped_all.extend(textwrap.wrap(sub, width=width) or [""])
    if not wrapped_all:
        return []
    if height and len(wrapped_all) > height:
        return wrapped_all[-height:]
    return wrapped_all


def render_focus_lines(focus: list[str], width: int, height: int) -> list[str]:
    """FOCUS pane (umwelt #129 slice A): the LAST ACTION's result —
    `examine`'s thing detail, or `look in <container>`'s numbered contents.
    `focus` holds a single (possibly multi-line) entry as a one-item list —
    unlike the history feed, there's nothing to accumulate, only the most
    recent result to show. Top-truncated with a '+N more' tail (mirrors
    render_things_lines' overflow style, not render_events_lines' — an
    overflowing RESULT should show its beginning with a continuation hint,
    not scroll to its end the way a chronological feed does)."""
    width = max(1, width)
    wrapped_all: list[str] = []
    for entry in focus:
        for sub in entry.split("\n"):
            wrapped_all.extend(textwrap.wrap(sub, width=width) or [""])
    if not wrapped_all:
        return []
    if height and len(wrapped_all) > height:
        shown = wrapped_all[:max(0, height - 1)]
        shown.append(f"…(+{len(wrapped_all) - len(shown)} more — see [h]istory)")
        return shown
    return wrapped_all


def render_history_overlay_lines(feed: list[str], width: int, height: int) -> list[str]:
    """Full-screen history overlay content (umwelt #129 slice A): a title
    line + the event feed body, wrapped to the FULL terminal width/height
    rather than the old side pane's — opened by `[h]`/`history`, closed by
    `esc`. Body reuses render_events_lines' wrap+newest-at-bottom logic
    unchanged; only the title line is new here."""
    width = max(1, width)
    title = "History — crossings + action outputs (esc to close)"
    if len(title) > width:
        title = title[:max(0, width - 1)] + "…"
    if height <= 1:
        return [title][:max(0, height)]
    body = render_events_lines(feed, width, height - 1)
    return [title] + body


def render_bar(width: int) -> list[str]:
    """Bottom static command bar."""
    width = max(1, width)
    text = _BAR_TEXT
    if len(text) > width:
        text = text[:max(0, width - 1)] + "…"
    return [text]


def render_input_line(buffer: str, width: int, prompt: str = "> ") -> str:
    """Input line: prompt + typed buffer, tail-truncated (keep the end
    visible, like a normal shell) when it overflows the pane width."""
    width = max(1, width)
    line = prompt + buffer
    if len(line) > width:
        line = "…" + line[-max(1, width - 1):]
    return line


# --------------------------------------------------------------------------
# curses-side state + drawing — thin; no layout logic lives here.
# --------------------------------------------------------------------------

@dataclass
class _TuiState:
    view: dict
    agent: str
    feed: list[str] = field(default_factory=list)
    # umwelt #129 slice A: `focus` holds the LAST dispatched action's
    # output, unconditionally (a single, possibly multi-line entry as a
    # one-item list) — separate from `feed`, which keeps accumulating
    # EVERYTHING (still the history overlay's source) regardless of what
    # updates focus. `overlay` is None in normal layout mode, or "history"
    # while the full-screen history overlay is open (`[h]`/`history` opens
    # it, `esc` closes it).
    focus: list[str] = field(default_factory=list)
    overlay: str | None = None


def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """addstr wrapped against curses.error — the bottom-right cell (and
    any off-by-one from a naive-width computation on unicode) can raise;
    losing one character at the terminal edge beats crashing the app."""
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def _safe_move(win, y: int, x: int) -> None:
    """move wrapped against curses.error — same resize-race rationale as
    _safe_addstr: `regions` was computed from a getmaxyx() call one
    loop-iteration earlier, so a resize landing in that window can leave
    the target cell outside the window's *current* bounds."""
    try:
        win.move(y, x)
    except curses.error:
        pass


def _draw(stdscr, regions: dict, state: _TuiState, input_buffer: str, prompt: str) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if regions.get("too_small"):
        msg = render_too_small_message(regions)
        _safe_addstr(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg[:w])
        stdscr.refresh()
        return

    if state.overlay == "history":
        # Full-screen (umwelt #129 slice A) — draws over the WHOLE terminal,
        # ignoring `regions` entirely; normal layout resumes the moment the
        # overlay closes (esc), nothing about `regions`/the panes below is
        # touched or needs recomputing for this branch.
        for i, line in enumerate(render_history_overlay_lines(state.feed, w, h)):
            _safe_addstr(stdscr, i, 0, line, curses.A_BOLD if i == 0 else 0)
        stdscr.refresh()
        return

    place, presence = regions["place"], regions["presence"]
    things, focus = regions["things"], regions["focus"]
    bar, inp = regions["bar"], regions["input"]

    for i, line in enumerate(render_place_lines(state.view, place["w"], place["h"])):
        _safe_addstr(stdscr, place["y"] + i, place["x"], line, curses.A_BOLD if i == 0 else 0)

    _safe_addstr(stdscr, presence["y"], presence["x"],
                 render_presence_line(state.view, state.agent, presence["w"]))

    _safe_addstr(stdscr, regions["divider_y"], 0, "─" * w)
    _safe_addstr(stdscr, regions["divider_y"], 0, " things " + "─" * max(0, regions["vdivider_x"] - 9))
    _safe_addstr(stdscr, regions["divider_y"], regions["vdivider_x"] + 1, " FOCUS ")

    for i, line in enumerate(render_things_lines(state.view, things["w"], things["h"])):
        _safe_addstr(stdscr, things["y"] + i, things["x"], line)

    vdx = regions["vdivider_x"]
    for row in range(things["y"], things["y"] + things["h"]):
        _safe_addstr(stdscr, row, vdx, "│")

    for i, line in enumerate(render_focus_lines(state.focus, focus["w"], focus["h"])):
        _safe_addstr(stdscr, focus["y"] + i, focus["x"], line)

    _safe_addstr(stdscr, bar["y"], bar["x"], render_bar(bar["w"])[0], curses.A_REVERSE)

    line = render_input_line(input_buffer, inp["w"], prompt)
    _safe_addstr(stdscr, inp["y"], inp["x"], line)
    _safe_move(stdscr, inp["y"], min(inp["x"] + len(line), inp["x"] + inp["w"] - 1))
    stdscr.refresh()


def _read_line(stdscr, state: _TuiState, prompt: str = "> ",
               escape_closes: bool = False) -> str:
    """Blocking line-read via get_wch (unicode-capable) — redraws every
    pane on every keystroke and on KEY_RESIZE so the layout is always
    current. Raises EOFError on Ctrl-D with an empty buffer,
    KeyboardInterrupt on Ctrl-C (both caught by the caller for clean
    teardown / quit).

    `escape_closes` (umwelt #129 slice A): when True (the caller passes
    this while the history overlay is open), a bare Escape keypress returns
    immediately as the sentinel `_ESC` — WITHOUT waiting for Enter, since
    "esc to close" is a keypress, not a typed command. False (the normal
    input-line case) leaves Escape's existing behavior unchanged (ignored,
    same as any other non-printable control char today)."""
    buf: list[str] = []
    while True:
        h, w = stdscr.getmaxyx()
        regions = layout_regions(h, w)
        _draw(stdscr, regions, state, "".join(buf), prompt)
        try:
            ch = stdscr.get_wch()
        except curses.error:
            continue
        if isinstance(ch, str):
            if escape_closes and ch == _ESC:
                return _ESC
            if ch in ("\n", "\r"):
                return "".join(buf)
            if ch == "\x04":  # Ctrl-D
                if not buf:
                    raise EOFError
                continue
            if ch == "\x03":  # Ctrl-C
                raise KeyboardInterrupt
            if ch in ("\x7f", "\x08"):  # backspace (DEL or BS)
                if buf:
                    buf.pop()
                continue
            if ch.isprintable():
                buf.append(ch)
        else:
            if ch in (curses.KEY_BACKSPACE,):
                if buf:
                    buf.pop()
            # KEY_RESIZE and anything else: just loop — the top of the
            # loop recomputes h/w and redraws with the new layout.


_INTERRUPTED_MSG = "interrupted — leaving the world."


def _run(stdscr, client: WorldClient, agent: str, initial_view: dict) -> str | None:
    """Runs the TUI session. Returns an exit message to print *after*
    curses teardown (e.g. on Ctrl-C/Ctrl-D), or None on a normal `quit`."""
    try:
        curses.curs_set(1)
    except curses.error:
        pass  # some terminals (TERM=dumb, some containerized ttys) don't
        # support cursor-visibility control — must not crash launch.
    stdscr.keypad(True)

    state = _TuiState(view=initial_view, agent=agent)
    place_label = (initial_view.get("place") or {}).get("label", "?")
    state.feed.append(f"You arrive at {place_label}.")

    def _tui_input_fn(prompt: str) -> str:
        # forge's multi-prompt flow reads from the TUI input line
        # sequentially (umwelt #74) — each sub-prompt is echoed into the
        # feed so the flow's context isn't lost once the input line
        # clears for the next prompt.
        val = _read_line(stdscr, state, prompt=prompt)
        state.feed.append(f"{prompt}{val}")
        return val

    # Build the Repl (and resolve WorldError) from the CLIENT's module, not
    # this file's imports — see _client_classes for why (dual-module identity
    # under `python -m umwelt.mud`). Without this, dispatch()'s WorldError
    # catch misses and a normal world-refusal crashes the TUI.
    _Repl, _WorldError = _client_classes(client)
    repl = _Repl(client, agent, width=78, input_fn=_tui_input_fn)
    repl.set_view(initial_view)

    while not repl.quit:
        h, w = stdscr.getmaxyx()
        repl.width = min(78, max(20, w - 2))
        try:
            line = _read_line(stdscr, state, prompt="> ",
                               escape_closes=(state.overlay is not None))
        except (EOFError, KeyboardInterrupt):
            return _INTERRUPTED_MSG

        if state.overlay is not None:
            # umwelt #129 slice A: while the history overlay is open, ESC
            # is the only keypress with any effect (the escape_closes
            # sentinel above) — anything else (a stray Enter on an empty
            # or non-empty buffer) is ignored and the overlay stays open;
            # never falls through to dispatch() while showing the overlay.
            if line == _ESC:
                state.overlay = None
            continue

        if not line.strip():
            continue

        if line.strip().lower() in ("h", "history"):
            # TUI-only chrome (umwelt #129 slice A) — never reaches
            # Repl.dispatch(), same as this file's other curses-loop-only
            # concerns (resize, quit-on-interrupt).
            state.overlay = "history"
            continue

        # dispatch() itself only catches WorldError — a Ctrl-C/Ctrl-D
        # raised from forge's nested input_fn prompts (_tui_input_fn ->
        # _read_line) propagates straight up through _collect_forge_spec
        # and _cmd_forge (which only catch EOFError/OSError) and out of
        # dispatch() uncaught. Left unguarded here, that traceback would
        # surface *after* curses.wrapper's teardown — catch it for the
        # same clean exit as the outer _read_line above.
        try:
            out = repl.dispatch(line)
        except (EOFError, KeyboardInterrupt):
            return _INTERRUPTED_MSG
        if out:
            # umwelt #129 slice A (Lei's final simplification, third
            # refinement in one evening): FOCUS shows every action's
            # output unconditionally — no exclusions, even when it's
            # redundant with a sticky pane. Simpler beats clever here.
            state.feed.append(f"> {line}")
            state.feed.append(out)
            state.focus = [out]
        state.view = repl.view

        if repl.quit:
            break

        # Re-render from the freshest view after every dispatch — a
        # look-equivalent refetch, per #74 ("fine and simple").
        try:
            repl._cmd_look("")
        except _WorldError as e:
            state.feed.append(str(e))
        state.view = repl.view

    return None


def launch(client: WorldClient, agent: str, initial_view: dict) -> None:
    """Entry point mud.py calls when --tui is passed. `curses.wrapper`
    guarantees endwin() runs (terminal restored) even on exception,
    Ctrl-C, or an internal bug — the terminal must never be left raw.
    On a Ctrl-C/Ctrl-D interrupt, `_run` returns a message instead of
    raising — printed here, after teardown, so it lands on the normal
    (non-curses) terminal rather than a stale curses screen."""
    msg = curses.wrapper(_run, client, agent, initial_view)
    if msg:
        print(msg)
