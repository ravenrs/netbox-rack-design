"""
Model-level tests for NetBox Rack Design.

These cover behaviour that the generic suites do NOT exercise: the custom
``clean()`` validation rules, sequence auto-assignment, and string/URL helpers.
The CRUD/permissions/changelog matrix lives in test_api.py and test_views.py.
"""

from dcim.models import Rack, Site
from django.core.exceptions import ValidationError
from django.test import TestCase

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignGroup, DesignPlacement
from .utils import create_dcim_environment


class DesignGroupTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.parent = DesignGroup.objects.create(name="Parent")
        cls.child = DesignGroup.objects.create(name="Child", parent=cls.parent)

    def test_str(self):
        self.assertEqual(str(self.parent), "Parent")

    def test_unique_name(self):
        with self.assertRaises(ValidationError):
            DesignGroup(name="Parent").full_clean()

    def test_cyclic_parent_rejected(self):
        # A group cannot be its own ancestor.
        self.parent.parent = self.child
        with self.assertRaises(ValidationError):
            self.parent.full_clean()


class DesignTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]

    def test_str_includes_version(self):
        design = Design.objects.create(title="Plan", site=self.site, version=2)
        self.assertEqual(str(design), "Plan (v2)")

    def test_sequence_auto_assigned(self):
        d1 = Design.objects.create(title="A", site=self.site)
        d2 = Design.objects.create(title="B", site=self.site)
        self.assertEqual(d1.sequence, 10)
        self.assertEqual(d2.sequence, 20)

    def test_cannot_be_based_on_self(self):
        design = Design.objects.create(title="A", site=self.site)
        design.based_on = design
        with self.assertRaises(ValidationError):
            design.full_clean()

    def test_single_approved_version_per_plan(self):
        root = Design.objects.create(
            title="Root", site=self.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        sibling = Design(
            title="V2",
            site=self.site,
            version=2,
            root=root,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        with self.assertRaises(ValidationError):
            sibling.full_clean()

    def test_first_approved_design_validates(self):
        # A brand-new, unsaved standalone design created directly as Approved must
        # validate cleanly -- it has no persisted version group to conflict with.
        # Regression: clean() previously raised ValueError on the unsaved root.
        design = Design(
            title="First", site=self.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        design.full_clean()  # must not raise

    def test_racks_can_be_added(self):
        design = Design.objects.create(title="Scoped", site=self.site)
        design.racks.add(*self.racks)
        self.assertEqual(
            set(design.racks.all()),
            set(self.racks),
        )

    def test_same_site_racks_validate(self):
        # Racks in the design's own site pass validation.
        design = Design.objects.create(title="Scoped", site=self.site)
        design.racks.add(*self.racks)
        design.full_clean()  # must not raise

    def test_rack_from_other_site_rejected(self):
        other_site = Site.objects.create(name="Other Site", slug="other-site")
        foreign_rack = Rack.objects.create(name="Foreign Rack", site=other_site)
        design = Design.objects.create(title="Scoped", site=self.site)
        design.racks.add(foreign_rack)
        with self.assertRaises(ValidationError) as ctx:
            design.full_clean()
        self.assertIn("racks", ctx.exception.message_dict)

    def test_seed_logic_pre_seeds_from_placements(self):
        # Mirrors the 0005 data migration's seed query: a design with placements
        # targeting in-site racks should end up scoping exactly those racks.
        design = Design.objects.create(title="Seed me", site=self.site)
        DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[0],
            target_position=10,
        )
        rack_ids = list(
            Rack.objects.filter(
                pk__in=DesignPlacement.objects.filter(
                    design=design, target_rack__isnull=False
                )
                .values_list("target_rack_id", flat=True)
                .distinct(),
                site_id=design.site_id,
            ).values_list("pk", flat=True)
        )
        design.racks.add(*rack_ids)
        self.assertEqual(set(design.racks.all()), {self.racks[0]})


class DesignPlacementTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]
        cls.design = Design.objects.create(title="Plan", site=cls.site)

    def test_add_requires_device_type(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_add_rejects_existing_device(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device=self.devices[0],
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_valid_add(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        placement.full_clean()  # should not raise

    def test_move_requires_device(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_valid_move(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[1],
            target_position=10,
        )
        placement.full_clean()  # should not raise

    def test_remove_needs_no_target(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[0],
        )
        placement.full_clean()  # should not raise

    def test_add_rejects_occupied_slot(self):
        # U1 in Rack 1 is occupied by Device 1.
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[0],
            target_position=1,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()
