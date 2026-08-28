"""REST API URL routing for NetBox Rack Design."""

from netbox.api.routers import NetBoxRouter

from .views import (
    DesignGroupViewSet,
    DesignPlacementViewSet,
    DesignPowerFeedViewSet,
    DesignViewSet,
    DeviceTypePowerViewSet,
    FavoriteDeviceTypeViewSet,
    FavoriteSetViewSet,
    HiddenDesignChassisViewSet,
    HiddenDesignRackViewSet,
)

app_name = "netbox_rack_design"

router = NetBoxRouter()
router.register("design-groups", DesignGroupViewSet)
router.register("designs", DesignViewSet)
router.register("placements", DesignPlacementViewSet)
router.register("planned-power-feeds", DesignPowerFeedViewSet)
router.register(
    "favorite-device-types",
    FavoriteDeviceTypeViewSet,
    basename="favoritedevicetype",
)
router.register(
    "favorite-sets",
    FavoriteSetViewSet,
    basename="favoriteset",
)
router.register(
    "hidden-design-chassis",
    HiddenDesignChassisViewSet,
    basename="hiddendesignchassis",
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
