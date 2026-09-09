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
placement this version cannot handle (a bay target, see below) is reported as
an explicit problem, not quietly dropped from the plan.

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

* **Bay placements** (a blade going into a chassis, real or planned) are out
  of scope: reported as an explicit problem, never silently skipped or
  applied incorrectly. A later phase teaches apply about device bays.
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

from dcim.choices import DeviceFaceChoices
from dcim.models import Device
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from netbox.plugins import get_plugin_config

from . import projection
from .choices import DesignPlacementKindChoices, DesignStatusChoices
from .models import DesignApply, DesignPlacement

logger = logging.getLogger("netbox_rack_design.apply")

__all__ = ("ApplyResult", "CreatedDevice", "UpdatedDevice", "RemovedDevice",
           "DeletedDevice", "RevertedDevice", "plan", "run")


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
class ApplyResult:
    """The full answer :func:`plan`/:func:`run` give: intended actions + problems.

    ``problems`` is the ONLY thing that matters for "can this be applied": any
    entry there means :func:`run` refuses and writes nothing. The other lists
    are the intended (or, after :func:`run`, the actual) creates/updates/
    status-flags/prunes/reverts, always populated by :func:`plan` regardless
    of whether problems exist elsewhere in the design -- a planner sees the
    whole picture, not just the first failure.
    """

    problems: list = field(default_factory=list)
    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    reverted: list = field(default_factory=list)

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


def _is_bay_placement(pl):
    """A blade placement -- out of scope for this version of apply (see module docstring)."""
    return bool(
        pl.target_bay_id or pl.parent_placement_id or pl.base_parent_placement_id
        or pl.target_bay_name
    )


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
            "device__rack", "target_rack", "device_type", "device_role", "tenant",
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

    # --- 5. bay placements are out of scope this version --------------------
    workable = []
    for pl in placements:
        if _is_bay_placement(pl):
            result.problems.append(
                f"{_placement_label(pl)} is a blade placement; blade placements "
                f"cannot be applied yet."
            )
        else:
            workable.append(pl)

    # --- one scan covering BOTH occupancy and site-wide name conflicts ------
    target_rack_ids = {
        pl.target_rack_id for pl in workable
        if pl.kind in (DesignPlacementKindChoices.KIND_ADD, DesignPlacementKindChoices.KIND_MOVE)
        and pl.target_rack_id and pl.target_position is not None
    }
    devices_in_scope = list(
        Device.objects.filter(Q(site_id=design.site_id) | Q(rack_id__in=target_rack_ids))
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

            if pl.target_position is not None:
                u_height = _u_height(device_type)
                full_depth = _is_full_depth(device_type)
                cells = _footprint(pl.target_rack_id, pl.target_position, u_height, pl.target_face, full_depth)
                conflict = next((occupied[c] for c in cells if c in occupied), None)
                if conflict is not None:
                    face_label = pl.target_face or DeviceFaceChoices.FACE_FRONT
                    result.problems.append(
                        f"U{projection._fmt_u(pl.target_position)} {face_label} in rack "
                        f"{pl.target_rack} is occupied by {conflict.name}."
                    )
                    blocked = True

            if name in existing_names or name in assigned_this_run:
                result.problems.append(
                    f"The name {name} is already used by another device in this site."
                )
                blocked = True
            else:
                assigned_this_run.add(name)

            if blocked:
                continue

            role = pl.resolved_role()
            tenant = pl.resolved_tenant()
            row = apply_by_placement.get(pl.pk)
            if row is None:
                result.created.append(CreatedDevice(
                    placement=pl, name=name, device_type=device_type, role=role,
                    tenant=tenant, rack=pl.target_rack, position=pl.target_position,
                    face=pl.target_face, status=planned_status,
                ))
            elif row.device_id is None:
                result.created.append(CreatedDevice(
                    placement=pl, name=name, device_type=device_type, role=role,
                    tenant=tenant, rack=pl.target_rack, position=pl.target_position,
                    face=pl.target_face, status=planned_status, recreated=True,
                    apply_row=row,
                ))
            else:
                changes = _diff_device(
                    row.device, name=name, rack=pl.target_rack, position=pl.target_position,
                    face=pl.target_face, role=role, tenant=tenant, status=planned_status,
                )
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

    # --- 4. dcim permissions, checked for exactly what the run requires -----
    need_add = bool(result.created)
    if need_add and not user.has_perm("dcim.add_device"):
        result.problems.append(
            f"You do not have permission to create devices in site {design.site}."
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
                result.problems.append(
                    f"You do not have permission to modify device {name} in site {design.site}."
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
                result.problems.append(
                    f"You do not have permission to delete device {name} in site {design.site}."
                )

    return result


# --- execution -----------------------------------------------------------

def run(design, user):
    """Apply ``design``: :func:`plan`, refuse on any problem, else write it all
    inside one transaction. See the module docstring for the all-or-nothing
    contract."""
    with transaction.atomic():
        result = plan(design, user)
        if not result.ok:
            return result
        _execute(design, user, result)
        return result


def _execute(design, user, result):
    removal_status = get_plugin_config("netbox_rack_design", "removal_status")

    for entry in result.created:
        device = Device(
            name=entry.name, device_type=entry.device_type, role=entry.role,
            tenant=entry.tenant, site=design.site, rack=entry.rack,
            position=entry.position, face=entry.face or "", status=entry.status,
        )
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

    for entry in result.updated:
        device = entry.device
        for attr, value in entry.changes.items():
            setattr(device, attr, value)
        device.full_clean()
        device.save()
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
