"""FilterSets for NetBox Rack Design."""

import django_filters
from dcim.models import Device, DeviceBay, DeviceRole, DeviceType, PowerFeed, Rack, Site
from django.db.models import Q
from netbox.filtersets import NetBoxModelFilterSet
from tenancy.models import Tenant
from utilities.filters import TreeNodeMultipleChoiceFilter

from .choices import DesignPlacementKindChoices, DesignStatusChoices
from .models import Design, DesignGroup, DesignPlacement, DesignPowerFeed

__all__ = (
    "DesignGroupFilterSet",
    "DesignFilterSet",
    "DesignPlacementFilterSet",
    "DesignPowerFeedFilterSet",
)


class DesignGroupFilterSet(NetBoxModelFilterSet):
    parent_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DesignGroup.objects.all(), label="Parent (ID)"
    )

    class Meta:
        model = DesignGroup
        fields = ("id", "name", "description", "link")

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
    based_on_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Design.objects.all(), label="Based on (ID)"
    )
    root_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Design.objects.all(), label="Root (ID)"
    )
    design_id = django_filters.ModelMultipleChoiceFilter(
        field_name="depends_on",
        queryset=Design.objects.all(),
        label="Depends on (ID)",
    )
    # The racks this design touches (M2M): "which designs touch this rack?".
    # Named after the MODEL FIELD (``racks_id``), NOT the ``rack_id`` that core's
    # coverage check derives from the related model's verbose_name, because the
    # Design viewset's custom detail actions (rack-power/, power-source/,
    # feeds/, planned-feed/) already take ``?rack_id=<pk>`` as their own
    # parameter -- and DRF's ``get_object()`` runs ``filter_queryset()``, so a
    # Design filter of that name would filter the design away and 404 every one
    # of those endpoints. The test declares the rename via ``filter_name_map``.
    racks_id = django_filters.ModelMultipleChoiceFilter(
        field_name="racks",
        queryset=Rack.objects.all(),
        label="Rack (ID)",
    )
    # "Designs with no parent" (PLAN-design-chains.md G9): the root of a chain,
    # or an ordinary single-layer design. Named ``no_parent`` rather than
    # something built on ``based_on_id`` (e.g. ``based_on_id__isnull``, not a
    # legal query-param spelling) -- and, like ``racks_id`` above, chosen with
    # the Design viewset's custom detail @actions in mind: none of them read a
    # ``no_parent`` query parameter, so this cannot repeat the ``rack_id`` trap
    # (see ``racks_id`` above) where a same-named filter 404s an action via
    # ``get_object()`` -> ``filter_queryset()``.
    no_parent = django_filters.BooleanFilter(
        field_name="based_on", lookup_expr="isnull", label="Has no parent (based_on)",
    )
    status = django_filters.MultipleChoiceFilter(choices=DesignStatusChoices)

    class Meta:
        model = Design
        fields = ("id", "title", "version", "sequence", "description", "link", "summary")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(title__icontains=value) | Q(summary__icontains=value))


class DesignPlacementFilterSet(NetBoxModelFilterSet):
    design_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Design.objects.all(), label="Design (ID)"
    )
    device_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Device.objects.all(), label="Device (ID)"
    )
    target_rack_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Rack.objects.all(), label="Target rack (ID)"
    )
    device_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DeviceType.objects.all(), label="Device type (ID)"
    )
    # DeviceRole is MPTT-nested, so this mirrors core's dcim ``role_id``:
    # a TreeNodeMultipleChoiceFilter matches the selected role AND its
    # descendants, which is what "show me every planned compute node" means.
    device_role_id = TreeNodeMultipleChoiceFilter(
        queryset=DeviceRole.objects.all(),
        field_name="device_role",
        lookup_expr="in",
        label="Device role (ID)",
    )
    tenant_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Tenant.objects.all(), label="Tenant (ID)"
    )
    kind = django_filters.MultipleChoiceFilter(choices=DesignPlacementKindChoices)
    # Device-bay targeting: find the blades planned into a given chassis, whether
    # the chassis is real (target_bay) or itself planned (parent_placement).
    target_bay_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DeviceBay.objects.all(), label="Target bay (ID)"
    )
    parent_placement_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DesignPlacement.objects.all(), label="Parent placement (ID)"
    )
    # The upstream (ancestor design's) placement a move/remove acts on when its
    # device is not yet real (G2, PLAN-design-chains.md). A missing <fk>_id
    # filter here would silently break {% htmx_table %} embeds and API
    # filtering scoped to one upstream placement's downstream references.
    base_placement_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DesignPlacement.objects.all(), label="Base placement (ID)"
    )
    # The ancestor design's CHASSIS placement a blade is planned into (G2) --
    # "show me every downstream blade riding this planned chassis". Declared for
    # the same reason as base_placement_id above: without the <fk>_id filter the
    # {% htmx_table %} embed and the API both silently return everything.
    base_parent_placement_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DesignPlacement.objects.all(), label="Base parent placement (ID)"
    )

    # Power wiring: "which planned devices hang off this PDU / this feed?"
    power_source_device_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Device.objects.all(), label="Power source device (ID)"
    )
    real_power_feed_id = django_filters.ModelMultipleChoiceFilter(
        queryset=PowerFeed.objects.all(), label="Real power feed (ID)"
    )
    planned_power_feed_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DesignPowerFeed.objects.all(), label="Planned power feed (ID)"
    )

    # "Show me everything this design lost when devices were decommissioned."
    stale = django_filters.BooleanFilter(label="Device deleted")

    class Meta:
        model = DesignPlacement
        fields = (
            "id", "proposed_name", "target_bay_name", "stale_device_name",
            "target_position", "target_face",
        )

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(proposed_name__icontains=value))


class DesignPowerFeedFilterSet(NetBoxModelFilterSet):
    design_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Design.objects.all(), label="Design (ID)"
    )
    rack_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Rack.objects.all(), label="Rack (ID)"
    )

    class Meta:
        model = DesignPowerFeed
        fields = ("id", "name", "voltage", "amperage", "phase", "supply")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(rack__name__icontains=value))
