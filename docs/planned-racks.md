# Planned racks

A design can only place devices into racks that already exist in NetBox — so
until now there was no way to plan a rack that does not exist yet: a new row
in a hall you're about to build out, or a rack you simply haven't racked. A
**planned rack** closes that gap: a rack with a name, a height and a
location, that behaves like any other rack in a design, right up until the
point where it needs to actually exist.

## Creating one

In the design editor, press **Create rack**. Give it a name, a U height, and
a location chosen from one of the design's sites (via the Site → Location
hierarchy). It appears in the editor immediately, and from that point on you
plan into it exactly as you would a real rack: drag devices in, wire up power,
run naming.

## It is shared, like a real rack

A planned rack is not owned by the design that created it. If a second
planner starts a design that also targets "the new row 3 rack in Location
X", they land on the same planned rack, and peer-conflict detection sees
them the same way it would if the rack already existed. Deleting a planned
rack is refused for as long as any design still references it — there is no
single owner who gets to remove it out from under someone else's plan.

## Applying it: adopt if it exists, otherwise create

Apply resolves a planned rack by **`(location, name)`** — exactly the
uniqueness NetBox itself enforces on `dcim.Rack`. Before creating anything,
it checks whether a real rack already exists at that location with that
name:

- if one does, it is **adopted** — the planned rack is matched to it, not
  duplicated
- if not, a new `dcim.Rack` is created

Either way, the planned rack's `realized_rack` field is set to record which
rack it resolved to, and the planned rack itself is never deleted — it stays
around, now marked realized, so the fact that this rack started life as a
plan is not lost.

**The rules worth knowing before you hit this:**

- **If you adopt an existing rack, the real rack wins.** Say your plan
  assumed 42U but the rack that actually exists at that location and name is
  47U — the real 47U governs, and your devices land against the true top of
  the rack. Apply only fails if a device genuinely does not fit in the real
  rack's free space.
- **Name and location together are the identity.** Two planned racks with
  the same name in *different* locations are unrelated racks and both apply
  independently. Get the location wrong and you will either adopt the wrong
  rack or create a duplicate under a name you didn't intend to reuse.
- **A planned rack cannot be saved as a template.** Templates are extracted
  from devices, and a planned rack has none by definition — there is nothing
  to extract yet.

## Power on a planned rack

A planned rack has no real power feeds and no rack-level custom fields of
its own — there is no `dcim.PowerFeed` row to bind to, because there is no
`dcim.Rack` row yet. The editor's PDU and rack-power dialogs work anyway:
a planned PDU can bind to a **planned power feed**, and a rack-power
override recorded against the planned rack is what the distribution engine
reads, since there is nothing else to read. Once the rack is realized on
Apply, that override keeps working exactly the same way against the real
rack.

## REST and GraphQL

Planned racks have their own endpoint at
`/api/plugins/rack-design/planned-racks/`, and a design's `planned_racks`
relationship is exposed the same way `racks` is. `DesignPlacement`,
`DesignPowerFeed` and `DesignRackPower` all gained a parallel
`target_planned_rack` / `planned_rack` field alongside their existing
real-rack one — exactly one of the two is ever set. The same fields are
available over GraphQL.
