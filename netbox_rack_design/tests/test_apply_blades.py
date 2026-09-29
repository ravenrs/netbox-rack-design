"""Apply installs planned blades into device bays.

The three routes a blade placement takes into a bay (models.DesignPlacement):
a real chassis's bay, a bay of a chassis planned in the same design, and a bay
of a chassis an ancestor design planned and already applied.
"""

from dcim.models import Device, DeviceBay, DeviceBayTemplate, DeviceType
from users.models import User

from .. import apply
from ..choices import DesignPlacementKindChoices
from ..models import DesignApply, DesignPlacement
from .test_apply import ApplyTestCase


class BladeApplyTestCase(ApplyTestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.chassis_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="Chassis-2U", slug="chassis-2u",
            u_height=2, subdevice_role="parent",
        )
        for name in ("bay1", "bay2"):
            DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name=name)
        cls.blade_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="Blade", slug="blade",
            u_height=0, subdevice_role="child",
        )

    def _real_chassis(self, name="real-chassis", position=30):
        chassis = Device.objects.create(
            name=name, device_type=self.chassis_type, role=self.device_role,
            site=self.site, rack=self.racks[0], position=position, face="front",
        )
        return chassis, {b.name: b for b in chassis.devicebays.all()}

    def _chassis_add(self, design, position, name):
        return self._add(design, position, name=name, device_type=self.chassis_type)

    def _blade(self, design, name, **target):
        return DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.blade_type, proposed_name=name,
            device_role=self.device_role, **target,
        )

    def _blade_device(self, name):
        return Device.objects.select_related("parent_bay__device").get(name=name)

    # --- the three routes --------------------------------------------------

    def test_a_blade_goes_into_a_real_chassis_bay(self):
        chassis, bays = self._real_chassis()
        design = self._design()
        self._blade(design, "blade-a", target_bay=bays["bay2"], target_bay_name="bay2")
        self._approve(design)

        result = apply.run(design, self.superuser)

        self.assertTrue(result.ok, result.problems)
        blade = self._blade_device("blade-a")
        self.assertEqual(blade.parent_bay.pk, bays["bay2"].pk)
        self.assertEqual(blade.rack_id, chassis.rack_id)
        self.assertIsNone(blade.position)
        self.assertEqual(blade.status, "planned")

    def test_a_blade_goes_into_a_chassis_planned_in_the_same_design(self):
        design = self._design()
        chassis = self._chassis_add(design, 30, "planned-chassis")
        self._blade(design, "blade-b1", parent_placement=chassis, target_bay_name="bay1")
        self._blade(design, "blade-b2", parent_placement=chassis, target_bay_name="bay2")
        self._approve(design)

        result = apply.run(design, self.superuser)

        self.assertTrue(result.ok, result.problems)
        new_chassis = Device.objects.get(name="planned-chassis")
        self.assertEqual(
            {b.name: b.installed_device.name for b in new_chassis.devicebays.all()},
            {"bay1": "blade-b1", "bay2": "blade-b2"},
        )

    def test_a_blade_goes_into_a_chassis_an_ancestor_applied(self):
        parent = self._design("Chassis plan")
        chassis = self._chassis_add(parent, 30, "ancestor-chassis")
        self._approve(parent)
        self.assertTrue(apply.run(parent, self.superuser).ok)

        child = self._design("Blade plan", based_on=parent)
        self._blade(child, "blade-c", base_parent_placement=chassis, target_bay_name="bay1")
        self._approve(child)

        result = apply.run(child, self.superuser)

        self.assertTrue(result.ok, result.problems)
        self.assertEqual(self._blade_device("blade-c").parent_bay.device.name, "ancestor-chassis")

    def test_the_confirmation_names_the_chassis_and_bay(self):
        design = self._design()
        chassis = self._chassis_add(design, 30, "planned-chassis")
        self._blade(design, "blade-w", parent_placement=chassis, target_bay_name="bay2")
        self._approve(design)

        result = apply.plan(design, self.superuser)

        entry = next(e for e in result.created if e.name == "blade-w")
        self.assertTrue(entry.blade)
        self.assertEqual(entry.where, "planned-chassis · bay2")

    # --- refusals ------------------------------------------------------------

    def test_an_occupied_bay_is_a_problem(self):
        chassis, bays = self._real_chassis()
        Device.objects.create(name="old-blade", device_type=self.blade_type,
                              role=self.device_role, site=self.site, rack=self.racks[0])
        bays["bay1"].installed_device = Device.objects.get(name="old-blade")
        bays["bay1"].save()
        # Built with objects.create: the model's own clean() would already
        # refuse the occupied bay -- this is the state a later DCIM edit leaves.
        design = self._design()
        self._blade(design, "blade-o", target_bay=bays["bay1"], target_bay_name="bay1")
        self._approve(design)

        result = apply.plan(design, self.superuser)

        self.assertFalse(result.ok)
        self.assertTrue(any("occupied by old-blade" in p for p in result.problems), result.problems)

    def test_a_chassis_the_ancestor_has_not_applied_is_a_problem(self):
        parent = self._design("Chassis plan")
        chassis = self._chassis_add(parent, 30, "unapplied-chassis")
        self._approve(parent)
        child = self._design("Blade plan", based_on=parent)
        self._blade(child, "blade-u", base_parent_placement=chassis, target_bay_name="bay1")
        self._approve(child)

        result = apply.plan(child, self.superuser)

        self.assertFalse(result.ok)
        self.assertTrue(any("has not applied yet" in p for p in result.problems), result.problems)

    def test_installing_blades_needs_the_device_bay_permission(self):
        from core.models import ObjectType
        from users.models import ObjectPermission

        chassis, bays = self._real_chassis()
        design = self._design()
        self._blade(design, "blade-p", target_bay=bays["bay1"], target_bay_name="bay1")
        self._approve(design)
        user = User.objects.create_user(username="devices-only")
        perm = ObjectPermission.objects.create(name="devices", actions=["view", "add", "change"])
        perm.users.add(user)
        perm.object_types.add(ObjectType.objects.get_for_model(Device))

        result = apply.plan(design, user)

        self.assertTrue(any("change device bays" in p for p in result.problems), result.problems)

    # --- idempotency and drift -----------------------------------------------

    def test_a_second_apply_finds_its_own_blade_in_the_bay(self):
        design = self._design()
        chassis = self._chassis_add(design, 30, "planned-chassis")
        self._blade(design, "blade-i", parent_placement=chassis, target_bay_name="bay1")
        self._approve(design)
        self.assertTrue(apply.run(design, self.superuser).ok)

        second = apply.run(design, self.superuser)

        self.assertTrue(second.ok, second.problems)
        self.assertEqual((second.created, second.updated), ([], []))

    def test_a_blade_whose_planned_bay_changed_moves_bays(self):
        chassis, bays = self._real_chassis()
        design = self._design()
        blade = self._blade(design, "blade-m", target_bay=bays["bay1"], target_bay_name="bay1")
        self._approve(design)
        self.assertTrue(apply.run(design, self.superuser).ok)

        DesignPlacement.objects.filter(pk=blade.pk).update(
            target_bay=bays["bay2"], target_bay_name="bay2")
        result = apply.run(design, self.superuser)

        self.assertTrue(result.ok, result.problems)
        self.assertEqual(self._blade_device("blade-m").parent_bay.name, "bay2")
        self.assertIsNone(DeviceBay.objects.get(pk=bays["bay1"].pk).installed_device)

    def test_a_blade_dropped_from_the_design_is_deleted_on_the_next_apply(self):
        design = self._design()
        chassis = self._chassis_add(design, 30, "planned-chassis")
        blade = self._blade(design, "blade-d", parent_placement=chassis, target_bay_name="bay1")
        self._approve(design)
        self.assertTrue(apply.run(design, self.superuser).ok)

        DesignPlacement.objects.filter(pk=blade.pk).delete()
        result = apply.run(design, self.superuser)

        self.assertTrue(result.ok, result.problems)
        self.assertFalse(Device.objects.filter(name="blade-d").exists())
        self.assertFalse(DesignApply.objects.filter(device_name="blade-d").exists())
