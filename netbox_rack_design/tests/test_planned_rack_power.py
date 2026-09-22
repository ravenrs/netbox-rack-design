"""
Model-level tests for letting ``DesignPowerFeed`` and ``DesignRackPower``
reference a ``PlannedRack`` (PLAN-templates.md T1.8b, D25/D26/D27).

Both models normally read/override a real rack's power data; a planned rack
has no real feeds and no ``rack.cf`` at all, so for it the plugin-side row
becomes the ONLY source. Covers, for each model:

- the "exactly one of rack / planned_rack" database-level check constraint,
  both failure directions (both set, neither set) -- a plain "not both"
  check (the shape ``DesignPlacement`` uses, where a 'remove' legitimately
  sets neither) would miss the "neither" case, which does not apply here:
  a feed or a power override always belongs to SOME rack.
- the regression case: only rack, or only planned_rack, saves fine.
- the two partial unique constraints (design, rack, name)/(design,
  planned_rack, name) for DesignPowerFeed, and (design, rack)/(design,
  planned_rack) for DesignRackPower.
- clean()'s same-site validation for a planned rack, mirroring
  ``DesignPlacement._validate_planned_rack_target``.
- __str__ not raising for either kind.
- ``DesignRackPower.effective_custom_fields`` accepting a planned rack.
"""

from dcim.models import Location, Site
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from ..models import DesignPowerFeed, DesignRackPower, PlannedRack
from .utils import create_dcim_environment, make_design


class DesignPowerFeedPlannedRackTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.design = make_design(title="Plan", site=cls.site)
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(name="Planned R1", location=cls.location)

    def test_both_rack_and_planned_rack_violates_check_constraint(self):
        feed = DesignPowerFeed(
            design=self.design, rack=self.racks[0], planned_rack=self.planned_rack,
            name="Feed A",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # Bypass clean()/full_clean() -- this proves the DATABASE-level
                # guard on its own, independent of the Python-level validation.
                feed.save()

    def test_neither_rack_nor_planned_rack_violates_check_constraint(self):
        feed = DesignPowerFeed(design=self.design, name="Feed A")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                feed.save()

    def test_only_planned_rack_saves_fine(self):
        feed = DesignPowerFeed(
            design=self.design, planned_rack=self.planned_rack, name="Feed A",
        )
        feed.full_clean()
        feed.save()

    def test_only_rack_saves_fine(self):
        # Regression: the new constraints must not break what works today.
        feed = DesignPowerFeed(design=self.design, rack=self.racks[0], name="Feed A")
        feed.full_clean()
        feed.save()

    def test_unique_design_planned_rack_name_rejected(self):
        DesignPowerFeed.objects.create(
            design=self.design, planned_rack=self.planned_rack, name="Feed A",
        )
        dupe = DesignPowerFeed(
            design=self.design, planned_rack=self.planned_rack, name="Feed A",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                dupe.save()

    def test_unique_design_rack_name_still_rejected(self):
        # Regression: the existing (design, rack, name) uniqueness must survive
        # becoming a partial constraint.
        DesignPowerFeed.objects.create(
            design=self.design, rack=self.racks[0], name="Feed A",
        )
        dupe = DesignPowerFeed(design=self.design, rack=self.racks[0], name="Feed A")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                dupe.save()

    def test_clean_rejects_planned_rack_in_other_site(self):
        other_site = Site.objects.create(name="Other Site", slug="other-site")
        other_location = Location.objects.create(
            name="Other Location", slug="other-location", site=other_site
        )
        foreign_planned = PlannedRack.objects.create(name="Foreign", location=other_location)
        feed = DesignPowerFeed(
            design=self.design, planned_rack=foreign_planned, name="Feed A",
        )
        with self.assertRaises(ValidationError) as ctx:
            feed.full_clean()
        self.assertIn("planned_rack", ctx.exception.message_dict)

    def test_str_does_not_raise_for_planned_rack(self):
        feed = DesignPowerFeed.objects.create(
            design=self.design, planned_rack=self.planned_rack, name="Feed A",
        )
        self.assertEqual(str(feed), "Feed A")

    def test_str_does_not_raise_for_real_rack(self):
        feed = DesignPowerFeed.objects.create(
            design=self.design, rack=self.racks[0], name="Feed B",
        )
        self.assertEqual(str(feed), "Feed B")


class DesignRackPowerPlannedRackTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.design = make_design(title="Plan", site=cls.site)
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(name="Planned R1", location=cls.location)

    def test_both_rack_and_planned_rack_violates_check_constraint(self):
        row = DesignRackPower(
            design=self.design, rack=self.racks[0], planned_rack=self.planned_rack,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                row.save()

    def test_neither_rack_nor_planned_rack_violates_check_constraint(self):
        row = DesignRackPower(design=self.design)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                row.save()

    def test_only_planned_rack_saves_fine(self):
        row = DesignRackPower(design=self.design, planned_rack=self.planned_rack)
        row.full_clean()
        row.save()

    def test_only_rack_saves_fine(self):
        # Regression: the new constraints must not break what works today.
        row = DesignRackPower(design=self.design, rack=self.racks[0])
        row.full_clean()
        row.save()

    def test_unique_design_planned_rack_rejected(self):
        DesignRackPower.objects.create(design=self.design, planned_rack=self.planned_rack)
        dupe = DesignRackPower(design=self.design, planned_rack=self.planned_rack)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                dupe.save()

    def test_unique_design_rack_still_rejected(self):
        # Regression: the existing (design, rack) uniqueness must survive
        # becoming a partial constraint.
        DesignRackPower.objects.create(design=self.design, rack=self.racks[0])
        dupe = DesignRackPower(design=self.design, rack=self.racks[0])
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                dupe.save()

    def test_clean_rejects_planned_rack_in_other_site(self):
        other_site = Site.objects.create(name="Other Site", slug="other-site")
        other_location = Location.objects.create(
            name="Other Location", slug="other-location", site=other_site
        )
        foreign_planned = PlannedRack.objects.create(name="Foreign", location=other_location)
        row = DesignRackPower(design=self.design, planned_rack=foreign_planned)
        with self.assertRaises(ValidationError) as ctx:
            row.full_clean()
        self.assertIn("planned_rack", ctx.exception.message_dict)

    def test_str_does_not_raise_for_planned_rack(self):
        row = DesignRackPower.objects.create(design=self.design, planned_rack=self.planned_rack)
        self.assertIn(str(self.planned_rack), str(row))

    def test_str_does_not_raise_for_real_rack(self):
        row = DesignRackPower.objects.create(design=self.design, rack=self.racks[0])
        self.assertIn(str(self.racks[0]), str(row))

    def test_effective_custom_fields_for_planned_rack(self):
        DesignRackPower.objects.create(
            design=self.design, planned_rack=self.planned_rack,
            power_config={"custom_fields": {"power_limitation": 8000, "pdu_location": "top"}},
        )
        merged, conflict = DesignRackPower.effective_custom_fields(
            self.design, self.planned_rack
        )
        self.assertIsNone(conflict)
        self.assertEqual(merged, {"power_limitation": 8000, "pdu_location": "top"})
