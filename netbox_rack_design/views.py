"""Views for NetBox Rack Design."""

from netbox.views import generic
from utilities.views import register_model_view

from . import filtersets, forms, models, tables

__all__ = (
    "DesignGroupView", "DesignGroupListView", "DesignGroupEditView", "DesignGroupDeleteView",
    "DesignGroupBulkImportView", "DesignGroupBulkEditView", "DesignGroupBulkDeleteView",
    "DesignView", "DesignListView", "DesignEditView", "DesignDeleteView",
    "DesignBulkImportView", "DesignBulkEditView", "DesignBulkDeleteView",
    "DesignPlacementView", "DesignPlacementListView", "DesignPlacementEditView", "DesignPlacementDeleteView",
    "DesignPlacementBulkImportView", "DesignPlacementBulkEditView", "DesignPlacementBulkDeleteView",
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


@register_model_view(models.DesignGroup, "bulk_import", detail=False)
class DesignGroupBulkImportView(generic.BulkImportView):
    queryset = models.DesignGroup.objects.all()
    model_form = forms.DesignGroupImportForm


@register_model_view(models.DesignGroup, "bulk_edit", path="edit", detail=False)
class DesignGroupBulkEditView(generic.BulkEditView):
    queryset = models.DesignGroup.objects.all()
    filterset = filtersets.DesignGroupFilterSet
    table = tables.DesignGroupTable
    form = forms.DesignGroupBulkEditForm


@register_model_view(models.DesignGroup, "bulk_delete", path="delete", detail=False)
class DesignGroupBulkDeleteView(generic.BulkDeleteView):
    queryset = models.DesignGroup.objects.all()
    filterset = filtersets.DesignGroupFilterSet
    table = tables.DesignGroupTable


# ---------------------------------------------------------------------------
# Design
# ---------------------------------------------------------------------------


@register_model_view(models.Design)
class DesignView(generic.ObjectView):
    queryset = models.Design.objects.all()


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


@register_model_view(models.Design, "bulk_import", detail=False)
class DesignBulkImportView(generic.BulkImportView):
    queryset = models.Design.objects.all()
    model_form = forms.DesignImportForm


@register_model_view(models.Design, "bulk_edit", path="edit", detail=False)
class DesignBulkEditView(generic.BulkEditView):
    queryset = models.Design.objects.all()
    filterset = filtersets.DesignFilterSet
    table = tables.DesignTable
    form = forms.DesignBulkEditForm


@register_model_view(models.Design, "bulk_delete", path="delete", detail=False)
class DesignBulkDeleteView(generic.BulkDeleteView):
    queryset = models.Design.objects.all()
    filterset = filtersets.DesignFilterSet
    table = tables.DesignTable


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


@register_model_view(models.DesignPlacement, "bulk_import", detail=False)
class DesignPlacementBulkImportView(generic.BulkImportView):
    queryset = models.DesignPlacement.objects.all()
    model_form = forms.DesignPlacementImportForm


@register_model_view(models.DesignPlacement, "bulk_edit", path="edit", detail=False)
class DesignPlacementBulkEditView(generic.BulkEditView):
    queryset = models.DesignPlacement.objects.all()
    filterset = filtersets.DesignPlacementFilterSet
    table = tables.DesignPlacementTable
    form = forms.DesignPlacementBulkEditForm


@register_model_view(models.DesignPlacement, "bulk_delete", path="delete", detail=False)
class DesignPlacementBulkDeleteView(generic.BulkDeleteView):
    queryset = models.DesignPlacement.objects.all()
    filterset = filtersets.DesignPlacementFilterSet
    table = tables.DesignPlacementTable
