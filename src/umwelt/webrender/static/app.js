// Umwelt Web Render — M0 + M1 + M2 + M3 frontend. Vanilla JS, no build step.
//
// Reads viewer/place from the URL query string (or the controls bar);
// fetches GET /scene; draws a CSS grid of things and avatars. Exits render
// as clickable buttons below the grid that re-fetch /scene at the new
// place. All untrusted (agent-authored) text (thing/avatar names, place
// label/description, crossing bodies/senders, inspect fields, `marker`) is
// placed via `textContent`, never `innerHTML` — DOM text nodes are
// auto-escaped, so no separate esc() helper is needed here (unlike the
// existing webview, which uses innerHTML and therefore needs one).
//
// M1 added: a crossings sidebar (GET /crossings, polled every ~2s per the
// design's §7 "v0 = polling" note), click-to-inspect on things (GET
// /inspect), and a brief dim/loading state on scene transitions.
//
// M2 adds: real sprites. A thing with a non-null `sprite_ref` draws an
// <img> pointing at that URL (pixelated, no smoothing) instead of the M0
// placeholder colored rectangle; a `color` tint (if set) composites over
// the sprite via a `mix-blend-mode` overlay div (not exact color science —
// "visibly different" per the design, not a perfect recolor); a `marker`
// (already server-validated to <=2 alphanumeric chars) renders as a small
// fixed-corner text overlay via `textContent`. A thing with a null
// `sprite_ref` (no render_spec, or a rejected/malformed one) falls back to
// EXACTLY M0's colored-rectangle rendering — that path is unchanged.
//
// M3 adds: the identity/write layer (umwelt#267 design charge; the write-
// layer design doc section — "DESIGN.md §12" — lands with PR #216, still
// open as of this writing, so cite the PR here rather than a section
// number that doesn't yet exist on `main`). Login as a human resident (e.g. alice) via
// POST /session/login (session cookie is httpOnly — this file never reads
// or writes it directly, only the returned csrf_token, held in the `state`
// object's memory and re-sent as the X-Csrf-Token header on every /act
// call). While logged in, clicking an exit ALSO performs a real move via
// POST /act {intent:"move", args:{place}} for your own avatar — spectator
// navigation (no login) is unchanged: it only re-fetches /scene for
// display, exactly as M0-M2 always did, per the design's "the write
// session does NOT widen reads" invariant (PR #216 §12.9). On page load,
// GET /session recovers an existing login (and its csrf_token — see
// gateway.py's session_status docstring for why that route now returns
// it) so a page reload doesn't
// silently strand you logged-in-but-uncallable.
//
// umwelt #239 adds: click-to-inspect on avatars (the M1 "avatars get no
// inspect action" deferral, closed) — reuses GET /inspect unchanged,
// keyed on the avatar's THING id (`scene.avatars[i].avatar_id`, new in
// this change), not its `agent` string. The inspect panel's status block
// is now distinct per-field rows (name/description/emotion/live state/
// last-active/activity_line) instead of a raw JSON dump.
//
// umwelt #267 "menu PR": replaces direct single-click-to-inspect on
// avatars/things with a real interact-menu (tap -> a small popup offering
// talk/examine for avatars, or per-action buttons for things read straight
// off the thing's own server-provided `.actions` — never hardcoded
// client-side). Wires the two new `/act` intents this needs, `talk`
// (-> POST /umwelt/send_crossing, destination server-resolved from the
// caller's own current place, never client-supplied) and `use` (-> POST
// /umwelt/use, covers both the "Use" and "Read" labels — same intent, same
// route, the label is purely a UI choice off the thing's `kind`). Both are
// thin proxies exactly like `move` already is: no new validation/rate-
// limiting/consent logic here, ever — see gateway.py's `/act` docstring.
// "Examine" stays wired to the existing GET /inspect (`inspectThing`,
// unchanged) — only WHEN it fires changed (menu selection, not a direct
// click). Doors are unchanged — no menu layer, `navigateTo` still fires
// directly on click. See openInteractMenuForAvatar/openInteractMenuForThing
// below.
//
// Avatar sprites shipped 2026-07-24 (world-finishing sprint) — see
// buildAvatarElement's sprite-vs-fallback split below.
// (A general text input/say bar outside the interact-menu's own talk
// sub-view shipped later, see the webrender-action-bar note further down
// this file.) The crossings sidebar is unchanged from M1.
//
// Emotion emoji bubbles (kepler #267 charge): a readable emoji bubble
// ABOVE the avatar's head (at street scale — the #256 face-badge alone is
// sub-pixel and unreadable there, per kepler's playtest report). The #256
// face-badge STAYS as the small on-avatar idle indicator (unchanged), and
// this bubble is an ADDITIONAL child: both display the same `a.emotion`
// data sourced from `scene.avatars[i].emotion` (server.py's
// `_present_summary`, same underlying data the click-to-inspect panel's
// `status.emotion` row already reads — the two can never disagree).
//
// Security invariant (non-negotiable, §12.10 — preserved by construction
// from #256/#240's own discipline): the emoji glyph is a fixed literal
// selected from the CLOSED `_EMOTION_EMOJI` map; the raw emotion string
// is ONLY a dict-lookup key, never assigned to `.textContent`/`.innerHTML`
// /any class name or attribute. The bubble's `textContent` is the
// mapped/fallback emoji, never the raw string. Unrecognized/non-string/
// absent emotions fall through to a single fixed fallback emoji (🙂) —
// simpler and safer than #256's hash-tinted fallback face (no per-string
// derivation at all). Emotion reads identically for human and agent
// avatars — same `scene.avatars[i].emotion` field, no actor-type fork
// anywhere in this path (DESIGN.md §12.1b).
//
// Avatar sprites (a real sprite pipeline for avatars, as opposed to these
// overlays — see the module-level design notes above buildFaceElement and
// buildEmojiBubble) shipped 2026-07-24 (world-finishing sprint):
// buildAvatarElement now renders scene.avatars[i].sprite_ref
// through the same sprite-wrap pattern buildThingElement uses, falling
// back to the pre-mint green circle when unset.
// (A general text input/say bar and use()/act wiring beyond `move` both
// shipped later — see the umwelt #267 "menu PR" and webrender-action-bar
// notes elsewhere in this file.) The crossings sidebar is unchanged from
// M1.
//
// P0 nav fix (umwelt #238, Lei's first playtest): the exits list now also
// renders `scene.parent` (the one direction `reachable`/`exits` structurally
// excludes — see gateway.py's `_build_scene` docstring) as an extra nav
// button, plus one ALWAYS-visible "back to the Square" button in the
// controls bar that isn't conditional on reachability at all. Login and
// session-recovery now also resolve+auto-navigate to the avatar's own
// current place (server-resolved, see gateway.py's `_resolve_avatar_place`)
// instead of requiring a manual "Enter / Look" click with a typed place id.
//
// umwelt #240 (P2 street-view FRAME, forum #267 design — Borges/Kepler):
// replaces render()'s CSS-grid-of-rectangles with a horizontal "street".
// This is the read-layer swap ONLY — no new write/mutation surface, no
// talk/use/act interact-menu (a later "menu PR"). What changed:
//   - The street (#street) is a single horizontal flex strip, left to
//     right: the back-door (scene.parent, when non-null — ALWAYS the first/
//     leftmost element so its position never varies place to place, the
//     direct fix for the #238 "stranded, no way back" class), then
//     scene.things, then scene.avatars, then scene.exits (already
//     server-sorted by label as of this change — rendered in the order
//     given, never re-sorted client-side).
//   - A banner (#street-banner) shows the place label + a present-count
//     (`scene.avatars.length`), both set via textContent.
//   - Doors (back-door and forward exits alike) are real <button>s
//     restyled as a door+sign (see index.html's `.door`/`.door-shape`/
//     `.door-sign` CSS) — clicking one still calls `navigateTo` unchanged,
//     the exact same move-then-navigate mechanism exit buttons already
//     used. `scene.cell`/`scene.footprint` positions are no longer used for
//     layout (the street is 1D, not a spatial grid) — the fields still
//     arrive in the response but this renderer ignores them.
//   - The caller's own avatar (when logged in — `state.identity` matched
//     against each avatar's `agent`) gets a `.self` CSS class (a glow/
//     outline) but stays exactly one entry within the avatars list, never a
//     separate body.
//   - No arrow-key/WASD movement — click/tap-a-sprite is the one
//     interaction model, per the design's mobile-first ("Leona's thumb")
//     requirement.
// Untouched by this change: #identity-bar, #controls (including the
// always-visible back-to-Square button), #crossings-panel, #inspect-panel/
// renderInspect(), the session/CSRF/login flow, and every existing sprite/
// fallback-color/marker rendering rule for a single thing or avatar (only
// repositioned, never restyled).
//
// Three-band street + full keyboard controls (forum #267 reply 79, Lei's
// GMUD queue item 1+2; rebanded from two lanes to three per umwelt #283,
// per team discussion with Borges/Kepler): the single #street row from the
// #240 street-view frame above splits into three stacked bands sharing one
// horizontal-scroll container (index.html's #street-sky / #street-land /
// #street-road), driven by each thing/avatar's own `lane` field
// (sky/land/road) from the gateway's /scene response — never by which
// response list (`scene.things` vs `scene.avatars`) an item arrived in.
// Doors (the back-door + scene.exits) have no `lane` field at all — they
// always render in #street-land, matching their exact pre-#283 position.
// `upperLaneItems` (the flat keyboard-navigable list, doors+things only —
// see below; umwelt #323: split by lane into `laneItems`, see that note
// further down) is built in this stable order: sky-lane things, then the
// land band's own items (back-door, then land-lane things, then exits —
// the SAME relative ordering #240 already used within that band), then
// road-lane things. This is a pure reordering/regrouping of the SAME set
// of items #278/#281 already exposed via `lane` — no new items, no
// removed items.
//
// The point of this change (unchanged from the original two-lane design)
// is full keyboard control, added on top of the existing click/tap model
// (never replacing it — every click listener above is untouched):
//   - ArrowLeft/ArrowRight move `cursorIndex`, a plain client-side view-
//     state integer indexing into `upperLaneItems` (rebuilt on every
//     render() call; umwelt #323: now indexes the screen-x-sorted
//     `allNavItems` instead — see that note further down) — no network
//     call, no world write. There is no
//     server-side intra-place position yet (umwelt #274's slot-addressing
//     design is the future real version of this); `cursorIndex` is
//     deliberately kept as ONE variable so a later slot number can replace
//     it — a swap, not a rework. No wrap at the ends — clampCursor.
//   - The logged-in viewer's own avatar (`a.agent === state.identity`) is
//     the visual "player cursor": instead of flowing normally in whichever
//     band its `lane` would otherwise route it to, it's pulled out and
//     positioned absolutely (`.avatar-cursor`, see updateCursorPosition)
//     so it visually tracks whichever upper-lane item `cursorIndex`
//     currently points at. Every OTHER avatar keeps its normal static
//     band-flex position (routed by its own `lane`, defaulting to `road`)
//     — untouched by cursor movement. (Judgment call, flagged in the PR
//     body: the spec's "your avatar IS the cursor" read is implemented
//     literally — the self avatar element itself moves — rather than a
//     separate floating marker alongside a never-moved self avatar.)
//   - Enter activates the item the cursor currently targets
//     (activateCursorTarget): a thing opens the interact-menu exactly like
//     a click would (openInteractMenuForThing); a door performs the exact
//     same move-then-navigate sequence a door click already does
//     (navigateTo); an avatar opens the SAME interact-menu a click on it
//     would (openInteractMenuForAvatar). Avatars were not reachable by
//     cursor in this original version — they were never added to
//     upperLaneItems/laneItems/allNavItems regardless of which band they
//     render in, a known v0 bound, not a bug — until the avatar-keyboard-
//     reachability change (render()'s avatar loop, activateCursorTarget)
//     closed that gap: avatars are now pushed into laneItems too, click/tap
//     interaction stays exactly as it was, unchanged.
//   - While the interact-menu is open, ArrowUp/ArrowDown move a highlighted
//     selection across its buttons (moveMenuHighlight — a CSS class; focus
//     follows the highlight too, via focusMenuHighlight)
//     and Enter activates the highlighted button
//     (activateHighlightedMenuButton) — a real `.click()`, so it reuses
//     each button's own existing click listener unchanged. Escape-to-close
//     is unchanged (see the existing keydown listener below).
//   - Text inputs ALWAYS keep their own keys — every key handler here
//     checks `ev.target` first and returns immediately for any INPUT/
//     TEXTAREA, so typing in the talk sub-view's input, the viewer field,
//     or the place field is never intercepted. The one deliberate
//     exception: Enter inside the talk sub-view's own input
//     (the composer input) is wired as a submit-equivalent (clicks
//     the existing Send button) — safe here because that field is a plain
//     single-line `<input>`, not a `<textarea>` (no multiline value Enter
//     could clobber; the Ctrl+Enter convention the spec calls for only
//     applies if this were ever changed to a textarea).
//   - `cursorIndex` persists across scene re-renders (render() calls
//     clampCursor() rather than resetting to 0) — clamped to the new
//     combined sky+land+road-things list's length so a place with fewer
//     items doesn't leave a now-out-of-range index.
// Untouched by this change: click/tap on every element (doors, things,
// avatars) — this is a keyboard layer ADDED alongside the existing mouse/
// touch model, not a replacement; the #259 console (this branch's base) —
// no changes to #console/consoleLog beyond mechanical layout coexistence
// (the console sits below #street-frame in the DOM, unaffected by the
// street's internal band split).
//
// umwelt #323 (Lei's design intent, evening-walk note): street navigation
// moves from scene-list order to SPATIAL/screen-absolute order. Before
// this change, ArrowLeft/ArrowRight walked `upperLaneItems` in the fixed
// sky-things -> land-band -> road-things append order described above —
// so leaving the last sky-lane item and pressing ArrowRight once could
// jump the cursor back to the FIRST land-lane item, regardless of either
// item's actual on-screen x position. What changed (see the "cursor
// state" block below for the implementation):
//   - `laneItems` (was the flat `upperLaneItems`) buckets the same items
//     by lane instead of one flat list, still built in the SAME sky ->
//     land -> road source order render() always used.
//   - `allNavItems`: every laneItems entry merged and sorted by each
//     element's #street-frame x via streetX() (umwelt #1132; not source order).
//     `cursorIndex` now indexes INTO allNavItems, so ArrowLeft/ArrowRight
//     walk the street in actual screen-x order, independent of lane —
//     "the avatar moves left/right on SCREEN, regardless of which lane
//     the adjacent thing is in" per the issue.
//   - `currentLane` (new): the selected interact-target lane.
//     ArrowUp/ArrowDown (new — previously a no-op when the interact-menu
//     was closed) cycle it via moveLane(delta), clamped across
//     LANE_ORDER. Does not move cursorIndex — lane and horizontal position
//     are orthogonal state, kept that way specifically so a future #274
//     slot ordinal can replace cursorIndex without touching the lane axis
//     (a swap, not a rework, per the issue's own composability
//     constraint).
//   - `currentLaneTarget()`: Enter's (activateCursorTarget) and the RPG
//     action bar's (_cursorTargetedThing) actual target — the nearest
//     item in `laneItems[currentLane]` to the current horizontal
//     position, per the issue's "nearest thing in the selected lane
//     at/near the avatar's current horizontal position". Highlighted via
//     the same pre-existing `.cursor-target` CSS class.
//   - The self-avatar cursor (updateCursorPosition) keeps tracking its
//     horizontal position off allNavItems (screen-absolute) but stays
//     vertically anchored to the road band regardless of currentLane —
//     judgment call, flagged in the PR body: the issue describes Up/Down
//     as selecting an interact-target lane, not physically relocating the
//     avatar's own sprite into the sky/land bands, so only the horizontal
//     position ever moves the avatar; which lane is selected is
//     communicated purely via the `.cursor-target` highlight.
//   - No server/gateway/db change anywhere in this slice — pure client
//     view-state, same as every keyboard-control change before it.
//
// umwelt #342 (Lei's live-walk feedback): "left/right should simply move the
// avatar's x-axis coordinate, and up/down shifts the focused lane." #323
// made ArrowLeft/ArrowRight walk allNavItems (every lane's items merged and
// sorted by offsetLeft) one ITEM at a time — so when sky and land each held
// a thing at (nearly) the same on-screen x, the cursor stopped at BOTH,
// costing two keypresses to pass one visual column. #342 groups allNavItems
// into on-screen COLUMNS (items within COLUMN_EPSILON_PX of each other's
// offsetLeft cluster together, see buildNavColumns below) and re-points
// cursorIndex at columns instead of items:
//   - `navColumns`: built once per render() (and once per moveCursor/
//     moveLane-adjacent state change is unnecessary — offsetLeft only
//     changes on a re-render), from the SAME sorted allNavItems #323 already
//     built — a clustering pass over the existing sort, not a new source of
//     truth. Each entry is `{x, items}` — `x` the representative on-screen
//     offsetLeft for the column (the FIRST item folded into it, i.e. the
//     leftmost — keeps the epsilon comparison anchored rather than drifting
//     as more items join the same column), `items` every laneItems entry
//     (across all three lanes) whose offsetLeft fell within the epsilon.
//   - `cursorIndex` (unchanged name/semantics) now indexes INTO
//     `navColumns` rather than `allNavItems` — ArrowLeft/ArrowRight
//     (moveCursor) walk columns, so two same-x items in different lanes
//     cost ONE keypress to pass, not two. `allNavItems` itself is
//     unchanged/still built (still used to clear the previous
//     `.cursor-target` highlight in updateCursorHighlight, and as
//     buildNavColumns' input) — a pure additive layer, not a replacement.
//   - `currentLaneTarget()`: now resolves within `navColumns[cursorIndex]`
//     first (the item in that column belonging to `currentLane`); when the
//     column has no item in `currentLane` (e.g. cursor is on a column that
//     only has a sky-lane thing but currentLane is "road"), falls back to
//     the item in that SAME column whose lane is nearest `currentLane` in
//     LANE_ORDER — judgment call (flagged in the PR body): #323's original
//     fallback picked the nearest item by offsetLeft distance ACROSS THE
//     WHOLE LANE (no column concept existed yet); #342 keeps the "nearest
//     available target" spirit but the natural distance metric for a
//     column-scoped fallback is LANE_ORDER position (how many bands away),
//     not offsetLeft (every item in the same column is within epsilon of
//     each other on the x-axis by construction, so x-distance is no longer
//     a meaningful tie-breaker there).
//   - `updateCursorPosition()`: the self-avatar cursor's horizontal anchor
//     is now `navColumns[cursorIndex].x` (the column's representative
//     offsetLeft) instead of reading offsetLeft off a single item —
//     "the avatar's x-axis coordinate" the issue describes is the column's
//     x, shared by every item folded into it.
//   - `COLUMN_EPSILON_PX`: half of `.item`'s CSS width (64px, index.html) —
//     items within the SAME lane are always spaced at least item-width +
//     gap (64 + 10 = 74px) apart (see render()'s per-band `gap: 10px`), so
//     an epsilon under half that pitch (32px) can never accidentally merge
//     two distinct same-lane items into one column, while still being
//     generous enough to cluster genuinely-aligned cross-lane items whose
//     offsetLeft differs only by sub-pixel/flex-rounding noise or by a
//     lane's own small alignment offset.
//   - Composability with #274 (per the issue's own framing): a column IS
//     the client-side stand-in for a future server-side slot ordinal — this
//     clustering pass is designed to be swappable for a direct
//     slot-number-into-navColumns mapping later, without touching the
//     lane axis or any other cursor-state shape.
//   - No server/gateway/db change — pure client view-state, same discipline
//     as every keyboard-control change above.

// ---------------------------------------------------------------------
// umwelt#286: passive client-side error capture (Ariadne's forum #432
// spec). There was previously NO client-side error capture at all -- a
// real-device crash left no trace beyond a user's own report. Placed as
// the FIRST executable code in this file (before every other statement
// below, including `state`/`consoleLog`/every function declaration) so
// these listeners are registered before anything they might need to
// report on has had a chance to run.
//
// Honest bound, stated once here rather than repeated at each caller
// (Ariadne's spec, verbatim): a true OS-level browser-tab kill (OOM) runs
// no JS at all and is invisible to this or any JS instrumentation. This
// only ever catches an uncaught script error or promise rejection that
// happens to look like, or cascade into, a crash on a constrained device
// -- still the half we can see, per Sol's original framing on the issue.
// "First executable code" above is true for THIS file only -- gadgets.js
// loads before app.js (index.html), so an error during gadgets.js's own
// load happens before these listeners exist and is not caught (Aleph's
// colleague-review finding, PR-848).
//
// `_lastAction` is a plain module-level variable, not an event system --
// each `act*` function below sets it to its own POST /act intent string
// immediately before sending the request (Ariadne's spec: "the most
// recent intent the UI sent"). It is read here only inside the error
// handlers, purely for diagnostic context in the report -- nothing else
// in this file reads it.
let _lastAction = null;

function _reportClientError(kind, message, stack) {
  // Handlers must NEVER throw themselves -- instrumentation that can
  // itself crash the page defeats the point (Ariadne's spec) -- so the
  // whole body is one try/catch with no re-throw and no console noise
  // beyond the existing on-page console log below.
  try {
    const payload = JSON.stringify({
      kind,
      message: String(message == null ? "" : message).slice(0, 2000),
      stack: stack ? String(stack).slice(0, 4000) : null,
      user_agent: navigator.userAgent || "",
      url: window.location.href || "",
      last_action: _lastAction,
    });
    // sendBeacon is the right transport for a fire-and-forget report from
    // inside an error handler -- it survives page unload (a crash often
    // IS a navigation-away) and never blocks. Not every browser/context
    // has it (very old browsers, some sandboxed embeds), so fall back to
    // a keepalive fetch that swallows its own failure -- either way the
    // response is never read; both are pure fire-and-forget.
    //
    // Send the payload as a PLAIN STRING, not a Blob with an explicit
    // "application/json" type (Aleph's colleague-review finding, PR-848):
    // sendBeacon restricts a Blob's `type` to the CORS-safelisted content
    // types, and Chrome has thrown a SecurityError for a non-safelisted
    // Blob type on sendBeacon (e.g. Chrome 80, cypress#7115) -- which this
    // function's own try/catch would swallow, losing the report entirely
    // with no trace. A bare string sent via sendBeacon defaults to
    // "text/plain;charset=UTF-8", which IS safelisted. The server parses
    // it with get_json(force=True, ...) specifically to accept that
    // Content-Type (see gateway.py's debug_client_error).
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/debug/client_error", payload);
    } else {
      fetch("/debug/client_error", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
        keepalive: true,
      }).catch(() => {});
    }
  } catch (e) {
    // Deliberately swallowed -- see the function-level comment above.
  }
  // consoleLog runs AFTER the report is sent, in its OWN try/catch
  // (Aleph's colleague-review finding, PR-848): consoleLog is a hoisted
  // function declaration but reads module-level `const`s
  // (consoleLogEl/_CONSOLE_MAX_ENTRIES) defined further down this file --
  // an error thrown during this file's own top-level startup, before
  // those consts have run, would make consoleLog itself throw a
  // ReferenceError. Calling it first (the original order) meant that
  // ReferenceError aborted the whole function before the report was ever
  // built or sent -- exactly the startup-crash case #286 most wants
  // visibility into. Splitting it into its own try means a failed
  // console-log never costs the report.
  try {
    if (typeof consoleLog === "function") {
      consoleLog("error", `${kind}: ${message}`);
    }
  } catch (e) {
    // Deliberately swallowed, same reasoning as above.
  }
}

window.addEventListener("error", (ev) => {
  _reportClientError(
    "error",
    ev.message || (ev.error && ev.error.message) || "",
    ev.error && ev.error.stack,
  );
});

window.addEventListener("unhandledrejection", (ev) => {
  const reason = ev.reason;
  const message =
    reason && reason.message ? reason.message : String(reason);
  const stack = reason && reason.stack ? reason.stack : null;
  _reportClientError("unhandledrejection", message, stack);
});

function qs(name) {
  return new URLSearchParams(window.location.search).get(name);
}

const state = {
  viewer: qs("viewer") || "",
  place: qs("place") || "",
  // M3: identity/write state. `identity` is "spectator" or a roster avatar
  // name; `csrfToken` is only ever set alongside a real identity (null
  // whenever identity is "spectator") — every /act call asserts both are
  // present before sending, so the two can never drift out of sync.
  identity: "spectator",
  csrfToken: null,
  // umwelt#495: "verified" or "unverified" (or null while spectator),
  // copied verbatim from the world's own three-state login rule via
  // /session/login or GET /session -- never re-derived client-side.
  // Read by renderIdentityBar() to keep the anti-silent-fail-open badge
  // live for the whole session, not just a one-time login toast.
  authState: null,
  // umwelt #936: the one selected thing, `{id, name, kind, where}` or null.
  // NEVER assigned directly -- `selectThing` is its only writer (a static
  // test pins this). `where` is "street" | "container:<name>" | "inventory"
  // | "pasted": display only, never trusted.
  selection: null,
};

const viewerInput = document.getElementById("viewer");
const placeInput = document.getElementById("place");
const goBtn = document.getElementById("go");
const errorEl = document.getElementById("error");

// umwelt #301: crossings-panel drawer toggle (mobile only — the button and
// the .open transform both no-op above the 640px breakpoint, see
// index.html's CSS). No hidden/open persistence across page loads by
// design — a fresh session always starts with the drawer closed, same
// posture as every other panel here (interact-menu, inspect-panel).
const crossingsPanelEl = document.getElementById("crossings-panel");
const crossingsToggleEl = document.getElementById("crossings-toggle");
crossingsToggleEl.addEventListener("click", () => {
  crossingsPanelEl.classList.toggle("open");
});
// umwelt #240: the street (horizontal road strip) replaces the old #grid.
const streetEl = document.getElementById("street");
// umwelt #283: the shared scroll container's three stacked bands, driven by
// each thing/avatar's own `lane` field (sky/land/road) from the gateway's
// /scene response — replaces the old #street-upper/#street-lower two-lane
// split (see this file's module-header note, "Three-band street").
const streetSkyEl = document.getElementById("street-sky");
const streetLandEl = document.getElementById("street-land");
const streetRoadEl = document.getElementById("street-road");

// ---------------------------------------------------------------------
// Three-band street + full keyboard controls: cursor state. See this
// file's module-header note (search "Three-band street + full keyboard
// controls") for the full design.
// ---------------------------------------------------------------------

// umwelt #323: absolute-horizontal movement + lane selection replaces the
// pre-#323 scene-list-order cursor. `laneItems` (was the flat, single-list
// `upperLaneItems`) buckets keyboard-navigable elements by their own lane —
// `{sky: [...], land: [...], road: [...]}`, each entry still the same
// `{el, kind: "door"|"thing"|"avatar", data}` shape `upperLaneItems` used
// (avatar was added to the `kind` union later, closing the street-M4
// avatar-keyboard-reachability gap — see render()'s avatar loop below;
// door/thing were always here), still built inside render() in the SAME
// sky -> land(door+things+exits) -> road source order as before (see
// render() below — this is a re-bucketing of the existing push calls, not
// a reordering; test_app_js_upper_lane_items_ordered_sky_then_land_then_road
// still holds unchanged).
let laneItems = { sky: [], land: [], road: [] };
// allNavItems: every laneItems entry merged into one list and sorted by
// each element's #street-frame x via streetX() (umwelt #1132; post-layout,
// not source/append order) — built once at the end of render(), after
// every item is already appended to the DOM (offsetLeft forces a layout
// flush if needed, same reliance the pre-#323 updateCursorPosition already
// had on this property). `cursorIndex` (unchanged name/semantics: a plain
// client view-state integer, no network call, no world write — umwelt
// #274's future slot-addressing design is the real server-side version of
// this; kept as ONE variable specifically so a slot number can replace it
// later, a swap not a rework) now indexes INTO allNavItems, so
// ArrowLeft/ArrowRight walk the street in actual screen-x order,
// independent of which lane an item lives in — "the avatar moves
// left/right on SCREEN, regardless of which lane the adjacent thing is
// in" per the issue.
let allNavItems = [];
// umwelt #342: COLUMN_EPSILON_PX — the on-screen-x proximity threshold
// buildNavColumns (below) uses to cluster allNavItems into columns. See the
// module-header "umwelt #342" note for the full rationale; short version:
// half of `.item`'s CSS width (64px), safely under half the minimum
// same-lane item pitch (item width + gap = 74px, index.html), so it can
// never fold two distinct same-lane items together while still catching
// genuinely-aligned cross-lane items.
const COLUMN_EPSILON_PX = 32;
// umwelt #342: navColumns — allNavItems clustered into on-screen columns by
// COLUMN_EPSILON_PX proximity, `{x, items}` per column (see buildNavColumns
// and the module-header note). `cursorIndex` (below) indexes INTO this list
// now, not allNavItems directly — ArrowLeft/ArrowRight walk columns, so two
// same-x items in different lanes cost ONE keypress, not two.
let navColumns = [];
// buildNavColumns: pure function over a screen-x-sorted item list (the same
// shape/order allNavItems already has) — kept standalone (no closure over
// module state beyond the epsilon constant) so it's a clean drop-in target
// for a future #274 slot-ordinal replacement (module-header "umwelt #342"
// composability note), and independently testable.
// umwelt #1132: streetX(el) — `el`'s left offset in #street's coordinate
// frame, the frame updateCursorPosition writes selfCursorEl.style.left in.
// Raw offsetLeft is relative to el.offsetParent, and that differs by band:
// sky/land items' offsetParent is #street, but road items' is #street-road
// (position: relative, index.html), which itself sits `padding-left` px in
// from #street's edge. Walk offsetParent up to #street adding each
// intermediate parent's offsetLeft (never #street's own). Bounded: an
// element not inside #street gets its plain offsetLeft.
function streetX(el) {
  let x = el.offsetLeft;
  let p = el.offsetParent;
  while (p && p !== streetEl) {
    x += p.offsetLeft;
    p = p.offsetParent;
  }
  return p === streetEl ? x : el.offsetLeft;
}
function buildNavColumns(sortedItems) {
  const columns = [];
  for (const item of sortedItems) {
    const x = streetX(item.el);
    const last = columns[columns.length - 1];
    if (last && Math.abs(x - last.x) <= COLUMN_EPSILON_PX) {
      last.items.push(item);
    } else {
      columns.push({ x, items: [item] });
    }
  }
  return columns;
}
// cursorIndex persists across render() calls (clampCursor(), never reset
// to 0) so a scene re-render (e.g. after moving through a door) doesn't
// discard the player's horizontal position in the new place. umwelt #342:
// now indexes navColumns (was allNavItems directly) — see above.
let cursorIndex = 0;
// currentLane: "sky" | "land" | "road" — the selected INTERACT-TARGET
// lane (umwelt #323's Up/Down axis), independent of cursorIndex/horizontal
// position. Also persists across render() calls, same discipline as
// cursorIndex. Judgment call (flagged in the PR body): the issue's own
// wording is "choose the interact-target lane (sky vs land)", but
// road-lane THINGS were already keyboard-reachable pre-#323 (only avatars
// were excluded at the time — see the road-lane loop in render() below) —
// dropping that reachability would be an unrequested regression, so all
// three THING lanes stay selectable via LANE_ORDER below. Avatars are now
// ALSO included in every lane's laneItems (render()'s avatar loop below) —
// closing the street-M4 avatar status/talk keyboard-reachability gap
// (DESIGN.md §12.5) — routed through the SAME openInteractMenuForAvatar a
// click already uses, not a parallel implementation.
let currentLane = "land";
// LANE_ORDER: top-to-bottom band order (matches the sky/land/road visual
// stacking) — ArrowUp moves toward the front of this list (sky), ArrowDown
// toward the back (road), clamped (no wrap) same as moveCursor below.
const LANE_ORDER = ["sky", "land", "road"];
// selfCursorEl: the logged-in viewer's own avatar element, when present in
// the current scene — null otherwise (spectator, or your avatar isn't at
// this place right now). Rebuilt fresh each render() call, alongside
// laneItems/allNavItems.
let selfCursorEl = null;

// No wrap-around at the street's ends — clamp instead (a street has ends;
// wrapping would teleport the cursor from one end to the other).
function clampCursor() {
  if (!navColumns.length) {
    cursorIndex = 0;
    return;
  }
  cursorIndex = Math.max(0, Math.min(navColumns.length - 1, cursorIndex));
}

function moveCursor(delta) {
  if (!navColumns.length) return;
  cursorIndex = Math.max(
    0, Math.min(navColumns.length - 1, cursorIndex + delta));
  updateCursorHighlight();
}

// umwelt #323: ArrowUp/ArrowDown — cycles currentLane across LANE_ORDER,
// clamped (no wrap at either end, same discipline as moveCursor). Does NOT
// touch cursorIndex — the horizontal position and the selected lane are
// two orthogonal pieces of state (per the issue's #274-composability
// constraint), so changing lane never moves the avatar's own screen
// position, only which item is highlighted as the interact target.
function moveLane(delta) {
  const idx = LANE_ORDER.indexOf(currentLane);
  const next = Math.max(0, Math.min(LANE_ORDER.length - 1, idx + delta));
  currentLane = LANE_ORDER[next];
  updateCursorHighlight();
}

// The actual interact target: umwelt #342 — the item in the CURRENT COLUMN
// (`navColumns[cursorIndex]`) belonging to `currentLane`, a direct hit
// whenever the selected column has an item in that lane; when it doesn't,
// falls back to the item in that SAME column whose lane is nearest
// `currentLane` in LANE_ORDER (module-header "umwelt #342" note explains
// why LANE_ORDER distance, not offsetLeft, is the right fallback metric
// once items are column-scoped). Null when there's nothing on the street at
// all, or (defensively) an empty column. Falls back to `laneItems[
// currentLane]`'s first item when navColumns hasn't been built yet (empty
// street / pre-render edge case), same "nothing to target" spirit #323
// used before columns existed.
function currentLaneTarget() {
  if (!navColumns.length) {
    const items = laneItems[currentLane];
    return items && items.length ? items[0] : null;
  }
  const column = navColumns[cursorIndex];
  if (!column || !column.items.length) return null;
  const direct = column.items.find((item) => item.lane === currentLane);
  if (direct) return direct;
  const curIdx = LANE_ORDER.indexOf(currentLane);
  let best = null;
  let bestDist = Infinity;
  // Tie-break (reviewer-fairy note, PR #346): strict `<` means the FIRST
  // item encountered at a given LANE_ORDER distance wins. column.items
  // preserves allNavItems' order, which pushes sky before land before road
  // at equal x (render() builds laneItems in LANE_ORDER) — so on a
  // sky-vs-road tie around `land`, sky wins, deterministically.
  for (const item of column.items) {
    const dist = Math.abs(LANE_ORDER.indexOf(item.lane) - curIdx);
    if (dist < bestDist) {
      best = item;
      bestDist = dist;
    }
  }
  return best;
}

// Highlights the item currentLaneTarget() currently resolves to (so
// Enter's target is unambiguous), scrolls it into view, and repositions
// the self-avatar cursor marker (updateCursorPosition) to match.
function updateCursorHighlight() {
  for (const item of allNavItems) {
    item.el.classList.remove("cursor-target");
  }
  const target = currentLaneTarget();
  if (target) {
    target.el.classList.add("cursor-target");
    // Auto-scroll the shared #street container so the target (and the
    // player cursor tracking it) stays in view when moving.
    target.el.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  updateCursorPosition();
  updateAvatarOverlaps();
  // GADGETS.md §3: "re-anchoring on street re-render is the render loop's
  // job (re-append after render() rebuilds bands), not per-gadget
  // listeners" — this is the one spot render() already re-anchors the
  // player cursor (updateCursorPosition, just above) after every scene
  // rebuild, so any open `street`-anchored gadget (umwelt #351's outcome
  // window) re-anchors alongside it, same discipline, same call site.
  // Also fires on every keyboard cursor move (updateCursorHighlight's
  // other caller, moveCursor/moveLane) — harmless/idempotent (cheap
  // offsetLeft/offsetTop recompute), and keeps an open street gadget
  // accurate through keyboard nav too, not just scene reloads.
  ggReanchorStreetGadgets();
}

// Positions the self-avatar cursor element (if the viewer's own avatar is
// present in this scene) so it visually tracks the current horizontal
// stop — "walks left/right... horizontally aligned under" whichever item
// (in ANY lane) allNavItems[cursorIndex] points at, per umwelt #323's
// absolute-horizontal-movement requirement. `selfCursorEl` is positioned
// in `#street`'s frame (render() appends it to streetEl), and column.x is
// measured in that same frame by streetX() — a road item's own
// offsetParent is `#street-road` (position: relative), so its raw
// offsetLeft is NOT in #street's frame (umwelt #1132). `#street` has
// `position: relative` (index.html) specifically so this coordinate math
// holds regardless of the street's own scroll position (offsetLeft/
// offsetTop are unaffected by scrollLeft/scrollTop on an ancestor).
//
// umwelt #323 judgment call (flagged in the PR body): the vertical anchor
// stays `streetRoadEl` regardless of `currentLane` — the issue describes
// Up/Down as selecting an INTERACT-target lane, not physically relocating
// the avatar's own sprite into the sky/land bands, so only the horizontal
// (x) position ever moves the avatar; which lane is selected is
// communicated purely via the `.cursor-target` highlight above, never by
// moving the avatar itself out of the road band.
function updateCursorPosition() {
  if (!selfCursorEl) return;
  if (!navColumns.length) {
    // Nothing on the street: the cursor has nowhere to point, but the self
    // avatar must stay VISIBLE (pre-two-lane behavior: self always
    // rendered). Park it at the road band's left edge instead of hiding.
    selfCursorEl.style.left = "0px";
    selfCursorEl.style.top = `${streetRoadEl.offsetTop
      + (streetRoadEl.clientHeight - selfCursorEl.offsetHeight) / 2}px`;
    return;
  }
  // umwelt #342: anchor to the COLUMN's representative x, not a single
  // item's offsetLeft — "the avatar's x-axis coordinate" the issue
  // describes is shared by every item folded into the current column.
  // A laneItems entry is a `.item` element for things AND avatars — both
  // share the SAME `.item` CSS class/width (64px, index.html;
  // buildAvatarElement sets `el.className = "item avatar"`, same as
  // buildThingElement's `"item thing"`) — or a `.door` button for doors (a
  // separate, narrower class). Avatars joining laneItems (closing the
  // street-M4 keyboard-reachability gap) doesn't change this: the first
  // item's offsetWidth stays a safe, representative centering width for
  // the whole column whenever a thing or avatar leads it, exactly as it
  // did when only things could.
  const column = navColumns[cursorIndex];
  const anchorWidth = column.items[0].el.offsetWidth;
  const left = column.x + (anchorWidth - selfCursorEl.offsetWidth) / 2;
  const top = streetRoadEl.offsetTop
    + (streetRoadEl.clientHeight - selfCursorEl.offsetHeight) / 2;
  selfCursorEl.style.left = `${left}px`;
  selfCursorEl.style.top = `${top}px`;
}

// umwelt #275: overlap detection for the self-cursor avatar vs. every
// other avatar in the road band (umwelt #283: `#street-road`, was
// `#street-lower` pre-rebanding — same element role, avatars still live
// there by default). There is no occupancy/collision-blocking logic
// anywhere in this codebase to remove (clampCursor/moveCursor/
// activateCursorTarget above are all cursor/menu state, never avatar
// position) — when the Square gets busy, updateCursorPosition's computed
// self-cursor position can legitimately land on top of one or more other
// avatars, same-size opaque circles piling up in a way that reads as
// "stuck", not "passing through". Lei's #275 scoping: "for now, just
// allow multiple avatars to stand and go across each other is a quick
// fix" — a rendering-legibility fix only, no server/db/gateway change, no
// exclusive-use-of-things mechanism. Called from updateCursorHighlight
// (below), which runs on both moveCursor's keyboard path and render()'s
// scene-rebuild path, so the overlap read stays correct as the self-
// cursor walks the street and as avatars appear/leave.
function updateAvatarOverlaps() {
  if (!selfCursorEl) return;
  const selfRect = selfCursorEl.getBoundingClientRect();
  let anyOverlap = false;
  for (const el of streetRoadEl.children) {
    const rect = el.getBoundingClientRect();
    const overlaps = rect.left < selfRect.right && rect.right > selfRect.left
      && rect.top < selfRect.bottom && rect.bottom > selfRect.top;
    // classList.toggle with an explicit force value is a no-op when the
    // class is already in that state — no removal+re-add cycle, so no
    // spurious CSS-transition restart / flicker on a render where the
    // overlap state hasn't actually changed.
    el.classList.toggle("avatar-overlapped", overlaps);
    if (overlaps) anyOverlap = true;
  }
  selfCursorEl.classList.toggle("avatar-overlapping", anyOverlap);
}

// Enter — the click-equivalent for currentLaneTarget()'s resolved item. A
// thing opens the interact-menu (openInteractMenuForThing); a door
// performs the exact same move-then-navigate sequence a door click already
// does (navigateTo); an avatar opens the SAME interact-menu a click on it
// would (openInteractMenuForAvatar — reused unchanged, not a parallel
// implementation). Avatars are now reachable this way (they're added to
// laneItems/allNavItems in render()'s avatar loop above, regardless of
// which band their `lane` routes them to) — closes the street-M4 "avatar
// status/talk is click/tap only" gap (DESIGN.md §12.5); the click/tap path
// itself is untouched. An empty selected lane at the current position (or
// an entirely empty street) means Enter is a no-op — same "nothing to
// activate" spirit as the pre-#323 empty-list guard.
function activateCursorTarget() {
  const target = currentLaneTarget();
  if (!target) return;
  if (target.kind === "thing") {
    openInteractMenuForThing(target.data);
  } else if (target.kind === "door") {
    navigateTo(target.data);
  } else if (target.kind === "avatar") {
    openInteractMenuForAvatar(target.data);
  }
}
const presentCountEl = document.getElementById("present-count");
const identityStatusEl = document.getElementById("identity-status");
const identityLoginControlsEl = document.getElementById("identity-login-controls");
const logoutBtnEl = document.getElementById("logout-btn");
const avatarPickerBtnEl = document.getElementById("avatar-picker-btn");
// umwelt #384: the login-gate view (index.html) -- see setGateActive() below.
const loginGateErrorEl = document.getElementById("login-gate-error");
// umwelt#495: username/password login form, replacing the old hardcoded
// three-name roster buttons.
const loginGateFormEl = document.getElementById("login-gate-form");
const loginUsernameEl = document.getElementById("login-username");
const loginPasswordEl = document.getElementById("login-password");
// umwelt#495 anti-silent-fail-open badge, lives in #identity-bar (stays
// visible for the life of an unverified session, not just at login).
const identityUnverifiedBadgeEl = document.getElementById(
  "identity-unverified-badge");

// ---------------------------------------------------------------------
// umwelt #259: bottom output console. actUse's response body used to be
// silently discarded on success — the human saw zero feedback ("Use does
// nothing"). This logs use/talk/move outcomes and errors so the human
// sees the same effect-return values agents see. Same no-innerHTML
// discipline as the rest of this file: every untrusted string (briefing
// text, stdout embedded in `detail`, etc.) is placed via textContent,
// never string-concatenated into markup.
// ---------------------------------------------------------------------

const consoleLogEl = document.getElementById("console-log");
const consoleClearBtn = document.getElementById("console-clear");
const _CONSOLE_MAX_ENTRIES = 100;

function _pad2(n) {
  return String(n).padStart(2, "0");
}

// consoleLog(kind, text, detail) — appends one entry: `HH:MM:SS` + a short
// kind tag (`use`/`talk`/`move`/`error`) + text. `detail` (optional; the
// full raw response object) renders as a collapsed <details><pre> with
// JSON.stringify(detail, null, 2) — collapsed by default so a `use`
// outcome's raw exit_code/stdout/world_diff fields don't dominate the log,
// but are one click away. Caps at 100 entries (drops the oldest DOM node)
// and auto-scrolls to the newest entry.
function consoleLog(kind, text, detail) {
  const entry = document.createElement("div");
  entry.className = `console-entry console-${kind}`;

  const now = new Date();
  const ts = document.createElement("span");
  ts.className = "console-ts";
  ts.textContent =
    `${_pad2(now.getHours())}:${_pad2(now.getMinutes())}:${_pad2(now.getSeconds())}`;
  entry.appendChild(ts);

  const kindEl = document.createElement("span");
  kindEl.className = "console-kind";
  kindEl.textContent = `[${kind}]`;
  entry.appendChild(kindEl);

  const textEl = document.createElement("span");
  textEl.className = "console-text";
  textEl.textContent = text;
  entry.appendChild(textEl);

  if (detail !== undefined && detail !== null) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "detail";
    details.appendChild(summary);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(detail, null, 2);
    details.appendChild(pre);
    entry.appendChild(details);
  }

  consoleLogEl.appendChild(entry);
  while (consoleLogEl.children.length > _CONSOLE_MAX_ENTRIES) {
    consoleLogEl.removeChild(consoleLogEl.firstChild);
  }
  consoleLogEl.scrollTop = consoleLogEl.scrollHeight;
}

consoleClearBtn.addEventListener("click", () => {
  // Borges's PR #260 review nit: replaceChildren() over innerHTML = "" —
  // functionally identical here (both just clear the log; there is no
  // untrusted-content risk either way since neither is fed a string built
  // from dynamic data), but replaceChildren() is the DOM-native "clear
  // this element's children" call and keeps this file off innerHTML
  // entirely, matching its own no-innerHTML module-header discipline.
  consoleLogEl.replaceChildren();
});

// ---------------------------------------------------------------------
// umwelt #351 (GADGETS.md §7.3): the console becomes closable — one
// button to open it (#console-toggle, near the action bar), an X on the
// corner to close it (#console-close), default CLOSED on load. #console
// itself already carries `.gg-window.gg-window--screen` in index.html's
// static markup (see that element's own CSS-block comment for why this
// isn't built via the `ggWindow()` factory directly) — this wiring gives
// it the SAME open/close/shared-dismiss-registry behavior a factory-built
// instance would get, via gadgets.js's ggMarkGadgetOpen/ggMarkGadgetClosed
// (the same stack #351's outcome window registers into via ggWindow()
// itself), so Escape/outside-click closes whichever of the two gadgets is
// topmost. Also logs through gadgets.js's ggLog (GADGETS.md §1 obligation
// 5) with the same `[gg:window#console]` shape a factory-built instance
// would get — a stable string id rather than the numeric counter
// ggWindow() assigns, since there's only ever one console.
// ---------------------------------------------------------------------

const consoleEl = document.getElementById("console");
const consoleToggleEl = document.getElementById("console-toggle");
const consoleCloseEl = document.getElementById("console-close");

const _consoleGadget = {
  el: consoleEl,
  close() {
    consoleEl.classList.add("gg-hidden");
    ggMarkGadgetClosed(_consoleGadget);
    ggLog("window", "console", "debug", "close");
  },
  open() {
    consoleEl.classList.remove("gg-hidden");
    ggMarkGadgetOpen(_consoleGadget);
    ggLog("window", "console", "debug", "open");
  },
};

consoleToggleEl.addEventListener("click", (ev) => {
  // Reviewer-fairy catch (PR #355): #console-toggle lives OUTSIDE #console's
  // own DOM subtree (it's a sibling near the action bar, not a titlebar
  // child), so without stopPropagation() this same click event continues
  // bubbling to gadgets.js's document-level outside-click listener right
  // after open() pushes the console onto the dismiss stack — that listener
  // sees the click's target isn't contained within #console and immediately
  // closes it again, so every click looked like a no-op. The close button
  // (#console-close) doesn't need this: it's a titlebar child, so its click
  // target IS contained within #console and the outside-click listener
  // already leaves it alone.
  ev.stopPropagation();
  if (consoleEl.classList.contains("gg-hidden")) {
    _consoleGadget.open();
  } else {
    _consoleGadget.close();
  }
});

consoleCloseEl.addEventListener("click", () => _consoleGadget.close());

viewerInput.value = state.viewer;
placeInput.value = state.place;

goBtn.addEventListener("click", () => {
  state.viewer = viewerInput.value.trim();
  state.place = placeInput.value.trim();
  loadScene();
});

// ---------------------------------------------------------------------
// M3: identity — login/logout, and recovering an existing session on load.
// ---------------------------------------------------------------------

function renderIdentityBar() {
  const loggedIn = state.identity !== "spectator";
  identityStatusEl.textContent = loggedIn
    ? `acting as ${state.identity}` : "spectator";
  identityStatusEl.className = loggedIn
    ? "identity-active" : "identity-spectator";
  identityLoginControlsEl.classList.toggle("hidden", loggedIn);
  logoutBtnEl.classList.toggle("hidden", !loggedIn);
  avatarPickerBtnEl.classList.toggle("hidden", !loggedIn);
  // umwelt#495 anti-silent-fail-open: stays live for the whole session,
  // re-derived on every render (login, page-reload recovery, logout) from
  // state.authState rather than set once and left to go stale.
  const unverified = loggedIn && state.authState === "unverified";
  identityUnverifiedBadgeEl.classList.toggle("shown", unverified);
  if (unverified) {
    identityUnverifiedBadgeEl.textContent = "unverified";
    identityUnverifiedBadgeEl.title =
      `${state.identity} has no password set -- this session was not ` +
      `credential-checked. Any admitted private-network caller can currently log in as ` +
      `${state.identity} the same way. See "forgot password?" on the ` +
      `login page to set one.`;
  }
  // umwelt#529: same loggedIn-gated visibility as every other identity-bar
  // control -- a spectator has no avatar for anything to be addressed to.
  inboxBellBtnEl.classList.toggle("hidden", !loggedIn);
}

// umwelt #384 (Lei re-scope): a logged-out session renders the login gate
// ONLY -- no street view/action bar/crossings polling behind it (the
// original bug: the full UI rendered anyway, with the backend's correct
// "not logged in" error landing invisibly at the bottom of the page).
// index.html's body.gate-active CSS does the actual hiding/showing; this
// just flips that one class and makes sure polling can't keep running
// behind a gate that just went up.
function setGateActive(active) {
  document.body.classList.toggle("gate-active", active);
  if (active) {
    stopCrossingsPolling();
    stopInboxPolling(); // umwelt#529: no avatar behind the gate, no inbox to poll
  } else {
    loginGateErrorEl.textContent = "";
  }
}

async function refreshSessionStatus() {
  // Called once on page load. Recovers an existing login (cookie survives
  // a reload; this file's in-memory csrfToken does not) — see
  // gateway.py's session_status docstring for why GET /session now
  // returns csrf_token alongside identity.
  let resp;
  try {
    resp = await fetch("/session");
  } catch (e) {
    return; // network hiccup on load — stay spectator, not fatal
  }
  if (!resp.ok) return;
  const data = await resp.json();
  state.identity = data.identity || "spectator";
  state.csrfToken = state.identity !== "spectator" ? (data.csrf_token || null) : null;
  // umwelt#495: same anti-silent-fail-open carry-forward as login() --
  // a page reload must not silently drop the unverified badge (GET
  // /session now returns `state` too, for exactly this parity reason).
  state.authState = state.identity !== "spectator" ? (data.state || null) : null;
  if (state.identity !== "spectator" && !state.viewer) {
    // A recovered/fresh login with no explicit ?viewer= in the URL:
    // default the display-viewer label to your own identity. Purely a
    // display convenience — /scene's `viewer` param is never forwarded to
    // the world server (see loadScene's docstring below), so this changes
    // nothing about what's readable, only whose name labels the render.
    state.viewer = state.identity;
    viewerInput.value = state.viewer;
  }
  // P0 nav (umwelt #238): a recovered session also resolves+returns the
  // avatar's own current place (see gateway.py's session_status docstring)
  // — pick it up UNLESS a human deliberately linked to a specific place via
  // ?place=, which state.place already holds from qs("place") at init.
  if (data.place && !state.place) {
    state.place = data.place;
    placeInput.value = state.place;
  }
  renderIdentityBar();
  // umwelt#529: a recovered session (cookie survived reload) still has an
  // avatar things can be addressed to -- same "polling starts wherever a
  // real identity exists" posture as crossings polling, just gated on a
  // different signal (login events vs. loadScene's per-place calls).
  if (state.identity !== "spectator") startInboxPolling();
}

async function login(username, password) {
  // umwelt #384: login is only ever reachable from the gate now, so its
  // errors render there (loginGateErrorEl), not #error at the bottom of a
  // page the gate is hiding.
  loginGateErrorEl.textContent = "";
  let resp;
  try {
    resp = await fetch("/session/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
  } catch (e) {
    loginGateErrorEl.textContent = `login failed: ${e}`;
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    // umwelt#495: the world (and the gateway's kind gate) return an
    // IDENTICAL message for "unknown username", "wrong password", and
    // "that's an agent, not a human" -- rendered verbatim, unchanged, on
    // purpose. Do NOT special-case any of these into a friendlier
    // per-case string here; that would undo the anti-enumeration finding
    // (forum #315) the backend specifically pays a uniform-401 cost for.
    loginGateErrorEl.textContent = body.error || `login error ${resp.status}`;
    loginPasswordEl.value = ""; // never leave a rejected password sitting in the field
    return;
  }
  const data = await resp.json();
  state.identity = data.identity;
  state.csrfToken = data.csrf_token;
  // Reviewer-fairy finding on PR #605: the failure path (above) cleared
  // this, but success left the typed password sitting in the DOM input
  // indefinitely -- clear it here too, not just on rejection.
  loginPasswordEl.value = "";
  // umwelt#495 anti-silent-fail-open: copied verbatim from the world's
  // three-state rule, never re-derived here. renderIdentityBar() below
  // is what actually surfaces it (badge stays live for the session, not
  // a one-time toast at login).
  state.authState = data.state || null;
  if (!state.viewer) {
    state.viewer = state.identity;
    viewerInput.value = state.viewer;
  }
  // P0 nav (umwelt #238): login now resolves+returns the avatar's own
  // current place server-side (see gateway.py's session_login docstring) —
  // navigate straight there instead of requiring a separate manual
  // "Enter / Look" click.
  state.place = data.place;
  placeInput.value = state.place;
  renderIdentityBar();
  setGateActive(false); // umwelt #384: tear down the gate, enter the world
  loadScene();
  startInboxPolling(); // umwelt#529: fresh login -- (re-)seed from this avatar's own cursor
}

async function logout() {
  try {
    await fetch("/session/logout", { method: "POST" });
  } catch (e) {
    // best-effort — clear local state either way so the UI doesn't get
    // stuck claiming a login the user just asked to end
  }
  state.identity = "spectator";
  state.csrfToken = null;
  state.authState = null;
  closeInteractMenu(); // umwelt #957: the menu belongs to the identity that opened it
  selectThing(null); // umwelt #936: so does the selection
  loginPasswordEl.value = ""; // don't leave a used password in the field when the gate reappears
  renderIdentityBar();
  setGateActive(true); // umwelt #384: back to the login gate, stop polling
  stopInboxPolling(); // umwelt#529: belt-and-suspenders -- setGateActive(true) already does this
  inboxUnseenCount = 0;
  inboxUnseenHasWake = false;
  // umwelt#787 part 2 follow-up: without this, a stale panel from the
  // outgoing avatar can still be on screen after switching avatars --
  // "Mark all read" there would write a boundary derived from THIS
  // avatar's data under the NEXT avatar's storage key (inboxReadSeqKey()
  // always reads the CURRENT state.identity). Reset all three so no stale
  // data/boundary/toggle survives the switch; startInboxPolling() already
  // re-seeds inboxReadSeq per avatar on the next login -- this just makes
  // sure nothing stale is readable in the gap before that happens.
  inboxLastData = null;
  inboxReadSeq = null;
  inboxShowAll = false;
  if (inboxWindow) inboxWindow.close();
  renderInboxBell();
}

// umwelt#495: username/password form submit, replacing the old hardcoded
// three-name roster buttons. preventDefault so a plain HTML form submit
// (which would navigate/reload) never fires -- login() does the fetch.
loginGateFormEl.addEventListener("submit", (e) => {
  e.preventDefault();
  const username = loginUsernameEl.value.trim();
  const password = loginPasswordEl.value;
  if (!username) return; // `required` on the input already covers this
  login(username, password);
});
logoutBtnEl.addEventListener("click", logout);

// ---------------------------------------------------------------------
// Avatar picker (2026-07-27, Lei's ask): browse already-approved avatar
// sprites and apply one to your own avatar via the `customize_avatar`
// intent (gateway.py). Gallery-pick ONLY — a "generate something custom"
// section is a deliberate follow-up, not built here: it needs a family
// fast-path around the normal 1-peer-approval moderation gate, and that
// bypass mechanism is pending Borges's security-lane read (his design has
// a hard rule against a second unguarded path to `live` status) rather
// than something to invent unilaterally. This ships the safe half today.
//
// A window + a grid of buttons is exactly GADGETS.md §2's composition
// rule ("the art-library browse UI is a window + rows -- no new class"):
// ggWindow for the frame, plain `.gg-btn` buttons for each pick — no new
// gadget class needed.
// ---------------------------------------------------------------------

let avatarPickerWin = null;

function buildAvatarGalleryContent(entries) {
  const wrap = document.createElement("div");
  wrap.className = "avatar-picker-grid";
  if (!entries.length) {
    const empty = document.createElement("p");
    empty.textContent = "No avatar sprites approved yet.";
    wrap.appendChild(empty);
    return wrap;
  }
  for (const entry of entries) {
    const card = document.createElement("div");
    card.className = "avatar-picker-card";

    const img = document.createElement("img");
    img.src = `/sprite/${entry.spec_key}`;
    img.alt = entry.label || (entry.render_spec && entry.render_spec.class) || "avatar sprite";
    img.width = 64;
    img.height = 64;

    const label = document.createElement("div");
    label.className = "avatar-picker-label";
    label.textContent = entry.label
      || (entry.render_spec && entry.render_spec.class)
      || entry.spec_key.slice(0, 8);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "gg-btn";
    btn.textContent = "Use this";
    btn.addEventListener("click", () => applyAvatarRenderSpec(entry.render_spec, btn));

    card.appendChild(img);
    card.appendChild(label);
    card.appendChild(btn);
    wrap.appendChild(card);
  }
  return wrap;
}

// ---------------------------------------------------------------------
// umwelt #957 (Lei, 2026-10-03): action-bar clicks used to give no visible
// feedback and never re-rendered, so a successful Take looked like a dead
// button. Three shared pieces, so a tenth /act caller cannot forget them:
//   actPost()        -- THE one door to POST /act. Never throws; always
//                       resolves to {ok, status, data, message, succeeded}.
//   showActStatus()  -- the visible outcome line (the ggStatus gadget).
//   refreshScene()   -- re-render the CURRENT place after a change, without
//                       the place-transition side effects loadScene() has.
// `ok` means the request resolved 2xx; `succeeded` means the world also did
// what was asked (a 2xx carrying effect_failures, or an error key, is
// resolved but NOT succeeded). Callers decide success from `succeeded`.
// ---------------------------------------------------------------------
let _actPending = 0;

// The visible outcome line is the `status` gadget (ggStatus, gadgets.js;
// GADGETS.md §2 + the "Action outcome" section). ONE instance, attached to
// the action bar's container, directly after the bar. `showActStatus` is kept
// as a thin call into it so the ~25 outcome sites read the same.
// kind: "pending" | "ok" | "error". `text` may carry server reason strings
// (untrusted); the gadget writes it with textContent ONLY.
// (actionBarEl is declared further down; look the bar up here so this
// top-level statement does not hit its temporal dead zone.)
const _actStatus = ggStatus({
  anchor: "attached",
  parentEl: document.getElementById("action-bar").parentNode,
  afterEl: document.getElementById("action-bar"),
  id: "act-status",
});

function showActStatus(kind, text) {
  _actStatus.show(kind, text);
}

// `quiet` (used only by the proposal viewer's background body read, whose
// failure is an expected graceful-degradation path) skips the pending line
// and busy state; every non-quiet call is followed by an ok/error line from
// its caller, which is what replaces the pending one.
// `logKind` is the charter's planned third parameter (GADGETS.md §7.3, "the
// consoleLog kind the caller uses for its success line"). Nothing reads it:
// callers keep their own console text. It is accepted so call sites match the
// charter's signature; it is NOT carried on the result.
async function actPost(intent, args, logKind, { quiet = false } = {}) {
  if (!state.csrfToken) {
    return { ok: false, status: 0, data: {},
             message: "no active session (log in first)", succeeded: false };
  }
  _lastAction = intent;
  if (!quiet) {
    _actPending++;
    actionBarEl.setAttribute("aria-busy", "true");
    actionBarEl.classList.add("act-pending");
    showActStatus("pending", `${intent}…`);
  }
  try {
    let resp;
    try {
      resp = await fetch("/act", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Csrf-Token": state.csrfToken,
        },
        body: JSON.stringify({ intent, args }),
      });
    } catch (e) {
      return { ok: false, status: 0, data: {}, message: String(e),
               succeeded: false };
    }
    let data = await resp.json().catch(() => ({}));
    if (!data || typeof data !== "object") data = {};
    if (!resp.ok) {
      return { ok: false, status: resp.status, data,
               message: data.error || `${intent} error ${resp.status}`,
               succeeded: false };
    }
    const effectFailed = Array.isArray(data.outcome?.effect_failures) &&
                         data.outcome.effect_failures.length > 0;
    const succeeded = !effectFailed && !data.refused && !data.error &&
                      !data.params_spec;
    return { ok: true, status: resp.status, data, message: "", succeeded };
  } finally {
    if (!quiet) {
      _actPending = Math.max(0, _actPending - 1);
      if (_actPending === 0) {
        actionBarEl.removeAttribute("aria-busy");
        actionBarEl.classList.remove("act-pending");
      }
    }
  }
}

// Why a resolved-but-not-succeeded (or failed) result did not work, as one
// line of text.
function _actRefusalReason(r) {
  const fails = r.data && r.data.outcome && r.data.outcome.effect_failures;
  if (Array.isArray(fails) && fails.length > 0) {
    return fails[0].reason || "unknown reason";
  }
  return r.message || (r.data && r.data.error) || "refused";
}

// The one failure path for the act* functions: errorEl (as before), the
// console line (as before), and the visible status line. `silent` suppresses
// the errorEl + status (the viewer's expected-failure body read) but keeps
// the console line, matching _actUseFetch's old behaviour.
function _actFail(intent, r, { silent = false } = {}) {
  const reason = _actRefusalReason(r);
  const text = `${intent} failed: ${reason}`;
  if (!silent) {
    // status 0 = no session / network error: old wording had the prefix;
    // an HTTP error showed the bare server message.
    errorEl.textContent = (r.ok || r.status === 0) ? text : reason;
    showActStatus("error", text);
  }
  consoleLog("error", text);
}

// Re-render the current place after an action changed the world. NOT
// loadScene(): that is a place transition (tears down the crossings poll and
// reloads the backlog). This only swaps the scene, leaves the status line,
// errorEl, the open thing viewer and the gadget stack alone, and drops a
// late answer if the user moved to another place meanwhile.
// _renderedSeq: the highest seq whose answer has actually rendered, so a load
// whose own answer arrives late can tell a NEWER request already rendered (#969).
let _refreshSeq = 0, _renderedSeq = 0;
async function refreshScene() {
  if (!state.viewer || !state.place) return;
  const placeAtStart = state.place;
  const seq = ++_refreshSeq;
  const params = new URLSearchParams({ viewer: state.viewer, place: placeAtStart });
  try {
    const resp = await fetch(`/scene?${params.toString()}`);
    if (!resp.ok) {
      const b = await resp.json().catch(() => ({}));
      showActStatus("error", `done, but the view could not refresh: ${
        (b && b.error) || `error ${resp.status}`}`);
      return;
    }
    const scene = await resp.json();
    if (state.place !== placeAtStart || seq !== _refreshSeq) return;
    render(scene);
    _renderedSeq = seq;
    lastLoadedScene = scene;
  } catch (e) {
    showActStatus("error", `done, but the view could not refresh: ${e}`);
  }
}

async function applyAvatarRenderSpec(renderSpec, triggerBtn) {
  if (!state.csrfToken) {
    errorEl.textContent = "pick avatar failed: no active session (log in first)";
    showActStatus("error", "pick avatar failed: no active session (log in first)");
    return;
  }
  if (triggerBtn) {
    triggerBtn.disabled = true;
    triggerBtn.textContent = "Applying…";
  }
  const r = await actPost("customize_avatar", { render_spec: renderSpec }, "customize_avatar");
  if (!r.ok) {
    errorEl.textContent = r.status === 0
      ? `pick avatar failed: ${r.message}`
      : (r.data.error || `pick avatar error ${r.status}`);
    showActStatus("error", `pick avatar failed: ${
      r.status === 0 ? r.message : (r.data.error || `error ${r.status}`)}`);
    if (triggerBtn) {
      triggerBtn.disabled = false;
      triggerBtn.textContent = "Use this";
    }
    return;
  }
  showActStatus("ok", "avatar updated");
  if (triggerBtn) triggerBtn.textContent = "Applied ✓";
  if (avatarPickerWin) avatarPickerWin.close();
  loadScene(); // re-render now so the new look shows immediately
}

async function openAvatarPicker() {
  if (!state.csrfToken) {
    errorEl.textContent = "pick avatar: log in first";
    return;
  }
  let resp;
  try {
    resp = await fetch("/library?status=live");
  } catch (e) {
    errorEl.textContent = `avatar picker failed to load: ${e}`;
    return;
  }
  if (!resp.ok) {
    errorEl.textContent = `avatar picker failed to load: ${resp.status}`;
    return;
  }
  const data = await resp.json();
  // Client-side filter — /library has no category param server-side
  // (status/sort only, manifest.list_entries' own signature), and every
  // entry already carries its category from the M4 pipeline.
  //
  // Accepts BOTH "avatar" and "avatars" (umwelt#382, Ariadne's tested-
  // first deploy check against real data): the live manifest has both
  // spellings in use (2 entries under the singular, 6 under the plural)
  // -- a category-naming split upstream in whatever tagged these entries,
  // not a client bug. Filtering on only one spelling silently hid 6 of 8
  // real avatar sprites. This is the fast, honest fix (both spellings
  // shown today); a proper category enum + data normalization is the
  // real follow-up, tracked in the same issue.
  const avatarEntries = (data.entries || [])
    .filter((e) => e.category === "avatar" || e.category === "avatars");
  const content = buildAvatarGalleryContent(avatarEntries);
  if (!avatarPickerWin) {
    avatarPickerWin = ggWindow({
      title: "Pick your avatar",
      anchor: { mode: "screen", pos: { top: 60, left: 60 } },
      closable: true,
      content,
      size: { width: 460, maxHeight: 480 },
    });
  } else {
    avatarPickerWin.update({ content });
  }
  avatarPickerWin.open();
}

avatarPickerBtnEl.addEventListener("click", openAvatarPicker);

// ---------------------------------------------------------------------
// M3: /act — the write chokepoint. Only `move` is wired (the only intent
// the M3 backend itself supports); every exit click, when logged in, also
// performs a real move for your own avatar. Returns true on success so
// callers can decide whether to proceed with the display-side navigation.
// ---------------------------------------------------------------------

async function actMove(place) {
  // Enforces (not just documents) the state.identity/state.csrfToken
  // pairing invariant — every current set-site (login/logout/
  // refreshSessionStatus) pairs them by construction, but this guard
  // means a future set-site that forgets to pair them fails loudly here
  // instead of silently sending a null X-Csrf-Token (#234 review).
  const r = await actPost("move", { place }, "move");
  if (!r.succeeded) {
    _actFail("move", r);
    return false;
  }
  // No re-render here: navigateTo's own loadScene() does it.
  showActStatus("ok", `moved to ${place}`);
  return true;
}

// webrender-action-bar: the most recently successfully-loaded scene
// object, kept purely so the action bar's Look action can log a one-line
// summary (place label + present/thing counts) without re-fetching or
// re-deriving that data from the DOM. loadScene() itself nulls this at
// the top of every call and only re-sets it on a successful fetch (see
// below), so a caller can tell "this specific loadScene() call succeeded"
// from "lastLoadedScene is stale from a previous call" — same
// null-then-only-pair-on-success discipline as state.csrfToken elsewhere
// in this file.
let lastLoadedScene = null;

async function loadScene() {
  errorEl.textContent = "";
  lastLoadedScene = null;
  // Feature 1: tear down any in-flight crossings poll BEFORE doing anything
  // else — every loadScene() call (initial load, "Enter/Look", or an exit
  // click) is a place transition, and a stale timer left running would keep
  // polling the OLD place under the NEW place's cursor state. This is the
  // one teardown call that has to happen unconditionally, even before the
  // viewer/place validation below, so a re-submitted blank form can't leave
  // a previous poll running either.
  stopCrossingsPolling();
  if (!state.viewer) {
    errorEl.textContent = "viewer is required";
    return;
  }
  // /scene requires `place` (no more enter-fallback — see gateway.py's
  // security fix); check client-side so a blank submit gets an immediate,
  // clear message instead of a raw 400 from the network round-trip.
  if (!state.place) {
    errorEl.textContent = "place is required — try place_square";
    return;
  }
  // #969: this call is about to render a scene, so any refreshScene() still
  // in flight (maybe for the OLD viewer, same place) is now older than this
  // load and must drop its late answer instead of overwriting it.
  // #969 round 2: keep the value so that, after OUR fetch, we can tell if a
  // refreshScene() started after us (newer seq) and already rendered.
  const mySeq = ++_refreshSeq;
  const placeAtStart = state.place;
  const params = new URLSearchParams({ viewer: state.viewer, place: placeAtStart });

  // Feature 3: nav polish — dim the street during the fetch instead of an
  // instant blank-then-redraw. Simple opacity toggle, no spinner/overlay.
  streetEl.classList.add("loading");

  let resp;
  try {
    resp = await fetch(`/scene?${params.toString()}`);
  } catch (e) {
    streetEl.classList.remove("loading");
    errorEl.textContent = `request failed: ${e}`;
    return;
  }
  if (!resp.ok) {
    streetEl.classList.remove("loading");
    const body = await resp.json().catch(() => ({}));
    errorEl.textContent = body.error || `error ${resp.status}`;
    return;
  }
  const scene = await resp.json();
  streetEl.classList.remove("loading");
  if (_renderedSeq > mySeq) {
    // A NEWER request (refresh or load) already rendered while we were in
    // flight, so do not render over it. (Not `mySeq !== _refreshSeq`: that
    // would also drop a load whose newer refresh has not answered yet, and
    // lose the scene entirely if that refresh then fails.) The "loading"
    // dim is already cleared above.
    // We stopped crossings polling at the top; if the user is still in the
    // place we asked for, restart it so a dropped load leaves it running.
    if (state.place === placeAtStart) loadCrossingsInitial(placeAtStart);
    return;
  }
  state.place = scene.place.id;
  placeInput.value = state.place;
  render(scene);
  _renderedSeq = mySeq;
  lastLoadedScene = scene;
  // Feature 1: fetch the full backlog for the new place, then start polling.
  loadCrossingsInitial(scene.place.id);
}

// ---------------------------------------------------------------------
// webrender-action-bar: a slim bar of GENERAL player actions. v0 was
// exactly two actions, Look and Say. Rendered from ACTIONS below, a small
// client-side table, so a future action is one array entry, not a new
// hardcoded button + listener pair.
//
// umwelt #267 "RPG action set" (forum #267, commander charge): adds
// Examine/Use/Take/Drop/Inventory/Map. Examine and Use act on the CURRENT
// keyboard-cursor target (`currentLaneTarget()` via `_cursorTargetedThing`
// — umwelt #323 renamed/generalized this from the flat
// `upperLaneItems[cursorIndex]`; see this file's "umwelt #323"
// module-header note above) rather than duplicating the interact-menu's
// own thing-targeted talk/examine/use — they reuse the EXISTING
// `inspectThing`/`actUse` functions unchanged.
//
// Take is also cursor-targeted, same as Examine/Use — a street thing is
// only ever reachable there while it's still place-held.
//
// Drop is DELIBERATELY NOT cursor-targeted (reviewer-fairy caught this on
// PR #307: the cursor-reachable lane lists are built from `scene.things`,
// which the world deliberately EXCLUDES anything with
// `holder_kind != 'place'` from — see
// db.py's `things_in_place` docstring, "a thing on someone's person... is
// not in the room". So a cursor can only ever land on things you do NOT
// hold, and `/umwelt/drop` requires the opposite — every cursor-targeted
// Drop attempt was a guaranteed 409, a dead button, not a real feature).
// Drop instead targets `_heldThing` — the thing most recently Taken this
// session, tracked below — falling back to a fresh `/inventory` fetch's
// first entry when nothing's been Taken yet this session (e.g. an avatar
// that walked in already carrying something from a prior session). This
// is a real v0 narrowing (single most-recent/first-carried item, not a
// full "pick which of your things to drop" UI — that needs a genuine
// selectable inventory list, which doesn't exist yet), flagged here
// rather than silently shipped as if it were general.
//
// Take and Drop are thin `/act` proxies via the new `actTake`/`actDrop`
// functions (mirroring `actUse`'s exact shape — see those functions
// below). Inventory and Map are reads, not `/act` mutations (gateway.py's
// `/act` docstring: "the write chokepoint") — each gets its own dedicated
// session-gated GET route (`/inventory`, `/map`) on the gateway, fetched
// directly here.
// ---------------------------------------------------------------------

const ACTIONS = [
  { id: "look", label: "Look" },
  { id: "say", label: "Say" },
  { id: "examine", label: "Examine" },
  { id: "use", label: "Use" },
  { id: "take", label: "Take" },
  { id: "drop", label: "Drop" },
  { id: "inventory", label: "Inventory" },
  { id: "map", label: "Map" },
];

const actionBarEl = document.getElementById("action-bar");
// umwelt #660: THE composer. There used to be two — an action-bar Say row
// and an interact-menu Talk sub-view — and which one you got depended on
// where you happened to click. That, not a missing picker, was the defect
// under Lei's "which channel am I speaking on": the channel was chosen FOR
// you by a widget, and nothing on screen said so. One surface now, always
// visible, with the channel stated on it.
const composerEl = document.getElementById("composer");
const composerEmotionsEl = document.getElementById("composer-emotions");
const composerInputEl = document.getElementById("composer-input");
const composerSendEl = document.getElementById("composer-send");
const composerHintEl = document.getElementById("composer-hint");
const composerChannelRoomEl = document.getElementById("composer-channel-room");
const composerChannelPrivateEl = document.getElementById("composer-channel-private");
const composerPrivateTargetEl = document.getElementById("composer-private-target");
const composerPrivateClearEl = document.getElementById("composer-private-clear");

// ---------------------------------------------------------------------
// umwelt #386 (Lei's live ask, issue #386 -- "the Talk/Say input box holds
// ~20 chars, no wrapping, typed text scrolls out of view, editing is
// painful... a really spacious, easy-to-navigate editor box"): both
// composer surfaces (the action-bar Say input below, and the
// interact-menu Talk input further down this file) become auto-growing
// `<textarea>`s sharing this one small helper pair, rather than a full
// gadgets.js `ggInputRow` factory -- judgment call (delegated by the spec,
// flagged here + in the PR body): the two rows differ in surrounding
// chrome (Say has the emotion-picker row above it and no header; Talk has
// a header and no emotion-picker), so a real shared-gadget consolidation
// would either force those differences into a new factory's config surface
// or still leave each call site doing custom composition around it --
// more diff than it saves for a two-call-site helper. A plain function
// pair keeps the change modest and confined to this file, with no
// gadgets.js/gadgets.css touch and no risk of dragging the THIRD input row
// (the identity/login fields) into scope. Two-surface upgrade, sharing
// logic not markup.
// ---------------------------------------------------------------------

// ~3 comfortable lines at this composer's own font-size/line-height (see
// index.html's shared `#composer textarea` CSS rule) -- kept here too (not just in the CSS min-height) so
// autoGrowComposer never shrinks the box below the "spacious" floor even
// on a nearly-empty textarea, matching the CSS min-height by construction
// rather than by two independently-tuned numbers.
const COMPOSER_MIN_HEIGHT_PX = 72;

// Grows (or shrinks back down, e.g. after a send clears the value)
// `textareaEl` to fit its content, up to ~40% of the viewport height --
// past that, the CSS max-height (40vh) clamps and overflow-y: auto (set
// permanently in CSS, PR #389 review r1) provides the internal scrollbar:
// this function's innerHeight read goes stale between keystrokes (rotate/
// resize), while the CSS clamp is continuous, so overflow must live with
// the clamp, not here. The height:auto reset before measuring
// scrollHeight is the standard auto-grow-textarea technique -- without
// it, scrollHeight would only ever report the CURRENT (possibly
// already-grown) height, never shrink back down after text is deleted.
function autoGrowComposer(textareaEl) {
  textareaEl.style.height = "auto";
  const maxHeight = window.innerHeight * 0.4;
  const nextHeight = Math.min(
    Math.max(textareaEl.scrollHeight, COMPOSER_MIN_HEIGHT_PX), maxHeight);
  textareaEl.style.height = `${nextHeight}px`;
}

// umwelt#412: FLIPPED the binding #389 shipped. Enter now inserts a
// newline (the browser's own default textarea behavior -- deliberately a
// no-op here); Shift+Enter OR Ctrl+Enter OR Cmd+Enter sends. Lei lost a
// message to the old Enter-sends binding within minutes of first using
// this composer -- he writes multi-paragraph design rulings and
// corrections here, and an editor whose Enter key destroys the draft is
// not the "spacious and easy to navigate editor box" #389 was asked to
// build in the first place. Uniform across both surfaces this function
// wires (Ariadne's forum #431 lean, adopted): a per-surface binding is
// exactly the kind of thing that produces a lost message when you switch
// between them, which is the failure being fixed here.
//
// `sendBtnEl.click()` reuses each surface's own EXISTING send listener
// (composerSendEl's click handler) rather than duplicating the send logic
// here -- the send key is exactly a click on the visible Send button
// (still the prominent mobile path), so the two paths can never drift
// (e.g. one validating `!body` and the other not).
//
// IME GUARD (Ariadne's forum #431 spec): `ev.isComposing` is true while an
// IME (e.g. confirming a Chinese pinyin candidate) is still resolving a
// keystroke -- Enter (and, per the same browser behavior, Shift/Ctrl/Cmd+
// Enter) during composition confirms the candidate, not the user's intent
// to send. This hazard predates this fix (the old Enter-sends binding had
// it too); closing it now rather than carrying it forward unexamined.
//
// SAFARI FOLLOW-UP (umwelt#412, caught by Kepler post-merge): `isComposing`
// alone misses Safari's own quirk -- on the keydown that CONFIRMS an IME
// candidate, Safari can report `isComposing: false` (composition has
// already ended by the time the event fires) while still setting the
// legacy `keyCode` to `229`, the historical "still IME-related" signal
// every other browser only used pre-`isComposing`. So Shift+Enter still
// sent under exactly the case the guard exists to block. Checking both
// covers Safari's timing without weakening the check anywhere else --
// `keyCode === 229` is deprecated and near-never set by a genuine Enter
// keydown, so this can only narrow, not widen, when the guard fires.
function wireComposerTextarea(textareaEl, sendBtnEl) {
  textareaEl.addEventListener("input", () => autoGrowComposer(textareaEl));
  textareaEl.addEventListener("keydown", (ev) => {
    if (ev.isComposing || ev.keyCode === 229) {
      return;
    }
    if (ev.key === "Enter" && (ev.shiftKey || ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      sendBtnEl.click();
    }
    // Plain Enter: no preventDefault -- the browser's own default textarea
    // behavior (insert a newline at the caret) is exactly what's wanted,
    // so this case is deliberately a no-op here.
  });
}

// umwelt #321: the emotion-picker. null = no emotion picked (the pre-#321
// say behavior: no `emotion` arg rides the intent, engine defaults to
// "calm"). `_buildSayEmotionPicker` (the builder) is DEFINED here but its
// one-time CALL lives further down this file, right after `_EMOTION_EMOJI`
// is assigned -- calling it here would reference that `const` before its
// declaration runs (module load order), which throws.
let _selectedSayEmotion = null;

// Builds one button per ACTIVE emotion into #composer-emotions -- "active"
// = every `_EMOTION_EMOJI` key EXCEPT "calm" (umwelt #321 design: "calm" is
// the engine's idle default and the emoji-bubble renders only for a
// non-default emotion, so offering "calm" here would let a user "pick" an
// emotion that visibly does nothing). Each button's emoji is set via
// `textContent` (never `innerHTML`), matching this file's no-innerHTML
// discipline for anything sourced from the closed `_EMOTION_EMOJI` map.
// Click toggles: picking the already-selected emotion clears back to none.
function _buildSayEmotionPicker() {
  for (const key of Object.keys(_EMOTION_EMOJI)) {
    if (key === "calm") continue;   // idle default -- never offered as a pick
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = _EMOTION_EMOJI[key];   // trusted literal, closed map
    btn.dataset.emotion = key;
    btn.title = key;
    btn.addEventListener("click", () => {
      _selectedSayEmotion = (_selectedSayEmotion === key) ? null : key;
      for (const b of composerEmotionsEl.children) {
        b.classList.toggle(
          "selected", b.dataset.emotion === _selectedSayEmotion);
      }
    });
    composerEmotionsEl.appendChild(btn);
  }
}

function _clearEmotionPick() {
  _selectedSayEmotion = null;
  for (const b of composerEmotionsEl.children) b.classList.remove("selected");
}

// ---------------------------------------------------------------------
// umwelt #660: composer channel state.
//
// TWO channels, not the three the task text names. Ariadne's spec caught
// that only two have transport: `say` resolves to the speaker's current
// place and `talk` requires a `to_agent`, while a crossing's destination is
// always a place -- there is no world-wide send. "Broadcast" in this
// codebase means UNADDRESSED WITHIN A PLACE, which is a narrower thing than
// the task word suggests. Rendering a third button that silently fell back
// to a room send would put the bug inside the very feature that exists to
// stop you being wrong about your channel.
// ---------------------------------------------------------------------

// "room" | "private". Room is the default and the only channel reachable
// with no target.
let composerChannel = "room";
// The private target -- agent id + the display name actTalk logs. Both null
// whenever no target is chosen, which is also what makes Private
// UNSELECTABLE rather than merely rejected at send (acceptance 4).
let composerPrivateTarget = null;
let composerPrivateDisplayName = null;

// Single writer for everything the human can see about the channel. Called
// on every state change so the visible state and the state that will
// actually be SENT cannot drift -- the drift is the bug this feature is
// about, so it gets one function rather than scattered toggles.
function renderComposerChannel() {
  const isPrivate = composerChannel === "private";
  composerEl.classList.toggle("private", isPrivate);
  composerChannelRoomEl.classList.toggle("selected", !isPrivate);
  composerChannelPrivateEl.classList.toggle("selected", isPrivate);
  // Private cannot be chosen without a target -- disabled, not validated
  // at send time. A control you cannot click is a promise; a send-time
  // rejection is an apology.
  composerChannelPrivateEl.disabled = !composerPrivateTarget;
  const label = composerPrivateDisplayName || composerPrivateTarget || "";
  composerPrivateTargetEl.textContent = label ? `→ ${label}` : "";
  composerPrivateTargetEl.classList.toggle("hidden", !composerPrivateTarget);
  composerPrivateClearEl.classList.toggle("hidden", !composerPrivateTarget);
  // The emotion arg rides `say` only -- actTalk sends {to_agent, body} and
  // nothing else. So the picker is HIDDEN while Private is selected rather
  // than left visible-and-inert: a control that is present but silently
  // does nothing is a worse lie than an absent one, and an emotion that
  // vanishes on send is the same you-were-wrong-about-what-you-sent family
  // as the channel itself.
  composerEmotionsEl.classList.toggle("hidden", isPrivate);
  if (isPrivate) _clearEmotionPick();
  composerInputEl.placeholder = isPrivate
    ? `private message to ${label}…`
    : "say something to the room…";
  composerHintEl.textContent = isPrivate
    ? `Private to ${label} — only they will see this · Enter for a new line · Shift+Enter to send (or tap Send)`
    : "Everyone here will see this · Enter for a new line · Shift+Enter to send (or tap Send)";
}

// umwelt #660, Ariadne's addition on review -- and she is right that
// inheriting the channel is NECESSARY AND NOT SUFFICIENT. Preselecting
// Private does not close the leak; it costs it one keystroke. The quote is
// sitting in the box, and switching to Room is EXACTLY the gesture of
// "actually, everyone should see this" -- which is a thought about the
// reply being written, not about the text already sitting above it. Nobody
// re-reads their own prefill.
//
// So a quote must not silently survive a switch away from the channel it
// came from. Tracked here rather than re-derived from the draft text: the
// exact prefix that was prefilled, and which channel it was prefilled for.
let composerQuoteBlock = null;
// The quote's DESTINATION, not its channel kind: "room", or the agent id it
// was addressed to. Keying on the channel string was a real hole -- see the
// guard below.
let composerQuoteDestination = null;
let composerQuoteSender = null;

function _forgetQuote() {
  composerQuoteBlock = null;
  composerQuoteDestination = null;
  composerQuoteSender = null;
}

// Where the composer would send RIGHT NOW, in the same vocabulary the quote
// records. One function so the two can never be compared in different terms.
function _composerDestination() {
  return composerChannel === "private" ? composerPrivateTarget : "room";
}

// Returns true if the switch to `nextChannel` may proceed.
//
// NARROWED to the harmful direction, deliberately, and flagged as a
// judgment call: a room quote moving to Private is the SAFE direction --
// strictly fewer people see it -- so guarding it would be a nag with no
// hazard behind it, and a guard that fires on harmless cases is the kind
// that gets clicked through on the harmful one. The asymmetry is the same
// one the whole feature rests on.
// WHAT THIS GUARD DOES NOT COVER, written here rather than only in the PR
// that introduced it. It holds the composer, and nothing else. A human who
// READS a private crossing and retypes it, or copies it by hand, walks past
// all of this untouched -- that is not a thing software can hold, and no
// amount of care here makes it one. The boundary is stated in the file
// because this entire feature exists on account of a protection nobody had
// written down quietly doing work: two separate composers were keeping a
// private quote out of a room send, and collapsing them removed a guarantee
// no one knew was there. A comment claiming more than the mechanism delivers
// is how the next reader inherits exactly that kind of phantom -- so read
// this as "the composer will not carry it for you", never as "private
// messages cannot leak". (Ariadne's review, and her line is the better one:
// naming the boundary is what stops someone in six months treating this as
// the stronger promise.)
function _guardQuoteOnDestinationChange(nextDestination) {
  if (!composerQuoteBlock || nextDestination === composerQuoteDestination) {
    return true;
  }
  // Keyed on DESTINATION, not on channel kind. Comparing channel strings left
  // a hole exactly where the harm is worst: reply privately to A, then click
  // B's Talk, and both sides read "private", so the guard passed and A's
  // private words sat in a draft addressed to B. Same class as the bug this
  // whole feature exists to close -- and it was a REGRESSION, since the old
  // interact-menu composer cleared its textarea on every open, so a target
  // switch always dropped the draft before these two surfaces were collapsed.
  // Destination identity covers private->private by construction rather than
  // by remembering to handle it at each call site.
  if (composerQuoteDestination === "room") {
    // The quote is already public; sending it anywhere reaches fewer people,
    // never more. Stop tracking, do not nag -- a prompt that fires on harmless
    // transitions is the prompt that gets clicked through on a harmful one.
    _forgetQuote();
    return true;
  }
  const draft = composerInputEl.value;
  if (draft.startsWith(composerQuoteBlock)) {
    // Untouched prefill -- remove exactly what was inserted, keeping every
    // word the human actually typed after it.
    composerInputEl.value = draft.slice(composerQuoteBlock.length);
    autoGrowComposer(composerInputEl);
    const whereTo = nextDestination === "room"
      ? "the room" : nextDestination;
    consoleLog(
      "composer",
      `removed the quoted message from ${composerQuoteSender} — it arrived `
      + `privately and you switched to ${whereTo}. Your own text was kept.`);
    _forgetQuote();
    return true;
  }
  // The human edited the quote, so there is no prefix to strip safely.
  // Name what is about to become public rather than guessing.
  const whereTo = nextDestination === "room"
    ? "the room — visible to everyone here" : nextDestination;
  const ok = confirm(
    `This draft still quotes ${composerQuoteSender}'s PRIVATE message.\n\n`
    + `Sending it to ${whereTo} shows them words they were not sent.\n\n`
    + `Continue?`);
  if (!ok) return false;
  _forgetQuote();
  return true;
}

// Switch to Room. Deliberately does NOT clear the private target: a human
// who flips Room→Private→Room while composing should not have to re-pick
// the person. What it must clear is any impression that Private is live,
// which renderComposerChannel does.
function selectRoomChannel() {
  if (!_guardQuoteOnDestinationChange("room")) return;
  composerChannel = "room";
  renderComposerChannel();
}

// Choose a private target and switch to Private. This is the interact
// menu's Talk entry point (clicking an avatar) -- the gesture is preserved,
// it just lands in the one composer instead of opening a second one.
function selectPrivateChannel(toAgent, displayName) {
  if (!toAgent) return;
  // The agent id, not the string "private" -- retargeting from A to B is a
  // destination change even though the channel kind does not move.
  if (!_guardQuoteOnDestinationChange(toAgent)) return;
  composerPrivateTarget = toAgent;
  composerPrivateDisplayName = displayName || toAgent;
  composerChannel = "private";
  renderComposerChannel();
}

// Drop the target AND the channel together. Ariadne's spec calls out the
// exact bug this prevents: "switch to Private, then away, and confirm the
// styling and the target both clear. A sticky target is the exact bug this
// feature exists to prevent."
function clearPrivateTarget() {
  // Same gesture as switching to Room, so it runs the same guard -- an
  // escape hatch that skipped it would be the hole with extra steps.
  if (!_guardQuoteOnDestinationChange("room")) return;
  composerPrivateTarget = null;
  composerPrivateDisplayName = null;
  composerChannel = "room";
  renderComposerChannel();
}

// umwelt #386: wires the composer's key-binding (umwelt#412: Enter-newline/
// Shift-or-Ctrl-or-Cmd+Enter-sends, flipped from #386's original Enter-
// sends) + auto-grow.
wireComposerTextarea(composerInputEl, composerSendEl);
composerChannelRoomEl.addEventListener("click", selectRoomChannel);
composerChannelPrivateEl.addEventListener("click", () => {
  if (!composerPrivateTarget) return;   // belt-and-braces; it is disabled
  if (!_guardQuoteOnDestinationChange(composerPrivateTarget)) return;
  composerChannel = "private";
  renderComposerChannel();
});
composerPrivateClearEl.addEventListener("click", clearPrivateTarget);

// Clears the draft after a send (or on demand) without touching the
// channel -- consecutive messages to the same person should not require
// re-picking them.
function clearComposerDraft() {
  _forgetQuote();
  composerInputEl.value = "";
  autoGrowComposer(composerInputEl);   // umwelt #386: reset height too
  _clearEmotionPick();
}

// ---------------------------------------------------------------------
// umwelt #387 (Lei's live ask, issue #387): a reply affordance on each
// crossing row (see renderCrossing below) that opens this same Say
// composer pre-filled with a short quote of the crossing being replied
// to, so the recipient has context for what's being answered. Plain-text
// quote convention only -- actSay/the /act "say" intent above are
// UNCHANGED, no threading/protocol addition.
// ---------------------------------------------------------------------

// First ~80 chars of `body`, cut at the nearest word boundary at or
// before that limit (cheap: a single lastIndexOf, not a tokenizer) --
// the `…` marker is appended ONLY when a cut actually happened, per the
// spec ("the `…` only when actually truncated"). Falls back to a hard
// cut at the limit itself if no space is found before it (e.g. one very
// long unbroken token), so this never returns an unbounded string.
const REPLY_QUOTE_BODY_LIMIT = 80;

function _truncateReplyBody(body) {
  // umwelt #1059: the quote is ONE `> sender: ...` line, so any whitespace
  // run (newline, tab, CR, U+2028/9, repeated spaces) collapses to a single
  // space and the ends are trimmed -- BEFORE the length check and cut, so the
  // 80 are counted on what is actually quoted. A body with a newline in its
  // first 80 chars (proposal notifications, #1057) would otherwise leave a
  // prefill line that does not start with `> `. Only the body is collapsed;
  // the sender name is left alone by the caller.
  body = body.replace(/\s+/g, " ").trim();
  if (body.length <= REPLY_QUOTE_BODY_LIMIT) {
    return { snippet: body, truncated: false };
  }
  let cut = body.lastIndexOf(" ", REPLY_QUOTE_BODY_LIMIT);
  if (cut <= 0) cut = REPLY_QUOTE_BODY_LIMIT;
  return { snippet: body.slice(0, cut), truncated: true };
}

// Builds the `> <sender>: <first ~80 chars>…` quote line for crossing
// `c` (requirement 2's exact prefill shape).
function _buildReplyQuote(c) {
  const { snippet, truncated } = _truncateReplyBody(c.body || "");
  return `> ${c.sender || ""}: ${snippet}${truncated ? "…" : ""}`;
}

// Opens the Say composer pre-filled with a quote of crossing `c`, plus an
// `@<sender> ` mention prefix on the blank reply line when `c.sender` is
// present (requirement 3 -- every crossing in this feed carries a real
// sender string, umwelt #387 spec: "if addressed from a named agent";
// there is no anonymous/system crossing shape in this feed to
// distinguish against, so a truthy `c.sender` is treated as "named
// agent"). Requirement 5's clobber guard: checked BEFORE anything is
// touched, against the composer's CURRENT value regardless of whether
// it's open or closed right now (the composer is always visible now, and
// a sent draft is cleared, so an empty box never falsely triggers the
// confirm).
//
// umwelt #660 -- THE REPLY INHERITS THE CHANNEL IT IS ANSWERING, and this
// is a correctness fix, not a convenience. This function prefills a QUOTE
// OF SOMEONE ELSE'S MESSAGE. Before the two composers were collapsed, reply
// always meant the Say (room) composer; a reply to a crossing that arrived
// privately would have quoted a private message into the room -- the
// unrecoverable direction of this feature's whole asymmetry, and worse than
// the ordinary version of the mistake because the leaked words are not
// yours. Two separate widgets were all that had been preventing it.
//
// A crossing carries `to_agent` (verified against the live world's
// crossings feed, not assumed): non-null means it was DIRECTED, null means
// it was said to the room. Directed -> reply privately to its sender. This
// deliberately keys on to_agent rather than on whether the viewer was the
// addressee: a directed crossing you merely overheard is still someone's
// private-shaped message, and the safe default is to answer it the way it
// arrived.
function openReplyToCrossing(c) {
  if (composerInputEl.value.trim() &&
      !confirm("Replace your current draft with this reply?")) {
    return;
  }
  // The clobber-confirm above has already asked the one question a human
  // should be asked here ("replace your current draft?"), so drop any quote
  // tracking BEFORE selecting a channel -- otherwise the channel switch
  // below would run its own quote guard and ask a second time about a draft
  // that is about to be discarded anyway.
  _forgetQuote();
  const quote = _buildReplyQuote(c);
  const mention = c.sender ? `@${c.sender} ` : "";
  if (c.to_agent && c.sender) {
    selectPrivateChannel(c.sender, c.sender);
  } else {
    selectRoomChannel();
  }
  clearComposerDraft();   // also forgets any previous quote
  const block = `${quote}\n\n`;
  composerInputEl.value = `${block}${mention}`;
  // Record what was inserted and where it came from, so a later channel
  // switch can remove it rather than carry it somewhere it does not belong.
  composerQuoteBlock = block;
  composerQuoteDestination = _composerDestination();
  composerQuoteSender = c.sender || "someone";
  autoGrowComposer(composerInputEl);
  const end = composerInputEl.value.length;
  composerInputEl.setSelectionRange(end, end);   // cursor at the end
  composerInputEl.focus();
}

// Look — reuses the existing loadScene()/render() pipeline UNCHANGED (the
// same call every exit/door click and the "Enter / Look" button already
// use — see navigateTo/goBtn above), then logs a one-line summary (place
// label + present/thing counts) to the output console. lastLoadedScene is
// non-null only when THIS loadScene() call itself succeeded (see
// loadScene's own comment on the variable) — a failed fetch already shows
// its own message in #error, so nothing further is logged here in that
// case.
async function handleActionLook() {
  await loadScene();
  if (!lastLoadedScene) return;
  const label = lastLoadedScene.place.label;
  const avatarCount = lastLoadedScene.avatars.length;
  const thingCount = lastLoadedScene.things.length;
  consoleLog(
    "look", `${label} — ${avatarCount} present, ${thingCount} things`);
}

// Say — POST /act {intent:"say", args:{body}} (plus an optional `emotion`,
// umwelt #321 — see gateway.py's `/act` "say" branch for the forwarding
// posture), a broadcast (no `to_agent` — see gateway.py's `/act` "say"
// branch). Same csrfToken-presence guard and error-handling shape as
// actTalk/actUse/actMove above (defined below this point in the file, but
// this function itself isn't called until a user clicks Send, by which
// time the whole script has finished loading).
// `emotion` (umwelt #321, optional, default undefined/null from the Send
// listener's `_selectedSayEmotion`): included in the POST body ONLY when
// truthy — a null/undefined pick sends the exact same `{body}`-only args
// this function sent before #321, so the no-emotion say path is
// byte-for-byte unchanged.
async function actSay(body, emotion) {
  const sayArgs = emotion ? { body, emotion } : { body };
  const r = await actPost("say", sayArgs, "say");
  if (!r.succeeded) {
    _actFail("say", r);
    return false;
  }
  const said = emotion ? `${body}  [${emotion}]` : body;
  consoleLog("say", said);
  // No scene refresh here: the crossings poller shows the new line.
  showActStatus("ok", said);
  return true;
}

// umwelt #660: Say no longer opens or closes anything -- the composer is
// always there. The button now means "put me in Room mode and give me the
// cursor", which is what the old open-the-Say-row gesture actually meant.
function handleActionSay() {
  selectRoomChannel();
  composerInputEl.focus();
}

// One send path for both channels. The transport functions (actSay/actTalk)
// are UNCHANGED -- this feature is the interface over them, per the spec's
// "keep both existing send paths as the transport".
composerSendEl.addEventListener("click", async () => {
  const body = composerInputEl.value.trim();
  if (!body) return;
  let sent;
  if (composerChannel === "private") {
    // Re-checked at send even though Private is unselectable without a
    // target: this is the one place where being wrong is unrecoverable, so
    // it does not rely on the disabled attribute alone.
    if (!composerPrivateTarget) return;
    sent = await actTalk(
      composerPrivateTarget, body, composerPrivateDisplayName);
  } else {
    sent = await actSay(body, _selectedSayEmotion);
  }
  if (sent) clearComposerDraft();
});

// umwelt #321: Use's args sub-view — a hidden row opened/closed the same (a
// hidden row, opened/closed the same way) so args become reachable
// without abandoning the existing single-click "Use" mental model for the
// common no-args case. `_useTarget` ({id, name}) is the thing the sub-view
// will act on once Send is clicked — set by handleActionUse below, the
// moment a thing is cursor-targeted and Use is pressed.
const actionBarUseEl = document.getElementById("action-bar-use");
const actionBarUseTargetEl = document.getElementById("action-bar-use-target");
const actionBarUseInputEl = document.getElementById("action-bar-use-input");
const actionBarUseSendEl = document.getElementById("action-bar-use-send");
let _useTarget = null;

function openActionBarUse(t) {
  _useTarget = { id: t.id, name: t.name };
  actionBarUseTargetEl.textContent = t.name;   // plain thing name, textContent only
  actionBarUseInputEl.value = "";
  actionBarUseEl.classList.remove("hidden");
  actionBarUseInputEl.focus();
}

function closeActionBarUse() {
  actionBarUseEl.classList.add("hidden");
  actionBarUseInputEl.value = "";
}

// Whitespace-split into an array of plain positional string args (matching
// the CLI `use`'s own args semantics) — deliberately NOT JSON.parse'd
// (umwelt #321: avoids an injection/parse surface on user input; each
// token stays a plain string, escape-safe on display since it's never
// rendered back as HTML).
actionBarUseSendEl.addEventListener("click", async () => {
  const raw = actionBarUseInputEl.value.trim();
  const useArgs = raw ? raw.split(/\s+/) : [];
  const used = await actUse(_useTarget.id, _useTarget.name, useArgs);
  if (used) {
    closeActionBarUse();
  }
});

// umwelt #267 "RPG action set": Examine/Use/Take/Drop all act on whichever
// item the keyboard cursor currently targets — the same
// `currentLaneTarget()` resolution `activateCursorTarget` (module-header
// note above) already reads (umwelt #323: was
// `upperLaneItems[cursorIndex]` directly; now the lane-aware resolution,
// same "only `kind === 'thing'` entries are examine/use/take/drop-able"
// contract). Returns the targeted thing's raw data object, or null when
// there is no thing currently targeted.
function _cursorTargetedThing() {
  const target = currentLaneTarget();
  return target && target.kind === "thing" ? target.data : null;
}

// `_heldThing` — {id, name} of the thing Drop will act on, or null. Set on
// every successful actTake (umwelt #267 review fix: see the module-header
// note above for why Drop can't use the cursor-target model at all).
// Cleared on a successful actDrop, since Drop only ever acts on this
// value, so a successful drop always means it's no longer held.
let _heldThing = null;

// Resolves Drop's target: the in-session `_heldThing` if set (fast path,
// no network call — covers the common Take-then-Drop flow), else a fresh
// `/inventory` fetch's first entry (covers an avatar that already held
// something before this page loaded). Returns {id, name} or null if
// nothing is carried (or the fetch itself fails — treated the same as
// "nothing to drop" rather than surfacing a separate network-error path
// here, since handleActionDrop's own caller already logs a clear
// "nothing to drop" message either way).
async function _resolveHeldThing() {
  if (_heldThing) return _heldThing;
  let resp;
  try {
    resp = await fetch("/inventory");
  } catch (e) {
    return null;
  }
  if (!resp.ok) return null;
  const data = await resp.json();
  const things = data.things || [];
  return things.length ? { id: things[0].id, name: things[0].name } : null;
}

function handleActionExamine() {
  const t = _cursorTargetedThing();
  if (!t) {
    consoleLog("error", "no item targeted -- use ← → to select one");
    return;
  }
  inspectThing(t.id);
}

function handleActionUse() {
  const t = _cursorTargetedThing();
  if (!t) {
    consoleLog("error", "no item targeted -- use ← → to select one");
    return;
  }
  // umwelt #321: opens the args sub-view rather than acting immediately —
  // see openActionBarUse/actionBarUseSendEl's listener above for the rest
  // of the flow.
  openActionBarUse(t);
}

function handleActionTake() {
  const t = _cursorTargetedThing();
  if (!t) {
    consoleLog("error", "no item targeted -- use ← → to select one");
    return;
  }
  actTake(t.id, t.name);
}

async function handleActionDrop() {
  const t = await _resolveHeldThing();
  if (!t) {
    consoleLog("error", "nothing to drop -- take something first");
    return;
  }
  actDrop(t.id, t.name);
}

// Inventory — GET /inventory (session-gated, no CSRF needed — a read).
// Same one-line-summary-to-console shape as handleActionLook, per this
// file's #259 console-output discipline.
async function handleActionInventory() {
  let resp;
  try {
    resp = await fetch("/inventory");
  } catch (e) {
    consoleLog("error", `inventory failed: ${e}`);
    return;
  }
  if (!resp.ok) {
    if (resp.status === 401) {
      consoleLog("error", "inventory failed: no active session (log in first)");
    } else {
      const body = await resp.json().catch(() => ({}));
      consoleLog("error", body.error || `inventory error ${resp.status}`);
    }
    return;
  }
  const data = await resp.json();
  const things = data.things || [];
  consoleLog(
    "inventory",
    `carrying ${things.length} things -- load ${data.load}/${data.capacity}`,
    things.map((t) => t.name).join(", "));
}

// Map — GET /map (session-gated, no CSRF needed — a read).
async function handleActionMap() {
  let resp;
  try {
    resp = await fetch("/map");
  } catch (e) {
    consoleLog("error", `map failed: ${e}`);
    return;
  }
  if (!resp.ok) {
    if (resp.status === 401) {
      consoleLog("error", "map failed: no active session (log in first)");
    } else {
      const body = await resp.json().catch(() => ({}));
      consoleLog("error", body.error || `map error ${resp.status}`);
    }
    return;
  }
  const data = await resp.json();
  const roots = data.roots || [];
  consoleLog(
    "map",
    `${roots.length} places visible`,
    roots.map((r) => r.label).join(", "));
}

// action.id -> handler. Keyed on OUR OWN trusted ACTIONS array (never an
// attacker-controlled string), unlike the emotion lookup's
// Object.create(null) precedent above — that discipline exists
// specifically for untrusted string keys, which this isn't.
const _ACTION_HANDLERS = {
  look: handleActionLook,
  say: handleActionSay,
  examine: handleActionExamine,
  use: handleActionUse,
  take: handleActionTake,
  drop: handleActionDrop,
  inventory: handleActionInventory,
  map: handleActionMap,
};

for (const action of ACTIONS) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = action.label;
  btn.addEventListener("click", () => _ACTION_HANDLERS[action.id]());
  actionBarEl.appendChild(btn);
}

// ---------------------------------------------------------------------
// P0 nav (umwelt #238): one shared "move (if logged in) then navigate the
// client" sequence, used by ordinary exit buttons, the parent nav button,
// and the always-visible back-to-Square button — one copy of the
// move-then-navigate logic instead of three.
// ---------------------------------------------------------------------

async function navigateTo(navItem) {
  // navItem: {place_id, label} — same trimmed shape scene.exits/scene.parent
  // entries already use.
  if (state.identity !== "spectator") {
    const moved = await actMove(navItem.place_id);
    if (!moved) return;
    // umwelt #259: the console log lives here rather than inside actMove
    // itself — navItem.label (the human-readable destination name) is
    // only available at this call site, not inside actMove (which only
    // ever sees the bare place id).
    consoleLog("move", `moved to ${navItem.label}`);
  }
  state.place = navItem.place_id;
  placeInput.value = state.place;
  loadScene();
}

// umwelt #240: a door — used for both the back-door (scene.parent) and
// forward exits (scene.exits). Still a real <button> (native keyboard/tap
// semantics), restyled as a door+sign via CSS instead of a text link. Click
// still calls navigateTo unchanged — the exact same move-then-navigate
// mechanism every exit button already used (see navigateTo's own docstring
// above; M3's "logged in -> a nav click is a real move for YOUR avatar, not
// just display-navigation" note still applies here unchanged).
//
// 2026-07-24 world-finishing sprint: place-marker sprites (forum #277
// #5000/#5006) — the fixed `.door-shape` below was Lei's "green squares"
// for places (every destination looked identical regardless of where it
// led). Same sprite-vs-fallback split buildThingElement/buildAvatarElement
// already use: `navItem.sprite_ref` (now populated by gateway.py's
// `_enrich_place_markers_with_sprites` when the destination has real or
// generic art) renders as an image inside `.door-shape`'s footprint;
// absent/null falls back to the original plain CSS shape, unchanged.
function buildDoorElement(navItem, { isBackDoor = false, labelPrefix = "" } = {}) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "door" + (isBackDoor ? " door-back" : "");
  // umwelt#543: the door's own [state][owner: x] authorization marker
  // (already rendered server-side, never reconstructed here) appended to
  // the hover tooltip when present — a desktop affordance, not the only
  // one; a touch-visible badge on the door sprite is out of scope here.
  // Field is `privacy_marker`, not `marker` — a door/exit entry's `marker`
  // name is already taken by the unrelated sprite-overlay-badge concept
  // things carry elsewhere in this same scene response (gateway.py's
  // `_build_scene` docstring has the full reasoning).
  btn.title = navItem.privacy_marker
    ? `${navItem.label} ${navItem.privacy_marker}` : navItem.label;

  const shape = document.createElement("div");
  shape.className = "door-shape";
  if (navItem.sprite_ref) {
    const img = document.createElement("img");
    img.src = navItem.sprite_ref;
    img.alt = navItem.label;
    img.className = "door-sprite-img";
    shape.appendChild(img);
  }
  btn.appendChild(shape);

  const sign = document.createElement("div");
  sign.className = "door-sign";
  // labelPrefix is a static, non-user-controlled string literal (e.g. the
  // back-door's "⟵ " marker below) — navItem.label is the only untrusted
  // part, and both are placed via textContent (never string-concatenated
  // into markup), same no-innerHTML discipline as every other untrusted
  // field in this file.
  sign.textContent = labelPrefix + navItem.label;
  btn.appendChild(sign);

  btn.addEventListener("click", () => navigateTo(navItem));
  return btn;
}

document.getElementById("back-to-square").addEventListener("click", () => {
  navigateTo({ place_id: "place_square", label: "the Square" });
});

// umwelt #240: a thing sprite/fallback-color element, exactly M0-M2's
// existing per-thing rendering (sprite_ref -> real sprite + optional tint;
// else the flat kind-colored rectangle + name text) — only extracted out of
// the old cell-positioned render() loop so it can be appended directly onto
// the street strip instead of into a grid cell. Click-to-inspect unchanged.
function buildThingElement(t) {
  const el = document.createElement("div");
  el.className = "item thing";
  el.title = t.name;
  // umwelt #351: the ONLY on-screen handle back to a thing's DOM element
  // this file keeps (nothing pre-existing stored one) — actUse (below)
  // still queries this. Originally used to street-anchor the outcome
  // window near the thing that produced it; that street-anchoring was
  // removed (Kepler's #351 clip-fix ruling, 2026-07-25, GADGETS.md §3's
  // street/screen amendment: the outcome window is content-sized and
  // always screen-anchored now), so the looked-up element is currently
  // passed through only for `_openOutcomeWindow`'s signature-compatible
  // `targetEl` param, not for anchoring. Judgment call (flagged in the
  // PR): a data attribute rather than a side-table (e.g. a thingId -> el
  // Map kept alongside laneItems) — simpler, and render() already
  // clears/rebuilds this element's whole subtree every scene reload, so
  // there's no separate cache to invalidate.
  el.dataset.thingId = t.id;

  if (t.sprite_ref) {
    // M2: real sprite. Falls back to M0's colored rectangle below when
    // sprite_ref is null (no render_spec, or a rejected/malformed one).
    const wrap = document.createElement("div");
    wrap.className = "sprite-wrap";
    const img = document.createElement("img");
    img.src = t.sprite_ref;
    img.alt = t.name;
    img.className = "sprite-img";
    wrap.appendChild(img);
    if (t.color) {
      // Per-instance tint (§A.5): a semi-transparent color overlay
      // composited over the sprite via mix-blend-mode. Not exact color
      // science — "visibly different" is the design's own bar, not a
      // perfect recolor.
      const tint = document.createElement("div");
      tint.className = "sprite-tint";
      tint.style.backgroundColor = t.color;
      wrap.appendChild(tint);
    }
    el.appendChild(wrap);
  } else {
    // M0's original placeholder rendering, name text now in its own
    // .item-label child (umwelt #257) so it can wrap onto 2 lines instead
    // of clipping mid-character — still plain textContent, same
    // no-innerHTML discipline as everywhere else in this file.
    el.style.background = t.color || "#999";
    const label = document.createElement("span");
    label.className = "item-label";
    label.textContent = t.name;
    el.appendChild(label);
  }

  if (t.marker) {
    // Server already validated marker to <=2 alphanumeric chars — still
    // placed via textContent, same no-innerHTML discipline as every
    // other untrusted field in this file.
    const markerEl = document.createElement("div");
    markerEl.className = "marker";
    markerEl.textContent = t.marker;
    el.appendChild(markerEl);
  }

  // umwelt #267 "menu PR": tap opens the interact-menu (was: direct
  // click-to-inspect). Avatars get the parallel listener in
  // buildAvatarElement below. stopPropagation so this click never bubbles
  // to a document-level "click elsewhere to dismiss" listener (inspect
  // panel's or the interact-menu's own) and immediately re-closes
  // whichever panel it just opened.
  el.addEventListener("click", (ev) => {
    ev.stopPropagation();
    selectThing(t.id, t.name, "street"); // umwelt #936: also the selection
    openInteractMenuForThing(t);
  });
  return el;
}

// --- emotion-render: #256 face-badge (idle indicator) + #267 emoji bubble --
//
// #256 face-badge: the small on-avatar idle indicator (pure CSS geometric
// shapes — eyes + mouth pseudo-shapes via plain <span>s). Recognized
// emotions map to fixed CSS face classes via a CLOSED `_EMOTION_FACES` dict
// (null-prototype, same Object.create(null) posture as `_EMOTION_EMOJI` —
// bracket lookup on any string not explicitly listed returns undefined,
// never an inherited function). Unrecognized emotions get a deterministic
// hash-derived RGB tint on a fixed neutral fallback shape (fnv1aHash →
// fallbackFaceColor). The raw emotion string is NEVER composited as text —
// only ever a dict-lookup key or hash input (§12.10 invariant).
// "calm" maps to the same face-neutral shape as "neutral" (both mean
// "nothing notable"), keeping the recognized set's visual vocabulary
// tight — server.py's _CROSSING_EMOTION_DEFAULT literal means every
// never-crossed avatar shows "calm", the most common case on a young
// street.
const _EMOTION_FACES = Object.create(null);
Object.assign(_EMOTION_FACES, {
  happy: "face-happy",
  sad: "face-sad",
  thinking: "face-thinking",
  asleep: "face-asleep",
  surprised: "face-surprised",
  neutral: "face-neutral",
  calm: "face-neutral",
});

// A stable (never session-varying) 32-bit FNV-1a hash of a string — the JS
// analog of render.py's `_hash_bytes` (that module hashes via Python's
// synchronous `hashlib.sha256`; this file is vanilla JS with a firm
// "no build step" constraint and no synchronous cryptographic hash
// primitive available, so FNV-1a substitutes — same STABILITY property
// (deterministic across every call, every session, every process; never
// `Math.random()`), not the same cryptographic strength, which nothing
// here requires: this hash is a color-tint SELECTOR only, never a value
// an attacker gains anything by predicting or colliding).
function fnv1aHash(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0; // unsigned 32-bit
}

// Deterministic RGB tint for an unrecognized emotion string, derived from
// a hash of the string itself — mirrors render.py's `_fallback_color`
// (render.py:65-73) exactly: NUMBERS ONLY, channels clamped to [40, 215]
// (never near-black/near-white, stays legible against the GB-palette
// badge), the raw string is hash input ONLY and never touches the DOM as
// text or as a class name built from unvalidated content.
function fallbackFaceColor(rawEmotion) {
  const h = fnv1aHash(`emotion:${rawEmotion}`);
  const r = 40 + (h % 176);
  const g = 40 + ((h >>> 8) % 176);
  const b = 40 + ((h >>> 16) % 176);
  return `rgb(${r}, ${g}, ${b})`;
}

// Selects the face CSS class for a (possibly attacker-authored, possibly
// non-string, possibly absent) emotion value. Recognized -> one of the
// closed `_EMOTION_FACES` set. Anything else (unrecognized string,
// non-string, null/undefined) -> the single deterministic "face-fallback"
// shape, tinted via fallbackFaceColor above. This is a sprite SELECTION,
// never a caption — the security invariant this whole slice exists to
// enforce (see the module-header comment).
function selectFaceClass(rawEmotion) {
  const e = typeof rawEmotion === "string" ? rawEmotion : "";
  return _EMOTION_FACES[e] || "face-fallback";
}

// Builds the small ambient face-badge overlay for one avatar (the idle
// indicator — a human street-walker should be able to tell happy from sad
// from thinking at a glance, though at street scale this is sub-pixel;
// kepler #267 adds a readable emoji bubble above the head). Pure DOM
// structure + CSS classes; the only piece of untrusted input (`rawEmotion`)
// is consumed exclusively as a dict-lookup key (recognized path) or hash
// input (fallback path) — never assigned to `.textContent` or `.innerHTML`
// anywhere in this function, matching the file's existing no-agent-text-
// as-markup discipline for every other untrusted field.
function buildFaceElement(rawEmotion) {
  const badge = document.createElement("div");
  const faceClass = selectFaceClass(rawEmotion);
  badge.className = `face-badge ${faceClass}`;
  if (faceClass === "face-fallback") {
    const e = typeof rawEmotion === "string" ? rawEmotion : "";
    badge.style.backgroundColor = fallbackFaceColor(e);
  }
  const eyeL = document.createElement("span");
  eyeL.className = "eye eye-l";
  const eyeR = document.createElement("span");
  eyeR.className = "eye eye-r";
  const mouth = document.createElement("span");
  mouth.className = "mouth";
  badge.appendChild(eyeL);
  badge.appendChild(eyeR);
  badge.appendChild(mouth);
  return badge;
}

// --- #267 emoji bubble (readable at street scale, above the avatar head) ---
//
// Recognized-emotion -> fixed emoji glyph. A CLOSED dict, same null-
// prototype posture as _EMOTION_FACES above. Emojis are fixed Unicode
// literals selected from the map, never derived from the raw emotion
// string. The raw emotion string is consumed EXCLUSIVELY as a dict-lookup
// key, never assigned to .textContent, .innerHTML, or any class name or
// attribute — matching this file's established no-innerHTML discipline for
// untrusted content (§12.10).
const _EMOTION_EMOJI = Object.create(null);
Object.assign(_EMOTION_EMOJI, {
  happy:     "\u{1F60A}",
  sad:       "\u{1F622}",
  thinking:  "\u{1F914}",
  asleep:    "\u{1F634}",
  surprised: "\u{1F62E}",
  neutral:   "\u{1F610}",
  calm:      "\u{1F60C}",
});
const _EMOTION_EMOJI_FALLBACK = "\u{1F642}";   // single fixed literal for unrecognized/non-string/absent

// umwelt #321: the one-time call into the Say sub-view's emotion-picker
// builder (defined up near the rest of the composer wiring — see
// `_buildSayEmotionPicker`'s own comment for why the CALL lives here
// instead: `_EMOTION_EMOJI` must already be assigned).
_buildSayEmotionPicker();

// umwelt #660: paint the composer's channel state once at load. index.html
// ships the Room-selected / Private-disabled shape statically so the bar is
// never briefly wrong before JS runs, but the hint copy and placeholder are
// generated here -- one writer for what the human reads about the channel.
renderComposerChannel();

function selectEmoji(rawEmotion) {
  const e = typeof rawEmotion === "string" ? rawEmotion : "";
  return _EMOTION_EMOJI[e] || _EMOTION_EMOJI_FALLBACK;
}

// The engine defaults an unset/omitted emotion to "calm" (umwelt #133;
// server.py _CROSSING_EMOTION_DEFAULT = db.CROSSING_EMOTION_DEFAULT, and
// build_crossing defaults an absent tag to "calm" at write time). Per kepler
// #267 #4840, the emoji bubble renders ONLY for an active (non-default)
// emotion — the #256 face-badge is the always-on idle indicator; the bubble
// APPEARING is the signal, not permanent thought-bubble clutter on every
// avatar. The /scene field does not expose default-vs-explicit, so an
// explicitly-set "calm" is indistinguishable from idle and also shows no
// bubble (flagged for the screenshot review). Decay/auto-hide is a follow-up
// (kepler #4840), NOT this slice — no client-side timer state is invented.
//
// `rawEmotion` is consumed here ONLY as a comparison key against fixed
// literals ("" and "calm") — never as DOM content. §12.10 is unaffected.
const _EMOTION_IDLE_DEFAULT = "calm";
function isActiveEmotion(rawEmotion) {
  const e = typeof rawEmotion === "string" ? rawEmotion : "";
  return e !== "" && e !== _EMOTION_IDLE_DEFAULT;
}

// Builds the readable emoji bubble above the avatar's head (kepler #267
// playtest finding: the #256 face-badge alone is sub-pixel at street
// scale). Returns null for an idle/default emotion (kepler #267 #4840: the
// bubble appears only for an active emotion; the face-badge alone is the
// idle state) — the caller must null-check before appending. Pure DOM
// structure + CSS class; the only piece of untrusted input (`rawEmotion`)
// is consumed exclusively as a dict-lookup key in selectEmoji — the bubble's
// textContent is the mapped/fallback emoji (a trusted literal), never the
// raw string. Mirrors the §12.10 discipline buildFaceElement enforces for
// the face-badge.
function buildEmojiBubble(rawEmotion) {
  if (!isActiveEmotion(rawEmotion)) return null;
  const bubble = document.createElement("div");
  bubble.className = "emotion-bubble";
  bubble.textContent = selectEmoji(rawEmotion);   // trusted literal from closed map
  bubble.setAttribute("aria-label", "emotion");    // no raw string in the attribute either
  return bubble;
}

// umwelt #240: an avatar element, exactly M0-M3's existing per-avatar
// rendering (flat green circle + name text, click-to-inspect via
// a.avatar_id) — same extraction-only treatment as buildThingElement above.
// New: the caller's own avatar (state.identity matched against a.agent)
// gets a `.self` class (a CSS glow/outline, index.html) — requirement 2 of
// the street-view design: distinct visual treatment, but still exactly one
// entry within the avatars list, never a second body drawn twice.
//
// #256 face-badge (always-on idle indicator) + #267 emoji bubble
// (readable at street scale, above the head, only for an active emotion —
// kepler #267 #4840). Both key off the same `a.emotion` data; see the
// module-header comment for the §12.10 security invariant that governs
// both paths.
// 2026-07-25, Lei's live feedback: avatar names come from the server as
// "<agent>'s avatar" (e.g. "sol's avatar") — fine as the full `.title`
// tooltip, too long for the now-single-line bottom-overlay label (see
// index.html's `.item.avatar .item-label` rule). Splits on the FIRST
// apostrophe and capitalizes the leading word; a name with no apostrophe
// (a future avatar-naming convention that doesn't follow the
// possessive pattern) falls back to the whole name unchanged rather than
// mangling something this function doesn't recognize.
function avatarDisplayName(name) {
  const raw = typeof name === "string" ? name : "";
  const apostropheIdx = raw.indexOf("'");
  const firstWord = apostropheIdx === -1 ? raw : raw.slice(0, apostropheIdx);
  const trimmed = firstWord.trim();
  if (!trimmed) return raw;
  return trimmed[0].toUpperCase() + trimmed.slice(1);
}

function buildAvatarElement(a) {
  const el = document.createElement("div");
  el.className = "item avatar";
  if (state.identity !== "spectator" && a.agent === state.identity) {
    el.classList.add("self");
  }
  el.title = a.name;

  // 2026-07-24 world-finishing sprint: avatar serve-path — same
  // sprite-vs-fallback split buildThingElement already
  // uses for things, mirrored here now that /scene actually populates
  // a.sprite_ref for a minted avatar instead of hardcoding null. No tint
  // (avatars carry no `color` field, unlike things — gateway.py's
  // _apply_avatar_render_spec deliberately doesn't set one) and no
  // .marker child (avatars don't use that field either).
  if (a.sprite_ref) {
    const wrap = document.createElement("div");
    wrap.className = "sprite-wrap";
    const img = document.createElement("img");
    img.src = a.sprite_ref;
    img.alt = a.name;
    img.className = "sprite-img";
    wrap.appendChild(img);
    el.appendChild(wrap);
  } else {
    // Pre-mint fallback — the original green circle, unchanged.
    el.style.background = "#2ecc71";
  }

  // umwelt #257: name text in its own .item-label child (2-line wrap
  // instead of 1-line clip) — same pattern as buildThingElement above.
  // Displayed text is shortened (avatarDisplayName) to fit the bottom-
  // overlay strip; `el.title` above keeps the full "<agent>'s avatar"
  // string as the hover tooltip, so nothing is actually lost, just not
  // both shown at once in the cramped street view.
  const label = document.createElement("span");
  label.className = "item-label";
  label.textContent = avatarDisplayName(a.name);
  el.appendChild(label);
  el.appendChild(buildFaceElement(a.emotion));
  // #267 #4840: the bubble renders only for an active (non-default) emotion;
  // buildEmojiBubble returns null for idle/calm, so null-check before append.
  const _bubble = buildEmojiBubble(a.emotion);
  if (_bubble) el.appendChild(_bubble);

  // umwelt #267 "menu PR": tap opens the interact-menu (was: direct
  // click-to-inspect, umwelt #239), parallel to the thing listener above
  // (same stopPropagation reasoning). `a.avatar_id` is the avatar's
  // underlying THING id — NOT `a.agent` (the canonical agent-id string)
  // — see gateway.py's `_build_scene` docstring for why /inspect needs
  // the former, not the latter (openInteractMenuForAvatar's own Examine
  // button below still uses `a.avatar_id` for exactly this reason).
  el.addEventListener("click", (ev) => {
    ev.stopPropagation();
    openInteractMenuForAvatar(a);
  });
  return el;
}

function render(scene) {
  // umwelt #267: a scene rebuild may remove the open interact-menu's target
  // thing/avatar. umwelt #957: close it only then -- a same-place refresh
  // that still contains the target keeps the menu and its sub-views.
  reconcileInteractMenu(scene);
  document.getElementById("place-label").textContent = scene.place.label;
  document.getElementById("place-desc").textContent = scene.place.description;
  // Banner present-count (design req. 5) — the "— " connector itself is
  // static CSS content (index.html's #present-count::before), never
  // string-concatenated with untrusted text; only the count is dynamic,
  // and it's a plain number, set via textContent regardless.
  presentCountEl.textContent = `${scene.avatars.length} here`;

  // umwelt #283: scene.cell/scene.footprint (the old grid-position fields)
  // are still unused for layout; DOM order within each band IS the layout.
  // Each thing/avatar is routed by its own `lane` field (sky/land/road) —
  // the gateway always emits a normalized value, but a fallback default is
  // still applied here defensively (mirrors gateway.py's `_lane_key`
  // per-source defaults: 'land' for things, 'road' for avatars) in case a
  // caller ever hands render() a hand-built scene missing the field.
  // Doors (back-door + scene.exits) have no `lane` field at all — they
  // always render in the land band, at their exact pre-#283 position.
  streetSkyEl.innerHTML = "";
  streetLandEl.innerHTML = "";
  streetRoadEl.innerHTML = "";

  // place-lane-tiles spec §4: paint each band's own repeating background
  // tile from scene.place.lane_tiles, independent of (and additive to) the
  // thing/avatar foreground rendering above/below — flex children are
  // positioned independently of the container's own background layer, so
  // this never needs a z-index change for things/avatars to stay on top.
  // These three band divs are reused across place-to-place navigation
  // (never recreated — only their .innerHTML is cleared above), so a lane
  // with no tile set on THIS place must explicitly clear any background
  // left over from a PRIOR place's render, not just leave it unset.
  for (const [bandEl, lane] of [
    [streetSkyEl, "sky"], [streetLandEl, "land"], [streetRoadEl, "road"],
  ]) {
    const tileRef = scene.place.lane_tiles?.[lane];
    if (tileRef) {
      bandEl.style.backgroundImage = `url(${tileRef})`;
      bandEl.style.backgroundRepeat = "repeat";
    } else {
      bandEl.style.backgroundImage = "";
      bandEl.style.backgroundRepeat = "";
    }
  }
  if (selfCursorEl && selfCursorEl.parentNode) {
    selfCursorEl.parentNode.removeChild(selfCursorEl);
  }
  selfCursorEl = null;
  // umwelt #323: laneItems (was the flat upperLaneItems) buckets by lane —
  // allNavItems (the screen-x-sorted merge) is built below, once every
  // item has been appended to the DOM.
  laneItems = { sky: [], land: [], road: [] };

  function thingLane(t) {
    return t.lane === "sky" || t.lane === "road" ? t.lane : "land";
  }
  const skyThings = scene.things.filter((t) => thingLane(t) === "sky");
  const landThings = scene.things.filter((t) => thingLane(t) === "land");
  const roadThings = scene.things.filter((t) => thingLane(t) === "road");

  // Sky band, left to right: sky-lane things only (umwelt #283 — new;
  // empty in every scene until something opts into lane='sky').
  for (const t of skyThings) {
    const el = buildThingElement(t);
    streetSkyEl.appendChild(el);
    laneItems.sky.push({ el, kind: "thing", data: t, lane: "sky" });
  }

  // Land band, left to right (UNCHANGED relative ordering from #240's
  // original single-row logic, only re-targeted at streetLandEl and
  // filtered to land-lane things): back-door (fixed leftmost, only when
  // scene.parent is non-null — design req. 3) -> land-lane things ->
  // exits (already server-sorted by label, rendered in the order given —
  // design req. 4).
  if (scene.parent) {
    // "⟵ " is a static, non-user-controlled string literal — same pattern
    // the old parent nav button's "↑ " prefix already used.
    const el = buildDoorElement(
      scene.parent, { isBackDoor: true, labelPrefix: "⟵ " });
    streetLandEl.appendChild(el);
    laneItems.land.push({ el, kind: "door", data: scene.parent, lane: "land" });
  }
  // When scene.parent is null (a top-level place), that edge position is
  // simply empty — no fake/disabled door element is ever appended here
  // (design req. 3's explicit "don't draw a fake/disabled door").

  for (const t of landThings) {
    const el = buildThingElement(t);
    streetLandEl.appendChild(el);
    laneItems.land.push({ el, kind: "thing", data: t, lane: "land" });
  }

  // M3: logged in — a nav click is a real move for YOUR avatar, not just a
  // display-navigation. If the move fails (e.g. a 401 because the session
  // expired mid-visit), navigateTo does NOT proceed to the display
  // navigation either — an error with the old scene still showing is more
  // honest than silently switching the view while the actual move never
  // happened. See navigateTo/buildDoorElement above.
  for (const ex of scene.exits) {
    const el = buildDoorElement(ex);
    streetLandEl.appendChild(el);
    laneItems.land.push({ el, kind: "door", data: ex, lane: "land" });
  }

  // Road band: road-lane things (umwelt #283 — new) then avatars (the
  // engine-side spec: "road is the bottom band — the road lane holds
  // avatars AND any road-things together"). Road-lane things ARE
  // keyboard-navigable (umwelt #323: added to laneItems.road — see
  // module-header note); avatars now are too (see below) — closes the
  // street-M4 "avatar status/talk is click/tap-only" keyboard-reachability
  // gap (DESIGN.md §12.5) on top of the unchanged click/tap path.
  for (const t of roadThings) {
    const el = buildThingElement(t);
    streetRoadEl.appendChild(el);
    laneItems.road.push({ el, kind: "thing", data: t, lane: "road" });
  }

  // The logged-in viewer's own avatar (if present in this scene) is pulled
  // OUT of the normal flex flow and becomes the player cursor (see
  // updateCursorPosition below), regardless of its own `lane` — it always
  // anchors to the road band (umwelt #283: streetRoadEl), the direct
  // successor of the old "lower lane" concept, since avatars are road-lane
  // by default and there is no lane-mutation path today. The self-cursor
  // avatar is NEVER pushed into laneItems (it's the cursor itself, not a
  // nav target) — untouched by the avatar-keyboard-reachability change
  // below. Every OTHER avatar renders in scene.avatars order, routed by
  // its own `lane` into the matching band, at its normal static flex
  // position — UNCHANGED — and is now ALSO pushed into that same lane's
  // laneItems (kind: "avatar") so ArrowLeft/ArrowRight can reach it and
  // Enter (activateCursorTarget below) opens the SAME interact-menu a
  // click on it already does (openInteractMenuForAvatar — no parallel
  // implementation). buildAvatarElement's own click listener is untouched;
  // this is a purely additive keyboard path alongside it.
  for (const a of scene.avatars) {
    const el = buildAvatarElement(a);
    if (state.identity !== "spectator" && a.agent === state.identity) {
      el.classList.add("avatar-cursor");
      selfCursorEl = el;
      streetEl.appendChild(el); // sibling of the three bands — shares
                                // #street as offsetParent with the upper-
                                // lane items updateCursorPosition measures.
      continue;
    }
    const lane = a.lane === "sky" || a.lane === "land" ? a.lane : "road";
    if (lane === "sky") {
      streetSkyEl.appendChild(el);
    } else if (lane === "land") {
      streetLandEl.appendChild(el);
    } else {
      streetRoadEl.appendChild(el);
    }
    laneItems[lane].push({ el, kind: "avatar", data: a, lane });
  }

  // umwelt #323: allNavItems — every laneItems entry merged and sorted by
  // its own #street-frame x via streetX() (umwelt #1132; not lane/source order),
  // built here (after every item above is already appended to the DOM) so
  // ArrowLeft/ArrowRight (moveCursor) can walk the street in actual
  // left-to-right screen position, independent of lane. See this file's
  // module-header note ("umwelt #323") for the full design.
  allNavItems = [...laneItems.sky, ...laneItems.land, ...laneItems.road]
    .sort((a, b) => streetX(a.el) - streetX(b.el));

  // umwelt #342: navColumns — allNavItems clustered by on-screen-x
  // proximity (buildNavColumns, this file's "umwelt #342" module-header
  // note) so ArrowLeft/ArrowRight (moveCursor) walk COLUMNS, not items —
  // two same-x items in different lanes now cost ONE keypress to pass.
  navColumns = buildNavColumns(allNavItems);

  clampCursor();
  updateCursorHighlight();
}

// ---------------------------------------------------------------------
// Feature 1: crossings sidebar (polling).
//
// Poll cursor semantics: `crossingsCursor` tracks the last-seen
// `to_position` from a `/crossings` response — this is where `cursor`
// finally gets real meaning (M0's `/scene` response only carried a
// placeholder `unseen_count`, unrelated to this cursor). Newest crossing
// renders at the BOTTOM of the list (a judgment call — either end is a
// reasonable read of "sidebar", picked bottom to match a chat-style feed
// and auto-scrolled into view on every update).
// ---------------------------------------------------------------------

const crossingsListEl = document.getElementById("crossings-list");
let crossingsTimer = null;
let crossingsCursor = 0;

// umwelt #286: the sidebar never capped its own DOM — over a long session
// in a busy place, every distinct crossing ever seen stayed in the DOM
// forever (only a full place-change ever cleared it, via
// loadCrossingsInitial's innerHTML=""). Confirmed live (Playwright, mobile
// viewport) that this is a genuine unbounded-growth leak, not just a
// theoretical one — exactly the shape that would crash a memory-
// constrained mobile browser after a long, busy session (a real user
// report). `since`/`crossingsCursor` themselves are correct (verified
// against the real gateway route) — this is a DOM-retention cap, not a
// polling/cursor fix. Trim from the FRONT (oldest first) so the visible
// tail (most recent, what the auto-scroll keeps in view) is always intact.
const MAX_RENDERED_CROSSINGS = 200;

function stopCrossingsPolling() {
  if (crossingsTimer !== null) {
    clearInterval(crossingsTimer);
    crossingsTimer = null;
  }
}

// umwelt#788 (Lei's direct report, 2026-09-08): shared muted-timestamp
// helper for both crossing-rendering surfaces (the always-visible room
// panel and the inbox), so the two agree. received_at is the engine-
// stamped, trustworthy world clock (see server.py's own comment at its
// crossing-mint site); sent_at is caller-supplied/display-only and used
// only as a fallback for the rare crossing whose received_at is still
// null (nullable in schema). Same-day (viewer's local zone) -> `HH:mm`,
// else `MM-DD HH:mm`; full ISO returned separately for a `title=` hover.
function formatCrossingTime(c) {
  const iso = c.received_at || c.sent_at;
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const pad = (n) => String(n).padStart(2, "0");
  const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const text = sameDay ? hhmm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hhmm}`;
  return { text, title: d.toISOString() };
}

function renderCrossing(c) {
  const item = document.createElement("div");
  item.className = "crossing";

  const rel = document.createElement("span");
  rel.className = "relation";
  rel.textContent = c.viewer_relation || "";

  const sender = document.createElement("span");
  sender.className = "sender";
  sender.textContent = c.sender || "";

  const crossingTime = formatCrossingTime(c);
  let time = null;
  if (crossingTime) {
    time = document.createElement("span");
    time.className = "time";
    time.textContent = crossingTime.text;
    time.title = crossingTime.title;
  }

  const body = document.createElement("div");
  body.className = "body";
  body.textContent = c.body || "";

  // voice-playback v1 (forum #282, manifest-fetch transport): a plain
  // button per crossing that plays back the crossing's body as sectioned
  // speech (POST /voice/manifest -> per-section POST /voice/section,
  // sequential <audio> elements, prefetch-next-during-current -- see
  // actVoice below). GADGETS.md §7.1 names this the play-button's own
  // gadget-adoption point; per its composition rule ("the crossing
  // play-button is a button inside the crossing row -- no new class")
  // this stays the row's own small accessory button (its existing
  // `.voice-btn` sizing/chrome, deliberately smaller/quieter than the
  // full `.gg-btn` recipe -- see index.html's own comment on that scale
  // choice), not a new ggButtonRow factory instance.
  const voiceBtn = document.createElement("button");
  voiceBtn.type = "button";
  voiceBtn.className = "voice-btn";
  voiceBtn.textContent = "▶";
  voiceBtn.title = "listen";
  voiceBtn.addEventListener("click", () => actVoice(c.id, voiceBtn));

  // umwelt #387 (Lei's live ask, issue #387): the reply affordance -- see
  // openReplyToCrossing above for the quote-prefill it opens. Same
  // "small accessory button inside the crossing row" composition as
  // voiceBtn above (GADGETS.md §7.1's rule for this row), its own
  // `.reply-btn` sizing/chrome (index.html), always-visible per that same
  // rule (see index.html's comment on why this isn't hover-reveal).
  const replyBtn = document.createElement("button");
  replyBtn.type = "button";
  replyBtn.className = "reply-btn";
  replyBtn.textContent = "↩";
  replyBtn.title = "reply";
  replyBtn.addEventListener("click", () => openReplyToCrossing(c));

  item.appendChild(rel);
  item.appendChild(sender);
  if (time) item.appendChild(time);
  item.appendChild(body);
  item.appendChild(voiceBtn);
  item.appendChild(replyBtn);
  crossingsListEl.appendChild(item);

  // Both call sites (loadCrossingsInitial's initial backlog fetch AND
  // startCrossingsPolling's recurring tick) funnel through this one
  // function, so capping here bounds the DOM regardless of which path
  // pushed it over the limit.
  while (crossingsListEl.children.length > MAX_RENDERED_CROSSINGS) {
    crossingsListEl.removeChild(crossingsListEl.firstChild);
  }
}

async function fetchCrossings(place, since) {
  const params = new URLSearchParams({
    place, since: String(since), viewer: state.viewer,
  });
  let resp;
  try {
    resp = await fetch(`/crossings?${params.toString()}`);
  } catch (e) {
    return null;
  }
  if (!resp.ok) {
    return null;
  }
  return resp.json();
}

async function loadCrossingsInitial(place) {
  crossingsListEl.innerHTML = "";
  crossingsCursor = 0;
  const data = await fetchCrossings(place, 0);
  if (data) {
    for (const c of data.crossings || []) renderCrossing(c);
    if (data.to_position !== null && data.to_position !== undefined) {
      crossingsCursor = data.to_position;
    }
    crossingsListEl.scrollTop = crossingsListEl.scrollHeight;
  }
  startCrossingsPolling(place);
}

function startCrossingsPolling(place) {
  // Always tear down any prior timer first — startCrossingsPolling is only
  // ever called from loadCrossingsInitial, which itself only runs after
  // loadScene()'s own stopCrossingsPolling() call, but this second guard
  // keeps the invariant ("at most one live timer") true even if that call
  // order ever changes.
  stopCrossingsPolling();
  crossingsTimer = setInterval(async () => {
    const data = await fetchCrossings(place, crossingsCursor);
    if (!data) return;
    const newCrossings = data.crossings || [];
    if (newCrossings.length) {
      for (const c of newCrossings) renderCrossing(c);
      crossingsListEl.scrollTop = crossingsListEl.scrollHeight;
    }
    if (data.to_position !== null && data.to_position !== undefined) {
      crossingsCursor = data.to_position;
    }
  }, 2000);
}

// ---------------------------------------------------------------------
// umwelt#529/#539: the human comms surface -- inbox bell (reactive) +
// inbox panel (proactive), both consumers of gateway.py's GET /inbox
// (see that route's own docstring for the since/to_position contract).
// Building one does not give the other (#539's own #541 docstring), but
// they share one poll + one cursor here, same avatar owning both halves.
// ---------------------------------------------------------------------

const inboxBellBtnEl = document.getElementById("inbox-bell-btn");
const inboxBellIconEl = document.getElementById("inbox-bell-icon");
const inboxBadgeEl = document.getElementById("inbox-badge");

let inboxTimer = null;
let inboxCursor = null; // null = "everything" (no since sent yet)
let inboxUnseenCount = 0;
let inboxUnseenHasWake = false; // any unseen item carried a granted wake
// umwelt#1064: bumped each time openInbox() APPLIES a snapshot (non-null
// data only). A poll tick captures it before its fetch; if it moved by
// the time the tick resolves, the open already showed (and moved the
// cursor past) whatever that tick fetched, so the tick drops its result.
let inboxOpenGen = 0;
let inboxWindow = null; // lazily created ggWindow (gadgets.js), reused

// umwelt#787 part 2: the unread model's own state. `inboxCursor` above is
// NOT a read cursor -- it tracks what the badge has counted, and
// startInboxPolling advances it on every 5s tick whether or not a human
// ever opened the panel (see that function's own comment). Building
// unread on top of it would make every item permanently "already read"
// with no error and no failing test. `inboxReadSeq` is the SEPARATE
// value: the highest crossing `seq` the human has actually seen inside an
// OPENED panel. `inboxShowAll` is not part of the read model at all --
// it's a pure display toggle, deliberately not persisted (see below).
let inboxReadSeq = null; // null = "everything is unread" (no open yet)
let inboxShowAll = false; // module-level, NOT persisted -- resets to
// unread-only on every page load, same as every other in-memory-only UI
// toggle in this file (no localStorage key for it, unlike inboxReadSeq).
let inboxLastData = null; // umwelt#787 part 2: cache of the last fetched
// /inbox response, so "Mark all read" and the Show all/unread-only toggle
// can re-render from already-fetched data without a refetch.

// umwelt#529 (Zephyr's design, inherited): the cursor is caller-tracked
// (localStorage), keyed by avatar -- there is no server-side per-human
// read-cursor mechanism (see /inbox's own docstring on why). Keying by
// avatar, not a single global key, is what keeps a login-as-a-different-
// human on the same browser from inheriting a stranger's cursor.
function inboxCursorKey() {
  return `umwelt_inbox_cursor_${state.identity}`;
}

function loadInboxCursor() {
  if (state.identity === "spectator") return null;
  const raw = window.localStorage.getItem(inboxCursorKey());
  if (raw === null) return null;
  const n = parseInt(raw, 10);
  return Number.isFinite(n) ? n : null;
}

function saveInboxCursor(position) {
  if (state.identity === "spectator" || position === null || position === undefined) return;
  window.localStorage.setItem(inboxCursorKey(), String(position));
}

// umwelt#787 part 2: `inboxReadSeq`'s own persistence -- same
// spectator guard, same Number.isFinite parse guard, same
// never-persist-for-spectator rule as inboxCursor's pair above, keyed by
// avatar the same way. This is deliberately a SEPARATE localStorage key
// from inboxCursorKey() -- the two values track different things (see
// the state comment above) and must never share a key or a variable.
function inboxReadSeqKey() {
  return `umwelt_inbox_read_${state.identity}`;
}

function loadInboxReadSeq() {
  if (state.identity === "spectator") return null;
  const raw = window.localStorage.getItem(inboxReadSeqKey());
  if (raw === null) return null;
  const n = parseInt(raw, 10);
  return Number.isFinite(n) ? n : null;
}

function saveInboxReadSeq(position) {
  if (state.identity === "spectator" || position === null || position === undefined) return;
  window.localStorage.setItem(inboxReadSeqKey(), String(position));
}

async function fetchInbox(since) {
  const params = since !== null && since !== undefined
    ? `?since=${encodeURIComponent(String(since))}` : "";
  let resp;
  try {
    resp = await fetch(`/inbox${params}`);
  } catch (e) {
    return null; // network hiccup -- next poll tries again, cursor untouched
  }
  if (!resp.ok) return null;
  return resp.json();
}

// A crossing carries the "was a wake granted" signal on two disjoint
// paths (umwelt#119): the 1:1 to_agent path's own `wake_granted` bool, or
// the @-mention path's `wakes_granted` list (this avatar's name in it).
// Matches wake_rate_ok's/the bell's own db-layer reasoning for the same
// OR -- see db.py's wake_rate_ok docstring.
function crossingHasGrantedWake(c) {
  return Boolean(c.wake_granted) ||
    (Array.isArray(c.wakes_granted) && c.wakes_granted.includes(state.identity));
}

function renderInboxBell() {
  inboxBellIconEl.textContent = inboxUnseenHasWake ? "🔔" : "📬";
  if (inboxUnseenCount > 0) {
    inboxBadgeEl.textContent = inboxUnseenCount > 99 ? "99+" : String(inboxUnseenCount);
    inboxBadgeEl.classList.remove("hidden");
  } else {
    inboxBadgeEl.classList.add("hidden");
  }
}

// umwelt#787 part 2: combines the two /inbox buckets (`to_agent` and
// `mentions`) into ONE seq-ordered list. This is the actual fix for Lei's
// bug -- the two buckets used to render as stacked sections ("Direct to
// you" then "Mentioned you"), so a mention could only ever appear BELOW
// every direct row, however recent it was. Tagging `kind` here (rather
// than re-deriving which bucket an item came from later) is what lets the
// per-row chip and the divider/unread logic below stay bucket-agnostic.
function mergeInboxItems(data) {
  const toAgent = ((data && data.to_agent) || []).map((c) => ({ ...c, kind: "direct" }));
  const mentions = ((data && data.mentions) || []).map((c) => ({ ...c, kind: "mention" }));
  const merged = [...toAgent, ...mentions];
  merged.sort((a, b) => {
    // Missing/non-finite seq sorts LAST and must not poison the
    // comparator with NaN (spec: do not rely on `-` on undefined).
    const aFinite = Number.isFinite(a.seq);
    const bFinite = Number.isFinite(b.seq);
    if (aFinite && bFinite) return b.seq - a.seq; // DESC, newest first
    if (aFinite) return -1;
    if (bFinite) return 1;
    return 0;
  });
  return merged;
}

// umwelt#787 part 2: `boundary` is `inboxReadSeq` (the OPEN-boundary),
// never `inboxCursor` (the POLL cursor) -- see the state comment above
// this block for why conflating the two silently breaks the whole
// feature. `boundary === null` means "no open yet", i.e. everything is
// unread.
function isInboxItemUnread(item, boundary) {
  return boundary === null || item.seq > boundary;
}

// Highest finite seq across `items`, or null for an empty/all-non-finite
// list -- used both to advance `inboxReadSeq` on open and by "Mark all
// read".
function inboxMaxSeq(items) {
  let max = null;
  for (const item of items) {
    if (Number.isFinite(item.seq) && (max === null || item.seq > max)) {
      max = item.seq;
    }
  }
  return max;
}

// umwelt#787 part 2: pure planning step (deliberately DOM-free) -- decides
// which rows renderInboxContent should draw and where the "New since you
// last looked" divider goes, if anywhere. Kept separate from DOM
// construction so this logic is real-execution-testable under Node (this
// suite has no headless-browser/DOM harness -- same no-headless-browser
// limitation documented elsewhere in this file's own tests).
function planInboxRows(merged, boundary, showAll) {
  const unreadItems = merged.filter((item) => isInboxItemUnread(item, boundary));
  const unreadCount = unreadItems.length;
  let rows;
  let showNothingNewNote = false;
  if (showAll) {
    rows = merged;
  } else if (unreadCount > 0) {
    rows = unreadItems;
  } else if (merged.length > 0) {
    // Unread-empty must not render a blank panel -- fall back to the
    // most recent 20 items with a "Nothing new" note.
    rows = merged.slice(0, 20);
    showNothingNewNote = true;
  } else {
    rows = [];
  }
  // The divider sits between the last unread and first read row, and only
  // when BOTH groups are non-empty. `merged` is seq-DESC and unread items
  // always sort before read items (see mergeInboxItems/isInboxItemUnread),
  // so in "show all" mode the split is exactly at index `unreadCount`.
  const dividerIndex = (showAll && unreadCount > 0 && unreadCount < rows.length)
    ? unreadCount : -1;
  return { rows, unreadCount, dividerIndex, showNothingNewNote };
}

function renderInboxCrossingItem(c, opts) {
  const unread = Boolean(opts && opts.unread);
  const item = document.createElement("div");
  item.className = unread ? "inbox-item inbox-item-unread" : "inbox-item";

  const header = document.createElement("div");
  header.className = "inbox-item-header";
  const sender = document.createElement("span");
  sender.className = "inbox-item-sender";
  sender.textContent = c.sender || "";
  header.appendChild(sender);
  const kindChip = document.createElement("span");
  kindChip.className = "inbox-item-kind-chip";
  kindChip.textContent = c.kind === "mention" ? "@mention" : "direct";
  header.appendChild(kindChip);
  const crossingTime = formatCrossingTime(c);
  if (crossingTime) {
    const time = document.createElement("span");
    time.className = "inbox-item-time";
    time.textContent = crossingTime.text;
    time.title = crossingTime.title;
    header.appendChild(time);
  }
  if (crossingHasGrantedWake(c)) {
    const bell = document.createElement("span");
    bell.textContent = "🔔";
    bell.title = "woke you";
    header.appendChild(bell);
  }
  item.appendChild(header);

  const body = document.createElement("div");
  body.className = "inbox-item-body";
  body.textContent = c.body || "";
  item.appendChild(body);

  return item;
}

// umwelt#787 part 2: `opts` is `{ boundary, showAll }`. One merged list,
// newest first, unread rows visually distinguished, with a header row
// (unread count / Mark all read / Show all toggle) above it. Replaces the
// old two-stacked-sections layout, which was Lei's actual bug -- a
// mention newer than all 41 of his direct rows still rendered BELOW every
// one of them.
function renderInboxContent(data, opts) {
  const boundary = opts && opts.boundary !== undefined ? opts.boundary : null;
  const showAll = Boolean(opts && opts.showAll);
  const merged = mergeInboxItems(data);
  const plan = planInboxRows(merged, boundary, showAll);

  const frag = document.createDocumentFragment();

  const header = document.createElement("div");
  header.className = "inbox-header";
  const countEl = document.createElement("span");
  countEl.className = "inbox-header-count";
  countEl.textContent = plan.unreadCount === 1 ? "1 unread" : `${plan.unreadCount} unread`;
  header.appendChild(countEl);
  const markAllBtn = document.createElement("button");
  markAllBtn.className = "inbox-mark-all-read-btn";
  markAllBtn.textContent = "Mark all read";
  markAllBtn.addEventListener("click", markAllInboxRead);
  header.appendChild(markAllBtn);
  const toggleBtn = document.createElement("button");
  toggleBtn.className = "inbox-toggle-showall-btn";
  toggleBtn.textContent = showAll ? "Show unread only" : "Show all";
  toggleBtn.addEventListener("click", toggleInboxShowAll);
  header.appendChild(toggleBtn);
  frag.appendChild(header);

  if (plan.rows.length === 0) {
    const empty = document.createElement("div");
    empty.className = "inbox-empty";
    empty.textContent = "Nothing addressed to you yet.";
    frag.appendChild(empty);
    return frag;
  }

  if (plan.showNothingNewNote) {
    const note = document.createElement("div");
    note.className = "inbox-empty inbox-nothing-new";
    note.textContent = "Nothing new.";
    frag.appendChild(note);
  }

  plan.rows.forEach((item, i) => {
    if (i === plan.dividerIndex) {
      const divider = document.createElement("div");
      divider.className = "inbox-divider";
      divider.textContent = "New since you last looked";
      frag.appendChild(divider);
    }
    frag.appendChild(renderInboxCrossingItem(item, { unread: isInboxItemUnread(item, boundary) }));
  });

  return frag;
}

function getInboxWindow() {
  if (!inboxWindow) {
    inboxWindow = ggWindow({
      title: "Inbox",
      anchor: { mode: "screen", pos: { top: "3rem", right: "1rem" } },
      size: { maxWidth: "min(360px, 90vw)", maxHeight: "60vh" },
      closable: true,
    });
  }
  return inboxWindow;
}

// umwelt#787 part 2: re-renders the panel from the already-fetched
// `inboxLastData` -- no refetch. Shared by the toggle and "Mark all read"
// handlers below.
function rerenderInboxPanel() {
  const win = getInboxWindow();
  win.update({
    content: inboxLastData
      ? renderInboxContent(inboxLastData, { boundary: inboxReadSeq, showAll: inboxShowAll })
      : "Could not load inbox.",
  });
}

function toggleInboxShowAll() {
  inboxShowAll = !inboxShowAll;
  rerenderInboxPanel();
}

function markAllInboxRead() {
  if (!inboxLastData) return;
  inboxReadSeq = inboxMaxSeq(mergeInboxItems(inboxLastData));
  saveInboxReadSeq(inboxReadSeq);
  // umwelt#800 round 5 (Borges): deliberately NOT zeroing inboxUnseenCount /
  // inboxUnseenHasWake here. Both are only ever bumped by poll ticks, and
  // openInbox() resets them to 0 at the same moment it takes the
  // `inboxLastData` snapshot -- so whatever they hold now was delivered
  // AFTER that snapshot, i.e. is newer than the boundary just set, and was
  // never displayed. Zeroing would drop the one prompt to reopen.
  renderInboxBell();
  rerenderInboxPanel();
}

// Opening the panel is also how the BADGE is marked read: umwelt#529 has
// no server-side read-cursor to advance (see /inbox's docstring), so
// "seen" (for the badge/inboxCursor) is purely local -- clearing the
// badge here is the whole mechanism, not a display nicety layered on top
// of a real one. umwelt#787 part 2 adds a SECOND, independent read
// boundary (`inboxReadSeq`) for the unread-model divider/filtering above
// -- capture-then-swap: `boundary` is captured BEFORE this open's own
// advance, so this open renders "new since the PREVIOUS open", not
// against a line this same open is about to draw.
async function openInbox() {
  const win = getInboxWindow();
  const identityAtRequest = state.identity;
  const data = await fetchInbox(null); // full inbox, not just the unseen delta
  // umwelt#800 delta-round (reviewer, round 3): the human can log out --
  // or switch avatars -- while this fetch is in flight. Nothing else in
  // this function re-checks identity after an await, so an unguarded
  // continuation would paint a stale avatar's inbox on top of a login
  // gate that has since gone back up (inboxWindow anchors to
  // document.body, not inside the gate-hidden #street -- it paints OVER
  // the gate, not behind it), and/or persist that avatar's read boundary
  // under the new identity's storage key. Bail out of BOTH halves below
  // -- no win.update()/win.open() and no state writes -- the moment
  // identity has moved out from under this request.
  if (state.identity !== identityAtRequest || state.identity === "spectator") return;
  const boundary = inboxReadSeq;
  win.update({
    content: data
      ? renderInboxContent(data, { boundary, showAll: inboxShowAll })
      : "Could not load inbox.",
  });
  win.open();
  // Reviewer-fairy catch (umwelt#546): only clear the badge on a genuine
  // load -- a null `data` (network/session hiccup) must not read as "seen"
  // when the user never actually saw anything. The cursor is already
  // correctly gated the same way below; this mirrors it, and so does the
  // new `inboxReadSeq` write.
  if (data) {
    inboxOpenGen++; // umwelt#1064: invalidate any poll tick still in flight
    inboxLastData = data;
    inboxUnseenCount = 0;
    inboxUnseenHasWake = false;
    renderInboxBell();
    inboxReadSeq = inboxMaxSeq(mergeInboxItems(data));
    saveInboxReadSeq(inboxReadSeq);
    if (data.to_position !== null && data.to_position !== undefined) {
      inboxCursor = data.to_position;
      saveInboxCursor(inboxCursor);
    }
  }
}

inboxBellBtnEl.addEventListener("click", openInbox);

function stopInboxPolling() {
  if (inboxTimer !== null) {
    clearInterval(inboxTimer);
    inboxTimer = null;
  }
}

// umwelt#529: ~5s poll (Zephyr's design) -- coarser than crossings' 2s
// since this checks a background signal, not an actively-open view.
function startInboxPolling() {
  stopInboxPolling(); // same belt-and-suspenders guard as crossings' own
  inboxCursor = loadInboxCursor();
  inboxReadSeq = loadInboxReadSeq(); // umwelt#787 part 2: (re-)seed the read boundary the same way, per avatar
  inboxTimer = setInterval(async () => {
    const identityAtRequest = state.identity;
    const openGenAtRequest = inboxOpenGen; // umwelt#1064
    const data = await fetchInbox(inboxCursor);
    // umwelt#800 delta-round (reviewer, round 3): clearInterval
    // (stopInboxPolling) only stops FUTURE ticks -- it cannot cancel a
    // tick that already started and is sitting on this await when
    // logout()/an avatar switch fires mid-flight. Same bail as
    // openInbox(): skip every state write below if identity moved out
    // from under this tick.
    // umwelt#1064: same shape of race against openInbox() -- if an open
    // applied its snapshot while this tick was in flight, the open already
    // shows these items and moved inboxCursor to its own (possibly newer)
    // to_position. Writing this tick's result would re-badge them and could
    // rewind the cursor. Drop it: nothing is lost, the NEXT tick fetches
    // from the open's cursor.
    if (!data || state.identity !== identityAtRequest || state.identity === "spectator"
        || inboxOpenGen !== openGenAtRequest) return;
    const fresh = [...(data.to_agent || []), ...(data.mentions || [])];
    if (fresh.length) {
      inboxUnseenCount += fresh.length;
      if (fresh.some(crossingHasGrantedWake)) inboxUnseenHasWake = true;
      renderInboxBell();
    }
    if (data.to_position !== null && data.to_position !== undefined) {
      inboxCursor = data.to_position;
      saveInboxCursor(inboxCursor);
    }
  }, 5000);
}

// ---------------------------------------------------------------------
// Feature 2: click-to-inspect panel.
// ---------------------------------------------------------------------

const inspectPanelEl = document.getElementById("inspect-panel");

// umwelt #936: #composer is position:sticky (it rides with the room's
// height) while #inspect-panel is position:fixed at the viewport bottom, so
// the composer can sit on top of the panel's rows. Lift the panel's bottom
// edge onto the composer's top instead of layering (no z-index: the
// GADGETS layering model is untouched). A zero-size rect means the composer
// is absent / display:none (its top would read 0 and push the panel
// offscreen at bottom = innerHeight), so that case is "no composer" -> 0.
// A composer below the viewport gives a negative inset, clamped to 0.
// umwelt #941: a lifted panel also keeps its top edge on-screen -- its
// max-height is capped to the room above the composer (min with the CSS
// 40vh), so a tall composer shrinks the panel (its contents scroll) instead
// of pushing the title and close button above the viewport. When the panel
// is not lifted the inline cap is cleared so the CSS 40vh applies again.
function syncInspectPanelInset() {
  const rect = composerEl ? composerEl.getBoundingClientRect() : null;
  if (!rect || (rect.width === 0 && rect.height === 0)) {
    inspectPanelEl.style.bottom = "0px";
    inspectPanelEl.style.maxHeight = "";
    return;
  }
  const inset = Math.max(0, window.innerHeight - rect.top);
  inspectPanelEl.style.bottom = inset + "px";
  inspectPanelEl.style.maxHeight = inset > 0
    ? Math.max(0, Math.min(0.4 * window.innerHeight, rect.top)) + "px"
    : "";
}
window.addEventListener("resize", () => {
  if (!inspectPanelEl.classList.contains("hidden")) syncInspectPanelInset();
});
window.addEventListener("scroll", () => {
  if (!inspectPanelEl.classList.contains("hidden")) syncInspectPanelInset();
}, { passive: true });
// The composer auto-grows while typing (and can end up in flow when the
// room shrinks) with no window resize/scroll, so observe its size too. The
// on-screen keyboard changes the visual viewport without a window resize.
// Created once here (not per show); both are no-ops while the panel is
// hidden, and absent APIs are skipped silently.
if (typeof ResizeObserver === "function" && composerEl) {
  new ResizeObserver(() => {
    if (inspectPanelEl.classList.contains("hidden")) return;
    syncInspectPanelInset();
  }).observe(composerEl);
}
if (window.visualViewport) {
  for (const evName of ["resize", "scroll"]) {
    window.visualViewport.addEventListener(evName, () => {
      if (inspectPanelEl.classList.contains("hidden")) return;
      syncInspectPanelInset();
    });
  }
}
const inspectNameEl = document.getElementById("inspect-name");
const inspectKindEl = document.getElementById("inspect-kind");
const inspectDescEl = document.getElementById("inspect-desc");
const inspectGateEl = document.getElementById("inspect-gate");
const inspectChecklistEl = document.getElementById("inspect-checklist");
const inspectChargeBoardEl = document.getElementById("inspect-charge-board");
const inspectContentsEl = document.getElementById("inspect-contents");
const inspectStatusEl = document.getElementById("inspect-status");
const inspectEmotionEl = document.getElementById("inspect-emotion");
const inspectStateEl = document.getElementById("inspect-state");
const inspectLastActiveEl = document.getElementById("inspect-last-active");
const inspectActivityLineEl = document.getElementById("inspect-activity-line");
const inspectCloseBtn = document.getElementById("inspect-close");

async function inspectThing(thingId) {
  let resp;
  try {
    resp = await fetch(`/inspect?thing=${encodeURIComponent(thingId)}`);
  } catch (e) {
    errorEl.textContent = `request failed: ${e}`;
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    errorEl.textContent = body.error || `error ${resp.status}`;
    return;
  }
  const data = await resp.json();
  renderInspect(data);
}

// umwelt #516: `look_in` — POST /look_in {container}. Reuses the inspect
// panel (the detail surface) but renders the container's CONTENTS as a
// list rather than one thing's own fields. The response carries no
// container description (only {things, load, capacity}), so the load line
// goes in the desc slot and each contained thing becomes its own row.
// Same csrfToken-presence guard / fetch-try-catch / !resp.ok handling as
// actUse; identity comes from the session cookie server-side, so the
// container id is the only body field.
async function lookInThing(thingId, displayName) {
  if (!state.csrfToken) {
    errorEl.textContent = "look in failed: no active session (log in first)";
    consoleLog("error", "look in failed: no active session (log in first)");
    return false;
  }
  let resp;
  try {
    resp = await fetch("/look_in", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Csrf-Token": state.csrfToken,
      },
      body: JSON.stringify({ container: thingId }),
    });
  } catch (e) {
    errorEl.textContent = `look in failed: ${e}`;
    consoleLog("error", `look in failed: ${e}`);
    return false;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    const msg = body.error || `look in error ${resp.status}`;
    errorEl.textContent = msg;
    consoleLog("error", `look in failed: ${msg}`);
    return false;
  }
  const data = await resp.json().catch(() => ({}));
  renderLookIn(data, displayName);
  consoleLog("look_in", `looked in ${displayName || thingId}`, data);
  return true;
}

// umwelt #976: the tray-row mark (renderLookIn: dim + badge) reads the SERVER's
// `closed: true` on the look_in row -- terminality is derived by the world from
// the row's kinddef (some action's `to` names the state and no action with a non-empty `from`
// lists it). The
// client keeps NO list of terminal words. Buttons are likewise not status-driven
// here: whether an action can run in the current state is the server's
// `available` flag (+ `reason`) from thing_actions.

// Render a container's contents into the inspect panel. Names/descriptions
// are world-content set by whoever forged the thing (attacker-influenceable
// public text per DESIGN.md §12.10), so every field is assigned via
// .textContent — never string-concatenated into markup.
function renderLookIn(data, containerName) {
  inspectNameEl.textContent = containerName || "";
  inspectKindEl.textContent = "container";
  if (data.load != null && data.capacity != null) {
    inspectDescEl.textContent = `carrying ${data.load} / ${data.capacity}`;
  } else {
    inspectDescEl.textContent = "";
  }
  inspectGateEl.textContent = "";
  inspectGateEl.style.display = "none";
  inspectChecklistEl.textContent = "";
  inspectChecklistEl.style.display = "none";
  inspectChargeBoardEl.textContent = "";
  inspectChargeBoardEl.style.display = "none";
  inspectStatusEl.style.display = "none";

  const things = Array.isArray(data.things) ? data.things : [];
  inspectContentsEl.textContent = "";
  if (things.length === 0) {
    const empty = document.createElement("div");
    empty.className = "inspect-contents-empty";
    empty.textContent = "empty";
    inspectContentsEl.appendChild(empty);
  } else {
    for (const it of things) {
      const row = document.createElement("div");
      row.className = "inspect-contents-item inspect-contents-item--openable";
      // umwelt #655: a contained thing is now openable in the proposal
      // viewer (degrades gracefully for non-proposals — no body if not
      // readable, no action bar if untyped). div+click+role=button rather
      // than a <button> element to keep the existing `.inspect-contents-
      // item` row styling (border-top/padding) and match buildThingElement's
      // div+click convention for clickable on-screen things; Enter/Space
      // keyboard-activate for free via the keydown handler. Name/desc stay
      // .textContent-only (DESIGN.md §12.10 — attacker-influenceable public
      // text), never string-concatenated into markup.
      row.setAttribute("role", "button");
      row.tabIndex = 0;
      const name = document.createElement("div");
      name.className = "inspect-contents-name";
      name.textContent = it.name || it.id || "";
      const desc = document.createElement("div");
      desc.className = "inspect-contents-desc";
      desc.textContent = it.description || "";
      row.appendChild(name);
      row.appendChild(desc);
      // umwelt #967: mark a closed proposal BEFORE any click -- dimmed, with a
      // badge gadget carrying its own status. Marked, not hidden: the row
      // stays openable so its history can still be read.
      if (it.closed === true) {
        row.classList.add("inspect-contents-item--closed");
        ggBadge({ kind: "closed", text: it.status, parentEl: name });
      }
      // stopPropagation: openThingViewer opens its ggWindow synchronously,
      // so without it this same click bubbles on to gadgets.js's
      // document-level outside-click listener, which sees a target outside
      // the just-opened viewer and closes it before it paints (the row
      // looked dead). Same class as #console-toggle's PR #355 fix.
      // Stopping the click also hides it from the interact menu's own
      // document-level dismiss, so close that menu here explicitly to keep
      // the pre-fix behaviour (a row click never leaves a stale menu open).
      // umwelt #936: a row is also the selection, from this container. Its
      // own listeners, registered BEFORE the viewer-opening ones below (same
      // element, so they run first and in order), leave those handlers
      // exactly as they were.
      const rowWhere = "container:" + (containerName || "?");
      row.addEventListener("click", () => selectThing(it.id, it.name, rowWhere));
      row.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          selectThing(it.id, it.name, rowWhere);
        }
      });
      row.addEventListener("click", (ev) => {
        ev.stopPropagation();
        closeInteractMenu();
        openThingViewer(it.id, it.name);
      });
      row.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          openThingViewer(it.id, it.name);
        }
      });
      inspectContentsEl.appendChild(row);
    }
  }
  inspectContentsEl.style.display = "block";
  inspectPanelEl.classList.remove("hidden");
  syncInspectPanelInset();
}

function renderInspect(data) {
  const t = data.thing || {};
  inspectNameEl.textContent = t.name || "";
  inspectKindEl.textContent = t.kind || "";
  inspectDescEl.textContent = t.description || "";

  inspectGateEl.textContent = t.gate_notice || "";
  inspectGateEl.style.display = t.gate_notice ? "block" : "none";

  inspectChecklistEl.textContent = data.checklist_render || "";
  inspectChecklistEl.style.display = data.checklist_render ? "block" : "none";

  inspectChargeBoardEl.textContent = data.charge_board_render || "";
  inspectChargeBoardEl.style.display = data.charge_board_render ? "block" : "none";

  // umwelt #516: look_in's contents list is not part of a plain look_at —
  // clear it so a prior look_in's list doesn't linger behind the next inspect.
  inspectContentsEl.textContent = "";
  inspectContentsEl.style.display = "none";

  // `status` (avatar-kind things only, look_at's sibling key — umwelt
  // #133/#239) — distinct per-field rows, each assigned via .textContent,
  // never string-concatenated into markup (DESIGN.md §12.10: emotion/
  // state/activity_line are all attacker-influenceable public text).
  const status = data.status;
  if (status) {
    inspectEmotionEl.textContent = `emotion: ${status.emotion || ""}`;

    // `state` (Phase 2, #268) is present only once the avatar has ever
    // sent a heartbeat — server.py's _avatar_status omits it entirely
    // (never nulls it) beforehand. Plain fallback row, never an error or
    // "undefined" in the UI.
    inspectStateEl.textContent = status.state
      ? `state: ${status.state}`
      : "no live state yet";

    inspectLastActiveEl.textContent = status.last_active_at
      ? `last active: ${status.last_active_at}`
      : "last active: never";

    // activity_line: omitted entirely when N/A (same convention
    // _avatar_status itself uses) — hide the row rather than show an
    // empty one. The single highest-risk field per DESIGN.md §12.10:
    // plain textContent only.
    if (status.activity_line) {
      inspectActivityLineEl.textContent = status.activity_line;
      inspectActivityLineEl.style.display = "block";
    } else {
      inspectActivityLineEl.textContent = "";
      inspectActivityLineEl.style.display = "none";
    }
  } else {
    inspectEmotionEl.textContent = "";
    inspectStateEl.textContent = "";
    inspectLastActiveEl.textContent = "";
    inspectActivityLineEl.textContent = "";
    inspectActivityLineEl.style.display = "none";
  }
  inspectStatusEl.style.display = status ? "block" : "none";

  inspectPanelEl.classList.remove("hidden");
  syncInspectPanelInset();
}

inspectCloseBtn.addEventListener("click", () => {
  inspectPanelEl.classList.add("hidden");
});

// Click-elsewhere-to-dismiss: any click NOT inside the panel (and not on a
// thing cell, which stopPropagation()s its own click above) closes it.
// Tested on the click's dispatch-time path (umwelt #945; see gadgets.js's
// dismiss listener): a click inside that re-renders its own target is
// still inside.
document.addEventListener("click", (ev) => {
  if (!inspectPanelEl.classList.contains("hidden")
      && !ev.composedPath().includes(inspectPanelEl)) {
    inspectPanelEl.classList.add("hidden");
  }
});

// ---------------------------------------------------------------------
// umwelt #267 "menu PR": the interact-menu. Tap an avatar/thing (see the
// click listeners in buildThingElement/buildAvatarElement above) opens
// this instead of jumping straight to /inspect. Built purely from
// server-provided data — an avatar's presence (talk/examine are always
// offered, identical for every avatar regardless of `type`: no
// human-vs-agent branch anywhere in this file, per umwelt #252 §12.1b) or
// a thing's own `.actions` list (gateway.py's `_thing_actions`, already
// returned by /scene — never hardcoded here). Reuses the inspect panel's
// `.hidden`-toggle + outside-click-dismiss CONVENTION above (not its exact
// DOM ids — this is a genuinely new component, see index.html's
// `#interact-menu`).
// ---------------------------------------------------------------------

const interactMenuEl = document.getElementById("interact-menu");
const interactMenuTitleEl = document.getElementById("interact-menu-title");
const interactMenuActionsEl = document.getElementById("interact-menu-actions");
const interactMenuCloseEl = document.getElementById("interact-menu-close");
// umwelt #660: the interact-menu no longer carries a Talk sub-view of its
// own. Clicking an avatar's Talk now preselects Private with that agent in
// the one composer (selectPrivateChannel above) and closes the menu -- the
// gesture is preserved, the second hidden composer is not.

// Field names that get a full ggEditor (toolbar + resizable textarea)
// instead of a plain one-line text input, whenever a field's declared
// type doesn't already say otherwise (see _buildParamInput below) or --
// for act()'s flat `requires` list, which carries no type metadata at
// all (openActParamsSubView below) -- unconditionally. Defined ONCE and
// shared by both N-field-form mechanisms (GADGETS.md's `editor` gadget,
// per its own inventory row) rather than duplicated per call site.
const _GG_EDITOR_FIELD_NAMES = new Set([
  "text", "body", "content", "ask", "why", "summary", "description", "notes",
]);

// umwelt #1050: the ONE editor-vs-input decision for an act()-params field,
// shared by openActParamsSubView and _openViewerActParams (both call it, so
// they cannot drift). `comment` (any case) gets a multi-line editor WITHOUT
// the markdown toolbar: the viewer shows a stored comment as plain text, so
// the toolbar's ** / ## insertions would read as literal noise. It is
// deliberately NOT added to _GG_EDITOR_FIELD_NAMES (that set is the
// toolbar-editor set). Every other name keeps its pre-#1050 choice: toolbar
// editor for the set, a plain one-line <input> otherwise. Returns the element
// (ggEditor's `el` is a drop-in for <input>: live `.value`, `.focus()`), so
// each submit handler's `input.value.trim()` works unchanged. Enter in the
// textarea inserts a newline; no keydown handler captures it (the document-
// level Enter handler returns early for any TEXTAREA).
function _actFieldInput(fieldName) {
  const key = String(fieldName).toLowerCase();
  const placeholder = `Enter ${_titleCaseFieldName(fieldName)}…`;
  if (key === "comment") {
    return ggEditor({ rows: 6, toolbar: false, placeholder }).el;
  }
  if (_GG_EDITOR_FIELD_NAMES.has(key)) {
    return ggEditor({ placeholder }).el;
  }
  const input = document.createElement("input");
  input.type = "text";
  input.className = "params-form-input";
  return input;
}

// ---------------------------------------------------------------------
// umwelt #522: the interact-menu's ACTION-PARAMS sub-view — for a typed
// action whose `requires` is non-empty (gateway.py's
// _enrich_things_with_typed_actions merges these onto a thing's
// `.actions` list now, see openInteractMenuForThing below). Mirrors
// openTalkSubView's show/hide-INSIDE-#interact-menu convention (a sibling
// panel shown alongside the still-visible action-button list, never a
// floating window) rather than this file's OTHER "N-field form" system
// (_buildParamsFormContent/ggWindow, built for use()'s richer typed
// params_spec — type/enum/default/description per field). act()'s
// `requires` is a flat list of plain field-name strings with none of
// that, so a lighter one-text-input-per-field form is the right fit, and
// mirroring openTalkSubView is what the #522 spec calls for.
//
// Built PROGRAMMATICALLY here and appended into interactMenuEl, rather
// than as static markup in index.html —
// umwelt #522 coder-fairy judgment call, flagged in the PR handoff:
// Piece 3's spec'd scope is this file only, so no index.html/CSS edit is
// made. Reuses the EXISTING, class-scoped (not ID-scoped) params-form
// CSS (`.params-form-row`/`.params-form-label`/`.params-form-input`/
// `.params-form-run-row`, all defined above the use()-params-form builder
// further down this file) plus the global `.hidden` class every other
// show/hide toggle in this file already uses, so this renders reasonably
// without any new CSS.
// ---------------------------------------------------------------------

let interactMenuActParamsEl = null;
let interactMenuActParamsHeaderEl = null;
let interactMenuActParamsFieldsEl = null;
let interactMenuActParamsSubmitEl = null;

// Field-name -> label text: "target_agent" -> "Target Agent". Same
// underscore-to-space + per-word-capitalize shape gateway.py's own
// `verb.replace("_", " ").title()` uses for the action VERB's button
// label (`_enrich_things_with_typed_actions`) — applied here to each
// individual FIELD name instead, so the two title-casing conventions
// read the same way to a human even though they're two separate call
// sites (JS has no built-in `.title()`).
function _titleCaseFieldName(name) {
  return String(name).replace(/_/g, " ").replace(
    /\S+/g, (w) => w.charAt(0).toUpperCase() + w.slice(1));
}

// Lazily builds the sub-view's DOM once (idempotent) and appends it as a
// sibling inside #interact-menu.
function _ensureInteractMenuActParams() {
  if (interactMenuActParamsEl) return;
  interactMenuActParamsEl = document.createElement("div");
  interactMenuActParamsEl.id = "interact-menu-act-params";
  interactMenuActParamsEl.classList.add("hidden");

  interactMenuActParamsHeaderEl = document.createElement("div");
  interactMenuActParamsHeaderEl.id = "interact-menu-act-params-header";
  interactMenuActParamsEl.appendChild(interactMenuActParamsHeaderEl);

  interactMenuActParamsFieldsEl = document.createElement("div");
  interactMenuActParamsFieldsEl.id = "interact-menu-act-params-fields";
  interactMenuActParamsEl.appendChild(interactMenuActParamsFieldsEl);

  const btnRow = document.createElement("div");
  btnRow.className = "params-form-run-row";

  interactMenuActParamsSubmitEl = document.createElement("button");
  interactMenuActParamsSubmitEl.type = "button";
  interactMenuActParamsSubmitEl.textContent = "Submit";
  btnRow.appendChild(interactMenuActParamsSubmitEl);

  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", closeActParamsSubView);
  btnRow.appendChild(cancelBtn);

  interactMenuActParamsEl.appendChild(btnRow);
  interactMenuEl.appendChild(interactMenuActParamsEl);
}

// Hides the sub-view and clears its fields — "a way to cancel back to the
// action menu" (the action-button list itself was never hidden while the
// sub-view was open, same coexistence as openTalkSubView/talkBtn above).
function closeActParamsSubView() {
  if (!interactMenuActParamsEl) return;
  interactMenuActParamsEl.classList.add("hidden");
  interactMenuActParamsFieldsEl.innerHTML = "";
}

// Opens the N-field parameter form for `actionVerb` (one of `t.actions`,
// a typed action whose `requires` is non-empty) on `thingId`. ONE field
// per required field name, labelled with the title-cased field name, a
// Submit button, and Cancel (built above) — a plain text <input> for
// most fields, or a ggEditor (GADGETS.md's `editor` gadget) for a field
// whose name is in `_GG_EDITOR_FIELD_NAMES` (act()'s `requires` carries
// no type metadata at all, unlike use()'s params_spec below, so the
// field-name heuristic is the only signal available here). The only
// client-side validation is "field is non-empty" (umwelt #522 spec,
// explicitly out of scope for anything richer) — the server's own
// `requires` gate in act() is the real enforcement; this is purely UX.
function openActParamsSubView(thingId, actionVerb, requires, displayName) {
  _ensureInteractMenuActParams();
  closeEditSubView(); // umwelt #547: fresh act-params sub-view, no stale sibling sub-view
  interactMenuActParamsHeaderEl.textContent =
    `${_titleCaseFieldName(actionVerb)} — ${displayName}`;
  interactMenuActParamsFieldsEl.innerHTML = "";

  const inputsByField = {};
  for (const fieldName of requires) {
    const row = document.createElement("div");
    row.className = "params-form-row";
    const label = document.createElement("label");
    label.className = "params-form-label";
    label.textContent = _titleCaseFieldName(fieldName);
    row.appendChild(label);
    // editor-vs-input decision lives in _actFieldInput (umwelt #1050); the
    // submit handler's `input.value.trim()` below works on either element.
    const input = _actFieldInput(fieldName);
    row.appendChild(input);
    interactMenuActParamsFieldsEl.appendChild(row);
    inputsByField[fieldName] = input;
  }

  // umwelt#546 test-session finding (Lei, live): this used to close +
  // clear the form THE INSTANT Submit was clicked, before actAct's async
  // response even started -- so a rejected board_entry_add (e.g. text
  // over _BOARD_ENTRY_TEXT_MAX_LEN) destroyed the typed text right
  // alongside the failure it was reporting, with no recovery path (the
  // field's own DOM node is gone, so not even native undo survives it).
  // Fixed: await the real outcome, and only close on a genuine success
  // (resolved AND no effect_failures) -- a rejected submission leaves the
  // form, and whatever you typed, exactly as it was so you can fix and
  // resubmit without retyping everything.
  interactMenuActParamsSubmitEl.onclick = async () => {
    // "the client's job is just not to send an obviously empty required
    // field" (umwelt #522 spec) — a blank field is simply omitted rather
    // than sent as "", so the server's own required-arg gate (which
    // treats a present-but-blank string as missing too, see act()'s
    // `missing_args` check) reports the SAME field name back on a
    // still-missing value, never a different one.
    const formValues = {};
    for (const [fieldName, input] of Object.entries(inputsByField)) {
      const v = input.value.trim();
      if (v) formValues[fieldName] = v;
    }
    interactMenuActParamsSubmitEl.disabled = true; // no double-submit while in flight
    const outcome = await actAct(thingId, actionVerb, formValues, displayName);
    const failed = !outcome ||
      (Array.isArray(outcome.effect_failures) && outcome.effect_failures.length > 0);
    if (failed) {
      interactMenuActParamsSubmitEl.disabled = false; // form + typed text stay put
      return;
    }
    closeInteractMenu();
  };

  interactMenuActParamsEl.classList.remove("hidden");
  const firstInput = Object.values(inputsByField)[0];
  if (firstInput) firstInput.focus();
}

// ---------------------------------------------------------------------
// umwelt #547: the interact-menu's EDIT sub-view — the `edit` action on a
// `kind == "readable"` thing (gateway.py's `_thing_actions`). Mirrors
// openActParamsSubView/_ensureInteractMenuActParams above in structure and
// naming convention, just for a single pre-filled ggEditor field (the
// thing's `description`) instead of an N-field act()-params form.
// Pre-loads the thing's current description via the EXISTING `GET
// /inspect` route (same call shape inspectThing already uses — no new
// read route) before showing the editor, and saves through a new `edit`
// /act intent (gateway.py, a thin proxy onto `update_thing`'s own
// author-only-gated PATCH — never a new write route).
// ---------------------------------------------------------------------

let interactMenuEditEl = null;
let interactMenuEditHeaderEl = null;
let interactMenuEditBodyEl = null;
let interactMenuEditSaveEl = null;

// The currently-open sub-view's ggEditor instance, so the Save handler can
// read its live `.el.value` — null whenever the sub-view is closed.
let _currentEditEditor = null;

// Lazily builds the sub-view's DOM once (idempotent) and appends it as a
// sibling of interactMenuActParamsEl inside #interact-menu.
function _ensureInteractMenuEdit() {
  if (interactMenuEditEl) return;
  interactMenuEditEl = document.createElement("div");
  interactMenuEditEl.id = "interact-menu-edit";
  interactMenuEditEl.classList.add("hidden");

  interactMenuEditHeaderEl = document.createElement("div");
  interactMenuEditHeaderEl.id = "interact-menu-edit-header";
  interactMenuEditEl.appendChild(interactMenuEditHeaderEl);

  interactMenuEditBodyEl = document.createElement("div");
  interactMenuEditBodyEl.id = "interact-menu-edit-body";
  interactMenuEditEl.appendChild(interactMenuEditBodyEl);

  const btnRow = document.createElement("div");
  btnRow.className = "params-form-run-row";

  interactMenuEditSaveEl = document.createElement("button");
  interactMenuEditSaveEl.type = "button";
  interactMenuEditSaveEl.textContent = "Save";
  btnRow.appendChild(interactMenuEditSaveEl);

  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", closeEditSubView);
  btnRow.appendChild(cancelBtn);

  interactMenuEditEl.appendChild(btnRow);
  interactMenuEl.appendChild(interactMenuEditEl);
}

// Hides the sub-view and clears its content/tracked state — "a way to
// cancel back to the action menu", same posture closeActParamsSubView has.
function closeEditSubView() {
  if (!interactMenuEditEl) return;
  interactMenuEditEl.classList.add("hidden");
  interactMenuEditBodyEl.innerHTML = "";
  _currentEditEditor = null;
}

// Opens the Edit sub-view for a `readable` thing: pre-loads its current
// `description` via the existing GET /inspect route, then shows a single
// pre-filled ggEditor field + Save/Cancel. `displayName` mirrors
// openActParamsSubView's own header convention.
async function openEditSubView(thingId, displayName) {
  _ensureInteractMenuEdit();
  closeActParamsSubView(); // fresh sub-view, no stale sibling sub-view
  interactMenuEditHeaderEl.textContent = `Edit — ${displayName}`;
  interactMenuEditBodyEl.innerHTML = "";
  interactMenuEditBodyEl.appendChild(document.createTextNode("Loading…"));
  interactMenuEditEl.classList.remove("hidden");

  let resp;
  try {
    resp = await fetch(`/inspect?thing=${encodeURIComponent(thingId)}`);
  } catch (e) {
    errorEl.textContent = `request failed: ${e}`;
    closeEditSubView();
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    errorEl.textContent = body.error || `error ${resp.status}`;
    closeEditSubView();
    return;
  }
  const data = await resp.json();
  const description = data.thing.description || "";

  interactMenuEditBodyEl.innerHTML = "";
  const editor = ggEditor({
    value: description, placeholder: "Edit description…", rows: 10,
  });
  interactMenuEditBodyEl.appendChild(editor.el);
  _currentEditEditor = editor;

  interactMenuEditSaveEl.onclick = async () => {
    interactMenuEditSaveEl.disabled = true; // no double-submit while in flight
    const outcome = await actEdit(thingId, editor.el.value, displayName);
    if (!outcome) {
      // umwelt#547 (live test-session finding on the sibling actAct/
      // openActParamsSubView mechanism, 7e2f290): a rejected save must
      // NOT destroy the user's typed edit. Re-enable Save and leave the
      // sub-view (and the ggEditor's current content) exactly as it was
      // -- errorEl already carries the reason (actEdit sets it on every
      // failure path), same as the params-form fix's own posture.
      interactMenuEditSaveEl.disabled = false;
      return;
    }
    closeEditSubView();
  };

  editor.focus();
}

// `edit` — POST /act {intent:"edit", args:{thing, description}}. Saves a
// `readable` thing's `description` via update_thing's PATCH (umwelt #547
// — the web door onto update_thing, previously reachable only from
// MCP/`uw`). Mirrors actAct's exact shape (csrfToken-presence guard,
// fetch, error handling, consoleLog on success/failure) — no outcome
// window: unlike act()'s response, update_thing's own response
// (`{"thing_id", "updated"}`) has no narrative status/log_entry to show.
//
// Returns `null` on ANY failure (never a bare `false`) and the real
// response body on success — same "null vs. real outcome, never a bare
// boolean" contract Luria's actAct fix (7e2f290, umwelt#546) established
// for the sibling mechanism, precisely so a caller can tell "resolved but
// failed" from "actually succeeded" and gate a destructive UI action
// (closing the sub-view, discarding the typed edit) on the real outcome
// rather than on "the promise settled."
async function actEdit(thingId, description, displayName) {
  const r = await actPost("edit", { thing: thingId, description }, "edit");
  if (!r.ok) {
    _actFail("edit", r);
    return null;
  }
  const data = r.data;
  const label = displayName || thingId;
  if (!r.succeeded) {
    // Resolved 2xx but the world reported a problem: a failure for the
    // caller too (the Save handler keeps the sub-view and the typed edit),
    // but refresh -- the world may have partly moved.
    _actFail("edit", r);
    refreshScene();
    return null;
  }
  consoleLog("edit", `edit ${label} — saved`, data);
  showActStatus("ok", `edit ${label} — saved`);
  refreshScene();
  return data;
}

// The `agent` string a pending Talk send targets — null whenever the talk
// sub-view isn't open, same "never send without a real target" posture as
// state.csrfToken's own pairing invariant elsewhere in this file.
// umwelt #660: the private target now lives on the composer
// (composerPrivateTarget / composerPrivateDisplayName above), not on the
// interact menu -- the menu is a way to CHOOSE a target, not a place to
// hold one.
// umwelt #259: the target's display name, tracked alongside
// the display name (not just the agent id) purely so actTalk's success
// console log can say `→ <displayName>: <body>` instead of the raw agent
// id — same pairing discipline (set together, cleared together).


// Two-lane keyboard controls: which of the menu's own action buttons is
// currently highlighted (ArrowUp/ArrowDown-navigable) — -1 whenever no
// button is highlighted (menu closed). See moveMenuHighlight/
// activateHighlightedMenuButton below.
let menuHighlightIndex = -1;

// umwelt #957: what the open menu is about -- {kind: "thing"|"avatar", id,
// viewer, identity} -- so render() can keep the menu (and an act-params/edit
// sub-view the user is typing in) across a same-place refresh as long as its
// target is still in the scene AND the viewer/identity that opened it is
// still current (a menu belongs to the identity that opened it: a half-typed
// form must never submit under a different session). null whenever closed.
let _interactMenuTarget = null;
// umwelt #936 round 2: bumped by closeInteractMenu and by every
// openInteractMenu* entry point. An async opener (openSelectionMenu) captures
// it before its await and drops its late answer if it moved: the menu was
// closed, or a different menu was opened, since the click.
let _interactMenuGen = 0;

function closeInteractMenu() {
  _interactMenuGen++;
  _interactMenuTarget = null;
  interactMenuEl.classList.add("hidden");
  closeActParamsSubView(); // umwelt #522: no stale action-params form left open
  closeEditSubView(); // umwelt #547: no stale edit sub-view left open
  menuHighlightIndex = -1;
  // umwelt #938 round 2: the highlight now follows into DOM focus (see
  // focusMenuHighlight), so a menu button may hold focus when the menu hides.
  // Release it, or Enter on the street would target a hidden button.
  const ae = document.activeElement;
  if (ae && interactMenuEl.contains(ae) && typeof ae.blur === "function") {
    ae.blur();
  }
}

function getMenuButtons() {
  return Array.from(interactMenuActionsEl.querySelectorAll("button"));
}

// Sets which menu button carries the `.menu-highlight` CSS class (index.
// html). The class itself is not `:focus`; umwelt #938 round 2 makes the
// ArrowUp/ArrowDown path AND the menu opening move real DOM focus to the
// highlighted button too (focusMenuHighlight), so Enter -- which a focused
// button now handles natively -- always acts on the button the user sees
// highlighted. setMenuHighlight itself never moves focus: reconcileInteractMenu
// calls it on a refresh and must not steal focus from a sub-view being typed in.
function setMenuHighlight(idx) {
  const buttons = getMenuButtons();
  buttons.forEach((b, i) => b.classList.toggle("menu-highlight", i === idx));
  menuHighlightIndex = buttons.length ? idx : -1;
}

// No wrap at the menu's ends either — same clamp-don't-wrap discipline as
// the street cursor above.
function moveMenuHighlight(delta) {
  const buttons = getMenuButtons();
  if (!buttons.length) return;
  const base = menuHighlightIndex >= 0 ? menuHighlightIndex : 0;
  const idx = Math.max(0, Math.min(buttons.length - 1, base + delta));
  setMenuHighlight(idx);
  focusMenuHighlight(idx);
}

// umwelt #938 round 2: keep DOM focus on the highlighted menu button. A
// disabled button cannot take focus, so release any focus left inside the menu
// instead -- Enter then reaches the document handler (target = body), which
// clicks the highlighted (disabled, so inert) button, as before.
function focusMenuHighlight(idx) {
  const b = getMenuButtons()[idx];
  if (!b) return;
  if (!b.disabled) {
    b.focus();
    return;
  }
  const ae = document.activeElement;
  if (ae && interactMenuEl.contains(ae) && typeof ae.blur === "function") {
    ae.blur();
  }
}

function activateHighlightedMenuButton() {
  const buttons = getMenuButtons();
  if (!buttons.length) return;
  const idx = menuHighlightIndex >= 0
    ? Math.min(menuHighlightIndex, buttons.length - 1) : 0;
  buttons[idx].click();
}

// Avatars: "Talk" (opens the talk sub-view below, targeting `a.agent`) +
// "Examine" (the existing inspectThing on `a.avatar_id`, unchanged) —
// always both, always in this order, for every avatar. No actor-type
// check anywhere here (umwelt #252 §12.1b fidelity).
function _populateAvatarMenuActions(a) {
  interactMenuTitleEl.textContent = a.name;
  interactMenuActionsEl.innerHTML = "";

  const talkBtn = document.createElement("button");
  talkBtn.type = "button";
  talkBtn.textContent = "Talk";
  talkBtn.addEventListener("click", () => openTalkSubView(a.agent, a.name));
  interactMenuActionsEl.appendChild(talkBtn);

  const examineBtn = document.createElement("button");
  examineBtn.type = "button";
  examineBtn.textContent = "Examine";
  examineBtn.addEventListener("click", () => {
    closeInteractMenu();
    inspectThing(a.avatar_id);
  });
  interactMenuActionsEl.appendChild(examineBtn);
}

function openInteractMenuForAvatar(a) {
  _interactMenuGen++;
  closeActParamsSubView(); // umwelt #522: fresh menu, no stale sub-view
  closeEditSubView(); // umwelt #547: fresh menu, no stale sub-view
  _interactMenuTarget = { kind: "avatar", id: a.avatar_id,
                          viewer: state.viewer, identity: state.identity };
  _populateAvatarMenuActions(a);

  interactMenuEl.classList.remove("hidden");
  // Two-lane keyboard controls: highlight the first button by default so
  // ArrowUp/ArrowDown/Enter have a visible, unambiguous starting point.
  setMenuHighlight(0);
  focusMenuHighlight(0); // umwelt #938 round 2: focus agrees with highlight
}

// Things: one menu button per `t.actions` entry (gateway.py's
// `_thing_actions`, already on the thing object /scene returns) — never
// recomputed/hardcoded client-side. `verb: "use"` and `verb: "read"` both
// map to the SAME `use` /act intent (gateway.py's `/umwelt/use` is a
// single unified verb; the label difference is purely a UI choice off the
// thing's `kind`); `verb: "look_at"` calls the existing inspectThing,
// unchanged; `verb: "look_in"` (containers, umwelt #516) calls lookInThing,
// which POSTs /look_in and renders the contents list in the inspect panel;
// `verb: "edit"` (readable things, umwelt #547) calls openEditSubView,
// which pre-fills a ggEditor with the thing's current description and
// saves it through actEdit.
// Any OTHER action (umwelt #522 — this stopped being a closed
// use/read/look_at/look_in set the moment gateway.py's
// `_enrich_things_with_typed_actions` started merging kind-declared
// typed actions onto kind_id-bearing things' `.actions` list) is a typed
// action: rendered greyed-out/disabled when `action.permitted === false`
// (the server ALWAYS flags every declared action regardless of
// permission — flag, don't filter, umwelt #522 resolved-decisions §1 —
// so "action doesn't exist" and "you can't do this" stay distinguishable
// even to a viewer who can't call it), and routed through a small N-field
// parameter form (openActParamsSubView above) when `action.requires` is
// non-empty, or straight to `actAct` when it's empty — same
// fires-immediately posture `use`/`read`/`look_in` already have.
//
// Dispatch is on the TYPED-ACTION MARKER (`"permitted" in action`), not
// verb-NAME string equality, checked first (reviewer-fairy catch on
// #524): `_thing_actions`' built-in entries (use/read/look_at/look_in/edit)
// never carry a `permitted` key, only `_enrich_things_with_typed_actions`'
// kind-declared ones do — so this is a real shape discriminator, not a
// naming convention. Nothing stops a kind author from declaring an
// action literally named `read`/`use`/`look_at`/`look_in`/`edit` (there is
// no reserved-word check server-side), and dispatching on verb-name alone
// would have silently misrouted that action to the wrong /act intent
// entirely — not merely unreachable, but firing the built-in's handler
// against a thing it was never meant to run on.
// umwelt #975: the title for an action the world flagged available:false --
// the server's own `reason` string, verbatim. The client composes no sentence
// about status; the constant fallback is only for a world too old to send one.
function _unavailableTitle(action) {
  return typeof action.reason === "string" && action.reason
    ? action.reason : "not available in this state";
}

function _populateThingMenuActions(t) {
  interactMenuTitleEl.textContent = t.name;
  interactMenuActionsEl.innerHTML = "";

  for (const action of t.actions || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = action.label;
    const isTypedAction = "permitted" in action;
    if (!isTypedAction && (action.verb === "use" || action.verb === "read")) {
      btn.addEventListener("click", () => {
        closeInteractMenu();
        actUse(t.id, t.name);
      });
    } else if (!isTypedAction && action.verb === "look_at") {
      btn.addEventListener("click", () => {
        closeInteractMenu();
        inspectThing(t.id);
      });
    } else if (!isTypedAction && action.verb === "look_in") {
      btn.addEventListener("click", () => {
        closeInteractMenu();
        lookInThing(t.id, t.name);
      });
    } else if (!isTypedAction && action.verb === "edit") {
      btn.addEventListener("click", () => {
        openEditSubView(t.id, t.name);
      });
    } else {
      // umwelt #522: a kind-declared typed action (gateway.py's
      // `_enrich_things_with_typed_actions`) -- reached here because
      // `isTypedAction` was true (a real permitted-flagged action, verb
      // name irrelevant) OR, defensively, because the verb matched none
      // of the built-ins above at all. `permitted === false` never
      // hides the button — it's rendered disabled/greyed instead (see
      // this function's own doc comment above on why: flag, don't
      // filter). There is no reusable generic disabled-button CSS class
      // in this codebase today (the one existing precedent,
      // `.crossing .voice-btn:disabled`, is scoped to a different
      // ancestor and can't be reused here without an index.html/CSS
      // edit, out of scope for this file-only PR) — so this relies on
      // the native `disabled` attribute's default browser styling
      // (dimmed, non-interactive) rather than inventing a new class.
      const requires = Array.isArray(action.requires) ? action.requires : [];
      const permitted = action.permitted !== false;
      if (!permitted) btn.disabled = true;
      // umwelt #975: `permitted` is WHO, `available` is WHEN (the from-state
      // rule, computed by the world; the viewer bar below reads it the same
      // way). A closed proposal's approve / request-changes arrive
      // available:false from the server -- disabled here too, titled with the
      // server's own `reason`; a from:[] action (comment) stays enabled.
      if (action.available === false) {
        btn.disabled = true;
        btn.title = _unavailableTitle(action);
      }
      btn.addEventListener("click", () => {
        if (requires.length) {
          openActParamsSubView(t.id, action.verb, requires, t.name);
        } else {
          closeInteractMenu();
          actAct(t.id, action.verb, {}, t.name);
        }
      });
    }
    interactMenuActionsEl.appendChild(btn);
  }
}

function openInteractMenuForThing(t) {
  _interactMenuGen++;
  closeActParamsSubView(); // umwelt #522: fresh menu, no stale sub-view
  closeEditSubView(); // umwelt #547: fresh menu, no stale sub-view
  _interactMenuTarget = { kind: "thing", id: t.id,
                          viewer: state.viewer, identity: state.identity,
                          place: state.place };
  _populateThingMenuActions(t);

  interactMenuEl.classList.remove("hidden");
  // Two-lane keyboard controls: highlight the first button by default so
  // ArrowUp/ArrowDown/Enter have a visible, unambiguous starting point.
  setMenuHighlight(0);
  focusMenuHighlight(0); // umwelt #938 round 2: focus agrees with highlight
}

// ---------------------------------------------------------------------
// umwelt #936: the selection backbone. ONE selected thing (`state.selection`)
// that every thing-row sets and the chip shows; opening the chip's menu asks
// /inspect for the thing's full action list (built-ins + typed, the same list
// /scene gives) and feeds the EXISTING openInteractMenuForThing -- no second
// renderer, and every flag (permitted / available + reason) stays as today.
// World text (names, kinds, the server's refusal) is rendered by the chip
// gadget with textContent only (DESIGN.md §12.10).
// ---------------------------------------------------------------------

// Bumped on every selectThing call so a slow /inspect answer for an earlier
// selection can never overwrite a newer one.
let _selectionSeq = 0;
// The server's own message for the last refused selection / dropped menu,
// shown on the chip ({text, kind}); cleared by the next selection.
let _selectionNotice = null;

function _selectionWhere(where) {
  return typeof where === "string" &&
    /^(street|inventory|pasted|container:[\s\S]*)$/.test(where)
    ? where : "pasted";
}

// The ONLY writer of `state.selection`. `selectThing(null)` clears it.
// Sets the selection at once (so the chip answers the click), then asks
// /inspect to fill in the kind / canonical name and to let the SERVER refuse:
// 404 or 403 (in_private_place / binned) drops the selection and shows the
// server's own message on the chip; any other failure keeps the selection
// and says the lookup failed. A thing already selected with a known kind is
// not re-fetched.
async function selectThing(id, name, where) {
  const seq = ++_selectionSeq;
  const tid = typeof id === "string" ? id.trim() : "";
  if (!tid) {
    state.selection = null;
    _selectionNotice = null;
    renderSelectionChip();
    return null;
  }
  const prev = state.selection;
  const known = !!(prev && prev.id === tid && prev.kind);
  state.selection = {
    id: tid,
    name: (typeof name === "string" && name) ? name : (known ? prev.name : tid),
    kind: known ? prev.kind : "",
    where: _selectionWhere(where),
  };
  _selectionNotice = null;
  renderSelectionChip();
  if (known) return state.selection;

  let resp;
  try {
    resp = await fetch(`/inspect?thing=${encodeURIComponent(tid)}`);
  } catch (e) {
    if (seq !== _selectionSeq) return null;
    _selectionNotice = { text: `couldn't look this up: ${e}`, kind: "error" };
    renderSelectionChip();
    return state.selection;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    if (seq !== _selectionSeq) return null;
    _selectionNotice = {
      text: (body && body.error) || `error ${resp.status}`, kind: "error" };
    if (resp.status === 404 || resp.status === 403) state.selection = null;
    renderSelectionChip();
    return state.selection;
  }
  const data = await resp.json().catch(() => null);
  if (seq !== _selectionSeq) return null;
  const t = data && data.thing;
  if (t) {
    state.selection = {
      id: tid, name: t.name || state.selection.name,
      kind: t.kind || "", where: state.selection.where };
  }
  renderSelectionChip();
  return state.selection;
}

function renderSelectionChip() {
  const s = state.selection;
  selectionChip.update({
    id: s ? s.id : "",
    name: s ? s.name : "",
    kind: s ? s.kind : "",
    where: s ? s.where : "",
    notice: _selectionNotice ? _selectionNotice.text : "",
    noticeKind: _selectionNotice ? _selectionNotice.kind : "",
  });
}

// A refused selection / dropped menu target: the selection (if it is that
// thing) is cleared through selectThing, then the server's message is shown.
function _showSelectionRefusal(thingId, message) {
  if (state.selection && state.selection.id === thingId) selectThing(null);
  _selectionNotice = { text: message, kind: "error" };
  renderSelectionChip();
}

// The chip's click: fetch the selection's full action list from /inspect and
// open the existing interact menu on it.
async function openSelectionMenu() {
  const sel = state.selection;
  if (!sel) return;
  const gen = _interactMenuGen;
  let resp;
  try {
    resp = await fetch(`/inspect?thing=${encodeURIComponent(sel.id)}`);
  } catch (e) {
    _selectionNotice = { text: `couldn't open actions: ${e}`, kind: "error" };
    renderSelectionChip();
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    const msg = (body && body.error) || `error ${resp.status}`;
    if (resp.status === 404 || resp.status === 403) {
      _showSelectionRefusal(sel.id, msg);
    } else {
      _selectionNotice = { text: msg, kind: "error" };
      renderSelectionChip();
    }
    return;
  }
  const data = await resp.json().catch(() => null);
  const t = data && data.thing;
  if (!t || !state.selection || state.selection.id !== sel.id) return;
  // The menu was closed, or another menu opened, since the click: a late
  // answer must neither reopen nor replace it.
  if (gen !== _interactMenuGen) return;
  _selectionNotice = null;
  renderSelectionChip();
  openInteractMenuForThing({
    id: sel.id, name: t.name || sel.name, kind: t.kind,
    actions: Array.isArray(data.actions) ? data.actions : [],
  });
}

const selectionChip = ggChip({
  id: "selection-chip",
  onOpen: openSelectionMenu,
  onClear: () => selectThing(null),
  onSelectById: (v) => selectThing(v, undefined, "pasted"),
});
// Docked as the composer's first row: inside it, the chip can never overlap
// it, and the inspect panel's existing lift onto the composer's top clears it.
composerEl.insertBefore(selectionChip.el, composerEl.firstChild);

// umwelt #957: render()'s own stated reason for closing the menu is "its
// target may no longer be in the new scene" -- so close it only then. A
// same-place refresh (refreshScene after an action) that still contains the
// target keeps the menu AND any sub-view the user is typing in (#522/#547:
// a refused act must leave the form and its text alone); only the action
// list is rebuilt from the target's fresh data. #interact-menu is a fixed
// bottom sheet, not anchored to the target's element, so there is nothing to
// re-anchor. A place change needs no special case: the target is not in the
// new place's scene, so the same rule closes it.
function reconcileInteractMenu(scene) {
  if (interactMenuEl.classList.contains("hidden")) return;
  const tgt = _interactMenuTarget;
  let fresh = null;
  // The menu belongs to the identity/viewer that opened it (a re-login or a
  // viewer switch in the same place must not carry a half-typed sub-view
  // into the new session).
  const sameSession = tgt && tgt.viewer === state.viewer &&
                      tgt.identity === state.identity;
  if (tgt && sameSession) {
    fresh = tgt.kind === "avatar"
      ? (scene.avatars || []).find((a) => a.avatar_id === tgt.id)
      : (scene.things || []).find((t) => t.id === tgt.id);
  }
  // umwelt #936: a THING target that is not in scene.things (a thing picked
  // from a container, or held) is not gone -- it is just not on the street.
  // Refresh it through /inspect instead of closing the menu on every
  // refreshScene (which also threw away a half-typed act-params form). A
  // place change still closes it, as before.
  if (!fresh && tgt && sameSession && tgt.kind === "thing" &&
      (!tgt.place || tgt.place === state.place)) {
    _refreshThingMenuViaInspect(tgt);
    return;
  }
  if (!fresh) {
    closeInteractMenu();
    return;
  }
  const keep = menuHighlightIndex;
  if (tgt.kind === "avatar") _populateAvatarMenuActions(fresh);
  else _populateThingMenuActions(fresh);
  setMenuHighlight(Math.max(0, Math.min(keep, getMenuButtons().length - 1)));
}

// umwelt #936: the non-scene half of reconcileInteractMenu. Rebuilds only the
// action list (same posture as the scene path: a sub-view being typed in is
// left alone). Closes the menu ONLY on a 404 / 403 from /inspect and then shows
// the server's own refusal on the chip; any other failure leaves the menu as is.
// Bumped per call: refreshes (act + poll) can overlap, and an answer whose
// request was superseded by a newer one is ignored (same idea as
// _selectionSeq in selectThing).
let _menuRefreshSeq = 0;

async function _refreshThingMenuViaInspect(tgt) {
  const seq = ++_menuRefreshSeq;
  let resp;
  try {
    resp = await fetch(`/inspect?thing=${encodeURIComponent(tgt.id)}`);
  } catch (e) {
    return;
  }
  if (_interactMenuTarget !== tgt || seq !== _menuRefreshSeq) return; // closed, retargeted or superseded
  if (!resp.ok) {
    if (resp.status === 404 || resp.status === 403) {
      const body = await resp.json().catch(() => ({}));
      if (_interactMenuTarget !== tgt || seq !== _menuRefreshSeq) return;
      closeInteractMenu();
      _showSelectionRefusal(tgt.id, (body && body.error) || `error ${resp.status}`);
    }
    return;
  }
  const data = await resp.json().catch(() => null);
  if (!data || !data.thing || _interactMenuTarget !== tgt ||
      seq !== _menuRefreshSeq || interactMenuEl.classList.contains("hidden")) return;
  const keep = menuHighlightIndex;
  _populateThingMenuActions({
    id: tgt.id, name: data.thing.name,
    actions: Array.isArray(data.actions) ? data.actions : [],
  });
  setMenuHighlight(Math.max(0, Math.min(keep, getMenuButtons().length - 1)));
}

// umwelt #660: was a sub-view that opened a SECOND composer inside the
// interact menu. Now it preselects Private with this agent in the one
// composer and gets out of the way. Kept under its old name and call site
// so the avatar-click gesture is unchanged from the caller's side.
function openTalkSubView(toAgent, displayName) {
  selectPrivateChannel(toAgent, displayName);
  closeInteractMenu();
  composerInputEl.focus();
}

// `talk` — POST /act {intent:"talk", args:{to_agent, body}}. Same
// csrfToken-presence guard as actMove (#234 review discipline: fail loudly
// here rather than send a null X-Csrf-Token). destination is never sent —
// resolved server-side from the caller's own current place (see
// gateway.py's /act docstring).
async function actTalk(toAgent, body, displayName) {
  const r = await actPost("talk", { to_agent: toAgent, body }, "talk");
  if (!r.succeeded) {
    _actFail("talk", r);
    return false;
  }
  const sent = `→ ${displayName || toAgent}: ${body}`;
  consoleLog("talk", sent);
  // No scene refresh here: the crossings poller shows the new line.
  showActStatus("ok", sent);
  return true;
}

// ---------------------------------------------------------------------
// umwelt #351: the floating outcome window (GADGETS.md's `window` gadget,
// the charter's proving case — see gadgets.js's ggWindow). Before this,
// a successful use()/read outcome's full text only ever reached the
// console's collapsed, JSON-wrapped `<details><pre>` view (still true —
// that debug/raw view is UNCHANGED, see consoleLog above) — destroying any
// formatted multi-line output (the motivating case: an orrery's spaced
// star chart flattens to an unreadable JSON string once
// JSON.stringify'd). This is the player-facing, UNWRAPPED view: the same
// `outcome.content`/`outcome.stdout` fields mcp_server.py's `use()` tool
// renders for agents (see that function's own docstring/body — the
// reference this shape is traced from), rendered pre-wrap/monospace,
// never JSON.stringify'd.
// ---------------------------------------------------------------------

// One reused instance (never stacks a second outcome window — a fresh
// Use/Read either opens it fresh or update()s + re-opens the existing
// one). Always screen-anchored (Kepler's #351 clip-fix ruling, 2026-07-25
// — see GADGETS.md §3's street/screen amendment): this window's content is
// unbounded (a readable's full file text, an executable's full stdout —
// the orrery's spaced star chart is the motivating case), and #street has
// `overflow-x: auto`, so a `street`-anchored child is clipped to the band's
// ~2-row height + capped at .gg-window's 420px class-default width
// regardless of how much content it holds. There is therefore only ever
// ONE mode for this gadget now — no more mode-tracking/rebuild-on-change
// machinery (the pre-fix version toggled `street` vs a defensive `screen`
// fallback depending on whether the used thing's on-screen element could be
// found; that toggle is gone along with `street` itself).
let _outcomeWindow = null;

// Builds the outcome window's CONTENT (never the window chrome itself —
// that's gadgets.js's job) from a raw `outcome` object. Mirrors the shape
// mcp_server.py's `use()` tool renders for agents (this file's module-
// header note traces the exact fields): a readable's `outcome.content` is
// `{path: text}`, one block per file; an executable's full output is
// `outcome.stdout`, falling back to the truncated one-line `briefing` only
// when `stdout` is absent. NEVER JSON.stringify's the outcome — that
// wrapping is the whole problem #351 exists to fix (the console's own
// collapsed raw-JSON `<details>` view, consoleLog above, is a DIFFERENT,
// still-JSON debug surface that this window deliberately does not
// duplicate).
function _outcomeWindowContent(outcome) {
  const wrap = document.createElement("div");
  if (outcome.kind === "readable") {
    const files = outcome.content || {};
    const paths = Object.keys(files);
    if (!paths.length) {
      // Gated (e.g. a tome/library, umwelt #88 slice 4) or otherwise
      // content-less readable — briefing is the only text the server gave
      // us; still never JSON-wrapped.
      wrap.textContent = outcome.briefing || "You read it.";
      return wrap;
    }
    for (const path of paths) {
      // "--- path ---" header, mirroring mcp_server.py's own use() tool
      // formatting for a readable's multi-file content (not required to
      // match byte-for-byte per the fairy spec — the requirement is the
      // full text renders pre-wrap/monospace, not exact string parity).
      const header = document.createElement("div");
      header.className = "gg-outcome-file-header";
      header.textContent = `--- ${path} ---`;
      wrap.appendChild(header);
      const body = document.createElement("pre");
      body.className = "gg-mono-block";
      body.textContent = files[path];
      wrap.appendChild(body);
    }
    return wrap;
  }
  // Executable outcome: full stdout if present, else the truncated
  // one-line briefing (server.py's _narrate_use caps this at 200 chars —
  // NOT sufficient for #351's acceptance criterion on its own, hence
  // stdout being preferred whenever it exists). Reviewer-fairy catch (PR
  // #355): an internal sandbox failure (e.g. a broken shebang) can return
  // an outcome with neither stdout nor briefing set, only `error`
  // (server.py sets this field on that path, and mud.py's own agent-facing
  // render already falls back to it the same way) — without this, that
  // case opened a blank window instead of showing the actual failure.
  //
  // act()-outcome fallback (umwelt #522): a typed action's outcome shape
  // is `{thing_id, action, by, status, log_entry, effect_failures?}` —
  // none of stdout/briefing/error, since act() has no free-text narration
  // field at all (log_entry is a structured {action,by,at,args} dict, not
  // prose). Flagged in #522's own PR handoff as a genuine gap rather than
  // silently improved on; fixed here by synthesizing a small readable
  // summary from the fields act() DOES send, rather than leaving Lei with
  // a blank floating window after every approve/request_changes/comment.
  //
  // Gated on `outcome.action` alone, NOT `outcome.status` too (reviewer-
  // fairy catch on #524's first fix): server.py's act() response always
  // sets `action` to the real action name, but `status` is
  // `new_metadata.get("status")` -- genuinely `null` for any action whose
  // kinddef declares no `to` (a `comment` that only logs, never
  // transitions state, is exactly this case). `outcome.status &&
  // outcome.action` made that case fail the same falsy-check this whole
  // fallback exists to avoid -- reproducing the blank window for actions
  // that don't change status, which given proposals' own comment/
  // request_changes actions is not a rare case.
  let text = outcome.stdout || outcome.briefing || outcome.error || "";
  if (!text && outcome.action) {
    text = outcome.status
      ? `${outcome.action} → ${outcome.status}`
      : `${outcome.action} (no status change)`;
    if (Array.isArray(outcome.effect_failures) && outcome.effect_failures.length) {
      text += `\n\n${outcome.effect_failures.length} effect(s) failed:\n` +
        outcome.effect_failures.map(
          (f) => `  ${f.type || "?"}: ${f.reason || "unknown reason"}`
        ).join("\n");
    }
  }
  const pre = document.createElement("pre");
  pre.className = "gg-mono-block";
  pre.textContent = text;
  wrap.appendChild(pre);
  return wrap;
}

// Opens (or updates + re-opens, if already showing) the shared outcome
// window — always screen-anchored now, bottom-right corner (mirrors
// #interact-menu's own desktop-popup corner, index.html's @media rule;
// distinct from the migrated console's bottom-left spot, see index.html's
// #console CSS comment, so the two screen-anchored gadgets never overlap).
// TASTE CALL flagged for Lei (trivial swap if he prefers it): a centered
// focal panel — `{mode: "screen", pos: {top: "10vh", left: "50%"}}` plus a
// transform, or a dedicated centered anchor.pos shape — is the alternative
// to this bottom-right corner.
// `size` caps the window at a comfortable reading width/height
// (`min(720px, 90vw)` / `70vh`) so long readable/stdout content scrolls
// inside the window (gg-window-content's overflow:auto) instead of the
// window itself growing past the viewport.
// `targetEl` (the used thing's on-screen `.item.thing` element, still
// looked up by actUse below) no longer selects an anchor mode — screen mode
// doesn't take a target — kept as a parameter for call-site/signature
// stability (a pre-existing served-content test asserts this exact
// signature; judgment call, flagged in the PR: cheaper to keep the unused
// param than to touch a test file outside this fix's scope).
function _openOutcomeWindow(displayName, outcome, targetEl) {
  const content = _outcomeWindowContent(outcome);
  if (!_outcomeWindow) {
    _outcomeWindow = ggWindow({
      title: displayName,
      anchor: { mode: "screen", pos: { bottom: "1rem", right: "1rem" } },
      size: { maxWidth: "min(720px, 90vw)", maxHeight: "70vh" },
      closable: true,
      content,
      variant: "mono",
    });
  } else {
    _outcomeWindow.update({ title: displayName, content });
  }
  _outcomeWindow.open();
}

// `use` — POST /act {intent:"use", args:{thing, args}}. Covers both the
// "Use" and "Read" menu labels (same intent, same route — see
// openInteractMenuForThing above). Same csrfToken-presence guard as
// actMove/actTalk.
// displayName (umwelt #259, new param): the thing's name, purely for the
// success console log's fallback text ("used <name>") when the outcome has
// no `briefing` field (e.g. blueprint-stamping's
// `{"outcome": {"stamped_thing_id": ...}}` shape) — never sent to the
// server, never used for anything but display.
// useArgs (umwelt #321, new param, default []): the args-input sub-view's
// whitespace-split tokens (see actionBarUseSendEl's listener above) —
// gateway.py's `use` branch already reads `args.args` and forwards it
// unchanged (no gateway change needed, this is a client-only slice). Every
// OTHER call site (e.g. openInteractMenuForThing's Use/Read menu buttons)
// still calls `actUse(t.id, t.name)` with no third argument, which is
// exactly the pre-#321 no-args behavior — `useArgs || []` below covers
// that case.
// ---------------------------------------------------------------------
// umwelt #396 CLIENT HALF: the dynamic parameter form.
//
// The engine half (#402) makes `use` on a params-declaring thing answer a
// bare call with `{"params_spec": {thing, name, params}}` at HTTP 200 —
// and answers a MALFORMED call with the same payload plus an `error`
// string. This half renders that one payload as a form. The agent reads
// the JSON as a `--help`; the human gets the same declaration as inputs.
//
// THE CLIENT KEEPS NO FORM DEFINITIONS OF ITS OWN (#396's load-bearing
// constraint). Every field here is derived from the spec the engine just
// sent — no per-thing special cases, no local table of "what the echo well
// takes". A thing that changes its declaration changes this form on the
// next call, with nothing to keep in sync.
//
// ESCAPING IS THIS FILE'S JOB (Borges's ruling, revising #396's original
// "escaping server-side" requirement). `params` is passed through by the
// engine verbatim — `_params_spec_payload`'s docstring says so explicitly
// ("returned exactly as stored in metadata.params, unmodified") — so
// `name` and `description` are AUTHOR-CONTROLLED strings arriving in a
// human's browser. The server enforces LENGTH (12 params, 240 chars);
// length is a resource bound, escaping is context-dependent and would
// corrupt the data for every non-HTML consumer if applied at store time.
// So: this whole builder uses createElement + textContent EXCLUSIVELY and
// never innerHTML / insertAdjacentHTML / string-built markup. That is the
// escaping, and it is why there is no _escapeHtml helper here to forget to
// call — the DOM API escapes by construction, a helper only escapes when
// remembered.
// ---------------------------------------------------------------------

let _paramsFormWindow = null;

// Does `errorMsg` name THIS param? The engine's binder quotes the param
// name in every per-param rejection it produces (`missing required param
// 'title'`, `param 'mode' must be one of [...]`, `param 'n' must be a
// number`), so a quoted-name test highlights the offending field without
// re-implementing the binder's rules client-side. Deliberately a
// COUPLING, and named as one: it reads the engine's message format. If a
// future message stops quoting the name the field simply isn't
// highlighted — the error text still renders in full at the top of the
// form, so this degrades to "less helpful", never to "wrong".
function _paramsErrorNamesField(errorMsg, paramName) {
  return !!errorMsg && errorMsg.includes(`'${paramName}'`);
}

// One input for one declared param. enum -> <select>; boolean -> a
// true/false <select> (not a checkbox: a checkbox cannot express "left
// blank so the declared default applies", which is a real third state
// here); type:"text" (or, absent any declared type, a param name in
// _GG_EDITOR_FIELD_NAMES) -> a ggEditor (GADGETS.md's `editor` gadget,
// toolbar + resizable textarea); number -> <input type="number">;
// anything else -> plain text.
function _buildParamInput(p) {
  const enumVals = Array.isArray(p.enum) ? p.enum : null;
  const ptype = typeof p.type === "string" ? p.type : "string";
  const isLongText = ptype === "text" ||
    (!p.type && _GG_EDITOR_FIELD_NAMES.has(String(p.name || "").toLowerCase()));
  let input;
  let isEditor = false;
  if (enumVals) {
    input = document.createElement("select");
    // A blank first option is what makes "use the declared default"
    // reachable from the form at all — without it a select is always
    // submitting SOMETHING, and an optional enum param could never be
    // left to its default.
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = p.required ? "— choose —" : "— default —";
    input.appendChild(blank);
    for (const v of enumVals) {
      const opt = document.createElement("option");
      opt.value = String(v);
      opt.textContent = String(v);   // author-controlled -> textContent
      input.appendChild(opt);
    }
  } else if (ptype === "boolean") {
    input = document.createElement("select");
    for (const v of ["", "true", "false"]) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v === "" ? (p.required ? "— choose —" : "— default —") : v;
      input.appendChild(opt);
    }
  } else if (isLongText) {
    // Default applied at CONSTRUCTION (opts.value) rather than via the
    // post-hoc `.value = ...` line below -- `el.value` DOES have a real
    // setter (gadgets.js's drop-in-compatibility contract), so that line
    // would also work here, but there's no reason to take two code paths
    // to the same end state; the `!isEditor` guard below just skips the
    // now-redundant second write. NOT given the "params-form-input" base
    // class (unlike the select/input branches) -- that feature class
    // supplies its own background/border/padding, which would double up
    // against .gg-editor's own chrome (GADGETS.md §1 obligation 2's
    // chrome-leakage rule); the error-state visual equivalent is
    // gadgets.css's `.gg-editor.params-form-input--error` rule instead.
    const defaultVal = p.default !== undefined && p.default !== null
      ? String(p.default) : undefined;
    input = ggEditor({ value: defaultVal }).el;
    isEditor = true;
  } else {
    input = document.createElement("input");
    input.type = ptype === "number" ? "number" : "text";
  }
  if (!isEditor) {
    input.className = "params-form-input";
  } // else: no extra class -- .gg-editor's own CSS already supplies the
    // width:100%/chrome this branch needs; adding "params-form-input"
    // here would double up chrome leakage (see comment above).
  input.dataset.paramName = p.name;
  // Defaults pre-filled (#396: "defaults pre-filled, with the Run button
  // on the form itself"). A declared default is shown as the field's
  // value so the human sees what running now would actually do, rather
  // than an empty box that silently means the same thing. The editor
  // branch already applied its default at construction, above.
  if (!isEditor && p.default !== undefined && p.default !== null) {
    input.value = String(p.default);
  }
  return input;
}

// The `?` affordance (#396: "every field carries a `?` affordance showing
// the parameter's description"). Toggles a description line rather than
// using title=, because a native tooltip is unreachable on touch — and the
// world is used from a Chromebook and a phone, not only a desktop.
function _buildParamHelp(p, descEl) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "params-form-help";
  btn.textContent = "?";
  btn.title = "what is this?";       // desktop affordance, not the only one
  btn.setAttribute("aria-expanded", "false");
  btn.addEventListener("click", () => {
    const showing = descEl.classList.toggle("params-form-desc--open");
    btn.setAttribute("aria-expanded", showing ? "true" : "false");
  });
  return btn;
}

// Build the whole form body for one params_spec. `onRun(args)` receives
// the POSITIONAL argv (see _collectParamsArgs for why it's built the way
// it is).
function _buildParamsFormContent(spec, errorMsg, onRun) {
  const params = Array.isArray(spec.params) ? spec.params : [];
  const wrap = document.createElement("div");
  wrap.className = "params-form";

  if (errorMsg) {
    // Requirement 2's client side: a malformed call and a deliberate ask
    // return the SAME payload, distinguished only by this key — so the
    // form must look different when it's present, or the human cannot
    // tell "here is the form" from "your last attempt was wrong".
    const err = document.createElement("div");
    err.className = "params-form-error";
    err.textContent = errorMsg;          // engine text -> textContent
    wrap.appendChild(err);
  }

  const inputs = [];
  for (const p of params) {
    if (!p || typeof p.name !== "string") continue;
    const row = document.createElement("div");
    row.className = "params-form-row";

    const label = document.createElement("label");
    label.className = "params-form-label";
    label.textContent = p.name;          // author-controlled -> textContent
    if (p.required) {
      const req = document.createElement("span");
      req.className = "params-form-required";
      req.textContent = "*";
      req.title = "required";
      label.appendChild(req);
    }

    const desc = document.createElement("div");
    desc.className = "params-form-desc";
    if (typeof p.description === "string" && p.description) {
      desc.textContent = p.description;  // author-controlled -> textContent
      label.appendChild(_buildParamHelp(p, desc));
    }

    const input = _buildParamInput(p);
    if (_paramsErrorNamesField(errorMsg, p.name)) {
      input.classList.add("params-form-input--error");
    }
    inputs.push({ p, input });

    row.appendChild(label);
    row.appendChild(input);
    row.appendChild(desc);
    wrap.appendChild(row);
  }

  const runRow = document.createElement("div");
  runRow.className = "params-form-run-row";
  const runBtn = document.createElement("button");
  runBtn.type = "button";
  runBtn.className = "gg-btn params-form-run";
  runBtn.textContent = "Run";
  // The blank-required guard. A required field the human left EMPTY must
  // never be submitted, and the reason is subtler than "the engine will
  // catch it" — it won't, in the commonest case:
  //
  // `_collectParamsArgs` sends a dense prefix (it must — binding is
  // positional), so a blank required field followed by a filled one is
  // sent as `""` rather than omitted. The engine's missing-required branch
  // only fires when the arg is ABSENT (`i >= len(args)`), and `""` is
  // present. Enum and number/boolean params would still be rejected by
  // their own checks — but a plain `type: "string"` param has no check to
  // fail, so it binds `""` and the thing RUNS with an empty required
  // value, silently, with the `*` marker rendered decorative.
  //
  // Verified against a real engine rather than reasoned about (Borges
  // caught this in colleague review on PR #406; I had generalized from a
  // fixture whose required param happened to carry an enum, which masked
  // it): a thing declaring a required plain-string `name` called with
  // `["", "note-value"]` returns HTTP 200, no `error`, and executes with
  // `name=[]`.
  //
  // So the guard keys on the FIELD being blank, not on the arg being
  // absent — which subsumes the absent case, since a trailing blank field
  // is exactly what makes an arg absent. The defect being prevented is the
  // FORM inventing a value the human never typed; that belongs here, not
  // in the engine, which is entitled to treat `""` as a real string.
  //
  // Still NOT the client re-implementing validation — #396 is explicit
  // that the client holds no form definitions of its own, and it doesn't:
  // `required` is read straight off the engine's declaration, the same
  // source as the `*` beside the label. Enum membership and type-parsing
  // are deliberately left alone; those produce real, well-worded engine
  // errors that come back through the `error` field and highlight the
  // right box.
  runBtn.addEventListener("click", () => {
    const unsupplied = inputs
      .map(({ p, input }, i) => ({ p, i, blank: input.value === "" }))
      .filter(({ p, blank }) => p.required && blank);
    if (unsupplied.length) {
      for (const { i } of unsupplied) {
        inputs[i].input.classList.add("params-form-input--error");
      }
      const names = unsupplied.map(({ p }) => p.name).join(", ");
      let notice = wrap.querySelector(".params-form-error");
      if (!notice) {
        notice = document.createElement("div");
        notice.className = "params-form-error";
        wrap.insertBefore(notice, wrap.firstChild);
      }
      notice.textContent =
        `fill in the required field${unsupplied.length > 1 ? "s" : ""}: ${names}`;
      inputs[unsupplied[0].i].input.focus();
      return;
    }
    onRun(_collectParamsArgs(inputs));
  });
  runRow.appendChild(runBtn);
  wrap.appendChild(runRow);
  return wrap;
}

// Turn the filled fields into use()'s argv.
//
// THE POSITIONAL CONSTRAINT, which shapes this whole function: the engine
// binds STRICTLY positionally — `_bind_params` does `params[i]` binds to
// `args[i]`, and a param with no corresponding arg falls back to its
// declared default. There is no by-name form. So a MIDDLE param cannot be
// skipped: to supply the 3rd param you must also supply the 1st and 2nd,
// or your 3rd value silently binds to the 1st slot.
//
// Therefore: send a DENSE PREFIX up to the last field the human actually
// filled, substituting each earlier blank field's declared default, and
// send nothing at all after it — letting the engine apply the remaining
// defaults itself.
//
// An earlier version of this comment claimed that a blank-with-no-default
// becoming "" was safe because "the engine will type-check and reject it
// if that isn't acceptable." That is FALSE for the commonest param shape
// and was corrected in colleague review: `""` is PRESENT, so the engine's
// missing-required branch never fires, and a plain `type: "string"` param
// has no other check to fail — it would simply run with an empty value.
// The synthesized "" is therefore only ever safe for an OPTIONAL field;
// a blank REQUIRED field is stopped before it reaches here, by the
// blank-required guard on the Run button (see its own note).
//
// The alternative — always sending every field — would turn a trailing
// optional param the human left alone into an explicit "" rather than
// letting its default apply, which is a different call.
function _collectParamsArgs(inputs) {
  const raw = inputs.map(({ input }) => input.value);
  let last = -1;
  for (let i = 0; i < raw.length; i++) {
    if (raw[i] !== "") last = i;
  }
  if (last < 0) return [];
  const args = [];
  for (let i = 0; i <= last; i++) {
    if (raw[i] !== "") {
      args.push(raw[i]);
    } else {
      const d = inputs[i].p.default;
      args.push(d === undefined || d === null ? "" : String(d));
    }
  }
  return args;
}

// Open (or re-render) the parameter form for `spec`. Re-uses ONE window
// the way _openOutcomeWindow does, so a re-submit that comes back with a
// new `error` updates in place instead of stacking windows.
function _openParamsForm(spec, errorMsg, thingId, displayName) {
  const onRun = (args) => {
    if (_paramsFormWindow) _paramsFormWindow.close();
    actUse(thingId, displayName, args);
  };
  const content = _buildParamsFormContent(spec, errorMsg, onRun);
  const title = displayName || spec.name || spec.thing;
  if (!_paramsFormWindow) {
    _paramsFormWindow = ggWindow({
      title,
      anchor: { mode: "screen", pos: { bottom: "1rem", right: "1rem" } },
      size: { maxWidth: "min(420px, 90vw)", maxHeight: "70vh" },
      closable: true,
      content,
    });
  } else {
    _paramsFormWindow.update({ title, content });
  }
  _paramsFormWindow.open();
}

// umwelt #655: the shared fetch+CSRF+error half of `actUse`, extracted so
// the proposal viewer's body-open (`openThingViewer`) reuses ONE impl
// rather than duplicating the POST /act intent:"use" shape. Returns the
// PARSED response object on success, or `null` on any failure (network
// throw, non-ok HTTP, missing session) — callers branch on the shape
// (`params_spec` vs `outcome`). Mirrors actUse's exact pre-#655 posture:
// same csrfToken-presence guard, same errorEl/consoleLog on every failure
// path, same `useArgs || []` default so a bare call is unchanged.
//
// `{ silent: true }` (umwelt #655 reviewer suggestion) suppresses the
// user-visible `errorEl` banner write on failure — for the viewer's body
// open, where a failed `use` is an EXPECTED graceful-degradation path (a
// non-readable thing, a params-declaring thing, a transient blip), not an
// error worth hijacking the global status banner for. `consoleLog` still
// fires (developer signal, not user-facing UX). `actUse` calls without it
// and is byte-for-byte unchanged.
// umwelt #957: `full: true` returns the whole actPost result (so actUse can
// read `succeeded`); the default still returns just the parsed body. `silent`
// also runs actPost quiet (no pending line / busy state) -- the viewer's body
// read is background, not a click the user is waiting on.
async function _actUseFetch(thingId, useArgs, { silent = false, full = false } = {}) {
  const r = await actPost(
    "use", { thing: thingId, args: useArgs || [] }, "use", { quiet: silent });
  if (!r.ok) {
    _actFail("use", r, { silent });
    return null;
  }
  return full ? r : r.data;
}

async function actUse(thingId, displayName, useArgs) {
  // umwelt #655: the fetch+CSRF+error half is shared with the proposal
  // viewer's body open (which needs a `use` call for the readable content
  // but does NOT want the outcome window / params-form handling below).
  // Extracted as `_actUseFetch` so both callers share one impl; this
  // function keeps the exact same observable behavior as before (params_spec
  // form, outcome window, console log) by branching on the parsed data.
  const res = await _actUseFetch(thingId, useArgs, { full: true });
  if (!res) return false;
  const data = res.data;
  // umwelt #259: this response body used to be discarded entirely on
  // success — the fix this file exists for. `outcome.briefing` is the
  // one-line natural-language narration, always present for a real
  // executable's outcome (see gateway.py's `/act` "use" branch / this
  // file's module-header note) — some use-paths (e.g. blueprint stamping)
  // return a different outcome shape with no `briefing`, hence the
  // fallback. The FULL response object is logged as `detail` regardless,
  // so the raw exit_code/stdout/world_diff/etc. fields are always
  // available one click away even when briefing covers the summary line.
  // umwelt #396: a params-declaring thing answers a bare or malformed
  // call with `params_spec` INSTEAD of `outcome`, at HTTP 200 — so this
  // check must come before the outcome handling below, and must not treat
  // the absent `outcome` as an empty success. `data.error` rides along
  // only on the malformed case (see the form builder's own note on why
  // the two must look different).
  if (data.params_spec) {
    _openParamsForm(data.params_spec, data.error || "", thingId, displayName);
    // Logged as a distinct console line rather than a `use` result: this
    // ran nothing in the world, and reporting it as a use would put a
    // success in the log for an action that hasn't happened yet.
    const needText = data.error
      ? `${displayName || thingId}: ${data.error}`
      : `${displayName || thingId} needs parameters`;
    consoleLog(data.error ? "error" : "info", needText, data);
    // Nothing ran, so no scene refresh; this resolves the pending line.
    showActStatus(data.error ? "error" : "ok", needText);
    return false;
  }
  const outcome = data.outcome || {};
  const label = displayName || thingId;
  const text = outcome.briefing || `used ${label}`;
  consoleLog("use", text, data);
  showActStatus(res.succeeded ? "ok" : "error",
                res.succeeded ? text : `use failed: ${_actRefusalReason(res)}`);
  refreshScene();
  // umwelt #351: the player-facing, unwrapped view — see this file's
  // module-header note on the outcome window, just above. `data-thing-id`
  // (buildThingElement) is still looked up here for `_openOutcomeWindow`'s
  // signature-compatible `targetEl` param (no longer anchor-relevant — see
  // that function's own note); CSS.escape guards the selector against a
  // thing id containing CSS-meaningful characters (defensive — ids are
  // server-generated, but this costs nothing).
  const targetEl = document.querySelector(
    `.item.thing[data-thing-id="${CSS.escape(thingId)}"]`);
  _openOutcomeWindow(label, outcome, targetEl || null);
  // A 2xx that did not succeed (an error key / refused effects) is a failure
  // for the caller too: the use input stays open, the status line has the
  // reason.
  return res.succeeded;
}

// `act` — POST /act {intent:"act", args:{thing, action, args}}. Runs a
// kind-declared action on a typed thing (umwelt #522 — the web door onto
// server.py's act(), previously reachable only from MCP/`uw`). Mirrors
// actTalk's exact shape (CSRF-presence guard, fetch, error handling,
// consoleLog on success/failure).
//
// On success this surfaces the outcome via the SAME `_openOutcomeWindow`
// path actUse's own success path already uses just above (umwelt #522
// spec: "don't invent a third pattern for showing an outcome"). The
// coder-fairy build correctly flagged this as a genuine gap rather than
// silently papering over it: act()'s outcome shape (`{thing_id, action,
// by, status, log_entry, effect_failures?}`) carries none of
// stdout/briefing/content/error, the fields `_outcomeWindowContent`'s
// executable-outcome branch originally read — a typical act() call would
// have opened a blank window. Fixed during review: that function now has
// an explicit act()-outcome fallback (a synthesized "<action> → <status>"
// summary, plus a listing of any effect_failures) so the floating window
// always shows something for Lei, not just the console line. The console
// line (below) keeps its own independent synthesized summary either way.
// Returns the parsed `outcome` object on a request that reached the server
// (regardless of whether any of ITS OWN effects failed -- see
// `effect_failures` below), or `null` on a request that never got a real
// server answer at all (no session, network error, non-2xx HTTP). Callers
// that need to decide "did my board_entry_add/etc. actually take" must
// check `outcome.effect_failures` themselves -- `actAct` resolving is not
// the same claim as every effect inside it succeeding (umwelt#546 test-
// session finding: the params-sub-view used to treat ANY resolution as
// success and destroy the form+typed text unconditionally, which is
// exactly wrong for the effect_failures case -- see openActParamsSubView's
// submit handler, the caller this return-shape change was made for).
async function actAct(thingId, actionVerb, actionArgs, displayName) {
  const r = await actPost("act", {
    thing: thingId, action: actionVerb, args: actionArgs || {},
  }, "act");
  if (!r.ok) {
    _actFail("act", r);
    return null;
  }
  const data = r.data;
  const outcome = data.outcome || {};
  const label = displayName || thingId;
  // umwelt #675: `outcome.status || "?"` rendered the SAME "?" for a
  // successful `to: None` action (e.g. a board add that only appends to
  // metadata, no state transition) and one whose effect silently failed
  // (e.g. the charge-board cap refusing the add) — a success-shaped
  // refusal on the one line a human actually scans. Mirrors
  // `_outcomeWindowContent`'s existing act()-outcome fallback, which
  // already distinguishes these two cases for the floating window; the
  // console line never got the same fix.
  const failed = Array.isArray(outcome.effect_failures) &&
                 outcome.effect_failures.length > 0;
  // Reviewer-fairy catch: a to:<non-null> action with a failing effect is
  // a real, exercised combination (e.g. test_typed_things.py's
  // to:"closed" + effects:[teleport]) — status transitions independently
  // of the effects loop (server.py's act() route computes new_metadata
  // BEFORE running effects). So `failed` alone must not replace the
  // status half of the line, only append to it — otherwise a genuine
  // status change reads as a total refusal.
  const text = outcome.status
    ? (failed
        ? `${actionVerb} on ${label} → ${outcome.status} — effect refused: ${
            outcome.effect_failures[0].reason || "unknown reason"}`
        : `${actionVerb} on ${label} → ${outcome.status}`)
    : (failed
        ? `${actionVerb} on ${label} — refused: ${
            outcome.effect_failures[0].reason || "unknown reason"}`
        : `${actionVerb} on ${label} → (no status change)`);
  consoleLog(failed ? "error" : "act", text, data);
  // umwelt #957: the status kind follows `succeeded` (an effect refusal is an
  // error line naming the reason), and the scene refreshes either way -- a
  // 2xx means the server acted and may have partly moved the world.
  showActStatus(r.succeeded ? "ok" : "error", text);
  refreshScene();
  const targetEl = document.querySelector(
    `.item.thing[data-thing-id="${CSS.escape(thingId)}"]`);
  _openOutcomeWindow(label, outcome, targetEl || null);
  return outcome;
}

// ---------------------------------------------------------------------
// umwelt #655: the proposal viewer — a ggWindow opened by clicking a
// look_in tray item (renderLookIn above). Shows a kind-typed readable
// thing's body + decision log + typed actions in one focal panel, so Lei
// can read a proposal and approve/request_changes/comment/revise/retract
// in-browser instead of falling back to CLI/MCP. No new gadget class —
// reuses ggWindow (the #352 charter's proving gadget); panel/button-row/
// badge stay out of scope (chrome is `.gg-*` classes only, per #352).
//
// Flow: open the window with a loading state, then fetch /inspect
// (metadata + actions) and POST /act use (the body) in parallel. Body
// rendering reuses `_outcomeWindowContent` (the outcome window's readable
// renderer) — no second readable renderer. Action dispatch reuses
// `actAct` for the actual write; the required-args form mirrors
// openActParamsSubView's field loop (`_GG_EDITOR_FIELD_NAMES` + ggEditor)
// but renders INLINE in the viewer, never routed through the
// interact-menu sub-view DOM (separate anchored element — confusing UX).
// After a successful actAct, re-fetch /inspect and re-render header +
// decision log + action bar in place (status changes, permitted flags
// flip — e.g. approve becomes disabled once approved). The actAct outcome
// summary still surfaces via the existing outcome window; the in-viewer
// refresh is the confirmation.
//
// ESCAPE: every field is assigned via .textContent (DESIGN.md §12.10 —
// title/priority/state/author/comment/log-entry text are all public,
// attacker-influenceable), never string-concatenated into markup.
// ---------------------------------------------------------------------

let _thingViewerWindow = null;
// Cached state for the in-viewer post-action refresh: the last /inspect
// and use payloads, so a re-render after actAct can rebuild header/log/
// action bar WITHOUT re-calling use() (which would double-count a read —
// use increments use_count, a real side effect). Reset on each open.
let _thingViewerInspectData = null;
let _thingViewerUseData = null;
let _thingViewerThingId = null;
let _thingViewerDisplayName = null;

// Opens (or re-opens, if already showing) the proposal viewer for `thingId`.
// Screen-anchored, focal — larger than the outcome window
// (`min(760px, 92vw)` / `80vh` vs the outcome window's `min(720px, 90vw)` /
// `70vh`), centered via a translateX(-50%) so it reads as the primary
// surface, not a corner popup. Same shared dismiss registry (outside-click/
// Escape) every ggWindow gets free.
function openThingViewer(thingId, displayName) {
  // umwelt #936: the viewer shows one thing, so that thing is the selection.
  // Its look_in row already selected it with the precise `where`; this only
  // covers an open that did not come through a row.
  if (!state.selection || state.selection.id !== thingId) {
    selectThing(thingId, displayName, "pasted");
  }
  _thingViewerThingId = thingId;
  _thingViewerDisplayName = displayName || thingId;
  _thingViewerInspectData = null;
  _thingViewerUseData = null;
  const loading = document.createElement("div");
  loading.className = "gg-thing-viewer-loading";
  loading.textContent = "loading…";
  if (!_thingViewerWindow) {
    _thingViewerWindow = ggWindow({
      title: _thingViewerDisplayName,
      anchor: {
        mode: "screen",
        pos: { top: "10vh", left: "50%", transform: "translateX(-50%)" },
      },
      size: { maxWidth: "min(760px, 92vw)", maxHeight: "80vh" },
      closable: true,
      content: loading,
    });
  } else {
    _thingViewerWindow.update({ title: _thingViewerDisplayName, content: loading });
  }
  _thingViewerWindow.open();
  _renderThingViewer();
}

// Fetches /inspect (metadata + actions) and POST /act use (the body) in
// parallel, then renders the full viewer content. A failed/empty use
// (non-readable thing, gated readable, params-declaring thing answering
// params_spec instead of an outcome) just omits the body section — the
// viewer still shows header/log/actions, so it degrades gracefully for
// any contained thing, not just proposals.
async function _renderThingViewer() {
  const thingId = _thingViewerThingId;
  const displayName = _thingViewerDisplayName;
  const inspectP = (async () => {
    try {
      const resp = await fetch(`/inspect?thing=${encodeURIComponent(thingId)}`);
      if (!resp.ok) return null;
      return await resp.json().catch(() => null);
    } catch (e) {
      return null;
    }
  })();
  // silent: a failed use here is an expected graceful-degradation path
  // (non-readable thing, params-declaring thing, transient blip) — the
  // viewer just omits the body section. Don't hijack the global errorEl
  // banner for it; the inspect fetch above already handles the "couldn't
  // open this thing" hard-failure case with its own in-view error row.
  const useP = _actUseFetch(thingId, [], { silent: true });
  const [inspectData, useData] = await Promise.all([inspectP, useP]);
  if (!inspectData) {
    const err = document.createElement("div");
    err.className = "gg-thing-viewer-error";
    err.textContent = "couldn't open this thing";
    _thingViewerWindow.update({ content: err });
    return;
  }
  _thingViewerInspectData = inspectData;
  _thingViewerUseData = useData;
  _renderThingViewerContent();
}

// Re-renders the window content from the cached inspect/use payloads.
function _renderThingViewerContent() {
  const inspectData = _thingViewerInspectData;
  if (!inspectData) return;
  const t = inspectData.thing || {};
  _thingViewerWindow.update({
    title: t.name || _thingViewerDisplayName,
    content: _thingViewerContent(
      inspectData, _thingViewerUseData, _thingViewerThingId, _thingViewerDisplayName),
  });
}

// ---------------------------------------------------------------------
// umwelt #1049 (slice A): inline comments on a proposal, sent as ONE review.
// The reviewer selects text in the viewer body, comments on it, collects
// several drafts in a pending list, then sends them ALL in one existing
// verdict action (approve / request_changes / comment) as one composed text.
// Web UI only: no server or kinddef change, every action stays an existing act
// through _viewerAct. Drafts persist in localStorage per thing id so a closed
// window (Escape / outside click, #1058) or a reload does not lose them; every
// storage access is guarded and the list falls back to memory.
// ---------------------------------------------------------------------
const _REVIEW_QUOTE_CAP = 160;        // code points kept from a quote
const _REVIEW_SEND_LIMIT = 100000;    // characters; the server has no cap, its request body cap is 262144 bytes
const _REVIEW_DRAFTS_KEY = "umwelt.review.drafts.";
const _REVIEW_DRAFTS_MEM = new Map();
const _REVIEW_DRAFTS_FAILED = new Set();   // thing ids whose last save did not reach storage

// WIRE FORMAT -- defined ONCE, here. A later slice (#1049 slice B) PARSES this
// text back out of the action log, and the first ~175 characters are what the
// author's bell line shows, so the count leads. It must not change silently:
// change it only together with that parser and docs/web-render/GADGETS.md.
//
//   Inline review: N comment.          (N === 1; "comments." otherwise)
//   <overall text, if any, verbatim>   (trimmed; internal newlines kept)
//   <blank line>
//   1. > QUOTE
//      the comment, every non-empty line indented exactly 3 spaces
//   2. > QUOTE
//      ...
//
// Items are separated by a single newline; blank comment lines stay truly
// empty; no trailing newline or space. With N === 0 the result is just the
// trimmed overall, so a form with no drafts behaves as before.
function composeReview(overall, drafts) {
  // CRLF and a lone CR become LF first, so no carriage return reaches the wire.
  const nl = (v) => String(v == null ? "" : v).replace(/\r\n?/g, "\n");
  const text = nl(overall).trim();
  const list = Array.isArray(drafts) ? drafts : [];
  const n = list.length;
  if (!n) return text;
  const lines = [`Inline review: ${n} ${n === 1 ? "comment" : "comments"}.`];
  if (text) lines.push(text);
  lines.push("");
  list.forEach((d, i) => {
    lines.push(`${i + 1}. > ${d.quote}`);
    for (const l of nl(d.comment).trim().split("\n")) {
      lines.push(l.trim() ? `   ${l}` : "");
    }
  });
  return lines.join("\n");
}

// The quote step: whitespace runs collapse to one space, trim, empty -> null,
// and a long quote keeps its first 160 CODE POINTS (Array.from, never UTF-16
// units, so an astral character is not split) plus one "…".
function _reviewQuote(raw) {
  const s = String(raw == null ? "" : raw).replace(/\s+/g, " ").trim();
  if (!s) return null;
  const cps = Array.from(s);
  return cps.length > _REVIEW_QUOTE_CAP
    ? cps.slice(0, _REVIEW_QUOTE_CAP).join("") + "…"
    : s;
}

// A stable id per draft, so Edit / Delete / clear-after-send act on THE draft
// and not on a list position another tab may have moved.
function _reviewNewId() {
  return "d" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
}

// Keeps only {id, quote: string, comment: string} items; anything else
// (corrupt storage, a non-array) is dropped, never thrown on. An item stored
// without an id (written before ids existed) gets one; `stats.assigned`
// counts those so the caller can persist them.
function _reviewDraftsClean(v, stats) {
  if (!Array.isArray(v)) return [];
  const seen = new Set();
  return v
    .filter((d) => d && typeof d.quote === "string" && typeof d.comment === "string")
    .map((d) => {
      let id = d.id;
      // missing, or a duplicate of an earlier draft's id (crafted / merged
      // storage): a fresh id, so Delete on one row can never hit another.
      if (typeof id !== "string" || !id || seen.has(id)) {
        id = _reviewNewId();
        if (stats) stats.assigned += 1;
      }
      seen.add(id);
      return { id, quote: d.quote, comment: d.comment };
    });
}

// Load: storage first; if storage is absent or throws, the in-memory list for
// that thing id (so a disabled localStorage still works for the session). While the last save for this thing did NOT reach storage
// (full / disabled), memory is authoritative: storage may still hold an older
// array and must not win. A getItem that SUCCEEDS is the truth, even when it
// answers "no entry": another tab deleted the last draft or sent the review,
// and the stale memory copy must not bring it back (it is dropped). Keyed by
// thing id, so two things never mix.
function _reviewDraftsLoad(thingId) {
  if (_REVIEW_DRAFTS_FAILED.has(thingId)) {
    return _reviewDraftsClean(_REVIEW_DRAFTS_MEM.get(thingId));
  }
  let raw = null;
  try {
    raw = localStorage.getItem(_REVIEW_DRAFTS_KEY + thingId);
  } catch (e) {
    // storage unreadable (absent / denied): the memory list is all there is
    return _reviewDraftsClean(_REVIEW_DRAFTS_MEM.get(thingId));
  }
  if (raw === null || raw === undefined) {
    _REVIEW_DRAFTS_MEM.delete(thingId);
    return [];
  }
  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    parsed = null;
  }
  const stats = { assigned: 0 };
  const list = _reviewDraftsClean(parsed, stats);
  if (list.length) _REVIEW_DRAFTS_MEM.set(thingId, list); else _REVIEW_DRAFTS_MEM.delete(thingId);
  if (stats.assigned) _reviewDraftsSave(thingId, list);   // persist the ids just assigned
  return _reviewDraftsClean(list);
}

// Saves the list. Returns {list, stored}: `stored` is false when the write did
// not reach storage (private mode, quota); the thing is then marked failed and
// memory stays authoritative until a save works again.
function _reviewDraftsSave(thingId, drafts) {
  const list = _reviewDraftsClean(drafts);
  if (list.length) _REVIEW_DRAFTS_MEM.set(thingId, list); else _REVIEW_DRAFTS_MEM.delete(thingId);
  let stored = true;
  try {
    if (list.length) {
      localStorage.setItem(_REVIEW_DRAFTS_KEY + thingId, JSON.stringify(list));
    } else {
      localStorage.removeItem(_REVIEW_DRAFTS_KEY + thingId);
    }
  } catch (e) {
    stored = false;   // the in-memory list above still works
  }
  if (stored) _REVIEW_DRAFTS_FAILED.delete(thingId); else _REVIEW_DRAFTS_FAILED.add(thingId);
  return { list, stored };
}

function _reviewDraftsAdd(thingId, draft) {
  const list = _reviewDraftsLoad(thingId);
  list.push({ id: _reviewNewId(), quote: draft.quote, comment: draft.comment });
  return _reviewDraftsSave(thingId, list);
}

// Edit replaces the comment of the draft with this id IN PLACE (same position,
// same quote). An id that is no longer there (deleted in another tab) is a
// no-op: it never lands on a different draft.
function _reviewDraftsEdit(thingId, id, comment) {
  const list = _reviewDraftsLoad(thingId);
  const i = list.findIndex((d) => d.id === id);
  if (i < 0) return { list, stored: true };
  list[i] = { id: list[i].id, quote: list[i].quote, comment };
  return _reviewDraftsSave(thingId, list);
}

function _reviewDraftsDelete(thingId, id) {
  const list = _reviewDraftsLoad(thingId);
  const i = list.findIndex((d) => d.id === id);
  if (i < 0) return { list, stored: true };
  list.splice(i, 1);
  return _reviewDraftsSave(thingId, list);
}

// With `sent` (a list of {id, comment}: what was composed into a sent text),
// removes only the drafts whose id AND comment both match, so a draft another
// tab added meanwhile, or edited after the click (that text was never sent),
// survives; without it, empties the thing.
function _reviewDraftsClear(thingId, sent) {
  if (!Array.isArray(sent)) return _reviewDraftsSave(thingId, []);
  const was = new Map(sent.map((d) => [d.id, d.comment]));
  return _reviewDraftsSave(thingId, _reviewDraftsLoad(thingId)
    .filter((d) => !(was.has(d.id) && was.get(d.id) === d.comment)));
}

// The cleaned quote of the CURRENT selection if it is non-collapsed and STARTS
// inside `bodyEl`, else null. Wherever it ENDS, the end is clamped to the body:
// a triple-click on the last line ends at (the node after the body, 0), and a
// drag that overshoots below the body ends in the control's own label or
// further away; both still quote the body part. The quote is taken from the
// clamped range, never from the browser's own selection string, so no outside
// text can ride along. A start outside the body (the title) stays rejected.
// Read at call time, never cached.
function _reviewSelectionQuote(bodyEl) {
  let sel = null;
  try {
    sel = window.getSelection();
  } catch (e) {
    return null;
  }
  if (!sel || sel.rangeCount < 1 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  if (!range.startContainer || !bodyEl.contains(range.startContainer)) return null;
  let clamped = range;
  if (!bodyEl.contains(range.endContainer)) {
    clamped = range.cloneRange();
    clamped.setEnd(bodyEl, bodyEl.childNodes.length);
  }
  return _reviewQuote(clamped.toString());
}

// True when the quote (whitespace ignored, a trailing "…" from the 160-cap
// removed) is no longer a substring of the body's text, given ONCE per list
// render as `squashedBody` (the body's textContent with all whitespace removed;
// a big body is slow to squash per draft): the draft then gets a
// "not in current text" mark. Whitespace is dropped on both sides, not just
// collapsed, because textContent has no space at block boundaries where a
// selection's text has a newline. The quote itself is never changed.
function _reviewIsStale(quote, squashedBody) {
  const q = String(quote).replace(/…$/, "").replace(/\s+/g, "");
  if (!q) return false;
  return !squashedBody.includes(q);
}

// Opens the no-toolbar editor (#1050) in `host` for one draft: the quote (plain
// text), the editor, and a confirm + Cancel row. A blank (after trim) comment
// is ignored and leaves the editor open.
function _reviewOpenEditor(host, quote, value, confirmLabel, onSave) {
  host.replaceChildren();
  const q = document.createElement("div");
  q.className = "gg-review-editor-quote";
  q.textContent = quote;
  const ed = ggEditor({
    rows: 4, toolbar: false, value, placeholder: "Your comment on this passage…",
  });
  const row = document.createElement("div");
  row.className = "params-form-run-row";
  const ok = document.createElement("button");
  ok.type = "button";
  ok.className = "gg-btn";
  ok.textContent = confirmLabel;
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "gg-btn";
  cancel.textContent = "Cancel";
  ok.addEventListener("click", () => {
    const v = ed.el.value.trim();
    if (!v) return;
    onSave(v);
    host.replaceChildren();
  });
  cancel.addEventListener("click", () => host.replaceChildren());
  row.appendChild(ok);
  row.appendChild(cancel);
  host.appendChild(q);
  host.appendChild(ed.el);
  host.appendChild(row);
  ed.el.focus();
}

// Re-renders the pending list in place (so the body's scroll and selection are
// untouched). Quote and comment are textContent only. Hidden when empty.
function _reviewRenderList(listEl, thingId, host, bodyEl) {
  const drafts = _reviewDraftsLoad(thingId);
  listEl.replaceChildren();
  listEl.style.display = drafts.length ? "" : "none";
  if (!drafts.length) return;
  const squashedBody = String(bodyEl.textContent).replace(/\s+/g, "");
  const head = document.createElement("div");
  head.className = "gg-review-list-header";
  head.textContent = `Pending inline comments (${drafts.length})`;
  listEl.appendChild(head);
  if (_REVIEW_DRAFTS_FAILED.has(thingId)) {
    const warn = document.createElement("div");
    warn.className = "gg-review-warning";
    warn.textContent = "could not save drafts in this browser (storage full or disabled); " +
      "changes, including deletes, will be lost if you close or reload this page";
    listEl.appendChild(warn);
  }
  drafts.forEach((d) => {
    const row = document.createElement("div");
    row.className = "gg-review-draft";
    const quote = document.createElement("div");
    quote.className = "gg-review-draft-quote";
    quote.textContent = d.quote;
    row.appendChild(quote);
    if (_reviewIsStale(d.quote, squashedBody)) {
      const stale = document.createElement("div");
      stale.className = "gg-review-draft-stale";
      stale.textContent = "not in current text";
      row.appendChild(stale);
    }
    const comment = document.createElement("div");
    comment.className = "gg-review-draft-comment";
    comment.textContent = d.comment;
    const actions = document.createElement("div");
    actions.className = "gg-review-draft-actions";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "gg-btn";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => {
      _reviewOpenEditor(host, d.quote, d.comment, "Save", (v) => {
        _reviewDraftsEdit(thingId, d.id, v);
        _reviewRenderList(listEl, thingId, host, bodyEl);
      });
    });
    const del = document.createElement("button");
    del.type = "button";
    del.className = "gg-btn";
    del.textContent = "Delete";
    del.addEventListener("click", () => {
      _reviewDraftsDelete(thingId, d.id);
      _reviewRenderList(listEl, thingId, host, bodyEl);
    });
    actions.appendChild(edit);
    actions.appendChild(del);
    row.appendChild(comment);
    row.appendChild(actions);
    listEl.appendChild(row);
  });
}

// True when at least one typed action takes a `comment` (any case, as the form
// does) and is not explicitly forbidden: only then can a draft ever be sent.
function _reviewHasCommentAction(actions) {
  return (Array.isArray(actions) ? actions : []).some((a) =>
    a && a.permitted !== false && Array.isArray(a.requires) &&
    a.requires.some((f) => String(f).toLowerCase() === "comment"));
}

// The review section under the viewer body: the "Comment on selected text"
// control, the editor host, and the pending list. The control is enabled only
// while a valid selection exists (selectionchange); mousedown is cancelled so
// clicking it does not clear the selection; the quote is read AT CLICK TIME.
function _reviewDraftsSection(thingId, bodyEl) {
  const sec = document.createElement("div");
  sec.className = "gg-review";
  const ctl = document.createElement("button");
  ctl.type = "button";
  ctl.className = "gg-btn gg-review-comment-btn";
  ctl.textContent = "Comment on selected text";
  ctl.disabled = true;
  const host = document.createElement("div");
  host.className = "gg-review-editor-host";
  const listEl = document.createElement("div");
  listEl.className = "gg-review-list";
  ctl.addEventListener("mousedown", (ev) => ev.preventDefault());
  ctl.addEventListener("click", () => {
    const quote = _reviewSelectionQuote(bodyEl);
    if (quote === null) return;
    _reviewOpenEditor(host, quote, "", "Add", (v) => {
      _reviewDraftsAdd(thingId, { quote, comment: v });
      _reviewRenderList(listEl, thingId, host, bodyEl);
      try {
        const s = window.getSelection();
        if (s) s.removeAllRanges();
      } catch (e) {
        // best effort: the selection UI is cosmetic
      }
      ctl.disabled = true;
    });
  });
  // One listener per section; it removes itself once the section has been
  // mounted and then detached (the viewer re-renders its content).
  let seen = false;
  const onSelectionChange = () => {
    if (sec.isConnected) {
      seen = true;
    } else if (seen) {
      document.removeEventListener("selectionchange", onSelectionChange);
      return;
    }
    ctl.disabled = _reviewSelectionQuote(bodyEl) === null;
  };
  document.addEventListener("selectionchange", onSelectionChange);
  sec.appendChild(ctl);
  sec.appendChild(host);
  sec.appendChild(listEl);
  _reviewRenderList(listEl, thingId, host, bodyEl);
  return sec;
}

// Builds the full viewer content DOM. Every public/attacker-influenceable
// field is .textContent-only — never string-concatenated into markup.
function _thingViewerContent(inspectData, useData, thingId, displayName) {
  const wrap = document.createElement("div");
  wrap.className = "gg-thing-viewer";
  const t = inspectData.thing || {};
  const meta = t.metadata || {};

  // Header: title (fall back to thing.name), priority badge, kind-state,
  // author. CRITICAL: `meta.status` is the KIND-STATE (draft/submitted/
  // approved/...), NOT `t.status` (the live/binned/landfilled LIFECYCLE) —
  // the two are distinct columns and must never be conflated.
  const header = document.createElement("div");
  header.className = "gg-thing-viewer-header";
  const titleEl = document.createElement("div");
  titleEl.className = "gg-thing-viewer-title";
  titleEl.textContent = meta.title || t.name || "";
  header.appendChild(titleEl);
  const metaRow = document.createElement("div");
  metaRow.className = "gg-thing-viewer-meta";
  if (meta.priority) {
    const badge = document.createElement("span");
    badge.className = "gg-thing-viewer-priority";
    badge.textContent = meta.priority;
    metaRow.appendChild(badge);
  }
  if (meta.status) {
    const state = document.createElement("span");
    state.className = "gg-thing-viewer-state";
    state.textContent = meta.status;
    metaRow.appendChild(state);
  }
  if (t.author) {
    const author = document.createElement("span");
    author.className = "gg-thing-viewer-author";
    author.textContent = `by ${t.author}`;
    metaRow.appendChild(author);
  }
  header.appendChild(metaRow);
  wrap.appendChild(header);

  // The typed actions (see the action bar below, which reads this same list).
  // umwelt #936: /inspect's `actions` now also carries the kind's built-ins
  // (use/read/edit/look_in/look_at, which never carry a `permitted` key); this
  // bar dispatches only typed actions through act(), so keep just those.
  const actions = (Array.isArray(inspectData.actions) ? inspectData.actions : [])
    .filter((a) => a && "permitted" in a);

  // Body: reuse the outcome window's readable renderer. Only when use()
  // returned a real `outcome` (a readable's content). A params_spec
  // response (params-declaring thing) or a failed/empty use omits the body
  // section entirely — no second readable renderer, no markdown (out of
  // scope; matches how agents read proposals).
  if (useData && useData.outcome) {
    const bodyWrap = document.createElement("div");
    bodyWrap.className = "gg-thing-viewer-body";
    bodyWrap.appendChild(_outcomeWindowContent(useData.outcome));
    wrap.appendChild(bodyWrap);
    // umwelt #1049: select text in the body, comment on it, collect drafts.
    // Only where a draft can be SENT: a permitted comment-bearing typed action.
    if (_reviewHasCommentAction(actions)) {
      wrap.appendChild(_reviewDraftsSection(thingId, bodyWrap));
    }
  }

  // Decision log: `metadata.action_log` — list of {action, by, at, args}.
  // General (any typed thing may carry one); proposals populate it fully.
  // `args.comment` shown when present (approve/request_changes/comment).
  const log = Array.isArray(meta.action_log) ? meta.action_log : [];
  if (log.length) {
    const logWrap = document.createElement("div");
    logWrap.className = "gg-thing-viewer-log";
    const logHeader = document.createElement("div");
    logHeader.className = "gg-thing-viewer-log-header";
    logHeader.textContent = "decision log";
    logWrap.appendChild(logHeader);
    for (const entry of log) {
      const row = document.createElement("div");
      row.className = "gg-thing-viewer-log-entry";
      const head = document.createElement("div");
      head.className = "gg-thing-viewer-log-head";
      const at = entry.at ? new Date(entry.at).toLocaleString() : "";
      head.textContent = `${entry.action || "?"} — ${entry.by || "?"}` +
        (at ? ` · ${at}` : "");
      row.appendChild(head);
      const args = entry.args || {};
      if (args.comment) {
        const c = document.createElement("div");
        c.className = "gg-thing-viewer-log-comment";
        c.textContent = args.comment;
        row.appendChild(c);
      }
      logWrap.appendChild(row);
    }
    wrap.appendChild(logWrap);
  }

  // Action bar: from `inspectData.actions` (gateway's /inspect enrichment,
  // same {verb,label,requires,permitted} shape openInteractMenuForThing
  // consumes). Reuses that function's typed-action branch verbatim:
  // permitted === false -> disabled (flag, don't filter — the server is the
  // authority, /act is the real gate); click -> required-args form if
  // `requires` non-empty, else actAct directly. `.gg-btn` for the buttons.
  // `actions` is the typed-action list built above (before the body).
  if (actions.length) {
    const bar = document.createElement("div");
    bar.className = "gg-thing-viewer-actions";
    for (const action of actions) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "gg-btn";
      btn.textContent = action.label;
      const requires = Array.isArray(action.requires) ? action.requires : [];
      if (action.permitted === false) btn.disabled = true;
      // umwelt #967: `available` is the SERVER's from-state rule (thing_actions,
      // the same helper act()'s 409 gate uses). `permitted` is who, `available`
      // is when; the client keeps no status list for buttons, so an action that
      // is valid in every state (e.g. comment, from:[]) stays enabled.
      if (action.available === false) {
        btn.disabled = true;
        btn.title = _unavailableTitle(action);
      }
      btn.addEventListener("click", () => {
        if (requires.length) {
          _openViewerActParams(thingId, action.verb, requires, displayName);
        } else {
          _viewerAct(thingId, action.verb, {}, displayName);
        }
      });
      bar.appendChild(btn);
    }
    wrap.appendChild(bar);
  }

  return wrap;
}

// The viewer's required-args form for a typed action whose `requires` is
// non-empty (e.g. approve/request_changes/comment all `requires:["comment"]`).
// Mirrors openActParamsSubView's field loop + submit exactly (same
// `_actFieldInput` editor-vs-<input> decision, same
// "blank field omitted, not sent empty" submit posture, same
// params-form-* CSS classes) but renders INLINE in the viewer window
// instead of the interact-menu sub-view DOM — a separate anchored element
// would be confusing UX. On submit -> `_viewerAct` (which calls `actAct`
// then refreshes). Cancel restores the viewer from cached inspect data.
function _openViewerActParams(thingId, actionVerb, requires, displayName) {
  const wrap = document.createElement("div");
  wrap.className = "gg-thing-viewer-actparams";
  const header = document.createElement("div");
  header.className = "gg-thing-viewer-actparams-header";
  header.textContent = `${_titleCaseFieldName(actionVerb)} — ${displayName || thingId}`;
  wrap.appendChild(header);

  // umwelt #1049: the comment field of a verdict form carries the pending
  // inline comments (if any) for this thing. With none, nothing below
  // differs from the pre-#1049 form.
  const commentField = requires.find((f) => String(f).toLowerCase() === "comment");
  const draftCount = commentField === undefined ? 0 : _reviewDraftsLoad(thingId).length;
  let draftNote = null;
  if (draftCount) {
    draftNote = document.createElement("div");
    draftNote.className = "gg-thing-viewer-actparams-note";
    draftNote.textContent = `${draftCount} inline comment${draftCount === 1 ? "" : "s"} will be added`;
    wrap.appendChild(draftNote);
  }

  const fieldsEl = document.createElement("div");
  const inputsByField = {};
  for (const fieldName of requires) {
    const row = document.createElement("div");
    row.className = "params-form-row";
    const label = document.createElement("label");
    label.className = "params-form-label";
    label.textContent = _titleCaseFieldName(fieldName);
    row.appendChild(label);
    // Same decision as openActParamsSubView, via the shared helper (#1050).
    const input = _actFieldInput(fieldName);
    row.appendChild(input);
    fieldsEl.appendChild(row);
    inputsByField[fieldName] = input;
  }
  wrap.appendChild(fieldsEl);

  const btnRow = document.createElement("div");
  btnRow.className = "params-form-run-row";
  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.className = "gg-btn";
  submitBtn.textContent = "Submit";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "gg-btn";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", _renderThingViewerContent);
  let submitPending = false;
  submitBtn.addEventListener("click", () => {
    // Same as openActParamsSubView: a blank field is omitted, not sent as
    // "", so the server's required-arg gate reports the SAME field name
    // back on a still-missing value. The server's `requires` gate is the
    // real enforcement; this is purely UX.
    const formValues = {};
    for (const [fieldName, input] of Object.entries(inputsByField)) {
      const v = input.value.trim();
      if (v) formValues[fieldName] = v;
    }
    // umwelt #1049: pending inline comments ride in the comment field as ONE
    // composed text (composeReview); the reviewer's typed text is the overall.
    const drafts = commentField === undefined ? [] : _reviewDraftsLoad(thingId);
    let onSuccess;
    if (drafts.length) {
      const composed = composeReview(formValues[commentField] || "", drafts);
      if (composed.length > _REVIEW_SEND_LIMIT) {
        _showThingViewerRefusal(
          `too long to send: ${composed.length} characters, limit ${_REVIEW_SEND_LIMIT} ` +
          "(the server accepts about 256 KB per request)");
        return;
      }
      formValues[commentField] = composed;
      // Cleared once the act is accepted (a refusal keeps the drafts), and only
      // the drafts (id AND text) that were composed into the sent text: one
      // another tab added or edited meanwhile stays.
      const sent = drafts.map((d) => ({ id: d.id, comment: d.comment }));
      onSuccess = () => _reviewDraftsClear(thingId, sent);
    }
    // Single-shot: Submit is disabled from here and re-enabled ONLY when the act
    // comes back refused / failed. After an accepted act it stays disabled (the
    // re-render replaces the form), so a second click in the window before that
    // cannot send again (it used to re-send the bare overall text as a second
    // comment). The handler is shared with plain comments, which double-sent
    // before this too; that is fixed here for both.
    if (submitPending) return;
    submitPending = true;
    submitBtn.disabled = true;
    let request;
    try {
      request = Promise.resolve(_viewerAct(thingId, actionVerb, formValues, displayName, onSuccess));
    } catch (e) {
      request = Promise.reject(e);
    }
    request
      .then((accepted) => {
        if (!accepted) {
          submitPending = false;
          submitBtn.disabled = false;
        } else if (draftNote &&
            _thingViewerWindow.el.querySelector(".gg-thing-viewer-actparams-note") === draftNote) {
          // Accepted, but the form is still up (the refresh failed): the drafts
          // are gone now, so the note must not keep promising them.
          draftNote.textContent = "review recorded, view could not refresh";
        }
      })
      .catch((err) => {
        // The request itself failed (it threw / rejected): not stuck, not silent.
        submitPending = false;
        submitBtn.disabled = false;
        _showThingViewerRefusal(`request failed: ${err && err.message ? err.message : err}`);
      });
  });
  btnRow.appendChild(submitBtn);
  btnRow.appendChild(cancelBtn);
  wrap.appendChild(btnRow);

  _thingViewerWindow.update({ content: wrap });
  const firstInput = Object.values(inputsByField)[0];
  if (firstInput) firstInput.focus();
}

// umwelt #945: shows an act refusal inside the open viewer, verbatim
// (.textContent only -- server messages can echo user input). One row,
// replaced on each refusal, placed at the top of the current content.
function _showThingViewerRefusal(message) {
  if (!_thingViewerWindow) return;
  const root = _thingViewerWindow.el;
  let row = root.querySelector(".gg-thing-viewer-refusal");
  if (!row) {
    row = document.createElement("div");
    row.className = "gg-thing-viewer-error gg-thing-viewer-refusal";
    const content = root.querySelector(".gg-thing-viewer-actparams, .gg-thing-viewer");
    if (content) content.prepend(row); else return;
  }
  row.textContent = message;
}

// Runs a typed action via `actAct` (the real /act write — the viewer never
// reimplements approve's declarative effects), then re-fetches /inspect
// and re-renders header + decision log + action bar in the SAME window
// (status changes, permitted flags flip). Only /inspect is re-fetched —
// use() is NOT re-called (it would double-count a read; the body doesn't
// change on an act). The actAct outcome summary still surfaces via the
// existing outcome window; this refresh is the in-view confirmation.
async function _viewerAct(thingId, actionVerb, actionArgs, displayName, onSuccess) {
  const outcome = await actAct(thingId, actionVerb, actionArgs, displayName);
  // umwelt #945: the viewer may have been re-opened on another thing while
  // the act was in flight; never write this act's result into that other
  // thing's viewer. (A plain close only hides the window and leaves
  // _thingViewerThingId set, so a late write then lands harmlessly in the
  // hidden window, which the next open replaces.)
  const stillShowing = () => _thingViewerThingId === thingId;
  if (!outcome) {
    // A refusal must be readable WHERE the human acted. actAct already put
    // the server's message in the page-level error banner, which the viewer
    // (and the say composer) can cover; repeat it verbatim inside the viewer
    // and leave the form up so the typed comment is not lost.
    if (stillShowing()) _showThingViewerRefusal(errorEl.textContent || `${actionVerb} failed`);
    return false;
  }
  // A 2xx can still carry a refused effect (actAct's own `failed` case);
  // that is a refusal too, and the human must see it in the viewer.
  const fails = Array.isArray(outcome.effect_failures) ? outcome.effect_failures : [];
  // umwelt #1049: an accepted act that carried effect_failures was still
  // RECORDED (measured: the action log has the entry; only an effect such as
  // notifying the author failed), so for a review send the message says that
  // plainly, worded by the failure's `type` ({type, index, reason}), with a
  // leading "failed: " of the server's reason stripped so it does not read
  // "failed: failed:". Any other act keeps its wording byte for byte.
  // `onSuccess` runs for any accepted act (2xx) FIRST, before the message is
  // built and before the re-render below: the refreshed viewer shows no stale
  // drafts, a resend cannot duplicate the recorded review, and nothing that
  // goes wrong while building the text can skip the clear of an accepted act.
  // A falsy outcome (above) keeps them.
  if (typeof onSuccess === "function") onSuccess();
  // A non-object entry (not server-shaped) counts as {} rather than throwing.
  const first = fails[0] && typeof fails[0] === "object" ? fails[0] : {};
  let refusal = null;
  if (fails.length) {
    if (typeof onSuccess === "function") {
      const why = String(first.reason || "unknown reason").replace(/^failed:\s*/i, "");
      refusal = first.type === "notify_author"
        ? `review recorded, but notifying the author failed: ${why}`
        : `review recorded, but an effect failed (${first.type || "unknown"}): ${why}`;
    } else {
      refusal = `${actionVerb} — effect refused: ${first.reason || "unknown reason"}`;
    }
  }
  try {
    const resp = await fetch(`/inspect?thing=${encodeURIComponent(thingId)}`);
    if (resp.ok) {
      const data = await resp.json().catch(() => null);
      if (data && stillShowing()) {
        _thingViewerInspectData = data;
        _renderThingViewerContent();
      }
    }
  } catch (e) {
    // best-effort refresh — the act itself already succeeded and surfaced
    // its outcome via actAct's outcome window; a failed refresh just leaves
    // the viewer at its pre-action state.
  }
  if (refusal && stillShowing()) _showThingViewerRefusal(refusal);
  return true;
}

// `take` — POST /act {intent:"take", args:{thing}}. umwelt #267 "RPG action
// set". Same csrfToken-presence guard/fetch-try-catch/!resp.ok handling
// shape as actUse above — thing_id/holder/load/capacity outcomes here have
// no natural-language `briefing` field to fall back through, so the success
// log is a plain fixed string.
async function actTake(thingId, displayName) {
  const r = await actPost("take", { thing: thingId }, "take");
  if (!r.succeeded) {
    _actFail("take", r);
    // Resolved 2xx but refused: the world may still have moved.
    if (r.ok) refreshScene();
    return false;
  }
  consoleLog("take", `took ${displayName || thingId}`);
  showActStatus("ok", `took ${displayName || thingId}`);
  refreshScene();
  // umwelt #267 review fix: this is what makes Drop's target resolvable
  // at all (see _heldThing's own declaration above) — the thing you just
  // took is now the one thing Drop knows how to act on.
  _heldThing = { id: thingId, name: displayName };
  return true;
}

// `drop` — POST /act {intent:"drop", args:{thing}}. umwelt #267 "RPG action
// set". Same shape as actTake above.
async function actDrop(thingId, displayName) {
  const r = await actPost("drop", { thing: thingId }, "drop");
  if (!r.succeeded) {
    _actFail("drop", r);
    // Resolved 2xx but refused: the world may still have moved.
    if (r.ok) refreshScene();
    // umwelt #267 review round 2: a 403/404/409 here means _heldThing
    // itself is stale (not currently carried / not the holder / gone) --
    // e.g. a second tab on the same session already dropped or otherwise
    // lost it. Clear it so the NEXT Drop click re-syncs via
    // _resolveHeldThing's /inventory fallback instead of retrying the
    // same doomed thing_id forever. A network-error/exception (caught
    // above, never reaches here) or a 401 (session itself is gone, not
    // the reference) don't imply the reference is bad, so this only
    // clears on the three status codes that specifically mean so.
    if ([403, 404, 409].includes(r.status)) {
      _heldThing = null;
    }
    return false;
  }
  consoleLog("drop", `dropped ${displayName || thingId}`);
  showActStatus("ok", `dropped ${displayName || thingId}`);
  refreshScene();
  // umwelt #267 review fix: you no longer hold this thing, so Drop has
  // nothing to act on until the next Take (or a fresh /inventory fetch
  // in _resolveHeldThing, if you're still carrying something else).
  _heldThing = null;
  return true;
}

// `voice` — forum #282 voice v1, manifest-fetch transport. Deliberately
// quieter than actUse/actTake/actDrop above: an unenrolled speaker (404
// voice_not_enrolled) or an unavailable voice service (502/503) are
// expected, common outcomes here, not bugs worth an errorEl/console-
// visible error dump -- the button just returns to its idle label.
// `buttonEl` (the clicked <button>, from renderCrossing) is disabled for
// the duration of playback so a double-click can't fire a second
// overlapping manifest+section sequence for the same crossing.
//
// Sequencing: POST /voice/manifest once (short request, no synth wait --
// see gateway.py's own docstring) to learn the section count, then walk
// `/voice/section` sequentially -- each section's audio is fetched WHILE
// the previous one plays (prefetch-next-during-current), so gen-time and
// play-time overlap exactly the way Kepler's architecture ruling
// describes. If a section's synth is slower than the prior section's
// playback, `await` on the prefetch promise below simply takes longer
// before the next `audio.play()` -- a visible brief pause, no separate
// "buffering" UI state and no manifest re-poll needed for v1 (the
// lazy-generate-on-fetch backend already makes this fall out for free;
// see gateway.py's PR #374 design notes for the fuller argument).
async function _fetchVoiceSection(url, crossingId, index) {
  let resp;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: { "X-Csrf-Token": state.csrfToken },
    });
  } catch (e) {
    ggLog("voice", crossingId, "warn", "section fetch failed (network)",
          { index, error: String(e) });
    return null;
  }
  if (!resp.ok) {
    ggLog("voice", crossingId, "warn", "section fetch failed",
          { index, status: resp.status });
    return null;
  }
  return resp.blob();
}

function _playVoiceBlob(url, crossingId, index) {
  return new Promise((resolve) => {
    const audio = new Audio(url);
    const done = () => {
      URL.revokeObjectURL(url);
      resolve();
    };
    audio.addEventListener("ended", done);
    audio.play().catch((err) => {
      // A rejected play() is almost always the browser's autoplay policy
      // (a user gesture's activation can lapse across a long synth
      // await). Surface it -- a silent catch here hid exactly this class
      // of bug (pre-v1 actVoice's own lesson, kept).
      ggLog("voice", crossingId, "warn", "playback blocked",
            { index, error: String(err) });
      done();
    });
  });
}

async function actVoice(crossingId, buttonEl) {
  if (!state.csrfToken) {
    // No active session -- nothing to authenticate the request with.
    // Quiet degrade (no errorEl/console noise): a spectator viewing the
    // page with no login is an expected state, not a broken one.
    buttonEl.disabled = true;
    buttonEl.title = "log in to listen";
    return;
  }
  const original = buttonEl.textContent;
  buttonEl.disabled = true;
  buttonEl.textContent = "…";

  let manifest;
  try {
    const resp = await fetch("/voice/manifest", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Csrf-Token": state.csrfToken,
      },
      body: JSON.stringify({ crossing_id: crossingId }),
    });
    if (!resp.ok) {
      buttonEl.textContent = original;
      if (resp.status === 404) {
        // voice_not_enrolled (or an unknown crossing_id, same status) --
        // this speaker has no voice yet. Stays disabled with an
        // explanatory title rather than inviting a retry that will fail
        // identically every time.
        buttonEl.disabled = true;
        buttonEl.title = "no voice yet";
      } else {
        // Any other failure (503 unconfigured, 502 unreachable, ...) --
        // transient/operational, worth letting the user try again.
        buttonEl.disabled = false;
      }
      return;
    }
    manifest = await resp.json();
  } catch (e) {
    buttonEl.textContent = original;
    buttonEl.disabled = false;
    return;
  }

  const urls = manifest.urls || [];
  ggLog("voice", crossingId, "debug", "manifest",
        { sections: manifest.sections, ready: manifest.ready });

  if (urls.length === 0) {
    buttonEl.textContent = original;
    buttonEl.disabled = false;
    return;
  }

  buttonEl.textContent = "■";
  let prefetch = _fetchVoiceSection(urls[0], crossingId, 0);
  for (let i = 0; i < urls.length; i++) {
    const blob = await prefetch;
    // Kick off the NEXT section's fetch before awaiting this one's
    // playback -- the overlap is the whole point (see docstring above).
    prefetch = i + 1 < urls.length
      ? _fetchVoiceSection(urls[i + 1], crossingId, i + 1) : null;
    if (!blob) break; // _fetchVoiceSection already logged why
    ggLog("voice", crossingId, "debug", "playing_section", { index: i });
    const objUrl = URL.createObjectURL(blob);
    await _playVoiceBlob(objUrl, crossingId, i);
  }

  buttonEl.textContent = original;
  buttonEl.disabled = false;
}

interactMenuCloseEl.addEventListener("click", () => {
  closeInteractMenu();
});

// Click-elsewhere-to-dismiss, same convention as the inspect panel above.
document.addEventListener("click", (ev) => {
  if (!interactMenuEl.classList.contains("hidden")
      && !ev.composedPath().includes(interactMenuEl)) {
    closeInteractMenu();
  }
});

// Escape-to-dismiss — a keyboard-equivalent to the outside-click dismiss,
// useful on desktop where the menu renders as a small popup rather than a
// full-width bottom sheet (see index.html's @media rule). Unchanged by the
// two-lane keyboard controls below — kept as its own listener.
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !interactMenuEl.classList.contains("hidden")) {
    closeInteractMenu();
  }
});

// umwelt #938: elements that own their own Enter activation. A focused
// <button>, link, role=button row (the look_in rows), etc. already acts on
// Enter itself (native click, or its own keydown listener). Without this
// guard the document-level handler below ALSO fired on the same keypress
// (activateCursorTarget() / activateHighlightedMenuButton()), and
// preventDefault() there would even cancel a native button's click
// (measured in headless Chrome with a real keypress: on the old code the
// button's click did not fire and only the street action ran). Matching
// by closest() against ONE selector covers the whole class, so any future
// focusable interactive element is covered by construction. Enter-only: the
// handler does not act on Space, and arrows deliberately ignore focus.
const INTERACTIVE_OWN_ACTIVATION_SELECTOR =
  "button, a[href], select, summary, [role=button], [role=link], "
  + "[role=menuitem], [contenteditable]:not([contenteditable=\"false\"])";

// Two-lane keyboard controls (forum #267 reply 79): ArrowLeft/ArrowRight
// move the street cursor, Enter activates its target; while the interact-
// menu is open, ArrowUp/ArrowDown move its highlight and Enter activates
// the highlighted button instead. Umwelt #323: when the interact-menu is
// CLOSED, ArrowUp/ArrowDown are no longer a no-op — they cycle the
// interact-target lane (moveLane) instead. See this file's module-header
// note ("umwelt #323") for the full design.
document.addEventListener("keydown", (ev) => {
  const t = ev.target;
  const isTextInput = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA");

  // Text inputs ALWAYS keep their own keys (viewer/place fields, the say/
  // talk composer textareas, any other current or future <input>/
  // <textarea> on this page) — arrows move the caret, typing types,
  // nothing here is ever stolen. umwelt #386: the composer's key-binding
  // convention (umwelt#412: Enter-newline, Shift/Ctrl/Cmd+Enter-sends) is
  // wired directly on the composer element (wireComposerTextarea, see the
  // module-header "umwelt #386" note above) rather than here — a
  // document-level handler can't tell the modifier keys apart from the
  // composer's own element-level listener without duplicating that same
  // check, and firing BOTH would double-send (this handler runs in the
  // bubble phase after
  // the element-level one already acted). So this generic text-input
  // branch is now a pure no-steal passthrough for every text input on the
  // page, composer textareas included.
  if (isTextInput) {
    return;
  }

  // umwelt #938: Enter on a focused interactive element belongs to that
  // element — do nothing here and do NOT preventDefault (its native
  // activation must still happen). Applies to both the menu-open and
  // menu-closed branches below; arrows are unaffected.
  if (ev.key === "Enter" && t && typeof t.closest === "function"
      && t.closest(INTERACTIVE_OWN_ACTIVATION_SELECTOR)) {
    return;
  }

  if (!interactMenuEl.classList.contains("hidden")) {
    // Interact-menu open: arrows navigate its buttons, Enter activates the
    // highlighted one. Escape is handled by the listener above, unchanged.
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      moveMenuHighlight(1);
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      moveMenuHighlight(-1);
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      activateHighlightedMenuButton();
    }
    return;
  }

  // Interact-menu closed: the street cursor itself. Umwelt #323:
  // ArrowLeft/ArrowRight are absolute horizontal movement (moveCursor,
  // umwelt #342: now indexing navColumns — on-screen-x COLUMNS, one
  // keypress per visual column regardless of how many lanes occupy it);
  // ArrowUp/ArrowDown select the interact-target lane (moveLane) at the
  // current horizontal position; Enter activates whichever item
  // currentLaneTarget() resolves to (activateCursorTarget).
  if (ev.key === "ArrowLeft") {
    ev.preventDefault();
    moveCursor(-1);
  } else if (ev.key === "ArrowRight") {
    ev.preventDefault();
    moveCursor(1);
  } else if (ev.key === "ArrowUp") {
    ev.preventDefault();
    moveLane(-1);
  } else if (ev.key === "ArrowDown") {
    ev.preventDefault();
    moveLane(1);
  } else if (ev.key === "Enter") {
    ev.preventDefault();
    activateCursorTarget();
  }
});

// M3 init: render spectator immediately (no flash-of-undefined-state),
// then recover any existing session before deciding whether to auto-load
// a scene — a recovered login may set state.viewer (see
// refreshSessionStatus) when the URL didn't already supply one.
//
// P0 nav (umwelt #238): refreshSessionStatus now ALSO sets state.place for
// a recovered logged-in session (unless a URL ?place= already set it — see
// its own comment above), so this existing `if (state.viewer)` guard
// correctly picks up both viewer AND place for a page-reload recovery
// without further changes here. Traced the one edge case where `place`
// still ends up unset (a spectator with a URL ?viewer= but no ?place= and
// no session): loadScene() itself already guards on `!state.place` and
// shows an inline "place is required" message WITHOUT firing a network
// request — so this guard staying `if (state.viewer)` (not `&&
// state.place`) causes no spurious fetch; it's the same pre-existing
// behavior this case already had before this fix.
//
// umwelt #384 (Lei re-scope): gate on FIRST, synchronously, right
// alongside the spectator-render-immediately call above -- a spectator is
// the default assumption until refreshSessionStatus proves otherwise, so
// the gate must be up before that await even starts (same "no flash"
// reasoning as renderIdentityBar's own placement here). A recovered login
// then tears the gate down before the existing viewer/place auto-load
// logic below runs, unchanged.
renderIdentityBar();
setGateActive(true);
(async () => {
  await refreshSessionStatus();
  if (state.identity === "spectator") {
    return; // stay behind the gate — no scene load, no polling
  }
  setGateActive(false);
  if (state.viewer) {
    loadScene();
  }
})();
