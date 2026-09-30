"""
Template filters for the read-only projected rack elevation.

These helpers translate a projected slot dict (see ``projection.py``) and the
target rack into the GridStack half-U grid geometry used by the shared per-rack
block (``inc/rack_block.html``, rendered by both the read-only elevation and the
editor). The grid runs at half-U resolution: every rack unit
is two grid rows, so ``gs-h`` / ``gs-max-row`` are the U values doubled. Adapted
from netbox-reorder-rack's ``templatetags/rack.py``.
"""

import json

from django import template
from utilities.html import foreground_color

from .. import planning_fields, projection, rackinfo

register = template.Library()


@register.filter()
def rack_dom_id(rack_key):
    """
    Turn a namespaced ``rack_key()`` string (``"r:<pk>"`` / ``"p:<pk>"``,
    models.py D27) into the spelling used in HTML ``id`` / ``gs-id`` /
    ``data-rack-id`` attributes.

    **A real rack keeps its BARE pk; only a planned rack is prefixed**
    (``"p-<pk>"``). That asymmetry is deliberate, and it is the whole point:

    - The collision D31 describes only ever happens BETWEEN the two kinds --
      ``dcim.Rack`` pk 1 and ``PlannedRack`` pk 1 are different racks (D28).
      Real pks are unique among real racks and planned pks among planned
      ones, so prefixing just one side removes the overlap completely. The
      invariant "no two racks in one page share a DOM id" holds either way.
    - Prefixing BOTH sides was tried first and broke all 125 editor e2e
      tests at once: they build selectors like
      ``"#nbx-rd-grid-front-" + rack_pk`` from the Python side, and every
      one of them went dead with ``Cannot read properties of null``.
      Rewriting 125 tests to buy nothing extra is churn, not safety.
    - "A bare integer means a real rack" is already this codebase's rule on
      the wire: ``api/views.py:parse_real_rack_id`` maps a bare int to a
      real rack, and D27 keeps accepting that form for one release. The DOM
      now says the same thing the API says.

    ``-`` rather than ``:`` for the planned prefix because ``:`` is a CSS
    selector metacharacter -- ``document.querySelector("#rd-rack-p:1")``
    would silently match nothing rather than raise. No such ``#id`` selector
    exists today (T1.5c-survey.md checked exhaustively); this is designing
    out a future footgun. The colon form stays everywhere else (JS-internal
    values, API payloads), so ``rackKeyToServer()`` is what converts back.
    """
    key = str(rack_key or "")
    if key.startswith("r:"):
        return key[2:]
    if key.startswith("p:"):
        return "p-" + key[2:]
    return key


@register.filter()
def rack_whole_unit(value):
    """True when a (possibly half-U) unit number lands on a whole rack unit."""
    try:
        return float(value) % 1 == 0
    except (TypeError, ValueError):
        return False


@register.filter()
def mul2(value):
    """Double a U value to its half-U grid-row count (1 U -> 2 rows)."""
    try:
        return int(value) * 2
    except (TypeError, ValueError):
        return 0


@register.filter()
def rack_unit_numbers(rack):
    """
    The numbering column's U values, top to bottom, for either rack kind
    (T1.5): ``rack.units`` does not exist on a ``models.PlannedRack``, so
    this shared template (used by both the editor and the read-only
    elevation) reads through ``rackinfo.rack_unit_numbers`` instead of the
    raw property.
    """
    return rackinfo.rack_unit_numbers(rack)


@register.filter()
def slot_gs_y(slot, rack):
    """
    Compute a slot's ``gs-y`` (top grid row) for a half-U GridStack column.

    GridStack lays out from row 0 at the top. NetBox racks number U1 at the
    bottom (ascending) unless ``desc_units`` flips them. Mirrors reorder-rack's
    ``calculate_u_position`` so the elevation reads top-of-rack first.
    ``rackinfo.rack_desc_units`` rather than the raw ``rack.desc_units``
    (T1.5): a ``PlannedRack`` has no field of its own for this -- see that
    helper's docstring for why ``False`` is the right default.
    """
    u_height = int(rack.u_height) * 2
    height = int(slot["u_height"]) * 2
    unit_id = int(slot["u_position"]) * 2

    if rackinfo.rack_desc_units(rack):
        return unit_id - 2
    if height > 1:
        return u_height - unit_id - height + 2
    return u_height - unit_id


@register.filter()
def stripe_top_pct(slot, rack):
    """
    Top offset of a displaced slot's OUTSIDE stripe bar, as a PERCENTAGE of
    the face grid's full row span (spec §3, ruling 2026-07-09). Percentages
    (not pixels) so the bar keeps tracking the rows through any resize --
    same geometry editor.js's makeStripeBar computes.
    """
    max_row = int(rack.u_height) * 2
    if not max_row:
        return 0
    return slot_gs_y(slot, rack) / max_row * 100


@register.filter()
def stripe_height_pct(slot, rack):
    """Height of a displaced slot's stripe bar, as a percentage (see above)."""
    max_row = int(rack.u_height) * 2
    if not max_row:
        return 0
    return int(slot["u_height"]) * 2 / max_row * 100


@register.filter()
def slot_color(slot):
    """Background hex color for a slot (its EFFECTIVE role's color, no leading #).

    Effective, not the device's own: a move that re-attributes the device is
    coloured by the role it is planned to have, which is the whole point of
    showing the planned world rather than the current one.
    """
    role = projection.slot_role(slot)
    if role is not None and role.color:
        return role.color
    return ""


@register.filter()
def slot_text_color(slot):
    """Foreground hex color contrasting the slot's role color (no leading #)."""
    color = slot_color(slot)
    if color:
        return foreground_color(color)
    return "000000"


@register.filter()
def slot_role_name(slot):
    """
    Device-role NAME for a slot's hover card.

    The EFFECTIVE role: a planned add's chosen role, a move's planned
    re-attribution, or otherwise the real device's own. Returns "" when there is
    none so the template can omit the line entirely.
    """
    role = projection.slot_role(slot)
    return role.name if role is not None else ""


@register.filter()
def slot_old_role_name(slot):
    """The role a MOVED device has today, when the design overrides it.

    Returns "" unless there is genuinely something to contrast, so the hover
    card shows a "was" line only where the plan actually changes the role.
    """
    device = slot.get("device")
    placement = slot.get("placement")
    if device is None or placement is None:
        return ""
    if not getattr(placement, "device_role_id", None):
        return ""
    if device.role is None or device.role_id == placement.device_role_id:
        return ""
    return device.role.name


@register.filter()
def slot_device_type_name(slot):
    """
    Device-type MODEL name for a slot's hover card.

    The projection sets ``device_type`` on EVERY slot that has one: a planned
    ``add`` carries its chosen catalog type, while existing / move / remove slots
    carry the real device's type (see ``projection.py``). Returns "" only when the
    type is genuinely unknown so the template can omit the line entirely.
    """
    device_type = slot.get("device_type")
    if device_type is not None and device_type.model:
        return device_type.model
    return ""


@register.filter()
def slot_tenant_name(slot):
    """
    Tenant NAME for a slot's hover card.

    The EFFECTIVE tenant: a planned add's or a move's planned tenant, otherwise
    the real device's own. Returns "" when there is none.
    """
    tenant = projection.slot_tenant(slot)
    return tenant.name if tenant is not None else ""


@register.filter()
def bays_used(bays):
    """
    How many of a chassis's bays are occupied, in the PROJECTED world.

    A blade this design removes has vacated, so it does not count as used --
    the same vacating-slot reading the rack rows use (spec §4.3/§10.3).
    """
    return sum(
        1 for bay in (bays or ())
        if bay.get("occupied") and bay.get("state") != "remove"
    )


@register.filter()
def bay_occupants(bays):
    """
    The occupant names of a chassis's bays, for the hover card (spec §10.4).

    ``bay: name`` per entry so the reader can tell WHICH bay holds what. Just
    the name -- the planned state is NOT appended: it read as "(add)" noise
    after every planned blade and told the reader nothing the tile's own colour
    does not (user 2026-08-25). The rack view reports this; editing happens in
    the chassis layer.
    """
    out = []
    for index, bay in enumerate(bays or (), start=1):
        if not bay.get("occupied"):
            continue
        label = bay.get("label") or ""
        # Numbered like a rack unit, not named: bay names in the wild are a mix
        # of "slot3", "top-left" and "pci9", so the NUMBER is the only stable
        # identifier a reader can use. The real name stays on the bay row in the
        # chassis layer, where there is room for it.
        out.append(f"Bay {index}: {label}")
    return ", ".join(out)


@register.filter()
def slot_moved_from(slot):
    """Where a moved device stands TODAY -- for the destination tile's card.

    A ``move_in`` tile is already drawn at its destination, so "To <this
    rack> - U<this unit>" only repeats what the tile's own position says.
    The half a planner cannot see from the tile is the ORIGIN: the device's
    real ``dcim`` rack and unit, which is exactly what the vacated ghost at
    the other end says in reverse (user ruling 2026-09-22).

    Returns ``"<rack> \u00b7 U<n>"``, or ``"<rack> \u00b7 tray"`` for a 0U
    device, and "" when the device has no rack to name (nothing to say, so
    the template omits the attribute).
    """
    device = slot.get("device")
    rack = getattr(device, "rack", None)
    if device is None or rack is None:
        return ""
    return _place_label(rack, getattr(device, "position", None))


@register.filter()
def slot_moved_to(slot):
    """Where a moved device is GOING -- for the vacated ghost's card.

    The mirror of :func:`slot_moved_from`: the ghost is drawn at the origin,
    so the half it cannot show is the destination. Reads the placement's
    target, real rack or planned (``PlannedRack`` has no site of its own --
    it takes its location's).
    """
    placement = slot.get("placement")
    if placement is None:
        return ""
    rack = getattr(placement, "target_rack", None) or getattr(
        placement, "target_planned_rack", None)
    if rack is None:
        return ""
    return _place_label(rack, getattr(placement, "target_position", None))


def _place_label(rack, position):
    """``"<site> \u00b7 <rack> \u00b7 U<n>"`` -- or ``\u00b7 tray`` for a 0U slot.

    The SITE leads, because a design may span several sites and rack names
    repeat across them: every hall has an R101, so a bare rack name names no
    place at all (user ruling 2026-09-22). A rack with no site to report
    (a planned rack whose location was cleared) simply omits that part.
    """
    site = getattr(rack, "site", None)
    where = f"U{int(position)}" if position is not None else "tray"
    if site is None:
        return f"{rack.name} \u00b7 {where}"
    return f"{site.name} \u00b7 {rack.name} \u00b7 {where}"


@register.filter()
def planning_rows(planning_data):
    """``(label, value)`` rows for a stored ``planning_data`` blob, in the
    configured order and read the way a person reads them (a choice's label,
    an object's name). Unset fields are omitted."""
    data = planning_data or {}
    rows = []
    for field in planning_fields.placement_field_schema():
        value = data.get(field["key"])
        if value in (None, "", []):
            continue
        rows.append((field["label"], planning_fields.display_value(field, value)))
    return rows


@register.filter()
def placement_planning(placement):
    """``(label, value, from_device)`` for EVERY configured placement field.

    The placement page lists them all, set or not, so a person sees which
    fields the deployment plans with. The design's own value wins; failing
    that, a placement that acts on a real device shows the device's current
    custom field (``from_device`` True), the value it keeps unless the
    design overrides it. ``None`` for a value that is unset everywhere.
    """
    planned = placement.planning_data or {}
    rows = []
    for field in planning_fields.placement_field_schema():
        value = planned.get(field["key"])
        from_device = False
        if value in (None, "", []) and placement.device_id:
            value = planning_fields.resolve_source(placement.device, field.get("target"))
            from_device = value not in (None, "", [])
        shown = None if value in (None, "", []) else planning_fields.display_value(field, value)
        rows.append((field["label"], shown, from_device))
    return rows


@register.filter()
def slot_planning(slot):
    """The deployment's config-declared planning fields for a slot's hover card.

    Emitted as a JSON array of ``[label, value]`` pairs rather than the
    ``a:b|c:d`` shape ``data-power`` uses, because a text planning field may
    legitimately contain a colon or a pipe. Returns "" when the deployment
    declares no planning fields (or none of them is set here), so the template
    omits the attribute entirely.
    """
    pairs = planning_fields.read_for_slot(
        slot.get("device"),
        getattr(slot.get("placement"), "planning_data", None),
    )
    if not pairs:
        return ""
    return json.dumps([[label, str(value)] for label, value in pairs])
