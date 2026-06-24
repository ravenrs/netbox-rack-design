"""Tables for NetBox Rack Design."""

import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from .models import Design, DesignGroup, DesignPlacement

__all__ = ("DesignGroupTable", "DesignTable", "DesignPlacementTable")


class DesignGroupTable(NetBoxTable):
    name = tables.Column(linkify=True)
    parent = tables.Column(linkify=True)
    design_count = columns.LinkedCountColumn(
        viewname="plugins:netbox_rack_design:design_list",
        url_params={"group_id": "pk"},
        verbose_name="Designs",
    )

    class Meta(NetBoxTable.Meta):
        model = DesignGroup
        fields = ("pk", "id", "name", "parent", "design_count", "description", "link", "actions")
        default_columns = ("name", "parent", "design_count", "description")


class DesignTable(NetBoxTable):
    title = tables.Column(linkify=True)
    site = tables.Column(linkify=True)
    status = columns.ChoiceFieldColumn()
    group = tables.Column(linkify=True)
    placement_count = columns.LinkedCountColumn(
        viewname="plugins:netbox_rack_design:designplacement_list",
        url_params={"design_id": "pk"},
        verbose_name="Placements",
    )

    class Meta(NetBoxTable.Meta):
        model = Design
        fields = (
            "pk", "id", "title", "site", "status", "version", "sequence",
            "group", "placement_count", "summary", "created", "last_updated", "actions",
        )
        default_columns = ("title", "site", "status", "version", "sequence", "group", "placement_count")


class DesignPlacementTable(NetBoxTable):
    design = tables.Column(linkify=True)
    kind = columns.ChoiceFieldColumn()
    device = tables.Column(linkify=True)
    device_type = tables.Column(linkify=True)
    target_rack = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = DesignPlacement
        fields = (
            "pk", "id", "design", "kind", "device", "device_type", "proposed_name",
            "target_rack", "target_position", "target_face", "actions",
        )
        default_columns = (
            "design", "kind", "device", "device_type", "target_rack", "target_position", "target_face",
        )
