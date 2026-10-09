"""strawberry-django GraphQL filters for NetBox Rack Design."""

import strawberry_django

from ..compat import GraphQLDescribedModelFilterBase, GraphQLModelFilterBase
from ..models import (
    Design,
    DesignGroup,
    DesignPlacement,
    DesignPowerFeed,
    DesignStep,
    PlannedRack,
    Template,
    TemplateGroup,
    TemplatePlacement,
)

__all__ = (
    "DesignGroupFilter",
    "DesignFilter",
    "DesignPlacementFilter",
    "DesignPowerFeedFilter",
    "DesignStepFilter",
    "PlannedRackFilter",
    "TemplateGroupFilter",
    "TemplateFilter",
    "TemplatePlacementFilter",
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


@strawberry_django.filter_type(DesignStep, lookups=True)
class DesignStepFilter(GraphQLModelFilterBase):
    pass


@strawberry_django.filter_type(DesignPowerFeed, lookups=True)
class DesignPowerFeedFilter(GraphQLModelFilterBase):
    pass


# PlannedRack carries `description` + `comments` for the same reason Design
# does (see Design's docstring and the comment on DesignFilter above) -- it is
# a NetBoxModel, not a PrimaryModel, so it needs the Described base to keep
# those two filters rather than dropping to the plain NetBoxModel filter base.
@strawberry_django.filter_type(PlannedRack, lookups=True)
class PlannedRackFilter(GraphQLDescribedModelFilterBase):
    pass


# TemplateGroup/Template carry `description` but NOT `comments` -- same shape
# as DesignGroup above, so they use the plain NetBoxModel filter base, not the
# Described one (which would add a `comments` filter for a field that does
# not exist on either model).
@strawberry_django.filter_type(TemplateGroup, lookups=True)
class TemplateGroupFilter(GraphQLModelFilterBase):
    pass


@strawberry_django.filter_type(Template, lookups=True)
class TemplateFilter(GraphQLModelFilterBase):
    pass


@strawberry_django.filter_type(TemplatePlacement, lookups=True)
class TemplatePlacementFilter(GraphQLModelFilterBase):
    pass
