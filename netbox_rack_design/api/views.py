"""
API viewsets for NetBox Rack Design.

For more information on NetBox REST API viewsets, see:
https://docs.netbox.dev/en/stable/plugins/development/rest-api/#viewsets

For Django REST Framework viewsets, see:
https://www.django-rest-framework.org/api-guide/viewsets/
"""

from netbox.api.viewsets import NetBoxModelViewSet

from ..models import Rackdesign
from .serializers import RackdesignSerializer


class RackdesignViewSet(NetBoxModelViewSet):
    queryset = Rackdesign.objects.all()
    serializer_class = RackdesignSerializer

