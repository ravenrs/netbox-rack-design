"""Forms for NetBox Rack Design."""

from dcim.models import Device, DeviceType, Rack, Site
from django import forms
from netbox.forms import (
    NetBoxModelBulkEditForm,
    NetBoxModelFilterSetForm,
    NetBoxModelForm,
    NetBoxModelImportForm,
)
from utilities.forms.fields import (
    CSVChoiceField,
    CSVModelChoiceField,
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
)

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
)


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

    class Meta:
        model = Design
        fields = (
            "title", "site", "status", "summary", "link",
            "group", "based_on", "depends_on", "sequence",
            "description", "comments", "tags",
        )


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
