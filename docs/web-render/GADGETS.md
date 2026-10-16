# Webrender Gadget Charter — the UI component system (#352)

*Design doc, Kepler 2026-07-25. Gates #351 (floating outcome window = gadget #1).
Relates to #350. First readers: Ariadne + Luria (both converged 2026-07-25; §0's
survey measured at main=a03a6da — Luria verified the one later commit, #349,
touched no counted chrome). Lei approved 2026-07-25 with one addition, folded as
§1 obligation 5: built-in logging (regular + debug mode) — the UI half of the
village-wide component-logging convention (engine half: #359). This file is the component
system's single source of truth: the contract, the inventory, the anchoring and
styling rules, and the migration policy. PRs that add a gadget class edit the
inventory table in the same diff.*

**Scope**: the G-MUD street client (`src/umwelt/webrender/static/index.html` +
`app.js`). The legacy world/projects HTML in `server.py` (`_render_world_html` /
`_render_projects_html`) is explicitly OUT of scope — it predates the street
client and is not growing.

## 0. Why (the honest current state)

The client is well-disciplined at the *content* level (textContent-only
rendering per DESIGN.md §12.10, a four-var palette, table-driven action bar) and
completely undisciplined at the *chrome* level. Measured, not estimated
(2026-07-25 survey of index.html/app.js):

- the input+send row exists as **3** independent copies (`#action-bar-say`,
  `#action-bar-use`, `#interact-menu-talk-row`) — near-identical CSS bodies,
  each with its own open/close functions; the duplication is acknowledged in
  comments at all three sites rather than factored;
- the button chrome recipe (`padding / --gb-dark bg / --gb-lightest fg / 1px
  --gb-light border / radius / pointer`) is written out **6** times;
- fixed-panel chrome (`position:fixed` + own background/border + own `.hidden`
  rule) exists **3** times, two off-palette (`#1a1a1a`/`#333`) and one on;
- the close-button (×2, divergent), outside-click-dismiss (×2 verbatim),
  dialogue-box recipe (×2, with `--gb-dark` written out numerically as
  `rgba(48,98,48,…)` both times), and two-line clamp (×2) all repeat;
- in JS, the `/act` POST wrapper is **6** verbatim copies and the element-ref
  clusters, open/close conventions (`.hidden` vs `.open` vs inline
  `style.display`), and per-surface state are scattered per feature.

Lei's charge (#352): reusable gadgets with unified style — never "a
piece-by-piece gigantic pile of things." A change to what a window looks like
must land in ONE place.

## 1. What a gadget is (the contract)

A **gadget** is a self-contained UI unit with four obligations:

1. **A factory, not a snippet.** `ggWindow(opts)`, `ggPanel(opts)`, … — each
   gadget class has exactly one constructor function, in `gadgets.js`. No
   feature ever assembles chrome DOM inline again. The factory returns a
   uniform handle: `{ el, open(), close(), update(data), destroy() }` (plus
   class-specific members the inventory documents). Factories return a
   detached node or `null` for "don't render" — the existing
   `buildEmojiBubble` null-contract, kept.

   **Amendment (`editor` gadget, #544, reviewer-fairy catch on the first
   draft):** an EMBEDDABLE CONTENT class — one meant to sit inside a
   caller's own layout rather than as a floating/anchored layer (`ggEditor`
   is the first instance; a future in-place image cropper or similar would
   be another) — has no open/close/anchor lifecycle for `open()`/`close()`
   to govern, and no floating position for `update()`'s `targetEl` to
   re-point. Shipping no-op stubs for members that don't apply would imply
   a lifecycle the class doesn't have; the inventory's "plus class-specific
   members" clause is read as also permitting *documented* omission of
   base members that have no meaning for the class, not only addition —
   same posture as §2's `text-measure` row, which isn't a factory at all.
   The inventory table's own Notes column is where an embeddable class
   states which base members it omits and why; silent omission (only a
   code comment, no inventory-table trace) is not sufficient — this is
   what closes that gap for `editor`, flagged during review rather than
   left as an unexamined precedent.
2. **Chrome from the system, content from the caller.** Every pixel of frame —
   borders, title bar, close button, shadow, padding, scroll, `.hidden`
   handling — comes from `.gg-*` classes + the design tokens (§4). The caller
   supplies only content (a DOM node or text) and options. Review rule: a
   selector touching border/background/position/z-index/close-button outside
   the gadget stylesheet section is chrome leakage.
3. **Anchored by declaration, not by hand.** A gadget states its anchor mode
   (§3); it never computes screen position ad hoc and never sets a numeric
   z-index.
4. **Registered.** Every gadget class is a row in §2's table. Not in the
   table ⇒ not a gadget ⇒ not mergeable as new UI. Adding a class is a design
   event (a small PR editing this file in the same diff); composing existing
   classes is free.
5. **Observable.** (Lei's sign-off addition, 2026-07-25.) Every gadget logs
   through the one shared seam, `ggLog` (in gadgets.js) — never bare
   `console.*` in gadget or feature UI code. Two modes:
   - **regular** (always on): warnings and errors only, prefixed
     `[gg:<class>#<id>]` so any report from a user session says which gadget
     instance spoke;
   - **debug** (opt-in, off by default): full lifecycle — create / open /
     close / update / anchor / target-gone / destroy, with a one-line data
     summary per event. Toggled at runtime from the browser console via
     `ggDebug(true|false)` (persisted in `localStorage` so it survives
     refresh; `?ggdebug=1` in the URL forces it on for a session — the
     phone-debugging path, where no console is handy).
   Debug mode may additionally mirror entries into the in-world `#console`
   view so a phone session can read them. This is the UI half of a
   village-wide convention (engine half tracked as #359): components ship
   with their own debugging built in, so future-us debugs by flipping a
   switch, not by adding prints.

Deliberately NOT a framework: no virtual DOM, no reactivity, no build step, no
bundler. Plain factories + CSS classes + custom properties — the smallest
structure that prevents the pile.

## 2. The inventory (v1 — closed list)

| Gadget | Factory | Seed / consumers | Notes |
|---|---|---|---|
| **window** | `ggWindow({title?, anchor, closable=true, content})` | #351 outcome display (gadget #1); avatar-side live conversation (Lei's example); console (becomes a `screen`-anchored window, closable per #351) | Monospace/pre-wrap content variant for ASCII output (orrery chart) via `variant:"mono"` |
| **panel** | `ggPanel({edge, width, resizable, content})` | crossings sidebar (#350 seed); inspect panel | Owns a text-measure; width becomes the `--gg-panel-w` token (today's 320px is hardcoded in 3 places) |
| **button-row** | `ggButtonRow([{label, onPress, …}])` | action bar (#307 seed — its ACTIONS-table pattern is the model); interact-menu action list | Server-driven rows (interact menu builds from `t.actions`) stay server-driven — the factory takes the built list |
| **input-row** | `ggInputRow({placeholder, onSubmit, extras?})` | the 3 existing say/use/talk copies → one factory, three call sites | `extras` slot carries the emotion picker |
| **badge** | `ggBadge({kind, text, parentEl?})` | face badge + emotion bubble (#309) + held-marker (seed list; not yet migrated, §7.2); **landed (#967):** the status mark on a closed (retracted / approved) tray row — so the OUT tray's approved items render closed too | Parent-relative anchored (§3 mode `attached`). Embeddable content class: handle is `{ el, update({kind, text}), destroy() }`; `open()`/`close()` are deliberately absent (no open/close lifecycle, §1 obligation 1's amendment). `kind` ∈ `info`/`closed` (unknown → `info`, warned); text via `textContent` only. The terminal-state classification that decides *when* a tray row gets a `closed` badge is the server's `closed: true` on the look_in row (derived from the kinddef, umwelt #976; app.js keeps no state list), not part of the gadget |
| **text-measure** | *(CSS utilities, not a factory)* | `.gg-clamp2` (item labels, door signs), `.gg-wrap-any` (#350 break-anywhere), `.gg-mono-block` (#351 pre-wrap) | Documented classes only — no JS object |
| **editor** | `ggEditor({value?, placeholder?, rows?, onInput?})` | proposal composer's `ask`/`why` fields; village charge board's `add-entry` text field; a readable thing's Edit action (interact-menu edit sub-view, umwelt#547) | Embeddable content widget, not anchored/floating — `open()`/`close()`/`update()` are deliberately absent from the handle (no floating/anchor lifecycle to govern; see §1 obligation 1's amendment). `el.value` is a live get+set accessor over the textarea for drop-in compatibility with existing `<input>`-reading collection code (`_collectParamsArgs`, `openActParamsSubView`) |
| **status** | `ggStatus({anchor, parentEl, afterEl?, id?})` | the action-bar outcome line (#957): "took X" / the server's refusal reason / "take…" while in flight | Embeddable `attached` content class: handle is `{ el, show(kind, text), clear(), destroy() }`. `open()`/`close()`/`update()` are deliberately absent — one line of text with no open/close lifecycle and no floating anchor to re-point (§1 obligation 1's amendment). `kind` ∈ `pending`/`ok`/`error`; text via `textContent` only (server reason strings are untrusted); `ok`/`error` auto-clear after ~6 s, a newer `show()` cancels the older timer, `pending` never auto-clears. `el` carries `role="status"` + `aria-live="polite"` |
| **chip** | `ggChip({parentEl?, id?, onOpen, onClear, onSelectById})` | the selection chip (#936): the one persistent "what is selected" bar — name, kind, short id (+ copy), where; click opens the interact menu for the selection, ✕ clears it, a "select by id" entry takes a pasted id | Embeddable `attached` content class: handle is `{ el, update({name, kind, id, where, notice, noticeKind}), destroy() }`. `open()`/`close()` are deliberately absent (no open/close lifecycle, §1 obligation 1's amendment). Docks INSIDE `#composer` (a full-width flex row of it), so it can never overlap the composer or the surfaces the composer already clears. It is dumb: it owns no selection state (app.js's `selectThing` is the single writer of `state.selection`) and fetches nothing. Every world-derived string (name, kind, id, where, and `notice` — the SERVER's own refusal text, e.g. `in_private_place` / `binned` / 404) goes through `textContent` only, never `innerHTML` (DESIGN.md §12.10). No `id` in `update()` = the empty state (only the select-by-id entry, plus any `notice`) |

Composition rule: new surfaces compose these (a window containing a button-row
and an input-row is a dialog; the art-library browse UI is a window + rows; the
crossing play-button is a button inside the crossing row — no new class).

## 3. Anchoring + layers

Three anchor modes, one implementation each:

- **`attached`** — the gadget is a DOM child of a world element, positioned by
  CSS relative to its parent. This is the *proven* pattern (face badge, emotion
  bubble, held-marker) and involves zero JS math. Requires the parent to opt
  into `overflow: visible` where the gadget escapes its box (as
  `.item.avatar` already does).
- **`street(x?, nearEl?)`** — the gadget is appended to `#street` (which is
  `position:relative`) and positioned via offset coordinates, exactly the way
  the player cursor already works (`updateCursorPosition`'s
  offsetLeft/offsetParent approach). Because it lives INSIDE the scroll
  container, it scrolls with the street for free — this dissolves the
  objection that made the interact menu decline element-anchoring (fixed
  positioning fights the street's horizontal scroll; street-child positioning
  doesn't). **#351's window anchors this way.** One shared helper
  (`ggAnchorToStreetEl(gadgetEl, targetEl)`) owns the math. Two invariants of
  this mode (Ariadne's first-read conditions — they are what makes the cursor
  precedent actually transfer):
  - **Re-ANCHOR, not re-append, is the load-bearing operation.** `render()`
    clears the *bands*, not `#street` — a street-anchored gadget survives the
    rebuild, but its anchor TARGET is destroyed and recreated, leaving the
    gadget measuring a detached node. The render loop therefore keeps a
    registry of active street-gadgets and recomputes
    `ggAnchorToStreetEl(gadget, freshTarget)` against the NEW target element
    every cycle (generalizing what `render()` today hardcodes for the single
    `selfCursorEl`).
  - **Declared target-gone policy.** When the new scene no longer contains the
    gadget's target, each street-anchored gadget class declares its behavior —
    `close` (the interact menu's existing posture), `re-park` (the cursor's
    park-at-0 posture), or `hold` (stay at last position; the shared dismiss
    registry bounds staleness to one outside-click). Never decided per call
    site. #351's window: `hold` — amended from `close` by its proving-case
    implementation (PR #355): a `take` outcome's own re-render removes the
    target, and `close` would destroy the outcome before it's read.
- **`screen(pos)`** — viewport-fixed corners/edges/sheets for panels, the
  interact menu, and the console-window. Mobile behavior per class (§5).

> **Amendment (Kepler, 2026-07-25, the #351 outcome-window clip-fix):**
> `street` mode is, by construction, for gadgets that fit within the band —
> badges, small popups, the cursor. `#street` itself has `overflow-x: auto`
> (index.html), which creates a clip context: any `street`-anchored,
> `position: absolute` child is bounded by the band's own height, no matter
> how much content it holds. Any content-sized gadget — one whose content
> can run longer than a couple of lines (a readable's full text, an
> executable's full stdout, the orrery's star chart) — MUST use `screen`
> mode instead. #351's outcome window was the first counter-example to this
> (it shipped `street`-anchored, worked for short outcomes, then clipped to
> ~2 rows and 420px wide on anything longer) and has since moved to
> `screen` + a per-gadget `size` override (`ggWindow`'s `size` option,
> gadgets.js) rather than any street-side workaround (no
> append-higher-than-the-band, no scroll-listener variant — both rejected).

**Layers**: z-index is banned from feature code and numeric literals retire.
One token block:

```css
--gg-z-street: 5;    /* cursor, street-anchored gadgets */
--gg-z-panel: 10;    /* docked panels */
--gg-z-window: 20;   /* floating windows, interact menu */
--gg-z-toggle: 30;   /* mobile FABs (crossings toggle) */
```

A gadget's layer comes from its class; `bringToFront()` reorders only within
the window band (via DOM order, not z-index arithmetic).

## 4. One styling source

- **Where**: a new served stylesheet `static/gadgets.css`, linked from
  index.html before the inline block. (First external sheet — the static
  folder already serves at root, so this is additive and test-safe; the 24
  string-grepping tests against `/app.js` and `/` are untouched.)
- **Tokens** (extending the four `--gb-*` palette vars, which stay): the
  `--gg-z-*` block (§3); `--gg-panel-w: 320px` (replacing the 3 hardcoded
  copies: body padding-right, crossings width, inspect width);
  `--gg-radius: 4px`; `--gg-chrome-bg/fg/border` — initially mapped to the
  existing dark-chrome greys so the first conversion is a no-op visually, and
  the palette-vs-grey unification becomes a one-line decision later instead of
  a 10-file hunt; the dialogue-box tint gets `--gg-dialog-tint` (retiring the
  numerically-written-out `rgba(48,98,48,…)` copies).
- **Classes**: `.gg-window`, `.gg-panel`, `.gg-row`, `.gg-input-row`,
  `.gg-btn`, `.gg-close`, `.gg-badge`, `.gg-clamp2`, `.gg-wrap-any`,
  `.gg-mono-block`, `.gg-hidden` (one hidden rule; the `.hidden`/`.open`/
  inline-display trio converges here as surfaces migrate).
- Per-feature CSS may style **content only**. The acceptance sentence of this
  whole charter: *"change what a window looks like" = edit one rule in
  gadgets.css; every window in the world updates.*

## 5. Mobile

Base case stays mobile-first ("Leona's thumb"). Each gadget class declares its
small-screen behavior ONCE in gadgets.css, at the shared `640px` breakpoint
(promoted to a documented constant in gadgets.css — CSS can't var() a media
query without a preprocessor, so the rule is: the breakpoint appears only in
gadgets.css, never in feature CSS):

- window → full-width bottom sheet (the interact menu's existing mobile/desktop
  split generalizes to the class);
- panel → off-canvas drawer + FAB toggle (crossings' existing behavior becomes
  the class behavior);
- rows/badges → unchanged;
- tap-target floor (`.gg-btn` min padding) — today only the interact menu
  treats tap size explicitly; the class makes it universal.

## 6. JS structure

- **New file** `static/gadgets.js`, loaded before `app.js`. No modules, no
  build step — same flat-script idiom; `gadgets.js` defines the `gg*`
  factories + the anchor helper + the shared outside-click/Escape dismiss
  registry (replacing the 2 verbatim document-level copies with one listener
  that consults an open-gadget stack; also gives Escape a well-defined
  close-topmost order for free) + `ggLog`/`ggDebug` (the §1.5 logging seam —
  factories wire it in, so a gadget author gets lifecycle logging without
  writing any).
- **Why a second file, not an app.js section**: 24 tests grep the *served
  content* of `/app.js` — additive files can't break them; and the boundary
  makes chrome-leakage review mechanical (feature diffs touch app.js,
  system diffs touch gadgets.js/gadgets.css).
- Data flows IN via `update(data)`; factories never read feature state.
  Feature-side patterns the charter keeps and blesses: the ACTIONS
  table-driven build, closed `Object.create(null)` lookup maps,
  textContent-only + `replaceChildren()`, DOM caps (console 100 / crossings
  200).
- Non-goals for v1: no event bus, no state store — the direct-call coupling
  (`render()` closes the interact menu) is small enough to keep until practice
  says otherwise (实践出真知 — build it when it binds, not before).

### 6.1 Action outcome — how a click reports back (#957)

Lei, 2026-10-03: a successful Take looked like a dead button, because the
only success signal was a console line and the scene never re-rendered. The
fix is a composition of four pieces; a future `/act` caller composes them
rather than re-inventing any:

1. **`actPost(intent, args, logKind)`** (app.js) — the ONE `fetch("/act")`.
   Stays in app.js, not gadgets.js: it reads `state.csrfToken`, and §6 says
   factories never read feature state. Never throws; always resolves to
   `{ ok, status, data, message, succeeded }`. `ok` = the request resolved
   2xx. `succeeded` = `ok` AND the world did what was asked: a 2xx carrying
   `outcome.effect_failures`, `refused`, or `error` is resolved but NOT
   succeeded. Callers decide success from `succeeded`, never from `ok`.
2. **In-flight state.** While any `actPost` is pending, `#action-bar` has
   `aria-busy="true"` and `.act-pending`, driven by a pending COUNTER so
   overlapping calls do not clear it early.
3. **`ggStatus`** (§2) shows the outcome: `pending` when `actPost` starts,
   then `ok` or `error` from the caller, with the same text the caller logs
   to the console. One instance, attached after the action bar.
4. **`refreshScene()`** (app.js) — re-render the CURRENT place after a
   succeeded OR partially-moved outcome (a 2xx with `effect_failures`: the
   server is the truth). Not `loadScene()`: that is a place transition (it
   tears down the crossings poll). `refreshScene` leaves the status line,
   the open thing viewer and the gadget stack alone, and drops a late answer
   if the user has changed place meanwhile. A failed refresh reports "done,
   but the view could not refresh", never that the action failed. Not used
   after `say`/`talk` (the crossings poller covers them) or `move`
   (`navigateTo` already loads the scene).

`render()` honours its own stated reason for closing the interact menu ("its
target may no longer be in the new scene"): `reconcileInteractMenu(scene)`
keeps the menu, and any act-params/edit sub-view the user is typing in, while
its target thing/avatar is still in the scene (only the action list is rebuilt
from the target's fresh data), and closes it otherwise. That one rule also
covers a place change (the target is not in the new place's scene), so
`refreshScene()` can run after a refused act without destroying the form.
The menu also belongs to the identity (and viewer) that opened it: a re-login,
viewer switch or logout closes it, so a half-typed form never submits under a
different session.
`actPost`'s third parameter `logKind` is the charter's planned signature and is
accepted but not read; callers keep their own console text.

## 7. Migration policy (incremental, no freeze)

1. **New features: gadgets from day one.** #351 (window, the proving case),
   #350 (panel + text-measure vocabulary), voice play-button (button in
   crossing row), art-library UI (first full consumer).
2. **Existing surfaces: adopt when next touched.** The PR that next touches a
   legacy surface converts it. No big-bang rewrite PR, ever.
3. **Predicted cheap order** (each also deletes counted duplication): the 3
   input-rows → `ggInputRow`; interact menu buttons → `.gg-btn`; console →
   `screen`-anchored window (satisfies #351's console-closable requirement);
   crossings/inspect panels → `ggPanel` (+ `--gg-panel-w` retires the 320px
   triplet); `/act` wrapper → one `actPost(intent, args, logKind)` helper
   (JS-side pile, same charter, kills 6 copies and the 3 no-target guards)
   — **landed (#957)**, see §6.1.
4. **Done-marker** (grep-able): no `position: fixed` and no `z-index` outside
   gadgets.css; no button styling outside `.gg-btn`; app.js contains no
   createElement of frame elements outside `build*`/`gg*` factories.

## 8. Review hooks (enforceable, not aspirational)

- Reviewer-fairy briefings for webrender PRs cite this file by path; checklist
  line: *"new UI is gadgets or compositions; no chrome outside gadgets.css; no
  numeric z-index; no bare `console.*` — logging via `ggLog`; new gadget class
  ⇒ this file edited in the same diff."*
- The done-marker greps in §7.4 are runnable one-liners — a reviewer can check
  chrome leakage mechanically.

## 9. Open questions for first-readers (Ariadne, Luria)

1. **gadgets.js/gadgets.css as new files vs sections of the existing pair** —
   I hold new-files (test-safety + mechanical review boundary, §6); if either
   of you knows a serving/caching reason to prefer inline sections, say so.
2. **The `street` anchor**: I claim the cursor's offsetParent approach
   generalizes to #351's window and dissolves the interact-menu's documented
   objection to element-anchoring. Ariadne — you were deepest in #341/#342
   column code; sanity-check the re-anchor-on-render story (re-append after
   `render()` rebuilds bands).
3. **#350 wrap-marker**: no-dash v1 (pure `overflow-wrap:anywhere` via
   `.gg-wrap-any`), dash marker as follow-up. Containment ships now,
   typography later. Object if the dash is load-bearing for Lei.
4. **Chrome color unification**: keep the dark-grey chrome as `--gg-chrome-*`
   initially (visual no-op), unify with the `--gb-*` palette as a later
   one-line decision — or unify now? I hold later: migration PRs should be
   behavior-preserving so review attention stays on structure.
