# Planning fields

A planned device does not exist yet, so there is nowhere to put the attributes
a planner already knows about it — which hardware class it is, how long it has
to burn in, whichever field your organisation tracks. `device_role` and
`tenant` are handled because they are universal NetBox concepts. Everything
else is per-deployment, and the plugin never hardcodes a custom-field name.

Instead you **declare** your fields in `PLUGINS_CONFIG`. The plugin ships the
mechanism; the field names are yours.

## Declaring them

```python
PLUGINS_CONFIG = {
    "netbox_rack_design": {
        "placement_fields": [
            {
                "key": "hw_class",
                "label": "HW class",
                "type": "choice",
                "choices": ["gp", "storage", "gpu"],
                "target": "cf.hw_class",
                "kinds": ["add"],
                "rail": True,
            },
            {
                "key": "burn_in_hours",
                "label": "Burn-in (h)",
                "type": "number",
                "target": "cf.burn_in_hours",
            },
        ],
    },
}
```

| Key | Required | Meaning |
|---|---|---|
| `key` | yes | The plugin-internal identifier, and the key the value is stored under. Renaming a custom field means editing `target`, not rewriting stored rows. |
| `label` | no | Shown next to the input. Defaults to `key`. |
| `type` | no | `text` (default), `number` or `choice`. |
| `choices` | for `choice` | The allowed values, as strings. |
| `target` | no | Where the value lands on the real device when the design is applied: `cf.<name>` for a custom field, a dotted path for a native attribute. |
| `kinds` | no | Which placement kinds may carry the field. Defaults to `["add", "move"]` — the same pair role and tenant are allowed on. |
| `rail` | no | Offer the field in the editor's toolbar as a sticky default for every device added or moved next. |
| `required` | no | The field must carry a value for the placement to validate. |

A malformed descriptor raises `ImproperlyConfigured` rather than being skipped:
an input that quietly stores nothing is worse than an error.

Configure nothing and nothing changes — no extra inputs anywhere.

## In the editor

A field declared with `rail: True` appears in the toolbar next to Role and
Tenant. Pick a value once and every device you touch afterwards inherits it —
both a device you drag in from the catalog and an existing one you **relocate**.

On a move the values are *planned overrides*: the design says what the device
becomes when it lands. Leave the rail empty and a move is a plain reposition —
nothing is re-attributed, and the device keeps its own role, tenant and custom
fields. Drag a device back to where it started and the overrides go with it,
because there is no longer a move to describe.

Every add and every move also gets a tag button in its top-left corner opening a
**Planning attributes** dialog, pre-filled with whatever the tile carries. That
is where one device departs from the rail default. The button is filled in when
the tile carries at least one value, so a glance across the rack shows what is
still blank.

A removal takes none of this: re-attributing gear you are decommissioning means
nothing, so `remove` is rejected.

## On hover

The device hover card shows the fields as extra rows, for **every** tile —
existing gear, a planned add, a move, a ghost, a flagged removal, and a blade in
a chassis column. The value comes from wherever it actually lives: a real
device's own custom field (the descriptor's `target`), or a planned add's
`planning_data`. So "who owns this?" is answerable by pointing at the rack,
not just at the things you are planning.

Unset fields are omitted rather than shown blank.

## In the API

Values live in `DesignPlacement.planning_data`, a flat `{key: value}` object.

Because the field names are yours, a client has to ask for them:

```
GET /api/plugins/rack-design/placement-fields/
```

returns the descriptors, minus `target` — that is apply-time plumbing, not part
of the client contract. Then create a placement as usual:

```
POST /api/plugins/rack-design/placements/
{
  "design": 12,
  "kind": "add",
  "device_type": 42,
  "target_rack": 7,
  "target_position": 10,
  "target_face": "front",
  "planning_data": {"hw_class": "gpu"}
}
```

An unknown key, a value outside `choices`, a wrong type, a missing `required`
field, or a field set on a kind outside its `kinds` list all come back as a
400. There is no silent-ignore path.

The editor's bulk `save-layout` action takes the same object per item. Omitting
the key leaves the stored values alone; sending `{}` clears them.

## In a naming template or script

A planned device's `cf` are visible to the naming engine, keyed by the **real**
custom field name from each descriptor's `target`:

```python
"naming_template": "{device.site.name}-{device.cf[hw_class]}-{n:02d}"
```

which means the same template works for a planned add and for an existing
device.

## What this is not

`planning_data` is not `custom_field_data`. A placement is a `NetBoxModel`, so
it has custom fields of its own — those describe *the placement*. Planning
fields describe *the device the placement plans*. Keeping them apart is what
lets both stay readable.

Nor is this an access-control mechanism. `planning_data` is exposed in REST and
GraphQL like any other placement attribute; standard NetBox object permissions
on `DesignPlacement` are the only gate.

## The read-side counterpart

An older, separate config key — `planning_fields` — declares values *read off
an object that already exists* (a rack's power ceiling, a real PDU's mount
side) for the power dialogs and distribution scripts. It uses the same
descriptor grammar with a `source` token instead of `target`. See
[Power distribution](power-distribution.md).
