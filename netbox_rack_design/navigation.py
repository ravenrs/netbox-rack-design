"""
Navigation menu items for NetBox Rack Design.

For more information on navigation menus, see:
https://docs.netbox.dev/en/stable/plugins/development/navigation/
"""

from netbox.plugins import PluginMenuButton, PluginMenuItem

plugin_buttons = [
    PluginMenuButton(
        link="plugins:netbox_rack_design:rackdesign_add",
        title="Add",
        icon_class="mdi mdi-plus-thick",
    )
]

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_rack_design:rackdesign_list",
        link_text="Rack Design",
        buttons=plugin_buttons,
    ),
)
