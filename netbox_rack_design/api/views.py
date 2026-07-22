"""REST API viewsets for NetBox Rack Design."""

import logging

from dcim.models import Device, DeviceRole, DeviceType, PowerFeed, Rack
from django.core.exceptions import ValidationError
from django.db import transaction
from netbox.api.authentication import TokenPermissions
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from tenancy.models import Tenant

from .. import filtersets, naming, projection
from ..choices import DesignPlacementKindChoices
from ..models import (
    Design,
    DesignGroup,
    DesignPlacement,
    DesignPowerFeed,
    DesignRackPower,
    FavoriteDeviceType,
    HiddenDesignRack,
)
from .serializers import (
    DesignGroupSerializer,
    DesignPlacementSerializer,
    DesignRackScopeSerializer,
    DesignSerializer,
    FavoriteToggleSerializer,
    HiddenRackShowAllSerializer,
    HiddenRackToggleSerializer,
    PlannedFeedSerializer,
    PlannedFeedUpsertSerializer,
    PreviewNameSerializer,
    RackPowerSerializer,
    SaveLayoutSerializer,
)

logger = logging.getLogger("netbox_rack_design.api")

__all__ = (
    "DesignGroupViewSet",
    "DesignViewSet",
    "DesignPlacementViewSet",
    "FavoriteDeviceTypeViewSet",
    "HiddenDesignRackViewSet",
    "DeviceTypePowerViewSet",
)


class DesignGroupViewSet(NetBoxModelViewSet):
    queryset = DesignGroup.objects.all()
    serializer_class = DesignGroupSerializer
    filterset_class = filtersets.DesignGroupFilterSet


def _norm_pos(value):
    """Normalise a U position to a float for comparison, or None."""
    return None if value is None else float(value)


def _feed_dict(feed, source):
    """
    The uniform feed contract shared by the bind-to-feed picker
    (docs/pdu-distribution-spec.md §6/§8): a real ``dcim.PowerFeed`` and a
    planned ``DesignPowerFeed`` carry the same field names
    (name/voltage/amperage/phase/supply), so this reads either without a
    real-vs-planned branch. ``phase``/``supply`` are plain CharFields with
    choices (values already "single-phase"/"three-phase", "ac"/"dc"), but the
    getattr guards against a wrapped enum-like value just in case.
    """
    return {
        "id": feed.pk,
        "name": feed.name,
        "voltage": feed.voltage,
        "amperage": feed.amperage,
        "phase": getattr(feed.phase, "value", feed.phase),
        "supply": getattr(feed.supply, "value", feed.supply),
        "source": source,
    }


class ChangeDesignPermissions(TokenPermissions):
    """
    These detail @actions (save-layout, add-rack, remove-rack) are writes that
    EDIT an existing Design (not creation), so a POST to them must require
    ``change_design`` rather than the default ``add_design`` that
    TokenPermissions maps POST to.
    """

    perms_map = {
        **TokenPermissions.perms_map,
        "POST": ["%(app_label)s.change_%(model_name)s"],
    }


# Backwards-compatible alias (the save-layout action referenced this name).
SaveLayoutPermissions = ChangeDesignPermissions


class ViewDesignPermissions(TokenPermissions):
    """
    The preview-name @action is a POST that computes a would-be name without any
    write, so it must require only ``view_design`` rather than the ``add_design``
    that TokenPermissions maps POST to by default.
    """

    perms_map = {
        **TokenPermissions.perms_map,
        "POST": ["%(app_label)s.view_%(model_name)s"],
    }


class DesignViewSet(NetBoxModelViewSet):
    queryset = Design.objects.prefetch_related("placements", "depends_on", "racks", "tags")
    serializer_class = DesignSerializer
    filterset_class = filtersets.DesignFilterSet

    def get_permissions(self):
        action = getattr(self, "action", None)
        if action in ("save_layout", "add_rack", "remove_rack", "rack_power", "planned_feed"):
            return [ChangeDesignPermissions()]
        if action in ("preview_name", "power_source", "feeds"):
            return [ViewDesignPermissions()]
        return super().get_permissions()

    @action(detail=True, methods=["post"], url_path="preview-name")
    def preview_name(self, request, pk=None):
        """
        Compute the would-be name for a PROSPECTIVE placement WITHOUT saving.

        Builds an UNSAVED DesignPlacement on this design from the request body
        (resolving FKs by PK, tolerating missing ones), then asks the naming
        engine for the name and whether it already collides in the design's site.
        Performs NO writes: no placement is saved and no dcim object is mutated.

        Body (all optional except enough to identify the kind):
          kind ("add"|"move"|"remove", default "add"), device_type, device,
          device_role, tenant, target_rack (PKs), target_position, target_face,
          index (the ordinal the tile would take).

        Returns {"name": "<generated>", "exists_in_site": <bool>}.

        URL name: plugins-api:netbox_rack_design-api:design-preview-name
        Path:     /api/plugins/rack-design/designs/<pk>/preview-name/
        """
        # Read-only preview: scope to designs this user may view.
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "view")
        design = self.get_object()

        body = PreviewNameSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        # Resolve each supplied FK by PK; a non-null PK that does not resolve is a
        # clear 400 (mirrors the other actions). A missing/null PK is tolerated.
        resolved = {}
        for field, model in (
            ("device_type", DeviceType),
            ("device", Device),
            ("device_role", DeviceRole),
            ("tenant", Tenant),
            ("target_rack", Rack),
        ):
            pk_value = data.get(field)
            if pk_value is None:
                resolved[field] = None
                continue
            obj = model.objects.filter(pk=pk_value).first()
            if obj is None:
                return Response(
                    {field: [f"{model.__name__} does not exist."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            resolved[field] = obj

        placement = DesignPlacement(
            design=design,
            kind=data.get("kind", DesignPlacementKindChoices.KIND_ADD),
            device=resolved["device"],
            device_type=resolved["device_type"],
            device_role=resolved["device_role"],
            tenant=resolved["tenant"],
            target_rack=resolved["target_rack"],
            target_position=data.get("target_position"),
            target_face=data.get("target_face") or "",
        )
        # Same-session sibling names (user bug 2026-07-10): stamped onto the
        # unsaved placement (same pattern as _projected_vacated_device_ids)
        # so the naming engine -- the built-in sequence mode AND naming
        # scripts via naming.pending_names() -- can count unsaved siblings.
        placement._rd_pending_names = data.get("pending_names") or []

        name = naming.generate_name(placement, index=data.get("index"))
        exists = naming.name_exists_in_site(name, design.site, exclude_placement=None)
        return Response(
            {"name": name, "exists_in_site": exists}, status=status.HTTP_200_OK
        )

    @action(detail=True, methods=["post"], url_path="add-rack")
    def add_rack(self, request, pk=None):
        """
        Add a rack to this design's planning scope (the ``design.racks`` M2M).

        Enforces the same-site rule (a rack from another site is rejected),
        mirroring ``Design.clean()`` / the design form. Respects NetBox object
        permissions for editing the Design. Idempotent: re-adding a rack already
        in scope is a no-op. Returns the updated rack scope (``rack_ids``).

        URL name: plugins-api:netbox_rack_design-api:design-add-rack
        Path:     /api/plugins/rack-design/designs/<pk>/add-rack/
        """
        # Restrict to designs this user may change (object-level permission).
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "change")
        design = self.get_object()

        body = DesignRackScopeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        rack_id = body.validated_data["rack_id"]

        rack = Rack.objects.filter(pk=rack_id).first()
        if rack is None:
            return Response(
                {"rack_id": ["Rack does not exist."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Same-site rule, identical to Design.clean(): a scoped rack must belong
        # to the design's site.
        if rack.site_id != design.site_id:
            return Response(
                {"rack_id": ["This rack is not in the design's site."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        design.racks.add(rack)

        rack_ids = list(design.racks.values_list("pk", flat=True))
        return Response({"rack_ids": rack_ids}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="remove-rack")
    def remove_rack(self, request, pk=None):
        """
        Remove a rack from this design's planning scope (DESTRUCTIVE, confirmed).

        Planned placements whose DESTINATION is this rack (strictly
        ``target_rack == R`` -- the planned adds into R and the move-ins to R)
        become meaningless once R leaves the scope and are DELETED as part of the
        removal. Remove-kind placements that merely flag a real device in R (their
        destination is not R) are NOT touched unless their target_rack is also R.

        Two-step confirmation:
          * If there is at least one affected placement and the request is NOT
            confirmed (``confirm`` is false): nothing is deleted or detached.
            Responds 409 with ``{"requires_confirmation": true, "affected_count",
            "affected": [...]}``.
          * If ``confirm`` is true, OR there are zero affected placements: in a
            single transaction, delete the affected placements then detach R from
            ``design.racks``. Responds 200 with ``{"deleted_count", "rack_ids"}``.

        Never touches real dcim.Device/Rack -- only the design's own placements and
        the M2M link. Respects NetBox object permissions for editing the Design.

        URL name: plugins-api:netbox_rack_design-api:design-remove-rack
        Path:     /api/plugins/rack-design/designs/<pk>/remove-rack/
        """
        # Restrict to designs this user may change (object-level permission).
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "change")
        design = self.get_object()

        body = DesignRackScopeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        rack_id = body.validated_data["rack_id"]
        confirm = body.validated_data["confirm"]

        rack = Rack.objects.filter(pk=rack_id).first()
        if rack is None:
            return Response(
                {"rack_id": ["Rack does not exist."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Placements made meaningless by the removal: strictly those targeting R.
        affected = DesignPlacement.objects.filter(design=design, target_rack=rack)

        if affected.exists() and not confirm:
            return Response(
                {
                    "requires_confirmation": True,
                    "affected_count": affected.count(),
                    "affected": [
                        {
                            "placement_id": p.pk,
                            "kind": p.kind,
                            "device_or_type": str(
                                p.device or p.device_type or ""
                            ),
                            "u_position": (
                                float(p.target_position)
                                if p.target_position is not None
                                else None
                            ),
                        }
                        for p in affected
                    ],
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Confirmed (or nothing to delete): delete affected placements + detach R.
        with transaction.atomic():
            deleted_count = affected.count()
            affected.delete()
            design.racks.remove(rack)

        rack_ids = list(design.racks.values_list("pk", flat=True))
        return Response(
            {"deleted_count": deleted_count, "rack_ids": rack_ids},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get", "post"], url_path="rack-power")
    def rack_power(self, request, pk=None):
        """
        Upsert/read this design's per-rack power custom-field override
        (``DesignRackPower`` -- docs/pdu-distribution-spec.md). The rack is
        persistent design data, so POST saves it immediately (it does not wait
        for the layout Save). Never writes to dcim.

        GET  .../designs/<pk>/rack-power/?rack_id=<id>
             -> {"power_config": {...} | null}
        POST .../designs/<pk>/rack-power/  body {"rack_id", "power_config"}
             -> {"power_config": {...} | null}

        URL name: plugins-api:netbox_rack_design-api:design-rack-power
        Path:     /api/plugins/rack-design/designs/<pk>/rack-power/
        """
        if request.user.is_authenticated:
            perm = "change" if request.method == "POST" else "view"
            self.queryset = Design.objects.restrict(request.user, perm)
        design = self.get_object()

        if request.method == "POST":
            body = RackPowerSerializer(data=request.data)
            body.is_valid(raise_exception=True)
            rack_id = body.validated_data["rack_id"]
            power_config = body.validated_data.get("power_config")

            rack = Rack.objects.filter(pk=rack_id).first()
            if rack is None:
                return Response(
                    {"rack_id": ["Rack does not exist."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            rack_power, _created = DesignRackPower.objects.get_or_create(
                design=design, rack=rack
            )
            rack_power.power_config = power_config
            rack_power.save()
            logger.debug(
                "api.rack_power: design=%s rack_id=%s %s",
                design.pk, rack_id, "created" if _created else "updated",
            )
            return Response(
                {"power_config": rack_power.power_config}, status=status.HTTP_200_OK
            )

        # GET: reopen the rack-power dialog pre-filled with the stored config.
        rack_id = request.query_params.get("rack_id")
        if not rack_id:
            return Response(
                {"rack_id": ["This query parameter is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        rack_power = DesignRackPower.objects.filter(
            design=design, rack_id=rack_id
        ).first()
        return Response(
            {"power_config": rack_power.power_config if rack_power else None},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="power-source")
    def power_source(self, request, pk=None):
        """
        Read-only lookup for the "copy from rack" mode of the rack power dialog:
        a source rack's custom fields (``kind=rack``). Performs NO writes.

        In the universal feed-binding design a planned PDU **binds to a feed**
        (see the ``feeds``/``planned-feed`` actions) rather than copying another
        PDU's electricals, so there is no ``kind=pdu`` here -- the rack copy path
        is the only remaining copy-from-rack flow.

        GET .../designs/<pk>/power-source/?rack_id=<id>&kind=rack
          kind=rack -> {"custom_fields": {...}}

        URL name: plugins-api:netbox_rack_design-api:design-power-source
        Path:     /api/plugins/rack-design/designs/<pk>/power-source/
        """
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "view")
        # Enforces design-level view permission/object scoping for this lookup.
        self.get_object()

        kind = request.query_params.get("kind")
        if kind != "rack":
            return Response(
                {"kind": ["kind must be 'rack'."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        rack_id = request.query_params.get("rack_id")
        rack = Rack.objects.filter(pk=rack_id).first() if rack_id else None
        if rack is None:
            return Response(
                {"rack_id": ["Rack does not exist."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        logger.debug("api.power_source: kind=rack rack_id=%s", rack_id)
        return Response({"custom_fields": dict(rack.cf)}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="feeds")
    def feeds(self, request, pk=None):
        """
        Read-only lookup for the bind-to-feed picker (docs/pdu-distribution-
        spec.md §6.3/§8): this rack's real ``dcim.PowerFeed``s plus this
        design's planned ``DesignPowerFeed``s, each in the uniform feed shape
        (``_feed_dict``) so the picker can list real feeds first, then
        planned. Performs NO writes.

        GET .../designs/<pk>/feeds/?rack_id=<id>
          -> {"real": [{"id","name","voltage","amperage","phase","supply",
                        "source":"real"}, ...],
              "planned": [{..., "source":"planned"}, ...]}

        URL name: plugins-api:netbox_rack_design-api:design-feeds
        Path:     /api/plugins/rack-design/designs/<pk>/feeds/
        """
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "view")
        design = self.get_object()

        rack_id = request.query_params.get("rack_id")
        if not rack_id:
            return Response(
                {"rack_id": ["This query parameter is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        logger.debug("api.feeds: design=%s rack_id=%s", design.pk, rack_id)

        real_feeds = [
            _feed_dict(f, "real") for f in PowerFeed.objects.filter(rack_id=rack_id)
        ]
        planned_feeds = [
            _feed_dict(f, "planned")
            for f in DesignPowerFeed.objects.filter(design=design, rack_id=rack_id)
        ]
        logger.debug(
            "api.feeds: design=%s rack_id=%s real=%d planned=%d",
            design.pk, rack_id, len(real_feeds), len(planned_feeds),
        )
        return Response(
            {"real": real_feeds, "planned": planned_feeds}, status=status.HTTP_200_OK
        )

    @action(detail=True, methods=["get", "post"], url_path="planned-feed")
    def planned_feed(self, request, pk=None):
        """
        Upsert/list this design's planned power feeds (``DesignPowerFeed`` --
        docs/pdu-distribution-spec.md §6.1/§8), for the greenfield "define
        planned feed" dialog flow. Rack-scoped and design-scoped; upserts by
        the ``(design, rack, name)`` unique_together so re-submitting the same
        name UPDATES the electricals rather than duplicating the row. Never
        writes to dcim.

        GET  .../designs/<pk>/planned-feed/?rack_id=<id>
             -> [{"id","name","voltage","amperage","phase","supply"}, ...]
        POST .../designs/<pk>/planned-feed/ body {"rack_id","name","voltage",
             "amperage","phase","supply"} -> upsert, returns the one feed.

        URL name: plugins-api:netbox_rack_design-api:design-planned-feed
        Path:     /api/plugins/rack-design/designs/<pk>/planned-feed/
        """
        if request.user.is_authenticated:
            perm = "change" if request.method == "POST" else "view"
            self.queryset = Design.objects.restrict(request.user, perm)
        design = self.get_object()

        if request.method == "POST":
            body = PlannedFeedUpsertSerializer(data=request.data)
            body.is_valid(raise_exception=True)
            data = body.validated_data
            rack_id = data["rack_id"]

            rack = Rack.objects.filter(pk=rack_id).first()
            if rack is None:
                return Response(
                    {"rack_id": ["Rack does not exist."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Same-site rule, mirroring add-rack/rack_power: a planned feed can
            # only be defined for a rack in the design's own site.
            if rack.site_id != design.site_id:
                return Response(
                    {"rack_id": ["This rack is not in the design's site."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            electricals = {
                k: data[k] for k in ("voltage", "amperage", "phase", "supply") if k in data
            }
            feed, created = DesignPowerFeed.objects.get_or_create(
                design=design, rack=rack, name=data["name"], defaults=electricals
            )
            if not created and electricals:
                for field_name, value in electricals.items():
                    setattr(feed, field_name, value)
                feed.save()
            logger.debug(
                "api.planned_feed: design=%s rack_id=%s name=%s %s",
                design.pk, rack_id, data["name"], "created" if created else "updated",
            )
            return Response(PlannedFeedSerializer(feed).data, status=status.HTTP_200_OK)

        # GET: list this rack's planned feeds.
        rack_id = request.query_params.get("rack_id")
        if not rack_id:
            return Response(
                {"rack_id": ["This query parameter is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        feeds_qs = DesignPowerFeed.objects.filter(design=design, rack_id=rack_id)
        logger.debug(
            "api.planned_feed: design=%s rack_id=%s list count=%d",
            design.pk, rack_id, feeds_qs.count(),
        )
        return Response(
            PlannedFeedSerializer(feeds_qs, many=True).data, status=status.HTTP_200_OK
        )

    @action(detail=True, methods=["post"], url_path="save-layout")
    def save_layout(self, request, pk=None):
        """
        Persist an editor layout for a single design as a diff of DesignPlacement
        rows (move/remove only this slice). Real Devices are never mutated.

        URL name: plugins-api:netbox_rack_design-api:design-save-layout
        Path:     /api/plugins/rack-design/designs/<pk>/save-layout/
        """
        # The base viewset's initial() restricted the queryset to .restrict(user,
        # 'add') (POST). For this edit action we need 'change' scoping instead, so
        # the design must be one the user may change.
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "change")
        design = self.get_object()

        # Additionally require placement add/change/delete on this design's edits.
        for codename in (
            "netbox_rack_design.add_designplacement",
            "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement",
        ):
            if not request.user.has_perm(codename):
                raise PermissionDenied(
                    "This user does not have permission to modify design placements."
                )

        body = SaveLayoutSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        errors = []
        # Track which placements we want to keep, per submitted rack, so we can
        # delete the design's stale move/remove rows for those racks afterwards.
        desired_placement_ids = set()
        submitted_rack_ids = set()
        # Devices the payload explicitly mentioned, per submitted rack. This is the
        # ONLY basis on which we may delete a pre-existing move/remove placement:
        # the user must have actually addressed that device in the editor (e.g.
        # dragged it back to its real slot, which _reconcile_item handles by
        # deleting). A placement whose device was never submitted is left ALONE —
        # the payload merely failing to mention it must NEVER cause data loss.
        submitted_device_ids = set()
        # Set True by _reconcile_item whenever it actually writes (creates or
        # genuinely updates a placement) or deletes one via its real-position
        # branch. Combined with stale deletions below to decide 200 vs 304.
        self._made_db_change = False

        # Devices this submit frees from their real slots: any device the payload
        # moves or removes, plus an "existing" tile the editor actually relocated
        # (target U/face differs from the device's real position). These must not
        # count as occupying the rack when we validate another device moving into
        # the slot they vacate (the swap / move-into-vacated case). Computed once
        # over the whole batch so cross-rack and not-yet-persisted moves are seen.
        self._batch_vacated_device_ids = self._compute_vacated_device_ids(data)

        try:
            with transaction.atomic():
                for rack_data in data["racks"]:
                    rack_id = rack_data["rack_id"]
                    submitted_rack_ids.add(rack_id)
                    try:
                        rack = Rack.objects.get(pk=rack_id)
                    except Rack.DoesNotExist:
                        errors.append({
                            "rack_id": rack_id,
                            "u_position": None,
                            "device_id": None,
                            "detail": "Rack does not exist.",
                        })
                        continue

                    items = []
                    for face_key in ("front", "rear", "other"):
                        for item in rack_data.get(face_key, []):
                            items.append((face_key, item))

                    for face_key, item in items:
                        device_id = item.get("device_id")
                        if device_id:
                            submitted_device_ids.add(device_id)
                        self._reconcile_item(
                            design, rack, face_key, item, errors, desired_placement_ids
                        )

                if errors:
                    raise ValidationError("collision")

                # Conservative stale-deletion: only delete a move/remove placement
                # when the user ACTUALLY addressed that device in the editor but the
                # reconciliation produced no surviving placement for it (e.g. they
                # dragged a moved device back to its real slot in another part of the
                # same submit). A placement whose device was NOT submitted is kept —
                # the payload failing to mention a device must never delete its
                # placement. This is the guard against the data-loss incident.
                stale = (
                    DesignPlacement.objects.filter(
                        design=design,
                        kind__in=(
                            DesignPlacementKindChoices.KIND_MOVE,
                            DesignPlacementKindChoices.KIND_REMOVE,
                        ),
                    )
                    .exclude(pk__in=desired_placement_ids)
                )
                deleted_any = False
                for p in stale:
                    rack_id = p.target_rack_id or (
                        p.device.rack_id if p.device_id else None
                    )
                    # Require BOTH: the placement's rack was submitted AND its
                    # device was explicitly named in the payload for that submit.
                    if (
                        rack_id in submitted_rack_ids
                        and p.device_id is not None
                        and p.device_id in submitted_device_ids
                    ):
                        p.delete()
                        deleted_any = True

                # A change occurred only if we actually wrote/deleted something.
                made_changes = self._made_db_change or deleted_any

        except ValidationError:
            return Response(
                {"errors": errors}, status=status.HTTP_400_BAD_REQUEST
            )

        if not made_changes:
            return Response(status=status.HTTP_304_NOT_MODIFIED)

        # Serialize the design's resulting add/move/remove placements. KIND_ADD is
        # included here so a brand-new (or repositioned) catalog add is returned;
        # it is deliberately NOT in the stale-deletion filter above (adds are only
        # ever removed via explicit cancel, never by omission).
        placements = DesignPlacement.objects.filter(
            design=design,
            kind__in=(
                DesignPlacementKindChoices.KIND_ADD,
                DesignPlacementKindChoices.KIND_MOVE,
                DesignPlacementKindChoices.KIND_REMOVE,
            ),
        )
        serializer = DesignPlacementSerializer(
            placements, many=True, context={"request": request}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @staticmethod
    def _snapshot(placement):
        """A comparable tuple of a placement's meaningful (mutable) fields."""
        return (
            placement.kind,
            placement.device_id,
            placement.device_type_id,
            placement.target_rack_id,
            _norm_pos(placement.target_position),
            placement.target_face or "",
            placement.device_role_id,
            placement.tenant_id,
            placement.proposed_name or "",
            placement.power_config,
            placement.real_power_feed_id,
            placement.planned_power_feed_id,
            placement.power_source_device_id,
        )

    @staticmethod
    def _compute_vacated_device_ids(data):
        """Device PKs the whole submit frees from their real slots.

        A device vacates its physical slot when the payload removes it, moves it,
        or lists it as "existing" at a position/face different from where it
        really sits. Such devices must not count as occupying the rack when we
        validate another device moving into the slot they leave (swap / move into
        a vacated slot). Injected into each placement's slot validation so the
        collision check reflects the design's PROJECTED layout, not raw reality.
        """
        candidate_ids = set()
        # (device_id -> (u_position, face)) the payload asserts as "existing".
        existing_targets = {}
        for rack_data in data["racks"]:
            for face_key in ("front", "rear", "other"):
                for item in rack_data.get(face_key, []):
                    device_id = item.get("device_id")
                    if not device_id:
                        continue
                    kind = item.get("kind")
                    if kind in ("move", "remove"):
                        candidate_ids.add(device_id)
                    elif kind == "existing":
                        pos = item.get("u_position")
                        face = "" if face_key == "other" else (item.get("face") or "")
                        existing_targets[device_id] = (
                            _norm_pos(pos), face, rack_data.get("rack_id"),
                        )
        # An "existing" tile that was actually relocated also vacates its slot.
        if existing_targets:
            devices = Device.objects.filter(pk__in=existing_targets).only(
                "pk", "rack_id", "position", "face"
            )
            for dev in devices:
                target_pos, target_face, target_rack_id = existing_targets[dev.pk]
                real = (_norm_pos(dev.position), dev.face or "", dev.rack_id)
                if (target_pos, target_face, target_rack_id) != real:
                    candidate_ids.add(dev.pk)
        return candidate_ids

    @staticmethod
    def _item_is_full_depth(item):
        """
        True when the item's device/device_type spans the full rack depth.

        Full-depth devices occupy BOTH faces, so the editor renders (and may POST)
        one tile per face for the same device. Resolved from device_type_id (the
        editor stamps it on every tile), else the device's type, else the
        referenced placement's type. Callers normalise a full-depth item's face to
        "" so the per-face copies reconcile to a single, idempotent placement.
        """
        dt_id = item.get("device_type_id")
        if dt_id:
            dt = DeviceType.objects.filter(pk=dt_id).only("is_full_depth").first()
            return bool(dt and dt.is_full_depth)
        dev_id = item.get("device_id")
        if dev_id:
            dev = Device.objects.filter(pk=dev_id).select_related("device_type").first()
            return bool(dev and dev.device_type and dev.device_type.is_full_depth)
        placement_id = item.get("placement_id")
        if placement_id:
            p = (
                DesignPlacement.objects.filter(pk=placement_id)
                .select_related("device_type", "device__device_type")
                .first()
            )
            if p is not None:
                dt = p.device_type or (p.device.device_type if p.device_id else None)
                return bool(dt and dt.is_full_depth)
        return False

    @staticmethod
    def _resolve_add_refs(item, rack, u_position, errors):
        """
        Validate the optional device_role_id / tenant_id on an 'add' item.

        Returns (ok, device_role_id, tenant_id). On a non-null id that does not
        resolve, append an error (mirroring device_type) and return ok=False.
        """
        device_role_id = item.get("device_role_id")
        tenant_id = item.get("tenant_id")
        if device_role_id is not None and not DeviceRole.objects.filter(pk=device_role_id).exists():
            errors.append({
                "rack_id": rack.pk,
                "u_position": _norm_pos(u_position),
                "device_id": None,
                "detail": "Device role does not exist.",
            })
            return False, None, None
        if tenant_id is not None and not Tenant.objects.filter(pk=tenant_id).exists():
            errors.append({
                "rack_id": rack.pk,
                "u_position": _norm_pos(u_position),
                "device_id": None,
                "detail": "Tenant does not exist.",
            })
            return False, None, None
        return True, device_role_id, tenant_id

    @staticmethod
    def _resolve_feed_binding(item, rack, u_position, errors):
        """
        Resolve the optional ``real_power_feed_id`` / ``planned_power_feed_id``
        on a PDU add item (docs/pdu-distribution-spec.md §6.2/§8) into the pair
        to assign onto the placement.

        Exclusivity is enforced HERE, before ``full_clean()``, so a bad item
        reports through the same per-item ``errors`` list as the rest of add
        validation (``DesignPlacement.clean()`` also guards this as a second
        line of defense): both ids set on one item -> append an error and bind
        NEITHER. An id that does not resolve to a real row is skipped
        gracefully (logged), not a hard error -- a stale/removed feed must
        never crash Save.

        Returns (ok, real_feed_id, planned_feed_id). Absent keys resolve to
        None (no binding requested by this item).
        """
        real_id = item.get("real_power_feed_id")
        planned_id = item.get("planned_power_feed_id")

        if real_id and planned_id:
            errors.append({
                "rack_id": rack.pk,
                "u_position": _norm_pos(u_position),
                "device_id": None,
                "detail": "An item cannot bind to both a real and a planned power feed.",
            })
            return False, None, None

        if real_id and not PowerFeed.objects.filter(pk=real_id).exists():
            logger.debug(
                "api._reconcile_item: real_power_feed_id=%s does not exist, skipping binding",
                real_id,
            )
            real_id = None
        if planned_id and not DesignPowerFeed.objects.filter(pk=planned_id).exists():
            logger.debug(
                "api._reconcile_item: planned_power_feed_id=%s does not exist, skipping binding",
                planned_id,
            )
            planned_id = None

        return True, real_id, planned_id

    @staticmethod
    def _resolve_power_source_device(item):
        """
        Resolve the optional ``power_source_device_id`` on a PDU add item
        (docs/pdu-distribution-spec.md §6): the real PDU device this planned PDU
        inherits its custom fields from (read live off ``device.cf``). An id that
        does not resolve to a real device is skipped gracefully (logged), never a
        hard error. Returns the id to assign, or None. Absent key -> None.
        """
        source_id = item.get("power_source_device_id")
        if source_id and not Device.objects.filter(pk=source_id).exists():
            logger.debug(
                "api._reconcile_item: power_source_device_id=%s does not exist, skipping",
                source_id,
            )
            return None
        return source_id

    def _reconcile_item(self, design, rack, face_key, item, errors, desired_placement_ids):
        """
        Map one desired item to its DesignPlacement (or no placement), upserting
        and full_clean()-validating as needed. Appends to ``errors`` on failure
        and returns the placement (or None when no placement is needed).
        """
        kind = item["kind"]
        device_id = item.get("device_id")
        device_type_id = item.get("device_type_id")
        placement_id = item.get("placement_id")
        # 'other' bucket means off-rack: no target position.
        u_position = None if face_key == "other" else item.get("u_position")
        face = "" if face_key == "other" else item.get("face") or ""

        # Full-depth devices occupy BOTH faces; the editor renders one tile per
        # face for the same device. Their face is meaningless for placement (the
        # model treats a full-depth target_face as None -- models.py:295), so we
        # normalise it to "" here. This makes the two per-face copies the editor
        # submits reconcile to an IDENTICAL placement: a single row, idempotent on
        # a no-op (no front/rear flip-flop or spurious move), never a duplicate.
        full_depth = self._item_is_full_depth(item)
        if full_depth:
            face = ""

        # An 'add' tile is a catalog-add placement projected into this rack. When
        # it carries a placement_id (no device_id) it re-asserts an EXISTING add:
        # we preserve it and let the user REPOSITION it within the rack (drag to a
        # new U/face) or cancel it. When it carries NO placement_id but a
        # device_type_id, it is a BRAND-NEW catalog add and we CREATE the
        # placement. Real Devices are never created/mutated either way.
        if kind == "add":
            # Brand-new catalog add: no placement to reposition, build a fresh one.
            if not placement_id and device_type_id:
                dt = DeviceType.objects.filter(pk=device_type_id).first()
                if dt is None:
                    errors.append({
                        "rack_id": rack.pk,
                        "u_position": _norm_pos(u_position),
                        "device_id": None,
                        "detail": "Device type does not exist.",
                    })
                    return None
                ok, device_role_id, tenant_id = self._resolve_add_refs(
                    item, rack, u_position, errors
                )
                if not ok:
                    return None
                ok, real_feed_id, planned_feed_id = self._resolve_feed_binding(
                    item, rack, u_position, errors
                )
                if not ok:
                    return None
                new_add = DesignPlacement(
                    design=design,
                    kind=DesignPlacementKindChoices.KIND_ADD,
                    device_type=dt,
                    device_role_id=device_role_id,
                    tenant_id=tenant_id,
                    target_rack=rack,
                    target_position=u_position,
                    target_face=face,
                    # Editor-chosen name (auto-filled from the naming engine and/or
                    # user-edited). Absent => "" (the model field is blank=True).
                    proposed_name=(item.get("proposed_name") or ""),
                    # The PDU power dialog's stashed config (docs/pdu-distribution-
                    # spec.md); only meaningful for a PDU add, but persisted as-is
                    # for whatever role sent it. Absent => None.
                    power_config=item.get("power_config"),
                    # The feed this PDU binds to (docs/pdu-distribution-spec.md
                    # §6.2); at most one of the pair is set (enforced above and
                    # again by DesignPlacement.clean()).
                    real_power_feed_id=real_feed_id,
                    planned_power_feed_id=planned_feed_id,
                    # The real PDU whose cf this planned PDU inherits live (§6).
                    power_source_device_id=self._resolve_power_source_device(item),
                )
                new_add._projected_vacated_device_ids = getattr(
                    self, "_batch_vacated_device_ids", None
                )
                try:
                    new_add.full_clean()
                    new_add.save()
                    self._made_db_change = True
                except ValidationError as exc:
                    detail = "; ".join(
                        f"{k}: {' '.join(str(m) for m in v)}"
                        for k, v in exc.message_dict.items()
                    ) if hasattr(exc, "message_dict") else str(exc)
                    errors.append({
                        "rack_id": rack.pk,
                        "u_position": _norm_pos(u_position),
                        "device_id": None,
                        "detail": detail,
                    })
                    return None
                if new_add.power_config:
                    logger.debug(
                        "api._reconcile_item: placement=%s (%s) got power_config",
                        new_add.pk, new_add.proposed_name or new_add,
                    )
                if new_add.real_power_feed_id or new_add.planned_power_feed_id:
                    logger.debug(
                        "api._reconcile_item: placement=%s (%s) bound to feed real=%s planned=%s",
                        new_add.pk, new_add.proposed_name or new_add,
                        new_add.real_power_feed_id, new_add.planned_power_feed_id,
                    )
                desired_placement_ids.add(new_add.pk)
                return new_add
            if not placement_id:
                return None
            add = DesignPlacement.objects.filter(
                pk=placement_id,
                design=design,
                kind=DesignPlacementKindChoices.KIND_ADD,
            ).first()
            if add is None:
                return None
            # The user flagged this planned addition for cancellation via the
            # editor's × — delete the add placement. This is an EXPLICIT delete
            # (never by omission), so we drop it without adding it to
            # desired_placement_ids and return None.
            if item.get("cancel"):
                add.delete()
                self._made_db_change = True
                return None
            ok, device_role_id, tenant_id = self._resolve_add_refs(
                item, rack, u_position, errors
            )
            if not ok:
                return None
            rebinding = "real_power_feed_id" in item or "planned_power_feed_id" in item
            if rebinding:
                ok, real_feed_id, planned_feed_id = self._resolve_feed_binding(
                    item, rack, u_position, errors
                )
                if not ok:
                    return None
            before = self._snapshot(add)
            add.target_rack = rack
            add.target_position = u_position
            add.target_face = face
            # Only overwrite role/tenant/name when the editor actually sent them,
            # so a plain reposition that omits the keys preserves the existing
            # values (and stays idempotent).
            if "device_role_id" in item:
                add.device_role_id = device_role_id
            if "tenant_id" in item:
                add.tenant_id = tenant_id
            if "proposed_name" in item:
                add.proposed_name = item.get("proposed_name") or ""
            if "power_config" in item:
                add.power_config = item.get("power_config")
            if "power_source_device_id" in item:
                add.power_source_device_id = self._resolve_power_source_device(item)
            # Only overwrite the binding when the editor actually sent one of the
            # two keys, so a plain reposition that omits both preserves the
            # existing binding (and stays idempotent).
            if rebinding:
                add.real_power_feed_id = real_feed_id
                add.planned_power_feed_id = planned_feed_id
            # Idempotent: an unmoved add round-trips without a write.
            if self._snapshot(add) == before:
                desired_placement_ids.add(add.pk)
                return add
            add._projected_vacated_device_ids = getattr(
                self, "_batch_vacated_device_ids", None
            )
            try:
                add.full_clean()
                add.save()
                self._made_db_change = True
            except ValidationError as exc:
                detail = "; ".join(
                    f"{k}: {' '.join(str(m) for m in v)}"
                    for k, v in exc.message_dict.items()
                ) if hasattr(exc, "message_dict") else str(exc)
                errors.append({
                    "rack_id": rack.pk,
                    "u_position": _norm_pos(u_position),
                    "device_id": None,
                    "detail": detail,
                })
                return None
            if "power_config" in item and add.power_config:
                logger.debug(
                    "api._reconcile_item: placement=%s (%s) got power_config",
                    add.pk, add.proposed_name or add,
                )
            if rebinding and (add.real_power_feed_id or add.planned_power_feed_id):
                logger.debug(
                    "api._reconcile_item: placement=%s (%s) bound to feed real=%s planned=%s",
                    add.pk, add.proposed_name or add,
                    add.real_power_feed_id, add.planned_power_feed_id,
                )
            desired_placement_ids.add(add.pk)
            return add

        device = None
        if device_id:
            device = Device.objects.filter(pk=device_id).first()
            if device is None:
                errors.append({
                    "rack_id": rack.pk,
                    "u_position": _norm_pos(u_position),
                    "device_id": device_id,
                    "detail": "Device does not exist.",
                })
                return None

        # Locate an existing placement to reconcile against.
        existing = None
        if placement_id:
            existing = DesignPlacement.objects.filter(
                pk=placement_id, design=design
            ).first()
        if existing is None and device_id:
            existing = DesignPlacement.objects.filter(
                design=design,
                device_id=device_id,
                kind__in=(
                    DesignPlacementKindChoices.KIND_MOVE,
                    DesignPlacementKindChoices.KIND_REMOVE,
                ),
            ).first()

        # Snapshot the matched placement's persisted fields so we can detect a
        # genuine change after we mutate it (idempotency guard, below).
        before = self._snapshot(existing) if existing is not None else None

        if kind == "existing":
            # Device sits at its real position/face → no placement needed; clean
            # up any stale move/remove this design holds for it. A full-depth
            # device occupies both faces, so we ignore the face here — otherwise
            # its rear (or front) per-face copy would look "moved" and spawn a
            # spurious move placement on an untouched save.
            at_real = (
                device is not None
                and device.rack_id == rack.pk
                and _norm_pos(device.position) == _norm_pos(u_position)
                # A tray target (u_position None) carries no face -- the real
                # device's face (front/rear/blank) is irrelevant off-rack (§9.5).
                and (full_depth or u_position is None or (device.face or "") == face)
            )
            if at_real:
                if existing is not None:
                    existing.delete()
                    self._made_db_change = True
                return None
            # Moved within the editor without an explicit kind → treat as move.
            kind = "move"

        if kind == "remove":
            placement = existing or DesignPlacement(design=design)
            placement.kind = DesignPlacementKindChoices.KIND_REMOVE
            placement.device = device
            placement.device_type = None
            placement.target_rack = None
            placement.target_position = None
            placement.target_face = ""
        else:  # move
            placement = existing or DesignPlacement(design=design)
            placement.kind = DesignPlacementKindChoices.KIND_MOVE
            placement.device = device
            placement.device_type = None
            placement.target_rack = rack
            placement.target_position = u_position
            placement.target_face = face

        # Persist the editor-chosen proposed name when the editor sent one (the
        # §4a move dialog's keep-old / rename choice). Omitted => leave the
        # placement's existing name untouched, so an unrelated reposition that
        # never opened the dialog stays idempotent.
        if "proposed_name" in item:
            placement.proposed_name = item.get("proposed_name") or ""

        # Idempotency guard: if we matched an existing placement and none of its
        # meaningful fields changed, do NOT write (no full_clean/save) so an
        # untouched round-trip neither bumps last_updated nor reports a change.
        if before is not None and self._snapshot(placement) == before:
            desired_placement_ids.add(placement.pk)
            return placement

        # Validate the target slot against the design's PROJECTED layout: devices
        # this same submit moves/removes out of their real slots don't block a
        # device moving in (the swap / move-into-vacated case).
        placement._projected_vacated_device_ids = getattr(
            self, "_batch_vacated_device_ids", None
        )
        try:
            placement.full_clean()
            placement.save()
            self._made_db_change = True
        except ValidationError as exc:
            detail = "; ".join(
                f"{k}: {' '.join(str(m) for m in v)}"
                for k, v in exc.message_dict.items()
            ) if hasattr(exc, "message_dict") else str(exc)
            errors.append({
                "rack_id": rack.pk,
                "u_position": _norm_pos(u_position),
                "device_id": device_id,
                "detail": detail,
            })
            return None

        desired_placement_ids.add(placement.pk)
        return placement


class DesignPlacementViewSet(NetBoxModelViewSet):
    queryset = DesignPlacement.objects.all()
    serializer_class = DesignPlacementSerializer
    filterset_class = filtersets.DesignPlacementFilterSet


class FavoriteDeviceTypeViewSet(viewsets.ViewSet):
    """
    User-scoped "favorite device types" (the catalog palette's stars).

    This is deliberately NOT a NetBoxModelViewSet: a generic model viewset would
    expose every user's rows. Every query here is filtered by ``request.user``
    and the client NEVER supplies a user — a user can only ever read or change
    their own favorites.

    Endpoints:
      GET  /api/plugins/rack-design/favorite-device-types/        -> {"device_type_ids": [...]}
      POST /api/plugins/rack-design/favorite-device-types/toggle/ -> star/unstar
    """

    permission_classes = [IsAuthenticated]

    def list(self, request):
        """Return only the requesting user's favorite device-type ids."""
        ids = list(
            FavoriteDeviceType.objects.filter(user=request.user)
            .values_list("device_type_id", flat=True)
        )
        return Response({"device_type_ids": ids})

    @action(detail=False, methods=["post"], url_path="toggle")
    def toggle(self, request):
        """
        Star or unstar a device type for the requesting user (idempotent).

        Body: {"device_type_id": <id>}. Returns {"device_type_id": <id>,
        "favorite": true|false} where ``favorite`` reflects the resulting state.
        """
        body = FavoriteToggleSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        device_type_id = body.validated_data["device_type_id"]

        if not DeviceType.objects.filter(pk=device_type_id).exists():
            return Response(
                {"device_type_id": ["Device type does not exist."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        favorite, created = FavoriteDeviceType.objects.get_or_create(
            user=request.user, device_type_id=device_type_id
        )
        if created:
            return Response({"device_type_id": device_type_id, "favorite": True})

        # Already starred → toggle off.
        favorite.delete()
        return Response({"device_type_id": device_type_id, "favorite": False})


class DeviceTypePowerViewSet(viewsets.ViewSet):
    """
    Projected power draw for bare device TYPES -- feeds the catalog palette so a
    freshly dropped catalog device shows its draw LIVE (before Save + reload),
    matching the projection's per-slot draw exactly.

    The palette itself is populated from NetBox's core ``/api/dcim/device-types/``
    endpoint, which carries no computed power figure, so this small companion
    endpoint resolves the draw for a batch of type ids using the SAME logic the
    projection applies to a planned add (``device_type_power_summary``). It is
    read-only and performs no writes; unknown ids are simply omitted.

    Endpoint:
      GET /api/plugins/rack-design/device-type-power/?id=1&id=2...
        -> {"results": {"1": {"draw_w", "draw_known", "power_ports": [...]}, ...}}
    """

    permission_classes = [IsAuthenticated]

    def list(self, request):
        """Return per-id power summaries for the requested device-type ids."""
        ids = []
        for raw in request.query_params.getlist("id"):
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        results = {}
        if ids:
            types = DeviceType.objects.filter(pk__in=ids).prefetch_related(
                "powerporttemplates"
            )
            for dt in types:
                results[str(dt.pk)] = projection.device_type_power_summary(dt)
        return Response({"results": results})


class HiddenDesignRackViewSet(viewsets.ViewSet):
    """
    User-scoped per-design rack visibility for the multi-rack editor workspace.

    Like FavoriteDeviceTypeViewSet, this is deliberately NOT a NetBoxModelViewSet:
    every query is filtered by ``request.user`` and the client NEVER supplies a
    user. We store HIDDEN rows, so an empty set means "all visible". Hiding a rack
    is purely personal view state -- it never affects another user and never
    changes the design's data or its ``racks`` scope.

    Endpoints:
      GET  /api/plugins/rack-design/hidden-design-racks/?design_id=<id>
           -> {"design_id": <id>, "hidden_rack_ids": [...]}
      POST /api/plugins/rack-design/hidden-design-racks/toggle/
           body {"design_id", "rack_id"} -> hide/show one rack
      POST /api/plugins/rack-design/hidden-design-racks/show-all/
           body {"design_id"} -> clear all hidden rows for the design
    """

    permission_classes = [IsAuthenticated]

    def _hidden_ids(self, user, design_id):
        return list(
            HiddenDesignRack.objects.filter(user=user, design_id=design_id)
            .values_list("rack_id", flat=True)
        )

    def list(self, request):
        """Return the requesting user's hidden rack ids for ?design_id=<id>."""
        design_id = request.query_params.get("design_id")
        if not design_id:
            return Response(
                {"design_id": ["This query parameter is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({
            "design_id": int(design_id),
            "hidden_rack_ids": self._hidden_ids(request.user, design_id),
        })

    @action(detail=False, methods=["post"], url_path="toggle")
    def toggle(self, request):
        """
        Hide or show one (design, rack) for the requesting user (idempotent).

        Returns {"design_id", "rack_id", "hidden": true|false, "hidden_rack_ids":
        [...]} where ``hidden`` reflects the resulting state and
        ``hidden_rack_ids`` is the user's full hidden set for the design.
        """
        body = HiddenRackToggleSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        design_id = body.validated_data["design_id"]
        rack_id = body.validated_data["rack_id"]

        if not Design.objects.filter(pk=design_id).exists():
            return Response(
                {"design_id": ["Design does not exist."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not Rack.objects.filter(pk=rack_id).exists():
            return Response(
                {"rack_id": ["Rack does not exist."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        hidden, created = HiddenDesignRack.objects.get_or_create(
            user=request.user, design_id=design_id, rack_id=rack_id
        )
        if created:
            resulting = True
        else:
            # Already hidden → show it again.
            hidden.delete()
            resulting = False

        return Response({
            "design_id": design_id,
            "rack_id": rack_id,
            "hidden": resulting,
            "hidden_rack_ids": self._hidden_ids(request.user, design_id),
        })

    @action(detail=False, methods=["post"], url_path="show-all")
    def show_all(self, request):
        """Clear ALL of the user's hidden rows for a design (show every rack)."""
        body = HiddenRackShowAllSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        design_id = body.validated_data["design_id"]

        HiddenDesignRack.objects.filter(
            user=request.user, design_id=design_id
        ).delete()
        return Response({"design_id": design_id, "hidden_rack_ids": []})
