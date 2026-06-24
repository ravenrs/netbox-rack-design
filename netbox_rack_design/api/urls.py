"""REST API URL routing for NetBox Rack Design."""

from netbox.api.routers import NetBoxRouter

from .views import DesignGroupViewSet, DesignPlacementViewSet, DesignViewSet

app_name = "netbox_rack_design"

router = NetBoxRouter()
router.register("design-groups", DesignGroupViewSet)
router.register("designs", DesignViewSet)
router.register("placements", DesignPlacementViewSet)

urlpatterns = router.urls
