"""REST API serializers for NetBox Rack Design."""

from dcim.api.serializers import (
    DeviceBaySerializer,
    DeviceRoleSerializer,
    DeviceSerializer,
    DeviceTypeSerializer,
    RackSerializer,
    SiteSerializer,
)
from dcim.choices import PowerFeedPhaseChoices, PowerFeedSupplyChoices
from dcim.models import Rack
from netbox.api.fields import SerializedPKRelatedField
from netbox.api.serializers import NetBoxModelSerializer, WritableNestedSerializer
from rest_framework import serializers
from tenancy.api.serializers import TenantSerializer

from ..models import Design, DesignGroup, DesignPlacement, DesignPowerFeed

__all__ = (
    "NestedDesignGroupSerializer",
    "NestedDesignSerializer",
    "NestedDesignPlacementSerializer",
    "DesignGroupSerializer",
    "DesignSerializer",
    "DesignPlacementSerializer",
    "DesignPowerFeedSerializer",
    "SaveLayoutSerializer",
    "PreviewNameSerializer",
    "FavoriteSetWriteSerializer",
    "FavoriteToggleSerializer",
    "DesignRackScopeSerializer",
    "HiddenRackToggleSerializer",
    "HiddenChassisToggleSerializer",
    "HiddenRackShowAllSerializer",
    "RackPowerSerializer",
    "PlannedFeedSerializer",
    "PlannedFeedDeleteSerializer",
    "PlannedFeedUpsertSerializer",
    "DesignRebaseSerializer",
)


# Self-referential FKs (DesignGroup.parent, Design.root / based_on / depends_on)
# cannot reference their own serializer from inside its class body, so they get an
# explicit brief serializer each -- the same shape as the corresponding
# ``brief_fields``, and the pattern core uses for its own recursive relations
# (e.g. dcim NestedRegionSerializer). WritableNestedSerializer renders the brief
# representation on read and accepts a PK (or an attrs dict) on write.
class NestedDesignGroupSerializer(WritableNestedSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:designgroup-detail"
    )

    class Meta:
        model = DesignGroup
        fields = ("id", "url", "display", "name")


class NestedDesignSerializer(WritableNestedSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:design-detail"
    )

    class Meta:
        model = Design
        fields = ("id", "url", "display", "title", "version", "status")


class DesignGroupSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:designgroup-detail"
    )
    parent = NestedDesignGroupSerializer(required=False, allow_null=True)

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
    site = SiteSerializer(nested=True)
    group = NestedDesignGroupSerializer(required=False, allow_null=True)
    root = NestedDesignSerializer(required=False, allow_null=True)
    based_on = NestedDesignSerializer(required=False, allow_null=True)
    depends_on = SerializedPKRelatedField(
        queryset=Design.objects.all(),
        serializer=NestedDesignSerializer,
        required=False,
        many=True,
    )
    # Read-only (PLAN-design-chains.md G9): a write against a frozen design
    # already gets a 409 from every design-scoped write action
    # (``_reject_frozen_design``) -- this lets a client know that in advance,
    # rather than discovering it by failing a write.
    is_frozen = serializers.BooleanField(read_only=True)

    class Meta:
        model = Design
        fields = (
            "id", "url", "display", "title", "site", "status", "summary", "link",
            "version", "root", "based_on", "sequence", "depends_on", "racks", "group",
            "description", "comments", "is_frozen", "tags", "custom_fields",
            "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "title", "version", "status")


class NestedDesignPlacementSerializer(WritableNestedSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:designplacement-detail"
    )

    class Meta:
        model = DesignPlacement
        fields = ("id", "url", "display", "kind")


class DesignPlacementSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:designplacement-detail"
    )
    design = NestedDesignSerializer()
    device = DeviceSerializer(nested=True, required=False, allow_null=True)
    device_type = DeviceTypeSerializer(nested=True, required=False, allow_null=True)
    device_role = DeviceRoleSerializer(nested=True, required=False, allow_null=True)
    tenant = TenantSerializer(nested=True, required=False, allow_null=True)
    target_rack = RackSerializer(nested=True, required=False, allow_null=True)
    # Device-bay targeting: a real dcim.DeviceBay (existing chassis) or the
    # placement of a chassis planned in the same design.
    target_bay = DeviceBaySerializer(nested=True, required=False, allow_null=True)
    parent_placement = NestedDesignPlacementSerializer(required=False, allow_null=True)
    # The upstream placement this move/remove acts on when the device it
    # targets is not yet real -- only an ancestor design's planned 'add' (G2,
    # PLAN-design-chains.md). Round-trips like parent_placement: a raw pk on
    # write, a nested object on read.
    base_placement = NestedDesignPlacementSerializer(required=False, allow_null=True)
    # The ancestor-planned CHASSIS this blade goes into (G2, the parent-side twin
    # of base_placement). Same round-trip shape: a raw pk on write, a nested
    # object on read.
    base_parent_placement = NestedDesignPlacementSerializer(required=False, allow_null=True)

    class Meta:
        model = DesignPlacement
        fields = (
            "id", "url", "display", "design", "kind", "device", "device_type",
            "proposed_name", "device_role", "tenant",
            "target_rack", "target_position", "target_face",
            "parent_placement", "target_bay", "target_bay_name",
            "base_placement", "base_parent_placement",
            "planning_data", "stale", "stale_device_name",
            "tags", "custom_fields", "created", "last_updated",
        )
        # Staleness is an OBSERVATION, never a client input: it is stamped when
        # the referenced device is deleted and cleared by re-pointing the
        # placement at a real one. A writable flag would let a client claim a
        # device-less move/remove is legitimate and bypass validation.
        read_only_fields = ("stale", "stale_device_name")
        brief_fields = ("id", "url", "display", "kind")


class DesignPowerFeedSerializer(NetBoxModelSerializer):
    """A design's PLANNED power feed -- read, edit and delete it like any object.

    Mirrors ``dcim.PowerFeed``'s field names on purpose (see the model), and
    exposes ``derated_watts``: the figure this feed actually contributes to its
    rack's capacity bar, so an API client sees the same number the UI does.
    """

    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:designpowerfeed-detail"
    )
    design = NestedDesignSerializer()
    rack = RackSerializer(nested=True)
    derated_watts = serializers.IntegerField(read_only=True)

    class Meta:
        model = DesignPowerFeed
        fields = (
            "id", "url", "display", "design", "rack", "name",
            "voltage", "amperage", "phase", "supply", "derated_watts",
            "tags", "custom_fields", "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


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
    # The deployment's own config-declared planning fields
    # (``placement_fields``), flat ``{key: value}``. WITHOUT a default so an
    # item that omits the key leaves the placement's stored values alone; an
    # explicit ``{}`` clears them.
    planning_data = serializers.JSONField(required=False, allow_null=True)
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
    # --- device-bay targeting (a blade into a chassis) ----------------------
    # ``ref`` is a CLIENT-side identifier the editor stamps on an item so another
    # item in the SAME submit can point at it. It is needed because a blade going
    # into a chassis that is itself being added has no placement_id to reference
    # yet -- the chassis row does not exist until this save creates it. The view
    # processes the rack buckets first, records ref -> placement, then resolves
    # ``parent_ref`` on the bay items. Neither is persisted.
    ref = serializers.CharField(required=False, allow_blank=True, max_length=64)
    parent_ref = serializers.CharField(required=False, allow_blank=True, max_length=64)
    # The chassis placement when it ALREADY EXISTS (the chassis layer only renders
    # chassis the design has saved, so it addresses them by pk rather than by a
    # client ref -- ``parent_ref`` is only needed for a chassis being created by
    # the very same submit).
    parent_placement_id = serializers.IntegerField(required=False, allow_null=True)
    # The real dcim.DeviceBay this blade goes into (chassis already in DCIM).
    target_bay_id = serializers.IntegerField(required=False, allow_null=True)
    # Which bay, by name -- required for a planned chassis (its bays do not exist
    # yet) and mirrored from the real bay otherwise.
    target_bay_name = serializers.CharField(
        required=False, allow_blank=True, max_length=64
    )

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
        if (
            kind in ("move", "remove")
            and not data.get("device_id")
            and not data.get("placement_id")
        ):
            # Ordinarily a move/remove addresses a real device (device_id).
            # PLAN-design-chains.md G3/§8.5.1: dragging an INHERITED tile whose
            # ancestor identity has no real device yet carries no device_id at
            # all -- the widget's own placement_id (the ancestor's 'add') is
            # the only handle on that identity, and the viewset resolves it to
            # base_placement. So a placement_id alone is also acceptable here;
            # the viewset itself refuses one that turns out not to name a true
            # ancestor 'add'.
            raise serializers.ValidationError(
                {"device_id": f"A '{kind}' item requires a device_id or a placement_id."}
            )
        return data


class SaveLayoutRackSerializer(serializers.Serializer):
    """One rack's desired contents, split by face plus an off-rack 'other' bucket."""

    rack_id = serializers.IntegerField()
    front = SaveLayoutItemSerializer(many=True, required=False, default=list)
    rear = SaveLayoutItemSerializer(many=True, required=False, default=list)
    other = SaveLayoutItemSerializer(many=True, required=False, default=list)
    # Blades: placed IN a chassis bay rather than AT a rack unit, so they cannot
    # live in a face bucket. Processed after the face buckets so a blade can
    # reference a chassis created by the same submit (see ``ref``/``parent_ref``).
    bays = SaveLayoutItemSerializer(many=True, required=False, default=list)


class SaveLayoutSerializer(serializers.Serializer):
    """Top-level body for POST .../designs/<pk>/save-layout/."""

    design_id = serializers.IntegerField()
    racks = SaveLayoutRackSerializer(many=True)


class RecomputeDistributionSerializer(SaveLayoutSerializer):
    """Body for POST .../designs/<pk>/recompute-distribution/.

    The save-layout body plus ``project_racks``: which racks the caller wants
    numbers back for. Every submitted rack is still RECONCILED -- a cross-rack
    move only makes sense with both ends applied, and a device that left rack A
    is described by a placement filed under rack B -- but only the listed racks
    are PROJECTED, and projection is the expensive half: the distribution engine
    runs once per rack, over that rack's devices and PDUs.

    Omit the field, or send an empty list, to project every submitted rack. That
    is what a full refresh wants (the first paint, a feed change), and it keeps
    an older editor working unchanged against a newer server.
    """

    project_racks = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )


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
    """Body for POST .../favorite-device-types/toggle/.

    ``set_id`` names which of the user's favorite SETS to star into. It stays
    optional so an older client (and the plain "star it" case) keeps working:
    the viewset falls back to the user's default set.
    """

    device_type_id = serializers.IntegerField()
    set_id = serializers.IntegerField(required=False, allow_null=True)


class FavoriteSetWriteSerializer(serializers.Serializer):
    """Body for POST/PATCH .../favorite-sets/ -- the set's name.

    A name is the user's only handle on a set, so a blank one is refused here
    rather than creating an unclickable row. Uniqueness per user is enforced by
    the viewset (it knows the requesting user; this serializer does not).
    """

    name = serializers.CharField(max_length=100, allow_blank=False, trim_whitespace=True)


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


class HiddenChassisToggleSerializer(serializers.Serializer):
    """Body for POST .../hidden-design-chassis/toggle/ (chassis layer view state)."""

    design_id = serializers.IntegerField()
    chassis_id = serializers.IntegerField()


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


class CopyFeedsSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/copy-feeds/ (clone a rack's feeds as
    planned feeds onto another rack)."""

    rack_id = serializers.IntegerField()
    source_rack_id = serializers.IntegerField()


class PlannedFeedDeleteSerializer(serializers.Serializer):
    """Body for DELETE .../designs/<pk>/planned-feed/.

    Addressed either by row id or by the natural key the dialog knows
    (``rack_id`` + ``name``), so a caller holding one or the other need not look
    the feed up first.
    """

    feed_id = serializers.IntegerField(required=False)
    rack_id = serializers.IntegerField(required=False)
    name = serializers.CharField(max_length=100, required=False)

    def validate(self, attrs):
        if attrs.get("feed_id") is None and not (
            attrs.get("rack_id") is not None and attrs.get("name")
        ):
            raise serializers.ValidationError(
                "Provide feed_id, or both rack_id and name.")
        return attrs


class PlannedFeedUpsertSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/planned-feed/ (upsert by rack+name)."""

    rack_id = serializers.IntegerField()
    name = serializers.CharField(max_length=100)
    voltage = serializers.IntegerField(required=False)
    amperage = serializers.IntegerField(required=False)
    phase = serializers.ChoiceField(choices=PowerFeedPhaseChoices, required=False)
    supply = serializers.ChoiceField(choices=PowerFeedSupplyChoices, required=False)


# ---------------------------------------------------------------------------
# Design-chain request serializer (PLAN-design-chains.md §5 phase 1 / G9)
#
# Validates only the shape of the POST body for .../designs/<pk>/rebase/. The
# viewset re-points ``based_on`` and runs the model's own ``full_clean()`` --
# same cycle guard and same site check the HTML DesignRebaseView reuses --
# rather than re-implementing either here.
# ---------------------------------------------------------------------------


class DesignRebaseSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/rebase/."""

    based_on = serializers.IntegerField(
        help_text="PK of the new base design. Must be APPROVED (§2.2).",
    )
