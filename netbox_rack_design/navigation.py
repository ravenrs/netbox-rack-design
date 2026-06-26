"""Navigation menu for NetBox Rack Design."""

from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label="Rack Design",
    icon_class="mdi mdi-floor-plan",
    groups=(
        (
            "Designs",
            (
                PluginMenuItem(
                    link="plugins:netbox_rack_design:design_list",
                    link_text="Designs",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_rack_design:design_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_rack_design:designgroup_list",
                    link_text="Design Groups",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_rack_design:designgroup_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_rack_design:designplacement_list",
                    link_text="Placements",
                ),
                PluginMenuItem(
                    link="plugins:netbox_rack_design:elevation_browser",
                    link_text="Elevations",
                    permissions=["netbox_rack_design.view_design"],
                ),
            ),
        ),
    ),
)
