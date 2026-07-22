"""Views for NetBox Rack Design."""

import os

from dcim.models import PowerFeed, Rack, Site
from django.conf import settings
from django.contrib.staticfiles import finders
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View
from django_tables2 import RequestConfig
from netbox.plugins import get_plugin_config
from netbox.views import generic
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.views import ContentTypePermissionRequiredMixin, register_model_view

from . import filtersets, forms, models, projection, tables
from .choices import DesignStatusChoices

PLUGIN_NAME = "netbox_rack_design"

__all__ = (
    "DesignGroupView", "DesignGroupListView", "DesignGroupEditView", "DesignGroupDeleteView",
    "DesignGroupBulkImportView", "DesignGroupBulkEditView", "DesignGroupBulkDeleteView",
    "DesignView", "DesignListView", "DesignEditView", "DesignDeleteView",
    "DesignBulkImportView", "DesignBulkEditView", "DesignBulkDeleteView",
    "DesignElevationView", "DesignElevationRackRedirectView",
    "DesignEditorView", "DesignEditorDefaultView", "ElevationBrowserView",
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

    def get_extra_context(self, request, instance):
        # Racks this design touches: those targeted by its placements, plus the
        # current racks of any real devices the placements reference.
        rack_ids = (
            set(instance.placements.values_list("target_rack", flat=True))
            | set(
                instance.placements.filter(device__isnull=False).values_list("device__rack", flat=True)
            )
        )
        affected_racks = (
            Rack.objects.restrict(request.user, "view")
            .filter(pk__in=filter(None, rack_ids))
            .select_related("site")
        )
        # The explicit planning scope (the design.racks M2M), ordered by name.
        scoped_racks = (
            instance.racks.restrict(request.user, "view")
            .select_related("site", "location")
            .order_by("name", "pk")
        )
        return {
            "affected_racks": affected_racks,
            "affected_rack_count": len(affected_racks),
            "scoped_racks": scoped_racks,
        }


@register_model_view(models.Design, "elevation", path="elevation")
class DesignElevationView(generic.ObjectView):
    """
    Read-only projected elevation of ALL the design's scoped racks.

    URL: /plugins/rack-design/designs/<pk>/elevation/
    Name: plugins:netbox_rack_design:design_elevation  (kwargs: pk)

    Renders the SAME multi-rack workspace as the editor — every rack in
    ``design.racks`` (ordered by name) side by side, BOTH Front and Rear faces,
    the full-depth opposite-face hatch and a hover card — but with NO edit
    affordances (no catalog/quick-access, no add-rack/design-racks panels, no
    drag, no remove, no favorites, no Save). It reuses the SAME
    ``_project_rack_bundle`` helper the editor uses, so the projection is
    identical. No writes are performed.
    """

    queryset = models.Design.objects.all()
    template_name = "netbox_rack_design/design_elevation.html"

    def get_extra_context(self, request, instance):
        # The design's planning scope (design.racks), ordered by name — the same
        # ordering the editor's multi-rack workspace uses. Not restricted by
        # dcim.view_rack so the read-only view always shows the full scope, like
        # the editor does.
        scoped_racks = list(
            instance.racks.select_related("site", "location").order_by("name", "pk")
        )
        # One per-rack projected bundle per scoped rack, shaped identically to the
        # editor's blocks (same projection.project_rack contract).
        rack_blocks = [_project_rack_bundle(instance, rack) for rack in scoped_racks]
        return {
            "design": instance,
            "scoped_racks": scoped_racks,
            "rack_blocks": rack_blocks,
            "asset_version": _asset_version(),
        }


@register_model_view(models.Design, "elevation_rack", path="racks/<int:rack_id>")
class DesignElevationRackRedirectView(generic.ObjectView):
    """
    Back-compat redirect for the old per-rack elevation URL.

    URL: /plugins/rack-design/designs/<pk>/racks/<rack_id>/
    Name: plugins:netbox_rack_design:design_elevation_rack  (kwargs: pk, rack_id)

    The read-only elevation is now a single all-racks view; this preserves every
    existing per-(design, rack) link by redirecting to that view anchored on the
    requested rack's block (``#rd-rack-<rack_id>``).
    """

    queryset = models.Design.objects.all()

    def get(self, request, pk, rack_id):
        design = get_object_or_404(self.queryset, pk=pk)
        url = reverse(
            "plugins:netbox_rack_design:design_elevation",
            kwargs={"pk": design.pk},
        )
        return redirect(f"{url}#rd-rack-{rack_id}")


# Editor static assets we cache-bust: a ?v=<token> derived from their newest
# mtime is appended in the template so a browser always fetches the current
# build instead of a stale cached copy (no manual hard-refresh needed).
_EDITOR_ASSETS = (
    "netbox_rack_design/js/editor.js",
    "netbox_rack_design/js/editor_panels.js",
    "netbox_rack_design/js/legend_filter.js",
    "netbox_rack_design/js/rack_design.js",
    "netbox_rack_design/css/editor.css",
    "netbox_rack_design/css/rack_design.css",
)


def _asset_version():
    """Cache-bust token = newest mtime across the editor's own static assets."""
    newest = 0
    for rel in _EDITOR_ASSETS:
        path = finders.find(rel)
        try:
            if path:
                newest = max(newest, int(os.path.getmtime(path)))
        except OSError:
            continue
    return newest


def _slot_to_widget(slot):
    """
    Flatten one projected-slot dict into a JSON-serializable widget dict for the
    editor JS. See ``projection.py`` for the slot contract this consumes.
    """
    device = slot.get("device")
    device_type = slot.get("device_type")
    placement = slot.get("placement")
    u_position = slot.get("u_position")
    u_height = slot.get("u_height")
    return {
        "kind": slot.get("state"),
        "device_id": device.pk if device is not None else None,
        "device_type_id": device_type.pk if device_type is not None else None,
        "proposed_name": placement.proposed_name if placement is not None else "",
        "placement_id": placement.pk if placement is not None else None,
        "u_position": float(u_position) if u_position is not None else None,
        "u_height": float(u_height) if u_height is not None else None,
        "face": slot.get("face"),
        "label": slot.get("label"),
        # Passive full-depth "blocked" copy on the non-mounted face: the editor JS
        # locks it and excludes it from the save payload (the interactive tile
        # lives on the mounted face).
        "opposite_face": slot.get("opposite_face", False),
        # Saved displacement (spec §3/§4.3, parity ruling 2026-07-09): the
        # editor applies the collapsed-tile + outside-stripe treatment on
        # LOAD from this marking (its live gesture flow re-derives it for
        # in-session displacements).
        "displaced": slot.get("displaced", False),
        "displaced_by": slot.get("displaced_by"),
        # Full-depth flag for device-LESS widgets (a reloaded catalog add):
        # editor.js's isFullDepthWidget() resolves real devices via its
        # server-seeded fullDepthDeviceIds map, but an add has no device_id,
        # so it needs the type's own flag (already true for SESSION adds,
        # which stamp it from the palette item's data attribute).
        "is_full_depth": bool(device_type is not None and device_type.is_full_depth),
        # Power projection (docs/power-projection-spec.md): the device's
        # projected draw in watts and whether any power data was found. Drives
        # the per-rack power bar and the heatmap gradient in the editor.
        "draw_w": float(slot.get("draw_w") or 0.0),
        "draw_known": bool(slot.get("draw_known")),
        # Role slug (docs/pdu-distribution-spec.md): the SAME signal
        # distribution_example.PDU_ROLE_SLUGS matches on server-side, reused here
        # so the editor JS can detect a planned PDU add exactly, not just guess
        # from the role's display name (reloaded add only -- a brand-new drag-in
        # has no placement yet, so the JS falls back to the palette's role name).
        "role_slug": projection._slot_role_slug(slot),
        # Planned-PDU power inputs (Phase A/D, models.DesignPlacement.power_config):
        # only ever set on a `kind=add` placement whose role is a PDU. Lets the
        # PDU power dialog reopen pre-filled after a reload.
        "power_config": placement.power_config if placement is not None else None,
        # Feed binding (docs/pdu-distribution-spec.md §6.2): whichever of these is
        # set on the placement rides back to the editor JS so the bind-to-feed
        # dialog can preselect the PDU's current binding on reopen. At most one is
        # ever non-null (DesignPlacement.clean() enforces it).
        "real_power_feed_id": placement.real_power_feed_id if placement is not None else None,
        "planned_power_feed_id": placement.planned_power_feed_id if placement is not None else None,
        # Referenced source PDU (docs/pdu-distribution-spec.md §6): the real PDU
        # device this planned PDU inherits cf from, delivered so the dialog can
        # preselect it on reopen. None for manual/absent cf.
        "power_source_device_id": placement.power_source_device_id if placement is not None else None,
    }


def _project_rack_bundle(design, rack):
    """
    Project ONE rack under a design into a per-rack widget bundle for the editor.

    Reuses ``projection.project_rack`` (the projection contract is unchanged) and
    the existing ``_slot_to_widget`` builder, so every visible rack in the
    multi-rack workspace is shaped identically to the single-rack context.
    """
    result = projection.project_rack(design, rack)
    widgets = [
        _slot_to_widget(slot)
        for slot in (*result.front, *result.rear, *result.non_racked)
    ]
    # Saved per-(design, rack) power planning override (docs/pdu-distribution-
    # spec.md, models.DesignRackPower): delivered into the editor context so the
    # rack-power button can pre-fill without an extra fetch (see api/views.py's
    # rack-power GET action, which this mirrors).
    rack_power_row = models.DesignRackPower.objects.filter(design=design, rack=rack).first()
    return {
        "rack": rack,
        "front": result.front,
        "rear": result.rear,
        "non_racked": result.non_racked,
        "widgets": widgets,
        "rack_meta": {
            "id": rack.pk,
            "u_height": rack.u_height,
            "desc_units": rack.desc_units,
        },
        # Power projection summary (docs/power-projection-spec.md): drives the
        # per-rack power bar shown in normal mode and the heatmap legend.
        "power": result.power,
        "rack_power": rack_power_row.power_config if rack_power_row else None,
        # Feed-model gating (docs/pdu-distribution-spec.md §6.3): the per-rack
        # "Power" button (greenfield planned-power flow) is only useful when the
        # rack has NO real PowerFeeds yet -- a provisioned rack's PDUs bind
        # straight to its real feeds via the bind-to-feed dialog instead.
        "has_real_feeds": PowerFeed.objects.filter(rack=rack).exists(),
    }


def _design_editor_context(request, design):
    """
    Shared multi-rack editor context: EVERY scoped rack of the design rendered
    side by side, plus the tool-drawer panels' data.

    Used by both the per-rack editor route (``design_editor``) and the default,
    no-rack route (``design_editor_default``). The default route is the primary
    entry point and is reachable even for a brand-new design with ZERO scoped
    racks — in that case ``all_rack_blocks`` is empty and the template shows a
    friendly empty state instead of bouncing to the detail page.
    """
    scoped_racks = list(
        design.racks.select_related("site", "location").order_by("name", "pk")
    )
    # VISIBLE racks = scope minus the current user's hidden rows for this design.
    # We store HIDDEN rows, so "no rows" => everything is visible.
    if request.user.is_authenticated:
        hidden_rack_ids = list(
            models.HiddenDesignRack.objects.filter(
                user=request.user, design=design
            ).values_list("rack_id", flat=True)
        )
    else:
        hidden_rack_ids = []
    # Render EVERY scoped rack block and flag the hidden ones so the "Design
    # racks" panel can show/hide them via a CSS class with no page reload.
    all_rack_blocks = [
        {
            **_project_rack_bundle(design, scoped_rack),
            "hidden": scoped_rack.pk in hidden_rack_ids,
        }
        for scoped_rack in scoped_racks
    ]
    # Rows for the "Design racks" panel: one per scoped rack with its current
    # shown/hidden state for this user.
    scoped_rack_rows = [
        {"rack": scoped_rack, "hidden": scoped_rack.pk in hidden_rack_ids}
        for scoped_rack in scoped_racks
    ]
    return {
        "scoped_racks": scoped_racks,
        "hidden_rack_ids": hidden_rack_ids,
        "all_rack_blocks": all_rack_blocks,
        "scoped_rack_rows": scoped_rack_rows,
        # Drives the empty-state markup + the drawer's default-open override.
        "has_racks": bool(all_rack_blocks),
        "save_url": f"/api/plugins/rack-design/designs/{design.pk}/save-layout/",
        # Read-only naming preview for the editor's add auto-fill (Phase 3).
        "preview_name_url": f"/api/plugins/rack-design/designs/{design.pk}/preview-name/",
        # User-scoped favorite device types (the catalog palette's stars).
        "favorites_url": "/api/plugins/rack-design/favorite-device-types/",
        "asset_version": _asset_version(),
        # Developer-mode flag: gates the editor JS's opt-in drag-lifecycle
        # tracer (window.__rdDragTrace). True only on a dev build -- DEBUG on,
        # or the Django Debug Toolbar installed -- so the tracer is never even
        # reachable on a production deployment.
        "rd_debug": bool(
            getattr(settings, "DEBUG", False)
            or "debug_toolbar" in getattr(settings, "INSTALLED_APPS", [])
        ),
        # Drives the left-rail manufacturer/role/tenant selectors as NetBox
        # API-backed searchable selects (see forms.DesignEditorPaletteForm).
        "palette_form": forms.DesignEditorPaletteForm(),
        # Drives the "Add rack" panel's Location + Rack choosers, scoped to this
        # design's site (see forms.DesignEditorAddRackForm).
        "add_rack_form": forms.DesignEditorAddRackForm(site_id=design.site_id),
        # Custom-field bridge schema (docs/pdu-distribution-spec.md §5): drives
        # the rack-power dialog's dynamically-rendered fields. `{}` (default) ->
        # the dialog shows only the copy-from-rack row, no hardcoded cf inputs.
        "planning_fields": get_plugin_config(PLUGIN_NAME, "planning_fields", {}),
    }


@register_model_view(models.Design, "editor", path="editor/<int:rack_id>")
class DesignEditorView(generic.ObjectView):
    """
    Interactive single-rack layout editor for ONE rack under a design.

    URL: /plugins/rack-design/designs/<pk>/editor/<rack_id>/
    Name: plugins:netbox_rack_design:design_editor  (kwargs: pk, rack_id)

    Loads the Design (pk) and the Rack (rack_id), projects the layout with
    ``projection.project_rack`` and hands a JSON-serializable list of widgets to
    the GridStack editor JS. This first slice supports MOVE + REMOVE on a single
    rack only.
    """

    queryset = models.Design.objects.all()
    template_name = "netbox_rack_design/design_editor.html"

    def get_object(self, **kwargs):
        # The URL also carries rack_id; the Design is identified by pk alone.
        kwargs.pop("rack_id", None)
        return super().get_object(**kwargs)

    def get_extra_context(self, request, instance):
        # The URL targets a specific rack; load it (unrestricted, like the editor
        # itself) but do NOT 404 if it is out of scope — the editor still renders
        # the design's whole scope and just flags this rack as out-of-scope.
        rack = get_object_or_404(Rack.objects.all(), pk=self.kwargs["rack_id"])
        result = projection.project_rack(instance, rack)

        widgets = [
            _slot_to_widget(slot)
            for slot in (*result.front, *result.rear, *result.non_racked)
        ]

        context = _design_editor_context(request, instance)
        scoped_racks = context["scoped_racks"]
        hidden_rack_ids = context["hidden_rack_ids"]
        # VISIBLE racks: the scope minus this user's hidden rows. Kept for the
        # rack-specific route's context contract (the template renders the full
        # all_rack_blocks set; this is exposed for callers/tests).
        visible_racks = [
            _project_rack_bundle(instance, scoped_rack)
            for scoped_rack in scoped_racks
            if scoped_rack.pk not in hidden_rack_ids
        ]
        # The currently-open rack is marked active in the template. If the URL
        # rack is NOT in scope we still render it (don't 404) and flag it.
        context.update({
            "rack": rack,
            "current_in_scope": any(r.pk == rack.pk for r in scoped_racks),
            "visible_racks": visible_racks,
            "front": result.front,
            "rear": result.rear,
            "non_racked": result.non_racked,
            "widgets": widgets,
            "rack_meta": {
                "id": rack.pk,
                "u_height": rack.u_height,
                "desc_units": rack.desc_units,
            },
        })
        return context


@register_model_view(models.Design, "editor_default", path="editor")
class DesignEditorDefaultView(generic.ObjectView):
    """
    Primary editor entry point: open the multi-rack editor by design alone.

    URL: /plugins/rack-design/designs/<pk>/editor/
    Name: plugins:netbox_rack_design:design_editor_default  (kwargs: pk)

    Renders the SAME multi-rack workspace as ``design_editor`` (every scoped rack
    side by side, the tool drawer, the Add-rack / Design-racks panels) but needs
    NO rack_id, so it is reachable for a brand-new design with ZERO scoped racks.
    In that case the workspace shows a friendly empty state and the drawer
    defaults OPEN on the Racks section so the first rack can be added from inside
    the editor (you no longer need a rack to reach the editor).
    """

    queryset = models.Design.objects.all()
    template_name = "netbox_rack_design/design_editor.html"

    def get_extra_context(self, request, instance):
        return _design_editor_context(request, instance)


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
# Elevation browser (standalone, non-model-bound)
# ---------------------------------------------------------------------------


class ElevationBrowserView(ContentTypePermissionRequiredMixin, View):
    """
    Standalone "Elevations" LIST page (not bound to any single object).

    URL: /plugins/rack-design/elevations/
    Name: plugins:netbox_rack_design:elevation_browser

    Renders a filterable TABLE of (design, rack) pairs -- one row per distinct
    (design, rack) where the design "touches" the rack, i.e. the design has a
    placement whose ``target_rack`` is the rack OR whose referenced
    ``device.rack`` is the rack. A design touching three racks yields three rows.
    Each row links to the per-(design, rack) elevation (``design_elevation``) and
    editor (``design_editor``) views; the actual elevation rendering lives in
    those separate views, not on this page.

    Filters (GET params ``design``, ``rack``, ``site``, ``status``) are applied
    server-side to the derived rows; empty filters show every entry.

    Gated by ``netbox_rack_design.view_design`` via
    ContentTypePermissionRequiredMixin, which also enforces login when
    LOGIN_REQUIRED is set (anonymous users cannot see it).
    """

    template_name = "netbox_rack_design/elevation_browser.html"

    def get_required_permission(self):
        return "netbox_rack_design.view_design"

    def _build_rows(self):
        """Derive one row dict per distinct (design, rack) the design touches."""
        placements = (
            models.DesignPlacement.objects.filter(
                Q(target_rack__isnull=False) | Q(device__rack__isnull=False)
            )
            .select_related(
                "design", "design__site",
                "target_rack", "target_rack__site",
                "device__rack", "device__rack__site",
            )
        )

        # Aggregate per (design_pk, rack_pk): count placements affecting the pair,
        # keeping one Design/Rack reference for rendering.
        rows = {}
        for placement in placements:
            design = placement.design
            candidate_racks = [placement.target_rack]
            if placement.device_id and placement.device.rack_id:
                candidate_racks.append(placement.device.rack)
            for rack in candidate_racks:
                if rack is None:
                    continue
                key = (design.pk, rack.pk)
                entry = rows.get(key)
                if entry is None:
                    rows[key] = {
                        "design": design,
                        "rack": rack,
                        "site": rack.site,
                        "placement_count": 1,
                    }
                else:
                    entry["placement_count"] += 1

        return list(rows.values())

    @staticmethod
    def _selected_ids(request, param):
        """Return the multi-valued GET param as a set of strings (empty => no constraint)."""
        return {v for v in request.GET.getlist(param) if v != ""}

    def _apply_filters(self, rows, sel_designs, sel_racks, sel_sites, sel_status):
        """
        Multi-select filtering: within a field OR the values, across fields AND.
        Each selection set is a set of strings; an empty set is no constraint.
        """
        if sel_designs:
            rows = [r for r in rows if str(r["design"].pk) in sel_designs]
        if sel_racks:
            rows = [r for r in rows if str(r["rack"].pk) in sel_racks]
        if sel_sites:
            rows = [r for r in rows if r["site"] and str(r["site"].pk) in sel_sites]
        if sel_status:
            rows = [r for r in rows if r["design"].status in sel_status]
        return rows

    def get(self, request):
        all_rows = self._build_rows()

        sel_designs = self._selected_ids(request, "design")
        sel_racks = self._selected_ids(request, "rack")
        sel_sites = self._selected_ids(request, "site")
        sel_status = self._selected_ids(request, "status")

        # ---- Narrow the OFFERED filter options from the derived rows + selection ----
        # Design options: every design that appears in any elevation row.
        design_ids = {r["design"].pk for r in all_rows}

        # Rows constrained only by the *current Design + Site* selection drive the
        # Rack and Status option sets (so Rack/Site options reflect the chosen
        # design(s)/site(s) but not a chosen rack/status, which would self-limit).
        ds_rows = all_rows
        if sel_designs:
            ds_rows = [r for r in ds_rows if str(r["design"].pk) in sel_designs]
        site_scoped_rows = ds_rows
        if sel_sites:
            site_scoped_rows = [r for r in ds_rows if r["site"] and str(r["site"].pk) in sel_sites]

        # Rack options: racks in elevations of the selected design(s), further
        # limited to the selected site(s) if any; else all racks present in rows.
        rack_ids = {r["rack"].pk for r in site_scoped_rows}
        # Site options: sites in elevations of the selected design(s); else all present.
        site_ids = {r["site"].pk for r in ds_rows if r["site"]}
        # Status options: statuses present among the design-narrowed rows.
        present_status = {r["design"].status for r in ds_rows}
        status_choices = [c for c in DesignStatusChoices if c[0] in present_status]

        form = forms.ElevationBrowserFilterForm(
            request.GET or None,
            design_qs=models.Design.objects.filter(pk__in=design_ids),
            rack_qs=Rack.objects.filter(pk__in=rack_ids),
            site_qs=Site.objects.filter(pk__in=site_ids),
            status_choices=status_choices,
        )

        # ---- Apply the active filters to the rows shown in the table ----
        rows = self._apply_filters(all_rows, sel_designs, sel_racks, sel_sites, sel_status)
        # Stable ordering: by design title, then rack name.
        rows.sort(key=lambda r: (r["design"].title.lower(), r["rack"].name.lower()))

        table = tables.ElevationTable(rows)
        RequestConfig(request, {
            "paginator_class": EnhancedPaginator,
            "per_page": get_paginate_count(request),
        }).configure(table)

        return render(request, self.template_name, {
            "form": form,
            "table": table,
            "row_count": len(rows),
        })


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
