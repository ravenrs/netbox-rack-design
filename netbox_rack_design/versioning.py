"""
Design versioning: clone a design into a new, draft version of the same plan
(PLAN-design-versions.md §2/§4).

Distinct from ``derive`` (``api/views.py``), which creates a new PLAN (a
child, ``based_on`` pointing at the source) that inherits placements LIVE
through the baseline chain and copies nothing. A version is the SAME plan,
revised: it copies ``based_on`` (not points at the source) and deep-copies
the design's whole content -- placements, planned power feeds, and rack
power overrides -- because nothing else can stand in for what it copies.
"""

from django.db import transaction
from django.db.models import Max, Q

from .choices import DesignStatusChoices
from .models import Design, DesignPlacement, DesignPowerFeed, DesignRackPower

__all__ = ("new_version",)


def new_version(design, *, title=None):
    """
    Clone ``design`` into a new, draft version of the same plan.

    Copied verbatim onto the new ``Design`` row: ``title`` (the source's own,
    unless ``title`` is given -- versions are distinguished by ``__str__``,
    "<title> (v<version>)", so no suffix is invented here), ``site``,
    ``group``, ``description``, ``comments``, ``summary``, ``link``,
    ``based_on`` (the SAME ancestor as the source -- a version is not a new
    plan), ``racks`` and ``depends_on`` (both M2M, set after ``save()``, since
    an M2M needs a pk).

    Set explicitly: ``status`` is always ``draft`` (even when the source is
    approved -- the clone has not been reviewed), ``root`` is the source's
    ``version_root`` (self when the source IS the root), ``version`` is one
    past the highest version anywhere in that root's group.

    NOT copied: ``sequence`` (auto-assigned per site on first save) and
    ``DesignApply`` rows (an apply record says "this design created this
    device" -- the clone has created nothing, and copying would additionally
    collide with the ``UniqueConstraint(fields=("placement",))``).

    ``DesignPowerFeed`` rows are cloned before placements, and
    ``DesignRackPower`` rows are cloned too (nothing references the latter).
    Placements are cloned in two passes -- see the loop below for why.

    Returns the new ``Design``. Raises ``ValidationError`` (via
    ``full_clean()``) if any copied row would not itself be valid; the whole
    clone runs inside one ``transaction.atomic()`` so a failure partway
    through leaves nothing behind.
    """
    with transaction.atomic():
        root = design.version_root
        max_version = (
            Design.objects.filter(Q(root=root) | Q(pk=root.pk))
            .aggregate(Max("version"))["version__max"]
        )

        clone = Design(
            title=title if title is not None else design.title,
            site=design.site,
            group=design.group,
            description=design.description,
            comments=design.comments,
            summary=design.summary,
            link=design.link,
            based_on=design.based_on,
            status=DesignStatusChoices.STATUS_DRAFT,
            root=root,
            version=(max_version or 0) + 1,
        )
        clone.full_clean()
        clone.save()
        # M2M needs a pk -- same ordering `derive` uses (api/views.py:2289).
        clone.racks.set(design.racks.all())
        clone.depends_on.set(design.depends_on.all())

        # --- planned power feeds: cloned FIRST, placements remap onto these ---
        feed_map = {}  # old DesignPowerFeed pk -> new DesignPowerFeed
        for feed in design.planned_feeds.all():
            new_feed = DesignPowerFeed(
                design=clone,
                rack=feed.rack,
                name=feed.name,
                voltage=feed.voltage,
                amperage=feed.amperage,
                phase=feed.phase,
                supply=feed.supply,
            )
            new_feed.full_clean()
            new_feed.save()
            feed_map[feed.pk] = new_feed

        # --- rack power overrides: nothing references these ---
        for rack_power in design.rack_power.all():
            new_rack_power = DesignRackPower(
                design=clone, rack=rack_power.rack, power_config=rack_power.power_config,
            )
            new_rack_power.full_clean()
            new_rack_power.save()

        # --- placements: two passes, because of the self-references ----------
        #
        # `parent_placement` names another placement in THIS SAME design (a
        # blade's chassis) and must be remapped onto the clone's own copy of
        # that row -- which does not exist yet when an earlier-created row
        # might need to point at a later one. So: pass 1 creates every row
        # with `parent_placement` null and records old_pk -> new row; pass 2
        # sets `parent_placement` from that map.
        #
        # `base_placement` / `base_parent_placement` point at an ANCESTOR
        # design's rows (case B/C's cross-design twins). The clone has the
        # SAME `based_on` as the source, so those rows are exactly as valid
        # for the clone as for the source -- copied verbatim, never remapped
        # or nulled (a null `base_placement` is what makes a placement lose
        # its identity, signals.py's pre_delete receivers).
        #
        # `planned_power_feed` names a `DesignPowerFeed` of THIS design, so it
        # is remapped via `feed_map` built above -- no ordering problem, since
        # feeds are already fully cloned by the time placements are created.
        #
        # Ordering conflict with full_clean(): a blade whose chassis is
        # planned in THIS SAME design (case B) carries `target_bay_name` with
        # `parent_placement` as its only "where" reference. With
        # `parent_placement` temporarily null (pass 1), `clean()` reaches the
        # "a bay name requires either a target bay or a parent placement"
        # branch and rejects the row -- this is `clean()`'s own logic, not
        # `clean_fields()`, so `full_clean(exclude=...)` cannot suppress it.
        # There is no partially-valid mode to ask for, so pass 1 deliberately
        # does NOT call `full_clean()` -- it only exists to mint a pk for the
        # map, inside this function's own transaction, and is never visible
        # outside it. Every row's FINAL save -- in pass 2, once
        # `parent_placement` is resolved -- goes through `full_clean()` first,
        # so nothing invalid is ever what a caller of this function can see.
        old_to_new = {}       # old DesignPlacement pk -> new (unsaved-clean) instance
        old_parent_of = {}    # old pk -> old pk of its parent_placement (if any)

        for placement in design.placements.all():
            new_placement = DesignPlacement(
                design=clone,
                kind=placement.kind,
                device=placement.device,
                device_type=placement.device_type,
                device_role=placement.device_role,
                tenant=placement.tenant,
                proposed_name=placement.proposed_name,
                target_rack=placement.target_rack,
                target_position=placement.target_position,
                target_face=placement.target_face,
                target_bay=placement.target_bay,
                target_bay_name=placement.target_bay_name,
                planning_data=placement.planning_data,
                power_config=placement.power_config,
                power_source_device=placement.power_source_device,
                real_power_feed=placement.real_power_feed,
                planned_power_feed=(
                    feed_map[placement.planned_power_feed_id]
                    if placement.planned_power_feed_id else None
                ),
                stale=placement.stale,
                stale_device_name=placement.stale_device_name,
                base_placement=placement.base_placement,
                base_parent_placement=placement.base_parent_placement,
                parent_placement=None,  # remapped in pass 2, see above
            )
            new_placement.save()
            old_to_new[placement.pk] = new_placement
            if placement.parent_placement_id:
                old_parent_of[placement.pk] = placement.parent_placement_id

        for old_pk, new_placement in old_to_new.items():
            old_parent_pk = old_parent_of.get(old_pk)
            if old_parent_pk is not None:
                new_placement.parent_placement = old_to_new[old_parent_pk]
            new_placement.full_clean()
            new_placement.save()

        return clone
