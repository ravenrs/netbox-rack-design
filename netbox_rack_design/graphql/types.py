"""strawberry-django GraphQL types for NetBox Rack Design."""

from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType

from ..models import Design, DesignGroup, DesignPlacement
from .filters import DesignFilter, DesignGroupFilter, DesignPlacementFilter

if TYPE_CHECKING:
    from dcim.graphql.types import DeviceType, DeviceTypeType, RackType, SiteType

__all__ = ("DesignGroupType", "DesignType", "DesignPlacementType")


@strawberry_django.type(DesignGroup, fields="__all__", filters=DesignGroupFilter, pagination=True)
class DesignGroupType(NetBoxObjectType):
    parent: Annotated["DesignGroupType", strawberry.lazy("netbox_rack_design.graphql.types")] | None


@strawberry_django.type(Design, fields="__all__", filters=DesignFilter, pagination=True)
class DesignType(NetBoxObjectType):
    # Cross-app FK: under object-level permissions a related object the GraphQL
    # user cannot view resolves to null, so the field must be nullable (the
    # established real-plugin pattern, e.g. netbox-bgp) even though site is
    # required at the DB level.
    site: Annotated["SiteType", strawberry.lazy("dcim.graphql.types")] | None
    group: Annotated["DesignGroupType", strawberry.lazy("netbox_rack_design.graphql.types")] | None
    based_on: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None
    root: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None
    depends_on: list[Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")]]


@strawberry_django.type(DesignPlacement, fields="__all__", filters=DesignPlacementFilter, pagination=True)
class DesignPlacementType(NetBoxObjectType):
    design: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None
    device: Annotated["DeviceType", strawberry.lazy("dcim.graphql.types")] | None
    device_type: Annotated["DeviceTypeType", strawberry.lazy("dcim.graphql.types")] | None
    target_rack: Annotated["RackType", strawberry.lazy("dcim.graphql.types")] | None
