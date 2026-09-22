"""Forms for NetBox Rack Design."""

from dcim.choices import PowerFeedPhaseChoices, PowerFeedSupplyChoices
from dcim.models import (
    Device,
    DeviceBay,
    DeviceRole,
    DeviceType,
    Location,
    Manufacturer,
    Rack,
    Site,
)
from django import forms
from django.utils.translation import gettext_lazy as _
from netbox.forms import (
    NetBoxModelBulkEditForm,
    NetBoxModelFilterSetForm,
    NetBoxModelForm,
    NetBoxModelImportForm,
)
from tenancy.models import Tenant
from utilities.forms.fields import (
    CSVChoiceField,
    CSVModelChoiceField,
    CSVModelMultipleChoiceField,
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
)
from utilities.forms.rendering import FieldSet

from .choices import DesignPlacementKindChoices, DesignStatusChoices, TemplatePlacementAnchorChoices
from .models import (
    Design,
    DesignGroup,
    DesignPlacement,
    DesignPowerFeed,
    PlannedRack,
    Template,
    TemplateGroup,
    TemplatePlacement,
)

__all__ = (
    "DesignGroupForm",
    "DesignForm",
    "DesignPlacementForm",
    "DesignPowerFeedForm",
    "PlannedRackForm",
    "TemplateGroupForm",
    "TemplateForm",
    "TemplatePlacementForm",
    "DesignGroupImportForm",
    "DesignImportForm",
    "DesignPlacementImportForm",
    "DesignPowerFeedImportForm",
    "DesignGroupBulkEditForm",
    "DesignBulkEditForm",
    "DesignPlacementBulkEditForm",
    "DesignPowerFeedBulkEditForm",
    "PlannedRackBulkEditForm",
    "PlannedRackImportForm",
    "TemplateImportForm",
    "TemplateGroupImportForm",
    "TemplatePlacementImportForm",
    "TemplateBulkEditForm",
    "TemplateGroupBulkEditForm",
    "TemplatePlacementBulkEditForm",
    "DesignGroupFilterForm",
    "DesignFilterForm",
    "DesignPlacementFilterForm",
    "DesignPowerFeedFilterForm",
    "ElevationBrowserFilterForm",
    "DesignEditorPaletteForm",
    "DesignEditorAddRackForm",
)


# ---------------------------------------------------------------------------
# Interactive editor left-rail selectors
# ---------------------------------------------------------------------------


class DesignEditorPaletteForm(forms.Form):
    """
    Drives the editor's left-rail selectors as NetBox-native, API-backed
    searchable selects (DynamicModelChoiceField → APISelect widget). NetBox's
    bundled select-init enhances these into TomSelect-with-remote-load, so typing
    queries the API live (unlike a plain <select> we populate after page load).

    These are NOT bound to a model — they're transient UI controls:
      • manufacturer filters the device-type catalog search (manufacturer_id);
      • device_role / tenant are applied to NEW adds at drop time.
    The editor JS reads each field's value by its Django widget id (id_<name>).
    """

    manufacturer = DynamicModelChoiceField(
        queryset=Manufacturer.objects.all(),
        required=False,
        label=_("Manufacturer"),
    )
    device_role = DynamicModelChoiceField(
        queryset=DeviceRole.objects.all(),
        required=False,
        label=_("Device role"),
    )
    tenant = DynamicModelChoiceField(
        queryset=Tenant.objects.all(),
        required=False,
        label=_("Tenant"),
    )


class DesignEditorAddRackForm(forms.Form):
    """
    Drives the editor's "Add rack" panel: a Site → Location → Rack chain of
    NetBox-native, API-backed searchable selects (DynamicModelChoiceField →
    APISelect → TomSelect with remote load). Like DesignEditorPaletteForm these
    are transient UI controls (NOT bound to a model); the panel JS reads the
    rack field's value and POSTs it to the design's add-rack endpoint.

    Scoping (PLAN-multi-site.md M8, set per-design in __init__):
      • ``add_site`` is a live picker over EVERY site, not just the design's
        own (user ruling 2026-09-22). A design covers one or more sites (M1)
        and this panel is the only place a planner reaches for a rack, so
        picking one across the hall is how a design becomes multi-site --
        the add-rack endpoint adds the rack's site to the design. With
        exactly one site so far it is pre-selected via ``initial``, which is
        a starting point, not a restriction.
      • ``add_location`` is chained to the chosen site (site_id=$add_site);
      • ``add_rack`` is chained to both the chosen site and the chosen location
        (site_id=$add_site, location_id=$add_location), so picking a location
        narrows the racks further.
    Location and Rack start out ``disabled`` in the rendered widget attrs; the
    editor JS (editor_panels.js) removes the attribute once ``add_site`` has a
    value and re-disables + clears both whenever ``add_site`` changes -- the
    dynamic query params above already make TomSelect reload/clear a field's
    OWN options when a dependency changes, but they don't touch the HTML
    ``disabled`` state, which is JS's job.
    Field names are prefixed ``add_`` so their rendered ids never collide with the
    palette form's selects on the same page.
    """

    add_site = DynamicModelChoiceField(
        queryset=Site.objects.all(),
        required=True,
        label=_("Site"),
    )
    add_location = DynamicModelChoiceField(
        queryset=Location.objects.all(),
        required=False,
        label=_("Location"),
        query_params={"site_id": "$add_site"},
    )
    add_rack = DynamicModelChoiceField(
        queryset=Rack.objects.all(),
        required=False,
        label=_("Rack"),
        query_params={"site_id": "$add_site", "location_id": "$add_location"},
    )

    def __init__(self, *args, sites=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Location/Rack are chained to add_site via the query_params declared
        # above; start them out HTML-disabled too so a design with more than
        # one site can't be typed into before a site is picked (JS lifts the
        # attribute once add_site has a value -- see editor_panels.js).
        self.fields["add_location"].widget.attrs["disabled"] = "disabled"
        self.fields["add_rack"].widget.attrs["disabled"] = "disabled"
        # Pre-select the design's site when it has exactly one -- the common
        # case, and it saves a click. Deliberately `initial` only: the field
        # keeps its full `Site.objects.all()` queryset and an unpinned remote
        # list, so the planner can pick ANY site and pull a rack from it (the
        # add-rack endpoint then widens the design). Pinning the widget to
        # the design's own sites was tried first and is what made this a
        # dead end: a design created with one site could never reach a
        # second one from the editor.
        if sites is not None:
            site_qs = sites.all() if hasattr(sites, "all") else sites
            site_list = list(site_qs[:2])
            if len(site_list) == 1:
                site = site_list[0]
                self.fields["add_site"].initial = site.pk
                # A pre-filled site means Location/Rack start ENABLED: there
                # is already a site to filter them by. Changing the site
                # re-disables and clears both (editor_panels.js).
                del self.fields["add_location"].widget.attrs["disabled"]
                del self.fields["add_rack"].widget.attrs["disabled"]


# ---------------------------------------------------------------------------
# Model forms
# ---------------------------------------------------------------------------


class DesignGroupForm(NetBoxModelForm):
    parent = DynamicModelChoiceField(queryset=DesignGroup.objects.all(), required=False)

    class Meta:
        model = DesignGroup
        fields = ("name", "parent", "description", "link", "tags")


class DesignForm(NetBoxModelForm):
    # M9 (PLAN-multi-site.md): a design now covers one OR MORE sites. `racks`
    # options are filtered live to whichever sites are chosen so far via
    # query_params (chained on this field's value).
    sites = DynamicModelMultipleChoiceField(
        queryset=Site.objects.all(),
        label=_("Sites"),
    )
    group = DynamicModelChoiceField(queryset=DesignGroup.objects.all(), required=False)
    # Only an approved design is derivable (PLAN-design-chains.md §2.2): its
    # placements are frozen the moment it can be a parent, which is exactly
    # what makes baselining on it safe. Left UNFILTERED by site (M5,
    # PLAN-multi-site.md): the "child and parent must share at least one
    # site" rule is validated below in clean() (and by `Design.clean()`),
    # not enforced by narrowing the picker's options -- a design with several
    # sites can legitimately base on a parent that shares only one of them.
    based_on = DynamicModelChoiceField(
        queryset=Design.objects.filter(status=DesignStatusChoices.STATUS_APPROVED),
        required=False,
        label=_("Based on"),
        help_text=_(
            "An approved design whose result this design is planned on top of. "
            "This design's baseline inherits every placement made by the "
            "selected design, as if it had already happened."
        ),
        query_params={
            "status": DesignStatusChoices.STATUS_APPROVED,
        },
    )
    depends_on = DynamicModelMultipleChoiceField(queryset=Design.objects.all(), required=False)
    # Racks this design plans across. Options are filtered live to the chosen
    # sites via query_params (chained on the `sites` field's value).
    racks = DynamicModelMultipleChoiceField(
        queryset=Rack.objects.all(),
        required=False,
        label=_("Racks"),
        query_params={"site_id": "$sites"},
    )

    # `based_on` sits in the primary "Design" fieldset, not "Lineage &
    # scheduling": picking a parent is a structural choice made at creation
    # time (PLAN-design-chains.md §5, "choosing what a design is based on is
    # part of creating it"), not scheduling metadata like `depends_on` /
    # `sequence`.
    fieldsets = (
        FieldSet(
            "title", "sites", "based_on", "status", "summary", "link", "racks",
            name=_("Design"),
        ),
        FieldSet(
            "group", "depends_on", "sequence",
            name=_("Lineage & scheduling"),
        ),
        FieldSet("description", "tags", name=_("Tags")),
    )

    class Meta:
        model = Design
        fields = (
            "title", "sites", "status", "summary", "link", "racks",
            "group", "based_on", "depends_on", "sequence",
            "description", "comments", "tags",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A design must never be offered as its own parent.
        if self.instance.pk:
            self.fields["based_on"].queryset = (
                self.fields["based_on"].queryset.exclude(pk=self.instance.pk)
            )
            # M9: seed the multi-select with the instance's current sites --
            # a plain ModelForm only does this automatically for scalar
            # fields, not for a hand-declared DynamicModelMultipleChoiceField
            # that shadows the model's M2M.
            self.fields["sites"].initial = list(
                self.instance.sites.values_list("pk", flat=True)
            )

    def clean(self):
        super().clean()
        # Enforce the same-sites rule at the FORM layer so it also holds on
        # CREATE. The model's clean() can't see the M2M before the instance is
        # saved (no pk → no through-rows), so a brand-new design would otherwise
        # skip this check. Keep the message consistent with Design.clean().
        sites = self.cleaned_data.get("sites") or []
        site_ids = {s.pk for s in sites}
        racks = self.cleaned_data.get("racks")
        if site_ids and racks:
            offending = [rack for rack in racks if rack.site_id not in site_ids]
            if offending:
                names = ", ".join(str(rack) for rack in offending)
                self.add_error(
                    "racks",
                    _("These racks are not in any of the design's sites: %(names)s.")
                    % {"names": names},
                )
        # M5 (PLAN-multi-site.md): child and parent must share AT LEAST ONE
        # site -- a parent's placements are site-scoped, so a child that
        # shares none of the parent's sites could never actually inherit
        # anything from it. `Design.clean()` enforces this too (a plain FK
        # with no pk-timing gap, so the model check alone already covers
        # every other layer: API, GraphQL, bulk import), but this form-level
        # check names both sides' sites, which is a materially better
        # field-level error than the model's generic one.
        based_on = self.cleaned_data.get("based_on")
        if site_ids and based_on:
            parent_site_ids = set(based_on.sites.values_list("pk", flat=True))
            if not (site_ids & parent_site_ids):
                self.add_error(
                    "based_on",
                    _("The parent design's sites (%(parent_sites)s) share no "
                      "site with this design's sites (%(sites)s).")
                    % {
                        "parent_sites": ", ".join(str(s) for s in based_on.sites.all()),
                        "sites": ", ".join(str(s) for s in sites),
                    },
                )

        # A design's `racks` scope is part of what was approved
        # (PLAN-design-chains.md §2.2/G4): `Design.clean()` enforces this too,
        # but only sees a pending m2m change via `self._m2m_values` (the REST
        # API's own side channel, netbox/api/serializers/base.py) -- Django's
        # ModelForm never sets anything like that, so this form carries the
        # equivalent check itself. `self.instance` still holds its PRE-edit
        # field values here (`_post_clean()`, which applies the submitted
        # ones, runs AFTER this method), so `self.instance.is_frozen` and
        # `self.instance.racks.all()` both reflect what the design WAS before
        # this submission -- exactly the state that matters. Skipped on
        # CREATE (`self.instance.pk` is falsy): a brand-new design has no
        # approved scope yet to protect.
        if self.instance.pk and self.instance.is_frozen:
            new_racks = set(self.cleaned_data.get("racks") or [])
            old_racks = set(self.instance.racks.all())
            if new_racks != old_racks:
                self.add_error(
                    "racks",
                    _("This design is approved, and approved designs are "
                      "frozen: its rack scope cannot be changed. Set the "
                      "design back to draft, or use the New version button "
                      "on it, to make this change."),
                )

        return self.cleaned_data


class DesignPlacementForm(NetBoxModelForm):
    design = DynamicModelChoiceField(queryset=Design.objects.all())
    device = DynamicModelChoiceField(queryset=Device.objects.all(), required=False)
    device_type = DynamicModelChoiceField(queryset=DeviceType.objects.all(), required=False)
    target_rack = DynamicModelChoiceField(queryset=Rack.objects.all(), required=False)

    # --- device-bay targeting (a blade into a chassis) ---------------------
    # ``chassis`` is NOT a model field: it exists only to scope the bay picker.
    # A DeviceBay list is unusable unfiltered (an instance can hold thousands of
    # bays, most named "slot1".."slot8"), so the user picks the chassis first and
    # ``target_bay`` chains off it via query_params.
    chassis = DynamicModelChoiceField(
        queryset=Device.objects.filter(device_type__subdevice_role="parent"),
        required=False,
        label=_("Chassis"),
        help_text=_("Existing chassis to install this blade into. Narrows the bay list."),
        query_params={"rack_id": "$target_rack"},
    )
    target_bay = DynamicModelChoiceField(
        queryset=DeviceBay.objects.all(),
        required=False,
        label=_("Target bay"),
        query_params={"device_id": "$chassis"},
    )
    parent_placement = DynamicModelChoiceField(
        queryset=DesignPlacement.objects.all(),
        required=False,
        label=_("Planned chassis"),
        help_text=_("Use instead of Target bay when the chassis is itself added by this design."),
        query_params={"design_id": "$design"},
    )

    fieldsets = (
        FieldSet("design", "kind", "device", "device_type", "proposed_name",
                 "tags", name=_("Placement")),
        FieldSet("target_rack", "target_position", "target_face", name=_("Rack slot")),
        FieldSet("chassis", "target_bay", "parent_placement", "target_bay_name",
                 name=_("Device bay")),
    )

    class Meta:
        model = DesignPlacement
        fields = (
            "design", "kind", "device", "device_type", "proposed_name",
            "target_rack", "target_position", "target_face",
            "parent_placement", "target_bay", "target_bay_name", "tags",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Editing an existing bay placement: preselect the chassis so the bay
        # picker shows the right list instead of coming up empty.
        bay = self.initial.get("target_bay") or getattr(self.instance, "target_bay", None)
        if bay is not None and not self.initial.get("chassis"):
            self.initial["chassis"] = bay.device_id

    def clean(self):
        # NetBoxModelForm.clean() returns None, so read cleaned_data directly
        # rather than the super() return value.
        super().clean()
        cleaned = self.cleaned_data
        # Mirror the bay's name so a consumer has one field to read for "which
        # bay", whichever of the two cases produced it (models.DesignPlacement).
        bay = cleaned.get("target_bay")
        if bay is not None and not cleaned.get("target_bay_name"):
            cleaned["target_bay_name"] = bay.name
            self.instance.target_bay_name = bay.name
        return cleaned


# ---------------------------------------------------------------------------
# Import forms
# ---------------------------------------------------------------------------


class DesignGroupImportForm(NetBoxModelImportForm):
    parent = CSVModelChoiceField(
        queryset=DesignGroup.objects.all(),
        to_field_name="name",
        required=False,
        help_text="Parent group (by name)",
    )

    class Meta:
        model = DesignGroup
        fields = ("name", "parent", "description", "link", "tags")


class DesignImportForm(NetBoxModelImportForm):
    # M9 (PLAN-multi-site.md): a design covers one OR MORE sites; the CSV
    # column carries a comma-separated list of site SLUGS (`CSVModelMultipleChoiceField`
    # already splits on ',' -- see utilities.forms.fields.csv), e.g.
    # "site-a,site-b". Slug rather than name to match the other multi-value
    # CSV columns' convention of an unambiguous, URL-safe key.
    sites = CSVModelMultipleChoiceField(
        queryset=Site.objects.all(),
        to_field_name="slug",
        help_text="Assigned sites (by slug, comma-separated)",
    )
    status = CSVChoiceField(choices=DesignStatusChoices, required=False)
    group = CSVModelChoiceField(
        queryset=DesignGroup.objects.all(),
        to_field_name="name",
        required=False,
        help_text="Group (by name)",
    )

    class Meta:
        model = Design
        fields = (
            "title", "sites", "status", "summary", "link",
            "group", "sequence", "description", "comments", "tags",
        )


class DesignPlacementImportForm(NetBoxModelImportForm):
    design = CSVModelChoiceField(
        queryset=Design.objects.all(),
        to_field_name="title",
        help_text="Parent design (by title)",
    )
    kind = CSVChoiceField(choices=DesignPlacementKindChoices)
    device = CSVModelChoiceField(
        queryset=Device.objects.all(),
        to_field_name="name",
        required=False,
        help_text="Existing device (by name)",
    )
    device_type = CSVModelChoiceField(
        queryset=DeviceType.objects.all(),
        to_field_name="model",
        required=False,
        help_text="Device type (by model)",
    )
    target_rack = CSVModelChoiceField(
        queryset=Rack.objects.all(),
        to_field_name="name",
        required=False,
        help_text="Target rack (by name)",
    )

    parent_placement = CSVModelChoiceField(
        queryset=DesignPlacement.objects.all(),
        to_field_name="proposed_name",
        required=False,
        help_text="Placement of the chassis this blade goes into, when the chassis "
                  "is also planned (by proposed name)",
    )

    class Meta:
        model = DesignPlacement
        fields = (
            "design", "kind", "device", "device_type", "proposed_name",
            "target_rack", "target_position", "target_face",
            "parent_placement", "target_bay_name", "tags",
        )


# ---------------------------------------------------------------------------
# Bulk edit forms
# ---------------------------------------------------------------------------


class DesignGroupBulkEditForm(NetBoxModelBulkEditForm):
    parent = DynamicModelChoiceField(queryset=DesignGroup.objects.all(), required=False)
    description = forms.CharField(max_length=200, required=False)
    link = forms.URLField(required=False)

    model = DesignGroup
    nullable_fields = ("parent", "description", "link")


class DesignBulkEditForm(NetBoxModelBulkEditForm):
    status = forms.ChoiceField(choices=DesignStatusChoices, required=False)
    summary = forms.CharField(max_length=200, required=False)
    group = DynamicModelChoiceField(queryset=DesignGroup.objects.all(), required=False)
    description = forms.CharField(max_length=200, required=False)
    # M9 (PLAN-multi-site.md): a plain M2M field, not add_sites/remove_sites.
    # The generic BulkEditView already handles a bare M2M form field natively
    # (bulk_views.py: it REPLACES the relation via `.set()` when a value is
    # submitted, and leaves it untouched when left blank) -- exactly the
    # "add or remove" affordance a picker with the object's *current* sites
    # pre-selected gives an operator, without a custom `post_save_operations`
    # override in views.py (out of scope for this change: only forms.py is
    # touched here). Deliberately NOT in `nullable_fields` -- a design
    # requires at least one site (M1), and the `_nullify` checkbox would let
    # an operator clear it to zero; `Design.clean()` rejects that via
    # `_m2m_values` before the M2M is actually written.
    sites = DynamicModelMultipleChoiceField(
        queryset=Site.objects.all(), required=False, label=_("Sites"),
    )

    model = Design
    nullable_fields = ("summary", "group", "description")


class DesignPlacementBulkEditForm(NetBoxModelBulkEditForm):
    proposed_name = forms.CharField(max_length=64, required=False)
    target_face = forms.CharField(max_length=10, required=False)
    target_bay_name = forms.CharField(max_length=64, required=False)

    model = DesignPlacement
    nullable_fields = ("proposed_name", "target_face", "target_bay_name")


# ---------------------------------------------------------------------------
# Filter forms
# ---------------------------------------------------------------------------


class DesignGroupFilterForm(NetBoxModelFilterSetForm):
    model = DesignGroup
    parent_id = DynamicModelMultipleChoiceField(queryset=DesignGroup.objects.all(), required=False, label="Parent")


class DesignFilterForm(NetBoxModelFilterSetForm):
    model = Design
    site_id = DynamicModelMultipleChoiceField(queryset=Site.objects.all(), required=False, label="Site")
    group_id = DynamicModelMultipleChoiceField(queryset=DesignGroup.objects.all(), required=False, label="Group")
    status = forms.MultipleChoiceField(choices=DesignStatusChoices, required=False)


class DesignPlacementFilterForm(NetBoxModelFilterSetForm):
    model = DesignPlacement
    # Only device_role_id/tenant_id are surfaced here from the newer
    # filtersets.py additions: they're the two planning attributes a user sets
    # on every ADD, so "show me every planned compute node" / "every placement
    # for Tenant X" are real filters-tab searches. The power FKs
    # (power_source_device_id, real_power_feed_id, planned_power_feed_id) and
    # the chain references (base_placement_id, base_parent_placement_id) stay
    # off the form: they exist for {% htmx_table %} embeds and the REST API
    # (scoping a related table to "this PDU" / "this ancestor placement" from
    # the OTHER object's detail page), not for a planner filtering placements
    # from this list by hand. target_position/target_face are plain
    # exact-match Meta.fields filters (a decimal U and a short code) with no
    # meaningful select widget -- nobody filters "give me everything at U12";
    # they look at the rack elevation instead.
    fieldsets = (
        FieldSet("q", "filter_id"),
        FieldSet("design_id", "kind", name=_("Placement")),
        FieldSet("device_role_id", "tenant_id", name=_("Planning")),
        FieldSet("target_rack_id", name=_("Rack slot")),
        FieldSet("target_bay_id", "parent_placement_id", name=_("Device bay")),
    )
    design_id = DynamicModelMultipleChoiceField(queryset=Design.objects.all(), required=False, label=_("Design"))
    kind = forms.MultipleChoiceField(choices=DesignPlacementKindChoices, required=False)
    device_role_id = DynamicModelMultipleChoiceField(
        queryset=DeviceRole.objects.all(), required=False, label=_("Device role")
    )
    tenant_id = DynamicModelMultipleChoiceField(
        queryset=Tenant.objects.all(), required=False, label=_("Tenant")
    )
    target_rack_id = DynamicModelMultipleChoiceField(
        queryset=Rack.objects.all(), required=False, label=_("Target rack")
    )
    target_bay_id = DynamicModelMultipleChoiceField(
        queryset=DeviceBay.objects.all(), required=False, label=_("Target bay")
    )
    parent_placement_id = DynamicModelMultipleChoiceField(
        queryset=DesignPlacement.objects.all(), required=False, label=_("Planned chassis")
    )


# ---------------------------------------------------------------------------
# Elevation browser (standalone, non-model-bound) filter form
# ---------------------------------------------------------------------------


class ElevationBrowserFilterForm(forms.Form):
    """
    GET-driven, multi-select filter for the standalone Elevations LIST page.

    Not a NetBoxModelFilterSetForm (the rows are derived (design, rack) pairs, not
    a single model's queryset) — a plain form whose four MULTI-select fields
    populate the ``design``, ``rack``, ``site`` and ``status`` query-string params.
    ElevationBrowserView applies them server-side to the derived rows; within a
    field the values are OR'd, across fields AND'd, and an empty field is no
    constraint.

    The OFFERED options are narrowed SERVER-SIDE to the current selection: the view
    passes the narrowed Design/Rack/Site querysets and Status choices (computed
    from the actual elevation rows) into __init__ so the form never offers a value
    that would yield zero rows. Plain Model/MultipleChoice fields are used (not the
    API-backed Dynamic* fields) precisely so the limited querysets/choices — not
    the full core catalog — drive the rendered <select> options. NetBox's frontend
    still upgrades each plain <select multiple> to the native TomSelect multi-select.
    """

    design = forms.ModelMultipleChoiceField(
        queryset=Design.objects.none(),
        required=False,
        label=_("Design"),
        to_field_name="pk",
    )
    rack = forms.ModelMultipleChoiceField(
        queryset=Rack.objects.none(),
        required=False,
        label=_("Rack"),
        to_field_name="pk",
    )
    site = forms.ModelMultipleChoiceField(
        queryset=Site.objects.none(),
        required=False,
        label=_("Site"),
        to_field_name="pk",
    )
    status = forms.MultipleChoiceField(
        choices=(),
        required=False,
        label=_("Status"),
    )

    fieldsets = (
        FieldSet("design", "rack", "site", "status", name=_("Elevations")),
    )

    def __init__(self, *args, design_qs=None, rack_qs=None, site_qs=None, status_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        if design_qs is not None:
            self.fields["design"].queryset = design_qs
        if rack_qs is not None:
            self.fields["rack"].queryset = rack_qs
        if site_qs is not None:
            self.fields["site"].queryset = site_qs
        if status_choices is not None:
            self.fields["status"].choices = status_choices


# ---------------------------------------------------------------------------
# Planned power feeds (docs/pdu-distribution-spec.md §6.1)
#
# The editor writes these from the rack-power and PDU-bind dialogs; these forms
# are the way to inspect, correct and delete one outside the editor.
# ---------------------------------------------------------------------------


class DesignPowerFeedForm(NetBoxModelForm):
    design = DynamicModelChoiceField(queryset=Design.objects.all())
    rack = DynamicModelChoiceField(
        queryset=Rack.objects.all(),
        help_text=_("The rack this planned feed supplies."),
    )

    fieldsets = (
        FieldSet("design", "rack", "name", "tags", name=_("Feed")),
        FieldSet("voltage", "amperage", "phase", "supply", name=_("Electrical")),
    )

    class Meta:
        model = DesignPowerFeed
        fields = (
            "design", "rack", "name", "voltage", "amperage", "phase", "supply",
            "tags",
        )


class DesignPowerFeedImportForm(NetBoxModelImportForm):
    design = CSVModelChoiceField(
        queryset=Design.objects.all(), to_field_name="title",
        help_text=_("Design (by title)"),
    )
    rack = CSVModelChoiceField(
        queryset=Rack.objects.all(), to_field_name="name",
        help_text=_("Rack (by name)"),
    )

    class Meta:
        model = DesignPowerFeed
        fields = (
            "design", "rack", "name", "voltage", "amperage", "phase", "supply",
            "tags",
        )


class DesignPowerFeedBulkEditForm(NetBoxModelBulkEditForm):
    rack = DynamicModelChoiceField(queryset=Rack.objects.all(), required=False)
    voltage = forms.IntegerField(required=False, min_value=1)
    amperage = forms.IntegerField(required=False, min_value=1)
    phase = forms.ChoiceField(choices=PowerFeedPhaseChoices, required=False)
    supply = forms.ChoiceField(choices=PowerFeedSupplyChoices, required=False)

    model = DesignPowerFeed
    fieldsets = (
        FieldSet("rack", name=_("Feed")),
        FieldSet("voltage", "amperage", "phase", "supply", name=_("Electrical")),
    )
    nullable_fields = ()


class DesignPowerFeedFilterForm(NetBoxModelFilterSetForm):
    model = DesignPowerFeed
    design_id = DynamicModelMultipleChoiceField(
        queryset=Design.objects.all(), required=False, label="Design")
    rack_id = DynamicModelMultipleChoiceField(
        queryset=Rack.objects.all(), required=False, label="Rack")
    phase = forms.MultipleChoiceField(choices=PowerFeedPhaseChoices, required=False)
    supply = forms.MultipleChoiceField(choices=PowerFeedSupplyChoices, required=False)


# ---------------------------------------------------------------------------
# PlannedRack (PLAN-templates.md §1) -- a rack that does not exist in NetBox
# yet. The model form plus a bulk-edit form: the list view renders NetBox's
# "Edit Selected"/"Delete Selected" buttons for any table with a checkbox
# column, so the views behind them have to exist or the form posts to a
# `None` URL (user report 2026-09-22). No import form yet.
# ---------------------------------------------------------------------------


class PlannedRackForm(NetBoxModelForm):
    location = DynamicModelChoiceField(
        queryset=Location.objects.all(),
        help_text=_(
            "Required: identity is (location, name), the same uniqueness "
            "dcim.Rack itself enforces -- see PlannedRack's docstring."
        ),
    )

    fieldsets = (
        FieldSet("name", "location", "u_height", "tags", name=_("Planned rack")),
        FieldSet("description", "comments", name=_("Notes")),
    )

    class Meta:
        model = PlannedRack
        fields = (
            "name", "location", "u_height", "description", "comments", "tags",
        )


# ---------------------------------------------------------------------------
# Templates (PLAN-templates.md §2) -- a reusable rack layout with no site.
# Stamping (Phase 3) is not implemented yet, so these forms cover only the
# ordinary create/edit path.
# ---------------------------------------------------------------------------


class PlannedRackImportForm(NetBoxModelImportForm):
    location = CSVModelChoiceField(
        queryset=Location.objects.all(), to_field_name="name",
        help_text=_("Location (by name) -- with the name, this is the rack's identity"),
    )

    class Meta:
        model = PlannedRack
        fields = ("name", "location", "u_height", "description", "comments", "tags")


class TemplateGroupImportForm(NetBoxModelImportForm):
    class Meta:
        model = TemplateGroup
        fields = ("name", "description", "tags")


class TemplateImportForm(NetBoxModelImportForm):
    group = CSVModelChoiceField(
        queryset=TemplateGroup.objects.all(), to_field_name="name", required=False,
        help_text=_("Template group (by name)"),
    )

    class Meta:
        model = Template
        fields = ("name", "group", "order", "u_height", "description", "tags")


class TemplatePlacementImportForm(NetBoxModelImportForm):
    template = CSVModelChoiceField(
        queryset=Template.objects.all(), to_field_name="name",
        help_text=_("Template (by name)"),
    )
    device_type = CSVModelChoiceField(
        queryset=DeviceType.objects.all(), to_field_name="model",
        help_text=_("Device type (by model)"),
    )
    device_role = CSVModelChoiceField(
        queryset=DeviceRole.objects.all(), to_field_name="name", required=False,
        help_text=_("Device role (by name)"),
    )
    tenant = CSVModelChoiceField(
        queryset=Tenant.objects.all(), to_field_name="name", required=False,
        help_text=_("Tenant (by name)"),
    )
    anchor = CSVChoiceField(
        choices=TemplatePlacementAnchorChoices,
        help_text=_("Which end of the rack the placement is measured from"),
    )

    class Meta:
        model = TemplatePlacement
        fields = (
            "template", "device_type", "device_role", "tenant", "anchor",
            "order", "face", "tags",
        )


class PlannedRackBulkEditForm(NetBoxModelBulkEditForm):
    """Only the fields a batch can sensibly share.

    ``name`` and ``location`` are deliberately absent: together they are the
    model's identity (D4), and setting one name across a selection would
    collide by definition. ``u_height`` is a physical fact worth fixing in
    bulk when a whole row was drafted at the wrong height.
    """

    model = PlannedRack
    u_height = forms.IntegerField(required=False, label=_("U height"))
    description = forms.CharField(required=False, max_length=200)

    nullable_fields = ("description",)


class TemplateGroupBulkEditForm(NetBoxModelBulkEditForm):
    model = TemplateGroup
    description = forms.CharField(required=False, max_length=200)

    nullable_fields = ("description",)



class TemplateBulkEditForm(NetBoxModelBulkEditForm):
    model = Template
    group = DynamicModelChoiceField(queryset=TemplateGroup.objects.all(), required=False)
    u_height = forms.IntegerField(required=False, label=_("U height"))
    description = forms.CharField(required=False, max_length=200)

    nullable_fields = ("group", "description")


class TemplatePlacementBulkEditForm(NetBoxModelBulkEditForm):
    model = TemplatePlacement
    device_role = DynamicModelChoiceField(queryset=DeviceRole.objects.all(), required=False)
    tenant = DynamicModelChoiceField(queryset=Tenant.objects.all(), required=False)
    description = forms.CharField(required=False, max_length=200)

    nullable_fields = ("device_role", "tenant", "description")


class TemplateGroupForm(NetBoxModelForm):
    fieldsets = (
        FieldSet("name", "description", "tags", name=_("Template group")),
    )

    class Meta:
        model = TemplateGroup
        fields = ("name", "description", "tags")


class TemplateForm(NetBoxModelForm):
    group = DynamicModelChoiceField(queryset=TemplateGroup.objects.all(), required=False)

    fieldsets = (
        FieldSet("name", "group", "order", "u_height", "tags", name=_("Template")),
        FieldSet("description", name=_("Notes")),
    )

    class Meta:
        model = Template
        fields = ("name", "group", "order", "u_height", "description", "tags")


class TemplatePlacementForm(NetBoxModelForm):
    template = DynamicModelChoiceField(queryset=Template.objects.all())
    device_type = DynamicModelChoiceField(queryset=DeviceType.objects.all())
    device_role = DynamicModelChoiceField(queryset=DeviceRole.objects.all(), required=False)
    tenant = DynamicModelChoiceField(queryset=Tenant.objects.all(), required=False)
    # The chassis this blade goes into, scoped to the same template -- there is
    # no cross-template case here, unlike DesignPlacement's ancestor-design
    # variant (a template never references another template's rows, D10).
    parent_placement = DynamicModelChoiceField(
        queryset=TemplatePlacement.objects.all(),
        required=False,
        label=_("Parent (chassis) placement"),
        query_params={"template_id": "$template"},
    )

    fieldsets = (
        FieldSet(
            "template", "device_type", "device_role", "tenant", "label", "tags",
            name=_("Placement"),
        ),
        FieldSet("anchor", "order", "face", name=_("Rack slot")),
        FieldSet("parent_placement", "target_bay_name", name=_("Device bay")),
    )

    class Meta:
        model = TemplatePlacement
        fields = (
            "template", "device_type", "device_role", "tenant", "label",
            "anchor", "order", "face", "parent_placement", "target_bay_name",
            "tags",
        )
        widgets = {
            "anchor": forms.Select(choices=TemplatePlacementAnchorChoices),
        }
