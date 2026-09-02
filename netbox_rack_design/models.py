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
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel

from . import planning_fields
from .choices import DesignPlacementKindChoices, DesignStatusChoices

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
)

# The plugin's hosted documentation (MkDocs -> GitHub Pages). NetBoxModel's
# default ``docs_url`` points at ``/static/docs/models/...``, which only exists
# for NetBox's OWN core docs -- a plugin's docs are not built into that path. Per
# the plugin dev guide (Database Models: "Plugin models can override this to
# return a custom URL ... your plugin's documentation"), each model below
# overrides ``docs_url`` to this site so the object detail page's help link
# resolves instead of 404ing. Kept in sync with ``mkdocs.yml`` ``site_url``.
DOCS_BASE_URL = "https://ravenrs.github.io/netbox-rack-design/"


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
    site = models.ForeignKey(
        to="dcim.Site",
        on_delete=models.PROTECT,
        related_name="rack_designs",
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

    # --- optional grouping ----------------------------------------------------
    group = models.ForeignKey(
        to="netbox_rack_design.DesignGroup",
        on_delete=models.SET_NULL,
        related_name="designs",
        blank=True,
        null=True,
    )

    clone_fields = ("site", "status", "summary", "link", "group")

    class Meta:
        ordering = ("site", "sequence", "pk")
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
        # Auto-assign a gapped per-site execution sequence on first save.
        if self.sequence is None:
            last = (
                Design.objects.filter(site=self.site)
                .aggregate(models.Max("sequence"))
                .get("sequence__max")
            )
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

        # A chain across two sites is meaningless (PLAN-design-chains.md gap
        # 1): a parent's placements are site-scoped, so a child in a different
        # site could never actually replay them into its own racks. Mirrors
        # the ``racks`` site check below, but -- unlike ``racks`` (a M2M) --
        # ``based_on`` is a plain FK, so its value IS visible on an unsaved
        # instance (no pk needed to read ``self.based_on``); this check does
        # NOT have the M2M pk-timing caveat documented on the ``racks`` check,
        # so it runs unconditionally and also covers CREATE, not just edits.
        if self.based_on_id and self.site_id and self.based_on.site_id != self.site_id:
            raise ValidationError(
                {"based_on": "The parent design's site does not match this "
                              "design's site."}
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
                                   "baseline. Create a new version of this design and "
                                   "re-base them onto it instead."}
                    )

        # Every scoped rack must belong to this design's site (consistent with the
        # site-scoping of placements). M2M-timing caveat: a many-to-many relation
        # cannot be read on an unsaved instance (pk=None) -- Django raises before
        # the through-rows exist -- so this check only runs once the design is
        # persisted (i.e. on edits). For a brand-new design the racks are attached
        # only after the initial save, so the form/serializer layer (a later phase)
        # must re-run full_clean() post-save to enforce this on create.
        if self.pk and self.site_id:
            offending = self.racks.exclude(site_id=self.site_id)
            if offending.exists():
                names = ", ".join(str(rack) for rack in offending)
                raise ValidationError(
                    {"racks": f"These racks are not in the design's site: {names}."}
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

    # Intended role/tenant for a planned new device (add). Only meaningful for
    # kind=add; applied to the real device when the design is later executed.
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
            raise ValidationError(
                "This design is approved, and approved designs are frozen: "
                "its placements cannot be created or edited. Set the design "
                "back to draft, or create a new version of it, to make this change."
            )

        # Config-declared planning fields: validated against the deployment's
        # ``placement_fields`` schema and normalised in place, so what reaches
        # the database is always type-correct and free of keys nothing reads.
        self.planning_data = planning_fields.validate_planning_data(self.planning_data, kind) or None

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

        # add / move require a target rack; the target position is optional --
        # None means a tray (non-racked) target (spec §9.5: mount vs dismount vs
        # tray-to-tray reassociation are all distinguished by target_position
        # being set vs None, never by a separate flag).
        if not self.target_rack:
            raise ValidationError({"target_rack": "A target rack is required."})
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
            if self.design_id and parent.rack_id and parent.rack.site_id != self.design.site_id:
                raise ValidationError({
                    "target_bay": "The chassis is not in the design's site.",
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
        A position-less (tray) target validates only same-site rack membership
        (spec §9.5) -- there is no slot availability to check since a tray is
        an unordered list, not a grid.
        """
        if self.design_id and self.target_rack.site_id != self.design.site_id:
            raise ValidationError(
                {"target_rack": "Target rack must be in the design's site."}
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
    """

    clone_fields = ("design", "rack", "voltage", "amperage", "phase", "supply")

    design = models.ForeignKey(
        to="netbox_rack_design.Design",
        on_delete=models.CASCADE,
        related_name="planned_feeds",
    )
    rack = models.ForeignKey(
        to="dcim.Rack",
        on_delete=models.CASCADE,
        related_name="+",
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
        ordering = ("design", "rack", "name")
        verbose_name = "planned power feed"
        verbose_name_plural = "planned power feeds"
        constraints = [
            models.UniqueConstraint(
                fields=("design", "rack", "name"),
                name="%(app_label)s_%(class)s_unique_design_rack_name",
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
            raise ValidationError(
                "This design is approved, and approved designs are frozen: "
                "its planned power feeds cannot be created or edited. Set "
                "the design back to draft, or create a new version of it, "
                "to make this change."
            )

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
    )
    # Same JSON shape as DesignPlacement.power_config, minus "feed" (a rack has
    # no feed of its own): {"source", "copied_from", "custom_fields": {...}}.
    power_config = models.JSONField(blank=True, null=True)

    class Meta:
        ordering = ("design", "rack")
        verbose_name = "design rack power"
        verbose_name_plural = "design rack power"
        constraints = [
            models.UniqueConstraint(
                fields=("design", "rack"),
                name="%(app_label)s_%(class)s_unique_design_rack",
            ),
        ]

    def __str__(self):
        return f"{self.design}: power for {self.rack}"

    @classmethod
    def effective_custom_fields(cls, design, rack):
        """The MERGED rack power custom fields ``design`` should read for
        ``rack``, across its baseline chain (PLAN-design-chains.md G5 item 2).

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

        merged = {}
        chain, conflict = projection.resolve_baseline_chain(design)
        for ancestor in chain:
            try:
                row = cls.objects.get(design=ancestor, rack=rack)
            except cls.DoesNotExist:
                continue
            merged.update((row.power_config or {}).get("custom_fields") or {})
        try:
            own = cls.objects.get(design=design, rack=rack)
        except cls.DoesNotExist:
            own = None
        if own is not None:
            merged.update((own.power_config or {}).get("custom_fields") or {})
        return merged, conflict
