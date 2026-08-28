"""GraphQL schema (Query) for NetBox Rack Design."""


import strawberry
import strawberry_django

from .types import (
    DesignGroupType,
    DesignPlacementType,
    DesignPowerFeedType,
    DesignType,
)

__all__ = ("RackDesignQuery", "schema")


@strawberry.type(name="Query")
class RackDesignQuery:
    design_group: DesignGroupType = strawberry_django.field()
    design_group_list: list[DesignGroupType] = strawberry_django.field()

    design: DesignType = strawberry_django.field()
    design_list: list[DesignType] = strawberry_django.field()

    design_placement: DesignPlacementType = strawberry_django.field()
    design_placement_list: list[DesignPlacementType] = strawberry_django.field()

    # Named for the model's verbose name ("planned power feed"), which is what
    # NetBox's generated GraphQL queries look for.
    planned_power_feed: DesignPowerFeedType = strawberry_django.field()
    planned_power_feed_list: list[DesignPowerFeedType] = strawberry_django.field()


schema = [RackDesignQuery]
