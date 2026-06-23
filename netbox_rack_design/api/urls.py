"""
API URL patterns for NetBox Rack Design.

For more information on NetBox REST API routing, see:
https://docs.netbox.dev/en/stable/plugins/development/rest-api/#routers

For Django REST Framework routers, see:
https://www.django-rest-framework.org/api-guide/routers/
"""

from netbox.api.routers import NetBoxRouter

from .views import RackdesignViewSet

app_name = "netbox_rack_design"

router = NetBoxRouter()
router.register("rack-designs", RackdesignViewSet)

urlpatterns = router.urls

