# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.1] - 2026-07-09

### Release Summary
**Displacement you can actually see** — a patch release polishing how displaced devices are marked, everywhere.

The displaced-occupant marker is now a NetBox-reservation-style bar hanging **outside** the rack frame (wider, red diagonal stripes, aligned to the displaced units, on both faces for full-depth devices) instead of a thin sliver squeezed inside the occupying tile next to its remove button — and hovering it shows the standard device hover card with the displaced device's name, type and role. Displaced-pair detection moved into the projection layer, which fixed a real rendering hole on two surfaces: the read-only elevation view showed a saved displacement as two overlapping composited tiles, and the editor itself had the same overlap on a fresh page load (it only rendered correctly during the interactive session that created the displacement). No schema changes, no breaking changes.

### Fixed
- Read-only elevation view: saved displacements render as one full tile + the outside stripe bar instead of two overlapping tiles.
- Editor on load: saved displacements now collapse and get their stripe bars immediately (`applySavedDisplacements`), routed through the same ownership records as live gestures so later restores behave identically.
- Reloaded catalog adds regained their full-depth flag in the widget payload — the rear mirror bar was silently skipped on load.
- Hovering the stripe bar reliably shows the displaced device's info (hover card on both the editor and elevation pages).

### Changed
- Stripe bar geometry: outside the rack frame, 10px wide, percentage-tracked to the displaced rows; legend filters apply to it like to the tile it stands for.

## [0.9.0] - 2026-07-09

### Release Summary
**The tray becomes real** — non-racked devices (0U/vertical PDUs, rear-door units, cable managers) are now first-class citizens of the editor.

This minor, fully backward-compatible release makes each rack's "Non-racked tray" show reality, not just plans: real DCIM devices mounted in a rack without a U position now render in the tray as existing tiles and are plannable exactly like racked devices — drag them into rack units (plan a mount), out of units into the tray (plan a 0U dismount), or into another rack's tray (reassociate), with the same validation, rename dialogs, origin ghosts and silent identity-based homecoming the editor applies everywhere else. The tray behaves as a compact append-only list: items never overlap, drops append, rows renumber after removals and the container grows and shrinks with content. There are **no schema changes** (the placement fields were already nullable — only validation logic was relaxed) and **no breaking changes**.

### Added
- **Real non-racked devices in the tray**: devices with a rack but no position project as `existing` tray slots (face is meaningless off-rack and normalizes to none); racks without such devices show an empty tray, exactly as before.
- **Full tray move semantics** (spec `docs/editor-behavior-spec.md` §9): units↔tray mount/dismount, cross-rack tray→tray reassociation, palette adds into the tray, and identity-based homecoming — returning a device onto its own tray ghost (any hop count) silently restores the original placement. Origin trays keep a properly-styled ghost entry while the hardware hasn't moved yet.
- **Compact list layout**: tray items each get their own row, drops append below the bottom-most tile, rows renumber to contiguous after any removal, and existing items are never shuffled by a new drop.
- **New I4 model invariant**: a device exists exactly once across the whole editor world (racked body, tray body, or ghost pair) — checked on every sweep step alongside I1/I2.
- **Save contracts** for position-less placements: mount (position gained), dismount (rack kept, position cleared), tray→tray reassociation (rack changed, no position) — each covered by API tests, and untouched real tray devices round-trip as no-ops.
- Editor e2e suite `tests/e2e/test_editor_tray.py` (11 deterministic tests) plus projection/model/API test coverage (backend suite grows to 311 tests).

### Fixed
- Regression coverage hardening (post-0.8.0): rename-dialog Cancel/× full-revert guards, dedicated cross-rack displacement-on-adoption tests, and full-world diff assertions extended to the dense-pack/hatch-overlap/shadow-ownership suites.
- A cancelled tray-origin move no longer strands the tile on a face grid — cancel/ghost/restore paths resolve tray origins correctly.
- Tray drops no longer land on top of existing tiles, and departures no longer leave dead empty rows behind.
- The tray payload no longer registers spurious move placements for untouched real tray devices on save.

## [0.8.0] - 2026-07-08

### Release Summary
**The editor grows up** — a ground-up rework of how the multi-rack editor decides, renders and verifies device placement, plus a configurable naming engine for planned devices.

This minor, fully backward-compatible release replaces the editor's "let the grid engine move things and clean up after" approach with an explicit object model and a validate-before-commit pipeline, specified in `docs/editor-behavior-spec.md` and enforced by a spec-conformance test matrix (`docs/editor-conformance-matrix.md`). The result is an editor that behaves predictably under every gesture we could enumerate: nothing ever moves except the tile in your hand, full-depth devices carry their opposite-face shadow with them (live, while you drag), placing onto a vacating slot is an explicit confirmed action with a NetBox-style reservation marker, and a device dragged back home — across any number of racks, even after saving and reloading — silently becomes itself again. There are **no schema changes** (no new migrations) and **no breaking changes**; live `Device`/`Rack` records remain untouched, as always.

### Added
- **Naming-convention engine** for planned devices (`naming.py`): proposed names are computed per placement with three configurable modes — `sequence` (`<design>-<n>`), `template` (a `str.format` template over dotted NetBox-model attribute paths, e.g. `{design.name}`, `{device.site.name}`), and `script` (a dotted path to a custom callable). Configured via plugin settings `naming_mode` / `naming_template` / `naming_script`; the proposed name flows through the editor's rename dialog, the save path and the REST payload.
- **Editor behavior specification** (`docs/editor-behavior-spec.md`) — the authoritative rules for placement, shadows, ghosts, displacement and dialogs — and a **spec-conformance matrix** (`docs/editor-conformance-matrix.md`, ~54 rule×context rows) mapping every rule to its covering test.
- **Displacement flow**: dropping a device onto a slot whose occupant is vacating (moved away or flagged removed) asks for confirmation, then collapses the vacating occupant to a **red reservation side stripe** (NetBox-reservation look, old name on hover, mirrored on the opposite face for full-depth devices); the stripe reverts to the normal ghost/removed rendering if the new occupant leaves. Exactly one device can be planned into a vacated slot.
- **Live full-depth shadows**: a full-depth device's opposite-face hatch is now part of the device — it follows the tile in real time during a drag (a live rear-side legality preview), lands atomically with it, tints by the owner's state (existing / add / move-in / removed-crossed), and renders a visible red **conflict hatch** when a pre-existing layout double-books the opposite face instead of silently disappearing.
- **Cursor-governed placement**: the drag preview follows the cursor only — no "suggested placement" fallback. Illegal rows show a red deny indicator; releasing there snaps a moved tile back home and discards a palette drag-in entirely (no phantom add, Save stays untouched).
- **Cross-rack homecoming**: dragging a moved device back onto its own origin ghost — directly, after multiple hops, or after save + reload — silently restores the original placement (ghost and mirror removed, no duplicate entities).
- **Deterministic e2e regression net**: self-provisioning Playwright suites sweeping devices in 0.5U steps across both faces and multiple racks (76/87/120-step sweeps, dense-pack rejection, displacement dialogs, homecoming chains), each step asserting a **full-world diff** — any bystander tile changing any class or position anywhere fails the test.

### Fixed
- Dense-rack drag crash: `RangeError: Maximum call stack size exceeded` from GridStack's collision cascade when dragging onto a fully packed rack — placement is now decided before commit and engine push/repack is neutralized during gestures, so the cascade cannot start.
- Bystander tiles being silently relocated during drags, hatch redraws, editor init on double-booked layouts, and rejected foreign drops (several distinct engine paths, including two that bypassed all collision hooks).
- Full-depth shadows: orphaned after rejected drags or cross-rack departures, missing for palette adds and removed devices, stale move-tint after a rejected foreign drop, wrong-name ghost mirrors after grouped moves.
- Rename/displacement dialog never appearing for cross-rack moves (an early return skipped adopted tiles on every path), and dialogs getting stuck open forever when confirmed/dismissed during the opening animation (Bootstrap `hide()` no-ops mid-fade).
- A device released while hovering occupied units no longer teleports to the last-valid preview slot; re-dragged palette adds are validated like every other move.

### Changed
- The editor's shadow/ghost rendering pipeline is now event-driven from each device's own lifecycle (the global recompute pass is gone), and every placement decision routes through a single model-based validator (`rdCanPlaceAt`) covering bodies, shadows, ghosts and both faces of full-depth devices.
- Dev tooling: `pre-commit` pinned to 4.6.0 in `requirements_dev.txt`.

## [0.7.0] - 2026-06-29

### Release Summary
**One design, every rack it touches** — Rack Design grows from a single-rack editor into a multi-rack workspace.

0.6.0 made a single rack's elevation read like the real thing; 0.7.0 lets a **design span multiple racks**
and reworks the editor around that. This minor, fully backward-compatible release adds an explicit
**rack scope** to each design (a `racks` set, validated to the design's site), renders **all of a design's
racks side by side** in one editor with a single design-level Save, and adds a **read-only elevation view**
that shows the whole projected design — every rack, both faces — without an editing surface. It also ships
a substantial editor-UX round: a redesigned, collapsible **tool drawer** with three independent
Device / Favorites / Racks panels, a compact one-line **role + tenant** toolbar merged with the state legend,
and an **empty-state** editor that walks you through adding your first rack. As always, the editor only
composes **design placements**; your live `Device` and `Rack` records are never modified.

This release adds three **additive, backward-compatible** migrations: `0004` adds the `Design.racks`
relationship, `0005` seeds it from the racks each existing design already touches, and `0006` adds a
per-user rack-visibility table. There are **no breaking changes** — existing designs, placements, the REST
API, and GraphQL all continue to work, and the migrations reverse cleanly.

#### Multi-rack designs
A design now carries an explicit **rack scope** — a set of racks (`Design.racks`) the design plans against,
validated to live in the design's own site. Migration `0005` backfills this from the distinct target racks
of each design's existing placements, so every current design keeps exactly the racks it already touches.
New REST endpoints manage the scope: `designs/<pk>/add-rack/` (enforces same-site) and
`designs/<pk>/remove-rack/`. Removing a rack is **destructive and confirmed**: it returns `409` with the
list of placements that would be affected unless called with `confirm: true`, at which point it deletes the
placements targeting that rack and detaches it in a single transaction.

#### Multi-rack editor workspace
The editor is now a **design-level workspace** that renders every visible scoped rack **side by side**
instead of one rack at a time, with a single Save that persists the whole design's layout at once. The old
single-rack tab switcher is gone. Each rack renders through a shared `inc/rack_block.html` partial (the same
markup the read-only view uses), and a per-user **`HiddenDesignRack`** store lets you hide racks you aren't
working on without changing the design — with `hidden-design-racks/` list, `toggle/`, and `show-all/`
endpoints backing it. Opening a design with no racks yet shows an **empty state** with an "Add your first
rack" entry point instead of an error.

#### Read-only elevation view
A new **read-only elevation view** at `designs/<pk>/elevation/` renders the entire projected design — all
scoped racks, both faces, with full-depth devices hatched on their opposite face and the device hover card —
with no editing controls. It shares the exact projection the editor uses, so the two always agree. The old
per-rack elevation URL now redirects to this view.

#### Editor UX overhaul
- **Tool drawer.** The editor's side tools are now a collapsible push-sidebar split into **three independent
  toggles — Device, Favorites, and Racks** — that can be opened in any combination and stack side by side as
  columns; the open/closed state of each is persisted in `localStorage`.
- **Role + tenant toolbar.** The planned-add **device role** and **tenant** selectors now live in a compact,
  always-visible one-line toolbar merged with the state legend, removing the previous duplicate hint.
- **Layout polish.** Drawer columns flex-stretch to match the rendered rack heights, so the catalog scrolls
  internally instead of overshooting, at both tall and short viewports.

### Added
- **Multi-rack designs.** A new `Design.racks` many-to-many rack scope (validated to the design's site),
  with migration `0004` (schema) and `0005` (seed each design's scope from its placements' target racks). The
  scope is editable through the design form (a site-filtered rack selector) and exposed on the REST API
  serializer.
- **Rack-scope REST actions:** `designs/<pk>/add-rack/` (same-site enforced) and `designs/<pk>/remove-rack/`
  (destructive/confirmed — returns `409` + the affected placements unless `confirm: true`, then deletes the
  rack's placements and detaches it in one transaction).
- **Multi-rack editor workspace** rendering all visible scoped racks side by side via a shared
  `inc/rack_block.html` partial, with one design-level Save (`design_editor.html`, `editor.js`,
  `editor_panels.js`, `editor.css`).
- **Read-only elevation view** at `designs/<pk>/elevation/` showing the whole projected design (all racks,
  both faces, full-depth hatch, hover card) with no editing controls (`design_elevation.html`).
- **Per-user rack visibility.** A `HiddenDesignRack` model (migration `0006`) lets each user hide scoped racks
  from their own editor view, with `hidden-design-racks/` list, `toggle/`, and `show-all/` endpoints. Hiding a
  rack is per-user and never alters the design.
- **Editor tool drawer** with three independent Device / Favorites / Racks toggles (any combination, stacked
  as columns), persisted in `localStorage`, and an **empty-state** with an "Add your first rack" entry point.

### Changed
- The editor now renders an entire design's racks **side by side in one workspace** with a single Save,
  replacing the previous single-rack-at-a-time tab switcher.
- The planned-add **device role** and **tenant** selectors moved into a compact always-visible one-line
  toolbar merged with the state legend.
- The per-rack elevation URL now **redirects** to the new design-level read-only elevation view.

### Fixed
- N/A

### Deprecated
- N/A

### Removed
- The single-rack elevation grid partial (`inc/elevation_grid.html`) and the editor's single-rack tab
  switcher, superseded by the shared `inc/rack_block.html` and the multi-rack workspace.

### Security
- N/A

### Upgrade
`pip install -U netbox-rack-design` and restart NetBox.

- **Run `python manage.py migrate`** — this release adds migrations `0004_design_racks` (the `Design.racks`
  relationship), `0005_seed_design_racks` (seeds each existing design's rack scope from its placements'
  target racks), and `0006_hiddendesignrack` (a per-user rack-visibility table). All three are **additive**
  (no changes to existing columns, no destructive data rewrite), safe against existing designs, and reverse
  cleanly. There are **no breaking changes**.
- **Run `python manage.py collectstatic`** — the editor's bundled JS/CSS were reworked for the multi-rack
  workspace, the tool drawer, and the new shared rack partial (new `editor_panels.js`), so the updated static
  assets must be collected for the editor to render correctly.
- **No configuration changes** are needed — existing `PLUGINS_CONFIG` settings continue to work unchanged.

---

## [0.6.0] - 2026-06-27

### Release Summary
**See both faces at once** — Rack Design's elevation editor now renders front and rear side by side.

0.5.0 made the device-type catalog personal; 0.6.0 makes the rack elevation read like the real
thing. This minor, fully backward-compatible release replaces the single-face view with
**independent Front and Rear toggles**: both faces render at the same time, so network gear on
the front and servers on the rear are visible together. The toggles are independent on/off
switches (you can show either or both) and the editor **never lets you hide both faces** at once.
Hovering any tile now pops a **device hover card** showing the device's name, role, and tenant —
gracefully omitting whichever fields are empty. Finally, **full-depth devices are now honored
across both faces**, matching NetBox core's own rack-elevation rendering.

There are **no breaking changes** and **no database migration** in this release — the changes are
limited to the projection layer, the editor template/JS/CSS, the save-layout payload, and two new
template filters.

#### Independent Front/Rear face toggles
The elevation editor now exposes Front and Rear as **independent on/off toggles** and renders both
faces side by side, so you can plan the front and rear of a rack together instead of flipping
between them. Either face can be hidden on its own, but the editor **always keeps at least one
face visible** — it will not allow both to be turned off.

#### Device hover card
Hovering a placed tile now shows a **hover card** with the device's **name, role, and tenant**.
When a field isn't set (no role, or no tenant), it is simply omitted from the card rather than
shown blank. Two new template filters, `slot_role_name` and `slot_tenant_name`, resolve the
display values.

#### Full-depth devices across both faces
**Full-depth devices are now correctly shown on both faces**, matching NetBox core's rack-elevation
behaviour. A full-depth device renders its normal coloured state on the face it is mounted on,
while the **opposite face** shows core's "blocked" diagonal hatch (the same `#f7f7f7`/`#ffc0c0`
45° stripe pattern core uses) labelled with the device name. The opposite-face rendering is
**passive**: it is not draggable, carries no `×`/star controls, and is **excluded from the save
payload**, so it can never create a duplicate `DesignPlacement`. This applies to existing devices
and across every design kind (add, move-in, move-out ghost, and remove), driven by a new
`opposite_face` flag on the projection slot contract and the editor payload.

### Added
- **Independent Front/Rear face toggles** in the elevation editor: both faces render side by side
  and each can be toggled on/off independently, with a guard that **never allows both faces to be
  hidden** (`design_editor.html`, `editor.js`, `editor.css`).
- **Device hover card** on placed tiles showing the device **name, role, and tenant**, omitting
  empty fields, backed by two new template filters `slot_role_name` and `slot_tenant_name`
  (`templatetags/rack_design.py`).
- New `opposite_face` flag on the projection slot contract and the editor widget payload so
  full-depth devices can be represented on the face they do not occupy.
- Tests covering full-depth opposite-face projection and the new behaviour
  (`tests/test_fulldepth.py`).

### Fixed
- **Full-depth devices now correctly occupy both faces**, matching NetBox core's rack-elevation
  rendering. The mounted face shows the normal coloured state; the opposite face shows core's
  "blocked" diagonal hatch (`#f7f7f7`/`#ffc0c0`, 45°) with the device name. The opposite-face
  tile is passive — not draggable, no `×`/star controls — and is **excluded from the save
  payload**, so it never creates a duplicate `DesignPlacement`. Applies to existing devices and
  to all design kinds (add / move-in / move-out ghost / remove) (`projection.py`, `views.py`,
  `editor.js`, `editor.css`).

### Changed
- The elevation editor now renders **both rack faces simultaneously** instead of a single face at
  a time (see the independent toggles above).

### Deprecated
- N/A

### Removed
- N/A

### Security
- N/A

### Upgrade
`pip install -U netbox-rack-design` and restart NetBox.

- **No database migration is required** this release — `python manage.py migrate` is a no-op for
  this plugin (the changes are limited to the projection layer, editor template/JS/CSS, the
  save-layout payload, and two template filters; no models changed).
- **Run `python manage.py collectstatic`** — the editor's bundled JS/CSS were updated for the
  independent face toggles, the device hover card, and the full-depth opposite-face rendering, so
  the new static assets must be collected for the editor to render correctly.
- **No configuration changes** are needed — existing `PLUGINS_CONFIG` settings continue to work
  unchanged.

---

## [0.5.0] - 2026-06-26

### Release Summary
**Pin the gear you reach for** — Rack Design gets per-user favorite device types.

0.4.0 gave the single-rack editor a searchable device-type catalog to drag new gear from;
0.5.0 makes that catalog *personal*. This minor, fully backward-compatible release lets each
user **star a device type** to pin it to a dedicated **Quick access** column in the editor,
so the handful of types you plan with most are always one click — and one drag — away,
without scrolling or re-filtering the full catalog. Favorites are **per-user**: starring a
type affects only your own Quick access column, never anyone else's, and they are persisted
through a new user-scoped favorites REST API. The Quick access column is **independent of the
catalog's search/manufacturer filter**, so narrowing the catalog never hides your pinned
types — your favorites stay put while you search.

As with every editor surface, this is purely a planning convenience: drag-to-plan from Quick
access composes **design placements** exactly like the main catalog, and your live `Device`
records are never touched.

There are **no breaking changes**. This release adds a single database migration that only
**adds one new table** (`FavoriteDeviceType`); it introduces no changes to existing models,
placements, the public REST API, or GraphQL, and reverses cleanly.

#### Per-user favorite device types
Each row in the device-type catalog now carries a **star toggle**. Starring a type adds it to
your **Quick access** column at the top of the palette; un-starring removes it. Favorites are
scoped to the logged-in user via a `FavoriteDeviceType` model (a unique `(user, device_type)`
pair), so two users planning the same rack see their own independent Quick access lists.

#### Quick access column in the editor
The Quick access column lists your starred device types as ready-to-drag tiles, **drag one
straight onto a free rack unit** to plan an add — the same planning surface the main catalog
uses. The column is rendered **independently of the catalog's type-ahead search and
manufacturer filter**, so filtering the catalog to find something never empties or reorders
your pinned favorites.

#### User-scoped favorites API
A new favorites endpoint backs the stars: a **GET** returns the current user's favorite device
types, and a **POST** toggles a device type in or out of that set (`FavoriteToggleSerializer`,
wired through `api/views.py` and the API router). All reads and writes are scoped to the
requesting user, so one user can never see or modify another's favorites.

### Added
- **Per-user favorite device types.** A new `FavoriteDeviceType` model (per-user `(user,
  device_type)` with a uniqueness constraint) lets each user pin the device types they plan
  with most.
- **"Quick access" favorites column** in the single-rack editor: starred device types render
  as drag-to-plan tiles in a dedicated column that is **independent of the catalog's
  search/manufacturer filter** (`design_editor.html`, `editor.js`, `editor.css`).
- **Star toggles** on catalog rows to add/remove a device type from your favorites.
- **User-scoped favorites REST API**: a GET list of the current user's favorites and a POST
  toggle action, both scoped to the requesting user (`FavoriteToggleSerializer` in
  `api/serializers.py`, the view in `api/views.py`, router entry in `api/urls.py`).
- API and view tests covering the favorites endpoint (list + toggle, user scoping) and the
  editor's Quick access wiring.

### Changed
- N/A — no changes to existing behavior, data model, or public API.

### Fixed
- N/A

### Deprecated
- N/A

### Removed
- N/A

### Security
- N/A

### Upgrade
`pip install -U netbox-rack-design` and restart NetBox.

- **Run `python manage.py migrate`** — this release adds migration
  `0003_favoritedevicetype`, which only **adds one new table** (`FavoriteDeviceType`). It is
  additive (no changes to existing tables, no data rewrite, no backfill), safe against existing
  designs, and reversible. There are **no breaking changes**.
- **Run `python manage.py collectstatic`** — the editor's bundled JS/CSS were updated for the
  Quick access favorites column and star toggles, so the new static assets must be collected
  for the editor to render correctly.
- **No configuration changes** are needed — existing `PLUGINS_CONFIG` settings continue to
  work unchanged.

---

## [0.4.0] - 2026-06-26

### Release Summary
**Plan new gear, not just rearrange it** — Rack Design gets a device-type catalog.

0.3.0 let you drag the rack's *existing* devices around; 0.4.0 lets you plan brand-new
ones. This minor, fully backward-compatible release adds an interactive **device-type
catalog palette** to the single-rack editor: search the catalog, narrow it by
manufacturer, and **drag a device type straight onto a free rack unit** to plan a new
add — no leaving the editor to pre-create anything. Each planned add can now also carry
an intended **device role** and **tenant**, chosen through NetBox's own dynamic selects,
so the plan records *what* you intend to rack and *whose* it is. As always the editor
only composes **design placements**; your live `Device` records are never touched until
a design is later executed.

This release also fixes a visual glitch where multi-unit state tiles rendered
semi-transparent (letting the grid bleed through), adds **cache-busting** to the bundled
editor JS/CSS so browsers reliably pick up new assets after an upgrade, and ships a new
committed **headless end-to-end regression suite** for the editor's client-side
behaviour.

There are **no breaking changes**. This release adds a database migration that only
**adds two nullable fields** (`device_role`, `tenant`) to `DesignPlacement`, so it is
safe to apply against existing data and reverses cleanly.

#### Device-type catalog palette
A new palette in the editor lists the device types you can plan. **Type-ahead search**
filters by name and a **manufacturer filter** narrows the catalog to a single vendor.
**Drag a device type onto an empty unit** and the editor plans an *add* placement at that
position — the same planning surface that already handled moves and removals, now able to
introduce devices that don't yet exist in the rack.

#### Role and tenant on planned adds
A planned add can now record an intended **device role** and **tenant**, selected via
NetBox **dynamic (API-backed) selects** in the editor. The two new `DesignPlacement`
fields are **add-only**: `clean()` rejects setting a role or tenant on a *move* or
*remove* placement, keeping the data model honest. Both fields are optional and surface
through the REST API and GraphQL.

#### Editor polish & regression coverage
- **Opaque multi-U tiles.** Tiles spanning more than one rack unit were rendering
  semi-transparent, letting the grid lines show through and muddying the colour coding;
  they are now fully opaque so add/move/remove states read clearly.
- **Asset cache-busting.** The bundled editor `*.js`/`*.css` are now requested with a
  version query string (`?v=`) derived from the asset set, so an upgraded plugin's new
  editor assets are loaded instead of a stale cached copy.
- **Headless e2e regression suite.** A new committed Playwright suite under `tests/e2e/`
  exercises the editor's real client-side DOM/GridStack behaviour (catalog drag-in, the
  context-sensitive `×`, the payload `buildRackPayload` would emit). It is strictly
  **read-only** against the dev database and **skips cleanly** when Playwright/Chrome or a
  dev server isn't present, so it stays out of the normal headless suite while remaining
  runnable on demand.

### Added
- **Device-type catalog palette** in the single-rack editor: type-ahead search + a
  manufacturer filter, and **drag-and-drop a device type onto a free unit** to plan a new
  add (`editor.js`, `design_editor.html`, `editor.css`).
- **`device_role` and `tenant` on planned adds.** Two new optional, nullable FK fields on
  `DesignPlacement` (to `dcim.DeviceRole` / `tenancy.Tenant`), selected via NetBox dynamic
  selects, add-only and validated as such in `clean()`, and exposed through the form, the
  REST API serializer, and GraphQL.
- **Committed headless e2e regression suite** (`tests/e2e/test_editor_e2e.py`) for the
  editor's client-side behaviour — read-only and self-skipping when prerequisites are
  absent.
- Expanded API and view tests covering the new fields and the catalog/editor wiring.

### Fixed
- **Multi-unit state tiles** in the editor were semi-transparent and let the grid bleed
  through; they now render fully opaque so the add/move/remove colour coding is legible.

### Changed
- Bundled editor JavaScript/CSS are now loaded with an `asset_version` cache-busting query
  string so upgraded assets aren't served stale from the browser cache.

### Deprecated
- N/A

### Removed
- N/A

### Security
- N/A

### Upgrade
`pip install -U netbox-rack-design` and restart NetBox.

- **Run `python manage.py migrate`** — this release adds migration
  `0002_designplacement_device_role_designplacement_tenant`, which only **adds two
  nullable fields** to `DesignPlacement`. It is additive (no data rewrite, no backfill),
  safe against existing designs, and reversible. There are **no breaking changes**.
- **Run `python manage.py collectstatic`** — the editor's bundled JS/CSS were updated for
  the catalog palette and role/tenant selects (and now carry cache-busting), so the new
  static assets must be collected for the editor to render correctly.
- **No configuration changes** are needed — existing `PLUGINS_CONFIG` settings continue to
  work unchanged.

---

## [0.3.0] - 2026-06-26

### Release Summary
**The rack comes to life** — Rack Design becomes *interactive*.

0.2.0 let you *see* a projected rack elevation; 0.3.0 lets you *change* it. This minor,
fully backward-compatible release adds a drag-and-drop **single-rack layout editor** so
you can plan a rack visually — move a device to a new unit, mark one for removal, or
back out an add — and save the result as design placements, all without ever touching
your live NetBox devices. It also adds a standalone, filterable **Elevations** list so
you can jump straight to any rack's projected layout, and surfaces designs on the core
rack pages.

Nothing about existing designs, placements, the REST API, GraphQL, or configuration
changes in an incompatible way — every prior URL, field, and endpoint behaves as before.
There are **no breaking changes** and **no database migrations** in this release.

#### Interactive single-rack layout editor
Open a design's rack and drag devices around a live GridStack elevation. The editor
understands the three placement kinds and gives you a single, **context-sensitive ×
control** whose meaning depends on what you're looking at:

- on an **existing** device → flags it for **removal**;
- on a device you've **moved** → **cancels the move**, snapping it back to its original unit;
- on a device you just **added** → **cancels the add**, removing it from the plan.

While you drag, a live **move-visualization ghost** previews the target position so you
can see exactly where a device will land before you let go. The editor is purely a
planning surface: it composes and edits **design placements** and never mutates the real
`Device` records underneath.

#### Save-layout API
A new save-layout REST action persists what you drew. Rather than blindly rewriting the
design, it **diffs the submitted layout against the current `DesignPlacement` rows** and
applies only the differences. Every created or changed placement is run through full
`clean()`/`full_clean` **validation** (e.g. unit availability) before it is saved, so an
invalid layout is rejected rather than half-written. The action is **idempotent** —
saving the same layout again is a no-op and round-trips cleanly — and **deletes
conservatively**, so re-saving never silently drops placement data you didn't intend to
remove. As with the editor, **real Devices are never modified**; only design placements
are written.

#### Standalone Elevations list
A new top-level, **filterable Elevations list view** lets you browse projected rack
elevations directly, without first drilling into a specific design — filter down to the
rack or design you care about and open its projected layout in one step.

#### Rack-page integration
The core **rack** detail pages now carry a **rack-designs panel** listing the designs
that touch the rack, each linking through to its editor/elevation, and a new
**navigation** entry exposes the Elevations list in the plugin menu.

### Added
- **Interactive single-rack layout editor** (`design_editor.html` + bundled
  `static/netbox_rack_design/js/editor.js` and `css/editor.css`) built on GridStack:
  drag-and-drop move, mark-for-removal, and cancel-add, driven by one context-sensitive
  `×` control (existing → flag removal, moved → cancel move, added → cancel add), with a
  live move-visualization ghost during drags. Edits design placements only; real Devices
  are never touched.
- **Save-layout REST action** that diffs the submitted layout against current
  `DesignPlacement` rows, validates every change with `full_clean`, is idempotent across
  round-trips, and deletes conservatively so no layout data is lost.
- **Standalone, filterable Elevations list view** (`elevation_browser.html`) for
  browsing projected rack elevations directly, plus a shared `inc/elevation_grid.html`
  partial and `legend_filter.js` for legend-driven filtering.
- **Rack-designs panel** on the core `dcim.rack` detail page listing designs that touch
  the rack, each linking to its editor/elevation.
- **Navigation entry** surfacing the Elevations list in the plugin menu.
- Accompanying API and view tests covering the save-layout action, the Elevations list,
  and the editor wiring.

### Changed
- N/A — no changes to existing behavior, data model, or public API.

### Fixed
- N/A

### Deprecated
- N/A

### Removed
- N/A

### Security
- N/A

### Upgrade
`pip install -U netbox-rack-design` and restart NetBox.

- **Run `python manage.py collectstatic`** — this release ships new bundled static
  assets (the editor's JavaScript/CSS and the legend filter) that must be collected so
  the editor renders correctly.
- **No database migrations** are required (`python manage.py migrate` is a no-op for this
  plugin) and **no configuration changes** are needed — existing `PLUGINS_CONFIG`
  settings continue to work unchanged.

---

## [0.2.0] - 2026-06-25

### Release Summary
**Projected rack elevations** — the first *visual* surface of NetBox Rack Design.

Until now a design was only a list of placement records; you couldn't actually *see*
the rack it described. This minor, backward-compatible release renders any design as a
full rack elevation showing how the rack **would** look once the design is applied:
planned **adds**, **moves**, and **removals** overlaid on the rack's real devices —
computed entirely in memory, with **zero changes to your live NetBox data** (nothing
is materialized until an explicit Apply, which arrives in a later release).

The elevation is drawn with a bundled GridStack layout (front/rear faces plus a
non-racked tray) and uses clear visual encoding so the plan reads at a glance:

- **green** — a device the design *adds*;
- **cyan** — a device *moved in*, with a faded **ghost** left at the slot it vacates;
- **red, struck-through** — a device marked for *removal*;
- **neutral** (the device role color) — existing devices the design leaves untouched.

Open it from a design at `/plugins/rack-design/designs/<id>/racks/<rack_id>/`, or
straight from the core **Rack** detail page via an optional panel that lists every
design touching that rack. This is the read-only foundation for the interactive
drag-and-drop editor coming in a future release.

This release also fixes two bugs, declares the project's Apache-2.0 license in the
package metadata, and ships the project icon.

**Upgrade:** `pip install -U netbox-rack-design` and restart NetBox. No database
migrations and no configuration changes are required. The rack-page panel is enabled
by default; set `enable_rack_panel = False` in `PLUGINS_CONFIG` to hide it.

### Added
- **Projected rack elevation (read-only).** A new `DesignElevationView` renders, for a
  given design and rack, the front/rear/non-racked layout the design would produce —
  existing devices in place, plus the design's add/move/remove placements overlaid at
  their target units with state-based colour coding and a legend. The projection is
  computed by `projection.project_rack()` purely in memory; real devices are never
  modified. Reachable at `/plugins/rack-design/designs/<pk>/racks/<rack_id>/`.
- **Rack-page panel.** An optional `PluginTemplateExtension` on the core `dcim.rack`
  detail page lists the designs whose placements touch that rack, each linking to its
  projected elevation. Gated by the new `enable_rack_panel` config setting (default
  `True`); renders nothing when no design touches the rack.
- Bundled GridStack assets and plugin CSS/JS under `static/netbox_rack_design/` (no
  external CDN), plus a `rack_design` template-tag library powering the elevation
  template.

### Fixed
- Placement **kind badges** (Add/Move/Remove) now render in their intended colours
  instead of grey — `DesignPlacement` was missing the `get_kind_color()` accessor that
  NetBox's choice-field column relies on.
- **Approving a brand-new design no longer errors.** `Design.clean()` previously raised
  an unhandled error (HTTP 500) when a first/standalone design was created directly with
  status *Approved*, because the "at most one approved version per plan" check queried
  against an unsaved version root. The check is now skipped until the root is persisted.
  Covered by a regression test.

### Changed
- The project's **Apache-2.0 license is now declared in the package metadata**
  (`license = "Apache-2.0"` in `pyproject.toml`) and the full `LICENSE` text ships with
  the distribution — previously the published package carried no license metadata.
- Added the **project icon** (CC0) under `docs/assets/` and wired it into the README and
  the MkDocs theme logo/favicon.

---

## [0.1.0] - 2026-06-24

### Release Summary
Initial release of NetBox Rack Design — a generic, public plugin that adds a
versioned **design layer** for planning rack changes on top of real NetBox data.
This first release delivers the data model and full management surface (Stage 1);
the interactive visual editor and apply/conflict/power features follow in later stages.

### Added
- **Models:** `Design` (versioned, sequenced, with dependencies and optional grouping),
  `DesignGroup` (hierarchical container), and `DesignPlacement` (add / move / remove
  actions, validated against `Rack.get_available_units()`).
- Full CRUD UI for all three models, including detail pages with related-object panels.
- REST API at `/api/plugins/rack-design/` (designs, design-groups, placements).
- GraphQL types and filters on the unified `/graphql` endpoint.
- Global search, navigation menu, change logging, journaling, custom fields, and tags.
- Config-driven statuses (`planned_statuses` / `removal_statuses` / `default_status`)
  via `PLUGINS_CONFIG` — nothing organization-specific is hardcoded.
- Test suite built on NetBox's standard test cases, plus MkDocs documentation.

### Fixed
- N/A (initial release)

### Changed
- N/A (initial release)

### Deprecated
- N/A (initial release)

### Removed
- N/A (initial release)

### Security
- N/A (initial release)

---

## Release Notes Template for Future Versions

When creating a new release, use this template:

```markdown
## [X.Y.Z] - YYYY-MM-DD

### Release Summary
Brief narrative summary describing the release type (major/minor/patch) and key highlights.

### **Breaking Changes**
<!-- Only include this section if there are breaking changes -->
- **[#issue]** Description of breaking change and migration path
- Link to detailed migration guide if needed

### Added
- New features and capabilities

### Fixed
- Bug fixes with issue references

### Changed
- Changes to existing functionality

### Deprecated
- Features marked for future removal

### Removed
- Features that have been removed

### Security
- Security improvements and fixes
```

---

**Best Practice**: For clear release communication, ensure each release includes:
1. Narrative summary characterizing the release type (major/minor/patch)
2. Clear indicators for bugs, features, or enhancements
3. Bold "Breaking Changes" header when applicable with migration guidance
4. Detailed changelog with issue references
