"""
Views for NetBox Rack Design.

For more information on NetBox views, see:
https://docs.netbox.dev/en/stable/plugins/development/views/

For generic view classes, see:
https://docs.netbox.dev/en/stable/development/views/
"""

from netbox.views import generic

from . import filtersets, forms, models, tables


class RackdesignView(generic.ObjectView):
    queryset = models.Rackdesign.objects.all()


class RackdesignListView(generic.ObjectListView):
    queryset = models.Rackdesign.objects.all()
    table = tables.RackdesignTable
    filterset = filtersets.RackdesignFilterSet


class RackdesignEditView(generic.ObjectEditView):
    queryset = models.Rackdesign.objects.all()
    form = forms.RackdesignForm


class RackdesignDeleteView(generic.ObjectDeleteView):
    queryset = models.Rackdesign.objects.all()
