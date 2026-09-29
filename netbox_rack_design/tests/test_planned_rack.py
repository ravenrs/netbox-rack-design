"""
Model-level tests for ``PlannedRack`` (PLAN-templates.md §1, task T1.1).

Covers the same shape of thing test_models.py covers for the plugin's other
models: the identity constraint, ``clean()`` validation, the small helper
properties, the module-level ``resolve_rack``/``rack_key`` resolver
(PLAN-templates.md D27), and that ``get_absolute_url()`` actually resolves
(the whole reason this task exists -- see PlannedRack's docstring / the task
brief on ``NoReverseMatch``).
"""

from dcim.models import Location, Rack, Site
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from ..models import PlannedRack, rack_key, resolve_rack


class PlannedRackTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Site 1", slug="site-1")
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )
        cls.other_location = Location.objects.create(
            name="Location 2", slug="location-2", site=cls.site
        )

    def test_unique_location_name_rejects_duplicate(self):
        PlannedRack.objects.create(name="R1", location=self.location)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PlannedRack.objects.create(name="R1", location=self.location)

    def test_same_name_different_location_allowed(self):
        PlannedRack.objects.create(name="R1", location=self.location)
        PlannedRack.objects.create(name="R1", location=self.other_location)
        self.assertEqual(PlannedRack.objects.filter(name="R1").count(), 2)

    def test_clean_rejects_u_height_below_min(self):
        planned = PlannedRack(name="R1", location=self.location, u_height=0)
        with self.assertRaises(ValidationError):
            planned.clean()

    def test_clean_rejects_u_height_above_max(self):
        planned = PlannedRack(name="R1", location=self.location, u_height=101)
        with self.assertRaises(ValidationError):
            planned.clean()

    def test_clean_accepts_normal_u_height(self):
        planned = PlannedRack(name="R1", location=self.location, u_height=42)
        planned.clean()  # must not raise

    def test_site_returns_location_site(self):
        planned = PlannedRack.objects.create(name="R1", location=self.location)
        self.assertEqual(planned.site, self.site)

    def test_is_realized_before_and_after(self):
        planned = PlannedRack.objects.create(name="R1", location=self.location)
        self.assertFalse(planned.is_realized)

        real_rack = Rack.objects.create(name="Real R1", site=self.site, location=self.location)
        planned.realized_rack = real_rack
        planned.save()
        planned.refresh_from_db()
        self.assertTrue(planned.is_realized)

    def test_get_absolute_url_resolves(self):
        planned = PlannedRack.objects.create(name="R1", location=self.location)
        url = planned.get_absolute_url()
        self.assertTrue(url)


class ResolveRackTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Site 1", slug="site-1")
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )
        cls.real_rack = Rack.objects.create(name="Real R1", site=cls.site, location=cls.location)
        cls.unrealized_planned = PlannedRack.objects.create(
            name="Planned R1", location=cls.location
        )
        cls.realized_planned = PlannedRack.objects.create(
            name="Planned R2", location=cls.location, realized_rack=cls.real_rack
        )

    def test_resolve_rack_real_given(self):
        self.assertEqual(resolve_rack(self.real_rack, None), self.real_rack)

    def test_resolve_rack_planned_unrealized(self):
        self.assertIsNone(resolve_rack(None, self.unrealized_planned))

    def test_resolve_rack_planned_realized(self):
        self.assertEqual(resolve_rack(None, self.realized_planned), self.real_rack)

    def test_resolve_rack_neither(self):
        self.assertIsNone(resolve_rack(None, None))

    def test_rack_key_real(self):
        self.assertEqual(rack_key(self.real_rack, None), f"r:{self.real_rack.pk}")

    def test_rack_key_planned(self):
        self.assertEqual(
            rack_key(None, self.unrealized_planned), f"p:{self.unrealized_planned.pk}"
        )

    def test_rack_key_neither(self):
        self.assertIsNone(rack_key(None, None))

    def test_rack_key_distinguishes_same_pk_real_vs_planned(self):
        # A real dcim.Rack and a PlannedRack that happen to share the SAME pk
        # value must NOT collide -- the entire reason rack_key is namespaced
        # (PLAN-templates.md D27). Django models don't need to be saved for
        # rack_key to read their .pk, so a bare unsaved instance with a forced
        # pk is enough to prove the two namespaces never overlap.
        same_pk = 999999
        real = Rack(pk=same_pk)
        planned = PlannedRack(pk=same_pk)
        self.assertEqual(rack_key(real, None), "r:999999")
        self.assertEqual(rack_key(None, planned), "p:999999")
        self.assertNotEqual(rack_key(real, None), rack_key(None, planned))
