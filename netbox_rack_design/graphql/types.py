"""strawberry-django GraphQL types for NetBox Rack Design."""

from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType

from ..models import Design, DesignGroup, DesignPlacement, DesignPowerFeed
from .filters import (
    DesignFilter,
    DesignGroupFilter,
    DesignPlacementFilter,
    DesignPowerFeedFilter,
)

if TYPE_CHECKING:
    from dcim.graphql.types import (
        DeviceBayType,
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

    # --- design chains (PLAN-design-chains.md G9) ----------------------------
    # ``children`` is a reverse-relation property (Design.children), not a
    # Django field, so ``fields="__all__"`` never picks it up -- it needs an
    # explicit resolver like every other non-field attribute on this type.
    @strawberry_django.field
    def children(self) -> list[Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")]]:
        return list(self.children)

    # ``is_frozen`` (Design.is_frozen) is a plain bool property.
    @strawberry_django.field
    def is_frozen(self) -> bool:
        return self.is_frozen

    @strawberry_django.field
    def ancestors(self) -> list[Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")]]:
        """
        The ordered ``based_on`` chain (oldest first), i.e.
        ``Design.baseline_chain()`` surfaced over GraphQL.

        ``baseline_chain()`` raises ``ValueError`` on a cycle in the lineage
        (a row saved before ``clean()`` grew its guard could already hold
        one) -- a GraphQL field must never 500 on that, so this degrades to
        an empty list exactly the way ``projection.resolve_baseline_chain``
        already degrades projection: an unresolvable lineage inherits
        nothing rather than crashing the query. A client that needs to
        distinguish "no parent" from "broken lineage" already has
        ``based_on`` for that -- a non-null ``based_on`` with empty
        ``ancestors`` says exactly that.
        """
        try:
            return self.baseline_chain()
        except ValueError:
            return []


# A PLANNED power feed. Now a queryable object in its own right (it became a
# NetBoxModel when it got its own list/detail/delete views), as well as the
# nested target of DesignPlacement.planned_power_feed.
@strawberry_django.type(
    DesignPowerFeed,
    fields="__all__",
    filters=DesignPowerFeedFilter,
    pagination=True,
)
class DesignPowerFeedType(NetBoxObjectType):
    # Cross-app/cross-model FKs are nullable for the same reason as DesignType's:
    # under object-level permissions a related object the caller cannot view
    # resolves to null.
    design: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None
    rack: Annotated["RackType", strawberry.lazy("dcim.graphql.types")] | None


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
    # Device-bay targeting (a blade into a chassis). Both nullable: a placement
    # carries at most one, an ordinary rack placement neither. Declared
    # explicitly because ``fields="__all__"`` otherwise resolves them to a bare
    # DjangoModelType with no queryable fields, which breaks the generated
    # GraphQL test queries.
    target_bay: Annotated["DeviceBayType", strawberry.lazy("dcim.graphql.types")] | None
    parent_placement: Annotated[
        "DesignPlacementType", strawberry.lazy("netbox_rack_design.graphql.types")
    ] | None
    # base_placement (G2, PLAN-design-chains.md): same self-referential FK
    # situation as parent_placement above, same fix.
    base_placement: Annotated[
        "DesignPlacementType", strawberry.lazy("netbox_rack_design.graphql.types")
    ] | None
    # base_parent_placement (G2): the ancestor-planned chassis a blade goes into
    # -- a third self-referential FK, so it needs the same explicit lazy
    # annotation or the generated GraphQL tests break on a fieldless
    # DjangoModelType.
    base_parent_placement: Annotated[
        "DesignPlacementType", strawberry.lazy("netbox_rack_design.graphql.types")
    ] | None
