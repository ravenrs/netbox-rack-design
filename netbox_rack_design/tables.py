"""Tables for NetBox Rack Design."""

import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from netbox.tables import NetBoxTable, columns

from .models import Design, DesignGroup, DesignPlacement

__all__ = ("DesignGroupTable", "DesignTable", "DesignPlacementTable", "ElevationTable")


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


class ElevationTable(tables.Table):
    """
    List of (design, rack) "elevation" pairs. There is no Elevation model, so this
    is a plain django_tables2.Table fed dict rows produced by the view, each:
        {"design": <Design>, "rack": <Rack>, "site": <Site|None>, "placement_count": int}
    """

    design = tables.Column(linkify=lambda record: record["design"].get_absolute_url(), verbose_name=_("Design"))
    rack = tables.Column(linkify=lambda record: record["rack"].get_absolute_url(), verbose_name=_("Rack"))
    site = tables.Column(
        linkify=lambda record: record["site"].get_absolute_url() if record["site"] else None,
        verbose_name=_("Site"),
    )
    status = tables.Column(empty_values=(), verbose_name=_("Status"), orderable=False)
    placement_count = tables.Column(verbose_name=_("Placements"), orderable=False)
    actions = tables.Column(empty_values=(), verbose_name=_("Actions"), orderable=False)

    class Meta:
        attrs = {"class": "table table-hover object-list"}
        fields = ("design", "rack", "site", "status", "placement_count", "actions")
        empty_text = _("No elevations found")

    def render_status(self, record):
        design = record["design"]
        return format_html(
            '<span class="badge text-bg-{}">{}</span>',
            design.get_status_color(),
            design.get_status_display(),
        )

    def render_actions(self, record):
        design = record["design"]
        rack = record["rack"]
        view_url = reverse(
            "plugins:netbox_rack_design:design_elevation",
            kwargs={"pk": design.pk, "rack_id": rack.pk},
        )
        edit_url = reverse(
            "plugins:netbox_rack_design:design_editor",
            kwargs={"pk": design.pk, "rack_id": rack.pk},
        )
        return format_html(
            '<a href="{}" class="btn btn-sm btn-primary">'
            '<i class="mdi mdi-eye"></i> {}</a> '
            '<a href="{}" class="btn btn-sm btn-warning">'
            '<i class="mdi mdi-pencil"></i> {}</a>',
            view_url, _("View elevation"),
            edit_url, _("Edit"),
        )


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
