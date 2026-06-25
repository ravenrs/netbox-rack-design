# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
