"""strawberry-django GraphQL filters for NetBox Rack Design."""

import strawberry_django

from ..compat import GraphQLDescribedModelFilterBase, GraphQLModelFilterBase
from ..models import Design, DesignGroup, DesignPlacement, DesignPowerFeed

__all__ = (
    "DesignGroupFilter",
    "DesignFilter",
    "DesignPlacementFilter",
    "DesignPowerFeedFilter",
)


@strawberry_django.filter_type(DesignGroup, lookups=True)
class DesignGroupFilter(GraphQLModelFilterBase):
    pass


# Design carries `description` + `comments` but is no longer a PrimaryModel (see
# models.Design). The PrimaryModel-level filter base contributes exactly those two
# lookups and nothing else, so keeping it here preserves the published GraphQL filter
# input unchanged; the alternative -- dropping to the NetBoxModel base -- would have
# silently removed both filters from existing queries.
@strawberry_django.filter_type(Design, lookups=True)
class DesignFilter(GraphQLDescribedModelFilterBase):
    pass


@strawberry_django.filter_type(DesignPlacement, lookups=True)
class DesignPlacementFilter(GraphQLModelFilterBase):
    pass


@strawberry_django.filter_type(DesignPowerFeed, lookups=True)
class DesignPowerFeedFilter(GraphQLModelFilterBase):
    pass
