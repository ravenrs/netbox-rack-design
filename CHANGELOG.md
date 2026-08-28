# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.23.0] - 2026-08-28

### Release Summary

A workspace-and-inventory release. Favorites stop being one flat list — they
become named sets you switch between — planned power feeds stop being invisible
and get list, detail and delete views of their own, and the multi-rack workspace
learns to hang racks from a common floor instead of a common ceiling.

### Added

- **Named favorite sets.** The catalog stars used to write one flat list per
  user, so planning a server build and a network build meant re-starring every
  time. Favorites now live in named sets ("Default", "for server", "for
  network"): the Quick-access panel carries a set selector plus create, rename
  and delete, the stars read and write the selected set, and the same device
  type may sit in as many sets as you like. Sets are per user and their names
  are unique per user — two people can each have their own "for server".
  Existing favorites are migrated into a set named "Default", which is also what
  a set-unaware client keeps talking to.
  New endpoint `/api/plugins/rack-design/favorite-sets/` (list/create/rename/
  delete); `/favorite-device-types/` gained an optional `set_id`.
- **Planned power feeds have their own views.** A feed created by the editor's
  rack-power or PDU-bind dialog could be seen nowhere and removed by nothing,
  while silently sizing a greenfield rack's capacity bar. `DesignPowerFeed` is
  now a first-class object: **Rack Design → Planned Power Feeds** lists them
  with filters, bulk edit and bulk delete; each feed has a detail page showing
  its derated capacity and the planned PDUs bound to it; and the design page's
  feed panel gained per-row edit and delete. Also exposed over REST
  (`/api/plugins/rack-design/planned-power-feeds/`) and GraphQL
  (`planned_power_feed`), and indexed for global search.
- **Racks of different heights now hang from a common floor.** A 47U rack beside
  a 42U one started at the same y, so the taller rack merely ran further down
  and U1 of one sat level with U6 of the other. Both the editor and the
  read-only elevation now align rack floors, so height reads as height and the
  same U lines up across racks.

### Fixed

- **The editor no longer collapses to a strip when no rack is visible.** A
  design with zero racks (or with every rack hidden) shrank the workspace to
  whatever was left, and since the drawer stretches to it, the Add-rack panel
  collapsed too — making the first rack painful to add. The workspace keeps a
  usable height.
- **The red × says what it will do.** On a planned move it now reads "Cancel
  this planned move" and on a planned add "Cancel this planned add", instead of
  "Flag for removal" on every tile — nothing is being flagged for removal there.

## [0.22.0] - 2026-08-28

### Release Summary

The per-bank distribution stops failing silently. When the engine breaks, the
rack now says so — which exception, from which script — instead of quietly
rendering an empty chip strip that looks exactly like a rack with no PDUs.

### Added

- **A rack with no per-bank chips now explains itself.** Four different causes
  produced the same blank strip — the script raised, the PDU has no resolvable
  feed, `distribution_mode` is `none`, or a stale script copy is being loaded —
  and telling them apart meant reproducing the data and calling the engine by
  hand. Under the power bar the editor now renders:
  - **failed** (red): "the distribution script failed", with the exception type,
    its message and the script's dotted path on hover;
  - **empty**: which PDU had to be omitted and why (input not cabled to a
    `dcim.PowerFeed` and not bound to a planned feed / no parseable outlet
    banks), or that the rack has no PDU device at all;
  - **off**: that per-bank distribution is switched off, so a configuration
    answer never reads as missing data.
- `generate_distribution_status(elevation)` returns `(distribution, status)`;
  the status also rides `elevation.power["distribution_status"]` and a new
  `distribution_status` block in the `recompute-distribution/` response, so an
  edit that breaks the engine reports itself immediately rather than at the next
  page load. `generate_distribution(elevation)` is unchanged for scripts and
  existing callers.

## [0.21.1] - 2026-08-28

### Release Summary

A rack could lose its entire per-bank distribution — every PDU chip gone —
because of a single device fed from a PDU in another rack.

### Fixed

- **One device cabled to a neighbouring rack's PDU no longer wipes the whole
  rack's per-bank distribution.** The shipped distribution scripts charged a
  device to the bank of whatever outlet it was cabled to, without checking that
  PDU belongs to the rack being computed. A foreign PDU is not in that rack's
  topology, so the charge raised `KeyError`, `generate_distribution` caught it
  and returned `None`, and the rack fell back to the per-device heatmap with no
  bank chips at all. Cross-rack feeding is ordinary — shared PDUs, and a device
  moved between racks keeps its cabling until the plan is implemented — so the
  foreign cabling is now ignored and the device is attributed by U position
  instead, exactly as the builtin engine has always done. `_charge` also skips an
  unresolvable PDU or bank rather than raising, so no future caller can take a
  rack's distribution down this way. Affects `distribution_example` and
  `distribution_advanced_example`; the builtin (`distribution_mode = "builtin"`)
  was never affected.

  **If you copied an example script into `SCRIPTS_ROOT`**, replace that copy from
  this release — a pip upgrade does not touch files under `scripts/`.

## [0.21.0] - 2026-08-28

### Release Summary

Planned power feeds stop being write-only. They size a greenfield rack's
capacity bar, yet nothing in the UI listed them and nothing could remove one, so
a feed copied by mistake inflated the bar with nothing to point at — and "copy
from rack" only ever added, which meant copying from several racks in turn left
the union of all of them. Copying now replaces, and the feeds a design plans are
visible on the design page and removable from the rack power dialog.

### Changed

- **"Copy from rack" now replaces a rack's planned feeds instead of adding to
  them.** Only a rack-name *prefix* is retargeted when a feed is copied, so feeds
  named by any other scheme (`Utility A`) never collided — copying from several
  racks in turn left the union of all of them, and the rack's capacity bar read
  as the sum of every source ever clicked. The target now ends up fed exactly
  like the source. A feed whose name survives the copy keeps its row, and with it
  every PDU bound to it; the rest are removed and their PDUs unbound, with the
  count reported in the confirmation. A source rack with no feeds is a no-op
  rather than a wipe, since that is far likelier to be a mis-click than an
  instruction to strip the rack's supply.

### Added

- **Planned power feeds are visible and removable.** They size a greenfield
  rack's capacity bar but had no UI at all — no list, no detail page, no way to
  delete one short of deleting the whole design. Now: the rack power dialog lists
  the rack's planned feeds with a × on each, and the design detail page gains a
  **Planned power feeds** panel showing every feed, its rack, its electricals,
  the derated watts the capacity bar actually uses, and the PDUs bound to it.
  Removing a feed reports how many PDUs lost their binding.
- `DELETE /api/plugins/rack-design/designs/<pk>/planned-feed/` — addressed by
  `feed_id`, or by `rack_id` + `name`. Requires `change_design`, since it edits a
  design rather than deleting one.

## [0.20.2] - 2026-08-27

### Release Summary

A one-fix patch: a planned device could not be dragged into another rack. The
gesture was refused at the destination's drop gate, which admitted only tiles
carrying a real device — so the tile snapped back, silently, while the same drag
inside its own rack worked.

### Fixed

- **A planned device can be moved into another rack.** Dragging a newly added
  tile to a different rack did nothing: the destination rack's drop gate admits
  only tiles carrying a real device, so a planned add was refused, snapped back,
  and left no trace in the console or the payload — while moving it inside its
  own rack worked, which made the failure look arbitrary. A planned add is now
  adopted like any other tile, and it is the simplest cross-rack case there is:
  nothing physical stays behind, so there is no origin ghost, no homecoming and
  no rename prompt, and the placement keeps its role, tenant, full-depth flag and
  the name it was given. The same gesture works one layer down: a planned blade
  now moves between chassis columns, with its bay re-addressed by the destination
  column.

## [0.20.1] - 2026-08-27

### Release Summary

A one-fix patch for a save that could not complete. Removing planned blades and
dropping new ones into the same bays was rejected with
`unique_design_planned_bay is violated`, because the replacement was written
before the cancelled occupant was deleted.

### Fixed

- **Re-filling a bay freed in the same save no longer fails with a constraint
  violation.** A device bay is the only placement target carrying a per-design
  UNIQUE constraint, and the editor replays a cancelled add at the end of its
  bucket — the tile is gone from the grid, so it is appended from the capture
  taken when the × was clicked. The replacement therefore reached the database
  while the cancelled placement still claimed the bay, and the whole save was
  rejected instead of the bay changing hands. Items that free a slot are now
  written before items that claim one, decided server-side so that any client
  cancelling and re-filling in one submit is correct, and applied to every
  bucket rather than as a bay special case.

## [0.20.0] - 2026-08-26

### Release Summary

A chassis and a rack become one implementation. 0.19.0 shipped the chassis layer
by giving a chassis its own copy of the rack's add / move / remove / cancel
logic, and every one of the bugs fixed below lived in that copy rather than in
the behaviour it duplicated. The editor, the save view and the projection are now
written against a **Frame** — an enclosure — and its **Containers**, addressable
grids of slots: a rack Frame has a front and a rear container, half-unit steps and
full-depth pairing; a chassis Frame has one bay container, whole-bay steps and no
pairing. Nothing downstream knows which it is holding, so a fix to rack behaviour
is now automatically a fix to chassis behaviour. Also here: the layer is renamed
for the container rather than its contents, a chassis column is offered only for a
device that actually has bays, and bay occupancy is finally visible without the
editor. The abstraction is written up in `docs/editor-behavior-spec.md` §2.6.

### **Breaking Changes**

- The chassis layer moved from `/plugins/rack-design/designs/<pk>/blades/` to
  `/plugins/rack-design/designs/<pk>/chassis/`. The old path is gone; update any
  bookmark or link. Nothing stored changes — no migration, no data conversion.

### Changed

- **A chassis and a rack are now one implementation, not two.** The editor, the
  save view and the projection are written against a **Frame** (an enclosure) and
  its **Containers** (addressable grids of slots) — a rack Frame has a front and a
  rear container, half-unit steps and full-depth pairing; a chassis Frame has one
  bay container, whole-bay steps and no pairing. Nothing else in the pipeline
  knows the difference. This deleted the three copies of rack logic the chassis
  layer had accumulated (`chassisColumnPayload` in the editor, the 158-line
  `_reconcile_bay_item` in the save view, and duplicated state/label derivation
  in the projection), so a fix to rack behaviour is now automatically a fix to
  chassis behaviour. Documented in `docs/editor-behavior-spec.md` §2.6.
- **The blade layer is now the chassis layer**, named for the container rather
  than its contents: a bay holds a blade in a compute chassis, but equally a
  module in a patch panel or a shelf insert. The URL moves from
  `/designs/<pk>/blades/` to `/designs/<pk>/chassis/` and the rack view's button
  reads **Chassis**.
- **A device earns a chassis column by HAVING A BAY**, not by carrying
  `subdevice_role=parent`. The role is widely set on plain servers — one
  4475-device instance had 2306 such devices with no bay at all — and each drew
  an empty `0/0` column nothing could be dropped into. A planned chassis is
  filtered the same way, on its type's `DeviceBayTemplate`s.

### Added

- **Read-only chassis elevation** at `/designs/<pk>/chassis-elevation/`, linked
  from the rack elevation. Bay occupancy is not an editing concern: reviewing a
  plan should not require change permission or the editor. It renders the same
  columns from the same projection as the editable layer, via a shared
  `inc/chassis_column.html`.

### Fixed

- **A blade could not be moved between bays.** GridStack asks the destination
  grid whether it accepts a tile before it will hand it over, and that gate
  identified a blade by a DOM marker only the palette stamps — so a rendered
  blade tile answered "not a blade" and every chassis column refused its own
  kind. The tile snapped back, Save stayed disabled and nothing was logged.
  Containment decides now.
- **A blade removed from a bay rendered as nothing at all.** The removal was
  saved correctly but the chassis projection looked for blades only by their
  planned target, which a removal does not have, so the bay came back empty and
  the removal looked discarded. It is found through the device's own bay now.
- **A bay freed by a removal stayed blocked** for the rest of the session: the
  vacated-device set the editor is given skipped bay items entirely, and that set
  overrides the model's own fallback, so even an already-saved removal did not
  free its bay.
- **Cancelling an already-saved planned blade is no longer silently dropped.**
  The cancel now carries its own bay address, so it reaches the server instead of
  being discarded while Save reported success.
- **Moving a blade between two bays of one chassis is no longer reported as "no
  change".** The idempotency snapshot did not include the bay fields, so the two
  positions hashed identically; a round-trip that changes nothing still answers
  `304`, and one that moves a blade no longer can.

## [0.19.0] - 2026-08-25

### Release Summary

Device bays and blades. A chassis was previously an opaque tile: its bays were
invisible, its blades polluted the non-racked tray, and nothing about them could
be planned. This release models bays as first-class planning targets, adds a
**blade layer** for editing them, and closes a power-accounting gap that came
with them. Full design rationale in `docs/editor-behavior-spec.md` §10.

The organising idea is that **a bay is to a device what a unit is to a rack
face** — so a chassis is rendered as a rack, and the entire existing movement
pipeline (validate → confirm → commit, blocking, ghosts, homecoming,
cursor-governed placement) applies to blades with no new gesture code. The only
difference is the step: one bay instead of half a rack unit.

### Added

- **Blade layer** at `/plugins/rack-design/designs/<pk>/blades/` — every chassis
  in a design's scope rendered as a column of bays, driven by the unmodified rack
  editor. Reached from a **Blades** button in the rack view, which appears only
  when the design's scope actually contains a chassis.
- **Plan a blade into a bay**, in both cases that exist: a chassis already in
  DCIM (`target_bay`), and a chassis this same design adds (`parent_placement` +
  `target_bay_name`, validated against the type's `DeviceBayTemplates`, since no
  bay rows exist until apply).
- The layer's **palette is filtered to child device types**, so a rack-mountable
  device can never be offered as a blade; conversely a blade is refused by every
  rack grid. Container/type agreement is enforced at the drop gate, not after
  the fact.
- **Chassis visibility** — per-user show/hide in the layer's Chassis panel, the
  blade-layer twin of the Design-racks panel (`HiddenDesignChassis`).
- **Bay occupancy on the rack page**: a chassis tile's hover card reports
  `N of M bays used` plus one line per occupant — in the editor and on the
  read-only projected elevation alike.
- The **power heatmap** toggle is now on the read-only elevation too, not only
  in the editor. It is a pure view layer, so the same script drives both; the
  on/off choice is shared between the two pages.
- REST and GraphQL expose `parent_placement`, `target_bay` and `target_bay_name`,
  with `?target_bay_id=` and `?parent_placement_id=` filters; the create/edit,
  import, bulk-edit and filter forms all carry them, and the edit form's chassis
  selector scopes the bay picker (an unfiltered `DeviceBay` list is unusable).
- **Save-before-switch**: changing layer with unsaved work asks
  *Cancel / Discard / Save and switch* instead of relying on the browser's
  generic unload warning.

### Fixed

- **Blades no longer render in the non-racked tray.** A child device keeps its
  `rack` but is forbidden a position, so it satisfied the tray's
  `position__isnull=True` test and drew alongside real 0U accessories. On a
  populated instance that is every blade in the rack.
- **Blade power is now accounted for.** Chassis and blades can never both be
  counted: if the chassis has a resolvable draw it wins and its blades are marked
  as already included; if it has none, the blades roll up into it. This mirrors
  what core achieves through cabling (`PowerPort.get_power_draw()` aggregates
  downstream only when the port carries no value of its own) — a plan has no
  cables, so the same result is derived from containment.

### Changed

- Bays are addressed by **number** rather than name in the UI. Real bay names are
  a mix of `slot3`, `top-left` and `pci9`, so the index is the only stable
  identifier; the real name stays in the tooltip.
- The save payload gains a fourth per-rack bucket, `bays`, processed after the
  face buckets so a blade can reference a chassis created by the same submit.
  An `existing` bay item is reconciled against reality server-side, exactly as
  face items are — the client never claims something moved.
- **`target_face` is now preserved for full-depth devices.** Saving a layout
  used to blank it, a dedup trick from when the editor POSTed one copy of a
  full-depth tile per face; the editor has since sent a single item, so the
  stored face was being discarded for nothing. API consumers diffing on
  `(device, position, face)` no longer miss every full-depth row. A client that
  still submits both copies gets one placement as before, and the face it was
  mounted on wins rather than whichever copy arrived last.

### Migrations

- `0008` — `DesignPlacement.parent_placement` / `target_bay` / `target_bay_name`,
  plus two uniqueness constraints (one design may claim a given bay once).
- `0009` — `HiddenDesignChassis`.

Both are additive; no existing data is rewritten.

## [0.18.0] - 2026-08-24

### Release Summary

Minor release fixing REST API conformance. Foreign keys on the Design, DesignGroup and
DesignPlacement endpoints were serialized as bare integers instead of the nested brief
objects every core NetBox endpoint returns. They now match core. A latent N+1 on the
placements endpoint was found and fixed in the same pass, so the list endpoints issue
*fewer* queries than before despite returning more data.

### **Breaking Changes**

- **REST API foreign keys now return nested objects instead of integers.** A response
  field that previously read `"site": 345` now reads
  `"site": {"id": 345, "url": "...", "display": "AMS1", "name": "AMS1", "slug": "ams1", ...}`.
  Affected fields: `Design.site` / `group` / `root` / `based_on` / `depends_on`,
  `DesignPlacement.design` / `device` / `device_type` / `device_role` / `tenant` /
  `target_rack`, and `DesignGroup.parent`.

  *Migration:* read the id from the nested object — `design["site"]["id"]` instead of
  `design["site"]`. Alternatively request `?brief=true`, or select the fields you need
  with `?fields=`. **Writes are unaffected**: POST and PATCH still accept a bare primary
  key (or an attributes dict) for every one of these fields, so no client that only
  writes needs to change.

### Fixed

- Foreign keys across all three API serializers were listed in `Meta.fields` but never
  declared, so DRF fell back to `PrimaryKeyRelatedField`. They are now declared with the
  core `Serializer(nested=True)` pattern; the four self-referential relations
  (`Design.root` / `based_on` / `depends_on`, `DesignGroup.parent`) use dedicated
  `WritableNestedSerializer` classes, matching how core handles its own recursive
  relations.
- `DesignPlacementViewSet` had no `select_related`/`prefetch_related` at all. Because
  NetBox's base viewset prefetches every relation listed in `Meta.fields` regardless of
  whether the serializer declares it, those rows were already being fetched and then
  discarded. All three viewsets now join their foreign keys up front.

### Changed

- Query-count baselines updated for the reduced query counts on the list endpoints:
  `designplacement` 15 → 12, `designgroup` 13 → 12, `design` 16 → 15.

## [0.17.2] - 2026-08-21

### Release Summary

Certification-readiness release. No functional change — the plugin behaves exactly as
0.17.1 does on NetBox 4.4 through 4.6. This corrects the package metadata a NetBox Labs
certification review reads first: the distribution described itself with the generator's
placeholder one-liner, the README compatibility matrix had fallen behind the shipped
versions, and the maturity classifier still said Alpha.

### Changed

- **Package description** replaced the cookiecutter placeholder
  (`"NetBox plugin for Rack Design."`) with a real value statement, in both
  `pyproject.toml` and `PluginConfig.description`. This is the summary shown on the PyPI
  project page and in NetBox's own installed-plugins list.
- **README compatibility matrix** now lists `0.17.x`; it previously stopped at `0.16.0`,
  three releases behind. `COMPATIBILITY.md` was already current.
- **Maturity classifier** raised from `Development Status :: 3 - Alpha` to
  `4 - Beta`, matching the shipped feature set.

## [0.17.1] - 2026-08-21

### Release Summary

Packaging-metadata release. No plugin code changed — the plugin behaves exactly as
0.17.0 does on NetBox 4.4 through 4.6. This ships the project links that point users at
the newly completed documentation: the full ten-part video tutorial, and a `Documentation`
URL that now resolves to the published docs site rather than the raw README on GitHub.

### Changed

- `[project.urls] Documentation` now points at <https://ravenrs.github.io/netbox-rack-design/>
  (the MkDocs site) instead of the README blob on GitHub, so the PyPI sidebar links to the
  real documentation.
- README links the complete ten-part tutorial playlist alongside the existing two-minute
  quick tour.

### Added

- `[project.urls] Tutorial` — the full tutorial playlist, surfaced as a link on the PyPI
  project page.

## [0.17.0] - 2026-08-20

### Release Summary

The power-distribution engine's three modes are now cleanly separated. `builtin` is
purely native: it derives breaker banks from PDU outlet names and feed bindings and
reads **no custom fields at all**, so a public install can no longer be steered by
field names that only mean something inside one organisation. Everything
custom-field-driven — a rack power ceiling, a PDU orientation — belongs to
`distribution_mode = "script"`, which is the tier meant to carry a site's own
conventions. Alongside that, greenfield planning gets a real shortcut: the rack power
dialog can now clone a sibling rack's feeds, and the editor's power bar stops waiting
for a Save to tell the truth — capacity now refreshes live when feeds change, exactly
like the per-bank chips already did.

### **Breaking Changes**

- **`distribution_mode = "builtin"` no longer reads the `power_limitation` or
  `pdu_location` rack custom fields.** Previously the built-in engine read both
  literally off `rack.cf`, which shipped one organisation's field names in a public
  plugin — the documentation had always claimed the opposite. In `builtin` the
  unit-to-bank split now always uses the fixed default direction, no rack ceiling is
  applied (`Distribution["rack"]["power_limitation_w"]` is always `null`) and
  `Distribution["pdu_location"]` is always `null`. Bank-overload alarms, which come
  from feed data alone, are unchanged.
  **Migration:** if you relied on either field, set
  `distribution_mode = "script"` with `distribution_script` pointing at
  `netbox_rack_design.distribution_example.build` (or your own copy), and declare the
  fields in `planning_fields` **including each entry's `source`**, e.g.
  `{"key": "power_limitation", "type": "number", "source": "cf.power_limitation"}`.
  An entry without `source` renders in the dialog but resolves to nothing.
- **The per-design rack power override (`DesignRackPower`) is applied in `script` mode
  only.** It exists to feed a script; in `builtin` it is ignored.
- **The rack and PDU planning dialogs only show their custom-field inputs when
  `distribution_mode = "script"`.** In `none`/`builtin` the rack dialog keeps its
  "Copy from rack" row and explains why the manual fields are hidden, rather than
  offering inputs nothing would read.

### Added

- **Copy a rack's power from a sibling rack.** The rack "Power" dialog's *Copy from
  rack* mode now clones the source rack's feeds as planned `DesignPowerFeed` rows, not
  just its custom fields — a greenfield rack in a row inherits the supply of the
  provisioned rack next door in one step. Copied feeds are renamed for the rack they
  land on (`R101-A` → `R103-A`; any other naming scheme is kept verbatim) and upsert by
  `(design, rack, name)`, so re-copying updates instead of duplicating. New endpoint
  `POST /api/plugins/rack-design/designs/<pk>/copy-feeds/`; `power-source/?kind=rack`
  now also reports the source rack's feeds so the dialog can preview them (falling back
  to the design's planned feeds when the source rack has none).
- **`recompute-distribution` also returns each rack's power summary** (`power` block:
  draw, capacity, utilisation, thresholds), which is what makes live capacity possible —
  the derating and phase maths stay on the server.

### Fixed

- **The rack power bar's capacity is now live.** Adding or copying a planned feed used
  to leave the denominator at whatever the page was rendered with until the next Save
  or reload, so a greenfield rack could read `1000 / 1000 W` in red while its banks
  were green. Capacity and thresholds now refresh from the server on the same signal
  the per-bank chips already used.
- **A freshly dropped PDU no longer paints as the unknown-draw hatch.** The palette
  resolved a device type's draw without knowing the role it would be given, so a PDU —
  whose inlet template carries no draw — came back "unknown", while the very same PDU
  after Save read a known 0 W like passive gear. `device-type-power/` now accepts an
  optional `role_id` and applies the projection's `power_exclude_roles` rule, and the
  palette re-stamps its rows when the Role selector changes.

### Changed

- `docs/pdu-distribution-spec.md` and `docs/power-distribution.md` now describe the
  three tiers as implemented, scope every "the plugin never hardcodes your custom-field
  names" claim to the script tier, document `source` as script-only, and correct
  `pdu_location`, which was documented as a PDU-level custom field when it is
  rack-level.

## [0.16.0] - 2026-08-19

### Release Summary

NetBox 4.5 and 4.6 support. The plugin declared `max_version = "4.4.99"`, so every
install on a current NetBox refused to load with *"requires NetBox maximum version
4.4.99"* (#25) — two minor versions of NetBox had shipped past the supported ceiling.
The range is now 4.4.0 – 4.6.99, served by a single codebase: all version-specific
knowledge lives in one new module, `netbox_rack_design/compat.py`, and CI tests every
declared version rather than only the oldest. Almost nothing in the plugin itself had
to change — two core APIs moved and the rest was test scaffolding. Also fixes a rack
editor bug, unrelated to NetBox versions, where a dragged device landed away from the
green landing preview whenever the browser was not at 100% zoom.

### Added

- **NetBox 4.5.x and 4.6.x are supported** (`min_version` 4.4.0, `max_version`
  4.6.99). Verified end to end against 4.4.8, 4.5.10 and 4.6.8: the plugin's own test
  suite, a missing-migration check, and the Playwright editor suites all pass on each,
  the last of these against a real database migrated forward from 4.4.
- **CI now tests the whole declared range.** `.github/workflows/ci.yaml` takes NetBox
  as a matrix dimension (4.4.8 and 4.5.10 on Python 3.12/3.14, 4.6.8 on 3.12/3.13/3.14,
  `fail-fast: false`), so the declared compatibility range can no longer drift away
  from what is actually tested.
- **`netbox_rack_design/compat.py`** — the single module allowed to know which NetBox
  it runs on. Each shim uses capability detection rather than a version comparison, so
  it keeps working when the next minor moves things again.

### Fixed

- **A dragged device now lands where the cursor points at any browser zoom level.**
  Away from 100% zoom, a device could land up to several rows from the green landing
  preview — worst on multi-U devices. Two row computations disagreed: the plugin
  measures the grid's real rect, so its row height tracks zoom (11px at 100%, 12.1 at
  110%, 16.5 at 150%) and matches the pointer coordinates, while GridStack uses a fixed
  `cellHeight` of 11 CSS px and is therefore only correct at 100%. Placement accepted
  the engine's row whenever the pointer fell inside the tile as parked — comparing rows
  measured in those two different spaces — so under zoom it committed the wrong one
  while the preview showed the right one. The cursor's row is now always authoritative.
- **A whole-U device no longer lands on a half-unit boundary.** The in-tile grab offset
  and the pointer row are floored independently, so drifting half a unit vertically
  during a move flipped the parity. Whole-unit alignment is now restored for a device
  that *started* on a whole unit, leaving genuinely half-unit-mounted devices (a rack
  with half-unit mounting holds devices at x.5) on their own rows.
- **API tokens minted by the test suite work on 4.5+.** From NetBox 4.5 a new token is
  a v2 token whose secret is never stored, so `Token.key` — the entire secret in 4.4 —
  became a non-sensitive identifier and the legacy header form returns
  `403 Invalid v1 token`.

### Changed

- **`Design` no longer inherits `PrimaryModel`.** That base contributed only
  `description` and `comments`, both now declared on `Design` itself with identical
  definitions, and NetBox 4.5 additionally gave it an `owner` foreign key. Inheriting a
  base whose field set changes between NetBox minors would make the plugin's own schema
  version-dependent — the same model would need a migration on 4.5+ that is invalid on
  4.4. **No migration and no database change on any supported version:** the two columns
  were already materialized in migration 0001, and `makemigrations --check` is clean on
  4.4.8, 4.5.10 and 4.6.8 alike. The REST API and the GraphQL filter input are both
  unchanged; `Design` gains no `owner` field.
- The `Framework :: Django` classifiers now list 5.2 and 6.0, the Django versions the
  supported NetBox range ships (4.4/4.5 → 5.2, 4.6 → 6.0).

### Notes for GraphQL consumers

Independently of this plugin, **NetBox 4.5 changed GraphQL filter syntax**: queries
filtering by object ID or enum must now use a lookup, so `id: 123` becomes
`id: {exact: 123}` (netbox-community/netbox#19338). On 4.5+ the `id` field of every
plugin filter input is therefore `IDComparisonFilterLookup` rather than `ID`. Queries
written against 4.4 need updating when the NetBox host is upgraded; the plugin's filter
fields themselves are unchanged.

## [0.15.3] - 2026-08-18

### Release Summary

Bug-fix release for the editor's power view. Four defects made per-rack and
per-bank power read wrong in specific topologies: a greenfield rack sized only by
planned feeds fell back to the flat capacity default and painted itself critical-red
while its own bank chips read green; the rack chassis kept the colour the page had
loaded with after a live NetBox theme switch; a cross-rack move dropped the moved
devices' draw out of both racks' bank chips; and a rack fed only by a B-named feed
charged single-corded devices nowhere. The per-bank chip strip now also tracks the
rack block width, so it narrows in step with the Front/Rear face toggles instead of
sitting as a fixed-width island under the bar.

### Fixed
- **A greenfield rack's power bar now counts its planned feeds.** `_rack_capacity_w()`
  only summed real `dcim.PowerFeed` rows, so a rack whose supply exists only as
  planned `DesignPowerFeed`s fell back to the flat `power_capacity_default_w` — the
  bar painted the rack critical-red while its own per-bank chips, sized from those
  same planned feeds, read comfortably green. Planned feeds now contribute at the
  same derating NetBox applies to a real feed (`POWERFEED_DEFAULT_MAX_UTILIZATION`),
  so a planned 2 × 230 V × 32 A rack reads 11,776 W just like its provisioned
  neighbours. The flat default remains the fallback only when a rack has no feeds of
  either kind.
- **The rack chassis now follows a live theme switch.** Switching NetBox's colour
  mode left the rack drawn in the colour the page had LOADED with — a black rack
  on a white page — until the next reload. NetBox's navbar toggle restyles the
  page by setting `data-bs-theme` on `<body>` while `<html>` keeps its
  server-rendered value, so the plugin's palette (declared on `:root` only) never
  re-resolved. Both palettes are now declared wherever the attribute lands.
- **A cross-rack move now carries its power into the destination rack's banks.**
  Dragging devices from one rack to another dropped their draw from the per-bank
  chips of both racks: a real device keeps its cabling to the SOURCE rack's PDU
  until the design is implemented, so the destination charged it to a PDU absent
  from its own topology and skipped it. Cabled attribution is now scoped to PDUs
  in the rack being computed; cabling that leads elsewhere falls back to
  U-position attribution, exactly like a planned add. Cabling inside the rack
  still wins over position.
- **A rack with no `a` leg no longer swallows single-corded devices.** Leg letters
  come from the feed names ("Feed B" → `b`), so a rack fed only by a B-named feed
  had no `a` leg and a single-PSU device was charged nowhere. Such a device now
  charges the legs the rack actually has.

### Changed
- **Per-bank chip strip now tracks the rack block width.** The rack power bar
  already resized with the Front/Rear face toggles while the bank chips stayed a
  fixed-width island, so the two read as unrelated widgets. The PDU columns now
  share the block width (growing from a 8.5rem basis), so hiding a face narrows
  the strip in step with the bar above it and the columns stack instead of
  leaving empty space.

## [0.15.2] - 2026-08-18

### Release Summary

Bug-fix release for the editor's power heatmap. Three defects made the power view
show less than it knew: device tiles were tinted by their PDU bank's utilization
rather than their own draw, so every device in a hot bank painted identically red
and a 300 W switch was indistinguishable from an 800 W server; the "unknown draw"
hatch never rendered, so a device with an unmeasured power port looked exactly like
passive gear with no ports at all; and the per-bank chip strip was still hidden
behind the heatmap toggle despite 0.15.0 intending it to be always visible.

### Fixed
- **Heatmap shows each device's own consumption.** A tile's fill and colour are now
  its draw as a share of the rack's biggest consumer (green → red), instead of
  restating its bank's utilization. Bank health is carried by the per-bank chips,
  where it is not repeated once per tile. Feed A/B edge accents are unchanged.
- **Unknown-draw hatch now renders.** The neutral hatch for a device with a power
  port but no known draw was defined but never applied: the generic heatmap rule
  carried two `:not()` guards, putting it at a higher specificity than the
  unknown-draw rule, and both used `!important`. The unknown rule now carries the
  same guards and wins.
- **Per-bank chip strip is always visible.** 0.15.0 made the strip render
  unconditionally but left the CSS rule gating it on the heatmap toggle, so it
  stayed hidden until the toggle was on. The strip now follows the power bar and is
  always shown; the heatmap toggle controls per-tile shading only.

## [0.15.1] - 2026-08-17

### Release Summary

Bug-fix release: the in-app documentation ("?") help link on the plugin's object
detail pages now resolves. NetBox's default `docs_url` points at
`/static/docs/models/…`, which only exists for NetBox's own core docs — a plugin's
docs are not built into that path, so the help link 404'd. Following the plugin
developer guide (Database Models: *"Plugin models can override this to return a
custom URL … your plugin's documentation"*), the models now override `docs_url` to
the plugin's hosted docs site.

### Fixed
- **In-app documentation link.** `Design`, `DesignGroup`, and `DesignPlacement` now
  override `docs_url` to point at the hosted docs
  (https://ravenrs.github.io/netbox-rack-design/), so the object detail page's help
  link opens the documentation instead of returning a 404.

## [0.15.0] - 2026-07-31

### Release Summary

Feature release making the editor's per-PDU/per-bank power distribution **live**. The
PDU bank chips (and the bank heat-tint) previously recomputed only when you pressed
Save, while the total power bar updated in real time — so an edit left the two power
views disagreeing until a save. Now the bank distribution recomputes on every
add/remove/move, exactly like the bar. Rather than duplicate the distribution logic
in the browser (bank assignment depends on real cabling and, in script mode, a custom
`distribution_script`), the editor re-runs the very same server engine over the
unsaved layout through a new read-only endpoint that applies the edit inside a
rolled-back transaction — live figures, nothing persisted. The bank chip strip is now
always visible (not gated on the heatmap toggle). Read-only over DCIM throughout.

### Added
- **Live per-bank power distribution in the editor.** The PDU bank chips now refresh
  on every tile add/remove/move, matching the always-live power bar. A new read-only
  `POST /api/plugins/rack-design/designs/<id>/recompute-distribution/` action re-runs
  the distribution engine (builtin or a custom `distribution_script`) over the current
  unsaved layout — applying it through the save-layout reconciliation inside a
  rolled-back transaction — and returns the fresh per-rack distribution without
  persisting anything. The per-bank chip strip is now always-on. See
  [docs/pdu-distribution-spec.md](docs/pdu-distribution-spec.md).

### Fixed
- **The "Power heatmap" toggle now survives a Save.** Saving reloads the editor, which
  previously reset the heatmap toggle to off, forcing you to re-enable it after every
  save. The toggle state is now remembered and restored on load.

## [0.14.0] - 2026-07-31

### Release Summary

Feature release for the shipped naming/distribution examples. The example modules
(`naming_example`, `distribution_example`, `distribution_advanced_example`) now each
carry a small `extras.scripts.Script` subclass, so they register as first-class
NetBox Scripts: you can add them straight through **Customization → Scripts → Add**
(or by dropping the file into `SCRIPTS_ROOT`) and open/edit them via the **Edit**
button, instead of hand-wrapping the example before it will appear. The naming logic
is unchanged — the plugin still calls the module-level `build_name` / `build`
directly, and those remain strictly read-only over DCIM.

### Added
- **Example scripts are now addable/editable from the UI.** `naming_example.py`,
  `distribution_example.py`, and `distribution_advanced_example.py` each ship a
  thin `Script` subclass (`DeviceNamingScript` / `PowerDistributionScript` /
  `PowerDistributionAdvancedScript`) whose `run()` is a documented no-op — the
  class exists only so NetBox lists the module under Customization → Scripts, where
  it can be viewed and edited. Referencing the example (`scripts.<module>.build_name`
  / `.build`) drives naming/distribution live, with edits picked up on the next
  preview and no restart. See [docs/device-naming.md](docs/device-naming.md).

## [0.13.5] - 2026-07-29

### Release Summary

Bug-fix release for the multi-rack editor. On a planned-add tile that is only a
single rack unit tall, the inline proposed-name input was clipped away by the
tile's height, leaving no way to override the auto-generated name. The name is now
shown compactly on the tile and edited through a small pencil affordance that pops
the input out over the tile, so renaming works at any tile height.

### Fixed
- **Editable name on 1U add tiles.** A planned-add tile stacked its device-type
  label above an always-visible name input; on a 1U-tall tile the input fell
  outside the clipped tile and could not be seen or focused. The tile now displays
  its assigned name and exposes a hover-revealed pencil (bottom-left, clear of the
  remove × and the PDU power control) that opens the name field as a pop-out
  overlay, keeping it fully usable regardless of tile height.

## [0.13.4] - 2026-07-29

### Release Summary

Bug-fix release. The "Placements" column on the Designs list (and the "Designs"
column on the Design Groups list) always showed 0, regardless of the real number
of related records. The list views now annotate the counts the tables display.

### Fixed
- **Designs / Design Groups list counts no longer show 0.** The `DesignListView`
  and `DesignGroupListView` querysets now annotate `placement_count` /
  `design_count` (via `count_related`), which the `LinkedCountColumn` reads —
  previously the columns rendered 0 for every row because the accessor was never
  annotated. This also fixes the embedded designs table on a Design Group's detail
  page (rendered through the same design list).

## [0.13.3] - 2026-07-23

### Release Summary

Bug-fix release for the multi-rack editor. Fixes a layout-on-reload defect where
a planned device placed on rows freed by a within-rack move could be pushed down
the rack after saving and reloading the editor. Also adds project screenshots to
the README.

### Fixed
- **Reclaimed rows no longer shift on reload.** When a device was moved within a
  rack, the rows it vacated (marked by a move-out ghost) could be reclaimed by a
  planned add or move-in. On reload, GridStack's initial collision pass ran over
  the still-attached ghost before it was detached and collapsed, pushing the
  reclaiming device down the rack. The editor now brackets that initial grid
  hydration in the same push-suppression the live-editing gestures already use,
  so reloaded placements render exactly where they were saved. Live editing was
  never affected.

### Changed
- **README now includes screenshots** of the editor, power heatmap, device
  palette, and dialogs.

## [0.13.2] - 2026-07-23

### Release Summary

Consistency release: two small power-model fixes plus a documentation refresh.
The per-bank power heatmap now honors the same `power_warn_pct` /
`power_critical_pct` thresholds as the rack-level bar, and the power projection
now reports which powered devices lack draw data. The README, roadmap, and site
navigation are brought up to date with what has actually shipped.

### Fixed
- **Per-bank heatmap thresholds are now configurable.** The `builtin`
  distribution mode previously hard-coded its warn/critical bank-state thresholds
  at 80% / 100%; it now reads `power_warn_pct` / `power_critical_pct` from
  `PLUGINS_CONFIG` (same defaults, same keys as the rack-level power bar), so both
  surfaces color consistently.
- **Power projection reports unknown-draw devices.** `_project_power` now returns
  `unknown_draw_count` / `unknown_devices` — powered devices that carry no draw
  value (counted as 0 W but flagged), distinct from the existing
  `unconnected_*` cabling-gap flag — matching the documented output contract.

### Changed
- **Documentation refreshed to the shipped state.** The README feature list and
  roadmap now reflect the delivered naming engine, power projection, and PDU power
  distribution (previously listed as planned); the compatibility and configuration
  tables are current. The docs site navigation exposes the power/distribution
  guides and specs.

## [0.13.1] - 2026-07-23

### Release Summary

Documentation and reference release for the power-distribution feature. Adds a
user-facing how-to guide and a second, richer example distribution script, and
scrubs a stray site-specific custom-field name from the shipped reference
example's docstring. No behavior or API changes.

### Added
- **`docs/power-distribution.md`** — a user-facing how-to for power distribution
  (modeled on `docs/device-naming.md`): the three `distribution_mode`s
  (`none` / `builtin` / `script`), the two universal conventions, feed binding and
  planned PDUs, the `planning_fields` custom-field bridge, writing/adapting a
  distribution script, and verify-on-instance steps. The design spec
  (`docs/pdu-distribution-spec.md`) remains the deep reference.
- **`netbox_rack_design.distribution_advanced_example.build`** — a second,
  richer reference script alongside the minimal `distribution_example`. It
  demonstrates the customization surfaces the minimal example leaves out: a
  computed topology `scheme` label (from the per-PDU bank-count signature), a
  per-PDU `pdu_scheme` override read via `planning_fields["pdu"]`, and
  WARN/CRITICAL bank thresholds read from plugin config (`power_warn_pct` /
  `power_critical_pct`). Reuses the shared distribution helpers; read-only.

### Changed
- The shipped `distribution_example` docstring no longer references a
  site-specific custom-field name in its illustrative `planning_fields` mapping
  (now a generic placeholder).

## [0.13.0] - 2026-07-22

### Release Summary

Feature release adding **universal PDU power distribution** — a per-PDU / per-bank
power heatmap that works out of the box, plus a full planning model for
greenfield racks. Distribution now has three tiers: a zero-config **`builtin`**
mode driven by two universal naming conventions, a **custom-field bridge** for
sites that keep power limits in custom fields, and a **script** mode for bespoke
logic. A planned PDU binds to a real or planned **power feed** (mirroring
NetBox's native power model) so its breaker is sized the same way whether the
rack is provisioned or greenfield. Fully backward-compatible and read-only over
`dcim`.

### Added
- **Universal PDU power distribution + per-bank heatmap** (see
  `docs/pdu-distribution-spec.md`). The `distribution_mode` config key now
  accepts `builtin` alongside `none` (default) and `script`:
  - **`builtin`** computes a real per-PDU/bank distribution with **no script**,
    from two documented conventions — the bank is the first segment of the outlet
    port name (`1/1` → bank 1), and a PDU's feed-leg comes from the feed it is
    **bound** to (below), not from name parsing.
  - The editor's power heatmap colors each PDU bank as a filled health bar
    (load-vs-breaker, overload = hard red), feed-leg-colors the per-PDU column
    headers (A/B), and gives each consumer tile an A/B feed edge for the leg(s) it
    lands on.
  - The shipped reference `netbox_rack_design.distribution_example.build` runs the
    same algorithm as a copyable script. A missing / unimportable / non-callable /
    raising script falls back to the per-device heatmap and never errors the page.
- **Power feed model + binding for planned PDUs.** A PDU sizes its breaker from
  the feed it draws from — a real cabled `dcim.PowerFeed`, or a new plugin-side
  **`DesignPowerFeed`** (name, V/A/phase/supply, scoped per design + rack) for
  greenfield racks. A planned PDU binds to one via two nullable FKs on
  `DesignPlacement` (`real_power_feed` / `planned_power_feed`, mutually
  exclusive), so the read path is uniform (real or planned → "read the bound
  feed"). The bind-to-feed dialog opens on PDU add — real feeds first, with a
  "define planned feed" fallback — and a per-rack **Power** button (shown when a
  rack has no real feeds) manages planned feeds and rack overrides.
- **Custom-field bridge (`planning_fields`).** A config schema maps a site's
  **custom fields only** (e.g. `power_limitation`, `pdu_location`) into the
  planning dialogs and the script's view of the rack — native fields are always
  read directly, never configured. A planned PDU can also **reference a real PDU
  device** (`power_source_device` FK) to inherit its custom fields live, or carry
  them manually in `power_config`. Rack custom-field overrides persist per design
  in `DesignRackPower`.
- New read-only API actions back the dialogs: `GET feeds/`,
  `GET/POST planned-feed/`, `GET/POST rack-power/`, and `GET power-source/`
  (rack copy-from). Save-layout items carry the feed binding and cf-source.
  Migration `0007` adds `DesignPowerFeed`, `DesignRackPower`, the binding /
  cf-source FKs, and `DesignPlacement.power_config`.
- Debug logging across the whole path (PDU + feed resolution, unit→bank map,
  per-device charge, overrides, every graceful fallback), plus a dev-only
  frontend tracer for the dialogs and heatmap.

### Notes
- Backward-compatible: `distribution_mode` defaults to `none` (today's per-device
  heatmap); no `dcim` writes, no design dirty flag. All planning data is stored
  by the plugin alongside the design and never written back to real records.

## [0.12.0] - 2026-07-17

### Release Summary
**Live power for new gear, and a hardened editor** — a minor, fully backward-compatible release. A freshly dropped catalog device now shows its projected draw **immediately** (per-rack bar + heatmap, no save/reload), via a new read-only power endpoint the palette reads. Alongside it, a batch of editor placement fixes found through real-mouse testing: cross-rack homecoming and reject paths no longer corrupt device identity or fling tiles to the wrong rack, a device dropped onto a just-removed slot now correctly shows **its** name and heat, and the power heatmap no longer colors a device that is leaving. No schema changes, no migrations.

### Added
- **Palette-add-live power**: a freshly dragged catalog device now shows its projected draw **immediately** in the per-rack power bar and heatmap — no save/reload needed. The catalog palette (search results *and* the favorites/quick-access list) fetches each device type's projected draw from a new read-only endpoint, `GET /api/plugins/rack-design/device-type-power/?id=…`, and stamps it on the row, so the drop carries the same `data-draw-w` / `data-power` a real device tile does. Draw is resolved by the same logic the projection uses for a planned add (`device_type_power_summary`), so the live figure matches what Save + reload produces. Endpoint is authenticated, read-only, and performs no writes.

### Fixed
- **Cross-rack homecoming no longer corrupts a device**: dragging a moved device back toward its home rack used to leave it half-reverted (existing in the DOM but still a "ghost" in state), so the next cross-rack drag mis-indexed another rack's state array and duplicated/corrupted an unrelated device. A homecoming now fully revives the device as a proper existing entry (kind + placement), so re-drags are adopted cleanly.
- **Illegal drops return to the last valid position**: a rejected cross-rack drop returns the device to the rack/slot it was dragged **from**, not its true origin; a re-dragged reloaded move keeps its move identity instead of reverting the whole move; and a within-rack illegal move of a just-reclaimed tile stays in that rack instead of flying to its home rack.
- **Homecoming onto an occupied slot is rejected** (validated against the actual drop rows, not the free origin slot), so it can no longer commit an overlap.
- **Name restored on homecoming**: returning a device to its own origin shows its real name again instead of keeping the move's `"<design>-<name>"` overlay.
- **Heatmap on a leaving device**: a tile flagged for removal or displaced (being replaced) no longer keeps a stale heatmap fill — a device that is leaving is not colored.
- **Drop onto a removed slot**: the device that takes a removed/displaced device's slot now renders above it, so **its** heat fill and name are shown (previously the collapsed tile stacked on top and blanked the new occupant's name).

### Changed
- Added a dev-only, opt-in editor drag-lifecycle tracer (`window.__rdDragTrace`), gated behind `DEBUG` / Django Debug Toolbar so it is never reachable on a production build.

## [0.11.1] - 2026-07-14

### Release Summary
**Power that moves with your plan** — a patch release that makes the power projection live and fixes a name-revert bug.

The per-rack power bar and the heatmap now recompute **in the browser as you shuffle hardware** — moving a device between racks drops the source rack's projected draw and raises the destination's instantly (and re-colors ok/warn/critical), flagging a device for removal drops it, all with no save/reload. Draw travels with each tile, so cross-rack moves are reflected on both racks at once. (A freshly-dropped catalog add still shows its draw after save/reload — the palette has no client-side power data yet.)

### Fixed
- Cancelling a move (× on a `move_in` tile) now reverts the tile's **name** as well as its position — previously the device snapped back but kept showing the move's `"<design>-<name>"` proposed name instead of its real name.
- "Power heatmap" toolbar toggle: restored the spacing between its checkbox and label (it sits outside the state legend and missed the legend's gap).

### Changed
- Power projection is now recomputed live client-side (per-rack bar + heatmap) on every in-editor add / move / removal, driven by per-tile `data-draw-w` and a MutationObserver — no server round-trip. Still strictly read-only.

## [0.11.0] - 2026-07-13

### Release Summary
**Power projection** — a minor, fully backward-compatible release that shows how much power a planned design will draw, per rack, before it is applied to DCIM.

The editor and the read-only elevation now compute a projected power draw for the *planned* world (existing − removes + adds, moves reassigned) and compare it to a capacity, reusing NetBox's own power model (`PowerPort` / `PowerPortTemplate` draw, `PowerFeed.available_power`). Each rack gets an always-visible **power bar** — projected draw / capacity / utilization %, colored ok/warn/critical — and a **"Power heatmap"** toggle that turns every device tile into a per-device consumption "health bar" (the rack's biggest consumer fills red, the rest proportionally toward green). Hovering a power bar lists the count of devices whose power ports aren't cabled and **pulls those tiles out of the rack** to point them out; hovering any device shows its PSUs and allocated power on the info card. PDUs are treated as power infrastructure (they distribute, not consume) and excluded from the total; passive gear with no power ports is skipped, not flagged. No schema changes, no breaking changes, no migrations.

### Added
- **Per-rack power bar** (`docs/power-projection-spec.md`): projected draw vs. capacity with ok/warn/critical thresholds, on both the editor and the read-only elevation. Capacity comes from the rack's `PowerFeed`s when modeled, else a configurable fallback.
- **Power heatmap toggle**: per-device fill bars, max-normalized per rack (biggest consumer = 100% red → green), suppressing the normal state tints while active and restoring them exactly when off.
- **Connection-gap flag**: the bar shows a ⚠ count of devices whose power ports are not cabled; hovering the bar highlights ("pulls out") those tiles.
- **PSU detail on the device hover card**: one row per power port (name + allocated draw), a total, and an `(nc)` marker for uncabled ports.
- **Naming**: a stock-runnable example naming module and graceful fallback (from 0.10.0) remain; power adds a new read-only projection layer only.
- Config keys: `power_capacity_default_w`, `power_draw_basis` (`allocated`/`maximum`), `power_warn_pct`, `power_critical_pct`, `power_exclude_roles`.
- Backend tests for draw resolution / capacity / thresholds / PDU exclusion / passive-skip / connection flag, and a deterministic Playwright e2e suite for the bar, heatmap, pull-out and PSU hover.

### Changed
- Power is strictly read-only: computed server-side in `projection.py` and surfaced on the projection bundle; it never writes to `dcim` and never marks the design dirty.

## [0.10.0] - 2026-07-10

### Release Summary
**Naming you can trust and placement you can see** — a minor, fully backward-compatible release that hardens the naming engine and makes palette placement obvious.

The naming engine gains a shipped, stock-runnable example and, crucially, **graceful degradation**: a mis-configured or unreachable `naming_script` (wrong dotted path, module not loaded yet, or a script that raises) no longer errors — it falls back to the default `sequence` name and logs a warning, so a bad config can never block planning. On the editor side, dragging a device from the palette now lands deterministically on the exact rack unit under the cursor (no more half-unit / off-by-one drops that depended on where you grabbed the palette row), the dragged tile turns translucent, and a green landing-preview band shows precisely where it will drop (red when the slot is illegal). Full-depth devices now read consistently: the front tile shows the planned name while the rear shadow always shows the device's hardware identity. No schema changes, no breaking changes, no migrations.

### Added
- **Shipped naming example** (`netbox_rack_design.naming_example`): a small, stock-runnable `build_name(placement)` demonstrating a family counter and A/B phase-paired PDU slots (`a1, b1, a2, b2, …`). Uses absolute imports so it keeps working verbatim when copied into NetBox's `SCRIPTS_ROOT`. Enable via `"naming_script": "netbox_rack_design.naming_example.build_name"`.
- **Drag landing preview**: while dragging a palette add or moving a tile, the tile turns translucent and a green band marks the exact rows it will occupy on release (red deny band over illegal rows).
- **Device naming docs** (`docs/device-naming.md`): all three modes, a fully-commented example, a step-by-step "verify on your instance" walkthrough, and two `SCRIPTS_ROOT` integration variants (UI Add vs. file copy).

### Fixed
- **Deterministic palette placement**: a whole-U palette add now snaps to the rack unit under the cursor regardless of where the palette row was grabbed — fixes intermittent off-by-one/half-unit landings (e.g. "dropped on U23, landed on U22").
- **Consistent full-depth rear face**: the rear shadow always shows the device's stable identity/type, never the mutable planned-name overlay, so every rear hatch reads uniformly (previously an add whose name preview hadn't returned yet leaked the name onto some rear hatches and not others).
- **Robust script-mode naming**: an unresolvable or raising `naming_script` falls back to the default sequence name (with a logged warning) instead of failing the name-preview request.

### Changed
- Preview-name requests carry the session's already-assigned sibling names (`pending_names`) so same-session palette adds get consecutive family numbers instead of colliding.

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
