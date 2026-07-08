"""
Naming-convention engine for NetBox Rack Design (Phase 1).

This module computes the *proposed name* for a ``DesignPlacement`` without ever
writing to ``dcim``. It is strictly read-only over real NetBox data: it builds a
string and (for collision warnings) issues read-only queries.

Three modes are supported, selected by the plugin config key ``naming_mode``
(read via ``get_plugin_config``):

``sequence`` (default)
    ``f"{design.title}-{n}"`` where ``n`` is the placement's 1-based ordinal
    within its design (see :func:`placement_ordinal`).

``template``
    A single-brace ``str.format``-style string (config key ``naming_template``)
    using **dotted attribute paths on real NetBox model objects** -- NOT flat
    aliases. The template is rendered against the context produced by
    :func:`_build_context`, whose root objects are documented in
    :data:`AVAILABLE_CONTEXT`:

    * ``design`` -- the ``Design`` instance, wrapped so that ``{design.name}``
      resolves to its ``title`` (the model has no ``name`` field). Every real
      attribute is still reachable: ``{design.title}``, ``{design.site.name}``,
      ``{design.sequence}``, ...
    * ``device`` -- for ``move``/``remove`` placements, the real
      ``placement.device`` (full ``dcim.Device`` attribute tree). For an ``add``,
      a lightweight placement-backed proxy exposing the SAME attribute paths
      resolved from the placement (``{device.site.name}``,
      ``{device.device_type.model}``, ``{device.rack.name}``,
      ``{device.role.name}``, ``{device.tenant.name}``, ``{device.position}``,
      ``{device.face}``, ``{device.name}``).
    * ``n`` -- the ordinal.

    Traversal is *safe*: a missing/blank attribute (or any
    ``AttributeError``/``KeyError``/``IndexError``/``TypeError``) renders as the
    empty string and never raises. Only attribute/index access (the default
    ``string.Formatter`` behaviour) is supported.

``script``
    Import the dotted path in config key ``naming_script`` to a callable
    ``fn(placement) -> str`` and return its result. A clear ``ValueError`` is
    raised if the path is empty, unimportable, or not callable.

The module is import-safe: no database access happens at import time.
"""

import string

from django.utils.module_loading import import_string
from netbox.plugins import get_plugin_config

__all__ = (
    "DEFAULT_NAMING_MODE",
    "DEFAULT_NAMING_TEMPLATE",
    "AVAILABLE_CONTEXT",
    "generate_name",
    "placement_ordinal",
    "name_exists_in_site",
)

PLUGIN_NAME = "netbox_rack_design"

DEFAULT_NAMING_MODE = "sequence"
DEFAULT_NAMING_TEMPLATE = "{design.name}-{n}"

#: Documents the root objects a ``template``-mode naming string may reference, so
#: a later UI/help text can surface what users may use. Maps each root token to a
#: human description and a few representative dotted paths.
AVAILABLE_CONTEXT = {
    "design": {
        "description": "The Design being planned.",
        "examples": [
            "{design.name}",  # alias for title
            "{design.title}",
            "{design.site.name}",
            "{design.sequence}",
        ],
    },
    "device": {
        "description": (
            "The placement's device. For move/remove this is the real "
            "dcim.Device; for an add it is a placement-backed proxy exposing "
            "the same attribute paths."
        ),
        "examples": [
            "{device.name}",
            "{device.site.name}",
            "{device.rack.name}",
            "{device.device_type.model}",
            "{device.role.name}",
            "{device.tenant.name}",
            "{device.position}",
            "{device.face}",
        ],
    },
    "n": {
        "description": "The placement's 1-based ordinal within its design.",
        "examples": ["{n}"],
    },
}


class _SafeFormatter(string.Formatter):
    """
    A ``string.Formatter`` whose field resolution never raises: a missing or
    blank attribute (or any traversal error) becomes the empty string. Only the
    default attribute/index access is supported.
    """

    def get_field(self, field_name, args, kwargs):
        try:
            obj, used_key = super().get_field(field_name, args, kwargs)
        except (AttributeError, KeyError, IndexError, TypeError):
            return "", field_name
        return obj, used_key

    def format_field(self, value, format_spec):
        if value is None:
            return ""
        try:
            return super().format_field(value, format_spec)
        except (ValueError, TypeError):
            return ""


_FORMATTER = _SafeFormatter()


class _DesignProxy:
    """
    Wraps a ``Design`` so ``{design.name}`` resolves to its ``title`` (the model
    has no ``name`` field). All other attributes delegate to the real design.
    """

    def __init__(self, design):
        self._design = design

    @property
    def name(self):
        return self._design.title

    def __getattr__(self, item):
        return getattr(self._design, item)


class _AddDevicePlaceholderProxy:
    """
    A placement-backed stand-in for a not-yet-existing device (kind=add),
    exposing the same dotted attribute paths a real ``dcim.Device`` would, so the
    same templates work for adds and for existing devices.
    """

    def __init__(self, placement):
        self._placement = placement

    @property
    def name(self):
        return self._placement.proposed_name

    @property
    def device_type(self):
        return self._placement.device_type

    @property
    def role(self):
        return self._placement.device_role

    @property
    def tenant(self):
        return self._placement.tenant

    @property
    def site(self):
        return self._placement.design.site

    @property
    def rack(self):
        return self._placement.target_rack

    @property
    def position(self):
        return self._placement.target_position

    @property
    def face(self):
        return self._placement.target_face


def _build_context(placement, n):
    """Build the template render context for a placement."""
    if placement.device_id:
        device = placement.device
    else:
        device = _AddDevicePlaceholderProxy(placement)
    return {
        "design": _DesignProxy(placement.design),
        "device": device,
        "n": n,
    }


def placement_ordinal(placement):
    """
    Return the placement's 1-based ordinal among its design's placements in model
    order (``Meta.ordering`` = design, target_position, pk).

    A single query; pass ``index`` to :func:`generate_name` to avoid it entirely.
    """
    pks = list(placement.design.placements.values_list("pk", flat=True))
    try:
        return pks.index(placement.pk) + 1
    except ValueError:
        # Unsaved placement (or not yet attached): it would sort last.
        return len(pks) + 1


def _run_script(placement):
    """Resolve and invoke the configured ``naming_script`` callable."""
    path = get_plugin_config(PLUGIN_NAME, "naming_script", "")
    if not path:
        raise ValueError(
            "naming_mode is 'script' but no 'naming_script' dotted path is configured."
        )
    try:
        fn = import_string(path)
    except ImportError as exc:
        raise ValueError(f"Could not import naming_script '{path}': {exc}") from exc
    if not callable(fn):
        raise ValueError(f"naming_script '{path}' is not callable.")
    return fn(placement)


def generate_name(placement, *, index=None):
    """
    Compute the proposed name for ``placement`` per the configured naming mode.

    ``index`` -- optional pre-computed ordinal; pass it to avoid the
    :func:`placement_ordinal` query when iterating a batch.

    Never writes to ``dcim`` and never suffixes/mutates for collisions (callers
    use :func:`name_exists_in_site` to warn).
    """
    mode = get_plugin_config(PLUGIN_NAME, "naming_mode", DEFAULT_NAMING_MODE)
    n = index if index is not None else placement_ordinal(placement)

    if mode == "template":
        template = get_plugin_config(
            PLUGIN_NAME, "naming_template", DEFAULT_NAMING_TEMPLATE
        )
        context = _build_context(placement, n)
        return _FORMATTER.vformat(template, (), context)

    if mode == "script":
        return _run_script(placement)

    # "sequence" (default) and any unrecognised mode fall back to the simple form.
    return f"{placement.design.title}-{n}"


def name_exists_in_site(name, site, *, exclude_placement=None):
    """
    Read-only collision check: return ``True`` if ``name`` is already used in
    ``site`` -- either by a real ``dcim.Device`` in that site, or by another
    ``DesignPlacement.proposed_name`` whose design targets the same site
    (excluding ``exclude_placement``).

    Performs no writes. Callers use this to WARN; the engine never resolves the
    collision itself.
    """
    if not name or site is None:
        return False

    from dcim.models import Device

    if Device.objects.filter(site=site, name=name).exists():
        return True

    from .models import DesignPlacement

    qs = DesignPlacement.objects.filter(proposed_name=name, design__site=site)
    if exclude_placement is not None and exclude_placement.pk:
        qs = qs.exclude(pk=exclude_placement.pk)
    return qs.exists()
