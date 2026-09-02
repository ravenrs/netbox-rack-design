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

Settled names (a design chain)
-----------------------------
``proposed_name`` carries two things at once: the *planning* name inside the
design that owns the change (``IDS-1234_old_name``, whose prefix marks "this
device is touched by that project") and the *settled* name the device ends up
with once that design is done (``old_name``). Inside one design they are the
same string; the moment a design B baselines on A they diverge, because from B's
point of view A's move has already happened and A's prefix is A's bookkeeping,
not part of the device's identity (PLAN-design-chains.md Sec 3).

:func:`settled_name` is therefore a FOURTH entry point alongside the three
modes, configured under the ``naming`` config sub-dict::

    "naming": {
        # where the planning prefix token comes from; omitted => derive from
        # the design title
        "prefix_source": "cf.<your project field>",
        # dotted path to fn(placement) -> str; omitted => the builtin
        "settled_name": "",
    }

The prefix is a PROJECT name, not derivable from a single global convention, and
no custom-field name is ever hardcoded in the plugin: ``prefix_source`` is a
dotted path resolved against the design through
:func:`netbox_rack_design.planning_fields.resolve_source` -- the same resolver
the power dialogs use. The builtin strips a leading ``<token>[-_]`` from
``proposed_name`` exactly ONCE, so a repeated token survives and a chain three
designs deep still strips one prefix per layer.

Unlike the three name-GENERATION modes, a settled-name failure never degrades to
a fallback string -- :func:`settled_name` raises :class:`SettledNameError`, and
:func:`settled_name_status` returns ``(None, {"state": "failed", ...})`` for a
caller that must not raise mid-render. See :class:`SettledNameError` for why.

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

logger = logging.getLogger("netbox_rack_design.naming")

__all__ = (
    "DEFAULT_NAMING_MODE",
    "DEFAULT_NAMING_TEMPLATE",
    "DEFAULT_NAMING_OPTIONS",
    "AVAILABLE_CONTEXT",
    "IDS_TOKEN_RE",
    "SettledNameError",
    "chain_placement_names",
    "derive_prefix_token",
    "generate_name",
    "naming_config",
    "pending_names",
    "placement_ordinal",
    "name_exists_in_site",
    "prefix_token",
    "settled_name",
    "settled_name_status",
    "strip_planning_prefix",
    "validate_naming_config",
)

PLUGIN_NAME = "netbox_rack_design"

DEFAULT_NAMING_MODE = "sequence"
DEFAULT_NAMING_TEMPLATE = "{design.name}-{n}"

#: The ticket/project token a design title is expected to carry, as
#: ``naming_example.build_name`` looks for it. Shared so the token a
#: name is BUILT with and the one stripped back off cannot drift apart.
IDS_TOKEN_RE = re.compile(r"IDS-?(\d+)", re.IGNORECASE)

#: The ``naming`` config sub-dict and its defaults. An empty ``prefix_source``
#: means "derive the token from the design title"; an empty ``settled_name``
#: means "use the builtin".
DEFAULT_NAMING_OPTIONS = {
    "prefix_source": "",
    "settled_name": "",
}

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


# --- settled names across a design chain (PLAN-design-chains.md Sec 3) -------


class SettledNameError(RuntimeError):
    """A settled name could not be determined.

    Raised instead of returning a plausible-but-wrong name. The three name
    GENERATION modes degrade to a default when a script breaks, because a
    preview name is cosmetic and always visible to the planner who typed it. A
    settled name is not cosmetic: it is the identity a child design's rack
    renders and generates against, so a quietly wrong one corrupts every
    downstream name with nothing on screen to reveal it.
    """


def naming_config():
    """The validated ``naming`` config sub-dict, with defaults filled in.

    Raises ``ImproperlyConfigured`` for a non-dict value, an unknown key (a typo
    such as ``prefix_soruce`` must not silently disable the feature) or a
    non-string value. Returns ``{"prefix_source": ..., "settled_name": ...}``,
    both stripped.
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
    fails the boot with a clear message instead of surfacing much later, as a
    missing prefix strip, on one placement.
    """
    return naming_config()


def derive_prefix_token(design):
    """The planning prefix derived from the design TITLE.

    The fallback used when no ``prefix_source`` is configured. This is the ONE
    implementation of that derivation: ``naming_example.build_name`` builds its
    server family prefix from it too, so the token a name is generated WITH and
    the token stripped back OFF can never drift apart.

    ``"Network sweep IDS-1000"`` and ``"ids1000 rebuild"`` both give
    ``"IDS-1000"``; a title with no ticket number gives ``"IDS-<title>"``.
    """
    title = design.title or ""
    match = IDS_TOKEN_RE.search(title)
    return f"IDS-{match.group(1) if match else title}"


def prefix_token(design):
    """The planning prefix token for ``design``.

    Resolved from the config-declared ``naming["prefix_source"]`` -- a dotted
    path relative to the DESIGN, resolved through
    :func:`netbox_rack_design.planning_fields.resolve_source`, the same one
    resolver the power dialogs use. The optional leading ``design.`` root token
    is accepted (``"design.cf.<field>"`` == ``"cf.<field>"``), mirroring
    ``{design.x}`` in a naming template. No custom-field name is hardcoded
    anywhere in the plugin: the prefix is a project name, and which field holds
    it is deployment-specific.

    With no ``prefix_source`` configured, falls back to
    :func:`derive_prefix_token`.

    Raises :class:`SettledNameError` when a configured path resolves to nothing
    -- never a quiet fallback to the title, which would hand back a plausible
    wrong name.
    """
    source = naming_config()["prefix_source"]
    if not source:
        return derive_prefix_token(design)

    path = source[len("design."):] if source.startswith("design.") else source
    value = None if path in ("", "design") else planning_fields.resolve_source(design, path)
    token = "" if value is None else str(value).strip()
    if not token:
        raise SettledNameError(
            f"naming['prefix_source'] = {source!r} resolved to no value on design "
            f"{design.title!r} (pk={design.pk}); the planning prefix cannot be "
            f"determined, so no settled name is produced. Fix the configured "
            f"source path, or give the design a value there."
        )
    return token


def strip_planning_prefix(name, token):
    """Strip a leading ``<token>`` + one ``-``/``_`` separator from ``name``.

    Applied EXACTLY ONCE (rule R3): a device genuinely called
    ``IDS-1000_x`` inside project ``IDS-1000`` settles to ``IDS-1000_x``, not to
    ``x``.

    The rules the strip must not get wrong, each one a silent name corruption:

    * the separator is REQUIRED, so a longer token that merely starts with this
      one (``IDS-10005_x`` against ``IDS-1000``) is left alone;
    * the token is matched LITERALLY -- a project name may contain regex
      metacharacters;
    * the match is ANCHORED, so a token inside the identity survives;
    * exactly ONE separator is consumed, so the result is always a suffix of the
      name the planner can see;
    * the match is CASE-INSENSITIVE -- the same project is written both ways in
      the wild (a title-derived token is upper-cased, a hand-typed planning name
      often is not), and failing to strip a differently-cased same token would
      leave a planning prefix on an inherited placement with nothing on screen
      to say so;
    * a name that is nothing but the prefix keeps its identity rather than
      settling to the empty string.
    """
    if not name:
        return ""
    if not token:
        return name
    match = re.match(re.escape(token) + r"[-_]", name, re.IGNORECASE)
    if not match:
        return name
    return name[match.end():] or name


def _builtin_settled_name(placement):
    """The default settled-name hook: strip this design's planning prefix."""
    token = prefix_token(placement.design)
    return strip_planning_prefix(placement.proposed_name or "", token)


def settled_name(placement):
    """The name ``placement``'s device ends up with once its design is done.

    The FOURTH naming entry point (see the module docstring), selected exactly
    like ``naming_mode``'s ``script``: a dotted path in ``naming["settled_name"]``
    to ``fn(placement) -> str``, defaulting to the builtin prefix strip.

    ``proposed_name`` is a PLANNING name -- it carries the owning design's
    project prefix. A child design that baselines on this one must render and
    generate against the settled name instead (rules R1/R2 of
    PLAN-design-chains.md Sec 3.2).

    Raises :class:`SettledNameError` if the answer cannot be determined (see
    that class for why there is no fallback), or ``ImproperlyConfigured`` for a
    malformed ``naming`` config. Callers that must not raise mid-render use
    :func:`settled_name_status`.
    """
    path = naming_config()["settled_name"]
    if not path:
        return _builtin_settled_name(placement)

    try:
        fn = import_string(path)
    except ImportError as exc:
        raise SettledNameError(
            f"naming['settled_name'] {path!r} could not be imported: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not callable(fn):
        raise SettledNameError(f"naming['settled_name'] {path!r} is not callable.")

    try:
        value = fn(placement)
    except SettledNameError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        raise SettledNameError(
            f"naming['settled_name'] {path!r} failed on placement "
            f"{placement.pk}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(value, str):
        raise SettledNameError(
            f"naming['settled_name'] {path!r} returned {type(value).__name__}, "
            f"not a string."
        )
    return value


def settled_name_status(placement):
    """:func:`settled_name` as ``(name, status)`` for a caller that must not raise.

    ``status`` is ``{"state": "ok"|"failed", "engine": "builtin"|"script",
    "detail": str}``, shaped like the distribution engine's status so a view can
    surface the failure in the UI. On failure ``name`` is ``None`` -- never a
    plausible substitute: the rule is "show the planner an error", not "carry on
    with a name that might be wrong".

    A malformed ``naming`` config still raises ``ImproperlyConfigured``; that is
    a boot-time deployment error (see :func:`validate_naming_config`), not a
    per-placement outcome.
    """
    engine = "script" if naming_config()["settled_name"] else "builtin"
    try:
        name = settled_name(placement)
    except SettledNameError as exc:
        logger.warning("settled_name failed (%s engine): %s", engine, exc)
        return None, {"state": "failed", "engine": engine, "detail": str(exc)}
    return name, {"state": "ok", "engine": engine, "detail": ""}

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

    * **self** contributes its placements' ``proposed_name`` -- the PLANNING
      names, exactly as before, because this design's own generated names live
      in the family this design's own prefix builds;
    * **each ancestor** contributes both its ``proposed_name`` AND its
      :func:`settled_name`. The settled name is the one that matters: an
      ancestor's row reads ``IDS-1234_ams1-sw-5``, which does not match the
      ``^ams1-sw-<digits>$`` family the child is counting, so widening the query
      without settling the name would find nothing and leave the collision in
      place while looking fixed. The planning name is kept too because it is
      equally reserved -- :func:`name_exists_in_site` warns on it -- and both
      only ever raise the counter's maximum, never lower it;
    * **siblings are NOT included.** Two children of one parent are blind to
      each other by design (Sec 2.1); the settled resolution is "first approved
      wins, the other re-bases". So the counter may propose a name a sibling
      already took, and that surfaces through
      :func:`name_exists_in_site` -- which matches every placement whose design
      targets the site, sibling included -- as the ordinary non-blocking
      collision warning, not as a silent clash.

    Costs one query for an unchained design (identical to the query it
    replaces), and for a chain one lineage hop per ancestor plus ONE placement
    query covering self and every ancestor together -- never one query per
    ancestor, and never one per name.

    A single ancestor row whose settled name cannot be determined is reported
    and counted under its planning name: refusing to name anything would block
    a planner who cannot fix an upstream design from here, and skipping the row
    silently could hand out a name that is already taken. The failure is
    surfaced properly where the chain is rendered (``settled_name_status``).

    Returns raw values, ``None`` included, exactly as the ``values_list`` it
    replaces did -- every caller already does ``pattern.match(name or "")``.
    """
    from .models import DesignPlacement

    design = placement.design
    ancestors = _naming_chain(design)
    if not ancestors:
        # No chain: byte-identical to the query this function replaced, and no
        # lineage walk to pay for.
        return list(
            DesignPlacement.objects.filter(design=design)
            .exclude(pk=placement.pk)
            .values_list("proposed_name", flat=True)
        )

    designs = {ancestor.pk: ancestor for ancestor in ancestors}
    rows = DesignPlacement.objects.filter(
        design_id__in=[design.pk, *designs]
    ).exclude(pk=placement.pk)

    names = []
    for row in rows:
        names.append(row.proposed_name)
        ancestor = designs.get(row.design_id)
        if ancestor is None:
            continue  # This design's own row: its planning name IS its name.
        # Hand the row its already-loaded design so the hook cannot turn into
        # one query per row.
        row.design = ancestor
        try:
            names.append(settled_name(row))
        except SettledNameError as exc:
            logger.warning(
                "Family counter: no settled name for placement %s (%r) in "
                "ancestor design %r, so it is counted under its planning name "
                "and may not match the family: %s",
                row.pk, row.proposed_name, str(ancestor), exc,
            )
    return names


def name_exists_in_site(name, site, *, exclude_placement=None, design=None):
    """
    Read-only collision check: return ``True`` if ``name`` is already used in
    ``site``, on either of two planes:

    * the **planning** plane (unchanged from before this compared settled
      names): a real ``dcim.Device`` named ``name`` in ``site``, or another
      ``DesignPlacement.proposed_name`` equal to ``name`` whose design targets
      the same site (excluding ``exclude_placement``);
    * the **settled** plane (PLAN-design-chains.md Sec 3.4), in BOTH
      directions: an existing placement's :func:`settled_name` equals
      ``name``; or ``name``'s OWN settled form equals an existing placement's
      settled or planning name, or a real ``dcim.Device`` name in ``site``.

    Settling ``name`` needs a design (that is where the planning-prefix token
    comes from): ``exclude_placement.design`` is used when ``exclude_placement``
    is given, otherwise pass ``design`` explicitly. With neither, only the
    first direction above (an existing row settling to ``name``) is checked --
    every existing caller keeps working unchanged.

    Performs no writes. Callers use this to WARN; the engine never resolves the
    collision itself, and a false NEGATIVE here (see the prefilter note below)
    is acceptable on a non-blocking warning channel -- silently pretending
    completeness is not.

    Performance: this must not load every placement in the site, nor settle
    one row at a time with its own query. A narrow SQL prefilter
    (``proposed_name`` ending with the target string) selects a small
    candidate set, which is then settled in Python with each row's ``design``
    already attached via ``select_related`` -- exactly the pattern
    :func:`chain_placement_names` uses -- so the hook cannot turn into one
    query per row.

    Prefilter limitation (be honest about this): the ``endswith`` prefilter is
    EXACT for the builtin strip-prefix engine, whose settled name is always a
    suffix of ``proposed_name`` (see :func:`strip_planning_prefix`). A
    deployment's custom ``naming["settled_name"]`` callable can return
    anything -- unrelated to ``proposed_name`` entirely -- and such a
    collision can be MISSED by this prefilter. That is an accepted false
    negative on a warning channel, not silent completeness.

    A row whose settled name cannot be determined (:class:`SettledNameError`)
    never raises out of this function: it is logged (via
    :func:`settled_name_status`) and that row is simply not counted as a
    settled-plane match -- it is still caught by the ordinary literal
    ``proposed_name`` check above, i.e. it is effectively compared under its
    planning name only.
    """
    if not name or site is None:
        return False

    from dcim.models import Device

    from .models import DesignPlacement

    settle_design = design if design is not None else (
        exclude_placement.design if exclude_placement is not None else None
    )

    # The candidate's own settled form, when we know which design it belongs
    # to (needed to resolve that design's planning-prefix token).
    settled_candidate = None
    if settle_design is not None:
        stand_in = DesignPlacement(design=settle_design, proposed_name=name)
        computed, status = settled_name_status(stand_in)
        if status["state"] == "ok" and computed:
            settled_candidate = computed

    check_names = {name}
    if settled_candidate:
        check_names.add(settled_candidate)

    if Device.objects.filter(site=site, name__in=check_names).exists():
        return True

    qs = DesignPlacement.objects.filter(design__site=site).select_related("design")
    if exclude_placement is not None and exclude_placement.pk:
        qs = qs.exclude(pk=exclude_placement.pk)

    # Literal planning-name match: `name` itself (the original behaviour) and,
    # if computable, the candidate's own settled form against another row's
    # raw planning name.
    if qs.filter(proposed_name__in=check_names).exists():
        return True

    # Settled-plane match: a row that itself settles to one of `check_names`.
    # See the prefilter limitation note in the docstring.
    from django.db.models import Q

    suffix_filter = Q()
    for target in check_names:
        suffix_filter |= Q(proposed_name__endswith=target)
    candidates = qs.exclude(proposed_name__in=check_names).filter(suffix_filter)
    for row in candidates:
        row_settled, row_status = settled_name_status(row)
        if row_status["state"] == "ok" and row_settled in check_names:
            return True

    return False
