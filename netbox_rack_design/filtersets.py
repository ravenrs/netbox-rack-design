"""FilterSets for NetBox Rack Design."""

import django_filters
from dcim.models import DeviceType, Rack, Site
from django.db.models import Q
from netbox.filtersets import NetBoxModelFilterSet

from .choices import DesignPlacementKindChoices, DesignStatusChoices
from .models import Design, DesignGroup, DesignPlacement

__all__ = ("DesignGroupFilterSet", "DesignFilterSet", "DesignPlacementFilterSet")


class DesignGroupFilterSet(NetBoxModelFilterSet):
    parent_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DesignGroup.objects.all(), label="Parent (ID)"
    )

    class Meta:
        model = DesignGroup
        fields = ("id", "name")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value))


class DesignFilterSet(NetBoxModelFilterSet):
    site_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Site.objects.all(), label="Site (ID)"
    )
    group_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DesignGroup.objects.all(), label="Group (ID)"
    )
    status = django_filters.MultipleChoiceFilter(choices=DesignStatusChoices)

    class Meta:
        model = Design
        fields = ("id", "title", "version", "sequence")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(title__icontains=value) | Q(summary__icontains=value))


class DesignPlacementFilterSet(NetBoxModelFilterSet):
    design_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Design.objects.all(), label="Design (ID)"
    )
    target_rack_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Rack.objects.all(), label="Target rack (ID)"
    )
    device_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DeviceType.objects.all(), label="Device type (ID)"
    )
    kind = django_filters.MultipleChoiceFilter(choices=DesignPlacementKindChoices)

    class Meta:
        model = DesignPlacement
        fields = ("id", "proposed_name")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(proposed_name__icontains=value))
