"""
``projection.project_rack(design, rack)`` accepting a ``PlannedRack`` (T1.4a,
PLAN-templates.md D3/D6/D7).

A planned rack does not exist in DCIM yet, so its baseline is entirely free --
every device the projection shows for it comes from the design's OWN
placements (``target_planned_rack``, not ``target_rack``). Before this task
``project_rack`` unconditionally called ``rack.get_rack_units(...)`` and
``rack.devices...`` -- both ``dcim.Rack``-only -- so calling it with a
``PlannedRack`` raised ``AttributeError``. Also covers the new
``rackinfo`` accessors directly, and that the distribution engine no longer
misreports a planned rack as a "failed" engine (it has no devices, which is
an ``empty`` distribution, not a broken one -- see distribution.py's
``_ok_or_empty``).

Same-design ADD/ADD collisions are NOT rejected at the model layer even for a
REAL rack (``DesignPlacement._validate_planned_rack_target``'s docstring says
this outright for planned racks, and empirically -- see the probe run for
this task -- a real rack's model-level ``full_clean()`` doesn't catch it
either, since neither ADD has a real ``dcim.Device`` row yet). So "the same
conflict a real rack would produce" is: both placements render as separate,
unflagged ``add`` slots -- project_rack does not crash and does not invent a
conflict that doesn't exist for a real rack either.
"""

from decimal import Decimal

from dcim.models import DeviceType, Location
from django.test import TestCase

from ..choices import DesignPlacementKindChoices
from ..distribution import generate_distribution_status
from ..models import DesignPlacement, PlannedRack
from ..projection import ProjectedSlotState, project_rack
from ..rackinfo import is_planned, rack_desc_units, rack_devices, rack_starting_unit, rack_units
from .utils import create_dcim_environment, make_design


class ProjectRackPlannedRackTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]  # 1U, half-depth
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Planned R1", location=cls.location, u_height=6,
        )
        cls.design = make_design(title="Greenfield plan", site=cls.site)

    def test_project_rack_does_not_raise(self):
        # The core crash this task fixes: project_rack used to call
        # rack.get_rack_units()/rack.devices unconditionally.
        result = project_rack(self.design, self.planned_rack)
        self.assertIsNotNone(result)

    def test_baseline_is_empty_on_both_faces(self):
        result = project_rack(self.design, self.planned_rack)
        self.assertEqual(result.front, [])
        self.assertEqual(result.rear, [])
        self.assertEqual(result.non_racked, [])

    def test_add_placement_appears_at_right_unit_and_face(self):
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_planned_rack=self.planned_rack,
            target_position=3,
            target_face="front",
            proposed_name="New Device",
        )
        result = project_rack(self.design, self.planned_rack)
        adds = [s for s in result.front if s["state"] == ProjectedSlotState.ADD]
        self.assertEqual(len(adds), 1, result.front)
        self.assertEqual(adds[0]["u_position"], Decimal(3))
        self.assertEqual(adds[0]["face"], "front")
        self.assertEqual(adds[0]["label"], "New Device")
        self.assertEqual(result.rear, [])

    def test_full_depth_add_shadows_both_faces(self):
        fd_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer,
            model="FD Planned", slug="fd-planned", u_height=2, is_full_depth=True,
        )
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=fd_type,
            target_planned_rack=self.planned_rack,
            target_position=1,
            target_face="front",
            proposed_name="FD Device",
        )
        result = project_rack(self.design, self.planned_rack)
        front_adds = [s for s in result.front if s["state"] == ProjectedSlotState.ADD]
        rear_adds = [s for s in result.rear if s["state"] == ProjectedSlotState.ADD]
        self.assertEqual(len(front_adds), 1, result.front)
        self.assertEqual(len(rear_adds), 1, result.rear)
        # The mounted face's copy is the normal colored one; the opposite face
        # is the passive "blocked" shadow -- mirrors _append()'s real-rack rule.
        self.assertFalse(front_adds[0]["opposite_face"])
        self.assertTrue(rear_adds[0]["opposite_face"])

    def test_colliding_adds_render_like_a_real_rack_would(self):
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_planned_rack=self.planned_rack,
            target_position=1, target_face="front", proposed_name="Dev A",
        )
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_planned_rack=self.planned_rack,
            target_position=1, target_face="front", proposed_name="Dev B",
        )
        result = project_rack(self.design, self.planned_rack)
        adds = [s for s in result.front if s["state"] == ProjectedSlotState.ADD]
        # Same behaviour a real rack has today (verified empirically): both
        # ADD placements render, unflagged -- project_rack does not crash and
        # does not invent a conflict a real rack wouldn't also have.
        self.assertEqual(len(adds), 2, result.front)
        labels = {s["label"] for s in adds}
        self.assertEqual(labels, {"Dev A", "Dev B"})
        for slot in adds:
            self.assertFalse(slot["conflict"], slot)


class RackInfoAccessorTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.real_rack = env["racks"][0]
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Planned R1", location=cls.location, u_height=4,
        )

    def test_is_planned(self):
        self.assertTrue(is_planned(self.planned_rack))
        self.assertFalse(is_planned(self.real_rack))

    def test_rack_devices_planned_is_empty_queryset_supporting_chaining(self):
        qs = rack_devices(self.planned_rack)
        self.assertEqual(list(qs), [])
        # Call sites chain .filter()/.exclude()/.select_related() onto this --
        # a plain [] would blow up there, an (empty) queryset does not.
        self.assertEqual(list(qs.filter(name="anything")), [])
        self.assertEqual(list(qs.exclude(name="anything")), [])
        self.assertEqual(list(qs.select_related("device_type")), [])

    def test_rack_devices_real_rack_matches_existing_devices(self):
        qs = rack_devices(self.real_rack)
        self.assertEqual(qs.count(), self.real_rack.devices.count())

    def test_rack_desc_units_planned_is_false(self):
        self.assertFalse(rack_desc_units(self.planned_rack))

    def test_rack_starting_unit_planned_is_one(self):
        self.assertEqual(rack_starting_unit(self.planned_rack), 1)

    def test_rack_units_planned_has_u_height_entries_all_free(self):
        units = rack_units(self.planned_rack, face="front", expand_devices=False)
        # Core's own elevation grid is half-U granularity (Rack.units), so an
        # N-U rack -- real or planned -- always has 2N entries here, not N;
        # verified against core directly for a same-height real rack below.
        self.assertEqual(len(units), 2 * self.planned_rack.u_height)
        for unit in units:
            self.assertIsNone(unit["device"])
            self.assertFalse(unit["occupied"])
            self.assertEqual(unit["face"], "front")

    def test_rack_units_real_rack_delegates_to_core(self):
        units = rack_units(self.real_rack, face="front", expand_devices=False)
        self.assertEqual(units, self.real_rack.get_rack_units(face="front", expand_devices=False))


class DistributionPlannedRackTestCase(TestCase):
    """A planned rack has no PDU devices -- that must read as an ``empty``
    distribution ("no PDU device"), not a ``failed`` engine. Before this task
    ``rack.devices.all()`` crashed inside ``build_native``'s bare
    ``except Exception``, so EVERY planned rack silently reported
    ``state: "failed"`` regardless of mode -- indistinguishable from a
    genuinely broken engine (the project's no-silent-engine-failures rule)."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Planned R1", location=cls.location, u_height=6,
        )
        cls.design = make_design(title="Greenfield plan", site=cls.site)

    def test_distribution_status_not_failed_for_planned_rack(self):
        result = project_rack(self.design, self.planned_rack)
        _distribution, status = generate_distribution_status(result, mode="builtin")
        self.assertNotEqual(status["state"], "failed", status)
        self.assertEqual(status["state"], "empty")
