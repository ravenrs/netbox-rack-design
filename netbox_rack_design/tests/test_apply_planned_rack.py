"""Tests for applying a design that plans into a ``PlannedRack`` (T1.6,
PLAN-templates.md §1, D4/D5/D7).

Mirrors ``test_apply.py``'s fixture/helper style: build placements while the
design is still draft, then flip ``status`` directly to approve. The only
addition here is a ``dcim.Location`` (``PlannedRack.location`` is mandatory,
see its docstring) and a ``_add_planned`` helper that targets
``target_planned_rack`` instead of ``target_rack``.

D4 says identity is ``(location, name)`` -- exactly ``dcim.Rack``'s own
uniqueness constraint -- so Apply's adopt-or-create lookup is keyed on that
pair throughout. D5 says an adopted rack's own attributes win silently over
whatever the plan said. D7 says the ``PlannedRack`` row survives Apply,
marked ``realized_rack``, and every subsequent Apply must be idempotent
against it (including when the realized rack has since been deleted out from
under it).
"""

from dcim.models import Device, Location, Rack
from django.core.exceptions import ValidationError
from django.test import TestCase
from users.models import User

from .. import apply
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignPlacement, PlannedRack
from .utils import create_dcim_environment


class PlannedRackApplyTestCase(TestCase):
    """Shared fixture + helpers for every test below."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]  # half-depth, u_height=1
        cls.device_role = env["device_role"]
        cls.tenant = env["tenant"]
        cls.superuser = User.objects.create_superuser(username="apply-planned-super")
        cls.location = Location.objects.create(
            name="Location 1", slug="location-1", site=cls.site
        )

    def _design(self, title="Plan"):
        return Design.objects.create(title=title, site=self.site)

    def _approve(self, design):
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        return design

    def _add_planned(self, design, planned_rack, position, *, name="add-1",
                       face="front", device_type=None, role=None):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type or self.device_type,
            target_planned_rack=planned_rack,
            target_position=position,
            target_face=face,
            proposed_name=name,
            device_role=role or self.device_role,
        )


class CreateOnApplyTestCase(PlannedRackApplyTestCase):

    def test_apply_creates_rack_realizes_it_and_places_device(self):
        planned = PlannedRack.objects.create(
            name="Greenfield R1", location=self.location, u_height=20,
        )
        design = self._design("Greenfield")
        self._add_planned(design, planned, 5, name="new-srv", face="front")
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(len(result.created), 1)

        planned.refresh_from_db()
        self.assertIsNotNone(planned.realized_rack)
        rack = planned.realized_rack
        self.assertEqual(rack.name, "Greenfield R1")
        self.assertEqual(rack.location, self.location)
        self.assertEqual(rack.site, self.site)
        self.assertEqual(rack.u_height, 20)

        device = result.created[0].device
        self.assertIsNotNone(device)
        self.assertEqual(device.rack_id, rack.pk)
        self.assertEqual(device.position, 5)
        self.assertEqual(device.face, "front")
        self.assertEqual(device.name, "new-srv")

        # Per-rack reporting (extends ApplyResult in the same style as
        # created/updated/removed): this rack was CREATED, not adopted.
        self.assertEqual(len(result.resolved_racks), 1)
        entry = result.resolved_racks[0]
        self.assertEqual(entry.planned_rack, planned)
        self.assertTrue(entry.created)
        self.assertEqual(entry.rack, rack)


class AdoptOnApplyTestCase(PlannedRackApplyTestCase):

    def test_existing_rack_same_location_name_is_adopted_unchanged(self):
        existing = Rack.objects.create(
            name="Shared R1", site=self.site, location=self.location, u_height=47,
        )
        planned = PlannedRack.objects.create(
            name="Shared R1", location=self.location, u_height=42,
        )
        design = self._design("Adopt")
        self._add_planned(design, planned, 40, name="adopted-srv")
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)

        # No second rack was created.
        self.assertEqual(
            Rack.objects.filter(location=self.location, name="Shared R1").count(), 1,
        )
        planned.refresh_from_db()
        self.assertEqual(planned.realized_rack_id, existing.pk)

        # D5: the existing rack's own attributes are untouched -- still 47U,
        # not silently rewritten to the plan's 42U.
        existing.refresh_from_db()
        self.assertEqual(existing.u_height, 47)

        # D5: the real 47U rack wins, so U40 (which would have been out of
        # bounds for the 42U plan) resolves against the real rack and lands.
        device = result.created[0].device
        self.assertEqual(device.rack_id, existing.pk)
        self.assertEqual(device.position, 40)

        self.assertEqual(len(result.resolved_racks), 1)
        self.assertFalse(result.resolved_racks[0].created)
        self.assertEqual(result.resolved_racks[0].rack, existing)

    def test_adopted_rack_too_short_for_device_fails_apply(self):
        Rack.objects.create(
            name="Tiny R1", site=self.site, location=self.location, u_height=10,
        )
        planned = PlannedRack.objects.create(
            name="Tiny R1", location=self.location, u_height=42,
        )
        design = self._design("Too tall")
        self._add_planned(design, planned, 40, name="wont-fit")
        self._approve(design)

        with self.assertRaises(ValidationError):
            apply.run(design, self.superuser)

        # All-or-nothing: nothing committed, not even the rack adoption.
        self.assertFalse(Device.objects.filter(name="wont-fit").exists())
        planned.refresh_from_db()
        self.assertIsNone(planned.realized_rack)


class IdempotencyTestCase(PlannedRackApplyTestCase):

    def test_apply_twice_creates_only_one_rack(self):
        planned = PlannedRack.objects.create(
            name="Idem R1", location=self.location, u_height=20,
        )
        design = self._design("Idempotent greenfield")
        self._add_planned(design, planned, 3, name="idem-srv")
        self._approve(design)

        first = apply.run(design, self.superuser)
        self.assertTrue(first.ok, first.problems)
        self.assertEqual(
            Rack.objects.filter(location=self.location, name="Idem R1").count(), 1,
        )

        second = apply.run(design, self.superuser)
        self.assertTrue(second.ok, second.problems)
        self.assertEqual(second.created, [])
        self.assertEqual(
            Rack.objects.filter(location=self.location, name="Idem R1").count(), 1,
        )

    def test_falls_back_to_lookup_when_realized_rack_deleted(self):
        # Simulates a planned rack that was already realized by a prior
        # apply, whose real rack was then deleted directly in DCIM.
        # ``Rack.realized_rack`` is SET_NULL, so Django itself nulls the FK
        # the moment ``ghost`` is deleted -- the point of this test is that
        # Apply's resolution copes with that instead of crashing on a stale
        # reference, falling through to the (location, name) lookup exactly
        # as a never-realized planned rack would (D4), and adopting whatever
        # now sits at that identity rather than creating a second rack.
        ghost = Rack.objects.create(
            name="Ghost R1", site=self.site, location=self.location, u_height=10,
        )
        planned = PlannedRack.objects.create(
            name="Ghost R1", location=self.location, u_height=10, realized_rack=ghost,
        )
        ghost.delete()
        replacement = Rack.objects.create(
            name="Ghost R1", site=self.site, location=self.location, u_height=15,
        )

        design = self._design("Fallback")
        self._add_planned(design, planned, 5, name="fallback-srv")
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(
            Rack.objects.filter(location=self.location, name="Ghost R1").count(), 1,
        )
        planned.refresh_from_db()
        self.assertEqual(planned.realized_rack_id, replacement.pk)
        self.assertEqual(result.created[0].device.rack_id, replacement.pk)


class FrozenGuardTestCase(PlannedRackApplyTestCase):

    def test_draft_design_with_planned_rack_still_refuses_to_apply(self):
        planned = PlannedRack.objects.create(
            name="Frozen R1", location=self.location, u_height=10,
        )
        design = self._design("Not yet approved")
        self._add_planned(design, planned, 2, name="frozen-srv")
        # NOT approved -- design.is_frozen is False, exactly the guard the
        # real-rack path already relies on (see test_apply.py's
        # test_draft_design_reports_problem).

        result = apply.plan(design, self.superuser)
        self.assertFalse(result.ok)
        self.assertTrue(any("not approved" in p for p in result.problems), result.problems)

        planned.refresh_from_db()
        self.assertIsNone(planned.realized_rack)
        self.assertFalse(
            Rack.objects.filter(location=self.location, name="Frozen R1").exists()
        )
