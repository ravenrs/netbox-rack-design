# API and integrations

▶ Video: [Part 20 — API, GraphQL & wrap-up](https://youtu.be/ZBFt_cOQ8cQ)

Everything the UI does is available to scripts and automation: a REST API with
the same permission model as the UI, GraphQL, and NetBox's global search.

## REST

Base path: `/api/plugins/rack-design/`. Authenticate with a NetBox API token,
as for any NetBox endpoint. Every model is a normal NetBox endpoint — list,
retrieve, create, update, delete, filter, bulk operations:

| Endpoint | Model |
|---|---|
| `designs/` | Designs |
| `design-groups/` | Design groups |
| `placements/` | Design placements |
| `planned-racks/` | Planned racks |
| `planned-power-feeds/` | Planned power feeds |
| `templates/`, `template-groups/`, `template-placements/` | Templates |
| `design-applies/` | What each apply created (read-only) |
| `favorite-sets/`, `favorite-device-types/`, `hidden-design-racks/`, `hidden-design-chassis/` | The calling user's own editor state |

### Design actions

`POST` unless noted, on `designs/<id>/<action>/`:

| Action | Does |
|---|---|
| `apply/` | `GET`: **dry run** — what applying would create, update, flag, delete, plus every problem that would stop it; writes nothing. `POST`: apply (200, or 409 with the problems if refused). |
| `derive/` | Create a draft child design based on this approved one. |
| `new-version/` | Clone this design as the next version of the same plan. |
| `rebase/` | Point `based_on` at another design (`{"based_on": <id>}`) — an approved one, or another version of the current parent. |
| `chain/`, `conflicts/` | `GET`: the design's ancestor chain; its chain, stale and peer conflicts. |
| `save-layout/` | What the editor's Save sends: the full set of placements. |
| `preview-name/` | The name the naming engine would give a prospective placement. |
| `rerun-naming-preview/`, `rerun-naming/` | Re-name placements whose names collide with a peer design. |
| `recompute-distribution/` | Per-PDU/bank power for a layout, without saving it. |
| `preview-template/` | What stamping a template onto racks would produce. |
| `add-rack/`, `remove-rack/`, `create-planned-rack/` | Change the design's rack scope. |
| `rack-power/` (`GET`/`POST`), `power-source/` (`GET`), `feeds/` (`GET`), `planned-feed/` (`GET`/`POST`/`DELETE`), `copy-feeds/` | A rack's power planning: overrides, supply, planned feeds, copying feeds from another rack. |

Templates add `templates/from-design/` (extract from a rack in a design) and
`templates/from-rack/` (extract from any real rack in DCIM).

### The apply dry run

```bash
curl -s -H "Authorization: Token $TOKEN" \
  https://netbox.example.com/api/plugins/rack-design/designs/832/apply/
```

```json
{
  "ok": true,
  "problems": [],
  "created":  [{"placement": 901, "device": null, "name": "ams1-server-1",
                "rack": null, "position": "20", "face": "front", "recreated": false}],
  "updated":  [],
  "removed":  [],
  "deleted":  [],
  "reverted": [],
  "racks":    [{"planned_rack": 12, "name": "R5", "location": 560,
                "rack": null, "created": true}],
  "feeds":    [{"planned_feed": 40, "name": "R5-A", "power_panel": 644,
                "rack": null, "existing": null, "feed": null}]
}
```

`deleted` lists planned devices a re-apply would delete because their
placement is gone — the only irreversible step, so it is in the dry run too.
`rack` is the real rack's id — `null` here because R5 is a planned rack that
apply will create. `racks` and `feeds` are what apply builds around the devices: planned racks it
creates (or adopts) and planned feeds it creates (or reuses) and cables.

### Apply records for automation

After an apply, `design-applies/` has one row per placement it materialized:
which design, which placement, which real device. That is how automation finds
its exact targets. Filters: `design_id`, `placement_id`, `device_id`, and
`no_design` / `no_placement` / `no_device` for the leftovers (a record whose
design, placement or device has since been deleted).

```bash
curl -s -H "Authorization: Token $TOKEN" \
  "https://netbox.example.com/api/plugins/rack-design/design-applies/?design_id=834"
```

## GraphQL

Every model is in NetBox's GraphQL schema: `design` / `design_list`,
`design_group`, `design_placement`, `planned_rack`, `planned_power_feed`,
`template`, `template_group`, `template_placement` (each with a `_list`).

```graphql
{
  design_list {
    title
    version
    status
    based_on { title version }
  }
}
```

```graphql
{
  design_placement_list {
    design { title version }
    kind
    proposed_name
    target_rack { name }
    target_position
  }
}
```

The GraphQL filters are NetBox's base ones (`id`, tags, created/updated);
to narrow by design, site or status, use the REST filters or select the
fields you need and filter client-side.

## Global search

Designs, design groups, planned racks, planned power feeds, templates and
template groups are indexed in NetBox's global search. After upgrading an
existing install, build the index once:

```bash
python manage.py reindex netbox_rack_design
```

## Related

- [Applying a design](apply.md) — what apply does, and its permissions.
- [Permissions](permissions.md) — what an API token's user needs.
