"""
Signal receivers.

Two receivers exist to keep a plan from silently losing rows when the ground it
stands on moves: deleting a ``dcim.Device`` nulls the ``device`` FK of every
``DesignPlacement`` that referenced it, and deleting a ``DesignPlacement`` that
a downstream design referenced -- as its identity via ``base_placement``, or as
the chassis it plans a blade into via ``base_parent_placement`` (G2,
PLAN-design-chains.md) -- nulls those FKs too (all SET_NULL, see models.py). Both
receivers stamp the affected rows as stale FIRST so the design can report the
loss instead of holding a reference-less row that says nothing.

Ordering is load-bearing and guaranteed by Django's deletion collector
(``django/db/models/deletion.py``, ``Collector.delete``): every ``pre_delete``
signal is sent BEFORE the ``field_updates`` pass that applies SET_NULL. So the
FK is still readable here, and because that pass writes only the FK column
being nulled, the flag written below survives it.
"""

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .choices import DesignPlacementKindChoices
from .models import DesignPlacement


@receiver(pre_delete, sender="dcim.Device")
def flag_placements_of_deleted_device(instance, **kwargs):
    """Mark every placement referencing a device about to be deleted as stale."""
    placements = DesignPlacement.objects.filter(
        device_id=instance.pk,
        kind__in=(
            DesignPlacementKindChoices.KIND_MOVE,
            DesignPlacementKindChoices.KIND_REMOVE,
        ),
    )
    # The name is captured now because after the delete there is nothing left to
    # read it from, and "some device is gone" is not an actionable report.
    name = (instance.name or str(instance) or "")[:64]
    for placement in placements:
        # snapshot() before mutating so the change lands in the object's
        # changelog: a planner looking at the design's history sees WHEN and WHY
        # the placement went inert, which is the whole point of keeping the row.
        placement.snapshot()
        placement.stale = True
        placement.stale_device_name = name
        # No full_clean(): the row is intentionally being put into the one state
        # clean() tolerates only via the stale branch, and the device FK is
        # nulled by the collector immediately after this.
        placement.save()


@receiver(pre_delete, sender=DesignPlacement)
def flag_placements_of_deleted_base_placement(instance, **kwargs):
    """Mark every downstream placement referencing a deleted ``base_placement``
    as stale (G2, PLAN-design-chains.md). Mirrors
    ``flag_placements_of_deleted_device`` above for the same reason: an
    ancestor design's planned 'add' can be cancelled or deleted, and a
    downstream move/remove that pointed at it must not silently disappear or
    silently keep a dangling reference -- it must survive, inert and
    reportable.

    ``sender=DesignPlacement`` means this ALSO fires for the instance being
    deleted itself, but neither relation ever contains the instance itself (a
    placement cannot reference itself as its own base_placement, and both
    ``base_placement`` and ``base_parent_placement`` always cross designs), so
    there is no self-match to worry about.

    TWO relations, because two different things a downstream row can lose when
    an ancestor's planned 'add' is cancelled, and both are SET_NULL for the same
    G2 reason:

    * ``downstream_placements`` -- the row's IDENTITY: a move/remove whose
      ``base_placement`` was that add no longer knows WHAT it acts on. An
      ``add`` never carries one (it IS an identity), so the kind filter holds.
    * ``downstream_bay_children`` -- the row's PARENT: a blade whose
      ``base_parent_placement`` was that add no longer knows WHERE it goes.
      Deliberately NOT kind-filtered: the common case is precisely an ``add``
      of a blade into an ancestor-planned chassis, and that is the row the
      planner most needs to keep and be told about.
    """
    placements = {}
    for placement in instance.downstream_placements.filter(
        kind__in=(
            DesignPlacementKindChoices.KIND_MOVE,
            DesignPlacementKindChoices.KIND_REMOVE,
        ),
    ):
        placements[placement.pk] = placement
    # De-duplicated by pk against the identity relation above: one downstream row
    # can legitimately reference this instance twice over only if it were both
    # its identity and its parent, which the model forbids -- but the dict costs
    # nothing and makes the double-save impossible rather than merely unlikely.
    for placement in instance.downstream_bay_children.all():
        placements.setdefault(placement.pk, placement)
    # The upstream's settled/proposed name is captured now because after the
    # delete there is nothing left to read it from -- "some upstream placement
    # is gone" is not an actionable report; naming WHICH planned device
    # vanished is.
    name = (instance.proposed_name or str(instance) or "")[:64]
    for placement in placements.values():
        # snapshot() before mutating so the change lands in the object's
        # changelog, same as the device receiver above.
        placement.snapshot()
        placement.stale = True
        placement.stale_device_name = name
        # No full_clean(): see the device receiver above -- the row is being put
        # into the one state clean() tolerates only via the stale branch, and
        # the base_placement FK is nulled by the collector immediately after.
        placement.save()
