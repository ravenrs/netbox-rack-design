<p align="center">
  <img src="https://raw.githubusercontent.com/ravenrs/netbox-rack-design/main/docs/assets/icon-500.png" alt="NetBox Rack Design" width="120" height="120" />
</p>

# NetBox Rack Design

**Plan rack changes as versioned designs — on top of your real NetBox data, without touching it until you're ready.**

NetBox Rack Design adds a lightweight *design layer* to NetBox for planning device adds, moves, and removals in your racks. A **Design** is a named, versioned proposal that overlays your live DCIM data: your real `dcim.Device` and `dcim.Rack` records stay untouched, and each planned change — add, move, or remove — is captured as a structured **placement** instead of a spreadsheet cell. This brings the *intended* rack layout into NetBox and renders it as a projected rack elevation, with power projection and an auto-naming engine already built in — an explicit Apply step and conflict detection are still arriving in later stages.

The plugin is fully generic and public — nothing organization-specific is hardcoded. Status names and behavior are driven entirely by `PLUGINS_CONFIG`, and only native NetBox mechanisms are used (change logging, tags, custom fields, permissions, REST + GraphQL APIs, global search).

## Features

Rack Design pairs a structured data model with an interactive visual editor for composing rack plans. The apply/conflict features are planned (see [Roadmap](#roadmap)).

- **Three models** for capturing rack plans:
  - **Design** — a proposed set of rack changes for a site, scoped to one or more racks. Versioned (clone-and-tweak, with one approved version per plan), ordered for execution per site via an auto-assigned `sequence`, may declare explicit `depends_on` relationships, and may optionally belong to a group. Carries `title`, `status`, `summary`, generic external `link`, plus description/comments/tags/custom fields.
  - **DesignGroup** — an optional, hierarchical container that links related designs into a larger effort (multi-stage work or cross-site coordination). Purely organizational; never affects execution order.
  - **DesignPlacement** — a single proposed change within a design: **add** a new device from the device-type catalog (with an intended role and tenant), **move** an existing device, or **remove** (planned) one. Target slots are validated against NetBox's own `Rack.get_available_units()` collision logic. Real devices are never mutated.
- **Interactive multi-rack visual editor** — a GridStack drag-and-drop editor that renders all of a design's racks side by side, across both front and rear faces, for composing adds/moves/removes. Includes a searchable **device-type catalog palette**, **per-user favorite device types** for quick access, and **per-user rack visibility** to focus the workspace. Every edit writes placements only — live devices are never touched.
- **Projected rack elevations** — a read-only elevation view showing how a design's racks *would* look once applied (all racks, both faces, full-depth devices rendered across both faces), plus a filterable elevations list.
- **Rack-page integration** — an optional panel on the core `dcim.rack` detail page listing the designs that touch that rack, each linking to its editor and elevation.
- **Config-driven statuses** — which device statuses count as "planned" and which mark a planned removal are read from `PLUGINS_CONFIG`, never hardcoded.
- **Naming convention engine** — auto-names planned devices via `naming_mode` = `sequence` / `template` / `script` (a dotted-path callable), with graceful fallback when a template or script fails. See [docs/device-naming.md](docs/device-naming.md).
- **Power projection & PDU distribution** — a read-only power overlay: a per-rack capacity-vs-projected-consumption bar plus a per-device power heatmap, and per-PDU/per-bank power distribution (`distribution_mode` = `none` / `builtin` / `script`) with planned-PDU feed binding. See [docs/power-projection-spec.md](docs/power-projection-spec.md), [docs/power-distribution.md](docs/power-distribution.md), and [docs/pdu-distribution-spec.md](docs/pdu-distribution-spec.md).
- Full **CRUD UI** with list/detail/edit/bulk views and a navigation menu.
- **REST API** at `/api/plugins/rack-design/`.
- **GraphQL API** integration.
- **Global search** integration.
- **Change logging**, **tags**, and **custom fields** on the models.
- Integration with NetBox's native **permission** system.

## Screenshots

_Screenshots coming soon. In the meantime, see the [documentation](https://ravenrs.github.io/netbox-rack-design/) for the editor and elevation views._

## Compatibility

| Plugin Version | Minimum NetBox Version | Maximum NetBox Version | Python    |
|----------------|------------------------|------------------------|-----------|
| 0.13.2         | 4.4.0                  | 4.4.99                 | 3.12+     |

The supported NetBox range is enforced at load time via the plugin's `min_version` / `max_version`. See [COMPATIBILITY.md](COMPATIBILITY.md) for the full per-version matrix.

## Dependencies

- **NetBox** 4.4.0 – 4.4.99
- **Python** 3.12 or later

No additional Python packages are required beyond NetBox's own dependencies.

## Installation

Install from PyPI into the same environment as your NetBox installation:

```bash
pip install netbox-rack-design
```

For NetBox Docker, add `netbox-rack-design` to your `plugin_requirements.txt`. See the
[netbox-docker plugin instructions](https://github.com/netbox-community/netbox-docker/wiki/Using-Netbox-Plugins).

Enable the plugin in your NetBox configuration (`configuration.py`, or `plugins.py` for netbox-docker):

```python
PLUGINS = [
    "netbox_rack_design",
]

# Optional — defaults shown. Only include keys you want to override.
PLUGINS_CONFIG = {
    "netbox_rack_design": {
        "planned_statuses": ["planned"],
        "removal_statuses": ["decommissioning"],
        "default_status": "draft",
        "enable_rack_panel": True,
    },
}
```

> **Note on `removal_statuses`.** The default `decommissioning` is the only native
> removal-oriented device status on a vanilla install. If `decommissioning` is
> *destructive* in your environment (e.g. it auto-deletes devices or triggers an
> external dismantle workflow), do **not** use it for planned removals. Instead add a
> safe custom status via NetBox's `FIELD_CHOICES` (for `dcim.Device.status`, e.g.
> `to_decommission`) and point `removal_statuses` at it.

Apply migrations and restart NetBox:

```bash
python manage.py migrate
# then restart your NetBox services (e.g. systemctl restart netbox netbox-rq)
```

## Configuration

All settings are optional and configured under the `netbox_rack_design` key in `PLUGINS_CONFIG`.

| Key                 | Default              | Description                                                                                                  |
|---------------------|----------------------|--------------------------------------------------------------------------------------------------------------|
| `planned_statuses`  | `["planned"]`        | Device statuses the plugin treats as "planned".                                                              |
| `removal_statuses`  | `["decommissioning"]`| Device statuses that mark a planned removal. Override with a safe custom status where `decommissioning` is destructive (see note above). |
| `default_status`    | `"draft"`            | Default lifecycle status for a new Design.                                                                    |
| `enable_rack_panel` | `True`               | Show the rack-page panel listing designs that touch a rack.                                                  |
| `naming_mode`       | `"sequence"`         | How a placement's proposed name is computed: `"sequence"` (`<design title>-<n>`), `"template"` (a `str.format` template over real model objects), or `"script"` (a dotted path to `fn(placement) -> str`). See [docs/device-naming.md](docs/device-naming.md). |
| `naming_template`   | `"{design.name}-{n}"`| Template used when `naming_mode == "template"`. Dotted attribute paths on the real Design/Device objects; `{design.name}` aliases the design title. |
| `naming_script`     | `""`                 | Dotted path to a callable used when `naming_mode == "script"`.                                                |
| `distribution_mode` | `"none"`             | How per-PDU/bank load is distributed for the power heatmap: `"none"` (per-rack total only, per-device gradient), `"builtin"` (native distribution from bank = outlet port name segment + feed-leg = bound feed, zero config), or `"script"` (a dotted path to `fn(rack, devices) -> Distribution` dict). See [docs/pdu-distribution-spec.md](docs/pdu-distribution-spec.md). |
| `distribution_script` | `""`               | Dotted path to a callable used when `distribution_mode == "script"`.                                          |
| `planning_fields`   | `{}`                 | Custom-field bridge mapping site custom fields into the rack/PDU planning dialogs. Empty by default; native fields (voltage/amperage/phase/supply, feed binding) are never listed here. |
| `power_capacity_default_w` | `1000`        | Fallback rack power capacity (watts) used when no `dcim.PowerFeed` is modeled on the rack. Not present in `default_settings`; read via `get_plugin_config` with this default. |
| `power_draw_basis`  | `"allocated"`        | Which PowerPort/PowerPortTemplate field to sum for projected draw: `"allocated"` or `"maximum"` (falls back to the other when the chosen one is unset). Not present in `default_settings`; read via `get_plugin_config` with this default. |
| `power_warn_pct`    | `80`                 | Utilization percentage at/above which a rack's power state is "warn". Not present in `default_settings`; read via `get_plugin_config` with this default. |
| `power_critical_pct`| `100`                | Utilization percentage at/above which a rack's power state is "critical". Not present in `default_settings`; read via `get_plugin_config` with this default. |
| `power_exclude_roles` | `("pdu", "unmanageable-pdu")` | Device role slugs (case-insensitive) excluded from the power-consumption sum — power infrastructure, not consumers. Not present in `default_settings`; read via `get_plugin_config` with this default. |

The `power_*` keys are not listed in the plugin's `default_settings` (they have no admin-facing default in `__init__.py`); they are still fully overridable via `PLUGINS_CONFIG`, resolved at read time by `netbox_rack_design/projection.py` with the defaults shown above.

## Roadmap

**Delivered**

- **Projected rack elevations (read-only)** — see how a design's racks *would* look once applied, with front/rear faces and full-depth devices rendered across both faces.
- **Interactive visual rack editor** — GridStack drag-and-drop adds/moves/removes across a **multi-rack workspace** and both rack faces, writing placements without mutating live devices. Includes a searchable device-type catalog palette, per-user favorite device types, and per-user rack visibility.
- **Multi-rack designs** — a design carries an explicit, site-validated rack scope and a read-only elevation view spanning all of its racks.
- **Naming convention engine** — auto-names planned devices via `naming_mode` = `"sequence"` / `"template"` / `"script"`, with graceful fallback when a template or script fails.
- **Power projection** — config-driven capacity vs. projected consumption per rack, rendered as a capacity bar plus a per-device power heatmap.
- **PDU power distribution** — per-PDU/per-bank load distribution (`distribution_mode` = `"none"` / `"builtin"` / `"script"`), planned-PDU feed binding for greenfield racks, and a per-bank heatmap.

**Planned for upcoming stages**

- **Apply ("Make in NetBox")** — an explicit step that materializes an approved design into real planned devices and applies removal statuses.
- **Conflict detection** — block approval of designs that conflict with an approved baseline, including dependency conflicts between designs.
- **Template-driven export** — generate work documents from a design via NetBox's native Export Templates.

## Support

- **Documentation:** https://ravenrs.github.io/netbox-rack-design/
- **Issues / bug reports / feature requests:** https://github.com/ravenrs/netbox-rack-design/issues

When reporting a bug, please include your NetBox version, plugin version, Python version, steps to reproduce, and expected vs. actual behavior.

## Contributing

Contributions are welcome. Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Licensed under the [Apache License 2.0](LICENSE).

---

This package was created with [Cookiecutter](https://github.com/audreyr/cookiecutter) and the [`netbox-community/cookiecutter-netbox-plugin`](https://github.com/netbox-community/cookiecutter-netbox-plugin) template.
