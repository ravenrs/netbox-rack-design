"""
Execution-plan simulation (PLAN-execution-steps.md Sec. 4).

A *plan* is an ordered list of steps, each a list of placement ids. Step N is
simulated CUMULATIVELY: the racks as they stand once steps 1..N are done, built
with ``projection.project_rack(design, rack, only_placements=<ids of 1..N>)``.
Nothing is written. The caller (the ``simulate-steps`` API action) supplies the
proposed order, which may differ from what is saved on the placements.

Rules the simulation applies
----------------------------
* Every listed id belongs to the design and is listed once. A blade (a placement
  with ``parent_placement`` / ``base_parent_placement``) is never listed: it
  follows the step of its chassis placement. A blade whose chassis was planned by
  an ANCESTOR design has no chassis step to follow, so it is part of every
  step's state (an ancestor is always fully applied).
* Placements in no step are "unscheduled" and part of no step's state.
* A step touches the target rack / planned rack of its placements, plus the
  CURRENT rack of the device a move / remove acts on (a move out of A changes
  A) and the rack of a real chassis a blade is installed into. Only touched
  racks are projected for that step.
"""

from collections import Counter

from dcim.models import PowerOutlet, Rack

from . import projection
from .choices import DesignPlacementKindChoices
from .distribution import removed_pdu_pks
from .models import DesignPlacement, PlannedRack, rack_key
from .projection import ProjectedSlotState, project_rack

__all__ = ("StepsValidationError", "auto_order", "simulate", "validate_steps")

# Slots that physically occupy their U (the same set the power pass counts).
_OCCUPYING = frozenset(
    (ProjectedSlotState.EXISTING, ProjectedSlotState.ADD, ProjectedSlotState.MOVE_IN)
)
_PLANNED_ENTRIES = (ProjectedSlotState.ADD, ProjectedSlotState.MOVE_IN)
# Banks the editor paints red (editor.css: nbx-rd-dist-critical / -overload).
_RED_BANK_STATES = ("critical", "overload")
# The orange chip (editor.css: nbx-rd-dist-warn; distribution._finalize_native).
_WARN_BANK_STATE = "warn"


class StepsValidationError(ValueError):
    """The proposed order is malformed. ``errors`` is a list of messages."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def validate_steps(design, steps):
    """
    Check a proposed order and return ``({pk: placement}, children_by_chassis,
    always_on_ids)``. Raises :class:`StepsValidationError` naming every problem.
    """
    errors = []
    seen = set()
    flat = []
    for step in steps:
        for pk in step:
            if pk in seen:
                errors.append(f"Placement {pk} is listed more than once.")
            seen.add(pk)
            flat.append(pk)

    placements = {
        p.pk: p
        for p in DesignPlacement.objects.filter(design=design, pk__in=set(flat))
        .select_related("device", "target_bay__device")
    }
    for pk in dict.fromkeys(flat):
        placement = placements.get(pk)
        if placement is None:
            errors.append(f"Placement {pk} does not belong to this design.")
        elif placement.parent_placement_id or placement.base_parent_placement_id:
            errors.append(
                f"Placement {pk} is a blade: it follows its chassis' step and "
                f"must not be listed."
            )
    if errors:
        raise StepsValidationError(list(dict.fromkeys(errors)))

    children = {}
    for child in DesignPlacement.objects.filter(
        design=design, parent_placement_id__in=placements
    ).values_list("pk", "parent_placement_id"):
        children.setdefault(child[1], []).append(child[0])
    always_on = list(
        DesignPlacement.objects.filter(design=design, base_parent_placement__isnull=False)
        .values_list("pk", flat=True)
    )
    return placements, children, always_on


def _touched(placement):
    """The racks one placement changes, as ``(kind, pk)`` pairs."""
    out = []
    if placement.target_rack_id:
        out.append(("r", placement.target_rack_id))
    if placement.target_planned_rack_id:
        out.append(("p", placement.target_planned_rack_id))
    if placement.device_id and placement.device.rack_id:
        out.append(("r", placement.device.rack_id))
    bay = placement.target_bay
    if bay is not None and bay.device.rack_id:
        out.append(("r", bay.device.rack_id))
    return out


def simulate(design, steps, *, from_step=None, to_step=None, racks=None):
    """
    Simulate ``steps`` (list of lists of placement ids). ``from_step`` /
    ``to_step`` (1-based, inclusive) limit which steps are COMPUTED -- the state
    of a step always includes every earlier one. ``racks`` (``(kind, pk)``
    pairs) limits the output to those racks, when a step touches them.

    Returns ``[{"index": N, "racks": {"<rack key>": {"power", "distribution",
    "distribution_status", "problems"}}}]``.
    """
    placements, children, always_on = validate_steps(design, steps)
    wanted = None if racks is None else set(racks)
    first = max(1, from_step or 1)
    last = min(len(steps), to_step or len(steps))

    rack_cache = {}

    def _rack(kind, pk):
        if (kind, pk) not in rack_cache:
            model = Rack if kind == "r" else PlannedRack
            rack_cache[(kind, pk)] = model.objects.filter(pk=pk).first()
        return rack_cache[(kind, pk)]

    result = []
    subset = set(always_on)
    for index, step in enumerate(steps, start=1):
        touched = {}
        for pk in step:
            subset.add(pk)
            subset.update(children.get(pk, ()))
            for pair in _touched(placements[pk]):
                touched[pair] = None
        if not first <= index <= last:
            continue
        entry = {"index": index, "racks": {}}
        for kind, pk in sorted(touched):
            if wanted is not None and (kind, pk) not in wanted:
                continue
            rack = _rack(kind, pk)
            if rack is None:
                continue
            elevation = project_rack(design, rack, only_placements=set(subset))
            key = rack_key(rack, None) if kind == "r" else rack_key(None, rack)
            power = elevation.power
            entry["racks"][key] = {
                "power": {k: v for k, v in power.items()
                          if k not in ("distribution", "distribution_status")},
                "distribution": power.get("distribution"),
                "distribution_status": power.get("distribution_status"),
                "problems": _problems(elevation),
            }
        result.append(entry)
    return result


# --- auto-order ---------------------------------------------------------------

_KIND_RANK = {
    DesignPlacementKindChoices.KIND_REMOVE: 0,
    DesignPlacementKindChoices.KIND_MOVE: 1,
    DesignPlacementKindChoices.KIND_ADD: 2,
}
#: Upper bound on ``project_rack`` calls one :func:`auto_order` makes. Beyond it
#: the steps still unplaced are stably sorted by kind (remove < move < add).
MAX_AUTO_ORDER_EVALS = 400


def auto_order(design, steps, *, max_evals=MAX_AUTO_ORDER_EVALS):
    """
    Propose a safe order for ``steps`` (list of lists of placement ids) by
    simulating it. Read-only; returns a new list of lists with the same steps
    (never merged), plus one single-action step per UNSCHEDULED placement.

    Greedy: at every position the first remaining step (in the caller's order)
    that introduces no NEW problem on the racks it touches -- relative to the
    same racks one step earlier -- is taken. When none is clean, the one with the
    fewest new (errors, warnings) wins; ties keep the caller's order, then the
    kind rank (remove < move < add). Afterwards each step still red with several
    actions is split into single-action steps in place and those are ordered by
    one more greedy pass. At most ``max_evals`` projections are made; once spent,
    whatever is left is placed by kind rank.
    """
    placements, children, always_on = validate_steps(design, steps)
    listed = {pk for step in steps for pk in step}
    extra = list(
        DesignPlacement.objects.filter(
            design=design, parent_placement__isnull=True, base_parent_placement__isnull=True)
        .exclude(pk__in=listed).select_related("device", "target_bay__device").order_by("pk"))
    for placement in extra:
        placements[placement.pk] = placement
    for child_pk, parent_pk in DesignPlacement.objects.filter(
        design=design, parent_placement_id__in=[p.pk for p in extra]
    ).values_list("pk", "parent_placement_id"):
        children.setdefault(parent_pk, []).append(child_pk)

    units = [list(step) for step in steps if step] + [[p.pk] for p in extra]
    ctx = _AutoOrder(design, placements, children, always_on, max_evals)
    ordered = ctx.greedy(units, [])

    # Split what is still red, in place, then order only those singles.
    red = ctx.red_steps(ordered)
    if any(red) and any(len(u) > 1 and r for u, r in zip(ordered, red, strict=True)):
        groups = [
            [[pk] for pk in unit] if (r and len(unit) > 1) else None
            for unit, r in zip(ordered, red, strict=True)
        ]
        result = []
        for unit, group in zip(ordered, groups, strict=True):
            result += ctx.greedy(group, result) if group is not None else [unit]
        ordered = result
    return ordered


class _AutoOrder:
    """State of one :func:`auto_order` run: caches rack problems per prefix."""

    def __init__(self, design, placements, children, always_on, max_evals):
        self.design = design
        self.placements = placements
        self.children = children
        self.always_on = set(always_on)
        self.budget = max_evals
        self.racks = {}
        self.cache = {}

    def _rack(self, kind, pk):
        if (kind, pk) not in self.racks:
            model = Rack if kind == "r" else PlannedRack
            self.racks[(kind, pk)] = model.objects.filter(pk=pk).first()
        return self.racks[(kind, pk)]

    def _subset(self, units):
        subset = set(self.always_on)
        for unit in units:
            for pk in unit:
                subset.add(pk)
                subset.update(self.children.get(pk, ()))
        return frozenset(subset)

    def _touched(self, unit):
        return sorted({pair for pk in unit for pair in _touched(self.placements[pk])})

    def _problems(self, pair, subset):
        """Problem counter of one rack at ``subset``; None when over budget."""
        key = (pair, subset)
        if key not in self.cache:
            rack = self._rack(*pair)
            if rack is None:
                self.cache[key] = Counter()
            else:
                if self.budget <= 0:
                    return None
                self.budget -= 1
                elevation = project_rack(self.design, rack, only_placements=set(subset))
                self.cache[key] = Counter(
                    (p["code"], p["severity"]) for p in _problems(elevation))
        return self.cache[key]

    def _score(self, unit, prefix_subset, after_subset):
        """(new errors, new warnings) of ``unit``; None when over budget."""
        errors = warnings = 0
        for pair in self._touched(unit):
            before = self._problems(pair, prefix_subset)
            after = self._problems(pair, after_subset)
            if before is None or after is None:
                return None
            for (_code, severity), count in (after - before).items():
                if severity == "error":
                    errors += count
                else:
                    warnings += count
        return errors, warnings

    def _rank(self, unit):
        return min(_KIND_RANK.get(self.placements[pk].kind, 2) for pk in unit)

    def greedy(self, units, prefix):
        """Order ``units`` after the already-ordered ``prefix`` (list of units)."""
        remaining = list(units)
        out = []
        while remaining:
            base = prefix + out
            prefix_subset = self._subset(base)
            best = None
            for index, unit in enumerate(remaining):
                after = self._subset(base + [unit])
                score = self._score(unit, prefix_subset, after)
                if score is None:  # budget spent: kind order for the rest
                    remaining.sort(key=self._rank)
                    return out + remaining
                key = (score, index, self._rank(unit))
                if best is None or key < best[0]:
                    best = (key, index)
                if score == (0, 0):
                    break
            out.append(remaining.pop(best[1]))
        return out

    def red_steps(self, ordered):
        """Per step: does it carry an error on a rack it touches (absolute)?"""
        flags = []
        for i, unit in enumerate(ordered):
            subset = self._subset(ordered[: i + 1])
            red = False
            for pair in self._touched(unit):
                found = self._problems(pair, subset)
                if found is None:
                    break
                if any(sev == "error" for (_c, sev) in found):
                    red = True
                    break
            flags.append(red)
        return flags


# --- problems ---------------------------------------------------------------

def _problem(code, severity, detail):
    return {"code": code, "severity": severity, "detail": detail}


def _problems(elevation):
    rack = elevation.rack
    power = elevation.power
    out = []
    out += _power_problems(rack, power)
    out += _distribution_problems(rack, power)
    out += _slot_problems(rack, elevation)
    out += _unpowered_problems(rack, elevation)
    status = power.get("distribution_status") or {}
    if status.get("state") == "failed":
        out.append(_problem("engine_error", "error", status.get("detail") or "Distribution failed."))
    return out


def _power_problems(rack, power):
    """Rack total vs capacity: the rack power bar's own state (``_project_power``:
    ``critical`` at ``power_critical_pct``, ``warn`` at ``power_warn_pct``)."""
    state = power.get("state")
    text = (
        f"{rack.name}: {power.get('draw_w', 0):.0f} W of "
        f"{power.get('capacity_w') or 0:.0f} W ({power.get('util_pct', 0):.0f}%)"
    )
    if state == "critical":
        return [_problem("rack_over", "error", text)]
    if state == "warn":
        return [_problem("rack_near", "warning", text)]
    return []


def _distribution_problems(rack, power):
    dist = power.get("distribution")
    if not dist:
        return []
    out = []
    for pdu_name, pdu in (dist.get("pdus") or {}).items():
        total = 0.0
        for bank_id, bank in (pdu.get("banks") or {}).items():
            load = (bank.get("allocated_power") or 0) + (bank.get("planned_power") or 0)
            total += load
            red = bank.get("state") in _RED_BANK_STATES
            if red or bank.get("state") == _WARN_BANK_STATE:
                out.append(_problem(
                    "bank_over" if red else "bank_near",
                    "error" if red else "warning",
                    f"PDU {pdu_name} bank {bank_id}: {load:.0f} W of "
                    f"{bank.get('max_power') or 0:.0f} W "
                    f"({bank.get('util_pct') or 0:.0f}%)"))
        limit = pdu.get("allocated_draw") or 0
        if limit and total > limit:
            out.append(_problem(
                "pdu_over", "error",
                f"PDU {pdu_name}: {total:.0f} W on a {limit:.0f} W input"))
    # A script's rack ceiling (power_limitation), as _finalize_native computes it.
    summary = dist.get("rack") or {}
    ceiling = summary.get("power_limitation_w")
    consumed = summary.get("power_consumption_w") or 0
    if ceiling and consumed > ceiling and power.get("state") != "critical":
        out.append(_problem(
            "rack_over", "error",
            f"{rack.name}: {consumed:.0f} W exceeds the power limitation of {ceiling:.0f} W"))
    return out


def _slot_problems(rack, elevation):
    """u_conflict / bay_conflict.

    * u_conflict: two slots on one face overlap, at least one of them a planned
      entry (add / move-in). The interval test is the projection's own
      (``_u_interval_overlap``, used by ``_mark_displaced`` and the peer check).
      A device whose removal is in the same or an earlier step shows as a
      ``remove`` slot, which does not occupy; in a later step it is still
      ``existing`` and collides. The projection's ``unit_occupied`` (an
      ancestor's hardware) is reported too.
    * bay_conflict: the projection's ``bay_occupied``.
    """
    out, seen = [], set()
    for face in (elevation.front, elevation.rear):
        slots = [s for s in face if s["state"] in _OCCUPYING and s["u_position"] is not None]
        for i, a in enumerate(slots):
            for b in slots[i + 1:]:
                if a["state"] not in _PLANNED_ENTRIES and b["state"] not in _PLANNED_ENTRIES:
                    continue
                if a.get("placement") is not None and a["placement"] is b.get("placement"):
                    continue
                if a.get("device") is not None and a["device"] is b.get("device"):
                    continue
                if not projection._u_interval_overlap(
                    float(a["u_position"]), float(a.get("u_height") or 1),
                    float(b["u_position"]), float(b.get("u_height") or 1),
                ):
                    continue
                names = sorted((_label(a), _label(b)))
                token = tuple(names)
                if token in seen:
                    continue
                seen.add(token)
                low = a if float(a["u_position"]) <= float(b["u_position"]) else b
                out.append(_problem(
                    "u_conflict", "error",
                    f"{rack.name} U{projection._fmt_u(low['u_position'])}: "
                    f"{names[0]} and {names[1]} occupy the same unit."))
    for conflict in elevation.conflicts:
        if conflict["kind"] == "unit_occupied":
            out.append(_problem("u_conflict", "error", conflict["detail"]))
        elif conflict["kind"] == "bay_occupied":
            out.append(_problem("bay_conflict", "error", conflict["detail"]))
    return out


def _label(slot):
    return slot.get("display_label") or slot.get("label") or "a device"


def _unpowered_problems(rack, elevation):
    """unpowered / redundancy_lost: PDUs removed by placements in steps 1..N while a
    device that is still racked in this projection is cabled to one of their outlets.

    Rule: for every ``remove`` / ``move_out_ghost`` PDU slot
    (``distribution.removed_pdu_pks``), follow the real cabling of its outlets
    (``PowerOutlet.link_peers`` -> power port -> device). A peer that is an
    ``existing`` / ``move_in`` slot of this rack's projection has lost a power
    path. If it has no other live path (no power port cabled to anything but a
    removed PDU's outlet) it is ``unpowered`` (error); if some path remains it only
    lost redundancy (``redundancy_lost``, warning). Devices in OTHER racks cabled
    to the PDU are not checked (only this rack's elevation is projected)."""
    slots = [s for face in (elevation.front, elevation.rear, elevation.non_racked) for s in face]
    alive = {
        s["device"].pk for s in slots
        if s["state"] in (ProjectedSlotState.EXISTING, ProjectedSlotState.MOVE_IN)
        and s.get("device") is not None
    }
    removed = removed_pdu_pks(elevation)
    pdus = {}
    for slot in slots:
        device = slot.get("device")
        if device is not None and device.pk in removed:
            pdus[device.pk] = device
    out = []
    for pdu in sorted(pdus.values(), key=lambda d: d.name):
        unpowered, degraded = {}, {}
        for outlet in PowerOutlet.objects.filter(device=pdu, cable__isnull=False):
            for peer in outlet.link_peers:
                device = getattr(peer, "device", None)
                if device is None or device.pk not in alive or device.pk == pdu.pk:
                    continue
                if _has_live_path(device, removed):
                    degraded[device.pk] = device.name
                else:
                    unpowered[device.pk] = device.name
        if unpowered:
            names = ", ".join(sorted(unpowered.values()))
            out.append(_problem(
                "unpowered", "error",
                f"{rack.name}: PDU {pdu.name} is removed while {names} "
                f"{'is' if len(unpowered) == 1 else 'are'} still cabled to it "
                f"and have no other power path."))
        if degraded:
            names = ", ".join(sorted(degraded.values()))
            out.append(_problem(
                "redundancy_lost", "warning",
                f"{rack.name}: PDU {pdu.name} is removed; {names} "
                f"{'keeps' if len(degraded) == 1 else 'keep'} a single power path."))
    return out


def _has_live_path(device, removed_pdu_pks):
    """True when one of the device's power ports is cabled to something other than
    an outlet of a removed PDU."""
    for port in device.powerports.all():
        for peer in (port.link_peers or []):
            peer_device = getattr(peer, "device", None)
            if peer_device is not None and peer_device.pk in removed_pdu_pks:
                continue
            return True
    return False
