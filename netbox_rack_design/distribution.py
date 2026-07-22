"""
Power-distribution engine for NetBox Rack Design.

The power projection (:mod:`netbox_rack_design.projection`) already computes each
planned device's draw and a per-rack total. This module is the next layer:
splitting that draw across the rack's **PDUs / feeds / banks** so the editor's
power heatmap can color the *banks* by load-vs-breaker, not just tint each device
by its share of the rack total.

Design: **universal base, native-first** (docs/pdu-distribution-spec.md §0). Two
site-agnostic conventions make a real distribution computable with zero config
and zero script: **bank** is the first segment of the outlet PORT name (e.g.
``1/1`` -> bank ``1``), and **feed-leg** is *the feed a PDU is bound to*
(:attr:`~netbox_rack_design.models.DesignPlacement.bound_feed` -- a real
``dcim.PowerFeed`` a PDU is cabled to, or a planned ``DesignPowerFeed`` it is
bound to). There is no device-name parsing in the base. Three modes, selected by
the plugin config key ``distribution_mode`` (read via ``get_plugin_config``):

``none`` (default)
    No distribution. :func:`generate_distribution` returns ``None`` and the
    frontend keeps the per-device rack-share heatmap.

``builtin``
    Compute the distribution natively (:func:`build_native`) using the two
    universal conventions above -- no config, no script. Degrades gracefully
    (never raises): an unresolvable PDU is logged and omitted rather than
    breaking the projection.

``script``
    Import the dotted path in config key ``distribution_script`` to a callable
    ``fn(rack, devices) -> Distribution`` and return its result. If the path is
    empty, unimportable, or not callable -- OR the script raises -- this logs a
    warning and **falls back to ``None``** (the ``none`` heatmap), so a
    mis-configured or buggy script degrades the overlay rather than breaking the
    editor. This mirrors the naming engine's ``script`` mode exactly
    (:mod:`netbox_rack_design.naming`). A script is only needed for *behaviour*
    that differs from the built-in (direction, ceilings, PSU schemes) -- never
    for feed *data*, which always comes from the binding.

The ``devices`` the script receives are the planned consumers built by
:func:`devices_from_elevation` -- the same planned world the projection already
computed (adds applied, removes dropped, moves at their target). Each entry
carries the normalized summary (name, role, status, U position, face, resolved
draw) *and* the underlying ``dcim.Device`` object, so a site script can also walk
real power cabling (``PowerPort`` -> ``PowerOutlet`` -> ``PowerFeed``) when it
needs the authoritative feed assignment.

The module is import-safe: no database access happens at import time, and it is
only ever reached from the read-only projection path (no ``dcim`` writes).
"""

import logging
import re

from django.utils.module_loading import import_string
from netbox.plugins import get_plugin_config

logger = logging.getLogger("netbox_rack_design.distribution")

__all__ = (
    "DEFAULT_DISTRIBUTION_MODE",
    "generate_distribution",
    "devices_from_elevation",
    "apply_rack_power_override",
    "feed_electricals",
    "breaker_watts",
    "build_native",
)

PLUGIN_NAME = "netbox_rack_design"

DEFAULT_DISTRIBUTION_MODE = "none"

# Slot states that carry a live, drawing device in the planned world (mirrors
# projection._DRAW_COUNTING_STATES). Only these become distribution consumers.
_CONSUMING_STATES = ("existing", "add", "move_in")

# --- build_native: the base distribution algorithm (docs/pdu-distribution- ---
# --- spec.md §0/§2) -- bank from the outlet port name, feed/leg from the ----
# --- binding. Shares its shape with the shipped reference script -----------
# --- (distribution_example.py), which uses device-name parsing instead. ----

# Roles that distribute power rather than consume it.
PDU_ROLE_SLUGS = frozenset(("pdu", "unmanageable-pdu"))
# Roles that never consume rack-bank power (parity with the internal calc).
SKIP_ROLE_SLUGS = frozenset((
    "cable-management", "patch-panel", "pdu", "unmanageable-pdu",
    "rack-mount-boxes", "rack-mount-kit",
))

# Utilization thresholds for a bank's state (percent of its breaker).
WARN_PCT = 80
CRITICAL_PCT = 100

# Bank/port on an outlet name: "<bank>/<port>", e.g. "1/1" -> bank 1 (§0.3.1).
_BANK_RE = re.compile(r"^(?P<bank>\d+)/\d+")
# A trailing feed-leg letter at a word boundary (start, whitespace, "-", "_"),
# optionally followed by digits, e.g. "Feed A" / "rack1-pdu-a1" -> "a". Used
# only as a cosmetic tie-break when a feed's own name already encodes its leg;
# the identity that actually GROUPS PDUs into legs is the feed name itself
# (see _assign_feed_legs), never this pattern.
_LEG_LETTER_SUFFIX_RE = re.compile(r"(?:^|[\s\-_])([A-Za-z])\d*$")


def feed_electricals(feed):
    """Normalize a feed (real ``dcim.PowerFeed`` or plugin ``DesignPowerFeed``)
    to the uniform electricals dict every distribution consumer reads (docs/
    pdu-distribution-spec.md, "the uniform feed contract")::

        {"voltage": int, "amperage": int, "phase": str, "supply": str, "name": str}

    ``phase``/``supply`` are read defensively (``getattr(x, "value", x)``)
    since a real ``dcim`` choice field may hand back a wrapped value object
    depending on access path, while ``DesignPowerFeed`` (a plain model) always
    hands back the raw string -- this makes both sources look identical.
    Returns ``None`` for a falsy/absent feed (an unbound PDU)."""
    if feed is None:
        return None
    phase = getattr(feed, "phase", None)
    supply = getattr(feed, "supply", None)
    return {
        "voltage": int(feed.voltage or 0),
        "amperage": int(feed.amperage or 0),
        "phase": getattr(phase, "value", phase),
        "supply": getattr(supply, "value", supply),
        "name": getattr(feed, "name", "") or "",
    }


def breaker_watts(feed):
    """The PDU input breaker in watts for ``feed`` -- a real ``PowerFeed``, a
    ``DesignPowerFeed``, or an already-normalized :func:`feed_electricals`
    dict: ``voltage x amperage x phase_rate`` (``phase_rate = 1.732`` for
    three-phase, else ``1``). Returns ``0`` for a falsy/unresolvable feed --
    never raises, so a bad feed just breakers a PDU at ``0W`` (visible as an
    immediate overload) rather than blowing up the projection."""
    electricals = feed if isinstance(feed, dict) else feed_electricals(feed)
    if not electricals:
        return 0
    phase_rate = 1.732 if electricals.get("phase") == "three-phase" else 1
    return round((electricals.get("voltage") or 0) * (electricals.get("amperage") or 0) * phase_rate)


def _slot_status(slot):
    """The planned device's status for a slot: the real device's status value
    (existing / move_in) or ``"planned"`` for an add (no real device yet)."""
    device = slot.get("device")
    if device is not None and getattr(device, "status", None):
        # dcim status is a ChoiceField; its ``value`` is the stored slug.
        return str(getattr(device.status, "value", device.status))
    return "planned"


def _slot_role_slug(slot):
    """The device's role slug for a slot (real device's role, else the add
    placement's chosen role). Lowercased; '' when unknown."""
    device = slot.get("device")
    if device is not None and getattr(device, "role_id", None):
        return (device.role.slug if device.role else "").lower()
    placement = slot.get("placement")
    if placement is not None and getattr(placement, "device_role_id", None):
        return (placement.device_role.slug if placement.device_role else "").lower()
    return ""


def _consumer_key(slot):
    """A stable identity for a slot so a full-depth device counted on both faces
    is charged once (same dedup as projection._project_power)."""
    device = slot.get("device")
    placement = slot.get("placement")
    if device is not None:
        return ("dev", device.pk)
    if placement is not None and placement.pk is not None:
        return ("pl", placement.pk)
    return ("id", id(slot))


def _placement_feed_info(placement):
    """``(feed_dict, source)`` from a placement's bound feed (docs/pdu-
    distribution-spec.md §6.2), or ``(None, None)`` when there is no placement
    or it is unbound. ``source`` is ``"real"``/``"planned"`` for which FK is
    set -- the uniform electricals dict never needs to branch on it, but the
    ``Distribution`` contract's ``feed_source`` does (§3)."""
    if placement is None:
        return None, None
    feed_obj = getattr(placement, "bound_feed", None)
    if feed_obj is None:
        return None, None
    source = "real" if placement.real_power_feed_id else "planned"
    return feed_electricals(feed_obj), source


def _placement_custom_fields(placement):
    """The planned PDU's custom fields, resolved source-agnostically (docs/pdu-
    distribution-spec.md §6): read LIVE off ``power_source_device.cf`` when the
    placement references a real PDU, else the manually-entered
    ``power_config["custom_fields"]``, else ``{}``. So a script sees a PDU's cf
    the same way whether it was referenced or typed -- and a referenced PDU's cf
    tracks edits to the source device (never snapshotted)."""
    if placement is None:
        return {}
    source_device = getattr(placement, "power_source_device", None)
    if source_device is not None:
        return dict(source_device.cf or {})
    config = getattr(placement, "power_config", None) or {}
    return dict(config.get("custom_fields") or {})


def devices_from_elevation(elevation):
    """Build the normalized planned-consumer list a distribution script receives.

    One entry per drawing device in the planned world (deduped across faces),
    decoupling the script from ``ProjectedElevation`` internals while still
    handing over the raw ``dcim.Device`` for scripts that walk real cabling.

    Each entry: ``{name, role, status, u_position, face, draw_w, draw_known,
    power_ports, device, device_type, power_config, feed, feed_source}``.
    ``device`` is the real ``dcim.Device`` for an existing/moved device, or
    ``None`` for a planned add. ``feed``/``feed_source`` are resolved from the
    placement's ``bound_feed`` (docs/pdu-distribution-spec.md §6.2) -- ``None``
    when the slot has no placement or the placement is unbound.
    """
    out = []
    seen = set()
    for face_slots in (elevation.front, elevation.rear, elevation.non_racked):
        for slot in face_slots:
            if slot.get("state") not in _CONSUMING_STATES:
                continue
            key = _consumer_key(slot)
            if key in seen:
                continue
            seen.add(key)
            placement = slot.get("placement")
            feed, feed_source = _placement_feed_info(placement)
            out.append({
                "name": slot.get("label") or "",
                "role": _slot_role_slug(slot),
                "status": _slot_status(slot),
                "u_position": slot.get("u_position"),
                "face": slot.get("face"),
                "draw_w": slot.get("draw_w", 0.0),
                "draw_known": slot.get("draw_known", False),
                "power_ports": slot.get("power_ports", []),
                "device": slot.get("device"),
                "device_type": slot.get("device_type"),
                # A planned PDU add's manual cf bridge (docs/pdu-distribution-
                # spec.md §6): {"custom_fields": {...}}. None for a real device or
                # a slot with no placement. Prefer the resolved ``custom_fields``
                # below -- this raw form is only for the dialog's manual reopen.
                "power_config": getattr(placement, "power_config", None) if placement else None,
                # Source-agnostic PDU cf (live from power_source_device, else
                # manual power_config) -- what a script should read (§6).
                "custom_fields": _placement_custom_fields(placement),
                # Resolved from placement.bound_feed (§6.2); None when unbound
                # or there is no placement (a plain existing/uninvolved device).
                "feed": feed,
                "feed_source": feed_source,
            })
    with_custom_fields = sum(1 for entry in out if entry.get("custom_fields"))
    with_feed = sum(1 for entry in out if entry.get("feed") is not None)
    logger.debug(
        "distribution.devices_from_elevation: rack=%r entries=%d with_custom_fields=%d with_feed=%d",
        getattr(elevation.rack, "name", None), len(out), with_custom_fields, with_feed,
    )
    return out


def apply_rack_power_override(elevation):
    """
    Merge a design's per-rack power custom-field override (``DesignRackPower``)
    over the in-memory rack's ``cf``, so the distribution script reads the
    effective planned values via ``rack.cf`` (docs/pdu-distribution-spec.md).

    Looks up ``DesignRackPower`` for ``(elevation.design, elevation.rack)``; when
    found and it carries a non-empty ``custom_fields`` dict, sets
    ``elevation.rack.__dict__["cf"] = {**rack.cf, **custom_fields}`` -- ``Rack.cf``
    is a ``cached_property``, so overriding the instance ``__dict__`` shadows it
    for the lifetime of this in-memory object only. Never persisted; never
    touches ``dcim``. A no-op when no override exists.
    """
    from .models import DesignRackPower

    rack = elevation.rack
    try:
        rack_power = DesignRackPower.objects.get(design=elevation.design, rack=rack)
    except DesignRackPower.DoesNotExist:
        logger.debug(
            "distribution.apply_rack_power_override: rack=%r design=%r no override found",
            getattr(rack, "name", None), getattr(elevation.design, "pk", None),
        )
        return

    config = rack_power.power_config or {}
    custom_fields = config.get("custom_fields") or {}
    if not custom_fields:
        logger.debug(
            "distribution.apply_rack_power_override: rack=%r design=%r override found but no custom_fields",
            getattr(rack, "name", None), getattr(elevation.design, "pk", None),
        )
        return

    merged = {**rack.cf, **custom_fields}
    rack.__dict__["cf"] = merged
    logger.debug(
        "distribution.apply_rack_power_override: rack=%r design=%r merged keys=%s",
        getattr(rack, "name", None), getattr(elevation.design, "pk", None),
        sorted(custom_fields.keys()),
    )


# --- build_native helpers ---------------------------------------------------


def _bank_of(outlet_name):
    """Parse the bank id (as ``int``) from an outlet name ``"<bank>/<port>"``
    (docs/pdu-distribution-spec.md §0.3.1)."""
    m = _BANK_RE.match(outlet_name or "")
    return int(m.group("bank")) if m else None


def _bank_ids_of(names):
    """Distinct, sorted bank ids parsed out of an iterable of outlet/template
    names (unparseable names are silently dropped, not raised on)."""
    return sorted({b for b in (_bank_of(n) for n in names) if b is not None})


def _real_pdu_cabled_feed(device):
    """The real ``dcim.PowerFeed`` a real PDU device's power port is cabled to
    (the native cable path), or ``None``. Used only when the PDU carries no
    binding of its own (docs/pdu-distribution-spec.md §1: "For a real PDU with
    no binding and a cabled PowerFeed, the native cable path supplies the same
    figure")."""
    for pp in device.powerports.all():
        for peer in (pp.link_peers or []):
            if peer.__class__.__name__ == "PowerFeed":
                return peer
    return None


def _cabled_bank_refs(device):
    """``[(pdu_name, bank_id), ...]`` for a real device's PowerPorts cabled to
    a PDU's PowerOutlet (docs/pdu-distribution-spec.md §2.2, "cabled" charge
    path) -- one ref per cabling, so a dual-corded device charges each PDU it
    is actually plugged into. Empty for a planned device (``device is None``)
    or one with no power cabling."""
    refs = []
    if device is None:
        return refs
    for pp in device.powerports.all():
        for peer in (pp.link_peers or []):
            if peer.__class__.__name__ == "PowerOutlet":
                pdu_device = peer.device
                bank_id = _bank_of(peer.name)
                if pdu_device is not None and bank_id is not None:
                    # Bank dicts are keyed by str(bank_id) (see _pdu_entry); the
                    # uncabled path already yields str keys, so normalize here too.
                    refs.append((pdu_device.name, str(bank_id)))
    return refs


def _pdu_entry(feed_dict, feed_source, bank_ids):
    """Build one PDU's topology dict (docs/pdu-distribution-spec.md §3): input
    breaker from the feed, split evenly across its banks."""
    allocated = breaker_watts(feed_dict)
    max_power = int(allocated / len(bank_ids)) if bank_ids else 0
    phase_num = 3 if feed_dict.get("phase") == "three-phase" else 1
    return {
        "feed_name": feed_dict.get("name") or "",
        "feed_letter": None,  # assigned by _assign_feed_legs once all PDUs are known
        "feed_source": feed_source,
        "phase": phase_num,
        "allocated_draw": allocated,
        "power_bank_count": len(bank_ids),
        "banks": {
            str(b): {
                "max_power": max_power,
                "allocated_power": 0,
                "planned_power": 0,
                "util_pct": 0.0,
                "state": "ok",
                "units": [],
                "devices": [],
            }
            for b in bank_ids
        },
    }


def _assign_feed_legs(pdus):
    """Assign each PDU's ``feed_letter`` from the identity of its bound feed
    (docs/pdu-distribution-spec.md §0.3.2): PDUs bound to the SAME feed name
    are the same leg. Distinct feed names are sorted and lettered a/b/c... in
    that order, UNLESS a name itself ends in a leg letter (e.g. "Feed A",
    "rack1-pdu-a1"), in which case that letter is used directly -- so a site
    that already encodes the leg in its feed name gets a stable letter across
    projections rather than an order-dependent one. Mutates ``pdus`` in place;
    robust to any number of distinct feeds (not just two)."""
    distinct_names = sorted({pdu["feed_name"] for pdu in pdus.values()})
    letters = {}
    for idx, name in enumerate(distinct_names):
        m = _LEG_LETTER_SUFFIX_RE.search(name or "")
        letters[name] = m.group(1).lower() if m else chr(ord("a") + idx)
    for pdu_name, pdu in pdus.items():
        pdu["feed_letter"] = letters[pdu["feed_name"]]
        logger.debug(
            "distribution.build_native: pdu=%r feed=%r -> leg %r",
            pdu_name, pdu["feed_name"], pdu["feed_letter"],
        )


def _collect_pdus(rack, devices):
    """Build the per-PDU topology dict (feed, phase, breaker, banks) for every
    resolvable PDU in the rack (docs/pdu-distribution-spec.md §2.1/§6):

    * real PDUs (``rack.devices.all()``) sized from their binding (a matching
      ``devices`` entry's resolved ``feed``) or, absent one, their cabled
      ``dcim.PowerFeed`` (the native path);
    * planned PDU adds (``devices`` entries with no ``dcim.Device`` yet) sized
      from their binding alone -- there is nothing in ``dcim`` to cable yet.

    A PDU whose feed or banks can't be resolved is logged (debug) and OMITTED
    rather than raising, so one bad/unbound PDU never breaks the rest (§0.3:
    "read-only overlay ... must never break the editor"). Returns
    ``{pdu_name: {...}}``, already leg-lettered via :func:`_assign_feed_legs`.
    """
    by_device_pk = {d["device"].pk: d for d in devices if d.get("device") is not None}
    pdus = {}

    for dev in rack.devices.all():
        try:
            role = (dev.role.slug if dev.role else "").lower()
            if role not in PDU_ROLE_SLUGS:
                continue
            entry = by_device_pk.get(dev.pk)
            feed_dict = entry.get("feed") if entry else None
            feed_source = entry.get("feed_source") if entry else None
            if feed_dict is None:
                cabled = _real_pdu_cabled_feed(dev)
                if cabled is not None:
                    feed_dict = feed_electricals(cabled)
                    feed_source = "real"
            if feed_dict is None:
                logger.debug(
                    "distribution.build_native: real PDU %r has no binding and "
                    "no cabled PowerFeed; omitted", dev.name,
                )
                continue
            bank_ids = _bank_ids_of(o.name for o in dev.poweroutlets.all())
            if not bank_ids:
                logger.debug(
                    "distribution.build_native: real PDU %r has no parseable "
                    "outlet banks; omitted", dev.name,
                )
                continue
            pdus[dev.name] = _pdu_entry(feed_dict, feed_source, bank_ids)
            logger.debug(
                "distribution.build_native: real PDU %r feed=%r (%s) banks=%d "
                "breaker=%dW", dev.name, feed_dict["name"], feed_source,
                len(bank_ids), pdus[dev.name]["allocated_draw"],
            )
        except Exception:  # noqa: BLE001 - one bad PDU must never break the rest
            logger.debug(
                "distribution.build_native: real PDU %r failed to resolve; omitted",
                getattr(dev, "name", None), exc_info=True,
            )

    for entry in devices:
        if entry.get("device") is not None:
            continue  # real device -- already covered above
        role = (entry.get("role") or "").lower()
        if role not in PDU_ROLE_SLUGS:
            continue
        name = entry.get("name") or ""
        try:
            feed_dict = entry.get("feed")
            feed_source = entry.get("feed_source")
            if feed_dict is None:
                logger.debug(
                    "distribution.build_native: planned PDU %r is unbound; omitted", name,
                )
                continue
            device_type = entry.get("device_type")
            if device_type is None:
                logger.debug(
                    "distribution.build_native: planned PDU %r has no device_type; "
                    "omitted", name,
                )
                continue
            bank_ids = _bank_ids_of(t.name for t in device_type.poweroutlettemplates.all())
            if not bank_ids:
                logger.debug(
                    "distribution.build_native: planned PDU %r has no parseable "
                    "outlet banks; omitted", name,
                )
                continue
            pdus[name] = _pdu_entry(feed_dict, feed_source, bank_ids)
            logger.debug(
                "distribution.build_native: planned PDU %r feed=%r (%s) banks=%d "
                "breaker=%dW", name, feed_dict["name"], feed_source,
                len(bank_ids), pdus[name]["allocated_draw"],
            )
        except Exception:  # noqa: BLE001 - one bad PDU must never break the rest
            logger.debug(
                "distribution.build_native: planned PDU %r failed to resolve; omitted",
                name, exc_info=True,
            )

    _assign_feed_legs(pdus)
    return pdus


def _unit_to_bank(rack, pdus):
    """Map each rack unit to a ``(pdu_name, bank_id)`` per feed leg (docs/pdu-
    distribution-spec.md §2.1).

    Splits the rack's units into contiguous, equal-sized slices -- one per
    bank on that leg, in bank order -- so an uncabled device can be attributed
    to a bank by its U position. ``pdu_location`` (``top``/``bottom``, read via
    the config-bridge ``rack.cf``) flips the direction so bank 1 sits where the
    PDU physically starts; absent, it defaults to ``bottom``. Returns
    ``{leg: {unit: (pdu_name, bank_id)}}``."""
    pdu_location = (rack.cf or {}).get("pdu_location") or "bottom"
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
            "distribution.build_native: leg=%r banks=%d units_per_bank=%d",
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


def _legs_for_native(device, unit_map):
    """The ``(pdu, bank)`` refs a device charges (docs/pdu-distribution-spec.md
    §2.2/§2.3):

    * **Cabled** (a real device's PowerPort -> PowerOutlet on a PDU): charge
      the outlet's bank directly, one ref per cabling -- full-per-leg
      redundancy falls out naturally (2 cablings -> 2 full charges).
    * **Uncabled** (planned or unconnected): attributed by U position via
      ``unit_map`` -- leg ``a`` only for a single PSU, ``a``+``b`` (never
      split) for 2+ PSUs. A leg absent from ``unit_map`` (fewer than 2 bound
      feeds in the rack) is silently skipped -- robust to any feed count.
    """
    cabled = _cabled_bank_refs(device.get("device"))
    if cabled:
        return cabled
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


def _charge_native(pdus, bank_ref, device, power_type):
    """Add ``device['draw_w']`` to a bank's allocated/planned bucket and record
    the device line (``{name, ru, draw_w, status, ports}`` -- docs/pdu-
    distribution-spec.md §3). ``bank_ref`` is ``(pdu_name, bank_id)``."""
    pdu_name, bank_id = bank_ref
    # A device may be cabled to a PDU/bank we could not resolve (e.g. a PDU with
    # no bound feed, omitted from the topology). Skip rather than KeyError.
    pdu = pdus.get(pdu_name)
    if pdu is None or bank_id not in pdu["banks"]:
        logger.debug(
            "distribution.build_native: device=%r cabled to unresolved bank %s/%s -- skipped",
            device.get("name", ""), pdu_name, bank_id,
        )
        return
    bank = pdu["banks"][bank_id]
    draw = float(device.get("draw_w") or 0)
    bank[power_type] += draw
    logger.debug(
        "distribution.build_native: device=%r charged %s bank %s/%s +%sW",
        device.get("name", ""), power_type, pdu_name, bank_id, draw,
    )
    bank["devices"].append({
        "name": device.get("name", ""),
        "ru": device.get("u_position"),
        "draw_w": draw,
        "status": device.get("status"),
        "ports": device.get("power_ports", []),
    })


def _power_limitation_w(rack):
    """Rack ceiling in watts from the ``power_limitation`` custom field (stored
    in kW as free text, read via the config-bridge ``rack.cf``), or ``None``."""
    raw = (rack.cf or {}).get("power_limitation")
    if raw in (None, ""):
        return None
    try:
        return float(raw) * 1000.0
    except (TypeError, ValueError):
        return None


def _finalize_native(pdus, power_limitation_w):
    """Compute per-bank util/state, roll up the rack total, and collect
    warnings. Returns the ``rack`` summary block (docs/pdu-distribution-
    spec.md §3): ``{power_limitation_w, power_consumption_w, alarm, warnings}``."""
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
                logger.debug("distribution.build_native: overload %s", msg)
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
        logger.debug("distribution.build_native: power_limitation breach %s", msg)
    return {
        "power_limitation_w": power_limitation_w,
        "power_consumption_w": rack_total,
        "alarm": alarm,
        "warnings": warnings,
    }


def build_native(rack, devices):
    """The base (announced) distribution builder -- docs/pdu-distribution-
    spec.md §0/§2: bank from the outlet port name, feed/leg from the binding.
    No config, no script required. Returns a ``Distribution`` dict (§3), or
    ``None`` when the rack has no resolvable PDUs (the heatmap then stays
    per-device). Never raises -- degrades by logging (debug) and omitting the
    unresolvable piece, same tolerance as the ``script`` mode fallback.
    """
    pdus = _collect_pdus(rack, devices)
    if not pdus:
        logger.debug(
            "distribution.build_native: rack=%r no resolvable PDUs",
            getattr(rack, "name", None),
        )
        return None

    unit_map = _unit_to_bank(rack, pdus)

    for device in devices:
        role = (device.get("role") or "").lower()
        if role in SKIP_ROLE_SLUGS:
            logger.debug(
                "distribution.build_native: device=%r skipped (skip-role %r)",
                device.get("name", ""), device.get("role"),
            )
            continue
        if not device.get("draw_known"):
            logger.debug(
                "distribution.build_native: device=%r skipped (unknown draw)",
                device.get("name", ""),
            )
            continue
        power_type = "planned_power" if device.get("status") == "planned" else "allocated_power"
        for bank_ref in _legs_for_native(device, unit_map):
            _charge_native(pdus, bank_ref, device, power_type)

    rack_summary = _finalize_native(pdus, _power_limitation_w(rack))
    logger.debug(
        "distribution.build_native: rack=%r pdus=%d alarm=%s",
        getattr(rack, "name", None), len(pdus), rack_summary["alarm"],
    )
    return {
        # No BANK_LIST_TO_PDU_SCHEMAS-style topology label ported yet (that
        # table lives only in the internal tooling, not this codebase) -- left
        # blank like the reference script; a site script may fill it in.
        "scheme": "",
        "pdu_location": (rack.cf or {}).get("pdu_location"),
        "pdus": pdus,
        "rack": rack_summary,
    }


def _run_script(rack, devices):
    """Resolve and invoke the configured ``distribution_script`` callable.

    Raises ``ValueError`` if the configured path is empty, unimportable, or not
    callable. Any exception the script itself raises propagates unchanged. The
    caller (:func:`generate_distribution`) turns these into a ``None`` fallback so
    a mis-configured or buggy script never breaks the projection.
    """
    path = get_plugin_config(PLUGIN_NAME, "distribution_script", "")
    if not path:
        raise ValueError(
            "distribution_mode is 'script' but no 'distribution_script' dotted "
            "path is configured."
        )
    try:
        fn = import_string(path)
    except ImportError as exc:
        raise ValueError(f"Could not import distribution_script '{path}': {exc}") from exc
    if not callable(fn):
        raise ValueError(f"distribution_script '{path}' is not callable.")
    return fn(rack, devices)


def generate_distribution(elevation, *, mode=None):
    """Compute the per-PDU/bank distribution for a projected rack, or ``None``.

    ``mode`` -- optional override for the configured ``distribution_mode`` (the
    editor/tests can force a mode without touching config).

    Returns the ``Distribution`` dict in ``builtin``/``script`` mode, or
    ``None`` in ``none`` mode (and as the graceful fallback in either of the
    other two). Never writes to ``dcim``.

    Robust to a broken engine: ``builtin`` never raises on its own (bad data
    degrades PDU-by-PDU inside :func:`build_native`), but an unexpected error
    is still caught here as a last resort; a broken ``script`` (unresolvable
    dotted path, not callable, or a raising callable) is always caught. Either
    failure logs a warning and **returns ``None``** -- the heatmap degrades to
    the per-device rack-share view rather than erroring the page.
    """
    if mode is None:
        mode = get_plugin_config(PLUGIN_NAME, "distribution_mode", DEFAULT_DISTRIBUTION_MODE)

    logger.debug("distribution.generate_distribution: rack=%r mode=%r", getattr(elevation.rack, "name", None), mode)

    if mode not in ("builtin", "script"):
        return None

    # Per-design rack power cf override (docs/pdu-distribution-spec.md): merge
    # DesignRackPower.power_config over the in-memory rack.cf right before the
    # engine runs, so it reads planned power_limitation/pdu_location. In-memory
    # only, never persisted; only reached in "builtin"/"script" mode so "none"
    # mode never queries DesignRackPower.
    apply_rack_power_override(elevation)
    devices = devices_from_elevation(elevation)
    planned_pdu_count = sum(
        1 for d in devices
        if d.get("device") is None and (d.get("role") or "") in ("pdu", "unmanageable-pdu")
    )
    consumer_count = len(devices) - planned_pdu_count
    logger.debug(
        "distribution.generate_distribution: rack=%r mode=%r consumer_entries=%d planned_pdu_entries=%d",
        getattr(elevation.rack, "name", None), mode, consumer_count, planned_pdu_count,
    )

    if mode == "builtin":
        try:
            result = build_native(elevation.rack, devices)
        except Exception:  # noqa: BLE001 - the built-in must never break the editor
            logger.warning(
                "distribution_mode 'builtin' raised unexpectedly for rack %r; "
                "falling back to no per-bank distribution (per-device heatmap).",
                getattr(elevation.rack, "name", None), exc_info=True,
            )
            result = None
        logger.debug(
            "distribution.generate_distribution: rack=%r builtin result=%s",
            getattr(elevation.rack, "name", None), "None (fallback)" if result is None else "dict",
        )
        return result

    # mode == "script"
    try:
        result = _run_script(elevation.rack, devices)
        logger.debug(
            "distribution.generate_distribution: rack=%r script result=%s",
            getattr(elevation.rack, "name", None), "None (fallback)" if result is None else "dict",
        )
        return result
    except Exception:  # noqa: BLE001 - any failure degrades to no distribution
        path = get_plugin_config(PLUGIN_NAME, "distribution_script", "")
        logger.warning(
            "distribution_script %r failed; falling back to no per-bank "
            "distribution (per-device heatmap). Fix the 'distribution_script' "
            "plugin config to restore per-bank distribution.",
            path, exc_info=True,
        )
        return None
