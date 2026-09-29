"""
Apply engine for NetBox Rack Design.

Applying a design is the step between "planned" and "physically real": for
every ``add``/``move`` placement it creates a **planned** ``dcim.Device`` at
the target slot -- reserving that slot so nothing else can land there, and
giving planned cabling something to attach to -- and for every ``remove``
placement it flags the real device's status. The design's own ``status`` is
never touched here: ``implemented`` is stamped later by external automation
once the hardware has actually moved, and this module has no opinion on when
that happens.

The real device a ``move`` acts on is left completely untouched -- still
``active``, still in its old slot -- for exactly as long as the window
between apply and the physical move lasts. During that window the rack
legitimately shows BOTH the real device and its planned successor; that
double-vision is the intended UI, not a bug for a later phase to fix.

THE TWO ENTRY POINTS, AND WHY THEY ARE SEPARATE
------------------------------------------------------------------------------

:func:`plan` is a pure inspection: it walks the design's placements, asks
"what would applying this design do, and what would stop it", and returns
that answer as an :class:`ApplyResult` -- **without writing anything**. It is
safe to call from a preview, a dry-run button, or a test, as often as wanted.

:func:`run` calls :func:`plan` and, if it found ANY problem, returns that same
result untouched -- nothing is written. Otherwise it performs every create /
update / status-flag / prune / revert :func:`plan` described, all inside ONE
database transaction. There is deliberately no partial "applied 7 of 12"
state: either the whole design's worth of changes lands, or none of it does,
so DCIM can never disagree with what the design's own ``DesignApply`` rows
say happened.

THE FAILURE PHILOSOPHY
------------------------------------------------------------------------------

Every obstacle :func:`plan` finds is collected -- never raised -- so a
planner sees every problem in one pass instead of fixing them one at a time
across repeated attempts (mirrors the batched-conflict style of
``projection.py``'s own refusals). Each problem is a plain sentence naming
the object and the obstacle ("U36 front in rack 0103 is occupied by
srv-09."), never a stack trace or a model repr, and never a silent skip: a
placement this version cannot handle is reported as an explicit problem, not
quietly dropped from the plan.

Slot-overlap and device-name validation are still ultimately NetBox's own:
:func:`run` calls ``full_clean()`` on every device it writes, so a bad device
is rejected by core's own rules rather than a bare database error. What
:func:`plan` computes ahead of that (one query for the target racks' real
devices, one for site-wide names, combined into a single scan) exists so
those SAME two problems can be reported to a planner *before* anything is
written, in the friendly sentence form above -- not to replace
``full_clean()`` as the authority. A failure ``full_clean()`` catches that
:func:`plan` did not anticipate still rolls back the whole transaction (see
"all-or-nothing" below); it is simply reported as a raised exception rather
than a collected problem, since :func:`plan` never claimed to be exhaustive.

WHAT THIS VERSION DELIBERATELY DOES NOT DO
------------------------------------------------------------------------------

* **Blades** are applied like any other add or move, into a device bay
  instead of a rack slot: a real chassis's bay, a bay of a chassis planned in
  the same design (created by the same run, so blades are written after every
  rack-level device), or one an ancestor design's apply already built. An
  occupied or missing bay is a problem, never a silent skip.
* **No REST action, no UI, no projection change** -- this is the engine only;
  later phases wire a button and an API action onto it.
* **Not cable-aware.** Deleting a planned device (the cleanup path below) may
  destroy cabling someone spent time on. That is the one irreversible step in
  the whole flow, which is exactly why every deletion is always reported,
  never silent.
* **Per-object ADD permission is approximated.** NetBox's own generic views
  resolve this the same way core does: an object-level ``add`` constraint can
  only be verified AFTER the row exists (there is no pk to filter on before
  save), so core creates first and rolls back if the saved row fails
  ``queryset.restrict(user, 'add')``. This module instead needs to REPORT the
  problem before writing anything, so it checks blanket ``dcim.add_device``
  only (does the user have that permission for *some* objects) rather than
  scoping it to the destination site/tenant. ``change``/``delete`` -- which
  act on devices that already exist -- ARE checked per-object, via
  ``Device.objects.restrict(user, ...)``, so a site/tenant-constrained user is
  correctly refused there.

PLANNED RACKS
------------------------------------------------------------------------------

A placement may target a ``PlannedRack`` instead of a real ``dcim.Rack``
(PLAN-templates.md T1.2/T1.6) -- a rack that does not exist in DCIM yet.
Applying such a placement first turns the ``PlannedRack`` into a real rack:
adopting one that already matches its ``(location, name)`` identity (D4), or
creating one if none does, and either way stamping ``PlannedRack.
realized_rack`` (D7) so every design that still references it derefs to the
real rack from then on -- the row itself is never deleted. On adoption the
real rack's own attributes win silently (D5): a plan drawn for 42U that turns
out to already exist as a 47U rack places its devices against the real 47U,
no reconciliation, no warning. Every planned rack a design's workable
placements touch is resolved this way BEFORE any device is written -- see
``_resolve_planned_racks``/``_realize_planned_racks`` -- so a rack that
cannot be resolved aborts the whole apply cleanly rather than leaving some
devices placed and others not. A rack created this way is never deleted by
any path in this module, cancel/undo included -- see
``_realize_planned_racks``'s docstring.

IDEMPOTENCY
------------------------------------------------------------------------------

Pressing apply twice must be a no-op the second time. Each placement's
``DesignApply`` row (one per placement, enforced by a database constraint) is
the find/create/update key: found with a device -> compared and only the
drifted fields are written back; found without one (someone deleted the
device by hand) -> recreated, and the result says so; not found at all ->
created fresh. A ``DesignApply`` row whose ``placement`` has gone null (the
placement itself was deleted) is cleanup, not a placement to apply -- see
:func:`_execute` -- and is distinguished from a create-orphan vs a
removal-orphan purely by whether ``prior_device_status`` is set, since that
field is otherwise only ever written for a removal.

QUERY BUDGET
------------------------------------------------------------------------------

:func:`plan` is written so its query count depends on how many DESIGNS,
RACKS and REAL DEVICES are involved -- never on how many PLACEMENTS the
design being applied has. One query fetches every existing ``DesignApply``
row for the design (idempotency), one fetches this design's own placements,
one scans the union of "every real device in the target racks" and "every
real device in the design's site" (occupancy + name conflicts, in a single
pass), one (only when there is a baseline chain) checks every ancestor for
unapplied placements in one batched ``Exists()`` subquery, and the
``change``/``delete`` permission checks add one more query each. See
``tests/test_apply.py``'s query-count test for the proof.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from dcim.choices import DeviceFaceChoices, RackStatusChoices
from dcim.models import Device, Rack
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from netbox.plugins import get_plugin_config

from . import planning_fields, projection
from .choices import DesignPlacementKindChoices, DesignStatusChoices
from .compat import custom_fields_for
from .models import DesignApply, DesignPlacement, PlannedRack

logger = logging.getLogger("netbox_rack_design.apply")

__all__ = ("ApplyResult", "CreatedDevice", "UpdatedDevice", "RemovedDevice",
           "DeletedDevice", "RevertedDevice", "ResolvedRack", "plan", "run")


# --- result shapes -----------------------------------------------------------

@dataclass
class CreatedDevice:
    """One 'add'/'move' placement that will get (or got) a new planned device.

    ``device`` is ``None`` in a :func:`plan` result (nothing has been written
    yet) and is filled in by :func:`run` once the device actually exists.
    ``apply_row`` is the existing device-less ``DesignApply`` row when
    ``recreated`` is True, else ``None`` (a fresh row is created).
    """

    placement: object
    name: str
    device_type: object
    role: object
    tenant: object
    rack: object
    position: object
    face: str
    status: str
    recreated: bool = False
    apply_row: object = None
    device: object = None
    # The device's custom fields as the design plans them (planning fields
    # bound to device custom fields; a move also carries the real device's
    # own), and planning values aimed at native attributes.
    custom_field_data: dict = field(default_factory=dict)
    native: dict = field(default_factory=dict)
    # A blade goes into a bay rather than a rack slot, by one of the three
    # routes models.DesignPlacement documents: a real ``bay``; a chassis
    # ``chassis_placement`` planned in this design (created by this same run,
    # or by an earlier one); or a ``chassis_device`` an ancestor design's
    # apply already created. ``bay_name`` names the bay in the latter two.
    blade: bool = False
    bay: object = None
    chassis_placement: object = None
    chassis_device: object = None
    bay_name: str = ""
    where: str = ""
    site: object = None


@dataclass
class UpdatedDevice:
    """An existing planned device whose attributes drifted from the plan."""

    placement: object
    device: object
    changes: dict
    apply_row: object = None


@dataclass
class RemovedDevice:
    """A 'remove' placement's real device, newly flagged or drift-corrected.

    ``apply_row`` is set only for ``corrected`` (the row already existed and
    is just being touched again); a brand-new removal creates its own row.
    """

    placement: object
    device: object
    prior_status: str
    corrected: bool = False
    apply_row: object = None


@dataclass
class DeletedDevice:
    """A planned device deleted because its placement no longer exists.

    The only irreversible step in the whole flow -- always reported.
    """

    design_title: str
    device_name: str
    apply_row: object
    device: object = None  # None once run() has actually deleted it


@dataclass
class RevertedDevice:
    """A removal undone because its placement no longer exists."""

    device_name: str
    prior_status: str
    apply_row: object
    device: object = None


@dataclass
class CreatedFeed:
    """One planned feed (``DesignPowerFeed``) that Apply turns into a real
    ``dcim.PowerFeed`` -- or, on a re-apply, finds already there and reuses.

    ``rack`` is ``None`` in a :func:`plan` result while its ``PlannedRack``
    has no real rack yet; :func:`run` fills it once the rack is realized.
    ``feed`` is the ``dcim.PowerFeed`` itself once :func:`run` has it.
    """

    planned_feed: object
    power_panel: object
    rack: object
    existing: object = None
    feed: object = None


@dataclass
class ResolvedRack:
    """One ``PlannedRack`` this design's workable placements target (T1.6,
    PLAN-templates.md D4/D5/D7): apply must turn it into a real ``dcim.Rack``
    -- by adopting one that already matches ``(location, name)``, or by
    creating one -- before any device can be placed into it.

    ``created`` distinguishes the two outcomes purely for reporting; apply's
    own placement machinery treats them identically once ``rack`` is filled
    in. Mirrors :class:`CreatedDevice`'s own ``device=None``-until-``run()``
    shape: :func:`plan` can already tell (via a read-only lookup) whether an
    adoption will happen, so ``rack``/``created`` are set then for an
    adoption, but a to-be-created rack's ``rack`` stays ``None`` until
    :func:`run` actually writes it -- ``plan`` must perform no writes.
    """

    planned_rack: object
    rack: object = None
    created: bool = False


@dataclass
class ApplyResult:
    """The full answer :func:`plan`/:func:`run` give: intended actions + problems.

    ``problems`` is the ONLY thing that matters for "can this be applied": any
    entry there means :func:`run` refuses and writes nothing. The other lists
    are the intended (or, after :func:`run`, the actual) creates/updates/
    status-flags/prunes/reverts, always populated by :func:`plan` regardless
    of whether problems exist elsewhere in the design -- a planner sees the
    whole picture, not just the first failure. ``resolved_racks`` is the T1.6
    addition: one entry per distinct ``PlannedRack`` this design's workable
    placements target, reporting whether it will be (or was) adopted or
    created.
    """

    problems: list = field(default_factory=list)
    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    reverted: list = field(default_factory=list)
    resolved_racks: list = field(default_factory=list)
    feeds: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.problems


# --- small helpers ------------------------------------------------------------

def _u_height(device_type):
    if device_type is not None and device_type.u_height:
        return Decimal(device_type.u_height)
    return Decimal(1)


def _is_full_depth(device_type):
    return bool(device_type is not None and device_type.is_full_depth)


def _footprint(rack_id, position, u_height, face, full_depth):
    """The set of (rack_id, unit, face) cells a device occupies."""
    faces = (
        (DeviceFaceChoices.FACE_FRONT, DeviceFaceChoices.FACE_REAR)
        if full_depth else (face or DeviceFaceChoices.FACE_FRONT,)
    )
    start = int(position)
    end = int(position + u_height)
    return {(rack_id, u, f) for f in faces for u in range(start, end)}


def _find_realized_or_adopt(planned):
    """Read-only D4 lookup: the ``dcim.Rack`` ``planned`` already resolves to,
    WITHOUT creating anything -- ``None`` means apply will need to create one.

    Trusts ``realized_rack`` only after re-confirming the row it points at
    still exists: a prior apply may have realized this planned rack and then
    someone deleted that rack in DCIM since. When that happens (or the rack
    was never realized at all) this falls through to the same
    ``(location, name)`` lookup a first-time resolution would use -- D4 made
    that pair unique specifically so this lookup can never be ambiguous.
    Used identically by :func:`plan` (a pure read, safe to call there) and by
    :func:`_execute`'s actual create-or-adopt step below.
    """
    if planned.realized_rack_id is not None:
        rack = Rack.objects.filter(pk=planned.realized_rack_id).first()
        if rack is not None:
            return rack
    return Rack.objects.filter(location_id=planned.location_id, name=planned.name).first()


def _resolve_planned_racks(placements):
    """The read-only half of T1.6: for every DISTINCT ``PlannedRack`` a
    workable 'add'/'move' placement targets, look up (never create) the real
    rack it would resolve to. Returns ``(resolved_racks, rack_by_planned_id)``
    -- the former is the ``ResolvedRack`` list :func:`plan` reports (a
    to-be-created rack's own ``.rack`` stays ``None``, since plan() must not
    write one), the latter maps ``planned_rack_id -> dcim.Rack or None`` for
    the occupancy scan and per-placement rack assignment below.

    One query per distinct planned rack referenced, not per placement -- see
    the module docstring's QUERY BUDGET section; a design with no planned-rack
    placements at all pays nothing extra.
    """
    planned_ids = {
        pl.target_planned_rack_id for pl in placements
        if pl.kind in (DesignPlacementKindChoices.KIND_ADD, DesignPlacementKindChoices.KIND_MOVE)
        and pl.target_planned_rack_id
    }
    resolved_racks = []
    rack_by_planned_id = {}
    if not planned_ids:
        return resolved_racks, rack_by_planned_id

    planned_racks = PlannedRack.objects.filter(pk__in=planned_ids).select_related("location")
    for planned in planned_racks:
        rack = _find_realized_or_adopt(planned)
        rack_by_planned_id[planned.pk] = rack
        resolved_racks.append(ResolvedRack(planned_rack=planned, rack=rack, created=rack is None))
    return resolved_racks, rack_by_planned_id


def _effective_target_rack(pl, rack_by_planned_id):
    """The ``dcim.Rack`` a placement's footprint should be checked against,
    whichever kind of destination it names (T1.6): the real rack directly, or
    -- for a placement targeting a still-planned rack -- whatever
    :func:`_resolve_planned_racks` already resolved it to. ``None`` means the
    rack does not exist yet and apply will create it, in which case there is
    by definition nothing occupying it to conflict with.
    """
    if pl.target_rack_id:
        return pl.target_rack
    if pl.target_planned_rack_id:
        return rack_by_planned_id.get(pl.target_planned_rack_id)
    return None


def _is_bay_placement(pl):
    """A blade placement -- out of scope for this version of apply (see module docstring)."""
    return bool(
        pl.target_bay_id or pl.parent_placement_id or pl.base_parent_placement_id
        or pl.target_bay_name
    )


def _chassis_bay(chassis, bay_name):
    return chassis.devicebays.filter(name=bay_name).select_related("installed_device").first()


def _resolve_blade_target(pl, name, rows_by_placement, own_planned_ids):
    """Where a blade goes, as :class:`CreatedDevice` keyword arguments, plus a
    problem sentence when it cannot go there (``None`` otherwise).

    The bay must exist and be free. "Free" counts only a device OTHER than
    this placement's own earlier planned blade: re-applying must find its own
    blade in the bay, not a conflict.
    """
    own_row = rows_by_placement.get(pl.pk)
    own_device_id = own_row.device_id if own_row else None

    def occupied(bay):
        return bay.installed_device_id and bay.installed_device_id != own_device_id

    if pl.target_bay_id:
        bay = pl.target_bay
        chassis = bay.device
        target = {"blade": True, "bay": bay, "where": f"{chassis} · {bay.name}",
                  "site": chassis.site}
        if occupied(bay):
            return target, (f"{name} goes into bay {bay.name} of {chassis}, which is "
                            f"occupied by {bay.installed_device}.")
        return target, None

    bay_name = pl.target_bay_name
    if pl.parent_placement_id:
        chassis_pl = pl.parent_placement
        chassis_name = _target_name(chassis_pl)
        target = {"blade": True, "chassis_placement": chassis_pl, "bay_name": bay_name,
                  "where": f"{chassis_name} · {bay_name}", "site": chassis_pl.site}
        chassis_row = rows_by_placement.get(chassis_pl.pk)
        chassis = chassis_row.device if chassis_row and chassis_row.device_id else None
    else:
        base = pl.base_parent_placement
        chassis_row = (DesignApply.objects.filter(placement=base, device__isnull=False)
                       .select_related("device").first())
        if chassis_row is None:
            return {}, (f"{name} goes into {_target_name(base)}, which {base.design} "
                        f"has not applied yet. Apply that design first.")
        chassis = chassis_row.device
        target = {"blade": True, "chassis_device": chassis, "bay_name": bay_name,
                  "where": f"{chassis} · {bay_name}", "site": chassis.site}

    if chassis is not None:
        bay = _chassis_bay(chassis, bay_name)
        if bay is None:
            return target, f"{name} goes into bay {bay_name}, which {chassis} does not have."
        if occupied(bay):
            return target, (f"{name} goes into bay {bay_name} of {chassis}, which is "
                            f"occupied by {bay.installed_device}.")
    return target, None


def _blade_bay_now(target, rows_by_placement):
    """The DeviceBay a blade belongs in, when its chassis already exists."""
    if target.get("bay") is not None:
        return target["bay"]
    chassis = target.get("chassis_device")
    if chassis is None and target.get("chassis_placement") is not None:
        row = rows_by_placement.get(target["chassis_placement"].pk)
        chassis = row.device if row and row.device_id else None
    return _chassis_bay(chassis, target["bay_name"]) if chassis is not None else None


def _blade_bay(entry, device_by_placement):
    """The DeviceBay a blade is installed into, resolved at write time."""
    if entry.bay is not None:
        return entry.bay
    chassis = entry.chassis_device
    if chassis is None:
        chassis = device_by_placement.get(entry.chassis_placement.pk)
    if chassis is None:
        row = (DesignApply.objects.filter(placement=entry.chassis_placement, device__isnull=False)
               .select_related("device").first())
        chassis = row.device if row else None
    bay = _chassis_bay(chassis, entry.bay_name) if chassis is not None else None
    if bay is None:
        raise ValidationError(f"{entry.name}: bay {entry.bay_name} not found on its chassis.")
    return bay


def _placement_label(pl):
    if pl.device_id:
        return pl.device.name
    if pl.proposed_name:
        return pl.proposed_name
    if pl.base_placement_id:
        return str(pl.base_placement)
    return pl.stale_device_name or f"placement {pl.pk}"


def _target_name(pl):
    """The name the placement's planned/moved device will carry (see naming.py)."""
    if pl.kind == DesignPlacementKindChoices.KIND_ADD:
        return pl.proposed_name
    # A 'move': a rename uses proposed_name; a keep-name move gets the same
    # "<design title>-<real name>" decoration the elevation shows, so the
    # planned device's name is unique while the real device still holds the
    # original (see naming.py's module docstring).
    if pl.proposed_name:
        return pl.proposed_name
    return f"{pl.design.title}-{pl.device.name}"


def _first_unapplied_ancestor(chain):
    """The oldest ancestor in ``chain`` that still has an un-applied placement.

    ONE query total regardless of chain depth or placement count: a single
    batched ``Exists()`` subquery, not ``design.baseline_chain()``-per-row and
    not one apply-lookup per placement. Bay placements are excluded -- they
    are out of scope for apply entirely (see module docstring) and would
    otherwise permanently and falsely block every descendant of any ancestor
    that happens to carry one.
    """
    if not chain:
        return None
    ancestor_ids = [a.pk for a in chain]
    bay_q = (
        Q(target_bay__isnull=False) | Q(parent_placement__isnull=False)
        | Q(base_parent_placement__isnull=False) | ~Q(target_bay_name="")
    )
    unapplied_design_ids = set(
        DesignPlacement.objects.filter(design_id__in=ancestor_ids, stale=False)
        .exclude(bay_q)
        .annotate(has_apply=Exists(DesignApply.objects.filter(placement_id=OuterRef("pk"))))
        .filter(has_apply=False)
        .values_list("design_id", flat=True)
    )
    if not unapplied_design_ids:
        return None
    for ancestor in chain:  # chain is oldest-first
        if ancestor.pk in unapplied_design_ids:
            return ancestor
    return None


def _diff_device(device, *, name, rack, position, face, role, tenant, status):
    """Only the fields that actually drifted -- see the module docstring's
    idempotency section: apply must write back nothing it does not have to."""
    changes = {}
    if device.name != name:
        changes["name"] = name
    if device.rack_id != (rack.pk if rack else None):
        changes["rack"] = rack
    if device.position != position:
        changes["position"] = position
    if (device.face or "") != (face or ""):
        changes["face"] = face
    role_id = role.pk if role else None
    if device.role_id != role_id:
        changes["role"] = role
    tenant_id = tenant.pk if tenant else None
    if device.tenant_id != tenant_id:
        changes["tenant"] = tenant
    if device.status != status:
        changes["status"] = status
    return changes


def _planned_custom_fields(pl):
    """The custom fields ``pl``'s planned device gets: for a move the real
    device's own, then every planning field the design sets on top."""
    data = {}
    if pl.kind == DesignPlacementKindChoices.KIND_MOVE and pl.device_id:
        data.update(pl.device.custom_field_data or {})
    data.update(planning_fields.planned_custom_field_data(pl.planning_data))
    return data


def _planning_diff(device, custom_field_data, native):
    """The planning-field part of an already-created device's drift."""
    changes = {}
    current = device.custom_field_data or {}
    if any(current.get(name) != value for name, value in custom_field_data.items()):
        changes["custom_field_data"] = {**current, **custom_field_data}
    for attr, value in native.items():
        if getattr(device, attr, None) != value:
            changes[attr] = value
    return changes


def _planning_field_problems(pl, label):
    """Planning values this placement sets that apply cannot write."""
    problems = []
    set_keys = {k for k, v in (pl.planning_data or {}).items() if v not in (None, "", [])}
    if not set_keys:
        return problems
    for spec in planning_fields.placement_field_schema():
        if spec["key"] not in set_keys:
            continue
        target = spec["target"]
        if not target:
            problems.append(
                f"{label} sets the planning field '{spec['label']}', which has no "
                f"target in the plugin configuration -- nothing to write it to."
            )
        elif target.startswith("cf.") and spec.get("cf") is None:
            problems.append(
                f"{label} sets the planning field '{spec['label']}', but no custom "
                f"field '{target[3:]}' exists on devices. Create it, or fix the "
                f"field's target in the plugin configuration."
            )
        elif not target.startswith("cf.") and (
            "." in target or target not in {f.name for f in Device._meta.concrete_fields}
        ):
            problems.append(
                f"{label} sets the planning field '{spec['label']}', whose target "
                f"'{target}' is not a device attribute apply can write."
            )
    return problems


# --- the pre-check ------------------------------------------------------------

def plan(design, user):
    """Pure inspection: what applying ``design`` would do, and what would stop it.

    Performs NO writes whatsoever -- see ``tests/test_apply.py``'s
    ``test_plan_performs_no_writes`` for the wrapped-in-a-transaction proof.
    """
    result = ApplyResult()
    planned_status = get_plugin_config("netbox_rack_design", "planned_status")
    removal_status = get_plugin_config("netbox_rack_design", "removal_status")

    # --- 1. approved -----------------------------------------------------
    if design.status != DesignStatusChoices.STATUS_APPROVED:
        result.problems.append(
            f"{design} is {design.get_status_display().lower()}, not approved: "
            f"only an approved design can be applied."
        )

    # --- 2. ancestors first ------------------------------------------------
    try:
        chain = design.baseline_chain()
    except ValueError:
        chain = []  # a broken lineage is reported by the chain check below instead
    unapplied_ancestor = _first_unapplied_ancestor(chain)
    if unapplied_ancestor is not None:
        result.problems.append(
            f"{unapplied_ancestor} has unapplied placements; apply it first."
        )

    # --- 3. the chain must resolve ------------------------------------------
    _, refusal = projection.resolve_baseline_chain(design)
    if refusal is not None:
        result.problems.append(refusal["detail"])

    # --- fetch this design's own placements + apply rows, ONE query each ----
    placements = list(
        design.placements.filter(stale=False)
        .select_related(
            "device", "device__role", "device__tenant", "device__device_type",
            "device__rack", "target_rack", "target_planned_rack",
            "target_planned_rack__location", "device_type", "device_role", "tenant",
            "base_placement",
        )
        .order_by("pk")
    )
    apply_rows = list(
        DesignApply.objects.filter(design=design)
        .select_related("device", "device__role", "device__tenant", "placement")
    )
    apply_by_placement = {row.placement_id: row for row in apply_rows if row.placement_id}
    orphan_rows = [row for row in apply_rows if row.placement_id is None]
    # Only THIS design's own add/move planned devices are excluded from the
    # occupancy/name scan below (a placement's own already-created device must
    # not be mistaken for a conflict with itself, or the second run would
    # never be idempotent). A 'remove' row's device is the REAL device it
    # flags -- still genuinely occupying its slot until the physical move
    # happens -- so it stays IN scope, never excluded.
    own_planned_ids = {
        row.device_id for row in apply_rows
        if row.device_id and row.placement_id
        and row.placement.kind in (
            DesignPlacementKindChoices.KIND_ADD, DesignPlacementKindChoices.KIND_MOVE,
        )
    }

    # --- 5. blades go into bays; everything else into rack slots -----------
    workable = list(placements)
    blade_rows_by_placement = {row.placement_id: row for row in apply_rows if row.placement_id}

    # --- T1.6: resolve every PlannedRack these placements target, BEFORE any
    # occupancy scan or device is planned/written, so a rack that cannot be
    # resolved never leaves a half-applied design (module docstring / D4/D7).
    # Read-only here -- see _find_realized_or_adopt's docstring -- the actual
    # create-or-adopt write happens in _execute() below.
    result.resolved_racks, rack_by_planned_id = _resolve_planned_racks(workable)

    # --- one scan covering BOTH occupancy and site-wide name conflicts ------
    target_rack_ids = {
        pl.target_rack_id for pl in workable
        if pl.kind in (DesignPlacementKindChoices.KIND_ADD, DesignPlacementKindChoices.KIND_MOVE)
        and pl.target_rack_id and pl.target_position is not None
    }
    # An ADOPTED planned rack already has real occupants that must be scanned
    # for conflicts exactly like any other real rack (T1.6) -- a rack that
    # will be freshly CREATED contributes nothing here, since it cannot
    # possibly have any devices in it yet.
    target_rack_ids |= {rack.pk for rack in rack_by_planned_id.values() if rack is not None}
    # M7: every device in ANY of the design's sites, not just a single
    # ``design.site`` (a multi-site design has more than one).
    devices_in_scope = list(
        Device.objects.filter(Q(site_id__in=design.sites.values_list("pk", flat=True)) | Q(rack_id__in=target_rack_ids))
        .exclude(pk__in=own_planned_ids)
        .select_related("device_type")
    )
    existing_names = {d.name for d in devices_in_scope}
    occupied = {}
    for d in devices_in_scope:
        if d.rack_id is not None and d.position is not None:
            for cell in _footprint(
                d.rack_id, d.position, _u_height(d.device_type), d.face,
                _is_full_depth(d.device_type),
            ):
                occupied.setdefault(cell, d)

    assigned_this_run = set()
    change_device_ids = set()
    delete_device_ids = set()

    for pl in workable:
        if pl.kind in (DesignPlacementKindChoices.KIND_ADD, DesignPlacementKindChoices.KIND_MOVE):
            device_type = pl.device_type if pl.kind == DesignPlacementKindChoices.KIND_ADD else pl.device.device_type
            name = _target_name(pl)
            blocked = False
            blade = _is_bay_placement(pl)
            bay_target = None
            if blade:
                bay_target, problem = _resolve_blade_target(
                    pl, name, blade_rows_by_placement, own_planned_ids)
                if problem:
                    result.problems.append(problem)
                    blocked = True
            # The real rack this placement's device will actually sit in --
            # either its real target directly, or (T1.6) whatever the planned
            # rack it targets was already resolved to above. None means the
            # rack itself does not exist yet and will be CREATED by run(), in
            # which case there is nothing yet to occupy it.
            target_rack = _effective_target_rack(pl, rack_by_planned_id)

            if pl.target_position is not None and target_rack is not None:
                u_height = _u_height(device_type)
                full_depth = _is_full_depth(device_type)
                cells = _footprint(target_rack.pk, pl.target_position, u_height, pl.target_face, full_depth)
                conflict = next((occupied[c] for c in cells if c in occupied), None)
                if conflict is not None:
                    face_label = pl.target_face or DeviceFaceChoices.FACE_FRONT
                    result.problems.append(
                        f"U{projection._fmt_u(pl.target_position)} {face_label} in rack "
                        f"{target_rack} is occupied by {conflict.name}."
                    )
                    blocked = True

            if name in existing_names or name in assigned_this_run:
                result.problems.append(
                    f"The name {name} is already used by another device in this site."
                )
                blocked = True
            else:
                assigned_this_run.add(name)

            # dcim.Device.role is required, but a placement may leave it unset
            # (an editor drop with the Role select still empty). That has to
            # be a blocker HERE, listed with everything else on the
            # confirmation page -- found any later, it is full_clean()
            # raising half-way through _execute(), which is a 500, not an
            # answer.
            role = pl.resolved_role()
            if role is None:
                result.problems.append(
                    f"{name} has no role, and NetBox requires one on every device. "
                    f"Set it on the placement -- the Role select in the editor "
                    f"before the drop, or the placement's own edit form -- and "
                    f"apply again."
                )
                blocked = True

            field_problems = _planning_field_problems(pl, name)
            if field_problems:
                result.problems.extend(field_problems)
                blocked = True

            if blocked:
                continue

            tenant = pl.resolved_tenant()
            custom_field_data = _planned_custom_fields(pl)
            native = planning_fields.native_target_values(pl.planning_data)
            blade_kwargs = {}
            if blade:
                blade_kwargs = dict(bay_target)
                target_rack = None
            row = apply_by_placement.get(pl.pk)
            if row is None:
                result.created.append(CreatedDevice(
                    placement=pl, name=name, device_type=device_type, role=role,
                    tenant=tenant, rack=target_rack, position=pl.target_position,
                    face=pl.target_face, status=planned_status,
                    custom_field_data=custom_field_data, native=native, **blade_kwargs,
                ))
            elif row.device_id is None:
                result.created.append(CreatedDevice(
                    placement=pl, name=name, device_type=device_type, role=role,
                    tenant=tenant, rack=target_rack, position=pl.target_position,
                    face=pl.target_face, status=planned_status, recreated=True,
                    apply_row=row, custom_field_data=custom_field_data, native=native,
                    **blade_kwargs,
                ))
            elif blade:
                device = row.device
                changes = _diff_device(
                    device, name=name, rack=device.rack, position=device.position,
                    face=device.face, role=role, tenant=tenant, status=planned_status,
                )
                changes.update(_planning_diff(
                    device, planning_fields.planned_custom_field_data(pl.planning_data), native,
                ))
                want_bay = _blade_bay_now(bay_target, blade_rows_by_placement)
                have_bay = getattr(device, "parent_bay", None)
                if want_bay is not None and (have_bay is None or have_bay.pk != want_bay.pk):
                    changes["parent_bay"] = want_bay
                if changes:
                    result.updated.append(UpdatedDevice(
                        placement=pl, device=device, changes=changes, apply_row=row,
                    ))
                    change_device_ids.add(row.device_id)
            else:
                changes = _diff_device(
                    row.device, name=name, rack=target_rack, position=pl.target_position,
                    face=pl.target_face, role=role, tenant=tenant, status=planned_status,
                )
                changes.update(_planning_diff(
                    row.device, planning_fields.planned_custom_field_data(pl.planning_data),
                    native,
                ))
                if changes:
                    result.updated.append(UpdatedDevice(
                        placement=pl, device=row.device, changes=changes, apply_row=row,
                    ))
                    change_device_ids.add(row.device_id)

        elif pl.kind == DesignPlacementKindChoices.KIND_REMOVE:
            if pl.device_id is None:
                continue  # stale rows are already excluded; belt and braces
            row = apply_by_placement.get(pl.pk)
            if row is None:
                result.removed.append(RemovedDevice(
                    placement=pl, device=pl.device, prior_status=pl.device.status,
                ))
                change_device_ids.add(pl.device_id)
            elif row.device_id is not None and row.device.status != removal_status:
                result.removed.append(RemovedDevice(
                    placement=pl, device=row.device, prior_status=row.prior_device_status,
                    corrected=True, apply_row=row,
                ))
                change_device_ids.add(row.device_id)
            # else: already flagged and undrifted -- no-op, nothing to report.

    # --- cleanup: apply rows whose placement is gone ------------------------
    for row in orphan_rows:
        if row.prior_device_status:
            # A removal orphan: restore the exact prior status. Reported even
            # when the device itself is already gone (device=None): the row
            # still needs pruning, and run() -- never plan() -- is what may
            # touch it.
            result.reverted.append(RevertedDevice(
                device_name=row.device_name, prior_status=row.prior_device_status,
                apply_row=row, device=row.device,
            ))
            if row.device_id is not None:
                change_device_ids.add(row.device_id)
        else:
            # A create orphan: the planned device is no longer wanted (or,
            # if it is already gone, just the bookkeeping row is).
            result.deleted.append(DeletedDevice(
                design_title=row.design_title, device_name=row.device_name,
                apply_row=row, device=row.device,
            ))
            if row.device_id is not None:
                delete_device_ids.add(row.device_id)

    # --- 3b. planned feeds become real ones ----------------------------------
    _plan_feeds(design, result, rack_by_planned_id)

    # --- 4. dcim permissions, checked for exactly what the run requires -----
    if any(e.existing is None for e in result.feeds) and not user.has_perm("dcim.add_powerfeed"):
        result.problems.append("You do not have permission to create power feeds.")
    if result.feeds and not user.has_perm("dcim.add_cable"):
        result.problems.append(
            "You do not have permission to create cables (to connect PDUs to their feeds).")
    # M7: the message names the DEVICE's own (target rack's) site, not the
    # design's -- a multi-site design's ``site`` property is None, and even
    # for a one-site design the rack's site is what actually matters. One
    # problem per created entry (mirrors the change/delete loops below),
    # falling back to the still-planned rack's location's site for an entry
    # whose real rack does not exist yet (a greenfield PlannedRack).
    blades = [e for e in result.created if e.blade]
    blades += [e for e in result.updated if "parent_bay" in e.changes]
    if blades and not user.has_perm("dcim.change_devicebay"):
        result.problems.append(
            "You do not have permission to change device bays (to install the "
            "planned blades).")
    if result.created and not user.has_perm("dcim.add_device"):
        for entry in result.created:
            if entry.blade:
                site = entry.site
            elif entry.rack is not None:
                site = entry.rack.site
            else:
                site = entry.placement.target_planned_rack.location.site
            result.problems.append(
                f"You do not have permission to create devices in site {site}."
            )
    if change_device_ids:
        allowed = set(
            Device.objects.restrict(user, "change")
            .filter(pk__in=change_device_ids).values_list("pk", flat=True)
        )
        missing = change_device_ids - allowed
        if missing:
            by_id = {
                entry.device.pk: entry.device
                for entry in (*result.updated, *result.removed, *result.reverted)
                if entry.device is not None
            }
            for device_id in missing:
                device = by_id.get(device_id)
                name = device.name if device else device_id
                # M7: the device's OWN site, not the design's.
                site = device.site if device else None
                result.problems.append(
                    f"You do not have permission to modify device {name} in site {site}."
                )
    if delete_device_ids:
        allowed = set(
            Device.objects.restrict(user, "delete")
            .filter(pk__in=delete_device_ids).values_list("pk", flat=True)
        )
        missing = delete_device_ids - allowed
        if missing:
            by_id = {entry.device.pk: entry for entry in result.deleted if entry.device is not None}
            for device_id in missing:
                entry = by_id.get(device_id)
                name = entry.device_name if entry else device_id
                # M7: the device's OWN site, not the design's.
                site = entry.device.site if entry and entry.device is not None else None
                result.problems.append(
                    f"You do not have permission to delete device {name} in site {site}."
                )

    # Last, and only once everything else is known: would NetBox accept the
    # custom fields of every device this apply is about to save?
    _check_custom_fields(result, removal_status)

    return result


# --- custom-field pre-check --------------------------------------------------

def _validation_messages(exc):
    """Flatten a ValidationError into readable one-liners. Field errors keep
    their field name; model-wide ones (custom fields land here) do not need
    one -- NetBox's own wording already names the field."""
    if hasattr(exc, "message_dict"):
        out = []
        for field_name, messages in exc.message_dict.items():
            for message in messages:
                out.append(message if field_name == "__all__" else f"{field_name}: {message}")
        return out
    return list(getattr(exc, "messages", [str(exc)]))


def _check_custom_fields(result, removal_status):
    """Report every device this apply would save whose custom fields NetBox
    would refuse -- before anything is written.

    NetBox validates custom fields on every save, including a save that only
    changes a device's status. So a removal flag on a device whose custom
    field already held a bad value in DCIM (a boolean in a text field, say)
    used to die in :func:`_execute` as a raw ValidationError 500. Checked here
    it becomes a line on the confirmation page naming the device and the
    field, and the planner fixes the device.

    Deliberately NOT a full ``full_clean()`` per device: that is ~14 queries
    each (FK lookups, uniqueness, NetBox's own rack checks), and the
    confirmation page runs :func:`plan` on every load. The field definitions
    are loaded ONCE and each value is validated in Python -- the query count
    stays flat however large the design (QueryBudgetTestCase). Anything
    rarer that still gets past plan() is caught by :func:`run` instead.
    """

    fields = custom_fields_for(Device)
    if not fields:
        return

    def check(label, data, *, existing):
        data = data or {}
        for cf in fields:
            value = data.get(cf.name)
            if value in (None, ""):
                if cf.required and cf.default in (None, ""):
                    result.problems.append(
                        f"{label} has no value for the required custom field "
                        f"'{cf.name}'." + (" Set it on the device itself, then apply again."
                                          if existing else "")
                    )
                continue
            try:
                cf.validate(value)
            except ValidationError as exc:
                for message in _validation_messages(exc):
                    if existing:
                        result.problems.append(
                            f"{label} cannot be saved as it stands in DCIM -- custom "
                            f"field '{cf.name}': {message} Fix it on the device "
                            f"itself, then apply again."
                        )
                    else:
                        result.problems.append(
                            f"{label} would be refused by NetBox -- custom field "
                            f"'{cf.name}': {message}"
                        )

    defaults = {cf.name: cf.default for cf in fields}
    for entry in result.created:
        check(entry.name, {**defaults, **entry.custom_field_data}, existing=False)
    for entry in result.updated:
        data = entry.changes.get("custom_field_data", entry.device.custom_field_data)
        check(entry.device.name, data, existing=True)
    for entry in result.removed:
        check(entry.device.name, entry.device.custom_field_data, existing=True)
    for entry in result.reverted:
        if entry.device is not None:
            check(entry.device_name, entry.device.custom_field_data, existing=True)


# --- planned feeds ------------------------------------------------------------

def _plan_feeds(design, result, rack_by_planned_id):
    """Work out which ``dcim.PowerFeed`` each of the design's planned feeds
    becomes, and on which power panel.

    A planned feed is part of the plan exactly like the rack and the PDUs it
    supplies, so Apply realizes it: a real feed, in 'planned' status, on its
    rack. ``dcim.PowerFeed`` cannot exist without a power panel, so each one
    needs one -- the panel recorded on the planned feed (the copy paths fill
    it from their source), else the site's panel when the site has exactly
    one. With neither, the feed is a blocker naming what to set, rather than
    a guess at which panel feeds the rack.

    A feed on a ``PlannedRack`` that no placement of this design builds is
    skipped: that rack is not being created, so neither is its supply.
    Idempotent: a feed already standing on that panel under that name (a
    previous apply) is reused, never duplicated. Read-only.
    """
    from dcim.models import PowerFeed, PowerPanel

    from .models import DesignPowerFeed

    panels_by_site = {}
    planned = (DesignPowerFeed.objects.filter(design=design)
               .select_related("power_panel", "rack__site", "planned_rack__location__site")
               .order_by("name"))
    for pf in planned:
        if pf.planned_rack_id:
            if pf.planned_rack_id not in rack_by_planned_id:
                continue
            rack = rack_by_planned_id[pf.planned_rack_id]   # None: created by this run
            site = pf.planned_rack.location.site
        else:
            rack = pf.rack
            site = rack.site

        panel = pf.power_panel
        if panel is None:
            if site.pk not in panels_by_site:
                panels_by_site[site.pk] = list(PowerPanel.objects.filter(site=site)[:2])
            candidates = panels_by_site[site.pk]
            if len(candidates) != 1:
                result.problems.append(
                    f"The planned feed {pf.name} has no power panel to hang on, and site "
                    f"{site} has {'no power panel' if not candidates else 'more than one'}. "
                    f"Set its power panel under Rack Design -> Planned Power Feeds, then "
                    f"apply again."
                )
                continue
            panel = candidates[0]

        existing = PowerFeed.objects.filter(power_panel=panel, name=pf.name).first()
        result.feeds.append(CreatedFeed(
            planned_feed=pf, power_panel=panel, rack=rack, existing=existing))


def _realize_feeds(result):
    """The write half of :func:`_plan_feeds`: create (or reuse) each feed on
    its now-real rack. Runs after the racks are realized and before any
    device is placed. Returns ``{DesignPowerFeed.pk: dcim.PowerFeed}``."""
    from dcim.choices import PowerFeedStatusChoices
    from dcim.models import PowerFeed

    rack_by_planned_id = {e.planned_rack.pk: e.rack for e in result.resolved_racks}
    by_planned = {}
    for entry in result.feeds:
        pf = entry.planned_feed
        if entry.rack is None and pf.planned_rack_id:
            entry.rack = rack_by_planned_id.get(pf.planned_rack_id)
        if entry.existing is not None:
            entry.feed = entry.existing
        else:
            feed = PowerFeed(
                power_panel=entry.power_panel, rack=entry.rack, name=pf.name,
                status=PowerFeedStatusChoices.STATUS_PLANNED,
                voltage=pf.voltage, amperage=pf.amperage,
                phase=pf.phase, supply=pf.supply,
            )
            feed.full_clean()
            feed.save()
            entry.feed = feed
        by_planned[pf.pk] = entry.feed
    return by_planned


def _cable_to_feed(device, feed):
    """Connect ``device``'s first free power port to ``feed`` with a planned
    cable -- what a bound PDU's binding means once both are real. A feed
    takes exactly one cable, so an already-cabled feed (a second PDU bound to
    the same leg) is left alone rather than failing the apply."""
    from dcim.choices import LinkStatusChoices
    from dcim.models import Cable

    if feed.cable_id:
        return
    port = device.powerports.filter(cable__isnull=True).order_by("name").first()
    if port is None:
        return
    cable = Cable(a_terminations=[port], b_terminations=[feed],
                  status=LinkStatusChoices.STATUS_PLANNED)
    cable.full_clean()
    cable.save()


# --- execution -----------------------------------------------------------

def run(design, user):
    """Apply ``design``: :func:`plan`, refuse on any problem, else write it all
    inside one transaction. See the module docstring for the all-or-nothing
    contract."""
    with transaction.atomic():
        result = plan(design, user)
        if not result.ok:
            return result
        try:
            _execute(design, user, result)
        except ValidationError as exc:
            # plan() checks everything it knows how to check; NetBox checks
            # more on save. If it still refuses a device half-way through,
            # undo every write this run made (all-or-nothing) and hand the
            # reason back as a problem -- the confirmation page shows it --
            # rather than letting it escape as a 500.
            transaction.set_rollback(True)
            for entry in result.created:
                entry.device = None     # rolled back: no such device exists
            result.problems.extend(
                f"NetBox refused to save a device, so nothing was applied: {message}"
                for message in _validation_messages(exc)
            )
        return result


def _realize_planned_racks(result):
    """The WRITE half of T1.6: adopt or create the real ``dcim.Rack`` for
    every ``ResolvedRack`` :func:`plan` identified, BEFORE any device is
    written -- a failure resolving a rack must abort cleanly rather than
    leaving some devices placed in a design where others could not be (module
    docstring's all-or-nothing contract; this runs inside the same
    transaction as everything else in :func:`_execute`).

    Re-runs :func:`_find_realized_or_adopt` rather than trusting
    ``entry.rack`` from :func:`plan`: :func:`plan` and :func:`run` execute
    back-to-back inside ONE transaction here, so nothing can have changed
    between them in practice, but re-resolving costs one query per distinct
    planned rack and keeps this function correct even if that ever stops
    being true (e.g. a future caller that plans once and executes later).

    ``PlannedRack.realized_rack`` (D7) is set to whichever rack it is -- the
    row itself is NEVER deleted, by design: it is what lets every design
    still referencing it deref to the real rack from now on (``resolve_rack``
    in models.py). A rack CREATED here is, symmetrically, never deleted by
    any path in this module -- not by the cancel/undo cleanup below (which
    only ever deletes a *planned device*, never the rack it stood in), and
    not by a later rollback: by the time anything could reconsider, the rack
    may already hold real hardware.
    """
    for entry in result.resolved_racks:
        planned = entry.planned_rack
        rack = _find_realized_or_adopt(planned)
        if rack is None:
            # 'planned', like every device this apply creates: the cabinet is
            # not standing in the hall yet. (NetBox's own default would make
            # it 'active'.) An ADOPTED rack is real and keeps its status (D5).
            rack = Rack(
                name=planned.name, location=planned.location,
                site=planned.location.site, u_height=planned.u_height,
                status=RackStatusChoices.STATUS_PLANNED,
            )
            rack.full_clean()
            rack.save()
            entry.created = True
        else:
            entry.created = False
        entry.rack = rack
        if planned.realized_rack_id != rack.pk:
            planned.realized_rack = rack
            planned.save(update_fields=["realized_rack"])


def _execute(design, user, result):
    removal_status = get_plugin_config("netbox_rack_design", "removal_status")

    _realize_planned_racks(result)
    if result.resolved_racks:
        # Some CreatedDevice/UpdatedDevice entries were built by plan() before
        # their planned rack existed, so their `rack`/`changes["rack"]` is
        # still None -- patch them now that _realize_planned_racks() above has
        # filled in the real rack. (An UpdatedDevice can only reference a
        # planned rack that a PRIOR apply already realized -- the device
        # being updated could not exist otherwise -- so this is a no-op there
        # in practice; handled anyway for robustness, never a duplicated
        # resolution.)
        rack_by_planned_id = {e.planned_rack.pk: e.rack for e in result.resolved_racks}
        for entry in result.created:
            if entry.rack is None and entry.placement.target_planned_rack_id:
                entry.rack = rack_by_planned_id[entry.placement.target_planned_rack_id]
        for entry in result.updated:
            if (
                "rack" in entry.changes and entry.changes["rack"] is None
                and entry.placement.target_planned_rack_id
            ):
                entry.changes["rack"] = rack_by_planned_id[entry.placement.target_planned_rack_id]

    feed_by_planned = _realize_feeds(result)

    from extras.models import CustomField

    cf_defaults = CustomField.objects.get_defaults_for_model(Device) if result.created else {}
    for entry in result.created:
        if entry.blade:
            continue            # into a bay, below -- its chassis may be created here
        device = Device(
            # M7: the device's site is its RACK's site (patched above to a
            # real rack for every entry by this point), not the design's.
            name=entry.name, device_type=entry.device_type, role=entry.role,
            tenant=entry.tenant, site=entry.rack.site, rack=entry.rack,
            position=entry.position, face=entry.face or "", status=entry.status,
        )
        # Defaults first (NetBox's own clean() wants every required field
        # present), then what the design plans on top.
        device.custom_field_data.update(cf_defaults)
        device.custom_field_data.update(entry.custom_field_data)
        for attr, value in entry.native.items():
            setattr(device, attr, value)
        device.full_clean()
        device.save()
        entry.device = device
        if entry.recreated:
            row = entry.apply_row
            row.device = device
        else:
            row = DesignApply(design=design, placement=entry.placement, device=device)
        row.applied_by = user
        row.save()

    # Blades: after every rack-level device exists, so a chassis planned in
    # this same design already has its bays (core builds them from the
    # device type's bay templates when the chassis is saved).
    device_by_placement = {e.placement.pk: e.device for e in result.created if e.device}
    for entry in result.created:
        if not entry.blade:
            continue
        bay = _blade_bay(entry, device_by_placement)
        chassis = bay.device
        device = Device(
            name=entry.name, device_type=entry.device_type, role=entry.role,
            tenant=entry.tenant, site=chassis.site, rack=chassis.rack,
            status=entry.status,
        )
        device.custom_field_data.update(cf_defaults)
        device.custom_field_data.update(entry.custom_field_data)
        for attr, value in entry.native.items():
            setattr(device, attr, value)
        device.full_clean()
        device.save()
        bay.installed_device = device
        bay.full_clean()
        bay.save()
        entry.device = device
        device_by_placement[entry.placement.pk] = device
        if entry.recreated:
            row = entry.apply_row
            row.device = device
        else:
            row = DesignApply(design=design, placement=entry.placement, device=device)
        row.applied_by = user
        row.save()

    # A PDU the plan bound to a planned feed gets cabled to the real one --
    # including a PDU an EARLIER apply already created (before its feed
    # existed), which is why this walks every applied device of the design
    # rather than just this run's creations. _cable_to_feed is idempotent.
    if feed_by_planned:
        for row in (DesignApply.objects.filter(design=design, device__isnull=False)
                    .select_related("placement", "device")):
            feed = feed_by_planned.get(row.placement.planned_power_feed_id)
            if feed is not None:
                _cable_to_feed(row.device, feed)

    for entry in result.updated:
        device = entry.device
        new_bay = entry.changes.get("parent_bay")
        for attr, value in entry.changes.items():
            if attr != "parent_bay":
                setattr(device, attr, value)
        device.full_clean()
        device.save()
        if new_bay is not None:
            old_bay = getattr(device, "parent_bay", None)
            if old_bay is not None and old_bay.pk != new_bay.pk:
                old_bay.installed_device = None
                old_bay.save()
            new_bay.installed_device = device
            new_bay.full_clean()
            new_bay.save()
        row = entry.apply_row
        row.applied_by = user
        row.save()

    for entry in result.removed:
        device = entry.device
        device.status = removal_status
        device.full_clean()
        device.save()
        if entry.corrected:
            row = entry.apply_row
            # prior_device_status is left untouched: it must keep recording the
            # status from BEFORE the very first removal, not this drift.
        else:
            row = DesignApply(
                design=design, placement=entry.placement, device=device,
                prior_device_status=entry.prior_status,
            )
        row.applied_by = user
        row.save()

    for entry in result.deleted:
        device = entry.device
        entry.apply_row.delete()
        if device is not None:
            device.delete()
        entry.device = None

    for entry in result.reverted:
        device = entry.device
        if device is not None:
            device.status = entry.prior_status
            device.full_clean()
            device.save()
        entry.apply_row.delete()
        entry.device = None
