"""
Models for NetBox Rack Design.

A *Design* is a proposed set of rack changes (one plan) that overlays on real
NetBox data without mutating it until applied. Designs are versioned
(clone-and-tweak; one approved version per plan), ordered for execution per
site, may declare explicit dependencies on other designs, and may optionally be
grouped into a larger (hierarchical) effort via DesignGroup.

All terminology is generic — no organization-specific concepts are hardcoded.
"""

from dcim.choices import PowerFeedPhaseChoices, PowerFeedSupplyChoices
from dcim.constants import RACK_U_HEIGHT_DEFAULT, RACK_U_HEIGHT_MAX
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from netbox.models import NetBoxModel

from . import planning_fields
from .choices import DesignPlacementKindChoices, DesignStatusChoices, TemplatePlacementAnchorChoices

__all__ = (
    "DesignGroup",
    "Design",
    "DesignPlacement",
    "DesignPowerFeed",
    "DesignRackPower",
    "FavoriteDeviceType",
    "FavoriteSet",
    "HiddenDesignRack",
    "HiddenDesignChassis",
    "PlannedRack",
    "resolve_rack",
    "rack_key",
    "TemplateGroup",
    "Template",
    "TemplatePlacement",
)

# The plugin's hosted documentation (MkDocs -> GitHub Pages). NetBoxModel's
# default ``docs_url`` points at ``/static/docs/models/...``, which only exists
# for NetBox's OWN core docs -- a plugin's docs are not built into that path. Per
# the plugin dev guide (Database Models: "Plugin models can override this to
# return a custom URL ... your plugin's documentation"), each model below
# overrides ``docs_url`` to this site so the object detail page's help link
# resolves instead of 404ing. Kept in sync with ``mkdocs.yml`` ``site_url``.
DOCS_BASE_URL = "https://ravenrs.github.io/netbox-rack-design/"


def _frozen_design_clean_message(what):
    """
    The message ``clean()`` raises when a write is rejected because the
    owning design is frozen (approved, PLAN-design-chains.md §2.2/G4).
    Shared by ``DesignPlacement.clean()`` and ``DesignPowerFeed.clean()`` so
    both raise identical wording rather than duplicating the sentence by
    hand. Cannot carry a link to the New version page the way the view-layer
    equivalent (``_frozen_design_message``, views.py) does: a model has no
    request context, and there is no reason to tie models.py to a specific
    URL name for it -- the caller (a form, view or API layer) is in a much
    better position to turn "the New version button" into an actual link if
    it wants one. ``what`` names the resource in the caller's own words,
    e.g. "its placements" or "its planned power feeds" -- mirrors the
    parameter of the same name on ``_frozen_design_message``.
    """
    return (
        f"This design is approved, and approved designs are frozen: {what} "
        "cannot be created or edited. Set the design back to draft, or use "
        "the New version button on it, to make this change."
    )


class DesignGroup(NetBoxModel):
    """
    An optional, hierarchical container that links designs into a larger effort
    (e.g. multi-stage work, or coordination across several sites). Purely
    organizational — it never affects execution order.
    """

    name = models.CharField(max_length=100, unique=True)
    parent = models.ForeignKey(
        to="self",
        on_delete=models.SET_NULL,
        related_name="children",
        blank=True,
        null=True,
    )
    description = models.CharField(max_length=200, blank=True)
    link = models.URLField(blank=True)

    class Meta:
        ordering = ("name",)
        verbose_name = "design group"
        verbose_name_plural = "design groups"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:netbox_rack_design:designgroup", args=[self.pk])

    @property
    def docs_url(self):
        return DOCS_BASE_URL

    def clean(self):
        super().clean()
        # Guard against cyclic parenting.
        ancestor = self.parent
        while ancestor is not None:
            if ancestor.pk == self.pk:
                raise ValidationError({"parent": "A group cannot be its own ancestor."})
            ancestor = ancestor.parent


class Design(NetBoxModel):
    """
    A proposed set of rack changes (one plan / one version).

    Deliberately NOT a ``PrimaryModel``: that base contributes only ``description``
    and ``comments``, both declared below, and in NetBox 4.5 it also began carrying
    an ``owner`` FK (``OwnerMixin``). Inheriting a base whose field set changes
    between NetBox minors would make the plugin's own schema version-dependent --
    the same design would need a migration on 4.5+ that is invalid on 4.4. Owning
    the two fields here keeps the table identical across the supported range.
    """

    # Formerly inherited from PrimaryModel; unchanged definitions, so the database
    # columns (already materialized in migration 0001) stay exactly as they were.
    description = models.CharField(max_length=200, blank=True)
    comments = models.TextField(blank=True)

    title = models.CharField(max_length=200)
    # M2M (PLAN-multi-site.md M1): a design may span one or more sites -- a
    # rollout across two neighbouring sites, or one plan for a whole campus.
    # At least one is required (enforced in clean(), same M2M-timing caveat
    # as `racks`/`planned_racks` below: only checkable once persisted).
    sites = models.ManyToManyField(
        to="dcim.Site",
        related_name="rack_designs",
        help_text="Sites this design plans across. At least one is required.",
    )
    status = models.CharField(
        max_length=30,
        choices=DesignStatusChoices,
        default=DesignStatusChoices.STATUS_DRAFT,
    )
    summary = models.CharField(max_length=200, blank=True)
    link = models.URLField(blank=True)

    # --- versioning / lineage ------------------------------------------------
    version = models.PositiveIntegerField(default=1)
    root = models.ForeignKey(
        to="self",
        on_delete=models.CASCADE,
        related_name="versions",
        blank=True,
        null=True,
        help_text="The first version of this plan; groups all its versions. Null on the root itself.",
    )
    based_on = models.ForeignKey(
        to="self",
        on_delete=models.SET_NULL,
        related_name="derived_designs",
        blank=True,
        null=True,
        help_text="Another design this one was derived from.",
    )

    # --- execution ordering & dependencies -----------------------------------
    sequence = models.PositiveIntegerField(
        blank=True,
        db_index=True,
        help_text="Execution order within a site (lower runs earlier). Auto-assigned if blank.",
    )
    depends_on = models.ManyToManyField(
        to="self",
        symmetrical=False,
        related_name="dependents",
        blank=True,
    )

    # --- scoping --------------------------------------------------------------
    # The explicit set of racks this design plans across. Historically the racks
    # a design touched were only implicit (the distinct ``target_rack`` of its
    # placements); this makes the planning scope first-class. Note: the related
    # name is ``scoped_designs`` (not ``rack_designs``, which the ``site`` FK
    # above already claims on dcim.Site).
    racks = models.ManyToManyField(
        to="dcim.Rack",
        related_name="scoped_designs",
        blank=True,
        help_text="Racks this design plans across. Every rack must belong to the design's site.",
    )
    # The planned-rack counterpart of ``racks`` above (PLAN-templates.md T1.3):
    # a design may also plan across racks that do not exist in NetBox yet (see
    # ``PlannedRack``'s docstring). Kept as a SEPARATE M2M rather than merged
    # into ``racks`` via some real-or-planned union field, for the same reason
    # ``DesignPlacement`` gets two separate rack FKs instead of one
    # GenericForeignKey (PlannedRack's docstring, and the target_rack /
    # target_planned_rack split above): filtering, select_related and the REST
    # serializers all stay ordinary FK joins on both sides. Reusing the same
    # related_name (``scoped_designs``) as ``racks`` is fine here -- Django
    # only requires a related_name to be unique per TARGET model, and the
    # target here is ``PlannedRack``, not ``dcim.Rack``.
    planned_racks = models.ManyToManyField(
        to="netbox_rack_design.PlannedRack",
        related_name="scoped_designs",
        blank=True,
        help_text="Planned racks this design plans across.",
    )

    # --- optional grouping ----------------------------------------------------
    group = models.ForeignKey(
        to="netbox_rack_design.DesignGroup",
        on_delete=models.SET_NULL,
        related_name="designs",
        blank=True,
        null=True,
    )

    # `sites` (M2M) is cloned automatically by CloningMixin -- not listed here.
    clone_fields = ("status", "summary", "link", "group")

    class Meta:
        ordering = ("sequence", "pk")
        verbose_name = "design"
        verbose_name_plural = "designs"
        constraints = [
            models.UniqueConstraint(
                fields=("root", "version"),
                name="%(app_label)s_%(class)s_unique_root_version",
            ),
        ]

    def __str__(self):
        return f"{self.title} (v{self.version})"

    def get_absolute_url(self):
        return reverse("plugins:netbox_rack_design:design", args=[self.pk])

    @property
    def docs_url(self):
        return DOCS_BASE_URL

    def get_status_color(self):
        return DesignStatusChoices.colors.get(self.status)

    @property
    def version_root(self):
        """The root design that groups this plan's versions (self if this is the root)."""
        return self.root or self

    @property
    def site(self):
        """
        Read-only back-compat mirror of the old single-site FK (PLAN-multi-site.md
        M2): the single site when this design has exactly one, else ``None``.

        Used ONLY by naming tokens and display -- no plugin code path may rely
        on it for validation or writes; every "must be in the design's site"
        check reads ``self.sites`` instead (M3).
        """
        if self.pk is None:
            return None
        sites = list(self.sites.all()[:2])
        return sites[0] if len(sites) == 1 else None

    @property
    def sites_display(self):
        """
        Comma-joined site names, for contexts that want a display string for
        EVERY design regardless of how many sites it covers (PLAN-multi-site.md
        M9) -- unlike ``site`` above, which is None as soon as a design has
        more than one. Used by ``search.py``'s ``display_attrs`` (global search
        results show, not filter by, this).
        """
        if self.pk is None:
            return ""
        return ", ".join(self.sites.values_list("name", flat=True))

    @property
    def is_frozen(self):
        """
        True once this design is APPROVED.

        Approving a design is a commitment, and approval is also what makes a
        design derivable (another design may baseline on it via ``based_on``,
        PLAN-design-chains.md §2.2) -- so from that point its content must stop
        moving, or every downstream design silently rots. The escape hatch is
        the status itself: take the design back to draft to edit it (subject to
        the dependents guard in ``clean()`` below), or create a new version.
        """
        return self.status == DesignStatusChoices.STATUS_APPROVED

    @property
    def children(self):
        """
        Designs directly based on this one (``based_on`` pointing here),
        ordered deterministically (``Meta.ordering``).

        Named ``children`` rather than ``dependents``: ``dependents`` already
        names the reverse of the ``depends_on`` M2M -- an unrelated,
        informational "must run after" edge that does not affect baselining
        (PLAN-design-chains.md §2.1) -- and reusing it here for the
        ``based_on`` lineage would collide with that existing relation.

        Backs the lineage panel, the freeze message, and the un-approve guard
        in ``clean()`` below (dropping this design back to draft would move
        the ground under everything derived from it).
        """
        return self.derived_designs.all()

    def baseline_chain(self):
        """
        The ordered stack of ``based_on`` ancestors: oldest ancestor first,
        immediate parent last, excluding self.

        Consumed by a future layered projection (PLAN-design-chains.md G1): a
        child design's baseline is "reality + replay(ancestors)", and this is
        the replay order. Resolved live through the ``based_on`` FK chain
        rather than copied -- an ancestor is frozen the moment it can be a
        parent (``is_frozen``), so live inheritance and a snapshot are
        equivalent (§2.2), and there is nothing to freshen and no snapshot to
        go stale.

        Raises ``ValueError``, not ``ValidationError``: existing rows could
        already hold a cycle (nothing prevented one before ``clean()`` grew a
        guard), so any caller walking the chain -- not just a form/clean()
        context -- needs a plain exception naming every design in the loop,
        never an infinite loop.
        """
        chain = []
        seen = {self.pk}
        current = self.based_on
        while current is not None:
            if current.pk in seen:
                path = " -> ".join(str(d) for d in [*chain, current])
                raise ValueError(f"Cycle detected in design lineage: {path}")
            chain.append(current)
            seen.add(current.pk)
            current = current.based_on
        chain.reverse()
        return chain

    @property
    def stale_placements(self):
        """Placements whose reference vanished: a real device deleted from DCIM,
        or (G2) an ancestor design's planned 'add' that was itself cancelled.

        These rows are inert -- projection skips them, so they neither render nor
        collide -- but they are NOT nothing: each one is a change the planner
        intended that can no longer happen. They survive precisely so this list
        is answerable, and the design page reports it rather than letting the
        plan quietly shrink (the placement FK used to CASCADE, which deleted the
        rows outright and left no way to know anything was lost).
        """
        return self.placements.filter(stale=True).order_by("stale_device_name", "pk")

    def save(self, *args, **kwargs):
        # Auto-assign a gapped GLOBAL execution sequence on first save (M6):
        # it was "per site" only because a design had exactly one; a per-site
        # number has no single meaning for a design spanning several.
        if self.sequence is None:
            last = Design.objects.aggregate(models.Max("sequence")).get("sequence__max")
            self.sequence = (last or 0) + 10
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.based_on_id and self.based_on_id == self.pk:
            raise ValidationError({"based_on": "A design cannot be based on itself."})
        # Longer cycles (A -> B -> A, or deeper): baseline_chain() already walks
        # the ancestor chain to detect one (G7), so reuse it rather than
        # duplicating the walk here.
        if self.based_on_id:
            try:
                self.baseline_chain()
            except ValueError as exc:
                raise ValidationError({"based_on": str(exc)}) from exc

        # A chain across two DISJOINT sites is meaningless (PLAN-design-chains.md
        # gap 1): a parent's placements are site-scoped, so a child that shares
        # none of the parent's sites could never actually replay them into its
        # own racks. (PLAN-multi-site.md M5): the rule loosens from "same site"
        # to "child and parent share at least one site". ``sites`` is now a
        # M2M, so -- unlike the old scalar ``site_id`` -- it cannot be read on
        # an unsaved instance (no pk means no through-rows yet); this check
        # only runs once persisted (mirrors the ``racks`` M2M-timing caveat
        # below), so it covers edits, not CREATE -- the form/serializer layer
        # re-runs full_clean() post-save to enforce it on create, same as the
        # ``racks`` scope check.
        if self.pk and self.based_on_id:
            if not self.sites.filter(pk__in=self.based_on.sites.values_list("pk", flat=True)).exists():
                raise ValidationError(
                    {"based_on": "The parent design shares no site with this "
                                  "design -- a chain across disjoint sites is "
                                  "meaningless."}
                )

        # depends_on cycle guard (G7): a many-to-many relation cannot be read on
        # an unsaved instance (pk=None) -- Django raises before the through-rows
        # exist -- so, mirroring the ``racks`` check below, this only runs once
        # the design is persisted (i.e. on edits; a brand-new design has no
        # dependents attached yet to form a cycle with).
        if self.pk:
            # DFS tracking the current recursion path's pks (not a flat "seen"
            # set collected across branches) so a revisit within ONE path is a
            # real cycle, an unrelated diamond (A depends on B and C, both
            # depending on D) is not mistaken for one, and a cycle that does
            # not happen to pass back through self (but is reachable from it)
            # still terminates instead of recursing forever.
            def _walk(node, path):
                if node.pk in {p.pk for p in path}:
                    chain = " -> ".join(str(d) for d in [*path, node])
                    raise ValidationError({"depends_on": f"Cycle in 'depends_on': {chain}"})
                for nxt in node.depends_on.all():
                    _walk(nxt, [*path, node])

            _walk(self, [])

        # At most one approved version per plan (root group). A brand-new, unsaved
        # root (pk=None, root=None) has no persisted version group yet, so there is
        # nothing it can conflict with -- and querying with an unsaved instance would
        # raise ValueError. Only run the sibling check once the root is persisted.
        if self.status == DesignStatusChoices.STATUS_APPROVED and self.version_root.pk is not None:
            root = self.version_root
            siblings = Design.objects.filter(
                models.Q(root=root) | models.Q(pk=root.pk)
            ).filter(status=DesignStatusChoices.STATUS_APPROVED)
            if self.pk:
                siblings = siblings.exclude(pk=self.pk)
            if siblings.exists():
                raise ValidationError(
                    "Another version of this plan is already approved. "
                    "Only one version may be approved at a time."
                )

        # Leaving 'approved' is blocked once something depends on this design
        # (§2.2): dropping it back to draft would silently move the ground
        # under every design baselined on it via `based_on`. Detecting "was
        # approved, now isn't" needs the pre-save state, which `self` (the
        # in-memory, about-to-be-saved instance) does not carry -- a one-query
        # refetch of the stored row by pk is the simplest way to get it, and it
        # only runs when there IS a persisted row to compare against and the
        # status is actually changing away from approved.
        if self.pk and self.status != DesignStatusChoices.STATUS_APPROVED:
            was_approved = (
                Design.objects.filter(pk=self.pk, status=DesignStatusChoices.STATUS_APPROVED)
                .exists()
            )
            if was_approved:
                children = list(self.children)
                if children:
                    names = ", ".join(str(d) for d in children)
                    raise ValidationError(
                        {"status": f"Cannot leave 'approved' status: {names} "
                                   "are based on this design and would silently lose their "
                                   "baseline. Use the New version button on this design to "
                                   "create one and re-base them onto it instead."}
                    )

        # At least one site is required (M1). Same M2M-timing caveat as every
        # other ``sites``-dependent check here: only checkable once persisted.
        if self.pk and not self.sites.exists():
            raise ValidationError({"sites": "A design must have at least one site."})

        # Every scoped rack must belong to one of this design's sites (M3;
        # consistent with the site-scoping of placements). M2M-timing caveat:
        # a many-to-many relation cannot be read on an unsaved instance
        # (pk=None) -- Django raises before the through-rows exist -- so this
        # check only runs once the design is persisted (i.e. on edits). For a
        # brand-new design the racks are attached only after the initial save,
        # so the form/serializer layer (a later phase) must re-run
        # full_clean() post-save to enforce this on create.
        if self.pk:
            offending = self.racks.exclude(site_id__in=self.sites.values_list("pk", flat=True))
            if offending.exists():
                names = ", ".join(str(rack) for rack in offending)
                raise ValidationError(
                    {"racks": f"These racks are not in one of the design's sites: {names}."}
                )

        # Same rule for `planned_racks` (T1.3): a PlannedRack has no site FK of
        # its own -- its site is reached through `location` (PlannedRack.site)
        # -- so this filters on `location__site_id` rather than `site_id`
        # directly. Same M2M-timing caveat as the `racks` check just above.
        if self.pk:
            offending_planned = self.planned_racks.exclude(
                location__site_id__in=self.sites.values_list("pk", flat=True)
            )
            if offending_planned.exists():
                names = ", ".join(str(rack) for rack in offending_planned)
                raise ValidationError(
                    {"planned_racks": f"These planned racks are not in one of the design's sites: {names}."}
                )


        # A design's `racks` scope is part of what was approved (§2.2/G4): the
        # `add-rack`/`remove-rack` API actions already refuse to widen or
        # narrow it on a frozen design, and this closes the same hole for
        # every OTHER write path (the plain REST endpoint, bulk import, a
        # script) at the model layer, rather than guarding N call sites by
        # hand. Deliberately does NOT block `status`, `summary`, `link` or any
        # other field here -- `status` is the escape hatch itself (drop back
        # to draft, or approve a new version, per `is_frozen`'s docstring), and
        # metadata like `summary`/`link` was never part of what got approved.
        #
        # Same M2M-timing trap as the site check just above -- clean() cannot
        # see a many-to-many the way it sees a scalar field, because writing
        # one goes straight to its own through-table with no clean() hook at
        # all. There is exactly one channel that IS visible here:
        # `self._m2m_values`, which NetBox's own `ValidatedModelSerializer`
        # (netbox/api/serializers/base.py) stashes the INCOMING m2m values on
        # the instance and calls `full_clean()` with BEFORE actually applying
        # them -- i.e. exactly the REST API path. A direct `design.racks.set()`
        # from a script, followed by `full_clean()`, has already committed the
        # through-table write by the time clean() runs, so there is nothing
        # left here to compare against and this check has nothing to catch --
        # a known gap, mirroring the one the comment above already documents
        # for the site check on CREATE. The HTML edit form has no
        # `_m2m_values` either (Django's ModelForm never touches an instance's
        # m2m before calling its `full_clean()`), so `DesignForm.clean()`
        # (forms.py) carries the equivalent check for that path, using
        # `self.instance`'s pre-edit field values.
        if self.pk:
            new_racks = getattr(self, "_m2m_values", {}).get("racks")
            if new_racks is not None:
                was_approved = Design.objects.filter(
                    pk=self.pk, status=DesignStatusChoices.STATUS_APPROVED
                ).exists()
                if was_approved:
                    new_ids = {rack.pk for rack in new_racks}
                    old_ids = set(self.racks.values_list("pk", flat=True))
                    if new_ids != old_ids:
                        raise ValidationError(
                            {"racks": "This design is approved, and approved designs "
                                      "are frozen: its rack scope cannot be changed. "
                                      "Set the design back to draft, or create a new "
                                      "version of it, to make this change."}
                        )


class DesignPlacement(NetBoxModel):
    """
    A single proposed change within a design: add a new device from the
    catalog, move an existing device, or mark one for (planned) removal.
    Never mutates the real device until the design is applied.
    """

    design = models.ForeignKey(
        to="netbox_rack_design.Design",
        on_delete=models.CASCADE,
        related_name="placements",
    )
    kind = models.CharField(max_length=20, choices=DesignPlacementKindChoices)

    # Existing device (move/remove); null for an add.
    #
    # SET_NULL, deliberately NOT CASCADE: deleting the real device must never
    # delete the plan that referenced it. Under CASCADE, decommissioning a
    # device in DCIM silently erased every placement pointing at it -- the
    # planner reopened the design and the move was simply gone, with no error
    # and nothing in the design to say why. Worse, applying a ``remove``
    # deletes the device and would therefore destroy the very placement that
    # recorded the removal, so a design erased its own history.
    #
    # On deletion the row now survives with ``device`` null and ``stale`` set
    # (stamped by the ``pre_delete`` receiver in signals.py, which runs before
    # Django's SET_NULL update), so the design can REPORT the loss instead of
    # quietly shrinking.
    device = models.ForeignKey(
        to="dcim.Device",
        on_delete=models.SET_NULL,
        related_name="design_placements",
        blank=True,
        null=True,
    )
    # True when whatever this move/remove referenced -- a real device, OR (G2,
    # below) an ancestor design's planned 'add' placement -- no longer exists.
    # A stale placement is inert -- projection already skips reference-less
    # move/remove rows -- but it stays visible and reportable until a planner
    # re-points it at another device/placement or deletes it.
    stale = models.BooleanField(default=False)
    # The name of whatever vanished, captured at deletion time: a real device's
    # name (dcim.Device pre_delete), or an ancestor placement's settled/proposed
    # name (DesignPlacement pre_delete, G2). Without it a stale row could only
    # say "something upstream is gone", which is not actionable.
    stale_device_name = models.CharField(max_length=64, blank=True)
    # The upstream placement this move/remove acts on, when the device being
    # moved/removed is not yet real -- it only exists as an ancestor design's
    # planned 'add' (PLAN-design-chains.md G2: "planned devices have no
    # identity"). For a move/remove, exactly one of `device` / `base_placement`
    # is set (never both), enforced in clean() below; neither is required when
    # the row is `stale`. An 'add' never sets this -- it IS the new identity,
    # not a reference to one.
    #
    # NOT the same relationship as `parent_placement` / `base_parent_placement`
    # below, despite all three being self-FKs -- see the three-way distinction
    # spelled out in the device-bay targeting block. In short: those two say
    # WHERE a blade goes (into which chassis), this one says WHICH device the
    # row is about. `base_placement` always crosses designs -- it
    # points UP the `based_on` chain at an ancestor's row -- and clean() below
    # requires it to be a `kind=add` placement in `self.design.baseline_chain()`
    # (an ancestor move/remove already acts on a real device, which a
    # downstream design references directly via `device` instead).
    #
    # SET_NULL, deliberately NOT CASCADE (G2 is explicit about this): cancelling
    # or deleting an upstream 'add' must not silently delete the downstream work
    # built on it. Mirrors the `device` FK's own SET_NULL rationale above --
    # deleting a `DesignPlacement` that others reference here now goes through
    # the analogous `pre_delete` receiver in signals.py (which stamps
    # `stale`/`stale_device_name`, using the vanished placement's
    # settled/proposed name, before the SET_NULL lands), so the loss is
    # reported instead of silently absorbed.
    base_placement = models.ForeignKey(
        to="self",
        on_delete=models.SET_NULL,
        related_name="downstream_placements",
        blank=True,
        null=True,
    )
    # New device from the catalog (add); null for move/remove.
    device_type = models.ForeignKey(
        to="dcim.DeviceType",
        on_delete=models.PROTECT,
        related_name="design_placements",
        blank=True,
        null=True,
    )
    proposed_name = models.CharField(max_length=64, blank=True)

    # Role/tenant. For an 'add', the planned new device's role/tenant,
    # applied when the design is later executed. For a 'move', a planned
    # OVERRIDE: set means the device becomes that role/tenant when it lands,
    # null means "leave the device's own value alone" (see clean() below and
    # resolved_role()/resolved_tenant()). Never set for a 'remove' (enforced
    # in clean()).
    device_role = models.ForeignKey(
        to="dcim.DeviceRole",
        on_delete=models.PROTECT,
        related_name="+",
        blank=True,
        null=True,
    )
    tenant = models.ForeignKey(
        to="tenancy.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        blank=True,
        null=True,
    )

    # Target placement (null for remove).
    target_rack = models.ForeignKey(
        to="dcim.Rack",
        on_delete=models.CASCADE,
        related_name="design_placements",
        blank=True,
        null=True,
    )
    # The planned-rack counterpart of ``target_rack`` (PLAN-templates.md T1.2):
    # a design may plan a device into a rack that does not exist in NetBox yet
    # (see ``PlannedRack``'s docstring for why that is a shared first-class
    # object rather than a design-scoped row). Exactly one of ``target_rack``/
    # ``target_planned_rack`` may be set for an 'add' or 'move' (enforced in
    # clean()); a 'remove' sets NEITHER -- which is why the database-level
    # guard in ``Meta.constraints`` below is "not both", not "exactly one"
    # (D26): a plain exactly-one CheckConstraint would reject every removal.
    #
    # CASCADE, mirroring ``target_rack``: this FK names a DESTINATION, not a
    # reference whose loss must be reported. Contrast ``device`` above, whose
    # SET_NULL + ``stale`` machinery exists precisely because that FK records
    # HISTORY (what is being acted on) -- ``target_rack``/``target_planned_rack``
    # instead record WHERE this row's device is headed, and if that destination
    # is deleted outright there is nothing left for the placement to mean, so
    # it disappears with it, exactly as it already does for a deleted
    # ``target_rack``.
    target_planned_rack = models.ForeignKey(
        to="netbox_rack_design.PlannedRack",
        on_delete=models.CASCADE,
        related_name="design_placements",
        blank=True,
        null=True,
    )
    target_position = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True
    )
    target_face = models.CharField(max_length=10, blank=True)

    # --- device-bay targeting (a blade into a chassis) -------------------------
    # Core forbids a child device from carrying a rack position or a face
    # (dcim.Device.clean), so a blade is never placed AT a U -- it is placed IN a
    # parent's bay. THREE cases, exactly one field each:
    #
    #   A. the chassis already exists in DCIM -> ``target_bay`` names the real
    #      dcim.DeviceBay row.
    #   B. the chassis is itself an 'add' in THIS design -> it has no bays yet
    #      (core instantiates them from the device type only when the device is
    #      created), so the blade points at the chassis's placement via
    #      ``parent_placement`` and names its bay via ``target_bay_name``, which is
    #      validated against the parent type's DeviceBayTemplates.
    #   C. the chassis is an 'add' in an ANCESTOR design -> same shape as B, but
    #      the reference crosses designs, so it is a different field with
    #      different deletion semantics: ``base_parent_placement``
    #      (+ ``target_bay_name``).
    #
    # ``target_bay_name`` is also filled in case A (mirroring the bay's name) so a
    # consumer has one field to read for "which bay" regardless of case.
    #
    # THE THREE SELF-FKs, AND WHY NONE OF THEM SUBSTITUTES FOR ANOTHER. Two
    # independent questions -- WHICH device is this row about, and WHERE does it
    # go -- crossed with whether the answer lives in this design or upstream:
    #
    #   * ``base_placement``        -- WHICH: the upstream 'add' that IS this
    #                                  blade's identity. Crosses designs.
    #                                  SET_NULL + staleness.
    #   * ``parent_placement``      -- WHERE: the chassis it goes into, planned
    #                                  in THIS design. Same design only.
    #                                  CASCADE is safe: the chassis and the
    #                                  blade are one design's work, so deleting
    #                                  the chassis legitimately deletes the
    #                                  blades planned into it.
    #   * ``base_parent_placement`` -- WHERE: the chassis it goes into, planned
    #                                  by an ANCESTOR. Crosses designs.
    #                                  SET_NULL + staleness, for the same reason
    #                                  ``base_placement`` is: CASCADE here would
    #                                  let an upstream design's deletion destroy
    #                                  downstream work (G2 is explicit).
    #
    # So a move of an inherited blade into an inherited chassis legitimately
    # carries ``base_placement`` AND ``base_parent_placement`` -- they answer
    # different questions -- while ``parent_placement`` and
    # ``base_parent_placement`` are mutually exclusive: a placement has exactly
    # one parent, reached by exactly one route.
    parent_placement = models.ForeignKey(
        to="self",
        on_delete=models.CASCADE,
        related_name="bay_children",
        blank=True,
        null=True,
        help_text="The placement of the chassis this blade goes into, when the "
                  "chassis is itself planned in this design.",
    )
    target_bay = models.ForeignKey(
        to="dcim.DeviceBay",
        on_delete=models.CASCADE,
        related_name="design_placements",
        blank=True,
        null=True,
        help_text="The real device bay this blade goes into, when the chassis "
                  "already exists in DCIM.",
    )
    target_bay_name = models.CharField(max_length=64, blank=True)
    # Case C: the chassis this blade goes into was planned by an ANCESTOR design
    # (PLAN-design-chains.md G2 / the phase-3 bay gap). Named for the two
    # references it sits between: ``base_`` marks the same up-the-chain crossing
    # ``base_placement`` marks, and ``parent_placement`` is the thing being
    # addressed -- the chassis. See the three-way distinction above.
    #
    # SET_NULL, deliberately NOT CASCADE, unlike the same-design
    # ``parent_placement``: cancelling an ancestor's chassis must not delete the
    # child's blades. The loss is REPORTED instead -- the ``DesignPlacement``
    # ``pre_delete`` receiver in signals.py stamps ``stale`` +
    # ``stale_device_name`` (the vanished chassis's name) on every downstream
    # blade before the SET_NULL lands, exactly as it already does for
    # ``base_placement``.
    base_parent_placement = models.ForeignKey(
        to="self",
        on_delete=models.SET_NULL,
        related_name="downstream_bay_children",
        blank=True,
        null=True,
        help_text="The placement of the chassis this blade goes into, when the "
                  "chassis is planned by an ancestor design in this design's "
                  "baseline chain.",
    )

    # Values for the deployment's own planning fields, declared in the
    # ``placement_fields`` plugin config and destined for the real device when
    # the design is applied. Flat ``{"<descriptor key>": value}``; validated in
    # clean() against that config, so an unknown key or a value outside a
    # descriptor's choices is rejected rather than silently stored.
    #
    # Deliberately NOT ``custom_field_data`` (which this NetBoxModel also has):
    # that means "NetBox custom fields ON the placement object", a different
    # thing from "values destined for the planned device". Keys here are the
    # plugin-internal descriptor keys, never a real custom-field name, so a
    # deployment renaming a cf edits its config and rewrites no rows.
    planning_data = models.JSONField(blank=True, null=True)

    # MANUAL custom-field bridge for a PLANNED PDU add (docs/pdu-distribution-spec
    # §6): the site-specific CUSTOM fields (declared via the ``planning_fields``
    # config) the distribution script wants but which a planned PDU (no real
    # device) has nowhere to read from -- used only when the cf are typed in by
    # hand. When the planned PDU instead REFERENCES a real PDU (power_source_device
    # below), cf are read live from that device and this stays null. NATIVE
    # electricals never live here -- a PDU's breaker comes from the bound feed
    # (real_power_feed / planned_power_feed). Shape: {"custom_fields": {...}}.
    # Null for every non-PDU placement. Never written to dcim.
    power_config = models.JSONField(blank=True, null=True)

    # An operator override of which feed leg(s) this device's PSUs draw from,
    # e.g. ``["b"]`` (single-PSU) or ``["a", "b"]`` / ``["c", "d"]``
    # (multi-PSU, ordered by PSU index -- a 2-PSU device sits on BOTH legs at
    # once, that is what redundancy means). ``None``/``[]`` means "use the
    # distribution engine's automatic a/a+b heuristic", unchanged. This is a
    # PLANNING HINT for the power projection only -- it steers which leg the
    # engine attributes an uncabled device's draw to and is never written to
    # dcim, never carried onto the real device when the design is applied.
    # That boundary is exactly why it is its own field rather than living in
    # ``planning_data`` (deployment-schema data that DOES get copied onto the
    # applied device) or ``power_config`` (documented as PDU-only, for a
    # planned PDU's own custom fields). Validated in clean(): a list of
    # distinct lowercase single-letter strings, or null/empty.
    preferred_feed_legs = models.JSONField(blank=True, null=True)

    # A planned PDU may INHERIT its custom fields from a real PDU device rather
    # than typing them (docs/pdu-distribution-spec §6): this FK is that source
    # device, and the distribution script reads ``power_source_device.cf`` LIVE
    # (never snapshotted -- editing the source device updates the plan). The FK is
    # also the copy provenance, so the dialog reopens by following it. Mutually
    # exclusive with a manual ``power_config`` (at most one supplies the cf). Null
    # for a manual/absent cf and every non-PDU placement.
    power_source_device = models.ForeignKey(
        to="dcim.Device",
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
    )

    # The feed this planned PDU draws its breaker from (docs/pdu-distribution-spec
    # §6.2). Exactly one may be set: a real dcim.PowerFeed (provisioned rack) OR a
    # plugin-side DesignPowerFeed (greenfield planning). ``bound_feed`` returns
    # whichever, exposing a uniform electricals shape so the distribution engine
    # never branches on real-vs-planned. Both null => unbound (degrades cleanly).
    real_power_feed = models.ForeignKey(
        to="dcim.PowerFeed",
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
    )
    planned_power_feed = models.ForeignKey(
        to="netbox_rack_design.DesignPowerFeed",
        on_delete=models.SET_NULL,
        related_name="bound_placements",
        blank=True,
        null=True,
    )

    # Provenance: which Template (if any) this placement was stamped from
    # (PLAN-templates.md §3, D20), and which version of that template it was
    # stamped at. Enough to answer "which racks use the standard ToR?" and to
    # warn that the template has since changed -- re-sync / diff-against-template
    # is explicitly DEFERRED (D20), so nothing here keeps this placement in
    # sync with its template after the stamp.
    #
    # SET_NULL, deliberately NOT CASCADE -- the same reasoning as ``device``
    # above, for the same reason: this FK records HISTORY (what this placement
    # was stamped from), not a destination whose loss should take the
    # placement with it. Deleting a ``Template`` (e.g. because "our standard
    # ToR" was retired or reorganised) must never delete every design
    # placement that was ever stamped from it -- those placements are real
    # devices/plans that stand on their own once stamped, exactly as a design
    # placement survives the deletion of the real device it referenced.
    # Unlike ``device``, losing this FK is not reportable data loss: the
    # provenance was informational from the start ("this came from a
    # template"), not the placement's identity, so there is no ``stale``-style
    # flag here -- ``from_template`` simply goes null and the placement is
    # otherwise unaffected.
    from_template = models.ForeignKey(
        to="netbox_rack_design.Template",
        on_delete=models.SET_NULL,
        related_name="stamped_placements",
        blank=True,
        null=True,
    )
    # The template's version (see Template.version below) at the moment this
    # placement was stamped. Compared against the live ``from_template.version``
    # to warn "this template has changed since you stamped it" -- the whole
    # point of D20. Null whenever ``from_template`` is null (an ordinary
    # hand-placed device never had a version to record), and also stays put
    # (not re-stamped) if the template is edited afterwards -- that is exactly
    # the drift this field exists to detect, not something to paper over.
    from_template_version = models.PositiveIntegerField(blank=True, null=True)

    class Meta:
        ordering = ("design", "target_position", "pk")
        verbose_name = "design placement"
        verbose_name_plural = "design placements"
        constraints = [
            # One design may claim a given bay once. Scoped to the design, not
            # global: two independent designs may each plan the same bay -- they
            # are competing proposals, and conflict detection between designs is
            # a separate concern from a design contradicting itself.
            models.UniqueConstraint(
                fields=("design", "target_bay"),
                condition=models.Q(target_bay__isnull=False),
                name="%(app_label)s_%(class)s_unique_design_target_bay",
            ),
            models.UniqueConstraint(
                fields=("design", "parent_placement", "target_bay_name"),
                condition=models.Q(parent_placement__isnull=False),
                name="%(app_label)s_%(class)s_unique_design_planned_bay",
            ),
            # The same rule for the CROSS-DESIGN route (case C). A separate
            # constraint rather than a widened one, because a partial index can
            # only be conditioned on one nullable column: the reasoning is
            # identical (a design must not contradict itself about one bay) and
            # the triple is canonical, since ``base_parent_placement`` always
            # points at the chassis's ORIGINATING 'add' (kind is enforced in
            # clean()), so two rows naming the same inherited chassis
            # necessarily name the same pk.
            models.UniqueConstraint(
                fields=("design", "base_parent_placement", "target_bay_name"),
                condition=models.Q(base_parent_placement__isnull=False),
                name="%(app_label)s_%(class)s_unique_design_base_parent_bay",
            ),
            # "Not both", not "exactly one" (D26, PLAN-templates.md): a plain
            # exactly-one constraint would reject every 'remove', which sets
            # neither target_rack nor target_planned_rack. clean() enforces
            # the stronger "exactly one, for add/move" rule; this is only the
            # database-level backstop against a row naming both destinations
            # at once.
            models.CheckConstraint(
                condition=models.Q(target_rack__isnull=True)
                | models.Q(target_planned_rack__isnull=True),
                name="%(app_label)s_%(class)s_single_target_rack",
            ),
        ]

    def __str__(self):
        # A stale row has no device left to name itself with, so fall back to
        # whatever it referenced -- an upstream placement (base_placement) or
        # the name captured when the reference vanished.
        label = (
            self.device or self.device_type or self.base_placement
            or self.stale_device_name or "?"
        )
        if self.stale:
            return f"{self.get_kind_display()}: {label} (reference gone)"
        return f"{self.get_kind_display()}: {label}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_rack_design:designplacement", args=[self.pk])

    @property
    def docs_url(self):
        return DOCS_BASE_URL

    def get_kind_color(self):
        return DesignPlacementKindChoices.colors.get(self.kind)

    @property
    def bound_feed(self):
        """The feed this planned PDU draws from, or None if unbound.

        Returns whichever of ``real_power_feed`` / ``planned_power_feed`` is set,
        as a uniform object exposing ``voltage``/``amperage``/``phase``/``supply``
        /``name`` -- a real ``dcim.PowerFeed`` and a ``DesignPowerFeed`` both carry
        those attributes, so the distribution engine reads either without a
        real-vs-planned branch (docs/pdu-distribution-spec §6.2).
        """
        return self.real_power_feed or self.planned_power_feed

    @property
    def site(self):
        """
        The site this placement actually targets (PLAN-multi-site.md M4): a
        multi-site design has a separate name space per site, so naming and
        collision checks scope by the PLACEMENT's site, never the design's.

        Derived from whichever of ``target_rack`` / ``target_planned_rack`` /
        the parent chassis's rack is set -- mirrors
        ``naming.RenderContext.rack``. ``None`` when nothing is targeted yet
        (a bare, freshly-built unsaved placement).
        """
        if self.target_bay_id:
            return self.target_bay.device.rack.site
        if self.target_rack_id:
            return self.target_rack.site
        if self.target_planned_rack_id:
            return self.target_planned_rack.location.site
        if self.parent_placement_id:
            return self.parent_placement.site
        if self.base_parent_placement_id:
            return self.base_parent_placement.site
        return None

    def resolved_role(self):
        """The role this placement's device will actually have.

        Null on ``device_role`` means "leave the device's own value alone" (see
        the field comment and clean()): so this returns the override
        (``device_role``) when one is set, and otherwise falls back to the
        carry-over source's own role -- the real ``device``'s role for a
        move/remove acting on one, or (recursively) the ancestor design's
        planned role for a move acting on a ``base_placement`` (an ancestor's
        still-planned 'add' has no real device yet, but its ``device_role`` IS
        its own planned role, so the recursion terminates there). Returns
        ``None`` when there is no device or base_placement to fall back on
        (an 'add' with no role chosen, or a stale row).
        """
        if self.device_role_id:
            return self.device_role
        if self.device_id:
            return self.device.role
        if self.base_placement_id:
            return self.base_placement.resolved_role()
        return None

    def resolved_tenant(self):
        """The tenant this placement's device will actually have.

        Same override/carry-over rule as :meth:`resolved_role`: null on
        ``tenant`` means "leave the device's own value alone", so this returns
        the override (``tenant``) when set, else the carry-over source's own
        tenant (the real ``device``'s tenant, or -- recursively -- a
        ``base_placement``'s own planned tenant), else ``None`` when there is
        nothing to fall back on.
        """
        if self.tenant_id:
            return self.tenant
        if self.device_id:
            return self.device.tenant
        if self.base_placement_id:
            return self.base_placement.resolved_tenant()
        return None

    def clean(self):
        super().clean()
        kind = self.kind

        # A design that is APPROVED is frozen (§2.2, Design.is_frozen): its
        # placements are read-only, because approval is what makes the design
        # derivable and downstream chains must be able to trust that a frozen
        # layer stops moving. Checked before anything else in this method so
        # a frozen design's placements reject a create/edit uniformly,
        # regardless of what else about the placement would otherwise be valid.
        if self.design_id and self.design.is_frozen:
            raise ValidationError(_frozen_design_clean_message("its placements"))

        # Config-declared planning fields: validated against the deployment's
        # ``placement_fields`` schema and normalised in place, so what reaches
        # the database is always type-correct and free of keys nothing reads.
        self.planning_data = planning_fields.validate_planning_data(self.planning_data, kind) or None

        # An operator's feed-leg override, if set, must be a list of distinct
        # lowercase single-letter leg identifiers. Deliberately NOT validated
        # against the rack's actual legs here -- a design can legitimately be
        # edited before its PDUs are bound, and the distribution engine
        # already ignores a leg that does not exist in the rack.
        if self.preferred_feed_legs:
            if not isinstance(self.preferred_feed_legs, list):
                raise ValidationError(
                    {"preferred_feed_legs": "Must be a list of feed leg letters."}
                )
            seen = set()
            for leg in self.preferred_feed_legs:
                if not isinstance(leg, str) or len(leg) != 1 or not ("a" <= leg <= "z"):
                    raise ValidationError(
                        {"preferred_feed_legs": f"Invalid feed leg {leg!r}: must be a single lowercase letter."}
                    )
                if leg in seen:
                    raise ValidationError(
                        {"preferred_feed_legs": f"Duplicate feed leg {leg!r}."}
                    )
                seen.add(leg)

        # A planned PDU binds to at most ONE feed (real xor planned).
        if self.real_power_feed_id and self.planned_power_feed_id:
            raise ValidationError(
                "A placement cannot bind to both a real and a planned power feed."
            )

        # A planned_power_feed (G5 item 3) must belong to THIS design or a
        # true ancestor's layer -- that layer has already happened from this
        # design's point of view (§9.2), same as base_placement.
        if self.planned_power_feed_id:
            self._validate_planned_power_feed()

        # A planned PDU's custom fields come from at most ONE source: a referenced
        # real device (cf read live) OR manual power_config -- never both (docs/
        # pdu-distribution-spec §6.5).
        if self.power_source_device_id and (self.power_config or {}).get("custom_fields"):
            raise ValidationError(
                "A placement cannot both reference a source device and carry "
                "manual power_config custom fields."
            )

        # Re-pointing a stale placement at a real device makes it live again, so
        # the flag clears itself rather than needing a separate "un-stale" action.
        if self.device_id:
            self.stale = False
            self.stale_device_name = ""
        # The same self-healing for an 'add' whose ancestor-planned chassis
        # vanished: giving it any parent again -- a real bay, a chassis planned
        # here, or another inherited one -- revives it. Scoped to 'add' so a
        # move/remove that lost its identity is never revived by acquiring a
        # mere TARGET, which says nothing about what is being moved.
        #
        # Scoped to an EXISTING row (``self.pk``) as well, because reviving is
        # only meaningful for a row that already lost something: a brand-new add
        # arriving with ``stale=True`` is not a survivor being re-pointed, it is
        # a caller inventing an observation, and the check further down must be
        # allowed to reject it rather than have it silently cleared here.
        if kind == DesignPlacementKindChoices.KIND_ADD and self.pk and (
            self.target_bay_id or self.parent_placement_id
            or self.base_parent_placement_id
        ):
            self.stale = False
            self.stale_device_name = ""

        if kind == DesignPlacementKindChoices.KIND_ADD:
            if not self.device_type:
                raise ValidationError({"device_type": "An 'add' requires a device type."})
            if self.device:
                raise ValidationError({"device": "An 'add' must not reference an existing device."})
            # An add never references an upstream placement -- it IS the new
            # identity a downstream design would reference, not a reference to
            # one (see the field comment on base_placement above).
            if self.base_placement_id:
                raise ValidationError({
                    "base_placement": "An 'add' must not reference a base_placement -- "
                                       "it creates a NEW planned identity, it does not act "
                                       "on an existing one.",
                })
            # An add never referenced a real DEVICE, so a device deletion can
            # never make it stale. It CAN now lose one thing: the ancestor-planned
            # chassis it was to be installed into (``base_parent_placement``,
            # SET_NULL). That leaves a bay-named add with no bay target at all,
            # and rejecting it here would hand back the very data loss SET_NULL
            # exists to prevent -- so exactly that shape is tolerated, and
            # nothing else is.
            if self.stale and not (
                self.target_bay_name
                and not self.target_bay_id
                and not self.parent_placement_id
                and not self.base_parent_placement_id
            ):
                raise ValidationError({
                    "stale": "An 'add' can only be stale when the ancestor-planned "
                             "chassis it was to go into is gone.",
                })
            # A stamped version with no template to have stamped it from is
            # meaningless -- ``from_template_version`` only ever records the
            # ``Template.version`` observed AT THE MOMENT ``from_template`` was
            # set (see that field's comment), so one without the other is not
            # a lesser form of provenance, it is an inconsistent one.
            if self.from_template_version is not None and not self.from_template_id:
                raise ValidationError({
                    "from_template_version": "from_template_version requires "
                                              "from_template to be set.",
                })
        else:
            # A stale move/remove is device-less AND base_placement-less BY
            # DEFINITION -- whatever it referenced (a real device, or an
            # ancestor's still-planned 'add') is gone. Rejecting it here would
            # make the row unsavable and hand back the data loss SET_NULL
            # exists to prevent.
            if not self.device_id and not self.base_placement_id and not self.stale:
                raise ValidationError({
                    "device": f"A '{kind}' requires either an existing device or a "
                              f"base_placement (an ancestor design's planned 'add').",
                })
            # Exactly one of device / base_placement -- never both. A move/remove
            # acts on ONE thing: a real device, or the not-yet-real identity an
            # ancestor design planned. Allowing both would leave it ambiguous
            # which one is authoritative.
            if self.device_id and self.base_placement_id:
                raise ValidationError({
                    "base_placement": f"A '{kind}' cannot set both device and "
                                       f"base_placement -- exactly one identifies what "
                                       f"is being acted on.",
                })
            if self.base_placement_id:
                self._validate_base_placement()
            if self.device_type:
                raise ValidationError({"device_type": f"A '{kind}' must not set a device type."})
            # Role / tenant on a MOVE are planned OVERRIDES: the design says
            # this device becomes that role/tenant when it lands. Null means
            # "leave the device's own value alone", so a plain reposition is
            # unaffected. A removal takes neither -- re-attributing gear you are
            # decommissioning means nothing.
            if kind != DesignPlacementKindChoices.KIND_MOVE:
                if self.device_role:
                    raise ValidationError({"device_role": f"A '{kind}' must not set a device role."})
                if self.tenant:
                    raise ValidationError({"tenant": f"A '{kind}' must not set a tenant."})
            # Provenance (D20) records where a NEW planned identity came from
            # -- it is meaningful only for an 'add', which is the one kind
            # that creates one. A move/remove acts on something that already
            # exists (a real device, or an ancestor's still-planned 'add'),
            # so it has its own provenance already and cannot acquire a
            # different one by being relocated -- unlike device_role/tenant,
            # this is refused for BOTH move and remove, not just remove.
            if self.from_template_id:
                raise ValidationError({
                    "from_template": f"A '{kind}' must not set from_template -- "
                                      f"template provenance only applies to an 'add'.",
                })
            if self.from_template_version is not None:
                raise ValidationError({
                    "from_template_version": f"A '{kind}' must not set "
                                              f"from_template_version -- template "
                                              f"provenance only applies to an 'add'.",
                })

        # A stale placement is inert: it projects nothing (projection skips
        # device-less move/remove rows), so validating its target against the
        # live world is both meaningless and liable to fail -- there is no
        # device type left to measure the slot with.
        if self.stale:
            return

        if kind == DesignPlacementKindChoices.KIND_REMOVE:
            return  # No target for a removal.

        # A bay target (blade into a chassis) is mutually exclusive with a rack
        # slot, and short-circuits the U/face validation below.
        if (self.target_bay_id or self.parent_placement_id
                or self.base_parent_placement_id):
            self._validate_bay_target()
            return
        if self.target_bay_name:
            raise ValidationError({
                "target_bay_name": "A bay name requires either a target bay or a "
                                   "parent placement.",
            })

        # add / move require a target rack OR a target planned rack -- exactly
        # one of the two (D26 / PLAN-templates.md T1.2): a real rack and a
        # planned rack are different destinations, never both at once, and
        # never neither (a 'remove', which already returned above, is the
        # only kind allowed to set neither).
        if not self.target_rack and not self.target_planned_rack:
            raise ValidationError({
                "target_rack": "A target rack or a target planned rack is required.",
            })
        if self.target_rack and self.target_planned_rack:
            raise ValidationError({
                "target_planned_rack": "A placement cannot target both a real rack "
                                       "and a planned rack -- exactly one identifies "
                                       "where it goes.",
            })

        if self.target_planned_rack_id:
            self._validate_planned_rack_target()
            return

        # the target position is optional -- None means a tray (non-racked)
        # target (spec §9.5: mount vs dismount vs tray-to-tray reassociation
        # are all distinguished by target_position being set vs None, never
        # by a separate flag).
        if self.target_position is None:
            self._validate_tray_target()
            return

        self._validate_target_slot()

    def _validate_base_placement(self):
        """A ``base_placement`` (G2) must point at a TRUE ancestor's 'add'.

        Same design, an unrelated design, or a descendant is invalid -- only a
        design in ``self.design.baseline_chain()`` has already happened from
        this design's point of view. And it must be a ``kind=add`` row: only
        an 'add' creates a NEW planned identity for this to reference; an
        ancestor's own move/remove already acts on an already-real device,
        which a downstream design references directly via ``device`` instead.
        """
        base = self.base_placement
        if not self.design_id:
            # Can't resolve an ancestor chain without a design yet; the
            # required-fields validation elsewhere will catch a missing design.
            return
        try:
            chain_design_ids = {d.pk for d in self.design.baseline_chain()}
        except ValueError as exc:
            raise ValidationError({"base_placement": str(exc)}) from exc
        if base.design_id not in chain_design_ids:
            raise ValidationError({
                "base_placement": f"{base.design} is not an ancestor of "
                                   f"{self.design} -- base_placement must reference a "
                                   f"placement belonging to a design in this design's "
                                   f"baseline chain.",
            })
        if base.kind != DesignPlacementKindChoices.KIND_ADD:
            raise ValidationError({
                "base_placement": "base_placement must reference an 'add' placement: "
                                   "only an add creates a new planned identity for a "
                                   "downstream design to act on; an ancestor move/remove "
                                   "already acts on an already-real device, which this "
                                   "design should reference through 'device' directly.",
            })

    def _validate_planned_power_feed(self):
        """A ``planned_power_feed`` (G5 item 3) must belong to THIS design or a
        TRUE ancestor -- unlike ``base_placement``, the SAME design is valid
        too (a plain, unchained PDU binding to a feed its own design planned
        is the ordinary case), only an unrelated design or a DESCENDANT is
        rejected: a design must not depend on a layer that has not happened
        from its own point of view. Mirrors ``_validate_base_placement``.
        """
        feed = self.planned_power_feed
        if not self.design_id:
            return
        if feed.design_id == self.design_id:
            return
        try:
            chain_design_ids = {d.pk for d in self.design.baseline_chain()}
        except ValueError as exc:
            raise ValidationError({"planned_power_feed": str(exc)}) from exc
        if feed.design_id not in chain_design_ids:
            raise ValidationError({
                "planned_power_feed": f"{feed.design} is not this design or an "
                                       f"ancestor of {self.design} -- a placement may "
                                       f"only bind to a planned power feed belonging to "
                                       f"itself or a design in its baseline chain.",
            })

    def _placed_device_type(self):
        """The DeviceType being placed: the add's own, the moved device's, or --
        for a move/remove acting on an ancestor's still-planned 'add' -- the
        upstream placement's device type (G2: the referenced device is not
        yet real, so there is nothing on ``self.device`` to read it from)."""
        if self.device_type_id:
            return self.device_type
        if self.device_id:
            return self.device.device_type
        if self.base_placement_id:
            return self.base_placement.device_type
        return None

    def _validate_bay_target(self):
        """
        Validate a blade placement against the ONE parent it names, by exactly
        one of the three routes the field comments above describe: a real chassis
        bay (``target_bay``), a bay of a chassis planned in this same design
        (``parent_placement`` + ``target_bay_name``), or a bay of a chassis
        planned by an ANCESTOR design (``base_parent_placement`` +
        ``target_bay_name``, PLAN-design-chains.md G2).
        """
        routes = [
            bool(self.target_bay_id),
            bool(self.parent_placement_id),
            bool(self.base_parent_placement_id),
        ]
        if sum(routes) > 1:
            raise ValidationError(
                "A placement targets a real device bay, a chassis planned in "
                "this design, or a chassis planned by an ancestor design -- "
                "exactly one of the three, never more."
            )
        if self.target_position is not None or self.target_face:
            raise ValidationError({
                "target_position": "A device placed in a bay takes no rack "
                                   "position or face -- those belong to its parent.",
            })

        device_type = self._placed_device_type()
        if device_type is not None and not device_type.is_child_device:
            raise ValidationError({
                "target_bay": f"{device_type} is not a child device type, so it "
                              f"cannot be installed in a device bay.",
            })

        if self.target_bay_id:
            parent = self.target_bay.device
            if self.target_rack_id and parent.rack_id != self.target_rack_id:
                raise ValidationError({
                    "target_rack": "Target rack must be the rack the chassis is in.",
                })
            if (
                self.design_id and parent.rack_id
                and not self.design.sites.filter(pk=parent.rack.site_id).exists()
            ):
                raise ValidationError({
                    "target_bay": "The chassis is not in one of the design's sites.",
                })
            # The bay must be free in the design's PROJECTED world: an occupant
            # this same design moves out or removes has already vacated it.
            occupant_id = self.target_bay.installed_device_id
            if occupant_id and occupant_id != self.device_id:
                if occupant_id not in self._vacated_device_ids():
                    raise ValidationError({
                        "target_bay": f"Bay {self.target_bay.name} is already "
                                      f"occupied by {self.target_bay.installed_device}.",
                    })
            return

        # Planned chassis in an ANCESTOR design (case C, G2). Checked before
        # case B so the same-design branch can keep assuming a non-null
        # ``parent_placement``.
        if self.base_parent_placement_id:
            self._validate_base_parent_placement()
            self._validate_planned_parent(
                "base_parent_placement", self.base_parent_placement
            )
            return

        # Planned chassis in THIS design (case B).
        parent_placement = self.parent_placement
        if parent_placement.pk == self.pk:
            raise ValidationError({"parent_placement": "A placement cannot be its own parent."})
        if self.design_id and parent_placement.design_id != self.design_id:
            raise ValidationError({
                "parent_placement": "The chassis placement must belong to the same design.",
            })
        self._validate_planned_parent("parent_placement", parent_placement)

    def _validate_base_parent_placement(self):
        """A ``base_parent_placement`` (G2) must be a TRUE ancestor's chassis 'add'.

        The parent-side twin of :meth:`_validate_base_placement`, and
        deliberately the same two rules for the same two reasons: only a design
        in ``self.design.baseline_chain()`` has already happened from this
        design's point of view (a sibling, a descendant or this design itself
        has not), and only a ``kind=add`` creates the NEW planned identity whose
        bays did not exist before -- an ancestor's move/remove acts on a chassis
        that is already real, whose bays are real ``dcim.DeviceBay`` rows a blade
        addresses through ``target_bay`` instead.
        """
        base_parent = self.base_parent_placement
        if not self.design_id:
            # No design yet, so no chain to resolve against; the required-field
            # validation elsewhere reports the missing design.
            return
        try:
            chain_design_ids = {d.pk for d in self.design.baseline_chain()}
        except ValueError as exc:
            raise ValidationError({"base_parent_placement": str(exc)}) from exc
        if base_parent.design_id not in chain_design_ids:
            raise ValidationError({
                "base_parent_placement": f"{base_parent.design} is not an ancestor of "
                                         f"{self.design} -- base_parent_placement must "
                                         f"reference a chassis planned by a design in "
                                         f"this design's baseline chain. A chassis "
                                         f"planned in THIS design is addressed by "
                                         f"parent_placement instead.",
            })
        if base_parent.kind != DesignPlacementKindChoices.KIND_ADD:
            raise ValidationError({
                "base_parent_placement": "base_parent_placement must reference an 'add' "
                                         "placement: only an add creates a new planned "
                                         "chassis whose bays do not exist yet. An "
                                         "ancestor's move/remove acts on a chassis that "
                                         "is already real, so its bays are real device "
                                         "bays -- use target_bay.",
            })

    def _validate_planned_parent(self, field_name, parent_placement):
        """The rules a PLANNED chassis parent shares, whichever route names it.

        One implementation for ``parent_placement`` (case B) and
        ``base_parent_placement`` (case C), reported against ``field_name``, so
        the two routes cannot drift about what a chassis is, which bays it has,
        or which rack it stands in. A planned chassis has no
        ``dcim.DeviceBay`` rows -- core instantiates those from the type's
        ``DeviceBayTemplate``s only when the real device is created -- so the
        bay name is validated against the templates, exactly what
        ``projection._attach_planned_chassis_bays`` later draws the strip from.
        """
        parent_type = parent_placement._placed_device_type()
        if parent_type is None or not parent_type.is_parent_device:
            raise ValidationError({
                field_name: "The referenced placement is not a parent "
                            "(chassis) device type.",
            })
        if not self.target_bay_name:
            raise ValidationError({
                "target_bay_name": "A bay name is required when the chassis is "
                                   "itself planned.",
            })
        valid_bays = set(
            parent_type.devicebaytemplates.values_list("name", flat=True)
        )
        if valid_bays and self.target_bay_name not in valid_bays:
            raise ValidationError({
                "target_bay_name": f"{parent_type} has no bay named "
                                   f"{self.target_bay_name!r}.",
            })
        if self.target_rack_id and parent_placement.target_rack_id != self.target_rack_id:
            raise ValidationError({
                "target_rack": "Target rack must be the rack the planned chassis is in.",
            })

    def _validate_tray_target(self):
        """
        A position-less (tray) target validates only that the rack is in one
        of the design's sites (spec §9.5, M3) -- there is no slot availability
        to check since a tray is an unordered list, not a grid.
        """
        if (
            self.design_id
            and not self.design.sites.filter(pk=self.target_rack.site_id).exists()
        ):
            raise ValidationError(
                {"target_rack": "Target rack must be in one of the design's sites."}
            )

    def _validate_planned_rack_target(self):
        """
        The ``target_planned_rack`` counterpart of ``_validate_tray_target``:
        same-site scope is the only thing checked here, regardless of whether
        ``target_position`` is set. A ``PlannedRack`` has no real
        ``dcim.Rack`` row to run ``get_available_units`` against and no
        occupancy of its own until Apply realizes it, so slot-collision
        checking against a planned rack's own (still hypothetical) layout is
        a projection-layer concern, not this model's -- out of scope here
        (PLAN-templates.md T1.2/T1.4 split).
        """
        # PlannedRack has no site FK of its own -- ``site`` is a property that
        # derefs through ``location`` (see PlannedRack.site) -- so this reads
        # location_id/site_id rather than comparing a column directly.
        if (
            self.design_id
            and not self.design.sites.filter(pk=self.target_planned_rack.location.site_id).exists()
        ):
            raise ValidationError(
                {"target_planned_rack": "Target planned rack must be in one of the design's sites."}
            )

    def _validate_target_slot(self):
        """Reuse NetBox's own collision logic to check the target slot is free.

        The slot must be free in the DESIGN's PROJECTED layout, not in the raw
        physical rack: a device the same design moves or removes out of its real
        slot no longer occupies it, so another device may legitimately move in
        (e.g. a swap). The set of such vacated device PKs is injected by the
        save-layout view (which sees the whole submitted batch) as
        ``_projected_vacated_device_ids``; absent that context we fall back to
        the design's persisted move/remove placements so the same rule holds for
        single-placement edits through the form/API.

        A design CHAIN adds a second world to check against (G1): an ancestor's
        planned add occupies no real U at all, and a real device an ancestor
        moved still occupies its OLD one, so ``get_available_units`` alone would
        happily let a child drop a device straight onto an inherited tile and
        the collision would surface only in the rendered elevation. The
        ancestor-baseline occupancy comes from the projection's replay
        (``projection.baseline_occupancy``) rather than being re-derived here,
        so the rule that validates a save and the rule that draws the rack can
        never disagree.
        """
        device_type = self._placed_device_type()
        if device_type is None:
            return
        claims, baseline_freed = self._baseline_claims()
        rack_face = None if device_type.is_full_depth else (self.target_face or None)
        exclude = [self.device.pk] if self.device_id else []
        exclude += [pk for pk in self._vacated_device_ids() if pk not in exclude]
        # Real devices the ancestor chain moves or removes are not where the
        # physical rack still says they are, so they cannot block this target.
        exclude += [pk for pk in baseline_freed if pk not in exclude]
        available = self.target_rack.get_available_units(
            u_height=device_type.u_height, rack_face=rack_face, exclude=exclude
        )
        if self.target_position and float(self.target_position) not in [float(u) for u in available]:
            raise ValidationError(
                {"target_position": f"U{self.target_position} is not available in {self.target_rack}."}
            )
        self._validate_baseline_slot(device_type, claims)

    def _baseline_claims(self):
        """``(claims, freed_device_ids)`` from this design's ancestor chain (G1).

        Empty and query-free for a design with no ``based_on``, which is the
        overwhelmingly common case -- a single FK-id test, so the chain support
        costs an unchained design nothing at validation time.
        """
        if not self.design_id or not self.design.based_on_id or self.target_rack_id is None:
            return [], set()
        from . import projection  # local: projection imports this module

        return projection.baseline_occupancy(self.design, self.target_rack)

    def _validate_baseline_slot(self, device_type, claims):
        """Reject a target the ANCESTOR baseline already claims (G1).

        Interval overlap on the same face, with the same full-depth rule
        ``get_available_units`` applies: a full-depth device (on either side of
        the comparison) spans both faces, so it collides with anything at those
        rows regardless of face.

        An identity THIS design moves or removes is excluded -- relocating an
        ancestor-planned device frees the U the ancestor gave it, which is the
        other half of what ``_vacated_device_ids`` does for real devices.
        """
        if not claims:
            return
        vacated = self._vacated_baseline_keys()
        start = float(self.target_position)
        end = start + float(device_type.u_height or 1)
        face = self.target_face or ""
        for claim in claims:
            if claim["key"] in vacated:
                continue
            spans_both = device_type.is_full_depth or claim["is_full_depth"]
            if not spans_both and claim["face"] != face:
                continue
            claim_start = float(claim["u_position"])
            claim_end = claim_start + float(claim["u_height"])
            if start < claim_end and claim_start < end:
                raise ValidationError({
                    "target_position": f"U{self.target_position} in "
                                       f"{self.target_rack} is already claimed by "
                                       f"{claim['source_design']}, which this design "
                                       f"is based on.",
                })

    def _vacated_baseline_keys(self):
        """Baseline identities this design frees, as ``projection`` identity keys.

        The companion to ``_vacated_device_ids`` for the chain world. That method
        can only ever answer in real device PKs, and an ancestor-planned identity
        has none (G2) -- so relocating one could not be expressed there at all.
        Keyed the same way the replay keys it (``("pl", <ancestor add pk>)`` /
        ``("dev", <device pk>)``) so the two agree by construction.

        Includes THIS row's own identity: a move of an ancestor-planned device
        frees the U the ancestor put it at, exactly as excluding ``self.device``
        does for a real one.
        """
        keys = set()
        if self.base_placement_id:
            keys.add(("pl", self.base_placement_id))
        elif self.device_id:
            keys.add(("dev", self.device_id))
        if self.design_id is None:
            return keys
        rows = (
            DesignPlacement.objects.filter(
                design_id=self.design_id,
                kind__in=(
                    DesignPlacementKindChoices.KIND_MOVE,
                    DesignPlacementKindChoices.KIND_REMOVE,
                ),
            )
            .exclude(pk=self.pk)
            .values_list("device_id", "base_placement_id")
        )
        for device_id, base_id in rows:
            if base_id:
                keys.add(("pl", base_id))
            elif device_id:
                keys.add(("dev", device_id))
        return keys

    def _vacated_device_ids(self):
        """PKs of devices this design frees from their real slots, so they don't
        count as occupying the target rack when validating another placement.

        Prefers the batch context the save-layout view injects (it knows every
        device the current submit moves/removes, including ones not yet
        persisted); otherwise reads the design's already-saved move/remove rows.

        Deliberately answers ONLY in real device PKs, because that is all
        ``get_available_units(exclude=...)`` understands. A base_placement-backed
        row (G2) has no real device to vacate -- the identity it acts on is not
        real yet -- so it is not expressible here at all and is handled by
        ``_vacated_baseline_keys`` against the ancestor baseline instead.
        """
        injected = getattr(self, "_projected_vacated_device_ids", None)
        if injected is not None:
            return {pk for pk in injected if pk}
        if self.design_id is None:
            return set()
        return set(
            DesignPlacement.objects.filter(
                design_id=self.design_id,
                kind__in=(
                    DesignPlacementKindChoices.KIND_MOVE,
                    DesignPlacementKindChoices.KIND_REMOVE,
                ),
                device_id__isnull=False,
            )
            .exclude(pk=self.pk)
            .values_list("device_id", flat=True)
        )


class FavoriteSet(models.Model):
    """
    A NAMED set of starred device types belonging to one user.

    One flat favorites list served a single way of working; people plan racks in
    modes -- a server build pulls different types than a network build -- and
    kept re-starring (user request 2026-08-28). A user has as many sets as they
    like ("Default", "for server", "for network"), each with its own membership,
    and the editor works within one selected set at a time.

    ``DEFAULT_NAME`` is the set every user starts with. It is not privileged:
    it can be renamed or deleted like any other, and is simply re-created empty
    if a user ends up with no sets at all.

    Deliberately a plain ``django.db.models.Model`` (NOT a NetBoxModel), for the
    same reason as the favorites it holds: a personal UI preference must not
    write ObjectChange rows, index for search, or carry custom fields/tags.
    """

    DEFAULT_NAME = "Default"

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="rack_design_favorite_sets",
    )
    name = models.CharField(max_length=100)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("user", "name")
        verbose_name = "favorite set"
        verbose_name_plural = "favorite sets"
        constraints = [
            # Names are the user's handle on their sets, so they must be unique
            # per user -- and only per user: two people may both have "Default".
            models.UniqueConstraint(
                fields=("user", "name"),
                name="%(app_label)s_%(class)s_unique_user_name",
            ),
        ]

    def __str__(self):
        return f"{self.user}: {self.name}"

    @classmethod
    def default_for(cls, user):
        """The user's default set, created on first use.

        Every entry point needs "the set to work in when none was chosen", and a
        user who has never starred anything has no rows at all -- so this both
        picks and provisions. An existing set named ``DEFAULT_NAME`` wins;
        otherwise the user's first set by name; otherwise a fresh one.
        """
        existing = cls.objects.filter(user=user, name=cls.DEFAULT_NAME).first()
        if existing is not None:
            return existing
        first = cls.objects.filter(user=user).order_by("name").first()
        if first is not None:
            return first
        return cls.objects.create(user=user, name=cls.DEFAULT_NAME)


class FavoriteDeviceType(models.Model):
    """
    A per-user UI preference: a device type the user has "starred" in the
    catalog palette, surfaced for quick access.

    Membership is per SET (:class:`FavoriteSet`), so the same device type can be
    starred in "for server" and "for network" at once -- which is why the
    uniqueness is (set, device_type) and not (user, device_type). ``user`` is
    kept alongside the set so every query in the user-scoped API can filter on
    the requesting user directly, without joining.

    Deliberately a plain ``django.db.models.Model`` (NOT a NetBoxModel): starring
    is a transient personal preference, so it must NOT carry change logging,
    search indexing, custom fields, or tags. Subclassing NetBoxModel would write
    an ObjectChange row on every star toggle, which is unwanted noise.
    """

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="rack_design_favorite_device_types",
    )
    favorite_set = models.ForeignKey(
        to="netbox_rack_design.FavoriteSet",
        on_delete=models.CASCADE,
        related_name="favorites",
    )
    device_type = models.ForeignKey(
        to="dcim.DeviceType",
        on_delete=models.CASCADE,
        related_name="+",
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("user", "favorite_set", "device_type")
        verbose_name = "favorite device type"
        verbose_name_plural = "favorite device types"
        constraints = [
            models.UniqueConstraint(
                fields=("favorite_set", "device_type"),
                name="%(app_label)s_%(class)s_unique_set_device_type",
            ),
        ]

    def __str__(self):
        return f"{self.user}: {self.device_type} ({self.favorite_set.name})"


class HiddenDesignRack(models.Model):
    """
    A per-user editor view-state row recording that ``user`` has HIDDEN ``rack``
    while working on ``design`` in the multi-rack workspace.

    We store HIDDEN rows (not visible ones) so the natural default -- no rows --
    means "all of the design's scoped racks are visible". Hiding/showing is a
    purely personal, transient preference: it never affects another user, never
    affects the design's data, and never changes the design.racks scope.

    Deliberately a plain ``django.db.models.Model`` (NOT a NetBoxModel), for the
    same reason as FavoriteDeviceType: toggling visibility must not write an
    ObjectChange row, index for search, or carry custom fields/tags.
    """

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="rack_design_hidden_racks",
    )
    design = models.ForeignKey(
        to="netbox_rack_design.Design",
        on_delete=models.CASCADE,
        related_name="hidden_rack_states",
    )
    rack = models.ForeignKey(
        to="dcim.Rack",
        on_delete=models.CASCADE,
        related_name="+",
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("user", "design", "rack")
        verbose_name = "hidden design rack"
        verbose_name_plural = "hidden design racks"
        constraints = [
            models.UniqueConstraint(
                fields=("user", "design", "rack"),
                name="%(app_label)s_%(class)s_unique_user_design_rack",
            ),
        ]

    def __str__(self):
        return f"{self.user}: {self.design} hides {self.rack}"


class HiddenDesignChassis(models.Model):
    """
    Per-user editor view-state for the CHASSIS LAYER (spec §10.3/§10.4): ``user``
    has HIDDEN ``chassis`` while working on ``design``.

    The chassis layer is the rack workspace re-pointed at chassis -- a chassis IS a
    rack there, bays in place of units -- so its visibility control mirrors
    HiddenDesignRack exactly: HIDDEN rows are stored, so no rows means "every
    chassis in scope is visible", and the preference is personal, never touching
    the design's data or anyone else's view.

    ``chassis`` is a dcim.Device (a parent-role one). A chassis that is itself
    PLANNED has no device row yet and therefore cannot be hidden -- it is always
    visible, which is also the useful behaviour: you just added it.
    """

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="rack_design_hidden_chassis",
    )
    design = models.ForeignKey(
        to="netbox_rack_design.Design",
        on_delete=models.CASCADE,
        related_name="hidden_chassis_states",
    )
    chassis = models.ForeignKey(
        to="dcim.Device",
        on_delete=models.CASCADE,
        related_name="+",
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("user", "design", "chassis")
        verbose_name = "hidden design chassis"
        verbose_name_plural = "hidden design chassis"
        constraints = [
            models.UniqueConstraint(
                fields=("user", "design", "chassis"),
                name="%(app_label)s_%(class)s_unique_user_design_chassis",
            ),
        ]

    def __str__(self):
        return f"{self.user}: {self.design} hides {self.chassis}"


class DesignPowerFeed(NetBoxModel):
    """
    A PLANNED power feed for one rack in one design
    (docs/pdu-distribution-spec.md §6.1). A real PDU sizes its breaker from the
    ``dcim.PowerFeed`` its power port is cabled to; a planned PDU in a greenfield
    rack (no real feeds yet) binds to one of these instead. Field names and value
    domains deliberately MIRROR ``dcim.PowerFeed`` (``voltage``/``amperage``/
    ``phase``/``supply``) so ``DesignPlacement.bound_feed`` reads a real feed and a
    planned feed through the same attributes -- the distribution engine never
    branches on real-vs-planned.

    A ``NetBoxModel`` (unlike DesignRackPower): a planned feed is design data a
    team reads, edits and deletes on its own -- it needed a list view, a detail
    page and a delete button of its own (user 2026-08-28), which is exactly what
    the generic views give a NetBoxModel. Read-only w.r.t. dcim; nothing is ever
    written to a real ``PowerFeed``.

    ``planned_rack`` (PLAN-templates.md D25/T1.8b) is the greenfield case this
    model's own docstring already names: a planned PDU in a rack that does not
    exist yet has no ``dcim.PowerFeed`` rows to size its breaker from, because
    it has no rack row at all. ``rack`` is therefore nullable too, and exactly
    one of the two is required (D26) -- unlike ``DesignPlacement``'s "not both"
    shape, a feed always belongs to SOME rack, real or planned, so there is no
    "neither" case to leave room for.
    """

    clone_fields = ("design", "rack", "planned_rack", "voltage", "amperage", "phase", "supply")

    design = models.ForeignKey(
        to="netbox_rack_design.Design",
        on_delete=models.CASCADE,
        related_name="planned_feeds",
    )
    rack = models.ForeignKey(
        to="dcim.Rack",
        on_delete=models.CASCADE,
        related_name="+",
        blank=True,
        null=True,
    )
    # The planned-rack counterpart of ``rack`` above (D25/D26). CASCADE, same as
    # ``rack``: this FK names WHERE the feed lives, not a reference whose loss
    # must be reported, so deleting the planned rack legitimately deletes the
    # feed planned for it.
    planned_rack = models.ForeignKey(
        to="netbox_rack_design.PlannedRack",
        on_delete=models.CASCADE,
        related_name="+",
        blank=True,
        null=True,
    )
    # The feed's identity/leg, e.g. "Feed A" -- the bank/leg the bound PDUs sit on.
    name = models.CharField(max_length=100)
    voltage = models.PositiveIntegerField(default=230)
    amperage = models.PositiveIntegerField(default=16)
    phase = models.CharField(
        max_length=20,
        choices=PowerFeedPhaseChoices,
        default=PowerFeedPhaseChoices.PHASE_SINGLE,
    )
    supply = models.CharField(
        max_length=20,
        choices=PowerFeedSupplyChoices,
        default=PowerFeedSupplyChoices.SUPPLY_AC,
    )

    class Meta:
        ordering = ("design", "rack", "planned_rack", "name")
        verbose_name = "planned power feed"
        verbose_name_plural = "planned power feeds"
        constraints = [
            # Nullable ``rack`` (D26): Postgres treats NULLs as distinct, so a
            # plain UniqueConstraint(design, rack, name) stops constraining
            # anything once every planned-rack-only row shares a NULL there.
            # Two partial constraints, one per destination kind, instead.
            models.UniqueConstraint(
                fields=("design", "rack", "name"),
                condition=models.Q(rack__isnull=False),
                name="%(app_label)s_%(class)s_unique_design_rack_name",
            ),
            models.UniqueConstraint(
                fields=("design", "planned_rack", "name"),
                condition=models.Q(planned_rack__isnull=False),
                name="%(app_label)s_%(class)s_unique_design_planned_rack_name",
            ),
            # EXACTLY one of rack / planned_rack, not merely "not both" (D26):
            # a feed always lives in some rack, real or planned -- there is no
            # "remove" case here the way DesignPlacement has one.
            models.CheckConstraint(
                condition=(
                    models.Q(rack__isnull=False, planned_rack__isnull=True)
                    | models.Q(rack__isnull=True, planned_rack__isnull=False)
                ),
                name="%(app_label)s_%(class)s_exactly_one_rack",
            ),
        ]

    def __str__(self):
        # Deliberately FK-free: GraphQL (and any partial-field fetch) builds
        # instances without the related columns loaded, and reaching for
        # ``self.design`` there raises instead of rendering a label. The design
        # and rack are separate columns everywhere this string is shown.
        return self.name or f"Planned feed {self.pk}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_rack_design:designpowerfeed", args=[self.pk])

    @property
    def docs_url(self):
        return DOCS_BASE_URL

    def clean(self):
        super().clean()
        # A design that is APPROVED is frozen (§2.2, Design.is_frozen), and a
        # planned feed is part of what an approved design claims -- it sizes
        # its rack's capacity bar, so adding, resizing or deleting one changes
        # what the plan means just as much as moving a placement does. Mirrors
        # DesignPlacement.clean()'s freeze check (above) so REST create/update,
        # the HTML views, bulk import and GraphQL are all covered from one
        # place rather than guarding each call site by hand. (Delete never
        # reaches clean() at all -- that half is guarded explicitly on the
        # viewset / HTML delete views instead, same as for placements.)
        if self.design_id and self.design.is_frozen:
            raise ValidationError(_frozen_design_clean_message("its planned power feeds"))

        # Python-level mirror of the CheckConstraint above, for a friendlier
        # form/API error than a bare IntegrityError.
        if self.rack_id and self.planned_rack_id:
            raise ValidationError({
                "planned_rack": "A planned feed cannot bind to both a real rack "
                                 "and a planned rack -- exactly one identifies "
                                 "where it lives.",
            })
        if not self.rack_id and not self.planned_rack_id:
            raise ValidationError({
                "rack": "A planned feed requires a rack or a planned rack.",
            })

        # Same-site scope as DesignPlacement._validate_planned_rack_target: a
        # PlannedRack has no site FK of its own -- ``site`` is a property that
        # derefs through ``location`` -- so this reads location_id/site_id
        # rather than comparing a column directly.
        if (
            self.planned_rack_id and self.design_id
            and not self.design.sites.filter(pk=self.planned_rack.location.site_id).exists()
        ):
            raise ValidationError({
                "planned_rack": "Planned rack must be in one of the design's sites.",
            })

    @property
    def derated_watts(self):
        """The usable watts this feed contributes to its rack's capacity.

        Delegates to the SAME helpers the projection uses -- ``breaker_watts``
        for the phase-aware breaker size, and the instance's live
        ``POWERFEED_DEFAULT_MAX_UTILIZATION`` for the derating NetBox stamps into
        a real feed's ``available_power`` -- so a feed never reports one figure
        in the list and another in the rack's capacity bar. Imported locally:
        the projection imports models, not the other way round.
        """
        from netbox.config import get_config

        from .distribution import breaker_watts

        watts = breaker_watts(self) or 0
        if not watts:
            return 0
        max_util = get_config().POWERFEED_DEFAULT_MAX_UTILIZATION or 100
        return int(round(watts * max_util / 100.0))


class DesignRackPower(models.Model):
    """
    Per-design power custom-field OVERRIDE for one rack
    (docs/pdu-distribution-spec.md). The distribution script reads rack power
    fields (``power_limitation``, ``pdu_location``) from ``rack.cf``; when a
    design plans a rack whose real cf is unset (or needs a different planned
    value), this holds the effective values -- merged over ``rack.cf`` for the
    distribution, never written back to dcim.

    Plain ``models.Model`` (like HiddenDesignRack): this is planning scratch
    data, not a change-logged/searchable object with its own cf/tags.

    ``planned_rack`` (PLAN-templates.md D25/T1.8b): a planned rack has no
    ``dcim.Rack`` row and therefore no ``rack.cf`` at all, so for it this row
    is not an override merged over something else -- it is the ONLY source of
    ``power_limitation``/``pdu_location``. Same nullable-``rack``-plus-partial-
    constraints shape as ``DesignPowerFeed`` (D26): exactly one of ``rack`` /
    ``planned_rack`` is required, never both, never neither.
    """

    design = models.ForeignKey(
        to="netbox_rack_design.Design",
        on_delete=models.CASCADE,
        related_name="rack_power",
    )
    rack = models.ForeignKey(
        to="dcim.Rack",
        on_delete=models.CASCADE,
        related_name="+",
        blank=True,
        null=True,
    )
    # The planned-rack counterpart of ``rack`` above (D25/D26). CASCADE, same
    # reasoning as DesignPowerFeed.planned_rack: this names WHERE the override
    # applies, so deleting the planned rack legitimately deletes it.
    planned_rack = models.ForeignKey(
        to="netbox_rack_design.PlannedRack",
        on_delete=models.CASCADE,
        related_name="+",
        blank=True,
        null=True,
    )
    # Same JSON shape as DesignPlacement.power_config, minus "feed" (a rack has
    # no feed of its own): {"source", "copied_from", "custom_fields": {...}}.
    power_config = models.JSONField(blank=True, null=True)

    class Meta:
        ordering = ("design", "rack", "planned_rack")
        verbose_name = "design rack power"
        verbose_name_plural = "design rack power"
        constraints = [
            # Same D26 shape as DesignPowerFeed: nullable ``rack`` destroys a
            # plain UniqueConstraint(design, rack)'s meaning, so two partial
            # constraints replace it, one per destination kind.
            models.UniqueConstraint(
                fields=("design", "rack"),
                condition=models.Q(rack__isnull=False),
                name="%(app_label)s_%(class)s_unique_design_rack",
            ),
            models.UniqueConstraint(
                fields=("design", "planned_rack"),
                condition=models.Q(planned_rack__isnull=False),
                name="%(app_label)s_%(class)s_unique_design_planned_rack",
            ),
            # EXACTLY one of rack / planned_rack: an override always applies to
            # SOME rack, real or planned -- there is no "neither" case here.
            models.CheckConstraint(
                condition=(
                    models.Q(rack__isnull=False, planned_rack__isnull=True)
                    | models.Q(rack__isnull=True, planned_rack__isnull=False)
                ),
                name="%(app_label)s_%(class)s_exactly_one_rack",
            ),
        ]

    def __str__(self):
        return f"{self.design}: power for {self.rack or self.planned_rack}"

    def clean(self):
        super().clean()
        # Python-level mirror of the CheckConstraint above, for a friendlier
        # form/API error than a bare IntegrityError.
        if self.rack_id and self.planned_rack_id:
            raise ValidationError({
                "planned_rack": "A rack power override cannot bind to both a "
                                 "real rack and a planned rack -- exactly one "
                                 "identifies where it applies.",
            })
        if not self.rack_id and not self.planned_rack_id:
            raise ValidationError({
                "rack": "A rack power override requires a rack or a planned rack.",
            })

        # Same-site scope as DesignPowerFeed.clean() / DesignPlacement.
        # _validate_planned_rack_target: PlannedRack has no site FK of its
        # own -- ``site`` is a property that derefs through ``location``.
        if (
            self.planned_rack_id and self.design_id
            and not self.design.sites.filter(pk=self.planned_rack.location.site_id).exists()
        ):
            raise ValidationError({
                "planned_rack": "Planned rack must be in one of the design's sites.",
            })

    @classmethod
    def effective_custom_fields(cls, design, rack):
        """The MERGED rack power custom fields ``design`` should read for
        ``rack``, across its baseline chain (PLAN-design-chains.md G5 item 2).

        ``rack`` may be EITHER a real ``dcim.Rack`` or a ``PlannedRack``
        (PLAN-templates.md D25/T1.8b) -- a planned rack has no ``rack.cf`` at
        all, so this is the only place its power custom fields can come from.
        Both kinds are looked up the same way; only which FK column is
        queried differs, so no caller needs to branch on real-vs-planned.

        The rule: a child INHERITS an approved ancestor's override for this
        rack, and MAY OVERRIDE any key of its own -- the same shape as a child
        re-planning an inherited placement (G2). This is safe to resolve LIVE,
        never snapshotted, because an ancestor able to be inherited from is by
        definition frozen (``Design.is_frozen``, §2.2): its row cannot change
        underneath the child.

        Ancestors are merged OLDEST FIRST so a nearer ancestor's key wins over
        a farther one, then ``design``'s own row is merged last so it wins
        over every ancestor -- mirroring ``_Baseline._replay``'s "last write
        wins" rule for a relocated identity. Uses the SAME §9.2 all-or-nothing
        chain resolution as the rack-face replay and the capacity bar
        (``projection.resolve_baseline_chain``): a non-approved/implemented
        ancestor, or a broken lineage, contributes nothing from ANY ancestor --
        but ``design``'s own row is unaffected, exactly as an ancestor refusal
        never erases this design's own placements.

        Returns ``(merged: dict, conflict: dict | None)`` -- ``conflict`` is
        the chain refusal (§8.3 shape), for a caller (e.g. a future
        distribution-status surface) that wants to report WHY an ancestor's
        override did not apply, rather than a plausible-but-wrong merge.
        """
        from . import projection  # local: projection imports this module

        rack_filter = (
            {"planned_rack": rack} if isinstance(rack, PlannedRack) else {"rack": rack}
        )

        merged = {}
        chain, conflict = projection.resolve_baseline_chain(design)
        for ancestor in chain:
            try:
                row = cls.objects.get(design=ancestor, **rack_filter)
            except cls.DoesNotExist:
                continue
            merged.update((row.power_config or {}).get("custom_fields") or {})
        try:
            own = cls.objects.get(design=design, **rack_filter)
        except cls.DoesNotExist:
            own = None
        if own is not None:
            merged.update((own.power_config or {}).get("custom_fields") or {})
        return merged, conflict


class DesignApply(models.Model):
    """
    A record that an apply run materialized one placement as a real
    ``dcim.Device`` -- what makes apply idempotent (find/create/update rather
    than duplicate this row's device on a re-apply), gives the external
    automation an exact target, and survives whichever side is later deleted
    so the history of "this design produced that device" is never silently
    lost.

    Plain ``models.Model`` (like ``DesignRackPower`` above), NOT a
    ``NetBoxModel``: contrast ``DesignPowerFeed``, which IS one because a team
    reads, edits and deletes planned feeds directly and needs a list view, a
    detail page and its own changelog for that. A ``DesignApply`` row is
    written only by the apply process, never hand-edited -- and the
    attribution that matters (who/when the device was created or changed) is
    already recorded on the device's OWN changelog, not on this bookkeeping
    row. So no cf/tags/changelog of its own is needed here.
    """

    design = models.ForeignKey(
        to="netbox_rack_design.Design",
        on_delete=models.SET_NULL,
        related_name="applies",
        blank=True,
        null=True,
    )
    # Snapshot of design.title, filled by snapshot_names() on every save() so
    # it cannot drift from the live design -- kept so this row still reads
    # "which design did this" after that design is deleted.
    design_title = models.CharField(max_length=200, blank=True)

    placement = models.ForeignKey(
        to="netbox_rack_design.DesignPlacement",
        on_delete=models.SET_NULL,
        related_name="applies",
        blank=True,
        null=True,
    )

    device = models.ForeignKey(
        to="dcim.Device",
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
    )
    # Snapshot of device.name, filled by snapshot_names() on every save() --
    # kept so this row still reads "which device did this apply" after that
    # device is deleted (e.g. the device this row recorded a REMOVAL of).
    device_name = models.CharField(max_length=64, blank=True)

    # The device's status BEFORE this apply changed it -- meaningful only for
    # a removal, where apply itself moves the device to a decommissioned-style
    # status. Captured so a later revert restores the EXACT prior status
    # rather than guessing "active": a device removed from `offline` must not
    # come back as `active`.
    prior_device_status = models.CharField(max_length=50, blank=True)

    applied_by = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
    )

    created = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created",)
        verbose_name = "design apply"
        verbose_name_plural = "design applies"
        constraints = [
            # One row per placement -- a re-apply of the same placement must
            # find this row rather than create a second one. Conditioned on
            # placement__isnull=False so a row whose placement was later
            # deleted (and this FK nulled) never blocks anything, and several
            # such orphaned rows may legitimately coexist.
            models.UniqueConstraint(
                fields=("placement",),
                condition=models.Q(placement__isnull=False),
                name="%(app_label)s_%(class)s_unique_placement",
            ),
        ]

    def snapshot_names(self):
        """Refresh ``design_title`` / ``device_name`` from the live FKs.

        Called from ``save()`` so the snapshots can never drift from what
        ``design``/``device`` pointed at when this row was last written.  A
        null FK is left alone: the point of the snapshot is to survive
        exactly the moment the FK it mirrors goes null, so once that happens
        overwriting it with an empty string would destroy the one thing this
        field exists to keep.

        No ``pre_delete`` receiver mirrors this model the way ``signals.py``
        mirrors ``DesignPlacement``. Those receivers exist because
        ``stale_device_name`` is captured ONLY at the moment of deletion --
        there is no earlier point where a placement snapshots the name of a
        device it merely references. Here it is the opposite: this row is
        never saved without its FKs already set (an apply run creates it with
        ``design``/``placement``/``device`` populated), so ``snapshot_names()``
        already ran at creation and the snapshot is sitting in the row long
        before any later deletion. Django's SET_NULL pass on a deleted
        ``dcim.Device``/``Design``/``DesignPlacement`` nulls this row's FK
        columns directly by SQL (``Collector.delete``'s ``field_updates``
        pass) without going through ``save()`` -- but by then there is
        nothing left to snapshot; the value this field exists to preserve was
        already written. A ``pre_delete`` receiver would only be needed if a
        ``DesignApply`` row could exist with a set FK and an unpopulated
        snapshot, which ``save()`` here never allows.
        """
        if self.design_id:
            self.design_title = str(self.design)
        if self.device_id:
            self.device_name = str(self.device)

    def save(self, *args, **kwargs):
        self.snapshot_names()
        super().save(*args, **kwargs)

    def __str__(self):
        # Prefer the live FKs, fall back to the snapshots -- the case the
        # snapshots exist for is precisely a null design/device here.
        design_label = self.design or self.design_title or "?"
        device_label = self.device or self.device_name or "?"
        return f"{design_label}: apply of {device_label}"


class PlannedRack(NetBoxModel):
    """
    A rack that does not exist in NetBox yet (PLAN-templates.md §1, D3/D6): the
    prerequisite for planning a greenfield row. Today ``Design.racks`` and
    ``DesignPlacement.target_rack`` can only point at a ``dcim.Rack`` that
    already exists, so there was no way to plan a rack before it is built.

    A shared first-class object, not a design-scoped row (D3/D6): it is not
    owned by any one ``Design``. Designs reference it the same way they
    reference a real rack, so two planners drafting the same future rack
    share one row and see each other's placements through the existing
    peer-conflict machinery, with no new projection path. Rejected:
    materializing a real ``dcim.Rack`` with ``status=planned`` up front --
    that leaks into DCIM before anything is approved, orphans on design
    deletion, and gives two planners two racks instead of one shared plan.

    A ``NetBoxModel``, NOT a ``PrimaryModel``: see ``Design``'s docstring above
    for why the plugin owns ``description``/``comments`` directly rather than
    inheriting a base whose field set changes between NetBox minors
    (``PrimaryModel`` gained an ``owner`` FK in 4.5). The same reasoning
    applies here without qualification.

    ``location`` is REQUIRED (D4), which is the whole reason Apply can never
    hit an ``IntegrityError``. ``dcim.Rack`` itself is unique only on
    ``(location, name)`` (see ``Rack.Meta.constraints`` in dcim/models/racks.py)
    because ``location`` is nullable there and Postgres treats NULLs as
    distinct -- two real racks named "R1" with no location are not a
    conflict. If this model allowed the same, ``(location, name)`` would stop
    constraining anything for the planned racks that share a null location,
    and Apply's "does a matching rack already exist" lookup could find more
    than one candidate, or silently diverge from what ``dcim.Rack`` itself
    considers a duplicate. Making ``location`` mandatory here keeps this
    model's identity exactly as narrow as core's, so a match is always
    unique and creation on Apply can never violate core's own constraint.

    ``realized_rack`` (D7) is null until a design that uses this planned rack
    is applied; Apply then either adopts a matching real rack or creates one,
    and sets this to whichever it is. The row is never deleted at that
    point -- it survives forever, marked realized, so every design that still
    references it derefs to the real rack from then on (see ``resolve_rack``
    below), and the fact that this rack was once only planned is not erased.
    """

    clone_fields = ("u_height", "location")

    name = models.CharField(max_length=100)
    u_height = models.PositiveSmallIntegerField(default=RACK_U_HEIGHT_DEFAULT)
    location = models.ForeignKey(
        to="dcim.Location",
        on_delete=models.PROTECT,
        related_name="planned_racks",
        help_text=_(
            "Required -- see PlannedRack's docstring: this is what makes "
            "(location, name) a unique identity, matching dcim.Rack's own "
            "constraint, so Apply's adopt-or-create lookup can never be "
            "ambiguous."
        ),
    )
    # Null until a design using this planned rack is applied (D7). SET_NULL
    # rather than CASCADE/PROTECT: deleting the real rack later must not take
    # this bookkeeping row down with it -- the row's whole purpose is to
    # outlive that and keep saying "this used to be planned".
    realized_rack = models.ForeignKey(
        to="dcim.Rack",
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
    )
    description = models.CharField(max_length=200, blank=True)
    comments = models.TextField(blank=True)

    class Meta:
        ordering = ("location", "name")
        verbose_name = "planned rack"
        verbose_name_plural = "planned racks"
        constraints = [
            models.UniqueConstraint(
                fields=("location", "name"),
                name="%(app_label)s_%(class)s_unique_location_name",
            ),
        ]

    def __str__(self):
        return self.name or f"Planned rack {self.pk}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_rack_design:plannedrack", args=[self.pk])

    @property
    def docs_url(self):
        return DOCS_BASE_URL

    def clean(self):
        super().clean()
        # dcim.Rack validates u_height with MinValueValidator(1) plus
        # RACK_U_HEIGHT_MAX (there is no RACK_U_HEIGHT_MIN constant in
        # dcim.constants across 4.4-4.6 -- core hardcodes the lower bound of 1
        # inline). Mirrored here rather than invented, so a planned rack can
        # never describe a height core itself would reject once realized.
        if self.u_height < 1 or self.u_height > RACK_U_HEIGHT_MAX:
            raise ValidationError({
                "u_height": _(
                    "U height must be between 1 and {max}."
                ).format(max=RACK_U_HEIGHT_MAX),
            })

    @property
    def site(self):
        # Convenience mirror of dcim.Rack.site: every reader that groups or
        # filters racks by site (the editor, the projection) can treat a
        # planned rack the same way without a location-vs-site branch.
        return self.location.site

    @property
    def is_realized(self):
        return self.realized_rack_id is not None

    # --- deletion rules (PLAN-templates.md §1 "Still open" / T1.9) -------------
    #
    # A PlannedRack is a SHARED object (D6): several designs, possibly owned by
    # different people, can reference the very same row. That is what makes the
    # deletion question different from an ordinary NetBoxModel delete -- there
    # is no single "owner" to ask. The guards below are deliberately a FULL
    # REFUSAL, not the two-step confirm/retry shape ``remove_rack`` (api/views.py)
    # uses: ``remove_rack`` detaches ONE rack from ONE design's own scope, a
    # blast radius the requesting user can see and confirm in the same request.
    # Deleting this row outright can destroy placements/feeds/rack-power
    # belonging to OTHER designs the requester may not even have permission to
    # view, some of which may be FROZEN (approved) -- exactly the kind of
    # cross-design, silent, possibly-irreversible loss the ``device`` FK's
    # SET_NULL+``stale`` treatment (DesignPlacement, above) exists to prevent
    # for a single design. A confirm click here would let one user blow away
    # another design's approved layout; refusing forces every referencing
    # design to detach first, mirroring how ``_design_children_rest_message``
    # refuses a Design delete outright rather than offering to confirm through it.
    def referencing_designs(self):
        """
        Every ``Design`` that still depends on this planned rack, through ANY
        of the paths that can name one: the explicit planning-scope M2M
        (``scoped_designs``), or a CASCADE FK from ``DesignPlacement``
        (``target_planned_rack``), ``DesignPowerFeed`` or ``DesignRackPower``
        (``planned_rack``, D25/D26). Nothing enforces that the CASCADE paths
        stay inside a design's declared scope, so all four are checked
        independently rather than trusting ``scoped_designs`` alone -- a
        placement pointed at a planned rack the design never formally scoped
        would otherwise be an undetected loss. The two power models declare
        ``related_name="+"`` (no reverse accessor), so they are queried
        directly rather than through a Python attribute.
        """
        design_ids = set(self.scoped_designs.values_list("pk", flat=True))
        design_ids |= set(self.design_placements.values_list("design_id", flat=True))
        design_ids |= set(
            DesignPowerFeed.objects.filter(planned_rack=self).values_list(
                "design_id", flat=True
            )
        )
        design_ids |= set(
            DesignRackPower.objects.filter(planned_rack=self).values_list(
                "design_id", flat=True
            )
        )
        return Design.objects.filter(pk__in=design_ids).order_by("pk")

    @property
    def is_orphan(self):
        """No design references this row through any path any more --
        PLAN-templates.md decision 3: dead weight, safe to delete, and exactly
        what the list view's ``orphan`` filter (filtersets.py) surfaces for a
        human to review and remove manually. Realization does not exempt a row
        from being an orphan -- a realized rack every design has since stopped
        referencing is still an orphan in this sense, it is just also
        protected from deletion by ``is_realized`` (see the delete guards in
        views.py / api/views.py, which check both independently).
        """
        return not self.referencing_designs().exists()

    @classmethod
    def orphaned(cls):
        """The queryset backing ``is_orphan`` above, without an N+1 per-row
        query for a list view: every PlannedRack with none of ``scoped_designs``,
        a referencing ``DesignPlacement``, ``DesignPowerFeed`` or
        ``DesignRackPower``.
        """
        referenced_via_placement = DesignPlacement.objects.filter(
            target_planned_rack__isnull=False
        ).values_list("target_planned_rack_id", flat=True)
        referenced_via_feed = DesignPowerFeed.objects.filter(
            planned_rack__isnull=False
        ).values_list("planned_rack_id", flat=True)
        referenced_via_power = DesignRackPower.objects.filter(
            planned_rack__isnull=False
        ).values_list("planned_rack_id", flat=True)
        return (
            cls.objects.filter(scoped_designs__isnull=True)
            .exclude(pk__in=referenced_via_placement)
            .exclude(pk__in=referenced_via_feed)
            .exclude(pk__in=referenced_via_power)
        )

    @property
    def matches_existing_rack(self):
        """
        True when a REAL ``dcim.Rack`` now exists at this row's ``(location,
        name)`` -- NetBox's own uniqueness identity (D4) -- while this row is
        STILL UNREALIZED. This is worse than a plain orphan (decision 3): Apply
        would silently ADOPT that rack (D5) the moment any design using this
        row is applied, so a planner editing this row today may already be
        looking at a plan for a rack somebody else created (or renamed onto)
        by hand outside this plugin, with no indication anything changed.
        Always ``False`` once realized -- ``realized_rack`` already names the
        one real rack this row means from that point on, and a second
        same-named rack appearing later in DCIM is none of this row's concern.
        """
        if self.is_realized:
            return False
        # Local import: avoids a module-load-order dependency on dcim from
        # this plugin's models module (see the string-only "dcim.Rack" FKs
        # elsewhere in this file for the same reason).
        from dcim.models import Rack

        return Rack.objects.filter(location=self.location, name=self.name).exists()


def resolve_rack(real, planned):
    """
    The one place D7's deref happens: return the ``dcim.Rack`` a reader
    should actually look at, or ``None`` while the rack is still only
    planned.

    ``real`` and ``planned`` are whatever a caller's own two FKs currently
    hold (e.g. ``DesignPlacement.target_rack`` / ``target_planned_rack``,
    once those exist -- T1.2). A real rack always wins outright: if it is
    set there is no planned rack to resolve at all. Otherwise, a set
    ``planned`` rack derefs through ``realized_rack``, which is ``None``
    until Apply runs and the real rack from then on. Written once here
    because a realized ``PlannedRack`` must deref identically everywhere it
    is read -- ``DesignPlacement``, ``DesignPowerFeed``, ``DesignRackPower``
    and the projection all call this rather than re-deriving the same
    ``if``/``else`` four separate ways, which is exactly how such logic
    drifts.
    """
    if real is not None:
        return real
    if planned is not None:
        return planned.realized_rack
    return None


def rack_key(real, planned):
    """
    A namespaced string key identifying a rack across BOTH kinds, for use
    anywhere racks are keyed by pk -- most notably the
    ``recompute-distribution`` API response and ``rack.js``'s indexing of it.

    ``dcim.Rack`` pk 5 and ``PlannedRack`` pk 5 are different racks, so a bare
    integer key collides the moment a design has both kinds in play. The
    format is ``"r:<pk>"`` for a real rack and ``"p:<pk>"`` for a planned one.
    Negative integers for planned racks and a shared pk sequence across the
    two apps were both considered and rejected (PLAN-templates.md D27): the
    first is a trick that still needs explaining years later, the second
    isn't possible when the two models live in different Django apps.
    Exactly one of ``real``/``planned`` is expected to be set (mirrors
    ``resolve_rack``'s inputs); returns ``None`` if neither is.
    """
    if real is not None:
        return f"r:{real.pk}"
    if planned is not None:
        return f"p:{planned.pk}"
    return None


class TemplateGroup(NetBoxModel):
    """
    An ordered set of ``Template``s describing a multi-rack product pod
    (PLAN-templates.md §2, D14) -- e.g. "compute pod": a spine template, a
    leaf template repeated per rack, a storage template. Purely organizational,
    exactly like ``DesignGroup``: this model never affects stamping by itself.
    A group's stamping semantics -- repetition vs. correspondence -- belong to
    Phase 3 (stamping a template into a design), which is out of scope here.
    """

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ("name",)
        verbose_name = "template group"
        verbose_name_plural = "template groups"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:netbox_rack_design:templategroup", args=[self.pk])

    @property
    def docs_url(self):
        return DOCS_BASE_URL


class Template(NetBoxModel):
    """
    A reusable rack layout with no site (PLAN-templates.md §2, D13/D14): "our
    standard ToR", "our standard product rack". Built once, then stamped onto
    racks in real designs -- stamping itself is Phase 3 and is NOT implemented
    here. Exactly ONE rack per ``Template``: a multi-rack product pod is a
    ``TemplateGroup`` of several Templates, not one Template with several
    racks, so stamping always answers "which rack does THIS template's content
    go in" without needing an inner index.

    A ``NetBoxModel``, NOT a ``PrimaryModel``, and NOT ``Design(is_template=
    True)`` (D13) -- see ``Design``'s and ``PlannedRack``'s docstrings above
    for why the plugin owns ``description`` directly rather than inheriting a
    base whose field set changes between NetBox minors (``PrimaryModel``
    gained an ``owner`` FK in 4.5). Reusing ``Design`` was rejected separately:
    it would mean making ``site`` nullable -- it is ``PROTECT, NOT NULL`` and
    ``Meta.ordering`` starts with it -- and filtering templates out of every
    design list, filterset and API endpoint that currently assumes every
    ``Design`` has one.

    ``u_height`` (D21) is a NOMINAL CANVAS SIZE ONLY, so a template editor has
    a rack to draw while there is no real rack yet. It is IGNORED once the
    template is stamped -- stamping checks only whether the template's devices
    fit in the TARGET rack's free space (PLAN-templates.md §3), never this
    field. A template drawn at 47U that only uses 5U of it stamps fine onto a
    24U rack. Do not read this field as a constraint anywhere outside the
    editor's canvas sizing -- that would reintroduce, backwards, exactly the
    "middle anchor" rule Phase 3 deliberately rejected (D15/D16).
    """

    name = models.CharField(max_length=100)
    description = models.CharField(max_length=200, blank=True)
    group = models.ForeignKey(
        to="netbox_rack_design.TemplateGroup",
        on_delete=models.SET_NULL,
        related_name="templates",
        blank=True,
        null=True,
    )
    # Position within the group (e.g. a spine template before the leaves).
    # Meaningless for a group-less template, which still gets a default so
    # ordering never depends on comparing against NULL.
    order = models.PositiveSmallIntegerField(default=0)
    u_height = models.PositiveSmallIntegerField(default=RACK_U_HEIGHT_DEFAULT)
    # An explicit counter, bumped whenever this template's CONTENTS change --
    # a ``TemplatePlacement`` under it is added, edited, or removed (see
    # ``bump_version()`` and ``TemplatePlacement.save()``/``delete()`` below).
    # Recorded on every ``DesignPlacement.from_template_version`` (D20) at
    # stamp time, so provenance can later ask "has this template changed
    # since I stamped it?" by comparing that snapshot against the live value.
    #
    # NOT ``last_updated`` (the ``NetBoxModel`` timestamp this row already
    # has): ``last_updated`` only changes when THIS row is saved, and saving a
    # child ``TemplatePlacement`` does not touch its parent row at all -- the
    # exact case D20 exists to detect (a planner edits a device inside the
    # template, the Template row itself is never written). An explicit
    # counter, bumped by the child's own save/delete, is the only mechanism
    # that actually observes that edit. It also survives a deployment
    # correcting the template's own name/description without that alone
    # looking like a content change, which a naive "any save bumps it" rule
    # would not.
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ("group", "order", "name")
        verbose_name = "template"
        verbose_name_plural = "templates"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:netbox_rack_design:template", args=[self.pk])

    @property
    def docs_url(self):
        return DOCS_BASE_URL

    def clean(self):
        super().clean()
        # Mirrors PlannedRack.clean(): a canvas height core itself would
        # reject once any rack (planned or real) is actually built at it.
        # dcim.Rack validates u_height with MinValueValidator(1) plus
        # RACK_U_HEIGHT_MAX; there is no RACK_U_HEIGHT_MIN constant, core
        # hardcodes the lower bound of 1 inline, so this is mirrored rather
        # than invented.
        if self.u_height < 1 or self.u_height > RACK_U_HEIGHT_MAX:
            raise ValidationError({
                "u_height": _(
                    "U height must be between 1 and {max}."
                ).format(max=RACK_U_HEIGHT_MAX),
            })

    def bump_version(self):
        """Increment ``version`` in place, atomically.

        Called by ``TemplatePlacement.save()``/``delete()`` whenever this
        template's contents change. An ``F()``-expression update rather than
        ``self.version += 1; self.save()`` -- the latter would race two
        planners editing different placements of the same template
        concurrently (last writer's increment wins, the other's is lost); the
        ``F()`` update is a single atomic SQL statement instead. ``self.version``
        is refreshed from the database afterwards so the in-memory instance
        (e.g. one already loaded by the caller) reflects the new value too.
        """
        type(self).objects.filter(pk=self.pk).update(version=models.F("version") + 1)
        self.refresh_from_db(fields=["version"])


class TemplatePlacement(NetBoxModel):
    """
    One device within a ``Template`` (PLAN-templates.md §2, D8/D9): everything
    a ``kind=add`` ``DesignPlacement`` carries EXCEPT the rack, the absolute
    position, and power --

        stored:      device_type, device_role, tenant, planning_data, face
        NOT stored:  power_config, power_source_device, planned_power_feed,
                     a device name, an absolute U position

    Power is site-bound and is configured after the device is dropped into an
    actual design, exactly as for a device dragged in by hand -- a real feed
    cannot travel with a template. Names are never stored (D9) -- the naming
    engine produces them at stamp time from the target design/site/rack, none
    of which this row has.

    Placement within the rack is ANCHOR + ORDER, not an absolute position or a
    stored offset (D15/D16/D17): ``anchor`` is ``"top"`` or ``"bottom"`` --
    there is no "middle" -- and ``order`` is this placement's position in the
    walk outward from that anchor. There is no reserved-gap field: stamping
    (Phase 3, not implemented here) compacts every placement against its
    anchor, skipping only slots a real occupant already holds in the TARGET
    rack. A deliberate gap is expressed by putting an actual blanking-panel
    device type in the template, the same as a real rack elevation would show
    one.

    ``label`` (D22) is free text for a human reading the template in an
    editor -- "leaf A", "spine 2" -- documentation only. It never becomes a
    device name and never leaves the template.

    Chassis and blades nest via ``parent_placement`` + ``target_bay_name``,
    mirroring ``DesignPlacement`` case B (a chassis planned in the SAME
    design has no real ``dcim.DeviceBay`` rows yet, so a blade addresses it by
    name against the chassis type's ``DeviceBayTemplate``s instead -- there is
    no case A/C here, because a template never references a real device or
    another template's placement). ``clean()`` below refuses two shapes
    (D10): a child (blade) device type with no ``parent_placement`` at all --
    an orphan child, which may not exist in a template without its chassis --
    and a ``parent_placement`` naming a placement that belongs to a DIFFERENT
    template.
    """

    template = models.ForeignKey(
        to="netbox_rack_design.Template",
        on_delete=models.CASCADE,
        related_name="placements",
    )
    device_type = models.ForeignKey(
        to="dcim.DeviceType",
        on_delete=models.PROTECT,
        related_name="+",
    )
    device_role = models.ForeignKey(
        to="dcim.DeviceRole",
        on_delete=models.PROTECT,
        related_name="+",
        blank=True,
        null=True,
    )
    tenant = models.ForeignKey(
        to="tenancy.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        blank=True,
        null=True,
    )
    # Values for the deployment's config-declared placement fields (D8) --
    # the same schema and validation DesignPlacement.planning_data uses (see
    # planning_fields.validate_planning_data), always validated as though for
    # an 'add' since a template placement IS the precursor of one.
    planning_data = models.JSONField(blank=True, null=True)
    face = models.CharField(max_length=10, blank=True)
    anchor = models.CharField(
        max_length=10,
        choices=TemplatePlacementAnchorChoices,
        default=TemplatePlacementAnchorChoices.ANCHOR_TOP,
    )
    order = models.PositiveSmallIntegerField(default=0)
    label = models.CharField(max_length=100, blank=True)
    # The chassis this blade goes into, within THIS SAME template (D10). CASCADE
    # is safe -- unlike DesignPlacement's cross-design base_parent_placement,
    # a template's own chassis and the blades planned into it are one row's
    # worth of work, so deleting the chassis legitimately deletes its blades.
    parent_placement = models.ForeignKey(
        to="self",
        on_delete=models.CASCADE,
        related_name="bay_children",
        blank=True,
        null=True,
        help_text="The placement of the chassis this blade goes into, within "
                  "this same template.",
    )
    target_bay_name = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ("template", "order", "pk")
        verbose_name = "template placement"
        verbose_name_plural = "template placements"
        constraints = [
            models.UniqueConstraint(
                fields=("template", "parent_placement", "target_bay_name"),
                condition=models.Q(parent_placement__isnull=False),
                name="%(app_label)s_%(class)s_unique_template_planned_bay",
            ),
        ]

    def __str__(self):
        if self.label:
            return f"{self.device_type} \"{self.label}\""
        return str(self.device_type)

    def get_absolute_url(self):
        return reverse("plugins:netbox_rack_design:templateplacement", args=[self.pk])

    @property
    def docs_url(self):
        return DOCS_BASE_URL

    def clean(self):
        super().clean()

        self.planning_data = planning_fields.validate_planning_data(self.planning_data, "add") or None

        if self.parent_placement_id:
            parent = self.parent_placement
            if parent.pk == self.pk:
                raise ValidationError({"parent_placement": "A placement cannot be its own parent."})
            if self.template_id and parent.template_id != self.template_id:
                raise ValidationError({
                    "parent_placement": "The chassis placement must belong to the same template.",
                })
            parent_type = parent.device_type
            if parent_type is None or not parent_type.is_parent_device:
                raise ValidationError({
                    "parent_placement": "The referenced placement is not a parent "
                                        "(chassis) device type.",
                })
            if not self.target_bay_name:
                raise ValidationError({
                    "target_bay_name": "A bay name is required when the chassis is "
                                       "itself planned.",
                })
            valid_bays = set(
                parent_type.devicebaytemplates.values_list("name", flat=True)
            )
            if valid_bays and self.target_bay_name not in valid_bays:
                raise ValidationError({
                    "target_bay_name": f"{parent_type} has no bay named "
                                       f"{self.target_bay_name!r}.",
                })
        elif self.device_type_id and self.device_type.is_child_device:
            # D10: a blade may not exist in a template without its chassis.
            raise ValidationError({
                "parent_placement": "A child (blade) device type requires "
                                    "parent_placement naming its chassis "
                                    "within this template.",
            })

    def save(self, *args, **kwargs):
        # A template's ``version`` (D20) exists to detect exactly this: its
        # CONTENTS changed. The parent Template row itself is not written when
        # a child placement is added or edited, so nothing observes that
        # unless the child bumps it explicitly here -- see Template.version's
        # docstring for why `last_updated` cannot substitute for this.
        super().save(*args, **kwargs)
        self.template.bump_version()

    def delete(self, *args, **kwargs):
        # Capture the template before the row (and its FK) is gone, so the
        # bump below still has something to bump -- removing a placement is
        # as much a content change as adding or editing one.
        template = self.template
        result = super().delete(*args, **kwargs)
        template.bump_version()
        return result
