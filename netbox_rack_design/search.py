"""Global-search indexes for NetBox Rack Design."""

from netbox.search import SearchIndex

from .models import Design, DesignGroup

__all__ = ("DesignIndex", "DesignGroupIndex", "indexes")


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


indexes = (DesignIndex, DesignGroupIndex)
