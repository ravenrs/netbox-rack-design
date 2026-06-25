# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-06-25

### Release Summary
A minor, backward-compatible feature release. It adds a **read-only projected rack
elevation**: for any design you can now view a rack as it *would* look once that
design's placements (add / move / remove) are applied, computed entirely in memory
without modifying real NetBox data. The projected elevation is rendered with a
bundled GridStack-based layout and is reachable both from a design's own elevation
tab and, optionally, from a panel injected onto the core `dcim.rack` detail page
(gated by the `enable_rack_panel` config). This release also fixes a couple of bugs,
declares the Apache-2.0 license in the package metadata, and ships the project icon.

### Added
- **Projected rack elevation (read-only):** `projection.project_rack()` computes the
  front/rear/non-racked layout a design would produce for a given rack, and the new
  `DesignElevationView` renders it at `/plugins/rack-design/designs/<pk>/racks/<rack_id>/`.
- Bundled GridStack assets and plugin CSS/JS under `static/netbox_rack_design/`, plus
  a `rack_design` template-tag library for the elevation template.
- Optional **rack-page panel** (`PluginTemplateExtension` on `dcim.rack`) listing the
  designs that touch a rack, gated by the `enable_rack_panel` config setting.

### Fixed
- `DesignPlacement.get_kind_color()` so placement kind badges render with the correct
  color.
- `Design.clean()` no longer raises when approving a brand-new, unsaved root design;
  the "at most one approved version per plan" sibling check is now skipped until the
  version root is persisted. Covered by a regression test.

### Changed
- Declared `license = "Apache-2.0"` in `pyproject.toml` and shipped the full
  Apache-2.0 `LICENSE` text. Added the project icon (CC0) under `docs/assets/`, wired
  it into the README and the MkDocs theme logo/favicon.

### Deprecated
- N/A

### Removed
- N/A

### Security
- N/A

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
