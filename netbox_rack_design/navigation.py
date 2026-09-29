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
                    link="plugins:netbox_rack_design:designpowerfeed_list",
                    link_text="Planned Power Feeds",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_rack_design:designpowerfeed_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_rack_design:plannedrack_list",
                    link_text="Planned Racks",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_rack_design:plannedrack_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_rack_design:elevation_browser",
                    link_text="Elevations",
                    permissions=["netbox_rack_design.view_design"],
                ),
                # Cross-design "which of my designs need attention" report
                # (PLAN-design-chains.md G4's reporting half): a refused
                # chain, or inert (stale) placements.
                PluginMenuItem(
                    link="plugins:netbox_rack_design:design_chain_health",
                    link_text="Chain Health",
                    permissions=["netbox_rack_design.view_design"],
                ),
            ),
        ),
        (
            "Templates",
            (
                PluginMenuItem(
                    link="plugins:netbox_rack_design:template_list",
                    link_text="Templates",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_rack_design:template_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_rack_design:templategroup_list",
                    link_text="Template Groups",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_rack_design:templategroup_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_rack_design:templateplacement_list",
                    link_text="Template Placements",
                ),
            ),
        ),
    ),
)
