"""
Planning fields on NetBox forms.

The editor's Planning attributes dialog is not the only place a planner meets
the deployment's ``placement_fields``: a template placement is edited in an
ordinary NetBox form. :class:`PlanningFieldsFormMixin` adds one input per
configured field to such a form -- typed from the device custom field the
field is bound to (planning_fields.py), so an integer is a number box, a
selection its choice set, an object field an API-backed picker -- pre-filled
from the instance's ``planning_data`` and written back to it as the same JSON
values the editor sends. The model's own ``clean()`` then validates them
through ``planning_fields.validate_planning_data`` as usual.
"""

from datetime import date, datetime
from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _
from utilities.forms.fields import DynamicModelChoiceField, DynamicModelMultipleChoiceField
from utilities.forms.rendering import FieldSet
from utilities.forms.widgets import DatePicker, DateTimePicker

from . import planning_fields

FIELD_PREFIX = "pf_"


def _form_field(field):
    """The Django form field for one bound planning-field descriptor."""
    label = field["label"]
    kind = field["type"]
    common = {"label": label, "required": False}
    choices = [(value, (field.get("choice_labels") or {}).get(value, value))
               for value in field.get("choices") or []]

    if kind in ("select", "choice"):
        return forms.ChoiceField(choices=[("", "---------"), *choices], **common)
    if kind == "multiselect":
        return forms.MultipleChoiceField(choices=choices, **common)
    if kind == "boolean":
        return forms.NullBooleanField(widget=forms.Select(choices=[
            ("", "---------"), ("true", _("Yes")), ("false", _("No"))]), **common)
    if kind == "integer":
        return forms.IntegerField(min_value=field.get("min"), max_value=field.get("max"), **common)
    if kind in ("decimal", "number"):
        return forms.DecimalField(min_value=field.get("min"), max_value=field.get("max"), **common)
    if kind == "date":
        return forms.DateField(widget=DatePicker(), **common)
    if kind == "datetime":
        return forms.DateTimeField(widget=DateTimePicker(), **common)
    if kind == "url":
        return forms.URLField(assume_scheme="https", **common) if _accepts_assume_scheme() \
            else forms.URLField(**common)
    if kind == "json":
        return forms.JSONField(**common)
    if kind == "longtext":
        return forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), **common)
    if kind in planning_fields.OBJECT_TYPES and field.get("model") is not None:
        queryset = field["model"].objects.all()
        if kind == "multiobject":
            return DynamicModelMultipleChoiceField(queryset=queryset, **common)
        return DynamicModelChoiceField(queryset=queryset, **common)
    return forms.CharField(**common)


def _accepts_assume_scheme():
    import inspect
    return "assume_scheme" in inspect.signature(forms.URLField.__init__).parameters


def _to_initial(field, value):
    """A stored planning value as its form field's initial value."""
    if value is None:
        return None
    kind = field["type"]
    if kind == "boolean":
        return "true" if value in (True, "true") else "false"
    if kind == "json":
        return value
    return value


def _to_stored(field, value):
    """A cleaned form value as the JSON planning_data holds (the editor's form)."""
    if value in (None, "", [], ()):
        return None
    kind = field["type"]
    if kind in planning_fields.OBJECT_TYPES:
        if hasattr(value, "__iter__") and not hasattr(value, "pk"):
            return [obj.pk for obj in value] or None
        return value.pk
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if kind == "multiselect":
        return list(value)
    return value


class PlanningFieldsFormMixin:
    """Add the deployment's placement_fields to a form over a model with a
    ``planning_data`` JSON field (a TemplatePlacement, which is always
    validated as an ``add``)."""

    planning_kind = "add"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._planning_schema = [
            f for f in planning_fields.placement_field_schema()
            if self.planning_kind in f["kinds"]
        ]
        stored = getattr(self.instance, "planning_data", None) or {}
        names = []
        for field in self._planning_schema:
            name = FIELD_PREFIX + field["key"]
            self.fields[name] = _form_field(field)
            if field["key"] in stored:
                self.initial.setdefault(name, _to_initial(field, stored[field["key"]]))
            names.append(name)
        if names:
            self.fieldsets = (*self.fieldsets, FieldSet(*names, name=_("Planning fields")))

    def clean(self):
        super().clean()
        if not self._planning_schema:
            return self.cleaned_data
        data = {}
        for field in self._planning_schema:
            value = _to_stored(field, self.cleaned_data.get(FIELD_PREFIX + field["key"]))
            if value is not None:
                data[field["key"]] = value
        # The model's clean() (run by the ModelForm after this) validates and
        # normalises it against the same schema the editor and API use.
        self.instance.planning_data = data or None
        return self.cleaned_data
