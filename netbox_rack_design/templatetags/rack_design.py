"""
Template filters for the read-only projected rack elevation.

These helpers translate a projected slot dict (see ``projection.py``) and the
target rack into the GridStack half-U grid geometry used by
``design_elevation.html``. The grid runs at half-U resolution: every rack unit
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
