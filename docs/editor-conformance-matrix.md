# Editor spec-conformance matrix

Cross-reference of every behavioral rule in `docs/editor-behavior-spec.md` §4
(movement rules) against the contexts it applies in, and the test(s) that
prove it. Built 2026-07-08 alongside the fix for the confirmed live
cross-rack "homecoming" bug (§4.6). Status legend:

- **covered** — a deterministic e2e test exercises this exact rule in this
  exact context and was GREEN at the time this matrix was written.
- **NEW** — did not exist before this pass; written and verified as part of
  this work (falsifiability where noted: failed on pre-fix code, passes
  after the fix).
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
| Displacement via cross-rack adoption | cross-rack | dialog + stripe semantics identical to same-rack | none dedicated | NEW-gap (not written this pass — see "Known gaps" below) |

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

## Known gaps (honest accounting, not silently left uncovered)

- **Displacement via a cross-rack adoption** (dropping a foreign tile onto a
  ghost/remove-flagged slot in the destination rack): since the 2026-07-08
  §4.1b fix (adopted move_in tiles now run the full displacement + rename
  pipeline), the cross-rack sweep exercises this whenever its subject
  crosses rack B's ghost rows and the `_sweep` dialog-pipeline assertion
  requires the dialogs to fire on every committed crossing. A dedicated
  targeted test (moveTo onto `_bghost_orig_gsy`, asserting the stripe on
  the destination ghost specifically) is still a recommended follow-up.
- ~~Cursor governance for palette drag-ins~~ **CLOSED (2026-07-08, palette
  pass)**: the tracker now arms at pointer-down on the palette item; a
  release over illegal cursor rows discards the drag-in entirely (no add,
  no dirty residue) with the deny indicator shown mid-drag. See the §4.1a
  palette rows (`test_palette_release_over_occupied_rows_creates_no_add`,
  `test_palette_cursor_release_on_vacated_slot_fires_displacement`).
- **EditorDisplacementTestCase / EditorShadowOwnershipTestCase /
  EditorHatchOverlapNoPushTestCase** were re-run under this work (all green)
  but their per-step assertions were NOT upgraded to the new full-world
  diff net — that upgrade was applied to the two long position sweeps
  (`EditorSweepTestCase`, `EditorCrossRackSweepTestCase`), which are the
  ones that actually iterate many steps and are where a scoped check
  provably missed a bystander drift. The short, few-step displacement/
  shadow-ownership tests already assert on every relevant entity directly
  (stripe, mirror, dialog) rather than sampling one subject across dozens
  of steps, so the marginal value of the full-world diff there is lower;
  flagged here rather than silently claimed as upgraded.

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

## Summary

- Spec rules enumerated (§4.1 incl. the 2026-07-08 hard-rule additions
  §4.1a/b/c with the palette context, §4.2–§4.8, I1–I3, full-world
  upgrade): **~54 rule×context rows** across the tables above.
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
- Fixed: (1) the cross-rack homecoming 5-entity bug (duplicate entity +
  orphan shadow leak); (2) the cursor-fallback commit bug (device landing
  on rows the user never pointed at); (3) the skipped dialog pipeline for
  adopted move_in tiles; (4) the stranded-dialog Bootstrap transition bug;
  (5) the palette-fallback add bug (gray placeholder over impossible
  positions, add committed at the fallback slot) + the dirty-residue of
  discarded drag-ins.
- Honest gaps: a dedicated targeted displacement-on-adoption test;
  full-world diff not extended to the 3 short targeted classes — both
  documented above rather than silently claimed as done.
