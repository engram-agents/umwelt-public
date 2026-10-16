// Webrender Gadget Charter (docs/web-render/GADGETS.md) — v1 component
// system, JS half. Loaded BEFORE app.js (same flat-script idiom, no
// modules, no build step — index.html's <script> order). Defines the
// `gg*` factories, the street-anchor helper, and the shared outside-
// click/Escape dismiss registry (GADGETS.md §6) — feature diffs touch
// app.js; system diffs touch this file + gadgets.css (the charter's own
// stated review boundary, §6).
//
// Ships with the FIRST gadget class, `window` (ggWindow) — #351's floating
// outcome display (street-anchored) + the migrated console
// (screen-anchored, GADGETS.md §7.3). Per GADGETS.md §2's inventory table,
// only `window` is implemented here; panel/button-row/input-row/badge are
// OUT OF SCOPE for this PR (other consumers claim them later, per the
// charter's incremental "adopt when next touched" migration policy, §7).
//
// Contract (GADGETS.md §1): a gadget factory returns a uniform handle
// `{el, open(), close(), update(data), destroy()}`. Chrome (border/
// background/title bar/close button/`.hidden` handling) lives ONLY in
// gadgets.css's `.gg-*` classes + the `--gg-*` tokens — this file never
// sets border/background/position/z-index inline; a gadget's caller
// supplies only content + options.
//
// Obligation 5, "Observable" (Lei's 2026-07-25 sign-off addition, landed
// after this file's first version): every gadget logs through `ggLog`,
// never bare `console.*`. `ggWindow` wires this in for free (create/open/
// close/update/anchor/target-gone/destroy in debug mode; malformed-opts
// warnings always on) — see `ggLog`/`ggDebug` below.

// ---------------------------------------------------------------------
// Shared dismiss registry (GADGETS.md §6): one document-level
// click/Escape listener backed by a small open-gadget stack, instead of
// each gadget instance wiring its own — needed because #351's outcome
// window and the migrated console can both be open at once, and Escape
// should close only the TOPMOST one. Any object shaped `{el, close()}`
// can register — a gadget doesn't have to be a ggWindow instance built by
// the factory below (see app.js's console-migration note for why the
// console itself registers this way rather than through ggWindow()
// directly).
// ---------------------------------------------------------------------

const _ggOpenStack = [];

function ggMarkGadgetOpen(handle) {
  const idx = _ggOpenStack.indexOf(handle);
  if (idx !== -1) _ggOpenStack.splice(idx, 1);
  _ggOpenStack.push(handle);
}

function ggMarkGadgetClosed(handle) {
  const idx = _ggOpenStack.indexOf(handle);
  if (idx !== -1) _ggOpenStack.splice(idx, 1);
}

// umwelt #945: test the click's DISPATCH-TIME path, not the live tree. A
// click whose own handler swaps the window's content (the proposal viewer's
// action buttons, its Submit/Cancel) detaches its target before the click
// reaches this listener; `contains()` then reads it as an outside click and
// closes the window it happened in. `composedPath()` is fixed at dispatch.
document.addEventListener("click", (ev) => {
  if (!_ggOpenStack.length) return;
  const top = _ggOpenStack[_ggOpenStack.length - 1];
  if (!ev.composedPath().includes(top.el)) {
    top.close();
  }
});

document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape" || !_ggOpenStack.length) return;
  _ggOpenStack[_ggOpenStack.length - 1].close();
});

// ---------------------------------------------------------------------
// Observability (GADGETS.md §1 obligation 5, Lei's 2026-07-25 sign-off
// addition): the ONE shared logging seam every gadget speaks through —
// no bare `console.*` in gadget or feature UI code. Two modes:
//   - regular (always on): warnings/errors only, prefixed
//     `[gg:<class>#<id>]` so a report from a real session names which
//     gadget instance spoke.
//   - debug (opt-in, off by default): full lifecycle — create / open /
//     close / update / anchor / target-gone / destroy, one-line data
//     summary per event. Toggled via `ggDebug(true|false)` from the
//     browser console (persisted in localStorage so it survives a
//     refresh) or `?ggdebug=1` in the URL (session-only, doesn't touch
//     the persisted preference — the phone-debugging path where no
//     console is handy to call ggDebug from).
// Debug mode also mirrors into the in-world #console (app.js's
// consoleLog, if it's loaded) so a phone session — no dev-tools console
// reachable — can still read the lifecycle trail.
// ---------------------------------------------------------------------

let _ggDebugMode = false;
try {
  _ggDebugMode = window.localStorage.getItem("ggDebug") === "1";
} catch (e) {
  // localStorage unavailable (private-browsing lockout, etc.) — default
  // stays off; regular-mode warn/error logging is unaffected either way.
}
if (/[?&]ggdebug=1(&|$)/.test(location.search)) {
  _ggDebugMode = true; // URL override — session-only, not persisted.
}

function ggDebug(enabled) {
  _ggDebugMode = !!enabled;
  try {
    window.localStorage.setItem("ggDebug", _ggDebugMode ? "1" : "0");
  } catch (e) {
    // Same private-browsing case as above — the in-memory flag still
    // takes effect for the rest of this session, it just won't persist.
  }
}
// Exposed on window per spec: "toggled at runtime from the browser
// console via ggDebug(true|false)".
window.ggDebug = ggDebug;

function ggLog(cls, id, level, event, data) {
  const prefix = `[gg:${cls}#${id}]`;
  if (level === "warn" || level === "error") {
    console[level](prefix, event, data !== undefined ? data : "");
    return;
  }
  // level === "debug" — full lifecycle, opt-in only.
  if (!_ggDebugMode) return;
  console.debug(prefix, event, data !== undefined ? data : "");
  if (typeof consoleLog === "function") {
    consoleLog("gg-debug", `${prefix} ${event}`, data);
  }
}

// ---------------------------------------------------------------------
// street(x?, nearEl?) anchor math (GADGETS.md §3): the one shared helper
// that owns re-anchoring a street-child gadget against a target element,
// via the SAME offsetParent-sharing trick app.js's updateCursorPosition()
// already uses/documents — the gadget and its target both share #street
// as their offsetParent, so this stays valid regardless of #street's own
// horizontal scroll position.
// ---------------------------------------------------------------------

function ggAnchorToStreetEl(gadgetEl, targetEl) {
  gadgetEl.style.left = `${targetEl.offsetLeft}px`;
  gadgetEl.style.top = `${targetEl.offsetTop + targetEl.offsetHeight + 6}px`;
}

// Every currently-open ggWindow instance anchored in "street" mode —
// swept by ggReanchorStreetGadgets() (below), which app.js calls
// alongside updateCursorPosition() (the render()-loop's own re-anchor
// point for the player cursor, per GADGETS.md §3: "re-anchoring on street
// re-render is the render loop's job... not per-gadget listeners").
const _ggStreetGadgets = [];

function ggReanchorStreetGadgets() {
  for (const handle of _ggStreetGadgets) {
    handle._ggReanchor();
  }
}

// ---------------------------------------------------------------------
// ggWindow(opts) — GADGETS.md §2's `window` gadget.
//
// opts:
//   title (str, optional)
//   anchor (required): {mode: "street", targetEl} or
//                       {mode: "screen", pos: {top?, right?, bottom?, left?}}
//                       (pos values are numbers -> "<n>px", or any other
//                       CSS length string used verbatim)
//   closable (bool, default true)
//   content (Node | string, optional): a detached DOM node (appended as-is)
//     or a plain string (set via textContent) — never HTML.
//   variant ("mono", optional): pre-wrap + monospace content styling
//     (GADGETS.md §2 — the orrery-chart / ASCII-output case).
//   size (optional): {width?, maxWidth?, maxHeight?} — a per-gadget size
//     override, factory-applied to inline style, same caller-supplied
//     precedent as anchor.pos above (numbers -> "<n>px", any other CSS
//     length string used verbatim). Only overrides what's passed — the CSS
//     class defaults (`.gg-window`'s max-width:420px, `.gg-window-content`'s
//     overflow:auto + max-height:60vh) are unchanged for callers that don't
//     pass size, so small windows stay small. `width`/`maxWidth` apply to
//     the window element itself (the box `.gg-window`'s own max-width
//     rule sizes); `maxHeight` applies to the content element (the box
//     `.gg-window-content`'s own max-height rule sizes, and whose
//     overflow:auto internal scroll a passed maxHeight still governs — see
//     GADGETS.md §3's street/screen amendment, the content-sized-gadget
//     case this option exists for).
//
// Returns {el, open(), close(), update(data), destroy()} — update(data)
// accepts {title?, content?, targetEl?} (targetEl only meaningful for a
// "street"-anchored instance; it lets a caller re-point the SAME window at
// a new target on reuse, e.g. #351's outcome window across successive
// Use actions, without stacking a second window instance).
// ---------------------------------------------------------------------

let _ggIdCounter = 0;

function ggWindow(opts) {
  opts = opts || {};
  const anchor = opts.anchor || {};
  const isStreet = anchor.mode === "street";
  const _id = ++_ggIdCounter;
  const _cls = "window";

  if (anchor.mode !== "street" && anchor.mode !== "screen") {
    // Regular-mode logging (GADGETS.md §1.5): a caller error, not a
    // lifecycle event — always logged, not gated behind debug mode.
    ggLog(_cls, _id, "warn", "unknown anchor mode, defaulting to screen",
          { mode: anchor.mode });
  }

  const el = document.createElement("div");
  el.className = "gg-window gg-hidden " +
    (isStreet ? "gg-window--street" : "gg-window--screen");
  if (opts.variant === "mono") {
    el.classList.add("gg-window--mono");
  }

  const titlebar = document.createElement("div");
  titlebar.className = "gg-window-titlebar";
  const titleEl = document.createElement("span");
  titleEl.className = "gg-window-title";
  titleEl.textContent = opts.title || "";
  titlebar.appendChild(titleEl);

  const closable = opts.closable !== false;
  if (closable) {
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "gg-close";
    closeBtn.textContent = "×"; // ×
    closeBtn.addEventListener("click", () => handle.close());
    titlebar.appendChild(closeBtn);
  }
  el.appendChild(titlebar);

  const contentEl = document.createElement("div");
  contentEl.className = "gg-window-content";
  el.appendChild(contentEl);

  function setContent(content) {
    contentEl.replaceChildren();
    if (content === undefined || content === null) return;
    if (typeof content === "string") {
      contentEl.textContent = content;
    } else if (content instanceof Node) {
      contentEl.appendChild(content);
    } else {
      // Regular-mode logging: a caller passed something that's neither a
      // string nor a DOM node (content contract, GADGETS.md §2) — always
      // logged, not gated behind debug mode, since a silently-blank window
      // is exactly the failure mode worth a loud signal for.
      ggLog(_cls, _id, "warn", "content is neither a string nor a Node, ignoring",
            { typeofContent: typeof content });
    }
  }
  setContent(opts.content);
  ggLog(_cls, _id, "debug", "create",
        { title: opts.title, anchorMode: anchor.mode, variant: opts.variant });

  // Screen-mode position is supplied by the caller (opts.anchor.pos),
  // never computed ad hoc (GADGETS.md §1 contract item 3) — applied once
  // at construction since a screen anchor's corner doesn't move.
  let streetTarget = isStreet ? (anchor.targetEl || null) : null;
  if (!isStreet && anchor.pos) {
    for (const key of Object.keys(anchor.pos)) {
      const v = anchor.pos[key];
      el.style[key] = typeof v === "number" ? `${v}px` : v;
    }
  }

  // opts.size (optional, see docstring above): applied once at construction,
  // same as anchor.pos — a gadget's size doesn't change after open() any
  // more than a screen anchor's corner does.
  if (opts.size) {
    const toLength = (v) => (typeof v === "number" ? `${v}px` : v);
    if (opts.size.width !== undefined) {
      el.style.width = toLength(opts.size.width);
    }
    if (opts.size.maxWidth !== undefined) {
      el.style.maxWidth = toLength(opts.size.maxWidth);
    }
    if (opts.size.maxHeight !== undefined) {
      contentEl.style.maxHeight = toLength(opts.size.maxHeight);
    }
  }

  function reanchorNow() {
    if (!isStreet || !streetTarget) return;
    if (document.body.contains(streetTarget)) {
      ggAnchorToStreetEl(el, streetTarget);
      ggLog(_cls, _id, "debug", "anchor",
            { left: el.style.left, top: el.style.top });
    } else {
      // The thing this window was anchored near is no longer on screen
      // (picked up, moved, scene re-rendered without it) — leaves the
      // window at its last position rather than crashing; logged so this
      // is visible in debug mode instead of silently going stale.
      ggLog(_cls, _id, "debug", "target-gone");
    }
  }

  const handle = {
    el,
    open() {
      const parent = isStreet ? document.getElementById("street") : document.body;
      if (el.parentNode !== parent) parent.appendChild(el);
      el.classList.remove("gg-hidden");
      reanchorNow();
      ggMarkGadgetOpen(handle);
      ggLog(_cls, _id, "debug", "open");
    },
    close() {
      el.classList.add("gg-hidden");
      ggMarkGadgetClosed(handle);
      ggLog(_cls, _id, "debug", "close");
    },
    update(data) {
      data = data || {};
      if (data.title !== undefined) titleEl.textContent = data.title;
      if (data.content !== undefined) setContent(data.content);
      if (isStreet && data.targetEl !== undefined) {
        streetTarget = data.targetEl;
      }
      reanchorNow();
      ggLog(_cls, _id, "debug", "update",
            { title: data.title, hasContent: data.content !== undefined });
    },
    destroy() {
      ggMarkGadgetClosed(handle);
      const idx = _ggStreetGadgets.indexOf(handle);
      if (idx !== -1) _ggStreetGadgets.splice(idx, 1);
      if (el.parentNode) el.parentNode.removeChild(el);
      ggLog(_cls, _id, "debug", "destroy");
    },
    _ggReanchor: reanchorNow,
  };

  if (isStreet) {
    _ggStreetGadgets.push(handle);
  }

  return handle;
}

// ---------------------------------------------------------------------
// ggEditor(opts) — GADGETS.md §2's `editor` gadget (Lei's direct ask: a
// reusable long-text editor with a markdown-insertion toolbar for
// proposal `ask`/`why`, document `body`, the village charge board's
// new-entry `text`, etc.).
//
// Unlike ggWindow, this is an EMBEDDABLE CONTENT widget — a toolbar row
// + a <textarea>, meant to be inserted into a CALLER'S OWN layout (a
// params-form row today; a future document-viewer), not a floating/
// anchored layer with its own open/close semantics. Per GADGETS.md §1
// obligation 1's "plus class-specific members the inventory documents"
// allowance: open()/close() are deliberately ABSENT from this handle —
// there is no floating/anchoring concept here for them to govern, so
// rather than ship no-op stubs that imply a lifecycle this class doesn't
// have, the contract is `{el, getValue(), setValue(v), focus(), destroy()}`.
//
// opts:
//   value (str, optional): initial textarea content.
//   placeholder (str, optional): textarea placeholder.
//   rows (number, default 8): textarea rows.
//   onInput ((value:string) => void, optional): called after any toolbar
//     insertion (mirroring a native `input` event) with the textarea's
//     current value.
//
// Returns {el, getValue(), setValue(v), focus(), destroy()}.
//
// CRITICAL drop-in-compatibility contract: `el.value` is a LIVE ACCESSOR
// (Object.defineProperty get+set) over the textarea's value — this is
// what lets `el` stand in for a plain <input> in the two existing
// N-field-form collection code paths in app.js
// (_collectParamsArgs reads `input.value` directly; openActParamsSubView's
// submit handler reads `input.value.trim()` directly) with ZERO changes
// to that collection code. `el.classList` forwards naturally (a real DOM
// element, so this is automatic — e.g. `_buildParamsFormContent`'s
// `input.classList.add("params-form-input--error")` call still lands on
// this root; see gadgets.css's `.gg-editor.params-form-input--error`
// rule for the visual equivalent, kept gadgets.css-scoped per GADGETS.md
// §1 obligation 2 rather than relying on that feature class's own
// declaration). `el.focus` is likewise overridden to delegate to the
// textarea — not literally read off `.value`/`.classList` by any
// existing call site the way those two are, but the same drop-in spirit:
// app.js's forms focus their first field the same way regardless of
// field type (see this class's PR handoff for the exact call site).
// ---------------------------------------------------------------------

// Selection-math helper shared by all nine toolbar buttons (one
// parameterized helper, not nine bespoke handlers). `mode`:
//   "wrap"        — payload {before, after, placeholder}: wraps the
//                    selection (or `placeholder` if none) in
//                    before+text+after, leaving the inserted text
//                    selected so typing replaces it immediately.
//   "block"       — payload {build(selectedText) -> {text, cursorStart,
//                    cursorEnd}}: replaces the selection with `text`;
//                    cursorStart/cursorEnd are offsets INTO `text`
//                    (relative to the insertion point) for the new
//                    selection range.
//   "line-prefix" — payload {prefixFor(line, i) -> string, singleLine?
//                    bool}: expands to the FULL LINE(S) touched by the
//                    selection (or, if `singleLine` is set, only the
//                    line containing selectionStart — the Heading
//                    button's "not multi-line-selection-aware" case),
//                    prefixes each line via `prefixFor`, and places the
//                    cursor at the end of the modified block.
// umwelt#548: replace the [replaceStart, replaceEnd) range with
// `replacementText` via `document.execCommand("insertText", ...)` on a
// pre-selected range, NOT a direct `textarea.value = ...` assignment.
// Browsers only push an edit onto the native undo stack for
// user-initiated input (typing, paste, execCommand) — a programmatic
// `.value` write is invisible to it, so every toolbar insertion
// permanently broke native Ctrl+Z for the rest of that editor session.
// Lei hit this live on the charge-board editor (#546 test session,
// 2026-08-09); flagged non-blocking by the reviewer-fairy on #544 and
// left unfixed until now. `execCommand` is deprecated but still
// broadly supported and remains the standard workaround for exactly
// this problem; browsers where it's unsupported/no-ops fall back to
// the old direct-assignment behavior (same trade-off this class always
// had pre-#548, not a regression).
function _ggEditorReplaceRange(textarea, replaceStart, replaceEnd, replacementText) {
  textarea.setSelectionRange(replaceStart, replaceEnd);
  let insertedViaNativeUndo = false;
  try {
    insertedViaNativeUndo =
      typeof document.execCommand === "function" &&
      document.execCommand("insertText", false, replacementText);
  } catch (e) {
    insertedViaNativeUndo = false;
  }
  if (!insertedViaNativeUndo) {
    const value = textarea.value;
    textarea.value =
      value.slice(0, replaceStart) + replacementText + value.slice(replaceEnd);
  }
}

function _ggEditorInsert(textarea, mode, payload) {
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const value = textarea.value;
  const selected = value.slice(start, end);

  if (mode === "wrap") {
    const text = selected || payload.placeholder || "";
    const replacement = payload.before + text + payload.after;
    _ggEditorReplaceRange(textarea, start, end, replacement);
    const selStart = start + payload.before.length;
    textarea.setSelectionRange(selStart, selStart + text.length);
  } else if (mode === "block") {
    const built = payload.build(selected);
    _ggEditorReplaceRange(textarea, start, end, built.text);
    textarea.setSelectionRange(start + built.cursorStart, start + built.cursorEnd);
  } else if (mode === "line-prefix") {
    // singleLine (Heading): confine the "touched" range to selectionStart
    // only, so a multi-line selection still only prefixes the one line
    // the caret is in — everything else here is identical to the
    // multi-line-aware buttons.
    const rangeEnd = payload.singleLine ? start : end;
    // Reviewer-fairy-caught bug on PR#544 (verified by reproduction
    // before this fix): at start === 0, `lastIndexOf("\n", start - 1)` is
    // `lastIndexOf("\n", -1)` — JS clamps a negative fromIndex to 0
    // rather than "search nothing," so if value[0] IS "\n" (an empty
    // first line, caret sitting on it), lastIndexOf matches that same
    // character and returns 0, giving lineStart = 1 instead of the
    // correct 0. Silently conflated "no line before this one" (the
    // start === 0 case, always lineStart = 0) with "check whether
    // position 0 itself is a newline" (what an unclamped fromIndex would
    // have asked). Explicit start === 0 special-case, not relying on the
    // clamp's behavior to happen to be right.
    let lineStart = start === 0 ? 0 : value.lastIndexOf("\n", start - 1) + 1;
    let lineEnd = value.indexOf("\n", rangeEnd);
    if (lineEnd === -1) lineEnd = value.length;
    const block = value.slice(lineStart, lineEnd);
    const lines = block.split("\n");
    const newBlock = lines.map((line, i) => payload.prefixFor(line, i)).join("\n");
    _ggEditorReplaceRange(textarea, lineStart, lineEnd, newBlock);
    const newEnd = lineStart + newBlock.length;
    textarea.setSelectionRange(newEnd, newEnd);
  }
}

// The nine toolbar buttons, in the spec's table order. Declarative so the
// button-building loop in ggEditor (below) is the only place that touches
// DOM, and _ggEditorInsert (above) is the only place that touches
// selection math. Glyphs are short, plain-ASCII, and mnemonic of the
// actual markdown syntax each button inserts (a coder-fairy judgment
// call — flagged in this PR's handoff for review) — the `title` MUST
// still name the block per the spec table, since the glyph alone (e.g.
// "B", "##") isn't self-explanatory as a tooltip-free label.
const _GG_EDITOR_TOOLBAR_BUTTONS = [
  { label: "B", title: "Bold", mode: "wrap",
    payload: { before: "**", after: "**", placeholder: "bold text" } },
  { label: "`_`", title: "Inline code", mode: "wrap",
    payload: { before: "`", after: "`", placeholder: "code" } },
  { label: "```", title: "Code block", mode: "block",
    payload: { build: (sel) => {
      const text = "```\n" + sel + "\n```";
      return { text, cursorStart: 4, cursorEnd: 4 + sel.length };
    } } },
  { label: "•", title: "Bullet list", mode: "line-prefix",
    payload: { prefixFor: (line) => `- ${line}` } },
  { label: "1.", title: "Numbered list", mode: "line-prefix",
    payload: { prefixFor: (line, i) => `${i + 1}. ${line}` } },
  { label: "[x]", title: "Checklist", mode: "line-prefix",
    payload: { prefixFor: (line) => `- [ ] ${line}` } },
  { label: ">", title: "Quote", mode: "line-prefix",
    payload: { prefixFor: (line) => `> ${line}` } },
  { label: "##", title: "Heading", mode: "line-prefix",
    payload: { singleLine: true, prefixFor: (line) => `## ${line}` } },
  // umwelt#544 review, left flagged non-blocking at the time ("reasonable
  // people could design this either way"): the original `build` ignored
  // `sel` entirely, so clicking Horizontal rule with text selected
  // DELETED that selection and replaced it with the divider. Preserve it
  // instead — same "don't destroy what the user selected" posture as the
  // other eight buttons (wrap keeps `selected`; code-block's `build`
  // takes `sel`) — by keeping the selection ahead of the divider rather
  // than discarding it.
  { label: "---", title: "Horizontal rule", mode: "block",
    payload: { build: (sel) => {
      const text = (sel ? sel + "\n" : "") + "\n---\n";
      return { text, cursorStart: text.length, cursorEnd: text.length };
    } } },
];

function ggEditor(opts) {
  opts = opts || {};
  const _id = ++_ggIdCounter; // shared counter with ggWindow, per spec.
  const _cls = "editor";

  const el = document.createElement("div");
  el.className = "gg-editor";

  const toolbar = document.createElement("div");
  toolbar.className = "gg-editor-toolbar";
  el.appendChild(toolbar);

  const textarea = document.createElement("textarea");
  textarea.className = "gg-editor-textarea";
  textarea.rows = opts.rows || 8;
  if (opts.placeholder) textarea.placeholder = opts.placeholder;
  if (opts.value !== undefined && opts.value !== null) textarea.value = opts.value;
  el.appendChild(textarea);

  function fireInput() {
    if (typeof opts.onInput === "function") opts.onInput(textarea.value);
  }
  textarea.addEventListener("input", fireInput);

  for (const def of _GG_EDITOR_TOOLBAR_BUTTONS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "gg-btn gg-editor-toolbar-btn";
    btn.textContent = def.label;
    btn.title = def.title;
    btn.addEventListener("click", () => {
      // umwelt#577 (Ariadne, colleague review, verified by real
      // headless-Chromium execution): focus() MUST run before
      // _ggEditorInsert, not after. _ggEditorReplaceRange's
      // execCommand("insertText", ...) acts on whatever element
      // currently holds focus/selection — on a fresh editor's FIRST
      // toolbar click, that's still the BUTTON (this click hasn't
      // moved focus to the textarea yet), so execCommand silently
      // returns false and the code falls back to the old
      // undo-breaking `.value =` write, reproducing #548 on its own
      // primary repro case. Reordering so the textarea holds focus
      // before insertion runs is the fix; a later click (once the
      // textarea already has focus from the prior insert) worked
      // either way, which is why "type then click" masked this.
      textarea.focus();
      _ggEditorInsert(textarea, def.mode, def.payload);
      fireInput();
      ggLog(_cls, _id, "debug", "toolbar-insert", { button: def.title });
    });
    toolbar.appendChild(btn);
  }

  // Drop-in-compatibility contract (see docstring above). A setter is
  // included alongside the getter even though no CURRENT call site writes
  // `.value =` on a params-form input (checked: the only such writes in
  // app.js target unrelated composer inputs) — a getter-only accessor on
  // something that otherwise looks exactly like a plain <input> is a
  // silent-no-op trap (classic scripts run sloppy-mode, so an assignment
  // to a getter-only prop fails quietly, not loudly) waiting for the next
  // "reset this form" feature to hit it. Delegates to setValue so there's
  // one real implementation.
  Object.defineProperty(el, "value", {
    get: () => textarea.value,
    set: (v) => { textarea.value = v; },
  });
  el.focus = () => textarea.focus();

  ggLog(_cls, _id, "debug", "create", { rows: textarea.rows });

  return {
    el,
    getValue() {
      return textarea.value;
    },
    setValue(v) {
      textarea.value = v;
    },
    focus() {
      textarea.focus();
    },
    destroy() {
      if (el.parentNode) el.parentNode.removeChild(el);
      ggLog(_cls, _id, "debug", "destroy");
    },
  };
}

// ---------------------------------------------------------------------
// `status` gadget (umwelt #957; GADGETS.md §2 inventory row + "Action
// outcome" section): the one-line outcome readout next to the action bar.
// Anchor mode `attached` (§3): a DOM child of `parentEl` (directly after
// `afterEl` when given), positioned by the normal flow -- no position:fixed, no numeric z-index. Chrome lives in
// gadgets.css (.gg-status / .gg-status-<kind>).
//
// Handle: { el, show(kind, text), clear(), destroy() }. open()/close()/
// update() are deliberately absent (embeddable content class with no
// open/close lifecycle; see §1 obligation 1's amendment and the inventory
// table's Notes cell). `kind` is "pending" | "ok" | "error"; `text` is
// written with textContent ONLY (server reason strings are untrusted).
// "ok"/"error" auto-clear after ~6 s and a newer show() cancels the older
// timer; "pending" never auto-clears (the caller's ok/error replaces it).
// ---------------------------------------------------------------------

const _GG_STATUS_CLEAR_MS = 6000;
const _GG_STATUS_KINDS = ["pending", "ok", "error"];

function ggStatus(opts) {
  opts = opts || {};
  const _id = ++_ggIdCounter;
  const _cls = "status";
  if (opts.anchor !== undefined && opts.anchor !== "attached") {
    ggLog(_cls, _id, "warn", "unsupported-anchor", { anchor: opts.anchor });
  }

  const el = document.createElement("div");
  if (opts.id) el.id = opts.id;
  el.className = "gg-status";
  el.setAttribute("role", "status");
  el.setAttribute("aria-live", "polite");
  let timer = null;

  function cancelTimer() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }
  function clear() {
    cancelTimer();
    el.className = "gg-status";
    el.textContent = "";
    ggLog(_cls, _id, "debug", "clear");
  }
  function show(kind, text) {
    if (!_GG_STATUS_KINDS.includes(kind)) {
      ggLog(_cls, _id, "warn", "unknown-kind", { kind });
      return;
    }
    cancelTimer();
    el.className = `gg-status gg-status-${kind}`;
    el.textContent = text;
    if (kind !== "pending") timer = setTimeout(clear, _GG_STATUS_CLEAR_MS);
    ggLog(_cls, _id, "debug", "show", { kind, len: String(text).length });
  }

  // `afterEl` (optional, a child of parentEl) places the line directly after
  // that sibling -- e.g. right after the action bar -- else it is appended.
  if (opts.parentEl) {
    if (opts.afterEl && opts.afterEl.parentNode === opts.parentEl) {
      opts.parentEl.insertBefore(el, opts.afterEl.nextSibling);
    } else {
      opts.parentEl.appendChild(el);
    }
  }
  ggLog(_cls, _id, "debug", "create");

  return {
    el,
    show,
    clear,
    destroy() {
      cancelTimer();
      if (el.parentNode) el.parentNode.removeChild(el);
      ggLog(_cls, _id, "debug", "destroy");
    },
  };
}

// ---------------------------------------------------------------------
// `badge` gadget (umwelt #967; GADGETS.md §2 inventory row `badge`, which
// the charter registered in v1 but no code had built until now): a short
// inline text label -- first consumer is the "retracted" / "approved"
// mark on a tray row. Anchor mode `attached` (§3): a DOM child of
// `parentEl`, laid out by the normal flow; chrome lives in gadgets.css
// (.gg-badge / .gg-badge-<kind>).
//
// Handle: { el, update({kind, text}), destroy() }. open()/close() are
// deliberately absent (embeddable content class, no open/close lifecycle;
// §1 obligation 1's amendment and the inventory Notes cell). `kind` is
// "info" | "closed"; `text` is written with textContent ONLY (a proposal's
// status string is world data). The face badge / emotion bubble / held-
// marker named in the charter's seed list stay as they are until next
// touched (§7.2) -- this factory does not migrate them.
// ---------------------------------------------------------------------

const _GG_BADGE_KINDS = ["info", "closed"];

function ggBadge(opts) {
  opts = opts || {};
  const _id = ++_ggIdCounter;
  const _cls = "badge";
  if (opts.anchor !== undefined && opts.anchor !== "attached") {
    ggLog(_cls, _id, "warn", "unsupported-anchor", { anchor: opts.anchor });
  }

  const el = document.createElement("span");
  function update(data) {
    data = data || {};
    const kind = _GG_BADGE_KINDS.includes(data.kind) ? data.kind : "info";
    if (data.kind !== undefined && kind !== data.kind) {
      ggLog(_cls, _id, "warn", "unknown-kind", { kind: data.kind });
    }
    el.className = `gg-badge gg-badge-${kind}`;
    el.textContent = data.text == null ? "" : String(data.text);
    ggLog(_cls, _id, "debug", "update", { kind, len: el.textContent.length });
  }
  update(opts);

  if (opts.parentEl) opts.parentEl.appendChild(el);
  ggLog(_cls, _id, "debug", "create");

  return {
    el,
    update,
    destroy() {
      if (el.parentNode) el.parentNode.removeChild(el);
      ggLog(_cls, _id, "debug", "destroy");
    },
  };
}

// ---------------------------------------------------------------------
// `chip` gadget (umwelt #936; GADGETS.md §2 inventory row `chip`): the
// persistent "what is selected" bar. Anchor mode `attached` (§3): a DOM
// child of `parentEl`, laid out by the normal flow -- it docks INSIDE the
// composer, so it can never overlap it, and it inherits whatever the
// composer's own stacking does. Chrome lives in gadgets.css (.gg-chip*).
//
// The chip is dumb: it renders what `update(data)` hands it and reports
// user intent through three callbacks. It owns no selection state (the
// feature's `selectThing` is the single writer) and fetches nothing.
//
//   opts: { parentEl?, id?, onOpen(), onClear(), onSelectById(id) }
//   update({ name, kind, id, where, notice, noticeKind })
//     - no `id` => the empty state: only the "select by id" entry shows
//       (plus `notice`, e.g. the server's refusal of a pasted id)
//     - `notice` is the SERVER's own message text, shown verbatim;
//       `noticeKind` is "error" (default) or "info"
//   handle: { el, update(data), destroy() }; open()/close() are
//   deliberately absent (embeddable content class, §1 obligation 1's
//   amendment and the inventory Notes cell).
//
// EVERY world-derived string (name, kind, id, where, notice) is written with
// textContent ONLY -- never innerHTML (DESIGN.md §12.10: names and
// descriptions are attacker-influenceable public text).
// ---------------------------------------------------------------------

const _GG_CHIP_ID_SHORT = 8;

function ggChip(opts) {
  opts = opts || {};
  const _id = ++_ggIdCounter;
  const _cls = "chip";
  const onOpen = typeof opts.onOpen === "function" ? opts.onOpen : () => {};
  const onClear = typeof opts.onClear === "function" ? opts.onClear : () => {};
  const onSelectById = typeof opts.onSelectById === "function"
    ? opts.onSelectById : () => {};

  const el = document.createElement("div");
  if (opts.id) el.id = opts.id;
  el.className = "gg-chip gg-chip-empty";
  el.setAttribute("role", "group");
  el.setAttribute("aria-label", "selection");

  // The selection summary (hidden in the empty state). The main button is
  // the click target that opens the interact menu for the selection.
  const sel = document.createElement("span");
  sel.className = "gg-chip-selection";
  const main = document.createElement("button");
  main.type = "button";
  main.className = "gg-chip-main";
  main.title = "open actions for the selected thing";
  const nameEl = document.createElement("span");
  nameEl.className = "gg-chip-name";
  const kindEl = document.createElement("span");
  kindEl.className = "gg-chip-kind";
  main.appendChild(nameEl);
  main.appendChild(kindEl);
  const idEl = document.createElement("span");
  idEl.className = "gg-chip-id";
  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "gg-chip-copy";
  copyBtn.textContent = "copy";
  copyBtn.title = "copy the full thing id";
  const whereEl = document.createElement("span");
  whereEl.className = "gg-chip-where";
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "gg-chip-clear";
  clearBtn.textContent = "✕";
  clearBtn.setAttribute("aria-label", "clear selection");
  clearBtn.title = "clear the selection";
  sel.appendChild(main);
  sel.appendChild(idEl);
  sel.appendChild(copyBtn);
  sel.appendChild(whereEl);
  sel.appendChild(clearBtn);
  el.appendChild(sel);

  // The server's own message (a refusal of a pasted/selected id).
  const noticeEl = document.createElement("span");
  noticeEl.className = "gg-chip-notice";
  noticeEl.setAttribute("role", "status");
  noticeEl.setAttribute("aria-live", "polite");
  el.appendChild(noticeEl);

  // "select by id": exact parity with an agent passing a uuid.
  const form = document.createElement("form");
  form.className = "gg-chip-byid";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "gg-chip-input";
  input.placeholder = "select by id";
  input.setAttribute("aria-label", "select a thing by id");
  input.autocomplete = "off";
  input.spellcheck = false;
  const go = document.createElement("button");
  go.type = "submit";
  go.className = "gg-chip-go";
  go.textContent = "select";
  form.appendChild(input);
  form.appendChild(go);
  el.appendChild(form);

  let currentId = "";

  // stopPropagation on every click: the chip's buttons open / swap
  // surfaces whose document-level outside-click dismiss would otherwise see
  // this same click and close what it just opened (the #937 class).
  main.addEventListener("click", (ev) => {
    ev.stopPropagation();
    onOpen();
  });
  clearBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    onClear();
  });
  copyBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (!currentId) return;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(currentId).then(
          () => { copyBtn.textContent = "copied"; },
          () => { copyBtn.textContent = "copy failed"; });
      } else {
        copyBtn.textContent = "copy unavailable";
      }
    } catch (e) {
      copyBtn.textContent = "copy failed";
    }
    setTimeout(() => { copyBtn.textContent = "copy"; }, 1500);
  });
  form.addEventListener("click", (ev) => ev.stopPropagation());
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const v = input.value.trim();
    if (!v) return;
    input.value = "";
    onSelectById(v);
  });

  function update(data) {
    data = data || {};
    currentId = data.id ? String(data.id) : "";
    const has = !!currentId;
    el.className = has ? "gg-chip" : "gg-chip gg-chip-empty";
    sel.hidden = !has;
    if (has) {
      nameEl.textContent = data.name == null || data.name === ""
        ? currentId : String(data.name);
      kindEl.textContent = data.kind ? String(data.kind) : "";
      idEl.textContent = currentId.slice(0, _GG_CHIP_ID_SHORT);
      idEl.title = currentId;
      whereEl.textContent = data.where ? String(data.where) : "";
    } else {
      nameEl.textContent = "";
      kindEl.textContent = "";
      idEl.textContent = "";
      idEl.title = "";
      whereEl.textContent = "";
    }
    const note = data.notice == null ? "" : String(data.notice);
    noticeEl.textContent = note;
    noticeEl.hidden = note === "";
    noticeEl.className = "gg-chip-notice" +
      (note && data.noticeKind === "info" ? " gg-chip-notice-info" : "");
    ggLog(_cls, _id, "debug", "update", { has, notice: note !== "" });
  }
  update({});

  if (opts.parentEl) opts.parentEl.appendChild(el);
  ggLog(_cls, _id, "debug", "create");

  return {
    el,
    update,
    destroy() {
      if (el.parentNode) el.parentNode.removeChild(el);
      ggLog(_cls, _id, "debug", "destroy");
    },
  };
}
