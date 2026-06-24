"""strawberry-django GraphQL filters for NetBox Rack Design."""

import strawberry_django
from netbox.graphql.filter_mixins import NetBoxModelFilterMixin, PrimaryModelFilterMixin

from ..models import Design, DesignGroup, DesignPlacement

__all__ = ("DesignGroupFilter", "DesignFilter", "DesignPlacementFilter")


@strawberry_django.filter_type(DesignGroup, lookups=True)
class DesignGroupFilter(NetBoxModelFilterMixin):
    pass


@strawberry_django.filter_type(Design, lookups=True)
class DesignFilter(PrimaryModelFilterMixin):
    pass


@strawberry_django.filter_type(DesignPlacement, lookups=True)
class DesignPlacementFilter(NetBoxModelFilterMixin):
    pass
