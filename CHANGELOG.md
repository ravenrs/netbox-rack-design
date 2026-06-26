# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
