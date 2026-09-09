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
    ``fn(placement) -> str`` and return its result. If the path is empty,
    unimportable, or not callable -- OR the script raises while computing a
    name -- :func:`generate_name` logs a warning and **falls back to the
    built-in ``sequence`` name** so a mis-configured or buggy script never
    breaks name preview.

A device name is stored ONLY when the plan actually changes it
----------------------------------------------------------------
``DesignPlacement.proposed_name`` is a first-class, non-empty value only when
this design is proposing to CHANGE the device's name: an ``add`` (there is no
prior name) or a ``move`` that renames. A ``move`` that keeps the device's own
name stores an empty ``proposed_name`` -- there is nothing to write down, and
nothing to strip back off later. Any "<design title>-<real name>" decoration a
planner sees on a keep-name tile is produced at RENDER time
(``projection.py``), never stored here: see that module's docstring for the
full rendering table. This module therefore never manufactures, strips, or
"settles" a prefix -- :func:`chain_placement_names` and
:func:`name_exists_in_site` compare EFFECTIVE names (``proposed_name`` or,
absent one, the device's real name) directly, with no per-design token to
resolve first.

Pending (in-editor, unsaved) sibling names
------------------------------------------
Two placements previewed in ONE editor session are invisible to each other in
the database, so a purely DB-driven "next number" hands both the SAME name
(confirmed live, 2026-07-10: two same-family palette adds both got
``dra4-dcs7010t-46``). The preview API therefore stamps the client-supplied
list of names already assigned in the session onto the (unsaved) placement as
``placement._rd_pending_names``; :func:`pending_names` surfaces it (default
``[]``). The built-in ``sequence`` mode consults it, and ``script``-mode
callables SHOULD too when they compute family counters::

    from netbox_rack_design.naming import pending_names
    for name in pending_names(placement):
        ...  # count it exactly like a persisted sibling's proposed_name

The module is import-safe: no database access happens at import time.
"""

import logging
import re
import string

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string
from netbox.plugins import get_plugin_config

from . import planning_fields
from .choices import DesignPlacementKindChoices

logger = logging.getLogger("netbox_rack_design.naming")

__all__ = (
    "DEFAULT_NAMING_MODE",
    "DEFAULT_NAMING_TEMPLATE",
    "DEFAULT_NAMING_OPTIONS",
    "AVAILABLE_CONTEXT",
    "IDS_TOKEN_RE",
    "chain_placement_names",
    "effective_name",
    "generate_name",
    "naming_config",
    "peer_name_claims",
    "pending_names",
    "placement_ordinal",
    "name_exists_in_site",
    "validate_naming_config",
)

PLUGIN_NAME = "netbox_rack_design"

DEFAULT_NAMING_MODE = "sequence"
DEFAULT_NAMING_TEMPLATE = "{design.name}-{n}"

#: The ticket/project token a design title is expected to carry. Exposed for
#: any naming script that wants to build a project-scoped name (a deployment's
#: own naming script, for instance, uses this to build an ``"IDS-<n>-"``
#: family prefix) without re-deriving the same regex.
IDS_TOKEN_RE = re.compile(r"IDS-?(\d+)", re.IGNORECASE)

#: The ``naming`` config sub-dict and its defaults. Currently empty: reserved
#: for future per-design naming options, validated the same way
#: ``naming_mode``/``naming_template``/``naming_script`` are -- an unknown key
#: (a typo) is rejected rather than silently ignored (see
#: :func:`validate_naming_config`).
DEFAULT_NAMING_OPTIONS = {}

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

    @property
    def cf(self):
        """The custom fields this planned device WILL have, keyed the way a
        real ``dcim.Device.cf`` is.

        A planned add has no device to read custom fields from, so the values
        come from the placement's ``planning_data`` -- mapped from the
        plugin-internal descriptor keys back to the deployment's real custom
        field names via each descriptor's ``target``. That mapping is what makes
        ``{device.cf[hw_class]}`` mean the same thing for an add as it does for
        an existing device. Descriptors with a non-``cf.`` target are skipped:
        they land on a native attribute, not in ``cf``.
        """
        data = self._placement.planning_data or {}
        out = {}
        for field in planning_fields.placement_field_schema():
            target = field.get("target") or ""
            if not target.startswith("cf."):
                continue
            out[target[3:]] = data.get(field["key"])
        return out


class _MoveDeviceProxy:
    """
    A move-placement-backed stand-in for the device once it LANDS, mirroring
    :class:`_AddDevicePlaceholderProxy`'s shape but for ``kind=move``.

    ``{device.role}`` / ``{device.tenant}`` must resolve to what the device
    WILL be -- the placement's override when set, the carry-over source's own
    value otherwise (:meth:`DesignPlacement.resolved_role` /
    :meth:`.resolved_tenant`) -- and ``{device.rack}`` / ``{device.position}``
    / ``{device.face}`` must resolve to WHERE it is going (the placement's
    ``target_*``), never where it is now. Everything else (``name``,
    ``device_type``, ``site``, ...) delegates to the real device -- or, for a
    move acting on an ancestor design's still-planned 'add' (no real device
    yet), to that ancestor placement via the same add-placeholder proxy.
    """

    def __init__(self, placement):
        self._placement = placement
        if placement.device_id:
            self._identity = placement.device
        else:
            self._identity = _AddDevicePlaceholderProxy(placement.base_placement)

    @property
    def role(self):
        return self._placement.resolved_role()

    @property
    def tenant(self):
        return self._placement.resolved_tenant()

    @property
    def rack(self):
        return self._placement.target_rack

    @property
    def position(self):
        return self._placement.target_position

    @property
    def face(self):
        return self._placement.target_face

    def __getattr__(self, item):
        return getattr(self._identity, item)


def _build_context(placement, n):
    """Build the template render context for a placement."""
    if placement.kind == DesignPlacementKindChoices.KIND_MOVE and (
        placement.device_id or placement.base_placement_id
    ):
        device = _MoveDeviceProxy(placement)
    elif placement.device_id:
        device = placement.device
    else:
        device = _AddDevicePlaceholderProxy(placement)
    return {
        "design": _DesignProxy(placement.design),
        "device": device,
        "n": n,
    }


def pending_names(placement):
    """
    Names already assigned in the CURRENT editor session (unsaved siblings),
    as injected by the preview API onto ``placement._rd_pending_names``.
    Returns ``[]`` when nothing was injected. Naming scripts should treat
    these exactly like persisted siblings' ``proposed_name`` values when
    computing family counters (see the module docstring).
    """
    return list(getattr(placement, "_rd_pending_names", None) or [])


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
    """Resolve and invoke the configured ``naming_script`` callable.

    Raises ``ValueError`` if the configured path is empty, unimportable, or not
    callable. Any exception the script itself raises propagates unchanged. The
    caller (:func:`generate_name`) is responsible for turning these into a safe
    fallback name so a mis-configured or buggy script never breaks name preview.
    """
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


def _sequence_name(placement, n):
    """The built-in default: ``"<design title>-<n>"``, bumped past any PENDING
    (same-session, unsaved) sibling already holding an ordinal in this design's
    ``"<title>-<digits>"`` family, so two previews in one session never collide
    (user bug 2026-07-10; see the module docstring)."""
    family = re.compile(r"^" + re.escape(placement.design.title) + r"-(\d+)$")
    highest_pending = 0
    for name in pending_names(placement):
        match = family.match(name or "")
        if match:
            highest_pending = max(highest_pending, int(match.group(1)))
    if highest_pending >= n:
        n = highest_pending + 1
    return f"{placement.design.title}-{n}"


def generate_name(placement, *, index=None):
    """
    Compute the proposed name for ``placement`` per the configured naming mode.

    ``index`` -- optional pre-computed ordinal; pass it to avoid the
    :func:`placement_ordinal` query when iterating a batch.

    Never writes to ``dcim`` and never suffixes/mutates for collisions (callers
    use :func:`name_exists_in_site` to warn).

    Robust to a broken ``script`` mode: if the configured ``naming_script``
    cannot be resolved (wrong/empty dotted path, not importable, not callable)
    OR the script raises while computing a name, this **falls back to the
    built-in default** :func:`_sequence_name` and logs a warning -- a
    mis-configured or buggy naming script degrades to sensible default names
    rather than breaking name preview (and it is only ever reached from the
    read-only preview endpoint, so nothing else is affected).
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
        try:
            return _run_script(placement)
        except Exception:  # noqa: BLE001 - any failure degrades to the default
            path = get_plugin_config(PLUGIN_NAME, "naming_script", "")
            logger.warning(
                "naming_script %r failed; falling back to the default sequence "
                "name. Fix the 'naming_script' plugin config to restore custom "
                "naming.", path, exc_info=True,
            )
            return _sequence_name(placement, n)

    # "sequence" (default) and any unrecognised mode.
    return _sequence_name(placement, n)


# --- the "naming" config sub-dict (currently unused, reserved) -------------


def naming_config():
    """The validated ``naming`` config sub-dict, with defaults filled in.

    Raises ``ImproperlyConfigured`` for a non-dict value, an unknown key (a typo
    must not silently disable a future option) or a non-string value.
    """
    options = get_plugin_config(PLUGIN_NAME, "naming", None)
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ImproperlyConfigured(
            f"netbox_rack_design: the 'naming' config option must be a dict of "
            f"naming options, got {type(options).__name__}."
        )
    known = ", ".join(sorted(DEFAULT_NAMING_OPTIONS))
    unknown = sorted(set(options) - set(DEFAULT_NAMING_OPTIONS))
    if unknown:
        raise ImproperlyConfigured(
            f"netbox_rack_design: unknown naming option(s): {', '.join(unknown)}. "
            f"Known options: {known}."
        )
    resolved = {}
    for key, default in DEFAULT_NAMING_OPTIONS.items():
        value = options.get(key, default)
        if not isinstance(value, str):
            raise ImproperlyConfigured(
                f"netbox_rack_design: naming[{key!r}] must be a string, got "
                f"{type(value).__name__}."
            )
        resolved[key] = value.strip()
    return resolved


def validate_naming_config():
    """Startup check for the ``naming`` sub-dict (see :func:`naming_config`).

    Registered in ``RackdesignConfig._rd_startup_checks()`` so a malformed value
    fails the boot with a clear message instead of surfacing much later.
    """
    return naming_config()


def _naming_chain(design):
    """The ancestor designs a family counter in ``design`` must respect.

    ``[]`` -- meaning "this design alone" -- for a design with no parent, for a
    lineage that cannot be resolved, and for a chain carrying an ancestor that
    is not APPROVED (in which case the WHOLE chain drops, not just the offending
    link: a layer is contributed whole or not at all, and every layer stacked on
    a broken one was planned against ITS result).

    That verdict is not re-decided here -- it is
    ``projection.resolve_baseline_chain``, the one answer to "which ancestors
    does this design's world include" (PLAN-design-chains.md Sec 9.2). If the
    counter held a second, slightly different notion of the chain, a child would
    hand out numbers that dodge placements its rack is not rendering, or reuse
    names from placements it IS. Imported lazily so this module keeps costing
    nothing at import time.

    A refusal is logged, never silent: it is the difference between "the family
    continues at 6" and "the family restarts at 1", and the planner already has
    the same sentence on screen as the baseline panel's conflict row.
    """
    from .projection import resolve_baseline_chain

    chain, refusal = resolve_baseline_chain(design)
    if refusal is not None:
        logger.warning(
            "Family counters in design %r fall back to this design alone, so a "
            "number an ancestor reserved may be reused: %s",
            str(design), refusal.get("detail") or refusal.get("kind"),
        )
    return chain


def chain_placement_names(placement):
    """Every placement-held name a family counter for ``placement`` must count.

    The counter's sibling query spans **ancestors + self** and nothing else
    (PLAN-design-chains.md Sec 3.4):

    * every row (this design's own, and every ancestor's) contributes its
      EFFECTIVE name -- ``proposed_name`` when the row renamed the device (or
      is an ``add``), otherwise the real device's own name. There is no
      planning prefix to strip: the row's ``proposed_name`` IS the name that
      design proposes, full stop;
    * a keep-name move therefore contributes the device's real name to the
      count instead of an empty string -- a defect the old prefix-and-settle
      scheme had, because an empty ``proposed_name`` matched no family regex
      and a script counting a family silently never saw that row;
    * **siblings are NOT included.** Two children of one parent are blind to
      each other by design (Sec 2.1); the resolution is "first approved wins,
      the other re-bases". So the counter may propose a name a sibling already
      took, and that surfaces through :func:`name_exists_in_site` -- which
      matches every placement whose design targets the site, sibling included
      -- as the ordinary non-blocking collision warning, not as a silent
      clash.

    A single query regardless of chain depth: ``design_id__in`` covers self and
    every ancestor together, and ``select_related("device")`` means reading a
    move/remove row's real name costs no further query.

    Returns raw values, ``None`` included, exactly as the ``values_list`` it
    replaces did -- every caller already does ``pattern.match(name or "")``.
    """
    from .models import DesignPlacement

    design = placement.design
    ancestors = _naming_chain(design)
    design_ids = [design.pk, *(ancestor.pk for ancestor in ancestors)]

    rows = (
        DesignPlacement.objects.filter(design_id__in=design_ids)
        .exclude(pk=placement.pk)
        .select_related("device")
    )
    return [
        row.proposed_name or (row.device.name if row.device_id else None)
        for row in rows
    ]


def name_exists_in_site(name, site, *, exclude_placement=None):
    """
    Read-only collision check: return ``True`` if ``name`` is already used in
    ``site`` -- either by a real ``dcim.Device``, or by another
    ``DesignPlacement.proposed_name`` equal to ``name`` whose design targets
    the same site (excluding ``exclude_placement``).

    Performs no writes. Callers use this to WARN; the engine never resolves the
    collision itself.

    Two queries, regardless of how many designs or how deep any chain is: a
    ``dcim.Device`` existence check, and a ``DesignPlacement`` existence check.
    """
    if not name or site is None:
        return False

    from dcim.models import Device

    from .models import DesignPlacement

    if Device.objects.filter(site=site, name=name).exists():
        return True

    qs = DesignPlacement.objects.filter(design__site=site, proposed_name=name)
    if exclude_placement is not None and exclude_placement.pk:
        qs = qs.exclude(pk=exclude_placement.pk)
    return qs.exists()


def effective_name(placement):
    """The name ``placement`` claims, if any: its ``proposed_name``, or (a
    keep-name move) its real device's own name. ``None`` for a not-yet-named
    add and for a remove (a removal claims no name going forward).

    The same effective-name rule :func:`chain_placement_names` inlines for its
    own rows, factored out so :func:`peer_name_claims` compares by the exact
    same definition rather than a second one that could drift.
    """
    if placement.proposed_name:
        return placement.proposed_name
    if placement.kind == DesignPlacementKindChoices.KIND_MOVE and placement.device_id:
        return placement.device.name or None
    return None


def peer_name_claims(placement, peer_placements):
    """Which of ``peer_placements`` claim the SAME effective name as
    ``placement`` -- the peer-aware sibling of :func:`name_exists_in_site`
    (PLAN-peer-conflicts.md P3).

    ``name_exists_in_site`` answers only True/False and, by design, does not
    exclude a design's own lineage or other versions of the same plan --
    its only consumer (``preview_name``) depends on exactly that
    design-blind semantics, so it is not touched here. This function instead
    takes an ALREADY-FILTERED peer list (the caller -- ``projection.py``'s
    peer query -- has already dropped ancestors, descendants, version
    siblings and implemented designs, P2) and reports WHICH of them claim the
    name, so the message can say "design C claims this name" instead of
    merely "taken".

    Performs no query of its own: ``peer_placements`` is the caller's single
    per-rack fetch, and comparison is plain Python string equality over each
    row's :func:`effective_name`.
    """
    name = effective_name(placement)
    if not name:
        return []
    return [peer for peer in peer_placements if effective_name(peer) == name]
