"""
API serializers for NetBox Rack Design.

Serializers are required for NetBox event handling (webhooks, change logging).
They also power the REST API endpoints.

For more information on NetBox REST API serializers, see:
https://docs.netbox.dev/en/stable/plugins/development/rest-api/#serializers

For Django REST Framework serializers, see:
https://www.django-rest-framework.org/api-guide/serializers/
"""

from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import Rackdesign


class RackdesignSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_rack_design-api:rackdesign-detail"
    )

    class Meta:
        model = Rackdesign
        fields = ("id", "url", "display", "name", "tags", "custom_fields", "created", "last_updated")
