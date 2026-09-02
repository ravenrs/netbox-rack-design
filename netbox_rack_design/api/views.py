"""REST API viewsets for NetBox Rack Design."""

import logging

from dcim.models import Device, DeviceBay, DeviceRole, DeviceType, PowerFeed, Rack
from django.core.exceptions import ValidationError
from django.db import transaction
from netbox.api.authentication import TokenPermissions
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status, views, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from tenancy.models import Tenant

from .. import filtersets, naming, planning_fields, projection
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import (
    Design,
    DesignGroup,
    DesignPlacement,
    DesignPowerFeed,
    DesignRackPower,
    FavoriteDeviceType,
    FavoriteSet,
    HiddenDesignChassis,
    HiddenDesignRack,
)
from .serializers import (
    CopyFeedsSerializer,
    DesignGroupSerializer,
    DesignPlacementSerializer,
    DesignPowerFeedSerializer,
    DesignRackScopeSerializer,
    DesignRebaseSerializer,
    DesignSerializer,
    FavoriteSetWriteSerializer,
    FavoriteToggleSerializer,
    HiddenChassisToggleSerializer,
    HiddenRackShowAllSerializer,
    HiddenRackToggleSerializer,
    NestedDesignSerializer,
    PlannedFeedDeleteSerializer,
    PlannedFeedSerializer,
    PlannedFeedUpsertSerializer,
    PreviewNameSerializer,
    RackPowerSerializer,
    RecomputeDistributionSerializer,
    SaveLayoutSerializer,
)

logger = logging.getLogger("netbox_rack_design.api")

__all__ = (
    "DesignGroupViewSet",
    "DesignViewSet",
    "DesignPlacementViewSet",
    "DesignPowerFeedViewSet",
    "FavoriteDeviceTypeViewSet",
    "FavoriteSetViewSet",
    "HiddenDesignRackViewSet",
    "DeviceTypePowerViewSet",
    "PlacementFieldsView",
)


class DesignGroupViewSet(NetBoxModelViewSet):
    queryset = DesignGroup.objects.select_related("parent").prefetch_related("tags")
    serializer_class = DesignGroupSerializer
    filterset_class = filtersets.DesignGroupFilterSet


def _norm_pos(value):
    """Normalise a U position to a float for comparison, or None."""
    return None if value is None else float(value)


class _RackSlotTarget:
    """A slot on a rack face: the classic target (spec §2 RackFaceContainer)."""

    is_bay = False

    def __init__(self, rack, u_position, face):
        self.rack = rack
        self.u_position = u_position
        self.face = face
        self.fields = {
            "target_rack": rack,
            "target_position": u_position,
            "target_face": face,
            # A rack slot is not a bay: clear any bay target a matched placement
            # still carries, or the row claims both and fails validation.
            "target_bay": None,
            "parent_placement": None,
            "target_bay_name": "",
        }

    def at_rest(self, device, full_depth=False):
        """The device already physically sits exactly here."""
        if device is None or device.rack_id != self.rack.pk:
            return False
        if _norm_pos(device.position) != _norm_pos(self.u_position):
            return False
        # A tray target (u_position None) carries no face, and a full-depth
        # device occupies both -- in neither case does the face decide.
        if full_depth or self.u_position is None:
            return True
        return (device.face or "") == self.face


class _BayTarget:
    """A device bay: the chassis target (spec §2 ChassisContainer).

    A child device may carry neither a rack position nor a face (core forbids
    both), so the target is a real ``dcim.DeviceBay`` or -- when the chassis is
    itself planned -- the chassis's own placement.
    """

    is_bay = True

    def __init__(self, rack, target_bay, parent_placement, bay_name):
        self.rack = rack
        self.target_bay = target_bay
        self.parent_placement = parent_placement
        self.u_position = None
        self.face = ""
        self.fields = {
            "target_rack": rack,
            "target_position": None,
            "target_face": "",
            "target_bay": target_bay,
            "parent_placement": parent_placement,
            "target_bay_name": bay_name,
        }

    def at_rest(self, device, full_depth=False):
        if device is None:
            return False
        if self.target_bay is None:
            # A planned chassis has no real bays yet, so nothing can be at rest
            # in one.
            return False
        from dcim.models import DeviceBay

        real_bay_id = DeviceBay.objects.filter(
            installed_device_id=device.pk
        ).values_list("pk", flat=True).first()
        return real_bay_id == self.target_bay.pk


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


def _retarget_feed_name(name, source_rack_name, target_rack_name):
    """Rename a copied feed for the rack it lands on.

    Feeds are conventionally named after their rack (``R101-A``/``R101-B``), so a
    copy onto R103 should read ``R103-A``/``R103-B`` rather than carry the source
    rack's name. Only a case-insensitive rack-name PREFIX is substituted; any
    other naming scheme is left verbatim (a feed called "Utility A" stays
    "Utility A"), and the result is clipped to DesignPowerFeed.name's max_length.
    """
    name = name or ""
    src = source_rack_name or ""
    if src and name.lower().startswith(src.lower()):
        name = (target_rack_name or "") + name[len(src):]
    return name[:100]


def _frozen_design_rest_message(design):
    """
    The message body for a REST 409 against a FROZEN design
    (PLAN-design-chains.md §2.2/G4): an approved design's placements, planned
    power feeds and rack power are read-only, because approval is what makes
    the design derivable and every downstream chain must be able to trust
    that a frozen layer stops moving. Mirrors what ``DesignPlacement.clean()``
    / ``DesignPowerFeed.clean()`` raise for the same reason (models.py), so a
    user sees one consistent explanation regardless of which write path
    caught it. Split out from ``_reject_frozen_design`` below so a caller that
    can't use a ``Response`` directly (``perform_destroy``, whose return
    value is discarded) can still raise the SAME wording instead of
    duplicating it by hand.
    """
    return (
        f"{design} is approved, and approved designs are frozen: its "
        "placements, planned power feeds and rack power cannot be "
        "changed. Set the design back to draft, or create a new "
        "version of it, to make this change."
    )


def _reject_frozen_design(design):
    """
    A 409 ``Response`` for a write against a FROZEN design. Callers invoke
    this BEFORE any reconciliation/mutation work starts, so a submit against
    a frozen design fails fast rather than tripping ``DesignPlacement.clean()``
    deep inside a loop.
    """
    return Response(
        {"detail": _frozen_design_rest_message(design)},
        status=status.HTTP_409_CONFLICT,
    )


class ChangeDesignPermissions(TokenPermissions):
    """
    These detail @actions (save-layout, add-rack, remove-rack) are writes that
    EDIT an existing Design (not creation), so a POST to them must require
    ``change_design`` rather than the default ``add_design`` that
    TokenPermissions maps POST to.

    DELETE is remapped for the same reason: deleting a planned feed edits the
    design, it does not delete the design, so requiring ``delete_design`` would
    demand a permission far broader than the act.
    """

    perms_map = {
        **TokenPermissions.perms_map,
        "POST": ["%(app_label)s.change_%(model_name)s"],
        "DELETE": ["%(app_label)s.change_%(model_name)s"],
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
    queryset = Design.objects.select_related(
        "site", "group", "root", "based_on"
    ).prefetch_related("placements", "depends_on", "racks", "tags")
    serializer_class = DesignSerializer
    filterset_class = filtersets.DesignFilterSet

    def get_permissions(self):
        action = getattr(self, "action", None)
        if action in ("save_layout", "add_rack", "remove_rack", "rack_power",
                      "planned_feed", "copy_feeds", "rebase"):
            # ``rebase`` re-points THIS design's own ``based_on`` -- an edit
            # to an existing Design, not a create -- so it needs
            # ``change_design`` rather than the ``add_design`` TokenPermissions
            # maps POST to by default (PLAN-design-chains.md §5/G9). ``derive``
            # is the opposite case -- it CREATES a new Design -- so it is
            # deliberately left out of this list: the default POST ->
            # ``add_design`` mapping is already exactly the rule §5 wants.
            return [ChangeDesignPermissions()]
        if action in ("preview_name", "power_source", "feeds", "recompute_distribution",
                      "chain"):
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
        exists = naming.name_exists_in_site(
            name, design.site, exclude_placement=None, design=design
        )
        return Response(
            {"name": name, "exists_in_site": exists}, status=status.HTTP_200_OK
        )

    @action(detail=True, methods=["post"], url_path="recompute-distribution")
    def recompute_distribution(self, request, pk=None):
        """
        Recompute per-rack power distribution for a PROSPECTIVE (unsaved) editor
        layout, WITHOUT persisting anything.

        The per-bank distribution is produced by the server-side distribution
        engine (builtin or a custom ``distribution_script``) reading the design's
        placements + real cabling -- the SAME computation that runs on save. To
        make the editor's per-bank chips refresh LIVE (like the always-live total
        power bar) rather than only on Save, this action applies the posted live
        layout through the exact ``save-layout`` reconciliation inside a
        transaction, projects each submitted rack, and then ROLLS THE TRANSACTION
        BACK -- so the engine sees the live edits but nothing is written.

        Body: the ``save-layout`` body plus an optional ``project_racks``
        (:class:`RecomputeDistributionSerializer`), so the editor reuses its
        existing per-rack save payload.

        Every submitted rack is RECONCILED, because a device that left rack A is
        described by a placement filed under whichever rack it landed in --
        project A without applying B's items and the device still looks present
        in A. Only the racks in ``project_racks`` are PROJECTED, which is the
        expensive half (the distribution engine runs once per rack, plus its PDU
        and per-device power queries). An empty or absent ``project_racks``
        projects everything, so a full refresh and an older editor both still
        work. Racks left unprojected are simply absent from the response, and
        the editor keeps their last-known numbers.

        Returns ``{"distributions": {"<rack_id>": <distribution-json-or-null>},
        "distribution_status": {"<rack_id>": {"state","engine","script","detail"}},
        "power": {"<rack_id>": <rack power summary without "distribution">}}``.
        ``distribution_status`` says WHY a rack has no distribution (engine off,
        the script raised, no usable PDU), so a live edit that breaks the engine
        reports itself instead of silently emptying the chip strip.
        The ``power`` block keeps the rack BAR live as well: its capacity comes
        from the rack's feeds (planned ones included) via maths the browser must
        not duplicate, so without it the denominator would stay at whatever the
        page was rendered with until the next Save.
        Performs NO writes (read-only preview; requires only ``view`` on the
        design).

        URL name: plugins-api:netbox_rack_design-api:design-recompute-distribution
        Path:     /api/plugins/rack-design/designs/<pk>/recompute-distribution/
        """
        # Read-only preview: scope to designs this user may view.
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "view")
        design = self.get_object()

        body = RecomputeDistributionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        # Which racks to project. Empty means "all of them".
        project_only = set(data.get("project_racks") or [])

        # State the reconciliation helpers rely on (mirrors save_layout). We never
        # inspect the resulting ids/flags here -- the whole transaction is rolled
        # back below -- but _reconcile_item reads _batch_vacated_device_ids so its
        # collision view matches the projected layout.
        errors = []
        desired_placement_ids = set()
        self._made_db_change = False
        self._batch_vacated_device_ids = self._compute_vacated_device_ids(data)

        distributions = {}
        dist_status = {}
        powers = {}
        with transaction.atomic():
            for rack_data in data["racks"]:
                rack_id = rack_data["rack_id"]
                rack = Rack.objects.filter(pk=rack_id).first()
                if rack is None:
                    distributions[str(rack_id)] = None
                    dist_status[str(rack_id)] = None
                    powers[str(rack_id)] = None
                    continue
                items = []
                for face_key in ("front", "rear", "other"):
                    for item in rack_data.get(face_key, []):
                        items.append((face_key, item))
                for face_key, item in items:
                    # A single mid-edit item that fails to reconcile (transient
                    # collision, etc.) must not poison the whole recompute: apply
                    # each in a savepoint and skip on failure. Collisions are only
                    # appended to ``errors`` (never raised) and are ignored here --
                    # this is a preview, not a save.
                    try:
                        with transaction.atomic():
                            self._reconcile_item(
                                design, rack, face_key, item, errors,
                                desired_placement_ids,
                            )
                    except Exception:  # noqa: BLE001 - preview must never 500
                        logger.debug(
                            "recompute_distribution: item skipped rack=%s item=%r",
                            rack_id, item, exc_info=True,
                        )

            # Project each submitted rack against the transient (uncommitted)
            # layout. project_rack re-queries design.placements, so it sees the
            # rows just reconciled inside this transaction.
            for rack_data in data["racks"]:
                rack_id = rack_data["rack_id"]
                if str(rack_id) in distributions:
                    continue  # rack did not exist
                if project_only and rack_id not in project_only:
                    # Not asked for: reconciled above (so the racks that WERE
                    # asked for see a complete layout), but not projected. The
                    # caller keeps whatever numbers it already had for it.
                    continue
                rack = Rack.objects.get(pk=rack_id)
                elevation = projection.project_rack(design, rack)
                distributions[str(rack_id)] = elevation.power.get("distribution")
                dist_status[str(rack_id)] = elevation.power.get("distribution_status")
                # The rack-level summary rides along so the editor's power BAR is
                # live too, not just the per-bank chips. Capacity is the reason:
                # it comes from the rack's feeds -- including PLANNED ones -- and
                # a browser must not re-derive the derating/phase maths, so the
                # bar's denominator would otherwise stay frozen at whatever the
                # page was rendered with until the next Save (user 2026-08-20).
                power = {k: v for k, v in elevation.power.items()
                         if k not in ("distribution", "distribution_status")}
                powers[str(rack_id)] = power

            # Read-only: discard every transient placement. Must be the LAST DB
            # action in this atomic block (no ORM use is allowed after it).
            transaction.set_rollback(True)

        return Response(
            {"distributions": distributions, "distribution_status": dist_status,
             "power": powers},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="add-rack")
    def add_rack(self, request, pk=None):
        """
        Add a rack to this design's planning scope (the ``design.racks`` M2M).

        Enforces the same-site rule (a rack from another site is rejected),
        mirroring ``Design.clean()`` / the design form. Refuses with a 409 on a
        FROZEN (approved) design (PLAN-design-chains.md §2.2/G4): the rack
        scope is part of what was approved, so widening it after the fact
        would silently change what the approved plan means. Respects NetBox
        object permissions for editing the Design. Idempotent: re-adding a
        rack already in scope is a no-op. Returns the updated rack scope
        (``rack_ids``).

        URL name: plugins-api:netbox_rack_design-api:design-add-rack
        Path:     /api/plugins/rack-design/designs/<pk>/add-rack/
        """
        # Restrict to designs this user may change (object-level permission).
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "change")
        design = self.get_object()

        # A design's rack scope is part of what was approved (PLAN-design-
        # chains.md §2.2/G4): silently widening it would change what the
        # approved plan means, exactly like narrowing it via remove-rack
        # already refuses. Checked before anything else is touched.
        if design.is_frozen:
            return _reject_frozen_design(design)

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

        # Removing a rack from scope DELETES the placements targeting it
        # (below), so a frozen design must reject this before anything is
        # touched (PLAN-design-chains.md §2.2/G4).
        if design.is_frozen:
            return _reject_frozen_design(design)

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
            if design.is_frozen:
                return _reject_frozen_design(design)

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
        a source rack's power planning inputs (``kind=rack``) -- its custom
        fields AND its power feeds, which is everything a greenfield rack needs
        to inherit from a provisioned sibling. Performs NO writes (the copy
        itself is POST ``copy-feeds/`` + POST ``rack-power/``).

        In the universal feed-binding design a planned PDU **binds to a feed**
        (see the ``feeds``/``planned-feed`` actions) rather than copying another
        PDU's electricals, so there is no ``kind=pdu`` here -- the rack copy path
        is the only remaining copy-from-rack flow.

        GET .../designs/<pk>/power-source/?rack_id=<id>&kind=rack
          kind=rack -> {"custom_fields": {...},
                        "feeds": [{"id","name","voltage","amperage","phase",
                                   "supply","source"}, ...]}
          ``feeds`` are the source rack's REAL feeds, or -- when it has none --
          this design's planned feeds for it, so a rack planned earlier in the
          same design can be cloned too.

        URL name: plugins-api:netbox_rack_design-api:design-power-source
        Path:     /api/plugins/rack-design/designs/<pk>/power-source/
        """
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "view")
        # Enforces design-level view permission/object scoping for this lookup;
        # the design is also needed to report ITS planned feeds for the source
        # rack when that rack has no real ones.
        design = self.get_object()

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

        feeds = [
            _feed_dict(feed, "real")
            for feed in PowerFeed.objects.filter(rack=rack).order_by("name")
        ]
        if not feeds:
            feeds = [
                _feed_dict(feed, "planned")
                for feed in DesignPowerFeed.objects.filter(
                    design=design, rack=rack).order_by("name")
            ]
        logger.debug(
            "api.power_source: kind=rack rack_id=%s feeds=%d", rack_id, len(feeds))
        return Response(
            {"custom_fields": dict(rack.cf), "feeds": feeds},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="copy-feeds")
    def copy_feeds(self, request, pk=None):
        """
        Clone a source rack's feeds onto a target rack as PLANNED feeds -- the
        "copy from rack" half of the rack power dialog that carries the supply
        itself, not just the custom fields (docs/pdu-distribution-spec.md §6.3).

        A greenfield rack in a row is normally fed exactly like its provisioned
        siblings, so one copy gives it the same electricals instead of retyping
        each feed. The source's REAL feeds are copied when it has any, otherwise
        this design's planned feeds for it (so a rack planned earlier in the same
        design can be cloned too).

        Each copied feed is named for the TARGET rack when the source name is
        prefixed with the source rack's name (``R101-A`` copied from R101 to R103
        becomes ``R103-A``); any other name is kept verbatim.

        REPLACES the target's planned feeds rather than adding to them (user
        2026-08-28): the button says "copy from rack", so the target must end up
        fed like the source -- no more, no less. Copying from three racks in turn
        used to leave the union of all three whenever their feeds were named by
        some scheme other than the rack-name prefix (``Utility A`` is kept
        verbatim, so nothing collided), and the rack's capacity read as the SUM
        of every source ever clicked. Feeds whose name survives the copy keep
        their row -- and therefore every PDU bound to them; the rest are deleted,
        which unbinds their PDUs (``planned_power_feed`` is SET_NULL), so the
        count of those is reported for the UI to warn about.

        Writes ONLY DesignPowerFeed rows -- never dcim, and never the target rack.

        POST .../designs/<pk>/copy-feeds/
          body {"rack_id": <target>, "source_rack_id": <source>}
          -> {"feeds": [{"id","name","voltage","amperage","phase","supply"}, ...],
              "created": <n>, "updated": <n>, "deleted": <n>, "unbound": <n>}

        URL name: plugins-api:netbox_rack_design-api:design-copy-feeds
        Path:     /api/plugins/rack-design/designs/<pk>/copy-feeds/
        """
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "change")
        design = self.get_object()
        if design.is_frozen:
            return _reject_frozen_design(design)

        body = CopyFeedsSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        target = Rack.objects.filter(pk=data["rack_id"]).first()
        if target is None:
            return Response({"rack_id": ["Rack does not exist."]},
                            status=status.HTTP_400_BAD_REQUEST)
        # Same-site rule, mirroring add-rack / rack-power / planned-feed.
        if target.site_id != design.site_id:
            return Response({"rack_id": ["This rack is not in the design's site."]},
                            status=status.HTTP_400_BAD_REQUEST)
        source = Rack.objects.filter(pk=data["source_rack_id"]).first()
        if source is None:
            return Response({"source_rack_id": ["Rack does not exist."]},
                            status=status.HTTP_400_BAD_REQUEST)
        if source.pk == target.pk:
            return Response(
                {"source_rack_id": ["Source and target rack must differ."]},
                status=status.HTTP_400_BAD_REQUEST)

        sources = list(PowerFeed.objects.filter(rack=source).order_by("name"))
        if not sources:
            sources = list(DesignPowerFeed.objects.filter(
                design=design, rack=source).order_by("name"))

        copied, created_count, updated_count = [], 0, 0
        with transaction.atomic():
            for feed in sources:
                name = _retarget_feed_name(feed.name, source.name, target.name)
                electricals = {
                    "voltage": feed.voltage,
                    "amperage": feed.amperage,
                    "phase": getattr(feed.phase, "value", feed.phase),
                    "supply": getattr(feed.supply, "value", feed.supply),
                }
                planned, created = DesignPowerFeed.objects.get_or_create(
                    design=design, rack=target, name=name, defaults=electricals)
                if created:
                    created_count += 1
                else:
                    for field_name, value in electricals.items():
                        setattr(planned, field_name, value)
                    planned.save()
                    updated_count += 1
                copied.append(planned)

            # The replace half: whatever this rack was planned to have before and
            # the source does not supply is gone. Counted BEFORE the delete, so
            # the dialog can say how many PDUs it just unbound.
            #
            # A source with NO feeds stays a no-op rather than a wipe: "copy from
            # a rack that has nothing" is far more likely a mis-click on the rack
            # picker than an instruction to strip this rack of its supply, and
            # the destructive reading has no undo.
            stale = DesignPowerFeed.objects.filter(
                design=design, rack=target
            ).exclude(pk__in=[f.pk for f in copied]) if copied else (
                DesignPowerFeed.objects.none())
            unbound = DesignPlacement.objects.filter(
                design=design, planned_power_feed__in=stale
            ).count()
            deleted_count = stale.count()
            stale.delete()

        logger.debug(
            "api.copy_feeds: design=%s %s -> %s created=%d updated=%d deleted=%d "
            "unbound=%d",
            design.pk, source.name, target.name, created_count, updated_count,
            deleted_count, unbound)
        return Response(
            {
                "feeds": PlannedFeedSerializer(copied, many=True).data,
                "created": created_count,
                "updated": updated_count,
                "deleted": deleted_count,
                "unbound": unbound,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="feeds")
    def feeds(self, request, pk=None):
        """
        Read-only lookup for the bind-to-feed picker (docs/pdu-distribution-
        spec.md §6.3/§8): this rack's real ``dcim.PowerFeed``s plus this
        design's OWN planned ``DesignPowerFeed``s plus -- G5, "a child's PDU
        may bind an ancestor's planned feed" -- every APPROVED ancestor's
        planned feeds for this rack, each in the uniform feed shape
        (``_feed_dict``) so the picker can list real feeds first, then
        planned. Performs NO writes.

        The ancestor chain is resolved through
        ``projection.resolve_baseline_chain`` -- the same §9.2 all-or-nothing
        answer the rack projection itself uses -- so this picker can never
        offer a feed the rack's own render disagrees with. A refused chain
        (a non-approved or ``implemented`` ancestor) contributes NOTHING;
        the design's own feeds are unaffected by a refusal.

        An inherited entry carries two extra keys beyond the base
        ``_feed_dict`` shape so two identically-named feeds from different
        designs stay distinguishable: ``"inherited": True``, ``"design_id"``
        and ``"design_name"`` naming the ancestor that owns it. The design's
        OWN feeds keep the exact original shape (no extra keys) -- for an
        unchained design this makes the response byte-for-byte unchanged.

        Ordering is deterministic: the design's own feeds first (model
        default ordering, i.e. by name), then each ancestor's feeds in
        oldest-ancestor-first chain order (by name within an ancestor) --
        never a set/dict merge that could reshuffle between requests.

        GET .../designs/<pk>/feeds/?rack_id=<id>
          -> {"real": [{"id","name","voltage","amperage","phase","supply",
                        "source":"real"}, ...],
              "planned": [{..., "source":"planned"},
                          {..., "source":"planned", "inherited": True,
                           "design_id":<id>, "design_name":<str>}, ...]}

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
        chain, _refusal = projection.resolve_baseline_chain(design)
        for ancestor in chain:
            for f in DesignPowerFeed.objects.filter(
                design=ancestor, rack_id=rack_id
            ):
                entry = _feed_dict(f, "planned")
                entry["inherited"] = True
                entry["design_id"] = ancestor.pk
                entry["design_name"] = str(ancestor)
                planned_feeds.append(entry)
        logger.debug(
            "api.feeds: design=%s rack_id=%s real=%d planned=%d",
            design.pk, rack_id, len(real_feeds), len(planned_feeds),
        )
        return Response(
            {"real": real_feeds, "planned": planned_feeds}, status=status.HTTP_200_OK
        )

    @action(detail=True, methods=["get", "post", "delete"], url_path="planned-feed")
    def planned_feed(self, request, pk=None):
        """
        Upsert/list this design's planned power feeds (``DesignPowerFeed`` --
        docs/pdu-distribution-spec.md §6.1/§8), for the greenfield "define
        planned feed" dialog flow. Rack-scoped and design-scoped; upserts by
        the ``(design, rack, name)`` unique_together so re-submitting the same
        name UPDATES the electricals rather than duplicating the row. Never
        writes to dcim.

        GET    .../designs/<pk>/planned-feed/?rack_id=<id>
               -> [{"id","name","voltage","amperage","phase","supply"}, ...]
        POST   .../designs/<pk>/planned-feed/ body {"rack_id","name","voltage",
               "amperage","phase","supply"} -> upsert, returns the one feed.
        DELETE .../designs/<pk>/planned-feed/ body {"feed_id"} (or
               {"rack_id","name"}) -> {"deleted","unbound"}. A planned feed had
               no way out of the UI at all, so a mistyped or no-longer-wanted one
               was unremovable short of deleting the design (user 2026-08-28).
               Deleting one unbinds the PDUs that drew from it
               (``planned_power_feed`` is SET_NULL) -- that count comes back so
               the dialog can say so.

        URL name: plugins-api:netbox_rack_design-api:design-planned-feed
        Path:     /api/plugins/rack-design/designs/<pk>/planned-feed/
        """
        if request.user.is_authenticated:
            perm = "view" if request.method == "GET" else "change"
            self.queryset = Design.objects.restrict(request.user, perm)
        design = self.get_object()

        if request.method in ("POST", "DELETE") and design.is_frozen:
            return _reject_frozen_design(design)

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

        if request.method == "DELETE":
            body = PlannedFeedDeleteSerializer(data=request.data)
            body.is_valid(raise_exception=True)
            data = body.validated_data
            feeds_qs = DesignPowerFeed.objects.filter(design=design)
            if data.get("feed_id") is not None:
                feeds_qs = feeds_qs.filter(pk=data["feed_id"])
            else:
                feeds_qs = feeds_qs.filter(
                    rack_id=data["rack_id"], name=data["name"])
            if not feeds_qs.exists():
                return Response(
                    {"detail": "No such planned feed in this design."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            with transaction.atomic():
                unbound = DesignPlacement.objects.filter(
                    design=design, planned_power_feed__in=feeds_qs
                ).count()
                deleted_count = feeds_qs.count()
                feeds_qs.delete()
            logger.debug(
                "api.planned_feed: design=%s deleted=%d unbound=%d",
                design.pk, deleted_count, unbound)
            return Response(
                {"deleted": deleted_count, "unbound": unbound},
                status=status.HTTP_200_OK,
            )

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

        # Frozen check BEFORE any reconciliation starts (PLAN-design-chains.md
        # §2.2/G4): an approved design's placements are read-only, and the
        # editor's bulk save path must fail fast rather than tripping this
        # deep inside the reconciliation loop.
        if design.is_frozen:
            return _reject_frozen_design(design)

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
                    items = self._frees_first(items)

                    # ref -> placement, so a blade in the "bays" bucket below can
                    # point at a chassis this same submit is creating (the chassis
                    # has no placement_id until it is saved).
                    ref_map = {}
                    for face_key, item in items:
                        device_id = item.get("device_id")
                        if device_id:
                            submitted_device_ids.add(device_id)
                        placement = self._reconcile_item(
                            design, rack, face_key, item, errors, desired_placement_ids
                        )
                        ref = item.get("ref")
                        if ref and placement is not None:
                            ref_map[ref] = placement

                    # Bay items last: they may depend on a chassis created just
                    # above. Same method as every other item -- only the TARGET
                    # differs, and _resolve_target owns that difference.
                    for _, item in self._frees_first(
                        [("bays", it) for it in rack_data.get("bays", [])]
                    ):
                        device_id = item.get("device_id")
                        if device_id:
                            submitted_device_ids.add(device_id)
                        self._reconcile_item(
                            design, rack, "bays", item, errors,
                            desired_placement_ids, ref_map=ref_map,
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
        """A comparable tuple of a placement's meaningful (mutable) fields.

        The BAY target belongs here as much as the rack one. Without it, a blade
        moved from bay A to bay B of the same chassis snapshots identically --
        same kind, device, rack, target_position None, target_face "" -- so the
        idempotency guard would read a real move as a no-op, skip the write and
        leave the placement pointing at the old bay.
        """
        return (
            placement.kind,
            placement.device_id,
            placement.device_type_id,
            placement.target_rack_id,
            _norm_pos(placement.target_position),
            placement.target_face or "",
            placement.target_bay_id,
            placement.parent_placement_id,
            placement.target_bay_name or "",
            placement.device_role_id,
            placement.tenant_id,
            placement.proposed_name or "",
            placement.power_config,
            placement.planning_data,
            placement.real_power_feed_id,
            placement.planned_power_feed_id,
            placement.power_source_device_id,
        )

    @staticmethod
    @staticmethod
    def _frees_first(items):
        """Order a rack's items so the ones that FREE a slot are written first.

        A cancel deletes its placement; every other kind writes one. The editor
        replays cancelled adds at the END of their bucket (their tiles are gone
        from the grid, so they are appended from a capture), which means a bay
        re-filled in the same submit was written while the cancelled placement
        still held it -- and a device bay is the one target with a UNIQUE
        constraint per design, so the save died on
        ``unique_design_target_bay`` / ``unique_design_planned_bay`` instead of
        simply replacing the occupant.

        Ordering here rather than in the editor on purpose: the constraint is
        the server's, the payload order is a client detail no contract promises,
        and this way ANY client that cancels and re-fills in one submit is
        correct. Stable, so items that neither free nor compete keep the order
        they arrived in.
        """
        return sorted(items, key=lambda pair: 0 if pair[1].get("cancel") else 1)

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
        # (device_id -> bay pk) the payload asserts as "existing" in a bay.
        existing_bays = {}
        for rack_data in data["racks"]:
            # "bays" belongs here as much as the rack buckets: a blade the submit
            # removes has vacated its bay, so another blade may take it. Leaving
            # it out meant a bay freed in the SAME save still read as occupied and
            # the replacement was rejected -- and because this injected set wins
            # over the model's own fallback, an ALREADY SAVED removal was ignored
            # too, so dropping into a freed bay never worked at all
            # (user 2026-08-26).
            for face_key in ("front", "rear", "other", "bays"):
                for item in rack_data.get(face_key, []):
                    device_id = item.get("device_id")
                    if not device_id:
                        continue
                    kind = item.get("kind")
                    if kind in ("move", "remove"):
                        candidate_ids.add(device_id)
                    elif kind == "existing" and face_key == "bays":
                        if item.get("target_bay_id"):
                            existing_bays[device_id] = item["target_bay_id"]
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
        # The bay equivalent: a blade the payload asserts in a bay other than the
        # one it physically sits in has vacated that one.
        if existing_bays:
            real_bay_by_device = dict(
                DeviceBay.objects.filter(installed_device_id__in=existing_bays)
                .values_list("installed_device_id", "pk")
            )
            for device_id, target_bay_id in existing_bays.items():
                if real_bay_by_device.get(device_id) != target_bay_id:
                    candidate_ids.add(device_id)
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
        Validate the optional device_role_id / tenant_id on an item.

        Used by an 'add' (the values the planned device is created with) and by
        a 'move' (planned overrides on an existing device).

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

    def _resolve_target(self, design, rack, face_key, item, ref_map, errors):
        """
        Where one item's placement POINTS -- the only thing that differs between
        a rack slot and a device bay (spec §2: a Frame's containers).

        Returns a target descriptor, or None with the error already appended:

            fields      the model attributes to write onto the placement
            at_rest(d)  True when device ``d`` already sits exactly here, so the
                        design needs no placement for it at all

        Everything else about reconciling an item -- add / move / remove /
        existing / cancel, validation, the idempotency guard -- is shared, and
        must stay that way: the two used to be separate methods and every rack
        fix had to be re-proved against bays by hand.
        """
        def fail(detail, u_position=None):
            errors.append({
                "rack_id": rack.pk,
                "u_position": u_position,
                "device_id": item.get("device_id"),
                "detail": detail,
            })

        if face_key != "bays":
            u_position = None if face_key == "other" else item.get("u_position")
            face = "" if face_key == "other" else (item.get("face") or "")
            return _RackSlotTarget(rack, u_position, face)

        # ---- a bay: a real dcim.DeviceBay, or a chassis planned in THIS submit
        bay_id = item.get("target_bay_id")
        parent_ref = item.get("parent_ref")
        bay_name = item.get("target_bay_name") or ""
        target_bay = None
        parent_placement = None

        if bay_id:
            target_bay = DeviceBay.objects.filter(pk=bay_id).select_related("device").first()
            if target_bay is None:
                fail("Device bay does not exist.")
                return None
            if not bay_name:
                bay_name = target_bay.name
        elif parent_ref:
            parent_placement = (ref_map or {}).get(parent_ref)
            if parent_placement is None:
                fail(f"Unknown parent reference {parent_ref!r} for a bay placement.")
                return None
        elif item.get("parent_placement_id"):
            parent_placement = DesignPlacement.objects.filter(
                pk=item["parent_placement_id"], design=design
            ).first()
            if parent_placement is None:
                fail("Parent chassis placement does not exist in this design.")
                return None
        elif item.get("cancel") or item.get("kind") == DesignPlacementKindChoices.KIND_REMOVE:
            # Neither needs a target. A cancel deletes by placement_id; a removal
            # addresses the DEVICE, and the model takes no target for one at all
            # -- it rides the bays bucket only because that is where the chassis
            # layer emits it from.
            return _BayTarget(rack, None, None, bay_name)
        else:
            fail("A bay item requires a target_bay_id, a parent_ref, or a "
                 "parent_placement_id.")
            return None

        return _BayTarget(rack, target_bay, parent_placement, bay_name)

    def _reconcile_item(self, design, rack, face_key, item, errors,
                        desired_placement_ids, ref_map=None):
        """
        Map one desired item to its DesignPlacement (or no placement), upserting
        and full_clean()-validating as needed. Appends to ``errors`` on failure
        and returns the placement (or None when no placement is needed).

        ONE path for every container (spec §2). ``face_key`` says which:
        "front"/"rear" are slots on a rack face, "other" is the tray, "bays" is a
        device bay in a chassis. Only the TARGET differs -- resolved once by
        _resolve_target -- and add/move/remove/existing/cancel, validation and
        the idempotency guard are shared. They used to be two methods, and every
        rack fix had to be re-proved against bays by hand.
        """
        kind = item["kind"]
        device_id = item.get("device_id")
        device_type_id = item.get("device_type_id")
        placement_id = item.get("placement_id")
        # The submitted kind, captured before the "existing" branch below may
        # rewrite the local ``kind`` to "move" as an unmoved-tile fallback.
        # Chain identity resolution (below) only ever applies to an EXPLICIT
        # move/remove -- never to that fallback -- so an unmodified inherited
        # tile that round-trips as "existing" cannot be silently promoted into
        # a new placement in this design.
        submitted_kind = kind

        # PLAN-design-chains.md G3/§8.5.1: a placement_id belonging to a
        # DIFFERENT design is never this design's own row to edit in place --
        # editing it would mutate an ancestor's (frozen, approved) placement
        # through the back door. Resolved once, up front, for both the guard
        # below (an 'add' item can only ever mean an add THIS design owns) and
        # the inherited-tile move/remove path (§8.5, G2), which is the one
        # sanctioned way a placement_id may legitimately name a placement
        # outside this design.
        foreign_placement = None
        if placement_id:
            foreign_placement = (
                DesignPlacement.objects.filter(pk=placement_id)
                .exclude(design=design)
                .select_related("design")
                .first()
            )

        target = self._resolve_target(design, rack, face_key, item, ref_map, errors)
        if target is None:
            return None
        # Kept for the error payloads and the vacated-slot comparison; the
        # placement's own target fields come from `target`, never from here.
        u_position = target.u_position

        # Full-depth devices occupy BOTH faces, and slot validation ignores their
        # face entirely (models.py: rack_face is None for a full-depth type). This
        # used to blank ``face`` for them, because the editor renders one tile per
        # face and the two copies had to reconcile to one row.
        #
        # It no longer needs to: buildRackPayload skips the opposite-face copy
        # (`if (w.opposite_face) return;`), so exactly ONE item per device is
        # posted -- the one on the face it is actually mounted on. Blanking it
        # threw away information API consumers legitimately need (an SDD diff
        # matching on (device, position, face) missed every full-depth row and
        # would delete-and-recreate them), while buying nothing.
        full_depth = self._item_is_full_depth(item)

        if foreign_placement is not None and kind == "add":
            # An inherited slot NEVER renders with kind="add" (it is always
            # "existing" + inherited -- see projection.py's baseline replay),
            # so a client sending kind="add" against a foreign placement_id is
            # either stale or malformed either way. Refuse it explicitly
            # rather than the old silent no-op (falling through the
            # design=design-scoped lookup below to "add is None -> return
            # None"), which looked like nothing happened but gave no feedback
            # that the edit was rejected.
            errors.append({
                "rack_id": rack.pk,
                "u_position": _norm_pos(u_position),
                "device_id": None,
                "detail": (
                    f"placement {placement_id} belongs to {foreign_placement.design}, "
                    f"not this design ({design}), and cannot be edited or cancelled "
                    f"here."
                ),
            })
            return None

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
                    **target.fields,
                    # Editor-chosen name (auto-filled from the naming engine and/or
                    # user-edited). Absent => "" (the model field is blank=True).
                    proposed_name=(item.get("proposed_name") or ""),
                    # The PDU power dialog's stashed config (docs/pdu-distribution-
                    # spec.md); only meaningful for a PDU add, but persisted as-is
                    # for whatever role sent it. Absent => None.
                    power_config=item.get("power_config"),
                    # The deployment's config-declared planning fields; validated
                    # against that schema by DesignPlacement.clean(). Absent =>
                    # None.
                    planning_data=item.get("planning_data"),
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
            for attr, value in target.fields.items():
                setattr(add, attr, value)
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
            if "planning_data" in item:
                add.planning_data = item.get("planning_data")
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

        # PLAN-design-chains.md G3/G2, §8.5.1: dragging an INHERITED tile whose
        # identity has no real device yet (an ancestor's still-planned 'add')
        # creates a move/remove in THIS design referencing base_placement,
        # never device. The item carries the SAME placement_id the widget
        # rendered -- the ancestor's 'add' pk, or (a later ancestor having
        # renamed it) a later ancestor's move that itself points at that same
        # add via its own base_placement_id -- and no device_id, since the
        # identity is not real. Only an EXPLICIT move/remove is eligible
        # (``submitted_kind``, not the "existing" fallback below): an
        # untouched inherited tile must never be silently promoted into a new
        # placement just because it lacks a real device to be "at rest" at.
        resolved_base_placement_id = None
        if (
            foreign_placement is not None
            and not device_id
            and submitted_kind in ("move", "remove")
        ):
            if foreign_placement.kind == DesignPlacementKindChoices.KIND_ADD:
                resolved_base_placement_id = foreign_placement.pk
            elif foreign_placement.base_placement_id:
                resolved_base_placement_id = foreign_placement.base_placement_id
            # Neither: `foreign_placement` names something that is not a
            # resolvable planned identity (e.g. a real-device move whose
            # device_id the item should have carried instead, but didn't).
            # Leave base_placement unset -- full_clean() below then reports
            # the ordinary "needs a device or a base_placement" validation
            # error, the same per-item error shape as any other collision.

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
            at_real = target.at_rest(device, full_depth)
            if at_real:
                if existing is not None:
                    existing.delete()
                    self._made_db_change = True
                return None
            # Moved within the editor without an explicit kind → treat as move.
            kind = "move"

        if kind == "remove":
            # A removal addresses the DEVICE: the model takes no target for one
            # at all, in a rack or a bay alike.
            placement = existing or DesignPlacement(design=design)
            placement.kind = DesignPlacementKindChoices.KIND_REMOVE
            placement.device = device
            placement.device_type = None
            placement.target_rack = None
            placement.target_position = None
            placement.target_face = ""
            placement.target_bay = None
            placement.parent_placement = None
            placement.target_bay_name = ""
            if resolved_base_placement_id:
                placement.base_placement_id = resolved_base_placement_id
        else:  # move
            placement = existing or DesignPlacement(design=design)
            placement.kind = DesignPlacementKindChoices.KIND_MOVE
            placement.device = device
            placement.device_type = None
            if resolved_base_placement_id:
                placement.base_placement_id = resolved_base_placement_id
            # Planned re-attribution (role / tenant / the deployment's own
            # planning fields): a move may state what the device BECOMES when it
            # lands, not just where it goes. Assigned only when the editor
            # actually sent the key, so a plain reposition that omits them keeps
            # whatever the placement already held and stays idempotent; an
            # explicit null clears the override and the device's own value
            # stands again.
            ok, move_role_id, move_tenant_id = self._resolve_add_refs(
                item, rack, u_position, errors
            )
            if not ok:
                return None
            if "device_role_id" in item:
                placement.device_role_id = move_role_id
            if "tenant_id" in item:
                placement.tenant_id = move_tenant_id
            if "planning_data" in item:
                placement.planning_data = item.get("planning_data")
            # A full-depth device occupies BOTH faces, so a client may still POST
            # one copy per face (the editor no longer does -- buildRackPayload
            # skips the opposite-face tile). Both copies reconcile to the same
            # placement, and the second must not flip the stored face: buckets are
            # walked front -> rear, so the FIRST copy wins and the face survives.
            # Captured BEFORE the target is applied, which writes the face too.
            already_written_this_pass = (
                full_depth
                and existing is not None
                and existing.pk in desired_placement_ids
            )
            face_already_chosen = (
                existing.target_face if already_written_this_pass else None
            )
            for attr, value in target.fields.items():
                setattr(placement, attr, value)
            if face_already_chosen is not None:
                placement.target_face = face_already_chosen

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

    # -----------------------------------------------------------------------
    # Design chains (PLAN-design-chains.md §5 phase 1 / G9): REST equivalents
    # of the ancestor/children lineage, the "Derive" HTML action, and the
    # "Re-base" HTML action.
    # -----------------------------------------------------------------------

    @action(detail=True, methods=["get"], url_path="chain")
    def chain(self, request, pk=None):
        """
        Read-only view of this design's lineage: its ``based_on`` ancestors
        (oldest first), its ``children`` (designs based on it), and whether the
        chain currently RESOLVES for projection -- reusing
        ``projection.resolve_baseline_chain`` (the §9.2 all-or-nothing rule)
        rather than re-deriving it, so this answers the SAME question the
        rack-face replay asks.

        ``ancestors`` is the raw ``based_on`` walk (``Design.baseline_chain()``)
        -- shown even when the chain is refused, so a client can see WHERE the
        break is, not just that there is one. A cycle degrades ``ancestors`` to
        ``[]`` (the walk cannot be ordered) but is still reported in
        ``refusal``.

        GET .../designs/<pk>/chain/
          -> {"ancestors": [<brief Design>, ...],
              "children": [<brief Design>, ...],
              "resolves": <bool>,
              "refusal": {"kind", "severity", "detail", "source_design"} | null}

        URL name: plugins-api:netbox_rack_design-api:design-chain
        Path:     /api/plugins/rack-design/designs/<pk>/chain/
        """
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "view")
        design = self.get_object()

        try:
            ancestors = design.baseline_chain()
        except ValueError:
            # A cycle: the walk itself cannot be ordered. resolve_baseline_chain
            # (below) reports the SAME break as ``refusal``; this just avoids
            # crashing the endpoint that displays it.
            ancestors = []
        children = list(design.children)
        _, refusal = projection.resolve_baseline_chain(design)

        context = {"request": request}
        refusal_data = None
        if refusal is not None:
            refusal_data = {
                "kind": refusal["kind"],
                "severity": refusal["severity"],
                "detail": refusal["detail"],
                "source_design": (
                    NestedDesignSerializer(refusal["source_design"], context=context).data
                    if refusal["source_design"] is not None else None
                ),
            }

        return Response(
            {
                "ancestors": NestedDesignSerializer(ancestors, many=True, context=context).data,
                "children": NestedDesignSerializer(children, many=True, context=context).data,
                "resolves": refusal is None,
                "refusal": refusal_data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="derive")
    def derive(self, request, pk=None):
        """
        Create a new design whose ``based_on`` points at this one -- the REST
        equivalent of ``DesignDeriveView`` (views.py), same rule: only an
        APPROVED (frozen) design may be derived from, because approval is what
        makes its placements trustworthy as a baseline (§2.2). Requires
        ``add_design`` (this CREATES a Design) -- the default TokenPermissions
        mapping for POST already gives exactly that, so ``get_permissions``
        does not override it for this action.

        POST .../designs/<pk>/derive/  (no body)
          -> 201 {<full Design representation of the new child>}
          -> 400 when this design is not approved

        URL name: plugins-api:netbox_rack_design-api:design-derive
        Path:     /api/plugins/rack-design/designs/<pk>/derive/
        """
        # Restrict by "add" (not "view"): the required permission for this
        # action IS add_design (it creates a Design), matching how add_rack /
        # remove_rack restrict by "change" -- the permission the action
        # actually needs, not a stricter or looser one.
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "add")
        design = self.get_object()

        if not design.is_frozen:
            return Response(
                {
                    "detail": (
                        f"Only an approved design can be derived from: approval "
                        f"is what makes a design's placements read-only, so a "
                        f"child can trust its baseline (PLAN-design-chains.md "
                        f"§2.2). {design} is "
                        f"{design.get_status_display().lower()}, not approved."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        child = Design(
            title=f"{design.title} (derived)",
            site=design.site,
            group=design.group,
            based_on=design,
            status=DesignStatusChoices.STATUS_DRAFT,
        )
        with transaction.atomic():
            child.full_clean()
            child.save()
            # G6: seed the child's rack scope with a SNAPSHOT of the parent's
            # racks at derive time, not a live link -- a rack added to the
            # parent later does NOT retroactively appear on the child, which
            # owns and edits its own scope from here on. This is safe because
            # baseline replay is per-rack: a rack present on the child but
            # absent from the parent simply has no inherited layer. Must run
            # after save() (the M2M needs a pk) and inside the same
            # transaction as the create, so a child can never exist with a
            # half-copied scope.
            child.racks.set(design.racks.all())
        logger.debug("api.derive: design=%s -> child=%s", design.pk, child.pk)
        return Response(
            DesignSerializer(child, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="rebase")
    def rebase(self, request, pk=None):
        """
        Re-point this design's ``based_on`` at a different (approved) design --
        the REST equivalent of ``DesignRebaseView`` (views.py). The documented
        way out of two situations (§2.2/§9.2): a parent later marked
        ``implemented`` (the chain refuses to project past it, so the child
        must re-base to render again), and a sibling that got approved first
        (§2.1 -- "first approved wins, the other re-bases"). Requires
        ``change_design`` (this edits THIS design's own field). Reuses the
        model's own cycle guard via ``full_clean()`` rather than
        re-implementing it -- only the "target must be approved" rule is
        checked here, because ``Design.clean()`` does not enforce it (that
        restriction lives only in the HTML form's queryset today).

        POST .../designs/<pk>/rebase/  body {"based_on": <pk>}
          -> 200 {<full Design representation, re-based>}
          -> 400 when the target does not exist, is not approved, or the
             resulting lineage is invalid (self-reference, cycle, cross-site)

        URL name: plugins-api:netbox_rack_design-api:design-rebase
        Path:     /api/plugins/rack-design/designs/<pk>/rebase/
        """
        if request.user.is_authenticated:
            self.queryset = Design.objects.restrict(request.user, "change")
        design = self.get_object()

        body = DesignRebaseSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        target_id = body.validated_data["based_on"]

        target = Design.objects.filter(pk=target_id).first()
        if target is None:
            return Response(
                {"based_on": ["Design does not exist."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if target.status != DesignStatusChoices.STATUS_APPROVED:
            return Response(
                {
                    "based_on": [
                        f"Only an approved design may be a base "
                        f"(PLAN-design-chains.md §2.2). {target} is "
                        f"{target.get_status_display().lower()}."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        previous_based_on_id = design.based_on_id
        design.based_on = target
        try:
            design.full_clean()
        except ValidationError as exc:
            design.based_on_id = previous_based_on_id
            errors = exc.message_dict if hasattr(exc, "message_dict") else {"based_on": [str(exc)]}
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        design.save()
        logger.debug("api.rebase: design=%s -> based_on=%s", design.pk, target.pk)
        return Response(
            DesignSerializer(design, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )


class DesignPlacementViewSet(NetBoxModelViewSet):
    # Every FK below is rendered as a nested brief by DesignPlacementSerializer, so
    # each one must be joined up front or the list endpoint issues a query per row
    # (device_type__manufacturer because the nested DeviceType brief includes it).
    queryset = DesignPlacement.objects.select_related(
        "design",
        "device",
        "device_type__manufacturer",
        "device_role",
        "tenant",
        "target_rack",
        "target_bay",
        "parent_placement",
    ).prefetch_related("tags")
    serializer_class = DesignPlacementSerializer
    filterset_class = filtersets.DesignPlacementFilterSet


class DesignPowerFeedViewSet(NetBoxModelViewSet):
    """A design's PLANNED power feeds -- the REST twin of the new UI views."""

    queryset = DesignPowerFeed.objects.select_related(
        "design", "rack"
    ).prefetch_related("tags", "bound_placements")
    serializer_class = DesignPowerFeedSerializer
    filterset_class = filtersets.DesignPowerFeedFilterSet

    def perform_destroy(self, instance):
        """
        ``DesignPowerFeed.clean()`` (models.py) now rejects a frozen design's
        create/update, but ``clean()`` never runs on delete -- exactly the
        gap the HTML delete/bulk-delete views (views.py) already guard
        explicitly for the same reason. ``perform_destroy`` is the one hook
        both DRF's single-object ``destroy()`` AND ``BulkDestroyModelMixin``'s
        ``bulk_destroy()`` funnel every deletion through, so overriding it
        here covers both with one check.

        ``_reject_frozen_design``'s ``Response`` isn't reusable AS-IS here:
        neither ``destroy()`` nor ``perform_bulk_destroy()`` do anything with
        this method's return value, so the only way to surface a 409 is to
        raise. Reuses its message text via ``_frozen_design_rest_message``
        rather than duplicating it by hand.
        """
        if instance.design.is_frozen:
            exc = APIException(_frozen_design_rest_message(instance.design))
            exc.status_code = status.HTTP_409_CONFLICT
            raise exc
        super().perform_destroy(instance)


class FavoriteSetViewSet(viewsets.ViewSet):
    """
    The requesting user's NAMED favorite sets ("Default", "for server", ...).

    Like the favorites it groups, this is deliberately NOT a NetBoxModelViewSet:
    every query is filtered by ``request.user`` and the client never supplies a
    user, so a user can only ever see or change their own sets.

    Endpoints:
      GET    /api/plugins/rack-design/favorite-sets/
             -> {"results": [{"id", "name", "is_default", "device_type_ids"}, ...]}
      POST   /api/plugins/rack-design/favorite-sets/   body {"name"}
      PATCH  /api/plugins/rack-design/favorite-sets/<id>/  body {"name"}  (rename)
      DELETE /api/plugins/rack-design/favorite-sets/<id>/  (drops its stars too)

    The listing always contains at least one set: a user who has never starred
    anything gets their default provisioned on first read, so the editor always
    has a set to work in.
    """

    permission_classes = [IsAuthenticated]

    @staticmethod
    def _serialize(fav_set, members):
        return {
            "id": fav_set.pk,
            "name": fav_set.name,
            "is_default": fav_set.name == FavoriteSet.DEFAULT_NAME,
            "device_type_ids": members.get(fav_set.pk, []),
        }

    def _rows(self, user):
        sets = list(FavoriteSet.objects.filter(user=user).order_by("name"))
        if not sets:
            sets = [FavoriteSet.default_for(user)]
        members = {}
        for set_id, dt_id in FavoriteDeviceType.objects.filter(
            favorite_set__in=sets
        ).values_list("favorite_set_id", "device_type_id"):
            members.setdefault(set_id, []).append(dt_id)
        # The default set leads: it is what the editor selects when the user has
        # made no choice, so it should not be hunted for in the middle of a list.
        sets.sort(key=lambda s: (s.name != FavoriteSet.DEFAULT_NAME, s.name.lower()))
        return [self._serialize(s, members) for s in sets]

    def list(self, request):
        return Response({"results": self._rows(request.user)})

    def create(self, request):
        body = FavoriteSetWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        name = body.validated_data["name"].strip()
        if FavoriteSet.objects.filter(user=request.user, name__iexact=name).exists():
            return Response(
                {"name": ["You already have a favorite set with that name."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        fav_set = FavoriteSet.objects.create(user=request.user, name=name)
        return Response(
            self._serialize(fav_set, {}), status=status.HTTP_201_CREATED
        )

    def partial_update(self, request, pk=None):
        """Rename one of the user's own sets."""
        fav_set = FavoriteSet.objects.filter(user=request.user, pk=pk).first()
        if fav_set is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        body = FavoriteSetWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        name = body.validated_data["name"].strip()
        clash = FavoriteSet.objects.filter(
            user=request.user, name__iexact=name
        ).exclude(pk=fav_set.pk).exists()
        if clash:
            return Response(
                {"name": ["You already have a favorite set with that name."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        fav_set.name = name
        fav_set.save(update_fields=["name"])
        members = {
            fav_set.pk: list(
                FavoriteDeviceType.objects.filter(favorite_set=fav_set)
                .values_list("device_type_id", flat=True)
            )
        }
        return Response(self._serialize(fav_set, members))

    def destroy(self, request, pk=None):
        """Delete one of the user's own sets, and with it its stars.

        Deleting the last set is allowed: the next read provisions an empty
        default rather than leaving the editor with nothing to work in.
        """
        fav_set = FavoriteSet.objects.filter(user=request.user, pk=pk).first()
        if fav_set is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        removed = FavoriteDeviceType.objects.filter(favorite_set=fav_set).count()
        fav_set.delete()
        return Response({"deleted": True, "favorites_removed": removed})


class FavoriteDeviceTypeViewSet(viewsets.ViewSet):
    """
    User-scoped "favorite device types" (the catalog palette's stars).

    This is deliberately NOT a NetBoxModelViewSet: a generic model viewset would
    expose every user's rows. Every query here is filtered by ``request.user``
    and the client NEVER supplies a user — a user can only ever read or change
    their own favorites.

    Stars live in a :class:`FavoriteSet`. ``set_id`` selects which one; omitted
    (or naming a set that is not the requesting user's) it falls back to that
    user's default set, which is also what an older client gets.

    Endpoints:
      GET  /api/plugins/rack-design/favorite-device-types/[?set_id=<id>]
           -> {"set_id": <id>, "device_type_ids": [...]}
      POST /api/plugins/rack-design/favorite-device-types/toggle/ -> star/unstar
    """

    permission_classes = [IsAuthenticated]

    @staticmethod
    def _resolve_set(user, raw_set_id):
        """The user's set named by ``raw_set_id``, or their default.

        Another user's set id resolves to the caller's default rather than
        404-ing: set ids are UI state that can go stale (the set was deleted in
        another tab), and the safe reading of a stale id is "no set chosen".
        """
        try:
            set_id = int(raw_set_id)
        except (TypeError, ValueError):
            set_id = 0
        if set_id:
            owned = FavoriteSet.objects.filter(user=user, pk=set_id).first()
            if owned is not None:
                return owned
        return FavoriteSet.default_for(user)

    def list(self, request):
        """Return the requesting user's favorite device-type ids in one set."""
        fav_set = self._resolve_set(request.user, request.query_params.get("set_id"))
        ids = list(
            FavoriteDeviceType.objects.filter(user=request.user, favorite_set=fav_set)
            .values_list("device_type_id", flat=True)
        )
        return Response({"set_id": fav_set.pk, "device_type_ids": ids})

    @action(detail=False, methods=["post"], url_path="toggle")
    def toggle(self, request):
        """
        Star or unstar a device type for the requesting user (idempotent).

        Body: {"device_type_id": <id>, "set_id": <id>?}. Returns
        {"device_type_id", "set_id", "favorite"} where ``favorite`` reflects the
        resulting state within that set.
        """
        body = FavoriteToggleSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        device_type_id = body.validated_data["device_type_id"]

        if not DeviceType.objects.filter(pk=device_type_id).exists():
            return Response(
                {"device_type_id": ["Device type does not exist."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        fav_set = self._resolve_set(request.user, body.validated_data.get("set_id"))
        favorite, created = FavoriteDeviceType.objects.get_or_create(
            user=request.user, favorite_set=fav_set, device_type_id=device_type_id
        )
        if created:
            return Response({
                "device_type_id": device_type_id,
                "set_id": fav_set.pk,
                "favorite": True,
            })

        # Already starred in this set → toggle off.
        favorite.delete()
        return Response({
            "device_type_id": device_type_id,
            "set_id": fav_set.pk,
            "favorite": False,
        })


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

    The optional ``role_id`` is the role the add would carry (the editor's
    palette Role select). It matters because the excluded-role rule
    (``power_exclude_roles``) lives in the projection: a PDU is not a consumer,
    so with the PDU role selected a type reports a known 0 W instead of the
    unknown its draw-less inlet template would otherwise yield -- the same
    figure the slot gets after Save. An unknown/blank role_id is ignored.

    Endpoint:
      GET /api/plugins/rack-design/device-type-power/?id=1&id=2...[&role_id=9]
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
        role = None
        try:
            role_id = int(request.query_params.get("role_id") or 0)
        except (TypeError, ValueError):
            role_id = 0
        if role_id:
            role = DeviceRole.objects.filter(pk=role_id).first()
        results = {}
        if ids:
            types = DeviceType.objects.filter(pk__in=ids).prefetch_related(
                "powerporttemplates"
            )
            for dt in types:
                results[str(dt.pk)] = projection.device_type_power_summary(
                    dt, role=role)
        return Response({"results": results})


class HiddenDesignChassisViewSet(viewsets.ViewSet):
    """
    User-scoped per-design CHASSIS visibility for the chassis layer (spec §10.3).

    The chassis-layer twin of HiddenDesignRackViewSet, and deliberately identical in
    shape: HIDDEN rows are stored (so an empty set means "everything visible"),
    every query is filtered by ``request.user``, and the client never supplies a
    user. Hiding a chassis is personal view state -- it never touches the design.

    Endpoints:
      GET  /api/plugins/rack-design/hidden-design-chassis/?design_id=<id>
      POST /api/plugins/rack-design/hidden-design-chassis/toggle/
           body {"design_id", "chassis_id"}
    """

    permission_classes = [IsAuthenticated]

    def _hidden_ids(self, user, design_id):
        return list(
            HiddenDesignChassis.objects.filter(user=user, design_id=design_id)
            .values_list("chassis_id", flat=True)
        )

    def list(self, request):
        design_id = request.query_params.get("design_id")
        if not design_id:
            return Response(
                {"design_id": ["This query parameter is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({
            "design_id": int(design_id),
            "hidden_chassis_ids": self._hidden_ids(request.user, design_id),
        })

    @action(detail=False, methods=["post"], url_path="toggle")
    def toggle(self, request):
        """Hide or show one (design, chassis) for the requesting user."""
        body = HiddenChassisToggleSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        design_id = body.validated_data["design_id"]
        chassis_id = body.validated_data["chassis_id"]

        if not Design.objects.filter(pk=design_id).exists():
            return Response({"design_id": ["Design does not exist."]},
                            status=status.HTTP_400_BAD_REQUEST)
        if not Device.objects.filter(pk=chassis_id).exists():
            return Response({"chassis_id": ["Device does not exist."]},
                            status=status.HTTP_400_BAD_REQUEST)

        hidden, created = HiddenDesignChassis.objects.get_or_create(
            user=request.user, design_id=design_id, chassis_id=chassis_id
        )
        if not created:
            hidden.delete()
        return Response({
            "design_id": design_id,
            "chassis_id": chassis_id,
            "hidden": created,
            "hidden_chassis_ids": self._hidden_ids(request.user, design_id),
        })


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


class PlacementFieldsView(views.APIView):
    """The deployment's ``placement_fields`` descriptors.

    An API client cannot know which planning fields exist -- that is the whole
    point of declaring them in config rather than hardcoding them -- so it asks
    here before POSTing a placement with ``planning_data``. Read-only, and
    ``target`` is withheld: it names a real custom field on the deployment's
    devices, which is apply-time plumbing rather than part of the client
    contract.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(planning_fields.public_placement_field_schema())
