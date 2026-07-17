"""REST API URL routing for NetBox Rack Design."""

from netbox.api.routers import NetBoxRouter

from .views import (
    DesignGroupViewSet,
    DesignPlacementViewSet,
    DesignViewSet,
    DeviceTypePowerViewSet,
    FavoriteDeviceTypeViewSet,
    HiddenDesignRackViewSet,
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
router.register(
    "hidden-design-racks",
    HiddenDesignRackViewSet,
    basename="hiddendesignrack",
)
router.register(
    "device-type-power",
    DeviceTypePowerViewSet,
    basename="devicetypepower",
)

urlpatterns = router.urls
