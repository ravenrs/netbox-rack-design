# Editor — known issues & debugging notes

## PARTIAL (task #34) — homecoming validated the ORIGIN slot but commits at the DROP slot → occupied drop in origin rack overlaps

**Reported:** 2026-07-16. A device moved out to another rack, dragged back to
its ORIGIN rack onto an OCCUPIED slot, committed an overlap
(`I1 sg2-sl-a36(body) overlaps sg2-sl-b15(body)`; b15 left `state:"existing"` on
top of a36).

**Root cause:** `homecomingAdopt` REVIVES the origin entry at the DROP position
but never repositions the tile. The task-#33 guard validated the ORIGIN slot
(`uPositionToGsY(ost.widget.u_position)`), which is FREE (the device left it) —
so an occupied DROP elsewhere passed the guard and committed an overlap.

**Fix applied:** `homecomingAdopt` now validates the slot WHERE THE TILE LANDED
(`rdCanPlaceAt(el, rackId, face, node.y, …)`), not the origin slot. An occupied
drop DECLINES the homecoming → falls through to adopt → `maybePromptMove` →
`rejectDrop` (returns the device to its last position). Legal drops (free slot,
at origin or elsewhere) still revive the origin entry as before.

**UNRESOLVED / caveat (be honest):** this could NOT be proven test-first. The
e2e harness (`__rdX.moveTo`) drives a cross-rack move via BOTH `makeWidget`
(→ `added` → homecomingAdopt) AND `fireDropped` (→ `dropped` →
`maybePromptMove`), so `maybePromptMove` always runs as a backstop and reverts
the overlap regardless of the homecomingAdopt decision — the #34 test passes on
the buggy code too. The LIVE bug is a real mouse drag (only `added` fires) where
`maybePromptMove` did NOT run (empty `__rdMoveLog`) — which the harness cannot
reproduce. WHY maybePromptMove doesn't run on the real added-only path is not
yet root-caused. The robust fix is likely a validate-before-commit gate IN the
`added` handler (reject to source without relying on maybePromptMove), verified
with a REAL drag (chrome-devtools) or an added-only harness primitive. The
applied `homecomingAdopt` fix is defensive/correct-by-analysis and does not
regress the suite, but the real-drag path is UNVERIFIED. Test:
`EditorCrossRackSweepTestCase::test_move_back_to_origin_rack_onto_occupied_nonorigin_slot_never_overlaps`
(end-to-end no-overlap guard; see its CAVEAT docstring).



## FIXED (task #33) — cross-rack reject reverts to TRUE ORIGIN, not the source rack → homecoming-into-occupied overlaps

**Reported:** 2026-07-15. A device moved A→B, then dragged back to its **origin
rack A** onto its now-**occupied** old slot, commits an overlap
(`I1 a75(body) overlaps b15(body)`) instead of being rejected.

**Analysis (systematic):** this is the same root principle as the same-rack
reject bug (#32) — *revert to "origin" instead of "where the drag began"* — but
across racks. `homecomingAdopt` now declines when the origin slot is occupied
(a `canPlaceAt` gate was added), so it falls to a normal cross-rack adoption
that then rejects; but the cross-rack reject path (`cancelMove`'s `crossRack`
branch) restores the device to its **true origin** (A U6), which is occupied →
overlap persists. The device should instead return to **the rack it came from**
(its source + pre-drag slot), never its true origin.

**Fix direction:** thread the pre-drag position (rack + slot) through
`tileInFlight` → `adoptForeignTile`, and add a distinct cross-rack **reject**
path (separate from the ×/dialog **cancel**, which correctly goes to the true
origin) that re-homes the tile into its source rack. Sizeable change to the
adoption machinery — do test-first.

**Unified design (implemented):** the reject rule is one principle on every
path — *capture the device's LAST POSITION (source rack + slot + face) at
drag-start; an illegal drop restores it exactly there.* Same-rack / cross-face
already snap to `st.preDragGsY`/`preDragFace`. For cross-rack:
1. `onDragStart` threads `preDragGsY`/`preDragFace` into `tileInFlight`;
   `adoptForeignTile` stores them on the adopted entry as `srcRackId` /
   `srcPreDragGsY` / `srcPreDragFace`.
2. `homecomingAdopt` now DECLINES (canPlaceAt) when the origin slot is occupied,
   so the drop falls through to a normal adoption + reject rather than
   committing an overlap at the origin.
3. `rejectDrop` gains a MULTI-HOP branch: when `srcRackId !== originRackId`
   (the device was dragged from a rack that is NOT its true origin), it calls
   the source controller's new `reclaimFromReject(el, face, gsY, info)`, which
   re-homes the tile into the source rack at its pre-drag slot as a move_in.
   A FRESH cross-rack move (`srcRackId === originRackId`) keeps the existing
   `cancelMove` path, so the cross-rack sweep / foreign-drop-onto-shadow tests
   are unaffected (their last-position IS the true origin).
4. Shadow ownership stays single: `reclaimFromReject` RETIRES any stale entry in
   the source rack for the same device (destroying its owned shadow + temp
   ghost) before re-homing, so no duplicate opposite-face hatch (I1) forms; the
   re-home runs under `refreshing` so its synchronous `added` event is skipped.
The ×/dialog CANCEL is untouched — it still reverts the whole move to the true
origin.

**Regression test (test-first, fail→pass):**
`EditorCrossRackSweepTestCase::test_homecoming_into_occupied_origin_never_overlaps`
— move A→B, occupy A's old slot, drag back to A: asserts no I1 overlap. Failed
(as `@expectedFailure`) before the fix; passes now.

---


Live-found bugs in the GridStack design editor, their root cause, and status.
This is the working record for the editor's move / displacement / remove flows
(see also `editor-behavior-spec.md` and `editor-conformance-matrix.md`).

## How to debug the editor read-model live

The editor ships a read-model + invariant checker, off by default:

```js
window.__rdDebugInvariants = true;         // re-derive + console.warn on every gesture
window.__rdModel.build();                  // structured snapshot (arrays: devices, racks, …)
window.__rdModel.check();                   // [] when consistent; else I1/I2/I4 strings
```

Each `model.devices[]` entry carries `deviceId, label, state, face, y, rows,
isFullDepth, rackId, shadow`. `state` derives from the tile's
`nbx-rd-state-*` class. The `I4` invariant means "a device has >1 LIVE body"
(a `move_out_ghost`/`remove` is NOT live, so a normal move's body+origin-ghost
pair is fine).

Convert a GridStack row to a U (front face): `uTop = uHeight - y/2 - rows/2 + 1`
(2 GridStack rows per U; `uHeight` is the rack's data-u-height).

---

## FIXED — re-entrant revert cascade promotes a neighbor's ghost to a live body

**Reported:** 2026-07-15 (Power Demo design 581 / rack 318).

**Symptom:** remove two existing devices, then move `sg2-sl-b15` onto a freed
area, and an *unrelated* reloaded cross-rack move (`sg2-sl-a23`, device 14110)
"jumps by itself" to the wrong unit. `__rdModel.check()` reports, 8×:
`I4 device 14110 (sg2-sl-a23) has 2 live entities: rack 321 units/front, rack 321
units/rear` — the device's rear origin **ghost** got promoted into a live
`existing` body while its front `move_in` tile stayed.

**Root cause (from the console stack traces):**

```
onPaletteDrop → maybePromptMove → cancelMove          (b15's drop rejected → revert)
  → restoreFromGhost → homeInto → GridStack makeWidget
      → fires "added" event synchronously
          → maybePromptMove (RE-ENTERS) → cancelMove → restoreFromGhost → homeInto → …
```

`homeInto` (used by `restoreTile` / `restoreFromGhost` / homecoming) calls
GridStack `makeWidget`, which fires the `added` event **synchronously**. The
per-rack `added` handler guarded only on `recomputing` — not on `refreshing`
(the re-entrancy flag set around every `homeInto`). Because `tileInFlight` was
still set from the original drag, the handler mistook our own programmatic
re-home for a fresh user cross-rack adoption and re-entered the
adopt → prompt → cancel → restore pipeline, corrupting the neighboring reloaded
move.

**Fix:** add `if (refreshing) { return; }` to the `added` handler, right next to
the existing `recomputing` guard — a programmatic re-home is not a user drop.
(`editor.js`, `grid.on("added", …)`.)

**Verified:** live repro no longer trips `I4`; `a23` stays put.

**TODO:** lock with a regression test (a reloaded cross-rack `move_in` whose
revert re-homes into a rack holding another reloaded `move_in`; assert
`__rdModel.check()` stays `[]`). Extend the `__rdDisplace` sweep harness.

---

## FIXED — cannot drop onto a slot freed by a remove; tile snaps back

**Reported:** 2026-07-15 (same session).

**Symptom:** flag device(s) for removal, then drag another device onto the space
they occupied. Instead of landing there, the dragged tile snaps back to its
origin (observed: b15 aimed at the freed U36–U37, always returned to U28).

**Root cause (the real one — an earlier "physical GridStack cell" theory was
wrong):** `flagRemove()` toggles `nbx-rd-state-remove` ON **without** removing
the tile's base `nbx-rd-state-existing` class, so a removed existing device's
tile carries BOTH (`[existing, remove]`). The read-model's
`rdStateFromClassList()` used a first-match regex over the className, which
returned `existing`. So the read-model classified a remove-flagged device as a
LIVE body (`RD_LIVE_STATES.existing`), and `rdCanPlaceAt` treated it as a
BLOCKER — rejecting any drop onto the being-vacated slot, which then snapped
back. Worst for full-depth gear, whose OPPOSITE face is validated too (the
mounted rear primary tile carried `[existing, remove]`; the derived front hatch
was rebuilt correctly with only `remove`).

Confirmed via `__rdModel`: with two full-depth `dccore` devices remove-flagged,
`__rdModel.build()` reported their state as `existing`, and
`__rdModel.canPlaceAt(b15, 321, 'front', 26, 4, true)` returned
`ok:false, blockers:[…dccore…]`.

**Fix:** resolve tile state by PRECEDENCE instead of first-match — an overlay
state wins over the base `existing`. Order: `remove > move_out_ghost > move_in >
add > existing`. (`editor.js`, `rdStateFromClassList`.)

**Verified:** after the fix, the same two remove-flagged dccore read as
`state:"remove"`, and `canPlaceAt(b15 → freed slot, full-depth)` returns
`ok:true, blockers:[]`, no invariant violations — so the drop now lands and the
removed devices route through the normal displacement-stripe flow.

**Regression test:** `tests/e2e/test_editor_sweep.py::EditorShadowOwnershipTestCase
::test_removed_fulldepth_frees_slot_for_placement` — remove-flags the full-depth
device, asserts its read-model state is `remove` on every face, and that
`canPlaceAt` allows a full-depth tile onto the freed rows. Verified test-first:
FAILS on the pre-fix `rdStateFromClassList` (`'existing' != 'remove'`), passes on
the fix.

---

## FIXED — re-dragged move reverts to ORIGIN instead of its last valid slot on an illegal drop

**Reported:** 2026-07-15 ("moved a device onto an occupied slot and everything
went to hell").

**Symptom:** move a device to a valid slot A (it becomes a `move_in`), then drag
it AGAIN onto an OCCUPIED slot. Instead of snapping back to A (its last valid
position), the whole move is undone and the device jumps back to its ORIGINAL
slot O.

**Root cause:** an illegal drop routed through `cancelMove`, which for an
already-moved tile reverts to the device's origin (the ghost / real slot). That
is correct for the explicit ×/dialog "cancel the move," but wrong for "reject
this drag" -- the two were conflated. Note the moved tile's `w.kind` stays
`"existing"` (only the move_in CLASS + temp ghost change), so a naive
`kind === "move_in"` check does NOT identify a moved tile.

**Fix (editor.js):** capture the tile's PRE-DRAG slot at `onDragStart`
(`preDragGsY`/`preDragFace`). A new `rejectDrop()` snaps a tile whose pre-drag
slot differs from its origin back to that pre-drag slot (keeping the move,
leaving the ghost in place); a tile still at its origin falls through to the full
`cancelMove`. The two illegal-drop paths in `maybePromptMove`
(`enforceCursorPlacement` reject + `tileOverlapsOther`) now call `rejectDrop`;
the explicit ×/dialog cancels still call `cancelMove`.

**Two variants, one fix:**
- **Variant 1 (existing device moved this session):** kind stays `"existing"`;
  the tile only grows the move_in class + temp ghost.
- **Variant 2 (RELOADED move_in):** kind is `"move_in"`; `st.origUPosition` is
  set to `w.u_position` at state-init — i.e. the move TARGET, not the real
  origin. So ANY origin comparison reads "still at origin" and a first cut of
  the fix (gated on `pre-drag != origin`) still fell through to `cancelMove`,
  which reverts a `move_in` all the way to its **ghost / real origin**. The user
  hit this: "the device returned to its old ghost place instead of the last slot
  of its move_in."

- **Variant 3 (CROSS-FACE reject):** a front move_in dragged onto an occupied
  REAR slot. The first cut only snapped back when the drop face matched the
  pre-drag face, so a cross-face reject slipped through to `cancelMove` → ghost.
  (Pinned by a `window.__rdRejectDebug` log: `rejectDrop{preDragFace:"front",
  curFace:"rear"} → cancelMove`.) This was the user's actual gesture.

**Fix (final):** `rejectDrop` snaps the tile back to its PRE-DRAG slot for
existing and move_in alike, on EITHER face — same-face via `grid.update`,
cross-face by re-homing (`homeInto`, under `refreshing` so its synchronous
`added` event is treated as our own re-home, mirroring `restoreFromGhost`). No
origin comparison. The only exception is a LIVE cross-rack adoption
(`st.crossRack`), which returns to its source rack via `cancelMove`.
`scheduleRefresh` then reconciles state.

**Regression tests (both test-first: FAIL on the pre-fix code with
`22 != 30`, pass on the fix):**
- `EditorSweepTestCase::test_moved_device_rejected_second_drop_returns_to_last_valid_slot`
  — variant 1 (existing device moved O→A, then illegal drop → back to A).
- `EditorSweepTestCase::test_reloaded_move_in_rejected_drop_returns_to_move_in_slot_not_ghost`
  — variant 2 (design pre-saved with a move → reloaded move_in at A, illegal
  drop → back to A, not the ghost O).
- `EditorSweepTestCase::test_moved_device_rejected_cross_face_drop_returns_to_last_valid_slot`
  — variant 3 (front move_in → illegal drop on an occupied REAR slot → back to
  A on the FRONT). Test-first: FAILS same-face-only (`['front', 22] !=
  ['front', 30]`), passes on the cross-face re-home.

## FIXED — full-depth move onto a removed slot renders a blank / duplicate opposite hatch

**Reported:** 2026-07-15 (surfaced once the remove-slot fix above unlocked the
move). The move itself and the saved result are CORRECT — this is purely the
opposite-face *shadow* rendering.

**Symptoms:** after moving a full-depth device (`a23`, type `Dell R640 sff8`)
onto the slot of a removed full-depth device (`dccore-*`):
- On the normal view the moved device's OPPOSITE-face hatch shows no device type
  (the primary face is fine).
- On the heatmap that hatch never fills and shows no name.

**Evidence (`__rdModel`):** the primary rear tile is fully populated
(`data-name`, `data-device-type-name=Dell R640 sff8`, `data-draw-w=432`,
`data-power`). But the front opposite hatch(es) have all of those `null`, and
`__rdModel.check()` reports `I1 rack 321 front rows 28-29:
sg2-sl-a23(shadow) overlaps sg2-sl-a23(shadow)` — TWO hatches for one owner
(widx 18).

**Two defects, both in the CLIENT opposite-hatch path:**
1. **Duplicate hatch (I1):** `syncDeviceShadow` keeps ONE `st.shadowEl` per
   device, but on this path a stale (server-rendered, load-time) opposite hatch
   is not reclaimed as that `st.shadowEl` before the move's face-flip creates a
   fresh one — leaving two overlapping shadows. Ownership/orphan-reclaim gap in
   `placeOrMoveShadow` / initial-load shadow adoption.
2. **Blank hatch content:** `placeOrMoveShadow` → `makeOppositeElement` only
   sets the `.nbx-rd-label` span; it does NOT stamp `data-device-type-name`
   (so the hatch can't show the device type the way the SERVER-rendered hatch
   in `inc/rack_block.html` does) nor `data-draw-w`/`data-power` (so
   `power_heatmap.js` can't fill it or show a name). Client and server hatches
   must render identically.

**Fix (three parts):**
1. `syncDeviceShadow` (editor.js): after `placeOrMoveShadow`, mirror the owner
   tile's `data-device-type-name` / `data-draw-w` / `data-draw-known` /
   `data-power` onto the hatch content, so a client hatch renders like the
   server one (shows the type; the heatmap can fill + label it).
2. `syncOwnedShadows` (editor.js): a duplicate/orphan sweep — remove any
   `data-rd-derived-opp` element not in the canonical owned set (`st.shadowEl`
   for each state + `ghostShadows[idx]`), killing a stale same-rows shadow (the
   `I1` "shadow overlaps shadow").
3. `power_heatmap.js`: fill opposite hatches by their owner's `data-draw-w`
   (excluded from `countingTiles`, so they never affect the max/total), so a
   full-depth device's consumption shows on BOTH faces.

**Analysis note (what the test taught us):** a full-depth MOVE legitimately
leaves TWO opposite hatches owned by the same widx — the body's move_in shadow
at the NEW rows AND the move-out GHOST's mirror (`nbx-rd-state-move_out_ghost`,
`ghostShadows[idx]`) at the OLD rows. Those are DIFFERENT elements at different
rows and are correct; the real `I1` bug is two shadows at the SAME rows/state.
The regression test therefore counts only the live (non-ghost) shadow.

**Regression test:** `EditorShadowOwnershipTestCase
::test_moved_fulldepth_shadow_is_unique_and_carries_identity` — moves a
full-depth device, asserts exactly ONE live opposite shadow carrying the owner's
device type + draw, and no `I1` naming it. Verified test-first: FAILS on the
pre-fix hatch (`dtName None`), passes on the fix.
