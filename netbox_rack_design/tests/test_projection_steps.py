"""
``project_rack(..., only_placements=...)`` -- PLAN-execution-steps.md Sec. 4.1.

The execution plan simulates "steps 1..N": the rack as if ONLY those own
placements of the design existed. ``None`` is today's behaviour; an empty
subset is the real NetBox state; a subset filters the design's own rows
(moves/removes, adds and planned blades) without touching ancestor designs.
"""

from dcim.choices import SubdeviceRoleChoices
from dcim.models import (
    Device,
    DeviceBayTemplate,
    DeviceRole,
    DeviceType,
    Manufacturer,
    PowerPort,
    PowerPortTemplate,
    Rack,
    Site,
)
from django.test import TestCase, override_settings

from ..choices import DesignPlacementKindChoices as Kind
from ..models import DesignPlacement
from ..projection import project_rack
from .utils import make_design


def _cfg():
    return {"netbox_rack_design": {
        "power_capacity_default_w": 10000,
        "power_draw_basis": "allocated",
        "power_warn_pct": 80,
        "power_critical_pct": 100,
    }}


def _labels(elev):
    return sorted(s["label"] for s in elev.front if s["label"])


@override_settings(PLUGINS_CONFIG=_cfg())
class ProjectRackOnlyPlacementsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="STP Site", slug="stp-site")
        mfr = Manufacturer.objects.create(name="STP Mfr", slug="stp-mfr")
        cls.rack = Rack.objects.create(name="STP A", site=cls.site, u_height=42)
        cls.rack_b = Rack.objects.create(name="STP B", site=cls.site, u_height=42)
        cls.role = DeviceRole.objects.create(name="STP Role", slug="stp-role")

        cls.dt_srv = DeviceType.objects.create(
            manufacturer=mfr, model="STP-Srv", slug="stp-srv",
            u_height=1, is_full_depth=False)
        PowerPortTemplate.objects.create(
            device_type=cls.dt_srv, name="PSU1", allocated_draw=200, maximum_draw=250)

        cls.chassis_type = DeviceType.objects.create(
            manufacturer=mfr, model="STP-Chassis", slug="stp-chassis", u_height=2,
            is_full_depth=False, subdevice_role=SubdeviceRoleChoices.ROLE_PARENT)
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="b1")
        cls.blade_type = DeviceType.objects.create(
            manufacturer=mfr, model="STP-Blade", slug="stp-blade", u_height=0,
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD)

        # Real devices: one to remove (300 W), one to move to rack B (200 W).
        cls.old = Device.objects.create(
            name="stp-old", device_type=cls.dt_srv, site=cls.site, rack=cls.rack,
            position=1, face="front", status="active", role=cls.role)
        PowerPort.objects.update_or_create(
            device=cls.old, name="PSU1",
            defaults={"allocated_draw": 300, "maximum_draw": 300})
        cls.mover = Device.objects.create(
            name="stp-mover", device_type=cls.dt_srv, site=cls.site, rack=cls.rack,
            position=3, face="front", status="active", role=cls.role)

        cls.design = make_design(title="STP plan", site=cls.site)
        cls.remove = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_REMOVE, device=cls.old)
        cls.add = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_ADD, device_type=cls.dt_srv,
            target_rack=cls.rack, target_position=10, target_face="front",
            proposed_name="stp-new")
        cls.move = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_MOVE, device=cls.mover,
            target_rack=cls.rack_b, target_position=5, target_face="front")

    def _project(self, only=None, rack=None):
        return project_rack(self.design, rack or self.rack, only_placements=only)

    def _tile(self, elev, label):
        return [s for s in elev.front if s["label"] == label]

    def test_none_equals_unfiltered_result(self):
        plain = project_rack(self.design, self.rack)
        none = self._project(None)
        self.assertEqual(none.power["draw_w"], plain.power["draw_w"])
        self.assertEqual(
            [(s["label"], s["u_position"], s["state"]) for s in none.front],
            [(s["label"], s["u_position"], s["state"]) for s in plain.front])
        # all ids == everything the design owns
        every = self._project({self.remove.pk, self.add.pk, self.move.pk})
        self.assertEqual(every.power["draw_w"], plain.power["draw_w"])
        self.assertEqual(_labels(every), _labels(plain))

    def test_empty_subset_is_real_netbox_state(self):
        elev = self._project(set())
        old = self._tile(elev, "stp-old")
        self.assertEqual(len(old), 1)
        self.assertNotEqual(old[0]["state"], "remove")
        self.assertEqual(self._tile(elev, "stp-new"), [])
        # real state: old 300 W + mover 200 W
        self.assertEqual(elev.power["draw_w"], 500.0)

    def test_subset_with_only_the_remove(self):
        elev = self._project({self.remove.pk})
        # The removed device is drawn as a removal and stops counting.
        self.assertEqual(self._tile(elev, "stp-new"), [])
        self.assertEqual(elev.power["draw_w"], 200.0)

    def test_subset_with_only_the_add(self):
        elev = self._project({self.add.pk})
        self.assertEqual(len(self._tile(elev, "stp-new")), 1)
        self.assertEqual(len(self._tile(elev, "stp-old")), 1)
        self.assertEqual(elev.power["draw_w"], 300.0 + 200.0 + 200.0)

    def test_subset_remove_and_add(self):
        elev = self._project({self.remove.pk, self.add.pk})
        self.assertEqual(len(self._tile(elev, "stp-new")), 1)
        self.assertEqual(elev.power["draw_w"], 200.0 + 200.0)

    def test_move_outside_subset_keeps_device_at_real_position(self):
        elev = self._project({self.add.pk})
        mover = self._tile(elev, "stp-mover")
        self.assertEqual(len(mover), 1)
        self.assertEqual(mover[0]["u_position"], 3)
        self.assertNotEqual(mover[0]["state"], "move_out_ghost")
        # and rack B receives nothing
        elev_b = self._project({self.add.pk}, rack=self.rack_b)
        self.assertEqual(self._tile(elev_b, "stp-mover"), [])

    def test_move_inside_subset_leaves_the_source_rack(self):
        elev_b = self._project({self.move.pk}, rack=self.rack_b)
        self.assertEqual(len(self._tile(elev_b, "stp-mover")), 1)
        full_b = self._project(None, rack=self.rack_b)
        self.assertEqual(len(self._tile(full_b, "stp-mover")), 1)

    def _chassis_with_blade(self):
        chassis = DesignPlacement.objects.create(
            design=self.design, kind=Kind.KIND_ADD, device_type=self.chassis_type,
            target_rack=self.rack, target_position=20, target_face="front",
            proposed_name="stp-chassis")
        blade = DesignPlacement.objects.create(
            design=self.design, kind=Kind.KIND_ADD, device_type=self.blade_type,
            target_rack=self.rack, parent_placement=chassis,
            target_bay_name="b1", proposed_name="stp-blade")
        return chassis, blade

    def test_chassis_in_subset_shows_the_blade(self):
        chassis, blade = self._chassis_with_blade()
        elev = self._project({chassis.pk, blade.pk})
        slot = self._tile(elev, "stp-chassis")[0]
        bay = next(b for b in slot["bays"] if b["name"] == "b1")
        self.assertEqual(bay["label"], "stp-blade")
        self.assertTrue(bay["occupied"])

    def test_chassis_not_in_subset_shows_neither(self):
        chassis, blade = self._chassis_with_blade()
        # the blade alone is a blade without its chassis: silently dropped
        elev = self._project({blade.pk})
        self.assertEqual(self._tile(elev, "stp-chassis"), [])
        self.assertEqual(self._tile(elev, "stp-blade"), [])
        elev = self._project(set())
        self.assertEqual(self._tile(elev, "stp-chassis"), [])
        self.assertEqual(self._tile(elev, "stp-blade"), [])

    def test_chassis_in_subset_without_blade_shows_empty_bay(self):
        chassis, blade = self._chassis_with_blade()
        elev = self._project({chassis.pk})
        slot = self._tile(elev, "stp-chassis")[0]
        bay = next(b for b in slot["bays"] if b["name"] == "b1")
        self.assertFalse(bay["occupied"])
