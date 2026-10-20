# Security

How the plugin applies NetBox's permission model, what a user who may not see
a design gets to see, and how to report a vulnerability.

## Permission model

The plugin's own object permissions are the boundary:

- `view_design`, `change_design` and `add_design` (and their constraints,
  for example `{"sites__slug__in": ["ams1"]}`) decide what a user reads and
  edits. Every REST action, HTML view, GraphQL field and search result goes
  through the restricted queryset for the user.
- Reading the DCIM data a design touches (racks, devices, power feeds shown in
  the editor) does **not** require extra `dcim.view_*` permissions; the
  design permission is the boundary. Racks, devices and feeds sent to a design
  action must belong to one of the design's sites.
- Writes to DCIM happen only in [Apply](apply.md) and run with the applying
  user's own permissions: each created, updated or removed object is checked
  (`dcim.add_rack` for a planned rack, `dcim.add_device`, `dcim.change_device`,
  ...) and again after it is saved. A missing permission is reported as a
  problem and nothing is written. See [Permissions](permissions.md).
- Custom REST actions honour a read-only API token: a token with writes
  disabled cannot call a write action.
- Templates, template groups and the new-version action use the plugin's own
  `view_*` / `add_*` permissions on the objects involved.
- A link a client writes over REST (`based_on`, `depends_on`, a placement's
  `design`, `base_placement`, a planned feed's or step's `design`, the rebase
  target, and the ids `save-layout` sends) resolves any existing object, whether
  or not the user may view it, exactly as NetBox core's writable nested
  serializers do (`PATCH` a device's `tenant` to a tenant you cannot view works
  in core). An id that does not exist is refused. The HTML forms and pickers are
  core's: their choices are limited to what the user may view.
- **Derive** follows the trust model: an *approved* design can be cloned by
  every `add_design` holder, `view_design` or not. **New version** copies the
  whole design, so it needs `view_design` on the source too.
  Every create-like action (derive, new-version, create-planned-rack) and re-base
  re-checks the new row against the user's own `add` / `change` constraints after
  it is written and undoes the whole action if the row falls outside them.
- Creating a planned rack from the editor needs `add_plannedrack` (a constraint
  such as `{"location__site__slug": "ams1"}` is honoured). A "Create rack" name
  pattern may expand to at most 200 racks.
- **Design actions write their rows under the design permissions.**
  `save-layout`, `save-steps`, `rerun-naming`, `planned-feed`, `copy-feeds`,
  `rack-power` and `new-version` create and change placements, steps and
  planned feeds of the design as part of one action, and are authorised by the
  permissions on the **design** (`change_design`, plus `add_design` for
  `new-version`). Object constraints placed on the `DesignPlacement`,
  `DesignStep` or `DesignPowerFeed` permissions are not applied by those
  actions; they apply to the plugin's own REST endpoints and views for those
  rows. To confine a team, constrain the design permission (for example by
  `sites`), not the rows inside it.
- Template extraction from a design or a rack re-checks a constrained
  `add_template` permission on the new template after writing it.
- Extracting a template **from a rack** (`templates/from-rack/`, REST and MCP
  only; the editor does not use it) needs `dcim.view_rack` for that rack: it is
  not scoped to a design.

## What a hidden design looks like

The plugin follows what NetBox core does today. Core restricts **lists and
pickers** to what the user may view, and still **names** a related object it
shows (a device's tenant, a region's parent in the breadcrumbs, the dependents
in a "cannot delete" message) even when the user could not open it; the link
then answers 404/403. Designs behave the same way:

| Where | Lists (children, "required by", elevation browser, rebase picker) | A related design that is named (parent, ancestor stack, error and guard messages, conflict sources) |
|---|---|---|
| REST | Hidden designs are left out | Named with its real title; `GET` of it is 404 |
| GraphQL | Hidden designs are left out (200 entries at most) | `based_on`, `root` and a placement's, feed's or step's `design` are `null` (a plain foreign key, like core's `device { tenant }`); `ancestors` returns the whole chain, like core's `region { parent }` |
| HTML | Hidden designs are left out | Named, linked; the link 404s |

A list that hangs off an object follows the same rule: the PDUs bound to a
planned feed (feed page, design page, feed list) are only the placements of a
design the reader may view (or that their placement permission covers), so a PDU
in a descendant design they cannot view is left out. The derive page
of a draft the reader cannot view is a 404; an approved design is open to every
`add_design` holder, as above.

So a chain broken by an ancestor you cannot view says which ancestor and why
(`ancestor_not_approved`, `ancestor_implemented`, `chain_broken`), the editor's
hover card names the design an inherited tile comes from, and a delete refusal
lists every design that blocks it.

[Peer conflicts](peer-conflicts.md) name a peer design's **title** and nothing
else about it (not where it puts a device). Set `peer_conflicts_enabled` to
`False` to turn that off.

## Operator recommendations

- **GraphQL depth.** Set `GRAPHQL_MAX_QUERY_DEPTH` in NetBox's
  `configuration.py` (unset by default); `10` to `15` is enough for ordinary
  queries and stops a client nesting `children` or `ancestors`
  without limit. See [GraphQL](api.md#design-lineage-and-permissions).
- **Scripts.** Naming and distribution scripts run server-side Python from
  your configured script storage. Only administrators who control that storage
  control the code that runs.
- **Object-constrained permissions** written as `{"site__slug": ...}` for
  designs must use `sites__…` (see [Permissions](permissions.md#scoping-with-constraints)).

## Reporting a vulnerability

Please do not open a public issue. Use a private GitHub security advisory on
the repository: **Security → Report a vulnerability** at
[ravenrs/netbox-rack-design](https://github.com/ravenrs/netbox-rack-design/security/advisories/new).
Include the plugin and NetBox versions, the permission or token the attacker
needs, and the request or steps to reproduce.
