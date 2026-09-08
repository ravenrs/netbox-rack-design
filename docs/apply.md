# Applying a design

A design is a plan. **Applying** it writes that plan into NetBox as *planned*
devices, so the space it needs is reserved and its cabling can be prepared
before any hardware is touched.

Two things make this worth doing:

- **Nobody can take the slot.** Until a design is applied, the unit it claims
  looks empty to everyone else in NetBox. A colleague racking hardware has no
  way to know your approved plan wants U36.
- **Cabling can be planned.** A device that has not moved yet has no ports at
  its future location. The planned device apply creates does, so interfaces and
  cables can be prepared against it in the normal way.

## The order of things

```
draft  ->  approved  ->  applied  ->  implemented
```

1. **Draft** — you plan. Nothing at all is written into DCIM; drag a device
   around ten times and no real object is created or destroyed.
2. **Approved** — the design freezes. Its placements become read-only, which is
   what makes it safe for another design to build on.
3. **Applied** — planned devices appear in NetBox at the target slots, and the
   devices the design removes are flagged. The design **stays approved**.
4. **Implemented** — the hardware actually moved. Apply never sets this; the
   automation that performs the physical work does.

Apply materializes the *plan*, not the move. The device being relocated is left
completely untouched — still active, still in its old slot — so between apply
and the physical work the rack honestly shows both it and its planned
destination.

## Running it

**In the UI**: open an approved design and press **Apply**. You get a
confirmation page first, listing exactly what will happen — every device to be
created, every status to be flagged, and, called out separately, anything that
will be **deleted**. Nothing is written until you confirm.

**From automation**:

```
GET  /api/plugins/rack-design/designs/<pk>/apply/     # dry run, writes nothing
POST /api/plugins/rack-design/designs/<pk>/apply/     # execute
```

The `GET` returns precisely what a real run would do and needs only
`view_design`, so it is safe to poll from a pipeline. Both return the same
shape:

```json
{
  "ok": true,
  "problems": [],
  "created": [...], "updated": [...], "removed": [...],
  "deleted": [...], "reverted": [...]
}
```

A refused `POST` returns `409` with the problems in the body.

## What it creates

| Placement | What apply does |
|---|---|
| `add` | Creates the device under the placement's name |
| `move` (renamed) | Creates a device under the new name |
| `move` (keeping the name) | Creates a device named `<design title>-<device name>` |
| `remove` | Creates nothing; flags the real device's status |

The decorated name for a keep-name move exists because two devices cannot share
a name while the original still holds it. It is the same string the elevation
shows for that tile.

Role and tenant follow the placement: whatever you set as a planned override is
applied, and anything you left blank carries over from the device being moved.

Statuses come from configuration:

```python
PLUGINS_CONFIG = {
    "netbox_rack_design": {
        "planned_status": "planned",
        "removal_status": "to_decommission",
    },
}
```

`removal_status` defaults to NetBox's native `decommissioning`. Where that
status triggers something destructive in your deployment, add your own via
`FIELD_CHOICES` and point this at it.

## Pressing it twice is safe

Apply reconciles; it does not duplicate. On a second run it finds the device it
created before, compares name, rack, position, face, role, tenant and status,
and writes back only what drifted. If someone renamed the planned device by
hand, the next apply renames it back. If someone deleted it, the next apply
recreates it.

This works because the plugin records which device it created for which
placement, rather than searching by name — a name search breaks the moment
someone edits a name, and would then create a second device.

## When it refuses

Apply checks everything **before** writing anything, and reports every problem
together rather than failing on the first one:

- *"U36 front in rack 0103 is occupied by srv-09."*
- *"The name IDS-1234-srv-01 is already used by another device in this site."*
- *"Design IDS-1000 has unapplied placements; apply it first."*
- *"You do not have permission to create devices in site AMS1."*

Fix them and press again. A run either completes in full or changes nothing at
all — it is a single transaction, so NetBox is never left half-applied.

### Chains apply in order

If your design is based on another, that ancestor must be applied first. A slot
should never be reserved for a move whose precondition does not exist yet.

Note this constrains only *applying*. **Planning** is deliberately unordered —
being able to plan on a design whose hardware work has not happened is the
entire point of [design chains](design-chains.md).

## Permissions

Apply runs with **your** permissions: `dcim.add_device`, `change_device` and
`delete_device`, as the run actually needs them, checked through NetBox's
object permissions so per-site and per-tenant constraints apply.

There is no service account behind the button, deliberately — otherwise
pressing it would let you do something you could not do directly. The practical
consequence: whoever applies a design needs DCIM write rights. Where planners
should not have those, apply belongs to operators or to automation with its own
token.

## Cleanup, and the one irreversible step

If you take a design back to draft, delete a placement, and re-approve, the
planned device that placement created is no longer wanted. The next apply
removes it — and **says so in its result**.

The same happens in reverse for a removal: if you drop a `remove` placement,
the next apply restores that device's status to exactly what it was before the
flag, recorded at the time rather than guessed at `active`.

Deleting a planned device is the only irreversible action in this flow, and it
takes any cables attached to it. That is why the confirmation page names every
deletion before you confirm, and why the API reports them in the dry run. The
plugin is not cable-aware in this version, so it cannot warn you that a
particular planned device carries a day's cabling work.

## Reading the records afterwards

Apply records which device it created for which placement. Automation that
performs the physical move reads them to find its exact target:

```
GET /api/plugins/rack-design/design-applies/?design_id=<pk>
```

Each record carries the design, placement, device, who applied it, when, and —
for a removal — the status the device had before it was flagged. The
`design_title` and `device_name` snapshots are always present, so a name is
readable without a second request and stays readable after the object it names
is gone.

Three filters find the leftovers:

| Filter | Finds |
|---|---|
| `no_device=true` | the planned device was deleted in DCIM |
| `no_placement=true` | the placement was deleted; the planned device is unwanted |
| `no_design=true` | the whole design was deleted |

The endpoint is read-only. These records are written by apply and nothing else
— a client able to edit one could make them disagree with DCIM, which is
exactly what they exist to prevent.

## On the elevations

Once applied, the design that created a device draws it as **one** tile, marked
as applied — the live DCIM copy is suppressed, so a device never appears twice.

Any *other* design covering that rack shows it as **reserved** by the design
that holds it. Both markers are filterable in the legend and appear in the
hover card, so it is always visible whose plan owns a slot.

## Not yet supported

A blade destined for a chassis bay cannot be applied. Apply reports it as
unsupported rather than skipping it — a silent skip would read as "applied
everything".
