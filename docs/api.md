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
| `design-steps/` | Execution-plan steps (see [Execution plan](#execution-plan)) |
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
| `save-steps/` | Save the execution plan: the whole step layout (`change` permission). |
| `simulate-steps/`, `auto-order/` | Check a proposed step order, or propose one. Read-only (`view` permission). |
| `work-order/` (`GET`) | The smart-hands work order of the plan, as JSON, Markdown or CSV. Read-only. |
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

## Execution plan

The [execution plan](execution-plan.md) is a design's ordered steps. Each
placement has a `step` (a `design-steps/` id, or `null` for unscheduled) and a
`step_order`. Write the plan through `save-steps/`, which validates the whole
order at once and is what the UI uses.

`placements/` also accepts `step` and `step_order`, but setting them one
placement at a time does not check the order as a whole. Use `save-steps/`.

`design-steps/` is a normal NetBox endpoint: list and retrieve, filter by
`design_id`, `index` and `title`. Its permissions are the standard ones for the
`netbox_rack_design` design-step model.

The examples below use illustrative numbers.

Permissions for the actions:

- `simulate-steps/`, `auto-order/`, `work-order/`: `view` on the design. The
  two `POST` calls write nothing, so a **read-only** API token (write disabled)
  can call them.
- `save-steps/`: `change` on the design.

### Save the plan

`POST designs/<id>/save-steps/` replaces the design's whole plan. Send every
step in order. Steps you omit are deleted, and placements you omit become
unscheduled.

```bash
curl -s -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  https://netbox.example.com/api/plugins/rack-design/designs/835/save-steps/ \
  -d '{"steps": [
        {"id": null, "title": "Window 1", "placements": [4101, 4102]},
        {"id": null, "title": "Window 2", "placements": [4103]}
      ]}'
```

```json
{"steps": [
  {"id": 61, "index": 1, "title": "Window 1", "placements": [4101, 4102]},
  {"id": 62, "index": 2, "title": "Window 2", "placements": [4103]}
]}
```

`id` is an existing step's id, or `null` for a new step. A blade is not listed:
it follows its chassis. A bad body returns 400 with the reasons and saves
nothing: a placement of another design, a placement listed twice, a blade, or
a step id that is not this design's.

### Simulate a proposed order

`POST designs/<id>/simulate-steps/` checks an order without saving it. Step *N*
is the state after steps 1 to *N*.

```bash
curl -s -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  https://netbox.example.com/api/plugins/rack-design/designs/835/simulate-steps/ \
  -d '{"steps": [[4103], [4101, 4102]], "from_step": 1, "to_step": 2, "racks": ["r:12"]}'
```

`from_step` and `to_step` (1-based, inclusive) limit which steps are computed.
`racks` limits the output to those racks, as `r:<id>` for a real rack or
`p:<id>` for a planned rack.

```json
{"steps": [
  {"index": 1,
   "racks": {"r:12": {
      "power": {"draw_w": 7800, "capacity_w": 8000, "util_pct": 97.5, "state": "critical"},
      "distribution": {"pdus": {"...": "..."}},
      "distribution_status": {"state": "ok"},
      "problems": [{"code": "bank_over", "severity": "error",
                    "detail": "PDU pdu-b bank 2: 4100 W of 4000 W (102%)"}]}}},
  {"index": 2,
   "racks": {"r:12": {"power": {"draw_w": 6700, "capacity_w": 8000, "util_pct": 83.8,
                                "state": "warn"},
                      "distribution": {"pdus": {"...": "..."}},
                      "distribution_status": {"state": "ok"},
                      "problems": [{"code": "rack_near", "severity": "warning",
                                    "detail": "R12: 6700 W of 8000 W (84%)"}]}}}
]}
```

The problem codes and their rules are in [Execution plan](execution-plan.md#problem-codes).
A placement of another design, a duplicate id or a listed blade returns 400
with `{"steps": [...]}` listing the reasons. A bad rack key returns 400 with
`{"racks": [...]}`. A malformed body returns the usual DRF field errors.

### Auto-order

`POST designs/<id>/auto-order/` proposes an order from the steps you send. The
same rule as the **Auto-order** button applies.

```json
{"steps": [[4101, 4102], [4103]]}
```

```json
{"steps": [[4103], [4101], [4102]], "titles_kept": true}
```

The result keeps the same steps. A step that is still red with several actions
is split into single-action steps. Unscheduled placements come back as
single-action steps. Titles are kept by the client, so `titles_kept` is always
`true`. The server does not save the result; send it to `save-steps/` if you want
to keep it.

### Work order

`GET designs/<id>/work-order/` returns the plan as a work order: the actions
with their from and to locations, power cabling, status after the action, and
each step's simulated power and problems. It is the source of the **Export**
button and of the MCP server's ticket data.

Query parameters:

| Parameter | Meaning |
|---|---|
| `step=N` | One step only. Without it, all steps. `404` if the design has no step N. |
| `output=json` | The default. |
| `output=md` | A Markdown checklist. |
| `output=csv` | One CSV row per action. |

Use **`output`**, not `format`. DRF reserves `format` for its own renderer
selector, so `?format=md` returns 404.

```bash
curl -s -H "Authorization: Token $TOKEN" \
  "https://netbox.example.com/api/plugins/rack-design/designs/835/work-order/?step=1&output=md"
```

JSON shape (abbreviated):

```json
{
  "design": {"id": 835, "title": "Network Refresh", "status": "approved",
             "sites": ["AMS1"], "url": "/plugins/rack-design/designs/835/"},
  "removal_status": "decommissioning",
  "planned_status": "planned",
  "total_steps": 3,
  "steps": [
    {"index": 1, "total_steps": 3, "title": "Window 1", "racks": ["R1"],
     "power": [{"rack": "R1", "draw_w": 7800.0, "capacity_w": 8000.0,
                "util_pct": 97.5, "state": "critical", "summary": "R1 at 7.8/8.0 kW"}],
     "problems": [{"code": "bank_over", "severity": "error", "detail": "...", "rack": "R1"}],
     "actions": [
       {"order": 1, "placement_id": 4103, "kind": "add", "kind_label": "Add",
        "stale": false,
        "device": {"name": "srv-101", "current_name": null, "id": null,
                   "manufacturer": "Dell", "model": "R650", "u_height": 1,
                   "full_depth": true, "role": "Server", "serial": "", "asset_tag": ""},
        "from": null,
        "to": {"site": "AMS1", "location": null, "rack": "R1", "position": "20",
               "face": "front"},
        "power": {"cabling": [], "preferred_feed_legs": [], "feed": null,
                  "source_device": null, "power_config": null},
        "status_after": "planned",
        "url": "/plugins/rack-design/placements/4103/",
        "device_url": null, "bay": null, "chassis": null,
        "planning_data": null, "children": []}
     ]}
  ],
  "unscheduled": [],
  "peaks": [{"rack": "R1", "draw_w": 7800.0, "capacity_w": 8000.0, "step": 1,
             "summary": "R1 peaks at 7.8/8.0 kW (step 1)"}]
}
```

- `status_after` is `removal_status` for a remove, the device's own status for
  a move, and `planned_status` for an add. Both status names come from the
  plugin configuration.
- `power.cabling` lists the device's real power cables: the port, the PDU, the
  outlet and the bank. `power.feed` is the bound feed (real or planned).
- `unscheduled` lists the actions that are in no step. It is empty when a single
  `step` is requested.
- `peaks` gives, for each rack, the highest simulated draw over all steps.

## GraphQL

Every model is in NetBox's GraphQL schema: `design` / `design_list`,
`design_group`, `design_placement`, `design_step`, `planned_rack`,
`planned_power_feed`, `template`, `template_group`, `template_placement` (each
with a `_list`).

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
