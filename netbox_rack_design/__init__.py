"""
NetBox Rack Design

Plugin configuration for NetBox Rack Design.

For a complete list of PluginConfig attributes, see:
https://docs.netbox.dev/en/stable/plugins/development/#pluginconfig-attributes
"""

__author__ = """Petr Voronov"""
__email__ = "ravenrs@gmail.com"
__version__ = "0.13.3"


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
        # --- Naming-convention engine (see naming.py) --------------------------
        # How a placement's proposed name is computed.
        #   "sequence" -> "<design title>-<n>"
        #   "template" -> a str.format template over real model objects
        #   "script"   -> a dotted path to fn(placement) -> str
        "naming_mode": "sequence",
        # Template used when naming_mode == "template". Dotted attribute paths on
        # the real Design/Device objects; {design.name} aliases the design title.
        "naming_template": "{design.name}-{n}",
        # Dotted path to a callable used when naming_mode == "script".
        "naming_script": "",
        # --- Power distribution engine (see distribution.py, docs/pdu-           -
        # distribution-spec.md) ---------------------------------------------------
        # How per-PDU/bank load is distributed for the power heatmap.
        #   "none"    -> per-rack total only, per-device gradient (default)
        #   "builtin" -> native distribution from the two universal conventions
        #                (bank = outlet port name segment, feed-leg = the bound
        #                feed) -- no config, no script.
        #   "script"  -> a dotted path to fn(rack, devices) -> Distribution dict
        "distribution_mode": "none",
        # Dotted path to a callable used when distribution_mode == "script".
        "distribution_script": "",
        # Custom-field bridge for the planning dialogs (Tier 2, §5). Maps site
        # custom fields into the rack/PDU planning inputs -- NATIVE fields
        # (voltage/amperage/phase/supply, the feed binding) are never listed
        # here. Empty by default: the base "builtin" feature needs none, and the
        # rack-power dialog then shows only the copy-from-rack row. Example:
        #   "planning_fields": {
        #     "rack": [
        #       {"key": "power_limitation", "label": "Power limitation (W)",
        #        "type": "number", "source": "cf.power_limitation"},
        #       {"key": "pdu_location", "label": "PDU location", "type": "choice",
        #        "choices": ["top", "bottom"], "source": "cf.pdu_location"},
        #     ],
        #   }
        "planning_fields": {},
    }


config = RackdesignConfig
