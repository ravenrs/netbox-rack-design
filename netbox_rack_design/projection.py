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

from dcim.choices import DeviceFaceChoices

from .choices import DesignPlacementKindChoices

__all__ = (
    "ProjectedSlotState",
    "ProjectedElevation",
    "project_rack",
)


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
):
    """Build a single projected-slot dict following the documented contract."""
    return {
        "u_position": u_position,
        "u_height": u_height,
        "face": face,
        "label": label,
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

    A tray slot's ``face`` is always "" (spec §9.2: "A tray slot is a Device
    with face = ''/u = None; it claims no Units") -- a tray is an unordered
    list, not a grid, so the device's REAL ``face`` field (which may be
    front/rear/blank, e.g. from a full-depth-agnostic 0U accessory) carries no
    layout meaning here and must not leak into the slot's own face, which the
    editor JS treats as a location identifier equivalent to "front"/"rear".
    """
    slots = []
    devices = (
        rack.devices.filter(position__isnull=True)
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
        .select_related("device", "device__device_type", "device_type", "target_rack")
    )
    adds = list(
        design.placements.filter(kind=DesignPlacementKindChoices.KIND_ADD)
        .filter(target_rack=rack)
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
                ),
                full_depth=full_depth,
            )

    # Displacement marking (spec §3/§4.3, parity ruling 2026-07-09): per-face
    # post-pass so the read-only elevation and the editor's on-load render
    # apply the SAME displaced treatment as the editor's live gesture flow.
    _mark_displaced(front)
    _mark_displaced(rear)

    # Order each racked face top-of-rack first (descending U), matching
    # get_rack_units default ordering; non_racked keeps insertion order.
    front.sort(key=lambda s: s["u_position"], reverse=True)
    rear.sort(key=lambda s: s["u_position"], reverse=True)

    return ProjectedElevation(
        design=design,
        rack=rack,
        front=front,
        rear=rear,
        non_racked=non_racked,
    )
