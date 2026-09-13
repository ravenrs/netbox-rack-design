"""
Model-level tests for letting placements and designs reference a
``PlannedRack`` (PLAN-templates.md T1.2/T1.3).

Covers:

- ``DesignPlacement.target_planned_rack`` alongside the existing
  ``target_rack``, and the "not both" database constraint (D26) -- a plain
  "exactly one" constraint would reject every 'remove', which sets neither.
- ``clean()``'s kind rules for the new field (an 'add'/'move' needs exactly
  one of target_rack/target_planned_rack; a 'remove' needs neither).
- ``Design.planned_racks`` and its site-scope validation, mirroring
  ``Design.racks``.
- ``resolve_rack`` behaving correctly for a placement of each kind, real and
  planned, including a planned rack that has since been realized.
"""

from dcim.models import Location, Rack, Site
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from ..choices import DesignPlacementKindChoices
from ..models import Design, DesignPlacement, PlannedRack, resolve_rack
from .utils import create_dcim_environment


class DesignPlacementTargetPlannedRackTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]
        cls.design = Design.objects.create(title="Plan", site=cls.site)
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(name="Planned R1", location=cls.location)

    def test_both_targets_set_violates_check_constraint(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_planned_rack=self.planned_rack,
            target_position=10,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # Bypass clean()/full_clean() -- this proves the DATABASE-level
                # guard on its own, independent of the Python-level validation.
                placement.save()

    def test_only_target_planned_rack_saves_fine(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_planned_rack=self.planned_rack,
            target_position=10,
        )
        placement.full_clean()
        placement.save()

    def test_only_target_rack_saves_fine(self):
        # Regression: the new constraint must not break what works today.
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        placement.full_clean()
        placement.save()

    def test_remove_with_neither_target_saves_fine(self):
        # The case a naive "exactly one" constraint would break.
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[0],
        )
        placement.full_clean()
        placement.save()

    def test_clean_rejects_add_with_neither_target(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_position=10,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_clean_rejects_add_with_both_targets(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_planned_rack=self.planned_rack,
            target_position=10,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_clean_accepts_move_with_only_target_planned_rack(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_planned_rack=self.planned_rack,
            target_position=10,
        )
        placement.full_clean()  # must not raise

    def test_clean_rejects_planned_rack_in_other_site(self):
        other_site = Site.objects.create(name="Other Site", slug="other-site")
        other_location = Location.objects.create(
            name="Other Location", slug="other-location", site=other_site
        )
        foreign_planned = PlannedRack.objects.create(name="Foreign", location=other_location)
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_planned_rack=foreign_planned,
            target_position=10,
        )
        with self.assertRaises(ValidationError) as ctx:
            placement.full_clean()
        self.assertIn("target_planned_rack", ctx.exception.message_dict)


class DesignPlannedRacksTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(name="Planned R1", location=cls.location)

    def test_planned_rack_in_same_site_validates(self):
        design = Design.objects.create(title="Scoped", site=self.site)
        design.planned_racks.add(self.planned_rack)
        design.full_clean()  # must not raise

    def test_planned_rack_in_other_site_rejected(self):
        other_site = Site.objects.create(name="Other Site", slug="other-site")
        other_location = Location.objects.create(
            name="Other Location", slug="other-location", site=other_site
        )
        foreign_planned = PlannedRack.objects.create(name="Foreign", location=other_location)
        design = Design.objects.create(title="Scoped", site=self.site)
        design.planned_racks.add(foreign_planned)
        with self.assertRaises(ValidationError) as ctx:
            design.full_clean()
        self.assertIn("planned_racks", ctx.exception.message_dict)


class ResolveRackForPlacementTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]
        cls.design = Design.objects.create(title="Plan", site=cls.site)
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )
        cls.unrealized_planned = PlannedRack.objects.create(
            name="Planned R1", location=cls.location
        )
        cls.real_rack_for_realization = Rack.objects.create(
            name="Real R2", site=cls.site, location=cls.location
        )
        cls.realized_planned = PlannedRack.objects.create(
            name="Planned R2", location=cls.location,
            realized_rack=cls.real_rack_for_realization,
        )

    def test_resolve_rack_for_real_target_placement(self):
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        self.assertEqual(
            resolve_rack(placement.target_rack, placement.target_planned_rack),
            self.racks[1],
        )

    def test_resolve_rack_for_unrealized_planned_target_placement(self):
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_planned_rack=self.unrealized_planned,
            target_position=10,
        )
        self.assertIsNone(
            resolve_rack(placement.target_rack, placement.target_planned_rack)
        )

    def test_resolve_rack_for_realized_planned_target_placement(self):
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_planned_rack=self.realized_planned,
            target_position=10,
        )
        self.assertEqual(
            resolve_rack(placement.target_rack, placement.target_planned_rack),
            self.real_rack_for_realization,
        )

    def test_resolve_rack_for_remove_placement(self):
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[0],
        )
        self.assertIsNone(
            resolve_rack(placement.target_rack, placement.target_planned_rack)
        )
