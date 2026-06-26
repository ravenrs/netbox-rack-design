"""
PluginTemplateExtensions injecting NetBox Rack Design UI into core pages.

Currently adds a panel to the right column of the core dcim.rack detail page
listing the Designs whose placements touch that rack, each linking into the
read-only projected elevation for that design + rack.
"""

from django.db.models import Count, Q
from netbox.plugins import PluginTemplateExtension

from .models import Design, DesignPlacement

__all__ = ("RackDesignsPanel",)


class RackDesignsPanel(PluginTemplateExtension):
    """Right-column panel on a Rack page: the designs that touch this rack."""

    models = ["dcim.rack"]

    def right_page(self):
        if not self.context["config"].get("enable_rack_panel"):
            return ""

        rack = self.context["object"]
        user = self.context["request"].user

        # A placement touches this rack if it targets the rack (add/move) or it
        # references an existing device that currently lives in the rack
        # (move/remove). One filtered query, restricted to viewable placements.
        touching = DesignPlacement.objects.restrict(user, "view").filter(
            Q(target_rack=rack) | Q(device__rack=rack)
        )

        # Distinct parent designs, restricted by object permissions, annotated
        # with how many of their placements affect this rack.
        designs = (
            Design.objects.restrict(user, "view")
            .filter(placements__in=touching)
            .annotate(
                rack_placement_count=Count(
                    "placements",
                    filter=Q(
                        Q(placements__target_rack=rack)
                        | Q(placements__device__rack=rack)
                    ),
                    distinct=True,
                )
            )
            .distinct()
        )

        if not designs:
            return ""

        return self.render(
            "netbox_rack_design/inc/rack_designs_panel.html",
            extra_context={"designs": designs, "rack": rack},
        )


template_extensions = [RackDesignsPanel]
