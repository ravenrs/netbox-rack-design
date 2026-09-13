"""Global-search indexes for NetBox Rack Design."""

from netbox.search import SearchIndex

from .models import Design, DesignGroup, DesignPowerFeed, PlannedRack, Template, TemplateGroup, TemplatePlacement

__all__ = (
    "DesignIndex", "DesignGroupIndex", "DesignPowerFeedIndex", "PlannedRackIndex",
    "TemplateGroupIndex", "TemplateIndex", "TemplatePlacementIndex", "indexes",
)


class DesignIndex(SearchIndex):
    model = Design
    fields = (
        ("title", 100),
        ("summary", 300),
        ("description", 500),
        ("comments", 5000),
    )
    display_attrs = ("site", "status", "version", "summary")


class DesignGroupIndex(SearchIndex):
    model = DesignGroup
    fields = (
        ("name", 100),
        ("description", 500),
    )
    display_attrs = ("parent", "description")


class DesignPowerFeedIndex(SearchIndex):
    model = DesignPowerFeed
    fields = (
        ("name", 100),
    )
    display_attrs = ("design", "rack", "voltage", "amperage")


class PlannedRackIndex(SearchIndex):
    model = PlannedRack
    fields = (
        ("name", 100),
        ("description", 500),
        ("comments", 5000),
    )
    display_attrs = ("location", "u_height", "is_realized")


class TemplateGroupIndex(SearchIndex):
    model = TemplateGroup
    fields = (
        ("name", 100),
        ("description", 500),
    )
    display_attrs = ("description",)


class TemplateIndex(SearchIndex):
    model = Template
    fields = (
        ("name", 100),
        ("description", 500),
    )
    display_attrs = ("group", "u_height")


class TemplatePlacementIndex(SearchIndex):
    model = TemplatePlacement
    fields = (
        ("label", 100),
    )
    display_attrs = ("template", "device_type", "anchor")


indexes = (
    DesignIndex, DesignGroupIndex, DesignPowerFeedIndex, PlannedRackIndex,
    TemplateGroupIndex, TemplateIndex, TemplatePlacementIndex,
)
