"""strawberry-django GraphQL types for NetBox Rack Design."""

from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType
from strawberry import Info

from .. import projection
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
from .filters import (
    DesignFilter,
    DesignGroupFilter,
    DesignPlacementFilter,
    DesignPowerFeedFilter,
    DesignStepFilter,
    PlannedRackFilter,
    TemplateFilter,
    TemplateGroupFilter,
    TemplatePlacementFilter,
)

if TYPE_CHECKING:
    from dcim.graphql.types import (
        DeviceBayType,
        DeviceRoleType,
        DeviceType,
        DeviceTypeType,
        LocationType,
        PowerFeedType,
        PowerPanelType,
        RackType,
        SiteType,
    )
    from tenancy.graphql.types import TenantType

__all__ = (
    "DesignLineageEntry",
    "DesignGroupType",
    "DesignType",
    "DesignPlacementType",
    "DesignPowerFeedType",
    "DesignStepType",
    "PlannedRackType",
    "TemplateGroupType",
    "TemplateType",
    "TemplatePlacementType",
)

# One query must not fan out into an unbounded child list. The lineage walk is
# already capped by models.MAX_LINEAGE_DEPTH; pair both with NetBox's
# GRAPHQL_MAX_QUERY_DEPTH (docs/api.md).
MAX_CHILDREN = 200


@strawberry.type
class DesignLineageEntry:
    """One ancestor in ``Design.lineage`` -- the GraphQL twin of an entry in the
    REST ``chain`` endpoint. An ancestor the caller may not view is a placeholder
    (``restricted`` true, ``id``/``design`` null, ``title`` the label), so the
    chain keeps its length without naming the design."""

    id: strawberry.ID | None
    title: str
    version: int | None
    status: str | None
    restricted: bool
    design: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None


def _viewable(info, design):
    """``design`` if the caller may view it, else ``None``. A forward FK is not
    restricted by strawberry-django, so without this ``based_on { description }``
    (or ``placement { design { ... } }``) hands out a design the caller's
    ``view_design`` constraints exclude -- the field resolves null instead, the
    convention this module's other cross-model FKs already document."""
    if design is None:
        return None
    return design if projection.visible_design_ids(info.context.request.user, [design.pk]) else None


def _resolve_based_on(root, info: Info):
    return _viewable(info, root.based_on)


def _resolve_root(root, info: Info):
    return _viewable(info, root.root)


def _resolve_depends_on(root, info: Info):
    return list(root.depends_on.restrict(info.context.request.user, "view")[:MAX_CHILDREN])


def _resolve_design(root, info: Info):
    return _viewable(info, root.design)


def _lineage(design):
    """``design.baseline_chain()``, or ``[]`` for a lineage that cannot be walked
    (a cycle, or deeper than ``MAX_LINEAGE_DEPTH``) -- a GraphQL field must never
    500 on that, and ``projection.resolve_baseline_chain`` degrades the same way."""
    try:
        return design.baseline_chain()
    except ValueError:
        return []


@strawberry_django.type(DesignGroup, fields="__all__", filters=DesignGroupFilter, pagination=True)
class DesignGroupType(NetBoxObjectType):
    parent: Annotated["DesignGroupType", strawberry.lazy("netbox_rack_design.graphql.types")] | None


@strawberry_django.type(Design, fields="__all__", filters=DesignFilter, pagination=True)
class DesignType(NetBoxObjectType):
    # M9 (PLAN-multi-site.md): `Design.sites` is now a M2M. A list field, unlike
    # a nullable-FK field, simply omits any related Site the requesting user
    # lacks 'view' on rather than needing the whole field to be Optional.
    sites: list[Annotated["SiteType", strawberry.lazy("dcim.graphql.types")]]
    group: Annotated["DesignGroupType", strawberry.lazy("netbox_rack_design.graphql.types")] | None
    # based_on / root / depends_on reach other designs: resolved through
    # _viewable / a restricted queryset, never as a bare FK (see _viewable).
    # Declared as annotation + resolver (not a method of the same name as the
    # model field), which strawberry-django resolves to a fieldless
    # DjangoModelType.
    based_on: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None = (
        strawberry_django.field(resolver=_resolve_based_on)
    )
    root: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None = (
        strawberry_django.field(resolver=_resolve_root)
    )
    depends_on: list[Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")]] = (
        strawberry_django.field(resolver=_resolve_depends_on)
    )
    # planned_racks (M2M to PlannedRack): declared explicitly like depends_on
    # above rather than relying on PlannedRackType being registered elsewhere
    # first, so this field's resolution never depends on import order.
    planned_racks: list[Annotated["PlannedRackType", strawberry.lazy("netbox_rack_design.graphql.types")]]

    # --- design chains (PLAN-design-chains.md G9) ----------------------------
    # ``children`` is a reverse-relation property (Design.children), not a
    # Django field, so ``fields="__all__"`` never picks it up -- it needs an
    # explicit resolver like every other non-field attribute on this type.
    @strawberry_django.field
    def children(self, info: Info) -> list[Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")]]:
        # Only designs the caller may view (a list, so a hidden one is dropped,
        # like REST ``chain``), capped at MAX_CHILDREN.
        return list(self.children.restrict(info.context.request.user, "view")[:MAX_CHILDREN])

    # ``is_frozen`` (Design.is_frozen) is a plain bool property.
    @strawberry_django.field
    def is_frozen(self) -> bool:
        return self.is_frozen

    @strawberry_django.field
    def ancestors(
        self, info: Info
    ) -> list[Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None]:
        """
        The ordered ``based_on`` chain (oldest first), i.e.
        ``Design.baseline_chain()`` surfaced over GraphQL. It keeps the chain
        length like REST ``chain``: an ancestor the caller may not view is
        ``null`` at its position. ``lineage`` carries the redacted placeholder
        (title, restricted flag) for those positions.

        An unwalkable lineage (see ``_lineage``) is an empty list. A client that
        needs to tell "no parent" from "broken lineage" has ``based_on`` for
        that: a non-null ``based_on`` with empty ``ancestors`` says exactly that.
        """
        chain = _lineage(self)
        visible = projection.visible_design_ids(info.context.request.user, [d.pk for d in chain])
        return [d if d.pk in visible else None for d in chain]

    @strawberry_django.field
    def lineage(
        self, info: Info
    ) -> list[Annotated["DesignLineageEntry", strawberry.lazy("netbox_rack_design.graphql.types")]]:
        """The full ``based_on`` chain (oldest first), a hidden ancestor redacted
        to a placeholder -- the same shape and length as REST ``chain``."""
        chain = _lineage(self)
        visible = projection.visible_design_ids(info.context.request.user, [d.pk for d in chain])
        return [
            DesignLineageEntry(
                id=strawberry.ID(str(d.pk)), title=d.title, version=d.version, status=d.status,
                restricted=False, design=d,
            ) if d.pk in visible else DesignLineageEntry(
                id=None, title=projection.RESTRICTED_DESIGN_LABEL, version=None, status=None,
                restricted=True, design=None,
            )
            for d in chain
        ]


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
    design: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None = strawberry_django.field(resolver=_resolve_design)

    rack: Annotated["RackType", strawberry.lazy("dcim.graphql.types")] | None
    # planned_rack (D25/T1.8b): the planned-rack counterpart of ``rack`` --
    # declared explicitly, like DesignPlacementType.target_planned_rack below,
    # because ``fields="__all__"`` otherwise resolves a bare, fieldless
    # DjangoModelType for it, which breaks the generated GraphQL tests.
    planned_rack: Annotated[
        "PlannedRackType", strawberry.lazy("netbox_rack_design.graphql.types")
    ] | None
    # power_panel: the panel a planned feed hangs from (migration 0024) --
    # declared for the same reason as planned_rack above.
    power_panel: Annotated["PowerPanelType", strawberry.lazy("dcim.graphql.types")] | None


# One step of a design's execution plan (PLAN-execution-steps.md Sec. 3), and
# the nested target of DesignPlacement.step.
@strawberry_django.type(DesignStep, fields="__all__", filters=DesignStepFilter, pagination=True)
class DesignStepType(NetBoxObjectType):
    design: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None = strawberry_django.field(resolver=_resolve_design)


# A PLANNED rack. Queryable in its own right, and the nested target of
# DesignPlacement.target_planned_rack and Design.planned_racks -- same
# dual role as DesignPowerFeedType above.
@strawberry_django.type(
    PlannedRack,
    fields="__all__",
    filters=PlannedRackFilter,
    pagination=True,
)
class PlannedRackType(NetBoxObjectType):
    # Cross-app FKs are nullable for the same reason as DesignPowerFeedType's:
    # under object-level permissions a related object the caller cannot view
    # resolves to null.
    location: Annotated["LocationType", strawberry.lazy("dcim.graphql.types")] | None
    realized_rack: Annotated["RackType", strawberry.lazy("dcim.graphql.types")] | None


# A product pod: an ordered set of Templates (PLAN-templates.md §2, D14).
@strawberry_django.type(TemplateGroup, fields="__all__", filters=TemplateGroupFilter, pagination=True)
class TemplateGroupType(NetBoxObjectType):
    pass


# A reusable rack layout with no site (PLAN-templates.md §2, D13/D14).
@strawberry_django.type(Template, fields="__all__", filters=TemplateFilter, pagination=True)
class TemplateType(NetBoxObjectType):
    group: Annotated["TemplateGroupType", strawberry.lazy("netbox_rack_design.graphql.types")] | None


# One device within a Template (PLAN-templates.md §2, D8/D9).
@strawberry_django.type(TemplatePlacement, fields="__all__", filters=TemplatePlacementFilter, pagination=True)
class TemplatePlacementType(NetBoxObjectType):
    template: Annotated["TemplateType", strawberry.lazy("netbox_rack_design.graphql.types")] | None
    device_type: Annotated["DeviceTypeType", strawberry.lazy("dcim.graphql.types")] | None
    device_role: Annotated["DeviceRoleType", strawberry.lazy("dcim.graphql.types")] | None
    tenant: Annotated["TenantType", strawberry.lazy("tenancy.graphql.types")] | None
    # Same-template self-FK (D10) -- declared explicitly like
    # DesignPlacementType.parent_placement, since fields="__all__" otherwise
    # resolves it to a bare, fieldless DjangoModelType.
    parent_placement: Annotated[
        "TemplatePlacementType", strawberry.lazy("netbox_rack_design.graphql.types")
    ] | None


@strawberry_django.type(DesignPlacement, fields="__all__", filters=DesignPlacementFilter, pagination=True)
class DesignPlacementType(NetBoxObjectType):
    design: Annotated["DesignType", strawberry.lazy("netbox_rack_design.graphql.types")] | None = strawberry_django.field(resolver=_resolve_design)

    device: Annotated["DeviceType", strawberry.lazy("dcim.graphql.types")] | None
    device_type: Annotated["DeviceTypeType", strawberry.lazy("dcim.graphql.types")] | None
    device_role: Annotated["DeviceRoleType", strawberry.lazy("dcim.graphql.types")] | None
    tenant: Annotated["TenantType", strawberry.lazy("tenancy.graphql.types")] | None
    target_rack: Annotated["RackType", strawberry.lazy("dcim.graphql.types")] | None
    target_planned_rack: Annotated[
        "PlannedRackType", strawberry.lazy("netbox_rack_design.graphql.types")
    ] | None
    # from_template (PLAN-templates.md §3, D20): provenance -- which Template
    # this placement was stamped from, if any. Declared explicitly for the
    # same reason as target_planned_rack above: fields="__all__" otherwise
    # resolves it to a bare, fieldless DjangoModelType, which breaks the
    # generated GraphQL tests for THIS type (not TemplateType's).
    from_template: Annotated[
        "TemplateType", strawberry.lazy("netbox_rack_design.graphql.types")
    ] | None
    step: Annotated["DesignStepType", strawberry.lazy("netbox_rack_design.graphql.types")] | None
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
