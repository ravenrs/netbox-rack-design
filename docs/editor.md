# The editor

The editor is where a design is composed: every rack in the design's scope,
side by side, on top of the live DCIM data, with adds, moves and removals
drawn as tiles. Nothing you do here touches a real device — **Save** writes
`DesignPlacement` rows on the design and nothing else.

Open it from a design's page — **Open editor** in the header, or in the Racks
card (**Edit layout** on each rack row) — or from the panel on a core rack
page. The URL is
`/plugins/rack-design/designs/<pk>/editor/<rack pk>/`; the rack pk only picks
which rack is scrolled into view.

## The workspace

▶ Video: [Part 2 — The editor workspace](https://youtu.be/NXj5lqCR5RI)

- **One block per rack**, in scope order. Each block shows the rack's name
  and site in its header and has its own **Front / Rear** toggle, so one rack
  can be viewed from the back while its neighbour stays on the front.
  Full-depth devices appear on both faces (the rear copy is hatched).
- **Racks of different heights hang from a common floor**, so U1 lines up
  across the row.
- **The non-racked tray** under each rack holds zero-U gear — PDUs, and
  anything else that belongs to the rack without taking a unit. Tray devices
  can be dragged into the rack and back. A planned PDU dropped into the tray
  [binds itself](power-distribution.md#automatic-binding) to the rack's matching
  feed — the ⚡ button on the tile names the feed it took and reopens the
  picker if you want a different one. The picker only opens by itself when
  there was nothing to bind to.
- **The power bar** under a rack's name shows projected draw vs. capacity,
  utilisation and a ⚠ count of devices with an unconnected power port; hover
  the ⚠ for their names. Below it, in `builtin` or `script` distribution
  mode, the **bank chips** — one block per PDU, one chip per breaker bank. See
  [Power distribution](power-distribution.md).
- **The hover card** — hover any tile for its type, role and tenant, every
  power port with its draw (`(nc)` marks one not cabled to power) and the
  total allocated, any [planning fields](planning-fields.md) it carries, and,
  for a chassis, `N of M bays used` plus the occupants. A moved device also
  shows **From** and **To** — on the device *and* on the ghost it left — plus
  its old name, role or tenant when the move changes them; an inherited,
  conflicting or reserved tile names the design responsible.

## The drawer

▶ Video: [Part 2 — The editor workspace](https://youtu.be/NXj5lqCR5RI)

Four buttons on the right edge open panels:

| Panel | What it does |
|---|---|
| **Device** | The searchable device-type catalog. Drag a tile onto a rack unit (or into the tray for a 0U type). |
| **Favorites** | Your per-user shortlist of device types, kept in **named sets** — "Default", "Network gear", whatever you like. The set selector has create / rename / delete; the ★ in the catalog reads and writes the selected set. The same type can sit in several sets; sets are private to you. |
| **Templates** | Your saved rack layouts and template groups. Drag a card onto a rack, or click it to apply to several racks. See [Templates](templates.md). |
| **Racks** | **Add rack** shows a Site selector (listing only the design's sites), then Location and Rack (both optional); with exactly one site it is pre-selected as a read-only chip. Location and Rack are disabled until a site is chosen; changing site clears both. **Create rack** has the same Site select before Location. The visibility toggles hide racks from *your* view only — they stay in the design and in everyone else's editor. |

## The legend is a filter

▶ Video: [Part 2 — The editor workspace](https://youtu.be/NXj5lqCR5RI)

The row of chips above the racks is both a legend and a set of live filters.
Untick one and that class of tile disappears until you tick it again.

| Chip | Meaning |
|---|---|
| **Existing** | A real device, as it stands in DCIM today |
| **Add** | A planned addition (green outline) — exists only in this design |
| **Move in** | A device this design moves into this slot |
| **Move out (ghost)** | The slot a moved device leaves behind |
| **Remove** | A device flagged for removal (red hatch) |
| **Inherited** | Rendered from an approved ancestor design — see [Design chains](design-chains.md) |
| **Conflict** | An inherited placement that no longer resolves cleanly, or your tile on a unit an ancestor now occupies |
| **Peer conflict** | Contested by an unrelated draft — see [Peer conflicts](peer-conflicts.md) |
| **Applied** | This design already wrote it into DCIM — see [Applying a design](apply.md) |
| **Reserved** | Another design applied it; this slot is spoken for |
| *Displaced (was here)* | Info only: the stripe on a unit something just moved out of |
| *Rejected* | Info only: the ring on a tile the server refused on the last Save |

Next to the legend: the **Power heatmap** switch (colour each tile by its share
of the rack's biggest consumer), the **Bank zones** switch (a strip beside each
rack showing which U range each PDU bank serves), and the **Role**, **Tenant**
and any [rail planning fields](planning-fields.md#in-the-editor) — values a
device picks up as you drop or move it.

## Editing

▶ Video: [Part 3 — Everyday editing](https://youtu.be/tTbLeEwYIUQ)

**Add.** Set the toolbar **Role** first — NetBox requires a role on every
device, so a drop with no role is refused: the tile goes back, a warning says
why, and the Role select is highlighted. Then drag a device type from the
catalog or Favorites onto a free unit. A green preview shows exactly where it
will land; the tile appears with a name from the
[naming engine](device-naming.md) and the current toolbar role, tenant and
rail values. Hover the tile and click the pencil to type a different name.

**Move.** Drag an existing device to another unit — in the same rack, to the
other face, or into another rack. Every move asks **Name this move**: *Keep
the old name* (shown in the design as `<design title>-<old name>` until it is
applied) or *Set a new name*, pre-filled with the naming engine's suggestion
for the new home. **Apply** confirms; **Cancel** (or closing the dialog)
undoes the move. The origin keeps a **ghost** so the move stays visible; drag the device back to where it started ("homecoming") and
both the ghost and the move disappear. Dropping onto a unit that is being
vacated (a ghost, or a flagged removal) is allowed and **displaces** it; the
striped unit marks what just changed. A move can carry a rename suggestion
from the naming engine and planned overrides for role, tenant and planning
fields — see [Planning fields](planning-fields.md#in-the-editor).

**Illegal drops** — onto an occupied unit, past the top of the rack, a blade
onto a rack unit — are refused at the preview; release anyway and the tile
snaps back to its last valid position.

**Remove.** The red × on an existing device flags it for removal. On a planned
add or move the same × reads *Cancel this planned add / move* and does exactly
that.

**Save.** Arms only when something changed. It writes the design's placements
in one request; if the server rejects a placement (someone racked into that
unit since you loaded the page) the tile gets the *Rejected* ring and nothing
else is lost. Leaving the page with unsaved work asks first; switching to the
chassis layer offers **Cancel / Discard / Save and switch**.

## Chassis & blades

▶ Video: [Part 6 — Chassis & blades](https://youtu.be/6mCIUimNaq8)

A device whose type has device bays — a blade chassis, a patch-panel frame,
anything that holds children — is a normal tile in the rack view. Its bays
are edited on the **chassis layer**, reached from the **Chassis** button that
appears in the header once the design's scope contains at least one such
device (real or planned).

- The layer shows every chassis in scope as a **column of bays**, driven by
  the same editor: drag a blade into a bay, between bays, or between chassis.
  A bay holds one occupant, so a drop onto an occupied bay is refused, never
  displaced.
- **The palette filters itself to child device types** on this layer, and the
  rack grids refuse them everywhere else — a blade can never be racked and a
  server can never be baid.
- **Both kinds of chassis work.** A real chassis offers its DCIM device bays;
  a chassis this same design adds offers the bays from its type's bay
  templates (there are no bay rows until it is applied).
- **Power is never double-counted**: a chassis with a draw of its own wins
  and its blades add nothing; a chassis with none rolls its blades up.
- Chassis visibility is per user, like rack visibility.
- The read-only **chassis elevation** (`/designs/<pk>/chassis-elevation/`)
  renders the same columns without the editor.

Applying installs every planned blade into its bay — see
[Apply](apply.md#blades). Not yet: moving a *real* blade between chassis in the
editor.

## Read-only views

▶ Video: [Part 19 — Read-only views](https://youtu.be/90WoG9IRy-Q)

- **Projected elevation** — the design's racks as they would look once
  applied, both faces, with the heatmap and every legend marker, but no
  editing. Linked from the design page and the editor.
- **Elevations** (menu) — a filterable browser across every design.
- **Chain Health** (menu) — every design whose chain is refused or that
  carries stale placements, each row linking to the fix. See
  [Design chains](design-chains.md#chain-health).
- **The rack page panel** — on a core `dcim.rack` page, the designs touching
  that rack, each linking to its editor and elevation (`enable_rack_panel`).

## Reference

The exact behaviour of every gesture — blocking rules, displacement, cursor
governance, the save contract — is specified in the
[Editor behavior spec](editor-behavior-spec.md).
