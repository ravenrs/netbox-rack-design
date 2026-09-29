# Designs and the data model

▶ Video: [Part 1 — The data model & the design page](https://youtu.be/pYAsDRdX_fU)

A **design** is a plan for changing racks — adds, moves and removals recorded
as intent on top of your live NetBox data. Nothing in DCIM changes until the
design is [applied](apply.md). This page covers what a design is made of, its
page, and its statuses.

## Three models

| Model | What it is |
|---|---|
| **Design** | The plan itself. Scoped to one or more sites and the racks it plans across. |
| **Design group** | An optional folder for related designs — every design of one rollout. Groups can nest (a group has an optional parent). Purely organizational: a group never changes what a design does. |
| **Design placement** | One recorded change inside a design: **add** a new device from a device type, **move** an existing device, or **remove** one. |

A placement never touches the real device. An *add* has a device type and no
device yet; a *move* points at the real device and a target slot; a *remove*
points at the real device and nothing else.

## A design's fields

| Field | Meaning |
|---|---|
| Title | The plan's name. All versions of a plan share it. |
| Sites | One or more sites. A design can span sites — a server can be moved from a rack in one site to a rack in another. |
| Racks / planned racks | The racks in scope: real ones, and [planned racks](planned-racks.md) that don't exist yet. |
| Status | Draft, Approved, Rejected, Implemented, Superseded — see below. |
| Summary, link | One line of what and why; a link to the ticket that asked for it. |
| Group | The design group it belongs to. |
| Version | 1 for a new plan; **New version** makes the next one (see [Design chains](design-chains.md#versioning-cloning-a-design-for-revision)). |
| Based on | An approved design this one is planned on top of (a *design chain*). |
| Depends on | Other designs this one is sequenced behind. |
| Sequence | Execution order; auto-assigned in steps of 10 when left blank. |
| Description, comments, tags, custom fields | As on any NetBox object. |

**Depends on** and **sequence** are for people and for your own automation:
they order lists and state intent, and a *depends on* cycle is refused, but
neither blocks saving, applying or anything else in the plugin.

## Statuses and the lifecycle

```
draft  ->  approved  ->  (applied)  ->  implemented
```

- **Draft** — where you plan. Placements can be changed freely.
- **Approved** — the design is **frozen**: its placements, planned feeds and
  rack scope are read-only. That is what makes it safe for another design to
  build on it, and what makes it eligible to be applied.
  - Only **one version** of a plan may be approved at a time.
  - An approved design **cannot leave approved while other designs are based
    on it** — re-base them first (see [Design chains](design-chains.md)).
- **Applied** is not a status: applying writes planned devices into DCIM and
  leaves the design approved.
- **Implemented** — the physical work is done. The plugin never sets it;
  whoever (or whatever automation) does the work does.
- **Rejected** and **Superseded** are labels for plans that won't go ahead or
  were replaced; the plugin attaches no behaviour to them.

## The design page

The header carries **Open editor** (straight into the [editor](editor.md)),
NetBox's Bookmark / Subscribe / Clone / Edit / Delete, and — on an approved
design — **Apply** in the Design card.

| Card | Shows |
|---|---|
| Design | Title, sites, status, summary, link, group |
| Versioning & sequencing | Version, based on, sequence |
| Comments | The design's comments |
| Dependencies | *Depends on* and *Required by* |
| Design chain | Parent, ancestor stack, children; **Derive design**, **New version**, **Re-base** |
| Tags | The design's tags |
| Placements | Every placement: kind, device or device type, target rack, position, face |
| Racks | The racks in scope, including planned racks (badge **Planned**, or **Built by apply** once applied); **View elevation**, **Open editor** |
| Stale placements | Placements whose device has since been deleted from DCIM — re-point or delete them |
| Planned power feeds | Feeds the design plans for racks without real supply, with capacity and the PDUs bound to them |
| Affected racks | Every rack the design's placements touch |

**Clone** is NetBox's ordinary clone: a new design pre-filled with the same
status, summary, link, group and tags — **no placements**. To copy a plan's
content, use **New version** (same plan, revised) or **Derive design** (a new
plan on top of this one).

## Related

- [The editor](editor.md) — where placements are made.
- [Design chains](design-chains.md) — versions, derive, re-base, conflicts.
- [Applying a design](apply.md) — turning a plan into planned devices.
