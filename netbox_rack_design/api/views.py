"""REST API viewsets for NetBox Rack Design."""

from netbox.api.viewsets import NetBoxModelViewSet

from .. import filtersets
from ..models import Design, DesignGroup, DesignPlacement
from .serializers import DesignGroupSerializer, DesignPlacementSerializer, DesignSerializer

__all__ = ("DesignGroupViewSet", "DesignViewSet", "DesignPlacementViewSet")


class DesignGroupViewSet(NetBoxModelViewSet):
    queryset = DesignGroup.objects.all()
    serializer_class = DesignGroupSerializer
    filterset_class = filtersets.DesignGroupFilterSet


class DesignViewSet(NetBoxModelViewSet):
    queryset = Design.objects.prefetch_related("placements", "depends_on", "tags")
    serializer_class = DesignSerializer
    filterset_class = filtersets.DesignFilterSet


class DesignPlacementViewSet(NetBoxModelViewSet):
    queryset = DesignPlacement.objects.all()
    serializer_class = DesignPlacementSerializer
    filterset_class = filtersets.DesignPlacementFilterSet
