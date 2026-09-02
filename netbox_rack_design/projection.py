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

    inherited     bool             True when the slot comes from an ANCESTOR
                                   design's layer rather than from reality or
                                   from this design (see "design chains"
                                   below).
    source_design_id int | None    The ancestor Design that last touched an
                                   inherited slot's identity.
    conflict      bool             True when something outside this design's
                                   control is wrong about this slot (today:
                                   its settled name could not be resolved).
    conflict_reason str | None     Human-readable reason when ``conflict``.

------------------------------------------------------------------------------
DESIGN CHAINS  (PLAN-design-chains.md G1 / §9.2)
------------------------------------------------------------------------------

A design may be based on another (``Design.based_on``), and that one on a third:
the transitive chain IS the layer stack. ``project_rack`` therefore composes
THREE layers, in this order:

    reality (dcim)  ->  each ancestor's layer, oldest first  ->  this design

An ancestor's effects are BASELINE, not proposals. From the child's point of
view they have already happened, so an ancestor ``add`` renders as an
``existing`` slot flagged ``inherited`` -- never as an ``add``/``move_in``/
``remove`` tile with this design's semantics. Replay semantics per ancestor
placement: an ``add`` occupies its target U, a ``move`` vacates the source U
*and* occupies the target, a ``remove`` frees the U.

BAYS compose the same way, through a PARALLEL replay on the same identity keys
(``_Baseline.bay_entries`` / ``bay_layer``): a blade is never a rack slot (core
forbids a child device a position or a face), so an ancestor's bay-targeted
placement is inherited INSIDE a chassis strip, and is structurally incapable of
reaching a face or the tray. The bay layers compose in this order -- reality
(minus whatever an ancestor moved or removed), the bay templates of a planned
chassis, the inherited blades, then this design's own -- for all three bay
surfaces: a rack elevation's strips (``_overlay_planned_blades``), which chassis
exist at all (``chassis_in_scope``, which also drops a real chassis an ancestor
removed or moved out of scope and adds one an ancestor planned), and one chassis
as a column (``project_chassis``).

A child may also plan a blade INTO an ancestor-planned chassis
(``DesignPlacement.base_parent_placement``, G2): the third way a placement names
its parent, and the only one that crosses designs in the parent direction
(``parent_placement`` is same-design by construction, and ``base_placement``
identifies the blade ITSELF, not its parent). Such a blade is the CHILD's own
proposal -- an ``add``/``move`` with this design's state and flags, never
``inherited`` -- sitting in a bay of an inherited chassis, and it renders in both
bay surfaces. It resolves its parent through the SAME identity seam the replay
uses: the field points at the originating ``add``, ``_chassis_identity_key``
reduces whichever ancestor row last named the chassis to that same key, so a
chassis a later ancestor re-planned is still the same parent. Claiming a bay an
inherited blade already holds is the ordinary ``bay_occupied`` warning -- the
child's tile still renders (§8.5.3) -- and a refused chain drops the inherited
chassis, and therefore the blade with it, rather than drawing an orphan.

A parent contributes its layer WHOLE or NOT AT ALL (§9.2), driven by
``Design.status``: only ``approved`` replays. An ``implemented`` ancestor is
refused -- reality may already contain part of its layer, so replaying it would
double-count and draw a believable rack that is simply false; the projection
drops the whole chain and reports it in ``conflicts`` instead (§9.5: the failure
mode is a BLOCK, not a lie). Anything else (draft/rejected/superseded) is
refused for the mirror-image reason: approval is what freezes a design (§2.2),
so an unapproved parent's layer is still moving and a child inheriting it would
render a world that changes under it with nothing on screen to say so.

    conflicts     list[dict]       Problems that are not slots (§8.3), each
                                   ``{kind, severity, slot, placement,
                                   source_design, detail}``. Rendered by a
                                   PERSISTENT panel, never a toast: an upstream
                                   conflict is not this design's fault and
                                   persists until someone re-bases.

Slots whose ``state`` is ``existing`` come straight from the real rack and were
not referenced by any placement. Slots touched by the design carry their
originating ``placement``.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from dcim.choices import DeviceFaceChoices, SubdeviceRoleChoices
from django.db.models import prefetch_related_objects
from netbox.plugins import get_plugin_config

from .choices import DesignPlacementKindChoices, DesignStatusChoices

__all__ = (
    "ProjectedSlotState",
    "ProjectedElevation",
    "project_rack",
    "baseline_occupancy",
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
    # Problems with this design that are NOT a slot (PLAN-design-chains.md
    # §8.3): before this list there was nowhere in the projection contract to
    # hang "the chain this design is built on cannot be projected". Each entry
    # is ``{kind, severity, slot, placement, source_design, detail}`` -- see
    # ``_conflict()`` for what each key means and who fills it. Consumed by the
    # persistent panel that already carries the stale-placement alert, NOT by a
    # toast: an upstream conflict is not this design's fault, cannot be fixed by
    # editing the tile, and persists across sessions until someone re-bases
    # (§8.2). Ordered as produced -- chain-level entries first, then per-slot.
    conflicts: list = field(default_factory=list)


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
    inherited=False,
    source_design_id=None,
    conflict=False,
    conflict_reason=None,
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
        # PROVENANCE (PLAN-design-chains.md §8.4): a FLAG, deliberately not a new
        # ProjectedSlotState member. True when this slot came from an ANCESTOR
        # design's layer -- from the child's point of view the change has already
        # happened, so the slot's ``state`` stays the plain ``existing`` of the
        # world it describes and only its provenance differs. New states
        # (base_existing, base_add, ...) would double the rendering matrix in
        # docs/editor-behavior-spec.md §3 for every state and break the legend's
        # one-checkbox-per-state filter model. ``source_design_id`` names the
        # ancestor that LAST touched the identity (a three-deep chain reports the
        # design whose move put the device where it now is, which is the one a
        # planner needs to talk to). Consumers: the read-only elevation template
        # and the editor's widget payload, which dim/outline such a tile and name
        # the source design in its hover card.
        "inherited": inherited,
        "source_design_id": source_design_id,
        # CONFLICT (§8.4), the same flag-not-state call: something outside this
        # design's control is wrong about this slot. Today the only producer is a
        # settled-name resolution failure (§3.3: an inherited slot must render
        # under its settled name, and a failure must be SURFACED rather than
        # quietly falling back to the ancestor's planning name). The slot still
        # renders -- its U is not in doubt, only its name -- and the matching
        # entry in ``ProjectedElevation.conflicts`` carries the detail for the
        # panel. Never the hard-collision path: a conflict marker never blocks a
        # save (§8.2).
        "conflict": conflict,
        "conflict_reason": conflict_reason,
        # Device bays of a PARENT device (a blade chassis), filled by
        # _attach_bays() in one pass over the finished elevation. Always a list:
        # empty for an ordinary device, so consumers never guard on the key.
        # Each entry: {name, device, device_type, label, occupied, state,
        # placement, draw_*, and the same inherited / source_design_id /
        # conflict / conflict_reason flags this slot carries}.
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


# ---------------------------------------------------------------------------
# Baseline replay across a design chain -- PLAN-design-chains.md G1 / §9.2
# ---------------------------------------------------------------------------


def _conflict(kind, *, severity="error", slot=None, placement=None,
              source_design=None, detail=""):
    """One entry for ``ProjectedElevation.conflicts`` (§8.3).

    * ``kind``          -- machine-readable category, so a renderer can pick an
      icon/word without parsing ``detail``. Phase 3 produces five:
      ``ancestor_implemented`` and ``ancestor_not_approved`` (the §9.2 refusal),
      ``chain_broken`` (the lineage does not resolve), ``settled_name``, and
      ``bay_occupied`` (this design claims a bay an ancestor's blade already
      holds -- §8.5.3, marked and never blocking).
    * ``severity``      -- ``"error"`` (this design cannot be trusted as drawn)
      or ``"warning"``. Never blocks a save either way (§8.2).
    * ``slot``          -- the slot dict this is about, or None for a
      design-level problem. The SAME dict object that is in a face list, so a
      renderer can match by identity.
    * ``placement``     -- the DesignPlacement involved, or None.
    * ``source_design`` -- the ANCESTOR design at fault, or None. This is what
      makes the message actionable: "re-base off X", not "something upstream".
    * ``detail``        -- the sentence a human reads.

    Kept deliberately general: phase 4 adds producers (upstream vacated what you
    built on, upstream now occupies your target U) into the same shape and the
    same panel, rather than a parallel alert per kind.
    """
    return {
        "kind": kind,
        "severity": severity,
        "slot": slot,
        "placement": placement,
        "source_design": source_design,
        "detail": detail,
    }


@dataclass
class _BaselineEntry:
    """One identity in the inherited world, and where the replay left it.

    An identity is either a REAL device (an ancestor moved it) or an
    ancestor-PLANNED one (an ancestor added it, and it has no ``dcim.Device``
    row -- PLAN-design-chains.md G2). ``key`` is what makes the replay
    idempotent: successive ancestor placements acting on the same identity
    overwrite this entry instead of adding a second slot, which is the whole
    reason an ancestor `move` can vacate and occupy in one step.

    ``rack_id``/``position``/``face`` are the identity's CURRENT baseline
    location -- i.e. after every ancestor has had its say, not where reality
    puts it. ``position is None`` means the tray (a position-less target).
    """

    key: tuple
    # The placement that NAMES this identity: the originating add, unless a later
    # ancestor layer re-planned the name. ``source_design`` tracks provenance
    # separately, because the design that last MOVED a device is often not the
    # one that named it.
    placement: object
    source_design: object
    device: object
    device_type: object
    rack_id: object
    position: object
    face: str
    # WHERE IN A CHASSIS, for an identity that lives in a BAY rather than at a U.
    # A blade is never a rack slot (core forbids a child device a position or a
    # face), so the bay replay addresses a location the three fields above cannot
    # express. Exactly one of the two forms is set, mirroring the two ways a
    # placement addresses a bay: ``parent_device_id`` + ``bay_name`` for a REAL
    # chassis (``target_bay``), or ``parent_key`` + ``bay_name`` for one the
    # ancestor itself planned (``parent_placement``). ``parent_key`` is an
    # IDENTITY key, not a pk, so a chassis a later ancestor re-planned is still
    # the same parent.
    bay_id: object = None
    bay_name: str = ""
    parent_device_id: object = None
    parent_key: tuple = None
    label: str = None
    display_label: str = None
    conflict: bool = False
    conflict_reason: str = None
    named: bool = False


def _identity_key(placement):
    """The baseline identity a placement acts on, or None if it acts on nothing.

    An ``add`` IS a new identity, so it keys on its own pk. A move/remove acts
    on an EXISTING one: a real device (``device``) or the not-yet-real one an
    ancestor planned (``base_placement``, always a ``kind=add`` row -- enforced
    by ``DesignPlacement._validate_base_placement``), so the add's pk is the
    identity and every downstream row acting on it keys to the same tuple.
    """
    if placement.kind == DesignPlacementKindChoices.KIND_ADD:
        return ("pl", placement.pk)
    if placement.base_placement_id:
        return ("pl", placement.base_placement_id)
    if placement.device_id:
        return ("dev", placement.device_id)
    return None


def _ancestor_refusal(ancestor):
    """The §9.2 conflict for an ancestor that must not be replayed, or None."""
    status = ancestor.status
    if status == DesignStatusChoices.STATUS_IMPLEMENTED:
        return _conflict(
            "ancestor_implemented",
            source_design=ancestor,
            detail=f"{ancestor} is marked implemented, so reality may already "
                   f"contain part of its layer and replaying it would count "
                   f"those changes twice. Nothing is inherited from this chain: "
                   f"re-base this design onto the world as it now stands.",
        )
    if status != DesignStatusChoices.STATUS_APPROVED:
        return _conflict(
            "ancestor_not_approved",
            source_design=ancestor,
            detail=f"{ancestor} is {ancestor.get_status_display().lower()}, not "
                   f"approved: its placements are still free to change, so a "
                   f"design built on it would render a world that moves "
                   f"underneath it. Nothing is inherited from this chain until "
                   f"it is approved.",
        )
    return None


def resolve_baseline_chain(design):
    """``(chain, refusal)`` -- the §9.2 all-or-nothing answer for ``design``.

    ``chain`` is ``design.baseline_chain()`` (oldest ancestor first, excluding
    ``design`` itself) when every ancestor may be replayed WHOLE; otherwise
    ``[]``. ``refusal`` is the :func:`_conflict` entry naming why, or ``None``
    when there is nothing to refuse (no ``based_on`` at all, or every ancestor
    approved).

    Factored out of ``_Baseline._build`` so every consumer of "which ancestors
    does this design's world include" -- the rack-face replay, the rack
    capacity bar (G5 item 1), and the ``DesignRackPower`` inheritance rule (G5
    item 2) -- asks the SAME question the SAME way. A second, slightly
    different notion of "the chain" in each place is exactly how a stray
    ancestor ends up counted where it should have been refused.
    """
    if not design.based_on_id:
        return [], None
    try:
        chain = design.baseline_chain()
    except ValueError as exc:
        # A cycle in the lineage. baseline_chain() raises rather than looping;
        # a projection must degrade to single-layer and SAY so, never 500 on a
        # page the planner cannot fix from there.
        return [], _conflict(
            "chain_broken",
            detail=f"This design's lineage cannot be resolved, so nothing is "
                   f"inherited: {exc}",
        )
    for ancestor in chain:
        refusal = _ancestor_refusal(ancestor)
        if refusal is not None:
            # §9.2: a layer is contributed WHOLE or not at all -- and a broken
            # ancestor breaks every layer stacked on top of it too, because
            # those layers were planned against ITS result. So the whole
            # chain is dropped, not just the offending link.
            return [], refusal
    return chain, None


class _Baseline:
    """The world a design inherits from its ``based_on`` ancestors, for one rack.

    Built once per :func:`project_rack` call and consumed three ways, which is
    why it is an object rather than a parameter threaded through
    ``_existing_slots``:

    * ``suppressed_device_ids`` widens the exclusion set the plain reality pass
      (``_existing_slots`` / ``_existing_tray_slots``) already takes, so a real
      device an ancestor moved or removed stops rendering at its REAL U. That
      needed no signature change anywhere -- reality is still reality; what
      changed is which parts of it are still true.
    * :meth:`emit` appends the inherited slots through the caller's own
      ``_append``, so full-depth face mirroring, the tray split and the
      top-of-rack sort are the ones already written for this design's layer,
      not a second copy of them.
    * :meth:`entry` answers "where is this identity now?" for the two callers
      that must start from the baseline rather than from reality: this design's
      own move/remove of an identity an ancestor already relocated, and
      ``DesignPlacement._validate_target_slot`` (G1), which cannot see planned
      occupancy through ``Rack.get_available_units``.

    Deliberately NOT folded into ``_existing_slots``: that function's data
    source is core's ``Rack.get_rack_units()``, an entirely different shape from
    a placement replay, and a surface that needs the baseline (the tray, a
    chassis's bays, the model-layer slot validation) would then have to re-enter
    through a rack-face function that returns nothing it wants.
    """

    def __init__(self, design, rack=None):
        self.design = design
        # ``rack`` is optional: the BAY consumers (chassis_in_scope,
        # project_chassis) are not per-rack, and a blade claims no U, so they
        # build a rack-less baseline. Only ``claims()`` and ``emit()`` -- the two
        # rack-face consumers -- need it.
        self.rack = rack
        self.conflicts = []
        # Real devices whose REAL slot is no longer true, because an ancestor
        # moved or removed them. Widens the exclusion set of the reality pass --
        # and, for a real BLADE, empties the real DeviceBay it still sits in.
        self.suppressed_device_ids = set()
        # identity key -> _BaselineEntry, in replay order (dicts preserve it).
        self.entries = {}
        # The PARALLEL replay for identities that live in a bay rather than at a
        # U, same keys and same names. Deliberately a second dict rather than a
        # flag on ``entries``: every rack-face consumer (``emit``, ``claims``)
        # iterates ``entries``, and a blade must be structurally incapable of
        # reaching a face or the tray -- not merely filtered out of them.
        self.bay_entries = {}
        # Identities whose settled-name failure has already been reported, so a
        # bay asked about by several consumers yields one panel row, not one per
        # question.
        self._reported_names = set()
        self._build()

    # -- construction -------------------------------------------------------

    def _build(self):
        from .models import DesignPlacement

        design = self.design
        chain, refusal = resolve_baseline_chain(design)
        # Exposed for consumers OUTSIDE the rack-face replay that need the same
        # approved-ancestors-or-nothing answer (G5): rack capacity and the
        # DesignRackPower inheritance rule both resolve through this same list,
        # rather than re-deriving it (and risking a second, divergent notion of
        # "which ancestors count").
        self.chain = chain
        if refusal is not None:
            self.conflicts.append(refusal)
        if not chain:
            return  # No chain (or refused): the baseline IS reality.

        chain_order = {ancestor.pk: index for index, ancestor in enumerate(chain)}
        placements = (
            DesignPlacement.objects.filter(design_id__in=chain_order)
            .select_related(
                "design", "device", "device__device_type", "device_type",
                "target_rack", "base_placement", "base_placement__device_type",
                "target_bay", "device__parent_bay",
            )
        )
        # OLDEST ANCESTOR FIRST, then pk within a design. The order is
        # load-bearing, not cosmetic: A adds a device at U10 and B relocates it
        # to U20, so replaying B before A would leave the device at U10 (or at
        # both), a believable rack that is simply false.
        for placement in sorted(
            placements, key=lambda p: (chain_order[p.design_id], p.pk)
        ):
            # ONE replay per LOCATION KIND, one identity space across both. A
            # bay-targeted row can never mean a U, and a U-targeted one can never
            # mean a bay, so routing here (rather than filtering the query, as
            # the rack-only replay did) is what lets a blade be inherited at all
            # while keeping it out of the faces and the tray.
            if self._is_bay_action(placement):
                self._replay_bay(placement)
            else:
                self._replay(placement)

    @staticmethod
    def _is_bay_action_target(placement):
        return bool(placement.target_bay_id or placement.parent_placement_id)

    def _is_bay_action(self, placement):
        """True when this placement acts INSIDE a chassis rather than at a U.

        Three ways, and the last two are why this is a method and not the query
        filter it replaces: a placement can address a bay explicitly, or act on
        an identity the replay has ALREADY put in a bay, or -- a ``remove``,
        which by model rule carries no target at all -- be recognisable only by
        the real bay its device currently sits in.
        """
        if self._is_bay_action_target(placement):
            return True
        key = _identity_key(placement)
        if key is not None and key in self.bay_entries:
            return True
        return bool(placement.device_id
                    and getattr(placement.device, "parent_bay", None) is not None)

    def _replay(self, placement):
        """Fold ONE ancestor placement into the baseline.

        The three verbs, exactly as G1 states them: an ``add`` occupies its
        target U, a ``move`` vacates the source U *and* occupies the target, a
        ``remove`` frees the U. "Vacates" is two different things depending on
        what the identity is -- a real device stops rendering at its real slot
        (``suppressed_device_ids``), a planned one simply moves its entry -- and
        both are handled by keying on the identity rather than on a location.
        """
        if placement.stale:
            return  # Whatever it referenced is gone; it projects nothing.
        key = _identity_key(placement)
        if key is None:
            return
        kind = placement.kind

        if kind == DesignPlacementKindChoices.KIND_ADD:
            self.entries[key] = _BaselineEntry(
                key=key,
                placement=placement,
                source_design=placement.design,
                device=None,
                device_type=placement.device_type,
                rack_id=placement.target_rack_id,
                position=placement.target_position,
                face=placement.target_face,
            )
            return

        # A move/remove of a REAL device makes that device's real slot untrue,
        # whichever rack it lands in (or none).
        if placement.device_id:
            self.suppressed_device_ids.add(placement.device_id)

        if kind == DesignPlacementKindChoices.KIND_REMOVE:
            self.entries.pop(key, None)
            return

        # KIND_MOVE. A relocation is not a re-creation: the entry is updated in
        # place, so an identity an earlier ancestor already named and typed keeps
        # both. Rebuilding it would silently drop an earlier layer's rename the
        # moment a later layer nudged the device one U.
        existing = self.entries.get(key)
        if existing is None:
            if placement.base_placement_id:
                # The upstream add is not in the replay (cancelled, or it targeted
                # a bay), so there is no identity to relocate. Reported as
                # staleness on the design that owns the row, never invented here.
                return
            existing = self.entries[key] = _BaselineEntry(
                key=key,
                placement=placement,
                source_design=placement.design,
                device=placement.device,
                device_type=_device_type_of(placement),
                rack_id=None,
                position=None,
                face="",
            )
        existing.rack_id = placement.target_rack_id
        existing.position = placement.target_position
        existing.face = placement.target_face
        # Provenance is the ancestor that LAST touched the identity -- the design
        # a planner has to talk to about where this device now is.
        existing.source_design = placement.design
        if placement.device_id:
            existing.device = placement.device
            existing.device_type = _device_type_of(placement)
        if placement.proposed_name:
            # Only a layer that actually NAMES the identity becomes the naming
            # placement; a plain reposition leaves the name where it was set.
            existing.placement = placement
            existing.named = False

    def _replay_bay(self, placement):
        """Fold ONE ancestor placement into the BAY baseline.

        The same three verbs as :meth:`_replay`, against a bay instead of a U --
        and deliberately the same identity keys, so an ancestor `move` of a blade
        vacates one bay and occupies another in a single step, and a downstream
        design acting on that blade resolves to the very same entry.

        Freeing a bay is again two different things: a real blade stops rendering
        in the real bay it still occupies (``suppressed_device_ids``, honoured by
        ``_attach_bays`` and ``project_chassis``), while an ancestor-planned one
        simply moves or drops its entry.
        """
        if placement.stale:
            return
        key = _identity_key(placement)
        if key is None:
            return
        kind = placement.kind

        if placement.device_id:
            self.suppressed_device_ids.add(placement.device_id)

        if kind == DesignPlacementKindChoices.KIND_REMOVE:
            self.bay_entries.pop(key, None)
            return

        bay = placement.target_bay
        address = {
            "bay_id": placement.target_bay_id,
            "bay_name": bay.name if bay is not None else placement.target_bay_name,
            "parent_device_id": bay.device_id if bay is not None else None,
            # A planned parent, addressed as an IDENTITY. Either route names one:
            # ``parent_placement`` (the ancestor's own chassis) or
            # ``base_parent_placement`` (a chassis planned FURTHER up the chain,
            # G2) -- both always point at the originating ``add``, so the key is
            # the same tuple whichever way this layer reached it.
            "parent_key": (
                ("pl", placement.parent_placement_id or placement.base_parent_placement_id)
                if (placement.parent_placement_id or placement.base_parent_placement_id)
                else None
            ),
        }

        if kind == DesignPlacementKindChoices.KIND_ADD:
            self.bay_entries[key] = _BaselineEntry(
                key=key,
                placement=placement,
                source_design=placement.design,
                device=None,
                device_type=placement.device_type,
                rack_id=placement.target_rack_id,
                position=None,
                face="",
                **address,
            )
            return

        # KIND_MOVE -- updated in place for the same reason the rack replay does
        # it: a relocation is not a re-creation, so an earlier layer's rename and
        # type survive a later layer nudging the blade one bay over.
        existing = self.bay_entries.get(key)
        if existing is None:
            if placement.base_placement_id:
                # The upstream add is not in the replay (cancelled, or it targeted
                # a U). Nothing to relocate; reported as staleness on the design
                # that owns the row, never invented here.
                return
            existing = self.bay_entries[key] = _BaselineEntry(
                key=key,
                placement=placement,
                source_design=placement.design,
                device=placement.device,
                device_type=_device_type_of(placement),
                rack_id=None,
                position=None,
                face="",
            )
        for attr, value in address.items():
            setattr(existing, attr, value)
        existing.rack_id = placement.target_rack_id
        existing.source_design = placement.design
        if placement.device_id:
            existing.device = placement.device
            existing.device_type = _device_type_of(placement)
        if placement.proposed_name:
            existing.placement = placement
            existing.named = False

    # -- names --------------------------------------------------------------

    def _resolve_names(self, entry):
        """Fill ``label``/``display_label`` on an entry, under §3.2 R1.

        An inherited slot renders under its SETTLED name: the planning prefix is
        the OWNING design's bookkeeping, so ``IDS-1234_srv-01`` in the ancestor
        is ``srv-01`` in the child.

        ``label`` vs ``display_label`` (the 2026-07-10 ruling documented in
        ``_slot``) resolves differently for the two kinds of identity, and both
        answers follow from ``label`` being the STABLE IDENTITY string:

        * a REAL device keeps its real name as ``label`` -- that is what anchors
          ghost pairing, harnesses and the read-model, and it is unchanged by an
          ancestor planning to rename it -- while ``display_label`` shows the
          settled name the ancestor gives it;
        * an ancestor-PLANNED identity has no real name to be stable about. The
          settled name is the only handle the child's world has for it, so it is
          both. Using the ancestor's planning name as ``label`` would leak
          ``IDS-1234_`` into ghost pairing and the read model, which is exactly
          the coupling R1 exists to break.

        A resolution failure must not break the render, so
        ``settled_name_status`` (the non-raising variant) is used -- but it must
        not silently produce a planning name either, so the fallback is flagged
        ``conflict`` on the slot and reported in ``conflicts``.
        """
        if entry.named:
            return
        entry.named = True
        placement = entry.placement
        device = entry.device
        real_name = (device.name or str(device)) if device is not None else None

        if not placement.proposed_name:
            # Nothing was ever named, so there is no prefix to strip and no
            # settled name to fail at: the real device's name, or the catalog
            # model for a planned add that was never named.
            entry.label = real_name or _placement_label(placement, entry.device_type)
            entry.display_label = entry.label
            return

        from .naming import settled_name_status

        settled, status = settled_name_status(placement)
        if settled is None:
            entry.conflict = True
            entry.conflict_reason = status["detail"]
            entry.label = real_name or placement.proposed_name
            entry.display_label = placement.proposed_name
            return
        entry.label = real_name or settled
        entry.display_label = settled

    # -- consumption --------------------------------------------------------

    def _report_settled_name(self, entry):
        """Surface an inherited BAY entry's settled-name failure, once (§3.3).

        The rack path reports it from ``emit`` while building the slot; a bay
        entry has no slot of its own and may be asked for by several consumers,
        so the report is deduped on the identity instead.
        """
        if not entry.conflict or entry.key in self._reported_names:
            return
        self._reported_names.add(entry.key)
        self.conflicts.append(_conflict(
            "settled_name",
            placement=entry.placement,
            source_design=entry.source_design,
            detail=f"The settled name of {entry.placement.proposed_name!r} "
                   f"(planned by {entry.source_design}) could not be resolved, so "
                   f"this bay is showing that design's PLANNING name: "
                   f"{entry.conflict_reason}",
        ))

    def bay_entry(self, key):
        """The baseline BAY entry for one identity, or None -- names resolved.

        The bay twin of :meth:`entry`, for the caller that must read an inherited
        blade's type and settled name off the baseline because its own row
        (a ``base_placement`` move/remove) carries neither.
        """
        entry = self.bay_entries.get(key)
        if entry is not None:
            self._resolve_names(entry)
        return entry

    def entry(self, key):
        """The baseline location of one identity, or None if it holds none.

        Used by this design's own move/remove pass: a device an ancestor already
        relocated must be vacated from where the ANCESTOR left it, not from
        where reality still shows it, or the ghost lands on the wrong U.
        """
        entry = self.entries.get(key)
        if entry is not None:
            self._resolve_names(entry)
        return entry

    def bay_layer(self, *, device=None, parent_key=None, own=None):
        """What this baseline puts in ONE chassis's bays: ``{bay name: payload}``.

        The single seam every bay consumer goes through -- the rack elevation's
        bay strips (``_overlay_planned_blades``) and the chassis layer's own
        column (``project_chassis``) -- so the two cannot disagree about what an
        inherited chassis holds. ``payload`` is the subset of the bay-entry
        contract that occupancy determines, ready to ``dict.update()`` into
        either shape.

        Address the chassis by ``device`` (a real one) or ``parent_key`` (the
        IDENTITY key of one an ancestor planned), exactly the two ways a
        placement can target a bay.

        ``own`` maps identity key -> the placement THIS design makes on that
        identity, and it is what keeps the child in charge of its own proposals:

        * a ``remove`` renders the inherited bay as this design's removal tile
          (state ``remove``, not ``inherited``) -- the same treatment the rack
          pass gives a child's remove of an ancestor-planned identity;
        * a ``move`` FREES the bay and emits nothing: the target bay is drawn by
          the design's own overlay, wherever that is;
        * anything else leaves the bay inherited.
        """
        out = {}
        for entry in self.bay_entries.values():
            if not entry.bay_name:
                continue
            if device is not None:
                if entry.parent_device_id != device.pk:
                    continue
            elif parent_key is not None:
                if entry.parent_key != parent_key:
                    continue
            else:
                continue
            acting = (own or {}).get(entry.key)
            if acting is not None and acting.kind != DesignPlacementKindChoices.KIND_REMOVE:
                continue  # Freed by this design; its target draws the occupant.
            self._resolve_names(entry)
            self._report_settled_name(entry)
            if acting is None:
                out[entry.bay_name] = {
                    "device": entry.device,
                    "device_type": entry.device_type,
                    # A bay entry has ONE name field and it is the visible one
                    # (that is what ``_placement_label`` fills for this design's
                    # own blades), so the settled name goes there -- §3.2 R1 by
                    # the same ``_resolve_names`` the rack layer uses.
                    "label": entry.display_label,
                    "occupied": True,
                    "state": ProjectedSlotState.EXISTING,
                    "placement": entry.placement,
                    "inherited": True,
                    "source_design_id": entry.source_design.pk,
                    "conflict": entry.conflict,
                    "conflict_reason": entry.conflict_reason,
                }
            else:
                out[entry.bay_name] = {
                    "device": entry.device,
                    "device_type": entry.device_type,
                    "label": entry.display_label,
                    "occupied": False,
                    "state": ProjectedSlotState.REMOVE,
                    "placement": acting,
                    "inherited": False,
                    "source_design_id": None,
                    "conflict": False,
                    "conflict_reason": None,
                }
        return out

    def inherited_chassis(self, rack_ids):
        """Ancestor-PLANNED chassis standing in ``rack_ids``, as scope rows.

        ``chassis_in_scope``'s missing third source: a chassis an ancestor
        planned is part of the child's world, so a blade may be planned into its
        bays -- but it has no ``dcim.Device`` row for the real-parent query to
        find and no placement in THIS design for the planned-parent query.

        Rows are in ``chassis_in_scope``'s own shape, minus ``rack`` (the caller
        holds the Rack objects) -- see there for the contract.
        """
        from dcim.models import DeviceBayTemplate

        rows = []
        for entry in self.bay_capable_chassis(rack_ids):
            names = sorted(
                DeviceBayTemplate.objects
                .filter(device_type_id=entry.device_type.pk)
                .values_list("name", flat=True),
                key=_natural_bay_key,
            )
            if not names:
                # No bay template means no bay will ever exist: the same
                # "a bay is the only thing that makes a chassis a chassis" rule
                # chassis_in_scope applies to this design's own planned adds.
                continue
            self._resolve_names(entry)
            rows.append({
                "key": f"pl-{entry.key[1]}",
                "label": entry.display_label or entry.device_type.model,
                "device": None,
                "placement": entry.placement,
                "rack_id": entry.rack_id,
                "device_type": entry.device_type,
                "bay_names": names,
                "inherited": True,
                "source_design_id": entry.source_design.pk,
            })
        return rows

    def bay_capable_chassis(self, rack_ids):
        """Baseline entries that are PLANNED parent devices inside ``rack_ids``."""
        out = []
        for entry in self.entries.values():
            if entry.device is not None or entry.device_type is None:
                continue
            if entry.rack_id not in rack_ids:
                continue
            if not getattr(entry.device_type, "is_parent_device", False):
                continue
            out.append(entry)
        return out

    def claims(self):
        """Every U this baseline claims in ``self.rack``, for slot validation.

        Flat dicts rather than slots, because the consumer
        (``DesignPlacement._validate_target_slot``) is doing interval arithmetic
        against a proposed target, not rendering anything.
        """
        out = []
        for entry in self.entries.values():
            if entry.rack_id != self.rack.pk or entry.position is None:
                continue
            out.append({
                "key": entry.key,
                "u_position": entry.position,
                "u_height": _u_height(entry.device_type),
                "face": _normalize_face(entry.face),
                "is_full_depth": _is_full_depth(entry.device_type),
                "placement": entry.placement,
                "source_design": entry.source_design,
            })
        return out

    def emit(self, append, *, skip_keys=()):
        """Append every inherited slot for this rack through ``append``.

        ``append`` is ``project_rack``'s own ``_append``, so full-depth face
        mirroring, the tray split and the sort are shared with this design's
        layer rather than reimplemented -- the one place the two layers must
        agree exactly, because a mirror rule fixed in one and not the other is
        precisely the class of bug the container refactor was about.

        ``skip_keys`` are the identities THIS design acts on: they are drawn by
        the design's own move/remove pass as a ghost/removal, and an inherited
        occupied slot at the same U would double them.
        """
        for entry in self.entries.values():
            if entry.key in skip_keys:
                continue
            if entry.rack_id != self.rack.pk:
                continue
            self._resolve_names(entry)
            slot = _slot(
                u_position=Decimal(entry.position) if entry.position is not None else None,
                u_height=_u_height(entry.device_type),
                face=_normalize_face(entry.face),
                label=entry.label,
                display_label=entry.display_label,
                state=ProjectedSlotState.EXISTING,
                device=entry.device,
                device_type=entry.device_type,
                placement=entry.placement,
                inherited=True,
                source_design_id=entry.source_design.pk,
                conflict=entry.conflict,
                conflict_reason=entry.conflict_reason,
            )
            if entry.conflict:
                self.conflicts.append(_conflict(
                    "settled_name",
                    slot=slot,
                    placement=entry.placement,
                    source_design=entry.source_design,
                    detail=f"The settled name of {entry.placement.proposed_name!r} "
                           f"(planned by {entry.source_design}) could not be "
                           f"resolved, so this tile is showing that design's "
                           f"PLANNING name: {entry.conflict_reason}",
                ))
            append(slot, full_depth=_is_full_depth(entry.device_type))


def baseline_occupancy(design, rack):
    """What ``design``'s ancestor chain claims in ``rack``: ``(claims, freed)``.

    The model layer's window onto the replay (G1). ``DesignPlacement``'s slot
    validation reuses ``Rack.get_available_units``, which knows only the REAL
    rack -- an ancestor's planned add occupies no real U, and a real device an
    ancestor moved still occupies its old one -- so without this a child could
    drop a device straight onto an inherited tile and discover the collision
    only by looking at the rendered elevation.

    ``claims`` is the list from :meth:`_Baseline.claims`; ``freed`` is the set of
    real device PKs the chain vacates, which the caller passes to
    ``get_available_units(exclude=...)``.

    Exposed as a function rather than the ``_Baseline`` object so the model layer
    depends on an answer, not on the replay's internals.
    """
    baseline = _Baseline(design, rack)
    return baseline.claims(), set(baseline.suppressed_device_ids)


def _existing_slots(rack, face, excluded_device_ids):
    """
    Real installed devices on one face, as 'existing' slots.

    Uses ``Rack.get_rack_units(expand_devices=False)`` so each device appears once
    (at its bottom-most U) with a ``height``. Devices referenced by the design
    (moves/removes) are excluded here -- they get their own design-aware slots.

    ``excluded_device_ids`` is also where a design CHAIN enters this function
    (G1): a real device an ancestor moved or removed is no longer where reality
    says it is, so ``_Baseline.suppressed_device_ids`` widens the same set. The
    reality pass itself is unchanged -- reality is still reality; what a
    baseline changes is which parts of it are still true -- and the inherited
    slots are appended separately by ``_Baseline.emit``.
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


def _attach_bays(slots_lists, suppressed_device_ids=()):
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

    ``suppressed_device_ids`` is the baseline's (G1): a real blade an ANCESTOR
    already moved or removed still sits in its real bay in DCIM, and rendering it
    there would contradict the very layer that moved it. The bay comes back
    EMPTY, and the blade is drawn wherever the ancestor put it -- exactly the
    treatment the same set gives a real device's rack slot.
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
        if installed is not None and installed.pk in suppressed_device_ids:
            installed = None  # An ancestor already moved/removed it (G1).
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
            # PROVENANCE / CONFLICT, the §8.4 flags -- the same four keys the
            # rack slot carries, for the same reason and read the same way.
            "inherited": False,
            "source_design_id": None,
            "conflict": False,
            "conflict_reason": None,
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
        "inherited": False,
        "source_design_id": None,
        "conflict": False,
        "conflict_reason": None,
    }


def _attach_planned_chassis_bays(slots_lists):
    """
    Give a PLANNED chassis (an 'add' of a parent device type) its bays.

    Such a chassis has no dcim.DeviceBay rows -- core instantiates those from the
    type's DeviceBayTemplates only when the real device is created -- so the bays
    come from the templates, which is also what validates a blade's
    ``target_bay_name`` (models.DesignPlacement._validate_bay_target).

    Covers an INHERITED planned chassis (G1) with no change: its slot is
    device-less and carries the ANCESTOR's placement, which is exactly the
    "device is None and a placement produced it" test below. A chassis planned
    upstream therefore gets its bay strip from the same pass and the same
    templates as one planned here -- the only difference is whose placement
    named it.
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


def _chassis_identity_key(slot):
    """The identity key of the chassis a slot draws, when it is a PLANNED one.

    Read through ``_identity_key`` rather than off the placement's pk, so a
    chassis an ancestor planned and a LATER ancestor re-planned still resolves to
    the identity its blades were addressed to.
    """
    placement = slot.get("placement")
    if slot.get("device") is not None or placement is None:
        return None
    return _identity_key(placement)


def _emit_baseline_bays(baseline, slots_lists, own):
    """Fold the inherited blades (G1) into every chassis strip in an elevation.

    Walks the strips ``_attach_bays`` / ``_attach_planned_chassis_bays`` have
    already built and asks the baseline what it puts in each -- addressing a real
    chassis by its device and a planned one by its identity key, the two ways a
    placement can name a parent.
    """
    if not baseline.bay_entries:
        return
    for slots in slots_lists:
        for slot in slots:
            entries = slot.get("bays")
            if not entries:
                continue
            layer = baseline.bay_layer(
                device=slot.get("device"),
                parent_key=_chassis_identity_key(slot),
                own=own,
            )
            if not layer:
                continue
            for entry in entries:
                payload = layer.get(entry["name"])
                if payload is not None:
                    entry.update(payload)


def _bay_identity_map(placements):
    """``{identity key: placement}`` for the rows a design makes on bay identities."""
    out = {}
    for placement in placements:
        key = _identity_key(placement)
        if key is not None:
            out[key] = placement
    return out


def _bay_conflict(sink, entry, placement, seen):
    """Flag a bay this design claims that the BASELINE already occupies (§8.5.3).

    The child's own tile still renders -- it is its proposal and it is not the
    hard-collision path (§8.2), which would reject the save and snap the drag
    back for something the child did not cause and cannot fix by moving that
    tile. It is marked instead, and the detail goes to the persistent panel.

    ``entry`` is only ever still ``inherited`` here when it holds a DIFFERENT
    identity: an inherited bay this design's own row acts on was already freed by
    ``bay_layer(own=...)``. ``seen`` dedupes a full-depth chassis, whose strip is
    emitted once per face.

    ``sink`` is the conflicts list the caller publishes: the elevation's for a
    rack (one list per rack, as for every other problem on it), a per-chassis one
    for the chassis layer, so a column never reports a conflict that is in a
    different chassis.
    """
    if not entry.get("inherited") or sink is None:
        return None
    occupant = entry.get("label") or "a device"
    reason = f"{occupant} already occupies this bay upstream."
    token = (placement.pk, entry.get("name"))
    if token in seen:
        # A full-depth chassis's strip is emitted once per face; both copies must
        # carry the same flag, but the panel wants one row.
        return reason
    seen.add(token)
    sink.append(_conflict(
        "bay_occupied",
        severity="warning",
        placement=placement,
        # The design at fault, so the message can say "re-base off X" -- read off
        # the inherited placement itself rather than the flag, because
        # ``_conflict`` carries the Design, not its pk.
        source_design=getattr(entry.get("placement"), "design", None),
        detail=f"Bay {entry.get('name')!r} is claimed by this design, but "
               f"{occupant} is already planned into it upstream. This design's "
               f"blade is still shown -- the two conflict until someone re-bases.",
    ))
    return reason


def _overlay_planned_blades(design, slots_lists, baseline=None):
    """
    Fold this design's blade placements into the bay strips they target.

    A blade is never a rack slot: core forbids a child device a position or a
    face, so a placement carrying ``target_bay`` (real chassis),
    ``parent_placement`` (chassis planned in this design) or
    ``base_parent_placement`` (chassis planned by an ANCESTOR, G2) belongs INSIDE
    a chassis's strip, not in the tray.

    The BASELINE's blades (G1) are folded in first, through
    ``_Baseline.bay_layer``, so this design's own layer sits on top of them in
    the bay strips exactly as it does on the rack faces. Ordering is the whole
    point: reality (``_attach_bays``) -> bay templates for a planned chassis ->
    the inherited layer -> this design.
    """
    from django.db.models import Q

    blades = list(
        design.placements.filter(
            Q(target_bay__isnull=False) | Q(parent_placement__isnull=False)
            # The child's OWN blade in a chassis an ANCESTOR planned (G2): its
            # parent is upstream, so neither of the two same-design routes above
            # can see it.
            | Q(base_parent_placement__isnull=False)
            # A move/remove of an ancestor-PLANNED blade addresses the identity,
            # not a bay: a remove carries no target at all (model rule), so the
            # only thing that says "this row is about that inherited blade" is
            # base_placement. Rows whose identity turns out to live at a U rather
            # than in a bay simply match nothing below.
            | Q(base_placement__isnull=False)
        ).select_related("device", "device__device_type", "device_type", "target_bay")
    )
    own = _bay_identity_map(blades)
    if baseline is not None:
        _emit_baseline_bays(baseline, slots_lists, own)
    if not blades:
        return

    by_real_bay = {}
    by_planned = {}
    # The cross-design parent route (G2), keyed on the chassis's IDENTITY rather
    # than on a placement pk: the slot that draws an inherited chassis carries
    # whichever ancestor layer last NAMED it, which is not necessarily the
    # originating add this row points at. ``_chassis_identity_key`` collapses
    # both to the same tuple.
    by_base_parent = {}
    for placement in blades:
        if placement.target_bay_id:
            by_real_bay[placement.target_bay_id] = placement
        elif placement.parent_placement_id:
            by_planned[(placement.parent_placement_id, placement.target_bay_name)] = placement
        elif placement.base_parent_placement_id:
            key = ("pl", placement.base_parent_placement_id)
            by_base_parent[(key, placement.target_bay_name)] = placement

    seen_conflicts = set()

    def _apply(entry, placement):
        device_type = _device_type_of(placement) if placement.device_type_id else (
            placement.device.device_type if placement.device_id else None
        )
        # A move/remove of an ancestor-PLANNED blade (base_placement, G2) carries
        # neither a device nor a device type: the identity's type and name live in
        # the baseline, and that is the only place to read them from.
        base_entry = (
            baseline.bay_entry(_identity_key(placement)) if baseline is not None else None
        )
        if device_type is None and base_entry is not None:
            device_type = base_entry.device_type
        label = placement.proposed_name or (
            base_entry.display_label if base_entry is not None
            else _placement_label(placement, device_type)
        )
        reason = _bay_conflict(
            baseline.conflicts if baseline is not None else None,
            entry, placement, seen_conflicts,
        )
        entry.update({
            "device": placement.device or (
                base_entry.device if base_entry is not None else None),
            "device_type": device_type,
            "label": label,
            "occupied": placement.kind != DesignPlacementKindChoices.KIND_REMOVE,
            "state": _placement_state(placement),
            "placement": placement,
            "inherited": False,
            "source_design_id": None,
            "conflict": reason is not None,
            "conflict_reason": reason,
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
                        continue
                if by_base_parent:
                    key = (_chassis_identity_key(slot), entry["name"])
                    if key in by_base_parent:
                        _apply(entry, by_base_parent[key])


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
    only when neither kind of feed exists.

    A design CHAIN (G5 item 1) widens which planned feeds count: an approved
    ancestor's layer has already happened from this design's point of view, so
    its planned feeds size the rack too -- the same §9.2 all-or-nothing rule
    the placement replay uses, via :func:`resolve_baseline_chain`. A refusal
    (broken lineage, or a non-approved/implemented ancestor) contributes
    NOTHING from that chain; it does not fall back to erroring, because the
    refusal is already reported as a conflict by the SAME ``project_rack``
    call this feeds into (``_Baseline._build``) -- inventing a second one here
    would just repeat it.
    """
    from dcim.models import PowerFeed

    from .distribution import breaker_watts
    from .models import DesignPowerFeed

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
        # ONE query over the union of design ids, never a per-design loop that
        # could visit the same row twice: each DesignPowerFeed row belongs to
        # exactly one design (its own FK), and baseline_chain() is cycle-guarded
        # and excludes ``design`` itself, so the id set below names each design
        # -- and therefore each feed row -- exactly once, structurally.
        chain, _refusal = resolve_baseline_chain(design)
        design_ids = {design.pk} | {ancestor.pk for ancestor in chain}
        for planned in DesignPowerFeed.objects.filter(design_id__in=design_ids, rack=rack):
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


def slot_role(slot):
    """The role this slot's device WILL have, or None.

    The placement wins where it states one -- an add's chosen role, or a move's
    planned re-attribution -- and otherwise the real device's own role stands.
    Null on the placement means "leave it alone", so a plain reposition keeps
    reading the device.
    """
    placement = slot.get("placement")
    if placement is not None and getattr(placement, "device_role_id", None):
        return placement.device_role
    device = slot.get("device")
    if device is not None and getattr(device, "role_id", None):
        return device.role
    return None


def slot_tenant(slot):
    """The tenant this slot's device WILL have, or None. Same precedence as
    :func:`slot_role`."""
    placement = slot.get("placement")
    if placement is not None and getattr(placement, "tenant_id", None):
        return placement.tenant
    device = slot.get("device")
    if device is not None and getattr(device, "tenant_id", None):
        return device.tenant
    return None


def _slot_role_slug(slot):
    """The slot's effective role slug, lowercased; '' when unknown."""
    role = slot_role(slot)
    return (role.slug if role else "").lower()


def _prefetch_power(slots_lists):
    """Load every power port / port template for the whole elevation at once.

    Three helpers below -- ``_device_draw_w``, ``_device_power_ports`` and
    ``_device_unconnected`` -- each ask a device for ``powerports.all()``, and a
    related manager re-queries on every access unless the objects were
    prefetched. That is three round trips per device, so a full rack cost
    hundreds of queries and most of the projection's wall time (measured: 1853
    queries for one live recompute of a five-rack design, ~1.1s of it here).

    Prefetching onto the already-loaded instances -- the same one-query-per-
    elevation approach ``_attach_bays`` takes -- makes all three read from cache
    instead. Bay occupants are included: ``_bay_draw`` reaches them too.
    """
    devices, device_types = [], []
    seen_devices, seen_types = set(), set()

    def collect(device, device_type):
        if device is not None and device.pk is not None and device.pk not in seen_devices:
            seen_devices.add(device.pk)
            devices.append(device)
        if device_type is not None and device_type.pk is not None and device_type.pk not in seen_types:
            seen_types.add(device_type.pk)
            device_types.append(device_type)

    for slots in slots_lists:
        for slot in slots:
            collect(slot.get("device"), slot.get("device_type"))
            for bay in (slot.get("bays") or ()):
                occupant = bay.get("device") if isinstance(bay, dict) else None
                collect(occupant, getattr(occupant, "device_type", None))

    if devices:
        prefetch_related_objects(devices, "powerports", "device_type__powerporttemplates")
    if device_types:
        prefetch_related_objects(device_types, "powerporttemplates")


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

    # A CHAINED design's answer comes from the full projection: the inherited
    # world both adds chassis (an ancestor's planned one) and takes them away (a
    # real one an ancestor removed), and neither is visible to the two cheap
    # queries below. Only chained designs pay for it.
    if design.based_on_id:
        return bool(chassis_in_scope(design))

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


def chassis_in_scope(design, baseline=None):
    """
    Every chassis the chassis layer should show for ``design`` (spec §10.3).

    Three sources, mirroring the parent kinds a blade can target:

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
    by: ``dev-<pk>`` for a real chassis, ``pl-<pk>`` for a planned one -- and for
    an inherited one, ``pl-<identity pk>``, the ancestor's originating add, so
    the key is stable however many later layers re-planned it.

    The third source is the BASELINE (G1): a chassis an ancestor planned is part
    of the world this design is built on, so its bays are plannable here. The
    baseline also CORRECTS the first source, because a real chassis an ancestor
    removed or moved out of scope no longer stands where DCIM still says it does
    -- offering its bays would be a column nothing can ever be built into. Every
    row carries ``inherited`` / ``source_design_id``, the same §8.4 flags a slot
    does. ``baseline`` may be passed in when the caller already has one.
    """
    from dcim.models import Device, DeviceBayTemplate

    racks = list(design.racks.all())
    rack_ids = {rack.pk for rack in racks}
    racks_by_id = {rack.pk: rack for rack in racks}
    if baseline is None:
        baseline = _Baseline(design)
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
    def _real_row(device, rack):
        return {
            "key": f"dev-{device.pk}",
            "label": device.name or str(device),
            "device": device,
            "placement": None,
            "rack": rack,
            "device_type": device.device_type,
            "bay_names": sorted(
                device.devicebays.values_list("name", flat=True), key=_natural_bay_key
            ),
            "inherited": False,
            "source_design_id": None,
        }

    for device in reals:
        if device.pk in baseline.suppressed_device_ids:
            moved = baseline.entry(("dev", device.pk))
            if moved is None:
                continue  # An ancestor removed it: it is not there to plan into.
            if moved.rack_id not in rack_ids:
                continue  # An ancestor moved it out of this design's scope.
        out.append(_real_row(device, device.rack))

    # A real chassis an ancestor moved INTO scope stands in a scoped rack in the
    # inherited world while DCIM still has it elsewhere, so the query above
    # cannot find it.
    listed = {row["key"] for row in out}
    for entry in baseline.entries.values():
        device = entry.device
        if device is None or entry.rack_id not in rack_ids:
            continue
        if f"dev-{device.pk}" in listed:
            continue
        if not getattr(device.device_type, "is_parent_device", False):
            continue
        if not device.devicebays.exists():
            continue
        out.append(_real_row(device, racks_by_id[entry.rack_id]))

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
            "inherited": False,
            "source_design_id": None,
        })

    # ...and the same rule read off the BASELINE, for a chassis an ancestor
    # planned. ``inherited_chassis`` applies the identical no-bay-template test.
    for row in baseline.inherited_chassis(rack_ids):
        row["rack"] = racks_by_id[row.pop("rack_id")]
        out.append(row)

    # The replay this scope was computed from, so ``project_chassis`` can reuse it
    # instead of rebuilding one per column: a chassis layer is one page, and it
    # must not re-derive the inherited world once per chassis on it. Private by
    # name because it is an internal handoff between these two functions, not
    # part of the row contract a template reads.
    for row in out:
        row["_baseline"] = baseline
    return out


def project_chassis(design, entry, baseline=None):
    """
    Project ONE chassis as a column of bays -- the chassis layer's answer to
    ``project_rack`` (spec §10.3: a chassis IS a rack, bays in place of units).

    ``entry`` is one row of :func:`chassis_in_scope`. The returned bays reuse the
    SLOT vocabulary so the layer's renderer and the rack renderer speak the same
    language: index (1-based, the "unit"), name, and the occupying device/
    placement with its §3 state.

    Occupancy comes from FOUR layers, later ones overriding earlier: reality (a
    real ``DeviceBay.installed_device``, minus whatever an ancestor already moved
    or removed), then the INHERITED blades (G1, via ``_Baseline.bay_layer`` --
    the same seam the rack elevation's strips go through), then this design's
    blade placements addressed by real bay, then those addressed by planned
    parent. ``baseline`` may be passed in when the caller already has one, which
    also lets a whole chassis layer share one replay.

    The result carries ``conflicts`` for the same reason ``ProjectedElevation``
    does (§8.3): a chain problem is not a bay, so there was nowhere to hang it.
    """
    from django.db.models import Q

    bay_names = list(entry["bay_names"])
    device = entry["device"]
    placement = entry["placement"]
    if baseline is None:
        baseline = entry.get("_baseline") or _Baseline(design)

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
        # This design's OWN blades in a chassis an ANCESTOR planned (G2). The
        # column is addressed by the chassis's IDENTITY, not by the placement pk
        # that named it -- a later ancestor layer may have re-planned the chassis,
        # in which case ``placement`` is that layer's row while the blade points
        # at the originating add. ``_identity_key`` collapses both to one tuple,
        # so this reads the identity's pk back out of it.
        chassis_key = _identity_key(placement) if placement is not None else None
        if chassis_key is not None and chassis_key[0] == "pl":
            blade_query |= Q(base_parent_placement_id=chassis_key[1])
    blades = list(
        design.placements.filter(blade_query).select_related(
            "device", "device__device_type", "device_type", "target_bay")
    ) if (device is not None or placement is not None) else []
    # This design's rows acting on an INHERITED blade (G2): a move/remove of an
    # ancestor-planned identity carries no device and, for a remove, no target
    # either, so ``base_placement`` is the only thing that identifies it. Queried
    # only when there is an inherited world to act on.
    own_rows = list(blades)
    if baseline.bay_entries:
        own_rows += list(
            design.placements.exclude(kind=DesignPlacementKindChoices.KIND_ADD)
            .filter(base_placement__isnull=False)
            .select_related("device", "device__device_type", "device_type", "target_bay")
        )
    own = _bay_identity_map(own_rows)

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

    # The inherited layer for THIS chassis, addressed the way the chassis itself
    # is: by device when it is real, by identity key when an ancestor planned it.
    inherited = baseline.bay_layer(
        device=device,
        parent_key=_identity_key(placement) if (device is None and placement is not None)
        else None,
        own=own,
    )
    seen_conflicts = set()
    # THIS chassis's own bay conflicts, kept out of the shared baseline: a column
    # must never report a conflict that lives in a different chassis, which is
    # what one design-wide list would do as soon as the layer shares a replay.
    bay_conflicts = []

    slots = []
    for index, name in enumerate(bay_names, start=1):
        bay = real_bays.get(name)
        installed = bay.installed_device if bay is not None else None
        if installed is not None and installed.pk in baseline.suppressed_device_ids:
            installed = None  # An ancestor already moved/removed it (G1).
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
            # The §8.4 flags, exactly as a rack slot and a bay strip entry carry
            # them, so one renderer serves all three.
            "inherited": False,
            "source_design_id": None,
            "conflict": False,
            "conflict_reason": None,
        }
        payload = inherited.get(name)
        if payload is not None:
            slot.update(payload)
        blade = by_name.get(name)
        if blade is not None:
            blade_type = blade.device_type or (
                blade.device.device_type if blade.device_id else None)
            base_entry = baseline.bay_entry(_identity_key(blade))
            if blade_type is None and base_entry is not None:
                blade_type = base_entry.device_type
            reason = _bay_conflict(bay_conflicts, slot, blade, seen_conflicts)
            slot.update({
                "device": blade.device or (
                    base_entry.device if base_entry is not None else None),
                "device_type": blade_type,
                "label": blade.proposed_name or (
                    base_entry.display_label if base_entry is not None
                    else _placement_label(blade, blade_type)),
                "state": _placement_state(blade),
                "placement": blade,
                "inherited": False,
                "source_design_id": None,
                "conflict": reason is not None,
                "conflict_reason": reason,
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
        # Chain-level problems first (a refused ancestor, an unresolvable
        # settled name -- design-wide, so every column reports them), then this
        # chassis's own bay conflicts.
        "conflicts": list(baseline.conflicts) + bay_conflicts,
    }


def project_rack(design, rack):
    """
    Compute the projected elevation of ``rack`` under ``design``.

    Returns a :class:`ProjectedElevation`. See the module docstring for the full
    result/slot contract, including how a design CHAIN composes (reality, then
    each ancestor's layer oldest first, then this design). Performs no writes.
    """
    from django.db.models import Q

    # The inherited world (G1). Built first because everything below depends on
    # it: which parts of reality are still true, where an identity this design
    # moves currently sits, and what this design's own layer sits on top of. A
    # design with no ``based_on`` builds an empty one and pays a single boolean.
    baseline = _Baseline(design, rack)

    # move/remove reference an existing identity; include those whose device is in
    # this rack (the target_rack for a move is also this rack for an in-rack move,
    # but the device's *current* rack is what anchors the ghost / removal).
    #
    # "Existing identity" is now two things (G2): a real ``device``, or an
    # ancestor design's planned add (``base_placement``) that has no dcim row
    # yet. A stale row has neither and is excluded by the same OR.
    moves_removes = list(
        design.placements.exclude(kind=DesignPlacementKindChoices.KIND_ADD)
        .filter(Q(device__isnull=False) | Q(base_placement__isnull=False))
        # As above: a move INTO a bay renders inside the chassis, not at a U --
        # including a bay of a chassis an ANCESTOR planned (G2).
        .filter(target_bay__isnull=True, parent_placement__isnull=True,
                base_parent_placement__isnull=True)
        .select_related("device", "device__device_type", "device_type", "target_rack",
                        "base_placement", "base_placement__device_type")
    )
    adds = list(
        design.placements.filter(kind=DesignPlacementKindChoices.KIND_ADD)
        .filter(target_rack=rack)
        # A blade is not a rack slot: a placement targeting a device bay -- real,
        # planned here, or planned by an ANCESTOR (G2) -- is folded into its
        # chassis's strip by _overlay_planned_blades() instead. Emitting it here
        # too would double it into the tray.
        .filter(target_bay__isnull=True, parent_placement__isnull=True,
                base_parent_placement__isnull=True)
        .select_related("device_type", "target_rack")
    )

    # Devices whose real slot should be suppressed from the plain 'existing' pass
    # because the design re-renders them (move_out_ghost / move_in / remove), or
    # because an ANCESTOR already moved/removed them so reality is out of date.
    design_device_ids = set(baseline.suppressed_device_ids)
    for placement in moves_removes:
        if placement.device_id and (
            placement.device.rack_id == rack.pk or placement.target_rack_id == rack.pk
        ):
            design_device_ids.add(placement.device_id)

    # The identities THIS design acts on: the baseline must not also draw them as
    # occupied, or a device would appear both where the ancestor left it and as
    # this design's ghost of that same U.
    own_keys = {
        key for key in (_identity_key(p) for p in moves_removes) if key is not None
    }

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

    # --- the ancestor layers (G1) ----------------------------------------------
    # Emitted BEFORE this design's own layer, through the same ``_append``, so
    # they are baseline for everything that follows: the displacement pass sees
    # them as part of the world (an ancestor's occupancy never "displaces"), and
    # the power pass counts them as consumers, which they are.
    baseline.emit(_append, skip_keys=own_keys)

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
        # WHERE THE IDENTITY CURRENTLY IS -- the baseline's answer, not reality's,
        # because an ancestor may already have moved it: A moves a device from U1
        # to U10 and this design moves it on to U20, so the ghost belongs at U10.
        # For an ancestor-PLANNED identity (base_placement, G2) the baseline is
        # the only answer there is: nothing real exists to read a position off.
        entry = baseline.entry(_identity_key(placement))
        if placement.base_placement_id and entry is None:
            # The upstream add is not in the projected baseline -- the chain was
            # refused (§9.2), or the ancestor's add is gone. Drawing this row
            # anyway would invent a device at a U nobody planned.
            continue
        if entry is not None:
            device_type = entry.device_type
            current_rack_id = entry.rack_id
            current_position = entry.position
            current_face = entry.face
            identity_label = entry.label
        else:
            device_type = _device_type_of(placement)
            current_rack_id = device.rack_id
            current_position = device.position
            current_face = device.face
            identity_label = device.name or str(device)
        u_height = _u_height(device_type)
        full_depth = _is_full_depth(device_type)

        if placement.kind == DesignPlacementKindChoices.KIND_REMOVE:
            # Flag the identity's current slot (only if it lives in this rack).
            if current_rack_id != rack.pk:
                continue
            _append(
                _slot(
                    u_position=Decimal(current_position) if current_position is not None else None,
                    u_height=u_height,
                    face=_normalize_face(current_face),
                    label=identity_label,
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
        if current_rack_id == rack.pk and current_position is not None:
            _append(
                _slot(
                    u_position=Decimal(current_position),
                    u_height=u_height,
                    face=_normalize_face(current_face),
                    label=identity_label,
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
                    label=identity_label,
                    state=ProjectedSlotState.MOVE_IN,
                    device=device,
                    device_type=device_type,
                    placement=placement,
                    # The plan's new identity for the device (user ruling
                    # 2026-07-10): the tile SHOWS the assigned name; the
                    # identity `label` above stays the device's real name (or,
                    # for an ancestor-planned identity, its settled name).
                    display_label=placement.proposed_name or None,
                ),
                full_depth=full_depth,
            )

    # Displacement marking (spec §3/§4.3, parity ruling 2026-07-09): per-face
    # post-pass so the read-only elevation and the editor's on-load render
    # apply the SAME displaced treatment as the editor's live gesture flow.
    _mark_displaced(front)
    _mark_displaced(rear)
    # The BAY layer, in the order the layers compose (G1): reality, minus the
    # parts an ancestor already invalidated; bay templates for a planned chassis;
    # the inherited blades; then this design's own.
    _attach_bays((front, rear, non_racked), baseline.suppressed_device_ids)
    _attach_planned_chassis_bays((front, rear, non_racked))
    _overlay_planned_blades(design, (front, rear, non_racked), baseline=baseline)
    # One pass for the whole elevation, before anything reads a device's power.
    _prefetch_power((front, rear, non_racked))

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
        # Whatever the replay could not do, in the order it hit it: the §9.2
        # chain refusal first, then per-slot problems from ``emit``.
        conflicts=baseline.conflicts,
    )
    # Power projection (docs/power-projection-spec.md): fills per-slot draw and
    # the rack-level summary over the planned world just built above.
    elevation.power = _project_power(elevation, **_power_config())
    # Per-PDU/bank distribution (docs/pdu-distribution-spec.md): computed in
    # "builtin" (native, zero-config) or "script" mode (else None -> the
    # frontend keeps the per-device heatmap). A broken builtin/script degrades
    # to None, never erroring the projection.
    from .distribution import generate_distribution_status
    dist, dist_status = generate_distribution_status(elevation)
    elevation.power["distribution"] = dist
    # WHY there is (or is not) a distribution, so the editor can say so instead
    # of rendering an empty strip -- a failing script must never look like a
    # rack that simply has no PDUs (user 2026-08-28).
    elevation.power["distribution_status"] = dist_status
    return elevation
