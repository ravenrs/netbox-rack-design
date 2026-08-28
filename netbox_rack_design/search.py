"""Global-search indexes for NetBox Rack Design."""

from netbox.search import SearchIndex

from .models import Design, DesignGroup, DesignPowerFeed

__all__ = ("DesignIndex", "DesignGroupIndex", "DesignPowerFeedIndex", "indexes")


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


indexes = (DesignIndex, DesignGroupIndex, DesignPowerFeedIndex)
