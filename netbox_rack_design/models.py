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

    # --- device-bay targeting (a blade into a chassis) -------------------------
    # Core forbids a child device from carrying a rack position or a face
    # (dcim.Device.clean), so a blade is never placed AT a U -- it is placed IN a
    # parent's bay. Two cases, exactly one field each:
    #
    #   A. the chassis already exists in DCIM -> ``target_bay`` names the real
    #      dcim.DeviceBay row.
    #   B. the chassis is itself an 'add' in THIS design -> it has no bays yet
    #      (core instantiates them from the device type only when the device is
    #      created), so the blade points at the chassis's placement via
    #      ``parent_placement`` and names its bay via ``target_bay_name``, which is
    #      validated against the parent type's DeviceBayTemplates.
    #
    # ``target_bay_name`` is also filled in case A (mirroring the bay's name) so a
    # consumer has one field to read for "which bay" regardless of case.
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
        ]

    def __str__(self):
        label = self.device or self.device_type or "?"
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

        # A planned PDU binds to at most ONE feed (real xor planned).
        if self.real_power_feed_id and self.planned_power_feed_id:
            raise ValidationError(
                "A placement cannot bind to both a real and a planned power feed."
            )

        # A planned PDU's custom fields come from at most ONE source: a referenced
        # real device (cf read live) OR manual power_config -- never both (docs/
        # pdu-distribution-spec §6.5).
        if self.power_source_device_id and (self.power_config or {}).get("custom_fields"):
            raise ValidationError(
                "A placement cannot both reference a source device and carry "
                "manual power_config custom fields."
            )

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

        # A bay target (blade into a chassis) is mutually exclusive with a rack
        # slot, and short-circuits the U/face validation below.
        if self.target_bay_id or self.parent_placement_id:
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

    def _placed_device_type(self):
        """The DeviceType being placed: the add's own, or the moved device's."""
        if self.device_type_id:
            return self.device_type
        return self.device.device_type if self.device_id else None

    def _validate_bay_target(self):
        """
        Validate a blade placement: into a real chassis bay (``target_bay``) or
        into a bay of a chassis planned in this same design
        (``parent_placement`` + ``target_bay_name``).
        """
        if self.target_bay_id and self.parent_placement_id:
            raise ValidationError(
                "A placement targets either a real device bay or a planned "
                "chassis, never both."
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

        # Planned chassis (case B).
        parent_placement = self.parent_placement
        if parent_placement.pk == self.pk:
            raise ValidationError({"parent_placement": "A placement cannot be its own parent."})
        if self.design_id and parent_placement.design_id != self.design_id:
            raise ValidationError({
                "parent_placement": "The chassis placement must belong to the same design.",
            })
        parent_type = parent_placement._placed_device_type()
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
