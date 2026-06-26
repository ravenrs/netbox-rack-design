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

__all__ = ("DesignGroup", "Design", "DesignPlacement", "FavoriteDeviceType")


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

        # add / move require a target placement.
        if not self.target_rack:
            raise ValidationError({"target_rack": "A target rack is required."})
        if self.target_position is None:
            raise ValidationError({"target_position": "A target position is required."})

        self._validate_target_slot()

    def _validate_target_slot(self):
        """Reuse NetBox's own collision logic to check the target slot is free."""
        device_type = self.device_type or (self.device.device_type if self.device else None)
        if device_type is None:
            return
        rack_face = None if device_type.is_full_depth else (self.target_face or None)
        exclude = [self.device.pk] if self.device_id else []
        available = self.target_rack.get_available_units(
            u_height=device_type.u_height, rack_face=rack_face, exclude=exclude
        )
        if self.target_position and float(self.target_position) not in [float(u) for u in available]:
            raise ValidationError(
                {"target_position": f"U{self.target_position} is not available in {self.target_rack}."}
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
