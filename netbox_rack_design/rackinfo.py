"""
"Which kind of rack is this" accessors -- the ONLY place that question gets
asked (PLAN-templates.md D3/D6, T1.4a).

``projection.py`` and ``distribution.py`` read a handful of attributes off a
``rack`` variable that used to be guaranteed a real ``dcim.Rack``:
``rack.get_rack_units(...)``, ``rack.devices``, ``rack.desc_units``. Now that
``project_rack(design, rack)`` also has to accept a ``PlannedRack`` (a rack
that does not exist in DCIM yet -- see ``models.PlannedRack``'s docstring),
every one of those reads needs a planned-rack answer, and every one of them
would otherwise crash with an ``AttributeError``.

The temptation is to sprinkle ``isinstance(rack, PlannedRack)`` at each call
site. Don't -- not because it's untidy, but because it doesn't survive:
``projection.py`` is ~150KB and under constant edit, so an inline branch is
invisible to the next person touching that function, who will write
``rack.devices`` again and reintroduce the exact crash this module exists to
prevent. A named accessor makes a raw ``rack.devices`` or ``rack.desc_units``
stand out as an anomaly the moment someone greps for it or reads the diff --
that's the whole reason this module exists, not tidiness for its own sake.

Every accessor takes a "rack" that is EITHER a ``dcim.Rack`` or a
``models.PlannedRack``, and returns the planned-rack-safe answer: no real
devices, an all-free elevation, ascending numbering starting at U1 (a planned
rack has never been given ``desc_units``/``starting_unit`` of its own -- see
``PlannedRack``'s field list -- so these are core's own defaults, not a made
up convention).

Deliberately NOT covered here (at the time the paragraph above was written):
peer conflicts for a shared planned rack, and namespacing a rack's identity
across the two pk spaces (``rack_key()`` in ``models.py`` already owns that)
-- both are separate tasks (PLAN-templates.md T1.4b/peer-conflicts), not
"which kind of rack" questions.

T1.4b ADDENDUM -- the same-pk landmine (PLAN-templates.md D28):
``PlannedRack`` and ``dcim.Rack`` have SEPARATE pk sequences, so pk 74 exists
in both tables and means two different racks. Before this addendum,
``projection.py`` compared a bare ``rack.pk`` against ``dcim.Rack``-typed FK
columns (``device.rack_id``, ``DesignPlacement.target_rack_id``,
``DesignApply.device__rack_id``) directly, in around a dozen places. When
``rack`` turned out to be a ``PlannedRack`` whose pk happened to equal some
unrelated real rack's pk, every one of those comparisons silently matched
that unrelated real rack's rows -- not a crash, plausible wrong output.

``placement_targets_rack``, ``device_is_in_rack`` and ``applies_for_rack``
below are the guarded replacements. The same "don't inline it" reasoning as
the rest of this module applies with extra force here: a raw
``== rack.pk``/``!= rack.pk`` reads as ordinary, correct-looking code, so an
inline guard is even less likely to survive the next edit than a missing
``rack.devices`` call would have been -- there is no crash to point at the
bug. A named accessor is what makes the comparison itself the visible
anomaly.
"""

from decimal import Decimal

__all__ = (
    "is_planned",
    "rack_units",
    "rack_unit_numbers",
    "rack_devices",
    "rack_desc_units",
    "rack_starting_unit",
    "placement_targets_rack",
    "device_is_in_rack",
    "applies_for_rack",
)


def is_planned(rack):
    """True if ``rack`` is a ``PlannedRack`` (not yet realized in DCIM)."""
    from .models import PlannedRack

    return isinstance(rack, PlannedRack)


def _planned_elevation_units(u_height):
    """
    The unit sequence a freshly-realized ``dcim.Rack`` of this height would
    have, top to bottom -- mirrors ``Rack.units`` (dcim/models/racks.py) for
    the ONLY ``desc_units``/``starting_unit`` combination a planned rack can
    ever have (``desc_units=False``, ``starting_unit=1``; see
    ``rack_desc_units``/``rack_starting_unit`` below). Reimplemented rather
    than instantiating a throwaway ``dcim.Rack`` because ``Rack.units`` is a
    plain property with no query behind it -- there is nothing to gain by
    involving the ORM for arithmetic, and every gain in not having to fake a
    valid unsaved ``dcim.Rack`` (site, a real ``RackType``/width, etc.) just to
    read one property off it.
    """
    u = Decimal(u_height) + Decimal("0.5")
    end = Decimal("0.5")
    while u > end:
        yield u
        u -= Decimal("0.5")


def rack_units(rack, *, face, expand_devices=False):
    """
    ``Rack.get_rack_units(face=face, expand_devices=expand_devices)``'s exact
    dict shape (``{'id', 'name', 'face', 'device', 'occupied'}``, top to
    bottom), for either rack kind.

    A real rack simply delegates to core. A planned rack has no ``dcim.Device``
    rows at all -- it does not exist in DCIM yet -- so every unit comes back
    free: ``device`` is always ``None``, ``occupied`` always ``False``, and
    (because nothing is ever occupied) the ``expand_devices=False`` variant's
    extra ``'height'`` key never applies. This dict shape is identical across
    NetBox 4.4/4.5/4.6 (verified against ``dcim/models/racks.py`` in all three
    checkouts) -- there is nothing here for ``compat.py`` to own.
    """
    if not is_planned(rack):
        return rack.get_rack_units(face=face, expand_devices=expand_devices)

    units = []
    for u in _planned_elevation_units(rack.u_height):
        name = f"U{u}".split(".")[0] if not u % 1 else f"U{u}"
        units.append({"id": u, "name": name, "face": face, "device": None, "occupied": False})
    return units


def rack_unit_numbers(rack):
    """
    ``rack.units`` for either rack kind -- the plain top-to-bottom list of U
    numbers the elevation's numbering column reads (T1.5,
    ``inc/rack_block.html``). ``dcim.Rack.units`` is a plain property with no
    query behind it and no counterpart on ``PlannedRack``, so a raw
    ``rack.units`` in a shared template crashes the moment ``rack`` is
    planned. Delegates to core for a real rack; reuses the SAME
    ``_planned_elevation_units`` generator ``rack_units()`` above already
    built for the ``desc_units=False``/``starting_unit=1`` case a planned
    rack is always in (see that function's docstring for why those are the
    only values it can ever have).
    """
    if is_planned(rack):
        return list(_planned_elevation_units(rack.u_height))
    return list(rack.units)


def rack_devices(rack):
    """
    A ``Device`` queryset scoped to this rack -- ``Device.objects.none()``,
    NOT ``[]``, for a planned rack: every call site chains ``.filter()``,
    ``.exclude()`` or ``.select_related()`` onto the result, which a plain
    list does not support.

    A planned rack has no real devices by definition (it has no ``dcim.Rack``
    row for a ``Device.rack`` FK to point at yet), so there is no query to
    run -- an always-empty queryset IS the correct answer, not a stand-in for
    one.
    """
    if is_planned(rack):
        from dcim.models import Device

        return Device.objects.none()
    devices = rack.devices.all()
    # While a distribution engine runs, the projection's removed / moved-out PDUs
    # are stamped on the rack (distribution.generate_distribution_status): they
    # are not power sources in that projected world.
    removed = rack.__dict__.get("_rd_removed_pdu_pks")
    if removed:
        devices = devices.exclude(pk__in=removed)
    return devices


def rack_desc_units(rack):
    """
    ``rack.desc_units`` for either rack kind. A planned rack has never been
    given a numbering direction of its own (it isn't a ``dcim.Rack`` field at
    all) -- ``False`` (ascending, U1 at the bottom) is core's own default for
    a freshly created rack, so a planned rack's elevation numbers the same
    way a real one would until someone says otherwise.
    """
    return False if is_planned(rack) else rack.desc_units


def rack_starting_unit(rack):
    """
    ``rack.starting_unit`` for either rack kind. Same reasoning as
    ``rack_desc_units``: a planned rack has no field of its own for this, so
    it gets core's own default of ``1``.
    """
    return 1 if is_planned(rack) else rack.starting_unit


def placement_targets_rack(placement, rack):
    """
    True if ``placement``'s destination IS ``rack``, whichever kind ``rack``
    is (T1.4b, PLAN-templates.md D28).

    A placement addresses its destination through two real FKs, never both
    (``models.py``'s ``PlannedRack`` docstring / D3): ``target_rack`` (real)
    or ``target_planned_rack`` (greenfield). A bare
    ``placement.target_rack_id == rack.pk`` is wrong for BOTH kinds of
    ``rack``: for a real one it happens to work only because
    ``target_planned_rack_id`` is never what a real rack's pk would collide
    with; for a planned one it is simply comparing the wrong column --
    ``target_rack_id`` is ``None`` for a placement that targets a planned
    rack, so the comparison would either silently fail to match the move-in
    it should (no collision), or, worse, silently match some unrelated real
    rack's placements that happen to target a rack whose pk equals this
    planned rack's pk (collision). Route every such comparison through here
    instead.
    """
    if is_planned(rack):
        return placement.target_planned_rack_id == rack.pk
    return placement.target_rack_id == rack.pk


def device_is_in_rack(device_rack_id, rack):
    """
    True if a real device (or an identity whose CURRENT location is a real
    rack pk, e.g. a ``_BaselineEntry.rack_id`` in ``projection.py``) sits in
    ``rack`` -- always ``False`` when ``rack`` is a ``PlannedRack``.

    A real ``dcim.Device`` can never sit in a planned rack: the rack does
    not exist in DCIM yet, so there is no ``dcim.Rack`` row for
    ``Device.rack`` to point at. ``device_rack_id`` is always a real
    ``dcim.Rack`` pk (or ``None``), so comparing it to ``rack.pk`` directly
    is the T1.4b landmine the moment ``rack`` is planned: PlannedRack and
    dcim.Rack keep separate pk sequences, so an unrelated real rack sharing
    the planned rack's pk would make an ordinary device look like it is
    sitting in the planned rack.
    """
    if is_planned(rack):
        return False
    return device_rack_id == rack.pk


def applies_for_rack(rack):
    """
    The ``DesignApply`` queryset for devices in ``rack`` -- empty by
    definition for a planned rack.

    A planned rack has no applied devices: applying a design materializes
    real ``dcim.Device`` rows in a REAL rack (see ``models.PlannedRack``'s
    ``realized_rack``/D7), and a planned rack is not one yet. Before this
    helper, ``project_rack`` ran
    ``DesignApply.objects.filter(device__rack_id=rack.pk, ...)``
    unconditionally, for every rack, planned or not (T1.4b) -- a planned
    rack whose pk collides with some unrelated real rack's would fetch
    THAT rack's apply rows. Every caller routes through here instead of the
    bare filter, so "no apply markers for a planned rack" is enforced in one
    place rather than re-guarded (or forgotten) at each call site.
    """
    from .models import DesignApply

    if is_planned(rack):
        return DesignApply.objects.none()
    return DesignApply.objects.filter(device__rack_id=rack.pk, device__isnull=False)
