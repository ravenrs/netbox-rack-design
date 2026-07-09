# Editor spec-conformance matrix

Cross-reference of every behavioral rule in `docs/editor-behavior-spec.md` §4
(movement rules) against the contexts it applies in, and the test(s) that
prove it. Built 2026-07-08 alongside the fix for the confirmed live
cross-rack "homecoming" bug (§4.6). Updated 2026-07-09 in a post-release
hardening pass that closed the two remaining "Known gaps" (issues #21/#22)
and added dedicated regression coverage for the §4a rename-dialog
cancel/× contract (issue #17). Status legend:

- **covered** — a deterministic e2e test exercises this exact rule in this
  exact context and was GREEN at the time this matrix was written.
- **NEW** — did not exist before this pass; written and verified as part of
  this work (falsifiability where noted: failed on pre-fix code, passes
  after the fix).
- **CLOSED (date)** — a row that was previously an open gap, now covered by
  a named test; falsifiability/verdict noted inline.
- **N/A** — the context does not apply to this rule (reason given).

Test files: `tests/e2e/test_editor_sweep.py` (classes
`EditorSweepTestCase`, `EditorDensePackRejectTestCase`,
`EditorHatchOverlapNoPushTestCase`, `EditorShadowOwnershipTestCase`,
`EditorDisplacementTestCase`, `EditorCrossRackSweepTestCase`),
`tests/e2e/test_editor_e2e.py` (`EditorE2ETestCase`),
`tests/e2e/test_editor_add_sweep.py` (`EditorAddSweepTestCase`).

## §4.1 — The pipeline (validate → confirm → commit)

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| No GridStack push during a gesture | same-rack, any depth | zero other tiles change position as a side effect of one drag | `EditorDensePackRejectTestCase::test_drop_onto_occupied_units_on_packed_rack_is_rejected` (full before/after position snapshot of every tile) | covered |
| No GridStack push during a gesture | cross-rack | zero other tiles (in EITHER rack) move as a side effect | `EditorCrossRackSweepTestCase::test_sweep_existing_fulldepth_across_racks` (full-world diff every step, both racks) | covered |
| No hatch-insertion push cascade | same-rack, full-depth | a legal move that inserts/moves an opposite-face hatch never collaterally relocates real rear bodies | `EditorHatchOverlapNoPushTestCase::test_overlapping_hatches_never_push_rear_bodies` | covered |
| Dialogs only after validation passes | displacement (any) | `canPlaceAt`/`tileOverlapsOther` passes before any dialog opens | `EditorDisplacementTestCase::test_e5_displace_ghost_confirm` (dialog only fires for an allowed target) | covered |
| Commit is atomic (body+shadow+ghost in one call) | full-depth, same-rack | no DOM state where only part of the device moved | `EditorShadowOwnershipTestCase::test_mid_drag_shadow_tracks_candidate_position` + full sweep hatch/position-in-lockstep checks | covered |

## §4.1a — Cursor-governed placement (hard rule added 2026-07-08)

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| Release while cursor over ILLEGAL rows = full snap-back (no last-valid fallback commit) | cross-rack, pointer-driven drag | tile snaps back home, world byte-identical, no dialog | `EditorCrossRackSweepTestCase::test_cursor_release_over_occupied_rows_snaps_back` | **NEW** — falsified pre-fix (committed at the placeholder's last-valid slot: the confirmed live dra4-sl-isp29 F11→F08-rear bug); green post-fix |
| Deny indicator at the CURSOR's rows while hovering illegal rows; vendor placeholder hidden meanwhile | mid-drag | red `.nbx-rd-cursor-deny` overlay at cursor rows, `.grid-stack-placeholder` display:none while deny active | same test (`denyVisible` assertion) | **NEW** |
| Commit position always equals the cursor's rows | any drag with pointer data | pointer inside the landed span → accepted; legal-but-divergent engine placement → repositioned to the cursor rows; divergent grid → snap-back | mechanism: `enforceCursorPlacement` on every drop path (`maybePromptMove` + `maybeRevertAddMove`); exercised by every real-mouse test (`test_07_fulldepth_crossface_move_follows_shadow_and_thaws`, live probes) | **NEW** (mechanism) |
| Shim/no-pointer gestures unaffected | deterministic tests | tracker inert without pointer data; engine-landed position governs | all existing shim-driven sweeps stayed green unchanged | covered |
| Release over ILLEGAL cursor rows discards the drag-in entirely | palette add | NO add created (no widget, no dirty residue — Save untouched), world byte-identical, deny indicator shown at the cursor rows mid-drag | `EditorCrossRackSweepTestCase::test_palette_release_over_occupied_rows_creates_no_add` | **NEW** (2026-07-08, palette pass) — falsified pre-fix: the add committed at the engine's fallback slot (rear y=0, `nbx-rd-state-add`), deny never shown |
| Release over legal free rows commits the add via the existing pipeline | palette add | add created, naming preview flow | `test_sweep_palette_add_across_faces`, `EditorDisplacementTestCase::test_e11_displace_ghost_via_palette_add`, `EditorE2ETestCase::test_03_palette_drop_adds_payload_item` | pre-covered |
| Release on a vacated (ghost) slot: no deny (a ghost never blocks), displacement dialog fires, add commits with `add` styling | palette add, cursor-armed | | `EditorCrossRackSweepTestCase::test_palette_cursor_release_on_vacated_slot_fires_displacement` | **NEW** (deny-absence + dialog under the armed tracker; the dialog itself was pre-covered by E11) |
| Palette gesture arming | palette add | tracker armed at pointer-DOWN on the `.nbx-rd-palette-item` (geometry from `data-u-height`/`data-is-full-depth`, grabRows=0), disarmed one tick after pointer-up so a plain click never leaves a stale gesture and the drop enforcement still sees it | mechanism, exercised by both palette tests above | **NEW** (mechanism — the former "palette drag-ins never arm the tracker" gap is CLOSED) |

## §4.1b — Dialog pipeline on every committed cross-rack move (ruling 2026-07-08)

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| §4a rename dialog opens on EVERY committed cross-rack adoption | cross-rack, 1-hop | `.nbx-rd-move-modal` opens; Apply commits, dismiss aborts | `EditorCrossRackSweepTestCase::test_committed_cross_rack_move_fires_rename_dialog` | **NEW** — falsified pre-fix: 0 dialogs (maybePromptMove's `kind !== "existing"` early-return skipped rename AND displacement for every adopted move_in, on every path, not only the fallback one) |
| Pipeline assertion on every committed rack-crossing sweep hop | cross-rack sweep, every hop | ≥1 rename/displacement dialog answered per committed crossing; exact-true-origin homecoming exempt (silent per §4.4/§8.3) | `_sweep`'s `dialog_pipeline_skipped` violation check, active in `test_sweep_existing_fulldepth_across_racks` | **NEW** |
| Displacement dialog also fires for a move_in landing on a vacated slot | cross-rack / adopted tile | same §4.3 flow as an existing-kind mover | exercised by the cross-rack sweep whenever the subject crosses rack B's ghost rows (dialogs answered per step; stripe toggling covered by the world diff) | **NEW** |
| Dialog never stranded by a fast confirm/dismiss | any dialog | a confirm/dismiss click during the show-fade still closes the dialog | exercised by every dialog-driving test at shim speed (~60ms after open) — pre-fix the FIRST click was silently swallowed by Bootstrap's `_isTransitioning` guard, stranding the dialog open forever (and making the old test-side dismissals no-ops) | **NEW** (transition-safe `requestHide` in both dialog builders) |
| §4a rename dialog Cancel/× fully reverts the committed move (issue #17) | cross-rack | dismissing via the Cancel button OR the × close button both run `cancelMove` (full revert: device back at origin rack/face/position, world byte-identical, no dialog left in the DOM) | `EditorCrossRackSweepTestCase::test_rename_dialog_cancel_button_fully_reverts_cross_rack_move`, `::test_rename_dialog_close_x_fully_reverts_cross_rack_move` | **CLOSED (2026-07-09)** — `showMoveNameDialog` on this checkout's HEAD already wired the explicit `finishCancel()+requestHide()` handlers to both affordances (matching `showDisplaceConfirmDialog`'s proven pattern), so the tests pass without a code change; falsifiability was verified by temporarily reverting to the old Bootstrap-delegation-only pattern and confirming the exact documented failure (`Page.wait_for_function: Timeout 5000ms exceeded` waiting for the dialog to close) before restoring the fix |

## §4.1c — One occupant per vacated slot (ruling 2026-07-08)

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| Second device onto an already-taken vacated slot is blocked | same-rack, palette add after a confirmed displacement | rejected before any dialog; first occupant untouched; model clean | `EditorDisplacementTestCase::test_second_device_blocked_on_taken_vacated_slot` | **pre-covered mechanism, NEW test** — `rdCanPlaceAt` already blocked it (verdict `occupied by <first occupant>`); the test's initial failure was the stranded-dialog artifact above, not the blocking rule |

## §4.2 — Blocking rules (`Unit.blockingClaimFor`)

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| Live body/shadow blocks | same-rack | drop rejected, snap back | `EditorSweepTestCase::test_sweep_existing_fulldepth_device` (explicit onto-occupied-obstacle assertion) | covered |
| Live body/shadow blocks | cross-rack | foreign drop onto a live shadow rejected, full revert | `EditorCrossRackSweepTestCase::test_foreign_drop_onto_shadow_rejected_restores_all` | covered |
| Live body/shadow blocks | packed rack (zero free rows) | rejected without stack overflow, zero collateral moves | `EditorDensePackRejectTestCase::test_drop_onto_occupied_units_on_packed_rack_is_rejected` | covered |
| Ghost/remove-flagged claim never blocks | same-rack | placement allowed, triggers displacement flow | `EditorDisplacementTestCase::test_e5_displace_ghost_confirm`, `test_e11_displace_ghost_via_palette_add` | covered |
| Own shadow/own ghost never blocks | same-rack | moving within own footprint legal | `EditorSweepTestCase` sweep (full 0.5U sweep incl. crossing own shadow rows) | covered |
| Own ghost never blocks (cross-rack identity) | cross-rack return | dropping D back onto D's own ghost is legal by construction | `EditorCrossRackSweepTestCase::test_homecoming_return_to_exact_origin_is_silent_and_clean` | **NEW** (falsified pre-fix: found a duplicate `move_in` entity instead) |

## §4.3 — Displacement (placing onto a vacating slot)

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| NEW renders with its own semantic style, never inherits ghost styling | move onto ghost | `move_in`/`add` styling | `EditorDisplacementTestCase::test_e5_displace_ghost_confirm` | covered |
| OLD collapses to side reservation stripe | half-depth OLD | red stripe, hover shows OLD's name | `test_e5_displace_ghost_confirm` | covered |
| OLD's mirror hatch collapses too | full-depth OLD, full-depth NEW | stripe on opposite face | `EditorDisplacementTestCase` (mirror-collapse assertions in e5) | covered |
| Confirmation dialog after validation, every displacement | same-rack | dialog shown; cancel → full revert | `test_e5_displace_ghost_confirm`, `test_e6_displace_ghost_cancel` | covered |
| Cancel restores OLD's ghost/remove rendering | same-rack | stripe removed, ghost/remove styling back | `test_e6_displace_ghost_cancel` | covered |
| Moving NEW away restores OLD | same-rack | OLD un-stripes when NEW leaves | `test_e7_displace_ghost_then_move_new_away` | covered |
| Palette add onto vacating slot | palette add | `add` styling, same displacement flow | `test_e11_displace_ghost_via_palette_add` | covered |
| Stripe geometry: bar OUTSIDE the rack frame (user ruling 2026-07-09) | any displacement | the stripe is a narrow red-striped bar hanging off the face grid's RIGHT edge, vertically spanning exactly the displaced rows (±3px), "was: <OLD>" hover title, one bar per collapsed face (front + full-depth mirror on the rear), removed when OLD is restored (E7 path); OLD's `.nbx-rd-displaced` collapse semantics unchanged; the bar carries the owner's `nbx-rd-state-*` class so legend filters toggle it identically | `EditorDisplacementTestCase::test_e5_stripe_bar_outside_rack_frame` | **CLOSED (2026-07-09, live-user restyle)** — falsified pre-fix verbatim: `AssertionError: 550 not greater than or equal to 558 : stripe bar must hang OUTSIDE the grid's right edge, not inside a tile` (the old `.nbx-rd-stripe` was an in-tile child at the occupying tile's right edge, cramped against its × button). Implementation: a `.nbx-rd-grid-wrap` positioning anchor around each face grid (rack_block.html), the bar owned by the displacement record (`d.stripeEls`, created in `displaceOne`/destroyed in `undisplaceOne` — no global scans), percentage top/height so it tracks rows through resize. Post-fix geometry (test summary): front bar left=561 vs grid right=559; rear bar left=823 vs 821; bar height 44.2px vs ghost 44px |
| Displacement via cross-rack adoption (issue #21) | cross-rack | dialog fires after validation; confirm → NEW `move_in`, OLD collapses to the red "was:" stripe + mirror hatch (both full-depth); cancel → full revert, ghost/mirror restored | `EditorCrossRackSweepTestCase::test_cross_rack_drop_onto_vacating_slot_displaces_confirm`, `::test_cross_rack_drop_onto_vacating_slot_displaces_cancel` | **CLOSED (2026-07-09)** — clean pass on current code (coverage gap, not a bug); new rack-B fixture (`dev_b_ghost_full`, 2U full-depth ghost source) added so both OLD and NEW are full-depth and the mirror-hatch collapse is actually exercised |
| SAVED-displacement rendering parity: projection marking + read-only elevation + editor on-load | any projection render | the projection marks a vacating slot overlapped by a live planned slot `displaced` (+`displaced_by`; same-placement/same-device never self-displaces; full-depth mirrors marked per face); the READ-ONLY elevation renders OLD as the outside stripe bar (no full tile); the EDITOR applies collapse+bars on LOAD from the widget payload's marking | backend: `test_projection.py::DisplacedProjectionTestCase` (4 tests), `test_views.py::DisplacedElevationRenderTest` (2 tests); e2e: `EditorDisplacementTestCase::test_saved_displacement_renders_on_load` | **CLOSED (2026-07-09, live-acceptance regression #3)** — falsified pre-fix verbatim: projection `KeyError: 'displaced'`; elevation `'nbx-rd-stripe' not found in ...` (both OLD's ghost and NEW's add rendered as full composited tiles at the same U — the reported screenshot); the e2e confirmed the EDITOR's on-load render had the same hole (the user's session only looked right because the interactive gesture had run displaceOne), plus a second pre-fix capture mid-implementation: `['front'] != ['front', 'rear']` (a reloaded catalog add carried no `is_full_depth`, so the mirror bar was skipped — fixed by adding the flag to `_slot_to_widget`). Implementation: `projection._mark_displaced()` per-face post-pass; `rack_block.html` non-editable branch skips displaced tiles and emits `.nbx-rd-stripe` bars (percent geometry via new `stripe_top_pct`/`stripe_height_pct` filters); `editor.js applySavedDisplacements()` runs once at the first `refreshGhosts` settle, routing through the SAME `displaceOne`/`st.displaces` records as the live flow (so move-NEW-away restores OLD identically); `rack_design.js` hover-card extended to `.nbx-rd-stripe` for the elevation page |

## §4.4 — Move within one face

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| Move creates ghost at origin + crossed shadow; destination `move_in` | same-face, full-depth | ghost+shadow rendered, owner-state-tinted | `EditorSweepTestCase` full sweep (`hatchLive`/`hatchGhost` assertions every step) | covered |
| Move creates ghost at origin | same-face, half-depth | ghost only, no shadow | `EditorDisplacementTestCase` fixtures (half-depth OLD) | covered |
| Moving back onto own ghost = silent revert, no dialog | same-rack, same-face | ghost destroyed, tile back to `existing`, no dialog | `EditorDisplacementTestCase::test_self_return_onto_own_ghost_is_silent` | covered |

## §4.5 — Cross-face move (front ↔ rear, same rack)

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| Body+shadow swap faces atomically | full-depth | one commit, no half-moved state, no lockup | `EditorE2ETestCase::test_07_fulldepth_crossface_move_follows_shadow_and_thaws` | covered |
| Origin ghost stays on original face (+ crossed shadow) | full-depth | ghost/mirror remain on source face after cross-face move | `EditorSweepTestCase` sweep (cross-face alternation phase) | covered |
| Non-full-depth cross-face move | half-depth | only origin ghost, no shadow bookkeeping | `EditorE2ETestCase::test_05_live_move_ghost` | covered |

## §4.6 — Cross-rack move (the confirmed bug's home rule)

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| Cross-rack move = same as 4.4/4.5, origin rack keeps Ghost, destination gains D | 1-hop, full-depth | adoption creates `move_in` in destination, ghost stays in origin | `EditorCrossRackSweepTestCase::test_sweep_existing_fulldepth_across_racks` | covered |
| Cross-rack move | 1-hop, palette add | adds never cross racks (`isForeignRealTile` excludes adds) | `EditorCrossRackSweepTestCase::test_sweep_palette_add_across_faces` (add swept on rack A only, by design) | covered |
| **Return to origin (exact U/face) fully clears the Ghost and restores original name/state** | 1-hop return, full-depth | single `existing` entity, no ghost, no orphan shadow anywhere | `EditorCrossRackSweepTestCase::test_homecoming_return_to_exact_origin_is_silent_and_clean` | **NEW** — falsified pre-fix (found duplicate `move_in` entity: no restore, orphan shadow, stale ghost — the reported 5-entity bug); green post-fix |
| Return to origin RACK, different U (near-miss) | 1-hop return, different U | revives ORIGINAL entry as an ordinary move; ghost stays at true origin; never two entities | `EditorCrossRackSweepTestCase::test_homecoming_near_miss_reuses_original_entry` | **NEW** — decision made explicit in this pass (spec was silent on this exact sub-case; resolved per the task's instruction to reuse the original entry) |
| Multi-hop chain (A→B→C→A) still finds TRUE origin | 3-hop | homecoming works no matter how many intermediate racks | `EditorCrossRackSweepTestCase::test_homecoming_after_three_hop_chain_still_finds_true_origin` | **NEW** |
| Homecoming after page reload (persistent ghost, no in-session bookkeeping) | 1-hop, save+reload | still homes correctly via device-identity ghost lookup, not session state | `EditorCrossRackSweepTestCase::test_homecoming_after_save_and_reload_persistent_ghost` | **NEW** — this exposed a real design gap in the first fix draft (see "Fix design" below); closed before shipping |
| Rejected cross-rack drop reverts fully (foreign tile + destination shadow) | 1-hop, rejected | tile back home, shadow class matches owner, no residue | `EditorCrossRackSweepTestCase::test_foreign_drop_onto_shadow_rejected_restores_all` | covered (now upgraded to a full-world diff, subject included) |
| A rack never holds two body entities for one device_id | any hop count | invariant | enforced by every homecoming test's `_assert_homecoming_contract` (exactly 1 body world-wide) | **NEW** |

## §4.7 — Rejected placement

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| Target blocked → snap back to exact prior position, zero other tiles move | same-rack, packed | E8: no `RangeError`, no console errors, clean read-model | `EditorDensePackRejectTestCase::test_drop_onto_occupied_units_on_packed_rack_is_rejected` | covered |
| Target blocked → snap back | same-rack, sparse | explicit obstacle-row assertion during full sweep | `EditorSweepTestCase::test_sweep_existing_fulldepth_device` | covered |
| Target blocked → full revert | cross-rack | `EditorCrossRackSweepTestCase::test_foreign_drop_onto_shadow_rejected_restores_all` | covered |

## §4.8 — Palette add

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| Add follows same pipeline, creates `add` state (+ shadow if full-depth) | same-rack | `EditorAddSweepTestCase::test_add_and_sweep_full_depth`, `test_add_and_sweep_one_u` | covered |
| Add re-dragged after initial drop validated like a real device | same-rack | reject-onto-occupied / displacement paths reused (`maybeRevertAddMove`) | `EditorCrossRackSweepTestCase::test_sweep_palette_add_across_faces` (re-drags across faces) | covered |
| Add onto vacating slot = §4.3 with `add` styling | same-rack | `EditorDisplacementTestCase::test_e11_displace_ghost_via_palette_add` | covered |
| Add across racks | cross-rack | N/A — `isForeignRealTile` deliberately excludes adds; spec never asks for this | N/A (by design, see spec §2.1 "existing/move_in" gate) | N/A |

## Global invariants (I1/I2/I3, spec §6) and the full-world upgrade

| Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|
| I1 no two live claims overlap | every step, both sweeps | `window.__rdModel.check()` clean every step | all sweep classes | covered |
| I2 exactly one shadow per full-depth device, opposite face, own units | every step | hatch-count/face/y/class assertions every step | `EditorSweepTestCase`, `EditorCrossRackSweepTestCase` | covered |
| I3 console/page error free | every step | `self.errors == []` assertions | all classes | covered |
| **Full-world diff**: every entity NOT owned by the swept subject is byte-identical (classes, geometry, title, owner identity) step-to-step | every step, both faces, both racks | zero "bystander" drift anywhere, not just the swept tile | `EditorSweepTestCase::test_sweep_existing_fulldepth_device` (76 steps), `EditorCrossRackSweepTestCase::test_sweep_existing_fulldepth_across_racks` (87 steps), `test_sweep_palette_add_across_faces` (31 steps), plus before/after diffs on `test_foreign_drop_onto_shadow_rejected_restores_all` and both new homecoming tests | **NEW** — this is the upgrade added in this pass specifically because scoped per-subject checks were provably insufficient to catch cross-rack duplicate-entity/orphan-shadow class bugs; ran clean (0 violations) on the fixed code across all of the above |
| **Full-world diff extended to the 3 short targeted classes (issue #22)** | every gesture in `EditorDensePackRejectTestCase` / `EditorHatchOverlapNoPushTestCase` / `EditorShadowOwnershipTestCase` | strict (subject-exempt=null) diff for a rejection; subject-exempt diff for a legal move | `test_drop_onto_occupied_units_on_packed_rack_is_rejected` (strict), `test_overlapping_hatches_never_push_rear_bodies`, `test_mid_drag_shadow_tracks_candidate_position`, `test_remove_state_shadow_is_crossed_out` (all subject-exempt) | **CLOSED (2026-07-09)** — ran clean (0 new violations) on current code; `test_conflict_shadow_rendered_and_reported` intentionally left without a diff (it performs no gesture, so there is no before/after to compare) |

## Known gaps (honest accounting, not silently left uncovered)

- ~~Displacement via a cross-rack adoption~~ **CLOSED (2026-07-09, issue
  #21)**: a dedicated test now drops a foreign (rack-A) tile directly onto
  rack B's ghost slot and asserts the full §4.3 contract (dialog after
  validation, NEW `move_in`, OLD's stripe + mirror hatch, cancel → full
  revert). See `EditorCrossRackSweepTestCase::test_cross_rack_drop_onto_vacating_slot_displaces_confirm`
  / `::test_cross_rack_drop_onto_vacating_slot_displaces_cancel`.
- ~~Cursor governance for palette drag-ins~~ **CLOSED (2026-07-08, palette
  pass)**: the tracker now arms at pointer-down on the palette item; a
  release over illegal cursor rows discards the drag-in entirely (no add,
  no dirty residue) with the deny indicator shown mid-drag. See the §4.1a
  palette rows (`test_palette_release_over_occupied_rows_creates_no_add`,
  `test_palette_cursor_release_on_vacated_slot_fires_displacement`).
- ~~EditorDisplacementTestCase / EditorShadowOwnershipTestCase /
  EditorHatchOverlapNoPushTestCase not upgraded to the full-world diff
  net~~ **CLOSED (2026-07-09, issue #22)** for `EditorHatchOverlapNoPushTestCase`
  and `EditorShadowOwnershipTestCase` (both now assert a full-world diff on
  every gesture-driving test). `EditorDisplacementTestCase` remains on its
  existing per-entity assertions (stripe, mirror, dialog) by design — its
  short/few-step tests already assert on every relevant entity directly, so
  the marginal value of a full-world diff there is lower; not silently
  claimed as upgraded. `EditorShadowOwnershipTestCase::test_conflict_shadow_rendered_and_reported`
  is also intentionally excluded (no gesture is performed, so there is no
  before/after to diff).

## Fix design (as implemented)

`netbox_rack_design/static/netbox_rack_design/js/editor.js`:

- `findOwnGhostEntryIndex(deviceId)` (new, ~20 lines, right before
  `homecomingAdopt`): device-identity lookup — scans THIS rack's temp
  ghosts (`tempGhosts`) and persistent (`nbx-rd-state-move_out_ghost`) DOM
  tiles for one whose state entry's `device_id` matches. A ghost's mere
  presence for device D in a rack is proof-by-construction that rack is D's
  true origin (ghosts are only ever created in `onTileDeparted` for a
  departing `existing` entry, or rendered by the server from that same
  fact) — so this works identically for same-session multi-hop chains AND
  for a page-reloaded persistent ghost, with no special-casing.
- `homecomingAdopt(el, d)` (new, ~35 lines): if `findOwnGhostEntryIndex`
  finds a match for the dropped device, revives that ORIGINAL state entry
  at the drop position instead of adopting a new one — destroys the
  ghost/temp-ghost + its mirror hatch, re-tags the DOM element with the
  original widget-index, resets classes to `existing`. Relies on the
  *existing, unchanged* `atOrigin` comparison in `refreshGhosts`/
  `maybePromptMove` to decide silent full restore (dropped exactly on the
  ghost) vs. an ordinary move (near-miss elsewhere in the origin rack) —
  zero new dialog/state-machine code.
- Wired into the `added` grid handler, checked before `adoptForeignTile`.
- `onTileDeparted`: fixed the symmetric departure leak — `destroyShadowEl`
  and `restoreDisplaced` now always run on departure (previously gated
  behind `kind === "existing"`, so a departing adopted `move_in` copy left
  its shadow orphaned); a departing non-`existing` entry is now nulled out
  of `state[]` instead of leaking a dead entry.

First implementation draft used `tileInFlight.originRackId`/
`originWidgetIndex` (in-session hop-chain bookkeeping) instead of the
device-identity ghost lookup; that was corrected during review because it
does not survive a page reload (state rehydrated from server JSON carries
no such runtime flags) — the shipped fix is identity-based per the original
task brief, not hop-count-based.

### Cursor-governed placement + dialog pipeline (added later on 2026-07-08)

Same file:

- Module-level pointer tracker (`rdLastPointer`/`rdCursorGesture`,
  `rdFaceHostAt`/`rdRowAt`/`rdCursorCandidate`/`rdUpdateCursorGesture`,
  document-level `pointermove`+`mousemove` listeners): armed per-gesture by
  `onDragStart` (only when the pointer is physically on the grabbed tile),
  disarmed by `thawAllTiles`. Renders the `.nbx-rd-cursor-deny` overlay at
  the cursor's candidate rows while they are illegal and hides the vendor
  placeholder meanwhile (`.nbx-rd-deny-active .grid-stack-placeholder`).
- `enforceCursorPlacement(itemEl, gsH, isFullDepth, onReject)`
  (per-controller, next to `tileOverlapsOther`): runs at the TOP of the
  drop pipeline in `maybePromptMove` and `maybeRevertAddMove`, before
  validation. Pointer inside the engine-landed span → accept; pointer rows
  illegal or over a different grid → `onReject()` (full snap-back);
  pointer rows legal but the engine parked the tile elsewhere → commit at
  the cursor's rows. No vendor code was patched for this feature.
- Dialog pipeline for adopted moves: removed `maybePromptMove`'s
  `kind !== "existing"` early-return; `adoptForeignTile` stamps
  `needsRename: true` on every adoption; `promptRename` prompts for
  `existing` movers and for `move_in` tiles with `needsRename` (cleared on
  Apply and by homecoming/restore paths). Displacement dialogs now also
  fire for move_in landings on vacated slots.
- Transition-safe dialog dismissal: both dialog builders queue `hide()`
  until `shown.bs.modal` (`requestHide`) — Bootstrap's `hide()` silently
  no-ops during the show-fade, which stranded a fast-clicked dialog on
  screen forever (and had been masking §4a dismissal-aborts semantics in
  the displacement tests).

### Palette cursor governance (third pass, 2026-07-08)

Same file:

- `rdTrackPointerDown` arms a `palette: true` gesture when the pointer
  goes down on a `.nbx-rd-palette-item` (geometry from `data-u-height` /
  `data-is-full-depth`; `grabRows` 0 — a palette row's height has no
  relation to the grid row scale). A document-level pointer-up handler
  disarms it one tick later, after GridStack's synchronous drop processing
  has consumed it, so a plain click never leaves a stale gesture.
- `onPaletteDrop` (device-type branch) enforces the gesture before
  validation: cursor inside the engine-landed span → normal pipeline;
  cursor rows illegal or over a different grid → the clone is removed
  outright (no add, no dialog); cursor rows legal but divergent → the add
  commits at the cursor's rows.
- Dirty-state hygiene: the per-grid `added`/`removed`/`dropped` listeners
  no longer `markDirty` for an UNREGISTERED palette clone (it still
  carries `data-device-type-id`; `finishAdd` strips it and calls
  `markDirty` itself on successful registration) — a discarded drag-in
  leaves the Save button untouched.

## §9 — Non-racked tray (0.9.0)

Test file: `tests/e2e/test_editor_tray.py` (`EditorTrayTestCase`), plus
backend unit tests `netbox_rack_design/tests/test_projection.py`,
`netbox_rack_design/tests/test_models.py` (tray-target `clean()` rows), and
`netbox_rack_design/tests/test_api.py::SaveLayoutTest` (tray save-contract
rows). Spec: `docs/editor-behavior-spec.md` §9.

| Row | Rule | Context | Expected behavior | Covering test | Status |
|---|---|---|---|---|---|
| T-tray-1 | Real position-less device projects as `existing` tray slot | load, real DCIM device with `rack=R, position=None` | tile renders in R's tray with `nbx-rd-state-existing`; a rack with none renders an empty tray | `test_projection.py::TrayProjectionTestCase` (backend); `test_editor_tray.py::test_tray_1_real_device_renders_as_existing`, `::test_tray_1_negative_rack_without_tray_devices_is_empty` | **NEW** — falsified pre-fix on both layers: backend `AssertionError: 'PDU-A1' not found in {}` with `_existing_tray_slots` neutralized; e2e `expected exactly one tray tile ..., got []` |
| T-tray-1 | Tray slot carries no face/row (spec §9.2) | projection + render | `slot["face"] == ""` regardless of the device's real face; I1/I2/I4-clean on load | `test_projection.py::test_real_tray_device_appears_as_existing`; `test_editor_tray.py::test_tray_1_model_check_is_clean` | **NEW** — a first implementation leaked the device's real face into the slot, which editor.js's origin comparison then misread as "moved" (rendered `move_in` instead of `existing`); fixed in `projection.py` + the `atOrigin` tray special-case in `editor.js` (see below) |
| T-tray-2 | units → tray (dismount) | same-rack, real racked device dragged into the tray | origin gets a crossed ghost; tray entry renders `move_in`; §4a rename dialog opens (kind=`existing` always prompts) | `test_editor_tray.py::test_tray_2_units_to_tray_then_homecoming_is_silent` (first half) | **NEW** |
| T-tray-2/T-tray-4 | back onto own tray-origin ghost = homecoming | same-rack, drag the tray tile back onto its real U/face | silent restore (no new dialog), ghost cleared, `existing` restored, I1/I2/I4-clean | `test_editor_tray.py::test_tray_2_units_to_tray_then_homecoming_is_silent` (second half) | **NEW** |
| T-tray-3 | tray → units (mount) | tray tile dragged onto a free U | full §4.2 blocking + §4.3 displacement rules apply at the target; shadow grows if full-depth | mechanism: `onPaletteDrop`'s non-palette branch → `maybePromptMove` runs unchanged for a tray-origin subject (no tray-specific bypass on the *destination* face side); exercised indirectly by T-tray-2's return leg (a clean target). **Displacement-onto-a-vacated-U-from-a-tray-origin and the full-depth-shadow-on-landing sub-cases are not yet covered by a dedicated test.** | **partial** (mechanism reused from §4.1–§4.3, not independently re-verified for a tray-origin subject) |
| T-tray-4 | tray → tray (cross-rack reassociation) | drag a tray tile onto ANOTHER rack's tray | backend: move placement persists with the new rack, no position | `test_api.py::SaveLayoutTest::test_tray_to_tray_reassociation_persists_new_rack_no_position` | covered (backend only) |
| T-tray-4 | tray → tray (cross-rack), the DRAG gesture itself | same | a real device tile dragged from rack A's tray onto rack B's tray: `acceptWidgets` permits it, §4a rename dialog opens, origin tray keeps a list-style ghost (no rows/shadow), destination renders `move_in`, I1/I2/I4-clean | `test_editor_tray.py::test_tray_4_cross_rack_accept_widgets_permits_foreign_tile`, `::test_tray_4_cross_rack_reassociation_drag_and_homecoming` | **CLOSED (2026-07-09)** — falsified pre-fix: `makeAccept(isTray)` unconditionally rejected a foreign real tile at a tray target (`!isTray && isForeignRealTile(el)`), so a real mouse drag never even fired `dropped`/`added` (native GridStack rejection); confirmed live on design 6 via a direct `acceptWidgets(el)` probe (`accepted: false` before, `true` after). Fixing acceptance alone was not enough: `ensureTempGhost`, `cancelMove`, `restoreTile`, and `restoreFromGhost` all resolved a tray origin via `faceGrids[face]` (undefined for `face=""`), so the origin ghost was silently never created and a cancelled/reverted tray-origin move left the tile stranded on a face grid at `move_in`/dirty (also confirmed live on design 6 before the fix, then clean after) — all four now route through a new `targetFor(face)` helper that resolves `""` to the tray grid/host with a fixed 2-row height and an appended (never overlapping) row |
| T-tray-5 | homecoming after save + reload | tray-origin ghost persisted to DB, page reloaded, tile dragged back | identity-based restore survives a reload (same mechanism as §4.6, extended to tray origins by `findOwnGhostEntryIndex`, which is state-agnostic about face) | not independently tested for a tray origin (§4.6's `test_homecoming_after_save_and_reload_persistent_ghost` covers a face origin only) | **known gap** |
| T-tray-6 | palette → tray (new off-rack device) | drop a catalog device type into the tray | `add`-styled entry, `u_position=None`, no dialog (adds use their own inline name field), no displacement | `test_editor_tray.py::test_tray_6_palette_add_into_tray`; backend `test_api.py::SaveLayoutTest::test_palette_add_into_tray_persists_placement_with_no_position` | **NEW** — pre-fix the tray's `dropped` handler unconditionally rejected every palette drop (`"Reject off-rack palette drops: a brand-new add needs a U."`); now routed through `onPaletteDrop` with `face=""`, which short-circuits the row/collision/displacement logic |
| T-tray-6 | discard on release outside any legal target | palette drag released off every grid | no add, no dirty residue | not independently tested (mechanism: GridStack's own `acceptWidgets` — a drop outside any registered grid never fires `dropped` at all) | **known gap** (no dedicated regression) |
| T-tray-7 | I4: one entity per device, world-wide | units→tray→units round trip | `window.__rdModel.check()` reports zero I4 violations at every step | `test_editor_tray.py::test_tray_2_units_to_tray_then_homecoming_is_silent` (asserts `check() == []` after each leg); `editor.js::rdCheckInvariants` I4 block | **NEW** — falsified in dev: an early version of I4 double-counted every tray device (it is present in both `model.devices` and `rack.trayDevices` by construction) and produced a false-positive `I4 device ... has 2 live entities: rack N units/, rack N tray` on every tray load; fixed by excluding non-front/rear faces from the `model.devices` half of the count |
| T-tray-layout | tray is a list — append-only, non-overlapping, no bystander movement | 2 existing + 1 palette add + 1 units→tray move | 4 distinct rows; the 2 pre-existing tiles' rows unchanged; container grows to fit | `test_editor_tray.py::test_tray_layout_appends_without_overlap_or_bystander_movement` | **NEW** — falsified pre-fix on a live dev-instance report: `AssertionError: 3 != 4 ... {'e2e-tray-pdu-...': 0, ..., 'e2e-tray-rackeddev-...': 0}` (the moved-in device landed exactly on top of the first PDU); fixed by giving every tray slot an explicit sequential `gs-y` at render time (`rack_block.html`) and re-asserting the next free row on every `dropped` event (`editor.js::trayAppendRow`) |
| T-tray-layout | origin tray ghost gets its OWN row + standard ghost visual | tray→tray cross-rack move that empties an earlier row | the ghost lands BELOW the bottom-most remaining tile (never on a bystander's rows), bystanders keep their rows, and the ghost carries the standard move_out_ghost visual (translucent rgba grey, dashed border, italic, no inline role background) | `test_editor_tray.py::test_tray_4_origin_ghost_row_and_style_parity` | **CLOSED (2026-07-09, live-acceptance regression)** — falsified pre-fix verbatim: `origin tray ghost (rows 2..3) overlaps remaining tile e2e-tray-pdu2-... (rows 2..3)`. Root cause was NOT CSS (the ghost's own class/computed style was already correct — rgba(108,117,125,0.12), dashed, italic): `trayAppendRow` computed the append row as `count*2`, which broke the moment a tile LEFT the tray (bystanders keep non-contiguous rows), landing the ghost on the remaining tile's row; the ghost's 12%-alpha background then composited over the solid role-colored neighbour into what read live as "a solid dark role-colored ghost with a struck label" (design 6, dra4-pdu-rf11-a1). Fixed: `trayAppendRow` now returns max(y+h) over the remaining tiles. Re-verified live: ghost at gs-y=4 below b1 at gs-y=2, correct translucent styling, model check clean |
| T-tray-layout | tray COMPACTION — a list has no holes | any tray departure / ghost destruction / cancel-revert | remaining tray tiles renumber to contiguous rows 0,2,4,... preserving relative order; container shrinks back to content height. Spec §9.4 ruling (2026-07-09): §4.1's no-bystander-movement constrains RACK positions (U), not list reflow | `test_editor_tray.py::test_tray_compaction_after_cancel_return`, `::test_tray_compaction_after_double_round_trip` (the user's exact live repro: both tiles out then both home) | **CLOSED (2026-07-09, live-acceptance regression #2)** — falsified pre-fix verbatim: scenario (a) `Lists differ: [2, 4] != [0, 2]`; scenario (b) `Lists differ: [8, 10] != [0, 2] ... containerHeight: 131.95` vs 65.98 on load (returned tiles appended below their then-live ghosts; the destroyed ghosts' rows stayed empty — the reported "big dead gap above"). Fixed: `compactTray()` in editor.js, run from `refreshGhosts`' settle pass under its push-suppression bracket, renumbering ONLY this rack's tray tiles (attached via `grid.update`, engine-detached temp ghosts via `_writePosAttr`). Re-verified live on design 6: both PDUs out → ghosts compact at rows 0/2; both home → tiles at rows 0/2, container back to 44px, model check clean |
| §9.5 | Save contract: mount / dismount / tray→tray | save-layout API | mount = `target_position` set; dismount = `target_rack` set + `target_position=None`; tray→tray = new rack, no position; same-site check for tray targets only (no slot-availability check) | `test_api.py::SaveLayoutTest::test_dismount_to_tray_persists_move_with_no_position`, `::test_mount_from_tray_persists_move_with_position`, `::test_tray_to_tray_reassociation_persists_new_rack_no_position`, `::test_tray_device_resubmitted_as_existing_is_idempotent_noop`; `test_models.py::test_move_with_no_position_is_a_valid_tray_target`, `::test_add_with_no_position_is_a_valid_tray_target`, `::test_tray_target_in_other_site_rejected` | **NEW** |

Honest gaps carried forward from this pass (not silently dropped, listed
so a future pass has a punch list): displacement-onto-a-vacated-U landing
FROM a tray origin (mechanism is shared code, not independently
re-verified); homecoming-after-reload for a tray origin specifically
(mechanism is shared/state-agnostic, not independently re-verified);
discard-on-release-outside-any-legal-target for a tray-bound palette drag
(no dedicated regression, relies on GridStack's own `acceptWidgets` never
firing `dropped` off-grid). The cross-rack tray→tray DRAG gesture (initially
listed here as a gap) was escalated mid-pass by explicit user report and is
now CLOSED — see the T-tray-4 row above.

## Summary

- Spec rules enumerated (§4.1 incl. the 2026-07-08 hard-rule additions
  §4.1a/b/c with the palette context, §4.2–§4.8, I1–I3, full-world
  upgrade): **~56 rule×context rows** across the tables above (was ~54
  before the 2026-07-09 post-release hardening pass added the issue #17
  and #21 rows and the issue #22 full-world-diff-extension row).
- Previously covered (existing tests, unchanged): **~30** (incl. the
  one-occupant blocking mechanism and the legal palette-add pipeline,
  which were already correct).
- Newly covered across the three 2026-07-08 passes: **20** new/rewritten
  assertions — homecoming (exact-origin, near-miss, 3-hop chain,
  save+reload, single-entity invariant), the full-world diff upgrade,
  cursor-governed release-over-illegal snap-back (+ deny indicator),
  rename-dialog-on-every-cross-rack-commit (+ the sweep-wide pipeline
  assertion), second-device-blocked, transition-safe dialogs, palette
  discard-on-illegal-release (+ deny, + dirty hygiene), palette
  displacement under the armed tracker.
- Newly covered in the 2026-07-09 post-release hardening pass (issues
  #17/#21/#22): **6** new tests —
  `test_rename_dialog_cancel_button_fully_reverts_cross_rack_move`,
  `test_rename_dialog_close_x_fully_reverts_cross_rack_move` (issue #17,
  both green — the underlying `showMoveNameDialog` fix was already
  shipped; falsifiability verified by temporarily reproducing and
  re-fixing the exact old-pattern failure), `test_cross_rack_drop_onto_vacating_slot_displaces_confirm`,
  `test_cross_rack_drop_onto_vacating_slot_displaces_cancel` (issue #21,
  both green — closes the dedicated cross-rack displacement-on-adoption
  gap with a new full-depth rack-B ghost fixture), plus the full-world
  diff net wired into 4 existing tests across
  `EditorDensePackRejectTestCase`/`EditorHatchOverlapNoPushTestCase`/
  `EditorShadowOwnershipTestCase` (issue #22, 0 new violations found).
- Fixed: (1) the cross-rack homecoming 5-entity bug (duplicate entity +
  orphan shadow leak); (2) the cursor-fallback commit bug (device landing
  on rows the user never pointed at); (3) the skipped dialog pipeline for
  adopted move_in tiles; (4) the stranded-dialog Bootstrap transition bug;
  (5) the palette-fallback add bug (gray placeholder over impossible
  positions, add committed at the fallback slot) + the dirty-residue of
  discarded drag-ins.
- Honest gaps: none currently open from this pass's brief. The only
  remaining note is that `EditorDisplacementTestCase` was deliberately NOT
  upgraded to the full-world diff net (its short/few-step tests already
  assert on every relevant entity directly; see "Known gaps" above), and
  `EditorShadowOwnershipTestCase::test_conflict_shadow_rendered_and_reported`
  has no gesture to diff.
