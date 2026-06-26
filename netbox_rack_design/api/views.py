"""REST API viewsets for NetBox Rack Design."""

from dcim.models import Device, Rack
from django.core.exceptions import ValidationError
from django.db import transaction
from netbox.api.authentication import TokenPermissions
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from .. import filtersets
from ..choices import DesignPlacementKindChoices
from ..models import Design, DesignGroup, DesignPlacement
from .serializers import (
    DesignGroupSerializer,
    DesignPlacementSerializer,
    DesignSerializer,
    SaveLayoutSerializer,
)

__all__ = ("DesignGroupViewSet", "DesignViewSet", "DesignPlacementViewSet")


class DesignGroupViewSet(NetBoxModelViewSet):
    queryset = DesignGroup.objects.all()
    serializer_class = DesignGroupSerializer
    filterset_class = filtersets.DesignGroupFilterSet


def _norm_pos(value):
    """Normalise a U position to a float for comparison, or None."""
    return None if value is None else float(value)


class SaveLayoutPermissions(TokenPermissions):
    """
    Save-layout is a write that EDITS an existing Design (not creation), so a
    POST to it must require ``change_design`` rather than the default
    ``add_design`` that TokenPermissions maps POST to.
    """

    perms_map = {
        **TokenPermissions.perms_map,
        "POST": ["%(app_label)s.change_%(model_name)s"],
    }


class DesignViewSet(NetBoxModelViewSet):
    queryset = Design.objects.prefetch_related("placements", "depends_on", "tags")
    serializer_class = DesignSerializer
    filterset_class = filtersets.DesignFilterSet

    def get_permissions(self):
        if getattr(self, "action", None) == "save_layout":
            return [SaveLayoutPermissions()]
        return super().get_permissions()

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

        # Serialize the design's resulting move/remove placements.
        placements = DesignPlacement.objects.filter(
            design=design,
            kind__in=(
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
        )

    def _reconcile_item(self, design, rack, face_key, item, errors, desired_placement_ids):
        """
        Map one desired item to its DesignPlacement (or no placement), upserting
        and full_clean()-validating as needed. Appends to ``errors`` on failure
        and returns the placement (or None when no placement is needed).
        """
        kind = item["kind"]
        device_id = item.get("device_id")
        placement_id = item.get("placement_id")
        # 'other' bucket means off-rack: no target position.
        u_position = None if face_key == "other" else item.get("u_position")
        face = "" if face_key == "other" else item.get("face") or ""

        # An 'add' tile is a catalog-add placement projected into this rack. It
        # carries its placement_id (no device_id). We preserve it and let the user
        # REPOSITION it within the rack (drag to a new U/face); brand-new catalog
        # adds aren't created here. The device_type is left untouched.
        if kind == "add":
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
            before = self._snapshot(add)
            add.target_rack = rack
            add.target_position = u_position
            add.target_face = face
            # Idempotent: an unmoved add round-trips without a write.
            if self._snapshot(add) == before:
                desired_placement_ids.add(add.pk)
                return add
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
            # up any stale move/remove this design holds for it.
            at_real = (
                device is not None
                and device.rack_id == rack.pk
                and _norm_pos(device.position) == _norm_pos(u_position)
                and (device.face or "") == face
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

        # Idempotency guard: if we matched an existing placement and none of its
        # meaningful fields changed, do NOT write (no full_clean/save) so an
        # untouched round-trip neither bumps last_updated nor reports a change.
        if before is not None and self._snapshot(placement) == before:
            desired_placement_ids.add(placement.pk)
            return placement

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
