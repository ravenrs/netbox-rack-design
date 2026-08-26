"""
Projected rack elevation service for NetBox Rack Design.

This module computes what a single rack *would look like* if a given
:class:`~netbox_rack_design.models.Design` were applied, **without mutating any
real NetBox data**. It is the read-only counterpart to applying a design: the
output is a structured, template-agnostic description of the projected rack that
the elevation template (and any future API/GridStack consumer) can render.

The projection starts from the rack's *real* installed devices (via NetBox's own
:meth:`Rack.get_rack_units`) and then layers the design's placements
(``DesignPlacement`` rows whose ``target_rack`` -- or, for moves/removes, whose
``device.rack`` -- is this rack) on top:

* ``add``    -> a virtual planned slot at ``(target_position, target_face)`` for
               the placement's ``device_type``.
* ``move``   -> the moved device is shown at its *target* U/face (``move_in``),
               and a "ghost" slot is left at its *original* U/face
               (``move_out_ghost``) to show what is being vacated.
* ``remove`` -> the device's existing slot is kept visible but flagged
               (``remove``).

Anything whose target has no position (a position-less ``add``/``move``) is
returned in a separate ``non_racked`` list rather than dropped. ``non_racked``
also includes real DCIM devices that ARE associated with this rack but have no
``position`` (``Device.rack == rack and Device.position is None`` -- 0U
accessories such as vertical PDUs, rear-door units, cable managers): these are
the tray's "reality" layer (spec §9.1), rendered as ``existing`` slots exactly
like a racked existing device, just without a U/face.

------------------------------------------------------------------------------
RESULT CONTRACT  (this is the shape the template / API consumes)
------------------------------------------------------------------------------

``project_rack(design, rack)`` returns a :class:`ProjectedElevation`
dataclass with these attributes:

    design        -- the Design that was projected (passthrough).
    rack          -- the Rack that was projected (passthrough).
    front         -- list[dict]  projected slots on the front face (see below).
    rear          -- list[dict]  projected slots on the rear face.
    non_racked    -- list[dict]  slots for placements with no target_position.

Each face list is ordered top-of-rack first (matching ``Rack.get_rack_units``).
Empty rack units are NOT included as slots -- only occupied/planned units appear,
so the template can lay them out by ``u_position`` over an empty grid.

Each *slot* is a plain ``dict`` with the following keys (stable contract):

    u_position    Decimal | None   The unit number of the slot's bottom-most U.
                                    None only for ``non_racked`` slots.
    u_height      Decimal          Height in rack units (>= 1; 1 if unknown).
    face          str              dcim face value: 'front' or 'rear'
                                   (DeviceFaceChoices). Empty string for
                                   full-depth/unknown on non_racked entries.
    label         str              Human label for the slot (device name,
                                   proposed_name, or device_type model).
    state         str              One of ProjectedSlotState:
                                     'existing'       real device, unchanged.
                                     'add'            new planned device.
                                     'move_in'        device at its new spot.
                                     'move_out_ghost' vacated original spot.
                                     'remove'         existing device flagged
                                                      for removal.
    device        dcim.Device | None       The real device, if any.
    device_type   dcim.DeviceType | None   The catalog type (always set for
                                            'add'; otherwise the device's type).
    placement     DesignPlacement | None   The placement that produced this
                                            slot. None for plain 'existing'
                                            slots not touched by the design.

    displaced     bool             True on a vacating slot (move_out_ghost/
                                   remove) whose rows are occupied by a live
                                   planned slot (add/move_in) on the same
                                   face (spec §3/§4.3). Renderers show such a
                                   slot as the outside stripe bar, never a
                                   full tile under the occupant.
    displaced_by  str | None       The occupant's label when displaced.

Slots whose ``state`` is ``existing`` come straight from the real rack and were
not referenced by any placement. Slots touched by the design carry their
originating ``placement``.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from dcim.choices import DeviceFaceChoices, SubdeviceRoleChoices
from netbox.plugins import get_plugin_config

from .choices import DesignPlacementKindChoices

__all__ = (
    "ProjectedSlotState",
    "ProjectedElevation",
    "project_rack",
    "device_type_power_summary",
)

PLUGIN_NAME = "netbox_rack_design"

# Power projection defaults (docs/power-projection-spec.md §2 Tier 1). Overridable
# via PLUGINS_CONFIG keys: power_capacity_default_w, power_draw_basis,
# power_warn_pct, power_critical_pct.
DEFAULT_POWER_CAPACITY_W = 1000
DEFAULT_POWER_BASIS = "allocated"
DEFAULT_POWER_WARN_PCT = 80
DEFAULT_POWER_CRITICAL_PCT = 100
# Roles treated as power INFRASTRUCTURE, not consumers: a PDU distributes power
# to the devices plugged into it, so counting its input draw would double-count
# those devices. Excluded from the consumption sum (config key
# power_exclude_roles). Matched case-insensitively against the device role slug.
DEFAULT_POWER_EXCLUDE_ROLES = ("pdu", "unmanageable-pdu")


class ProjectedSlotState:
    """The lifecycle state of a projected slot (see module docstring)."""

    EXISTING = "existing"
    ADD = "add"
    MOVE_IN = "move_in"
    MOVE_OUT_GHOST = "move_out_ghost"
    REMOVE = "remove"


@dataclass
class ProjectedElevation:
    """Structured, template-agnostic result of projecting one design onto one rack."""

    design: object
    rack: object
    front: list = field(default_factory=list)
    rear: list = field(default_factory=list)
    non_racked: list = field(default_factory=list)
    # Power projection (Tier 1, crude/zero-config) -- see
    # docs/power-projection-spec.md. Rack-level summary populated by
    # _project_power() at the end of project_rack(); each counting slot also
    # gets per-slot ``draw_w``/``draw_known`` for the heatmap. Keys:
    # draw_w, capacity_w, util_pct, state, unconnected_count, unconnected_devices,
    # unknown_draw_count, unknown_devices, basis, warn_pct, critical_pct.
    power: dict = field(default_factory=dict)


# The two things EVERY container's projection derives the same way, whatever its
# slots are (spec §2). They were copied verbatim into the rack overlay and the
# chassis one; a state or label rule fixed in one silently left the other wrong.
_PLACEMENT_STATE = {
    DesignPlacementKindChoices.KIND_ADD: ProjectedSlotState.ADD,
    DesignPlacementKindChoices.KIND_MOVE: ProjectedSlotState.MOVE_IN,
    DesignPlacementKindChoices.KIND_REMOVE: ProjectedSlotState.REMOVE,
}


def _placement_state(placement):
    """The §3 slot state a placement projects to."""
    return _PLACEMENT_STATE.get(placement.kind, ProjectedSlotState.ADD)


def _placement_label(placement, device_type=None):
    """What a placement is CALLED on a tile.

    The editor's chosen name wins; then the real device's; then the catalog
    model for a planned add that has no name yet. "?" only when a placement
    names nothing at all, which the model should already prevent.
    """
    if device_type is None:
        device_type = placement.device_type or (
            placement.device.device_type if placement.device_id else None
        )
    if placement.proposed_name:
        return placement.proposed_name
    if placement.device_id:
        return placement.device.name or str(placement.device)
    return device_type.model if device_type else "?"


def _slot(
    *,
    u_position,
    u_height,
    face,
    label,
    state,
    device=None,
    device_type=None,
    placement=None,
    opposite_face=False,
    display_label=None,
):
    """Build a single projected-slot dict following the documented contract."""
    return {
        "u_position": u_position,
        "u_height": u_height,
        "face": face,
        "label": label,
        # Tile label = ASSIGNED name (user ruling 2026-07-10): the VISIBLE
        # name. A renamed move shows its proposed_name here while ``label``
        # stays the stable IDENTITY string (the device's real name) that
        # anchors ghost pairing, harnesses, and the read-model.
        "display_label": display_label if display_label is not None else label,
        "state": state,
        "device": device,
        "device_type": device_type,
        "placement": placement,
        # True only for the passive "blocked" copy of a full-depth device on the
        # face it is NOT mounted on (mirrors core's draw_device_rear: the name is
        # shown but the fill is the hatched "blocked" pattern, no state/role
        # color). The PRIMARY (mounted-face) copy keeps opposite_face=False.
        "opposite_face": opposite_face,
        # Displacement marking (spec §3/§4.3, parity ruling 2026-07-09): True
        # on a vacating slot (move_out_ghost/remove) whose rows are occupied
        # by a live planned slot (add/move_in) on the same face;
        # ``displaced_by`` then names the occupant. Set by _mark_displaced().
        # Consumers (the read-only elevation template, the editor's widget
        # payload) render such a slot as the outside stripe bar, never as a
        # full tile composited under the occupant.
        "displaced": False,
        "displaced_by": None,
        # Power projection (docs/power-projection-spec.md §1): the device's
        # projected draw in watts and whether any power data was found. Filled
        # by _project_power() for draw-counting slots (existing/add/move_in);
        # 0/False on vacating (ghost/remove) slots that don't consume.
        "draw_w": 0.0,
        "draw_known": False,
        # Per-PSU detail for the hover card (name / draw / connected), filled by
        # _project_power() for draw-counting slots.
        "power_ports": [],
        # Device bays of a PARENT device (a blade chassis), filled by
        # _attach_bays() in one pass over the finished elevation. Always a list:
        # empty for an ordinary device, so consumers never guard on the key.
        # Each entry: {name, device, device_type, label, occupied}.
        "bays": [],
    }


def _device_type_of(placement):
    """Resolve the relevant DeviceType for a placement (its own, or its device's)."""
    if placement.device_type_id:
        return placement.device_type
    if placement.device_id and placement.device:
        return placement.device.device_type
    return None


def _u_height(device_type):
    """Height in rack units for a device type, defaulting to 1 when unknown."""
    if device_type is not None and device_type.u_height:
        return Decimal(device_type.u_height)
    return Decimal(1)


def _normalize_face(value):
    """Coerce a (possibly blank) face string into a valid dcim face value."""
    if value in (DeviceFaceChoices.FACE_FRONT, DeviceFaceChoices.FACE_REAR):
        return value
    return DeviceFaceChoices.FACE_FRONT


def _is_full_depth(device_type):
    """True when a device type spans the full rack depth (occupies both faces)."""
    return bool(device_type is not None and device_type.is_full_depth)


def _existing_slots(rack, face, excluded_device_ids):
    """
    Real installed devices on one face, as 'existing' slots.

    Uses ``Rack.get_rack_units(expand_devices=False)`` so each device appears once
    (at its bottom-most U) with a ``height``. Devices referenced by the design
    (moves/removes) are excluded here -- they get their own design-aware slots.
    """
    slots = []
    units = rack.get_rack_units(face=face, expand_devices=False)
    for unit in units:
        device = unit.get("device")
        if device is None:
            continue
        if device.pk in excluded_device_ids:
            continue
        u_height = Decimal(unit.get("height") or device.device_type.u_height or 1)
        # get_rack_units returns full-depth devices on BOTH faces. On the face the
        # device is NOT mounted on, mark the slot as the passive "blocked" copy --
        # exactly mirroring core draw_face(): `device.face == face` -> colored,
        # else -> blocked hatch. (Non-full-depth devices only ever come back on
        # their own face, so this is never True for them.)
        opposite = _is_full_depth(device.device_type) and (device.face or "") != face
        slots.append(
            _slot(
                u_position=Decimal(unit["id"]),
                u_height=u_height,
                face=face,
                label=device.name or str(device),
                state=ProjectedSlotState.EXISTING,
                device=device,
                device_type=device.device_type,
                opposite_face=opposite,
            )
        )
    return slots


def _attach_bays(slots_lists):
    """
    Fill each parent device's slot with its DeviceBays, in ONE query for the
    whole elevation.

    A chassis is a container: the question a rack diagram has to answer is which
    of its bays are free. Core forbids a child device from carrying a position or
    a face, so a blade can only ever be reached through its parent -- the bays
    are the only place it can be rendered.

    Done as a post-pass rather than inside ``_slot()`` because the racked slots
    come from core's ``Rack.get_rack_units()``, which we cannot add a prefetch
    to; a per-slot lookup would be N+1 across the rack.
    """
    from dcim.models import DeviceBay

    by_device = {}
    for slots in slots_lists:
        for slot in slots:
            device = slot.get("device")
            device_type = slot.get("device_type")
            if device is None or device_type is None:
                continue
            if not getattr(device_type, "is_parent_device", False):
                continue
            by_device.setdefault(device.pk, []).append(slot)
    if not by_device:
        return

    bays = (
        DeviceBay.objects.filter(device_id__in=by_device)
        .select_related("installed_device", "installed_device__device_type")
    )  # DeviceBay.Meta.ordering is ('device', 'name') on every supported NetBox
    grouped = {}
    for bay in bays:
        installed = bay.installed_device
        grouped.setdefault(bay.device_id, []).append({
            "name": bay.name,
            "bay": bay,
            "device": installed,
            "device_type": installed.device_type if installed is not None else None,
            "label": (installed.name or str(installed)) if installed is not None else "",
            "occupied": installed is not None,
            "state": ProjectedSlotState.EXISTING if installed is not None else None,
            "placement": None,
            # Filled by _project_power(): the occupant's own draw, and whether
            # that draw is already accounted for by the chassis's PSUs.
            "draw_w": 0.0,
            "draw_known": False,
            "draw_included_in_parent": False,
        })
    for device_pk, slots in by_device.items():
        entries = grouped.get(device_pk, [])
        for slot in slots:
            # A full-depth chassis is emitted once per face; each copy is a
            # distinct dict, so give each its own list rather than sharing one.
            slot["bays"] = [dict(entry) for entry in entries]


def _empty_bay(name, bay=None):
    return {
        "name": name,
        "bay": bay,
        "device": None,
        "device_type": None,
        "label": "",
        "occupied": False,
        "state": None,
        "placement": None,
        "draw_w": 0.0,
        "draw_known": False,
        "draw_included_in_parent": False,
    }


def _attach_planned_chassis_bays(slots_lists):
    """
    Give a PLANNED chassis (an 'add' of a parent device type) its bays.

    Such a chassis has no dcim.DeviceBay rows -- core instantiates those from the
    type's DeviceBayTemplates only when the real device is created -- so the bays
    come from the templates, which is also what validates a blade's
    ``target_bay_name`` (models.DesignPlacement._validate_bay_target).
    """
    from dcim.models import DeviceBayTemplate

    by_type = {}
    for slots in slots_lists:
        for slot in slots:
            if slot.get("device") is not None or slot.get("placement") is None:
                continue
            device_type = slot.get("device_type")
            if device_type is None or not getattr(device_type, "is_parent_device", False):
                continue
            by_type.setdefault(device_type.pk, []).append(slot)
    if not by_type:
        return

    names = {}
    for tmpl in DeviceBayTemplate.objects.filter(device_type_id__in=by_type).order_by("name", "pk"):
        names.setdefault(tmpl.device_type_id, []).append(tmpl.name)
    for type_pk, slots in by_type.items():
        for slot in slots:
            slot["bays"] = [_empty_bay(n) for n in names.get(type_pk, [])]


def _overlay_planned_blades(design, slots_lists):
    """
    Fold this design's blade placements into the bay strips they target.

    A blade is never a rack slot: core forbids a child device a position or a
    face, so a placement carrying ``target_bay`` (real chassis) or
    ``parent_placement`` (chassis planned in this design) belongs INSIDE a
    chassis's strip, not in the tray.
    """
    from django.db.models import Q

    blades = list(
        design.placements.filter(
            Q(target_bay__isnull=False) | Q(parent_placement__isnull=False)
        ).select_related("device", "device__device_type", "device_type", "target_bay")
    )
    if not blades:
        return

    by_real_bay = {}
    by_planned = {}
    for placement in blades:
        if placement.target_bay_id:
            by_real_bay[placement.target_bay_id] = placement
        elif placement.parent_placement_id:
            by_planned[(placement.parent_placement_id, placement.target_bay_name)] = placement

    def _apply(entry, placement):
        device_type = _device_type_of(placement) if placement.device_type_id else (
            placement.device.device_type if placement.device_id else None
        )
        entry.update({
            "device": placement.device,
            "device_type": device_type,
            "label": _placement_label(placement, device_type),
            "occupied": placement.kind != DesignPlacementKindChoices.KIND_REMOVE,
            "state": _placement_state(placement),
            "placement": placement,
        })

    for slots in slots_lists:
        for slot in slots:
            for entry in slot.get("bays") or ():
                bay = entry.get("bay")
                if bay is not None and bay.pk in by_real_bay:
                    _apply(entry, by_real_bay[bay.pk])
                    continue
                parent_placement = slot.get("placement")
                if parent_placement is not None:
                    key = (parent_placement.pk, entry["name"])
                    if key in by_planned:
                        _apply(entry, by_planned[key])


def _mark_displaced(slots):
    """
    Mark every vacating slot (move_out_ghost/remove) whose rows are occupied
    by a live planned slot (add/move_in) in the SAME face list as
    ``displaced`` (spec §3/§4.3, parity ruling 2026-07-09), recording the
    occupant's label in ``displaced_by``.

    Full-depth handling falls out of the per-face slot copies: a full-depth
    device's ghost/move_in/add is already emitted once per face (see
    ``_append``), so scanning each face list independently marks the mirror
    copies too, exactly matching the editor's §4.3.3 mirror-collapse rule.

    A device's own planned slot never displaces its own vacating slot (spec
    §4.2: a device's own footprint never blocks itself) -- guarded by both
    placement identity and device identity.
    """
    vacating = [
        s for s in slots
        if s["state"] in (ProjectedSlotState.MOVE_OUT_GHOST, ProjectedSlotState.REMOVE)
        and s["u_position"] is not None
    ]
    live = [
        s for s in slots
        if s["state"] in (ProjectedSlotState.ADD, ProjectedSlotState.MOVE_IN)
        and s["u_position"] is not None
    ]
    for old in vacating:
        old_start = float(old["u_position"])
        old_end = old_start + float(old["u_height"])
        for new in live:
            if old["placement"] is not None and new["placement"] is old["placement"]:
                continue
            if old["device"] is not None and new["device"] is old["device"]:
                continue
            new_start = float(new["u_position"])
            new_end = new_start + float(new["u_height"])
            if old_start < new_end and new_start < old_end:
                old["displaced"] = True
                old["displaced_by"] = new["label"]
                break


def _existing_tray_slots(rack, excluded_device_ids):
    """
    Real devices associated with this rack but not mounted at a U (DCIM
    ``Device.rack == rack`` and ``Device.position is None``), as 'existing'
    non-racked slots (spec §9.1/§9.2: 0U/vertical PDUs, rear-door units, cable
    managers, etc). Devices referenced by the design (moves/removes) are
    excluded here -- they get their own design-aware slots.

    Child devices are excluded. A device installed in a parent's DeviceBay (a
    blade in a chassis) keeps ``rack`` set but is forbidden by core from
    carrying a position or a face (dcim/models/devices.py), so it satisfies the
    position-less test above without being loose hardware -- it lives inside its
    parent's bay. Without this exclusion every blade in the rack rendered in the
    tray beside the real 0U accessories.

    A tray slot's ``face`` is always "" (spec §9.2: "A tray slot is a Device
    with face = ''/u = None; it claims no Units") -- a tray is an unordered
    list, not a grid, so the device's REAL ``face`` field (which may be
    front/rear/blank, e.g. from a full-depth-agnostic 0U accessory) carries no
    layout meaning here and must not leak into the slot's own face, which the
    editor JS treats as a location identifier equivalent to "front"/"rear".
    """
    slots = []
    devices = (
        rack.devices.filter(position__isnull=True, parent_bay__isnull=True)
        .exclude(pk__in=excluded_device_ids)
        .select_related("device_type")
        .order_by("name", "pk")
    )
    for device in devices:
        slots.append(
            _slot(
                u_position=None,
                u_height=_u_height(device.device_type),
                face="",
                label=device.name or str(device),
                state=ProjectedSlotState.EXISTING,
                device=device,
                device_type=device.device_type,
            )
        )
    return slots


# ---------------------------------------------------------------------------
# Power projection (Tier 1, crude / zero-config) -- docs/power-projection-spec.md
# ---------------------------------------------------------------------------

# States whose device actually CONSUMES power in the planned world. A vacating
# ghost (move_out_ghost) and a flagged removal do not (the body draws at its
# target, or is gone).
_DRAW_COUNTING_STATES = frozenset(
    (ProjectedSlotState.EXISTING, ProjectedSlotState.ADD, ProjectedSlotState.MOVE_IN)
)


def _port_draw(obj, basis):
    """Draw (watts) of one PowerPort or PowerPortTemplate for ``basis``
    ('allocated'|'maximum'), falling back to the other field when the chosen one
    is unset. Returns None when neither is set."""
    primary = getattr(obj, f"{basis}_draw", None)
    if primary:
        return primary
    other = "maximum" if basis == "allocated" else "allocated"
    return getattr(obj, f"{other}_draw", None)


def _device_draw_w(device, device_type, basis):
    """Projected draw of a device in watts, plus a status:

    * ``"known"``   -- a draw was resolved (from the device's PowerPorts, or
      failing that its type's PowerPortTemplates).
    * ``"unknown"`` -- the device HAS power ports (or its type defines port
      templates) but none carry a draw value -- a powered device we can't
      account for. Flagged so the total isn't silently under-reported.
    * ``"passive"`` -- the device has NO power ports at all (patch panels,
      cable managers, blanking panels): it legitimately draws nothing, so it is
      neither counted nor flagged.

    Returns ``(watts, status)`` (watts is 0.0 unless status == "known").
    """
    has_ports = False
    if device is not None:
        ports = list(device.powerports.all())
        if ports:
            has_ports = True
            vals = [v for v in (_port_draw(p, basis) for p in ports) if v is not None]
            if vals:
                return float(sum(vals)), "known"
    dt = device_type or (device.device_type if device is not None else None)
    if dt is not None:
        templates = list(dt.powerporttemplates.all())
        if templates:
            has_ports = True
            vals = [v for v in (_port_draw(t, basis) for t in templates) if v is not None]
            if vals:
                return float(sum(vals)), "known"
    return 0.0, ("unknown" if has_ports else "passive")


def _rack_capacity_w(rack, default_w, design=None):
    """Rack power capacity in watts: every feed that will power this rack.

    Real ``dcim.PowerFeed`` rows contribute their ``available_power`` (NetBox's
    own electrical model). A greenfield rack has none yet -- the design plans
    them as ``DesignPowerFeed`` rows instead (docs/pdu-distribution-spec.md
    §6.1) -- so those count too, at the same derating NetBox applies, and the
    bar sizes against the power the rack is *planned* to have. Ignoring them
    left a planned rack pinned to the flat fallback and painted critical-red
    while its own per-bank chips read green: the two power views contradicting
    each other about the same rack. The flat ``default_w`` remains the fallback
    only when neither kind of feed exists."""
    from dcim.models import PowerFeed

    from .distribution import breaker_watts

    total = 0.0
    any_feed = False
    for feed in PowerFeed.objects.filter(rack=rack):
        available = feed.available_power
        if available:
            total += float(available)
            any_feed = True
    if design is not None:
        # Derate planned feeds by the SAME max-utilization NetBox stamps into a
        # real feed's available_power. Read the live config parameter (the field
        # default is a lazy ``ConfigItem``, not a number) so the two stay in step
        # with whatever the instance is configured to use.
        from netbox.config import get_config

        max_util = get_config().POWERFEED_DEFAULT_MAX_UTILIZATION or 100
        for planned in design.planned_feeds.filter(rack=rack):
            watts = breaker_watts(planned)
            if watts:
                total += float(round(watts * max_util / 100.0))
                any_feed = True
    return total if any_feed else float(default_w)


def _device_power_ports(device, device_type, basis):
    """Per-PSU detail for the hover card: a list of
    ``{"name", "draw", "connected"}`` for the device's real PowerPorts, or its
    type's PowerPortTemplates for a planned add (``connected`` is None then --
    a template has no cabling). ``draw`` is the chosen-basis draw (0 if unset)."""
    out = []
    if device is not None:
        ports = list(device.powerports.all())
        if ports:
            for p in ports:
                out.append({
                    "name": p.name,
                    "draw": _port_draw(p, basis) or 0,
                    "connected": getattr(p, "cable_id", None) is not None,
                })
            return out
    dt = device_type or (device.device_type if device is not None else None)
    if dt is not None:
        for t in dt.powerporttemplates.all():
            out.append({"name": t.name, "draw": _port_draw(t, basis) or 0,
                        "connected": None})
    return out


def _device_unconnected(device):
    """True when a REAL device HAS power ports but at least one is not cabled to
    power (a connection gap). False for adds (no real device), passive gear (no
    power ports), and fully-cabled devices."""
    if device is None:
        return False
    ports = list(device.powerports.all())
    if not ports:
        return False
    return any(getattr(p, "cable_id", None) is None for p in ports)


def _bay_draw(slot, basis, parent_status):
    """
    Resolve the power the blades in a chassis contribute, and annotate each bay
    entry with its own figure.

    Mirrors what NetBox does structurally. Core reaches a blade only THROUGH the
    chassis: ``PowerPort.get_power_draw()`` uses the port's own value when it has
    one and otherwise aggregates whatever is cabled downstream, so a chassis and
    its blades can never both be counted. There are no cables in a plan, so the
    same outcome is produced from the containment relationship instead:

    * the chassis has a resolvable draw  -> that wins; the blades are annotated
      ``draw_included_in_parent`` and contribute nothing further.
    * the chassis has none               -> the blades are rolled up.

    Returns ``(watts, status)`` for the roll-up case, else ``(None, None)``.
    """
    entries = slot.get("bays") or ()
    if not entries:
        return None, None

    parent_known = parent_status == "known"
    total = 0.0
    any_known = False
    for entry in entries:
        if not entry.get("occupied"):
            continue
        if entry.get("state") == ProjectedSlotState.REMOVE:
            continue  # a blade this design pulls out no longer draws
        watts, status = _device_draw_w(entry.get("device"), entry.get("device_type"), basis)
        entry["draw_w"] = watts
        entry["draw_known"] = status != "unknown"
        entry["draw_included_in_parent"] = parent_known
        if not parent_known:
            total += watts
            any_known = any_known or status == "known"
    if parent_known:
        return None, None
    return total, ("known" if any_known else None)


def _slot_role_slug(slot):
    """The device's role slug for a slot: the real device's role (existing/move)
    or the placement's chosen role (add). Lowercased; '' when unknown."""
    device = slot.get("device")
    role = None
    if device is not None and getattr(device, "role_id", None):
        role = device.role
    else:
        placement = slot.get("placement")
        if placement is not None and getattr(placement, "device_role_id", None):
            role = placement.device_role
    return (role.slug if role else "").lower()


def _project_power(elevation, *, capacity_default_w, basis, warn_pct, critical_pct,
                   exclude_roles=()):
    """Populate per-slot ``draw_w``/``draw_known`` and return the rack-level
    power summary. Sums each consuming device once (a full-depth device appears
    on both faces but must not double-count) over the planned world.

    Devices whose role is in ``exclude_roles`` (power infrastructure -- PDUs)
    are NOT counted as consumers: their input draw is the aggregate of the
    devices they feed, so counting it double-counts. They get draw_w=0 and are
    left out of the total (and the unknown tally)."""
    exclude = {r.lower() for r in exclude_roles}
    seen = set()
    draw_total = 0.0
    unconnected_devices = []
    unknown_devices = []
    for face_slots in (elevation.front, elevation.rear, elevation.non_racked):
        for slot in face_slots:
            if slot["state"] not in _DRAW_COUNTING_STATES:
                continue
            # Per-PSU detail for the hover card (all consumers + PDUs alike).
            slot["power_ports"] = _device_power_ports(
                slot["device"], slot["device_type"], basis)
            # Power infrastructure (PDU): not a consumer -> 0, excluded from
            # the total, never flagged.
            if _slot_role_slug(slot) in exclude:
                slot["draw_w"] = 0.0
                slot["draw_known"] = True
                continue
            watts, status = _device_draw_w(slot["device"], slot["device_type"], basis)
            # A chassis is a container: fold its bays in (see _bay_draw). When the
            # chassis has no draw of its own the blades supply it; when it does,
            # they are already inside that figure.
            bay_watts, bay_status = _bay_draw(slot, basis, status)
            if bay_watts is not None:
                watts = bay_watts
                if bay_status:
                    status = bay_status
            slot["draw_w"] = watts
            # Passive gear (no power ports) reads as "known 0" -- it draws
            # nothing by design, so the heatmap treats it as a low consumer,
            # not the unknown hatch. Only a powered-but-undrawn device is unknown.
            slot["draw_known"] = status != "unknown"
            device = slot["device"]
            placement = slot["placement"]
            if device is not None:
                key = ("dev", device.pk)
            elif placement is not None and placement.pk is not None:
                key = ("pl", placement.pk)
            else:
                key = ("id", id(slot))
            if key in seen:
                continue
            seen.add(key)
            draw_total += watts
            # Unknown draw (spec §1.3): a device that HAS power ports (or whose
            # type defines port templates) but none carry a draw value -- counted
            # as 0 W but FLAGGED, so the UI can name which powered devices lack
            # draw data instead of silently under-reporting. Passive gear (no
            # ports) is a known 0 and never lands here (draw_known stays True).
            if not slot["draw_known"]:
                unknown_devices.append(slot.get("label") or "")
            # Connection completeness (user ruling 2026-07-13): flag a REAL
            # device that HAS power ports but at least one is NOT cabled to
            # power -- a planning gap ("device with ports not connected"). Keep
            # the count AND names for the hover. Passive gear (no power ports)
            # is skipped, and adds (no real device yet) aren't cabled so aren't
            # flagged.
            if _device_unconnected(device):
                unconnected_devices.append(slot.get("label") or "")

    capacity = _rack_capacity_w(
        elevation.rack, capacity_default_w, design=getattr(elevation, "design", None))
    util = (draw_total / capacity * 100.0) if capacity else 0.0
    if util >= critical_pct:
        state = "critical"
    elif util >= warn_pct:
        state = "warn"
    else:
        state = "ok"
    return {
        "draw_w": draw_total,
        "capacity_w": capacity,
        "util_pct": util,
        "state": state,
        "unconnected_count": len(unconnected_devices),
        "unconnected_devices": unconnected_devices,
        "unknown_draw_count": len(unknown_devices),
        "unknown_devices": unknown_devices,
        "basis": basis,
        # Thresholds echoed so the editor can recolor the bar LIVE (client-side)
        # as devices are shuffled, matching the server's ok/warn/critical.
        "warn_pct": warn_pct,
        "critical_pct": critical_pct,
    }


def device_type_power_summary(device_type, basis=None, role=None):
    """Projected power for a bare device TYPE (no real device yet) -- the draw a
    freshly dropped catalog add carries BEFORE it is saved. Mirrors exactly what
    ``_project_power`` computes for the resulting 'add' slot (same basis, same
    PowerPortTemplate resolution, same excluded-role rule), so a palette add
    shows the same draw live as it will after Save + reload.

    Returns ``{"draw_w": float, "draw_known": bool, "power_ports": [...]}`` where
    each ``power_ports`` entry is ``{"name", "draw", "connected": None}`` (a
    template has no cabling). ``draw_known`` is False only when the type defines
    power-port templates that carry no draw value (a powered type we can't
    account for); a type with no templates at all is passive -> known 0.

    ``role`` is the role the add would carry (the editor's palette Role select).
    A role in ``power_exclude_roles`` is power infrastructure -- a PDU is not a
    consumer -- so it reports the same ``draw_w=0`` / ``draw_known=True`` that
    ``_project_power`` gives the saved slot, instead of the unknown a PDU inlet
    template would otherwise produce. Its per-PSU ``power_ports`` are still
    listed, exactly as ``_project_power`` does for an excluded slot.

    ``basis`` defaults to the configured ``power_draw_basis``.
    """
    config = _power_config()
    if basis is None:
        basis = config["basis"]
    ports = _device_power_ports(None, device_type, basis)
    exclude = {r.lower() for r in config["exclude_roles"]}
    if role is not None and (role.slug or "").lower() in exclude:
        return {"draw_w": 0.0, "draw_known": True, "power_ports": ports}
    watts, status = _device_draw_w(None, device_type, basis)
    return {
        "draw_w": watts,
        "draw_known": status != "unknown",
        "power_ports": ports,
    }


def _power_config():
    """Resolve the power projection config (PLUGINS_CONFIG with defaults)."""
    return {
        "capacity_default_w": get_plugin_config(
            PLUGIN_NAME, "power_capacity_default_w", DEFAULT_POWER_CAPACITY_W),
        "basis": get_plugin_config(
            PLUGIN_NAME, "power_draw_basis", DEFAULT_POWER_BASIS),
        "warn_pct": get_plugin_config(
            PLUGIN_NAME, "power_warn_pct", DEFAULT_POWER_WARN_PCT),
        "critical_pct": get_plugin_config(
            PLUGIN_NAME, "power_critical_pct", DEFAULT_POWER_CRITICAL_PCT),
        "exclude_roles": get_plugin_config(
            PLUGIN_NAME, "power_exclude_roles", DEFAULT_POWER_EXCLUDE_ROLES),
    }


def _natural_bay_key(name):
    """
    Sort key that orders ``slot2`` before ``slot10``.

    dcim.DeviceBay has no naturalized ``_name`` column (unlike most component
    models), so its default ordering is plain alphabetical -- which lists an
    18-bay chassis as slot1, slot10, slot11 ... slot2. Split the name into text
    and number runs so the numeric parts compare numerically.
    """
    import re

    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", name or "")
    ]


def has_chassis_in_scope(design):
    """
    Cheap "does the chassis layer apply to this design at all" test.

    Exists so the rack editor can gate the layer switch without paying for the
    full :func:`chassis_in_scope` projection on every page load. Applies the
    SAME has-bays rule as chassis_in_scope, or the button would offer a layer
    that then renders nothing.
    """
    from dcim.models import Device

    if Device.objects.filter(
        rack__in=design.racks.all(),
        device_type__subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        devicebays__isnull=False,
    ).exists():
        return True
    return design.placements.filter(
        kind=DesignPlacementKindChoices.KIND_ADD,
        device_type__subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        device_type__devicebaytemplates__isnull=False,
    ).exists()


def chassis_in_scope(design):
    """
    Every chassis the chassis layer should show for ``design`` (spec §10.3).

    Two sources, mirroring the two parent kinds a blade can target:

    * REAL parent devices standing in the design's scoped racks -- they have bays
      whether or not this design touches them, and a blade may be planned into
      any free one;
    * PLANNED chassis -- an ``add`` of a parent device type in this design. It has
      no device row and no bays yet, so its bays come from the type's
      DeviceBayTemplates, exactly as ``_attach_planned_chassis_bays`` does.

    Returns a list of dicts, ordered rack-then-name so the layer's columns are
    stable across reloads:
    ``{key, label, device, placement, rack, device_type, bay_names}``.
    ``key`` is what the visibility toggle and the save payload address a column
    by: ``dev-<pk>`` for a real chassis, ``pl-<pk>`` for a planned one.
    """
    from dcim.models import Device, DeviceBayTemplate

    racks = list(design.racks.all())
    out = []

    # A BAY IS THE ONLY THING THAT MAKES A CHASSIS A CHASSIS here. Matching on
    # subdevice_role alone is not enough: the role is routinely set on plain
    # servers (a 4475-device instance had 2306 of them with no bay at all --
    # "sff8"/"lff4" drive-slot models flagged parent by hand), and each one
    # would draw an empty 0/0 column nothing can ever be dropped into.
    reals = (
        Device.objects.filter(
            rack__in=racks,
            device_type__subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
            devicebays__isnull=False,
        )
        .distinct()
        .select_related("device_type", "rack")
        .order_by("rack__name", "name", "pk")
    )
    for device in reals:
        out.append({
            "key": f"dev-{device.pk}",
            "label": device.name or str(device),
            "device": device,
            "placement": None,
            "rack": device.rack,
            "device_type": device.device_type,
            "bay_names": sorted(
                device.devicebays.values_list("name", flat=True), key=_natural_bay_key
            ),
        })

    # Same rule for a PLANNED chassis, read off the type: no bay template means
    # no bay will exist once it is applied, so there is nothing to plan into.
    planned = (
        design.placements.filter(
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type__subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
            device_type__devicebaytemplates__isnull=False,
        )
        .distinct()
        .select_related("device_type", "target_rack")
        .order_by("target_rack__name", "proposed_name", "pk")
    )
    template_names = {}
    for placement in planned:
        dt_id = placement.device_type_id
        if dt_id not in template_names:
            template_names[dt_id] = sorted(
                DeviceBayTemplate.objects.filter(device_type_id=dt_id)
                .values_list("name", flat=True),
                key=_natural_bay_key,
            )
        out.append({
            "key": f"pl-{placement.pk}",
            "label": placement.proposed_name or placement.device_type.model,
            "device": None,
            "placement": placement,
            "rack": placement.target_rack,
            "device_type": placement.device_type,
            "bay_names": template_names[dt_id],
        })
    return out


def project_chassis(design, entry):
    """
    Project ONE chassis as a column of bays -- the chassis layer's answer to
    ``project_rack`` (spec §10.3: a chassis IS a rack, bays in place of units).

    ``entry`` is one row of :func:`chassis_in_scope`. The returned bays reuse the
    SLOT vocabulary so the layer's renderer and the rack renderer speak the same
    language: index (1-based, the "unit"), name, and the occupying device/
    placement with its §3 state.

    Occupancy comes from three layers, later ones overriding earlier:
    reality (a real ``DeviceBay.installed_device``), then this design's blade
    placements addressed by real bay, then those addressed by planned parent.
    """
    from django.db.models import Q

    bay_names = list(entry["bay_names"])
    device = entry["device"]
    placement = entry["placement"]

    real_bays = {}
    if device is not None:
        for bay in device.devicebays.select_related(
                "installed_device", "installed_device__device_type"):
            real_bays[bay.name] = bay

    # A REMOVE takes no target at all (the model forbids one), so it can only be
    # found through the DEVICE -- via the real bay that device sits in. Matching
    # solely on target_bay/parent_placement made a saved blade removal invisible:
    # it was stored correctly, rendered as nothing, and read to the user as
    # "Save threw my removal away" (user 2026-08-26).
    if device is not None:
        blade_query = Q(target_bay__device=device) | Q(device__parent_bay__device=device)
    else:
        blade_query = Q(parent_placement=placement)
    blades = list(
        design.placements.filter(blade_query).select_related(
            "device", "device__device_type", "device_type", "target_bay")
    ) if (device is not None or placement is not None) else []
    by_name = {}
    for blade in blades:
        if blade.target_bay_id:
            name = blade.target_bay.name
        elif blade.target_bay_name:
            name = blade.target_bay_name
        else:
            # A removal: the bay it is being emptied OUT of.
            real_bay = getattr(blade.device, "parent_bay", None) if blade.device_id else None
            name = real_bay.name if real_bay is not None else ""
        if name:
            by_name[name] = blade

    slots = []
    for index, name in enumerate(bay_names, start=1):
        bay = real_bays.get(name)
        installed = bay.installed_device if bay is not None else None
        slot = {
            "index": index,
            "name": name,
            "bay": bay,
            "device": installed,
            "device_type": installed.device_type if installed is not None else None,
            "label": (installed.name or str(installed)) if installed is not None else "",
            "state": ProjectedSlotState.EXISTING if installed is not None else None,
            "placement": None,
            "draw_w": 0.0,
            "draw_known": False,
        }
        blade = by_name.get(name)
        if blade is not None:
            blade_type = blade.device_type or (
                blade.device.device_type if blade.device_id else None)
            slot.update({
                "device": blade.device,
                "device_type": blade_type,
                "label": _placement_label(blade, blade_type),
                "state": _placement_state(blade),
                "placement": blade,
            })
        slots.append(slot)
    return {
        "key": entry["key"],
        "label": entry["label"],
        "rack": entry["rack"],
        "device": device,
        "placement": placement,
        "device_type": entry["device_type"],
        "slots": slots,
        "bay_count": len(bay_names),
        "used": sum(
            1 for s in slots
            if s["label"] and s["state"] != ProjectedSlotState.REMOVE
        ),
    }


def project_rack(design, rack):
    """
    Compute the projected elevation of ``rack`` under ``design``.

    Returns a :class:`ProjectedElevation`. See the module docstring for the full
    result/slot contract. Performs no writes.
    """
    # move/remove reference an existing device; include those whose device is in
    # this rack (the target_rack for a move is also this rack for an in-rack move,
    # but the device's *current* rack is what anchors the ghost / removal).
    moves_removes = list(
        design.placements.exclude(kind=DesignPlacementKindChoices.KIND_ADD)
        .filter(device__isnull=False)
        # As above: a move INTO a bay renders inside the chassis, not at a U.
        .filter(target_bay__isnull=True, parent_placement__isnull=True)
        .select_related("device", "device__device_type", "device_type", "target_rack")
    )
    adds = list(
        design.placements.filter(kind=DesignPlacementKindChoices.KIND_ADD)
        .filter(target_rack=rack)
        # A blade is not a rack slot: a placement targeting a device bay (real or
        # planned) is folded into its chassis's strip by _overlay_planned_blades()
        # instead. Emitting it here too would double it into the tray.
        .filter(target_bay__isnull=True, parent_placement__isnull=True)
        .select_related("device_type", "target_rack")
    )

    # Devices whose real slot should be suppressed from the plain 'existing' pass
    # because the design re-renders them (move_out_ghost / move_in / remove).
    design_device_ids = set()
    for placement in moves_removes:
        if placement.device_id and (
            placement.device.rack_id == rack.pk or placement.target_rack_id == rack.pk
        ):
            design_device_ids.add(placement.device_id)

    front = _existing_slots(rack, DeviceFaceChoices.FACE_FRONT, design_device_ids)
    rear = _existing_slots(rack, DeviceFaceChoices.FACE_REAR, design_device_ids)
    # Real position-less devices (the tray's "reality" layer, spec §9.1) come
    # first; design-driven non_racked entries (adds/moves with no target
    # position) are appended below by the _append() helper.
    non_racked = _existing_tray_slots(rack, design_device_ids)

    def _append(slot, full_depth=False):
        # A position-less slot (e.g. a target-less add/move) is never face-mirrored.
        if slot["u_position"] is None:
            non_racked.append(slot)
            return
        # Full-depth devices physically occupy BOTH faces, so a design slot for
        # one must render on each face (mirroring how core get_rack_units already
        # returns existing full-depth devices on both faces). Emit one slot PER
        # face -- identical state/label/device/device_type/placement/U, differing
        # only in `face` -- so each face elevation colors/edits it the same and the
        # save path (which dedupes by placement_id) still resolves to ONE
        # placement.
        if full_depth:
            # The slot's own `face` is the device's real/target (mounted) face;
            # that copy keeps its normal colored state. The OTHER face copy is the
            # passive "blocked" indicator (opposite_face=True).
            mounted = slot["face"]
            front_slot = dict(slot)
            front_slot["face"] = DeviceFaceChoices.FACE_FRONT
            front_slot["opposite_face"] = mounted != DeviceFaceChoices.FACE_FRONT
            rear_slot = dict(slot)
            rear_slot["face"] = DeviceFaceChoices.FACE_REAR
            rear_slot["opposite_face"] = mounted != DeviceFaceChoices.FACE_REAR
            front.append(front_slot)
            rear.append(rear_slot)
            return
        if slot["face"] == DeviceFaceChoices.FACE_REAR:
            rear.append(slot)
        else:
            front.append(slot)

    # --- adds: virtual planned slots in this rack -------------------------------
    for placement in adds:
        device_type = placement.device_type
        label = placement.proposed_name or (device_type.model if device_type else "?")
        position = placement.target_position
        _append(
            _slot(
                u_position=Decimal(position) if position is not None else None,
                u_height=_u_height(device_type),
                face=_normalize_face(placement.target_face),
                label=label,
                state=ProjectedSlotState.ADD,
                device_type=device_type,
                placement=placement,
            ),
            full_depth=_is_full_depth(device_type),
        )

    # --- moves & removes --------------------------------------------------------
    for placement in moves_removes:
        device = placement.device
        device_type = _device_type_of(placement)
        u_height = _u_height(device_type)
        full_depth = _is_full_depth(device_type)

        if placement.kind == DesignPlacementKindChoices.KIND_REMOVE:
            # Flag the device's current slot (only if it lives in this rack).
            if device.rack_id != rack.pk:
                continue
            current_face = _normalize_face(device.face)
            _append(
                _slot(
                    u_position=Decimal(device.position) if device.position else None,
                    u_height=u_height,
                    face=current_face,
                    label=device.name or str(device),
                    state=ProjectedSlotState.REMOVE,
                    device=device,
                    device_type=device_type,
                    placement=placement,
                ),
                full_depth=full_depth,
            )
            continue

        # KIND_MOVE: ghost at the original spot (if currently in this rack) and a
        # move_in slot at the target (if the target is this rack).
        if device.rack_id == rack.pk and device.position:
            _append(
                _slot(
                    u_position=Decimal(device.position),
                    u_height=u_height,
                    face=_normalize_face(device.face),
                    label=device.name or str(device),
                    state=ProjectedSlotState.MOVE_OUT_GHOST,
                    device=device,
                    device_type=device_type,
                    placement=placement,
                ),
                full_depth=full_depth,
            )
        if placement.target_rack_id == rack.pk:
            position = placement.target_position
            _append(
                _slot(
                    u_position=Decimal(position) if position is not None else None,
                    u_height=u_height,
                    face=_normalize_face(placement.target_face),
                    label=device.name or str(device),
                    state=ProjectedSlotState.MOVE_IN,
                    device=device,
                    device_type=device_type,
                    placement=placement,
                    # The plan's new identity for the device (user ruling
                    # 2026-07-10): the tile SHOWS the assigned name; the
                    # identity `label` above stays the device's real name.
                    display_label=placement.proposed_name or None,
                ),
                full_depth=full_depth,
            )

    # Displacement marking (spec §3/§4.3, parity ruling 2026-07-09): per-face
    # post-pass so the read-only elevation and the editor's on-load render
    # apply the SAME displaced treatment as the editor's live gesture flow.
    _mark_displaced(front)
    _mark_displaced(rear)
    _attach_bays((front, rear, non_racked))
    _attach_planned_chassis_bays((front, rear, non_racked))
    _overlay_planned_blades(design, (front, rear, non_racked))

    # Order each racked face top-of-rack first (descending U), matching
    # get_rack_units default ordering; non_racked keeps insertion order.
    front.sort(key=lambda s: s["u_position"], reverse=True)
    rear.sort(key=lambda s: s["u_position"], reverse=True)

    elevation = ProjectedElevation(
        design=design,
        rack=rack,
        front=front,
        rear=rear,
        non_racked=non_racked,
    )
    # Power projection (docs/power-projection-spec.md): fills per-slot draw and
    # the rack-level summary over the planned world just built above.
    elevation.power = _project_power(elevation, **_power_config())
    # Per-PDU/bank distribution (docs/pdu-distribution-spec.md): computed in
    # "builtin" (native, zero-config) or "script" mode (else None -> the
    # frontend keeps the per-device heatmap). A broken builtin/script degrades
    # to None, never erroring the projection.
    from .distribution import generate_distribution
    elevation.power["distribution"] = generate_distribution(elevation)
    return elevation
