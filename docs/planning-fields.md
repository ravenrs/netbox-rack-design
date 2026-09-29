# Planning fields

▶ Video: [Part 5 — Planning fields](https://youtu.be/7Z8d_P3Sqio)

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
            {"key": "responsible", "label": "Responsible",
             "target": "cf.responsible", "rail": True},
            {"key": "burn_in", "label": "Burn-in hours", "target": "cf.burn_in_hours"},
            {"key": "tier", "label": "Hardware tier", "target": "cf.hw_tier", "kinds": ["add"]},
            {"key": "staging", "label": "Staging site", "target": "cf.staging_site"},
        ],
    },
}
```

Each field points at a custom field that already exists on `dcim.device`, and
takes that custom field's own definition — see
[Custom-field types](#custom-field-types) below. No `type` is declared.

| Key | Required | Meaning |
|---|---|---|
| `key` | yes | The plugin-internal identifier, and the key the value is stored under. Renaming a custom field means editing `target`, not rewriting stored rows. |
| `label` | no | Shown next to the input. Defaults to `key`. |
| `type` | no | `text` (default), `number` or `choice`. Ignored when `target` names an existing device custom field — that field's own type is used. |
| `choices` | for `choice` | The allowed values, as strings. Ignored for a custom field: its choice set is used. |
| `target` | no | Where the value lands on the real device when the design is applied: `cf.<name>` for a custom field, or a native device attribute such as `serial`, `asset_tag` or `description`. |
| `kinds` | no | Which placement kinds may carry the field. Defaults to `["add", "move"]` — the same pair role and tenant are allowed on. |
| `rail` | no | Offer the field in the editor's toolbar as a sticky default for every device added or moved next. |
| `required` | no | The field must carry a value for the placement to validate. |

A malformed descriptor raises `ImproperlyConfigured` rather than being skipped:
an input that quietly stores nothing is worse than an error.

Configure nothing and nothing changes — no extra inputs anywhere.

## Custom-field types

A field whose `target` is `cf.<name>` is **bound** to that device custom field:
the editor input, the validation, the stored value and what apply writes all
follow the custom field's own definition in NetBox — its type, choice set,
related object type, minimum/maximum and regex. What the plugin accepts is
therefore exactly what NetBox accepts when the device is saved.

| Custom field type | Editor input | Stored as |
|---|---|---|
| Text, Text (long), URL | text box (long text: a text area) | string; the field's regex applies |
| Integer | number box, whole numbers, the field's min/max | integer |
| Decimal | number box | number |
| Boolean | Yes / No / — | `true` / `false` |
| Date | date picker | `YYYY-MM-DD` |
| Date & time | date-time picker | ISO 8601 |
| JSON | text area | the parsed JSON |
| Selection | the choice set's choices, by label | the choice's value |
| Multiple selection | multi-select | list of values |
| Object | a search box over the related type's REST list, then a pick | the object's ID |
| Multiple objects | the same, multi-select | list of IDs |

A value that does not fit — `2.5` for an integer, a date that does not exist,
a choice outside the set, an ID with no such object — is refused when the
placement is saved (a 400 over the API), with NetBox's own message.

The hover card shows values the way a person reads them: a choice's label, an
object's name, *Yes* / *No*.

A `cf.` target naming no custom field keeps the declared `type`, and apply
refuses a design that sets it (see below) rather than dropping the value.

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

The same dialog carries a **Power** block — one select per PSU, `Automatic`
or a specific feed leg — which is not a planning field but the placement's
`preferred_feed_legs`; see
[Power distribution](power-distribution.md#bank-zones-and-choosing-a-feed-per-device).

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

## On apply

Apply writes every planning value onto the device it creates:

- a **custom field** target goes into the device's `custom_field_data`, after
  the custom fields' defaults — so a *required* device custom field is
  satisfied by the value the planner set in the design;
- a **native attribute** target (`serial`, `asset_tag`, ...) is set on the
  device;
- a **move**'s successor device keeps the real device's own custom fields,
  with the design's values on top.

A value changed in the design after an apply is written back on the next one.
Apply lists — before anything is written — a required custom field left
empty, a `cf.` target that no longer exists, and a native target that is not
a device attribute.

## In the API

Values live in `DesignPlacement.planning_data`, a flat `{key: value}` object.

Because the field names are yours, a client has to ask for them:

```
GET /api/plugins/rack-design/placement-fields/
```

returns the descriptors, minus `target` — that is apply-time plumbing, not part
of the client contract. A bound field carries its custom field's `type`,
`choices` and `choice_labels`, `multiple`, `min`/`max`, and — for an object
field — `api_url` (the REST list to pick from) and `object_type`. Then create a
placement as usual:

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
device. A bound value arrives as NetBox's own Python value — an object field
gives the object (`{device.cf[staging_site]}` renders its name), a date field
a `date` — just as it does on a real device.

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
