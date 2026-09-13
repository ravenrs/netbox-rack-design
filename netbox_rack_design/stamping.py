"""
Pure template-stamping algorithm (PLAN-templates.md Phase 3).

This module has no database, no HTTP, no Django model imports and no import
of ``projection.py`` -- on purpose. It decides where a template's items land
in a target rack from plain data alone, so it can be unit-tested without a
test database and reused unchanged from wherever the real work eventually
calls it (a preview API, a management command, a script).

Decisions implemented here (see PLAN-templates.md Sec 3 for the rationale):

D1   Walk the template's items in the order given. Each item takes the FIRST
     FREE SLOT NEAREST ITS ANCHOR: "top" scans downward from the highest
     unit, "bottom" scans upward from the lowest.
D12  An item that cannot be placed anywhere is reported as unplaced. It does
     not consume a slot and it does not shift any other item -- the walk
     simply continues to the next item as if the failed one were never
     there.
D15/D16  Anchor is exactly "top" or "bottom". There is no "middle".
D17  No stored offsets, no preserved gaps: items are compacted against their
     anchor. A reserved gap is expressed in the template as a real blanking
     item, not as empty space the algorithm is asked to remember.

The rack's free unit is not "1U granularity". ``DesignPlacement.position`` is
``DecimalField(max_digits=4, decimal_places=1)`` -- half-unit shelves and
organizers are real inputs -- so every interval here is compared with
``_occupancy._overlaps`` below, which MUST stay in lock-step with
``projection.py:_u_interval_overlap``. That function is the one place the
rest of the plugin decides "is this unit taken"; inventing a second overlap
rule here would let the two silently drift apart. Copied (not imported,
per this module's no-projection-import rule), the test is:

    start_a < end_b and start_b < end_a

i.e. two half-open intervals ``[start, start+height)`` overlap.
"""

from decimal import Decimal

# --- input/output shapes -----------------------------------------------------
#
# ``items``: any ordered sequence of objects (a dataclass, a Django model
# instance, a namedtuple -- duck-typed, deliberately not constrained to one
# type) exposing these four attributes:
#
#   u_height       Decimal | float | int -- may be a 0.5 step.
#   face           "front" | "rear"
#   anchor         "top" | "bottom"
#   is_full_depth  bool -- occupies (and is blocked by) BOTH faces.
#
# ``occupied``: the space already taken in the target rack before this call,
# as a dict of per-face interval lists:
#
#   {"front": [(start, height), ...], "rear": [(start, height), ...]}
#
# Each tuple is one already-occupied span, in the same (start, height) units
# as ``position``/``u_height`` elsewhere in the plugin (lowest unit occupied,
# NetBox's own convention for ``dcim.Device.position``). A full-depth
# existing occupant must appear in BOTH lists by the caller -- this mirrors
# how ``projection.py`` already emits one slot per face for a full-depth
# device (see the ``_u_interval_overlap`` docstring it references), so this
# module does not need to know why a span is occupied, only that it is.
#
# Returns ``(placements, unplaced)``:
#
#   placements  [(item, position, face), ...] in walk order (successes only).
#               ``position`` is the LOWEST unit the item occupies, matching
#               ``dcim.Device.position``.
#   unplaced    [(item, reason), ...] in walk order (failures only).


def _overlaps(start_a, height_a, start_b, height_b):
    """Mirror of ``projection.py:_u_interval_overlap`` -- MUST stay identical
    to that function's semantics. Kept as a local copy rather than an import
    because this module is not allowed to depend on ``projection.py`` (it
    must stay pure/DB-free); if that function's rule ever changes, this one
    has to change with it."""
    end_a = start_a + height_a
    end_b = start_b + height_b
    return start_a < end_b and start_b < end_a


class _FaceOccupancy:
    """Mutable running list of occupied intervals for one face, built once
    per ``compute_stamp`` call and extended as items are placed (D1 rule 5:
    earlier placements in the same walk must block later ones)."""

    def __init__(self, initial):
        self._spans = [(_as_decimal(s), _as_decimal(h)) for s, h in initial]

    def is_free(self, start, height):
        return not any(_overlaps(start, height, s, h) for s, h in self._spans)

    def occupy(self, start, height):
        self._spans.append((start, height))


def _find_slot(front, rear, is_full_depth, face, anchor, height, low, high):
    """Return the lowest unit of the first free slot for one item, scanning
    from the anchor, or ``None`` if none fits within ``[low, high]``
    inclusive-of-start (a slot's top edge must not exceed ``high + 1`` --
    see rule 4 below).

    ``low``/``high`` are the rack's lowest and highest valid unit numbers
    (``starting_unit`` and ``starting_unit + u_height - 1``). The highest
    LEGAL start for an item of this height is ``high + 1 - height`` -- e.g.
    a 2U item in a 47U rack (units 1..47) must start at 46 at the latest, so
    it occupies 46..48 exclusive == units 46 and 47, not 47..49 which would
    hang off the top (rule 4).
    """
    faces = (front, rear) if is_full_depth else ((front,) if face == "front" else (rear,))

    highest_start = high + 1 - height
    if highest_start < low:
        # The item is taller than the entire rack -- no legal start exists
        # anywhere, regardless of occupancy.
        return None

    if anchor == "top":
        # Scan every legal start from the top down. Slots are always a
        # multiple of 0.5U apart in practice, but rather than assume a step
        # size, walk over the set of "candidate starts" -- the highest start
        # and, below it, every occupied span's edges -- which is unnecessary
        # complexity for a first cut. Half-U granularity is guaranteed by
        # the model field (max_digits=4, decimal_places=1), so stepping by
        # 0.5 is exact and never skips a legal position.
        start = highest_start
        while start >= low:
            if all(f.is_free(start, height) for f in faces):
                return start
            start -= _STEP
    else:  # anchor == "bottom"
        start = low
        while start <= highest_start:
            if all(f.is_free(start, height) for f in faces):
                return start
            start += _STEP

    return None


# Half-unit granularity: DesignPlacement.position is DecimalField(max_digits=4,
# decimal_places=1), so 0.5 is the finest step any real position can take.
# Kept as a Decimal (not a float) so stepping never mixes float/Decimal
# arithmetic with a caller that (correctly, per the model field) passes
# Decimal heights -- Decimal - float raises TypeError in Python, Decimal -
# Decimal does not.
_STEP = Decimal("0.5")


def _as_decimal(value):
    """Normalize a height/position to Decimal so every arithmetic op in this
    module is Decimal-vs-Decimal (or Decimal-vs-int, which Python allows
    freely). Callers may pass int, float or Decimal for heights/positions
    (the docstring's contract); mixing float and Decimal in a subtraction
    is the one combination Python refuses, so everything numeric that
    reaches arithmetic here is funneled through this first.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def compute_stamp(items, occupied, u_height, *, starting_unit=1):
    """Decide where each of ``items`` (in order) lands in a rack of
    ``u_height`` units starting at ``starting_unit``.

    Returns ``(placements, unplaced)`` -- see the module docstring for the
    exact shapes. Pure function: no database, no HTTP, no Django models, no
    import of ``projection.py``. Safe to call repeatedly with the same
    arguments and get the same result (rule 7 / D1's determinism).
    """
    front = _FaceOccupancy(occupied.get("front", ()))
    rear = _FaceOccupancy(occupied.get("rear", ()))

    low = starting_unit
    high = starting_unit + u_height - 1

    placements = []
    unplaced = []

    for item in items:
        height = _as_decimal(item.u_height)
        start = _find_slot(
            front, rear, item.is_full_depth, item.face, item.anchor, height, low, high
        )
        if start is None:
            unplaced.append((item, "does not fit"))
            continue

        if item.is_full_depth:
            front.occupy(start, height)
            rear.occupy(start, height)
        elif item.face == "front":
            front.occupy(start, height)
        else:
            rear.occupy(start, height)

        placements.append((item, start, item.face))

    return placements, unplaced


# --- compute_anchors: the inverse of compute_stamp ---------------------------
#
# ``compute_stamp`` turns "anchor + order" into an absolute position.
# ``compute_anchors`` is the other direction: given devices already sitting at
# absolute positions (an existing rack, real or a design's projected one), it
# derives the ``(anchor, order)`` pair that -- fed back through
# ``compute_stamp`` into an EMPTY rack of the same height/starting_unit -- lands
# each device on the exact position it started at.
#
# This is only possible for devices that form a CONTIGUOUS run touching a
# physical end of the rack (D17: the stamping algorithm has no concept of a
# preserved gap, only "compact against the anchor"). A device with genuine
# dead air on both sides -- touching neither physical end -- has no
# (anchor, order) that reproduces it. That is reported back as an "island"
# rather than silently reattached or dropped (PLAN-templates.md D32); the
# caller is expected to surface it as a warning to whoever is saving the
# template.
#
# ``desc_units`` is the trap this function exists to get right. Every rack in
# this plugin is walked in "position" terms -- the lowest unit number a device
# occupies, NetBox's own ``dcim.Device.position`` convention -- but whether the
# NUMERICALLY highest position is the PHYSICAL top depends on the rack's own
# unit numbering:
#
#   desc_units=False (NetBox's default, and the only value a PlannedRack ever
#   returns -- see rackinfo.py): U1 is at the physical BOTTOM, so ascending
#   position numbers climb toward the physical top. The numerically highest
#   position is the physical top. This is the assumption ``compute_stamp``'s
#   own ``_find_slot`` bakes in when it scans "top" downward from ``high``.
#
#   desc_units=True: U1 is at the physical TOP, so ascending position numbers
#   descend toward the physical bottom. The numerically LOWEST position is the
#   physical top -- the exact opposite of the default case.
#
# Getting this backwards doesn't raise -- it silently mirrors the whole layout
# top-for-bottom, which is exactly the class of "looks like ordinary, correct
# code" bug this repo's own rackinfo.py warns about. So this function takes
# ``desc_units`` as an explicit keyword rather than trying to infer it from the
# data, and every place the two cases diverge is called out below.


def _distance_from_top(position, height, low, high, desc_units):
    """Units of empty space between the physical top of the rack and the top
    edge of this item's span, in the item's own face -- 0 means the item's
    span starts flush against the physical top.

    The two branches are mirror images of each other (see the module
    docstring above): which one is "flush with the top" flips with
    ``desc_units`` because that is exactly what "physical top" means.
    """
    if not desc_units:
        # Ascending numbering, U1 at the physical bottom: the physical top is
        # the numerically highest unit, i.e. ``high``. The item's top edge
        # (in physical terms) is at ``position + height - 1`` -- an item's
        # *top* edge is the highest unit it occupies, not its lowest.
        return (high + 1) - (position + height)
    else:
        # desc_units=True, U1 at the physical top: the physical top IS the
        # numerically lowest unit, i.e. ``low``, and ``position`` (the
        # lowest unit the item occupies) is already the item's edge nearest
        # that physical top.
        return position - low


def _distance_from_bottom(position, height, low, high, desc_units):
    """Mirror of ``_distance_from_top`` for the physical bottom -- see that
    function's docstring; the two branches swap for the same reason."""
    if not desc_units:
        return position - low
    else:
        return (high + 1) - (position + height)


def _peel_face(item_ids, info):
    """Peel a contiguous run touching the physical top, then a contiguous run
    touching the physical bottom, from one face's bucket of item ids.

    Returns ``(top_run, bottom_run, islands)``, each a list of item ids.
    ``top_run``/``bottom_run`` are in walk order (nearest the anchor first --
    exactly the order ``compute_stamp`` needs to reproduce them). An id can
    only end up in one of the three lists: the top run is peeled first and
    those ids are removed from consideration before the bottom run is peeled,
    so a rack fully occupied end-to-end on one face still partitions cleanly
    (whichever run reaches a given device first claims it).
    """
    remaining = sorted(item_ids, key=lambda i: info[i]["dtop"])
    used = set()
    top_run = []
    expected = Decimal(0)
    for i in remaining:
        if info[i]["dtop"] != expected:
            break  # first gap -- the run stops touching the physical top
        top_run.append(i)
        used.add(i)
        expected += info[i]["height"]

    rest = sorted((i for i in item_ids if i not in used), key=lambda i: info[i]["dbot"])
    bottom_run = []
    expected = Decimal(0)
    for i in rest:
        if info[i]["dbot"] != expected:
            break
        bottom_run.append(i)
        used.add(i)
        expected += info[i]["height"]

    islands = [i for i in item_ids if i not in used]
    return top_run, bottom_run, islands


def compute_anchors(items, u_height, *, starting_unit=1, desc_units=False):
    """Derive ``(anchor, order)`` per item such that feeding the result back
    through ``compute_stamp`` into an EMPTY rack of the same ``u_height`` /
    ``starting_unit`` reproduces the original absolute positions.

    ``items`` are duck-typed objects (same contract as ``compute_stamp``'s,
    plus ``position`` -- the LOWEST unit the item occupies, matching
    ``dcim.Device.position``):

        position       Decimal | float | int
        u_height       Decimal | float | int
        face           "front" | "rear"
        is_full_depth  bool -- occupies (and is extracted as occupying) BOTH
                       faces at the same position.

    Returns ``(anchored, islands)``:

        anchored  [(item, anchor, order), ...] -- ``anchor`` is "top" or
                  "bottom", ``order`` is 0-based within its anchor group, in
                  the exact sequence ``compute_stamp`` should walk them (this
                  function's own return order already satisfies that -- all
                  "top" entries first in walk order, then all "bottom"
                  entries, exactly as PLAN-templates.md D32 specifies).
        islands   [(item, reason), ...] -- items with no representable
                  (anchor, order): real dead air on both sides, touching
                  neither physical end (D17 stores no gaps, so this is not a
                  bug to fix, it is an accepted, reported limitation).

    Pure function: no database, no HTTP, no Django models, no import of
    ``projection.py``. Front and rear are independent occupancy spaces (as in
    ``compute_stamp``); a full-depth item is peeled on BOTH faces (it blocks
    both) and de-duplicated into a single output entry, using whichever
    face's peel actually reaches it -- see the inline comments below for the
    rare case where the two faces disagree.
    """
    low = _as_decimal(starting_unit)
    high = low + _as_decimal(u_height) - 1

    info = {}
    for item in items:
        height = _as_decimal(item.u_height)
        position = _as_decimal(item.position)
        info[id(item)] = {
            "item": item,
            "height": height,
            "dtop": _distance_from_top(position, height, low, high, desc_units),
            "dbot": _distance_from_bottom(position, height, low, high, desc_units),
        }

    front_ids = [id(it) for it in items if it.face == "front" or it.is_full_depth]
    rear_ids = [id(it) for it in items if it.face == "rear" or it.is_full_depth]

    front_top, front_bottom, front_islands = _peel_face(front_ids, info)
    rear_top, rear_bottom, rear_islands = _peel_face(rear_ids, info)

    # A full-depth item is present in both ``front_ids`` and ``rear_ids``, so
    # it was peeled twice above. Almost always both faces agree (or one face
    # has no other occupant nearby and so contributes no opinion) -- see the
    # module docstring's worked reasoning for why that is still safe to
    # round-trip. The one case that genuinely cannot be reconciled is a
    # full-depth item that a contiguous run on ONE face reaches from the top
    # while a run on the OTHER face reaches it from the bottom: it cannot be
    # both "order 0 from the top" and "order 0 from the bottom" at once, so
    # that is reported as an island rather than guessed at.
    conflicts = set()
    for item in items:
        if not item.is_full_depth:
            continue
        i = id(item)
        front_cls = "top" if i in front_top else ("bottom" if i in front_bottom else None)
        rear_cls = "top" if i in rear_top else ("bottom" if i in rear_bottom else None)
        if front_cls and rear_cls and front_cls != rear_cls:
            conflicts.add(i)

    # This is the other half of the desc_units trap (see the module
    # docstring): ``front_top``/``rear_top`` above are runs touching the
    # PHYSICAL top, but ``compute_stamp`` itself has no idea what
    # ``desc_units`` even means -- its "top" always scans from the
    # numerically highest unit. That only coincides with the physical top
    # when ``desc_units=False``. When ``desc_units=True``, the physical top
    # is the numerically LOWEST unit, which is exactly where
    # ``compute_stamp``'s anchor="bottom" scans -- so a run touching the
    # physical top must be labelled anchor="bottom" (not "top") for
    # ``compute_stamp`` to put it back where it physically was. The peeling
    # above (based on physical distance) stays the same either way; only
    # the label handed to ``compute_stamp`` flips.
    physical_top_anchor = "bottom" if desc_units else "top"
    physical_bottom_anchor = "top" if desc_units else "bottom"

    anchored = []
    islands = []
    assigned = set()

    order = 0
    for i in front_top + rear_top:
        if i in assigned or i in conflicts:
            continue
        anchored.append((info[i]["item"], physical_top_anchor, order))
        order += 1
        assigned.add(i)

    order = 0
    for i in front_bottom + rear_bottom:
        if i in assigned or i in conflicts:
            continue
        anchored.append((info[i]["item"], physical_bottom_anchor, order))
        order += 1
        assigned.add(i)

    for item in items:
        i = id(item)
        if i in assigned:
            continue
        if i in conflicts:
            reason = (
                "full-depth item is anchored to the top on one face and the "
                "bottom on the other; cannot be represented as one (anchor, order)"
            )
        else:
            reason = (
                "does not touch either physical end of the rack (dead air on "
                "both sides); a template cannot store a preserved gap"
            )
        islands.append((item, reason))

    return anchored, islands
