"""REST API URL routing for NetBox Rack Design."""

from netbox.api.routers import NetBoxRouter

from .views import (
    DesignGroupViewSet,
    DesignPlacementViewSet,
    DesignViewSet,
    FavoriteDeviceTypeViewSet,
)

app_name = "netbox_rack_design"

router = NetBoxRouter()
router.register("design-groups", DesignGroupViewSet)
router.register("designs", DesignViewSet)
router.register("placements", DesignPlacementViewSet)
router.register(
    "favorite-device-types",
    FavoriteDeviceTypeViewSet,
    basename="favoritedevicetype",
)

urlpatterns = router.urls
