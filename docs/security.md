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
- Templates, template groups and the new-version/rebase actions use the plugin's
  own `view_*` / `add_*` permissions on the objects involved.

## What a hidden design looks like

A design the user may not view is never listed. Where another design links to
it, the user sees this:

| Where | Direct link (`based_on`, `root`, `group`) | List entry (ancestor chain) | Lists of children, dependents, "referenced by" |
|---|---|---|---|
| REST | Shows the linked design as in NetBox core | `chain` returns a placeholder `{"title": "(restricted design)", "restricted": true, "id": null, "url": null, "version": null, "status": null}` | Hidden designs are left out |
| GraphQL | `null` | `ancestors` is `null` at that position; `lineage` has `restricted: true` and the title `(restricted design)` | Hidden designs are left out (200 entries at most) |
| HTML | Link and title of the direct parent | `(restricted design)`, no link or status | Hidden designs are left out |

`depends_on` is shown as it is in the design's own page (HTML, REST) and is
limited to the designs the user may view in GraphQL.

The chain keeps its length so the reader still sees how deep the lineage is.
Error and refusal messages name the designs the user may view and count the
rest ("2 design(s) you cannot view").

[Peer conflicts](peer-conflicts.md) are the one deliberate exception: a peer
design's **title** appears in a conflict, nothing else about it. Set
`peer_conflicts_enabled` to `False` to turn that off.

## Operator recommendations

- **GraphQL depth.** Set `GRAPHQL_MAX_QUERY_DEPTH` in NetBox's
  `configuration.py` (unset by default); `10` to `15` is enough for ordinary
  queries and stops a client nesting `children`, `ancestors` or `lineage`
  without limit. See [GraphQL](api.md#design-lineage-and-permissions).
- **Reindex after upgrading to 1.2.2.** Planned power feed search results no
  longer carry the design name; rebuild the index to drop it from cached results:

    ```bash
    python manage.py reindex netbox_rack_design
    ```

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
