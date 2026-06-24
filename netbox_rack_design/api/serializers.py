"""REST API serializers for NetBox Rack Design."""

from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import Design, DesignGroup, DesignPlacement

__all__ = ("DesignGroupSerializer", "DesignSerializer", "DesignPlacementSerializer")


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

    class Meta:
        model = Design
        fields = (
            "id", "url", "display", "title", "site", "status", "summary", "link",
            "version", "root", "based_on", "sequence", "depends_on", "group",
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
            "proposed_name", "target_rack", "target_position", "target_face",
            "tags", "custom_fields", "created", "last_updated",
        )
        brief_fields = ("id", "url", "display", "kind")
