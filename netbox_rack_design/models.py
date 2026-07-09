"""
Models for NetBox Rack Design.

A *Design* is a proposed set of rack changes (one plan) that overlays on real
NetBox data without mutating it until applied. Designs are versioned
(clone-and-tweak; one approved version per plan), ordered for execution per
site, may declare explicit dependencies on other designs, and may optionally be
grouped into a larger (hierarchical) effort via DesignGroup.

All terminology is generic — no organization-specific concepts are hardcoded.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel, PrimaryModel

from .choices import DesignPlacementKindChoices, DesignStatusChoices

__all__ = (
    "DesignGroup",
    "Design",
    "DesignPlacement",
    "FavoriteDeviceType",
    "HiddenDesignRack",
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

    def clean(self):
        super().clean()
        # Guard against cyclic parenting.
        ancestor = self.parent
        while ancestor is not None:
            if ancestor.pk == self.pk:
                raise ValidationError({"parent": "A group cannot be its own ancestor."})
            ancestor = ancestor.parent


class Design(PrimaryModel):
    """
    A proposed set of rack changes (one plan / one version).
    """

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

    def get_status_color(self):
        return DesignStatusChoices.colors.get(self.status)

    @property
    def version_root(self):
        """The root design that groups this plan's versions (self if this is the root)."""
        return self.root or self

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
    device = models.ForeignKey(
        to="dcim.Device",
        on_delete=models.CASCADE,
        related_name="design_placements",
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

    class Meta:
        ordering = ("design", "target_position", "pk")
        verbose_name = "design placement"
        verbose_name_plural = "design placements"

    def __str__(self):
        label = self.device or self.device_type or "?"
        return f"{self.get_kind_display()}: {label}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_rack_design:designplacement", args=[self.pk])

    def get_kind_color(self):
        return DesignPlacementKindChoices.colors.get(self.kind)

    def clean(self):
        super().clean()
        kind = self.kind

        if kind == DesignPlacementKindChoices.KIND_ADD:
            if not self.device_type:
                raise ValidationError({"device_type": "An 'add' requires a device type."})
            if self.device:
                raise ValidationError({"device": "An 'add' must not reference an existing device."})
        else:
            if not self.device:
                raise ValidationError({"device": f"A '{kind}' requires an existing device."})
            if self.device_type:
                raise ValidationError({"device_type": f"A '{kind}' must not set a device type."})
            if self.device_role:
                raise ValidationError({"device_role": f"A '{kind}' must not set a device role."})
            if self.tenant:
                raise ValidationError({"tenant": f"A '{kind}' must not set a tenant."})

        if kind == DesignPlacementKindChoices.KIND_REMOVE:
            return  # No target for a removal.

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
        """
        device_type = self.device_type or (self.device.device_type if self.device else None)
        if device_type is None:
            return
        rack_face = None if device_type.is_full_depth else (self.target_face or None)
        exclude = [self.device.pk] if self.device_id else []
        exclude += [pk for pk in self._vacated_device_ids() if pk not in exclude]
        available = self.target_rack.get_available_units(
            u_height=device_type.u_height, rack_face=rack_face, exclude=exclude
        )
        if self.target_position and float(self.target_position) not in [float(u) for u in available]:
            raise ValidationError(
                {"target_position": f"U{self.target_position} is not available in {self.target_rack}."}
            )

    def _vacated_device_ids(self):
        """PKs of devices this design frees from their real slots, so they don't
        count as occupying the target rack when validating another placement.

        Prefers the batch context the save-layout view injects (it knows every
        device the current submit moves/removes, including ones not yet
        persisted); otherwise reads the design's already-saved move/remove rows.
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


class FavoriteDeviceType(models.Model):
    """
    A per-user UI preference: a device type the user has "starred" in the
    catalog palette, surfaced for quick access.

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
    device_type = models.ForeignKey(
        to="dcim.DeviceType",
        on_delete=models.CASCADE,
        related_name="+",
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("user", "device_type")
        verbose_name = "favorite device type"
        verbose_name_plural = "favorite device types"
        constraints = [
            models.UniqueConstraint(
                fields=("user", "device_type"),
                name="%(app_label)s_%(class)s_unique_user_device_type",
            ),
        ]

    def __str__(self):
        return f"{self.user}: {self.device_type}"


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
