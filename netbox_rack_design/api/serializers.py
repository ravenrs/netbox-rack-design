"""REST API serializers for NetBox Rack Design."""

from dcim.api.serializers import RackSerializer
from dcim.choices import PowerFeedPhaseChoices, PowerFeedSupplyChoices
from dcim.models import Rack
from netbox.api.fields import SerializedPKRelatedField
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import Design, DesignGroup, DesignPlacement, DesignPowerFeed

__all__ = (
    "DesignGroupSerializer",
    "DesignSerializer",
    "DesignPlacementSerializer",
    "SaveLayoutSerializer",
    "PreviewNameSerializer",
    "FavoriteToggleSerializer",
    "DesignRackScopeSerializer",
    "HiddenRackToggleSerializer",
    "HiddenRackShowAllSerializer",
    "RackPowerSerializer",
    "PlannedFeedSerializer",
    "PlannedFeedUpsertSerializer",
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
    # The editor-chosen proposed name for an 'add' (auto-filled from the naming
    # engine, user-editable) or a 'move' (the §4a keep/rename choice). Optional and
    # WITHOUT a default so the viewset can tell "the editor sent a name" (set it)
    # from "the editor omitted it" (leave the placement's existing name untouched).
    proposed_name = serializers.CharField(
        required=False, allow_blank=True, max_length=64
    )
    # When true on an 'add' item, the user flagged the planned addition for
    # cancellation via the editor's × — the add placement is DELETED on save.
    cancel = serializers.BooleanField(required=False, default=False)
    # The PDU power dialog's stashed config (docs/pdu-distribution-spec.md), sent
    # only for a PDU add. WITHOUT a default so an item that omits it (any other
    # role, or an untouched reposition) leaves the placement's existing
    # power_config field alone.
    power_config = serializers.JSONField(required=False, allow_null=True)
    # The feed this PDU add binds to (docs/pdu-distribution-spec.md §6.2/§8) --
    # a real dcim.PowerFeed OR a planned DesignPowerFeed, never both. WITHOUT a
    # default so an item that omits both (any other role, or an untouched
    # reposition) leaves the placement's existing binding alone.
    real_power_feed_id = serializers.IntegerField(required=False, allow_null=True)
    planned_power_feed_id = serializers.IntegerField(required=False, allow_null=True)
    # The real PDU device this planned PDU inherits its custom fields from
    # (docs/pdu-distribution-spec.md §6) -- cf are then read LIVE off that device,
    # an alternative to a manual ``power_config``. WITHOUT a default so an item
    # that omits it leaves the placement's existing source device alone.
    power_source_device_id = serializers.IntegerField(required=False, allow_null=True)

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
# Name-preview request serializer (Phase 2)
#
# Validates the *shape* of a prospective placement so the editor can ask the
# naming engine what a tile WOULD be named without persisting anything. It is
# not a ModelSerializer: the viewset builds an UNSAVED DesignPlacement from these
# values, resolves the FKs by PK (tolerating missing ones), and never writes.
# ---------------------------------------------------------------------------


class PreviewNameSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/preview-name/."""

    kind = serializers.ChoiceField(
        choices=("add", "move", "remove"), required=False, default="add"
    )
    # FKs are accepted as bare PKs; the viewset resolves them (400 on a bad PK).
    device_type = serializers.IntegerField(required=False, allow_null=True)
    device = serializers.IntegerField(required=False, allow_null=True)
    device_role = serializers.IntegerField(required=False, allow_null=True)
    tenant = serializers.IntegerField(required=False, allow_null=True)
    target_rack = serializers.IntegerField(required=False, allow_null=True)
    target_position = serializers.DecimalField(
        max_digits=4, decimal_places=1, required=False, allow_null=True
    )
    target_face = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
    # The ordinal the prospective tile would take, so the editor can preview a
    # name for a not-yet-persisted position without first saving the placement.
    index = serializers.IntegerField(required=False, allow_null=True)
    # Names already assigned in the CURRENT editor session (unsaved siblings,
    # invisible to the DB) so the naming engine never hands two same-session
    # previews the same name (user bug 2026-07-10). Capped defensively.
    pending_names = serializers.ListField(
        child=serializers.CharField(max_length=200, allow_blank=True),
        required=False,
        default=list,
        max_length=500,
    )


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


# ---------------------------------------------------------------------------
# Rack power request serializer (Phase B)
#
# Validates only the shape of the POST body for .../designs/<pk>/rack-power/.
# The viewset upserts the DesignRackPower row; this never writes to dcim.
# ---------------------------------------------------------------------------


class RackPowerSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/rack-power/."""

    rack_id = serializers.IntegerField()
    power_config = serializers.JSONField(required=False, allow_null=True)


# ---------------------------------------------------------------------------
# Planned power feed serializers (Phase C, docs/pdu-distribution-spec.md §6/§8)
#
# DesignPowerFeed is plain planning scratch data (not a NetBoxModel), so a
# plain ModelSerializer is enough -- no url/display/tags/custom_fields.
# ---------------------------------------------------------------------------


class PlannedFeedSerializer(serializers.ModelSerializer):
    """Read shape for one DesignPowerFeed (the planned-feed action's response)."""

    class Meta:
        model = DesignPowerFeed
        fields = ("id", "name", "voltage", "amperage", "phase", "supply")


class PlannedFeedUpsertSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/planned-feed/ (upsert by rack+name)."""

    rack_id = serializers.IntegerField()
    name = serializers.CharField(max_length=100)
    voltage = serializers.IntegerField(required=False)
    amperage = serializers.IntegerField(required=False)
    phase = serializers.ChoiceField(choices=PowerFeedPhaseChoices, required=False)
    supply = serializers.ChoiceField(choices=PowerFeedSupplyChoices, required=False)
