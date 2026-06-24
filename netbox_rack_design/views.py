"""Views for NetBox Rack Design."""

from netbox.views import generic
from utilities.views import register_model_view

from . import filtersets, forms, models, tables

__all__ = (
    "DesignGroupView", "DesignGroupListView", "DesignGroupEditView", "DesignGroupDeleteView",
    "DesignView", "DesignListView", "DesignEditView", "DesignDeleteView",
    "DesignPlacementView", "DesignPlacementListView", "DesignPlacementEditView", "DesignPlacementDeleteView",
)


# ---------------------------------------------------------------------------
# DesignGroup
# ---------------------------------------------------------------------------


@register_model_view(models.DesignGroup)
class DesignGroupView(generic.ObjectView):
    queryset = models.DesignGroup.objects.all()


@register_model_view(models.DesignGroup, "list", path="", detail=False)
class DesignGroupListView(generic.ObjectListView):
    queryset = models.DesignGroup.objects.all()
    table = tables.DesignGroupTable
    filterset = filtersets.DesignGroupFilterSet
    filterset_form = forms.DesignGroupFilterForm


@register_model_view(models.DesignGroup, "add", detail=False)
@register_model_view(models.DesignGroup, "edit")
class DesignGroupEditView(generic.ObjectEditView):
    queryset = models.DesignGroup.objects.all()
    form = forms.DesignGroupForm


@register_model_view(models.DesignGroup, "delete")
class DesignGroupDeleteView(generic.ObjectDeleteView):
    queryset = models.DesignGroup.objects.all()


# ---------------------------------------------------------------------------
# Design
# ---------------------------------------------------------------------------


@register_model_view(models.Design)
class DesignView(generic.ObjectView):
    queryset = models.Design.objects.all()

    def get_extra_context(self, request, instance):
        placements = instance.placements.all()
        table = tables.DesignPlacementTable(placements)
        table.configure(request)
        return {"placement_table": table}


@register_model_view(models.Design, "list", path="", detail=False)
class DesignListView(generic.ObjectListView):
    queryset = models.Design.objects.all()
    table = tables.DesignTable
    filterset = filtersets.DesignFilterSet
    filterset_form = forms.DesignFilterForm


@register_model_view(models.Design, "add", detail=False)
@register_model_view(models.Design, "edit")
class DesignEditView(generic.ObjectEditView):
    queryset = models.Design.objects.all()
    form = forms.DesignForm


@register_model_view(models.Design, "delete")
class DesignDeleteView(generic.ObjectDeleteView):
    queryset = models.Design.objects.all()


# ---------------------------------------------------------------------------
# DesignPlacement
# ---------------------------------------------------------------------------


@register_model_view(models.DesignPlacement)
class DesignPlacementView(generic.ObjectView):
    queryset = models.DesignPlacement.objects.all()


@register_model_view(models.DesignPlacement, "list", path="", detail=False)
class DesignPlacementListView(generic.ObjectListView):
    queryset = models.DesignPlacement.objects.all()
    table = tables.DesignPlacementTable
    filterset = filtersets.DesignPlacementFilterSet
    filterset_form = forms.DesignPlacementFilterForm


@register_model_view(models.DesignPlacement, "add", detail=False)
@register_model_view(models.DesignPlacement, "edit")
class DesignPlacementEditView(generic.ObjectEditView):
    queryset = models.DesignPlacement.objects.all()
    form = forms.DesignPlacementForm


@register_model_view(models.DesignPlacement, "delete")
class DesignPlacementDeleteView(generic.ObjectDeleteView):
    queryset = models.DesignPlacement.objects.all()
