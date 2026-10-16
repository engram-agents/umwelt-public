# Charge board editor — design of record

**Charge:** Lei, 2026-08-18, direct. *"the charge board. It's still awkward for me to edit the
charge board, the entries are not the board file, so I don't have a easy way to see all of them
and edit. A charge board editor is needed for me to write on it, please design it and build it
with Zephyr."*

**Pair:** Ariadne (design) + Zephyr (build), per Lei. **Status:** design, not started —
**edit semantics ruled by Lei 2026-08-18** (see that section); this revision folds the ruling,
its edge cases, and the capacity finding (#632) into the shape Zephyr builds against.

---

## Decision in one line

**The board IS its entries.** Build one surface that renders them as a visible, editable list —
active newest-first, with an honest count of what is hidden — where each line carries its own
controls. The read half of that surface is exactly what #610 already rules, so **#610 and this
are one piece of work, not two.**

## The complaint is precise and it is not about ergonomics

> *"the entries are not the board file"*

Read literally, that is a **mental-model** complaint, and it is correct. The charge board is
today **two artifacts wearing one name**:

| | `board.md` | the entries |
|---|---|---|
| read | whole, at once | only via a 96KB dump |
| write | edit the file | `add-entry` only, append-only |
| remove | edit the file | `retire-entry`, by an id you cannot see |
| what it holds | a **superseded 2026-08-09 snapshot** plus pointers | **the live charge** |

Lei learned one model and the other half does not obey it. **An editor that puts a nicer input
box on the entry log leaves the split exactly where it is** — that is the failure mode this
design exists to avoid, and it is the tempting build because it is the small one.

## Measured, not assumed

Everything below was measured on 2026-08-18 against the deployed world.

**The read is broken at scale — and it is browser-side too, not just tooling.** `look_at` on the
Office board returns **~100K characters and rising** (measured twice on 2026-08-18: 96,788 chars /
60 entries in the morning, 115,965 / 61 entries a few hours later — it grows with use), and it
overflows the tool-result cap.

> **⚠️ WHERE THAT WEIGHT ACTUALLY SITS — and it is NOT where an earlier version of this doc, or my
> own #610 ruling, aimed.** Of the 115,965-char response, **100,391 chars (86.6%) is a raw
> `metadata` JSON dump** and only **14,356** is the purpose-built `charge_board_render`.
> `mcp_server.py`'s `look_at` emits `metadata: {json.dumps(...)}` **unconditionally for any thing
> with non-empty metadata** — it is kind-agnostic and does not know what a charge board is. So
> **filtering the charge-board render moves ~7K of a ~116K payload, about 6%**, and the overflow
> survives. Filed separately as **#629**, because it belongs to `look_at` and every kind has it —
> a project board with a long task list hits the identical wall today. **This design does not fix
> the opening number; #629 does.** Stated plainly because the earlier draft implied otherwise. Finding *today's* charge required spilling it to a file and parsing
it with a script. Zephyr traced the browser path: the same unbounded blob lands in
`#inspect-charge-board`, so this is what Lei sees in the inspect panel. **A charge surface that
needs a script to read has stopped being a charge surface.**

**Striking a line is not possible through the rendered UI.** The four typed actions do surface as
real buttons. But `retire-entry` requires an `entry_id`, and **entry ids are never rendered** —
they live in metadata. So the sub-view offers a bare text box labelled *Entry Id* for a value
nothing on screen tells you. This is not "awkward"; the prerequisite is missing.

*(Precision, from review: the ids ARE in the network payload — the gateway propagates `look_at`'s
response as-is, so devtools would reveal them. "Not achievable through the rendered UI" is the
defensible claim, and it is the one that matters: no one editing their own charge board should
need a Network tab.)*

**The write path refuses success-shaped.** `_BOARD_ENTRY_TEXT_MAX_LEN = 500`. There is no
character counter, so the limit is discovered by failing. I hit it twice; both times the call
returned `status=None` — **byte-identical to the successful call** — with the rejection only in a
separate `effect_failures` list. In the browser the outcome window does surface that list, but the
console line prints `-> ?`, because a board action has no state transition. **Ambiguous, not
refused.**

## What a reviewer of his own charge board actually needs

Numbers to design against, agreed with Zephyr. *"Convenient" is not a spec; an action count is.*

| task | today | designed |
|---|---|---|
| add a charge | 4 actions, if under 500 chars | **3** — open, type (with live counter), Save |
| strike a stale line | **not achievable** — id invisible | **2** — pick the visible line's control, confirm |
| strike one + add tomorrow's | not achievable | **~5, with ZERO id-hunting steps** |
| **fix a typo in a live line** | **not achievable** — no edit path exists | **3** — pencil, cursor into the prefilled text, Save |

**Eliminating the go-find-the-id step *is* the specification.** If a build satisfies every other
line here and still requires reading raw data to remove an entry, it has not done the job.

## Shape

One sub-view on the board thing, replacing the inspect dump:

- **The list.** Active entries, **newest first** — today's charge is the first thing on screen,
  not the last thing after 60 entries. Each row: text, author, timestamp, and its **own retire
  control** and **pencil** (see *Edit semantics* below). The row is the handle; nothing is typed
  to identify it.
- **The honest count.** `+ 32 retired entries — show all` whenever any are hidden. A filtered view
  that does not say it is filtered is worse than the bloated one, because it reads as complete.
- **The add row**, in place at the top, with a **live character counter against 500**.
- **`board.md` demoted to a masthead.** It should describe the board and carry standing rules —
  never a copy of any entry, and never a superseded snapshot of the charge. That is what resolves
  the split at the root instead of papering over it. *(Correction to an earlier draft: I wrote that
  the board's body was "already marked SUPERSEDED in place." That is true of `board.md`, the FILE —
  not of the thing's `description`, which is simply unedited 2026-08-09 text carrying that day's
  rulings. I conflated the two surfaces in the document whose central argument is that those two
  surfaces get conflated. The justification is staleness, and it stands without the overclaim.)*

## Edit semantics — Lei's ruling, 2026-08-18

I put "what does *edit* mean here" to Lei as a gate, because the answer decides whether this
needs a new engine action. His answer **splits the question** rather than picking a side, and the
split is the right one:

> *"For the UI, I'd want a real edit-in-place experience, that is, when I click edit, e.g. the
> pencil icon next to an entry, I'll be given a editor PREFILLED with the existing content, and I
> can just move my cursor inside it, and replace part of it with new text, then when I click Save,
> it get published to the board, REPLACING the previous content. Now, in terms of the underlying
> data, I'm ok with 'retiring and adding' form, the old content isn't silently changed, but
> retired with the trace, and my edited version is added as a new entry, it would be even nicer if
> the two entries have a 'supersede' relation preserved somehow."*

**What this costs to build, stated precisely — because "no engine change" would be the
convenient overclaim here and it is false.** The *mutation* is `board_entry_retire` +
`board_entry_add`, both of which already exist and ship, so **no new verb and no new authority**:
this stays out of the design-substantial lane, and the routing below is unchanged. But the
**supersede link is not free** — `_apply_effect_board_entry_add` writes a fixed five-key entry
(`id`, `text`, `created_by`, `created_at`, `retired_at`) with nowhere to put a `supersedes`
pointer or an original-author field, so the effect has to learn them. That is a **small additive
engine change to an existing effect**, which is a different thing from both "client-only" and
"new capability". The genuinely client-side half is the prefill affordance.

**What it changes in this document:** the shape above specified an add-row plus a per-row retire
control. That is not what Lei asked for. A per-row **pencil** that prefills is a different
affordance, and the versioning non-goal below is reversed by the supersede requirement.

### Edge cases, ranked by damage

**1 — Authorship moves silently. This is the one to design first.** Read from source, not
inferred: `_apply_effect_board_entry_add` stamps `"created_by": actor` — the *caller*. So when
Lei edits an entry **Borges** wrote, retire-and-add mints a new entry authored by Lei. The board
then either **reattributes Borges's words to Lei**, or — if the client compensates by carrying
the old author forward — publishes **Lei's words under Borges's name**. Both are wrong, and both
are silent. The supersede link must carry **original author and editor as distinct fields**; the
row must render both ("borges, edited by lei"). *(Verified live: today's 61 entries carry exactly
two authors, `ariadne` (32) and `borges` (29) — cross-author editing is the normal case here, not
a corner.)*

**2 — Concurrent edit is a lost update.** Two editors open the pencil on the same entry, both
prefill from the same text, both Save. The second retire is an **idempotent no-op success** by
`_apply_effect_board_entry_retire`'s documented posture, so nothing fails — and the board ends up
with two live successors and no winner. Save must carry the `entry_id` it was prefilled from and
be refused if that entry is no longer live, with the refusal shown **at the pencil**, not in a
list.

**3 — Editing drives a hard capacity wall, and the wall is closer than anyone thinks.** Filed as
**#632** during this update. `_BOARD_ENTRIES_MAX_COUNT = 100`, **retired entries count toward it**,
and retiring never frees a slot. The office board is at **61/100 (28 live, 33 retired)** and has
taken ~6.8 entries/calendar-day over its 9-day life. **Under retire-and-add, one edit consumes one
slot permanently** — identical to a new entry. At the cap, `board_entry_add` fails
**success-shaped** (`status=None`, `-> ?`), so the day's charge would silently not land. Editing
does not cause this, but it is the fastest accelerant, and shipping the editor onto a board at
61/100 is shipping into it. **#632 should land before or with the editor, not after.**

> **UPDATE (this PR): #632 has landed.** The cap now counts only ACTIVE entries —
> retired entries no longer count against `_BOARD_ENTRIES_MAX_COUNT` and retiring
> DOES free a slot. The paragraph above describes the pre-fix behavior; left in
> place as the record of why the fix was urgent, not as current fact.

**4 — The 500-char counter meets a prefilled box.** The counter must count from the **prefilled
length**, not from zero: an entry already near `_BOARD_ENTRY_TEXT_MAX_LEN` must not present as
having 500 characters free. And a length refusal must **not clear the buffer** — losing an edit
just typed is materially worse than losing an empty add, which is the case the original counter
requirement was written for.

Two smaller ones, spec lines rather than design questions:

- **Editing a retired entry is refused.** Otherwise the pencil either resurrects a struck line or
  forks a dead branch. Retired rows show no pencil.
- **A no-op Save is skipped, not minted.** Saving unchanged text should do nothing rather than
  burn a capacity slot (see #632) and add a supersede link between two identical entries.

### Read-side consequence

The supersede link is only worth storing if something renders it. Minimum: an edited row shows
**"edited"** with the prior text reachable — not a diff view, just the trace Lei asked for. This
lands on the same render #610 governs, which is another reason the read half and this are one
piece of work.

## The one requirement I would not ship without

**Saved-vs-refused must be loud, in plain words, at the control where Lei just typed.**

Not in a separate `effect_failures` list the surface has to remember to render. Not `-> ?`. The
editor says **"saved"** or **"refused: over the 500-character limit"**, in the same place the text
was entered. This is Zephyr's requirement and I am adopting it verbatim, because the defect it
guards against is the one that bit me twice today and is the day's dominant failure family: a
refusal whose output is indistinguishable from success.

## Deliberate non-goals

- ~~**No edit-history or versioning.**~~ **REVERSED by Lei, 2026-08-18** — see *Edit semantics*
  above. Retire-and-re-add remains the data model, but the supersede link between the two entries
  is now a requirement, so the edit history is exactly what the board must be able to show.
- **No rich text.** Plain text, one field.
- **No permissions work.** Who may add is a live question (`borges` succeeds, `kepler` 403s, the
  grant is not enumerated anywhere) — real, and **not this**. Filed separately if it bites.

## Build order

1. **The read half — #610 AND #629, and they are not the same size.** #610 (active newest-first +
   honest count) makes the *rendered* view legible and is the right default — but it is ~6% of the
   payload. **#629 — bounding `look_at`'s unconditional raw-metadata dump — is the one that fixes
   the read cost**, and it is generic engine work rather than board work. Do not build #610 alone
   and report the read as fixed.
   *(Ownership, stated precisely because an earlier draft overstated it: Zephyr accepted #610
   in-world on 2026-08-18, before the #629 measurement existed; the issue tracker shows no
   assignee. After that measurement I asked him to HOLD — writes are Lei's stated priority as of
   14:36Z, and this is explicitly the gap-filler lane.)*
2. **#632 — the capacity wall**, before or with the editor rather than after. It is the one item
   here whose deadline is set by usage rather than by us: the board is at 61/100 and every add
   *and every edit* consumes a permanent slot.
3. **Per-row retire control.** The single highest-value action: it converts an impossible task into
   a two-tap one.
4. **The pencil — edit-in-place with a supersede link** (per *Edit semantics* above). Ordered
   after retire because it reuses that row control's plumbing, and because its authorship and
   compare-and-swap questions want the row model settled first.
5. **The add row with a live counter, and loud saved/refused.**
6. **`board.md` demoted to a masthead.**

## Verification

**Acceptance is Lei clicking**, on the **test** tier — `:5013` now serves a managed alpha
webrender, and an agent can drive it end to end as a human-kind test identity. So unlike previous
board work, **we can verify this ourselves before asking for his eyes**, and should.

**Honest bound on this document:** I did **not** click through a browser to write it. The browser
findings are Zephyr's — he traced the render path and then ran a live `thing_actions` check
against the deployed world to corroborate it. I attempted to stand up a test board on alpha to see
it myself, could not set the kind through `create_thing`'s spec, and stopped rather than widening
the detour. **Before build starts, someone should look at the real thing in a browser**, because
every measurement above except that one is mine and this design is the poorer for the gap.

## Routing

Render + sub-view work over existing typed actions: **ordinary round** (reviewer-fairy →
colleague → merge), no security lane. If implementation finds itself changing who may act on the
board, that is the permissions non-goal above and it stops for a separate decision.
