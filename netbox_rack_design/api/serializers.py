"""REST API serializers for NetBox Rack Design."""

from dcim.api.serializers import (
    DeviceBaySerializer,
    DeviceRoleSerializer,
    DeviceSerializer,
    DeviceTypeSerializer,
    LocationSerializer,
    PowerPanelSerializer,
    RackSerializer,
    SiteSerializer,
)
from dcim.choices import PowerFeedPhaseChoices, PowerFeedSupplyChoices
from dcim.models import Rack, Site
from netbox.api.fields import SerializedPKRelatedField
from netbox.api.serializers import BaseModelSerializer, NetBoxModelSerializer, WritableNestedSerializer
from rest_framework import serializers
from tenancy.api.serializers import TenantSerializer
from users.api.serializers import UserSerializer

from ..models import (
    Design,
    DesignApply,
    DesignGroup,
    DesignPlacement,
    DesignPowerFeed,
    DesignStep,
    PlannedRack,
    Template,
    TemplateGroup,
    TemplatePlacement,
)

__all__ = (
    "NestedDesignGroupSerializer",
    "NestedDesignSerializer",
    "NestedDesignPlacementSerializer",
    "DesignGroupSerializer",
    "DesignSerializer",
    "DesignPlacementSerializer",
    "DesignStepSerializer",
    "PlannedRackSerializer",
    "DesignPowerFeedSerializer",
    "DesignApplySerializer",
    "TemplateGroupSerializer",
    "NestedTemplateGroupSerializer",
    "TemplateSerializer",
    "NestedTemplateSerializer",
    "TemplatePlacementSerializer",
    "NestedTemplatePlacementSerializer",
    "SaveLayoutSerializer",
    "RecomputeDistributionSerializer",
    "SimulateStepsSerializer",
    "PreviewTemplateSerializer",
    "PreviewNameSerializer",
    "FavoriteSetWriteSerializer",
    "FavoriteToggleSerializer",
    "DesignRackScopeSerializer",
    "CreatePlannedRackSerializer",
    "ExtractTemplateFromDesignSerializer",
    "ExtractTemplateFromRackSerializer",
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


class PlannedRackSerializer(NetBoxModelSerializer):
    """A rack that does not exist in DCIM yet (PLAN-templates.md D3/D6/T1.7).

    Mirrors ``DesignPowerFeedSerializer``'s shape -- the closest existing
    precedent for a plugin ``NetBoxModel`` with its own list/detail API.
    ``realized_rack`` is exposed read/write like any other nested FK: D7 lets
    ``Apply`` set it once the plan is realized, but nothing stops a client
    from linking an already-realized rack by hand (e.g. backfilling history).
    """

    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:plannedrack-detail"
    )
    location = LocationSerializer(nested=True)
    realized_rack = RackSerializer(nested=True, required=False, allow_null=True)
    is_realized = serializers.BooleanField(read_only=True)

    class Meta:
        model = PlannedRack
        fields = (
            "id", "url", "display", "name", "u_height", "location", "realized_rack",
            "is_realized", "description", "comments", "tags", "custom_fields",
            "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


# ---------------------------------------------------------------------------
# Templates (PLAN-templates.md §2, task T2.1) -- a reusable rack layout with
# no site. Stamping (Phase 3) is not implemented here.
# ---------------------------------------------------------------------------


class NestedTemplateGroupSerializer(WritableNestedSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:templategroup-detail"
    )

    class Meta:
        model = TemplateGroup
        fields = ("id", "url", "display", "name")


class TemplateGroupSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:templategroup-detail"
    )

    class Meta:
        model = TemplateGroup
        fields = (
            "id", "url", "display", "name", "description",
            "tags", "custom_fields", "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class NestedTemplateSerializer(WritableNestedSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:template-detail"
    )

    class Meta:
        model = Template
        fields = ("id", "url", "display", "name")


class TemplateSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:template-detail"
    )
    group = NestedTemplateGroupSerializer(required=False, allow_null=True)
    # Read-only: bumped by TemplatePlacement.save()/delete(), never set by a
    # client. Exposed because it is half of D20's provenance -- a placement
    # records the version it was stamped from, and the only way to tell that a
    # template has DRIFTED since is to compare that against this.
    version = serializers.IntegerField(read_only=True)

    class Meta:
        model = Template
        fields = (
            "id", "url", "display", "name", "group", "order", "u_height",
            "version", "description", "tags", "custom_fields", "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class NestedTemplatePlacementSerializer(WritableNestedSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:templateplacement-detail"
    )

    class Meta:
        model = TemplatePlacement
        fields = ("id", "url", "display")


class TemplatePlacementSerializer(NetBoxModelSerializer):
    """
    A single device within a ``Template`` (PLAN-templates.md §2, D8/D9):
    everything a ``kind=add`` ``DesignPlacement`` carries except the rack, the
    absolute position and power -- see the model's own docstring. No name
    field exists at all: the naming engine assigns names at stamp time
    (Phase 3, not implemented here), never before.
    """

    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:templateplacement-detail"
    )
    template = NestedTemplateSerializer()
    device_type = DeviceTypeSerializer(nested=True)
    device_role = DeviceRoleSerializer(nested=True, required=False, allow_null=True)
    tenant = TenantSerializer(nested=True, required=False, allow_null=True)
    parent_placement = NestedTemplatePlacementSerializer(required=False, allow_null=True)

    class Meta:
        model = TemplatePlacement
        fields = (
            "id", "url", "display", "template", "device_type", "device_role",
            "tenant", "planning_data", "face", "anchor", "offset", "order", "label",
            "parent_placement", "target_bay_name",
            "tags", "custom_fields", "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "device_type")


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
    # The planned-rack counterpart of ``racks`` above (PLAN-templates.md T1.3):
    # same writable-nested shape, over the SEPARATE ``PlannedRack`` pk space
    # (D28) -- a pk here is never confused with a ``racks`` pk because they
    # round-trip through different querysets/serializers entirely.
    planned_racks = SerializedPKRelatedField(
        queryset=PlannedRack.objects.all(),
        serializer=PlannedRackSerializer,
        nested=True,
        required=False,
        many=True,
    )
    # M9 (PLAN-multi-site.md): a design covers one or more sites. Writable-M2M
    # nested (same pattern as ``racks``/``depends_on`` above): brief Site
    # representations on read, a list of Site PKs on write. ``Design.clean()``
    # is the real "at least one" enforcement point (M1); the serializer also
    # rejects an empty list itself so an API client gets a clear 400 rather
    # than discovering the model-level error only after a save attempt.
    sites = SerializedPKRelatedField(
        queryset=Site.objects.all(),
        serializer=SiteSerializer,
        nested=True,
        many=True,
    )
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
            "id", "url", "display", "title", "sites", "status", "summary", "link",
            "version", "root", "based_on", "sequence", "depends_on", "racks",
            "planned_racks", "group",
            "description", "comments", "is_frozen", "tags", "custom_fields",
            "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "title", "version", "status")

    def validate(self, data):
        # M1 (PLAN-multi-site.md): at least one site is required. ``Design.clean()``
        # enforces this too, but a plain ``ModelSerializer`` for an M2M field does
        # not run the model's ``clean()`` by default the way it does for scalar
        # fields, so this is the actual guard for an API write; the model-level
        # check remains for non-API callers (management commands, scripts).
        sites = data.get("sites")
        if sites is not None and len(sites) == 0:
            raise serializers.ValidationError(
                {"sites": "A design must have at least one site."}
            )
        return data


class NestedDesignPlacementSerializer(WritableNestedSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:designplacement-detail"
    )

    class Meta:
        model = DesignPlacement
        fields = ("id", "url", "display", "kind")


class DesignStepSerializer(NetBoxModelSerializer):
    """One step of a design's execution plan (PLAN-execution-steps.md Sec. 3).
    """

    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:designstep-detail"
    )
    design = NestedDesignSerializer()

    class Meta:
        model = DesignStep
        fields = (
            "id", "url", "display", "design", "index", "title",
            "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "index", "title")


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
    # The planned-rack counterpart of ``target_rack`` (PLAN-templates.md T1.2):
    # a device may be planned into a rack that does not exist in DCIM yet. See
    # ``PlannedRack``'s docstring for why this is a separate FK rather than a
    # GenericForeignKey -- this nested serializer is exactly the reason.
    target_planned_rack = PlannedRackSerializer(nested=True, required=False, allow_null=True)
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
    # Provenance (PLAN-templates.md §3, D20): which Template this placement was
    # stamped from, if any, and the template's ``version`` at that moment.
    # Nested like target_planned_rack above -- a raw pk on write, a nested
    # object on read. Populated by save-layout (T3.5) from the
    # from_template_id/from_template_version the editor echoes back on an
    # 'add' item, having gotten them from this same design's own
    # preview-template call.
    from_template = NestedTemplateSerializer(required=False, allow_null=True)
    # The execution-plan step (PLAN-execution-steps.md Sec. 3): a raw pk on
    # write, a nested brief on read; null = unscheduled.
    step = DesignStepSerializer(nested=True, required=False, allow_null=True)

    class Meta:
        model = DesignPlacement
        fields = (
            "id", "url", "display", "design", "kind", "device", "device_type",
            "proposed_name", "device_role", "tenant",
            "target_rack", "target_planned_rack", "target_position", "target_face",
            "parent_placement", "target_bay", "target_bay_name",
            "base_placement", "base_parent_placement",
            "planning_data", "preferred_feed_legs", "stale", "stale_device_name",
            "from_template", "from_template_version", "step", "step_order",
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
    # Nullable on both sides (D25/D26): a planned feed lives in EITHER a real
    # rack or a planned one, never both/neither (model.clean() enforces
    # exactly-one). Was previously required=True here, which predates
    # ``planned_rack`` -- see the model's own docstring for why ``rack``
    # became nullable in the first place.
    rack = RackSerializer(nested=True, required=False, allow_null=True)
    planned_rack = PlannedRackSerializer(nested=True, required=False, allow_null=True)
    # Where Apply hangs the real dcim.PowerFeed (see the model field).
    power_panel = PowerPanelSerializer(nested=True, required=False, allow_null=True)
    derated_watts = serializers.IntegerField(read_only=True)

    class Meta:
        model = DesignPowerFeed
        fields = (
            "id", "url", "display", "design", "rack", "planned_rack", "name",
            "power_panel", "voltage", "amperage", "phase", "supply", "derated_watts",
            "tags", "custom_fields", "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class DesignApplySerializer(BaseModelSerializer):
    """
    One apply run's record of a placement materialized as a real device
    (models.py ``DesignApply``, ``apply.py``) -- read-only, see
    ``DesignApplyViewSet`` (api/views.py) for why.

    ``DesignApply`` is a plain ``models.Model``, not a ``NetBoxModel``: it has
    no tags/custom fields/changelog of its own (models.py docstring), so this
    subclasses ``BaseModelSerializer`` directly rather than
    ``NetBoxModelSerializer`` -- pulling in the tag/custom-field/changelog
    mixins here would offer features the model cannot back.

    ``design_title`` and ``device_name`` are included alongside the live
    ``design``/``device`` FKs, not instead of them: they are the snapshot
    ``DesignApply.snapshot_names()`` freezes on every save, and the only
    readable identity once that FK goes null (all three FKs are SET_NULL).
    A polling client should never have to fetch a related object just to
    learn a name that might not exist by the time it asks.
    """

    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:designapply-detail"
    )
    design = NestedDesignSerializer(required=False, allow_null=True)
    placement = NestedDesignPlacementSerializer(required=False, allow_null=True)
    device = DeviceSerializer(nested=True, required=False, allow_null=True)
    applied_by = UserSerializer(nested=True, required=False, allow_null=True)

    class Meta:
        model = DesignApply
        fields = (
            "id", "url", "display", "design", "design_title", "placement",
            "device", "device_name", "prior_device_status", "applied_by",
            "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "design_title", "device_name")


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
    # An operator override of which feed leg(s) this device's PSUs draw from
    # (docs/pdu-distribution-spec.md), for the power projection only --
    # e.g. ``["b"]`` or ``["a", "b"]``. WITHOUT a default so an item that
    # omits the key leaves the placement's stored override alone; an explicit
    # ``null``/``[]`` clears it back to the engine's automatic heuristic.
    preferred_feed_legs = serializers.JSONField(required=False, allow_null=True)
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
    # Editor session action counter (PLAN-execution-steps.md E6): the order the
    # user performed the action in. save-layout creates NEW placements in
    # ascending action_seq across all racks so ``created`` follows it.
    action_seq = serializers.IntegerField(required=False, allow_null=True, min_value=0)
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
    # --- template provenance (PLAN-templates.md D20) ------------------------
    # Which Template (and which Template.version) a brand-new 'add' was
    # stamped from -- ``preview-template`` (T3.4) returns both on every item
    # it computes, and the editor echoes them back verbatim on the matching
    # save-layout 'add' item so the placement records where it came from.
    # WITHOUT a default, like the other add-only fields above: an ordinary
    # hand-placed device simply omits them, and the viewset leaves
    # ``from_template``/``from_template_version`` null rather than inventing
    # zeroes. Suffixed ``_id`` to match ``device_role_id``/``tenant_id`` --
    # this is the id an 'add' item carries in, not the model's own FK name.
    from_template_id = serializers.IntegerField(required=False, allow_null=True)
    from_template_version = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, data):
        kind = data["kind"]
        if kind != "add" and (
            data.get("from_template_id") is not None
            or data.get("from_template_version") is not None
        ):
            # Provenance is meaningful only for an 'add' -- it records where a
            # NEW planned identity came from, and a move/remove acts on an
            # identity that already exists. Rejected here, at the request
            # boundary, rather than only downstream in
            # DesignPlacement.clean() (which also enforces it, for any other
            # caller that builds a placement directly): a save-layout client
            # sending this on a move/remove is confused about what the field
            # means, and that deserves an explicit 400, not a value that is
            # silently ignored because the move/remove branch never reads it.
            raise serializers.ValidationError({
                "from_template_id": f"A '{kind}' item must not carry template "
                                     f"provenance -- it only applies to an 'add'.",
            })
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
    """One rack's desired contents, split by face plus an off-rack 'other' bucket.

    ``rack_id`` is a ``CharField`` rather than ``IntegerField`` (T1.4d) so it
    accepts EITHER the legacy bare integer (a real ``dcim.Rack`` pk, kept
    working for one release) or the namespaced ``models.rack_key()`` form
    (``"r:<pk>"``/``"p:<pk>"``, PLAN-templates.md D27). DRF's ``CharField``
    already coerces a JSON number to its string form, so an in-flight request
    from an older editor tab (which only ever sends a plain int) keeps
    working unchanged. The viewset parses the result via
    ``parse_real_rack_id``/``parse_rack_id``.
    """

    rack_id = serializers.CharField(max_length=32)
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

    # CharField child (T1.4d) -- see SaveLayoutRackSerializer's rack_id
    # docstring; the viewset parses each entry via parse_rack_id.
    project_racks = serializers.ListField(
        child=serializers.CharField(max_length=32), required=False, default=list
    )


class SaveStepsStepSerializer(serializers.Serializer):
    """One step of the layout in a save-steps body: ``id`` is the existing
    ``DesignStep`` pk (null = create), ``placements`` the ordered placement ids."""

    id = serializers.IntegerField(min_value=1, required=False, allow_null=True, default=None)
    title = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    placements = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=True, required=False, default=list
    )


class SaveStepsSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/save-steps/ (PLAN-execution-steps.md Sec. 6):
    the design's WHOLE step layout, which replaces what is stored."""

    steps = serializers.ListField(child=SaveStepsStepSerializer(), allow_empty=True)


class AutoOrderSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/auto-order/: the current order, a list of
    steps, each a list of placement ids."""

    steps = serializers.ListField(
        child=serializers.ListField(child=serializers.IntegerField(min_value=1)),
        allow_empty=True,
    )


class SimulateStepsSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/simulate-steps/ (PLAN-execution-steps.md
    Sec. 4): a PROPOSED order -- ``steps`` is a list of steps, each a list of
    placement ids -- plus optional limits. ``from_step``/``to_step`` (1-based,
    inclusive) bound which steps are computed; ``racks`` (namespaced
    ``"r:<pk>"``/``"p:<pk>"`` keys, parsed by the viewset) bounds which racks
    are reported. Whether the ids belong to the design is checked by
    ``steps.validate_steps``."""

    steps = serializers.ListField(
        child=serializers.ListField(child=serializers.IntegerField(min_value=1)),
        allow_empty=True,
    )
    from_step = serializers.IntegerField(min_value=1, required=False)
    to_step = serializers.IntegerField(min_value=1, required=False)
    racks = serializers.ListField(
        child=serializers.CharField(max_length=32), required=False, allow_null=True
    )

    def validate(self, attrs):
        lo, hi = attrs.get("from_step"), attrs.get("to_step")
        if lo is not None and hi is not None and lo > hi:
            raise serializers.ValidationError({"from_step": "from_step must not exceed to_step."})
        return attrs


class PreviewTemplateSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/preview-template/ (PLAN-templates.md §3,
    T3.2; ``group`` added in T3.6).

    ``racks`` uses the NAMESPACED rack keys from ``models.rack_key()``
    (D27): ``"r:<pk>"`` for a real ``dcim.Rack``, ``"p:<pk>"`` for a
    ``PlannedRack``. This is a brand-new endpoint, so it is the first (and
    for now only) place that format is required on the way IN -- the
    viewset itself validates and reports a malformed key rather than
    leaving django-filter/DRF to produce an opaque 400 (or, worse, silently
    coerce a value that happens to look numeric).

    Exactly ONE of ``template`` / ``group`` is required (``validate()``
    below). The two give ``racks`` different meanings, matching D14/D24:

    - ``template`` -- REPETITION. Every listed rack gets the SAME content
      (a single ToR dropped on several racks, or the single-template
      checklist dialog's "which racks" selection).
    - ``group`` -- CORRESPONDENCE. ``racks`` is POSITIONAL against the
      group's ordered ``Template`` members (member 1 -> ``racks[0]``,
      member 2 -> ``racks[1]``, ...), never repeated content. See the
      viewset action's docstring for what happens when ``racks`` is
      SHORTER than the group's member count (PLAN-templates.md §6 "Still
      open": "Group apply when there are fewer target racks than
      members" -- T3.6 closes this by refusing the whole apply rather than
      landing part of the pod).

    ``layout`` is the editor's current (possibly unsaved) ``save-layout``
    body, reused verbatim -- D19's whole reason for existing server-side is
    to see exactly this. Optional: a caller previewing against already-saved
    state omits it entirely, exactly like ``recompute-distribution``'s own
    ``project_racks``-less full-refresh case.
    """

    template = serializers.IntegerField(required=False)
    group = serializers.IntegerField(required=False)
    racks = serializers.ListField(
        child=serializers.CharField(max_length=32), allow_empty=False,
    )
    layout = SaveLayoutSerializer(required=False)

    def validate(self, attrs):
        has_template = "template" in attrs
        has_group = "group" in attrs
        if has_template == has_group:  # both set, or neither
            raise serializers.ValidationError(
                "Provide exactly one of 'template' or 'group'."
            )
        return attrs


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
    # A rack KEY, not strictly a pk: the editor sends what it drew the tile
    # in -- a bare pk (or "r:<pk>") for a real rack, "p:<pk>" for a planned
    # one (D28/D31, frame.js serverRackId). A planned rack names its tiles
    # like any other, so this must accept both spellings.
    target_rack = serializers.CharField(required=False, allow_null=True)
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
    # The planning values the tile carries (the rail's, for a fresh drop), so
    # a naming template can read {device.cf[...]} before anything is saved.
    planning_data = serializers.DictField(required=False, allow_null=True)


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
    """Body for POST .../designs/<pk>/add-rack/ and .../remove-rack/.

    ``rack_id`` is a rack KEY, not strictly an integer: a bare pk (or
    ``"r:<pk>"``) is a real ``dcim.Rack``, ``"p:<pk>"`` a ``PlannedRack``
    (``parse_rack_id``, D28). remove-rack accepts both -- a planned rack has
    to be detachable from a design, since deleting one is refused while any
    design still plans across it. add-rack resolves real racks only.
    """

    rack_id = serializers.CharField()
    # remove-rack only: must be true to confirm a destructive removal when the
    # rack still has planned placements targeting it. Ignored by add-rack.
    confirm = serializers.BooleanField(required=False, default=False)


class CreatePlannedRackSerializer(serializers.Serializer):
    """
    Body for POST .../designs/<pk>/create-planned-rack/ (PLAN-templates.md
    §1, the editor's "Create rack" dialog). Deliberately a plain input
    serializer, not ``PlannedRackSerializer`` itself: the dialog only ever
    collects the fields a planner types (name/height/location/optional copy
    source), never ``realized_rack`` or the NetBoxModel bookkeeping fields,
    and reusing the model serializer here would let a client set those by
    accident.

    ``name`` accepts NetBox's own bracketed pattern syntax (``R[1-4]``,
    ``utilities.forms.utils.expand_alphanumeric_pattern`` -- the same helper
    interface creation uses), expanded by the view into one rack per
    resulting name; a name with no brackets still names exactly one rack.

    ``copy_feeds_from_rack_id`` is optional (the "copy feeds while
    creating" half of this action): a rack key (``models.rack_key()`` form,
    or a legacy bare pk for a real rack) this design may clone feeds from
    for EVERY rack this request creates. Omitted or null means no feeds are
    copied, exactly as before this field existed.
    """

    name = serializers.CharField(max_length=100)
    u_height = serializers.IntegerField()
    location_id = serializers.IntegerField()
    copy_feeds_from_rack_id = serializers.CharField(
        max_length=32, required=False, allow_null=True, allow_blank=True, default=None
    )


class ExtractTemplateFromDesignSerializer(serializers.Serializer):
    """
    Body for POST .../templates/from-design/ (PLAN-templates.md Sec 4, D18/
    D32, T4.1): build a new ``Template`` from what a design's rack will LOOK
    LIKE under that design -- its projected state, not merely this design's
    own ``kind=add`` placements.

    ``rack`` uses the SAME namespaced key ``models.rack_key()`` defines
    (D27) that ``preview-template``/``recompute-distribution`` already speak:
    ``"r:<pk>"`` for a real ``dcim.Rack``, ``"p:<pk>"`` for a ``PlannedRack``.
    A ``"p:<pk>"`` value is well-formed here (it resolves to a real row) but
    is refused by the viewset with a 400 (D32: a planned rack has no devices
    by definition, so extracting from one could only ever produce an empty
    template).
    """

    design = serializers.IntegerField()
    rack = serializers.CharField(max_length=32)
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    group_id = serializers.IntegerField(required=False, allow_null=True, default=None)


class ExtractTemplateFromRackSerializer(serializers.Serializer):
    """
    Body for POST .../templates/from-rack/ (PLAN-templates.md Sec 4, D18,
    T4.1): build a new ``Template`` from a real ``dcim.Rack``'s actual
    devices, reading ``device.role``/``device.tenant``. Always a plain
    ``dcim.Rack`` pk -- a ``PlannedRack`` is never addressable through this
    action (it has no devices at all, ``rackinfo.rack_devices`` returns
    ``Device.objects.none()`` for one), so there is no namespaced-key
    ambiguity to resolve here the way ``from-design`` has to.
    """

    rack_id = serializers.IntegerField()
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    group_id = serializers.IntegerField(required=False, allow_null=True, default=None)


class HiddenRackToggleSerializer(serializers.Serializer):
    """Body for POST .../hidden-design-racks/toggle/ (per-user view state)."""

    design_id = serializers.IntegerField()
    rack_id = serializers.IntegerField(required=False)
    planned_rack_id = serializers.IntegerField(required=False)

    def validate(self, data):
        if ("rack_id" in data) == ("planned_rack_id" in data):
            raise serializers.ValidationError(
                "Give exactly one of rack_id or planned_rack_id.")
        return data


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
    """Body for POST .../designs/<pk>/rack-power/.

    ``rack_id`` is a ``CharField`` rather than ``IntegerField`` (T1.4d) so it
    accepts EITHER the legacy bare integer or the namespaced
    ``models.rack_key()`` form (``"r:<pk>"``/``"p:<pk>"``) -- DRF's
    ``CharField`` already coerces a JSON number to its string form, so an
    older client sending a plain int keeps working unchanged. The viewset
    parses the result via ``parse_real_rack_id``.
    """

    rack_id = serializers.CharField(max_length=32)
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
    planned feeds onto another rack).

    Both ids are ``CharField`` (T1.4d) -- see ``RackPowerSerializer``'s
    ``rack_id`` docstring for why.
    """

    rack_id = serializers.CharField(max_length=32)
    source_rack_id = serializers.CharField(max_length=32)


class PlannedFeedDeleteSerializer(serializers.Serializer):
    """Body for DELETE .../designs/<pk>/planned-feed/.

    Addressed either by row id or by the natural key the dialog knows
    (``rack_id`` + ``name``), so a caller holding one or the other need not look
    the feed up first.
    """

    feed_id = serializers.IntegerField(required=False)
    # CharField (T1.4d) -- see RackPowerSerializer's rack_id docstring.
    rack_id = serializers.CharField(max_length=32, required=False)
    name = serializers.CharField(max_length=100, required=False)

    def validate(self, attrs):
        if attrs.get("feed_id") is None and not (
            attrs.get("rack_id") is not None and attrs.get("name")
        ):
            raise serializers.ValidationError(
                "Provide feed_id, or both rack_id and name.")
        return attrs


class PlannedFeedUpsertSerializer(serializers.Serializer):
    """Body for POST .../designs/<pk>/planned-feed/ (upsert by rack+name).

    ``rack_id`` is a ``CharField`` (T1.4d) -- see ``RackPowerSerializer``'s
    ``rack_id`` docstring.
    """

    rack_id = serializers.CharField(max_length=32)
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
