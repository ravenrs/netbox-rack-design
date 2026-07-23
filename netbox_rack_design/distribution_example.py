"""
A ready-to-adapt example power-distribution script for
``distribution_mode = "script"`` (see ``docs/pdu-distribution-spec.md``).

It runs the **same** algorithm as the built-in (``distribution_mode =
"builtin"`` -- :func:`netbox_rack_design.distribution.build_native`), over the
**same** shared helpers, under the **same** two universal conventions (spec
Sec 0.3): bank = the first segment of the outlet PORT name (``"<bank>/<port>"``),
feed-leg = the feed a PDU is *bound to* (a real ``dcim.PowerFeed`` or a planned
``DesignPowerFeed`` -- never PDU-name parsing). Because that means it needs no
site-specific naming at all, it is a genuinely working reference: enabling it
reproduces the built-in distribution verbatim, and it exists as a **copyable
template** for the piece a site actually customizes -- its *behaviour*
(direction, ceilings, PSU schemes), read through the ``planning_fields``
config bridge (spec Sec 5) rather than hardcoded custom-field names.

Enable it in ``configuration.py``::

    PLUGINS_CONFIG = {
        "netbox_rack_design": {
            "distribution_mode": "script",
            "distribution_script": "netbox_rack_design.distribution_example.build",
            # Only needed if your rack cf uses different names than the
            # generic "power_limitation" / "pdu_location" keys below:
            "planning_fields": {
                "rack": [
                    {"key": "power_limitation", "label": "Power limitation (W)",
                     "type": "number", "source": "cf.power_limitation"},
                    {"key": "pdu_location", "label": "PDU location", "type": "choice",
                     "choices": ["top", "bottom"], "source": "cf.pdu_location"},
                ],
            },
        },
    }

Contract: ``build(rack, devices) -> Distribution | None``. ``rack`` is the real
``dcim.Rack``; ``devices`` is the normalized planned-consumer list from
:func:`netbox_rack_design.distribution.devices_from_elevation` (each entry also
carries the raw ``dcim.Device`` for scripts that walk real cabling, and the
resolved ``feed``/``feed_source`` from the placement's binding). Return the
``Distribution`` dict (``docs/pdu-distribution-spec.md`` Sec 3), or ``None`` when
the rack has no PDUs to distribute across (the heatmap then stays per-device).
It is strictly **read-only** -- it never writes to ``dcim``.

Custom fields (``power_limitation``, ``pdu_location``, or any future site
field) are read **generically** via the ``planning_fields`` plugin config
(spec Sec 5) through :func:`read_planning_fields` / :func:`read_planning_field`
-- this file never hardcodes a site's actual cf name, only the *key* the
algorithm below expects (``power_limitation``, ``pdu_location``). A site whose
rack cf is named e.g. ``rack_power_cap`` maps it via config; a site using the
generic names needs no ``planning_fields`` config at all (the shipped default,
``{}``, falls back cleanly: ``pdu_location`` defaults to ``"bottom"``,
``power_limitation`` is absent -- no rack cap).
"""

import logging

# ABSOLUTE imports so this file keeps working verbatim if COPIED out of the
# package into NetBox's SCRIPTS_ROOT (a relative import would break there).
# ``netbox_rack_design.distribution`` stays importable regardless of where THIS
# file lives, because it's the installed plugin package, not a sibling script.
from netbox.plugins import get_plugin_config

from netbox_rack_design.distribution import (
    _bank_of,
    _collect_pdus,
)

# Plain getLogger(__name__) (not the plugin's dotted logger) so this file works
# unchanged whether it lives in the package or a copied-out SCRIPTS_ROOT script.
logger = logging.getLogger(__name__)

PLUGIN_NAME = "netbox_rack_design"

# Roles that never consume rack-bank power (parity with the internal calc and
# with distribution.build_native's SKIP_ROLE_SLUGS).
SKIP_ROLE_SLUGS = frozenset((
    "cable-management", "patch-panel", "pdu", "unmanageable-pdu",
    "rack-mount-boxes", "rack-mount-kit",
))

# Utilization thresholds for a bank's state (percent of its breaker). Adjust to
# taste; a copied-out script can read them from plugin config instead.
WARN_PCT = 80
CRITICAL_PCT = 100


# --- config-bridge: read custom fields generically, never by hardcoded name -


def read_planning_field(config, source, obj):
    """Resolve one ``planning_fields`` entry's ``source`` against ``obj``
    (docs/pdu-distribution-spec.md Sec 5's token grammar).

    * ``cf.<name>`` reads a custom field off ``obj.cf`` (the value dict every
      ``dcim``/plugin object with custom fields exposes) -- the only grammar a
      distribution site needs.
    * Any other dotted path (e.g. ``role.name``) walks native attributes off
      ``obj`` instead, for a copy-from-rack style source elsewhere in the
      planning dialogs.

    ``config`` is the field's own schema dict (``{"key", "label", "type",
    "source"[, "choices"]}``) -- unused by the resolver itself, but threaded
    through so a caller can post-process the raw value by its declared
    ``type`` (e.g. cast ``"number"`` to ``float``). Returns ``None`` for a
    missing/unset value or an unresolvable path; never raises.
    """
    if not source:
        return None
    parts = source.split(".")
    if parts[0] == "cf":
        if len(parts) != 2:
            return None
        cf = getattr(obj, "cf", None) or {}
        return cf.get(parts[1])
    value = obj
    for part in parts:
        if value is None:
            return None
        value = getattr(value, part, None)
    return value


def read_planning_fields(role, obj):
    """Read every configured ``planning_fields[role]`` entry off ``obj``,
    returning ``{key: value}``. This is how this script sees a site's custom
    fields WITHOUT hardcoding their names (docs/pdu-distribution-spec.md Sec
    5) -- e.g. a site whose rack cf is called ``rack_power_cap`` maps it to the
    ``power_limitation`` key the algorithm below reads. An empty/missing
    schema (the shipped default) returns ``{}`` for every ``role``, so this
    script still runs -- just without any cf-derived override."""
    schema = get_plugin_config(PLUGIN_NAME, "planning_fields", {}) or {}
    fields = schema.get(role) or []
    out = {}
    for field in fields:
        key = field.get("key")
        source = field.get("source")
        if not key or not source:
            continue
        out[key] = read_planning_field(field, source, obj)
    return out


# --- the algorithm (shared shape with distribution.build_native) -----------


def _unit_to_bank(rack, pdus, pdu_location):
    """Map each rack unit to a ``(pdu_name, bank_id)`` per feed leg -- same
    contiguous-slice algorithm as ``distribution.build_native``'s
    ``_unit_to_bank``, except ``pdu_location`` is passed in already resolved
    through the ``planning_fields`` bridge (this is the "direction" behaviour
    piece a site script may override, docs/pdu-distribution-spec.md Sec 2.1).
    """
    units = list(range(1, (rack.u_height or 0) + 1))
    if pdu_location == "top":
        units.reverse()

    result = {}
    legs = {}
    for name, pdu in pdus.items():
        legs.setdefault(pdu["feed_letter"], []).append(name)
    for leg, names in legs.items():
        ordered_banks = []  # [(pdu_name, bank_id_str), ...] in physical order
        for name in sorted(names):
            for bank_id in sorted(pdus[name]["banks"], key=int):
                ordered_banks.append((name, bank_id))
        if not ordered_banks:
            continue
        per_bank = max(1, round(len(units) / len(ordered_banks)))
        logger.debug(
            "distribution_example._unit_to_bank: leg=%r banks=%d units_per_bank=%d",
            leg, len(ordered_banks), per_bank,
        )
        leg_map = {}
        for idx, (pdu_name, bank_id) in enumerate(ordered_banks):
            start = idx * per_bank
            # Last bank absorbs the remainder so every unit is owned.
            end = len(units) if idx == len(ordered_banks) - 1 else start + per_bank
            for unit in units[start:end]:
                leg_map[unit] = (pdu_name, bank_id)
                pdus[pdu_name]["banks"][bank_id]["units"].append(unit)
        result[leg] = leg_map
    return result


def _legs_for(device, unit_map):
    """The ``(pdu, bank)`` refs a device charges, one per redundant leg.

    * **Cabled** (a real device's PowerPort -> PowerOutlet on a PDU): charge
      the outlet's bank directly, one ref per cabling.
    * **Uncabled** (planned/unconnected): a device with 2+ PSUs is redundant
      -> charged in FULL to each leg it maps to (worst-case failover); a
      single-PSU device sits on ONE leg only (never split), attributed by U
      position via ``unit_map``.
    """
    device_obj = device.get("device")
    if device_obj is not None:
        refs = []
        for pp in device_obj.powerports.all():
            for peer in (pp.link_peers or []):
                if peer.__class__.__name__ == "PowerOutlet":
                    pdu_device = peer.device
                    bank_id = _bank_of(peer.name)
                    if pdu_device is not None and bank_id is not None:
                        # banks are keyed by str(bank_id) (_collect_pdus).
                        refs.append((pdu_device.name, str(bank_id)))
        if refs:
            return refs
    unit = device.get("u_position")
    try:
        unit = int(unit)
    except (TypeError, ValueError):
        return []
    psu_count = len(device.get("power_ports") or [])
    legs = ["a", "b"] if psu_count >= 2 else ["a"]
    refs = []
    for leg in legs:
        ref = unit_map.get(leg, {}).get(unit)
        if ref is not None:
            refs.append(ref)
    return refs


def _charge(pdus, bank_ref, device, power_type):
    """Add ``device['draw_w']`` to a bank's allocated/planned bucket and record
    the device line. ``bank_ref`` is ``(pdu_name, bank_id)``."""
    pdu_name, bank_id = bank_ref
    bank = pdus[pdu_name]["banks"][bank_id]
    draw = float(device.get("draw_w") or 0)
    bank[power_type] += draw
    logger.debug(
        "distribution_example._charge: device=%r charged %s bank %s/%s +%sW",
        device.get("name", ""), power_type, pdu_name, bank_id, draw,
    )
    bank["devices"].append({
        "name": device.get("name", ""),
        "ru": device.get("u_position"),
        "draw_w": draw,
        "status": device.get("status"),
        "ports": device.get("power_ports", []),
    })


def _finalize(pdus, power_limitation_w):
    """Compute per-bank util/state, roll up the rack total, and collect
    warnings. Returns the ``rack`` summary block (docs/pdu-distribution-
    spec.md Sec 3) -- the "ceilings" behaviour piece."""
    warnings = []
    alarm = False
    rack_total = 0.0
    for pdu_name, pdu in pdus.items():
        for bank_id, bank in pdu["banks"].items():
            load = bank["allocated_power"] + bank["planned_power"]
            rack_total += load
            breaker = bank["max_power"] or 0
            bank["util_pct"] = (load / breaker * 100.0) if breaker else 0.0
            if breaker and bank["allocated_power"] > breaker:
                bank["state"] = "overload"
                alarm = True
                msg = (
                    f"PDU {pdu_name} bank {bank_id}: {int(bank['allocated_power'])}W "
                    f"exceeds breaker {breaker}W")
                warnings.append(msg)
                logger.debug("distribution_example._finalize: overload %s", msg)
            elif bank["util_pct"] >= CRITICAL_PCT:
                bank["state"] = "critical"
            elif bank["util_pct"] >= WARN_PCT:
                bank["state"] = "warn"
            else:
                bank["state"] = "ok"
    if power_limitation_w and rack_total > power_limitation_w:
        alarm = True
        msg = (
            f"Rack total {int(rack_total)}W exceeds power limitation "
            f"{int(power_limitation_w)}W")
        warnings.append(msg)
        logger.debug("distribution_example._finalize: power_limitation breach %s", msg)
    return {
        "power_limitation_w": power_limitation_w,
        "power_consumption_w": rack_total,
        "alarm": alarm,
        "warnings": warnings,
    }


def _power_limitation_w(rack):
    """Rack ceiling in watts from the ``power_limitation`` planning-field key
    (mapped through ``planning_fields`` -- stored in kW as free text, per the
    same convention as the built-in), or ``None``."""
    raw = read_planning_fields("rack", rack).get("power_limitation")
    if raw in (None, ""):
        return None
    try:
        return float(raw) * 1000.0
    except (TypeError, ValueError):
        return None


def build(rack, devices):
    """Distribute the planned devices' draw across the rack's PDUs/feeds/banks.

    Runs the same algorithm as ``distribution.build_native`` (bank from the
    outlet port name, feed/leg from the binding -- ``distribution._collect_pdus``
    is reused verbatim) with the ``pdu_location``/``power_limitation`` behaviour
    read via ``planning_fields`` instead of a hardcoded cf name. Returns a
    ``Distribution`` dict (``docs/pdu-distribution-spec.md`` Sec 3), or ``None``
    when the rack has no resolvable PDUs (heatmap stays per-device).
    """
    pdus = _collect_pdus(rack, devices)
    if not pdus:
        logger.debug("distribution_example.build: rack=%r no resolvable PDUs", getattr(rack, "name", None))
        return None

    pdu_location = read_planning_fields("rack", rack).get("pdu_location") or "bottom"
    unit_map = _unit_to_bank(rack, pdus, pdu_location)

    for device in devices:
        role = (device.get("role") or "").lower()
        if role in SKIP_ROLE_SLUGS:
            logger.debug(
                "distribution_example.build: device=%r skipped (skip-role %r)",
                device.get("name", ""), device.get("role"),
            )
            continue
        if not device.get("draw_known"):
            # Unknown draw stays uncharged (frontend shows the neutral hatch);
            # a 0-known passive device charges nothing anyway.
            logger.debug(
                "distribution_example.build: device=%r skipped (unknown draw)",
                device.get("name", ""),
            )
            continue
        power_type = "planned_power" if device.get("status") == "planned" else "allocated_power"
        for bank_ref in _legs_for(device, unit_map):
            _charge(pdus, bank_ref, device, power_type)

    rack_summary = _finalize(pdus, _power_limitation_w(rack))
    logger.debug(
        "distribution_example.build: rack=%r pdus=%d alarm=%s",
        getattr(rack, "name", None), len(pdus), rack_summary["alarm"],
    )
    return {
        "scheme": "",  # a site script may label the topology here
        "pdu_location": pdu_location,
        "pdus": pdus,
        "rack": rack_summary,
    }
