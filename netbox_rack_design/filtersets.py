"""FilterSets for NetBox Rack Design."""

import django_filters
from dcim.models import Device, DeviceBay, DeviceRole, DeviceType, Location, PowerFeed, PowerPanel, Rack, Site
from django.db.models import Q
from netbox.filtersets import BaseFilterSet, NetBoxModelFilterSet
from tenancy.models import Tenant
from utilities.filters import TreeNodeMultipleChoiceFilter

from .choices import DesignPlacementKindChoices, DesignStatusChoices, TemplatePlacementAnchorChoices
from .models import (
    Design,
    DesignApply,
    DesignGroup,
    DesignPlacement,
    DesignPowerFeed,
    PlannedRack,
    Template,
    TemplateGroup,
    TemplatePlacement,
)

__all__ = (
    "DesignGroupFilterSet",
    "DesignFilterSet",
    "DesignPlacementFilterSet",
    "PlannedRackFilterSet",
    "DesignPowerFeedFilterSet",
    "DesignApplyFilterSet",
    "TemplateGroupFilterSet",
    "TemplateFilterSet",
    "TemplatePlacementFilterSet",
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
    # M9 (PLAN-multi-site.md): `Design.sites` is a M2M, so both the PK filter
    # and the human-friendly slug filter resolve against it, not a single FK.
    site_id = django_filters.ModelMultipleChoiceFilter(
        field_name="sites", queryset=Site.objects.all(), label="Site (ID)"
    )
    site = django_filters.ModelMultipleChoiceFilter(
        field_name="sites__slug",
        queryset=Site.objects.all(),
        to_field_name="slug",
        label="Site (slug)",
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
    # The planned-rack counterpart of ``racks_id`` above (T1.3): same rename
    # rationale -- named after the MODEL FIELD (``planned_racks_id``), not a
    # bare ``planned_rack_id``, so it cannot collide with a query param any
    # detail action might one day take.
    planned_racks_id = django_filters.ModelMultipleChoiceFilter(
        field_name="planned_racks",
        queryset=PlannedRack.objects.all(),
        label="Planned rack (ID)",
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
    # The planned-rack counterpart of ``target_rack_id`` above (T1.2).
    target_planned_rack_id = django_filters.ModelMultipleChoiceFilter(
        queryset=PlannedRack.objects.all(), label="Target planned rack (ID)"
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

    # Provenance (D20): "which placements were stamped from this template?"
    from_template_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Template.objects.all(), label="From template (ID)"
    )

    # "Show me everything this design lost when devices were decommissioned."
    stale = django_filters.BooleanFilter(label="Device deleted")

    class Meta:
        model = DesignPlacement
        fields = (
            "id", "proposed_name", "target_bay_name", "stale_device_name",
            "target_position", "target_face", "from_template_version",
        )

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        # A removal or keep-name move has no proposed name -- only its device
        # (or, once that device is deleted, the name it had).
        return queryset.filter(
            Q(proposed_name__icontains=value)
            | Q(device__name__icontains=value)
            | Q(stale_device_name__icontains=value)
        )


class PlannedRackFilterSet(NetBoxModelFilterSet):
    """A rack that does not exist in DCIM yet (PLAN-templates.md T1.7).

    Same shape as any other plugin ``NetBoxModel`` filterset -- ``location_id``
    and ``realized_rack_id`` are the two FKs the REST API and any future
    ``{% htmx_table %}`` embed need to filter on.
    """

    # Location is MPTT-nested (mirrors DesignPlacementFilterSet.device_role_id
    # above): filtering by a parent location must also match planned racks in
    # a CHILD location, exactly like core's own dcim ``location_id``.
    location_id = TreeNodeMultipleChoiceFilter(
        queryset=Location.objects.all(),
        field_name="location",
        lookup_expr="in",
        label="Location (ID)",
    )
    # Null while the plan hasn't been applied yet (D7) -- "which planned racks
    # have already been realized as THIS rack" is exactly what a caller
    # cross-referencing dcim.Rack would ask.
    realized_rack_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Rack.objects.all(), label="Realized rack (ID)"
    )
    # The reverse side of Design.planned_racks (related_name="scoped_designs",
    # shared with Design.racks -- see that field's own comment in models.py):
    # "which designs plan across this planned rack?". Named after the RELATED
    # model (design_id), which is exactly what the coverage check expects for
    # a reverse M2M -- no rename needed here, unlike Design's own racks_id/
    # planned_racks_id (those dodge a *different* collision: Design's own
    # detail @actions already read ?rack_id=).
    design_id = django_filters.ModelMultipleChoiceFilter(
        field_name="scoped_designs", queryset=Design.objects.all(), label="Design (ID)"
    )
    # T1.9 decision 3: cleanup is MANUAL, not automatic -- a planned rack no
    # design references any more is dead weight, but nothing in this plugin
    # deletes it on its own (see ``PlannedRack.orphaned()``'s docstring). This
    # filter is what makes that manual review actually findable: without it an
    # orphan looks identical to any other row in the list until someone opens
    # its detail page and checks. ``value=True`` -> only orphans;
    # ``value=False`` -> only rows something still references.
    orphan = django_filters.BooleanFilter(
        method="filter_orphan", label="Orphan (no design references it)"
    )

    class Meta:
        model = PlannedRack
        fields = ("id", "name", "u_height", "description", "comments")

    def filter_orphan(self, queryset, name, value):
        orphan_ids = PlannedRack.orphaned().values_list("pk", flat=True)
        if value:
            return queryset.filter(pk__in=orphan_ids)
        return queryset.exclude(pk__in=orphan_ids)

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value))


class DesignPowerFeedFilterSet(NetBoxModelFilterSet):
    design_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Design.objects.all(), label="Design (ID)"
    )
    rack_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Rack.objects.all(), label="Rack (ID)"
    )
    # The planned-rack counterpart of ``rack_id`` above (D25/T1.8b), mirroring
    # ``DesignPlacementFilterSet.target_planned_rack_id``.
    planned_rack_id = django_filters.ModelMultipleChoiceFilter(
        queryset=PlannedRack.objects.all(), label="Planned rack (ID)"
    )
    # The panel a planned feed hangs from (migration 0024).
    power_panel_id = django_filters.ModelMultipleChoiceFilter(
        queryset=PowerPanel.objects.all(), label="Power panel (ID)"
    )

    class Meta:
        model = DesignPowerFeed
        fields = ("id", "name", "voltage", "amperage", "phase", "supply")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(rack__name__icontains=value))


class DesignApplyFilterSet(BaseFilterSet):
    """
    ``DesignApply`` is a plain ``models.Model``, not a ``NetBoxModel`` -- it
    has no tags/custom fields, so this subclasses ``BaseFilterSet`` (which
    ``NetBoxModelFilterSet`` builds on) rather than ``NetBoxModelFilterSet``
    itself: inheriting the tag/tag_id/custom-field machinery would advertise
    filters the model cannot back (a ``tags`` lookup on this model raises a
    ``FieldError`` the moment anyone actually uses ``?tag=``).

    The three ``no_*`` filters exist for exactly one reason: finding the
    orphans left behind when a design/placement/device this row pointed at
    (all three FKs are SET_NULL) is deleted -- the whole reason automation
    polls this endpoint (docs/apply.md, models.py ``DesignApply``).
    """

    q = django_filters.CharFilter(method="search", label="Search")
    design_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Design.objects.all(), label="Design (ID)"
    )
    placement_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DesignPlacement.objects.all(), label="Placement (ID)"
    )
    device_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Device.objects.all(), label="Device (ID)"
    )
    # Named after the pattern DesignFilterSet.no_parent already uses for "is
    # this FK null" -- not "<fk>_id__isnull", which is not a legal query-param
    # spelling. DesignApplyViewSet defines no custom @actions, so none of
    # these names can shadow an action's own query parameter the way a
    # `rack_id` filter once 404'd four Design power actions.
    no_design = django_filters.BooleanFilter(
        field_name="design", lookup_expr="isnull", label="Design deleted (orphan)",
    )
    no_placement = django_filters.BooleanFilter(
        field_name="placement", lookup_expr="isnull", label="Placement deleted",
    )
    no_device = django_filters.BooleanFilter(
        field_name="device", lookup_expr="isnull", label="Device deleted",
    )

    class Meta:
        model = DesignApply
        fields = ("id", "design_title", "device_name", "prior_device_status")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(design_title__icontains=value) | Q(device_name__icontains=value)
        )


class TemplateGroupFilterSet(NetBoxModelFilterSet):
    class Meta:
        model = TemplateGroup
        fields = ("id", "name", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value))


class TemplateFilterSet(NetBoxModelFilterSet):
    group_id = django_filters.ModelMultipleChoiceFilter(
        queryset=TemplateGroup.objects.all(), label="Group (ID)"
    )

    class Meta:
        model = Template
        fields = ("id", "name", "description", "order", "u_height")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value))


class TemplatePlacementFilterSet(NetBoxModelFilterSet):
    template_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Template.objects.all(), label="Template (ID)"
    )
    device_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DeviceType.objects.all(), label="Device type (ID)"
    )
    # DeviceRole is MPTT-nested, mirroring DesignPlacementFilterSet.device_role_id.
    device_role_id = TreeNodeMultipleChoiceFilter(
        queryset=DeviceRole.objects.all(),
        field_name="device_role",
        lookup_expr="in",
        label="Device role (ID)",
    )
    tenant_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Tenant.objects.all(), label="Tenant (ID)"
    )
    anchor = django_filters.MultipleChoiceFilter(choices=TemplatePlacementAnchorChoices)
    parent_placement_id = django_filters.ModelMultipleChoiceFilter(
        queryset=TemplatePlacement.objects.all(), label="Parent placement (ID)"
    )

    class Meta:
        model = TemplatePlacement
        fields = ("id", "order", "label", "face", "target_bay_name")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(label__icontains=value))
