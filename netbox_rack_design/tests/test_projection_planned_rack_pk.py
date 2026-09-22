"""
The same-pk landmine (PLAN-templates.md D28, T1.4b).

``PlannedRack`` and ``dcim.Rack`` keep SEPARATE pk sequences, so pk 74 exists
in both tables and means two different racks. ``projection.py`` compares a
bare ``rack.pk`` against ``dcim.Rack``-typed FK columns (``device.rack_id``,
``DesignPlacement.target_rack_id``, ``DesignApply.device__rack_id``) in
several places. When ``rack`` is a ``PlannedRack`` whose pk happens to equal
some unrelated real rack's pk, those comparisons silently match rows that
belong to the REAL rack, not the planned one -- plausible, wrong output, not
a crash.

This forces the collision on purpose (``PlannedRack.objects.create(pk=...)``)
so the test cannot pass "by luck": without a forced collision a planned
rack's pk would never equal a real rack's pk in practice, and the bug would
never be exercised.

The scenario: a real device sits in ``real_rack`` at U1/front. A design
moves that device INTO a ``PlannedRack`` that shares ``real_rack``'s pk. The
device-side comparison (``device.rack_id == rack.pk`` / ``current_rack_id ==
rack.pk``) must not treat "the device's real rack" and "this planned rack"
as the same thing just because the integers match -- the move-out ghost
belongs to ``real_rack``'s projection, never the planned rack's. The
target-side comparison (``target_rack_id == rack.pk``) has the opposite
symptom: a move INTO a planned rack must resolve through
``target_planned_rack_id``, not the (always-None-for-this-placement)
``target_rack_id`` -- so the move-in tile must actually appear.

A second, unrelated real placement (``real_add``, an 'add' targeting
``real_rack`` directly) and a ``DesignApply`` row on the real device are
also planted, so a planned-rack projection that leaked ANY of the real
rack's world would show them.
"""

from decimal import Decimal

from dcim.models import DeviceType, Location, Manufacturer, Rack, Site
from django.test import TestCase
from utilities.testing import create_test_device

from ..choices import DesignPlacementKindChoices
from ..models import DesignApply, DesignPlacement, PlannedRack
from ..projection import ProjectedSlotState, project_rack
from .utils import make_design


class ProjectRackPkCollisionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Site PK collision", slug="site-pk-collision")
        manufacturer = Manufacturer.objects.create(name="Mfr PK", slug="mfr-pk")
        cls.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="DT PK", slug="dt-pk",
            u_height=1, is_full_depth=False,
        )

        cls.real_rack = Rack.objects.create(name="Real Rack", site=cls.site)
        cls.device = create_test_device(
            "Real Device", site=cls.site, rack=cls.real_rack,
            position=1, face="front",
        )

        cls.design = make_design(title="Design PK collision", site=cls.site)
        cls.other_design = make_design(title="Apply owner", site=cls.site)

        # An UNRELATED real placement in real_rack -- must never surface when
        # projecting the pk-colliding PlannedRack.
        cls.real_add = DesignPlacement.objects.create(
            design=cls.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type, target_rack=cls.real_rack,
            target_position=5, target_face="front", proposed_name="Real Add",
        )
        # An apply marker on the real device -- must never surface either.
        cls.apply_row = DesignApply.objects.create(
            design=cls.other_design, device=cls.device,
        )

        # Force the pk collision: PlannedRack has its OWN pk sequence, so this
        # explicit assignment is the only way pk 74 (say) ever means both
        # "PlannedRack 74" and "Rack 74" -- exactly the scenario D28 warns
        # about, not something that happens by chance.
        cls.location = Location.objects.create(
            name="Loc PK", slug="loc-pk", site=cls.site,
        )
        cls.planned_rack = PlannedRack(
            name="Planned pk collision", location=cls.location, u_height=6,
        )
        cls.planned_rack.pk = cls.real_rack.pk
        cls.planned_rack.save(force_insert=True)
        assert cls.planned_rack.pk == cls.real_rack.pk, (
            "setup did not actually force the pk collision this test needs"
        )

        # The move that exercises BOTH branches: the device's CURRENT rack is
        # real_rack (device-side), the placement's TARGET is the planned rack
        # (target-side, via target_planned_rack -- not target_rack).
        cls.move_placement = DesignPlacement.objects.create(
            design=cls.design, kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.device, target_planned_rack=cls.planned_rack,
            target_position=2, target_face="front",
        )

    def test_planned_rack_projection_has_no_contamination(self):
        result = project_rack(self.design, self.planned_rack)

        # THE LANDMINE: device.rack_id (real_rack.pk) must not be treated as
        # "this planned rack" just because the integers match. Without the
        # fix this renders a MOVE_OUT_GHOST at U1/front -- the device's real,
        # unrelated location in real_rack.
        ghosts = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST]
        self.assertEqual(ghosts, [], f"real rack's device leaked into planned rack: {result.front}")

        # The move-in tile DOES belong here -- this is the actual target.
        # (Also proves the target-side fix: target_rack_id is None for this
        # placement, so a bare `target_rack_id == rack.pk` would never match
        # and the tile would silently fail to render at all.)
        move_ins = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_IN]
        self.assertEqual(len(move_ins), 1, result.front)
        self.assertEqual(move_ins[0]["u_position"], Decimal(2))

        # The unrelated real 'add' (targeting real_rack, not this planned
        # rack) must not appear.
        adds = [s for s in result.front if s["state"] == ProjectedSlotState.ADD]
        self.assertEqual(adds, [], f"unrelated real rack placement leaked: {result.front}")

        # No apply/reservation marker leaked onto anything drawn here.
        for slot in result.front + result.rear + result.non_racked:
            self.assertFalse(slot.get("applied"), slot)
            self.assertIsNone(slot.get("reserved_by_design_id"), slot)

    def test_real_rack_projection_unaffected_by_the_pk_collision(self):
        """The mirror assertion: real_rack's own projection must still show
        everything it should, unaffected by a PlannedRack merely sharing its
        pk (D28's whole point is that the two must never be confused in
        EITHER direction)."""
        result = project_rack(self.design, self.real_rack)

        # The device is being moved OUT of real_rack -- its real slot renders
        # as a ghost here, not as a plain existing device.
        ghosts = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST]
        self.assertEqual(len(ghosts), 1, result.front)
        self.assertEqual(ghosts[0]["u_position"], Decimal(1))

        # The unrelated real 'add' still projects normally in its own rack.
        adds = [s for s in result.front if s["state"] == ProjectedSlotState.ADD]
        self.assertEqual(len(adds), 1, result.front)
        self.assertEqual(adds[0]["u_position"], Decimal(5))
        self.assertEqual(adds[0]["label"], "Real Add")

        # The move-in tile must NOT appear here -- the target is the planned
        # rack, not this one, collision or not.
        move_ins = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_IN]
        self.assertEqual(move_ins, [], result.front)
