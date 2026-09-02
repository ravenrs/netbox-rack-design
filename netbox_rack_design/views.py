"""Views for NetBox Rack Design."""

import json
import os

from dcim.models import PowerFeed, Rack, Site
from django import forms as django_forms
from django.conf import settings
from django.contrib import messages
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View
from django_tables2 import RequestConfig
from netbox.plugins import get_plugin_config
from netbox.views import generic
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.query import count_related
from utilities.views import ContentTypePermissionRequiredMixin, register_model_view

from . import filtersets, forms, models, planning_fields, projection, tables
from .choices import DesignStatusChoices
from .distribution import DEFAULT_DISTRIBUTION_MODE

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
    "DesignDeriveView", "DesignRebaseView", "DesignChainHealthView",
)


def _frozen_design_message(design, what):
    """
    The message shown when a write path is rejected because ``design`` is
    frozen (PLAN-design-chains.md §2.2/G4). Mirrors the wording
    ``DesignPlacement.clean()`` raises for the same reason (models.py), so a
    user sees one consistent explanation no matter which write path caught it.

    ``what`` names the resource in the caller's own words, e.g. "its
    placements" or "its planned power feeds".
    """
    return (
        f"{design} is approved, and approved designs are frozen: {what} "
        "cannot be created, edited or deleted. Set the design back to draft, "
        "or create a new version of it, to make this change."
    )


# ---------------------------------------------------------------------------
# DesignGroup
# ---------------------------------------------------------------------------


@register_model_view(models.DesignGroup)
class DesignGroupView(generic.ObjectView):
    queryset = models.DesignGroup.objects.all()


@register_model_view(models.DesignGroup, "list", path="", detail=False)
class DesignGroupListView(generic.ObjectListView):
    queryset = models.DesignGroup.objects.annotate(
        design_count=count_related(models.Design, "group"),
    )
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
            "planned_feeds": self._planned_feed_rows(instance),
            # Changes this design can no longer carry out because the device
            # they referenced was deleted from DCIM. Reported here because the
            # rows are inert everywhere else: the editor and the elevation skip
            # them, so this page is the only place the loss can be seen.
            "stale_placements": instance.stale_placements.select_related("target_rack"),
        }

    @staticmethod
    def _planned_feed_rows(design):
        """The design's PLANNED power feeds, one row per feed.

        Planned feeds are written by the rack-power dialog ("copy from rack")
        and by the PDU bind dialog, and until now they were visible NOWHERE --
        not on this page, not in a list view, not through a UI route of any kind
        (user 2026-08-28: "where do we look at the feeds we created?"). They size
        a greenfield rack's capacity bar, so a stray one silently inflates it,
        which is exactly the kind of thing a plan must be able to show.

        Each row carries the derated watts the capacity bar actually uses, and
        the PDUs bound to that feed, so the page answers "where did this number
        come from" and "what breaks if I remove it".
        """
        from netbox.config import get_config

        from .distribution import breaker_watts

        max_util = get_config().POWERFEED_DEFAULT_MAX_UTILIZATION or 100
        rows = []
        feeds = (
            design.planned_feeds.select_related("rack")
            .prefetch_related("bound_placements")
            .order_by("rack__name", "name")
        )
        for feed in feeds:
            watts = breaker_watts(feed) or 0
            rows.append({
                "feed": feed,
                "rack": feed.rack,
                "watts": round(watts * max_util / 100.0) if watts else 0,
                "bound": list(feed.bound_placements.all()),
            })
        return rows


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
            # Gates the read-only chassis link, on the same test the editor uses.
            "has_chassis_in_scope": projection.has_chassis_in_scope(instance),
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


def _design_names_for_slots(slots):
    """
    Batch-resolve ``source_design_id`` -> design name for a set of slots.

    PLAN-design-chains.md §5/G3: the widget dict must carry the ancestor
    design's NAME (``source_design_name``), not just its pk, so the frontend
    never has to fetch it separately. One query per call site (per rack)
    rather than one per inherited slot.
    """
    ids = {slot.get("source_design_id") for slot in slots if slot.get("source_design_id")}
    if not ids:
        return {}
    # str(design), not just its title, so this matches the SAME representation
    # ``chain_conflicts`` uses for a design-level entry's ``source_design_name``
    # (both read off ``str(source_design)`` -- see ``_conflict()`` in
    # projection.py, whose messages already read "{ancestor}").
    return {d.pk: str(d) for d in models.Design.objects.filter(pk__in=ids)}


def _slot_to_widget(slot, design_names=None):
    """
    Flatten one projected-slot dict into a JSON-serializable widget dict for the
    editor JS. See ``projection.py`` for the slot contract this consumes.

    ``design_names`` is the ``_design_names_for_slots`` map for the batch this
    slot belongs to; omitted defaults to an empty map (no inherited slots to
    resolve, or a caller that has not built one).
    """
    design_names = design_names or {}
    device = slot.get("device")
    device_type = slot.get("device_type")
    placement = slot.get("placement")
    u_position = slot.get("u_position")
    u_height = slot.get("u_height")
    source_design_id = slot.get("source_design_id")
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
        # The deployment's config-declared planning fields
        # (``placement_fields``): the values the planner typed, delivered back
        # so the tile's attributes dialog reopens pre-filled after a reload.
        "planning_data": (placement.planning_data or {}) if placement is not None else {},
        # Planned re-attribution, round-tripped so a reloaded add or move keeps
        # what the design says it becomes (and the editor can show it).
        "device_role_id": placement.device_role_id if placement is not None else None,
        "tenant_id": placement.tenant_id if placement is not None else None,
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
        # PROVENANCE + CONFLICT (PLAN-design-chains.md §5/G3, §8.4): flags
        # carried straight off the slot dict's own ``inherited``/
        # ``source_design_id``/``conflict``/``conflict_reason`` (projection.py
        # already computes these; see its module docstring for the contract).
        # ``source_design_name`` is derived here, server-side, from
        # ``design_names`` so the frontend never has to fetch it separately.
        "inherited": bool(slot.get("inherited")),
        "source_design_id": source_design_id,
        "source_design_name": design_names.get(source_design_id),
        "conflict": bool(slot.get("conflict")),
        "conflict_reason": slot.get("conflict_reason"),
    }


def _project_rack_bundle(design, rack):
    """
    Project ONE rack under a design into a per-rack widget bundle for the editor.

    Reuses ``projection.project_rack`` (the projection contract is unchanged) and
    the existing ``_slot_to_widget`` builder, so every visible rack in the
    multi-rack workspace is shaped identically to the single-rack context.
    """
    result = projection.project_rack(design, rack)
    all_slots = (*result.front, *result.rear, *result.non_racked)
    design_names = _design_names_for_slots(all_slots)
    widgets = [_slot_to_widget(slot, design_names) for slot in all_slots]
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
        # Chain conflicts (PLAN-design-chains.md §8.3/G3), for this rack's
        # projection specifically. Folded into the design-level
        # ``chain_conflicts`` context key by ``_design_editor_context`` --
        # kept here too so a caller working with one bundle (not the whole
        # editor context) can still see them.
        "conflicts": result.conflicts,
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
    # Chain conflicts (PLAN-design-chains.md §8.2/§8.3/G3): every rack's
    # ``ProjectedElevation.conflicts``, flattened into ONE list for the
    # persistent panel a chain conflict must render in (never a toast --
    # §8.2, it is not this design's fault and persists until someone
    # re-bases). ``slot_key`` is deliberately the SAME identifier the widget
    # dicts already carry -- a conflict's ``placement`` (when set) is the
    # exact placement a slot's ``placement_id`` came from (see
    # projection.py's ``_conflict()``/``emit()``), so the frontend joins a
    # conflict to its tile by ``slot_key == widget.placement_id`` with no new
    # scheme. A design-level refusal (chain_broken / ancestor_implemented /
    # ancestor_not_approved) carries no placement at all -- ``slot_key`` is
    # None, meaning the entry is about the rack/chain as a whole, not any one
    # tile, and the panel renders it without trying to highlight a tile.
    chain_conflicts = []
    for block in all_rack_blocks:
        rack_pk = block["rack"].pk
        for entry in block["conflicts"]:
            placement = entry.get("placement")
            source_design = entry.get("source_design")
            chain_conflicts.append({
                "kind": entry["kind"],
                "severity": entry["severity"],
                "detail": entry["detail"],
                "rack_id": rack_pk,
                "source_design_id": source_design.pk if source_design is not None else None,
                "source_design_name": str(source_design) if source_design is not None else None,
                "slot_key": placement.pk if placement is not None else None,
            })
    return {
        "scoped_racks": scoped_racks,
        "hidden_rack_ids": hidden_rack_ids,
        "all_rack_blocks": all_rack_blocks,
        "scoped_rack_rows": scoped_rack_rows,
        "chain_conflicts": chain_conflicts,
        # Drives the empty-state markup + the drawer's default-open override.
        "has_racks": bool(all_rack_blocks),
        # Gates the chassis-layer switch (spec §10.3/§10.4): offered only when the
        # design's scope actually contains a chassis, so a deployment with no
        # blade hardware never sees the feature. Cheap existence check -- the full
        # chassis projection only runs inside the layer itself.
        "has_chassis_in_scope": projection.has_chassis_in_scope(design),
        "save_url": f"/api/plugins/rack-design/designs/{design.pk}/save-layout/",
        # Read-only naming preview for the editor's add auto-fill (Phase 3).
        "preview_name_url": f"/api/plugins/rack-design/designs/{design.pk}/preview-name/",
        # User-scoped favorite device types (the catalog palette's stars).
        "favorites_url": "/api/plugins/rack-design/favorite-device-types/",
        # The user's NAMED favorite sets ("Default", "for server", ...), which
        # the palette's stars read from and write into.
        "favorite_sets_url": "/api/plugins/rack-design/favorite-sets/",
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
        # Config-declared placement fields (planning_fields.py): the descriptors
        # the editor renders as extra inputs -- in the palette rail for the ones
        # flagged ``rail``, and in each add tile's attributes dialog. `[]`
        # (default) -> the editor shows no extra inputs at all. ``target`` is
        # stripped: it names a real custom field and is deployment plumbing, not
        # part of what the frontend needs.
        "placement_fields": planning_fields.public_placement_field_schema(),
        # Effective power-distribution engine (docs/pdu-distribution-spec.md):
        # editor.js reads this to decide whether the rack/PDU power dialogs'
        # manual cf inputs are worth showing at all -- they only ever reach a
        # user's distribution script, so in "none"/"builtin" mode rendering
        # them would promise an effect the active engine cannot deliver.
        "distribution_mode": get_plugin_config(
            PLUGIN_NAME, "distribution_mode", DEFAULT_DISTRIBUTION_MODE
        ),
        # Same loss the detail page reports (see DesignView.get_extra_context):
        # placements whose device was deleted from DCIM. The editor otherwise
        # skips these rows entirely (they're inert -- projection.py filters
        # device__isnull=False for move/remove), so a planner working here would
        # never learn a planned change silently stopped happening.
        "stale_placements": design.stale_placements,
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

        all_slots = (*result.front, *result.rear, *result.non_racked)
        design_names = _design_names_for_slots(all_slots)
        widgets = [_slot_to_widget(slot, design_names) for slot in all_slots]

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


# A planned chassis has no device pk, so its synthetic grid id is offset past any
# plausible dcim.Device pk. Keeps the two id namespaces disjoint inside the
# editor's per-"rack" registries without inventing a compound key initRack would
# have to understand.
_PLANNED_CHASSIS_GRID_OFFSET = 1_000_000_000


def _chassis_layer_context(request, design):
    """
    Context for the CHASSIS LAYER (spec §10.3): the same workspace as the rack
    editor, re-pointed at chassis.

    A chassis IS a rack there -- bays in place of units -- so this mirrors
    ``_design_editor_context`` field for field: every chassis in scope rendered as
    a column, the user's hidden ones flagged (not dropped, so the Chassis panel
    can toggle them with no reload), and the same save/preview/favourites URLs.
    """
    entries = projection.chassis_in_scope(design)
    if request.user.is_authenticated:
        hidden_chassis_ids = set(
            models.HiddenDesignChassis.objects.filter(
                user=request.user, design=design
            ).values_list("chassis_id", flat=True)
        )
    else:
        hidden_chassis_ids = set()

    columns = []
    for entry in entries:
        column = projection.project_chassis(design, entry)
        column["hidden"] = bool(
            entry["device"] is not None and entry["device"].pk in hidden_chassis_ids
        )
        # --- make the column a DEGENERATE RACK for initRack (spec §10.3) ------
        # A synthetic per-column grid id, because initRack keys every element id
        # and its controller registry off ONE integer. A real chassis uses its
        # device pk; a planned one is offset past any plausible device pk so the
        # two namespaces cannot collide.
        if entry["device"] is not None:
            column["grid_id"] = entry["device"].pk
        else:
            column["grid_id"] = _PLANNED_CHASSIS_GRID_OFFSET + entry["placement"].pk
        column["grid_id_str"] = str(column["grid_id"])
        column["bay_names_json"] = json.dumps([s["name"] for s in column["slots"]])
        # Parallel array of dcim.DeviceBay pks (null for a planned chassis, whose
        # bays do not exist yet). Needed on the BLOCK because an EMPTY bay renders
        # no element of its own -- the stripe behind the grid is the empty slot --
        # so a drop into one has nowhere else to read the bay's id from.
        column["bay_ids_json"] = json.dumps(
            [(s["bay"].pk if s["bay"] is not None else None) for s in column["slots"]]
        )

        # ONE BAY == ONE WHOLE "U". A rack grid steps in HALF units (gsYToUPosition
        # divides gsY by 2, so a 1U device is gs-h=2), so a bay is modelled as a
        # 1U device in a rack whose height is the bay count: gs_y = (index-1)*2.
        # With data-desc-units="true" gsYToUPosition then returns exactly the
        # 1-based BAY INDEX, so initRack needs no arithmetic of its own and a
        # freshly loaded column is identical to what it would save.
        widgets = []
        for slot in column["slots"]:
            slot["gs_y"] = (slot["index"] - 1) * 2
            if not slot["label"]:
                slot["widget_index"] = None
                continue
            slot["widget_index"] = len(widgets)
            placement = slot["placement"]
            widgets.append({
                "kind": (placement.kind if placement is not None else "existing"),
                "device_id": slot["device"].pk if slot["device"] is not None else None,
                "device_type_id": (
                    slot["device_type"].pk if slot["device_type"] is not None else None
                ),
                "placement_id": placement.pk if placement is not None else None,
                "u_height": 1,        # one bay == one whole unit (gsH = 2)
                "u_position": slot["index"],
                "label": slot["label"],
                "face": "front",
                "is_full_depth": False,
                "bay_name": slot["name"],
                "bay_id": slot["bay"].pk if slot["bay"] is not None else None,
            })
        column["widgets"] = widgets
        columns.append(column)

    context = _design_editor_context(request, design)
    context.update({
        "chassis_layer": True,
        "chassis_columns": columns,
        "chassis_rows": [
            {"key": c["key"], "label": c["label"], "rack": c["rack"],
             "used": c["used"], "bay_count": c["bay_count"],
             "device": c["device"], "hidden": c["hidden"]}
            for c in columns
        ],
        "has_chassis": bool(columns),
        # The layer's canvas is editable; the flag is separate from the rack
        # editor's own, which is what lets DesignChassisElevationView render the
        # very same columns read-only.
        "editable_layer": True,
        # The ROUTER's path, not a per-design one: HiddenDesignChassisViewSet is
        # registered at the plugin API root and takes design_id in the body, the
        # same shape as hidden-design-racks.
        "hidden_chassis_url": "/api/plugins/rack-design/hidden-design-chassis/",
    })
    return context


@register_model_view(models.Design, "chassis", path="chassis")
class DesignChassisLayerView(generic.ObjectView):
    """
    The chassis layer: every chassis in the design's scope as a column of bays.

    Named for the CONTAINER, not its contents: a bay holds a blade in a compute
    chassis, but equally a module in a patch panel or a shelf insert, and the
    layer treats them all the same (user 2026-08-26).

    URL: /plugins/rack-design/designs/<pk>/chassis/
    Name: plugins:netbox_rack_design:design_chassis  (kwargs: pk)

    Exists because a rack elevation cannot also be a bay elevation -- an 8-bay
    chassis in a 3U tile is unreadable (spec §10.3, rejected 2026-08-25). Here a
    chassis is rendered AS a rack, so every §4 rule (validate -> confirm ->
    commit, blocking, ghosts, homecoming, cursor governance) applies verbatim
    with one bay as the step instead of 0.5U.
    """

    queryset = models.Design.objects.all()
    template_name = "netbox_rack_design/design_chassis.html"

    def get_extra_context(self, request, instance):
        return _chassis_layer_context(request, instance)


@register_model_view(models.Design, "chassis_elevation", path="chassis-elevation")
class DesignChassisElevationView(generic.ObjectView):
    """
    Read-only projected CHASSIS elevation -- the chassis twin of
    :class:`DesignElevationView`.

    URL: /plugins/rack-design/designs/<pk>/chassis-elevation/
    Name: plugins:netbox_rack_design:design_chassis_elevation  (kwargs: pk)

    Exists because bay occupancy is not an editing concern: someone reviewing a
    plan needs to see what sits in which bay without holding change permission
    or entering the editor (user 2026-08-26). Renders the SAME columns from the
    SAME projection as the editable layer, with ``editable_layer`` False.
    """

    queryset = models.Design.objects.all()
    template_name = "netbox_rack_design/design_chassis_elevation.html"

    def get_extra_context(self, request, instance):
        context = _chassis_layer_context(request, instance)
        context["editable_layer"] = False
        return context


@register_model_view(models.Design, "list", path="", detail=False)
class DesignListView(generic.ObjectListView):
    queryset = models.Design.objects.annotate(
        placement_count=count_related(models.DesignPlacement, "design"),
    )
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
    """
    Deleting a placement never runs ``DesignPlacement.clean()`` (Django's
    delete path calls no ``clean()`` at all), so the freeze
    (PLAN-design-chains.md §2.2/G4) that create/edit already gets from
    ``clean()`` needs its own check here.
    """

    queryset = models.DesignPlacement.objects.all()

    def post(self, request, *args, **kwargs):
        obj = self.get_object(**kwargs)
        if obj.design.is_frozen:
            messages.error(request, _frozen_design_message(obj.design, "its placements"))
            return redirect(obj.get_absolute_url())
        return super().post(request, *args, **kwargs)


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
    """See :class:`DesignPlacementDeleteView` -- bulk delete has the same gap."""

    queryset = models.DesignPlacement.objects.all()
    filterset = filtersets.DesignPlacementFilterSet
    table = tables.DesignPlacementTable

    def post(self, request, **kwargs):
        if "_confirm" in request.POST:
            if request.POST.get("_all"):
                qs = self.queryset.all()
                if self.filterset is not None:
                    qs = self.filterset(request.GET, qs, request=request).qs
                pk_list = list(qs.values_list("pk", flat=True))
            else:
                pk_list = [int(pk) for pk in request.POST.getlist("pk")]
            frozen_designs = sorted({
                str(design) for design in models.Design.objects.filter(
                    placements__pk__in=pk_list, status=DesignStatusChoices.STATUS_APPROVED,
                ).distinct()
            })
            if frozen_designs:
                messages.error(
                    request,
                    "Cannot delete placements belonging to frozen (approved) "
                    "designs: " + ", ".join(frozen_designs) + ". Set those "
                    "designs back to draft, or create a new version of them, "
                    "to make this change.",
                )
                return redirect(self.get_return_url(request))
        return super().post(request, **kwargs)


# ---------------------------------------------------------------------------
# DesignPowerFeed (planned power feeds)
#
# The editor writes planned feeds from the rack-power and PDU-bind dialogs, and
# for a long time there was no UI route to see or remove one (user 2026-08-28:
# "I don't see any way to delete feeds -- make a separate view like placements").
# A stray feed silently inflates a greenfield rack's capacity bar, so the plan
# has to be able to show and unmake them. Same generic-view set as placements.
# ---------------------------------------------------------------------------


@register_model_view(models.DesignPowerFeed)
class DesignPowerFeedView(generic.ObjectView):
    queryset = models.DesignPowerFeed.objects.select_related("design", "rack")


@register_model_view(models.DesignPowerFeed, "list", path="", detail=False)
class DesignPowerFeedListView(generic.ObjectListView):
    queryset = models.DesignPowerFeed.objects.select_related(
        "design", "rack").prefetch_related("bound_placements")
    table = tables.DesignPowerFeedTable
    filterset = filtersets.DesignPowerFeedFilterSet
    filterset_form = forms.DesignPowerFeedFilterForm


@register_model_view(models.DesignPowerFeed, "add", detail=False)
@register_model_view(models.DesignPowerFeed, "edit")
class DesignPowerFeedEditView(generic.ObjectEditView):
    """
    ``DesignPowerFeed`` has no ``clean()`` override at all (unlike
    ``DesignPlacement``), so the freeze (PLAN-design-chains.md §2.2/G4) needs
    an explicit check here rather than riding the model form's
    ``full_clean()`` the way placement create/edit does.
    """

    queryset = models.DesignPowerFeed.objects.all()
    form = forms.DesignPowerFeedForm

    def post(self, request, *args, **kwargs):
        obj = self.get_object(**kwargs)
        design_id = obj.design_id or request.POST.get("design")
        design = models.Design.objects.filter(pk=design_id).first() if design_id else None
        if design is not None and design.is_frozen:
            messages.error(request, _frozen_design_message(design, "its planned power feeds"))
            return redirect(design.get_absolute_url())
        return super().post(request, *args, **kwargs)


@register_model_view(models.DesignPowerFeed, "delete")
class DesignPowerFeedDeleteView(generic.ObjectDeleteView):
    queryset = models.DesignPowerFeed.objects.all()

    def post(self, request, *args, **kwargs):
        obj = self.get_object(**kwargs)
        if obj.design.is_frozen:
            messages.error(request, _frozen_design_message(obj.design, "its planned power feeds"))
            return redirect(obj.get_absolute_url())
        return super().post(request, *args, **kwargs)


@register_model_view(models.DesignPowerFeed, "bulk_import", detail=False)
class DesignPowerFeedBulkImportView(generic.BulkImportView):
    queryset = models.DesignPowerFeed.objects.all()
    model_form = forms.DesignPowerFeedImportForm

    def save_object(self, object_form, request):
        design = object_form.instance.design
        if design is not None and design.is_frozen:
            raise ValidationError(
                _frozen_design_message(design, "its planned power feeds")
            )
        return super().save_object(object_form, request)


@register_model_view(models.DesignPowerFeed, "bulk_edit", path="edit", detail=False)
class DesignPowerFeedBulkEditView(generic.BulkEditView):
    queryset = models.DesignPowerFeed.objects.all()
    filterset = filtersets.DesignPowerFeedFilterSet
    table = tables.DesignPowerFeedTable
    form = forms.DesignPowerFeedBulkEditForm

    def _update_objects(self, form, request):
        frozen_designs = sorted({
            str(design) for design in models.Design.objects.filter(
                pk__in=self.queryset.filter(
                    pk__in=form.cleaned_data["pk"]
                ).values_list("design_id", flat=True),
                status=DesignStatusChoices.STATUS_APPROVED,
            )
        })
        if frozen_designs:
            raise ValidationError(
                "Cannot bulk-edit planned power feeds belonging to frozen "
                "(approved) designs: " + ", ".join(frozen_designs) + ". Set "
                "those designs back to draft, or create a new version of "
                "them, to make this change."
            )
        return super()._update_objects(form, request)


@register_model_view(models.DesignPowerFeed, "bulk_delete", path="delete", detail=False)
class DesignPowerFeedBulkDeleteView(generic.BulkDeleteView):
    queryset = models.DesignPowerFeed.objects.all()
    filterset = filtersets.DesignPowerFeedFilterSet
    table = tables.DesignPowerFeedTable

    def post(self, request, **kwargs):
        if "_confirm" in request.POST:
            if request.POST.get("_all"):
                qs = self.queryset.all()
                if self.filterset is not None:
                    qs = self.filterset(request.GET, qs, request=request).qs
                pk_list = list(qs.values_list("pk", flat=True))
            else:
                pk_list = [int(pk) for pk in request.POST.getlist("pk")]
            frozen_designs = sorted({
                str(design) for design in models.Design.objects.filter(
                    planned_feeds__pk__in=pk_list, status=DesignStatusChoices.STATUS_APPROVED,
                ).distinct()
            })
            if frozen_designs:
                messages.error(
                    request,
                    "Cannot delete planned power feeds belonging to frozen "
                    "(approved) designs: " + ", ".join(frozen_designs) + ". Set "
                    "those designs back to draft, or create a new version of "
                    "them, to make this change.",
                )
                return redirect(self.get_return_url(request))
        return super().post(request, **kwargs)


# ---------------------------------------------------------------------------
# Design chains (PLAN-design-chains.md phase 1): derive a new design from an
# approved one, and re-base an existing design onto a different parent.
# ---------------------------------------------------------------------------


class DesignRebaseForm(django_forms.Form):
    """
    Standalone (not in forms.py -- owned by another concurrent change) form for
    :class:`DesignRebaseView`: pick a new ``based_on`` target. Restricted to
    APPROVED designs, mirroring the create form's parent-picker rule (§5) and
    ``Design.is_frozen`` (only a frozen design is safe to baseline on, §2.2).
    """

    based_on = django_forms.ModelChoiceField(
        queryset=models.Design.objects.filter(status=DesignStatusChoices.STATUS_APPROVED),
        label="New base design",
        help_text="Only an approved design may be a base (PLAN-design-chains.md §2.2).",
    )


@register_model_view(models.Design, "derive", path="derive")
class DesignDeriveView(generic.ObjectView):
    """
    Create a new design whose ``based_on`` points at this one
    (PLAN-design-chains.md §5 phase 1).

    Available only from an APPROVED design: approval is what makes a design's
    placements read-only (``Design.is_frozen``), and a chain can only trust a
    layer that has stopped moving (§2.2). Deriving from a draft would let a
    child baseline on placements that could still change under it.

    URL: /plugins/rack-design/designs/<pk>/derive/
    Name: plugins:netbox_rack_design:design_derive  (kwargs: pk)
    """

    queryset = models.Design.objects.all()
    template_name = "netbox_rack_design/design_derive.html"

    def get_required_permission(self):
        # Deriving CREATES a new Design.
        return "netbox_rack_design.add_design"

    def get(self, request, pk):
        design = self.get_object(pk=pk)
        return render(request, self.template_name, {
            "object": design,
            "return_url": design.get_absolute_url(),
        })

    def post(self, request, pk):
        design = self.get_object(pk=pk)
        if not design.is_frozen:
            messages.error(
                request,
                "Only an approved design can be derived from: approval is "
                "what makes a design's placements read-only, so a child can "
                "trust its baseline (PLAN-design-chains.md §2.2). Approve "
                "this design first.",
            )
            return redirect(design.get_absolute_url())

        child = models.Design(
            title=f"{design.title} (derived)",
            site=design.site,
            group=design.group,
            based_on=design,
            status=DesignStatusChoices.STATUS_DRAFT,
        )
        with transaction.atomic():
            child.full_clean()
            child.save()
            # G6: seed the child's rack scope with a SNAPSHOT of the parent's
            # racks at derive time, not a live link -- a rack added to the
            # parent later does NOT retroactively appear on the child, which
            # owns and edits its own scope from here on. This is safe because
            # baseline replay is per-rack: a rack present on the child but
            # absent from the parent simply has no inherited layer. Must run
            # after save() (the M2M needs a pk) and inside the same
            # transaction as the create, so a child can never exist with a
            # half-copied scope.
            child.racks.set(design.racks.all())
        messages.success(request, f"Created {child} based on {design}.")
        return redirect(child.get_absolute_url())


@register_model_view(models.Design, "rebase", path="rebase")
class DesignRebaseView(generic.ObjectView):
    """
    Re-point an existing design's ``based_on`` at a different (approved)
    design (PLAN-design-chains.md §2.2/§9.2).

    The documented way out of two situations: a parent that has since been
    marked ``implemented`` (§9.2 -- the chain refuses to project past an
    implemented parent, so the child must re-base to render again), and a
    sibling that got approved first (§2.1 -- "first approved wins, the other
    re-bases"). Runs the model's own cycle guard via ``full_clean()`` rather
    than re-implementing it.

    URL: /plugins/rack-design/designs/<pk>/rebase/
    Name: plugins:netbox_rack_design:design_rebase  (kwargs: pk)
    """

    queryset = models.Design.objects.all()
    template_name = "netbox_rack_design/design_rebase.html"

    def get_required_permission(self):
        # Re-basing EDITS this design's own based_on field.
        return "netbox_rack_design.change_design"

    def get(self, request, pk):
        design = self.get_object(pk=pk)
        form = DesignRebaseForm(initial={"based_on": design.based_on_id})
        return render(request, self.template_name, {
            "object": design,
            "form": form,
            "return_url": design.get_absolute_url(),
        })

    def post(self, request, pk):
        design = self.get_object(pk=pk)
        form = DesignRebaseForm(request.POST)
        if form.is_valid():
            previous_based_on = design.based_on_id
            design.based_on = form.cleaned_data["based_on"]
            try:
                design.full_clean()
            except ValidationError as exc:
                design.based_on_id = previous_based_on
                if hasattr(exc, "message_dict"):
                    for field, errs in exc.message_dict.items():
                        target = field if field in form.fields else None
                        for err in errs:
                            form.add_error(target, err)
                else:
                    form.add_error(None, exc)
            else:
                design.save()
                messages.success(request, f"Re-based {design} onto {design.based_on}.")
                return redirect(design.get_absolute_url())
        return render(request, self.template_name, {
            "object": design,
            "form": form,
            "return_url": design.get_absolute_url(),
        })


# ---------------------------------------------------------------------------
# Chain health report (standalone, non-model-bound) -- PLAN-design-chains.md
# G4's reporting half.
# ---------------------------------------------------------------------------


def _chain_refusal_map():
    """
    design pk -> ``(refusal_kind, ancestor_pk)`` for every design whose
    ``based_on`` lineage the §9.2 all-or-nothing rule drops WHOLE, or ``None``
    when the design has no chain or its whole chain is clean.

    Mirrors the rule ``projection.resolve_baseline_chain`` applies to ONE
    design -- an ``implemented`` or not-``approved`` ancestor anywhere in the
    lineage refuses every layer stacked on top of it, and a lineage cycle
    refuses the same way -- but answers it for EVERY design at once: ONE query
    fetching ``(pk, based_on_id, status)`` for the whole install, followed by
    a pure-Python, memoized graph walk. Calling ``design.baseline_chain()``
    (itself one query per ancestor hop, since ``based_on`` is a live FK) once
    per design here would be N designs * M ancestors deep queries -- this is
    the query-count risk the task calls out, and the reason this function
    exists rather than a loop over ``resolve_baseline_chain``.

    The walk resolves oldest-ancestor-first (matching
    ``resolve_baseline_chain``'s iteration order): for a design whose parent
    is itself fine, the reported refusal (if any) is whichever ancestor is
    CLOSEST TO THE ROOT among the bad ones, not the nearest parent -- exactly
    what a chain "contributed whole or not at all" implies, since the layers
    stacked on a bad ancestor were planned against a result that was itself
    never trustworthy.
    """
    by_pk = {
        row["pk"]: row
        for row in models.Design.objects.values("pk", "based_on_id", "status")
    }
    memo = {}

    def resolve(pk, stack):
        if pk in memo:
            return memo[pk]
        if pk in stack:
            # A genuine lineage cycle in existing data (baseline_chain() would
            # raise ValueError walking it) -- not memoized here; the frame
            # that owns this pk's OWN top-level call finishes normally and
            # memoizes it below.
            return ("chain_broken", None)
        row = by_pk.get(pk)
        parent_id = row["based_on_id"] if row else None
        if parent_id is None or parent_id not in by_pk:
            result = None
        else:
            upstream = resolve(parent_id, stack | {pk})
            if upstream is not None:
                result = upstream
            else:
                parent_status = by_pk[parent_id]["status"]
                if parent_status == DesignStatusChoices.STATUS_IMPLEMENTED:
                    result = ("ancestor_implemented", parent_id)
                elif parent_status != DesignStatusChoices.STATUS_APPROVED:
                    result = ("ancestor_not_approved", parent_id)
                else:
                    result = None
        memo[pk] = result
        return result

    return {pk: resolve(pk, frozenset()) for pk in by_pk}


def _chain_health_detail(kind, ancestor):
    """The sentence a human reads for one flagged design's chain issue.

    Mirrors ``projection._ancestor_refusal``'s reasoning in the report's own
    words -- the two must not disagree about WHY, only about audience (this
    is a summary row, not the editor's persistent panel).
    """
    if kind == "ancestor_implemented":
        return (
            f"{ancestor} is marked implemented: reality may already contain "
            f"part of its layer, so replaying it would double-count those "
            f"changes. Re-base past it."
        )
    if kind == "ancestor_not_approved":
        return (
            f"{ancestor} is {ancestor.get_status_display().lower()}, not "
            f"approved: its placements are still free to change. Re-base "
            f"once it is approved, or onto another parent."
        )
    return (
        "This design's lineage cannot be resolved (a cycle). Re-base onto a "
        "valid parent."
    )


def _chain_health_rows(user):
    """
    One row per design needing attention, restricted to what ``user`` may
    view: a refused chain (G4/§9.2) or inert (stale) placements.

    Query budget, independent of how many designs exist:
    1. ``_chain_refusal_map()``'s one ``values()`` query.
    2. One aggregate query for stale-placement counts (proportional to how
       many designs actually HAVE a stale row, not to the total design count).
    3. One ``restrict()``-ed query for the visible flagged designs.
    4. At most one more query for the (few) ancestors those refusals name.
    """
    refusals = _chain_refusal_map()
    refused_pks = {pk for pk, v in refusals.items() if v is not None}

    stale_counts = dict(
        models.DesignPlacement.objects.filter(stale=True)
        .values("design_id")
        .annotate(n=Count("id"))
        .values_list("design_id", "n")
    )

    candidate_pks = refused_pks | set(stale_counts)
    if not candidate_pks:
        return []

    visible_designs = {
        d.pk: d
        for d in models.Design.objects.restrict(user, "view")
        .filter(pk__in=candidate_pks)
        .select_related("site")
    }
    if not visible_designs:
        return []

    needed_ancestor_pks = {
        refusals[pk][1]
        for pk in visible_designs
        if refusals.get(pk) is not None and refusals[pk][1] is not None
    }
    ancestors = (
        {d.pk: d for d in models.Design.objects.filter(pk__in=needed_ancestor_pks)}
        if needed_ancestor_pks else {}
    )

    rows = []
    for pk, design in visible_designs.items():
        refusal = refusals.get(pk)
        reasons = []
        if refusal is not None:
            kind, ancestor_pk = refusal
            ancestor = ancestors.get(ancestor_pk) if ancestor_pk is not None else None
            reasons.append({
                "kind": kind,
                "ancestor": ancestor,
                "detail": _chain_health_detail(kind, ancestor),
            })
        rows.append({
            "design": design,
            "site": design.site,
            "reasons": reasons,
            "stale_count": stale_counts.get(pk, 0),
        })
    rows.sort(key=lambda r: r["design"].title.lower())
    return rows


class DesignChainHealthView(ContentTypePermissionRequiredMixin, View):
    """
    Standalone "Chain Health" REPORT page (not bound to any single object).

    URL: /plugins/rack-design/chain-health/
    Name: plugins:netbox_rack_design:design_chain_health

    Answers "which of my designs need attention right now" across every
    design at once (PLAN-design-chains.md G4's reporting half): a design
    whose ``based_on`` chain is refused (an implemented or not-yet-approved
    ancestor, or a broken lineage, §9.2) and/or a design carrying inert
    (stale) placements. Everything here already exists PER-DESIGN -- the
    "Design chain" card and stale-placements card on design.html, and the
    editor's ``chain_conflicts`` panel -- so this page does not restate
    those; each row links straight into the fix (re-base, or the design's
    placements filtered to its stale rows) and into the design itself.

    A healthy install (the normal case) shows a quiet empty state, not an
    alarming empty table -- see ``chain_health.html``.

    Gated by ``netbox_rack_design.view_design`` via
    ``ContentTypePermissionRequiredMixin`` (login required, same as the
    Elevations browser), and every row is additionally restricted to designs
    ``user`` has object-level view permission on, via
    ``Design.objects.restrict()`` inside ``_chain_health_rows`` -- a chain
    conflict naming an ancestor the user cannot view is still safe to show
    (the same is already true of the editor's ``chain_conflicts`` panel,
    which never permission-checks ``source_design``), but the flagged design
    itself never leaks to someone without view rights on it.
    """

    template_name = "netbox_rack_design/chain_health.html"

    def get_required_permission(self):
        return "netbox_rack_design.view_design"

    def get(self, request):
        rows = _chain_health_rows(request.user)

        table = tables.ChainHealthTable(rows)
        RequestConfig(request, {
            "paginator_class": EnhancedPaginator,
            "per_page": get_paginate_count(request),
        }).configure(table)

        return render(request, self.template_name, {
            "table": table,
            "row_count": len(rows),
        })
