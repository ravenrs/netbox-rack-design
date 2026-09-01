"""
Config-declared planning fields.

The plugin never hardcodes a custom-field name. A deployment declares, in
``PLUGINS_CONFIG``, which of *its* custom fields the planner may set or read,
and every layer -- editor inputs, validation, the naming engine, distribution
scripts -- goes through the descriptor schema declared here.

Two schemas share one descriptor grammar:

``planning_fields`` -- ``{role: [descriptor, ...]}``
    Values READ off an object that already exists (a rack's cf, a real PDU's
    cf). Each descriptor carries a ``source`` token saying where to read from.
    This is the older of the two; it feeds the rack/PDU power dialogs and the
    distribution scripts (docs/pdu-distribution-spec.md Sec 5). Its resolver
    used to be copy-pasted into each distribution script -- it lives here now,
    and the scripts import it.

``placement_fields`` -- ``[descriptor, ...]``
    Values a planner TYPES on a planned placement, stored in
    ``DesignPlacement.planning_data`` and destined for the real device when the
    design is applied. Each descriptor carries a ``target`` token saying where
    the value lands on that device. There is nothing to read from yet -- the
    device does not exist -- which is exactly why the value has to be stored.

Descriptor keys::

    key       required  the plugin-internal identifier; the dict key under
                        which the value is stored. Renaming a real custom
                        field in config never rewrites stored rows.
    label     optional  human label for the editor input; defaults to ``key``
    type      optional  "text" (default) | "number" | "choice"
    choices   required for type "choice"; a list of strings
    source    planning_fields only: where to READ the value from
    target    placement_fields only: where the value LANDS on the real device
    kinds     placement_fields only: which placement kinds may set the field;
              defaults to ("add",), mirroring the role/tenant rule
    rail      placement_fields only: offer it as a sticky default in the
              palette rail
    required  placement_fields only: the field must carry a value

Both ``source`` and ``target`` use the same token grammar: ``cf.<name>`` for a
custom field, any other dotted path for native attributes.
"""

from django.core.exceptions import ImproperlyConfigured, ValidationError
from netbox.plugins import get_plugin_config

PLUGIN_NAME = "netbox_rack_design"

FIELD_TYPES = ("text", "number", "choice")
DEFAULT_FIELD_TYPE = "text"
# Which placement kinds a placement_fields entry may be set on when the
# descriptor does not say. The two kinds that put a device somewhere: a brand
# new one, or an existing one the design relocates -- the same pair device_role
# and tenant are allowed on (models.DesignPlacement.clean). A removal takes
# none of them: re-attributing gear you are decommissioning means nothing.
DEFAULT_KINDS = ("add", "move")

__all__ = (
    "PLUGIN_NAME",
    "FIELD_TYPES",
    "coerce_value",
    "placement_field_schema",
    "planning_field_schema",
    "public_placement_field_schema",
    "read_for_slot",
    "read_planning_field",
    "read_planning_fields",
    "resolve_source",
    "validate_planning_data",
)


# --- reading a value off an existing object --------------------------------


def resolve_source(obj, source):
    """Resolve a ``cf.<name>`` / dotted-attribute token against ``obj``.

    Returns ``None`` for a missing value or an unresolvable path -- a planning
    field is always optional from the reader's point of view.
    """
    if not source or obj is None:
        return None
    parts = source.split(".")
    if parts[0] == "cf":
        if len(parts) != 2:
            return None
        # Prefer the stored dict over ``obj.cf``. The latter is a merged view
        # that re-queries CustomField for the object type on EVERY access --
        # three queries a call, which a per-slot caller multiplies by the whole
        # rack elevation (measured: 516 queries to render one editor page).
        # ``custom_field_data`` is already on the instance and carries the same
        # value. ``.cf`` stays as the fallback for objects that only expose it,
        # such as the proxy views the distribution scripts build.
        data = getattr(obj, "custom_field_data", None)
        if isinstance(data, dict):
            return data.get(parts[1])
        cf = getattr(obj, "cf", None) or {}
        return cf.get(parts[1])
    value = obj
    for part in parts:
        if value is None:
            return None
        value = getattr(value, part, None)
    return value


def read_planning_field(config, source, obj):
    """Resolve one ``planning_fields`` entry against ``obj``.

    ``config`` is the descriptor itself -- unused here, kept in the signature
    because the distribution scripts pass it and a future caller may want its
    ``type``.
    """
    return resolve_source(obj, source)


def read_planning_fields(role, obj):
    """Read every configured ``planning_fields[role]`` entry off ``obj``.

    Returns ``{key: value}``. A deployment's real custom-field names appear
    only in its config, never in any caller.
    """
    out = {}
    for field in planning_field_schema(role):
        out[field["key"]] = resolve_source(obj, field.get("source"))
    return out


def read_for_slot(device, planning_data):
    """The ``(label, value)`` pairs to show for one projected slot.

    Answers "what will this planning field be here once the design is done?",
    which has two possible sources and a fixed precedence between them:

    1. the placement's ``planning_data`` -- the value the design SETS. A planned
       add has only this; a move may carry it as an override.
    2. the real device's custom field, named by the descriptor's ``target``.

    So the override wins where the design states one, and everything else falls
    back to what the device already is. That precedence is what makes a move
    show its planned attribution rather than its current one.

    Ordered as the config declares. Unset values are omitted, not rendered
    blank.
    """
    planned = planning_data or {}
    out = []
    for field in placement_field_schema():
        value = planned.get(field["key"])
        if value is None or value == "":
            value = resolve_source(device, field.get("target"))
        if value is None or value == "":
            continue
        out.append((field["label"], value))
    return out


# --- schema loading + validation -------------------------------------------


def _validate_descriptor(entry, where, *, needs_source):
    """Normalise one descriptor, raising ImproperlyConfigured on a bad one.

    A malformed descriptor is a deployment mistake, and a silently ignored
    field is worse than a startup error: the planner would see an input that
    quietly stores nothing.
    """
    if not isinstance(entry, dict):
        raise ImproperlyConfigured(f"{where}: each entry must be a dict, got {type(entry).__name__}.")
    key = entry.get("key")
    if not key or not isinstance(key, str):
        raise ImproperlyConfigured(f"{where}: every entry needs a non-empty string 'key'.")

    field_type = entry.get("type") or DEFAULT_FIELD_TYPE
    if field_type not in FIELD_TYPES:
        raise ImproperlyConfigured(
            f"{where}[{key}]: type {field_type!r} is not one of {', '.join(FIELD_TYPES)}."
        )
    choices = entry.get("choices") or []
    if field_type == "choice":
        if not isinstance(choices, (list, tuple)) or not choices:
            raise ImproperlyConfigured(f"{where}[{key}]: type 'choice' requires a non-empty 'choices' list.")
        choices = [str(c) for c in choices]

    normalised = {
        "key": key,
        "label": entry.get("label") or key,
        "type": field_type,
        "choices": choices,
    }

    if needs_source:
        if not entry.get("source"):
            raise ImproperlyConfigured(f"{where}[{key}]: a 'source' token is required.")
        normalised["source"] = entry["source"]
        return normalised

    # placement_fields: the value is typed, not read.
    kinds = entry.get("kinds") or DEFAULT_KINDS
    if not isinstance(kinds, (list, tuple)) or not all(isinstance(k, str) for k in kinds):
        raise ImproperlyConfigured(f"{where}[{key}]: 'kinds' must be a list of placement-kind strings.")
    normalised.update({
        "target": entry.get("target") or "",
        "kinds": tuple(kinds),
        "rail": bool(entry.get("rail")),
        "required": bool(entry.get("required")),
    })
    return normalised


def planning_field_schema(role):
    """The validated ``planning_fields[role]`` descriptors, or ``[]``."""
    schema = get_plugin_config(PLUGIN_NAME, "planning_fields", {}) or {}
    entries = schema.get(role) or []
    return [
        _validate_descriptor(entry, f"planning_fields[{role!r}]", needs_source=True)
        for entry in entries
    ]


def placement_field_schema():
    """The validated ``placement_fields`` descriptors, or ``[]``."""
    entries = get_plugin_config(PLUGIN_NAME, "placement_fields", []) or []
    if not isinstance(entries, (list, tuple)):
        raise ImproperlyConfigured("placement_fields must be a list of descriptors.")
    return [
        _validate_descriptor(entry, "placement_fields", needs_source=False)
        for entry in entries
    ]


def public_placement_field_schema():
    """``placement_field_schema()`` without the deployment plumbing.

    ``target`` names a real custom field on the deployment's devices; it is how
    an apply step routes the value, not part of the contract an editor or API
    client needs. Everything else is published.
    """
    return [{k: v for k, v in field.items() if k != "target"} for field in placement_field_schema()]


# --- validating what a planner (or an API client) sent ----------------------


def coerce_value(field, value):
    """Coerce one submitted value to the descriptor's type.

    Raises ``ValueError`` with a human message when the value does not fit.
    ``None`` and ``""`` both mean "not set" and come back as ``None``.
    """
    if value is None or value == "":
        return None
    field_type = field["type"]
    if field_type == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field['label']}: {value!r} is not a number.") from None
        return int(number) if number.is_integer() else number
    if field_type == "choice":
        value = str(value)
        if value not in field["choices"]:
            raise ValueError(
                f"{field['label']}: {value!r} is not one of {', '.join(field['choices'])}."
            )
        return value
    return str(value)


def validate_planning_data(data, kind):
    """Validate a ``planning_data`` blob against the configured schema.

    Returns the cleaned dict (unset keys dropped). Raises Django's
    ``ValidationError`` keyed on ``planning_data`` so it surfaces the same way
    in a form, in ``full_clean()`` and in a REST 400.
    """
    if data in (None, {}):
        data = {}
    if not isinstance(data, dict):
        raise ValidationError({"planning_data": "Planning data must be an object."})

    schema = {field["key"]: field for field in placement_field_schema()}

    unknown = sorted(set(data) - set(schema))
    if unknown:
        # No silent-ignore path: a key nothing will ever read is a mistake the
        # planner needs to see, not a value that vanishes on save.
        known = ", ".join(sorted(schema)) or "none configured"
        raise ValidationError({
            "planning_data": f"Unknown planning field(s): {', '.join(unknown)}. Configured: {known}.",
        })

    cleaned = {}
    errors = []
    for key, field in schema.items():
        if key in data:
            if kind not in field["kinds"]:
                errors.append(f"{field['label']}: cannot be set on a '{kind}' placement.")
                continue
            try:
                value = coerce_value(field, data[key])
            except ValueError as exc:
                errors.append(str(exc))
                continue
        else:
            value = None
        if value is None:
            if field["required"] and kind in field["kinds"]:
                errors.append(f"{field['label']}: a value is required.")
            continue
        cleaned[key] = value

    if errors:
        raise ValidationError({"planning_data": errors})
    return cleaned
