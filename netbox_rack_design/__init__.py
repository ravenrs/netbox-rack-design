"""
NetBox Rack Design

Plugin configuration for NetBox Rack Design.

For a complete list of PluginConfig attributes, see:
https://docs.netbox.dev/en/stable/plugins/development/#pluginconfig-attributes
"""

__author__ = """Petr Voronov"""
__email__ = "ravenrs@gmail.com"
__version__ = "0.1.0"


from netbox.plugins import PluginConfig


class RackdesignConfig(PluginConfig):
    name = "netbox_rack_design"
    verbose_name = "NetBox Rack Design"
    description = "NetBox plugin for Rack Design."
    author= "Petr Voronov"
    author_email = "ravenrs@gmail.com"
    version = __version__
    base_url = "netbox_rack_design"
    min_version = "4.5.0"
    max_version = "4.5.99"
    graphql_schema = "graphql.schema"


config = RackdesignConfig
