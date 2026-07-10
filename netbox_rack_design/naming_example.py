"""
A ready-to-run example naming script for ``naming_mode = "script"``.

Unlike the fully-worked corporate example in ``docs/device-naming.md``, this
module is deliberately small and **works on a stock NetBox** with no extra data
(no custom role slugs, no lookup tables): drop it in as-is to see the two script
patterns that a template cannot express.

Enable it in ``configuration.py``::

    PLUGINS_CONFIG = {
        "netbox_rack_design": {
            "naming_mode": "script",
            "naming_script": "netbox_rack_design.naming_example.build_name",
        },
    }

Contract: ``build_name(placement) -> str``. The plugin calls it for every
palette add / moved device to PREVIEW a name. It is strictly read-only -- it
computes a string and never writes to ``dcim``.

Two patterns worth stealing:

* **Family counter** (:func:`_next_number`) -- continue a numbered family
  (``<site>-sw-1`` -> next is ``-2``) by asking NetBox for the highest existing
  number, so you never hand-pick the next digit. It also counts names proposed
  by OTHER unsaved tiles in the same editor session (via
  :func:`netbox_rack_design.naming.pending_names`), so two quick drops get
  consecutive numbers instead of colliding.

* **Phase pairs** (:func:`_next_pdu_slot`) -- PDUs run ``a1, b1, a2, b2, ...``
  (an A/B phase pair per index) instead of a flat counter.
"""

import re

# ABSOLUTE imports (not ``from .naming``) so this file keeps working verbatim
# when COPIED out of the package into NetBox's SCRIPTS_ROOT -- a relative import
# would break there (there is no ``scripts.naming``). See docs/device-naming.md.
from netbox_rack_design.naming import pending_names


def _family_names(placement, prefix):
    """Every existing name in the ``<prefix>...`` family that matters for a
    counter: persisted devices, this design's other placements, and unsaved
    same-session siblings. Read-only."""
    from dcim.models import Device

    from netbox_rack_design.models import DesignPlacement

    names = list(
        Device.objects.filter(name__startswith=prefix).values_list("name", flat=True)
    )
    names += list(
        DesignPlacement.objects.filter(design=placement.design)
        .exclude(pk=placement.pk)
        .values_list("proposed_name", flat=True)
    )
    names += pending_names(placement)
    return names


def _next_number(placement, prefix):
    """Continue a flat numbered family: return ``<prefix><max+1>``."""
    tail = re.compile(r"^" + re.escape(prefix) + r"(\d+)$")
    highest = 0
    for name in _family_names(placement, prefix):
        m = tail.match(name or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return str(highest + 1)


def _next_pdu_slot(placement, prefix):
    """Continue an A/B phase-paired family: a1, b1, a2, b2, a3, ..."""
    slot = re.compile(r"^" + re.escape(prefix) + r"([ab])(\d+)$", re.IGNORECASE)

    def ordinal(letter, num):
        # a1->1, b1->2, a2->3, b2->4, ...
        return (num - 1) * 2 + (1 if letter.lower() == "a" else 2)

    highest = 0
    for name in _family_names(placement, prefix):
        m = slot.match(name or "")
        if m:
            highest = max(highest, ordinal(m.group(1), int(m.group(2))))
    nxt = highest + 1
    return ("a" if nxt % 2 == 1 else "b") + str((nxt + 1) // 2)


def _role_slug(placement):
    """The placement's role slug: the chosen role for an add, the real device's
    role for a move/remove. Empty string when none is set."""
    role = placement.device_role or (
        placement.device.role if placement.device else None
    )
    return (role.slug if role else "").lower()


def _site_slug(placement):
    """Lowercased site name from the target rack (falling back to the real
    device's site). Empty string when unknown."""
    rack = placement.target_rack or (
        placement.device.rack if placement.device else None
    )
    site = rack.site if (rack and rack.site) else (
        placement.device.site if placement.device else None
    )
    return (site.name if site else "").lower().replace(" ", "-")


def _rack_token(placement):
    """A compact, punctuation-free rack token for embedding in a name."""
    rack = placement.target_rack or (
        placement.device.rack if placement.device else None
    )
    name = rack.name if rack else ""
    return re.sub(r"[^0-9a-z]", "", name.lower())


def build_name(placement):
    """Compute the proposed name for ``placement``.

    Rules (all generic -- adjust to your convention):

    1. **PDUs** (role slug contains ``pdu``): ``<site>-pdu-r<rack>-a1/b1/...``
       -- A/B phase pairs.
    2. **Everything else**: ``<site>-<role>-<n>`` -- a flat family counter,
       falling back to the device-type slug when no role is set so the name is
       never left with an empty segment.
    """
    site = _site_slug(placement) or "site"
    role = _role_slug(placement)

    # Rule 1 -- PDUs: phase-paired slots per rack.
    if "pdu" in role:
        prefix = f"{site}-pdu-r{_rack_token(placement)}-"
        return prefix + _next_pdu_slot(placement, prefix)

    # Rule 2 -- general: <site>-<role|type>-<n>.
    if not role:
        dt = placement.device_type or (
            placement.device.device_type if placement.device else None
        )
        role = (dt.slug if dt else "") or "dev"
    prefix = f"{site}-{role}-"
    return prefix + _next_number(placement, prefix)
