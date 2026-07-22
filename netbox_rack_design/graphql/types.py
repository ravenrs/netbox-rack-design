"""strawberry-django GraphQL types for NetBox Rack Design."""

from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType

from ..models import Design, DesignGroup, DesignPlacement, DesignPowerFeed
from .filters import DesignFilter, DesignGroupFilter, DesignPlacementFilter

if TYPE_CHECKING:
    from dcim.graphql.types import (
        DeviceRoleType,
        DeviceType,
        DeviceTypeType,
        PowerFeedType,
        RackType,
        SiteType,
    )
    from tenancy.graphql.types import TenantType

__all__ = (
    "DesignGroupType",
    "DesignType",
    "DesignPlacementType",
    "DesignPowerFeedType",
)


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


# A PLANNED power feed (plain planning model). Exposed only as the nested target
# of DesignPlacement.planned_power_feed -- just its electricals + identity, no
# nested design/rack FKs (keeps the schema flat and avoids over-exposing).
@strawberry_django.type(
    DesignPowerFeed,
    fields=["id", "name", "voltage", "amperage", "phase", "supply"],
    pagination=True,
)
class DesignPowerFeedType:
    pass


@strawberry_django.type(DesignPlacement, fields="__all__", filters=DesignPlacementFilter, pagination=True)
class DesignPlacementType(NetBoxObjectType):
    design: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None
    device: Annotated["DeviceType", strawberry.lazy("dcim.graphql.types")] | None
    device_type: Annotated["DeviceTypeType", strawberry.lazy("dcim.graphql.types")] | None
    device_role: Annotated["DeviceRoleType", strawberry.lazy("dcim.graphql.types")] | None
    tenant: Annotated["TenantType", strawberry.lazy("tenancy.graphql.types")] | None
    target_rack: Annotated["RackType", strawberry.lazy("dcim.graphql.types")] | None
    real_power_feed: Annotated["PowerFeedType", strawberry.lazy("dcim.graphql.types")] | None
    planned_power_feed: Annotated["DesignPowerFeedType", strawberry.lazy("netbox_rack_design.graphql.types")] | None
    power_source_device: Annotated["DeviceType", strawberry.lazy("dcim.graphql.types")] | None
