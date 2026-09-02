# Editor Behavior Specification — Device-as-Object Model

Status: **shipped** — the OOP migration described in §7 (Phases 1–4) is complete. This
document defines the *intended* behavior of the rack-design editor and remains the single
source of truth against which the implementation and test suite are checked. Code that
disagrees with this document is wrong.

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

### 2.6 `Frame` / `Container` (the generalisation — 0.20.0)

A rack is not the only enclosure with slots: a chassis holds blades in bays, and
a patch-panel frame holds modules in the same way. `Rack`/`Face` is therefore the
*rack instance* of a two-level abstraction, and the editor, the save view and the
projection are written against that abstraction rather than against units:

```
Frame          one enclosure         owns Containers, and everything that spans them
Container      one addressable grid of slots
```

| | rack Frame | chassis Frame |
|---|---|---|
| containers | `front`, `rear` | `bays` — exactly one |
| tray (§9.2) | yes | none |
| pairing rule | a full-depth device claims the same slot in **both** containers | none — one container has no opposite |
| step | 0.5U | one bay |
| address | `{u_position, face}` | `{target_bay_id}`, or `{target_bay_name, parent_placement_id\|parent_ref}` |

A Container answers exactly three questions, and nothing else in the pipeline is
allowed to ask them another way:

- `slotFromGeometry(y, h)` / `slotToGeometry(slot)` — grid geometry ↔ slot index,
  which is where the 0.5U-vs-one-bay step lives;
- `addressForSlot(slot)` — the save address for that slot;
- `accepts(el)` — the drop gate (rack-mountable vs child).

**Why this is a rule and not a refactoring note.** The chassis layer previously
translated a bay payload back out of `u_position` after the payload was built. A
translation pass can *discard* an item it cannot resolve, and it did: a cancelled
planned blade carried no position, so its cancel never reached the server while
Save reported success. The rule that prevents the whole class of bug: **an item
is addressed once, by its own Container, when it is built** — there is no later
pass that could drop it. A chassis Frame declares one container and no pairing
rule, so the rack-only machinery is *absent* rather than suppressed, and the two
layers cannot drift.

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
| **`inherited`** (flag, not a state — design chains, §12) | renders as its underlying state (almost always `existing`) with a distinct dimmed/outlined treatment | follows the body's state per the table above | — |
| **`conflict`** (flag, not a state — design chains §12) | tile keeps its normal state styling AND gains an amber conflict marker (stripe-bar geometry identical to `displaced`'s red stripe, but amber — red stays reserved for displacement) | mirrored on the opposite face for a full-depth device | — |

`inherited` and `conflict` are **flags layered on top of a state**, exactly
like `displaced` already was, not new `ProjectedSlotState` members (§12.4) —
so every row of this table (`existing`/`add`/`move_in`/`move_out_ghost`/
`remove`) may additionally carry either flag, and the legend gives each flag
its own checkbox (§12.6) rather than doubling the state count.

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

**An upstream (design-chain) conflict follows NEITHER this path nor §4.7's.**
An ancestor design occupying your target unit, or vacating a slot you built
on, is not a live claim your OWN gesture is contending with — it is a
standing disagreement between your design and a design you cannot edit from
here. So there is no dialog, no snap-back, and Save is never blocked: the
tile keeps rendering at the position you gave it, gains the amber `conflict`
flag (§3, §12), and the condition is reported in the editor's persistent
conflicts panel until the design is re-based. See §12.

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
- **A PLANNED ADD crosses Frames too**, and is the simplest case there is: no
  hardware stays behind, so there is no Ghost, no homecoming and no §4a rename —
  the planned placement simply names a different Frame, keeping the role, tenant,
  full-depth flag and the name the user typed. It is one gesture at both levels
  (§2.6): rack → rack, and chassis column → chassis column for a planned blade,
  whose address is re-derived by the DESTINATION Container. Refusing a
  device-less tile at the destination's drop gate is what made this look broken —
  the tile snapped back with nothing logged (user 2026-08-27).
- Moving D back to its origin rack+units later must fully clear the Ghost and restore
  the original name/state — no stale "wrong name" shadow (bug #11). This falls out of
  ownership: the Ghost is D's `originGhost`, so when D returns, D destroys it. There
  is nothing to re-derive and therefore nothing to derive *wrongly*.

### 4.7 Rejected placement

- Target blocked (live body/shadow): tile snaps back to its exact prior position.
  **Zero other tiles move.** No dialog, no console error, no residue (shadows/ghosts
  unchanged) — on any rack density. (Regression: isp26 → U2 on the packed 46U rack.)

**An upstream (design-chain) conflict is never rejected this way either.** A
hard collision here means YOUR gesture just tried to claim a unit a live
claim already blocks, and the server would refuse the save the same way — so
snap-back is correct: there is nothing to commit. An upstream conflict is the
opposite shape: the placement was already valid when made, and only became
disputed because an ancestor design's layer changed underneath it. Rejecting
it on every render would make it impossible to ever look at the tile you are
supposed to be fixing. See §4.3 and §12.

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

The `_fixCollisions` recursion guard added on 2026-07-07 is retained as the
push-suppression mechanism (`rdPushSuppressDepth`, a depth counter, not a bool):
it is the load-bearing way the editor tells GridStack to defer to the model's
verdict instead of auto-pushing neighbors, and has been kept and extended rather
than deleted.

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

---

## 10. Device bays / chassis (shipped — 0.19.0, unified 0.20.0)

Status: **shipped**. 0.19.0 delivered the chassis layer alongside the rack
editor; 0.20.0 made it the *same* code — a chassis is a Frame with one Container
(§2.6), so it is not a rack-shaped special case with a translation layer but the
degenerate case of the general one. Read §2.6 first; this section then says only
what is bay-specific.

### 10.1 What a bay represents

DCIM forbids a child device a rack position **and** a face
(`dcim.Device.clean()`), so a blade is never *at* a U — it is *in* a bay of its
parent. A bay is therefore a **slot owned by a Device**, exactly as a Unit is a
slot owned by a Face. That symmetry is the whole design: everything §4 says
about validate → confirm → commit, cursor governance and homecoming applies
unchanged; only the container differs.

Two kinds of parent, both required:

| Parent | Bays come from | Blade targets |
|---|---|---|
| a real chassis in DCIM | its `dcim.DeviceBay` rows | `target_bay` (the bay's pk) |
| a chassis this design adds | the type's `DeviceBayTemplates` (no rows exist yet) | `parent_placement` + `target_bay_name` |

### 10.2 Model

- **`Bay`** — new domain object, owned by a Device (`spec §2.1` gains
  `bays: Bay[]`, empty for a non-parent type):
  `{ parent: Device, name, occupant: Device|null, state, el }`.
  A Bay is the bay-side twin of `Unit` (§2.3): the thing a placement competes
  for. Unlike a Unit it is **named, not numbered**, and there is exactly one
  occupant — no row range, so no partial overlap and no displacement.
- **`Device`** gains `bay: Bay|null` — the bay it occupies, null for a racked or
  tray device. A blade has `y = rows = null` and `face = ""`.
- A blade **claims no Units** and **casts no Shadow** (§2.2): it is inside its
  parent's envelope, which already claims those rows. The parent's shadow covers
  it. I1/I2 therefore exclude blades, exactly as they exclude tray devices.
- **I5 (new):** a Bay holds at most one occupant, and a Device occupies at most
  one Bay. Combined with I4 (§9.2) a device is in exactly one of: units, tray,
  or a bay.

### 10.3 The chassis layer (how bays are edited)

**Rejected: editing bays inside the chassis tile.** Tried and discarded
2026-08-25 after seeing it live — an 8-bay chassis in a 3U tile renders six
cramped cells with unreadable names, and it only gets worse with an 18-bay
chassis. There is no tile size at which a rack elevation can also be a bay
elevation.

**The model instead: a chassis is a Frame with one Container** (§2.6). §10.1 says
a Bay is to a Device what a Unit is to a Face; §2.6 is that symmetry made
literal, so *every* rule in §4 applies verbatim — validate → confirm → commit,
blocking claims, displacement, ghosts, homecoming, cursor governance. **No new
gesture code exists**, and none may be added: a difference between the two layers
belongs in the Container's three answers (step, address, accept gate) or it is a
bug. Every property that separates them is in the §2.6 table.

- **Rack view**: a chassis is an ordinary tile. No bay strip. Its hover card
  gains `N of M bays used` and the occupant names, so the rack view still
  *answers* the capacity question without trying to *edit* it.
- **Chassis layer**: a view toggle, present only when the design's scope contains
  at least one parent device. Switching to it replaces the rack elevations with
  chassis elevations — each chassis a column, its bays numbered 1..N — and the
  palette filters itself to child device types.
- Entering the layer from a specific chassis (clicking its tile) scrolls to that
  chassis; the layer always shows every chassis in scope, so a blade can be
  dragged from one chassis to another exactly as it can be dragged between racks
  (§4.6).

Gestures are therefore NOT re-specified here: read §4. The mapping is
`Unit -> Bay`, `Face -> the chassis's single bay column`, `Rack -> Chassis`.
Two consequences follow from a bay being single-occupancy rather than a row
range:

- a drop onto an occupied bay is **rejected**, never displaced (§4.3 does not
  apply — there is no partial overlap);
- a chassis column has no opposite face, so blades cast **no shadow** (§2.2) and
  I1/I2 exclude them, as they exclude tray devices.

Not offered: **bay → rack unit** and **rack unit → bay**. A child type may not be
racked and a non-child may not be baid; the model rejects both
(`DesignPlacement._validate_bay_target`), so neither view ever presents the other
as a legal target.

### 10.4 Rendering

**Rack view** — a chassis is a normal tile with normal state colouring. Its hover
card adds:

- `N of M bays used`;
- the occupant names (planned ones in their §3 state styling), so the rack view
  answers "what is in there / is there room" without becoming an editor.

**Chassis layer** — chassis laid out side by side, each a single column of bays
numbered 1..N with the bay name as the row label. Tiles reuse the §3 state table
(`existing` / `add` / `move_in` / `move_out_ghost` / `remove`) unchanged; there
are no shadows and no full-depth hatching (a chassis column has no opposite
face). Legend filters apply as in the rack view.

The toggle is hidden entirely when the design's scope contains no parent device,
so a deployment with no blade hardware never sees the feature.

### 10.5 Power

A chassis and its blades must never both be counted. Core cannot double-count
because it only reaches a blade *through* the chassis's power port
(`PowerPort.get_power_draw()` aggregates downstream only when the port has no
value of its own). A plan has no cables, so the same result is derived from
containment instead:

- chassis has a resolvable draw → **it wins**; blades are annotated
  `draw_included_in_parent` and add nothing;
- chassis has none → **blades roll up** into the chassis's figure.

A blade flagged `remove` stops drawing, as any removal does.

### 10.6 Save contract

Blades cannot ride a face bucket (no position, no face), so the rack payload
gains a fourth bucket, `bays`, processed **after** `front`/`rear`/`other`:

- real chassis → item carries `target_bay_id` (and `target_bay_name` is mirrored
  from the bay server-side);
- planned chassis → the chassis item carries a client-side `ref`, the blade item
  carries the matching `parent_ref` plus `target_bay_name`. The view reconciles
  the face buckets first, builds `ref → placement`, then resolves the bay items.
  Neither `ref` nor `parent_ref` is persisted — the chassis has no placement id
  until the same save creates it, which is the only reason they exist.
- cancel of a planned blade deletes its placement, and must flag the write or
  save-layout answers `304 Not Modified`.

### 10.7 Tests (derive per the conformance-matrix discipline, test-first)

- T-bay-1: a blade in a chassis bay never renders as a tray slot. **(done)**
- T-bay-2: a parent Device's slot exposes its bays, occupied and empty. **(done)**
- T-bay-3: a blade planned into a real bay renders in that bay. **(done)**
- T-bay-4: a blade planned into a *planned* chassis renders in its templated
  bays. **(done)**
- T-bay-5: model validation for every rule in §10.2/§10.3. **(done)**
- T-bay-6: save-layout round-trip for both parent kinds, incl. unknown
  `parent_ref` and cancel. **(done)**
- T-bay-7: power — chassis-wins, blade-roll-up, planned blade counted, removal
  stops drawing. **(done)**
- T-bay-8: palette -> bay commits, the rack grid never accepts a blade, and a
  planned chassis offers its bays. **(done)**
- T-bay-9..: the chassis layer — the §4 scenarios re-run with `Unit -> Bay`:
  add, cancel of a saved planned blade, move between bays (idempotency snapshot),
  a freed bay accepting a replacement. **(done)**. Ghost + homecoming for blades
  is still out of scope (§10.8).
- T-bay-10: the rack view's chassis hover card reports occupancy and names, and
  the read-only elevation reports them too. **(done)**
- T-bay-11: the chassis-layer toggle is absent when the design has no parent
  device in scope, and a parent type with **no bays at all** is not offered as a
  chassis. **(done)**
- T-bay-12: every chassis grid accepts a *rendered* blade tile — the drop gate
  must decide by containment, not by a palette-only DOM marker. The cross-frame
  e2e coverage drives moves through a JS shim that bypasses `acceptWidgets`
  entirely, so this is asserted at the gate itself. **(done)**

### 10.8 Still out of scope

Blade homecoming and the blade rename field. Bay → bay reseat across different
chassis left this list on 2026-08-27 for a PLANNED blade, which now travels
between columns like any other planned add (§4.6); moving a REAL blade between
chassis still is not offered, because that needs the ghost/homecoming machinery
a device-less tile does without. Both remaining items are cheaper after §2.6 than
before it, which was part of the point.

## 11. Palette favorites (named sets — Unreleased)

### 11.1 What a set is

A **favorite set** is a named list of device types belonging to ONE user. People
plan in modes — a server build and a network build pull different hardware — and
one flat list meant re-starring on every switch (user request 2026-08-28).

- A user has any number of sets; `(user, name)` is unique, and only per user, so
  two people may each keep a "for server".
- `Default` is the set a user starts with. It is not privileged: it can be
  renamed or deleted like any other, and is re-created empty if the user ends up
  with none, so the editor always has a set to work in.
- Membership is per set: the same device type may be starred in several sets at
  once. Uniqueness is `(favorite_set, device_type)`, NOT `(user, device_type)`.
- Deleting a set deletes its stars. It never touches the device types.

### 11.2 Model

`FavoriteSet(user, name, created)` and `FavoriteDeviceType(user, favorite_set,
device_type, created)`. Both are plain `django.db.models.Model`, never
`NetBoxModel`: starring is a personal UI preference and must not write
ObjectChange rows, index for search, or carry custom fields/tags.

`user` stays on `FavoriteDeviceType` alongside the set so the user-scoped API can
filter by the requesting user without a join.

### 11.3 API

| Endpoint | Does |
|---|---|
| `GET /favorite-sets/` | the user's sets, default first, each with `device_type_ids`; provisions the default on first read |
| `POST /favorite-sets/` | create (400 on a duplicate name, case-insensitive, or a blank one) |
| `PATCH /favorite-sets/<id>/` | rename |
| `DELETE /favorite-sets/<id>/` | delete the set and its stars; reports `favorites_removed` |
| `GET /favorite-device-types/?set_id=` | that set's ids (default set when omitted) |
| `POST /favorite-device-types/toggle/` | body `{device_type_id, set_id?}` |

Every query is filtered by `request.user` and the client never names a user. A
`set_id` that is not the caller's own resolves to their default rather than
404-ing: set ids are UI state that goes stale (the set was deleted in another
tab), and the safe reading of a stale id is "no set chosen".

### 11.4 UI

The Quick-access panel's header carries the set `<select>` (each option showing
its member count) plus new / rename / delete. The selection is remembered in
`localStorage` — it is browser-local UI state, not design data. The catalog
stars read and write the selected set, and their tooltip names it ("Star (add to
for network)"), so it is always clear which list is being changed.

The `<select>` carries `no-ts` so NetBox's TomSelect enhancement leaves it alone:
its options are rebuilt after every set change, which a TomSelect wrapper would
not pick up.

## 12. Design chains: inherited & conflict flags (shipped)

Status: **shipped**. See `docs/design-chains.md` for the user-facing workflow
and `PLAN-design-chains.md` for the design record; this section covers only
what changes in the rendering/legend contract of §3 and §4.

### 12.1 What the two flags mean

A design may baseline on an approved ancestor (`Design.based_on`). The
ancestor's placements are replayed into this design's world as **baseline**,
not as proposals — from this design's point of view they already happened.
Two flags on a slot dict capture everything that changes:

- **`inherited`** — this slot's identity/location came from an ancestor
  design's layer, not from reality untouched or from this design's own
  placements. `source_design_id` names which ancestor.
- **`conflict`** — something outside this design's control disagrees with
  this slot (an ancestor's settled name could not be resolved, an ancestor
  now occupies a unit this design already claimed, or a downstream
  placement's upstream reference is stale). `conflict_reason` is the
  human-readable detail.

### 12.2 Flags, not states (§8.4 of the plan)

`inherited` and `conflict` are **flags layered on top of an existing state**,
exactly as `displaced` already was — never new `ProjectedSlotState` members.
Reasons this matters for rendering:

- every row of the §3 table may carry either flag independently of its
  state, so the state × part matrix does not double for every combination;
- the legend's one-checkbox-per-state filter model stays intact — each flag
  gets its **own** checkbox that filters by OR against the flag, combined
  with the state checkboxes rather than multiplying them (§12.6).

### 12.3 Rendering (extends §3)

- **Inherited, no conflict**: draws as its underlying state — almost always
  `existing`, since an ancestor's effects are baseline — with a dimmed/
  outlined treatment distinguishing it from untouched reality. Hovering
  names the source design.
- **Inherited, in conflict**: same tile, PLUS the amber conflict marker —
  the same stripe-bar geometry `displaced` uses (hanging outside the rack
  frame on the right edge), recolored amber so it is never confused with a
  live displacement (red stays reserved for that). Full-depth mirrors the
  marker on the opposite face.
- **Not inherited, in conflict**: this design's own placement can carry the
  `conflict` flag too — e.g. its `base_placement` reference went stale, or
  its target unit is now contested by an ancestor. Same amber marker,
  attached to a tile that is otherwise rendered exactly as an ordinary
  add/move/remove of this design.

### 12.4 Movement rules that change (extends §4)

Dragging an inherited tile does **not** edit the ancestor's (frozen)
placement. It creates a **move in this design** referencing the upstream
identity — the ordinary §4 pipeline (validate → confirm → commit) applies
unchanged; only what gets created differs (a move keyed on the ancestor's
identity rather than on a `dcim.Device`).

An upstream conflict follows **neither** §4.3 (displacement) **nor** §4.7
(rejected placement) — see those sections for why: it does not block Save,
and it does not snap back on render. It is reported, persistently, in the
editor's conflicts panel (§12.5) until the design is re-based.

### 12.5 The conflicts panel (§8.3 of the plan)

`ProjectedElevation` carries a `conflicts` list (`{kind, severity, slot,
placement, source_design, detail}`) alongside the per-slot flags. The editor
renders every entry in ONE persistent panel (`design_editor.html`, alongside
the pre-existing stale-placements alert) — never a toast, because an upstream
conflict outlives the session that surfaced it. The panel names the source
design and links to it, so "re-base off X" is always the visible next step.

### 12.6 Legend (extends the legend debt fix, §8.6 of the plan)

The legend now carries two kinds of control on one row:

- **State checkboxes** (`data-rd-state`): `Existing / Add / Move in / Move
  out (ghost) / Remove` — unchanged, filter by state.
- **Flag checkboxes** (`data-rd-flag`, new): `Inherited / Conflict` — filter
  independently of, and combine by OR with, the state checkboxes.
- **Info-only keys** (no checkbox, new): `Displaced (was here)` and
  `Rejected` — these two markers existed before design chains but had NO
  legend entry at all, so a user had to hover a stripe or trigger a save
  rejection to learn what either meant. Both are fixed here rather than
  replicated: `Displaced` documents the existing red side-stripe (§4.3),
  `Rejected` documents the existing `.nbx-rd-error` ring (§4.7). Neither is
  a checkbox because unchecking a state already hides that state's own
  stripe, and the error ring is a transient highlight, not something to
  filter.
