"""Forms for NetBox Rack Design."""

from django import forms
from dcim.models import Device, DeviceType, Rack, Site
from netbox.forms import NetBoxModelFilterSetForm, NetBoxModelForm
from utilities.forms.fields import DynamicModelChoiceField, DynamicModelMultipleChoiceField

from .choices import DesignPlacementKindChoices, DesignStatusChoices
from .models import Design, DesignGroup, DesignPlacement

__all__ = (
    "DesignGroupForm",
    "DesignForm",
    "DesignPlacementForm",
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
