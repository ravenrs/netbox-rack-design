"""REST API serializers for NetBox Rack Design."""

from dcim.api.serializers import RackSerializer
from dcim.models import Rack
from netbox.api.fields import SerializedPKRelatedField
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import Design, DesignGroup, DesignPlacement

__all__ = (
    "DesignGroupSerializer",
    "DesignSerializer",
    "DesignPlacementSerializer",
    "SaveLayoutSerializer",
    "FavoriteToggleSerializer",
    "DesignRackScopeSerializer",
    "HiddenRackToggleSerializer",
    "HiddenRackShowAllSerializer",
)


class DesignGroupSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:designgroup-detail"
    )

    class Meta:
        model = DesignGroup
        fields = (
            "id", "url", "display", "name", "parent", "description", "link",
            "tags", "custom_fields", "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class DesignSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:design-detail"
    )
    # Brief Rack representations on read; accepts a list of rack PKs on write
    # (SerializedPKRelatedField is the writable-M2M-nested pattern core uses).
    racks = SerializedPKRelatedField(
        queryset=Rack.objects.all(),
        serializer=RackSerializer,
        nested=True,
        required=False,
        many=True,
    )

    class Meta:
        model = Design
        fields = (
            "id", "url", "display", "title", "site", "status", "summary", "link",
            "version", "root", "based_on", "sequence", "depends_on", "racks", "group",
            "description", "comments", "tags", "custom_fields", "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "title", "version", "status")


class DesignPlacementSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:designplacement-detail"
    )

    class Meta:
        model = DesignPlacement
        fields = (
            "id", "url", "display", "design", "kind", "device", "device_type",
            "proposed_name", "device_role", "tenant",
            "target_rack", "target_position", "target_face",
            "tags", "custom_fields", "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "kind")


# ---------------------------------------------------------------------------
# Save-layout request serializers (Stage 2, increment 2a)
#
# These validate the *shape* of the editor's "save" payload only. They are not
# ModelSerializers: the actual diff/upsert against DesignPlacement happens in
# the viewset action, where every built placement is run through full_clean().
# ---------------------------------------------------------------------------


class SaveLayoutItemSerializer(serializers.Serializer):
    """A single device entry within one face (or 'other') of a rack."""

    kind = serializers.ChoiceField(choices=("existing", "move", "remove", "add"))
    device_id = serializers.IntegerField(required=False, allow_null=True)
    device_type_id = serializers.IntegerField(required=False, allow_null=True)
    placement_id = serializers.IntegerField(required=False, allow_null=True)
    # Intended role/tenant for a brand-new planned device (add); optional.
    device_role_id = serializers.IntegerField(required=False, allow_null=True)
    tenant_id = serializers.IntegerField(required=False, allow_null=True)
    u_position = serializers.DecimalField(
        max_digits=4, decimal_places=1, required=False, allow_null=True
    )
    face = serializers.ChoiceField(
        choices=("front", "rear", ""), required=False, allow_blank=True, default=""
    )
    # Accepted for forward-compatibility with 'add'; ignored this slice.
    proposed_name = serializers.CharField(required=False, allow_blank=True, default="")
    # When true on an 'add' item, the user flagged the planned addition for
    # cancellation via the editor's × — the add placement is DELETED on save.
    cancel = serializers.BooleanField(required=False, default=False)

    def validate(self, data):
        kind = data["kind"]
        if kind == "add" and not data.get("placement_id") and not data.get("device_type_id"):
            # An 'add' item is valid when it either re-asserts an EXISTING add
            # placement (carrying its placement_id, for reposition/cancel) OR
            # creates a brand-new catalog add (carrying a device_type_id). An
            # 'add' that has NEITHER is meaningless and is rejected.
            raise serializers.ValidationError(
                {"kind": "An 'add' item requires either a placement_id or a device_type_id."}
            )
        if kind in ("move", "remove") and not data.get("device_id"):
            raise serializers.ValidationError(
                {"device_id": f"A '{kind}' item requires a device_id."}
            )
        return data


class SaveLayoutRackSerializer(serializers.Serializer):
    """One rack's desired contents, split by face plus an off-rack 'other' bucket."""

    rack_id = serializers.IntegerField()
    front = SaveLayoutItemSerializer(many=True, required=False, default=list)
    rear = SaveLayoutItemSerializer(many=True, required=False, default=list)
    other = SaveLayoutItemSerializer(many=True, required=False, default=list)


class SaveLayoutSerializer(serializers.Serializer):
    """Top-level body for POST .../designs/<pk>/save-layout/."""

    design_id = serializers.IntegerField()
    racks = SaveLayoutRackSerializer(many=True)


# ---------------------------------------------------------------------------
# Favorite-device-type request serializer (increment 2c-1)
#
# Validates only the shape of the toggle body. The viewset enforces that the
# referenced DeviceType exists and scopes every row to request.user.
# ---------------------------------------------------------------------------


class FavoriteToggleSerializer(serializers.Serializer):
    """Body for POST .../favorite-device-types/toggle/."""

    device_type_id = serializers.IntegerField()


# ---------------------------------------------------------------------------
# Multi-rack workspace request serializers (Phase A)
#
# Validate only the shape of the request body. The viewset/action enforces the
# same-site rule, object permissions, and user scoping.
# ---------------------------------------------------------------------------


class DesignRackScopeSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/add-rack/ and .../remove-rack/."""

    rack_id = serializers.IntegerField()
    # remove-rack only: must be true to confirm a destructive removal when the
    # rack still has planned placements targeting it. Ignored by add-rack.
    confirm = serializers.BooleanField(required=False, default=False)


class HiddenRackToggleSerializer(serializers.Serializer):
    """Body for POST .../hidden-design-racks/toggle/ (per-user view state)."""

    design_id = serializers.IntegerField()
    rack_id = serializers.IntegerField()


class HiddenRackShowAllSerializer(serializers.Serializer):
    """Body for POST .../hidden-design-racks/show-all/ (per-user view state)."""

    design_id = serializers.IntegerField()
