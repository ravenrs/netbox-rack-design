"""
NetBox Rack Design

Plugin configuration for NetBox Rack Design.

For a complete list of PluginConfig attributes, see:
https://docs.netbox.dev/en/stable/plugins/development/#pluginconfig-attributes
"""

__author__ = """Petr Voronov"""
__email__ = "ravenrs@gmail.com"
__version__ = "0.5.0"


from netbox.plugins import PluginConfig


class RackdesignConfig(PluginConfig):
    name = "netbox_rack_design"
    verbose_name = "NetBox Rack Design"
    description = "NetBox plugin for Rack Design."
    author= "Petr Voronov"
    author_email = "ravenrs@gmail.com"
    version = __version__
    base_url = "rack-design"
    min_version = "4.4.0"
    max_version = "4.4.99"
    graphql_schema = "graphql.schema"
    default_settings = {
        # Device statuses the plugin treats as "planned".
        "planned_statuses": ["planned"],
        # Device statuses that mark a planned removal. Default uses native
        # 'decommissioning'. Environments where that status is destructive
        # (auto-delete / inventory dismantle) should override with a custom
        # status added via FIELD_CHOICES (e.g. 'to_decommission').
        "removal_statuses": ["decommissioning"],
        # Default lifecycle status for a new Design.
        "default_status": "draft",
        # Show the rack-page panel listing designs that touch a rack.
        "enable_rack_panel": True,
    }


config = RackdesignConfig
