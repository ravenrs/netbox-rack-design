"""
URL patterns for NetBox Rack Design.

For more information on URL routing, see:
https://docs.netbox.dev/en/stable/plugins/development/views/#url-registration

For Django URL patterns, see:
https://docs.djangoproject.com/en/stable/topics/http/urls/
"""

from django.urls import path
from netbox.views.generic import ObjectChangeLogView

from . import models, views

urlpatterns = (
    path("rack-designs/", views.RackdesignListView.as_view(), name="rackdesign_list"),
    path("rack-designs/add/", views.RackdesignEditView.as_view(), name="rackdesign_add"),
    path("rack-designs/<int:pk>/", views.RackdesignView.as_view(), name="rackdesign"),
    path("rack-designs/<int:pk>/edit/", views.RackdesignEditView.as_view(), name="rackdesign_edit"),
    path("rack-designs/<int:pk>/delete/", views.RackdesignDeleteView.as_view(), name="rackdesign_delete"),
    path(
        "rack-designs/<int:pk>/changelog/",
        ObjectChangeLogView.as_view(),
        name="rackdesign_changelog",
        kwargs={"model": models.Rackdesign},
    ),
)
