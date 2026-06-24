"""URL patterns for NetBox Rack Design."""

from django.urls import include, path
from utilities.urls import get_model_urls

from . import views  # noqa: F401  (import registers @register_model_view views)

app_name = "netbox_rack_design"

urlpatterns = (
    path("groups/", include(get_model_urls("netbox_rack_design", "designgroup", detail=False))),
    path("groups/<int:pk>/", include(get_model_urls("netbox_rack_design", "designgroup"))),
    path("designs/", include(get_model_urls("netbox_rack_design", "design", detail=False))),
    path("designs/<int:pk>/", include(get_model_urls("netbox_rack_design", "design"))),
    path("placements/", include(get_model_urls("netbox_rack_design", "designplacement", detail=False))),
    path("placements/<int:pk>/", include(get_model_urls("netbox_rack_design", "designplacement"))),
)
