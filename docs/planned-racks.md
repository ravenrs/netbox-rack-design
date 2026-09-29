# Planned racks

▶ Video: [Part 9 — Planned racks](https://youtu.be/By6q8FNIDJ0)

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

### Creating a row at once

The name field takes the same range patterns NetBox uses elsewhere (the syntax
behind `Et1/[1-48]` when you add interfaces): `R[1-4]` creates R1, R2, R3 and
R4 in one press, `R[a-c]` creates Ra, Rb and Rc, and a comma list like
`R[1,3,7]` creates exactly those. Every rack gets the same U height and
location. A collision anywhere in the expansion creates nothing and names the
offender, so a half-made row is never left behind.

### Seeding power at creation

▶ Video: [Part 11 — Copying power, PDUs that bind themselves](https://youtu.be/X-16j1W0NmM)

**Copy feeds from** (optional) points at a rack already in the design and seeds
each new rack's planned power supply from that rack's feeds — the same write
the rack power dialog's *Copy from rack* performs. Every rack the form creates
gets **its own** copy, named for itself: creating `R[1-3]` off a source with
`A`/`B` feeds leaves R1 with `R1-A`/`R1-B`, R2 with `R2-A`/`R2-B`, and so on.

Feeds are what give a rack its capacity, and a PDU planned into the rack
afterwards [binds to one of them by
itself](power-distribution.md#automatic-binding) — so a row created this way is
ready for a template stamp with no further power clicks.

## It is shared, like a real rack

A planned rack is not owned by the design that created it. If a second
planner starts a design that also targets "the new row 3 rack in Location
X", they land on the same planned rack, and peer-conflict detection sees
them the same way it would if the rack already existed. Deleting a planned
rack is refused for as long as any design still references it — there is no
single owner who gets to remove it out from under someone else's plan.

## Applying it: adopt if it exists, otherwise create

▶ Video: [Part 18 — Apply: a rack and its power from nothing](https://youtu.be/QWR9C5GDJ6M)

Apply resolves a planned rack by **`(location, name)`** — exactly the
uniqueness NetBox itself enforces on `dcim.Rack`. Before creating anything,
it checks whether a real rack already exists at that location with that
name:

- if one does, it is **adopted** — the planned rack is matched to it, not
  duplicated
- if not, a new `dcim.Rack` is created, with status **Planned** — it isn't
  standing in the hall yet. The rack's [planned power feeds](#power-on-a-planned-rack)
  are created as real (planned) feeds on their power panel and the PDUs bound
  to them are cabled, in the same apply (see [Applying a design](apply.md)).

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
- **A planned rack can be saved as a template** from what the design plans in
  it — **Save as template** on its header, the same as a real rack.

## Taking it out of a design

A planned rack is listed in the editor's **Racks** panel alongside the real
ones, with a **Planned** badge and the same controls: the eye hides it
from your own view (like a real rack's), the trash removes it. Removing it detaches it
from *this* design and deletes that design's placements, planned feeds and
rack-power rows for it, after the usual confirmation naming what will go.
The rack itself survives: it is shared, so another design planning the same
future rack is untouched.

Deleting the rack outright, from **Rack Design → Planned Racks**, is
refused while any design still plans across it — detach it from each of
them first. The rack's own page lists those designs under **Used by
designs**, each with a link straight into its editor, so there is no
hunting for who is holding it. A rack that has already been realized is never deletable
(see above).

## Power on a planned rack

A planned rack has no real power feeds and no rack-level custom fields of
its own — there is no `dcim.PowerFeed` row to bind to, because there is no
`dcim.Rack` row yet. The editor's PDU and rack-power dialogs work anyway:
drop a PDU into the rack and the bind dialog offers **Define planned
feed**; the feed is a `DesignPowerFeed` on the planned rack, and the PDU
binds to it exactly as it would to a real one. **Copy from rack** works
too, in both directions — copying a neighbouring real rack's supply into a
greenfield planned rack is the common case. A rack-power override recorded
against the planned rack is what the distribution engine reads, since
there is nothing else to read.

From there the rack behaves like any other: its capacity comes from its
planned feeds (derated by NetBox's max-utilization rule), and the per-bank
chips, bank zones and heatmap all work — live, as you edit, with no reload
and no layout Save. Once the rack is realized on Apply,
that override keeps working exactly the same way against the real rack.

## REST and GraphQL

Planned racks have their own endpoint at
`/api/plugins/rack-design/planned-racks/`, and a design's `planned_racks`
relationship is exposed the same way `racks` is. `DesignPlacement`,
`DesignPowerFeed` and `DesignRackPower` all gained a parallel
`target_planned_rack` / `planned_rack` field alongside their existing
real-rack one — exactly one of the two is ever set. The same fields are
available over GraphQL.
