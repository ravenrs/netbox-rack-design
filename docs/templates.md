# Templates

▶ Video: [Part 13 — Rack templates](https://youtu.be/joqDaBdxYbg)

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

A **zero-U device in the non-racked tray** — a vertical PDU — is part of the
template like any other: it is anchored to the tray, and stamping puts it back
into the target rack's tray — as it does a server parked unmounted there.

Power is not stored, but it isn't lost either: when a template is stamped
onto a rack that has feeds (real or planned), the PDUs it carries — in the
tray or rack-mounted — **bind themselves** to them: a PDU whose name ends in an
A or B leg letter takes the matching feed, any other takes the first free one,
so the bank chips appear without opening a power dialog.

## Planning fields

A template placement carries the deployment's [planning fields](planning-fields.md)
like a design's add does, and hands them on:

- **Saving a rack as a template** takes them from what stands there — a design's
  own adds keep their values, and every real device (untouched, or moved with
  planned values) gives its own custom fields, read back through each field's
  `target`. A device value the field would refuse is left out.
- **By hand**, the template placement's form has one input per field, typed
  from its custom field — a number box, the choice set, a site picker — and
  its page lists them by label.
- **Stamping** puts them on the new placements, and **apply** writes them onto
  the devices' custom fields.

## Anchors, offsets and order — no absolute positions

Instead of an absolute U position, each placement stores an **anchor** — `top`,
`bottom`, or `tray` — an **offset**, and an **order**:

- `top` / `bottom`: the device is measured from that end of the *target* rack.
  Its **offset** is the number of empty units kept between that end and the
  device; stamping starts looking for a free slot that far in and walks
  inward. **Order** decides which device is placed first.
- `tray`: the device goes into the target rack's non-racked tray — a zero-U PDU,
  or a server parked unmounted — whatever its height.

Because a device is measured from an end, not from U1, the same template
fits racks of any height: a switch at `top`, offset 0 lands on U42 in a 42U
rack and on U47 in a 47U one; servers at `bottom` stay at the bottom; and a
device with free units around it keeps exactly that distance from its end.

If the target rack already has something where a device would land, that
device moves on to the next free unit toward the middle. A device that fits
nowhere is reported as not fitting rather than partially applied.

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

Either way, extraction decides anchor, offset and order for you. Devices in
a contiguous run from the physical top or bottom are packed against that end
(offset 0, in order). Any other device — with free units on both sides — is
anchored to the end of the half it sits in, at its real distance from that
end, so its gap comes back when the template is stamped. A device standing
in the non-racked tray is anchored to the tray.

A **planned rack** is saved the same way, from the devices the design plans
in it.

## Permissions

Templates use ordinary, global NetBox object permissions — the same
mechanism as every other model in this plugin. If you need "only team X can
see their own templates," configure that through NetBox's own per-permission
queryset constraints rather than looking for a plugin-specific setting.
