# Templates

A **template** is a reusable rack layout with no site: "our standard ToR",
"our standard product rack". You build it once, then stamp it onto one or
more racks in a real design instead of dragging in the same dozen devices
every time. A **template group** is an ordered set of templates describing a
multi-rack product pod — a spine template, a leaf template repeated per
rack, a storage template — applied as a unit.

## What a template stores, and what it deliberately does not

A template placement carries device type, role, tenant, and any [planning
fields](planning-fields.md) you've configured — the same things an `add`
placement carries in a real design. It does **not** carry:

- a rack, a site, or any absolute position
- power configuration — a real power feed is site-bound and cannot travel
  with a template; you wire up power after the drop, exactly as you would
  for a device dragged in by hand
- a device name — names are generated at the point the template is stamped,
  from the target design/site/rack, none of which the template itself has

## Anchors and ordering — no positions, no gaps

Instead of an absolute U position, each placement stores an **anchor**
(`top` or `bottom`) and an **order** within that anchor. Stamping walks the
template's placements in order and drops each one into the first free slot
nearest its anchor in the *target* rack — scanning down from the top for a
`top`-anchored device, up from the bottom for a `bottom`-anchored one. There
is no `middle` anchor.

This means a template never stores a gap. If the target rack already has
something sitting where your template would otherwise land, that one device
drifts by a unit and everything past it keeps compacting toward the anchor
— you don't get a five-device shift for one collision, but you also don't
get to reserve a blank space "just in case". **If you actually want a gap
preserved, put a real blanking-panel device type in the template at that
spot.** That's the only way to express reserved space.

A collision with the target rack's own free space is the only thing that
can stop a stamp: if the template's devices simply do not fit, nothing is
placed there, and it's reported as not fitting rather than partially
applied.

## Chassis and blades

A template can nest a chassis and its blades, the same way a design does —
build the chassis, then add blades into its named bays. A blade cannot exist
in a template without its parent chassis; an orphaned blade is refused when
you try to save it.

## Stamping a template into a design

A **Templates** tab in the editor's side panel lists your templates and
groups. Two ways to apply one:

- **Drag** a template card onto a rack in the editor — stamps that one rack.
  This is the common case.
- **Click** the card to open a dialog — for applying one template to several
  racks at once, or for mapping a group's members onto a rack each.

Applying one template to several racks repeats the same content in each.
Applying a **group** maps each member template to a specific target rack you
choose, so the spine template doesn't end up in the rack you meant for
storage.

Nothing is written to the database at drop time — the server computes
positions and names for the tiles you're about to get, you see them land as
unsaved tiles in the editor, and the ordinary **Save** is what actually
persists them. Escape, Cancel, and further drags all keep working exactly as
they do for anything else you drag in — a stamp is provisional until you
save, same as any other edit.

Every placement that came from a template records which template and which
version of it — useful later for answering "which racks use the standard
ToR?" — though the template itself is not automatically re-applied if you
change it afterwards; a changed template does not retroactively touch racks
it was already stamped into.

## Extracting a template from what already exists

You don't have to build a template placement-by-placement. Two sources:

- **From a design.** In the design editor, use **Save as template** on a
  rack to capture its current, projected layout — including real devices
  already standing there, not only the placements this design itself added.
- **From a real rack in DCIM.** This one has no button yet — it is an API
  action. `POST /api/plugins/rack-design/templates/from-rack/` with
  `{"name": "Standard ToR", "rack_id": 3457, "description": "…",
  "group_id": null}` reads the actual devices, roles and tenants sitting in
  the rack (blades in a chassis included) and returns `{"template_id",
  "placement_count", "warnings"}`. The design-side twin is
  `POST …/templates/from-design/` with `design` and `rack` (a real rack pk or a
  `planned:<pk>` key), which is what **Save as template** calls.

Either way, extraction has to decide anchor and order for you, and it does
this by peeling contiguous runs of devices from the physical top and the
physical bottom of the rack.

**One thing extraction cannot do anything about: an "island."** A device
with real empty space on *both* sides of it — touching neither the top nor
the bottom of the occupied run — has no `(anchor, order)` that can
reproduce its position; a template genuinely cannot express it. Rather than
silently dropping such a device from the template, extraction reports it as
a warning naming the device and its position, so you know to go add it back
by hand if you want it in the template at all.

**A planned rack cannot be saved as a template.** It has no devices by
definition — there is nothing yet to extract.

## Permissions

Templates use ordinary, global NetBox object permissions — the same
mechanism as every other model in this plugin. If you need "only team X can
see their own templates," configure that through NetBox's own per-permission
queryset constraints rather than looking for a plugin-specific setting.
