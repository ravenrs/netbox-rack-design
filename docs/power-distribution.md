# Power distribution

The plugin computes how each planned device's power draw distributes across the
rack's PDUs, feeds, and banks for a **per-bank power heatmap**. The projection
reads existing `dcim` data (real PDUs, feeds, device draws) and remains read-only
— your NetBox records are never modified. The heatmap overlay shows load per bank
(colored health bar), feed-leg redundancy (header colors), and overload warnings.

## Configuration

All settings live in `PLUGINS_CONFIG`:

```python
PLUGINS_CONFIG = {
    "netbox_rack_design": {
        # one of: "none" (default), "builtin", "script"
        "distribution_mode": "none",

        # dotted path to a callable, used when distribution_mode == "script"
        "distribution_script": "",

        # Custom-field bridge for planning dialogs (optional; empty by default)
        "planning_fields": {},
    },
}
```

## Mode: `none` (default)

Per-device rack-share gradient — the current behavior. PDUs are excluded
infrastructure. The heatmap colors each tile by the device's share of total
rack power.

## Mode: `builtin`

Zero-configuration distribution across PDUs and banks, built on two
**universal naming conventions** that work on any NetBox instance:

```python
"distribution_mode": "builtin",
```

### The two universal conventions

The built-in mode works without a script or custom config, iff:

1. **Bank = first segment of the outlet port name.** A PDU outlet is named
   `"<bank>/<port>"`; the part before `/` is the bank ID (`1/1` → bank 1, `2/3`
   → bank 2). This is how NetBox's native outlet naming works — no custom
   parsing.

2. **Feed-leg = the feed a PDU is bound to.** A PDU draws from **one feed**
   (real `dcim.PowerFeed` or planned `DesignPowerFeed`); the feed's identity
   determines the redundancy leg (A or B), never the PDU's device name. Binding
   is explicit — you choose which feed each planned PDU uses when you add it to
   the rack.

Under these conventions, the plugin distributes each device's load per bank:

- **Cabled device** (real or planned, with a power outlet on a PDU) → charge the
  outlet's bank directly.
- **Uncabled device** (planned, not yet cabled) → its position in the rack
  determines which bank feeds it; redundant devices (2+ PSUs) charge both
  feed-legs in full (worst-case failover).

Per-bank breaker = `pdu_input_draw / power_bank_count` (where
`power_bank_count` is the distinct bank IDs on that PDU).

## Mode: `script`

For distribution behavior a configuration cannot express, point `distribution_script` at any
importable callable with the signature `fn(rack, devices) -> Distribution | None`.
The callable receives the planned `dcim.Rack` and the list of planned consumers
+ PDUs, and returns a distribution dict (see
[pdu-distribution-spec.md](./pdu-distribution-spec.md) §3 for the full schema),
or `None` when the rack has no PDUs to distribute across (heatmap stays
per-device).

It runs inside the NetBox process with full ORM read access — by convention,
read-only: compute a structure, never write to `dcim`.

### Ready-to-run example (ships with the plugin)

The plugin ships a **stock-runnable** minimal example that reproduces the
built-in distribution verbatim under the two conventions, with no site-specific
data or custom field names:

```python
"distribution_mode": "script",
"distribution_script": "netbox_rack_design.distribution_example.build",
```

It is a **copyable template**: enable it as-is to see the distribution in action,
then copy and edit for your site's behavior customization (direction, breaker
ceilings, PSU redundancy schemes). Like the naming example, it reads custom fields
**generically** via the `planning_fields` config bridge (§ Planning fields below)
— no site-specific cf names are hardcoded in the shipped code.

### Fully-worked example

The plugin also ships a richer reference that fills in three customization
surfaces the minimal example leaves stubbed:

```python
"distribution_mode": "script",
"distribution_script": "netbox_rack_design.distribution_advanced_example.build",
# Optional: override the bank health-bar thresholds
"power_warn_pct": 75,
"power_critical_pct": 95,
"planning_fields": {
    "rack": [
        {"key": "power_limitation", "label": "Power limitation (W)",
         "type": "number", "source": "cf.power_limitation"},
        {"key": "pdu_location", "label": "PDU location", "type": "choice",
         "choices": ["top", "bottom"], "source": "cf.pdu_location"},
    ],
    "pdu": [
        {"key": "pdu_scheme", "label": "PDU topology label",
         "type": "text", "source": "cf.pdu_scheme"},
    ],
},
```

It demonstrates:

- **Topology scheme label** — computes a human-readable topology string
  (e.g., `"2x1PH2Banks"`) from the per-PDU bank-count signature, and allows a
  per-PDU override via a custom field.
- **Config-driven thresholds** — reads bank utilization thresholds
  (`power_warn_pct` / `power_critical_pct`) from plugin config, matching the
  naming engine's pattern, instead of hardcoding constants.
- **Per-PDU custom field** — reads the `planning_fields["pdu"]` config to show
  how a site can layer in PDU-specific planning data via the config bridge.

Both examples share the **same core algorithm and helper functions** with the
built-in — you are not re-implementing distribution; you are customizing
*behaviour* (direction, ceilings, scheme labels) by reading data through the
config bridge. Start from `distribution_example.py` and adapt it to your needs.

### Notes

- **Robust to misconfiguration.** If `distribution_script` can't be resolved
  (wrong or empty dotted path, module not importable, target not callable) — or
  the script raises while computing a distribution — the engine logs a warning
  and **falls back to the `none` heatmap** (per-device gradient). A broken or
  not-yet-loaded script therefore degrades the overlay gracefully rather than
  blocking the editor. Fix `distribution_script` to restore custom distribution.
- The structure the script returns remains read-only — the heatmap reads it,
  never writes. No `dcim` mutations, no design dirty flag.
- `rack.pk` is always set (a distribution runs on a saved rack). A script never
  receives a preview rack.

## Feed binding & planned PDUs

A real PDU cabled to a real `dcim.PowerFeed` sizes its breaker from that feed
electricals (voltage × amperage × phase, the native path). A **planned PDU** (a
placement add, not yet realized in `dcim`) has no real feed yet, so the plugin
models planned feeds the same way: a **`DesignPowerFeed`** row carries the same
electrical fields (voltage, amperage, phase, supply), and a planned PDU **binds**
to it just like a real PDU binds to a real feed.

### Dialog flows

When you add a PDU to a rack:

- **If the rack has real feeds** (a provisioned rack with `dcim.PowerFeed`
  records) — a **bind-to-feed picker** appears, listing the rack's feeds (real
  first, then any planned feeds you've defined). Pick one to bind the PDU.
- **If the rack has no real feeds** (greenfield planning) — a per-rack **Power**
  button opens the planned-power flow where you define feeds manually (name,
  voltage, amperage, phase) or copy them from another rack's real feeds.

After feeds are defined, each PDU binding travels with the design and is
restored on load. A PDU with no binding is omitted from the distribution (logged,
no error).

A planned PDU's custom fields — e.g., `pdu_location` (top/bottom, for directing
which bank claims which units) — come from one source:

- **Live device reference** — if you link the PDU to an existing real device via
  `power_source_device`, its custom fields are read live on every heatmap
  render. Edit the source device's fields and the heatmap updates immediately.
- **Manual entry** — via the planning dialog, driven by `planning_fields["pdu"]`
  entries. The values persist in `power_config` on Save.

The two are mutually exclusive per placement.

## Planning fields — custom-field bridge

Most sites keep power policy in **custom fields**: power limitations (rack
ceiling), PDU locations (direction), or future site-specific fields. The plugin
never hardcodes a site's custom-field names — instead, `planning_fields` in
`PLUGINS_CONFIG` maps them generically so the code never changes per deployment.

```python
"planning_fields": {
    "rack": [
        {"key": "power_limitation", "label": "Power limitation (W)",
         "type": "number", "source": "cf.power_limitation"},
        {"key": "pdu_location", "label": "PDU location", "type": "choice",
         "choices": ["top", "bottom"], "source": "cf.pdu_location"},
    ],
    "pdu": [
        {"key": "pdu_scheme", "label": "Custom PDU scheme",
         "type": "text", "source": "cf.pdu_scheme"},
    ],
}
```

Each entry declares:

- `key` — the name the script/dialog knows it by (e.g., `power_limitation`).
- `label` — what the UI shows.
- `type` — `number`, `text`, or `choice`; `choice` requires `choices`.
- `source` — where to read the value: `cf.<fieldname>` for custom fields.
  Dotted paths are self-documenting, matching the naming-template grammar.

The plugin reads from custom fields only (never native fields) and never writes
back to `dcim`. An empty `planning_fields` (the default) is fine — the base
builtin feature needs none. The dialogs then show only native inputs (copy-from-rack,
bind-to-feed).

## Writing and adapting a script

Copy `netbox_rack_design/distribution_example.py` and edit the parts that matter
to your site: the **direction** (unit-to-bank mapping if PDU placement is atypical),
**breaker ceilings** (per-bank limits), and **redundancy scheme** (which PSU lands
on which feed for a given device type).

### Key points

- **Contract**: `build(rack, devices) -> Distribution | None`. The `rack` is a
  real saved `dcim.Rack`; `devices` is a normalized list of planned consumers
  and PDUs. Return the `Distribution` dict (or `None`). It's read-only — compute,
  never write.
- **Config bridge**: read custom fields through `read_planning_fields(role, obj)`
  — this abstracts the `planning_fields` map, so a site's `rack_power_cap` maps to
  the key `power_limitation` your algorithm knows. Never hardcode cf names.
- **Shared helpers**: both shipped examples reuse `distribution._collect_pdus` and
  `distribution_example`'s `_unit_to_bank`, `_legs_for`, `_charge` functions.
  You are not reinventing the algorithm — you're configuring its behavior.
- **Absolute imports**: use `from netbox_rack_design.distribution import ...`
  (not relative imports) so the script keeps working if you copy it out of the
  package into `SCRIPTS_ROOT`. NetBox's `netbox_rack_design` package is always
  importable.

### Keeping the script in NetBox's `SCRIPTS_ROOT`

You can keep the script where NetBox already manages Python: `SCRIPTS_ROOT`
(default `<netbox>/scripts/`). A file there is importable as `scripts.<module>`,
so reference it as:

```python
"distribution_mode": "script",
"distribution_script": "scripts.power_distribution.build",
```

**Set the config once.** Edit `configuration.py` with the config above and
restart NetBox — this is the only step that needs a restart. While the script
isn't present yet, the distribution safely falls back to `none` (per-device
heatmap).

After that one-time config change, **adding or editing the script itself needs
no restart**. NetBox picks it up and the plugin imports the `build` function on
the next heatmap render.

#### Variant 1 — through the UI (Customization → Scripts → Add)

Use this to view and edit the script from the NetBox UI. NetBox lists a module
under Scripts only when it contains an `extras.scripts.Script` subclass, so wrap
the example with a tiny one:

```python
from extras.scripts import Script

class PowerDistributionScript(Script):
    class Meta:
        name = "Rack Design power distribution"
    def run(self, data, commit):
        self.log_info("Distribution script for the rack-design editor; nothing to run.")

# ... paste the helpers and build() function from distribution_example.py below ...
```

**Customization → Scripts → Add**, name the module `power_distribution`, and
paste the wrapped script. No restart — reopen a design and the new distribution
appears immediately. Edit it later the same way.

#### Variant 2 — copy the file into `SCRIPTS_ROOT`

If you don't need UI editing, just drop the file in:

```bash
cp .../netbox_rack_design/distribution_example.py  $SCRIPTS_ROOT/power_distribution.py
```

No restart needed; the plugin imports it on the next heatmap render.

> Referencing the package copy directly,
> `netbox_rack_design.distribution_example.build`, also works and needs no copy
> at all — use that if you only want the shipped defaults and no per-instance
> editing.

## Verify on your instance

A quick end-to-end check that distribution works the way you expect:

1. **Configure and restart.** Set `distribution_mode` in `PLUGINS_CONFIG`:

   ```python
   "distribution_mode": "builtin",
   ```

   Restart NetBox so the config is loaded.

2. **Open a rack with PDUs and planned devices.** Open a design in the editor
   and add devices (or view an existing design with devices already placed).
   Ensure the rack has real or planned `PowerFeed` records, and each PDU is
   bound to one.

3. **Toggle the power heatmap.** At the top of the editor, find the power
   (lightning bolt) icon / heatmap toggle. Turn it on. You should see:
   - Each PDU column labeled with the feed-leg header (A or B).
   - Inside each PDU, stacked colored banks (green → orange → red by load %).
   - Device tiles with A/B feed-leg edges (if the device is redundant).
   - Instant tooltips on hover (used W / breaker W per bank).
   - Overload warnings in red if any bank exceeds its breaker.

4. **Drag a device to a different rack unit.** The bank colors should update
   to reflect the new load distribution. If the device is cabled to a specific
   PDU outlet, it charges that bank; if uncabled (planned), it charges the bank
   corresponding to its new U position.

5. **Check custom fields (if using `planning_fields`).** Add a rack with a
   `power_limitation` custom field. Open the **Power** dialog and verify the
   field appears as configured. Same for PDU-role fields if your script reads
   them.

Nothing here writes to `dcim`: distribution only ever runs on the read-only
projection, never on page load or on **Save**.

### Switching between modes

- **`none` → `builtin`**: Restart NetBox. The heatmap now shows per-bank colors
  instead of per-device.
- **`builtin` → `script`**: Set `distribution_script` and restart. The script
  replaces the built-in; behavior may differ if the script implements
  customizations.
- **`script` → `none`**: Set `distribution_mode` to `none` and restart. The
  heatmap reverts to per-device gradient.

A misconfigured or broken script (wrong path, unimportable module, runtime error)
degrades to `none` gracefully — logs a warning, never errors the editor.

## Troubleshooting

**I see a per-device gradient, not per-bank colors.**

- Check `distribution_mode` in `PLUGINS_CONFIG` — if it's `"none"`, that's
  expected (the default). Set it to `"builtin"` or `"script"` and restart.
- If using `"script"`: check that `distribution_script` is a valid importable
  path and the function exists. Enable [DEBUG logging](./pdu-distribution-spec.md)
  to see import/execution errors logged on each heatmap render.

**The heatmap looks incomplete; some PDUs are missing.**

- Check that each PDU is **bound to a feed** (real or planned). A PDU with no
  binding is omitted from the distribution (logged at debug level).
- Check that feeds exist: real feeds for a provisioned rack, or planned feeds
  defined via the **Power** button for greenfield racks.
- Check that outlet ports follow the `"<bank>/<port>"` convention (e.g., `1/1`,
  `2/3`). An outlet named `pdu-outlet-1` with no `/` has no bank and is ignored.

**The distribution looks wrong; devices are charged to the wrong banks.**

- Verify cabling: a cabled device should charge the PDU outlet's bank directly
  (the first segment before `/`).
- Verify uncabled devices are placed at their intended U positions. The rack
  distribution assumes unit-to-bank mapping based on PDU location and bank
  count; a device at U5 might charge a different bank than U7.
- Check `pdu_location` if using the config bridge: `"top"` or `"bottom"`
  determines the direction (default `"bottom"`).
- Inspect the debug logs: `logger.debug` messages in `distribution_example.py` /
  `distribution_advanced_example.py` trace each device charge and unit mapping.

**Warnings or errors appear at the top of the editor.**

- If a warning mentions an overload or power limitation breach, check the
  specific bank/PDU and device draws. Thresholds (`power_warn_pct`,
  `power_critical_pct`) default to 80% / 100%; override them in `PLUGINS_CONFIG`
  or in a custom script.
- If a warning mentions an unresolvable `power_source_device`, the planned PDU
  linked to a real device whose custom fields were requested, but the device was
  deleted. The distribution falls back to manual `power_config` fields (or `{}`
  if none); no error, just logged.

**How do I troubleshoot a custom script?**

- Enable [DEBUG logging](./pdu-distribution-spec.md) on the plugin
  (`netbox_rack_design.distribution*`). Each heatmap render logs entry, per-device
  charges, and the final `Distribution` object.
- Add `logger.debug()` calls to your script to trace its own logic.
- Test in a dev/staging environment first; a broken script degrades the heatmap
  but never breaks the editor.
- Restart NetBox once to load a config change (`distribution_script` path); after
  that, editing the script file alone needs no restart.

## See also

- [PDU distribution specification](./pdu-distribution-spec.md) — the full design
  reference, feed model details, and `Distribution` data contract.
- [Device naming](./device-naming.md) — the naming engine, which follows the same
  configuration patterns (mode selection, built-in, script, config bridge).
