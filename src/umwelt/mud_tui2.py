"""Umwelt interactive TUI — `python -m umwelt.mud --tui2` (umwelt #173).

Lei's pilot cockpit, designed to his 2026-07-15 request: *"a TUI I can
scroll when content overflows, use arrow keys to go from one item to
another, and Enter to choose available actions on them."*

This is the FOURTH renderer over the one world API (line REPL, web view,
sticky-pane TUI, and now this) — same `WorldClient`, same `Repl` for typed
commands; no parallel world logic.

Layout
------
    ┌ umwelt · lei @ The Square · ✉0 ──────────────────────────┐
    │ HERE                        │ DETAIL                     │
    │ > 1. kepler's telescope     │ kepler's telescope         │
    │   2. the echo well          │ A brass telescope on a     │
    │   3. ⌂ Area: commons        │ worn wooden tripod...      │
    │   4. ⇄ The Workshop         │                            │
    │   5. ☺ borges               │ [Enter] actions            │
    ├─ LOG ────────────────────────────────────────────────────┤
    │ [12:01] you: hello          │
    │ [12:02] the world: ...      │
    ├──────────────────────────────────────────────────────────┤
    │ ↑↓ select · Enter actions · Tab pane · m map · h hear    │
    │ : command · ? help · q quit                              │
    └──────────────────────────────────────────────────────────┘

The HERE pane lists everything actionable where you stand: things,
people, sub-places (⌂), doors (⇄), and your anchors (⌂!). DETAIL shows
the selected item. Enter opens a context menu of the verbs that make
sense for the selected item's kind. LOG accumulates every action's
output and incoming crossings; it scrolls.

Keys (global unless an overlay is open)
---------------------------------------
  ↑ / ↓        move selection (HERE) or scroll (LOG, when focused)
  PgUp / PgDn  page the focused pane
  Tab / ← / →  switch focus between HERE and LOG
  Enter        open the action menu for the selected HERE item
  m            map overlay — the whole world tree; ↑↓ move, Enter
               travels there, any letters typed filter by name,
               Esc closes
  h            hear — fetch new crossings here into LOG
  :  or  s     the command line — type anything the line REPL accepts
               (say hello / go square / read / forge ...); Esc cancels.
               Framed editor box: ←/→ move the cursor, Home/End (or
               ^A/^E) jump to the ends, Delete removes forward.
  ?            help overlay (this table)
  q            quit

Menu overlays: ↑↓ select, Enter runs, Esc closes.

Design rules (same constitution as mud_tui.py):
  - Every layout/state decision is a PURE function over plain data —
    the test suite drives them without a tty.
  - The curses loop is thin: read key -> update state -> render rows.
  - All curses writes guarded; `curses.wrapper` guarantees teardown.
  - Unicode width: naive len() fallback, documented limitation.
"""

from __future__ import annotations

import curses
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable

from umwelt.mud import (
    _ALIASES,
    Repl,
    WorldClient,
    WorldError,
    relative_time,
)

# --------------------------------------------------------------------------
# Data model — items the HERE pane can select
# --------------------------------------------------------------------------

# kind -> (icon, ordered list of (label, verb-template))
# {id} / {label} are filled from the item; templates run through
# Repl.dispatch so every action stays a normal REPL command (one world
# API, one command grammar — the TUI adds navigation, not new verbs).
ACTION_MENU: dict[str, list[tuple[str, str]]] = {
    "thing": [
        ("examine", "examine {id}"),
        ("use", "use {id}"),
        ("read (look in)", "look in {id}"),
        ("take", "take {id}"),
    ],
    "person": [
        ("look at", "examine {id}"),      # id = avatar_id (exact, #175)
        ("whisper…", ":whisper {label} "),  # whisper wants the NAME;
                                            # ':' prefix = command line pre-filled
    ],
    "subplace": [
        ("go there", "go {id}"),
        ("peek (map)", "map {label}"),
    ],
    "door": [
        ("go through", "go {id}"),
    ],
    "anchor": [
        ("go home", "go {id}"),
    ],
}

_ICON = {"thing": " ", "person": "☺", "subplace": "⌂", "door": "⇄",
         "anchor": "⌂"}


@dataclass
class Item:
    kind: str          # thing | person | subplace | door | anchor
    id: str
    label: str         # DISPLAY label — may carry decoration (unread badge)
    desc: str = ""
    raw: str = ""      # undecorated label for command templates; empty
                       # means the display label is already clean

    @property
    def icon(self) -> str:
        return _ICON.get(self.kind, " ")

    @property
    def raw_label(self) -> str:
        return self.raw or self.label


@dataclass
class ListState:
    """A selectable, scrollable list — shared by HERE, menus, and the map."""
    items: list = field(default_factory=list)
    cursor: int = 0
    scroll: int = 0

    def clamp(self) -> None:
        n = len(self.items)
        self.cursor = 0 if n == 0 else max(0, min(self.cursor, n - 1))
        self.scroll = max(0, min(self.scroll, max(0, n - 1)))


def move_cursor(st: ListState, delta: int, page: int = 1) -> None:
    """Move the selection, keeping it visible. Pure state mutation —
    no I/O. `page` is the pane height for PgUp/PgDn jumps."""
    st.cursor += delta * page
    st.clamp()
    if st.cursor < st.scroll:
        st.scroll = st.cursor
    # caller re-clamps scroll against pane height at render time


def visible_slice(st: ListState, height: int) -> tuple[int, int]:
    """The [start, end) window of rows to draw for a pane of `height`.
    Keeps the cursor inside the window (scrolling follows selection)."""
    if height <= 0:
        return (0, 0)
    if st.cursor < st.scroll:
        st.scroll = st.cursor
    if st.cursor >= st.scroll + height:
        st.scroll = st.cursor - height + 1
    return (st.scroll, min(len(st.items), st.scroll + height))


def scroll_only(st: ListState, delta: int, height: int, total: int) -> None:
    """Scroll a non-selecting pane (LOG): move the window, no cursor."""
    st.scroll = max(0, min(st.scroll + delta, max(0, total - height)))


# --------------------------------------------------------------------------
# Building the HERE list — pure over the view dicts
# --------------------------------------------------------------------------

def build_here_items(view: dict, contextual: dict | None,
                     agent: str) -> list[Item]:
    """Everything actionable where you stand: things, people, sub-places,
    doors, anchors — in that order, so muscle memory holds."""
    items: list[Item] = []
    place = view.get("place") or {}
    for t in view.get("things") or []:
        items.append(Item("thing", t.get("id", "?"), t.get("name", "?"),
                          t.get("description", "")))
    for p in view.get("present") or []:
        name = p if isinstance(p, str) else p.get("agent", "?")
        if name == agent:
            continue
        # avatar_id when the server provides it (umwelt #175) — exact,
        # never ambiguous against things sharing the agent's name-prefix;
        # the agent name itself is the fallback for older servers (the
        # REPL's examine now resolves present-avatar names exactly too).
        av = p.get("avatar_id") if isinstance(p, dict) else None
        items.append(Item("person", av or name, name,
                          "a fellow resident, present here"))
    if contextual:
        cur = contextual.get("current") or {}
        for k in cur.get("children") or []:
            unread = k.get("unread") or 0
            badge = f" ✉{unread}" if unread else ""
            raw = k.get("label") or k["id"]
            items.append(Item("subplace", k["id"], raw + badge,
                              "a place inside this one — Enter to go",
                              raw=raw))
        for l in cur.get("links") or []:
            items.append(Item("door", l.get("dst_id", "?"),
                              l.get("dst_label") or l.get("dst_id", "?"),
                              "a door — Enter to go through"))
        for a in contextual.get("anchors") or []:
            if a.get("id") == place.get("id"):
                continue
            items.append(Item("anchor", a["id"],
                              (a.get("label") or a["id"]) + " (yours)",
                              "always reachable — Enter to go"))
    return items


def actions_for(item: Item) -> list[tuple[str, str]]:
    """The context menu for an item — (label, command) pairs, commands
    already filled in. Pure."""
    out = []
    for label, tmpl in ACTION_MENU.get(item.kind, []):
        out.append((label, tmpl.format(id=item.id, label=item.raw_label)))
    return out


# --------------------------------------------------------------------------
# Map overlay model
# --------------------------------------------------------------------------

def flatten_map(roots: list[dict], flt: str = "") -> list[tuple[int, dict]]:
    """(depth, node) rows for the map overlay, in render order. When a
    filter string is given, keep matching nodes plus their ancestors
    (client-side echo of the server's own path-preserving semantics —
    used for LIVE keystroke filtering without a round-trip per key)."""
    rows: list[tuple[int, dict]] = []

    def _walk(n: dict, depth: int) -> bool:
        my_row = (depth, n)
        kids = n.get("children") or []
        keep_self = not flt or flt.lower() in (n.get("label") or "").lower()
        idx = len(rows)
        rows.append(my_row)
        keep_kid = False
        for c in kids:
            keep_kid = _walk(c, depth + 1) or keep_kid
        if not (keep_self or keep_kid):
            del rows[idx]
            # children that also failed removed themselves already
            return False
        if not keep_self and keep_kid:
            pass  # kept as ancestor path
        return True

    for r in roots:
        _walk(r, 0)
    return rows


# --------------------------------------------------------------------------
# Pure renderers — state in, list[str] rows out
# --------------------------------------------------------------------------

def _fit(s: str, w: int) -> str:
    s = s.replace("\n", " ")
    return s[:w].ljust(w)


def render_status(agent: str, place_label: str, unread: int, w: int) -> str:
    return _fit(f" umwelt · {agent} @ {place_label} · ✉{unread}", w)


def render_here_rows(st: ListState, height: int, w: int,
                     focused: bool) -> list[str]:
    start, end = visible_slice(st, height)
    rows = []
    for i in range(start, end):
        it: Item = st.items[i]
        sel = ">" if (i == st.cursor and focused) else " "
        rows.append(_fit(f"{sel}{it.icon} {i + 1}. {it.label}", w))
    while len(rows) < height:
        rows.append(" " * w)
    if end < len(st.items):
        rows[-1] = _fit(f"  … +{len(st.items) - end} more (↓)", w)
    return rows


def render_detail_rows(item: Item | None, height: int, w: int) -> list[str]:
    if item is None:
        rows = ["(nothing here to select)"]
    else:
        rows = [item.label, ""]
        rows += textwrap.wrap(item.desc or "(no description)", max(8, w - 1))
        rows += ["", "[Enter] actions"]
    rows = [_fit(r, w) for r in rows[:height]]
    while len(rows) < height:
        rows.append(" " * w)
    return rows


def render_log_rows(lines: list[str], st: ListState, height: int, w: int,
                    focused: bool) -> list[str]:
    if height <= 0:
        return []
    total = len(lines)
    if st.scroll == 0 and not focused:
        window = lines[-height:] if total > height else lines
    else:
        # scroll counts UP from the tail: scroll=0 means "pinned to newest"
        end = max(0, total - st.scroll)
        window = lines[max(0, end - height):end]
    rows = [_fit(l, w) for l in window]
    while len(rows) < height:
        rows.append(" " * w)
    if st.scroll > 0:
        rows[-1] = _fit(f"  … scrolled ↑{st.scroll} (End: newest)", w)
    return rows


def render_menu_rows(title: str, entries: list[str], cursor: int,
                     w: int) -> list[str]:
    inner = max(len(title), *(len(e) for e in entries)) + 6 if entries else 20
    inner = min(inner, w - 4)
    top = "┌" + "─" * inner + "┐"
    rows = [top, "│" + _fit(" " + title, inner) + "│",
            "│" + "─" * inner + "│"]
    for i, e in enumerate(entries):
        mark = "> " if i == cursor else "  "
        rows.append("│" + _fit(f" {mark}{e}", inner) + "│")
    rows.append("└" + "─" * inner + "┘")
    return rows


KEYBAR = (" ↑↓ select · Enter actions · Tab pane · m map · h hear · "
          ": command · ? help · q quit")


# --------------------------------------------------------------------------
# The interactive session state
# --------------------------------------------------------------------------

@dataclass
class Session:
    client: WorldClient
    repl: Repl
    agent: str
    view: dict
    contextual: dict | None = None
    here: ListState = field(default_factory=ListState)
    log_lines: list[str] = field(default_factory=list)
    log: ListState = field(default_factory=ListState)
    focus: str = "here"                # here | log
    overlay: str | None = None         # None | menu | map | help
    menu: ListState = field(default_factory=ListState)
    menu_item: Item | None = None
    map_rows: list[tuple[int, dict]] = field(default_factory=list)
    map_list: ListState = field(default_factory=ListState)
    map_filter: str = ""
    map_roots: list[dict] = field(default_factory=list)
    quit: bool = False

    # -- world refresh ----------------------------------------------------
    def refresh(self) -> None:
        # umwelt #184/#219/forum #263: re-pointed off the side-effecting
        # move_to(agent, self.repl.place_id) — the same teleport class as
        # mud.py's _cmd_look, just in TUI2's own refresh loop (called after
        # EVERY dispatched command): a stale self.repl.place_id would move
        # the avatar back to wherever this session last thought it stood,
        # rather than rendering where it actually is. look() resolves the
        # real current place server-side, no place_id needed at all.
        try:
            self.view = self.client.look(self.agent)
            self.repl.set_view(self.view)
        except WorldError as e:
            self.push_log(f"⚠ {e}")
        try:
            self.contextual = self.client.places_contextual(self.agent)
        except WorldError:
            self.contextual = None
        keep = self.here.cursor
        self.here.items = build_here_items(self.view, self.contextual,
                                           self.agent)
        self.here.cursor = keep
        self.here.clamp()

    def push_log(self, text: str) -> None:
        for ln in (text or "").split("\n"):
            self.log_lines.append(ln)
        self.log.scroll = 0     # new output pins the log to newest

    def run_command(self, cmd: str) -> None:
        """Every action funnels through the one REPL grammar. NEVER raises:
        a UI action that fails must land in the LOG, not crash the screen
        (umwelt #176 — Lei's first-flight 'look in' on a non-container)."""
        try:
            out = self.repl.dispatch(cmd)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:            # noqa: BLE001 — the cockpit's floor
            self.push_log(f"⚠ that didn't work: {e}")
            # the failed action may still have landed a server-side state
            # change before raising — refresh anyway (itself error-safe)
            # so the view can't go stale behind an error.
            self.refresh()
            return
        if out:
            self.push_log(out)
        if getattr(self.repl, "quit", False):
            # ':quit' typed at the command line leaves the TUI too —
            # the REPL's quit flag propagates to the session's.
            self.quit = True
            return
        self.refresh()

    def selected(self) -> Item | None:
        if not self.here.items:
            return None
        return self.here.items[self.here.cursor]

    # -- hear ---------------------------------------------------------------
    def hear(self) -> None:
        try:
            resp = self.client.crossings_here(self.agent, self.repl.place_id,
                                              advance=True, limit=25)
        except WorldError as e:
            self.push_log(f"⚠ {e}")
            return
        crossings = resp.get("crossings") or []
        if not crossings:
            self.push_log("(nothing new here)")
            return
        for c in crossings:
            rel = relative_time(c.get("received_at"))
            body = c.get("body") or "(a wordless signal)"
            self.push_log(f"[{rel}] {c.get('sender', '?')}: {body}")
        skipped = resp.get("skipped") or 0
        if skipped:
            self.push_log(f"(+{skipped} older not shown — ':read all' for history)")

    # -- map ------------------------------------------------------------------
    def open_map(self) -> None:
        try:
            resp = self.client.map(self.agent)
        except WorldError as e:
            self.push_log(f"⚠ {e}")
            return
        self.map_roots = resp.get("roots") or []
        self.map_filter = ""
        self.map_rows = flatten_map(self.map_roots)
        self.map_list = ListState(items=self.map_rows)
        self.overlay = "map"

    def refilter_map(self) -> None:
        self.map_rows = flatten_map(self.map_roots, self.map_filter)
        self.map_list = ListState(items=self.map_rows)


# --------------------------------------------------------------------------
# Key handling — pure-ish (mutates session, no curses)
# --------------------------------------------------------------------------

def handle_key(s: Session, key: int, page: int,
               read_line: Callable[[str], str | None]) -> None:
    """One keypress -> state change. `read_line(prefill)` is the injected
    command-line prompt (curses in real life, scripted in tests); it
    returns the typed line or None on Esc."""
    if s.overlay == "menu":
        _handle_menu_key(s, key, read_line)
        return
    if s.overlay == "map":
        _handle_map_key(s, key, page)
        return
    if s.overlay == "help":
        s.overlay = None
        return

    if key in (ord("q"), ord("Q")):
        s.quit = True
    elif key == ord("?"):
        s.overlay = "help"
    elif key in (9, curses.KEY_LEFT, curses.KEY_RIGHT):   # Tab / arrows
        s.focus = "log" if s.focus == "here" else "here"
    elif key == curses.KEY_UP:
        if s.focus == "here":
            move_cursor(s.here, -1)
        else:
            scroll_only(s.log, +1, page, len(s.log_lines))
    elif key == curses.KEY_DOWN:
        if s.focus == "here":
            move_cursor(s.here, +1)
        else:
            scroll_only(s.log, -1, page, len(s.log_lines))
    elif key == curses.KEY_PPAGE:
        if s.focus == "here":
            move_cursor(s.here, -1, page)
        else:
            scroll_only(s.log, +page, page, len(s.log_lines))
    elif key == curses.KEY_NPAGE:
        if s.focus == "here":
            move_cursor(s.here, +1, page)
        else:
            scroll_only(s.log, -page, page, len(s.log_lines))
    elif key == curses.KEY_END:
        s.log.scroll = 0
    elif key in (10, 13, curses.KEY_ENTER):
        item = s.selected()
        if item and s.focus == "here":
            entries = actions_for(item)
            if entries:
                s.menu_item = item
                s.menu = ListState(items=entries)
                s.overlay = "menu"
    elif key in (ord(":"), ord("s"), ord("S")):
        line = read_line("")
        if line:
            # bare text typed at ':' with no known verb reads naturally
            # as speech — 'say ' is prefixed when the first word isn't a
            # REPL command (so ':hello everyone' just says it).
            first = line.split(None, 1)[0].lower() if line.split() else ""
            known = _ALIASES.get(first, first)
            if not hasattr(s.repl, f"_cmd_{known}"):
                line = "say " + line
            s.run_command(line)
    elif key in (ord("h"), ord("H")):
        s.hear()
    elif key in (ord("m"), ord("M")):
        s.open_map()


def _handle_menu_key(s: Session, key: int,
                     read_line: Callable[[str], str | None]) -> None:
    if key == 27:                     # Esc
        s.overlay = None
        return
    if key == curses.KEY_UP:
        move_cursor(s.menu, -1)
    elif key == curses.KEY_DOWN:
        move_cursor(s.menu, +1)
    elif key in (10, 13, curses.KEY_ENTER):
        if not s.menu.items:
            s.overlay = None
            return
        _, cmd = s.menu.items[s.menu.cursor]
        s.overlay = None
        if cmd.startswith(":"):       # pre-filled command line
            line = read_line(cmd[1:])
            if line:
                s.run_command(line)
        else:
            s.run_command(cmd)


def _handle_map_key(s: Session, key: int, page: int) -> None:
    if key == 27:                     # Esc
        s.overlay = None
        return
    if key == curses.KEY_UP:
        move_cursor(s.map_list, -1)
    elif key == curses.KEY_DOWN:
        move_cursor(s.map_list, +1)
    elif key == curses.KEY_PPAGE:
        move_cursor(s.map_list, -1, page)
    elif key == curses.KEY_NPAGE:
        move_cursor(s.map_list, +1, page)
    elif key in (10, 13, curses.KEY_ENTER):
        if s.map_list.items:
            _, node = s.map_list.items[s.map_list.cursor]
            s.overlay = None
            s.run_command(f"go {node.get('id')}")
    elif key in (curses.KEY_BACKSPACE, 127, 8):
        s.map_filter = s.map_filter[:-1]
        s.refilter_map()
    elif 32 <= key < 127:             # live typing filters
        s.map_filter += chr(key)
        s.refilter_map()


# --------------------------------------------------------------------------
# Frame assembly (pure) + the thin curses loop
# --------------------------------------------------------------------------

def render_frame(s: Session, w: int, h: int) -> list[str]:
    """The whole screen as rows of exactly width w — pure."""
    place = (s.view.get("place") or {})
    unread = 0
    if s.contextual:
        unread = (s.contextual.get("current") or {}).get("unread") or 0
    rows = [render_status(s.agent, place.get("label", "?"), unread, w)]

    body_h = max(4, h - 4)            # status + divider + keybar + input
    here_h = max(3, int(body_h * 0.55))
    log_h = body_h - here_h - 1
    lw = max(20, w // 2)
    rw = w - lw - 1

    here_rows = render_here_rows(s.here, here_h, lw, s.focus == "here")
    detail_rows = render_detail_rows(s.selected(), here_h, rw)
    for hr, dr in zip(here_rows, detail_rows):
        rows.append(hr + "│" + dr)
    rows.append(_fit("─ LOG " + ("(focused) " if s.focus == "log" else "")
                     + "─" * w, w))
    rows.extend(render_log_rows(s.log_lines, s.log, log_h, w,
                                s.focus == "log"))
    rows.append(_fit(KEYBAR, w))

    # overlays draw over the top rows
    overlay_rows: list[str] = []
    if s.overlay == "menu" and s.menu_item:
        overlay_rows = render_menu_rows(f"{s.menu_item.label} — actions",
                                        [e[0] for e in s.menu.items],
                                        s.menu.cursor, w)
    elif s.overlay == "map":
        title = f"map — type to filter: {s.map_filter or '(all)'}"
        entries = []
        start, end = visible_slice(s.map_list, max(4, h - 8))
        for i in range(start, end):
            depth, node = s.map_list.items[i]
            unread_n = node.get("unread") or 0
            badge = f" ✉{unread_n}" if unread_n else ""
            entries.append("  " * depth + (node.get("label") or "?") + badge)
        cursor_in_window = s.map_list.cursor - start
        overlay_rows = render_menu_rows(title, entries or ["(no match)"],
                                        cursor_in_window, w)
    elif s.overlay == "help":
        overlay_rows = render_menu_rows(
            "keys", [
                "↑/↓ select or scroll · PgUp/PgDn pages",
                "Tab or ←/→  switch HERE ⇄ LOG",
                "Enter  actions for the selected item",
                "m map (type filters · Enter travels)",
                "h hear new crossings",
                ": command line (also plain speech)",
                "End  jump log to newest",
                "q quit · Esc closes overlays",
            ], -1, w)

    for i, orow in enumerate(overlay_rows):
        at = 2 + i
        if at < len(rows):
            pad = max(0, (w - len(orow)) // 2)
            base = rows[at]
            rows[at] = (base[:pad] + orow + base[pad + len(orow):])[:w]
    return rows


# --------------------------------------------------------------------------
# Command-line editor — pure state + key handling, framed by rules above
# and below like Claude Code's input box (umwelt #179). Free cursor nav
# and insert-at-cursor, with a horizontal scroll window so long input
# never hides characters off the right edge. One editor, one call site
# (read_line) — shared by the ':' command line, whisper prefill, and any
# REPL command typed there (forge included).
# --------------------------------------------------------------------------

@dataclass
class EditorState:
    text: str = ""
    cursor: int = 0        # index into text, 0..len(text)
    scroll: int = 0        # leftmost visible column


def editor_visible_window(st: EditorState, width: int) -> tuple[str, int]:
    """The [scroll, scroll+width) slice of text to draw, and the cursor's
    column within it. Keeps the cursor inside the window — the same
    follow-the-cursor scrolling as visible_slice, just horizontal."""
    if width <= 0:
        return "", 0
    if st.cursor < st.scroll:
        st.scroll = st.cursor
    if st.cursor >= st.scroll + width:
        st.scroll = st.cursor - width + 1
    st.scroll = max(0, st.scroll)
    return st.text[st.scroll: st.scroll + width], st.cursor - st.scroll


def editor_key(st: EditorState, key: int) -> str | None:
    """One keypress -> state mutation. Returns 'submit' on Enter, 'cancel'
    on Esc, None otherwise (still editing). Pure over EditorState — no
    curses — so this is what the test suite drives directly."""
    if key == 27:
        return "cancel"
    if key in (10, 13, curses.KEY_ENTER):
        return "submit"
    if key in (curses.KEY_BACKSPACE, 127, 8):
        if st.cursor > 0:
            st.text = st.text[: st.cursor - 1] + st.text[st.cursor:]
            st.cursor -= 1
    elif key == curses.KEY_DC:                       # forward delete
        st.text = st.text[: st.cursor] + st.text[st.cursor + 1:]
    elif key == curses.KEY_LEFT:
        st.cursor = max(0, st.cursor - 1)
    elif key == curses.KEY_RIGHT:
        st.cursor = min(len(st.text), st.cursor + 1)
    elif key in (curses.KEY_HOME, 1):                 # Home, or ^A
        st.cursor = 0
    elif key in (curses.KEY_END, 5):                  # End, or ^E
        st.cursor = len(st.text)
    elif 32 <= key < 127:
        st.text = st.text[: st.cursor] + chr(key) + st.text[st.cursor:]
        st.cursor += 1
    return None


def render_editor_box(st: EditorState, width: int) -> tuple[list[str], int]:
    """The framed input box — top rule, one content row, bottom rule.
    `width` is the interior width (rows are width+2 with the corners).
    Returns (rows, cursor_col) — cursor_col is where the cursor sits
    within the content row."""
    visible, col = editor_visible_window(st, width)
    top = "┌" + "─" * width + "┐"
    mid = "│" + visible.ljust(width)[:width] + "│"
    bot = "└" + "─" * width + "┘"
    return [top, mid, bot], col


def _read_line_curses(stdscr, prefill: str) -> str | None:
    """The command-line editor, framed by rules above and below like
    Claude Code's input box — the curses adapter around the pure
    EditorState / editor_key / render_editor_box core above. Draws over
    the 3 rows render_frame leaves free at the bottom of the screen."""
    h, w = stdscr.getmaxyx()
    st = EditorState(text=prefill, cursor=len(prefill))
    box_w = max(1, w - 2)
    curses.curs_set(1)
    try:
        while True:
            rows, col = render_editor_box(st, box_w)
            for i, row in enumerate(rows):
                y = h - 3 + i
                if y < 0:
                    continue
                try:
                    stdscr.addstr(y, 0, row[:w])
                except curses.error:
                    pass
            try:
                stdscr.move(h - 2, min(col + 1, max(0, w - 1)))
            except curses.error:
                pass
            ch = stdscr.getch()
            signal = editor_key(st, ch)
            if signal == "cancel":
                return None
            if signal == "submit":
                return st.text.strip()
    finally:
        curses.curs_set(0)


def _run(stdscr, client: WorldClient, agent: str, initial_view: dict) -> str:
    curses.curs_set(0)
    stdscr.keypad(True)
    repl = Repl(client, agent)
    repl.set_view(initial_view)
    s = Session(client=client, repl=repl, agent=agent, view=initial_view)
    s.refresh()
    s.push_log(f"welcome, {agent} — ? for keys")
    while not s.quit:
        h, w = stdscr.getmaxyx()
        # erase first: render_frame only draws rows 0..h-4, leaving the
        # editor box's 3 reserved rows untouched — without this, a
        # cancelled/submitted command line leaves stale box content on
        # screen (found live-driving the #179 editor via tmux).
        stdscr.erase()
        for y, row in enumerate(render_frame(s, w - 1, h - 1)):
            try:
                stdscr.addstr(y, 0, row)
            except curses.error:
                pass
        stdscr.refresh()
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            break
        try:
            handle_key(s, key, page=max(3, (h - 6) // 2),
                       read_line=lambda pre: _read_line_curses(stdscr, pre))
        except WorldError as e:
            s.push_log(f"⚠ {e}")
    return "left the world — the village keeps."


def launch(client: WorldClient, agent: str, initial_view: dict) -> None:
    print(curses.wrapper(_run, client, agent, initial_view))
