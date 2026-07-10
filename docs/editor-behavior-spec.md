# Editor Behavior Specification — Device-as-Object Model

Status: **draft for review** (2026-07-07). This document defines the *intended* behavior
of the rack-design editor. It is the single source of truth from which the OOP refactor
and the test suite are derived. Code that disagrees with this document is wrong.

---

## 1. Why a new model

Today GridStack owns the layout and the editor reacts after the fact:
GridStack mutates the grid → we re-scan everything (`recomputeOpposites`) → we try to
detect and undo illegal outcomes (`tileOverlapsOther`, `cancelMove`). Every hard bug of
the last weeks is the same root cause wearing a different mask:

| Bug | Root cause |
|---|---|
| Cross-face drag lockup | reacting to GridStack events that fire differently across grid instances |
| Orphaned / wrong-name shadow | shadow is *derived* by a global re-scan, not *owned* by its device |
| `RangeError: Maximum call stack size exceeded` on dense rack | GridStack is allowed to "resolve" an impossible placement by pushing neighbors (`_fixCollisions ↔ moveNode` infinite recursion — confirmed live, stack is 100% vendor code) |
| Tiles "jumping" during drags | GridStack float-push moves *other* tiles as a side effect of a drag we never validated |

**Inversion this spec mandates:** the editor model decides whether a move is legal
*before* anything is committed. GridStack becomes a dumb rendering/drag surface. Its
collision resolution (push/float cascade) is never used to decide placement — which
eliminates the stack overflow *by construction*, not by a recursion cap.

---

## 2. Domain objects

### 2.1 `Device` (the tile)

A planned placement of one device. **One JS object per device, owning everything that
renders on its behalf** — including its opposite-face shadow. Nothing about a device is
ever reconstructed by scanning the DOM.

```
Device {
  // identity
  deviceId        // NetBox device PK (null for newly added, not-yet-saved)
  placementId     // plugin Placement PK (null until saved)
  label           // display name
  deviceTypeId, heightU, isFullDepth

  // position (authoritative — GridStack mirrors this, never the reverse)
  rackId, face ("front"|"rear"), uPosition   // 0.5U resolution

  // lifecycle state (matches backend ProjectedSlotState)
  state           // existing | add | move_in | move_out_ghost | remove

  // owned view parts
  bodyEl          // the GridStack widget on `face`
  shadow          // Shadow | null — exists iff isFullDepth
  originGhost     // Ghost | null — exists iff this device was moved and its
                  // origin slot is still shown as vacating
}
```

Methods (the contract, names indicative):

- `canPlaceAt(rack, face, u)` → `{ok, reason, displaces}` — pure check, **no mutation**.
  Checks the target units on `face` AND (if full-depth) the mirrored units on the
  opposite face. Returns which occupant would be displaced if the target is a
  vacating slot (see §4.3).
- `placeAt(rack, face, u)` — commits: updates own fields, moves `bodyEl`, moves
  `shadow` atomically, creates/updates `originGhost`.
- `revert()` — returns to last committed position; body, shadow, ghost all restored
  in one call (no global re-scan).
- `renderState()` — applies CSS classes for `state` to body + shadow + ghost.
- `destroy()` — removes body, shadow, ghost together.

### 2.2 `Shadow` (part of the Device, never independent)

The opposite-face projection of a full-depth device. It has **no lifecycle of its
own**: it is created when its Device is created (if full-depth), moves in the same
call that moves the body, and is destroyed with the Device. It is never produced by a
global "recompute" pass.

Rendering follows the owner's state (see table §3). A Shadow is **non-interactive**
(not draggable, not a drop target for its own device) but it **participates in
occupancy**: other devices' `canPlaceAt` must see it.

**Live mid-drag tracking (confirmed requirement, 2026-07-07):** while the user is
dragging a full-depth device (grabbed, not yet released), its shadow follows the
body in real time on the opposite face — sliding U-by-U with the cursor. This is
both feedback ("the rear half moves with me") and a live legality preview (the
user sees a rear-side conflict before dropping). The shadow must never lag until
drop/redraw.

### 2.3 `Unit` (slot)

One 0.5U row on one face of one rack. The unit of occupancy accounting.

```
Unit { rackId, face, row }   // row in 0.5U grid coordinates
```

- `claims()` → list of `{device, kind}` where kind ∈ `body | shadow | ghost`.
- `blockingClaimFor(device)` → the claim that forbids placement, or null.
  Blocking rules in §4.2.

Units are how hover-validation works: while dragging, the target units under the
cursor are asked *before* any drop is accepted, and the drop indicator shows
allowed/denied accordingly.

### 2.4 `Ghost` (origin reservation of a moved device)

When a Device moves away from a slot that still physically contains it (state
`move_out_ghost` at origin), the origin slot shows a Ghost. Semantics: **"this space
is being vacated — you may plan into it, but the hardware is still there today."**

A device is removed from a slot for exactly two reasons, and the Ghost must serve both:

1. **Free the unit** — someone will later place a different device there.
2. **Reuse** — the same physical device is being reinstalled elsewhere (the move).

Therefore a Ghost is *plannable-over* (it does not block placement) but *visible*
(the planner must see the hardware hasn't left yet).

### 2.5 `Rack` / `Face`

`Rack` owns two `Face`s + tray, its Devices, and its Units. `Face` wraps one
GridStack instance purely as a view. All validation questions go through
`Rack`/`Unit`, never through `grid.getGridItems()` inspection at decision time.

---

## 3. Rendering table (state × part)

| Device state | Body (own face) | Shadow (opposite face) | Origin slot |
|---|---|---|---|
| `existing` | solid, existing style | hatched "occupied (full-depth)" | — |
| `add` | add style (green) | hatched, add-tinted | — |
| `move_in` (arrived here) | move style (blue) | hatched, move-tinted | see `move_out_ghost` row at origin |
| `move_out_ghost` (origin marker) | **crossed-out / struck-through hatch** — clearly "leaving" | **also crossed-out hatch** — ⚠ today it renders like a normal live device (red), which is wrong | n/a (this *is* the origin) |
| `remove` | remove style (red, struck) | crossed-out hatch | — |
| **`displaced`** (new, see §4.3) | not rendered as a full tile — replaced by **side reservation stripe** | side stripe on opposite face too (full-depth) | — |

**Side reservation stripe (`displaced`):** rendered like NetBox core's rack
reservation marker — a narrow vertical bar spanning exactly the displaced
units — colored **red**. Geometry (user ruling 2026-07-09): the bar renders
**OUTSIDE the rack frame**, hanging off the elevation's RIGHT edge (exactly
how core draws reservations alongside the elevation), never inside the
occupying tile (the earlier in-tile sliver sat cramped against the tile's ×
remove button). Front-face displacement bars hang off the front elevation; a
full-depth OLD's mirror bar hangs off the rear elevation. Hover/tooltip shows
the displaced device's name ("was: `dra4-sl-isp26`"). The new device
occupying the slot renders with its own movement/add style at full width.
This is the picture Petr provided: NetBox reservation look, red, old name on
hover.

The displaced treatment applies to EVERY projection render, not only the
editor's live session (parity ruling 2026-07-09): the projection layer marks
a vacating slot whose rows are occupied by a live planned slot as
`displaced` (+ `displaced_by`), the read-only elevation renders it as the
stripe bar server-side, and the editor applies the same collapse+bar on LOAD
from that marking — a saved displacement never renders as two composited
full tiles anywhere.

**Tile label = assigned name (user ruling 2026-07-10):** once a placement
carries a `proposed_name` (auto-filled by the naming engine, typed into an
add's inline field, or chosen in the §4a rename dialog), the tile's VISIBLE
label shows that name — falling back to the device-type model (adds) or the
device's real name (moves) only while no name exists. Implementation note:
the visible name is a separate display span layered over the stable
`.nbx-rd-label` identity span, which is never rewritten (it anchors ghost
pairing, the read-model and the test harnesses); ghost (origin) tiles keep
showing the physical device's real name.

**Hover card = identity story (user ruling 2026-07-10):** hovering a
`move_in` tile (or a renamed add) shows the full picture — the plan's new
name, the device's real dcim name ("Was"), old tenant, type, role, and the
target rack/U ("To"). Hovering a ghost shows where the device WENT (new name
+ destination rack/U, resolvable from the paired move placement for saved
moves). Applies to the editor and the read-only elevation alike (both hover
cards read the same `data-*` attributes).

**Ghost ↔ body hover link (user ruling 2026-07-10):** hovering a `move_in`
body highlights its origin ghost and vice versa (`.nbx-rd-hover-linked`
outline/glow) — same-rack, cross-rack and tray ghosts alike, paired by
device identity (`data-rd-device-id`), cleared on mouse-leave.

Legend filters (`Existing / Add / Move in / Move out (ghost) / Remove`) apply
uniformly to bodies, shadows, ghosts and stripes of the corresponding state.

---

## 4. Movement rules

### 4.1 The pipeline (validate → confirm → commit)

Every placement gesture (drag of an existing tile, drop from palette) follows:

```
dragover  : target Units asked canPlaceAt → live allow/deny indicator
drop      : canPlaceAt re-checked (authoritative)
            ├─ not ok  → revert() — tile snaps back, nothing else moved, no dialog
            └─ ok      → if user decision needed (name reuse, displacement) → dialog
                          ├─ cancel → revert()
                          └─ confirm → placeAt() commits model, then syncs GridStack
```

Hard rules:

- **No GridStack push.** During any gesture, no other tile may change position as a
  side effect. GridStack collision resolution is disabled/neutralized; the model is
  the only authority. (This is what kills the `RangeError` and the "things jump
  around" class of bugs.)
- **Cursor-governed placement (Petr's ruling, 2026-07-08).** The drag preview
  follows the CURSOR's target rows only — there is no "suggested placement":
  the placeholder must never relocate to a different (last-valid) slot while the
  cursor hovers an illegal one, and the commit position is always the cursor's
  rows, never a fallback. Cursor over legal rows → preview renders there (allow
  style). Cursor over illegal rows → deny indicator at the cursor rows, no
  placeholder anywhere else, and release = full snap-back home (§4.7). A device
  must never land on rows the user was not pointing at.
- **One occupant per vacated slot.** A ghost/removed slot accepts exactly ONE
  incoming planned device (§4.3): once NEW occupies it, NEW's live body claim
  blocks all further placements. No stacking of plans into one vacated unit.
- **Dialogs come after validation.** A dialog is only shown for a placement that has
  already passed `canPlaceAt`. Never dialog-then-discover-invalid.
- **Commit is atomic.** Body + shadow + origin ghost move in one model call. There is
  no window where the DOM shows a half-moved device.

### 4.2 Blocking rules (`Unit.blockingClaimFor`)

For device D targeting a unit range (on D's face, plus mirrored range on the opposite
face when D is full-depth):

| Claim present in target units | Blocks D? |
|---|---|
| `body` of a live device (`existing`/`add`/`move_in`) | **yes** — reject before any mutation |
| `shadow` of a live full-depth device | **yes** |
| `ghost` (origin of a moved device) | **no** — allowed; triggers displacement flow §4.3 |
| `body`/`shadow` of a `remove`-flagged device | **no** — allowed; triggers displacement flow §4.3 |
| stripe of an already-`displaced` device | **no** — the physical occupant is already accounted for; stripe remains |
| D's own shadow / own ghost | **no** (moving within your own footprint is legal) |

### 4.3 Placing onto a vacating slot (ghost or remove-flagged) — displacement

This is the case Petr has restated many times; spelled out once and for all:

Given: device OLD occupies units physically; in the plan it is leaving (moved away →
ghost at origin, or flagged `remove`). Device NEW is dropped onto those units.

Expected outcome:

1. Placement is **allowed** (passes validation).
2. **NEW renders normally** in the slot with its own semantic style: `move_in` if it
   was moved there, `add` if it came from the palette. It does NOT inherit any ghost
   styling.
3. **OLD collapses to the side reservation stripe** (state `displaced`): red vertical
   bar at the right edge of those units, NetBox-reservation look. Hover shows OLD's
   name. If OLD is full-depth, the mirrored units on the opposite face get the stripe
   too (replacing OLD's crossed-out shadow there).
4. **Confirmation dialog on every displacement** — but strictly *after* validation
   has passed (never dialog-then-discover-invalid):
   - "Units X–Y are occupied by **OLD** (being removed / being moved). Place **NEW**
     here?"
   - When OLD is leaving-to-free-and-reuse-the-name (rename workflow): the same
     dialog additionally offers name reuse per the naming-convention feature.
   - Cancel → full `revert()`.
5. Undoing NEW's placement (moving NEW away again, or cancel) **restores OLD's ghost /
   remove rendering** — the stripe exists only while something else occupies the slot.

### 4.4 Move within one face

- D `placeAt` new units → origin gets Ghost (crossed-out body-style + crossed-out
  shadow per §3), destination shows D as `move_in`.
- Moving D back onto its own origin ghost = plain revert: ghost disappears, D returns
  to `existing` (or its prior state). No dialog.

### 4.5 Cross-face move (front ↔ rear, same rack)

- Full-depth D: face flip means body and shadow swap faces. Atomic in one commit.
- Origin ghost stays on the *original* face (+ its crossed shadow on the opposite).
- Non-full-depth D: simple; only origin ghost on the source face.

### 4.6 Cross-rack move

- Same as 4.4/4.5 but origin Rack keeps the Ghost, destination Rack gains D.
- Moving D back to its origin rack+units later must fully clear the Ghost and restore
  the original name/state — no stale "wrong name" shadow (bug #11). This falls out of
  ownership: the Ghost is D's `originGhost`, so when D returns, D destroys it. There
  is nothing to re-derive and therefore nothing to derive *wrongly*.

### 4.7 Rejected placement

- Target blocked (live body/shadow): tile snaps back to its exact prior position.
  **Zero other tiles move.** No dialog, no console error, no residue (shadows/ghosts
  unchanged) — on any rack density. (Regression: isp26 → U2 on the packed 46U rack.)

### 4.8 Palette add

- Same pipeline. Drop from palette creates a Device in state `add` (with Shadow if
  the device type is full-depth) only after validation passes. Dropping onto a
  vacating slot follows §4.3 with NEW.state = `add`.

---

## 5. What GridStack is still allowed to do

- Render tiles, provide the drag gesture and pixel↔row math.
- Fire `dragstart/dragover/drop`-level events that we translate into model calls.
- **Not allowed:** float-push of neighbors, cross-grid auto-adoption decisions,
  being the source of truth for position. `acceptWidgets`/collision hooks are
  configured so GridStack always defers to the model's verdict.

The `_fixCollisions` recursion guard added on 2026-07-07 stays **temporarily** as a
vendor-level backstop (it is inert unless the cascade fires) and is deleted in the
phase that disables GridStack pushing entirely.

---

## 6. Test scenarios (derived 1:1 from §4)

Unit-model tests (pure JS, no browser — become possible only with the OOP model):

- U1. `canPlaceAt` truth table of §4.2 (each claim kind × full-depth yes/no).
- U2. Atomicity: `placeAt` leaves body/shadow/ghost consistent after every call.
- U3. `revert()` restores the pre-gesture snapshot exactly.

E2E scenarios (deterministic, self-provisioning, per existing sweep harness):

- E1. Move within face → ghost + crossed shadow at origin; destination `move_in`. (§4.4)
- E2. Move back onto own ghost → everything restored, no dialog. (§4.4)
- E3. Cross-face full-depth move → body+shadow swap faces atomically; origin ghost
  on source face; no lockup. (§4.5)
- E4. Cross-rack move and return → no stale ghost, correct name. (§4.6, bug #11)
- E5. Drop NEW onto ghost slot → dialog → confirm → NEW styled `move_in`/`add`,
  OLD = red side stripe, hover shows OLD name; opposite face striped when OLD is
  full-depth. (§4.3)
- E6. E5 then cancel at the dialog → full revert, ghost rendering restored. (§4.3.5)
- E7. Move NEW away from a displaced slot → OLD's ghost rendering returns. (§4.3.5)
- E8. Drop onto live-occupied units on a **fully packed rack** → snap-back, zero
  other tiles moved, zero console errors. (§4.7 — the isp26→U2 crash)
- E9. Legend filter toggles hide/show ghosts, shadows and stripes consistently. (§3)
- E10. 0.5U sweep suites (existing `test_editor_sweep.py`, `test_editor_add_sweep.py`)
  re-based on the invariants above, alternating front/rear across both racks.
- E11. Palette add onto vacating slot = §4.3 with `add` styling. (§4.8)

Every scenario asserts the same three global invariants after each step:
**(I1)** no two live claims overlap on any Unit; **(I2)** every full-depth device has
exactly one shadow, on the opposite face, at its own units; **(I3)** console is free
of errors.

---

## 7. Migration plan (incremental, each phase shippable & testable)

1. **Phase 0 (done):** recursion guard backstop; this spec.
2. **Phase 1 — model without behavior change:** introduce `Device/Shadow/Unit/Rack`
   classes populated from current data; keep existing event flow; add invariant
   assertions (I1–I3) behind a debug flag. Sweeps must stay green.
3. **Phase 2 — validate-before-commit:** route all drops/drags through
   `canPlaceAt`/`placeAt`; disable GridStack push (neutralize `_fixCollisions` path);
   delete `freezeOthers` complexity where obsoleted. E8 turns green by construction.
4. **Phase 3 — owned shadows/ghosts:** shadows and ghosts created/moved/destroyed by
   their Device; delete `recomputeOpposites` global scan. E1–E4 green; bug #11 dies here.
5. **Phase 4 — displacement UX:** `displaced` state, side stripe rendering + hover,
   confirmation dialog per §4.3. E5–E7, E11 green. Remove the Phase-0 guard.

---

## 8. Decisions (confirmed by Petr, 2026-07-07)

1. §4.3.4 — dialog on **every** displacement, always after validation passes.
2. §3 stripe — **NetBox reservation hatch recolored red** (same diagonal-stripe
   pattern as core rack reservations).
3. §4.4 — moving a device back onto its own ghost restores **silently**, no dialog.

---

## 9. Non-racked tray (planned — 0.9.0)

Status: **spec draft 2026-07-09** (user request: real off-rack devices — 0U/vertical
PDUs, rear-door units, cable managers — must be visible and plannable).

### 9.1 What the tray represents

Each rack's tray is the projection of "devices associated with this rack but not
mounted at a U": in DCIM terms, `Device.rack == R and Device.position is None`.
Today only *planned* position-less placements render there; real position-less
devices are invisible. 0.9.0 makes the tray show reality plus plan, exactly like
the faces do.

### 9.2 Model

- A tray slot is a `Device` with `face = ""`/`u = None`; it claims **no Units**.
  Tray claims never collide (a tray is an unordered list, not a grid) and cast
  **no shadow** (there is no opposite face off-rack).
- `RDRack.trayDevices` (already present in the read-model) becomes fully
  populated: `existing` tray devices from DCIM + planned tray placements, each a
  normal `RDDevice` with `face: "tray"`-equivalent semantics.
- Invariants: I1/I2 exclude tray devices (no rows, no shadow); new **I4**: a
  device appears at most once per design world (body in units XOR tray XOR
  ghost-origin pair) — the §4.6 one-entity rule extended to the tray.

### 9.3 Moves (all reuse the §4.1 pipeline: validate → dialog → atomic commit)

| Gesture | Meaning | Rules |
|---|---|---|
| units → tray (same or other rack) | plan a dismount-to-0U / accessory reassignment | origin gets a ghost + crossed mirror per §3; tray entry renders `move_in`; rename dialog per naming feature |
| tray → units | plan a mount at a U | full §4.2 blocking rules + §4.3 displacement apply at the target; full-depth devices gain their shadow on landing |
| tray → tray (cross-rack) | reassociate with another rack | origin tray keeps a ghost entry (list-style, no rows); dialog per cross-rack move |
| back onto own tray ghost | homecoming | silent restore per §4.4/§4.6 — identity-based, any hop count, survives save+reload |
| palette → tray | plan a new off-rack device | `add` styling; cursor-governed (§4.1): the tray highlights as the legal target under the cursor |

- Cursor governance applies: the tray is a legal target only when the cursor is
  over it; a release elsewhere snaps back / discards per §4.1.
- Displacement (§4.3) does not apply inside the tray (no exclusive slots), so
  tray drops never displace and never dialog for displacement — only the rename
  dialog fires where naming requires it.

### 9.4 Rendering

- `existing` tray devices: normal tile styling, laid out as a horizontal list.
- Planned states reuse the §3 table (add/move_in/move_out_ghost/remove) minus
  shadows/stripes (n/a off-rack).
- Legend filters apply to tray tiles the same as to face tiles.
- The tray is a compact list: rows renumber to contiguous after any removal;
  the §4.1 no-bystander-movement rule constrains rack positions (U), not list
  reflow (coordinator-approved interpretation, 2026-07-09).

### 9.5 Save contract

- Mount (tray → U): placement gains `target_position`/`target_face` as usual.
- Dismount (U → tray): placement with `target_rack = R`, `target_position = None`.
- Reassociation (tray → tray): move placement with the new rack, no position.
- Server validation mirrors §4.2 for unit targets; tray targets validate only
  same-site rack membership (no slot availability applies).

### 9.6 Tests (derive per the conformance-matrix discipline, test-first)

- T-tray-1: real 0U device renders in the tray as `existing` on load.
- T-tray-2..5: each row of the §9.3 table, confirm + cancel variants, full-world
  diff per step, homecoming contract for the return legs.
- T-tray-6: palette add into tray; discard on release outside any legal target.
- T-tray-7: I4 holds across a units→tray→units round-trip (single entity).
