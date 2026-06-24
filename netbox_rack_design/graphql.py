"""GraphQL schema for NetBox Rack Design."""

from typing import List

import strawberry
import strawberry_django

from .models import Design, DesignGroup, DesignPlacement

__all__ = ("DesignGroupType", "DesignType", "DesignPlacementType", "schema")


@strawberry_django.type(DesignGroup, fields="__all__")
class DesignGroupType:
    pass


@strawberry_django.type(Design, fields="__all__")
class DesignType:
    pass


@strawberry_django.type(DesignPlacement, fields="__all__")
class DesignPlacementType:
    pass


@strawberry.type(name="Query")
class RackDesignQuery:
    design_group: DesignGroupType = strawberry_django.field()
    design_group_list: List[DesignGroupType] = strawberry_django.field()

    design: DesignType = strawberry_django.field()
    design_list: List[DesignType] = strawberry_django.field()

    design_placement: DesignPlacementType = strawberry_django.field()
    design_placement_list: List[DesignPlacementType] = strawberry_django.field()


schema = [RackDesignQuery]
