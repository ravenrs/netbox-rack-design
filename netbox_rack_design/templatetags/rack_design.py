"""
Template filters for the read-only projected rack elevation.

These helpers translate a projected slot dict (see ``projection.py``) and the
target rack into the GridStack half-U grid geometry used by the shared per-rack
block (``inc/rack_block.html``, rendered by both the read-only elevation and the
editor). The grid runs at half-U resolution: every rack unit
is two grid rows, so ``gs-h`` / ``gs-max-row`` are the U values doubled. Adapted
from netbox-reorder-rack's ``templatetags/rack.py``.
"""

from django import template
from utilities.html import foreground_color

register = template.Library()


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
def slot_gs_y(slot, rack):
    """
    Compute a slot's ``gs-y`` (top grid row) for a half-U GridStack column.

    GridStack lays out from row 0 at the top. NetBox racks number U1 at the
    bottom (ascending) unless ``desc_units`` flips them. Mirrors reorder-rack's
    ``calculate_u_position`` so the elevation reads top-of-rack first.
    """
    u_height = int(rack.u_height) * 2
    height = int(slot["u_height"]) * 2
    unit_id = int(slot["u_position"]) * 2

    if rack.desc_units:
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
    """Background hex color for a slot (its device role color, no leading #)."""
    device = slot.get("device")
    if device is not None and device.role and device.role.color:
        return device.role.color
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

    Real devices (existing / move / remove) carry their own role; a planned
    ``add`` carries the role chosen on its placement. Returns "" when neither
    has a role so the template can omit the line entirely.
    """
    device = slot.get("device")
    if device is not None and device.role:
        return device.role.name
    placement = slot.get("placement")
    if placement is not None and placement.device_role:
        return placement.device_role.name
    return ""


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
    Tenant NAME for a slot's hover card (real device's tenant, or a planned
    add's tenant). Returns "" when there is no tenant.
    """
    device = slot.get("device")
    if device is not None and device.tenant:
        return device.tenant.name
    placement = slot.get("placement")
    if placement is not None and placement.tenant:
        return placement.tenant.name
    return ""


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
    the blade layer.
    """
    out = []
    for index, bay in enumerate(bays or (), start=1):
        if not bay.get("occupied"):
            continue
        label = bay.get("label") or ""
        # Numbered like a rack unit, not named: bay names in the wild are a mix
        # of "slot3", "top-left" and "pci9", so the NUMBER is the only stable
        # identifier a reader can use. The real name stays on the bay row in the
        # blade layer, where there is room for it.
        out.append(f"Bay {index}: {label}")
    return ", ".join(out)
