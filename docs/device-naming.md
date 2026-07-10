# Device naming

The plugin proposes a name for every planned device (palette adds and moved
devices) through a configurable **naming engine**. The proposal appears in the
editor's rename dialog and is stored on the placement as `proposed_name` — your
live `dcim` data is never modified.

## Configuration

All settings live in `PLUGINS_CONFIG`:

```python
PLUGINS_CONFIG = {
    "netbox_rack_design": {
        # one of: "sequence" (default), "template", "script"
        "naming_mode": "sequence",

        # used when naming_mode == "template"
        "naming_template": "{design.name}-{n}",

        # dotted path to a callable, used when naming_mode == "script"
        "naming_script": "",
    },
}
```

## Mode: `sequence` (default)

`<design title>-<n>` where `n` is the placement's 1-based ordinal within its
design. Zero configuration, always safe.

## Mode: `template`

A `str.format`-style template over **dotted attribute paths on real NetBox
model objects** — not flat aliases. Available roots:

| Token root | Resolves to |
|---|---|
| `{design...}` | the `Design` (`{design.name}` is an alias for its title; any real attribute works: `{design.site.name}`, …) |
| `{device...}` | for moves/removes: the real `dcim.Device` (full attribute tree). For adds: a placement-backed proxy exposing the same paths (`{device.site.name}`, `{device.device_type.model}`, `{device.rack.name}`, `{device.role.name}`, `{device.tenant.name}`, `{device.position}`, `{device.face}`) |
| `{n}` | the placement's ordinal |

Traversal is safe: a missing attribute renders as an empty string and never
raises.

```python
"naming_template": "{design.name}-{device.site.name}-{device.role.name}-{n}"
# -> "Migration-AMS1-Server-3"
```

## Mode: `script`

For conventions a template cannot express, point `naming_script` at any
importable callable with the signature `fn(placement) -> str`. The callable
receives the (possibly unsaved) `DesignPlacement` and returns the proposed
name. It runs inside the NetBox process with full ORM access — read-only by
convention: compute a string, never write.

### Ready-to-run example (ships with the plugin)

The plugin ships a small, **stock-runnable** example you can enable as-is — no
extra data, no custom role slugs, no lookup tables:

```python
"naming_mode": "script",
"naming_script": "netbox_rack_design.naming_example.build_name",
```

It demonstrates the two patterns a template cannot express:

- **Family counter** — continue a numbered family (`ams1-leaf-switch-1` → next
  is `-2`) by asking NetBox for the highest existing number, so you never
  hand-pick the next digit. It also counts names proposed by *other unsaved
  tiles in the same editor session*, so two quick drops get consecutive numbers
  instead of colliding.
- **Phase pairs** — PDUs run `a1, b1, a2, b2, …` (an A/B phase pair per index)
  instead of a flat counter.

Copy `netbox_rack_design/naming_example.py` and edit the rules to match your
convention. The fully-worked, heavily-commented version below shows a richer
multi-rule corporate scheme built the same way.

### Fully-commented example

The example below implements a realistic multi-rule corporate convention and
demonstrates the most valuable script-only trick: **continuing a numbered
family** (`ams1-sw7050-3` → next device becomes `…-4`) by querying NetBox for
the highest existing number — the lookup engineers otherwise do by hand.

Save it anywhere importable by NetBox (e.g. next to `manage.py`) and set:

```python
"naming_mode": "script",
"naming_script": "my_naming.build_name",
```

```python
"""Example naming script for netbox-rack-design (naming_mode = "script")."""

import re

# ---------------------------------------------------------------------------
# Convention tables. In a real deployment these encode your naming standard;
# extend them freely — this is plain Python.
# ---------------------------------------------------------------------------

# Device-type part number -> short type code used inside the name.
TYPE_CODES = {
    "DCS-7050CX3-32S-R": "sw7050",
    "DCS-7010T-48-R": "sw7010",
    "AP8853": "pdu",
    # ... add your fleet here
}

# Role slug -> the role token embedded in the name (network gear only).
ROLE_CODES = {
    "core-switch": "core",
    "leaf-switch": "leaf",
    "oob-switch": "oob",
}

# Roles whose devices are numbered per PROJECT rather than per family.
PROJECT_NUMBERED_ROLES = {"server", "disk-enclosure"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_number(placement, prefix):
    """Continue a numbered family: find the highest existing <prefix><digits>
    device name in NetBox — plus any names already proposed in this editing
    session (the plugin passes them on the placement) — and return max + 1.

    This automates the manual "open NetBox and find the last number" step.
    """
    from dcim.models import Device

    tail = re.compile(r"^" + re.escape(prefix) + r"(\d+)$")
    highest = 0
    # Saved devices in NetBox:
    for name in Device.objects.filter(name__startswith=prefix).values_list(
        "name", flat=True
    ):
        m = tail.match(name)
        if m:
            highest = max(highest, int(m.group(1)))
    # Names proposed by OTHER unsaved tiles in the same editor session, so two
    # pending adds get consecutive numbers instead of colliding:
    for name in getattr(placement, "_rd_pending_names", []):
        m = tail.match(name or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return str(highest + 1)


def _role_slug(placement):
    """The placement's role slug: the chosen role for adds, the real device's
    role for moves/removes."""
    role = placement.device_role or (
        placement.device.role if placement.device else None
    )
    return (role.slug if role else "").lower()


def _site_and_rack(placement):
    """Site/rack come from the TARGET rack (falling back to the real device)."""
    rack = placement.target_rack or (
        placement.device.rack if placement.device else None
    )
    site = rack.site.name if (rack and rack.site) else ""
    return site.lower(), (rack.name if rack else "")


# ---------------------------------------------------------------------------
# The entry point: fn(placement) -> str
# ---------------------------------------------------------------------------

def build_name(placement):
    role = _role_slug(placement)
    site, rack_name = _site_and_rack(placement)

    device_type = placement.device_type or (
        placement.device.device_type if placement.device else None
    )
    part = (device_type.part_number or device_type.model) if device_type else ""
    type_code = TYPE_CODES.get(part, "dev")

    # Rule 1 — project-numbered gear: PRJ-<project number>-<n>.
    # The project number is parsed from the design title (e.g. "PRJ-1042 ...").
    if role in PROJECT_NUMBERED_ROLES:
        m = re.search(r"PRJ-?(\d+)", placement.design.title, re.IGNORECASE)
        project = m.group(1) if m else placement.design.title
        prefix = f"PRJ-{project}-"
        return prefix + _next_number(placement, prefix)

    # Rule 2 — PDUs embed the rack: <site>-pdu-r<rack, cleaned>-<n>.
    if role in ("pdu", "unmanageable-pdu"):
        cleaned = re.sub(r"[./\-_:]", "", rack_name).lower()
        prefix = f"{site}-pdu-r{cleaned}-"
        return prefix + _next_number(placement, prefix)

    # Rule 3 — network gear: <site>-<type code>-<role code>-<n>,
    # numbering continued per family across all of NetBox.
    role_code = ROLE_CODES.get(role, role or "misc")
    prefix = f"{site}-{type_code}-{role_code}-"
    return prefix + _next_number(placement, prefix)
```

### Notes

- **Robust to misconfiguration.** If `naming_script` can't be resolved (wrong or
  empty dotted path, module not importable, target not callable) — or the script
  raises while computing a name — the engine logs a warning and **falls back to
  the default `sequence` name** (`<design title>-<n>`). A broken or not-yet-loaded
  script therefore degrades to sensible default names rather than blocking
  planning. Fix `naming_script` to restore custom naming.
- Everything the script proposes remains editable in the rename dialog — the
  engine suggests, the user decides.
- `placement.pk` is `None` for previews; never rely on the placement being
  saved.

## Verify on your test instance

A quick end-to-end check that naming works the way you expect:

1. **Configure and restart.** Set `naming_mode` / `naming_script` (or
   `naming_template`) in `PLUGINS_CONFIG` and restart NetBox so the new config
   is loaded. To start from the shipped example:

   ```python
   "naming_mode": "script",
   "naming_script": "netbox_rack_design.naming_example.build_name",
   ```

2. **Open a design in the editor** and drag a device from the palette onto a
   rack. The proposed name appears on the tile immediately (and in the rename
   dialog for a moved device) — e.g. `ams1-leaf-switch-1`.

3. **Check the family counter.** Drop a second device of the same family; it
   should get the next number (`…-2`), and two quick drops before saving get
   consecutive numbers rather than colliding.

4. **Confirm the fallback (optional).** Point `naming_script` at a name that
   does not exist and restart: dropping a device now yields the default
   `<design title>-<n>` instead of erroring — proof a bad config can't break the
   editor. Restore the correct path afterwards.

Nothing here writes to `dcim`: naming only ever runs on the read-only
name-preview request, never on page load or on **Save**.

### Keeping the script in NetBox's `SCRIPTS_ROOT`

Because `naming_script` is any importable dotted path, you can keep the script
where NetBox already manages Python: `SCRIPTS_ROOT` (default `<netbox>/scripts/`).
A file there is importable as `scripts.<module>`, so you reference it as
`scripts.<module>.build_name`. Two ways to get it there — both verified:

The shipped `netbox_rack_design/naming_example.py` uses **absolute imports**
precisely so it keeps working when copied out of the package. Start from it
rather than writing one from scratch.

**Set the config once.** Point `naming_script` at the module path and restart
NetBox — this is the only step that needs a restart (it edits
`configuration.py`). While the script isn't present yet, naming safely falls
back to the default `sequence` name.

```python
"naming_mode": "script",
"naming_script": "scripts.device_naming.build_name",
```

After that one-time config change, **adding or editing the script itself needs
no restart** — NetBox picks it up and the plugin imports `build_name` live on
the next name preview.

#### Variant 1 — through the UI (Customization → Scripts → Add)

Use this to view and edit the convention from the NetBox UI. NetBox lists a
module under Scripts only when it contains an `extras.scripts.Script` subclass,
so wrap the example with a tiny one (the plugin still calls the module-level
`build_name` directly — the `Script` class is only there for UI presence, and
`build_name` must stay read-only):

```python
from extras.scripts import Script

class DeviceNamingScript(Script):
    class Meta:
        name = "Rack Design device naming"
    def run(self, data, commit):
        self.log_info("Naming convention for the rack-design editor; nothing to run.")

# ... paste the helpers and build_name() from naming_example.py below ...
```

**Customization → Scripts → Add**, name the module `device_naming` (to match the
config path above), and paste the wrapped script. No restart — reopen a design
and the new names appear immediately. Edit it later the same way.

#### Variant 2 — copy the file into `SCRIPTS_ROOT`

If you don't need UI editing, just drop the file in — **no `Script` subclass
required**, because the plugin imports `build_name` directly:

```bash
cp .../netbox_rack_design/naming_example.py  $SCRIPTS_ROOT/device_naming.py
```

No restart needed; the plugin imports it on the next name preview.

> Referencing the package copy directly,
> `netbox_rack_design.naming_example.build_name`, also works and needs no copy
> at all — use that if you only want the defaults and no per-instance editing.
