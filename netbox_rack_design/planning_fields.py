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
    type      optional  "text" (default) | "number" | "choice". Ignored for a
                        placement field whose ``target`` names an existing
                        device custom field: that field's own NetBox type wins
                        (integer, select, object, ... -- see ``CF_TYPES``).
    choices   required for type "choice"; a list of strings. Likewise taken
              from the custom field's choice set when the target is one.
    source    planning_fields only: where to READ the value from
    target    placement_fields only: where the value LANDS on the real device
    kinds     placement_fields only: which placement kinds may set the field;
              defaults to ("add",), mirroring the role/tenant rule
    rail      placement_fields only: offer it as a sticky default in the
              palette rail
    required  placement_fields only: the field must carry a value

Both ``source`` and ``target`` use the same token grammar: ``cf.<name>`` for a
custom field, any other dotted path for native attributes.

A placement field aimed at a device custom field is BOUND to it: the editor
input, the validation and the stored JSON all follow that custom field's own
definition -- its type, choice set, related object type, minimum/maximum and
regex -- so a value the plugin accepts is one NetBox accepts too, and apply
writes it onto the device unchanged.
"""

import json
from datetime import date, datetime

from django.core.exceptions import ImproperlyConfigured, ValidationError
from netbox.plugins import get_plugin_config

PLUGIN_NAME = "netbox_rack_design"

# What a config descriptor may declare. A descriptor bound to a device custom
# field reports that field's type instead, one of CF_TYPES.
FIELD_TYPES = ("text", "number", "choice")
DEFAULT_FIELD_TYPE = "text"
CF_TYPES = (
    "text", "longtext", "integer", "decimal", "boolean", "date", "datetime",
    "url", "json", "select", "multiselect", "object", "multiobject",
)
MULTI_TYPES = ("multiselect", "multiobject")
OBJECT_TYPES = ("object", "multiobject")
# Which placement kinds a placement_fields entry may be set on when the
# descriptor does not say. The two kinds that put a device somewhere: a brand
# new one, or an existing one the design relocates -- the same pair device_role
# and tenant are allowed on (models.DesignPlacement.clean). A removal takes
# none of them: re-attributing gear you are decommissioning means nothing.
DEFAULT_KINDS = ("add", "move")

__all__ = (
    "PLUGIN_NAME",
    "CF_TYPES",
    "FIELD_TYPES",
    "coerce_value",
    "display_value",
    "native_target_values",
    "placement_field_schema",
    "planned_custom_field_data",
    "planning_field_schema",
    "public_placement_field_schema",
    "read_for_slot",
    "read_planning_field",
    "read_planning_fields",
    "resolve_source",
    "unbound_cf_targets",
    "validate_planning_data",
)


# --- request-scoped memo ----------------------------------------------------


def _request_memo(name, build):
    """``build()``, remembered on the current request.

    The schema is read once per rendered slot (hundreds on an editor page),
    and binding it to the device custom fields costs a query. Scoping the memo
    to the request keeps that to one query per page while never serving a
    stale definition: outside a request (apply, the shell, tests) nothing is
    remembered at all.
    """
    from netbox.context import current_request

    request = current_request.get()
    if request is None:
        return build()
    memo = getattr(request, "_nbx_rd_memo", None)
    if memo is None:
        memo = {}
        try:
            request._nbx_rd_memo = memo
        except AttributeError:
            return build()
    if name not in memo:
        memo[name] = build()
    return memo[name]


def _device_custom_fields():
    """``{name: CustomField}`` for every custom field on ``dcim.Device``."""
    def build():
        from dcim.models import Device

        from .compat import custom_fields_for

        return {cf.name: cf for cf in custom_fields_for(Device)}
    return _request_memo("device_cfs", build)


# --- reading a value off an existing object --------------------------------


def _read_cf(obj, name):
    """One custom field off ``obj``.

    Prefer the stored dict over ``obj.cf``. The latter is a merged view that
    re-queries CustomField for the object type on EVERY access -- three queries
    a call, which a per-slot caller multiplies by the whole rack elevation
    (measured: 516 queries to render one editor page). ``custom_field_data`` is
    already on the instance and carries the same value. ``.cf`` stays as the
    fallback for objects that only expose it, such as the proxy views the
    distribution scripts build.
    """
    data = getattr(obj, "custom_field_data", None)
    if isinstance(data, dict):
        return data.get(name)
    cf = getattr(obj, "cf", None) or {}
    return cf.get(name)


def resolve_source(obj, source):
    """Resolve a ``cf.<name>`` / dotted-attribute token against ``obj``.

    A ``cf`` segment may appear ANYWHERE along the path, not only at the head:
    ``cf.project`` reads a custom field off ``obj`` itself, and
    ``design.cf.project`` walks to ``obj.design`` first. That is what lets a
    caller declare a path rooted at a related object (a ``planning_fields``
    descriptor's ``source``, declared relative to the placement/design) through
    this one resolver instead of a second, subtly different one.

    Returns ``None`` for a missing value or an unresolvable path -- a planning
    field is always optional from the reader's point of view.
    """
    if not source or obj is None:
        return None
    parts = source.split(".")
    value = obj
    index = 0
    while index < len(parts):
        if value is None:
            return None
        part = parts[index]
        if part == "cf":
            if index + 1 >= len(parts):
                # A dangling "cf" names no field.
                return None
            value = _read_cf(value, parts[index + 1])
            index += 2
            continue
        value = getattr(value, part, None)
        index += 1
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
    blank; each value comes back as the text a person reads (a choice's label,
    an object's name), not its stored form.
    """
    planned = planning_data or {}
    out = []
    for field in placement_field_schema():
        value = planned.get(field["key"])
        if _is_unset(value):
            value = resolve_source(device, field.get("target"))
        if _is_unset(value):
            continue
        out.append((field["label"], display_value(field, value)))
    return out


def _is_unset(value):
    return value is None or value == "" or value == []


def _object_names(field, ids):
    """``{pk: str(obj)}`` for the related objects of an object-type field."""
    model = field.get("model")
    if model is None or not ids:
        return {}
    key = f"objnames:{model._meta.label_lower}"
    names = _request_memo(key, dict)
    missing = [pk for pk in ids if pk not in names]
    if missing:
        for obj in model.objects.filter(pk__in=missing):
            names[obj.pk] = str(obj)
    return names


def display_value(field, value):
    """The text a hover card shows for one stored planning value."""
    field_type = field["type"]
    if field_type == "boolean":
        return "Yes" if value in (True, 1, "true", "True") else "No"
    labels = field.get("choice_labels") or {}
    if field_type == "multiselect" and isinstance(value, list):
        return ", ".join(str(labels.get(v, v)) for v in value)
    if field_type in ("select", "choice"):
        return str(labels.get(value, value))
    if field_type in OBJECT_TYPES:
        ids = value if isinstance(value, list) else [value]
        ids = [pk for pk in ids if isinstance(pk, int)]
        names = _object_names(field, ids)
        return ", ".join(names.get(pk, f"#{pk}") for pk in ids)
    if field_type == "json" and not isinstance(value, str):
        return json.dumps(value, sort_keys=True)
    return str(value)


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


def _cf_name(target):
    """The custom-field name a ``cf.<name>`` target names, else ``None``."""
    if target and target.startswith("cf.") and "." not in target[3:]:
        return target[3:]
    return None


def _bind(field, custom_fields):
    """Bind one descriptor to the device custom field its target names.

    The custom field's own definition replaces whatever the descriptor
    declared: the type, the choices (from its choice set, labels kept), the
    related model of an object field, and the numeric bounds. A target naming
    no existing custom field keeps the declared type -- and apply reports it.
    """
    cf = custom_fields.get(_cf_name(field["target"]) or "")
    field.update({"cf": cf, "choice_labels": {}, "model": None,
                  "multiple": False, "min": None, "max": None})
    if cf is None:
        if field["type"] == "choice":
            field["choice_labels"] = {c: c for c in field["choices"]}
        return field
    field["type"] = cf.type
    field["multiple"] = cf.type in MULTI_TYPES
    if cf.type in ("select", "multiselect") and cf.choice_set_id:
        pairs = [(str(value), str(label)) for value, label in cf.choice_set.choices]
        field["choices"] = [value for value, _label in pairs]
        field["choice_labels"] = dict(pairs)
    if cf.type in OBJECT_TYPES and cf.related_object_type_id:
        field["model"] = cf.related_object_type.model_class()
    if cf.type in ("integer", "decimal"):
        cast = int if cf.type == "integer" else float
        field["min"] = None if cf.validation_minimum is None else cast(cf.validation_minimum)
        field["max"] = None if cf.validation_maximum is None else cast(cf.validation_maximum)
    return field


def placement_field_schema():
    """The validated ``placement_fields`` descriptors, bound to the device
    custom fields they target, or ``[]``."""
    entries = get_plugin_config(PLUGIN_NAME, "placement_fields", []) or []
    if not isinstance(entries, (list, tuple)):
        raise ImproperlyConfigured("placement_fields must be a list of descriptors.")
    fields = [
        _validate_descriptor(entry, "placement_fields", needs_source=False)
        for entry in entries
    ]
    if not fields:
        return fields
    custom_fields = _device_custom_fields()
    return [_bind(field, custom_fields) for field in fields]


def _api_url(model):
    from django.urls import NoReverseMatch, reverse
    from utilities.views import get_viewname

    try:
        return reverse(get_viewname(model, action="list", rest_api=True))
    except NoReverseMatch:
        return None


def public_placement_field_schema():
    """``placement_field_schema()`` without the deployment plumbing.

    ``target`` names a real custom field on the deployment's devices; it is how
    an apply step routes the value, not part of the contract an editor or API
    client needs. An object field publishes the REST list its values are
    picked from (``api_url``) instead of its model. Everything else is
    published.
    """
    out = []
    for field in placement_field_schema():
        public = {k: v for k, v in field.items() if k not in ("target", "cf", "model")}
        model = field.get("model")
        public["api_url"] = _api_url(model) if model is not None else None
        public["object_type"] = str(model._meta.verbose_name) if model is not None else None
        out.append(public)
    return out


# --- validating what a planner (or an API client) sent ----------------------


def coerce_value(field, value):
    """Coerce one submitted value to the descriptor's type.

    Raises ``ValueError`` with a human message when the value does not fit.
    ``None`` and ``""`` both mean "not set" and come back as ``None``.
    """
    if _is_unset(value):
        return None
    value = _coerce_to_type(field, value)
    if value is None:
        return None
    cf = field.get("cf")
    if cf is not None:
        # The custom field's own rules (regex, minimum/maximum, choice set):
        # the exact check NetBox runs when apply saves the device.
        try:
            cf.validate(value)
        except ValidationError as exc:
            raise ValueError(f"{field['label']}: {' '.join(exc.messages)}") from None
    return value


def _as_list(value):
    """A multi-value submission: a list, or one comma-separated string."""
    if isinstance(value, (list, tuple)):
        return [v for v in value if not _is_unset(v)]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _coerce_to_type(field, value):
    """``value`` in the JSON form a device's ``custom_field_data`` holds."""
    label = field["label"]
    field_type = field["type"]

    if field_type == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: {value!r} is not a number.") from None
        return int(number) if number.is_integer() else number

    if field_type == "integer":
        if isinstance(value, bool):
            raise ValueError(f"{label}: {value!r} is not a whole number.")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: {value!r} is not a whole number.") from None
        if not number.is_integer():
            raise ValueError(f"{label}: {value!r} is not a whole number.")
        return int(number)

    if field_type == "decimal":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: {value!r} is not a number.") from None
        return int(number) if number.is_integer() else number

    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{label}: {value!r} is not true or false.")

    if field_type == "date":
        text = str(value).strip()
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise ValueError(f"{label}: {value!r} is not a date (YYYY-MM-DD).") from None

    if field_type == "datetime":
        text = str(value).strip()
        try:
            return datetime.fromisoformat(text).isoformat()
        except ValueError:
            raise ValueError(
                f"{label}: {value!r} is not a date and time (YYYY-MM-DD HH:MM)."
            ) from None

    if field_type == "json":
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                raise ValueError(f"{label}: {value!r} is not valid JSON.") from None
        return value

    if field_type in ("choice", "select"):
        value = str(value)
        if value not in field["choices"]:
            raise ValueError(f"{label}: {value!r} is not one of {', '.join(field['choices'])}.")
        return value

    if field_type == "multiselect":
        values = [str(v) for v in _as_list(value)]
        bad = [v for v in values if v not in field["choices"]]
        if bad:
            raise ValueError(f"{label}: {', '.join(bad)} not one of {', '.join(field['choices'])}.")
        return values or None

    if field_type in OBJECT_TYPES:
        raw = _as_list(value) if field_type == "multiobject" else [value]
        ids = []
        for item in raw:
            if isinstance(item, dict):
                item = item.get("id")
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                raise ValueError(f"{label}: {item!r} is not an object ID.") from None
        model = field.get("model")
        if model is not None and ids:
            found = set(model.objects.filter(pk__in=ids).values_list("pk", flat=True))
            missing = [pk for pk in ids if pk not in found]
            if missing:
                raise ValueError(
                    f"{label}: no {model._meta.verbose_name} with ID "
                    f"{', '.join(str(pk) for pk in missing)}."
                )
        if field_type == "multiobject":
            return ids or None
        return ids[0]

    # text, longtext, url
    return str(value)


def planning_data_from_device(device, overrides=None, kind="add"):
    """The ``planning_data`` a template takes from a real device: each
    placement field read back from where it lands (``target``) -- the
    device's custom field, or a native attribute -- with ``overrides`` (a
    move's own planned values) on top. Fields a ``kind`` placement may not
    carry are left out, as are unset ones."""
    data = {}
    for field in placement_field_schema():
        if kind not in field["kinds"]:
            continue
        target = field["target"]
        name = _cf_name(target)
        if name:
            value = (getattr(device, "custom_field_data", None) or {}).get(name)
        elif target and "." not in target:
            value = getattr(device, target, None)
        else:
            continue
        if _is_unset(value):
            continue
        try:
            value = coerce_value(field, value)
        except ValueError:
            # A device value the field would refuse (data older than its
            # rules): leave it out rather than fail the whole template.
            continue
        if value is not None:
            data[field["key"]] = value
    for key, value in (overrides or {}).items():
        if not _is_unset(value):
            data[key] = value
    return data or None


# --- what apply writes onto the device -------------------------------------


def planned_custom_field_data(planning_data):
    """``{custom field name: value}`` for the values ``planning_data`` sets on
    fields that target a device custom field. Unset keys are absent."""
    data = planning_data or {}
    out = {}
    for field in placement_field_schema():
        name = _cf_name(field["target"])
        if name and not _is_unset(data.get(field["key"])):
            out[name] = data[field["key"]]
    return out


def native_target_values(planning_data):
    """``{attribute: value}`` for values aimed at a native device attribute
    (a one-segment ``target`` such as ``serial`` or ``asset_tag``)."""
    data = planning_data or {}
    out = {}
    for field in placement_field_schema():
        target = field["target"]
        if target and not target.startswith("cf.") and "." not in target \
                and not _is_unset(data.get(field["key"])):
            out[target] = data[field["key"]]
    return out


def unbound_cf_targets():
    """The ``(label, custom field name)`` of every field whose ``cf.`` target
    names no custom field on devices -- a value apply could not write."""
    return [
        (field["label"], _cf_name(field["target"]))
        for field in placement_field_schema()
        if _cf_name(field["target"]) and field.get("cf") is None
    ]


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
