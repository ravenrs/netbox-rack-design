"""
Tests for ``versioning.new_version`` (PLAN-design-versions.md §2/§4): cloning a
design into a new, draft version of the same plan.

Model-layer only -- no view, no API action (that is phase 2). See
``test_models.py`` for the conventions these fixtures follow.
"""

from unittest import mock

from core.models import ObjectType
from dcim.choices import SubdeviceRoleChoices
from dcim.models import DeviceBayTemplate, DeviceType
from django.core.exceptions import ValidationError
from django.test import TestCase
from extras.models import CustomField, Tag

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignApply, DesignPlacement, DesignPowerFeed, DesignRackPower
from ..versioning import new_version
from .utils import create_dcim_environment


class NewVersionBasicsTestCase(TestCase):
    """External-FK fidelity, feed/rack-power cloning, and DesignApply exclusion."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.tenant = env["tenant"]
        cls.devices = env["devices"]

        cls.tag = Tag.objects.create(name="Tag A", slug="tag-a")

        # Registered CustomFields for "project" (Design/DesignPlacement) and
        # "leg" (DesignPowerFeed): new_version() calls full_clean() on every
        # cloned row, and full_clean() rejects an unregistered cf key.
        project_cf = CustomField.objects.create(name="project", type="text", required=False)
        project_cf.object_types.set([
            ObjectType.objects.get_for_model(Design),
            ObjectType.objects.get_for_model(DesignPlacement),
        ])
        leg_cf = CustomField.objects.create(name="leg", type="text", required=False)
        leg_cf.object_types.set([ObjectType.objects.get_for_model(DesignPowerFeed)])

        cls.design = Design.objects.create(
            title="Plan", site=cls.site, summary="s", link="http://example.com/",
            description="d", comments="c",
            custom_field_data={"project": "IDS-1000"},
        )
        cls.design.racks.add(*cls.racks)
        cls.design.tags.set([cls.tag])

        cls.feed = DesignPowerFeed.objects.create(
            design=cls.design, rack=cls.racks[0], name="Feed A", voltage=400,
        )
        cls.rack_power = DesignRackPower.objects.create(
            design=cls.design, rack=cls.racks[0], power_config={"custom_fields": {"x": 1}},
        )

        cls.add_placement = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            tenant=cls.tenant,
            target_rack=cls.racks[1],
            target_position=5,
            target_face="front",
            proposed_name="new-node",
            planned_power_feed=cls.feed,
            custom_field_data={"project": "IDS-1000"},
        )
        cls.add_placement.tags.set([cls.tag])
        cls.move_placement = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.devices[0],
            target_rack=cls.racks[1],
            target_position=6,
        )

    def test_clone_has_same_placement_count_and_matching_external_fks(self):
        clone = new_version(self.design)
        self.assertEqual(clone.placements.count(), self.design.placements.count())

        clone_add = clone.placements.get(kind=DesignPlacementKindChoices.KIND_ADD)
        self.assertEqual(clone_add.device_type_id, self.add_placement.device_type_id)
        self.assertEqual(clone_add.device_role_id, self.add_placement.device_role_id)
        self.assertEqual(clone_add.tenant_id, self.add_placement.tenant_id)
        self.assertEqual(clone_add.target_rack_id, self.add_placement.target_rack_id)
        self.assertEqual(clone_add.target_position, self.add_placement.target_position)
        self.assertEqual(clone_add.proposed_name, self.add_placement.proposed_name)

        clone_move = clone.placements.get(kind=DesignPlacementKindChoices.KIND_MOVE)
        self.assertEqual(clone_move.device_id, self.move_placement.device_id)

    def test_planned_power_feed_points_at_the_clones_feed(self):
        clone = new_version(self.design)
        clone_add = clone.placements.get(kind=DesignPlacementKindChoices.KIND_ADD)
        self.assertIsNotNone(clone_add.planned_power_feed_id)
        self.assertNotEqual(clone_add.planned_power_feed_id, self.feed.pk)
        self.assertEqual(clone_add.planned_power_feed.design_id, clone.pk)
        self.assertEqual(clone_add.planned_power_feed.name, self.feed.name)

    def test_feeds_and_rack_power_are_cloned(self):
        clone = new_version(self.design)
        self.assertEqual(clone.planned_feeds.count(), 1)
        cloned_feed = clone.planned_feeds.get()
        self.assertNotEqual(cloned_feed.pk, self.feed.pk)
        self.assertEqual(cloned_feed.voltage, self.feed.voltage)

        self.assertEqual(clone.rack_power.count(), 1)
        cloned_rp = clone.rack_power.get()
        self.assertNotEqual(cloned_rp.pk, self.rack_power.pk)
        self.assertEqual(cloned_rp.power_config, self.rack_power.power_config)

    def test_design_custom_field_data_and_tags_are_copied(self):
        clone = new_version(self.design)
        self.assertEqual(clone.custom_field_data, {"project": "IDS-1000"})
        self.assertEqual(set(clone.tags.all()), {self.tag})

    def test_placement_custom_field_data_and_tags_are_copied(self):
        clone = new_version(self.design)
        clone_add = clone.placements.get(kind=DesignPlacementKindChoices.KIND_ADD)
        self.assertEqual(clone_add.custom_field_data, {"project": "IDS-1000"})
        self.assertEqual(set(clone_add.tags.all()), {self.tag})
        # A row with nothing set copies an empty dict, not a KeyError/None.
        clone_move = clone.placements.get(kind=DesignPlacementKindChoices.KIND_MOVE)
        self.assertEqual(clone_move.custom_field_data, {})
        self.assertEqual(set(clone_move.tags.all()), set())

    def test_feed_custom_field_data_and_tags_are_copied(self):
        self.feed.custom_field_data = {"leg": "A"}
        self.feed.tags.set([self.tag])
        self.feed.save()

        clone = new_version(self.design)
        cloned_feed = clone.planned_feeds.get()
        self.assertEqual(cloned_feed.custom_field_data, {"leg": "A"})
        self.assertEqual(set(cloned_feed.tags.all()), {self.tag})

    def test_rack_power_has_no_custom_fields_or_tags_to_copy(self):
        # DesignRackPower is a plain models.Model (not a NetBoxModel), so it
        # carries neither custom_field_data nor tags -- confirmed here rather
        # than assumed.
        self.assertFalse(hasattr(DesignRackPower, "custom_field_data"))
        self.assertFalse(hasattr(DesignRackPower, "tags"))

    def test_design_apply_rows_are_not_cloned(self):
        DesignApply.objects.create(
            design=self.design, placement=self.move_placement, device=self.devices[0],
        )
        clone = new_version(self.design)
        self.assertEqual(DesignApply.objects.filter(design=clone).count(), 0)

    def test_racks_and_depends_on_are_copied(self):
        other_design = Design.objects.create(title="Dep", site=self.site)
        self.design.depends_on.add(other_design)

        clone = new_version(self.design)
        self.assertEqual(set(clone.racks.all()), set(self.design.racks.all()))
        self.assertEqual(set(clone.depends_on.all()), {other_design})

    def test_clone_is_draft_even_when_source_is_approved(self):
        self.design.status = DesignStatusChoices.STATUS_APPROVED
        self.design.save()
        clone = new_version(self.design)
        self.assertEqual(clone.status, DesignStatusChoices.STATUS_DRAFT)

    def test_title_defaults_to_source_title_unchanged(self):
        clone = new_version(self.design)
        self.assertEqual(clone.title, self.design.title)

    def test_title_override_used_verbatim(self):
        clone = new_version(self.design, title="Custom Title")
        self.assertEqual(clone.title, "Custom Title")

    def test_stale_placement_is_copied_not_dropped(self):
        move = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[1],
            target_rack=self.racks[1],
            target_position=7,
        )
        stale_name = self.devices[1].name
        self.devices[1].delete()
        move.refresh_from_db()
        self.assertTrue(move.stale)

        clone = new_version(self.design)
        clone_move = clone.placements.get(stale=True)
        self.assertTrue(clone_move.stale)
        self.assertEqual(clone_move.stale_device_name, stale_name)
        self.assertIsNone(clone_move.device_id)


class NewVersionParentPlacementTestCase(TestCase):
    """A blade's ``parent_placement`` must be remapped to the CLONE's own copy
    of the chassis row -- not left pointing at the source's."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        manufacturer = env["device_type"].manufacturer

        cls.chassis_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Chassis-T", slug="chassis-t",
            u_height=2, subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="bay-a")
        cls.blade_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Blade-T", slug="blade-t",
            u_height=0, subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )

        cls.design = Design.objects.create(title="Bay plan", site=cls.site)
        cls.chassis_placement = DesignPlacement.objects.create(
            design=cls.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.chassis_type, target_rack=cls.racks[0],
            target_position=20, target_face="front",
        )
        cls.blade_placement = DesignPlacement.objects.create(
            design=cls.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.blade_type, target_rack=cls.racks[0],
            parent_placement=cls.chassis_placement, target_bay_name="bay-a",
        )

    def test_blade_parent_placement_points_at_the_clones_chassis_row(self):
        clone = new_version(self.design)
        clone_blade = clone.placements.get(device_type=self.blade_type)
        clone_chassis = clone.placements.get(device_type=self.chassis_type)

        self.assertIsNotNone(clone_blade.parent_placement_id)
        self.assertEqual(clone_blade.parent_placement_id, clone_chassis.pk)
        self.assertNotEqual(clone_blade.parent_placement_id, self.chassis_placement.pk)
        self.assertEqual(clone_blade.target_bay_name, "bay-a")


class NewVersionAncestorReferencesTestCase(TestCase):
    """``base_placement`` / ``base_parent_placement`` must still point at the
    ANCESTOR's rows after cloning -- the clone shares the source's
    ``based_on``, so those rows are exactly as valid for it."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        manufacturer = cls.device_type.manufacturer

        cls.chassis_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Chassis-T", slug="chassis-t",
            u_height=2, subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="bay-a")
        cls.blade_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Blade-T", slug="blade-t",
            u_height=0, subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )

        cls.parent_design = Design.objects.create(title="Parent", site=cls.site)
        cls.upstream_add = DesignPlacement.objects.create(
            design=cls.parent_design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type, target_rack=cls.racks[0], target_position=5,
            proposed_name="upstream-node",
        )
        cls.upstream_chassis = DesignPlacement.objects.create(
            design=cls.parent_design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.chassis_type, target_rack=cls.racks[0], target_position=10,
        )
        cls.parent_design.status = DesignStatusChoices.STATUS_APPROVED
        cls.parent_design.save()

        cls.child_design = Design.objects.create(
            title="Child", site=cls.site, based_on=cls.parent_design,
        )
        cls.child_move = DesignPlacement.objects.create(
            design=cls.child_design, kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=cls.upstream_add, target_rack=cls.racks[1], target_position=11,
        )
        cls.child_blade = DesignPlacement.objects.create(
            design=cls.child_design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.blade_type, target_rack=cls.racks[0],
            base_parent_placement=cls.upstream_chassis, target_bay_name="bay-a",
        )

    def test_base_placement_still_points_at_the_ancestors_row(self):
        clone = new_version(self.child_design)
        clone_move = clone.placements.get(kind=DesignPlacementKindChoices.KIND_MOVE)
        self.assertEqual(clone_move.base_placement_id, self.upstream_add.pk)
        self.assertEqual(clone_move.base_placement.design_id, self.parent_design.pk)

    def test_base_parent_placement_still_points_at_the_ancestors_row(self):
        clone = new_version(self.child_design)
        clone_blade = clone.placements.get(device_type=self.blade_type)
        self.assertEqual(clone_blade.base_parent_placement_id, self.upstream_chassis.pk)
        self.assertIsNone(clone_blade.parent_placement_id)


class NewVersionNumberingTestCase(TestCase):
    """``version``/``root`` for a v1 source, and for cloning a v2 (the third
    version's root must still be v1, not v2)."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]

    def test_v1_source_clone_is_v2_rooted_at_v1(self):
        v1 = Design.objects.create(title="Root", site=self.site)
        self.assertIsNone(v1.root)

        clone = new_version(v1)
        self.assertEqual(clone.version, 2)
        self.assertEqual(clone.root_id, v1.pk)

    def test_v2_of_v2_is_v3_rooted_at_v1_not_v2(self):
        v1 = Design.objects.create(title="Root", site=self.site)
        v2 = new_version(v1)
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.root_id, v1.pk)

        v3 = new_version(v2)
        self.assertEqual(v3.version, 3)
        self.assertEqual(v3.root_id, v1.pk)
        self.assertNotEqual(v3.root_id, v2.pk)


class NewVersionAtomicityTestCase(TestCase):
    """A failure partway through the clone must leave nothing behind."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]

        cls.design = Design.objects.create(title="Plan", site=cls.site)
        cls.p1 = DesignPlacement.objects.create(
            design=cls.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type, target_rack=cls.racks[0], target_position=1,
        )
        cls.p2 = DesignPlacement.objects.create(
            design=cls.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type, target_rack=cls.racks[0], target_position=2,
        )

    def test_failure_partway_through_leaves_nothing_behind(self):
        design_count = Design.objects.count()
        placement_count = DesignPlacement.objects.count()

        real_full_clean = DesignPlacement.full_clean
        calls = {"n": 0}

        def flaky_full_clean(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ValidationError("forced failure for the atomicity test")
            return real_full_clean(self, *args, **kwargs)

        with mock.patch.object(DesignPlacement, "full_clean", flaky_full_clean):
            with self.assertRaises(ValidationError):
                new_version(self.design)

        self.assertEqual(Design.objects.count(), design_count)
        self.assertEqual(DesignPlacement.objects.count(), placement_count)
