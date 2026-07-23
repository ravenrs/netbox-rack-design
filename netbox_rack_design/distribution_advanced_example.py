"""
A richer, worked example power-distribution script for ``distribution_mode =
"script"`` (see ``docs/pdu-distribution-spec.md``) -- the sibling of
``distribution_example.py``.

``distribution_example.py`` is deliberately minimal: it runs the shared §2
algorithm and leaves a few customization surfaces stubbed (``"scheme": ""``,
module-level threshold constants, and it never reads the ``planning_fields``
**``"pdu"``** role -- only ``"rack"``). This file is a genuinely runnable
public example that fills those surfaces in, so a site can see the *shape* of
each customization without inventing it from scratch:

1. **Topology scheme label** -- computes the ``Distribution["scheme"]`` string
   (spec Sec 2.1) from the sorted per-PDU bank-count signature, via a small
   lookup table (:data:`BANK_LIST_TO_PDU_SCHEMAS`), instead of leaving it
   blank.
2. **Per-PDU custom field via ``planning_fields["pdu"]``** -- reads an
   optional ``pdu_scheme`` planning field off each PDU entry (bridged through
   the config's ``"pdu"`` role, spec Sec 5/6.5) and, when a site has set one,
   lets it OVERRIDE the computed label for the whole rack. Demonstrates the
   ``"pdu"`` half of the config bridge the minimal example doesn't exercise.
3. **Config-driven thresholds** -- reads the bank WARN/CRITICAL utilization
   thresholds from plugin config (``power_warn_pct``/``power_critical_pct`` --
   the same keys the Tier-1 per-device heatmap already uses, see
   ``projection.py``) instead of hardcoding module constants.

It reuses the **same** core algorithm and helpers as the minimal example and
the built-in (``distribution.build_native``) -- ``distribution._collect_pdus``
verbatim, and ``distribution_example``'s ``_unit_to_bank``/``_legs_for``/
``_charge``/``_power_limitation_w``/``read_planning_field(s)`` -- so none of
the §2 algorithm is re-implemented here; only the three customization pieces
above are new.

Enable it in ``configuration.py``::

    PLUGINS_CONFIG = {
        "netbox_rack_design": {
            "distribution_mode": "script",
            "distribution_script":
                "netbox_rack_design.distribution_advanced_example.build",
            # Optional: override the bank health-bar thresholds (defaults
            # match the Tier-1 heatmap's power_warn_pct/power_critical_pct).
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
        },
    }

Contract: ``build(rack, devices) -> Distribution | None`` -- identical to
``distribution_example.build``. ``rack`` is the real ``dcim.Rack``; ``devices``
is the normalized planned-consumer list from
:func:`netbox_rack_design.distribution.devices_from_elevation` (PDU entries
among them carry a resolved ``custom_fields`` dict and/or the raw ``device``,
per docs/pdu-distribution-spec.md Sec 6.5). Returns the ``Distribution`` dict
(Sec 3), or ``None`` when the rack has no PDUs to distribute across. Strictly
**read-only** -- it never writes to ``dcim``.
"""

import logging

# ABSOLUTE imports so this file keeps working verbatim if COPIED out of the
# package into NetBox's SCRIPTS_ROOT (a relative import would break there).
from netbox.plugins import get_plugin_config

from netbox_rack_design.distribution import (
    PDU_ROLE_SLUGS,
    _collect_pdus,
)
from netbox_rack_design.distribution_example import (
    SKIP_ROLE_SLUGS,
    _charge,
    _legs_for,
    _power_limitation_w,
    _unit_to_bank,
    read_planning_fields,
)

# Plain getLogger(__name__) (not the plugin's dotted logger) so this file works
# unchanged whether it lives in the package or a copied-out SCRIPTS_ROOT script.
logger = logging.getLogger(__name__)

PLUGIN_NAME = "netbox_rack_design"

# Default bank utilization thresholds, used only when the site hasn't set the
# power_warn_pct/power_critical_pct plugin config keys (spec Sec 3, "state"
# uses the existing thresholds" -- same keys the Tier-1 heatmap reads in
# projection.py's _power_config()).
DEFAULT_WARN_PCT = 80
DEFAULT_CRITICAL_PCT = 100

# --- topology scheme label (spec Sec 2.1's validation-aid table) -----------

# The sorted, "_"-joined multiset of per-PDU bank counts -> a human topology
# label. Copied from docs/pdu-distribution-spec.md Sec 2.1's
# ``BANK_LIST_TO_PDU_SCHEMAS``; extend with any additional PDU shapes a site
# uses. An unrecognized signature is not an error -- see _scheme_label's
# fallback below.
BANK_LIST_TO_PDU_SCHEMAS = {
    "1": "1x1PH1Bank",
    "1_1": "2x1PH1Bank",
    "2": "1x1PH2Banks",
    "2_2": "2x1PH2Banks",
    "2_2_2": "3x1PH2Banks",
    "2_2_2_2": "4x1PH2Banks",
    "3": "1x3PH3Banks",
    "3_3": "2x3PH3Banks",
    "3_3_3": "3x3PH3Banks",
    "6": "1x3PH6Banks",
    "6_6": "2x3PH6Banks",
}


def _scheme_label(pdus):
    """Compute the ``Distribution["scheme"]`` topology label (spec Sec 2.1)
    from the sorted per-PDU bank-count signature -- e.g. two 2-bank PDUs give
    signature ``"2_2"`` -> ``"2x1PH2Banks"``. An unrecognized signature (a PDU
    shape not in :data:`BANK_LIST_TO_PDU_SCHEMAS`) falls back to the raw
    signature string rather than raising -- a script must degrade the label,
    never break the heatmap (spec Sec 0.3)."""
    counts = sorted(len(pdu["banks"]) for pdu in pdus.values())
    signature = "_".join(str(c) for c in counts)
    label = BANK_LIST_TO_PDU_SCHEMAS.get(signature)
    if label is None:
        logger.debug(
            "distribution_advanced_example._scheme_label: unrecognized bank "
            "signature %r; falling back to raw signature", signature,
        )
        return signature
    return label


# --- per-PDU custom field via planning_fields["pdu"] ------------------------


class _PduCfView:
    """Tiny adapter exposing a ``devices`` PDU entry's custom fields as
    ``.cf``, so the shared ``read_planning_field``/``read_planning_fields``
    resolver (the ``cf.<name>`` grammar, spec Sec 5) works uniformly whether
    the entry's custom fields came from the resolved planned-PDU bridge
    (``entry["custom_fields"]``, spec Sec 6.5.3) or -- for a real, untouched
    PDU with no design placement -- straight off the raw ``dcim.Device``
    (``entry["device"].cf``)."""

    def __init__(self, entry):
        cf = entry.get("custom_fields") or {}
        if not cf:
            device = entry.get("device")
            cf = dict(getattr(device, "cf", None) or {}) if device is not None else {}
        self.cf = cf


def _pdu_scheme_override(devices):
    """The first ``pdu_scheme`` planning field found among the rack's PDU
    entries (``planning_fields["pdu"]``, spec Sec 5/6.5) -- demonstrates the
    ``"pdu"`` role of the config bridge (the minimal example only reads
    ``"rack"``). Returns ``None`` when no PDU carries one (the shipped
    default, ``planning_fields = {}``, falls back cleanly to the computed
    :func:`_scheme_label`)."""
    for entry in devices:
        role = (entry.get("role") or "").lower()
        if role not in PDU_ROLE_SLUGS:
            continue
        value = read_planning_fields("pdu", _PduCfView(entry)).get("pdu_scheme")
        if value:
            logger.debug(
                "distribution_advanced_example._pdu_scheme_override: pdu=%r "
                "override=%r", entry.get("name"), value,
            )
            return value
    return None


# --- config-driven thresholds ------------------------------------------------


def _thresholds():
    """Bank WARN/CRITICAL utilization thresholds from plugin config (the same
    ``power_warn_pct``/``power_critical_pct`` keys the Tier-1 per-device
    heatmap reads, ``projection.py``'s ``_power_config()``) instead of the
    module-level constants the minimal example hardcodes."""
    warn_pct = get_plugin_config(PLUGIN_NAME, "power_warn_pct", DEFAULT_WARN_PCT)
    critical_pct = get_plugin_config(
        PLUGIN_NAME, "power_critical_pct", DEFAULT_CRITICAL_PCT)
    return warn_pct, critical_pct


def _finalize(pdus, power_limitation_w, warn_pct, critical_pct):
    """Compute per-bank util/state, roll up the rack total, and collect
    warnings -- same shape as ``distribution_example._finalize``, but the
    WARN/CRITICAL thresholds are parameters (read from plugin config by the
    caller) rather than module constants."""
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
                logger.debug("distribution_advanced_example._finalize: overload %s", msg)
            elif bank["util_pct"] >= critical_pct:
                bank["state"] = "critical"
            elif bank["util_pct"] >= warn_pct:
                bank["state"] = "warn"
            else:
                bank["state"] = "ok"
    if power_limitation_w and rack_total > power_limitation_w:
        alarm = True
        msg = (
            f"Rack total {int(rack_total)}W exceeds power limitation "
            f"{int(power_limitation_w)}W")
        warnings.append(msg)
        logger.debug(
            "distribution_advanced_example._finalize: power_limitation breach %s", msg)
    return {
        "power_limitation_w": power_limitation_w,
        "power_consumption_w": rack_total,
        "alarm": alarm,
        "warnings": warnings,
    }


def build(rack, devices):
    """Distribute the planned devices' draw across the rack's PDUs/feeds/banks
    (same §2 algorithm and shared helpers as ``distribution_example.build``),
    then layer in this file's three customizations: a computed/overridden
    ``scheme`` label and config-driven WARN/CRITICAL thresholds. Returns a
    ``Distribution`` dict (docs/pdu-distribution-spec.md Sec 3), or ``None``
    when the rack has no resolvable PDUs (heatmap stays per-device)."""
    pdus = _collect_pdus(rack, devices)
    if not pdus:
        logger.debug(
            "distribution_advanced_example.build: rack=%r no resolvable PDUs",
            getattr(rack, "name", None),
        )
        return None

    pdu_location = read_planning_fields("rack", rack).get("pdu_location") or "bottom"
    unit_map = _unit_to_bank(rack, pdus, pdu_location)

    for device in devices:
        role = (device.get("role") or "").lower()
        if role in SKIP_ROLE_SLUGS:
            logger.debug(
                "distribution_advanced_example.build: device=%r skipped "
                "(skip-role %r)", device.get("name", ""), device.get("role"),
            )
            continue
        if not device.get("draw_known"):
            logger.debug(
                "distribution_advanced_example.build: device=%r skipped "
                "(unknown draw)", device.get("name", ""),
            )
            continue
        power_type = "planned_power" if device.get("status") == "planned" else "allocated_power"
        for bank_ref in _legs_for(device, unit_map):
            _charge(pdus, bank_ref, device, power_type)

    warn_pct, critical_pct = _thresholds()
    rack_summary = _finalize(pdus, _power_limitation_w(rack), warn_pct, critical_pct)
    scheme = _pdu_scheme_override(devices) or _scheme_label(pdus)
    logger.debug(
        "distribution_advanced_example.build: rack=%r pdus=%d scheme=%r "
        "warn_pct=%s critical_pct=%s alarm=%s",
        getattr(rack, "name", None), len(pdus), scheme, warn_pct, critical_pct,
        rack_summary["alarm"],
    )
    return {
        "scheme": scheme,
        "pdu_location": pdu_location,
        "pdus": pdus,
        "rack": rack_summary,
    }
