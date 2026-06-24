# NetBox Rack Design

**Plan rack changes as versioned designs — on top of your real NetBox data, without touching it until you're ready.**

NetBox Rack Design adds a lightweight *design layer* to NetBox for planning device adds, moves, and removals in your racks. A **Design** is a named, versioned proposal that overlays your live DCIM data: planned devices stay real `dcim.Device` records (`status=planned`), and each change is captured as a structured placement instead of a spreadsheet cell. This brings the *intended* rack layout into NetBox — the prerequisite for projected rack elevations, conflict detection, and power projection arriving in later stages.

The plugin is fully generic and public — nothing organization-specific is hardcoded. Status names and behavior are driven entirely by `PLUGINS_CONFIG`, and only native NetBox mechanisms are used (change logging, tags, custom fields, permissions, REST + GraphQL APIs, global search).

## Features

This is the **Stage 1** release: the data model and the standard NetBox object surface for it. The interactive visual editor and the apply/conflict/power features are planned (see [Roadmap](#roadmap)).

- **Three models** for capturing rack plans:
  - **Design** — a proposed set of rack changes for a site. Versioned (clone-and-tweak, with one approved version per plan), ordered for execution per site via an auto-assigned `sequence`, may declare explicit `depends_on` relationships, and may optionally belong to a group. Carries `title`, `status`, `summary`, generic external `link`, plus description/comments/tags/custom fields.
  - **DesignGroup** — an optional, hierarchical container that links related designs into a larger effort (multi-stage work or cross-site coordination). Purely organizational; never affects execution order.
  - **DesignPlacement** — a single proposed change within a design: **add** a new device from the device-type catalog, **move** an existing device, or **remove** (planned) one. Target slots are validated against NetBox's own `Rack.get_available_units()` collision logic. Real devices are never mutated.
- **Config-driven statuses** — which device statuses count as "planned" and which mark a planned removal are read from `PLUGINS_CONFIG`, never hardcoded.
- Full **CRUD UI** with list/detail/edit/bulk views and a navigation menu.
- **REST API** at `/api/plugins/rack-design/`.
- **GraphQL API** integration.
- **Global search** integration.
- **Change logging**, **tags**, and **custom fields** on the models.
- Integration with NetBox's native **permission** system.

## Screenshots

_Screenshots coming with the Stage 2 visual editor._

## Compatibility

| Plugin Version | Minimum NetBox Version | Maximum NetBox Version | Python    |
|----------------|------------------------|------------------------|-----------|
| 0.1.0          | 4.4.0                  | 4.4.99                 | 3.12+     |

The supported NetBox range is enforced at load time via the plugin's `min_version` / `max_version`. See [COMPATIBILITY.md](COMPATIBILITY.md) for the maintained matrix.

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

## Roadmap

Planned for upcoming stages:

- **Interactive visual rack editor** — drag-and-drop adds/moves/removes across multi-rack, front/rear, and non-racked views (GridStack-based), writing placements without mutating live devices.
- **Apply ("Make in NetBox")** — an explicit step that materializes an approved design into real planned devices and applies removal statuses.
- **Conflict detection** — block approval of designs that conflict with an approved baseline, including dependency conflicts between designs.
- **Power projection** — config-driven capacity vs. projected consumption per design.
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
