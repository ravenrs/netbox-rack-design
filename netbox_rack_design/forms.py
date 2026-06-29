"""Forms for NetBox Rack Design."""

from dcim.models import Device, DeviceRole, DeviceType, Location, Manufacturer, Rack, Site
from django import forms
from django.utils.translation import gettext_lazy as _
from netbox.forms import (
    NetBoxModelBulkEditForm,
    NetBoxModelFilterSetForm,
    NetBoxModelForm,
    NetBoxModelImportForm,
)
from tenancy.models import Tenant
from utilities.forms.fields import (
    CSVChoiceField,
    CSVModelChoiceField,
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
)
from utilities.forms.rendering import FieldSet

from .choices import DesignPlacementKindChoices, DesignStatusChoices
from .models import Design, DesignGroup, DesignPlacement

__all__ = (
    "DesignGroupForm",
    "DesignForm",
    "DesignPlacementForm",
    "DesignGroupImportForm",
    "DesignImportForm",
    "DesignPlacementImportForm",
    "DesignGroupBulkEditForm",
    "DesignBulkEditForm",
    "DesignPlacementBulkEditForm",
    "DesignGroupFilterForm",
    "DesignFilterForm",
    "DesignPlacementFilterForm",
    "ElevationBrowserFilterForm",
    "DesignEditorPaletteForm",
    "DesignEditorAddRackForm",
)


# ---------------------------------------------------------------------------
# Interactive editor left-rail selectors
# ---------------------------------------------------------------------------


class DesignEditorPaletteForm(forms.Form):
    """
    Drives the editor's left-rail selectors as NetBox-native, API-backed
    searchable selects (DynamicModelChoiceField → APISelect widget). NetBox's
    bundled select-init enhances these into TomSelect-with-remote-load, so typing
    queries the API live (unlike a plain <select> we populate after page load).

    These are NOT bound to a model — they're transient UI controls:
      • manufacturer filters the device-type catalog search (manufacturer_id);
      • device_role / tenant are applied to NEW adds at drop time.
    The editor JS reads each field's value by its Django widget id (id_<name>).
    """

    manufacturer = DynamicModelChoiceField(
        queryset=Manufacturer.objects.all(),
        required=False,
        label=_("Manufacturer"),
    )
    device_role = DynamicModelChoiceField(
        queryset=DeviceRole.objects.all(),
        required=False,
        label=_("Device role"),
    )
    tenant = DynamicModelChoiceField(
        queryset=Tenant.objects.all(),
        required=False,
        label=_("Tenant"),
    )


class DesignEditorAddRackForm(forms.Form):
    """
    Drives the editor's "Add rack" panel: a Location chooser and a Rack chooser,
    both NetBox-native, API-backed searchable selects (DynamicModelChoiceField →
    APISelect → TomSelect with remote load). Like DesignEditorPaletteForm these
    are transient UI controls (NOT bound to a model); the panel JS reads the
    rack field's value and POSTs it to the design's add-rack endpoint.

    Scoping (set per-design in __init__):
      • both fields are limited to the design's site (site_id query param);
      • the rack chooser is additionally chained to the chosen location
        (location_id=$add_location) so picking a location narrows the racks.
    Field names are prefixed ``add_`` so their rendered ids never collide with the
    palette form's selects on the same page.
    """

    add_location = DynamicModelChoiceField(
        queryset=Location.objects.all(),
        required=False,
        label=_("Location"),
    )
    add_rack = DynamicModelChoiceField(
        queryset=Rack.objects.all(),
        required=False,
        label=_("Rack"),
        query_params={"location_id": "$add_location"},
    )

    def __init__(self, *args, site_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope both choosers to the design's site (computed per-design in the
        # view), keeping the static location_id chaining declared above.
        if site_id is not None:
            self.fields["add_location"].widget.add_query_param("site_id", site_id)
            self.fields["add_rack"].widget.add_query_param("site_id", site_id)


# ---------------------------------------------------------------------------
# Model forms
# ---------------------------------------------------------------------------


class DesignGroupForm(NetBoxModelForm):
    parent = DynamicModelChoiceField(queryset=DesignGroup.objects.all(), required=False)

    class Meta:
        model = DesignGroup
        fields = ("name", "parent", "description", "link", "tags")


class DesignForm(NetBoxModelForm):
    site = DynamicModelChoiceField(queryset=Site.objects.all())
    group = DynamicModelChoiceField(queryset=DesignGroup.objects.all(), required=False)
    based_on = DynamicModelChoiceField(queryset=Design.objects.all(), required=False)
    depends_on = DynamicModelMultipleChoiceField(queryset=Design.objects.all(), required=False)
    # Racks this design plans across. Options are filtered live to the chosen
    # site via query_params (chained on the `site` field's value).
    racks = DynamicModelMultipleChoiceField(
        queryset=Rack.objects.all(),
        required=False,
        label=_("Racks"),
        query_params={"site_id": "$site"},
    )

    fieldsets = (
        FieldSet(
            "title", "site", "status", "summary", "link", "racks",
            name=_("Design"),
        ),
        FieldSet(
            "group", "based_on", "depends_on", "sequence",
            name=_("Lineage & scheduling"),
        ),
        FieldSet("description", "tags", name=_("Tags")),
    )

    class Meta:
        model = Design
        fields = (
            "title", "site", "status", "summary", "link", "racks",
            "group", "based_on", "depends_on", "sequence",
            "description", "comments", "tags",
        )

    def clean(self):
        super().clean()
        # Enforce the same-site rule at the FORM layer so it also holds on
        # CREATE. The model's clean() can't see the M2M before the instance is
        # saved (no pk → no through-rows), so a brand-new design would otherwise
        # skip this check. Keep the message consistent with Design.clean().
        site = self.cleaned_data.get("site")
        racks = self.cleaned_data.get("racks")
        if site and racks:
            offending = [rack for rack in racks if rack.site_id != site.pk]
            if offending:
                names = ", ".join(str(rack) for rack in offending)
                self.add_error(
                    "racks",
                    _("These racks are not in the design's site: %(names)s.")
                    % {"names": names},
                )
        return self.cleaned_data


class DesignPlacementForm(NetBoxModelForm):
    design = DynamicModelChoiceField(queryset=Design.objects.all())
    device = DynamicModelChoiceField(queryset=Device.objects.all(), required=False)
    device_type = DynamicModelChoiceField(queryset=DeviceType.objects.all(), required=False)
    target_rack = DynamicModelChoiceField(queryset=Rack.objects.all(), required=False)

    class Meta:
        model = DesignPlacement
        fields = (
            "design", "kind", "device", "device_type", "proposed_name",
            "target_rack", "target_position", "target_face", "tags",
        )


# ---------------------------------------------------------------------------
# Import forms
# ---------------------------------------------------------------------------


class DesignGroupImportForm(NetBoxModelImportForm):
    parent = CSVModelChoiceField(
        queryset=DesignGroup.objects.all(),
        to_field_name="name",
        required=False,
        help_text="Parent group (by name)",
    )

    class Meta:
        model = DesignGroup
        fields = ("name", "parent", "description", "link", "tags")


class DesignImportForm(NetBoxModelImportForm):
    site = CSVModelChoiceField(
        queryset=Site.objects.all(),
        to_field_name="name",
        help_text="Assigned site (by name)",
    )
    status = CSVChoiceField(choices=DesignStatusChoices, required=False)
    group = CSVModelChoiceField(
        queryset=DesignGroup.objects.all(),
        to_field_name="name",
        required=False,
        help_text="Group (by name)",
    )

    class Meta:
        model = Design
        fields = (
            "title", "site", "status", "summary", "link",
            "group", "sequence", "description", "comments", "tags",
        )


class DesignPlacementImportForm(NetBoxModelImportForm):
    design = CSVModelChoiceField(
        queryset=Design.objects.all(),
        to_field_name="title",
        help_text="Parent design (by title)",
    )
    kind = CSVChoiceField(choices=DesignPlacementKindChoices)
    device = CSVModelChoiceField(
        queryset=Device.objects.all(),
        to_field_name="name",
        required=False,
        help_text="Existing device (by name)",
    )
    device_type = CSVModelChoiceField(
        queryset=DeviceType.objects.all(),
        to_field_name="model",
        required=False,
        help_text="Device type (by model)",
    )
    target_rack = CSVModelChoiceField(
        queryset=Rack.objects.all(),
        to_field_name="name",
        required=False,
        help_text="Target rack (by name)",
    )

    class Meta:
        model = DesignPlacement
        fields = (
            "design", "kind", "device", "device_type", "proposed_name",
            "target_rack", "target_position", "target_face", "tags",
        )


# ---------------------------------------------------------------------------
# Bulk edit forms
# ---------------------------------------------------------------------------


class DesignGroupBulkEditForm(NetBoxModelBulkEditForm):
    parent = DynamicModelChoiceField(queryset=DesignGroup.objects.all(), required=False)
    description = forms.CharField(max_length=200, required=False)
    link = forms.URLField(required=False)

    model = DesignGroup
    nullable_fields = ("parent", "description", "link")


class DesignBulkEditForm(NetBoxModelBulkEditForm):
    status = forms.ChoiceField(choices=DesignStatusChoices, required=False)
    summary = forms.CharField(max_length=200, required=False)
    group = DynamicModelChoiceField(queryset=DesignGroup.objects.all(), required=False)
    description = forms.CharField(max_length=200, required=False)

    model = Design
    nullable_fields = ("summary", "group", "description")


class DesignPlacementBulkEditForm(NetBoxModelBulkEditForm):
    proposed_name = forms.CharField(max_length=64, required=False)
    target_face = forms.CharField(max_length=10, required=False)

    model = DesignPlacement
    nullable_fields = ("proposed_name", "target_face")


# ---------------------------------------------------------------------------
# Filter forms
# ---------------------------------------------------------------------------


class DesignGroupFilterForm(NetBoxModelFilterSetForm):
    model = DesignGroup
    parent_id = DynamicModelMultipleChoiceField(queryset=DesignGroup.objects.all(), required=False, label="Parent")


class DesignFilterForm(NetBoxModelFilterSetForm):
    model = Design
    site_id = DynamicModelMultipleChoiceField(queryset=Site.objects.all(), required=False, label="Site")
    group_id = DynamicModelMultipleChoiceField(queryset=DesignGroup.objects.all(), required=False, label="Group")
    status = forms.MultipleChoiceField(choices=DesignStatusChoices, required=False)


class DesignPlacementFilterForm(NetBoxModelFilterSetForm):
    model = DesignPlacement
    design_id = DynamicModelMultipleChoiceField(queryset=Design.objects.all(), required=False, label="Design")
    target_rack_id = DynamicModelMultipleChoiceField(queryset=Rack.objects.all(), required=False, label="Target rack")
    kind = forms.MultipleChoiceField(choices=DesignPlacementKindChoices, required=False)


# ---------------------------------------------------------------------------
# Elevation browser (standalone, non-model-bound) filter form
# ---------------------------------------------------------------------------


class ElevationBrowserFilterForm(forms.Form):
    """
    GET-driven, multi-select filter for the standalone Elevations LIST page.

    Not a NetBoxModelFilterSetForm (the rows are derived (design, rack) pairs, not
    a single model's queryset) — a plain form whose four MULTI-select fields
    populate the ``design``, ``rack``, ``site`` and ``status`` query-string params.
    ElevationBrowserView applies them server-side to the derived rows; within a
    field the values are OR'd, across fields AND'd, and an empty field is no
    constraint.

    The OFFERED options are narrowed SERVER-SIDE to the current selection: the view
    passes the narrowed Design/Rack/Site querysets and Status choices (computed
    from the actual elevation rows) into __init__ so the form never offers a value
    that would yield zero rows. Plain Model/MultipleChoice fields are used (not the
    API-backed Dynamic* fields) precisely so the limited querysets/choices — not
    the full core catalog — drive the rendered <select> options. NetBox's frontend
    still upgrades each plain <select multiple> to the native TomSelect multi-select.
    """

    design = forms.ModelMultipleChoiceField(
        queryset=Design.objects.none(),
        required=False,
        label=_("Design"),
        to_field_name="pk",
    )
    rack = forms.ModelMultipleChoiceField(
        queryset=Rack.objects.none(),
        required=False,
        label=_("Rack"),
        to_field_name="pk",
    )
    site = forms.ModelMultipleChoiceField(
        queryset=Site.objects.none(),
        required=False,
        label=_("Site"),
        to_field_name="pk",
    )
    status = forms.MultipleChoiceField(
        choices=(),
        required=False,
        label=_("Status"),
    )

    fieldsets = (
        FieldSet("design", "rack", "site", "status", name=_("Elevations")),
    )

    def __init__(self, *args, design_qs=None, rack_qs=None, site_qs=None, status_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        if design_qs is not None:
            self.fields["design"].queryset = design_qs
        if rack_qs is not None:
            self.fields["rack"].queryset = rack_qs
        if site_qs is not None:
            self.fields["site"].queryset = site_qs
        if status_choices is not None:
            self.fields["status"].choices = status_choices
