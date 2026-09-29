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

from dcim.choices import RackStatusChoices
from dcim.models import Device, Location, Rack
from django.test import TestCase
from users.models import User

from .. import apply
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import DesignPlacement, PlannedRack
from .utils import create_dcim_environment, make_design


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
        return make_design(title=title, site=self.site)

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

    def test_a_rack_created_by_apply_is_planned_not_active(self):
        """Apply puts every device it creates into 'planned' status -- the
        hardware is not there yet. The rack it creates for them is exactly as
        un-built, so it is 'planned' too; it used to come out as NetBox's
        default, 'active', i.e. claiming a cabinet that is standing in the
        hall. (An ADOPTED rack is a real one and keeps its own status -- D5,
        covered by test_existing_rack_same_location_name_is_adopted_unchanged.)"""
        planned = PlannedRack.objects.create(
            name="Greenfield R2", location=self.location, u_height=20,
        )
        design = self._design("Greenfield status")
        self._add_planned(design, planned, 5, name="new-srv-2", face="front")
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)

        planned.refresh_from_db()
        self.assertEqual(planned.realized_rack.status, RackStatusChoices.STATUS_PLANNED)

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

        # NetBox refuses U40 in a 10U rack on save. run() reports that as a
        # problem (and rolls back) rather than letting it escape as a 500.
        result = apply.run(design, self.superuser)
        self.assertFalse(result.ok, result.problems)
        self.assertTrue(result.problems, "the refusal must be reported")

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


class PlannedFeedApplyTestCase(PlannedRackApplyTestCase):
    """A planned feed is part of the plan like the rack and the PDU on it --
    Apply turns it into a real dcim.PowerFeed (planned, like everything else
    Apply creates) and cables the PDU bound to it. It used to be skipped
    outright: the design showed the supply, DCIM never got it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from dcim.models import DeviceType, PowerPanel, PowerPortTemplate
        cls.panel = PowerPanel.objects.create(site=cls.site, name="PP-1")
        cls.pdu_type = DeviceType.objects.create(
            manufacturer=cls.device_type.manufacturer, model="PDU-T",
            slug="pdu-t", u_height=0)
        PowerPortTemplate.objects.create(device_type=cls.pdu_type, name="input")

    def _pdu_on(self, design, planned_rack, feed, name):
        return DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.pdu_type, target_planned_rack=planned_rack,
            target_position=None, target_face="", proposed_name=name,
            device_role=self.device_role, planned_power_feed=feed,
        )

    def test_apply_creates_the_feed_planned_and_cables_its_pdu(self):
        from dcim.choices import PowerFeedStatusChoices
        from dcim.models import Cable, PowerFeed

        from ..models import DesignPowerFeed

        planned = PlannedRack.objects.create(
            name="Feed R1", location=self.location, u_height=20)
        design = self._design("Feeds")
        feed = DesignPowerFeed.objects.create(
            design=design, planned_rack=planned, name="Feed R1-A",
            voltage=230, amperage=32, power_panel=self.panel)
        self._pdu_on(design, planned, feed, "pdu-a1")
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)

        planned.refresh_from_db()
        real = PowerFeed.objects.get(power_panel=self.panel, name="Feed R1-A")
        self.assertEqual(real.rack, planned.realized_rack)
        self.assertEqual(real.status, PowerFeedStatusChoices.STATUS_PLANNED)
        self.assertEqual((real.voltage, real.amperage), (230, 32))

        pdu = Device.objects.get(name="pdu-a1")
        port = pdu.powerports.get()
        self.assertIsNotNone(port.cable, "the bound PDU must be cabled to its feed")
        self.assertEqual(port.link_peers, [real])

        # Re-applying must not duplicate the feed or the cable.
        again = apply.run(design, self.superuser)
        self.assertTrue(again.ok, again.problems)
        self.assertEqual(PowerFeed.objects.filter(name="Feed R1-A").count(), 1)
        self.assertEqual(Cable.objects.filter(pk=port.cable_id).count(), 1)

    def test_the_apply_page_lists_the_rack_and_the_feed_it_will_create(self):
        """The confirmation page listed devices only, so a design that also
        builds a rack and its supply looked like it built neither -- the
        page a reviewer reads before pressing Apply must say so (and the
        toast after it must count them)."""
        from django.urls import reverse

        from ..models import DesignPowerFeed

        planned = PlannedRack.objects.create(
            name="Page R1", location=self.location, u_height=20)
        design = self._design("Page")
        feed = DesignPowerFeed.objects.create(
            design=design, planned_rack=planned, name="Page R1-A",
            voltage=230, amperage=32, power_panel=self.panel)
        self._pdu_on(design, planned, feed, "page-pdu-a1")
        self._approve(design)

        self.client.force_login(self.superuser)
        url = reverse("plugins:netbox_rack_design:design_apply", kwargs={"pk": design.pk})
        page = self.client.get(url).content.decode()
        self.assertIn("Racks", page)
        self.assertIn("Page R1", page)
        self.assertIn("Power feeds", page)
        self.assertIn("Page R1-A", page)
        self.assertIn("PP-1", page)

        response = self.client.post(url, follow=True)
        toast = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("1 rack created", toast)
        self.assertIn("1 power feed created", toast)

    def test_a_feed_with_no_panel_to_hang_on_is_a_blocker(self):
        from dcim.models import PowerPanel

        from ..models import DesignPowerFeed

        PowerPanel.objects.create(site=self.site, name="PP-2")   # now ambiguous
        planned = PlannedRack.objects.create(
            name="Feed R2", location=self.location, u_height=20)
        design = self._design("No panel")
        DesignPowerFeed.objects.create(
            design=design, planned_rack=planned, name="Feed R2-A")
        self._add_planned(design, planned, 5, name="srv-x")
        self._approve(design)

        result = apply.plan(design, self.superuser)

        self.assertFalse(result.ok, result.problems)
        self.assertTrue(any("Feed R2-A" in p and "panel" in p.lower()
                            for p in result.problems), result.problems)
