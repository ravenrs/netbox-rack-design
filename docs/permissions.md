# Permissions

The plugin uses NetBox's own object permissions — no plugin-specific roles.
Grant them to groups in **Admin → Permissions**, with constraints if you want
them scoped (per site, per tenant). This page lists what each kind of user
needs.

## Reviewer — sees plans, changes nothing

| Object types | Actions |
|---|---|
| `netbox_rack_design` · design, design group, design placement, planned power feed, design rack power, planned rack, template, template group, template placement | view |
| `netbox_rack_design` · design apply | view |
| `dcim` · site, location, rack, device, device type, device role, power panel, power feed, power port, power outlet | view |

With view rights a user can open design pages, lists, the projected
elevations and the editor itself; saving in the editor needs the planner
rights below. For a strictly read-only audience, point them at **Elevations**
rather than the editor.

## Planner — edits designs

Everything a reviewer has, plus:

| Object types | Actions | Why |
|---|---|---|
| design, design placement | add, change, delete | Creating designs; the editor's Save writes placements |
| planned power feed, design rack power | add, change, delete | The rack Power dialog (planned feeds, copy from rack, overrides) |
| planned rack | add, change, delete | **Create rack** in the editor |
| template, template group, template placement | add, change, delete | **Save as template**, template groups (omit to let planners only *use* templates) |
| favorite set, favorite device type, hidden design rack, hidden design chassis | add, change, delete | The user's own favorites and rack/chassis visibility — per-user state, safe to grant broadly |

Only `view` on DCIM is needed for planning: a planner never writes to DCIM.

## Whoever applies

Applying writes real DCIM objects **with the applying user's own
permissions** — there is no service account (see
[Applying a design](apply.md#permissions)). On top of `change` on the design:

| Object types | Actions | Needed when |
|---|---|---|
| `dcim.device` | add, change, delete | Always: planned devices are created, updated, flagged; a re-apply may delete one whose placement is gone |
| `dcim.rack` | add, change | The design holds planned racks (created, or adopted) |
| `dcim.powerfeed` | add | The design holds planned power feeds |
| `dcim.cable` | add | Planned feeds get their PDUs cabled |

Each object is checked in the site it lands in, so on a multi-site design the
user needs these rights in **every** site the design touches. Anything
missing is reported on the Apply page as a problem, and nothing is written.

## Scoping with constraints

Design permissions scope by site through the design's `sites`:

```json
{"sites__slug__in": ["ams1", "ams2"]}
```

Placements, planned feeds and rack power scope through their design:
`{"design__sites__slug__in": [...]}`; planned racks through their location:
`{"location__site__slug__in": [...]}`. (Designs used to have a single `site`;
constraints written as `{"site__slug": ...}` must be updated to `sites__…`.)

## Related

- [Applying a design](apply.md)
- [Templates](templates.md#permissions)
